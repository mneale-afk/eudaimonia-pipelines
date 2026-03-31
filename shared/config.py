"""
Shared configuration for all Eudaimonia pipelines.

Defines the Firestore schema, source identifiers, and common settings.
"""

import os

# ---------------------------------------------------------------------------
# GCP
# ---------------------------------------------------------------------------
PROJECT_ID = os.environ.get("GCP_PROJECT")
TOKEN_BUCKET = os.environ.get("TOKEN_BUCKET", f"{PROJECT_ID}-eudaimonia-tokens")

# ---------------------------------------------------------------------------
# Firestore Schema
# ---------------------------------------------------------------------------
# Root collection — all pipelines write under this prefix.
#
# Schema:
#   eudaimonia/
#   ├── daily/{date}/                     ← all sources write here by date
#   │   ├── garmin_summary
#   │   ├── garmin_heart_rate
#   │   ├── garmin_sleep
#   │   ├── garmin_stress
#   │   ├── garmin_steps
#   │   ├── garmin_body_composition
#   │   ├── garmin_hydration
#   │   ├── garmin_spo2
#   │   ├── garmin_respiration
#   │   ├── garmin_hrv
#   │   ├── garmin_body_battery
#   │   ├── garmin_training_readiness
#   │   ├── garmin_training_status
#   │   ├── garmin_max_metrics
#   │   ├── garmin_blood_pressure
#   │   ├── garmin_floors
#   │   ├── mfp_nutrition
#   │   ├── mfp_meals
#   │   ├── withings_weight
#   │   ├── withings_body_composition
#   │   ├── openweather_current
#   │   └── gemini_analysis               ← written by the analysis job
#   ├── activities/items/{activity_id}     ← Garmin activities (enriched)
#   ├── profile/items/{source}_{key}       ← user profiles, devices, etc.
#   └── sync_log/items/{timestamp}_{src}   ← audit trail of every sync run
#
FIRESTORE_ROOT = os.environ.get("FIRESTORE_ROOT", "eudaimonia")

# ---------------------------------------------------------------------------
# Source identifiers — used as prefixes in Firestore document names.
# Keeps data from different sources clearly separated within the same
# date collection, while still making cross-source date queries trivial.
# ---------------------------------------------------------------------------
SOURCE_GARMIN = "garmin"
SOURCE_MFP = "mfp"
SOURCE_WITHINGS = "withings"
SOURCE_OPENWEATHER = "openweather"
SOURCE_GEMINI = "gemini"

# ---------------------------------------------------------------------------
# Default schedule lookback
# ---------------------------------------------------------------------------
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "1"))
