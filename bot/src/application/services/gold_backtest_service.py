from __future__ import annotations

import logging
import math
import time
from collections import defaultdict
from typing import Any

import pandas as pd

from bot.src.application.services.gold_runner import GoldRunner
from bot.src.infrastructure.charting.live_plot import LiveChartRenderer
from bot.src.infrastructure.config.settings import GoldSettings
from bot.src.infrastructure.persistence.sqlite_store import GoldPositionStore

logger = logging.getLogger(__name__)


class GoldBacktestService:
    def __init__(self, settings: GoldSettings, runner: GoldRunner, chart_renderer: LiveChartRenderer, position_store: GoldPositionStore | None = None) -> None:
        self.settings = settings
        self.runner = runner
        self.chart_renderer = chart_renderer
        self.position_store = position_store or GoldPositionStore(getattr(settings, "position_db_path", "logs/gold_positions.sqlite3"))
        self._open_positions: list[dict[str, Any]] = []
        self._entry_direction_lock_by_pair: dict[str, str] = {}

    def run(
        self,
        lower_frame: pd.DataFrame,
        higher_frame: pd.DataFrame,
        source_name: str,
        artifact_stem: str,
        frames_by_timeframe: dict[str, pd.DataFrame] | None = None,
        strategy_timeframe_paths: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if frames_by_timeframe:
            return self._run_multi_timeframe(
                frames_by_timeframe=frames_by_timeframe,
                source_name=source_name,
                artifact_stem=artifact_stem,
                strategy_timeframe_paths=strategy_timeframe_paths or {},
            )

        working_lower = self._normalize_frame(lower_frame)
        working_higher = self._normalize_frame(higher_frame)
        working_lower, working_higher = self._apply_backtest_lookback(working_lower, working_higher)
        if working_lower.empty or working_higher.empty:
            self.chart_renderer.close()
            result = {
                "signals": [],
                "total_signals": 0,
                "wins": 0,
                "losses": 0,
                "breakeven": 0,
                "win_rate": 0.0,
                "start_balance": 0.0,
                "end_balance": 0.0,
                "balance_change": 0.0,
                "status": "completed_no_data",
                "processed_bars": 0,
            }
            self._log_backtest_exit_summary(result=result, source_name=source_name, artifact_stem=artifact_stem)
            return result

        lookback = max(300, int(getattr(self.settings, "ema_trend_period", 200)) * 3)
        higher_datetimes = pd.to_datetime(working_higher["datetime"], errors="coerce").tolist()
        higher_end = 0
        equity = float(getattr(self.settings, "backtest_initial_balance", 10000.0))
        initial_equity = equity
        equity_curve: list[dict[str, Any]] = []
        account_snapshot = {
            "login": "backtest",
            "balance": equity,
            "equity": equity,
            "margin": 0.0,
            "free_margin": equity,
            "currency": "USD",
        }
        account_change = {"delta_balance": 0.0, "delta_equity": 0.0}

        history: list[dict[str, Any]] = []
        markers: list[dict[str, Any]] = []
        markers_by_timeframe: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._open_positions = []
        self._entry_direction_lock_by_pair = {}
        wins = 0
        losses = 0
        breakeven = 0
        margin_rejections = 0
        volume_cap_events = 0
        sizing_rule = "default_minimum_volume"
        strategy_names = [name.strip().lower() for name in self.settings.strategy_names if str(name).strip()]
        if not strategy_names:
            strategy_names = ["trend_following"]
        self._log_enabled_strategy_configs(strategy_names)
        volume_samples: list[float] = []
        processed_bars = 0
        status = "completed"
        warned_high_volume = False
        warned_high_equity = False
        profile = self._resolve_backtest_profile()
        requested_symbol = self.settings.symbols[0] if self.settings.symbols else "XAUUSD"
        logger.info("🚀 Backtest service started | symbol=%s lower_tf=%s higher_tf=%s", requested_symbol, self.settings.lower_timeframe, self.settings.higher_timeframe)
        logger.info(
            "🧮 Backtest chart candle config | ltf_window=%s htf_window=%s lower_rows=%s higher_rows=%s",
            self.settings.plot_ltf_candles,
            self.settings.plot_htf_candles,
            len(working_lower),
            len(working_higher),
        )
        logger.info(
            "💼 Account check | balance=%.2f equity=%.2f margin=%.2f free_margin=%.2f",
            account_snapshot["balance"],
            account_snapshot["equity"],
            account_snapshot["margin"],
            account_snapshot["free_margin"],
        )
        try:
            for idx in range(len(working_lower)):
                processed_bars = idx + 1
                lower_start = max(0, idx - lookback + 1)
                lower_slab = working_lower.iloc[lower_start : idx + 1].copy()
                ts = lower_slab.iloc[-1]["datetime"] if "datetime" in lower_slab.columns else idx

                while higher_end < len(higher_datetimes) and higher_datetimes[higher_end] <= ts:
                    higher_end += 1
                higher_slab = self._higher_slab_by_index(working_higher, higher_end, lookback)

                logger.info(
                    "💼 Account check | balance=%.2f equity=%.2f margin=%.2f free_margin=%.2f",
                    account_snapshot["balance"],
                    account_snapshot["equity"],
                    account_snapshot["margin"],
                    account_snapshot["free_margin"],
                )
                prev_equity = equity
                equity, closed_positions = self._update_open_positions(ts=ts, close_price=float(lower_slab.iloc[-1]["close"]), equity=equity)
                if closed_positions:
                    account_change = {"delta_balance": equity - prev_equity, "delta_equity": equity - prev_equity}
                    account_snapshot = {
                        "login": "backtest",
                        "balance": equity,
                        "equity": equity,
                        "margin": 0.0,
                        "free_margin": equity,
                        "currency": "USD",
                    }
                logger.info("📍 Position monitor | open_positions=%s strategy=%s", len(self._open_positions), "backtest")

                candidates: list[Any] = []
                for strategy_name in strategy_names:
                    candidates.extend(
                        self._evaluate_candidates(
                            lower_slab,
                            higher_frame=higher_slab,
                            strategy_names=[strategy_name],
                            context_key=f"{strategy_name}:{self.settings.lower_timeframe}:{self.settings.higher_timeframe}:0",
                        )
                    )
                logger.info("📈 Cycle %s generated %s candidate signal(s)", idx + 1, len(candidates))
                for candidate in candidates:
                    strategy_key = str(getattr(candidate, "strategy", "")).strip().lower()
                    pair_lock_key = f"{strategy_key}:{str(self.settings.lower_timeframe).upper()}/{str(self.settings.higher_timeframe).upper()}#1"
                    if not self._direction_lock_allows_candidate(pair_lock_key, str(getattr(candidate, "direction", ""))):
                        logger.info(
                            "🔒 Backtest signal skipped (direction lock active) | lock_key=%s direction=%s",
                            pair_lock_key,
                            str(getattr(candidate, "direction", "")).lower(),
                        )
                        continue

                    preset = self._strategy_runtime_config(str(getattr(candidate, "strategy", "")))
                    stop_loss_pips = float(preset["stop_loss_pips"])
                    take_profit_pips = float(preset["take_profit_pips"])
                    raw_volume, sizing_rule = self._resolve_backtest_volume(equity)
                    volume, was_capped = self._normalize_backtest_volume(raw_volume, profile)
                    signal_key = f"{requested_symbol}:{candidate.strategy}:{candidate.direction}:{ts.isoformat()}"
                    logger.info(
                        "📣 Signal detected | key=%s symbol=%s strategy=%s direction=%s reason=%s candidate_price=%.5f ltf_ts=%s htf_ts=%s",
                        signal_key,
                        requested_symbol,
                        candidate.strategy,
                        candidate.direction,
                        candidate.reason,
                        float(candidate.price),
                        ts,
                        higher_slab.iloc[-1]["datetime"] if not higher_slab.empty and "datetime" in higher_slab.columns else "n/a",
                    )
                    if was_capped:
                        volume_cap_events += 1
                    volume_samples.append(volume)

                    if not warned_high_volume and float(getattr(self.settings, "backtest_warn_volume_above", 0.0)) > 0 and volume >= float(getattr(self.settings, "backtest_warn_volume_above", 0.0)):
                        warned_high_volume = True
                        logger.warning(
                            "⚠️ Backtest high volume warning | volume=%.2f rule=%s threshold=%.2f equity=%.2f",
                            volume,
                            sizing_rule,
                            float(getattr(self.settings, "backtest_warn_volume_above", 0.0)),
                            equity,
                        )

                    equity_warning_multiple = max(1.0, float(getattr(self.settings, "backtest_warn_equity_multiplier", 20.0)))
                    if not warned_high_equity and initial_equity > 0 and equity >= (initial_equity * equity_warning_multiple):
                        warned_high_equity = True
                        logger.warning(
                            "⚠️ Backtest high equity growth warning | equity=%.2f start=%.2f multiple=%.2f",
                            equity,
                            initial_equity,
                            equity / initial_equity,
                        )

                    if self._should_reject_for_margin(
                        entry_price=float(candidate.price),
                        volume=volume,
                        equity=equity,
                        profile=profile,
                    ):
                        margin_rejections += 1
                        logger.info(
                            "⛔ Backtest margin reject simulated | strategy=%s direction=%s price=%.5f volume=%.2f equity=%.2f",
                            candidate.strategy,
                            candidate.direction,
                            float(candidate.price),
                            volume,
                            equity,
                        )
                        continue

                    pnl = self._estimate_trade_outcome(
                        lower_frame=working_lower,
                        idx=idx,
                        direction=candidate.direction,
                        entry_price=float(candidate.price),
                        volume=volume,
                    )
                    if pnl > 0:
                        wins += 1
                    elif pnl < 0:
                        losses += 1
                    else:
                        breakeven += 1
                    prev_equity = equity
                    equity += pnl
                    account_snapshot = {
                        "login": "backtest",
                        "balance": equity,
                        "equity": equity,
                        "margin": 0.0,
                        "free_margin": equity,
                        "currency": "USD",
                    }
                    account_change = {"delta_balance": equity - prev_equity, "delta_equity": equity - prev_equity}
                    logger.info(
                        "💼 Account check | balance=%.2f equity=%.2f margin=%.2f free_margin=%.2f",
                        account_snapshot["balance"],
                        account_snapshot["equity"],
                        account_snapshot["margin"],
                        account_snapshot["free_margin"],
                    )
                    logger.info("📍 Position monitor | open_positions=%s strategy=%s", 0, candidate.strategy)
                    logger.info(
                        "✅ Signal confirmed for execution | key=%s symbol=%s strategy=%s direction=%s reason=%s market_price=%.5f",
                        signal_key,
                        requested_symbol,
                        candidate.strategy,
                        candidate.direction,
                        candidate.reason,
                        float(candidate.price),
                    )
                    ladder_entries = self.runner.trade_manager.build_ladder(
                        candidate,
                        stop_loss_pips=stop_loss_pips,
                        take_profit_pips=take_profit_pips,
                    )
                    for ladder_trade in ladder_entries:
                        trade_markers = self._build_trade_level_markers(
                            ts=ts,
                            direction=candidate.direction,
                            entry_price=float(ladder_trade.get("entry_price", candidate.price)),
                            stop_loss_pips=float(ladder_trade.get("stop_loss_pips", stop_loss_pips)),
                            take_profit_pips=float(ladder_trade.get("take_profit_pips", take_profit_pips)),
                        )
                        markers.extend(trade_markers)
                        markers_by_timeframe[str(self.settings.lower_timeframe).upper()].extend(trade_markers)
                        markers_by_timeframe[str(self.settings.higher_timeframe).upper()].extend(trade_markers)
                        entry_price = float(trade_markers[0]["price"]) if trade_markers else float(candidate.price)
                        sl_price = float(trade_markers[1]["price"]) if len(trade_markers) > 1 else float(candidate.price)
                        tp_price = float(trade_markers[2]["price"]) if len(trade_markers) > 2 else float(candidate.price)
                        logger.info(
                            "🧾 Trade execution request | key=%s symbol=%s strategy=%s level=%s direction=%s volume=%.2f market_price=%.5f request_entry=%.5f sl=%.5f tp=%.5f",
                            signal_key,
                            requested_symbol,
                            candidate.strategy,
                            int(ladder_trade.get("level", 1)),
                            candidate.direction,
                            float(volume),
                            float(candidate.price),
                            entry_price,
                            sl_price,
                            tp_price,
                        )
                        self._open_positions.append(
                            {
                                "position_key": f"backtest:{signal_key}:L{int(ladder_trade.get('level', 1))}",
                                "ticket": None,
                                "symbol": requested_symbol,
                                "direction": candidate.direction,
                                "volume": float(volume),
                                "entry_price": float(ladder_trade.get("entry_price", candidate.price)),
                                "stop_loss": float(entry_price) - (float(ladder_trade.get("stop_loss_pips", stop_loss_pips)) * max(0.00001, float(getattr(self.settings, "pip_size", 0.01)))) if candidate.direction.lower() == "buy" else float(entry_price) + (float(ladder_trade.get("stop_loss_pips", stop_loss_pips)) * max(0.00001, float(getattr(self.settings, "pip_size", 0.01)))),
                                "take_profit": float(entry_price) + (float(ladder_trade.get("take_profit_pips", take_profit_pips)) * max(0.00001, float(getattr(self.settings, "pip_size", 0.01)))) if candidate.direction.lower() == "buy" else float(entry_price) - (float(ladder_trade.get("take_profit_pips", take_profit_pips)) * max(0.00001, float(getattr(self.settings, "pip_size", 0.01)))),
                                "strategy": candidate.strategy,
                                "source": "backtest",
                                "is_external": 0,
                                "status": "open",
                                "opened_at": str(ts),
                                "level": int(ladder_trade.get("level", 1)),
                            }
                        )
                        self.position_store.upsert_position(
                            {
                                "position_key": f"backtest:{signal_key}:L{int(ladder_trade.get('level', 1))}",
                                "ticket": None,
                                "symbol": requested_symbol,
                                "direction": candidate.direction,
                                "volume": float(volume),
                                "entry_price": float(ladder_trade.get("entry_price", candidate.price)),
                                "stop_loss": float(entry_price) - (float(ladder_trade.get("stop_loss_pips", stop_loss_pips)) * max(0.00001, float(getattr(self.settings, "pip_size", 0.01)))) if candidate.direction.lower() == "buy" else float(entry_price) + (float(ladder_trade.get("stop_loss_pips", stop_loss_pips)) * max(0.00001, float(getattr(self.settings, "pip_size", 0.01)))),
                                "take_profit": float(entry_price) + (float(ladder_trade.get("take_profit_pips", take_profit_pips)) * max(0.00001, float(getattr(self.settings, "pip_size", 0.01)))) if candidate.direction.lower() == "buy" else float(entry_price) - (float(ladder_trade.get("take_profit_pips", take_profit_pips)) * max(0.00001, float(getattr(self.settings, "pip_size", 0.01)))),
                                "strategy": candidate.strategy,
                                "source": "backtest",
                                "is_external": 0,
                                "status": "open",
                                "opened_at": str(ts),
                            }
                        )

                    self._entry_direction_lock_by_pair[pair_lock_key] = str(candidate.direction).lower()
                    logger.info(
                        "🔒 Backtest direction lock set | lock_key=%s direction=%s",
                        pair_lock_key,
                        str(candidate.direction).lower(),
                    )
                    signal = {
                        "strategy": candidate.strategy,
                        "direction": candidate.direction,
                        "reason": candidate.reason,
                        "entry_price": round(float(candidate.price), 5),
                        "take_profit_pips": round(take_profit_pips, 2),
                        "stop_loss_pips": round(stop_loss_pips, 2),
                        "volume": round(volume, 2),
                        "sizing_rule": sizing_rule,
                        "level": 1,
                        "datetime": str(ts),
                    }
                    exit_targets = self.runner.trade_manager.update_exit_targets(
                        entry_price=float(candidate.price),
                        current_price=float(candidate.price),
                        direction=candidate.direction,
                        stop_loss_pips=stop_loss_pips,
                        take_profit_pips=take_profit_pips,
                        move_sl_pips=max(1.0, stop_loss_pips / 2.0),
                        move_tp_pips=max(1.0, take_profit_pips / 2.0),
                    )
                    signal["stop_loss"] = exit_targets["stop_loss"]
                    signal["take_profit"] = exit_targets["take_profit"]
                    history.append(signal)
                    signal_marker = {"datetime": ts, "price": float(candidate.price), "direction": candidate.direction, "type": "signal"}
                    markers.append(signal_marker)
                    markers_by_timeframe[str(self.settings.lower_timeframe).upper()].append(signal_marker)
                    markers_by_timeframe[str(self.settings.higher_timeframe).upper()].append(signal_marker)

                equity_curve.append({"datetime": ts, "equity": equity, "balance": equity})

                if self.settings.plot_enabled and (idx % self.settings.refresh_candle_count == 0 or idx == len(working_lower) - 1):
                    chart_frames = {
                        str(self.settings.lower_timeframe).upper(): lower_slab,
                        str(self.settings.higher_timeframe).upper(): higher_slab,
                    }
                    self.chart_renderer.render_timeframe_charts(
                        frames_by_timeframe=chart_frames,
                        symbol=self.settings.symbols[0] if self.settings.symbols else "XAUUSD",
                        markers_by_timeframe=markers_by_timeframe,
                        account_snapshot=account_snapshot,
                        account_change=account_change,
                        open_positions_count=len(self._open_positions),
                        open_positions=self._open_positions,
                        mode_label="backtest",
                        output_name_pattern=f"{artifact_stem}_{{timeframe}}_backtest_heikinashi.png",
                    )
                    delay_seconds = max(0.0, float(self.settings.backtest_speed_ms) / 1000.0)
                    if delay_seconds > 0:
                        time.sleep(delay_seconds)
        except KeyboardInterrupt:
            status = "canceled_by_user"
            logger.info("⏹️ Backtest interrupted by user.")
        finally:
            self.chart_renderer.close()

        logger.info(
            "Backtest equity summary | start=%.2f end=%.2f change=%+.2f wins=%s losses=%s breakeven=%s",
            initial_equity,
            equity,
            equity - initial_equity,
            wins,
            losses,
            breakeven,
        )
        closed = wins + losses + breakeven
        win_rate = (wins / closed * 100.0) if closed > 0 else 0.0
        avg_volume = (sum(volume_samples) / len(volume_samples)) if volume_samples else 0.0
        result = {
            "signals": history,
            "total_signals": len(history),
            "wins": wins,
            "losses": losses,
            "breakeven": breakeven,
            "win_rate": win_rate,
            "start_balance": initial_equity,
            "end_balance": equity,
            "balance_change": equity - initial_equity,
            "sizing_rule": sizing_rule,
            "avg_volume": avg_volume,
            "margin_rejections": margin_rejections,
            "volume_cap_events": volume_cap_events,
            "profile_source": str(profile.get("source", "defaults")),
            "status": status,
            "processed_bars": processed_bars,
        }
        self._log_backtest_exit_summary(result=result, source_name=source_name, artifact_stem=artifact_stem)
        return result

    def _run_multi_timeframe(
        self,
        frames_by_timeframe: dict[str, pd.DataFrame],
        source_name: str,
        artifact_stem: str,
        strategy_timeframe_paths: dict[str, Any],
    ) -> dict[str, Any]:
        strategy_names = [name.strip().lower() for name in self.settings.strategy_names if str(name).strip()]
        if not strategy_names:
            strategy_names = ["trend_following"]
        self._log_enabled_strategy_configs(strategy_names)

        strategy_configs = {name: self._strategy_runtime_configs(name) for name in strategy_names}
        normalized_frames: dict[str, pd.DataFrame] = {}
        for timeframe, frame in frames_by_timeframe.items():
            normalized_frames[str(timeframe).upper()] = self._normalize_frame(frame)
        normalized_frames = self._apply_backtest_lookback_multi(normalized_frames)

        if not normalized_frames:
            self.chart_renderer.close()
            result = {
                "signals": [],
                "total_signals": 0,
                "wins": 0,
                "losses": 0,
                "breakeven": 0,
                "win_rate": 0.0,
                "start_balance": 0.0,
                "end_balance": 0.0,
                "balance_change": 0.0,
                "status": "completed_no_data",
                "processed_bars": 0,
            }
            self._log_backtest_exit_summary(result=result, source_name=source_name, artifact_stem=artifact_stem)
            return result

        lookback = max(300, int(getattr(self.settings, "ema_trend_period", 200)) * 3)
        ltf_timeframes = {
            str(config["lower_timeframe"]).upper()
            for configs in strategy_configs.values()
            for config in configs
        }
        timestamps: set[pd.Timestamp] = set()
        for timeframe in ltf_timeframes:
            frame = normalized_frames.get(timeframe)
            if frame is None or frame.empty or "datetime" not in frame.columns:
                continue
            timestamps.update(pd.to_datetime(frame["datetime"], errors="coerce").dropna().tolist())

        ordered_timestamps = sorted(timestamps)
        if not ordered_timestamps:
            self.chart_renderer.close()
            result = {
                "signals": [],
                "total_signals": 0,
                "wins": 0,
                "losses": 0,
                "breakeven": 0,
                "win_rate": 0.0,
                "start_balance": 0.0,
                "end_balance": 0.0,
                "balance_change": 0.0,
                "status": "completed_no_data",
                "processed_bars": 0,
            }
            self._log_backtest_exit_summary(result=result, source_name=source_name, artifact_stem=artifact_stem)
            return result

        equity = float(getattr(self.settings, "backtest_initial_balance", 10000.0))
        initial_equity = equity
        account_snapshot = {
            "login": "backtest",
            "balance": equity,
            "equity": equity,
            "margin": 0.0,
            "free_margin": equity,
            "currency": "USD",
        }
        account_change = {"delta_balance": 0.0, "delta_equity": 0.0}

        history: list[dict[str, Any]] = []
        markers: list[dict[str, Any]] = []
        markers_by_timeframe: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._open_positions = []
        self._entry_direction_lock_by_pair = {}
        wins = 0
        losses = 0
        breakeven = 0
        margin_rejections = 0
        volume_cap_events = 0
        sizing_rule = "default_minimum_volume"
        volume_samples: list[float] = []
        processed_bars = 0
        status = "completed"
        warned_high_volume = False
        warned_high_equity = False
        profile = self._resolve_backtest_profile()
        requested_symbol = self.settings.symbols[0] if self.settings.symbols else "XAUUSD"
        equity_curve: list[dict[str, Any]] = []

        logger.info("🚀 Backtest service started | symbol=%s strategies=%s", requested_symbol, ",".join(strategy_names))
        logger.info(
            "🧮 Backtest chart candle config | ltf_window=%s htf_window=%s source_map=%s",
            self.settings.plot_ltf_candles,
            self.settings.plot_htf_candles,
            {k: str(v) for k, v in strategy_timeframe_paths.items()},
        )
        logger.info(
            "💼 Account check | balance=%.2f equity=%.2f margin=%.2f free_margin=%.2f",
            account_snapshot["balance"],
            account_snapshot["equity"],
            account_snapshot["margin"],
            account_snapshot["free_margin"],
        )

        try:
            for idx, ts in enumerate(ordered_timestamps):
                processed_bars = idx + 1

                strategy_slabs: list[dict[str, Any]] = []
                strategy_close_prices: dict[str, float] = {}
                for strategy_name in strategy_names:
                    for config in strategy_configs[strategy_name]:
                        pair_index = int(config.get("pair_index", 0))
                        lower_tf = str(config["lower_timeframe"]).upper()
                        higher_tf = str(config["higher_timeframe"]).upper()
                        lower_frame = normalized_frames.get(lower_tf, pd.DataFrame())
                        higher_frame = normalized_frames.get(higher_tf, pd.DataFrame())
                        if lower_frame.empty or higher_frame.empty:
                            continue

                        lower_slice = lower_frame[pd.to_datetime(lower_frame["datetime"], errors="coerce") <= ts]
                        higher_slice = higher_frame[pd.to_datetime(higher_frame["datetime"], errors="coerce") <= ts]
                        if lower_slice.empty or higher_slice.empty:
                            continue

                        lower_slab = lower_slice.tail(lookback).copy()
                        higher_slab = higher_slice.tail(lookback).copy()
                        if lower_slab.empty or higher_slab.empty:
                            continue

                        strategy_key = f"{strategy_name}|{pair_index}"
                        strategy_slabs.append(
                            {
                                "strategy_name": strategy_name,
                                "pair_index": pair_index,
                                "strategy_key": strategy_key,
                                "lower_timeframe": lower_tf,
                                "higher_timeframe": higher_tf,
                                "stop_loss_pips": float(config["stop_loss_pips"]),
                                "take_profit_pips": float(config["take_profit_pips"]),
                                "lower_slab": lower_slab,
                                "higher_slab": higher_slab,
                            }
                        )
                        strategy_close_prices[strategy_key] = float(lower_slab.iloc[-1]["close"])

                logger.info(
                    "💼 Account check | balance=%.2f equity=%.2f margin=%.2f free_margin=%.2f",
                    account_snapshot["balance"],
                    account_snapshot["equity"],
                    account_snapshot["margin"],
                    account_snapshot["free_margin"],
                )

                prev_equity = equity
                equity, closed_positions, closed_wins, closed_losses, closed_breakeven = self._update_open_positions_by_strategy(
                    ts=ts,
                    close_prices_by_strategy=strategy_close_prices,
                    equity=equity,
                )
                if closed_positions:
                    wins += closed_wins
                    losses += closed_losses
                    breakeven += closed_breakeven
                    account_change = {"delta_balance": equity - prev_equity, "delta_equity": equity - prev_equity}
                    account_snapshot = {
                        "login": "backtest",
                        "balance": equity,
                        "equity": equity,
                        "margin": 0.0,
                        "free_margin": equity,
                        "currency": "USD",
                    }

                logger.info("📍 Position monitor | open_positions=%s strategy=%s", len(self._open_positions), "backtest")

                total_candidates = 0
                for slab in strategy_slabs:
                    strategy_name = str(slab["strategy_name"])
                    strategy_key = str(slab["strategy_key"])
                    pair_index = int(slab["pair_index"])
                    lower_tf = str(slab["lower_timeframe"]).upper()
                    higher_tf = str(slab["higher_timeframe"]).upper()
                    stop_loss_pips = float(slab["stop_loss_pips"])
                    take_profit_pips = float(slab["take_profit_pips"])
                    lower_slab = slab["lower_slab"]
                    higher_slab = slab["higher_slab"]

                    candidates = self._evaluate_candidates(
                        lower_slab,
                        higher_frame=higher_slab,
                        strategy_names=[strategy_name],
                        context_key=f"{strategy_name}:{lower_tf}:{higher_tf}:{pair_index}",
                    )
                    total_candidates += len(candidates)

                    for candidate in candidates:
                        pair_lock_key = f"{str(candidate.strategy).strip().lower()}:{lower_tf}/{higher_tf}#{pair_index + 1}"
                        if not self._direction_lock_allows_candidate(pair_lock_key, str(getattr(candidate, "direction", ""))):
                            logger.info(
                                "🔒 Backtest signal skipped (direction lock active) | lock_key=%s direction=%s",
                                pair_lock_key,
                                str(getattr(candidate, "direction", "")).lower(),
                            )
                            continue

                        raw_volume, sizing_rule = self._resolve_backtest_volume(equity)
                        volume, was_capped = self._normalize_backtest_volume(raw_volume, profile)
                        pair_tag = f"{lower_tf}/{higher_tf}#{pair_index + 1}"
                        signal_key = f"{requested_symbol}:{candidate.strategy}:{pair_tag}:{candidate.direction}:{pd.Timestamp(ts).isoformat()}"
                        logger.info(
                            "📣 Signal detected | key=%s symbol=%s strategy=%s direction=%s reason=%s candidate_price=%.5f ltf=%s ltf_ts=%s htf=%s htf_ts=%s",
                            signal_key,
                            requested_symbol,
                            candidate.strategy,
                            candidate.direction,
                            candidate.reason,
                            float(candidate.price),
                            lower_tf,
                            lower_slab.iloc[-1]["datetime"],
                            higher_tf,
                            higher_slab.iloc[-1]["datetime"],
                        )
                        if was_capped:
                            volume_cap_events += 1
                        volume_samples.append(volume)

                        if not warned_high_volume and float(getattr(self.settings, "backtest_warn_volume_above", 0.0)) > 0 and volume >= float(getattr(self.settings, "backtest_warn_volume_above", 0.0)):
                            warned_high_volume = True
                            logger.warning(
                                "⚠️ Backtest high volume warning | volume=%.2f rule=%s threshold=%.2f equity=%.2f",
                                volume,
                                sizing_rule,
                                float(getattr(self.settings, "backtest_warn_volume_above", 0.0)),
                                equity,
                            )

                        equity_warning_multiple = max(1.0, float(getattr(self.settings, "backtest_warn_equity_multiplier", 20.0)))
                        if not warned_high_equity and initial_equity > 0 and equity >= (initial_equity * equity_warning_multiple):
                            warned_high_equity = True
                            logger.warning(
                                "⚠️ Backtest high equity growth warning | equity=%.2f start=%.2f multiple=%.2f",
                                equity,
                                initial_equity,
                                equity / initial_equity,
                            )

                        if self._should_reject_for_margin(
                            entry_price=float(candidate.price),
                            volume=volume,
                            equity=equity,
                            profile=profile,
                        ):
                            margin_rejections += 1
                            logger.info(
                                "⛔ Backtest margin reject simulated | strategy=%s direction=%s price=%.5f volume=%.2f equity=%.2f",
                                candidate.strategy,
                                candidate.direction,
                                float(candidate.price),
                                volume,
                                equity,
                            )
                            continue

                        logger.info(
                            "✅ Signal confirmed for execution | key=%s symbol=%s strategy=%s direction=%s reason=%s market_price=%.5f",
                            signal_key,
                            requested_symbol,
                            candidate.strategy,
                            candidate.direction,
                            candidate.reason,
                            float(candidate.price),
                        )
                        ladder_entries = self.runner.trade_manager.build_ladder(
                            candidate,
                            stop_loss_pips=stop_loss_pips,
                            take_profit_pips=take_profit_pips,
                        )
                        for ladder_trade in ladder_entries:
                            trade_markers = self._build_trade_level_markers(
                                ts=ts,
                                direction=candidate.direction,
                                entry_price=float(ladder_trade.get("entry_price", candidate.price)),
                                stop_loss_pips=float(ladder_trade.get("stop_loss_pips", stop_loss_pips)),
                                take_profit_pips=float(ladder_trade.get("take_profit_pips", take_profit_pips)),
                            )
                            markers.extend(trade_markers)
                            markers_by_timeframe[lower_tf].extend(trade_markers)
                            markers_by_timeframe[higher_tf].extend(trade_markers)
                            entry_price = float(trade_markers[0]["price"]) if trade_markers else float(candidate.price)
                            sl_price = float(trade_markers[1]["price"]) if len(trade_markers) > 1 else float(candidate.price)
                            tp_price = float(trade_markers[2]["price"]) if len(trade_markers) > 2 else float(candidate.price)
                            logger.info(
                                "🧾 Trade execution request | key=%s symbol=%s strategy=%s level=%s direction=%s volume=%.2f market_price=%.5f request_entry=%.5f sl=%.5f tp=%.5f",
                                signal_key,
                                requested_symbol,
                                candidate.strategy,
                                int(ladder_trade.get("level", 1)),
                                candidate.direction,
                                float(volume),
                                float(candidate.price),
                                entry_price,
                                sl_price,
                                tp_price,
                            )

                            position_key = f"backtest:{signal_key}:L{int(ladder_trade.get('level', 1))}"
                            position_payload = {
                                "position_key": position_key,
                                "ticket": None,
                                "symbol": requested_symbol,
                                "direction": candidate.direction,
                                "volume": float(volume),
                                "entry_price": float(ladder_trade.get("entry_price", candidate.price)),
                                "stop_loss": float(entry_price)
                                - (float(ladder_trade.get("stop_loss_pips", stop_loss_pips)) * max(0.00001, float(getattr(self.settings, "pip_size", 0.01))))
                                if candidate.direction.lower() == "buy"
                                else float(entry_price)
                                + (float(ladder_trade.get("stop_loss_pips", stop_loss_pips)) * max(0.00001, float(getattr(self.settings, "pip_size", 0.01)))),
                                "take_profit": float(entry_price)
                                + (float(ladder_trade.get("take_profit_pips", take_profit_pips)) * max(0.00001, float(getattr(self.settings, "pip_size", 0.01))))
                                if candidate.direction.lower() == "buy"
                                else float(entry_price)
                                - (float(ladder_trade.get("take_profit_pips", take_profit_pips)) * max(0.00001, float(getattr(self.settings, "pip_size", 0.01)))),
                                "strategy": strategy_key,
                                "strategy_name": candidate.strategy,
                                "pair_index": pair_index,
                                "timeframes": f"{lower_tf}/{higher_tf}",
                                "source": "backtest",
                                "is_external": 0,
                                "status": "open",
                                "opened_at": str(ts),
                                "level": int(ladder_trade.get("level", 1)),
                            }
                            self._open_positions.append(position_payload)
                            self.position_store.upsert_position(position_payload)

                        self._entry_direction_lock_by_pair[pair_lock_key] = str(candidate.direction).lower()
                        logger.info(
                            "🔒 Backtest direction lock set | lock_key=%s direction=%s",
                            pair_lock_key,
                            str(candidate.direction).lower(),
                        )

                        signal = {
                            "strategy": candidate.strategy,
                            "strategy_key": strategy_key,
                            "pair_index": pair_index,
                            "timeframes": f"{lower_tf}/{higher_tf}",
                            "direction": candidate.direction,
                            "reason": candidate.reason,
                            "entry_price": round(float(candidate.price), 5),
                            "take_profit_pips": round(take_profit_pips, 2),
                            "stop_loss_pips": round(stop_loss_pips, 2),
                            "volume": round(volume, 2),
                            "sizing_rule": sizing_rule,
                            "level": 1,
                            "datetime": str(ts),
                        }
                        exit_targets = self.runner.trade_manager.update_exit_targets(
                            entry_price=float(candidate.price),
                            current_price=float(candidate.price),
                            direction=candidate.direction,
                            stop_loss_pips=stop_loss_pips,
                            take_profit_pips=take_profit_pips,
                            move_sl_pips=max(1.0, stop_loss_pips / 2.0),
                            move_tp_pips=max(1.0, take_profit_pips / 2.0),
                        )
                        signal["stop_loss"] = exit_targets["stop_loss"]
                        signal["take_profit"] = exit_targets["take_profit"]
                        history.append(signal)
                        signal_marker = {"datetime": ts, "price": float(candidate.price), "direction": candidate.direction, "type": "signal"}
                        markers.append(signal_marker)
                        markers_by_timeframe[lower_tf].append(signal_marker)
                        markers_by_timeframe[higher_tf].append(signal_marker)

                if total_candidates:
                    logger.info("📈 Cycle %s generated %s candidate signal(s)", idx + 1, total_candidates)

                equity_curve.append({"datetime": ts, "equity": equity, "balance": equity})

                if self.settings.plot_enabled and (idx % self.settings.refresh_candle_count == 0 or idx == len(ordered_timestamps) - 1):
                    chart_frames: dict[str, pd.DataFrame] = {}
                    for slab in strategy_slabs:
                        ltf = str(slab["lower_timeframe"]).upper()
                        htf = str(slab["higher_timeframe"]).upper()
                        lower_slab = slab["lower_slab"]
                        higher_slab = slab["higher_slab"]
                        if isinstance(lower_slab, pd.DataFrame) and not lower_slab.empty:
                            existing_ltf = chart_frames.get(ltf, pd.DataFrame())
                            if existing_ltf.empty or len(lower_slab) >= len(existing_ltf):
                                chart_frames[ltf] = lower_slab
                        if isinstance(higher_slab, pd.DataFrame) and not higher_slab.empty:
                            existing_htf = chart_frames.get(htf, pd.DataFrame())
                            if existing_htf.empty or len(higher_slab) >= len(existing_htf):
                                chart_frames[htf] = higher_slab
                    if chart_frames:
                        self.chart_renderer.render_timeframe_charts(
                            frames_by_timeframe=chart_frames,
                            symbol=self.settings.symbols[0] if self.settings.symbols else "XAUUSD",
                            markers_by_timeframe=markers_by_timeframe,
                            account_snapshot=account_snapshot,
                            account_change=account_change,
                            open_positions_count=len(self._open_positions),
                            open_positions=self._open_positions,
                            mode_label="backtest",
                            output_name_pattern=f"{artifact_stem}_{{timeframe}}_backtest_heikinashi.png",
                        )
                        delay_seconds = max(0.0, float(self.settings.backtest_speed_ms) / 1000.0)
                        if delay_seconds > 0:
                            time.sleep(delay_seconds)
        except KeyboardInterrupt:
            status = "canceled_by_user"
            logger.info("⏹️ Backtest interrupted by user.")
        finally:
            self.chart_renderer.close()

        logger.info(
            "Backtest equity summary | start=%.2f end=%.2f change=%+.2f wins=%s losses=%s breakeven=%s",
            initial_equity,
            equity,
            equity - initial_equity,
            wins,
            losses,
            breakeven,
        )
        closed = wins + losses + breakeven
        win_rate = (wins / closed * 100.0) if closed > 0 else 0.0
        avg_volume = (sum(volume_samples) / len(volume_samples)) if volume_samples else 0.0
        result = {
            "signals": history,
            "total_signals": len(history),
            "wins": wins,
            "losses": losses,
            "breakeven": breakeven,
            "win_rate": win_rate,
            "start_balance": initial_equity,
            "end_balance": equity,
            "balance_change": equity - initial_equity,
            "sizing_rule": sizing_rule,
            "avg_volume": avg_volume,
            "margin_rejections": margin_rejections,
            "volume_cap_events": volume_cap_events,
            "profile_source": str(profile.get("source", "defaults")),
            "status": status,
            "processed_bars": processed_bars,
        }
        self._log_backtest_exit_summary(result=result, source_name=source_name, artifact_stem=artifact_stem)
        return result

    def _log_backtest_exit_summary(self, result: dict[str, Any], source_name: str, artifact_stem: str) -> None:
        logger.info(
            "📊 Backtest run status | status=%s source=%s artifact=%s",
            result.get("status", "unknown"),
            source_name,
            artifact_stem,
        )
        table_rows = [
            {"metric": "status", "value": str(result.get("status", "unknown"))},
            {"metric": "profile_source", "value": str(result.get("profile_source", "defaults"))},
            {"metric": "sizing_rule", "value": str(result.get("sizing_rule", "default_minimum_volume"))},
            {"metric": "avg_volume", "value": f"{float(result.get('avg_volume', 0.0)):.2f}"},
            {"metric": "volume_cap_events", "value": str(result.get("volume_cap_events", 0))},
            {"metric": "margin_rejections", "value": str(result.get("margin_rejections", 0))},
            {"metric": "processed_bars", "value": str(result.get("processed_bars", 0))},
            {"metric": "signals", "value": str(result.get("total_signals", 0))},
            {"metric": "wins", "value": str(result.get("wins", 0))},
            {"metric": "losses", "value": str(result.get("losses", 0))},
            {"metric": "breakeven", "value": str(result.get("breakeven", 0))},
            {"metric": "win_rate_pct", "value": f"{float(result.get('win_rate', 0.0)):.2f}"},
            {"metric": "start_balance", "value": f"{float(result.get('start_balance', 0.0)):.2f}"},
            {"metric": "end_balance", "value": f"{float(result.get('end_balance', 0.0)):.2f}"},
            {"metric": "balance_change", "value": f"{float(result.get('balance_change', 0.0)):+.2f}"},
            {
                "metric": "positions_passed_failed",
                "value": f"{int(result.get('wins', 0))}/{int(result.get('losses', 0))}",
            },
        ]
        logger.info("📋 Backtest summary table:\n%s", self._format_table(table_rows, ["metric", "value"]))

    def _format_table(self, rows: list[dict[str, Any]], columns: list[str]) -> str:
        normalized = []
        widths: dict[str, int] = {}
        for col in columns:
            widths[col] = len(col)
        for row in rows:
            normalized_row: dict[str, str] = {}
            for col in columns:
                value = row.get(col, "")
                text = f"{value}"
                normalized_row[col] = text
                widths[col] = max(widths[col], len(text))
            normalized.append(normalized_row)

        header = " | ".join(col.ljust(widths[col]) for col in columns)
        separator = "-+-".join("-" * widths[col] for col in columns)
        lines = [header, separator]
        for row in normalized:
            lines.append(" | ".join(row[col].ljust(widths[col]) for col in columns))
        return "\n".join(lines)

    def _evaluate_candidates(
        self,
        frame: pd.DataFrame,
        higher_frame: pd.DataFrame | None,
        strategy_names: list[str],
        context_key: str,
    ) -> list[Any]:
        try:
            return self.runner.evaluate_candidates(
                frame,
                higher_frame=higher_frame,
                strategy_names=strategy_names,
                context_key=context_key,
            )
        except TypeError:
            return self.runner.evaluate_candidates(
                frame,
                higher_frame=higher_frame,
                strategy_names=strategy_names,
            )

    def _normalize_frame(self, frame: pd.DataFrame) -> pd.DataFrame:
        working = frame.copy()
        if "datetime" in working.columns:
            working["datetime"] = pd.to_datetime(working["datetime"], errors="coerce")
            working = working.dropna(subset=["datetime"]).sort_values("datetime").reset_index(drop=True)
        return working

    def _higher_slab_by_index(self, higher_frame: pd.DataFrame, end_index: int, lookback: int) -> pd.DataFrame:
        if end_index <= 0:
            return higher_frame.iloc[:1].copy()
        start_index = max(0, end_index - lookback)
        return higher_frame.iloc[start_index:end_index].copy()

    def _apply_backtest_lookback(self, lower_frame: pd.DataFrame, higher_frame: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
        lookback_value = int(getattr(self.settings, "backtest_lookback_value", 0))
        lookback_unit = str(getattr(self.settings, "backtest_lookback_unit", "weeks")).lower()
        if lookback_value <= 0:
            return lower_frame, higher_frame
        if "datetime" not in lower_frame.columns or "datetime" not in higher_frame.columns:
            logger.warning("Backtest lookback requested but datetime column is missing; skipping lookback filter")
            return lower_frame, higher_frame

        lower_max = pd.to_datetime(lower_frame["datetime"], errors="coerce").max()
        higher_max = pd.to_datetime(higher_frame["datetime"], errors="coerce").max()
        anchor = max(lower_max, higher_max)
        if pd.isna(anchor):
            return lower_frame, higher_frame

        if lookback_unit == "months":
            cutoff = anchor - pd.DateOffset(months=lookback_value)
        else:
            cutoff = anchor - pd.Timedelta(weeks=lookback_value)

        filtered_lower = lower_frame[pd.to_datetime(lower_frame["datetime"], errors="coerce") >= cutoff].copy()
        filtered_higher = higher_frame[pd.to_datetime(higher_frame["datetime"], errors="coerce") >= cutoff].copy()
        logger.info(
            "Backtest lookback applied | unit=%s value=%s cutoff=%s | lower_rows=%s->%s higher_rows=%s->%s",
            lookback_unit,
            lookback_value,
            cutoff,
            len(lower_frame),
            len(filtered_lower),
            len(higher_frame),
            len(filtered_higher),
        )
        return filtered_lower.reset_index(drop=True), filtered_higher.reset_index(drop=True)

    def _apply_backtest_lookback_multi(self, frames_by_timeframe: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
        lookback_value = int(getattr(self.settings, "backtest_lookback_value", 0))
        lookback_unit = str(getattr(self.settings, "backtest_lookback_unit", "weeks")).lower()
        if lookback_value <= 0:
            return {tf: frame.reset_index(drop=True) for tf, frame in frames_by_timeframe.items()}

        anchor_candidates: list[pd.Timestamp] = []
        for frame in frames_by_timeframe.values():
            if "datetime" not in frame.columns:
                continue
            dt_series = pd.to_datetime(frame["datetime"], errors="coerce")
            if dt_series.empty:
                continue
            max_dt = dt_series.max()
            if pd.notna(max_dt):
                anchor_candidates.append(max_dt)
        if not anchor_candidates:
            return {tf: frame.reset_index(drop=True) for tf, frame in frames_by_timeframe.items()}

        anchor = max(anchor_candidates)
        if lookback_unit == "months":
            cutoff = anchor - pd.DateOffset(months=lookback_value)
        else:
            cutoff = anchor - pd.Timedelta(weeks=lookback_value)

        output: dict[str, pd.DataFrame] = {}
        for timeframe, frame in frames_by_timeframe.items():
            if "datetime" not in frame.columns:
                output[timeframe] = frame.reset_index(drop=True)
                continue
            dt_series = pd.to_datetime(frame["datetime"], errors="coerce")
            filtered = frame[dt_series >= cutoff].copy()
            output[timeframe] = filtered.reset_index(drop=True)

        logger.info(
            "Backtest lookback applied | unit=%s value=%s cutoff=%s | timeframes=%s",
            lookback_unit,
            lookback_value,
            cutoff,
            ",".join(sorted(output.keys())),
        )
        return output

    def _build_trade_level_markers(self, ts: Any, direction: str, entry_price: float, stop_loss_pips: float | None = None, take_profit_pips: float | None = None) -> list[dict[str, Any]]:
        pip_size = max(0.00001, float(getattr(self.settings, "pip_size", 0.01)))
        sl_pips = max(1.0, float(stop_loss_pips if stop_loss_pips is not None else getattr(self.settings, "stop_loss_pips", 120.0)))
        tp_pips = max(1.0, float(take_profit_pips if take_profit_pips is not None else getattr(self.settings, "take_profit_pips", 250.0)))

        is_buy = direction.lower() == "buy"
        sl_price = entry_price - (sl_pips * pip_size) if is_buy else entry_price + (sl_pips * pip_size)
        tp_price = entry_price + (tp_pips * pip_size) if is_buy else entry_price - (tp_pips * pip_size)

        return [
            {"datetime": ts, "price": entry_price, "direction": direction, "type": "entry", "label": ""},
            {"datetime": ts, "price": sl_price, "direction": direction, "type": "sl", "label": ""},
            {"datetime": ts, "price": tp_price, "direction": direction, "type": "tp", "label": ""},
        ]

    def _direction_lock_allows_candidate(self, lock_key: str, candidate_direction: str) -> bool:
        key = str(lock_key).strip().lower()
        direction = str(candidate_direction).strip().lower()
        if not key or direction not in {"buy", "sell"}:
            return True

        locked = str(self._entry_direction_lock_by_pair.get(key, "")).strip().lower()
        if not locked:
            return True

        if locked != direction:
            self._entry_direction_lock_by_pair.pop(key, None)
            logger.info(
                "🔓 Backtest direction lock released by opposite signal | lock_key=%s previous=%s incoming=%s",
                key,
                locked,
                direction,
            )
            return True

        return False

    def _update_open_positions(self, ts: Any, close_price: float, equity: float) -> tuple[float, int]:
        remaining_positions: list[dict[str, Any]] = []
        closed_positions = 0
        for position in self._open_positions:
            direction = str(position.get("direction", "buy")).lower()
            entry_price = float(position.get("entry_price", 0.0))
            stop_loss = float(position.get("stop_loss", 0.0))
            take_profit = float(position.get("take_profit", 0.0))
            volume = max(0.0, float(position.get("volume", 0.0)))
            pip_size = max(0.00001, float(getattr(self.settings, "pip_size", 0.01)))
            if direction == "buy":
                if close_price >= take_profit:
                    exit_price = take_profit
                    pnl = (exit_price - entry_price) / pip_size * max(0.0, volume)
                    equity += pnl
                    closed_positions += 1
                    self.position_store.upsert_position({**position, "status": "closed", "closed_at": str(ts), "close_price": exit_price})
                elif close_price <= stop_loss:
                    exit_price = stop_loss
                    pnl = (exit_price - entry_price) / pip_size * max(0.0, volume)
                    equity += pnl
                    closed_positions += 1
                    self.position_store.upsert_position({**position, "status": "closed", "closed_at": str(ts), "close_price": exit_price})
                else:
                    remaining_positions.append(position)
            else:
                if close_price <= take_profit:
                    exit_price = take_profit
                    pnl = (entry_price - exit_price) / pip_size * max(0.0, volume)
                    equity += pnl
                    closed_positions += 1
                    self.position_store.upsert_position({**position, "status": "closed", "closed_at": str(ts), "close_price": exit_price})
                elif close_price >= stop_loss:
                    exit_price = stop_loss
                    pnl = (entry_price - exit_price) / pip_size * max(0.0, volume)
                    equity += pnl
                    closed_positions += 1
                    self.position_store.upsert_position({**position, "status": "closed", "closed_at": str(ts), "close_price": exit_price})
                else:
                    remaining_positions.append(position)
        self._open_positions = remaining_positions
        return equity, closed_positions

    def _update_open_positions_by_strategy(self, ts: Any, close_prices_by_strategy: dict[str, float], equity: float) -> tuple[float, int, int, int, int]:
        remaining_positions: list[dict[str, Any]] = []
        closed_positions = 0
        wins = 0
        losses = 0
        breakeven = 0
        pip_size = max(0.00001, float(getattr(self.settings, "pip_size", 0.01)))

        for position in self._open_positions:
            strategy_key = str(position.get("strategy", "")).strip().lower()
            close_price = close_prices_by_strategy.get(strategy_key)
            if close_price is None:
                remaining_positions.append(position)
                continue

            direction = str(position.get("direction", "buy")).lower()
            entry_price = float(position.get("entry_price", 0.0))
            stop_loss = float(position.get("stop_loss", 0.0))
            take_profit = float(position.get("take_profit", 0.0))
            volume = max(0.0, float(position.get("volume", 0.0)))

            is_closed = False
            pnl = 0.0
            exit_price = None

            if direction == "buy":
                if close_price >= take_profit:
                    exit_price = take_profit
                    pnl = (exit_price - entry_price) / pip_size * volume
                    is_closed = True
                elif close_price <= stop_loss:
                    exit_price = stop_loss
                    pnl = (exit_price - entry_price) / pip_size * volume
                    is_closed = True
            else:
                if close_price <= take_profit:
                    exit_price = take_profit
                    pnl = (entry_price - exit_price) / pip_size * volume
                    is_closed = True
                elif close_price >= stop_loss:
                    exit_price = stop_loss
                    pnl = (entry_price - exit_price) / pip_size * volume
                    is_closed = True

            if not is_closed:
                remaining_positions.append(position)
                continue

            equity += pnl
            closed_positions += 1
            if pnl > 0:
                wins += 1
            elif pnl < 0:
                losses += 1
            else:
                breakeven += 1
            self.position_store.upsert_position({**position, "status": "closed", "closed_at": str(ts), "close_price": float(exit_price)})

        self._open_positions = remaining_positions
        return equity, closed_positions, wins, losses, breakeven

    def _resolve_backtest_volume(self, equity: float) -> tuple[float, str]:
        fixed_backtest_volume = max(0.0, float(getattr(self.settings, "backtest_fixed_volume", 0.0)))
        if fixed_backtest_volume > 0:
            return max(0.01, fixed_backtest_volume), "backtest_fixed_volume"

        fixed_lot_size = max(0.0, float(getattr(self.settings, "fixed_lot_size", 0.0)))
        if fixed_lot_size > 0:
            return max(0.01, fixed_lot_size), "gold_fixed_lot_size"

        return 0.01, "default_minimum_volume"

    def _strategy_runtime_configs(self, strategy_name: str) -> list[dict[str, Any]]:
        key = str(strategy_name).strip().lower()
        preset = self.settings.strategy_presets.get(key) if hasattr(self.settings, "strategy_presets") else None
        if preset is None:
            return [
                {
                    "pair_index": 0,
                    "lower_timeframe": self.settings.lower_timeframe,
                    "higher_timeframe": self.settings.higher_timeframe,
                    "stop_loss_pips": float(self.settings.stop_loss_pips),
                    "take_profit_pips": float(self.settings.take_profit_pips),
                }
            ]
        if not hasattr(preset, "pair_configs"):
            return [
                {
                    "pair_index": 0,
                    "lower_timeframe": str(getattr(preset, "lower_timeframe", self.settings.lower_timeframe)).upper(),
                    "higher_timeframe": str(getattr(preset, "higher_timeframe", self.settings.higher_timeframe)).upper(),
                    "stop_loss_pips": float(getattr(preset, "stop_loss_pips", self.settings.stop_loss_pips)),
                    "take_profit_pips": float(getattr(preset, "take_profit_pips", self.settings.take_profit_pips)),
                }
            ]
        return [
            {
                "pair_index": int(pair.get("pair_index", 0)),
                "lower_timeframe": str(pair["lower_timeframe"]).upper(),
                "higher_timeframe": str(pair["higher_timeframe"]).upper(),
                "stop_loss_pips": float(pair["stop_loss_pips"]),
                "take_profit_pips": float(pair["take_profit_pips"]),
            }
            for pair in preset.pair_configs()
        ]

    def _strategy_runtime_config(self, strategy_name: str) -> dict[str, Any]:
        return self._strategy_runtime_configs(strategy_name)[0]

    def _log_enabled_strategy_configs(self, strategy_names: list[str]) -> None:
        env_path = str(getattr(self.settings, "config_env_path", ".env"))
        logger.info(
            "🧩 Strategy setup overview | env=%s enabled_strategies=%s",
            env_path,
            ",".join(strategy_names),
        )
        for strategy_name in strategy_names:
            config_source = "strategy_preset" if strategy_name in getattr(self.settings, "strategy_presets", {}) else "fallback_defaults"
            for preset in self._strategy_runtime_configs(strategy_name):
                logger.info(
                    "🧩 Strategy setup | enabled=true strategy=%s source=%s pair_index=%s ltf=%s htf=%s sl_pips=%.2f tp_pips=%.2f multi_entry=%s ladder_entries=%s ladder_step_ratio=%.2f fixed_lot=%.2f",
                    strategy_name,
                    config_source,
                    int(preset.get("pair_index", 0)),
                    str(preset["lower_timeframe"]),
                    str(preset["higher_timeframe"]),
                    float(preset["stop_loss_pips"]),
                    float(preset["take_profit_pips"]),
                    bool(self.settings.enable_multi_entry),
                    int(self.settings.ladder_entries),
                    float(self.settings.ladder_step_ratio),
                    float(self.settings.fixed_lot_size),
                )

    def _normalize_backtest_volume(self, raw_volume: float, profile: dict[str, float | str]) -> tuple[float, bool]:
        min_volume = max(0.01, float(profile.get("volume_min", 0.01) or 0.01))
        max_volume = max(min_volume, float(profile.get("volume_max", 50.0) or 50.0))
        hard_cap = max(0.0, float(getattr(self.settings, "backtest_max_volume_cap", 0.0)))
        if hard_cap > 0:
            max_volume = min(max_volume, hard_cap)
        step = max(0.0001, float(profile.get("volume_step", 0.01) or 0.01))

        stepped = math.floor(max(0.0, float(raw_volume)) / step) * step
        bounded = max(min_volume, min(max_volume, stepped))
        was_capped = bounded + 1e-9 < float(raw_volume)
        precision = self._step_precision(step)
        return round(bounded, precision), was_capped

    def _resolve_backtest_profile(self) -> dict[str, float | str]:
        profile: dict[str, float | str] = {
            "source": "defaults",
            "volume_min": max(0.01, float(getattr(self.settings, "backtest_volume_min", 0.01))),
            "volume_max": max(0.01, float(getattr(self.settings, "backtest_volume_max", 50.0))),
            "volume_step": max(0.0001, float(getattr(self.settings, "backtest_volume_step", 0.01))),
            "contract_size": max(0.0, float(getattr(self.settings, "backtest_default_contract_size", 100.0))),
            "leverage": max(1.0, float(getattr(self.settings, "backtest_default_leverage", 100.0))),
            "margin_initial": 0.0,
        }
        if not bool(getattr(self.settings, "backtest_use_broker_profile", True)):
            logger.info("Backtest broker profile | source=defaults (broker profile disabled)")
            return profile

        connector = GoldCTraderConnector(self.settings)
        if not connector.connect():
            logger.info("Backtest broker profile | source=defaults (ctrader unavailable)")
            return profile

        try:
            requested_symbol = self.settings.symbols[0] if self.settings.symbols else "XAUUSD"
            resolved_symbol = connector.resolve_symbol(requested_symbol) or requested_symbol
            symbol_meta = connector.symbol_info(resolved_symbol) or {}
            account_meta = connector.account_info() or {}
            if float(symbol_meta.get("volume_min", 0.0) or 0.0) > 0:
                profile["volume_min"] = float(symbol_meta.get("volume_min", profile["volume_min"]))
            if float(symbol_meta.get("volume_max", 0.0) or 0.0) > 0:
                profile["volume_max"] = float(symbol_meta.get("volume_max", profile["volume_max"]))
            if float(symbol_meta.get("volume_step", 0.0) or 0.0) > 0:
                profile["volume_step"] = float(symbol_meta.get("volume_step", profile["volume_step"]))
            if float(symbol_meta.get("trade_contract_size", 0.0) or 0.0) > 0:
                profile["contract_size"] = float(symbol_meta.get("trade_contract_size", profile["contract_size"]))
            if float(symbol_meta.get("margin_initial", 0.0) or 0.0) > 0:
                profile["margin_initial"] = float(symbol_meta.get("margin_initial", 0.0))
            if float(account_meta.get("leverage", 0.0) or 0.0) > 0:
                profile["leverage"] = float(account_meta.get("leverage", profile["leverage"]))
            profile["source"] = "ctrader"
            logger.info(
                "Backtest broker profile | source=ctrader symbol=%s volume_min=%.2f volume_max=%.2f volume_step=%.4f contract_size=%.2f leverage=%.0f margin_initial=%.2f",
                resolved_symbol,
                float(profile.get("volume_min", 0.01)),
                float(profile.get("volume_max", 50.0)),
                float(profile.get("volume_step", 0.01)),
                float(profile.get("contract_size", 100.0)),
                float(profile.get("leverage", 100.0)),
                float(profile.get("margin_initial", 0.0)),
            )
        finally:
            connector.disconnect()
        return profile

    def _estimate_required_margin(self, entry_price: float, volume: float, profile: dict[str, float | str]) -> float:
        margin_initial = max(0.0, float(profile.get("margin_initial", 0.0) or 0.0))
        if margin_initial > 0:
            return margin_initial * max(0.0, volume)
        leverage = max(1.0, float(profile.get("leverage", 100.0) or 100.0))
        contract_size = max(0.0, float(profile.get("contract_size", 100.0) or 100.0))
        notional = max(0.0, entry_price) * max(0.0, volume) * contract_size
        return notional / leverage if leverage > 0 else 0.0

    def _should_reject_for_margin(self, entry_price: float, volume: float, equity: float, profile: dict[str, float | str]) -> bool:
        if not bool(getattr(self.settings, "backtest_simulate_margin_rejection", True)):
            return False
        required_margin = self._estimate_required_margin(entry_price=entry_price, volume=volume, profile=profile)
        if required_margin <= 0:
            return False
        margin_available_ratio = min(1.0, max(0.1, float(getattr(self.settings, "backtest_margin_available_ratio", 0.95))))
        available_margin = max(0.0, equity * margin_available_ratio)
        return required_margin > available_margin

    def _step_precision(self, step: float) -> int:
        text = f"{step:.8f}".rstrip("0")
        if "." not in text:
            return 2
        return max(2, len(text.split(".", 1)[1]))

    def _estimate_trade_outcome(
        self,
        lower_frame: pd.DataFrame,
        idx: int,
        direction: str,
        entry_price: float,
        volume: float,
    ) -> float:
        if idx + 1 >= len(lower_frame):
            return 0.0

        next_close = float(lower_frame.iloc[idx + 1]["close"])
        pip_size = max(0.00001, float(getattr(self.settings, "pip_size", 0.01)))
        sl_pips = max(1.0, float(getattr(self.settings, "stop_loss_pips", 120.0)))
        tp_pips = max(1.0, float(getattr(self.settings, "take_profit_pips", 250.0)))

        if direction.lower() == "buy":
            move = next_close - entry_price
        else:
            move = entry_price - next_close

        pips = move / pip_size
        bounded_pips = max(-sl_pips, min(tp_pips, pips))
        return bounded_pips * max(0.0, float(volume))
