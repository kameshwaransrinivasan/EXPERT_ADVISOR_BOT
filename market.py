# =========================================
# market.py — Session Tracker & Holiday Engine
# EA BOT v2 | SRD Section 4
#
# Sessions (UTC):
#   Asian   00:00–09:00
#   London  08:00–17:00
#   NY      13:00–22:00
#   Overlap 13:00–17:00 (peak)
#
# Weekend:
#   Scalping/Intraday → force-close Fri 21:00 UTC
#   Swing             → weekend hold permitted
#   Resume            → Sun 22:00 UTC
# =========================================

from datetime import datetime, timezone

# Hard-coded major bank holidays (MM-DD)
FIXED_HOLIDAYS = [
    "01-01",  # New Year's Day
    "12-25",  # Christmas
    "07-04",  # US Independence Day
]


# ---- Current UTC time helper ----
def _now_utc():
    return datetime.now(timezone.utc)


# ---- Is market open at all? ----
def is_market_open() -> bool:
    """
    Returns False on full weekends and before Sunday 22:00 UTC resumption.
    Returns False on fixed holidays.
    """
    now = _now_utc()
    wd  = now.weekday()   # 0=Mon … 6=Sun
    h   = now.hour
    m   = now.minute

    # Saturday: close after 21:00 Friday already handled → all day closed
    if wd == 5:
        return False
    # Sunday: open only after 22:00 UTC
    if wd == 6:
        return h >= 22
    # Monday: normal (Sunday 22:00+ resumed)
    # Friday: close at 21:00 UTC
    if wd == 4 and (h > 21 or (h == 21 and m >= 0)):
        return False
    # Holiday check
    if is_holiday():
        return False
    return True


# ---- Weekend force-close trigger ----
def should_force_close_now() -> bool:
    """
    Returns True on Friday at or after 21:00 UTC.
    Used by Scalping and Intraday pipelines to flush all positions.
    Swing positions ignore this.
    """
    now = _now_utc()
    return now.weekday() == 4 and now.hour >= 21


# ---- Session detection (UTC) ----
def get_current_session() -> str:
    h = _now_utc().hour
    if  0 <= h <  8:  return "ASIAN"
    if  8 <= h < 13:  return "LONDON"
    if 13 <= h < 17:  return "OVERLAP"   # London/NY overlap — peak
    if 17 <= h < 22:  return "NEWYORK"
    return "OFF"


# ---- Session strength ----
def session_strength() -> str:
    s = get_current_session()
    if s == "OVERLAP":               return "PEAK"
    if s in ["LONDON", "NEWYORK"]:   return "STRONG"
    if s == "ASIAN":                 return "MEDIUM"
    return "WEAK"


# ---- Holiday check ----
def is_holiday() -> bool:
    return _now_utc().strftime("%m-%d") in FIXED_HOLIDAYS


# ---- Full status dict ----
def market_status() -> dict:
    return {
        "market_open":    is_market_open(),
        "holiday":        is_holiday(),
        "session":        get_current_session(),
        "strength":       session_strength(),
        "force_close":    should_force_close_now(),
        "utc_time":       _now_utc().strftime("%Y-%m-%d %H:%M:%S UTC"),
    }
