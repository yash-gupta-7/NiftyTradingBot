
import signal as _signal

def _handle_shutdown(sig, frame):
    """FIX #22: Graceful shutdown — square off all positions before exit."""
    import sys
    logger.critical(f"⚠️ SHUTDOWN SIGNAL {sig} — squaring off all open positions")
    # Agent instance is module-level so we can reach it here
    try:
        _active_agent.orders._exit_all("EMERGENCY_SHUTDOWN")
    except Exception as e:
        logger.error(f"Emergency exit during shutdown: {e}")
    sys.exit(1)

_active_agent = None  # set in run_agent()

"""
main.py — Trading Agent Orchestrator (Strategy V2.0)
Runs the complete daily trading cycle:

  8:50 AM  →  Startup, auth, pre-market data
  9:15 AM  →  Market open, VWAP reset, tick collection begins
  9:45 AM  →  Lock opening range, run range + gap filters
  9:45–10:30  →  Watch for breakout with all V2 filters
  10:30 AM →  Hard cutoff — no more entries
  3:10 PM  →  Force square-off all positions
  3:15 PM  →  Generate daily report

Usage:
    python main.py              # Live trading
    python main.py --paper      # Paper trading (no real orders)
    python main.py --check      # Just run pre-market checks and exit
"""

import argparse
import logging
import sys
import time
from datetime import datetime
import pytz
IST = pytz.timezone('Asia/Kolkata')

from auth import AuthManager
from data import MarketData
from strategy import StrategyEngine, Signal
from strike_selector import StrikeSelector
from order_manager import OrderManager, TradeState
from risk_manager import RiskManager
from logger import TradeLogger, setup_logging
from volume_cache import VolumeCache
from strategy_v3 import QuickScalpStrategy, V3State  # FIX #13
from utils import simulate_slippage, Heartbeat, retry  # FIX #23 #24 #25
from monitor_loop import DualLoopMonitor
from alerts import (alert_startup, alert_skip, alert_trade_entered,  # FIX #21
                     alert_trade_closed, alert_risk_circuit,
                     alert_squareoff_warning, alert_daily_report)
from config import (
    OPENING_RANGE_END, ENTRY_WINDOW_START, ENTRY_WINDOW_END,
    V3_OPENING_RANGE_END, V3_ENTRY_WINDOW_START, V3_ENTRY_WINDOW_END,
    SQUAREOFF_TIME, ALERT_TIME, MAX_TRADES_PER_DAY,
    CANDLE_5MIN, CAPITAL,
)

logger = logging.getLogger(__name__)

# ─── PAPER TRADING STUB ───────────────────────────────────────────────────────


class PaperV3Strategy(QuickScalpStrategy):
    """Paper-mode V3 — all exits simulated, no real orders."""
    def __init__(self):
        super().__init__()
        self._paper = True

    def _place_exit_market(self, symbol, qty, side):
        logger.info(f"📄 [PAPER V3] {side} {qty}×{symbol}")

class PaperOrderManager(OrderManager):
    """
    Paper trading version — logs orders but never calls Groww API.
    Use during validation phase before going live.
    """

    def enter_trade(self, signal_direction, spread_info):
        logger.info(f"📄 [PAPER] ENTER: {signal_direction} | {spread_info}")
        self.signal_direction = signal_direction
        self.spread_info      = spread_info
        # FIX #25: simulate realistic entry slippage
        raw_cost = spread_info["net_spread_cost"]
        slipped_buy  = simulate_slippage(spread_info["buy_ltp"],  "BUY")
        slipped_sell = simulate_slippage(spread_info["sell_ltp"], "SELL")
        self.net_entry_cost = round(slipped_buy - slipped_sell, 2)
        logger.info(f"📄 [PAPER] Slippage: raw=₹{raw_cost:.2f} → slipped=₹{self.net_entry_cost:.2f}")
        self.state            = TradeState.OPEN
        self.entry_time       = datetime.now(IST)
        self._set_initial_stop_loss()
        return True

    def _exit_all(self, reason):
        spread_val = self._get_current_spread_value() or self.net_entry_cost
        pnl = (spread_val - self.net_entry_cost) * self.remaining_units
        self.realised_pnl   += pnl
        self.remaining_units  = 0
        self.exited_units     = self.total_units
        self.state            = TradeState.CLOSED
        self.exit_time        = datetime.now(IST)
        self.exit_reason      = reason
        logger.info(f"📄 [PAPER] EXIT: reason={reason} | P&L=₹{self.realised_pnl:.0f}")
        return reason

    def _execute_level1_exit(self):
        units = int(self.total_units * 0.40)
        spread_val = self._get_current_spread_value() or self.net_entry_cost
        pnl = (spread_val - self.net_entry_cost) * units
        self.realised_pnl   += pnl
        self.exited_units   += units
        self.remaining_units -= units
        self.current_sl_spread_value = self.net_entry_cost
        self.at_breakeven   = True
        self.state          = TradeState.PARTIAL_EXIT
        logger.info(f"📄 [PAPER] LEVEL1 EXIT: {units} units | P&L=₹{pnl:.0f}")


