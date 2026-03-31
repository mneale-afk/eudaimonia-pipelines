"""
MyFitnessPal → Firestore sync pipeline.
Cloud Function (2nd Gen) entry point.

Scrapes nutrition and meal data from MyFitnessPal and writes it
to the unified Eudaimonia Firestore schema.

NOTE: MyFitnessPal shut down their public API. This pipeline uses
web scraping via the `myfitnesspal` Python library (or a custom scraper).
You may need to adjust the scraper based on current MFP site changes.

Popular libraries:
  - https://github.com/coddingtonbear/python-myfitnesspal
  - Custom Selenium/Playwright scraper if the above breaks

TODO: Implement your preferred MFP scraping approach below.
"""

import functions_framework
import json
import logging
from datetime import date, timedelta

from config import SOURCE_MFP, LOOKBACK_DAYS, PROJECT_ID
from firestore_client import FirestoreWriter
from gcp_secrets import get_secret

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Secret names in Secret Manager
MFP_USERNAME_SECRET = "MFP_EMAIL"
MFP_PASSWORD_SECRET = "MFP_PASSWORD"


@functions_framework.http
def sync_mfp(request):
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

        writer = FirestoreWriter()

        # --- Authenticate to MFP ---
        username = get_secret(PROJECT_ID, MFP_USERNAME_SECRET)
        password = get_secret(PROJECT_ID, MFP_PASSWORD_SECRET)
        mfp_client = _create_mfp_client(username, password)

        results = {}
        for sync_date in dates_to_sync:
            date_str = sync_date.isoformat()
            day_results = _sync_date(mfp_client, writer, sync_date)
            results[date_str] = day_results

        writer.log_sync(SOURCE_MFP, "ok", results)
        return (json.dumps({"status": "ok", "results": results}), 200)

    except Exception as e:
        logger.exception("MFP sync failed")
        writer = FirestoreWriter()
        writer.log_sync(SOURCE_MFP, "error", {"message": str(e)})
        return (json.dumps({"status": "error", "message": str(e)}), 500)


def _create_mfp_client(username: str, password: str):
    """
    Create and authenticate an MFP client.

    Option A — python-myfitnesspal library:
        import myfitnesspal
        return myfitnesspal.Client(username, password)

    Option B — Custom scraper:
        from mfp_scraper import MFPScraper
        return MFPScraper(username, password)

    Uncomment your preferred approach below.
    """
    import myfitnesspal
    client = myfitnesspal.Client(username, password)
    return client


def _sync_date(mfp_client, writer: FirestoreWriter, sync_date: date) -> dict:
    """Sync nutrition data for a single date."""
    date_str = sync_date.isoformat()
    results = {}

    try:
        # Get daily nutrition totals
        day = mfp_client.get_date(sync_date)

        # Totals (calories, protein, carbs, fat, etc.)
        totals = day.totals
        goals = day.goals

        nutrition_data = {
            "totals": totals,
            "goals": goals,
            "date": date_str,
        }
        writer.write_daily(SOURCE_MFP, "nutrition", date_str, nutrition_data)
        results["nutrition"] = True

        # Individual meals (breakfast, lunch, dinner, snacks)
        meals_data = {}
        for meal in day.meals:
            meal_entries = []
            for entry in meal.entries:
                meal_entries.append({
                    "name": entry.name,
                    "nutrition": entry.nutrition_information,
                })
            meals_data[meal.name] = meal_entries

        writer.write_daily(SOURCE_MFP, "meals", date_str, {"meals": meals_data})
        results["meals"] = True

    except Exception as e:
        logger.warning(f"MFP sync failed for {date_str}: {e}")

    # Water intake
    try:
        water = mfp_client.get_water(sync_date)
        writer.write_daily(SOURCE_MFP, "water", date_str, {"water_ml": water})
        results["water"] = True
    except Exception as e:
        logger.warning(f"MFP water failed for {date_str}: {e}")

    return results
