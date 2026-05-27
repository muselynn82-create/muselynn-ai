import os
import time
import requests
import pandas as pd
import gspread

from oauth2client.service_account import ServiceAccountCredentials
from binance.client import Client
from datetime import datetime
from zoneinfo import ZoneInfo

SYMBOL = "BTCUSDT"

ENTRY_SCORE = 70
RSI_LIMIT = 26
TAKE_PROFIT = 1.80
STOP_LOSS = -1.50
TRAIL_START = 1.50
TRAIL_BACK = 0.70
FEE_ROUND_TRIP = 0.20

REENTRY_COOLDOWN_MINUTES = 15
MIN_HOLD_MINUTES = 5
MAX_CONSECUTIVE_LOSSES = 5
LOOP_SLEEP_SECONDS = 180
USE_TIME_FILTER = True

KST = ZoneInfo("Asia/Seoul")

API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_SECRET_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GOOGLE_CLIENT_EMAIL = os.getenv("GOOGLE_CLIENT_EMAIL")
GOOGLE_PRIVATE_KEY = os.getenv("GOOGLE_PRIVATE_KEY", "").replace("\\n", "\n")
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME")

client = Client(API_KEY, API_SECRET)


def now_kst():
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")


def send_telegram(message, force=False):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return
    now = datetime.now(KST)
    if not force and 3 <= now.hour < 8:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": message},
            timeout=10,
        )
    except Exception as e:
        print(f"Telegram error: {e}", flush=True)


scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]

creds_dict = {
    "type": "service_account",
    "project_id": os.getenv("GOOGLE_PROJECT_ID"),
    "private_key_id": os.getenv("GOOGLE_PRIVATE_KEY_ID"),
    "private_key": GOOGLE_PRIVATE_KEY,
    "client_email": GOOGLE_CLIENT_EMAIL,
    "client_id": os.getenv("GOOGLE_CLIENT_ID"),
    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
    "token_uri": "https://oauth2.googleapis.com/token",
    "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
    "client_x509_cert_url": f"https://www.googleapis.com/robot/v1/metadata/x509/{GOOGLE_CLIENT_EMAIL}",
}
credentials = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
gc = gspread.authorize(credentials)
spreadsheet = gc.open(GOOGLE_SHEET_NAME)

try:
    sheet = spreadsheet.worksheet("BTC_TRADING_LOG")
except gspread.WorksheetNotFound:
    sheet = spreadsheet.add_worksheet(title="BTC_TRADING_LOG", rows=5000, cols=30)

try:
    state_sheet = spreadsheet.worksheet("STATE_OPT_LONG")
except gspread.WorksheetNotFound:
    state_sheet = spreadsheet.add_worksheet(title="STATE_OPT_LONG", rows=40, cols=2)

HEADERS = [
    "time", "candle_time", "symbol", "big_trend", "strategy", "signal",
    "price", "rsi", "score", "ema20", "ema50", "ema100", "ema200",
    "position_open", "entry_price", "gross_pnl", "net_pnl",
    "total_trades", "win_rate", "cumulative_pnl", "exit_reason", "strategy_enabled",
]


def init_sheet_header():
    sheet.update(range_name="A1:V1", values=[HEADERS])


def save_log(data):
    print("SHEET WRITE START", flush=True)
    sheet.append_row([
        data["time"], data["candle_time"], data["symbol"], data["big_trend"],
        data["strategy"], data["signal"], data["price"], data["rsi"],
        data["score"], data["ema20"], data["ema50"], data["ema100"],
        data["ema200"], data["position_open"], data["entry_price"],
        data["gross_pnl"], data["net_pnl"], data["total_trades"],
        data["win_rate"], data["cumulative_pnl"], data["exit_reason"],
        data["strategy_enabled"],
    ])
    print("SHEET WRITE SUCCESS", flush=True)


