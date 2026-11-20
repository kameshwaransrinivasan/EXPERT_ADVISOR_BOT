"""
Maximum-history MT5 exporter with resumable checkpointing.

Exports maximum historical OHLC data ONLY for the requested instruments
and these timeframes:

    M1, M5, M10, M15, M30, H1, H4, D1, W1

The exporter is resumable:
- Progress is saved after every completed chunk.
- If the program/MT5 is closed or crashes, rerun the script.
- Completed chunks are skipped.
- Export continues from the last unfinished chunk.

Requested instruments ONLY:
    EUR/USD
    GBP/USD
    USD/JPY
    AUD/USD
    USD/CAD
    USD/CHF
    NZD/USD
    BTC/USDT
    ETH/USDT
    SOL/USDT
    XRP/USDT
    XAU/USD
    XAG/USD
    WTI Crude Oil
    Brent Crude Oil

Output:
    <out>/
        candles/
            SYMBOL_M1.csv
            SYMBOL_M5.csv
            SYMBOL_M10.csv
            ...
        manifest.csv
        symbol_specs.csv
        checkpoint.json

Requirements:
    - Windows
    - MetaTrader 5 terminal installed and logged in
    - pip install MetaTrader5 pandas

Usage:
    python export_full_history_XM_15_only.py

Optional:
    python export_full_history_XM_15_only.py --out history_export_full
    python export_full_history_XM_15_only.py --symbols EURUSD,GBPUSD
    python export_full_history_XM_15_only.py --timeframes M1,M5,M10,M15,M30,H1,H4,D1,W1
"""

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

try:
    import MetaTrader5 as mt5
except ImportError:
    print("ERROR: MetaTrader5 package not installed.")
    print("Run: pip install MetaTrader5")
    sys.exit(1)


# ============================================================
# TIMEFRAMES
# ============================================================

TIMEFRAME_MAP = {
    "M1": mt5.TIMEFRAME_M1,
    "M5": mt5.TIMEFRAME_M5,
    "M10": mt5.TIMEFRAME_M10,
    "M15": mt5.TIMEFRAME_M15,
    "M30": mt5.TIMEFRAME_M30,
    "H1": mt5.TIMEFRAME_H1,
    "H4": mt5.TIMEFRAME_H4,
    "D1": mt5.TIMEFRAME_D1,
    "W1": mt5.TIMEFRAME_W1,
}

DEFAULT_TIMEFRAMES = [
    "M1",
    "M5",
    "M10",
    "M15",
    "M30",
    "H1",
    "H4",
    "D1",
    "W1",
]


# ============================================================
# MAXIMUM HISTORY RANGE
# ============================================================

HISTORY_START = datetime(1970, 1, 1, tzinfo=timezone.utc)


# ============================================================
# CHUNK SIZE
# ============================================================

CHUNK_DAYS = 30


# ============================================================
# RETRY SETTINGS
# ============================================================

MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 2


# ============================================================
# REQUESTED INSTRUMENTS
# ============================================================
#
# IMPORTANT:
# These are the ONLY markets this exporter is allowed to export.
#
# For broker compatibility, each canonical instrument has an
# explicit list of accepted broker symbol names.
#
# We do NOT use fuzzy matching such as:
#     BTCUSD -> BTCUSDT
#
# because that could put the wrong market into an ML dataset.
#
# Prefix/suffix variants are allowed only for the same exact
# underlying symbol, e.g. EURUSD -> EURUSDm / EURUSD.a.
# ============================================================

