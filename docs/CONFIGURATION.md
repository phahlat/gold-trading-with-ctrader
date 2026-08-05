# Gold Bot Configuration Guide

## Configuration Layers

The gold bot reads its configuration from the environment file in gold_bot/.env. Copy gold_bot/.env.example to gold_bot/.env before running the bot.

The file contains grouped sections with detailed comments so you can understand both the purpose of each variable and how it affects the runtime.

## Environment Variable Groups

### cTrader Connection Settings

Used only for live trading.

1. CTRADER_CLIENT_ID
2. CTRADER_CLIENT_SECRET
3. CTRADER_ACCESS_TOKEN
4. CTRADER_REFRESH_TOKEN
5. CTRADER_ACCOUNT_ID
6. CTRADER_LIVE_ACCOUNT_ID
7. CTRADER_DEMO_ACCOUNT_ID
8. CTRADER_HOST
9. CTRADER_REQUEST_TIMEOUT_SECONDS
10. CTRADER_CONNECT_TIMEOUT_SECONDS

### Runtime Scope

Used for both live and backtest runs.

1. ENABLE_TRADING
2. PLOT_ENABLED
3. SYMBOLS
4. TIMEFRAME
5. LOWER_TIMEFRAME
6. HIGHER_TIMEFRAME
7. CANDLE_COUNT
8. REFRESH_CANDLE_COUNT
9. POLL_SECONDS
10. POSITION_MONITOR_SECONDS
11. ACCOUNT_MONITOR_SECONDS
12. CHART_UPDATE_SECONDS
13. CHART_WIDTH
14. CHART_HEIGHT
15. PLOT_LTF_CANDLES
16. PLOT_HTF_CANDLES
17. MAX_CYCLES
18. CTRADER_POSITION_DB_PATH
19. LOG_LEVEL
20. STRATEGY_EVAL_LOG_VERBOSE

Chart sizing behavior:
- Chart size and visible candle windows are fully controlled by env variables above.
- There is no renderer fallback for these values; missing values raise a startup error.