position_open = False
last_report_time = 0
entry_price = 0.0
entry_time = None
entry_candle_time = None
entry_big_trend = None
entry_strategy = None
entry_score = 0
max_pnl = 0.0
last_exit_time = None
strategy_enabled = True
total_trades = 0
win_trades = 0
loss_trades = 0
consecutive_losses = 0
cumulative_pnl = 0.0
last_report_time = time.time()
error_count = 0
signal_count = 0


def safe_float(value, default=0.0):
    try:
        if value in ["", None]:
            return default
        return float(value)
    except Exception:
        return default


def safe_int(value, default=0):
    try:
        if value in ["", None]:
            return default
        return int(float(value))
    except Exception:
        return default


def safe_bool(value, default=False):
    if value in ["True", "TRUE", "true", True]:
        return True
    if value in ["False", "FALSE", "false", False]:
        return False
    return default


def load_state():
    global position_open, entry_price, entry_time, entry_candle_time
    global entry_big_trend, entry_strategy, entry_score, max_pnl
    global last_exit_time, strategy_enabled
    global total_trades, win_trades, loss_trades, consecutive_losses, cumulative_pnl
    rows = state_sheet.get_all_records()
    if not rows:
        save_state()
        return
    state = {str(row.get("key")): row.get("value") for row in rows if row.get("key")}
    position_open = safe_bool(state.get("position_open"), False)
    entry_price = safe_float(state.get("entry_price"), 0.0)
    entry_time = state.get("entry_time") or None
    entry_candle_time = state.get("entry_candle_time") or None
    entry_big_trend = state.get("entry_big_trend") or None
    entry_strategy = state.get("entry_strategy") or None
    entry_score = safe_int(state.get("entry_score"), 0)
    max_pnl = safe_float(state.get("max_pnl"), 0.0)
    last_exit_time = state.get("last_exit_time") or None
    strategy_enabled = safe_bool(state.get("strategy_enabled"), True)
    total_trades = safe_int(state.get("total_trades"), 0)
    win_trades = safe_int(state.get("win_trades"), 0)
    loss_trades = safe_int(state.get("loss_trades"), 0)
    consecutive_losses = safe_int(state.get("consecutive_losses"), 0)
    cumulative_pnl = safe_float(state.get("cumulative_pnl"), 0.0)


def save_state():
    values = [
        ["position_open", str(position_open)],
        ["entry_price", str(entry_price)],
        ["entry_time", entry_time or ""],
        ["entry_candle_time", entry_candle_time or ""],
        ["entry_big_trend", entry_big_trend or ""],
        ["entry_strategy", entry_strategy or ""],
        ["entry_score", str(entry_score)],
        ["max_pnl", str(max_pnl)],
        ["last_exit_time", last_exit_time or ""],
        ["strategy_enabled", str(strategy_enabled)],
        ["total_trades", str(total_trades)],
        ["win_trades", str(win_trades)],
        ["loss_trades", str(loss_trades)],
        ["consecutive_losses", str(consecutive_losses)],
        ["cumulative_pnl", str(cumulative_pnl)],
    ]
    state_sheet.update(range_name="A1:B16", values=[["key", "value"]] + values)


def get_klines(interval, limit):
    candles = client.get_klines(symbol=SYMBOL, interval=interval, limit=limit)
    df = pd.DataFrame(candles, columns=[
        "time", "open", "high", "low", "close", "volume",
        "close_time", "quote_asset_volume", "trades",
        "taker_buy_base", "taker_buy_quote", "ignore",
    ])
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    df["datetime"] = pd.to_datetime(df["time"], unit="ms", utc=True).dt.tz_convert("Asia/Seoul")
    return df


