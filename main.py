import os
import time
import requests
import pandas as pd
import gspread

from oauth2client.service_account import ServiceAccountCredentials
from binance.client import Client
from datetime import datetime
from zoneinfo import ZoneInfo


def now_kst():
    return datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M:%S")


API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_SECRET_KEY")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

GOOGLE_CLIENT_EMAIL = os.getenv("GOOGLE_CLIENT_EMAIL")
GOOGLE_PRIVATE_KEY = os.getenv("GOOGLE_PRIVATE_KEY").replace("\\n", "\n")
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME")

client = Client(API_KEY, API_SECRET)

scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
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
    "client_x509_cert_url": f"https://www.googleapis.com/robot/v1/metadata/x509/{GOOGLE_CLIENT_EMAIL}"
}

credentials = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
gc = gspread.authorize(credentials)

spreadsheet = gc.open(GOOGLE_SHEET_NAME)
sheet = spreadsheet.sheet1

try:
    state_sheet = spreadsheet.worksheet("STATE")
except gspread.WorksheetNotFound:
    state_sheet = spreadsheet.add_worksheet(title="STATE", rows=30, cols=2)

SYMBOL = "BTCUSDT"

ENTRY_SCORE = 60
FEE_ROUND_TRIP = 0.20

REENTRY_COOLDOWN_MINUTES = 15
MIN_HOLD_MINUTES = 5
MAX_CONSECUTIVE_LOSSES = 5

last_report_time = time.time()
last_market = None
last_big_trend = None
last_exit_time = None

position_open = False
entry_price = 0.0
entry_time = None
entry_market = None
entry_big_trend = None
entry_strategy = None
max_pnl = 0.0

strategy_enabled = True

signal_count = 0
error_count = 0
market_change_count = 0

total_trades = 0
win_trades = 0
loss_trades = 0
consecutive_losses = 0
cumulative_pnl = 0.0


def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": CHAT_ID, "text": message})


HEADERS = [
    "time", "symbol", "big_trend", "market", "strategy", "signal",
    "price", "rsi", "score", "ema20", "ema50", "ema100", "ema200",
    "position_open", "entry_price", "gross_pnl", "net_pnl",
    "total_trades", "win_rate", "cumulative_pnl",
    "exit_reason", "strategy_enabled"
]


def init_sheet_header():
    sheet.update("A1:V1", [HEADERS])


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
    global position_open, entry_price, entry_time, entry_market
    global entry_big_trend, entry_strategy, max_pnl, last_exit_time
    global strategy_enabled, total_trades, win_trades, loss_trades
    global consecutive_losses, cumulative_pnl

    rows = state_sheet.get_all_records()

    if not rows:
        save_state()
        return

    state = {str(row["key"]): row["value"] for row in rows}

    position_open = safe_bool(state.get("position_open"), False)
    entry_price = safe_float(state.get("entry_price"), 0.0)
    entry_time = state.get("entry_time") or None
    entry_market = state.get("entry_market") or None
    entry_big_trend = state.get("entry_big_trend") or None
    entry_strategy = state.get("entry_strategy") or None
    max_pnl = safe_float(state.get("max_pnl"), 0.0)
    last_exit_time = state.get("last_exit_time") or None

    strategy_enabled = safe_bool(state.get("strategy_enabled"), True)
    total_trades = safe_int(state.get("total_trades"), 0)
    win_trades = safe_int(state.get("win_trades"), 0)
    loss_trades = safe_int(state.get("loss_trades"), 0)
    consecutive_losses = safe_int(state.get("consecutive_losses"), 0)
    cumulative_pnl = safe_float(state.get("cumulative_pnl"), 0.0)


def save_state():
    state_values = [
        ["position_open", str(position_open)],
        ["entry_price", str(entry_price)],
        ["entry_time", entry_time or ""],
        ["entry_market", entry_market or ""],
        ["entry_big_trend", entry_big_trend or ""],
        ["entry_strategy", entry_strategy or ""],
        ["max_pnl", str(max_pnl)],
        ["last_exit_time", last_exit_time or ""],
        ["strategy_enabled", str(strategy_enabled)],
        ["total_trades", str(total_trades)],
        ["win_trades", str(win_trades)],
        ["loss_trades", str(loss_trades)],
        ["consecutive_losses", str(consecutive_losses)],
        ["cumulative_pnl", str(cumulative_pnl)]
    ]

    state_sheet.update("A1:B15", [["key", "value"]] + state_values)


