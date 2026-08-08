from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

import bot.src.infrastructure.ctrader.connector as ctrader_connector_module

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bot.src.application.services.gold_live_service import GoldLiveService
from bot.src.application.services.gold_runner import GoldRunner
from bot.src.infrastructure.charting.live_plot import LiveChartRenderer
from bot.src.infrastructure.config.settings import load_gold_settings
from bot.src.infrastructure.ctrader.connector import GoldCTraderConnector
from bot.src.infrastructure.persistence.sqlite_store import GoldPositionStore


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


def test_ctrader_connector_requires_credentials() -> None:
    settings = SimpleNamespace(
        ctrader_client_id="",
        ctrader_client_secret="",
        ctrader_access_token="",
        ctrader_refresh_token="",
        ctrader_account_id=0,
        ctrader_host="demo",
        ctrader_request_timeout_seconds=3.0,
        ctrader_connect_timeout_seconds=3.0,
    )
    connector = GoldCTraderConnector(settings)

    assert connector.connect() is False


def test_connector_retries_initial_connect_on_transient_failure(monkeypatch) -> None:
    settings = SimpleNamespace(
        ctrader_client_id="id",
        ctrader_client_secret="secret",
        ctrader_access_token="token",
        ctrader_refresh_token="refresh",
        ctrader_account_id=123,
        ctrader_host="demo",
        ctrader_request_timeout_seconds=3.0,
        ctrader_connect_timeout_seconds=3.0,
        ctrader_reconnect_attempts=2,
        ctrader_reconnect_wait_seconds=0.0,
    )
    connector = GoldCTraderConnector(settings)
    attempts = {"count": 0}

    class FakeClient:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def setConnectedCallback(self, *_args, **_kwargs) -> None:
            return None

        def setDisconnectedCallback(self, *_args, **_kwargs) -> None:
            return None

        def setMessageReceivedCallback(self, *_args, **_kwargs) -> None:
            return None

        def startService(self) -> None:
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise RuntimeError("transient startup issue")

        def stopService(self) -> None:
            return None

    monkeypatch.setattr(ctrader_connector_module, "Client", FakeClient)
    monkeypatch.setattr(ctrader_connector_module, "_ensure_reactor_running", lambda: None)
    monkeypatch.setattr(connector, "_call_in_reactor", lambda fn, timeout: fn())
    monkeypatch.setattr(connector, "_wait_for_socket_ready", lambda timeout: None)
    monkeypatch.setattr(connector, "_authenticate_application_and_account", lambda: None)
    monkeypatch.setattr(connector, "_prime_symbol_catalog", lambda: None)

    assert connector.connect() is True
    assert attempts["count"] == 2
    assert connector._connected is True


def test_ctrader_volume_unit_conversion_uses_lots() -> None:
    settings = SimpleNamespace(
        ctrader_client_id="id",
        ctrader_client_secret="secret",
        ctrader_access_token="token",
        ctrader_refresh_token="refresh",
        ctrader_account_id=123,
        ctrader_host="demo",
        ctrader_request_timeout_seconds=3.0,
        ctrader_connect_timeout_seconds=3.0,
    )
    connector = GoldCTraderConnector(settings)

    assert connector._volume_from_api(100) == 0.01
    assert connector._volume_from_api(1000000) == 100.0
    assert connector._volume_to_api(0.01) == 100
    assert connector._volume_to_api(1.0) == 10000


