# =============================================================================
# main.py — EA BOT v2 | SINGLE RUN FILE
# =============================================================================
# python main.py   <- this only
#
# Threads:
#   Thread 1 -> SCALPING pipeline   (every 10 seconds,  Asian session)
#   Thread 2 -> INTRADAY pipeline   (every 30 seconds,  London/NY/Overlap)
#   Thread 3 -> SWING pipeline      (every 60 minutes,  all sessions)
#   Thread 4 -> NEWS pipeline       (every 15 seconds,  London/NY/Overlap)
#   Main     -> position manager + status dashboard + emails
# =============================================================================

import MetaTrader5 as mt5
import threading
import time
import os
from datetime import datetime, timezone

# ---- Core ----
from strategy        import smc_hybrid_signal, get_candles
from fxjers_signal   import fxjers_signal, swing_signal
from execution       import (place_trade, place_limit_order,
                              close_position, is_scalp_whitelisted)
from trade_manager   import (manage_position, weekend_force_close,
                              detect_mode_from_comment)
from memory_ai       import save_trade_memory, init_memory, extract_ticket
from error_log       import log_error, log_news_block
from closed_memory   import update_trade_result
from risk            import dd_protection, dynamic_risk_engine, pipeline_caps
from market          import (is_market_open, get_current_session,
                              is_holiday, session_strength,
                              should_force_close_now)
from market_regime   import classify_regime
from market_structure import get_market_structure

# ---- Scalping pipeline (sweet spot refactor) ----
from scalping_pipeline import (get_scalp_signal, manage_scalp_positions,
                                MIN_SCORE)

# ---- 3-Confirmation systems ----
from scalping_confirmation  import check_scalping_confirmations
# removed: check_intraday_confirmations — consolidated into fxjers_signal.
# intraday_signal()'s own M15 score (see intraday_confirmation.py header)
# removed: from swing_confirmation import check_swing_confirmations


# ---- News ----
from fxnews          import get_forex_factory_news
from news            import get_news_signal as smc_news_filter, is_red_news
from news_signal     import get_news_signal as build_news_signal, has_live_news
from news_bias       import (get_best_bias, scalp_news_block,
                              pre_news_scan, direction_zone_failsafe)
from news_smc        import analyze_news

# ---- AI / ML ----
from ai_learning     import ai_filter, performance_ai
# ML removed — pure strategy mode

# ---- Journal / Reports ----
from notion_journal  import log_signal, update_trade_result as notion_update
from email_report    import (send_daily_report, send_weekly_report,
                              send_monthly_report)


# =============================================================================
# CONFIG
# =============================================================================
SCALPING_SYMBOLS = [
    "GOLD",
    "EURUSD",
    "GBPUSD",
]

INTRADAY_SYMBOLS = [
    "GOLD",
    "EURUSD",
    "GBPUSD",
]

SWING_SYMBOLS = [
    "GOLD",
    "EURUSD",
    "GBPUSD",
]

NEWS_SYMBOLS = [
    "GOLD",
    "EURUSD",
    "GBPUSD",
]

SCALP_INTERVAL    = 30    # 30 sec — fast scalping
INTRADAY_INTERVAL = 60    # 60 sec — intraday
SWING_INTERVAL    = 120   # 120 sec — swing (was 3600, too slow)
NEWS_INTERVAL     = 15
MAIN_INTERVAL     = 30


# =============================================================================
# MT5 INIT
# =============================================================================
if not mt5.initialize():
    print("MT5 FAILED — open terminal first")
    quit()

print("=" * 55)
print("EA BOT v2 — STARTING [Pure Strategy Mode]")
print("=" * 55)
init_memory()
print("No ML — Sweet Spot + Confirmation gate only")
print()


# =============================================================================
# SHARED STATE
# =============================================================================
_lock             = threading.Lock()
news_cache        = []
notion_page_map   = {}
traded_news_ids   = set()
last_news_refresh = None

