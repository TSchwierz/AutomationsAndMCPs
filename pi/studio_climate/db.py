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
    "ntfy_server",
    "ntfy_topic",
    "ntfy_token",
    "sample_interval_seconds",
    "quiet_hours_enabled",
    "quiet_hours_start",
    "quiet_hours_end",
    "quiet_hours_timezone",
)


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