Logging behavior:
- `LOG_LEVEL` controls runtime verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`).
- Default is `DEBUG` when variable is not set.
- `STRATEGY_EVAL_LOG_VERBOSE` controls detailed live strategy evaluation logs (`true` or `false`).
- When enabled, logs include per-strategy and per-timeframe evaluation status (`running`, `successful`, `failed`) and decision outcomes.

Broker symbol handling:
- `SYMBOLS` accepts aliases such as `GOLD` or `XAUUSD`.
- In live mode, the bot validates the requested symbol against broker symbols and resolves aliases automatically.
- Startup logs include `🔎 Symbol verification | requested=... resolved=... mode=...`.

Live signal and entry diagnostics:
- Signal-to-entry flow is explicitly logged with emoji markers to make decision paths easy to scan.
- Look for `📣` (signal details), `⛔` (skip reason), `🧾` (entry request), `✅` (accepted/filled), and `❌` (rejected/failed).

### Backtest Controls

Used when running against historical CSV data.

1. BACKTEST_DATA_DIR
2. BACKTEST_INITIAL_BALANCE
3. BACKTEST_FIXED_VOLUME
4. BACKTEST_RESULTS_SUBDIR
5. BACKTEST_SPEED
6. BACKTEST_LOOKBACK_VALUE
7. BACKTEST_LOOKBACK_UNIT
8. BACKTEST_USE_BROKER_PROFILE
9. BACKTEST_SIMULATE_MARGIN_REJECTION
10. BACKTEST_VOLUME_MIN
11. BACKTEST_VOLUME_MAX
12. BACKTEST_VOLUME_STEP
13. BACKTEST_MAX_VOLUME_CAP
14. BACKTEST_DEFAULT_CONTRACT_SIZE
15. BACKTEST_DEFAULT_LEVERAGE
16. BACKTEST_MARGIN_AVAILABLE_RATIO
17. BACKTEST_WARN_VOLUME_ABOVE
18. BACKTEST_WARN_EQUITY_MULTIPLIER

Backtest data expectations:
- Point `BACKTEST_DATA_DIR` to a directory that contains timeframe CSVs for both LTF and HTF.
- The loader matches file suffix by timeframe minutes (for example `M15 -> *15.csv`, `H1 -> *60.csv`, `H4 -> *240.csv`).
- Strategy evaluation uses both loaded frames; HTF is not synthesized from LTF.

Backtest lookback window:
- Set `BACKTEST_LOOKBACK_VALUE` to a positive integer and `BACKTEST_LOOKBACK_UNIT` to `weeks` or `months`.
- Example: `BACKTEST_LOOKBACK_VALUE=12` and `BACKTEST_LOOKBACK_UNIT=weeks` uses only the last 12 weeks of data.
- Set `BACKTEST_LOOKBACK_VALUE=0` to disable filtering and use full dataset.

Backtest realism controls:
- `BACKTEST_USE_BROKER_PROFILE=true` loads broker symbol constraints and leverage (when cTrader is reachable).
- `BACKTEST_SIMULATE_MARGIN_REJECTION=true` skips trades that would exceed estimated available margin.
- `BACKTEST_VOLUME_MIN/MAX/STEP` provide fallback lot constraints when broker metadata is unavailable.
- `BACKTEST_MAX_VOLUME_CAP` adds an optional hard upper lot cap on top of broker limits.
- `BACKTEST_DEFAULT_CONTRACT_SIZE` and `BACKTEST_DEFAULT_LEVERAGE` are used for fallback margin estimates.
- `BACKTEST_MARGIN_AVAILABLE_RATIO` controls conservative margin allowance (for example `0.95`).
- `BACKTEST_WARN_VOLUME_ABOVE` and `BACKTEST_WARN_EQUITY_MULTIPLIER` emit one-time realism warnings.

Backtest plot and report outputs:
- Backtest output files are uniquely named per run using this pattern:
	- `<source>__<strategy-list>__<utc-timestamp>_summary.txt`
	- `<source>__<strategy-list>__<utc-timestamp>_signals.csv`
	- `<source>__<strategy-list>__<utc-timestamp>_backtest_heikinashi.png`
- Backtest chart markers now include labeled levels for each entry event: `Entry`, `SL`, and `TP1..TPx`.

### Strategy and Ladder Settings

Used by the signal engine and ladder trade builder.

1. GOLD_STRATEGY_NAMES
2. GOLD_ENABLE_MULTI_ENTRY
3. GOLD_LADDER_ENTRIES
4. GOLD_LADDER_STEP_RATIO
5. GOLD_FIXED_LOT_SIZE

Per-strategy timeframe and exit presets:

1. `<STRATEGY>_LTF` and `<STRATEGY>_HTF` accept comma-separated lists (for example `M15,M30` and `H1,H4`).
2. Both lists must have equal length so each LTF maps to one HTF.
3. `<STRATEGY>_GOLD_STOP_LOSS_PIPS` and `<STRATEGY>_GOLD_TAKE_PROFIT_PIPS` accept either one value (broadcast to all pairs) or a list matching pair count.
4. Invalid list lengths fail fast during settings load.

### Risk and Exit Settings

Used to convert signals into trade plans.

1. GOLD_RISK_PERCENT
2. GOLD_STOP_LOSS_PIPS
3. GOLD_TAKE_PROFIT_PIPS
4. GOLD_EMA_FAST
5. GOLD_EMA_SLOW
6. GOLD_EMA_TREND_PERIOD
7. GOLD_MAX_DAILY_TRADES
8. GOLD_MAX_DAILY_RISK_PCT
9. GOLD_MAX_OPEN_POSITIONS
10. GOLD_TRADE_MAGIC_NUMBER
11. GOLD_TRADE_COMMENT_PREFIX
12. GOLD_PIP_SIZE

## Recommended Presets

### Backtest Validation

```env
ENABLE_TRADING=false
PLOT_ENABLED=false
SYMBOLS=XAUUSD
TIMEFRAME=M15
BACKTEST_DATA_DIR=path/to/your/candle_directory/
BACKTEST_RESULTS_SUBDIR=gold_bot
BACKTEST_SPEED=100ms
GOLD_STRATEGY_NAMES=trend_following,price_action
GOLD_ENABLE_MULTI_ENTRY=true
GOLD_LADDER_ENTRIES=3
```

### Live Dry Run

```env
ENABLE_TRADING=false
PLOT_ENABLED=false
SYMBOLS=GOLD
TIMEFRAME=M15
LOWER_TIMEFRAME=M15
HIGHER_TIMEFRAME=H1
POSITION_MONITOR_SECONDS=5
ACCOUNT_MONITOR_SECONDS=30
GOLD_STRATEGY_NAMES=trend_following
GOLD_ENABLE_MULTI_ENTRY=true
```

## Validation Checklist

1. Copy the example file into gold_bot/.env.
2. Run a small backtest first.
3. Review the generated summary and signal CSV in backtest/results/gold_bot.
4. Promote to live only after dry-run validation.
5. Check startup logs for `Symbol verification` to confirm broker symbol resolution.
