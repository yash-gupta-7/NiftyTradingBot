# Nifty Trading Agent — Complete Deployment Guide

**Codebase:** 21 Python modules · 8,242 lines · 97 tests passing  
**Strategies:** V2 ORB Pro · V3 Quick Scalp · Scalp Momentum  
**Platform:** Groww API · Python 3.11+ · Ubuntu/macOS/Windows  

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Machine Setup](#2-machine-setup)
3. [Project Installation](#3-project-installation)
4. [Groww API Setup](#4-groww-api-setup)
5. [Configuration](#5-configuration)
6. [System Verification](#6-system-verification)
7. [Paper Trading — Scalp Strategy](#7-paper-trading--scalp-strategy)
8. [Paper Trading — V2 ORB](#8-paper-trading--v2-orb)
9. [Reviewing Results](#9-reviewing-results)
10. [Go-Live Checklist](#10-go-live-checklist)
11. [Production Deployment](#11-production-deployment)
12. [Daily Operations](#12-daily-operations)
13. [Monitoring & Alerts](#13-monitoring--alerts)
14. [Troubleshooting](#14-troubleshooting)
15. [Emergency Procedures](#15-emergency-procedures)
16. [File Reference](#16-file-reference)

---

## 1. Prerequisites

### Accounts Required
- Groww account with F&O trading enabled
- Groww Trading API subscription (₹499/month)
  → https://groww.in/trade-api
- Optional: Telegram account for live alerts

### Capital Requirements
| Phase | Capital | Purpose |
|-------|---------|---------|
| Paper trading | ₹0 | No real money — simulated only |
| Live (start) | ₹50,000 minimum | 1 lot buffer + drawdown headroom |
| Live (scale) | ₹1,00,000+ | After 3 profitable months |

### Hardware Requirements
| Requirement | Minimum | Recommended |
|-------------|---------|-------------|
| CPU | 2 cores | 4 cores |
| RAM | 2 GB | 4 GB |
| Storage | 10 GB | 20 GB |
| Internet | 10 Mbps stable | 50 Mbps + backup SIM |
| OS | Ubuntu 20.04 / macOS 12 / Windows 10 | Ubuntu 22.04 LTS |

> **Critical:** The machine must be ON from 8:45 AM to 3:20 PM IST on every trading day.
> A VPS (Virtual Private Server) is strongly recommended over a laptop.

---

## 2. Machine Setup

### Option A — Local Machine (laptop/desktop)

```bash
# Install Python 3.11+
# Ubuntu
sudo apt update && sudo apt install python3.11 python3.11-venv python3-pip -y

# macOS (using Homebrew)
brew install python@3.11

# Windows
# Download from https://python.org/downloads/ — check "Add to PATH"
```

### Option B — VPS (Recommended for Production)

Use a VPS so the agent runs even when your laptop is off.

**Recommended providers:**
- DigitalOcean Droplet (₹700/month, Mumbai region)
- AWS EC2 t3.micro (₹600/month, Mumbai ap-south-1)
- Hetzner Cloud CX11 (₹400/month, cheapest reliable option)

```bash
# After SSH into your VPS
ssh root@YOUR_VPS_IP

# Update system
sudo apt update && sudo apt upgrade -y

# Install Python 3.11
sudo apt install python3.11 python3.11-venv python3-pip git -y

# Set timezone to IST — critical for correct market hours
sudo timedatectl set-timezone Asia/Kolkata
timedatectl   # verify: "Asia/Kolkata"
```

---

## 3. Project Installation

```bash
# 1. Create project directory
mkdir -p ~/trading_agent && cd ~/trading_agent

# 2. Create Python virtual environment (isolates dependencies)
python3.11 -m venv venv
source venv/bin/activate          # Linux/macOS
# venv\Scripts\activate           # Windows

# 3. Copy all project files into ~/trading_agent/
# (upload via scp, git clone your private repo, or paste manually)

# 4. Install all dependencies
pip install --upgrade pip
pip install -r requirements.txt

# 5. Verify installation
python -m pytest test_agent.py --tb=no -q
# Expected: 97 passed
```

### requirements.txt contents
```
growwapi>=1.5.0
pyotp>=2.9.0
pandas>=2.0.0
numpy>=1.24.0
schedule>=1.2.0
python-dotenv>=1.0.0
requests>=2.31.0
pytz>=2024.1
pytest>=7.0.0
```

---

## 4. Groww API Setup

### Step 1 — Enable F&O Trading
1. Open Groww app → Profile → Settings
2. Enable Futures & Options trading
3. Complete the required SEBI questionnaire

### Step 2 — Subscribe to Trading API
1. Go to https://groww.in/trade-api
2. Click "Subscribe" → ₹499 + GST/month
3. Wait for activation email (usually same day)

### Step 3 — Generate TOTP Credentials
1. Log in to Groww web → Profile → Settings → Trading APIs
2. Click **"Generate TOTP Token"** (not "API Key")
3. You will see two values — save both securely:
   - **TOTP Token** → this is your `GROWW_TOTP_TOKEN`
   - **TOTP Secret** → this is your `GROWW_TOTP_SECRET`

> **Security:** Never share these. Never commit them to git.
> Anyone with these two values can trade your account.

### Step 4 — Verify Option Symbol Format
Before going live, you MUST confirm the exact symbol format Groww uses.
The verify.py script does this automatically — see Section 6.

### Step 5 — Check Lot Size
NSE periodically revises Nifty lot sizes. Confirm the current lot size:
```python
# Run this once after auth
chain = groww.get_option_chain(exchange="NSE", underlying="NIFTY", expiry_date="YYYY-MM-DD")
print(chain["data"][0].get("lot_size"))   # should print current lot size
```
Update `LOT_SIZE` in `config.py` and `strategy_scalp.py` to match.

---

## 5. Configuration

### Step 1 — Create .env file
```bash
cp .env.example .env
nano .env    # or use any text editor
```

Fill in your values:
```env
# Groww API credentials — from Step 3 above
GROWW_TOTP_TOKEN=paste_your_totp_token_here
GROWW_TOTP_SECRET=paste_your_totp_secret_here

# Starting capital in ₹
CAPITAL=50000

# Telegram alerts — optional, leave blank to disable
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

### Step 2 — Telegram Alerts (Optional but Recommended)

Get trade alerts on your phone in real time:

1. Open Telegram → search @BotFather → `/newbot`
2. Follow prompts → copy the **bot token**
3. Start a chat with your new bot
4. Get your chat ID: visit `https://api.telegram.org/bot<TOKEN>/getUpdates`
   after sending any message to the bot
5. Add both to `.env`

### Step 3 — Review config.py

Key parameters to review before starting:

```python
# Risk limits (config.py)
MAX_RISK_PER_TRADE_PCT  = 0.015   # 1.5% of capital per trade
MAX_DAILY_LOSS_PCT      = 0.030   # 3% daily stop
MAX_WEEKLY_LOSS_PCT     = 0.060   # 6% weekly stop
MAX_MONTHLY_LOSS_PCT    = 0.120   # 12% monthly stop

# Scalp strategy (strategy_scalp.py)
LOT_SIZE                = 65      # verify against live API
MAX_TRADES_PER_DAY      = 10      # start conservative
DAILY_LOSS_LIMIT        = 2000.0  # ₹2,000 daily stop for scalp
CHARGES_PER_TRADE       = 80.0    # update if Groww pricing changes
```

### Step 4 — Add Event Dates to Skip List

Edit `config.py` → `SKIP_DATES` list. Add known event dates at the
start of each month:

```python
SKIP_DATES = [
    "2026-02-01",    # Budget day
    "2026-04-09",    # RBI policy (add actual dates)
    # Check RBI calendar: https://rbi.org.in/Scripts/BS_PressReleaseDisplay.aspx
]
```

---

## 6. System Verification

Run this BEFORE any paper trading. All checks must show ✅.

```bash
cd ~/trading_agent
source venv/bin/activate
python verify.py
```

### What verify.py checks:
| Check | What it tests |
|-------|--------------|
| Config | All env variables set |
| Authentication | Groww TOTP login works |
| Nifty LTP | Live Nifty price feed |
| BankNifty LTP | Live BankNifty price feed |
| Historical data + ATR20 | Candle API + computation |
| Option chain + symbols | Real symbol format printed |
| Volume cache | 10-day slot averages built |
| Risk manager | State file readable/writable |
| Log directory | logs/ writable |
| Open positions | No stuck positions from before |

### Expected output:
```
══════════════════════════════════════════════════════
  NIFTY ORB AGENT — PRE-LIVE VERIFICATION
  14 Jun 2026
══════════════════════════════════════════════════════

── Config ─────────────────────────────────────────
  ✅  CAPITAL = ₹50,000
  ✅  GROWW_TOTP_TOKEN is set
  ✅  GROWW_TOTP_SECRET is set

── Authentication ──────────────────────────────────
  ✅  Groww API authentication successful

── Live Price Feeds ────────────────────────────────
  ✅  Nifty LTP = 23366.5
  ✅  BankNifty LTP = 51200.0

── Historical Data + ATR20 ─────────────────────────
  ✅  ATR20 = 178.4 pts
  ✅  ADX (prev day) = 19.2
  ✅  Breakout buffer = 8.9 pts
  ✅  EMA20 above count = 3/5 sessions

── Option Chain + Symbol Format ────────────────────
  ✅  Option chain fetched: 248 strikes for expiry 2026-06-19

    ┌─ SAMPLE OPTION SYMBOLS (verify format in strike_selector.py) ─
    │  NIFTY26JUN2326600CE          strike=23266 type=CE ltp=145.2
    │  NIFTY26JUN2326650CE          strike=23267 type=CE ltp=118.4
    ...
    ⚠️   ACTION REQUIRED: Compare symbols above against _build_symbol()

══════════════════════════════════════════════════════
  RESULT: 11/11 checks passed | 0 failed
  🟢 All checks passed — system is READY
══════════════════════════════════════════════════════
```

> **Action required after verify.py:** Look at the sample option symbols
> printed and confirm they match the format in `strike_selector.py`'s
> `_build_symbol()` method. If they don't match, update the method before
> any trading.

---

## 7. Paper Trading — Scalp Strategy

The scalp strategy (`paper_scalp.py`) is your first paper trading target.
It runs on live market data but places NO real orders.

### Daily Schedule

| Time | Action |
|------|--------|
| 8:50 AM | Start the runner |
| 9:15 AM | Runner detects market open, resets VWAP |
| 9:20 AM | Entry window opens |
| 9:20–3:00 PM | Agent monitors and logs simulated trades |
| 3:00 PM | Entry window closes |
| 3:15 PM | Session ends, daily report printed |

### Starting the Runner

```bash
cd ~/trading_agent
source venv/bin/activate

# Start at 8:50 AM IST
python paper_scalp.py
```

### What You Will See

```
08:52:14 |    INFO | ══════════════════════════════════════════════════════
08:52:14 |    INFO |   SCALP PAPER TRADER — STARTING
08:52:14 |    INFO |   Monday, 14 Jun 2026
08:52:14 |    INFO | ══════════════════════════════════════════════════════
08:52:15 |    INFO | 📊 Building volume cache (1-min slots)...
08:52:28 |    INFO | ✅ Volume cache built: 375 time slots | 09:30 avg=42,180
09:20:00 |    INFO | 🔔 Trading window open (09:20)
09:20:01 |    INFO | ⚡ Fast monitor started (1.5s)
09:32:14 |    INFO | 🕐 1-min candle 09:32 | O=23340 H=23368 L=23332 C=23365 V=89420
09:32:14 |    INFO | 📈 CALL signal | close=23365 > VWAP=23348 & swing_H=23352 | vol=89420 (2.1× avg)
09:32:14 |    INFO | 🎯 ATM option: NIFTY26JUN23350CE @ ₹142.50
09:32:15 |    INFO | 📄 [PAPER] BUY NIFTY26JUN23350CE | Entry=₹144.64 (incl slippage)
09:32:15 |    INFO | 🚀 SCALP ENTRY | CALL | Premium=₹144.64 | Dynamic SL=₹2.80/unit
09:34:18 |    INFO | ⚡ Fast | value=₹148.20 | gain=+3.6 | SL=₹141.8 | peak=₹148.20
09:35:01 |    INFO | 📊 Score 2 → trail ₹1.0 (run free) | SL=₹147.20
09:37:42 |    INFO | 💰 Trail SL hit: ₹147.50 | gain=₹2.86/unit | Net P&L=₹106
09:37:42 |    INFO | 📝 Trade logged → logs/paper_scalp/scalp_20260614.csv
```

### Keeping the Runner Running All Day

If you close the terminal, the runner stops. Use one of these:

**Option A — tmux (recommended for VPS):**
```bash
tmux new -s scalp
python paper_scalp.py
# Detach: Ctrl+B then D
# Reattach: tmux attach -t scalp
```

**Option B — nohup (simple):**
```bash
nohup python paper_scalp.py > logs/scalp_session.log 2>&1 &
echo "PID: $!"   # note this PID to kill later
```

**Option C — systemd service (best for VPS production):**
See Section 11 — Production Deployment.

---

## 8. Paper Trading — V2 ORB

The V2 ORB strategy uses spread orders and a 30-min opening range.
Run this in parallel with scalp paper trading on a different terminal.

```bash
# Terminal 2 — start at 8:50 AM
cd ~/trading_agent
source venv/bin/activate
python main.py --paper
```

For V3 Quick Scalp:
```bash
python main.py --paper --v3
```

---

## 9. Reviewing Results

### After Each Day

```bash
# View today's scalp trades
cat logs/paper_scalp/scalp_$(date +%Y%m%d).csv

# View today's scalp report
cat logs/paper_scalp/report_$(date +%Y%m%d).txt

# View V2/V3 ORB trades
cat logs/trades.csv | tail -20

# Run full V2/V3 paper analyzer
python paper_analyzer.py
```

### Sample Daily Scalp Report

```
══════════════════════════════════════════════════════
  SCALP STRATEGY — DAILY REPORT
══════════════════════════════════════════════════════
  Trades    : 7
  Wins      : 4   Losses: 3
  Win rate  : 57.1%
  Net P&L   : ₹187
  Charges   : ₹560
──────────────────────────────────────────────────────
  ✅ # 1 | CALL | TRAIL_SL           | ₹142.6→₹148.2 | Net ₹+287 | 312s
  🔴 # 2 | PUT  | MOMENTUM_GATE      | ₹118.4→₹118.1 | Net ₹-60  | 61s
  ✅ # 3 | CALL | CHECKPOINT_EXIT    | ₹135.2→₹137.3 | Net ₹+57  | 145s
  🔴 # 4 | PUT  | PREMIUM_SL         | ₹122.8→₹120.1 | Net ₹-256 | 88s
  ✅ # 5 | CALL | TRAIL_SL           | ₹145.0→₹151.4 | Net ₹+336 | 487s
  🔴 # 6 | CALL | TIME_SL            | ₹138.2→₹137.9 | Net ₹-60  | 90s
  ✅ # 7 | PUT  | TRAIL_SL           | ₹128.4→₹132.1 | Net ₹+160 | 223s
══════════════════════════════════════════════════════
```

### What to Track Over 20 Days

Create a simple spreadsheet with these columns:

| Date | Trades | Wins | Win% | Net P&L | Gate Exits | SL Hits | Score2 Runs |
|------|--------|------|------|---------|------------|---------|-------------|

**Green light signals (ready to go live):**
- Win rate consistently above 55%
- Momentum gate exits are cheap (average loss < ₹100)
- Score 2 trail exits are profitable (average profit > ₹250)
- Net P&L positive in at least 12 of 20 days

**Red flags (do not go live yet):**
- Premium SL hit more than 3 times per day on average
- Win rate below 50% over 20 days
- Net P&L negative over the full 20-day period

---

## 10. Go-Live Checklist

Complete every item before placing a real rupee.

### Technical Checklist

```
□ python verify.py — all 11 checks ✅
□ python -m pytest test_agent.py — 97 tests passing
□ Option symbol format confirmed against live chain
□ Lot size confirmed against live API
□ Charges per trade verified against Groww fee schedule
□ .env has real credentials (not example values)
□ data/risk_state.json exists and is clean
□ logs/ directory exists and is writable
□ Heartbeat monitor set up (see Section 11)
□ VPS timezone confirmed as Asia/Kolkata
```

### Paper Trading Checklist

```
□ Minimum 20 trading days of paper trading completed
□ Win rate > 55% over 20 days
□ Net P&L positive over 20 days
□ No day with more than 5 consecutive losses
□ Max single-day loss was within acceptable range
□ paper_analyzer.py shows all 6 thresholds GREEN
□ You have reviewed at least 3 full day logs
□ You understand why every exit reason happened
```

### Capital Checklist

```
□ Starting capital ≥ ₹50,000 in Groww account
□ F&O margin available (not locked in equity holdings)
□ Brokerage account shows F&O segment enabled
□ Test a manual buy/sell of 1 lot Nifty options first
  to confirm margin and execution work correctly
```

### Risk Checklist

```
□ Daily loss limit (₹2,000 scalp / 3% ORB) is acceptable to you
□ You will not manually override the agent during market hours
□ You have a phone with Groww app to monitor/intervene if needed
□ Emergency exit procedure is memorised (see Section 15)
□ You accept that losses will occur and the paper trading
  results are not a guarantee of live results
```

---

## 11. Production Deployment

### Recommended: systemd Service (Linux VPS)

Create a service file so the agent starts automatically:

```bash
sudo nano /etc/systemd/system/scalp-agent.service
```

Paste:
```ini
[Unit]
Description=Nifty Scalp Paper Trading Agent
After=network.target
Wants=network-online.target

[Service]
Type=simple
User=YOUR_USERNAME
WorkingDirectory=/home/YOUR_USERNAME/trading_agent
Environment="PATH=/home/YOUR_USERNAME/trading_agent/venv/bin"
ExecStart=/home/YOUR_USERNAME/trading_agent/venv/bin/python paper_scalp.py
Restart=on-failure
RestartSec=10s
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

```bash
# Enable and start
sudo systemctl daemon-reload
sudo systemctl enable scalp-agent
sudo systemctl start scalp-agent

# Check status
sudo systemctl status scalp-agent
journalctl -u scalp-agent -f   # live logs
```

### Cron-Based Schedule (Alternative)

If you don't want systemd, use cron to start at 8:50 AM and stop at 3:25 PM:

```bash
crontab -e
```

Add these lines:
```cron
# Start agent at 8:50 AM IST (Mon–Fri only)
50 8 * * 1-5 cd /home/ubuntu/trading_agent && source venv/bin/activate && python paper_scalp.py >> logs/cron.log 2>&1

# Kill any running agent at 3:25 PM (safety net)
25 15 * * 1-5 pkill -f paper_scalp.py
```

### External Watchdog (Heartbeat Monitor)

The agent writes a heartbeat file every loop cycle (`data/heartbeat`).
Set up an external check to restart if it goes stale:

```bash
# Create watchdog script
cat > ~/trading_agent/watchdog.sh << 'EOF'
#!/bin/bash
HEARTBEAT_FILE="/home/ubuntu/trading_agent/data/heartbeat"
MAX_AGE=360   # 6 minutes

if [ -f "$HEARTBEAT_FILE" ]; then
    AGE=$(( $(date +%s) - $(stat -c %Y "$HEARTBEAT_FILE") ))
    if [ $AGE -gt $MAX_AGE ]; then
        echo "$(date): Heartbeat stale (${AGE}s) — restarting agent"
        sudo systemctl restart scalp-agent
    fi
fi
EOF
chmod +x ~/trading_agent/watchdog.sh

# Add to cron — run every 5 minutes during market hours
crontab -e
# Add:
# */5 9-15 * * 1-5 /home/ubuntu/trading_agent/watchdog.sh >> /tmp/watchdog.log 2>&1
```

---

## 12. Daily Operations

### Morning Routine (8:45 AM IST)

```bash
# 1. Check system is running
sudo systemctl status scalp-agent   # if using systemd
# OR
tmux attach -t scalp                 # if using tmux

# 2. Verify no stale positions from yesterday
python verify.py

# 3. If agent not running, start it
python paper_scalp.py   # paper mode
```

### During Market Hours (9:20–3:00 PM)

- Watch Telegram alerts for trade entries and exits
- Do NOT manually trade the same options the agent is watching
- Do NOT restart the agent while a position is open (check logs first)

### End of Day (3:15–3:30 PM)

```bash
# View today's report
cat logs/paper_scalp/report_$(date +%Y%m%d).txt

# Check logs for any errors
grep -i "error\|warning\|emergency" logs/paper_scalp/agent_$(date +%Y%m%d).log

# Add today's results to your tracking spreadsheet
```

### Weekly Review (Every Friday)

```bash
# Aggregate the week's CSVs
python paper_analyzer.py --csv logs/paper_scalp/scalp_$(date +%Y%m%d).csv

# Check if any risk limits need adjustment
# Review which exit reasons dominated this week
# Verify volume cache is still accurate
```

---

## 13. Monitoring & Alerts

### Telegram Alerts You Will Receive

| Alert | When |
|-------|------|
| 🟢 LIVE Agent Started | 8:50 AM — startup confirmation |
| 📈/📉 TRADE ENTERED | Every entry with symbol, premium, SL |
| 💰/🔴 TRADE CLOSED | Every exit with reason, net P&L |
| ⏰ 3:00 PM Warning | 10 minutes before squareoff |
| 🛑 RISK CIRCUIT | Daily loss limit hit |
| 📊 Daily Report | After 3:15 PM |

### Log Files

| File | Contents |
|------|---------|
| `logs/paper_scalp/agent_YYYYMMDD.log` | Full debug log with timestamps |
| `logs/paper_scalp/scalp_YYYYMMDD.csv` | One row per trade |
| `logs/paper_scalp/report_YYYYMMDD.txt` | Daily summary |
| `logs/trades.csv` | V2/V3 ORB trade log |
| `data/heartbeat` | Last heartbeat timestamp |
| `data/risk_state.json` | Cumulative P&L and loss streaks |

### Live Log Monitoring

```bash
# Follow live logs during market hours
tail -f logs/paper_scalp/agent_$(date +%Y%m%d).log

# Filter for just trades
grep -E "ENTRY|CLOSED|EXIT|P&L" logs/paper_scalp/agent_$(date +%Y%m%d).log

# Check heartbeat age
python -c "
import time, os
f='data/heartbeat'
if os.path.exists(f):
    age = time.time() - os.path.getmtime(f)
    print(f'Heartbeat age: {age:.0f}s ({'OK' if age < 300 else 'STALE'})')
else:
    print('No heartbeat file')
"
```

---

## 14. Troubleshooting

### Problem: Authentication fails at startup

```
❌ Auth failed: check GROWW_TOTP_TOKEN in .env
```

**Fix:**
1. Verify `.env` has the correct values (not the example placeholders)
2. Check if your TOTP token has expired — regenerate from Groww portal
3. Confirm your Groww API subscription is active
4. Try manually: `python -c "from auth import AuthManager; AuthManager().refresh_if_needed()"`

---

### Problem: Volume cache build fails

```
⚠️  Volume cache failed — volume filter disabled today
```

**Fix:**
This is not fatal — the agent continues but volume filtering is looser.
Common causes:
- Market was closed yesterday (holiday) — no data available
- API rate limit hit — wait 5 minutes and retry
- Historical candle API endpoint temporarily down — check Groww status

---

### Problem: Option chain empty / symbol mismatch

```
⚠️  Strike 23350 CE not found in chain
```

**Fix:**
1. Run `python verify.py` and check the printed symbol format
2. Update `_build_symbol()` in `strike_selector.py` to match
3. Confirm the expiry date is correct (not a past date)
4. Verify Nifty is not halted (circuit breaker day)

---

### Problem: Both legs fail to fill (spread entry)

```
❌ Fill timeout — emergency exit of filled legs
```

**Fix:**
1. Check Groww app immediately for any partially open positions
2. If one leg is open and the other isn't, close the open leg manually
3. Check if market was in a circuit breaker state
4. Reduce `_wait_for_fills(timeout=15)` is not the issue — it's exchange latency

---

### Problem: Agent crashes mid-session with open position

```bash
# Check if position is open
python -c "
from auth import AuthManager
from growwapi import GrowwAPI
auth = AuthManager()
auth.refresh_if_needed()
g = auth.get_client()
pos = g.get_positions_for_user(segment='FNO')
print(pos)
"

# If position is open — EXIT MANUALLY via Groww app immediately
# Do not restart the agent until position is flat
```

---

### Problem: Agent not detecting signals (paper mode — no trades for hours)

Normal in choppy markets. Check:
```bash
# Are volume filters blocking?
grep "volume" logs/paper_scalp/agent_$(date +%Y%m%d).log | tail -20

# Is VWAP computed?
grep "VWAP" logs/paper_scalp/agent_$(date +%Y%m%d).log | tail -5

# Is swing lookback met?
grep "insufficient" logs/paper_scalp/agent_$(date +%Y%m%d).log | tail -5
```

If no candles are being received, check API connectivity:
```bash
python -c "
from auth import AuthManager
from data import MarketData
auth = AuthManager()
auth.refresh_if_needed()
md = MarketData(auth.get_client())
print(md.get_live_nifty_price())
"
```

---

## 15. Emergency Procedures

### E1 — Immediate manual exit required

If the agent crashes with an open position:

1. **Open Groww app immediately** (fastest)
2. Go to Portfolio → F&O Positions
3. Click the open position → Square Off
4. Confirm the exit

Do NOT restart the agent before the position is closed.

---

### E2 — Agent is placing too many orders

If you see unexpected order activity:

```bash
# Kill the agent immediately
sudo systemctl stop scalp-agent
# OR
pkill -f paper_scalp.py
pkill -f main.py

# Check and close any open positions via Groww app
```

---

### E3 — Daily loss limit hit

The agent stops itself automatically when `DAILY_LOSS_LIMIT` is hit.
No action needed — it will print:

```
🛑 Daily loss limit ₹2,000 hit — stopping for today
```

Do NOT override this and continue trading manually.

---

### E4 — Internet disconnects with open position

1. Agent will fail to fetch LTPs and log warnings
2. systemd will restart the agent after 10s (`RestartSec=10`)
3. On restart, the agent reads risk state but does NOT automatically
   know about the open position from the previous session
4. **Action:** Check Groww app, close position manually if needed
5. After closing, restart the agent fresh

---

### E5 — Groww API is down during market hours

1. Check https://status.groww.in
2. Close any open positions via the Groww app (not the agent)
3. Stop the agent: `sudo systemctl stop scalp-agent`
4. Do not restart until API is confirmed healthy

---

## 16. File Reference

```
trading_agent/
│
├── ENTRY POINTS
│   ├── main.py              ← V2/V3 ORB: python main.py --paper
│   ├── paper_scalp.py       ← Scalp: python paper_scalp.py
│   ├── verify.py            ← Pre-live check: python verify.py
│   ├── backtest.py          ← History test: python backtest.py --months 6
│   └── paper_analyzer.py    ← Results: python paper_analyzer.py
│
├── STRATEGIES
│   ├── strategy.py          ← V2 ORB Pro (30-min range, spread)
│   ├── strategy_v3.py       ← V3 Quick Scalp (15-min, trailing)
│   ├── strategy_scalp.py    ← Scalp Momentum (1-min, VWAP+swing)
│   └── strategy_straddle.py ← Straddle scaffold (future use)
│
├── EXECUTION
│   ├── monitor_loop.py      ← Dual-frequency: 1.5s fast + 5-min slow
│   ├── order_manager.py     ← Concurrent legs, exact fills, retry
│   ├── strike_selector.py   ← ATM option from live chain
│   └── data.py              ← ATR, ADX, VWAP, LTP, candles
│
├── SUPPORT
│   ├── config.py            ← All parameters (edit here only)
│   ├── auth.py              ← Groww TOTP auth
│   ├── risk_manager.py      ← Loss limits, circuit breakers
│   ├── volume_cache.py      ← 10-day slot volume averages
│   ├── alerts.py            ← Telegram notifications
│   ├── utils.py             ← @retry, slippage, heartbeat
│   └── logger.py            ← CSV log + daily report
│
├── VALIDATION
│   └── test_agent.py        ← 97 unit tests
│
├── CONFIG FILES
│   ├── .env                 ← Your credentials (never commit)
│   ├── .env.example         ← Template
│   ├── requirements.txt     ← pip dependencies
│   └── README.md            ← Quick start
│
└── RUNTIME DIRECTORIES (auto-created)
    ├── logs/                ← All log files and reports
    ├── logs/paper_scalp/    ← Scalp-specific logs
    └── data/                ← Agent state files
```

---

## Quick Reference Card

```
PAPER TRADING COMMANDS
──────────────────────
python verify.py                   # pre-flight check (always first)
python paper_scalp.py              # scalp paper trading
python main.py --paper             # V2 ORB paper trading
python main.py --paper --v3        # V3 quick scalp paper trading
python paper_analyzer.py           # analyze paper results

MONITORING
──────────────────────
tail -f logs/paper_scalp/agent_$(date +%Y%m%d).log   # live logs
cat logs/paper_scalp/report_$(date +%Y%m%d).txt       # daily report

TESTING
──────────────────────
python -m pytest test_agent.py --tb=short   # run all 97 tests

EMERGENCY
──────────────────────
pkill -f paper_scalp.py            # kill agent immediately
pkill -f main.py                   # kill ORB agent
# Then: open Groww app → close any open F&O positions manually

DEPLOY SEQUENCE
──────────────────────
1. python verify.py              ← all green
2. python paper_scalp.py         ← 20 trading days minimum
3. python paper_analyzer.py      ← all 6 thresholds green
4. python main.py --paper        ← V2 20 days
5. LIVE TRADING after both pass
```

---

*Last updated: June 2026 · Codebase v2.4 · 97 tests · 8,242 lines*