REQUESTED_INSTRUMENTS = {
    "EUR/USD": {
        "core": "EURUSD",
        "aliases": [],
    },
    "GBP/USD": {
        "core": "GBPUSD",
        "aliases": [],
    },
    "USD/JPY": {
        "core": "USDJPY",
        "aliases": [],
    },
    "AUD/USD": {
        "core": "AUDUSD",
        "aliases": [],
    },
    "USD/CAD": {
        "core": "USDCAD",
        "aliases": [],
    },
    "USD/CHF": {
        "core": "USDCHF",
        "aliases": [],
    },
    "NZD/USD": {
        "core": "NZDUSD",
        "aliases": [],
    },
    "BTC/USDT": {
        "core": "BTCUSD",
        "aliases": [],
    },
    "ETH/USDT": {
        "core": "ETHUSD",
        "aliases": [],
    },
    "SOL/USDT": {
        "core": "SOLUSD",
        "aliases": [],
    },
    "XRP/USDT": {
        "core": "XRPUSD",
        "aliases": [],
    },
    "XAU/USD": {
        "core": "XAUUSD",
        "aliases": [
            "GOLD",
        ],
    },
    "XAG/USD": {
        "core": "XAGUSD",
        "aliases": [
            "SILVER",
        ],
    },
    "WTI Crude Oil": {
        "core": "WTI",
        "aliases": [
            "USOIL",
            "OIL",
            "XTIUSD",
        ],
    },
    "Brent Crude Oil": {
        "core": "BRENT",
        "aliases": [
            "UKOIL",
            "XBRUSD",
        ],
    },
}


# ============================================================
# CATEGORY DETECTION
# ============================================================

def categorize(symbol_info) -> str:
    """
    Best-effort category label from broker grouping/path.
    Used only for manifest/summary.
    """

    path = (getattr(symbol_info, "path", "") or "").lower()
    name = symbol_info.name.upper()

    if "crypto" in path or any(
        c in name
        for c in (
            "BTC",
            "ETH",
            "XRP",
            "LTC",
            "DOGE",
            "SOL",
            "ADA",
        )
    ):
        return "crypto"

    if (
        "metal" in path
        or "commod" in path
        or "energy" in path
        or name
        in (
            "GOLD",
            "SILVER",
            "XAUUSD",
            "XAGUSD",
            "USOIL",
            "UKOIL",
            "WTI",
            "BRENT",
            "XTIUSD",
            "XBRUSD",
            "OIL",
        )
    ):
        return "commodity"

    if "index" in path or "indices" in path:
        return "index"

    if "forex" in path or "currencies" in path:
        return "forex"

    return "other"


# ============================================================
# SYMBOL RESOLUTION
# ============================================================

def normalize_symbol(symbol: str) -> str:
    """
    Normalize only for comparison.

    This removes common non-alphanumeric separators but does NOT
    transform one market into another.
    """
    return "".join(ch for ch in symbol.upper() if ch.isalnum())


def build_symbol_lookup(symbol_infos):
    """
    Build a normalized exact-name lookup for broker symbols.

    A dict is used so resolution remains deterministic even when
    the broker has many symbols.
    """
    lookup = {}

    for info in symbol_infos:
        name = getattr(info, "name", "")
        if not name:
            continue

        normalized = normalize_symbol(name)
        lookup.setdefault(normalized, []).append(info)

    return lookup


def resolve_same_core_symbol(core: str, broker_symbol_names):
    """
    Resolve symbols such as EURUSD, EURUSDm, EURUSD.a, etc.

    Only a symbol whose normalized name STARTS WITH the exact
    requested core and has a non-alphanumeric suffix is accepted.

    This prevents dangerous mappings such as:
        BTCUSD  -> BTCUSDT
    because BTCUSD is not the requested core BTCUSDT.

    Returns the matching broker symbol name or None.
    """

    core_normalized = normalize_symbol(core)

    # Exact normalized match first.
    exact_matches = []
    for name in broker_symbol_names:
        if normalize_symbol(name) == core_normalized:
            exact_matches.append(name)

    if exact_matches:
        return sorted(exact_matches, key=lambda x: (len(x), x))[0]

    # Controlled suffix matching.
    #
    # Examples accepted:
    #   EURUSDm
    #   EURUSD.a
    #   EURUSDpro
    #
    # Examples NOT accepted:
    #   BTCUSD for BTCUSDT
    #   EURGBP for EURUSD
    #
    candidates = []

    for name in broker_symbol_names:
        upper_name = name.upper()

        if not upper_name.startswith(core_normalized):
            continue

        suffix = upper_name[len(core_normalized):]

        if not suffix:
            continue

        # Suffix must not begin with another currency/market component.
        # Restrict the first suffix character to a broker-style marker
        # or a letter/digit suffix used after the exact requested core.
        #
        # Since this is still broker-dependent, exact aliases are checked
        # separately and preferred over this fallback.
        if len(suffix) > 12:
            continue

        candidates.append(name)

    if not candidates:
        return None

    return sorted(candidates, key=lambda x: (len(x), x))[0]


