# Eudaimonia Pipelines

A multi-source health data platform that syncs data from Garmin, MyFitnessPal, Withings, and OpenWeather into Firestore, then uses Gemini to find correlations across all your health data.

## Architecture

```
┌─────────────┐  ┌──────────────┐  ┌───────────┐  ┌─────────────┐
│   Garmin     │  │ MyFitnessPal │  │  Withings  │  │ OpenWeather │
│  (6:00 AM)   │  │  (6:30 AM)   │  │ (7:00 AM)  │  │  (5:00 AM)  │
└──────┬───────┘  └──────┬───────┘  └─────┬──────┘  └──────┬──────┘
       │                 │                │                │
       └────────┬────────┴───────┬────────┘                │
                │                │                         │
                ▼                ▼                         ▼
        ┌─────────────────────────────────────────────────────┐
        │             Firestore (unified schema)               │
        │  eudaimonia/daily/{date}/{source}_{metric}           │
        └──────────────────────┬──────────────────────────────┘
                               │ Firestore trigger
                               ▼
                    ┌──────────────────────┐
                    │   Gemini Analysis     │
                    │  (event-driven)       │
                    └──────────────────────┘
```

## Project Structure

```
eudaimonia-pipelines/
├── shared/                      ← Common code (copied into each function at deploy)
│   ├── config.py                  Schema constants, source IDs, env config
│   ├── firestore_client.py        Unified Firestore writer
│   ├── secrets.py                 Secret Manager helper
│   └── token_store.py             Cloud Storage token persistence
├── pipelines/
│   ├── garmin/                  ← Garmin Connect sync
│   │   ├── main.py                16 daily metrics + activities + profile
│   │   ├── garmin_client.py       Auth wrapper with token persistence
│   │   └── requirements.txt
│   ├── myfitnesspal/            ← MFP nutrition scraper
│   │   ├── main.py                Nutrition totals, meals, water
│   │   └── requirements.txt
│   ├── withings/                ← Withings body scale
│   │   ├── main.py                Weight, body comp, 20+ measure types
│   │   └── requirements.txt
│   ├── openweather/             ← Weather & air quality
│   │   ├── main.py                Current weather, AQI, optional forecast
│   │   └── requirements.txt
│   └── gemini-analysis/         ← AI correlation engine
│       ├── main.py                Firestore-triggered, cross-source analysis
│       └── requirements.txt
├── deploy.sh                    ← One-command deploy for any/all pipelines
└── SETUP.md
```

## Firestore Schema

All sources write into a unified schema, making cross-source queries trivial:

```
eudaimonia/
├── daily/{date}/                        ← One subcollection per date
│   ├── garmin_summary                     Daily totals (steps, calories, distance...)
│   ├── garmin_heart_rate                  Resting HR, HR zones, time series
│   ├── garmin_sleep                       Sleep stages, duration, score
│   ├── garmin_stress                      Stress levels throughout the day
│   ├── garmin_steps                       Step count breakdown
│   ├── garmin_body_composition            Body fat %, muscle mass, etc.
│   ├── garmin_hydration                   Water intake
│   ├── garmin_spo2                        Blood oxygen
│   ├── garmin_respiration                 Breathing rate
│   ├── garmin_hrv                         Heart rate variability
│   ├── garmin_body_battery                Garmin Body Battery score
│   ├── garmin_training_readiness          Training readiness score
│   ├── garmin_training_status             Training load, VO2max trend
│   ├── garmin_max_metrics                 VO2max, recovery time
│   ├── garmin_blood_pressure              BP readings
│   ├── garmin_floors                      Floors climbed
│   ├── mfp_nutrition                      Calories, macros (P/C/F), goals
│   ├── mfp_meals                          Breakfast, lunch, dinner, snacks
│   ├── mfp_water                          Water logged in MFP
│   ├── withings_body                      Weight, body fat, muscle, bone mass
│   ├── openweather_current                Temp, humidity, pressure, wind
│   ├── openweather_air_quality            AQI, PM2.5, PM10, pollutants
│   └── gemini_analysis                    AI-generated correlations & insights
├── activities/items/{activity_id}       ← Enriched Garmin activities
├── profile/items/{source}_{key}         ← User profiles, devices, records
└── sync_log/items/{timestamp}_{source}  ← Audit trail
```

