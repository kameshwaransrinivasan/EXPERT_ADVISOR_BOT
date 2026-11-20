# =========================================
# strategy.py — Core Strategy Engine
# EA BOT v2 | Active Normal Mode
#
# Entry logic change:
#   OLD: Zone AND FVG AND Sweep AND Pattern (ALL required)
#   NEW: Zone MANDATORY + ANY 2 of (FVG, Sweep, Pattern/Structure)
#
# New patterns added:
#   INSIDE_BAR, MOMENTUM_BAR (body > 2x avg of last 5)
#
# Zone depth entry:
#   Price > 75% deep into zone → entry allowed
#   even without a clear candle pattern
# =========================================

import MetaTrader5 as mt5
import pandas as pd

from candles import detect_pattern, candle_body, candle_range
from smc import smc_analysis, supply_demand_analysis, liquidity_engine
from zones import (
    detect_demand_zones, detect_supply_zones, mark_touched_zones,
    detect_fvg as detect_fvg_zone, zone_fvg_confluence,
)


# ---- GET CANDLES ----
def get_candles(symbol, timeframe, count=100):
    rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, count)
    if rates is None:
        return []
    df = pd.DataFrame(rates)
    return [
        {"open": r["open"], "high": r["high"],
         "low": r["low"], "close": r["close"],
         "volume": r["tick_volume"]}
        for _, r in df.iterrows()
    ]


# ---- TREND ----
def get_trend(candles):
    if len(candles) < 20:
        return "SIDEWAYS"
    closes = [c["close"] for c in candles]
    sma20  = sum(closes[-20:]) / 20
    cur    = closes[-1]
    if cur > sma20:  return "BULLISH"
    if cur < sma20:  return "BEARISH"
    return "SIDEWAYS"

def get_5m_trend(symbol):
    return get_trend(get_candles(symbol, mt5.TIMEFRAME_M5, 50))


# ---- TREND SCORE ----
def calculate_trend_score(candles):
    if len(candles) < 20:
        return 0.5
    closes = [c["close"] for c in candles]
    sma20  = sum(closes[-20:]) / 20
    sma10  = sum(closes[-10:]) / 10
    cur    = closes[-1]
    score  = 0.50
    score += 0.15 if cur > sma20 else -0.15
    score += 0.10 if sma10 > sma20 else -0.10
    score += 0.10 if closes[-1] > closes[-5] else -0.10
    last3  = candles[-3:]
    if all(c["close"] > c["open"] for c in last3):
        score += 0.10
    return round(max(0.0, min(score, 1.0)), 2)


# ---- VOLATILITY ----
def calculate_volatility(candles):
    if len(candles) < 10:
        return 0.0
    ranges = [c["high"] - c["low"] for c in candles[-10:]]
    return round(sum(ranges) / len(ranges), 5)


# ---- SIDEWAYS FILTER ----
def is_sideways(candles):
    if len(candles) < 20:
        return True
    recent  = candles[-10:]
    ranges  = [c["high"] - c["low"] for c in recent]
    avg_rng = sum(ranges) / len(ranges)
    closes  = [c["close"] for c in recent]
    return abs(closes[-1] - closes[0]) < avg_rng * 0.5


# ---- SPREAD FILTER ----
def market_quality_filter(symbol, candles):
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return False
    spread = abs(tick.ask - tick.bid)
    if "GOLD" in symbol or "XAU" in symbol:
        return spread <= 2.0
    if "JPY" in symbol:
        return spread <= 0.050
    return spread <= 0.020


# ---- CANDLE PRESSURE ----
def candle_pressure(candles):
    if len(candles) < 3:
        return "NEUTRAL"
    c    = candles[-1]
    body = abs(c["close"] - c["open"])
    uw   = c["high"] - max(c["open"], c["close"])
    lw   = min(c["open"], c["close"]) - c["low"]
    if c["close"] > c["open"] and body > uw:  return "BULLISH"
    if c["close"] < c["open"] and body > lw:  return "BEARISH"
    return "NEUTRAL"


# ---- CONTINUATION ----
def continuation_strength(candles):
    if len(candles) < 3:
        return "NONE"
    c1, c2 = candles[-2], candles[-1]
    if c1["close"] > c1["open"] and c2["close"] > c2["open"]: return "BULLISH"
    if c1["close"] < c1["open"] and c2["close"] < c2["open"]: return "BEARISH"
    return "NONE"


# ---- EXHAUSTION ----
def exhaustion_signal(candles):
    if len(candles) < 5:
        return False
    small = 0
    for c in candles[-3:]:
        body = abs(c["close"] - c["open"])
        full = c["high"] - c["low"]
        if full > 0 and body / full < 0.25:
            small += 1
    return small >= 2


