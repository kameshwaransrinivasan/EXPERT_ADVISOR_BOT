# EA BOT v2 — Change Log

## 2026-08 — Items 2/3/4/5 (Deep-Review Follow-up, Priority 2 Still Pending)

Full patch plan delivered separately as `TRADEX_EA_Patch_Plan_Items_1-5.md`,
approved item-by-item before implementation. Priority 2 (Item 1: cross-strategy
position/exposure architecture) is explicitly NOT included here — still
pending MT5 account Hedging/Netting confirmation.

### Item 2 — `move_sl_to_be()` made mode-aware
| File | Change |
|---|---|
| `trade_manager.py` | `move_sl_to_be()`: INTRADAY now fetches H1 candles (was M5) for its breakeven-volatility trigger, matching `intraday_exit()`'s own H1 authority. SWING now fetches D1 (was M5), matching `swing_exit()`'s own D1 authority. Same trigger formula (`vol × 1.5`) — only the candle timeframe changed. `manage_position()` now passes its already-computed `mode` through instead of a second `detect_mode_from_comment()` call. |

### Item 3 — Scalping's duplicate break-even system removed
| File | Change |
|---|---|
| `trade_manager.py` | `move_sl_to_be()`'s SCALPING/RED_NEWS branch is now a no-op — it used to independently move SL to breakeven at a fixed 10 pips (100pts Gold) / entry+half-pip, on the main-loop's 30s poll, racing `scalping_pipeline.apply_be_hook()`'s own adaptive `min(8pips, SL distance)` / entry+full-spread trigger on the scalping thread's own 30s poll. `apply_be_hook()` (unchanged) is now the sole Scalping/RED_NEWS breakeven authority. |

### Item 4 — Intraday VOLATILE_EXPANSION no longer a hard block
| File | Change |
|---|---|
| `main.py` | `intraday_thread()`: removed `if regime["action"] == "HALVE_RISK": continue`. Verified against `CHANGELOG`'s own original SRD regime table ("Halve risk per trade") and against Scalping/Swing (which already trade through this regime at half size) before changing — the hard skip was an undocumented Intraday-only deviation, not a deliberate design choice. `execute_trade(..., risk_multiplier=regime["risk_multiplier"])` — unchanged — now actually applies to the trades this unblocks. |

