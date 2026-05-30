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
# BTC SOL 1D VS 1W FINAL COMPARE 2022-2026
# ============================================================
# 목적:
#   BTC/SOL 대상 최근장 2022~2026 일봉 최고 전략 vs 주봉 최고 전략 비교
#
# 비교 대상 코인:
#   BTCUSDT / SOLUSDT
#
# 일봉 최고 전략:
#   interval = 1d
#   lookback_bars = 6
#   risk_reward = 4.0
#   min_volume_ratio = 1.5
#   min_body_ratio = 0.5
#   ema200_filter = True
#   breakout_mode = BODY
#
# 주봉 최고 전략:
#   interval = 1w
#   lookback_bars = 6
#   risk_reward = 3.0
#   min_volume_ratio = 1.0
#   min_body_ratio = 0.5
#   ema200_filter = False
#   breakout_mode = BODY
#
# 검증 기간:
#   2022-01-01 ~ 2026-05-25
#   단, SOL은 Binance 상장 이후 데이터부터 자동 적용
#
# 총 조합:
#   2코인 × 2전략 = 4개
#
# 출력 시트:
#   BTC_SOL_FINAL_2022_RESULTS
#   BTC_SOL_FINAL_2022_TRADES
#   BTC_SOL_FINAL_2022_MDD
#   BTC_SOL_FINAL_2022_YEARLY
#   BTC_SOL_FINAL_2022_RUNLOG
#
# MDD:
#   realized_mdd   = 청산 후 계좌 기준 MDD
#   intratrade_mdd = 포지션 보유 중 저가 기준 미실현 MDD
#   total_mdd      = 둘 중 더 나쁜 값
# ============================================================


API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_SECRET_KEY")
client = Client(API_KEY, API_SECRET, requests_params={"timeout": 20})

START_DATE = "2022-01-01"
END_DATE = "2026-05-25"

# 현재 Binance Spot BNB 할인 기준: Maker/Taker 0.075% + 0.075%
FEE_ROUND_TRIP = 0.15

KST = ZoneInfo("Asia/Seoul")

GOOGLE_CLIENT_EMAIL = os.getenv("GOOGLE_CLIENT_EMAIL")
GOOGLE_PRIVATE_KEY = os.getenv("GOOGLE_PRIVATE_KEY", "").replace("\\n", "\n")
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME")

RESULT_SHEET_NAME = "BTC_SOL_FINAL_2022_RESULTS"
TRADES_SHEET_NAME = "BTC_SOL_FINAL_2022_TRADES"
MDD_SHEET_NAME = "BTC_SOL_FINAL_2022_MDD"
YEARLY_SHEET_NAME = "BTC_SOL_FINAL_2022_YEARLY"
RUN_LOG_SHEET_NAME = "BTC_SOL_FINAL_2022_RUNLOG"

CACHE_PREFIX = "btc_sol_1d_vs_1w_2022_2026"

SYMBOLS = ["BTCUSDT", "SOLUSDT"]

STRATEGY_TEMPLATES = [
    {
        "strategy_family": "DAILY_BEST",
        "interval": "1d",
        "lookback_bars": 6,
        "risk_reward": 4.0,
        "breakout_mode": "BODY",
        "max_prior_return_pct": 999.0,
        "min_body_ratio": 0.5,
        "min_volume_ratio": 1.5,
        "ema200_filter": True,
    },
    {
        "strategy_family": "WEEKLY_BEST",
        "interval": "1w",
        "lookback_bars": 6,
        "risk_reward": 3.0,
        "breakout_mode": "BODY",
        "max_prior_return_pct": 999.0,
        "min_body_ratio": 0.5,
        "min_volume_ratio": 1.0,
        "ema200_filter": False,
    },
]

PARAM_LIST = []
for symbol in SYMBOLS:
    for template in STRATEGY_TEMPLATES:
        p = dict(template)
        p["symbol"] = symbol
        PARAM_LIST.append(p)


def now_kst():
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")


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


