# EXPERT_ADVISOR_BOT

A Python-based Expert Advisor automation project that connects to a MetaTrader 5 desktop terminal and evaluates market data through several trading pipelines. The current primary runner in this repository is `main.py`.

> **Important:** This project can place real orders through the connected MetaTrader 5 account. Use a demo account first. The code has not been shown to be profitable, and the presence of risk controls does not remove the possibility of loss.

## Project overview

The primary runner starts four concurrent pipelines:

| Pipeline | Purpose                                                                       | Main components                                                |
| -------- | ----------------------------------------------------------------------------- | -------------------------------------------------------------- |
| Scalping | Short-term entries using M5 context and M1 confirmations                      | `scalping_pipeline.py`, `scalping_confirmation.py`             |
| Intraday | Higher-timeframe direction and zone context with lower-timeframe confirmation | `fxjers_signal.py`, `intraday_confirmation.py`                 |
| Swing    | Weekly/daily market context and daily zone-based entries                      | `fxjers_signal.py`, `swing_confirmation.py`                    |
| Red-news | News-event filtering, directional bias, zone checks and entry confirmation    | `news_signal.py`, `news_bias.py`, `news_zones.py`, `fxnews.py` |

The current symbol lists in `main.py` are set to `GOLD`, `EURUSD`, and `GBPUSD` for each pipeline. Broker symbol names and suffixes can differ, so confirm that these names exist in your MetaTrader 5 account before running the bot.

## Main capabilities

- Market analysis using market structure, BOS/CHoCH concepts, supply and demand zones, fair value gaps (FVG), liquidity conditions and candlestick patterns.
- Different signal paths for scalping, intraday, swing and news-event trading.
- Dynamic lot sizing and risk calculations, drawdown checks, position limits, spread checks, stop-loss validation and a no-hedging check.
- Position management that includes structural exits and mode-specific trade-management functions, including break-even and partial-close logic where applicable.
- Market-session, weekend and fixed-date holiday checks.
- Forex Factory calendar retrieval through the feed used by `fxnews.py`.
- CSV-based trade memory and execution-error logging.
- Historical performance analysis and daily, weekly and monthly email-report generation.
- Optional Notion trade-journal integration.
- A separate MetaTrader 5 historical OHLC exporter in `Export history.py`.

These are descriptions of code paths present in the repository, not guarantees that every broker, symbol, account type or market condition will work without configuration and testing.

## Technology

- Python 3.10 or newer (the source uses Python 3.10+ type-annotation syntax).
- MetaTrader 5 desktop terminal and the Python `MetaTrader5` package.
- `pandas`
- `numpy`
- `requests`

The archive does not include a `requirements.txt` file, and package versions are not pinned.

## Repository structure

```text
EXPERT_ADVISOR_BOT/
├── main.py                      # Primary multi-pipeline runner
├── strategy.py                  # Core strategy and market-analysis functions
├── fxjers_signal.py             # Swing, intraday and signal-generation logic
├── fxjers_main.py               # Alternate runner; needs missing ml_engine.py
├── scalping_pipeline.py         # M1/M5 scalping signal and management logic
├── scalping_confirmation.py     # Scalping confirmation checks
├── intraday_confirmation.py     # Intraday confirmation helpers
├── swing_confirmation.py        # Swing confirmation helpers
├── market_structure.py          # Market-structure calculations
├── market_regime.py             # Market-regime classification
├── market.py                    # Sessions, market-hours and holiday checks
├── candles.py                   # Candlestick-pattern helpers
├── smc.py                       # Smart Money Concepts helpers
├── zones.py                     # Supply/demand zones, FVG and related helpers
├── execution.py                 # MetaTrader 5 order execution
├── risk.py                      # Risk and lot-size calculations
├── trade_manager.py              # Open-position management and exits
├── fxnews.py                    # Forex Factory calendar fetcher
├── news.py                      # News filtering helpers
├── news_bias.py                 # News bias and event checks
├── news_signal.py               # News signal generation
├── news_main.py                 # Standalone news runner; needs missing ml_engine.py
├── news_smc.py                  # News classification and bias helpers
├── news_zones.py                # News-time zone and confirmation checks
├── memory_ai.py                 # CSV trade-memory storage
├── closed_memory.py             # Closed-trade result synchronisation
├── ai_learning.py               # Historical-performance filters and analysis
├── analytics.py                 # Performance and risk analytics
├── charts.py                    # HTML/SVG chart generation
├── email_report.py              # Email report generation and sending
├── notion_journal.py             # Optional Notion integration
├── error_log.py                 # Execution-error logging
├── self_ai.py                   # Trade-history analysis helpers
├── Export history.py            # Separate MT5 OHLC history exporter
├── trade_memory.csv              # Trade-memory CSV header supplied in archive
└── CHANGELOG.md                 # Project change history
```

