# =========================================
# risk.py — Dynamic Risk & Lot Sizing Engine
# EA BOT v2 | SRD Sections 1, 3, 6
#
# Group 1 (Gold/Silver): max 1.5% risk/trade
# Group 2/3 (Forex):     max 2.0% risk/trade
#
# Scalping caps scale with balance.
# Volatile expansion → halve risk automatically.
# =========================================

import MetaTrader5 as mt5

# ---- SYMBOL GROUPS ----
GROUP_1_METALS  = {"XAUUSD", "XAGUSD", "GOLD"}
GROUP_2_FOREX   = {
    "EURUSD", "GBPUSD", "USDJPY", "USDCAD",
    "AUDUSD", "EURGBP", "GBPJPY", "EURJPY", "USDCHF", "NZDUSD"
}

# ---- BASE RISK BY GROUP ----
BASE_RISK = {
    "GROUP1": 1.5,   # Metals
    "GROUP2": 2.0,   # Forex
}


def symbol_group(symbol: str) -> str:
    clean = symbol.replace(".i#", "").replace("#", "").upper()
    for k in GROUP_1_METALS:
        if k in clean:
            return "GROUP1"
    return "GROUP2"


# ---- DRAWDOWN PROTECTION ----
def dd_protection() -> dict:
    account = mt5.account_info()
    if account is None:
        return {"status": "NORMAL", "risk_mult": 1.0, "max_trades": 3, "dd": 0.0}

    balance = account.balance
    equity  = account.equity

    if balance <= 0:
        return {"status": "DANGER", "risk_mult": 0.0, "max_trades": 0, "dd": 100.0}

    dd = round(((balance - equity) / balance) * 100, 2)
    print(f"📉 DD: {dd}%")

    if dd < 3:  return {"status": "NORMAL",    "risk_mult": 1.00, "max_trades": 6, "dd": dd}
    if dd < 5:  return {"status": "REDUCE",    "risk_mult": 0.75, "max_trades": 4, "dd": dd}
    if dd < 8:  return {"status": "ELITE",     "risk_mult": 0.50, "max_trades": 3, "dd": dd}
    if dd < 15: return {"status": "DANGER",    "risk_mult": 0.25, "max_trades": 2, "dd": dd}
    return          {"status": "TEST_MODE", "risk_mult": 0.10, "max_trades": 1, "dd": dd}


# ---- PIPELINE POSITION CAPS ----
def pipeline_caps(mode: str, balance: float) -> dict:
    """
    Returns {"pair_max": int, "portfolio_max": int}
    for the given mode.

    Per-pair caps (requested): SCALPING=3, INTRADAY=2, SWING=2 — same time,
    same pair, enforced per mode. portfolio_max is pair_max * number of
    symbols traded so one very active pair can't itself block other pairs.
    """
    if mode == "SCALPING":
        return {"pair_max": 3, "portfolio_max": 3 * 9}
    if mode == "INTRADAY":
        return {"pair_max": 2, "portfolio_max": 2 * 9}
    if mode == "SWING":
        return {"pair_max": 2, "portfolio_max": 2 * 9}
    if mode == "RED_NEWS":
        return {"pair_max": 1, "portfolio_max": 2}
    return {"pair_max": 1, "portfolio_max": 3}


# ---- LOT CALCULATOR ----
def calculate_lot(symbol: str, balance: float,
                  risk_percent: float, sl_distance: float) -> float:
    info = mt5.symbol_info(symbol)
    if info is None or sl_distance <= 0:
        return info.volume_min if info else 0.01

    contract_size     = info.trade_contract_size
    point             = info.point
    pip_value_per_lot = contract_size * point
    sl_in_pips        = sl_distance / point
    risk_amount       = balance * (risk_percent / 100)

    if pip_value_per_lot <= 0 or sl_in_pips <= 0:
        return info.volume_min

    lot = risk_amount / (sl_in_pips * pip_value_per_lot)
    lot = max(info.volume_min, min(info.volume_max, lot))
    lot = round(round(lot / info.volume_step) * info.volume_step, 2)
    return lot


# ---- DYNAMIC RISK ENGINE ----
def dynamic_risk_engine(symbol: str, session: str, balance: float,
                        mode: str, sl_distance: float,
                        ai_data=None, volatile_expansion=False) -> dict:
    """
    Full risk calculation per SRD Sections 3 & 6.

    NOTE ON volatile_expansion: the SRD-6 "halve risk during volatile
    expansion" rule is applied by the CALLER via regime["risk_multiplier"]
    (main.py's execute_trade()/scalping_thread(), which multiply this
    function's returned lot by regime["risk_multiplier"] == 0.5 during
    VOLATILE_EXPANSION). This function used to ALSO halve risk_percent
    internally when volatile_expansion=True, so the two multiplied
    together (0.5 x 0.5 = 0.25x) and quartered risk instead of halving
    it as documented. Fixed: this function no longer changes risk_percent
    based on volatile_expansion — it's accepted only so callers don't
    need to change their call signature, and is used for logging only.
    The single source of truth for the volatility risk reduction is now
    the caller's regime["risk_multiplier"].
    """
    group        = symbol_group(symbol)
    risk_percent = BASE_RISK[group]

    if volatile_expansion:
        print(f"⚡ VOLATILE EXPANSION — risk reduction applied once, by caller's regime risk_multiplier (not duplicated here)")

    # Mode multiplier
    multiplier = {
        "SCALPING":  1.0,
        "INTRADAY":  1.0,
        "SWING":     1.0,
        "RED_NEWS":  0.5,
    }.get(mode, 1.0)
    risk_percent *= multiplier

    # Session adjustment
    if session == "ASIAN":
        risk_percent *= 0.80

    # AI adjustment
    if ai_data:
        wr     = ai_data.get("win_rate", 50)
        profit = ai_data.get("total_profit", 0)
        if wr < 45 or profit < 0:
            risk_percent *= 0.70
            print("⚠️ AI: Defensive mode")
        elif wr > 65 and profit > 0:
            risk_percent *= 1.10
            print("🚀 AI: Aggressive mode")

    # DD protection
    dd_info      = dd_protection()
    risk_percent *= dd_info["risk_mult"]
    risk_percent  = round(max(0.1, min(risk_percent, BASE_RISK[group])), 2)

    # Calculate raw lot
    lot = calculate_lot(symbol, balance, risk_percent, sl_distance)

    # Hard safety caps (never exceed even if math allows)
    is_metal = group == "GROUP1"
    if balance <= 500:
        hard_cap = 0.05 if is_metal else 0.20
    elif balance <= 1000:
        hard_cap = 0.10 if is_metal else 0.50
    elif balance <= 5000:
        hard_cap = 0.20 if is_metal else 1.00
    else:
        hard_cap = 0.50 if is_metal else 2.00

    lot = round(max(0.01, min(lot, hard_cap)), 2)
    print(f"💰 Risk: {risk_percent}% | Lot: {lot} | Group: {group} | DD: {dd_info['status']}")

    return {
        "risk_percent":  risk_percent,
        "lot":           lot,
        "dd_status":     dd_info["status"],
        "dd_pct":        dd_info["dd"],
        "group":         group,
    }
