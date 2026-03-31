"""
Garmin Connect → Firestore sync pipeline.
Cloud Function (2nd Gen) entry point.

Fetches all available health/fitness data from Garmin Connect
and writes it to the unified Eudaimonia Firestore schema.
"""

import functions_framework
import json
import logging
from datetime import date, timedelta

from config import SOURCE_GARMIN, LOOKBACK_DAYS
from firestore_client import FirestoreWriter
from garmin_client import GarminClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Metrics to sync — each maps to a garminconnect method and accepts a date str.
DAILY_METRICS = [
    ("summary",            "get_stats"),
    ("heart_rate",         "get_heart_rates"),
    ("sleep",              "get_sleep_data"),
    ("body_composition",   "get_body_composition"),
    ("stress",             "get_stress_data"),
    ("steps",              "get_steps_data"),
    ("hydration",          "get_hydration_data"),
    ("spo2",               "get_spo2_data"),
    ("respiration",        "get_respiration_data"),
    ("hrv",                "get_hrv_data"),
    ("body_battery",       "get_body_battery"),
    ("training_readiness", "get_training_readiness"),
    ("training_status",    "get_training_status"),
    ("max_metrics",        "get_max_metrics"),
    ("blood_pressure",     "get_blood_pressure"),
    ("floors",             "get_floors"),
]


@functions_framework.http
def sync_garmin(request):
    """HTTP Cloud Function entry point."""
    try:
        request_json = request.get_json(silent=True) or {}
        target_date_str = request_json.get("date")
        lookback = int(request_json.get("lookback_days", LOOKBACK_DAYS))

        if target_date_str:
            dates_to_sync = [date.fromisoformat(target_date_str)]
        else:
            today = date.today()
            dates_to_sync = [today - timedelta(days=i) for i in range(lookback)]

        garmin = GarminClient()
        writer = FirestoreWriter()

        garmin.authenticate()

        results = {}
        for sync_date in dates_to_sync:
            day_results = _sync_date(garmin, writer, sync_date)
            results[sync_date.isoformat()] = day_results

        activity_result = _sync_activities(garmin, writer)
        results["activities"] = activity_result

        _sync_profile(garmin, writer)

        garmin.save_tokens()

        writer.log_sync(SOURCE_GARMIN, "ok", results)
        return (json.dumps({"status": "ok", "results": results}), 200)

    except Exception as e:
        logger.exception("Garmin sync failed")
        writer = FirestoreWriter()
        writer.log_sync(SOURCE_GARMIN, "error", {"message": str(e)})
        return (json.dumps({"status": "error", "message": str(e)}), 500)


def _sync_date(garmin: GarminClient, writer: FirestoreWriter, sync_date: date) -> dict:
    """Sync all daily metrics for a single date."""
    date_str = sync_date.isoformat()
    results = {}

    for metric_name, method_name in DAILY_METRICS:
        data = garmin.safe_call(method_name, date_str)
        if data:
            writer.write_daily(SOURCE_GARMIN, metric_name, date_str, data)
            results[metric_name] = True

    return results


def _sync_activities(garmin: GarminClient, writer: FirestoreWriter) -> dict:
    """Sync recent activities with full detail."""
    results = {"synced": 0, "skipped": 0}

    activities = garmin.safe_call("get_activities", 0, 20)
    if not activities:
        return results

    for activity in activities:
        activity_id = str(activity.get("activityId"))
        if not activity_id:
            continue

        if writer.document_exists("activities", activity_id):
            results["skipped"] += 1
            continue

        detail = garmin.safe_call("get_activity", activity_id)
        splits = garmin.safe_call("get_activity_splits", activity_id)
        hr_zones = garmin.safe_call("get_activity_hr_in_timezones", activity_id)
        weather = garmin.safe_call("get_activity_weather", activity_id)

        enriched = {
            "summary": activity,
            "detail": detail,
            "splits": splits,
            "hr_zones": hr_zones,
            "weather": weather,
            "_source": SOURCE_GARMIN,
            "_synced_at": writer.server_timestamp(),
        }

        writer.write_document("activities", activity_id, enriched)
        results["synced"] += 1

    return results


def _sync_profile(garmin: GarminClient, writer: FirestoreWriter):
    """Sync user profile and device info."""
    profile = garmin.safe_call("get_user_summary")
    if profile:
        writer.write_document("profile", f"{SOURCE_GARMIN}_user", profile)

    devices = garmin.safe_call("get_devices")
    if devices:
        writer.write_document("profile", f"{SOURCE_GARMIN}_devices", {"devices": devices})

    records = garmin.safe_call("get_personal_record")
    if records:
        writer.write_document("profile", f"{SOURCE_GARMIN}_records", {"records": records})
