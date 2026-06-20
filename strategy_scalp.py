"""
strategy_scalp.py — Nifty 1-Min Momentum Scalp
════════════════════════════════════════════════

Entry:    VWAP + 10-candle swing break with 2.5× volume
SL:       2× option 1-min ATR (floor ₹2.5, cap ₹5.0)
          OR level break OR momentum gate (60s, <₹0.5 move)
Target:   Trailing stop — score 0→exit at +₹2, 1→trail 1.5pt, 2→trail 1pt
Limits:   10 trades/day, ₹2,000 daily loss limit, 3-loss pause 30 min
"""

import logging
from collections import deque
from datetime import datetime, timedelta
from typing import Optional, Tuple
from enum import Enum
from dataclasses import dataclass, field

import pytz
import numpy as np

logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")

# ─── ALL CONSTANTS (single place, no magic numbers) ───────────────────────────

LOT_SIZE             = 65      # verify via live API — NSE may revise
# NOTE: Nifty F&O lot size was revised from 75 → 25 in 2024.
# Run: groww.get_option_chain(...) and check lot_size field before live trading.

# Entry filters
SWING_LOOKBACK       = 10      # candles for swing high/low detection
VOLUME_ENTRY_MULT    = 2.5     # breakout candle needs 2.5× slot average
VOLUME_HOLD_MULT     = 1.5     # momentum gate: volume must stay above this

# Stop loss
OPTION_ATR_PERIOD    = 10      # 1-min candles to compute option ATR
OPTION_ATR_MULT      = 2.0     # SL = 2× option ATR
SL_FLOOR             = 2.5     # minimum SL in ₹/unit
SL_CAP               = 5.0     # maximum SL in ₹/unit

# Momentum gate
GATE_CHECK_SECS      = 30      # check every 30 seconds
GATE_FAIL_COUNT      = 2       # 2 consecutive fails → exit cheap
GATE_MIN_MOVE        = 0.5     # option must have moved ₹0.5 toward target

# Profit trailing
CHECKPOINT_UNIT      = 2.0     # ₹2/unit → check momentum score
TRAIL_SCORE_0        = None    # score 0 → exit immediately at checkpoint
TRAIL_SCORE_1        = 1.5     # score 1 → trail ₹1.5 below rolling peak
TRAIL_SCORE_2        = 1.0     # score 2 → trail ₹1.0 below rolling peak

# Daily risk limits
MAX_TRADES_PER_DAY   = 10
DAILY_LOSS_LIMIT     = 2000.0  # ₹ — stop for day if hit
CONSEC_LOSS_PAUSE    = 3       # pause 30 min after this many consecutive losses
PAUSE_MINUTES        = 30
CHARGES_PER_TRADE    = 80.0    # ₹ round trip (brokerage + STT + exchange + GST)

# Time
TRADE_START          = "09:20"
TRADE_END            = "15:00"


# ─── DATA STRUCTURES ──────────────────────────────────────────────────────────

class ScalpState(Enum):
    IDLE       = "IDLE"
    OPEN       = "OPEN"          # in trade, within momentum gate window
    TRAILING   = "TRAILING"      # past checkpoint, trailing stop active
    CLOSED     = "CLOSED"


@dataclass
class Candle:
    time:   str
    open:   float
    high:   float
    low:    float
    close:  float
    volume: float


@dataclass
class ScalpSignal:
    direction:    str    # "CALL" or "PUT"
    entry_level:  float  # the swing level that was broken
    vwap:         float
    volume:       float
    avg_volume:   float
    candle_time:  str


# ─── INDICATOR ENGINE ─────────────────────────────────────────────────────────

