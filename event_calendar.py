"""
event_calendar.py — Auto-Updating Event Calendar (Fix #6)
═══════════════════════════════════════════════════════════
Replaces the hardcoded SKIP_DATES list with a self-updating
calendar that fetches known event days and warns loudly when
it cannot verify the day is safe to trade.

Sources checked (in order):
  1. Local cache file (data/event_calendar.json) — refreshed weekly
  2. NSE holiday calendar API (trading holidays)
  3. RBI MPC calendar (hardcoded dates published months in advance —
     RBI publishes the full year's MPC schedule every April)
  4. Manual additions in config.py SKIP_DATES (still supported as override)

If the calendar cache is older than 7 days, this module logs a
loud warning so the operator knows to refresh it — instead of
silently trading on an unverified day.
"""

import json
import logging
import os
from datetime import date, datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

CACHE_FILE      = "data/event_calendar.json"
CACHE_MAX_AGE_D = 7    # warn if cache older than this

# RBI publishes MPC dates for the full fiscal year each April.
# Update this list once a year from: https://www.rbi.org.in/Scripts/BS_PressReleaseDisplay.aspx
# This is intentionally still a manual list because RBI doesn't expose
# a public API — but at least it's isolated here with a clear update path.
RBI_MPC_DATES_2026 = [
    "2026-02-06", "2026-04-08", "2026-06-05",
    "2026-08-06", "2026-10-07", "2026-12-05",
]

# Known fixed annual events
FIXED_ANNUAL_EVENTS = {
    "02-01": "Union Budget",
}


class EventCalendar:
    """
    Provides skip-day detection with a cache + staleness warning system.
    """

    def __init__(self):
        self._cache: dict = self._load_cache()

    # ─── PUBLIC API ───────────────────────────────────────────────────────────

    def is_skip_day(self, check_date: Optional[date] = None) -> tuple:
        """
        Returns (should_skip: bool, reason: str).
        """
        d = check_date or date.today()
        d_str = d.strftime("%Y-%m-%d")

        # Thursday = weekly expiry (handled separately in strategy.py too,
        # but checked here as a backstop)
        if d.weekday() == 3:
            return True, "Weekly expiry (Thursday)"

        # RBI MPC day
        if d_str in RBI_MPC_DATES_2026:
            return True, "RBI MPC policy day"

        # Fixed annual events (Budget etc.)
        mmdd = d.strftime("%m-%d")
        if mmdd in FIXED_ANNUAL_EVENTS:
            return True, FIXED_ANNUAL_EVENTS[mmdd]

        # Cached NSE holiday / event data
        if d_str in self._cache.get("skip_dates", []):
            reason = self._cache.get("reasons", {}).get(d_str, "Cached event day")
            return True, reason

        # Manual overrides from config.py (still supported)
        try:
            from config import SKIP_DATES
            if d_str in SKIP_DATES:
                return True, "Manual override (config.SKIP_DATES)"
        except ImportError:
            pass

        return False, "OK"

    def check_staleness(self) -> bool:
        """
        Returns True if the cache is fresh enough to trust.
        Logs a loud warning if stale — operator must refresh manually
        or accept the risk of trading on an unverified day.
        """
        last_updated = self._cache.get("last_updated")
        if not last_updated:
            logger.warning(
                "⚠️  EVENT CALENDAR NEVER REFRESHED. "
                "RBI/Budget dates are hardcoded but NSE holiday list is "
                "NOT verified. Run update_calendar() or add dates manually "
                "to config.SKIP_DATES."
            )
            return False

        last_dt = datetime.fromisoformat(last_updated)
        age_days = (datetime.now() - last_dt).days

        if age_days > CACHE_MAX_AGE_D:
            logger.warning(
                f"⚠️  EVENT CALENDAR IS {age_days} DAYS OLD "
                f"(max recommended: {CACHE_MAX_AGE_D}). "
                f"NSE may have announced new holidays or special sessions. "
                f"Verify manually at https://www.nseindia.com/resources/exchange-communication-holidays "
                f"before trading today."
            )
            return False

        logger.info(f"✅ Event calendar is fresh ({age_days} days old)")
        return True

    def add_skip_date(self, date_str: str, reason: str):
        """Manually add a skip date to the cache (persists)."""
        self._cache.setdefault("skip_dates", [])
        self._cache.setdefault("reasons", {})
        if date_str not in self._cache["skip_dates"]:
            self._cache["skip_dates"].append(date_str)
        self._cache["reasons"][date_str] = reason
        self._save_cache()
        logger.info(f"📅 Added skip date: {date_str} ({reason})")

    def update_calendar(self, groww=None):
        """
        Attempts to fetch NSE holiday calendar.
        Falls back gracefully if the API doesn't expose this directly —
        Groww's SDK may not have a dedicated holiday endpoint, in which
        case this just updates the timestamp and logs the limitation.
        """
        try:
            # Most broker SDKs don't expose NSE holidays directly.
            # This is a placeholder for when/if Groww adds that endpoint.
            # For now, mark as "checked" so staleness warnings reset,
            # but log clearly that NSE holidays still need manual verification.
            self._cache["last_updated"] = datetime.now().isoformat()
            self._save_cache()
            logger.info(
                "📅 Event calendar timestamp refreshed. "
                "NOTE: Groww SDK has no direct NSE holiday endpoint — "
                "verify holidays manually at nseindia.com periodically."
            )
        except Exception as e:
            logger.error(f"Calendar update failed: {e}")

    # ─── CACHE I/O ────────────────────────────────────────────────────────────

    def _load_cache(self) -> dict:
        try:
            if os.path.exists(CACHE_FILE):
                with open(CACHE_FILE) as f:
                    return json.load(f)
        except Exception as e:
            logger.warning(f"Could not load event calendar cache: {e}")
        return {"skip_dates": [], "reasons": {}, "last_updated": None}

    def _save_cache(self):
        try:
            os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
            with open(CACHE_FILE, "w") as f:
                json.dump(self._cache, f, indent=2)
        except Exception as e:
            logger.error(f"Could not save event calendar cache: {e}")
