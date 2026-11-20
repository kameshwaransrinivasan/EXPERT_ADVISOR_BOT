# =============================================================================
# scalping_pipeline.py — Sweet Spot Scalping Engine
# EA BOT v2 | Directive Refactor
#
# Changes from directive:
#   - FVG completely removed from scoring and signal
#   - TF anchored: M1 entry | M5 trend filter (not M15 anymore)
#   - New 3-axis scoring matrix (max 6 pts, min 4 to trade):
#       +2  M5 trend alignment (price vs M5 EMA50)
#       +2  M1/M5 BOS or MSS confirmed
#       +2  M1 candlestick pattern (Engulfing/Pinbar/InsideBar/body pressure)
#   - ML gate ≥ 0.55 (advisory, not blocking — adds to score only)
#   - Limit order at M1 swing high/low boundary (no FVG needed)
#   - BE hook: 8–10 pips → SL = entry + spread (risk-free)
#   - 70/30 partial close: 70% at 1:1.5 RR, 30% at 1:2 RR
#   - CHOPPY_RANGE: mean reversion at zone extremes, 50% lot
#   - Comment format: "SCALPING|{pattern}" for closed_memory.py
# =============================================================================

import MetaTrader5 as mt5
from datetime import datetime, timezone

from strategy         import get_candles, calculate_volatility, is_momentum_bar
from candles          import (bullish_engulfing, bearish_engulfing,
                               bullish_pinbar, bearish_pinbar,
                               inside_bar, candle_body, candle_range)
from market_structure import (detect_bos, detect_choch,
                               detect_liquidity_sweep,
                               get_market_structure)
from zones            import (get_active_zone, detect_fvg, zone_fvg_confluence,
                               detect_demand_zones, detect_supply_zones,
                               mark_touched_zones, price_in_zone)
from execution        import (place_limit_order, place_trade,
                               close_position, is_scalp_whitelisted,
                               get_spread_pips, _spread_ok)
# Reuse existing validated BOS/CHoCH (body-close) and OB-retest checks
# instead of re-deriving them — locked SCALPING strategy needs both a
# validated M5 BOS/CHoCH and an M1 retest, and these already do it right.
from scalping_confirmation import _check_m5_bos_body, _check_m1_ob_retest


# =============================================================================
# CONFIG
# =============================================================================
SCALP_SYMBOLS   = [
    "GOLD",
    "EURUSD",
    "GBPUSD",
]
MIN_SCORE       = 2        # min of {DISPLACEMENT, BOS_CHOCH, RETEST} required (FVG is mandatory, scored separately)
ML_GATE         = 0.55     # ML advisory gate (+1 pt if passes)
NEWS_BUFFER_MIN = 15       # ±15 min high-impact news freeze
BE_PIPS         = 8        # break-even trigger pips
PARTIAL_RR_1    = 1.5      # 70% close at 1:1.5 RR
PARTIAL_RR_2    = 2.0      # 30% close at 1:2 RR
EMA_PERIOD      = 50       # M5 EMA period (changed from M15)


# =============================================================================
# EMA HELPER
# =============================================================================
def _ema(values: list, period: int) -> float:
    if len(values) < period:
        return sum(values) / len(values) if values else 0.0
    k   = 2 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = v * k + ema * (1 - k)
    return ema


def get_m5_ema50(symbol: str) -> float:
    """M5 EMA(50) — trend direction filter."""
    candles = get_candles(symbol, mt5.TIMEFRAME_M5, 120)
    if len(candles) < EMA_PERIOD:
        return 0.0
    return _ema([c["close"] for c in candles], EMA_PERIOD)


# =============================================================================
# NEWS BUFFER  (±15 min)
# =============================================================================
def news_buffer_ok(symbol: str, news_cache: list) -> bool:
    """Returns False if within ±15 min of HIGH impact news."""
    from news_bias import get_pair_currencies
    now   = datetime.now(timezone.utc)
    currs = get_pair_currencies(symbol)
    rel   = {currs.get("base", ""), currs.get("quote", "")}

    for item in news_cache:
        try:
            if str(item.get("impact", "")).upper() != "HIGH":
                continue
            if str(item.get("currency", "")).upper() not in rel:
                continue
            diff_min = abs((item["time"] - now).total_seconds()) / 60
            if diff_min <= NEWS_BUFFER_MIN:
                return False
        except Exception:
            continue
    return True


