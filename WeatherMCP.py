from typing import Any
import httpx2
from mcp.server import MCPServer
import openmeteo_requests
import pandas as pd
import requests_cache
from retry_requests import retry

# Initialize MCPServer
mcp = MCPServer("Weather")

# Constants
USER_AGENT = "Weather-app/1.0"
COORDINATES = {
    "Nijmegen": [51.8425593893818, 5.854625564074581],
    "Den Bosch": [51.69597345232003, 5.304554239967503],
    "Kerpen": [50.91634119102821, 6.711334903326844],
}
CITIES = COORDINATES.keys()

# Make sure all required weather variables are listed here
# The order of variables in hourly or daily is important to assign them correctly below
url = "https://api.open-meteo.com/v1/forecast"
params = {
	"latitude": [51.8425593893818, 51.69597345232003, 50.91634119102821],
	"longitude": [5.854625564074581, 5.304554239967503, 6.711334903326844],
	"hourly": ["temperature_2m", "apparent_temperature", "precipitation", "weather_code"],
	"timezone": "auto",
	"forecast_days": 3,
}

def get_params(latitude, longitude):
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "hourly": ["temperature_2m", "apparent_temperature", "precipitation", "weather_code"],
        "timezone": "auto",
        "forecast_days": 3,
    }

def process_location(response):
    print(f"\nCoordinates: {response.Latitude()}°N {response.Longitude()}°E")
    print(f"Elevation: {response.Elevation()} m asl")
    print(f"Timezone: {response.Timezone()}{response.TimezoneAbbreviation()}")
    print(f"Timezone difference to GMT+0: {response.UtcOffsetSeconds()}s")

    # Process hourly data. The order of variables needs to be the same as requested.
    hourly = response.Hourly()
    hourly_temperature_2m = hourly.Variables(0).ValuesAsNumpy()
    hourly_apparent_temperature = hourly.Variables(1).ValuesAsNumpy()
    hourly_precipitation = hourly.Variables(2).ValuesAsNumpy()
    hourly_weather_code = hourly.Variables(3).ValuesAsNumpy()

    hourly_data = {
        "date": pd.date_range(
            start=pd.to_datetime(hourly.Time(), unit="s", utc=True),
            end=pd.to_datetime(hourly.TimeEnd(), unit="s", utc=True),
            freq=pd.Timedelta(seconds=hourly.Interval()),
            inclusive="left"
        ).tz_convert(response.Timezone().decode())
    }

    hourly_data["temperature_2m"] = hourly_temperature_2m
    hourly_data["apparent_temperature"] = hourly_apparent_temperature
    hourly_data["precipitation"] = hourly_precipitation
    hourly_data["weather_code"] = hourly_weather_code

    hourly_dataframe = pd.DataFrame(data=hourly_data)
    return hourly_dataframe

def process_response(response):
    data = []
    for i, res in enumerate(response):
        data[CITIES[i]] = process_location(res)
    return pd.DataFrame(data)

@mcp.tool()
def get_Forecast_all():
    """ Returns the forecast for all locations as panda DataFrame """
    responses = openmeteo.weather_api(url, params=params)
    return process_response(responses)

@mcp.tool()
def get_Forecast_Nijmegen():
    """ Returns the forecast for only Nijmegen as panda DataFrame """
    params = get_params(COORDINATES["Nijmegen"])
    responses = openmeteo.weather_api(url, params=params)
    return process_location(responses[0])

@mcp.tool()
def get_Forecast_DenBosch():
    """ Returns the forecast for only Den Bosch as panda DataFrame """
    params = get_params(COORDINATES["Den Bosch"])
    responses = openmeteo.weather_api(url, params=params)
    return process_location(responses[0])

@mcp.tool()
def get_Forecast_Kerpen():
    """ Returns the forecast for only Kerpen as panda DataFrame """
    params = get_params(COORDINATES["Kerpen"])
    responses = openmeteo.weather_api(url, params=params)
    return process_location(responses[0])

if __name__ == "__main__":
    # Setup the Open-Meteo API client with cache and retry on error
    cache_session = requests_cache.CachedSession('.cache', expire_after=3600)
    retry_session = retry(cache_session, retries=5, backoff_factor=0.2)
    openmeteo = openmeteo_requests.Client(session=retry_session)

    mcp.run(transport="stdio")
