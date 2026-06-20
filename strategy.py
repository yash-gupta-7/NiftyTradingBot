"""
strategy.py — Strategy V2.0 Signal Engine
Implements all pre-market filters, opening range validation,
and breakout detection with VWAP + BankNifty + trend confirmation.
"""

import logging
from datetime import datetime
import pytz
IST = pytz.timezone('Asia/Kolkata')
from typing import Optional, Tuple
from enum import Enum

from data import MarketData
from config import (
    BNF_BREAKOUT_BUFFER_PTS,
    OPENING_RANGE_END, ENTRY_WINDOW_START, ENTRY_WINDOW_END,
    MIN_RANGE_ATR_MULTIPLE, MAX_RANGE_ATR_MULTIPLE,
    MAX_GAP_PCT, VIX_MIN, VIX_MAX, IV_RANK_MAX,
    ADX_MIN, VOLUME_MULTIPLIER, BREAKOUT_BUFFER_ATR_PCT,
    SKIP_DATES,
)

logger = logging.getLogger(__name__)


class Signal(Enum):
    NONE  = "NONE"
    BUY_CALL = "BUY_CALL"   # Bullish breakout
    BUY_PUT  = "BUY_PUT"    # Bearish breakout
    SKIP  = "SKIP"          # Filters failed — no trade today


class SkipReason(Enum):
    CALENDAR_DAY    = "Calendar skip (expiry/event/holiday)"
    RANGE_TOO_SMALL = "Opening range too small"
    RANGE_TOO_LARGE = "Opening range too large"
    DOJI_CANDLE     = "Opening candle is a Doji"
    GAP_TOO_LARGE   = "Gap from previous close > 0.5%"
    VIX_OUT_OF_RANGE = "VIX outside 11–20"
    IV_RANK_HIGH    = "IV Rank > 60%"
    ADX_TOO_LOW     = "ADX < 15 (choppy market)"
    NO_BREAKOUT     = "No valid breakout by 10:30 AM"
    FALSE_BREAKOUT  = "Breakout reversed within 2 candles"
    VWAP_MISMATCH   = "Breakout direction conflicts with VWAP"
    TREND_MISMATCH  = "Breakout conflicts with 5-day trend"
    BNF_MISMATCH    = "BankNifty not confirming breakout"
    VOLUME_WEAK     = "Volume below 2× average"
    RISK_CIRCUIT    = "Risk circuit breaker active"


