# =============================================================================
# intraday_confirmation.py — Intraday confirmation helpers
# EA BOT v2
#
# STATUS (consolidation): check_intraday_confirmations() below, along with
# _check_h1_poi() (C0) and _check_h4_trend() (C1), is RETIRED as a live
# gate. Both re-derived checks that fxjers_signal.intraday_signal() already
# performs (H1 structure+zone+FVG, H4 structure) from freshly re-fetched
# candles — independently of, and occasionally in disagreement with, the
# same checks already done there. It's kept here for reference and is no
# longer imported by main.py.
#
# _check_m15_liq_sweep_or_ob() (C3) is the one part of this module that
# ISN'T a duplicate — it's still live, but is now called directly from
# fxjers_signal.intraday_signal() (reusing that function's own already-
# fetched M15 candles) as one input to its consolidated M15 score, rather
# than being re-run here against a separately-fetched candle set.
#
# Original design (for history):
#   C1: H4 trend (HH+HL / LH+LL) | C2: M15 BOS matching H4 | C3: M15
#   liquidity sweep+reversal OR M15 OB retest+rejection. Score 3/3 enter,
#   2/3 wait, <2 skip.
# =============================================================================

import MetaTrader5 as mt5

from strategy         import get_candles
from market_structure import (
    get_swing_highs, get_swing_lows,
    get_market_structure, detect_bos,
    detect_liquidity_sweep
)
from zones            import get_active_zone, detect_fvg, zone_fvg_confluence


# =============================================================================
# CONFIRMATION 0 — H1 VALID POI  (LOCKED INTRADAY STRATEGY)
# H4 = main direction | H1 = valid POI/Supply/Demand | M15 = entry engine.
# H1 must contain a fresh Supply/Demand zone matching the trade side
# BEFORE the M15 confirmations below are even evaluated — this was
# previously missing entirely (H1 was skipped by this module although
# it's used to build the signal itself).
# =============================================================================
def _check_h1_poi(candles_h1: list, side: str) -> dict:
    if len(candles_h1) < 20:
        return {"passed": False, "tag": "NO_DATA",
                "reason": "H1 insufficient candles"}

    # ---- Valid Market Structure ----
    h1_structure = get_market_structure(candles_h1)
    expected_structure = "BULLISH" if side == "BUY" else "BEARISH"
    if h1_structure not in ("SIDEWAYS", expected_structure):
        return {"passed": False, "tag": "H1_STRUCTURE_MISMATCH",
                "reason": f"H1 structure is {h1_structure}, need {expected_structure}"}

    # ---- Valid Supply/Demand -> Important POI ----
    zone = get_active_zone(candles_h1)
    if zone is None:
        return {"passed": False, "tag": "NO_H1_POI",
                "reason": "No fresh H1 Supply/Demand zone at current price"}

    expected_zone = "DEMAND" if side == "BUY" else "SUPPLY"
    if zone["type"] != expected_zone:
        return {"passed": False, "tag": "H1_ZONE_MISMATCH",
                "reason": f"H1 zone is {zone['type']}, need {expected_zone}"}

    # ---- Valid FVG (mandatory, not optional) ----
    fvg = detect_fvg(candles_h1)
    fvg_ok = zone_fvg_confluence(zone, fvg)
    if not fvg_ok:
        return {"passed": False, "tag": "H1_NO_FVG_CONFLUENCE",
                "reason": "No H1 zone+FVG confluence"}

    return {"passed": True, "tag": "H1_POI_VALID",
            "reason": (f"H1 {h1_structure} structure, fresh {zone['type']} zone "
                       f"{zone['low']:.5f}-{zone['high']:.5f}, FVG confluence OK"),
            "zone": zone}


