import os
import math
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import numpy as np
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from binance.client import Client


# ============================================================
# BTC WEEKLY vs 4H OVERLAP TEST 2022-2026
# ============================================================
# 목적:
#   1) BTC 주봉 메인 전략과 BTC 4시간봉 보조 전략의 보유기간 겹침 확인
#   2) 4시간봉 수익이 주봉 보유구간에서 나온 것인지, 주봉 공백구간에서 나온 것인지 분해
#   3) 주봉 현금구간에서 4시간봉을 돌릴 가치가 있는지 판단
#
# 기간:
#   2022-01-01 ~ 2026-05-25
#
# 주봉 전략:
#   BTCUSDT 1W
#   LB6 / RR3.0 / VOL1.0 / BODY0.5 / EMA OFF
#
# 4시간봉 전략:
#   BTCUSDT 4H
#   LONG_ONLY / TP 3.5 / SL -2.4 / EMA distance 0.5 / max_hold 48
#
# 수수료:
#   왕복 0.15%
#
# 출력 시트:
#   BTC_W1_4H_OVERLAP_SUMMARY
#   BTC_W1_4H_OVERLAP_DETAIL
#   BTC_W1_4H_WEEKLY_TRADES
#   BTC_W1_4H_4H_TRADES
#   BTC_W1_4H_RUNLOG
# ============================================================


API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_SECRET_KEY")
client = Client(API_KEY, API_SECRET, requests_params={"timeout": 20})

START_DATE = "2022-01-01"
END_DATE = "2026-05-25"
SYMBOL = "BTCUSDT"
FEE_ROUND_TRIP = 0.15
KST = ZoneInfo("Asia/Seoul")

GOOGLE_CLIENT_EMAIL = os.getenv("GOOGLE_CLIENT_EMAIL")
GOOGLE_PRIVATE_KEY = os.getenv("GOOGLE_PRIVATE_KEY", "").replace("\\n", "\n")
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME")

SUMMARY_SHEET_NAME = "BTC_W1_4H_OVERLAP_SUMMARY"
DETAIL_SHEET_NAME = "BTC_W1_4H_OVERLAP_DETAIL"
WEEKLY_TRADES_SHEET_NAME = "BTC_W1_4H_WEEKLY_TRADES"
H4_TRADES_SHEET_NAME = "BTC_W1_4H_4H_TRADES"
RUN_LOG_SHEET_NAME = "BTC_W1_4H_RUNLOG"

CACHE_PREFIX = "btc_w1_4h_overlap_2022_2026"


WEEKLY_PARAMS = {
    "strategy_name": "WEEKLY_BEST",
    "symbol": SYMBOL,
    "interval": "1w",
    "lookback_bars": 6,
    "risk_reward": 3.0,
    "breakout_mode": "BODY",
    "max_prior_return_pct": 999.0,
    "min_body_ratio": 0.5,
    "min_volume_ratio": 1.0,
    "ema200_filter": False,
}

H4_PARAMS = {
    "strategy_name": "H4_HA_STOCH_BEST",
    "symbol": SYMBOL,
    "interval": "4h",
    "direction_mode": "LONG_ONLY",
    "take_profit": 3.5,
    "stop_loss": -2.4,
    "stoch_zone": "NONE",
    "wick_tolerance": 0.0003,
    "ema_price_source": "REAL_CLOSE",
    "min_ema_distance_pct": 0.5,
    "max_hold_bars": 48,
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
    try:
        ws.resize(rows=max(len(values), 1), cols=max(len(safe_headers), 1))
    except Exception as e:
        print(f"Worksheet resize skipped: {e}", flush=True)
    ws.update(values)


def append_run_log(ws, message):
    try:
        ws.append_row([now_kst(), message])
    except Exception as e:
        print(f"Runlog append failed: {e}", flush=True)


def interval_to_binance(interval_label):
    if interval_label == "4h":
        return Client.KLINE_INTERVAL_4HOUR
    if interval_label == "1w":
        return Client.KLINE_INTERVAL_1WEEK
    raise ValueError(f"Unsupported interval: {interval_label}")


def fetch_klines(symbol, interval, start_dt, end_dt):
    rows = []
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)

    while True:
        klines = client.get_klines(
            symbol=symbol,
            interval=interval,
            startTime=start_ms,
            endTime=end_ms,
            limit=1000,
        )
        if not klines:
            break

        rows.extend(klines)
        last_open_time = klines[-1][0]
        start_ms = last_open_time + 1

        print(
            f"Downloaded {symbol} {interval}: {len(rows)} candles, last={datetime.fromtimestamp(last_open_time / 1000, tz=KST)}",
            flush=True,
        )

        if len(klines) < 1000:
            break

    df = pd.DataFrame(
        rows,
        columns=[
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "quote_asset_volume", "number_of_trades",
            "taker_buy_base_asset_volume", "taker_buy_quote_asset_volume", "ignore",
        ],
    )

    if df.empty:
        return df

    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df["datetime"] = pd.to_datetime(df["open_time"], unit="ms", utc=True).dt.tz_convert(KST).dt.tz_localize(None)
    df = df[["datetime", "open", "high", "low", "close", "volume"]].dropna().reset_index(drop=True)
    return df


