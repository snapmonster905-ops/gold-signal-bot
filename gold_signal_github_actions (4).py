"""
AI-Style Gold (XAU/USD) Buy/Sell Signal Bot — GitHub Actions Version (Interactive)
====================================================================================
This version runs on a SCHEDULE (every 15 minutes via GitHub Actions) and does TWO things
each time it runs:

1. AUTOMATIC ALERTS: Checks the 6-layer framework (EMA, MACD, RSI, VWAP, ADX, session
   timing). If all conditions line up for a high-confidence BUY or SELL, it sends you
   a Telegram alert automatically - no action needed from you.

2. ON-DEMAND REPLIES: Checks if you've sent the bot any new message since the last run
   (e.g. "/start", "target", "status"). If so, it immediately replies with the CURRENT
   market status and target levels - even if it's not a high-confidence signal - so you
   always get an answer within ~15 minutes of asking.

Because this only runs on a schedule (not a constantly-listening server), replies are
not instant - expect up to a 15-minute delay (matching how often GitHub Actions runs
this). This keeps everything at ZERO cost - no server, no VPS, no card needed anywhere.

Uses the 6-Layer Framework:
  1. EMA-50        -> Trend direction
  2. MACD          -> Momentum
  3. RSI (14)      -> Overbought/Oversold
  4. VWAP          -> Intraday fair value / bias
  5. ADX (14)      -> Trend strength (only auto-alert when ADX >= 25)
  6. Session time  -> Only auto-alert during London/NY overlap hours

State (last signal sent + last processed Telegram message) is stored in a small
JSON file that GitHub Actions commits back to the repo after each run.

Data source: Crypto.com Exchange public market-data API (free, no key needed).
"""

import os
import sys
import json
import requests
from datetime import datetime

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "")
INSTRUMENT = os.environ.get("INSTRUMENT", "XAUUSD-PERP")
TIMEFRAME = os.environ.get("TIMEFRAME", "15m")
STATE_FILE = "bot_state.json"

EMA_PERIOD = 50
RSI_PERIOD = 14
ATR_PERIOD = 14
ADX_PERIOD = 14
ADX_MIN_THRESHOLD = 25
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9

# Any incoming message containing one of these words (case-insensitive) triggers
# an on-demand "current status" reply.
TRIGGER_WORDS = ["start", "target", "status", "signal", "price", "gold"]

CANDLESTICK_URL = "https://api.crypto.com/exchange/v1/public/get-candlestick"
TELEGRAM_SEND_URL = "https://api.telegram.org/bot{token}/sendMessage"
TELEGRAM_GET_UPDATES_URL = "https://api.telegram.org/bot{token}/getUpdates"


# ============================================================
# DATA FETCHING
# ============================================================

def fetch_candles(instrument, timeframe, count=150):
    params = {"instrument_name": instrument, "timeframe": timeframe, "count": count}
    resp = requests.get(CANDLESTICK_URL, params=params, timeout=15)
    resp.raise_for_status()
    candles = resp.json()["result"]["data"]
    candles.sort(key=lambda c: c["t"])
    # Crypto.com's API returns price/volume fields as strings (e.g. "4434.40").
    # Convert them to floats here so all downstream math works correctly.
    for c in candles:
        c["o"] = float(c["o"])
        c["h"] = float(c["h"])
        c["l"] = float(c["l"])
        c["c"] = float(c["c"])
        c["v"] = float(c["v"])
    return candles


# ============================================================
# INDICATORS
# ============================================================

def ema(values, period):
    out = [None] * len(values)
    if len(values) < period:
        return out
    k = 2 / (period + 1)
    out[period - 1] = sum(values[:period]) / period
    for i in range(period, len(values)):
        out[i] = (values[i] - out[i - 1]) * k + out[i - 1]
    return out


def rsi(values, period=14):
    out = [None] * len(values)
    if len(values) < period + 1:
        return out
    gains, losses = [], []
    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    rs = avg_gain / avg_loss if avg_loss != 0 else 0
    out[period] = 100 - (100 / (1 + rs)) if avg_loss != 0 else 100
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rs = avg_gain / avg_loss if avg_loss != 0 else 0
        out[i + 1] = 100 - (100 / (1 + rs)) if avg_loss != 0 else 100
    return out


