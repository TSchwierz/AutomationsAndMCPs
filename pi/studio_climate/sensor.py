from __future__ import annotations

import logging
import math
import random
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class Reading:
    temperature_c: float
    humidity_pct: float


class SensorError(RuntimeError):
    pass


class MockSensor:
    """Deterministic-ish mock readings for PC/dev without DHT11 hardware."""

    def __init__(self) -> None:
        self._t0 = time.time()

    def read(self) -> Reading:
        elapsed = time.time() - self._t0
        # Slow sine around a comfortable studio band with light noise.
        temp = 21.5 + 1.8 * math.sin(elapsed / 3600) + random.uniform(-0.2, 0.2)
        humidity = 48 + 6 * math.sin(elapsed / 2700 + 1.0) + random.uniform(-0.5, 0.5)
        return Reading(round(temp, 1), round(humidity, 1))


class DHT11Sensor:
    def __init__(self, gpio_pin: int = 23) -> None:
        self.gpio_pin = gpio_pin
        self._device = None
        self._init_device()

    def _init_device(self) -> None:
        try:
            import board
            import adafruit_dht
        except ImportError as exc:
            raise SensorError(
                "adafruit-circuitpython-dht (and blinka) are required on the Pi. "
                "Or set mock_sensor = true in config.toml for testing."
            ) from exc

        pin = getattr(board, f"D{self.gpio_pin}", None)
        if pin is None:
            raise SensorError(f"Unknown board pin D{self.gpio_pin}")
        # use_pulseio=False is recommended on Raspberry Pi Linux.
        self._device = adafruit_dht.DHT11(pin, use_pulseio=False)

    def read(self) -> Reading:
        assert self._device is not None
        try:
            temperature_c = self._device.temperature
            humidity = self._device.humidity
        except RuntimeError as exc:
            # DHT sensors fail reads often; caller should retry / skip.
            raise SensorError(str(exc)) from exc

        if temperature_c is None or humidity is None:
            raise SensorError("Sensor returned empty reading")
        return Reading(float(temperature_c), float(humidity))

    def close(self) -> None:
        if self._device is not None:
            try:
                self._device.exit()
            except Exception:  # noqa: BLE001
                logger.debug("DHT exit failed", exc_info=True)
            self._device = None


def create_sensor(*, mock: bool, gpio_pin: int):
    if mock:
        logger.info("Using mock sensor (no DHT11 hardware)")
        return MockSensor()
    return DHT11Sensor(gpio_pin=gpio_pin)


def band_status(
    temperature_c: float,
    humidity_pct: float,
    temp_min: float,
    temp_max: float,
    humidity_min: float,
    humidity_max: float,
) -> dict[str, bool | str]:
    temp_ok = temp_min <= temperature_c <= temp_max
    humidity_ok = humidity_min <= humidity_pct <= humidity_max
    if temp_ok and humidity_ok:
        overall = "in_band"
    else:
        overall = "out_of_band"
    return {
        "temperature_in_band": temp_ok,
        "humidity_in_band": humidity_ok,
        "overall": overall,
    }
