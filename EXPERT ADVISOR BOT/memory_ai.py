# =========================================
# memory_ai.py — Trade Memory CSV
# =========================================

import csv
import datetime
import os

FILE_NAME = "trade_memory.csv"
HEADERS = [
    "time", "pair", "side", "trade_type", "mode",
    "spread", "trend_score", "pattern", "volatility",
    "balance", "session", "news_mode", "news_bias",
    "result", "profit",
    # ---- added for dashboard: risk/duration analytics ----
    "ticket", "lot", "entry_price", "sl_price", "exit_price",
    "duration_min", "rr"
]


def extract_ticket(result) -> int:
    """Pulls the MT5 ticket out of place_trade()/place_limit_order()'s
    success string, e.g. '✅ TRADE OPENED EURUSD #123456' -> 123456.
    Returns 0 if not found (never raises — dashboard fields just show 0)."""
    try:
        s = str(result)
        if "#" not in s:
            return 0
        return int(s.rsplit("#", 1)[-1].strip())
    except Exception:
        return 0


def init_memory():
    if not os.path.exists(FILE_NAME):
        with open(FILE_NAME, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(HEADERS)
        print("🧠 trade_memory.csv created")
        return

    # ---- Migrate old header (fewer columns) to new schema ----
    # Existing data rows are left untouched — pandas/csv.DictReader treat
    # missing trailing fields on old rows as blank/NaN automatically, so
    # this is safe and non-destructive.
    try:
        with open(FILE_NAME, "r", encoding="utf-8") as f:
            first_line = f.readline().strip()
        current_header = first_line.split(",")
        if current_header != HEADERS and current_header[:1] == ["time"]:
            with open(FILE_NAME, "r", encoding="utf-8") as f:
                rest = f.read().split("\n", 1)
                remainder = rest[1] if len(rest) > 1 else ""
            with open(FILE_NAME, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(HEADERS)
                f.write(remainder)
            print("🧠 trade_memory.csv header migrated to new schema "
                  "(ticket/lot/entry/sl/exit/duration/rr added)")
    except Exception as e:
        print(f"⚠️ trade_memory.csv header migration check failed: {e}")


def get_session():
    h = datetime.datetime.now().hour
    if 5  <= h < 12: return "ASIAN"
    if 12 <= h < 17: return "LONDON"
    if 17 <= h < 22: return "NEWYORK"
    return "OFF"


def save_trade_memory(
    pair, spread, trend_score, pattern,
    result, profit, balance, volatility,
    trade_type, side, mode,
    session=None, news_mode="NORMAL", news_bias="NEUTRAL",
    ticket=0, lot=0.0, entry_price=0.0, sl_price=0.0,
    exit_price=0.0, duration_min=0.0, rr=0.0
):
    init_memory()
    try:
        row = [
            str(datetime.datetime.now()),
            str(pair), str(side), str(trade_type), str(mode),
            round(float(spread), 5), round(float(trend_score), 2),
            str(pattern), round(float(volatility), 5),
            round(float(balance), 2),
            str(session or get_session()),
            str(news_mode), str(news_bias),
            str(result), round(float(profit), 2),
            int(ticket) if ticket else 0,
            round(float(lot), 2),
            round(float(entry_price), 5),
            round(float(sl_price), 5),
            round(float(exit_price), 5),
            round(float(duration_min), 2),
            round(float(rr), 2),
        ]
        with open(FILE_NAME, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(row)
        print(f"🧠 Memory saved | {pair} | {mode} | {result}")
    except Exception as e:
        print("❌ Save trade failed:", e)


def ai_probability(pair, trend_score, pattern):
    p = 0.50 + trend_score * 0.30
    if pattern in ["BULLISH_ENGULFING", "BEARISH_ENGULFING"]: p += 0.15
    elif pattern in ["BULLISH_PINBAR", "BEARISH_PINBAR"]:     p += 0.12
    else:                                                      p += 0.05
    if ("GOLD" in pair or "XAU" in pair) and trend_score >= 0.70: p += 0.10
    if trend_score >= 0.85:                                         p += 0.05
    return round(min(max(p, 0.05), 0.95), 2)


def mode_summary():
    if not os.path.exists(FILE_NAME):
        return {}
    stats = {m: {"trades": 0, "wins": 0, "losses": 0, "profit": 0}
             for m in ["SCALPING", "INTRADAY", "SWING", "RED_NEWS"]}
    try:
        with open(FILE_NAME, "r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("result") not in {"WIN", "LOSS", "BE"}:
                    continue
                mode = row.get("mode", "UNKNOWN")
                if mode not in stats:
                    continue
                try:    profit = float(row.get("profit", 0))
                except: profit = 0
                stats[mode]["trades"] += 1
                stats[mode]["profit"] += profit
                if profit > 0: stats[mode]["wins"]   += 1
                else:          stats[mode]["losses"] += 1
        for m in stats:
            t = stats[m]["trades"]
            stats[m]["win_rate"] = round(stats[m]["wins"] / t * 100, 2) if t else 0
    except Exception as e:
        print("❌ Mode summary failed:", e)
    return stats
