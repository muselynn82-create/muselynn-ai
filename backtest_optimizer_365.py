import os
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from binance.client import Client

# =========================
# CONFIG - OPT_LONG_PULLBACK_365
# =========================
SYMBOL = "BTCUSDT"
START_DATE = "2025-05-26"
END_DATE = "2026-05-25"

ENTRY_SCORE = 70
RSI_LIMIT = 26
TAKE_PROFIT = 1.80
STOP_LOSS = -1.50
TRAIL_START = 1.50
TRAIL_BACK = 0.70
FEE_ROUND_TRIP = 0.20

REENTRY_COOLDOWN_MINUTES = 15
MIN_HOLD_MINUTES = 5
KST = ZoneInfo("Asia/Seoul")

SUMMARY_SHEET_NAME = "OPT365_TIME_SUMMARY"
TRADES_SHEET_NAME = "OPT365_TIME_TRADES"
RUN_LOG_SHEET_NAME = "OPT365_TIME_RUNLOG"

API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_SECRET_KEY")
GOOGLE_CLIENT_EMAIL = os.getenv("GOOGLE_CLIENT_EMAIL")
GOOGLE_PRIVATE_KEY = os.getenv("GOOGLE_PRIVATE_KEY", "").replace("\\n", "\n")
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME")

client = Client(API_KEY, API_SECRET)


def now_kst():
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")


def init_gspread():
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
    return gc.open(GOOGLE_SHEET_NAME)


def get_or_create_ws(spreadsheet, title, rows=2000, cols=40):
    try:
        return spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        return spreadsheet.add_worksheet(title=title, rows=rows, cols=cols)


def clear_and_write(ws, headers, rows):
    ws.clear()
    values = [headers] + rows
    ws.update(range_name="A1", values=values)


def append_run_log(ws, message):
    ws.append_row([now_kst(), message])


def dt_to_ms(dt):
    return int(dt.timestamp() * 1000)


def fetch_klines(symbol, interval, start_dt, end_dt):
    print(f"Downloading {symbol} {interval} data...", flush=True)
    all_rows = []
    start_ms = dt_to_ms(start_dt)
    end_ms = dt_to_ms(end_dt)

    while start_ms < end_ms:
        candles = client.get_klines(
            symbol=symbol,
            interval=interval,
            startTime=start_ms,
            endTime=end_ms,
            limit=1000,
        )
        if not candles:
            break
        all_rows.extend(candles)
        start_ms = candles[-1][0] + 1
        time.sleep(0.08)

    df = pd.DataFrame(all_rows, columns=[
        "time", "open", "high", "low", "close", "volume",
        "close_time", "quote_asset_volume", "trades",
        "taker_buy_base", "taker_buy_quote", "ignore",
    ])
    if df.empty:
        raise RuntimeError(f"No data downloaded for {interval}")

    df = df.drop_duplicates(subset=["time"]).reset_index(drop=True)
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


def in_allowed_time(candle_time, use_time_filter):
    if not use_time_filter:
        return True
    hour = candle_time.hour
    return (17 <= hour <= 23) or (0 <= hour <= 5)


