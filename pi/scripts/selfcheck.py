#!/usr/bin/env python3
"""Minimal self-check without DHT11 hardware (uses mock sensor config)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from studio_climate.alerts import evaluate_alerts
from studio_climate.collector import default_settings
from studio_climate.config import load_config
from studio_climate.db import ClimateDB
from studio_climate.sensor import MockSensor, band_status


def main() -> int:
    cfg = load_config(ROOT / "config.example.toml")
    db_path = ROOT / "data" / "selfcheck.db"
    if db_path.exists():
        db_path.unlink()
    db = ClimateDB(db_path)
    db.init_schema(default_settings(cfg))
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
    print("selfcheck ok", m.temperature_c, m.humidity_pct, status["overall"])
    db_path.unlink(missing_ok=True)
    for suffix in ("-wal", "-shm"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
