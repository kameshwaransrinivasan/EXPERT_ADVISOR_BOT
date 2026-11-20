# =========================================
# analytics.py — Performance Dashboard Engine
#
# Computes every dashboard section (overall performance, risk, trading
# stats, session/symbol/pattern breakdowns, error analysis, AI
# suggestions, chart series) from trade_memory.csv + error_log.csv for
# an arbitrary date range. Used by email_report.py to build the daily/
# weekly/monthly emails.
#
# NOTE on scope: "RR" and "$ risk per trade" require entry/SL/exit
# prices, which are only captured for trades opened AFTER this update
# (ticket/lot/entry_price/sl_price/exit_price/duration_min/rr columns).
# Older rows will show 0 for these — they're excluded from those
# specific averages (not counted as zeros) so historic gaps don't skew
# the numbers, but they DO still count in win/loss/profit totals.
# =========================================

import os
import calendar
import datetime
import pandas as pd
import numpy as np

from ai_learning import load_closed_df, mode_analysis

ERROR_FILE = "error_log.csv"


# =========================================
# DATE RANGE HELPERS (calendar-aligned, not rolling windows)
# =========================================
def get_previous_day_range(now: datetime.datetime):
    """Yesterday 00:00 -> today 00:00 (the full calendar day that just ended)."""
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start = today_start - datetime.timedelta(days=1)
    return start, today_start


def get_current_week_range(now: datetime.datetime):
    """Monday 00:00 of this week -> now. Sent Saturday, this spans the
    just-finished Mon-Fri trading week (forex closes Fri evening)."""
    monday = now.replace(hour=0, minute=0, second=0, microsecond=0) - datetime.timedelta(days=now.weekday())
    return monday, now


def get_previous_month_range(now: datetime.datetime):
    """1st -> last day of the PREVIOUS calendar month (the month that
    just completed — correct for a report sent on the 1st)."""
    first_of_this_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last_month_end = first_of_this_month
    last_month_start = (first_of_this_month - datetime.timedelta(days=1)).replace(day=1)
    return last_month_start, last_month_end


def filter_range(df: pd.DataFrame, start, end) -> pd.DataFrame:
    if df.empty:
        return df
    d = df.copy()
    d["time"] = pd.to_datetime(d["time"], errors="coerce")
    start = pd.Timestamp(start)
    end   = pd.Timestamp(end)
    return d[(d["time"] >= start) & (d["time"] < end)]


# =========================================
# DATA LOADING (extends ai_learning's loader with the new numeric cols)
# =========================================
def load_dashboard_df() -> pd.DataFrame:
    df = load_closed_df()
    if df.empty:
        return df
    for col in ["ticket", "lot", "entry_price", "sl_price",
                "exit_price", "duration_min", "rr"]:
        if col not in df.columns:
            df[col] = 0
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
    return df


def load_errors_df() -> pd.DataFrame:
    if not os.path.exists(ERROR_FILE):
        return pd.DataFrame()
    try:
        df = pd.read_csv(ERROR_FILE)
        if df.empty:
            return df
        df["time"] = pd.to_datetime(df["time"], errors="coerce")
        return df
    except Exception as e:
        print(f"❌ load_errors_df: {e}")
        return pd.DataFrame()


# =========================================
# 📊 OVERALL PERFORMANCE
# =========================================
def overall_performance(df: pd.DataFrame) -> dict:
    if df.empty:
        return {"total_trades": 0, "wins": 0, "losses": 0, "win_rate": 0,
                "total_profit": 0, "total_loss": 0, "net_profit": 0,
                "profit_factor": None}

    wins_df   = df[df["profit"] > 0]
    losses_df = df[df["profit"] < 0]
    total     = len(df)
    total_profit = round(wins_df["profit"].sum(), 2)
    total_loss   = round(losses_df["profit"].sum(), 2)   # negative number
    net_profit   = round(df["profit"].sum(), 2)

    profit_factor = (round(total_profit / abs(total_loss), 2)
                      if total_loss != 0 else None)  # None = no losses yet (undefined, not infinite)

    return {
        "total_trades": total,
        "wins": len(wins_df),
        "losses": len(losses_df),
        "win_rate": round(len(wins_df) / total * 100, 2) if total else 0,
        "total_profit": total_profit,
        "total_loss": total_loss,
        "net_profit": net_profit,
        "profit_factor": profit_factor,
    }