# =============================================================================
# CANDLESTICK PATTERN DETECTOR
# Axis 3 of scoring: Engulfing, Pinbar, InsideBar, Body Pressure
# Returns (pattern_name, is_bullish_match, is_bearish_match)
# =============================================================================
def detect_candle_signal(candles_m1: list, side: str) -> tuple:
    """
    Detects:
      - Bullish/Bearish Engulfing
      - Bullish/Bearish Pinbar
      - Inside Bar (neutral — counts for both)
      - Body pressure: body > 60% of candle range = momentum bar

    Returns: (pattern_label: str, matched: bool)
    """
    if len(candles_m1) < 2:
        return ("NO_PATTERN", False)

    curr = candles_m1[-1]
    prev = candles_m1[-2]
    rng  = candle_range(curr)
    body = candle_body(curr)

    # Engulfing
    if side == "BUY" and bullish_engulfing(prev, curr):
        return ("BULLISH_ENGULFING", True)
    if side == "SELL" and bearish_engulfing(prev, curr):
        return ("BEARISH_ENGULFING", True)

    # Pinbar
    if side == "BUY" and bullish_pinbar(curr):
        return ("BULLISH_PINBAR", True)
    if side == "SELL" and bearish_pinbar(curr):
        return ("BEARISH_PINBAR", True)

    # Inside bar — neutral consolidation, does NOT confirm either direction
    if inside_bar(prev, curr):
        return ("INSIDE_BAR", False)

    # Body pressure momentum bar
    if rng > 0 and (body / rng) >= 0.60:
        if side == "BUY"  and curr["close"] > curr["open"]:
            return ("MOMENTUM_BULL", True)
        if side == "SELL" and curr["close"] < curr["open"]:
            return ("MOMENTUM_BEAR", True)

    return ("NO_PATTERN", False)


# =============================================================================
# LOCKED SCALPING STRATEGY — M5 LEG
# M5 Market Structure -> Valid Supply/Demand -> Valid BOS/CHoCH -> POI
#
# Detection alone is never enough here: the zone must be FRESH (untested)
# and must line up with the current M5 structure, and the BOS/CHoCH must
# be a validated body-close break (reused from scalping_confirmation.py),
# not a raw wick touch.
# =============================================================================
def _get_m5_context(candles_m5: list) -> tuple:
    """
    Returns (side, poi_zone, m5_structure).
    side/poi_zone are None if there is no valid M5 POI yet.
    """
    m5_structure = get_market_structure(candles_m5)
    if m5_structure == "SIDEWAYS":
        return None, None, m5_structure

    demand = mark_touched_zones(candles_m5, detect_demand_zones(candles_m5))
    supply = mark_touched_zones(candles_m5, detect_supply_zones(candles_m5))
    fresh_demand = [z for z in demand if z["fresh"]]
    fresh_supply = [z for z in supply if z["fresh"]]

    price = candles_m5[-1]["close"]

    if m5_structure == "BULLISH":
        for z in reversed(fresh_demand):
            if price_in_zone(price, z):
                return "BUY", z, m5_structure
    elif m5_structure == "BEARISH":
        for z in reversed(fresh_supply):
            if price_in_zone(price, z):
                return "SELL", z, m5_structure

    return None, None, m5_structure


