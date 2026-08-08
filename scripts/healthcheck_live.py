from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def _parse_timestamp(value: str) -> datetime:
    text = str(value or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def check_status_file(status_file: Path, max_age_seconds: float) -> int:
    if not status_file.exists():
        print(f"healthcheck failed: missing status file {status_file}")
        return 1

    try:
        payload = json.loads(status_file.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"healthcheck failed: unreadable status file {status_file}: {exc}")
        return 1

    status = str(payload.get("status", "")).strip().lower()
    if status not in {"starting", "running", "stopping"}:
        print(f"healthcheck failed: unexpected status '{status}'")
        return 1

    updated_raw = payload.get("updated_at_utc")
    if not updated_raw:
        print("healthcheck failed: missing updated_at_utc")
        return 1

    try:
        updated_at = _parse_timestamp(str(updated_raw))
    except Exception as exc:
        print(f"healthcheck failed: invalid updated_at_utc '{updated_raw}': {exc}")
        return 1

    age_seconds = (datetime.now(timezone.utc) - updated_at).total_seconds()
    if age_seconds > max_age_seconds:
        print(
            "healthcheck failed: stale heartbeat "
            f"age={age_seconds:.1f}s max_age={max_age_seconds:.1f}s status={status}"
        )
        return 1

    print(f"healthcheck ok: status={status} age={age_seconds:.1f}s")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Live bot heartbeat healthcheck")
    parser.add_argument("--status-file", default="logs/live_heartbeat.json")
    parser.add_argument("--max-age-seconds", type=float, default=120.0)
    args = parser.parse_args()

    return check_status_file(Path(args.status_file), max(1.0, float(args.max_age_seconds)))


if __name__ == "__main__":
    raise SystemExit(main())
