# =========================================
# fxjers_main.py — MASTER BOT (RUN THIS)
# EA BOT v2 | FX Jers Mentorship 2.0
# Updated: SRD Full Integration
#
# Pipelines:
#   SWING     → D1/H1 | weekend hold OK
#   INTRADAY  → H4/M15 | no weekend hold
#   SCALPING  → M5/M1  | no weekend hold
#   RED_NEWS  → news_main.py handles
#
# New in v2:
#   - No fixed TP (structural exits only)
#   - Mandatory SL gate on every order
#   - No hedging guard
#   - Weekend force-close Fri 21:00 UTC
#   - Regime classifier (volatile → halve risk)
#   - Directional alignment: intraday & swing
#     must match — close if conflict
#   - Scalping caps scale with balance
# =========================================

import MetaTrader5 as mt5
import time
from datetime import datetime, timezone

from fxjers_signal   import fxjers_signal, swing_signal
from strategy        import smc_hybrid_signal, get_candles
from execution       import place_trade
from trade_manager   import (manage_position, weekend_force_close,
                              detect_mode_from_comment)
from memory_ai       import save_trade_memory, init_memory, extract_ticket
from error_log       import log_error, log_news_block
from closed_memory   import update_trade_result
from risk            import dd_protection, dynamic_risk_engine, pipeline_caps
from market          import (is_market_open, get_current_session,
                              is_holiday, session_strength, should_force_close_now)
from market_regime   import classify_regime
from news            import get_news_signal, is_red_news
from fxnews          import get_forex_factory_news
from news_bias       import scalp_news_block, pre_news_scan
from notion_journal  import log_signal, update_trade_result as notion_update
from ai_learning     import ai_filter, performance_ai, weekly_ai_review
from email_report    import (send_daily_report, send_weekly_report,
                              send_monthly_report)
from ml_engine       import MLEngine

# ---- Initialise ML engine (loads saved models if available) ----
_ml = MLEngine()
from market_structure import get_market_structure


# =========================================
# SYMBOLS
# =========================================
SYMBOLS = [
    "GOLD",
    "EURUSD",
    "USDJPY#",
    "GBPUSD",
    "EURJPY#",
    "GBPJPY#",
    "XAGUSD#",
    "AUDUSD#",
    "USDCAD#",
]

SESSION_MODULE = {
    "OVERLAP":  "INTRADAY",
    "LONDON":   "INTRADAY",
    "NEWYORK":  "INTRADAY",
    "ASIAN":    "SCALPING",
    "OFF":      None,
}


# =========================================
# MT5 INIT
# =========================================
if not mt5.initialize():
    print("❌ MT5 FAILED — check terminal is open")
    quit()
print("✅ MT5 CONNECTED")
init_memory()


# =========================================
# STATE
# =========================================
news_cache        = []
notion_page_map   = {}
last_daily_email  = None
last_weekly_email = None
last_monthly_email= None
last_swing_scan   = None
last_news_refresh = None


# =========================================
# HELPERS
# =========================================
def can_open_trade(symbol: str, mode: str,
                   trend_score: float, dd_status: str,
                   balance: float) -> bool:
    """
    Checks DD, position caps (SRD 3B), directional conflict.
    Counts pending orders as well as open positions — pending limit
    orders were previously uncounted, letting them stack unchecked.
    """
    if dd_status in ["DANGER", "TEST_MODE"]:
        print(f"🚫 DD BLOCK — {dd_status}")
        return False

    positions = mt5.positions_get() or []
    pending   = mt5.orders_get() or []

    def _mode_tag(comment):
        return str(comment).split("|")[0].upper()

    pair_count  = sum(1 for p in positions if p.symbol == symbol and _mode_tag(p.comment) == mode)
    pair_count += sum(1 for o in pending   if o.symbol == symbol and _mode_tag(o.comment) == mode)

    total = sum(1 for p in positions if _mode_tag(p.comment) == mode)
    total += sum(1 for o in pending   if _mode_tag(o.comment) == mode)

    caps = pipeline_caps(mode, balance)
    pair_max  = caps["pair_max"]
    port_max  = caps["portfolio_max"]

    # Elite trade bump
    if trend_score >= 0.80:
        pair_max  += 1
        port_max  += 1

    if total >= port_max:
        print(f"🚫 PORTFOLIO MAX ({port_max}) REACHED [{mode}]")
        return False
    if pair_count >= pair_max:
        print(f"🚫 {symbol} PAIR MAX ({pair_max}) [{mode}]")
        return False
    return True