# ─── MAIN AGENT ───────────────────────────────────────────────────────────────

class TradingAgent:

    def __init__(self, paper_mode: bool = False, use_v3: bool = False):
        self.paper_mode   = paper_mode
        self.use_v3       = use_v3
        self.trades_today = 0
        self.v3_strategy: QuickScalpStrategy = None  # FIX #13
        self.monitor: DualLoopMonitor = None         # dual-loop monitor

        # Core components
        self.auth    = AuthManager()
        self.risk    = RiskManager()
        self.tlogger = TradeLogger()

        self.groww   = None
        self.md      = None
        self.engine  = None
        self.striker = None
        self.orders  = None
        self.vol_cache = None

        # Daily state
        self.skip_reason_today: str | None = None
        self.vix: float | None = None

    # ─── STARTUP ──────────────────────────────────────────────────────────────

    def startup(self) -> bool:
        """8:50 AM — authenticate, load data, run pre-market checks."""
        alert_startup(
            strategy="V3" if self.use_v3 else "V2",
            capital=CAPITAL,
            paper_mode=self.paper_mode
        )
        logger.info("=" * 54)
        logger.info(f"  NIFTY ORB AGENT {'[PAPER MODE]' if self.paper_mode else '[LIVE]'} STARTING")
        logger.info(f"  {date.today().strftime('%A, %d %b %Y')}")
        logger.info("=" * 54)

        # 1. Authenticate
        if not self.auth.refresh_if_needed():
            logger.error("❌ Authentication failed. Exiting.")
            return False

        self.groww = self.auth.get_client()

        # 2. Initialise components
        self.md      = MarketData(self.groww)
        self.engine  = StrategyEngine(self.md)
        self.striker = StrikeSelector(self.groww)

        if self.paper_mode:
            self.orders = PaperOrderManager(self.groww, self.md)
        else:
            self.orders = OrderManager(self.groww, self.md)

        # FIX #13: init V3 if flag set
        if self.use_v3:
            self.v3_strategy = PaperV3Strategy() if self.paper_mode else QuickScalpStrategy()
            logger.info('🔀 Strategy V3 Quick Scalp ACTIVE')

        # 3. Fetch ATR, ADX, trend data (daily candles)
        logger.info("📡 Fetching pre-market data (ATR20, ADX, EMA trend)...")
        if not self.md.fetch_atr20():
            logger.error("❌ Failed to fetch ATR20. Cannot trade today.")
            self.skip_reason_today = "ATR20 fetch failed"
            return True  # Return True so we still generate a report

        # 4. Build volume cache (10-day per-slot averages)
        self.vol_cache = VolumeCache(self.groww)
        self.vol_cache.build()
        logger.info(self.vol_cache.summary())

        # 4. Get VIX (you can fetch from Groww or NSE directly)
        self.vix = self._get_vix()
        self.engine.vix = self.vix

        # 5. Risk daily reset
        self.risk.reset_daily_pnl()

        # 6. Pre-market strategy checks
        allowed, reason = self.risk.is_trading_allowed()
        if not allowed:
            self.engine.risk_circuit_active = True
            logger.warning(f"🛑 Risk circuit active: {reason}")

        skip = self.engine.run_premarket_checks()
        if skip:
            self.skip_reason_today = skip.value
            logger.info(f"🚫 Today is a SKIP day: {skip.value}")
            return True

        logger.info("✅ Pre-market startup complete. Ready for market open.")
        return True

    # ─── MARKET OPEN ──────────────────────────────────────────────────────────

    def on_market_open(self):
        """9:15 AM — reset VWAP, begin collecting ticks."""
        logger.info("🔔 Market OPEN (9:15 AM)")
        self.md.reset_vwap()

    # ─── OPENING RANGE LOCK ───────────────────────────────────────────────────

    def on_opening_range_close(self) -> bool:
        """
        V2: 9:45 AM — lock 30-min range (9:15–9:45).
        V3: 9:30 AM — lock 15-min range (9:15–9:30).
        Returns True if range is valid and we should watch for breakout.
        """
        if self.skip_reason_today:
            return False

        range_label = "9:30 AM (V3 15-min)" if self.use_v3 else "9:45 AM (V2 30-min)"
        logger.info(f"📊 Locking opening range ({range_label})...")
        if not self.md.fetch_opening_range():
            self.skip_reason_today = "Failed to fetch opening range"
            return False

        # Get current Nifty price to compute gap
        open_price = self.md.get_live_nifty_price()
        if not open_price:
            self.skip_reason_today = "Could not get live price"
            return False

        skip = self.engine.run_opening_range_checks(open_price)
        if skip:
            self.skip_reason_today = skip.value
            return False

        logger.info(
            f"✅ Range locked: {self.md.opening_high}/{self.md.opening_low} "
            f"({self.md.range_size} pts) | ATR20={self.md.atr20} | "
            f"Buffer={self.md.breakout_buffer:.1f}"
        )
        return True

    # ─── BREAKOUT MONITORING LOOP ─────────────────────────────────────────────

    def run_monitoring_loop(self):
        """
        9:45–10:30 AM (pre-trade): check for breakout every candle close.
        After entry: monitor P&L, stops, targets every candle close.
        After 3:10 PM: force exit and end.
        """
        if self.skip_reason_today:
            self._wait_until(SQUAREOFF_TIME)
            return

        logger.info("👁️  Monitoring loop started...")
        candle_seconds = CANDLE_5MIN * 60

        while True:
            now = datetime.now(IST)
            now_str = now.strftime("%H:%M")  # FIX #7: kept for logging only

            # ── End of day ─────────────────────────────────────────────────────
            if _time_reached(SQUAREOFF_TIME):
                if self.orders.state in (TradeState.OPEN, TradeState.PARTIAL_EXIT):
                    logger.info("⏰ 3:10 PM — Force square-off triggered")
                    self.orders.monitor(
                        self.md.get_live_nifty_price() or 0,
                        candles_df=None
                    )
                break

            # ── Fetch current data ─────────────────────────────────────────────
            nifty_price = self.md.get_live_nifty_price()
            bnf_price   = self.md.get_live_banknifty_price()
            candles_df  = self.md.get_recent_5min_candles(n=5)

            if nifty_price is None:
                logger.warning("⚠️ No live price — waiting 10s")
                time.sleep(10)
                continue

            # ── Update VWAP with latest candle ────────────────────────────────
            if candles_df is not None and not candles_df.empty:
                latest = candles_df.iloc[-1]
                self.md.update_vwap(
                    high=float(latest["high"]),
                    low=float(latest["low"]),
                    close=float(latest["close"]),
                    volume=float(latest["volume"])
                )

            # ── If no trade yet — look for breakout ───────────────────────────
            if self.orders.state == TradeState.IDLE and self.trades_today == 0:
                if not _time_reached(ENTRY_WINDOW_START):
                    logger.info(f"⏳ Waiting for entry window ({ENTRY_WINDOW_START})...")
                elif not _time_reached(ENTRY_WINDOW_END):
                    self._check_and_enter(nifty_price, bnf_price, candles_df)
                else:
                    if not self.skip_reason_today:
                        self.skip_reason_today = "No breakout by 10:30 AM"
                        logger.info("⏰ Entry window closed. No trade today.")

            # ── If trade is open — monitor it ─────────────────────────────────
            elif self.orders.state in (TradeState.OPEN, TradeState.PARTIAL_EXIT):
                exit_reason = self.orders.monitor(nifty_price, candles_df)
                if exit_reason:
                    logger.info(f"✅ Trade exited: {exit_reason}")
                    self._on_trade_closed()
                    # After close, just wait for 3:10 PM
                    self._wait_until(SQUAREOFF_TIME)
                    break

            # ── Sleep until next 5-min candle ─────────────────────────────────
            # FIX #12: correct modulo — use total seconds-into-candle not just now.second
            seconds_into_interval = (now.minute * 60 + now.second) % candle_seconds
            sleep_secs = max(10, candle_seconds - seconds_into_interval)
            Heartbeat.pulse()  # FIX #23
            if _time_reached(ALERT_TIME) and not _time_reached(SQUAREOFF_TIME):
                if not getattr(self, "_alerted_squareoff", False):
                    alert_squareoff_warning()
                    self._alerted_squareoff = True
            logger.info(f"💤 Sleeping {sleep_secs}s until next candle check...")
            time.sleep(sleep_secs)

    # ─── ENTRY HANDLER ────────────────────────────────────────────────────────

    def _check_and_enter(self, nifty_price, bnf_price, candles_df):
        """Checks for breakout and places trade if signal is confirmed."""
        if self.trades_today >= MAX_TRADES_PER_DAY:
            return

        # Volume: use per-slot average from cache (proper time-of-day normalisation)
        candle_vol = 0
        avg_vol    = 1  # fallback
        if candles_df is not None and not candles_df.empty:
            candle_vol  = float(candles_df.iloc[-1]["volume"])
            slot_avg    = self.vol_cache.get_current_slot_avg() if self.vol_cache else 0
            avg_vol     = slot_avg if slot_avg > 0 else float(candles_df["volume"].mean()) or 1

        signal, skip_reason = self.engine.check_breakout(
            current_price=nifty_price,
            candles_df=candles_df,
            breakout_candle_volume=candle_vol,
            avg_volume_for_slot=avg_vol,
            banknifty_price=bnf_price
        )

        if signal in (Signal.BUY_CALL, Signal.BUY_PUT):
            # Get spread strikes
            spread_info = self.striker.get_spread_strikes(signal.value, nifty_price)
            if not spread_info:
                logger.warning("⚠️ Could not get valid spread — skipping entry")
                self.skip_reason_today = "Spread unavailable or too expensive"
                return

            # Check per-trade risk limit
            allowed, reason = self.risk.is_spread_cost_within_risk(spread_info["total_cost"])
            if not allowed:
                logger.warning(f"🛑 Per-trade risk check failed: {reason}")
                self.skip_reason_today = reason
                return

            # Enter trade
            success = self.orders.enter_trade(signal.value, spread_info)
            if success:
                self.trades_today += 1
                logger.info(f"🎯 Trade #{self.trades_today} entered successfully")
                # Register with dual-loop monitor — fast SL kicks in immediately
                if self.monitor:
                    self.monitor.register_trade(
                        buy_symbol=spread_info["buy_symbol"],
                        sell_symbol=spread_info["sell_symbol"],
                        entry_premium=self.orders.net_entry_cost,
                        atr20=self.md.atr20 or 150,
                        is_spread=True,
                    )

        elif signal == Signal.SKIP and skip_reason:
            self.skip_reason_today = skip_reason.value
            logger.info(f"🚫 Breakout SKIPPED: {skip_reason.value}")

    # ─── TRADE CLOSED HANDLER ─────────────────────────────────────────────────

    def _on_trade_closed(self):
        """Records trade outcome in risk manager."""
        summary = self.orders.get_trade_summary()
        pnl     = summary.get("realised_pnl", 0)
        is_loss = pnl < 0
        self.risk.record_trade(pnl, is_loss)
        logger.info(
            f"{'🟢' if not is_loss else '🔴'} Trade complete | "
            f"P&L=₹{pnl:.0f} | Exit={summary.get('exit_reason')}"
        )
        alert_trade_closed(
            reason=summary.get('exit_reason', ''),
            net_pnl=pnl,
            peak_gain=summary.get('peak_gain_pct', 'N/A'),
            hold_mins=summary.get('hold_minutes', 0) or 0
        )

    # ─── EOD REPORT ───────────────────────────────────────────────────────────

    def generate_report(self):
        """3:15 PM — generate and print the daily report."""
        trade_summary = (
            self.orders.get_trade_summary()
            if self.orders and self.orders.state == TradeState.CLOSED
            else None
        )

        market_summary = {
            "opening_high": self.md.opening_high if self.md else "N/A",
            "opening_low":  self.md.opening_low  if self.md else "N/A",
            "range_size":   self.md.range_size   if self.md else "N/A",
            "atr20":        self.md.atr20         if self.md else "N/A",
            "adx":          self.md.adx_value     if self.md else "N/A",
            "vix":          self.vix,
            "gap_pct":      f"{self.md.gap_pct:.2f}" if (self.md and self.md.gap_pct) else "N/A",
            "vwap":         self.md.vwap           if self.md else "N/A",
        }

        risk_stats = self.risk.get_stats()
        report = self.tlogger.generate_daily_report(
            trade_summary=trade_summary,
            risk_stats=risk_stats,
            market_data_summary=market_summary,
            skip_reason=self.skip_reason_today
        )
        print("\n" + report)

        # Log to CSV
        if trade_summary:
            self.tlogger.log_trade(trade_summary, risk_stats)
        else:
            self.tlogger.log_skip(self.skip_reason_today or "Unknown")

    # ─── UTILS ────────────────────────────────────────────────────────────────

    def _wait_until(self, hhmm: str):
        """Sleeps until a target time HH:MM."""
        while datetime.now(IST).strftime("%H:%M") < hhmm:
            logger.info(f"⏳ Waiting until {hhmm}... current={datetime.now(IST).strftime('%H:%M:%S')}")
            time.sleep(60)

    def _get_vix(self) -> float | None:
        """
        Fetches India VIX.
        VIX is available via NSE — you can also hardcode for testing.
        """
        try:
            quote = self.groww.get_ltp(
                trading_symbol="INDIAVIX",
                exchange=self.groww.EXCHANGE_NSE,
                segment=self.groww.SEGMENT_CASH
            )
            vix = float(quote.get("ltp", 0))
            logger.info(f"⚡ India VIX = {vix}")
            return vix
        except Exception as e:
            logger.warning(f"Could not fetch VIX: {e} — VIX filter will be skipped today")
            return None


