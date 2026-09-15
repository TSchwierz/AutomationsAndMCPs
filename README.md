# Studio Climate Monitor

LAN-only temperature and humidity monitor for a studio: mold risk + PLA filament climate.

- **Raspberry Pi (Python):** KY-015 / DHT11 collector → SQLite → ntfy alerts → FastAPI on port `8787`
- **PC (Node.js):** local Vite/React dashboard for charts and remote band settings
- **Outside conditions:** hourly average of three weather services, for indoor/outdoor comparison and a 4-hour outlook

Nothing is exposed to the public internet. The PC talks to the Pi over the same Wi‑Fi.

## Hardware (Joy-IT KY-015)

| Sensor | Raspberry Pi |
| --- | --- |
| Signal | GPIO 23 (physical pin 16) |
| +V | 3.3 V (pin 1) |
| GND | GND (pin 6) |

Docs: [KY-015 SensorKit](https://www.sensorkit.joy-it.net/en/sensors/ky-015)

## Quick start — Raspberry Pi

```bash
cd pi
python3 -m venv --system-site-packages .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install adafruit-circuitpython-dht adafruit-blinka
# DHT on Pi often also needs:
sudo apt-get install -y libgpiod2

cp config.example.toml config.toml
# Edit config.toml:
#   mock_sensor = false
#   api_token = "a-long-secret"
#   ntfy.topic = "your-private-topic"

python -m studio_climate all
```

API (same Wi‑Fi): `http://<pi-hostname>.local:8787` or `http://<pi-lan-ip>:8787`

### systemd

Edit paths/user in `pi/studio-climate.service`, then:

```bash
sudo cp studio-climate.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now studio-climate
```

### ntfy

1. Install the ntfy app on your phone.
2. Subscribe to a hard-to-guess topic.
3. Set `ntfy.topic` in `config.toml` (or via the dashboard **Target bands** form).
4. Out-of-band alerts fire after `sustain_minutes` (default 30), with `alert_cooldown_minutes` between repeats (default 360 / 6 hours). Sudden shifts in the rolling average of the last `rolling_window_points` samples (default 8) open a **climate shift** incident and send a warning. Tag those in the dashboard.

### Quiet hours

Notifications are held back during the `[quiet_hours]` window in `config.toml` (default 23:00–08:00, so nothing buzzes overnight):

```toml
[quiet_hours]
enabled = true
start = "23:00"
end = "08:00"
# IANA zone, e.g. "Europe/Berlin". Empty = the Pi's own local time.
timezone = ""
```

Readings and breach tracking continue as normal — only the ntfy push is skipped. If a breach is still active when the window ends, the alert goes out on the next sample. The window may wrap past midnight, and it is editable from the dashboard.

## Outside conditions

Outdoor temperature and humidity come from three services queried in parallel and averaged, so one flaky or offline service degrades the reading instead of losing it:

| Service | Key needed | Notes |
| --- | --- | --- |
| [Open-Meteo](https://open-meteo.com/) | no | Includes DWD ICON; free for non-commercial use |
| [OpenWeatherMap](https://openweathermap.org/) | yes | Skipped when `openweathermap_api_key` is empty |
| [Bright Sky](https://brightsky.dev/) | no | Official DWD data (MOSMIX + SYNOP observations) |

Numeric values are averaged over whichever services answered; the weather condition is a majority vote, with the more disruptive condition winning a tie. Bright Sky's hourly forecast reports no relative humidity, so it is derived from its dew point via the Magnus formula.

The collector polls on its own thread every `poll_interval_minutes` (default 60) and stores both the averaged reading and a 12-hour hourly outlook. An outgoing alert also carries the outside conditions: it reuses the stored reading when it is younger than `alert_max_age_minutes`, otherwise it fetches fresh before sending.

Because indoor and outdoor dew points are both known, alerts and the dashboard say whether opening a window would actually help:

```
Humidity high — mold risk: 68.0% RH for 90 min (threshold 55% RH). Studio climate out of target band.
Outside: 20.4°C, 58% RH, cloudy (dew point 11.8°C). Outside air is drier — dew point 3.5°C below inside. Airing out removes moisture.
```

Configure the location and services in `config.toml`:

```toml
[weather]
enabled = true
latitude = 51.2277
longitude = 6.7735
poll_interval_minutes = 60
# On an alert, reuse the stored reading if younger than this; else refetch.
alert_max_age_minutes = 30
forecast_hours = 12
request_timeout_seconds = 10
# Optional — leave empty to average over Open-Meteo and Bright Sky only.
openweathermap_api_key = ""
```

API keys stay in `config.toml` and are never returned by the API. Set `enabled = false` for a Pi without internet access; the dashboard then hides the outdoor cards.

The dashboard shows an **Outside** card beside **Now**, a **Next 4 hours** strip highlighting the hour the weather is expected to turn, and dashed outdoor lines on the chart. Click a legend entry to hide a series.

## Quick start — PC dashboard

```bash
cd pc-dashboard
cp .env.example .env
# Set VITE_PI_API_BASE to your Pi, e.g. http://raspberrypi.local:8787
# Set VITE_API_TOKEN to the same value as pi config api_token
npm install
npm run dev
```

Open http://localhost:5173

## API

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/health` | Liveness |
| GET | `/current` | Latest reading, rolling average, in/out of band, open incident |
| GET | `/measurements?from=&to=&limit=` | History (ISO timestamps) |
| GET | `/stats?from=&to=` | min/max/avg |
| GET | `/rolling-average?from=&to=&window=` | Current vs previous window, plus the rolling series |
| GET | `/incidents?from=&to=&open_only=` | Detected climate-shift incidents |
| GET | `/incidents/tags` | Suggested + custom labels |
| PATCH | `/incidents/{id}` | Set `tags` and/or `notes`; send header `X-API-Token` if configured |
| GET | `/outside` | Averaged outdoor reading, indoor/outdoor comparison, next 4 h outlook |
| GET | `/outside/measurements?from=&to=&limit=` | Outdoor history |
| POST | `/outside/refresh` | Force a poll now; send header `X-API-Token` if configured |
| GET | `/settings` | Bands, ntfy, intervals, quiet hours |
| PUT | `/settings` | Update bands; send header `X-API-Token` if configured |

Default bands: humidity 40–55% RH, temperature 18–24 °C (editable). Out-of-band cooldown defaults to 6 hours. Climate-shift warnings use the last 8 samples (~8 min at a 60 s interval) and fire when humidity moves by 6% RH or temperature by 1.5 °C versus the previous window.

Tagged incidents stay in SQLite with the indoor/outdoor snapshot from when they opened, so a later exporter can send that history to an analyser.

## Local smoke test (no Pi / no sensor)

On this PC:

```bash
cd pi
python -m venv .venv
# Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp config.example.toml config.toml
# Set mock_sensor = true and optionally api_token in config.toml
python -m studio_climate all
```

Optional DB/sensor self-check:

```bash
python scripts/selfcheck.py
```

In another terminal:

```bash
cd pc-dashboard
cp .env.example .env
npm install
npm run dev
```
## SD card backup

Frequent SQLite writes wear SD cards. Periodically copy the DB off the Pi:

```bash
# From the PC (PowerShell example)
scp pi@raspberrypi.local:~/studio-climate/pi/data/climate.db .\backups\climate-$(Get-Date -Format yyyyMMdd).db
```

Or rsync/cron on the Pi to a NAS. Prefer keeping `data/` on a USB disk if you have one.

## Project layout

```
studio-climate/
  pi/                 Python collector + API
  pc-dashboard/       Node.js Vite + React charts
```
