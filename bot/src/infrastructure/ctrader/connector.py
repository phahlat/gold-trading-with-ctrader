from __future__ import annotations

import logging
import math
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from ctrader_open_api import Client, EndPoints, Protobuf, TcpProtocol
from ctrader_open_api.messages.OpenApiMessages_pb2 import (
    ProtoOAAccountAuthReq,
    ProtoOAApplicationAuthReq,
    ProtoOADealListReq,
    ProtoOAExecutionEvent,
    ProtoOAGetAccountListByAccessTokenReq,
    ProtoOAGetPositionUnrealizedPnLReq,
    ProtoOAGetTrendbarsReq,
    ProtoOANewOrderReq,
    ProtoOAReconcileReq,
    ProtoOASpotEvent,
    ProtoOASubscribeSpotsReq,
    ProtoOASymbolByIdReq,
    ProtoOASymbolsListReq,
    ProtoOATraderReq,
    ProtoOAUnsubscribeSpotsReq,
)
from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
    ProtoOAExecutionType,
    ProtoOAOrderType,
    ProtoOATradeSide,
    ProtoOATrendbarPeriod,
)
from twisted.internet import reactor

logger = logging.getLogger(__name__)

_REACTOR_LOCK = threading.Lock()
_REACTOR_THREAD: threading.Thread | None = None

_CTRADER_JSON_PORT = 5036

_TIMEFRAME_TO_PERIOD = {
    "M1": "M1",
    "M5": "M5",
    "M15": "M15",
    "M30": "M30",
    "H1": "H1",
    "H4": "H4",
    "D1": "D1",
}

_TIMEFRAME_MINUTES = {
    "M1": 1,
    "M5": 5,
    "M15": 15,
    "M30": 30,
    "H1": 60,
    "H4": 240,
    "D1": 1440,
}

_SYMBOL_ALIASES = {
    "GOLD": ["XAUUSD", "XAUUSD.", "XAUUSDM", "XAUUSDMICRO", "XAUUSDPRO"],
    "XAUUSD": ["XAUUSD", "GOLD", "XAUUSD.", "XAUUSDM", "XAUUSDMICRO", "XAUUSDPRO"],
}


