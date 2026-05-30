import os
import time
import math
from datetime import datetime, timezone
from itertools import product
from zoneinfo import ZoneInfo

import pandas as pd
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from binance.client import Client


# ============================================================
# PARKER BROOKS STYLE LIQUIDITY SWEEP / FAKEOUT BACKTEST
# ------------------------------------------------------------
# 핵심:
# - Previous High / Previous Low 기준
# - 저점 하향 스윕 후 종가가 다시 위로 회복하면 LONG
# - 고점 상향 스윕 후 종가가 다시 아래로 회복하면 SHORT
# - 손절: 스윕 캔들의 저점/고점 바깥
# - 익절: RR 또는 박스 반대편
# - 로그는 Railway 콘솔만 출력
# - 캐시 깨짐 자동 삭제
# ============================================================

API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_SECRET_KEY")
client = Client(API_KEY, API_SECRET, requests_params={"timeout": 20})

KST = ZoneInfo("Asia/Seoul")

START_DATE = "2022-01-01"
END_DATE = "2026-05-25"

FEE_ROUND_TRIP = 0.20

GOOGLE_CLIENT_EMAIL = os.getenv("GOOGLE_CLIENT_EMAIL")
GOOGLE_PRIVATE_KEY = os.getenv("GOOGLE_PRIVATE_KEY", "").replace("\\n", "\n")
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME")

RESULT_SHEET_NAME = "PB_FAKEOUT_RESULTS"
TOP_SHEET_NAME = "PB_FAKEOUT_TOP20"
TRADES_SHEET_NAME = "PB_FAKEOUT_TRADES"
RUN_LOG_SHEET_NAME = "PB_FAKEOUT_RUNLOG"

CACHE_PREFIX = "pb_fakeout_2022_2026"

SYMBOLS = [
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "XRPUSDT",
]

INTERVAL_MAP = {
    "15m": Client.KLINE_INTERVAL_15MINUTE,
}

PARAM_GRID = {
    "symbol": ["BTCUSDT"],
    "interval": ["15m"],

    "lookback_bars": [48, 96],
    "direction_mode": ["BOTH"],
    "risk_reward": [2.0, 3.0],
    "stop_buffer_pct": [0.05],
    "min_sweep_pct": [0.05, 0.10],
    "min_reclaim_body_ratio": [0.0],
    "min_range_pct": [0.20],
    "max_range_pct": [3.5],
    "min_volume_ratio": [0.0],
    "target_mode": ["RR"],
    "max_hold_bars": [24],
    "cooldown_bars": [12],
}

MIN_TRADES = 30


def now_kst():
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")


def dt_to_ms(dt):
    return int(dt.timestamp() * 1000)


def net_after_fee(gross_pnl):
    return ((1 + gross_pnl / 100) * (1 - FEE_ROUND_TRIP / 100) - 1) * 100


def calc_cagr(total_return_pct):
    start_dt = datetime.strptime(START_DATE, "%Y-%m-%d")
    end_dt = datetime.strptime(END_DATE, "%Y-%m-%d")
    years = max((end_dt - start_dt).days / 365.25, 0.01)
    try:
        final_equity = 1 + float(total_return_pct) / 100
        if final_equity <= 0:
            return -100.0
        return round(((final_equity ** (1 / years)) - 1) * 100, 2)
    except Exception:
        return 0.0


def append_run_log(ws, message):
    print(f"[RUNLOG] {now_kst()} {message}", flush=True)


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


def get_or_create_ws(spreadsheet, title, rows=1000, cols=60):
    try:
        return spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        return spreadsheet.add_worksheet(title=title, rows=rows, cols=cols)