def test_connector_marks_stale_socket_for_reconnect(monkeypatch) -> None:
    settings = SimpleNamespace(
        ctrader_client_id="id",
        ctrader_client_secret="secret",
        ctrader_access_token="token",
        ctrader_refresh_token="refresh",
        ctrader_account_id=123,
        ctrader_host="demo",
        ctrader_request_timeout_seconds=3.0,
        ctrader_connect_timeout_seconds=3.0,
        ctrader_reconnect_attempts=1,
        ctrader_reconnect_wait_seconds=0.0,
        ctrader_heartbeat_interval_seconds=0.1,
        ctrader_heartbeat_timeout_seconds=0.1,
    )
    connector = GoldCTraderConnector(settings)
    connector._connected = True
    connector._last_activity_at = time.monotonic() - 1.0
    reconnect_calls: list[float | None] = []

    monkeypatch.setattr(connector, "_schedule_reconnect", lambda delay=None: reconnect_calls.append(delay))

    connector._handle_stale_connection()

    assert reconnect_calls == [0.0]


def test_connector_does_not_auto_reconnect_after_intentional_disconnect(monkeypatch) -> None:
    settings = SimpleNamespace(
        ctrader_client_id="id",
        ctrader_client_secret="secret",
        ctrader_access_token="token",
        ctrader_refresh_token="refresh",
        ctrader_account_id=123,
        ctrader_host="demo",
        ctrader_request_timeout_seconds=3.0,
        ctrader_connect_timeout_seconds=3.0,
    )
    connector = GoldCTraderConnector(settings)
    connector._intentional_disconnect = True
    connector._connected = True
    reconnect_calls: list[float | None] = []

    monkeypatch.setattr(connector, "_schedule_reconnect", lambda delay=None: reconnect_calls.append(delay))

    connector._on_socket_disconnected(SimpleNamespace(), None)

    assert connector._connected is False
    assert reconnect_calls == []


def test_call_in_reactor_executes_inline_when_on_reactor_thread(monkeypatch) -> None:
    settings = SimpleNamespace(
        ctrader_client_id="id",
        ctrader_client_secret="secret",
        ctrader_access_token="token",
        ctrader_refresh_token="refresh",
        ctrader_account_id=123,
        ctrader_host="demo",
        ctrader_request_timeout_seconds=3.0,
        ctrader_connect_timeout_seconds=3.0,
    )
    connector = GoldCTraderConnector(settings)

    monkeypatch.setattr(ctrader_connector_module, "_REACTOR_THREAD", threading.current_thread())

    assert connector._call_in_reactor(lambda: 42, timeout=1.0) == 42


def test_position_store_persists_and_closes_positions(tmp_path: Path) -> None:
    store = GoldPositionStore(tmp_path / "positions.sqlite3")

    store.upsert_position(
        {
            "position_key": "bot:ticket-1:XAUUSD",
            "ticket": 1,
            "symbol": "XAUUSD",
            "direction": "buy",
            "volume": 0.1,
            "entry_price": 2345.0,
            "stop_loss": 2335.0,
            "take_profit": 2365.0,
            "strategy": "trend_following",
            "source": "bot",
            "is_external": 0,
            "status": "open",
            "opened_at": "2026-07-28T12:00:00",
        }
    )

    open_positions = store.list_positions(status="open")
    assert len(open_positions) == 1

    store.mark_closed("bot:ticket-1:XAUUSD", 2350.0, "2026-07-28T12:05:00")
    closed_positions = store.list_positions(status="closed")
    assert len(closed_positions) == 1
    assert closed_positions[0]["close_price"] == 2350.0