def dt_to_ms(dt):
    return int(dt.timestamp() * 1000)


def net_after_fee(gross_pnl):
    return ((1 + gross_pnl / 100) * (1 - FEE_ROUND_TRIP / 100) - 1) * 100


def calc_cagr(total_return_pct):
    start_dt = datetime.strptime(START_DATE, "%Y-%m-%d")
    end_dt = datetime.strptime(END_DATE, "%Y-%m-%d")
    years = max((end_dt - start_dt).days / 365.25, 0.01)
    final_equity = 1 + float(total_return_pct) / 100
    if final_equity <= 0:
        return -100.0
    return round(((final_equity ** (1 / years)) - 1) * 100, 2)


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


def get_or_create_ws(spreadsheet, title, rows=1000, cols=80):
    try:
        return spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        return spreadsheet.add_worksheet(title=title, rows=rows, cols=cols)


def clear_and_write(ws, headers, rows):
    ws.clear()

    safe_headers = [sanitize_for_sheet(v) for v in headers]
    safe_rows = [[sanitize_for_sheet(v) for v in row] for row in rows]
    values = [safe_headers] + safe_rows

    try:
        ws.resize(rows=max(len(values), 1), cols=max(len(safe_headers), 1))
    except Exception as e:
        print(f"Worksheet resize skipped: {e}", flush=True)

    ws.update(range_name="A1", values=values)


def append_run_log(ws, message):
    print(f"[RUNLOG] {now_kst()} {message}", flush=True)
    try:
        ws.append_row([now_kst(), message])
    except Exception as e:
        print(f"RUNLOG append failed: {e}", flush=True)


def interval_to_binance(interval):
    if interval == "5m":
        return Client.KLINE_INTERVAL_5MINUTE
    if interval == "15m":
        return Client.KLINE_INTERVAL_15MINUTE
    if interval == "1h":
        return Client.KLINE_INTERVAL_1HOUR
    if interval == "4h":
        return Client.KLINE_INTERVAL_4HOUR
    if interval == "1d":
        return Client.KLINE_INTERVAL_1DAY
    if interval == "1w":
        return Client.KLINE_INTERVAL_1WEEK
    if interval == "1M":
        return Client.KLINE_INTERVAL_1MONTH
    raise ValueError(f"Unsupported interval: {interval}")


def fetch_klines(symbol, interval, start_dt, end_dt):
    print(f"Downloading {symbol} {interval} data...", flush=True)

    all_rows = []
    start_ms = dt_to_ms(start_dt)
    end_ms = dt_to_ms(end_dt)
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

    df["body_top"] = df[["open", "close"]].max(axis=1)
    df["range"] = df["high"] - df["low"]
    df["body"] = (df["close"] - df["open"]).abs()
    df["body_ratio"] = df["body"] / df["range"].replace(0, pd.NA)

    df["ema200"] = df["close"].ewm(span=200, adjust=False).mean()

    df["volume_ma"] = df["volume"].rolling(20).mean()
    df["volume_ratio"] = df["volume"] / df["volume_ma"]

    return df


def cache_name(symbol, interval_label):
    return f"{CACHE_PREFIX}_{symbol}_{interval_label}.pkl"


