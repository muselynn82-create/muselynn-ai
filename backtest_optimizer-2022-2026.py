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

RESULT_SHEET_NAME = "REGIME_ELITE_2022_2026_RESULTS"
TOP_SHEET_NAME = "REGIME_ELITE_2022_2026_TOP20"
TRADES_SHEET_NAME = "REGIME_ELITE_2022_2026_TRADES"
RUN_LOG_SHEET_NAME = "REGIME_ELITE_2022_2026_RUNLOG"

USE_TIME_FILTER = True

# 시장상태별 스위칭 백테스트
# BIG_BULL에서만 ELITE_PULLBACK 진입
# BIG_BEAR / NO_TRADE / BIG_CRASH에서는 신규 진입 차단
PARAM_GRID = {
    "strategy_type": ["REGIME_ELITE_LONG"],

    "entry_score": [70, 75, 80],
    "rsi_limit": [36, 40, 44],
    "take_profit": [1.8, 2.5, 3.5],
    "stop_loss": [-1.0, -1.2, -1.5],
    "trail_start": [1.5, 2.0],
    "trail_back": [0.5, 0.7, 1.0],
}

MIN_TRADES = 12
MAX_DRAWDOWN_LIMIT = -20.0

CACHE_PREFIX = "btc_20220101_20260525"


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


# =========================
# DATA / INDICATORS
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

    df["prev_high_20"] = high.shift(1).rolling(20).max()
    df["ema20_slope"] = (df["ema20"] / df["ema20"].shift(8) - 1) * 100
    df["ema50_slope"] = (df["ema50"] / df["ema50"].shift(8) - 1) * 100

    return df


def load_or_fetch_cached(symbol, interval, start_dt, end_dt, cache_path):
    if os.path.exists(cache_path):
        print(f"Loading cached {cache_path}...", flush=True)
        return pd.read_pickle(cache_path)

    df = calculate_indicators(fetch_klines(symbol, interval, start_dt, end_dt))
    df.to_pickle(cache_path)
    return df


# =========================
# STRATEGY
# =========================

def in_us_time(candle_time):
    if not USE_TIME_FILTER:
        return True
    hour = candle_time.hour
    return (17 <= hour <= 23) or (0 <= hour <= 5)


