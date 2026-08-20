# weather-packing-bot

Every Friday afternoon: a Slack message covering the weather from now
through Monday evening across your Kerpen -> Nijmegen -> Den Bosch -> Kerpen
travel loop, with a packing recommendation and a small text chart. Built as
an excuse to learn MCP: the forecast is served by a real MCP server, called
by a real MCP client, not just imported as a Python function.

## How it fits together

```
GitHub Actions (Fridays 15:00 CEST)
        │
        ▼
bot/packing_advisor.py            <- MCP client
        │  spawns as subprocess, talks over stdio
        ▼
mcp_server/weather_server.py      <- MCP server, calls Open-Meteo
        │
        ▼
   Slack incoming webhook
```

**Why two processes instead of one script?** That's the whole point of MCP:
the server doesn't know or care who's calling it - it could be this bot,
Claude Desktop, or any other MCP client. The client doesn't import the
server's Python functions directly; it discovers them at runtime by asking
"what tools do you have?" (`session.list_tools()`) and then calls them by
name over a protocol (`session.call_tool(...)`). That's what makes it MCP
rather than just "a function I imported."

### `mcp_server/weather_server.py`
Four tools via `MCPServer` (from the `mcp` package):
- `list_cities()` - which cities are configured.
- `get_forecast(city, days=3)` - daily summary for one city.
- `get_forecast_all(days=3)` - the same, for every configured city.
- `render_temperature_chart(days=4)` - a compact per-city, per-day text
  chart (temperature bar + weather icon + rain), meant to be dropped
  straight into a Slack code block.

Add more cities by extending the `COORDINATES` dict.

### `bot/packing_advisor.py`
1. Works out **how many days to ask for**: today through the next Monday,
   inclusive (`forecast_window()`). Triggered on a Friday that's 4 days
   (Fri/Sat/Sun/Mon).
2. Spawns `weather_server.py` as a subprocess, does the MCP handshake, calls
   `get_forecast_all` and `render_temperature_chart` for that many days.
3. Matches each date in the window against `WEEKLY_ITINERARY` - a small
   dict at the top of the file mapping weekday -> which city (or cities)
   you're in - so the recommendation only considers weather where you'll
   actually be, not all three cities every day. Monday has two entries
   (Den Bosch by day, Kerpen by evening) since that's a travel day.
4. Runs the matched days through `recommend_packing()` - plain temperature/
   precipitation thresholds, not an AI call, so it's fast, free, and
   predictable. Tune the thresholds (or `WEEKLY_ITINERARY`, if your route
   changes) directly in that file.
5. Formats a Slack Block Kit message (day-by-day breakdown, packing list,
   chart) and POSTs it to `SLACK_WEBHOOK_URL`.

### The chart, and why it's ASCII, not a PNG
`render_temperature_chart` builds the chart, which matches what you asked
for ("construction might be handled by the MCP"). It's plain text/monospace
rather than an image, though, for a concrete reason: **a Slack incoming
webhook can only post text/Block-Kit JSON - it cannot upload a binary
image.** To post a real PNG you'd need a Slack *bot token* (create a Slack
App, add the `files:write` scope, install it to your workspace) and call
`files.upload` on Slack's Web API instead of the webhook. That's a
reasonable upgrade later, but it's a bigger, separate piece of setup
(managing an OAuth token as another secret) - the current version gets you
a genuinely useful "chart" today with zero extra infrastructure. If you
want to go there, the code you already have does 90% of the work: you'd
add a matplotlib-based tool to the MCP server that returns a base64 PNG,
and swap `post_to_slack` for a `files.upload` call.

## Bugs fixed from the original draft
Worth knowing about since they're easy to reintroduce:
- `import httpx2` - typo/unused import, removed.
- `get_params()` built a dict but never `return`ed it.
- `openmeteo` (the Open-Meteo client) was only created inside
  `if __name__ == "__main__"`, so any tool call would hit a `NameError` -
  moved to module scope.
- `process_response` did `data[CITIES[i]] = ...` where `data` was a `list`
  (needs a dict) and `CITIES` was a `dict_keys` object (not subscriptable).
- Tool functions returned pandas `DataFrame`s / numpy floats, which aren't
  JSON-serializable - MCP results go over the wire as JSON. Everything a
  tool returns now is plain `dict`/`float`/`str`/`int`.
- Worth flagging on myself too: I initially "corrected" your original
  `from mcp.server import MCPServer` import to a different (FastMCP)
  import based on search results, which turned out to be wrong for the SDK
  version actually installed - your original import was right. Caught it
  by actually running the code in a sandbox rather than trusting docs. Two
  different frameworks (`fastmcp` the standalone fork vs. `mcp` the
  official SDK) get mixed up in tutorials constantly - worth knowing if you
  go looking for more MCP examples yourself.

## Setup

### 1. Slack webhook
You said you already have one - just make sure "Incoming Webhooks" is
enabled on the Slack app and you have the `https://hooks.slack.com/services/...`
URL.

### 2. Local test run
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in SLACK_WEBHOOK_URL
export $(grep -v '^#' .env | xargs)   # or use python-dotenv / direnv
python bot/packing_advisor.py
```
You should see a message land in the Slack channel your webhook points to.
Set `SIMULATE_DATE=2026-08-21` (a Friday) in `.env` to test the itinerary
logic without waiting for an actual Friday.

You can also poke at just the MCP server on its own, without Slack:
```bash
pip install "mcp[cli]"
mcp dev mcp_server/weather_server.py   # opens the MCP Inspector in your browser
```
Good way to see the tool schemas and try `get_forecast` /
`render_temperature_chart` by hand before wiring the bot around them.

### 3. GitHub repo config
- **Secret** `SLACK_WEBHOOK_URL` - Settings -> Secrets and variables ->
  Actions -> Secrets -> New repository secret.
- Nothing else is required - the itinerary logic doesn't need repo
  variables since it's driven by `WEEKLY_ITINERARY` in the code.

### 4. Schedule
`.github/workflows/weekly-forecast.yml` runs Fridays at 13:00 UTC (15:00
CEST) via `cron: "0 13 * * 5"`, and can also be triggered manually from the
Actions tab (`workflow_dispatch`, with an optional `simulate_date` input) -
use that to test the whole pipeline in CI before trusting the schedule.

## Extending it
- Route changes: edit `WEEKLY_ITINERARY` in `bot/packing_advisor.py`.
- Add cities: extend `COORDINATES` in `weather_server.py`.
- Real PNG chart in Slack: see "The chart" section above.
- Swap the rule-based `recommend_packing()` for a call to the Anthropic API
  if you want more conversational Slack copy - the MCP tool result is just
  a dict, so you'd hand the matched days to `client.messages.create(...)`
  and use the text back instead of the bullet list.
