# =============================================================================
# swing_confirmation.py — Swing 3-Confirmation System
# EA BOT v2
#
# CONFIRMATION 1: Weekly + Daily bias aligned
#   Both W1 and D1 must agree on direction
#   Uses HH/HL (bullish) or LH/LL (bearish) on both TFs
#
# CONFIRMATION 2: Daily BOS or CHoCH confirmed candle close
#   D1 candle body must CLOSE beyond last D1 swing structure
#   Wicks do NOT count
#
# CONFIRMATION 3: H4 Order Block rejection
#   Price pulls back to H4 OB zone
#   Rejection candle body closes away from OB
#
# Score 3/3 → ENTER
# Score 2/3 → WAIT
# Score 1/3 or 0/3 → SKIP
#
# Usage:
#   from swing_confirmation import check_swing_confirmations
#   result = check_swing_confirmations(symbol, candles_h4, candles_d1, candles_w1)
#   if result["score"] == 3: execute_trade()
# =============================================================================

import MetaTrader5 as mt5

from strategy         import get_candles
from market_structure import (
    get_swing_highs, get_swing_lows,
    get_market_structure, detect_bos, detect_choch
)


# =============================================================================
# CONFIRMATION 1 — WEEKLY + DAILY BIAS ALIGNED
# Both must show same directional bias (HH/HL or LH/LL)
# =============================================================================
def _get_tf_bias(candles: list) -> str:
    """
    Returns 'BULLISH', 'BEARISH', or 'SIDEWAYS'
    based on swing high/low structure.
    """
    if len(candles) < 10:
        return "SIDEWAYS"

    highs = get_swing_highs(candles)
    lows  = get_swing_lows(candles)

    if len(highs) < 2 or len(lows) < 2:
        return "SIDEWAYS"

    h1, h2 = highs[-2]["price"], highs[-1]["price"]
    l1, l2 = lows[-2]["price"],  lows[-1]["price"]

    if h2 > h1 and l2 > l1:
        return "BULLISH"
    if h2 < h1 and l2 < l1:
        return "BEARISH"
    return "SIDEWAYS"


def _check_weekly_daily_bias(candles_d1: list,
                              candles_w1: list,
                              side: str) -> dict:
    """
    Both Weekly and Daily must show same bias as trade direction.
    SIDEWAYS on either = fail.
    """
    w1_bias = _get_tf_bias(candles_w1)
    d1_bias = _get_tf_bias(candles_d1)

    expected = "BULLISH" if side == "BUY" else "BEARISH"

    w1_ok = (w1_bias == expected)
    d1_ok = (d1_bias == expected)

    if w1_ok and d1_ok:
        return {
            "passed": True,
            "tag":    f"W1_D1_{expected}",
            "reason": (f"W1 bias:{w1_bias} + D1 bias:{d1_bias} "
                       f"— both aligned {expected}"),
            "w1_bias": w1_bias,
            "d1_bias": d1_bias,
        }

    failures = []
    if not w1_ok:
        failures.append(f"W1:{w1_bias}")
    if not d1_ok:
        failures.append(f"D1:{d1_bias}")

    return {
        "passed": False,
        "tag":    "BIAS_MISMATCH",
        "reason": (f"Bias mismatch for {side}: "
                   f"{' | '.join(failures)} (need {expected})"),
        "w1_bias": w1_bias,
        "d1_bias": d1_bias,
    }


# =============================================================================
# CONFIRMATION 2 — DAILY BOS or CHoCH (body close rule)
# D1 candle body must CLOSE through last D1 swing structure
# =============================================================================
def _check_d1_bos_body(candles_d1: list, side: str) -> dict:
    """
    D1 BOS or CHoCH via strict body close.
    A wick that crosses the level but body closes back = FAIL.
    """
    if len(candles_d1) < 10:
        return {"passed": False, "tag": "NO_DATA",
                "reason": "D1 insufficient candles"}

    highs = get_swing_highs(candles_d1)
    lows  = get_swing_lows(candles_d1)

    if not highs or not lows:
        return {"passed": False, "tag": "NO_SWINGS",
                "reason": "No D1 swing points detected"}

    last_swing_high = highs[-1]["price"]
    last_swing_low  = lows[-1]["price"]
    close           = candles_d1[-1]["close"]   # body close — strict

    if side == "BUY":
        # BOS: body close above last D1 swing high
        if close > last_swing_high:
            return {
                "passed": True,
                "tag":    "D1_BOS_BULL",
                "reason": (f"D1 body close {close:.5f} > "
                           f"swing high {last_swing_high:.5f}"),
            }
        # CHoCH: was bearish structure, body close breaks above swing high
        choch = detect_choch(candles_d1)
        if choch == "BULLISH_CHOCH":
            return {
                "passed": True,
                "tag":    "D1_CHOCH_BULL",
                "reason": (f"D1 CHoCH bullish confirmed "
                           f"(close:{close:.5f})"),
            }
        return {
            "passed": False,
            "tag":    "D1_NO_BOS",
            "reason": (f"D1 close {close:.5f} has not cleared "
                       f"swing high {last_swing_high:.5f}"),
        }

    else:
        if close < last_swing_low:
            return {
                "passed": True,
                "tag":    "D1_BOS_BEAR",
                "reason": (f"D1 body close {close:.5f} < "
                           f"swing low {last_swing_low:.5f}"),
            }
        choch = detect_choch(candles_d1)
        if choch == "BEARISH_CHOCH":
            return {
                "passed": True,
                "tag":    "D1_CHOCH_BEAR",
                "reason": (f"D1 CHoCH bearish confirmed "
                           f"(close:{close:.5f})"),
            }
        return {
            "passed": False,
            "tag":    "D1_NO_BOS",
            "reason": (f"D1 close {close:.5f} has not broken "
                       f"swing low {last_swing_low:.5f}"),
        }


