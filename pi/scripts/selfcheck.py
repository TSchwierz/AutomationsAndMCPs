#!/usr/bin/env python3
"""Minimal self-check without DHT11 hardware (uses mock sensor config)."""

from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from studio_climate.advisor import AdvisorStore
from studio_climate.alerts import evaluate_alerts
from studio_climate.briefing import build_briefing, write_briefing
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


def _check_briefing_and_anomaly(cfg) -> None:
    now = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
    db_path = ROOT / "data" / "selfcheck-advisor.db"
    advisor_root = ROOT / "data" / "selfcheck-advisor-files"
    if db_path.exists():
        db_path.unlink()
    if advisor_root.exists():
        for child in advisor_root.rglob("*"):
            if child.is_file():
                child.unlink()
    db = ClimateDB(db_path)
    db.init_schema(default_settings(cfg))
    store = AdvisorStore(advisor_root)
    store.ensure()

    for i in range(12):
        db.insert_measurement(21.0, 48.0, ts=now + timedelta(hours=i))
    payload = build_briefing(db, week="2026-W38", now=now)
    assert payload["week"] == "2026-W38"
    assert payload["indoor"]["samples"] >= 1
    path = write_briefing(db, store, week="2026-W38", now=now)
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["week"] == "2026-W38"

    window = 8
    last = None
    for i in range(window):
        last = db.insert_measurement(21.0, 40.0, ts=now + timedelta(days=1, minutes=i))
    for i in range(window):
        last = db.insert_measurement(21.0, 58.0, ts=now + timedelta(days=1, minutes=window + i))
    assert last is not None
    evaluate_incidents(db, last, advisor=store)
    open_inc = db.get_open_incident()
    assert open_inc is not None
    assert not open_inc.tags
    for i in range(window):
        last = db.insert_measurement(21.0, 58.0, ts=now + timedelta(days=1, minutes=2 * window + i))
        evaluate_incidents(db, last, advisor=store)
    closed = db.get_incident(open_inc.id)
    assert closed is not None and closed.ended_at is not None
    notes = store.read_markdown("anomalies.md")
    assert f"incident-{closed.id}" in notes

    db_path.unlink(missing_ok=True)
    for suffix in ("-wal", "-shm"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)
    shutil.rmtree(advisor_root, ignore_errors=True)


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
    _check_briefing_and_anomaly(cfg)
    print("selfcheck ok", m.temperature_c, m.humidity_pct, status["overall"])
    db_path.unlink(missing_ok=True)
    for suffix in ("-wal", "-shm"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
