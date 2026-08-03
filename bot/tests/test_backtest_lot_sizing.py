from __future__ import annotations

from types import SimpleNamespace

import pandas as pd

from bot.src.application.services.gold_backtest_service import GoldBacktestService
from bot.src.application.services.gold_trade_manager import GoldTradeManager
from bot.src.domain.services.gold_strategies import SignalCandidate


class _NoopRunner:
    pass


class _NoopChart:
    def close(self) -> None:
        return None


def _service(settings: SimpleNamespace) -> GoldBacktestService:
    return GoldBacktestService(settings=settings, runner=_NoopRunner(), chart_renderer=_NoopChart())


def test_backtest_volume_prefers_backtest_fixed_volume() -> None:
    settings = SimpleNamespace(backtest_fixed_volume=0.03, fixed_lot_size=0.07, risk_percent=1.0, stop_loss_pips=120.0)
    service = _service(settings)

    volume, rule = service._resolve_backtest_volume(1000.0)

    assert volume == 0.03
    assert rule == "backtest_fixed_volume"


def test_backtest_volume_uses_fixed_lot_when_backtest_fixed_not_set() -> None:
    settings = SimpleNamespace(backtest_fixed_volume=0.0, fixed_lot_size=0.07, risk_percent=1.0, stop_loss_pips=120.0)
    service = _service(settings)

    volume, rule = service._resolve_backtest_volume(1000.0)

    assert volume == 0.07
    assert rule == "gold_fixed_lot_size"


def test_backtest_volume_falls_back_to_default_minimum_volume() -> None:
    settings = SimpleNamespace(backtest_fixed_volume=0.0, fixed_lot_size=0.0, stop_loss_pips=100.0)
    service = _service(settings)

    volume, rule = service._resolve_backtest_volume(1000.0)

    assert round(volume, 2) == 0.01
    assert rule == "default_minimum_volume"


def test_backtest_volume_normalization_caps_to_profile_max() -> None:
    settings = SimpleNamespace(backtest_max_volume_cap=0.0)
    service = _service(settings)

    normalized, was_capped = service._normalize_backtest_volume(
        raw_volume=12.73,
        profile={"volume_min": 0.01, "volume_max": 2.0, "volume_step": 0.01},
    )

    assert normalized == 2.0
    assert was_capped is True


def test_backtest_margin_rejection_triggers_when_required_margin_exceeds_equity() -> None:
    settings = SimpleNamespace(backtest_simulate_margin_rejection=True, backtest_margin_available_ratio=0.95)
    service = _service(settings)

    should_reject = service._should_reject_for_margin(
        entry_price=3000.0,
        volume=1.0,
        equity=1000.0,
        profile={"contract_size": 100.0, "leverage": 100.0, "margin_initial": 0.0},
    )

    assert should_reject is True


def test_backtest_outcome_scales_with_volume() -> None:
    settings = SimpleNamespace(pip_size=0.01, stop_loss_pips=120.0, take_profit_pips=250.0)
    service = _service(settings)
    frame = pd.DataFrame(
        {
            "close": [2000.00, 2001.00],
        }
    )

    pnl = service._estimate_trade_outcome(
        lower_frame=frame,
        idx=0,
        direction="buy",
        entry_price=2000.00,
        volume=0.10,
    )

    # 1.00 move at pip_size 0.01 -> 100 pips, multiplied by 0.10 lots.
    assert pnl == 10.0


def test_ladder_step_ratio_scales_tp_targets_for_later_levels() -> None:
    settings = SimpleNamespace(
        enable_multi_entry=True,
        ladder_entries=3,
        ladder_step_ratio=1.2,
        stop_loss_pips=120.0,
        take_profit_pips=240.0,
        fixed_lot_size=0.0,
        risk_percent=1.0,
        pip_size=0.01,
    )
    manager = GoldTradeManager(settings)
    candidate = SignalCandidate(strategy="trend_following", direction="buy", reason="test", price=1000.0)

    ladder = manager.build_ladder(candidate)

    assert ladder[0]["take_profit_pips"] == 240.0
    assert ladder[1]["take_profit_pips"] == 528.0
    assert ladder[2]["take_profit_pips"] == 816.0


def test_ladder_step_ratio_below_one_scales_tp_targets_downward() -> None:
    settings = SimpleNamespace(
        enable_multi_entry=True,
        ladder_entries=3,
        ladder_step_ratio=0.5,
        stop_loss_pips=150.0,
        take_profit_pips=450.0,
        fixed_lot_size=0.01,
        risk_percent=1.0,
        pip_size=0.01,
    )
    manager = GoldTradeManager(settings)
    candidate = SignalCandidate(strategy="trend_following", direction="buy", reason="test", price=1000.0)

    ladder = manager.build_ladder(candidate)

    assert ladder[0]["take_profit_pips"] == 450.0
    assert ladder[1]["take_profit_pips"] == 675.0
    assert ladder[2]["take_profit_pips"] == 900.0


def test_move_sl_tp_rule_updates_targets_once_price_moves_favorably() -> None:
    settings = SimpleNamespace(
        pip_size=0.01,
        stop_loss_pips=120.0,
        take_profit_pips=240.0,
    )
    manager = GoldTradeManager(settings)

    updated = manager.update_exit_targets(
        entry_price=1000.0,
        current_price=1010.0,
        direction="buy",
        stop_loss_pips=120.0,
        take_profit_pips=240.0,
        move_sl_pips=60.0,
        move_tp_pips=120.0,
    )

    assert updated["stop_loss"] == 1000.6
    assert updated["take_profit"] == 1011.2


def test_build_order_request_falls_back_to_configured_pips_when_zero_is_passed() -> None:
    settings = SimpleNamespace(
        pip_size=0.01,
        stop_loss_pips=150.0,
        take_profit_pips=450.0,
        fixed_lot_size=0.01,
        risk_percent=1.0,
    )
    manager = GoldTradeManager(settings)
    candidate = SignalCandidate(strategy="trend_following", direction="buy", reason="test", price=4106.47)

    order = manager.build_order_request(
        candidate=candidate,
        symbol="GOLD",
        entry_price=4106.47,
        account_info={"equity": 1000.0},
        symbol_info={"volume_min": 0.01, "volume_max": 100.0, "volume_step": 0.01},
        stop_loss_pips=0.0,
        take_profit_pips=0.0,
        level=1,
    )

    assert order["stop_loss"] == 4104.97
    assert order["take_profit"] == 4110.97
