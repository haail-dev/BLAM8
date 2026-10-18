from datetime import datetime, timedelta, timezone


def approved_run():
    return {"run_id": "test", "state": "running", "days": 14, "limit": 100,
            "window_end": "2026-10-18T20:00:00+00:00",
            "expires_at": (datetime.now(timezone.utc)+timedelta(minutes=15)).isoformat(),
            "approval": {"decision": "import", "version": 1}}
