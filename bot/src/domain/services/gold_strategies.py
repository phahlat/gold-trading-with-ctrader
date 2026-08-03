from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import pandas as pd


class GoldStrategyName(str, Enum):
    TREND_FOLLOWING = "trend_following"
    PRICE_ACTION = "price_action"
    SCALPING = "scalping"
    NEWS = "news"
    SESSION_BREAKOUT = "session_breakout"


@dataclass(frozen=True)
class SignalCandidate:
    strategy: str
    direction: str
    reason: str
    price: float


class GoldStrategyEngine:
    def __init__(self, strategy_names: list[str]) -> None:
        self.strategy_names = [name.lower() for name in strategy_names]

    def evaluate(
        self,
        frame: pd.DataFrame,
        settings: object,
        higher_frame: pd.DataFrame | None = None,
        strategy_names: list[str] | None = None,
    ) -> list[SignalCandidate]:
        candidates: list[SignalCandidate] = []
        if frame.empty:
            return candidates

        active_strategies = {name.strip().lower() for name in (strategy_names or self.strategy_names) if str(name).strip()}

        close = frame["close"].astype(float)
        high = frame["high"].astype(float)
        low = frame["low"].astype(float)

        if "trend_following" in active_strategies:
            candidates.extend(self._trend_following(close, frame, settings))
        if "price_action" in active_strategies:
            candidates.extend(self._price_action(close, frame))
        if "scalping" in active_strategies:
            candidates.extend(self._scalping(close, frame, settings))
        if "news" in active_strategies:
            candidates.extend(self._news(close, high, low, frame))
        if "session_breakout" in active_strategies:
            candidates.extend(self._session_breakout(close, frame))

        return self._apply_higher_timeframe_bias(candidates, higher_frame, settings)

    def _apply_higher_timeframe_bias(
        self,
        candidates: list[SignalCandidate],
        higher_frame: pd.DataFrame | None,
        settings: object,
    ) -> list[SignalCandidate]:
        if not candidates or higher_frame is None or higher_frame.empty:
            return candidates

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
        return [candidate for candidate in candidates if candidate.direction == bias]

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

    def _scalping(self, close: pd.Series, frame: pd.DataFrame, settings: object) -> list[SignalCandidate]:
        if len(close) < 3:
            return []
        fast_period = int(getattr(settings, "ema_fast", 9))
        slow_period = int(getattr(settings, "ema_slow", 21))
        ema_fast = close.ewm(span=fast_period, adjust=False).mean()
        ema_slow = close.ewm(span=slow_period, adjust=False).mean()
        last_fast = float(ema_fast.iloc[-1])
        last_slow = float(ema_slow.iloc[-1])
        prev_fast = float(ema_fast.iloc[-2])
        prev_slow = float(ema_slow.iloc[-2])
        if last_fast > last_slow and prev_fast <= prev_slow:
            return [SignalCandidate(strategy="scalping", direction="buy", reason="EMA crossover", price=float(close.iloc[-1]))]
        if last_fast < last_slow and prev_fast >= prev_slow:
            return [SignalCandidate(strategy="scalping", direction="sell", reason="EMA crossover", price=float(close.iloc[-1]))]
        return []

    def _news(self, close: pd.Series, high: pd.Series, low: pd.Series, frame: pd.DataFrame) -> list[SignalCandidate]:
        if len(close) < 3:
            return []
        current = float(close.iloc[-1])
        prev = float(close.iloc[-2])
        if current > prev and high.iloc[-1] > high.iloc[-2]:
            return [SignalCandidate(strategy="news", direction="buy", reason="fade recovery after spike", price=current)]
        if current < prev and low.iloc[-1] < low.iloc[-2]:
            return [SignalCandidate(strategy="news", direction="sell", reason="fade recovery after spike", price=current)]
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
