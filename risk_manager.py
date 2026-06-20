"""
risk_manager.py — Risk Management Module
Enforces all institutional-grade risk controls:
  - Per-trade risk limit
  - Daily / weekly / monthly loss limits
  - Consecutive loss circuit breakers
  - Capital scaling rules
  - State persistence across sessions
"""

import json
import logging
import os
from datetime import date, datetime
from typing import Optional, Tuple

from config import (
    CAPITAL,
    MAX_RISK_PER_TRADE_PCT,
    MAX_DAILY_LOSS_PCT,
    MAX_WEEKLY_LOSS_PCT,
    MAX_MONTHLY_LOSS_PCT,
    CONSECUTIVE_LOSS_PAUSE,
    CONSECUTIVE_LOSS_STOP,
)
RISK_STATE_FILE = "data/risk_state.json"  # FIX #5: own file, no race condition

logger = logging.getLogger(__name__)


class RiskManager:
    """
    Tracks and enforces all risk rules.
    State is persisted to disk so it survives agent restarts.
    """

    def __init__(self):
        self.state = self._load_state()
        self._migrate_state()
        logger.info(f"📊 Risk state loaded: {self._summarise()}")

    # ─── PUBLIC CHECKS ────────────────────────────────────────────────────────

    # is_trading_allowed() moved above with FIX 5 unrealized P&L check

    def is_spread_cost_within_risk(self, spread_total_cost: float) -> Tuple[bool, str]:
        """
        Checks if the spread cost is within per-trade risk limit.
        Max risk per trade = 1.5% of capital.
        """
        max_risk = CAPITAL * MAX_RISK_PER_TRADE_PCT
        if spread_total_cost > max_risk:
            return False, (
                f"Trade cost ₹{spread_total_cost:.0f} exceeds "
                f"max risk/trade ₹{max_risk:.0f} (1.5% of ₹{CAPITAL:.0f})"
            )
        return True, "OK"

    # ─── TRADE OUTCOME RECORDING ──────────────────────────────────────────────

    def record_trade(self, pnl: float, was_loss: bool):
        """
        Call after every completed trade to update risk state.
        """
        today     = str(date.today())
        week_key  = self._week_key()
        month_key = self._month_key()

        # Daily P&L
        self.state.setdefault("daily_pnl", 0.0)
        self.state["daily_pnl"] = round(self.state["daily_pnl"] + pnl, 2)
        self.state["last_trade_date"] = today

        # Weekly P&L
        self.state.setdefault("weekly_pnl", {})
        self.state["weekly_pnl"][week_key] = round(
            self.state["weekly_pnl"].get(week_key, 0.0) + pnl, 2
        )

        # Monthly P&L
        self.state.setdefault("monthly_pnl", {})
        self.state["monthly_pnl"][month_key] = round(
            self.state["monthly_pnl"].get(month_key, 0.0) + pnl, 2
        )

        # Total P&L
        self.state["total_pnl"] = round(self.state.get("total_pnl", 0.0) + pnl, 2)

        # Consecutive loss tracking
        if was_loss:
            self.state["consecutive_losses"] = self.state.get("consecutive_losses", 0) + 1
            self.state["last_loss_date"]      = today
            logger.warning(
                f"🔴 Loss recorded. Consecutive losses: {self.state['consecutive_losses']}"
            )
        else:
            self.state["consecutive_losses"] = 0  # Reset on any win
            logger.info("🟢 Win recorded. Consecutive loss streak reset.")

        # Trade count
        self.state["total_trades"] = self.state.get("total_trades", 0) + 1

        # Win/loss count
        if was_loss:
            self.state["total_losses"] = self.state.get("total_losses", 0) + 1
        else:
            self.state["total_wins"] = self.state.get("total_wins", 0) + 1

        self._save_state()
        logger.info(f"📊 Risk state updated: {self._summarise()}")

    def reset_daily_pnl(self):
        """Call at start of each trading day to reset daily counter."""
        self.state["daily_pnl"] = 0.0
        self._save_state()

    # ─── STATISTICS ───────────────────────────────────────────────────────────

    def get_stats(self) -> dict:
        """Returns a dict of key risk and performance statistics."""
        total_trades = self.state.get("total_trades", 0)
        total_wins   = self.state.get("total_wins", 0)
        win_rate     = round(total_wins / total_trades * 100, 1) if total_trades > 0 else 0

        return {
            "total_trades":       total_trades,
            "total_wins":         total_wins,
            "total_losses":       self.state.get("total_losses", 0),
            "win_rate_pct":       win_rate,
            "total_pnl":          self.state.get("total_pnl", 0.0),
            "daily_pnl":          self.state.get("daily_pnl", 0.0),
            "weekly_pnl":         self._get_weekly_pnl(),
            "monthly_pnl":        self._get_monthly_pnl(),
            "consecutive_losses": self.state.get("consecutive_losses", 0),
            "capital":            CAPITAL,
            "max_daily_loss":     round(CAPITAL * MAX_DAILY_LOSS_PCT, 2),
            "max_weekly_loss":    round(CAPITAL * MAX_WEEKLY_LOSS_PCT, 2),
            "max_monthly_loss":   round(CAPITAL * MAX_MONTHLY_LOSS_PCT, 2),
        }


    def _get_weekly_pnl(self) -> float:
        return self.state.get("weekly_pnl", {}).get(self._week_key(), 0.0)

    def _get_monthly_pnl(self) -> float:
        return self.state.get("monthly_pnl", {}).get(self._month_key(), 0.0)

    def _week_key(self) -> str:
        today = date.today()
        return f"{today.year}-W{today.isocalendar()[1]:02d}"

    def _month_key(self) -> str:
        today = date.today()
        return f"{today.year}-{today.month:02d}"

    def _summarise(self) -> str:
        return (
            f"daily=₹{self.state.get('daily_pnl', 0):.0f} | "
            f"weekly=₹{self._get_weekly_pnl():.0f} | "
            f"consec_losses={self.state.get('consecutive_losses', 0)} | "
            f"total_trades={self.state.get('total_trades', 0)}"
        )


    def reconcile_with_broker(self, groww) -> dict:
        """
        FIX 5: On every startup, reconcile local risk state against
        the broker's actual position and order data.

        Problem: if the agent crashes mid-trade, the loss is never
        recorded in risk_state.json. On restart, the bot thinks it
        hasn't hit its loss limits and keeps trading.

        Solution: fetch today's realised P&L directly from the broker
        (order history + positions), compare with local state, and
        take the WORSE of the two figures. Always conservative.

        Returns dict with reconciliation details for logging.
        """
        result = {
            "local_daily_pnl":  self.state.get("daily_pnl", 0.0),
            "broker_daily_pnl": None,
            "reconciled_pnl":   self.state.get("daily_pnl", 0.0),
            "adjustment":       0.0,
            "open_positions":   [],
        }

        try:
            # ── Step 1: Check for any open F&O positions ──────────────────────
            positions = groww.get_positions_for_user(
                segment=groww.SEGMENT_FNO
            )
            open_pos = [
                p for p in positions.get("positions", [])
                if abs(int(p.get("quantity", 0))) > 0
            ]
            result["open_positions"] = [
                {
                    "symbol":   p.get("trading_symbol"),
                    "qty":      p.get("quantity"),
                    "avg":      p.get("average_price"),
                    "ltp":      p.get("last_traded_price"),
                    "pnl":      p.get("pnl"),
                }
                for p in open_pos
            ]

            if open_pos:
                logger.warning(
                    f"⚠️  {len(open_pos)} open F&O position(s) found on startup. "
                    f"These may be from a crashed session. Check Groww app."
                )
                for p in result["open_positions"]:
                    logger.warning(
                        f"  Open: {p['symbol']} qty={p['qty']} "
                        f"avg=₹{p['avg']} ltp=₹{p['ltp']} pnl=₹{p['pnl']}"
                    )

            # ── Step 2: Fetch today's realised P&L from order history ─────────
            from datetime import date as _date
            today = _date.today().strftime("%Y-%m-%d")

            # Groww order book: fetch completed orders for today
            orders = groww.get_orders(segment=groww.SEGMENT_FNO)
            today_orders = [
                o for o in orders.get("orders", [])
                if o.get("status", "").upper() in ("COMPLETE", "FILLED", "TRADED")
                and today in str(o.get("order_timestamp", ""))
            ]

            # Compute realised P&L from matched buy/sell pairs
            # Group by symbol: sum up sell proceeds - buy costs
            from collections import defaultdict
            symbol_flow = defaultdict(float)
            for o in today_orders:
                qty  = int(o.get("filled_quantity", 0))
                avg  = float(o.get("average_price", 0))
                txn  = o.get("transaction_type", "").upper()
                sym  = o.get("trading_symbol", "")
                if txn == "SELL":
                    symbol_flow[sym] += qty * avg    # proceeds
                elif txn == "BUY":
                    symbol_flow[sym] -= qty * avg    # cost

            broker_pnl = sum(symbol_flow.values())
            result["broker_daily_pnl"] = round(broker_pnl, 2)

            # ── Step 3: Take the WORSE (more negative) of local vs broker ─────
            local_pnl  = self.state.get("daily_pnl", 0.0)
            reconciled = min(local_pnl, broker_pnl)  # always conservative
            adjustment = reconciled - local_pnl

            if abs(adjustment) > 50:   # only flag meaningful differences
                logger.warning(
                    f"📊 Risk reconciliation: "
                    f"local=₹{local_pnl:.0f} | broker=₹{broker_pnl:.0f} | "
                    f"using=₹{reconciled:.0f} (adjustment ₹{adjustment:.0f})"
                )
                self.state["daily_pnl"] = reconciled
                self._save_state()
            else:
                logger.info(
                    f"📊 Risk state matches broker (diff ₹{abs(adjustment):.0f} — OK)"
                )

            result["reconciled_pnl"] = reconciled
            result["adjustment"]     = round(adjustment, 2)

        except Exception as e:
            # Reconciliation failure must NOT block trading startup.
            # Log and continue — local state is better than nothing.
            logger.error(
                f"❌ Broker reconciliation failed: {e}. "
                f"Using local risk state — verify manually."
            )

        return result

    def record_unrealized_loss(self, unrealized_pnl: float):
        """
        FIX 5: Called by the fast monitor loop every 1.5s with the
        current open position's unrealized P&L.
        Ensures circuit breakers fire even if the bot crashes before
        a trade closes — next startup will see the correct state.
        """
        if unrealized_pnl >= 0:
            return   # only track losses
        key = "peak_unrealized_loss_today"
        current_peak = self.state.get(key, 0.0)
        if unrealized_pnl < current_peak:
            self.state[key] = unrealized_pnl
            self._save_state()

    def is_trading_allowed(self) -> tuple:
        """
        Override: also check if unrealized loss from a previous crashed
        session would breach daily limit.
        """
        # FIX 5: factor in peak unrealized loss from today
        phantom_loss = self.state.get("peak_unrealized_loss_today", 0.0)
        if phantom_loss < 0:
            effective_pnl = self.state.get("daily_pnl", 0.0) + phantom_loss
            max_daily     = CAPITAL * MAX_DAILY_LOSS_PCT
            if effective_pnl <= -max_daily:
                return (
                    False,
                    f"DAILY LIMIT (incl. unrealized): "
                    f"effective loss ₹{abs(effective_pnl):.0f} > ₹{max_daily:.0f}"
                )

        # Original checks
        consec = self.state.get("consecutive_losses", 0)
        if consec >= CONSECUTIVE_LOSS_STOP:
            return False, f"STOPPED: {consec} consecutive losses"
        if consec >= CONSECUTIVE_LOSS_PAUSE:
            if self.state.get("last_loss_date") == str(__import__("datetime").date.today()):
                return False, f"PAUSED: {consec} consecutive losses"
        daily_pnl = self.state.get("daily_pnl", 0.0)
        if daily_pnl <= -(CAPITAL * MAX_DAILY_LOSS_PCT):
            return False, f"DAILY LIMIT: loss ₹{abs(daily_pnl):.0f}"
        weekly_pnl = self._get_weekly_pnl()
        if weekly_pnl <= -(CAPITAL * MAX_WEEKLY_LOSS_PCT):
            return False, f"WEEKLY LIMIT: loss ₹{abs(weekly_pnl):.0f}"
        monthly_pnl = self._get_monthly_pnl()
        if monthly_pnl <= -(CAPITAL * MAX_MONTHLY_LOSS_PCT):
            return False, f"MONTHLY LIMIT: loss ₹{abs(monthly_pnl):.0f}"
        return True, "OK"

    def reset_unrealized_peak(self):
        """Call at start of each day to clear the crash-protection state."""
        self.state["peak_unrealized_loss_today"] = 0.0
        self._save_state()

    # ─── PERSISTENCE ──────────────────────────────────────────────────────────

    def _load_state(self) -> dict:
        try:
            if os.path.exists(RISK_STATE_FILE):
                with open(RISK_STATE_FILE, "r") as f:
                    return json.load(f)
        except Exception as e:
            logger.warning(f"Could not load state file: {e}")
        return {}

    def _save_state(self):
        try:
            os.makedirs(os.path.dirname(RISK_STATE_FILE), exist_ok=True)
            import tempfile
            with tempfile.NamedTemporaryFile("w", dir=os.path.dirname(RISK_STATE_FILE), delete=False, suffix=".tmp") as f:
                json.dump(self.state, f, indent=2)
                tmp = f.name
            os.replace(tmp, RISK_STATE_FILE)
        except Exception as e:
            logger.error(f"❌ Could not save risk state: {e}")

    def _migrate_state(self):
        """Ensures state file has all required keys (handles upgrades)."""
        defaults = {
            "daily_pnl":         0.0,
            "weekly_pnl":        {},
            "monthly_pnl":       {},
            "total_pnl":         0.0,
            "total_trades":      0,
            "total_wins":        0,
            "total_losses":      0,
            "consecutive_losses": 0,
            "last_trade_date":   None,
            "last_loss_date":    None,
        }
        for key, default in defaults.items():
            self.state.setdefault(key, default)
        self._save_state()
