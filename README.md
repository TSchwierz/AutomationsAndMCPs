# weather-packing-bot

A scheduled bot that posts a Slack message with a multi-day weather
forecast and a packing recommendation for a recurring three-city travel
route. It is also a small reference implementation of an MCP (Model
Context Protocol) server and client pair: the forecast data is served by a
standalone MCP server and consumed by an MCP client, rather than being
fetched directly by the bot script.

## Overview

The bot runs on a weekly schedule (Fridays, 15:00 CEST) and posts a Slack
message covering the weather from that day through the following Monday
evening. The forecast and packing recommendation are itinerary-aware: they
account for which of three cities the traveler is expected to be in on
each day of that window, rather than showing all locations' weather
indiscriminately.

```
GitHub Actions (scheduled, Fridays)
        │
        ▼
bot/packing_advisor.py            MCP client
        │  spawns as subprocess, communicates over stdio
        ▼
mcp_server/weather_server.py      MCP server, queries Open-Meteo
        │
        ▼
   Slack incoming webhook
```

## Why an MCP server and client

The server exposes weather tools without any knowledge of who calls them
or why - it could be this bot, an IDE assistant, or any other MCP-capable
client. The client does not import the server's Python functions directly;
it discovers available tools at runtime (`session.list_tools()`) and
invokes them by name over the protocol (`session.call_tool(...)`). This
separation is what distinguishes the design from simply calling a function
in the same process.

## Components

### `mcp_server/weather_server.py`

An MCP server (using the `MCPServer` class from the `mcp` package) that
wraps the Open-Meteo API and exposes four tools:

| Tool | Description |
|---|---|
| `list_cities()` | Returns the configured city names. |
| `get_forecast(city, days=3)` | Daily weather summary for one city. |
| `get_forecast_all(days=3)` | Daily weather summary for every configured city. |
| `render_temperature_chart(days=4)` | A compact per-city, per-day text chart (temperature bar, weather icon, precipitation), formatted for a Slack code block. |

Cities are defined in the `COORDINATES` dict and can be extended freely.

### `bot/packing_advisor.py`

An MCP client that:

1. Computes the forecast window - the current date through the following
   Monday, inclusive - via `forecast_window()`.
2. Spawns `weather_server.py` as a subprocess, performs the MCP handshake,
   and calls `get_forecast_all` and `render_temperature_chart` for that
   window.
3. Matches each date in the window against `WEEKLY_ITINERARY`, a dict
   mapping weekday to the city (or cities) the traveler is expected to be
   in that day, so that only relevant locations inform the output.
4. Passes the matched days to `recommend_packing()`, a rule-based function
   using temperature and precipitation thresholds (no external AI call).
5. Formats a Slack Block Kit message - day-by-day breakdown, packing list,
   and chart - and posts it via an incoming webhook.

### Travel itinerary

`WEEKLY_ITINERARY` in `bot/packing_advisor.py` encodes a recurring weekly
route:

```python
WEEKLY_ITINERARY = {
    4: [("Nijmegen", "arrival")],       # Friday
    5: [("Nijmegen", "")],              # Saturday
    6: [("Nijmegen", "")],              # Sunday
    0: [("Den Bosch", "day"), ("Kerpen", "evening, back home")],  # Monday
}
```

Monday has two entries because it is a transition day: the itinerary
places the traveler in Den Bosch during the day and back in Kerpen by
evening. This dict is the single place to edit if the route changes.

### The chart

`render_temperature_chart` produces a plain-text, monospace chart rather
than an image. This is a deliberate constraint: a Slack *incoming webhook*
can only post text or Block Kit JSON - it cannot upload a binary file.
Posting an actual image would require a Slack bot token (an installed
Slack App with the `files:write` scope) and a call to Slack's `files.upload`
Web API instead of the webhook. That is a viable extension but requires
managing an additional credential; the current implementation avoids that
requirement entirely.

## Setup

### Prerequisites
- A Slack app with an incoming webhook URL.
- A GitHub repository to host the code and run the scheduled workflow.

### Local run
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in SLACK_WEBHOOK_URL
export $(grep -v '^#' .env | xargs)
python bot/packing_advisor.py
```

`SIMULATE_DATE` (see `.env.example`) can be set to an ISO date to exercise
the itinerary/forecast-window logic without waiting for an actual Friday.

The MCP server can also be inspected on its own:
```bash
pip install "mcp[cli]"
mcp dev mcp_server/weather_server.py
```
This opens the MCP Inspector in a browser, useful for viewing tool schemas
and calling tools such as `get_forecast` or `render_temperature_chart`
directly.

### GitHub repository configuration
- Add `SLACK_WEBHOOK_URL` as a repository secret (Settings → Secrets and
  variables → Actions → Secrets).
- No other repository configuration is required; the itinerary is defined
  in code rather than as a repository variable.

### Schedule
`.github/workflows/weekly-forecast.yml` runs every Friday at 13:00 UTC
(15:00 CEST) via `cron: "0 13 * * 5"`. Cron schedules in GitHub Actions do
not observe daylight saving time, so during the CET (winter) period this
fires at 14:00 local time instead of 15:00; the cron expression can be
adjusted seasonally if that offset matters. The workflow also supports
manual triggering (`workflow_dispatch`) with an optional `simulate_date`
input, useful for testing before relying on the schedule.

## Known limitations and possible extensions
- The Slack message includes a text-based chart rather than an image, for
  the reason described above. A PNG chart via matplotlib, combined with a
  Slack bot token and `files.upload`, is a possible follow-up.
- `recommend_packing()` is rule-based. Replacing it with a call to an LLM
  (e.g. the Anthropic API) would allow more natural-language output; the
  MCP tool results are plain dicts, so they can be passed directly into
  such a call.
- The itinerary is a fixed weekly pattern. A calendar-integration source
  (e.g. reading actual travel dates from a calendar) would generalize it
  beyond a repeating weekly route.

## Notes on the source material
The original draft server script had several issues that were corrected
during development of this implementation:
- An unused, non-existent `httpx2` import.
- `get_params()` built a parameters dict but did not return it.
- The Open-Meteo client was constructed only inside `if __name__ ==
  "__main__"`, making it unavailable to the tool functions that referenced
  it at module scope.
- `process_response` attempted to assign into a `list` using a `dict_keys`
  object as an index, which is not valid.
- Tool functions returned pandas `DataFrame` objects and numpy scalar
  types, which are not JSON-serializable; MCP tool results must be
  serializable, since they are transmitted as JSON.
