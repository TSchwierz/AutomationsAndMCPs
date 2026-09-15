# Advisor files

The live copies live in `pi/data/advisor/` (gitignored): `info.md`, `strategies.md`, `anomalies.md`, and `reports/YYYY-Www.json`.

On first start the API copies the `*.example.md` templates from this folder if those files are missing.

1. Copy `info.example.md` → `../data/advisor/info.md` and describe your studio.
2. Tag climate-shift incidents in the dashboard as they happen.
3. Enable `studio-climate-advisor.timer` so a weekly JSON is written and Vibe may append to `strategies.md`.
4. Chat from the PC dashboard (`npm run dev`) with `MISTRAL_API_KEY` in `pc-dashboard/.env`.
