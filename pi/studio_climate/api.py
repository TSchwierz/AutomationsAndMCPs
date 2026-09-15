from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .advisor import AdvisorStore
from .collector import create_weather_service, default_settings
from .config import AppConfig, load_config
from .db import (
    SETTING_KEYS,
    ClimateDB,
    ForecastPoint,
    Incident,
    OutsideReading,
    parse_iso,
    to_iso,
    utc_now,
)
from .incidents import rolling_series, rolling_snapshot
from .quiet_hours import format_hhmm, parse_bool, parse_hhmm
from .sensor import band_status
from .weather import (
    condition_label,
    dew_point_c,
    summarize_next_hours,
    ventilation_advice,
    wind_label,
)

FORECAST_WINDOW_HOURS = 4

logger = logging.getLogger(__name__)


class SettingsUpdate(BaseModel):
    humidity_min: float | None = None
    humidity_max: float | None = None
    temp_min: float | None = None
    temp_max: float | None = None
    sustain_minutes: int | None = Field(default=None, ge=1)
    alert_cooldown_minutes: int | None = Field(default=None, ge=1)
    rolling_window_points: int | None = Field(default=None, ge=2, le=120)
    incident_humidity_delta: float | None = Field(default=None, gt=0)
    incident_temp_delta: float | None = Field(default=None, gt=0)
    incident_cooldown_minutes: int | None = Field(default=None, ge=1)
    ntfy_server: str | None = None
    ntfy_topic: str | None = None
    ntfy_token: str | None = None
    sample_interval_seconds: int | None = Field(default=None, ge=2)
    quiet_hours_enabled: bool | None = None
    quiet_hours_start: str | None = None
    quiet_hours_end: str | None = None
    quiet_hours_timezone: str | None = None


class MarkdownUpdate(BaseModel):
    markdown: str


class IncidentUpdate(BaseModel):
    tags: list[str] | None = None
    notes: str | None = None


def _typed_settings(raw: dict[str, str]) -> dict[str, Any]:
    float_keys = {
        "humidity_min",
        "humidity_max",
        "temp_min",
        "temp_max",
        "incident_humidity_delta",
        "incident_temp_delta",
    }
    int_keys = {
        "sustain_minutes",
        "alert_cooldown_minutes",
        "sample_interval_seconds",
        "rolling_window_points",
        "incident_cooldown_minutes",
    }
    bool_keys = {"quiet_hours_enabled"}
    out: dict[str, Any] = {}
    for key, value in raw.items():
        if key in bool_keys:
            out[key] = parse_bool(value)
        elif key in float_keys:
            try:
                out[key] = float(value)
            except ValueError:
                out[key] = value
        elif key in int_keys:
            try:
                out[key] = int(float(value))
            except ValueError:
                out[key] = value
        else:
            out[key] = value
    return out


def _outside_reading_json(reading: OutsideReading | None) -> dict[str, Any] | None:
    if reading is None:
        return None
    return {
        "ts": to_iso(reading.ts),
        "temperature_c": reading.temperature_c,
        "humidity_pct": reading.humidity_pct,
        "dew_point_c": reading.dew_point_c,
        "condition": reading.condition,
        "condition_label": condition_label(reading.condition),
        "wind_kph": reading.wind_kph,
        "wind_label": wind_label(reading.wind_kph),
        "precipitation_mm": reading.precipitation_mm,
        "sources": reading.sources,
        "provider_count": reading.provider_count,
    }


def _forecast_point_json(point: ForecastPoint) -> dict[str, Any]:
    return {
        "ts": to_iso(point.ts),
        "temperature_c": point.temperature_c,
        "humidity_pct": point.humidity_pct,
        "condition": point.condition,
        "condition_label": condition_label(point.condition),
        "precipitation_probability": point.precipitation_probability,
        "wind_kph": point.wind_kph,
        "provider_count": point.provider_count,
    }


