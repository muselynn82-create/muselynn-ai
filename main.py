import os
import time
import requests
import pandas as pd
import gspread

from oauth2client.service_account import ServiceAccountCredentials
from binance.client import Client

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
sheet = gc.open(GOOGLE_SHEET_NAME).sheet1

SYMBOL = "BTCUSDT"

FEE_ROUND_TRIP = 0.20
MAX_CONSECUTIVE_LOSSES = 3

last_report_time = time.time()
last_market = None
last_big_trend = None

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


def save_log(data):
    sheet.append_row([
        data["time"],
        data["symbol"],
        data["big_trend"],
        data["market"],
        data["strategy"],
        data["signal"],
        data["price"],
        data["rsi"],
        data["score"],
        data["ema20"],
        data["ema50"],
        data["ema100"],
        data["ema200"],
        data["position_open"],
        data["entry_price"],
        data["gross_pnl"],
        data["net_pnl"],
        data["total_trades"],
        data["win_rate"],
        data["cumulative_pnl"],
        data["exit_reason"],
        data["strategy_enabled"]
    ])


def get_win_rate():
    if total_trades == 0:
        return 0.0
    return round((win_trades / total_trades) * 100, 2)


def get_gross_pnl(price):
    if not position_open or entry_price == 0:
        return 0.0
    return round(((price - entry_price) / entry_price) * 100, 4)


def get_net_pnl(price):
    if not position_open or entry_price == 0:
        return 0.0
    return round(get_gross_pnl(price) - FEE_ROUND_TRIP, 4)


def get_klines(interval, limit):
    candles = client.get_klines(
        symbol=SYMBOL,
        interval=interval,
        limit=limit
    )

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

    df["bb_upper"] = df["bb_mid"] + (std * 2)
    df["bb_lower"] = df["bb_mid"] - (std * 2)
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

    return df


def detect_big_trend(df_1h, df_4h):
    h1 = df_1h.iloc[-1]
    h4 = df_4h.iloc[-1]

    h1_price = h1["close"]
    h4_price = h4["close"]

    h1_bull = h1_price > h1["ema50"] > h1["ema200"]
    h4_bull = h4_price > h4["ema50"] > h4["ema200"]

    h1_bear = h1_price < h1["ema50"] < h1["ema200"]
    h4_bear = h4_price < h4["ema50"] < h4["ema200"]

    crash = (
        h1["atr_rate"] > 0.025 or
        h4["atr_rate"] > 0.045
    )

    if crash:
        return "BIG_CRASH"

    if h1_bull and h4_bull:
        return "BIG_BULL"

    if h1_bear and h4_bear:
        return "BIG_BEAR"

    return "BIG_SIDE"


def detect_short_market(df_5m):
    now = df_5m.iloc[-1]

    price = now["close"]

    if now["atr_rate"] > 0.012 or now["bb_width"] > 0.055:
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
        return "NO_TRADE"

    if big_trend == "BIG_SIDE":
        if market == "SIDE":
            return "SIDE_RSI_BB"
        if market == "BULL":
            return "BULL_PULLBACK_LIGHT"
        return "NO_TRADE"

    if big_trend == "BIG_BEAR":
        if market == "SIDE":
            return "BEAR_SCALP"
        return "NO_TRADE_BEAR"

    return "NO_TRADE"


def calculate_score(df_5m, big_trend, market, strategy):
    now = df_5m.iloc[-1]
    prev = df_5m.iloc[-2]

    price = now["close"]
    rsi = now["rsi"]

    score = 0

    if strategy == "SIDE_RSI_BB":
        if prev["close"] < prev["bb_lower"]:
            score += 35
        if price > now["bb_lower"]:
            score += 25
        if rsi < 35:
            score += 30
        if market == "SIDE":
            score += 10

    elif strategy == "BULL_PULLBACK":
        if 38 <= rsi <= 55:
            score += 35
        if price > now["ema50"]:
            score += 25
        if price <= now["ema20"] * 1.004:
            score += 25
        if big_trend == "BIG_BULL":
            score += 15

    elif strategy == "BULL_PULLBACK_LIGHT":
        if 40 <= rsi <= 55:
            score += 30
        if price > now["ema50"]:
            score += 25
        if price <= now["ema20"] * 1.003:
            score += 20

    elif strategy == "BEAR_SCALP":
        if rsi < 22:
            score += 40
        if price < now["bb_lower"]:
            score += 35
        if market == "SIDE":
            score += 10

    return score


def get_risk_params(strategy):
    if strategy == "SIDE_RSI_BB":
        return {
            "take_profit": 0.75,
            "stop_loss": -0.65,
            "trail_start": 0.45,
            "trail_back": 0.30,
            "max_hold_minutes": 45
        }

    if strategy == "BULL_PULLBACK":
        return {
            "take_profit": 1.60,
            "stop_loss": -0.85,
            "trail_start": 0.90,
            "trail_back": 0.45,
            "max_hold_minutes": 180
        }

    if strategy == "BULL_PULLBACK_LIGHT":
        return {
            "take_profit": 1.00,
            "stop_loss": -0.70,
            "trail_start": 0.60,
            "trail_back": 0.35,
            "max_hold_minutes": 90
        }

    if strategy == "BEAR_SCALP":
        return {
            "take_profit": 0.40,
            "stop_loss": -0.45,
            "trail_start": 0.25,
            "trail_back": 0.20,
            "max_hold_minutes": 25
        }

    return {
        "take_profit": 0.0,
        "stop_loss": 0.0,
        "trail_start": 0.0,
        "trail_back": 0.0,
        "max_hold_minutes": 0
    }