def calculate_indicators(df):
    df = df.copy()
    close = df["close"]
    high = df["high"]
    low = df["low"]
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(14).mean()
    avg_loss = loss.rolling(14).mean()
    rs = avg_gain / avg_loss
    df["rsi"] = 100 - (100 / (1 + rs))
    df["bb_mid"] = close.rolling(20).mean()
    std = close.rolling(20).std()
    df["bb_upper"] = df["bb_mid"] + std * 2
    df["bb_lower"] = df["bb_mid"] - std * 2
    df["ema20"] = close.ewm(span=20, adjust=False).mean()
    df["ema50"] = close.ewm(span=50, adjust=False).mean()
    df["ema100"] = close.ewm(span=100, adjust=False).mean()
    df["ema200"] = close.ewm(span=200, adjust=False).mean()
    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    df["tr"] = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    df["atr"] = df["tr"].rolling(14).mean()
    df["atr_rate"] = df["atr"] / close
    df["volume_ma"] = df["volume"].rolling(20).mean()
    df["volume_ratio"] = df["volume"] / df["volume_ma"]
    return df


def detect_big_trend(h1, h4):
    if h1["atr_rate"] > 0.03 or h4["atr_rate"] > 0.055:
        return "BIG_CRASH"
    if (
        h4["close"] > h4["ema200"]
        and h4["ema50"] > h4["ema200"]
        and h1["close"] > h1["ema50"]
        and h1["ema20"] > h1["ema50"]
    ):
        return "BIG_BULL"
    if (
        h4["close"] < h4["ema200"]
        and h4["ema50"] < h4["ema200"]
        and h1["close"] < h1["ema50"]
        and h1["ema20"] < h1["ema50"]
    ):
        return "BIG_BEAR"
    return "NO_TRADE"


def get_strategy(big_trend):
    return "LONG_PULLBACK" if big_trend == "BIG_BULL" else "NO_TRADE"


def calculate_score(candle, strategy):
    price = candle["close"]
    score = 0
    if strategy == "LONG_PULLBACK":
        if (
            candle["rsi"] < RSI_LIMIT
            and candle["low"] <= candle["bb_lower"]
            and candle["close"] > candle["open"]
        ):
            score += 70
        if price > candle["ema100"]:
            score += 15
        if candle["volume_ratio"] >= 1.0:
            score += 10
        if price >= candle["ema20"] * 0.995:
            score += 10
    return score


def in_allowed_time(candle_time):
    if not USE_TIME_FILTER:
        return True
    hour = candle_time.hour
    return (17 <= hour <= 23) or (0 <= hour <= 5)


def get_win_rate():
    if total_trades == 0:
        return 0.0
    return round((win_trades / total_trades) * 100, 2)


def get_hold_minutes():
    if not entry_time:
        return 0
    entry_ts = time.mktime(time.strptime(entry_time, "%Y-%m-%d %H:%M:%S"))
    now_ts = time.mktime(time.strptime(now_kst(), "%Y-%m-%d %H:%M:%S"))
    return (now_ts - entry_ts) / 60


def in_cooldown():
    if not last_exit_time:
        return False
    exit_ts = time.mktime(time.strptime(last_exit_time, "%Y-%m-%d %H:%M:%S"))
    now_ts = time.mktime(time.strptime(now_kst(), "%Y-%m-%d %H:%M:%S"))
    return (now_ts - exit_ts) / 60 < REENTRY_COOLDOWN_MINUTES


def get_gross_pnl(price):
    if not position_open or entry_price == 0:
        return 0.0
    return round(((price - entry_price) / entry_price) * 100, 4)


def get_net_pnl(price):
    if not position_open or entry_price == 0:
        return 0.0
    return round(((1 + get_gross_pnl(price) / 100) * (1 - FEE_ROUND_TRIP / 100) - 1) * 100, 4)


def write_log(candle, big_trend, strategy, signal, score, exit_reason="-"):
    price = candle["close"]
    save_log({
        "time": now_kst(),
        "candle_time": candle["datetime"].strftime("%Y-%m-%d %H:%M:%S"),
        "symbol": SYMBOL,
        "big_trend": big_trend,
        "strategy": strategy,
        "signal": signal,
        "price": round(price, 2),
        "rsi": round(candle["rsi"], 2),
        "score": score,
        "ema20": round(candle["ema20"], 2),
        "ema50": round(candle["ema50"], 2),
        "ema100": round(candle["ema100"], 2),
        "ema200": round(candle["ema200"], 2),
        "position_open": position_open,
        "entry_price": round(entry_price, 2),
        "gross_pnl": get_gross_pnl(price),
        "net_pnl": get_net_pnl(price),
        "total_trades": total_trades,
        "win_rate": get_win_rate(),
        "cumulative_pnl": round(cumulative_pnl, 4),
        "exit_reason": exit_reason,
        "strategy_enabled": strategy_enabled,
    })


