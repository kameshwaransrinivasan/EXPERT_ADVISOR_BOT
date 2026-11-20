# =========================================
# fxjers_signal.py — Signal Engine
# Module A: Swing | B: Intraday | C: Scalp
# EA BOT v2 | Directive Refactor
#
# Intraday (consolidated — H4/H1 mandatory, M15 = single scored layer):
#   - MANDATORY: H4 structure, H1 structure agreement, H1 fresh zone,
#     H1 zone+FVG confluence — this is the "H4 direction -> H1 POI"
#     backbone and is never scored, only gated.
#   - SCORED (need MIN_M15_SCORE of 5): M15 BOS alignment, M15 wick
#     rejection >40%, M15 zone+FVG confluence, M15 liquidity-sweep-or-
#     OB-retest (reused from intraday_confirmation.py), and the M1-
#     zone/pattern/displacement OR-set. These used to be enforced as
#     3 separately-mandatory gates plus a whole second confirmation
#     module (check_intraday_confirmations(), which also re-derived
#     H1/H4 a second time and re-fetched M15 candles independently,
#     creating a data-freshness race with the checks here) stacked on
#     top of this function's own mandatory chain — collapsed into one
#     scored M15 decision, computed once, reusing one candle fetch.
#   - Pattern field always populated (never UNKNOWN)
#   - comment field: "INTRADAY|{pattern}|{trade_type}"
# =========================================

import MetaTrader5 as mt5
from strategy import (get_candles, calculate_volatility,
                      BULL_PATTERNS, BEAR_PATTERNS, is_momentum_bar)
from candles import detect_pattern
from market_structure import (
    get_market_structure, detect_bos,
    detect_liquidity_sweep
)
from zones import (
    get_active_zone,
    zone_has_rejection,
    price_at_ganbox_midline,
    ganbox_levels,
    detect_trendline_break,
    detect_fvg,
    zone_fvg_confluence,
    detect_demand_zones,
    detect_supply_zones,
    mark_touched_zones,
    price_in_zone,
    candle_body,
    candle_range,
)
# Reused rather than re-derived: the M15 sweep/OB-retest check is the one
# genuinely new (non-duplicate) piece from the old check_intraday_confirmations()
# gate. Importing it here means it runs once, on the same M15 candles as
# every other M15 check in this function — no second fetch, no race.
from intraday_confirmation import _check_m15_liq_sweep_or_ob

# Starting threshold for the consolidated M15 score (out of 5 — see the
# scored-components list in intraday_signal()). This is a deliberate
# loosening from "all must pass" to "most must agree" and is a tunable
# starting point, not a validated-optimal number — watch real trade
# outcomes at this threshold before treating it as settled.
MIN_M15_SCORE = 3

TF = {
    "W1":  mt5.TIMEFRAME_W1,
    "D1":  mt5.TIMEFRAME_D1,
    "H4":  mt5.TIMEFRAME_H4,
    "H1":  mt5.TIMEFRAME_H1,
    "M30": mt5.TIMEFRAME_M30,
    "M15": mt5.TIMEFRAME_M15,
    "M5":  mt5.TIMEFRAME_M5,
    "M1":  mt5.TIMEFRAME_M1,
}


def load(symbol, tf_key, count=100):
    return get_candles(symbol, TF[tf_key], count)


def calc_sl(side, zone, candles, buf=0.20):
    vol = calculate_volatility(candles)
    b   = vol * buf
    if side == "BUY":
        return round(zone["low"] - b, 5)
    return round(zone["high"] + b, 5)


def calc_tp(side, entry, sl, rr=2.0):
    risk = abs(entry - sl)
    if side == "BUY":
        return round(entry + risk * rr, 5)
    return round(entry - risk * rr, 5)


