from __future__ import annotations

import argparse
import csv
import logging
import os
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path

from bot.src.application.services.gold_backtest_service import GoldBacktestService
from bot.src.application.services.gold_live_service import GoldLiveService
from bot.src.application.services.gold_runner import GoldRunner
from bot.src.infrastructure.charting.live_plot import LiveChartRenderer
from bot.src.infrastructure.config.settings import load_gold_settings
from bot.src.infrastructure.ctrader.connector import GoldCTraderConnector
from bot.src.infrastructure.logging.runtime import GoldQueueLoggingManager
from bot.src.infrastructure.market_data.csv_loader import load_ohlc_frame, resolve_backtest_timeframe_file
from bot.src.infrastructure.persistence.sqlite_store import GoldPositionStore

logger = logging.getLogger(__name__)


def _slugify(value: str) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() else "_" for ch in value.strip())
    collapsed = "_".join(part for part in cleaned.split("_") if part)
    return collapsed or "unknown"


def _strategy_label(strategy_names: list[str]) -> str:
    if not strategy_names:
        return "none"
    return "-".join(_slugify(name) for name in strategy_names)


def _backtest_artifact_stem(source_name: str, strategy_names: list[str]) -> str:
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    return f"{_slugify(source_name)}__{_strategy_label(strategy_names)}__{run_id}"


def _resolve_backtest_data_source(args: object, settings: object) -> str:
    def _normalize(value: str | Path) -> str:
        return str(Path(value)).replace("\\", "/")

    def _as_directory(value: str | Path) -> Path:
        path = Path(value)
        return path.parent if path.exists() and path.is_file() else path

    def _prefer_existing(candidate: str | Path) -> str | None:
        path = _as_directory(candidate)
        if path.exists():
            return _normalize(path)
        return None

    cli_backtest_dir = getattr(args, "backtest_data_dir", None)
    if cli_backtest_dir:
        return _normalize(_as_directory(cli_backtest_dir))

    cli_data = getattr(args, "data", None)
    if cli_data and cli_data != "backtest/data":
        return _normalize(_as_directory(cli_data))

    configured = getattr(settings, "backtest_data_dir", None)
    if configured:
        candidate = _as_directory(configured)
        if candidate.exists():
            return _normalize(candidate)

    for bundled in [
        Path("bot/backtest/data"),
        Path("gold_bot/backtest/data"),
        Path("backtest/data"),
    ]:
        resolved = _prefer_existing(bundled)
        if resolved is not None:
            return resolved

    return _normalize(Path("bot/backtest/data"))


