"""
data.py — Market Data Module
Fetches and computes all data needed by the strategy:
  - Opening range (OH/OL from 9:15–9:45 candle)
  - ATR20 (for dynamic filters and targets)
  - VWAP (intraday, resets at 9:15 AM)
  - ADX (regime detection, previous day)
  - BankNifty direction (confirmation filter)
  - Live Nifty spot price
"""

import logging
import time
from datetime import datetime, date, timedelta
import pytz
IST = pytz.timezone('Asia/Kolkata')
from typing import Optional

import numpy as np
import pandas as pd
from growwapi import GrowwAPI
from utils import retry   # transient network protection

from config import (
    NIFTY_SYMBOL, BANKNIFTY_SYMBOL, EXCHANGE,
    SEGMENT_FNO, INDEX_SEGMENT,
    CANDLE_5MIN, CANDLE_30MIN,
    ATR_LOOKBACK, ADX_LOOKBACK,
    OPENING_RANGE_START, OPENING_RANGE_END,
)

logger = logging.getLogger(__name__)


class MarketData:
    """
    Fetches and caches all market data required by the strategy.
    Call fetch_opening_range() after 9:45 AM.
    Call get_live_nifty_price() in the monitoring loop.
    """

    def __init__(self, groww: GrowwAPI):
        self.groww = groww

        # Opening range (locked at 9:45 AM)
        self.opening_high: Optional[float] = None
        self.opening_low:  Optional[float] = None
        self.range_size:   Optional[float] = None

        # ATR20 of Nifty (computed from daily candles)
        self.atr20: Optional[float] = None

        # Breakout buffer (5% of ATR20)
        self.breakout_buffer: Optional[float] = None

        # Regime data
        self.adx_value: Optional[float] = None  # prev-day ADX
        self.prev_day_close: Optional[float] = None

        # Gap filter
        self.gap_pct: Optional[float] = None

        # BankNifty opening range
        self.bnf_opening_high: Optional[float] = None
        self.bnf_opening_low:  Optional[float] = None

        # FIX #11: initialise is_doji so _check_doji never raises AttributeError
        self.is_doji: bool = False

        # Intraday VWAP state
        self._vwap_cumulative_pv: float = 0.0
        self._vwap_cumulative_vol: float = 0.0
        self.vwap: Optional[float] = None
        self._last_vwap_candle_ts: Optional[str] = None  # FIX #20: dedup

        # 20 EMA of Nifty close (trend filter)
        self.ema20_above_count: int = 0  # how many of last 5 days Nifty closed above EMA20

    # ─── OPENING RANGE ────────────────────────────────────────────────────────

    def fetch_opening_range(self) -> bool:
        """
        Fetches the 30-min opening candle (9:15–9:45 AM) for Nifty.
        Must be called after 9:45 AM.
        Returns True if successful.
        """
        try:
            today = date.today().strftime("%Y-%m-%d")
            start_dt = f"{today} {OPENING_RANGE_START}:00"
            end_dt   = f"{today} {OPENING_RANGE_END}:00"

            @retry(max_attempts=3, base_delay=1.0)
            def _fetch_range():
                return self.groww.get_historical_candle_data(
                    trading_symbol=NIFTY_SYMBOL,
                    exchange=self.groww.EXCHANGE_NSE,
                    segment=self.groww.SEGMENT_CASH,
                    start_time=start_dt,
                    end_time=end_dt,
                    interval_in_minutes=CANDLE_30MIN
                )
            candle_data = _fetch_range()

            candles = candle_data.get("candles", [])
            if not candles:
                logger.error("❌ No opening range candle data received.")
                return False

            # candle format: [timestamp, open, high, low, close, volume]
            candle = candles[0]
            _, open_p, high_p, low_p, close_p, volume = candle

            self.opening_high = float(high_p)
            self.opening_low  = float(low_p)
            self.range_size   = round(self.opening_high - self.opening_low, 2)

            # Doji check: body must be > 30% of range
            body = abs(float(close_p) - float(open_p))
            self.is_doji = body < (self.range_size * 0.30)

            logger.info(
                f"📊 Opening Range: High={self.opening_high}, Low={self.opening_low}, "
                f"Range={self.range_size} pts | Doji={self.is_doji}"
            )

            # Also fetch BankNifty opening range
            self._fetch_banknifty_opening_range(today, start_dt, end_dt)

            return True

        except Exception as e:
            logger.error(f"❌ Failed to fetch opening range: {e}")
            return False

    def _fetch_banknifty_opening_range(self, today, start_dt, end_dt):
        """Fetches BankNifty opening range for confirmation filter."""
        try:
            bnf_data = self.groww.get_historical_candle_data(
                trading_symbol=BANKNIFTY_SYMBOL,
                exchange=self.groww.EXCHANGE_NSE,
                segment=self.groww.SEGMENT_CASH,
                start_time=start_dt,
                end_time=end_dt,
                interval_in_minutes=CANDLE_30MIN
            )
            candles = bnf_data.get("candles", [])
            if candles:
                _, _, high_p, low_p, _, _ = candles[0]
                self.bnf_opening_high = float(high_p)
                self.bnf_opening_low  = float(low_p)
                logger.info(f"📊 BNF Opening Range: High={self.bnf_opening_high}, Low={self.bnf_opening_low}")
        except Exception as e:
            logger.warning(f"Could not fetch BankNifty opening range: {e}")

    # ─── ATR20 ────────────────────────────────────────────────────────────────

    def fetch_atr20(self) -> bool:
        """
        Fetches last 25 daily candles of Nifty and computes ATR20.
        Call this at agent startup (8:50 AM) before market open.
        """
        try:
            end_date   = date.today()
            start_date = end_date - timedelta(days=40)  # extra buffer for weekends/holidays

            candle_data = self.groww.get_historical_candle_data(
                trading_symbol=NIFTY_SYMBOL,
                exchange=self.groww.EXCHANGE_NSE,
                segment=self.groww.SEGMENT_CASH,
                start_time=start_date.strftime("%Y-%m-%d 09:15:00"),
                end_time=end_date.strftime("%Y-%m-%d 15:30:00"),
                interval_in_minutes=375  # daily candle (375 min = 6.25 hours)
            )

            candles = candle_data.get("candles", [])
            if len(candles) < ATR_LOOKBACK + 1:
                logger.error(f"❌ Not enough daily candles for ATR20 ({len(candles)} received)")
                return False

            df = pd.DataFrame(candles, columns=["ts", "open", "high", "low", "close", "volume"])
            df = df.astype({"open": float, "high": float, "low": float, "close": float})
            df = df.tail(ATR_LOOKBACK + 5)  # keep only what we need

            # True Range
            df["prev_close"] = df["close"].shift(1)
            df["tr"] = df.apply(
                lambda r: max(
                    r["high"] - r["low"],
                    abs(r["high"] - r["prev_close"]),
                    abs(r["low"]  - r["prev_close"])
                ),
                axis=1
            )

            self.atr20          = round(df["tr"].rolling(ATR_LOOKBACK).mean().iloc[-1], 2)
            self.breakout_buffer = round(self.atr20 * 0.05, 2)  # 5% of ATR20
            self.prev_day_close  = float(df["close"].iloc[-1])

            logger.info(f"📈 ATR20={self.atr20}, Breakout buffer={self.breakout_buffer}")

            # Compute ADX from daily candles
            self._compute_adx(df)

            # Compute EMA20 trend filter
            self._compute_ema20_trend(df)

            # Compute gap
            self._compute_gap(df)

            return True

        except Exception as e:
            logger.error(f"❌ Failed to fetch ATR20: {e}")
            return False

    # ─── ADX ──────────────────────────────────────────────────────────────────

    def _compute_adx(self, df: pd.DataFrame):
        """Computes ADX14 from daily OHLC data."""
        try:
            df = df.copy()
            df["prev_high"]  = df["high"].shift(1)
            df["prev_low"]   = df["low"].shift(1)
            df["prev_close"] = df["close"].shift(1)

            df["+DM"] = df.apply(lambda r: max(r["high"] - r["prev_high"], 0)
                                  if r["high"] - r["prev_high"] > r["prev_low"] - r["low"] else 0, axis=1)
            df["-DM"] = df.apply(lambda r: max(r["prev_low"] - r["low"], 0)
                                  if r["prev_low"] - r["low"] > r["high"] - r["prev_high"] else 0, axis=1)

            period = ADX_LOOKBACK
            # FIX #19: Wilder's smoothing = EMA with alpha=1/period (not SMA)
            alpha = 1.0 / period
            df["ATR_adx"] = df["tr"].ewm(alpha=alpha, adjust=False).mean()
            df["+DI"]  = 100 * (df["+DM"].ewm(alpha=alpha, adjust=False).mean() / df["ATR_adx"])
            df["-DI"]  = 100 * (df["-DM"].ewm(alpha=alpha, adjust=False).mean() / df["ATR_adx"])
            df["DX"]   = 100 * abs(df["+DI"] - df["-DI"]) / (df["+DI"] + df["-DI"])
            df["ADX"]  = df["DX"].ewm(alpha=alpha, adjust=False).mean()

            self.adx_value = round(df["ADX"].iloc[-2], 2)  # previous day's ADX
            logger.info(f"📊 Previous day ADX={self.adx_value}")

        except Exception as e:
            logger.warning(f"Could not compute ADX: {e}")
            self.adx_value = None

    # ─── EMA20 TREND FILTER ───────────────────────────────────────────────────

    def _compute_ema20_trend(self, df: pd.DataFrame):
        """
        Checks how many of the last 5 sessions closed above 20-day EMA.
        Used to determine if we should only take calls or only puts.
        """
        try:
            df = df.copy()
            df["ema20"] = df["close"].ewm(span=20, adjust=False).mean()
            last5 = df.tail(5)
            self.ema20_above_count = int((last5["close"] > last5["ema20"]).sum())
            logger.info(f"📈 Nifty closed above EMA20 in {self.ema20_above_count}/5 recent sessions")
        except Exception as e:
            logger.warning(f"Could not compute EMA20 trend: {e}")
            self.ema20_above_count = 3  # neutral fallback

    # ─── GAP ──────────────────────────────────────────────────────────────────

    def _compute_gap(self, df: pd.DataFrame):
        """Computes today's gap from yesterday's close."""
        try:
            prev_close = float(df["close"].iloc[-1])
            # We'll compute actual gap once market opens using live price
            self._prev_close_for_gap = prev_close
            logger.info(f"📊 Previous close for gap calc: {prev_close}")
        except Exception as e:
            logger.warning(f"Could not compute gap: {e}")

    def compute_gap_from_open(self, open_price: float) -> float:
        """Call this after market opens with the actual open price."""
        try:
            prev = getattr(self, "_prev_close_for_gap", None)
            if prev:
                self.gap_pct = abs(open_price - prev) / prev * 100
                logger.info(f"📊 Gap from prev close: {self.gap_pct:.2f}%")
                return self.gap_pct
        except Exception as e:
            logger.warning(f"Could not compute gap from open: {e}")
        return 0.0

    # ─── LIVE PRICE ───────────────────────────────────────────────────────────

    def get_live_nifty_price(self) -> Optional[float]:
        """Fetches live Nifty spot price."""
        try:
            @retry(max_attempts=2, base_delay=0.5)
            def _ltp(): return self.groww.get_ltp(
                trading_symbol=NIFTY_SYMBOL,
                exchange=self.groww.EXCHANGE_NSE,
                segment=self.groww.SEGMENT_CASH
            )
            return float(_ltp().get("ltp", 0))
        except Exception as e:
            logger.error(f"❌ Failed to get live Nifty price: {e}")
            return None

    def get_live_banknifty_price(self) -> Optional[float]:
        """Fetches live BankNifty spot price."""
        try:
            @retry(max_attempts=2, base_delay=0.5)
            def _bnf(): return self.groww.get_ltp(
                trading_symbol=BANKNIFTY_SYMBOL,
                exchange=self.groww.EXCHANGE_NSE,
                segment=self.groww.SEGMENT_CASH
            )
            return float(_bnf().get("ltp", 0))
        except Exception as e:
            logger.error(f"❌ Failed to get live BankNifty price: {e}")
            return None

    # ─── VWAP (INTRADAY) ──────────────────────────────────────────────────────

    def update_vwap(self, high: float, low: float, close: float,
                    volume: float, candle_ts: Optional[str] = None):
        """
        FIX #20: Accepts candle_ts to skip duplicate candles.
        VWAP resets to 0 at start of each day (call reset_vwap() at 9:15 AM).
        """
        if candle_ts and candle_ts == self._last_vwap_candle_ts:
            return  # same candle fetched again — skip to prevent double-count
        self._last_vwap_candle_ts = candle_ts
        typical_price = (high + low + close) / 3.0
        self._vwap_cumulative_pv  += typical_price * volume
        self._vwap_cumulative_vol += volume
        if self._vwap_cumulative_vol > 0:
            self.vwap = round(self._vwap_cumulative_pv / self._vwap_cumulative_vol, 2)

    def reset_vwap(self):
        """Reset VWAP at market open (9:15 AM)."""
        self._vwap_cumulative_pv   = 0.0
        self._vwap_cumulative_vol  = 0.0
        self.vwap                  = None
        self._last_vwap_candle_ts  = None  # FIX #20

    # ─── 5-MIN CANDLE FETCH (for breakout and trailing) ───────────────────────

    def get_recent_5min_candles(self, n: int = 10) -> Optional[pd.DataFrame]:
        """
        Fetches the last N × 5-min candles of Nifty.
        Used for breakout close confirmation and trailing stop calculation.
        """
        try:
            now   = datetime.now(IST)
            start = now - timedelta(minutes=n * CANDLE_5MIN + 15)

            @retry(max_attempts=2, base_delay=1.0)
            def _fetch5():
                return self.groww.get_historical_candle_data(
                    trading_symbol=NIFTY_SYMBOL,
                    exchange=self.groww.EXCHANGE_NSE,
                    segment=self.groww.SEGMENT_CASH,
                    start_time=start.strftime("%Y-%m-%d %H:%M:%S"),
                    end_time=now.strftime("%Y-%m-%d %H:%M:%S"),
                    interval_in_minutes=CANDLE_5MIN
                )
            candle_data = _fetch5()

            candles = candle_data.get("candles", [])
            if not candles:
                return None

            df = pd.DataFrame(candles, columns=["ts", "open", "high", "low", "close", "volume"])
            df = df.astype({"open": float, "high": float, "low": float, "close": float, "volume": float})
            df["datetime"] = pd.to_datetime(df["ts"], unit="s")
            df = df.set_index("datetime").sort_index()
            return df.tail(n)

        except Exception as e:
            logger.error(f"❌ Failed to fetch 5-min candles: {e}")
            return None

    # ─── OPTION LTP ───────────────────────────────────────────────────────────

    def get_option_ltp(self, trading_symbol: str) -> Optional[float]:
        """Fetches live LTP for a specific option trading symbol."""
        try:
            @retry(max_attempts=2, base_delay=0.5)
            def _opt(): return self.groww.get_ltp(
                trading_symbol=trading_symbol,
                exchange=self.groww.EXCHANGE_NSE,
                segment=self.groww.SEGMENT_FNO
            )
            return float(_opt().get("ltp", 0))
        except Exception as e:
            logger.error(f"❌ Failed to get option LTP for {trading_symbol}: {e}")
            return None