def test_connector_get_rates_normalizes_trendbars(monkeypatch) -> None:
    settings = SimpleNamespace(
        ctrader_client_id="id",
        ctrader_client_secret="secret",
        ctrader_access_token="token",
        ctrader_refresh_token="refresh",
        ctrader_account_id=123,
        ctrader_host="demo",
        ctrader_request_timeout_seconds=3.0,
        ctrader_connect_timeout_seconds=3.0,
    )
    connector = GoldCTraderConnector(settings)
    connector._connected = True
    connector._account_id = 123
    connector._symbols_by_name = {"XAUUSD": {"symbolId": 987, "symbolName": "XAUUSD"}}

    class FakeTrendbar:
        def __init__(self) -> None:
            self.utcTimestampInMinutes = 1710000000 // 60
            self.low = 230000000
            self.deltaOpen = 100000
            self.deltaClose = 120000
            self.deltaHigh = 180000
            self.volume = 42

    class FakeResponse:
        def __init__(self) -> None:
            self.trendbar = [FakeTrendbar()]

    monkeypatch.setattr(connector, "_send_and_extract", lambda request, timeout=None: FakeResponse())

    rates = connector.get_rates("XAUUSD", "M15", count=1)

    assert len(rates) == 1
    assert rates[0]["symbol"] == "XAUUSD"
    assert rates[0]["open"] == 2301.0
    assert rates[0]["close"] == 2301.2
    assert rates[0]["high"] == 2301.8
    assert rates[0]["low"] == 2300.0
    assert rates[0]["tick_volume"] == 42


def test_connector_resolves_gold_alias_to_broker_symbol() -> None:
    settings = SimpleNamespace(
        ctrader_client_id="id",
        ctrader_client_secret="secret",
        ctrader_access_token="token",
        ctrader_refresh_token="refresh",
        ctrader_account_id=123,
        ctrader_host="demo",
        ctrader_request_timeout_seconds=3.0,
        ctrader_connect_timeout_seconds=3.0,
    )
    connector = GoldCTraderConnector(settings)
    connector._connected = True
    connector._symbols_by_name = {
        "EURUSD": {"symbolId": 1, "symbolName": "EURUSD"},
        "XAUUSD": {"symbolId": 2, "symbolName": "XAUUSD"},
        "GBPUSD": {"symbolId": 3, "symbolName": "GBPUSD"},
    }

    assert connector.resolve_symbol("GOLD") == "XAUUSD"


def test_connector_resolves_gold_alias_with_suffix() -> None:
    settings = SimpleNamespace(
        ctrader_client_id="id",
        ctrader_client_secret="secret",
        ctrader_access_token="token",
        ctrader_refresh_token="refresh",
        ctrader_account_id=123,
        ctrader_host="demo",
        ctrader_request_timeout_seconds=3.0,
        ctrader_connect_timeout_seconds=3.0,
    )
    connector = GoldCTraderConnector(settings)
    connector._connected = True
    connector._symbols_by_name = {
        "EURUSD": {"symbolId": 1, "symbolName": "EURUSD"},
        "XAUUSD.A": {"symbolId": 2, "symbolName": "XAUUSD.A"},
        "GBPUSD": {"symbolId": 3, "symbolName": "GBPUSD"},
    }

    assert connector.resolve_symbol("GOLD") == "XAUUSD.A"