### Item 5 — Minimum-lot Scalping partial-close can no longer be mislabeled
| File | Change |
|---|---|
| `scalping_pipeline.py` | `manage_scalp_partials()`: when 70% of the position rounds back up to the full position size (broker `volume_min`/`volume_step` won't allow a real split), the exit behavior is unchanged (still a full close at 1:1.5 RR, per instruction — no move to a 2R target without backtest evidence) but is now tagged distinctly: `"MIN-LOT FULL CLOSE..."` in the console log instead of `"70% PARTIAL (full)"`, and `comment="SCALP_MINLOT_FULL_1.5R"` on the actual MT5 deal instead of the generic `"STRUCTURAL_EXIT"`. |
| `execution.py` | `close_position()` gained an optional `comment` parameter (default `"STRUCTURAL_EXIT"`, unchanged for every other caller) so the tag above lands on the real MT5 deal record, not just the console. |

---

## 2026-08 — Consolidation Fixes (Root-Cause Audit, Priorities 1/3/4/5)

Full analysis delivered separately as `TRADEX_EA_Root_Cause_Analysis.md` and
`TRADEX_EA_Final_Implementation_Plan.md`. Priority 2 (cross-strategy
position/exposure model) is NOT included here — pending confirmation of
whether the MT5 account is in Hedging or Netting mode.

### Priority 1 — Scalping session double-gate
| File | Change |
|---|---|
| `scalping_confirmation.py` | `_check_session()` no longer defines its own London(07-12)/NY(13-17) UTC windows. It now delegates to `market.get_current_session()` — the same authority `main.py`'s `scalping_thread()` gates on — so there's one session decision instead of two that silently AND together and blocked the entire Asian session. |
| `main.py` | Updated the stale comment above the `conf["c1"]` reuse in `scalping_thread()` to reflect that it's now a confirmatory restatement of the same session decision, not a second independent gate. |

### Priority 3 — Intraday duplicate gate stacking
| File | Change |
|---|---|
| `fxjers_signal.py` | `intraday_signal()`: H4/H1 mandatory gates (structure, fresh zone, H1 zone+FVG confluence) unchanged. M15-level checks (BOS alignment, wick rejection >40%, M15 zone+FVG confluence, M15 sweep/OB-retest, M1/pattern/displacement OR-set) consolidated into one scored decision — `MIN_M15_SCORE = 3` of 5, a tunable starting point, not an asserted-optimal number. Reuses `_check_m15_liq_sweep_or_ob()` from `intraday_confirmation.py` directly on its own already-fetched M15 candles (fixes a data-freshness race where that check used to run again later against separately re-fetched candles). |
| `intraday_confirmation.py` | `check_intraday_confirmations()` (and its `_check_h1_poi`/`_check_h4_trend` sub-checks) retired as a live gate — both duplicated checks `intraday_signal()` already does. Kept in the file for reference; `_check_m15_liq_sweep_or_ob()` remains live, now called from `fxjers_signal.py`. |
| `main.py` | `intraday_thread()`: removed the `check_intraday_confirmations()` import/call (retired second gate). The one-day-sample "weak pattern" hard block is now a log-only watch, not a skip. |

### Priority 4 — VOLATILE_EXPANSION risk-sizing bug
| File | Change |
|---|---|
| `risk.py` | `dynamic_risk_engine()` no longer halves `risk_percent` internally when `volatile_expansion=True`. That halving was also being applied by the caller's `regime["risk_multiplier"]` (also 0.5 for this regime), so risk was quartered instead of the documented halving. Single source of truth is now the caller-side multiplier — matches how `CHOPPY_RANGE`/`MACRO_REVERSAL` already worked correctly. |

### Priority 5 — Swing/Intraday SL trail too tight
| File | Change |
|---|---|
| `trade_manager.py` | `lock_profit()` is now mode-aware: SWING trails on D1 (3-candle lookback, matching `swing_exit()`'s own D1 authority), INTRADAY trails on H1 (5-candle, matching `intraday_exit()`'s H1 authority), SCALPING/RED_NEWS unchanged (M5, 5-candle — already matched `scalping_exit()`'s own M5 authority). Previously always used M5/5-candle for every mode, which could stop out Swing (and to a lesser extent Intraday) positions on ordinary lower-timeframe noise their own exit logic is designed to tolerate. `manage_position()` now passes its already-computed `mode` through instead of leaving `lock_profit()` to guess. |

---

## SRD Integration: What Changed & Why

---

## NEW FILE: `market_regime.py`
**SRD Section 6 — Market Adaptive Engine**

Completely new module. Classifies the current market state per symbol:

| Regime | Trigger | Action |
|---|---|---|
| `SMOOTH_TREND` | ATR_14/ATR_100 = 0.7–1.5 | Normal operation |
| `CHOPPY_RANGE` | Vm < 0.7 or SIDEWAYS structure | Freeze breakout entries, tighten scalp matrices |
| `VOLATILE_EXPANSION` | Vm ≥ 1.5 | Halve risk per trade (Gold 1.5%→0.75%, FX 2.0%→1.0%) |
| `MACRO_REVERSAL` | H4 CHoCH confirmed | Trigger swing exit signal |

Called from both `fxjers_main.py` and `news_main.py` before every entry.

---

## UPDATED: `market.py`
**SRD Section 4 — Session Tracker & Holiday Failsafe**

| Before | After |
|---|---|
| Sessions used local time, vague hours | Full UTC times: Asian 00–09, London 08–17, Overlap 13–17, NY 17–22 |
| Simple ASIAN/LONDON/NEWYORK/OFF | Added `OVERLAP` session (London/NY 13–17 UTC = peak) |
| Basic weekend check | `should_force_close_now()` → True on Fri ≥ 21:00 UTC |
| 2 hardcoded holidays | Fixed holiday list (Jan 1, Dec 25, Jul 4 US) |
| No resume logic | Documented Sun 22:00 UTC resume |

New helper: `should_force_close_now()` used by both main loops.

---

## UPDATED: `risk.py`
**SRD Sections 3 & 6 — Dynamic Risk Engine**

| Before | After |
|---|---|
| Single base risk rate | Group 1 (Gold/Silver): max 1.5% · Group 2 (FX): max 2.0% |
| Flat scalping caps | `scalp_cap_for_balance()`: ≤$100→2, $200–$500→4, $500–$1000→6 |
| No volatile expansion handling | `volatile_expansion=True` parameter halves risk automatically |
| Mode multiplier on fixed base | Mode multiplier applied on top of group base rate |
| Shared `max_trades` | `pipeline_caps()` returns `{"pair_max", "portfolio_max"}` per mode |
| No group concept | `symbol_group()` maps symbols to GROUP1/GROUP2 |

Key new function: `pipeline_caps(mode, balance)` — used by `can_open_trade()` in both main loops.

---

## UPDATED: `execution.py`
**SRD Sections 1 & 2 — Mandatory SL, No Hedging, No Fixed TP**

| Before | After |
|---|---|
| SL=0 would pass silently | **BLOCKED** — returns error string if `stop_loss == 0` |
| No hedging check | Scans existing positions — blocks if opposite direction found |
| `take_profit` used as-is | `tp_value = 0.0` always — TP parameter accepted but discarded |
| Comment: `f"{mode}_{trade_type}"` | Same — no change |

The `take_profit` parameter is kept in the function signature for call-site compatibility but is never sent to MT5. All exits are structural.

---

## UPDATED: `trade_manager.py`
**SRD Section 2 — Structural Exits, No Fixed TP, Weekend Force-Close**

Major rewrite. Old file had a single `trend_exit()` function. New file has a full dispatcher:

| Function | Pipeline | Exit Trigger |
|---|---|---|
| `scalping_exit()` | SCALPING / RED_NEWS | M1/M5 CHoCH, buy/sell-side sweep + bearish pressure, exhaustion |
| `intraday_exit()` | INTRADAY | M15 CHoCH, M15+H1 structure flip (both BEARISH/BULLISH simultaneously) |
| `swing_exit()` | SWING | H4 CHoCH or D1 market structure flip — weekend hold is fine |
| `manage_position()` | All | Dispatches by `mode` from trade comment, applies SL management first |
| `weekend_force_close()` | SCALPING + INTRADAY | Closes all non-SWING positions when `should_force_close_now()` = True |

`partial_tp()` updated: uses `risk_dist = abs(entry - sl)` (1:1 from SL) instead of fixed TP price. Only triggers when exhaustion confirmed.

`move_sl_to_be()` + `lock_profit()` unchanged in logic, now both call `tp=0.0` in modify requests.

---

## UPDATED: `news_bias.py`
**SRD Section 5 — Pre-Scan, Zone Failsafe, Timing**

| Before | After |
|---|---|
| No T-1 minute pre-scan | `pre_news_scan()` — finds HIGH/MEDIUM events releasing in next 60s, returns pairs affected |
| No scalping force-close / freeze logic | `scalp_news_block()` — returns `FORCE_CLOSE` (T-3min) or `FREEZE` (30min post-release) |
| No direction-zone failsafe | `direction_zone_failsafe(direction, zone_type)` — BUY + SUPPLY = BLOCK, SELL + DEMAND = BLOCK |
| Basic pair-currency map | Added XAGUSD, EURGBP, USDCAD, USDCHF |
| `get_pairs_for_currency()` missing | Added — used by pre_news_scan |

---

## UPDATED: `ai_learning.py`
**SRD Sections 6 & 7 — Advisory-Only Engine**

| Before (ai_learing.py) | After (ai_learning.py) |
|---|---|
| Filename had typo `ai_learing.py` | Fixed to `ai_learning.py` |
| No write restriction documented | Header + docstring: "NEVER writes to source files" |
| `weekly_ai_review()` had no write output | Now appends to `ai_advisory.csv` (append-only log) |
| No Mistake Analyzer | `analyze_sl_hits(df)` — diagnoses each LOSS trade with reason tag |
| No 7-day window filter | `load_last_n_days(days)` — filters by UTC time window |
| `ai_suggestion` was a list | Extended with specific SRD suggestions (ATR, XAUUSD cap, etc.) |
| No `why_best` / `why_worst` logic | Added — reads mode breakdown for best/worst pairs |

Advisory log (`ai_advisory.csv`) is **append-only**. The AI module has **no write access to any `.py` file**.

---

## UPDATED: `email_report.py`
**SRD Sections 7 & 8 — HTML Reports, Daily/Weekly/Monthly**

| Before | After |
|---|---|
| Plain text email | **HTML email** with styled tables and color headers |
| Single `send_email_report()` | Three separate senders: `send_daily_report()`, `send_weekly_report()`, `send_monthly_report()` |
| No per-pair breakdown table | `_html_table()` renders Pair · Trades · Wins · Losses · WR% · Profit |
| No Mistake Analyzer | Calls `analyze_sl_hits()` → table of SL hit reasons per trade |
| No AI advisory block | Dedicated section with orange border: "Advisory suggestions — Manual Review Only" |
| No `why_best`/`why_worst` | Period reports include reasoning for best and worst pairs |
| No monthly report | `send_monthly_report()` + `_monthly_review()` added (30-day window) |
| Schedule triggered externally | Schedule logic in main loop checks `now.day == 1` for monthly |

---

## UPDATED: `fxjers_main.py`
**Full SRD Integration**

| Before | After |
|---|---|
| `place_trade(..., tp=entry + risk*2)` | `place_trade(..., 0.0)` — no fixed TP |
| `trend_exit(pos)` | `manage_position(pos)` — dispatches scalp/intraday/swing exits |
| No weekend force-close | `weekend_force_close()` called when `should_force_close_now()` |
| No regime classifier | `classify_regime(symbol)` called per symbol — blocks breakouts during CHOPPY_RANGE |
| No directional alignment check | `check_intraday_swing_alignment()` — closes intraday if conflicts with swing direction |
| Single `can_open_trade()` | Now calls `pipeline_caps(mode, balance)` for balance-scaled caps |
| `volatile_expansion` not passed to risk | `dynamic_risk_engine(..., volatile_expansion=regime["volatile_expansion"])` |
| Scalping caps hardcoded | Delegated to `pipeline_caps()` which reads balance |
| No T-1 pre-news alert | `pre_news_scan()` called each loop, prints alerts |
| Single daily email | `send_daily_report()`, `send_weekly_report()`, `send_monthly_report()` |
| `OVERLAP` session missing | Added to `SESSION_MODULE` → `"INTRADAY"` |

---

## UPDATED: `news_main.py`
**SRD Section 5 Full Implementation**

| Before | After |
|---|---|
| No pre-news scan | `pre_news_scan()` + print alerts |
| No scalp force-close at T-3 | `scalp_news_block()` → FORCE_CLOSE closes all scalp positions at T-3min |
| No 30-min post-news freeze | `scalp_news_block()` → FREEZE skips entry for 30 min |
| No direction-zone failsafe | `direction_zone_failsafe()` called before execute — BLOCK on mismatch |
| Fixed TP used | `place_trade(..., 0.0)` — no fixed TP |
| Old `move_sl_to_be` etc | `manage_position(pos)` |
| No regime check | `classify_regime(symbol)` — `volatile_expansion` passed to risk engine |
| No weekend force-close | `weekend_force_close()` on Fri 21:00 UTC |

---

## FILES UNCHANGED (copied as-is)

| File | Reason |
|---|---|
| `candles.py` | Pattern detection — no SRD changes needed |
| `closed_memory.py` | Trade sync logic — correct |
| `fxjers_signal.py` | Signal engine — structural, no TP changes needed at this layer |
| `fxnews.py` | Forex Factory API fetcher — no changes |
| `market_structure.py` | BOS/CHoCH/sweep detection — correct |
| `memory_ai.py` | CSV trade memory — correct |
| `news.py` | Basic news filter — used by fxjers_main |
| `news_signal.py` | News signal master pipeline — unchanged |
| `news_smc.py` | News SMC analysis — unchanged |
| `news_zones.py` | 30M zone detection + LTF confirmation — unchanged |
| `notion_journal.py` | Notion API logger — unchanged |
| `self_ai.py` | Self-analysis — unchanged |
| `smc.py` | Core SMC engine — unchanged |
| `strategy.py` | Core strategy functions — unchanged |
| `zones.py` | S&D zones, FVG, GanBox — unchanged |

---

## IMPORT RENAME NOTE
`ai_learing.py` → `ai_learning.py` (typo fix)

All imports updated:
- `fxjers_main.py`: `from ai_learning import ...`
- `news_main.py`: `from ai_learning import ...`
- `email_report.py`: `from ai_learning import ...`

If you still have the old filename on disk, rename it:
```
rename ai_learing.py ai_learning.py
```

---

## SETUP CHECKLIST

1. All 27 files in **one folder** (no subfolders)
2. File names are **lowercase with underscores** — Windows rename if needed
3. `news_main.PY` → rename to `news_main.py`
4. `ai_learing.py` → rename to `ai_learning.py`
5. Fill in config values in `notion_journal.py` and `email_report.py`
6. MT5 terminal must be open before running either main file
7. Run **fxjers_main.py** for SMC strategy
8. Run **news_main.py** for institutional news scalping
9. Both can run simultaneously in separate terminals
