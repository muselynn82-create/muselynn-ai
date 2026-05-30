import os
import time
import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from itertools import product

import pandas as pd
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from binance.client import Client


# ============================================================
# NY OPEN 5M SHORT MULTI-COIN V2
# Focused upgrade based on best result area:
# - SHORT only
# - EMA200_AND_BEAR / BIG_BEAR regime
# - ATR target 중심
# - high-volume NY open candle
# - compact grid + autosave + Google Sheet safe write
# ============================================================


API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_SECRET_KEY")
client = Client(API_KEY, API_SECRET, requests_params={"timeout": 20})

SYMBOLS = [
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "XRPUSDT",
]

START_DATE = "2022-01-01"
END_DATE = "2026-05-25"

FEE_ROUND_TRIP = 0.20
KST = ZoneInfo("Asia/Seoul")

GOOGLE_CLIENT_EMAIL = os.getenv("GOOGLE_CLIENT_EMAIL")
GOOGLE_PRIVATE_KEY = os.getenv("GOOGLE_PRIVATE_KEY", "").replace("\\n", "\n")
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME")

RESULT_SHEET_NAME = "NY_OPEN_MULTI_RESULTS"
TOP_SHEET_NAME = "NY_OPEN_MULTI_TOP20"
TRADES_SHEET_NAME = "NY_OPEN_MULTI_TRADES"
RUN_LOG_SHEET_NAME = "NY_OPEN_MULTI_RUNLOG"

OPEN_HOUR_KST = 22
OPEN_MINUTE_KST = 30
MIN_TRADES = 30

# 1차 확대 테스트:
# 기존 BTC 단일 결과에서 가장 좋았던 영역 중심으로만 탐색
PARAM_GRID = {
    "symbol": SYMBOLS,

    "trend_filter": [
        "EMA200_AND_BEAR",
        "BIG_BEAR",
    ],

    # 이번 버전은 ATR 타겟 중심.
    # RR은 중복/약한 결과가 많아서 제외.
    "target_mode": [
        "ATR",
    ],

    # target_mode=ATR이면 risk_reward는 실질 영향이 거의 없지만
    # 결과 비교용 컬럼 유지를 위해 2.0 고정.
    "risk_reward": [
        2.0,
    ],

    "atr_target_mult": [
        1.2,
        1.5,
        1.8,
    ],

    "retest_tolerance": [
        0.0005,
        0.001,
    ],

    "min_range_pct": [
        0.20,
        0.25,
        0.30,
    ],

    "max_range_pct": [
        1.0,
        1.2,
        1.5,
    ],

    "min_volume_ratio": [
        1.5,
        1.6,
        1.8,
    ],

    "min_range_atr_mult": [
        1.0,
        1.2,
    ],

    "entry_search_hours": [
        1,
        2,
    ],

    "max_hold_hours": [
        4,
        8,
    ],

    "confirm_close": [True],
}


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


def get_or_create_ws(spreadsheet, title, rows=1000, cols=40):
    try:
        return spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        return spreadsheet.add_worksheet(title=title, rows=rows, cols=cols)


def sanitize_for_sheet(value):
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return ""
    return value