class IndicatorEngine:
    """
    Maintains a rolling 1-min candle buffer.
    Computes VWAP, swing highs/lows, volume slot averages.
    """

    def __init__(self):
        # Rolling Nifty spot candles (1-min)
        self._nifty_candles: deque = deque(maxlen=60)

        # Option premium 1-min "candles" (open/close of premium each minute)
        self._option_premiums: deque = deque(maxlen=OPTION_ATR_PERIOD + 5)

        # VWAP accumulators (reset at 9:15 AM each day)
        self._cum_pv:  float = 0.0
        self._cum_vol: float = 0.0
        self.vwap:     Optional[float] = None

        # Volume slot averages — passed in from VolumeCache
        self._slot_averages: dict = {}

    def reset_day(self):
        self._nifty_candles.clear()
        self._option_premiums.clear()
        self._cum_pv  = 0.0
        self._cum_vol = 0.0
        self.vwap     = None

    def set_slot_averages(self, slot_averages: dict):
        """Load from VolumeCache. Format: {"09:20": 42000.0, ...}"""
        self._slot_averages = slot_averages

    def add_nifty_candle(self, c: Candle):
        """Add a new 1-min Nifty candle. Updates VWAP and swing levels."""
        self._nifty_candles.append(c)
        tp = (c.high + c.low + c.close) / 3.0
        self._cum_pv  += tp * c.volume
        self._cum_vol += c.volume
        if self._cum_vol > 0:
            self.vwap = round(self._cum_pv / self._cum_vol, 2)

    def add_option_premium(self, minute_str: str, open_p: float, close_p: float):
        """
        Add a 1-min option premium observation.
        Used to compute option ATR for dynamic SL.
        """
        move = abs(close_p - open_p)
        self._option_premiums.append(move)

    def compute_option_atr(self) -> float:
        """
        Returns the 1-min ATR of the option premium.
        Used for dynamic SL: SL = clamp(2× ATR, floor, cap)
        """
        if len(self._option_premiums) < 3:
            return SL_FLOOR   # not enough data yet — use floor
        atr = float(np.mean(list(self._option_premiums)[-OPTION_ATR_PERIOD:]))
        return atr

    def compute_dynamic_sl(self) -> float:
        """Returns the SL in ₹/unit, clamped between floor and cap."""
        raw_sl = self.compute_option_atr() * OPTION_ATR_MULT
        return max(SL_FLOOR, min(SL_CAP, round(raw_sl, 2)))

    def get_swing_high(self) -> Optional[float]:
        """Highest high of last SWING_LOOKBACK candles."""
        if len(self._nifty_candles) < SWING_LOOKBACK:
            return None
        recent = list(self._nifty_candles)[-SWING_LOOKBACK:]
        return max(c.high for c in recent)

    def get_swing_low(self) -> Optional[float]:
        """Lowest low of last SWING_LOOKBACK candles."""
        if len(self._nifty_candles) < SWING_LOOKBACK:
            return None
        recent = list(self._nifty_candles)[-SWING_LOOKBACK:]
        return min(c.low for c in recent)

    def get_slot_avg_volume(self, time_str: str) -> float:
        """10-day average volume for this 1-min time slot."""
        return self._slot_averages.get(time_str, 0.0)

    def last_n_nifty_closes(self, n: int) -> list:
        """Last N Nifty 1-min closing prices."""
        candles = list(self._nifty_candles)
        return [c.close for c in candles[-n:]]


# ─── ENTRY SIGNAL DETECTOR ────────────────────────────────────────────────────

