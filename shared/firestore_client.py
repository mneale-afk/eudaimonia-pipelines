"""
Unified Firestore writer for all Eudaimonia pipelines.

All sources write through this client to maintain a consistent schema.
The key design: everything date-based goes into eudaimonia/daily/{date}/*
with source-prefixed document names (e.g., "garmin_sleep", "mfp_nutrition").
This makes cross-source date queries trivial for the Gemini analysis job.
"""

import logging
from datetime import datetime, timezone
from typing import Any

from google.cloud import firestore

from config import FIRESTORE_ROOT

logger = logging.getLogger(__name__)


class FirestoreWriter:
    """Writes health data to Firestore with a unified schema."""

    def __init__(self):
        self.db = firestore.Client()
        self.root = FIRESTORE_ROOT

    # ------------------------------------------------------------------
    # Daily data  →  eudaimonia/daily/{date}/{source}_{metric}
    # ------------------------------------------------------------------

    def write_daily(self, source: str, metric: str, date_str: str, data: Any):
        """
        Write date-specific data.
        Path: {root}/daily/{date}/{source}_{metric}

        Args:
            source:   e.g. "garmin", "mfp", "withings", "openweather"
            metric:   e.g. "sleep", "nutrition", "weight"
            date_str: ISO date string, e.g. "2026-03-31"
            data:     The payload (dict or list)
        """
        doc_name = f"{source}_{metric}"
        doc_ref = (
            self.db.collection(self.root)
            .document("daily")
            .collection(date_str)
            .document(doc_name)
        )

        payload = self._prepare_payload(data, source, metric)
        doc_ref.set(payload, merge=True)
        logger.info(f"Wrote {self.root}/daily/{date_str}/{doc_name}")

    # ------------------------------------------------------------------
    # Named documents  →  eudaimonia/{collection}/items/{doc_id}
    # ------------------------------------------------------------------

    def write_document(self, collection: str, doc_id: str, data: Any):
        """
        Write to a named collection (activities, profile, etc.).
        Path: {root}/{collection}/items/{doc_id}
        """
        doc_ref = (
            self.db.collection(self.root)
            .document(collection)
            .collection("items")
            .document(doc_id)
        )

        payload = self._prepare_payload(data)
        doc_ref.set(payload, merge=True)
        logger.info(f"Wrote {self.root}/{collection}/items/{doc_id}")

    def document_exists(self, collection: str, doc_id: str) -> bool:
        """Check if a document already exists (used to skip re-fetching)."""
        doc_ref = (
            self.db.collection(self.root)
            .document(collection)
            .collection("items")
            .document(doc_id)
        )
        return doc_ref.get().exists

    # ------------------------------------------------------------------
    # Sync log  →  eudaimonia/sync_log/items/{timestamp}_{source}
    # ------------------------------------------------------------------

    def log_sync(self, source: str, status: str, details: dict = None):
        """
        Write a sync audit log entry.
        Useful for monitoring and debugging across all pipelines.
        """
        now = datetime.now(timezone.utc)
        doc_id = f"{now.strftime('%Y%m%dT%H%M%S')}_{source}"

        entry = {
            "source": source,
            "status": status,
            "timestamp": now.isoformat(),
            "details": details or {},
            "_synced_at": firestore.SERVER_TIMESTAMP,
        }

        doc_ref = (
            self.db.collection(self.root)
            .document("sync_log")
            .collection("items")
            .document(doc_id)
        )
        doc_ref.set(entry)
        logger.info(f"Logged sync: {source} → {status}")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def server_timestamp(self):
        """Return a Firestore server timestamp sentinel."""
        return firestore.SERVER_TIMESTAMP

    def get_daily_collection(self, date_str: str):
        """
        Return a reference to the daily collection for a given date.
        Used by the Gemini analysis job to read all sources for a date.
        """
        return (
            self.db.collection(self.root)
            .document("daily")
            .collection(date_str)
        )

    def _prepare_payload(self, data: Any, source: str = None, metric: str = None) -> dict:
        """Ensure data is a dict, add metadata, sanitize keys."""
        if isinstance(data, list):
            payload = {"data": data}
        elif isinstance(data, dict):
            payload = data.copy()
        else:
            payload = {"value": data}

        payload["_synced_at"] = firestore.SERVER_TIMESTAMP
        payload["_sync_version"] = "1.0"
        if source:
            payload["_source"] = source
        if metric:
            payload["_metric"] = metric

        return self._sanitize_keys(payload)

    def _sanitize_keys(self, obj: Any) -> Any:
        """Firestore doesn't allow '.' or '/' in field names."""
        if isinstance(obj, dict):
            return {
                k.replace(".", "_").replace("/", "_"): self._sanitize_keys(v)
                for k, v in obj.items()
            }
        elif isinstance(obj, list):
            return [self._sanitize_keys(item) for item in obj]
        return obj