# =============================================================================
# CONFIRMATION 1 — H4 TREND: HH/HL or LH/LL
# =============================================================================
def _check_h4_trend(candles_h4: list, side: str) -> dict:
    """
    Validates H4 trend via swing structure.

    Bullish = HH (higher highs) + HL (higher lows)
    Bearish = LH (lower highs) + LL (lower lows)

    Needs at least 2 swing highs and 2 swing lows.
    """
    if len(candles_h4) < 20:
        return {"passed": False, "tag": "NO_DATA",
                "reason": "H4 insufficient candles"}

    highs = get_swing_highs(candles_h4)
    lows  = get_swing_lows(candles_h4)

    if len(highs) < 2 or len(lows) < 2:
        return {"passed": False, "tag": "NO_SWINGS",
                "reason": f"Not enough H4 swings (highs:{len(highs)} lows:{len(lows)})"}

    h1, h2 = highs[-2]["price"], highs[-1]["price"]   # h2 = most recent
    l1, l2 = lows[-2]["price"],  lows[-1]["price"]    # l2 = most recent

    is_bullish = h2 > h1 and l2 > l1   # HH + HL
    is_bearish = h2 < h1 and l2 < l1   # LH + LL

    if side == "BUY":
        if is_bullish:
            return {
                "passed": True,
                "tag":    "H4_HH_HL",
                "reason": (f"H4 bullish: HH({h1:.5f}→{h2:.5f}) "
                           f"HL({l1:.5f}→{l2:.5f})"),
                "h4_structure": "BULLISH",
            }
        return {
            "passed": False,
            "tag":    "H4_NOT_BULLISH",
            "reason": (f"H4 not bullish: highs({h1:.5f},{h2:.5f}) "
                       f"lows({l1:.5f},{l2:.5f})"),
        }
    else:
        if is_bearish:
            return {
                "passed": True,
                "tag":    "H4_LH_LL",
                "reason": (f"H4 bearish: LH({h1:.5f}→{h2:.5f}) "
                           f"LL({l1:.5f}→{l2:.5f})"),
                "h4_structure": "BEARISH",
            }
        return {
            "passed": False,
            "tag":    "H4_NOT_BEARISH",
            "reason": (f"H4 not bearish: highs({h1:.5f},{h2:.5f}) "
                       f"lows({l1:.5f},{l2:.5f})"),
        }


# =============================================================================
# CONFIRMATION 2 — M15 BOS aligned with H4 direction
# Strict body close rule (not wick)
# =============================================================================
def _check_m15_bos(candles_m15: list, side: str) -> dict:
    """
    M15 BOS must confirm same direction as H4 trend.
    Uses candle body CLOSE — wicks don't count.
    """
    if len(candles_m15) < 10:
        return {"passed": False, "tag": "NO_DATA",
                "reason": "M15 insufficient candles"}

    highs = get_swing_highs(candles_m15)
    lows  = get_swing_lows(candles_m15)

    if not highs or not lows:
        return {"passed": False, "tag": "NO_SWINGS",
                "reason": "No M15 swing points"}

    last_swing_high = highs[-1]["price"]
    last_swing_low  = lows[-1]["price"]
    close           = candles_m15[-1]["close"]   # body close strict

    if side == "BUY":
        if close > last_swing_high:
            return {
                "passed": True,
                "tag":    "M15_BOS_BULL",
                "reason": (f"M15 body close {close:.5f} > "
                           f"swing high {last_swing_high:.5f}"),
            }
        return {
            "passed": False,
            "tag":    "M15_NO_BOS",
            "reason": (f"M15 close {close:.5f} < "
                       f"swing high {last_swing_high:.5f}"),
        }
    else:
        if close < last_swing_low:
            return {
                "passed": True,
                "tag":    "M15_BOS_BEAR",
                "reason": (f"M15 body close {close:.5f} < "
                           f"swing low {last_swing_low:.5f}"),
            }
        return {
            "passed": False,
            "tag":    "M15_NO_BOS",
            "reason": (f"M15 close {close:.5f} > "
                       f"swing low {last_swing_low:.5f}"),
        }


# =============================================================================
# CONFIRMATION 3 — M15 LIQUIDITY SWEEP + REVERSAL
#                  OR M15 OB RETEST + REJECTION
# Either condition passes C3
# =============================================================================
def _detect_m15_ob(candles_m15: list, side: str) -> dict | None:
    """Detect most recent M15 Order Block."""
    if len(candles_m15) < 10:
        return None
    lookback = candles_m15[-20:]
    if side == "BUY":
        for c in reversed(lookback[:-2]):
            if c["close"] < c["open"]:
                return {"ob_high": c["high"], "ob_low": c["low"],
                        "ob_mid": (c["high"] + c["low"]) / 2,
                        "type": "M15_DEMAND_OB"}
    else:
        for c in reversed(lookback[:-2]):
            if c["close"] > c["open"]:
                return {"ob_high": c["high"], "ob_low": c["low"],
                        "ob_mid": (c["high"] + c["low"]) / 2,
                        "type": "M15_SUPPLY_OB"}
    return None


