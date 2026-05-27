import os
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from binance.client import Client


# =========================
# CONFIG
# =========================

API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_SECRET_KEY")
client = Client(API_KEY, API_SECRET)

SYMBOL = "BTCUSDT"

START_DATE = "2025-05-26"
END_DATE = "2026-05-25"

FEE_ROUND_TRIP = 0.20
KST = ZoneInfo("Asia/Seoul")

ENTRY_SCORE = 70
REENTRY_COOLDOWN_MINUTES = 15
MIN_HOLD_MINUTES = 5

USE_US_TIME_FILTER = True

SUMMARY_SHEET_NAME = "LIVE365_SUMMARY"
TRADES_SHEET_NAME = "LIVE365_TRADES"
RUN_LOG_SHEET_NAME = "LIVE365_RUNLOG"

GOOGLE_CLIENT_EMAIL = os.getenv("GOOGLE_CLIENT_EMAIL")
GOOGLE_PRIVATE_KEY = os.getenv("GOOGLE_PRIVATE_KEY").replace("\\n", "\n")
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME")


# =========================
# GOOGLE SHEETS
# =========================

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


# =========================
# DATA
# =========================

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
    df["bb_width"] = (df["bb_upper"] - df["bb_lower"]) / df["bb_mid"]

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


# =========================
# LIVE BOT STRATEGY LOGIC
# =========================

def detect_big_trend(h1, h4):
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


def detect_short_market(now):
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


def calculate_score(df_5m, i, big_trend, market, strategy):
    now = df_5m.iloc[i]
    prev = df_5m.iloc[i - 1]

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
        if (
            rsi < 26
            and now["low"] <= now["bb_lower"]
            and now["close"] > now["open"]
        ):
            score += 70

        if price > now["ema100"]:
            score += 15

        if now["ema20"] > now["ema50"]:
            score += 15

        if now["volume"] > now["volume_ma"] * 2.0:
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
        return {"take_profit": 0.75, "stop_loss": -0.45, "trail_start": 0.80, "trail_back": 0.40}
    if strategy == "SIDE_DEEP_REBOUND":
        return {"take_profit": 0.60, "stop_loss": -0.40, "trail_start": 0.40, "trail_back": 0.22}
    if strategy == "BULL_PULLBACK":
        return {"take_profit": 1.30, "stop_loss": -0.70, "trail_start": 0.80, "trail_back": 0.35}
    if strategy == "BULL_PULLBACK_LIGHT":
        return {"take_profit": 0.90, "stop_loss": -0.55, "trail_start": 0.60, "trail_back": 0.30}
    if strategy == "BULL_DEEP_PULLBACK":
        return {"take_profit": 1.80, "stop_loss": -1.00, "trail_start": 1.50, "trail_back": 0.70}
    if strategy == "BEAR_SCALP":
        return {"take_profit": 0.50, "stop_loss": -0.35, "trail_start": 0.35, "trail_back": 0.18}
    return {"take_profit": 0, "stop_loss": 0, "trail_start": 0, "trail_back": 0}


def is_us_trading_time(current_time):
    hour = current_time.hour
    return (17 <= hour <= 23) or (0 <= hour <= 5)


# =========================
# BACKTEST
# =========================

