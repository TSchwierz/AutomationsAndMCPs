from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Sequence

from .advisor import AdvisorStore
from .alerts import _as_float, _as_int, describe_outside, publish_ntfy
from .db import (
    DEFAULT_INCIDENT_TAGS,
    ClimateDB,
    Incident,
    Measurement,
    OutsideReading,
    to_iso,
    utc_now,
)
from .quiet_hours import QuietHours
from .weather import WeatherService

logger = logging.getLogger(__name__)

DEFAULT_WINDOW_POINTS = 8
DEFAULT_HUMIDITY_DELTA = 6.0
DEFAULT_TEMP_DELTA = 1.5
DEFAULT_INCIDENT_COOLDOWN_MINUTES = 20
SETTLE_FRACTION = 0.5
ANOMALY_LOOKBACK_DAYS = 14


def _avg(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def rolling_series(
    measurements: Sequence[Measurement], window: int
) -> list[dict[str, Any]]:
    """Sliding-window averages, using a partial window until `window` samples exist."""
    if window < 1 or not measurements:
        return []
    series: list[dict[str, Any]] = []
    temp_sum = 0.0
    hum_sum = 0.0
    for i, measurement in enumerate(measurements):
        temp_sum += measurement.temperature_c
        hum_sum += measurement.humidity_pct
        if i >= window:
            dropped = measurements[i - window]
            temp_sum -= dropped.temperature_c
            hum_sum -= dropped.humidity_pct
        count = min(i + 1, window)
        series.append(
            {
                "ts": to_iso(measurement.ts),
                "temperature_c": round(temp_sum / count, 3),
                "humidity_pct": round(hum_sum / count, 3),
                "window_points": count,
            }
        )
    return series


def rolling_snapshot(
    measurements: Sequence[Measurement], window: int
) -> dict[str, Any] | None:
    """Inspectable current vs previous window averages."""
    if window < 1 or not measurements:
        return None
    current = measurements[-window:] if len(measurements) >= window else list(measurements)
    previous: Sequence[Measurement] = []
    if len(measurements) >= 2 * window:
        previous = measurements[-2 * window : -window]
    elif len(measurements) > len(current):
        previous = measurements[: -len(current)]

    current_temp = _avg([m.temperature_c for m in current])
    current_humidity = _avg([m.humidity_pct for m in current])
    previous_temp = _avg([m.temperature_c for m in previous]) if previous else None
    previous_humidity = _avg([m.humidity_pct for m in previous]) if previous else None
    span_seconds = (
        0
        if len(current) < 2
        else int((current[-1].ts - current[0].ts).total_seconds())
    )
    return {
        "window_points": window,
        "sample_count": len(current),
        "from": to_iso(current[0].ts),
        "to": to_iso(current[-1].ts),
        "span_seconds": span_seconds,
        "temperature_c": round(current_temp, 3),
        "humidity_pct": round(current_humidity, 3),
        "previous": (
            None
            if previous_temp is None or previous_humidity is None
            else {
                "sample_count": len(previous),
                "from": to_iso(previous[0].ts),
                "to": to_iso(previous[-1].ts),
                "temperature_c": round(previous_temp, 3),
                "humidity_pct": round(previous_humidity, 3),
            }
        ),
        "delta": (
            None
            if previous_temp is None or previous_humidity is None
            else {
                "temperature_c": round(current_temp - previous_temp, 3),
                "humidity_pct": round(current_humidity - previous_humidity, 3),
            }
        ),
    }


def _direction(delta: float) -> str:
    return "up" if delta >= 0 else "down"


def _combined_direction(*, humidity_hit: bool, temp_hit: bool, d_h: float, d_t: float) -> str:
    dirs: list[str] = []
    if humidity_hit:
        dirs.append(_direction(d_h))
    if temp_hit:
        dirs.append(_direction(d_t))
    if not dirs:
        return "up"
    if all(item == dirs[0] for item in dirs):
        return dirs[0]
    return "mixed"


def _metric_name(*, humidity_hit: bool, temp_hit: bool) -> str:
    if humidity_hit and temp_hit:
        return "both"
    if temp_hit:
        return "temperature"
    return "humidity"


def _larger_magnitude(current: float, previous: float) -> float:
    return current if abs(current) >= abs(previous) else previous


def _incident_title(incident: Incident) -> str:
    if incident.metric == "both":
        return "Studio climate shifted"
    if incident.metric == "temperature":
        return "Studio temperature shifted"
    return "Studio humidity shifted"


def _format_change(label: str, delta: float, unit: str, before: float, after: float) -> str:
    verb = "rose" if delta >= 0 else "fell"
    return (
        f"{label} {verb} {abs(delta):.1f}{unit} "
        f"({before:.1f}{unit} → {after:.1f}{unit})"
    )


def describe_incident(incident: Incident, sample_interval_seconds: int) -> str:
    minutes = max(1, int(incident.window_points * sample_interval_seconds / 60))
    parts: list[str] = []
    if incident.metric in {"humidity", "both"}:
        parts.append(
            _format_change(
                "Humidity",
                incident.delta_humidity_pct,
                "% RH",
                incident.baseline_humidity_pct,
                incident.current_humidity_pct,
            )
        )
    if incident.metric in {"temperature", "both"}:
        parts.append(
            _format_change(
                "Temperature",
                incident.delta_temp_c,
                "°C",
                incident.baseline_temp_c,
                incident.current_temp_c,
            )
        )
    body = "; ".join(parts)
    tags = ", ".join(DEFAULT_INCIDENT_TAGS)
    return (
        f"{body} over the last {incident.window_points} samples (~{minutes} min). "
        f"Tag this in the dashboard ({tags})."
    )


def is_irregular(db: ClimateDB, incident: Incident) -> bool:
    """Untagged close with no same metric+direction event in the last 14 days."""
    if incident.tags:
        return False
    lookback = incident.started_at - timedelta(days=ANOMALY_LOOKBACK_DAYS)
    previous = db.list_incidents(start=lookback, end=incident.started_at, limit=10_000)
    for other in previous:
        if other.id == incident.id:
            continue
        if other.metric == incident.metric and other.direction == incident.direction:
            return False
    return True


def maybe_note_anomaly(
    db: ClimateDB,
    incident: Incident,
    settings: dict[str, str],
    advisor: AdvisorStore | None,
) -> bool:
    if advisor is None or not is_irregular(db, incident):
        return False
    advisor.append_anomaly(incident)
    quiet_hours = QuietHours.from_settings(settings)
    if quiet_hours.is_quiet(utc_now()):
        logger.info("Quiet hours active; skipping anomaly ntfy for incident %s", incident.id)
        return True
    publish_ntfy(
        server=settings.get("ntfy_server", "https://ntfy.sh"),
        topic=settings.get("ntfy_topic", ""),
        token=settings.get("ntfy_token", ""),
        title="Unusual studio climate shift",
        message=(
            f"Untagged {incident.metric} {incident.direction} with no similar event "
            f"in {ANOMALY_LOOKBACK_DAYS} days. Ask the dashboard advisor."
        ),
        priority=2,
        tags="grey_question,thought_balloon",
    )
    return True


def evaluate_incidents(
    db: ClimateDB,
    measurement: Measurement,
    weather: WeatherService | None = None,
    advisor: AdvisorStore | None = None,
) -> list[int]:
    """Open or update a climate-change incident from rolling-window deltas."""
    settings = db.get_settings()
    window = max(2, _as_int(settings, "rolling_window_points", DEFAULT_WINDOW_POINTS))
    h_thresh = max(0.5, _as_float(settings, "incident_humidity_delta", DEFAULT_HUMIDITY_DELTA))
    t_thresh = max(0.2, _as_float(settings, "incident_temp_delta", DEFAULT_TEMP_DELTA))
    cooldown = timedelta(
        minutes=max(
            1,
            _as_int(
                settings,
                "incident_cooldown_minutes",
                DEFAULT_INCIDENT_COOLDOWN_MINUTES,
            ),
        )
    )
    sample_interval = max(
        2, _as_int(settings, "sample_interval_seconds", 60)
    )
    quiet_hours = QuietHours.from_settings(settings)
    now = utc_now()

    recent = db.recent_measurements(window * 2)
    if len(recent) < window * 2:
        return []

    snapshot = rolling_snapshot(recent, window)
    if snapshot is None or snapshot["delta"] is None:
        return []

    d_h = float(snapshot["delta"]["humidity_pct"])
    d_t = float(snapshot["delta"]["temperature_c"])
    cur_h = float(snapshot["humidity_pct"])
    cur_t = float(snapshot["temperature_c"])
    prev = snapshot["previous"]
    assert prev is not None
    base_h = float(prev["humidity_pct"])
    base_t = float(prev["temperature_c"])

    humidity_hit = abs(d_h) >= h_thresh
    temp_hit = abs(d_t) >= t_thresh
    humidity_settled = abs(d_h) < h_thresh * SETTLE_FRACTION
    temp_settled = abs(d_t) < t_thresh * SETTLE_FRACTION

    open_inc = db.get_open_incident()
    changed_ids: list[int] = []

    if open_inc is not None:
        metric = open_inc.metric
        if humidity_hit and metric == "temperature":
            metric = "both"
        if temp_hit and metric == "humidity":
            metric = "both"
        if metric == "both":
            direction = _combined_direction(
                humidity_hit=True, temp_hit=True, d_h=d_h, d_t=d_t
            )
        elif metric == "humidity":
            direction = _direction(d_h)
        else:
            direction = _direction(d_t)

        peak_d_h = _larger_magnitude(d_h, open_inc.peak_delta_humidity_pct)
        peak_d_t = _larger_magnitude(d_t, open_inc.peak_delta_temp_c)
        peak_h = cur_h if abs(d_h) >= abs(open_inc.peak_delta_humidity_pct) else open_inc.peak_humidity_pct
        peak_t = cur_t if abs(d_t) >= abs(open_inc.peak_delta_temp_c) else open_inc.peak_temp_c

        db.update_incident(
            open_inc.id,
            metric=metric,
            direction=direction,
            current_temp_c=cur_t,
            current_humidity_pct=cur_h,
            peak_temp_c=peak_t,
            peak_humidity_pct=peak_h,
            delta_temp_c=d_t,
            delta_humidity_pct=d_h,
            peak_delta_temp_c=peak_d_t,
            peak_delta_humidity_pct=peak_d_h,
        )

        watching_humidity = metric in {"humidity", "both"}
        watching_temp = metric in {"temperature", "both"}
        settled = (not watching_humidity or humidity_settled) and (
            not watching_temp or temp_settled
        )
        if settled and not humidity_hit and not temp_hit:
            db.close_incident(open_inc.id, now)
            logger.info("Incident %s closed", open_inc.id)
            closed = db.get_incident(open_inc.id)
            if closed is not None:
                maybe_note_anomaly(db, closed, settings, advisor)
        return changed_ids

    if not humidity_hit and not temp_hit:
        return changed_ids

    metric = _metric_name(humidity_hit=humidity_hit, temp_hit=temp_hit)
    direction = _combined_direction(
        humidity_hit=humidity_hit, temp_hit=temp_hit, d_h=d_h, d_t=d_t
    )

    outside: OutsideReading | None = None
    if weather is not None:
        try:
            outside = weather.reading_for_alert()
        except Exception:  # noqa: BLE001
            logger.exception("Weather snapshot for incident failed")

    incident = db.insert_incident(
        Incident(
            id=0,
            started_at=measurement.ts,
            metric=metric,
            direction=direction,
            window_points=window,
            baseline_temp_c=base_t,
            baseline_humidity_pct=base_h,
            current_temp_c=cur_t,
            current_humidity_pct=cur_h,
            peak_temp_c=cur_t,
            peak_humidity_pct=cur_h,
            delta_temp_c=d_t,
            delta_humidity_pct=d_h,
            peak_delta_temp_c=d_t,
            peak_delta_humidity_pct=d_h,
            indoor_temp_c=measurement.temperature_c,
            indoor_humidity_pct=measurement.humidity_pct,
            outdoor_temp_c=None if outside is None else outside.temperature_c,
            outdoor_humidity_pct=None if outside is None else outside.humidity_pct,
            outdoor_condition=None if outside is None else outside.condition,
            outdoor_dew_point_c=None if outside is None else outside.dew_point_c,
        )
    )
    changed_ids.append(incident.id)
    logger.info(
        "Incident %s opened (%s %s, dH=%.1f dT=%.1f)",
        incident.id,
        metric,
        direction,
        d_h,
        d_t,
    )

    last_notified = db.latest_notified_incident()
    if last_notified is not None and now - last_notified.started_at < cooldown:
        logger.info("Incident %s opened; notification cooldown active", incident.id)
        return changed_ids

    if quiet_hours.is_quiet(now):
        logger.info(
            "Quiet hours (%s) active; suppressing change warning for incident %s",
            quiet_hours.describe(),
            incident.id,
        )
        return changed_ids

    message = describe_incident(incident, sample_interval)
    outside_text = describe_outside(outside, measurement)
    ok = publish_ntfy(
        server=settings.get("ntfy_server", "https://ntfy.sh"),
        topic=settings.get("ntfy_topic", ""),
        token=settings.get("ntfy_token", ""),
        title=_incident_title(incident),
        message=f"{message}\n{outside_text}",
        priority=3,
        tags="warning,chart_with_upwards_trend",
    )
    if ok:
        db.mark_incident_notified(incident.id)
        logger.info("Change warning sent for incident %s", incident.id)

    return changed_ids
