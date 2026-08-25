from __future__ import annotations

import logging
from datetime import timedelta

import httpx

from .db import ClimateDB, Measurement, parse_iso, to_iso, utc_now

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
) -> bool:
    if not topic:
        logger.debug("ntfy topic empty; skip notification")
        return False
    url = f"{server.rstrip('/')}/{topic}"
    headers = {
        "Title": title,
        "Priority": str(priority),
        "Tags": "thermometer,droplet",
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


def evaluate_alerts(db: ClimateDB, measurement: Measurement) -> list[str]:
    """Update sustain state and notify when a breach lasts long enough."""
    settings = db.get_settings()
    sustain = timedelta(minutes=_as_int(settings, "sustain_minutes", 30))
    cooldown = timedelta(minutes=_as_int(settings, "alert_cooldown_minutes", 120))
    now = utc_now()
    sent: list[str] = []

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

        unit = "% RH" if field == "humidity_pct" else "°C"
        minutes = int(duration.total_seconds() // 60)
        message = (
            f"{label}: {value:.1f}{unit} for {minutes} min "
            f"(threshold {threshold:g}{unit}). "
            f"Studio climate out of target band."
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
