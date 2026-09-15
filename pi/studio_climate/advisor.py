from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .config import PI_ROOT
from .db import Incident, to_iso

logger = logging.getLogger(__name__)

CONTEXT_CHAR_LIMIT = 100_000
MARKDOWN_FILES = ("info.md", "strategies.md", "anomalies.md")
EXAMPLE_DIR = PI_ROOT / "advisor"


def default_advisor_path() -> Path:
    return PI_ROOT / "data" / "advisor"


def iso_week_id(moment) -> str:
    iso = moment.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


class AdvisorStore:
    """Markdown notes + weekly JSON under data/advisor (gitignored)."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or default_advisor_path()
        self.reports_dir = self.root / "reports"

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        for name in MARKDOWN_FILES:
            path = self.root / name
            if path.exists():
                continue
            example = EXAMPLE_DIR / name.replace(".md", ".example.md")
            if example.exists():
                path.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
            else:
                path.write_text("", encoding="utf-8")

    def read_markdown(self, name: str) -> str:
        if name not in MARKDOWN_FILES:
            raise ValueError(f"Unknown advisor file: {name}")
        self.ensure()
        path = self.root / name
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8")

    def write_markdown(self, name: str, body: str) -> str:
        if name not in MARKDOWN_FILES:
            raise ValueError(f"Unknown advisor file: {name}")
        self.ensure()
        text = body if body.endswith("\n") or body == "" else body + "\n"
        (self.root / name).write_text(text, encoding="utf-8")
        return text

    def list_report_ids(self) -> list[str]:
        self.ensure()
        ids = [p.stem for p in self.reports_dir.glob("*.json")]
        ids.sort(reverse=True)
        return ids

    def read_report(self, week_id: str) -> dict[str, Any] | None:
        path = self.reports_dir / f"{week_id}.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def write_report(self, week_id: str, payload: dict[str, Any]) -> Path:
        self.ensure()
        path = self.reports_dir / f"{week_id}.json"
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return path

    def append_anomaly(self, incident: Incident) -> None:
        self.ensure()
        path = self.root / "anomalies.md"
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        marker = f"incident-{incident.id}"
        if marker in existing:
            return
        ended = to_iso(incident.ended_at) if incident.ended_at else "open"
        dew = (
            f"{incident.outdoor_dew_point_c:.1f}°C"
            if incident.outdoor_dew_point_c is not None
            else "n/a"
        )
        block = (
            f"\n## {to_iso(incident.started_at)} ({marker})\n"
            f"- {incident.metric} {incident.direction}, untagged\n"
            f"- Δ RH {incident.peak_delta_humidity_pct:+.1f}% / "
            f"Δ T {incident.peak_delta_temp_c:+.1f}°C\n"
            f"- ended {ended}; outdoor dew point {dew}\n"
        )
        path.write_text(existing.rstrip() + "\n" + block, encoding="utf-8")
        logger.info("Appended anomaly note for incident %s", incident.id)

    def context_pack(
        self,
        *,
        live: dict[str, Any] | None = None,
        report_limit: int = 4,
    ) -> dict[str, Any]:
        self.ensure()
        week_ids = self.list_report_ids()[:report_limit]
        info = self.read_markdown("info.md")
        strategies = self.read_markdown("strategies.md")
        anomalies = self.read_markdown("anomalies.md")
        reports: list[dict[str, Any]] = []
        for week_id in week_ids:
            payload = self.read_report(week_id)
            if payload is not None:
                reports.append(payload)

        sections = [
            "# Room notes (info.md)\n" + info.strip(),
            "# Strategies\n" + strategies.strip(),
            "# Anomalies\n" + anomalies.strip(),
        ]
        for payload in reports:
            week = payload.get("week", "unknown")
            sections.append(
                f"# Weekly report {week}\n```json\n"
                + json.dumps(payload, ensure_ascii=False, indent=2)
                + "\n```"
            )
        if live:
            sections.append(
                "# Live snapshot\n```json\n"
                + json.dumps(live, ensure_ascii=False, indent=2)
                + "\n```"
            )
        pack = "\n\n".join(sections)
        truncated = False
        while len(pack) > CONTEXT_CHAR_LIMIT and reports:
            reports.pop()
            truncated = True
            sections = [
                "# Room notes (info.md)\n" + info.strip(),
                "# Strategies\n" + strategies.strip(),
                "# Anomalies\n" + anomalies.strip(),
            ]
            for payload in reports:
                week = payload.get("week", "unknown")
                sections.append(
                    f"# Weekly report {week}\n```json\n"
                    + json.dumps(payload, ensure_ascii=False, indent=2)
                    + "\n```"
                )
            if live:
                sections.append(
                    "# Live snapshot\n```json\n"
                    + json.dumps(live, ensure_ascii=False, indent=2)
                    + "\n```"
                )
            pack = "\n\n".join(sections)
        if len(pack) > CONTEXT_CHAR_LIMIT:
            pack = pack[: CONTEXT_CHAR_LIMIT - 20] + "\n…[truncated]"
            truncated = True

        return {
            "weeks": [str(p.get("week", "")) for p in reports],
            "info": info,
            "strategies": strategies,
            "anomalies": anomalies,
            "live": live,
            "pack": pack,
            "truncated": truncated,
        }
