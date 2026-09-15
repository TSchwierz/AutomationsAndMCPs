from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .advisor import AdvisorStore, iso_week_id
from .db import ClimateDB, Incident, Measurement, OutsideReading, to_iso
from .sensor import band_status


def parse_iso_week(week: str) -> tuple[int, int]:
    text = week.strip().upper()
    if "-W" not in text:
        raise ValueError("week must look like YYYY-Www")
    year_s, week_s = text.split("-W", 1)
    year = int(year_s)
    week_no = int(week_s)
    if not 1 <= week_no <= 53:
        raise ValueError("ISO week must be 1–53")
    return year, week_no


def week_bounds(week: str | None, now: datetime) -> tuple[str, datetime, datetime]:
    if week:
        year, week_no = parse_iso_week(week)
        label = f"{year}-W{week_no:02d}"
    else:
        label = iso_week_id(now)
        year, week_no = parse_iso_week(label)
    monday = date.fromisocalendar(year, week_no, 1)
    start = datetime(monday.year, monday.month, monday.day, tzinfo=timezone.utc)
    return label, start, start + timedelta(days=7)


def _hours_out_of_band(
    measurements: list[Measurement],
    settings: dict[str, Any],
    sample_interval_seconds: int,
) -> dict[str, float]:
    t_min = float(settings.get("temp_min", 18))
    t_max = float(settings.get("temp_max", 24))
    h_min = float(settings.get("humidity_min", 40))
    h_max = float(settings.get("humidity_max", 55))
    hours = sample_interval_seconds / 3600.0
    temp_hours = 0.0
    humidity_hours = 0.0
    either_hours = 0.0
    for item in measurements:
        status = band_status(
            item.temperature_c,
            item.humidity_pct,
            t_min,
            t_max,
            h_min,
            h_max,
        )
        temp_out = not status["temperature_in_band"]
        hum_out = not status["humidity_in_band"]
        if temp_out:
            temp_hours += hours
        if hum_out:
            humidity_hours += hours
        if temp_out or hum_out:
            either_hours += hours
    return {
        "temperature": round(temp_hours, 2),
        "humidity": round(humidity_hours, 2),
        "either": round(either_hours, 2),
    }


def _daily_indoor(measurements: list[Measurement]) -> list[dict[str, Any]]:
    buckets: dict[str, list[Measurement]] = defaultdict(list)
    for item in measurements:
        buckets[item.ts.date().isoformat()].append(item)
    days: list[dict[str, Any]] = []
    for day in sorted(buckets):
        rows = buckets[day]
        temps = [m.temperature_c for m in rows]
        hums = [m.humidity_pct for m in rows]
        days.append(
            {
                "date": day,
                "samples": len(rows),
                "temperature_c": {
                    "min": round(min(temps), 2),
                    "max": round(max(temps), 2),
                    "avg": round(sum(temps) / len(temps), 2),
                },
                "humidity_pct": {
                    "min": round(min(hums), 2),
                    "max": round(max(hums), 2),
                    "avg": round(sum(hums) / len(hums), 2),
                },
            }
        )
    return days


def _incident_brief(incident: Incident) -> dict[str, Any]:
    return {
        "id": incident.id,
        "started_at": to_iso(incident.started_at),
        "ended_at": to_iso(incident.ended_at) if incident.ended_at else None,
        "metric": incident.metric,
        "direction": incident.direction,
        "delta_temp_c": incident.peak_delta_temp_c,
        "delta_humidity_pct": incident.peak_delta_humidity_pct,
        "tags": incident.tags,
        "notes": incident.notes,
        "outdoor_temp_c": incident.outdoor_temp_c,
        "outdoor_humidity_pct": incident.outdoor_humidity_pct,
        "outdoor_dew_point_c": incident.outdoor_dew_point_c,
        "outdoor_condition": incident.outdoor_condition,
    }


def _outdoor_summary(rows: list[OutsideReading]) -> dict[str, Any] | None:
    if not rows:
        return None
    temps = [r.temperature_c for r in rows]
    hums = [r.humidity_pct for r in rows]
    dews = [r.dew_point_c for r in rows if r.dew_point_c is not None]
    return {
        "samples": len(rows),
        "temperature_c": {
            "min": round(min(temps), 2),
            "max": round(max(temps), 2),
            "avg": round(sum(temps) / len(temps), 2),
        },
        "humidity_pct": {
            "min": round(min(hums), 2),
            "max": round(max(hums), 2),
            "avg": round(sum(hums) / len(hums), 2),
        },
        "dew_point_c": (
            None
            if not dews
            else {
                "min": round(min(dews), 2),
                "max": round(max(dews), 2),
                "avg": round(sum(dews) / len(dews), 2),
            }
        ),
    }


def build_briefing(
    db: ClimateDB,
    *,
    week: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    moment = now or datetime.now(timezone.utc)
    label, start, end = week_bounds(week, moment)
    settings = db.get_settings()
    try:
        interval = max(2, int(float(settings.get("sample_interval_seconds", 60))))
    except (TypeError, ValueError):
        interval = 60
    indoor = db.list_measurements(start=start, end=end, limit=100_000)
    outdoor = db.list_outside_readings(start=start, end=end, limit=10_000)
    incidents = db.list_incidents(start=start, end=end, limit=10_000)
    tag_counts = Counter(tag for incident in incidents for tag in incident.tags)
    untagged = sum(1 for incident in incidents if not incident.tags)
    return {
        "week": label,
        "from": to_iso(start),
        "to": to_iso(end),
        "bands": {
            "humidity_min": float(settings.get("humidity_min", 40)),
            "humidity_max": float(settings.get("humidity_max", 55)),
            "temp_min": float(settings.get("temp_min", 18)),
            "temp_max": float(settings.get("temp_max", 24)),
        },
        "sample_interval_seconds": interval,
        "indoor": {
            "samples": len(indoor),
            "hours_out_of_band": _hours_out_of_band(indoor, settings, interval),
            "days": _daily_indoor(indoor),
        },
        "outdoor": _outdoor_summary(outdoor),
        "incidents": [_incident_brief(item) for item in reversed(incidents)],
        "tag_counts": dict(sorted(tag_counts.items())),
        "untagged_incidents": untagged,
    }


def write_briefing(
    db: ClimateDB,
    store: AdvisorStore,
    *,
    week: str | None = None,
    now: datetime | None = None,
) -> Path:
    payload = build_briefing(db, week=week, now=now)
    return store.write_report(str(payload["week"]), payload)
