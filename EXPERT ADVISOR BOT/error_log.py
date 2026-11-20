# =========================================
# error_log.py — Execution Error Tracking
#
# Previously every order/news-block failure just went to print() and
# was lost. This persists them to error_log.csv so the dashboard's
# Error Analysis section has real data instead of nothing.
# =========================================

import csv
import os
import datetime

FILE    = "error_log.csv"
HEADERS = ["time", "symbol", "mode", "category", "detail"]

# Order matters — first matching category wins.
_CATEGORY_KEYWORDS = [
    ("Invalid stops",    ["sl mandatory", "stop loss is mandatory", "invalid stop"]),
    ("Invalid comment",  ["comment"]),
    ("Spread too high",  ["spread too wide", "spread too high"]),
    ("Margin failure",   ["insufficient margin", "margin"]),
    ("Hedge blocked",    ["hedge"]),
    ("News-filter block",["news-filter block", "news block"]),
    ("Regime filter block", ["regime filter", "choppy range skip"]),
    ("Pattern filter block", ["pattern filter"]),
    ("Price/tick error", ["tick error", "symbol error", "must be below", "must be above", "account error"]),
    ("MT5 error",        ["mt5 error", "retcode", "order send failed"]),
]


def _categorize(detail: str) -> str:
    low = detail.lower()
    for category, keywords in _CATEGORY_KEYWORDS:
        if any(kw in low for kw in keywords):
            return category
    return "Other"


def init_error_log():
    if not os.path.exists(FILE):
        with open(FILE, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(HEADERS)


def log_error(symbol: str, mode: str, detail: str):
    """Call this from the failure branch of any order-placement result."""
    init_error_log()
    try:
        category = _categorize(str(detail))
        with open(FILE, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow([
                str(datetime.datetime.now()), str(symbol), str(mode),
                category, str(detail)[:200]
            ])
    except Exception as e:
        print(f"⚠️ error_log write failed: {e}")


def log_news_block(symbol: str, mode: str, reason: str = "news filter active"):
    log_error(symbol, mode, f"News-filter block: {reason}")
