import os
import time
import requests
import pandas as pd
import gspread

from oauth2client.service_account import ServiceAccountCredentials
from binance.client import Client

# =========================
# 환경변수
# =========================

API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_SECRET_KEY")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

GOOGLE_CLIENT_EMAIL = os.getenv("GOOGLE_CLIENT_EMAIL")
GOOGLE_PRIVATE_KEY = os.getenv("GOOGLE_PRIVATE_KEY").replace("\\n", "\n")
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME")

# =========================
# 바이낸스 연결
# =========================

client = Client(API_KEY, API_SECRET)

# =========================
# 구글시트 연결
# =========================

scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]

creds_dict = {
    "type": "service_account",
    "client_email": GOOGLE_CLIENT_EMAIL,
    "private_key": GOOGLE_PRIVATE_KEY,
    "token_uri": "https://oauth2.googleapis.com/token"
}

credentials = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
gc = gspread.authorize(credentials)

sheet = gc.open(GOOGLE_SHEET_NAME).sheet1

# =========================
# 설정
# =========================

SYMBOL = "BTCUSDT"
INTERVAL = Client.KLINE_INTERVAL_5MINUTE
LIMIT = 120

last_signal = None
last_market = None

# =========================
# 텔레그램
# =========================

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    requests.post(url, data={
        "chat_id": CHAT_ID,
        "text": message
    })

# =========================
# 시트 기록
# =========================

def save_log(data):

    row = [
        data["time"],
        data["market"],
        data["signal"],
        data["price"],
        data["rsi"],
        data["score"]
    ]

    sheet.append_row(row)

# =========================
# 캔들 가져오기
# =========================

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

# =========================
# 지표 계산
# =========================

def calculate_indicators(df):

    close = df["close"]

    # RSI
    delta = close.diff()

    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.rolling(14).mean()
    avg_loss = loss.rolling(14).mean()

    rs = avg_gain / avg_loss

    df["rsi"] = 100 - (100 / (1 + rs))

    # 볼린저밴드
    df["bb_mid"] = close.rolling(20).mean()

    std = close.rolling(20).std()

    df["bb_upper"] = df["bb_mid"] + (std * 2)
    df["bb_lower"] = df["bb_mid"] - (std * 2)

    # EMA
    df["ema20"] = close.ewm(span=20, adjust=False).mean()
    df["ema50"] = close.ewm(span=50, adjust=False).mean()
    df["ema100"] = close.ewm(span=100, adjust=False).mean()

    return df

# =========================
# 시장 상태 판단
# =========================

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

# =========================
# 전략
# =========================

def check_signal(df):

    global last_signal
    global last_market

    now = df.iloc[-1]
    prev = df.iloc[-2]

    price = now["close"]
    rsi = now["rsi"]

    bb_lower = now["bb_lower"]
    bb_upper = now["bb_upper"]
    bb_mid = now["bb_mid"]

    market = detect_market(df)

    score = 0

    # =====================
    # 시장 상태 변경 알림
    # =====================

    if market != last_market:

        send_telegram(
            f"📊 시장상태 변경\n\n"
            f"현재 상태: {market}\n"
            f"가격: {price:.2f}\n"
            f"RSI: {rsi:.2f}"
        )

        last_market = market

    # =====================
    # BULL 전략
    # =====================

    if market == "BULL":

        if rsi < 40:
            score += 40

        if price < bb_mid:
            score += 30

        if price > now["ema20"]:
            score += 30

    # =====================
    # SIDE 전략
    # =====================

    elif market == "SIDE":

        if prev["close"] < prev["bb_lower"]:
            score += 40

        if price > bb_lower:
            score += 30

        if rsi < 35:
            score += 30

    # =====================
    # BEAR 전략
    # =====================

    elif market == "BEAR":

        if rsi < 25:
            score += 50

        if price < bb_lower:
            score += 30

        if price < now["ema20"]:
            score += 20

    # =====================
    # 진입 신호
    # =====================

    if score >= 70 and last_signal != "BUY":

        message = (
            f"🟢 BTC 진입 관심 신호\n\n"
            f"시장상태: {market}\n"
            f"가격: {price:.2f}\n"
            f"RSI: {rsi:.2f}\n"
            f"진입점수: {score}"
        )

        send_telegram(message)

        save_log({
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "market": market,
            "signal": "BUY",
            "price": price,
            "rsi": round(rsi, 2),
            "score": score
        })

        last_signal = "BUY"

    # =====================
    # 청산 신호
    # =====================

    if last_signal == "BUY":

        if rsi > 60 or price > bb_upper:

            message = (
                f"🔴 BTC 청산 관심 신호\n\n"
                f"시장상태: {market}\n"
                f"가격: {price:.2f}\n"
                f"RSI: {rsi:.2f}"
            )

            send_telegram(message)

            save_log({
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "market": market,
                "signal": "SELL",
                "price": price,
                "rsi": round(rsi, 2),
                "score": score
            })

            last_signal = "SELL"

    print(
        f"{market} | PRICE={price:.2f} | RSI={rsi:.2f} | SCORE={score}"
    )

# =========================
# 시작
# =========================

send_telegram("🚀 시장상태 분기형 BTC 5분봉 전략봇 시작")

while True:

    try:

        df = get_klines()

        df = calculate_indicators(df)

        check_signal(df)

        time.sleep(60)

    except Exception as e:

        send_telegram(f"❌ 오류 발생\n{str(e)}")

        time.sleep(60)