class EntryDetector:
    """
    Detects valid entry signals on each new 1-min candle.
    Both VWAP+swing break AND volume must agree.
    """

    def __init__(self, indicators: IndicatorEngine):
        self.ind = indicators

    def check(self, candle: Candle) -> Optional[ScalpSignal]:
        """
        Call after each 1-min Nifty candle closes.
        Returns ScalpSignal if valid entry, None otherwise.
        """
        if self.ind.vwap is None:
            return None

        swing_high = self.ind.get_swing_high()
        swing_low  = self.ind.get_swing_low()
        if swing_high is None or swing_low is None:
            return None

        slot_avg = self.ind.get_slot_avg_volume(candle.time)
        if slot_avg <= 0:
            return None   # no volume baseline for this slot yet

        # ── CALL SIGNAL ───────────────────────────────────────────────────────
        # Candle closes ABOVE VWAP and ABOVE the 10-candle swing high
        if (candle.close > self.ind.vwap and
                candle.close > swing_high and
                candle.volume >= slot_avg * VOLUME_ENTRY_MULT):

            logger.info(
                f"📈 CALL signal | close={candle.close:.1f} > "
                f"VWAP={self.ind.vwap:.1f} & swing_H={swing_high:.1f} | "
                f"vol={candle.volume:.0f} ({candle.volume/slot_avg:.1f}× avg)"
            )
            return ScalpSignal(
                direction   = "CALL",
                entry_level = swing_high,
                vwap        = self.ind.vwap,
                volume      = candle.volume,
                avg_volume  = slot_avg,
                candle_time = candle.time,
            )

        # ── PUT SIGNAL ────────────────────────────────────────────────────────
        if (candle.close < self.ind.vwap and
                candle.close < swing_low and
                candle.volume >= slot_avg * VOLUME_ENTRY_MULT):

            logger.info(
                f"📉 PUT signal | close={candle.close:.1f} < "
                f"VWAP={self.ind.vwap:.1f} & swing_L={swing_low:.1f} | "
                f"vol={candle.volume:.0f} ({candle.volume/slot_avg:.1f}× avg)"
            )
            return ScalpSignal(
                direction   = "PUT",
                entry_level = swing_low,
                vwap        = self.ind.vwap,
                volume      = candle.volume,
                avg_volume  = slot_avg,
                candle_time = candle.time,
            )

        return None


# ─── MOMENTUM SCORER ──────────────────────────────────────────────────────────

class MomentumScorer:
    """
    Scores 0–2 at the ₹2 checkpoint.
    Determines how wide the trailing stop is.
    """

    @staticmethod
    def score(
        direction:     str,
        current_vol:   float,
        avg_vol:       float,
        last_2_closes: list,
    ) -> int:
        s = 0

        # +1: volume still elevated
        if avg_vol > 0 and current_vol >= avg_vol * VOLUME_HOLD_MULT:
            s += 1
            logger.info("  Momentum +1: volume still strong")

        # +1: last 2 Nifty 1-min candles moving in trade direction
        if len(last_2_closes) >= 2:
            if direction == "CALL" and last_2_closes[-1] > last_2_closes[-2]:
                s += 1
                logger.info("  Momentum +1: Nifty candles bullish")
            elif direction == "PUT" and last_2_closes[-1] < last_2_closes[-2]:
                s += 1
                logger.info("  Momentum +1: Nifty candles bearish")

        logger.info(f"📊 Momentum score: {s}/2")
        return s


# ─── POSITION MANAGER ─────────────────────────────────────────────────────────