def net_after_fee(gross_pnl):
    return ((1 + gross_pnl / 100) * (1 - FEE_ROUND_TRIP / 100) - 1) * 100


def load_raw_data(symbol, interval_label):
    cache_file = f"{CACHE_PREFIX}_{symbol}_{interval_label}.pkl"
    if os.path.exists(cache_file):
        print(f"Loading cached data: {cache_file}", flush=True)
        return pd.read_pickle(cache_file)

    start_dt = datetime.strptime(START_DATE, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end_dt = datetime.strptime(END_DATE, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    df = fetch_klines(symbol, interval_to_binance(interval_label), start_dt, end_dt)
    df.to_pickle(cache_file)
    return df


# =========================
# Weekly big candle strategy
# =========================

def add_weekly_indicators(df):
    df = df.copy()
    df["body"] = (df["close"] - df["open"]).abs()
    df["range"] = (df["high"] - df["low"]).replace(0, np.nan)
    df["body_ratio"] = df["body"] / df["range"]
    df["body_top"] = df[["open", "close"]].max(axis=1)
    df["ema200"] = df["close"].ewm(span=200, adjust=False).mean()
    df["volume_ma"] = df["volume"].rolling(20, min_periods=1).mean()
    df["volume_ratio"] = df["volume"] / df["volume_ma"].replace(0, np.nan)
    return df


def weekly_is_setup(df, i, params):
    lb = int(params["lookback_bars"])
    now = df.iloc[i]
    prev = df.iloc[i - lb:i]

    if now["close"] <= now["open"]:
        return False
    if pd.isna(now["body_ratio"]) or now["body_ratio"] < params["min_body_ratio"]:
        return False
    if pd.isna(now["volume_ratio"]) or now["volume_ratio"] < params["min_volume_ratio"]:
        return False
    if params["ema200_filter"]:
        if pd.isna(now["ema200"]) or now["close"] <= now["ema200"]:
            return False
    if params["max_prior_return_pct"] < 999:
        prior_return = ((prev["close"].iloc[-1] - prev["close"].iloc[0]) / prev["close"].iloc[0]) * 100
        if prior_return > params["max_prior_return_pct"]:
            return False

    if params["breakout_mode"] == "BODY":
        return now["close"] > prev["body_top"].max()
    if params["breakout_mode"] == "HIGH":
        return now["close"] > prev["high"].max()
    return False


def simulate_weekly_trade(df, entry_idx, entry_price, stop_price, target_price):
    for j in range(entry_idx + 1, len(df)):
        row = df.iloc[j]
        low = row["low"]
        high = row["high"]
        exit_time = row["datetime"].strftime("%Y-%m-%d %H:%M:%S")

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

    row = df.iloc[-1]
    exit_price = row["close"]
    exit_time = row["datetime"].strftime("%Y-%m-%d %H:%M:%S")
    gross_pnl = ((exit_price - entry_price) / entry_price) * 100
    return len(df) - 1, exit_time, exit_price, "TIME_EXIT", gross_pnl


def backtest_weekly(df, params):
    df = add_weekly_indicators(df)
    trades = []
    equity = 100.0
    peak_equity = 100.0
    max_drawdown = 0.0
    lb = int(params["lookback_bars"])
    i = lb
    trade_no = 0

    while i < len(df):
        if not weekly_is_setup(df, i, params):
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
        exit_idx, exit_time, exit_price, exit_reason, gross_pnl = simulate_weekly_trade(
            df, i, entry_price, stop_price, target_price
        )
        net_pnl = net_after_fee(gross_pnl)

        trade_no += 1
        equity_before = equity
        equity *= (1 + net_pnl / 100)
        peak_equity = max(peak_equity, equity)
        max_drawdown = min(max_drawdown, ((equity - peak_equity) / peak_equity) * 100)

        trades.append({
            **params,
            "trade_no": trade_no,
            "entry_time": now["datetime"].strftime("%Y-%m-%d %H:%M:%S"),
            "exit_time": exit_time,
            "entry_price": round(entry_price, 4),
            "exit_price": round(exit_price, 4),
            "gross_pnl": round(gross_pnl, 4),
            "net_pnl": round(net_pnl, 4),
            "exit_reason": exit_reason,
            "equity_before": round(equity_before, 4),
            "equity_after": round(equity, 4),
            "max_drawdown": round(max_drawdown, 4),
        })
        i = exit_idx + 1

    return pd.DataFrame(trades)


# =========================
# 4H HA + Stoch RSI strategy
# =========================

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


def add_h4_indicators(df):
    df = add_heikin_ashi(df)
    df["ema200_real"] = df["close"].ewm(span=200, adjust=False).mean()
    df["ema200_ha"] = df["ha_close"].ewm(span=200, adjust=False).mean()
    df = add_stoch_rsi(df, 14, 14, 3, 3)
    return df


def has_no_lower_wick(row, tolerance):
    body_low = min(row["ha_open"], row["ha_close"])
    if body_low == 0:
        return False
    wick = body_low - row["ha_low"]
    return (wick / body_low) <= tolerance


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


def simulate_h4_trade(df, entry_idx, entry_price, take_profit, stop_loss, max_hold_bars):
    target_price = entry_price * (1 + take_profit / 100)
    stop_price = entry_price * (1 + stop_loss / 100)
    last_idx = min(len(df) - 1, entry_idx + int(max_hold_bars))

    for j in range(entry_idx + 1, last_idx + 1):
        row = df.iloc[j]
        high = row["high"]
        low = row["low"]
        exit_time = row["datetime"].strftime("%Y-%m-%d %H:%M:%S")
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

    row = df.iloc[last_idx]
    exit_price = row["close"]
    exit_time = row["datetime"].strftime("%Y-%m-%d %H:%M:%S")
    gross_pnl = ((exit_price - entry_price) / entry_price) * 100
    return last_idx, exit_time, exit_price, "TIME_EXIT", gross_pnl


def backtest_h4(df, params):
    df = add_h4_indicators(df)
    trades = []
    equity = 100.0
    peak_equity = 100.0
    max_drawdown = 0.0
    i = 201
    trade_no = 0

    while i < len(df):
        prev = df.iloc[i - 1]
        now = df.iloc[i]

        long_signal = (
            trend_ok_long(now, params)
            and stoch_cross_long(prev, now, params["stoch_zone"])
            and now["ha_bull"]
            and has_no_lower_wick(now, params["wick_tolerance"])
        )

        if not long_signal:
            i += 1
            continue

        entry_price = now["close"]
        exit_idx, exit_time, exit_price, exit_reason, gross_pnl = simulate_h4_trade(
            df=df,
            entry_idx=i,
            entry_price=entry_price,
            take_profit=params["take_profit"],
            stop_loss=params["stop_loss"],
            max_hold_bars=params["max_hold_bars"],
        )
        net_pnl = net_after_fee(gross_pnl)

        trade_no += 1
        equity_before = equity
        equity *= (1 + net_pnl / 100)
        peak_equity = max(peak_equity, equity)
        max_drawdown = min(max_drawdown, ((equity - peak_equity) / peak_equity) * 100)

        trades.append({
            **params,
            "trade_no": trade_no,
            "entry_time": now["datetime"].strftime("%Y-%m-%d %H:%M:%S"),
            "exit_time": exit_time,
            "entry_price": round(entry_price, 4),
            "exit_price": round(exit_price, 4),
            "gross_pnl": round(gross_pnl, 4),
            "net_pnl": round(net_pnl, 4),
            "exit_reason": exit_reason,
            "equity_before": round(equity_before, 4),
            "equity_after": round(equity, 4),
            "max_drawdown": round(max_drawdown, 4),
        })
        i = exit_idx + 1

    return pd.DataFrame(trades)


# =========================
# Overlap analysis
# =========================

def compound_return(pnls):
    equity = 100.0
    peak = 100.0
    mdd = 0.0
    for pnl in pnls:
        equity *= (1 + float(pnl) / 100)
        peak = max(peak, equity)
        mdd = min(mdd, ((equity - peak) / peak) * 100)
    return round(equity - 100, 4), round(mdd, 4)


def analyze_overlap(weekly_df, h4_df):
    if h4_df.empty:
        return pd.DataFrame(), pd.DataFrame()

    weekly = weekly_df.copy()
    h4 = h4_df.copy()

    for df in [weekly, h4]:
        if not df.empty:
            df["entry_dt"] = pd.to_datetime(df["entry_time"])
            df["exit_dt"] = pd.to_datetime(df["exit_time"])

    detail_rows = []

    for _, h in h4.iterrows():
        h_entry = h["entry_dt"]
        h_exit = h["exit_dt"]
        overlaps = []

        for _, w in weekly.iterrows():
            w_entry = w["entry_dt"]
            w_exit = w["exit_dt"]

            overlap_start = max(h_entry, w_entry)
            overlap_end = min(h_exit, w_exit)

            if overlap_start < overlap_end:
                overlap_hours = (overlap_end - overlap_start).total_seconds() / 3600
                h_duration_hours = max((h_exit - h_entry).total_seconds() / 3600, 0.0001)
                overlaps.append({
                    "weekly_trade_no": int(w["trade_no"]),
                    "weekly_entry_time": w["entry_time"],
                    "weekly_exit_time": w["exit_time"],
                    "overlap_hours": round(overlap_hours, 2),
                    "overlap_ratio_of_h4_trade": round(overlap_hours / h_duration_hours * 100, 2),
                })

        if overlaps:
            max_overlap = max(overlaps, key=lambda x: x["overlap_hours"])
            overlap_status = "OVERLAP_WEEKLY_POSITION"
        else:
            max_overlap = {
                "weekly_trade_no": "",
                "weekly_entry_time": "",
                "weekly_exit_time": "",
                "overlap_hours": 0,
                "overlap_ratio_of_h4_trade": 0,
            }
            overlap_status = "OUTSIDE_WEEKLY_POSITION"

        detail_rows.append({
            "h4_trade_no": int(h["trade_no"]),
            "h4_entry_time": h["entry_time"],
            "h4_exit_time": h["exit_time"],
            "h4_net_pnl": h["net_pnl"],
            "h4_exit_reason": h["exit_reason"],
            "overlap_status": overlap_status,
            **max_overlap,
        })

    detail_df = pd.DataFrame(detail_rows)

    all_return, all_mdd = compound_return(h4["net_pnl"].tolist())
    inside_pnls = detail_df.loc[detail_df["overlap_status"] == "OVERLAP_WEEKLY_POSITION", "h4_net_pnl"].tolist()
    outside_pnls = detail_df.loc[detail_df["overlap_status"] == "OUTSIDE_WEEKLY_POSITION", "h4_net_pnl"].tolist()

    inside_return, inside_mdd = compound_return(inside_pnls)
    outside_return, outside_mdd = compound_return(outside_pnls)

    weekly_return, weekly_mdd = compound_return(weekly["net_pnl"].tolist() if not weekly.empty else [])
    h4_total_trades = len(detail_df)
    inside_count = len(inside_pnls)
    outside_count = len(outside_pnls)

    summary_rows = [
        {
            "metric": "WEEKLY_TOTAL",
            "trades": len(weekly),
            "compound_return": weekly_return,
            "mdd": weekly_mdd,
            "note": "주봉 전략 단독 성과",
        },
        {
            "metric": "H4_TOTAL",
            "trades": h4_total_trades,
            "compound_return": all_return,
            "mdd": all_mdd,
            "note": "4시간봉 전략 전체 성과",
        },
        {
            "metric": "H4_INSIDE_WEEKLY_POSITION",
            "trades": inside_count,
            "trade_ratio_pct": round(inside_count / h4_total_trades * 100, 2) if h4_total_trades else 0,
            "compound_return": inside_return,
            "mdd": inside_mdd,
            "note": "주봉 보유 중 발생한 4시간봉 거래",
        },
        {
            "metric": "H4_OUTSIDE_WEEKLY_POSITION",
            "trades": outside_count,
            "trade_ratio_pct": round(outside_count / h4_total_trades * 100, 2) if h4_total_trades else 0,
            "compound_return": outside_return,
            "mdd": outside_mdd,
            "note": "주봉 미보유/현금 구간에서 발생한 4시간봉 거래",
        },
    ]

    if h4_total_trades:
        overlap_trade_ratio = inside_count / h4_total_trades * 100
        if overlap_trade_ratio >= 70:
            decision = "겹침 높음: 4시간봉 보조전략 가치 낮음"
        elif overlap_trade_ratio >= 40:
            decision = "겹침 중간: 주봉 공백기 성과를 같이 확인"
        else:
            decision = "겹침 낮음: 주봉 공백기 보조전략 가치 있음"
        summary_rows.append({
            "metric": "OVERLAP_DECISION",
            "trades": "",
            "trade_ratio_pct": round(overlap_trade_ratio, 2),
            "compound_return": "",
            "mdd": "",
            "note": decision,
        })

    return pd.DataFrame(summary_rows), detail_df


def main():
    print("BTC W1 vs 4H overlap test started:", now_kst(), flush=True)

    spreadsheet = init_gspread()
    summary_ws = get_or_create_ws(spreadsheet, SUMMARY_SHEET_NAME, rows=100, cols=40)
    detail_ws = get_or_create_ws(spreadsheet, DETAIL_SHEET_NAME, rows=1000, cols=40)
    weekly_ws = get_or_create_ws(spreadsheet, WEEKLY_TRADES_SHEET_NAME, rows=100, cols=60)
    h4_ws = get_or_create_ws(spreadsheet, H4_TRADES_SHEET_NAME, rows=1000, cols=60)
    log_ws = get_or_create_ws(spreadsheet, RUN_LOG_SHEET_NAME, rows=100, cols=10)

    append_run_log(log_ws, "Overlap test started")

    print("Downloading/loading weekly data...", flush=True)
    append_run_log(log_ws, "Loading weekly data")
    weekly_raw = load_raw_data(SYMBOL, "1w")

    print("Downloading/loading 4h data...", flush=True)
    append_run_log(log_ws, "Loading 4h data")
    h4_raw = load_raw_data(SYMBOL, "4h")

    weekly_trades = backtest_weekly(weekly_raw, WEEKLY_PARAMS)
    h4_trades = backtest_h4(h4_raw, H4_PARAMS)

    print(f"Weekly trades: {len(weekly_trades)}", flush=True)
    print(f"4H trades: {len(h4_trades)}", flush=True)

    summary_df, detail_df = analyze_overlap(weekly_trades, h4_trades)

    for df in [summary_df, detail_df, weekly_trades, h4_trades]:
        if not df.empty:
            df.replace([float("inf"), float("-inf")], "", inplace=True)
            df.fillna("", inplace=True)

    clear_and_write(
        summary_ws,
        list(summary_df.columns) if not summary_df.empty else ["message"],
        summary_df.astype(str).values.tolist() if not summary_df.empty else [["No summary"]],
    )
    clear_and_write(
        detail_ws,
        list(detail_df.columns) if not detail_df.empty else ["message"],
        detail_df.astype(str).values.tolist() if not detail_df.empty else [["No overlap detail"]],
    )
    clear_and_write(
        weekly_ws,
        list(weekly_trades.columns) if not weekly_trades.empty else ["message"],
        weekly_trades.astype(str).values.tolist() if not weekly_trades.empty else [["No weekly trades"]],
    )
    clear_and_write(
        h4_ws,
        list(h4_trades.columns) if not h4_trades.empty else ["message"],
        h4_trades.astype(str).values.tolist() if not h4_trades.empty else [["No 4h trades"]],
    )

    append_run_log(log_ws, "Overlap test finished")

    print("Saved summary to:", SUMMARY_SHEET_NAME, flush=True)
    print("Saved detail to:", DETAIL_SHEET_NAME, flush=True)
    print("Saved weekly trades to:", WEEKLY_TRADES_SHEET_NAME, flush=True)
    print("Saved 4H trades to:", H4_TRADES_SHEET_NAME, flush=True)
    print("BTC W1 vs 4H overlap test finished:", now_kst(), flush=True)


if __name__ == "__main__":
    main()