# =========================================
# MODULE A — SWING  (LOCKED STRATEGY)
#
# Weekly  -> Macro Direction
# Daily   -> Primary Structure -> STRICT valid Supply/Demand
# VALID D1 ZONE TOUCH -> DIRECT ENTRY
#
# NO mandatory H4 BOS / CHoCH / FVG / confirmation, and NO mandatory H1
# confirmation, before entry. H4 is used ONLY for post-entry monitoring
# (trade_manager.swing_exit()) — never as an entry gate here.
#
# "Strict" D1 zone validation = the impulse candle that created the zone
# must show a materially stronger body/range ratio than the standard
# zone-detection threshold, since there is no lower-timeframe
# confirmation layer to catch a weak zone before entry.
# =========================================
STRICT_ZONE_BODY_RATIO = 0.65   # stricter than zones.py's default 0.60


def _is_strict_valid_zone(zone: dict, candles: list) -> bool:
    """
    STRICT validation for the SWING D1 Supply/Demand POI. Detection
    alone (zones.detect_demand_zones / detect_supply_zones) is not
    enough — the originating impulse candle must clear a higher
    body-strength bar than the normal zone standard.
    """
    impulse_idx = zone["index"] + 1
    if impulse_idx >= len(candles):
        return False
    impulse = candles[impulse_idx]
    rng = candle_range(impulse)
    if rng == 0:
        return False
    return (candle_body(impulse) / rng) >= STRICT_ZONE_BODY_RATIO


def swing_signal(symbol):
    c_w1 = load(symbol, "W1", 60)
    c_d1 = load(symbol, "D1", 120)

    if len(c_d1) < 20:
        return "NO DATA"

    # ---- D1 primary structure (mandatory, not SIDEWAYS) ----
    d1_structure = get_market_structure(c_d1)
    if d1_structure == "SIDEWAYS":
        return "SWING: D1 STRUCTURE UNCLEAR"

    # ---- W1 macro context: must not actively CONTRADICT D1. ----
    # W1 SIDEWAYS is tolerated (no strong macro opinion either way) so
    # we don't over-filter swing frequency waiting for weekly alignment
    # that may take a long time to resolve.
    w1_bias = get_market_structure(c_w1) if len(c_w1) >= 20 else "SIDEWAYS"
    if w1_bias != "SIDEWAYS" and w1_bias != d1_structure:
        return f"SWING: W1 ({w1_bias}) CONTRADICTS D1 ({d1_structure})"

    # ---- D1 STRICT valid Supply/Demand ----
    demand_zones  = mark_touched_zones(c_d1, detect_demand_zones(c_d1))
    supply_zones  = mark_touched_zones(c_d1, detect_supply_zones(c_d1))
    strict_demand = [z for z in demand_zones if z["fresh"] and _is_strict_valid_zone(z, c_d1)]
    strict_supply = [z for z in supply_zones if z["fresh"] and _is_strict_valid_zone(z, c_d1)]

    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return "SWING: NO TICK"
    live_price = (tick.bid + tick.ask) / 2

    # ---- VALID D1 ZONE TOUCH -> DIRECT ENTRY (no H4/H1 gate) ----
    side, touched_zone = None, None
    if d1_structure == "BULLISH":
        for z in reversed(strict_demand):
            if price_in_zone(live_price, z):
                side, touched_zone = "BUY", z
                break
    elif d1_structure == "BEARISH":
        for z in reversed(strict_supply):
            if price_in_zone(live_price, z):
                side, touched_zone = "SELL", z
                break

    if side is None:
        return "SWING: NO VALID D1 ZONE TOUCH"

    entry   = tick.ask if side == "BUY" else tick.bid
    sl      = calc_sl(side, touched_zone, c_d1)
    pattern = f"SWING_D1TOUCH_{side}"

    print(f"SWING | {symbol} | {side} | D1:{d1_structure} W1:{w1_bias}")
    print(f"  D1 zone touch: {touched_zone['low']:.5f}-{touched_zone['high']:.5f} "
          f"| SL:{sl:.5f}")

    return {
        "side":        side,
        "mode":        "SWING",
        "entry":       entry,
        "stop":        sl,
        "tp":          0.0,
        "rr":          3.0,
        "zone":        touched_zone,
        "htf_trend":   d1_structure,
        "w1_bias":     w1_bias,
        "bos":         "DIRECT_TOUCH",
        "sweep":       "NONE",
        "trend_score": 0.80,
        "pattern":     pattern,
        "volatility":  calculate_volatility(c_d1),
        "comment":     f"SWING|{pattern}|NORMAL",
    }


