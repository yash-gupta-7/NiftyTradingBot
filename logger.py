"""
logger.py — Trade Logger & Daily Report
Logs every trade, every skip, and every decision to CSV and text files.
Generates a clean daily report at 3:15 PM.
"""

import csv
import json
import logging
import os
from datetime import date, datetime
from typing import Optional

from config import TRADE_LOG_FILE, DAILY_LOG_FILE, LOG_DIR

logger = logging.getLogger(__name__)


def setup_logging():
    """
    Configures the root logger. Call once at agent startup.
    Logs to both console and daily rotating file.
    """
    os.makedirs(LOG_DIR, exist_ok=True)
    today = date.today().strftime("%Y%m%d")
    log_file = os.path.join(LOG_DIR, f"agent_{today}.log")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)7s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler()
        ]
    )
    logger.info(f"✅ Logging initialised → {log_file}")


class TradeLogger:
    """
    Writes structured trade records to CSV and generates daily reports.
    """

    def __init__(self):
        os.makedirs(LOG_DIR, exist_ok=True)
        self._ensure_csv_header()

    # ─── LOG TRADE ────────────────────────────────────────────────────────────

    def log_trade(self, trade_summary: dict, risk_stats: dict):
        """
        Logs a completed (or skipped) trade to the CSV file.
        """
        today = date.today().strftime("%Y-%m-%d")
        row = {
            "date":             today,
            "signal":           trade_summary.get("signal", "SKIP"),
            "buy_symbol":       trade_summary.get("buy_symbol", "-"),
            "sell_symbol":      trade_summary.get("sell_symbol", "-"),
            "entry_cost":       trade_summary.get("entry_cost", 0),
            "total_units":      trade_summary.get("total_units", 0),
            "exit_reason":      trade_summary.get("exit_reason", "-"),
            "entry_time":       trade_summary.get("entry_time", "-"),
            "exit_time":        trade_summary.get("exit_time", "-"),
            "realised_pnl":     trade_summary.get("realised_pnl", 0),
            "daily_pnl":        risk_stats.get("daily_pnl", 0),
            "weekly_pnl":       risk_stats.get("weekly_pnl", 0),
            "monthly_pnl":      risk_stats.get("monthly_pnl", 0),
            "consecutive_losses": risk_stats.get("consecutive_losses", 0),
            "win_rate_pct":     risk_stats.get("win_rate_pct", 0),
        }

        try:
            with open(TRADE_LOG_FILE, "a", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=row.keys())
                writer.writerow(row)
            logger.info(f"📝 Trade logged to {TRADE_LOG_FILE}")
        except Exception as e:
            logger.error(f"❌ Failed to write trade log: {e}")

    def log_skip(self, reason: str, market_data_summary: dict = None):
        """
        Logs a skipped day (no trade taken).
        """
        today = date.today().strftime("%Y-%m-%d")
        row = {
            "date":             today,
            "signal":           "SKIP",
            "buy_symbol":       "-",
            "sell_symbol":      "-",
            "entry_cost":       0,
            "total_units":      0,
            "exit_reason":      reason,
            "entry_time":       "-",
            "exit_time":        "-",
            "realised_pnl":     0,
            "daily_pnl":        0,
            "weekly_pnl":       0,
            "monthly_pnl":      0,
            "consecutive_losses": 0,
            "win_rate_pct":     "-",
        }

        if market_data_summary:
            row.update(market_data_summary)

        try:
            with open(TRADE_LOG_FILE, "a", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=list(row.keys()))
                writer.writerow(row)
        except Exception as e:
            logger.error(f"❌ Failed to log skip: {e}")

    # ─── DAILY REPORT ─────────────────────────────────────────────────────────

    def generate_daily_report(
        self,
        trade_summary: Optional[dict],
        risk_stats: dict,
        market_data_summary: dict,
        skip_reason: Optional[str] = None
    ) -> str:
        """
        Generates a formatted daily report. Saves to file and returns as string.
        """
        today = date.today().strftime("%d-%b-%Y (%A)")
        now   = datetime.now(IST).strftime("%H:%M:%S")

        lines = [
            "═" * 54,
            "         NIFTY ORB AGENT — DAILY REPORT",
            f"         {today}",
            f"         Generated at {now}",
            "═" * 54,
        ]

        # ── Market data summary ────────────────────────────────────────────────
        lines += [
            "",
            "MARKET CONDITIONS",
            "─" * 40,
            f"  Opening High   : {market_data_summary.get('opening_high', 'N/A')}",
            f"  Opening Low    : {market_data_summary.get('opening_low', 'N/A')}",
            f"  Range Size     : {market_data_summary.get('range_size', 'N/A')} pts",
            f"  ATR20          : {market_data_summary.get('atr20', 'N/A')}",
            f"  ADX            : {market_data_summary.get('adx', 'N/A')}",
            f"  VIX            : {market_data_summary.get('vix', 'N/A')}",
            f"  Gap            : {market_data_summary.get('gap_pct', 'N/A')}%",
            f"  VWAP at entry  : {market_data_summary.get('vwap', 'N/A')}",
        ]

        # ── Trade or skip ──────────────────────────────────────────────────────
        if skip_reason:
            lines += [
                "",
                "TRADE DECISION",
                "─" * 40,
                f"  Status         : ⏭  SKIPPED",
                f"  Reason         : {skip_reason}",
                f"  P&L Today      : ₹0",
            ]
        elif trade_summary:
            pnl = trade_summary.get("realised_pnl", 0)
            pnl_icon = "✅" if pnl >= 0 else "🔴"
            lines += [
                "",
                "TRADE DETAILS",
                "─" * 40,
                f"  Status         : {pnl_icon} EXECUTED",
                f"  Signal         : {trade_summary.get('signal', '-')}",
                f"  Buy Symbol     : {trade_summary.get('buy_symbol', '-')}",
                f"  Sell Symbol    : {trade_summary.get('sell_symbol', '-')}",
                f"  Entry Cost/unit: ₹{trade_summary.get('entry_cost', 0):.2f}",
                f"  Units Traded   : {trade_summary.get('total_units', 0)}",
                f"  Entry Time     : {trade_summary.get('entry_time', '-')}",
                f"  Exit Time      : {trade_summary.get('exit_time', '-')}",
                f"  Exit Reason    : {trade_summary.get('exit_reason', '-')}",
                "",
                f"  Realised P&L   : ₹{pnl:.2f}",
            ]
        else:
            lines += ["", "  Status: NO TRADE"]

        # ── Risk summary ───────────────────────────────────────────────────────
        lines += [
            "",
            "RISK SUMMARY",
            "─" * 40,
            f"  Daily P&L      : ₹{risk_stats.get('daily_pnl', 0):.2f}",
            f"  Weekly P&L     : ₹{risk_stats.get('weekly_pnl', 0):.2f}",
            f"  Monthly P&L    : ₹{risk_stats.get('monthly_pnl', 0):.2f}",
            f"  Total P&L      : ₹{risk_stats.get('total_pnl', 0):.2f}",
            f"  Total Trades   : {risk_stats.get('total_trades', 0)}",
            f"  Win Rate       : {risk_stats.get('win_rate_pct', 0):.1f}%",
            f"  Consec Losses  : {risk_stats.get('consecutive_losses', 0)}",
            "",
            f"  Daily limit    : ₹{risk_stats.get('max_daily_loss', 0):.0f}",
            f"  Weekly limit   : ₹{risk_stats.get('max_weekly_loss', 0):.0f}",
            f"  Monthly limit  : ₹{risk_stats.get('max_monthly_loss', 0):.0f}",
            "",
            "═" * 54,
        ]

        report = "\n".join(lines)

        # Save to file
        try:
            daily_file = os.path.join(
                LOG_DIR,
                f"report_{date.today().strftime('%Y%m%d')}.txt"
            )
            with open(daily_file, "w") as f:
                f.write(report)
            logger.info(f"📄 Daily report saved → {daily_file}")
        except Exception as e:
            logger.error(f"❌ Failed to save daily report: {e}")

        return report

    # ─── HELPERS ──────────────────────────────────────────────────────────────

    def _ensure_csv_header(self):
        """Writes CSV header if file doesn't exist yet."""
        if not os.path.exists(TRADE_LOG_FILE):
            headers = [
                "date", "signal", "buy_symbol", "sell_symbol",
                "entry_cost", "total_units", "exit_reason",
                "entry_time", "exit_time", "realised_pnl",
                "daily_pnl", "weekly_pnl", "monthly_pnl",
                "consecutive_losses", "win_rate_pct"
            ]
            try:
                with open(TRADE_LOG_FILE, "w", newline="") as f:
                    writer = csv.DictWriter(f, fieldnames=headers)
                    writer.writeheader()
            except Exception as e:
                logger.error(f"❌ Failed to create trade log: {e}")
