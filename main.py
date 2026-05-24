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
INTERVAL = Client.KLINE_INTERVAL_5MINUTE
LIMIT = 120

last_signal = None
last_market = None
last_report_time = time.time()

position_open = False
entry_price = 0.0
entry_time = None

signal_count = 0
error_count = 0
market_change_count = 0

trade_history = []


def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": CHAT_ID, "text": message})


def init_sheet_header():
    values = sheet.get_all_values()
    if not values:
        sheet.append_row([
            "time",
            "symbol",
            "market",
            "signal",
            "price",
            "rsi",
            "score",
            "ema20",
            "ema50",
            "ema100",
            "position_open",
            "entry_price",
            "pnl_percent"
        ])


def save_log(data):
    row = [
        data["time"],
        data["symbol"],
        data["market"],
        data["signal"],
        data["price"],
        data["rsi"],
        data["score"],
        data["ema20"],
        data["ema50"],
        data["ema100"],
        data["position_open"],
        data["entry_price"],
        data["pnl_percent"]
    ]

    sheet.append_row(row)


def get_klines():
    candles = client.get_klines(
        symbol=SYMBOL,
        interval=INTERVAL,
        limit=LIMIT
    )

    df = pd.DataFrame(candles, columns=[
        "time", "open", "high", "low", "close", "volume",
        "close_time", "quote_asset_volume", "trades",
        "taker_buy_base", "taker_buy_quote", "ignore"
    ])

    df["close"] = df["close"].astype(float)

    return df


def calculate_indicators(df):
    close = df["close"]

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

    df["ema20"] = close.ewm(span=20, adjust=False).mean()
    df["ema50"] = close.ewm(span=50, adjust=False).mean()
    df["ema100"] = close.ewm(span=100, adjust=False).mean()

    return df


def detect_market(df):
    now = df.iloc[-1]

    price = now["close"]
    ema20 = now["ema20"]
    ema50 = now["ema50"]
    ema100 = now["ema100"]

    if price > ema20 > ema50 > ema100:
        return "BULL"

    elif price < ema20 < ema50 < ema100:
        return "BEAR"

    else:
        return "SIDE"


def calculate_score(df, market):
    now = df.iloc[-1]
    prev = df.iloc[-2]

    price = now["close"]
    rsi = now["rsi"]
    bb_lower = now["bb_lower"]
    bb_mid = now["bb_mid"]

    score = 0

    if market == "BULL":
        if rsi < 40:
            score += 40

        if price < bb_mid:
            score += 30

        if price > now["ema20"]:
            score += 30

    elif market == "SIDE":
        if prev["close"] < prev["bb_lower"]:
            score += 40

        if price > bb_lower:
            score += 30

        if rsi < 35:
            score += 30

    elif market == "BEAR":
        if rsi < 25:
            score += 50

        if price < bb_lower:
            score += 30

        if price < now["ema20"]:
            score += 20

    return score


def get_pnl_percent(price):
    if not position_open or entry_price == 0:
        return 0.0

    return round(((price - entry_price) / entry_price) * 100, 4)


def write_status_log(df, market, score):
    now = df.iloc[-1]
    price = now["close"]

    save_log({
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "symbol": SYMBOL,
        "market": market,
        "signal": "WATCH",
        "price": round(price, 2),
        "rsi": round(now["rsi"], 2),
        "score": score,
        "ema20": round(now["ema20"], 2),
        "ema50": round(now["ema50"], 2),
        "ema100": round(now["ema100"], 2),
        "position_open": position_open,
        "entry_price": round(entry_price, 2),
        "pnl_percent": get_pnl_percent(price)
    })


def send_hourly_report(df):
    now = df.iloc[-1]

    price = now["close"]
    rsi = now["rsi"]
    market = detect_market(df)
    score = calculate_score(df, market)
    pnl = get_pnl_percent(price)

    report = (
        f"📈 1시간 시스템 리포트\n\n"
        f"심볼: {SYMBOL}\n"
        f"현재 시장상태: {market}\n"
        f"현재 가격: {price:.2f}\n"
        f"현재 RSI: {rsi:.2f}\n"
        f"현재 진입점수: {score}\n\n"
        f"포지션 보유: {position_open}\n"
        f"진입가: {entry_price:.2f}\n"
        f"현재 가상수익률: {pnl:.4f}%\n\n"
        f"최근 신호 수: {signal_count}\n"
        f"시장상태 변경 수: {market_change_count}\n"
        f"오류 수: {error_count}\n"
        f"누적 BUY/SELL 기록 수: {len(trade_history)}\n\n"
        f"시스템 상태: 정상 감시 중"
    )

    send_telegram(report)


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
            f"시장상태가 너무 자주 바뀌고 있음\n"
            f"변경 수: {market_change_count}\n"
            f"조치: 횡보/혼조장 가능성, 실거래 시 진입 보수 권장"
        )

        market_change_count = 0

    if error_count >= 5:
        send_telegram(
            f"🚨 시스템 위험\n\n"
            f"오류가 반복 발생 중\n"
            f"오류 수: {error_count}\n"
            f"조치: Railway 로그 확인 필요"
        )

        error_count = 0


