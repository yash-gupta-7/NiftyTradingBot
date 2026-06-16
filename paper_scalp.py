"""
paper_scalp.py — Live Paper Trading Runner for Scalp Strategy
══════════════════════════════════════════════════════════════
Runs the scalp strategy against live Groww market data.
No real orders placed. Every signal is logged as if executed.

Usage:
    python paper_scalp.py               # run today's session
    python paper_scalp.py --replay      # replay from log file

Architecture:
    FAST thread  (1.5s): fetch live option LTP + Nifty spot
                         → feed into ScalpPosition.monitor_fast()
    SLOW loop    (60s):  fetch new 1-min Nifty candle when closed
                         → feed into IndicatorEngine + EntryDetector
                         → feed into ScalpPosition.monitor_slow()
"""

import argparse
import csv
import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, date
from typing import Optional

import pytz
import pandas as pd

from strategy_scalp import (
    IndicatorEngine, EntryDetector, ScalpPosition,
    ScalpDayController, ScalpSignal, Candle,
    LOT_SIZE, CHARGES_PER_TRADE,
    TRADE_START, TRADE_END,
)
from volume_cache import VolumeCache
from utils import retry, simulate_slippage, Heartbeat
from alerts import (alert_startup, alert_trade_entered,
                    alert_trade_closed, alert_daily_report)

logger = logging.getLogger(__name__)
IST    = pytz.timezone("Asia/Kolkata")

# ─── PAPER TRADE LOG ──────────────────────────────────────────────────────────

PAPER_LOG_DIR  = "logs/paper_scalp"
os.makedirs(PAPER_LOG_DIR, exist_ok=True)

def _paper_log_file() -> str:
    return os.path.join(PAPER_LOG_DIR,
                        f"scalp_{date.today().strftime('%Y%m%d')}.csv")

def _ensure_log_header():
    fpath = _paper_log_file()
    if not os.path.exists(fpath):
        with open(fpath, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=[
                "time", "trade_no", "direction", "strike",
                "entry_premium", "exit_premium", "exit_reason",
                "gain_per_unit", "gross_pnl", "net_pnl",
                "hold_seconds", "trail_width", "score",
                "entry_level", "vwap_at_entry", "peak_premium",
            ])
            writer.writeheader()

def _log_trade(trade_no: int, signal: ScalpSignal,
               summary: dict, strike: str):
    fpath = _paper_log_file()
    gain  = summary["exit_premium"] - summary["entry_premium"]
    row   = {
        "time":           datetime.now(IST).strftime("%H:%M:%S"),
        "trade_no":       trade_no,
        "direction":      summary["direction"],
        "strike":         strike,
        "entry_premium":  summary["entry_premium"],
        "exit_premium":   summary["exit_premium"],
        "exit_reason":    summary["exit_reason"],
        "gain_per_unit":  round(gain, 2),
        "gross_pnl":      round(gain * LOT_SIZE, 2),
        "net_pnl":        summary["net_pnl"],
        "hold_seconds":   summary["hold_seconds"],
        "trail_width":    summary["trail_width"] or 0,
        "score":          "",
        "entry_level":    summary["entry_level"],
        "vwap_at_entry":  signal.vwap,
        "peak_premium":   summary["peak_premium"],
    }
    with open(fpath, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=row.keys())
        writer.writerow(row)
    logger.info(f"📝 Trade logged → {fpath}")


# ─── SIMULATED OPTION LTP (paper mode — uses real market data, no orders) ─────

class PaperOptionTracker:
    """
    In paper mode we don't own a real position.
    We track the live option LTP as if we had bought at entry_premium.
    Slippage is simulated at entry only.
    """

    def __init__(self, groww, option_symbol: str, entry_premium: float):
        self.groww          = groww
        self.option_symbol  = option_symbol
        # Simulate realistic entry slippage (buy at ASK, not MID)
        self.entry_premium  = simulate_slippage(entry_premium, "BUY", pct=0.015)
        self.current_ltp    = entry_premium

    @retry(max_attempts=2, base_delay=0.5)
    def fetch_ltp(self) -> Optional[float]:
        try:
            resp = self.groww.get_ltp(
                trading_symbol=self.option_symbol,
                exchange=self.groww.EXCHANGE_NSE,
                segment=self.groww.SEGMENT_FNO,
            )
            ltp = float(resp.get("ltp", 0))
            if ltp > 0:
                self.current_ltp = ltp
                return ltp
        except Exception as e:
            logger.debug(f"LTP fetch: {e}")
        return None


# ─── STRIKE SELECTOR (paper mode) ─────────────────────────────────────────────

