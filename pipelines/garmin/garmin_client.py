"""
Garmin Connect client wrapper.

Uses cyberjunky/python-garminconnect (Garth OAuth under the hood).
Authentication strategy with year-long token persistence:

    1. Restore saved OAuth tokens from Cloud Storage.
       a. If tokens exist and are < TOKEN_MAX_AGE_DAYS old → login via Garth
          session resume (no credentials needed).
       b. If the restored session fails (e.g. Garmin revoked it) → fall
          through to step 2.
    2. Full email/password auth via GCP Secret Manager → new OAuth session.
       Resets created_at to now (starts a fresh 365-day window).

Every successful call to save_tokens() updates last_refreshed while
preserving the original created_at. When the token hits 365 days,
token_store.restore_tokens() returns None and forces a clean re-auth.

Designed for hourly Cloud Scheduler invocations — the Garth refresh
token gets exercised frequently, keeping the session alive.
"""

import base64
import logging
import tempfile
from pathlib import Path

from garminconnect import Garmin

from config import PROJECT_ID, SOURCE_GARMIN
from gcp_secrets import get_secret
import token_store

logger = logging.getLogger(__name__)

# Secret Manager secret names (match GCP console naming)
EMAIL_SECRET = "GARMIN_EMAIL"
PASSWORD_SECRET = "GARMIN_PASSWORD"
OAUTH_B64_SECRET = "GARMIN_OAUTH_B64"

# Warn when tokens are within this many days of expiry
TOKEN_EXPIRY_WARNING_DAYS = 30


class GarminClient:
    """Wraps garminconnect.Garmin with GCP-native OAuth and year-long persistence."""

    def __init__(self):
        self._client: Garmin | None = None
        self._token_dir = Path(tempfile.mkdtemp(prefix="garmin_tokens_"))
        self._created_at: str | None = None   # tracks the OAuth session origin
        self._is_fresh_auth: bool = False      # True if we did email/password login

    def authenticate(self):
        """
        Auth strategy (three tiers):
        1. Cloud Storage tokens — hourly refreshes keep these warm (year window).
        2. GARMIN_OAUTH_B64 secret — base64-encoded Garth token dir, useful for
           bootstrapping or disaster recovery without email/password.
        3. Email/password from Secret Manager — full re-auth, resets 365-day window.
        """
        # --- Tier 1: Cloud Storage (hot path for hourly syncs) ---
        bundle = token_store.restore_tokens(SOURCE_GARMIN)

        if bundle is not None:
            try:
                self._client = Garmin()
                self._client.login(str(bundle.token_dir))
                self._token_dir = bundle.token_dir
                self._created_at = bundle.created_at

                remaining = token_store.TOKEN_MAX_AGE_DAYS - bundle.age_days
                if remaining <= TOKEN_EXPIRY_WARNING_DAYS:
                    logger.warning(
                        f"OAuth session expires in {remaining} days — "
                        f"consider rotating credentials"
                    )

                logger.info(
                    f"Authenticated via Cloud Storage tokens "
                    f"(age={bundle.age_days}d, last_refreshed={bundle.last_refreshed})"
                )
                return
            except Exception as e:
                logger.warning(f"Cloud Storage token restore failed: {e}")
                token_store.delete_tokens(SOURCE_GARMIN)

        # --- Tier 2: GARMIN_OAUTH_B64 secret (bootstrap / recovery) ---
        if self._try_oauth_b64_secret():
            return

        # --- Tier 3: Full email/password re-auth ---
        self._full_auth()

    def _try_oauth_b64_secret(self) -> bool:
        """
        Attempt auth from the GARMIN_OAUTH_B64 secret.

        This secret holds a base64-encoded JSON mapping of Garth token
        filenames to their contents — the same format token_store uses
        internally. Useful for initial bootstrap: run Garth locally,
        encode the token dir, and store it in Secret Manager.

        Encoding command:
            python -c "
            import base64, json, pathlib, sys
            d = pathlib.Path(sys.argv[1])
            blob = {f.name: f.read_text() for f in d.iterdir() if f.is_file()}
            print(base64.b64encode(json.dumps(blob).encode()).decode())
            " ~/.garth
        """
        try:
            b64_data = get_secret(PROJECT_ID, OAUTH_B64_SECRET)
        except Exception:
            logger.info("No GARMIN_OAUTH_B64 secret found — skipping tier 2")
            return False

        try:
            decoded = base64.b64decode(b64_data)
            import json
            token_files = json.loads(decoded)

            token_dir = Path(tempfile.mkdtemp(prefix="garmin_oauth_b64_"))
            for filename, content in token_files.items():
                (token_dir / filename).write_text(content)

            self._client = Garmin()
            self._client.login(str(token_dir))
            self._token_dir = token_dir
            self._created_at = None  # will be set to now on first save

            logger.info("Authenticated via GARMIN_OAUTH_B64 secret")
            self.save_tokens()  # Promote to Cloud Storage for future hourly syncs
            return True
        except Exception as e:
            logger.warning(f"GARMIN_OAUTH_B64 auth failed: {e}")
            return False

    def _full_auth(self):
        """Authenticate with email/password — starts a new 365-day token window."""
        email = get_secret(PROJECT_ID, EMAIL_SECRET)
        password = get_secret(PROJECT_ID, PASSWORD_SECRET)

        self._client = Garmin(email, password)
        self._client.login()

        self._is_fresh_auth = True
        self._created_at = None  # token_store will set to now

        logger.info("Authenticated via email/password — new OAuth session created")
        self.save_tokens()

    def save_tokens(self):
        """
        Persist current Garth OAuth session to Cloud Storage.

        On a fresh auth, created_at resets to now (new 365-day window).
        On a token refresh (hourly sync), created_at is preserved from the
        existing blob while last_refreshed updates.
        """
        try:
            self._client.garth.dump(str(self._token_dir))
            token_store.save_tokens(
                SOURCE_GARMIN,
                self._token_dir,
                created_at=self._created_at,  # None on fresh auth → defaults to now
            )
        except Exception as e:
            logger.error(f"Failed to save OAuth tokens: {e}")

    def safe_call(self, method_name: str, *args, **kwargs):
        """
        Call a Garmin client method by name, returning None on failure.
        Resilient to individual metric failures so the sync continues.
        """
        method = getattr(self._client, method_name, None)
        if method is None:
            logger.warning(f"Method '{method_name}' not found on Garmin client")
            return None

        try:
            result = method(*args, **kwargs)
            logger.info(f"{method_name}({args}) → OK")
            return result
        except Exception as e:
            logger.warning(f"{method_name}({args}) → FAILED: {e}")
            return None
