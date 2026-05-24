import os
import time
import requests
from binance.client import Client

API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_SECRET_KEY")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

client = Client(API_KEY, API_SECRET)

SYMBOL = "BTCUSDT"

last_signal = None


def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    data = {
        "chat_id": CHAT_ID,
        "text": message
    }

    requests.post(url, data=data)


def get_price():
    ticker = client.get_symbol_ticker(symbol=SYMBOL)
    return float(ticker["price"])


send_telegram("🚀 Muselynn AI 비트코인 봇 시작")

while True:
    try:
        price = get_price()

        print(f"BTC PRICE: {price}")

        if price > 110000 and last_signal != "SELL":
            send_telegram(f"🔴 BTC 매도 신호 발생\n현재가: {price}")
            last_signal = "SELL"

        elif price < 90000 and last_signal != "BUY":
            send_telegram(f"🟢 BTC 매수 신호 발생\n현재가: {price}")
            last_signal = "BUY"

        time.sleep(30)

    except Exception as e:
        send_telegram(f"❌ 오류 발생: {str(e)}")
        time.sleep(30)
