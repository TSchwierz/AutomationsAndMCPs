"""Outside conditions from three weather services, averaged into one reading.

Open-Meteo, OpenWeatherMap and Bright Sky (DWD) are polled in parallel and
their answers combined, so a single flaky or offline service degrades the
result instead of losing it.
"""

from __future__ import annotations

import logging
import math
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Sequence

import httpx

from .db import ClimateDB, ForecastPoint, OutsideReading, utc_now

logger = logging.getLogger(__name__)

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
OWM_CURRENT_URL = "https://api.openweathermap.org/data/2.5/weather"
OWM_FORECAST_URL = "https://api.openweathermap.org/data/2.5/forecast"
BRIGHTSKY_CURRENT_URL = "https://api.brightsky.dev/current_weather"
BRIGHTSKY_WEATHER_URL = "https://api.brightsky.dev/weather"

# Shared condition vocabulary, ordered from calm to disruptive. Aggregation
# uses this order to break ties, and the dashboard uses it to decide whether an
# upcoming hour counts as a change worth showing.
CONDITION_SEVERITY: dict[str, int] = {
    "clear": 0,
    "partly_cloudy": 1,
    "cloudy": 2,
    "fog": 3,
    "drizzle": 4,
    "rain": 5,
    "sleet": 6,
    "snow": 7,
    "heavy_rain": 8,
    "hail": 9,
    "thunderstorm": 10,
}

# Beaufort 6 is the point where a window can start slamming; 62 km/h is the
# German "Sturm" threshold (Beaufort 8).
WINDY_KPH = 39.0
STORMY_KPH = 62.0

_MAGNUS_A = 17.62
_MAGNUS_B = 243.12


def dew_point_c(temperature_c: float, humidity_pct: float) -> float | None:
    """Dew point via the Magnus-Tetens approximation."""
    if humidity_pct <= 0 or humidity_pct > 100:
        return None
    gamma = math.log(humidity_pct / 100.0) + (
        _MAGNUS_A * temperature_c / (_MAGNUS_B + temperature_c)
    )
    denominator = _MAGNUS_A - gamma
    if denominator == 0:
        return None
    return round(_MAGNUS_B * gamma / denominator, 2)


def humidity_from_dew_point(temperature_c: float, dew_point: float) -> float | None:
    """Inverse of :func:`dew_point_c`, for services that only report dew point."""
    try:
        ratio = math.exp(
            (_MAGNUS_A * dew_point / (_MAGNUS_B + dew_point))
            - (_MAGNUS_A * temperature_c / (_MAGNUS_B + temperature_c))
        )
    except (OverflowError, ValueError, ZeroDivisionError):
        return None
    return round(max(0.0, min(100.0, ratio * 100.0)), 1)


def _floor_hour(moment: datetime) -> datetime:
    return moment.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


def _mean(values: Iterable[float | None]) -> float | None:
    present = [float(v) for v in values if v is not None]
    if not present:
        return None
    return sum(present) / len(present)


def _round(value: float | None, digits: int) -> float | None:
    return None if value is None else round(value, digits)


def _vote_condition(conditions: Iterable[str | None]) -> str | None:
    """Majority vote across services; the more disruptive one wins a tie."""
    present = [c for c in conditions if c]
    if not present:
        return None
    counts: dict[str, int] = {}
    for condition in present:
        counts[condition] = counts.get(condition, 0) + 1
    return max(counts, key=lambda c: (counts[c], CONDITION_SEVERITY.get(c, 0)))


def wind_label(wind_kph: float | None) -> str | None:
    if wind_kph is None:
        return None
    if wind_kph >= STORMY_KPH:
        return "stormy"
    if wind_kph >= WINDY_KPH:
        return "windy"
    return None


# --------------------------------------------------------------------------
# Per-service condition mapping
# --------------------------------------------------------------------------

_WMO_CONDITIONS: dict[int, str] = {
    0: "clear",
    1: "clear",
    2: "partly_cloudy",
    3: "cloudy",
    45: "fog",
    48: "fog",
    51: "drizzle",
    53: "drizzle",
    55: "drizzle",
    56: "sleet",
    57: "sleet",
    61: "rain",
    63: "rain",
    65: "heavy_rain",
    66: "sleet",
    67: "sleet",
    71: "snow",
    73: "snow",
    75: "snow",
    77: "snow",
    80: "rain",
    81: "rain",
    82: "heavy_rain",
    85: "snow",
    86: "snow",
    95: "thunderstorm",
    96: "thunderstorm",
    99: "thunderstorm",
}