def directional_conflict(symbol: str, side: str) -> bool:
    """
    SRD Section 1: No hedging — block if opposite trade exists.
    Also: intraday/swing must align — close conflicts.
    """
    positions = mt5.positions_get(symbol=symbol) or []
    for p in positions:
        if p.type == mt5.ORDER_TYPE_BUY  and side == "SELL": return True
        if p.type == mt5.ORDER_TYPE_SELL and side == "BUY":  return True
    return False


def check_intraday_swing_alignment():
    """
    SRD 1: If intraday and swing positions on same pair
    are in opposite directions → close the intraday one.
    """
    positions = mt5.positions_get() or []
    by_symbol: dict = {}

    for p in positions:
        sym  = p.symbol
        mode = detect_mode_from_comment(p.comment)
        if sym not in by_symbol:
            by_symbol[sym] = {}
        if mode not in by_symbol[sym]:
            by_symbol[sym][mode] = []
        by_symbol[sym][mode].append(p)

    for sym, modes in by_symbol.items():
        swing_pos    = modes.get("SWING",    [])
        intraday_pos = modes.get("INTRADAY", [])
        if not swing_pos or not intraday_pos:
            continue
        swing_type = swing_pos[0].type
        for ip in intraday_pos:
            if ip.type != swing_type:
                print(f"⚠️ ALIGNMENT CONFLICT: closing intraday {sym}")
                from execution import close_position
                close_position(ip)


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


def run_swing_scanner(session: str, balance: float, ai_data):
    global last_swing_scan
    now = datetime.now(timezone.utc)
    if last_swing_scan and (now - last_swing_scan).total_seconds() < 3600:
        return
    last_swing_scan = now
    print("\n🔭 SWING SCANNER...")

    for symbol in SYMBOLS:
        sig = swing_signal(symbol)
        if not isinstance(sig, dict):
            continue

        # Regime check — skip during volatile expansion for swing
        regime = classify_regime(symbol)
        if regime["action"] == "EXIT_SWING":
            print(f"🌊 REGIME: skip swing entry {symbol} — macro reversal")
            continue

        side        = sig["side"]
        trend_score = sig["trend_score"]
        dd_info     = dd_protection()

        if not can_open_trade(symbol, "SWING", trend_score,
                               dd_info["status"], balance):
            continue
        if directional_conflict(symbol, side):
            print(f"🚫 SWING: direction conflict {symbol}")
            continue

        sl_dist = abs(sig["entry"] - sig["stop"])
        if sl_dist <= 0:
            continue

        risk = dynamic_risk_engine(
            symbol=symbol, session=session, balance=balance,
            mode="SWING", sl_distance=sl_dist, ai_data=ai_data,
            volatile_expansion=regime["volatile_expansion"]
        )
        lot     = risk["lot"]
        pattern = sig.get("pattern", "") or "SWING_BOS"
        result  = place_trade(
            symbol, side, lot,
            sig["stop"],
            0.0,
            mode="SWING",
            trade_type="NORMAL",
            pattern=pattern
        )
        if isinstance(result, str) and "✅" in result:
            print(f"📈 SWING OPENED: {symbol}")
            tick    = mt5.symbol_info_tick(symbol)
            page_id = log_signal(symbol, sig, lot, balance, session)
            save_trade_memory(
                pair=symbol, side=side,
                spread=round(abs(tick.ask - tick.bid), 5),
                trend_score=trend_score,
                pattern=pattern,
                balance=round(balance, 2),
                volatility=sig.get("volatility", 0),
                trade_type="NORMAL",
                mode="SWING", result="OPEN", profit=0,
                session=session, news_mode="NORMAL", news_bias="NEUTRAL",
                ticket=extract_ticket(result),
                lot=lot,
                entry_price=(tick.ask if side == "BUY" else tick.bid),
                sl_price=sig["stop"],
            )
        else:
            print(f"❌ SWING FAILED: {symbol} | {result}")
            log_error(symbol, "SWING", result)