def test_monitor_reuses_local_position_metadata_by_comment(tmp_path: Path) -> None:
    settings = SimpleNamespace(
        strategy_names=["trend_following"],
        trade_comment_prefix="gold-bot",
        trade_magic_number=550015,
        ladder_entries=2,
        enable_multi_entry=True,
        max_daily_trades=10,
        enable_trading=True,
        fixed_lot_size=0.01,
        stop_loss_pips=100.0,
        take_profit_pips=140.0,
        ladder_step_ratio=1.0,
        pip_size=0.01,
        lower_timeframe="M5",
        higher_timeframe="M30",
        poll_seconds=0.2,
        position_monitor_seconds=10.0,
        account_monitor_seconds=10.0,
        chart_update_seconds=10.0,
        plot_enabled=False,
        plot_ltf_candles=120,
        plot_htf_candles=90,
        candle_count=100,
        max_cycles=0,
        strategy_presets={},
        config_env_path="bot/.env",
        ema_fast=12,
        ema_slow=26,
        ema_trend_period=50,
    )
    runner = GoldRunner(settings)
    position_store = GoldPositionStore(tmp_path / "positions.sqlite3")

    class FakeConnector:
        def open_positions(self, symbol: str | None = None) -> list[dict[str, object]]:
            return [
                {
                    "ticket": 39820336,
                    "symbol": "XAUUSD",
                    "type": 0,
                    "volume": 1.0,
                    "price_open": 4060.98,
                    "sl": 4054.93,
                    "tp": 4096.93,
                    "profit": 9.78,
                    "comment": "gold-bot:trend_following:L1",
                }
            ]

    service = GoldLiveService(
        settings=settings,
        runner=runner,
        connector=FakeConnector(),
        position_store=position_store,
        chart_renderer=LiveChartRenderer(
            output_dir=tmp_path,
            interactive=False,
            chart_width=14.0,
            chart_height=8.0,
            max_lower_candles=120,
            max_higher_candles=90,
        ),
    )

    position_store.upsert_position(
        {
            "position_key": "ctrader:ticket-999:XAUUSD",
            "ticket": 999,
            "symbol": "XAUUSD",
            "direction": "buy",
            "volume": 0.2,
            "entry_price": 4063.42,
            "stop_loss": 4062.58,
            "take_profit": 4064.98,
            "strategy": "trend_following",
            "timeframes": "M5/M30",
            "comment": "gold-bot:trend_following:L1",
            "source": "ctrader",
            "is_external": 1,
            "status": "open",
            "opened_at": "2026-08-04T09:00:00",
        }
    )

    service._monitor_positions("XAUUSD")

    stored = position_store.list_positions(status="open")
    assert len(stored) == 1
    assert stored[0]["ticket"] == 39820336
    assert stored[0]["strategy"] == "trend_following"
    assert stored[0]["timeframes"] == "M5/M30"


def test_monitor_preserves_strategy_and_timeframes_for_open_positions(tmp_path: Path) -> None:
    settings = SimpleNamespace(
        strategy_names=["trend_following"],
        trade_comment_prefix="gold-bot",
        trade_magic_number=550015,
        ladder_entries=2,
        enable_multi_entry=True,
        max_daily_trades=10,
        enable_trading=True,
        fixed_lot_size=0.01,
        stop_loss_pips=100.0,
        take_profit_pips=140.0,
        lower_timeframe="M5",
        higher_timeframe="M30",
        poll_seconds=0.2,
        position_monitor_seconds=10.0,
        account_monitor_seconds=10.0,
        chart_update_seconds=10.0,
        plot_enabled=False,
        plot_ltf_candles=120,
        plot_htf_candles=90,
        candle_count=100,
        max_cycles=0,
        strategy_presets={},
        config_env_path="bot/.env",
        ema_fast=12,
        ema_slow=26,
        ema_trend_period=50,
    )
    runner = GoldRunner(settings)
    position_store = GoldPositionStore(tmp_path / "positions.sqlite3")

    class FakeConnector:
        def open_positions(self, symbol: str | None = None) -> list[dict[str, object]]:
            return [
                {
                    "ticket": 42,
                    "symbol": "XAUUSD",
                    "type": 0,
                    "volume": 0.2,
                    "price_open": 4063.42,
                    "sl": 4062.58,
                    "tp": 4064.98,
                    "profit": 0.15,
                }
            ]

    service = GoldLiveService(
        settings=settings,
        runner=runner,
        connector=FakeConnector(),
        position_store=position_store,
        chart_renderer=LiveChartRenderer(
            output_dir=tmp_path,
            interactive=False,
            chart_width=14.0,
            chart_height=8.0,
            max_lower_candles=120,
            max_higher_candles=90,
        ),
    )

    position_store.upsert_position(
        {
            "position_key": "ctrader:ticket-42:XAUUSD",
            "ticket": 42,
            "symbol": "XAUUSD",
            "direction": "buy",
            "volume": 0.2,
            "entry_price": 4063.42,
            "stop_loss": 4062.58,
            "take_profit": 4064.98,
            "strategy": "trend_following",
            "timeframes": "M5/M30",
            "source": "ctrader",
            "is_external": 1,
            "status": "open",
            "opened_at": "2026-08-04T09:00:00",
        }
    )

    service._monitor_positions("XAUUSD")

    stored = position_store.list_positions(status="open")
    assert len(stored) == 1
    assert stored[0]["strategy"] == "trend_following"
    assert stored[0]["timeframes"] == "M5/M30"


