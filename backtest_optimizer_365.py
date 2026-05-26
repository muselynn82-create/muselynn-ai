import os
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from itertools import product

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

GOOGLE_CLIENT_EMAIL = os.getenv("GOOGLE_CLIENT_EMAIL")
GOOGLE_PRIVATE_KEY = os.getenv("GOOGLE_PRIVATE_KEY").replace("\\n", "\n")
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME")

RESULT_SHEET_NAME = "OPTIMIZER_RESULTS_365"
TOP_SHEET_NAME = "OPTIMIZER_TOP20_365"
RUN_LOG_SHEET_NAME = "OPTIMIZER_RESULTS_365"

# 너무 넓히면 오래 걸리니 1차 자동 연구 범위
PARAM_GRID = {
    "strategy_type": ["LONG_PULLBACK", "DEADCAT_SHORT"],
    "entry_score": [70, 80],
    "rsi_limit": [26, 28, 30],
    "take_profit": [1.2, 1.8, 2.5],
    "stop_loss": [-1.0, -1.2, -1.5],
    "trail_start": [1.0, 1.5, 2.0],
    "trail_back": [0.5, 0.7, 1.0],
}

MIN_TRADES = 10
MAX_DRAWDOWN_LIMIT = -20.0


# =========================
# GOOGLE SHEETS
# =========================

def init_gspread():
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
    return gc.open(GOOGLE_SHEET_NAME)


def get_or_create_ws(spreadsheet, title, rows=2000, cols=40):
    try:
        return spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        return spreadsheet.add_worksheet(title=title, rows=rows, cols=cols)


def clear_and_write(ws, headers, rows):
    ws.clear()
    values = [headers] + rows
    if values:
        ws.update(range_name="A1", values=values)


def append_run_log(ws, message):
    ws.append_row([now_kst(), message])


# =========================
# HELPERS
# =========================

def now_kst():
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")


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
        "taker_buy_base", "taker_buy_quote", "ignore"
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


# =========================
# STRATEGY
# =========================

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


def calculate_score(now, params):
    price = now["close"]
    score = 0

    if params["strategy_type"] == "LONG_PULLBACK":

        if (
            now["rsi"] < params["rsi_limit"]
            and now["low"] <= now["bb_lower"]
            and now["close"] > now["open"]
        ):
            score += 70

        if price > now["ema100"]:
            score += 15

        if now["volume_ratio"] >= 1.0:
            score += 10

        if price >= now["ema20"] * 0.995:
            score += 10

    elif params["strategy_type"] == "DEADCAT_SHORT":

        if (
            now["rsi"] > 100 - params["rsi_limit"]
            and now["high"] >= now["bb_upper"]
            and now["close"] < now["open"]
        ):
            score += 70

        if price < now["ema100"]:
            score += 15

        if now["volume_ratio"] >= 1.0:
            score += 10

        if price <= now["ema20"] * 1.005:
            score += 10

    return score


