# =============================================================================
# scalping_confirmation.py — Scalping 3-Confirmation System
# EA BOT v2
#
# CONFIRMATION 1: Session check
#   Delegates to market.get_current_session() — the single session
#   authority shared with main.py's scalping_thread() gate. Allowed:
#   ASIAN + LONDON + OVERLAP (00:00-17:00 UTC). See SCALP_ALLOWED_SESSIONS
#   below. (Previously this defined its own separate London(07-12)/
#   NY(13-17) windows that silently contradicted the thread-level gate
#   and blocked the entire Asian session — fixed.)
#
# CONFIRMATION 2: M5 BOS or CHoCH
#   Candle BODY close above/below structure (not wick)
#   Uses close price — strict body close rule
#
# CONFIRMATION 3: M1 Order Block Retest
#   Price pulls back to M1 OB zone
#   Rejection candle closes away from OB
#
# Score 3/3 → ENTER
# Score 2/3 → WAIT
# Score 1/3 or 0/3 → SKIP
#
# Usage:
#   from scalping_confirmation import check_scalping_confirmations
#   result = check_scalping_confirmations(symbol, candles_m1, candles_m5)
#   if result["score"] == 3: execute_trade()
# =============================================================================

import MetaTrader5 as mt5
from datetime import datetime, timezone

from strategy         import get_candles
from market_structure import (
    get_swing_highs, get_swing_lows,
    detect_bos, detect_choch
)
from market           import get_current_session


# =============================================================================
# CONFIRMATION 1 — SESSION CHECK
#
# SINGLE SESSION AUTHORITY: this used to define its own London(07-12)/
# NY(13-17) UTC windows independently of main.py's scalping_thread(),
# which gates on market.get_current_session() being ASIAN/LONDON/OVERLAP
# (00:00-17:00 UTC). The two disagreed, and since both were required
# (AND logic), the intersection silently blocked the entire Asian
# session (00:00-07:00 UTC) — the exact session scalping_thread()'s own
# docstring says it targets — plus a dead 12:00-13:00 UTC gap.
#
# Fix: delegate to market.get_current_session() so there is exactly one
# place that defines "when is scalping allowed." This confirmation now
# restates that same decision rather than re-deriving a different one.
# =============================================================================
SCALP_ALLOWED_SESSIONS = ("ASIAN", "LONDON", "OVERLAP")


def _check_session() -> dict:
    """
    Active session windows for scalping — delegates to
    market.get_current_session() (the single session authority) instead
    of maintaining a second, independent hour-range definition.
    """
    session = get_current_session()

    if session in SCALP_ALLOWED_SESSIONS:
        return {
            "passed":  True,
            "session": session,
            "reason":  f"{session} session active — scalping allowed",
        }

    return {
        "passed":  False,
        "session": session,
        "reason":  (f"{session} session — outside scalping window "
                    f"({'/'.join(SCALP_ALLOWED_SESSIONS)})"),
    }