def save_log(data):
    sheet.append_row([
        data["time"], data["symbol"], data["big_trend"], data["market"],
        data["strategy"], data["signal"], data["price"], data["rsi"],
        data["score"], data["ema20"], data["ema50"], data["ema100"],
        data["ema200"], data["position_open"], data["entry_price"],
        data["gross_pnl"], data["net_pnl"], data["total_trades"],
        data["win_rate"], data["cumulative_pnl"], data["exit_reason"],
        data["strategy_enabled"]
    ])


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

    minutes = (now_ts - exit_ts) / 60

    return minutes < REENTRY_COOLDOWN_MINUTES


def get_gross_pnl(price):
    if not position_open or entry_price == 0:
        return 0.0
    return round(((price - entry_price) / entry_price) * 100, 4)


def get_net_pnl(price):
    if not position_open or entry_price == 0:
        return 0.0
    return round(get_gross_pnl(price) - FEE_ROUND_TRIP, 4)


def get_klines(interval, limit):
    candles = client.get_klines(symbol=SYMBOL, interval=interval, limit=limit)

    df = pd.DataFrame(candles, columns=[
        "time", "open", "high", "low", "close", "volume",
        "close_time", "quote_asset_volume", "trades",
        "taker_buy_base", "taker_buy_quote", "ignore"
    ])

    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)

    return df


def calculate_indicators(df):
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
    df["bb_width"] = (df["bb_upper"] - df["bb_lower"]) / df["bb_mid"]

    df["ema20"] = close.ewm(span=20, adjust=False).mean()
    df["ema50"] = close.ewm(span=50, adjust=False).mean()
    df["ema100"] = close.ewm(span=100, adjust=False).mean()
    df["ema200"] = close.ewm(span=200, adjust=False).mean()

    df["prev_close"] = close.shift(1)
    df["tr1"] = high - low
    df["tr2"] = (high - df["prev_close"]).abs()
    df["tr3"] = (low - df["prev_close"]).abs()
    df["tr"] = df[["tr1", "tr2", "tr3"]].max(axis=1)
    df["atr"] = df["tr"].rolling(14).mean()
    df["atr_rate"] = df["atr"] / close

    df["volume_ma"] = df["volume"].rolling(20).mean()
    df["volume_ratio"] = df["volume"] / df["volume_ma"]

    return df


def detect_big_trend(df_1h, df_4h):
    h1 = df_1h.iloc[-1]
    h4 = df_4h.iloc[-1]

    h1_bull = h1["close"] > h1["ema50"] > h1["ema200"]
    h4_bull = h4["close"] > h4["ema50"] > h4["ema200"]

    h1_bear = h1["close"] < h1["ema50"] < h1["ema200"]
    h4_bear = h4["close"] < h4["ema50"] < h4["ema200"]

    if h1["atr_rate"] > 0.03 or h4["atr_rate"] > 0.055:
        return "BIG_CRASH"

    if h1_bull and h4_bull:
        return "BIG_BULL"

    if h1_bear and h4_bear:
        return "BIG_BEAR"

    return "BIG_SIDE"


def detect_short_market(df_5m):
    now = df_5m.iloc[-1]
    price = now["close"]

    if now["atr_rate"] > 0.014 or now["bb_width"] > 0.065:
        return "VOLATILE"

    if price > now["ema20"] > now["ema50"] > now["ema100"]:
        return "BULL"

    if price < now["ema20"] < now["ema50"] < now["ema100"]:
        return "BEAR"

    return "SIDE"


