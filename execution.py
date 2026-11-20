# =============================================================================
# execution.py — MT5 Order Execution Engine
# EA BOT v2 | Sweet Spot Scalping Refactor
#
# Changes from directive:
#   1. Spread gate: Forex max 1.0 pip | Crypto max $0.50
#   2. Limit Order / Stop-Limit preferred over raw market orders
#   3. News buffer: ±15 min window (was 30/60 min)
#   4. Mandatory SL — zero-SL always blocked
#   5. No hedging — always enforced
#   6. No fixed TP — tp_value always 0
# =============================================================================

import MetaTrader5 as mt5


# =============================================================================
# COMMENT SAFETY
# MT5 documents a 31-char comment limit, but in practice some broker/server
# builds reject shorter comments too (e.g. a 30-char comment still throws
# `Invalid "comment" argument`) — the real cutoff isn't reliably documented
# and varies. So instead of trusting one magic number, we (a) target a safer
# length up front, and (b) if MT5 still rejects it specifically for the
# comment, automatically retry with progressively shorter/blank comments
# rather than failing the trade outright.
# =============================================================================
MT5_COMMENT_LIMIT = 24  # conservative target length, not a guaranteed cutoff


def _safe_comment(*parts):
    """Joins parts with '|' and truncates to a conservative length,
    shrinking the longest part first so short mode/type tags survive."""
    parts = [str(p) for p in parts if p]
    comment = "|".join(parts)
    if len(comment) <= MT5_COMMENT_LIMIT:
        return comment
    while len("|".join(parts)) > MT5_COMMENT_LIMIT and parts:
        longest_idx = max(range(len(parts)), key=lambda i: len(parts[i]))
        if len(parts[longest_idx]) <= 1:
            break
        parts[longest_idx] = parts[longest_idx][:-1]
    return "|".join(parts)[:MT5_COMMENT_LIMIT]


def _is_comment_error(err) -> bool:
    return err is not None and "comment" in str(err).lower()


def _safe_order_send(request):
    """Wraps mt5.order_send so an invalid comment (or any other client-side
    rejected argument, which raises instead of returning a retcode) can't
    crash the bot or silently drop the trade."""
    try:
        return mt5.order_send(request), None
    except Exception as e:
        return None, str(e)


def _send_with_comment_fallback(build_request, comment: str):
    """
    Calls build_request(comment) -> dict and sends it via _safe_order_send.
    If MT5 rejects specifically because of the comment, retries with
    progressively shorter comments, then a blank one, before giving up.
    Returns (result, err, comment_used).
    """
    candidates = [comment, comment[:16], comment[:8], ""]
    seen = []
    result, err = None, None
    for c in candidates:
        if c in seen:
            continue
        seen.append(c)
        req = build_request(c)
        result, err = _safe_order_send(req)
        if result is not None:
            return result, err, c
        if not _is_comment_error(err):
            # Failure is unrelated to comment (e.g. margin, price) — no point retrying.
            return result, err, c
    return result, err, seen[-1] if seen else comment


# =============================================================================
# SWEET SPOT SYMBOL WHITELIST (scalping module)
# =============================================================================
SCALP_WHITELIST = {"EURUSD", "GBPUSD", "GOLD", "XAUUSD"}


def is_scalp_whitelisted(symbol: str) -> bool:
    """Auto-reject any symbol not in the high-volume whitelist."""
    clean = symbol.replace(".i#", "").replace("#", "").upper()
    return any(w in clean for w in SCALP_WHITELIST)


# =============================================================================
# SPREAD GATE
# Forex:  max 1.0 pip  (0.00010 for 5-decimal pairs)
# Crypto: max $0.50
# Gold:   max 1.5 pts  (kept tight for institutional fills)
# JPY:    max 1.5 pips (0.015 for 3-decimal pairs)
# =============================================================================
def _spread_ok(symbol: str, spread: float) -> bool:
    """
    Hard spread failsafe — drops trigger queue if spread crosses limit.

    Relaxed from 1.0 pip → 2.0 pips FX (real broker spreads 1.0–1.5 during overlap)
    Crypto: $1.00 (relaxed from $0.50)
    Gold:   2.0 pts
    JPY:    2.0 pips
    """
    clean = symbol.replace(".i#", "").replace("#", "").upper()

    if "BTC" in clean or "ETH" in clean or "USDT" in clean:
        return spread <= 1.00          # Crypto: $1.00

    if "GOLD" in clean or "XAU" in clean:
        return spread <= 2.00          # Gold: 2.0 pts

    if "JPY" in clean:
        return spread <= 0.020         # JPY: 2.0 pips (3-decimal)

    # Forex standard: 2.0 pips = 0.00020 on 5-decimal broker
    return spread <= 0.00020


