"""
OpenWeather → Firestore sync pipeline.
Cloud Function (2nd Gen) entry point.

Fetches current weather and daily forecast for your location,
writing it to the unified Eudaimonia Firestore schema.

Useful for correlating weather (temperature, humidity, UV, pressure)
with health metrics like sleep quality, HRV, stress, etc.

API docs: https://openweathermap.org/api
"""

import functions_framework
import json
import logging
import os
from datetime import date, datetime, timezone

import requests

from config import SOURCE_OPENWEATHER, PROJECT_ID, LOOKBACK_DAYS
from firestore_client import FirestoreWriter
from secrets import get_secret

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Secret / config
API_KEY_SECRET = "OPENWEATHER_API_KEY"
# Default to Sydney, override via env vars
LATITUDE = float(os.environ.get("WEATHER_LAT", "-33.8688"))
LONGITUDE = float(os.environ.get("WEATHER_LON", "151.2093"))

ONECALL_URL = "https://api.openweathermap.org/data/3.0/onecall"
CURRENT_URL = "https://api.openweathermap.org/data/2.5/weather"


@functions_framework.http
def sync_openweather(request):
    """HTTP Cloud Function entry point."""
    try:
        writer = FirestoreWriter()
        api_key = get_secret(PROJECT_ID, API_KEY_SECRET)

        results = {}

        # --- Current weather ---
        current = _fetch_current(api_key)
        if current:
            today_str = date.today().isoformat()
            weather_data = _extract_current(current)
            writer.write_daily(SOURCE_OPENWEATHER, "current", today_str, weather_data)
            results["current"] = True

        # --- One Call API (if you have a subscription) ---
        # Includes hourly, daily forecast, and historical data.
        # Uncomment if you have the One Call 3.0 subscription.
        #
        # onecall = _fetch_onecall(api_key)
        # if onecall:
        #     today_str = date.today().isoformat()
        #     writer.write_daily(SOURCE_OPENWEATHER, "forecast", today_str, onecall)
        #     results["forecast"] = True

        # --- Air Quality ---
        air = _fetch_air_quality(api_key)
        if air:
            today_str = date.today().isoformat()
            writer.write_daily(SOURCE_OPENWEATHER, "air_quality", today_str, air)
            results["air_quality"] = True

        writer.log_sync(SOURCE_OPENWEATHER, "ok", results)
        return (json.dumps({"status": "ok", "results": results}), 200)

    except Exception as e:
        logger.exception("OpenWeather sync failed")
        writer = FirestoreWriter()
        writer.log_sync(SOURCE_OPENWEATHER, "error", {"message": str(e)})
        return (json.dumps({"status": "error", "message": str(e)}), 500)


def _fetch_current(api_key: str) -> dict | None:
    """Fetch current weather from OpenWeather API."""
    try:
        resp = requests.get(
            CURRENT_URL,
            params={
                "lat": LATITUDE,
                "lon": LONGITUDE,
                "appid": api_key,
                "units": "metric",
            },
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.warning(f"Current weather fetch failed: {e}")
        return None


def _extract_current(data: dict) -> dict:
    """Extract key weather fields into a flat, analysis-friendly structure."""
    main = data.get("main", {})
    wind = data.get("wind", {})
    weather = data.get("weather", [{}])[0]
    clouds = data.get("clouds", {})

    return {
        "temperature_c": main.get("temp"),
        "feels_like_c": main.get("feels_like"),
        "temp_min_c": main.get("temp_min"),
        "temp_max_c": main.get("temp_max"),
        "humidity_pct": main.get("humidity"),
        "pressure_hpa": main.get("pressure"),
        "wind_speed_ms": wind.get("speed"),
        "wind_gust_ms": wind.get("gust"),
        "wind_deg": wind.get("deg"),
        "clouds_pct": clouds.get("all"),
        "condition": weather.get("main"),
        "description": weather.get("description"),
        "visibility_m": data.get("visibility"),
        "sunrise": data.get("sys", {}).get("sunrise"),
        "sunset": data.get("sys", {}).get("sunset"),
        "timestamp": data.get("dt"),
        "_raw": data,
    }


def _fetch_air_quality(api_key: str) -> dict | None:
    """Fetch current air quality index."""
    try:
        resp = requests.get(
            "https://api.openweathermap.org/data/2.5/air_pollution",
            params={
                "lat": LATITUDE,
                "lon": LONGITUDE,
                "appid": api_key,
            },
        )
        resp.raise_for_status()
        body = resp.json()

        item = body.get("list", [{}])[0]
        components = item.get("components", {})
        aqi = item.get("main", {}).get("aqi")

        return {
            "aqi": aqi,
            "aqi_label": {1: "Good", 2: "Fair", 3: "Moderate", 4: "Poor", 5: "Very Poor"}.get(aqi, "Unknown"),
            "pm2_5": components.get("pm2_5"),
            "pm10": components.get("pm10"),
            "co": components.get("co"),
            "no2": components.get("no2"),
            "o3": components.get("o3"),
            "so2": components.get("so2"),
            "_raw": body,
        }
    except Exception as e:
        logger.warning(f"Air quality fetch failed: {e}")
        return None


def _fetch_onecall(api_key: str) -> dict | None:
    """Fetch One Call 3.0 data (requires paid subscription)."""
    try:
        resp = requests.get(
            ONECALL_URL,
            params={
                "lat": LATITUDE,
                "lon": LONGITUDE,
                "appid": api_key,
                "units": "metric",
                "exclude": "minutely",
            },
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.warning(f"One Call fetch failed: {e}")
        return None
