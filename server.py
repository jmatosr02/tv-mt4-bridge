import os
import time
import uuid
from collections import deque
from flask import Flask, request, jsonify

app = Flask(__name__)

SECRET = os.environ.get("SECRET", "")
QUEUE = deque(maxlen=200)

def bad(msg, code=400):
    return jsonify({"ok": False, "error": msg}), code

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

    if symbol != "XAUUSD.pro":
        return bad("symbol not allowed")

    if side not in ("buy", "sell"):
        return bad("side invalid")

    valid_types = {"market", "buy_limit", "buy_stop", "sell_limit", "sell_stop"}
    if ordertype not in valid_types:
        return bad("ordertype invalid")

    price = None
    if ordertype != "market":
        if data.get("price") is None:
            return bad("price required for pending orders")
        try:
            price = float(data["price"])
        except Exception:
            return bad("price invalid")

    signal = {
        "id": str(uuid.uuid4()),
        "ts": int(time.time()),
        "symbol": symbol,
        "side": side,
        "ordertype": ordertype,
        "price": price,
        "timeframe": str(data.get("timeframe", "15")),
        "strategy": str(data.get("strategy", "TV-XAU")),
    }
    QUEUE.append(signal)
    return jsonify({"ok": True, "queued": signal["id"]})

@app.get("/next")
def next_signal():
    if not QUEUE:
        return jsonify({"ok": True, "signal": None})
    return jsonify({"ok": True, "signal": QUEUE[0]})

@app.post("/pop")
def pop_signal():
    data = request.get_json(silent=True) or {}

    if data.get("secret") != SECRET:
        return bad("unauthorized", 401)

    sid = data.get("id")
    if not QUEUE:
        return jsonify({"ok": True, "popped": None})

    if sid and QUEUE[0]["id"] != sid:
        return bad("id mismatch", 409)

    popped = QUEUE.popleft()
    return jsonify({"ok": True, "popped": popped["id"]})