def clear_and_write(ws, headers, rows):
    ws.clear()

    safe_headers = [sanitize_for_sheet(v) for v in headers]
    safe_rows = [[sanitize_for_sheet(v) for v in row] for row in rows]
    values = [safe_headers] + safe_rows

    if not values:
        return

    need_rows = max(len(values), 1)
    need_cols = max(len(safe_headers), 1)

    # Google Sheets 1000만 cell 제한 방지:
    # 시트는 작게 만들고 실제 저장 범위만큼만 resize.
    try:
        ws.resize(rows=need_rows, cols=need_cols)
    except Exception as e:
        print(f"Worksheet resize skipped: {e}", flush=True)

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
    batch_count = 0
    retry_count = 0

    while start_ms < end_ms:
        try:
            candles = client.get_klines(
                symbol=symbol,
                interval=interval,
                startTime=start_ms,
                endTime=end_ms,
                limit=1000,
            )
        except Exception as e:
            retry_count += 1
            print(f"Download retry {retry_count}/5 for {symbol} {interval}: {e}", flush=True)
            time.sleep(3)

            if retry_count >= 5:
                raise

            continue

        retry_count = 0

        if not candles:
            break

        all_rows.extend(candles)
        start_ms = candles[-1][0] + 1
        batch_count += 1

        if batch_count % 5 == 0:
            last_dt = pd.to_datetime(candles[-1][0], unit="ms", utc=True).tz_convert("Asia/Seoul")
            print(
                f"Downloaded {symbol} {interval}: {len(all_rows)} candles, last={last_dt}",
                flush=True,
            )

        time.sleep(0.35)

    df = pd.DataFrame(all_rows, columns=[
        "time", "open", "high", "low", "close", "volume",
        "close_time", "quote_asset_volume", "trades",
        "taker_buy_base", "taker_buy_quote", "ignore",
    ])

    if df.empty:
        raise RuntimeError(f"No data downloaded for {symbol} {interval}")

    df = df.drop_duplicates(subset=["time"]).reset_index(drop=True)
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)

    df["datetime"] = pd.to_datetime(df["time"], unit="ms", utc=True).dt.tz_convert("Asia/Seoul")
    df["date"] = df["datetime"].dt.date

    print(f"Finished downloading {symbol} {interval}: {len(df)} candles", flush=True)
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


def cache_name(symbol, interval_label):
    return f"ny_open_multi_{symbol}_{interval_label}_2022_2026.pkl"


