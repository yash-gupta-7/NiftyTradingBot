"""
auth.py — Groww Authentication
Uses TOTP flow (no daily expiry unlike API Key flow).
Token is cached and reused until invalid.
"""

import json
import os
import time
import logging
import pyotp
from growwapi import GrowwAPI
from config import GROWW_TOTP_TOKEN, GROWW_TOTP_SECRET
AUTH_STATE_FILE = "data/auth_state.json"  # FIX #5: separate from risk state

logger = logging.getLogger(__name__)


class AuthManager:
    """
    Manages Groww API authentication using TOTP flow.
    Caches the access token so we don't re-auth on every run.
    Automatically re-authenticates if token is invalid.
    """

    def __init__(self):
        self.groww_client = None
        self.access_token = None

    # ─── PUBLIC ───────────────────────────────────────────────────────────────

    def get_client(self) -> GrowwAPI:
        """
        Returns an authenticated GrowwAPI client.
        Re-authenticates if needed.
        """
        if self.groww_client is None:
            self._authenticate()
        return self.groww_client

    def refresh_if_needed(self) -> bool:
        """FIX #4: Always authenticates fresh via TOTP. No cached token."""
        try:
            self._authenticate()
            return True
        except Exception as e:
            logger.error(f"❌ Auth failed: {e}")
            return False

    # ─── PRIVATE ──────────────────────────────────────────────────────────────

    def _authenticate(self):
        """
        Performs TOTP-based authentication with Groww API.
        """
        if not GROWW_TOTP_TOKEN or not GROWW_TOTP_SECRET:
            raise ValueError(
                "GROWW_TOTP_TOKEN and GROWW_TOTP_SECRET must be set in .env file. "
                "Get them from: https://groww.in/trade-api/api-keys"
            )

        try:
            # Generate current TOTP
            totp_gen = pyotp.TOTP(GROWW_TOTP_SECRET)
            current_totp = totp_gen.now()

            logger.info(f"Authenticating with TOTP (generated: {current_totp[:2]}****)")

            self.access_token = GrowwAPI.get_access_token(
                api_key=GROWW_TOTP_TOKEN,
                totp=current_totp
            )

            self.groww_client = GrowwAPI(self.access_token)
            # FIX #4: Token NOT cached to disk — TOTP re-auth is <2s
            # and avoids plaintext token storage security risk.
            logger.info("✅ Authentication successful.")

        except Exception as e:
            raise RuntimeError(f"Authentication failed: {e}")

    def _validate_token(self, token: str) -> bool:
        """
        Validates token by making a lightweight API call.
        """
        try:
            test_client = GrowwAPI(token)
            # Lightweight call — just checks if token works
            test_client.get_positions_for_user(segment="FNO")
            return True
        except Exception:
            return False

    def _save_cached_token(self, token: str):
        """Saves token to state file."""
        try:
            os.makedirs(os.path.dirname(AUTH_STATE_FILE), exist_ok=True)
            state = self._load_state()
            state["access_token"] = token
            state["token_saved_at"] = time.time()
            with open(AUTH_STATE_FILE, "w") as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            logger.warning(f"Could not save token to cache: {e}")

    def _load_cached_token(self) -> str | None:
        """Loads cached token from state file."""
        try:
            state = self._load_state()
            return state.get("access_token")
        except Exception:
            return None

    def _load_state(self) -> dict:
        """Loads the full agent state file."""
        try:
            if os.path.exists(AUTH_STATE_FILE):
                with open(AUTH_STATE_FILE, "r") as f:
                    return json.load(f)
        except Exception:
            pass
        return {}
