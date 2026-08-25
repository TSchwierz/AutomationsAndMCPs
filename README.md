# Studio Climate Monitor

LAN-only temperature and humidity monitor for a studio: mold risk + PLA filament climate.

- **Raspberry Pi (Python):** KY-015 / DHT11 collector → SQLite → ntfy alerts → FastAPI on port `8787`
- **PC (Node.js):** local Vite/React dashboard for charts and remote band settings

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
4. Alerts fire after `sustain_minutes` outside the band (default 30), with `alert_cooldown_minutes` between repeats.

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
| GET | `/current` | Latest reading + in/out of band |
| GET | `/measurements?from=&to=&limit=` | History (ISO timestamps) |
| GET | `/stats?from=&to=` | min/max/avg |
| GET | `/settings` | Bands, ntfy, intervals |
| PUT | `/settings` | Update bands; send header `X-API-Token` if configured |

Default bands: humidity 40–55% RH, temperature 18–24 °C (editable).

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
