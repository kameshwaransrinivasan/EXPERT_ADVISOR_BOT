# =========================================
# ai_learning.py — AI Performance Engine
# EA BOT v2 | SRD Sections 6 & 7
#
# CRITICAL POLICY (SRD 7):
#   "Strictly NO auto-upgrade system."
#   This module NEVER writes to source files.
#   All suggestions are ADVISORY ONLY — logged
#   to CSV + included in email reports for
#   MANUAL HUMAN IMPLEMENTATION.
# =========================================

import csv
import pandas as pd
import os
from datetime import datetime, timezone, timedelta

FILE          = "trade_memory.csv"
RESULT_VALUES = {"WIN", "LOSS", "BE"}

# Advisory log — append-only, no code writes
ADVISORY_LOG = "ai_advisory.csv"


# =========================================
# DATA LOADER
# =========================================
def load_closed_df() -> pd.DataFrame:
    if not os.path.exists(FILE):
        return pd.DataFrame()
    try:
        df = pd.read_csv(FILE)
        if df.empty:
            return pd.DataFrame()
        df = df[df["result"].isin(RESULT_VALUES)]
        for col in ["profit", "trend_score", "volatility"]:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
        return df
    except Exception as e:
        print("❌ load_closed_df:", e)
        return pd.DataFrame()


def load_last_n_days(days=7) -> pd.DataFrame:
    df = load_closed_df()
    if df.empty:
        return df
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    try:
        df["time"] = pd.to_datetime(df["time"], utc=True, errors="coerce")
        return df[df["time"] >= cutoff]
    except:
        return df


# =========================================
# TRADE FILTERS
# =========================================
def ai_filter(symbol: str) -> bool:
    """Block entry if symbol WR < 40% over last 20 trades."""
    df     = load_closed_df()
    sym_df = df[df["pair"] == symbol] if not df.empty else pd.DataFrame()
    if len(sym_df) < 20:
        return True
    wr = len(sym_df[sym_df["profit"] > 0]) / len(sym_df)
    print(f"🤖 {symbol} WR:{round(wr*100,1)}%")
    if wr < 0.40:
        print(f"❌ AI BLOCKED: {symbol}")
        return False
    return True


def bad_setup_filter(df: pd.DataFrame, pattern: str) -> bool:
    """Returns True if pattern has < 35% win rate over 5+ trades."""
    s = df[df["pattern"] == pattern]
    if len(s) < 5:
        return False
    wins = len(s[s["profit"] > 0])
    return (wins / len(s) * 100) < 35


# =========================================
# PERFORMANCE SUMMARY
# =========================================
def performance_ai() -> dict | None:
    df = load_closed_df()
    if df.empty:
        return None

    total  = len(df)
    wins   = len(df[df["profit"] > 0])
    wr     = round(wins / total * 100, 2)
    profit = round(df["profit"].sum(), 2)
    pp     = df.groupby("pair")["profit"].sum()
    sp     = df.groupby("session")["profit"].sum()
    mp     = df.groupby("mode")["profit"].sum()
    pat    = df.groupby("pattern")["profit"].sum()

    result = {
        "df":           df,
        "win_rate":     wr,
        "total_profit": profit,
        "total_trades": total,
        "best_pair":    pp.idxmax()  if not pp.empty  else "NONE",
        "worst_pair":   pp.idxmin()  if not pp.empty  else "NONE",
        "best_session": sp.idxmax()  if not sp.empty  else "UNKNOWN",
        "best_mode":    mp.idxmax()  if not mp.empty  else "UNKNOWN",
        "best_pattern": pat.idxmax() if not pat.empty else "UNKNOWN",
        "confidence":   wr,
    }
    print(f"\n🧠 PERF AI | WR:{wr}% | Profit:{profit}")
    return result


