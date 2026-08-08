from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts.healthcheck_live import check_status_file


def _write_payload(path: Path, *, status: str, updated_at: datetime) -> None:
    path.write_text(
        json.dumps(
            {
                "status": status,
                "updated_at_utc": updated_at.isoformat(),
            }
        ),
        encoding="utf-8",
    )


def test_healthcheck_ok_for_fresh_running_status(tmp_path: Path) -> None:
    status_file = tmp_path / "live_heartbeat.json"
    _write_payload(status_file, status="running", updated_at=datetime.now(timezone.utc))

    assert check_status_file(status_file, max_age_seconds=120.0) == 0


def test_healthcheck_fails_for_stale_heartbeat(tmp_path: Path) -> None:
    status_file = tmp_path / "live_heartbeat.json"
    _write_payload(
        status_file,
        status="running",
        updated_at=datetime.now(timezone.utc) - timedelta(seconds=500),
    )

    assert check_status_file(status_file, max_age_seconds=120.0) == 1


def test_healthcheck_fails_for_unexpected_status(tmp_path: Path) -> None:
    status_file = tmp_path / "live_heartbeat.json"
    _write_payload(status_file, status="stopped", updated_at=datetime.now(timezone.utc))

    assert check_status_file(status_file, max_age_seconds=120.0) == 1