bot_status = {
    "scalping": {"state": "STARTING", "last_trade": "-", "trades_today": 0},
    "intraday": {"state": "STARTING", "last_trade": "-", "trades_today": 0},
    "swing":    {"state": "STARTING", "last_trade": "-", "trades_today": 0},
    "news":     {"state": "STARTING", "last_trade": "-", "trades_today": 0},
}

last_daily_email   = None
last_weekly_email  = None
last_monthly_email = None


# =============================================================================
# HELPERS
# =============================================================================
def _set_status(pipeline, state, last_trade=None):
    with _lock:
        bot_status[pipeline]["state"] = state
        if last_trade:
            bot_status[pipeline]["last_trade"]   = last_trade
            bot_status[pipeline]["trades_today"] += 1


def _get_cache():
    with _lock:
        return list(news_cache)


def no_hedge(symbol, side):
    for p in (mt5.positions_get(symbol=symbol) or []):
        if p.type == mt5.ORDER_TYPE_BUY  and side == "SELL": return False
        if p.type == mt5.ORDER_TYPE_SELL and side == "BUY":  return False
    return True


def can_open(symbol, mode, trend_score, dd_status, balance):
    if dd_status in ["DANGER", "TEST_MODE"]:
        return False
    positions   = mt5.positions_get() or []
    pending     = mt5.orders_get() or []          # pending limit/stop orders — were NOT counted before, causing duplicate stacking
    caps        = pipeline_caps(mode, balance)
    pair_max    = caps["pair_max"]   + (1 if trend_score >= 0.80 else 0)
    port_max    = caps["portfolio_max"] + (1 if trend_score >= 0.80 else 0)

    def _mode_tag(comment):
        return str(comment).split("|")[0].upper()

    pair_count  = sum(1 for p in positions if p.symbol == symbol and _mode_tag(p.comment) == mode)
    pair_count += sum(1 for o in pending   if o.symbol == symbol and _mode_tag(o.comment) == mode)

    total_count = sum(1 for p in positions if _mode_tag(p.comment) == mode)
    total_count += sum(1 for o in pending   if _mode_tag(o.comment) == mode)

    return total_count < port_max and pair_count < pair_max


def sync_notion():
    now   = datetime.now(timezone.utc)
    past  = now.replace(hour=0, minute=0, second=0)
    deals = mt5.history_deals_get(past, now)
    if not deals:
        return
    for deal in deals:
        if deal.entry != mt5.DEAL_ENTRY_OUT:
            continue
        with _lock:
            if deal.ticket in notion_page_map:
                profit = round(deal.profit, 2)
                result = "WIN" if profit > 0 else ("LOSS" if profit < 0 else "BE")
                notion_update(notion_page_map[deal.ticket], result, profit)
                del notion_page_map[deal.ticket]


def check_alignment():
    positions = mt5.positions_get() or []
    by_sym = {}
    for p in positions:
        mode = detect_mode_from_comment(p.comment)
        by_sym.setdefault(p.symbol, {}).setdefault(mode, []).append(p)
    for sym, modes in by_sym.items():
        swings   = modes.get("SWING",    [])
        intraday = modes.get("INTRADAY", [])
        if not swings or not intraday:
            continue
        for ip in intraday:
            if ip.type != swings[0].type:
                print(f"ALIGNMENT: closing intraday conflict {sym}")
                close_position(ip)


