from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


class GoldPositionStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _initialize(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS positions (
                    position_key TEXT PRIMARY KEY,
                    ticket INTEGER,
                    symbol TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    volume REAL NOT NULL,
                    entry_price REAL NOT NULL,
                    stop_loss REAL,
                    take_profit REAL,
                    strategy TEXT,
                    timeframes TEXT,
                    source TEXT,
                    is_external INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'open',
                    opened_at TEXT,
                    closed_at TEXT,
                    close_price REAL
                )
                """
            )
            conn.commit()

    def upsert_position(self, payload: dict[str, Any]) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO positions (
                    position_key, ticket, symbol, direction, volume, entry_price, stop_loss,
                    take_profit, strategy, timeframes, source, is_external, status, opened_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(position_key) DO UPDATE SET
                    ticket=excluded.ticket,
                    symbol=excluded.symbol,
                    direction=excluded.direction,
                    volume=excluded.volume,
                    entry_price=excluded.entry_price,
                    stop_loss=excluded.stop_loss,
                    take_profit=excluded.take_profit,
                    strategy=excluded.strategy,
                    timeframes=excluded.timeframes,
                    source=excluded.source,
                    is_external=excluded.is_external,
                    status=excluded.status,
                    opened_at=excluded.opened_at
                """,
                (
                    payload["position_key"],
                    payload.get("ticket"),
                    payload.get("symbol"),
                    payload.get("direction"),
                    payload.get("volume"),
                    payload.get("entry_price"),
                    payload.get("stop_loss"),
                    payload.get("take_profit"),
                    payload.get("strategy"),
                    payload.get("timeframes"),
                    payload.get("source"),
                    payload.get("is_external", 0),
                    payload.get("status", "open"),
                    payload.get("opened_at"),
                ),
            )
            conn.commit()

    def list_positions(self, status: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM positions"
        params: tuple[Any, ...] = ()
        if status is not None:
            query += " WHERE status = ?"
            params = (status,)
        query += " ORDER BY opened_at"
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def mark_closed(self, position_key: str, close_price: float, closed_at: str) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE positions SET status = 'closed', close_price = ?, closed_at = ? WHERE position_key = ?",
                (close_price, closed_at, position_key),
            )
            conn.commit()
