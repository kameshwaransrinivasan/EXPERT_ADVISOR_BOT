# =========================================
# smc.py — Smart Money Concepts Engine
# =========================================

def smc_analysis(candles):
    if len(candles) < 20:
        return {"bos": "NONE", "structure": "SIDEWAYS"}

    highs  = [c["high"]  for c in candles]
    lows   = [c["low"]   for c in candles]
    closes = [c["close"] for c in candles]

    recent_high = max(highs[-10:])
    recent_low  = min(lows[-10:])
    prev_high   = max(highs[-20:-10])
    prev_low    = min(lows[-20:-10])
    current     = closes[-1]

    bos = "NONE"
    if current > recent_high and recent_high > prev_high:
        bos = "BULLISH_BOS"
    elif current < recent_low and recent_low < prev_low:
        bos = "BEARISH_BOS"
    elif current > recent_high and recent_high < prev_high:
        bos = "BULLISH_CHOCH"
    elif current < recent_low and recent_low > prev_low:
        bos = "BEARISH_CHOCH"

    structure = "SIDEWAYS"
    if recent_high > prev_high and recent_low > prev_low:
        structure = "BULLISH"
    elif recent_high < prev_high and recent_low < prev_low:
        structure = "BEARISH"

    return {"bos": bos, "structure": structure}


def supply_demand_analysis(candles):
    demand_zones = []
    supply_zones = []

    for i in range(2, len(candles) - 1):
        curr = candles[i]
        prev = candles[i - 1]
        body_curr = abs(curr["close"] - curr["open"])
        body_prev = abs(prev["close"] - prev["open"])
        rng_curr  = curr["high"] - curr["low"]

        if rng_curr == 0:
            continue

        # Demand: strong bullish impulse after bearish base
        if (curr["close"] > curr["open"]
                and body_curr / rng_curr > 0.60
                and prev["close"] < prev["open"]):
            demand_zones.append({
                "high":  prev["high"],
                "low":   prev["low"],
                "mid":   (prev["high"] + prev["low"]) / 2,
                "index": i - 1
            })

        # Supply: strong bearish impulse after bullish base
        if (curr["close"] < curr["open"]
                and body_curr / rng_curr > 0.60
                and prev["close"] > prev["open"]):
            supply_zones.append({
                "high":  prev["high"],
                "low":   prev["low"],
                "mid":   (prev["high"] + prev["low"]) / 2,
                "index": i - 1
            })

    return {
        "demand_zones": demand_zones[-3:],
        "supply_zones": supply_zones[-3:]
    }


def liquidity_engine(candles):
    if len(candles) < 10:
        return {"buy_side": None, "sell_side": None}

    highs = [c["high"] for c in candles[-10:]]
    lows  = [c["low"]  for c in candles[-10:]]

    return {
        "buy_side":  max(highs),
        "sell_side": min(lows)
    }
