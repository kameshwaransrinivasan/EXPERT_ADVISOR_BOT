# =========================================
# self_ai.py — Adaptive Mode Engine
# =========================================

import pandas as pd
import os

FILE = "trade_memory.csv"


def analyze_trades():
    if not os.path.exists(FILE):
        return None
    try:
        df = pd.read_csv(FILE)
    except:
        return None

    df = df[df["result"].isin(["WIN", "LOSS", "BE"])]
    if len(df) < 20:
        return None

    df["profit"]      = pd.to_numeric(df["profit"],      errors="coerce").fillna(0)
    df["trend_score"] = pd.to_numeric(df["trend_score"], errors="coerce").fillna(0)

    wins   = df[df["profit"] > 0]
    losses = df[df["profit"] <= 0]

    return {
        "win_rate":     len(wins) / len(df),
        "avg_win":      wins["profit"].mean()   if len(wins)   else 0,
        "avg_loss":     losses["profit"].mean() if len(losses) else 0,
        "total_trades": len(df),
        "total_profit": round(df["profit"].sum(), 2),
        "pattern":      df.groupby("pattern")["profit"].mean().to_dict()
                        if "pattern" in df.columns else {},
    }


def ai_decision(report):
    if report is None:
        return {"mode": "NORMAL", "reason": "No data yet"}

    win_rate = report["win_rate"]
    avg_win  = report["avg_win"]
    avg_loss = report["avg_loss"]
    pf       = abs(avg_win / avg_loss) if avg_loss != 0 else 999

    if win_rate < 0.40 or pf < 1.0:
        return {"mode": "SAFE",       "reason": f"WR={round(win_rate*100,1)}% PF={round(pf,2)}", "win_rate": win_rate}
    if win_rate > 0.62 and pf >= 1.5:
        return {"mode": "AGGRESSIVE", "reason": f"WR={round(win_rate*100,1)}% PF={round(pf,2)}", "win_rate": win_rate}
    return     {"mode": "NORMAL",    "reason": f"WR={round(win_rate*100,1)}% PF={round(pf,2)}", "win_rate": win_rate}