def _write_backtest_report(results_dir: Path, artifact_stem: str, source_name: str, result: dict[str, object]) -> Path:
    results_dir.mkdir(parents=True, exist_ok=True)
    summary_path = results_dir / f"{artifact_stem}_summary.txt"
    csv_path = results_dir / f"{artifact_stem}_signals.csv"

    summary_lines = [
        f"source={source_name}",
        f"total_signals={result.get('total_signals', 0)}",
        f"signals_written={len(result.get('signals', []))}",
        f"wins={result.get('wins', 0)}",
        f"losses={result.get('losses', 0)}",
        f"breakeven={result.get('breakeven', 0)}",
        f"win_rate={float(result.get('win_rate', 0.0)):.2f}",
        f"start_balance={float(result.get('start_balance', 0.0)):.2f}",
        f"end_balance={float(result.get('end_balance', 0.0)):.2f}",
        f"balance_change={float(result.get('balance_change', 0.0)):+.2f}",
    ]
    summary_path.write_text("\n".join(summary_lines) + "\n", encoding="utf-8")

    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["strategy", "direction", "reason", "entry_price", "take_profit_pips", "stop_loss_pips", "level"])
        writer.writeheader()
        for signal in result.get("signals", []):
            writer.writerow({key: signal.get(key, "") for key in writer.fieldnames})

    return summary_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Gold trading bot")
    parser.add_argument("--env", default="bot/.env", help="Path to the environment file")
    parser.add_argument("--mode", choices=["live", "backtest"], default="live")
    plot_group = parser.add_mutually_exclusive_group()
    plot_group.add_argument("--plot", dest="plot_enabled", action="store_true")
    plot_group.add_argument("--no-plot", dest="plot_enabled", action="store_false")
    trade_group = parser.add_mutually_exclusive_group()
    trade_group.add_argument("--trade", dest="enable_trading", action="store_true")
    trade_group.add_argument("--no-trade", dest="enable_trading", action="store_false")
    parser.add_argument("--symbols", help="Comma-separated symbols")
    parser.add_argument("--strategy", help="Gold strategy name or comma-separated strategy list")
    parser.add_argument("--max-cycles", dest="max_cycles", type=int)
    parser.add_argument("--refresh-seconds", dest="chart_update_seconds", type=float)
    parser.add_argument("--backtest-data-dir", dest="backtest_data_dir")
    parser.add_argument("--backtest-initial-balance", dest="backtest_initial_balance", type=float)
    parser.add_argument("--backtest-fixed-volume", dest="backtest_fixed_volume", type=float)
    parser.add_argument("--backtest-results-subdir", dest="backtest_results_subdir")
    parser.add_argument("--speed", dest="backtest_speed", help="Backtest replay delay, for example 100ms")
    parser.add_argument("--backtest-lookback-value", dest="backtest_lookback_value", type=int, help="Backtest lookback window size")
    parser.add_argument(
        "--backtest-lookback-unit",
        dest="backtest_lookback_unit",
        choices=["weeks", "months"],
        help="Backtest lookback unit",
    )
    parser.add_argument("--data", dest="data", default=None, help="Directory with CSV candle data")
    parser.set_defaults(plot_enabled=None)
    parser.set_defaults(enable_trading=None)
    args = parser.parse_args()

    if args.plot_enabled is not None:
        os.environ["PLOT_ENABLED"] = "true" if args.plot_enabled else "false"
    if args.enable_trading is not None:
        os.environ["ENABLE_TRADING"] = "true" if args.enable_trading else "false"
    if args.symbols:
        os.environ["SYMBOLS"] = args.symbols
    if args.strategy:
        os.environ["GOLD_STRATEGY_NAMES"] = args.strategy
    if args.chart_update_seconds is not None:
        os.environ["CHART_UPDATE_SECONDS"] = str(args.chart_update_seconds)
    if args.max_cycles is not None:
        os.environ["MAX_CYCLES"] = str(args.max_cycles)
    if args.backtest_data_dir:
        os.environ["BACKTEST_DATA_DIR"] = args.backtest_data_dir
    if args.backtest_initial_balance is not None:
        os.environ["BACKTEST_INITIAL_BALANCE"] = str(args.backtest_initial_balance)
    if args.backtest_fixed_volume is not None:
        os.environ["BACKTEST_FIXED_VOLUME"] = str(args.backtest_fixed_volume)
    if args.backtest_results_subdir:
        os.environ["BACKTEST_RESULTS_SUBDIR"] = args.backtest_results_subdir
    if args.backtest_speed:
        os.environ["BACKTEST_SPEED"] = args.backtest_speed
    if args.backtest_lookback_value is not None:
        os.environ["BACKTEST_LOOKBACK_VALUE"] = str(args.backtest_lookback_value)
    if args.backtest_lookback_unit:
        os.environ["BACKTEST_LOOKBACK_UNIT"] = args.backtest_lookback_unit

    try:
        settings = load_gold_settings(args.env)
    except ValueError as exc:
        logger.error("%s", exc)
        return 1
    GoldQueueLoggingManager.configure(Path("logs"), settings.log_level)
    logger.info("Configuration loaded from env file: %s", settings.config_env_path)
    runner = GoldRunner(settings)

    if args.mode == "backtest":
        data_source = _resolve_backtest_data_source(args, settings)
        data_path = Path(data_source)

        strategy_names = [name.strip().lower() for name in settings.strategy_names if str(name).strip()]
        if not strategy_names:
            strategy_names = ["trend_following"]

        symbol = settings.symbols[0] if settings.symbols else "XAUUSD"
        strategy_pairs: dict[str, list[dict[str, object]]] = {}
        for strategy_name in strategy_names:
            preset = settings.strategy_presets.get(strategy_name)
            if preset is None:
                strategy_pairs[strategy_name] = [
                    {
                        "lower_timeframe": str(settings.lower_timeframe).upper(),
                        "higher_timeframe": str(settings.higher_timeframe).upper(),
                    }
                ]
            else:
                strategy_pairs[strategy_name] = [
                    {
                        "lower_timeframe": str(pair["lower_timeframe"]).upper(),
                        "higher_timeframe": str(pair["higher_timeframe"]).upper(),
                    }
                    for pair in preset.pair_configs()
                ]

        frames_by_timeframe: dict[str, object] = {}
        timeframe_paths: dict[str, Path] = {}
        try:
            for pair_list in strategy_pairs.values():
                for pair in pair_list:
                    lower_tf = str(pair["lower_timeframe"])
                    higher_tf = str(pair["higher_timeframe"])
                    for timeframe in (lower_tf, higher_tf):
                        if timeframe in frames_by_timeframe:
                            continue
                        resolved_path = resolve_backtest_timeframe_file(data_source=data_path, symbol=symbol, timeframe=timeframe)
                        frames_by_timeframe[timeframe] = load_ohlc_frame(resolved_path)
                        timeframe_paths[timeframe] = resolved_path
        except FileNotFoundError as exc:
            logger.error("%s", exc)
            return 1
        except ValueError as exc:
            logger.error("%s", exc)
            return 1

        required_columns = {"open", "high", "low", "close"}
        for timeframe, frame in frames_by_timeframe.items():
            if not required_columns.issubset(frame.columns):
                logger.error("CSV file missing OHLC columns for timeframe %s", timeframe)
                return 1

        primary_strategy = strategy_names[0]
        primary_pair = strategy_pairs[primary_strategy][0]
        primary_lower_tf = str(primary_pair["lower_timeframe"])
        primary_higher_tf = str(primary_pair["higher_timeframe"])
        lower_frame = frames_by_timeframe[primary_lower_tf]
        higher_frame = frames_by_timeframe[primary_higher_tf]
        lower_path = timeframe_paths[primary_lower_tf]
        higher_path = timeframe_paths[primary_higher_tf]

        chart_renderer = LiveChartRenderer(
            output_dir=Path("logs"),
            interactive=True,
            chart_width=settings.chart_width,
            chart_height=settings.chart_height,
            max_lower_candles=settings.plot_ltf_candles,
            max_higher_candles=settings.plot_htf_candles,
            strategy_names=settings.strategy_names,
            ema_fast=settings.ema_fast,
            ema_slow=settings.ema_slow,
            ema_trend_period=settings.ema_trend_period,
            tp_marker_size=settings.chart_tp_marker_size,
            sl_marker_size=settings.chart_sl_marker_size,
            entry_marker_size=settings.chart_entry_marker_size,
            direction_marker_size=settings.chart_direction_marker_size,
        )
        source_name = lower_path.stem if lower_path.suffix else lower_path.name
        artifact_stem = _backtest_artifact_stem(source_name=source_name, strategy_names=settings.strategy_names)
        result = GoldBacktestService(settings=settings, runner=runner, chart_renderer=chart_renderer).run(
            lower_frame=lower_frame,
            higher_frame=higher_frame,
            source_name=source_name,
            artifact_stem=artifact_stem,
            frames_by_timeframe=frames_by_timeframe,
            strategy_timeframe_paths=timeframe_paths,
        )
        results_dir = Path("backtest/results") / settings.backtest_results_subdir
        summary_path = _write_backtest_report(results_dir, artifact_stem, source_name, result)
        source_labels: list[str] = []
        for strategy_name in strategy_names:
            for pair in strategy_pairs[strategy_name]:
                lower_tf = str(pair["lower_timeframe"])
                higher_tf = str(pair["higher_timeframe"])
                source_labels.append(
                    f"{strategy_name}:LTF={timeframe_paths[lower_tf]}({lower_tf}) HTF={timeframe_paths[higher_tf]}({higher_tf})"
                )
        logger.info(
            "Backtest sources | %s | lookback=%s %s | signals=%s",
            " ; ".join(source_labels),
            settings.backtest_lookback_value,
            settings.backtest_lookback_unit,
            result["total_signals"],
        )
        logger.info(
            "📊 Backtest completion summary | status=%s strategy=%s wins=%s losses=%s breakeven=%s win_rate=%.2f%% start_balance=%.2f end_balance=%.2f balance_change=%+.2f",
            result.get("status", "completed"),
            ",".join(settings.strategy_names),
            result.get("wins", 0),
            result.get("losses", 0),
            result.get("breakeven", 0),
            float(result.get("win_rate", 0.0)),
            float(result.get("start_balance", 0.0)),
            float(result.get("end_balance", 0.0)),
            float(result.get("balance_change", 0.0)),
        )
        logger.info("Backtest artifact prefix: %s", artifact_stem)
        logger.info("Backtest summary saved to %s", summary_path)
        return 0

    logger.info(
        "🚀 Gold bot initialized | log_level=%s trading_enabled=%s plot_enabled=%s symbols=%s strategies=%s",
        settings.log_level,
        settings.enable_trading,
        settings.plot_enabled,
        settings.symbols,
        settings.strategy_names,
    )
    connector = GoldCTraderConnector(settings)
    position_store = GoldPositionStore(settings.position_db_path)
    chart_renderer = LiveChartRenderer(
        output_dir=Path("logs"),
        interactive=True,
        chart_width=settings.chart_width,
        chart_height=settings.chart_height,
        max_lower_candles=settings.plot_ltf_candles,
        max_higher_candles=settings.plot_htf_candles,
        strategy_names=settings.strategy_names,
        ema_fast=settings.ema_fast,
        ema_slow=settings.ema_slow,
        ema_trend_period=settings.ema_trend_period,
        tp_marker_size=settings.chart_tp_marker_size,
        sl_marker_size=settings.chart_sl_marker_size,
        entry_marker_size=settings.chart_entry_marker_size,
        direction_marker_size=settings.chart_direction_marker_size,
    )
    live_service = GoldLiveService(
        settings=settings,
        runner=runner,
        connector=connector,
        position_store=position_store,
        chart_renderer=chart_renderer,
    )

    previous_sigint = signal.getsignal(signal.SIGINT)
    previous_sigterm = signal.getsignal(signal.SIGTERM)

    def _handle_shutdown_signal(signum: int, _frame: object) -> None:
        name = "SIGTERM" if signum == signal.SIGTERM else "SIGINT"
        logger.warning("Received %s; requesting graceful shutdown", name)
        live_service.request_shutdown(reason=name)

    signal.signal(signal.SIGINT, _handle_shutdown_signal)
    signal.signal(signal.SIGTERM, _handle_shutdown_signal)
    try:
        return live_service.run()
    finally:
        signal.signal(signal.SIGINT, previous_sigint)
        signal.signal(signal.SIGTERM, previous_sigterm)


if __name__ == "__main__":
    sys.exit(main())
