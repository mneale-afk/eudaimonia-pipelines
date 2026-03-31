#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Eudaimonia Pipelines — Unified Deployment Script
# ---------------------------------------------------------------------------
# Usage:
#   ./deploy.sh <PROJECT_ID> <PIPELINE> [REGION]
#
# Examples:
#   ./deploy.sh my-project garmin              # Deploy Garmin pipeline
#   ./deploy.sh my-project openweather         # Deploy OpenWeather pipeline
#   ./deploy.sh my-project gemini-analysis     # Deploy Gemini analysis
#   ./deploy.sh my-project all                 # Deploy everything
#
# The script copies shared/ modules into each pipeline directory before
# deploying, then cleans up. This is required because Cloud Functions
# can only deploy from a single flat directory.
# ---------------------------------------------------------------------------
set -euo pipefail

PROJECT_ID="${1:?Usage: ./deploy.sh <PROJECT_ID> <PIPELINE|all> [REGION]}"
PIPELINE="${2:?Usage: ./deploy.sh <PROJECT_ID> <PIPELINE|all> [REGION]}"
REGION="${3:-us-central1}"
TOKEN_BUCKET="${PROJECT_ID}-eudaimonia-tokens"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SHARED_DIR="${SCRIPT_DIR}/shared"
PIPELINES_DIR="${SCRIPT_DIR}/pipelines"

# Pipeline configs: name → entry_point, trigger_type, schedule, extra_env
declare -A ENTRY_POINTS=(
    [garmin]="sync_garmin"
    [myfitnesspal]="sync_mfp"
    [withings]="sync_withings"
    [openweather]="sync_openweather"
    [gemini-analysis]="on_daily_write"
)

