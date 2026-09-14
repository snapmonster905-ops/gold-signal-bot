"""
Gold Trading Signals — Professional V5 Telegram Bot
====================================================
GitHub Actions / Telegram edition.

V5 Professional 10-Layer architecture:
  1. Higher-timeframe EMA trend (1H)
  2. EMA-50 current-timeframe trend
  3. MACD momentum
  4. RSI-14 bias / momentum
  5. VWAP fair-value bias
  6. ADX + DI trend strength/direction
  7. Bollinger Bands squeeze / breakout confirmation
  8. EMA Angle trend steepness
  9. Price-action candle confirmation
 10. Session + volatility safety filter

Design:
  • Uses a weighted 10-layer score instead of requiring every indicator to agree.
  • BUY/SELL requires a strong score plus mandatory safety filters.
  • HOLD is used when the setup is mixed or unsafe.
  • Higher-timeframe confirmation reduces counter-trend signals.
  • ATR-based trade map adapts SL/T1/T2 to current volatility.
  • Keeps the original Telegram command center and GitHub Actions workflow.
  • Technical signals only — not financial advice.

IMPORTANT:
  No strategy can guarantee a particular win rate. V5 is designed to improve
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

# Score controls. There are 10 strategy layers.
MIN_DIRECTIONAL_SCORE = 8
MIN_SCORE_EDGE = 3

# Safety / volatility controls.
ATR_MIN_PCT = 0.03
ATR_MAX_PCT = 1.50
MIN_CANDLE_BODY_ATR = 0.05
MAX_CANDLE_RANGE_ATR = 2.50

# Session: 07:00–16:00 UTC = 12:30–21:30 IST.
SESSION_START_UTC = 7
SESSION_END_UTC = 16

# ATR-based trade map.
SL_ATR_MULT = 1.5
T1_ATR_MULT = 1.5
T2_ATR_MULT = 3.0

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
    "/layers": "10-layer score breakdown",
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
        "👋 <b>Welcome to Gold Trading Signals V5</b>\n\n"
        "🤖 <b>Bot status:</b> CONNECTED\n"
        f"🥇 <b>Market:</b> {esc(INSTRUMENT)}\n"
        f"⏱️ <b>Timeframe:</b> {esc(TIMEFRAME)}\n"
        f"🧭 <b>HTF confirmation:</b> {esc(HTF_TIMEFRAME)}\n\n"
        "📊 <b>10-Layer Professional Strategy</b>\n"
        "1️⃣ HTF EMA Trend\n"
        "2️⃣ EMA-50 Trend\n"
        "3️⃣ MACD Momentum\n"
        "4️⃣ RSI-14 Bias\n"
        "5️⃣ VWAP Fair Value\n"
        "6️⃣ ADX + DI Trend Strength\n"
        "7️⃣ Bollinger Squeeze / Breakout\n"
        "8️⃣ EMA Angle\n"
        "9️⃣ Price Action\n"
        "🔟 Session + Volatility Safety\n\n"
        "<b>Commands</b>\n"
        "• /status — market snapshot\n"
        "• /signal — BUY / SELL / HOLD\n"
        "• /price — live candle price\n"
        "• /target — entry / SL / T1 / T2\n"
        "• /bands — Bollinger view\n"
        "• /layers — 10-layer score\n"
        "• /health — bot connection check\n"
        "• /help — command list\n\n"
        "⏰ Checks run automatically every 15 minutes.\n"
        "🎯 Automatic Entry/SL/Target update is sent every run.\n\n"
        "⚠️ <i>Technical signals only — not financial advice.</i>"
    )
    return send_telegram_message(text)


# ============================================================
# DATA FETCHING
# ============================================================

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
        ema_values[-1],
        rsi_values[-1],
        macd_line[-1],
        signal_line[-1],
        atr_value,
        vwap_value,
        adx_value,
        plus_di,
        minus_di,
        bb_upper[-1],
        bb_middle[-1],
        bb_lower[-1],
        bb_bandwidth[-1],
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

    # --------------------------------------------------------
    # Layer 1: Higher-timeframe trend
    # --------------------------------------------------------
    htf_bull = htf_price > htf_ema_value
    htf_bear = htf_price < htf_ema_value

    # --------------------------------------------------------
    # Layer 2: Current EMA-50 trend
    # --------------------------------------------------------
    ema_bull = price > ema_value
    ema_bear = price < ema_value

    # --------------------------------------------------------
    # Layer 3: MACD momentum
    # --------------------------------------------------------
    macd_bull = macd_value > macd_signal_value
    macd_bear = macd_value < macd_signal_value

    # --------------------------------------------------------
    # Layer 4: RSI
    # Avoid buying at extreme RSI or selling at extreme RSI.
    # --------------------------------------------------------
    rsi_bull = 50 < rsi_value < 72
    rsi_bear = 28 < rsi_value < 50

    # --------------------------------------------------------
    # Layer 5: VWAP
    # --------------------------------------------------------
    vwap_bull = price > vwap_value
    vwap_bear = price < vwap_value

    # --------------------------------------------------------
    # Layer 6: ADX + DI
    # --------------------------------------------------------
    strong_trend = adx_value >= ADX_MIN_THRESHOLD
    adx_bull = strong_trend and plus_di > minus_di
    adx_bear = strong_trend and minus_di > plus_di

    # --------------------------------------------------------
    # Layer 7: Bollinger
    # Breakout is directional only when price closes outside band.
    # Otherwise it is supportive when price is on the correct side
    # of the middle band.
    # --------------------------------------------------------
    recent_bandwidths = [
        v for v in bb_bandwidth[-50:] if v is not None
    ]

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

    # --------------------------------------------------------
    # Layer 8: EMA Angle
    # --------------------------------------------------------
    strong_angle = abs(ema_angle_value) >= ANGLE_MIN_THRESHOLD
    angle_bull = ema_angle_value >= ANGLE_MIN_THRESHOLD
    angle_bear = ema_angle_value <= -ANGLE_MIN_THRESHOLD

    # --------------------------------------------------------
    # Layer 9: Price action
    # --------------------------------------------------------
    pa_bull = price_action == "BULLISH"
    pa_bear = price_action == "BEARISH"

    # --------------------------------------------------------
    # Layer 10: Session + volatility safety
    # This layer is mandatory safety, not a directional vote.
    # --------------------------------------------------------
    good_session = in_trading_session()
    volatility_ok = volatility_safe(price, atr_value)
    safety_ok = good_session and volatility_ok and price_action != "EXTENDED"

    bullish_layers = [
        htf_bull,
        ema_bull,
        macd_bull,
        rsi_bull,
        vwap_bull,
        adx_bull,
        bb_bull,
        angle_bull,
        pa_bull,
        safety_ok,
    ]
    bearish_layers = [
        htf_bear,
        ema_bear,
        macd_bear,
        rsi_bear,
        vwap_bear,
        adx_bear,
        bb_bear,
        angle_bear,
        pa_bear,
        safety_ok,
    ]

    bullish_score = sum(bool(x) for x in bullish_layers)
    bearish_score = sum(bool(x) for x in bearish_layers)

    signal = "HOLD"
    filter_reason = None

    if safety_ok and bullish_score >= MIN_DIRECTIONAL_SCORE:
        if bullish_score - bearish_score >= MIN_SCORE_EDGE:
            signal = "BUY"
        else:
            filter_reason = "Bullish score exists but directional edge is weak."

    elif safety_ok and bearish_score >= MIN_DIRECTIONAL_SCORE:
        if bearish_score - bullish_score >= MIN_SCORE_EDGE:
            signal = "SELL"
        else:
            filter_reason = "Bearish score exists but directional edge is weak."

    else:
        reasons = []
        if not good_session:
            reasons.append("outside London/NY session")
        if not volatility_ok:
            reasons.append("ATR volatility outside safe range")
        if price_action == "EXTENDED":
            reasons.append("latest candle is extended")
        if bullish_score < MIN_DIRECTIONAL_SCORE and bearish_score < MIN_DIRECTIONAL_SCORE:
            reasons.append("score threshold not reached")
        filter_reason = ", ".join(reasons) if reasons else "setup not confirmed"

    return {
        "signal": signal,
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
        "strong_angle": strong_angle,
        "price_action": price_action,
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
            "Session + Volatility": "PASS" if safety_ok else "BLOCK",
        },
        "filter_reason": filter_reason,
    }


# ============================================================
# TRADE MAP
# ============================================================

def trade_levels(a):
    price = a["price"]
    atr_value = a["atr"]

    if a["signal"] == "SELL":
        return {
            "entry": price,
            "sl": price + SL_ATR_MULT * atr_value,
            "t1": price - T1_ATR_MULT * atr_value,
            "t2": price - T2_ATR_MULT * atr_value,
        }

    return {
        "entry": price,
        "sl": price - SL_ATR_MULT * atr_value,
        "t1": price + T1_ATR_MULT * atr_value,
        "t2": price + T2_ATR_MULT * atr_value,
    }


# ============================================================
# TELEGRAM FORMATTING
# ============================================================

def format_alert(a):
    levels = trade_levels(a)
    signal = a["signal"]
    emoji = "🟢" if signal == "BUY" else "🔴"
    score = (
        a["bullish_score"]
        if signal == "BUY"
        else a["bearish_score"]
    )

    return (
        f"{emoji} <b>{signal} • GOLD V5</b>\n"
        f"<b>{esc(INSTRUMENT)}</b> • {esc(TIMEFRAME)}\n\n"
        f"🔥 <b>10-Layer Score:</b> {score}/10\n"
        f"🧭 <b>HTF:</b> {fmt_price(a['htf_price'])} vs EMA {fmt_price(a['htf_ema'])}\n"
        f"💪 <b>ADX:</b> {a['adx']:.1f} "
        f"(DI+ {a['plus_di']:.1f} / DI- {a['minus_di']:.1f})\n"
        f"📐 <b>EMA Angle:</b> {a['ema_angle']:.1f}°\n"
        f"🕯️ <b>Price Action:</b> {esc(a['price_action'])}\n"
        f"🛡️ <b>Safety:</b> PASS ✓\n\n"
        f"💰 <b>Price:</b> {fmt_price(a['price'])}\n"
        f"📈 <b>RSI:</b> {a['rsi']:.1f}\n"
        f"📊 <b>MACD:</b> {a['macd']:.2f} "
        f"(Signal {a['macd_signal']:.2f})\n"
        f"⚖️ <b>VWAP:</b> {fmt_price(a['vwap'])}\n"
        f"📐 <b>Bollinger:</b> {esc(a['bb_position']) if 'bb_position' in a else 'Active'}"
        f"{' ⚡ Squeeze!' if a['bb_squeeze'] else ''}\n\n"
        f"🎯 <b>TRADE MAP</b>\n"
        f"Entry  • {fmt_price(levels['entry'])}\n"
        f"SL     • {fmt_price(levels['sl'])}\n"
        f"T1     • {fmt_price(levels['t1'])}\n"
        f"T2     • {fmt_price(levels['t2'])}\n\n"
        f"🕒 {stamp()}\n"
        "⚠️ <i>Technical signal only. Manage your own risk.</i>"
    )


def format_status_reply(a):
    if a["bullish_score"] > a["bearish_score"]:
        bias = "BULLISH 🟢"
    elif a["bearish_score"] > a["bullish_score"]:
        bias = "BEARISH 🔴"
    else:
        bias = "MIXED ⚪"

    signal = a["signal"]
    if signal == "BUY":
        signal_line = "🟢 <b>BUY — high-confidence setup active</b>"
    elif signal == "SELL":
        signal_line = "🔴 <b>SELL — high-confidence setup active</b>"
    else:
        signal_line = "⚪ <b>HOLD — no high-confidence setup</b>"

    filter_line = ""
    if a.get("filter_reason"):
        filter_line = f"\n🛡️ <b>Filter:</b> {esc(a['filter_reason'])}"

    return (
        "📊 <b>GOLD MARKET STATUS — V5</b>\n"
        f"{esc(INSTRUMENT)} • {esc(TIMEFRAME)} / HTF {esc(HTF_TIMEFRAME)}\n\n"
        f"💰 <b>Price:</b> {fmt_price(a['price'])}\n"
        f"🧭 <b>Bias:</b> {bias}\n"
        f"🎯 <b>Signal:</b> {signal}\n\n"
        f"HTF EMA • {fmt_price(a['htf_ema'])}\n"
        f"EMA-50  • {fmt_price(a['ema'])}\n"
        f"VWAP    • {fmt_price(a['vwap'])}\n"
        f"RSI-14  • {a['rsi']:.1f}\n"
        f"MACD    • {a['macd']:.2f} / {a['macd_signal']:.2f}\n"
        f"ADX-14  • {a['adx']:.1f}\n"
        f"DI+ / DI- • {a['plus_di']:.1f} / {a['minus_di']:.1f}\n"
        f"EMA∠    • {a['ema_angle']:.1f}°\n"
        f"ATR-14  • {fmt_price(a['atr'])}\n"
        f"Price Action • {esc(a['price_action'])}\n\n"
        f"🟢 Bullish score: {a['bullish_score']}/10\n"
        f"🔴 Bearish score: {a['bearish_score']}/10\n"
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
        headline = "🟢 <b>BUY</b>"
        score = a["bullish_score"]
        reason = "Strong bullish 10-layer score with mandatory safety filters passed."
    elif a["signal"] == "SELL":
        headline = "🔴 <b>SELL</b>"
        score = a["bearish_score"]
        reason = "Strong bearish 10-layer score with mandatory safety filters passed."
    else:
        headline = "⚪ <b>HOLD</b>"
        score = max(a["bullish_score"], a["bearish_score"])
        reason = a.get("filter_reason") or "No high-confidence setup is active."

    return (
        "🎯 <b>GOLD V5 SIGNAL</b>\n\n"
        f"{headline}\n"
        f"💰 Price: {fmt_price(a['price'])}\n"
        f"🔥 Best score: {score}/10\n"
        f"💪 ADX: {a['adx']:.1f}\n"
        f"🛡️ Safety: {'PASS' if a['safety_ok'] else 'BLOCK'}\n\n"
        f"{esc(reason)}\n\n"
        f"🕒 {stamp()}"
    )


def format_price_reply(a):
    return (
        "💰 <b>GOLD PRICE — V5</b>\n\n"
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
        "📐 <b>BOLLINGER BANDS V5 (20, 2σ)</b>\n\n"
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
        "🎯 <b>GOLD V5 TRADE MAP</b>\n\n"
        f"Direction: <b>{direction}</b>\n"
        f"Entry: <b>{fmt_price(levels['entry'])}</b>\n"
        f"Stop Loss: <b>{fmt_price(levels['sl'])}</b>\n"
        f"Target 1: <b>{fmt_price(levels['t1'])}</b>\n"
        f"Target 2: <b>{fmt_price(levels['t2'])}</b>\n\n"
        f"ATR-14: {fmt_price(a['atr'])}\n"
        f"Method: {SL_ATR_MULT}× ATR SL / "
        f"{T1_ATR_MULT}× ATR T1 / {T2_ATR_MULT}× ATR T2\n\n"
        f"🕒 {stamp()}\n"
        "⚠️ <i>These are ATR-based technical levels, not guaranteed targets.</i>"
    )


def format_layers_reply(a):
    lines = ["🧠 <b>V5 — 10-LAYER ENGINE</b>\n"]

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

        lines.append(f"{index}. {icon} <b>{esc(name)}</b> — {esc(state)}")

    lines.extend([
        "",
        f"🟢 Bullish: {a['bullish_score']}/10",
        f"🔴 Bearish: {a['bearish_score']}/10",
        f"🎯 Signal: <b>{a['signal']}</b>",
        f"🛡️ Safety: {'PASS' if a['safety_ok'] else 'BLOCK'}",
        "",
        f"🕒 {stamp()}",
    ])
    return "\n".join(lines)


def format_help():
    lines = ["📚 <b>GOLD SIGNALS V5 — COMMAND CENTER</b>\n"]
    for command, description in COMMANDS.items():
        lines.append(f"{command} — {description}")

    lines.extend([
        "",
        "🤖 <b>Automatic mode:</b> V5 evaluates 10 strategy layers "
        "on every configured GitHub Actions run.",
        "🎯 BUY/SELL requires a strong layer score plus mandatory "
        "session/volatility safety.",
        "⏱️ Commands are processed on the next scheduled run.",
        "",
        "⚠️ <i>Technical signals only — not financial advice.</i>",
    ])
    return "\n".join(lines)


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
        "🩺 <b>V5 BOT HEALTH CHECK</b>\n\n"
        f"Telegram API: {telegram_status}{bot_name}\n"
        f"Market API: {market_status}\n"
        f"Instrument: {esc(INSTRUMENT)}\n"
        f"Timeframe: {esc(TIMEFRAME)}\n"
        f"HTF: {esc(HTF_TIMEFRAME)}\n"
        "Strategy: 10-Layer V5\n\n"
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
    last_update_id = int(state.get("last_update_id", 0) or 0)

    print("=" * 60)
    print("GOLD SIGNAL BOT V5 — RUN START")
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
                "⚠️ <b>GOLD V5 — DATA ERROR</b>\n\n"
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
                "to calculate all V5 layers yet."
            )

        write_state(state)
        return

    print(
        f"Price={result['price']:.2f} | "
        f"Signal={result['signal']} | "
        f"Bull={result['bullish_score']}/10 | "
        f"Bear={result['bearish_score']}/10 | "
        f"ADX={result['adx']:.1f} | "
        f"PA={result['price_action']} | "
        f"Safety={result['safety_ok']}"
    )

    if result.get("filter_reason"):
        print(f"[FILTERED] {result['filter_reason']}")

    # 3. Automatic high-confidence alert only when signal changes.
    if (
        result["signal"] in ("BUY", "SELL")
        and result["signal"] != last_signal
    ):
        if send_telegram_message(format_alert(result)):
            state["last_signal"] = result["signal"]
            print(f"[AUTO] New {result['signal']} alert sent.")

    elif result["signal"] == "HOLD":
        state["last_signal"] = None
        print("[AUTO] HOLD — no alert.")

    else:
        print("[AUTO] Same signal as previous run — no duplicate alert.")

    # 4. Automatic target update every run.
    if send_telegram_message(format_target_reply(result)):
        print("[AUTO] Automatic target update sent.")

    # 5. Process explicit Telegram commands.
    if commands:
        process_commands(commands, result)

    state["last_success_ist"] = stamp()
    write_state(state)

    print("[DONE] V5 state saved successfully.")


if __name__ == "__main__":
    main()