# =============================================================================
# CONFIRMATION 2 — M5 BOS or CHoCH (BODY close rule)
# Candle body must CLOSE above swing high (BOS bull) or below swing low (BOS bear)
# Wick crosses do NOT count
# Buffer: 0.5 pip — avoids missing real BOS by 1-2 pips
# Lookback: last 20 bars only — avoids stale swing levels
# =============================================================================
def _check_m5_bos_body(candles_m5: list, side: str) -> dict:
    """
    Body-close BOS/CHoCH detection on M5.
    Uses candle CLOSE vs recent swing structure (last 20 bars).
    Includes 0.5 pip buffer — if price within 0.5 pip of level, counts as BOS.
    """
    if len(candles_m5) < 10:
        return {"passed": False, "tag": "NO_DATA",
                "reason": "M5 insufficient candles"}

    # Use only last 20 bars for swing detection — avoids stale levels
    recent = candles_m5[-20:]
    highs  = get_swing_highs(recent)
    lows   = get_swing_lows(recent)

    # Fallback to full candles if recent has no swings
    if not highs or not lows:
        highs = get_swing_highs(candles_m5)
        lows  = get_swing_lows(candles_m5)

    if not highs or not lows:
        return {"passed": False, "tag": "NO_SWINGS",
                "reason": "No M5 swing points detected"}

    last_swing_high = highs[-1]["price"]
    last_swing_low  = lows[-1]["price"]
    close           = candles_m5[-1]["close"]

    # 0.5 pip buffer — handles real BOS missed by tiny margin
    pip_buf = 0.00005   # 0.5 pip standard FX

    if side == "BUY":
        if close >= last_swing_high - pip_buf:
            gap = close - last_swing_high
            tag = "M5_BOS_BULL" if gap >= 0 else "M5_BOS_BULL_NEAR"
            return {
                "passed": True,
                "tag":    tag,
                "reason": (f"M5 body close {close:.5f} ≥ swing high "
                           f"{last_swing_high:.5f} (gap:{gap:+.5f})"),
            }
        bos = detect_bos(candles_m5)
        if bos == "BULLISH_BOS":
            return {
                "passed": True,
                "tag":    "M5_CHOCH_BULL",
                "reason": f"M5 CHoCH bullish confirmed (close:{close:.5f})",
            }
        return {
            "passed": False,
            "tag":    "NO_M5_BOS",
            "reason": (f"M5 close {close:.5f} has not cleared "
                       f"swing high {last_swing_high:.5f} "
                       f"(need {pip_buf*10000*10:.1f} more pips)"),
        }

    else:  # SELL
        if close <= last_swing_low + pip_buf:
            gap = last_swing_low - close
            tag = "M5_BOS_BEAR" if gap >= 0 else "M5_BOS_BEAR_NEAR"
            return {
                "passed": True,
                "tag":    tag,
                "reason": (f"M5 body close {close:.5f} ≤ swing low "
                           f"{last_swing_low:.5f} (gap:{gap:+.5f})"),
            }
        bos = detect_bos(candles_m5)
        if bos == "BEARISH_BOS":
            return {
                "passed": True,
                "tag":    "M5_CHOCH_BEAR",
                "reason": f"M5 CHoCH bearish confirmed (close:{close:.5f})",
            }
        return {
            "passed": False,
            "tag":    "NO_M5_BOS",
            "reason": (f"M5 close {close:.5f} has not broken "
                       f"swing low {last_swing_low:.5f} "
                       f"(need {pip_buf*10000*10:.1f} more pips)"),
        }


# =============================================================================
# CONFIRMATION 3 — M1 ORDER BLOCK RETEST
# OB = last bearish candle before bullish BOS (demand OB)
#      last bullish candle before bearish BOS (supply OB)
#
# Fix: Find OB closest to current price (not just most recent candle).
# Also: price doesn't need to be exactly IN zone — within proximity counts.
# Proximity = within 5 pips of OB boundary.
# =============================================================================
def _detect_m1_ob(candles_m1: list, side: str) -> dict | None:
    """
    Detects the M1 Order Block CLOSEST to current price.

    For BUY  (demand OB): look for bearish candles below current price
    For SELL (supply OB): look for bullish candles above current price

    Searches last 30 bars. Returns the OB zone closest to current price.
    """
    if len(candles_m1) < 10:
        return None

    current_price = candles_m1[-1]["close"]
    lookback      = candles_m1[-30:-1]   # exclude current candle
    candidates    = []

    if side == "BUY":
        # Demand OB: bearish candles BELOW current price
        for c in lookback:
            if c["close"] < c["open"] and c["high"] < current_price:
                dist = abs(current_price - c["high"])
                candidates.append({
                    "ob_high": c["high"],
                    "ob_low":  c["low"],
                    "ob_mid":  (c["high"] + c["low"]) / 2,
                    "type":    "DEMAND_OB",
                    "dist":    dist,
                })
    else:
        # Supply OB: bullish candles ABOVE current price
        for c in lookback:
            if c["close"] > c["open"] and c["low"] > current_price:
                dist = abs(c["low"] - current_price)
                candidates.append({
                    "ob_high": c["high"],
                    "ob_low":  c["low"],
                    "ob_mid":  (c["high"] + c["low"]) / 2,
                    "type":    "SUPPLY_OB",
                    "dist":    dist,
                })

    if not candidates:
        return None

    # Return OB closest to current price
    return min(candidates, key=lambda x: x["dist"])