def execute_trade(symbol, side, mode, signal, balance, session, pipeline,
                  news_text="OK", news_status="OK", risk_multiplier=1.0):
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return False
    entry   = tick.ask if side == "BUY" else tick.bid
    sl      = signal["stop"]
    sl_dist = abs(entry - sl)
    if sl_dist <= 0:
        return False

    # Extract pattern — never allow UNKNOWN through
    pattern = signal.get("pattern", "")
    if not pattern or pattern == "UNKNOWN":
        # Intelligent fallback based on mode
        _fallbacks = {
            "SCALPING":  "SMC_SCALP",
            "INTRADAY":  "SMC_INTRADAY",
            "SWING":     "SWING_BOS",
            "RED_NEWS":  "NEWS_TRADE",
        }
        pattern = _fallbacks.get(mode, "SMC_SIGNAL")

    # ML gate removed — pure strategy mode
    # All entries decided by Sweet Spot + Confirmation gate only

    # Risk — apply regime risk_multiplier
    regime   = classify_regime(symbol)
    eff_mult = min(risk_multiplier, regime["risk_multiplier"])
    ai_data  = performance_ai()
    risk     = dynamic_risk_engine(
        symbol=symbol, session=session, balance=balance,
        mode=mode, sl_distance=sl_dist, ai_data=ai_data,
        volatile_expansion=regime["volatile_expansion"]
    )
    lot = round(max(0.01, risk["lot"] * eff_mult), 2)
    print(f"   Risk:{risk['risk_percent']}% Lot:{lot} (x{eff_mult})")

    trade_type = "ELITE" if signal.get("trend_score", 0) >= 0.80 else "NORMAL"

    result = place_trade(
        symbol, side, lot, sl, 0.0,
        mode=mode,
        trade_type=trade_type,
        pattern=pattern          # ← injected into MT5 comment as MODE|PATTERN|TYPE
    )

    if isinstance(result, str) and ("✅" in result or "OPENED" in result):
        ts      = datetime.now(timezone.utc).strftime("%H:%M:%S")
        page_id = log_signal(symbol, signal, lot, balance, session)
        if page_id:
            with _lock:
                notion_page_map[page_id] = page_id
        na = analyze_news({"title": news_text, "impact": news_status})
        save_trade_memory(
            pair=symbol, side=side,
            spread=round(abs(tick.ask - tick.bid), 5),
            trend_score=signal.get("trend_score", 0),
            pattern=pattern,     # ← always a real value, never UNKNOWN
            balance=round(balance, 2),
            volatility=signal.get("volatility", 0),
            trade_type=trade_type,
            mode=mode, result="OPEN", profit=0,
            session=session,
            news_mode=na["mode"],
            news_bias=na["bias"]["bias"],
            ticket=extract_ticket(result),
            lot=lot,
            entry_price=(tick.ask if side == "BUY" else tick.bid),
            sl_price=sl,
        )
        _set_status(pipeline, "IN TRADE",
                    last_trade=f"{symbol} {side} {ts}")
        print(f"   OPENED {mode} | {symbol} {side} | Pattern:{pattern}")
        return True

    print(f"   FAILED: {result}")
    log_error(symbol, mode, result)
    return False