def test_price_action_cooldown_blocks_repeated_signals(tmp_path: Path) -> None:
    settings = SimpleNamespace(
        strategy_names=["price_action"],
        trade_comment_prefix="gold-bot",
        trade_magic_number=550015,
        ladder_entries=1,
        enable_multi_entry=False,
        max_daily_trades=10,
        enable_trading=True,
        fixed_lot_size=0.01,
        stop_loss_pips=100.0,
        take_profit_pips=140.0,
        ladder_step_ratio=1.0,
        pip_size=0.01,
        lower_timeframe="M5",
        higher_timeframe="M30",
        poll_seconds=0.2,
        position_monitor_seconds=10.0,
        account_monitor_seconds=10.0,
        chart_update_seconds=10.0,
        plot_enabled=False,
        plot_ltf_candles=120,
        plot_htf_candles=90,
        candle_count=100,
        max_cycles=0,
        strategy_presets={},
        config_env_path="bot/.env",
        ema_fast=12,
        ema_slow=26,
        ema_trend_period=50,
        price_action_cooldown_minutes=60.0,
    )
    runner = GoldRunner(settings)
    position_store = GoldPositionStore(tmp_path / "positions.sqlite3")
    orders = []

    class FakeConnector:
        def open_positions(self, symbol: str | None = None) -> list[dict[str, object]]:
            return []

        def account_info(self) -> dict[str, object]:
            return {"balance": 1000.0, "equity": 1000.0}

        def symbol_info(self, symbol: str) -> dict[str, object]:
            return {"symbol": symbol}

        def current_price(self, symbol: str, direction: str) -> float:
            return 4000.0

        def place_market_order(self, **kwargs) -> dict[str, object]:
            orders.append(kwargs)
            return {"ok": True, "order": 1, "price": 4000.0, "filling": "filled"}

    service = GoldLiveService(
        settings=settings,
        runner=runner,
        connector=FakeConnector(),
        position_store=position_store,
        chart_renderer=LiveChartRenderer(
            output_dir=tmp_path,
            interactive=False,
            chart_width=14.0,
            chart_height=8.0,
            max_lower_candles=120,
            max_higher_candles=90,
        ),
    )

    candidate = SimpleNamespace(strategy="price_action", direction="buy", reason="breakout", price=4000.0)
    first_lower = pd.DataFrame({"datetime": [pd.Timestamp("2026-08-04T12:00:00")], "open": [4000.0], "high": [4001.0], "low": [3999.0], "close": [4000.0]})
    first_higher = pd.DataFrame({"datetime": [pd.Timestamp("2026-08-04T11:45:00")], "open": [4000.0], "high": [4001.0], "low": [3999.0], "close": [4000.0]})
    second_lower = pd.DataFrame({"datetime": [pd.Timestamp("2026-08-04T12:01:00")], "open": [4001.0], "high": [4002.0], "low": [4000.0], "close": [4001.0]})
    second_higher = pd.DataFrame({"datetime": [pd.Timestamp("2026-08-04T11:46:00")], "open": [4001.0], "high": [4002.0], "low": [4000.0], "close": [4001.0]})

    service._handle_candidate("XAUUSD", candidate, first_lower, first_higher, "M5", "M30", 100.0, 140.0)
    service._handle_candidate("XAUUSD", candidate, second_lower, second_higher, "M5", "M30", 100.0, 140.0)

    assert len(orders) == 1