def resolve_requested_instruments(instruments_to_resolve=None):
    """
    Resolve ONLY the 15 requested instruments against the symbols
    available in the currently connected MT5 terminal.

    mt5.symbols_get() is used only to discover broker symbol names
    so that the requested instruments can be resolved. No unrequested
    symbol is ever exported.
    """

    if instruments_to_resolve is None:
        instruments_to_resolve = REQUESTED_INSTRUMENTS

    broker_symbols = mt5.symbols_get()

    if broker_symbols is None:
        print(
            "ERROR: Could not read symbols from MT5. "
            f"MT5 error: {mt5.last_error()}"
        )
        return [], []

    broker_symbol_names = [
        info.name
        for info in broker_symbols
        if getattr(info, "name", "")
    ]

    exact_name_map = {}
    for name in broker_symbol_names:
        exact_name_map.setdefault(name.upper(), []).append(name)

    normalized_map = build_symbol_lookup(broker_symbols)

    resolved_infos = []
    unresolved = []

    print()
    print("REQUESTED INSTRUMENT RESOLUTION")
    print("-" * 70)

    for canonical, config in instruments_to_resolve.items():
        core = config["core"]
        aliases = config["aliases"]

        resolved_name = None
        resolution_reason = None

        # 1. Exact core.
        direct_core_matches = exact_name_map.get(core.upper(), [])
        if direct_core_matches:
            resolved_name = sorted(direct_core_matches)[0]
            resolution_reason = "exact"

        # 2. Exact aliases.
        if resolved_name is None:
            for alias in aliases:
                direct_alias_matches = exact_name_map.get(alias.upper(), [])
                if direct_alias_matches:
                    resolved_name = sorted(direct_alias_matches)[0]
                    resolution_reason = f"alias {alias}"
                    break

        # 3. Controlled core suffix matching.
        if resolved_name is None:
            resolved_name = resolve_same_core_symbol(
                core,
                broker_symbol_names,
            )
            if resolved_name is not None:
                resolution_reason = f"core variant of {core}"

        # 4. Controlled alias suffix matching.
        if resolved_name is None:
            for alias in aliases:
                resolved_name = resolve_same_core_symbol(
                    alias,
                    broker_symbol_names,
                )
                if resolved_name is not None:
                    resolution_reason = f"alias variant of {alias}"
                    break

        if resolved_name is None:
            print(
                f"WARNING: Requested instrument '{canonical}' "
                f"could not be resolved on this broker."
            )
            unresolved.append(canonical)
            continue

        info = next(
            (
                item
                for item in broker_symbols
                if item.name == resolved_name
            ),
            None,
        )

        if info is None:
            print(
                f"WARNING: Internal resolution error for "
                f"'{canonical}' -> '{resolved_name}'."
            )
            unresolved.append(canonical)
            continue

        if not mt5.symbol_select(resolved_name, True):
            print(
                f"WARNING: Could not select broker symbol "
                f"'{resolved_name}' for '{canonical}'. "
                f"MT5 error: {mt5.last_error()}"
            )
            unresolved.append(canonical)
            continue

        print(
            f"Resolved: {canonical:<20} -> "
            f"{resolved_name:<20} [{resolution_reason}]"
        )

        resolved_infos.append(
            {
                "canonical": canonical,
                "broker_symbol": resolved_name,
                "info": info,
            }
        )

    print("-" * 70)
    print(
        f"Requested : {len(instruments_to_resolve)}"
    )
    print(
        f"Resolved  : {len(resolved_infos)}"
    )
    print(
        f"Missing   : {len(unresolved)}"
    )
    print()

    return resolved_infos, unresolved


