from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence


SETTING_KEYS = (
    "humidity_min",
    "humidity_max",
    "temp_min",
    "temp_max",
    "sustain_minutes",
    "alert_cooldown_minutes",
    "rolling_window_points",
    "incident_humidity_delta",
    "incident_temp_delta",
    "incident_cooldown_minutes",
    "ntfy_server",
    "ntfy_topic",
    "ntfy_token",
    "sample_interval_seconds",
    "quiet_hours_enabled",
    "quiet_hours_start",
    "quiet_hours_end",
    "quiet_hours_timezone",
)

DEFAULT_INCIDENT_TAGS = (
    "Shower",
    "Cooking",
    "Airing out",
    "Outside weather effects",
)

OLD_ALERT_COOLDOWN_MINUTES = "120"
SCHEMA_VERSION = 2


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@dataclass
class Measurement:
    id: int
    ts: datetime
    temperature_c: float
    humidity_pct: float


@dataclass
class OutsideReading:
    """Outdoor conditions averaged across the weather services."""

    ts: datetime
    temperature_c: float
    humidity_pct: float
    dew_point_c: float | None = None
    condition: str | None = None
    wind_kph: float | None = None
    precipitation_mm: float | None = None
    sources: list[str] = field(default_factory=list)
    provider_count: int = 0
    id: int | None = None


@dataclass
class Incident:
    """A detected strong change in the rolling indoor climate average.

    Stored so the dashboard can label causes, and so a later export can send
    tagged history to an analyser.
    """

    id: int
    started_at: datetime
    metric: str
    direction: str
    window_points: int
    baseline_temp_c: float
    baseline_humidity_pct: float
    current_temp_c: float
    current_humidity_pct: float
    peak_temp_c: float
    peak_humidity_pct: float
    delta_temp_c: float
    delta_humidity_pct: float
    peak_delta_temp_c: float
    peak_delta_humidity_pct: float
    ended_at: datetime | None = None
    notified: bool = False
    notes: str = ""
    indoor_temp_c: float | None = None
    indoor_humidity_pct: float | None = None
    outdoor_temp_c: float | None = None
    outdoor_humidity_pct: float | None = None
    outdoor_condition: str | None = None
    outdoor_dew_point_c: float | None = None
    tags: list[str] = field(default_factory=list)


@dataclass
class ForecastPoint:
    """One hour of the averaged outlook."""

    ts: datetime
    temperature_c: float
    humidity_pct: float | None = None
    condition: str | None = None
    precipitation_probability: float | None = None
    wind_kph: float | None = None
    provider_count: int = 0
    fetched_at: datetime | None = None


def _row_to_outside_reading(row: sqlite3.Row) -> OutsideReading:
    raw_sources = str(row["sources"] or "")
    return OutsideReading(
        id=int(row["id"]),
        ts=parse_iso(row["ts"]),
        temperature_c=float(row["temperature_c"]),
        humidity_pct=float(row["humidity_pct"]),
        dew_point_c=row["dew_point_c"],
        condition=row["condition"],
        wind_kph=row["wind_kph"],
        precipitation_mm=row["precipitation_mm"],
        sources=[s for s in raw_sources.split(",") if s],
        provider_count=int(row["provider_count"] or 0),
    )


def _row_to_incident(row: sqlite3.Row, tags: list[str] | None = None) -> Incident:
    return Incident(
        id=int(row["id"]),
        started_at=parse_iso(row["started_at"]),
        ended_at=parse_iso(row["ended_at"]) if row["ended_at"] else None,
        metric=str(row["metric"]),
        direction=str(row["direction"]),
        window_points=int(row["window_points"]),
        baseline_temp_c=float(row["baseline_temp_c"]),
        baseline_humidity_pct=float(row["baseline_humidity_pct"]),
        current_temp_c=float(row["current_temp_c"]),
        current_humidity_pct=float(row["current_humidity_pct"]),
        peak_temp_c=float(row["peak_temp_c"]),
        peak_humidity_pct=float(row["peak_humidity_pct"]),
        delta_temp_c=float(row["delta_temp_c"]),
        delta_humidity_pct=float(row["delta_humidity_pct"]),
        peak_delta_temp_c=float(row["peak_delta_temp_c"]),
        peak_delta_humidity_pct=float(row["peak_delta_humidity_pct"]),
        notified=bool(int(row["notified"] or 0)),
        notes=str(row["notes"] or ""),
        indoor_temp_c=row["indoor_temp_c"],
        indoor_humidity_pct=row["indoor_humidity_pct"],
        outdoor_temp_c=row["outdoor_temp_c"],
        outdoor_humidity_pct=row["outdoor_humidity_pct"],
        outdoor_condition=row["outdoor_condition"],
        outdoor_dew_point_c=row["outdoor_dew_point_c"],
        tags=tags or [],
    )