def clear_and_write(ws, headers, rows):
    safe_headers = [sanitize_for_sheet(v) for v in headers]
    safe_rows = [[sanitize_for_sheet(v) for v in row] for row in rows]
    values = [safe_headers] + safe_rows
    ws.clear()
    if not values:
        return
    need_rows = max(len(values), 1)
    need_cols = max(len(safe_headers), 1)
    try:
        ws.resize(rows=need_rows, cols=need_cols)
    except Exception as e:
        print(f"Worksheet resize skipped: {e}", flush=True)
    ws.update(range_name="A1", values=values)


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
        raise RuntimeError(f"No data downloaded for {symbol} {interval}")
    df = df.drop_duplicates(subset=["time"]).reset_index(drop=True)
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    df["datetime"] = pd.to_datetime(df["time"], unit="ms", utc=True).dt.tz_convert("Asia/Seoul")
    print(f"Finished downloading {symbol} {interval}: {len(df)} candles", flush=True)
    return add_indicators(df)


def add_indicators(df):
    df = df.copy()
    df["range"] = df["high"] - df["low"]
    df["body"] = (df["close"] - df["open"]).abs()
    df["body_ratio"] = df["body"] / df["range"].replace(0, pd.NA)
    df["upper_wick"] = df["high"] - df[["open", "close"]].max(axis=1)
    df["lower_wick"] = df[["open", "close"]].min(axis=1) - df["low"]
    df["upper_wick_ratio"] = df["upper_wick"] / df["range"].replace(0, pd.NA)
    df["lower_wick_ratio"] = df["lower_wick"] / df["range"].replace(0, pd.NA)
    df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()
    df["ema200"] = df["close"].ewm(span=200, adjust=False).mean()
    df["volume_ma"] = df["volume"].rolling(20).mean()
    df["volume_ratio"] = df["volume"] / df["volume_ma"]
    return df


def cache_name(symbol, interval_label):
    return f"{CACHE_PREFIX}_{symbol}_{interval_label}.pkl"