# =========================================
# 💰 RISK ANALYSIS
# =========================================
def risk_analysis(df: pd.DataFrame) -> dict:
    if df.empty:
        return {"avg_rr": None, "risk_per_trade_pct": None, "risk_per_trade_usd": None,
                "max_drawdown_pct": None, "largest_win": 0, "largest_loss": 0,
                "daily_loss_breakdown": [], "weekly_loss_breakdown": []}

    # Avg RR — only over trades where we actually have entry/sl/exit data
    rr_df = df[df["rr"] != 0]
    avg_rr = round(rr_df["rr"].mean(), 2) if not rr_df.empty else None

    # Risk per trade, estimated from realized losses (the most reliable
    # proxy available without per-symbol contract-size data): with a
    # mandatory fixed SL and no partial-risk exits, a losing trade's
    # realized loss approximates what was actually risked.
    losses_df = df[df["profit"] < 0]
    risk_usd = round(abs(losses_df["profit"].mean()), 2) if not losses_df.empty else None
    risk_pct = None
    if not losses_df.empty and "balance" in df.columns:
        nonzero_bal = losses_df[losses_df["balance"] > 0]
        if not nonzero_bal.empty:
            risk_pct = round((nonzero_bal["profit"].abs() / nonzero_bal["balance"] * 100).mean(), 2)

    # Max drawdown — from the balance column (real running balance snapshots)
    max_dd = None
    if "balance" in df.columns:
        bal = df.sort_values("time")["balance"]
        bal = bal[bal > 0]
        if len(bal) >= 2:
            running_peak = bal.cummax()
            dd_pct = (running_peak - bal) / running_peak * 100
            max_dd = round(dd_pct.max(), 2)

    largest_win  = round(df["profit"].max(), 2) if not df.empty else 0
    largest_loss = round(df["profit"].min(), 2) if not df.empty else 0

    # Loss breakdown by day (useful in weekly/monthly reports)
    daily = df.copy()
    daily["date"] = pd.to_datetime(daily["time"], errors="coerce").dt.date
    daily_loss = (daily[daily["profit"] < 0].groupby("date")["profit"].sum()
                  .round(2).sort_index())
    daily_loss_breakdown = [(str(d), v) for d, v in daily_loss.items()]

    weekly = df.copy()
    weekly["time"] = pd.to_datetime(weekly["time"], errors="coerce")
    weekly["iso_week"] = weekly["time"].dt.strftime("%G-W%V")
    weekly_loss = (weekly[weekly["profit"] < 0].groupby("iso_week")["profit"].sum()
                   .round(2).sort_index())
    weekly_loss_breakdown = [(w, v) for w, v in weekly_loss.items()]

    return {
        "avg_rr": avg_rr,
        "risk_per_trade_pct": risk_pct,
        "risk_per_trade_usd": risk_usd,
        "max_drawdown_pct": max_dd,
        "largest_win": largest_win,
        "largest_loss": largest_loss,
        "daily_loss_breakdown": daily_loss_breakdown,
        "weekly_loss_breakdown": weekly_loss_breakdown,
    }


# =========================================
# 📈 TRADING STATISTICS
# =========================================
def trading_statistics(df: pd.DataFrame) -> dict:
    if df.empty:
        return {"avg_win": 0, "avg_loss": 0, "max_consec_wins": 0,
                "max_consec_losses": 0, "avg_duration_min": None,
                "total_trading_hours": None}

    wins_df   = df[df["profit"] > 0]
    losses_df = df[df["profit"] < 0]
    avg_win  = round(wins_df["profit"].mean(), 2) if not wins_df.empty else 0
    avg_loss = round(losses_df["profit"].mean(), 2) if not losses_df.empty else 0

    # Consecutive win/loss streaks, in chronological order
    ordered = df.sort_values("time")
    streak_max_w = streak_max_l = cur_w = cur_l = 0
    for p in ordered["profit"]:
        if p > 0:
            cur_w += 1; cur_l = 0
        elif p < 0:
            cur_l += 1; cur_w = 0
        else:
            cur_w = cur_l = 0
        streak_max_w = max(streak_max_w, cur_w)
        streak_max_l = max(streak_max_l, cur_l)

    dur_df = df[df["duration_min"] > 0]
    avg_duration = round(dur_df["duration_min"].mean(), 1) if not dur_df.empty else None
    total_hours  = round(dur_df["duration_min"].sum() / 60, 1) if not dur_df.empty else None

    return {
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "max_consec_wins": streak_max_w,
        "max_consec_losses": streak_max_l,
        "avg_duration_min": avg_duration,
        "total_trading_hours": total_hours,
    }


