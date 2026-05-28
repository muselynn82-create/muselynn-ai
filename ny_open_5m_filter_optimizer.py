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

START_DATE = "2022-01-01"
END_DATE = "2026-05-25"

FEE_ROUND_TRIP = 0.20
KST = ZoneInfo("Asia/Seoul")

GOOGLE_CLIENT_EMAIL = os.getenv("GOOGLE_CLIENT_EMAIL")
GOOGLE_PRIVATE_KEY = os.getenv("GOOGLE_PRIVATE_KEY", "").replace("\\n", "\n")
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME")

RESULT_SHEET_NAME = "NY_OPEN_FILTER_RESULTS"
TOP_SHEET_NAME = "NY_OPEN_FILTER_TOP20"
TRADES_SHEET_NAME = "NY_OPEN_FILTER_TRADES"
RUN_LOG_SHEET_NAME = "NY_OPEN_FILTER_RUNLOG"

CACHE_5M = "btc_5m_2022_2026.pkl"
CACHE_1H = "btc_1h_2022_2026.pkl"
CACHE_4H = "btc_4h_2022_2026.pkl"

OPEN_HOUR_KST = 22
OPEN_MINUTE_KST = 30

PARAM_GRID = {
    "direction_mode": ["LONG_ONLY", "SHORT_ONLY", "BOTH"],
    "trend_filter": ["NONE", "EMA200", "BIG_TREND"],
    "risk_reward": [1.5, 2.0, 2.5, 3.0],
    "retest_tolerance": [0.0, 0.0005, 0.001],
    "min_range_pct": [0.05, 0.10, 0.15],
    "max_range_pct": [0.8, 1.2, 1.8],
    "min_volume_ratio": [0.0, 1.0, 1.3],
    "entry_search_hours": [2, 4, 8],
    "confirm_close": [True],
}

MIN_TRADES = 30


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


def get_or_create_ws(spreadsheet, title, rows=5000, cols=50):
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
        time.sleep(0.35)

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
    df["date"] = df["datetime"].dt.date

    return df


def add_indicators(df):
    df = df.copy()
    close = df["close"]
    high = df["high"]
    low = df["low"]

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