# =============================================================================
# LOCKED SCALPING STRATEGY — M1 ENTRY ENGINE
# M1 Structure Confirmation -> Strong Displacement -> Valid BOS/CHoCH ->
# VALID FVG/Refined Zone -> Retest -> ENTRY
#
# VALID FVG / REFINED ZONE IS MANDATORY — NOT OPTIONAL. Detection alone
# is not enough: the FVG must pass existing validation (zone_fvg_
# confluence when a matching M1 zone exists, i.e. it must line up on
# the same trade idea rather than be a lone/unrelated gap).
#
# Structure/Displacement/BOS-CHoCH/Retest are scored as an OR-matrix
# (min 2 of 3, excluding structure which stays context-only) so the
# mandatory FVG gate doesn't get stacked with an equally-mandatory
# everything-else and needlessly choke trade frequency.
# =============================================================================
def _m1_entry_confirmations(candles_m1: list, side: str) -> tuple:
    confs      = []
    breakdown  = {}

    # ---- Structure confirmation (context only, not a hard block) ----
    m1_structure = get_market_structure(candles_m1)
    breakdown["M1_STRUCTURE"] = m1_structure

    # ---- Strong displacement ----
    displacement = is_momentum_bar(candles_m1)
    disp_ok = ((side == "BUY"  and displacement == "BULLISH_MOMENTUM") or
               (side == "SELL" and displacement == "BEARISH_MOMENTUM"))
    if disp_ok:
        confs.append("DISPLACEMENT")
    breakdown["M1_DISPLACEMENT"] = displacement

    # ---- Valid BOS/CHoCH ----
    m1_bos   = detect_bos(candles_m1)
    m1_choch = detect_choch(candles_m1)
    bos_ok = ((side == "BUY"  and (m1_bos == "BULLISH_BOS" or m1_choch == "BULLISH_CHOCH")) or
              (side == "SELL" and (m1_bos == "BEARISH_BOS" or m1_choch == "BEARISH_CHOCH")))
    if bos_ok:
        confs.append("BOS_CHOCH")
    breakdown["M1_BOS_CHOCH"] = f"bos:{m1_bos} choch:{m1_choch}"

    # ---- MANDATORY: Valid FVG / Refined Zone ----
    m1_fvg  = detect_fvg(candles_m1)
    m1_zone = get_active_zone(candles_m1)

    fvg_side_ok = (m1_fvg is not None and
                   ((side == "BUY"  and m1_fvg["type"] == "BULLISH_FVG") or
                    (side == "SELL" and m1_fvg["type"] == "BEARISH_FVG")))

    if fvg_side_ok and m1_zone is not None:
        # Both exist — validate they line up on the same trade idea
        # (reuses the existing zone+FVG confluence validator).
        fvg_valid = zone_fvg_confluence(m1_zone, m1_fvg)
    elif fvg_side_ok:
        # No competing M1 zone — the FVG itself is the refined entry
        # zone, already side-validated above.
        fvg_valid = True
    else:
        fvg_valid = False

    if fvg_valid:
        confs.append("FVG_REFINED_ZONE")
    breakdown["M1_FVG_ZONE"] = (
        f"fvg:{m1_fvg['type'] if m1_fvg else None} "
        f"zone:{m1_zone['type'] if m1_zone else None} valid:{fvg_valid}"
    )

    # ---- Retest (reuses the existing M1 order-block retest check) ----
    retest = _check_m1_ob_retest(candles_m1, side)
    if retest["passed"]:
        confs.append("RETEST")
    breakdown["M1_RETEST"] = retest["reason"]

    return confs, breakdown, fvg_valid


# =============================================================================
# STOP LOSS — structural invalidation
# =============================================================================
def calc_scalp_sl(symbol: str, side: str, candles_m1: list) -> float:
    info  = mt5.symbol_info(symbol)
    point = info.point if info else 0.00001
    buf   = point * 30   # 3 pip buffer

    if side == "BUY":
        swing_low = min(c["low"] for c in candles_m1[-10:])
        return round(swing_low - buf, 5)
    else:
        swing_high = max(c["high"] for c in candles_m1[-10:])
        return round(swing_high + buf, 5)


# =============================================================================
# LIMIT PRICE — at M1 swing boundary (replaces FVG boundary)
# =============================================================================
def get_structural_limit_price(candles_m1: list, side: str) -> float | None:
    """
    Places limit order at the most recent M1 swing pivot:
      BUY  → last M1 swing LOW (price expected to retrace to it)
      SELL → last M1 swing HIGH (price expected to retrace to it)

    Returns None if current price is already past the pivot
    (use market order instead).
    """
    if len(candles_m1) < 5:
        return None

    tick = None
    # Find the most recent swing pivot in last 10 bars
    if side == "BUY":
        # Pivot = lowest low of last 5 bars
        pivot = min(c["low"] for c in candles_m1[-5:])
        return round(pivot, 5)
    else:
        pivot = max(c["high"] for c in candles_m1[-5:])
        return round(pivot, 5)