def _apply_schema_migrations(conn: sqlite3.Connection, defaults: dict[str, Any]) -> None:
    row = conn.execute(
        "SELECT value FROM schema_meta WHERE key = 'schema_version'"
    ).fetchone()
    version = int(row["value"]) if row else 1
    if version < 2:
        cooldown = conn.execute(
            "SELECT value FROM settings WHERE key = 'alert_cooldown_minutes'"
        ).fetchone()
        if cooldown is not None and str(cooldown["value"]) == OLD_ALERT_COOLDOWN_MINUTES:
            conn.execute(
                "UPDATE settings SET value = ? WHERE key = 'alert_cooldown_minutes'",
                (str(defaults.get("alert_cooldown_minutes", 360)),),
            )
        version = 2
    conn.execute(
        """
        INSERT INTO schema_meta(key, value) VALUES ('schema_version', ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (str(max(version, SCHEMA_VERSION)),),
    )


class ClimateDB:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_schema(self, defaults: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS measurements (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    temperature_c REAL NOT NULL,
                    humidity_pct REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_measurements_ts
                    ON measurements(ts);

                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS alert_state (
                    condition_key TEXT PRIMARY KEY,
                    breach_started_at TEXT,
                    last_alerted_at TEXT,
                    last_value REAL
                );

                CREATE TABLE IF NOT EXISTS outside_readings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    temperature_c REAL NOT NULL,
                    humidity_pct REAL NOT NULL,
                    dew_point_c REAL,
                    condition TEXT,
                    wind_kph REAL,
                    precipitation_mm REAL,
                    sources TEXT,
                    provider_count INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_outside_readings_ts
                    ON outside_readings(ts);

                -- Keyed by forecast hour so each poll refreshes the horizon
                -- in place instead of piling up duplicate rows.
                CREATE TABLE IF NOT EXISTS outside_forecast (
                    ts TEXT PRIMARY KEY,
                    fetched_at TEXT NOT NULL,
                    temperature_c REAL NOT NULL,
                    humidity_pct REAL,
                    condition TEXT,
                    precipitation_probability REAL,
                    wind_kph REAL,
                    provider_count INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS incidents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    metric TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    window_points INTEGER NOT NULL,
                    baseline_temp_c REAL NOT NULL,
                    baseline_humidity_pct REAL NOT NULL,
                    current_temp_c REAL NOT NULL,
                    current_humidity_pct REAL NOT NULL,
                    peak_temp_c REAL NOT NULL,
                    peak_humidity_pct REAL NOT NULL,
                    delta_temp_c REAL NOT NULL,
                    delta_humidity_pct REAL NOT NULL,
                    peak_delta_temp_c REAL NOT NULL,
                    peak_delta_humidity_pct REAL NOT NULL,
                    notified INTEGER NOT NULL DEFAULT 0,
                    notes TEXT NOT NULL DEFAULT '',
                    indoor_temp_c REAL,
                    indoor_humidity_pct REAL,
                    outdoor_temp_c REAL,
                    outdoor_humidity_pct REAL,
                    outdoor_condition TEXT,
                    outdoor_dew_point_c REAL
                );
                CREATE INDEX IF NOT EXISTS idx_incidents_started
                    ON incidents(started_at);
                CREATE INDEX IF NOT EXISTS idx_incidents_open
                    ON incidents(ended_at);

                CREATE TABLE IF NOT EXISTS incident_tags (
                    incident_id INTEGER NOT NULL,
                    tag TEXT NOT NULL,
                    PRIMARY KEY (incident_id, tag),
                    FOREIGN KEY (incident_id) REFERENCES incidents(id)
                );

                CREATE TABLE IF NOT EXISTS tag_catalog (
                    tag TEXT PRIMARY KEY
                );
                """
            )
            for key, value in defaults.items():
                conn.execute(
                    """
                    INSERT OR IGNORE INTO settings(key, value)
                    VALUES (?, ?)
                    """,
                    (key, str(value)),
                )
            for tag in DEFAULT_INCIDENT_TAGS:
                conn.execute(
                    "INSERT OR IGNORE INTO tag_catalog(tag) VALUES (?)",
                    (tag,),
                )
            _apply_schema_migrations(conn, defaults)

    def insert_measurement(
        self, temperature_c: float, humidity_pct: float, ts: datetime | None = None
    ) -> Measurement:
        when = ts or utc_now()
        iso = to_iso(when)
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO measurements(ts, temperature_c, humidity_pct)
                VALUES (?, ?, ?)
                """,
                (iso, temperature_c, humidity_pct),
            )
            row_id = int(cur.lastrowid)
        return Measurement(row_id, when, temperature_c, humidity_pct)

    def latest_measurement(self) -> Measurement | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT id, ts, temperature_c, humidity_pct
                FROM measurements
                ORDER BY ts DESC, id DESC
                LIMIT 1
                """
            ).fetchone()
        if row is None:
            return None
        return Measurement(
            int(row["id"]),
            parse_iso(row["ts"]),
            float(row["temperature_c"]),
            float(row["humidity_pct"]),
        )

    def list_measurements(
        self,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 5000,
    ) -> list[Measurement]:
        clauses: list[str] = []
        params: list[Any] = []
        if start is not None:
            clauses.append("ts >= ?")
            params.append(to_iso(start))
        if end is not None:
            clauses.append("ts <= ?")
            params.append(to_iso(end))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(limit, 100_000)))
        sql = f"""
            SELECT id, ts, temperature_c, humidity_pct
            FROM measurements
            {where}
            ORDER BY ts ASC
            LIMIT ?
        """
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            Measurement(
                int(r["id"]),
                parse_iso(r["ts"]),
                float(r["temperature_c"]),
                float(r["humidity_pct"]),
            )
            for r in rows
        ]

    def recent_measurements(self, limit: int) -> list[Measurement]:
        """Latest `limit` samples, returned oldest-first."""
        params = (max(1, min(limit, 100_000)),)
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, ts, temperature_c, humidity_pct
                FROM measurements
                ORDER BY ts DESC, id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        measurements = [
            Measurement(
                int(r["id"]),
                parse_iso(r["ts"]),
                float(r["temperature_c"]),
                float(r["humidity_pct"]),
            )
            for r in rows
        ]
        measurements.reverse()
        return measurements

    def stats(
        self, start: datetime | None = None, end: datetime | None = None
    ) -> dict[str, Any]:
        clauses: list[str] = []
        params: list[Any] = []
        if start is not None:
            clauses.append("ts >= ?")
            params.append(to_iso(start))
        if end is not None:
            clauses.append("ts <= ?")
            params.append(to_iso(end))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"""
            SELECT
                COUNT(*) AS count,
                MIN(temperature_c) AS temp_min,
                MAX(temperature_c) AS temp_max,
                AVG(temperature_c) AS temp_avg,
                MIN(humidity_pct) AS humidity_min,
                MAX(humidity_pct) AS humidity_max,
                AVG(humidity_pct) AS humidity_avg
            FROM measurements
            {where}
        """
        with self.connect() as conn:
            row = conn.execute(sql, params).fetchone()
        return {
            "count": int(row["count"] or 0),
            "temperature_c": {
                "min": row["temp_min"],
                "max": row["temp_max"],
                "avg": row["temp_avg"],
            },
            "humidity_pct": {
                "min": row["humidity_min"],
                "max": row["humidity_max"],
                "avg": row["humidity_avg"],
            },
        }

    # ------------------------------------------------------------------
    # Outside conditions
    # ------------------------------------------------------------------

    def insert_outside_reading(self, reading: OutsideReading) -> OutsideReading:
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO outside_readings(
                    ts, temperature_c, humidity_pct, dew_point_c, condition,
                    wind_kph, precipitation_mm, sources, provider_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    to_iso(reading.ts),
                    reading.temperature_c,
                    reading.humidity_pct,
                    reading.dew_point_c,
                    reading.condition,
                    reading.wind_kph,
                    reading.precipitation_mm,
                    ",".join(reading.sources),
                    reading.provider_count,
                ),
            )
            reading.id = int(cur.lastrowid)
        return reading

    def latest_outside_reading(self) -> OutsideReading | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM outside_readings
                ORDER BY ts DESC, id DESC
                LIMIT 1
                """
            ).fetchone()
        return None if row is None else _row_to_outside_reading(row)

    def list_outside_readings(
        self,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 5000,
    ) -> list[OutsideReading]:
        clauses: list[str] = []
        params: list[Any] = []
        if start is not None:
            clauses.append("ts >= ?")
            params.append(to_iso(start))
        if end is not None:
            clauses.append("ts <= ?")
            params.append(to_iso(end))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(limit, 100_000)))
        sql = f"""
            SELECT * FROM outside_readings
            {where}
            ORDER BY ts ASC
            LIMIT ?
        """
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_outside_reading(r) for r in rows]

    def upsert_forecast(
        self, points: Sequence[ForecastPoint], fetched_at: datetime | None = None
    ) -> None:
        stamp = to_iso(fetched_at or utc_now())
        with self.connect() as conn:
            conn.executemany(
                """
                INSERT INTO outside_forecast(
                    ts, fetched_at, temperature_c, humidity_pct, condition,
                    precipitation_probability, wind_kph, provider_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ts) DO UPDATE SET
                    fetched_at = excluded.fetched_at,
                    temperature_c = excluded.temperature_c,
                    humidity_pct = excluded.humidity_pct,
                    condition = excluded.condition,
                    precipitation_probability = excluded.precipitation_probability,
                    wind_kph = excluded.wind_kph,
                    provider_count = excluded.provider_count
                """,
                [
                    (
                        to_iso(p.ts),
                        stamp,
                        p.temperature_c,
                        p.humidity_pct,
                        p.condition,
                        p.precipitation_probability,
                        p.wind_kph,
                        p.provider_count,
                    )
                    for p in points
                ],
            )

    def list_forecast(
        self, start: datetime | None = None, end: datetime | None = None
    ) -> list[ForecastPoint]:
        clauses: list[str] = []
        params: list[Any] = []
        if start is not None:
            clauses.append("ts >= ?")
            params.append(to_iso(start))
        if end is not None:
            clauses.append("ts <= ?")
            params.append(to_iso(end))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM outside_forecast {where} ORDER BY ts ASC", params
            ).fetchall()
        return [
            ForecastPoint(
                ts=parse_iso(r["ts"]),
                temperature_c=float(r["temperature_c"]),
                humidity_pct=r["humidity_pct"],
                condition=r["condition"],
                precipitation_probability=r["precipitation_probability"],
                wind_kph=r["wind_kph"],
                provider_count=int(r["provider_count"] or 0),
                fetched_at=parse_iso(r["fetched_at"]) if r["fetched_at"] else None,
            )
            for r in rows
        ]

    def prune_forecast(self, before: datetime) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM outside_forecast WHERE ts < ?", (to_iso(before),))

    def get_settings(self) -> dict[str, str]:
        with self.connect() as conn:
            rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return {str(r["key"]): str(r["value"]) for r in rows}

    def update_settings(self, updates: dict[str, Any]) -> dict[str, str]:
        with self.connect() as conn:
            for key, value in updates.items():
                if key not in SETTING_KEYS:
                    continue
                conn.execute(
                    """
                    INSERT INTO settings(key, value) VALUES (?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (key, str(value)),
                )
        return self.get_settings()

    def get_alert_state(self, condition_key: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT condition_key, breach_started_at, last_alerted_at, last_value
                FROM alert_state
                WHERE condition_key = ?
                """,
                (condition_key,),
            ).fetchone()
        if row is None:
            return None
        return {
            "condition_key": row["condition_key"],
            "breach_started_at": row["breach_started_at"],
            "last_alerted_at": row["last_alerted_at"],
            "last_value": row["last_value"],
        }

    def upsert_alert_state(
        self,
        condition_key: str,
        breach_started_at: str | None,
        last_alerted_at: str | None,
        last_value: float | None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO alert_state(
                    condition_key, breach_started_at, last_alerted_at, last_value
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(condition_key) DO UPDATE SET
                    breach_started_at = excluded.breach_started_at,
                    last_alerted_at = excluded.last_alerted_at,
                    last_value = excluded.last_value
                """,
                (condition_key, breach_started_at, last_alerted_at, last_value),
            )

    def clear_alert_breach(self, condition_key: str) -> None:
        state = self.get_alert_state(condition_key)
        last_alerted = state["last_alerted_at"] if state else None
        self.upsert_alert_state(condition_key, None, last_alerted, None)

    # ------------------------------------------------------------------
    # Climate-change incidents
    # ------------------------------------------------------------------

    def _tags_for_incidents(
        self, conn: sqlite3.Connection, incident_ids: Sequence[int]
    ) -> dict[int, list[str]]:
        if not incident_ids:
            return {}
        placeholders = ",".join("?" * len(incident_ids))
        rows = conn.execute(
            f"""
            SELECT incident_id, tag FROM incident_tags
            WHERE incident_id IN ({placeholders})
            ORDER BY tag ASC
            """,
            tuple(incident_ids),
        ).fetchall()
        grouped: dict[int, list[str]] = {int(i): [] for i in incident_ids}
        for row in rows:
            grouped[int(row["incident_id"])].append(str(row["tag"]))
        return grouped

    def insert_incident(self, incident: Incident) -> Incident:
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO incidents(
                    started_at, ended_at, metric, direction, window_points,
                    baseline_temp_c, baseline_humidity_pct,
                    current_temp_c, current_humidity_pct,
                    peak_temp_c, peak_humidity_pct,
                    delta_temp_c, delta_humidity_pct,
                    peak_delta_temp_c, peak_delta_humidity_pct,
                    notified, notes,
                    indoor_temp_c, indoor_humidity_pct,
                    outdoor_temp_c, outdoor_humidity_pct,
                    outdoor_condition, outdoor_dew_point_c
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    to_iso(incident.started_at),
                    to_iso(incident.ended_at) if incident.ended_at else None,
                    incident.metric,
                    incident.direction,
                    incident.window_points,
                    incident.baseline_temp_c,
                    incident.baseline_humidity_pct,
                    incident.current_temp_c,
                    incident.current_humidity_pct,
                    incident.peak_temp_c,
                    incident.peak_humidity_pct,
                    incident.delta_temp_c,
                    incident.delta_humidity_pct,
                    incident.peak_delta_temp_c,
                    incident.peak_delta_humidity_pct,
                    1 if incident.notified else 0,
                    incident.notes,
                    incident.indoor_temp_c,
                    incident.indoor_humidity_pct,
                    incident.outdoor_temp_c,
                    incident.outdoor_humidity_pct,
                    incident.outdoor_condition,
                    incident.outdoor_dew_point_c,
                ),
            )
            incident.id = int(cur.lastrowid)
        return incident

    def update_incident(self, incident_id: int, **fields: Any) -> None:
        allowed = {
            "metric",
            "direction",
            "current_temp_c",
            "current_humidity_pct",
            "peak_temp_c",
            "peak_humidity_pct",
            "delta_temp_c",
            "delta_humidity_pct",
            "peak_delta_temp_c",
            "peak_delta_humidity_pct",
            "notified",
            "notes",
            "ended_at",
        }
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return
        assignments: list[str] = []
        params: list[Any] = []
        for key, value in updates.items():
            assignments.append(f"{key} = ?")
            if key == "ended_at" and isinstance(value, datetime):
                params.append(to_iso(value))
            elif key == "notified":
                params.append(1 if value else 0)
            else:
                params.append(value)
        params.append(incident_id)
        with self.connect() as conn:
            conn.execute(
                f"UPDATE incidents SET {', '.join(assignments)} WHERE id = ?",
                params,
            )

    def close_incident(self, incident_id: int, ended_at: datetime | None = None) -> None:
        self.update_incident(incident_id, ended_at=ended_at or utc_now())

    def mark_incident_notified(self, incident_id: int) -> None:
        self.update_incident(incident_id, notified=True)

    def get_incident(self, incident_id: int) -> Incident | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM incidents WHERE id = ?", (incident_id,)
            ).fetchone()
            if row is None:
                return None
            tags = self._tags_for_incidents(conn, [incident_id]).get(incident_id, [])
        return _row_to_incident(row, tags)

    def get_open_incident(self) -> Incident | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM incidents
                WHERE ended_at IS NULL
                ORDER BY started_at DESC, id DESC
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None
            tags = self._tags_for_incidents(conn, [int(row["id"])]).get(int(row["id"]), [])
        return _row_to_incident(row, tags)

    def latest_notified_incident(self) -> Incident | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM incidents
                WHERE notified = 1
                ORDER BY started_at DESC, id DESC
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None
            tags = self._tags_for_incidents(conn, [int(row["id"])]).get(int(row["id"]), [])
        return _row_to_incident(row, tags)

    def list_incidents(
        self,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 200,
        open_only: bool = False,
    ) -> list[Incident]:
        clauses: list[str] = []
        params: list[Any] = []
        if start is not None:
            clauses.append("started_at >= ?")
            params.append(to_iso(start))
        if end is not None:
            clauses.append("started_at <= ?")
            params.append(to_iso(end))
        if open_only:
            clauses.append("ended_at IS NULL")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(limit, 10_000)))
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM incidents
                {where}
                ORDER BY started_at DESC, id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
            ids = [int(r["id"]) for r in rows]
            tags = self._tags_for_incidents(conn, ids)
        return [_row_to_incident(r, tags.get(int(r["id"]), [])) for r in rows]

    def set_incident_tags(self, incident_id: int, tags: Sequence[str]) -> Incident | None:
        cleaned: list[str] = []
        seen: set[str] = set()
        for raw in tags:
            tag = " ".join(str(raw).split())
            if not tag or tag in seen:
                continue
            seen.add(tag)
            cleaned.append(tag)
        with self.connect() as conn:
            exists = conn.execute(
                "SELECT id FROM incidents WHERE id = ?", (incident_id,)
            ).fetchone()
            if exists is None:
                return None
            conn.execute(
                "DELETE FROM incident_tags WHERE incident_id = ?", (incident_id,)
            )
            for tag in cleaned:
                conn.execute(
                    "INSERT OR IGNORE INTO tag_catalog(tag) VALUES (?)", (tag,)
                )
                conn.execute(
                    """
                    INSERT INTO incident_tags(incident_id, tag) VALUES (?, ?)
                    """,
                    (incident_id, tag),
                )
        return self.get_incident(incident_id)

    def set_incident_notes(self, incident_id: int, notes: str) -> Incident | None:
        if self.get_incident(incident_id) is None:
            return None
        self.update_incident(incident_id, notes=notes)
        return self.get_incident(incident_id)

    def list_tag_catalog(self) -> list[str]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT tag FROM tag_catalog ORDER BY tag ASC"
            ).fetchall()
        tags = [str(r["tag"]) for r in rows]
        # Keep the suggested labels first, then any custom ones.
        preferred = [tag for tag in DEFAULT_INCIDENT_TAGS if tag in tags]
        extra = [tag for tag in tags if tag not in set(DEFAULT_INCIDENT_TAGS)]
        return preferred + extra
