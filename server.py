from flask import Flask, request, jsonify
from datetime import datetime
import threading
import os
import requests

app = Flask(__name__)

# -----------------------
# ENV (SIEMPRE desde Render)
# -----------------------
def get_env():
    """
    Lee variables de entorno.
    - TG_BOT_TOKEN es el nombre correcto (como lo tienes en Render)
    - TG_TOKEN se deja como fallback por compatibilidad
    """
    secret = os.getenv("SECRET", "").strip()
    tg_token = (os.getenv("TG_BOT_TOKEN", "") or os.getenv("TG_TOKEN", "")).strip()
    tg_chat_id = os.getenv("TG_CHAT_ID", "").strip()
    return secret, tg_token, tg_chat_id

# -----------------------
# In-memory queue
# -----------------------
_queue = []
_lock = threading.Lock()

def now_iso():
    return datetime.utcnow().isoformat() + "Z"

def ok_env():
    secret, tg_token, tg_chat_id = get_env()
    return {
        "has_secret": bool(secret),
        "has_tg_token": bool(tg_token),
        "has_tg_chat": bool(tg_chat_id),
    }

def tg_send(text: str) -> bool:
    """Send Telegram message. Returns True/False."""
    _, tg_token, tg_chat_id = get_env()
    if not (tg_token and tg_chat_id):
        return False

    try:
        url = f"https://api.telegram.org/bot{tg_token}/sendMessage"
        payload = {
            "chat_id": tg_chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        r = requests.post(url, json=payload, timeout=15)
        return r.ok
    except Exception:
        return False

def auth_ok(req_json: dict) -> bool:
    secret, _, _ = get_env()
    return bool(secret) and req_json.get("secret") == secret

# -----------------------
# ROUTES
# -----------------------
@app.get("/health")
def health():
    with _lock:
        pending = len(_queue)

    return jsonify({
        "ok": True,
        "pending": pending,
        **ok_env(),
        "time": now_iso(),
    })

@app.post("/tv")
def tv():
    """
    Receives TradingView webhook JSON and queues it.
    IMPORTANT: No Telegram here (signals do NOT notify).
    Expected JSON:
      secret, symbol, side, ordertype, timeframe, strategy
    Optional:
      price (for limit/stop), meta (string)
    """
    data = request.get_json(silent=True) or {}
    if not auth_ok(data):
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    # Minimal validation
    symbol = str(data.get("symbol", "")).strip()
    side = str(data.get("side", "")).strip().lower()
    ordertype = str(data.get("ordertype", "market")).strip().lower()

    if symbol == "" or side not in ("buy", "sell"):
        return jsonify({"ok": False, "error": "bad_request"}), 400

    item = {
        "id": f"sig_{int(datetime.utcnow().timestamp()*1000)}",
        "ts": now_iso(),
        "symbol": symbol,
        "side": side,
        "ordertype": ordertype,
        "timeframe": str(data.get("timeframe", "")),
        "strategy": str(data.get("strategy", "")),
        "price": data.get("price"),
        "meta": data.get("meta", ""),
    }

    with _lock:
        _queue.append(item)

    return jsonify({"ok": True, "queued": item["id"]})

@app.get("/next")
def next_signal():
    """Returns the next queued signal without removing it."""
    with _lock:
        if not _queue:
            return jsonify({"ok": True, "signal": None})
        return jsonify({"ok": True, "signal": _queue[0]})

@app.post("/pop")
def pop_signal():
    """
    Removes a signal by id (or pops first if id not provided).
    Body: { "secret": "...", "id": "sig_..." }
    """
    data = request.get_json(silent=True) or {}
    if not auth_ok(data):
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    sig_id = str(data.get("id", "")).strip()

    with _lock:
        if not _queue:
            return jsonify({"ok": True, "removed": None})

        if sig_id == "":
            removed = _queue.pop(0)
            return jsonify({"ok": True, "removed": removed["id"]})

        for i, s in enumerate(_queue):
            if s.get("id") == sig_id:
                _queue.pop(i)
                return jsonify({"ok": True, "removed": sig_id})

    return jsonify({"ok": True, "removed": None})

@app.get("/tg_ping")
def tg_ping():
    """
    Quick Telegram test.
    Visit in browser: /tg_ping?secret=...
    """
    req_secret = request.args.get("secret", "").strip()
    secret, _, _ = get_env()

    if not (secret and req_secret == secret):
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    ok = tg_send("✅ Ping OK desde tu bridge (Render).")
    return jsonify({"ok": ok})

@app.post("/trade_event")
def trade_event():
    """
    Telegram notifications ONLY for trade open/close.
    Body:
    {
      "secret": "...",
      "event": "OPEN" | "CLOSE",
      "symbol": "XAUUSD.pro",
      "side": "buy|sell",
      "lot": 0.01,
      "ticket": 12345,
      "price": 1234.56,
      "sl": 1230.00,
      "tp": 1240.00,
      "profit": -4.20,              (CLOSE only)
      "reason": "TP|SL|MANUAL|OTHER" (CLOSE only)
    }
    """
    data = request.get_json(silent=True) or {}
    if not auth_ok(data):
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    event = str(data.get("event", "")).upper().strip()
    symbol = str(data.get("symbol", "")).strip()
    side = str(data.get("side", "")).strip().lower()
    lot = data.get("lot", "")
    ticket = data.get("ticket", "")
    price = data.get("price", "")
    sl = data.get("sl", "")
    tp = data.get("tp", "")

    if event not in ("OPEN", "CLOSE"):
        return jsonify({"ok": False, "error": "bad_event"}), 400

    if event == "OPEN":
        msg = (
            f"📈 Trade ABIERTO\n"
            f"• {symbol} ({side.upper()})\n"
            f"• Lote: {lot}\n"
            f"• Ticket: {ticket}\n"
            f"• Entrada: {price}\n"
            f"• SL: {sl}\n"
            f"• TP: {tp}\n"
        )
    else:
        profit = data.get("profit", "")
        reason = str(data.get("reason", "OTHER")).upper().strip()
        msg = (
            f"✅ Trade CERRADO\n"
            f"• {symbol} ({side.upper()})\n"
            f"• Ticket: {ticket}\n"
            f"• Cierre: {price}\n"
            f"• P/L: {profit}\n"
            f"• Razón: {reason}\n"
        )

    ok = tg_send(msg)
    return jsonify({"ok": ok})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