def load_or_fetch(symbol, interval_label, interval):
    start_dt = datetime.strptime(START_DATE, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end_dt = datetime.strptime(END_DATE, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    cache_file = cache_name(symbol, interval_label)
    if os.path.exists(cache_file):
        try:
            print(f"Loading cached data: {cache_file}", flush=True)
            df = pd.read_pickle(cache_file)
            required_cols = ["body_ratio", "upper_wick_ratio", "lower_wick_ratio", "volume_ratio", "ema200"]
            if not all(col in df.columns for col in required_cols):
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
    df.to_pickle(cache_file)
    return df


def allowed_direction(params, side):
    mode = params["direction_mode"]
    if mode == "BOTH":
        return True
    if mode == "LONG_ONLY":
        return side == "LONG"
    if mode == "SHORT_ONLY":
        return side == "SHORT"
    return False


def build_target(side, entry_price, stop_price, prev_high, prev_low, params):
    risk = abs(entry_price - stop_price)
    if risk <= 0:
        return None
    if params["target_mode"] == "RR":
        if side == "LONG":
            return entry_price + risk * params["risk_reward"]
        return entry_price - risk * params["risk_reward"]
    if side == "LONG":
        target = prev_high
        rr_actual = (target - entry_price) / risk
        if target <= entry_price or rr_actual < params["risk_reward"]:
            return None
        return target
    target = prev_low
    rr_actual = (entry_price - target) / risk
    if target >= entry_price or rr_actual < params["risk_reward"]:
        return None
    return target


def simulate_trade(df, entry_idx, side, entry_price, stop_price, target_price, max_hold_bars):
    last_idx = min(len(df) - 1, entry_idx + int(max_hold_bars))
    for j in range(entry_idx + 1, last_idx + 1):
        row = df.iloc[j]
        high = row["high"]
        low = row["low"]
        exit_time = row["datetime"].strftime("%Y-%m-%d %H:%M:%S")
        if side == "LONG":
            hit_sl = low <= stop_price
            hit_tp = high >= target_price
            if hit_sl and hit_tp:
                gross_pnl = ((stop_price - entry_price) / entry_price) * 100
                return j, exit_time, stop_price, "STOP_LOSS_SAME_CANDLE", gross_pnl
            if hit_sl:
                gross_pnl = ((stop_price - entry_price) / entry_price) * 100
                return j, exit_time, stop_price, "STOP_LOSS", gross_pnl
            if hit_tp:
                gross_pnl = ((target_price - entry_price) / entry_price) * 100
                return j, exit_time, target_price, "TAKE_PROFIT", gross_pnl
        else:
            hit_sl = high >= stop_price
            hit_tp = low <= target_price
            if hit_sl and hit_tp:
                gross_pnl = ((entry_price - stop_price) / entry_price) * 100
                return j, exit_time, stop_price, "STOP_LOSS_SAME_CANDLE", gross_pnl
            if hit_sl:
                gross_pnl = ((entry_price - stop_price) / entry_price) * 100
                return j, exit_time, stop_price, "STOP_LOSS", gross_pnl
            if hit_tp:
                gross_pnl = ((entry_price - target_price) / entry_price) * 100
                return j, exit_time, target_price, "TAKE_PROFIT", gross_pnl
    row = df.iloc[last_idx]
    exit_price = row["close"]
    exit_time = row["datetime"].strftime("%Y-%m-%d %H:%M:%S")
    if side == "LONG":
        gross_pnl = ((exit_price - entry_price) / entry_price) * 100
    else:
        gross_pnl = ((entry_price - exit_price) / entry_price) * 100
    return last_idx, exit_time, exit_price, "TIME_EXIT", gross_pnl


def detect_fakeout_setup(df, i, params):
    lb = int(params["lookback_bars"])
    row = df.iloc[i]
    prev = df.iloc[i - lb:i]
    prev_high = float(prev["high"].max())
    prev_low = float(prev["low"].min())
    if prev_high <= prev_low:
        return None
    prev_range_pct = ((prev_high - prev_low) / row["close"]) * 100
    if prev_range_pct < params["min_range_pct"] or prev_range_pct > params["max_range_pct"]:
        return None
    if params["min_volume_ratio"] > 0:
        if pd.isna(row["volume_ratio"]) or row["volume_ratio"] < params["min_volume_ratio"]:
            return None
    if pd.isna(row["body_ratio"]) or row["body_ratio"] < params["min_reclaim_body_ratio"]:
        return None

    setups = []
    sweep_depth_long = ((prev_low - row["low"]) / prev_low) * 100 if prev_low > 0 else 0
    if (
        allowed_direction(params, "LONG")
        and row["low"] < prev_low
        and row["close"] > prev_low
        and sweep_depth_long >= params["min_sweep_pct"]
    ):
        stop_price = row["low"] * (1 - params["stop_buffer_pct"] / 100)
        entry_price = row["close"]
        target_price = build_target("LONG", entry_price, stop_price, prev_high, prev_low, params)
        if target_price is not None and target_price > entry_price:
            setups.append({
                "side": "LONG",
                "entry_price": entry_price,
                "stop_price": stop_price,
                "target_price": target_price,
                "prev_high": prev_high,
                "prev_low": prev_low,
                "sweep_depth_pct": sweep_depth_long,
                "prev_range_pct": prev_range_pct,
            })

    sweep_depth_short = ((row["high"] - prev_high) / prev_high) * 100 if prev_high > 0 else 0
    if (
        allowed_direction(params, "SHORT")
        and row["high"] > prev_high
        and row["close"] < prev_high
        and sweep_depth_short >= params["min_sweep_pct"]
    ):
        stop_price = row["high"] * (1 + params["stop_buffer_pct"] / 100)
        entry_price = row["close"]
        target_price = build_target("SHORT", entry_price, stop_price, prev_high, prev_low, params)
        if target_price is not None and target_price < entry_price:
            setups.append({
                "side": "SHORT",
                "entry_price": entry_price,
                "stop_price": stop_price,
                "target_price": target_price,
                "prev_high": prev_high,
                "prev_low": prev_low,
                "sweep_depth_pct": sweep_depth_short,
                "prev_range_pct": prev_range_pct,
            })
    if not setups:
        return None
    setups = sorted(setups, key=lambda x: x["sweep_depth_pct"], reverse=True)
    return setups[0]


def backtest_params(df, params, collect_trades=False):
    trades = []
    equity = 100.0
    peak_equity = 100.0
    max_drawdown = 0.0
    lb = int(params["lookback_bars"])
    i = lb
    cooldown_until = -1
    while i < len(df):
        if i < cooldown_until:
            i += 1
            continue
        setup = detect_fakeout_setup(df, i, params)
        if setup is None:
            i += 1
            continue
        side = setup["side"]
        exit_idx, exit_time, exit_price, exit_reason, gross_pnl = simulate_trade(
            df=df,
            entry_idx=i,
            side=side,
            entry_price=setup["entry_price"],
            stop_price=setup["stop_price"],
            target_price=setup["target_price"],
            max_hold_bars=params["max_hold_bars"],
        )
        net_pnl = net_after_fee(gross_pnl)
        equity *= (1 + net_pnl / 100)
        peak_equity = max(peak_equity, equity)
        dd = ((equity - peak_equity) / peak_equity) * 100
        max_drawdown = min(max_drawdown, dd)
        trade = {"net_pnl": net_pnl, "exit_reason": exit_reason, "equity": equity, "max_drawdown": max_drawdown}
        if collect_trades:
            row = df.iloc[i]
            trade.update({
                **params,
                "side": side,
                "entry_time": row["datetime"].strftime("%Y-%m-%d %H:%M:%S"),
                "exit_time": exit_time,
                "entry_price": round(setup["entry_price"], 4),
                "stop_price": round(setup["stop_price"], 4),
                "target_price": round(setup["target_price"], 4),
                "exit_price": round(exit_price, 4),
                "gross_pnl": round(gross_pnl, 4),
                "net_pnl": round(net_pnl, 4),
                "exit_reason": exit_reason,
                "equity": round(equity, 4),
                "max_drawdown": round(max_drawdown, 4),
                "prev_high": round(setup["prev_high"], 4),
                "prev_low": round(setup["prev_low"], 4),
                "sweep_depth_pct": round(setup["sweep_depth_pct"], 4),
                "prev_range_pct": round(setup["prev_range_pct"], 4),
                "body_ratio": round(row["body_ratio"], 4) if not pd.isna(row["body_ratio"]) else "",
                "upper_wick_ratio": round(row["upper_wick_ratio"], 4) if not pd.isna(row["upper_wick_ratio"]) else "",
                "lower_wick_ratio": round(row["lower_wick_ratio"], 4) if not pd.isna(row["lower_wick_ratio"]) else "",
                "volume_ratio": round(row["volume_ratio"], 4) if not pd.isna(row["volume_ratio"]) else "",
            })
        trades.append(trade)
        cooldown_until = int(exit_idx) + int(params["cooldown_bars"])
        i = int(exit_idx) + 1

    trades_df = pd.DataFrame(trades)
    if trades_df.empty:
        return {
            **params,
            "trades": 0,
            "win_rate": 0,
            "total_return": 0,
            "cagr": 0,
            "max_drawdown": 0,
            "avg_win": 0,
            "avg_loss": 0,
            "profit_factor": 0,
            "tp_count": 0,
            "sl_count": 0,
            "same_candle_sl_count": 0,
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
        "cagr": calc_cagr(total_return),
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
    trades = int(row["trades"])
    total_return = float(row["total_return"])
    cagr = float(row["cagr"])
    max_dd = float(row["max_drawdown"])
    win_rate = float(row["win_rate"])
    score += pf * 120
    score += total_return * 1.5
    score += cagr * 5
    score += win_rate * 0.5
    score += max_dd * 5
    if trades < MIN_TRADES:
        score -= 250
    elif trades > 800:
        score -= 80
    else:
        score += 80
    if pf < 1:
        score -= 150
    if total_return < 0:
        score -= 120
    if max_dd < -25:
        score -= 150
    return round(score, 4)


def main():
    print("PB Fakeout Liquidity Sweep Backtest started:", now_kst(), flush=True)
    spreadsheet = init_gspread()
    result_ws = get_or_create_ws(spreadsheet, RESULT_SHEET_NAME, rows=1000, cols=60)
    top_ws = get_or_create_ws(spreadsheet, TOP_SHEET_NAME, rows=100, cols=60)
    trades_ws = get_or_create_ws(spreadsheet, TRADES_SHEET_NAME, rows=1000, cols=60)
    log_ws = get_or_create_ws(spreadsheet, RUN_LOG_SHEET_NAME, rows=100, cols=10)
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
        interval_label = params["interval"]
        interval = INTERVAL_MAP[interval_label]
        cache_key = f"{symbol}_{interval_label}"
        if params["min_range_pct"] >= params["max_range_pct"]:
            continue
        if cache_key not in data_cache:
            data_cache[cache_key] = load_or_fetch(symbol, interval_label, interval)
        df = data_cache[cache_key]
        if df.empty or len(df) < params["lookback_bars"] + params["max_hold_bars"] + 20:
            continue
        stats, _ = backtest_params(df, params, collect_trades=False)
        stats["rank_score"] = score_rank(stats)
        stats["run_time"] = now_kst()
        rows.append(stats)
        if idx % 100 == 0:
            print(f"Progress: {idx}/{len(combos)}", flush=True)
        if idx % 500 == 0:
            temp_df = pd.DataFrame(rows)
            if not temp_df.empty:
                temp_df = temp_df.replace([float("inf"), float("-inf")], "").fillna("")
                temp_df = temp_df.sort_values(by=["rank_score", "profit_factor", "total_return"], ascending=False)
                clear_and_write(result_ws, list(temp_df.head(500).columns), temp_df.head(500).astype(str).values.tolist())
                time.sleep(3)
                clear_and_write(top_ws, list(temp_df.head(20).columns), temp_df.head(20).astype(str).values.tolist())
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
    save_results_df = results_df.head(1000)
    top20_df = results_df.head(20)
    best_params = top20_df.iloc[0][keys].to_dict()
    for k in ["risk_reward", "stop_buffer_pct", "min_sweep_pct", "min_reclaim_body_ratio", "min_range_pct", "max_range_pct", "min_volume_ratio"]:
        best_params[k] = float(best_params[k])
    for k in ["lookback_bars", "max_hold_bars", "cooldown_bars"]:
        best_params[k] = int(best_params[k])
    best_key = f"{best_params['symbol']}_{best_params['interval']}"
    best_df = data_cache.get(best_key)
    if best_df is None:
        best_df = load_or_fetch(best_params["symbol"], best_params["interval"], INTERVAL_MAP[best_params["interval"]])
    _, best_trades = backtest_params(best_df, best_params, collect_trades=True)
    best_trades = best_trades.replace([float("inf"), float("-inf")], "").fillna("")
    clear_and_write(result_ws, list(save_results_df.columns), save_results_df.astype(str).values.tolist())
    time.sleep(3)
    clear_and_write(top_ws, list(top20_df.columns), top20_df.astype(str).values.tolist())
    time.sleep(3)
    if not best_trades.empty:
        best_trades = best_trades.head(1000)
        clear_and_write(trades_ws, list(best_trades.columns), best_trades.astype(str).values.tolist())
    else:
        clear_and_write(trades_ws, ["message"], [["No trades"]])
    append_run_log(log_ws, "Backtest finished")
    print("PB Fakeout Liquidity Sweep Backtest finished:", now_kst(), flush=True)
    print("Saved result to:", RESULT_SHEET_NAME, flush=True)
    print("Saved top20 to:", TOP_SHEET_NAME, flush=True)
    print("Saved best trades to:", TRADES_SHEET_NAME, flush=True)


if __name__ == "__main__":
    main()