# =========================================
# MAIN LOOP
# =========================================
print("\n🚀 EA BOT v2 ACTIVE")
print("📚 SMC + FX Jers | No Fixed TP | Structural Exits")
print("=" * 50)

while True:

    # ---- MARKET OPEN ----
    if not is_market_open():
        print("🚫 MARKET CLOSED")
        time.sleep(60)
        continue

    if is_holiday():
        print("🎄 HOLIDAY — skipping")
        time.sleep(60)
        continue

    now             = datetime.now(timezone.utc)
    current_session = get_current_session()
    strength        = session_strength()
    module          = SESSION_MODULE.get(current_session)

    print(f"\n===== {now.strftime('%H:%M:%S UTC')} | {current_session} | {strength} | {module} =====")

    # ---- WEEKEND FORCE-CLOSE (Scalping + Intraday) ----
    if should_force_close_now():
        weekend_force_close()
        print("🔒 Weekend close triggered. Sleeping 3600s...")
        time.sleep(3600)
        continue

    if module is None:
        print("⏸️ OFF SESSION")
        time.sleep(120)
        continue

    # ---- DD CHECK ----
    dd_info = dd_protection()
    print(f"🛡️ DD: {dd_info['status']} | {dd_info['dd']}%")
    if dd_info["status"] == "TEST_MODE":
        print("🚫 DD > 15% — HALTED")
        time.sleep(60)
        continue

    # ---- SYNC ----
    update_trade_result()
    sync_notion_results()
    check_intraday_swing_alignment()
    ai_data = performance_ai()

    # ---- ACCOUNT ----
    account = mt5.account_info()
    if account is None:
        time.sleep(30)
        continue
    balance = account.balance

    # ---- SWING SCAN (hourly) ----
    run_swing_scanner(current_session, balance, ai_data)

    # ---- NEWS REFRESH (every 5 min) ----
    refresh_due = (
        last_news_refresh is None or
        (now - last_news_refresh).total_seconds() >= 300
    )
    if refresh_due:
        temp = get_forex_factory_news()
        if temp:
            news_cache        = temp
            last_news_refresh = now
            print(f"📰 News: {len(news_cache)} events loaded")

    # ---- PRE-NEWS ALERT (T-1 minute) ----
    alerts = pre_news_scan(news_cache, SYMBOLS)
    for alert in alerts:
        print(f"⚠️ PRE-NEWS T-1min: {alert['currency']} — "
              f"{alert['title']} | Pairs: {alert['pairs_affected']}")

    # ---- EMAIL SCHEDULE (UTC) ----
    today = now.date()
    if now.hour == 0 and now.minute < 1:
        if last_daily_email != today:
            send_daily_report()
            last_daily_email = today
        if now.weekday() == 5 and last_weekly_email != today:
            send_weekly_report()
            _ml.run_weekly_review()   # RL advisory review → ai_advisory.csv
            last_weekly_email = today
        if now.day == 1 and last_monthly_email != today:
            send_monthly_report()
            last_monthly_email = today

    # =========================================
    # SYMBOL LOOP
    # =========================================
    for symbol in SYMBOLS:
        print(f"\n📊 {symbol}")

        # ---- REGIME CHECK ----
        regime = classify_regime(symbol)
        volatile = regime["volatile_expansion"]
        if regime["action"] == "EXIT_SWING":
            # Macro reversal — swing exit handled by manage_position
            pass

        # ---- NEWS FILTER ----
        news_status, news_text = get_news_signal(symbol, news_cache)
        print(f"📰 {news_text}")

        # Scalping: check pre/post news block window
        if module == "SCALPING":
            scalp_block = scalp_news_block(symbol, news_cache)
            if scalp_block == "FORCE_CLOSE":
                print(f"🔴 SCALP NEWS: force-close {symbol} positions (T-3min)")
                for p in (mt5.positions_get(symbol=symbol) or []):
                    if "SCALPING" in p.comment.upper():
                        from execution import close_position as _cp
                        _cp(p)
                continue
            if scalp_block == "FREEZE":
                print(f"⏸️ SCALP NEWS FREEZE: {symbol} (30min post-release)")
                continue

        if news_status == "BLOCK":
            print(f"🚫 NEWS BLOCK: {symbol}")
            log_news_block(symbol, "INTRADAY")
            continue

        # ---- REGIME FREEZE (breakout entries) ----
        if regime["action"] == "TIGHTEN" and module != "SCALPING":
            print(f"🌊 CHOPPY RANGE: freezing breakout entry {symbol}")
            continue

        # ---- SIGNAL ----
        red_news = is_red_news(symbol, news_cache)
        if red_news:
            # Hand off to news_main for institutional news scalping
            print(f"🔴 RED NEWS: delegating {symbol} to news engine")
            continue

        signal = fxjers_signal(symbol, module)
        if isinstance(signal, str):
            signal = smc_hybrid_signal(symbol, get_candles)

        if isinstance(signal, str):
            print(f"⚠️ {signal}")
            continue

        side        = signal["side"]
        trend_score = signal["trend_score"]
        mode        = signal["mode"]
        print(f"✅ SIGNAL: {side} | {mode} | Score:{trend_score}")

        # ---- CHECKS ----
        if not can_open_trade(symbol, mode, trend_score,
                               dd_info["status"], balance):
            continue
        if directional_conflict(symbol, side):
            print("🚫 DIRECTION CONFLICT — blocked (no hedging)")
            continue
        if not ai_filter(symbol):
            print("🤖 AI BLOCKED")
            continue

        # ---- ML PROBABILITY GATE (SRD 7: ≥65% required) ----
        _candles_gate = get_candles(symbol, mt5.TIMEFRAME_M5, 150)
        _gate = _ml.evaluate_entry(_candles_gate, side, symbol)
        print(f"🎯 ML Gate: {_gate['reason']}")
        if not _gate["allow"]:
            continue

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
            balance=balance, mode=mode,
            sl_distance=sl_distance, ai_data=ai_data,
            volatile_expansion=volatile,
        )
        lot        = risk_result["lot"]
        trade_type = "ELITE" if trend_score >= 0.80 else "NORMAL"
        pattern    = signal.get("pattern", "") or f"SMC_{mode}"
        print(f"💰 Risk:{risk_result['risk_percent']}% | Lot:{lot} | Pattern:{pattern}")

        # ---- EXECUTE (no fixed TP) ----
        result = place_trade(
            symbol, side, lot,
            sl_price,
            0.0,
            mode=mode,
            trade_type=trade_type,
            pattern=pattern
        )

        if isinstance(result, str) and "✅" in result:
            print(f"✅ TRADE OPENED | {symbol} | {side} | {mode}")
            print(f"   ML Score: {_gate['score']:.1%} | "
                  f"Regime: {_gate['regime']} | "
                  f"LSTM: {_gate['lstm_direction']}")
            page_id = log_signal(symbol, signal, lot, balance, current_session)

            from news_smc import analyze_news
            news_analysis = analyze_news({"title": news_text,
                                          "impact": news_status})
            save_trade_memory(
                pair=symbol,      side=side,
                spread=round(abs(tick.ask - tick.bid), 5),
                trend_score=trend_score,
                pattern=pattern,
                balance=round(balance, 2),
                volatility=signal.get("volatility", 0),
                trade_type=trade_type,
                mode=mode,        result="OPEN", profit=0,
                session=current_session,
                news_mode=news_analysis["mode"],
                news_bias=news_analysis["bias"]["bias"],
                ticket=extract_ticket(result),
                lot=lot,
                entry_price=entry_price,
                sl_price=sl_price,
            )
        else:
            print(f"❌ {result}")
            log_error(symbol, mode, result)

    # =========================================
    # MANAGE OPEN POSITIONS
    # =========================================
    positions = mt5.positions_get()
    if positions:
        for pos in positions:
            manage_position(pos)    # dispatches by mode: scalp/intra/swing

    time.sleep(30)