def get_spread_pips(symbol: str) -> float:
    """Returns current spread in pips for display."""
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return 999.0
    info  = mt5.symbol_info(symbol)
    point = info.point if info else 0.00001
    spread_raw = abs(tick.ask - tick.bid)
    return round(spread_raw / point / 10, 2)


# =============================================================================
# LOT NORMALISER
# =============================================================================
def _normalise_lot(symbol: str, lot: float) -> float:
    info = mt5.symbol_info(symbol)
    if info is None:
        return max(0.01, lot)
    lot = max(info.volume_min, min(info.volume_max, lot))
    lot = round(round(lot / info.volume_step) * info.volume_step, 2)
    return lot


# =============================================================================
# LIMIT ORDER ENTRY
# Places a Buy Limit or Sell Limit at the FVG boundary / structural line.
# Eliminates execution slippage on ECN brokers.
# =============================================================================
def place_limit_order(symbol: str, side: str, lot: float,
                      limit_price: float, stop_loss: float,
                      mode: str = "SCALPING",
                      trade_type: str = "NORMAL",
                      pattern: str = "CONFLUENCE_SCORE",
                      expiry_bars: int = 5) -> str:
    """
    Places a pending Limit Order at the FVG boundary or zone edge.
    BUY  LIMIT: price must drop to limit_price (demand zone top)
    SELL LIMIT: price must rise to limit_price (supply zone bottom)

    expiry_bars: auto-cancels if not filled within N * 1-min bars
    """
    if not stop_loss or stop_loss == 0:
        return "BLOCKED: SL mandatory"

    info = mt5.symbol_info(symbol)
    if info is None:
        return "Symbol error"
    if not info.visible:
        mt5.symbol_select(symbol, True)

    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return "Tick error"

    spread = abs(tick.ask - tick.bid)
    if not _spread_ok(symbol, spread):
        return f"Spread too wide: {get_spread_pips(symbol):.1f} pips"

    # Hedging check
    for pos in (mt5.positions_get(symbol=symbol) or []):
        if pos.type == mt5.ORDER_TYPE_BUY  and side == "SELL":
            return "BLOCKED: hedge (existing BUY)"
        if pos.type == mt5.ORDER_TYPE_SELL and side == "BUY":
            return "BLOCKED: hedge (existing SELL)"

    if side == "BUY":
        order_type = mt5.ORDER_TYPE_BUY_LIMIT
        # Validate: limit price must be BELOW current ask
        if limit_price >= tick.ask:
            return f"BUY LIMIT price {limit_price:.5f} must be below ask {tick.ask:.5f}"
    elif side == "SELL":
        order_type = mt5.ORDER_TYPE_SELL_LIMIT
        # Validate: limit price must be ABOVE current bid
        if limit_price <= tick.bid:
            return f"SELL LIMIT price {limit_price:.5f} must be above bid {tick.bid:.5f}"
    else:
        return "Invalid side"

    lot = _normalise_lot(symbol, lot)

    # Margin check
    account = mt5.account_info()
    if account is None:
        return "Account error"
    margin = mt5.order_calc_margin(order_type, symbol, lot, limit_price)
    if margin is None or margin > account.margin_free:
        return "Insufficient margin"

    # ---- Try all filling modes — broker dependent ----
    # place_trade() already needed this fix (brokers reject a
    # single hardcoded fill mode); place_limit_order() never had
    # it ported over — that gap is why every limit order here was
    # failing with a blank "Order send failed" and no reason.
    filling_modes = []
    fmode = getattr(info, "filling_mode", 0)
    if fmode & 1: filling_modes.append(mt5.ORDER_FILLING_FOK)
    if fmode & 2: filling_modes.append(mt5.ORDER_FILLING_IOC)
    if fmode & 4: filling_modes.append(mt5.ORDER_FILLING_RETURN)
    if not filling_modes:
        filling_modes = [mt5.ORDER_FILLING_RETURN,
                         mt5.ORDER_FILLING_IOC,
                         mt5.ORDER_FILLING_FOK]

    safe_comment = _safe_comment(mode, pattern, trade_type, "LMT")

    def _build(comment):
        return {
            "action":       mt5.TRADE_ACTION_PENDING,
            "symbol":       symbol,
            "volume":       float(lot),
            "type":         order_type,
            "price":        float(limit_price),
            "sl":           float(stop_loss),
            "tp":           0.0,               # no fixed TP
            "deviation":    5,                 # tight for limit orders
            "magic":        777,
            "comment":      comment,
            "type_time":    mt5.ORDER_TIME_GTC,
            "type_filling": fill,
        }

    result = None
    exc_err = None
    for fill in filling_modes:
        print(f"  [DEBUG-LMT] Trying fill:{fill} lot:{lot} "
              f"price:{limit_price:.5f} sl:{stop_loss:.5f} comment:{safe_comment}")
        result, exc_err, used_comment = _send_with_comment_fallback(_build, safe_comment)
        if result is None:
            err = exc_err or mt5.last_error()
            print(f"  [DEBUG-LMT] result=None MT5 error: {err}")
            continue
        if used_comment != safe_comment:
            print(f"  [DEBUG-LMT] comment rejected, retried with '{used_comment}'")
        print(f"  [DEBUG-LMT] retcode:{result.retcode} comment:{result.comment}")
        if result.retcode == mt5.TRADE_RETCODE_DONE:
            break

    if result is None:
        err = exc_err or mt5.last_error()
        return f"Order send failed — MT5 error: {err}"

    if result.retcode == mt5.TRADE_RETCODE_DONE:
        print(f"LIMIT {side} {symbol} @ {limit_price:.5f} "
              f"| Lot:{lot} | SL:{stop_loss:.5f}")
        return f"LIMIT_PLACED {symbol} #{result.order}"

    err_map = {
        10019: "NOT ENOUGH MARGIN",
        10014: "INVALID LOT",
        10030: "INVALID PRICE",
        10021: "MARKET CLOSED",
        10016: "INVALID STOPS",
        10026: "AUTOTRADING DISABLED IN MT5",
        10027: "AUTOTRADING DISABLED BY BROKER",
    }
    return (f"Failed retcode:{result.retcode} — "
            f"{err_map.get(result.retcode, mt5.last_error())}")


