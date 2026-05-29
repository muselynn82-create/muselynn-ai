import os
import time
from datetime import datetime, timezone
from itertools import product
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

START_DATE = "2022-01-01"
END_DATE = "2026-05-25"

FEE_ROUND_TRIP = 0.20
KST = ZoneInfo("Asia/Seoul")

GOOGLE_CLIENT_EMAIL = os.getenv("GOOGLE_CLIENT_EMAIL")
GOOGLE_PRIVATE_KEY = os.getenv("GOOGLE_PRIVATE_KEY", "").replace("\\n", "\n")
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME")

RESULT_SHEET_NAME = "BIG_CANDLE_RESULTS"
TOP_SHEET_NAME = "BIG_CANDLE_TOP20"
TRADES_SHEET_NAME = "BIG_CANDLE_TRADES"
RUN_LOG_SHEET_NAME = "BIG_CANDLE_RUNLOG"

CACHE_PREFIX = "btc_big_candle"

# 5개 이상 조정봉 후 강한 양봉이 몸통 기준으로 모두 상회하는 패턴
PARAM_GRID = {
    "interval": ["1d", "1w", "1M"],

    # 조정/횡보 캔들 개수
    "lookback_bars": [5, 6, 8, 10],

    # 손익비
    "risk_reward": [1.5, 2.0, 2.5, 3.0],

    # 돌파 기준
    # BODY: 돌파봉 종가가 직전 lookback 봉들의 몸통 상단을 모두 상회
    # HIGH: 돌파봉 종가가 직전 lookback 봉들의 고가까지 상회
    "breakout_mode": ["BODY", "HIGH"],

    # 직전 구간이 실제 조정/횡보인지 보는 기준
    # 0.0이면 조건 없음
    # 예: 2.0이면 직전 lookback 구간 상승률이 2% 이하일 때만 조정으로 인정
    "max_prior_return_pct": [0.0, 2.0, 5.0],

    # 돌파봉 몸통 강도: body/range
    "min_body_ratio": [0.5, 0.6, 0.7],

    # 돌파봉 거래량 필터
    "min_volume_ratio": [0.0, 1.0, 1.3],
}

MIN_TRADES = 3


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


def get_or_create_ws(spreadsheet, title, rows=5000, cols=60):
    try:
        return spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        return spreadsheet.add_worksheet(title=title, rows=rows, cols=cols)


def sanitize_for_sheet(value):
    if pd.isna(value):
        return ""
    if isinstance(value, float):
        if value == float("inf") or value == float("-inf"):
            return ""
    return value


def clear_and_write(ws, headers, rows):
    ws.clear()

    safe_headers = [sanitize_for_sheet(v) for v in headers]
    safe_rows = [
        [sanitize_for_sheet(v) for v in row]
        for row in rows
    ]

    values = [safe_headers] + safe_rows

    if values:
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
    df["body_top"] = df[["open", "close"]].max(axis=1)
    df["body_bottom"] = df[["open", "close"]].min(axis=1)
    df["range"] = df["high"] - df["low"]
    df["body"] = (df["close"] - df["open"]).abs()
    df["body_ratio"] = df["body"] / df["range"].replace(0, pd.NA)
    df["volume_ma"] = df["volume"].rolling(20).mean()
    df["volume_ratio"] = df["volume"] / df["volume_ma"]

    return df


