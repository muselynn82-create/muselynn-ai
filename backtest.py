import os
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
from binance.client import Client


# =========================
# BACKTEST CONFIG
# =========================

API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_SECRET_KEY")

client = Client(API_KEY, API_SECRET)

SYMBOL = "BTCUSDT"

BACKTEST_DAYS = 365
FEE_ROUND_TRIP = 0.20

ENTRY_SCORE = 60
MIN_HOLD_MINUTES = 5
MAX_CONSECUTIVE_LOSSES = 5

KST = ZoneInfo("Asia/Seoul")

OUTPUT_TRADES = "backtest_trades.csv"
OUTPUT_EQUITY = "backtest_equity.csv"
OUTPUT_SUMMARY = "backtest_summary.txt"


# =========================
# TIME HELPERS
# =========================

def now_kst():
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")


def ms_to_kst(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(KST)


def dt_to_ms(dt):
    return int(dt.timestamp() * 1000)


# =========================
# BINANCE DATA
# =========================

def fetch_klines(symbol, interval, start_dt, end_dt):
    print(f"Downloading {symbol} {interval} data...")

    all_rows = []
    start_ms = dt_to_ms(start_dt)
    end_ms = dt_to_ms(end_dt)

    while start_ms < end_ms:
        candles = client.get_klines(
            symbol=symbol,
            interval=interval,
            startTime=start_ms,
            endTime=end_ms,
            limit=1000
        )

        if not candles:
            break

        all_rows.extend(candles)

        last_open_time = candles[-1][0]
        start_ms = last_open_time + 1

        time.sleep(0.15)

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


# =========================
# INDICATORS
# =========================

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

    df["prev_close"] = close.shift(1)
    df["tr1"] = high - low
    df["tr2"] = (high - df["prev_close"]).abs()
    df["tr3"] = (low - df["prev_close"]).abs()
    df["tr"] = df[["tr1", "tr2", "tr3"]].max(axis=1)
    df["atr"] = df["tr"].rolling(14).mean()
    df["atr_rate"] = df["atr"] / close

    df["volume_ma"] = df["volume"].rolling(20).mean()
    df["volume_ratio"] = df["volume"] / df["volume_ma"]

    return df


# =========================
# STRATEGY LOGIC
# =========================

def detect_big_trend(h1, h4):

    if h1["atr_rate"] > 0.03 or h4["atr_rate"] > 0.055:
        return "BIG_CRASH"

    # 상승추세: 4시간 장기 상승 + 1시간 단기 지지
    if h4["close"] > h4["ema200"] and h1["close"] > h1["ema50"]:
        return "BIG_BULL"

    # 하락추세: 4시간 장기 하락 + 1시간 단기 약세
    if h4["close"] < h4["ema200"] and h1["close"] < h1["ema50"]:
        return "BIG_BEAR"

    return "BIG_SIDE"


def detect_short_market(now):
    price = now["close"]

    if now["atr_rate"] > 0.014 or now["bb_width"] > 0.065:
        return "VOLATILE"

    if price > now["ema20"] > now["ema50"] > now["ema100"]:
        return "BULL"

    if price < now["ema20"] < now["ema50"] < now["ema100"]:
        return "BEAR"

    return "SIDE"


def get_strategy(big_trend, market):
    if big_trend == "BIG_CRASH":
        return "NO_TRADE_CRASH"

    if big_trend == "BIG_BULL":
        if market in ["BULL", "SIDE"]:
            return "BULL_PULLBACK"
        if market == "BEAR":
            return "BULL_DEEP_PULLBACK"

    if big_trend == "BIG_SIDE":
        if market == "SIDE":
            return "SIDE_RSI_BB"
        if market == "BULL":
            return "BULL_PULLBACK_LIGHT"
        if market == "BEAR":
            return "SIDE_DEEP_REBOUND"

    if big_trend == "BIG_BEAR":
        return "NO_TRADE"

    return "NO_TRADE"


def calculate_score(now, prev, big_trend, market, strategy):
    price = now["close"]
    rsi = now["rsi"]
    volume_ratio = now["volume_ratio"]

    score = 0

    if strategy == "SIDE_RSI_BB":
        if rsi < 35:
            score += 25
        if price <= now["bb_lower"] * 1.004:
            score += 25
        if price <= now["bb_lower"] * 1.003:
            score += 20
        if market == "SIDE":
            score += 15
        if volume_ratio >= 0.9:
            score += 15

    elif strategy == "SIDE_DEEP_REBOUND":
        if rsi < 28:
            score += 35
        if price <= now["bb_lower"] * 1.003:
            score += 30
        if volume_ratio >= 0.9:
            score += 15

    elif strategy == "BULL_PULLBACK":
        if 35 <= rsi <= 55:
            score += 35
        if price > now["ema50"]:
            score += 25
        if price <= now["ema20"] * 1.004:
            score += 25
        if big_trend == "BIG_BULL":
            score += 15
        if volume_ratio >= 1.0:
            score += 15

    elif strategy == "BULL_PULLBACK_LIGHT":
        if 38 <= rsi <= 55:
            score += 30
        if price > now["ema50"]:
            score += 25
        if price <= now["ema20"] * 1.004:
            score += 20
        if volume_ratio >= 0.9:
            score += 10

    elif strategy == "BULL_DEEP_PULLBACK":
        if rsi < 30:
            score += 40
        if price <= now["bb_lower"] * 1.004:
            score += 30
        if price > now["ema100"]:
            score += 20
        if volume_ratio >= 1.0:
            score += 15

    elif strategy == "BEAR_SCALP":
        if rsi < 30:
            score += 35
        if price <= now["bb_lower"] * 1.006:
            score += 30
        if volume_ratio >= 0.9:
            score += 20
        if price > now["bb_lower"]:
            score += 15

    return score


def get_risk_params(strategy):
    if strategy == "SIDE_RSI_BB":
        return {
            "take_profit": 0.90,
            "stop_loss": -0.25,
            "trail_start": 0.55,
            "trail_back": 0.28
        }

    if strategy == "SIDE_DEEP_REBOUND":
        return {
            "take_profit": 0.80,
            "stop_loss": -0.25,
            "trail_start": 0.50,
            "trail_back": 0.25
        }

    if strategy == "BULL_PULLBACK":
        return {
            "take_profit": 1.50,
            "stop_loss": -0.35,
            "trail_start": 0.90,
            "trail_back": 0.40
        }

    if strategy == "BULL_PULLBACK_LIGHT":
        return {
            "take_profit": 1.10,
            "stop_loss": -0.30,
            "trail_start": 0.70,
            "trail_back": 0.32
        }

    if strategy == "BULL_DEEP_PULLBACK":
        return {
            "take_profit": 1.10,
            "stop_loss": -0.30,
            "trail_start": 0.70,
            "trail_back": 0.32
        }

    if strategy == "BEAR_SCALP":
        return {
            "take_profit": 0.70,
            "stop_loss": -0.22,
            "trail_start": 0.45,
            "trail_back": 0.22
        }

    return {
        "take_profit": 0,
        "stop_loss": 0,
        "trail_start": 0,
        "trail_back": 0
    }


def get_min_net_for_trailing(strategy):
    return {
        "SIDE_RSI_BB": 0.20,
        "SIDE_DEEP_REBOUND": 0.15,
        "BULL_PULLBACK": 0.35,
        "BULL_PULLBACK_LIGHT": 0.25,
        "BULL_DEEP_PULLBACK": 0.25,
        "BEAR_SCALP": 0.12
    }.get(strategy, 0.20)


# =========================
# BACKTEST ENGINE
# =========================

def run_backtest(df_5m, df_1h, df_4h):
    position_open = False
    entry_price = 0.0
    entry_time = None
    entry_strategy = None
    entry_big_trend = None
    entry_market = None
    entry_score = 0

    entry_take_profit = 0.0
    entry_stop_loss = 0.0
    entry_trail_start = 0.0
    entry_trail_back = 0.0

    max_pnl = 0.0

    last_exit_time = None
    consecutive_losses = 0
    strategy_enabled = True

    total_pnl = 0.0
    equity = 100.0
    peak_equity = 100.0
    max_drawdown = 0.0

    trades = []
    equity_rows = []

    df_1h_times = df_1h["datetime"].tolist()
    df_4h_times = df_4h["datetime"].tolist()

    i1 = 0
    i4 = 0

    for i in range(220, len(df_5m)):
        now = df_5m.iloc[i]
        prev = df_5m.iloc[i - 1]
        current_time = now["datetime"]

        while i1 + 1 < len(df_1h_times) and df_1h_times[i1 + 1] <= current_time:
            i1 += 1

        while i4 + 1 < len(df_4h_times) and df_4h_times[i4 + 1] <= current_time:
            i4 += 1

        h1 = df_1h.iloc[i1]
        h4 = df_4h.iloc[i4]

        if pd.isna(now["rsi"]) or pd.isna(h1["ema200"]) or pd.isna(h4["ema200"]):
            continue

        big_trend = detect_big_trend(h1, h4)
        market = detect_short_market(now)
        strategy = get_strategy(big_trend, market)
        score = calculate_score(now, prev, big_trend, market, strategy)

        price = now["close"]

        # EXIT
        if position_open:
            gross_pnl = ((price - entry_price) / entry_price) * 100
            net_pnl = gross_pnl - FEE_ROUND_TRIP

            if gross_pnl > max_pnl:
                max_pnl = gross_pnl

            exit_reason = None

            if gross_pnl <= entry_stop_loss:
                exit_reason = "STOP_LOSS"

            elif net_pnl >= entry_take_profit:
                exit_reason = "TAKE_PROFIT"

            elif (
                net_pnl >= get_min_net_for_trailing(entry_strategy) and
                max_pnl >= entry_trail_start and
                gross_pnl <= max_pnl - entry_trail_back
            ):
                exit_reason = "TRAILING_STOP"

            elif big_trend == "BIG_CRASH":
                exit_reason = "BIG_CRASH_EXIT"

            if exit_reason:
                total_pnl += net_pnl
                equity *= (1 + net_pnl / 100)

                peak_equity = max(peak_equity, equity)
                drawdown = ((equity - peak_equity) / peak_equity) * 100
                max_drawdown = min(max_drawdown, drawdown)

                if net_pnl > 0:
                    consecutive_losses = 0
                else:
                    consecutive_losses += 1

                trades.append({
                    "entry_time": entry_time,
                    "exit_time": current_time.strftime("%Y-%m-%d %H:%M:%S"),
                    "strategy": entry_strategy,
                    "entry_big_trend": entry_big_trend,
                    "exit_big_trend": big_trend,
                    "entry_market": entry_market,
                    "exit_market": market,
                    "entry_score": entry_score,
                    "exit_score": score,
                    "entry_price": round(entry_price, 2),
                    "exit_price": round(price, 2),
                    "gross_pnl": round(gross_pnl, 4),
                    "net_pnl": round(net_pnl, 4),
                    "max_pnl": round(max_pnl, 4),
                    "exit_reason": exit_reason,
                    "equity": round(equity, 4),
                    "consecutive_losses": consecutive_losses
                })

                last_exit_time = current_time

                position_open = False
                entry_price = 0.0
                entry_time = None
                entry_strategy = None
                entry_big_trend = None
                entry_market = None
                entry_score = 0

                entry_take_profit = 0.0
                entry_stop_loss = 0.0
                entry_trail_start = 0.0
                entry_trail_back = 0.0

                max_pnl = 0.0

                # if consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
                #     strategy_enabled = False

        # ENTRY
        if not position_open and strategy_enabled:
            in_cooldown = False
            if last_exit_time:
                cooldown_minutes = (current_time - last_exit_time).total_seconds() / 60
                in_cooldown = cooldown_minutes < 3

            if (
                not in_cooldown and
                not strategy.startswith("NO_TRADE") and
                score >= ENTRY_SCORE
            ):
                params = get_risk_params(strategy)

                position_open = True
                entry_price = price
                entry_time = current_time.strftime("%Y-%m-%d %H:%M:%S")
                entry_strategy = strategy
                entry_big_trend = big_trend
                entry_market = market
                entry_score = score

                entry_take_profit = params["take_profit"]
                entry_stop_loss = params["stop_loss"]
                entry_trail_start = params["trail_start"]
                entry_trail_back = params["trail_back"]

                max_pnl = 0.0

        equity_rows.append({
            "time": current_time.strftime("%Y-%m-%d %H:%M:%S"),
            "equity": round(equity, 4),
            "position_open": position_open,
            "price": round(price, 2),
            "big_trend": big_trend,
            "market": market,
            "strategy": strategy,
            "score": score
        })

    trades_df = pd.DataFrame(trades)
    equity_df = pd.DataFrame(equity_rows)

    return trades_df, equity_df, max_drawdown


# =========================
# SUMMARY
# =========================

def create_summary(trades_df, equity_df, max_drawdown):
    if trades_df.empty:
        return "No trades generated."

    total_trades = len(trades_df)
    wins = trades_df[trades_df["net_pnl"] > 0]
    losses = trades_df[trades_df["net_pnl"] <= 0]

    win_rate = len(wins) / total_trades * 100 if total_trades else 0
    total_return = equity_df["equity"].iloc[-1] - 100
    avg_win = wins["net_pnl"].mean() if not wins.empty else 0
    avg_loss = losses["net_pnl"].mean() if not losses.empty else 0
    profit_factor = abs(wins["net_pnl"].sum() / losses["net_pnl"].sum()) if not losses.empty and losses["net_pnl"].sum() != 0 else 999

    by_strategy = trades_df.groupby("strategy")["net_pnl"].agg(["count", "sum", "mean"]).sort_values("sum", ascending=False)
    by_reason = trades_df.groupby("exit_reason")["net_pnl"].agg(["count", "sum", "mean"]).sort_values("sum", ascending=False)
    by_big_trend = trades_df.groupby("entry_big_trend")["net_pnl"].agg(["count", "sum", "mean"]).sort_values("sum", ascending=False)

    summary = []
    summary.append("========== BACKTEST SUMMARY ==========")
    summary.append(f"Symbol: {SYMBOL}")
    summary.append(f"Days: {BACKTEST_DAYS}")
    summary.append(f"Fee round trip: {FEE_ROUND_TRIP}%")
    summary.append("")
    summary.append(f"Total trades: {total_trades}")
    summary.append(f"Win rate: {win_rate:.2f}%")
    summary.append(f"Total return: {total_return:.2f}%")
    summary.append(f"Max drawdown: {max_drawdown:.2f}%")
    summary.append(f"Avg win: {avg_win:.4f}%")
    summary.append(f"Avg loss: {avg_loss:.4f}%")
    summary.append(f"Profit factor: {profit_factor:.4f}")
    summary.append("")
    summary.append("----- By Strategy -----")
    summary.append(by_strategy.to_string())
    summary.append("")
    summary.append("----- By Exit Reason -----")
    summary.append(by_reason.to_string())
    summary.append("")
    summary.append("----- By Big Trend -----")
    summary.append(by_big_trend.to_string())

    return "\n".join(summary)


# =========================
# MAIN
# =========================

def main():
    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=BACKTEST_DAYS + 45)

    print("Backtest started:", now_kst())

    df_5m = calculate_indicators(fetch_klines(SYMBOL, Client.KLINE_INTERVAL_5MINUTE, start_dt, end_dt))
    df_1h = calculate_indicators(fetch_klines(SYMBOL, Client.KLINE_INTERVAL_1HOUR, start_dt, end_dt))
    df_4h = calculate_indicators(fetch_klines(SYMBOL, Client.KLINE_INTERVAL_4HOUR, start_dt, end_dt))

    cutoff = datetime.now(KST) - timedelta(days=BACKTEST_DAYS)
    df_5m = df_5m[df_5m["datetime"] >= cutoff].reset_index(drop=True)

    trades_df, equity_df, max_drawdown = run_backtest(df_5m, df_1h, df_4h)

    trades_df.to_csv(OUTPUT_TRADES, index=False, encoding="utf-8-sig")
    equity_df.to_csv(OUTPUT_EQUITY, index=False, encoding="utf-8-sig")

    summary = create_summary(trades_df, equity_df, max_drawdown)

    with open(OUTPUT_SUMMARY, "w", encoding="utf-8") as f:
        f.write(summary)

    print(summary)
    print("")
    print(f"Saved: {OUTPUT_TRADES}")
    print(f"Saved: {OUTPUT_EQUITY}")
    print(f"Saved: {OUTPUT_SUMMARY}")
    print("Backtest finished:", now_kst())


if __name__ == "__main__":
    main()