def run_backtest(df_15m, df_1h, df_4h, use_time_filter):
    position_open = False
    entry_price = 0.0
    entry_time = None
    entry_score = 0
    entry_big_trend = None
    max_pnl = 0.0
    last_exit_time = None

    equity = 100.0
    peak_equity = 100.0
    max_drawdown = 0.0
    trades = []

    df_1h_times = df_1h["datetime"].tolist()
    df_4h_times = df_4h["datetime"].tolist()
    i1 = 0
    i4 = 0

    for i in range(220, len(df_15m)):
        candle = df_15m.iloc[i]
        current_time = candle["datetime"]

        # live bot uses closed candles; align higher timeframe to confirmed closes only
        while i1 + 1 < len(df_1h_times) and df_1h_times[i1 + 1] <= current_time - timedelta(hours=1):
            i1 += 1
        while i4 + 1 < len(df_4h_times) and df_4h_times[i4 + 1] <= current_time - timedelta(hours=4):
            i4 += 1

        h1 = df_1h.iloc[i1]
        h4 = df_4h.iloc[i4]

        if pd.isna(candle["rsi"]) or pd.isna(h1["ema200"]) or pd.isna(h4["ema200"]):
            continue

        big_trend = detect_big_trend(h1, h4)
        strategy = get_strategy(big_trend)
        score = calculate_score(candle, strategy)
        price = candle["close"]

        # EXIT first, same as live bot
        if position_open:
            gross_pnl = ((price - entry_price) / entry_price) * 100
            net_pnl = ((1 + gross_pnl / 100) * (1 - FEE_ROUND_TRIP / 100) - 1) * 100
            hold_minutes = (current_time - entry_time).total_seconds() / 60 if entry_time is not None else 0
            max_pnl = max(max_pnl, net_pnl)

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
                equity *= (1 + net_pnl / 100)
                peak_equity = max(peak_equity, equity)
                drawdown = ((equity - peak_equity) / peak_equity) * 100
                max_drawdown = min(max_drawdown, drawdown)

                trades.append({
                    "use_time_filter": use_time_filter,
                    "entry_time": entry_time.strftime("%Y-%m-%d %H:%M:%S"),
                    "exit_time": current_time.strftime("%Y-%m-%d %H:%M:%S"),
                    "entry_big_trend": entry_big_trend,
                    "exit_big_trend": big_trend,
                    "strategy": "LONG_PULLBACK",
                    "entry_price": round(entry_price, 2),
                    "exit_price": round(price, 2),
                    "entry_score": entry_score,
                    "gross_pnl": round(gross_pnl, 4),
                    "net_pnl": round(net_pnl, 4),
                    "max_pnl": round(max_pnl, 4),
                    "exit_reason": exit_reason,
                    "equity": round(equity, 4),
                })

                position_open = False
                entry_price = 0.0
                entry_time = None
                entry_score = 0
                entry_big_trend = None
                max_pnl = 0.0
                last_exit_time = current_time

        # ENTRY
        if not position_open:
            if strategy.startswith("NO_TRADE"):
                continue
            if not in_allowed_time(current_time, use_time_filter):
                continue
            if last_exit_time is not None:
                cooldown_minutes = (current_time - last_exit_time).total_seconds() / 60
                if cooldown_minutes < REENTRY_COOLDOWN_MINUTES:
                    continue
            if score < ENTRY_SCORE:
                continue

            position_open = True
            entry_price = price
            entry_time = current_time
            entry_score = score
            entry_big_trend = big_trend
            max_pnl = 0.0

    trades_df = pd.DataFrame(trades)
    if trades_df.empty:
        return {
            "use_time_filter": use_time_filter,
            "trades": 0,
            "win_rate": 0,
            "total_return": 0,
            "max_drawdown": 0,
            "avg_win": 0,
            "avg_loss": 0,
            "profit_factor": 0,
            "tp_count": 0,
            "sl_count": 0,
            "trail_count": 0,
            "crash_count": 0,
        }, trades_df

    wins = trades_df[trades_df["net_pnl"] > 0]
    losses = trades_df[trades_df["net_pnl"] <= 0]
    total_trades = len(trades_df)
    win_rate = len(wins) / total_trades * 100
    total_return = trades_df["equity"].iloc[-1] - 100
    avg_win = wins["net_pnl"].mean() if not wins.empty else 0
    avg_loss = losses["net_pnl"].mean() if not losses.empty else 0
    profit_factor = abs(wins["net_pnl"].sum() / losses["net_pnl"].sum()) if not losses.empty and losses["net_pnl"].sum() != 0 else 999
    exit_counts = trades_df["exit_reason"].value_counts().to_dict()

    stats = {
        "use_time_filter": use_time_filter,
        "trades": total_trades,
        "win_rate": round(win_rate, 2),
        "total_return": round(total_return, 2),
        "max_drawdown": round(max_drawdown, 2),
        "avg_win": round(avg_win, 4),
        "avg_loss": round(avg_loss, 4),
        "profit_factor": round(profit_factor, 4),
        "tp_count": int(exit_counts.get("TAKE_PROFIT", 0)),
        "sl_count": int(exit_counts.get("STOP_LOSS", 0)),
        "trail_count": int(exit_counts.get("TRAILING_STOP", 0)),
        "crash_count": int(exit_counts.get("BIG_CRASH_EXIT", 0)),
    }
    return stats, trades_df


def main():
    print("OPT Long Pullback 365 Time Compare Backtest started:", now_kst(), flush=True)
    spreadsheet = init_gspread()
    summary_ws = get_or_create_ws(spreadsheet, SUMMARY_SHEET_NAME, rows=100, cols=30)
    trades_ws = get_or_create_ws(spreadsheet, TRADES_SHEET_NAME, rows=5000, cols=40)
    log_ws = get_or_create_ws(spreadsheet, RUN_LOG_SHEET_NAME, rows=500, cols=5)
    append_run_log(log_ws, "Backtest started")

    start_dt = datetime.strptime(START_DATE, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    # Include the whole END_DATE day by adding 1 day to the exclusive end timestamp
    end_dt = (datetime.strptime(END_DATE, "%Y-%m-%d") + timedelta(days=1)).replace(tzinfo=timezone.utc)

    df_15m = calculate_indicators(fetch_klines(SYMBOL, Client.KLINE_INTERVAL_15MINUTE, start_dt, end_dt))
    df_1h = calculate_indicators(fetch_klines(SYMBOL, Client.KLINE_INTERVAL_1HOUR, start_dt, end_dt))
    df_4h = calculate_indicators(fetch_klines(SYMBOL, Client.KLINE_INTERVAL_4HOUR, start_dt, end_dt))

    summary_rows = []
    all_trades = []
    for use_time_filter in [True, False]:
        stats, trades = run_backtest(df_15m, df_1h, df_4h, use_time_filter=use_time_filter)
        stats["run_time"] = now_kst()
        summary_rows.append(stats)
        if not trades.empty:
            all_trades.append(trades)
        append_run_log(log_ws, f"Finished use_time_filter={use_time_filter}: {stats}")

    summary_df = pd.DataFrame(summary_rows)
    summary_headers = list(summary_df.columns)
    summary_values = summary_df.astype(str).values.tolist()
    clear_and_write(summary_ws, summary_headers, summary_values)

    if all_trades:
        trades_df = pd.concat(all_trades, ignore_index=True)
        trade_headers = list(trades_df.columns)
        trade_values = trades_df.astype(str).values.tolist()
    else:
        trade_headers = [
            "use_time_filter", "entry_time", "exit_time", "entry_big_trend", "exit_big_trend",
            "strategy", "entry_price", "exit_price", "entry_score", "gross_pnl", "net_pnl",
            "max_pnl", "exit_reason", "equity"
        ]
        trade_values = []
    clear_and_write(trades_ws, trade_headers, trade_values)

    append_run_log(log_ws, "Backtest finished")
    print("OPT Long Pullback 365 Time Compare Backtest finished:", now_kst(), flush=True)
    print("Saved summary to:", SUMMARY_SHEET_NAME, flush=True)
    print("Saved trades to:", TRADES_SHEET_NAME, flush=True)


if __name__ == "__main__":
    main()
