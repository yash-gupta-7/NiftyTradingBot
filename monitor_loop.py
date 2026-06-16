"""
monitor_loop.py — Dual-Frequency Monitoring Architecture
─────────────────────────────────────────────────────────
Replaces the single-loop in main.py that slept for 5 minutes between checks.

TWO LOOPS RUNNING CONCURRENTLY:

  FAST LOOP  (daemon thread, every 1.5 seconds)
  ├── Fetch live LTP for both option legs
  ├── Compute current spread / option value
  ├── Check: value <= stop_loss           → EXIT immediately
  ├── Check: value >= level1_target       → partial exit
  ├── Check: value >= hard_target         → EXIT immediately
  ├── Check: time >= squareoff            → EXIT immediately
  └── Check: V3 trailing SL hit          → EXIT immediately

  SLOW LOOP  (main thread, every 5-minute candle close)
  ├── Fetch new 5-min OHLCV candle
  ├── Update VWAP with candle data
  ├── Structural SL (candle CLOSE, not tick — fix #3 preserved)
  ├── Update trailing stop (2-bar swing method)
  └── Breakout detection (if no trade yet)

THREAD SAFETY:
  All shared state lives in LoopState, mutated only under _state_lock.
  exit_triggered flag is checked at the start of every fast-loop cycle.
  Once True, both loops drain gracefully — no double-exit possible.
"""

import threading
import time
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Callable

import pytz

from config import (
    SQUAREOFF_TIME, ALERT_TIME, CANDLE_5MIN,
    SL_PREMIUM_PCT, LEVEL1_ATR_MULTIPLE, LEVEL1_EXIT_PCT,
    LEVEL3_ATR_MULTIPLE, NIFTY_LOT_SIZE, LOTS_PER_TRADE,
)

logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")

FAST_POLL_SECS  = 1.5   # poll live prices every 1.5 seconds
SLOW_POLL_SECS  = 30    # slow loop checks every 30s; acts only on new candle


# ─── SHARED STATE ─────────────────────────────────────────────────────────────

@dataclass
class LoopState:
    """
    All mutable state shared between fast and slow loops.
    Never mutate fields outside of code holding _lock.
    """
    # Trade existence
    trade_open:       bool  = False
    exit_triggered:   bool  = False   # once True → both loops stop acting
    exit_reason:      str   = ""

    # Position identifiers (set at entry)
    buy_symbol:       str   = ""
    sell_symbol:      str   = ""
    is_spread:        bool  = True    # V2 spread=True; V3 naked option=False

    # Premium levels (all in ₹ per unit)
    entry_premium:    float = 0.0
    current_sl:       float = 0.0    # updated by slow loop (trailing / breakeven)
    level1_target:    float = 0.0    # set at entry
    hard_target:      float = 0.0    # set at entry (L3 for V2, or trail-based for V3)
    peak_premium:     float = 0.0    # updated by fast loop

    # State flags
    level1_done:      bool  = False   # partial exit already executed
    at_breakeven:     bool  = False   # SL moved to breakeven

    # Last known value (for logging throttle)
    last_logged_pct:  float = 0.0


# ─── DUAL LOOP MONITOR ────────────────────────────────────────────────────────

