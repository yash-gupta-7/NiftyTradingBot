"""
strategy_v3.py — Quick Scalp ORB with Continuous Trailing Stop
Exits are entirely trail-based — no hard % targets.

Flow:
  Entry → initial 20% SL → hits +15% checkpoint →
  SL moves to breakeven → trailing SL activates →
  trail updates EVERY candle as premium rises →
  trail tightens as gains grow → market takes you out naturally

Momentum score at 15% sets the trail width:
  Score 0-1  →  trail 10% below peak  (tight — exits fast on reversal)
  Score 2    →  trail 15% below peak  (medium room)
  Score 3    →  trail 20% below peak  (loose — lets strong moves run)

Trail auto-tightens as profit grows:
  Premium gain 15–30%   →  use base trail
  Premium gain 30–50%   →  tighten by 3%   (protect more profit)
  Premium gain 50%+     →  tighten by 6%   (exceptional day — lock it in)

Only hard exits:
  • 30-min time limit
  • Nifty closes back inside opening range (structural false-breakout)
"""

import logging
from datetime import datetime
import pytz
IST = pytz.timezone('Asia/Kolkata')
from typing import Optional
from enum import Enum

logger = logging.getLogger(__name__)

# ─── V3 CONFIG ────────────────────────────────────────────────────────────────

# All constants imported from config.py — no magic numbers here
from config import (
    V3_OPENING_RANGE_END  as _V3_RANGE_END,
    V3_ENTRY_WINDOW_START as _V3_ENTRY_START,
    V3_ENTRY_WINDOW_END   as _V3_ENTRY_END,
    V3_BREAKOUT_BUFFER    as BREAKOUT_BUFFER_PTS,
    V3_BROKERAGE          as BROKERAGE,
    V3_MIN_RANGE_PTS      as MIN_RANGE_PTS,
    V3_MAX_RANGE_PTS      as MAX_RANGE_PTS,
    V3_SL_INITIAL_PCT     as SL_INITIAL_PCT,
    V3_CHECKPOINT_PCT     as CHECKPOINT_PCT,
    V3_TRAIL_BY_SCORE     as TRAIL_BY_SCORE,
    V3_TRAIL_TIGHTEN      as TRAIL_TIGHTEN,
    V3_MAX_HOLD_MINUTES   as MAX_HOLD_MINUTES,
    VOLUME_MULTIPLIER,
)
OPENING_RANGE_MINS = 15     # 9:15–9:30

# Initial stop loss (before checkpoint)







# ─── MOMENTUM SCORER ──────────────────────────────────────────────────────────

class MomentumScore:
    """3-point score evaluated at the 15% checkpoint."""

    @staticmethod
    def evaluate(
        candles_df,
        direction: str,
        current_volume: float,
        avg_volume: float,
        banknifty_price: Optional[float],
        bnf_range_high: Optional[float],
        bnf_range_low:  Optional[float],
    ) -> int:
        score = 0

        # 1 — Volume still elevated on current candle
        if avg_volume > 0 and current_volume >= avg_volume * VOLUME_MULTIPLIER:
            score += 1
            logger.info("  +1: volume still elevated")

        # 2 — Last 2 candles both moving in trade direction
        if candles_df is not None and len(candles_df) >= 2:
            last2  = list(candles_df.tail(2)["close"])
            if direction == "UP"   and last2[-1] > last2[-2]: score += 1; logger.info("  +1: candles bullish")
            if direction == "DOWN" and last2[-1] < last2[-2]: score += 1; logger.info("  +1: candles bearish")

        # 3 — BankNifty still confirming
        if banknifty_price and bnf_range_high and bnf_range_low:
            if direction == "UP"   and banknifty_price > bnf_range_high: score += 1; logger.info("  +1: BNF above range H")
            if direction == "DOWN" and banknifty_price < bnf_range_low:  score += 1; logger.info("  +1: BNF below range L")

        logger.info(f"📊 Momentum score: {score}/3")
        return score


# ─── STATE ────────────────────────────────────────────────────────────────────

class V3State(Enum):
    """
    INTERNAL state for checkpoint/trail logic only.
    External code must check OrderManager.state (TradeState) for authoritative
    trade lifecycle — not this enum. V3 emits exit signals; OrderManager acts.
    """
    IDLE     = "IDLE"
    OPEN     = "OPEN"        # active, initial SL
    TRAILING = "TRAILING"    # past 15% checkpoint, trail SL active
    CLOSED   = "CLOSED"


