from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pandas as pd

from bot.src.application.services.gold_trade_manager import GoldTradeManager
from bot.src.domain.services.gold_strategies import GoldStrategyEngine
from bot.src.infrastructure.config.settings import GoldSettings

logger = logging.getLogger(__name__)


class GoldRunner:
    def __init__(self, settings: GoldSettings) -> None:
        self.settings = settings
        self.engine = GoldStrategyEngine(settings.strategy_names)
        self.trade_manager = GoldTradeManager(settings)

    def evaluate_candidates(
        self,
        frame: pd.DataFrame,
        higher_frame: pd.DataFrame | None = None,
        strategy_names: list[str] | None = None,
    ) -> list[Any]:
        return self.engine.evaluate(frame, self.settings, higher_frame=higher_frame, strategy_names=strategy_names)

    def run_once(self, frame: pd.DataFrame, higher_frame: pd.DataFrame | None = None) -> list[dict[str, Any]]:
        candidates = self.evaluate_candidates(frame, higher_frame=higher_frame)
        if not candidates:
            return []

        trades: list[dict[str, Any]] = []
        for candidate in candidates:
            ladder_entries = self.trade_manager.build_ladder(candidate)
            trades.extend(ladder_entries)
        return trades

    def run_backtest(self, frame: pd.DataFrame, higher_frame: pd.DataFrame | None = None) -> dict[str, Any]:
        history: list[dict[str, Any]] = []
        for idx in range(len(frame)):
            slab = frame.iloc[: idx + 1].copy()
            higher_slab = None
            if higher_frame is not None:
                if "datetime" in slab.columns and "datetime" in higher_frame.columns:
                    cutoff = slab.iloc[-1]["datetime"]
                    higher_slab = higher_frame[higher_frame["datetime"] <= cutoff].copy()
                else:
                    higher_slab = higher_frame.iloc[: idx + 1].copy()
            signals = self.run_once(slab, higher_frame=higher_slab)
            for signal in signals:
                history.append(signal)
        return {"signals": history, "total_signals": len(history)}
