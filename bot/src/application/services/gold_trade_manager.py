from __future__ import annotations

import math
from typing import Any

from bot.src.domain.services.gold_strategies import SignalCandidate
from bot.src.infrastructure.config.settings import GoldSettings


class GoldTradeManager:
    def __init__(self, settings: GoldSettings) -> None:
        self.settings = settings

    def build_ladder(
        self,
        candidate: SignalCandidate,
        stop_loss_pips: float | None = None,
        take_profit_pips: float | None = None,
    ) -> list[dict[str, Any]]:
        if not self.settings.enable_multi_entry:
            return [self._build_trade(candidate, 1, stop_loss_pips=stop_loss_pips, take_profit_pips=take_profit_pips)]

        entries: list[dict[str, Any]] = []
        for level in range(1, self.settings.ladder_entries + 1):
            entries.append(self._build_trade(candidate, level, stop_loss_pips=stop_loss_pips, take_profit_pips=take_profit_pips))
        return entries

    def update_exit_targets(
        self,
        entry_price: float,
        current_price: float,
        direction: str,
        stop_loss_pips: float | None = None,
        take_profit_pips: float | None = None,
        move_sl_pips: float | None = None,
        move_tp_pips: float | None = None,
    ) -> dict[str, float]:
        pip_size = float(getattr(self.settings, "pip_size", 0.01))
        sl_pips = float(stop_loss_pips) if stop_loss_pips is not None else float(self.settings.stop_loss_pips)
        tp_pips = float(take_profit_pips) if take_profit_pips is not None else float(self.settings.take_profit_pips)
        move_sl = float(move_sl_pips) if move_sl_pips is not None else max(1.0, sl_pips / 2.0)
        move_tp = float(move_tp_pips) if move_tp_pips is not None else max(1.0, tp_pips / 2.0)

        if direction == "buy":
            price_move = current_price - entry_price
            if price_move >= move_tp * pip_size:
                stop_loss = entry_price + (move_sl * pip_size)
                take_profit = current_price + (move_tp * pip_size)
            else:
                stop_loss = entry_price - (sl_pips * pip_size)
                take_profit = entry_price + (tp_pips * pip_size)
        else:
            price_move = entry_price - current_price
            if price_move >= move_tp * pip_size:
                stop_loss = entry_price - (move_sl * pip_size)
                take_profit = current_price - (move_tp * pip_size)
            else:
                stop_loss = entry_price + (sl_pips * pip_size)
                take_profit = entry_price - (tp_pips * pip_size)

        return {"stop_loss": round(stop_loss, 5), "take_profit": round(take_profit, 5)}

    def _build_trade(
        self,
        candidate: SignalCandidate,
        level: int,
        stop_loss_pips: float | None = None,
        take_profit_pips: float | None = None,
    ) -> dict[str, Any]:
        stop_pips = float(stop_loss_pips) if stop_loss_pips is not None else float(self.settings.stop_loss_pips)
        take_pips = float(take_profit_pips) if take_profit_pips is not None else float(self.settings.take_profit_pips)
        multiplier = self.settings.ladder_step_ratio ** (level - 1)
        price = candidate.price
        if candidate.direction == "buy":
            price = price + (stop_pips * self.settings.pip_size) * multiplier
        else:
            price = price - (stop_pips * self.settings.pip_size) * multiplier

        tp_base = take_pips
        tp_increment = tp_base * self.settings.ladder_step_ratio
        take_profit_pips = tp_base + (tp_increment * (level - 1))
        return {
            "strategy": candidate.strategy,
            "direction": candidate.direction,
            "reason": candidate.reason,
            "entry_price": round(price, 5),
            "take_profit_pips": round(take_profit_pips, 2),
            "stop_loss_pips": round(stop_pips, 2),
            "level": level,
        }

    def build_order_request(
        self,
        candidate: SignalCandidate,
        symbol: str,
        entry_price: float,
        account_info: dict[str, Any] | None,
        symbol_info: dict[str, Any] | None,
        stop_loss_pips: float | None = None,
        take_profit_pips: float | None = None,
        level: int = 1,
    ) -> dict[str, Any]:
        pip_size = float(getattr(self.settings, "pip_size", 0.01))
        configured_stop_pips = float(getattr(self.settings, "stop_loss_pips", 0.0))
        configured_take_pips = float(getattr(self.settings, "take_profit_pips", 0.0))
        stop_pips_raw = float(stop_loss_pips) if stop_loss_pips is not None else configured_stop_pips
        take_pips_raw = float(take_profit_pips) if take_profit_pips is not None else configured_take_pips
        stop_pips = stop_pips_raw if stop_pips_raw > 0 else configured_stop_pips
        take_pips = take_pips_raw if take_pips_raw > 0 else configured_take_pips
        stop_distance = stop_pips * pip_size
        take_distance = take_pips * pip_size
        if candidate.direction == "buy":
            stop_loss = entry_price - stop_distance
            take_profit = entry_price + take_distance
        else:
            stop_loss = entry_price + stop_distance
            take_profit = entry_price - take_distance

        volume = self._calculate_volume(account_info, symbol_info, stop_distance)
        return {
            "symbol": symbol,
            "direction": candidate.direction,
            "volume": volume,
            "entry_price": entry_price,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "strategy": candidate.strategy,
            "reason": candidate.reason,
            "level": int(level),
        }

    def _calculate_volume(self, account_info: dict[str, Any] | None, symbol_info: dict[str, Any] | None, stop_distance: float) -> float:
        default_volume = 0.1
        fixed_lot_size = float(getattr(self.settings, "fixed_lot_size", 0.0))
        if fixed_lot_size > 0:
            return self._normalize_volume(fixed_lot_size, symbol_info)

        return self._normalize_volume(default_volume, symbol_info)

    def _normalize_volume(self, raw_volume: float, symbol_info: dict[str, Any] | None) -> float:
        if not symbol_info:
            return round(max(0.01, raw_volume), 2)
        min_volume = float(symbol_info.get("volume_min") or 0.01)
        max_volume = float(symbol_info.get("volume_max") or 100.0)
        step = float(symbol_info.get("volume_step") or 0.01)
        if step <= 0:
            step = 0.01
        stepped = math.floor(raw_volume / step) * step
        bounded = max(min_volume, min(max_volume, stepped))
        return round(bounded, 2)
