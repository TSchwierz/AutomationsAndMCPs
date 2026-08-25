from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

PI_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PI_ROOT / "config.toml"
EXAMPLE_CONFIG_PATH = PI_ROOT / "config.example.toml"


@dataclass(frozen=True)
class AppConfig:
    host: str
    port: int
    api_token: str
    db_path: Path
    sample_interval_seconds: int
    gpio_pin: int
    mock_sensor: bool
    ntfy_server: str
    ntfy_topic: str
    ntfy_token: str
    humidity_min: float
    humidity_max: float
    temp_min: float
    temp_max: float
    sustain_minutes: int
    alert_cooldown_minutes: int


def _resolve_db_path(raw: str) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        path = PI_ROOT / path
    return path


def load_config(path: Path | None = None) -> AppConfig:
    config_path = path or DEFAULT_CONFIG_PATH
    if not config_path.exists():
        if EXAMPLE_CONFIG_PATH.exists():
            config_path = EXAMPLE_CONFIG_PATH
        else:
            raise FileNotFoundError(
                f"No config found at {DEFAULT_CONFIG_PATH} or {EXAMPLE_CONFIG_PATH}"
            )

    with config_path.open("rb") as fh:
        raw = tomllib.load(fh)

    server = raw.get("server", {})
    database = raw.get("database", {})
    collector = raw.get("collector", {})
    ntfy = raw.get("ntfy", {})
    defaults = raw.get("defaults", {})

    return AppConfig(
        host=str(server.get("host", "0.0.0.0")),
        port=int(server.get("port", 8787)),
        api_token=str(server.get("api_token", "")),
        db_path=_resolve_db_path(str(database.get("path", "data/climate.db"))),
        sample_interval_seconds=int(collector.get("sample_interval_seconds", 60)),
        gpio_pin=int(collector.get("gpio_pin", 23)),
        mock_sensor=bool(collector.get("mock_sensor", False)),
        ntfy_server=str(ntfy.get("server", "https://ntfy.sh")).rstrip("/"),
        ntfy_topic=str(ntfy.get("topic", "")),
        ntfy_token=str(ntfy.get("token", "")),
        humidity_min=float(defaults.get("humidity_min", 40)),
        humidity_max=float(defaults.get("humidity_max", 55)),
        temp_min=float(defaults.get("temp_min", 18)),
        temp_max=float(defaults.get("temp_max", 24)),
        sustain_minutes=int(defaults.get("sustain_minutes", 30)),
        alert_cooldown_minutes=int(defaults.get("alert_cooldown_minutes", 120)),
    )