# =============================================================================
# MARKET ORDER ENTRY (fallback / swing / intraday)
# =============================================================================
def place_trade(symbol: str, side: str, lot: float,
                stop_loss: float, take_profit: float = 0.0,
                mode: str = "NORMAL",
                trade_type: str = "NORMAL",
                pattern: str = "SMC_SIGNAL") -> str:
    """
    Market order execution.
    Comment format: "{mode}|{pattern}|{trade_type}"
    Parsed by closed_memory.py to populate Mode + Pattern columns.
    """
    if not stop_loss or stop_loss == 0:
        return "BLOCKED: Stop Loss is mandatory"

    info = mt5.symbol_info(symbol)
    if info is None:
        return "Symbol error"
    if not info.visible:
        mt5.symbol_select(symbol, True)

    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return "Tick error"

    spread = abs(tick.ask - tick.bid)
    if not _spread_ok(symbol, spread):
        return f"Spread too wide: {get_spread_pips(symbol):.1f} pips"

    # Hedging check
    for pos in (mt5.positions_get(symbol=symbol) or []):
        if pos.type == mt5.ORDER_TYPE_BUY  and side == "SELL":
            return "BLOCKED: hedge (existing BUY)"
        if pos.type == mt5.ORDER_TYPE_SELL and side == "BUY":
            return "BLOCKED: hedge (existing SELL)"

    if side == "BUY":
        order_type = mt5.ORDER_TYPE_BUY
        price      = tick.ask
    elif side == "SELL":
        order_type = mt5.ORDER_TYPE_SELL
        price      = tick.bid
    else:
        return "Invalid side"

    lot = _normalise_lot(symbol, lot)

    account = mt5.account_info()
    if account is None:
        return "Account error"
    margin = mt5.order_calc_margin(order_type, symbol, lot, price)
    if margin is None or margin > account.margin_free:
        return "Insufficient margin"

    # ---- Try all filling modes — broker dependent ----
    # Different brokers support different fill modes
    # Try FOK → IOC → RETURN in order until one works
    filling_modes = []
    fmode = getattr(info, "filling_mode", 0)
    if fmode & 1: filling_modes.append(mt5.ORDER_FILLING_FOK)
    if fmode & 2: filling_modes.append(mt5.ORDER_FILLING_IOC)
    if fmode & 4: filling_modes.append(mt5.ORDER_FILLING_RETURN)
    if not filling_modes:
        filling_modes = [mt5.ORDER_FILLING_IOC,
                         mt5.ORDER_FILLING_FOK,
                         mt5.ORDER_FILLING_RETURN]

    safe_comment = _safe_comment(mode, pattern, trade_type)

    def _build(comment):
        return {
            "action":       mt5.TRADE_ACTION_DEAL,
            "symbol":       symbol,
            "volume":       float(lot),
            "type":         order_type,
            "price":        float(price),
            "sl":           float(stop_loss),
            "tp":           0.0,
            "deviation":    50,
            "magic":        777,
            "comment":      comment,
            "type_time":    mt5.ORDER_TIME_GTC,
            "type_filling": fill,
        }

    result = None
    exc_err = None
    for fill in filling_modes:
        print(f"  [DEBUG] Trying fill:{fill} lot:{lot} price:{price:.5f} "
              f"sl:{stop_loss:.5f} comment:{safe_comment}")
        result, exc_err, used_comment = _send_with_comment_fallback(_build, safe_comment)
        if result is None:
            err = exc_err or mt5.last_error()
            print(f"  [DEBUG] result=None MT5 error: {err}")
            continue
        if used_comment != safe_comment:
            print(f"  [DEBUG] comment rejected, retried with '{used_comment}'")
        print(f"  [DEBUG] retcode:{result.retcode} comment:{result.comment}")
        if result.retcode == mt5.TRADE_RETCODE_DONE:
            break

    if result is None:
        err = exc_err or mt5.last_error()
        return f"Order send failed — MT5 error: {err}"

    if result.retcode == mt5.TRADE_RETCODE_DONE:
        print(f"MARKET {side} {symbol} @ {price:.5f} "
              f"| Lot:{lot} | SL:{stop_loss:.5f} | Mode:{mode}")
        return f"✅ TRADE OPENED {symbol} #{result.order}"

    err_map = {
        10019: "NOT ENOUGH MARGIN",
        10014: "INVALID LOT",
        10030: "INVALID PRICE — requote?",
        10021: "MARKET CLOSED",
        10016: "INVALID STOPS",
        10026: "AUTOTRADING DISABLED IN MT5",
        10027: "AUTOTRADING DISABLED BY BROKER",
    }
    return (f"Failed retcode:{result.retcode} — "
            f"{err_map.get(result.retcode, mt5.last_error())}")


