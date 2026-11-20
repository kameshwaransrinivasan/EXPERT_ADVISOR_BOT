# =========================================
# news_smc.py — News Bias Analysis
# =========================================

from datetime import datetime

RED_EVENTS    = ["CPI","NFP","FOMC","INTEREST RATE","FED","ECB","GDP"]
ORANGE_EVENTS = ["PMI","RETAIL SALES","UNEMPLOYMENT","PPI"]


def is_red_news(news):
    title = news.get("title", "").upper()
    return any(e in title for e in RED_EVENTS)

def is_orange_news(news):
    title = news.get("title", "").upper()
    return any(e in title for e in ORANGE_EVENTS)

def compare_actual_forecast(news):
    try:
        actual   = float(news.get("actual",   0))
        forecast = float(news.get("forecast", 0))
        if actual > forecast: return "BULLISH"
        if actual < forecast: return "BEARISH"
    except:
        pass
    return "NEUTRAL"

def get_news_bias(news):
    return {
        "title":    news.get("title",    ""),
        "currency": news.get("currency", ""),
        "impact":   news.get("impact",   "LOW"),
        "actual":   news.get("actual",   ""),
        "forecast": news.get("forecast", ""),
        "previous": news.get("previous", ""),
        "bias":     compare_actual_forecast(news)
    }

def news_mode(news):
    if is_red_news(news):    return "RED"
    if is_orange_news(news): return "ORANGE"
    return "NORMAL"

def wait_after_news():
    return datetime.now().second >= 5

def analyze_news(news):
    return {
        "mode":              news_mode(news),
        "bias":              get_news_bias(news),
        "wait_confirmation": wait_after_news()
    }