def load_or_fetch(symbol, interval_label, interval):
    start_dt = datetime.strptime(START_DATE, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end_dt = datetime.strptime(END_DATE, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    cache_file = cache_name(symbol, interval_label)

    if os.path.exists(cache_file):
        try:
            print(f"Loading cached data: {cache_file}", flush=True)
            df = pd.read_pickle(cache_file)

            if "atr" not in df.columns or "volume_ratio" not in df.columns:
                df = add_indicators(df)
                df.to_pickle(cache_file)

            return df

        except Exception as e:
            print(f"Broken cache detected: {cache_file} / {e}", flush=True)
            try:
                os.remove(cache_file)
                print(f"Deleted broken cache: {cache_file}", flush=True)
            except Exception as remove_error:
                print(f"Failed to delete broken cache: {remove_error}", flush=True)

    df = fetch_klines(symbol, interval, start_dt, end_dt)
    df = add_indicators(df)
    df.to_pickle(cache_file)
    return df


def load_symbol_data(symbol):
    df_5m = load_or_fetch(symbol, "5m", Client.KLINE_INTERVAL_5MINUTE)
    df_1h = load_or_fetch(symbol, "1h", Client.KLINE_INTERVAL_1HOUR)
    df_4h = load_or_fetch(symbol, "4h", Client.KLINE_INTERVAL_4HOUR)
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

        big_trends.append(detect_big_trend(df_1h.iloc[i1], df_4h.iloc[i4]))

    df_5m["big_trend"] = big_trends
    return df_5m


def net_after_fee(gross_pnl):
    return ((1 + gross_pnl / 100) * (1 - FEE_ROUND_TRIP / 100) - 1) * 100


def simulate_trade(day_df, entry_idx, entry_price, stop_price, target_price, max_hold_hours):
    entry_time = day_df.iloc[entry_idx]["datetime"]
    max_exit_time = entry_time + timedelta(hours=max_hold_hours)

    for j in range(entry_idx + 1, len(day_df)):
        row = day_df.iloc[j]

        if row["datetime"] > max_exit_time:
            exit_price = row["open"]
            gross_pnl = ((entry_price - exit_price) / entry_price) * 100
            return row["datetime"].strftime("%Y-%m-%d %H:%M:%S"), exit_price, "MAX_HOLD_EXIT", gross_pnl

        hit_sl = row["high"] >= stop_price
        hit_tp = row["low"] <= target_price
        exit_time = row["datetime"].strftime("%Y-%m-%d %H:%M:%S")

        if hit_sl and hit_tp:
            gross_pnl = ((entry_price - stop_price) / entry_price) * 100
            return exit_time, stop_price, "STOP_LOSS_SAME_CANDLE", gross_pnl

        if hit_sl:
            gross_pnl = ((entry_price - stop_price) / entry_price) * 100
            return exit_time, stop_price, "STOP_LOSS", gross_pnl

        if hit_tp:
            gross_pnl = ((entry_price - target_price) / entry_price) * 100
            return exit_time, target_price, "TAKE_PROFIT", gross_pnl

    row = day_df.iloc[-1]
    exit_price = row["close"]
    gross_pnl = ((entry_price - exit_price) / entry_price) * 100
    return row["datetime"].strftime("%Y-%m-%d %H:%M:%S"), exit_price, "TIME_EXIT", gross_pnl


def trend_allowed(row, params):
    filt = params["trend_filter"]

    if filt == "BIG_BEAR":
        return row["big_trend"] == "BIG_BEAR"

    if filt == "EMA200_AND_BEAR":
        return row["close"] < row["ema200"] and row["big_trend"] in ["BIG_BEAR", "BIG_CRASH"]

    return False


def calc_target(entry_price, stop_price, row, params):
    if params["target_mode"] == "RR":
        risk = stop_price - entry_price
        return entry_price - risk * params["risk_reward"]

    atr = row["atr"]

    if pd.isna(atr) or atr <= 0:
        return None

    return entry_price - atr * params["atr_target_mult"]


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

        if range_high <= range_low or pd.isna(open_candle["atr"]) or pd.isna(open_candle["volume_ratio"]):
            continue

        range_pct = ((range_high - range_low) / open_candle["close"]) * 100
        range_atr_mult = (range_high - range_low) / open_candle["atr"]

        if range_pct < params["min_range_pct"] or range_pct > params["max_range_pct"]:
            continue

        if open_candle["volume_ratio"] < params["min_volume_ratio"]:
            continue

        if params["min_range_atr_mult"] > 0 and range_atr_mult < params["min_range_atr_mult"]:
            continue

        entry_end_time = open_candle["datetime"] + timedelta(hours=params["entry_search_hours"])
        test_end_time = entry_end_time + timedelta(hours=params["max_hold_hours"])

        day_df = day_all[
            (day_all["datetime"] >= open_candle["datetime"])
            & (day_all["datetime"] <= test_end_time)
        ].reset_index(drop=True)

        broke_down = False

        for i in range(1, len(day_df)):
            row = day_df.iloc[i]

            if row["datetime"] > entry_end_time:
                break

            if row["low"] < range_low:
                broke_down = True

            if not trend_allowed(row, params):
                continue

            short_retest = (
                broke_down
                and row["high"] >= range_low * (1 - params["retest_tolerance"])
                and row["low"] <= range_low
            )

            if params["confirm_close"]:
                short_retest = short_retest and row["close"] <= range_low

            if not short_retest:
                continue

            entry_price = range_low
            stop_price = midpoint

            if stop_price <= entry_price:
                continue

            target_price = calc_target(entry_price, stop_price, row, params)

            if target_price is None or target_price >= entry_price:
                continue

            exit_time, exit_price, exit_reason, gross_pnl = simulate_trade(
                day_df=day_df,
                entry_idx=i,
                entry_price=entry_price,
                stop_price=stop_price,
                target_price=target_price,
                max_hold_hours=params["max_hold_hours"],
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
                    "range_atr_mult": round(range_atr_mult, 4),
                    "open_volume_ratio": round(open_candle["volume_ratio"], 4),
                    "entry_price": round(entry_price, 4),
                    "stop_price": round(stop_price, 4),
                    "target_price": round(target_price, 4),
                    "exit_price": round(exit_price, 4),
                    "gross_pnl": round(gross_pnl, 4),
                    "net_pnl": round(net_pnl, 4),
                    "exit_reason": exit_reason,
                    "equity": round(equity, 4),
                    "max_drawdown": round(max_drawdown, 4),
                })

            trades.append(trade)

            # 하루 1회만 진입
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
            "same_candle_sl_count": 0,
            "max_hold_count": 0,
            "time_exit_count": 0,
        }, trades_df

    wins = trades_df[trades_df["net_pnl"] > 0]
    losses = trades_df[trades_df["net_pnl"] <= 0]
    exit_counts = trades_df["exit_reason"].value_counts().to_dict()

    total_trades = len(trades_df)
    win_rate = len(wins) / total_trades * 100
    total_return = trades_df["equity"].iloc[-1] - 100
    max_dd = trades_df["max_drawdown"].min()

    avg_win = wins["net_pnl"].mean() if not wins.empty else 0
    avg_loss = losses["net_pnl"].mean() if not losses.empty else 0

    gross_profit = wins["net_pnl"].sum() if not wins.empty else 0
    gross_loss = losses["net_pnl"].sum() if not losses.empty else 0

    profit_factor = 999 if gross_loss == 0 else abs(gross_profit / gross_loss)

    return {
        **params,
        "trades": total_trades,
        "win_rate": round(win_rate, 2),
        "total_return": round(total_return, 2),
        "max_drawdown": round(max_dd, 2),
        "avg_win": round(avg_win, 4),
        "avg_loss": round(avg_loss, 4),
        "profit_factor": round(profit_factor, 4),
        "tp_count": int(exit_counts.get("TAKE_PROFIT", 0)),
        "sl_count": int(exit_counts.get("STOP_LOSS", 0)) + int(exit_counts.get("STOP_LOSS_SAME_CANDLE", 0)),
        "same_candle_sl_count": int(exit_counts.get("STOP_LOSS_SAME_CANDLE", 0)),
        "max_hold_count": int(exit_counts.get("MAX_HOLD_EXIT", 0)),
        "time_exit_count": int(exit_counts.get("TIME_EXIT", 0)),
    }, trades_df


