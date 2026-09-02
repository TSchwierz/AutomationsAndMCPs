from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time, tzinfo
from typing import Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

DEFAULT_START = time(23, 0)
DEFAULT_END = time(8, 0)

_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off", ""}


def parse_bool(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUE_VALUES:
        return True
    if text in _FALSE_VALUES:
        return False
    return default


def parse_hhmm(value: object) -> time | None:
    """Parse "HH:MM" (or "HH") into a time, or None if malformed."""
    text = str(value).strip()
    if not text:
        return None
    parts = text.split(":")
    if len(parts) > 2:
        return None
    try:
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) == 2 else 0
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return time(hour=hour, minute=minute)


def resolve_timezone(name: object) -> tzinfo | None:
    """Return the named zone, or None to mean "use the machine's local time"."""
    text = str(name).strip()
    if not text:
        return None
    try:
        return ZoneInfo(text)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("Unknown timezone %r; falling back to system local time", text)
        return None


def format_hhmm(value: time) -> str:
    return f"{value.hour:02d}:{value.minute:02d}"


@dataclass(frozen=True)
class QuietHours:
    enabled: bool
    start: time
    end: time
    tz: tzinfo | None
    tz_name: str

    @classmethod
    def from_settings(cls, settings: Mapping[str, object]) -> QuietHours:
        start = parse_hhmm(settings.get("quiet_hours_start", "")) or DEFAULT_START
        end = parse_hhmm(settings.get("quiet_hours_end", "")) or DEFAULT_END
        tz_name = str(settings.get("quiet_hours_timezone", "") or "").strip()
        return cls(
            enabled=parse_bool(settings.get("quiet_hours_enabled", False)),
            start=start,
            end=end,
            tz=resolve_timezone(tz_name),
            tz_name=tz_name,
        )

    def is_quiet(self, moment: datetime) -> bool:
        if not self.enabled or self.start == self.end:
            return False
        local = moment.astimezone(self.tz)
        now = local.time()
        if self.start < self.end:
            return self.start <= now < self.end
        # Window wraps past midnight, e.g. 23:00 → 08:00.
        return now >= self.start or now < self.end

    def describe(self) -> str:
        zone = self.tz_name or "system local time"
        return f"{format_hhmm(self.start)}–{format_hhmm(self.end)} {zone}"
