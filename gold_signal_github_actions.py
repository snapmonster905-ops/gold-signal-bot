
"""
Gold Trading Signals — Professional V6 Telegram Bot
====================================================
GitHub Actions / Telegram edition.

V6 Professional 14-Layer architecture:
  1. Higher-timeframe EMA trend
  2. Current-timeframe EMA-50 trend
  3. MACD momentum
  4. RSI-14 bias / momentum
  5. VWAP fair-value bias
  6. ADX + DI trend strength/direction
  7. Bollinger squeeze / breakout context
  8. EMA angle / trend steepness
  9. Price-action candle confirmation
 10. Previous-day high/low liquidity sweep
 11. Market structure / BOS confirmation
 12. Fair Value Gap (FVG) context
 13. Order-block / supply-demand zone context
 14. Volume/impulse confirmation

Design:
  • Uses a weighted 10-layer score instead of requiring every indicator to agree.
  • BUY/SELL requires a strong score plus mandatory safety filters.
  • HOLD is used when the setup is mixed or unsafe.
  • Higher-timeframe confirmation reduces counter-trend signals.
  • ATR-based trade map adapts SL/T1/T2 to current volatility.
  • Keeps the original Telegram command center and GitHub Actions workflow.
  • Technical signals only — not financial advice.

IMPORTANT:
  No strategy can guarantee a particular win rate. V6 is designed to improve
  signal quality and risk control, not to promise profits.
"""

import html
import json
import math
import os
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests


# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("CHAT_ID", "").strip()
INSTRUMENT = os.environ.get("INSTRUMENT", "XAUUSD-PERP").strip()
TIMEFRAME = os.environ.get("TIMEFRAME", "15m").strip()
HTF_TIMEFRAME = os.environ.get("HTF_TIMEFRAME", "1h").strip()
STATE_FILE = os.environ.get("STATE_FILE", "bot_state.json")

EMA_PERIOD = 50
RSI_PERIOD = 14
ATR_PERIOD = 14
ADX_PERIOD = 14
ADX_MIN_THRESHOLD = 23
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
BB_PERIOD = 20
BB_STD_DEV = 2

ANGLE_LOOKBACK = 5
ANGLE_MIN_THRESHOLD = 12

# Score controls. There are 14 strategy layers.
MIN_DIRECTIONAL_SCORE = 10
MIN_SCORE_EDGE = 3
# Weighted confidence: trend/structure/liquidity layers carry more weight.
MIN_WEIGHTED_SCORE = 0.66
MIN_SETUP_SCORE = 10
# Use the last fully closed candle by default to reduce intrabar/repainting noise.
USE_CLOSED_CANDLE = True
# Avoid alerting repeatedly on the same closed candle.
ALERT_COOLDOWN_CANDLES = 1

# Safety / volatility controls.
ATR_MIN_PCT = 0.03
ATR_MAX_PCT = 1.50
MIN_CANDLE_BODY_ATR = 0.05
MAX_CANDLE_RANGE_ATR = 2.50

# Main London + New York overlap/liquidity window.
# 07:00–20:00 UTC = 12:30–01:30 IST (next day).
SESSION_START_UTC = 7
SESSION_END_UTC = 20

# ATR-based trade map.
SL_ATR_MULT = 1.6
T1_ATR_MULT = 1.5
T2_ATR_MULT = 3.0
MIN_RR_T2 = 1.80
T1_RR_FLOOR = 1.5  # minimum Risk:Reward floor for Target 1
MAX_RISK_ATR = 2.20

CANDLESTICK_URL = "https://api.crypto.com/exchange/v1/public/get-candlestick"
TELEGRAM_API = "https://api.telegram.org/bot{token}"

COMMANDS = {
    "/start": "Welcome + current market snapshot",
    "/help": "Show available commands",
    "/status": "Current market status",
    "/signal": "Current BUY / SELL / HOLD state",
    "/price": "Current gold price",
    "/target": "ATR-based entry, SL and targets",
    "/bands": "Bollinger squeeze / breakout view",
    "/layers": "14-layer score breakdown",
    "/why": "Why BUY/SELL/HOLD and what is blocking the setup",
    "/risk": "Risk, ATR and reward-to-risk map",
    "/health": "Bot/API health check",
}


# ============================================================
# TIME / HELPERS
# ============================================================

def now_ist():
    return datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Kolkata"))


def stamp():
    return now_ist().strftime("%d %b %Y • %I:%M:%S %p IST")


def esc(value):
    return html.escape(str(value))


def fmt_price(value):
    return f"${value:,.2f}"


def telegram_url(method):
    return f"{TELEGRAM_API.format(token=BOT_TOKEN)}/{method}"


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram_message(text):
    if not BOT_TOKEN or not CHAT_ID:
        print("[ERROR] BOT_TOKEN or CHAT_ID is missing.")
        return False

    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    try:
        response = requests.post(
            telegram_url("sendMessage"),
            data=payload,
            timeout=20,
        )
        if response.ok:
            print("[SENT] Telegram message delivered.")
            return True

        print(f"[ERROR] Telegram send failed ({response.status_code}): {response.text}")
        return False
    except requests.RequestException as exc:
        print(f"[ERROR] Telegram network error: {exc}")
        return False


def telegram_health():
    try:
        response = requests.get(telegram_url("getMe"), timeout=15)
        if not response.ok:
            return False, response.text

        data = response.json()
        if not data.get("ok"):
            return False, data

        return True, data.get("result", {})
    except requests.RequestException as exc:
        return False, str(exc)


def get_new_updates(last_update_id):
    try:
        params = {"timeout": 0}
        if last_update_id:
            params["offset"] = last_update_id + 1

        response = requests.get(
            telegram_url("getUpdates"),
            params=params,
            timeout=15,
        )

        if not response.ok:
            print(f"[ERROR] getUpdates failed ({response.status_code}): {response.text}")
            return [], False, last_update_id

        data = response.json()
        if not data.get("ok"):
            print(f"[ERROR] getUpdates API error: {data}")
            return [], False, last_update_id

        updates = data.get("result", [])
        commands = []
        start_requested = False
        new_max_id = last_update_id

        for update in updates:
            new_max_id = max(new_max_id, int(update.get("update_id", last_update_id)))

            message = update.get("message") or {}
            chat = message.get("chat") or {}
            incoming_chat_id = str(chat.get("id", ""))

            if incoming_chat_id != str(CHAT_ID):
                continue

            raw = str(message.get("text", "")).strip()
            if not raw:
                continue

            command = raw.split()[0].split("@")[0].lower()

            if command in COMMANDS:
                commands.append(command)
                if command == "/start":
                    start_requested = True

        return commands, start_requested, new_max_id

    except (requests.RequestException, ValueError, TypeError) as exc:
        print(f"[ERROR] getUpdates exception: {exc}")
        return [], False, last_update_id