def run_backtest(df_15m, df_1h, df_4h, params, collect_trades=False):
    position_open = False
    position_side = None
    entry_price = 0.0
    entry_time = None
    entry_score = 0
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
        now = df_15m.iloc[i]
        current_time = now["datetime"]

        # 확정 마감된 상위 타임프레임 봉만 사용
        while i1 + 1 < len(df_1h_times) and df_1h_times[i1 + 1] <= current_time - timedelta(hours=1):
            i1 += 1

        while i4 + 1 < len(df_4h_times) and df_4h_times[i4 + 1] <= current_time - timedelta(hours=4):
            i4 += 1

        h1 = df_1h.iloc[i1]
        h4 = df_4h.iloc[i4]

        if pd.isna(now["rsi"]) or pd.isna(h1["ema200"]) or pd.isna(h4["ema200"]):
            continue

        big_trend = detect_big_trend(h1, h4)
        price = now["close"]

        # EXIT
        if position_open:

            if position_side == "LONG":
                gross_pnl = ((price - entry_price) / entry_price) * 100
            else:
                gross_pnl = ((entry_price - price) / entry_price) * 100

            net_pnl = (
                ((1 + gross_pnl / 100) * (1 - FEE_ROUND_TRIP / 100)) - 1
            ) * 100

            max_pnl = max(max_pnl, net_pnl)

            should_exit = False
            exit_reason = ""

            if net_pnl >= params["take_profit"]:
                should_exit = True
                exit_reason = "TP"

            elif net_pnl <= params["stop_loss"]:
                should_exit = True
                exit_reason = "SL"

            elif (
                max_pnl >= params["trail_start"]
                and net_pnl <= max_pnl - params["trail_back"]
            ):
                should_exit = True
                exit_reason = "TRAIL"

            elif big_trend == "BIG_CRASH":
                should_exit = True
                exit_reason = "CRASH"

            if should_exit:

                trades.append({
                    "entry_time": entry_time,
                    "exit_time": current_time.strftime("%Y-%m-%d %H:%M:%S"),
                    "side": position_side,
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
                position_side = None
                entry_price = 0.0
                entry_time = None
                entry_score = 0
                max_pnl = 0.0
                last_exit_time = current_time

        # ENTRY
        if not position_open:

            in_cooldown = False

            if last_exit_time:
                cooldown_minutes = (current_time - last_exit_time).total_seconds() / 60
                in_cooldown = cooldown_minutes < 3

            allow_entry = (
                (
                    params["strategy_type"] == "LONG_PULLBACK"
                    and big_trend == "BIG_BULL"
                )
                or
                (
                    params["strategy_type"] == "DEADCAT_SHORT"
                    and big_trend == "BIG_BEAR"
                )
            )

            score = calculate_score(now, params)

            if allow_entry and not in_cooldown and score >= params["entry_score"]:

                position_open = True

                position_side = (
                    "LONG"
                    if params["strategy_type"] == "LONG_PULLBACK"
                    else "SHORT"
                )

                entry_price = price
                entry_time = current_time.strftime("%Y-%m-%d %H:%M:%S")
                entry_score = score
                max_pnl = 0.0

    trades_df = pd.DataFrame(trades)

    if trades_df.empty:
        return empty_stats(params), trades_df

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
        **params,
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


def empty_stats(params):
    return {
        **params,
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
    }


def score_rank(row):
    score = 0
    score += row["profit_factor"] * 100
    score += row["total_return"] * 2
    score += row["win_rate"] * 0.4
    score += row["max_drawdown"] * 3

    # 실전성 필터
    if row["trades"] < MIN_TRADES:
        score -= 120
    elif row["trades"] < 20:
        score -= 50
    elif row["trades"] > 200:
        score -= 40

    if row["profit_factor"] < 1:
        score -= 80

    if row["total_return"] < 0:
        score -= 50

    if row["max_drawdown"] < MAX_DRAWDOWN_LIMIT:
        score -= 200

    return round(score, 4)


# =========================
# MAIN
# =========================

def main():
    print("Google Sheet Optimizer started:", now_kst(), flush=True)

    spreadsheet = init_gspread()
    result_ws = get_or_create_ws(spreadsheet, RESULT_SHEET_NAME, rows=6000, cols=40)
    top_ws = get_or_create_ws(spreadsheet, TOP_SHEET_NAME, rows=100, cols=40)
    log_ws = get_or_create_ws(spreadsheet, RUN_LOG_SHEET_NAME, rows=1000, cols=5)

    append_run_log(log_ws, "Optimizer started")

    start_dt = datetime.strptime(START_DATE, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end_dt = datetime.strptime(END_DATE, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    df_15m = calculate_indicators(fetch_klines(SYMBOL, Client.KLINE_INTERVAL_15MINUTE, start_dt, end_dt))
    df_1h = calculate_indicators(fetch_klines(SYMBOL, Client.KLINE_INTERVAL_1HOUR, start_dt, end_dt))
    df_4h = calculate_indicators(fetch_klines(SYMBOL, Client.KLINE_INTERVAL_4HOUR, start_dt, end_dt))

    keys = list(PARAM_GRID.keys())
    combos = list(product(*[PARAM_GRID[k] for k in keys]))

    append_run_log(log_ws, f"Total combinations: {len(combos)}")
    print(f"Total combinations: {len(combos)}", flush=True)

    rows = []

    for idx, values in enumerate(combos, start=1):
        params = dict(zip(keys, values))
        stats, _ = run_backtest(df_15m, df_1h, df_4h, params)
        stats["rank_score"] = score_rank(stats)
        stats["run_time"] = now_kst()
        rows.append(stats)

        if idx % 100 == 0:
            print(f"Progress: {idx}/{len(combos)}", flush=True)
            append_run_log(log_ws, f"Progress: {idx}/{len(combos)}")

    results_df = pd.DataFrame(rows)
    results_df = results_df.sort_values(
        by=["rank_score", "profit_factor", "total_return"],
        ascending=False
    )

    top20_df = results_df.head(20)

    result_headers = list(results_df.columns)
    result_rows = results_df.astype(str).values.tolist()

    top_headers = list(top20_df.columns)
    top_rows = top20_df.astype(str).values.tolist()

    clear_and_write(result_ws, result_headers, result_rows)
    clear_and_write(top_ws, top_headers, top_rows)

    append_run_log(log_ws, "Optimizer finished")
    print("Google Sheet Optimizer finished:", now_kst(), flush=True)
    print("Top 20 saved to Google Sheet:", TOP_SHEET_NAME, flush=True)


if __name__ == "__main__":
    main()