def macd(values, fast=12, slow=26, signal=9):
    ema_fast = ema(values, fast)
    ema_slow = ema(values, slow)
    macd_line = [None] * len(values)
    for i in range(len(values)):
        if ema_fast[i] is not None and ema_slow[i] is not None:
            macd_line[i] = ema_fast[i] - ema_slow[i]
    clean = [v for v in macd_line if v is not None]
    signal_clean = ema(clean, signal)
    signal_line = [None] * len(values)
    offset = len(values) - len(clean)
    for i, v in enumerate(signal_clean):
        if v is not None:
            signal_line[offset + i] = v
    return macd_line, signal_line


def atr(candles, period=14):
    trs = []
    for i in range(1, len(candles)):
        h, l, prev_c = candles[i]["h"], candles[i]["l"], candles[i - 1]["c"]
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        trs.append(tr)
    if len(trs) < period:
        return None
    return sum(trs[-period:]) / period


def calculate_adx(candles, period=14):
    """Wilder's ADX - measures trend STRENGTH (not direction).
    ADX >= 25 = strong/tradeable trend. Below 20 = choppy, skip trading."""
    highs = [c["h"] for c in candles]
    lows = [c["l"] for c in candles]
    closes = [c["c"] for c in candles]

    trs, plus_dm, minus_dm = [], [], []
    for i in range(1, len(candles)):
        high, low, prev_close = highs[i], lows[i], closes[i - 1]
        prev_high, prev_low = highs[i - 1], lows[i - 1]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
        up_move = high - prev_high
        down_move = prev_low - low
        plus_dm.append(up_move if (up_move > down_move and up_move > 0) else 0)
        minus_dm.append(down_move if (down_move > up_move and down_move > 0) else 0)

    if len(trs) < period * 2:
        return None

    def wilder_smooth(values, period):
        smoothed = [None] * len(values)
        smoothed[period - 1] = sum(values[:period])
        for i in range(period, len(values)):
            smoothed[i] = smoothed[i - 1] - (smoothed[i - 1] / period) + values[i]
        return smoothed

    tr_s = wilder_smooth(trs, period)
    plus_dm_s = wilder_smooth(plus_dm, period)
    minus_dm_s = wilder_smooth(minus_dm, period)

    dx = [None] * len(trs)
    for i in range(period - 1, len(trs)):
        if tr_s[i]:
            plus_di = 100 * (plus_dm_s[i] / tr_s[i])
            minus_di = 100 * (minus_dm_s[i] / tr_s[i])
            di_sum = plus_di + minus_di
            dx[i] = 100 * abs(plus_di - minus_di) / di_sum if di_sum != 0 else 0

    dx_clean = [v for v in dx if v is not None]
    if len(dx_clean) < period:
        return None

    adx_val = sum(dx_clean[:period]) / period
    for i in range(period, len(dx_clean)):
        adx_val = (adx_val * (period - 1) + dx_clean[i]) / period
    return adx_val


def in_trading_session():
    """Only auto-alert during London / London-NY overlap sessions
    (12:30 PM - 9:30 PM IST = 07:00 - 16:00 UTC)."""
    utc_hour = datetime.utcnow().hour
    return 7 <= utc_hour < 16


def session_vwap(candles):
    cum_pv, cum_v = 0, 0
    for c in candles:
        typical = (c["h"] + c["l"] + c["c"]) / 3
        cum_pv += typical * c["v"]
        cum_v += c["v"]
    return cum_pv / cum_v if cum_v > 0 else None


# ============================================================
# ANALYSIS
# ============================================================

