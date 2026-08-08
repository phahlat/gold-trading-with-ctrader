# Gold Bot Strategies

The bot supports three strategies:

1. trend_following
2. price_action
3. session_breakout
4. ema_crossover

## Timeframe Model

The engine always uses paired timeframes per strategy.

1. LTF drives entry signal creation and simulated exits in backtest.
2. HTF provides directional trend filtering through EMA bias.
3. Each strategy can define one or more LTF/HTF pairs in `.env` using comma-separated values.

There are no hardcoded M15/H1 assumptions in strategy execution. Timeframes are loaded from each strategy preset in bot/.env.

Examples:

- TREND_FOLLOWING_LTF / TREND_FOLLOWING_HTF
- PRICE_ACTION_LTF / PRICE_ACTION_HTF
- SESSION_BREAKOUT_LTF / SESSION_BREAKOUT_HTF
- Optional per-pair exits: `<STRATEGY>_GOLD_STOP_LOSS_PIPS` and `<STRATEGY>_GOLD_TAKE_PROFIT_PIPS`

## Live vs Backtest Consistency

Both live and backtest evaluate each active strategy independently across all configured LTF/HTF pairs.

1. Live mode: pulls broker candles per strategy timeframe.
2. Backtest mode: loads timeframe CSV files from BACKTEST_DATA_DIR per strategy timeframe.
3. Both modes: apply HTF EMA bias filter before trade execution.

With multiple active strategies, each strategy can use different LTF/HTF pairs in the same run.

List rules:

1. `*_LTF` and `*_HTF` must have the same number of comma-separated items.
2. `*_GOLD_STOP_LOSS_PIPS` and `*_GOLD_TAKE_PROFIT_PIPS` can be either:
3. A single value, applied to every pair, or
4. A comma-separated list with exactly the same number of items as `*_LTF`/`*_HTF`.

## Strategy Logic

### trend_following

Signal model:

1. Compute EMA fast, EMA slow, EMA trend on LTF.
2. Buy when close > EMA trend and fast crosses above slow.
3. Sell when close < EMA trend and fast crosses below slow.

### price_action

Signal model:

1. Build prior 5-candle window on LTF.
2. Buy when latest close breaks above recent high.
3. Sell when latest close breaks below recent low.

### session_breakout

Signal model:

1. Build prior 8-candle session range on LTF.
2. Buy when latest close breaks session high.
3. Sell when latest close breaks session low.

### ema_crossover

Signal model (per timeframe state machine):

1. Candle 1 must close above EMA slow for bullish, below EMA slow for bearish.
2. Candle 2 confirmations:
3. Bullish: close >= Candle 1 close -> buy confirmation.
4. Bullish continuation: close < Candle 1 close and still above EMA slow -> wait Candle 3.
5. Bullish Candle 3: close > Candle 1 and Candle 2 close, or close == Candle 1 close -> buy confirmation.
6. Bearish: close >= Candle 1 close and still below EMA slow -> sell confirmation.
7. Bearish continuation: close < Candle 1 close and still below EMA slow -> wait Candle 3.
8. Bearish Candle 3: close > Candle 1 and Candle 2 close -> sell confirmation; otherwise reset.

Environment variables used by this strategy:

1. EMA_FAST
2. EMA_SLOW
3. STOP_LOSS_PIPS
4. TAKE_PROFIT_PIPS
5. TIMEFRAMES
6. RISK_REWARD_RATIO
7. GLOBAL_POSITION_LIMIT

## HTF Bias Filter

After LTF candidate generation, the same HTF filter is applied:

1. Buy bias when HTF close >= HTF EMA trend and HTF EMA fast >= HTF EMA slow.
2. Sell bias when HTF close <= HTF EMA trend and HTF EMA fast <= HTF EMA slow.
3. Neutral bias keeps all candidates.
4. Non-neutral bias keeps only matching-direction candidates.

## Preset Examples

### Trend preset

```env
GOLD_STRATEGY_NAMES=trend_following
TREND_FOLLOWING_LTF=M30
TREND_FOLLOWING_HTF=H4
TREND_FOLLOWING_GOLD_STOP_LOSS_PIPS=120
TREND_FOLLOWING_GOLD_TAKE_PROFIT_PIPS=250
```

### Trend preset with multiple pairs

```env
GOLD_STRATEGY_NAMES=trend_following
TREND_FOLLOWING_LTF=M15,M30
TREND_FOLLOWING_HTF=H1,H4
TREND_FOLLOWING_GOLD_STOP_LOSS_PIPS=100,140
TREND_FOLLOWING_GOLD_TAKE_PROFIT_PIPS=220,320
```

### Price action preset

```env
GOLD_STRATEGY_NAMES=price_action
PRICE_ACTION_LTF=M30
PRICE_ACTION_HTF=H4
```

### Session breakout preset

```env
GOLD_STRATEGY_NAMES=session_breakout
SESSION_BREAKOUT_LTF=M15
SESSION_BREAKOUT_HTF=H1
```

## Multi-Strategy Example

```env
GOLD_STRATEGY_NAMES=trend_following,price_action,session_breakout
TREND_FOLLOWING_LTF=M30
TREND_FOLLOWING_HTF=H4
PRICE_ACTION_LTF=M5
PRICE_ACTION_HTF=M30
SESSION_BREAKOUT_LTF=M15
SESSION_BREAKOUT_HTF=H1
```

In this configuration, the bot loads and evaluates M30/H4, M5/M30, and M15/H1 concurrently.
