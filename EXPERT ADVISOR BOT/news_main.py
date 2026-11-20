# =========================================
# news_main.py
# EA BOT v2 | Institutional News Reaction Bot
# SRD Section 5 — Full Implementation
#
# Flow per SRD:
#   T-1 min  → pre_news_scan: identify currency + pairs
#   T-3 min  → force-close all scalp positions
#   Release  → wait 2 min (no-trade window)
#   2–5 min  → need 2 LTF confirmations
#   > 5 min  → need 1 confirmation
#   Zone     → price must reach 30M S/D zone
#   Failsafe → direction vs zone mismatch → BLOCK
#   Exit     → structural CHoCH only (no fixed TP)
# =========================================

import MetaTrader5 as mt5
import time
from datetime import datetime, timezone

from fxnews        import get_forex_factory_news
from news_signal   import get_news_signal as build_news_signal, has_live_news
from news_bias     import (get_best_bias, scalp_news_block,
                           pre_news_scan, direction_zone_failsafe)
from execution     import place_trade
from trade_manager import manage_position, weekend_force_close
from memory_ai     import save_trade_memory, init_memory, extract_ticket
from error_log     import log_error, log_news_block
from closed_memory import update_trade_result
from risk          import dd_protection, dynamic_risk_engine
from market        import (is_market_open, get_current_session,
                           is_holiday, session_strength,
                           should_force_close_now)
from market_regime import classify_regime
from ai_learning   import ai_filter, performance_ai, weekly_ai_review
from notion_journal import log_signal, update_trade_result as notion_update
from email_report  import send_daily_report, send_weekly_report
from ml_engine     import MLEngine

# ---- Shared ML engine instance ----
_ml = MLEngine()


# =========================================
# CONFIG
# =========================================
SYMBOLS = [
    "EURUSD", "GBPUSD", "USDJPY#",
    "GOLD", "EURJPY#", "GBPJPY#",
    "AUDUSD#", "USDCHF#",
]
ALLOWED_SESSIONS   = ["LONDON", "NEWYORK", "OVERLAP"]
NEWS_REFRESH_SECS  = 60


# =========================================
# MT5 INIT
# =========================================
if not mt5.initialize():
    print("❌ MT5 FAILED")
    quit()
print("✅ MT5 CONNECTED — NEWS BOT v2")
init_memory()


# =========================================
# STATE
# =========================================
news_cache          = []
last_news_refresh   = None
notion_page_map     = {}
last_daily_email    = None
last_weekly_email   = None
traded_news_ids     = set()


# =========================================
# HELPERS
# =========================================
def can_open_trade(symbol: str, dd_status: str, balance: float) -> bool:
    if dd_status in ["DANGER", "TEST_MODE"]:
        print(f"🚫 DD BLOCK — {dd_status}")
        return False
    positions  = mt5.positions_get() or []
    total      = len(positions)
    pair_count = sum(1 for p in positions if p.symbol == symbol)
    if total >= 4:
        print("🚫 MAX TOTAL POSITIONS (4)")
        return False
    if pair_count >= 2:
        print(f"🚫 {symbol} MAX 2 NEWS TRADES")
        return False
    return True


def sync_notion_results():
    now  = datetime.now(timezone.utc)
    past = now.replace(hour=0, minute=0, second=0)
    deals = mt5.history_deals_get(past, now)
    if not deals:
        return
    for deal in deals:
        if deal.entry != mt5.DEAL_ENTRY_OUT:
            continue
        ticket = deal.ticket
        if ticket in notion_page_map:
            profit = round(deal.profit, 2)
            result = "WIN" if profit > 0 else ("LOSS" if profit < 0 else "BE")
            notion_update(notion_page_map[ticket], result, profit)
            del notion_page_map[ticket]


def make_news_id(bias_report: dict) -> str:
    return (f"{bias_report['currency']}_"
            f"{bias_report['title'][:20]}_"
            f"{bias_report['elapsed_seconds']:.0f}")


# =========================================
# MAIN LOOP
# =========================================
print("\n🔥 NEWS SCALPING BOT v2 ACTIVE")
print("📚 Strategy: Institutional News Reaction (SRD Section 5)")
print("Flow: Bias → 2min Wait → 30M Zone + Failsafe → LTF Confirm → Trade")
print("=" * 55)