def analyze(candles):
    closes = [c["c"] for c in candles]
    price = closes[-1]

    ema_vals = ema(closes, EMA_PERIOD)
    rsi_vals = rsi(closes, RSI_PERIOD)
    macd_line, signal_line = macd(closes, MACD_FAST, MACD_SLOW, MACD_SIGNAL)
    atr_val = atr(candles, ATR_PERIOD)
    vwap_val = session_vwap(candles[-96:])
    adx_val = calculate_adx(candles, ADX_PERIOD)

    ema_val = ema_vals[-1]
    rsi_val = rsi_vals[-1]
    macd_val, macd_sig = macd_line[-1], signal_line[-1]

    if None in (ema_val, rsi_val, macd_val, macd_sig, vwap_val, atr_val, adx_val):
        return None

    bullish, bearish = 0, 0
    if price > ema_val: bullish += 1
    else: bearish += 1
    if macd_val > macd_sig: bullish += 1
    else: bearish += 1
    if rsi_val > 50: bullish += 1
    else: bearish += 1
    if price > vwap_val: bullish += 1
    else: bearish += 1

    strong_trend = adx_val >= ADX_MIN_THRESHOLD
    good_session = in_trading_session()

    signal = "HOLD"
    skip_reason = None
    if bullish == 4:
        if strong_trend and good_session:
            signal = "BUY"
        else:
            skip_reason = f"Bullish 4/4 but filtered out (ADX={adx_val:.1f}, session_ok={good_session})"
    elif bearish == 4:
        if strong_trend and good_session:
            signal = "SELL"
        else:
            skip_reason = f"Bearish 4/4 but filtered out (ADX={adx_val:.1f}, session_ok={good_session})"

    return {
        "signal": signal, "price": price, "ema": ema_val, "rsi": rsi_val,
        "macd": macd_val, "macd_signal": macd_sig, "vwap": vwap_val,
        "atr": atr_val, "adx": adx_val, "good_session": good_session,
        "bullish_votes": bullish, "bearish_votes": bearish,
        "skip_reason": skip_reason,
    }


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram_message(text):
    url = TELEGRAM_SEND_URL.format(token=BOT_TOKEN)
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"}
    r = requests.post(url, data=payload, timeout=15)
    if r.status_code != 200:
        print(f"[ERROR] Telegram send failed: {r.text}")
    else:
        print("[SENT] Telegram message delivered.")


def get_new_updates(last_update_id):
    """Fetch any new messages sent to the bot since last_update_id."""
    url = TELEGRAM_GET_UPDATES_URL.format(token=BOT_TOKEN)
    params = {"offset": last_update_id + 1, "timeout": 0} if last_update_id else {"timeout": 0}
    r = requests.get(url, params=params, timeout=15)
    if r.status_code != 200:
        print(f"[ERROR] getUpdates failed: {r.text}")
        return [], last_update_id
    updates = r.json().get("result", [])
    new_max_id = last_update_id
    triggered = False
    for u in updates:
        new_max_id = max(new_max_id, u["update_id"])
        msg = u.get("message", {})
        text = msg.get("text", "").lower()
        if any(word in text for word in TRIGGER_WORDS):
            triggered = True
    return triggered, new_max_id


# ============================================================
# MESSAGE FORMATTING
# ============================================================

def format_alert(a):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    emoji = "🟢" if a["signal"] == "BUY" else "🔴"
    votes = a["bullish_votes"] if a["signal"] == "BUY" else a["bearish_votes"]

    if a["signal"] == "BUY":
        sl = a["price"] - (1.5 * a["atr"])
        t1 = a["price"] + (1.5 * a["atr"])
        t2 = a["price"] + (3 * a["atr"])
    else:
        sl = a["price"] + (1.5 * a["atr"])
        t1 = a["price"] - (1.5 * a["atr"])
        t2 = a["price"] - (3 * a["atr"])

    return (
        f"{emoji} *{a['signal']} SIGNAL - GOLD (XAU/USD)*\n"
        f"Confidence: {votes}/4 indicators agree + ADX confirmed + good session\n\n"
        f"Price: ${a['price']:,.2f}\n"
        f"RSI(14): {a['rsi']:.1f}\n"
        f"MACD: {a['macd']:.2f} (Signal: {a['macd_signal']:.2f})\n"
        f"EMA-50: ${a['ema']:,.2f}\n"
        f"VWAP: ${a['vwap']:,.2f}\n"
        f"ADX(14): {a['adx']:.1f} (strong trend ✓)\n"
        f"ATR(14): ${a['atr']:.2f}\n\n"
        f"*Suggested (ATR-based):*\n"
        f"Entry: ~${a['price']:,.2f}\n"
        f"SL: ${sl:,.2f}\n"
        f"T1: ${t1:,.2f} | T2: ${t2:,.2f}\n\n"
        f"Time: {now}\n"
        f"⚠️ Technical signal only, not financial advice. Manage your own risk."
    )


