import os
import time
from datetime import datetime, timedelta, timezone
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

RESULT_SHEET_NAME = "NY_OPEN_5M_RESULTS"
TRADES_SHEET_NAME = "NY_OPEN_5M_TRADES"
RUN_LOG_SHEET_NAME = "NY_OPEN_5M_RUNLOG"

CACHE_FILE = "btc_5m_2022_2026.pkl"

# 미국 뉴욕장 첫 5분봉: 한국시간 22:30~22:35
# 주의: 미국 서머타임 기간에는 실제 뉴욕 현지 09:30이 한국시간 22:30이고,
# 겨울철에는 23:30이지만, 사용자가 말한 22:30~22:35 기준으로 고정.
OPEN_HOUR_KST = 22
OPEN_MINUTE_KST = 30

RISK_REWARD = 2.0

# 진입은 첫 5분봉 완성 후 당일 몇 시간까지만 찾을지
ENTRY_SEARCH_HOURS = 8

# 리테스트 허용 오차. 0.0005 = 0.05%
RETEST_TOLERANCE = 0.0005

# 돌파 후 바로 진입하지 않고, 레벨 재접촉 후 방향 확인 봉에서 진입
# True: 리테스트 봉의 종가가 레벨 위/아래로 닫혀야 진입
CONFIRM_CLOSE = True


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


def get_or_create_ws(spreadsheet, title, rows=3000, cols=40):
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
        raise RuntimeError("No data downloaded")

    df = df.drop_duplicates(subset=["time"]).reset_index(drop=True)

    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)

    df["datetime"] = pd.to_datetime(df["time"], unit="ms", utc=True).dt.tz_convert("Asia/Seoul")
    df["date"] = df["datetime"].dt.date

    return df


