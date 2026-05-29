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
# BIG CANDLE BREAKOUT v2
# 목적:
# - 기존 BIG_CANDLE의 표본 부족 / PF999 과대평가 문제 수정
# - BTC만이 아니라 바이낸스 주요 코인 여러 개 동시 검증
# - 월봉/주봉/일봉을 따로 보고, 랭킹은 거래수 부족 패널티 반영
# - 중간 미실현 MDD도 반영
# - 같은 봉 TP/SL 동시 터치 시 보수적으로 SL 처리
# ============================================================


# =========================
# CONFIG
# =========================

API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_SECRET_KEY")
client = Client(API_KEY, API_SECRET)

START_DATE = "2017-08-17"
END_DATE = "2026-05-25"

FEE_ROUND_TRIP = 0.20
KST = ZoneInfo("Asia/Seoul")

GOOGLE_CLIENT_EMAIL = os.getenv("GOOGLE_CLIENT_EMAIL")
GOOGLE_PRIVATE_KEY = os.getenv("GOOGLE_PRIVATE_KEY", "").replace("\\n", "\n")
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME")

RESULT_SHEET_NAME = "BIG_CANDLE_V2_RESULTS"
TOP_SHEET_NAME = "BIG_CANDLE_V2_TOP20"
TRADES_SHEET_NAME = "BIG_CANDLE_V2_TRADES"
RUN_LOG_SHEET_NAME = "BIG_CANDLE_V2_RUNLOG"

CACHE_PREFIX = "big_candle_v2"

# 바이낸스에서 받을 수 있는 주요 USDT 현물 코인
# 너무 많이 넣으면 오래 걸리니 1차는 이 정도만 추천
SYMBOLS = [
    "BTCUSDT",
    "ETHUSDT",
    "BNBUSDT",
    "SOLUSDT",
    "XRPUSDT",
    "ADAUSDT",
    "DOGEUSDT",
]

# interval:
# 1d = 일봉, 1w = 주봉, 1M = 월봉
# 월봉/주봉은 거래수가 적으니 신호 참고용, 일봉은 표본 확보용
PARAM_GRID = {
    "symbol": SYMBOLS,
    "interval": ["1d", "1w", "1M"],

    # 5개 이상의 조정/횡보 캔들
    "lookback_bars": [5, 6, 8, 10],

    # 손익비
    "risk_reward": [1.5, 2.0, 2.5, 3.0],

    # BODY: 직전 봉들의 몸통 상단 돌파
    # HIGH: 직전 봉들의 고가 돌파
    "breakout_mode": ["BODY", "HIGH"],

    # 직전 lookback 구간 수익률이 이 값 이하일 때만 조정/횡보 인정
    # 999는 사실상 제한 없음
    "max_prior_return_pct": [2.0, 5.0, 999.0],

    # 돌파봉 몸통 강도 body/range
    "min_body_ratio": [0.5, 0.6, 0.7],

    # 거래량 필터. 0이면 사용 안 함
    "min_volume_ratio": [0.0, 1.0, 1.3],

    # EMA200 위에서만 롱
    "ema200_filter": [False, True],

    # 최소 보유 제한 없이 TP/SL까지 보유
    # 단 너무 오래 열려 있으면 마지막 캔들에서 강제 종료
}

# 거래수 신뢰도 기준
MIN_TRADES_STRONG = 30
MIN_TRADES_WEAK = 10


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


def get_or_create_ws(spreadsheet, title, rows=12000, cols=80):
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

    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return ""

    return value


def clear_and_write(ws, headers, rows):
    ws.clear()

    safe_headers = [sanitize_for_sheet(v) for v in headers]
    safe_rows = [[sanitize_for_sheet(v) for v in row] for row in rows]

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
        return df

    df = df.drop_duplicates(subset=["time"]).reset_index(drop=True)

    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)

    df["datetime"] = pd.to_datetime(df["time"], unit="ms", utc=True).dt.tz_convert("Asia/Seoul")

    return add_indicators(df)


def add_indicators(df):
    df = df.copy()

    df["body_top"] = df[["open", "close"]].max(axis=1)
    df["body_bottom"] = df[["open", "close"]].min(axis=1)
    df["range"] = df["high"] - df["low"]
    df["body"] = (df["close"] - df["open"]).abs()
    df["body_ratio"] = df["body"] / df["range"].replace(0, pd.NA)

    df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()
    df["ema200"] = df["close"].ewm(span=200, adjust=False).mean()

    df["volume_ma"] = df["volume"].rolling(20).mean()
    df["volume_ratio"] = df["volume"] / df["volume_ma"]

    return df


