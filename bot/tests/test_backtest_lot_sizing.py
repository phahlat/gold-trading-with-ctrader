from __future__ import annotations

from types import SimpleNamespace

import pandas as pd

import bot.src.application.services.gold_backtest_service as gold_backtest_service_module
from bot.src.application.services.gold_backtest_service import GoldBacktestService
from bot.src.application.services.gold_trade_manager import GoldTradeManager
from bot.src.domain.services.gold_strategies import SignalCandidate


class _NoopRunner:
    pass


class _NoopChart:
    def close(self) -> None:
        return None

    def render_dual_timeframe(self, *args, **kwargs) -> None:  # pragma: no cover - defensive no-op
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


def test_backtest_volume_normalization_keeps_fixed_lot_at_broker_minimum() -> None:
    settings = SimpleNamespace(backtest_max_volume_cap=0.0)
    service = _service(settings)

    normalized, was_capped = service._normalize_backtest_volume(
        raw_volume=0.01,
        profile={"volume_min": 0.01, "volume_max": 100.0, "volume_step": 0.01},
    )

    assert normalized == 0.01
    assert was_capped is False


def test_backtest_broker_profile_preserves_point_zero_one_lot_size(monkeypatch) -> None:
    settings = SimpleNamespace(
        backtest_use_broker_profile=True,
        backtest_volume_min=0.01,
        backtest_volume_max=50.0,
        backtest_volume_step=0.01,
        backtest_default_contract_size=100.0,
        backtest_default_leverage=100.0,
        symbols=["XAUUSD"],
        backtest_max_volume_cap=0.0,
    )
    service = _service(settings)

    class FakeConnector:
        def __init__(self, _settings) -> None:
            pass

        def connect(self) -> bool:
            return True

        def disconnect(self) -> None:
            return None

        def resolve_symbol(self, requested_symbol: str) -> str:
            return requested_symbol

        def symbol_info(self, _symbol: str) -> dict[str, float]:
            return {
                "volume_min": 0.01,
                "volume_max": 100.0,
                "volume_step": 0.01,
                "trade_contract_size": 10000.0,
                "margin_initial": 0.0,
            }

        def account_info(self) -> dict[str, float]:
            return {"leverage": 100.0}

    monkeypatch.setattr(gold_backtest_service_module, "GoldCTraderConnector", FakeConnector)

    profile = service._resolve_backtest_profile()
    normalized, was_capped = service._normalize_backtest_volume(raw_volume=0.01, profile=profile)

    assert profile["source"] == "ctrader"
    assert profile["volume_min"] == 0.01
    assert profile["volume_step"] == 0.01
    assert normalized == 0.01
    assert was_capped is False


def test_build_order_request_respects_fixed_lot_size_of_point_zero_one() -> None:
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
        stop_loss_pips=150.0,
        take_profit_pips=450.0,
        level=1,
    )

    assert order["volume"] == 0.01


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


def test_multi_timeframe_backtest_uses_strategy_specific_ltf_and_htf_frames() -> None:
    calls: list[tuple[str, int, int]] = []

    class RunnerStub:
        def evaluate_candidates(self, frame, higher_frame=None, strategy_names=None):
            strategy = (strategy_names or [""])[0]
            calls.append((strategy, len(frame), len(higher_frame) if higher_frame is not None else 0))
            return []

    settings = SimpleNamespace(
        strategy_names=["trend_following", "price_action", "session_breakout"],
        strategy_presets={
            "trend_following": SimpleNamespace(lower_timeframe="M30", higher_timeframe="H4", stop_loss_pips=120.0, take_profit_pips=250.0),
            "price_action": SimpleNamespace(lower_timeframe="M5", higher_timeframe="M30", stop_loss_pips=80.0, take_profit_pips=180.0),
            "session_breakout": SimpleNamespace(lower_timeframe="M15", higher_timeframe="H1", stop_loss_pips=100.0, take_profit_pips=220.0),
        },
        lower_timeframe="M15",
        higher_timeframe="H1",
        stop_loss_pips=100.0,
        take_profit_pips=140.0,
        ema_trend_period=20,
        backtest_initial_balance=1000.0,
        symbols=["XAUUSD"],
        plot_ltf_candles=120,
        plot_htf_candles=90,
        plot_enabled=False,
        refresh_candle_count=5,
        enable_multi_entry=False,
        ladder_entries=1,
        ladder_step_ratio=1.0,
        fixed_lot_size=0.01,
        backtest_fixed_volume=0.01,
        backtest_warn_volume_above=0.0,
        backtest_warn_equity_multiplier=20.0,
        backtest_use_broker_profile=False,
        config_env_path="bot/.env",
        pip_size=0.01,
    )

    service = GoldBacktestService(settings=settings, runner=RunnerStub(), chart_renderer=_NoopChart())

    def _frame(datetimes: list[str], closes: list[float]) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "datetime": pd.to_datetime(datetimes),
                "open": closes,
                "high": [value + 1.0 for value in closes],
                "low": [value - 1.0 for value in closes],
                "close": closes,
            }
        )

    frames_by_timeframe = {
        "M5": _frame(
            [
                "2026-08-01 00:00:00",
                "2026-08-01 00:05:00",
                "2026-08-01 00:10:00",
                "2026-08-01 00:15:00",
                "2026-08-01 00:20:00",
                "2026-08-01 00:25:00",
            ],
            [4000.0, 4001.0, 4002.0, 4003.0, 4004.0, 4005.0],
        ),
        "M15": _frame(
            ["2026-08-01 00:00:00", "2026-08-01 00:15:00", "2026-08-01 00:30:00", "2026-08-01 00:45:00"],
            [4010.0, 4011.0, 4012.0, 4013.0],
        ),
        "M30": _frame(
            ["2026-08-01 00:00:00", "2026-08-01 00:30:00"],
            [4020.0, 4021.0],
        ),
        "H1": _frame(
            ["2026-08-01 00:00:00", "2026-08-01 01:00:00"],
            [4030.0, 4031.0],
        ),
        "H4": _frame(
            ["2026-08-01 00:00:00"],
            [4040.0],
        ),
    }

    result = service.run(
        lower_frame=pd.DataFrame(),
        higher_frame=pd.DataFrame(),
        source_name="xauusd",
        artifact_stem="multi_tf_dispatch",
        frames_by_timeframe=frames_by_timeframe,
        strategy_timeframe_paths={},
    )

    max_lens: dict[str, tuple[int, int]] = {}
    for strategy, ltf_len, htf_len in calls:
        prev_ltf, prev_htf = max_lens.get(strategy, (0, 0))
        max_lens[strategy] = (max(prev_ltf, ltf_len), max(prev_htf, htf_len))

    assert max_lens["trend_following"] == (2, 1)
    assert max_lens["price_action"] == (6, 2)
    assert max_lens["session_breakout"] == (4, 1)
    assert result["processed_bars"] > 0
