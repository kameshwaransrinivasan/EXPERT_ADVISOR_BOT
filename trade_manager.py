# =========================================
# trade_manager.py — Position Management
# EA BOT v2 | SRD Section 2 | LOCKED STRATEGY exit model
#
# CRITICAL POLICY: NO FIXED TAKE PROFIT.
# All exits are structural/reversal-based.
#
# LOCKED two-tier exit rule per pipeline — a lower timeframe is EARLY
# WARNING only (logged, never closes by itself); a higher timeframe is
# the PRIMARY CONFIRMATION that actually authorizes the close:
#   Scalping  → M1  = warning | M5 = primary confirmation
#   Intraday  → M15 = warning | H1 = primary confirmation
#   Swing     → H4  = warning | D1 = primary confirmation
#
# "Confirmed" (on the primary TF) = at least 2 independent signals must
# agree (CHoCH + momentum, or sweep + exhaustion + momentum) before
# closing — a lone noisy candle (e.g. one wick, one doji) is never
# enough by itself, on either tier.
#
# Weekend force-close:
#   Scalping + Intraday → force-close Fri 21:00 UTC
#   Swing               → hold through weekend
# =========================================

import MetaTrader5 as mt5
from strategy import (
    get_candles, calculate_volatility,
    candle_pressure, continuation_strength,
    exhaustion_signal, market_phase
)
from market_structure import detect_choch, detect_liquidity_sweep
from market import should_force_close_now


# =========================================
# SHARED REVERSAL CONFIRMATION
# Used by scalping/intraday/swing exits alike — only the candle set
# (timeframe) passed in differs per mode. Requires real confluence so
# exits fire on genuine reversals, not single-candle noise.
# =========================================
def _reversal_confirmed(candles, want_bearish: bool, require_choch: bool = False):
    """
    Returns (True, reason_str) only when 2+ independent signals agree on
    a reversal in the requested direction. Returns (False, "") otherwise.

    require_choch=True drops the SWEEP+EXHAUSTION+PRESSURE path (the only
    one that doesn't need an actual confirmed structural break) — use
    this on the noisiest timeframes (M1 scalping) where that weaker path
    tends to fire on normal consolidation and cut winning trades short
    before they can develop.
    """
    if len(candles) < 10:
        return False, ""

    choch = detect_choch(candles)
    sweep = detect_liquidity_sweep(candles)
    pres  = candle_pressure(candles)
    exh   = exhaustion_signal(candles)
    cont  = continuation_strength(candles)

    target_choch = "BEARISH_CHOCH" if want_bearish else "BULLISH_CHOCH"
    target_sweep = "BUY_SIDE_SWEEP" if want_bearish else "SELL_SIDE_SWEEP"
    target_dir   = "BEARISH" if want_bearish else "BULLISH"

    # Confirmed structural break (candle CLOSE beyond swing point) + momentum agrees
    if choch == target_choch and pres == target_dir:
        return True, "CHoCH+PRESSURE"
    # Confirmed structural break + 2-candle continuation agrees (covers a
    # single neutral-pressure candle right at the break)
    if choch == target_choch and cont == target_dir:
        return True, "CHoCH+CONTINUATION"
    if require_choch:
        return False, ""
    # No structural break yet, but liquidity sweep + exhaustion + momentum
    # all agree — 3-way confluence substitutes for the missing CHoCH
    if sweep == target_sweep and exh and pres == target_dir:
        return True, "SWEEP+EXHAUSTION+PRESSURE"
    return False, ""




# =========================================
# PRIMITIVES
# =========================================
def modify_sl(position, new_sl: float) -> bool:
    req = {
        "action":   mt5.TRADE_ACTION_SLTP,
        "symbol":   position.symbol,
        "position": position.ticket,
        "sl":       float(new_sl),
        "tp":       0.0,          # NO FIXED TP — always 0
    }
    res = mt5.order_send(req)
    return res is not None and res.retcode == mt5.TRADE_RETCODE_DONE