def get_atm_option_symbol(groww, direction: str, spot: float) -> tuple:
    """
    Returns (symbol, ltp) for the ATM weekly option.
    direction: "CALL" → CE, "PUT" → PE
    """
    from datetime import timedelta
    today     = date.today()
    days_thu  = (3 - today.weekday()) % 7 or 7
    expiry    = (today + timedelta(days=days_thu)).strftime("%Y-%m-%d")

    # Round to nearest 50 for ATM
    atm_strike = int(round(spot / 50) * 50)
    opt_type   = "CE" if direction == "CALL" else "PE"

    try:
        chain = groww.get_option_chain(
            exchange=groww.EXCHANGE_NSE,
            underlying="NIFTY",
            expiry_date=expiry,
        )
        for item in chain.get("data", []):
            if (int(item.get("strike_price", 0)) == atm_strike and
                    item.get("option_type", "").upper() == opt_type):
                sym = item.get("trading_symbol", "")
                ltp = float(item.get("ltp") or 0)
                if sym and ltp > 0:
                    logger.info(f"🎯 ATM option: {sym} @ ₹{ltp:.2f}")
                    return sym, ltp
    except Exception as e:
        logger.error(f"Option chain fetch failed: {e}")

    # Fallback manual symbol
    exp_date = date.today() + timedelta(days=days_thu)
    sym = f"NIFTY{exp_date.strftime('%y%b').upper()}{atm_strike}{opt_type}"
    logger.warning(f"Using manual symbol: {sym}")
    return sym, 0.0


# ─── NIFTY 1-MIN CANDLE FETCHER ───────────────────────────────────────────────

class CandleFetcher:
    """
    Fetches the latest completed 1-min Nifty candle.
    Tracks which candle was last processed to avoid duplicates.
    """

    def __init__(self, groww):
        self.groww           = groww
        self._last_candle_t  = None

    @retry(max_attempts=3, base_delay=1.0)
    def get_latest_candle(self) -> Optional[Candle]:
        from datetime import timedelta as td
        now   = datetime.now(IST)
        start = now - td(minutes=15)
        try:
            resp = self.groww.get_historical_candle_data(
                trading_symbol="NIFTY",
                exchange=self.groww.EXCHANGE_NSE,
                segment=self.groww.SEGMENT_CASH,
                start_time=start.strftime("%Y-%m-%d %H:%M:%S"),
                end_time=now.strftime("%Y-%m-%d %H:%M:%S"),
                interval_in_minutes=1,
            )
            candles = resp.get("candles", [])
            if not candles:
                return None

            # Last complete candle (not the current open one)
            ts, o, h, l, c, vol = candles[-2] if len(candles) >= 2 else candles[-1]
            ts_int = int(ts)
            if ts_int > 1_000_000_000_000:
                ts_int //= 1000
            dt   = datetime.fromtimestamp(ts_int, tz=IST)
            tstr = dt.strftime("%H:%M")

            if tstr == self._last_candle_t:
                return None   # already processed this candle

            self._last_candle_t = tstr
            return Candle(
                time=tstr,
                open=float(o), high=float(h),
                low=float(l),  close=float(c),
                volume=float(vol),
            )
        except Exception as e:
            logger.error(f"Candle fetch error: {e}")
            return None

    @retry(max_attempts=2, base_delay=0.5)
    def get_live_nifty_price(self) -> Optional[float]:
        try:
            resp = self.groww.get_ltp(
                trading_symbol="NIFTY",
                exchange=self.groww.EXCHANGE_NSE,
                segment=self.groww.SEGMENT_CASH,
            )
            return float(resp.get("ltp", 0)) or None
        except Exception:
            return None


# ─── MAIN PAPER RUNNER ────────────────────────────────────────────────────────

