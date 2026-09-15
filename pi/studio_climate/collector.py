from __future__ import annotations

import logging
import signal
import threading
import time
from typing import Any

from .advisor import AdvisorStore
from .alerts import evaluate_alerts
from .config import AppConfig, load_config
from .db import ClimateDB
from .incidents import evaluate_incidents
from .sensor import SensorError, create_sensor
from .weather import WeatherService

logger = logging.getLogger(__name__)


def default_settings(cfg: AppConfig) -> dict[str, Any]:
    return {
        "humidity_min": cfg.humidity_min,
        "humidity_max": cfg.humidity_max,
        "temp_min": cfg.temp_min,
        "temp_max": cfg.temp_max,
        "sustain_minutes": cfg.sustain_minutes,
        "alert_cooldown_minutes": cfg.alert_cooldown_minutes,
        "rolling_window_points": cfg.rolling_window_points,
        "incident_humidity_delta": cfg.incident_humidity_delta,
        "incident_temp_delta": cfg.incident_temp_delta,
        "incident_cooldown_minutes": cfg.incident_cooldown_minutes,
        "ntfy_server": cfg.ntfy_server,
        "ntfy_topic": cfg.ntfy_topic,
        "ntfy_token": cfg.ntfy_token,
        "sample_interval_seconds": cfg.sample_interval_seconds,
        "quiet_hours_enabled": "true" if cfg.quiet_hours_enabled else "false",
        "quiet_hours_start": cfg.quiet_hours_start,
        "quiet_hours_end": cfg.quiet_hours_end,
        "quiet_hours_timezone": cfg.quiet_hours_timezone,
    }


def create_weather_service(cfg: AppConfig, db: ClimateDB) -> WeatherService | None:
    if not cfg.weather_enabled:
        return None
    return WeatherService(
        db,
        latitude=cfg.latitude,
        longitude=cfg.longitude,
        openweathermap_api_key=cfg.openweathermap_api_key,
        forecast_hours=cfg.weather_forecast_hours,
        request_timeout_seconds=cfg.weather_request_timeout_seconds,
        alert_max_age_minutes=cfg.weather_alert_max_age_minutes,
    )


def _run_weather_poller(
    service: WeatherService, interval_seconds: int, stop: threading.Event
) -> None:
    """Refresh outside conditions on its own thread, so sampling never waits."""
    while not stop.is_set():
        try:
            service.refresh()
        except Exception:  # noqa: BLE001
            logger.exception("Weather poll failed")
        stop.wait(interval_seconds)


def run_collector(cfg: AppConfig | None = None) -> None:
    cfg = cfg or load_config()
    db = ClimateDB(cfg.db_path)
    db.init_schema(default_settings(cfg))
    advisor = AdvisorStore(cfg.advisor_path)
    advisor.ensure()

    sensor = create_sensor(mock=cfg.mock_sensor, gpio_pin=cfg.gpio_pin)
    stop_event = threading.Event()

    def _handle_signal(signum, _frame) -> None:  # noqa: ANN001
        logger.info("Received signal %s; stopping collector", signum)
        stop_event.set()

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

    weather = create_weather_service(cfg, db)
    weather_thread: threading.Thread | None = None
    if weather is not None:
        logger.info(
            "Weather poll every %s min for %.4f,%.4f via %s",
            cfg.weather_poll_interval_minutes,
            cfg.latitude,
            cfg.longitude,
            ", ".join(weather.provider_names),
        )
        weather_thread = threading.Thread(
            target=_run_weather_poller,
            args=(weather, cfg.weather_poll_interval_minutes * 60, stop_event),
            name="weather-poller",
            daemon=True,
        )
        weather_thread.start()

    while not stop_event.is_set():
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
            evaluate_alerts(db, measurement, weather=weather)
            evaluate_incidents(db, measurement, weather=weather, advisor=advisor)
        except SensorError as exc:
            logger.warning("Sensor read failed: %s", exc)
        except Exception:  # noqa: BLE001
            logger.exception("Collector loop error")

        elapsed = time.monotonic() - loop_started
        sleep_for = max(0.0, interval - elapsed)
        stop_event.wait(sleep_for)

    if weather_thread is not None:
        weather_thread.join(timeout=2)

    close = getattr(sensor, "close", None)
    if callable(close):
        close()
    logger.info("Collector stopped")
