"""
Gemini Correlation Analysis — Firestore-triggered Cloud Function.

Fires whenever new data lands in the daily collection. Collects all
available data for that date across all sources and sends it to
Gemini for correlation analysis.

Uses a debounce mechanism: waits for data from multiple sources to
arrive before running analysis, to avoid redundant Gemini calls.
"""

import functions_framework
import json
import logging
import os
from datetime import datetime, timezone

from google.cloud import firestore
import google.generativeai as genai

from config import FIRESTORE_ROOT, PROJECT_ID, SOURCE_GEMINI
from firestore_client import FirestoreWriter
from secrets import get_secret

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuration
GEMINI_API_KEY_SECRET = "GEMINI_API_KEY"
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")

# Sources we expect data from — analysis runs once we have at least
# MIN_SOURCES_THRESHOLD sources for a given date.
EXPECTED_SOURCES = {"garmin", "mfp", "withings", "openweather"}
MIN_SOURCES_THRESHOLD = int(os.environ.get("MIN_SOURCES", "2"))


@functions_framework.cloud_event
def on_daily_write(cloud_event):
    """
    Firestore trigger — fires on any write to:
      eudaimonia/daily/{date}/{document}

    Checks how many sources have data for that date, and if we've
    crossed the threshold, runs the Gemini analysis.
    """
    try:
        # Extract the date from the document path
        # Path format: projects/.../documents/eudaimonia/daily/{date}/{doc}
        resource = cloud_event.data.get("value", {}).get("name", "")
        path_parts = resource.split("/documents/")[-1].split("/")
        # path_parts = ["eudaimonia", "daily", "{date}", "{doc}"]

        if len(path_parts) < 4:
            logger.warning(f"Unexpected path format: {resource}")
            return

        date_str = path_parts[2]
        doc_name = path_parts[3]
        source = doc_name.split("_")[0]  # e.g. "garmin" from "garmin_sleep"

        logger.info(f"Triggered by write: {date_str}/{doc_name} (source: {source})")

        # Skip if the write was from the Gemini analysis itself
        if source == SOURCE_GEMINI:
            logger.info("Skipping — triggered by our own analysis write")
            return

        writer = FirestoreWriter()

        # Check how many distinct sources have data for this date
        daily_ref = writer.get_daily_collection(date_str)
        docs = list(daily_ref.stream())
        sources_present = set()
        for doc in docs:
            doc_source = doc.id.split("_")[0]
            if doc_source in EXPECTED_SOURCES:
                sources_present.add(doc_source)

        logger.info(f"Sources present for {date_str}: {sources_present} ({len(sources_present)}/{MIN_SOURCES_THRESHOLD})")

        if len(sources_present) < MIN_SOURCES_THRESHOLD:
            logger.info("Below threshold — waiting for more sources")
            return

        # Check if we already ran analysis for this date
        analysis_ref = daily_ref.document(f"{SOURCE_GEMINI}_analysis")
        existing = analysis_ref.get()
        if existing.exists:
            existing_sources = set(existing.to_dict().get("_sources_analyzed", []))
            if existing_sources == sources_present:
                logger.info("Analysis already exists for same sources — skipping")
                return
            logger.info(f"New sources available ({sources_present - existing_sources}), re-running analysis")

        # Gather all data for this date
        all_data = {}
        for doc in docs:
            if not doc.id.startswith(f"{SOURCE_GEMINI}_"):
                all_data[doc.id] = doc.to_dict()
                # Remove Firestore metadata from the prompt
                all_data[doc.id].pop("_synced_at", None)
                all_data[doc.id].pop("_sync_version", None)

        # Run Gemini analysis
        analysis = _run_gemini_analysis(date_str, all_data, sources_present)

        # Write results back to Firestore
        analysis_data = {
            "date": date_str,
            "analysis": analysis,
            "_sources_analyzed": list(sources_present),
            "_source_count": len(sources_present),
            "_analyzed_at": firestore.SERVER_TIMESTAMP,
            "_model": GEMINI_MODEL,
        }
        writer.write_daily(SOURCE_GEMINI, "analysis", date_str, analysis_data)
        logger.info(f"Wrote Gemini analysis for {date_str}")

    except Exception as e:
        logger.exception(f"Gemini analysis failed: {e}")


def _run_gemini_analysis(date_str: str, data: dict, sources: set) -> dict:
    """Send collected health data to Gemini for correlation analysis."""
    api_key = get_secret(PROJECT_ID, GEMINI_API_KEY_SECRET)
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(GEMINI_MODEL)

    prompt = f"""You are a health data analyst. Analyze the following health and
environmental data collected on {date_str} from multiple sources and identify
meaningful correlations, patterns, and actionable insights.

Sources available: {', '.join(sorted(sources))}

DATA:
{json.dumps(data, indent=2, default=str)[:30000]}

Please provide your analysis in the following JSON structure:
{{
    "summary": "A 2-3 sentence overview of the day's health picture",
    "correlations": [
        {{
            "finding": "Description of the correlation",
            "sources": ["source1", "source2"],
            "confidence": "high|medium|low",
            "actionable": true/false
        }}
    ],
    "anomalies": [
        {{
            "metric": "metric name",
            "observation": "what was unusual",
            "possible_cause": "hypothesis"
        }}
    ],
    "recommendations": [
        "Actionable recommendation 1",
        "Actionable recommendation 2"
    ],
    "trends_to_watch": [
        "Thing to monitor over coming days"
    ]
}}

Respond with ONLY valid JSON, no markdown fences.
"""

    response = model.generate_content(prompt)
    text = response.text.strip()

    # Try to parse as JSON, fall back to raw text
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Clean up markdown fences if present
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                pass
        return {"raw_analysis": text}
