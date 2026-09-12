"""
AI-Style Gold (XAU/USD) Buy/Sell Signal Bot — GitHub Actions Version
======================================================================
This version runs ONCE per execution (no infinite loop) because GitHub
Actions runs it on a SCHEDULE (e.g. every 15 minutes) instead of a
continuously running process. This means it works with ZERO cost and
ZERO payment method — no server, no VPS, no card needed anywhere.

Uses the 5-Indicator Framework:
  1. EMA-50        -> Trend direction
  2. MACD          -> Momentum
  3. RSI (14)      -> Overbought/Oversold
  4. VWAP          -> Intraday fair value / bias
  5. ATR (14)      -> Volatility (used for SL/Target sizing)

A BUY or SELL signal only fires when ALL 4 direction-indicators
(EMA, MACD, RSI, VWAP) agree — same high-confidence rule as before.

State (the last sent signal) is stored in a small text file
(last_signal.txt) that GitHub Actions commits back to the repo after
each run, so you don't get repeated alerts for the same signal.

Data source: Crypto.com Exchange public market-data API (free, no key).
"""

import os
import sys
import requests
from datetime import datetime

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "")
INSTRUMENT = os.environ.get("INSTRUMENT", "XAUUSD-PERP")
TIMEFRAME = os.environ.get("TIMEFRAME", "15m")
STATE_FILE = "last_signal.txt"

EMA_PERIOD = 50
RSI_PERIOD = 14
ATR_PERIOD = 14
ADX_PERIOD = 14
ADX_MIN_THRESHOLD = 25   # only trade when trend strength is confirmed
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9

CANDLESTICK_URL = "https://api.crypto.com/exchange/v1/public/get-candlestick"
TELEGRAM_SEND_URL = "https://api.telegram.org/bot{token}/sendMessage"


def fetch_candles(instrument, timeframe, count=150):
    params = {"instrument_name": instrument, "timeframe": timeframe, "count": count}
    resp = requests.get(CANDLESTICK_URL, params=params, timeout=15)
    resp.raise_for_status()
    candles = resp.json()["result"]["data"]
    candles.sort(key=lambda c: c["t"])
    return candles


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
    """Only trade during London / London-NY overlap sessions
    (12:30 PM - 9:30 PM IST = 07:00 - 16:00 UTC). Avoids the choppy,
    low-liquidity Asian session."""
    utc_hour = datetime.utcnow().hour
    return 7 <= utc_hour < 16


def session_vwap(candles):
    cum_pv, cum_v = 0, 0
    for c in candles:
        typical = (c["h"] + c["l"] + c["c"]) / 3
        cum_pv += typical * c["v"]
        cum_v += c["v"]
    return cum_pv / cum_v if cum_v > 0 else None


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

    # Layer 5: ADX must confirm a strong, tradeable trend
    strong_trend = adx_val >= ADX_MIN_THRESHOLD

    # Layer 6: only trade during London / London-NY overlap hours
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


def send_telegram_message(text):
    url = TELEGRAM_SEND_URL.format(token=BOT_TOKEN)
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"}
    r = requests.post(url, data=payload, timeout=15)
    if r.status_code != 200:
        print(f"[ERROR] Telegram send failed: {r.text}")
    else:
        print("[SENT] Telegram alert delivered.")


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


def read_last_signal():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return f.read().strip()
    return None


def write_last_signal(signal):
    with open(STATE_FILE, "w") as f:
        f.write(signal)


def main():
    if not BOT_TOKEN or not CHAT_ID:
        print("BOT_TOKEN or CHAT_ID not set. Exiting.")
        sys.exit(1)

    candles = fetch_candles(INSTRUMENT, TIMEFRAME, count=150)
    result = analyze(candles)

    if result is None:
        print("Not enough candle data yet.")
        return

    print(
        f"Price: {result['price']:.2f} | Signal: {result['signal']} | "
        f"Bull:{result['bullish_votes']} Bear:{result['bearish_votes']} | "
        f"ADX:{result['adx']:.1f} | Session_OK:{result['good_session']}"
    )
    if result.get("skip_reason"):
        print(f"[FILTERED] {result['skip_reason']}")

    last_signal = read_last_signal()

    if result["signal"] in ("BUY", "SELL") and result["signal"] != last_signal:
        send_telegram_message(format_alert(result))
        write_last_signal(result["signal"])
    elif result["signal"] == "HOLD":
        # Don't overwrite state on HOLD; keep waiting for the next BUY/SELL flip.
        print("No high-confidence signal this run.")
    else:
        print("Same signal as last time, no alert sent.")


if __name__ == "__main__":
    main()