class ScalpPosition:
    """
    Manages a single open scalp trade from entry to exit.
    Emit signals — does NOT place orders.

    Signals:
      None                → hold
      "EXIT:<reason>"     → close now at market
    """

    def __init__(self, signal: ScalpSignal, entry_premium: float, dynamic_sl: float):
        self.signal        = signal
        self.direction     = signal.direction
        self.entry_level   = signal.entry_level
        self.entry_premium = entry_premium
        self.entry_time    = datetime.now(IST)

        self.state         = ScalpState.OPEN
        self.current_sl    = round(entry_premium - dynamic_sl, 2)
        self.peak_premium  = entry_premium
        self.trail_width   = None   # set at checkpoint
        self.momentum_score = None  # FIX A: remember score for logging

        # Momentum gate counters
        self._gate_fails   = 0
        self._last_gate_check = datetime.now(IST)
        self._last_premium = entry_premium

        # Result
        self.exit_premium  = 0.0
        self.exit_reason   = ""
        self.realised_pnl  = 0.0

        logger.info(
            f"🚀 SCALP ENTRY | {self.direction} | "
            f"Premium=₹{entry_premium:.2f} | "
            f"Dynamic SL=₹{dynamic_sl:.2f}/unit (SL level=₹{self.current_sl:.2f}) | "
            f"Trigger level={self.entry_level:.1f}"
        )

    # ─── FAST MONITOR (call every 1.5s from fast loop) ────────────────────────

    def monitor_fast(
        self,
        option_ltp:    float,
        nifty_price:   float,
        current_vol:   float,
        avg_vol:       float,
    ) -> Optional[str]:
        """
        Runs every 1.5 seconds.
        Checks: premium SL, structural SL, momentum gate.
        Returns "EXIT:<reason>" or None.
        """
        if self.state == ScalpState.CLOSED:
            return None

        now        = datetime.now(IST)
        gain       = option_ltp - self.entry_premium
        gain_per_u = gain

        # Update peak
        if option_ltp > self.peak_premium:
            self.peak_premium = option_ltp

        # ── STRUCTURAL SL: Nifty crosses back through entry level ─────────────
        if self.direction == "CALL" and nifty_price < self.entry_level:
            logger.info(f"🛑 Structural SL: Nifty {nifty_price:.1f} < level {self.entry_level:.1f}")
            return self._close("STRUCTURAL_SL", option_ltp)

        if self.direction == "PUT" and nifty_price > self.entry_level:
            logger.info(f"🛑 Structural SL: Nifty {nifty_price:.1f} > level {self.entry_level:.1f}")
            return self._close("STRUCTURAL_SL", option_ltp)

        # ── PREMIUM SL ────────────────────────────────────────────────────────
        if option_ltp <= self.current_sl:
            logger.info(
                f"🛑 Premium SL: ₹{option_ltp:.2f} <= SL ₹{self.current_sl:.2f}"
            )
            return self._close("PREMIUM_SL", option_ltp)

        # ── MOMENTUM GATE (only while in OPEN state, first 90s) ───────────────
        if self.state == ScalpState.OPEN:
            secs_held = (now - self.entry_time).total_seconds()

            if secs_held <= 90:
                # Check every GATE_CHECK_SECS seconds
                secs_since_check = (now - self._last_gate_check).total_seconds()
                if secs_since_check >= GATE_CHECK_SECS:
                    self._last_gate_check = now
                    move_toward_target = gain_per_u  # positive = good direction

                    vol_ok  = avg_vol > 0 and current_vol >= avg_vol * VOLUME_HOLD_MULT
                    move_ok = move_toward_target >= GATE_MIN_MOVE

                    if not vol_ok and not move_ok:
                        self._gate_fails += 1
                        logger.info(
                            f"⚠️  Gate fail #{self._gate_fails}: "
                            f"vol={current_vol:.0f} ({current_vol/(avg_vol or 1):.1f}×) | "
                            f"move=₹{move_toward_target:.2f}"
                        )
                        if self._gate_fails >= GATE_FAIL_COUNT:
                            logger.info("🚪 Momentum gate: exiting cheap to preserve charges")
                            return self._close("MOMENTUM_GATE", option_ltp)
                    else:
                        self._gate_fails = 0   # reset on any pass

            else:
                # 90 seconds up, still no checkpoint → exit
                if gain_per_u < CHECKPOINT_UNIT:
                    logger.info(f"⏰ Time SL (90s): gain=₹{gain_per_u:.2f} < ₹{CHECKPOINT_UNIT}")
                    return self._close("TIME_SL", option_ltp)

        # ── TRAILING STOP (TRAILING state only) ───────────────────────────────
        if self.state == ScalpState.TRAILING and self.trail_width is not None:
            new_sl = self.peak_premium - self.trail_width
            if new_sl > self.current_sl:
                old = self.current_sl
                self.current_sl = round(new_sl, 2)
                logger.info(
                    f"📐 Trail SL raised: ₹{old:.2f} → ₹{self.current_sl:.2f} "
                    f"(peak=₹{self.peak_premium:.2f})"
                )

            if option_ltp <= self.current_sl:
                logger.info(
                    f"💰 Trail SL hit: ₹{option_ltp:.2f} | "
                    f"gain=₹{gain_per_u:.2f}/unit"
                )
                return self._close("TRAIL_SL", option_ltp)

        return None  # still open

    # ─── SLOW MONITOR (call on each 1-min candle close) ───────────────────────

    def monitor_slow(
        self,
        option_ltp:   float,
        current_vol:  float,
        avg_vol:      float,
        last_2_closes: list,
    ) -> Optional[str]:
        """
        Runs on each 1-min candle close.
        Checks: checkpoint (₹2 hit), assign trail width.
        """
        if self.state != ScalpState.OPEN:
            return None

        gain = option_ltp - self.entry_premium

        if gain >= CHECKPOINT_UNIT:
            score = MomentumScorer.score(
                direction     = self.direction,
                current_vol   = current_vol,
                avg_vol       = avg_vol,
                last_2_closes = last_2_closes,
            )
            self.momentum_score = score   # FIX A: persist for logging

            if score == 0:
                # No momentum — take the ₹2 now
                logger.info(f"📊 Score 0 → exit at checkpoint +₹{gain:.2f}")
                return self._close("CHECKPOINT_EXIT", option_ltp)

            elif score == 1:
                self.trail_width = TRAIL_SCORE_1
                self.state       = ScalpState.TRAILING
                self.current_sl  = round(self.peak_premium - self.trail_width, 2)
                logger.info(
                    f"📊 Score 1 → trail ₹{TRAIL_SCORE_1} | "
                    f"SL=₹{self.current_sl:.2f}"
                )

            else:  # score == 2
                self.trail_width = TRAIL_SCORE_2
                self.state       = ScalpState.TRAILING
                self.current_sl  = round(self.peak_premium - self.trail_width, 2)
                logger.info(
                    f"📊 Score 2 → trail ₹{TRAIL_SCORE_2} (run free) | "
                    f"SL=₹{self.current_sl:.2f}"
                )

        return None

    # ─── CLOSE ────────────────────────────────────────────────────────────────

    def _close(self, reason: str, exit_premium: float) -> str:
        gross = (exit_premium - self.entry_premium) * LOT_SIZE
        net   = gross - CHARGES_PER_TRADE

        self.exit_premium = exit_premium
        self.exit_reason  = reason
        self.realised_pnl = round(net, 2)
        self.state        = ScalpState.CLOSED

        icon = "💰" if net >= 0 else "🔴"
        held = int((datetime.now(IST) - self.entry_time).total_seconds())
        logger.info(
            f"{icon} SCALP CLOSED | {reason} | "
            f"Entry=₹{self.entry_premium:.2f} Exit=₹{exit_premium:.2f} | "
            f"Gain=₹{exit_premium-self.entry_premium:.2f}/unit | "
            f"Net P&L=₹{net:.0f} | Held={held}s"
        )
        return f"EXIT:{reason}"

    def get_summary(self) -> dict:
        held = int((datetime.now(IST) - self.entry_time).total_seconds())
        return {
            "direction":     self.direction,
            "entry_level":   self.entry_level,
            "entry_premium": self.entry_premium,
            "exit_premium":  self.exit_premium,
            "exit_reason":   self.exit_reason,
            "peak_premium":  self.peak_premium,
            "trail_width":   self.trail_width,
            "momentum_score": self.momentum_score,
            "net_pnl":       self.realised_pnl,
            "hold_seconds":  held,
            "state":         self.state.value,
        }