def mode_analysis(df: pd.DataFrame) -> dict:
    modes  = ["SCALPING", "INTRADAY", "SWING", "RED_NEWS"]
    report = {}
    for mode in modes:
        mdf    = df[df["mode"] == mode]
        trades = len(mdf)
        wins   = len(mdf[mdf["profit"] > 0])
        losses = len(mdf[mdf["profit"] < 0])
        profit = round(mdf["profit"].sum(), 2)
        wr     = round(wins / trades * 100, 2) if trades > 0 else 0
        pp     = mdf.groupby("pair")["profit"].sum() if trades > 0 else pd.Series(dtype=float)
        report[mode] = {
            "trades": trades, "wins": wins, "losses": losses,
            "win_rate": wr,   "profit": profit,
            "best_pair": pp.idxmax() if not pp.empty else "NONE",
        }
    return report


# =========================================
# MISTAKE ANALYZER
# SRD Daily Report: SL hit reason tagging
# =========================================
SL_LOSS_TAGS = {
    "HIGH":    "News Impact Volatility Spike",
    "MEDIUM":  "Medium Impact News Fakeout",
    "NORMAL":  "Low Liquidity Fakeout",
}


def analyze_sl_hits(df: pd.DataFrame) -> list:
    """
    For every LOSS trade, diagnose the likely SL hit reason.
    Returns list of {pair, mode, pattern, reason, profit} dicts.
    """
    losses  = df[df["profit"] < 0]
    results = []
    for _, row in losses.iterrows():
        news_mode = str(row.get("news_mode", "NORMAL")).upper()
        vol       = float(row.get("volatility", 0))
        session   = str(row.get("session", ""))
        trend_s   = float(row.get("trend_score", 0))

        if news_mode in ("HIGH", "RED"):
            reason = "News Impact Volatility Spike"
        elif news_mode == "MEDIUM":
            reason = "Medium Impact News Fakeout"
        elif vol > 0.003:
            reason = "High Volatility Expansion — Wide Spread"
        elif session == "ASIAN":
            reason = "Low Liquidity Asian Session Fakeout"
        elif trend_s < 0.50:
            reason = "Weak Setup — Low Trend Score"
        else:
            reason = "Structural Misalignment / Confluence Miss"

        results.append({
            "pair":    row.get("pair", ""),
            "mode":    row.get("mode", ""),
            "pattern": row.get("pattern", ""),
            "reason":  reason,
            "profit":  row.get("profit", 0),
        })
    return results