## Quick Start

### 1. Prerequisites

- Google Cloud project with billing enabled
- `gcloud` CLI installed and authenticated
- Accounts: Garmin Connect, MyFitnessPal, Withings, OpenWeather

### 2. Create secrets

```bash
PROJECT=your-project-id

# Garmin
echo -n 'your@email.com' | gcloud secrets create garmin-email --data-file=- --project=$PROJECT
echo -n 'your_password'   | gcloud secrets create garmin-password --data-file=- --project=$PROJECT

# MyFitnessPal
echo -n 'mfp_username' | gcloud secrets create mfp-username --data-file=- --project=$PROJECT
echo -n 'mfp_password' | gcloud secrets create mfp-password --data-file=- --project=$PROJECT

# Withings (OAuth2 — get these from https://developer.withings.com)
echo -n 'client_id'     | gcloud secrets create withings-client-id --data-file=- --project=$PROJECT
echo -n 'client_secret' | gcloud secrets create withings-client-secret --data-file=- --project=$PROJECT
echo -n 'refresh_token' | gcloud secrets create withings-refresh-token --data-file=- --project=$PROJECT

# OpenWeather (get from https://openweathermap.org/api)
echo -n 'your_api_key' | gcloud secrets create openweather-api-key --data-file=- --project=$PROJECT

# Gemini
echo -n 'your_gemini_key' | gcloud secrets create gemini-api-key --data-file=- --project=$PROJECT
```

### 3. Deploy

```bash
chmod +x deploy.sh

# Deploy everything
./deploy.sh your-project-id all

# Or deploy individually
./deploy.sh your-project-id garmin
./deploy.sh your-project-id openweather
```

### 4. Test

```bash
# Trigger a single pipeline
gcloud scheduler jobs run eudaimonia-garmin-sync --location=us-central1 --project=$PROJECT

# Backfill 30 days of Garmin data
curl -X POST \
  -H "Authorization: Bearer $(gcloud auth print-identity-token)" \
  -H "Content-Type: application/json" \
  -d '{"lookback_days": 30}' \
  GARMIN_FUNCTION_URL
```

## Schedules

| Pipeline       | Schedule    | Function Name             |
|----------------|-------------|---------------------------|
| OpenWeather    | 5:00 AM     | eudaimonia-openweather    |
| Garmin         | 6:00 AM     | eudaimonia-garmin         |
| MyFitnessPal   | 6:30 AM     | eudaimonia-myfitnesspal   |
| Withings       | 7:00 AM     | eudaimonia-withings       |
| Gemini Analysis| Event-driven| eudaimonia-gemini-analysis|

The staggered schedule ensures data arrives sequentially. Gemini analysis
fires automatically once at least 2 sources have data for a given date,
so it typically runs after the second pipeline completes.

## How the Gemini Analysis Works

1. Any pipeline writes data to `eudaimonia/daily/{date}/*`
2. A Firestore trigger fires `eudaimonia-gemini-analysis`
3. The function checks how many distinct sources have data for that date
4. Once the threshold is met (default: 2 sources), it collects all data
5. Sends everything to Gemini with a structured analysis prompt
6. Writes the analysis back to `eudaimonia/daily/{date}/gemini_analysis`
7. Skips re-analysis if the same sources were already analyzed
8. Re-runs if new sources have arrived since the last analysis

## Monitoring

```bash
# View logs for a specific pipeline
gcloud functions logs read eudaimonia-garmin --gen2 --region=us-central1 --project=$PROJECT

# View all sync logs in Firestore
# Navigate to: Firestore → eudaimonia → sync_log → items
```

## Adding a New Data Source

1. Create `pipelines/your-source/main.py` and `requirements.txt`
2. Use the shared modules: `from config import ...` and `from firestore_client import FirestoreWriter`
3. Write data with: `writer.write_daily("yoursource", "metric_name", date_str, data)`
4. Add the entry point and schedule to `deploy.sh`
5. Add `"yoursource"` to `EXPECTED_SOURCES` in `gemini-analysis/main.py`
6. Deploy: `./deploy.sh your-project-id your-source`