def format_status_reply(a):
    """Reply sent when user messages the bot asking for status/target,
    even if there's no high-confidence auto-alert signal right now."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    bias = "Bullish" if a["bullish_votes"] > a["bearish_votes"] else "Bearish" if a["bearish_votes"] > a["bullish_votes"] else "Mixed"

    lines = [
        f"📊 *GOLD (XAU/USD) - Current Status*",
        f"",
        f"Price: ${a['price']:,.2f}",
        f"Bias: {bias} ({a['bullish_votes']}/4 bullish, {a['bearish_votes']}/4 bearish)",
        f"RSI(14): {a['rsi']:.1f}",
        f"MACD: {a['macd']:.2f} (Signal: {a['macd_signal']:.2f})",
        f"EMA-50: ${a['ema']:,.2f}",
        f"VWAP: ${a['vwap']:,.2f}",
        f"ADX(14): {a['adx']:.1f} ({'strong trend' if a['adx'] >= ADX_MIN_THRESHOLD else 'weak/choppy trend'})",
        f"ATR(14): ${a['atr']:.2f}",
        f"",
    ]

    if a["signal"] in ("BUY", "SELL"):
        lines.append(f"✅ High-confidence {a['signal']} signal is currently active - check the alert above/below for entry details.")
    else:
        atr_v = a["atr"]
        if bias == "Bullish":
            lines.append(f"⚠️ No high-confidence auto-alert yet (needs ADX≥25 and 4/4 agreement), but bias leans bullish.")
            lines.append(f"If considering a long: Entry ~${a['price']:,.2f} | SL ${a['price'] - 1.5*atr_v:,.2f} | T1 ${a['price'] + 1.5*atr_v:,.2f}")
        elif bias == "Bearish":
            lines.append(f"⚠️ No high-confidence auto-alert yet (needs ADX≥25 and 4/4 agreement), but bias leans bearish.")
            lines.append(f"If considering a short: Entry ~${a['price']:,.2f} | SL ${a['price'] + 1.5*atr_v:,.2f} | T1 ${a['price'] - 1.5*atr_v:,.2f}")
        else:
            lines.append("⚠️ Indicators are mixed right now - no clear direction. Best to wait for a cleaner setup.")

    lines.append("")
    lines.append(f"Time: {now}")
    lines.append("⚠️ Technical view only, not financial advice.")
    return "\n".join(lines)


# ============================================================
# STATE (persisted across runs via GitHub Actions commit)
# ============================================================

def read_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {"last_signal": None, "last_update_id": 0}


def write_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


# ============================================================
# MAIN
# ============================================================

def main():
    if not BOT_TOKEN or not CHAT_ID:
        print("BOT_TOKEN or CHAT_ID not set. Exiting.")
        sys.exit(1)

    state = read_state()
    last_signal = state.get("last_signal")
    last_update_id = state.get("last_update_id", 0)

    # Check for any new user messages (e.g. "/start", "target") since last run
    user_asked, new_update_id = get_new_updates(last_update_id)
    state["last_update_id"] = new_update_id

    candles = fetch_candles(INSTRUMENT, TIMEFRAME, count=150)
    result = analyze(candles)

    if result is None:
        print("Not enough candle data yet.")
        write_state(state)
        return

    print(
        f"Price: {result['price']:.2f} | Signal: {result['signal']} | "
        f"Bull:{result['bullish_votes']} Bear:{result['bearish_votes']} | "
        f"ADX:{result['adx']:.1f} | Session_OK:{result['good_session']} | "
        f"UserAsked:{user_asked}"
    )
    if result.get("skip_reason"):
        print(f"[FILTERED] {result['skip_reason']}")

    # 1. Automatic high-confidence alert (only on signal CHANGE)
    if result["signal"] in ("BUY", "SELL") and result["signal"] != last_signal:
        send_telegram_message(format_alert(result))
        state["last_signal"] = result["signal"]
    elif result["signal"] == "HOLD":
        print("No high-confidence auto-alert this run.")
    else:
        print("Same signal as last time, no auto-alert sent.")

    # 2. On-demand reply if the user messaged the bot since the last run
    if user_asked:
        send_telegram_message(format_status_reply(result))
        print("[SENT] On-demand status reply (user asked).")

    write_state(state)


if __name__ == "__main__":
    main()