# ─── SCHEDULED RUNNER ─────────────────────────────────────────────────────────



def _parse_v3_signal(signal: str) -> tuple:
    """
    Parses a V3 strategy signal string.
    Returns (action, value):
      ('EXIT', 'STOP_LOSS')   → call orders._exit_all('STOP_LOSS')
      ('SL_UPDATE', '95.5')   → call orders update_sl
      (None, None)             → no action
    """
    if signal and signal.startswith("EXIT:"):
        return "EXIT", signal[5:]
    if signal and signal.startswith("SL_UPDATE:"):
        return "SL_UPDATE", signal[10:]
    return None, None

def _time_reached(hhmm: str) -> bool:
    """FIX #7: Proper datetime comparison instead of fragile string compare."""
    now = datetime.now(IST)
    h, m = map(int, hhmm.split(":"))
    return (now.hour, now.minute) >= (h, m)

def run_agent(paper_mode: bool = False, use_v3: bool = False):
    """
    Main entry point. Waits for the right time, then runs each phase.
    """
    global _active_agent
    agent = TradingAgent(paper_mode=paper_mode, use_v3=use_v3)
    _active_agent = agent
    _signal.signal(_signal.SIGINT,  _handle_shutdown)
    _signal.signal(_signal.SIGTERM, _handle_shutdown)

    def wait_for(hhmm: str, label: str):
        logger.info(f"⏳ Waiting for {label} ({hhmm})...")
        while True:
            now = datetime.now(IST).strftime("%H:%M")
            if now >= hhmm:
                break
            time.sleep(30)

    # ── Phase 1: Startup at 8:50 AM ───────────────────────────────────────────
    wait_for("08:50", "startup")
    if not agent.startup():
        logger.error("❌ Startup failed — exiting")
        sys.exit(1)

    if agent.skip_reason_today:
        logger.info(f"🚫 Skipping today: {agent.skip_reason_today}")
        wait_for("15:15", "report time")
        agent.generate_report()
        return

    # ── Phase 2: Market open at 9:15 AM ───────────────────────────────────────
    wait_for("09:15", "market open")
    agent.on_market_open()

    # ── Phase 3: Opening range lock at 9:45 AM ────────────────────────────────
    wait_for("09:46", "opening range close")  # slight delay to ensure candle is settled
    range_valid = agent.on_opening_range_close()

    if not range_valid:
        logger.info(f"🚫 Opening range invalid: {agent.skip_reason_today}")
        wait_for("15:15", "report time")
        agent.generate_report()
        return

    # ── Phase 4: Monitoring loop (9:46 AM → 3:10 PM) ─────────────────────────
    agent.run_monitoring_loop()

    # ── Phase 5: Daily report at 3:15 PM ─────────────────────────────────────
    wait_for("15:15", "report time")
    agent.generate_report()

    logger.info("✅ Trading day complete. Agent shutting down.")


# ─── ENTRY POINT ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    setup_logging()

    parser = argparse.ArgumentParser(description="Nifty ORB Trading Agent V2.0")
    parser.add_argument("--paper", action="store_true",
                        help="Paper trading mode (no real orders placed)")
    parser.add_argument("--check", action="store_true",
                        help="Run pre-market checks only and exit")
    parser.add_argument("--v3", action="store_true",
                        help="Use Strategy V3 Quick Scalp instead of V2")
    args = parser.parse_args()

    if args.check:
        # FIX #8: setup_logging() already called above — do NOT call again
        agent = TradingAgent(paper_mode=True)
        agent.startup()
        logger.info("✅ Pre-market check complete. Exiting.")
        sys.exit(0)

    run_agent(paper_mode=args.paper, use_v3=args.v3)