def get_strategy(big_trend, market):
    if big_trend == "BIG_CRASH":
        return "NO_TRADE_CRASH"

    if big_trend == "BIG_BULL":
        if market in ["BULL", "SIDE"]:
            return "BULL_PULLBACK"
        if market == "BEAR":
            return "BULL_DEEP_PULLBACK"

    if big_trend == "BIG_SIDE":
        if market == "SIDE":
            return "SIDE_RSI_BB"
        if market == "BULL":
            return "BULL_PULLBACK_LIGHT"
        if market == "BEAR":
            return "SIDE_DEEP_REBOUND"

    if big_trend == "BIG_BEAR":
        if market in ["SIDE", "BEAR"]:
            return "BEAR_SCALP"

    return "NO_TRADE"


def calculate_score(df_5m, big_trend, market, strategy):
    now = df_5m.iloc[-1]
    prev = df_5m.iloc[-2]

    price = now["close"]
    rsi = now["rsi"]
    volume_ratio = now["volume_ratio"]

    score = 0

    if strategy == "SIDE_RSI_BB":
        if rsi < 38:
            score += 25
        if price <= now["bb_lower"] * 1.004:
            score += 25
        if price > now["bb_lower"]:
            score += 20
        if market == "SIDE":
            score += 15
        if volume_ratio >= 0.9:
            score += 15

    elif strategy == "SIDE_DEEP_REBOUND":
        if rsi < 28:
            score += 35
        if price <= now["bb_lower"] * 1.003:
            score += 30
        if volume_ratio >= 0.9:
            score += 15

    elif strategy == "BULL_PULLBACK":
        if 35 <= rsi <= 55:
            score += 35
        if price > now["ema50"]:
            score += 25
        if price <= now["ema20"] * 1.004:
            score += 25
        if big_trend == "BIG_BULL":
            score += 15

    elif strategy == "BULL_PULLBACK_LIGHT":
        if 38 <= rsi <= 55:
            score += 30
        if price > now["ema50"]:
            score += 25
        if price <= now["ema20"] * 1.004:
            score += 20
        if volume_ratio >= 0.9:
            score += 10

    elif strategy == "BULL_DEEP_PULLBACK":
        if rsi < 30:
            score += 40
        if price <= now["bb_lower"] * 1.004:
            score += 30
        if price > now["ema100"]:
            score += 20

    elif strategy == "BEAR_SCALP":
        if rsi < 30:
            score += 35
        if price <= now["bb_lower"] * 1.006:
            score += 30
        if volume_ratio >= 0.9:
            score += 20
        if prev["close"] < prev["bb_lower"] and price > now["bb_lower"]:
            score += 20

    return score


def get_risk_params(strategy):
    if strategy == "SIDE_RSI_BB":
        return {"take_profit": 0.75, "stop_loss": -0.45, "trail_start": 0.50, "trail_back": 0.25, "max_hold_minutes": 45}

    if strategy == "SIDE_DEEP_REBOUND":
        return {"take_profit": 0.60, "stop_loss": -0.40, "trail_start": 0.40, "trail_back": 0.22, "max_hold_minutes": 35}

    if strategy == "BULL_PULLBACK":
        return {"take_profit": 1.30, "stop_loss": -0.70, "trail_start": 0.80, "trail_back": 0.35, "max_hold_minutes": 150}

    if strategy == "BULL_PULLBACK_LIGHT":
        return {"take_profit": 0.90, "stop_loss": -0.55, "trail_start": 0.60, "trail_back": 0.30, "max_hold_minutes": 90}

    if strategy == "BULL_DEEP_PULLBACK":
        return {"take_profit": 0.85, "stop_loss": -0.55, "trail_start": 0.55, "trail_back": 0.28, "max_hold_minutes": 75}

    if strategy == "BEAR_SCALP":
        return {"take_profit": 0.50, "stop_loss": -0.35, "trail_start": 0.35, "trail_back": 0.18, "max_hold_minutes": 25}

    return {"take_profit": 0, "stop_loss": 0, "trail_start": 0, "trail_back": 0, "max_hold_minutes": 0}


