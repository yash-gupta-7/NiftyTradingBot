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

    def is_trading_allowed(self) -> Tuple[bool, str]:
        """
        Master check — call before any trade entry.
        Returns (allowed, reason).
        """
        # 1. Consecutive loss circuit breaker
        consec = self.state.get("consecutive_losses", 0)
        if consec >= CONSECUTIVE_LOSS_STOP:
            return False, f"STOPPED: {consec} consecutive losses — stop for the week"

        if consec >= CONSECUTIVE_LOSS_PAUSE:
            last_loss_date = self.state.get("last_loss_date")
            if last_loss_date == str(date.today()):
                return False, f"PAUSED: {consec} consecutive losses — skip today"

        # 2. Daily loss limit
        daily_pnl = self.state.get("daily_pnl", 0.0)
        max_daily_loss = CAPITAL * MAX_DAILY_LOSS_PCT
        if daily_pnl <= -max_daily_loss:
            return False, f"DAILY LIMIT: loss ₹{abs(daily_pnl):.0f} > max ₹{max_daily_loss:.0f}"

        # 3. Weekly loss limit
        weekly_pnl = self._get_weekly_pnl()
        max_weekly_loss = CAPITAL * MAX_WEEKLY_LOSS_PCT
        if weekly_pnl <= -max_weekly_loss:
            return False, f"WEEKLY LIMIT: loss ₹{abs(weekly_pnl):.0f} > max ₹{max_weekly_loss:.0f}"

        # 4. Monthly loss limit
        monthly_pnl = self._get_monthly_pnl()
        max_monthly_loss = CAPITAL * MAX_MONTHLY_LOSS_PCT
        if monthly_pnl <= -max_monthly_loss:
            return False, f"MONTHLY LIMIT: loss ₹{abs(monthly_pnl):.0f} > max ₹{max_monthly_loss:.0f}"

        return True, "OK"

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

    # ─── STATE HELPERS ────────────────────────────────────────────────────────

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
            with open(RISK_STATE_FILE, "w") as f:
                json.dump(self.state, f, indent=2)
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