# =========================================
# ⏰ SESSION / 💱 SYMBOL / 🧠 PATTERN ANALYSIS
# (Generic groupby table used for all three)
# =========================================
def _group_table(df: pd.DataFrame, col: str) -> list:
    if df.empty or col not in df.columns:
        return []
    rows = []
    for key, grp in df.groupby(col):
        trades = len(grp)
        wins   = len(grp[grp["profit"] > 0])
        wr     = round(wins / trades * 100, 1) if trades else 0
        profit = round(grp["profit"].sum(), 2)
        rows.append({"name": key, "trades": trades, "win_rate": wr, "profit": profit})
    rows.sort(key=lambda r: r["profit"], reverse=True)   # ranked, best first
    return rows


def session_analysis(df: pd.DataFrame) -> list:
    return _group_table(df, "session")


def symbol_analysis(df: pd.DataFrame) -> list:
    return _group_table(df, "pair")


def pattern_analysis(df: pd.DataFrame) -> list:
    """
    Grouped by the actual tracked `pattern` field (candle-pattern /
    setup tag, e.g. SWING_BOS, CONFLUENCE_SCORE, BULLISH_ENGULFING).
    NOTE: this codebase doesn't currently tag trades by zone-type
    (Order Block / FVG / Liquidity Sweep specifically) separately from
    candle pattern, so this shows what's actually tracked rather than
    forcing a BOS/OB/FVG/Sweep split the data doesn't support yet.
    """
    return _group_table(df, "pattern")


def strategy_analysis(df: pd.DataFrame) -> list:
    """By mode (Scalping/Intraday/Swing/Red News) — reuses ai_learning's
    mode_analysis so this stays consistent with existing reports."""
    if df.empty:
        return []
    stats = mode_analysis(df)
    rows = [{"name": m, "trades": s["trades"], "win_rate": s["win_rate"],
             "profit": s["profit"]} for m, s in stats.items() if s["trades"] > 0]
    rows.sort(key=lambda r: r["profit"], reverse=True)
    return rows


# =========================================
# ⚠️ ERROR ANALYSIS
# =========================================
def error_analysis(errors_df: pd.DataFrame) -> dict:
    if errors_df is None or errors_df.empty:
        return {"total": 0, "by_category": [], "samples": {}}
    by_cat = (errors_df.groupby("category").size()
              .sort_values(ascending=False))
    samples = {}
    for cat in by_cat.index:
        cat_rows = errors_df[errors_df["category"] == cat]
        samples[cat] = cat_rows["detail"].drop_duplicates().head(3).tolist()
    return {
        "total": len(errors_df),
        "by_category": [(cat, int(n)) for cat, n in by_cat.items()],
        "samples": samples,
    }


# =========================================
# 🤖 AI SUGGESTIONS (advisory only, rule-based)
# =========================================
def ai_suggestions(df: pd.DataFrame, stats: dict) -> list:
    suggestions = []
    if df.empty:
        return suggestions

    # Worst session+symbol combo (mirrors "Avoid Asia session for EURUSD")
    combo = df.groupby(["session", "pair"])["profit"].agg(["sum", "count"])
    combo = combo[combo["count"] >= 3]
    if not combo.empty:
        worst = combo["sum"].idxmin()
        worst_sum = combo.loc[worst, "sum"]
        if worst_sum < 0:
            money_str = f"-${abs(worst_sum):.0f}"
            suggestions.append(f"Avoid {worst[0]} session for {worst[1]} "
                                f"({money_str} over {int(combo.loc[worst,'count'])} trades)")

    # Best vs worst pattern (mirrors "Order Block entries outperform FVG entries")
    pat = df.groupby("pattern")["profit"].agg(["sum", "count"])
    pat = pat[pat["count"] >= 3]
    if len(pat) >= 2:
        best_p  = pat["sum"].idxmax()
        worst_p = pat["sum"].idxmin()
        if best_p != worst_p:
            suggestions.append(f"{best_p} entries are outperforming {worst_p} entries this period")

    # Consecutive losses (mirrors "Reduce lot size after three consecutive losses")
    if stats.get("max_consec_losses", 0) >= 3:
        suggestions.append(f"Hit {stats['max_consec_losses']} consecutive losses this period — "
                            f"consider a lot-size step-down rule after 3 in a row")

    # Best profit-factor symbol (mirrors "XAUUSD has the highest profit factor this month")
    sym_rows = []
    for pair, grp in df.groupby("pair"):
        w = grp[grp["profit"] > 0]["profit"].sum()
        l = abs(grp[grp["profit"] < 0]["profit"].sum())
        if l > 0 and len(grp) >= 3:
            sym_rows.append((pair, w / l))
    if sym_rows:
        best_sym, best_pf = max(sym_rows, key=lambda x: x[1])
        if best_pf > 0:
            suggestions.append(f"{best_sym} has the highest profit factor this period ({best_pf:.2f})")

    return suggestions


