# =========================================
# news_bias.py
# EA BOT v2 | SRD Section 5
#
# Institutional News Reaction Engine
#
# Flow:
#   T-1 min  → Pre-news scan: currency, pairs, indicator
#   Release  → Actual vs Forecast divergence math
#   Entry    → Price must reach 30M zone + LTF confirm
#   Failsafe → If direction mismatches zone → BLOCK
# =========================================

from datetime import datetime, timezone


# =========================================
# PAIR → CURRENCY MAP
# =========================================
PAIR_CURRENCIES = {
    "EURUSD": {"base": "EUR", "quote": "USD"},
    "GBPUSD": {"base": "GBP", "quote": "USD"},
    "USDJPY": {"base": "USD", "quote": "JPY"},
    "USDCHF": {"base": "USD", "quote": "CHF"},
    "AUDUSD": {"base": "AUD", "quote": "USD"},
    "NZDUSD": {"base": "NZD", "quote": "USD"},
    "USDCAD": {"base": "USD", "quote": "CAD"},
    "EURGBP": {"base": "EUR", "quote": "GBP"},
    "EURJPY": {"base": "EUR", "quote": "JPY"},
    "GBPJPY": {"base": "GBP", "quote": "JPY"},
    "XAUUSD": {"base": "XAU", "quote": "USD"},
    "GOLD":   {"base": "XAU", "quote": "USD"},
    "XAGUSD": {"base": "XAG", "quote": "USD"},
}


def get_pair_currencies(symbol: str) -> dict:
    clean = symbol.replace(".i#", "").replace("#", "").upper()
    for pair, currs in PAIR_CURRENCIES.items():
        if pair in clean:
            return currs
    return {"base": "", "quote": ""}


def get_pairs_for_currency(currency: str) -> list:
    """Returns all tracked pairs that involve this currency."""
    result = []
    for pair, currs in PAIR_CURRENCIES.items():
        if currency in (currs["base"], currs["quote"]):
            result.append(pair)
    return result


# =========================================
# STEP 1 — PARSE NEWS VALUES
# =========================================
def parse_news_value(value_str) -> float | None:
    if not value_str or str(value_str).strip() == "":
        return None
    s = str(value_str).strip().upper().replace(",", "")
    mult = 1
    if s.endswith("K"):   mult = 1_000;          s = s[:-1]
    elif s.endswith("M"): mult = 1_000_000;       s = s[:-1]
    elif s.endswith("B"): mult = 1_000_000_000;   s = s[:-1]
    s = s.replace("%", "")
    try:
        return float(s) * mult
    except:
        return None


# =========================================
# STEP 2 — CURRENCY BIAS
# Actual > Forecast → BULLISH (currency stronger)
# Actual < Forecast → BEARISH (currency weaker)
# =========================================
NEGATIVE_INDICATORS = [
    "UNEMPLOYMENT", "JOBLESS", "CLAIMS",
    "CPI DEFLATION", "DEFICIT",
]


def get_currency_bias(news_item: dict) -> str:
    actual   = parse_news_value(news_item.get("actual",   ""))
    forecast = parse_news_value(news_item.get("forecast", ""))
    impact   = str(news_item.get("impact", "")).upper()
    title    = str(news_item.get("title",  "")).upper()

    if actual is None or forecast is None:
        return "NEUTRAL"

    diff     = actual - forecast
    diff_pct = abs(diff / forecast * 100) if forecast != 0 else 0

    # Minimum deviation thresholds
    min_diff = {"HIGH": 0.5, "MEDIUM": 1.0, "LOW": 2.0}.get(impact, 1.0)
    if diff_pct < min_diff:
        return "NEUTRAL"

    is_negative = any(n in title for n in NEGATIVE_INDICATORS)
    if is_negative:
        return "BEARISH" if actual > forecast else "BULLISH"
    else:
        return "BULLISH" if actual > forecast else "BEARISH"


# =========================================
# STEP 3 — PAIR TRADE DIRECTION
# =========================================
def get_pair_direction(symbol: str, news_currency: str, currency_bias: str) -> str:
    currs = get_pair_currencies(symbol)
    base  = currs["base"]
    quote = currs["quote"]
    if not base or not quote:
        return "NONE"

    if news_currency == base:
        return "BUY"  if currency_bias == "BULLISH" else (
               "SELL" if currency_bias == "BEARISH" else "NONE")
    elif news_currency == quote:
        return "SELL" if currency_bias == "BULLISH" else (
               "BUY"  if currency_bias == "BEARISH" else "NONE")
    return "NONE"


# =========================================
# STEP 4 — TIMING
# =========================================
def seconds_since_news(news_time) -> float:
    return (datetime.now(timezone.utc) - news_time).total_seconds()


def is_in_no_trade_window(news_time, secs=120) -> bool:
    e = seconds_since_news(news_time)
    return 0 <= e <= secs


def is_in_volatile_window(news_time, end_secs=300) -> bool:
    e = seconds_since_news(news_time)
    return 120 <= e <= end_secs


def required_confirmations(news_time) -> int:
    e = seconds_since_news(news_time)
    if e < 120:  return 0
    if e < 300:  return 2
    return 1