def write_log(df_5m, big_trend, market, strategy, signal, score, exit_reason="-"):
    now = df_5m.iloc[-1]
    price = now["close"]

    save_log({
        "time": now_kst(),
        "symbol": SYMBOL,
        "big_trend": big_trend,
        "market": market,
        "strategy": strategy,
        "signal": signal,
        "price": round(price, 2),
        "rsi": round(now["rsi"], 2),
        "score": score,
        "ema20": round(now["ema20"], 2),
        "ema50": round(now["ema50"], 2),
        "ema100": round(now["ema100"], 2),
        "ema200": round(now["ema200"], 2),
        "position_open": position_open,
        "entry_price": round(entry_price, 2),
        "gross_pnl": get_gross_pnl(price),
        "net_pnl": get_net_pnl(price),
        "total_trades": total_trades,
        "win_rate": get_win_rate(),
        "cumulative_pnl": round(cumulative_pnl, 4),
        "exit_reason": exit_reason,
        "strategy_enabled": strategy_enabled
    })


def close_position(df_5m, big_trend, market, score, exit_reason):
    global position_open, entry_price, entry_time, entry_market
    global entry_big_trend, entry_strategy, max_pnl
    global signal_count, total_trades, win_trades, loss_trades
    global consecutive_losses, cumulative_pnl, strategy_enabled
    global last_exit_time

    now = df_5m.iloc[-1]
    price = now["close"]
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
        f"🔴 BTC 청산 신호\n\n"
        f"사유: {exit_reason}\n"
        f"장기추세: {entry_big_trend}\n"
        f"단기장세: {entry_market}\n"
        f"전략: {entry_strategy}\n"
        f"진입가: {entry_price:.2f}\n"
        f"청산가: {price:.2f}\n"
        f"총수익률: {get_gross_pnl(price):.4f}%\n"
        f"수수료반영: {net_pnl:.4f}%\n"
        f"누적손익: {cumulative_pnl:.4f}%\n"
        f"승률: {get_win_rate()}%\n"
        f"연속손실: {consecutive_losses}"
    )

    write_log(df_5m, big_trend, market, entry_strategy, "SELL", score, exit_reason)

    signal_count += 1
    last_exit_time = now_kst()

    position_open = False
    entry_price = 0.0
    entry_time = None
    entry_market = None
    entry_big_trend = None
    entry_strategy = None
    max_pnl = 0.0

    save_state()

    if consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
        strategy_enabled = False
        save_state()
        send_telegram(
            f"🚨 전략 자동 OFF\n\n"
            f"사유: {consecutive_losses}연속 손실\n"
            f"조치: 데이터 확인 후 재가동 필요"
        )


def check_exit(df_5m, big_trend, market, score):
    global max_pnl

    if not position_open:
        return

    now = df_5m.iloc[-1]
    price = now["close"]
    gross_pnl = get_gross_pnl(price)
    hold_minutes = get_hold_minutes()

    if gross_pnl > max_pnl:
        max_pnl = gross_pnl
        save_state()

    params = get_risk_params(entry_strategy)

    if gross_pnl <= params["stop_loss"]:
        close_position(df_5m, big_trend, market, score, "STOP_LOSS")
        return

    if hold_minutes < MIN_HOLD_MINUTES:
        return

    if gross_pnl >= params["take_profit"]:
        close_position(df_5m, big_trend, market, score, "TAKE_PROFIT")
        return

    if max_pnl >= params["trail_start"] and gross_pnl <= max_pnl - params["trail_back"]:
        close_position(df_5m, big_trend, market, score, "TRAILING_STOP")
        return

    if hold_minutes >= params["max_hold_minutes"]:
        close_position(df_5m, big_trend, market, score, "TIME_EXIT")
        return

    if big_trend == "BIG_CRASH":
        close_position(df_5m, big_trend, market, score, "BIG_CRASH_EXIT")
        return


def check_entry(df_5m, big_trend, market, strategy, score):
    global position_open, entry_price, entry_time, entry_market
    global entry_big_trend, entry_strategy, max_pnl, signal_count

    if not strategy_enabled:
        return

    if position_open:
        return

    if in_cooldown():
        return

    if strategy.startswith("NO_TRADE"):
        return

    if score < ENTRY_SCORE:
        return

    now = df_5m.iloc[-1]
    price = now["close"]
    rsi = now["rsi"]

    position_open = True
    entry_price = price
    entry_time = now_kst()
    entry_market = market
    entry_big_trend = big_trend
    entry_strategy = strategy
    max_pnl = 0.0

    save_state()

    send_telegram(
        f"🟢 BTC 진입 관심 신호\n\n"
        f"모드: FILTERED_DATA_COLLECTION\n"
        f"장기추세: {big_trend}\n"
        f"단기장세: {market}\n"
        f"전략: {strategy}\n"
        f"가격: {price:.2f}\n"
        f"RSI: {rsi:.2f}\n"
        f"진입점수: {score}"
    )

    write_log(df_5m, big_trend, market, strategy, "BUY", score, "-")
    signal_count += 1