# =========================================
# MODULE B — INTRADAY
# Analysis: H4  |  Entry: M15  |  Precision: M1
#
# Checks (all mandatory unless marked optional):
#   1. H4 structure BULLISH or BEARISH via HH/HL or LH/LL (not SIDEWAYS)
#   2. H1 FRESH Supply/Demand zone tap (untested zone only)
#   3. M15 BOS confirmed, same direction as H4
#   4. M15 wick rejection > 40% from zone
#   5. MANDATORY: zone + FVG confluence (FVG must overlap the H1 zone
#      and match its bias — this is the piece that was removed before)
#   6. Need >=1 of: M1 zone alignment (1M S/D zone matches H1 zone
#      type) OR a real directional M15 candle pattern (inside bar does
#      NOT count for either side — it's neutral, not a confirmation)
# =========================================
def intraday_signal(symbol):
    c_h4  = load(symbol, "H4",  100)
    c_h1  = load(symbol, "H1",  100)
    c_m30 = load(symbol, "M30", 100)
    c_m15 = load(symbol, "M15", 100)
    c_m1  = load(symbol, "M1",  50)

    if any(len(c) < 20 for c in [c_h4, c_h1, c_m15]):
        return "NO DATA"

    # ---- 1. H4 structure (HH/HL or LH/LL, skip SIDEWAYS) ----
    h4_structure = get_market_structure(c_h4)
    if h4_structure == "SIDEWAYS":
        return "INTRADAY: H4 SIDEWAYS"

    # ---- 2. H1 Valid Market Structure ----
    # Previously H1's own structure was never read at all — only H4's —
    # which let H1 be silently bypassed. H1 must not actively contradict
    # H4 (H1 SIDEWAYS is tolerated so we don't over-filter frequency).
    h1_structure = get_market_structure(c_h1)
    if h1_structure != "SIDEWAYS" and h1_structure != h4_structure:
        return f"INTRADAY: H1 STRUCTURE ({h1_structure}) CONTRADICTS H4 ({h4_structure})"

    # ---- 3. H1 Valid Supply/Demand -> Important POI (fresh zone tap) ----
    active_zone = get_active_zone(c_h1)
    if active_zone is None:
        return "INTRADAY: NO FRESH H1 ZONE TAP"

    side = None
    if h4_structure == "BULLISH" and active_zone["type"] == "DEMAND":
        side = "BUY"
    elif h4_structure == "BEARISH" and active_zone["type"] == "SUPPLY":
        side = "SELL"
    else:
        return "INTRADAY: ZONE MISMATCH"

    # ---- 4. H1 Valid FVG (mandatory, not optional) — the ONE H1 ----
    #         zone+FVG check. (A second, near-duplicate FVG confluence
    #         check against the same H1 zone used to also be mandatory
    #         on M15 — that's now one of the scored M15 components below
    #         instead of a second independent hard gate.)
    h1_fvg = detect_fvg(c_h1)
    h1_fvg_ok = zone_fvg_confluence(active_zone, h1_fvg)
    if not h1_fvg_ok:
        return "INTRADAY: NO H1 ZONE+FVG CONFLUENCE"

    # ================================================================
    # M15 ENTRY-TIMING SCORE — H4/H1 above are the mandatory direction
    # + POI gates; M15 is entry timing/quality, scored rather than
    # AND-gated. Need MIN_M15_SCORE of 5:
    #   - M15 BOS confirming H4 direction
    #   - M15 wick rejection > 40%
    #   - M15 zone+FVG confluence (M15-level support for the H1 zone)
    #   - M15 liquidity sweep+reversal OR M15 OB retest+rejection
    #     (reused from intraday_confirmation.py, computed once here —
    #     this is what used to be re-fetched and re-checked independently
    #     by check_intraday_confirmations(), which could disagree with
    #     this function purely from candle-fetch timing, not new info)
    #   - >=1 of: M1 zone alignment / real M15 pattern (excl. inside
    #     bar) / M15 strong displacement (this was already an OR-set —
    #     kept as a single scored component here)
    # ================================================================
    m15_bos = detect_bos(c_m15)
    req_bos = "BULLISH_BOS" if side == "BUY" else "BEARISH_BOS"
    bos_ok  = (m15_bos == req_bos)

    m15_rej = zone_has_rejection(c_m15, active_zone)

    m15_fvg = detect_fvg(c_m15)
    fvg_ok  = zone_fvg_confluence(active_zone, m15_fvg)

    sweep_or_ob = _check_m15_liq_sweep_or_ob(c_m15, side)["passed"]

    m1_zone = get_active_zone(c_m1) if len(c_m1) >= 10 else None
    m1_ok   = m1_zone is not None and m1_zone["type"] == active_zone["type"]

    m15_pattern = detect_pattern(c_m15)
    pattern_ok  = (m15_pattern != "INSIDE_BAR" and (
        (side == "BUY"  and m15_pattern in BULL_PATTERNS) or
        (side == "SELL" and m15_pattern in BEAR_PATTERNS)
    ))

    m15_displacement = is_momentum_bar(c_m15)
    displacement_ok  = ((side == "BUY"  and m15_displacement == "BULLISH_MOMENTUM") or
                        (side == "SELL" and m15_displacement == "BEARISH_MOMENTUM"))

    final_confirm_ok = (m1_ok or pattern_ok or displacement_ok)

    m15_score = sum([bos_ok, m15_rej, fvg_ok, sweep_or_ob, final_confirm_ok])

    if m15_score < MIN_M15_SCORE:
        return (f"INTRADAY: M15 SCORE {m15_score}/5 < {MIN_M15_SCORE} "
                f"(BOS:{bos_ok} Rej:{m15_rej} FVG:{fvg_ok} "
                f"SweepOB:{sweep_or_ob} Final:{final_confirm_ok})")

    # ---- Build signal ----
    sweep = detect_liquidity_sweep(c_h1)
    tick  = mt5.symbol_info_tick(symbol)
    if tick is None:
        return "INTRADAY: NO TICK"

    entry = tick.ask if side == "BUY" else tick.bid
    sl    = calc_sl(side, active_zone, c_m15)

    tags = ["BOS"] if bos_ok else []
    if sweep != "NONE":  tags.append("SWP")
    if h1_fvg_ok:        tags.append("H1FVG")
    if fvg_ok:           tags.append("FVG")
    if m1_ok:            tags.append("M1")
    if pattern_ok:       tags.append(m15_pattern[:4])
    if displacement_ok:  tags.append("DISP")
    pattern = f"INTRA_{'_'.join(tags)}_{side}"[:40]

    print(f"INTRADAY | {symbol} | {side} | H4:{h4_structure} H1:{h1_structure}")
    print(f"  H1 FVG:{h1_fvg_ok} | M15 score:{m15_score}/5 (need {MIN_M15_SCORE}) "
          f"BOS:{bos_ok} Rej:{m15_rej} FVG:{fvg_ok} SweepOB:{sweep_or_ob} "
          f"Final:{final_confirm_ok} (M1:{m1_ok} Pattern:{m15_pattern}/{pattern_ok} "
          f"Disp:{m15_displacement}/{displacement_ok})")
    print(f"  Entry:{entry:.5f} SL:{sl:.5f}")

    return {
        "side":        side,
        "mode":        "INTRADAY",
        "entry":       entry,
        "stop":        sl,
        "tp":          0.0,
        "rr":          2.0,
        "zone":        active_zone,
        "htf_trend":   h4_structure,
        "h1_structure": h1_structure,
        "h1_fvg_confluence": h1_fvg_ok,
        "bos":         m15_bos,
        "sweep":       sweep,
        "fvg_confluence": fvg_ok,
        "m1_zone_align":  m1_ok,
        "m15_score":   m15_score,
        "trend_score": 0.70,
        "pattern":     pattern,
        "volatility":  calculate_volatility(c_m30),
        "comment":     f"INTRADAY|{pattern}|NORMAL",
    }


