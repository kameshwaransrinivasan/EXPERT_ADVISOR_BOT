# =========================================
# market_structure.py — HH/HL/LH/LL Engine
# =========================================

def get_swing_highs(candles, lookback=3):
    highs = []
    for i in range(lookback, len(candles) - lookback):
        window = candles[i - lookback: i + lookback + 1]
        peak   = candles[i]["high"]
        if all(peak >= c["high"] for c in window):
            highs.append({"index": i, "price": peak})
    return highs

def get_swing_lows(candles, lookback=3):
    lows = []
    for i in range(lookback, len(candles) - lookback):
        window = candles[i - lookback: i + lookback + 1]
        trough = candles[i]["low"]
        if all(trough <= c["low"] for c in window):
            lows.append({"index": i, "price": trough})
    return lows

def get_market_structure(candles):
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

def detect_bos(candles):
    highs = get_swing_highs(candles)
    lows  = get_swing_lows(candles)
    if not highs or not lows:
        return "NONE"
    current         = candles[-1]["close"]
    last_swing_high = highs[-1]["price"]
    last_swing_low  = lows[-1]["price"]
    if current > last_swing_high:
        return "BULLISH_BOS"
    if current < last_swing_low:
        return "BEARISH_BOS"
    return "NONE"

def detect_choch(candles):
    structure = get_market_structure(candles)
    highs     = get_swing_highs(candles)
    lows      = get_swing_lows(candles)
    if not highs or not lows:
        return "NONE"
    current         = candles[-1]["close"]
    last_swing_high = highs[-1]["price"]
    last_swing_low  = lows[-1]["price"]
    if structure == "BULLISH" and current < last_swing_low:
        return "BEARISH_CHOCH"
    if structure == "BEARISH" and current > last_swing_high:
        return "BULLISH_CHOCH"
    return "NONE"

def equal_highs(candles, threshold_pct=0.001):
    highs = get_swing_highs(candles)
    if len(highs) < 2:
        return False
    h1, h2 = highs[-2]["price"], highs[-1]["price"]
    return abs(h1 - h2) / h1 < threshold_pct

def equal_lows(candles, threshold_pct=0.001):
    lows = get_swing_lows(candles)
    if len(lows) < 2:
        return False
    l1, l2 = lows[-2]["price"], lows[-1]["price"]
    return abs(l1 - l2) / l1 < threshold_pct

def detect_liquidity_sweep(candles):
    current = candles[-1]
    highs   = get_swing_highs(candles)
    lows    = get_swing_lows(candles)
    if equal_highs(candles) and highs:
        last_high = highs[-1]["price"]
        if current["high"] > last_high and current["close"] < last_high:
            return "BUY_SIDE_SWEEP"
    if equal_lows(candles) and lows:
        last_low = lows[-1]["price"]
        if current["low"] < last_low and current["close"] > last_low:
            return "SELL_SIDE_SWEEP"
    return "NONE"

def market_structure_report(candles):
    return {
        "structure": get_market_structure(candles),
        "bos":       detect_bos(candles),
        "choch":     detect_choch(candles),
        "sweep":     detect_liquidity_sweep(candles),
        "eq_highs":  equal_highs(candles),
        "eq_lows":   equal_lows(candles),
    }
