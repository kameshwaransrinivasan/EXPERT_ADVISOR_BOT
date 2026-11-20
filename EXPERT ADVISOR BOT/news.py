# =========================================
# news.py — News Filter
# =========================================

from datetime import datetime, timezone


def get_news_signal(symbol, news_list):
    if not news_list:
        return ("OK", "No news")

    now    = datetime.now(timezone.utc)
    symbol = symbol.replace(".i#", "").replace("#", "").upper()

    pair_map = {
        "EURUSD": ["EUR","USD"], "USDJPY": ["USD","JPY"],
        "GBPUSD": ["GBP","USD"], "EURJPY": ["EUR","JPY"],
        "GBPJPY": ["GBP","JPY"], "GOLD":   ["USD"],
        "XAUUSD": ["USD"]
    }
    currencies = []
    for pair, currs in pair_map.items():
        if pair in symbol:
            currencies = currs
            break

    for item in news_list:
        try:
            currency   = item["currency"].strip().upper()
            impact     = item["impact"].strip().upper()
            news_time  = item["time"]
            diff       = abs((news_time - now).total_seconds()) / 60

            if currency not in currencies:
                continue
            if impact == "HIGH" and diff <= 30:
                return ("BLOCK",   f"🔴 High news: {currency}")
            if impact == "MEDIUM" and diff <= 20:
                return ("CAUTION", f"🟠 Medium news: {currency}")
        except:
            continue
    return ("OK", "✅ Normal")


def is_red_news(symbol, news_cache):
    if not news_cache:
        return False
    symbol = symbol.replace(".i#", "").replace("#", "").upper()
    important = []
    if "GOLD" in symbol or "XAU" in symbol:
        important = ["USD", "XAU", "FOMC"]
    elif "EURUSD" in symbol: important = ["EUR", "USD"]
    elif "USDJPY" in symbol: important = ["USD", "JPY"]
    elif "GBPUSD" in symbol: important = ["GBP", "USD"]
    elif "EURJPY" in symbol: important = ["EUR", "JPY"]
    elif "GBPJPY" in symbol: important = ["GBP", "JPY"]

    now = datetime.now(timezone.utc)
    for item in news_cache:
        try:
            currency  = item["currency"].strip().upper()
            impact    = item["impact"].strip().upper()
            diff      = abs((item["time"] - now).total_seconds()) / 60
            if impact != "HIGH" or diff > 3:
                continue
            if currency in important:
                return True
        except:
            continue
    return False