def load_data(symbol, interval_label):
    cache_file = cache_name(symbol, interval_label)

    if os.path.exists(cache_file):
        try:
            print(f"Loading cached data: {cache_file}", flush=True)
            df = pd.read_pickle(cache_file)

            required_cols = ["body_ratio", "ema200", "volume_ratio", "body_top"]
            if not all(col in df.columns for col in required_cols):
                print("Cached data missing indicators. Rebuilding indicators.", flush=True)
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

    start_dt = datetime.strptime(START_DATE, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end_dt = datetime.strptime(END_DATE, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    interval = interval_to_binance(interval_label)
    df = fetch_klines(symbol, interval, start_dt, end_dt)
    df.to_pickle(cache_file)
    return df


def strategy_label(params):
    symbol = params["symbol"]
    family = params.get("strategy_family", "STRATEGY")
    interval = params["interval"]
    lb = int(params["lookback_bars"])
    vol = float(params["min_volume_ratio"])
    rr = float(params["risk_reward"])
    body = float(params["min_body_ratio"])
    ema = "EMA_ON" if bool(params["ema200_filter"]) else "EMA_OFF"
    return f"{symbol}_{family}_{interval}_LB_{lb}_RR_{rr}_VOL_{vol}_BODY_{body}_{ema}"


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

    # EMA200 필터
    if params["ema200_filter"]:
        if pd.isna(now["ema200"]) or now["close"] <= now["ema200"]:
            return False

    # 직전 구간 과열 제한. 999면 사실상 비활성.
    max_prior = params["max_prior_return_pct"]
    if max_prior < 999:
        prior_return = ((prev["close"].iloc[-1] - prev["close"].iloc[0]) / prev["close"].iloc[0]) * 100
        if prior_return > max_prior:
            return False

    # BODY 돌파
    if params["breakout_mode"] == "BODY":
        return now["close"] > prev["body_top"].max()

    # HIGH 돌파
    if params["breakout_mode"] == "HIGH":
        return now["close"] > prev["high"].max()

    return False


def simulate_long_detailed(df, entry_idx, entry_price, stop_price, target_price, current_equity):
    local_min_equity = current_equity
    local_min_price = entry_price
    local_min_time = df.iloc[entry_idx]["datetime"].strftime("%Y-%m-%d %H:%M:%S")
    local_min_net_pnl = 0.0

    for j in range(entry_idx + 1, len(df)):
        row = df.iloc[j]
        low = row["low"]
        high = row["high"]
        exit_time = row["datetime"].strftime("%Y-%m-%d %H:%M:%S")

        # 보유 중 저가 기준 미실현 손실
        unrealized_low_pnl = ((low - entry_price) / entry_price) * 100
        unrealized_low_net = net_after_fee(unrealized_low_pnl)
        mark_equity = current_equity * (1 + unrealized_low_net / 100)

        if mark_equity < local_min_equity:
            local_min_equity = mark_equity
            local_min_price = low
            local_min_time = exit_time
            local_min_net_pnl = unrealized_low_net

        hit_sl = low <= stop_price
        hit_tp = high >= target_price

        # 같은 봉에서 TP/SL 둘 다 터치하면 보수적으로 SL 우선
        if hit_sl and hit_tp:
            gross_pnl = ((stop_price - entry_price) / entry_price) * 100
            return (
                j, exit_time, stop_price, "STOP_LOSS_SAME_CANDLE", gross_pnl,
                local_min_equity, local_min_price, local_min_time, local_min_net_pnl
            )

        if hit_sl:
            gross_pnl = ((stop_price - entry_price) / entry_price) * 100
            return (
                j, exit_time, stop_price, "STOP_LOSS", gross_pnl,
                local_min_equity, local_min_price, local_min_time, local_min_net_pnl
            )

        if hit_tp:
            gross_pnl = ((target_price - entry_price) / entry_price) * 100
            return (
                j, exit_time, target_price, "TAKE_PROFIT", gross_pnl,
                local_min_equity, local_min_price, local_min_time, local_min_net_pnl
            )

    row = df.iloc[-1]
    exit_price = row["close"]
    exit_time = row["datetime"].strftime("%Y-%m-%d %H:%M:%S")
    gross_pnl = ((exit_price - entry_price) / entry_price) * 100

    return (
        len(df) - 1, exit_time, exit_price, "TIME_EXIT", gross_pnl,
        local_min_equity, local_min_price, local_min_time, local_min_net_pnl
    )


def backtest_params(df, params):
    trades = []
    mdd_events = []

    equity = 100.0
    peak_equity = 100.0
    realized_mdd = 0.0
    intratrade_mdd = 0.0
    total_mdd = 0.0

    lb = int(params["lookback_bars"])
    label = strategy_label(params)

    i = lb
    trade_no = 0

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

        (
            exit_idx, exit_time, exit_price, exit_reason, gross_pnl,
            local_min_equity, local_min_price, local_min_time, local_min_net_pnl
        ) = simulate_long_detailed(df, i, entry_price, stop_price, target_price, equity)

        trade_no += 1

        # 포지션 보유 중 최저 equity 기준 MDD
        trade_intratrade_dd = ((local_min_equity - peak_equity) / peak_equity) * 100
        if trade_intratrade_dd < intratrade_mdd:
            intratrade_mdd = trade_intratrade_dd
            mdd_events.append({
                **params,
                "strategy_key": label,
                "mdd_type": "INTRATRADE_LOW",
                "trade_no": trade_no,
                "entry_time": now["datetime"].strftime("%Y-%m-%d %H:%M:%S"),
                "mdd_time": local_min_time,
                "entry_price": round(entry_price, 2),
                "mdd_price": round(local_min_price, 2),
                "equity_before_trade": round(equity, 4),
                "peak_equity_before_trade": round(peak_equity, 4),
                "mdd_equity": round(local_min_equity, 4),
                "mdd_pct": round(trade_intratrade_dd, 4),
                "trade_low_net_pnl": round(local_min_net_pnl, 4),
            })

        net_pnl = net_after_fee(gross_pnl)
        equity_before = equity
        equity *= (1 + net_pnl / 100)
        peak_equity = max(peak_equity, equity)

        trade_realized_dd = ((equity - peak_equity) / peak_equity) * 100
        if trade_realized_dd < realized_mdd:
            realized_mdd = trade_realized_dd
            mdd_events.append({
                **params,
                "strategy_key": label,
                "mdd_type": "REALIZED_CLOSE",
                "trade_no": trade_no,
                "entry_time": now["datetime"].strftime("%Y-%m-%d %H:%M:%S"),
                "mdd_time": exit_time,
                "entry_price": round(entry_price, 2),
                "mdd_price": round(exit_price, 2),
                "equity_before_trade": round(equity_before, 4),
                "peak_equity_before_trade": round(peak_equity, 4),
                "mdd_equity": round(equity, 4),
                "mdd_pct": round(trade_realized_dd, 4),
                "trade_low_net_pnl": round(net_pnl, 4),
            })

        total_mdd = min(realized_mdd, intratrade_mdd)

        trades.append({
            **params,
            "strategy_key": label,
            "trade_no": trade_no,
            "entry_time": now["datetime"].strftime("%Y-%m-%d %H:%M:%S"),
            "entry_year": int(now["datetime"].year),
            "exit_time": exit_time,
            "exit_year": int(pd.to_datetime(exit_time).year),
            "entry_price": round(entry_price, 2),
            "stop_price": round(stop_price, 2),
            "target_price": round(target_price, 2),
            "exit_price": round(exit_price, 2),
            "gross_pnl": round(gross_pnl, 4),
            "net_pnl": round(net_pnl, 4),
            "exit_reason": exit_reason,
            "equity_before": round(equity_before, 4),
            "equity_after": round(equity, 4),
            "peak_equity_after": round(peak_equity, 4),
            "realized_mdd": round(realized_mdd, 4),
            "intratrade_mdd": round(intratrade_mdd, 4),
            "total_mdd": round(total_mdd, 4),
            "trade_low_time": local_min_time,
            "trade_low_price": round(local_min_price, 2),
            "trade_low_net_pnl": round(local_min_net_pnl, 4),
            "trade_intratrade_dd": round(trade_intratrade_dd, 4),
            "body_ratio": round(now["body_ratio"], 4) if not pd.isna(now["body_ratio"]) else "",
            "volume_ratio": round(now["volume_ratio"], 4) if not pd.isna(now["volume_ratio"]) else "",
            "ema200": round(now["ema200"], 2) if not pd.isna(now["ema200"]) else "",
        })

        i = int(exit_idx) + 1

    trades_df = pd.DataFrame(trades)
    mdd_df = pd.DataFrame(mdd_events)

    if trades_df.empty:
        return {
            **params,
            "strategy_key": label,
            "trades": 0,
            "win_rate": 0,
            "total_return": 0,
            "cagr": 0,
            "realized_mdd": 0,
            "intratrade_mdd": 0,
            "total_mdd": 0,
            "avg_win": 0,
            "avg_loss": 0,
            "profit_factor": 0,
            "tp_count": 0,
            "sl_count": 0,
            "same_candle_sl_count": 0,
            "time_exit_count": 0,
        }, trades_df, mdd_df

    wins = trades_df[trades_df["net_pnl"] > 0]
    losses = trades_df[trades_df["net_pnl"] <= 0]
    exit_counts = trades_df["exit_reason"].value_counts().to_dict()

    total_return = trades_df["equity_after"].iloc[-1] - 100
    gross_profit = wins["net_pnl"].sum() if not wins.empty else 0
    gross_loss = losses["net_pnl"].sum() if not losses.empty else 0
    profit_factor = 999.0 if gross_loss == 0 else abs(gross_profit / gross_loss)

    return {
        **params,
        "strategy_key": label,
        "trades": len(trades_df),
        "win_rate": round(len(wins) / len(trades_df) * 100, 2),
        "total_return": round(total_return, 2),
        "cagr": calc_cagr(total_return),
        "realized_mdd": round(realized_mdd, 2),
        "intratrade_mdd": round(intratrade_mdd, 2),
        "total_mdd": round(total_mdd, 2),
        "avg_win": round(wins["net_pnl"].mean() if not wins.empty else 0, 4),
        "avg_loss": round(losses["net_pnl"].mean() if not losses.empty else 0, 4),
        "profit_factor": round(profit_factor, 4),
        "tp_count": int(exit_counts.get("TAKE_PROFIT", 0)),
        "sl_count": int(exit_counts.get("STOP_LOSS", 0)) + int(exit_counts.get("STOP_LOSS_SAME_CANDLE", 0)),
        "same_candle_sl_count": int(exit_counts.get("STOP_LOSS_SAME_CANDLE", 0)),
        "time_exit_count": int(exit_counts.get("TIME_EXIT", 0)),
    }, trades_df, mdd_df


def make_yearly_summary(trades_df, params, label):
    if trades_df.empty:
        return pd.DataFrame()

    rows = []
    for year, g in trades_df.groupby("entry_year"):
        equity = 100.0
        peak = 100.0
        realized_mdd = 0.0

        for _, row in g.iterrows():
            equity *= (1 + float(row["net_pnl"]) / 100)
            peak = max(peak, equity)
            dd = ((equity - peak) / peak) * 100
            realized_mdd = min(realized_mdd, dd)

        wins = g[g["net_pnl"].astype(float) > 0]
        losses = g[g["net_pnl"].astype(float) <= 0]

        rows.append({
            **params,
            "strategy_key": label,
            "year": int(year),
            "trades": len(g),
            "win_rate": round(len(wins) / len(g) * 100, 2) if len(g) else 0,
            "year_return": round(equity - 100, 2),
            "year_realized_mdd": round(realized_mdd, 2),
            "avg_win": round(wins["net_pnl"].astype(float).mean(), 4) if not wins.empty else 0,
            "avg_loss": round(losses["net_pnl"].astype(float).mean(), 4) if not losses.empty else 0,
            "tp_count": int((g["exit_reason"] == "TAKE_PROFIT").sum()),
            "sl_count": int(g["exit_reason"].astype(str).str.contains("STOP_LOSS").sum()),
        })

    return pd.DataFrame(rows)


def main():
    print("BTC SOL 1D vs 1W Final Compare 2022-2026 started:", now_kst(), flush=True)

    spreadsheet = init_gspread()
    result_ws = get_or_create_ws(spreadsheet, RESULT_SHEET_NAME, rows=100, cols=80)
    trades_ws = get_or_create_ws(spreadsheet, TRADES_SHEET_NAME, rows=1000, cols=80)
    mdd_ws = get_or_create_ws(spreadsheet, MDD_SHEET_NAME, rows=1000, cols=80)
    yearly_ws = get_or_create_ws(spreadsheet, YEARLY_SHEET_NAME, rows=1000, cols=80)
    log_ws = get_or_create_ws(spreadsheet, RUN_LOG_SHEET_NAME, rows=100, cols=10)

    append_run_log(log_ws, "BTC SOL 1D vs 1W final compare 2022-2026 started")

    combos = PARAM_LIST

    print(f"Total combinations: {len(combos)}", flush=True)
    append_run_log(log_ws, f"Total combinations: {len(combos)}")

    data_cache = {}
    result_rows = []
    all_trades = []
    all_mdd = []
    all_yearly = []

    for idx, params in enumerate(combos, start=1):
        params = dict(params)
        label = strategy_label(params)
        cache_key = f"{params['symbol']}_{params['interval']}"

        if cache_key not in data_cache:
            data_cache[cache_key] = load_data(params["symbol"], params["interval"])

        stats, trades_df, mdd_df = backtest_params(data_cache[cache_key], params)
        stats["run_time"] = now_kst()
        result_rows.append(stats)

        if not trades_df.empty:
            all_trades.append(trades_df)
            ydf = make_yearly_summary(trades_df, params, label)
            if not ydf.empty:
                all_yearly.append(ydf)

        if not mdd_df.empty:
            all_mdd.append(mdd_df)

        print(f"Progress: {idx}/{len(combos)} {label}", flush=True)

    results_df = pd.DataFrame(result_rows).replace([float("inf"), float("-inf")], "").fillna("")
    results_df = results_df.sort_values(
        by=["total_return", "total_mdd", "profit_factor", "trades"],
        ascending=[False, False, False, False],
    )

    trades_all_df = (
        pd.concat(all_trades, ignore_index=True)
        .replace([float("inf"), float("-inf")], "")
        .fillna("")
        if all_trades else pd.DataFrame()
    )

    mdd_all_df = (
        pd.concat(all_mdd, ignore_index=True)
        .replace([float("inf"), float("-inf")], "")
        .fillna("")
        if all_mdd else pd.DataFrame()
    )

    yearly_df = (
        pd.concat(all_yearly, ignore_index=True)
        .replace([float("inf"), float("-inf")], "")
        .fillna("")
        if all_yearly else pd.DataFrame()
    )

    clear_and_write(result_ws, list(results_df.columns), results_df.astype(str).values.tolist())
    time.sleep(3)

    if not trades_all_df.empty:
        clear_and_write(trades_ws, list(trades_all_df.columns), trades_all_df.astype(str).values.tolist())
    else:
        clear_and_write(trades_ws, ["message"], [["No trades"]])

    time.sleep(3)

    if not mdd_all_df.empty:
        mdd_all_df = mdd_all_df.sort_values(by=["mdd_pct"], ascending=True)
        clear_and_write(mdd_ws, list(mdd_all_df.columns), mdd_all_df.astype(str).values.tolist())
    else:
        clear_and_write(mdd_ws, ["message"], [["No MDD events"]])

    time.sleep(3)

    if not yearly_df.empty:
        clear_and_write(yearly_ws, list(yearly_df.columns), yearly_df.astype(str).values.tolist())
    else:
        clear_and_write(yearly_ws, ["message"], [["No yearly data"]])

    append_run_log(log_ws, "BTC SOL 1D vs 1W final compare 2022-2026 finished")
    print("BTC SOL 1D vs 1W Final Compare 2022-2026 finished:", now_kst(), flush=True)
    print("Saved result to:", RESULT_SHEET_NAME, flush=True)
    print("Saved trades to:", TRADES_SHEET_NAME, flush=True)
    print("Saved MDD to:", MDD_SHEET_NAME, flush=True)
    print("Saved yearly to:", YEARLY_SHEET_NAME, flush=True)


if __name__ == "__main__":
    main()
