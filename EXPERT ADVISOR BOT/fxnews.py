# =========================================
# fxnews.py — Forex Factory News Fetcher
# =========================================

import requests
from datetime import datetime, timezone

URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"


def get_forex_factory_news():
    try:
        r = requests.get(URL, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            print(f"❌ FF status: {r.status_code}")
            return []

        data = r.json()
        news = []

        for item in data:
            try:
                date_str = item.get("date")
                if not date_str:
                    continue
                t = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                news.append({
                    "currency": item.get("currency", "").strip().upper(),
                    "impact":   item.get("impact",   "").strip().upper(),
                    "time":     t,
                    "actual":   item.get("actual",   ""),
                    "forecast": item.get("forecast", ""),
                    "previous": item.get("previous", ""),
                    "title":    item.get("title",    "")
                })
            except:
                continue

        print(f"✅ FF News loaded: {len(news)}")
        return news

    except requests.exceptions.Timeout:
        print("❌ FF timeout")
        return []
    except requests.exceptions.ConnectionError:
        print("❌ FF connection failed")
        return []
    except Exception as e:
        print("❌ FF ERROR:", e)
        return []