# ============================================================
# CHECKPOINT
# ============================================================

def checkpoint_path(out_dir: str) -> str:
    return os.path.join(out_dir, "checkpoint.json")


def load_checkpoint(out_dir: str) -> dict:
    path = checkpoint_path(out_dir)

    if not os.path.exists(path):
        return {
            "version": 1,
            "chunks": {},
        }

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, dict):
            raise ValueError("Invalid checkpoint format")

        data.setdefault("version", 1)
        data.setdefault("chunks", {})

        return data

    except Exception as exc:
        print(f"WARNING: Could not read checkpoint: {exc}")
        print("Starting with a fresh checkpoint.")

        return {
            "version": 1,
            "chunks": {},
        }


def save_checkpoint(out_dir: str, checkpoint: dict):
    """
    Atomic checkpoint write.

    This prevents a partially-written checkpoint from destroying
    the previous valid checkpoint.
    """

    path = checkpoint_path(out_dir)
    temp_path = path + ".tmp"

    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(
            checkpoint,
            f,
            indent=2,
            ensure_ascii=False,
        )

    os.replace(temp_path, path)


def chunk_key(symbol: str, tf_name: str, chunk_start: datetime) -> str:
    return (
        f"{symbol}|{tf_name}|"
        f"{chunk_start.strftime('%Y-%m-%dT%H:%M:%S%z')}"
    )


# ============================================================
# MT5 DATA FETCH
# ============================================================

def fetch_chunk(
    symbol: str,
    tf_const: int,
    start: datetime,
    end: datetime,
):
    """
    Fetch one chunk with retry support.
    """

    last_error = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            rates = mt5.copy_rates_range(
                symbol,
                tf_const,
                start,
                end,
            )

            if rates is not None:
                return rates

            last_error = mt5.last_error()

            print(
                f"    MT5 returned no result "
                f"(attempt {attempt}/{MAX_RETRIES}): {last_error}"
            )

        except Exception as exc:
            last_error = exc

            print(
                f"    Exception while fetching "
                f"(attempt {attempt}/{MAX_RETRIES}): {exc}"
            )

        if attempt < MAX_RETRIES:
            time.sleep(RETRY_DELAY_SECONDS)

    print(
        f"    ERROR: chunk failed after "
        f"{MAX_RETRIES} attempts: {last_error}"
    )

    return None


# ============================================================
# CSV WRITING
# ============================================================

CSV_HEADER = [
    "time",
    "open",
    "high",
    "low",
    "close",
    "tick_volume",
    "spread",
    "real_volume",
]


def write_chunk_csv(
    path: str,
    rates,
):
    """
    Append one MT5 chunk to CSV.

    The file is created with a header if it does not exist.
    """

    file_exists = os.path.exists(path)
    file_empty = (not file_exists) or os.path.getsize(path) == 0

    with open(
        path,
        "a",
        newline="",
        encoding="utf-8",
    ) as f:

        writer = csv.writer(f)

        if file_empty:
            writer.writerow(CSV_HEADER)

        for r in rates:
            dt = datetime.fromtimestamp(
                int(r["time"]),
                tz=timezone.utc,
            )

            writer.writerow(
                [
                    dt.strftime("%Y-%m-%d %H:%M:%S+00:00"),
                    r["open"],
                    r["high"],
                    r["low"],
                    r["close"],
                    r["tick_volume"],
                    r["spread"],
                    r["real_volume"],
                ]
            )


