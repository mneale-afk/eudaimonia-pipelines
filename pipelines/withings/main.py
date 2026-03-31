"""
Withings Body Scale → Firestore sync pipeline.
Cloud Function (2nd Gen) entry point.

Fetches weight, body composition, and other measurements from the
Withings Health API and writes them to the unified Eudaimonia Firestore schema.

Withings uses OAuth2 — tokens are persisted in Cloud Storage between invocations.

API docs: https://developer.withings.com/api-reference
"""

import functions_framework
import json
import logging
import os
from datetime import date, datetime, timedelta, timezone

import requests

from config import SOURCE_WITHINGS, PROJECT_ID, LOOKBACK_DAYS
from firestore_client import FirestoreWriter
from secrets import get_secret
import token_store

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Withings API endpoints
WITHINGS_TOKEN_URL = "https://wbsapi.withings.net/v2/oauth2"
WITHINGS_MEASURE_URL = "https://wbsapi.withings.net/measure"

# Secret Manager secrets
CLIENT_ID_SECRET = "WITHINGS_CLIENT_ID"
CLIENT_SECRET_SECRET = "WITHINGS_CLIENT_SECRET"
REFRESH_TOKEN_SECRET = "WITHINGS_REFRESH_TOKEN"

# Withings measure types
MEASURE_TYPES = {
    1: "weight_kg",
    4: "height_m",
    5: "fat_free_mass_kg",
    6: "fat_ratio_pct",
    8: "fat_mass_weight_kg",
    9: "diastolic_bp",
    10: "systolic_bp",
    11: "heart_pulse_bpm",
    12: "temperature_c",
    54: "spo2_pct",
    71: "body_temperature_c",
    73: "skin_temperature_c",
    76: "muscle_mass_kg",
    77: "hydration_kg",
    88: "bone_mass_kg",
    91: "pulse_wave_velocity",
    122: "electrodermal_activity",
    123: "vo2max",
    130: "atrial_fibrillation",
    135: "fat_mass_segments",
    136: "muscle_mass_segments",
    137: "vascular_age",
    138: "nerve_health_score",
}


@functions_framework.http
def sync_withings(request):
    """HTTP Cloud Function entry point."""
    try:
        request_json = request.get_json(silent=True) or {}
        lookback = int(request_json.get("lookback_days", LOOKBACK_DAYS))

        writer = FirestoreWriter()
        access_token = _get_access_token()

        # Fetch measurements
        today = date.today()
        start_date = today - timedelta(days=lookback)

        measurements = _fetch_measurements(access_token, start_date, today)
        results = _process_measurements(measurements, writer)

        writer.log_sync(SOURCE_WITHINGS, "ok", results)
        return (json.dumps({"status": "ok", "results": results}), 200)

    except Exception as e:
        logger.exception("Withings sync failed")
        writer = FirestoreWriter()
        writer.log_sync(SOURCE_WITHINGS, "error", {"message": str(e)})
        return (json.dumps({"status": "error", "message": str(e)}), 500)


def _get_access_token() -> str:
    """
    Get a valid Withings access token.
    Uses the refresh token from Secret Manager to obtain a new access token.
    """
    client_id = get_secret(PROJECT_ID, CLIENT_ID_SECRET)
    client_secret = get_secret(PROJECT_ID, CLIENT_SECRET_SECRET)
    refresh_token = get_secret(PROJECT_ID, REFRESH_TOKEN_SECRET)

    response = requests.post(
        WITHINGS_TOKEN_URL,
        data={
            "action": "requesttoken",
            "grant_type": "refresh_token",
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
        },
    )
    response.raise_for_status()
    body = response.json()

    if body.get("status") != 0:
        raise RuntimeError(f"Withings token refresh failed: {body}")

    # TODO: If the refresh token rotates, update it in Secret Manager.
    # new_refresh = body["body"]["refresh_token"]
    return body["body"]["access_token"]


def _fetch_measurements(access_token: str, start: date, end: date) -> list:
    """Fetch measurement groups from Withings API."""
    start_ts = int(datetime.combine(start, datetime.min.time()).replace(tzinfo=timezone.utc).timestamp())
    end_ts = int(datetime.combine(end, datetime.max.time()).replace(tzinfo=timezone.utc).timestamp())

    response = requests.post(
        WITHINGS_MEASURE_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        data={
            "action": "getmeas",
            "startdate": start_ts,
            "enddate": end_ts,
        },
    )
    response.raise_for_status()
    body = response.json()

    if body.get("status") != 0:
        raise RuntimeError(f"Withings API error: {body}")

    return body.get("body", {}).get("measuregrps", [])


def _process_measurements(measure_groups: list, writer: FirestoreWriter) -> dict:
    """
    Process Withings measurement groups and write to Firestore.
    Groups measurements by date, then writes one document per date.
    """
    # Group by date
    by_date: dict[str, dict] = {}

    for grp in measure_groups:
        ts = grp.get("date", 0)
        grp_date = datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()

        if grp_date not in by_date:
            by_date[grp_date] = {"measurements": [], "raw_groups": []}

        by_date[grp_date]["raw_groups"].append(grp)

        for measure in grp.get("measures", []):
            mtype = measure.get("type")
            value = measure.get("value", 0) * (10 ** measure.get("unit", 0))
            name = MEASURE_TYPES.get(mtype, f"unknown_{mtype}")

            by_date[grp_date]["measurements"].append({
                "type": name,
                "value": round(value, 4),
                "timestamp": ts,
            })

    # Write to Firestore
    results = {}
    for date_str, data in by_date.items():
        # Extract key metrics for top-level fields
        flat = {m["type"]: m["value"] for m in data["measurements"]}
        flat["_all_measurements"] = data["measurements"]

        writer.write_daily(SOURCE_WITHINGS, "body", date_str, flat)
        results[date_str] = list(flat.keys())

    return results
