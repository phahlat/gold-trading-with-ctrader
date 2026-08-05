from pathlib import Path

from bot.src.infrastructure.market_data.csv_loader import load_backtest_ltf_htf_frames, resolve_backtest_timeframe_file


def test_resolve_backtest_timeframe_file_prefers_matching_symbol() -> None:
    data_dir = Path("gold_bot/backtest/data")
    ltf = resolve_backtest_timeframe_file(data_source=data_dir, symbol="XAUUSD", timeframe="M15")
    htf = resolve_backtest_timeframe_file(data_source=data_dir, symbol="XAUUSD", timeframe="H1")

    assert ltf.name == "XAUUSD15.csv"
    assert htf.name == "XAUUSD60.csv"


def test_load_backtest_ltf_htf_frames_supports_gold_alias() -> None:
    data_dir = Path("gold_bot/backtest/data")
    lower, higher, lower_path, higher_path = load_backtest_ltf_htf_frames(
        data_source=data_dir,
        symbol="GOLD",
        lower_timeframe="M15",
        higher_timeframe="H1",
    )

    assert lower_path.name == "XAUUSD15.csv"
    assert higher_path.name == "XAUUSD60.csv"
    assert not lower.empty
    assert not higher.empty


def test_load_backtest_frames_uses_timeframes_even_when_source_is_single_file() -> None:
    data_file = Path("gold_bot/backtest/data/XAUUSD15.csv")
    lower, higher, lower_path, higher_path = load_backtest_ltf_htf_frames(
        data_source=data_file,
        symbol="XAUUSD",
        lower_timeframe="M5",
        higher_timeframe="H1",
    )

    assert lower_path.name == "XAUUSD5.csv"
    assert higher_path.name == "XAUUSD60.csv"
    assert not lower.empty
    assert not higher.empty
