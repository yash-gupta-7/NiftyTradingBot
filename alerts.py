"""
alerts.py — Notification System (Fix #21)
Sends trade alerts via Telegram (primary) with console fallback.
Completely optional — agent works fine without it.

Setup:
    1. Create a Telegram bot via @BotFather → copy the token
    2. Start a chat with your bot → get your chat_id
    3. Add to .env:
         TELEGRAM_BOT_TOKEN=your_bot_token
         TELEGRAM_CHAT_ID=your_chat_id
"""

import os
import logging
import requests
from datetime import datetime
import pytz

logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TELEGRAM_URL     = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
ALERTS_ENABLED   = bool(TELEGRAM_TOKEN and TELEGRAM_CHAT_ID)


def _send(message: str, silent: bool = False) -> bool:
    """Sends a message to Telegram. Returns True on success."""
    if not ALERTS_ENABLED:
        logger.info(f"[ALERT — no Telegram] {message}")
        return False
    try:
        resp = requests.post(TELEGRAM_URL, json={
            "chat_id":              TELEGRAM_CHAT_ID,
            "text":                 message,
            "parse_mode":           "Markdown",
            "disable_notification": silent,
        }, timeout=5)
        resp.raise_for_status()
        return True
    except Exception as e:
        logger.warning(f"Telegram alert failed: {e}")
        return False


def alert_startup(strategy: str, capital: float, paper_mode: bool):
    mode = "📄 PAPER" if paper_mode else "🟢 LIVE"
    now  = datetime.now(IST).strftime("%d %b %Y %H:%M IST")
    _send(
        f"{mode} *Nifty ORB Agent Started*\n"
        f"Strategy : {strategy}\n"
        f"Capital  : ₹{capital:,.0f}\n"
        f"Time     : {now}",
        silent=True
    )


def alert_skip(reason: str):
    now = datetime.now(IST).strftime("%H:%M IST")
    _send(f"⏭ *No trade today* — {reason} ({now})", silent=True)


def alert_trade_entered(direction: str, symbol: str, premium: float, sl: float):
    icon = "📈" if direction == "UP" else "📉"
    now  = datetime.now(IST).strftime("%H:%M:%S IST")
    _send(
        f"{icon} *TRADE ENTERED*\n"
        f"Signal  : {direction}\n"
        f"Symbol  : `{symbol}`\n"
        f"Premium : ₹{premium:.2f}\n"
        f"SL      : ₹{sl:.2f}\n"
        f"Time    : {now}"
    )


def alert_checkpoint(momentum_score: int, trail_pct: float):
    now = datetime.now(IST).strftime("%H:%M IST")
    _send(
        f"🎯 *+15% Checkpoint*\n"
        f"Momentum score : {momentum_score}/3\n"
        f"Trail width    : {trail_pct*100:.0f}%\n"
        f"SL → breakeven | {now}"
    )


def alert_trade_closed(reason: str, net_pnl: float, peak_gain: str,
                        hold_mins: int):
    icon   = "💰" if net_pnl >= 0 else "🔴"
    now    = datetime.now(IST).strftime("%H:%M IST")
    _send(
        f"{icon} *TRADE CLOSED*\n"
        f"Exit reason : {reason}\n"
        f"Net P&L     : ₹{net_pnl:,.0f}\n"
        f"Peak gain   : {peak_gain}\n"
        f"Hold time   : {hold_mins} min\n"
        f"Time        : {now}"
    )


def alert_risk_circuit(reason: str):
    _send(f"🛑 *RISK CIRCUIT ACTIVE*\n{reason}")


def alert_squareoff_warning():
    _send("⏰ *3:00 PM — 10 min to square-off*", silent=False)


def alert_daily_report(pnl: float, win_rate: float, trades: int):
    icon = "📊"
    now  = datetime.now(IST).strftime("%d %b %Y")
    _send(
        f"{icon} *Daily Report — {now}*\n"
        f"Net P&L  : ₹{pnl:,.0f}\n"
        f"Trades   : {trades}\n"
        f"Win rate : {win_rate:.1f}%",
        silent=True
    )