def detect_big_trend(h1, h4):
    # 고변동/급락 구간은 신규 진입 차단 및 보유 포지션 방어용
    if h1["atr_rate"] > 0.03 or h4["atr_rate"] > 0.055:
        return "BIG_CRASH"

    # 4h + 1h 동시 상승장만 공격
    if (
        h4["close"] > h4["ema200"]
        and h4["ema50"] > h4["ema200"]
        and h4["ema20"] > h4["ema50"]
        and h1["close"] > h1["ema50"]
        and h1["ema20"] > h1["ema50"]
        and h1["ema50_slope"] > -0.05
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


def calculate_score(now, prev, params):
    price = now["close"]
    score = 0

    # REGIME_ELITE_LONG: 상승장 깊은 눌림 후 회복만 진입
    if (
        now["rsi"] < params["rsi_limit"]
        and now["low"] <= now["bb_lower"] * 1.003
        and now["close"] > now["open"]
    ):
        score += 70

    if price > now["ema100"]:
        score += 15

    if now["volume_ratio"] >= 0.85:
        score += 10

    if now["ema20_slope"] > 0:
        score += 15
    else:
        score -= 20

    # 과열 추격 방지
    if now["rsi"] > 68:
        score -= 20

    # 변동성 과열 방지
    if now["atr_rate"] > 0.025:
        score -= 25

    return score


def run_backtest(df_15m, df_1h, df_4h, params, collect_trades=False):
    position_open = False
    entry_price = 0.0
    entry_time = None
    entry_big_trend = None
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
        prev = df_15m.iloc[i - 1]
        current_time = now["datetime"]

        # 확정 마감된 상위 타임프레임 봉만 사용
        while i1 + 1 < len(df_1h_times) and df_1h_times[i1 + 1] <= current_time - timedelta(hours=1):
            i1 += 1

        while i4 + 1 < len(df_4h_times) and df_4h_times[i4 + 1] <= current_time - timedelta(hours=4):
            i4 += 1

        h1 = df_1h.iloc[i1]
        h4 = df_4h.iloc[i4]

        if (
            pd.isna(now["rsi"])
            or pd.isna(now["ema200"])
            or pd.isna(now["prev_high_20"])
            or pd.isna(h1["ema200"])
            or pd.isna(h4["ema200"])
            or pd.isna(h1["ema50_slope"])
        ):
            continue

        big_trend = detect_big_trend(h1, h4)
        price = now["close"]

        # EXIT
        if position_open:
            gross_pnl = ((price - entry_price) / entry_price) * 100
            net_pnl = ((1 + gross_pnl / 100) * (1 - FEE_ROUND_TRIP / 100) - 1) * 100
            max_pnl = max(max_pnl, net_pnl)

            exit_reason = None

            if net_pnl <= params["stop_loss"]:
                exit_reason = "STOP_LOSS"

            elif net_pnl >= params["take_profit"]:
                exit_reason = "TAKE_PROFIT"

            elif (
                net_pnl >= 0.25
                and max_pnl >= params["trail_start"]
                and net_pnl <= max_pnl - params["trail_back"]
            ):
                exit_reason = "TRAILING_STOP"

            elif big_trend in ["BIG_CRASH", "BIG_BEAR"]:
                exit_reason = "REGIME_EXIT"

            if exit_reason:
                equity *= (1 + net_pnl / 100)
                peak_equity = max(peak_equity, equity)
                drawdown = ((equity - peak_equity) / peak_equity) * 100
                max_drawdown = min(max_drawdown, drawdown)

                trades.append({
                    "strategy_type": params["strategy_type"],
                    "entry_time": entry_time,
                    "exit_time": current_time.strftime("%Y-%m-%d %H:%M:%S"),
                    "entry_big_trend": entry_big_trend,
                    "exit_big_trend": big_trend,
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
                entry_big_trend = None
                entry_score = 0
                max_pnl = 0.0
                last_exit_time = current_time

        # ENTRY
        if not position_open:
            if not in_us_time(current_time):
                continue

            if big_trend != "BIG_BULL":
                continue

            if last_exit_time:
                cooldown_minutes = (current_time - last_exit_time).total_seconds() / 60
                if cooldown_minutes < 15:
                    continue

            score = calculate_score(now, prev, params)

            if score >= params["entry_score"]:
                position_open = True
                entry_price = price
                entry_time = current_time.strftime("%Y-%m-%d %H:%M:%S")
                entry_big_trend = big_trend
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
        "regime_exit_count": int(exit_counts.get("REGIME_EXIT", 0)),
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
        "regime_exit_count": 0,
    }


def score_rank(row):
    score = 0
    score += row["profit_factor"] * 120
    score += row["total_return"] * 4
    score += row["win_rate"] * 0.5
    score += row["max_drawdown"] * 5

    if row["trades"] < MIN_TRADES:
        score -= 120
    elif row["trades"] < 20:
        score -= 50
    elif row["trades"] > 100:
        score -= 60

    if row["profit_factor"] < 1:
        score -= 150

    if row["total_return"] < 0:
        score -= 100

    if row["max_drawdown"] < MAX_DRAWDOWN_LIMIT:
        score -= 250

    if 20 <= row["trades"] <= 80:
        score += 40

    return round(score, 4)


# =========================
# MAIN
# =========================

def main():
    print("Regime Elite 2022-2026 Backtest started:", now_kst(), flush=True)

    spreadsheet = init_gspread()
    result_ws = get_or_create_ws(spreadsheet, RESULT_SHEET_NAME, rows=8000, cols=50)
    top_ws = get_or_create_ws(spreadsheet, TOP_SHEET_NAME, rows=100, cols=50)
    trades_ws = get_or_create_ws(spreadsheet, TRADES_SHEET_NAME, rows=3000, cols=40)
    log_ws = get_or_create_ws(spreadsheet, RUN_LOG_SHEET_NAME, rows=1000, cols=5)

    append_run_log(log_ws, "Backtest started")

    start_dt = datetime.strptime(START_DATE, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end_dt = datetime.strptime(END_DATE, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    df_15m = load_or_fetch_cached(
        SYMBOL,
        Client.KLINE_INTERVAL_15MINUTE,
        start_dt,
        end_dt,
        f"{CACHE_PREFIX}_15m.pkl",
    )
    df_1h = load_or_fetch_cached(
        SYMBOL,
        Client.KLINE_INTERVAL_1HOUR,
        start_dt,
        end_dt,
        f"{CACHE_PREFIX}_1h.pkl",
    )
    df_4h = load_or_fetch_cached(
        SYMBOL,
        Client.KLINE_INTERVAL_4HOUR,
        start_dt,
        end_dt,
        f"{CACHE_PREFIX}_4h.pkl",
    )

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

        if idx % 50 == 0:
            print(f"Progress: {idx}/{len(combos)}", flush=True)
            append_run_log(log_ws, f"Progress: {idx}/{len(combos)}")

    results_df = pd.DataFrame(rows)
    results_df = results_df.sort_values(
        by=["rank_score", "profit_factor", "total_return"],
        ascending=False,
    )

    top20_df = results_df.head(20)

    best_params = top20_df.iloc[0][keys].to_dict()
    for k in ["entry_score"]:
        best_params[k] = int(best_params[k])
    for k in ["rsi_limit", "take_profit", "stop_loss", "trail_start", "trail_back"]:
        best_params[k] = float(best_params[k])

    _, best_trades = run_backtest(df_15m, df_1h, df_4h, best_params, collect_trades=True)

    clear_and_write(result_ws, list(results_df.columns), results_df.astype(str).values.tolist())
    clear_and_write(top_ws, list(top20_df.columns), top20_df.astype(str).values.tolist())

    if not best_trades.empty:
        clear_and_write(trades_ws, list(best_trades.columns), best_trades.astype(str).values.tolist())
    else:
        clear_and_write(trades_ws, ["message"], [["No trades"]])

    append_run_log(log_ws, "Backtest finished")
    print("Regime Elite 2022-2026 Backtest finished:", now_kst(), flush=True)
    print("Saved result to:", RESULT_SHEET_NAME, flush=True)
    print("Saved top20 to:", TOP_SHEET_NAME, flush=True)
    print("Saved best trades to:", TRADES_SHEET_NAME, flush=True)


if __name__ == "__main__":
    main()