# =============================================================================
# THREAD 1 — SCALPING  (Asian session, every 10s)
# =============================================================================
def scalping_thread():
    """
    Sweet Spot Scalping Thread.
    Uses confluence scoring (min 4 pts), limit orders at FVG boundary,
    BE hook at 8–10 pips, 70/30 partial close loop.
    Whitelist: EURUSD, GBPUSD, BTCUSDT only.
    Sessions: ASIAN + LONDON (scalping runs both).
    """
    print("Scalping Thread started [Sweet Spot Mode]")
    _partials_done: set = set()   # tracks 70% partial completed tickets

    while True:
        try:
            if not is_market_open() or is_holiday():
                _set_status("scalping", "MARKET CLOSED")
                time.sleep(60)
                continue

            if should_force_close_now():
                _set_status("scalping", "WEEKEND CLOSE")
                time.sleep(60)
                continue

            session = get_current_session()
            # Scalping: Asian + London (whitelisted pairs liquid both sessions)
            if session not in ["ASIAN", "LONDON", "OVERLAP"]:
                _set_status("scalping", f"WAITING ({session})")
                time.sleep(SCALP_INTERVAL)
                continue

            dd_info = dd_protection()
            if dd_info["status"] in ["DANGER", "TEST_MODE"]:
                _set_status("scalping", "DD BLOCK")
                time.sleep(60)
                continue

            account = mt5.account_info()
            if not account:
                time.sleep(SCALP_INTERVAL)
                continue
            balance = account.balance
            cache   = _get_cache()

            # ---- Manage open scalp positions first (BE + 70/30 + exit) ----
            _partials_done = manage_scalp_positions(_partials_done)

            _set_status("scalping", "SCANNING")

            # ---- Signal loop ----
            for symbol in SCALPING_SYMBOLS:

                if not can_open(symbol, "SCALPING", 0.6,
                                dd_info["status"], balance):
                    continue
                if not no_hedge(symbol, "BUY"):   # pre-check both sides
                    continue

                # Full sweet spot signal (scoring, whitelist, spread, news buffer)
                signal = get_scalp_signal(symbol, cache)

                if isinstance(signal, str):
                    print(f"[SCALP] {symbol}: {signal}")
                    continue

                side  = signal["side"]
                score = signal["score"]

                if not no_hedge(symbol, side):
                    continue

                # ================================================================
                # LOCKED SCALPING GATE
                #
                # get_scalp_signal() already performed the full locked M5
                # (structure -> valid S/D -> valid BOS/CHoCH -> POI) -> M1
                # (structure/displacement/BOS-CHoCH/FVG-refined-zone/retest,
                # min 2/4) pipeline before returning a dict at all. The only
                # thing still needed here is the session-timing check — c2/c3
                # of check_scalping_confirmations are redundant with
                # get_scalp_signal's own M5-BOS and M1-retest checks, so we
                # reuse only its session check (c1). c1 now delegates to the
                # same market.get_current_session() authority as the "session"
                # gate above (line ~320), so this is a confirmatory restatement
                # of that same decision, not a second independent gate.
                # ================================================================
                c_m1 = get_candles(symbol, mt5.TIMEFRAME_M1, 100)
                c_m5 = get_candles(symbol, mt5.TIMEFRAME_M5, 100)
                conf = check_scalping_confirmations(symbol, c_m1, c_m5, side)

                if score < MIN_SCORE:
                    print(f"[SCALP] SKIP {symbol} — M1 confirmations {score}/4 < {MIN_SCORE}")
                    continue

                if not conf["c1"]["passed"]:
                    print(f"[SCALP] SKIP {symbol} — {conf['c1']['reason']}")
                    continue

                # Gate passed → ENTER
                print(f"\n[SCALP] ENTER {symbol} {side} | M5 POI+BOS ✅ | M1 confs:{score}/4 ✅")

                # ---- Risk sizing ----
                regime  = classify_regime(symbol)

                # CHOPPY_RANGE skip: C2/C3 confirmation (M5 BOS + M1 OB
                # retest+continuation) are momentum/breakout criteria —
                # unreliable in a choppy range where breaks are more often
                # false. True mean-reversion entry logic (fade zone
                # extremes) was never actually built despite the old
                # comment claiming it — until it is, skip scalping here
                # entirely rather than taking momentum-style entries at
                # half size in the regime least suited to them.
                if regime["choppy_range"]:
                    print(f"[SCALP] SKIP {symbol} — CHOPPY_RANGE "
                          f"(momentum confirmation unreliable in chop)")
                    log_error(symbol, "SCALPING", "Regime filter: choppy range skip")
                    continue

                ai_data = performance_ai()
                sl_dist = abs(signal["entry_ref"] - signal["stop"])
                if sl_dist <= 0:
                    continue

                risk = dynamic_risk_engine(
                    symbol=symbol, session=session, balance=balance,
                    mode="SCALPING", sl_distance=sl_dist, ai_data=ai_data,
                    volatile_expansion=regime["volatile_expansion"]
                )
                lot  = round(max(0.01,
                                 risk["lot"] * regime["risk_multiplier"]), 2)

                # ---- ORDER: direct market entry only (no pending orders) ----
                # Pattern = actual candle pattern from signal + conf tag
                pattern = (signal.get("pattern") or
                           conf["c3"].get("tag") or
                           f"SCALP_CONF_{conf['score']}PT")

                result = place_trade(
                    symbol, side, lot,
                    signal["stop"], 0.0,
                    mode="SCALPING",
                    trade_type="NORMAL",
                    pattern=pattern              # ← injected into comment
                )

                if "LIMIT_PLACED" in str(result) or "✅" in str(result):
                    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                    print(f"  MARKET ORDER: {symbol} {side} "
                          f"| SL:{signal['stop']:.5f} | Pattern:{pattern}")
                    _set_status("scalping", "IN TRADE",
                                last_trade=f"{symbol} {side} {ts}")

                    tick   = mt5.symbol_info_tick(symbol)
                    spread = abs(tick.ask - tick.bid) if tick else 0
                    save_trade_memory(
                        pair=symbol, side=side,
                        spread=round(spread, 5),
                        trend_score=signal["trend_score"],
                        pattern=pattern,             # ← always real value
                        balance=round(balance, 2),
                        volatility=signal["volatility"],
                        trade_type="NORMAL",
                        mode="SCALPING",
                        result="OPEN", profit=0,
                        session=session,
                        news_mode="NORMAL", news_bias="NEUTRAL",
                        ticket=extract_ticket(result),
                        lot=lot,
                        entry_price=(tick.ask if side == "BUY" else tick.bid) if tick else 0,
                        sl_price=signal["stop"],
                    )
                else:
                    print(f"  [SCALP] Order failed: {result}")
                    log_error(symbol, "SCALPING", result)

        except Exception as e:
            import traceback
            print(f"[SCALP ERROR] {e}")
            traceback.print_exc()
            _set_status("scalping", "ERROR")

        time.sleep(SCALP_INTERVAL)


