"""
Gold Trading Signals — Professional Telegram Bot
================================================
GitHub Actions / Telegram edition.

Keeps the original 6-layer strategy:
  1. EMA-50  -> trend
  2. MACD    -> momentum
  3. RSI-14  -> momentum/bias
  4. VWAP    -> intraday fair-value bias
  5. ADX-14  -> trend strength
  6. Session -> London/NY trading window

The bot:
  • Sends a welcome message on /start
  • Supports /help, /status, /signal, /price, /target, /health
  • Sends high-confidence BUY/SELL alerts automatically
  • Avoids repeating the same automatic signal
  • Persists Telegram update ID and signal state in bot_state.json
  • Uses IST timestamps for a cleaner India-based Telegram experience
  • Validates Telegram credentials and reports API/data errors clearly

IMPORTANT:
  This remains a technical signal tool, not financial advice.
  GitHub Actions runs on a schedule, so Telegram commands are not instant.
"""

import html
import json
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
STATE_FILE = os.environ.get("STATE_FILE", "bot_state.json")

EMA_PERIOD = 50
RSI_PERIOD = 14
ATR_PERIOD = 14
ADX_PERIOD = 14
ADX_MIN_THRESHOLD = 25
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9

# Original strategy's automatic-alert session:
# 07:00–16:00 UTC = 12:30–21:30 IST.
SESSION_START_UTC = 7
SESSION_END_UTC = 16

CANDLESTICK_URL = "https://api.crypto.com/exchange/v1/public/get-candlestick"
TELEGRAM_API = "https://api.telegram.org/bot{token}"

COMMANDS = {
    "/start": "Welcome + current market snapshot",
    "/help": "Show available commands",
    "/status": "Current market status",
    "/signal": "Current BUY / SELL / HOLD state",
    "/price": "Current gold price",
    "/target": "ATR-based entry, SL and targets",
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
    """Validate the bot token and return Telegram bot information."""
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
    """
    Read new Telegram messages.

    Only messages from the configured CHAT_ID are processed.
    Returns:
      (commands, start_requested, new_max_update_id)
    """
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

            # Ignore everybody except the configured Telegram chat.
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
        "👋 <b>Welcome to Gold Trading Signals</b>\n\n"
        "🤖 <b>Bot status:</b> CONNECTED\n"
        f"🥇 <b>Market:</b> {esc(INSTRUMENT)}\n"
        f"⏱️ <b>Timeframe:</b> {esc(TIMEFRAME)}\n\n"
        "📊 <b>6-Layer Strategy</b>\n"
        "• EMA-50 — Trend\n"
        "• MACD — Momentum\n"
        "• RSI-14 — Bias\n"
        "• VWAP — Fair value\n"
        "• ADX-14 — Trend strength\n"
        "• Session filter — London/NY window\n\n"
        "<b>Commands</b>\n"
        "• /status — market snapshot\n"
        "• /signal — BUY / SELL / HOLD\n"
        "• /price — live candle price\n"
        "• /target — entry / SL / T1 / T2\n"
        "• /health — bot connection check\n"
        "• /help — command list\n\n"
        "⏰ Checks run automatically every 15 minutes.\n\n"
        "⚠️ <i>Technical signals only — not financial advice.</i>"
    )
    return send_telegram_message(text)


# ============================================================
# DATA FETCHING
# ============================================================

def fetch_candles(instrument, timeframe, count=150):
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

    for candle in candles:
        candle["o"] = float(candle["o"])
        candle["h"] = float(candle["h"])
        candle["l"] = float(candle["l"])
        candle["c"] = float(candle["c"])
        candle["v"] = float(candle["v"])

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


def atr(candles, period=14):
    trs = []

    for i in range(1, len(candles)):
        high = candles[i]["h"]
        low = candles[i]["l"]
        previous_close = candles[i - 1]["c"]

        tr = max(
            high - low,
            abs(high - previous_close),
            abs(low - previous_close),
        )
        trs.append(tr)

    if len(trs) < period:
        return None

    return sum(trs[-period:]) / period