def send_welcome_message():
    text = (
        "👋 <b>Welcome to Gold Trading Signals V6</b>\n\n"
        "🤖 <b>Bot status:</b> CONNECTED\n"
        f"🥇 <b>Market:</b> {esc(INSTRUMENT)}\n"
        f"⏱️ <b>Timeframe:</b> {esc(TIMEFRAME)}\n"
        f"🧭 <b>HTF:</b> {esc(HTF_TIMEFRAME)}\n\n"
        "🚀 <b>14-Layer Professional Gold Engine</b>\n"
        "1️⃣ HTF Trend • 2️⃣ EMA • 3️⃣ MACD • 4️⃣ RSI\n"
        "5️⃣ VWAP • 6️⃣ ADX/DI • 7️⃣ Bollinger • 8️⃣ EMA Angle\n"
        "9️⃣ Price Action • 🔟 Liquidity Sweep • 1️⃣1️⃣ Structure\n"
        "1️⃣2️⃣ FVG • 1️⃣3️⃣ Order Block • 1️⃣4️⃣ Volume Impulse\n\n"
        "🧠 <b>Smart decision:</b> weighted confirmation + safety filters.\n"
        "🛡️ HOLD is preferred when confirmation is weak or conditions are unsafe.\n\n"
        "<b>Commands</b>\n"
        "• /status — full market snapshot\n"
        "• /signal — current BUY / SELL / HOLD\n"
        "• /price — current price\n"
        "• /target — entry / SL / T1 / T2\n"
        "• /layers — 14-layer breakdown\n"
        "• /why — explain the decision\n"
        "• /risk — risk & RR map\n"
        "• /bands — Bollinger view\n"
        "• /health — API health\n"
        "• /help — command list\n\n"
        "⏰ Checks run automatically on every scheduled GitHub Actions run.\n"
        "⚠️ <i>Technical signals only — no guaranteed win rate or profit.</i>"
    )
    return send_telegram_message(text)


def fetch_candles(instrument, timeframe, count=200):
    params = {
        "instrument_name": instrument,
        "timeframe": timeframe,
        "count": count,
    }

    response = requests.get(CANDLESTICK_URL, params=params, timeout=20)
    response.raise_for_status()

    payload = response.json()
    if payload.get("code") not in (0, "0", None):
        raise RuntimeError(f"Market API returned error: {payload}")

    result = payload.get("result") or {}
    candles = result.get("data") or []

    if not candles:
        raise RuntimeError(
            f"No candle data returned for {instrument} / {timeframe}. "
            "Check the instrument name and timeframe."
        )

    candles.sort(key=lambda c: c["t"])

    clean = []
    for candle in candles:
        clean.append({
            "t": candle["t"],
            "o": float(candle["o"]),
            "h": float(candle["h"]),
            "l": float(candle["l"]),
            "c": float(candle["c"]),
            "v": float(candle["v"]),
        })

    return clean


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

    out[period] = (
        100 - (100 / (1 + (avg_gain / avg_loss)))
        if avg_loss != 0 else 100
    )

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

        out[i + 1] = (
            100 - (100 / (1 + (avg_gain / avg_loss)))
            if avg_loss != 0 else 100
        )

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

    for i, value in enumerate(signal_clean):
        if value is not None:
            signal_line[offset + i] = value

    return macd_line, signal_line


def true_range(candles):
    trs = []
    for i in range(1, len(candles)):
        high = candles[i]["h"]
        low = candles[i]["l"]
        previous_close = candles[i - 1]["c"]
        trs.append(max(
            high - low,
            abs(high - previous_close),
            abs(low - previous_close),
        ))
    return trs


def atr(candles, period=14):
    trs = true_range(candles)
    if len(trs) < period:
        return None
    return sum(trs[-period:]) / period


def bollinger_bands(values, period=20, num_std=2):
    n = len(values)
    upper = [None] * n
    middle = [None] * n
    lower = [None] * n
    bandwidth = [None] * n

    for i in range(period - 1, n):
        window = values[i - period + 1:i + 1]
        mean = sum(window) / period
        variance = sum((x - mean) ** 2 for x in window) / period
        std = variance ** 0.5

        upper[i] = mean + num_std * std
        lower[i] = mean - num_std * std
        middle[i] = mean
        bandwidth[i] = ((upper[i] - lower[i]) / mean) if mean else None

    return upper, middle, lower, bandwidth


def calculate_ema_angle(ema_values, atr_value, lookback=5):
    clean = [v for v in ema_values if v is not None]
    if len(clean) < lookback + 1 or not atr_value:
        return None

    rise = clean[-1] - clean[-1 - lookback]
    normalized_slope = (rise / lookback) / atr_value
    return math.degrees(math.atan(normalized_slope))


def calculate_adx_details(candles, period=14):
    highs = [c["h"] for c in candles]
    lows = [c["l"] for c in candles]
    closes = [c["c"] for c in candles]

    trs, plus_dm, minus_dm = [], [], []

    for i in range(1, len(candles)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)

        up_move = highs[i] - highs[i - 1]
        down_move = lows[i - 1] - lows[i]

        plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0)
        minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0)

    if len(trs) < period * 2:
        return None, None, None

    def wilder_smooth(values, p):
        smoothed = [None] * len(values)
        smoothed[p - 1] = sum(values[:p])

        for i in range(p, len(values)):
            smoothed[i] = (
                smoothed[i - 1]
                - (smoothed[i - 1] / p)
                + values[i]
            )
        return smoothed

    tr_s = wilder_smooth(trs, period)
    plus_dm_s = wilder_smooth(plus_dm, period)
    minus_dm_s = wilder_smooth(minus_dm, period)

    dx = []
    plus_di_values = []
    minus_di_values = []

    for i in range(period - 1, len(trs)):
        if not tr_s[i]:
            continue

        plus_di = 100 * plus_dm_s[i] / tr_s[i]
        minus_di = 100 * minus_dm_s[i] / tr_s[i]
        di_sum = plus_di + minus_di

        plus_di_values.append(plus_di)
        minus_di_values.append(minus_di)

        dx.append(
            100 * abs(plus_di - minus_di) / di_sum
            if di_sum else 0
        )

    if len(dx) < period:
        return None, None, None

    adx_value = sum(dx[:period]) / period
    for value in dx[period:]:
        adx_value = ((adx_value * (period - 1)) + value) / period

    return adx_value, plus_di_values[-1], minus_di_values[-1]


def session_vwap(candles):
    cumulative_pv = 0
    cumulative_volume = 0

    for candle in candles:
        typical = (candle["h"] + candle["l"] + candle["c"]) / 3
        cumulative_pv += typical * candle["v"]
        cumulative_volume += candle["v"]

    return (
        cumulative_pv / cumulative_volume
        if cumulative_volume > 0 else None
    )



# ============================================================
# ADVANCED GOLD PRICE-ACTION / SMC HELPERS
# ============================================================

