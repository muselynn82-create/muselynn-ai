import os
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from itertools import product

import pandas as pd
from binance.client import Client


API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_SECRET_KEY")
client = Client(API_KEY, API_SECRET)

SYMBOL = "BTCUSDT"
BACKTEST_DAYS = 365
FEE_ROUND_TRIP = 0.20
KST = ZoneInfo("Asia/Seoul")

OUTPUT_RESULTS = "optimizer_results.csv"
OUTPUT_TOP = "optimizer_top20.txt"
OUTPUT_TRADES_BEST = "optimizer_best_trades.csv"

PARAM_GRID = {
    "entry_score": [70, 75, 80, 85],
    "rsi_limit": [26, 28, 30, 32],
    "volume_ratio": [1.0, 1.2, 1.5],
    "take_profit": [1.2, 1.5, 1.8, 2.2],
    "stop_loss": [-0.5, -0.7, -1.0],
    "trail_start": [0.8, 1.0, 1.2],
    "trail_back": [0.4, 0.5, 0.7],
}

MIN_TRADES = 10
MAX_DRAWDOWN_LIMIT = -20.0


def now_kst():
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")


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
        time.sleep(0.08)

    df = pd.DataFrame(all_rows, columns=[
        "time", "open", "high", "low", "close", "volume",
        "close_time", "quote_asset_volume", "trades",
        "taker_buy_base", "taker_buy_quote", "ignore"
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


def detect_big_trend(h1, h4):
    if h1["atr_rate"] > 0.03 or h4["atr_rate"] > 0.055:
        return "BIG_CRASH"

    if h4["close"] > h4["ema200"] and h1["close"] > h1["ema50"]:
        return "BIG_BULL"

    if h4["close"] < h4["ema200"] and h1["close"] < h1["ema50"]:
        return "BIG_BEAR"

    return "BIG_SIDE"


def calculate_score(now, params):
    price = now["close"]
    score = 0

    if (
        now["rsi"] < params["rsi_limit"]
        and now["low"] <= now["bb_lower"]
        and now["close"] > now["open"]
    ):
        score += 70

    if price > now["ema100"]:
        score += 15

    if now["volume_ratio"] >= params["volume_ratio"]:
        score += 10

    if price >= now["ema20"] * 0.995:
        score += 10

    return score


def run_backtest(df_15m, df_1h, df_4h, params):
    position_open = False
    entry_price = 0.0
    entry_time = None
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
        current_time = now["datetime"]

        while i1 + 1 < len(df_1h_times) and df_1h_times[i1 + 1] <= current_time - timedelta(hours=1):
            i1 += 1

        while i4 + 1 < len(df_4h_times) and df_4h_times[i4 + 1] <= current_time - timedelta(hours=4):
            i4 += 1

        h1 = df_1h.iloc[i1]
        h4 = df_4h.iloc[i4]

        if pd.isna(now["rsi"]) or pd.isna(h1["ema200"]) or pd.isna(h4["ema200"]):
            continue

        big_trend = detect_big_trend(h1, h4)
        price = now["close"]

        if position_open:
            gross_pnl = ((price - entry_price) / entry_price) * 100
            net_pnl = ((1 + gross_pnl / 100) * (1 - FEE_ROUND_TRIP / 100) - 1) * 100

            if gross_pnl > max_pnl:
                max_pnl = gross_pnl

            exit_reason = None

            if gross_pnl <= params["stop_loss"]:
                exit_reason = "STOP_LOSS"
            elif net_pnl >= params["take_profit"]:
                exit_reason = "TAKE_PROFIT"
            elif (
                net_pnl >= 0.25
                and max_pnl >= params["trail_start"]
                and gross_pnl <= max_pnl - params["trail_back"]
            ):
                exit_reason = "TRAILING_STOP"
            elif big_trend == "BIG_CRASH":
                exit_reason = "BIG_CRASH_EXIT"

            if exit_reason:
                equity *= (1 + net_pnl / 100)
                peak_equity = max(peak_equity, equity)
                drawdown = ((equity - peak_equity) / peak_equity) * 100
                max_drawdown = min(max_drawdown, drawdown)

                trades.append({
                    "entry_time": entry_time,
                    "exit_time": current_time.strftime("%Y-%m-%d %H:%M:%S"),
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
                entry_score = 0
                max_pnl = 0.0
                last_exit_time = current_time

        if not position_open and big_trend == "BIG_BULL":
            in_cooldown = False
            if last_exit_time:
                cooldown_minutes = (current_time - last_exit_time).total_seconds() / 60
                in_cooldown = cooldown_minutes < 3

            score = calculate_score(now, params)

            if not in_cooldown and score >= params["entry_score"]:
                position_open = True
                entry_price = price
                entry_time = current_time.strftime("%Y-%m-%d %H:%M:%S")
                entry_score = score
                max_pnl = 0.0

    return pd.DataFrame(trades), max_drawdown


def run_stats(df_15m, df_1h, df_4h, params):
    trades_df, max_drawdown = run_backtest(df_15m, df_1h, df_4h, params)

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
            "score_rank": -9999,
        }

    wins = trades_df[trades_df["net_pnl"] > 0]
    losses = trades_df[trades_df["net_pnl"] <= 0]

    total_trades = len(trades_df)
    win_rate = len(wins) / total_trades * 100
    total_return = trades_df["equity"].iloc[-1] - 100
    avg_win = wins["net_pnl"].mean() if not wins.empty else 0
    avg_loss = losses["net_pnl"].mean() if not losses.empty else 0
    profit_factor = abs(wins["net_pnl"].sum() / losses["net_pnl"].sum()) if not losses.empty and losses["net_pnl"].sum() != 0 else 999

    score_rank = (
        profit_factor * 100
        + total_return * 2
        + win_rate * 0.5
        + max_drawdown * 2
    )

    if total_trades < MIN_TRADES:
        score_rank -= 100

    if max_drawdown < MAX_DRAWDOWN_LIMIT:
        score_rank -= 200

    return {
        **params,
        "trades": total_trades,
        "win_rate": round(win_rate, 2),
        "total_return": round(total_return, 2),
        "max_drawdown": round(max_drawdown, 2),
        "avg_win": round(avg_win, 4),
        "avg_loss": round(avg_loss, 4),
        "profit_factor": round(profit_factor, 4),
        "score_rank": round(score_rank, 4),
    }


def main():
    print("Optimizer started:", now_kst(), flush=True)

    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=BACKTEST_DAYS + 45)

    df_15m = calculate_indicators(fetch_klines(SYMBOL, Client.KLINE_INTERVAL_15MINUTE, start_dt, end_dt))
    df_1h = calculate_indicators(fetch_klines(SYMBOL, Client.KLINE_INTERVAL_1HOUR, start_dt, end_dt))
    df_4h = calculate_indicators(fetch_klines(SYMBOL, Client.KLINE_INTERVAL_4HOUR, start_dt, end_dt))

    cutoff = datetime.now(KST) - timedelta(days=BACKTEST_DAYS)
    df_15m = df_15m[df_15m["datetime"] >= cutoff].reset_index(drop=True)

    keys = list(PARAM_GRID.keys())
    combos = list(product(*[PARAM_GRID[k] for k in keys]))
    print(f"Total combinations: {len(combos)}", flush=True)

    rows = []
    best_score = -999999
    best_params = None

    for idx, values in enumerate(combos, start=1):
        params = dict(zip(keys, values))
        result = run_stats(df_15m, df_1h, df_4h, params)
        rows.append(result)

        if result["score_rank"] > best_score:
            best_score = result["score_rank"]
            best_params = params
            print(
                f"[NEW BEST {idx}/{len(combos)}] "
                f"PF={result['profit_factor']} "
                f"Return={result['total_return']}% "
                f"MDD={result['max_drawdown']}% "
                f"Trades={result['trades']} "
                f"Params={best_params}",
                flush=True,
            )

        if idx % 50 == 0:
            print(f"Progress: {idx}/{len(combos)}", flush=True)

    results_df = pd.DataFrame(rows)
    results_df = results_df.sort_values(
        by=["score_rank", "profit_factor", "total_return"],
        ascending=False,
    )

    results_df.to_csv(OUTPUT_RESULTS, index=False, encoding="utf-8-sig")

    top20 = results_df.head(20)

    with open(OUTPUT_TOP, "w", encoding="utf-8") as f:
        f.write("========== OPTIMIZER TOP 20 ==========\n")
        f.write(top20.to_string(index=False))
        f.write("\n")

    if not top20.empty:
        best = top20.iloc[0].to_dict()
        best_params = {k: best[k] for k in keys}
        best_trades, _ = run_backtest(df_15m, df_1h, df_4h, best_params)
        best_trades.to_csv(OUTPUT_TRADES_BEST, index=False, encoding="utf-8-sig")

    print("\n========== OPTIMIZER TOP 20 ==========", flush=True)
    print(top20.to_string(index=False), flush=True)
    print("")
    print(f"Saved: {OUTPUT_RESULTS}", flush=True)
    print(f"Saved: {OUTPUT_TOP}", flush=True)
    print(f"Saved: {OUTPUT_TRADES_BEST}", flush=True)
    print("Optimizer finished:", now_kst(), flush=True)


if __name__ == "__main__":
    main()