def calculate_adx(candles, period=14):
    highs = [c["h"] for c in candles]
    lows = [c["l"] for c in candles]
    closes = [c["c"] for c in candles]

    trs, plus_dm, minus_dm = [], [], []

    for i in range(1, len(candles)):
        high = highs[i]
        low = lows[i]
        previous_close = closes[i - 1]
        previous_high = highs[i - 1]
        previous_low = lows[i - 1]

        tr = max(
            high - low,
            abs(high - previous_close),
            abs(low - previous_close),
        )
        trs.append(tr)

        up_move = high - previous_high
        down_move = previous_low - low

        plus_dm.append(
            up_move if up_move > down_move and up_move > 0 else 0
        )
        minus_dm.append(
            down_move if down_move > up_move and down_move > 0 else 0
        )

    if len(trs) < period * 2:
        return None

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

    dx = [None] * len(trs)

    for i in range(period - 1, len(trs)):
        if tr_s[i]:
            plus_di = 100 * (plus_dm_s[i] / tr_s[i])
            minus_di = 100 * (minus_dm_s[i] / tr_s[i])
            di_sum = plus_di + minus_di

            dx[i] = (
                100 * abs(plus_di - minus_di) / di_sum
                if di_sum != 0 else 0
            )

    clean = [value for value in dx if value is not None]

    if len(clean) < period:
        return None

    adx_value = sum(clean[:period]) / period

    for i in range(period, len(clean)):
        adx_value = (
            (adx_value * (period - 1) + clean[i]) / period
        )

    return adx_value


def in_trading_session():
    utc_hour = datetime.now(timezone.utc).hour
    return SESSION_START_UTC <= utc_hour < SESSION_END_UTC


def session_vwap(candles):
    cumulative_pv = 0
    cumulative_volume = 0

    for candle in candles:
        typical = (
            candle["h"] + candle["l"] + candle["c"]
        ) / 3

        cumulative_pv += typical * candle["v"]
        cumulative_volume += candle["v"]

    return (
        cumulative_pv / cumulative_volume
        if cumulative_volume > 0 else None
    )


# ============================================================
# ANALYSIS
# ============================================================

def analyze(candles):
    closes = [c["c"] for c in candles]
    price = closes[-1]

    ema_values = ema(closes, EMA_PERIOD)
    rsi_values = rsi(closes, RSI_PERIOD)
    macd_line, signal_line = macd(
        closes, MACD_FAST, MACD_SLOW, MACD_SIGNAL
    )
    atr_value = atr(candles, ATR_PERIOD)
    vwap_value = session_vwap(candles[-96:])
    adx_value = calculate_adx(candles, ADX_PERIOD)

    ema_value = ema_values[-1]
    rsi_value = rsi_values[-1]
    macd_value = macd_line[-1]
    macd_signal_value = signal_line[-1]

    if None in (
        ema_value,
        rsi_value,
        macd_value,
        macd_signal_value,
        vwap_value,
        atr_value,
        adx_value,
    ):
        return None

    bullish = 0
    bearish = 0

    if price > ema_value:
        bullish += 1
    else:
        bearish += 1

    if macd_value > macd_signal_value:
        bullish += 1
    else:
        bearish += 1

    if rsi_value > 50:
        bullish += 1
    else:
        bearish += 1

    if price > vwap_value:
        bullish += 1
    else:
        bearish += 1

    strong_trend = adx_value >= ADX_MIN_THRESHOLD
    good_session = in_trading_session()

    signal = "HOLD"
    filter_reason = None

    if bullish == 4:
        if strong_trend and good_session:
            signal = "BUY"
        else:
            filter_reason = (
                f"4/4 bullish, but filtered: "
                f"ADX={adx_value:.1f}, session={good_session}"
            )

    elif bearish == 4:
        if strong_trend and good_session:
            signal = "SELL"
        else:
            filter_reason = (
                f"4/4 bearish, but filtered: "
                f"ADX={adx_value:.1f}, session={good_session}"
            )

    return {
        "signal": signal,
        "price": price,
        "ema": ema_value,
        "rsi": rsi_value,
        "macd": macd_value,
        "macd_signal": macd_signal_value,
        "vwap": vwap_value,
        "atr": atr_value,
        "adx": adx_value,
        "good_session": good_session,
        "strong_trend": strong_trend,
        "bullish_votes": bullish,
        "bearish_votes": bearish,
        "filter_reason": filter_reason,
    }