_BRIGHTSKY_ICONS: dict[str, str] = {
    "clear-day": "clear",
    "clear-night": "clear",
    "partly-cloudy-day": "partly_cloudy",
    "partly-cloudy-night": "partly_cloudy",
    "cloudy": "cloudy",
    "fog": "fog",
    "wind": "cloudy",
    "rain": "rain",
    "sleet": "sleet",
    "snow": "snow",
    "hail": "hail",
    "thunderstorm": "thunderstorm",
}


def _from_wmo_code(code: Any) -> str | None:
    try:
        return _WMO_CONDITIONS.get(int(code))
    except (TypeError, ValueError):
        return None


def _from_owm_id(weather: Sequence[dict[str, Any]] | None) -> str | None:
    """Map an OpenWeatherMap condition id (https://openweathermap.org/weather-conditions)."""
    if not weather:
        return None
    try:
        code = int(weather[0].get("id"))
    except (TypeError, ValueError, IndexError, AttributeError):
        return None
    if 200 <= code < 300:
        return "thunderstorm"
    if 300 <= code < 400:
        return "drizzle"
    if code in (502, 503, 504, 522, 531):
        return "heavy_rain"
    if code in (511, 611, 612, 613, 615, 616):
        return "sleet"
    if 500 <= code < 600:
        return "rain"
    if 600 <= code < 700:
        return "snow"
    if 700 <= code < 800:
        return "fog"
    if code == 800:
        return "clear"
    if code in (801, 802):
        return "partly_cloudy"
    if code in (803, 804):
        return "cloudy"
    return None


def _from_cloud_cover(cloud_cover: Any) -> str | None:
    try:
        cover = float(cloud_cover)
    except (TypeError, ValueError):
        return None
    if cover < 25:
        return "clear"
    if cover < 70:
        return "partly_cloudy"
    return "cloudy"


def _from_brightsky(record: dict[str, Any]) -> str | None:
    condition = record.get("condition")
    # `condition` is authoritative for precipitation but says nothing about
    # cloud cover, so a "dry" hour falls back to the icon or the cloud reading.
    if condition and condition != "dry":
        mapped = {
            "fog": "fog",
            "rain": "rain",
            "sleet": "sleet",
            "snow": "snow",
            "hail": "hail",
            "thunderstorm": "thunderstorm",
        }.get(str(condition))
        if mapped:
            return mapped
    icon = _BRIGHTSKY_ICONS.get(str(record.get("icon")))
    return icon or _from_cloud_cover(record.get("cloud_cover"))


def _first_number(record: dict[str, Any], keys: Sequence[str]) -> float | None:
    for key in keys:
        value = record.get(key)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return None


# --------------------------------------------------------------------------
# Provider results
# --------------------------------------------------------------------------


@dataclass
class ProviderSnapshot:
    """One service's answer: current conditions, an hourly outlook, or an error."""

    name: str
    current: OutsideReading | None = None
    forecast: list[ForecastPoint] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and (self.current is not None or bool(self.forecast))


@dataclass
class WeatherSnapshot:
    reading: OutsideReading | None
    forecast: list[ForecastPoint]
    providers: list[dict[str, Any]]


def _reading(
    *,
    source: str,
    ts: datetime,
    temperature_c: float | None,
    humidity_pct: float | None,
    condition: str | None,
    wind_kph: float | None,
    precipitation_mm: float | None,
    dew_point: float | None = None,
) -> OutsideReading | None:
    if temperature_c is None:
        return None
    if humidity_pct is None and dew_point is not None:
        humidity_pct = humidity_from_dew_point(temperature_c, dew_point)
    if humidity_pct is None:
        return None
    if dew_point is None:
        dew_point = dew_point_c(temperature_c, humidity_pct)
    return OutsideReading(
        ts=ts,
        temperature_c=round(float(temperature_c), 2),
        humidity_pct=round(float(humidity_pct), 1),
        dew_point_c=dew_point,
        condition=condition,
        wind_kph=_round(wind_kph, 1),
        precipitation_mm=_round(precipitation_mm, 2),
        sources=[source],
        provider_count=1,
    )