# =============================================================================
# THREAD 2 — INTRADAY  (London/NY/Overlap, every 30s)
# =============================================================================
def intraday_thread():
    print("Intraday Thread started")
    while True:
        try:
            if not is_market_open() or is_holiday():
                _set_status("intraday", "MARKET CLOSED")
                time.sleep(60); continue

            if should_force_close_now():
                _set_status("intraday", "WEEKEND CLOSE")
                time.sleep(60); continue

            session = get_current_session()
            if session not in ["LONDON", "NEWYORK", "OVERLAP"]:
                _set_status("intraday", f"WAITING ({session})")
                time.sleep(INTRADAY_INTERVAL); continue

            dd_info = dd_protection()
            if dd_info["status"] in ["DANGER", "TEST_MODE"]:
                _set_status("intraday", "DD BLOCK")
                time.sleep(60); continue

            account = mt5.account_info()
            if not account:
                time.sleep(INTRADAY_INTERVAL); continue
            balance = account.balance
            cache   = _get_cache()

            _set_status("intraday", "SCANNING")

            for symbol in INTRADAY_SYMBOLS:
                ns, nt = smc_news_filter(symbol, cache)
                if ns == "BLOCK" or is_red_news(symbol, cache):
                    continue

                regime = classify_regime(symbol)
                # Active Normal Mode: intraday still uses breakout logic
                # but CHOPPY_RANGE: allow mean reversion at zone extremes
                #
                # VOLATILE_EXPANSION: previously skipped Intraday entirely
                # here. Verified against CHANGELOG's original SRD regime
                # table ("VOLATILE_EXPANSION -> Halve risk per trade") and
                # against Scalping/Swing, which both already trade through
                # this regime at reduced size via regime["risk_multiplier"]
                # rather than skipping — Intraday was the one inconsistent
                # pipeline, with no documented reason to be the exception.
                # No hard block here anymore; risk_multiplier (0.5 for this
                # regime, unchanged) is still applied below via execute_trade,
                # same as it always was for the trades that did get through.

                if regime["choppy_range"]:
                    from strategy import mean_reversion_signal
                    signal = mean_reversion_signal(symbol, get_candles)
                else:
                    # LOCKED INTRADAY STRATEGY: H4->H1->M15 only. The
                    # previous smc_hybrid_signal() fallback bypassed H4/H1
                    # entirely (an M1/M5-anchored signal) and could label
                    # an unrelated setup as "INTRADAY" — removed to keep
                    # strategy isolation intact.
                    signal = fxjers_signal(symbol, "INTRADAY")

                if isinstance(signal, str):
                    continue

                # ---- Pattern watch (informational only — NOT a gate) ----
                # BULLISH_PINBAR/BEARISH_ENGULFING/INSIDE_BAR were flagged
                # from a single day's small per-pattern samples (4-8 trades)
                # as possibly weak. That's not enough evidence to hard-block
                # on, so this no longer skips the trade — it just logs the
                # pattern so you can review whether the pattern actually
                # correlates with losses once there's a real sample size.
                sig_pattern = str(signal.get("pattern", "")).upper()
                _watch_patterns = {"BULLISH_PINBAR", "BEARISH_ENGULFING", "INSIDE_BAR"}
                if any(p in sig_pattern for p in _watch_patterns):
                    print(f"[INTRADAY] WATCH {symbol} — pattern:{sig_pattern} (not blocked, logging only)")

                side = signal["side"]
                ts   = signal.get("trend_score", 0)
                m15_score = signal.get("m15_score")

                if not can_open(symbol, "INTRADAY", ts, dd_info["status"], balance):
                    continue
                if not no_hedge(symbol, side):
                    continue
                if not ai_filter(symbol):
                    continue

                # intraday_signal() already ran the full consolidated
                # H4->H1(mandatory)->M15(scored) gate — no second
                # confirmation module needed here (see intraday_confirmation.py).
                print(f"\n[INTRADAY] {symbol} {side} Score:{ts} | M15 score:{m15_score}/5")
                execute_trade(symbol, side, "INTRADAY", signal,
                              balance, session, "intraday", nt, ns,
                              risk_multiplier=regime["risk_multiplier"])

        except Exception as e:
            print(f"[INTRADAY ERROR] {e}")
            _set_status("intraday", "ERROR")

        time.sleep(INTRADAY_INTERVAL)