def _summary_json(summary: dict[str, Any] | None) -> dict[str, Any] | None:
    if summary is None:
        return None
    out = dict(summary)
    for key in ("from", "to", "change_at"):
        value = out.get(key)
        out[key] = to_iso(value) if isinstance(value, datetime) else None
    out["condition_now_label"] = condition_label(out.get("condition_now"))
    out["condition_peak_label"] = condition_label(out.get("condition_peak"))
    return out


def _incident_json(incident: Incident) -> dict[str, Any]:
    return {
        "id": incident.id,
        "started_at": to_iso(incident.started_at),
        "ended_at": to_iso(incident.ended_at) if incident.ended_at else None,
        "open": incident.ended_at is None,
        "metric": incident.metric,
        "direction": incident.direction,
        "window_points": incident.window_points,
        "baseline_temp_c": incident.baseline_temp_c,
        "baseline_humidity_pct": incident.baseline_humidity_pct,
        "current_temp_c": incident.current_temp_c,
        "current_humidity_pct": incident.current_humidity_pct,
        "peak_temp_c": incident.peak_temp_c,
        "peak_humidity_pct": incident.peak_humidity_pct,
        "delta_temp_c": incident.delta_temp_c,
        "delta_humidity_pct": incident.delta_humidity_pct,
        "peak_delta_temp_c": incident.peak_delta_temp_c,
        "peak_delta_humidity_pct": incident.peak_delta_humidity_pct,
        "notified": incident.notified,
        "notes": incident.notes,
        "indoor_temp_c": incident.indoor_temp_c,
        "indoor_humidity_pct": incident.indoor_humidity_pct,
        "outdoor_temp_c": incident.outdoor_temp_c,
        "outdoor_humidity_pct": incident.outdoor_humidity_pct,
        "outdoor_condition": incident.outdoor_condition,
        "outdoor_dew_point_c": incident.outdoor_dew_point_c,
        "tags": incident.tags,
    }


def _rolling_from_db(db: ClimateDB, settings: dict[str, Any]) -> dict[str, Any] | None:
    try:
        window = max(2, int(settings.get("rolling_window_points", 8)))
    except (TypeError, ValueError):
        window = 8
    recent = db.recent_measurements(window * 2)
    snapshot = rolling_snapshot(recent, window)
    if snapshot is None:
        return None
    snapshot["thresholds"] = {
        "humidity_pct": settings.get("incident_humidity_delta", 6),
        "temperature_c": settings.get("incident_temp_delta", 1.5),
    }
    return snapshot


