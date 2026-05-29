import os
import time
import math
from datetime import datetime, timezone
from itertools import product
from zoneinfo import ZoneInfo

import pandas as pd
import numpy as np
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from binance.client import Client


API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_SECRET_KEY")
client = Client(API_KEY, API_SECRET, requests_params={"timeout": 20})

START_DATE = "2022-01-01"
END_DATE = "2026-05-25"
FEE_ROUND_TRIP = 0.20
KST = ZoneInfo("Asia/Seoul")

GOOGLE_CLIENT_EMAIL = os.getenv("GOOGLE_CLIENT_EMAIL")
GOOGLE_PRIVATE_KEY = os.getenv("GOOGLE_PRIVATE_KEY", "").replace("\\n", "\n")
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME")

RESULT_SHEET_NAME = "HA_STOCH_RESULTS"
TOP_SHEET_NAME = "HA_STOCH_TOP20"
TRADES_SHEET_NAME = "HA_STOCH_TRADES"
RUN_LOG_SHEET_NAME = "HA_STOCH_RUNLOG"

CACHE_PREFIX = "ha_stoch"

SYMBOLS = [
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
]

INTERVAL_MAP = {
    "1h": Client.KLINE_INTERVAL_1HOUR,
    "2h": Client.KLINE_INTERVAL_2HOUR,
    "4h": Client.KLINE_INTERVAL_4HOUR,
}

PARAM_GRID = {
    # 1차 압축 테스트용: 30,618 조합 -> 1,296 조합
    "symbol": SYMBOLS,
    "interval": ["1h", "4h"],
    "direction_mode": ["LONG_ONLY", "SHORT_ONLY"],
    "take_profit": [3.0, 4.0],
    "stop_loss": [-1.5, -2.0],
    "stoch_zone": ["NONE", "SOFT_ZONE"],
    "wick_tolerance": [0.0, 0.0005],
    "ema_price_source": ["REAL_CLOSE"],
    "min_ema_distance_pct": [0.0, 0.5],
}

MIN_TRADES = 50


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

    # Google Sheets 전체 1000만 cell 제한 방지:
    # 시트는 작게 만들고, 실제 저장할 범위만큼만 resize한다.
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
            print(f"Downloaded {symbol} {interval}: {len(all_rows)} candles, last={last_dt}", flush=True)

        time.sleep(0.35)

    df = pd.DataFrame(all_rows, columns=[
        "time", "open", "high", "low", "close", "volume",
        "close_time", "quote_asset_volume", "trades",
        "taker_buy_base", "taker_buy_quote", "ignore",
    ])

    if df.empty:
        return df

    df = df.drop_duplicates(subset=["time"]).reset_index(drop=True)
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)

    df["datetime"] = pd.to_datetime(df["time"], unit="ms", utc=True).dt.tz_convert("Asia/Seoul")
    print(f"Finished downloading {symbol} {interval}: {len(df)} candles", flush=True)
    return add_indicators(df)


def add_heikin_ashi(df):
    df = df.copy()
    ha_close = (df["open"] + df["high"] + df["low"] + df["close"]) / 4
    ha_open = []

    for i in range(len(df)):
        if i == 0:
            ha_open.append((df.loc[i, "open"] + df.loc[i, "close"]) / 2)
        else:
            ha_open.append((ha_open[i - 1] + ha_close.iloc[i - 1]) / 2)

    df["ha_open"] = ha_open
    df["ha_close"] = ha_close
    df["ha_high"] = pd.concat([df["high"], df["ha_open"], df["ha_close"]], axis=1).max(axis=1)
    df["ha_low"] = pd.concat([df["low"], df["ha_open"], df["ha_close"]], axis=1).min(axis=1)
    df["ha_bull"] = df["ha_close"] > df["ha_open"]
    df["ha_bear"] = df["ha_close"] < df["ha_open"]
    return df


def add_stoch_rsi(df, rsi_length=14, stoch_length=14, k_smooth=3, d_smooth=3):
    df = df.copy()

    # pd.NA가 rolling 연산에서 object dtype 에러를 만들 수 있어서
    # 전부 float + np.nan 기반으로 계산한다.
    close = pd.to_numeric(df["close"], errors="coerce").astype(float)

    delta = close.diff()
    gain = delta.clip(lower=0).astype(float)
    loss = (-delta.clip(upper=0)).astype(float)

    avg_gain = gain.ewm(alpha=1 / rsi_length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / rsi_length, adjust=False).mean()

    avg_loss = avg_loss.replace(0, np.nan)
    rs = avg_gain / avg_loss
    rsi = (100 - (100 / (1 + rs))).astype(float)

    min_rsi = rsi.rolling(stoch_length, min_periods=stoch_length).min()
    max_rsi = rsi.rolling(stoch_length, min_periods=stoch_length).max()

    denom = (max_rsi - min_rsi).replace(0, np.nan)
    stoch_rsi = ((rsi - min_rsi) / denom * 100).astype(float)

    k = stoch_rsi.rolling(k_smooth, min_periods=k_smooth).mean()
    d = k.rolling(d_smooth, min_periods=d_smooth).mean()

    df["rsi"] = rsi
    df["stoch_k"] = k.astype(float)
    df["stoch_d"] = d.astype(float)

    return df