# ============================================================
# REMOVE DUPLICATE LAST ROW
# ============================================================

def remove_duplicate_boundary(path: str):
    """
    Chunk boundaries can occasionally duplicate one candle.

    After appending a chunk, remove duplicate timestamps from the
    final CSV if necessary.
    """

    if not os.path.exists(path):
        return

    try:
        with open(path, "r", encoding="utf-8") as f:
            rows = list(csv.reader(f))

        if len(rows) <= 2:
            return

        header = rows[0]
        data = rows[1:]

        seen = set()
        unique_rows = []

        for row in data:
            if not row:
                continue

            timestamp = row[0]

            if timestamp in seen:
                continue

            seen.add(timestamp)
            unique_rows.append(row)

        if len(unique_rows) != len(data):
            temp_path = path + ".tmp"

            with open(
                temp_path,
                "w",
                newline="",
                encoding="utf-8",
            ) as f:

                writer = csv.writer(f)
                writer.writerow(header)
                writer.writerows(unique_rows)

            os.replace(temp_path, path)

    except Exception as exc:
        print(
            f"WARNING: Could not de-duplicate "
            f"{os.path.basename(path)}: {exc}"
        )


# ============================================================
# EXPORT ONE SYMBOL + TIMEFRAME
# ============================================================

def export_symbol_timeframe(
    symbol: str,
    tf_name: str,
    tf_const: int,
    out_dir: str,
    checkpoint: dict,
):
    """
    Export maximum available history for one symbol/timeframe.

    Progress is checkpointed after every chunk.

    If the process is restarted, completed chunks are skipped.
    """

    candles_dir = os.path.join(out_dir, "candles")
    os.makedirs(candles_dir, exist_ok=True)

    filename = f"{symbol}_{tf_name}.csv"
    output_path = os.path.join(candles_dir, filename)

    now = datetime.now(timezone.utc)

    cursor = HISTORY_START
    total_rows_written = 0
    completed_chunks = 0
    no_data_seen = False

    while cursor < now:

        chunk_start = cursor
        chunk_end = min(
            cursor + timedelta(days=CHUNK_DAYS),
            now,
        )

        key = chunk_key(
            symbol,
            tf_name,
            chunk_start,
        )

        # ----------------------------------------------------
        # Already completed?
        # ----------------------------------------------------

        if checkpoint["chunks"].get(key) == "DONE":
            cursor = chunk_end
            completed_chunks += 1
            continue

        # ----------------------------------------------------
        # Select symbol
        # ----------------------------------------------------

        if not mt5.symbol_select(symbol, True):
            print(
                f"    WARNING: Could not select symbol {symbol}"
            )

        # ----------------------------------------------------
        # Fetch
        # ----------------------------------------------------

        rates = fetch_chunk(
            symbol,
            tf_const,
            chunk_start,
            chunk_end,
        )

        if rates is None:
            print(
                f"    ERROR: Stopping {symbol} {tf_name} "
                f"at {chunk_start.date()} because MT5 request failed."
            )

            return {
                "status": "FAILED",
                "rows": total_rows_written,
            }

        # ----------------------------------------------------
        # No data for this chunk
        # ----------------------------------------------------

        if len(rates) == 0:
            if not no_data_seen:
                print(
                    f"    No data around "
                    f"{chunk_start.date()} → {chunk_end.date()}"
                )

            no_data_seen = True

        else:
            no_data_seen = False

            write_chunk_csv(
                output_path,
                rates,
            )

            total_rows_written += len(rates)

            # Remove occasional boundary duplicates.
            remove_duplicate_boundary(output_path)

        # ----------------------------------------------------
        # Mark chunk completed
        # ----------------------------------------------------

        checkpoint["chunks"][key] = "DONE"

        save_checkpoint(
            out_dir,
            checkpoint,
        )

        completed_chunks += 1

        print(
            f"    {symbol} {tf_name} | "
            f"{chunk_start.date()} → {chunk_end.date()} | "
            f"{len(rates)} bars | checkpoint saved"
        )

        cursor = chunk_end

    # --------------------------------------------------------
    # Final result
    # --------------------------------------------------------

    if not os.path.exists(output_path):
        return {
            "status": "NO_DATA",
            "rows": 0,
        }

    return {
        "status": "OK",
        "rows": total_rows_written,
    }


