from __future__ import annotations

import logging
import signal
import threading
import time
from typing import Any

from .alerts import evaluate_alerts
from .config import AppConfig, load_config
from .db import ClimateDB
from .sensor import SensorError, create_sensor

logger = logging.getLogger(__name__)


def default_settings(cfg: AppConfig) -> dict[str, Any]:
    return {
        "humidity_min": cfg.humidity_min,
        "humidity_max": cfg.humidity_max,
        "temp_min": cfg.temp_min,
        "temp_max": cfg.temp_max,
        "sustain_minutes": cfg.sustain_minutes,
        "alert_cooldown_minutes": cfg.alert_cooldown_minutes,
        "ntfy_server": cfg.ntfy_server,
        "ntfy_topic": cfg.ntfy_topic,
        "ntfy_token": cfg.ntfy_token,
        "sample_interval_seconds": cfg.sample_interval_seconds,
    }


def run_collector(cfg: AppConfig | None = None) -> None:
    cfg = cfg or load_config()
    db = ClimateDB(cfg.db_path)
    db.init_schema(default_settings(cfg))

    sensor = create_sensor(mock=cfg.mock_sensor, gpio_pin=cfg.gpio_pin)
    stop = False

    def _handle_signal(signum, _frame) -> None:  # noqa: ANN001
        nonlocal stop
        logger.info("Received signal %s; stopping collector", signum)
        stop = True

    # signal.signal only works in the main thread (not when started via `all`).
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGINT, _handle_signal)
        signal.signal(signal.SIGTERM, _handle_signal)

    logger.info(
        "Collector started (interval=%ss, db=%s, mock=%s)",
        cfg.sample_interval_seconds,
        cfg.db_path,
        cfg.mock_sensor,
    )

    while not stop:
        loop_started = time.monotonic()
        try:
            settings = db.get_settings()
            try:
                interval = int(float(settings.get("sample_interval_seconds", cfg.sample_interval_seconds)))
            except (TypeError, ValueError):
                interval = cfg.sample_interval_seconds
            interval = max(2, interval)

            reading = sensor.read()
            measurement = db.insert_measurement(reading.temperature_c, reading.humidity_pct)
            logger.info(
                "Stored T=%.1f°C RH=%.1f%%",
                measurement.temperature_c,
                measurement.humidity_pct,
            )
            evaluate_alerts(db, measurement)
        except SensorError as exc:
            logger.warning("Sensor read failed: %s", exc)
        except Exception:  # noqa: BLE001
            logger.exception("Collector loop error")

        elapsed = time.monotonic() - loop_started
        sleep_for = max(0.0, interval - elapsed)
        # Sleep in short slices so SIGTERM is responsive.
        end = time.monotonic() + sleep_for
        while not stop and time.monotonic() < end:
            time.sleep(min(0.5, end - time.monotonic()))

    close = getattr(sensor, "close", None)
    if callable(close):
        close()
    logger.info("Collector stopped")