def load_data():
    start_dt = datetime.strptime(START_DATE, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end_dt = datetime.strptime(END_DATE, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    if os.path.exists(CACHE_FILE):
        print(f"Loading cached data: {CACHE_FILE}", flush=True)
        return pd.read_pickle(CACHE_FILE)

    df = fetch_klines(
        SYMBOL,
        Client.KLINE_INTERVAL_5MINUTE,
        start_dt,
        end_dt,
    )
    df.to_pickle(CACHE_FILE)
    return df


# =========================
# STRATEGY
# =========================

def net_after_fee(gross_pnl):
    return ((1 + gross_pnl / 100) * (1 - FEE_ROUND_TRIP / 100) - 1) * 100


def simulate_trade(day_df, entry_idx, side, entry_price, stop_price, target_price):
    """
    진입 이후 5분봉 OHLC 기준으로 TP/SL 먼저 닿는지 검사.
    같은 봉에서 TP와 SL이 동시에 닿으면 보수적으로 SL 우선 처리.
    """
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

    # 당일 탐색 구간 끝까지 TP/SL 미도달 시 마지막 종가 청산
    row = day_df.iloc[-1]
    exit_price = row["close"]
    exit_time = row["datetime"].strftime("%Y-%m-%d %H:%M:%S")

    if side == "LONG":
        gross_pnl = ((exit_price - entry_price) / entry_price) * 100
    else:
        gross_pnl = ((entry_price - exit_price) / entry_price) * 100

    return exit_time, exit_price, "TIME_EXIT", gross_pnl


def backtest_ny_open(df):
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
        open_idx = int(open_candle_df.index[0])

        range_high = float(open_candle["high"])
        range_low = float(open_candle["low"])
        midpoint = (range_high + range_low) / 2

        if range_high <= range_low:
            continue

        end_time = open_candle["datetime"] + timedelta(hours=ENTRY_SEARCH_HOURS)
        day_df = day_all[
            (day_all["datetime"] >= open_candle["datetime"])
            & (day_all["datetime"] <= end_time)
        ].reset_index(drop=True)

        # day_df 기준 첫 봉은 open_candle
        broke_up = False
        broke_down = False
        trade_taken = False

        for i in range(1, len(day_df)):
            row = day_df.iloc[i]
            high = row["high"]
            low = row["low"]
            close = row["close"]

            # 돌파 상태 기록
            if high > range_high:
                broke_up = True

            if low < range_low:
                broke_down = True

            # 롱: 고가 돌파 후 고가 라인 리테스트
            long_retest = (
                broke_up
                and low <= range_high * (1 + RETEST_TOLERANCE)
                and high >= range_high
            )

            if CONFIRM_CLOSE:
                long_retest = long_retest and close >= range_high

            if long_retest:
                entry_price = range_high
                stop_price = midpoint
                risk = entry_price - stop_price

                if risk > 0:
                    target_price = entry_price + risk * RISK_REWARD
                    exit_time, exit_price, exit_reason, gross_pnl = simulate_trade(
                        day_df, i, "LONG", entry_price, stop_price, target_price
                    )
                    net_pnl = net_after_fee(gross_pnl)
                    equity *= (1 + net_pnl / 100)
                    peak_equity = max(peak_equity, equity)
                    drawdown = ((equity - peak_equity) / peak_equity) * 100
                    max_drawdown = min(max_drawdown, drawdown)

                    trades.append({
                        "date": str(trade_date),
                        "side": "LONG",
                        "open_candle_time": open_candle["datetime"].strftime("%Y-%m-%d %H:%M:%S"),
                        "entry_time": row["datetime"].strftime("%Y-%m-%d %H:%M:%S"),
                        "exit_time": exit_time,
                        "range_high": round(range_high, 2),
                        "range_low": round(range_low, 2),
                        "midpoint": round(midpoint, 2),
                        "entry_price": round(entry_price, 2),
                        "stop_price": round(stop_price, 2),
                        "target_price": round(target_price, 2),
                        "exit_price": round(exit_price, 2),
                        "gross_pnl": round(gross_pnl, 4),
                        "net_pnl": round(net_pnl, 4),
                        "exit_reason": exit_reason,
                        "equity": round(equity, 4),
                        "max_drawdown": round(max_drawdown, 4),
                    })
                    trade_taken = True
                    break

            # 숏: 저가 이탈 후 저가 라인 리테스트
            short_retest = (
                broke_down
                and high >= range_low * (1 - RETEST_TOLERANCE)
                and low <= range_low
            )

            if CONFIRM_CLOSE:
                short_retest = short_retest and close <= range_low

            if short_retest:
                entry_price = range_low
                stop_price = midpoint
                risk = stop_price - entry_price

                if risk > 0:
                    target_price = entry_price - risk * RISK_REWARD
                    exit_time, exit_price, exit_reason, gross_pnl = simulate_trade(
                        day_df, i, "SHORT", entry_price, stop_price, target_price
                    )
                    net_pnl = net_after_fee(gross_pnl)
                    equity *= (1 + net_pnl / 100)
                    peak_equity = max(peak_equity, equity)
                    drawdown = ((equity - peak_equity) / peak_equity) * 100
                    max_drawdown = min(max_drawdown, drawdown)

                    trades.append({
                        "date": str(trade_date),
                        "side": "SHORT",
                        "open_candle_time": open_candle["datetime"].strftime("%Y-%m-%d %H:%M:%S"),
                        "entry_time": row["datetime"].strftime("%Y-%m-%d %H:%M:%S"),
                        "exit_time": exit_time,
                        "range_high": round(range_high, 2),
                        "range_low": round(range_low, 2),
                        "midpoint": round(midpoint, 2),
                        "entry_price": round(entry_price, 2),
                        "stop_price": round(stop_price, 2),
                        "target_price": round(target_price, 2),
                        "exit_price": round(exit_price, 2),
                        "gross_pnl": round(gross_pnl, 4),
                        "net_pnl": round(net_pnl, 4),
                        "exit_reason": exit_reason,
                        "equity": round(equity, 4),
                        "max_drawdown": round(max_drawdown, 4),
                    })
                    trade_taken = True
                    break

        if trade_taken:
            continue

    trades_df = pd.DataFrame(trades)

    if trades_df.empty:
        return pd.DataFrame([{
            "strategy": "NY_OPEN_5M_RETEST",
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
            "run_time": now_kst(),
        }]), trades_df

    wins = trades_df[trades_df["net_pnl"] > 0]
    losses = trades_df[trades_df["net_pnl"] <= 0]

    total_trades = len(trades_df)
    win_rate = len(wins) / total_trades * 100
    total_return = trades_df["equity"].iloc[-1] - 100
    max_drawdown = trades_df["max_drawdown"].min()
    avg_win = wins["net_pnl"].mean() if not wins.empty else 0
    avg_loss = losses["net_pnl"].mean() if not losses.empty else 0
    profit_factor = abs(wins["net_pnl"].sum() / losses["net_pnl"].sum()) if not losses.empty and losses["net_pnl"].sum() != 0 else 999
    exit_counts = trades_df["exit_reason"].value_counts().to_dict()

    summary_df = pd.DataFrame([{
        "strategy": "NY_OPEN_5M_RETEST",
        "start_date": START_DATE,
        "end_date": END_DATE,
        "symbol": SYMBOL,
        "open_time_kst": f"{OPEN_HOUR_KST:02d}:{OPEN_MINUTE_KST:02d}",
        "risk_reward": RISK_REWARD,
        "retest_tolerance": RETEST_TOLERANCE,
        "confirm_close": CONFIRM_CLOSE,
        "entry_search_hours": ENTRY_SEARCH_HOURS,
        "trades": total_trades,
        "win_rate": round(win_rate, 2),
        "total_return": round(total_return, 2),
        "max_drawdown": round(max_drawdown, 2),
        "avg_win": round(avg_win, 4),
        "avg_loss": round(avg_loss, 4),
        "profit_factor": round(profit_factor, 4),
        "tp_count": int(exit_counts.get("TAKE_PROFIT", 0)),
        "sl_count": int(exit_counts.get("STOP_LOSS", 0))
            + int(exit_counts.get("STOP_LOSS_SAME_CANDLE", 0)),
        "same_candle_sl_count": int(exit_counts.get("STOP_LOSS_SAME_CANDLE", 0)),
        "time_exit_count": int(exit_counts.get("TIME_EXIT", 0)),
        "final_equity": round(trades_df["equity"].iloc[-1], 4),
        "run_time": now_kst(),
    }])

    return summary_df, trades_df


# =========================
# MAIN
# =========================

def main():
    print("NY Open 5M Retest Backtest started:", now_kst(), flush=True)

    spreadsheet = init_gspread()
    result_ws = get_or_create_ws(spreadsheet, RESULT_SHEET_NAME, rows=100, cols=40)
    trades_ws = get_or_create_ws(spreadsheet, TRADES_SHEET_NAME, rows=10000, cols=40)
    log_ws = get_or_create_ws(spreadsheet, RUN_LOG_SHEET_NAME, rows=1000, cols=5)

    append_run_log(log_ws, "Backtest started")

    df = load_data()
    summary_df, trades_df = backtest_ny_open(df)

    clear_and_write(result_ws, list(summary_df.columns), summary_df.astype(str).values.tolist())

    if not trades_df.empty:
        clear_and_write(trades_ws, list(trades_df.columns), trades_df.astype(str).values.tolist())
    else:
        clear_and_write(trades_ws, ["message"], [["No trades"]])

    append_run_log(log_ws, "Backtest finished")
    print("NY Open 5M Retest Backtest finished:", now_kst(), flush=True)
    print("Saved summary to:", RESULT_SHEET_NAME, flush=True)
    print("Saved trades to:", TRADES_SHEET_NAME, flush=True)


if __name__ == "__main__":
    main()