# =============================================================================
# THREAD 3 — SWING  (every 60 min, weekend hold OK)
# =============================================================================
def swing_thread():
    print("Swing Thread started")
    while True:
        try:
            if not is_market_open() or is_holiday():
                _set_status("swing", "MARKET CLOSED")
                time.sleep(300); continue

            session = get_current_session()
            if session == "OFF":
                _set_status("swing", "WAITING (OFF)")
                time.sleep(SWING_INTERVAL); continue

            dd_info = dd_protection()
            if dd_info["status"] in ["DANGER", "TEST_MODE"]:
                _set_status("swing", "DD BLOCK")
                time.sleep(300); continue

            account = mt5.account_info()
            if not account:
                time.sleep(SWING_INTERVAL); continue
            balance = account.balance

            _set_status("swing", "SCANNING")
            print("\n[SWING] Scanner running...")

            for symbol in SWING_SYMBOLS:
                regime = classify_regime(symbol)
                if regime["action"] == "EXIT_SWING":
                    continue

                signal = swing_signal(symbol)
                if not isinstance(signal, dict):
                    continue

                side = signal["side"]
                ts   = signal.get("trend_score", 0)

                if not can_open(symbol, "SWING", ts, dd_info["status"], balance):
                    continue
                if not no_hedge(symbol, side):
                    continue

                # LOCKED SWING STRATEGY: valid D1 zone touch = direct
                # entry. swing_signal() already ran the full W1(macro)
                # -> D1(structure + STRICT valid S/D) -> zone-touch
                # pipeline before returning a dict at all — no mandatory
                # H4/H1 confirmation gate before entry. H4 is used only
                # for post-entry monitoring inside trade_manager.swing_exit().
                print(f"\n[SWING] {symbol} {side} Score:{ts} | D1 direct-touch entry")
                execute_trade(symbol, side, "SWING", signal,
                              balance, session, "swing")

        except Exception as e:
            print(f"[SWING ERROR] {e}")
            _set_status("swing", "ERROR")

        time.sleep(SWING_INTERVAL)


