from __future__ import annotations

import logging
from datetime import timedelta

import httpx

from .db import ClimateDB, Measurement, OutsideReading, parse_iso, to_iso, utc_now
from .quiet_hours import QuietHours
from .weather import (
    WeatherService,
    condition_label,
    dew_point_c,
    ventilation_advice,
    wind_label,
)

logger = logging.getLogger(__name__)

CONDITIONS = (
    ("humidity_high", "humidity_pct", "gt", "humidity_max", "Humidity high — mold risk"),
    ("humidity_low", "humidity_pct", "lt", "humidity_min", "Humidity low — PLA may dry out / comfort"),
    ("temp_high", "temperature_c", "gt", "temp_max", "Temperature high"),
    ("temp_low", "temperature_c", "lt", "temp_min", "Temperature low"),
)


def _as_float(settings: dict[str, str], key: str, default: float) -> float:
    try:
        return float(settings.get(key, default))
    except (TypeError, ValueError):
        return float(default)


def _as_int(settings: dict[str, str], key: str, default: int) -> int:
    try:
        return int(float(settings.get(key, default)))
    except (TypeError, ValueError):
        return int(default)


def _is_breaching(value: float, op: str, threshold: float) -> bool:
    if op == "gt":
        return value > threshold
    if op == "lt":
        return value < threshold
    return False


def publish_ntfy(
    *,
    server: str,
    topic: str,
    token: str,
    title: str,
    message: str,
    priority: int = 3,
    tags: str = "thermometer,droplet",
) -> bool:
    if not topic:
        logger.debug("ntfy topic empty; skip notification")
        return False
    url = f"{server.rstrip('/')}/{topic}"
    headers = {
        "Title": title,
        "Priority": str(priority),
        "Tags": tags,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        response = httpx.post(url, content=message.encode("utf-8"), headers=headers, timeout=15.0)
        response.raise_for_status()
        return True
    except Exception:  # noqa: BLE001
        logger.exception("Failed to publish ntfy notification")
        return False


def describe_outside(reading: OutsideReading | None, measurement: Measurement) -> str:
    """The outside block appended to an alert, including a ventilation hint."""
    if reading is None:
        return "Outside conditions unavailable."

    parts = [f"{reading.temperature_c:.1f}°C", f"{reading.humidity_pct:.0f}% RH"]
    if reading.condition:
        parts.append(condition_label(reading.condition))
    gust = wind_label(reading.wind_kph)
    if gust:
        parts.append(f"{gust} {reading.wind_kph:.0f} km/h")
    age_minutes = int((utc_now() - reading.ts).total_seconds() // 60)
    freshness = f", {age_minutes} min old" if age_minutes >= 5 else ""

    indoor_dew = dew_point_c(measurement.temperature_c, measurement.humidity_pct)
    advice = ventilation_advice(indoor_dew, reading.dew_point_c)
    return (
        f"Outside: {', '.join(parts)} "
        f"(dew point {reading.dew_point_c:.1f}°C{freshness}). {advice['reason']}"
        if reading.dew_point_c is not None
        else f"Outside: {', '.join(parts)}{freshness}. {advice['reason']}"
    )


def evaluate_alerts(
    db: ClimateDB,
    measurement: Measurement,
    weather: WeatherService | None = None,
) -> list[str]:
    """Update sustain state and notify when a breach lasts long enough."""
    settings = db.get_settings()
    sustain = timedelta(minutes=_as_int(settings, "sustain_minutes", 30))
    cooldown = timedelta(minutes=_as_int(settings, "alert_cooldown_minutes", 360))
    quiet_hours = QuietHours.from_settings(settings)
    now = utc_now()
    quiet = quiet_hours.is_quiet(now)
    sent: list[str] = []

    outside: OutsideReading | None = None
    outside_loaded = False

    def outside_block() -> str:
        """Fetched at most once per evaluation, and only if an alert goes out."""
        nonlocal outside, outside_loaded
        if weather is None:
            return ""
        if not outside_loaded:
            outside = weather.reading_for_alert()
            outside_loaded = True
        return "\n" + describe_outside(outside, measurement)

    for key, field, op, threshold_key, label in CONDITIONS:
        value = getattr(measurement, field)
        threshold = _as_float(settings, threshold_key, 0.0)
        breaching = _is_breaching(value, op, threshold)
        state = db.get_alert_state(key)

        if not breaching:
            if state and state.get("breach_started_at"):
                db.clear_alert_breach(key)
            continue

        breach_started_at = state.get("breach_started_at") if state else None
        last_alerted_at = state.get("last_alerted_at") if state else None

        if not breach_started_at:
            db.upsert_alert_state(key, to_iso(now), last_alerted_at, value)
            continue

        started = parse_iso(breach_started_at)
        duration = now - started
        db.upsert_alert_state(key, breach_started_at, last_alerted_at, value)

        if duration < sustain:
            continue

        if last_alerted_at:
            last = parse_iso(last_alerted_at)
            if now - last < cooldown:
                continue

        if quiet:
            # last_alerted_at stays untouched, so the alert fires on the first
            # sample after the window ends if the breach is still going.
            logger.info(
                "Quiet hours (%s) active; suppressing alert: %s",
                quiet_hours.describe(),
                key,
            )
            continue

        unit = "% RH" if field == "humidity_pct" else "°C"
        minutes = int(duration.total_seconds() // 60)
        message = (
            f"{label}: {value:.1f}{unit} for {minutes} min "
            f"(threshold {threshold:g}{unit}). "
            f"Studio climate out of target band."
            f"{outside_block()}"
        )
        ok = publish_ntfy(
            server=settings.get("ntfy_server", "https://ntfy.sh"),
            topic=settings.get("ntfy_topic", ""),
            token=settings.get("ntfy_token", ""),
            title="Studio climate alert",
            message=message,
            priority=4,
        )
        if ok:
            db.upsert_alert_state(key, breach_started_at, to_iso(now), value)
            sent.append(key)
            logger.info("Alert sent: %s", key)

    return sent
