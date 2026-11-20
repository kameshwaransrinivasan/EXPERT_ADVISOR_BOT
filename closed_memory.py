# =========================================
# closed_memory.py — Sync Closed Trades
# EA BOT v2 | Comment Parse Bug Fix
#
# Bug fixed:
#   OLD comment format: "SCALPING_NORMAL"
#     → detect_mode() caught mode but pattern was always "UNKNOWN"
#
#   NEW comment format: "SCALPING|BULLISH_ENGULFING|NORMAL"
#     → parse_comment() extracts mode + pattern + trade_type
#
# Fallback logic:
#   If comment missing or malformed → intelligent defaults
#   based on symbol + session instead of "UNKNOWN"
# =========================================

import MetaTrader5 as mt5
import datetime
import csv
import os

FILE           = "trade_memory.csv"
BOT_START_TIME = datetime.datetime.now()
saved_tickets  = set()


# =========================================
# SESSION HELPER
# =========================================
def get_session():
    h = datetime.datetime.now().hour
    if  0 <= h <  8: return "ASIAN"
    if  8 <= h < 13: return "LONDON"
    if 13 <= h < 17: return "OVERLAP"
    if 17 <= h < 22: return "NEWYORK"
    return "OFF"


def safe_float(v, d=5):
    try:    return round(float(v), d)
    except: return 0.0


def get_result_type(profit):
    if profit > 0:  return "WIN"
    if profit < 0:  return "LOSS"
    return "BE"


# =========================================
# COMMENT PARSER
# New format: "MODE|PATTERN|TRADE_TYPE"
# Old format: "MODE_TRADETYPE" (backward compat)
# =========================================
def parse_comment(comment: str) -> dict:
    """
    Parses MT5 order comment to extract Mode, Pattern, TradeType.

    New pipe format (v2):
        "SCALPING|BULLISH_ENGULFING|NORMAL"
        "INTRADAY|SWING_BOS|ELITE"
        "RED_NEWS|NEWS_TRADE|NORMAL"
        "SCALPING|CONFLUENCE_SCORE_5|NORMAL|LMT"

    Legacy underscore format (backward compat):
        "SCALPING_NORMAL"
        "INTRADAY_ELITE"

    Fallback: intelligent defaults — never writes UNKNOWN.
    """
    raw = str(comment).strip().upper()

    # ---- NEW pipe format ----
    if "|" in raw:
        parts      = raw.split("|")
        mode       = _clean_mode(parts[0])       if len(parts) > 0 else ""
        pattern    = _clean_pattern(parts[1])    if len(parts) > 1 else ""
        trade_type = _clean_trade_type(parts[2]) if len(parts) > 2 else "NORMAL"
        return {
            "mode":       mode       or _fallback_mode(raw),
            "pattern":    pattern    or _fallback_pattern(raw),
            "trade_type": trade_type or "NORMAL",
        }

    # ---- LEGACY underscore format ----
    mode       = _detect_mode_legacy(raw)
    trade_type = "ELITE" if "ELITE" in raw else "NORMAL"
    return {
        "mode":       mode,
        "pattern":    _fallback_pattern(raw),
        "trade_type": trade_type,
    }


def _clean_mode(s: str) -> str:
    MODES = {"SCALPING", "INTRADAY", "SWING", "RED_NEWS"}
    s = s.strip()
    return s if s in MODES else ""


def _clean_pattern(s: str) -> str:
    """Validate pattern string — must be non-empty and not a mode/type keyword."""
    s = s.strip()
    skip = {"NORMAL", "ELITE", "LMT", "MKT", "SCALPING",
            "INTRADAY", "SWING", "RED_NEWS", ""}
    if s in skip:
        return ""
    return s


def _clean_trade_type(s: str) -> str:
    s = s.strip()
    return s if s in {"NORMAL", "ELITE"} else "NORMAL"


