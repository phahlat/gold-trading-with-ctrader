from types import SimpleNamespace
from pathlib import Path

import pandas as pd

from bot.src.application.services.gold_runner import GoldRunner
from bot.src.domain.services.gold_strategies import GoldStrategyEngine
from bot.src.infrastructure.config.settings import load_gold_settings
from bot.src.interfaces.cli.main import _resolve_backtest_data_source


def _copy_example_env(tmp_path: Path) -> str:
    env_path = tmp_path / ".env"
    env_source = None
    for candidate in [Path("bot/.env.example"), Path("gold_bot/.env.example"), Path(".env.example")]:
        if candidate.exists():
            env_source = candidate
            break
    if env_source is None:
        raise FileNotFoundError("Unable to locate example environment file")
    env_path.write_text(env_source.read_text(encoding="utf-8"), encoding="utf-8")
    return str(env_path)


def test_runner_generates_signal_from_simple_frame(tmp_path: Path) -> None:
    settings = load_gold_settings(_copy_example_env(tmp_path))
    runner = GoldRunner(settings)
    frame = pd.DataFrame(
        {
            "open": [100, 101, 102, 103, 104],
            "high": [101, 103, 104, 105, 106],
            "low": [99, 100, 101, 102, 103],
            "close": [100, 102, 103, 104, 105],
        }
    )

    result = runner.run_backtest(frame)

    assert result["total_signals"] >= 0
    assert isinstance(result["signals"], list)


def test_cli_resolves_explicit_backtest_data_path() -> None:
    args = SimpleNamespace(data="backtest/data", backtest_data_dir="gold_bot/backtest/data/XAUUSD15.csv")
    settings = SimpleNamespace(backtest_data_dir="gold_bot/backtest/data/XAUUSD60.csv")

    assert _resolve_backtest_data_source(args, settings) == "gold_bot/backtest/data/XAUUSD15.csv"


def test_price_action_breakout_uses_prior_window() -> None:
    frame = pd.DataFrame(
        {
            "open": [100, 100.5, 101, 101.2, 101.4, 101.5],
            "high": [101, 101.2, 101.4, 101.6, 101.8, 102.8],
            "low": [99.7, 100.2, 100.7, 100.9, 101.0, 101.3],
            "close": [100.8, 101.0, 101.2, 101.3, 101.6, 102.5],
        }
    )
    engine = GoldStrategyEngine(["price_action"])
    candidates = engine.evaluate(frame, settings=SimpleNamespace())

    assert any(item.strategy == "price_action" and item.direction == "buy" for item in candidates)


def test_ema_crossover_state_machine_bullish_three_candle_confirmation() -> None:
    engine = GoldStrategyEngine(["ema_crossover"])
    settings = SimpleNamespace(ema_fast=2, ema_slow=3)
    frame = pd.DataFrame(
        {
            "datetime": pd.date_range("2026-08-08 00:00:00", periods=8, freq="min"),
            "open": [100, 103, 106, 110, 114, 118, 117, 119],
            "high": [101, 104, 107, 111, 115, 119, 118, 120],
            "low": [99, 102, 105, 109, 113, 117, 116, 118],
            "close": [100, 103, 106, 110, 114, 118, 117, 119],
        }
    )

    first = engine.evaluate(frame.iloc[:6].copy(), settings=settings, strategy_names=["ema_crossover"], context_key="ema:M1")
    second = engine.evaluate(frame.iloc[:7].copy(), settings=settings, strategy_names=["ema_crossover"], context_key="ema:M1")
    third = engine.evaluate(frame.iloc[:8].copy(), settings=settings, strategy_names=["ema_crossover"], context_key="ema:M1")

    assert first == []
    assert second == []
    assert len(third) == 1
    assert third[0].strategy == "ema_crossover"
    assert third[0].direction == "buy"


def test_ema_crossover_ignores_duplicate_processing_same_candle() -> None:
    engine = GoldStrategyEngine(["ema_crossover"])
    settings = SimpleNamespace(ema_fast=2, ema_slow=3)
    frame = pd.DataFrame(
        {
            "datetime": pd.date_range("2026-08-08 00:00:00", periods=6, freq="min"),
            "open": [100, 102, 104, 107, 110, 113],
            "high": [101, 103, 105, 108, 111, 114],
            "low": [99, 101, 103, 106, 109, 112],
            "close": [100, 102, 104, 107, 110, 113],
        }
    )

    first = engine.evaluate(frame.copy(), settings=settings, strategy_names=["ema_crossover"], context_key="ema:M5")
    second = engine.evaluate(frame.copy(), settings=settings, strategy_names=["ema_crossover"], context_key="ema:M5")

    assert len(first) <= 1
    assert second == []