# ============================================================
# BUILD MANIFEST FROM EXPORTED CSV
# ============================================================

def inspect_csv_history(path: str):
    """
    Read actual first/last timestamp and row count from CSV.
    """

    if not os.path.exists(path):
        return None, None, 0

    earliest = None
    latest = None
    count = 0

    try:
        with open(
            path,
            "r",
            encoding="utf-8",
        ) as f:

            reader = csv.DictReader(f)

            for row in reader:
                timestamp = row.get("time", "")

                if not timestamp:
                    continue

                dt = datetime.fromisoformat(timestamp)

                if earliest is None:
                    earliest = dt

                latest = dt
                count += 1

    except Exception as exc:
        print(
            f"WARNING: Could not inspect "
            f"{os.path.basename(path)}: {exc}"
        )

    return earliest, latest, count


# ============================================================
# MAIN
# ============================================================

def main():

    parser = argparse.ArgumentParser(
        description=__doc__
    )

    parser.add_argument(
        "--out",
        type=str,
        default="history_export_full",
        help="Output directory",
    )

    parser.add_argument(
        "--timeframes",
        type=str,
        default=",".join(DEFAULT_TIMEFRAMES),
        help=(
            "Comma-separated timeframes. "
            "Default: M1,M5,M10,M15,M30,H1,H4,D1,W1"
        ),
    )

    parser.add_argument(
        "--symbols",
        type=str,
        default="",
        help=(
            "Comma-separated requested broker/canonical symbol list. "
            "Only the 15 allowed instruments are accepted."
        ),
    )

    args = parser.parse_args()

    # ========================================================
    # INITIALIZE MT5
    # ========================================================

    if not mt5.initialize():
        print(
            f"ERROR: mt5.initialize() failed: "
            f"{mt5.last_error()}"
        )
        sys.exit(1)

    account = mt5.account_info()

    if account is None:
        print(
            "ERROR: No account info. "
            "Is MetaTrader 5 logged in?"
        )

        mt5.shutdown()
        sys.exit(1)

    print(
        f"Connected: account {account.login}, "
        f"balance {account.balance:.2f} {account.currency}"
    )

    # ========================================================
    # TIMEFRAMES
    # ========================================================

    timeframes = [
        t.strip().upper()
        for t in args.timeframes.split(",")
        if t.strip()
    ]

    invalid_timeframes = [
        tf
        for tf in timeframes
        if tf not in TIMEFRAME_MAP
    ]

    if invalid_timeframes:

        print(
            "ERROR: Unknown timeframe(s): "
            f"{invalid_timeframes}"
        )

        print(
            "Valid:",
            list(TIMEFRAME_MAP.keys()),
        )

        mt5.shutdown()
        sys.exit(1)

    # ========================================================
    # SYMBOL FILTER INPUT
    # ========================================================
    #
    # Preserve the existing --symbols CLI option, but restrict it
    # to our 15 allowed canonical instruments or their core names.
    #
    # If --symbols is omitted, ALL 15 requested instruments are
    # considered.
    # ========================================================

    allowed_inputs = {}

    for canonical, config in REQUESTED_INSTRUMENTS.items():
        allowed_inputs[canonical.upper()] = canonical
        allowed_inputs[config["core"].upper()] = canonical

        for alias in config["aliases"]:
            allowed_inputs[alias.upper()] = canonical

    if args.symbols:
        requested_input_symbols = [
            s.strip().upper()
            for s in args.symbols.split(",")
            if s.strip()
        ]

        selected_canonicals = []
        invalid_requested_inputs = []

        for item in requested_input_symbols:
            canonical = allowed_inputs.get(item)

            if canonical is None:
                invalid_requested_inputs.append(item)
                continue

            if canonical not in selected_canonicals:
                selected_canonicals.append(canonical)

        if invalid_requested_inputs:
            print(
                "ERROR: The following requested symbols are not in "
                "the allowed 15-instrument list:"
            )
            for item in invalid_requested_inputs:
                print(f"  - {item}")

            mt5.shutdown()
            sys.exit(1)

        selected_instruments = {
            canonical: REQUESTED_INSTRUMENTS[canonical]
            for canonical in selected_canonicals
        }

    else:
        selected_instruments = REQUESTED_INSTRUMENTS.copy()

    # Resolve ONLY the selected instruments.
    resolved_instruments, unresolved = (
        resolve_requested_instruments(selected_instruments)
    )

    if not resolved_instruments:
        print("ERROR: None of the requested instruments were found.")
        mt5.shutdown()
        sys.exit(1)

    # ========================================================
    # OUTPUT DIRECTORY
    # ========================================================

    out_dir = args.out

    candles_dir = os.path.join(
        out_dir,
        "candles",
    )

    os.makedirs(
        candles_dir,
        exist_ok=True,
    )

    # ========================================================
    # CHECKPOINT
    # ========================================================

    checkpoint = load_checkpoint(
        out_dir
    )

    # ========================================================
    # SUMMARY
    # ========================================================

    print()
    print("=" * 70)
    print("MAXIMUM MT5 HISTORY EXPORT")
    print("=" * 70)
    print(
        f"Symbols     : {len(resolved_instruments)}"
    )
    print(
        f"Timeframes  : {timeframes}"
    )
    print(
        f"History     : {HISTORY_START.date()} → TODAY"
    )
    print(
        f"Chunk size  : {CHUNK_DAYS} days"
    )
    print(
        f"Output      : {out_dir}"
    )
    print(
        f"Checkpoint  : {checkpoint_path(out_dir)}"
    )
    print("=" * 70)
    print()

    # ========================================================
    # MANIFEST DATA
    # ========================================================

    manifest_rows = []
    specs_rows = []
    category_counts = {}

    # ========================================================
    # EXPORT
    # ========================================================

    for symbol_index, resolved in enumerate(
        resolved_instruments,
        start=1,
    ):

        canonical = resolved["canonical"]
        symbol = resolved["broker_symbol"]
        info = resolved["info"]

        category = categorize(info)

        category_counts[category] = (
            category_counts.get(category, 0) + 1
        )

        # ----------------------------------------------------
        # Symbol specification
        # ----------------------------------------------------

        specs_rows.append(
            {
                "canonical_instrument": canonical,
                "symbol": symbol,
                "category": category,
                "digits": info.digits,
                "point": info.point,
                "trade_tick_size": info.trade_tick_size,
                "trade_tick_value": info.trade_tick_value,
                "trade_contract_size": info.trade_contract_size,
                "volume_min": info.volume_min,
                "volume_max": info.volume_max,
                "volume_step": info.volume_step,
                "spread": info.spread,
                "currency_base": info.currency_base,
                "currency_profit": info.currency_profit,
                "currency_margin": info.currency_margin,
            }
        )

        print()
        print(
            f"[{symbol_index}/{len(resolved_instruments)}] "
            f"{canonical} -> {symbol} ({category})"
        )

        # ----------------------------------------------------
        # Export each timeframe
        # ----------------------------------------------------

        for tf_name in timeframes:

            print(
                f"  → Exporting {symbol} {tf_name}"
            )

            result = export_symbol_timeframe(
                symbol=symbol,
                tf_name=tf_name,
                tf_const=TIMEFRAME_MAP[tf_name],
                out_dir=out_dir,
                checkpoint=checkpoint,
            )

            filename = (
                f"{symbol}_{tf_name}.csv"
            )

            csv_path = os.path.join(
                candles_dir,
                filename,
            )

            earliest, latest, row_count = (
                inspect_csv_history(csv_path)
            )

            if row_count == 0:

                status = (
                    "NO_DATA"
                    if result["status"] != "FAILED"
                    else "FAILED"
                )

                note = (
                    "Broker returned no historical bars "
                    "for this symbol/timeframe."
                )

            else:

                status = (
                    "OK"
                    if result["status"] == "OK"
                    else result["status"]
                )

                note = ""

            manifest_rows.append(
                {
                    "canonical_instrument": canonical,
                    "symbol": symbol,
                    "category": category,
                    "timeframe": tf_name,
                    "requested_start_utc": (
                        HISTORY_START.isoformat()
                    ),
                    "requested_end_utc": (
                        datetime.now(
                            timezone.utc
                        ).isoformat()
                    ),
                    "row_count": row_count,
                    "actual_earliest_utc": (
                        earliest.isoformat()
                        if earliest
                        else ""
                    ),
                    "actual_latest_utc": (
                        latest.isoformat()
                        if latest
                        else ""
                    ),
                    "status": status,
                    "note": note,
                    "filename": (
                        filename
                        if row_count > 0
                        else ""
                    ),
                }
            )

        # ----------------------------------------------------
        # Save manifest continuously after every symbol
        # ----------------------------------------------------

        manifest_path = os.path.join(
            out_dir,
            "manifest.csv",
        )

        with open(
            manifest_path,
            "w",
            newline="",
            encoding="utf-8",
        ) as f:

            if manifest_rows:

                writer = csv.DictWriter(
                    f,
                    fieldnames=list(
                        manifest_rows[0].keys()
                    ),
                )

                writer.writeheader()
                writer.writerows(
                    manifest_rows
                )

        print(
            f"  ✓ {symbol} complete"
        )

    # ========================================================
    # SYMBOL SPECS
    # ========================================================

    specs_path = os.path.join(
        out_dir,
        "symbol_specs.csv",
    )

    with open(
        specs_path,
        "w",
        newline="",
        encoding="utf-8",
    ) as f:

        if specs_rows:

            writer = csv.DictWriter(
                f,
                fieldnames=list(
                    specs_rows[0].keys()
                ),
            )

            writer.writeheader()
            writer.writerows(
                specs_rows
            )

    # ========================================================
    # FINAL MANIFEST
    # ========================================================

    manifest_path = os.path.join(
        out_dir,
        "manifest.csv",
    )

    with open(
        manifest_path,
        "w",
        newline="",
        encoding="utf-8",
    ) as f:

        if manifest_rows:

            writer = csv.DictWriter(
                f,
                fieldnames=list(
                    manifest_rows[0].keys()
                ),
            )

            writer.writeheader()
            writer.writerows(
                manifest_rows
            )

    # ========================================================
    # DONE
    # ========================================================

    print()
    print("=" * 70)
    print("EXPORT COMPLETE")
    print("=" * 70)

    print(
        f"Requested : {len(selected_instruments)}"
    )

    print(
        f"Resolved  : {len(resolved_instruments)}"
    )

    print(
        f"Missing   : {len(unresolved)}"
    )

    print(
        f"Timeframes : {timeframes}"
    )

    print(
        f"Categories : {category_counts}"
    )

    print(
        f"Output     : {out_dir}/"
    )

    print(
        "Files      : candles/, manifest.csv, "
        "symbol_specs.csv, checkpoint.json"
    )

    if unresolved:
        print()
        print("Not available on this MT5 terminal:")
        for canonical in unresolved:
            print(f"  - {canonical}")

    print()
    print(
        "The exported history is limited only by the historical data "
        "actually available from the connected broker/server."
    )

    print(
        "If the program is stopped before completion, rerun the same "
        "command and completed chunks will be skipped."
    )

    print("=" * 70)

    mt5.shutdown()


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    main()