# =========================================
# CHART DATA
# =========================================
def equity_curve_values(df: pd.DataFrame) -> list:
    """Uses the real logged `balance` column (actual running balance),
    not a synthetic cumulative sum — this reflects the account, not just this period's trades."""
    if df.empty or "balance" not in df.columns:
        return []
    ordered = df.sort_values("time")
    vals = [v for v in ordered["balance"].tolist() if v > 0]
    return vals


def drawdown_series(df: pd.DataFrame) -> list:
    if df.empty or "balance" not in df.columns:
        return []
    ordered = df.sort_values("time")
    bal = ordered["balance"]
    bal = bal[bal > 0]
    if bal.empty:
        return []
    peak = bal.cummax()
    dd = -((peak - bal) / peak * 100)   # negative = below peak
    return dd.tolist()


def hourly_heatmap_data(df: pd.DataFrame) -> dict:
    """{(weekday, hour): {"trades": n, "profit": p}} — hour derived from
    close time minus duration (i.e. entry time) where available, else
    close time as a fallback approximation."""
    if df.empty:
        return {}
    d = df.copy()
    d["time"] = pd.to_datetime(d["time"], errors="coerce")
    d["open_time"] = d["time"] - pd.to_timedelta(d["duration_min"].fillna(0), unit="m")
    d = d.dropna(subset=["open_time"])

    result = {}
    for _, row in d.iterrows():
        key = (int(row["open_time"].weekday()), int(row["open_time"].hour))
        if key not in result:
            result[key] = {"trades": 0, "profit": 0.0}
        result[key]["trades"] += 1
        result[key]["profit"] += float(row["profit"])
    for k in result:
        result[k]["profit"] = round(result[k]["profit"], 2)
    return result


def daily_profit_map(df: pd.DataFrame) -> dict:
    """{datetime.date: profit} for the monthly calendar chart."""
    if df.empty:
        return {}
    d = df.copy()
    d["time"] = pd.to_datetime(d["time"], errors="coerce")
    d["date"] = d["time"].dt.date
    grouped = d.groupby("date")["profit"].sum().round(2)
    return {k: v for k, v in grouped.items()}


def trailing_loss_snapshot(full_df: pd.DataFrame, now: datetime.datetime) -> dict:
    """Today / this-week / this-month loss, computed from the FULL history
    regardless of which report is being built — so every email shows all
    three loss figures together as requested."""
    if full_df.empty:
        return {"daily": 0, "weekly": 0, "monthly": 0}

    d_start, d_end = get_previous_day_range(now)
    w_start, w_end = get_current_week_range(now)
    m_start_prev, m_end_prev = get_previous_month_range(now)
    # "This month" for a snapshot should be month-to-date, not last month
    m_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    def _loss_sum(start, end):
        sub = filter_range(full_df, start, end)
        return round(sub[sub["profit"] < 0]["profit"].sum(), 2) if not sub.empty else 0

    return {
        "daily":   _loss_sum(d_start, d_end),
        "weekly":  _loss_sum(w_start, w_end),
        "monthly": _loss_sum(m_start, now),
    }


# =========================================
# 🧭 FULL DASHBOARD BUNDLE
# =========================================
def compute_dashboard(df: pd.DataFrame, errors_df: pd.DataFrame = None,
                       full_df: pd.DataFrame = None, now: datetime.datetime = None) -> dict:
    """Everything a report needs, computed once.
    `full_df`/`now` (optional) enable the daily/weekly/monthly trailing-loss
    snapshot regardless of the report's own period."""
    stats = trading_statistics(df)
    bundle = {
        "overall":   overall_performance(df),
        "risk":      risk_analysis(df),
        "stats":     stats,
        "sessions":  session_analysis(df),
        "symbols":   symbol_analysis(df),
        "patterns":  pattern_analysis(df),
        "strategies":strategy_analysis(df),
        "errors":    error_analysis(errors_df),
        "suggestions": ai_suggestions(df, stats),
        "equity_curve": equity_curve_values(df),
        "drawdown_curve": drawdown_series(df),
        "heatmap": hourly_heatmap_data(df),
        "daily_profit": daily_profit_map(df),
    }
    if full_df is not None and now is not None:
        bundle["trailing_loss"] = trailing_loss_snapshot(full_df, now)
    else:
        bundle["trailing_loss"] = {"daily": 0, "weekly": 0, "monthly": 0}
    return bundle