def create_app(cfg: AppConfig | None = None) -> FastAPI:
    cfg = cfg or load_config()
    db = ClimateDB(cfg.db_path)
    db.init_schema(default_settings(cfg))
    advisor = AdvisorStore(cfg.advisor_path)
    advisor.ensure()

    app = FastAPI(title="Studio Climate Monitor", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "http://localhost:4173",
            "http://127.0.0.1:4173",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def require_write_token(
        x_api_token: str | None = Header(default=None, alias="X-API-Token"),
        authorization: str | None = Header(default=None),
    ) -> None:
        expected = cfg.api_token
        if not expected:
            return
        provided = x_api_token
        if not provided and authorization and authorization.lower().startswith("bearer "):
            provided = authorization[7:].strip()
        if provided != expected:
            raise HTTPException(status_code=401, detail="Invalid or missing API token")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/current")
    def current() -> dict[str, Any]:
        measurement = db.latest_measurement()
        settings = _typed_settings(db.get_settings())
        rolling = _rolling_from_db(db, settings)
        open_incident = db.get_open_incident()
        if measurement is None:
            return {
                "measurement": None,
                "bands": {
                    "humidity_min": settings.get("humidity_min"),
                    "humidity_max": settings.get("humidity_max"),
                    "temp_min": settings.get("temp_min"),
                    "temp_max": settings.get("temp_max"),
                },
                "status": {"overall": "unknown"},
                "rolling": rolling,
                "open_incident": None if open_incident is None else _incident_json(open_incident),
            }
        status = band_status(
            measurement.temperature_c,
            measurement.humidity_pct,
            float(settings["temp_min"]),
            float(settings["temp_max"]),
            float(settings["humidity_min"]),
            float(settings["humidity_max"]),
        )
        return {
            "measurement": {
                "id": measurement.id,
                "ts": to_iso(measurement.ts),
                "temperature_c": measurement.temperature_c,
                "humidity_pct": measurement.humidity_pct,
            },
            "bands": {
                "humidity_min": settings.get("humidity_min"),
                "humidity_max": settings.get("humidity_max"),
                "temp_min": settings.get("temp_min"),
                "temp_max": settings.get("temp_max"),
            },
            "status": status,
            "rolling": rolling,
            "open_incident": None if open_incident is None else _incident_json(open_incident),
        }

    @app.get("/measurements")
    def measurements(
        from_: str | None = Query(default=None, alias="from"),
        to: str | None = None,
        limit: int = Query(default=5000, ge=1, le=100_000),
    ) -> dict[str, Any]:
        start = parse_iso(from_) if from_ else None
        end = parse_iso(to) if to else None
        rows = db.list_measurements(start=start, end=end, limit=limit)
        return {
            "count": len(rows),
            "measurements": [
                {
                    "id": m.id,
                    "ts": to_iso(m.ts),
                    "temperature_c": m.temperature_c,
                    "humidity_pct": m.humidity_pct,
                }
                for m in rows
            ],
        }

    @app.get("/stats")
    def stats(
        from_: str | None = Query(default=None, alias="from"),
        to: str | None = None,
    ) -> dict[str, Any]:
        start = parse_iso(from_) if from_ else None
        end = parse_iso(to) if to else None
        return db.stats(start=start, end=end)

    weather = create_weather_service(cfg, db)
    stale_after = timedelta(minutes=cfg.weather_poll_interval_minutes * 2)

    def outside_payload() -> dict[str, Any]:
        now = utc_now()
        reading = db.latest_outside_reading()
        indoor = db.latest_measurement()

        indoor_dew = (
            dew_point_c(indoor.temperature_c, indoor.humidity_pct)
            if indoor is not None
            else None
        )
        # Include the hour we are inside so a change one hour out still shows.
        forecast = db.list_forecast(start=now - timedelta(hours=1))
        summary = summarize_next_hours(
            forecast, reading, hours=FORECAST_WINDOW_HOURS, now=now
        )
        window_end = now + timedelta(hours=FORECAST_WINDOW_HOURS)
        points = [
            p
            for p in forecast
            if p.ts >= now.replace(minute=0, second=0, microsecond=0)
            and p.ts <= window_end
        ]
        fetched_at = next((p.fetched_at for p in points if p.fetched_at), None)
        age_seconds = (
            None if reading is None else int((now - reading.ts).total_seconds())
        )

        return {
            "enabled": cfg.weather_enabled,
            "location": {"latitude": cfg.latitude, "longitude": cfg.longitude},
            "providers": weather.provider_names if weather else [],
            "poll_interval_minutes": cfg.weather_poll_interval_minutes,
            "reading": _outside_reading_json(reading),
            "age_seconds": age_seconds,
            "stale": reading is None or (now - reading.ts) > stale_after,
            "indoor": (
                None
                if indoor is None
                else {
                    "ts": to_iso(indoor.ts),
                    "temperature_c": indoor.temperature_c,
                    "humidity_pct": indoor.humidity_pct,
                    "dew_point_c": indoor_dew,
                }
            ),
            "comparison": {
                "temperature_delta_c": (
                    None
                    if indoor is None or reading is None
                    else round(indoor.temperature_c - reading.temperature_c, 2)
                ),
                "humidity_delta_pct": (
                    None
                    if indoor is None or reading is None
                    else round(indoor.humidity_pct - reading.humidity_pct, 1)
                ),
                "indoor_dew_point_c": indoor_dew,
                "outside_dew_point_c": None if reading is None else reading.dew_point_c,
                "ventilation": ventilation_advice(
                    indoor_dew, reading.dew_point_c if reading else None
                ),
            },
            "forecast": {
                "fetched_at": to_iso(fetched_at) if fetched_at else None,
                "window_hours": FORECAST_WINDOW_HOURS,
                "summary": _summary_json(summary),
                "points": [_forecast_point_json(p) for p in points],
            },
        }

    @app.get("/outside")
    def outside() -> dict[str, Any]:
        return outside_payload()

    @app.get("/outside/measurements")
    def outside_measurements(
        from_: str | None = Query(default=None, alias="from"),
        to: str | None = None,
        limit: int = Query(default=5000, ge=1, le=100_000),
    ) -> dict[str, Any]:
        start = parse_iso(from_) if from_ else None
        end = parse_iso(to) if to else None
        rows = db.list_outside_readings(start=start, end=end, limit=limit)
        return {
            "count": len(rows),
            "measurements": [_outside_reading_json(r) for r in rows],
        }

    @app.post("/outside/refresh")
    def refresh_outside(_: None = Depends(require_write_token)) -> dict[str, Any]:
        if weather is None:
            raise HTTPException(status_code=409, detail="Weather polling is disabled")
        snapshot = weather.refresh()
        if snapshot.reading is None:
            raise HTTPException(
                status_code=502,
                detail={
                    "message": "No weather service returned a usable reading",
                    "providers": snapshot.providers,
                },
            )
        return outside_payload()

    @app.get("/settings")
    def get_settings() -> dict[str, Any]:
        return _typed_settings(db.get_settings())

    @app.put("/settings")
    def put_settings(
        body: SettingsUpdate,
        _: None = Depends(require_write_token),
    ) -> dict[str, Any]:
        updates = body.model_dump(exclude_none=True)
        if not updates:
            raise HTTPException(status_code=400, detail="No settings provided")
        unknown = [k for k in updates if k not in SETTING_KEYS]
        if unknown:
            raise HTTPException(status_code=400, detail=f"Unknown keys: {unknown}")

        humidity_min = updates.get("humidity_min")
        humidity_max = updates.get("humidity_max")
        temp_min = updates.get("temp_min")
        temp_max = updates.get("temp_max")
        current = _typed_settings(db.get_settings())
        h_min = float(humidity_min if humidity_min is not None else current["humidity_min"])
        h_max = float(humidity_max if humidity_max is not None else current["humidity_max"])
        t_min = float(temp_min if temp_min is not None else current["temp_min"])
        t_max = float(temp_max if temp_max is not None else current["temp_max"])
        if h_min >= h_max:
            raise HTTPException(status_code=400, detail="humidity_min must be < humidity_max")
        if t_min >= t_max:
            raise HTTPException(status_code=400, detail="temp_min must be < temp_max")

        for key in ("quiet_hours_start", "quiet_hours_end"):
            if key in updates:
                parsed = parse_hhmm(updates[key])
                if parsed is None:
                    raise HTTPException(
                        status_code=400, detail=f"{key} must be a 24h time like '23:00'"
                    )
                updates[key] = format_hhmm(parsed)

        q_start = updates.get("quiet_hours_start", current.get("quiet_hours_start"))
        q_end = updates.get("quiet_hours_end", current.get("quiet_hours_end"))
        if q_start and q_end and q_start == q_end:
            raise HTTPException(
                status_code=400, detail="quiet_hours_start and quiet_hours_end must differ"
            )

        if "quiet_hours_timezone" in updates:
            tz_name = str(updates["quiet_hours_timezone"]).strip()
            if tz_name:
                try:
                    ZoneInfo(tz_name)
                except (ZoneInfoNotFoundError, ValueError) as exc:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Unknown timezone '{tz_name}'; use an IANA name like Europe/Berlin",
                    ) from exc
            updates["quiet_hours_timezone"] = tz_name

        if "quiet_hours_enabled" in updates:
            updates["quiet_hours_enabled"] = "true" if updates["quiet_hours_enabled"] else "false"

        return _typed_settings(db.update_settings(updates))

    @app.get("/rolling-average")
    def get_rolling_average(
        from_: str | None = Query(default=None, alias="from"),
        to: str | None = None,
        window: int | None = Query(default=None, ge=2, le=120),
        limit: int = Query(default=20_000, ge=1, le=100_000),
    ) -> dict[str, Any]:
        settings = _typed_settings(db.get_settings())
        try:
            configured = max(2, int(settings.get("rolling_window_points", 8)))
        except (TypeError, ValueError):
            configured = 8
        size = window or configured
        start = parse_iso(from_) if from_ else None
        end = parse_iso(to) if to else None
        rows = db.list_measurements(start=start, end=end, limit=limit)
        snapshot = rolling_snapshot(rows, size)
        if snapshot is not None:
            snapshot["thresholds"] = {
                "humidity_pct": settings.get("incident_humidity_delta", 6),
                "temperature_c": settings.get("incident_temp_delta", 1.5),
            }
        return {
            "window_points": size,
            "count": len(rows),
            "current": snapshot,
            "series": rolling_series(rows, size),
        }

    @app.get("/incidents")
    def get_incidents(
        from_: str | None = Query(default=None, alias="from"),
        to: str | None = None,
        limit: int = Query(default=200, ge=1, le=10_000),
        open_only: bool = False,
    ) -> dict[str, Any]:
        start = parse_iso(from_) if from_ else None
        end = parse_iso(to) if to else None
        rows = db.list_incidents(start=start, end=end, limit=limit, open_only=open_only)
        return {
            "count": len(rows),
            "incidents": [_incident_json(item) for item in rows],
        }

    @app.get("/incidents/tags")
    def get_incident_tags() -> dict[str, Any]:
        return {"tags": db.list_tag_catalog()}

    @app.patch("/incidents/{incident_id}")
    def patch_incident(
        incident_id: int,
        body: IncidentUpdate,
        _: None = Depends(require_write_token),
    ) -> dict[str, Any]:
        incident = db.get_incident(incident_id)
        if incident is None:
            raise HTTPException(status_code=404, detail="Incident not found")
        updates = body.model_dump(exclude_none=True)
        if not updates:
            raise HTTPException(status_code=400, detail="No incident fields provided")
        if "notes" in updates:
            incident = db.set_incident_notes(incident_id, str(updates["notes"]))
        if "tags" in updates:
            incident = db.set_incident_tags(incident_id, updates["tags"])
        if incident is None:
            raise HTTPException(status_code=404, detail="Incident not found")
        return _incident_json(incident)

    @app.get("/advisor/context")
    def advisor_context() -> dict[str, Any]:
        settings = _typed_settings(db.get_settings())
        measurement = db.latest_measurement()
        open_incident = db.get_open_incident()
        live = {
            "measurement": (
                None
                if measurement is None
                else {
                    "ts": to_iso(measurement.ts),
                    "temperature_c": measurement.temperature_c,
                    "humidity_pct": measurement.humidity_pct,
                }
            ),
            "rolling": _rolling_from_db(db, settings),
            "open_incident": None if open_incident is None else _incident_json(open_incident),
        }
        return advisor.context_pack(live=live)

    @app.get("/advisor/reports")
    def advisor_reports() -> dict[str, Any]:
        ids = advisor.list_report_ids()
        return {"count": len(ids), "weeks": ids}

    @app.get("/advisor/info")
    def get_advisor_info() -> dict[str, str]:
        return {"markdown": advisor.read_markdown("info.md")}

    @app.get("/advisor/strategies")
    def get_advisor_strategies() -> dict[str, str]:
        return {"markdown": advisor.read_markdown("strategies.md")}

    @app.put("/advisor/info")
    def put_advisor_info(
        body: MarkdownUpdate,
        _: None = Depends(require_write_token),
    ) -> dict[str, str]:
        return {"markdown": advisor.write_markdown("info.md", body.markdown)}

    @app.put("/advisor/strategies")
    def put_advisor_strategies(
        body: MarkdownUpdate,
        _: None = Depends(require_write_token),
    ) -> dict[str, str]:
        return {"markdown": advisor.write_markdown("strategies.md", body.markdown)}

    return app


def run_api(cfg: AppConfig | None = None) -> None:
    import uvicorn

    cfg = cfg or load_config()
    app = create_app(cfg)
    logger.info("API listening on %s:%s", cfg.host, cfg.port)
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="info")