# =============================================================================
# MAIN SIGNAL ENGINE — LOCKED SCALPING STRATEGY
# M5 (Market Structure -> Valid S/D -> Valid BOS/CHoCH -> POI)
#   -> M1 (Structure Confirmation -> Displacement -> BOS/CHoCH ->
#          FVG/Refined Zone -> Retest -> ENTRY)
# =============================================================================
def get_scalp_signal(symbol: str, news_cache: list) -> dict | str:
    """
    Full scalping signal pipeline — Pure Strategy, No ML.
    Locked flow: M5 establishes the valid POI, M1 is the precision entry
    engine. Detection alone never triggers a trade — every BOS/CHoCH/
    zone/FVG used here is validated (fresh zone, body-close BOS/CHoCH,
    FVG confluence) before it counts.
    """

    # ---- 1. Whitelist ----
    if not is_scalp_whitelisted(symbol):
        return f"WHITELIST REJECT: {symbol}"

    # ---- 2. Spread gate ----
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return "NO TICK"
    spread = abs(tick.ask - tick.bid)
    if not _spread_ok(symbol, spread):
        return f"SPREAD {get_spread_pips(symbol):.1f}pips > limit"

    # ---- 3. News buffer ±15 min ----
    if not news_buffer_ok(symbol, news_cache):
        return f"NEWS BUFFER: ±{NEWS_BUFFER_MIN}min freeze"

    # ---- 4. Candle data (M1 + M5) ----
    c_m1 = get_candles(symbol, mt5.TIMEFRAME_M1, 100)
    c_m5 = get_candles(symbol, mt5.TIMEFRAME_M5, 150)

    if len(c_m1) < 20 or len(c_m5) < 30:
        return "INSUFFICIENT CANDLE DATA"

    # ---- 5. M5 LEG: Market Structure -> Valid S/D -> POI ----
    side, poi_zone, m5_structure = _get_m5_context(c_m5)
    if side is None:
        return f"SCALP: NO VALID M5 POI (structure:{m5_structure})"

    # ---- 6. M5 LEG: Valid BOS/CHoCH (body-close validated) ----
    m5_bos_check = _check_m5_bos_body(c_m5, side)
    if not m5_bos_check["passed"]:
        return f"SCALP: M5 BOS/CHOCH NOT VALID ({m5_bos_check['reason']})"

    # ---- 7. M1 ENTRY ENGINE: structure / displacement / BOS-CHoCH / ----
    #         VALID FVG-refined-zone (mandatory) / retest
    confs, breakdown, fvg_valid = _m1_entry_confirmations(c_m1, side)

    print(f"[SCALP] {symbol} {side} | M5 POI:{poi_zone['type']} "
          f"BOS:{m5_bos_check['tag']} | M1 confs:{confs}")
    for k, v in breakdown.items():
        print(f"  {k}: {v}")

    # ---- 7a. MANDATORY: Valid FVG / Refined Zone — never optional ----
    if not fvg_valid:
        return f"SCALP: NO VALID M1 FVG/REFINED ZONE ({breakdown['M1_FVG_ZONE']})"

    # ---- 7b. Remaining M1 axes (displacement/BOS-CHoCH/retest) scored ----
    soft_confs = [c for c in confs if c != "FVG_REFINED_ZONE"]
    if len(soft_confs) < MIN_SCORE:
        return (f"SCALP: M1 ONLY {len(soft_confs)}/{MIN_SCORE} "
                f"CONFIRMATIONS (besides mandatory FVG) {soft_confs}")

    # ---- 8. SL at structural invalidation ----
    sl = calc_scalp_sl(symbol, side, c_m1)

    entry_ref  = tick.ask if side == "BUY" else tick.bid
    volatility = calculate_volatility(c_m1)
    pattern    = f"M5POI_M1_{'_'.join(confs)}"[:40]

    return {
        "side":          side,
        "stop":          sl,
        "score":         len(confs),
        "confs":         confs,
        "breakdown":     breakdown,
        "m5_structure":  m5_structure,
        "m5_bos":        m5_bos_check["tag"],
        "zone":          poi_zone,
        "ml_score":      0.0,
        "trend_score":   min(len(confs) / 4, 1.0),
        "pattern":       pattern,          # real pattern name — never UNKNOWN
        "volatility":    volatility,
        "mode":          "SCALPING",
        "entry_ref":     entry_ref,
        "comment":       f"SCALPING|{pattern}|NORMAL",  # pipe format for parser
    }