def close_position(candle, big_trend, strategy, score, exit_reason):
    global position_open, entry_price, entry_time, entry_candle_time
    global entry_big_trend, entry_strategy, entry_score, max_pnl
    global total_trades, win_trades, loss_trades, consecutive_losses, cumulative_pnl
    global strategy_enabled, last_exit_time, signal_count
    price = candle["close"]
    gross_pnl = get_gross_pnl(price)
    net_pnl = get_net_pnl(price)
    total_trades += 1
    cumulative_pnl += net_pnl
    if net_pnl > 0:
        win_trades += 1
        consecutive_losses = 0
    else:
        loss_trades += 1
        consecutive_losses += 1
    send_telegram(
        f"🔴 BTC 매도 신호\n\n"
        f"전략: {entry_strategy}\n사유: {exit_reason}\n장기추세: {entry_big_trend}\n"
        f"진입가: {entry_price:.2f}\n청산가: {price:.2f}\n"
        f"총수익률: {gross_pnl:.4f}%\n수수료반영: {net_pnl:.4f}%\n"
        f"누적손익: {cumulative_pnl:.4f}%\n승률: {get_win_rate()}%\n연속손실: {consecutive_losses}",
        force=True,
    )
    write_log(candle, big_trend, entry_strategy or strategy, "SELL", score, exit_reason)
    signal_count += 1
    last_exit_time = now_kst()
    position_open = False
    last_report_time = 0
    entry_price = 0.0
    entry_time = None
    entry_candle_time = None
    entry_big_trend = None
    entry_strategy = None
    entry_score = 0
    max_pnl = 0.0
    if consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
        strategy_enabled = False
        send_telegram(f"🚨 전략 자동 OFF\n\n사유: {consecutive_losses}연속 손실\n조치: 데이터 확인 후 재가동 필요", force=True)
    save_state()


def check_exit(candle, big_trend, strategy, score):
    global max_pnl
    if not position_open:
        return
    price = candle["close"]
    net_pnl = get_net_pnl(price)
    hold_minutes = get_hold_minutes()
    if net_pnl > max_pnl:
        max_pnl = net_pnl
        save_state()
    exit_reason = None
    if net_pnl <= STOP_LOSS:
        exit_reason = "STOP_LOSS"
    elif hold_minutes >= MIN_HOLD_MINUTES and net_pnl >= TAKE_PROFIT:
        exit_reason = "TAKE_PROFIT"
    elif (
        hold_minutes >= MIN_HOLD_MINUTES
        and net_pnl >= 0.25
        and max_pnl >= TRAIL_START
        and net_pnl <= max_pnl - TRAIL_BACK
    ):
        exit_reason = "TRAILING_STOP"
    elif big_trend == "BIG_CRASH":
        exit_reason = "BIG_CRASH_EXIT"
    if exit_reason:
        close_position(candle, big_trend, strategy, score, exit_reason)


def check_entry(candle, big_trend, strategy, score):
    global position_open, entry_price, entry_time, entry_candle_time
    global entry_big_trend, entry_strategy, entry_score, max_pnl, signal_count
    if not strategy_enabled or position_open or in_cooldown():
        return
    if strategy.startswith("NO_TRADE"):
        return
    if not in_allowed_time(candle["datetime"]):
        return
    if score < ENTRY_SCORE:
        return
    price = candle["close"]
    position_open = True
    entry_price = price
    entry_time = now_kst()
    entry_candle_time = candle["datetime"].strftime("%Y-%m-%d %H:%M:%S")
    entry_big_trend = big_trend
    entry_strategy = strategy
    entry_score = score
    max_pnl = 0.0
    save_state()
    send_telegram(
        f"🟢 BTC 매수 신호\n\n"
        f"전략: {strategy}\n장기추세: {big_trend}\n가격: {price:.2f}\n"
        f"RSI: {candle['rsi']:.2f}\n진입점수: {score}\n"
        f"익절: {TAKE_PROFIT}%\n손절: {STOP_LOSS}%\n트레일: {TRAIL_START}/{TRAIL_BACK}",
        force=True,
    )
    write_log(candle, big_trend, strategy, "BUY", score, "-")
    signal_count += 1