# =========================================
# PRE-NEWS SCAN (T-1 minute)
# SRD 5.1: scan exactly 1 minute before release
# =========================================
def pre_news_scan(news_cache: list, symbols: list) -> list:
    """
    Returns list of upcoming HIGH impact events releasing
    within the next 60 seconds.
    Each item includes: currency, title, pairs_affected, time_to_release.
    """
    now    = datetime.now(timezone.utc)
    alerts = []

    for item in news_cache:
        try:
            impact    = str(item.get("impact", "")).upper()
            news_time = item.get("time")
            currency  = str(item.get("currency", "")).strip().upper()
            title     = str(item.get("title", ""))

            if impact not in ("HIGH", "MEDIUM"):
                continue

            secs_to_release = (news_time - now).total_seconds()
            if not (0 <= secs_to_release <= 60):
                continue

            pairs = [s for s in symbols
                     if currency in (get_pair_currencies(s).get("base", ""),
                                     get_pair_currencies(s).get("quote", ""))]
            if not pairs:
                continue

            alerts.append({
                "currency":         currency,
                "title":            title,
                "impact":           impact,
                "time":             news_time,
                "secs_to_release":  round(secs_to_release, 1),
                "pairs_affected":   pairs,
            })
        except:
            continue

    return alerts


# =========================================
# SCALPING PRE-NEWS: force-close 3 min before,
# freeze entry 30 min after (SRD 5.1 scalping rule)
# =========================================
def scalp_news_block(symbol: str, news_cache: list) -> str:
    """
    Returns:
      "FORCE_CLOSE"  — within 3 min before HIGH impact news
      "FREEZE"       — within 30 min after HIGH impact news
      "OK"           — safe to trade
    """
    now  = datetime.now(timezone.utc)
    currs = get_pair_currencies(symbol)
    relevant = [currs["base"], currs["quote"]]

    for item in news_cache:
        try:
            impact    = str(item.get("impact", "")).upper()
            currency  = str(item.get("currency", "")).strip().upper()
            news_time = item.get("time")

            if impact != "HIGH" or currency not in relevant:
                continue

            secs = (news_time - now).total_seconds()
            elapsed = -secs

            # 3 min before → force-close all scalp positions
            if -180 <= secs <= 0:
                return "FORCE_CLOSE"

            # 30 min after → freeze new entries
            if 0 < elapsed <= 1800:
                return "FREEZE"
        except:
            continue
    return "OK"


# =========================================
# SRD 5: DIRECTION-ZONE ALIGNMENT FAILSAFE
# "If data indicates BUY but price hits supply
#  zone first → INVALIDATE the setup."
# =========================================
def direction_zone_failsafe(direction: str, zone_type: str) -> bool:
    """
    Returns True (OK to proceed) only if:
      BUY  direction + DEMAND zone
      SELL direction + SUPPLY zone

    Returns False (INVALIDATE) if mismatch:
      BUY  direction + SUPPLY zone → chasing market upward → block
      SELL direction + DEMAND zone → chasing market downward → block
    """
    if direction == "BUY"  and zone_type == "SUPPLY": return False
    if direction == "SELL" and zone_type == "DEMAND": return False
    return True


# =========================================
# FULL BIAS REPORT
# =========================================
def get_news_bias_report(news_item: dict, symbol: str) -> dict:
    currency  = str(news_item.get("currency", "")).strip().upper()
    impact    = str(news_item.get("impact",   "")).strip().upper()
    news_time = news_item.get("time")
    title     = str(news_item.get("title",    ""))

    bias      = get_currency_bias(news_item)
    direction = get_pair_direction(symbol, currency, bias)
    elapsed   = seconds_since_news(news_time) if news_time else -1
    req_conf  = required_confirmations(news_time) if news_time else 0
    no_trade  = is_in_no_trade_window(news_time) if news_time else True

    return {
        "currency":        currency,
        "impact":          impact,
        "title":           title,
        "actual":          news_item.get("actual",   ""),
        "forecast":        news_item.get("forecast", ""),
        "previous":        news_item.get("previous", ""),
        "bias":            bias,
        "direction":       direction,
        "elapsed_seconds": round(elapsed, 1),
        "no_trade_window": no_trade,
        "required_confs":  req_conf,
        "tradeable": (
            direction != "NONE"
            and bias != "NEUTRAL"
            and not no_trade
            and impact in ["HIGH", "MEDIUM"]
        ),
    }


def get_relevant_news(symbol: str, news_cache: list, max_age_mins=30) -> list:
    currs = get_pair_currencies(symbol)
    base  = currs["base"]
    quote = currs["quote"]
    now   = datetime.now(timezone.utc)
    out   = []

    for item in news_cache:
        try:
            currency  = str(item.get("currency", "")).strip().upper()
            impact    = str(item.get("impact",   "")).strip().upper()
            news_time = item.get("time")
            if currency not in [base, quote]:          continue
            if impact not in ["HIGH", "MEDIUM"]:       continue
            elapsed_m = (now - news_time).total_seconds() / 60
            if elapsed_m < 0 or elapsed_m > max_age_mins: continue
            out.append(get_news_bias_report(item, symbol))
        except:
            continue

    out.sort(key=lambda x: x["elapsed_seconds"])
    return out


def get_best_bias(symbol: str, news_cache: list) -> dict | None:
    relevant = get_relevant_news(symbol, news_cache)
    for r in relevant:
        if r["tradeable"] and r["impact"] == "HIGH":
            return r
    for r in relevant:
        if r["tradeable"] and r["impact"] == "MEDIUM":
            return r
    return None
