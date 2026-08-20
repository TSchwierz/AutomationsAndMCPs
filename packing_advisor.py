"""
MCP client + Slack bot.

Spawns mcp_server/weather_server.py as a subprocess, talks to it over stdio
using the MCP protocol, and turns the result into a packing recommendation +
mini weather chart posted to Slack.

Env vars (see .env.example):
    SLACK_WEBHOOK_URL - Slack incoming webhook URL
    SIMULATE_DATE      - optional ISO date (e.g. "2026-08-21") to pretend
                          "today" is that date. Handy for testing the
                          itinerary logic on a day other than Friday.
"""

import asyncio
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER_SCRIPT = Path(__file__).resolve() / "AutomationsAndMCPs" / "weather_server.py"
LOCAL_TZ = ZoneInfo("Europe/Berlin")  # Kerpen/Nijmegen/Den Bosch are all this offset

# --- Your weekly travel pattern ------------------------------------------
# Weekday index (Monday=0 ... Sunday=6) -> list of (city, label) you're in
# that day. Monday has two entries because you're in Den Bosch during the
# day and back in Kerpen by evening - edit this whenever your route changes.
WEEKLY_ITINERARY: dict[int, list[tuple[str, str]]] = {
    4: [("Nijmegen", "arrival")],       # Friday
    5: [("Nijmegen", "")],              # Saturday
    6: [("Nijmegen", "")],              # Sunday
    0: [("Den Bosch", "day"), ("Kerpen", "evening, back home")],  # Monday
}


# --- 1. Figure out the forecast window --------------------------------
def forecast_window(today: date) -> list[date]:
    """Every date from today up to and including the next Monday."""
    days_to_monday = (7 - today.weekday()) % 7  # Monday itself -> 0
    monday = today + timedelta(days=days_to_monday)
    return [today + timedelta(days=i) for i in range((monday - today).days + 1)]


# --- 2. Talk to the MCP server -------------------------------------------
async def fetch_all_forecasts(days: int) -> tuple[dict, str]:
    """Spawn the weather MCP server and call get_forecast_all + the chart tool."""
    server_params = StdioServerParameters(command=sys.executable, args=[str(SERVER_SCRIPT)])

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            forecast_result = await session.call_tool("get_forecast_all", arguments={"days": days})
            if forecast_result.is_error:
                raise RuntimeError(f"get_forecast_all failed: {forecast_result.content}")
            forecasts = json.loads(forecast_result.content[0].text)

            chart_result = await session.call_tool("render_temperature_chart", arguments={"days": days})
            if chart_result.is_error:
                raise RuntimeError(f"render_temperature_chart failed: {chart_result.content}")
            chart_text = chart_result.content[0].text

            return forecasts, chart_text


# --- 3. Match itinerary to forecast days -------------------------------
def itinerary_days(forecasts: dict, window: list[date]) -> list[dict]:
    """For each date in the window, pull the forecast for whichever
    city(ies) the itinerary says you're actually in that day.

    Returns a flat list of day dicts (one per city per date) enriched with
    'weekday', 'city', 'label' - both for building the Slack message and
    for feeding into recommend_packing().
    """
    entries = []
    for d in window:
        legs = WEEKLY_ITINERARY.get(d.weekday(), [])
        for city, label in legs:
            city_days = forecasts.get(city, {}).get("days", [])
            match = next((day for day in city_days if day["date"] == d.isoformat()), None)
            if match is None:
                continue  # city's forecast didn't cover this date, skip
            entries.append({**match, "weekday": d.strftime("%A"), "city": city, "label": label})
    return entries


# --- 4. Turn the itinerary-matched days into packing advice ----------------
def recommend_packing(days: list[dict]) -> list[str]:
    """Rule-based packing advice across every day/city you'll actually be
    in. Tune the thresholds to taste."""
    if not days:
        return ["No itinerary data for the coming days - check WEEKLY_ITINERARY."]

    temp_max = max(d["temp_max_c"] for d in days)
    temp_min = min(d["temp_min_c"] for d in days)
    feels_like_min = min(d["feels_like_min_c"] for d in days)
    total_rain = sum(d["precipitation_mm"] for d in days)
    max_code = max(d["max_weather_code"] for d in days)

    items = []

    if temp_max >= 24:
        items.append("Shorts are fine, but pack one pair of light trousers just in case")
    elif temp_max >= 17:
        items.append("Light trousers")
    else:
        items.append("Long trousers - it won't get warm enough for shorts")

    if feels_like_min <= 0:
        items.append("Thermal base layer")
    if temp_min <= 12:
        items.append("A warm sweater")
    if temp_min <= 5:
        items.append("A proper winter coat")
    elif temp_min <= 12:
        items.append("A light jacket for evenings/mornings")

    if total_rain >= 1 or (50 <= max_code <= 67) or (80 <= max_code <= 82):
        items.append("Rain jacket or umbrella - rain expected somewhere on the trip")

    if 71 <= max_code <= 77 or max_code in (85, 86):
        items.append("Snow is possible - waterproof boots and gloves")

    if temp_min <= -5:
        items.append("Hat and gloves - it's going to be properly cold")

    return items


# --- 5. Slack formatting ---------------------------------------------------
def format_slack_message(days: list[dict], packing: list[str], chart_text: str) -> dict:
    day_lines = []
    for d in days:
        label = f" ({d['label']})" if d["label"] else ""
        day_lines.append(
            f"*{d['weekday']} - {d['city']}{label}*: "
            f"{d['temp_min_c']}\u00b0C - {d['temp_max_c']}\u00b0C, "
            f"{d['precipitation_mm']}mm rain"
        )

    packing_lines = "\n".join(f"\u2022 {item}" for item in packing)

    return {
        "blocks": [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "\U0001f9f3 Packing forecast: Kerpen -> Nijmegen -> Den Bosch -> Kerpen",
                },
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "\n".join(day_lines)},
            },
            {"type": "divider"},
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Pack:*\n{packing_lines}"},
            },
            {"type": "divider"},
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"```\n{chart_text}\n```"},
            },
        ]
    }


# --- 6. Post to Slack -----------------------------------------------------
def post_to_slack(webhook_url: str, message: dict) -> None:
    resp = requests.post(webhook_url, json=message, timeout=10)
    resp.raise_for_status()


# --- Entrypoint -------------------------------------------------------
async def main() -> None:
    webhook_url = os.environ["SLACK_WEBHOOK_URL"]  # fail loudly if missing

    simulate = os.environ.get("SIMULATE_DATE")
    today = date.fromisoformat(simulate) if simulate else datetime.now(LOCAL_TZ).date()

    window = forecast_window(today)
    days_needed = len(window)

    forecasts, chart_text = await fetch_all_forecasts(days_needed)
    matched_days = itinerary_days(forecasts, window)
    packing = recommend_packing(matched_days)
    message = format_slack_message(matched_days, packing, chart_text)
    post_to_slack(webhook_url, message)

    print(f"Posted packing forecast for {today} .. {window[-1]} to Slack.")


if __name__ == "__main__":
    asyncio.run(main())