# ---- MARKET PHASE ----
def market_phase(candles):
    if len(candles) < 20:
        return "UNKNOWN"
    vol   = calculate_volatility(candles)
    trend = get_trend(candles)
    if vol > 0.003:
        return "EXPANSION"
    if trend in ["BULLISH", "BEARISH"]:
        return "TREND"
    if is_sideways(candles):
        return "CONSOLIDATION"
    return "DISTRIBUTION"


# ---- LIQUIDITY RAID ----
def liquidity_raid(candles):
    if len(candles) < 10:
        return "NONE"
    highs  = [c["high"] for c in candles[-10:]]
    lows   = [c["low"]  for c in candles[-10:]]
    cur    = candles[-1]
    eq_hi  = max(highs[:-1])
    eq_lo  = min(lows[:-1])
    if cur["high"] > eq_hi and cur["close"] < eq_hi: return "BUY_SIDE_RAID"
    if cur["low"]  < eq_lo and cur["close"] > eq_lo: return "SELL_SIDE_RAID"
    return "NONE"


# ---- ACTIVE ZONE ----
def get_current_zone(candles_5m):
    sd    = supply_demand_analysis(candles_5m)
    price = candles_5m[-1]["close"]
    for z in sd["demand_zones"]:
        if z["low"] <= price <= z["high"]:
            return "DEMAND"
    for z in sd["supply_zones"]:
        if z["low"] <= price <= z["high"]:
            return "SUPPLY"
    return "NONE"


# ---- FVG DETECT ----
def detect_fvg(candles):
    if len(candles) < 3:
        return "NONE"
    c1, c3 = candles[-3], candles[-1]
    if c3["low"]  > c1["high"]: return "BULLISH_FVG"
    if c3["high"] < c1["low"]:  return "BEARISH_FVG"
    return "NONE"


# =========================================
# ACTIVE NORMAL MODE — NEW HELPERS
# =========================================

# ---- MOMENTUM BAR ----
# Body > 2x average body of last 5 candles
def is_momentum_bar(candles) -> str:
    """
    Returns 'BULLISH_MOMENTUM' / 'BEARISH_MOMENTUM' / 'NONE'
    Momentum bar = current candle body > 2x avg of last 5 bodies.
    """
    if len(candles) < 6:
        return "NONE"
    last5_bodies = [candle_body(c) for c in candles[-6:-1]]
    avg_body     = sum(last5_bodies) / len(last5_bodies) if last5_bodies else 0
    curr         = candles[-1]
    curr_body    = candle_body(curr)
    if avg_body > 0 and curr_body > avg_body * 2:
        if curr["close"] > curr["open"]:
            return "BULLISH_MOMENTUM"
        return "BEARISH_MOMENTUM"
    return "NONE"


# ---- ZONE DEPTH CHECK ----
# Price > 75% deep into zone → entry without pattern required
def zone_depth_pct(price: float, zone_low: float, zone_high: float,
                   side: str) -> float:
    """
    Returns how deep (0.0–1.0) price is into zone.
    BUY (demand): deeper = closer to zone LOW
    SELL (supply): deeper = closer to zone HIGH
    """
    zone_size = zone_high - zone_low
    if zone_size <= 0:
        return 0.0
    if side == "BUY":
        return (zone_high - price) / zone_size
    else:
        return (price - zone_low) / zone_size


# ---- EXPANDED PATTERN CHECK ----
# Now includes: Engulfing, Pinbar, Marubozu, Inside Bar, Momentum Bar
BULL_PATTERNS = {
    "BULLISH_ENGULFING", "BULLISH_PINBAR",
    "MARUBOZU_BULLISH",  "MARUBOZU",
    "INSIDE_BAR",        "BULLISH_MOMENTUM",
}
BEAR_PATTERNS = {
    "BEARISH_ENGULFING", "BEARISH_PINBAR",
    "MARUBOZU_BEARISH",  "MARUBOZU",
    "INSIDE_BAR",        "BEARISH_MOMENTUM",
}


