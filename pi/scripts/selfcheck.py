#!/usr/bin/env python3
"""Minimal self-check without DHT11 hardware (uses mock sensor config)."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from studio_climate.alerts import evaluate_alerts
from studio_climate.collector import default_settings
from studio_climate.config import load_config
from studio_climate.db import ClimateDB
from studio_climate.incidents import evaluate_incidents, rolling_snapshot
from studio_climate.sensor import MockSensor, band_status


def _check_incidents(db: ClimateDB) -> None:
    now = datetime.now(timezone.utc)
    window = 8
    last = None
    for i in range(window):
        last = db.insert_measurement(21.0, 45.0, ts=now + timedelta(minutes=i))
    snapshot = rolling_snapshot(db.recent_measurements(window), window)
    assert snapshot is not None
    assert abs(snapshot["humidity_pct"] - 45.0) < 0.01

    for i in range(window):
        last = db.insert_measurement(21.0, 62.0, ts=now + timedelta(minutes=window + i))
    assert last is not None
    opened = evaluate_incidents(db, last)
    assert opened, "humidity jump should open a climate-shift incident"
    incident = db.get_open_incident()
    assert incident is not None
    assert incident.metric in {"humidity", "both"}
    assert incident.delta_humidity_pct >= 6
    tagged = db.set_incident_tags(incident.id, ["Shower"])
    assert tagged is not None and tagged.tags == ["Shower"]

    for i in range(window):
        last = db.insert_measurement(21.0, 62.0, ts=now + timedelta(minutes=2 * window + i))
        evaluate_incidents(db, last)
    closed = db.get_incident(incident.id)
    assert closed is not None and closed.ended_at is not None
    assert closed.tags == ["Shower"]


def main() -> int:
    cfg = load_config(ROOT / "config.example.toml")
    assert cfg.alert_cooldown_minutes == 360
    db_path = ROOT / "data" / "selfcheck.db"
    if db_path.exists():
        db_path.unlink()
    db = ClimateDB(db_path)
    db.init_schema(default_settings(cfg))
    settings = db.get_settings()
    assert settings["alert_cooldown_minutes"] == "360"
    reading = MockSensor().read()
    m = db.insert_measurement(reading.temperature_c, reading.humidity_pct)
    evaluate_alerts(db, m)
    status = band_status(
        m.temperature_c,
        m.humidity_pct,
        cfg.temp_min,
        cfg.temp_max,
        cfg.humidity_min,
        cfg.humidity_max,
    )
    assert db.latest_measurement() is not None
    assert status["overall"] in {"in_band", "out_of_band"}
    _check_incidents(db)
    print("selfcheck ok", m.temperature_c, m.humidity_pct, status["overall"])
    db_path.unlink(missing_ok=True)
    for suffix in ("-wal", "-shm"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
