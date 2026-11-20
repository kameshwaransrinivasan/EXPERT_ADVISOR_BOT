# =========================================
# news_signal.py
# Institutional News Reaction Scalping Bot
# MASTER SIGNAL ENGINE
# News Bias + 30M Zone + LTF Confirmation
# =========================================

import MetaTrader5 as mt5
from strategy import get_candles, calculate_volatility
from news_bias import get_best_bias, required_confirmations
from news_zones import (
    get_30m_zone_touch,
    get_nearest_30m_zone,
    get_best_confirmation,
    calculate_news_sl,
    spread_ok,
    get_spread,
    volatility_ok,
)


# =========================================
# TAKE PROFIT CALCULATOR
# Based on RR ratio from zone
# =========================================
def calculate_news_tp(direction, entry, sl, rr=2.0):
    risk = abs(entry - sl)
    if direction == "BUY":
        return round(entry + risk * rr, 5)
    return round(entry - risk * rr, 5)


# =========================================
# IMPACT RULES
# Red     → strict  (need zone + confs)
# Orange  → medium  (zone preferred)
# Yellow  → simpler (1 confirmation ok)
# =========================================
IMPACT_RULES = {
    "HIGH":   {"need_zone": True,  "min_conf_score": 3},
    "MEDIUM": {"need_zone": True,  "min_conf_score": 2},
    "LOW":    {"need_zone": False, "min_conf_score": 1},
}