# ─── MAIN STRATEGY CLASS ──────────────────────────────────────────────────────

class QuickScalpStrategy:
    """
    Full lifecycle for the V3 Quick Scalp trade.
    Wire into main.py with --v3 flag.
    """

    def __init__(self):
        # Opening range
        self.range_high:    Optional[float] = None
        self.range_low:     Optional[float] = None
        self.range_size:    Optional[float] = None
        self.bnf_range_high: Optional[float] = None
        self.bnf_range_low:  Optional[float] = None

        # Trade state
        self.state          = V3State.IDLE
        self.direction:     Optional[str]   = None   # "UP" | "DOWN"
        self.entry_premium: Optional[float] = None
        self.entry_time:    Optional[datetime] = None

        # SL tracking
        self.current_sl:    Optional[float] = None   # absolute premium ₹ value
        self.peak_premium:  Optional[float] = None   # rolling highest premium seen
        self.base_trail_pct: float          = 0.10   # set by momentum score

        # Results
        self.realised_pnl:  float           = 0.0
        self.exit_reason:   Optional[str]   = None
        self.exit_premium:  Optional[float] = None
        self.exit_time:     Optional[datetime] = None
        self.momentum_score: int            = 0
        self.peak_gain_pct: float           = 0.0    # track highest % gain seen

    # ─── RANGE LOCK ───────────────────────────────────────────────────────────

    def set_opening_range(
        self,
        range_high: float,
        range_low: float,
        bnf_high: Optional[float] = None,
        bnf_low:  Optional[float] = None,
    ) -> bool:
        """Lock the 9:15–9:30 candle range. Returns False if range is invalid."""
        self.range_high     = range_high
        self.range_low      = range_low
        self.range_size     = round(range_high - range_low, 2)
        self.bnf_range_high = bnf_high
        self.bnf_range_low  = bnf_low

        logger.info(
            f"📊 V3 Range: H={range_high} / L={range_low} = {self.range_size} pts"
        )

        if self.range_size < MIN_RANGE_PTS:
            logger.info(f"🚫 Range too small ({self.range_size} < {MIN_RANGE_PTS}) — skip")
            return False
        if self.range_size > MAX_RANGE_PTS:
            logger.info(f"🚫 Range too large ({self.range_size} > {MAX_RANGE_PTS}) — skip")
            return False
        return True

    # ─── BREAKOUT CHECK ───────────────────────────────────────────────────────

    def check_breakout(
        self,
        candle_close: float,
        candle_volume: float,
        avg_volume: float,
        banknifty_price: Optional[float],
        current_time: Optional[str] = None,
    ) -> Optional[str]:
        """
        Call on every 5-min candle close from 9:30 to 9:40.
        Returns "UP", "DOWN", or None.
        """
        if self.state != V3State.IDLE:
            return None
        if not self.range_high:
            return None

        # Hard time gate — only first 1–2 candles after 9:30
        t = current_time or datetime.now(IST).strftime("%H:%M")
        if t > "09:40":
            return None

        # Volume check
        if avg_volume > 0 and candle_volume < avg_volume * VOLUME_MULTIPLIER:
            logger.info(f"🔇 V3 volume weak: {candle_volume:.0f} < {avg_volume * VOLUME_MULTIPLIER:.0f}")
            return None

        if candle_close > self.range_high + BREAKOUT_BUFFER_PTS:
            if banknifty_price and self.bnf_range_high:
                if banknifty_price <= self.bnf_range_high:
                    logger.info("🏦 BNF not confirming UP — skip")
                    return None
            logger.info(f"📈 V3 UP breakout: {candle_close:.1f} > {self.range_high + BREAKOUT_BUFFER_PTS:.1f}")
            return "UP"

        if candle_close < self.range_low - BREAKOUT_BUFFER_PTS:
            if banknifty_price and self.bnf_range_low:
                if banknifty_price >= self.bnf_range_low:
                    logger.info("🏦 BNF not confirming DOWN — skip")
                    return None
            logger.info(f"📉 V3 DOWN breakout: {candle_close:.1f} < {self.range_low - BREAKOUT_BUFFER_PTS:.1f}")
            return "DOWN"

        return None

    # ─── ENTRY ────────────────────────────────────────────────────────────────

    def record_entry(self, direction: str, entry_premium: float):
        """Call immediately after order fills."""
        self.direction      = direction
        self.entry_premium  = entry_premium
        self.peak_premium   = entry_premium
        self.entry_time     = datetime.now(IST)
        self.current_sl     = round(entry_premium * (1 - SL_INITIAL_PCT), 2)
        self.state          = V3State.OPEN

        logger.info(
            f"🚀 V3 ENTRY | {direction} | Premium=₹{entry_premium:.2f} | "
            f"Initial SL=₹{self.current_sl:.2f} (−20%)"
        )

    # ─── MONITOR (call every 5-min candle close) ──────────────────────────────

    def monitor(
        self,
        current_premium: float,
        current_volume:  float,
        avg_volume:      float,
        candles_df,
        banknifty_price: Optional[float],
        nifty_price:     float,
        lot_size:        int = 75,
    ) -> Optional[str]:
        """
        Emits a signal string — does NOT place orders directly.
        Return values:
          None               → no action yet, keep monitoring
          'EXIT:<reason>'    → OrderManager should call _exit_all(reason)
          'SL_UPDATE:<val>'  → OrderManager should raise SL to <val>
        OrderManager (not strategy) owns the authoritative trade state.
        """
        if self.state == V3State.CLOSED or self.entry_premium is None:
            return None

        now       = datetime.now(IST)
        mins_held = int((now - self.entry_time).total_seconds() // 60)
        gain_pct  = (current_premium - self.entry_premium) / self.entry_premium
        self.peak_gain_pct = max(self.peak_gain_pct, gain_pct)

        logger.info(
            f"📊 V3 | Premium=₹{current_premium:.1f} | "
            f"Gain={gain_pct*100:.1f}% | Peak={self.peak_gain_pct*100:.1f}% | "
            f"Trail SL=₹{self.current_sl:.1f} | Mins={mins_held}"
        )

        # ── HARD EXIT 1: time limit ────────────────────────────────────────────
        if mins_held >= MAX_HOLD_MINUTES:
            logger.info(f"⏰ V3: {MAX_HOLD_MINUTES}-min limit — exit at ₹{current_premium:.1f}")
            return self._close("TIME_EXIT", current_premium, lot_size)

        # ── HARD EXIT 2: structural SL (Nifty back inside range) ──────────────
        if self._structural_sl_hit(nifty_price):
            logger.info("🛑 V3: Nifty back inside opening range — structural SL")
            return self._close("STRUCTURAL_SL", current_premium, lot_size)

        # ── TRAIL UPDATE: always update peak and recompute trail SL ───────────
        if current_premium > self.peak_premium:
            self.peak_premium = current_premium
            if self.state == V3State.TRAILING:
                new_sl = self._compute_trail_sl(current_premium, gain_pct)
                # Trail SL only ever moves UP — never lower
                if new_sl > self.current_sl:
                    old_sl = self.current_sl
                    self.current_sl = round(new_sl, 2)
                    logger.info(
                        f"📈 Trail SL raised: ₹{old_sl:.1f} → ₹{self.current_sl:.1f} "
                        f"(peak=₹{self.peak_premium:.1f}, gain={gain_pct*100:.1f}%)"
                    )

        # ── SL HIT check ──────────────────────────────────────────────────────
        if current_premium <= self.current_sl:
            reason = "TRAIL_SL" if self.state == V3State.TRAILING else "INITIAL_SL"
            logger.info(
                f"🛑 V3 {reason}: ₹{current_premium:.1f} <= SL ₹{self.current_sl:.1f}"
            )
            return self._close(reason, current_premium, lot_size)

        # ── CHECKPOINT: first time gain hits +15% ─────────────────────────────
        if self.state == V3State.OPEN and gain_pct >= CHECKPOINT_PCT:
            logger.info(
                f"🎯 V3 CHECKPOINT +{gain_pct*100:.1f}% reached | "
                f"SL moving to BREAKEVEN"
            )

            # Score momentum
            self.momentum_score = MomentumScore.evaluate(
                candles_df=candles_df,
                direction=self.direction,
                current_volume=current_volume,
                avg_volume=avg_volume,
                banknifty_price=banknifty_price,
                bnf_range_high=self.bnf_range_high,
                bnf_range_low=self.bnf_range_low,
            )

            # Set trail width based on score
            self.base_trail_pct = TRAIL_BY_SCORE[self.momentum_score]

            # Move SL to BREAKEVEN — can never lose from here
            self.current_sl = self.entry_premium
            self.state      = V3State.TRAILING

            logger.info(
                f"✅ Trailing activated | Score={self.momentum_score}/3 | "
                f"Base trail={self.base_trail_pct*100:.0f}% | "
                f"SL=₹{self.current_sl:.2f} (breakeven)"
            )

            # Immediately compute first trail SL from current level
            first_trail_sl = self._compute_trail_sl(current_premium, gain_pct)
            if first_trail_sl > self.current_sl:
                self.current_sl = round(first_trail_sl, 2)
                logger.info(f"📐 First trail SL set: ₹{self.current_sl:.2f}")

        return None  # trade still open

    # ─── TRAIL SL CALCULATOR ──────────────────────────────────────────────────

    def _compute_trail_sl(self, current_premium: float, gain_pct: float) -> float:
        """
        Computes the trail SL as a % below the current PEAK premium.
        Auto-tightens as gains grow so we protect more profit on big moves.

              Gain 15–30%  →  base trail (10/15/20%)
              Gain 30–50%  →  base trail − 3%  (tighter)
              Gain 50%+    →  base trail − 6%  (very tight — exceptional day)

        SL is always anchored to PEAK (never current_premium directly).
        """
        trail = self.base_trail_pct

        # Tighten trail as profits grow
        for threshold, tighten in TRAIL_TIGHTEN:
            if gain_pct >= threshold:
                trail = max(trail - tighten, 0.05)  # never tighten below 5%
                break

        trail_sl = self.peak_premium * (1 - trail)

        logger.debug(
            f"Trail calc: peak=₹{self.peak_premium:.1f} × "
            f"(1 − {trail*100:.0f}%) = ₹{trail_sl:.1f}"
        )
        return trail_sl

    # ─── STRUCTURAL SL ────────────────────────────────────────────────────────

    def _structural_sl_hit(self, nifty_price: float) -> bool:
        """Price re-entered opening range = false breakout confirmed."""
        if not self.range_high or not self.range_low:
            return False
        if self.direction == "UP":
            return nifty_price < self.range_high
        return nifty_price > self.range_low

    # ─── CLOSE ────────────────────────────────────────────────────────────────

    def _close(self, reason: str, exit_premium: float, lot_size: int = 75) -> str:
        gross = (exit_premium - self.entry_premium) * lot_size
        net   = gross - BROKERAGE

        self.realised_pnl  = round(net, 2)
        self.exit_reason   = reason
        self.exit_premium  = exit_premium
        self.exit_time     = datetime.now(IST)
        self.state         = V3State.CLOSED

        mins = int((self.exit_time - self.entry_time).total_seconds() // 60)
        icon = "💰" if net >= 0 else "🔴"

        logger.info(
            f"{icon} V3 EXIT:{reason} | "
            f"Entry=₹{self.entry_premium:.1f} → Exit=₹{exit_premium:.1f} | "
            f"Peak gain={self.peak_gain_pct*100:.1f}% | "
            f"Held={mins} min | Est P&L=₹{net:.0f}"
        )
        return f"EXIT:{reason}"   # OrderManager reads prefix to act

    # ─── SUMMARY ──────────────────────────────────────────────────────────────

    def get_summary(self) -> dict:
        dur = None
        if self.entry_time and self.exit_time:
            dur = int((self.exit_time - self.entry_time).total_seconds() // 60)

        return {
            "version":         "V3_TRAILING",
            "direction":       self.direction,
            "state":           self.state.value,
            "entry_premium":   self.entry_premium,
            "peak_premium":    self.peak_premium,
            "peak_gain_pct":   f"{self.peak_gain_pct*100:.1f}%",
            "momentum_score":  self.momentum_score,
            "base_trail_pct":  f"{self.base_trail_pct*100:.0f}%",
            "exit_reason":     self.exit_reason,
            "exit_premium":    self.exit_premium,
            "realised_pnl":    self.realised_pnl,
            "hold_minutes":    dur,
            "entry_time":      self.entry_time.strftime("%H:%M:%S") if self.entry_time else None,
            "exit_time":       self.exit_time.strftime("%H:%M:%S") if self.exit_time else None,
        }