# =========================================
# MODULE C — SCALPING
# Handled by scalping_pipeline.py
# This legacy function kept for AUTO fallback
# =========================================
def scalp_signal(symbol):
    c_m5 = load(symbol, "M5", 100)
    c_m1 = load(symbol, "M1", 100)

    if any(len(c) < 20 for c in [c_m5, c_m1]):
        return "NO DATA"

    m5_structure = get_market_structure(c_m5)
    sweep        = detect_liquidity_sweep(c_m5)
    tl_break     = detect_trendline_break(c_m5)

    if sweep == "NONE" and tl_break == "NONE":
        return "SCALP: NO LIQUIDITY TRIGGER"

    active_zone = get_active_zone(c_m5) or get_active_zone(c_m1)
    if active_zone is None:
        return "SCALP: NO ZONE"

    side = None
    if   sweep    == "SELL_SIDE_SWEEP"     and active_zone["type"] == "DEMAND": side = "BUY"
    elif sweep    == "BUY_SIDE_SWEEP"      and active_zone["type"] == "SUPPLY": side = "SELL"
    elif tl_break == "BEAR_TRENDLINE_BREAK" and active_zone["type"] == "DEMAND": side = "BUY"
    elif tl_break == "BULL_TRENDLINE_BREAK" and active_zone["type"] == "SUPPLY": side = "SELL"
    else:
        return "SCALP: SWEEP MISMATCH"

    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return "SCALP: NO TICK"

    entry  = tick.ask if side == "BUY" else tick.bid
    levels = ganbox_levels(active_zone)
    at_mid = price_at_ganbox_midline(entry, active_zone)
    deep   = (entry <= levels["50pct"] if side == "BUY" else entry >= levels["50pct"])

    if not at_mid and not deep:
        return "SCALP: WAIT GANBOX MIDLINE"

    sl      = calc_sl(side, active_zone, c_m1, buf=0.10)
    pattern = f"AGGRESSIVE_SCALP_{side}"

    return {
        "side":        side,
        "mode":        "SCALPING",
        "entry":       entry,
        "stop":        sl,
        "tp":          0.0,
        "rr":          2.0,
        "zone":        active_zone,
        "htf_trend":   m5_structure,
        "bos":         detect_bos(c_m1),
        "sweep":       sweep,
        "tl_break":    tl_break,
        "trend_score": 0.65,
        "pattern":     pattern,
        "volatility":  calculate_volatility(c_m5),
        "comment":     f"SCALPING|{pattern}|NORMAL",
    }


# =========================================
# MASTER ROUTER
# =========================================
def fxjers_signal(symbol, module="AUTO"):
    if module == "SWING":    return swing_signal(symbol)
    if module == "INTRADAY": return intraday_signal(symbol)
    if module == "SCALPING": return scalp_signal(symbol)

    sig = intraday_signal(symbol)
    if isinstance(sig, dict):
        return sig
    sig = scalp_signal(symbol)
    if isinstance(sig, dict):
        return sig
    return "NO SIGNAL"