def load_data(symbol, interval):
    cache_file = f"{CACHE_PREFIX}_{symbol}_{interval}.pkl"

    if os.path.exists(cache_file):
        print(f"Loading cached {symbol} {interval}: {cache_file}", flush=True)
        df = pd.read_pickle(cache_file)
        required_cols = ["body_ratio", "ema200", "volume_ratio"]
        if all(col in df.columns for col in required_cols):
            return df
        df = add_indicators(df)
        df.to_pickle(cache_file)
        return df

    start_dt = datetime.strptime(START_DATE, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end_dt = datetime.strptime(END_DATE, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    df = fetch_klines(symbol, interval, start_dt, end_dt)

    if df.empty:
        print(f"No data: {symbol} {interval}", flush=True)
        return df

    df.to_pickle(cache_file)
    return df


# =========================
# BACKTEST
# =========================

def net_after_fee(gross_pnl):
    return ((1 + gross_pnl / 100) * (1 - FEE_ROUND_TRIP / 100) - 1) * 100


def is_setup(df, i, params):
    lb = int(params["lookback_bars"])
    now = df.iloc[i]
    prev = df.iloc[i - lb:i]

    # 강한 양봉
    if now["close"] <= now["open"]:
        return False

    if pd.isna(now["body_ratio"]) or now["body_ratio"] < params["min_body_ratio"]:
        return False

    # 거래량 필터
    if params["min_volume_ratio"] > 0:
        if pd.isna(now["volume_ratio"]) or now["volume_ratio"] < params["min_volume_ratio"]:
            return False

    # EMA200 위에서만 롱
    if params["ema200_filter"]:
        if pd.isna(now["ema200"]) or now["close"] <= now["ema200"]:
            return False

    # 직전 구간이 조정/횡보였는지
    max_prior = params["max_prior_return_pct"]
    if max_prior < 999:
        prior_return = ((prev["close"].iloc[-1] - prev["close"].iloc[0]) / prev["close"].iloc[0]) * 100
        if prior_return > max_prior:
            return False

    # 돌파 기준
    if params["breakout_mode"] == "BODY":
        breakout_level = prev["body_top"].max()
        return now["close"] > breakout_level

    if params["breakout_mode"] == "HIGH":
        breakout_level = prev["high"].max()
        return now["close"] > breakout_level

    return False


def simulate_long_with_unrealized_mdd(df, entry_idx, entry_price, stop_price, target_price, current_equity, peak_equity):
    local_min_equity = current_equity

    for j in range(entry_idx + 1, len(df)):
        row = df.iloc[j]
        low = row["low"]
        high = row["high"]
        exit_time = row["datetime"].strftime("%Y-%m-%d %H:%M:%S")

        # 미실현 손실 기준 MDD 반영
        unrealized_low_pnl = ((low - entry_price) / entry_price) * 100
        unrealized_low_net = net_after_fee(unrealized_low_pnl)
        mark_equity = current_equity * (1 + unrealized_low_net / 100)
        local_min_equity = min(local_min_equity, mark_equity)

        hit_sl = low <= stop_price
        hit_tp = high >= target_price

        # 같은 봉에서 TP/SL 둘 다 터치하면 보수적으로 SL 우선
        if hit_sl and hit_tp:
            gross_pnl = ((stop_price - entry_price) / entry_price) * 100
            return exit_time, stop_price, "STOP_LOSS_SAME_CANDLE", gross_pnl, local_min_equity

        if hit_sl:
            gross_pnl = ((stop_price - entry_price) / entry_price) * 100
            return exit_time, stop_price, "STOP_LOSS", gross_pnl, local_min_equity

        if hit_tp:
            gross_pnl = ((target_price - entry_price) / entry_price) * 100
            return exit_time, target_price, "TAKE_PROFIT", gross_pnl, local_min_equity

    row = df.iloc[-1]
    exit_price = row["close"]
    exit_time = row["datetime"].strftime("%Y-%m-%d %H:%M:%S")
    gross_pnl = ((exit_price - entry_price) / entry_price) * 100

    return exit_time, exit_price, "TIME_EXIT", gross_pnl, local_min_equity


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

        exit_time, exit_price, exit_reason, gross_pnl, local_min_equity = simulate_long_with_unrealized_mdd(
            df=df,
            entry_idx=i,
            entry_price=entry_price,
            stop_price=stop_price,
            target_price=target_price,
            current_equity=equity,
            peak_equity=peak_equity,
        )

        # 미실현 MDD 먼저 반영
        unrealized_dd = ((local_min_equity - peak_equity) / peak_equity) * 100
        max_drawdown = min(max_drawdown, unrealized_dd)

        net_pnl = net_after_fee(gross_pnl)
        equity *= (1 + net_pnl / 100)
        peak_equity = max(peak_equity, equity)

        realized_dd = ((equity - peak_equity) / peak_equity) * 100
        max_drawdown = min(max_drawdown, realized_dd)

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
                "body_ratio": round(now["body_ratio"], 4) if not pd.isna(now["body_ratio"]) else "",
                "volume_ratio": round(now["volume_ratio"], 4) if not pd.isna(now["volume_ratio"]) else "",
                "ema200": round(now["ema200"], 2) if not pd.isna(now["ema200"]) else "",
            })

        trades.append(trade)

        # 같은 포지션 중복 진입 방지: 종료 캔들 이후부터 탐색
        exit_matches = df.index[df["datetime"].dt.strftime("%Y-%m-%d %H:%M:%S") == exit_time].tolist()
        if exit_matches:
            i = int(exit_matches[0]) + 1
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
            "same_candle_sl_count": 0,
            "time_exit_count": 0,
            "trust_grade": "NO_SAMPLE",
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

    if gross_loss == 0:
        profit_factor = 999.0
    else:
        profit_factor = abs(gross_profit / gross_loss)

    if total_trades >= MIN_TRADES_STRONG:
        trust_grade = "STRONG"
    elif total_trades >= MIN_TRADES_WEAK:
        trust_grade = "WEAK"
    else:
        trust_grade = "LOW_SAMPLE"

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
        "trust_grade": trust_grade,
    }, trades_df


