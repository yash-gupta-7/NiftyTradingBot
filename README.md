# Nifty ORB Trading Agent — V2.0

Automated intraday trading agent for Nifty Options using the
Open Range Breakout strategy. Built on Groww API (Python SDK).

## Strategy Summary
- **Instrument**: Nifty weekly options (bull/bear call spread)
- **Opening range**: 9:15–9:45 AM (30-min candle)
- **Entry window**: 9:45–10:30 AM only
- **Filters**: VWAP, BankNifty, EMA trend, ADX, ATR range, VIX, volume
- **Exits**: Level 1 at 1× ATR, trail with 2-bar swing, hard exit at 3:10 PM
- **Risk**: 1 trade/day, 1 lot, 1.5% max risk per trade

---

## Setup

### 1. Clone and install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure credentials

```bash
cp .env.example .env
# Edit .env and add your Groww TOTP token + secret
```

Get your TOTP credentials from:
https://groww.in/trade-api/api-keys
→ Click "Generate TOTP token"

### 3. Subscribe to Groww Trading API
https://groww.in/user/profile/trading-apis
(₹499 + GST per month)

---

## Running the Agent

### Paper trading (recommended first — no real money)
```bash
python main.py --paper
```

### Pre-market check only (test auth + data pipeline)
```bash
python main.py --check
```

### Live trading (real money — only after paper validation)
```bash
python main.py
```

---

## Project Structure

```
trading_agent/
├── main.py            ← Orchestrator (run this)
├── config.py          ← All strategy parameters
├── auth.py            ← Groww TOTP authentication
├── data.py            ← Market data (ATR, VWAP, candles)
├── strategy.py        ← ORB signal engine + all filters
├── strike_selector.py ← ATM strike + spread leg selection
├── order_manager.py   ← Order placement + position monitoring
├── risk_manager.py    ← Risk controls + circuit breakers
├── logger.py          ← Trade logging + daily reports
├── requirements.txt
├── .env.example
├── logs/              ← Daily logs + trade CSV
└── data/              ← Agent state (P&L, streaks)
```

---

## Deployment Phases

| Phase | Duration | Mode | Goal |
|-------|----------|------|------|
| 1 — Backtest | 2 weeks | Historical data | Validate strategy |
| 2 — Paper trade | 4–8 weeks | `--paper` flag | Validate live execution |
| 3 — Live (1 lot) | 3–6 months | Live | Validate real P&L |
| 4 — Scale | After Phase 3 | Live | +1 lot at a time |

**Do not skip phases. Do not go live before 4 weeks of paper trading.**

---

## Risk Limits (from config.py)

| Limit | Value |
|-------|-------|
| Max trades/day | 1 |
| Max lot size | 1 lot (75 units) |
| Max risk/trade | 1.5% of capital |
| Daily loss limit | 3% of capital |
| Weekly loss limit | 6% of capital |
| Monthly loss limit | 12% of capital |
| Consecutive losses → pause | 3 |
| Consecutive losses → stop | 5 |

---

## Important Notes

1. **Symbol format**: Verify Groww's exact option symbol format before live trading.
   Use `groww.get_option_chain()` to inspect symbol names from the chain.

2. **VIX**: India VIX symbol may differ — confirm with Groww instruments master.

3. **Spread orders**: Each spread leg is a separate order. Both must fill for
   the trade to be valid. Always verify in Groww app.

4. **Token**: TOTP flow has no daily expiry. Still validate at startup each day.

5. **Cron (optional)**: Schedule `python main.py` to run at 8:50 AM daily:
   ```
   50 8 * * 1-5 cd /path/to/trading_agent && python main.py >> logs/cron.log 2>&1
   ```