class PaperScalpRunner:
    """
    Runs the full paper trading session for the scalp strategy.
    FAST thread: 1.5s monitoring loop.
    SLOW loop:   1-min candle processing (main thread).
    """

    def __init__(self, groww):
        self.groww      = groww
        self.indicators = IndicatorEngine()
        self.detector   = EntryDetector(self.indicators)
        self.controller = ScalpDayController()
        self.fetcher    = CandleFetcher(groww)
        self.vol_cache  = VolumeCache(groww)

        self._active_position: Optional[ScalpPosition]     = None
        self._active_tracker:  Optional[PaperOptionTracker] = None
        self._active_signal:   Optional[ScalpSignal]        = None
        self._active_strike:   str                          = ""

        self._lock    = threading.Lock()
        self._running = False
        self._fast_thread: Optional[threading.Thread] = None

    # ─── STARTUP ──────────────────────────────────────────────────────────────

    def startup(self) -> bool:
        logger.info("=" * 54)
        logger.info("  SCALP PAPER TRADER — STARTING")
        logger.info(f"  {date.today().strftime('%A, %d %b %Y')}")
        logger.info("=" * 54)

        _ensure_log_header()

        logger.info("📊 Building volume cache (1-min slots)...")
        if not self.vol_cache.build():
            logger.warning("⚠️  Volume cache failed — volume filter disabled today")

        self.indicators.set_slot_averages(self.vol_cache._cache)
        self.indicators.reset_day()

        alert_startup("SCALP (paper)", LOT_SIZE * 100, paper_mode=True)
        return True

    # ─── FAST LOOP ────────────────────────────────────────────────────────────

    def _fast_loop(self):
        while self._running:
            try:
                self._fast_cycle()
            except Exception as e:
                logger.warning(f"Fast loop error: {e}")
            time.sleep(1.5)

    def _fast_cycle(self):
        with self._lock:
            pos     = self._active_position
            tracker = self._active_tracker
            signal  = self._active_signal
            strike  = self._active_strike

        if pos is None or tracker is None:
            return

        opt_ltp    = tracker.fetch_ltp()
        nifty_spot = self.fetcher.get_live_nifty_price()

        if opt_ltp is None or nifty_spot is None:
            return

        # Get current volume for momentum gate
        now_slot = datetime.now(IST).strftime("%H:%M")
        avg_vol  = self.indicators.get_slot_avg_volume(now_slot)
        # We don't have live volume per second — use last known candle volume
        # Fast loop uses last fetched candle volume as proxy
        cur_vol  = getattr(self, "_last_candle_volume", avg_vol)

        result = pos.monitor_fast(
            option_ltp  = opt_ltp,
            nifty_price = nifty_spot,
            current_vol = cur_vol,
            avg_vol     = avg_vol,
        )

        if result and result.startswith("EXIT:"):
            reason = result[5:]
            logger.info(f"⚡ Fast exit: {reason} @ ₹{opt_ltp:.2f}")
            self._close_position(pos, signal, strike, opt_ltp)

    # ─── SLOW LOOP (main thread) ───────────────────────────────────────────────

    def run(self):
        self._running = True
        self._fast_thread = threading.Thread(
            target=self._fast_loop, name="ScalpFast", daemon=True
        )
        self._fast_thread.start()
        logger.info("⚡ Fast monitor started (1.5s)")

        while True:
            now_str = datetime.now(IST).strftime("%H:%M")

            if now_str >= TRADE_END:
                logger.info("⏰ Market closing — ending session")
                self._force_close_any_open()
                break

            Heartbeat.pulse()
            self._slow_cycle()

            # Sleep until the next 1-min boundary
            now = datetime.now(IST)
            secs_into_min = now.second
            sleep_for = max(5, 60 - secs_into_min)
            logger.debug(f"💤 Slow loop sleeping {sleep_for}s")
            time.sleep(sleep_for)

        self._running = False
        self._print_daily_report()

    def _slow_cycle(self):
        """Called once per minute. Fetches new candle, checks signals."""
        candle = self.fetcher.get_latest_candle()
        if candle is None:
            return

        logger.info(
            f"🕐 1-min candle {candle.time} | "
            f"O={candle.open:.1f} H={candle.high:.1f} "
            f"L={candle.low:.1f} C={candle.close:.1f} "
            f"V={candle.volume:.0f}"
        )

        self._last_candle_volume = candle.volume
        self.indicators.add_nifty_candle(candle)

        # ── If position open: slow-path checks ──────────────────────────────
        with self._lock:
            pos     = self._active_position
            tracker = self._active_tracker
            signal  = self._active_signal
            strike  = self._active_strike

        if pos is not None and tracker is not None:
            opt_ltp = tracker.current_ltp
            avg_vol = self.indicators.get_slot_avg_volume(candle.time)
            last2   = self.indicators.last_n_nifty_closes(2)

            # Update option premium ATR with this minute's move
            self.indicators.add_option_premium(
                candle.time,
                open_p =getattr(tracker, "_prev_ltp", opt_ltp),
                close_p=opt_ltp,
            )
            tracker._prev_ltp = opt_ltp

            result = pos.monitor_slow(
                option_ltp    = opt_ltp,
                current_vol   = candle.volume,
                avg_vol       = avg_vol,
                last_2_closes = last2,
            )
            if result and result.startswith("EXIT:"):
                self._close_position(pos, signal, strike, opt_ltp)
            return  # don't look for new entries while position is open

        # ── No position: look for entry signal ────────────────────────────
        allowed, reason = self.controller.can_enter()
        if not allowed:
            logger.info(f"🚫 Entry blocked: {reason}")
            return

        sig = _entry_signal_candidate
        if sig is None:
            return

        # ── Valid signal → simulate entry ────────────────────────────────
        nifty_spot = self.fetcher.get_live_nifty_price() or candle.close
        opt_sym, opt_ltp = get_atm_option_symbol(self.groww, sig.direction, nifty_spot)

        if opt_ltp <= 0:
            logger.warning("⚠️  Could not get option LTP — skipping entry")
            return

        dynamic_sl = self.indicators.compute_dynamic_sl()
        tracker    = PaperOptionTracker(self.groww, opt_sym, opt_ltp)
        pos        = self.controller.open_position(sig, tracker.entry_premium, dynamic_sl)

        with self._lock:
            self._active_position = pos
            self._active_tracker  = tracker
            self._active_signal   = sig
            self._active_strike   = opt_sym

        logger.info(
            f"📄 [PAPER] BUY {opt_sym} | "
            f"Entry=₹{tracker.entry_premium:.2f} (incl slippage) | "
            f"SL=₹{pos.current_sl:.2f} | "
            f"Direction={sig.direction}"
        )
        alert_trade_entered(
            direction=sig.direction,
            symbol=opt_sym,
            premium=tracker.entry_premium,
            sl=pos.current_sl,
        )

    # ─── CLOSE ────────────────────────────────────────────────────────────────

    def _close_position(
        self,
        pos:     ScalpPosition,
        signal:  ScalpSignal,
        strike:  str,
        exit_ltp: float,
    ):
        if pos.state.value == "CLOSED":
            return

        # Simulate exit slippage (sell at BID, slightly below LTP)
        slipped_exit = simulate_slippage(exit_ltp, "SELL", pct=0.015)
        pos._close(pos.exit_reason or "PAPER_CLOSE", slipped_exit)

        self.controller.close_position(pos)
        summary = pos.get_summary()

        _log_trade(self.controller.trades_today, signal, summary, strike)

        alert_trade_closed(
            reason    = summary["exit_reason"],
            net_pnl   = summary["net_pnl"],
            peak_gain = f"₹{summary['peak_premium']:.2f}",
            hold_mins = summary["hold_seconds"] // 60,
        )

        with self._lock:
            self._active_position = None
            self._active_tracker  = None
            self._active_signal   = None
            self._active_strike   = ""

    def _force_close_any_open(self):
        with self._lock:
            pos     = self._active_position
            tracker = self._active_tracker
            signal  = self._active_signal
            strike  = self._active_strike

        if pos and tracker and pos.state.value != "CLOSED":
            ltp = tracker.fetch_ltp() or tracker.current_ltp
            logger.info(f"⏰ EOD force close @ ₹{ltp:.2f}")
            self._close_position(pos, signal, strike, ltp)

    def _print_daily_report(self):
        report = self.controller.daily_report()
        print("\n" + report)
        rfile = os.path.join(
            PAPER_LOG_DIR,
            f"report_{date.today().strftime('%Y%m%d')}.txt"
        )
        with open(rfile, "w") as f:
            f.write(report)
        logger.info(f"📄 Report saved → {rfile}")

        alert_daily_report(
            pnl=self.controller.daily_pnl,
            win_rate=sum(1 for t in self.controller.trade_history if t["net_pnl"] > 0)
                     / max(len(self.controller.trade_history), 1) * 100,
            trades=self.controller.trades_today,
        )


# ─── ENTRY POINT ──────────────────────────────────────────────────────────────

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)7s | %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(
                os.path.join(PAPER_LOG_DIR,
                             f"agent_{date.today().strftime('%Y%m%d')}.log")
            ),
        ],
    )

    from auth import AuthManager
    auth = AuthManager()
    if not auth.refresh_if_needed():
        logger.error("❌ Auth failed — check GROWW_TOTP_TOKEN in .env")
        sys.exit(1)

    groww  = auth.get_client()
    runner = PaperScalpRunner(groww)

    if not runner.startup():
        sys.exit(1)

    # Wait for market open
    def wait_for(hhmm: str):
        while datetime.now(IST).strftime("%H:%M") < hhmm:
            logger.info(f"⏳ Waiting for {hhmm}...")
            time.sleep(30)

    wait_for("09:15")
    runner.indicators.reset_day()

    wait_for(TRADE_START)
    logger.info(f"🔔 Trading window open ({TRADE_START})")
    runner.run()


if __name__ == "__main__":
    main()