def test_live_service_requires_ctrader_connection(tmp_path: Path) -> None:
    settings = load_gold_settings(_copy_example_env(tmp_path))
    runner = GoldRunner(settings)

    class FakeConnector:
        def connect(self) -> bool:
            return False

        def disconnect(self) -> None:
            return None

    service = GoldLiveService(
        settings=settings,
        runner=runner,
        connector=FakeConnector(),
        position_store=GoldPositionStore(tmp_path / "positions.sqlite3"),
        chart_renderer=LiveChartRenderer(
            output_dir=tmp_path,
            interactive=False,
            chart_width=14.0,
            chart_height=8.0,
            max_lower_candles=120,
            max_higher_candles=90,
        ),
    )

    assert service.run() == 1


def test_trailing_stop_updates_after_reaching_rr_trigger(tmp_path: Path) -> None:
    settings = SimpleNamespace(
        strategy_names=["ema_crossover"],
        trade_comment_prefix="gold-bot",
        trade_magic_number=550015,
        ladder_entries=1,
        enable_multi_entry=False,
        max_daily_trades=10,
        max_open_positions=2,
        enable_trading=True,
        fixed_lot_size=0.01,
        stop_loss_pips=100.0,
        take_profit_pips=200.0,
        risk_reward_ratio=2.0,
        ladder_step_ratio=1.0,
        pip_size=0.01,
        lower_timeframe="M1",
        higher_timeframe="M1",
        poll_seconds=0.2,
        position_monitor_seconds=10.0,
        account_monitor_seconds=10.0,
        chart_update_seconds=10.0,
        plot_enabled=False,
        plot_ltf_candles=120,
        plot_htf_candles=90,
        candle_count=100,
        max_cycles=0,
        strategy_presets={},
        config_env_path="bot/.env",
        ema_fast=50,
        ema_slow=200,
        ema_trend_period=200,
    )
    runner = GoldRunner(settings)
    position_store = GoldPositionStore(tmp_path / "positions.sqlite3")
    amendments: list[dict[str, object]] = []

    class FakeConnector:
        def current_price(self, symbol: str, direction: str) -> float:
            return 102.2

        def amend_position_protection(self, ticket: int, stop_loss: float | None, take_profit: float | None) -> dict[str, object]:
            amendments.append({"ticket": ticket, "stop_loss": stop_loss, "take_profit": take_profit})
            return {"ok": True}

    service = GoldLiveService(
        settings=settings,
        runner=runner,
        connector=FakeConnector(),
        position_store=position_store,
        chart_renderer=LiveChartRenderer(
            output_dir=tmp_path,
            interactive=False,
            chart_width=14.0,
            chart_height=8.0,
            max_lower_candles=120,
            max_higher_candles=90,
        ),
    )

    stored_key = "ctrader:ticket-7:XAUUSD"
    position_store.upsert_position(
        {
            "position_key": stored_key,
            "ticket": 7,
            "symbol": "XAUUSD",
            "direction": "buy",
            "volume": 0.1,
            "entry_price": 100.0,
            "stop_loss": 99.0,
            "take_profit": 104.0,
            "strategy": "ema_crossover",
            "timeframes": "M1",
            "comment": "gold-bot:ema_crossover:M1-M1-P1:L1",
            "source": "ctrader",
            "is_external": 1,
            "status": "open",
            "opened_at": "2026-08-08T00:00:00",
        }
    )

    live_positions = [
        {
            "ticket": 7,
            "symbol": "XAUUSD",
            "type": 0,
            "volume": 0.1,
            "price_open": 100.0,
            "sl": 99.0,
            "tp": 104.0,
            "profit": 0.0,
            "comment": "gold-bot:ema_crossover:M1-M1-P1:L1",
        }
    ]

    service._apply_trailing_stops(live_positions, {stored_key: position_store.list_positions(status="open")[0]})

    assert len(amendments) == 1
    assert amendments[0]["ticket"] == 7
    assert float(amendments[0]["stop_loss"]) == 101.2