def send_hourly_report(candle, big_trend, strategy, score):
    price = candle["close"]
    send_telegram(
        f"📈 1시간 시스템 리포트\n\n"
        f"모드: OPT_LONG_PULLBACK_365\n장기추세: {big_trend}\n전략: {strategy}\n"
        f"가격: {price:.2f}\nRSI: {candle['rsi']:.2f}\n점수: {score}\n\n"
        f"전략활성화: {strategy_enabled}\n포지션: {position_open}\n진입가: {entry_price:.2f}\n"
        f"현재수익률: {get_net_pnl(price):.4f}%\n\n"
        f"총 거래: {total_trades}\n승률: {get_win_rate()}%\n누적손익: {cumulative_pnl:.4f}%\n연속손실: {consecutive_losses}"
    )


def run_bot():
    global last_report_time

    df_15m = calculate_indicators(
        get_klines(Client.KLINE_INTERVAL_15MINUTE, 300)
    )

    df_1h = calculate_indicators(
        get_klines(Client.KLINE_INTERVAL_1HOUR, 300)
    )

    df_4h = calculate_indicators(
        get_klines(Client.KLINE_INTERVAL_4HOUR, 300)
    )

    candle = df_15m.iloc[-2]
    h1 = df_1h.iloc[-2]
    h4 = df_4h.iloc[-2]
    if pd.isna(candle["rsi"]) or pd.isna(h1["ema200"]) or pd.isna(h4["ema200"]):
        raise RuntimeError("Indicator warmup not ready")
    big_trend = detect_big_trend(h1, h4)
    strategy = get_strategy(big_trend)
    score = calculate_score(candle, strategy)
    check_exit(candle, big_trend, strategy, score)
    check_entry(candle, big_trend, strategy, score)
    # 10분마다 로그 기록
    if time.time() - last_report_time >= 600:
        write_log(candle, big_trend, strategy, "WATCH", score, "-")
        last_report_time = time.time()
    print(f"{now_kst()} | {candle['datetime']} | {big_trend} | {strategy} | SCORE={score} | POSITION={position_open}", flush=True)
    return candle, big_trend, strategy, score


init_sheet_header()
load_state()

send_telegram(
    f"🚀 OPT_LONG_PULLBACK_365 라이브봇 시작\n\n"
    f"전략: LONG_PULLBACK only\nENTRY_SCORE: {ENTRY_SCORE}\nRSI_LIMIT: {RSI_LIMIT}\n"
    f"TP/SL: {TAKE_PROFIT}% / {STOP_LOSS}%\nTRAIL: {TRAIL_START}/{TRAIL_BACK}\n"
    f"시간필터: {USE_TIME_FILTER}\n\n복구 포지션: {position_open}\n진입가: {entry_price}\n누적손익: {cumulative_pnl:.4f}%",
    force=True,
)

while True:
    try:
        candle, big_trend, strategy, score = run_bot()
        if time.time() - last_report_time >= 3600:
            send_hourly_report(candle, big_trend, strategy, score)
            last_report_time = time.time()
        time.sleep(LOOP_SLEEP_SECONDS)
    except Exception as e:
        error_count += 1
        msg = str(e)
        send_telegram(f"❌ 오류 발생\n{msg}", force=True)
        print(f"ERROR: {msg}", flush=True)
        if "-1003" in msg:
            time.sleep(600)
        else:
            time.sleep(LOOP_SLEEP_SECONDS)
