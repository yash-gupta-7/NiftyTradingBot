"""
strategy_straddle.py — Gamma Scalping Straddle
═══════════════════════════════════════════════

CONCEPT
  Buy ATM Call + ATM Put simultaneously (a straddle).
  Nifty moves in EITHER direction → one leg becomes profitable.
  Exit the winning leg at target. Hold the other leg.
  If Nifty reverses, the held leg now becomes profitable too.
  Repeat throughout the day on fresh setups.

WHY IT WORKS
  You profit from MOVEMENT — direction doesn't matter.
  Every swing up profits the call. Every swing down profits the put.
  This is called gamma scalping — used by professional options desks.

WHY 2-POINT TARGETS DON'T WORK (must read)
  • ATM option bid-ask spread = ₹1.5–3. A 2-pt MID gain → ₹0 at BID.
  • Charges = ₹109 per straddle. 2-pt × 75 = ₹150 gross → ₹41 net.
  • Groww API latency 100–500ms. 2-pt moves happen in 0.3–1s.
  • Minimum viable target: ₹10/unit (₹750 gross, ₹640 net per leg).

PRACTICAL SETUP (per straddle)
  Entry:  Buy ATM Call + ATM Put at market price
  Target: ₹15/unit gain on EITHER leg → exit that leg immediately
          (requires ~30 Nifty point move at delta 0.5)
  SL:     Combined position value drops ₹20/unit below cost
  Trail:  After one leg exits profitably, trail the other leg
  Time:   Max 45 min hold per straddle, then close both legs
  Daily:  Max 5 straddles per day
  Window: 9:20–11:00 AM and 1:30–3:00 PM (high gamma periods)
"""

import logging
from datetime import datetime, timedelta
from typing import Optional, Tuple
from enum import Enum
from dataclasses import dataclass, field

import pytz

from config import (
    NIFTY_LOT_SIZE, LOTS_PER_TRADE,
    V3_MAX_HOLD_MINUTES,
)

logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")

# ─── STRADDLE CONFIG ──────────────────────────────────────────────────────────

# Targets (₹ per unit on the option premium)
TARGET_PER_UNIT     = 15.0    # exit winning leg at +₹15/unit
SL_PER_UNIT         = 10.0    # exit losing leg at -₹10/unit from entry
COMBINED_SL_UNIT    = 20.0    # exit BOTH legs if combined loss > ₹20/unit

# Trail on the held leg after first leg exits
TRAIL_ACTIVATION    = 5.0     # start trailing after held leg gains ₹5/unit
TRAIL_DISTANCE      = 8.0     # trail SL ₹8 below peak on held leg

# Time limits
MAX_HOLD_MINUTES    = 45      # close both legs after 45 min regardless
SQUAREOFF_TIME      = "15:00" # hard close by 3 PM (earlier than main squareoff)

# Valid entry windows
ENTRY_WINDOWS = [
    ("09:20", "11:00"),   # morning: high gamma, large swings
    ("13:30", "15:00"),   # afternoon: pre-close volatility
]

# Minimum Nifty move expected (for setup qualification)
MIN_EXPECTED_MOVE   = 25      # skip if Nifty ATR10 suggests < 25pt expected move today
MAX_TRADES_PER_DAY  = 5       # hard limit on straddle count

# Costs (used for P&L accounting only, not decision-making)
BROKERAGE_PER_STRADDLE = 109.0  # ₹109 realistic (brokerage + STT + exchange + GST)


# ─── STRADDLE STATE ───────────────────────────────────────────────────────────

class StraddleState(Enum):
    IDLE      = "IDLE"
    BOTH_OPEN = "BOTH_OPEN"    # both call and put legs are open
    ONE_OPEN  = "ONE_OPEN"     # one leg closed profitably, other still running
    CLOSED    = "CLOSED"       # both legs closed


@dataclass
class LegStatus:
    symbol:        str   = ""
    entry_premium: float = 0.0
    current_val:   float = 0.0
    peak_val:      float = 0.0
    trail_sl:      float = 0.0
    is_open:       bool  = False
    exit_premium:  float = 0.0
    exit_reason:   str   = ""
    pnl:           float = 0.0