# =============================================================================
# 70/30 ELASTIC PARTIAL CLOSE
# 70% at 1:1.5 RR | 30% rides to 1:2 RR
# =============================================================================
def manage_scalp_partials(position, _partials_done: set) -> set:
    ticket = position.ticket
    symbol = position.symbol
    tick   = mt5.symbol_info_tick(symbol)
    if tick is None:
        return _partials_done

    entry = position.price_open
    sl    = position.sl
    if sl == 0:
        return _partials_done

    risk = abs(entry - sl)
    info = mt5.symbol_info(symbol)
    if info is None:
        return _partials_done

    price_now = tick.bid if position.type == mt5.ORDER_TYPE_BUY else tick.ask

    # ---- Phase 1: 70% close at 1:1.5 ----
    if ticket not in _partials_done:
        target_15 = (entry + risk * PARTIAL_RR_1
                     if position.type == mt5.ORDER_TYPE_BUY
                     else entry - risk * PARTIAL_RR_1)

        reached = ((position.type == mt5.ORDER_TYPE_BUY  and price_now >= target_15) or
                   (position.type == mt5.ORDER_TYPE_SELL and price_now <= target_15))

        if reached:
            vol_70 = round(position.volume * 0.70, 2)
            vol_70 = max(info.volume_min,
                         round(round(vol_70 / info.volume_step) * info.volume_step, 2))

            if vol_70 >= position.volume:
                # 70% of this position rounds back up to the full position
                # size (broker volume_min/volume_step won't allow a real
                # split) — this is NOT a 70% partial, it's the whole
                # position closing at the 1.5R trigger. Keeping that exact
                # behavior unchanged per instruction (no move to a 2R
                # target without backtest evidence) — only making sure
                # this case is never recorded as if a 70/30 split happened,
                # in the console log and in the actual MT5 deal comment.
                close_position(position, comment="SCALP_MINLOT_FULL_1.5R")
                _partials_done.add(ticket)
                print(f"MIN-LOT FULL CLOSE (70/30 split not possible — "
                      f"vol_min={info.volume_min}): {symbol} @ 1:1.5 RR")
                return _partials_done

            req = {
                "action":       mt5.TRADE_ACTION_DEAL,
                "symbol":       symbol,
                "volume":       float(vol_70),
                "type":         (mt5.ORDER_TYPE_SELL if position.type == mt5.ORDER_TYPE_BUY
                                 else mt5.ORDER_TYPE_BUY),
                "position":     ticket,
                "price":        price_now,
                "deviation":    20,
                "magic":        777,
                "comment":      "PARTIAL_70",
                "type_time":    mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }
            res = mt5.order_send(req)
            if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                _partials_done.add(ticket)
                print(f"70% PARTIAL CLOSED: {symbol} {vol_70}lots @ 1:1.5 RR")

    # ---- Phase 2: 30% closes at 1:2 ----
    else:
        target_20 = (entry + risk * PARTIAL_RR_2
                     if position.type == mt5.ORDER_TYPE_BUY
                     else entry - risk * PARTIAL_RR_2)

        reached = ((position.type == mt5.ORDER_TYPE_BUY  and price_now >= target_20) or
                   (position.type == mt5.ORDER_TYPE_SELL and price_now <= target_20))

        if reached:
            close_position(position)
            _partials_done.discard(ticket)
            print(f"30% FINAL CLOSE: {symbol} @ 1:2 RR")

    return _partials_done


