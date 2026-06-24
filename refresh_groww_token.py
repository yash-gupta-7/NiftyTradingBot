#!/usr/bin/env python3
"""
Pre-market Groww auth refresh/preflight.

The trading agent uses AuthManager.refresh_if_needed(), which performs a fresh
TOTP login instead of relying on a persisted daily access-token cache. This
script exercises the same auth path before market open so cron can fail early
if credentials or the Groww auth flow are broken.
"""

import logging
import sys
from datetime import date

from auth import AuthManager
from event_calendar import EventCalendar
from logger import setup_logging


logger = logging.getLogger(__name__)


def main() -> int:
    setup_logging()

    calendar = EventCalendar()
    calendar.check_staleness()
    should_skip, reason = calendar.is_skip_day(date.today())
    if should_skip:
        logger.info("Skipping Groww auth refresh: %s", reason)
        return 0

    auth = AuthManager()
    if not auth.refresh_if_needed():
        logger.error("Groww auth refresh/preflight failed.")
        return 1

    logger.info("Groww auth refresh/preflight succeeded.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
