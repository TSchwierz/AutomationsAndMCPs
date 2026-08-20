"""
MCP server that exposes weather-forecast tools backed by the Open-Meteo API.

Run it directly to speak MCP over stdio:
    python mcp_server/weather_server.py

A client (see bot/packing_advisor.py) spawns this file as a subprocess and
talks to it over stdio using the MCP protocol - it never imports these
functions directly. That's the whole point of MCP: the server is a
standalone process with its own tool definitions, and any MCP-aware client
can discover and call those tools without knowing your code.
"""

from datetime import datetime, timezone

import openmeteo_requests
import pandas as pd
import requests_cache
from mcp.server import MCPServer
from retry_requests import retry

# --- MCP server instance -----------------------------------------------
mcp = MCPServer("Weather")

# --- Static config --------------------------------------------------------
COORDINATES = {
    "Nijmegen": (51.8425593893818, 5.854625564074581),
    "Den Bosch": (51.69597345232003, 5.304554239967503),
    "Kerpen": (50.91634119102821, 6.711334903326844),
}

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
HOURLY_VARS = ["temperature_2m", "apparent_temperature", "precipitation", "weather_code"]

# --- Open-Meteo client (module scope, built once) --------------------------
# This was previously only created inside `if __name__ == "__main__"`, which
# meant the tool functions below would crash with a NameError as soon as
# anything actually called them - `openmeteo` didn't exist yet at that point.
_cache_session = requests_cache.CachedSession(".cache", expire_after=3600)
_retry_session = retry(_cache_session, retries=5, backoff_factor=0.2)
openmeteo = openmeteo_requests.Client(session=_retry_session)


def _params_for(city: str, days: int) -> dict:
    if city not in COORDINATES:
        raise ValueError(f"Unknown city '{city}'. Known cities: {', '.join(COORDINATES)}")
    lat, lon = COORDINATES[city]
    return {
        "latitude": lat,
        "longitude": lon,
        "hourly": HOURLY_VARS,
        "timezone": "auto",
        "forecast_days": days,
    }


def _hourly_dataframe(response) -> pd.DataFrame:
    """Turn one Open-Meteo response object into a tidy hourly DataFrame."""
    hourly = response.Hourly()

    data = {
        "date": pd.date_range(
            start=pd.to_datetime(hourly.Time(), unit="s", utc=True),
            end=pd.to_datetime(hourly.TimeEnd(), unit="s", utc=True),
            freq=pd.Timedelta(seconds=hourly.Interval()),
            inclusive="left",
        )
    }
    for i, var_name in enumerate(HOURLY_VARS):
        data[var_name] = hourly.Variables(i).ValuesAsNumpy()

    return pd.DataFrame(data)


def _daily_summary(df: pd.DataFrame) -> list[dict]:
    """Collapse the hourly DataFrame into one dict per calendar day.

    MCP tool results are serialized to JSON, so we return plain
    dicts/floats/strings here rather than a DataFrame or numpy types -
    those aren't JSON-serializable and would blow up over the wire.
    """
    df = df.copy()
    df["day"] = df["date"].dt.date

    days = []
    for day, group in df.groupby("day"):
        days.append(
            {
                "date": day.isoformat(),
                "temp_min_c": round(float(group["temperature_2m"].min()), 1),
                "temp_max_c": round(float(group["temperature_2m"].max()), 1),
                "feels_like_min_c": round(float(group["apparent_temperature"].min()), 1),
                "feels_like_max_c": round(float(group["apparent_temperature"].max()), 1),
                "precipitation_mm": round(float(group["precipitation"].sum()), 1),
                "max_weather_code": int(group["weather_code"].max()),
            }
        )
    return days


def _fetch_forecast(city: str, days: int = 3) -> dict:
    params = _params_for(city, days)
    responses = openmeteo.weather_api(FORECAST_URL, params=params)
    df = _hourly_dataframe(responses[0])
    return {
        "city": city,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "days": _daily_summary(df),
    }


# --- Weather-code -> emoji, used by the chart tool -------------------------
# WMO weather interpretation codes, grouped into a handful of buckets.
_WEATHER_ICONS = [
    (0, 0, "\u2600\ufe0f"),  # clear
    (1, 3, "\u26c5"),  # partly cloudy
    (45, 48, "\U0001f32b\ufe0f"),  # fog
    (51, 57, "\U0001f326\ufe0f"),  # drizzle
    (61, 67, "\U0001f327\ufe0f"),  # rain
    (71, 77, "\u2744\ufe0f"),  # snow
    (80, 82, "\U0001f326\ufe0f"),  # rain showers
    (85, 86, "\U0001f328\ufe0f"),  # snow showers
    (95, 99, "\u26c8\ufe0f"),  # thunderstorm
]


def _weather_icon(code: int) -> str:
    for lo, hi, icon in _WEATHER_ICONS:
        if lo <= code <= hi:
            return icon
    return "\u2601\ufe0f"  # cloudy, fallback


def _temp_bar(temp_max_c: float, width: int = 20, lo: float = -5, hi: float = 30) -> str:
    """A crude horizontal bar so relative temperature is visible at a glance
    in a monospace Slack code block. Fixed scale (lo..hi) so bars are
    comparable week over week, not just within a single message."""
    fraction = max(0.0, min(1.0, (temp_max_c - lo) / (hi - lo)))
    filled = round(fraction * width)
    return "#" * filled + "-" * (width - filled)


# --- MCP tools --------------------------------------------------------
@mcp.tool()
def list_cities() -> list[str]:
    """Return the list of cities this server has coordinates for."""
    return list(COORDINATES.keys())


@mcp.tool()
def get_forecast(city: str, days: int = 3) -> dict:
    """Get a daily weather summary (temps, feels-like, precipitation,
    dominant weather code) for one supported city.

    Args:
        city: One of the supported city names, e.g. "Nijmegen".
        days: How many days ahead to include (Open-Meteo allows up to 16).
    """
    return _fetch_forecast(city, days)


@mcp.tool()
def get_forecast_all(days: int = 3) -> dict:
    """Get the daily weather summary for every supported city.

    Args:
        days: How many days ahead to include for each city.
    """
    return {city: _fetch_forecast(city, days) for city in COORDINATES}


@mcp.tool()
def render_temperature_chart(days: int = 4) -> str:
    """Render a compact text chart (temperature bar + weather icon +
    precipitation per day) for every supported city, meant to be dropped
    into a Slack code block (monospace) as-is.

    This lives in the server rather than the client on purpose: the server
    already has the forecast data and the weather-code -> icon mapping, so
    it can hand back something ready to display instead of making the
    client re-fetch and re-interpret raw numbers.

    Args:
        days: How many days ahead to include for each city.
    """
    lines = []
    for city in COORDINATES:
        forecast = _fetch_forecast(city, days)
        lines.append(city)
        for day in forecast["days"]:
            weekday = datetime.fromisoformat(day["date"]).strftime("%a")
            icon = _weather_icon(day["max_weather_code"])
            bar = _temp_bar(day["temp_max_c"])
            rain = f"{day['precipitation_mm']:>4.1f}mm" if day["precipitation_mm"] > 0 else "       "
            lines.append(
                f"  {weekday} {icon} {day['temp_min_c']:>4.1f}-{day['temp_max_c']:>4.1f}C "
                f"[{bar}] {rain}"
            )
        lines.append("")
    return "\n".join(lines).rstrip()


if __name__ == "__main__":
    mcp.run(transport="stdio")
