# =========================================
# zones.py — S&D Zones, FVG, GanBox
# FX Jers Mentorship 2.0
# =========================================

def candle_body(c):
    return abs(c["close"] - c["open"])

def candle_range(c):
    return c["high"] - c["low"]

def is_strong_candle(c, ratio=0.60):
    r = candle_range(c)
    return False if r == 0 else candle_body(c) / r >= ratio

def is_bullish(c):
    return c["close"] > c["open"]

def is_bearish(c):
    return c["close"] < c["open"]


# ---- FVG ----
def detect_fvg(candles):
    if len(candles) < 3:
        return None
    c1 = candles[-3]
    c3 = candles[-1]
    if c3["low"] > c1["high"]:
        return {"type": "BULLISH_FVG", "high": c3["low"],
                "low": c1["high"], "mid": (c3["low"] + c1["high"]) / 2}
    if c3["high"] < c1["low"]:
        return {"type": "BEARISH_FVG", "high": c1["low"],
                "low": c3["high"], "mid": (c1["low"] + c3["high"]) / 2}
    return None


# ---- DEMAND ZONES ----
def detect_demand_zones(candles):
    zones = []
    for i in range(2, len(candles) - 1):
        curr = candles[i]
        prev = candles[i - 1]
        if is_bullish(curr) and is_strong_candle(curr):
            if is_bearish(prev) or candle_body(prev) < candle_body(curr) * 0.4:
                zones.append({
                    "type": "DEMAND", "high": prev["high"],
                    "low": prev["low"], "mid": (prev["high"] + prev["low"]) / 2,
                    "index": i - 1, "touched": False, "fresh": True
                })
    return zones


# ---- SUPPLY ZONES ----
def detect_supply_zones(candles):
    zones = []
    for i in range(2, len(candles) - 1):
        curr = candles[i]
        prev = candles[i - 1]
        if is_bearish(curr) and is_strong_candle(curr):
            if is_bullish(prev) or candle_body(prev) < candle_body(curr) * 0.4:
                zones.append({
                    "type": "SUPPLY", "high": prev["high"],
                    "low": prev["low"], "mid": (prev["high"] + prev["low"]) / 2,
                    "index": i - 1, "touched": False, "fresh": True
                })
    return zones


# ---- FRESHNESS ----
def mark_touched_zones(candles, zones):
    for zone in zones:
        for c in candles[zone["index"] + 1:]:
            if zone["type"] == "DEMAND":
                if c["low"] <= zone["high"] and c["low"] >= zone["low"]:
                    zone["touched"] = True
                    zone["fresh"]   = False
                    break
            else:
                if c["high"] >= zone["low"] and c["high"] <= zone["high"]:
                    zone["touched"] = True
                    zone["fresh"]   = False
                    break
    return zones


def price_in_zone(price, zone):
    return zone["low"] <= price <= zone["high"]


def get_active_zone(candles):
    price    = candles[-1]["close"]
    demands  = mark_touched_zones(candles, detect_demand_zones(candles))
    supplies = mark_touched_zones(candles, detect_supply_zones(candles))
    for zone in reversed(demands):
        if zone["fresh"] and price_in_zone(price, zone):
            return zone
    for zone in reversed(supplies):
        if zone["fresh"] and price_in_zone(price, zone):
            return zone
    return None


# ---- ZONE + FVG CONFLUENCE ----
def zone_fvg_confluence(zone, fvg):
    """
    True only if a detected FVG overlaps the given S/D zone AND
    matches its directional bias.
      DEMAND zone requires an overlapping BULLISH_FVG.
      SUPPLY zone requires an overlapping BEARISH_FVG.
    This is the "zone AND fvg" confluence check — a zone alone or an
    FVG alone is not enough, they must line up on the same trade idea.
    """
    if zone is None or fvg is None:
        return False
    if zone["type"] == "DEMAND" and fvg["type"] != "BULLISH_FVG":
        return False
    if zone["type"] == "SUPPLY" and fvg["type"] != "BEARISH_FVG":
        return False
    return not (fvg["high"] < zone["low"] or fvg["low"] > zone["high"])


# ---- GANBOX ----
def ganbox_levels(zone):
    h, l   = zone["high"], zone["low"]
    rng    = h - l
    is_sup = zone["type"] == "SUPPLY"
    return {
        "0pct":   h if is_sup else l,
        "25pct":  h - rng * 0.25 if is_sup else l + rng * 0.25,
        "50pct":  (h + l) / 2,
        "75pct":  h - rng * 0.75 if is_sup else l + rng * 0.75,
        "100pct": l if is_sup else h,
    }

def price_at_ganbox_midline(price, zone, tol=0.10):
    levels  = ganbox_levels(zone)
    mid     = levels["50pct"]
    tol_val = (zone["high"] - zone["low"]) * tol
    return abs(price - mid) <= tol_val


# ---- REJECTION ----
def zone_has_rejection(candles, zone):
    c   = candles[-1]
    rng = candle_range(c)
    if rng == 0:
        return False
    if zone["type"] == "DEMAND":
        return (min(c["open"], c["close"]) - c["low"]) / rng > 0.40
    return (c["high"] - max(c["open"], c["close"])) / rng > 0.40


# ---- TRENDLINE BREAK ----
def detect_trendline_break(candles):
    if len(candles) < 10:
        return "NONE"
    lows    = [c["low"]  for c in candles]
    highs   = [c["high"] for c in candles]
    current = candles[-1]
    if lows[-1] > lows[-10]:
        if current["close"] < lows[-1]:
            return "BULL_TRENDLINE_BREAK"
    if highs[-1] < highs[-10]:
        if current["close"] > highs[-1]:
            return "BEAR_TRENDLINE_BREAK"
    return "NONE"


# ---- ZONE REPORT ----
def zone_report(candles):
    demands  = mark_touched_zones(candles, detect_demand_zones(candles))
    supplies = mark_touched_zones(candles, detect_supply_zones(candles))
    return {
        "demand_zones":    [z for z in demands  if z["fresh"]],
        "supply_zones":    [z for z in supplies if z["fresh"]],
        "fvg":             detect_fvg(candles),
        "trendline_break": detect_trendline_break(candles),
        "active_zone":     get_active_zone(candles),
    }
