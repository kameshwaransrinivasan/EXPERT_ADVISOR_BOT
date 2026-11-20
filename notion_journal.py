# =========================================
# notion_journal.py — Auto Trade Journal
# Logs every trade to Notion Database
# =========================================

import os
import requests
import json
from datetime import datetime

# ---- CONFIG ----
# Set these as environment variables before starting the bot:
#   NOTION_TOKEN          -> your Notion integration token
#   NOTION_DATABASE_ID    -> the target database's ID
# (Previously these were hardcoded placeholder strings, which is why every
# Notion call was failing 100% of the time — it was authenticating as the
# literal text "your_notion_integration_token".)
NOTION_TOKEN = os.getenv("NOTION_TOKEN", "")
DATABASE_ID  = os.getenv("NOTION_DATABASE_ID", "")

HEADERS = {
    "Authorization":  f"Bearer {NOTION_TOKEN}",
    "Content-Type":   "application/json",
    "Notion-Version": "2022-06-28"
}
URL = "https://api.notion.com/v1/pages"


def _configured() -> bool:
    return bool(NOTION_TOKEN) and bool(DATABASE_ID)


if not _configured():
    print("⚠️⚠️⚠️ NOTION NOT CONFIGURED — set NOTION_TOKEN and "
          "NOTION_DATABASE_ID env vars. All Notion logging is DISABLED "
          "until this is set (won't spam this warning per-trade). ⚠️⚠️⚠️")


def _title(text):
    return {"title": [{"text": {"content": str(text)}}]}

def _text(text):
    return {"rich_text": [{"text": {"content": str(text)}}]}

def _number(value):
    try:    return {"number": round(float(value), 5)}
    except: return {"number": 0}

def _select(option):
    return {"select": {"name": str(option)}}

def _date(dt=None):
    if dt is None: dt = datetime.now()
    return {"date": {"start": dt.strftime("%Y-%m-%dT%H:%M:%S")}}


def log_trade_open(symbol, side, mode, entry, stop_loss, take_profit,
                   rr, zone_type="UNKNOWN", bos="NONE", sweep="NONE",
                   fvg=None, trend_score=0, pattern="UNKNOWN",
                   session="UNKNOWN", lot=0.01, balance=0):
    if not _configured():
        return None
    try:
        fvg_label = fvg["type"] if isinstance(fvg, dict) else str(fvg or "NONE")
        payload   = {
            "parent": {"database_id": DATABASE_ID},
            "properties": {
                "Trade":       _title(f"{symbol} {side} — {mode}"),
                "Symbol":      _text(symbol),
                "Side":        _select(side),
                "Mode":        _select(mode),
                "Entry Price": _number(entry),
                "Stop Loss":   _number(stop_loss),
                "Take Profit": _number(take_profit),
                "RR Ratio":    _number(rr),
                "Zone Type":   _select(zone_type),
                "BOS":         _text(bos),
                "Sweep":       _text(sweep),
                "FVG":         _text(fvg_label),
                "Pattern":     _text(pattern),
                "Trend Score": _number(trend_score),
                "Session":     _select(session),
                "Lot Size":    _number(lot),
                "Balance":     _number(balance),
                "Status":      _select("OPEN"),
                "Result":      _select("PENDING"),
                "Profit":      _number(0),
                "Date":        _date(),
            }
        }
        response = requests.post(URL, headers=HEADERS, data=json.dumps(payload))
        if response.status_code == 200:
            page_id = response.json().get("id", "")
            print(f"📓 Notion logged: {symbol} {side} | {page_id[:8]}...")
            return page_id
        else:
            print(f"⚠️ Notion failed: {response.status_code}")
            return None
    except Exception as e:
        print(f"❌ Notion error (open): {e}")
        return None


def update_trade_result(page_id, result, profit):
    if not page_id or not _configured():
        return
    try:
        response = requests.patch(
            f"https://api.notion.com/v1/pages/{page_id}",
            headers=HEADERS,
            data=json.dumps({
                "properties": {
                    "Status": _select("CLOSED"),
                    "Result": _select(result),
                    "Profit": _number(profit),
                }
            })
        )
        if response.status_code == 200:
            print(f"📓 Notion updated: {result} | ${profit}")
    except Exception as e:
        print(f"❌ Notion error (update): {e}")


def log_signal(symbol, signal, lot, balance, session):
    if not isinstance(signal, dict):
        return None
    zone      = signal.get("zone") or {}
    zone_type = zone.get("type", "UNKNOWN") if isinstance(zone, dict) else "UNKNOWN"
    return log_trade_open(
        symbol=symbol,      side=signal.get("side",        "UNKNOWN"),
        mode=signal.get("mode", "UNKNOWN"),
        entry=signal.get("entry",       0),
        stop_loss=signal.get("stop",    0),
        take_profit=signal.get("tp",    0),
        rr=signal.get("rr",            2.0),
        zone_type=zone_type,
        bos=signal.get("bos",          "NONE"),
        sweep=signal.get("sweep",      "NONE"),
        fvg=signal.get("fvg",           None),
        trend_score=signal.get("trend_score", 0),
        pattern=signal.get("pattern",  "UNKNOWN"),
        session=session,    lot=lot,    balance=balance,
    )
