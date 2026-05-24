import os
import time
import csv
import requests
import pandas as pd
from datetime import datetime
from binance.client import Client

# =========================
# 환경 변수
# =========================

API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_SECRET_KEY")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

client = Client(API_KEY, API_SECRET)

# =========================
# 기본 설정
# =========================

SYMBOL = "BTCUSDT"
INTERVAL = Client.KLINE_INTERVAL_5MINUTE
LIMIT = 120

last_signal = None

# =========================
# 텔레그램 전송
# =========================

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    data = {
        "chat_id": CHAT_ID,
        "text": message
    }

    requests.post(url, data=data)

# =========================
# CSV 저장
# =========================

def save_signal(signal, price, rsi, trend):
    file_name = "signals.csv"

    row = [
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        signal,
        round(price, 2),
        round(rsi, 2),
        trend
    ]

    with open(file_name, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(row)

# =========================
# 캔들 데이터 가져오기
# =========================

def get_klines():
    candles = client.get_klines(
        symbol=SYMBOL,
        interval=INTERVAL,
        limit=LIMIT
    )

    df = pd.DataFrame(candles, columns=[
        "time",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "close_time",
        "quote_asset_volume",
        "trades",
        "taker_buy_base",
        "taker_buy_quote",
        "ignore"
    ])

    df["close"] = df["close"].astype(float)
    df["volume"] = df["volume"].astype(float)

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

    # 볼린저 밴드
    df["bb_mid"] = close.rolling(20).mean()
    df["bb_std"] = close.rolling(20).std()

    df["bb_upper"] = df["bb_mid"] + (df["bb_std"] * 2)
    df["bb_lower"] = df["bb_mid"] - (df["bb_std"] * 2)

    # EMA
    df["ema20"] = close.ewm(span=20, adjust=False).mean()
    df["ema200"] = close.ewm(span=100, adjust=False).mean()

    return df

# =========================
# 신호 판단
# =========================

def check_signal(df):

    global last_signal

    now = df.iloc[-1]
    prev = df.iloc[-2]

    price = now["close"]
    rsi = now["rsi"]

    bb_lower = now["bb_lower"]
    bb_mid = now["bb_mid"]

    ema20 = now["ema20"]
    ema200 = now["ema200"]

    # =========================
    # 장세 판단
    # =========================

    trend = "횡보"

    if price > ema20 > ema200:
        trend = "상승"

    elif price < ema20 < ema200:
        trend = "하락"

    # =========================
    # 매수 조건
    # =========================

    buy_condition = (

        prev["close"] < prev["bb_lower"] and
        price > bb_lower and
        rsi < 35 and
        trend != "하락"

    )

    # =========================
    # 매도 조건
    # =========================

    sell_condition = (

        price >= bb_mid and
        rsi > 50

    )

    # =========================
    # BUY SIGNAL
    # =========================

    if buy_condition and last_signal != "BUY":

        msg = (
            f"🟢 BTC 5분봉 매수 관심 신호\n\n"
            f"가격: {price:.2f}\n"
            f"RSI: {rsi:.2f}\n"
            f"장세: {trend}\n"
            f"조건: 볼린저 하단 이탈 후 복귀"
        )

        send_telegram(msg)

        save_signal(
            "BUY",
            price,
            rsi,
            trend
        )

        last_signal = "BUY"

    # =========================
    # SELL SIGNAL
    # =========================

    elif sell_condition and last_signal == "BUY":

        msg = (
            f"🔴 BTC 5분봉 익절/청산 관심 신호\n\n"
            f"가격: {price:.2f}\n"
            f"RSI: {rsi:.2f}\n"
            f"장세: {trend}\n"
            f"조건: 볼밴 중심선 회복"
        )

        send_telegram(msg)

        save_signal(
            "SELL",
            price,
            rsi,
            trend
        )

        last_signal = "SELL"

    # =========================
    # 로그 출력
    # =========================

    print(
        f"PRICE={price:.2f} | "
        f"RSI={rsi:.2f} | "
        f"TREND={trend}"
    )

# =========================
# 시작 메시지
# =========================

send_telegram(
    "🚀 RSI + 볼린저밴드 5분봉 감시봇 시작"
)

# =========================
# 메인 루프
# =========================

while True:

    try:

        df = get_klines()

        df = calculate_indicators(df)

        check_signal(df)

        time.sleep(60)

    except Exception as e:

        send_telegram(
            f"❌ 봇 오류 발생\n{str(e)}"
        )

        time.sleep(60)