class DualLoopMonitor:
    """
    Runs the fast and slow monitoring loops concurrently.
    Attach to main.py via agent.monitor = DualLoopMonitor(...).

    Usage:
        mon = DualLoopMonitor(groww, market_data, orders, on_exit_callback)
        mon.register_trade(buy_symbol, sell_symbol, entry_premium, atr20)
        mon.start()   # blocks main thread in slow loop; fast runs in background
    """

    def __init__(self, groww, market_data, orders, on_exit: Callable):
        self.groww      = groww
        self.md         = market_data
        self.orders     = orders
        self.on_exit    = on_exit          # callback(exit_reason) when trade closes

        self._state     = LoopState()
        self._lock      = threading.Lock()
        self._fast_thread: Optional[threading.Thread] = None
        self._running   = False

        # Slow-loop candle tracking
        self._last_candle_time: Optional[str] = None

    # ─── SETUP ────────────────────────────────────────────────────────────────

    def register_trade(
        self,
        buy_symbol:    str,
        sell_symbol:   str,
        entry_premium: float,
        atr20:         float,
        is_spread:     bool = True,
    ):
        """Call immediately after trade entry to register for monitoring."""
        units = NIFTY_LOT_SIZE * LOTS_PER_TRADE

        with self._lock:
            self._state.trade_open     = True
            self._state.exit_triggered = False
            self._state.buy_symbol     = buy_symbol
            self._state.sell_symbol    = sell_symbol
            self._state.is_spread      = is_spread
            self._state.entry_premium  = entry_premium
            self._state.peak_premium   = entry_premium

            # Stop loss
            self._state.current_sl = round(entry_premium * (1 - SL_PREMIUM_PCT), 2)

            # Level 1 target: entry + (1× ATR × delta) converted to option premium
            # For spread: net spread gains ~SPREAD_DELTA × Nifty move
            # Use a simpler approximation: 30% of entry premium as Level 1
            self._state.level1_target = round(entry_premium * 1.30, 2)

            # Hard target: 1.8× ATR move in Nifty → spread gains ~60%
            self._state.hard_target   = round(entry_premium * 1.60, 2)

            self._state.level1_done   = False
            self._state.at_breakeven  = False

        logger.info(
            f"🎯 Monitor registered | entry=₹{entry_premium:.2f} | "
            f"SL=₹{self._state.current_sl:.2f} | "
            f"L1=₹{self._state.level1_target:.2f} | "
            f"Hard=₹{self._state.hard_target:.2f}"
        )

    def move_sl_to_breakeven(self):
        """Called by slow loop at Level 1 partial exit — SL moves to entry."""
        with self._lock:
            if self._state.current_sl < self._state.entry_premium:
                old = self._state.current_sl
                self._state.current_sl  = self._state.entry_premium
                self._state.at_breakeven = True
                logger.info(
                    f"🛡️ SL raised to BREAKEVEN: ₹{old:.2f} → ₹{self._state.current_sl:.2f}"
                )

    def update_trailing_sl(self, new_sl: float):
        """Slow loop calls this to raise the trailing SL (never lowers it)."""
        with self._lock:
            if new_sl > self._state.current_sl:
                old = self._state.current_sl
                self._state.current_sl = round(new_sl, 2)
                logger.info(
                    f"📐 Trail SL raised: ₹{old:.2f} → ₹{self._state.current_sl:.2f}"
                )

    # ─── START / STOP ─────────────────────────────────────────────────────────

    def start(self, slow_loop_fn: Callable):
        """
        Starts the fast loop as a daemon thread, then runs slow_loop_fn
        in the main thread.  slow_loop_fn must call self.is_running()
        to know when to exit.
        """
        self._running = True

        self._fast_thread = threading.Thread(
            target=self._fast_loop,
            name="FastMonitor",
            daemon=True          # dies automatically if main thread exits
        )
        self._fast_thread.start()
        logger.info(f"⚡ Fast monitor started (poll={FAST_POLL_SECS}s)")

        # Slow loop runs in main thread (blocking)
        slow_loop_fn()

    def stop(self):
        """Signal both loops to stop."""
        self._running = False
        logger.info("🔴 Monitor stopping")

    def is_running(self) -> bool:
        return self._running and not self._state.exit_triggered

    # ─── FAST LOOP ────────────────────────────────────────────────────────────

    def _fast_loop(self):
        """
        Runs every FAST_POLL_SECS in background thread.
        Fetches live LTP and checks SL + hard targets.
        """
        while self._running:
            try:
                self._fast_cycle()
            except Exception as e:
                logger.warning(f"Fast loop error (will retry): {e}")
            time.sleep(FAST_POLL_SECS)

    def _fast_cycle(self):
        """One iteration of the fast loop."""
        # Check exit condition first — atomic read
        with self._lock:
            if self._state.exit_triggered or not self._state.trade_open:
                return
            sl           = self._state.current_sl
            l1_target    = self._state.level1_target
            hard_target  = self._state.hard_target
            l1_done      = self._state.level1_done
            entry        = self._state.entry_premium
            peak         = self._state.peak_premium

        # ── Time check — squareoff ─────────────────────────────────────────────
        now = datetime.now(IST)
        if (now.hour, now.minute) >= tuple(map(int, SQUAREOFF_TIME.split(":"))):
            self._trigger_exit("SQUAREOFF_TIME")
            return

        # ── Fetch live spread / option value ──────────────────────────────────
        current_value = self._fetch_current_value()
        if current_value is None:
            return  # API error — skip this cycle, retry in 1.5s

        # Update peak
        with self._lock:
            if current_value > self._state.peak_premium:
                self._state.peak_premium = current_value
            peak = self._state.peak_premium

        gain_pct = (current_value - entry) / entry * 100

        # Throttled logging — only log every 1% change
        with self._lock:
            if abs(gain_pct - self._state.last_logged_pct) >= 1.0:
                logger.info(
                    f"⚡ Fast | value=₹{current_value:.1f} | "
                    f"gain={gain_pct:+.1f}% | "
                    f"SL=₹{sl:.1f} | peak=₹{peak:.1f}"
                )
                self._state.last_logged_pct = gain_pct

        # ── STOP LOSS ─────────────────────────────────────────────────────────
        if current_value <= sl:
            logger.warning(
                f"🛑 FAST SL HIT: ₹{current_value:.1f} <= SL ₹{sl:.1f} "
                f"(gain={gain_pct:+.1f}%)"
            )
            self._trigger_exit("STOP_LOSS")
            return

        # ── LEVEL 1 TARGET (first time only) ──────────────────────────────────
        if not l1_done and current_value >= l1_target:
            logger.info(
                f"💰 FAST L1 TARGET: ₹{current_value:.1f} >= ₹{l1_target:.1f}"
            )
            with self._lock:
                self._state.level1_done = True
            # Delegate actual partial exit to orders (thread-safe via lock in orders)
            try:
                self.orders._execute_level1_exit()
                self.move_sl_to_breakeven()
            except Exception as e:
                logger.error(f"L1 exit failed: {e}")
            return

        # ── HARD TARGET ───────────────────────────────────────────────────────
        if current_value >= hard_target:
            logger.info(
                f"💰 FAST HARD TARGET: ₹{current_value:.1f} >= ₹{hard_target:.1f}"
            )
            self._trigger_exit("TARGET_HARD")
            return

    # ─── SLOW LOOP (called from main thread) ──────────────────────────────────

    def run_slow_cycle(self, candles_df, nifty_price: float, bnf_price: float,
                       avg_volume: float) -> Optional[str]:
        """
        Call once per 5-min candle close.
        Returns exit reason string if trade closed, else None.
        """
        with self._lock:
            if self._state.exit_triggered or not self._state.trade_open:
                return self._state.exit_reason or None

        # ── Structural SL (candle close price) ────────────────────────────────
        if self._structural_sl_hit(candles_df, nifty_price):
            logger.info("🛑 SLOW: structural SL — candle closed back inside range")
            return self._trigger_exit("STRUCTURAL_SL")

        # ── Trailing SL update ────────────────────────────────────────────────
        if self._state.at_breakeven and candles_df is not None:
            self._update_trailing_sl_from_candles(candles_df, nifty_price)

        return None

    def _structural_sl_hit(self, candles_df, nifty_price: float) -> bool:
        """Uses candle CLOSE (not tick) — preserves fix #3."""
        oh = getattr(self.md, "opening_high", None)
        ol = getattr(self.md, "opening_low",  None)
        direction = getattr(self.orders, "signal_direction", None)
        if not oh or not ol or not direction:
            return False
        check = nifty_price
        if candles_df is not None and not candles_df.empty:
            check = float(candles_df.iloc[-1]["close"])
        if direction == "BUY_CALL":
            return check < oh
        return check > ol

    def _update_trailing_sl_from_candles(self, candles_df, nifty_price: float):
        """2-bar swing trailing SL — only moves SL up, never down."""
        if len(candles_df) < 2:
            return
        direction = getattr(self.orders, "signal_direction", "BUY_CALL")
        last2 = candles_df.tail(2)
        if direction == "BUY_CALL":
            swing = float(last2["low"].min())
        else:
            swing = float(last2["high"].max())
        # Convert Nifty level to approximate option premium impact
        # and pass to update_trailing_sl only if it raises the floor
        # For now, use the raw Nifty level as a proxy — proper conversion
        # requires delta which changes intraday. Slow loop uses structure.
        current_sl = self._state.current_sl
        # Only update if swing represents an improvement
        if direction == "BUY_CALL" and swing > nifty_price * 0.995:
            pass  # swing too close — skip
        self.orders._update_trailing_stop(candles_df, nifty_price)
        # Sync the orders trailing_sl_price back into shared state
        trail_sl_price = getattr(self.orders, "trailing_sl_price", None)
        if trail_sl_price:
            # Approximate: if Nifty SL level is hit → option SL ~ entry_premium * 0.90
            # This is conservative — actual delta calc would be more precise
            pass  # trailing is managed by orders._update_trailing_stop

    # ─── TRIGGER EXIT ─────────────────────────────────────────────────────────

    def _trigger_exit(self, reason: str) -> str:
        """
        Thread-safe single exit trigger.
        Returns the reason. Second call is a no-op (exit already triggered).
        """
        with self._lock:
            if self._state.exit_triggered:
                return self._state.exit_reason   # already done
            self._state.exit_triggered = True
            self._state.exit_reason    = reason
            self._state.trade_open     = False

        logger.info(f"🔔 Exit triggered by fast/slow loop: {reason}")

        # Execute the actual order exit (orders module is thread-safe via its own lock)
        try:
            self.orders._exit_all(reason)
        except Exception as e:
            logger.error(f"Exit order failed: {e} — check Groww app immediately")

        # Fire callback in a separate thread so we don't block the fast loop
        threading.Thread(
            target=self.on_exit,
            args=(reason,),
            daemon=True
        ).start()

        self._running = False
        return reason

    # ─── HELPERS ──────────────────────────────────────────────────────────────

    def _fetch_current_value(self) -> Optional[float]:
        """
        Fetches the current spread value (V2) or option LTP (V3).
        Two lightweight LTP calls — fast and low-latency.
        """
        try:
            with self._lock:
                buy_sym  = self._state.buy_symbol
                sell_sym = self._state.sell_symbol
                is_spread = self._state.is_spread

            buy_ltp  = self._get_ltp(buy_sym)
            if buy_ltp is None:
                return None

            if is_spread:
                sell_ltp = self._get_ltp(sell_sym)
                if sell_ltp is None:
                    return None
                return max(round(buy_ltp - sell_ltp, 2), 0.0)
            else:
                return buy_ltp   # V3 naked option

        except Exception as e:
            logger.debug(f"LTP fetch error: {e}")
            return None

    def _get_ltp(self, symbol: str) -> Optional[float]:
        try:
            resp = self.groww.get_ltp(
                trading_symbol=symbol,
                exchange=self.groww.EXCHANGE_NSE,
                segment=self.groww.SEGMENT_FNO
            )
            return float(resp.get("ltp", 0)) or None
        except Exception:
            return None
