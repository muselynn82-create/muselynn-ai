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
            "ema100"
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
        data["ema100"]
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


def write_status_log(df, market, score):
    now = df.iloc[-1]

    save_log({
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "symbol": SYMBOL,
        "market": market,
        "signal": "WATCH",
        "price": round(now["close"], 2),
        "rsi": round(now["rsi"], 2),
        "score": score,
        "ema20": round(now["ema20"], 2),
        "ema50": round(now["ema50"], 2),
        "ema100": round(now["ema100"], 2)
    })


def check_signal(df):
    global last_signal
    global last_market

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

    if score >= 70 and last_signal != "BUY":
        send_telegram(
            f"🟢 BTC 진입 관심 신호\n\n"
            f"시장상태: {market}\n"
            f"가격: {price:.2f}\n"
            f"RSI: {rsi:.2f}\n"
            f"진입점수: {score}"
        )

        save_log({
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "symbol": SYMBOL,
            "market": market,
            "signal": "BUY",
            "price": round(price, 2),
            "rsi": round(rsi, 2),
            "score": score,
            "ema20": round(now["ema20"], 2),
            "ema50": round(now["ema50"], 2),
            "ema100": round(now["ema100"], 2)
        })

        last_signal = "BUY"

    if last_signal == "BUY":
        if rsi > 60 or price > bb_upper:
            send_telegram(
                f"🔴 BTC 청산 관심 신호\n\n"
                f"시장상태: {market}\n"
                f"가격: {price:.2f}\n"
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
                "ema100": round(now["ema100"], 2)
            })

            last_signal = "SELL"

    write_status_log(df, market, score)

    print(
        f"{market} | PRICE={price:.2f} | RSI={rsi:.2f} | SCORE={score}"
    )


init_sheet_header()
send_telegram("🚀 Google Sheets 기록형 BTC 5분봉 전략봇 시작")

while True:
    try:
        df = get_klines()
        df = calculate_indicators(df)
        check_signal(df)
        time.sleep(60)

    except Exception as e:
        send_telegram(f"❌ 오류 발생\n{str(e)}")
        time.sleep(60)