def trade_levels(a):
    price = a["price"]
    atr_value = a["atr"]

    if a["signal"] == "SELL":
        return {
            "entry": price,
            "sl": price + (1.5 * atr_value),
            "t1": price - (1.5 * atr_value),
            "t2": price - (3.0 * atr_value),
        }

    return {
        "entry": price,
        "sl": price - (1.5 * atr_value),
        "t1": price + (1.5 * atr_value),
        "t2": price + (3.0 * atr_value),
    }


# ============================================================
# TELEGRAM FORMATTING
# ============================================================

def format_alert(a):
    levels = trade_levels(a)
    signal = a["signal"]

    emoji = "🟢" if signal == "BUY" else "🔴"
    votes = (
        a["bullish_votes"]
        if signal == "BUY"
        else a["bearish_votes"]
    )

    return (
        f"{emoji} <b>{signal} • GOLD</b>\n"
        f"<b>{esc(INSTRUMENT)}</b> • {esc(TIMEFRAME)}\n\n"
        f"🔥 <b>Confidence:</b> {votes}/4 directional indicators\n"
        f"💪 <b>ADX:</b> {a['adx']:.1f} • Strong trend ✓\n"
        f"🕐 <b>Session:</b> Active ✓\n\n"
        f"💰 <b>Price:</b> {fmt_price(a['price'])}\n"
        f"📈 <b>RSI:</b> {a['rsi']:.1f}\n"
        f"📊 <b>MACD:</b> {a['macd']:.2f} "
        f"(Signal {a['macd_signal']:.2f})\n"
        f"〽️ <b>EMA-50:</b> {fmt_price(a['ema'])}\n"
        f"⚖️ <b>VWAP:</b> {fmt_price(a['vwap'])}\n"
        f"🌊 <b>ATR:</b> {fmt_price(a['atr'])}\n\n"
        f"🎯 <b>TRADE MAP</b>\n"
        f"Entry  • {fmt_price(levels['entry'])}\n"
        f"SL     • {fmt_price(levels['sl'])}\n"
        f"T1     • {fmt_price(levels['t1'])}\n"
        f"T2     • {fmt_price(levels['t2'])}\n\n"
        f"🕒 {stamp()}\n"
        "⚠️ <i>Technical signal only. Manage your own risk.</i>"
    )


def format_status_reply(a):
    if a["bullish_votes"] > a["bearish_votes"]:
        bias = "BULLISH 🟢"
    elif a["bearish_votes"] > a["bullish_votes"]:
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
        "📊 <b>GOLD MARKET STATUS</b>\n"
        f"{esc(INSTRUMENT)} • {esc(TIMEFRAME)}\n\n"
        f"💰 <b>Price:</b> {fmt_price(a['price'])}\n"
        f"🧭 <b>Bias:</b> {bias}\n"
        f"🎯 <b>Signal:</b> {signal}\n\n"
        f"EMA-50  • {fmt_price(a['ema'])}\n"
        f"VWAP    • {fmt_price(a['vwap'])}\n"
        f"RSI-14  • {a['rsi']:.1f}\n"
        f"MACD    • {a['macd']:.2f} / {a['macd_signal']:.2f}\n"
        f"ADX-14  • {a['adx']:.1f}\n"
        f"ATR-14  • {fmt_price(a['atr'])}\n\n"
        f"🟢 Bullish votes: {a['bullish_votes']}/4\n"
        f"🔴 Bearish votes: {a['bearish_votes']}/4\n"
        f"💪 Strong trend: {'YES' if a['strong_trend'] else 'NO'}\n"
        f"🕐 Session active: {'YES' if a['good_session'] else 'NO'}"
        f"{filter_line}\n\n"
        f"{signal_line}\n\n"
        f"🕒 {stamp()}\n"
        "⚠️ <i>Technical view only — not financial advice.</i>"
    )


def format_signal_reply(a):
    if a["signal"] == "BUY":
        headline = "🟢 <b>BUY</b>"
        reason = "All 4 directional indicators agree + ADX/session filters passed."
    elif a["signal"] == "SELL":
        headline = "🔴 <b>SELL</b>"
        reason = "All 4 directional indicators agree + ADX/session filters passed."
    else:
        headline = "⚪ <b>HOLD</b>"
        reason = "No high-confidence BUY/SELL setup is active."

    return (
        f"🎯 <b>GOLD SIGNAL</b>\n\n"
        f"{headline}\n"
        f"💰 Price: {fmt_price(a['price'])}\n"
        f"🧭 Bias votes: {a['bullish_votes']} bullish / "
        f"{a['bearish_votes']} bearish\n"
        f"💪 ADX: {a['adx']:.1f}\n\n"
        f"{esc(reason)}\n\n"
        f"🕒 {stamp()}"
    )