# =========================================
# MASTER NEWS SIGNAL
# Returns full signal dict or reason string
# =========================================
def get_news_signal(symbol, news_cache):
    """
    Full pipeline:
    1. Get best bias for symbol
    2. Check no-trade window
    3. Check spread
    4. Check volatility
    5. Check 30M zone touch
    6. Check LTF confirmations
    7. Apply impact rules
    8. Build signal
    """

    print(f"\n{'='*45}")
    print(f"📰 NEWS SIGNAL CHECK | {symbol}")
    print(f"{'='*45}")

    # ---- STEP 1: GET BEST NEWS BIAS ----
    bias_report = get_best_bias(symbol, news_cache)
    if bias_report is None:
        return "NEWS: NO RELEVANT BIAS"

    direction  = bias_report["direction"]
    impact     = bias_report["impact"]
    bias       = bias_report["bias"]
    news_time  = None
    elapsed    = bias_report["elapsed_seconds"]
    req_confs  = bias_report["required_confs"]

    print(f"📊 Bias: {bias} | Direction: {direction}")
    print(f"⏱  Elapsed: {elapsed:.0f}s | Need: {req_confs} confirmations")
    print(f"🔴 Impact: {impact} | Currency: {bias_report['currency']}")
    print(f"📰 News: {bias_report['title']}")
    print(f"   Actual:{bias_report['actual']} | Forecast:{bias_report['forecast']}")

    # ---- STEP 2: NO-TRADE WINDOW ----
    if bias_report["no_trade_window"]:
        print("⛔ IN NO-TRADE WINDOW (first 2 mins) — waiting")
        return "NEWS: NO TRADE WINDOW — WAIT 2 MINS"

    if direction == "NONE" or bias == "NEUTRAL":
        return "NEWS: NEUTRAL — NO TRADE"

    # ---- STEP 3: SPREAD FILTER ----
    if not spread_ok(symbol):
        return f"NEWS: SPREAD TOO WIDE ({get_spread(symbol):.5f})"

    # ---- STEP 4: VOLATILITY FILTER ----
    if not volatility_ok(symbol):
        print("⚠️ VOLATILITY TOO HIGH — waiting for calm")
        return "NEWS: VOLATILITY TOO HIGH"

    # ---- STEP 5: 30M ZONE TOUCH ----
    rules     = IMPACT_RULES.get(impact, IMPACT_RULES["MEDIUM"])
    zone      = get_30m_zone_touch(symbol, direction)
    zone_near = None

    if zone is None:
        # Check proximity
        zone_near = get_nearest_30m_zone(symbol, direction)
        if zone_near:
            print(f"📍 ZONE NEARBY (not yet touched): {zone_near['low']:.5f}–{zone_near['high']:.5f}")

        if rules["need_zone"]:
            return "NEWS: WAITING FOR 30M ZONE TOUCH"

    active_zone = zone or zone_near

    # ---- STEP 6: LTF CONFIRMATIONS ----
    conf_result = get_best_confirmation(symbol, direction)
    conf_count  = conf_result["count"]
    conf_score  = conf_result["score"]
    confs       = conf_result["confirmations"]
    conf_tf     = conf_result.get("timeframe", "M1")

    print(f"✅ Confirmations: {confs}")
    print(f"📊 Score: {conf_score} | Count: {conf_count}")

    # ---- STEP 7: APPLY IMPACT RULES ----
    # Time-based: within first 5 min = need 2 confs, after = 1 conf
    needed_confs = req_confs  # from bias_report (0 / 1 / 2)

    if conf_count < needed_confs:
        return (f"NEWS: NEED {needed_confs} CONFIRMATIONS "
                f"(have {conf_count}) — [{', '.join(confs)}]")

    if conf_score < rules["min_conf_score"]:
        return (f"NEWS: CONF SCORE TOO LOW "
                f"({conf_score} < {rules['min_conf_score']})")

    # ---- STEP 8: BUILD SIGNAL ----
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return "NEWS: NO TICK DATA"

    entry = tick.ask if direction == "BUY" else tick.bid

    # SL: use zone if available, else use volatility-based SL
    if active_zone:
        sl = calculate_news_sl(symbol, direction, active_zone)
    else:
        candles = get_candles(symbol, mt5.TIMEFRAME_M5, 20)
        vol     = calculate_volatility(candles) if len(candles) >= 10 else 0.0005
        sl = round(entry - vol * 2 if direction == "BUY"
                   else entry + vol * 2, 5)

    if abs(entry - sl) <= 0:
        return "NEWS: ZERO SL DISTANCE"

    # RR based on impact
    rr_map = {"HIGH": 3.0, "MEDIUM": 2.0, "LOW": 1.5}
    rr     = rr_map.get(impact, 2.0)
    tp     = calculate_news_tp(direction, entry, sl, rr)

    # Trend score based on confirmation strength
    trend_score = min(0.50 + conf_score * 0.05, 0.95)

    signal = {
        "side":          direction,
        "mode":          "RED_NEWS",
        "entry":         entry,
        "stop":          sl,
        "tp":            tp,
        "rr":            rr,
        "zone":          active_zone,
        "zone_on_30m":   zone is not None,
        "confirmations": confs,
        "conf_score":    conf_score,
        "conf_tf":       conf_tf,
        "impact":        impact,
        "bias":          bias,
        "currency":      bias_report["currency"],
        "news_title":    bias_report["title"],
        "elapsed_secs":  elapsed,
        "spread":        get_spread(symbol),
        "trend_score":   trend_score,
        "pattern":       confs[0] if confs else "NEWS_TRADE",
        "volatility":    calculate_volatility(
                            get_candles(symbol, mt5.TIMEFRAME_M5, 20)
                         ),
    }

    print(f"\n🚀 NEWS SIGNAL READY!")
    print(f"   Symbol: {symbol} | Side: {direction}")
    print(f"   Entry: {entry} | SL: {sl} | TP: {tp}")
    print(f"   RR: {rr} | Impact: {impact} | Bias: {bias}")
    print(f"   Confs: {confs} | Score: {conf_score}")
    print(f"   Zone (30M): {zone is not None} | TF: {conf_tf}")

    return signal


# =========================================
# QUICK CHECK: Is there any live news now?
# =========================================
def has_live_news(symbol, news_cache):
    """
    Returns True if there's any HIGH/MEDIUM impact
    news that released in the last 30 minutes
    relevant to this symbol.
    """
    bias = get_best_bias(symbol, news_cache)
    return bias is not None
