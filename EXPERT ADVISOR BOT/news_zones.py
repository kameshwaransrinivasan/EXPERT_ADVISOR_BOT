# =========================================
# news_zones.py
# Institutional News Reaction Scalping Bot
# Step 5-6: 30M Zone Touch + LTF Confirmation
# =========================================

import MetaTrader5 as mt5
from strategy import get_candles, calculate_volatility
from zones import (
    detect_demand_zones, detect_supply_zones,
    mark_touched_zones, price_in_zone,
    detect_fvg, zone_has_rejection
)
from market_structure import (
    detect_bos, detect_choch,
    detect_liquidity_sweep, get_swing_highs, get_swing_lows
)
from candles import (
    bullish_engulfing, bearish_engulfing,
    bullish_pinbar, bearish_pinbar, marubozu
)


# =========================================
# SPREAD FILTER
# VERY IMPORTANT — avoid bad fills
# =========================================
def spread_ok(symbol):
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return False
    spread = abs(tick.ask - tick.bid)

    if "GOLD" in symbol or "XAU" in symbol:
        max_spread = 0.80      # Gold tight during news
    elif "JPY" in symbol:
        max_spread = 0.030
    else:
        max_spread = 0.008     # FX pairs — tighter than usual

    ok = spread <= max_spread
    if not ok:
        print(f"🚫 SPREAD TOO WIDE: {spread:.5f} > {max_spread}")
    return ok


def get_spread(symbol):
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return 999
    return round(abs(tick.ask - tick.bid), 5)


# =========================================
# STEP 5 — 30M SUPPLY / DEMAND ZONE TOUCH
# Returns the zone price is currently touching
# =========================================
def get_30m_zone_touch(symbol, direction):
    """
    Checks if price is currently touching a 30M zone
    aligned with the news direction.

    direction = "BUY"  → needs DEMAND zone touch
    direction = "SELL" → needs SUPPLY zone touch

    Returns zone dict or None.
    """
    candles_30m = get_candles(symbol, mt5.TIMEFRAME_M30, 100)
    if len(candles_30m) < 20:
        return None

    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return None
    price = (tick.ask + tick.bid) / 2

    if direction == "BUY":
        zones = mark_touched_zones(candles_30m, detect_demand_zones(candles_30m))
        for zone in reversed(zones):
            if zone["fresh"] and price_in_zone(price, zone):
                print(f"📦 30M DEMAND ZONE TOUCH: {zone['low']:.5f} – {zone['high']:.5f}")
                return zone

    elif direction == "SELL":
        zones = mark_touched_zones(candles_30m, detect_supply_zones(candles_30m))
        for zone in reversed(zones):
            if zone["fresh"] and price_in_zone(price, zone):
                print(f"📦 30M SUPPLY ZONE TOUCH: {zone['low']:.5f} – {zone['high']:.5f}")
                return zone

    return None


# =========================================
# NEAREST ZONE (even if price not in it yet)
# Used for proximity detection
# =========================================
def get_nearest_30m_zone(symbol, direction, max_distance_pct=0.002):
    """
    Returns the nearest fresh 30M zone within
    max_distance_pct of current price, even if
    price hasn't fully entered the zone yet.
    Useful for pre-positioning awareness.
    """
    candles_30m = get_candles(symbol, mt5.TIMEFRAME_M30, 100)
    if len(candles_30m) < 20:
        return None

    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return None
    price = (tick.ask + tick.bid) / 2

    if direction == "BUY":
        zones = mark_touched_zones(candles_30m, detect_demand_zones(candles_30m))
        for zone in reversed(zones):
            if not zone["fresh"]:
                continue
            dist = abs(price - zone["high"]) / price
            if dist <= max_distance_pct or price_in_zone(price, zone):
                return zone

    elif direction == "SELL":
        zones = mark_touched_zones(candles_30m, detect_supply_zones(candles_30m))
        for zone in reversed(zones):
            if not zone["fresh"]:
                continue
            dist = abs(price - zone["low"]) / price
            if dist <= max_distance_pct or price_in_zone(price, zone):
                return zone

    return None


# =========================================
# STEP 6 — LTF CONFIRMATION ENGINE
# Checks 1M and 5M for confirmation signals
#
# Confirmation Strengths:
# BOS / CHOCH          → VERY STRONG (3 pts)
# Engulfing candle      → STRONG      (2 pts)
# Liquidity sweep rej   → STRONG      (2 pts)
# Rejection wick        → MEDIUM      (1 pt)
# Momentum candle       → MEDIUM      (1 pt)
# =========================================