# ─── DAILY CONTROLLER ─────────────────────────────────────────────────────────

class ScalpDayController:
    """
    Manages the full day's trading lifecycle:
    - Enforces MAX_TRADES_PER_DAY
    - Tracks daily P&L
    - Enforces ₹2,000 daily loss limit
    - Manages 3-loss pause
    - Time window gate
    """

    def __init__(self):
        self.trades_today:    int   = 0
        self.daily_pnl:       float = 0.0
        self.consecutive_loss: int  = 0
        self.pause_until:     Optional[datetime] = None
        self.trade_history:   list  = []
        self.active_position: Optional[ScalpPosition] = None

    # ─── GATE CHECK ───────────────────────────────────────────────────────────

    def can_enter(self) -> Tuple[bool, str]:
        """Master gate — all conditions must pass before entry."""
        now     = datetime.now(IST)
        now_str = now.strftime("%H:%M")

        if not getattr(self, '_in_trade_hours', lambda: (
                TRADE_START <= now_str < TRADE_END))():
            return False, f"Outside trade hours ({TRADE_START}–{TRADE_END})"

        if self.trades_today >= MAX_TRADES_PER_DAY:
            return False, f"Daily limit reached ({MAX_TRADES_PER_DAY} trades)"

        if self.daily_pnl <= -DAILY_LOSS_LIMIT:
            return False, f"Daily loss limit ₹{DAILY_LOSS_LIMIT:.0f} hit"

        if self.pause_until and now < self.pause_until:
            remaining = int((self.pause_until - now).total_seconds() // 60)
            return False, f"Paused after {CONSEC_LOSS_PAUSE} losses ({remaining} min remaining)"

        if self.active_position and self.active_position.state != ScalpState.CLOSED:
            return False, "Position already open"

        return True, "OK"

    # ─── OPEN / CLOSE ─────────────────────────────────────────────────────────

    def open_position(
        self,
        signal:        ScalpSignal,
        entry_premium: float,
        dynamic_sl:    float,
    ) -> ScalpPosition:
        pos = ScalpPosition(signal, entry_premium, dynamic_sl)
        self.active_position = pos
        self.trades_today   += 1
        logger.info(
            f"📋 Trade #{self.trades_today}/{MAX_TRADES_PER_DAY} opened | "
            f"Daily P&L so far: ₹{self.daily_pnl:.0f}"
        )
        return pos

    def close_position(self, position: ScalpPosition):
        summary = position.get_summary()
        pnl     = summary["net_pnl"]

        self.daily_pnl    += pnl
        self.trade_history.append(summary)

        if pnl < 0:
            self.consecutive_loss += 1
            logger.warning(
                f"🔴 Loss #{self.consecutive_loss} consecutive | "
                f"Daily P&L: ₹{self.daily_pnl:.0f}"
            )
            if self.consecutive_loss >= CONSEC_LOSS_PAUSE:
                self.pause_until = datetime.now(IST) + timedelta(minutes=PAUSE_MINUTES)
                logger.warning(
                    f"⏸️  Pausing {PAUSE_MINUTES} min after "
                    f"{CONSEC_LOSS_PAUSE} consecutive losses"
                )
        else:
            self.consecutive_loss = 0
            logger.info(
                f"🟢 Win | consecutive loss streak reset | "
                f"Daily P&L: ₹{self.daily_pnl:.0f}"
            )

    # ─── DAILY REPORT ─────────────────────────────────────────────────────────

    def daily_report(self) -> str:
        wins   = [t for t in self.trade_history if t["net_pnl"] > 0]
        losses = [t for t in self.trade_history if t["net_pnl"] <= 0]
        wr     = len(wins) / len(self.trade_history) * 100 if self.trade_history else 0

        lines = [
            "═" * 54,
            "  SCALP STRATEGY — DAILY REPORT",
            "═" * 54,
            f"  Trades    : {self.trades_today}",
            f"  Wins      : {len(wins)}   Losses: {len(losses)}",
            f"  Win rate  : {wr:.1f}%",
            f"  Net P&L   : ₹{self.daily_pnl:.0f}",
            f"  Charges   : ₹{self.trades_today * CHARGES_PER_TRADE:.0f}",
            "─" * 54,
        ]
        for i, t in enumerate(self.trade_history, 1):
            icon = "✅" if t["net_pnl"] >= 0 else "🔴"
            lines.append(
                f"  {icon} #{i:2d} | {t['direction']:4s} | "
                f"{t['exit_reason']:18s} | "
                f"₹{t['entry_premium']:.1f}→₹{t['exit_premium']:.1f} | "
                f"Net ₹{t['net_pnl']:+.0f} | {t['hold_seconds']}s"
            )
        lines.append("═" * 54)
        return "\n".join(lines)
