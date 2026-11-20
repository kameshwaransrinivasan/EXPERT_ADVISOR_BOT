# =========================================
# market_regime.py — Market Adaptive Engine
# EA BOT v2 | Active Normal Mode Update
#
# CHOPPY_RANGE change:
#   OLD → freeze all entries
#   NEW → mean reversion allowed at zone boundaries
#         risk_multiplier = 0.5 (50% size)
#
# Regime actions:
#   SMOOTH_TREND     → NORMAL
#   CHOPPY_RANGE     → MEAN_REVERT (buy demand bottom, sell supply top)
#   VOLATILE_EXPANSION → HALVE_RISK
#   MACRO_REVERSAL   → EXIT_SWING
# =========================================

import MetaTrader5 as mt5
from strategy import get_candles, calculate_volatility
from market_structure import get_market_structure, detect_choch


# ---- ATR ----
def _atr(candles: list, period: int) -> float:
    if len(candles) < period:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        c = candles[i]
        p = candles[i - 1]
        tr = max(
            c["high"] - c["low"],
            abs(c["high"] - p["close"]),
            abs(c["low"]  - p["close"]),
        )
        trs.append(tr)
    recent = trs[-period:]
    return sum(recent) / len(recent) if recent else 0.0


# ---- Volatility Multiplier ----
def volatility_multiplier(symbol: str, timeframe=None) -> float:
    if timeframe is None:
        timeframe = mt5.TIMEFRAME_H1
    candles = get_candles(symbol, timeframe, 120)
    if len(candles) < 20:
        return 1.0
    atr14  = _atr(candles, 14)
    atr100 = _atr(candles, min(100, len(candles) - 1))
    if atr100 == 0:
        return 1.0
    return round(atr14 / atr100, 3)


# ---- Regime Classifier ----
def classify_regime(symbol: str) -> dict:
    """
    Returns regime dict with action field.

    CHOPPY_RANGE action = MEAN_REVERT (not TIGHTEN/freeze anymore).
    Mean reversion: buy at demand zone LOW, sell at supply zone HIGH.
    Risk multiplier = 0.5 during CHOPPY_RANGE.
    """
    vm = volatility_multiplier(symbol)

    c_h1 = get_candles(symbol, mt5.TIMEFRAME_H1, 100)
    c_h4 = get_candles(symbol, mt5.TIMEFRAME_H4, 100)
    if len(c_h1) < 20:
        return {
            "regime":             "SMOOTH_TREND",
            "vm":                 vm,
            "action":             "NORMAL",
            "volatile_expansion": False,
            "choppy_range":       False,
            "risk_multiplier":    1.0,
        }

    structure = get_market_structure(c_h1)
    choch_h4  = detect_choch(c_h4) if len(c_h4) >= 20 else "NONE"

    if choch_h4 in ("BULLISH_CHOCH", "BEARISH_CHOCH"):
        regime          = "MACRO_REVERSAL"
        action          = "EXIT_SWING"
        risk_multiplier = 0.5

    elif vm >= 1.5:
        regime          = "VOLATILE_EXPANSION"
        action          = "HALVE_RISK"
        risk_multiplier = 0.5   # halve risk during volatile expansion

    elif vm <= 0.7 or structure == "SIDEWAYS":
        # ACTIVE NORMAL MODE: mean reversion instead of freeze
        regime          = "CHOPPY_RANGE"
        action          = "MEAN_REVERT"   # was TIGHTEN
        risk_multiplier = 0.5             # 50% size — safe farming

    else:
        regime          = "SMOOTH_TREND"
        action          = "NORMAL"
        risk_multiplier = 1.0

    volatile_expansion = (regime == "VOLATILE_EXPANSION")
    choppy_range       = (regime == "CHOPPY_RANGE")

    print(f"REGIME [{symbol}]: {regime} | Vm:{vm} | "
          f"Action:{action} | RiskMult:{risk_multiplier}")

    return {
        "regime":             regime,
        "vm":                 vm,
        "action":             action,
        "volatile_expansion": volatile_expansion,
        "choppy_range":       choppy_range,
        "risk_multiplier":    risk_multiplier,
        "structure":          structure,
        "choch_h4":           choch_h4,
    }


def should_freeze_breakout(symbol: str) -> bool:
    """
    Active Normal Mode: CHOPPY_RANGE no longer freezes.
    Only VOLATILE_EXPANSION freezes breakout entries.
    """
    r = classify_regime(symbol)
    return r["regime"] == "VOLATILE_EXPANSION"


def should_tighten_scalp(symbol: str) -> bool:
    r = classify_regime(symbol)
    return r["choppy_range"]