def write_log(df_5m, big_trend, market, strategy, signal, score, exit_reason="-"):
    now = df_5m.iloc[-1]
    price = now["close"]

    save_log({
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
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
    global position_open
    global entry_price
    global entry_time
    global entry_market
    global entry_big_trend
    global entry_strategy
    global max_pnl
    global signal_count
    global total_trades
    global win_trades
    global loss_trades
    global consecutive_losses
    global cumulative_pnl
    global strategy_enabled

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

    position_open = False
    entry_price = 0.0
    entry_time = None
    entry_market = None
    entry_big_trend = None
    entry_strategy = None
    max_pnl = 0.0

    if consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
        strategy_enabled = False
        send_telegram(
            f"🚨 전략 자동 OFF\n\n"
            f"사유: {consecutive_losses}연속 손실\n"
            f"조치: 실거래 금지, 전략 재점검 필요"
        )


def check_exit(df_5m, big_trend, market, score):
    global max_pnl

    if not position_open:
        return

    now = df_5m.iloc[-1]
    price = now["close"]
    rsi = now["rsi"]

    gross_pnl = get_gross_pnl(price)

    if gross_pnl > max_pnl:
        max_pnl = gross_pnl

    params = get_risk_params(entry_strategy)

    if gross_pnl <= params["stop_loss"]:
        close_position(df_5m, big_trend, market, score, "STOP_LOSS")
        return

    if gross_pnl >= params["take_profit"]:
        close_position(df_5m, big_trend, market, score, "TAKE_PROFIT")
        return

    if (
        max_pnl >= params["trail_start"] and
        gross_pnl <= max_pnl - params["trail_back"]
    ):
        close_position(df_5m, big_trend, market, score, "TRAILING_STOP")
        return

    if entry_time:
        entry_ts = time.mktime(time.strptime(entry_time, "%Y-%m-%d %H:%M:%S"))
        hold_minutes = (time.time() - entry_ts) / 60

        if hold_minutes >= params["max_hold_minutes"]:
            close_position(df_5m, big_trend, market, score, "TIME_EXIT")
            return

    if big_trend == "BIG_CRASH":
        close_position(df_5m, big_trend, market, score, "BIG_CRASH_EXIT")
        return

    if entry_big_trend == "BIG_BULL" and big_trend == "BIG_BEAR":
        close_position(df_5m, big_trend, market, score, "BIG_TREND_REVERSAL")
        return

    if entry_strategy in ["SIDE_RSI_BB", "BEAR_SCALP"] and rsi > 60:
        close_position(df_5m, big_trend, market, score, "RSI_EXIT")
        return


def check_entry(df_5m, big_trend, market, strategy, score):
    global position_open
    global entry_price
    global entry_time
    global entry_market
    global entry_big_trend
    global entry_strategy
    global max_pnl
    global signal_count

    if not strategy_enabled:
        return

    if position_open:
        return

    if strategy.startswith("NO_TRADE"):
        return

    if score < 70:
        return

    now = df_5m.iloc[-1]
    price = now["close"]
    rsi = now["rsi"]

    position_open = True
    entry_price = price
    entry_time = time.strftime("%Y-%m-%d %H:%M:%S")
    entry_market = market
    entry_big_trend = big_trend
    entry_strategy = strategy
    max_pnl = 0.0

    send_telegram(
        f"🟢 BTC 진입 관심 신호\n\n"
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
    global signal_count
    global error_count
    global market_change_count

    if signal_count >= 10:
        send_telegram(
            f"⚠️ 이상감지\n\n"
            f"최근 신호가 과도하게 발생 중\n"
            f"신호 수: {signal_count}\n"
            f"조치: 진입 조건 강화 검토 필요"
        )
        signal_count = 0

    if market_change_count >= 8:
        send_telegram(
            f"⚠️ 이상감지\n\n"
            f"시장상태가 너무 자주 바뀜\n"
            f"변경 수: {market_change_count}\n"
            f"조치: 혼조장 가능성, 실거래 보수 권장"
        )
        market_change_count = 0

    if error_count >= 5:
        send_telegram(
            f"🚨 시스템 위험\n\n"
            f"오류 반복 발생\n"
            f"오류 수: {error_count}\n"
            f"조치: Railway 로그 확인 필요"
        )
        error_count = 0


def send_hourly_report(df_5m, big_trend, market, strategy, score):
    now = df_5m.iloc[-1]
    price = now["close"]

    send_telegram(
        f"📈 1시간 시스템 리포트\n\n"
        f"심볼: {SYMBOL}\n"
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
        f"연속손실: {consecutive_losses}\n"
        f"오류 수: {error_count}"
    )


def run_bot():
    global last_market
    global last_big_trend
    global market_change_count

    df_5m = calculate_indicators(get_klines(Client.KLINE_INTERVAL_5MINUTE, 220))
    df_1h = calculate_indicators(get_klines(Client.KLINE_INTERVAL_1HOUR, 220))
    df_4h = calculate_indicators(get_klines(Client.KLINE_INTERVAL_4HOUR, 220))

    big_trend = detect_big_trend(df_1h, df_4h)
    market = detect_short_market(df_5m)
    strategy = get_strategy(big_trend, market)
    score = calculate_score(df_5m, big_trend, market, strategy)

    if big_trend != last_big_trend or market != last_market:
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

    print(
        f"{big_trend} | {market} | {strategy} | "
        f"SCORE={score} | POSITION={position_open}"
    )

    return df_5m, big_trend, market, strategy, score


init_sheet_header()
send_telegram("🚀 장기추세 + 장세별 익절손절 BTC 전략봇 시작")

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
