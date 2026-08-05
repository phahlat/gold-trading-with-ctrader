from __future__ import annotations

import logging
import math
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

from bot.src.application.services.gold_runner import GoldRunner
from bot.src.infrastructure.charting.live_plot import LiveChartRenderer
from bot.src.infrastructure.config.settings import GoldSettings
from bot.src.infrastructure.ctrader.connector import GoldCTraderConnector
from bot.src.infrastructure.persistence.sqlite_store import GoldPositionStore

logger = logging.getLogger(__name__)


class GoldLiveService:
    def __init__(
        self,
        settings: GoldSettings,
        runner: GoldRunner,
        connector: GoldCTraderConnector,
        position_store: GoldPositionStore,
        chart_renderer: LiveChartRenderer,
    ) -> None:
        self.settings = settings
        self.runner = runner
        self.connector = connector
        self.position_store = position_store
        self.chart_renderer = chart_renderer
        self._signal_markers_by_timeframe: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._last_signal_keys: set[str] = set()
        self._strategy_cooldowns: dict[str, datetime] = {}
        self._daily_trade_count: dict[str, int] = defaultdict(int)
        self._daily_risk_pct: dict[str, float] = defaultdict(float)
        self._last_account_snapshot: dict[str, Any] | None = None
        self._last_account_change: dict[str, Any] | None = None
        self._last_positions: list[dict[str, Any]] = []
        self._equity_curve: list[dict[str, Any]] = []
        self._tick_trail: list[dict[str, Any]] = []
        self._session_started_at: datetime | None = None
        self._session_start_balance: float = 0.0
        self._session_start_equity: float = 0.0
        self._orders_attempted: int = 0
        self._orders_filled: int = 0
        self._orders_rejected: int = 0

    def _log_strategy_eval(self, message: str, *args: Any) -> None:
        if bool(getattr(self.settings, "strategy_eval_log_verbose", True)):
            logger.info(message, *args)

    def run(self) -> int:
        requested_symbol = self.settings.symbols[0] if self.settings.symbols else "XAUUSD"
        if not self.connector.connect():
            logger.error("❌ Live mode requires cTrader connectivity. Refusing CSV fallback.")
            return 1

        symbol = self.connector.resolve_symbol(requested_symbol)
        if not symbol:
            broker_sample = self.connector.broker_symbols()[:10]
            logger.error(
                "❌ Requested symbol '%s' was not found on broker symbols. Sample=%s",
                requested_symbol,
                broker_sample,
            )
            return 1

        resolution_mode = "alias" if symbol.upper() != requested_symbol.strip().upper() else "exact"
        logger.info(
            "🔎 Symbol verification | requested=%s resolved=%s mode=%s",
            requested_symbol,
            symbol,
            resolution_mode,
        )

        logger.info(
            "🚀 Live service started | symbol=%s lower_tf=%s higher_tf=%s poll=%.2fs",
            symbol,
            self.settings.lower_timeframe,
            self.settings.higher_timeframe,
            self.settings.poll_seconds,
        )
        strategy_names = [name.strip().lower() for name in self.settings.strategy_names if str(name).strip()]
        if not strategy_names:
            strategy_names = ["trend_following"]
        self._log_enabled_strategy_configs(strategy_names)
        logger.info(
            "🧮 Chart candle config | ltf_window=%s htf_window=%s base_candle_count=%s fetch_ltf=%s fetch_htf=%s",
            self.settings.plot_ltf_candles,
            self.settings.plot_htf_candles,
            self.settings.candle_count,
            self._bars_to_pull(self.settings.lower_timeframe),
            self._bars_to_pull(self.settings.higher_timeframe),
        )
        self._session_started_at = datetime.utcnow()
        opening_account = self.connector.account_info() or {}
        self._session_start_balance = float(opening_account.get("balance", 0.0))
        self._session_start_equity = float(opening_account.get("equity", self._session_start_balance))

        cycle = 0
        last_position_monitor = 0.0
        last_account_monitor = 0.0
        last_plot_update = 0.0
        run_status = "completed"

        try:
            while True:
                if self.settings.max_cycles > 0 and cycle >= self.settings.max_cycles:
                    run_status = "completed_max_cycles"
                    break
                try:
                    frames_by_timeframe: dict[str, pd.DataFrame] = {}
                    missing_frames: set[str] = set()
                    unique_timeframes = self._active_strategy_timeframes(strategy_names)
                    for timeframe in unique_timeframes:
                        frame = self._pull_frame(symbol, timeframe)
                        if frame.empty:
                            missing_frames.add(timeframe)
                        frames_by_timeframe[timeframe] = frame

                    if missing_frames:
                        logger.warning(
                            "⚠️ Missing cTrader bars for %s timeframe(s): %s. Retrying.",
                            symbol,
                            ",".join(sorted(missing_frames)),
                        )
                        time.sleep(max(0.2, self.settings.poll_seconds))
                        continue

                    total_candidates = 0
                    for strategy_name in strategy_names:
                        for pair_config in self._strategy_runtime_configs(strategy_name):
                            pair_index = int(pair_config.get("pair_index", 0))
                            lower_tf = str(pair_config["lower_timeframe"])
                            higher_tf = str(pair_config["higher_timeframe"])
                            lower_frame = self._closed_candle_frame(
                                frames_by_timeframe.get(lower_tf, pd.DataFrame()),
                                lower_tf,
                            )
                            higher_frame = self._closed_candle_frame(
                                frames_by_timeframe.get(higher_tf, pd.DataFrame()),
                                higher_tf,
                            )
                            if lower_frame.empty or higher_frame.empty:
                                self._log_strategy_eval(
                                    "🧪 Strategy evaluation result | status=failed symbol=%s strategy=%s pair_index=%s ltf=%s htf=%s reason=missing_frame",
                                    symbol,
                                    strategy_name,
                                    pair_index,
                                    lower_tf,
                                    higher_tf,
                                )
                                continue

                            decision_context = self._decision_context_for_candidate(
                                strategy_name=strategy_name,
                                lower_frame=lower_frame,
                                higher_frame=higher_frame,
                                lower_timeframe=lower_tf,
                                higher_timeframe=higher_tf,
                            )
                            ltf_ts = lower_frame.iloc[-1]["datetime"] if "datetime" in lower_frame.columns and not lower_frame.empty else "n/a"
                            htf_ts = higher_frame.iloc[-1]["datetime"] if "datetime" in higher_frame.columns and not higher_frame.empty else "n/a"
                            self._log_strategy_eval(
                                "🧪 Strategy evaluation | status=running symbol=%s strategy=%s pair_index=%s ltf=%s ltf_ts=%s htf=%s htf_ts=%s decision_data=%s",
                                symbol,
                                strategy_name,
                                pair_index,
                                lower_tf,
                                ltf_ts,
                                higher_tf,
                                htf_ts,
                                decision_context,
                            )

                            htf_bias_context = self._higher_timeframe_bias_context(higher_frame)
                            logger.info(
                                "🧭 HTF confirmation | symbol=%s strategy=%s timeframe=%s bias=%s close=%.5f ema_fast=%.5f ema_slow=%.5f ema_trend=%.5f",
                                symbol,
                                strategy_name,
                                higher_tf,
                                htf_bias_context["bias"],
                                float(htf_bias_context["close"]),
                                float(htf_bias_context["ema_fast"]),
                                float(htf_bias_context["ema_slow"]),
                                float(htf_bias_context["ema_trend"]),
                            )

                            candidates = self.runner.evaluate_candidates(
                                lower_frame,
                                higher_frame=higher_frame,
                                strategy_names=[strategy_name],
                            )
                            if not candidates:
                                self._log_strategy_eval(
                                    "🧪 Strategy evaluation result | status=failed symbol=%s strategy=%s pair_index=%s ltf=%s ltf_ts=%s htf=%s htf_ts=%s reason=no_signal_passed_strategy_or_htf_filter",
                                    symbol,
                                    strategy_name,
                                    pair_index,
                                    lower_tf,
                                    ltf_ts,
                                    higher_tf,
                                    htf_ts,
                                )
                            else:
                                unique_reasons = sorted({str(getattr(item, "reason", "")) for item in candidates})
                                directions = sorted({str(getattr(item, "direction", "")) for item in candidates})
                                self._log_strategy_eval(
                                    "🧪 Strategy evaluation result | status=successful symbol=%s strategy=%s pair_index=%s ltf=%s ltf_ts=%s htf=%s htf_ts=%s candidates=%s directions=%s reasons=%s",
                                    symbol,
                                    strategy_name,
                                    pair_index,
                                    lower_tf,
                                    ltf_ts,
                                    higher_tf,
                                    htf_ts,
                                    len(candidates),
                                    directions,
                                    unique_reasons,
                                )
                            total_candidates += len(candidates)
                            for candidate in candidates:
                                self._handle_candidate(
                                    symbol=symbol,
                                    candidate=candidate,
                                    lower_frame=lower_frame,
                                    higher_frame=higher_frame,
                                    lower_timeframe=lower_tf,
                                    higher_timeframe=higher_tf,
                                    stop_loss_pips=float(pair_config["stop_loss_pips"]),
                                    take_profit_pips=float(pair_config["take_profit_pips"]),
                                    pair_index=pair_index,
                                )
                    if total_candidates:
                        logger.info("📈 Cycle %s generated %s candidate signal(s)", cycle + 1, total_candidates)

                    now = time.monotonic()
                    if now - last_position_monitor >= self.settings.position_monitor_seconds:
                        try:
                            self._monitor_positions(symbol)
                        except Exception as exc:
                            logger.exception("⚠️ Position monitor failed | symbol=%s", symbol)
                            if not self._recover_from_monitor_failure(symbol, exc):
                                logger.error("❌ Position monitor recovery failed; will retry on next cycle | symbol=%s", symbol)
                        last_position_monitor = now

                    if now - last_account_monitor >= self.settings.account_monitor_seconds:
                        try:
                            self._monitor_account()
                        except Exception:
                            logger.exception("⚠️ Account monitor failed | symbol=%s", symbol)
                        last_account_monitor = now

                    if self.settings.plot_enabled and now - last_plot_update >= self.settings.chart_update_seconds:
                        primary = self._strategy_runtime_config(strategy_names[0])
                        chart_ltf = str(primary["lower_timeframe"])
                        chart_htf = str(primary["higher_timeframe"])
                        lower_frame = frames_by_timeframe.get(chart_ltf, pd.DataFrame())
                        higher_frame = frames_by_timeframe.get(chart_htf, pd.DataFrame())
                        if lower_frame.empty or higher_frame.empty:
                            cycle += 1
                            time.sleep(max(0.05, self.settings.poll_seconds))
                            continue
                        tick_price = self.connector.current_price(symbol, "buy")
                        ticker_point = None
                        if tick_price is not None:
                            ticker_point = {
                                "datetime": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S"),
                                "price": float(tick_price),
                                "direction": "buy",
                            }
                            self._tick_trail.append(ticker_point)
                            if len(self._tick_trail) > 40:
                                self._tick_trail = self._tick_trail[-40:]
                        logger.info(
                            "🛰️ Plot update input | ltf_rows=%s htf_rows=%s",
                            len(lower_frame),
                            len(higher_frame),
                        )
                        chart_path = self.chart_renderer.render_dual_timeframe(
                            lower_frame=lower_frame,
                            higher_frame=higher_frame,
                            symbol=symbol,
                            lower_timeframe=chart_ltf,
                            higher_timeframe=chart_htf,
                            lower_markers=self._signal_markers_by_timeframe[chart_ltf],
                            higher_markers=self._signal_markers_by_timeframe[chart_htf],
                            account_snapshot=self._last_account_snapshot,
                            account_change=self._last_account_change,
                            open_positions_count=len(self._last_positions),
                            equity_curve=self._equity_curve,
                            ticker_point=ticker_point,
                            ticker_trail=self._tick_trail,
                            mode_label="live",
                            output_name=f"{symbol}_dual_live_heikinashi.png",
                        )
                        logger.info("🖼️ Live chart refreshed: %s", chart_path)
                        last_plot_update = now

                    cycle += 1
                    time.sleep(max(0.05, self.settings.poll_seconds))
                except (TimeoutError, RuntimeError) as exc:
                    if not self._is_connection_error(exc):
                        raise
                    recovered_symbol = self._recover_connection(requested_symbol)
                    if not recovered_symbol:
                        run_status = "failed_connection_retries_exhausted"
                        break
                    symbol = recovered_symbol
                    continue
        except KeyboardInterrupt:
            run_status = "canceled_by_user"
            logger.info("⏹️ Interrupted by user.")
        finally:
            self._log_exit_summary(symbol=symbol, run_status=run_status)
            self.chart_renderer.close()
            self.connector.disconnect()
        return 0

    def _pull_frame(self, symbol: str, timeframe: str) -> pd.DataFrame:
        bars = self.connector.get_rates(symbol=symbol, timeframe=timeframe, count=self._bars_to_pull(timeframe))
        if not bars:
            return pd.DataFrame()
        frame = pd.DataFrame(bars)
        frame["datetime"] = pd.to_datetime(frame["time"], unit="s", utc=True).dt.tz_convert(None)
        frame = frame.sort_values("datetime").reset_index(drop=True)
        required = ["datetime", "open", "high", "low", "close"]
        frame = frame[required].copy()

        # Guard against malformed broker bars (for example zeroed lows) that can
        # collapse plot scaling and skew signal calculations.
        for column in ["open", "high", "low", "close"]:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        valid = (
            frame[["open", "high", "low", "close"]].notna().all(axis=1)
            & (frame[["open", "high", "low", "close"]] > 0).all(axis=1)
            & (frame["high"] >= frame[["open", "close", "low"]].max(axis=1))
            & (frame["low"] <= frame[["open", "close", "high"]].min(axis=1))
        )
        dropped = int((~valid).sum())
        if dropped > 0:
            logger.warning("Filtered %s malformed %s bars for %s", dropped, timeframe, symbol)
        sanitized = frame.loc[valid].reset_index(drop=True)
        return self._closed_candle_frame(sanitized, timeframe)

    def _closed_candle_frame(self, frame: pd.DataFrame, timeframe: str) -> pd.DataFrame:
        if frame.empty or "datetime" not in frame.columns:
            return frame
        if len(frame) < 2:
            return frame

        timeframe_delta = self._timeframe_delta(timeframe)
        if timeframe_delta is None:
            # Unknown timeframe format, keep conservative behavior by dropping latest bar.
            return frame.iloc[:-1].reset_index(drop=True)

        last_ts = pd.to_datetime(frame.iloc[-1]["datetime"], errors="coerce")
        if pd.isna(last_ts):
            return frame.iloc[:-1].reset_index(drop=True)

        now_utc_naive = datetime.now(timezone.utc).replace(tzinfo=None)
        if last_ts + timeframe_delta > now_utc_naive:
            return frame.iloc[:-1].reset_index(drop=True)
        return frame.reset_index(drop=True)

    def _timeframe_delta(self, timeframe: str) -> timedelta | None:
        value = str(timeframe or "").strip().upper()
        if len(value) < 2:
            return None
        unit = value[0]
        amount_text = value[1:]
        if not amount_text.isdigit():
            return None
        amount = int(amount_text)
        if amount <= 0:
            return None
        if unit == "M":
            return timedelta(minutes=amount)
        if unit == "H":
            return timedelta(hours=amount)
        if unit == "D":
            return timedelta(days=amount)
        if unit == "W":
            return timedelta(weeks=amount)
        return None

    def _is_connection_error(self, exc: Exception) -> bool:
        if isinstance(exc, TimeoutError):
            return True
        text = str(exc)
        return "cTrader" in text or "Timed out waiting for" in text

    def _recover_connection(self, requested_symbol: str) -> str | None:
        attempts = max(1, int(getattr(self.settings, "ctrader_reconnect_attempts", 5)))
        wait_seconds = max(0.0, float(getattr(self.settings, "ctrader_reconnect_wait_seconds", 30.0)))
        logger.warning(
            "⚠️ cTrader connectivity issue detected. Starting reconnect sequence | attempts=%s wait_seconds=%.1f",
            attempts,
            wait_seconds,
        )

        for attempt in range(1, attempts + 1):
            logger.warning("🔌 Reconnect attempt %s/%s", attempt, attempts)
            try:
                self.connector.disconnect()
            except Exception:
                pass

            if self.connector.connect():
                resolved_symbol = self.connector.resolve_symbol(requested_symbol)
                if resolved_symbol:
                    logger.info(
                        "✅ cTrader reconnection successful | attempt=%s/%s requested_symbol=%s resolved_symbol=%s",
                        attempt,
                        attempts,
                        requested_symbol,
                        resolved_symbol,
                    )
                    return resolved_symbol
                logger.error(
                    "❌ Reconnected to cTrader but symbol '%s' is unavailable after reconnect.",
                    requested_symbol,
                )

            if attempt < attempts and wait_seconds > 0:
                logger.info("⏳ Waiting %.1f seconds before next reconnect attempt", wait_seconds)
                time.sleep(wait_seconds)

        logger.error("❌ Failed to restore cTrader connection after %s attempt(s).", attempts)
        return None

    def _bars_to_pull(self, timeframe: str) -> int:
        base_count = max(1, int(self.settings.candle_count))
        timeframe_text = (timeframe or "").strip().upper()
        strategy_names = [name.strip().lower() for name in self.settings.strategy_names if str(name).strip()]
        if not strategy_names:
            strategy_names = ["trend_following"]

        active_lower: set[str] = set()
        active_higher: set[str] = set()
        for strategy_name in strategy_names:
            for config in self._strategy_runtime_configs(strategy_name):
                active_lower.add(str(config["lower_timeframe"]).upper())
                active_higher.add(str(config["higher_timeframe"]).upper())

        if timeframe_text in active_lower:
            return max(base_count, int(self.settings.plot_ltf_candles))
        if timeframe_text in active_higher:
            return max(base_count, int(self.settings.plot_htf_candles))
        return base_count

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

    def _active_strategy_timeframes(self, strategy_names: list[str]) -> list[str]:
        values: set[str] = set()
        for strategy_name in strategy_names:
            for preset in self._strategy_runtime_configs(strategy_name):
                values.add(str(preset["lower_timeframe"]).upper())
                values.add(str(preset["higher_timeframe"]).upper())
        return sorted(values)

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

    def _handle_candidate(
        self,
        symbol: str,
        candidate: Any,
        lower_frame: pd.DataFrame,
        higher_frame: pd.DataFrame,
        lower_timeframe: str,
        higher_timeframe: str,
        stop_loss_pips: float,
        take_profit_pips: float,
        pair_index: int = 0,
    ) -> None:
        ts = lower_frame.iloc[-1]["datetime"]
        strategy_name = str(getattr(candidate, "strategy", "")).strip().lower()
        pair_tag = f"{lower_timeframe}/{higher_timeframe}#{pair_index + 1}"
        signal_key = f"{symbol}:{candidate.strategy}:{pair_tag}:{candidate.direction}:{ts.isoformat()}"
        decision_context = self._decision_context_for_candidate(
            strategy_name=strategy_name,
            lower_frame=lower_frame,
            higher_frame=higher_frame,
            lower_timeframe=lower_timeframe,
            higher_timeframe=higher_timeframe,
        )
        logger.info(
            "📣 Signal detected | key=%s symbol=%s strategy=%s direction=%s reason=%s candidate_price=%.5f ltf=%s ltf_ts=%s htf=%s htf_ts=%s decision_data=%s",
            signal_key,
            symbol,
            candidate.strategy,
            candidate.direction,
            candidate.reason,
            float(candidate.price),
            lower_timeframe,
            ts,
            higher_timeframe,
            higher_frame.iloc[-1]["datetime"] if not higher_frame.empty and "datetime" in higher_frame.columns else "n/a",
            decision_context,
        )
        if signal_key in self._last_signal_keys:
            self._log_strategy_eval(
                "🧪 Strategy decision result | status=failed symbol=%s strategy=%s pair=%s ltf=%s htf=%s reason=duplicate_signal",
                symbol,
                candidate.strategy,
                pair_tag,
                lower_timeframe,
                higher_timeframe,
            )
            logger.debug("🔁 Duplicate signal skipped | key=%s", signal_key)
            return

        self._last_signal_keys.add(signal_key)
        self._append_marker(lower_timeframe, ts, float(candidate.price), candidate.direction, "signal")

        if not self.settings.enable_trading:
            logger.info(
                "🧪 Signal confirmed (dry-run only) | key=%s symbol=%s strategy=%s direction=%s reason=%s candidate_price=%.5f",
                signal_key,
                symbol,
                candidate.strategy,
                candidate.direction,
                candidate.reason,
                float(candidate.price),
            )
            self._log_strategy_eval(
                "🧪 Strategy decision result | status=successful symbol=%s strategy=%s pair=%s ltf=%s htf=%s reason=dry_run_mode",
                symbol,
                candidate.strategy,
                pair_tag,
                lower_timeframe,
                higher_timeframe,
            )
            return

        today_key = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        open_positions = self._open_positions_with_recovery(symbol)
        if open_positions is None:
            self._log_strategy_eval(
                "🧪 Strategy decision result | status=failed symbol=%s strategy=%s pair=%s ltf=%s htf=%s reason=position_lookup_failed",
                symbol,
                candidate.strategy,
                pair_tag,
                lower_timeframe,
                higher_timeframe,
            )
            logger.info(
                "⛔ Signal skipped after position lookup failure | key=%s strategy=%s direction=%s",
                signal_key,
                candidate.strategy,
                candidate.direction,
            )
            return
        strategy_open_positions = self._count_open_positions_by_strategy(open_positions)
        ladder_target = max(1, int(self.settings.ladder_entries if self.settings.enable_multi_entry else 1))
        current_strategy_open = int(strategy_open_positions.get(strategy_name, 0))
        available_strategy_slots = max(0, ladder_target - current_strategy_open)
        if available_strategy_slots <= 0:
            self._log_strategy_eval(
                "🧪 Strategy decision result | status=failed symbol=%s strategy=%s pair=%s ltf=%s htf=%s reason=strategy_ladder_cap_reached",
                symbol,
                candidate.strategy,
                pair_tag,
                lower_timeframe,
                higher_timeframe,
            )
            logger.info(
                "⛔ Signal not confirmed (strategy ladder cap) | key=%s strategy=%s symbol=%s open=%s cap=%s direction=%s",
                signal_key,
                candidate.strategy,
                symbol,
                current_strategy_open,
                ladder_target,
                candidate.direction,
            )
            return

        execution_now = datetime.now(timezone.utc)
        if not self._is_strategy_execution_allowed(strategy_name, execution_now):
            cooldown_minutes = self._cooldown_minutes_for_strategy(strategy_name)
            self._log_strategy_eval(
                "🧪 Strategy decision result | status=failed symbol=%s strategy=%s pair=%s ltf=%s htf=%s reason=cooldown_active cooldown_minutes=%.1f",
                symbol,
                candidate.strategy,
                pair_tag,
                lower_timeframe,
                higher_timeframe,
                cooldown_minutes,
            )
            logger.info(
                "⏳ Signal skipped (cooldown active) | key=%s strategy=%s cooldown_minutes=%.1f",
                signal_key,
                candidate.strategy,
                cooldown_minutes,
            )
            return

        ladder_entries = self.runner.trade_manager.build_ladder(
            candidate,
            stop_loss_pips=stop_loss_pips,
            take_profit_pips=take_profit_pips,
        )
        daily_trade_slots = max(0, int(self.settings.max_daily_trades) - int(self._daily_trade_count[today_key]))
        executable_slots = min(len(ladder_entries), available_strategy_slots, daily_trade_slots)
        if executable_slots <= 0:
            self._log_strategy_eval(
                "🧪 Strategy decision result | status=failed symbol=%s strategy=%s pair=%s ltf=%s htf=%s reason=no_execution_slots",
                symbol,
                candidate.strategy,
                pair_tag,
                lower_timeframe,
                higher_timeframe,
            )
            logger.info(
                "⛔ Signal not confirmed (no available execution slots) | key=%s strategy=%s direction=%s strategy_slots=%s daily_trade_slots=%s",
                signal_key,
                candidate.strategy,
                candidate.direction,
                available_strategy_slots,
                daily_trade_slots,
            )
            return

        if executable_slots < len(ladder_entries):
            logger.info(
                "⚠️ Ladder reduced by active limits | key=%s strategy=%s requested=%s executable=%s strategy_slots=%s daily_trade_slots=%s",
                signal_key,
                candidate.strategy,
                len(ladder_entries),
                executable_slots,
                available_strategy_slots,
                daily_trade_slots,
            )

        account_info = self.connector.account_info()
        symbol_info = self.connector.symbol_info(symbol)
        entry_price = self.connector.current_price(symbol, candidate.direction)
        if entry_price is None:
            self._log_strategy_eval(
                "🧪 Strategy decision result | status=failed symbol=%s strategy=%s pair=%s ltf=%s htf=%s reason=missing_live_quote",
                symbol,
                candidate.strategy,
                pair_tag,
                lower_timeframe,
                higher_timeframe,
            )
            logger.warning(
                "⚠️ Signal not confirmed (no live quote) | key=%s symbol=%s strategy=%s direction=%s",
                signal_key,
                symbol,
                candidate.strategy,
                candidate.direction,
            )
            return

        logger.info(
            "✅ Signal confirmed for execution | key=%s symbol=%s strategy=%s direction=%s reason=%s market_price=%.5f",
            signal_key,
            symbol,
            candidate.strategy,
            candidate.direction,
            candidate.reason,
            float(entry_price),
        )

        for ladder_trade in ladder_entries[:executable_slots]:
            order = self.runner.trade_manager.build_order_request(
                candidate=candidate,
                symbol=symbol,
                entry_price=entry_price,
                account_info=account_info,
                symbol_info=symbol_info,
                stop_loss_pips=float(ladder_trade.get("stop_loss_pips", stop_loss_pips)),
                take_profit_pips=float(ladder_trade.get("take_profit_pips", take_profit_pips)),
                level=int(ladder_trade.get("level", 1)),
            )
            exit_targets = self.runner.trade_manager.update_exit_targets(
                entry_price=float(order["entry_price"]),
                current_price=float(entry_price),
                direction=order["direction"],
                stop_loss_pips=float(ladder_trade.get("stop_loss_pips", stop_loss_pips)),
                take_profit_pips=float(ladder_trade.get("take_profit_pips", take_profit_pips)),
                move_sl_pips=max(1.0, float(ladder_trade.get("stop_loss_pips", stop_loss_pips)) / 2.0),
                move_tp_pips=max(1.0, float(ladder_trade.get("take_profit_pips", take_profit_pips)) / 2.0),
            )
            order["stop_loss"] = exit_targets["stop_loss"]
            order["take_profit"] = exit_targets["take_profit"]
            comment_pair = f"{lower_timeframe}-{higher_timeframe}-P{pair_index + 1}"
            order_comment = f"{self.settings.trade_comment_prefix}:{candidate.strategy}:{comment_pair}:L{order['level']}"
            logger.info(
                "🧾 Trade execution request | key=%s symbol=%s strategy=%s level=%s direction=%s volume=%.2f market_price=%.5f request_entry=%.5f sl=%.5f tp=%.5f magic=%s comment=%s",
                signal_key,
                symbol,
                order["strategy"],
                order["level"],
                order["direction"],
                float(order["volume"]),
                float(entry_price),
                float(order["entry_price"]),
                float(order["stop_loss"]),
                float(order["take_profit"]),
                self.settings.trade_magic_number,
                order_comment,
            )

            order_result = self.connector.place_market_order(
                symbol=symbol,
                direction=order["direction"],
                volume=order["volume"],
                stop_loss=order["stop_loss"],
                take_profit=order["take_profit"],
                magic_number=self.settings.trade_magic_number,
                comment=order_comment,
            )
            self._orders_attempted += 1

            if not order_result.get("ok"):
                self._orders_rejected += 1
                self._log_strategy_eval(
                    "🧪 Strategy decision result | status=failed symbol=%s strategy=%s pair=%s ltf=%s htf=%s reason=order_rejected level=%s",
                    symbol,
                    candidate.strategy,
                    pair_tag,
                    lower_timeframe,
                    higher_timeframe,
                    order["level"],
                )
                logger.error(
                    "❌ Trade execution rejected | key=%s symbol=%s strategy=%s level=%s direction=%s reason=%s retcode=%s filling=%s details=%s",
                    signal_key,
                    symbol,
                    candidate.strategy,
                    order["level"],
                    candidate.direction,
                    order_result.get("reason"),
                    order_result.get("retcode"),
                    order_result.get("filling"),
                    order_result.get("details"),
                )
                continue

            now_iso = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
            position_key = f"ctrader:ticket-{order_result.get('order', 0)}:{symbol}"
            self.position_store.upsert_position(
                {
                    "position_key": position_key,
                    "ticket": int(order_result.get("order", 0)),
                    "symbol": symbol,
                    "direction": order["direction"],
                    "volume": order["volume"],
                    "entry_price": float(order_result.get("price", order["entry_price"])),
                    "stop_loss": order["stop_loss"],
                    "take_profit": order["take_profit"],
                    "strategy": order["strategy"],
                    "timeframes": f"{lower_timeframe}/{higher_timeframe}",
                    "comment": order_comment,
                    "source": "ctrader",
                    "is_external": 0,
                    "status": "open",
                    "opened_at": now_iso,
                }
            )
            self._append_marker(lower_timeframe, ts, float(order_result.get("price", order["entry_price"])), order["direction"], "entry")
            self._append_marker(
                higher_timeframe,
                higher_frame.iloc[-1]["datetime"],
                float(order_result.get("price", order["entry_price"])),
                order["direction"],
                "entry",
            )
            self._daily_trade_count[today_key] += 1
            self._orders_filled += 1
            self._mark_strategy_executed(strategy_name, execution_now)
            self._log_strategy_eval(
                "🧪 Strategy decision result | status=successful symbol=%s strategy=%s pair=%s ltf=%s htf=%s reason=order_filled level=%s",
                symbol,
                candidate.strategy,
                pair_tag,
                lower_timeframe,
                higher_timeframe,
                order["level"],
            )
            logger.info(
                "✅ Trade executed | key=%s symbol=%s strategy=%s level=%s ticket=%s direction=%s volume=%.2f entry=%.5f sl=%.5f tp=%.5f filling=%s",
                signal_key,
                symbol,
                order["strategy"],
                order["level"],
                order_result.get("order"),
                order["direction"],
                order["volume"],
                float(order_result.get("price", order["entry_price"])),
                float(order["stop_loss"]),
                float(order["take_profit"]),
                order_result.get("filling"),
            )

    def _cooldown_minutes_for_strategy(self, strategy_name: str) -> float:
        key = str(strategy_name).strip().lower()
        if key == "price_action":
            return max(0.0, float(getattr(self.settings, "price_action_cooldown_minutes", 0.0)))
        return 0.0

    def _is_strategy_execution_allowed(self, strategy_name: str, now: datetime | None = None) -> bool:
        cooldown_minutes = self._cooldown_minutes_for_strategy(strategy_name)
        if cooldown_minutes <= 0:
            return True
        key = str(strategy_name).strip().lower()
        last_execution = self._strategy_cooldowns.get(key)
        if last_execution is None:
            return True
        current_time = now or datetime.now(timezone.utc)
        return current_time - last_execution >= timedelta(minutes=cooldown_minutes)

    def _mark_strategy_executed(self, strategy_name: str, now: datetime | None = None) -> None:
        cooldown_minutes = self._cooldown_minutes_for_strategy(strategy_name)
        if cooldown_minutes <= 0:
            return
        key = str(strategy_name).strip().lower()
        self._strategy_cooldowns[key] = now or datetime.now(timezone.utc)

    def _count_open_positions_by_strategy(self, open_positions: list[dict[str, Any]]) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        prefix = f"{self.settings.trade_comment_prefix}:"
        for item in open_positions:
            comment = str(item.get("comment", "") or "")
            if not comment.startswith(prefix):
                continue
            tail = comment[len(prefix) :]
            if not tail:
                continue
            strategy_name = tail.split(":", 1)[0].strip().lower()
            if not strategy_name:
                continue
            counts[strategy_name] += 1
        return counts

    def _log_exit_summary(self, symbol: str, run_status: str) -> None:
        ended_at = datetime.utcnow()
        started_at = self._session_started_at or ended_at
        account = self.connector.account_info() or {}
        end_balance = float(account.get("balance", self._session_start_balance))
        end_equity = float(account.get("equity", end_balance))
        performance = self.connector.session_trade_performance(
            started_at=started_at,
            ended_at=ended_at,
            symbol=symbol,
            magic_number=self.settings.trade_magic_number,
            comment_prefix=self.settings.trade_comment_prefix,
        )
        wins = int(performance.get("wins", 0))
        losses = int(performance.get("losses", 0))
        closed = int(performance.get("closed_trades", 0))
        win_rate = (wins / closed * 100.0) if closed > 0 else 0.0
        logger.info("📊 Live run status | status=%s symbol=%s", run_status, symbol)
        logger.info(
            "📊 Live session summary | symbol=%s started=%s ended=%s orders_attempted=%s orders_filled=%s orders_rejected=%s closed_trades=%s wins=%s losses=%s breakeven=%s win_rate=%.2f%% balance_start=%.2f balance_end=%.2f balance_change=%+.2f equity_start=%.2f equity_end=%.2f equity_change=%+.2f net_trade_profit=%+.2f",
            symbol,
            started_at.strftime("%Y-%m-%dT%H:%M:%S"),
            ended_at.strftime("%Y-%m-%dT%H:%M:%S"),
            self._orders_attempted,
            self._orders_filled,
            self._orders_rejected,
            closed,
            wins,
            losses,
            int(performance.get("breakeven", 0)),
            win_rate,
            self._session_start_balance,
            end_balance,
            end_balance - self._session_start_balance,
            self._session_start_equity,
            end_equity,
            end_equity - self._session_start_equity,
            float(performance.get("net_profit", 0.0)),
        )
        summary_rows = [
            {"metric": "status", "value": run_status},
            {"metric": "orders_attempted", "value": self._orders_attempted},
            {"metric": "orders_filled", "value": self._orders_filled},
            {"metric": "orders_rejected", "value": self._orders_rejected},
            {"metric": "positions_passed_failed", "value": f"{wins}/{losses}"},
            {"metric": "closed_trades", "value": closed},
            {"metric": "win_rate_pct", "value": f"{win_rate:.2f}"},
            {"metric": "balance_start", "value": f"{self._session_start_balance:.2f}"},
            {"metric": "balance_end", "value": f"{end_balance:.2f}"},
            {"metric": "balance_change", "value": f"{(end_balance - self._session_start_balance):+.2f}"},
            {"metric": "equity_start", "value": f"{self._session_start_equity:.2f}"},
            {"metric": "equity_end", "value": f"{end_equity:.2f}"},
            {"metric": "equity_change", "value": f"{(end_equity - self._session_start_equity):+.2f}"},
            {"metric": "net_trade_profit", "value": f"{float(performance.get('net_profit', 0.0)):+.2f}"},
        ]
        logger.info("📋 Live summary table:\n%s", self._format_table(summary_rows, ["metric", "value"]))

    def _append_marker(self, timeframe: str, timestamp: Any, price: float, direction: str, marker_type: str) -> None:
        markers = self._signal_markers_by_timeframe[timeframe]
        markers.append(
            {
                "datetime": pd.to_datetime(timestamp),
                "price": price,
                "direction": direction,
                "type": marker_type,
            }
        )
        # Keep memory bounded for long sessions.
        if len(markers) > 300:
            self._signal_markers_by_timeframe[timeframe] = markers[-300:]

    def _monitor_positions(self, symbol: str) -> None:
        positions = self._open_positions_with_recovery(symbol)
        if positions is None:
            raise RuntimeError(f"Unable to retrieve positions for {symbol}")
        self._last_positions = positions
        logger.info("📌 Position monitor | symbol=%s open_positions=%s", symbol, len(positions))
        now_iso = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
        existing_rows = [
            row for row in self.position_store.list_positions(status="open") if row.get("position_key")
        ]
        existing_positions_by_key = {
            str(row.get("position_key", "")): row
            for row in existing_rows
        }
        existing_positions_by_ticket = {
            (str(row.get("ticket") or ""), str(row.get("symbol") or "")): row
            for row in existing_rows
        }
        existing_positions_by_comment = {
            str(row.get("comment") or ""): row
            for row in existing_rows
            if str(row.get("comment") or "")
        }
        for item in positions:
            position_key = f"ctrader:ticket-{item['ticket']}:{item['symbol']}"
            direction = "buy" if int(item.get("type", 0)) == 0 else "sell"
            comment = str(item.get("comment") or "")
            existing = existing_positions_by_key.get(position_key, {})
            if not existing:
                existing = existing_positions_by_ticket.get((str(item.get("ticket") or ""), str(item.get("symbol") or "")), {})
            if not existing and comment:
                existing = existing_positions_by_comment.get(comment, {})
            if existing and str(existing.get("position_key") or "") != position_key:
                self.position_store.delete_position(str(existing.get("position_key") or ""))
            strategy = str(existing.get("strategy") or "external")
            timeframes = str(existing.get("timeframes") or "")
            opened_at = str(existing.get("opened_at") or now_iso)
            self.position_store.upsert_position(
                {
                    "position_key": position_key,
                    "ticket": item["ticket"],
                    "symbol": item["symbol"],
                    "direction": direction,
                    "volume": item["volume"],
                    "entry_price": item["price_open"],
                    "stop_loss": item["sl"],
                    "take_profit": item["tp"],
                    "strategy": strategy,
                    "timeframes": timeframes,
                    "comment": comment or str(existing.get("comment") or ""),
                    "source": "ctrader",
                    "is_external": 1,
                    "status": "open",
                    "opened_at": opened_at,
                }
            )
        position_rows = []
        for item in positions:
            ticket = str(item.get("ticket") or "")
            symbol = str(item.get("symbol") or "")
            position_key = f"ctrader:ticket-{ticket}:{symbol}"
            existing = existing_positions_by_key.get(position_key, {})
            if not existing:
                existing = existing_positions_by_ticket.get((ticket, symbol), {})
            if not existing:
                comment = str(item.get("comment") or "")
                existing = existing_positions_by_comment.get(comment, {}) if comment else {}
            strategy = str(existing.get("strategy") or "external")
            timeframes = str(existing.get("timeframes") or "")
            position_rows.append(
                {
                    "ticket": int(item.get("ticket", 0)),
                    "symbol": symbol,
                    "strategy": strategy,
                    "timeframes": timeframes,
                    "direction": "buy" if int(item.get("type", 0)) == 0 else "sell",
                    "volume": float(item.get("volume", 0.0)),
                    "entry": float(item.get("price_open", 0.0)),
                    "sl": float(item.get("sl", 0.0)),
                    "tp": float(item.get("tp", 0.0)),
                    "profit": float(item.get("profit", 0.0)),
                }
            )
        if not position_rows:
            position_rows = [{"ticket": "-", "symbol": "-", "strategy": "-", "timeframes": "-", "direction": "-", "volume": 0.0, "entry": 0.0, "sl": 0.0, "tp": 0.0, "profit": 0.0}]
        logger.info("📋 Open positions table:\n%s", self._format_table(position_rows, ["ticket", "symbol", "strategy", "timeframes", "direction", "volume", "entry", "sl", "tp", "profit"]))

    def _open_positions_with_recovery(self, symbol: str) -> list[dict[str, Any]] | None:
        try:
            return self.connector.open_positions(symbol=symbol)
        except Exception as exc:
            logger.exception("⚠️ Open positions lookup failed | symbol=%s", symbol)
            if not self._recover_from_monitor_failure(symbol, exc):
                return None
            try:
                return self.connector.open_positions(symbol=symbol)
            except Exception:
                logger.exception("⚠️ Open positions retry failed | symbol=%s", symbol)
                return None

    def _recover_from_monitor_failure(self, symbol: str, exc: Exception) -> bool:
        if not self._is_connection_error(exc):
            logger.warning("⚠️ Non-connection monitor failure detected; attempting reconnect anyway | symbol=%s", symbol)
        recovered_symbol = self._recover_connection(symbol)
        return recovered_symbol is not None

    def _monitor_account(self) -> None:
        account_info = self.connector.account_info()
        if not account_info:
            logger.warning("⚠️ Account monitor | account info unavailable")
            return
        previous = self._last_account_snapshot
        delta_balance = 0.0
        delta_equity = 0.0
        if previous:
            delta_balance = float(account_info.get("balance", 0.0)) - float(previous.get("balance", 0.0))
            delta_equity = float(account_info.get("equity", 0.0)) - float(previous.get("equity", 0.0))
        self._last_account_snapshot = account_info
        self._last_account_change = {"delta_balance": delta_balance, "delta_equity": delta_equity}
        self._equity_curve.append(
            {
                "datetime": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S"),
                "equity": float(account_info.get("equity", 0.0)),
                "balance": float(account_info.get("balance", 0.0)),
            }
        )
        if len(self._equity_curve) > 2000:
            self._equity_curve = self._equity_curve[-2000:]

        logger.info(
            "💰 Account monitor | login=%s balance=%.2f equity=%.2f margin=%.2f free_margin=%.2f",
            account_info.get("login"),
            float(account_info.get("balance", 0.0)),
            float(account_info.get("equity", 0.0)),
            float(account_info.get("margin", 0.0)),
            float(account_info.get("free_margin", 0.0)),
        )
        account_row = {
            "login": account_info.get("login", "-"),
            "balance": round(float(account_info.get("balance", 0.0)), 2),
            "equity": round(float(account_info.get("equity", 0.0)), 2),
            "d_balance": round(delta_balance, 2),
            "d_equity": round(delta_equity, 2),
            "margin": round(float(account_info.get("margin", 0.0)), 2),
            "free_margin": round(float(account_info.get("free_margin", 0.0)), 2),
            "open_positions": len(self._last_positions),
        }
        logger.info(
            "🧾 Account table:\n%s",
            self._format_table([account_row], ["login", "balance", "equity", "d_balance", "d_equity", "margin", "free_margin", "open_positions"]),
        )

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

    def _higher_timeframe_bias_context(self, higher_frame: pd.DataFrame) -> dict[str, Any]:
        context = {
            "bias": "neutral",
            "close": 0.0,
            "ema_fast": 0.0,
            "ema_slow": 0.0,
            "ema_trend": 0.0,
        }
        if higher_frame.empty or "close" not in higher_frame.columns:
            return context

        higher_close = higher_frame["close"].astype(float)
        if higher_close.empty:
            return context

        ema_fast = higher_close.ewm(span=max(1, int(self.settings.ema_fast)), adjust=False).mean()
        ema_slow = higher_close.ewm(span=max(1, int(self.settings.ema_slow)), adjust=False).mean()
        ema_trend = higher_close.ewm(span=max(1, int(self.settings.ema_trend_period)), adjust=False).mean()
        last_close = float(higher_close.iloc[-1])
        last_fast = float(ema_fast.iloc[-1])
        last_slow = float(ema_slow.iloc[-1])
        last_trend = float(ema_trend.iloc[-1])

        bias = "neutral"
        if last_close >= last_trend and last_fast >= last_slow:
            bias = "buy"
        elif last_close <= last_trend and last_fast <= last_slow:
            bias = "sell"

        context.update(
            {
                "bias": bias,
                "close": last_close,
                "ema_fast": last_fast,
                "ema_slow": last_slow,
                "ema_trend": last_trend,
            }
        )
        return context

    def _decision_context_for_candidate(
        self,
        strategy_name: str,
        lower_frame: pd.DataFrame,
        higher_frame: pd.DataFrame,
        lower_timeframe: str,
        higher_timeframe: str,
    ) -> dict[str, Any]:
        context: dict[str, Any] = {
            "ltf": lower_timeframe,
            "htf": higher_timeframe,
            "htf_confirmation": self._higher_timeframe_bias_context(higher_frame),
        }
        if lower_frame.empty or "close" not in lower_frame.columns:
            return context

        close = lower_frame["close"].astype(float)
        context["ltf_close"] = float(close.iloc[-1])

        if strategy_name == "trend_following":
            ema_fast = close.ewm(span=max(1, int(self.settings.ema_fast)), adjust=False).mean()
            ema_slow = close.ewm(span=max(1, int(self.settings.ema_slow)), adjust=False).mean()
            context["ema_fast"] = float(ema_fast.iloc[-1])
            context["ema_slow"] = float(ema_slow.iloc[-1])
            if len(ema_fast) > 1:
                context["ema_fast_prev"] = float(ema_fast.iloc[-2])
                context["ema_slow_prev"] = float(ema_slow.iloc[-2])
        if strategy_name == "trend_following":
            ema_trend = close.ewm(span=max(1, int(self.settings.ema_trend_period)), adjust=False).mean()
            context["ema_trend"] = float(ema_trend.iloc[-1])
            if len(ema_trend) > 1:
                context["ema_trend_prev"] = float(ema_trend.iloc[-2])
        if strategy_name == "price_action" and len(lower_frame) >= 2:
            prior = lower_frame.iloc[:-1]
            context["window_bars"] = 5
            context["recent_high_5"] = float(prior["high"].tail(5).max())
            context["recent_low_5"] = float(prior["low"].tail(5).min())
        if strategy_name == "session_breakout" and len(lower_frame) >= 8:
            prior = lower_frame.iloc[:-1]
            context["window_bars"] = 8
            context["session_high_8"] = float(prior["high"].tail(8).max())
            context["session_low_8"] = float(prior["low"].tail(8).min())
            context["session_close"] = float(close.iloc[-1])
        return context
