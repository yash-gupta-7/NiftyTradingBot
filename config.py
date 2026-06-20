"""
config.py — Strategy V2.0 Configuration
All parameters in one place. Change here, takes effect everywhere.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ─── GROWW API CREDENTIALS ────────────────────────────────────────────────────
# Store these in a .env file, never hardcode
GROWW_TOTP_TOKEN  = os.getenv("GROWW_TOTP_TOKEN")   # TOTP token from Groww API Keys page
GROWW_TOTP_SECRET = os.getenv("GROWW_TOTP_SECRET")  # TOTP secret from Groww API Keys page

# ─── ACCOUNT ─────────────────────────────────────────────────────────────────
CAPITAL           = float(os.getenv("CAPITAL", 50000))   # Starting capital in ₹
LOTS_PER_TRADE    = 1                                     # Fixed — do NOT change for 6 months
NIFTY_LOT_SIZE    = 25   # NSE revised lot size (2024 revision from 75 → 25)

# ─── OPENING RANGE ────────────────────────────────────────────────────────────
OPENING_RANGE_START = "09:15"   # Market open

# V2 (ORB Pro) — 30-min range
OPENING_RANGE_END   = "09:45"   # V2: 30-min candle closes at 9:45
ENTRY_WINDOW_START  = "09:45"   # V2: earliest entry
ENTRY_WINDOW_END    = "10:30"   # V2: hard cutoff

# V3 (Quick Scalp) — 15-min range, trade in first candle only
V3_OPENING_RANGE_END  = "09:30"   # V3: 15-min candle closes at 9:30
V3_ENTRY_WINDOW_START = "09:30"   # V3: watch from 9:30 immediately
V3_ENTRY_WINDOW_END   = "09:45"   # V3: hard cutoff — only first 2 candles
V3_BREAKOUT_BUFFER    = 10        # V3: fixed buffer pts (not ATR-based)

# ─── SQUARE-OFF ───────────────────────────────────────────────────────────────
SQUAREOFF_TIME      = "15:10"   # Force exit all positions
ALERT_TIME          = "15:00"   # 10-min warning

# ─── RANGE FILTERS (ATR-based — V2 change from fixed points) ──────────────────
ATR_LOOKBACK            = 20    # 20-day ATR
MIN_RANGE_ATR_MULTIPLE  = 0.35  # Range must be > 35% of ATR20
MAX_RANGE_ATR_MULTIPLE  = 1.40  # Range must be < 140% of ATR20

# ─── GAP FILTER ───────────────────────────────────────────────────────────────
MAX_GAP_PCT     = 0.50          # Skip if gap > 0.5% (V2: tightened from 0.75%)

# ─── VOLATILITY FILTERS ───────────────────────────────────────────────────────
VIX_MIN         = 11.0          # Skip if VIX below this
VIX_MAX         = 20.0          # Skip if VIX above this (V2: tightened from 18)
IV_RANK_MAX     = 60            # Skip if IV rank > 60%

# ─── REGIME FILTER ────────────────────────────────────────────────────────────
ADX_LOOKBACK    = 14            # ADX period
ADX_MIN         = 15            # Skip if prev-day ADX below this (choppy market)

# ─── VOLUME FILTER ────────────────────────────────────────────────────────────
VOLUME_MULTIPLIER = 2.0         # Breakout candle must have 2× average volume

# ─── BREAKOUT BUFFER ──────────────────────────────────────────────────────────
BREAKOUT_BUFFER_ATR_PCT = 0.05  # Buffer = 5% of ATR20 (dynamic, not fixed 5 pts)

# ─── OPTION SPREAD PARAMETERS ─────────────────────────────────────────────────
SPREAD_WIDTH        = 50        # ATM+50 for calls, ATM-50 for puts
MAX_SPREAD_COST     = 5000      # Skip trade if spread costs more than ₹5,000/lot
STRIKE_INTERVAL     = 50        # Nifty strike interval

# ─── STOP LOSS ────────────────────────────────────────────────────────────────
SL_PREMIUM_PCT  = 0.25          # Exit if spread value drops to 25% of cost (V2: tightened from 40%)

# ─── PROFIT BOOKING ───────────────────────────────────────────────────────────
LEVEL1_ATR_MULTIPLE     = 1.0   # Exit 40% at 1× ATR move
LEVEL1_EXIT_PCT         = 0.40  # Exit 40% of position at Level 1 (V2: changed from 50%)
LEVEL2_TRAIL_CANDLES    = 2     # Trail using 2-bar swing high/low on 5-min chart
LEVEL3_ATR_MULTIPLE     = 1.80  # Hard exit at 1.8× ATR move

# ─── RISK LIMITS (% of capital) ───────────────────────────────────────────────
MAX_RISK_PER_TRADE_PCT  = 0.015  # 1.5% of capital per trade
MAX_DAILY_LOSS_PCT      = 0.030  # 3% of capital daily loss limit
MAX_WEEKLY_LOSS_PCT     = 0.060  # 6% of capital weekly loss limit
MAX_MONTHLY_LOSS_PCT    = 0.120  # 12% of capital monthly loss limit

# ─── CIRCUIT BREAKERS ─────────────────────────────────────────────────────────
CONSECUTIVE_LOSS_PAUSE  = 3     # Pause trading after N consecutive losses
CONSECUTIVE_LOSS_STOP   = 5     # Stop for week after N consecutive losses
MAX_TRADES_PER_DAY      = 1     # Absolute maximum — never more than 1 trade/day

# ─── INSTRUMENTS ──────────────────────────────────────────────────────────────
NIFTY_SYMBOL        = "NIFTY"
BANKNIFTY_SYMBOL    = "BANKNIFTY"
EXCHANGE            = "NSE"
SEGMENT_FNO         = "FNO"
SEGMENT_CASH        = "CASH"
INDEX_SEGMENT       = "INDICES"

# ─── CANDLE INTERVALS ─────────────────────────────────────────────────────────
CANDLE_5MIN         = 5     # minutes — used for breakout and trailing
CANDLE_30MIN        = 30    # minutes — used for opening range

# ─── SKIP DATES (YYYY-MM-DD) ──────────────────────────────────────────────────
# Add known event days here at start of each month.
# The agent also automatically skips all Thursdays.
SKIP_DATES = [
    # Budget day
    "2026-02-01",
    # Add RBI policy dates, election results etc.
    # "2026-04-09",  # Example: RBI policy
]

# ─── STRATEGY V3 PARAMETERS ─────────────────────────────────────────────────
# FIX: moved from strategy_v3.py so all magic numbers live in one place
V3_BROKERAGE        = 400     # ₹ round-trip charges per lot
V3_MIN_RANGE_PTS    = 30      # skip if 9:15–9:30 candle range < 30 pts
V3_MAX_RANGE_PTS    = 150     # skip if range > 150 pts
V3_SL_INITIAL_PCT   = 0.20    # 20% initial stop loss on premium
V3_CHECKPOINT_PCT   = 0.15    # 15% gain → SL moves to breakeven
V3_TRAIL_BY_SCORE   = {0: 0.10, 1: 0.10, 2: 0.15, 3: 0.20}  # trail % by momentum score
V3_TRAIL_TIGHTEN    = [(0.50, 0.06), (0.30, 0.03)]  # auto-tighten at gain thresholds
V3_MAX_HOLD_MINUTES = 30      # hard time exit

# ─── BANKNIFTY CONFIRMATION BUFFER ───────────────────────────────────────────
# FIX #17: was hardcoded 20 pts in strategy.py — now config-driven
BNF_BREAKOUT_BUFFER_PTS = 30  # BankNifty moves ~2× Nifty; 30 pts is appropriate

# ─── LOGGING ──────────────────────────────────────────────────────────────────
LOG_DIR         = "logs"
TRADE_LOG_FILE  = "logs/trades.csv"
DAILY_LOG_FILE  = "logs/daily_report.txt"
STATE_FILE      = "data/agent_state.json"
