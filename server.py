import os
import time
import uuid
from collections import deque
from flask import Flask, request, jsonify
import requests

app = Flask(__name__)

SECRET = os.environ.get("SECRET", "")

TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")

# Cola de señales pendientes
QUEUE = deque(maxlen=200)

# Señales guardadas por id (para aprobar/denegar desde Telegram)
SIGNALS = {}  # id -> dict


def bad(msg, code=400):
    return jsonify({"ok": False, "error": msg}), code


def tg_api(method: str, payload: dict):
    if not TG_BOT_TOKEN:
        return None
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/{method}"
    try:
        r = requests.post(url, json=payload, timeout=15)
        return r.json()
    except Exception:
        return None


def tg_send_approval(signal: dict):
    """
    Envía mensaje con botones Aceptar / Denegar
    """
    if not (TG_BOT_TOKEN and TG_CHAT_ID):
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


@app.get("/health")
def health():
    return jsonify({"ok": True, "pending": len(QUEUE)})


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

    if not symbol or side not in ("buy", "sell") or not ordertype:
        return bad("missing/invalid fields")

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

    # Telegram notify
    tg_send_approval(signal)

    return jsonify({"ok": True, "id": sid, "pending": len(QUEUE)})


@app.get("/next")
def next_signal():
    """
    Devuelve la próxima señal (aunque sea pending) para diagnóstico.
    El EA debe respetar el campo status.
    """
    if not QUEUE:
        return jsonify({"ok": True, "signal": None})

    sid = QUEUE[0]
    sig = SIGNALS.get(sid)
    if not sig:
        QUEUE.popleft()
        return jsonify({"ok": True, "signal": None})

    return jsonify({"ok": True, "signal": sig})


@app.post("/pop")
def pop_signal():
    """
    Quita una señal específica de la cola (limpiar)
    """
    data = request.get_json(silent=True) or {}
    if data.get("secret") != SECRET:
        return bad("unauthorized", 401)

    sid = data.get("id")
    if not sid:
        return bad("missing id")

    # remover de cola
    try:
        QUEUE.remove(sid)
    except Exception:
        pass

    if sid in SIGNALS:
        SIGNALS.pop(sid, None)

    return jsonify({"ok": True, "pending": len(QUEUE)})


@app.post("/tg")
def telegram_webhook():
    """
    Recibe callbacks de botones inline (Aceptar/Denegar)
    """
    upd = request.get_json(silent=True) or {}

    # Callback query (botones)
    cq = upd.get("callback_query")
    if cq:
        data = cq.get("data", "")
        cb_id = cq.get("id")
        msg = cq.get("message", {})
        chat = msg.get("chat", {})
        chat_id = str(chat.get("id", ""))

        # Solo aceptar acciones del chat autorizado
        if TG_CHAT_ID and chat_id != str(TG_CHAT_ID):
            tg_api("answerCallbackQuery", {"callback_query_id": cb_id, "text": "No autorizado"})
            return jsonify({"ok": True})

        if ":" in data:
            action, sid = data.split(":", 1)
        else:
            action, sid = data, ""

        sig = SIGNALS.get(sid)
        if not sig:
            tg_api("answerCallbackQuery", {"callback_query_id": cb_id, "text": "Señal no encontrada"})
            return jsonify({"ok": True})

        if action == "APPROVE":
            sig["status"] = "approved"
            tg_api("answerCallbackQuery", {"callback_query_id": cb_id, "text": "✅ Aceptada"})
            tg_api(
                "sendMessage",
                {
                    "chat_id": TG_CHAT_ID,
                    "text": f"✅ Señal aprobada: {sig['symbol']} {sig['side']} ({sig['ordertype']})",
                },
            )
        elif action == "DENY":
            sig["status"] = "denied"
            # sacarla de la cola
            try:
                QUEUE.remove(sid)
            except Exception:
                pass
            SIGNALS.pop(sid, None)

            tg_api("answerCallbackQuery", {"callback_query_id": cb_id, "text": "❌ Denegada"})
            tg_api(
                "sendMessage",
                {
                    "chat_id": TG_CHAT_ID,
                    "text": f"❌ Señal denegada y removida.",
                },
            )
        else:
            tg_api("answerCallbackQuery", {"callback_query_id": cb_id, "text": "Acción inválida"})

        return jsonify({"ok": True})

    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