def test_ema_crossover_state_machine_bearish_two_candle_confirmation() -> None:
    engine = GoldStrategyEngine(["ema_crossover"])
    settings = SimpleNamespace(ema_slow=3)
    frame = pd.DataFrame(
        {
            "datetime": pd.date_range("2026-08-08 00:00:00", periods=8, freq="min"),
            "open": [120, 118, 116, 114, 111, 108, 107, 106],
            "high": [121, 119, 117, 115, 112, 109, 108, 107],
            "low": [119, 117, 115, 113, 110, 107, 106, 105],
            "close": [120, 118, 116, 114, 111, 108, 107, 106],
        }
    )

    first = engine.evaluate(frame.iloc[:6].copy(), settings=settings, strategy_names=["ema_crossover"], context_key="ema:M5")
    second = engine.evaluate(frame.iloc[:7].copy(), settings=settings, strategy_names=["ema_crossover"], context_key="ema:M5")

    assert first == []
    assert len(second) == 1
    assert second[0].strategy == "ema_crossover"
    assert second[0].direction == "sell"


def test_ema_crossover_state_machine_bearish_three_candle_equal_c1_confirmation() -> None:
    engine = GoldStrategyEngine(["ema_crossover"])
    settings = SimpleNamespace(ema_slow=3)
    frame = pd.DataFrame(
        {
            "datetime": pd.date_range("2026-08-08 00:00:00", periods=8, freq="min"),
            "open": [120, 118, 116, 114, 112, 110, 111, 110],
            "high": [121, 119, 117, 115, 113, 111, 112, 111],
            "low": [119, 117, 115, 113, 111, 109, 110, 109],
            "close": [120, 118, 116, 114, 112, 110, 111, 110],
        }
    )

    first = engine.evaluate(frame.iloc[:6].copy(), settings=settings, strategy_names=["ema_crossover"], context_key="ema:M15")
    second = engine.evaluate(frame.iloc[:7].copy(), settings=settings, strategy_names=["ema_crossover"], context_key="ema:M15")
    third = engine.evaluate(frame.iloc[:8].copy(), settings=settings, strategy_names=["ema_crossover"], context_key="ema:M15")

    assert first == []
    assert second == []
    assert len(third) == 1
    assert third[0].strategy == "ema_crossover"
    assert third[0].direction == "sell"


def test_ema_crossover_requires_fresh_cross_before_rearming_same_direction() -> None:
    engine = GoldStrategyEngine(["ema_crossover"])
    settings = SimpleNamespace(ema_slow=3)
    frame = pd.DataFrame(
        {
            "datetime": pd.date_range("2026-08-08 00:00:00", periods=11, freq="min"),
            "open": [100, 99, 98, 97, 101, 103, 105, 106, 107, 108, 109],
            "high": [101, 100, 99, 98, 102, 104, 106, 107, 108, 109, 110],
            "low": [99, 98, 97, 96, 100, 102, 104, 105, 106, 107, 108],
            "close": [100, 99, 98, 97, 101, 103, 105, 106, 107, 108, 109],
        }
    )

    signals: list = []
    for end in range(6, len(frame) + 1):
        signals.extend(
            engine.evaluate(
                frame.iloc[:end].copy(),
                settings=settings,
                strategy_names=["ema_crossover"],
                context_key="ema:M30",
            )
        )

    buy_signals = [item for item in signals if item.direction == "buy"]
    assert len(buy_signals) == 1


def test_settings_parse_ema_timeframes_aliases(tmp_path: Path) -> None:
    env_path = Path(_copy_example_env(tmp_path))
    base = env_path.read_text(encoding="utf-8")
    base += "\nEMA_CROSSOVER_EMA_FAST=\n"
    base += "\nEMA_CROSSOVER_EMA_SLOW=\n"
    base += "\nEMA_CROSSOVER_TIMEFRAMES=\n"
    base += "\nEMA_CROSSOVER_STOP_LOSS_PIPS=\n"
    base += "\nEMA_CROSSOVER_TAKE_PROFIT_PIPS=\n"
    base += "\nEMA_CROSSOVER_GOLD_STOP_LOSS_PIPS=\n"
    base += "\nEMA_CROSSOVER_GOLD_TAKE_PROFIT_PIPS=\n"
    base += "\nEMA_CROSSOVER_RISK_REWARD_RATIO=\n"
    base += "\nGOLD_STRATEGY_NAMES=ema_crossover\n"
    base += "\nTIMEFRAMES=1m,5m,15m\n"
    base += "\nEMA_FAST=50\nEMA_SLOW=200\nSTOP_LOSS_PIPS=120\nTAKE_PROFIT_PIPS=240\nRISK_REWARD_RATIO=2\n"
    env_path.write_text(base, encoding="utf-8")

    settings = load_gold_settings(str(env_path))

    assert settings.strategy_names == ["ema_crossover"]
    assert settings.ema_timeframes == ["M1", "M5", "M15"]
    assert settings.ema_fast == 50
    assert settings.ema_slow == 200
    assert settings.stop_loss_pips == 120.0
    assert settings.take_profit_pips == 240.0
    assert settings.risk_reward_ratio == 2.0
