"""
utils.py — Shared utilities
FIX #24: Exponential backoff retry decorator for all Groww API calls.
FIX #25: Slippage simulation for paper trading.
"""

import time
import logging
import functools
import random

logger = logging.getLogger(__name__)


def retry(max_attempts: int = 3, base_delay: float = 1.0, exceptions=(Exception,)):
    """
    FIX #24: Decorator that retries a function with exponential backoff.
    Use on any Groww API call that might fail due to transient network errors.

    Usage:
        @retry(max_attempts=3, base_delay=1.0)
        def my_api_call():
            return groww.place_order(...)
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_exc = e
                    if attempt == max_attempts:
                        logger.error(
                            f"❌ {func.__name__} failed after {max_attempts} attempts: {e}"
                        )
                        raise
                    delay = base_delay * (2 ** (attempt - 1))  # 1s, 2s, 4s
                    logger.warning(
                        f"⚠️ {func.__name__} attempt {attempt} failed: {e}. "
                        f"Retrying in {delay:.1f}s..."
                    )
                    time.sleep(delay)
            raise last_exc
        return wrapper
    return decorator


def simulate_slippage(price: float, side: str, pct: float = 0.015) -> float:
    """
    FIX #25: Simulates realistic bid-ask slippage for paper trading.
    Adds slippage in the unfavourable direction:
        BUY  → price is slightly higher than LTP
        SELL → price is slightly lower than LTP
    Default slippage: ±1.5% (conservative for options)
    """
    # Random slippage between 0.5% and pct (not always max)
    actual_pct = random.uniform(0.005, pct)
    if side.upper() == "BUY":
        return round(price * (1 + actual_pct), 2)
    else:
        return round(price * (1 - actual_pct), 2)


class Heartbeat:
    """
    FIX #23: Writes a heartbeat file every cycle.
    An external cron checks if file is stale and restarts the agent.
    """
    FILE = "data/heartbeat"

    @classmethod
    def pulse(cls):
        """Call once per monitoring loop iteration."""
        try:
            import os
            os.makedirs("data", exist_ok=True)
            with open(cls.FILE, "w") as f:
                f.write(str(time.time()))
        except Exception as e:
            logger.warning(f"Heartbeat write failed: {e}")

    @classmethod
    def is_alive(cls, max_age_seconds: int = 360) -> bool:
        """Returns True if heartbeat is fresh (called by external watchdog)."""
        try:
            with open(cls.FILE) as f:
                ts = float(f.read())
            return (time.time() - ts) < max_age_seconds
        except Exception:
            return False