def check_signal(df):
    global last_signal
    global last_market
    global signal_count
    global market_change_count
    global trade_history
    global position_open
    global entry_price
    global entry_time

    now = df.iloc[-1]

    price = now["close"]
    rsi = now["rsi"]
    bb_upper = now["bb_upper"]

    market = detect_market(df)
    score = calculate_score(df, market)

    if market != last_market:
        send_telegram(
            f"📊 시장상태 변경\n\n"
            f"현재 상태: {market}\n"
            f"가격: {price:.2f}\n"
            f"RSI: {rsi:.2f}"
        )

        last_market = market
        market_change_count += 1

    if score >= 70 and not position_open:
        send_telegram(
            f"🟢 BTC 진입 관심 신호\n\n"
            f"시장상태: {market}\n"
            f"가격: {price:.2f}\n"
            f"RSI: {rsi:.2f}\n"
            f"진입점수: {score}"
        )

        position_open = True
        entry_price = price
        entry_time = time.strftime("%Y-%m-%d %H:%M:%S")

        save_log({
            "time": entry_time,
            "symbol": SYMBOL,
            "market": market,
            "signal": "BUY",
            "price": round(price, 2),
            "rsi": round(rsi, 2),
            "score": score,
            "ema20": round(now["ema20"], 2),
            "ema50": round(now["ema50"], 2),
            "ema100": round(now["ema100"], 2),
            "position_open": position_open,
            "entry_price": round(entry_price, 2),
            "pnl_percent": 0.0
        })

        trade_history.append({
            "time": entry_time,
            "signal": "BUY",
            "market": market,
            "price": round(price, 2),
            "rsi": round(rsi, 2),
            "score": score
        })

        signal_count += 1
        last_signal = "BUY"

    if position_open:
        pnl = get_pnl_percent(price)

        if rsi > 60 or price > bb_upper:
            send_telegram(
                f"🔴 BTC 청산 관심 신호\n\n"
                f"시장상태: {market}\n"
                f"진입가: {entry_price:.2f}\n"
                f"청산가: {price:.2f}\n"
                f"가상수익률: {pnl:.4f}%\n"
                f"RSI: {rsi:.2f}"
            )

            save_log({
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "symbol": SYMBOL,
                "market": market,
                "signal": "SELL",
                "price": round(price, 2),
                "rsi": round(rsi, 2),
                "score": score,
                "ema20": round(now["ema20"], 2),
                "ema50": round(now["ema50"], 2),
                "ema100": round(now["ema100"], 2),
                "position_open": False,
                "entry_price": round(entry_price, 2),
                "pnl_percent": pnl
            })

            trade_history.append({
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "signal": "SELL",
                "market": market,
                "price": round(price, 2),
                "rsi": round(rsi, 2),
                "score": score,
                "pnl_percent": pnl
            })

            signal_count += 1
            last_signal = "SELL"

            position_open = False
            entry_price = 0.0
            entry_time = None

    write_status_log(df, market, score)

    print(
        f"{market} | PRICE={price:.2f} | RSI={rsi:.2f} | SCORE={score} | POSITION={position_open}"
    )


init_sheet_header()
send_telegram("🚀 포지션 관리형 BTC 5분봉 전략봇 시작")

while True:
    try:
        df = get_klines()
        df = calculate_indicators(df)
        check_signal(df)

        detect_anomaly()

        if time.time() - last_report_time >= 3600:
            send_hourly_report(df)
            last_report_time = time.time()

        time.sleep(60)

    except Exception as e:
        error_count += 1
        send_telegram(f"❌ 오류 발생\n{str(e)}")
        time.sleep(60)