# =========================================
# WEEKLY AI REVIEW
# SRD 7: ADVISORY ONLY — writes to log CSV
# =========================================
def weekly_ai_review() -> dict | None:
    df = load_last_n_days(7)
    if len(df) < 10:
        return None

    total  = len(df)
    wins   = len(df[df["profit"] > 0])
    losses = len(df[df["profit"] < 0])
    pp     = df.groupby("pair")["profit"].sum()
    sp     = df.groupby("session")["profit"].sum()
    elite  = df[df["trend_score"] >= 0.70]
    mode_s = mode_analysis(df)
    sl_hits = analyze_sl_hits(df)

    # ---- ADVISORY SUGGESTIONS (never auto-applied) ----
    suggestions = []
    if mode_s["SCALPING"]["win_rate"] < 40:
        suggestions.append("Reduce scalping position cap — low WR detected")
    if mode_s["SCALPING"]["win_rate"] < 40 and "XAUUSD" in mode_s["SCALPING"].get("best_pair", ""):
        suggestions.append("Lower scalping cap on XAUUSD due to increased ATR volatility")
    if mode_s["SWING"]["win_rate"] > 60:
        suggestions.append("Swing setups strong — consider increasing swing risk allocation")
    if mode_s["RED_NEWS"]["win_rate"] < 40:
        suggestions.append("Reduce news scalping — WR below threshold")
    elite_wr = round(len(elite[elite["profit"] > 0]) / max(len(elite), 1) * 100, 2)
    if elite_wr > 70:
        suggestions.append("Elite trend score ≥0.70 setups outperforming — prioritize")

    # Best/Worst pair reasoning
    best_pair  = pp.idxmax() if not pp.empty else "NONE"
    worst_pair = pp.idxmin() if not pp.empty else "NONE"

    best_df   = df[df["pair"] == best_pair]
    worst_df  = df[df["pair"] == worst_pair]
    best_mode = (best_df.groupby("mode")["profit"].sum().idxmax()
                 if not best_df.empty else "UNKNOWN")
    worst_mode= (worst_df.groupby("mode")["profit"].sum().idxmin()
                 if not worst_df.empty else "UNKNOWN")

    why_best  = f"Strong {best_mode} setups with high structure alignment"
    why_worst = f"Failed multi-timeframe structural alignment in {worst_mode} mode"

    report = {
        "period":              "7 days",
        "total_trades":        total,
        "wins":                wins,
        "losses":              losses,
        "win_rate":            round(wins / total * 100, 2),
        "total_profit":        round(df["profit"].sum(), 2),
        "best_pair":           best_pair,
        "worst_pair":          worst_pair,
        "why_best":            why_best,
        "why_worst":           why_worst,
        "best_session":        sp.idxmax() if not sp.empty else "UNKNOWN",
        "elite_score_profit":  round(elite["profit"].sum(), 2),
        "elite_score_winrate": elite_wr,
        "mode_stats":          mode_s,
        "sl_hit_analysis":     sl_hits[:10],   # top 10 loss diagnoses
        "ai_suggestions":      suggestions,    # ADVISORY ONLY
    }

    # ---- Log to advisory CSV (append-only, no code writes) ----
    _log_advisory(report)
    return report


def _log_advisory(report: dict):
    """
    Appends weekly suggestions to ai_advisory.csv.
    NEVER writes to .py source files.
    """
    try:
        exists = os.path.exists(ADVISORY_LOG)
        with open(ADVISORY_LOG, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if not exists:
                w.writerow(["timestamp", "win_rate", "total_profit",
                             "best_pair", "worst_pair", "suggestions"])
            w.writerow([
                datetime.now().isoformat(),
                report["win_rate"],
                report["total_profit"],
                report["best_pair"],
                report["worst_pair"],
                " | ".join(report["ai_suggestions"]),
            ])
    except Exception as e:
        print(f"⚠️ Advisory log error: {e}")


# =========================================
# SELF-ANALYSIS
# =========================================
def self_analysis_ai() -> dict | None:
    df = load_closed_df()
    if df.empty:
        return None
    sp     = df.groupby("session")["profit"].sum()
    pp     = df.groupby("pair")["profit"].sum()
    elite  = df[df["trend_score"] >= 0.70]
    losses = df[df["profit"] < 0]
    total  = len(df)
    wins   = len(df[df["profit"] > 0])

    return {
        "total_losses":           len(losses),
        "low_score_losses":       len(losses[losses["trend_score"] < 0.50]),
        "high_volatility_losses": len(losses[losses["volatility"] > losses["volatility"].quantile(0.75)])
                                  if len(losses) > 0 else 0,
        "best_session":           sp.idxmax() if not sp.empty else "UNKNOWN",
        "worst_session":          sp.idxmin() if not sp.empty else "UNKNOWN",
        "best_pair":              pp.idxmax() if not pp.empty else "UNKNOWN",
        "worst_pair":             pp.idxmin() if not pp.empty else "UNKNOWN",
        "elite_profit":           round(elite["profit"].sum(), 2),
        "total_profit":           round(df["profit"].sum(), 2),
        "ai_thoughts": [
            f"Win rate: {round(wins/max(total,1)*100,1)}%",
            f"Best session: {sp.idxmax() if not sp.empty else 'UNKNOWN'}",
            f"Advisory output only — no auto-modifications made",
        ],
    }