# =============================================================================
# CONFIRMATION 3 — H4 ORDER BLOCK REJECTION
# Price must pull back to H4 OB, rejection candle closes away
# =============================================================================
def _detect_h4_ob(candles_h4: list, side: str) -> dict | None:
    """
    Detects the most recent H4 Order Block.
    BUY  → last bearish H4 candle before an up-move (demand OB)
    SELL → last bullish H4 candle before a down-move (supply OB)
    """
    if len(candles_h4) < 10:
        return None

    lookback = candles_h4[-30:]

    if side == "BUY":
        for c in reversed(lookback[:-2]):
            if c["close"] < c["open"]:
                return {
                    "ob_high": c["high"],
                    "ob_low":  c["low"],
                    "ob_mid":  (c["high"] + c["low"]) / 2,
                    "type":    "H4_DEMAND_OB",
                }
    else:
        for c in reversed(lookback[:-2]):
            if c["close"] > c["open"]:
                return {
                    "ob_high": c["high"],
                    "ob_low":  c["low"],
                    "ob_mid":  (c["high"] + c["low"]) / 2,
                    "type":    "H4_SUPPLY_OB",
                }
    return None


def _check_h4_ob_rejection(candles_h4: list, side: str) -> dict:
    """
    Checks if price is retesting H4 OB AND
    rejection candle body closes back away from the OB.
    """
    if len(candles_h4) < 5:
        return {"passed": False, "tag": "NO_DATA",
                "reason": "H4 insufficient candles"}

    ob = _detect_h4_ob(candles_h4, side)
    if ob is None:
        return {
            "passed": False,
            "tag":    "NO_H4_OB",
            "reason": (f"No H4 {'demand' if side == 'BUY' else 'supply'} "
                       f"OB detected in last 30 bars"),
        }

    curr = candles_h4[-1]

    # Check if price is within OB zone
    in_zone = (ob["ob_low"] <= curr["low"]  <= ob["ob_high"] or
               ob["ob_low"] <= curr["high"] <= ob["ob_high"] or
               (curr["low"] <= ob["ob_low"] and curr["high"] >= ob["ob_high"]))

    if not in_zone:
        return {
            "passed": False,
            "tag":    "H4_NOT_IN_OB",
            "reason": (f"H4 price not in OB zone "
                       f"{ob['ob_low']:.5f}–{ob['ob_high']:.5f}"),
            "ob":     ob,
        }

    # Rejection: body must close back away from OB
    if side == "BUY":
        # Bullish rejection: close > open, body above OB midpoint
        if curr["close"] > curr["open"] and curr["close"] > ob["ob_mid"]:
            return {
                "passed": True,
                "tag":    "H4_DEMAND_OB_REJECT",
                "reason": (f"H4 demand OB pullback + bull rejection "
                           f"(close:{curr['close']:.5f} > "
                           f"ob_mid:{ob['ob_mid']:.5f})"),
                "ob":     ob,
            }
    else:
        # Bearish rejection: close < open, body below OB midpoint
        if curr["close"] < curr["open"] and curr["close"] < ob["ob_mid"]:
            return {
                "passed": True,
                "tag":    "H4_SUPPLY_OB_REJECT",
                "reason": (f"H4 supply OB pullback + bear rejection "
                           f"(close:{curr['close']:.5f} < "
                           f"ob_mid:{ob['ob_mid']:.5f})"),
                "ob":     ob,
            }

    return {
        "passed": False,
        "tag":    "H4_OB_NO_REJECTION",
        "reason": (f"In H4 OB zone but rejection candle not formed yet "
                   f"(close:{curr['close']:.5f} ob_mid:{ob['ob_mid']:.5f})"),
        "ob":     ob,
    }


# =============================================================================
# MASTER FUNCTION — check_swing_confirmations()
# =============================================================================
def check_swing_confirmations(symbol: str,
                               candles_h4: list,
                               candles_d1: list,
                               candles_w1: list,
                               side: str) -> dict:
    """
    Runs all 3 swing confirmations.

    Args:
        symbol      : e.g. "GOLD"
        candles_h4  : list of H4 OHLCV dicts
        candles_d1  : list of D1 OHLCV dicts
        candles_w1  : list of W1 OHLCV dicts
        side        : "BUY" or "SELL"

    Returns:
        {
            score    : int  (0–3)
            passed   : bool
            action   : "ENTER" | "WAIT" | "SKIP"
            c1, c2, c3 : individual results
            summary  : str
        }
    """
    c1 = _check_weekly_daily_bias(candles_d1, candles_w1, side)
    c2 = _check_d1_bos_body(candles_d1, side)
    c3 = _check_h4_ob_rejection(candles_h4, side)

    score = sum([c1["passed"], c2["passed"], c3["passed"]])

    if score == 3:
        action = "ENTER"
    elif score == 2:
        action = "WAIT"
    else:
        action = "SKIP"

    summary = (
        f"[SWING CONF] {symbol} {side} | Score:{score}/3 | {action}\n"
        f"  C1 W1+D1 Bias : {'✅' if c1['passed'] else '❌'} {c1['reason']}\n"
        f"  C2 D1 BOS     : {'✅' if c2['passed'] else '❌'} {c2['reason']}\n"
        f"  C3 H4 OB Rej  : {'✅' if c3['passed'] else '❌'} {c3['reason']}"
    )

    print(summary)

    return {
        "score":   score,
        "passed":  score == 3,
        "action":  action,
        "c1":      c1,
        "c2":      c2,
        "c3":      c3,
        "summary": summary,
        "symbol":  symbol,
        "side":    side,
    }