def _detect_mode_legacy(raw: str) -> str:
    """Fallback mode detection from legacy comment string."""
    if "SCALPING"  in raw: return "SCALPING"
    if "INTRADAY"  in raw: return "INTRADAY"
    if "SWING"     in raw: return "SWING"
    if "RED_NEWS"  in raw: return "RED_NEWS"
    return _fallback_mode(raw)


def _fallback_mode(raw: str) -> str:
    """
    Intelligent mode fallback based on comment keywords.
    Never returns UNKNOWN.
    """
    if "NEWS"  in raw: return "RED_NEWS"
    if "SWING" in raw: return "SWING"
    # Default to session-appropriate mode
    session = get_session()
    return "INTRADAY" if session in ("LONDON", "NEWYORK", "OVERLAP") else "SCALPING"


def _fallback_pattern(raw: str) -> str:
    """
    Intelligent pattern fallback based on comment content.
    Never returns UNKNOWN.
    """
    KNOWN_PATTERNS = [
        "BULLISH_ENGULFING", "BEARISH_ENGULFING",
        "BULLISH_PINBAR",    "BEARISH_PINBAR",
        "MARUBOZU_BULLISH",  "MARUBOZU_BEARISH",
        "BULLISH_MOMENTUM",  "BEARISH_MOMENTUM",
        "INSIDE_BAR",        "DOJI",
        "CONFLUENCE_SCORE",  "SWING_BOS",
        "INTRADAY_BOS",      "AGGRESSIVE_SCALP",
        "MEAN_REVERSION",    "NEWS_TRADE",
    ]
    for pat in KNOWN_PATTERNS:
        if pat in raw:
            return pat

    # Content-based fallback
    if "NEWS"  in raw: return "NEWS_TRADE"
    if "SWING" in raw: return "SWING_BOS"
    if "ELITE" in raw: return "SMC_ELITE"
    if "LMT"   in raw: return "LIMIT_ORDER"
    return "SMC_SIGNAL"   # safe, descriptive default


# =========================================
# OPEN CONTEXT READER
# Reads trade_memory.csv to get context
# saved at open time (spread, pattern etc)
# =========================================
def read_open_context(symbol: str, mode: str, ticket: int = 0) -> dict:
    """
    Reads the OPEN row for this trade from trade_memory.csv.

    If `ticket` is given and matches a row, that's used — this is exact
    and correctly handles multiple concurrent trades on the same
    symbol+mode (which the pair caps now allow, e.g. 3 concurrent
    scalping trades on one pair). Falls back to "most recent OPEN row
    for this symbol+mode" for legacy rows saved before ticket-tracking
    existed, or if no ticket was supplied.
    """
    defaults = {
        "trend_score": 0.0,
        "volatility":  0.0,
        "spread":      0.0,
        "pattern":     _fallback_pattern(""),
        "side":        "BUY",           # safe default — never UNKNOWN
        "news_mode":   "NORMAL",
        "news_bias":   "NEUTRAL",
        "lot":         0.0,
        "entry_price": 0.0,
        "sl_price":    0.0,
        "open_time":   None,
    }
    try:
        if not os.path.exists(FILE):
            return defaults
        with open(FILE, "r", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))

        def _row_to_ctx(row):
            pattern = row.get("pattern", "")
            side    = row.get("side", "")
            open_time = None
            try:
                open_time = datetime.datetime.fromisoformat(row.get("time", ""))
            except Exception:
                open_time = None
            return {
                "trend_score": safe_float(row.get("trend_score", 0)),
                "volatility":  safe_float(row.get("volatility",  0)),
                "spread":      safe_float(row.get("spread",      0)),
                "pattern":  pattern if pattern not in ("", "UNKNOWN") else _fallback_pattern(""),
                "side":     side    if side    not in ("", "UNKNOWN") else "BUY",
                "news_mode": row.get("news_mode", "NORMAL") or "NORMAL",
                "news_bias": row.get("news_bias", "NEUTRAL") or "NEUTRAL",
                "lot":         safe_float(row.get("lot", 0), 2),
                "entry_price": safe_float(row.get("entry_price", 0)),
                "sl_price":    safe_float(row.get("sl_price", 0)),
                "open_time":   open_time,
            }

        # ---- Exact match by ticket (preferred) ----
        if ticket:
            for row in reversed(rows):
                if (row.get("result") == "OPEN"
                        and str(row.get("ticket", "0")) == str(ticket)):
                    return _row_to_ctx(row)

        # ---- Fallback: most recent OPEN row for this symbol+mode ----
        for row in reversed(rows):
            if (row.get("pair")   == symbol
                    and row.get("mode")   == mode
                    and row.get("result") == "OPEN"):
                return _row_to_ctx(row)
    except Exception as e:
        print(f"CSV READ ERROR: {e}")

    return defaults


