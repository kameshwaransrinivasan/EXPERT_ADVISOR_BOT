# =========================================
# candles.py — Candlestick Pattern Engine
# =========================================

def candle_body(c):
    return abs(c["close"] - c["open"])

def candle_range(c):
    return abs(c["high"] - c["low"])

def upper_wick(c):
    return c["high"] - max(c["open"], c["close"])

def lower_wick(c):
    return min(c["open"], c["close"]) - c["low"]

def bullish_pinbar(c):
    body = candle_body(c)
    lw   = lower_wick(c)
    return lw > body * 2

def bearish_pinbar(c):
    body = candle_body(c)
    uw   = upper_wick(c)
    return uw > body * 2

def bullish_engulfing(prev, curr):
    return (curr["close"] > prev["open"]
            and candle_body(curr) > candle_body(prev)
            and curr["close"] > curr["open"])

def bearish_engulfing(prev, curr):
    return (curr["close"] < prev["open"]
            and candle_body(curr) > candle_body(prev)
            and curr["close"] < curr["open"])

def doji(c):
    total = candle_range(c)
    if total == 0:
        return False
    return candle_body(c) / total < 0.10

def inside_bar(prev, curr):
    return curr["high"] < prev["high"] and curr["low"] > prev["low"]

def marubozu(c):
    total = candle_range(c)
    if total == 0:
        return False
    return candle_body(c) / total > 0.90

def detect_pattern(candles):
    if len(candles) < 2:
        return "NONE"
    curr = candles[-1]
    prev = candles[-2]

    if bullish_pinbar(curr):
        return "BULLISH_PINBAR"
    if bearish_pinbar(curr):
        return "BEARISH_PINBAR"
    if bullish_engulfing(prev, curr):
        return "BULLISH_ENGULFING"
    if bearish_engulfing(prev, curr):
        return "BEARISH_ENGULFING"
    if doji(curr):
        return "DOJI"
    if inside_bar(prev, curr):
        return "INSIDE_BAR"
    if marubozu(curr):
        if curr["close"] > curr["open"]:
            return "MARUBOZU_BULLISH"
        return "MARUBOZU_BEARISH"
    return "NONE"