def format_price_reply(a):
    return (
        "💰 <b>GOLD PRICE</b>\n\n"
        f"🥇 {esc(INSTRUMENT)}\n"
        f"💵 <b>{fmt_price(a['price'])}</b>\n"
        f"⏱️ Timeframe: {esc(TIMEFRAME)}\n"
        f"🕒 {stamp()}\n\n"
        "ℹ️ Price is from the configured market-data source."
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
        "🎯 <b>GOLD TRADE MAP</b>\n\n"
        f"Direction: <b>{direction}</b>\n"
        f"Entry: <b>{fmt_price(levels['entry'])}</b>\n"
        f"Stop Loss: <b>{fmt_price(levels['sl'])}</b>\n"
        f"Target 1: <b>{fmt_price(levels['t1'])}</b>\n"
        f"Target 2: <b>{fmt_price(levels['t2'])}</b>\n\n"
        f"ATR-14: {fmt_price(a['atr'])}\n"
        "Method: 1.5× ATR SL/T1 and 3× ATR T2\n\n"
        f"🕒 {stamp()}\n"
        "⚠️ <i>These are ATR-based technical levels, not guaranteed targets.</i>"
    )


def format_help():
    lines = ["📚 <b>GOLD SIGNALS — COMMAND CENTER</b>\n"]

    for command, description in COMMANDS.items():
        lines.append(f"{command} — {description}")

    lines.extend([
        "",
        "🤖 <b>Automatic mode:</b> high-confidence BUY/SELL alerts are sent when the "
        "6-layer rules pass.",
        "⏱️ GitHub Actions checks on its configured schedule; commands are processed "
        "on the next run.",
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
        "🩺 <b>BOT HEALTH CHECK</b>\n\n"
        f"Telegram API: {telegram_status}{bot_name}\n"
        f"Market API: {market_status}\n"
        f"Instrument: {esc(INSTRUMENT)}\n"
        f"Timeframe: {esc(TIMEFRAME)}\n"
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

            elif command == "/health":
                telegram_ok, bot_info = telegram_health()

                market_ok = True
                market_error = None
                try:
                    fetch_candles(INSTRUMENT, TIMEFRAME, count=2)
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
    print("GOLD SIGNAL BOT — RUN START")
    print(f"Market: {INSTRUMENT}")
    print(f"Timeframe: {TIMEFRAME}")
    print(f"Time: {stamp()}")
    print("=" * 60)

    # 1. Read Telegram messages first.
    commands, start_requested, new_update_id = get_new_updates(last_update_id)
    state["last_update_id"] = new_update_id

    if commands:
        print(f"[TELEGRAM] Commands received: {commands}")

    # 2. Fetch market data.
    try:
        candles = fetch_candles(INSTRUMENT, TIMEFRAME, count=150)
        result = analyze(candles)
    except Exception as exc:
        print(f"[ERROR] Market data/analysis failed: {exc}")

        # If the user requested something, at least return a useful error.
        if commands:
            send_telegram_message(
                "⚠️ <b>GOLD BOT — DATA ERROR</b>\n\n"
                f"Unable to calculate the requested market information.\n\n"
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
                "to calculate all indicators yet."
            )

        write_state(state)
        return

    print(
        f"Price={result['price']:.2f} | "
        f"Signal={result['signal']} | "
        f"Bull={result['bullish_votes']} | "
        f"Bear={result['bearish_votes']} | "
        f"ADX={result['adx']:.1f} | "
        f"Session={result['good_session']}"
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
        # Reset so a future BUY/SELL can alert again.
        state["last_signal"] = None
        print("[AUTO] HOLD — no alert.")

    else:
        print("[AUTO] Same signal as previous run — no duplicate alert.")

    # 4. Process explicit Telegram commands.
    if commands:
        process_commands(commands, result)

    state["last_success_ist"] = stamp()
    write_state(state)

    print("[DONE] State saved successfully.")


if __name__ == "__main__":
    main()