def _check_m15_liq_sweep_or_ob(candles_m15: list, side: str) -> dict:
    """
    Passes if EITHER:
      A) M15 liquidity sweep of prev high/low + reversal
      B) M15 OB zone retest + rejection candle

    Tries A first, then B.
    """
    if len(candles_m15) < 5:
        return {"passed": False, "tag": "NO_DATA",
                "reason": "M15 insufficient candles"}

    curr = candles_m15[-1]

    # ---- Option A: Liquidity sweep + reversal ----
    sweep = detect_liquidity_sweep(candles_m15)

    if side == "BUY" and sweep == "SELL_SIDE_SWEEP":
        # Swept sell-side liquidity then closed back up = bullish reversal
        if curr["close"] > curr["open"]:
            return {
                "passed": True,
                "tag":    "M15_SELL_SWEEP_REVERSAL",
                "reason": ("M15 sell-side liquidity sweep + "
                           "bullish reversal candle close"),
            }

    if side == "SELL" and sweep == "BUY_SIDE_SWEEP":
        if curr["close"] < curr["open"]:
            return {
                "passed": True,
                "tag":    "M15_BUY_SWEEP_REVERSAL",
                "reason": ("M15 buy-side liquidity sweep + "
                           "bearish reversal candle close"),
            }

    # ---- Option B: M15 OB retest + rejection ----
    ob = _detect_m15_ob(candles_m15, side)

    if ob is not None:
        in_zone = (ob["ob_low"] <= curr["low"]  <= ob["ob_high"] or
                   ob["ob_low"] <= curr["high"] <= ob["ob_high"] or
                   (curr["low"] <= ob["ob_low"] and curr["high"] >= ob["ob_high"]))

        if in_zone:
            if side == "BUY" and curr["close"] > curr["open"] and curr["close"] > ob["ob_mid"]:
                return {
                    "passed": True,
                    "tag":    "M15_OB_DEMAND_REJECT",
                    "reason": (f"M15 demand OB retest rejection "
                               f"(close:{curr['close']:.5f} > "
                               f"ob_mid:{ob['ob_mid']:.5f})"),
                    "ob":     ob,
                }
            if side == "SELL" and curr["close"] < curr["open"] and curr["close"] < ob["ob_mid"]:
                return {
                    "passed": True,
                    "tag":    "M15_OB_SUPPLY_REJECT",
                    "reason": (f"M15 supply OB retest rejection "
                               f"(close:{curr['close']:.5f} < "
                               f"ob_mid:{ob['ob_mid']:.5f})"),
                    "ob":     ob,
                }
            return {
                "passed": False,
                "tag":    "M15_OB_NO_REJECT",
                "reason": "In M15 OB zone but no rejection candle yet",
            }

    return {
        "passed": False,
        "tag":    "M15_NO_SWEEP_OR_OB",
        "reason": (f"No M15 liquidity sweep or OB retest found "
                   f"(sweep:{sweep})"),
    }


# =============================================================================
# MASTER FUNCTION — check_intraday_confirmations()
# =============================================================================
def check_intraday_confirmations(symbol: str,
                                  candles_m15: list,
                                  candles_h1: list,
                                  candles_h4: list,
                                  side: str) -> dict:
    """
    Runs the intraday confirmation gate — LOCKED STRATEGY:
    H4 = main direction | H1 = valid POI (mandatory) | M15 = entry engine.

    Args:
        symbol      : e.g. "EURUSD"
        candles_m15 : list of M15 OHLCV dicts
        candles_h1  : list of H1 OHLCV dicts
        candles_h4  : list of H4 OHLCV dicts
        side        : "BUY" or "SELL"

    Returns:
        {
            score    : int  (0–3, H4/M15 confirmations)
            passed   : bool
            action   : "ENTER" | "WAIT" | "SKIP"
            c0       : H1 POI check (mandatory gate)
            c1, c2, c3 : individual H4/M15 results
            summary  : str
        }
    """
    c0 = _check_h1_poi(candles_h1, side)
    if not c0["passed"]:
        summary = (f"[INTRA CONF] {symbol} {side} | SKIP — "
                   f"H1 POI missing: {c0['reason']}")
        print(summary)
        return {
            "score": 0, "passed": False, "action": "SKIP",
            "c0": c0, "c1": None, "c2": None, "c3": None,
            "summary": summary, "symbol": symbol, "side": side,
        }

    c1 = _check_h4_trend(candles_h4, side)
    c2 = _check_m15_bos(candles_m15, side)
    c3 = _check_m15_liq_sweep_or_ob(candles_m15, side)

    score = sum([c1["passed"], c2["passed"], c3["passed"]])

    if score == 3:
        action = "ENTER"
    elif score == 2:
        action = "WAIT"
    else:
        action = "SKIP"

    summary = (
        f"[INTRA CONF] {symbol} {side} | Score:{score}/3 | {action}\n"
        f"  C0 H1 POI   : ✅ {c0['reason']}\n"
        f"  C1 H4 Trend : {'✅' if c1['passed'] else '❌'} {c1['reason']}\n"
        f"  C2 M15 BOS  : {'✅' if c2['passed'] else '❌'} {c2['reason']}\n"
        f"  C3 Sweep/OB : {'✅' if c3['passed'] else '❌'} {c3['reason']}"
    )

    print(summary)

    return {
        "score":   score,
        "passed":  score == 3,
        "action":  action,
        "c0":      c0,
        "c1":      c1,
        "c2":      c2,
        "c3":      c3,
        "summary": summary,
        "symbol":  symbol,
        "side":    side,
    }
