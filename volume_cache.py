"""
volume_cache.py — Time-Slot Volume Cache
Pre-computes the 10-day average volume for each 5-min time slot
(e.g. 09:45, 09:50, 09:55 ... 10:30).

This is used by the strategy to check whether a breakout candle
has >= 2× the average volume for that specific time of day.

Why per-slot? Volume at 9:45 AM is naturally higher than at 10:20 AM.
Comparing against a flat daily average would give noisy signals.

Usage:
    vc = VolumeCache(groww_client)
    vc.build()                         # run once at 8:50 AM
    avg = vc.get_avg_volume("09:45")   # call during monitoring
"""

import logging
from datetime import date, datetime, timedelta
from typing import Optional
from growwapi import GrowwAPI
from config import NIFTY_SYMBOL, CANDLE_5MIN

logger = logging.getLogger(__name__)


class VolumeCache:
    """
    Fetches last 10 trading days of 5-min Nifty candles and
    computes the average volume at each 5-min time slot.
    """

    LOOKBACK_DAYS  = 10       # how many days to average over
    FETCH_DAYS     = 16       # extra buffer for weekends + holidays

    def __init__(self, groww: GrowwAPI):
        self.groww = groww
        self._cache: dict[str, float] = {}   # "HH:MM" → avg volume
        self._built = False

    # ─── PUBLIC ───────────────────────────────────────────────────────────────

    def build(self) -> bool:
        """
        Fetches historical 5-min candles and builds the volume cache.
        Call once at agent startup (8:50 AM). Takes ~5–10 seconds.
        Returns True if successful.
        """
        logger.info("📊 Building volume cache (10-day per-slot averages)...")

        end_date   = date.today() - timedelta(days=1)  # exclude today
        start_date = end_date - timedelta(days=self.FETCH_DAYS)

        try:
            candle_data = self.groww.get_historical_candle_data(
                trading_symbol=NIFTY_SYMBOL,
                exchange=self.groww.EXCHANGE_NSE,
                segment=self.groww.SEGMENT_CASH,
                start_time=start_date.strftime("%Y-%m-%d 09:15:00"),
                end_time=end_date.strftime("%Y-%m-%d 15:30:00"),
                interval_in_minutes=CANDLE_5MIN
            )

            candles = candle_data.get("candles", [])
            if not candles:
                logger.warning("⚠️ No candle data for volume cache — volume filter disabled")
                return False

            # Group by time slot: "HH:MM" → list of volumes
            slot_volumes: dict[str, list] = {}
            for candle in candles:
                ts, o, h, l, c, volume = candle
                # FIX #9: Handle both Unix seconds and millisecond timestamps
                ts_int = int(ts)
                if ts_int > 1_000_000_000_000:   # ms timestamp (13 digits)
                    ts_int //= 1000
                dt     = datetime.fromtimestamp(ts_int)
                slot   = dt.strftime("%H:%M")
                volume = float(volume)
                if slot not in slot_volumes:
                    slot_volumes[slot] = []
                slot_volumes[slot].append(volume)

            # Average per slot (use last LOOKBACK_DAYS entries)
            for slot, vols in slot_volumes.items():
                self._cache[slot] = round(
                    sum(vols[-self.LOOKBACK_DAYS:]) / min(len(vols), self.LOOKBACK_DAYS), 2
                )

            self._built = True
            logger.info(
                f"✅ Volume cache built: {len(self._cache)} time slots | "
                f"Sample 09:45 avg={self._cache.get('09:45', 0):.0f}"
            )
            return True

        except Exception as e:
            logger.error(f"❌ Volume cache build failed: {e}")
            return False

    def get_avg_volume(self, time_str: str) -> float:
        """
        Returns the 10-day average volume for a given time slot.
        time_str format: "HH:MM" (e.g. "09:45")
        Returns 0 if cache not built or slot not found (disables volume filter).
        """
        if not self._built:
            return 0.0
        return self._cache.get(time_str, 0.0)

    def get_current_slot_avg(self) -> float:
        """Returns avg volume for the current 5-min time slot."""
        slot = datetime.now().strftime("%H:%M")
        # Round down to nearest 5-min mark
        minute = (datetime.now().minute // 5) * 5
        slot   = f"{datetime.now().hour:02d}:{minute:02d}"
        return self.get_avg_volume(slot)

    def summary(self) -> str:
        """Returns a readable summary of cached slots."""
        if not self._built:
            return "Volume cache not built"
        entry_slots = {k: v for k, v in self._cache.items()
                       if "09:45" <= k <= "10:30"}
        lines = ["Volume cache (entry window):"]
        for slot, avg in sorted(entry_slots.items()):
            lines.append(f"  {slot}  →  avg {avg:,.0f}")
        return "\n".join(lines)
