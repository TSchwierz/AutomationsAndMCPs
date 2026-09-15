#!/usr/bin/env bash
# Weekly JSON briefing, then a restricted Vibe pass over data/advisor only.
set -euo pipefail

PI_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PI_ROOT"

if [[ -x "${PI_ROOT}/.venv/bin/python" ]]; then
  PYTHON="${PI_ROOT}/.venv/bin/python"
else
  PYTHON="$(command -v python3)"
fi

REPORT="$("$PYTHON" -m studio_climate briefing)"
echo "Wrote ${REPORT}"
WEEK="$(basename "${REPORT}" .json)"

ADVISOR="${PI_ROOT}/data/advisor"
mkdir -p "${ADVISOR}/reports"
cd "${ADVISOR}"

if ! command -v vibe >/dev/null 2>&1; then
  echo "vibe not found; weekly JSON written, skipping strategies.md update"
  exit 0
fi

PROMPT="Read @info.md, @reports/${WEEK}.json, and @anomalies.md. Append dated bullets to strategies.md only, summarizing what this week implies for mold risk and PLA storage. Do not edit reports, info.md, anomalies.md, or any file outside this folder."

vibe --trust \
  --prompt "${PROMPT}" \
  --enabled-tools read \
  --enabled-tools write_file \
  --enabled-tools edit \
  --max-turns 4 \
  --output json
