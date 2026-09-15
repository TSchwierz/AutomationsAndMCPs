# Advisor files

Tracked in git (edit these):

- `pi/data/advisor/info.md` — room facts for the chat
- `pi/data/advisor/strategies.md` — what you tried
- `pi/data/advisor/anomalies.md` — unusual untagged shifts

Not tracked (generated):

- `pi/data/advisor/reports/YYYY-Www.json` — weekly briefings

`*.example.md` in this folder are templates. The API copies them into `data/advisor/` only if a file is missing, so it will not overwrite `info.md`.