while True:

    # ---- MARKET ----
    if not is_market_open():
        print("🚫 MARKET CLOSED")
        time.sleep(60)
        continue

    if is_holiday():
        print("🎄 HOLIDAY")
        time.sleep(60)
        continue

    # ---- WEEKEND FORCE-CLOSE ----
    if should_force_close_now():
        weekend_force_close()
        time.sleep(3600)
        continue

    now             = datetime.now(timezone.utc)
    current_session = get_current_session()
    strength        = session_strength()

    print(f"\n[{now.strftime('%H:%M:%S UTC')}] "
          f"Session: {current_session} | {strength}")

    if current_session not in ALLOWED_SESSIONS:
        print(f"⏸️ {current_session} — News scalping London/NY/Overlap only")
        time.sleep(120)
        continue

    # ---- DD ----
    dd_info = dd_protection()
    print(f"🛡️ DD: {dd_info['status']} ({dd_info['dd']}%)")
    if dd_info["status"] == "TEST_MODE":
        print("🚫 DD > 15% — HALTED")
        time.sleep(60)
        continue

    # ---- ACCOUNT ----
    account = mt5.account_info()
    if account is None:
        time.sleep(30)
        continue
    balance = account.balance

    # ---- SYNC ----
    update_trade_result()
    sync_notion_results()
    ai_data = performance_ai()

    # ---- NEWS REFRESH ----
    refresh_due = (
        last_news_refresh is None or
        (now - last_news_refresh).total_seconds() >= NEWS_REFRESH_SECS
    )
    if refresh_due:
        print("📰 Refreshing news...")
        temp = get_forex_factory_news()
        if temp:
            news_cache        = temp
            last_news_refresh = now
            print(f"✅ {len(news_cache)} events loaded")
        else:
            print("⚠️ Using cached news")

    if not news_cache:
        print("📭 No news — waiting")
        time.sleep(30)
        continue

    # ---- PRE-NEWS ALERTS (T-1 minute) ----
    alerts = pre_news_scan(news_cache, SYMBOLS)
    for a in alerts:
        print(f"⚠️ PRE-NEWS: {a['currency']} — {a['title']} "
              f"in {a['secs_to_release']:.0f}s | Pairs: {a['pairs_affected']}")

    # ---- EMAIL SCHEDULE ----
    today = now.date()
    if now.hour == 0 and now.minute < 1 and last_daily_email != today:
        send_daily_report()
        last_daily_email = today
    if now.weekday() == 5 and now.hour == 0 and last_weekly_email != today:
        send_weekly_report()
        last_weekly_email = today

    # =========================================
    # SYMBOL LOOP
    # =========================================
    for symbol in SYMBOLS:

        # Quick: any relevant news?
        if not has_live_news(symbol, news_cache):
            continue

        print(f"\n📊 {symbol} — live news detected")

        # ---- SCALP NEWS BLOCK (T-3min force-close / 30min freeze) ----
        scalp_block = scalp_news_block(symbol, news_cache)
        if scalp_block == "FORCE_CLOSE":
            print(f"🔴 T-3min: force-close scalp positions {symbol}")
            for p in (mt5.positions_get(symbol=symbol) or []):
                from execution import close_position as _cp
                _cp(p)
            continue
        if scalp_block == "FREEZE":
            print(f"⏸️ 30-min post-news freeze: {symbol}")
            continue

        # ---- POSITION LIMITS ----
        if not can_open_trade(symbol, dd_info["status"], balance):
            continue

        # ---- REGIME ----
        regime   = classify_regime(symbol)
        volatile = regime["volatile_expansion"]

        # ---- AI FILTER ----
        if not ai_filter(symbol):
            print(f"🤖 AI BLOCKED {symbol}")
            continue

        # ---- FULL SIGNAL PIPELINE ----
        signal = build_news_signal(symbol, news_cache)

        if isinstance(signal, str):
            print(f"⚠️ {signal}")
            continue

        # ---- DUPLICATE CHECK ----
        bias_report = get_best_bias(symbol, news_cache)
        if bias_report:
            news_id = make_news_id(bias_report)
            if news_id in traded_news_ids:
                print(f"🔁 Already traded: {news_id[:30]}")
                continue

        # ---- DIRECTION-ZONE FAILSAFE (SRD 5.4) ----
        zone = signal.get("zone")
        if zone and bias_report:
            zone_type = zone.get("type", "")
            direction = signal["side"]
            if not direction_zone_failsafe(direction, zone_type):
                print(f"⛔ FAILSAFE: {direction} bias but price at "
                      f"{zone_type} zone — setup INVALIDATED")
                continue

        # ---- HEDGING CHECK ----
        side = signal["side"]
        existing = mt5.positions_get(symbol=symbol) or []
        conflict = any(
            (p.type == mt5.ORDER_TYPE_BUY  and side == "SELL") or
            (p.type == mt5.ORDER_TYPE_SELL and side == "BUY")
            for p in existing
        )
        if conflict:
            print("🚫 OPPOSITE TRADE EXISTS — no hedging")
            continue

        # ---- ML PROBABILITY GATE (SRD 7: ≥65% required) ----
        import MetaTrader5 as _mt5_inner
        from strategy import get_candles as _gc
        _candles_gate = _gc(symbol, _mt5_inner.TIMEFRAME_M5, 150)
        _gate = _ml.evaluate_entry(_candles_gate, side, symbol)
        print(f"🎯 ML Gate: {_gate['reason']}")
        if not _gate["allow"]:
            continue

        # ---- TICK ----
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            continue
        entry_price = tick.ask if side == "BUY" else tick.bid
        sl_price    = signal["stop"]
        sl_distance = abs(entry_price - sl_price)
        if sl_distance <= 0:
            continue

        # ---- LOT SIZE ----
        risk_result = dynamic_risk_engine(
            symbol=symbol, session=current_session,
            balance=balance, mode="RED_NEWS",
            sl_distance=sl_distance, ai_data=ai_data,
            volatile_expansion=volatile
        )
        lot     = risk_result["lot"]
        pattern = signal.get("pattern", "") or "NEWS_TRADE"
        confs   = signal.get("confirmations", [])
        if confs and pattern in ("", "NEWS_TRADE"):
            pattern = confs[0] if confs else "NEWS_TRADE"
        print(f"💰 Risk: {risk_result['risk_percent']}% | Lot: {lot} | Pattern:{pattern}")

        # ---- EXECUTE (no fixed TP) ----
        result = place_trade(
            symbol, side, lot,
            sl_price,
            0.0,
            mode="RED_NEWS",
            trade_type="NORMAL",
            pattern=pattern
        )

        if isinstance(result, str) and "✅" in result:
            print(f"✅ NEWS TRADE OPENED | {symbol} | {side}")
            print(f"   Impact: {signal['impact']} | Bias: {signal['bias']}")
            print(f"   Confs: {signal['confirmations']} | Score: {signal['conf_score']}")
            print(f"   Zone(30M): {signal['zone_on_30m']}")
            print(f"   Failsafe OK ✅")

            if bias_report:
                traded_news_ids.add(news_id)

            page_id = log_signal(symbol, signal, lot, balance, current_session)
            save_trade_memory(
                pair=symbol, side=side,
                spread=signal["spread"],
                trend_score=signal["trend_score"],
                pattern=pattern,
                balance=round(balance, 2),
                volatility=signal["volatility"],
                trade_type="NORMAL", mode="RED_NEWS",
                result="OPEN", profit=0,
                session=current_session,
                news_mode=signal["impact"],
                news_bias=signal["bias"],
                ticket=extract_ticket(result),
                lot=lot,
                entry_price=entry_price,
                sl_price=sl_price,
            )
        else:
            print(f"❌ Trade failed: {result}")
            log_error(symbol, "RED_NEWS", result)

    # =========================================
    # MANAGE OPEN POSITIONS
    # =========================================
    positions = mt5.positions_get()
    if positions:
        for pos in positions:
            manage_position(pos)

    # Prune old news IDs
    if len(traded_news_ids) > 50:
        traded_news_ids.clear()

    time.sleep(15)