def get_all_confirmations(candles, side: str, smc_bos: str,
                          fvg: str, raid: str, zone_depth: float) -> list:
    """
    Returns list of active confirmations for OR-matrix scoring.

    Mandatory: zone tap (checked before calling this)
    Flexible:  need ANY 2 from this list

    Confirmations:
        "FVG"     — fair value gap present
        "SWEEP"   — liquidity sweep/raid
        "PATTERN" — candle pattern confirmed
        "BOS"     — break of structure
        "DEPTH"   — price > 75% deep into zone
    """
    confs = []

    # FVG
    if side == "BUY"  and fvg == "BULLISH_FVG":  confs.append("FVG")
    if side == "SELL" and fvg == "BEARISH_FVG":   confs.append("FVG")

    # Liquidity sweep
    if side == "BUY"  and raid in ("SELL_SIDE_RAID", "BUY_SIDE_RAID"):
        confs.append("SWEEP")
    if side == "SELL" and raid in ("BUY_SIDE_RAID", "SELL_SIDE_RAID"):
        confs.append("SWEEP")

    # BOS / CHoCH
    if side == "BUY"  and smc_bos in ("BULLISH_BOS", "BULLISH_CHOCH"):
        confs.append("BOS")
    if side == "SELL" and smc_bos in ("BEARISH_BOS", "BEARISH_CHOCH"):
        confs.append("BOS")

    # Candle pattern (expanded)
    pattern      = detect_pattern(candles)
    momentum_bar = is_momentum_bar(candles)
    effective_pat = momentum_bar if momentum_bar != "NONE" else pattern

    if side == "BUY"  and effective_pat in BULL_PATTERNS: confs.append("PATTERN")
    if side == "SELL" and effective_pat in BEAR_PATTERNS:  confs.append("PATTERN")

    # Zone depth > 75% (no pattern required)
    if zone_depth >= 0.75:
        confs.append("DEPTH")

    return confs


# =========================================
# MEAN REVERSION SIGNAL (CHOPPY_RANGE)
# Buy at demand zone BOTTOM boundary
# Sell at supply zone TOP boundary
# =========================================
def mean_reversion_signal(symbol, candle_function):
    """
    Active Normal Mode — CHOPPY_RANGE regime.
    Fades extremes: buys at a FRESH demand zone bottom, sells at a FRESH
    supply zone top. 50% risk applied by caller via regime risk_multiplier.

    Unlike before, this now REQUIRES:
      1. Zone is FRESH (untested) — not just "price is in some zone"
      2. Zone + FVG confluence (FVG overlaps the zone, same bias)
      3. A real M1 candle pattern confirming that direction
         (inside bar does not count — it's neutral, not a signal)
    This is the same standard of proof as intraday_signal(); the old
    version fired on zone-percentage alone with zero confirmation,
    which is why choppy conditions were producing so many low quality
    entries.
    """
    c5m = candle_function(symbol, mt5.TIMEFRAME_M5, 100)
    c1m = candle_function(symbol, mt5.TIMEFRAME_M1, 50)

    if len(c5m) < 20 or len(c1m) < 5:
        return "NO DATA"

    if not market_quality_filter(symbol, c5m):
        return "BAD MARKET"

    demand_zones = mark_touched_zones(c5m, detect_demand_zones(c5m))
    supply_zones = mark_touched_zones(c5m, detect_supply_zones(c5m))
    fresh_demand = [z for z in demand_zones if z["fresh"]]
    fresh_supply = [z for z in supply_zones if z["fresh"]]

    price = c5m[-1]["close"]
    tick  = mt5.symbol_info_tick(symbol)
    if tick is None:
        return "NO TICK"

    volatility  = calculate_volatility(c5m)
    trend_score = calculate_trend_score(c5m)
    fvg_5m      = detect_fvg_zone(c5m)
    m1_pattern  = detect_pattern(c1m)

    # BUY: fresh demand zone, price in bottom 25%, FVG + pattern required
    for z in fresh_demand:
        zone_low, zone_high = z["low"], z["high"]
        if not (zone_low <= price <= zone_low + (zone_high - zone_low) * 0.25):
            continue
        if not zone_fvg_confluence(z, fvg_5m):
            continue
        if m1_pattern == "INSIDE_BAR" or m1_pattern not in BULL_PATTERNS:
            continue
        sl = round(zone_low - volatility * 0.5, 5)
        print(f"MEAN_REVERT BUY {symbol} | Zone:{zone_low:.5f}-{zone_high:.5f} "
              f"| FVG+{m1_pattern} confirmed")
        return {
            "side":        "BUY",
            "stop":        sl,
            "trend_score": trend_score,
            "pattern":     f"MEAN_REVERT_{m1_pattern}",
            "volatility":  volatility,
            "mode":        "SCALPING",
            "phase":       "CONSOLIDATION",
            "raid":        "NONE",
        }

    # SELL: fresh supply zone, price in top 25%, FVG + pattern required
    for z in fresh_supply:
        zone_low, zone_high = z["low"], z["high"]
        if not (zone_high - (zone_high - zone_low) * 0.25 <= price <= zone_high):
            continue
        if not zone_fvg_confluence(z, fvg_5m):
            continue
        if m1_pattern == "INSIDE_BAR" or m1_pattern not in BEAR_PATTERNS:
            continue
        sl = round(zone_high + volatility * 0.5, 5)
        print(f"MEAN_REVERT SELL {symbol} | Zone:{zone_low:.5f}-{zone_high:.5f} "
              f"| FVG+{m1_pattern} confirmed")
        return {
            "side":        "SELL",
            "stop":        sl,
            "trend_score": trend_score,
            "pattern":     f"MEAN_REVERT_{m1_pattern}",
            "volatility":  volatility,
            "mode":        "SCALPING",
            "phase":       "CONSOLIDATION",
            "raid":        "NONE",
        }

    return "NO MEAN_REVERT SETUP"


