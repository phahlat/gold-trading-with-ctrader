from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class StrategyPreset:
    lower_timeframes: list[str]
    higher_timeframes: list[str]
    stop_loss_pips_list: list[float]
    take_profit_pips_list: list[float]

    @property
    def lower_timeframe(self) -> str:
        return self.lower_timeframes[0]

    @property
    def higher_timeframe(self) -> str:
        return self.higher_timeframes[0]

    @property
    def stop_loss_pips(self) -> float:
        return self.stop_loss_pips_list[0]

    @property
    def take_profit_pips(self) -> float:
        return self.take_profit_pips_list[0]

    def pair_configs(self) -> list[dict[str, float | str | int]]:
        pairs: list[dict[str, float | str | int]] = []
        pair_count = len(self.lower_timeframes)
        for index in range(pair_count):
            pairs.append(
                {
                    "pair_index": index,
                    "lower_timeframe": self.lower_timeframes[index],
                    "higher_timeframe": self.higher_timeframes[index],
                    "stop_loss_pips": float(self.stop_loss_pips_list[index]),
                    "take_profit_pips": float(self.take_profit_pips_list[index]),
                }
            )
        return pairs


@dataclass(frozen=True)
class GoldSettings:
    ctrader_client_id: str
    ctrader_client_secret: str
    ctrader_access_token: str
    ctrader_refresh_token: str
    ctrader_account_id: int
    ctrader_live_account_id: int
    ctrader_demo_account_id: int
    ctrader_host: str
    ctrader_request_timeout_seconds: float
    ctrader_connect_timeout_seconds: float
    ctrader_reconnect_attempts: int
    ctrader_reconnect_wait_seconds: float
    ctrader_heartbeat_interval_seconds: float
    ctrader_heartbeat_timeout_seconds: float
    enable_trading: bool
    plot_enabled: bool
    symbols: list[str]
    strategy_presets: dict[str, StrategyPreset]
    lower_timeframe: str
    higher_timeframe: str
    candle_count: int
    refresh_candle_count: int
    poll_seconds: float
    position_monitor_seconds: float
    account_monitor_seconds: float
    chart_update_seconds: float
    chart_width: float
    chart_height: float
    chart_tp_marker_size: float
    chart_sl_marker_size: float
    chart_entry_marker_size: float
    chart_direction_marker_size: float
    plot_ltf_candles: int
    plot_htf_candles: int
    max_cycles: int
    log_level: str
    strategy_eval_log_verbose: bool
    backtest_data_dir: str
    backtest_initial_balance: float
    backtest_fixed_volume: float
    backtest_results_subdir: str
    backtest_speed_ms: float
    backtest_lookback_value: int
    backtest_lookback_unit: str
    backtest_use_broker_profile: bool
    backtest_simulate_margin_rejection: bool
    backtest_volume_min: float
    backtest_volume_max: float
    backtest_volume_step: float
    backtest_max_volume_cap: float
    backtest_default_contract_size: float
    backtest_default_leverage: float
    backtest_margin_available_ratio: float
    backtest_warn_volume_above: float
    backtest_warn_equity_multiplier: float
    strategy_names: list[str]
    enable_multi_entry: bool
    ladder_entries: int
    ladder_step_ratio: float
    fixed_lot_size: float
    stop_loss_pips: float
    take_profit_pips: float
    ema_fast: int
    ema_slow: int
    ema_trend_period: int
    max_daily_trades: int
    max_open_positions: int
    trade_magic_number: int
    trade_comment_prefix: str
    pip_size: float
    price_action_cooldown_minutes: float
    position_db_path: str
    config_env_path: str