class StrategyEngine:
    """
    Core strategy logic for the ORB V2.0 agent.

    Usage:
        engine = StrategyEngine(market_data, vix, iv_rank)
        skip_reason = engine.run_premarket_checks()
        if not skip_reason:
            signal, skip_reason = engine.check_breakout(current_price, volume, candles_df)
    """

    def __init__(self, market_data: MarketData):
        self.md = market_data
        self.signal: Signal = Signal.NONE
        self.skip_reason: Optional[SkipReason] = None
        self.breakout_price: Optional[float] = None
        self.breakout_time: Optional[datetime] = None

        # Set externally before running checks
        self.vix: Optional[float] = None
        self.iv_rank: Optional[float] = None  # 0–100
        self.risk_circuit_active: bool = False

        # Breakout tracking
        self._breakout_confirmed: bool = False
        self._false_breakout_candles: int = 0

    # ─── PRE-MARKET CHECKS (run at 8:50 AM) ───────────────────────────────────

    def run_premarket_checks(self) -> Optional[SkipReason]:
        """
        Runs all pre-market filters.
        Returns None if all pass (trade allowed today).
        Returns SkipReason if any filter fails.
        """
        checks = [
            self._check_calendar_day,
            self._check_risk_circuit,
            self._check_vix,
            self._check_adx,
        ]

        for check in checks:
            reason = check()
            if reason:
                self.skip_reason = reason
                logger.info(f"🚫 Pre-market SKIP: {reason.value}")
                return reason

        logger.info("✅ All pre-market checks passed.")
        return None

    # ─── OPENING RANGE CHECKS (run at 9:45 AM) ────────────────────────────────

    def run_opening_range_checks(self, open_price: float) -> Optional[SkipReason]:
        """
        Validates the opening range after it is locked at 9:45 AM.
        Returns None if range is valid.
        """
        # FIX #10: removed unused `checks` list; use direct iteration
        self.md.compute_gap_from_open(open_price)

        for check_fn in [self._check_range_size, self._check_gap, self._check_doji]:
            reason = check_fn()
            if reason:
                self.skip_reason = reason
                logger.info(f"🚫 Opening range SKIP: {reason.value}")
                return reason

        logger.info(
            f"✅ Opening range valid: {self.md.opening_high} / {self.md.opening_low} "
            f"= {self.md.range_size} pts (ATR20={self.md.atr20})"
        )
        return None

    # ─── BREAKOUT DETECTION (call in monitoring loop) ─────────────────────────

    def check_breakout(
        self,
        current_price: float,
        candles_df,  # recent 5-min candles DataFrame
        breakout_candle_volume: float,
        avg_volume_for_slot: float,
        banknifty_price: Optional[float] = None
    ) -> Tuple[Signal, Optional[SkipReason]]:
        """
        Core breakout detection with all V2 filters.
        Call this every time a new 5-min candle closes.

        Returns (Signal, SkipReason).
        If Signal is BUY_CALL/BUY_PUT, trade is valid.
        If Signal is SKIP, reason explains why.
        """
        now = datetime.now(IST)

        # Hard time cutoff
        if now.strftime("%H:%M") >= ENTRY_WINDOW_END:
            reason = SkipReason.NO_BREAKOUT
            self.skip_reason = reason
            logger.info(f"⏰ Past entry window ({ENTRY_WINDOW_END}). No trade today.")
            return Signal.SKIP, reason

        if candles_df is None or candles_df.empty:
            return Signal.NONE, None

        latest_candle = candles_df.iloc[-1]
        candle_close = float(latest_candle["close"])
        candle_high  = float(latest_candle["high"])
        candle_low   = float(latest_candle["low"])
        buffer = self.md.breakout_buffer or 5.0

        # ── Check bullish breakout ─────────────────────────────────────────────
        if candle_close > self.md.opening_high + buffer:
            logger.info(f"📈 Potential BULLISH breakout: close={candle_close} > OH+buffer={self.md.opening_high + buffer:.1f}")
            reason = self._validate_breakout(
                direction="UP",
                candles_df=candles_df,
                candle_volume=breakout_candle_volume,
                avg_volume=avg_volume_for_slot,
                banknifty_price=banknifty_price
            )
            if reason is None:
                self.signal = Signal.BUY_CALL
                self.breakout_price = candle_close
                self.breakout_time  = now
                logger.info(f"✅ CONFIRMED: BUY CALL at {self.breakout_price}")
                return Signal.BUY_CALL, None
            else:
                return Signal.SKIP, reason

        # ── Check bearish breakout ─────────────────────────────────────────────
        if candle_close < self.md.opening_low - buffer:
            logger.info(f"📉 Potential BEARISH breakout: close={candle_close} < OL-buffer={self.md.opening_low - buffer:.1f}")
            reason = self._validate_breakout(
                direction="DOWN",
                candles_df=candles_df,
                candle_volume=breakout_candle_volume,
                avg_volume=avg_volume_for_slot,
                banknifty_price=banknifty_price
            )
            if reason is None:
                self.signal = Signal.BUY_PUT
                self.breakout_price = candle_close
                self.breakout_time  = now
                logger.info(f"✅ CONFIRMED: BUY PUT at {self.breakout_price}")
                return Signal.BUY_PUT, None
            else:
                return Signal.SKIP, reason

        # No breakout yet
        return Signal.NONE, None

    # ─── BREAKOUT VALIDATION (all V2 filters) ─────────────────────────────────

    def _validate_breakout(
        self,
        direction: str,
        candles_df,
        candle_volume: float,
        avg_volume: float,
        banknifty_price: Optional[float]
    ) -> Optional[SkipReason]:
        """
        Runs all 4 confirmation filters on a detected breakout.
        Returns None if all pass, SkipReason if any fail.
        """

        # 1. Volume filter
        if avg_volume > 0 and candle_volume < avg_volume * VOLUME_MULTIPLIER:
            logger.info(f"🔇 Volume too weak: {candle_volume:.0f} < {avg_volume * VOLUME_MULTIPLIER:.0f}")
            return SkipReason.VOLUME_WEAK

        # 2. VWAP alignment
        vwap_reason = self._check_vwap_alignment(direction)
        if vwap_reason:
            return vwap_reason

        # 3. 5-day trend alignment
        trend_reason = self._check_trend_alignment(direction)
        if trend_reason:
            return trend_reason

        # 4. BankNifty confirmation
        bnf_reason = self._check_banknifty_confirmation(direction, banknifty_price)
        if bnf_reason:
            return bnf_reason

        return None  # All filters passed ✅

    def _check_vwap_alignment(self, direction: str) -> Optional[SkipReason]:
        """
        For BUY_CALL: spot must be ABOVE VWAP.
        For BUY_PUT:  spot must be BELOW VWAP.
        """
        # FIX 4: fail-closed
        if self.md.vwap is None:
            logger.warning("VWAP unavailable — blocking entry (fail-closed)")
            return SkipReason.VWAP_MISMATCH   # conservative skip

        live_price = self.md.get_live_nifty_price()
        if live_price is None:
            return None

        if direction == "UP" and live_price < self.md.vwap:
            logger.info(f"📊 VWAP mismatch: price={live_price} < VWAP={self.md.vwap} for bullish trade")
            return SkipReason.VWAP_MISMATCH
        if direction == "DOWN" and live_price > self.md.vwap:
            logger.info(f"📊 VWAP mismatch: price={live_price} > VWAP={self.md.vwap} for bearish trade")
            return SkipReason.VWAP_MISMATCH

        logger.info(f"✅ VWAP OK: price={live_price}, VWAP={self.md.vwap}, direction={direction}")
        return None

    def _check_trend_alignment(self, direction: str) -> Optional[SkipReason]:
        """
        Nifty closed above EMA20 in ≥3 of last 5 days → only BUY_CALL.
        Nifty closed below EMA20 in ≥3 of last 5 days → only BUY_PUT.
        If mixed (2–3 days each way) → allow both directions.
        """
        above_count = self.md.ema20_above_count

        if direction == "UP" and above_count < 2:
            logger.info(f"📉 Trend mismatch: only {above_count}/5 sessions above EMA20, but direction is UP")
            return SkipReason.TREND_MISMATCH

        if direction == "DOWN" and above_count > 3:
            logger.info(f"📈 Trend mismatch: {above_count}/5 sessions above EMA20, but direction is DOWN")
            return SkipReason.TREND_MISMATCH

        logger.info(f"✅ Trend aligned: EMA20 above count={above_count}/5, direction={direction}")
        return None

    def _check_banknifty_confirmation(self, direction: str, bnf_price: Optional[float]) -> Optional[SkipReason]:
        """
        BankNifty must also be breaking its opening range in the same direction.
        """
        if bnf_price is None or self.md.bnf_opening_high is None:
            logger.warning("BankNifty data unavailable — skipping BNF confirmation filter")
            return None

        bnf_buffer = BNF_BREAKOUT_BUFFER_PTS  # FIX #17: from config

        if direction == "UP":
            if bnf_price <= self.md.bnf_opening_high + bnf_buffer:
                logger.info(f"🏦 BNF not confirming: price={bnf_price} <= BNF_OH={self.md.bnf_opening_high}")
                return SkipReason.BNF_MISMATCH
        else:
            if bnf_price >= self.md.bnf_opening_low - bnf_buffer:
                logger.info(f"🏦 BNF not confirming: price={bnf_price} >= BNF_OL={self.md.bnf_opening_low}")
                return SkipReason.BNF_MISMATCH

        logger.info(f"✅ BankNifty confirming direction={direction}")
        return None

    # ─── INDIVIDUAL PRE-MARKET CHECKS ─────────────────────────────────────────

    def _check_calendar_day(self) -> Optional[SkipReason]:
        """
        FIX 6: Uses EventCalendar (auto-checking + staleness warnings)
        instead of relying solely on a hardcoded list.
        """
        from event_calendar import EventCalendar
        cal = EventCalendar()
        cal.check_staleness()   # logs warning if calendar needs refresh

        should_skip, reason = cal.is_skip_day()
        if should_skip:
            logger.info(f"📅 SKIP: {reason}")
            return SkipReason.CALENDAR_DAY

        return None

    def _check_vix(self) -> Optional[SkipReason]:
        # FIX 4: fail-closed — missing data blocks the trade
        if self.vix is None:
            logger.warning("VIX unavailable — blocking trade (fail-closed)")
            return SkipReason.VIX_OUT_OF_RANGE   # conservative skip
        if self.vix < VIX_MIN or self.vix > VIX_MAX:
            logger.info(f"⚡ VIX={self.vix} outside range [{VIX_MIN}, {VIX_MAX}] — SKIP")
            return SkipReason.VIX_OUT_OF_RANGE
        if self.iv_rank and self.iv_rank > IV_RANK_MAX:
            logger.info(f"⚡ IV Rank={self.iv_rank} > {IV_RANK_MAX}% — SKIP")
            return SkipReason.IV_RANK_HIGH
        return None

    def _check_adx(self) -> Optional[SkipReason]:
        # FIX 4: fail-closed
        if self.md.adx_value is None:
            logger.warning("ADX unavailable — blocking trade (fail-closed)")
            return SkipReason.ADX_TOO_LOW   # conservative skip
        if self.md.adx_value < ADX_MIN:
            logger.info(f"📊 ADX={self.md.adx_value} < {ADX_MIN} (choppy market) — SKIP")
            return SkipReason.ADX_TOO_LOW
        return None

    def _check_risk_circuit(self) -> Optional[SkipReason]:
        if self.risk_circuit_active:
            logger.info("🛑 Risk circuit breaker is active — SKIP")
            return SkipReason.RISK_CIRCUIT
        return None

    def _check_range_size(self) -> Optional[SkipReason]:
        # FIX 4: fail-closed
        if self.md.range_size is None or self.md.atr20 is None:
            logger.warning("Range/ATR unavailable — blocking trade (fail-closed)")
            return SkipReason.RANGE_TOO_SMALL
        min_range = self.md.atr20 * MIN_RANGE_ATR_MULTIPLE
        max_range = self.md.atr20 * MAX_RANGE_ATR_MULTIPLE
        if self.md.range_size < min_range:
            logger.info(f"📏 Range {self.md.range_size} < min {min_range:.1f} (0.35× ATR20) — SKIP")
            return SkipReason.RANGE_TOO_SMALL
        if self.md.range_size > max_range:
            logger.info(f"📏 Range {self.md.range_size} > max {max_range:.1f} (1.4× ATR20) — SKIP")
            return SkipReason.RANGE_TOO_LARGE
        return None

    def _check_gap(self) -> Optional[SkipReason]:
        # FIX 4: fail-closed
        if self.md.gap_pct is None:
            logger.warning("Gap data unavailable — blocking trade (fail-closed)")
            return SkipReason.GAP_TOO_LARGE
        if self.md.gap_pct > MAX_GAP_PCT:
            logger.info(f"📊 Gap {self.md.gap_pct:.2f}% > {MAX_GAP_PCT}% — SKIP")
            return SkipReason.GAP_TOO_LARGE
        return None

    def _check_doji(self) -> Optional[SkipReason]:
        if getattr(self.md, "is_doji", False):
            logger.info("📊 Opening candle is a Doji — SKIP")
            return SkipReason.DOJI_CANDLE
        return None