class GoldCTraderConnector:
    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self._client: Client | None = None
        self._connected = False
        self._account_id: int | None = None
        self._lock = threading.Lock()

        self._spot_by_symbol_id: dict[int, dict[str, float | int]] = {}
        self._subscribed_symbol_ids: set[int] = set()
        self._symbols_by_name: dict[str, dict[str, Any]] = {}
        self._symbols_by_id: dict[int, dict[str, Any]] = {}
        self._symbol_meta_by_id: dict[int, dict[str, Any]] = {}
        self._socket_connected_event = threading.Event()
        self._active_host_name = "live"
        self._active_host = EndPoints.PROTOBUF_LIVE_HOST
        self._active_port = EndPoints.PROTOBUF_PORT

    def connect(self) -> bool:
        if self._connected:
            return True

        if not self._credentials_complete():
            logger.warning("cTrader credentials are incomplete; skipping live connection")
            return False

        host_name = str(getattr(self.settings, "ctrader_host", "live")).strip().lower()
        host = EndPoints.PROTOBUF_LIVE_HOST if host_name == "live" else EndPoints.PROTOBUF_DEMO_HOST
        self._active_host_name = host_name
        self._active_host = host
        self._active_port = EndPoints.PROTOBUF_PORT

        logger.info(
            "cTrader Open API endpoints | protobuf_live=%s:%s protobuf_demo=%s:%s json_live=%s:%s json_demo=%s:%s",
            EndPoints.PROTOBUF_LIVE_HOST,
            EndPoints.PROTOBUF_PORT,
            EndPoints.PROTOBUF_DEMO_HOST,
            EndPoints.PROTOBUF_PORT,
            EndPoints.PROTOBUF_LIVE_HOST,
            _CTRADER_JSON_PORT,
            EndPoints.PROTOBUF_DEMO_HOST,
            _CTRADER_JSON_PORT,
        )
        logger.info(
            "cTrader connect target | mode=%s transport=protobuf endpoint=%s:%s",
            host_name,
            host,
            EndPoints.PROTOBUF_PORT,
        )

        try:
            _ensure_reactor_running()
            self._socket_connected_event.clear()
            self._client = Client(host, EndPoints.PROTOBUF_PORT, TcpProtocol)
            self._client.setConnectedCallback(self._on_socket_connected)
            self._client.setDisconnectedCallback(self._on_socket_disconnected)
            self._client.setMessageReceivedCallback(self._on_message_received)
            self._call_in_reactor(self._client.startService, timeout=max(2.0, float(getattr(self.settings, "ctrader_connect_timeout_seconds", 15.0))))
            self._wait_for_socket_ready(float(getattr(self.settings, "ctrader_connect_timeout_seconds", 15.0)))
            self._authenticate_application_and_account()
            self._prime_symbol_catalog()
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.exception("cTrader connection failed: %s", exc)
            self.disconnect()
            return False

        self._connected = True
        logger.info(
            "cTrader connected | mode=%s endpoint=%s:%s account_id=%s",
            host_name,
            host,
            EndPoints.PROTOBUF_PORT,
            self._account_id,
        )
        return True

    def disconnect(self) -> None:
        if self._client and self._account_id is not None and self._subscribed_symbol_ids:
            try:
                req = ProtoOAUnsubscribeSpotsReq()
                req.ctidTraderAccountId = int(self._account_id)
                for symbol_id in sorted(self._subscribed_symbol_ids):
                    req.symbolId.append(int(symbol_id))
                self._send_and_extract(req, timeout=5.0)
            except Exception:
                pass

        if self._client:
            try:
                self._call_in_reactor(self._client.stopService, timeout=5.0)
            except Exception:
                pass

        self._client = None
        self._connected = False
        self._account_id = None
        self._subscribed_symbol_ids.clear()
        self._spot_by_symbol_id.clear()
        self._socket_connected_event.clear()

    def account_info(self) -> dict[str, Any] | None:
        if not self._connected or self._account_id is None:
            return None

        req = ProtoOATraderReq()
        req.ctidTraderAccountId = int(self._account_id)
        trader_res = self._send_and_extract(req)
        trader = getattr(trader_res, "trader", None)
        if trader is None:
            return None

        money_digits = int(getattr(trader, "moneyDigits", 2) or 2)
        balance = self._money_to_float(getattr(trader, "balance", 0), money_digits)
        leverage_cents = int(getattr(trader, "leverageInCents", 0) or 0)
        leverage = float(leverage_cents) / 100.0 if leverage_cents > 0 else 0.0

        unrealized = 0.0
        try:
            pnl_req = ProtoOAGetPositionUnrealizedPnLReq()
            pnl_req.ctidTraderAccountId = int(self._account_id)
            pnl_res = self._send_and_extract(pnl_req)
            pnl_digits = int(getattr(pnl_res, "moneyDigits", money_digits) or money_digits)
            for item in getattr(pnl_res, "positionUnrealizedPnL", []):
                unrealized += self._money_to_float(getattr(item, "netUnrealizedPnL", 0), pnl_digits)
        except Exception:
            unrealized = 0.0

        equity = balance + unrealized
        return {
            "login": int(self._account_id),
            "balance": balance,
            "equity": equity,
            "margin": 0.0,
            "free_margin": equity,
            "leverage": leverage,
            "currency": "",
        }

    def broker_symbols(self) -> list[str]:
        if not self._connected:
            return []
        return sorted(self._symbols_by_name.keys())

    def resolve_symbol(self, requested_symbol: str) -> str | None:
        if not self._connected:
            return None

        requested = requested_symbol.strip().upper()
        available = self.broker_symbols()
        if not available:
            return None

        lookup = {name.upper(): name for name in available}
        if requested in lookup:
            return lookup[requested]

        candidates = [requested]
        candidates.extend(_SYMBOL_ALIASES.get(requested, []))
        for candidate in candidates:
            if candidate.upper() in lookup:
                return lookup[candidate.upper()]

        fuzzy = []
        normalized = [x.upper() for x in candidates]
        for name in available:
            upper_name = name.upper()
            if any(token in upper_name for token in normalized):
                fuzzy.append(name)
        if fuzzy:
            fuzzy.sort(key=len)
            return fuzzy[0]
        return None

    def symbol_info(self, symbol: str) -> dict[str, Any] | None:
        if not self._connected or self._account_id is None:
            return None

        symbol_item = self._symbols_by_name.get(symbol)
        if not symbol_item:
            return None

        symbol_id = int(symbol_item["symbolId"])
        if symbol_id not in self._symbol_meta_by_id:
            req = ProtoOASymbolByIdReq()
            req.ctidTraderAccountId = int(self._account_id)
            req.symbolId.append(symbol_id)
            response = self._send_and_extract(req)
            for item in self._iter_proto_items(getattr(response, "symbol", None)):
                self._symbol_meta_by_id[int(item.symbolId)] = self._normalize_symbol_meta(item)

        meta = self._symbol_meta_by_id.get(symbol_id)
        if not meta:
            return None

        output = dict(meta)
        output["symbol"] = symbol
        return output

    def current_price(self, symbol: str, direction: str) -> float | None:
        if not self._connected or self._account_id is None:
            return None

        symbol_item = self._symbols_by_name.get(symbol)
        if not symbol_item:
            return None

        symbol_id = int(symbol_item["symbolId"])
        self._ensure_spot_subscription(symbol_id)

        tick = self._spot_by_symbol_id.get(symbol_id)
        if not tick:
            return None

        bid = float(tick.get("bid", 0.0) or 0.0)
        ask = float(tick.get("ask", 0.0) or 0.0)
        if bid <= 0 or ask <= 0:
            return None

        if direction.lower() == "buy":
            return ask
        return bid

    def open_positions(self, symbol: str | None = None) -> list[dict[str, Any]]:
        if not self._connected or self._account_id is None:
            return []

        req = ProtoOAReconcileReq()
        req.ctidTraderAccountId = int(self._account_id)
        response = self._send_and_extract(req)

        unrealized_map: dict[int, float] = {}
        try:
            pnl_req = ProtoOAGetPositionUnrealizedPnLReq()
            pnl_req.ctidTraderAccountId = int(self._account_id)
            pnl_res = self._send_and_extract(pnl_req)
            pnl_digits = int(getattr(pnl_res, "moneyDigits", 2) or 2)
            for item in self._iter_proto_items(getattr(pnl_res, "positionUnrealizedPnL", None)):
                position_id = int(getattr(item, "positionId", 0) or 0)
                unrealized = self._money_to_float(getattr(item, "netUnrealizedPnL", 0), pnl_digits)
                if position_id > 0:
                    unrealized_map[position_id] = float(unrealized)
        except Exception as exc:
            logger.debug("Unable to fetch unrealized PnL map: %s", exc)

        positions: list[dict[str, Any]] = []
        for item in self._iter_proto_items(getattr(response, "position", None)):
            trade_data = getattr(item, "tradeData", None)
            if trade_data is None:
                continue
            symbol_id = int(getattr(trade_data, "symbolId", 0))
            symbol_name = self._symbol_name_from_id(symbol_id)
            if symbol and symbol_name.upper() != symbol.upper():
                continue

            side = int(getattr(trade_data, "tradeSide", 0))
            direction = 0 if side == ProtoOATradeSide.Value("BUY") else 1
            volume = float(getattr(trade_data, "volume", 0.0)) / 100.0
            opened_ms = int(getattr(trade_data, "openTimestamp", 0) or 0)
            position_id = int(getattr(item, "positionId", 0) or 0)

            positions.append(
                {
                    "ticket": position_id,
                    "symbol": symbol_name,
                    "type": direction,
                    "volume": volume,
                    "price_open": float(getattr(item, "price", 0.0) or 0.0),
                    "sl": float(getattr(item, "stopLoss", 0.0) or 0.0),
                    "tp": float(getattr(item, "takeProfit", 0.0) or 0.0),
                    "profit": float(unrealized_map.get(position_id, 0.0)),
                    "time": int(opened_ms / 1000) if opened_ms > 0 else 0,
                    "comment": str(getattr(trade_data, "comment", "") or ""),
                }
            )

        return positions

    def place_market_order(
        self,
        symbol: str,
        direction: str,
        volume: float,
        stop_loss: float,
        take_profit: float,
        magic_number: int,
        comment: str,
    ) -> dict[str, Any]:
        if not self._connected or self._account_id is None:
            return {"ok": False, "reason": "not_connected"}

        symbol_item = self._symbols_by_name.get(symbol)
        if not symbol_item:
            return {"ok": False, "reason": "symbol_unavailable"}

        symbol_id = int(symbol_item["symbolId"])
        req = ProtoOANewOrderReq()
        req.ctidTraderAccountId = int(self._account_id)
        req.symbolId = symbol_id
        req.orderType = ProtoOAOrderType.Value("MARKET")
        req.tradeSide = ProtoOATradeSide.Value("BUY" if direction.lower() == "buy" else "SELL")
        req.volume = max(1, int(round(float(volume) * 100.0)))
        req.stopLoss = float(stop_loss)
        req.takeProfit = float(take_profit)
        req.comment = comment
        req.label = f"magic:{int(magic_number)}"

        result = self._send_and_extract(req)
        if not isinstance(result, ProtoOAExecutionEvent):
            return {"ok": False, "reason": "unexpected_response", "details": str(type(result))}

        if getattr(result, "errorCode", ""):
            return {"ok": False, "reason": "rejected", "details": str(result.errorCode)}

        execution_type = int(getattr(result, "executionType", 0))
        rejected = execution_type in {
            ProtoOAExecutionType.Value("ORDER_REJECTED"),
            ProtoOAExecutionType.Value("ORDER_CANCEL_REJECTED"),
        }
        if rejected:
            return {
                "ok": False,
                "reason": "rejected",
                "retcode": execution_type,
                "details": str(execution_type),
                "filling": "market",
            }

        order = getattr(result, "order", None)
        deal = getattr(result, "deal", None)
        order_id = int(getattr(order, "orderId", 0) or 0)
        deal_id = int(getattr(deal, "dealId", 0) or 0)

        execution_price = float(getattr(deal, "executionPrice", 0.0) or 0.0)
        if execution_price <= 0 and order is not None:
            execution_price = float(getattr(order, "executionPrice", 0.0) or 0.0)
        if execution_price <= 0:
            current = self.current_price(symbol, direction)
            execution_price = float(current or 0.0)

        return {
            "ok": True,
            "retcode": execution_type,
            "order": order_id,
            "deal": deal_id,
            "price": execution_price,
            "volume": float(volume),
            "filling": "market",
        }

    def session_trade_performance(
        self,
        started_at: datetime,
        ended_at: datetime | None = None,
        symbol: str | None = None,
        magic_number: int | None = None,
        comment_prefix: str | None = None,
    ) -> dict[str, Any]:
        if not self._connected or self._account_id is None:
            return {"closed_trades": 0, "wins": 0, "losses": 0, "breakeven": 0, "net_profit": 0.0}

        req = ProtoOADealListReq()
        req.ctidTraderAccountId = int(self._account_id)
        req.fromTimestamp = int(started_at.replace(tzinfo=timezone.utc).timestamp() * 1000)
        req.toTimestamp = int((ended_at or datetime.utcnow()).replace(tzinfo=timezone.utc).timestamp() * 1000)
        req.maxRows = 5000
        response = self._send_and_extract(req)

        closed_trades = 0
        wins = 0
        losses = 0
        breakeven = 0
        net_profit = 0.0

        for deal in getattr(response, "deal", []):
            close_detail = getattr(deal, "closePositionDetail", None)
            if close_detail is None or not close_detail.ListFields():
                continue

            symbol_name = self._symbol_name_from_id(int(getattr(deal, "symbolId", 0)))
            if symbol and symbol_name.upper() != symbol.upper():
                continue

            # cTrader deal model does not expose original comment/magic directly.
            _ = magic_number
            _ = comment_prefix

            money_digits = int(getattr(deal, "moneyDigits", 2) or 2)
            gross = self._money_to_float(getattr(close_detail, "grossProfit", 0), money_digits)
            swap = self._money_to_float(getattr(close_detail, "swap", 0), money_digits)
            commission = self._money_to_float(getattr(close_detail, "commission", 0), money_digits)
            pnl_fee = self._money_to_float(getattr(close_detail, "pnlConversionFee", 0), money_digits)
            total = gross + swap + commission + pnl_fee

            closed_trades += 1
            net_profit += total
            if total > 0:
                wins += 1
            elif total < 0:
                losses += 1
            else:
                breakeven += 1

        return {
            "closed_trades": closed_trades,
            "wins": wins,
            "losses": losses,
            "breakeven": breakeven,
            "net_profit": net_profit,
        }

    def get_symbol_ticks(self, symbol: str) -> list[dict[str, Any]]:
        if not self._connected:
            return []

        symbol_item = self._symbols_by_name.get(symbol)
        if not symbol_item:
            return []
        symbol_id = int(symbol_item["symbolId"])
        tick = self._spot_by_symbol_id.get(symbol_id)
        if not tick:
            return []

        bid = float(tick.get("bid", 0.0) or 0.0)
        ask = float(tick.get("ask", 0.0) or 0.0)
        if bid <= 0 or ask <= 0:
            return []

        return [
            {
                "time": int(tick.get("time", 0)),
                "bid": bid,
                "ask": ask,
                "last": float(tick.get("last", tick.get("bid", 0.0))),
            }
        ]

    def get_rates(self, symbol: str, timeframe: str, count: int = 200) -> list[dict[str, Any]]:
        if not self._connected or self._account_id is None:
            return []

        symbol_item = self._symbols_by_name.get(symbol)
        if not symbol_item:
            return []
        symbol_id = int(symbol_item["symbolId"])

        tf_key = timeframe.strip().upper()
        period_name = _TIMEFRAME_TO_PERIOD.get(tf_key, "M15")
        period = ProtoOATrendbarPeriod.Value(period_name)
        step_minutes = _TIMEFRAME_MINUTES.get(tf_key, 15)

        now_utc = datetime.now(timezone.utc)
        window_minutes = max(10, int(count) * step_minutes + step_minutes)
        from_ts = int((now_utc - timedelta(minutes=window_minutes)).timestamp() * 1000)
        to_ts = int(now_utc.timestamp() * 1000)

        req = ProtoOAGetTrendbarsReq()
        req.ctidTraderAccountId = int(self._account_id)
        req.fromTimestamp = from_ts
        req.toTimestamp = to_ts
        req.period = period
        req.symbolId = symbol_id
        req.count = max(1, int(count))

        response = self._send_and_extract(req)
        bars: list[dict[str, Any]] = []
        for item in getattr(response, "trendbar", []):
            low = float(getattr(item, "low", 0.0)) / 100000.0
            open_price = (float(getattr(item, "low", 0.0)) + float(getattr(item, "deltaOpen", 0.0))) / 100000.0
            close_price = (float(getattr(item, "low", 0.0)) + float(getattr(item, "deltaClose", 0.0))) / 100000.0
            high_price = (float(getattr(item, "low", 0.0)) + float(getattr(item, "deltaHigh", 0.0))) / 100000.0
            timestamp = int(getattr(item, "utcTimestampInMinutes", 0) or 0) * 60
            bars.append(
                {
                    "time": timestamp,
                    "symbol": symbol,
                    "open": open_price,
                    "high": high_price,
                    "low": low,
                    "close": close_price,
                    "tick_volume": int(getattr(item, "volume", 0) or 0),
                }
            )

        bars.sort(key=lambda x: int(x["time"]))
        if len(bars) > count:
            bars = bars[-count:]
        return bars

    def _credentials_complete(self) -> bool:
        return bool(
            str(getattr(self.settings, "ctrader_client_id", "")).strip()
            and str(getattr(self.settings, "ctrader_client_secret", "")).strip()
            and str(getattr(self.settings, "ctrader_access_token", "")).strip()
        )

    def _wait_for_socket_ready(self, timeout: float) -> None:
        if not self._socket_connected_event.wait(max(1.0, timeout)):
            raise TimeoutError("Timed out waiting for cTrader socket connection")

    def _authenticate_application_and_account(self) -> None:
        assert self._client is not None

        app_req = ProtoOAApplicationAuthReq()
        app_req.clientId = str(getattr(self.settings, "ctrader_client_id", ""))
        app_req.clientSecret = str(getattr(self.settings, "ctrader_client_secret", ""))
        self._send_and_extract(app_req)

        access_token = str(getattr(self.settings, "ctrader_access_token", ""))
        accounts_req = ProtoOAGetAccountListByAccessTokenReq()
        accounts_req.accessToken = access_token
        accounts_res = self._send_and_extract(accounts_req)

        host_name = str(getattr(self.settings, "ctrader_host", "live")).strip().lower()
        use_live = host_name == "live"
        requested_account_id = int(getattr(self.settings, "ctrader_account_id", 0) or 0)
        if requested_account_id <= 0:
            host_specific = "ctrader_live_account_id" if use_live else "ctrader_demo_account_id"
            requested_account_id = int(getattr(self.settings, host_specific, 0) or 0)

        selected_account_id: int | None = None
        accounts = list(getattr(accounts_res, "ctidTraderAccount", []))
        for account in accounts:
            account_id = int(getattr(account, "ctidTraderAccountId", 0) or 0)
            if requested_account_id > 0 and account_id == requested_account_id:
                selected_account_id = account_id
                break

        if selected_account_id is None:
            for account in accounts:
                account_id = int(getattr(account, "ctidTraderAccountId", 0) or 0)
                is_live = bool(getattr(account, "isLive", False))
                if account_id > 0 and is_live == use_live:
                    selected_account_id = account_id
                    break

        if selected_account_id is None and accounts:
            selected_account_id = int(getattr(accounts[0], "ctidTraderAccountId", 0) or 0)

        if not selected_account_id:
            raise RuntimeError("No cTrader account found for provided access token")

        auth_req = ProtoOAAccountAuthReq()
        auth_req.ctidTraderAccountId = int(selected_account_id)
        auth_req.accessToken = access_token
        self._send_and_extract(auth_req)

        self._account_id = int(selected_account_id)

    def _prime_symbol_catalog(self) -> None:
        assert self._account_id is not None
        req = ProtoOASymbolsListReq()
        req.ctidTraderAccountId = int(self._account_id)
        req.includeArchivedSymbols = False
        response = self._send_and_extract(req)

        self._symbols_by_name.clear()
        self._symbols_by_id.clear()
        for item in self._iter_proto_items(getattr(response, "symbol", None)):
            symbol_name = str(getattr(item, "symbolName", "") or "").strip().upper()
            symbol_id = int(getattr(item, "symbolId", 0) or 0)
            if not symbol_name or symbol_id <= 0:
                continue
            value = {"symbolId": symbol_id, "symbolName": symbol_name}
            self._symbols_by_name[symbol_name] = value
            self._symbols_by_id[symbol_id] = value

    def _ensure_spot_subscription(self, symbol_id: int) -> None:
        if symbol_id in self._subscribed_symbol_ids:
            return
        assert self._account_id is not None

        req = ProtoOASubscribeSpotsReq()
        req.ctidTraderAccountId = int(self._account_id)
        req.symbolId.append(int(symbol_id))
        req.subscribeToSpotTimestamp = True
        self._send_and_extract(req)
        self._subscribed_symbol_ids.add(symbol_id)

    def _normalize_symbol_meta(self, item: Any) -> dict[str, Any]:
        digits = int(getattr(item, "digits", 2) or 2)
        point = 10 ** (-digits)

        min_volume = float(getattr(item, "minVolume", 0) or 0) / 100.0
        max_volume = float(getattr(item, "maxVolume", 0) or 0) / 100.0
        step_volume = float(getattr(item, "stepVolume", 0) or 0) / 100.0

        lot_size = float(getattr(item, "lotSize", 100) or 100.0)
        return {
            "digits": digits,
            "point": point,
            "trade_tick_size": point,
            "trade_tick_value": 0.0,
            "trade_contract_size": lot_size,
            "margin_initial": 0.0,
            "margin_maintenance": 0.0,
            "volume_min": max(0.01, min_volume if min_volume > 0 else 0.01),
            "volume_max": max(0.01, max_volume if max_volume > 0 else 50.0),
            "volume_step": max(0.01, step_volume if step_volume > 0 else 0.01),
            "filling_mode": 0,
            "trade_exemode": 0,
        }

    def _symbol_name_from_id(self, symbol_id: int) -> str:
        value = self._symbols_by_id.get(int(symbol_id))
        if not value:
            return str(symbol_id)
        return str(value.get("symbolName", symbol_id))

    def _money_to_float(self, value: int | float, digits: int) -> float:
        scale = float(10 ** max(0, int(digits)))
        return float(value) / scale if scale > 0 else float(value)

    def _iter_proto_items(self, value: Any) -> list[Any]:
        if value is None:
            return []
        if isinstance(value, list):
            return value
        if isinstance(value, tuple):
            return list(value)
        if isinstance(value, (str, bytes, dict)):
            return [value]
        if hasattr(value, "__iter__"):
            try:
                return list(value)
            except TypeError:
                return [value]
        return [value]

    def _on_message_received(self, _client: Client, message: Any) -> None:
        payload = Protobuf.extract(message)
        if isinstance(payload, ProtoOASpotEvent):
            symbol_id = int(getattr(payload, "symbolId", 0) or 0)
            bid = float(getattr(payload, "bid", 0.0) or 0.0) / 100000.0
            ask = float(getattr(payload, "ask", 0.0) or 0.0) / 100000.0
            if bid <= 0 or ask <= 0:
                logger.debug("Ignoring invalid spot quote | symbol_id=%s bid=%s ask=%s", symbol_id, bid, ask)
                return
            timestamp_ms = int(getattr(payload, "timestamp", 0) or 0)
            self._spot_by_symbol_id[symbol_id] = {
                "time": int(timestamp_ms / 1000) if timestamp_ms > 0 else int(time.time()),
                "bid": bid,
                "ask": ask,
                "last": (bid + ask) / 2.0 if bid > 0 and ask > 0 else max(bid, ask),
            }

    def _on_socket_connected(self, _client: Client) -> None:
        logger.info(
            "cTrader socket connected | mode=%s endpoint=%s:%s",
            self._active_host_name,
            self._active_host,
            self._active_port,
        )
        self._socket_connected_event.set()

    def _on_socket_disconnected(self, _client: Client, _reason: Any) -> None:
        logger.info(
            "cTrader socket disconnected | mode=%s endpoint=%s:%s",
            self._active_host_name,
            self._active_host,
            self._active_port,
        )
        self._socket_connected_event.clear()

    def _send_and_extract(self, request: Any, timeout: float | None = None) -> Any:
        if self._client is None:
            raise RuntimeError("cTrader client is not initialized")

        wait_timeout = float(timeout if timeout is not None else getattr(self.settings, "ctrader_request_timeout_seconds", 12.0))
        event = threading.Event()
        state: dict[str, Any] = {"value": None, "error": None}

        def _ok(result: Any) -> None:
            try:
                state["value"] = Protobuf.extract(result)
            except Exception:
                state["value"] = result
            finally:
                event.set()

        def _err(failure: Any) -> None:
            state["error"] = failure
            event.set()

        with self._lock:
            def _dispatch() -> None:
                try:
                    deferred = self._client.send(request, responseTimeoutInSeconds=max(1, int(math.ceil(wait_timeout))))
                    deferred.addCallbacks(_ok, _err)
                except Exception as exc:
                    _err(exc)

            reactor.callFromThread(_dispatch)

        if not event.wait(max(0.5, wait_timeout)):
            raise TimeoutError(f"Timed out waiting for cTrader response to {type(request).__name__}")

        if state["error"] is not None:
            raise RuntimeError(f"cTrader API call failed: {state['error']}")

        value = state["value"]
        if value is None:
            raise RuntimeError("cTrader API call returned empty response")
        return value

    def _call_in_reactor(self, fn: Any, timeout: float) -> Any:
        done = threading.Event()
        state: dict[str, Any] = {"value": None, "error": None}

        def _invoke() -> None:
            try:
                state["value"] = fn()
            except Exception as exc:
                state["error"] = exc
            finally:
                done.set()

        reactor.callFromThread(_invoke)
        if not done.wait(max(0.5, timeout)):
            raise TimeoutError("Timed out while dispatching operation to Twisted reactor")
        if state["error"] is not None:
            raise RuntimeError(state["error"])
        return state["value"]


def _run_reactor_forever() -> None:
    reactor.run(installSignalHandlers=False)


def _ensure_reactor_running() -> None:
    global _REACTOR_THREAD
    if reactor.running:
        return

    with _REACTOR_LOCK:
        if reactor.running:
            return
        if _REACTOR_THREAD is None or not _REACTOR_THREAD.is_alive():
            _REACTOR_THREAD = threading.Thread(target=_run_reactor_forever, name="ctrader-twisted-reactor", daemon=True)
            _REACTOR_THREAD.start()

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if reactor.running:
            return
        time.sleep(0.05)

    raise RuntimeError("Failed to start Twisted reactor")