declare -A SCHEDULES=(
    [garmin]="0 * * * *"
    [myfitnesspal]="30 6 * * *"
    [withings]="0 7 * * *"
    [openweather]="0 5 * * *"
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

enable_apis() {
    echo "--- Enabling required APIs ---"
    gcloud services enable \
        cloudfunctions.googleapis.com \
        cloudscheduler.googleapis.com \
        secretmanager.googleapis.com \
        firestore.googleapis.com \
        storage.googleapis.com \
        cloudbuild.googleapis.com \
        run.googleapis.com \
        eventarc.googleapis.com \
        --project="${PROJECT_ID}"
}

create_token_bucket() {
    echo "--- Ensuring token bucket exists ---"
    gsutil ls -b "gs://${TOKEN_BUCKET}" 2>/dev/null || \
        gsutil mb -p "${PROJECT_ID}" -l "${REGION}" "gs://${TOKEN_BUCKET}"
}

deploy_pipeline() {
    local name="$1"
    local pipeline_dir="${PIPELINES_DIR}/${name}"
    local entry_point="${ENTRY_POINTS[$name]}"
    local function_name="eudaimonia-${name}"

    if [ ! -d "${pipeline_dir}" ]; then
        echo "ERROR: Pipeline directory not found: ${pipeline_dir}"
        return 1
    fi

    echo ""
    echo "============================================"
    echo " Deploying: ${name}"
    echo "============================================"

    # --- Copy shared modules into the pipeline directory ---
    echo "--- Copying shared modules ---"
    for f in "${SHARED_DIR}"/*.py; do
        cp "$f" "${pipeline_dir}/"
    done

    # --- Create .env.yaml ---
    cat > "${pipeline_dir}/.env.yaml" <<EOF
GCP_PROJECT: "${PROJECT_ID}"
TOKEN_BUCKET: "${TOKEN_BUCKET}"
FIRESTORE_ROOT: "eudaimonia"
LOOKBACK_DAYS: "1"
EOF

    # --- Deploy ---
    if [ "${name}" = "gemini-analysis" ]; then
        # Firestore-triggered function
        echo "--- Deploying as Firestore-triggered function ---"
        gcloud functions deploy "${function_name}" \
            --gen2 \
            --region="${REGION}" \
            --runtime=python312 \
            --source="${pipeline_dir}" \
            --entry-point="${entry_point}" \
            --trigger-event-filters="type=google.cloud.firestore.document.v1.written" \
            --trigger-event-filters="database=(default)" \
            --trigger-event-filters-path-pattern="document=eudaimonia/daily/{date}/{doc}" \
            --memory=1GiB \
            --timeout=300s \
            --env-vars-file="${pipeline_dir}/.env.yaml" \
            --project="${PROJECT_ID}"
    else
        # HTTP-triggered function (called by Cloud Scheduler)
        echo "--- Deploying as HTTP function ---"
        gcloud functions deploy "${function_name}" \
            --gen2 \
            --region="${REGION}" \
            --runtime=python312 \
            --source="${pipeline_dir}" \
            --entry-point="${entry_point}" \
            --trigger-http \
            --no-allow-unauthenticated \
            --memory=512MiB \
            --timeout=540s \
            --env-vars-file="${pipeline_dir}/.env.yaml" \
            --project="${PROJECT_ID}"

        # --- Get function URL and set up scheduler ---
        local function_url
        function_url=$(gcloud functions describe "${function_name}" \
            --gen2 --region="${REGION}" --project="${PROJECT_ID}" \
            --format='value(serviceConfig.uri)')

        local sa_email
        sa_email=$(gcloud functions describe "${function_name}" \
            --gen2 --region="${REGION}" --project="${PROJECT_ID}" \
            --format='value(serviceConfig.serviceAccountEmail)')

        # --- IAM: Secret Manager access ---
        echo "--- Granting IAM permissions ---"
        _grant_secret_access "${sa_email}"
        gsutil iam ch "serviceAccount:${sa_email}:objectAdmin" "gs://${TOKEN_BUCKET}" 2>/dev/null || true

        # --- Cloud Scheduler ---
        if [ -n "${SCHEDULES[$name]:-}" ]; then
            local job_name="eudaimonia-${name}-sync"
            local schedule="${SCHEDULES[$name]}"

            echo "--- Setting up scheduler: ${schedule} ---"
            if gcloud scheduler jobs describe "${job_name}" --location="${REGION}" --project="${PROJECT_ID}" &>/dev/null; then
                gcloud scheduler jobs update http "${job_name}" \
                    --location="${REGION}" \
                    --schedule="${schedule}" \
                    --uri="${function_url}" \
                    --http-method=POST \
                    --body='{"lookback_days": 1}' \
                    --headers="Content-Type=application/json" \
                    --oidc-service-account-email="${sa_email}" \
                    --oidc-token-audience="${function_url}" \
                    --project="${PROJECT_ID}"
            else
                gcloud scheduler jobs create http "${job_name}" \
                    --location="${REGION}" \
                    --schedule="${schedule}" \
                    --uri="${function_url}" \
                    --http-method=POST \
                    --body='{"lookback_days": 1}' \
                    --headers="Content-Type=application/json" \
                    --oidc-service-account-email="${sa_email}" \
                    --oidc-token-audience="${function_url}" \
                    --project="${PROJECT_ID}"
            fi

            echo "Scheduler: ${job_name} → ${schedule}"
        fi

        echo "Function URL: ${function_url}"
    fi

    # --- Clean up shared modules from pipeline dir ---
    echo "--- Cleaning up shared modules ---"
    for f in "${SHARED_DIR}"/*.py; do
        rm -f "${pipeline_dir}/$(basename "$f")"
    done
    rm -f "${pipeline_dir}/.env.yaml"

    echo "--- ${name} deployed successfully ---"
}

_grant_secret_access() {
    local sa="$1"
    local secrets=(
        GARMIN_EMAIL GARMIN_PASSWORD GARMIN_OAUTH_B64
        MFP_EMAIL MFP_PASSWORD
        OPENWEATHER_API_KEY
        WITHINGS_CLIENT_ID WITHINGS_CLIENT_SECRET WITHINGS_REFRESH_TOKEN
        GEMINI_API_KEY
    )
    for secret in "${secrets[@]}"; do
        if gcloud secrets describe "${secret}" --project="${PROJECT_ID}" &>/dev/null; then
            gcloud secrets add-iam-policy-binding "${secret}" \
                --member="serviceAccount:${sa}" \
                --role="roles/secretmanager.secretAccessor" \
                --project="${PROJECT_ID}" \
                --quiet 2>/dev/null || true
        fi
    done
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

enable_apis
create_token_bucket

if [ "${PIPELINE}" = "all" ]; then
    for name in garmin myfitnesspal withings openweather gemini-analysis; do
        deploy_pipeline "${name}"
    done
else
    deploy_pipeline "${PIPELINE}"
fi

echo ""
echo "============================================"
echo " Deployment complete!"
echo "============================================"
echo ""
echo "Schedules:"
echo "  garmin          → Hourly (top of every hour)"
echo "  myfitnesspal    → 6:30 AM daily"
echo "  withings        → 7:00 AM daily"
echo "  openweather     → 5:00 AM daily"
echo "  gemini-analysis → triggered by Firestore writes"
echo ""
echo "To test a pipeline manually:"
echo "  gcloud scheduler jobs run eudaimonia-garmin-sync --location=${REGION} --project=${PROJECT_ID}"
echo ""