def _check_m1_ob_retest(candles_m1: list, side: str) -> dict:
    """
    Checks if current M1 price is at or near an OB zone.

    Two pass conditions:
      A) Price IS inside the OB zone + rejection candle
      B) Price is within 5 pips of OB + any bullish/bearish candle

    Pass B allows entries when price is approaching OB (proximity entry).
    """
    if len(candles_m1) < 5:
        return {"passed": False, "tag": "NO_DATA",
                "reason": "M1 insufficient candles"}

    ob = _detect_m1_ob(candles_m1, side)
    if ob is None:
        return {
            "passed": False,
            "tag":    "NO_OB",
            "reason": (f"No M1 {'demand' if side == 'BUY' else 'supply'} "
                       f"OB found near price"),
        }

    curr          = candles_m1[-1]
    current_price = curr["close"]

    # Get pip size for proximity check
    # approximate: 0.0001 for FX, broader check used
    pip5 = 0.00050   # 5 pips buffer for FX (works for JPY too, scaled)

    in_zone = (ob["ob_low"] - pip5 <= current_price <= ob["ob_high"] + pip5)

    if not in_zone:
        return {
            "passed": False,
            "tag":    "NOT_IN_OB",
            "reason": (f"Price {current_price:.5f} not near OB "
                       f"{ob['ob_low']:.5f}–{ob['ob_high']:.5f} "
                       f"(dist:{ob['dist']:.5f})"),
        }

    # ---- Pass A: inside zone + rejection ----
    if side == "BUY":
        if curr["close"] > curr["open"]:
            return {
                "passed": True,
                "tag":    "M1_OB_DEMAND_REJECT",
                "reason": (f"M1 demand OB retest + bull close "
                           f"({current_price:.5f} near OB {ob['ob_low']:.5f}–{ob['ob_high']:.5f})"),
                "ob":     ob,
            }
        # Pass B: approaching OB, any candle (more permissive)
        if ob["dist"] <= pip5 * 2:
            return {
                "passed": True,
                "tag":    "M1_OB_APPROACH",
                "reason": (f"M1 demand OB approach "
                           f"(price:{current_price:.5f} dist:{ob['dist']:.5f})"),
                "ob":     ob,
            }
    else:
        if curr["close"] < curr["open"]:
            return {
                "passed": True,
                "tag":    "M1_OB_SUPPLY_REJECT",
                "reason": (f"M1 supply OB retest + bear close "
                           f"({current_price:.5f} near OB {ob['ob_low']:.5f}–{ob['ob_high']:.5f})"),
                "ob":     ob,
            }
        if ob["dist"] <= pip5 * 2:
            return {
                "passed": True,
                "tag":    "M1_OB_APPROACH",
                "reason": (f"M1 supply OB approach "
                           f"(price:{current_price:.5f} dist:{ob['dist']:.5f})"),
                "ob":     ob,
            }

    return {
        "passed": False,
        "tag":    "OB_NO_REJECTION",
        "reason": (f"Near OB but no rejection candle "
                   f"(close:{current_price:.5f} ob_mid:{ob['ob_mid']:.5f})"),
    }


# =============================================================================
# MASTER FUNCTION — check_scalping_confirmations()
# =============================================================================
def check_scalping_confirmations(symbol: str,
                                  candles_m1: list,
                                  candles_m5: list,
                                  side: str) -> dict:
    """
    Runs all 3 scalping confirmations.

    Args:
        symbol      : e.g. "EURUSD"
        candles_m1  : list of M1 OHLCV dicts
        candles_m5  : list of M5 OHLCV dicts
        side        : "BUY" or "SELL"

    Returns:
        {
            score    : int  (0–3)
            passed   : bool (True if score == 3)
            action   : "ENTER" | "WAIT" | "SKIP"
            c1, c2, c3 : individual confirmation results
            summary  : str
        }
    """
    c1 = _check_session()
    c2 = _check_m5_bos_body(candles_m5, side)
    c3 = _check_m1_ob_retest(candles_m1, side)

    score = sum([c1["passed"], c2["passed"], c3["passed"]])

    if score == 3:
        action = "ENTER"
    elif score == 2:
        action = "WAIT"
    else:
        action = "SKIP"

    summary = (
        f"[SCALP CONF] {symbol} {side} | Score:{score}/3 | {action}\n"
        f"  C1 Session : {'✅' if c1['passed'] else '❌'} {c1['reason']}\n"
        f"  C2 M5 BOS  : {'✅' if c2['passed'] else '❌'} {c2['reason']}\n"
        f"  C3 M1 OB   : {'✅' if c3['passed'] else '❌'} {c3['reason']}"
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