def load_or_fetch(cache_file, interval):
    start_dt = datetime.strptime(START_DATE, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end_dt = datetime.strptime(END_DATE, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    if os.path.exists(cache_file):
        print(f"Loading cached data: {cache_file}", flush=True)
        return pd.read_pickle(cache_file)

    df = fetch_klines(SYMBOL, interval, start_dt, end_dt)
    df = add_indicators(df)
    df.to_pickle(cache_file)
    return df


def load_data():
    df_5m = load_or_fetch(CACHE_5M, Client.KLINE_INTERVAL_5MINUTE)
    df_1h = load_or_fetch(CACHE_1H, Client.KLINE_INTERVAL_1HOUR)
    df_4h = load_or_fetch(CACHE_4H, Client.KLINE_INTERVAL_4HOUR)
    return df_5m, df_1h, df_4h


def detect_big_trend(h1, h4):
    if pd.isna(h1["ema200"]) or pd.isna(h4["ema200"]):
        return "NO_TRADE"

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


def prepare_trend_map(df_5m, df_1h, df_4h):
    df_5m = df_5m.copy()

    df_1h_times = df_1h["datetime"].tolist()
    df_4h_times = df_4h["datetime"].tolist()
    i1 = 0
    i4 = 0

    big_trends = []

    for _, row in df_5m.iterrows():
        current_time = row["datetime"]

        while i1 + 1 < len(df_1h_times) and df_1h_times[i1 + 1] <= current_time - timedelta(hours=1):
            i1 += 1

        while i4 + 1 < len(df_4h_times) and df_4h_times[i4 + 1] <= current_time - timedelta(hours=4):
            i4 += 1

        h1 = df_1h.iloc[i1]
        h4 = df_4h.iloc[i4]
        big_trends.append(detect_big_trend(h1, h4))

    df_5m["big_trend"] = big_trends
    return df_5m


def net_after_fee(gross_pnl):
    return ((1 + gross_pnl / 100) * (1 - FEE_ROUND_TRIP / 100) - 1) * 100


def simulate_trade(day_df, entry_idx, side, entry_price, stop_price, target_price):
    for j in range(entry_idx + 1, len(day_df)):
        row = day_df.iloc[j]
        high = row["high"]
        low = row["low"]
        exit_time = row["datetime"].strftime("%Y-%m-%d %H:%M:%S")

        if side == "LONG":
            hit_sl = low <= stop_price
            hit_tp = high >= target_price

            if hit_sl and hit_tp:
                exit_price = stop_price
                gross_pnl = ((exit_price - entry_price) / entry_price) * 100
                return exit_time, exit_price, "STOP_LOSS_SAME_CANDLE", gross_pnl

            if hit_sl:
                exit_price = stop_price
                gross_pnl = ((exit_price - entry_price) / entry_price) * 100
                return exit_time, exit_price, "STOP_LOSS", gross_pnl

            if hit_tp:
                exit_price = target_price
                gross_pnl = ((exit_price - entry_price) / entry_price) * 100
                return exit_time, exit_price, "TAKE_PROFIT", gross_pnl

        else:
            hit_sl = high >= stop_price
            hit_tp = low <= target_price

            if hit_sl and hit_tp:
                exit_price = stop_price
                gross_pnl = ((entry_price - exit_price) / entry_price) * 100
                return exit_time, exit_price, "STOP_LOSS_SAME_CANDLE", gross_pnl

            if hit_sl:
                exit_price = stop_price
                gross_pnl = ((entry_price - exit_price) / entry_price) * 100
                return exit_time, exit_price, "STOP_LOSS", gross_pnl

            if hit_tp:
                exit_price = target_price
                gross_pnl = ((entry_price - exit_price) / entry_price) * 100
                return exit_time, exit_price, "TAKE_PROFIT", gross_pnl

    row = day_df.iloc[-1]
    exit_price = row["close"]
    exit_time = row["datetime"].strftime("%Y-%m-%d %H:%M:%S")

    if side == "LONG":
        gross_pnl = ((exit_price - entry_price) / entry_price) * 100
    else:
        gross_pnl = ((entry_price - exit_price) / entry_price) * 100

    return exit_time, exit_price, "TIME_EXIT", gross_pnl


def side_allowed(side, params):
    mode = params["direction_mode"]
    return mode == "BOTH" or mode == f"{side}_ONLY"


def trend_allowed(side, row, params):
    filt = params["trend_filter"]

    if filt == "NONE":
        return True

    if filt == "EMA200":
        return row["close"] > row["ema200"] if side == "LONG" else row["close"] < row["ema200"]

    if filt == "BIG_TREND":
        return row["big_trend"] == "BIG_BULL" if side == "LONG" else row["big_trend"] in ["BIG_BEAR", "BIG_CRASH"]

    return False


def backtest_params(df, params, collect_trades=False):
    trades = []
    equity = 100.0
    peak_equity = 100.0
    max_drawdown = 0.0

    grouped = df.groupby("date", sort=True)

    for trade_date, day_all in grouped:
        day_all = day_all.reset_index(drop=True)

        open_candle_df = day_all[
            (day_all["datetime"].dt.hour == OPEN_HOUR_KST)
            & (day_all["datetime"].dt.minute == OPEN_MINUTE_KST)
        ]

        if open_candle_df.empty:
            continue

        open_candle = open_candle_df.iloc[0]
        range_high = float(open_candle["high"])
        range_low = float(open_candle["low"])
        midpoint = (range_high + range_low) / 2

        if range_high <= range_low:
            continue

        range_pct = ((range_high - range_low) / open_candle["close"]) * 100

        if range_pct < params["min_range_pct"] or range_pct > params["max_range_pct"]:
            continue

        if params["min_volume_ratio"] > 0 and open_candle["volume_ratio"] < params["min_volume_ratio"]:
            continue

        end_time = open_candle["datetime"] + timedelta(hours=params["entry_search_hours"])
        day_df = day_all[
            (day_all["datetime"] >= open_candle["datetime"])
            & (day_all["datetime"] <= end_time)
        ].reset_index(drop=True)

        broke_up = False
        broke_down = False

        for i in range(1, len(day_df)):
            row = day_df.iloc[i]
            high = row["high"]
            low = row["low"]
            close = row["close"]

            if high > range_high:
                broke_up = True

            if low < range_low:
                broke_down = True

            if side_allowed("LONG", params) and trend_allowed("LONG", row, params):
                long_retest = (
                    broke_up
                    and low <= range_high * (1 + params["retest_tolerance"])
                    and high >= range_high
                )

                if params["confirm_close"]:
                    long_retest = long_retest and close >= range_high

                if long_retest:
                    entry_price = range_high
                    stop_price = midpoint
                    risk = entry_price - stop_price

                    if risk > 0:
                        target_price = entry_price + risk * params["risk_reward"]
                        exit_time, exit_price, exit_reason, gross_pnl = simulate_trade(
                            day_df, i, "LONG", entry_price, stop_price, target_price
                        )
                        net_pnl = net_after_fee(gross_pnl)
                        equity *= (1 + net_pnl / 100)
                        peak_equity = max(peak_equity, equity)
                        drawdown = ((equity - peak_equity) / peak_equity) * 100
                        max_drawdown = min(max_drawdown, drawdown)

                        trade = {
                            "net_pnl": net_pnl,
                            "exit_reason": exit_reason,
                            "equity": equity,
                            "max_drawdown": max_drawdown,
                        }

                        if collect_trades:
                            trade.update({
                                **params,
                                "date": str(trade_date),
                                "side": "LONG",
                                "open_candle_time": open_candle["datetime"].strftime("%Y-%m-%d %H:%M:%S"),
                                "entry_time": row["datetime"].strftime("%Y-%m-%d %H:%M:%S"),
                                "exit_time": exit_time,
                                "big_trend": row["big_trend"],
                                "range_pct": round(range_pct, 4),
                                "open_volume_ratio": round(open_candle["volume_ratio"], 4),
                                "range_high": round(range_high, 2),
                                "range_low": round(range_low, 2),
                                "entry_price": round(entry_price, 2),
                                "stop_price": round(stop_price, 2),
                                "target_price": round(target_price, 2),
                                "exit_price": round(exit_price, 2),
                                "gross_pnl": round(gross_pnl, 4),
                            })
                        trades.append(trade)
                        break

            if side_allowed("SHORT", params) and trend_allowed("SHORT", row, params):
                short_retest = (
                    broke_down
                    and high >= range_low * (1 - params["retest_tolerance"])
                    and low <= range_low
                )

                if params["confirm_close"]:
                    short_retest = short_retest and close <= range_low

                if short_retest:
                    entry_price = range_low
                    stop_price = midpoint
                    risk = stop_price - entry_price

                    if risk > 0:
                        target_price = entry_price - risk * params["risk_reward"]
                        exit_time, exit_price, exit_reason, gross_pnl = simulate_trade(
                            day_df, i, "SHORT", entry_price, stop_price, target_price
                        )
                        net_pnl = net_after_fee(gross_pnl)
                        equity *= (1 + net_pnl / 100)
                        peak_equity = max(peak_equity, equity)
                        drawdown = ((equity - peak_equity) / peak_equity) * 100
                        max_drawdown = min(max_drawdown, drawdown)

                        trade = {
                            "net_pnl": net_pnl,
                            "exit_reason": exit_reason,
                            "equity": equity,
                            "max_drawdown": max_drawdown,
                        }

                        if collect_trades:
                            trade.update({
                                **params,
                                "date": str(trade_date),
                                "side": "SHORT",
                                "open_candle_time": open_candle["datetime"].strftime("%Y-%m-%d %H:%M:%S"),
                                "entry_time": row["datetime"].strftime("%Y-%m-%d %H:%M:%S"),
                                "exit_time": exit_time,
                                "big_trend": row["big_trend"],
                                "range_pct": round(range_pct, 4),
                                "open_volume_ratio": round(open_candle["volume_ratio"], 4),
                                "range_high": round(range_high, 2),
                                "range_low": round(range_low, 2),
                                "entry_price": round(entry_price, 2),
                                "stop_price": round(stop_price, 2),
                                "target_price": round(target_price, 2),
                                "exit_price": round(exit_price, 2),
                                "gross_pnl": round(gross_pnl, 4),
                            })
                        trades.append(trade)
                        break

    trades_df = pd.DataFrame(trades)

    if trades_df.empty:
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
            "time_exit_count": 0,
        }, trades_df

    wins = trades_df[trades_df["net_pnl"] > 0]
    losses = trades_df[trades_df["net_pnl"] <= 0]
    exit_counts = trades_df["exit_reason"].value_counts().to_dict()

    total_trades = len(trades_df)
    win_rate = len(wins) / total_trades * 100
    total_return = trades_df["equity"].iloc[-1] - 100
    max_drawdown = trades_df["max_drawdown"].min()
    avg_win = wins["net_pnl"].mean() if not wins.empty else 0
    avg_loss = losses["net_pnl"].mean() if not losses.empty else 0
    profit_factor = abs(wins["net_pnl"].sum() / losses["net_pnl"].sum()) if not losses.empty and losses["net_pnl"].sum() != 0 else 999

    return {
        **params,
        "trades": total_trades,
        "win_rate": round(win_rate, 2),
        "total_return": round(total_return, 2),
        "max_drawdown": round(max_drawdown, 2),
        "avg_win": round(avg_win, 4),
        "avg_loss": round(avg_loss, 4),
        "profit_factor": round(profit_factor, 4),
        "tp_count": int(exit_counts.get("TAKE_PROFIT", 0)),
        "sl_count": int(exit_counts.get("STOP_LOSS", 0)) + int(exit_counts.get("STOP_LOSS_SAME_CANDLE", 0)),
        "same_candle_sl_count": int(exit_counts.get("STOP_LOSS_SAME_CANDLE", 0)),
        "time_exit_count": int(exit_counts.get("TIME_EXIT", 0)),
    }, trades_df


def score_rank(row):
    score = 0
    score += row["profit_factor"] * 100
    score += row["total_return"] * 2
    score += row["win_rate"] * 0.5
    score += row["max_drawdown"] * 3

    if row["trades"] < MIN_TRADES:
        score -= 150
    elif row["trades"] > 500:
        score -= 80

    if row["profit_factor"] < 1:
        score -= 120

    if row["total_return"] < 0:
        score -= 80

    return round(score, 4)


def main():
    print("NY Open 5M Filter Optimizer started:", now_kst(), flush=True)

    spreadsheet = init_gspread()
    result_ws = get_or_create_ws(spreadsheet, RESULT_SHEET_NAME, rows=10000, cols=60)
    top_ws = get_or_create_ws(spreadsheet, TOP_SHEET_NAME, rows=100, cols=60)
    trades_ws = get_or_create_ws(spreadsheet, TRADES_SHEET_NAME, rows=10000, cols=60)
    log_ws = get_or_create_ws(spreadsheet, RUN_LOG_SHEET_NAME, rows=1000, cols=5)

    append_run_log(log_ws, "Backtest started")

    df_5m, df_1h, df_4h = load_data()
    df_5m = prepare_trend_map(df_5m, df_1h, df_4h)

    keys = list(PARAM_GRID.keys())
    combos = list(product(*[PARAM_GRID[k] for k in keys]))

    print(f"Total combinations: {len(combos)}", flush=True)
    append_run_log(log_ws, f"Total combinations: {len(combos)}")

    rows = []

    for idx, values in enumerate(combos, start=1):
        params = dict(zip(keys, values))

        if params["min_range_pct"] >= params["max_range_pct"]:
            continue

        stats, _ = backtest_params(df_5m, params, collect_trades=False)
        stats["rank_score"] = score_rank(stats)
        stats["run_time"] = now_kst()
        rows.append(stats)

        if idx % 100 == 0:
            print(f"Progress: {idx}/{len(combos)}", flush=True)
            append_run_log(log_ws, f"Progress: {idx}/{len(combos)}")

    results_df = pd.DataFrame(rows)

    if results_df.empty:
        clear_and_write(result_ws, ["message"], [["No results"]])
        clear_and_write(top_ws, ["message"], [["No results"]])
        clear_and_write(trades_ws, ["message"], [["No trades"]])
        return

    results_df = results_df.sort_values(
        by=["rank_score", "profit_factor", "total_return"],
        ascending=False,
    )

    top20_df = results_df.head(20)

    best_params = top20_df.iloc[0][keys].to_dict()
    for k in ["risk_reward", "retest_tolerance", "min_range_pct", "max_range_pct", "min_volume_ratio"]:
        best_params[k] = float(best_params[k])
    best_params["entry_search_hours"] = int(best_params["entry_search_hours"])
    best_params["confirm_close"] = str(best_params["confirm_close"]).lower() == "true"

    _, best_trades = backtest_params(df_5m, best_params, collect_trades=True)

    clear_and_write(result_ws, list(results_df.columns), results_df.astype(str).values.tolist())
    clear_and_write(top_ws, list(top20_df.columns), top20_df.astype(str).values.tolist())

    if not best_trades.empty:
        clear_and_write(trades_ws, list(best_trades.columns), best_trades.astype(str).values.tolist())
    else:
        clear_and_write(trades_ws, ["message"], [["No trades"]])

    append_run_log(log_ws, "Backtest finished")
    print("NY Open 5M Filter Optimizer finished:", now_kst(), flush=True)
    print("Saved result to:", RESULT_SHEET_NAME, flush=True)
    print("Saved top20 to:", TOP_SHEET_NAME, flush=True)
    print("Saved best trades to:", TRADES_SHEET_NAME, flush=True)


if __name__ == "__main__":
    main()
