import os
import time
import csv
import requests
import pandas as pd
from datetime import datetime
from binance.client import Client

API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_SECRET_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

client = Client(API_KEY, API_SECRET)

SYMBOL = "BTCUSDT"
INTERVAL = Client.KLINE_INTERVAL_5MINUTE
LIMIT = 220

last_signal = None
last_market_state = None


def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": CHAT_ID, "text": message})


def save_signal(signal, price, rsi, market_state, strategy):
    file_name = "signals.csv"

    row = [
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        SYMBOL,
        signal,
        round(price, 2),
        round(rsi, 2),
        market_state,
        strategy,
    ]

    with open(file_name, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(row)


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

    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)

    return df


def calculate_indicators(df):
    close = df["close"]
    high = df["high"]
    low = df["low"]

    # RSI
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.rolling(14).mean()
    avg_loss = loss.rolling(14).mean()
    rs = avg_gain / avg_loss

    df["rsi"] = 100 - (100 / (1 + rs))

    # Bollinger Band
    df["bb_mid"] = close.rolling(20).mean()
    df["bb_std"] = close.rolling(20).std()
    df["bb_upper"] = df["bb_mid"] + df["bb_std"] * 2
    df["bb_lower"] = df["bb_mid"] - df["bb_std"] * 2
    df["bb_width"] = (df["bb_upper"] - df["bb_lower"]) / df["bb_mid"]

    # EMA
    df["ema20"] = close.ewm(span=20, adjust=False).mean()
    df["ema50"] = close.ewm(span=50, adjust=False).mean()
    df["ema200"] = close.ewm(span=200, adjust=False).mean()

    # ATR
    df["prev_close"] = close.shift(1)
    df["tr1"] = high - low
    df["tr2"] = (high - df["prev_close"]).abs()
    df["tr3"] = (low - df["prev_close"]).abs()
    df["tr"] = df[["tr1", "tr2", "tr3"]].max(axis=1)
    df["atr"] = df["tr"].rolling(14).mean()
    df["atr_rate"] = df["atr"] / close

    # Volume
    df["volume_ma"] = df["volume"].rolling(20).mean()
    df["volume_ratio"] = df["volume"] / df["volume_ma"]

    return df


def detect_market_state(df):
    now = df.iloc[-1]

    price = now["close"]
    ema20 = now["ema20"]
    ema50 = now["ema50"]
    ema200 = now["ema200"]
    atr_rate = now["atr_rate"]
    bb_width = now["bb_width"]

    if atr_rate > 0.012 or bb_width > 0.055:
        return "VOLATILE"

    if price > ema20 > ema50 > ema200:
        return "BULL"

    if price < ema20 < ema50 < ema200:
        return "BEAR"

    return "SIDEWAYS"


def check_signal(df):
    global last_signal, last_market_state

    now = df.iloc[-1]
    prev = df.iloc[-2]

    price = now["close"]
    rsi = now["rsi"]
    bb_lower = now["bb_lower"]
    bb_mid = now["bb_mid"]
    ema20 = now["ema20"]
    ema50 = now["ema50"]
    volume_ratio = now["volume_ratio"]

    market_state = detect_market_state(df)

    if market_state != last_market_state:
        send_telegram(
            f"📊 시장상태 변경\n\n"
            f"현재 상태: {market_state}\n"
            f"가격: {price:.2f}\n"
            f"RSI: {rsi:.2f}"
        )
        last_market_state = market_state

    signal = None
    strategy = None

    # =========================
    # 횡보장 전략
    # RSI + 볼린저밴드 반등
    # =========================
    if market_state == "SIDEWAYS":
        strategy = "RSI_BB_REVERSION"

        if (
            prev["close"] < prev["bb_lower"] and
            price > bb_lower and
            rsi < 35 and
            volume_ratio > 0.7
        ):
            signal = "BUY"

        elif last_signal == "BUY" and price >= bb_mid and rsi > 50:
            signal = "SELL"

    # =========================
    # 상승장 전략
    # EMA 눌림목
    # =========================
    elif market_state == "BULL":
        strategy = "BULL_PULLBACK"

        if (
            price > ema50 and
            price <= ema20 * 1.003 and
            40 <= rsi <= 55 and
            volume_ratio > 0.7
        ):
            signal = "BUY"

        elif last_signal == "BUY" and (rsi > 68 or price < ema20 * 0.995):
            signal = "SELL"

    # =========================
    # 하락장 전략
    # 현물 기준 매수 금지
    # =========================
    elif market_state == "BEAR":
        strategy = "NO_TRADE_BEAR"
        signal = None

    # =========================
    # 고변동성 전략
    # 거래 중지
    # =========================
    elif market_state == "VOLATILE":
        strategy = "NO_TRADE_VOLATILE"
        signal = None

    if signal == "BUY" and last_signal != "BUY":
        msg = (
            f"🟢 BTC 매수 관심 신호\n\n"
            f"시장상태: {market_state}\n"
            f"전략: {strategy}\n"
            f"가격: {price:.2f}\n"
            f"RSI: {rsi:.2f}\n"
            f"거래량비율: {volume_ratio:.2f}"
        )

        send_telegram(msg)
        save_signal("BUY", price, rsi, market_state, strategy)
        last_signal = "BUY"

    elif signal == "SELL" and last_signal == "BUY":
        msg = (
            f"🔴 BTC 청산 관심 신호\n\n"
            f"시장상태: {market_state}\n"
            f"전략: {strategy}\n"
            f"가격: {price:.2f}\n"
            f"RSI: {rsi:.2f}"
        )

        send_telegram(msg)
        save_signal("SELL", price, rsi, market_state, strategy)
        last_signal = "SELL"

    print(
        f"PRICE={price:.2f} | "
        f"RSI={rsi:.2f} | "
        f"STATE={market_state} | "
        f"STRATEGY={strategy} | "
        f"VOL={volume_ratio:.2f}"
    )


send_telegram("🚀 시장상태 분기형 BTC 5분봉 전략봇 시작")

while True:
    try:
        df = get_klines()
        df = calculate_indicators(df)
        check_signal(df)
        time.sleep(60)

    except Exception as e:
        send_telegram(f"❌ 봇 오류 발생\n{str(e)}")
        time.sleep(60)