# =============================================================================
# THREAD 4 — NEWS  (London/NY/Overlap, every 15s)
# =============================================================================
def news_thread():
    global news_cache, last_news_refresh, traded_news_ids
    print("News Thread started")

    while True:
        try:
            if not is_market_open() or is_holiday():
                _set_status("news", "MARKET CLOSED")
                time.sleep(60); continue

            if should_force_close_now():
                _set_status("news", "WEEKEND CLOSE")
                time.sleep(60); continue

            now     = datetime.now(timezone.utc)
            session = get_current_session()

            if session not in ["LONDON", "NEWYORK", "OVERLAP"]:
                _set_status("news", f"WAITING ({session})")
                time.sleep(NEWS_INTERVAL); continue

            dd_info = dd_protection()
            if dd_info["status"] in ["DANGER", "TEST_MODE"]:
                _set_status("news", "DD BLOCK")
                time.sleep(60); continue

            account = mt5.account_info()
            if not account:
                time.sleep(NEWS_INTERVAL); continue
            balance = account.balance

            # ---- SMART ADAPTIVE REFRESH ----
            # Normal state   → check every 10 min (600s) — avoids FF 429
            # News within 8min → switch to every 60 sec (high alert)
            # News passed     → back to 10 min
            cache = _get_cache()

            # Find nearest upcoming high-impact news
            nearest_news_sec = None
            if cache:
                for item in cache:
                    try:
                        if str(item.get("impact","")).upper() != "HIGH":
                            continue
                        diff = (item["time"] - now).total_seconds()
                        if 0 < diff < 600:   # within 10 min ahead
                            if nearest_news_sec is None or diff < nearest_news_sec:
                                nearest_news_sec = diff
                    except Exception:
                        continue

            # Decide refresh interval
            if nearest_news_sec is not None and nearest_news_sec <= 480:
                # News within 8 min → high alert mode → refresh every 60s
                refresh_interval = 60
                alert_label = f"⚡ NEWS IN {int(nearest_news_sec//60)}m{int(nearest_news_sec%60)}s"
            else:
                # Normal → refresh every 10 min
                refresh_interval = 600
                alert_label = "normal"

            refresh_due = (
                last_news_refresh is None or
                (now - last_news_refresh).total_seconds() >= refresh_interval
            )

            if refresh_due:
                temp = get_forex_factory_news()
                if temp:
                    with _lock:
                        news_cache        = temp
                        last_news_refresh = now
                    print(f"[NEWS] Refreshed: {len(temp)} events "
                          f"| next check in {refresh_interval//60}min "
                          f"| {alert_label}")

            if not cache:
                _set_status("news", "NO DATA")
                time.sleep(NEWS_INTERVAL); continue

            # T-1 min pre-news alerts
            for a in pre_news_scan(cache, NEWS_SYMBOLS):
                print(f"[NEWS] T-1min: {a['currency']} | "
                      f"{a['title']} | {a['secs_to_release']:.0f}s")

            _set_status("news", "SCANNING")

            for symbol in NEWS_SYMBOLS:
                if not has_live_news(symbol, cache):
                    continue

                block = scalp_news_block(symbol, cache)
                if block == "FORCE_CLOSE":
                    for p in (mt5.positions_get(symbol=symbol) or []):
                        close_position(p)
                    continue
                if block == "FREEZE":
                    continue

                if not can_open(symbol, "RED_NEWS", 0.65,
                                dd_info["status"], balance):
                    continue
                if not ai_filter(symbol):
                    continue

                signal = build_news_signal(symbol, cache)
                if isinstance(signal, str):
                    continue

                bias_report = get_best_bias(symbol, cache)
                if bias_report:
                    nid = (f"{bias_report['currency']}_"
                           f"{bias_report['title'][:20]}_"
                           f"{bias_report['elapsed_seconds']:.0f}")
                    with _lock:
                        if nid in traded_news_ids:
                            continue

                zone = signal.get("zone")
                if zone and bias_report:
                    if not direction_zone_failsafe(
                            signal["side"], zone.get("type", "")):
                        print(f"[NEWS] FAILSAFE block: {symbol}")
                        continue

                side = signal["side"]
                if not no_hedge(symbol, side):
                    continue

                print(f"\n[NEWS] {symbol} {side} "
                      f"Impact:{signal['impact']} Bias:{signal['bias']}")

                ok = execute_trade(symbol, side, "RED_NEWS", signal,
                                   balance, session, "news",
                                   signal["impact"], signal["bias"])
                if ok and bias_report:
                    with _lock:
                        traded_news_ids.add(nid)

            with _lock:
                if len(traded_news_ids) > 50:
                    traded_news_ids.clear()

        except Exception as e:
            print(f"[NEWS ERROR] {e}")
            _set_status("news", "ERROR")

        time.sleep(NEWS_INTERVAL)