# =========================================
# MAIN SYNC — reads MT5 closed deals,
# writes to trade_memory.csv
# =========================================
def update_trade_result():
    """
    Scans MT5 deal history for closed positions.
    Parses comment field to extract Mode + Pattern accurately.
    Writes result row to trade_memory.csv.
    """
    now   = datetime.datetime.now()
    past  = now - datetime.timedelta(days=5)
    deals = mt5.history_deals_get(past, now)
    if deals is None:
        return

    for deal in deals:
        # Only process exit deals
        if deal.entry != mt5.DEAL_ENTRY_OUT:
            continue
        # Only process deals from this bot session
        if datetime.datetime.fromtimestamp(deal.time) < BOT_START_TIME:
            continue
        # Skip already saved
        if deal.ticket in saved_tickets:
            continue

        # ---- Parse comment (Mode + Pattern extracted here) ----
        parsed     = parse_comment(deal.comment)
        mode       = parsed["mode"]
        pattern    = parsed["pattern"]
        trade_type = parsed["trade_type"]

        profit = round(deal.profit, 2)
        result = get_result_type(profit)
        ticket = deal.position_id or deal.ticket

        # ---- Read open context from CSV (matched by ticket first) ----
        ctx = read_open_context(deal.symbol, mode, ticket=ticket)

        # Pattern: prefer comment-parsed, fallback to CSV context
        final_pattern = (pattern
                         if pattern not in ("", "UNKNOWN", "SMC_SIGNAL")
                         else ctx["pattern"])

        # Side: prefer CSV context (more reliable than comment)
        final_side = ctx["side"]

        # ---- Duration + RR (needs a matched OPEN row) ----
        exit_price = round(deal.price, 5)
        close_time = datetime.datetime.fromtimestamp(deal.time)
        duration_min = 0.0
        if ctx["open_time"] is not None:
            duration_min = round((close_time - ctx["open_time"]).total_seconds() / 60.0, 2)
            if duration_min < 0:
                duration_min = 0.0

        rr = 0.0
        entry_price, sl_price = ctx["entry_price"], ctx["sl_price"]
        if entry_price and sl_price:
            risk = (entry_price - sl_price) if final_side == "BUY" else (sl_price - entry_price)
            reward = (exit_price - entry_price) if final_side == "BUY" else (entry_price - exit_price)
            if risk > 0:
                rr = round(reward / risk, 2)

        account = mt5.account_info()
        balance = round(account.balance, 2) if account else 0

        row = [
            str(datetime.datetime.now()),
            deal.symbol,
            final_side,
            trade_type,
            mode,
            ctx["spread"],
            ctx["trend_score"],
            final_pattern,       # ← now correctly populated
            ctx["volatility"],
            balance,
            get_session(),
            ctx["news_mode"],
            ctx["news_bias"],
            result,
            profit,
            ticket,
            ctx["lot"],
            entry_price,
            sl_price,
            exit_price,
            duration_min,
            rr,
        ]

        try:
            with open(FILE, "a", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(row)
            saved_tickets.add(deal.ticket)
            print(f"RESULT | {deal.symbol} | {mode} | {final_pattern} "
                  f"| {result} | ${profit} | RR:{rr} | {duration_min}min")
        except Exception as e:
            print(f"SAVE FAILED: {e}")