def _forecast_point(
    *,
    ts: datetime,
    temperature_c: float | None,
    humidity_pct: float | None,
    condition: str | None,
    precipitation_probability: float | None,
    wind_kph: float | None,
    dew_point: float | None = None,
) -> ForecastPoint | None:
    if temperature_c is None:
        return None
    if humidity_pct is None and dew_point is not None:
        humidity_pct = humidity_from_dew_point(temperature_c, dew_point)
    return ForecastPoint(
        ts=_floor_hour(ts),
        temperature_c=round(float(temperature_c), 2),
        humidity_pct=_round(humidity_pct, 1),
        condition=condition,
        precipitation_probability=_round(precipitation_probability, 0),
        wind_kph=_round(wind_kph, 1),
        provider_count=1,
    )


# --------------------------------------------------------------------------
# Providers
# --------------------------------------------------------------------------


def fetch_open_meteo(
    client: httpx.Client, latitude: float, longitude: float, forecast_hours: int
) -> ProviderSnapshot:
    snapshot = ProviderSnapshot(name="open_meteo")
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "current": "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m,precipitation",
        "hourly": (
            "temperature_2m,relative_humidity_2m,precipitation_probability,"
            "weather_code,wind_speed_10m"
        ),
        "forecast_hours": max(1, min(forecast_hours, 24)),
        "timezone": "UTC",
    }
    payload = client.get(OPEN_METEO_URL, params=params).raise_for_status().json()

    current = payload.get("current") or {}
    if current:
        snapshot.current = _reading(
            source="open_meteo",
            ts=_parse_naive_utc(current.get("time")) or utc_now(),
            temperature_c=current.get("temperature_2m"),
            humidity_pct=current.get("relative_humidity_2m"),
            condition=_from_wmo_code(current.get("weather_code")),
            wind_kph=current.get("wind_speed_10m"),
            precipitation_mm=current.get("precipitation"),
        )

    hourly = payload.get("hourly") or {}
    times = hourly.get("time") or []
    for index, raw_time in enumerate(times):
        ts = _parse_naive_utc(raw_time)
        if ts is None:
            continue
        point = _forecast_point(
            ts=ts,
            temperature_c=_at(hourly.get("temperature_2m"), index),
            humidity_pct=_at(hourly.get("relative_humidity_2m"), index),
            condition=_from_wmo_code(_at(hourly.get("weather_code"), index)),
            precipitation_probability=_at(hourly.get("precipitation_probability"), index),
            wind_kph=_at(hourly.get("wind_speed_10m"), index),
        )
        if point:
            snapshot.forecast.append(point)
    return snapshot