# =============================================================================
# AGGRESSIVE BE HOOK
# 8–10 pips → SL = entry + spread (risk-free)
# =============================================================================
def apply_be_hook(position):
    symbol = position.symbol
    tick   = mt5.symbol_info_tick(symbol)
    if tick is None:
        return

    entry  = position.price_open
    sl     = position.sl
    info   = mt5.symbol_info(symbol)
    if info is None:
        return

    point  = info.point
    spread = abs(tick.ask - tick.bid)

    # Pip trigger per instrument
    if "GOLD" in symbol or "XAU" in symbol:
        pip_trigger = point * 800
    elif "JPY" in symbol:
        pip_trigger = point * 80
    elif "BTC" in symbol:
        pip_trigger = 80.0
    else:
        pip_trigger = point * 80   # 8 pips standard FX

    rr_trigger = abs(entry - sl) if sl > 0 else pip_trigger
    be_trigger = min(pip_trigger, rr_trigger)

    if position.type == mt5.ORDER_TYPE_BUY:
        if (tick.bid - entry) >= be_trigger and sl < entry:
            new_sl = round(entry + spread, 5)
            req = {
                "action":   mt5.TRADE_ACTION_SLTP,
                "symbol":   symbol,
                "position": position.ticket,
                "sl":       new_sl,
                "tp":       0.0,
            }
            res = mt5.order_send(req)
            if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                pips = round((tick.bid - entry) / point / 10, 1)
                print(f"BE BUY {symbol}: +{pips}pips → SL:{new_sl:.5f} (risk-free)")
    else:
        if (entry - tick.ask) >= be_trigger and (sl > entry or sl == 0):
            new_sl = round(entry - spread, 5)
            req = {
                "action":   mt5.TRADE_ACTION_SLTP,
                "symbol":   symbol,
                "position": position.ticket,
                "sl":       new_sl,
                "tp":       0.0,
            }
            res = mt5.order_send(req)
            if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                pips = round((entry - tick.ask) / point / 10, 1)
                print(f"BE SELL {symbol}: +{pips}pips → SL:{new_sl:.5f} (risk-free)")


# =============================================================================
# STRUCTURAL EXIT (M1 CHoCH / sweep) — LEGACY, NO LONGER CALLED
#
# Superseded by trade_manager.scalping_exit(), which implements the
# LOCKED SCALPING exit rule: M1 is early warning only, M5 is the primary
# confirmation timeframe that actually authorizes the close. This
# function closed on M1 signals alone, which the locked strategy
# explicitly disallows ("do not close solely on one small M1 CHoCH").
# Left in place (not deleted) but no longer invoked from
# manage_scalp_positions() below.
# =============================================================================
def _scalp_structural_exit(position):
    symbol = position.symbol
    c_m1   = get_candles(symbol, mt5.TIMEFRAME_M1, 50)
    if len(c_m1) < 10:
        return

    from strategy import candle_pressure
    choch = detect_choch(c_m1)
    sweep = detect_liquidity_sweep(c_m1)
    pres  = candle_pressure(c_m1)

    if position.type == mt5.ORDER_TYPE_BUY:
        if choch == "BEARISH_CHOCH":
            print(f"SCALP EXIT CHoCH BEARISH: {symbol}")
            close_position(position)
        elif sweep == "BUY_SIDE_SWEEP" and pres == "BEARISH":
            print(f"SCALP EXIT SWEEP+PRESSURE: {symbol}")
            close_position(position)
    else:
        if choch == "BULLISH_CHOCH":
            print(f"SCALP EXIT CHoCH BULLISH: {symbol}")
            close_position(position)
        elif sweep == "SELL_SIDE_SWEEP" and pres == "BULLISH":
            print(f"SCALP EXIT SWEEP+PRESSURE: {symbol}")
            close_position(position)


# =============================================================================
# POSITION MANAGER  (called every 10s from scalping thread)
# =============================================================================
def manage_scalp_positions(partials_done: set) -> set:
    positions = mt5.positions_get() or []
    for pos in positions:
        if "SCALPING" not in pos.comment.upper():
            continue
        try:
            apply_be_hook(pos)
            partials_done = manage_scalp_partials(pos, partials_done)
            # Structural (reversal) exit is handled by trade_manager's
            # two-tier M1-warning/M5-confirm logic, called from the main
            # position-management loop — not here. See note above.
        except Exception as e:
            print(f"[SCALP MGMT ERROR] {pos.symbol}: {e}")
    return partials_done