# =============================================================================
# STATUS DASHBOARD
# =============================================================================
def print_dashboard(session, strength, dd_info, balance, open_count):
    os.system("cls" if os.name == "nt" else "clear")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    with _lock:
        s = {k: dict(v) for k, v in bot_status.items()}

    print("=" * 56)
    print("        EA BOT v2  --  LIVE STATUS")
    print("=" * 56)
    print(f"  Time      : {now}")
    print(f"  Session   : {session} | {strength}")
    print(f"  Balance   : ${balance:,.2f}")
    print(f"  DD        : {dd_info['dd']}%  [{dd_info['status']}]")
    print(f"  Open Pos  : {open_count}")
    print("-" * 56)
    print(f"  {'PIPELINE':<12}  {'STATE':<22}  {'TRADES':>6}")
    print("-" * 56)
    for name, data in s.items():
        print(f"  {name.upper():<12}  {data['state']:<22}  "
              f"{data['trades_today']:>6}")
    print("-" * 56)
    print(f"  Last Scalp  : {s['scalping']['last_trade']}")
    print(f"  Last Intra  : {s['intraday']['last_trade']}")
    print(f"  Last Swing  : {s['swing']['last_trade']}")
    print(f"  Last News   : {s['news']['last_trade']}")
    print("=" * 56)


# =============================================================================
# MAIN THREAD
# =============================================================================
def main():
    global last_daily_email, last_weekly_email, last_monthly_email

    print("\nLaunching 4 pipeline threads...")

    target_map = {
        "SCALPING":  scalping_thread,
        "INTRADAY":  intraday_thread,
        "SWING":     swing_thread,
        "NEWS":      news_thread,
    }

    threads = []
    for name, target in target_map.items():
        t = threading.Thread(target=target, name=name, daemon=True)
        t.start()
        threads.append(t)
        print(f"  {name} thread started")

    print("\nALL THREADS RUNNING")
    print("=" * 55)

    while True:
        try:
            now      = datetime.now(timezone.utc)
            session  = get_current_session()
            strength = session_strength()

            if should_force_close_now():
                weekend_force_close()

            update_trade_result()
            sync_notion()
            check_alignment()

            account   = mt5.account_info()
            balance   = account.balance if account else 0.0
            dd_info   = dd_protection()
            positions = mt5.positions_get() or []

            # Position management (structural exits)
            for pos in positions:
                manage_position(pos)

            # Dashboard
            print_dashboard(session, strength, dd_info,
                            balance, len(positions))

            # Email schedule
            today = now.date()
            if now.hour == 0 and now.minute < 1:
                if last_daily_email != today:
                    send_daily_report()
                    last_daily_email = today
                if now.weekday() == 5 and last_weekly_email != today:
                    send_weekly_report()
                    pass  # ML removed
                    last_weekly_email = today
                if now.day == 1 and last_monthly_email != today:
                    send_monthly_report()
                    last_monthly_email = today

            # Thread watchdog — restart if crashed
            for t in threads:
                if not t.is_alive():
                    print(f"WARNING: {t.name} thread died — restarting")
                    new_t = threading.Thread(
                        target=target_map[t.name],
                        name=t.name, daemon=True
                    )
                    new_t.start()
                    threads[threads.index(t)] = new_t

        except Exception as e:
            print(f"[MAIN ERROR] {e}")

        time.sleep(MAIN_INTERVAL)


if __name__ == "__main__":
    main()
