"""
Cloud Storage-based token persistence.
Shared across pipelines that need to store OAuth/session tokens
between Cloud Function invocations (Garmin, Withings, etc.).

Supports year-long persistence with metadata tracking:
  - created_at:      when the token was first obtained
  - last_refreshed:  when it was last successfully refreshed
  - expires_at:      hard ceiling (created_at + TOKEN_MAX_AGE_DAYS)

Tokens are stored as a single JSON blob per source in a dedicated
Cloud Storage bucket. The blob contains the serialised Garth files
plus a _metadata envelope so the caller can decide whether to
re-authenticate from scratch when the token is too old.
"""

import json
import logging
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import NamedTuple

from google.cloud import storage

from config import PROJECT_ID, TOKEN_BUCKET

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# Maximum token lifetime before forcing a full re-auth.
# Garth OAuth refresh tokens can survive much longer than a year, but we cap
# at 365 days as a safety net and to match the project requirement.
TOKEN_MAX_AGE_DAYS = 365

_gcs_client = None


class TokenBundle(NamedTuple):
    """Returned by restore_tokens when a valid token set is found."""
    token_dir: Path
    created_at: str          # ISO-8601
    last_refreshed: str      # ISO-8601
    expires_at: str          # ISO-8601
    age_days: int            # convenience — days since created_at


def _get_gcs():
    global _gcs_client
    if _gcs_client is None:
        _gcs_client = storage.Client(project=PROJECT_ID)
    return _gcs_client


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def save_tokens(source: str, token_dir: Path, created_at: str | None = None):
    """
    Upload all files from a local token directory to Cloud Storage.

    Blob name: {source}_tokens.json
    Structure:
        {
            "_metadata": {
                "created_at": "...",
                "last_refreshed": "...",
                "expires_at": "...",
                "version": 2
            },
            "files": {
                "filename1": "content1",
                ...
            }
        }

    Args:
        source:     Source identifier (e.g. "garmin").
        token_dir:  Local directory containing Garth token files.
        created_at: ISO-8601 timestamp of first auth. If None, the store
                    will read the existing blob's created_at (for refreshes)
                    or default to now (for first-time saves).
    """
    try:
        bucket = _get_gcs().bucket(TOKEN_BUCKET)
        blob_name = f"{source}_tokens.json"
        now_iso = _utcnow().isoformat()

        # Resolve created_at: caller override → existing blob → now
        if created_at is None:
            created_at = _read_existing_created_at(bucket, blob_name) or now_iso

        expires_at = (
            datetime.fromisoformat(created_at) + timedelta(days=TOKEN_MAX_AGE_DAYS)
        ).isoformat()

        # Serialise token files
        token_files = {}
        for f in token_dir.iterdir():
            if f.is_file():
                token_files[f.name] = f.read_text()

        payload = {
            "_metadata": {
                "created_at": created_at,
                "last_refreshed": now_iso,
                "expires_at": expires_at,
                "version": 2,
            },
            "files": token_files,
        }

        blob = bucket.blob(blob_name)
        blob.upload_from_string(
            json.dumps(payload),
            content_type="application/json",
        )
        logger.info(
            f"Saved {source} tokens to gs://{TOKEN_BUCKET}/{blob_name} "
            f"(created={created_at}, refreshed={now_iso}, expires={expires_at})"
        )
    except Exception as e:
        logger.error(f"Failed to save {source} tokens: {e}")


def restore_tokens(source: str) -> TokenBundle | None:
    """
    Download saved tokens from Cloud Storage into a temp directory.

    Returns a TokenBundle with the local path and metadata, or None if:
      - No tokens exist for this source
      - The tokens have exceeded TOKEN_MAX_AGE_DAYS (expired)

    The caller should check age_days / expires_at if it wants to
    proactively refresh before hard expiry.
    """
    try:
        bucket = _get_gcs().bucket(TOKEN_BUCKET)
        blob_name = f"{source}_tokens.json"
        blob = bucket.blob(blob_name)

        if not blob.exists():
            logger.info(f"No saved tokens for {source}")
            return None

        content = blob.download_as_text()
        raw = json.loads(content)

        # Handle v1 blobs (pre-metadata) — treat as expired so we re-auth
        if "_metadata" not in raw:
            logger.warning(f"Legacy v1 token blob for {source} — forcing re-auth")
            return None

        metadata = raw["_metadata"]
        token_files = raw.get("files", {})

        created_at = metadata["created_at"]
        last_refreshed = metadata["last_refreshed"]
        expires_at = metadata["expires_at"]

        # Check hard expiry
        now = _utcnow()
        expires_dt = datetime.fromisoformat(expires_at)
        if now >= expires_dt:
            age = (now - datetime.fromisoformat(created_at)).days
            logger.warning(
                f"{source} tokens expired (age={age}d, max={TOKEN_MAX_AGE_DAYS}d) — "
                f"forcing full re-auth"
            )
            return None

        # Write files to temp directory
        token_dir = Path(tempfile.mkdtemp(prefix=f"{source}_tokens_"))
        for filename, data in token_files.items():
            (token_dir / filename).write_text(data)

        age_days = (now - datetime.fromisoformat(created_at)).days

        logger.info(
            f"Restored {source} tokens from Cloud Storage "
            f"(age={age_days}d, last_refreshed={last_refreshed})"
        )

        return TokenBundle(
            token_dir=token_dir,
            created_at=created_at,
            last_refreshed=last_refreshed,
            expires_at=expires_at,
            age_days=age_days,
        )

    except Exception as e:
        logger.warning(f"Failed to restore {source} tokens: {e}")
        return None


def delete_tokens(source: str):
    """Remove a source's token blob (used when forcing a clean re-auth)."""
    try:
        bucket = _get_gcs().bucket(TOKEN_BUCKET)
        blob = bucket.blob(f"{source}_tokens.json")
        if blob.exists():
            blob.delete()
            logger.info(f"Deleted {source} token blob")
    except Exception as e:
        logger.warning(f"Failed to delete {source} tokens: {e}")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _read_existing_created_at(bucket, blob_name: str) -> str | None:
    """Read the created_at from an existing blob (for refresh saves)."""
    try:
        blob = bucket.blob(blob_name)
        if not blob.exists():
            return None
        content = blob.download_as_text()
        raw = json.loads(content)
        return raw.get("_metadata", {}).get("created_at")
    except Exception:
        return None
