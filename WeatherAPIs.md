Here are the 3 best free-friendly weather APIs for your use case (outdoor temperature, humidity, and upcoming weather changes) with endpoint routes and example request/response shapes.

1) Open-Meteo (recommended first choice)
Why: No API key, generous free tier, includes DWD ICON data, simple JSON, good for Europe/NRW.
License: Free for non‑commercial use (commercial plans available).

Endpoint
Route: GET https://api.open-meteo.com/v1/forecast

Example request
text
GET https://api.open-meteo.com/v1/forecast?latitude=51.2277&longitude=6.7735&current=temperature_2m,relative_humidity_2m,weather_code&hourly=temperature_2m,relative_humidity_2m,precipitation_probability,weather_code&timezone=auto&forecast_days=3
Key parameters:

latitude, longitude: location (e.g. Düsseldorf)

current: which current fields you want

hourly: hourly forecast fields (for “upcoming changes”)

forecast_days: number of forecast days (1–16)

timezone: auto or IANA timezone

Example response (simplified)
json
{
  "latitude": 51.2277,
  "longitude": 6.7735,
  "timezone": "Europe/Berlin",
  "current": {
    "time": "2026-09-02T20:00",
    "temperature_2m": 19.3,
    "relative_humidity_2m": 72,
    "weather_code": 3
  },
  "hourly": {
    "time": [
      "2026-09-02T20:00",
      "2026-09-02T21:00",
      "2026-09-02T22:00"
    ],
    "temperature_2m": [19.3, 18.8, 18.1],
    "relative_humidity_2m": [72, 75, 78],
    "precipitation_probability": [10, 20, 35],
    "weather_code": [3, 61, 61]
  }
}
temperature_2m: °C

relative_humidity_2m: %

weather_code: WMO code (e.g. 0=clear, 61=rain) – use this to detect upcoming weather changes.

2) OpenWeatherMap – Current + 5‑day Forecast
Why: Very popular, good documentation, free tier with 1M calls/month.
License: Free tier available; attribution required.

Endpoints
Current weather:
GET https://api.openweathermap.org/data/2.5/weather

5‑day / 3‑hour forecast:
GET https://api.openweathermap.org/data/2.5/forecast

You need an appid (API key) from openweathermap.org.

Example request – current
text
GET https://api.openweathermap.org/data/2.5/weather?lat=51.2277&lon=6.7735&units=metric&appid=YOUR_API_KEY
Example response – current (simplified)
json
{
  "name": "Düsseldorf",
  "main": {
    "temp": 19.4,
    "feels_like": 18.9,
    "humidity": 71,
    "temp_min": 18.0,
    "temp_max": 20.5,
    "pressure": 1015
  },
  "weather": [
    {
      "id": 803,
      "main": "Clouds",
      "description": "broken clouds",
      "icon": "04n"
    }
  ],
  "dt": 1725307200
}
main.temp: °C (with units=metric)

main.humidity: %

weather[0].main / description: current condition.

Example request – forecast
text
GET https://api.openweathermap.org/data/2.5/forecast?lat=51.2277&lon=6.7735&units=metric&appid=YOUR_API_KEY
Example response – forecast (simplified)
json
{
  "cod": "200",
  "cnt": 40,
  "list": [
    {
      "dt": 1725310800,
      "main": {
        "temp": 19.1,
        "feels_like": 18.7,
        "humidity": 73,
        "temp_min": 18.5,
        "temp_max": 19.1,
        "pressure": 1015
      },
      "weather": [
        {
          "id": 500,
          "main": "Rain",
          "description": "light rain",
          "icon": "10n"
        }
      ],
      "dt_txt": "2026-09-02 21:00:00"
    }
  ],
  "city": {
    "name": "Düsseldorf",
    "timezone": 7200
  }
}
Use the list array (3‑hour steps) to detect upcoming temperature/humidity trends and weather changes (weather[0].main).

3) DWD Open Data (German Weather Service)
Why: Official German data, excellent for NRW, free and open (CC BY 4.0).
Caveat: More “data service” than a single simple endpoint; you typically call MOSMIX (station forecasts) and/or ICON endpoints directly.

Typical pattern (MOSMIX point forecast)
Base: https://www.dwd.de/DE/leistungen/opendata/opendata.html

Example forecast endpoint (JSON, station-based):
GET https://www.dwd.de/DE/leistungen/opendata/weather/station/mosmix/{STATION_ID}.json
(You first look up a nearby station ID; many wrappers exist that hide this complexity.)

Because raw DWD endpoints are more complex, most developers use a thin wrapper (e.g. InfraNode’s DWD API) that exposes a simple REST interface.

Example via a simple DWD wrapper (InfraNode-style)
Route: GET https://api.infranode.dev/dwd/weather/current?city=Düsseldorf
(This is an example pattern; exact host/path may vary by provider.)

Example response (simplified)
json
{
  "city": "Düsseldorf",
  "source": "Deutscher Wetterdienst, DWD",
  "current": {
    "time": "2026-09-02T20:00+02:00",
    "temperature": 19.2,
    "humidity": 70,
    "condition": "overcast"
  },
  "forecast": [
    {
      "time": "2026-09-02T21:00+02:00",
      "temperature": 18.8,
      "humidity": 73,
      "condition": "light rain"
    },
    {
      "time": "2026-09-02T22:00+02:00",
      "temperature": 18.3,
      "humidity": 76,
      "condition": "light rain"
    }
  ]
}
temperature: °C

humidity: %

condition: textual description derived from DWD codes.

Implementation note: the DWD wrapper used in `pi/studio_climate/weather.py` is Bright Sky (https://brightsky.dev), which needs no API key and exposes the same DWD data through two stable routes:

GET https://api.brightsky.dev/current_weather?lat=51.2277&lon=6.7735
GET https://api.brightsky.dev/weather?lat=51.2277&lon=6.7735&date=...&last_date=...

Two shapes to watch for: `current_weather` reports wind and precipitation as `wind_speed_10` / `precipitation_60` style windows rather than plain fields, and the hourly `/weather` forecast leaves `relative_humidity` null, so humidity is derived from `dew_point`.
