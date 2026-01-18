import os
import time
import uuid
from collections import deque

import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# =========================
# ENV
# =========================
SECRET = os.environ.get("SECRET", "")

TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")

# =========================
# IN-MEMORY STATE
# =========================
QUEUE = deque(maxlen=200)   # guarda ids en orden
SIGNALS = {}                # id -> signal dict


def bad(msg, code=400):
    return jsonify({"ok": False, "error": msg}), code


def tg_api(method: str, payload: dict):
    """
    Llama Telegram Bot API usando el token que está corriendo en Render.
    Retorna JSON dict o None si falla.
    """
    if not TG_BOT_TOKEN:
        return {"ok": False, "error": "TG_BOT_TOKEN missing"}

    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/{method}"
    try:
        r = requests.post(url, json=payload, timeout=15)
        # Telegram siempre responde JSON
        return r.json()
    except Exception as e:
        return {"ok": False, "error": f"exception: {str(e)}"}


def tg_send_approval(signal: dict):
    """
    Envía mensaje con botones Aceptar / Denegar al chat autorizado.
    """
    if not TG_CHAT_ID:
        return

    sid = signal["id"]
    symbol = signal.get("symbol", "")
    side = signal.get("side", "")
    ordertype = signal.get("ordertype", "")
    timeframe = signal.get("timeframe", "")
    strategy = signal.get("strategy", "")
    price = signal.get("price")

    txt = (
        "📌 *Señal pendiente*\n"
        f"*Symbol:* {symbol}\n"
        f"*Side:* {side}\n"
        f"*Type:* {ordertype}\n"
        + (f"*Price:* {price}\n" if price else "")
        + f"*TF:* {timeframe}\n"
        f"*Strategy:* {strategy}\n\n"
        "Responde con los botones:"
    )

    keyboard = {
        "inline_keyboard": [
            [
                {"text": "✅ ACEPTAR", "callback_data": f"APPROVE:{sid}"},
                {"text": "❌ DENEGAR", "callback_data": f"DENY:{sid}"},
            ]
        ]
    }

    tg_api(
        "sendMessage",
        {
            "chat_id": TG_CHAT_ID,
            "text": txt,
            "parse_mode": "Markdown",
            "reply_markup": keyboard,
        },
    )


# =========================
# HEALTH
# =========================
@app.get("/health")
def health():
    return jsonify(
        {
            "ok": True,
            "pending": len(QUEUE),
            "has_secret": bool(SECRET),
            "has_tg_token": bool(TG_BOT_TOKEN),
            "has_tg_chat": bool(TG_CHAT_ID),
        }
    )


# =========================
# TELEGRAM TEST (IMPORTANT)
# =========================
@app.get("/tg_test")
def tg_test():
    """
    Envía un mensaje usando el TG_BOT_TOKEN que está ACTIVO en Render.
    Si aquí falla con 401 => Render tiene token viejo o mal pegado.
    """
    if not TG_CHAT_ID:
        return jsonify({"ok": False, "error": "TG_CHAT_ID missing"}), 400

    resp = tg_api("sendMessage", {"chat_id": TG_CHAT_ID, "text": "✅ TG_TEST desde Render"})
    return jsonify({"ok": True, "telegram_response": resp})


# =========================
# TRADINGVIEW WEBHOOK
# =========================
@app.post("/tv")
def tv_webhook():
    data = request.get_json(silent=True) or {}

    if not SECRET:
        return bad("server SECRET not set", 500)

    if data.get("secret") != SECRET:
        return bad("unauthorized", 401)

    symbol = (data.get("symbol") or "").strip()
    side = (data.get("side") or "").lower().strip()
    ordertype = (data.get("ordertype") or "").lower().strip()
    timeframe = str(data.get("timeframe") or "").strip()
    strategy = (data.get("strategy") or "").strip()
    price = data.get("price", None)

    if not symbol:
        return bad("missing symbol")
    if side not in ("buy", "sell"):
        return bad("invalid side")
    if not ordertype:
        return bad("missing ordertype")

    sid = str(uuid.uuid4())
    signal = {
        "id": sid,
        "symbol": symbol,
        "side": side,
        "ordertype": ordertype,
        "timeframe": timeframe,
        "strategy": strategy,
        "price": price,
        "ts": int(time.time()),
        "status": "pending",  # pending -> approved/denied
    }

    SIGNALS[sid] = signal
    QUEUE.append(sid)

    # Envía Telegram con botones
    tg_send_approval(signal)

    return jsonify({"ok": True, "id": sid, "pending": len(QUEUE)})


# =========================
# NEXT SIGNAL
# =========================
@app.get("/next")
def next_signal():
    """
    Devuelve la próxima señal en cola.
    El EA debe operar SOLO si status == "approved".
    """
    if not QUEUE:
        return jsonify({"ok": True, "signal": None})

    sid = QUEUE[0]
    sig = SIGNALS.get(sid)
    if not sig:
        QUEUE.popleft()
        return jsonify({"ok": True, "signal": None})

    return jsonify({"ok": True, "signal": sig})


# =========================
# POP (remove a specific signal)
# =========================
@app.post("/pop")
def pop_signal():
    data = request.get_json(silent=True) or {}
    if data.get("secret") != SECRET:
        return bad("unauthorized", 401)

    sid = data.get("id")
    if not sid:
        return bad("missing id")

    try:
        QUEUE.remove(sid)
    except Exception:
        pass

    SIGNALS.pop(sid, None)
    return jsonify({"ok": True, "pending": len(QUEUE)})


# =========================
# TELEGRAM WEBHOOK (buttons)
# =========================
@app.post("/tg")
def telegram_webhook():
    upd = request.get_json(silent=True) or {}

    cq = upd.get("callback_query")
    if not cq:
        # No es botón, lo ignoramos
        return jsonify({"ok": True})

    cb_id = cq.get("id")
    data = cq.get("data", "")

    msg = cq.get("message", {}) or {}
    chat = msg.get("chat", {}) or {}
    chat_id = str(chat.get("id", ""))

    # Seguridad: solo permitir tu chat
    if TG_CHAT_ID and chat_id != str(TG_CHAT_ID):
        tg_api("answerCallbackQuery", {"callback_query_id": cb_id, "text": "No autorizado"})
        return jsonify({"ok": True})

    if ":" not in data:
        tg_api("answerCallbackQuery", {"callback_query_id": cb_id, "text": "Acción inválida"})
        return jsonify({"ok": True})

    action, sid = data.split(":", 1)
    sig = SIGNALS.get(sid)
    if not sig:
        tg_api("answerCallbackQuery", {"callback_query_id": cb_id, "text": "Señal no encontrada"})
        return jsonify({"ok": True})

    if action == "APPROVE":
        sig["status"] = "approved"
        tg_api("answerCallbackQuery", {"callback_query_id": cb_id, "text": "✅ Aceptada"})
        tg_api("sendMessage", {"chat_id": TG_CHAT_ID, "text": "✅ Señal aprobada. MT4 puede ejecutar en sesión."})

    elif action == "DENY":
        sig["status"] = "denied"
        # Remover de cola y borrar
        try:
            QUEUE.remove(sid)
        except Exception:
            pass
        SIGNALS.pop(sid, None)

        tg_api("answerCallbackQuery", {"callback_query_id": cb_id, "text": "❌ Denegada"})
        tg_api("sendMessage", {"chat_id": TG_CHAT_ID, "text": "❌ Señal denegada y removida."})

    else:
        tg_api("answerCallbackQuery", {"callback_query_id": cb_id, "text": "Acción inválida"})

    return jsonify({"ok": True})


# =========================
# MAIN
# =========================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