def score_rank(row):
    score = 0.0

    pf = min(float(row["profit_factor"]), 10.0)

    score += pf * 100
    score += float(row["total_return"]) * 1.8
    score += float(row["win_rate"]) * 0.5
    score += float(row["max_drawdown"]) * 4

    trades = int(row["trades"])

    # 표본 부족 강한 패널티
    if trades < 3:
        score -= 500
    elif trades < MIN_TRADES_WEAK:
        score -= 250
    elif trades < MIN_TRADES_STRONG:
        score -= 80
    else:
        score += 100

    if float(row["profit_factor"]) < 1:
        score -= 150

    if float(row["total_return"]) < 0:
        score -= 120

    if float(row["max_drawdown"]) < -25:
        score -= 150

    # 월봉/주봉은 참고용이라 과대평가 방지
    if row["interval"] == "1M":
        score -= 150
    elif row["interval"] == "1w":
        score -= 50

    return round(score, 4)


# =========================
# MAIN
# =========================

def main():
    print("Big Candle Breakout V2 started:", now_kst(), flush=True)

    spreadsheet = init_gspread()
    result_ws = get_or_create_ws(spreadsheet, RESULT_SHEET_NAME, rows=20000, cols=80)
    top_ws = get_or_create_ws(spreadsheet, TOP_SHEET_NAME, rows=100, cols=80)
    trades_ws = get_or_create_ws(spreadsheet, TRADES_SHEET_NAME, rows=10000, cols=80)
    log_ws = get_or_create_ws(spreadsheet, RUN_LOG_SHEET_NAME, rows=2000, cols=10)

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
        interval = params["interval"]
        cache_key = f"{symbol}_{interval}"

        if cache_key not in data_cache:
            data_cache[cache_key] = load_data(symbol, interval)

        df = data_cache[cache_key]

        if df.empty or len(df) < params["lookback_bars"] + 10:
            continue

        stats, _ = backtest_params(df, params, collect_trades=False)
        stats["rank_score"] = score_rank(stats)
        stats["run_time"] = now_kst()
        rows.append(stats)

        if idx % 200 == 0:
            print(f"Progress: {idx}/{len(combos)}", flush=True)
            append_run_log(log_ws, f"Progress: {idx}/{len(combos)}")

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

    top20_df = results_df.head(20)

    # 최상위 파라미터 거래내역 저장
    best_params = top20_df.iloc[0][keys].to_dict()
    best_params["lookback_bars"] = int(best_params["lookback_bars"])
    best_params["risk_reward"] = float(best_params["risk_reward"])
    best_params["max_prior_return_pct"] = float(best_params["max_prior_return_pct"])
    best_params["min_body_ratio"] = float(best_params["min_body_ratio"])
    best_params["min_volume_ratio"] = float(best_params["min_volume_ratio"])
    best_params["ema200_filter"] = str(best_params["ema200_filter"]).lower() == "true" or best_params["ema200_filter"] is True

    best_key = f"{best_params['symbol']}_{best_params['interval']}"
    best_df = data_cache.get(best_key)
    if best_df is None:
        best_df = load_data(best_params["symbol"], best_params["interval"])

    _, best_trades = backtest_params(best_df, best_params, collect_trades=True)
    best_trades = best_trades.replace([float("inf"), float("-inf")], "").fillna("")

    clear_and_write(result_ws, list(results_df.columns), results_df.astype(str).values.tolist())
    clear_and_write(top_ws, list(top20_df.columns), top20_df.astype(str).values.tolist())

    if not best_trades.empty:
        clear_and_write(trades_ws, list(best_trades.columns), best_trades.astype(str).values.tolist())
    else:
        clear_and_write(trades_ws, ["message"], [["No trades"]])

    append_run_log(log_ws, "Backtest finished")
    print("Big Candle Breakout V2 finished:", now_kst(), flush=True)
    print("Saved result to:", RESULT_SHEET_NAME, flush=True)
    print("Saved top20 to:", TOP_SHEET_NAME, flush=True)
    print("Saved best trades to:", TRADES_SHEET_NAME, flush=True)


if __name__ == "__main__":
    main()