def fetch_openweathermap(
    client: httpx.Client,
    latitude: float,
    longitude: float,
    api_key: str,
    forecast_hours: int,
) -> ProviderSnapshot:
    snapshot = ProviderSnapshot(name="openweathermap")
    if not api_key:
        snapshot.error = "no api key configured"
        return snapshot

    base = {"lat": latitude, "lon": longitude, "units": "metric", "appid": api_key}
    current = client.get(OWM_CURRENT_URL, params=base).raise_for_status().json()
    main = current.get("main") or {}
    snapshot.current = _reading(
        source="openweathermap",
        ts=_parse_unix(current.get("dt")) or utc_now(),
        temperature_c=main.get("temp"),
        humidity_pct=main.get("humidity"),
        condition=_from_owm_id(current.get("weather")),
        wind_kph=_mps_to_kph((current.get("wind") or {}).get("speed")),
        precipitation_mm=(current.get("rain") or {}).get("1h"),
    )

    # The free forecast is in 3-hour steps, so ask for enough steps to cover
    # the horizon and let the hourly averaging place them where they land.
    steps = max(1, min(-(-forecast_hours // 3) + 1, 40))
    forecast = (
        client.get(OWM_FORECAST_URL, params={**base, "cnt": steps})
        .raise_for_status()
        .json()
    )
    for entry in forecast.get("list") or []:
        ts = _parse_unix(entry.get("dt"))
        if ts is None:
            continue
        entry_main = entry.get("main") or {}
        pop = entry.get("pop")
        point = _forecast_point(
            ts=ts,
            temperature_c=entry_main.get("temp"),
            humidity_pct=entry_main.get("humidity"),
            condition=_from_owm_id(entry.get("weather")),
            precipitation_probability=None if pop is None else float(pop) * 100.0,
            wind_kph=_mps_to_kph((entry.get("wind") or {}).get("speed")),
        )
        if point:
            snapshot.forecast.append(point)
    return snapshot


def fetch_brightsky(
    client: httpx.Client, latitude: float, longitude: float, forecast_hours: int
) -> ProviderSnapshot:
    snapshot = ProviderSnapshot(name="brightsky")
    location = {"lat": latitude, "lon": longitude}

    current = (
        client.get(BRIGHTSKY_CURRENT_URL, params=location).raise_for_status().json()
    )
    record = current.get("weather") or {}
    if record:
        snapshot.current = _reading(
            source="brightsky",
            ts=_parse_iso_aware(record.get("timestamp")) or utc_now(),
            temperature_c=record.get("temperature"),
            humidity_pct=record.get("relative_humidity"),
            condition=_from_brightsky(record),
            # SYNOP observations arrive as 10/30/60-minute windows.
            wind_kph=_first_number(record, ("wind_speed_10", "wind_speed_30", "wind_speed_60")),
            precipitation_mm=_first_number(
                record, ("precipitation_60", "precipitation_30", "precipitation_10")
            ),
            dew_point=record.get("dew_point"),
        )

    start = _floor_hour(utc_now())
    forecast = (
        client.get(
            BRIGHTSKY_WEATHER_URL,
            params={
                **location,
                "date": start.isoformat(),
                "last_date": (start + timedelta(hours=forecast_hours)).isoformat(),
            },
        )
        .raise_for_status()
        .json()
    )
    for entry in forecast.get("weather") or []:
        ts = _parse_iso_aware(entry.get("timestamp"))
        if ts is None:
            continue
        # MOSMIX forecasts leave relative_humidity null but do carry dew point.
        point = _forecast_point(
            ts=ts,
            temperature_c=entry.get("temperature"),
            humidity_pct=entry.get("relative_humidity"),
            condition=_from_brightsky(entry),
            precipitation_probability=entry.get("precipitation_probability"),
            wind_kph=entry.get("wind_speed"),
            dew_point=entry.get("dew_point"),
        )
        if point:
            snapshot.forecast.append(point)
    return snapshot


def _at(values: Sequence[Any] | None, index: int) -> Any:
    if not values or index >= len(values):
        return None
    return values[index]


def _mps_to_kph(value: Any) -> float | None:
    try:
        return float(value) * 3.6
    except (TypeError, ValueError):
        return None


def _parse_naive_utc(value: Any) -> datetime | None:
    """Parse Open-Meteo's `2026-09-02T19:00` (already UTC when timezone=UTC)."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _parse_iso_aware(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_unix(value: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


# --------------------------------------------------------------------------
# Combining
# --------------------------------------------------------------------------


def combine_readings(snapshots: Sequence[ProviderSnapshot]) -> OutsideReading | None:
    readings = [s.current for s in snapshots if s.current is not None]
    if not readings:
        return None
    temperature = _mean(r.temperature_c for r in readings)
    humidity = _mean(r.humidity_pct for r in readings)
    if temperature is None or humidity is None:
        return None
    return OutsideReading(
        ts=utc_now(),
        temperature_c=round(temperature, 2),
        humidity_pct=round(humidity, 1),
        dew_point_c=_round(_mean(r.dew_point_c for r in readings), 2),
        condition=_vote_condition(r.condition for r in readings),
        wind_kph=_round(_mean(r.wind_kph for r in readings), 1),
        precipitation_mm=_round(_mean(r.precipitation_mm for r in readings), 2),
        sources=sorted(s for r in readings for s in r.sources),
        provider_count=len(readings),
    )


def combine_forecasts(snapshots: Sequence[ProviderSnapshot]) -> list[ForecastPoint]:
    """Average each service's outlook into one series, bucketed by the hour."""
    buckets: dict[datetime, list[ForecastPoint]] = {}
    for snapshot in snapshots:
        for point in snapshot.forecast:
            buckets.setdefault(point.ts, []).append(point)

    combined: list[ForecastPoint] = []
    for ts in sorted(buckets):
        points = buckets[ts]
        temperature = _mean(p.temperature_c for p in points)
        if temperature is None:
            continue
        combined.append(
            ForecastPoint(
                ts=ts,
                temperature_c=round(temperature, 2),
                humidity_pct=_round(_mean(p.humidity_pct for p in points), 1),
                condition=_vote_condition(p.condition for p in points),
                precipitation_probability=_round(
                    _mean(p.precipitation_probability for p in points), 0
                ),
                wind_kph=_round(_mean(p.wind_kph for p in points), 1),
                provider_count=len(points),
            )
        )
    return combined


# --------------------------------------------------------------------------
# Service
# --------------------------------------------------------------------------


class WeatherService:
    """Polls the services, stores the combined result, and serves cached reads."""

    def __init__(
        self,
        db: ClimateDB,
        *,
        latitude: float,
        longitude: float,
        openweathermap_api_key: str = "",
        forecast_hours: int = 12,
        request_timeout_seconds: float = 10.0,
        alert_max_age_minutes: int = 30,
    ) -> None:
        self.db = db
        self.latitude = latitude
        self.longitude = longitude
        self.openweathermap_api_key = openweathermap_api_key
        self.forecast_hours = forecast_hours
        self.request_timeout_seconds = request_timeout_seconds
        self.alert_max_age = timedelta(minutes=alert_max_age_minutes)

    @property
    def provider_names(self) -> list[str]:
        names = ["open_meteo", "brightsky"]
        if self.openweathermap_api_key:
            names.insert(1, "openweathermap")
        return names

    def _tasks(
        self, client: httpx.Client
    ) -> list[tuple[str, Callable[[], ProviderSnapshot]]]:
        return [
            (
                "open_meteo",
                lambda: fetch_open_meteo(
                    client, self.latitude, self.longitude, self.forecast_hours
                ),
            ),
            (
                "openweathermap",
                lambda: fetch_openweathermap(
                    client,
                    self.latitude,
                    self.longitude,
                    self.openweathermap_api_key,
                    self.forecast_hours,
                ),
            ),
            (
                "brightsky",
                lambda: fetch_brightsky(
                    client, self.latitude, self.longitude, self.forecast_hours
                ),
            ),
        ]

    def poll(self) -> list[ProviderSnapshot]:
        """Query every service in parallel; never raises."""
        timeout = httpx.Timeout(self.request_timeout_seconds)
        snapshots: list[ProviderSnapshot] = []
        with httpx.Client(timeout=timeout, headers={"User-Agent": "studio-climate/0.1"}) as client:
            tasks = self._tasks(client)
            with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
                futures = [(name, pool.submit(fn)) for name, fn in tasks]
                for name, future in futures:
                    try:
                        snapshots.append(future.result())
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("Weather provider %s failed: %s", name, exc)
                        snapshots.append(ProviderSnapshot(name=name, error=str(exc)))
        return snapshots

    def refresh(self) -> WeatherSnapshot:
        """Poll, average, and persist. Returns whatever could be gathered."""
        snapshots = self.poll()
        reading = combine_readings(snapshots)
        forecast = combine_forecasts(snapshots)

        if reading is not None:
            self.db.insert_outside_reading(reading)
        if forecast:
            self.db.upsert_forecast(forecast)
            self.db.prune_forecast(before=utc_now() - timedelta(hours=3))

        ok = [s.name for s in snapshots if s.ok]
        failed = [s.name for s in snapshots if not s.ok]
        if reading is None:
            logger.warning("Weather refresh got no usable reading (failed: %s)", failed)
        else:
            logger.info(
                "Outside %.1f°C RH=%.1f%% (%s) from %s",
                reading.temperature_c,
                reading.humidity_pct,
                reading.condition or "unknown",
                ", ".join(ok),
            )
        return WeatherSnapshot(
            reading=reading,
            forecast=forecast,
            providers=[
                {"name": s.name, "ok": s.ok, "error": s.error} for s in snapshots
            ],
        )

    def reading_for_alert(self) -> OutsideReading | None:
        """Latest stored reading, refreshed first if it is too old to be useful."""
        stored = self.db.latest_outside_reading()
        if stored is not None and utc_now() - stored.ts <= self.alert_max_age:
            return stored
        try:
            refreshed = self.refresh().reading
        except Exception:  # noqa: BLE001
            logger.exception("Weather refresh for alert failed")
            return stored
        return refreshed or stored


# --------------------------------------------------------------------------
# Indoor/outdoor comparison
# --------------------------------------------------------------------------

# Below this the two dew points are too close to call, given sensor accuracy.
DEW_POINT_MARGIN_C = 1.0


def ventilation_advice(
    indoor_dew_point: float | None, outside_dew_point: float | None
) -> dict[str, Any]:
    """Whether airing out would move moisture in or out of the room."""
    if indoor_dew_point is None or outside_dew_point is None:
        return {"action": "unknown", "delta_c": None, "reason": "No outside data yet."}
    delta = round(outside_dew_point - indoor_dew_point, 2)
    if delta <= -DEW_POINT_MARGIN_C:
        return {
            "action": "ventilate",
            "delta_c": delta,
            "reason": (
                f"Outside air is drier — dew point {abs(delta):.1f}°C below inside. "
                "Airing out removes moisture."
            ),
        }
    if delta >= DEW_POINT_MARGIN_C:
        return {
            "action": "keep_closed",
            "delta_c": delta,
            "reason": (
                f"Outside air is wetter — dew point {delta:.1f}°C above inside. "
                "Airing out would add moisture."
            ),
        }
    return {
        "action": "neutral",
        "delta_c": delta,
        "reason": "Inside and outside dew points are about equal; airing out changes little.",
    }


def summarize_next_hours(
    forecast: Sequence[ForecastPoint],
    reading: OutsideReading | None,
    hours: int = 4,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Condense the next `hours` of the outlook into one expected-change summary."""
    moment = now or utc_now()
    horizon_end = moment + timedelta(hours=hours)
    # Keep the hour we are currently inside, so a change an hour out is visible.
    points = [p for p in forecast if p.ts >= _floor_hour(moment) and p.ts <= horizon_end]
    if not points:
        return None

    baseline = reading.condition if reading and reading.condition else points[0].condition
    baseline_severity = CONDITION_SEVERITY.get(baseline or "", 0)

    peak = max(
        points, key=lambda p: CONDITION_SEVERITY.get(p.condition or "", 0)
    )
    change_at = next(
        (
            p
            for p in points
            if CONDITION_SEVERITY.get(p.condition or "", 0) > baseline_severity
        ),
        None,
    )

    temperatures = [p.temperature_c for p in points]
    humidities = [p.humidity_pct for p in points if p.humidity_pct is not None]
    winds = [p.wind_kph for p in points if p.wind_kph is not None]
    pops = [
        p.precipitation_probability
        for p in points
        if p.precipitation_probability is not None
    ]
    wind_max = max(winds) if winds else None

    start_temp = reading.temperature_c if reading else temperatures[0]
    summary = {
        "from": points[0].ts,
        "to": points[-1].ts,
        "condition_now": baseline,
        "condition_peak": peak.condition,
        "changing": change_at is not None,
        "change_at": change_at.ts if change_at else None,
        "temperature_c": {
            "min": round(min(temperatures), 1),
            "max": round(max(temperatures), 1),
            "delta": round(temperatures[-1] - start_temp, 1),
        },
        "humidity_pct": {
            "min": round(min(humidities), 1) if humidities else None,
            "max": round(max(humidities), 1) if humidities else None,
        },
        "precipitation_probability_max": max(pops) if pops else None,
        "wind_kph_max": wind_max,
        "wind_label": wind_label(wind_max),
    }
    summary["headline"] = _headline(summary, hours)
    return summary


_CONDITION_WORDS: dict[str, str] = {
    "clear": "clear",
    "partly_cloudy": "partly cloudy",
    "cloudy": "cloudy",
    "fog": "fog",
    "drizzle": "drizzle",
    "rain": "rain",
    "heavy_rain": "heavy rain",
    "sleet": "sleet",
    "snow": "snow",
    "hail": "hail",
    "thunderstorm": "thunderstorm",
}


def condition_label(condition: str | None) -> str:
    return _CONDITION_WORDS.get(condition or "", "unknown")


def _headline(summary: dict[str, Any], hours: int) -> str:
    peak = summary["condition_peak"]
    change_at: datetime | None = summary["change_at"]
    wind = summary["wind_label"]

    if change_at is not None and peak:
        when = change_at.astimezone().strftime("%H:%M")
        text = f"{condition_label(peak).capitalize()} expected around {when}"
    else:
        text = f"Staying {condition_label(summary['condition_now'])} for the next {hours} h"

    delta = summary["temperature_c"]["delta"]
    if abs(delta) >= 2:
        text += f", {'warming' if delta > 0 else 'cooling'} {abs(delta):.0f}°C"
    if wind == "stormy":
        text += ", storm-force gusts"
    elif wind == "windy":
        text += ", turning windy"
    return text