def close_position(position) -> bool:
    tick = mt5.symbol_info_tick(position.symbol)
    if tick is None:
        return False
    if position.type == mt5.ORDER_TYPE_BUY:
        order_type, price = mt5.ORDER_TYPE_SELL, tick.bid
    else:
        order_type, price = mt5.ORDER_TYPE_BUY, tick.ask
    req = {
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       position.symbol,
        "volume":       position.volume,
        "type":         order_type,
        "position":     position.ticket,
        "price":        price,
        "deviation":    20,
        "magic":        777,
        "comment":      "STRUCTURAL_EXIT",
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    res = mt5.order_send(req)
    if res and res.retcode == mt5.TRADE_RETCODE_DONE:
        print(f"❌ CLOSED {position.symbol} #{position.ticket}")
        return True
    return False


def partial_close(position, ratio=0.5) -> bool:
    symbol = position.symbol
    tick   = mt5.symbol_info_tick(symbol)
    info   = mt5.symbol_info(symbol)
    if tick is None or info is None:
        return False
    vol = round(position.volume * ratio, 2)
    vol = max(info.volume_min, vol)
    vol = round(round(vol / info.volume_step) * info.volume_step, 2)
    if vol >= position.volume:
        return close_position(position)
    if position.type == mt5.ORDER_TYPE_BUY:
        order_type, price = mt5.ORDER_TYPE_SELL, tick.bid
    else:
        order_type, price = mt5.ORDER_TYPE_BUY, tick.ask
    req = {
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       symbol,
        "volume":       float(vol),
        "type":         order_type,
        "position":     position.ticket,
        "price":        price,
        "deviation":    20,
        "magic":        777,
        "comment":      "PARTIAL_CLOSE",
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    res = mt5.order_send(req)
    if res and res.retcode == mt5.TRADE_RETCODE_DONE:
        print(f"💰 PARTIAL {symbol} {vol} lots")
        return True
    return False


# =========================================
# SL MANAGEMENT
# =========================================
def move_sl_to_be(position, mode=None):
    """
    Break-even routing — Active Normal Mode:

    SCALPING/RED_NEWS: NOT handled here anymore. scalping_pipeline.
        apply_be_hook() (called from scalping_thread() via
        manage_scalp_positions()) is now the ONE authoritative BE system
        for these modes — it was previously duplicated here with a
        different, less-safe trigger/offset (fixed 10 pips vs. apply_be_hook's
        adaptive min(8pips, SL distance); entry+half-pip buffer vs.
        apply_be_hook's entry+full-spread buffer), running on a second,
        independently-polling thread. Removing the duplicate rather than
        keeping both in sync.

    INTRADAY / SWING: BE triggered at vol(mode-matched timeframe) * 1.5,
        using each mode's own primary exit-confirmation timeframe (see
        intraday_exit()/swing_exit() docstrings: H1/D1 respectively) —
        same fix pattern already applied to lock_profit() below. Used to
        always use M5 (20 candles) for both, which is short-timeframe
        noise relative to what Intraday/Swing are meant to be judged
        against.
    """
    symbol = position.symbol
    tick   = mt5.symbol_info_tick(symbol)
    if tick is None:
        return
    entry = position.price_open
    sl    = position.sl

    if mode is None:
        mode = detect_mode_from_comment(position.comment)

    # ---- SCALPING/RED_NEWS: handled solely by apply_be_hook() now ----
    if mode in ("SCALPING", "RED_NEWS"):
        return

    # ---- INTRADAY / SWING: BE at 1.5x mode-matched-timeframe volatility ----
    tf = {
        "SWING":    mt5.TIMEFRAME_D1,   # matches swing_exit()'s D1 authority
        "INTRADAY": mt5.TIMEFRAME_H1,   # matches intraday_exit()'s H1 authority
    }.get(mode, mt5.TIMEFRAME_M5)       # unexpected mode: safe fallback, unchanged

    candles    = get_candles(symbol, tf, 20)
    vol        = calculate_volatility(candles) if len(candles) >= 10 else 0.0005
    be_trigger = vol * 1.5

    if position.type == mt5.ORDER_TYPE_BUY:
        if (tick.bid - entry) >= be_trigger and sl < entry:
            if modify_sl(position, entry):
                print(f"BE SET BUY {symbol} [{mode}]")
    else:
        if (entry - tick.ask) >= be_trigger and (sl > entry or sl == 0):
            if modify_sl(position, entry):
                print(f"BE SET SELL {symbol} [{mode}]")


def lock_profit(position, mode=None):
    """
    Trail SL to recent swing low/high — timeframe/lookback matched to
    each mode's own primary exit-confirmation timeframe (see the
    scalping_exit()/intraday_exit()/swing_exit() docstrings above:
    M5/H1/D1 respectively), instead of always M5.

    This used to always use M5 + a 5-candle (~25min) lookback for every
    mode. That matches Scalping's own M5 primary-confirmation timeframe,
    but is far tighter than Intraday's (H1) or Swing's (D1) own exit
    philosophy — for Swing in particular, this could ratchet the SL up
    close to price within an hour or two and then stop the trade out on
    completely ordinary lower-timeframe noise that swing_exit() is
    explicitly designed to hold through, bypassing that patient logic
    via the raw SL order instead.
    """
    symbol = position.symbol
    tick   = mt5.symbol_info_tick(symbol)
    if tick is None:
        return

    if mode is None:
        mode = detect_mode_from_comment(position.comment)

    tf, lookback = {
        "SWING":    (mt5.TIMEFRAME_D1, 3),   # matches swing_exit()'s D1 authority
        "INTRADAY": (mt5.TIMEFRAME_H1, 5),   # matches intraday_exit()'s H1 authority
    }.get(mode, (mt5.TIMEFRAME_M5, 5))       # SCALPING/RED_NEWS/unknown: unchanged

    candles = get_candles(symbol, tf, max(30, lookback + 15))
    if len(candles) < lookback + 5:
        return
    vol = calculate_volatility(candles)
    buf = vol * 0.25

    if position.type == mt5.ORDER_TYPE_BUY:
        swing_low = min(c["low"] for c in candles[-lookback:])
        new_sl    = swing_low - buf
        if new_sl > position.sl and new_sl < tick.bid:
            if modify_sl(position, round(new_sl, 5)):
                print(f"🔒 TRAIL BUY {symbol} [{mode}] SL:{new_sl:.5f}")
    else:
        swing_high = max(c["high"] for c in candles[-lookback:])
        new_sl     = swing_high + buf
        if (new_sl < position.sl or position.sl == 0) and new_sl > tick.ask:
            if modify_sl(position, round(new_sl, 5)):
                print(f"🔒 TRAIL SELL {symbol} [{mode}] SL:{new_sl:.5f}")


# =========================================
# PARTIAL TP (structural, not fixed)
# =========================================
def partial_tp(position):
    """
    Close 50% when price reaches 1:1 RR zone
    AND momentum shows signs of fading.
    NO fixed TP price used.
    """
    symbol = position.symbol
    tick   = mt5.symbol_info_tick(symbol)
    if tick is None:
        return
    candles = get_candles(symbol, mt5.TIMEFRAME_M5, 50)
    if len(candles) < 20:
        return

    pressure     = candle_pressure(candles)
    continuation = continuation_strength(candles)
    phase        = market_phase(candles)
    entry        = position.price_open
    vol          = calculate_volatility(candles)
    sl           = position.sl

    risk_dist = abs(entry - sl) if sl > 0 else vol * 2

    if position.type == mt5.ORDER_TYPE_BUY:
        # Strong continuation — hold
        if (pressure == "BULLISH" and continuation == "BULLISH"
                and phase in ["TREND", "EXPANSION"]):
            print(f"🚀 HOLD BUY {symbol}")
            return
        # At 1:1 with fading momentum → partial close
        if tick.bid >= entry + risk_dist and exhaustion_signal(candles):
            partial_close(position, 0.5)
    else:
        if (pressure == "BEARISH" and continuation == "BEARISH"
                and phase in ["TREND", "EXPANSION"]):
            print(f"🚀 HOLD SELL {symbol}")
            return
        if tick.ask <= entry - risk_dist and exhaustion_signal(candles):
            partial_close(position, 0.5)


# =========================================
# STRUCTURAL EXIT ENGINES
# =========================================
def scalping_exit(position):
    """
    Scalping exit — LOCKED STRATEGY: M1 is early warning only, M5 is the
    primary confirmation timeframe that actually authorizes the close.
    A lone M1 CHoCH/BOS never closes the trade by itself — it only gets
    logged as a warning while M5 keeps being watched.
    """
    symbol = position.symbol
    c_m1   = get_candles(symbol, mt5.TIMEFRAME_M1, 50)
    c_m5   = get_candles(symbol, mt5.TIMEFRAME_M5, 100)

    is_buy = position.type == mt5.ORDER_TYPE_BUY

    warn, warn_reason = _reversal_confirmed(c_m1, want_bearish=is_buy, require_choch=True)
    if warn:
        print(f"⚠️  SCALP WARNING [M1 {warn_reason}] {symbol} — watching for M5 confirmation")

    ok, reason = _reversal_confirmed(c_m5, want_bearish=is_buy)
    if ok:
        print(f"📐 SCALP EXIT [M5 confirmed: {reason}] {symbol}")
        close_position(position)


def intraday_exit(position):
    """
    Intraday exit — LOCKED STRATEGY: M15 is early warning only, H1 is the
    primary confirmation timeframe that actually authorizes the close.
    """
    symbol = position.symbol
    c_m15  = get_candles(symbol, mt5.TIMEFRAME_M15, 100)
    c_h1   = get_candles(symbol, mt5.TIMEFRAME_H1, 100)

    is_buy = position.type == mt5.ORDER_TYPE_BUY

    warn, warn_reason = _reversal_confirmed(c_m15, want_bearish=is_buy, require_choch=True)
    if warn:
        print(f"⚠️  INTRADAY WARNING [M15 {warn_reason}] {symbol} — watching for H1 confirmation")

    ok, reason = _reversal_confirmed(c_h1, want_bearish=is_buy)
    if ok:
        print(f"📐 INTRADAY EXIT [H1 confirmed: {reason}] {symbol}")
        close_position(position)


def swing_exit(position):
    """
    Swing exit — LOCKED STRATEGY: H4 is early warning only, D1 is the
    primary confirmation timeframe that actually authorizes the close.
    Weekend hold is fine — do NOT force-close on Fridays.

    A lone H4 CHoCH/BOS is logged as a warning only; the position stays
    open (held through normal lower-timeframe pullback/noise) until D1
    structure is genuinely invalidated.

    FREE TRADE hook (unchanged mechanic):
    At 1:2 RR reached → move SL to entry + spread
    = Zero risk, let it ride to the next confirmed D1 reversal.
    """
    symbol = position.symbol
    tick   = mt5.symbol_info_tick(symbol)
    if tick is None:
        return

    c_h4 = get_candles(symbol, mt5.TIMEFRAME_H4, 100)
    c_d1 = get_candles(symbol, mt5.TIMEFRAME_D1, 100)
    if len(c_d1) < 10:
        return

    # ---- FREE TRADE BE at 1:2 RR (unchanged) ----
    entry  = position.price_open
    sl     = position.sl
    spread = abs(tick.ask - tick.bid)

    if sl > 0:
        risk = abs(entry - sl)
        rr2_target = (entry + risk * 2.0 if position.type == mt5.ORDER_TYPE_BUY
                      else entry - risk * 2.0)

        price_now = tick.bid if position.type == mt5.ORDER_TYPE_BUY else tick.ask

        if position.type == mt5.ORDER_TYPE_BUY:
            at_rr2     = price_now >= rr2_target
            not_free   = sl < entry          # SL still below entry = not free yet
        else:
            at_rr2     = price_now <= rr2_target
            not_free   = sl > entry or sl == 0

        if at_rr2 and not_free:
            # Move SL to entry + spread = free trade
            new_sl = (round(entry + spread, 5) if position.type == mt5.ORDER_TYPE_BUY
                      else round(entry - spread, 5))
            req = {
                "action":   mt5.TRADE_ACTION_SLTP,
                "symbol":   symbol,
                "position": position.ticket,
                "sl":       new_sl,
                "tp":       0.0,
            }
            res = mt5.order_send(req)
            if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                print(f"🆓 SWING FREE TRADE {symbol} "
                      f"{'BUY' if position.type == mt5.ORDER_TYPE_BUY else 'SELL'} "
                      f"→ SL moved to {new_sl:.5f} (1:2 RR reached, zero risk)")

    # ---- H4 = early warning, D1 = primary confirmation ----
    is_buy = position.type == mt5.ORDER_TYPE_BUY

    if len(c_h4) >= 10:
        warn, warn_reason = _reversal_confirmed(c_h4, want_bearish=is_buy, require_choch=True)
        if warn:
            print(f"⚠️  SWING WARNING [H4 {warn_reason}] {symbol} — watching for D1 confirmation")

    ok, reason = _reversal_confirmed(c_d1, want_bearish=is_buy)
    if ok:
        print(f"🌊 SWING EXIT [D1 confirmed: {reason}] {symbol}")
        close_position(position)


# =========================================
# UNIFIED EXIT DISPATCHER
# =========================================
def detect_mode_from_comment(comment: str) -> str:
    c = comment.upper()
    if "SWING"    in c: return "SWING"
    if "INTRADAY" in c: return "INTRADAY"
    if "SCALPING" in c: return "SCALPING"
    if "RED_NEWS" in c: return "RED_NEWS"
    return "SCALPING"


def manage_position(position):
    """
    Master position manager.
    Dispatches to the correct exit engine based on trade mode.
    Also handles SL management for all modes.
    """
    mode = detect_mode_from_comment(position.comment)

    # Always apply SL management
    move_sl_to_be(position, mode)
    lock_profit(position, mode)

    # Structural exit logic by mode
    if mode in ["SCALPING", "RED_NEWS"]:
        scalping_exit(position)
    elif mode == "INTRADAY":
        intraday_exit(position)
        partial_tp(position)
    elif mode == "SWING":
        swing_exit(position)
        # No partial_tp for swing — let it run to full reversal


# =========================================
# WEEKEND FORCE-CLOSE
# Scalping + Intraday only.  Swing exempt.
# =========================================
def weekend_force_close():
    """
    Call from main loop.  Closes all non-swing positions on Fri 21:00 UTC.
    """
    if not should_force_close_now():
        return False

    positions = mt5.positions_get()
    if not positions:
        return True

    closed = 0
    for pos in positions:
        mode = detect_mode_from_comment(pos.comment)
        if mode == "SWING":
            print(f"⛵ SWING POSITION HELD OVER WEEKEND: {pos.symbol}")
            continue
        print(f"🔒 WEEKEND FORCE-CLOSE [{mode}]: {pos.symbol}")
        if close_position(pos):
            closed += 1

    print(f"🔒 Weekend force-close: {closed} positions closed")
    return True


# =========================================
# LEGACY ALIASES (backward compat)
# =========================================
def move_sl_to_be_legacy(position):
    move_sl_to_be(position)

def trend_exit(position):
    manage_position(position)