# ─── MAIN STRATEGY CLASS ──────────────────────────────────────────────────────

class StraddleScalper:
    """
    Manages one straddle position (call + put simultaneously).
    Instantiate a new one for each fresh straddle entry.

    Flow:
      1. record_entry()         → log entry premiums for both legs
      2. monitor()              → call every 1.5s from fast loop
         → returns signal string or None
      3. get_summary()          → final P&L report

    Signals returned by monitor():
      None                      → still open, keep monitoring
      "EXIT_CALL:<reason>"      → close the CALL leg
      "EXIT_PUT:<reason>"       → close the PUT leg
      "EXIT_BOTH:<reason>"      → close BOTH legs simultaneously
    """

    def __init__(self, straddle_id: int = 1):
        self.straddle_id   = straddle_id
        self.state         = StraddleState.IDLE
        self.call_leg      = LegStatus()
        self.put_leg       = LegStatus()
        self.entry_time:   Optional[datetime] = None
        self.total_pnl:    float = 0.0
        self._trailing_leg: Optional[str] = None  # "CALL" or "PUT"
        self._last_log_pct: float = 0.0

    # ─── ENTRY ────────────────────────────────────────────────────────────────

    def record_entry(
        self,
        call_symbol:   str,
        put_symbol:    str,
        call_premium:  float,
        put_premium:   float,
    ):
        """Call after BOTH legs are confirmed filled."""
        units = NIFTY_LOT_SIZE * LOTS_PER_TRADE

        self.call_leg = LegStatus(
            symbol        = call_symbol,
            entry_premium = call_premium,
            current_val   = call_premium,
            peak_val      = call_premium,
            trail_sl      = call_premium - SL_PER_UNIT,
            is_open       = True,
        )
        self.put_leg = LegStatus(
            symbol        = put_symbol,
            entry_premium = put_premium,
            current_val   = put_premium,
            peak_val      = put_premium,
            trail_sl      = put_premium - SL_PER_UNIT,
            is_open       = True,
        )

        self.entry_time = datetime.now(IST)
        self.state      = StraddleState.BOTH_OPEN
        total_cost      = (call_premium + put_premium) * units

        logger.info(
            f"🔀 STRADDLE #{self.straddle_id} OPEN | "
            f"CALL {call_symbol} @ ₹{call_premium:.1f} | "
            f"PUT  {put_symbol}  @ ₹{put_premium:.1f} | "
            f"Total outlay: ₹{total_cost:.0f} | "
            f"Target: +₹{TARGET_PER_UNIT}/unit on either leg"
        )

    # ─── MONITOR (call from fast loop every 1.5s) ─────────────────────────────

    def monitor(
        self,
        call_ltp:  Optional[float],
        put_ltp:   Optional[float],
    ) -> Optional[str]:
        """
        Core monitoring function. Call every 1.5s with live LTPs.
        Returns a signal string or None.

        Signal format:
          "EXIT_CALL:<reason>"   → OrderManager closes CALL leg
          "EXIT_PUT:<reason>"    → OrderManager closes PUT leg
          "EXIT_BOTH:<reason>"   → OrderManager closes BOTH legs
          None                   → no action
        """
        if self.state == StraddleState.CLOSED:
            return None
        if call_ltp is None and put_ltp is None:
            return None

        now = datetime.now(IST)

        # ── Time exit ─────────────────────────────────────────────────────────
        mins_held = (now - self.entry_time).seconds // 60 if self.entry_time else 0
        if mins_held >= MAX_HOLD_MINUTES:
            logger.info(f"⏰ Straddle #{self.straddle_id}: {MAX_HOLD_MINUTES}-min time exit")
            return self._close_both("TIME_EXIT")

        if now.strftime("%H:%M") >= SQUAREOFF_TIME:
            return self._close_both("SQUAREOFF")

        # Update current values
        if call_ltp and self.call_leg.is_open:
            self.call_leg.current_val = call_ltp
            self.call_leg.peak_val    = max(self.call_leg.peak_val, call_ltp)
        if put_ltp and self.put_leg.is_open:
            self.put_leg.current_val  = put_ltp
            self.put_leg.peak_val     = max(self.put_leg.peak_val, put_ltp)

        # Log periodically
        self._log_status(mins_held)

        # ── BOTH LEGS OPEN ─────────────────────────────────────────────────────
        if self.state == StraddleState.BOTH_OPEN:
            # Combined SL: if both options lost too much (flat/whipsaw day)
            combined_loss = self._combined_loss_per_unit()
            if combined_loss >= COMBINED_SL_UNIT:
                logger.warning(
                    f"🛑 Combined loss ₹{combined_loss:.1f}/unit >= ₹{COMBINED_SL_UNIT} → close both"
                )
                return self._close_both("COMBINED_SL")

            # Check if CALL leg hit target
            if call_ltp:
                call_gain = call_ltp - self.call_leg.entry_premium
                if call_gain >= TARGET_PER_UNIT:
                    logger.info(
                        f"💰 CALL TARGET: +₹{call_gain:.1f}/unit "
                        f"(₹{call_gain*NIFTY_LOT_SIZE:.0f} gross)"
                    )
                    self.call_leg.exit_premium = call_ltp
                    self.call_leg.exit_reason  = "TARGET"
                    self.call_leg.is_open      = False
                    self.call_leg.pnl          = call_gain * NIFTY_LOT_SIZE * LOTS_PER_TRADE
                    self.state                 = StraddleState.ONE_OPEN
                    self._trailing_leg         = "PUT"
                    self._activate_trail(self.put_leg)
                    return "EXIT_CALL:TARGET"

            # Check if PUT leg hit target
            if put_ltp:
                put_gain = put_ltp - self.put_leg.entry_premium
                if put_gain >= TARGET_PER_UNIT:
                    logger.info(
                        f"💰 PUT TARGET: +₹{put_gain:.1f}/unit "
                        f"(₹{put_gain*NIFTY_LOT_SIZE:.0f} gross)"
                    )
                    self.put_leg.exit_premium = put_ltp
                    self.put_leg.exit_reason  = "TARGET"
                    self.put_leg.is_open      = False
                    self.put_leg.pnl          = put_gain * NIFTY_LOT_SIZE * LOTS_PER_TRADE
                    self.state                = StraddleState.ONE_OPEN
                    self._trailing_leg        = "CALL"
                    self._activate_trail(self.call_leg)
                    return "EXIT_PUT:TARGET"

            # Individual SL on either leg (protect against one-sided crash)
            if call_ltp:
                call_loss = self.call_leg.entry_premium - call_ltp
                if call_loss >= SL_PER_UNIT * 2:  # 2× SL before individual close
                    logger.warning(f"🛑 CALL individual SL: -₹{call_loss:.1f}/unit")
                    self.call_leg.exit_premium = call_ltp
                    self.call_leg.exit_reason  = "INDIVIDUAL_SL"
                    self.call_leg.is_open      = False
                    self.call_leg.pnl          = -call_loss * NIFTY_LOT_SIZE * LOTS_PER_TRADE
                    self.state                 = StraddleState.ONE_OPEN
                    self._trailing_leg         = "PUT"
                    self._activate_trail(self.put_leg)
                    return "EXIT_CALL:INDIVIDUAL_SL"

            if put_ltp:
                put_loss = self.put_leg.entry_premium - put_ltp
                if put_loss >= SL_PER_UNIT * 2:
                    logger.warning(f"🛑 PUT individual SL: -₹{put_loss:.1f}/unit")
                    self.put_leg.exit_premium = put_ltp
                    self.put_leg.exit_reason  = "INDIVIDUAL_SL"
                    self.put_leg.is_open      = False
                    self.put_leg.pnl          = -put_loss * NIFTY_LOT_SIZE * LOTS_PER_TRADE
                    self.state                = StraddleState.ONE_OPEN
                    self._trailing_leg        = "CALL"
                    self._activate_trail(self.call_leg)
                    return "EXIT_PUT:INDIVIDUAL_SL"

        # ── ONE LEG OPEN (trail the survivor) ─────────────────────────────────
        elif self.state == StraddleState.ONE_OPEN:
            held = self.call_leg if self._trailing_leg == "CALL" else self.put_leg
            ltp  = call_ltp if self._trailing_leg == "CALL" else put_ltp

            if ltp is None:
                return None

            gain = ltp - held.entry_premium

            # Update trail SL (only upward)
            if gain >= TRAIL_ACTIVATION:
                new_trail = ltp - TRAIL_DISTANCE
                if new_trail > held.trail_sl:
                    old_trail       = held.trail_sl
                    held.trail_sl   = new_trail
                    held.peak_val   = max(held.peak_val, ltp)
                    logger.info(
                        f"📐 Trail SL raised: ₹{old_trail:.1f} → ₹{held.trail_sl:.1f} "
                        f"(gain=₹{gain:.1f}/unit)"
                    )

            # Trail SL hit?
            if ltp <= held.trail_sl:
                held.exit_premium = ltp
                held.exit_reason  = "TRAIL_SL"
                held.is_open      = False
                held.pnl          = (ltp - held.entry_premium) * NIFTY_LOT_SIZE * LOTS_PER_TRADE
                tag = "EXIT_CALL" if self._trailing_leg == "CALL" else "EXIT_PUT"
                logger.info(
                    f"💰 Trail SL hit: ₹{ltp:.1f} | gain=₹{gain:.1f}/unit | "
                    f"leg P&L=₹{held.pnl:.0f}"
                )
                return self._close_both_from_trail(tag)

            # Second target: held leg also hits target
            if gain >= TARGET_PER_UNIT:
                held.exit_premium = ltp
                held.exit_reason  = "TARGET"
                held.is_open      = False
                held.pnl          = gain * NIFTY_LOT_SIZE * LOTS_PER_TRADE
                tag = "EXIT_CALL" if self._trailing_leg == "CALL" else "EXIT_PUT"
                return f"{tag}:TARGET"

        return None

    # ─── HELPERS ──────────────────────────────────────────────────────────────

    def _combined_loss_per_unit(self) -> float:
        """Combined loss per unit across both open legs."""
        call_loss = max(0, self.call_leg.entry_premium - self.call_leg.current_val)
        put_loss  = max(0, self.put_leg.entry_premium  - self.put_leg.current_val)
        return call_loss + put_loss

    def _activate_trail(self, leg: LegStatus):
        """Set initial trail SL on the surviving leg at entry − SL_PER_UNIT."""
        leg.trail_sl = leg.entry_premium - SL_PER_UNIT
        logger.info(
            f"📐 Trail activated on {leg.symbol} | "
            f"SL=₹{leg.trail_sl:.1f} (entry ₹{leg.entry_premium:.1f} − ₹{SL_PER_UNIT})"
        )

    def _close_both(self, reason: str) -> str:
        self.state = StraddleState.CLOSED
        self._compute_final_pnl()
        return f"EXIT_BOTH:{reason}"

    def _close_both_from_trail(self, first_tag: str) -> str:
        """Called when the trailing leg hits its SL — both legs now closed."""
        self.state = StraddleState.CLOSED
        self._compute_final_pnl()
        reason = first_tag.split(":")[1] if ":" in first_tag else "TRAIL_SL"
        return f"EXIT_BOTH:{reason}"

    def _compute_final_pnl(self):
        """Sums both legs' P&L minus charges."""
        legs_pnl    = self.call_leg.pnl + self.put_leg.pnl
        self.total_pnl = legs_pnl - BROKERAGE_PER_STRADDLE
        icon = "💰" if self.total_pnl >= 0 else "🔴"
        logger.info(
            f"{icon} STRADDLE #{self.straddle_id} CLOSED | "
            f"Call P&L=₹{self.call_leg.pnl:.0f} | "
            f"Put P&L=₹{self.put_leg.pnl:.0f} | "
            f"Charges=₹{BROKERAGE_PER_STRADDLE:.0f} | "
            f"NET=₹{self.total_pnl:.0f}"
        )

    def _log_status(self, mins_held: int):
        """Throttled status log — only on significant moves."""
        if not self.call_leg.is_open and not self.put_leg.is_open:
            return
        call_g = (self.call_leg.current_val - self.call_leg.entry_premium) if self.call_leg.is_open else 0
        put_g  = (self.put_leg.current_val  - self.put_leg.entry_premium)  if self.put_leg.is_open  else 0
        combined_g = call_g + put_g
        if abs(combined_g - self._last_log_pct) >= 2.0:
            logger.info(
                f"📊 Straddle #{self.straddle_id} | min={mins_held} | "
                f"Call={call_g:+.1f} | Put={put_g:+.1f} | Combined={combined_g:+.1f}"
            )
            self._last_log_pct = combined_g

    # ─── SUMMARY ──────────────────────────────────────────────────────────────

    def get_summary(self) -> dict:
        mins = 0
        if self.entry_time:
            mins = int((datetime.now(IST) - self.entry_time).total_seconds() // 60)
        return {
            "straddle_id":      self.straddle_id,
            "state":            self.state.value,
            "call_symbol":      self.call_leg.symbol,
            "put_symbol":       self.put_leg.symbol,
            "call_entry":       self.call_leg.entry_premium,
            "put_entry":        self.put_leg.entry_premium,
            "call_exit":        self.call_leg.exit_premium,
            "put_exit":         self.put_leg.exit_premium,
            "call_pnl":         round(self.call_leg.pnl, 2),
            "put_pnl":          round(self.put_leg.pnl, 2),
            "charges":          BROKERAGE_PER_STRADDLE,
            "net_pnl":          round(self.total_pnl, 2),
            "hold_minutes":     mins,
            "call_exit_reason": self.call_leg.exit_reason,
            "put_exit_reason":  self.put_leg.exit_reason,
        }


# ─── DAILY STRADDLE CONTROLLER ────────────────────────────────────────────────

class StraddleController:
    """
    Manages the full day of straddle trades.
    Enforces MAX_TRADES_PER_DAY and entry window rules.
    """

    def __init__(self):
        self.trades_today:  int   = 0
        self.daily_pnl:     float = 0.0
        self.active:        Optional[StraddleScalper] = None
        self.history:       list  = []

    def can_enter(self) -> Tuple[bool, str]:
        """Returns (allowed, reason)."""
        if self.trades_today >= MAX_TRADES_PER_DAY:
            return False, f"Daily limit: {MAX_TRADES_PER_DAY} straddles reached"
        if not self._in_entry_window():
            return False, "Outside entry windows (9:20–11:00, 13:30–15:00)"
        if self.active and self.active.state not in (StraddleState.CLOSED,):
            return False, "Previous straddle still open"
        return True, "OK"

    def open_straddle(
        self,
        call_symbol:  str,
        put_symbol:   str,
        call_premium: float,
        put_premium:  float,
    ) -> StraddleScalper:
        """Opens a new straddle and registers it."""
        self.trades_today += 1
        straddle = StraddleScalper(straddle_id=self.trades_today)
        straddle.record_entry(call_symbol, put_symbol, call_premium, put_premium)
        self.active = straddle
        return straddle

    def close_straddle(self, straddle: StraddleScalper):
        """Records closed straddle to history."""
        summary = straddle.get_summary()
        self.daily_pnl += summary["net_pnl"]
        self.history.append(summary)
        logger.info(
            f"📊 Daily P&L after straddle #{straddle.straddle_id}: "
            f"₹{self.daily_pnl:.0f} | "
            f"Trades: {self.trades_today}/{MAX_TRADES_PER_DAY}"
        )

    def _in_entry_window(self) -> bool:
        now = datetime.now(IST).strftime("%H:%M")
        return any(start <= now <= end for start, end in ENTRY_WINDOWS)

    def daily_report(self) -> str:
        lines = [
            "═" * 48,
            f"  STRADDLE SCALPER — DAILY REPORT",
            f"  Straddles: {self.trades_today} | Net P&L: ₹{self.daily_pnl:.0f}",
            "─" * 48,
        ]
        for s in self.history:
            icon = "✅" if s["net_pnl"] >= 0 else "🔴"
            lines.append(
                f"  {icon} #{s['straddle_id']} | "
                f"Call {s['call_exit_reason'] or '-'} | "
                f"Put {s['put_exit_reason'] or '-'} | "
                f"Net ₹{s['net_pnl']:.0f}"
            )
        lines.append("═" * 48)
        return "\n".join(lines)