## Installation

### 1. Prepare MetaTrader 5

1. Install the MetaTrader 5 desktop terminal.
2. Sign in to a **demo trading account** first.
3. Keep the terminal open while the Python bot is running.
4. Confirm that the configured instruments are visible in Market Watch and that candle/tick data is available.

### 2. Create a Python environment

Run these commands in PowerShell from the project directory:

```powershell
py -3.10 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install MetaTrader5 pandas numpy requests
```

If PowerShell blocks activation, use the Python environment's activation instructions for your Windows configuration. Do not install packages globally unless you intentionally want a system-wide installation.

## Configuration

### Trading symbols

Edit the symbol lists near the top of `main.py`:

- `SCALPING_SYMBOLS`
- `INTRADAY_SYMBOLS`
- `SWING_SYMBOLS`
- `NEWS_SYMBOLS`

Use the exact instrument names shown by your broker. Check the scalping whitelist in `execution.py` as well before adding a new scalping symbol.

### Optional Notion integration

`notion_journal.py` reads these environment variables:

- `NOTION_TOKEN`
- `NOTION_DATABASE_ID`

The integration remains disabled when the required values are not set.

### Optional email reports

`email_report.py` reads these environment variables:

- `BOT_EMAIL` — sender email address
- `BOT_PASS` — Gmail app password (not the normal account password)
- `BOT_RECV` — recipient email address

**Security prerequisite:** Before publishing this repository, remove the credential-like hard-coded fallback currently present in `email_report.py` and use environment variables only. The uploaded source also contains hard-coded email addresses. Replace those defaults with empty or non-personal placeholders. If the embedded app-password-like value belongs to a real account, revoke it and generate a new one; treat it as exposed. Never commit passwords, API tokens, account credentials or private configuration files.

Example PowerShell environment-variable setup for the current terminal session:

```powershell
$env:BOT_EMAIL = "your-sender@example.com"
$env:BOT_PASS = "your-gmail-app-password"
$env:BOT_RECV = "your-recipient@example.com"
$env:NOTION_TOKEN = "your-notion-integration-token"
$env:NOTION_DATABASE_ID = "your-notion-database-id"
```

Use real values only in your local terminal or secure secret manager. Do not paste them into this README or GitHub.

## Run the primary bot

From the project directory, with the virtual environment active and MetaTrader 5 open on a demo account:

```powershell
python main.py
```

`main.py` initializes the MetaTrader 5 connection and then starts the Scalping, Intraday, Swing and News pipeline threads. The current polling values are configured in `main.py` (`SCALP_INTERVAL`, `INTRADAY_INTERVAL`, `SWING_INTERVAL` and `NEWS_INTERVAL`).

**This is an execution bot, not a dry-run command.** When connected to a trading account, the code can send orders if its entry conditions pass. Do not run it on a live account until the code has been independently reviewed, tested on a demo account and checked against your broker's symbol specifications, contract sizes, leverage, margin rules and account mode.

## Export historical market data

`Export history.py` is a separate utility that connects to MetaTrader 5 and exports historical OHLC candles. Check its built-in help before using it:

```powershell
python "Export history.py" --help
```

The exporter has its own arguments for output location, symbols and timeframes. Exported market data should be stored locally and reviewed before it is shared.

## Runtime data files

The bot can create or update local files such as:

- `trade_memory.csv` — trade and performance history.
- `error_log.csv` — execution and filter errors.
- `ai_advisory.csv` — advisory analysis records.
- History-export CSV files and checkpoint files created by the exporter.

These files may contain private trading/account activity. Keep them local and exclude them from public Git commits unless you have deliberately reviewed and anonymised the contents.

## Important repository notes

1. **Use `main.py` as the primary entry point in this archive.** It is the runner whose imports do not depend on the missing `ml_engine.py` file.
2. `fxjers_main.py` and `news_main.py` import `ml_engine`, but `ml_engine.py` is not present in the uploaded archive. Those two scripts are therefore incomplete as supplied and should not be treated as runnable until that dependency is restored or the imports are deliberately reconciled.
3. The main runner prints that it is operating in “Pure Strategy Mode”. `ai_learning.py` contains historical win-rate/performance calculations and filters; this archive does not contain a trained ML model implementation. Do not describe the current primary runner as a trained machine-learning bot.
4. The archive includes `__pycache__` compiled files. They are generated artifacts and should be removed from the Git repository; Python will recreate them when needed.
5. No automated test suite, pinned dependency file or verified backtest results were found in the archive. Source syntax was checked, but a successful live MetaTrader 5 runtime, order execution, broker compatibility and strategy performance have not been verified by that syntax check.

## Recommended `.gitignore`