def detect_anomaly():
    global signal_count, error_count, market_change_count

    if signal_count >= 15:
        send_telegram(f"⚠️ 이상감지\n\n최근 신호 과다\n신호 수: {signal_count}")
        signal_count = 0

    if market_change_count >= 10:
        send_telegram(f"⚠️ 이상감지\n\n시장상태 변경 과다\n변경 수: {market_change_count}")
        market_change_count = 0

    if error_count >= 5:
        send_telegram(f"🚨 시스템 위험\n\n오류 반복 발생\n오류 수: {error_count}")
        error_count = 0


def send_hourly_report(df_5m, big_trend, market, strategy, score):
    now = df_5m.iloc[-1]
    price = now["close"]

    send_telegram(
        f"📈 1시간 시스템 리포트\n\n"
        f"모드: FILTERED_DATA_COLLECTION\n"
        f"장기추세: {big_trend}\n"
        f"단기장세: {market}\n"
        f"전략: {strategy}\n"
        f"가격: {price:.2f}\n"
        f"RSI: {now['rsi']:.2f}\n"
        f"점수: {score}\n\n"
        f"전략활성화: {strategy_enabled}\n"
        f"포지션: {position_open}\n"
        f"진입가: {entry_price:.2f}\n"
        f"현재수익률: {get_net_pnl(price):.4f}%\n\n"
        f"총 거래: {total_trades}\n"
        f"승률: {get_win_rate()}%\n"
        f"누적손익: {cumulative_pnl:.4f}%\n"
        f"연속손실: {consecutive_losses}"
    )


def run_bot():
    global last_market, last_big_trend, market_change_count

    df_5m = calculate_indicators(get_klines(Client.KLINE_INTERVAL_5MINUTE, 220))
    df_1h = calculate_indicators(get_klines(Client.KLINE_INTERVAL_1HOUR, 220))
    df_4h = calculate_indicators(get_klines(Client.KLINE_INTERVAL_4HOUR, 220))

    big_trend = detect_big_trend(df_1h, df_4h)
    market = detect_short_market(df_5m)
    strategy = get_strategy(big_trend, market)
    score = calculate_score(df_5m, big_trend, market, strategy)

    if (big_trend != last_big_trend or market != last_market) and score >= 50:
        send_telegram(
            f"📊 시장상태 변경\n\n"
            f"장기추세: {big_trend}\n"
            f"단기장세: {market}\n"
            f"전략: {strategy}\n"
            f"점수: {score}"
        )
        last_big_trend = big_trend
        last_market = market
        market_change_count += 1

    check_exit(df_5m, big_trend, market, score)
    check_entry(df_5m, big_trend, market, strategy, score)
    write_log(df_5m, big_trend, market, strategy, "WATCH", score, "-")

    print(f"{big_trend} | {market} | {strategy} | SCORE={score} | POSITION={position_open}")

    return df_5m, big_trend, market, strategy, score


init_sheet_header()
load_state()

send_telegram(
    f"🚀 FILTERED_DATA_COLLECTION 모드 BTC 전략봇 시작\n\n"
    f"복구 포지션: {position_open}\n"
    f"진입가: {entry_price}\n"
    f"전략: {entry_strategy}\n"
    f"누적손익: {cumulative_pnl:.4f}%"
)

while True:
    try:
        df_5m, big_trend, market, strategy, score = run_bot()

        detect_anomaly()

        if time.time() - last_report_time >= 3600:
            send_hourly_report(df_5m, big_trend, market, strategy, score)
            last_report_time = time.time()

        time.sleep(60)

    except Exception as e:
        error_count += 1
        send_telegram(f"❌ 오류 발생\n{str(e)}")
        time.sleep(60)