def load_data(interval):
    cache_file = f"{CACHE_PREFIX}_{interval}.pkl"

    if os.path.exists(cache_file):
        print(f"Loading cached {interval}: {cache_file}", flush=True)
        return pd.read_pickle(cache_file)

    start_dt = datetime.strptime(START_DATE, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end_dt = datetime.strptime(END_DATE, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    df = fetch_klines(SYMBOL, interval, start_dt, end_dt)
    df.to_pickle(cache_file)
    return df


# =========================
# BACKTEST
# =========================

def net_after_fee(gross_pnl):
    return ((1 + gross_pnl / 100) * (1 - FEE_ROUND_TRIP / 100) - 1) * 100


def simulate_long(df, entry_idx, entry_price, stop_price, target_price):
    for j in range(entry_idx + 1, len(df)):
        row = df.iloc[j]
        low = row["low"]
        high = row["high"]
        exit_time = row["datetime"].strftime("%Y-%m-%d %H:%M:%S")

        hit_sl = low <= stop_price
        hit_tp = high >= target_price

        # 같은 봉에서 TP/SL 둘 다 닿으면 보수적으로 SL 우선
        if hit_sl and hit_tp:
            gross_pnl = ((stop_price - entry_price) / entry_price) * 100
            return exit_time, stop_price, "STOP_LOSS_SAME_CANDLE", gross_pnl

        if hit_sl:
            gross_pnl = ((stop_price - entry_price) / entry_price) * 100
            return exit_time, stop_price, "STOP_LOSS", gross_pnl

        if hit_tp:
            gross_pnl = ((target_price - entry_price) / entry_price) * 100
            return exit_time, target_price, "TAKE_PROFIT", gross_pnl

    row = df.iloc[-1]
    exit_price = row["close"]
    exit_time = row["datetime"].strftime("%Y-%m-%d %H:%M:%S")
    gross_pnl = ((exit_price - entry_price) / entry_price) * 100
    return exit_time, exit_price, "TIME_EXIT", gross_pnl


def is_setup(df, i, params):
    lb = int(params["lookback_bars"])
    now = df.iloc[i]
    prev = df.iloc[i - lb:i]

    # 강한 양봉
    if now["close"] <= now["open"]:
        return False

    if pd.isna(now["body_ratio"]) or now["body_ratio"] < params["min_body_ratio"]:
        return False

    if params["min_volume_ratio"] > 0:
        if pd.isna(now["volume_ratio"]) or now["volume_ratio"] < params["min_volume_ratio"]:
            return False

    # 직전 구간: 5개 이상 조정/횡보
    if params["max_prior_return_pct"] > 0:
        prior_return = ((prev["close"].iloc[-1] - prev["close"].iloc[0]) / prev["close"].iloc[0]) * 100
        if prior_return > params["max_prior_return_pct"]:
            return False

    if params["breakout_mode"] == "BODY":
        breakout_level = prev["body_top"].max()
        return now["close"] > breakout_level

    if params["breakout_mode"] == "HIGH":
        breakout_level = prev["high"].max()
        return now["close"] > breakout_level

    return False


def backtest_params(df, params, collect_trades=False):
    trades = []
    equity = 100.0
    peak_equity = 100.0
    max_drawdown = 0.0

    lb = int(params["lookback_bars"])

    i = lb
    while i < len(df):
        if not is_setup(df, i, params):
            i += 1
            continue

        now = df.iloc[i]
        entry_price = now["close"]
        stop_price = now["low"]
        risk = entry_price - stop_price

        if risk <= 0:
            i += 1
            continue

        target_price = entry_price + risk * params["risk_reward"]

        exit_time, exit_price, exit_reason, gross_pnl = simulate_long(
            df, i, entry_price, stop_price, target_price
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
                "entry_time": now["datetime"].strftime("%Y-%m-%d %H:%M:%S"),
                "exit_time": exit_time,
                "entry_price": round(entry_price, 2),
                "stop_price": round(stop_price, 2),
                "target_price": round(target_price, 2),
                "exit_price": round(exit_price, 2),
                "gross_pnl": round(gross_pnl, 4),
                "net_pnl": round(net_pnl, 4),
                "exit_reason": exit_reason,
                "equity": round(equity, 4),
                "max_drawdown": round(max_drawdown, 4),
                "body_ratio": round(now["body_ratio"], 4),
                "volume_ratio": round(now["volume_ratio"], 4) if not pd.isna(now["volume_ratio"]) else 0,
            })

        trades.append(trade)

        # 포지션 종료 시점 다음 캔들부터 재탐색
        exit_idx_candidates = df.index[df["datetime"].dt.strftime("%Y-%m-%d %H:%M:%S") == exit_time].tolist()
        if exit_idx_candidates:
            i = int(exit_idx_candidates[0]) + 1
        else:
            i += 1

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
    score += row["profit_factor"] * 120
    score += row["total_return"] * 2
    score += row["win_rate"] * 0.7
    score += row["max_drawdown"] * 3

    if row["trades"] < MIN_TRADES:
        score -= 120

    if row["profit_factor"] < 1:
        score -= 120

    if row["total_return"] < 0:
        score -= 80

    return round(score, 4)


# =========================
# MAIN
# =========================

def main():
    print("Big Candle Breakout Backtest started:", now_kst(), flush=True)

    spreadsheet = init_gspread()
    result_ws = get_or_create_ws(spreadsheet, RESULT_SHEET_NAME, rows=10000, cols=60)
    top_ws = get_or_create_ws(spreadsheet, TOP_SHEET_NAME, rows=100, cols=60)
    trades_ws = get_or_create_ws(spreadsheet, TRADES_SHEET_NAME, rows=5000, cols=60)
    log_ws = get_or_create_ws(spreadsheet, RUN_LOG_SHEET_NAME, rows=1000, cols=5)

    append_run_log(log_ws, "Backtest started")

    keys = list(PARAM_GRID.keys())
    combos = list(product(*[PARAM_GRID[k] for k in keys]))

    rows = []
    data_cache = {}

    print(f"Total combinations: {len(combos)}", flush=True)
    append_run_log(log_ws, f"Total combinations: {len(combos)}")

    for idx, values in enumerate(combos, start=1):
        params = dict(zip(keys, values))
        interval = params["interval"]

        if interval not in data_cache:
            data_cache[interval] = load_data(interval)

        df = data_cache[interval]
        stats, _ = backtest_params(df, params, collect_trades=False)
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
    best_params["lookback_bars"] = int(best_params["lookback_bars"])
    for k in ["risk_reward", "max_prior_return_pct", "min_body_ratio", "min_volume_ratio"]:
        best_params[k] = float(best_params[k])

    if best_params["interval"] in data_cache:
        best_df = data_cache[best_params["interval"]]
    else:
        best_df = load_data(best_params["interval"])
    _, best_trades = backtest_params(best_df, best_params, collect_trades=True)

    clear_and_write(result_ws, list(results_df.columns), results_df.replace([float("inf"), float("-inf")], "").fillna("").astype(str).values.tolist())
    clear_and_write(top_ws, list(top20_df.columns), top20_df.replace([float("inf"), float("-inf")], "").fillna("").astype(str).values.tolist())

    if not best_trades.empty:
        clear_and_write(trades_ws, list(best_trades.columns), best_trades.replace([float("inf"), float("-inf")], "").fillna("").astype(str).values.tolist())
    else:
        clear_and_write(trades_ws, ["message"], [["No trades"]])

    append_run_log(log_ws, "Backtest finished")
    print("Big Candle Breakout Backtest finished:", now_kst(), flush=True)
    print("Saved result to:", RESULT_SHEET_NAME, flush=True)
    print("Saved top20 to:", TOP_SHEET_NAME, flush=True)
    print("Saved best trades to:", TRADES_SHEET_NAME, flush=True)


if __name__ == "__main__":
    main()