def check_confirmations(symbol, direction, timeframe="M1"):
    """
    Checks LTF for confirmation signals.
    Returns dict with list of confirmations found
    and total strength score.

    direction = "BUY" or "SELL"
    timeframe = "M1" or "M5"
    """
    tf_map = {
        "M1":  mt5.TIMEFRAME_M1,
        "M5":  mt5.TIMEFRAME_M5,
        "M15": mt5.TIMEFRAME_M15,
    }
    tf = tf_map.get(timeframe, mt5.TIMEFRAME_M1)
    candles = get_candles(symbol, tf, 50)

    if len(candles) < 10:
        return {"confirmations": [], "score": 0, "count": 0}

    confirmations = []
    score         = 0
    current       = candles[-1]
    previous      = candles[-2]

    # ---- BOS ----
    bos = detect_bos(candles)
    if direction == "BUY"  and bos == "BULLISH_BOS":
        confirmations.append("BOS_BULLISH")
        score += 3
    if direction == "SELL" and bos == "BEARISH_BOS":
        confirmations.append("BOS_BEARISH")
        score += 3

    # ---- CHOCH ----
    choch = detect_choch(candles)
    if direction == "BUY"  and choch == "BULLISH_CHOCH":
        confirmations.append("CHOCH_BULLISH")
        score += 3
    if direction == "SELL" and choch == "BEARISH_CHOCH":
        confirmations.append("CHOCH_BEARISH")
        score += 3

    # ---- ENGULFING ----
    if direction == "BUY"  and bullish_engulfing(previous, current):
        confirmations.append("BULLISH_ENGULFING")
        score += 2
    if direction == "SELL" and bearish_engulfing(previous, current):
        confirmations.append("BEARISH_ENGULFING")
        score += 2

    # ---- LIQUIDITY SWEEP REJECTION ----
    sweep = detect_liquidity_sweep(candles)
    if direction == "BUY"  and sweep == "SELL_SIDE_SWEEP":
        confirmations.append("SELL_SIDE_SWEEP")
        score += 2
    if direction == "SELL" and sweep == "BUY_SIDE_SWEEP":
        confirmations.append("BUY_SIDE_SWEEP")
        score += 2

    # ---- REJECTION WICK ----
    from candles import lower_wick, upper_wick, candle_range
    rng = candle_range(current)
    if rng > 0:
        if direction == "BUY"  and lower_wick(current) / rng > 0.45:
            confirmations.append("REJECTION_WICK_BULL")
            score += 1
        if direction == "SELL" and upper_wick(current) / rng > 0.45:
            confirmations.append("REJECTION_WICK_BEAR")
            score += 1

    # ---- MOMENTUM CANDLE ----
    from candles import candle_body
    body = candle_body(current)
    if rng > 0 and body / rng > 0.70:
        if direction == "BUY"  and current["close"] > current["open"]:
            confirmations.append("MOMENTUM_BULL")
            score += 1
        if direction == "SELL" and current["close"] < current["open"]:
            confirmations.append("MOMENTUM_BEAR")
            score += 1

    # ---- FVG ----
    fvg = detect_fvg(candles)
    if fvg:
        if direction == "BUY"  and fvg["type"] == "BULLISH_FVG":
            confirmations.append("BULLISH_FVG")
            score += 1
        if direction == "SELL" and fvg["type"] == "BEARISH_FVG":
            confirmations.append("BEARISH_FVG")
            score += 1

    print(f"🔍 {timeframe} CONFIRMATIONS [{direction}]: {confirmations} | Score: {score}")

    return {
        "confirmations": confirmations,
        "score":         score,
        "count":         len(confirmations),
        "candles":       candles,
    }


# =========================================
# STEP 6 COMBINED — Check both M1 and M5
# Returns best result
# =========================================
def get_best_confirmation(symbol, direction):
    """
    Checks M1 first (faster), then M5.
    Returns the one with higher score.
    """
    m1 = check_confirmations(symbol, direction, "M1")
    m5 = check_confirmations(symbol, direction, "M5")

    if m1["score"] >= m5["score"]:
        m1["timeframe"] = "M1"
        return m1
    m5["timeframe"] = "M5"
    return m5


# =========================================
# STEP 9 — STOP LOSS CALCULATOR
# SL above supply zone high (SELL)
# SL below demand zone low (BUY)
# =========================================
def calculate_news_sl(symbol, direction, zone, buffer_multiplier=0.20):
    """
    Places SL just beyond the zone boundary.
    Buffer = 20% of volatility to avoid tight stops.
    """
    candles = get_candles(symbol, mt5.TIMEFRAME_M5, 20)
    vol     = calculate_volatility(candles) if len(candles) >= 10 else 0.0005
    buf     = vol * buffer_multiplier

    if direction == "BUY":
        sl = round(zone["low"] - buf, 5)
    else:
        sl = round(zone["high"] + buf, 5)

    return sl


# =========================================
# VOLATILITY STATE CHECK
# Used to decide if market is calm enough
# =========================================
def volatility_ok(symbol):
    """
    Returns True if volatility is within acceptable range.
    After big news, volatility can be too extreme to trade.
    """
    candles_1m = get_candles(symbol, mt5.TIMEFRAME_M1, 10)
    if len(candles_1m) < 5:
        return True

    ranges = [c["high"] - c["low"] for c in candles_1m[-5:]]
    avg    = sum(ranges) / len(ranges)

    # Gold: max avg range per 1M candle
    if "GOLD" in symbol or "XAU" in symbol:
        return avg <= 3.0

    # JPY pairs
    if "JPY" in symbol:
        return avg <= 0.300

    # FX pairs
    return avg <= 0.0030