def add_indicators(df):
    df = add_heikin_ashi(df)
    df["ema200_real"] = df["close"].ewm(span=200, adjust=False).mean()
    df["ema200_ha"] = df["ha_close"].ewm(span=200, adjust=False).mean()
    df = add_stoch_rsi(df, 14, 14, 3, 3)
    return df


def load_data(symbol, interval_name):
    interval = INTERVAL_MAP[interval_name]
    cache_file = f"{CACHE_PREFIX}_{symbol}_{interval_name}.pkl"

    if os.path.exists(cache_file):
        print(f"Loading cached {symbol} {interval_name}: {cache_file}", flush=True)
        df = pd.read_pickle(cache_file)
        required_cols = ["ha_open", "ha_close", "ema200_real", "stoch_k", "stoch_d"]
        if all(col in df.columns for col in required_cols):
            return df
        df = add_indicators(df)
        df.to_pickle(cache_file)
        return df

    start_dt = datetime.strptime(START_DATE, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end_dt = datetime.strptime(END_DATE, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    df = fetch_klines(symbol, interval, start_dt, end_dt)
    if df.empty:
        print(f"No data: {symbol} {interval_name}", flush=True)
        return df
    df.to_pickle(cache_file)
    return df


def net_after_fee(gross_pnl):
    return ((1 + gross_pnl / 100) * (1 - FEE_ROUND_TRIP / 100) - 1) * 100


def has_no_lower_wick(row, tolerance):
    body_low = min(row["ha_open"], row["ha_close"])
    allowed = row["close"] * tolerance
    return abs(row["ha_low"] - body_low) <= allowed


def has_no_upper_wick(row, tolerance):
    body_high = max(row["ha_open"], row["ha_close"])
    allowed = row["close"] * tolerance
    return abs(row["ha_high"] - body_high) <= allowed


def stoch_cross_long(prev, now, zone):
    if pd.isna(prev["stoch_k"]) or pd.isna(prev["stoch_d"]) or pd.isna(now["stoch_k"]) or pd.isna(now["stoch_d"]):
        return False
    crossed = prev["stoch_k"] <= prev["stoch_d"] and now["stoch_k"] > now["stoch_d"]
    if not crossed:
        return False
    if zone == "STRICT_ZONE":
        return now["stoch_k"] <= 20 or now["stoch_d"] <= 20
    if zone == "SOFT_ZONE":
        return now["stoch_k"] <= 35 or now["stoch_d"] <= 35
    return True


def stoch_cross_short(prev, now, zone):
    if pd.isna(prev["stoch_k"]) or pd.isna(prev["stoch_d"]) or pd.isna(now["stoch_k"]) or pd.isna(now["stoch_d"]):
        return False
    crossed = prev["stoch_k"] >= prev["stoch_d"] and now["stoch_k"] < now["stoch_d"]
    if not crossed:
        return False
    if zone == "STRICT_ZONE":
        return now["stoch_k"] >= 80 or now["stoch_d"] >= 80
    if zone == "SOFT_ZONE":
        return now["stoch_k"] >= 65 or now["stoch_d"] >= 65
    return True


def trend_ok_long(row, params):
    if params["ema_price_source"] == "HA_CLOSE":
        price = row["ha_close"]
        ema = row["ema200_ha"]
    else:
        price = row["close"]
        ema = row["ema200_real"]
    if pd.isna(ema):
        return False
    dist = ((price - ema) / ema) * 100
    return price > ema and dist >= params["min_ema_distance_pct"]


def trend_ok_short(row, params):
    if params["ema_price_source"] == "HA_CLOSE":
        price = row["ha_close"]
        ema = row["ema200_ha"]
    else:
        price = row["close"]
        ema = row["ema200_real"]
    if pd.isna(ema):
        return False
    dist = ((ema - price) / ema) * 100
    return price < ema and dist >= params["min_ema_distance_pct"]


def side_allowed(side, params):
    mode = params["direction_mode"]
    return mode == "BOTH" or mode == f"{side}_ONLY"


def simulate_trade(df, entry_idx, side, entry_price, take_profit, stop_loss):
    if side == "LONG":
        target_price = entry_price * (1 + take_profit / 100)
        stop_price = entry_price * (1 + stop_loss / 100)
    else:
        target_price = entry_price * (1 - take_profit / 100)
        stop_price = entry_price * (1 - stop_loss / 100)

    for j in range(entry_idx + 1, len(df)):
        row = df.iloc[j]
        high = row["high"]
        low = row["low"]
        exit_time = row["datetime"].strftime("%Y-%m-%d %H:%M:%S")

        if side == "LONG":
            hit_sl = low <= stop_price
            hit_tp = high >= target_price
            if hit_sl and hit_tp:
                gross_pnl = ((stop_price - entry_price) / entry_price) * 100
                return exit_time, stop_price, "STOP_LOSS_SAME_CANDLE", gross_pnl
            if hit_sl:
                gross_pnl = ((stop_price - entry_price) / entry_price) * 100
                return exit_time, stop_price, "STOP_LOSS", gross_pnl
            if hit_tp:
                gross_pnl = ((target_price - entry_price) / entry_price) * 100
                return exit_time, target_price, "TAKE_PROFIT", gross_pnl
        else:
            hit_sl = high >= stop_price
            hit_tp = low <= target_price
            if hit_sl and hit_tp:
                gross_pnl = ((entry_price - stop_price) / entry_price) * 100
                return exit_time, stop_price, "STOP_LOSS_SAME_CANDLE", gross_pnl
            if hit_sl:
                gross_pnl = ((entry_price - stop_price) / entry_price) * 100
                return exit_time, stop_price, "STOP_LOSS", gross_pnl
            if hit_tp:
                gross_pnl = ((entry_price - target_price) / entry_price) * 100
                return exit_time, target_price, "TAKE_PROFIT", gross_pnl

    row = df.iloc[-1]
    exit_price = row["close"]
    exit_time = row["datetime"].strftime("%Y-%m-%d %H:%M:%S")
    if side == "LONG":
        gross_pnl = ((exit_price - entry_price) / entry_price) * 100
    else:
        gross_pnl = ((entry_price - exit_price) / entry_price) * 100
    return exit_time, exit_price, "TIME_EXIT", gross_pnl


def backtest_params(df, params, collect_trades=False):
    trades = []
    equity = 100.0
    peak_equity = 100.0
    max_drawdown = 0.0

    i = 201
    while i < len(df):
        prev = df.iloc[i - 1]
        now = df.iloc[i]
        side = None

        long_signal = (
            side_allowed("LONG", params)
            and trend_ok_long(now, params)
            and stoch_cross_long(prev, now, params["stoch_zone"])
            and now["ha_bull"]
            and has_no_lower_wick(now, params["wick_tolerance"])
        )
        short_signal = (
            side_allowed("SHORT", params)
            and trend_ok_short(now, params)
            and stoch_cross_short(prev, now, params["stoch_zone"])
            and now["ha_bear"]
            and has_no_upper_wick(now, params["wick_tolerance"])
        )

        if long_signal:
            side = "LONG"
        elif short_signal:
            side = "SHORT"
        else:
            i += 1
            continue

        entry_price = now["close"]
        exit_time, exit_price, exit_reason, gross_pnl = simulate_trade(
            df=df,
            entry_idx=i,
            side=side,
            entry_price=entry_price,
            take_profit=params["take_profit"],
            stop_loss=params["stop_loss"],
        )

        net_pnl = net_after_fee(gross_pnl)
        equity *= (1 + net_pnl / 100)
        peak_equity = max(peak_equity, equity)
        drawdown = ((equity - peak_equity) / peak_equity) * 100
        max_drawdown = min(max_drawdown, drawdown)

        trade = {"net_pnl": net_pnl, "exit_reason": exit_reason, "equity": equity, "max_drawdown": max_drawdown}
        if collect_trades:
            trade.update({
                **params,
                "side": side,
                "entry_time": now["datetime"].strftime("%Y-%m-%d %H:%M:%S"),
                "exit_time": exit_time,
                "entry_price": round(entry_price, 4),
                "exit_price": round(exit_price, 4),
                "gross_pnl": round(gross_pnl, 4),
                "net_pnl": round(net_pnl, 4),
                "exit_reason": exit_reason,
                "equity": round(equity, 4),
                "max_drawdown": round(max_drawdown, 4),
                "ha_open": round(now["ha_open"], 4),
                "ha_close": round(now["ha_close"], 4),
                "ha_high": round(now["ha_high"], 4),
                "ha_low": round(now["ha_low"], 4),
                "ema200_real": round(now["ema200_real"], 4) if not pd.isna(now["ema200_real"]) else "",
                "stoch_k": round(now["stoch_k"], 4) if not pd.isna(now["stoch_k"]) else "",
                "stoch_d": round(now["stoch_d"], 4) if not pd.isna(now["stoch_d"]) else "",
            })
        trades.append(trade)

        exit_matches = df.index[df["datetime"].dt.strftime("%Y-%m-%d %H:%M:%S") == exit_time].tolist()
        if exit_matches:
            i = int(exit_matches[0]) + 1
        else:
            i += 1

    trades_df = pd.DataFrame(trades)
    if trades_df.empty:
        return {**params, "trades": 0, "win_rate": 0, "total_return": 0, "max_drawdown": 0, "avg_win": 0,
                "avg_loss": 0, "profit_factor": 0, "tp_count": 0, "sl_count": 0,
                "same_candle_sl_count": 0, "time_exit_count": 0}, trades_df

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
    profit_factor = 999.0 if gross_loss == 0 else abs(gross_profit / gross_loss)

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
        "time_exit_count": int(exit_counts.get("TIME_EXIT", 0)),
    }, trades_df


def score_rank(row):
    score = 0.0
    pf = min(float(row["profit_factor"]), 10.0)
    score += pf * 120
    score += float(row["total_return"]) * 2.0
    score += float(row["win_rate"]) * 0.6
    score += float(row["max_drawdown"]) * 4
    trades = int(row["trades"])
    if trades < MIN_TRADES:
        score -= 250
    elif trades > 800:
        score -= 60
    else:
        score += 80
    if float(row["profit_factor"]) < 1:
        score -= 150
    if float(row["total_return"]) < 0:
        score -= 120
    if float(row["max_drawdown"]) < -30:
        score -= 120
    return round(score, 4)


def main():
    print("Heikin Ashi Stoch RSI Backtest started:", now_kst(), flush=True)
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
        interval_name = params["interval"]
        cache_key = f"{symbol}_{interval_name}"
        if cache_key not in data_cache:
            data_cache[cache_key] = load_data(symbol, interval_name)
        df = data_cache[cache_key]
        if df.empty or len(df) < 300:
            continue
        stats, _ = backtest_params(df, params, collect_trades=False)
        stats["rank_score"] = score_rank(stats)
        stats["run_time"] = now_kst()
        rows.append(stats)
        if idx % 100 == 0:
            print(f"Progress: {idx}/{len(combos)}", flush=True)
            append_run_log(log_ws, f"Progress: {idx}/{len(combos)}")

        # 중간 저장: Railway 재시작/크레딧 부족/마지막 저장 실패 대비
        if idx % 300 == 0:
            temp_df = pd.DataFrame(rows)

            if not temp_df.empty:
                temp_df = temp_df.replace([float("inf"), float("-inf")], "").fillna("")
                temp_df = temp_df.sort_values(
                    by=["rank_score", "profit_factor", "total_return"],
                    ascending=False,
                )

                temp_save_df = temp_df.head(500)

                clear_and_write(
                    result_ws,
                    list(temp_save_df.columns),
                    temp_save_df.astype(str).values.tolist(),
                )

                clear_and_write(
                    top_ws,
                    list(temp_df.head(20).columns),
                    temp_df.head(20).astype(str).values.tolist(),
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
    results_df = results_df.sort_values(by=["rank_score", "profit_factor", "total_return"], ascending=False)

    # Google Sheet 저장량 제한:
    # 전체 결과 대신 상위 1000개만 저장해서 마지막 저장 에러 방지
    save_results_df = results_df.head(500)
    top20_df = results_df.head(20)

    best_params = top20_df.iloc[0][keys].to_dict()
    for k in ["take_profit", "stop_loss", "wick_tolerance", "min_ema_distance_pct"]:
        best_params[k] = float(best_params[k])

    best_key = f"{best_params['symbol']}_{best_params['interval']}"
    best_df = data_cache.get(best_key)
    if best_df is None:
        best_df = load_data(best_params["symbol"], best_params["interval"])
    _, best_trades = backtest_params(best_df, best_params, collect_trades=True)
    best_trades = best_trades.replace([float("inf"), float("-inf")], "").fillna("")

    clear_and_write(result_ws, list(save_results_df.columns), save_results_df.astype(str).values.tolist())
    clear_and_write(top_ws, list(top20_df.columns), top20_df.astype(str).values.tolist())
    if not best_trades.empty:
        best_trades = best_trades.head(500)
        clear_and_write(trades_ws, list(best_trades.columns), best_trades.astype(str).values.tolist())
    else:
        clear_and_write(trades_ws, ["message"], [["No trades"]])

    append_run_log(log_ws, "Backtest finished")
    print("Heikin Ashi Stoch RSI Backtest finished:", now_kst(), flush=True)
    print("Saved result to:", RESULT_SHEET_NAME, flush=True)
    print("Saved top20 to:", TOP_SHEET_NAME, flush=True)
    print("Saved best trades to:", TRADES_SHEET_NAME, flush=True)


if __name__ == "__main__":
    main()