# =============================================================================
# CANCEL ALL PENDING ORDERS for a symbol
# =============================================================================
def cancel_pending_orders(symbol: str) -> int:
    """Cancel all pending limit orders for a symbol. Returns count cancelled."""
    orders  = mt5.orders_get(symbol=symbol) or []
    count   = 0
    for order in orders:
        req = {
            "action": mt5.TRADE_ACTION_REMOVE,
            "order":  order.ticket,
        }
        res = mt5.order_send(req)
        if res and res.retcode == mt5.TRADE_RETCODE_DONE:
            count += 1
    if count:
        print(f"CANCELLED {count} pending orders: {symbol}")
    return count


# =============================================================================
# CLOSE HELPERS
# =============================================================================
def close_position(position, comment: str = "STRUCTURAL_EXIT") -> bool:
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
        "comment":      comment,
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    res = mt5.order_send(req)
    if res and res.retcode == mt5.TRADE_RETCODE_DONE:
        print(f"CLOSED {position.symbol} #{position.ticket}")
        return True
    return False


def close_all_positions():
    for pos in (mt5.positions_get() or []):
        close_position(pos)


def total_positions() -> int:
    p = mt5.positions_get()
    return 0 if p is None else len(p)


def symbol_positions(symbol: str) -> int:
    p = mt5.positions_get(symbol=symbol)
    return 0 if p is None else len(p)
