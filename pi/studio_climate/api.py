from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .collector import default_settings
from .config import AppConfig, load_config
from .db import SETTING_KEYS, ClimateDB, parse_iso, to_iso
from .sensor import band_status

logger = logging.getLogger(__name__)


class SettingsUpdate(BaseModel):
    humidity_min: float | None = None
    humidity_max: float | None = None
    temp_min: float | None = None
    temp_max: float | None = None
    sustain_minutes: int | None = Field(default=None, ge=1)
    alert_cooldown_minutes: int | None = Field(default=None, ge=1)
    ntfy_server: str | None = None
    ntfy_topic: str | None = None
    ntfy_token: str | None = None
    sample_interval_seconds: int | None = Field(default=None, ge=2)


def _typed_settings(raw: dict[str, str]) -> dict[str, Any]:
    float_keys = {
        "humidity_min",
        "humidity_max",
        "temp_min",
        "temp_max",
    }
    int_keys = {
        "sustain_minutes",
        "alert_cooldown_minutes",
        "sample_interval_seconds",
    }
    out: dict[str, Any] = {}
    for key, value in raw.items():
        if key in float_keys:
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


def create_app(cfg: AppConfig | None = None) -> FastAPI:
    cfg = cfg or load_config()
    db = ClimateDB(cfg.db_path)
    db.init_schema(default_settings(cfg))

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

        return _typed_settings(db.update_settings(updates))

    return app


def run_api(cfg: AppConfig | None = None) -> None:
    import uvicorn

    cfg = cfg or load_config()
    app = create_app(cfg)
    logger.info("API listening on %s:%s", cfg.host, cfg.port)
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="info")