def _closed_candles(candles):
    """Return candles used for signal calculation.
    Crypto/CFD APIs can include the still-forming candle. By default we
    remove it so a signal is based on a completed bar.
    """
    if USE_CLOSED_CANDLE and len(candles) >= 3:
        return candles[:-1]
    return candles


def previous_day_levels(candles):
    """Approximate previous-day high/low from UTC calendar days."""
    if len(candles) < 10:
        return None, None
    days = {}
    for c in candles:
        try:
            dt = datetime.fromtimestamp(float(c["t"]) / 1000, tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            continue
        key = dt.date()
        d = days.setdefault(key, {"h": -float("inf"), "l": float("inf")})
        d["h"] = max(d["h"], c["h"])
        d["l"] = min(d["l"], c["l"])
    keys = sorted(days)
    if len(keys) < 2:
        return None, None
    prev = days[keys[-2]]
    return prev["h"], prev["l"]


def liquidity_sweep(candles, pdh, pdl):
    """Detect a rejection of previous-day high/low on the latest closed bar."""
    if not candles or pdh is None or pdl is None:
        return "NEUTRAL"
    c = candles[-1]
    # Sweep high: price traded above PDH but closed back below it.
    if c["h"] > pdh and c["c"] < pdh:
        return "BEARISH"
    # Sweep low: price traded below PDL but closed back above it.
    if c["l"] < pdl and c["c"] > pdl:
        return "BULLISH"
    return "NEUTRAL"


def swing_points(candles, lookback=2):
    """Return simple confirmed swing highs/lows."""
    highs, lows = [], []
    if len(candles) < lookback * 2 + 1:
        return highs, lows
    for i in range(lookback, len(candles) - lookback):
        h = candles[i]["h"]
        l = candles[i]["l"]
        if h == max(x["h"] for x in candles[i-lookback:i+lookback+1]):
            highs.append((i, h))
        if l == min(x["l"] for x in candles[i-lookback:i+lookback+1]):
            lows.append((i, l))
    return highs, lows


def market_structure_signal(candles, lookback=2):
    """BOS/CHoCH-style confirmation using confirmed swing levels."""
    highs, lows = swing_points(candles, lookback)
    if len(highs) < 2 or len(lows) < 2:
        return "NEUTRAL"
    c = candles[-1]["c"]
    last_swing_high = highs[-1][1]
    last_swing_low = lows[-1][1]
    prev_swing_high = highs[-2][1]
    prev_swing_low = lows[-2][1]

    if c > last_swing_high and last_swing_high >= prev_swing_high:
        return "BULLISH"
    if c < last_swing_low and last_swing_low <= prev_swing_low:
        return "BEARISH"

    # Structure trend when no fresh BOS occurred.
    if last_swing_high > prev_swing_high and last_swing_low > prev_swing_low:
        return "BULLISH"
    if last_swing_high < prev_swing_high and last_swing_low < prev_swing_low:
        return "BEARISH"
    return "NEUTRAL"


def fair_value_gap_signal(candles, atr_value):
    """Three-candle imbalance/FVG context.
    Bullish FVG: current low > candle[-3] high.
    Bearish FVG: current high < candle[-3] low.
    Also accepts a near-fill/reaction around a recent FVG.
    """
    if len(candles) < 4 or not atr_value:
        return "NEUTRAL"
    a, b, c = candles[-3], candles[-2], candles[-1]
    gap_min = max(atr_value * 0.05, 0.01)

    if c["l"] - a["h"] >= gap_min:
        return "BULLISH"
    if a["l"] - c["h"] >= gap_min:
        return "BEARISH"

    # Recent gap reaction (last 12 bars), useful after an FVG has formed.
    for i in range(max(2, len(candles) - 12), len(candles) - 2):
        x, y, z = candles[i], candles[i+1], candles[i+2]
        if y["l"] > x["h"] + gap_min:
            gap_low, gap_high = x["h"], y["l"]
            if gap_low <= c["c"] <= gap_high and c["c"] > c["o"]:
                return "BULLISH"
        if y["h"] < x["l"] - gap_min:
            gap_low, gap_high = y["h"], x["l"]
            if gap_low <= c["c"] <= gap_high and c["c"] < c["o"]:
                return "BEARISH"
    return "NEUTRAL"


def order_block_signal(candles, atr_value):
    """Conservative order-block proxy: last opposite candle before an impulse."""
    if len(candles) < 8 or not atr_value:
        return "NEUTRAL"
    recent = candles[-6:]
    impulse_up = recent[-1]["c"] - recent[0]["c"]
    impulse_down = recent[0]["c"] - recent[-1]["c"]

    if impulse_up >= 1.2 * atr_value:
        for c in reversed(recent[:-1]):
            if c["c"] < c["o"]:
                zone_high = c["h"]
                zone_low = c["l"]
                last = recent[-1]
                if last["c"] >= zone_low:
                    return "BULLISH"
                break

    if impulse_down >= 1.2 * atr_value:
        for c in reversed(recent[:-1]):
            if c["c"] > c["o"]:
                zone_high = c["h"]
                zone_low = c["l"]
                last = recent[-1]
                if last["c"] <= zone_high:
                    return "BEARISH"
                break
    return "NEUTRAL"


def volume_impulse_signal(candles):
    """Volume confirmation using a rolling median/average proxy."""
    if len(candles) < 21:
        return "NEUTRAL"
    last = candles[-1]
    volumes = [max(0.0, c.get("v", 0.0)) for c in candles[-21:-1]]
    baseline = sum(volumes) / len(volumes) if volumes else 0
    if baseline <= 0:
        return "NEUTRAL"
    body = abs(last["c"] - last["o"])
    rng = last["h"] - last["l"]
    if last.get("v", 0.0) >= 1.20 * baseline and rng > 0 and body / rng >= 0.50:
        return "BULLISH" if last["c"] > last["o"] else "BEARISH"
    return "NEUTRAL"


def weighted_confidence(bullish_layers, bearish_layers, weights):
    """Return 0..1 confidence for the dominant direction."""
    bull = sum(w for x, w in zip(bullish_layers, weights) if x)
    bear = sum(w for x, w in zip(bearish_layers, weights) if x)
    total = sum(weights) or 1.0
    dominant = max(bull, bear)
    return dominant / total, bull, bear



# ============================================================
# STRATEGY LAYERS
# ============================================================

def in_trading_session():
    utc_hour = datetime.now(timezone.utc).hour
    return SESSION_START_UTC <= utc_hour < SESSION_END_UTC


def candle_price_action(candles, atr_value):
    if len(candles) < 2 or not atr_value:
        return "NEUTRAL"

    candle = candles[-1]
    previous = candles[-2]

    body = abs(candle["c"] - candle["o"])
    candle_range = candle["h"] - candle["l"]

    if candle_range <= 0:
        return "NEUTRAL"

    body_ratio = body / candle_range
    upper_wick = candle["h"] - max(candle["o"], candle["c"])
    lower_wick = min(candle["o"], candle["c"]) - candle["l"]

    # Reject an abnormally large candle: chasing an extended candle is risky.
    if candle_range > MAX_CANDLE_RANGE_ATR * atr_value:
        return "EXTENDED"

    if body < MIN_CANDLE_BODY_ATR * atr_value:
        return "NEUTRAL"

    # Strong directional body.
    if candle["c"] > candle["o"] and body_ratio >= 0.55:
        return "BULLISH"

    if candle["c"] < candle["o"] and body_ratio >= 0.55:
        return "BEARISH"

    # Rejection candle.
    if lower_wick > body * 1.5 and candle["c"] > candle["o"]:
        return "BULLISH"

    if upper_wick > body * 1.5 and candle["c"] < candle["o"]:
        return "BEARISH"

    # Break of previous candle with close confirmation.
    if candle["c"] > previous["h"] and candle["c"] > candle["o"]:
        return "BULLISH"

    if candle["c"] < previous["l"] and candle["c"] < candle["o"]:
        return "BEARISH"

    return "NEUTRAL"


def volatility_safe(price, atr_value):
    if not price or not atr_value:
        return False

    atr_pct = (atr_value / price) * 100
    return ATR_MIN_PCT <= atr_pct <= ATR_MAX_PCT


def analyze(candles, htf_candles):
    candles = _closed_candles(candles)
    htf_candles = _closed_candles(htf_candles)
    closes = [c["c"] for c in candles]
    price = closes[-1]

    ema_values = ema(closes, EMA_PERIOD)
    rsi_values = rsi(closes, RSI_PERIOD)
    macd_line, signal_line = macd(
        closes, MACD_FAST, MACD_SLOW, MACD_SIGNAL
    )
    atr_value = atr(candles, ATR_PERIOD)
    vwap_value = session_vwap(candles[-96:])
    adx_value, plus_di, minus_di = calculate_adx_details(
        candles, ADX_PERIOD
    )
    bb_upper, bb_middle, bb_lower, bb_bandwidth = bollinger_bands(
        closes, BB_PERIOD, BB_STD_DEV
    )

    htf_closes = [c["c"] for c in htf_candles]
    htf_ema_values = ema(htf_closes, EMA_PERIOD)

    values = (
        ema_values[-1], rsi_values[-1], macd_line[-1], signal_line[-1],
        atr_value, vwap_value, adx_value, plus_di, minus_di,
        bb_upper[-1], bb_middle[-1], bb_lower[-1], bb_bandwidth[-1],
        htf_ema_values[-1],
    )
    if any(v is None for v in values):
        return None

    ema_value = ema_values[-1]
    rsi_value = rsi_values[-1]
    macd_value = macd_line[-1]
    macd_signal_value = signal_line[-1]
    bb_upper_value = bb_upper[-1]
    bb_middle_value = bb_middle[-1]
    bb_lower_value = bb_lower[-1]
    bb_bandwidth_value = bb_bandwidth[-1]
    htf_ema_value = htf_ema_values[-1]
    htf_price = htf_closes[-1]

    ema_angle_value = calculate_ema_angle(
        ema_values, atr_value, ANGLE_LOOKBACK
    )
    if ema_angle_value is None:
        return None

    price_action = candle_price_action(candles, atr_value)
    pdh, pdl = previous_day_levels(candles)
    sweep = liquidity_sweep(candles, pdh, pdl)
    structure = market_structure_signal(candles)
    fvg = fair_value_gap_signal(candles, atr_value)
    ob = order_block_signal(candles, atr_value)
    volume_signal = volume_impulse_signal(candles)

    # 1 HTF trend
    htf_bull = htf_price > htf_ema_value
    htf_bear = htf_price < htf_ema_value

    # 2 Current EMA trend
    ema_bull = price > ema_value
    ema_bear = price < ema_value

    # 3 MACD momentum
    macd_bull = macd_value > macd_signal_value
    macd_bear = macd_value < macd_signal_value

    # 4 RSI bias; avoid chasing extreme conditions.
    rsi_bull = 50 < rsi_value < 72
    rsi_bear = 28 < rsi_value < 50

    # 5 VWAP
    vwap_bull = price > vwap_value
    vwap_bear = price < vwap_value

    # 6 ADX + DI
    strong_trend = adx_value >= ADX_MIN_THRESHOLD
    adx_bull = strong_trend and plus_di > minus_di
    adx_bear = strong_trend and minus_di > plus_di

    # 7 Bollinger context
    recent_bandwidths = [v for v in bb_bandwidth[-50:] if v is not None]
    bb_squeeze = False
    if len(recent_bandwidths) >= 10:
        sorted_bw = sorted(recent_bandwidths)
        threshold_index = max(0, int(len(sorted_bw) * 0.20) - 1)
        bb_squeeze = bb_bandwidth_value <= sorted_bw[threshold_index]

    bb_bull = price > bb_middle_value
    bb_bear = price < bb_middle_value
    if price > bb_upper_value:
        bb_bull = True
    elif price < bb_lower_value:
        bb_bear = True

    # 8 EMA angle
    angle_bull = ema_angle_value >= ANGLE_MIN_THRESHOLD
    angle_bear = ema_angle_value <= -ANGLE_MIN_THRESHOLD

    # 9 Price action
    pa_bull = price_action == "BULLISH"
    pa_bear = price_action == "BEARISH"

    # 10 Liquidity sweep
    sweep_bull = sweep == "BULLISH"
    sweep_bear = sweep == "BEARISH"

    # 11 Structure
    structure_bull = structure == "BULLISH"
    structure_bear = structure == "BEARISH"

    # 12 FVG
    fvg_bull = fvg == "BULLISH"
    fvg_bear = fvg == "BEARISH"

    # 13 Order block / zone
    ob_bull = ob == "BULLISH"
    ob_bear = ob == "BEARISH"

    # 14 Volume/impulse
    vol_bull = volume_signal == "BULLISH"
    vol_bear = volume_signal == "BEARISH"

    # Safety is separate from directional votes.
    good_session = in_trading_session()
    volatility_ok = volatility_safe(price, atr_value)
    safety_ok = (
        good_session
        and volatility_ok
        and price_action != "EXTENDED"
        and atr_value / price * 100 <= ATR_MAX_PCT
    )

    bullish_layers = [
        htf_bull, ema_bull, macd_bull, rsi_bull, vwap_bull,
        adx_bull, bb_bull, angle_bull, pa_bull, sweep_bull,
        structure_bull, fvg_bull, ob_bull, vol_bull
    ]
    bearish_layers = [
        htf_bear, ema_bear, macd_bear, rsi_bear, vwap_bear,
        adx_bear, bb_bear, angle_bear, pa_bear, sweep_bear,
        structure_bear, fvg_bear, ob_bear, vol_bear
    ]

    # More weight on market structure/liquidity/HTF trend than generic oscillators.
    weights = [1.25, 1.0, 0.8, 0.65, 0.75, 1.0, 0.55, 0.65, 0.8,
               1.35, 1.35, 1.0, 0.95, 0.75]
    confidence, weighted_bull, weighted_bear = weighted_confidence(
        bullish_layers, bearish_layers, weights
    )
    total_weight = sum(weights)
    bullish_score = sum(bool(x) for x in bullish_layers)
    bearish_score = sum(bool(x) for x in bearish_layers)
    bullish_pct = weighted_bull / total_weight
    bearish_pct = weighted_bear / total_weight

    signal = "HOLD"
    filter_reason = None

    dominant = max(weighted_bull, weighted_bear)
    edge = abs(weighted_bull - weighted_bear) / total_weight

    if safety_ok and bullish_score >= MIN_SETUP_SCORE and weighted_bull >= MIN_WEIGHTED_SCORE * total_weight:
        if weighted_bull > weighted_bear and edge >= 0.18:
            signal = "BUY"
    elif safety_ok and bearish_score >= MIN_SETUP_SCORE and weighted_bear >= MIN_WEIGHTED_SCORE * total_weight:
        if weighted_bear > weighted_bull and edge >= 0.18:
            signal = "SELL"

    if signal == "HOLD":
        reasons = []
        if not good_session:
            reasons.append("outside active London/NY session")
        if not volatility_ok:
            reasons.append("ATR volatility outside safe range")
        if price_action == "EXTENDED":
            reasons.append("latest closed candle is extended")
        if max(bullish_score, bearish_score) < MIN_SETUP_SCORE:
            reasons.append("setup score below confirmation threshold")
        if edge < 0.18:
            reasons.append("bull/bear edge is too small")
        if not reasons:
            reasons.append("weighted confirmation threshold not reached")
        filter_reason = "; ".join(reasons)

    bias = "BULLISH" if weighted_bull > weighted_bear else (
        "BEARISH" if weighted_bear > weighted_bull else "MIXED"
    )
    confidence_pct = max(bullish_pct, bearish_pct) * 100

    return {
        "signal": signal,
        "bias": bias,
        "confidence_pct": confidence_pct,
        "price": price,
        "ema": ema_value,
        "htf_price": htf_price,
        "htf_ema": htf_ema_value,
        "rsi": rsi_value,
        "macd": macd_value,
        "macd_signal": macd_signal_value,
        "vwap": vwap_value,
        "atr": atr_value,
        "adx": adx_value,
        "plus_di": plus_di,
        "minus_di": minus_di,
        "good_session": good_session,
        "volatility_ok": volatility_ok,
        "safety_ok": safety_ok,
        "bullish_score": bullish_score,
        "bearish_score": bearish_score,
        "weighted_bull_pct": bullish_pct * 100,
        "weighted_bear_pct": bearish_pct * 100,
        "bb_upper": bb_upper_value,
        "bb_middle": bb_middle_value,
        "bb_lower": bb_lower_value,
        "bb_width_pct": bb_bandwidth_value * 100,
        "bb_squeeze": bb_squeeze,
        "bb_position": (
            "Above upper band" if price > bb_upper_value else
            "Below lower band" if price < bb_lower_value else
            "Inside bands"
        ),
        "ema_angle": ema_angle_value,
        "strong_angle": abs(ema_angle_value) >= ANGLE_MIN_THRESHOLD,
        "price_action": price_action,
        "pdh": pdh,
        "pdl": pdl,
        "liquidity_sweep": sweep,
        "structure": structure,
        "fvg": fvg,
        "order_block": ob,
        "volume_signal": volume_signal,
        "weights_total": total_weight,
        "filter_reason": filter_reason,
        "layers": {
            "HTF Trend": "BULL" if htf_bull else "BEAR",
            "EMA-50": "BULL" if ema_bull else "BEAR",
            "MACD": "BULL" if macd_bull else "BEAR",
            "RSI": "BULL" if rsi_bull else "BEAR",
            "VWAP": "BULL" if vwap_bull else "BEAR",
            "ADX + DI": "BULL" if adx_bull else "BEAR",
            "Bollinger": "BULL" if bb_bull else "BEAR",
            "EMA Angle": "BULL" if angle_bull else "BEAR",
            "Price Action": price_action,
            "PDH/PDL Sweep": sweep,
            "Market Structure": structure,
            "FVG": fvg,
            "Order Block": ob,
            "Volume Impulse": volume_signal,
        },
    }


# ============================================================
# TRADE MAP
# ============================================================

def trade_levels(a):
    price = a["price"]
    atr_value = a["atr"]
    signal = a["signal"]
    direction = signal if signal in ("BUY", "SELL") else a.get("bias", "BULLISH")

    # Use structure/liquidity reference when it gives a sensible nearby stop;
    # otherwise fall back to ATR. Never let the stop become excessively wide.
    if direction == "SELL":
        structural_stop = a.get("pdh")
        atr_stop = price + SL_ATR_MULT * atr_value
        if structural_stop and structural_stop > price:
            sl = max(atr_stop, structural_stop)
        else:
            sl = atr_stop
        max_sl = price + MAX_RISK_ATR * atr_value
        sl = min(sl, max_sl)
        risk = max(sl - price, 0.01)
        t1 = price - max(T1_ATR_MULT * atr_value, risk * T1_RR_FLOOR)
        t2 = price - max(T2_ATR_MULT * atr_value, risk * MIN_RR_T2)
    else:
        structural_stop = a.get("pdl")
        atr_stop = price - SL_ATR_MULT * atr_value
        if structural_stop and structural_stop < price:
            sl = min(atr_stop, structural_stop)
        else:
            sl = atr_stop
        min_sl = price - MAX_RISK_ATR * atr_value
        sl = max(sl, min_sl)
        risk = max(price - sl, 0.01)
        t1 = price + max(T1_ATR_MULT * atr_value, risk * T1_RR_FLOOR)
        t2 = price + max(T2_ATR_MULT * atr_value, risk * MIN_RR_T2)

    rr1 = abs(t1 - price) / max(abs(price - sl), 0.01)
    rr2 = abs(t2 - price) / max(abs(price - sl), 0.01)
    return {
        "entry": price,
        "sl": sl,
        "t1": t1,
        "t2": t2,
        "risk": abs(price - sl),
        "rr1": rr1,
        "rr2": rr2,
        "direction": direction,
    }


# ============================================================
# TELEGRAM FORMATTING
# ============================================================

def format_alert(a):
    levels = trade_levels(a)
    signal = a["signal"]
    emoji = "🟢" if signal == "BUY" else "🔴"
    score = a["bullish_score"] if signal == "BUY" else a["bearish_score"]
    conf = a.get("confidence_pct", 0)

    return (
        f"{emoji} <b>GOLD V6 • {signal} CONFIRMED</b>\n"
        f"<b>{esc(INSTRUMENT)}</b> • {esc(TIMEFRAME)}\n\n"
        f"🔥 <b>Confirmation:</b> {score}/14 layers\n"
        f"🧠 <b>Weighted confidence:</b> {conf:.0f}%\n"
        f"🧭 <b>HTF Bias:</b> {esc(a['bias'])}\n"
        f"💧 <b>Liquidity:</b> {esc(a['liquidity_sweep'])}\n"
        f"🏗️ <b>Structure:</b> {esc(a['structure'])}\n"
        f"📦 <b>FVG:</b> {esc(a['fvg'])} • <b>OB:</b> {esc(a['order_block'])}\n"
        f"🕯️ <b>Price Action:</b> {esc(a['price_action'])}\n"
        f"💪 <b>ADX:</b> {a['adx']:.1f} • DI+ {a['plus_di']:.1f} / DI- {a['minus_di']:.1f}\n"
        f"🛡️ <b>Safety:</b> PASS ✓\n\n"
        f"💰 <b>Entry:</b> {fmt_price(levels['entry'])}\n"
        f"🛑 <b>SL:</b> {fmt_price(levels['sl'])}  "
        f"({levels['risk']:.2f} risk)\n"
        f"🎯 <b>T1:</b> {fmt_price(levels['t1'])}  • RR {levels['rr1']:.2f}\n"
        f"🏁 <b>T2:</b> {fmt_price(levels['t2'])}  • RR {levels['rr2']:.2f}\n\n"
        f"📈 RSI {a['rsi']:.1f} • MACD {a['macd']:.2f} • ATR {fmt_price(a['atr'])}\n"
        f"🕒 {stamp()}\n"
        "⚠️ <i>Technical signal only. No win-rate or profit is guaranteed.</i>"
    )


def format_status_reply(a):
    if a["weighted_bull_pct"] > a["weighted_bear_pct"]:
        bias = "BULLISH 🟢"
    elif a["weighted_bear_pct"] > a["weighted_bull_pct"]:
        bias = "BEARISH 🔴"
    else:
        bias = "MIXED ⚪"

    signal = a["signal"]
    if signal == "BUY":
        signal_line = "🟢 <b>BUY — multi-layer confirmation active</b>"
    elif signal == "SELL":
        signal_line = "🔴 <b>SELL — multi-layer confirmation active</b>"
    else:
        signal_line = "⚪ <b>HOLD — waiting for stronger confirmation</b>"

    filter_line = ""
    if a.get("filter_reason"):
        filter_line = f"\n🛡️ <b>Why HOLD:</b> {esc(a['filter_reason'])}"

    return (
        "📊 <b>GOLD MARKET STATUS — V6</b>\n"
        f"{esc(INSTRUMENT)} • {esc(TIMEFRAME)} / HTF {esc(HTF_TIMEFRAME)}\n\n"
        f"💰 <b>Price:</b> {fmt_price(a['price'])}\n"
        f"🧭 <b>Bias:</b> {bias}\n"
        f"🎯 <b>Signal:</b> {signal}\n"
        f"🧠 <b>Confidence:</b> {a['confidence_pct']:.0f}%\n\n"
        f"🔥 Bullish: {a['bullish_score']}/14 ({a['weighted_bull_pct']:.0f}% weighted)\n"
        f"🔻 Bearish: {a['bearish_score']}/14 ({a['weighted_bear_pct']:.0f}% weighted)\n\n"
        f"💧 Liquidity Sweep: {esc(a['liquidity_sweep'])}\n"
        f"🏗️ Structure: {esc(a['structure'])}\n"
        f"📦 FVG: {esc(a['fvg'])} • OB: {esc(a['order_block'])}\n"
        f"📊 Volume: {esc(a['volume_signal'])}\n"
        f"💪 ADX: {a['adx']:.1f} • DI+ {a['plus_di']:.1f} / DI- {a['minus_di']:.1f}\n"
        f"📈 RSI: {a['rsi']:.1f} • ATR: {fmt_price(a['atr'])}\n"
        f"🕯️ Price Action: {esc(a['price_action'])}\n\n"
        f"🛡️ Safety: {'PASS' if a['safety_ok'] else 'BLOCK'}\n"
        f"🕐 Session: {'ACTIVE' if a['good_session'] else 'INACTIVE'}\n"
        f"🌊 Volatility: {'OK' if a['volatility_ok'] else 'OUTSIDE RANGE'}"
        f"{filter_line}\n\n"
        f"{signal_line}\n\n"
        f"🕒 {stamp()}\n"
        "⚠️ <i>Technical view only — not financial advice.</i>"
    )


def format_signal_reply(a):
    if a["signal"] == "BUY":
        headline = "🟢 <b>BUY CONFIRMED</b>"
        score = a["bullish_score"]
        reason = "Bullish weighted confirmation passed the V6 threshold."
    elif a["signal"] == "SELL":
        headline = "🔴 <b>SELL CONFIRMED</b>"
        score = a["bearish_score"]
        reason = "Bearish weighted confirmation passed the V6 threshold."
    else:
        headline = "⚪ <b>HOLD / WAIT</b>"
        score = max(a["bullish_score"], a["bearish_score"])
        reason = a.get("filter_reason") or "No high-confidence setup is active."

    return (
        "🎯 <b>GOLD V6 SIGNAL ENGINE</b>\n\n"
        f"{headline}\n"
        f"💰 Price: {fmt_price(a['price'])}\n"
        f"🧭 Bias: <b>{esc(a['bias'])}</b>\n"
        f"🔥 Score: {score}/14\n"
        f"🧠 Confidence: {a['confidence_pct']:.0f}%\n"
        f"💧 Liquidity: {esc(a['liquidity_sweep'])}\n"
        f"🏗️ Structure: {esc(a['structure'])}\n"
        f"📦 FVG / OB: {esc(a['fvg'])} / {esc(a['order_block'])}\n"
        f"🛡️ Safety: {'PASS' if a['safety_ok'] else 'BLOCK'}\n\n"
        f"{esc(reason)}\n\n"
        f"🕒 {stamp()}"
    )


def format_price_reply(a):
    return (
        "💰 <b>GOLD PRICE — V6</b>\n\n"
        f"🥇 {esc(INSTRUMENT)}\n"
        f"💵 <b>{fmt_price(a['price'])}</b>\n"
        f"⏱️ Timeframe: {esc(TIMEFRAME)}\n"
        f"🧭 HTF: {esc(HTF_TIMEFRAME)}\n"
        f"🕒 {stamp()}\n\n"
        "ℹ️ Price is from the configured market-data source."
    )


def format_bands_reply(a):
    squeeze_line = (
        "⚡ <b>SQUEEZE DETECTED</b> — volatility is unusually tight. "
        "Direction still requires confirmation."
        if a["bb_squeeze"]
        else "No squeeze right now — bands are at a normal width."
    )

    position = "Above upper band" if a["price"] > a["bb_upper"] else (
        "Below lower band" if a["price"] < a["bb_lower"]
        else "Inside bands"
    )

    return (
        "📐 <b>BOLLINGER BANDS V6 (20, 2σ)</b>\n\n"
        f"💰 Price: {fmt_price(a['price'])}\n"
        f"⬆️ Upper Band: {fmt_price(a['bb_upper'])}\n"
        f"➖ Middle: {fmt_price(a['bb_middle'])}\n"
        f"⬇️ Lower Band: {fmt_price(a['bb_lower'])}\n"
        f"📏 Band Width: {a['bb_width_pct']:.2f}%\n\n"
        f"📍 <b>Position:</b> {position}\n\n"
        f"{squeeze_line}\n\n"
        f"🕒 {stamp()}\n"
        "⚠️ <i>Technical view only — not financial advice.</i>"
    )


def format_target_reply(a):
    levels = trade_levels(a)

    if a["signal"] == "SELL":
        direction = "🔴 SELL"
    elif a["signal"] == "BUY":
        direction = "🟢 BUY"
    else:
        direction = "⚪ Current bias-based map"

    return (
        "🎯 <b>GOLD V6 TRADE MAP</b>\n\n"
        f"Direction: <b>{direction}</b>\n"
        f"Entry: <b>{fmt_price(levels['entry'])}</b>\n"
        f"Stop Loss: <b>{fmt_price(levels['sl'])}</b>\n"
        f"Target 1: <b>{fmt_price(levels['t1'])}</b>\n"
        f"Target 2: <b>{fmt_price(levels['t2'])}</b>\n"
        f"RR: T1 {levels['rr1']:.2f} • T2 {levels['rr2']:.2f}\n\n"
        f"ATR-14: {fmt_price(a['atr'])}\n"
        f"Method: {SL_ATR_MULT}× ATR SL / "
        f"{T1_ATR_MULT}× ATR T1 / {T2_ATR_MULT}× ATR T2\n\n"
        f"🕒 {stamp()}\n"
        "⚠️ <i>These are ATR-based technical levels, not guaranteed targets.</i>"
    )


def format_layers_reply(a):
    lines = ["🧠 <b>V6 — 14-LAYER GOLD ENGINE</b>\n"]
    for index, (name, state) in enumerate(a["layers"].items(), start=1):
        if state == "BULL":
            icon = "🟢"
        elif state == "BEAR":
            icon = "🔴"
        elif state == "PASS":
            icon = "✅"
        elif state == "BLOCK":
            icon = "⛔"
        else:
            icon = "⚪"
        lines.append(f"{index:02d}. {icon} <b>{esc(name)}</b> — {esc(state)}")

    lines.extend([
        "",
        f"🟢 Bullish: {a['bullish_score']}/14 • {a['weighted_bull_pct']:.0f}% weighted",
        f"🔴 Bearish: {a['bearish_score']}/14 • {a['weighted_bear_pct']:.0f}% weighted",
        f"🧠 Confidence: {a['confidence_pct']:.0f}%",
        f"🎯 Signal: <b>{a['signal']}</b>",
        f"🛡️ Safety: {'PASS' if a['safety_ok'] else 'BLOCK'}",
        "",
        f"🕒 {stamp()}",
    ])
    return "\n".join(lines)


def format_help():
    lines = ["📚 <b>GOLD SIGNALS V6 — COMMAND CENTER</b>\n"]
    for command, description in COMMANDS.items():
        lines.append(f"{command} — {description}")

    lines.extend([
        "",
        "🧠 <b>V6 engine:</b> 14-layer weighted confirmation.",
        "💧 Liquidity sweep + market structure + FVG + OB are added to the original technical stack.",
        "🛡️ Safety filters can block a setup even when indicators look bullish/bearish.",
        "⏱️ Signals are calculated from the latest completed candle by default.",
        "",
        "⚠️ <i>Technical signals only — not financial advice.</i>",
    ])
    return "\n".join(lines)


def format_why_reply(a):
    levels = trade_levels(a)
    if a["signal"] == "BUY":
        action = "🟢 BUY"
    elif a["signal"] == "SELL":
        action = "🔴 SELL"
    else:
        action = "⚪ HOLD"

    return (
        "🔎 <b>WHY THIS SIGNAL? — GOLD V6</b>\n\n"
        f"Action: <b>{action}</b>\n"
        f"Bias: {esc(a['bias'])}\n"
        f"Confidence: {a['confidence_pct']:.0f}%\n\n"
        f"🟢 Bull weighted: {a['weighted_bull_pct']:.0f}%\n"
        f"🔴 Bear weighted: {a['weighted_bear_pct']:.0f}%\n\n"
        f"💧 Liquidity sweep: {esc(a['liquidity_sweep'])}\n"
        f"🏗️ Market structure: {esc(a['structure'])}\n"
        f"📦 FVG: {esc(a['fvg'])}\n"
        f"🧱 Order block: {esc(a['order_block'])}\n"
        f"📊 Volume impulse: {esc(a['volume_signal'])}\n"
        f"🕯️ Price action: {esc(a['price_action'])}\n"
        f"💪 ADX: {a['adx']:.1f}\n\n"
        f"🛡️ Safety: {'PASS' if a['safety_ok'] else 'BLOCK'}\n"
        f"{('⛔ ' + esc(a['filter_reason'])) if a.get('filter_reason') else ''}\n\n"
        f"🎯 Map: {fmt_price(levels['entry'])} → SL {fmt_price(levels['sl'])} → "
        f"T1 {fmt_price(levels['t1'])} → T2 {fmt_price(levels['t2'])}\n"
        f"RR: T1 {levels['rr1']:.2f} / T2 {levels['rr2']:.2f}\n\n"
        f"🕒 {stamp()}"
    )


def format_risk_reply(a):
    levels = trade_levels(a)
    return (
        "🛡️ <b>GOLD V6 RISK MAP</b>\n\n"
        f"Direction: <b>{esc(levels['direction'])}</b>\n"
        f"Entry: {fmt_price(levels['entry'])}\n"
        f"SL: {fmt_price(levels['sl'])}\n"
        f"T1: {fmt_price(levels['t1'])} • RR {levels['rr1']:.2f}\n"
        f"T2: {fmt_price(levels['t2'])} • RR {levels['rr2']:.2f}\n\n"
        f"ATR-14: {fmt_price(a['atr'])}\n"
        f"Risk distance: {levels['risk']:.2f}\n"
        f"Safety: {'PASS' if a['safety_ok'] else 'BLOCK'}\n\n"
        "⚠️ <i>Position size is not calculated because account balance, "
        "leverage and broker contract specifications are not configured.</i>\n"
        f"🕒 {stamp()}"
    )


def format_health(ok, bot_info=None, market_ok=None, market_error=None):
    telegram_status = "🟢 ONLINE" if ok else "🔴 ERROR"

    if market_ok is None:
        market_status = "⚪ NOT TESTED"
    elif market_ok:
        market_status = "🟢 ONLINE"
    else:
        market_status = "🔴 ERROR"

    bot_name = ""
    if isinstance(bot_info, dict):
        username = bot_info.get("username")
        if username:
            bot_name = f"\n🤖 @{esc(username)}"

    error_line = ""
    if market_error:
        error_line = f"\n\n<code>{esc(market_error)}</code>"

    return (
        "🩺 <b>V6 BOT HEALTH CHECK</b>\n\n"
        f"Telegram API: {telegram_status}{bot_name}\n"
        f"Market API: {market_status}\n"
        f"Instrument: {esc(INSTRUMENT)}\n"
        f"Timeframe: {esc(TIMEFRAME)}\n"
        f"HTF: {esc(HTF_TIMEFRAME)}\n"
        "Strategy: 14-Layer V6\n\n"
        f"Schedule: every configured GitHub Actions run\n\n"
        f"🕒 {stamp()}"
        f"{error_line}"
    )


# ============================================================
# STATE
# ============================================================

def read_state():
    default = {
        "last_signal": None,
        "last_update_id": 0,
        "last_success_ist": None,
    }

    if not os.path.exists(STATE_FILE):
        return default

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            state = json.load(file)

        if not isinstance(state, dict):
            return default

        default.update(state)
        return default

    except (OSError, json.JSONDecodeError):
        print("[WARN] State file unreadable; starting with defaults.")
        return default


def write_state(state):
    temp_file = f"{STATE_FILE}.tmp"

    with open(temp_file, "w", encoding="utf-8") as file:
        json.dump(state, file, indent=2)

    os.replace(temp_file, STATE_FILE)


# ============================================================
# COMMAND ROUTER
# ============================================================

def process_commands(commands, analysis):
    for command in commands:
        try:
            if command == "/start":
                send_welcome_message()

            elif command == "/help":
                send_telegram_message(format_help())

            elif command == "/status":
                send_telegram_message(format_status_reply(analysis))

            elif command == "/signal":
                send_telegram_message(format_signal_reply(analysis))

            elif command == "/price":
                send_telegram_message(format_price_reply(analysis))

            elif command == "/target":
                send_telegram_message(format_target_reply(analysis))

            elif command == "/bands":
                send_telegram_message(format_bands_reply(analysis))

            elif command == "/layers":
                send_telegram_message(format_layers_reply(analysis))

            elif command == "/why":
                send_telegram_message(format_why_reply(analysis))

            elif command == "/risk":
                send_telegram_message(format_risk_reply(analysis))

            elif command == "/health":
                telegram_ok, bot_info = telegram_health()

                market_ok = True
                market_error = None
                try:
                    fetch_candles(INSTRUMENT, TIMEFRAME, count=2)
                    fetch_candles(INSTRUMENT, HTF_TIMEFRAME, count=2)
                except Exception as exc:
                    market_ok = False
                    market_error = str(exc)

                send_telegram_message(
                    format_health(
                        telegram_ok,
                        bot_info,
                        market_ok,
                        market_error,
                    )
                )

        except Exception as exc:
            print(f"[ERROR] Command {command} failed: {exc}")
            send_telegram_message(
                "⚠️ <b>Command error</b>\n\n"
                f"<code>{esc(exc)}</code>\n\n"
                f"🕒 {stamp()}"
            )


# ============================================================
# MAIN
# ============================================================

def main():
    if not BOT_TOKEN or not CHAT_ID:
        print("BOT_TOKEN or CHAT_ID not set. Exiting.")
        sys.exit(1)

    state = read_state()
    last_signal = state.get("last_signal")
    last_alert_candle = state.get("last_alert_candle")
    last_update_id = int(state.get("last_update_id", 0) or 0)

    print("=" * 60)
    print("GOLD SIGNAL BOT V6 — RUN START")
    print(f"Market: {INSTRUMENT}")
    print(f"Timeframe: {TIMEFRAME}")
    print(f"HTF: {HTF_TIMEFRAME}")
    print(f"Time: {stamp()}")
    print("=" * 60)

    # 1. Read Telegram messages.
    commands, start_requested, new_update_id = get_new_updates(last_update_id)
    state["last_update_id"] = new_update_id

    if commands:
        print(f"[TELEGRAM] Commands received: {commands}")

    # 2. Fetch current + higher-timeframe market data.
    try:
        candles = fetch_candles(INSTRUMENT, TIMEFRAME, count=200)
        htf_candles = fetch_candles(INSTRUMENT, HTF_TIMEFRAME, count=200)
        result = analyze(candles, htf_candles)
    except Exception as exc:
        print(f"[ERROR] Market data/analysis failed: {exc}")

        if commands:
            send_telegram_message(
                "⚠️ <b>GOLD V6 — DATA ERROR</b>\n\n"
                "Unable to calculate the requested market information.\n\n"
                f"<code>{esc(exc)}</code>\n\n"
                f"🕒 {stamp()}"
            )

        write_state(state)
        return

    if result is None:
        print("[WARN] Not enough candle data to calculate indicators.")

        if commands:
            send_telegram_message(
                "⚠️ <b>Not enough market data</b>\n\n"
                "The bot received candles, but there is not enough data "
                "to calculate all V6 layers yet."
            )

        write_state(state)
        return

    print(
        f"Price={result['price']:.2f} | "
        f"Signal={result['signal']} | "
        f"Bull={result['bullish_score']}/14 | "
        f"Bear={result['bearish_score']}/14 | "
        f"Conf={result['confidence_pct']:.0f}% | "
        f"Structure={result['structure']} | "
        f"Sweep={result['liquidity_sweep']} | "
        f"Safety={result['safety_ok']}"
    )

    if result.get("filter_reason"):
        print(f"[FILTERED] {result['filter_reason']}")

    # 3. Automatic high-confidence alert.
    # Use the latest closed candle timestamp so repeated scheduled runs do not
    # spam the same signal.
    analysis_candle = candles[-2] if USE_CLOSED_CANDLE and len(candles) >= 3 else candles[-1]
    candle_id = str(analysis_candle.get("t", ""))
    should_alert = (
        result["signal"] in ("BUY", "SELL")
        and result["signal"] != last_signal
        and candle_id != str(last_alert_candle or "")
    )
    if should_alert:
        if send_telegram_message(format_alert(result)):
            state["last_signal"] = result["signal"]
            state["last_alert_candle"] = candle_id
            print(f"[AUTO] New {result['signal']} alert sent.")

    elif result["signal"] == "HOLD":
        state["last_signal"] = None
        print("[AUTO] HOLD — no alert.")

    else:
        print("[AUTO] Same signal/candle as previous run — no duplicate alert.")

    # 4. Automatic target update only for a confirmed directional signal.
    if result["signal"] in ("BUY", "SELL"):
        if send_telegram_message(format_target_reply(result)):
            print("[AUTO] Automatic target update sent.")

    # 5. Process explicit Telegram commands.
    if commands:
        process_commands(commands, result)

    state["last_success_ist"] = stamp()
    write_state(state)

    print("[DONE] V6 state saved successfully.")


if __name__ == "__main__":
    main()