def run_backtest(df_5m, df_1h, df_4h, use_time_filter=True):
    position_open = False
    entry_price = 0.0
    entry_time = None
    entry_strategy = None
    entry_market = None
    entry_big_trend = None
    entry_score = 0
    max_pnl = 0.0
    last_exit_time = None

    entry_take_profit = 0.0
    entry_stop_loss = 0.0
    entry_trail_start = 0.0
    entry_trail_back = 0.0

    equity = 100.0
    peak_equity = 100.0
    max_drawdown = 0.0
    trades = []

    df_1h_times = df_1h["datetime"].tolist()
    df_4h_times = df_4h["datetime"].tolist()
    i1 = 0
    i4 = 0

    for i in range(220, len(df_5m)):
        now = df_5m.iloc[i]
        current_time = now["datetime"]
        price = now["close"]

        while i1 + 1 < len(df_1h_times) and df_1h_times[i1 + 1] <= current_time - timedelta(hours=1):
            i1 += 1

        while i4 + 1 < len(df_4h_times) and df_4h_times[i4 + 1] <= current_time - timedelta(hours=4):
            i4 += 1

        h1 = df_1h.iloc[i1]
        h4 = df_4h.iloc[i4]

        if pd.isna(now["rsi"]) or pd.isna(h1["ema200"]) or pd.isna(h4["ema200"]):
            continue

        big_trend = detect_big_trend(h1, h4)
        market = detect_short_market(now)
        strategy = get_strategy(big_trend, market)
        score = calculate_score(df_5m, i, big_trend, market, strategy)

        # EXIT
        if position_open:
            gross_pnl = ((price - entry_price) / entry_price) * 100
            net_pnl = gross_pnl - FEE_ROUND_TRIP
            hold_minutes = (current_time - entry_time).total_seconds() / 60

            if gross_pnl > max_pnl:
                max_pnl = gross_pnl

            exit_reason = None

            if gross_pnl <= entry_stop_loss:
                exit_reason = "STOP_LOSS"

            elif hold_minutes >= MIN_HOLD_MINUTES and net_pnl >= entry_take_profit:
                exit_reason = "TAKE_PROFIT"

            else:
                min_net_for_trailing = {
                    "SIDE_RSI_BB": 0.20,
                    "SIDE_DEEP_REBOUND": 0.15,
                    "BULL_PULLBACK": 0.35,
                    "BULL_PULLBACK_LIGHT": 0.25,
                    "BULL_DEEP_PULLBACK": 0.25,
                    "BEAR_SCALP": 0.12,
                }.get(entry_strategy, 0.20)

                if (
                    hold_minutes >= MIN_HOLD_MINUTES
                    and net_pnl >= min_net_for_trailing
                    and max_pnl >= entry_trail_start
                    and gross_pnl <= max_pnl - entry_trail_back
                ):
                    exit_reason = "TRAILING_STOP"

            if exit_reason is None and big_trend == "BIG_CRASH":
                exit_reason = "BIG_CRASH_EXIT"

            if exit_reason:
                equity *= (1 + net_pnl / 100)
                peak_equity = max(peak_equity, equity)
                drawdown = ((equity - peak_equity) / peak_equity) * 100
                max_drawdown = min(max_drawdown, drawdown)

                trades.append({
                    "entry_time": entry_time.strftime("%Y-%m-%d %H:%M:%S"),
                    "exit_time": current_time.strftime("%Y-%m-%d %H:%M:%S"),
                    "big_trend": entry_big_trend,
                    "market": entry_market,
                    "strategy": entry_strategy,
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
                entry_strategy = None
                entry_market = None
                entry_big_trend = None
                entry_score = 0
                max_pnl = 0.0
                last_exit_time = current_time

        # ENTRY
        if not position_open:
            if use_time_filter and not is_us_trading_time(current_time):
                continue

            if strategy.startswith("NO_TRADE"):
                continue

            if last_exit_time:
                cooldown_minutes = (current_time - last_exit_time).total_seconds() / 60
                if cooldown_minutes < REENTRY_COOLDOWN_MINUTES:
                    continue

            if score < ENTRY_SCORE:
                continue

            params = get_risk_params(strategy)
            if params["take_profit"] == 0:
                continue

            position_open = True
            entry_price = price
            entry_time = current_time
            entry_strategy = strategy
            entry_market = market
            entry_big_trend = big_trend
            entry_score = score
            max_pnl = 0.0

            entry_take_profit = params["take_profit"]
            entry_stop_loss = params["stop_loss"]
            entry_trail_start = params["trail_start"]
            entry_trail_back = params["trail_back"]

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

    win_rate = len(wins) / len(trades_df) * 100
    total_return = trades_df["equity"].iloc[-1] - 100
    avg_win = wins["net_pnl"].mean() if not wins.empty else 0
    avg_loss = losses["net_pnl"].mean() if not losses.empty else 0
    profit_factor = abs(wins["net_pnl"].sum() / losses["net_pnl"].sum()) if not losses.empty and losses["net_pnl"].sum() != 0 else 999
    exit_counts = trades_df["exit_reason"].value_counts().to_dict()

    return {
        "use_time_filter": use_time_filter,
        "trades": len(trades_df),
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
    }, trades_df


def main():
    print("Live Strategy 365 Backtest started:", now_kst(), flush=True)

    spreadsheet = init_gspread()
    summary_ws = get_or_create_ws(spreadsheet, SUMMARY_SHEET_NAME, rows=20, cols=20)
    trades_ws = get_or_create_ws(spreadsheet, TRADES_SHEET_NAME, rows=5000, cols=30)
    log_ws = get_or_create_ws(spreadsheet, RUN_LOG_SHEET_NAME, rows=1000, cols=5)

    append_run_log(log_ws, "Backtest started")

    start_dt = datetime.strptime(START_DATE, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end_dt = datetime.strptime(END_DATE, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    df_5m = calculate_indicators(fetch_klines(SYMBOL, Client.KLINE_INTERVAL_5MINUTE, start_dt, end_dt))
    df_1h = calculate_indicators(fetch_klines(SYMBOL, Client.KLINE_INTERVAL_1HOUR, start_dt, end_dt))
    df_4h = calculate_indicators(fetch_klines(SYMBOL, Client.KLINE_INTERVAL_4HOUR, start_dt, end_dt))

    stats_time, trades_time = run_backtest(df_5m, df_1h, df_4h, use_time_filter=True)
    stats_all, trades_all = run_backtest(df_5m, df_1h, df_4h, use_time_filter=False)

    summary_df = pd.DataFrame([stats_time, stats_all])
    summary_headers = list(summary_df.columns)
    summary_rows = summary_df.astype(str).values.tolist()
    clear_and_write(summary_ws, summary_headers, summary_rows)

    trades_time = trades_time.copy()
    if not trades_time.empty:
        trades_time.insert(0, "use_time_filter", True)
        trade_headers = list(trades_time.columns)
        trade_rows = trades_time.astype(str).values.tolist()
        clear_and_write(trades_ws, trade_headers, trade_rows)
    else:
        clear_and_write(trades_ws, ["message"], [["No trades with time filter"]])

    append_run_log(log_ws, "Backtest finished")
    print("Live Strategy 365 Backtest finished:", now_kst(), flush=True)
    print("Saved summary to:", SUMMARY_SHEET_NAME, flush=True)
    print("Saved trades to:", TRADES_SHEET_NAME, flush=True)


if __name__ == "__main__":
    main()