# =========================================
# MAIN SMC HYBRID SIGNAL — ACTIVE NORMAL MODE
#
# Entry rules (OR matrix):
#   1. Price MUST be in valid Supply/Demand zone (MANDATORY)
#   2. ANY 2 confirmations from: FVG, Sweep, Pattern, BOS, Depth>75%
# =========================================
def smc_hybrid_signal(symbol, candle_function):
    c1m = candle_function(symbol, mt5.TIMEFRAME_M1, 100)
    c5m = candle_function(symbol, mt5.TIMEFRAME_M5, 100)

    if len(c1m) < 20 or len(c5m) < 20:
        return "NO DATA"

    if not market_quality_filter(symbol, c1m):
        return "BAD MARKET"

    # Exhaustion only blocks if ALL 3 candles are tiny — still strict
    if exhaustion_signal(c1m):
        return "MARKET EXHAUSTION"

    pressure     = candle_pressure(c1m)
    phase        = market_phase(c5m)
    trend_5m     = get_5m_trend(symbol)
    trend_score  = calculate_trend_score(c5m)
    volatility   = calculate_volatility(c5m)
    pattern      = detect_pattern(c1m)
    momentum_bar = is_momentum_bar(c1m)
    effective_pat= momentum_bar if momentum_bar != "NONE" else pattern
    raid         = liquidity_raid(c1m)
    smc          = smc_analysis(c5m)
    zone         = get_current_zone(c5m)
    fvg          = detect_fvg(c1m)
    current      = c1m[-1]

    print(f"RAID:{raid} | PRESSURE:{pressure} | ZONE:{zone}")
    print(f"BOS:{smc['bos']} | FVG:{fvg} | PAT:{effective_pat}")
    print(f"SCORE:{trend_score} | PHASE:{phase}")

    # ---- STEP 1: ZONE CHECK (MANDATORY) ----
    if zone == "NONE":
        return "NO ZONE — waiting for zone tap"

    # ---- Get zone boundaries for depth check ----
    sd     = supply_demand_analysis(c5m)
    price  = c5m[-1]["close"]

    def _get_zone_bounds(zone_type):
        zones_list = sd["demand_zones"] if zone_type == "DEMAND" else sd["supply_zones"]
        for z in zones_list:
            if z["low"] <= price <= z["high"]:
                return z["low"], z["high"]
        return None, None

    # ---- STEP 2: BUY SETUP ----
    if zone == "DEMAND":
        z_low, z_high = _get_zone_bounds("DEMAND")
        depth = zone_depth_pct(price, z_low, z_high, "BUY") if z_low else 0

        confs = get_all_confirmations(
            c1m, "BUY", smc["bos"], fvg, raid, depth)

        print(f"BUY confs: {confs} (need 2)")

        if len(confs) >= 2:
            sl = round(z_low - volatility * 0.3, 5) if z_low else current["low"]
            return {
                "side":        "BUY",
                "stop":        sl,
                "trend_score": trend_score,
                "pattern":     effective_pat,
                "volatility":  volatility,
                "mode":        "SCALPING",
                "phase":       phase,
                "raid":        raid,
                "confs":       confs,
                "zone_depth":  round(depth, 2),
            }
        return f"BUY: only {len(confs)}/2 confs [{confs}]"

    # ---- STEP 3: SELL SETUP ----
    if zone == "SUPPLY":
        z_low, z_high = _get_zone_bounds("SUPPLY")
        depth = zone_depth_pct(price, z_low, z_high, "SELL") if z_low else 0

        confs = get_all_confirmations(
            c1m, "SELL", smc["bos"], fvg, raid, depth)

        print(f"SELL confs: {confs} (need 2)")

        if len(confs) >= 2:
            sl = round(z_high + volatility * 0.3, 5) if z_high else current["high"]
            return {
                "side":        "SELL",
                "stop":        sl,
                "trend_score": trend_score,
                "pattern":     effective_pat,
                "volatility":  volatility,
                "mode":        "SCALPING",
                "phase":       phase,
                "raid":        raid,
                "confs":       confs,
                "zone_depth":  round(depth, 2),
            }
        return f"SELL: only {len(confs)}/2 confs [{confs}]"

    return "NO CONFIRMATION"


def red_news_strategy(symbol, candle_function):
    return smc_hybrid_signal(symbol, candle_function)