def score_rank(row):
    score = 0.0

    pf = min(float(row["profit_factor"]), 10.0)
    trades = int(row["trades"])
    total_return = float(row["total_return"])
    max_dd = float(row["max_drawdown"])
    win_rate = float(row["win_rate"])

    score += pf * 140
    score += total_return * 4.0
    score += win_rate * 0.4
    score += max_dd * 6.0

    if trades < MIN_TRADES:
        score -= 250
    elif trades > 500:
        score -= 60
    else:
        score += 80

    if pf < 1:
        score -= 150

    if total_return < 0:
        score -= 120

    if max_dd < -15:
        score -= 120

    # 지나치게 거래가 적고 수익률만 좋아 보이는 조합 방지
    if trades < 45:
        score -= 60

    return round(score, 4)


def main():
    print("NY Open 5M Multi Short Optimizer started:", now_kst(), flush=True)

    spreadsheet = init_gspread()
    result_ws = get_or_create_ws(spreadsheet, RESULT_SHEET_NAME, rows=1000, cols=40)
    top_ws = get_or_create_ws(spreadsheet, TOP_SHEET_NAME, rows=100, cols=40)
    trades_ws = get_or_create_ws(spreadsheet, TRADES_SHEET_NAME, rows=1000, cols=40)
    log_ws = get_or_create_ws(spreadsheet, RUN_LOG_SHEET_NAME, rows=500, cols=10)

    append_run_log(log_ws, "Backtest started")

    keys = list(PARAM_GRID.keys())
    combos = list(product(*[PARAM_GRID[k] for k in keys]))

    print(f"Total combinations: {len(combos)}", flush=True)
    append_run_log(log_ws, f"Total combinations: {len(combos)}")

    data_cache = {}
    rows = []

    for idx, values in enumerate(combos, start=1):
        params = dict(zip(keys, values))
        symbol = params["symbol"]

        if params["min_range_pct"] >= params["max_range_pct"]:
            continue

        if symbol not in data_cache:
            df_5m, df_1h, df_4h = load_symbol_data(symbol)
            df_5m = prepare_trend_map(df_5m, df_1h, df_4h)
            data_cache[symbol] = df_5m

        df_5m = data_cache[symbol]

        stats, _ = backtest_params(df_5m, params, collect_trades=False)
        stats["rank_score"] = score_rank(stats)
        stats["run_time"] = now_kst()
        rows.append(stats)

        if idx % 100 == 0:
            print(f"Progress: {idx}/{len(combos)}", flush=True)
            append_run_log(log_ws, f"Progress: {idx}/{len(combos)}")

        # 중간 저장: Railway 크레딧 부족/재시작/마지막 저장 실패 대비
        if idx % 300 == 0:
            temp_df = pd.DataFrame(rows)

            if not temp_df.empty:
                temp_df = temp_df.replace([float("inf"), float("-inf")], "").fillna("")
                temp_df = temp_df.sort_values(
                    by=["rank_score", "profit_factor", "total_return"],
                    ascending=False,
                )

                temp_save_df = temp_df.head(500)
                temp_top20_df = temp_df.head(20)

                clear_and_write(
                    result_ws,
                    list(temp_save_df.columns),
                    temp_save_df.astype(str).values.tolist(),
                )

                clear_and_write(
                    top_ws,
                    list(temp_top20_df.columns),
                    temp_top20_df.astype(str).values.tolist(),
                )

                append_run_log(log_ws, f"Auto saved top results at {idx}/{len(combos)}")
                print(f"Auto Saved: {idx}/{len(combos)}", flush=True)

    results_df = pd.DataFrame(rows)

    if results_df.empty:
        clear_and_write(result_ws, ["message"], [["No results"]])
        clear_and_write(top_ws, ["message"], [["No results"]])
        clear_and_write(trades_ws, ["message"], [["No trades"]])
        append_run_log(log_ws, "No results")
        return

    results_df = results_df.replace([float("inf"), float("-inf")], "").fillna("")
    results_df = results_df.sort_values(
        by=["rank_score", "profit_factor", "total_return"],
        ascending=False,
    )

    save_results_df = results_df.head(500)
    top20_df = results_df.head(20)

    best_params = top20_df.iloc[0][keys].to_dict()

    for k in [
        "risk_reward",
        "atr_target_mult",
        "retest_tolerance",
        "min_range_pct",
        "max_range_pct",
        "min_volume_ratio",
        "min_range_atr_mult",
    ]:
        best_params[k] = float(best_params[k])

    for k in ["entry_search_hours", "max_hold_hours"]:
        best_params[k] = int(best_params[k])

    best_params["confirm_close"] = str(best_params["confirm_close"]).lower() == "true"

    best_symbol = best_params["symbol"]
    best_df = data_cache.get(best_symbol)

    if best_df is None:
        df_5m, df_1h, df_4h = load_symbol_data(best_symbol)
        best_df = prepare_trend_map(df_5m, df_1h, df_4h)

    _, best_trades = backtest_params(best_df, best_params, collect_trades=True)
    best_trades = best_trades.replace([float("inf"), float("-inf")], "").fillna("")

    clear_and_write(
        result_ws,
        list(save_results_df.columns),
        save_results_df.astype(str).values.tolist(),
    )

    clear_and_write(
        top_ws,
        list(top20_df.columns),
        top20_df.astype(str).values.tolist(),
    )

    if not best_trades.empty:
        best_trades = best_trades.head(500)
        clear_and_write(
            trades_ws,
            list(best_trades.columns),
            best_trades.astype(str).values.tolist(),
        )
    else:
        clear_and_write(trades_ws, ["message"], [["No trades"]])

    append_run_log(log_ws, "Backtest finished")

    print("NY Open 5M Multi Short Optimizer finished:", now_kst(), flush=True)
    print("Saved result to:", RESULT_SHEET_NAME, flush=True)
    print("Saved top20 to:", TOP_SHEET_NAME, flush=True)
    print("Saved best trades to:", TRADES_SHEET_NAME, flush=True)


if __name__ == "__main__":
    main()
