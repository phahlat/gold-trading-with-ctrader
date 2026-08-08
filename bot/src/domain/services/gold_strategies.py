from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

import pandas as pd


class GoldStrategyName(str, Enum):
    TREND_FOLLOWING = "trend_following"
    PRICE_ACTION = "price_action"
    SESSION_BREAKOUT = "session_breakout"
    EMA_CROSSOVER = "ema_crossover"


@dataclass(frozen=True)
class SignalCandidate:
    strategy: str
    direction: str
    reason: str
    price: float


@dataclass
class _EmaCrossoverState:
    phase: int = 0
    direction: str = ""
    candle1_close: float = 0.0
    candle2_close: float = 0.0
    candle1_time: str = ""
    last_processed_time: str = ""
    last_relation: int = 0


class GoldStrategyEngine:
    def __init__(self, strategy_names: list[str]) -> None:
        self.strategy_names = [name.lower() for name in strategy_names]
        self._ema_state_by_key: dict[str, _EmaCrossoverState] = {}

    def evaluate(
        self,
        frame: pd.DataFrame,
        settings: object,
        higher_frame: pd.DataFrame | None = None,
        strategy_names: list[str] | None = None,
        context_key: str | None = None,
    ) -> list[SignalCandidate]:
        candidates: list[SignalCandidate] = []
        if frame.empty:
            return candidates

        active_strategies = {name.strip().lower() for name in (strategy_names or self.strategy_names) if str(name).strip()}

        close = frame["close"].astype(float)
        if "trend_following" in active_strategies:
            candidates.extend(self._trend_following(close, frame, settings))
        if "price_action" in active_strategies:
            candidates.extend(self._price_action(close, frame))
        if "session_breakout" in active_strategies:
            candidates.extend(self._session_breakout(close, frame))
        if "ema_crossover" in active_strategies:
            candidates.extend(self._ema_crossover(close, frame, settings, context_key=context_key))

        return self._apply_higher_timeframe_bias(candidates, higher_frame, settings)

    def _apply_higher_timeframe_bias(
        self,
        candidates: list[SignalCandidate],
        higher_frame: pd.DataFrame | None,
        settings: object,
    ) -> list[SignalCandidate]:
        if not candidates or higher_frame is None or higher_frame.empty:
            return candidates

        non_ema = [candidate for candidate in candidates if candidate.strategy != "ema_crossover"]
        ema_candidates = [candidate for candidate in candidates if candidate.strategy == "ema_crossover"]
        if not non_ema:
            return ema_candidates

        if "close" not in higher_frame.columns:
            return candidates

        higher_close = higher_frame["close"].astype(float)
        if len(higher_close) < 3:
            return candidates

        fast_period = int(getattr(settings, "ema_fast", 9))
        slow_period = int(getattr(settings, "ema_slow", 21))
        trend_period = int(getattr(settings, "ema_trend_period", 200))
        ema_fast = higher_close.ewm(span=fast_period, adjust=False).mean()
        ema_slow = higher_close.ewm(span=slow_period, adjust=False).mean()
        ema_trend = higher_close.ewm(span=trend_period, adjust=False).mean()

        last_close = float(higher_close.iloc[-1])
        last_fast = float(ema_fast.iloc[-1])
        last_slow = float(ema_slow.iloc[-1])
        last_trend = float(ema_trend.iloc[-1])

        bias = "neutral"
        if last_close >= last_trend and last_fast >= last_slow:
            bias = "buy"
        elif last_close <= last_trend and last_fast <= last_slow:
            bias = "sell"

        if bias == "neutral":
            return candidates
        filtered = [candidate for candidate in non_ema if candidate.direction == bias]
        return filtered + ema_candidates

    def _trend_following(self, close: pd.Series, frame: pd.DataFrame, settings: object) -> list[SignalCandidate]:
        fast_period = int(getattr(settings, "ema_fast", 9))
        slow_period = int(getattr(settings, "ema_slow", 21))
        trend_period = int(getattr(settings, "ema_trend_period", 200))
        ema_fast = close.ewm(span=fast_period, adjust=False).mean()
        ema_slow = close.ewm(span=slow_period, adjust=False).mean()
        ema_trend = close.ewm(span=trend_period, adjust=False).mean()
        if len(close) < 3:
            return []
        last_close = float(close.iloc[-1])
        last_fast = float(ema_fast.iloc[-1])
        last_slow = float(ema_slow.iloc[-1])
        last_trend = float(ema_trend.iloc[-1])
        prev_fast = float(ema_fast.iloc[-2])
        prev_slow = float(ema_slow.iloc[-2])
        prev_trend = float(ema_trend.iloc[-2])
        if last_close > last_trend and last_fast > last_slow and prev_fast <= prev_slow:
            return [SignalCandidate(strategy="trend_following", direction="buy", reason="EMA pullback breakout", price=last_close)]
        if last_close < last_trend and last_fast < last_slow and prev_fast >= prev_slow:
            return [SignalCandidate(strategy="trend_following", direction="sell", reason="EMA pullback breakout", price=last_close)]
        return []

    def _price_action(self, close: pd.Series, frame: pd.DataFrame) -> list[SignalCandidate]:
        if len(close) < 2:
            return []
        current = float(close.iloc[-1])
        prior_window = frame.iloc[:-1]
        if prior_window.empty:
            return []
        recent_high = float(prior_window["high"].tail(5).max())
        recent_low = float(prior_window["low"].tail(5).min())
        if current > recent_high:
            return [SignalCandidate(strategy="price_action", direction="buy", reason="rejection at resistance", price=current)]
        if current < recent_low:
            return [SignalCandidate(strategy="price_action", direction="sell", reason="rejection at support", price=current)]
        return []

    def _session_breakout(self, close: pd.Series, frame: pd.DataFrame) -> list[SignalCandidate]:
        if len(close) < 8:
            return []
        prior_window = frame.iloc[:-1]
        if prior_window.empty:
            return []
        session_high = float(prior_window["high"].tail(8).max())
        session_low = float(prior_window["low"].tail(8).min())
        recent_close = float(close.iloc[-1])
        if recent_close > session_high:
            return [SignalCandidate(strategy="session_breakout", direction="buy", reason="breakout above session range", price=recent_close)]
        if recent_close < session_low:
            return [SignalCandidate(strategy="session_breakout", direction="sell", reason="breakout below session range", price=recent_close)]
        return []

    def _ema_crossover(
        self,
        close: pd.Series,
        frame: pd.DataFrame,
        settings: object,
        context_key: str | None = None,
    ) -> list[SignalCandidate]:
        ema_slow_period = int(getattr(settings, "ema_slow", 200))
        if len(close) < max(ema_slow_period + 2, 3):
            return []

        ema_slow = close.ewm(span=ema_slow_period, adjust=False).mean()

        ts_value: Any = frame.iloc[-1]["datetime"] if "datetime" in frame.columns else len(frame) - 1
        candle_time = pd.Timestamp(ts_value).isoformat()
        state_key = str(context_key or "ema_crossover:default")
        state = self._ema_state_by_key.setdefault(state_key, _EmaCrossoverState())
        if state.last_processed_time == candle_time:
            return []

        state.last_processed_time = candle_time
        last_close = float(close.iloc[-1])
        last_slow = float(ema_slow.iloc[-1])
        relation = 1 if last_close > last_slow else -1 if last_close < last_slow else 0
        bullish_env = relation > 0
        bearish_env = relation < 0
        bullish_cross = state.last_relation <= 0 and relation > 0
        bearish_cross = state.last_relation >= 0 and relation < 0
        state.last_relation = relation

        def _start_new(phase_direction: str) -> None:
            state.phase = 1
            state.direction = phase_direction
            state.candle1_close = last_close
            state.candle2_close = 0.0
            state.candle1_time = candle_time

        if state.phase == 0:
            if bullish_cross:
                _start_new("buy")
            elif bearish_cross:
                _start_new("sell")
            return []

        if state.phase == 1 and state.direction == "buy":
            if not bullish_env:
                state.phase = 0
                state.direction = ""
                if bearish_cross:
                    _start_new("sell")
                return []
            if last_close >= state.candle1_close:
                state.phase = 0
                state.direction = ""
                return [SignalCandidate(strategy="ema_crossover", direction="buy", reason="EMA C1/C2 bullish confirmation", price=last_close)]
            state.phase = 2
            state.candle2_close = last_close
            return []

        if state.phase == 1 and state.direction == "sell":
            if not bearish_env:
                state.phase = 0
                state.direction = ""
                if bullish_cross:
                    _start_new("buy")
                return []
            if last_close <= state.candle1_close:
                state.phase = 0
                state.direction = ""
                return [SignalCandidate(strategy="ema_crossover", direction="sell", reason="EMA C1/C2 bearish confirmation", price=last_close)]
            state.phase = 2
            state.candle2_close = last_close
            return []

        if state.phase == 2 and state.direction == "buy":
            if not bullish_env:
                state.phase = 0
                state.direction = ""
                if bearish_cross:
                    _start_new("sell")
                return []
            if last_close > state.candle1_close and last_close > state.candle2_close:
                state.phase = 0
                state.direction = ""
                return [SignalCandidate(strategy="ema_crossover", direction="buy", reason="EMA C1/C2/C3 bullish confirmation", price=last_close)]
            if last_close == state.candle1_close:
                state.phase = 0
                state.direction = ""
                return [SignalCandidate(strategy="ema_crossover", direction="buy", reason="EMA C3 equals C1 bullish confirmation", price=last_close)]
            if last_close < state.candle1_close:
                state.phase = 0
                state.direction = ""
                if bullish_cross:
                    _start_new("buy")
                elif bearish_cross:
                    _start_new("sell")
            return []

        if state.phase == 2 and state.direction == "sell":
            if not bearish_env:
                state.phase = 0
                state.direction = ""
                if bullish_cross:
                    _start_new("buy")
                return []
            if (last_close < state.candle1_close and last_close < state.candle2_close) or last_close == state.candle1_close:
                state.phase = 0
                state.direction = ""
                return [SignalCandidate(strategy="ema_crossover", direction="sell", reason="EMA C1/C2/C3 bearish confirmation", price=last_close)]
            state.phase = 0
            state.direction = ""
            if bearish_cross:
                _start_new("sell")
            elif bullish_cross:
                _start_new("buy")
            return []

        state.phase = 0
        state.direction = ""
        return []