def _bool_env(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _float_env(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def _int_env(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _required_float_env(name: str) -> float:
    value = os.getenv(name)
    if value is None or not value.strip():
        raise ValueError(f"Missing required environment variable: {name}")
    return float(value)


def _required_int_env(name: str) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        raise ValueError(f"Missing required environment variable: {name}")
    return int(value)


def _required_backtest_speed_ms() -> float:
    raw_value = os.getenv("BACKTEST_SPEED")
    if raw_value is None or not raw_value.strip():
        raise ValueError("Missing required environment variable: BACKTEST_SPEED")

    text = raw_value.strip().lower()
    try:
        if text.endswith("ms"):
            value = float(text[:-2].strip())
        elif text.endswith("s"):
            value = float(text[:-1].strip()) * 1000.0
        else:
            value = float(text)
    except ValueError as exc:
        raise ValueError(f"Invalid BACKTEST_SPEED value: {raw_value}. Use formats like 100ms, 1s, or 250.") from exc

    if value < 0:
        raise ValueError(f"Invalid BACKTEST_SPEED value: {raw_value}. Value must be >= 0.")
    return value


def _resolve_env_path(env_path: str) -> Path:
    raw_path = Path(env_path).expanduser()
    if raw_path.is_absolute():
        return raw_path

    candidate_paths: list[Path] = [raw_path]
    if raw_path.parts and raw_path.parts[0] == "gold_bot":
        candidate_paths.append(Path("bot") / Path(*raw_path.parts[1:]))
    elif raw_path.parts and raw_path.parts[0] == "bot":
        candidate_paths.append(Path("gold_bot") / Path(*raw_path.parts[1:]))

    if raw_path.name == ".env":
        candidate_paths.extend([Path("bot/.env"), Path("gold_bot/.env")])
    elif raw_path.suffix == ".env":
        candidate_paths.extend([Path("bot") / raw_path.name, Path("gold_bot") / raw_path.name])

    for candidate in candidate_paths:
        if candidate.exists():
            return candidate.resolve()

    return raw_path.resolve()


def _strategy_preset_env(
    prefix: str,
    default_ltf: str,
    default_htf: str,
    default_stop_loss_pips: float,
    default_take_profit_pips: float,
) -> StrategyPreset:
    def _list_env(raw_value: str | None, default_value: str) -> list[str]:
        source = raw_value if raw_value is not None and raw_value.strip() else default_value
        items = [item.strip().upper() for item in source.split(",") if item.strip()]
        return items or [default_value.strip().upper()]

    def _float_list_env(raw_value: str | None, default_value: float) -> list[float]:
        source = raw_value if raw_value is not None and raw_value.strip() else str(default_value)
        values: list[float] = []
        for token in source.split(","):
            cleaned = token.strip()
            if not cleaned:
                continue
            values.append(max(1.0, float(cleaned)))
        return values or [max(1.0, float(default_value))]

    lower_timeframes = _list_env(os.getenv(f"{prefix}_LTF"), default_ltf)
    higher_timeframes = _list_env(os.getenv(f"{prefix}_HTF"), default_htf)
    if len(lower_timeframes) != len(higher_timeframes):
        raise ValueError(
            f"Invalid {prefix} timeframe lists: {prefix}_LTF has {len(lower_timeframes)} item(s) but {prefix}_HTF has {len(higher_timeframes)} item(s)."
        )

    stop_loss_values = _float_list_env(os.getenv(f"{prefix}_GOLD_STOP_LOSS_PIPS"), default_stop_loss_pips)
    take_profit_values = _float_list_env(os.getenv(f"{prefix}_GOLD_TAKE_PROFIT_PIPS"), default_take_profit_pips)
    pair_count = len(lower_timeframes)

    if len(stop_loss_values) == 1:
        stop_loss_values = [stop_loss_values[0] for _ in range(pair_count)]
    elif len(stop_loss_values) != pair_count:
        raise ValueError(
            f"Invalid {prefix}_GOLD_STOP_LOSS_PIPS list: expected 1 or {pair_count} item(s), got {len(stop_loss_values)}."
        )

    if len(take_profit_values) == 1:
        take_profit_values = [take_profit_values[0] for _ in range(pair_count)]
    elif len(take_profit_values) != pair_count:
        raise ValueError(
            f"Invalid {prefix}_GOLD_TAKE_PROFIT_PIPS list: expected 1 or {pair_count} item(s), got {len(take_profit_values)}."
        )

    return StrategyPreset(
        lower_timeframes=lower_timeframes,
        higher_timeframes=higher_timeframes,
        stop_loss_pips_list=stop_loss_values,
        take_profit_pips_list=take_profit_values,
    )


def load_gold_settings(env_path: str = ".env") -> GoldSettings:
    resolved_env_path = _resolve_env_path(env_path)
    if resolved_env_path.name != ".env" and resolved_env_path.suffix != ".env":
        raise ValueError(f"Only .env files are supported for runtime configuration. Received: {env_path}")
    if not resolved_env_path.exists():
        raise ValueError(f"Environment file not found: {resolved_env_path}")

    load_dotenv(str(resolved_env_path), override=True)
    backtest_speed_ms = _required_backtest_speed_ms()

    raw_strategy_names = []
    for item in os.getenv("GOLD_STRATEGY_NAMES", "trend_following,price_action,session_breakout").split(","):
        cleaned = item.split("#", 1)[0].strip().lower()
        if cleaned:
            raw_strategy_names.append(cleaned)
    strategy_presets: dict[str, StrategyPreset] = {
        "trend_following": _strategy_preset_env("TREND_FOLLOWING", "M15", "H1", 120.0, 250.0),
        "price_action": _strategy_preset_env("PRICE_ACTION", "M5", "M30", 80.0, 180.0),
        "session_breakout": _strategy_preset_env("SESSION_BREAKOUT", "M15", "H1", 100.0, 220.0),
    }
    unsupported = [name for name in raw_strategy_names if name not in strategy_presets]
    if unsupported:
        raise ValueError(
            "Unsupported GOLD_STRATEGY_NAMES value(s): "
            + ",".join(unsupported)
            + ". Supported: trend_following,price_action,session_breakout"
        )
    strategy_names = [name for name in raw_strategy_names if name in strategy_presets]
    if not strategy_names:
        strategy_names = ["trend_following"]
    primary_strategy = strategy_names[0] if strategy_names and strategy_names[0] in strategy_presets else "trend_following"
    primary_preset = strategy_presets[primary_strategy]
    return GoldSettings(
        ctrader_client_id=os.getenv("CTRADER_CLIENT_ID", ""),
        ctrader_client_secret=os.getenv("CTRADER_CLIENT_SECRET", ""),
        ctrader_access_token=os.getenv("CTRADER_ACCESS_TOKEN", ""),
        ctrader_refresh_token=os.getenv("CTRADER_REFRESH_TOKEN", ""),
        ctrader_account_id=_int_env("CTRADER_ACCOUNT_ID", 0),
        ctrader_live_account_id=_int_env("CTRADER_LIVE_ACCOUNT_ID", 0),
        ctrader_demo_account_id=_int_env("CTRADER_DEMO_ACCOUNT_ID", 0),
        ctrader_host=os.getenv("CTRADER_HOST", "live").strip().lower(),
        ctrader_request_timeout_seconds=max(2.0, _float_env("CTRADER_REQUEST_TIMEOUT_SECONDS", 12.0)),
        ctrader_connect_timeout_seconds=max(3.0, _float_env("CTRADER_CONNECT_TIMEOUT_SECONDS", 15.0)),
        ctrader_reconnect_attempts=max(1, _int_env("CTRADER_RECONNECT_ATTEMPTS", 5)),
        ctrader_reconnect_wait_seconds=max(0.0, _float_env("CTRADER_RECONNECT_WAIT_SECONDS", 30.0)),
        ctrader_heartbeat_interval_seconds=max(0.1, _float_env("CTRADER_HEARTBEAT_INTERVAL_SECONDS", 15.0)),
        ctrader_heartbeat_timeout_seconds=max(0.1, _float_env("CTRADER_HEARTBEAT_TIMEOUT_SECONDS", 30.0)),
        enable_trading=_bool_env(os.getenv("ENABLE_TRADING"), default=True),
        plot_enabled=_bool_env(os.getenv("PLOT_ENABLED"), default=True),
        symbols=[s.strip().upper() for s in os.getenv("SYMBOLS", "XAUUSD").split(",") if s.strip()],
        strategy_presets=strategy_presets,
        lower_timeframe=primary_preset.lower_timeframe,
        higher_timeframe=primary_preset.higher_timeframe,
        candle_count=_int_env("CANDLE_COUNT", 200),
        refresh_candle_count=max(2, _int_env("REFRESH_CANDLE_COUNT", 5)),
        poll_seconds=_float_env("POLL_SECONDS", 0.5),
        position_monitor_seconds=max(0.1, _float_env("POSITION_MONITOR_SECONDS", 5.0)),
        account_monitor_seconds=max(1.0, _float_env("ACCOUNT_MONITOR_SECONDS", 30.0)),
        chart_update_seconds=_float_env("CHART_UPDATE_SECONDS", 0.5),
        chart_width=max(8.0, _required_float_env("CHART_WIDTH")),
        chart_height=max(5.0, _required_float_env("CHART_HEIGHT")),
        chart_tp_marker_size=max(4.0, _float_env("CHART_TP_MARKER_SIZE", 10.0)),
        chart_sl_marker_size=max(4.0, _float_env("CHART_SL_MARKER_SIZE", 10.0)),
        chart_entry_marker_size=max(6.0, _float_env("CHART_ENTRY_MARKER_SIZE", 9.0)),
        chart_direction_marker_size=max(4.0, _float_env("CHART_DIRECTION_MARKER_SIZE", 10.0)),
        plot_ltf_candles=max(1, _required_int_env("PLOT_LTF_CANDLES")),
        plot_htf_candles=max(1, _required_int_env("PLOT_HTF_CANDLES")),
        max_cycles=_int_env("MAX_CYCLES", 0),
        log_level=os.getenv("LOG_LEVEL", "DEBUG").strip().upper(),
        strategy_eval_log_verbose=_bool_env(os.getenv("STRATEGY_EVAL_LOG_VERBOSE"), default=True),
        backtest_data_dir=os.getenv("BACKTEST_DATA_DIR", "backtest/data"),
        backtest_initial_balance=_float_env("BACKTEST_INITIAL_BALANCE", 10000.0),
        backtest_fixed_volume=_float_env("BACKTEST_FIXED_VOLUME", 0.0),
        backtest_results_subdir=os.getenv("BACKTEST_RESULTS_SUBDIR", "gold_bot"),
        backtest_speed_ms=backtest_speed_ms,
        backtest_lookback_value=max(0, _int_env("BACKTEST_LOOKBACK_VALUE", 0)),
        backtest_lookback_unit=os.getenv("BACKTEST_LOOKBACK_UNIT", "weeks").strip().lower(),
        backtest_use_broker_profile=_bool_env(
            os.getenv("BACKTEST_USE_BROKER_PROFILE", os.getenv("BACKTEST_USE_MT5_PROFILE")),
            default=True,
        ),
        backtest_simulate_margin_rejection=_bool_env(os.getenv("BACKTEST_SIMULATE_MARGIN_REJECTION"), default=True),
        backtest_volume_min=max(0.0, _float_env("BACKTEST_VOLUME_MIN", 0.01)),
        backtest_volume_max=max(0.01, _float_env("BACKTEST_VOLUME_MAX", 50.0)),
        backtest_volume_step=max(0.0001, _float_env("BACKTEST_VOLUME_STEP", 0.01)),
        backtest_max_volume_cap=max(0.0, _float_env("BACKTEST_MAX_VOLUME_CAP", 0.0)),
        backtest_default_contract_size=max(0.0, _float_env("BACKTEST_DEFAULT_CONTRACT_SIZE", 100.0)),
        backtest_default_leverage=max(1.0, _float_env("BACKTEST_DEFAULT_LEVERAGE", 100.0)),
        backtest_margin_available_ratio=min(1.0, max(0.1, _float_env("BACKTEST_MARGIN_AVAILABLE_RATIO", 0.95))),
        backtest_warn_volume_above=max(0.0, _float_env("BACKTEST_WARN_VOLUME_ABOVE", 5.0)),
        backtest_warn_equity_multiplier=max(1.0, _float_env("BACKTEST_WARN_EQUITY_MULTIPLIER", 20.0)),
        strategy_names=strategy_names,
        enable_multi_entry=_bool_env(os.getenv("GOLD_ENABLE_MULTI_ENTRY"), default=True),
        ladder_entries=_int_env("GOLD_LADDER_ENTRIES", 3),
        ladder_step_ratio=max(0.01, _float_env("GOLD_LADDER_STEP_RATIO", 1.2)),
        fixed_lot_size=max(0.0, _float_env("GOLD_FIXED_LOT_SIZE", 0.0)),
        stop_loss_pips=primary_preset.stop_loss_pips,
        take_profit_pips=primary_preset.take_profit_pips,
        ema_fast=_int_env("GOLD_EMA_FAST", 9),
        ema_slow=_int_env("GOLD_EMA_SLOW", 21),
        ema_trend_period=_int_env("GOLD_EMA_TREND_PERIOD", 200),
        max_daily_trades=_int_env("GOLD_MAX_DAILY_TRADES", 3),
        max_open_positions=max(1, _int_env("GOLD_MAX_OPEN_POSITIONS", 2)),
        trade_magic_number=_int_env("GOLD_TRADE_MAGIC_NUMBER", 550015),
        trade_comment_prefix=os.getenv("GOLD_TRADE_COMMENT_PREFIX", "gold-bot"),
        pip_size=max(0.00001, _float_env("GOLD_PIP_SIZE", 0.01)),
        price_action_cooldown_minutes=max(0.0, _float_env("PRICE_ACTION_COOLDOWN_MINUTES", 0.0)),
        position_db_path=os.getenv("CTRADER_POSITION_DB_PATH", os.getenv("MT5_POSITION_DB_PATH", "logs/gold_positions.sqlite3")),
        config_env_path=str(resolved_env_path),
    )
