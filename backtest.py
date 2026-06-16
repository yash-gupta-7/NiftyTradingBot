"""
backtest.py — Strategy V2.0 Historical Backtester
Tests the strategy on real historical Nifty 5-min data from Groww.

Option P&L is simulated using a delta-based spread model because
Groww doesn't provide historical option chain data. The model is
conservative and realistic for ATM spreads.

Spread P&L model:
  - Net spread cost = ATR20 × SPREAD_COST_ATR_FACTOR
  - Spread delta    = 0.20 (ATM delta 0.5 minus OTM delta ~0.3)
  - Spread gains as Nifty moves: capped at max spread width (50pts)
  - Theta decay: 12% of spread cost per hour held
  - Stop loss at 25% of spread cost (same as live)

Usage:
    python backtest.py                  # backtest last 6 months
    python backtest.py --months 12      # backtest last 12 months
    python backtest.py --from 2024-01-01 --to 2024-12-31
    python backtest.py --no-auth        # use synthetic data (no API needed)
"""

import argparse
import json
import logging
import math
import os
import sys
from datetime import date, datetime, timedelta
from typing import Optional

logging.basicConfig(level=logging.WARNING, format="%(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

# ─── CONSTANTS ────────────────────────────────────────────────────────────────

# FIX #14: import from config instead of duplicating constants
from config import (
    STRIKE_INTERVAL        as SPREAD_WIDTH,
    SL_PREMIUM_PCT         as SL_PCT,
    LEVEL1_ATR_MULTIPLE    as L1_ATR_MULTIPLE,
    LEVEL1_EXIT_PCT        as L1_EXIT_PCT,
    LEVEL3_ATR_MULTIPLE    as L3_ATR_MULTIPLE,
    ENTRY_WINDOW_END,
    SQUAREOFF_TIME,
    MIN_RANGE_ATR_MULTIPLE,
    MAX_RANGE_ATR_MULTIPLE,
    MAX_GAP_PCT,
    VIX_MIN,
    VIX_MAX,
    ADX_MIN,
    VOLUME_MULTIPLIER,
    NIFTY_LOT_SIZE         as LOT_SIZE,
)

# Backtest-specific constants (not in config — simulation model parameters)
SPREAD_COST_ATR_FACTOR   = 0.28    # net spread cost ≈ 28% of ATR20
SPREAD_DELTA             = 0.20    # effective spread delta (per index point)
THETA_PER_HOUR_FACTOR    = 0.12    # 12% of spread cost per hour decay
BROKERAGE_PER_TRADE      = 400     # ₹ round-trip charges


# ─── OPTION PRICING MODEL ─────────────────────────────────────────────────────

class OptionModel:
    """
    Simplified ATM spread pricing model.
    Conservative estimates — real results may differ due to IV changes.
    """

    @staticmethod
    def spread_cost(atr20: float) -> float:
        """Estimates net spread cost (buy ATM - sell OTM) in pts."""
        return round(atr20 * SPREAD_COST_ATR_FACTOR, 2)

    @staticmethod
    def spread_value(
        entry_cost: float,
        nifty_move: float,      # pts from breakout in trade direction
        hours_held: float,      # hours since entry
    ) -> float:
        """
        Estimates current spread value given Nifty move and time held.
        Spread value = entry_cost + delta_gain - theta_decay, capped at max
        """
        delta_gain   = min(abs(nifty_move) * SPREAD_DELTA, SPREAD_WIDTH - entry_cost)
        theta_decay  = entry_cost * THETA_PER_HOUR_FACTOR * hours_held
        value        = entry_cost + delta_gain - theta_decay
        return max(round(value, 2), 0.0)


# ─── BACKTESTER ───────────────────────────────────────────────────────────────

class Backtester:

    def __init__(self, groww=None):
        self.groww   = groww
        self.model   = OptionModel()
        self.results = []      # one dict per trading day
        self.trades  = []      # one dict per traded day

    # ─── MAIN RUN ─────────────────────────────────────────────────────────────

    def run(self, start_date: date, end_date: date) -> dict:
        """
        Runs backtest from start_date to end_date.
        Returns a summary dict with all statistics.
        """
        print(f"\n🔄 Fetching historical data: {start_date} → {end_date}...")

        # Fetch daily data for ATR/ADX/EMA computation
        daily_data = self._fetch_daily_candles(start_date - timedelta(days=60), end_date)
        if not daily_data:
            print("❌ No daily data. Cannot run backtest.")
            return {}

        # Fetch 5-min data for the backtest period
        intraday_data = self._fetch_5min_candles(start_date, end_date)
        if not intraday_data:
            print("❌ No intraday data. Cannot run backtest.")
            return {}

        print(f"✅ Data loaded: {len(daily_data)} daily candles | {len(intraday_data)} 5-min candles")
        print(f"\n{'─'*70}")
        print(f"{'Date':12} {'Signal':10} {'Entry':8} {'Exit Rsn':18} {'P&L':>10} {'Running':>12}")
        print(f"{'─'*70}")

        # Group intraday candles by date
        candles_by_date = self._group_by_date(intraday_data)
        trading_days    = sorted(candles_by_date.keys())

        # Compute rolling daily metrics
        daily_df = self._compute_daily_metrics(daily_data)

        running_pnl        = 0.0
        total_trades       = 0
        skip_days          = 0
        prev_intraday_close = None   # last 5-min close of previous day

        for trade_date in trading_days:
            if trade_date < start_date or trade_date > end_date:
                # Still update prev_intraday_close so it's correct when we enter range
                day_c = candles_by_date.get(trade_date, [])
                if day_c:
                    prev_intraday_close = day_c[-1]["close"]
                continue

            day_candles = candles_by_date[trade_date]
            day_metrics = daily_df.get(trade_date)

            if day_metrics is None:
                if day_candles:
                    prev_intraday_close = day_candles[-1]["close"]
                continue

            result = self._simulate_day(trade_date, day_candles, day_metrics,
                                        prev_intraday_close=prev_intraday_close)
            # Update for next iteration
            if day_candles:
                prev_intraday_close = day_candles[-1]["close"]

            self.results.append(result)

            if result["traded"]:
                total_trades += 1
                running_pnl  += result["net_pnl"]
                self.trades.append(result)
                signal_str = result["signal"]
                print(
                    f"{str(trade_date):12} "
                    f"{signal_str:10} "
                    f"{result['entry_time']:8} "
                    f"{result['exit_reason'][:18]:18} "
                    f"{'₹'+str(int(result['net_pnl'])):>10} "
                    f"{'₹'+str(int(running_pnl)):>12}"
                )
            else:
                skip_days += 1
                print(
                    f"{str(trade_date):12} "
                    f"{'SKIP':10} "
                    f"{'':8} "
                    f"{result['skip_reason'][:18]:18} "
                    f"{'₹0':>10} "
                    f"{'₹'+str(int(running_pnl)):>12}"
                )

        print(f"{'─'*70}")

        return self._compute_stats(running_pnl, total_trades, skip_days)

    # ─── DAY SIMULATION ───────────────────────────────────────────────────────

    def _simulate_day(self, trade_date: date, candles: list, metrics: dict,
                      prev_intraday_close: float = None) -> dict:
        """
        Simulates one trading day. Applies all V2 filters, then simulates
        breakout entry and exit.
        """
        result = {
            "date":        str(trade_date),
            "traded":      False,
            "signal":      None,
            "entry_time":  "-",
            "exit_reason": "-",
            "gross_pnl":   0.0,
            "net_pnl":     0.0,
            "skip_reason": "-",
            "spread_cost": 0.0,
            "entry_nifty": 0.0,
            "exit_nifty":  0.0,
        }

        # ── Filter 1: Skip Thursdays ───────────────────────────────────────────
        if trade_date.weekday() == 3:
            result["skip_reason"] = "Expiry Thursday"
            return result

        atr20       = metrics.get("atr20", 0)
        adx         = metrics.get("adx", 0)
        ema20_above = metrics.get("ema20_above", 3)  # count of sessions above EMA20
        prev_close  = metrics.get("prev_close", 0)
        vix_approx  = metrics.get("vix_approx", 14)  # estimated VIX

        if atr20 == 0:
            result["skip_reason"] = "ATR20 not computed"
            return result

        # ── Filter 2: ADX ─────────────────────────────────────────────────────
        if adx < ADX_MIN:
            result["skip_reason"] = f"ADX {adx:.1f} < {ADX_MIN}"
            return result

        # ── Filter 3: VIX (approximated from realized vol) ────────────────────
        if not (VIX_MIN <= vix_approx <= VIX_MAX):
            result["skip_reason"] = f"VIX {vix_approx:.1f} out of range"
            return result

        # ── Opening range from first 30-min ───────────────────────────────────
        range_candles = [c for c in candles if c["time"] <= "09:45"]
        if not range_candles:
            result["skip_reason"] = "No opening range candles"
            return result

        oh = max(c["high"]  for c in range_candles)
        ol = min(c["low"]   for c in range_candles)
        range_size = oh - ol

        # ── Filter 4: Gap ─────────────────────────────────────────────────────
        # Use prev_intraday_close (last 5-min close of yesterday) for gap
        # This keeps gap within the same data stream — accurate for both real and synthetic
        first_open   = candles[0]["open"] if candles else 0
        ref_close    = prev_intraday_close if prev_intraday_close else prev_close
        gap_pct      = abs(first_open - ref_close) / ref_close * 100 if ref_close else 0
        if gap_pct > MAX_GAP_PCT:
            result["skip_reason"] = f"Gap {gap_pct:.2f}% > {MAX_GAP_PCT}%"
            return result

        # ── Filter 5: Range size (ATR-based) ──────────────────────────────────
        min_range = atr20 * MIN_RANGE_ATR_MULTIPLE
        max_range = atr20 * MAX_RANGE_ATR_MULTIPLE
        if range_size < min_range:
            result["skip_reason"] = f"Range {range_size:.0f} < min {min_range:.0f}"
            return result
        if range_size > max_range:
            result["skip_reason"] = f"Range {range_size:.0f} > max {max_range:.0f}"
            return result

        # ── Filter 6: Doji — use full 30-min body (first open → last close) ──
        range_open  = range_candles[0]["open"]
        range_close = range_candles[-1]["close"]
        body = abs(range_close - range_open)
        if range_size > 0 and body < range_size * 0.20:   # 20% threshold
            result["skip_reason"] = "Doji opening candle"
            return result

        # ── Breakout detection (entry window 09:45–10:30) ─────────────────────
        buffer = atr20 * 0.05
        spread_cost = OptionModel.spread_cost(atr20)

        entry_candle = None
        signal = None

        for candle in candles:
            if candle["time"] < "09:45":
                continue
            if candle["time"] > ENTRY_WINDOW_END:
                break

            # Volume filter (simplified: check if candle volume is 2× day avg)
            avg_vol = metrics.get("avg_5min_volume", 1)
            if avg_vol > 0 and candle["volume"] < avg_vol * VOLUME_MULTIPLIER:
                continue

            # ── Trend filter: EMA20 alignment ────────────────────────────────
            close = candle["close"]
            if close > oh + buffer:
                if ema20_above >= 2:   # bullish trend bias or neutral
                    signal       = "BUY_CALL"
                    entry_candle = candle
                    break
                else:
                    result["skip_reason"] = "Trend mismatch (bearish trend, bullish breakout)"
                    return result

            elif close < ol - buffer:
                if ema20_above <= 3:   # bearish trend bias or neutral
                    signal       = "BUY_PUT"
                    entry_candle = candle
                    break
                else:
                    result["skip_reason"] = "Trend mismatch (bullish trend, bearish breakout)"
                    return result

        if entry_candle is None:
            result["skip_reason"] = "No breakout by 10:30 AM"
            return result

        # ── Simulate trade ────────────────────────────────────────────────────
        result["traded"]      = True
        result["signal"]      = signal
        result["entry_time"]  = entry_candle["time"]
        result["entry_nifty"] = entry_candle["close"]
        result["spread_cost"] = spread_cost

        entry_dt_str = f"{trade_date} {entry_candle['time']}"
        entry_dt     = datetime.strptime(entry_dt_str, "%Y-%m-%d %H:%M")

        # Simulate exit: walk remaining candles
        total_units       = LOT_SIZE
        remaining_units   = total_units
        realised_pnl      = 0.0
        l1_taken          = False
        sl_value          = spread_cost * SL_PCT
        exit_reason       = "SQUAREOFF"
        exit_nifty        = entry_candle["close"]
        exit_time_str     = SQUAREOFF_TIME

        for candle in candles:
            if candle["time"] <= entry_candle["time"]:
                continue

            if candle["time"] >= SQUAREOFF_TIME:
                exit_reason   = "SQUAREOFF"
                exit_nifty    = candle["close"]
                exit_time_str = SQUAREOFF_TIME
                break

            # Compute Nifty move in trade direction
            if signal == "BUY_CALL":
                move = candle["close"] - result["entry_nifty"]
            else:
                move = result["entry_nifty"] - candle["close"]

            # Hours held
            candle_dt   = datetime.strptime(f"{trade_date} {candle['time']}", "%Y-%m-%d %H:%M")
            hours_held  = (candle_dt - entry_dt).seconds / 3600.0

            current_val = OptionModel.spread_value(spread_cost, move, hours_held)

            # ── Stop loss check ───────────────────────────────────────────────
            if current_val <= sl_value:
                exit_reason = "STOP_LOSS"
                exit_nifty  = candle["close"]
                exit_time_str = candle["time"]
                loss_on_remaining = (current_val - spread_cost) * remaining_units
                realised_pnl += loss_on_remaining
                remaining_units = 0
                break

            # ── Structural SL: price back inside range ─────────────────────────
            if signal == "BUY_CALL" and candle["close"] < oh:
                exit_reason  = "STRUCTURAL_SL"
                exit_nifty   = candle["close"]
                exit_time_str = candle["time"]
                loss_on_remaining = (current_val - spread_cost) * remaining_units
                realised_pnl += loss_on_remaining
                remaining_units = 0
                break

            if signal == "BUY_PUT" and candle["close"] > ol:
                exit_reason  = "STRUCTURAL_SL"
                exit_nifty   = candle["close"]
                exit_time_str = candle["time"]
                loss_on_remaining = (current_val - spread_cost) * remaining_units
                realised_pnl += loss_on_remaining
                remaining_units = 0
                break

            # ── Level 1 exit at 1× ATR ────────────────────────────────────────
            if not l1_taken and move >= atr20 * L1_ATR_MULTIPLE:
                l1_units  = int(total_units * L1_EXIT_PCT)
                l1_profit = (current_val - spread_cost) * l1_units
                realised_pnl   += l1_profit
                remaining_units -= l1_units
                l1_taken        = True
                sl_value        = spread_cost  # move SL to breakeven

            # ── Level 3 exit at 1.8× ATR ──────────────────────────────────────
            if move >= atr20 * L3_ATR_MULTIPLE:
                exit_reason  = "TARGET_L3"
                exit_nifty   = candle["close"]
                exit_time_str = candle["time"]
                profit_remaining = (current_val - spread_cost) * remaining_units
                realised_pnl    += profit_remaining
                remaining_units  = 0
                break

        # Final exit of remaining units at squareoff or last candle
        if remaining_units > 0:
            last_candle = candles[-1]
            last_move   = (last_candle["close"] - result["entry_nifty"]
                           if signal == "BUY_CALL"
                           else result["entry_nifty"] - last_candle["close"])
            total_hours = (datetime.strptime(f"{trade_date} {last_candle['time']}", "%Y-%m-%d %H:%M")
                           - entry_dt).seconds / 3600.0
            final_val    = OptionModel.spread_value(spread_cost, last_move, total_hours)
            realised_pnl += (final_val - spread_cost) * remaining_units
            exit_nifty    = last_candle["close"]
            exit_time_str = last_candle["time"]

        gross_pnl = realised_pnl
        net_pnl   = gross_pnl - BROKERAGE_PER_TRADE

        result.update({
            "exit_reason": exit_reason,
            "exit_nifty":  exit_nifty,
            "exit_time":   exit_time_str,
            "gross_pnl":   round(gross_pnl, 2),
            "net_pnl":     round(net_pnl, 2),
        })
        return result

    # ─── STATS ────────────────────────────────────────────────────────────────

    def _compute_stats(self, total_pnl: float, total_trades: int, skip_days: int) -> dict:
        if total_trades == 0:
            print("No trades to analyse.")
            return {}

        wins    = [t for t in self.trades if t["net_pnl"] > 0]
        losses  = [t for t in self.trades if t["net_pnl"] <= 0]
        win_rate = len(wins) / total_trades * 100

        avg_win  = sum(t["net_pnl"] for t in wins)  / len(wins)  if wins   else 0
        avg_loss = sum(t["net_pnl"] for t in losses) / len(losses) if losses else 0

        profit_factor = (
            (sum(t["net_pnl"] for t in wins) /
             abs(sum(t["net_pnl"] for t in losses)))
            if losses and avg_loss != 0 else float("inf")
        )

        expectancy = (win_rate / 100 * avg_win) + ((1 - win_rate / 100) * avg_loss)

        # Max drawdown
        running  = 0.0
        peak     = 0.0
        max_dd   = 0.0
        for t in self.trades:
            running += t["net_pnl"]
            peak     = max(peak, running)
            max_dd   = min(max_dd, running - peak)

        # Exit reason breakdown
        exit_counts = {}
        for t in self.trades:
            er = t["exit_reason"]
            exit_counts[er] = exit_counts.get(er, 0) + 1

        # Consecutive loss streaks
        max_streak   = 0
        cur_streak   = 0
        for t in self.trades:
            if t["net_pnl"] <= 0:
                cur_streak += 1
                max_streak  = max(max_streak, cur_streak)
            else:
                cur_streak  = 0

        stats = {
            "period":         f"{self.results[0]['date']} → {self.results[-1]['date']}",
            "total_days":     len(self.results),
            "traded_days":    total_trades,
            "skip_days":      skip_days,
            "skip_rate_pct":  round(skip_days / len(self.results) * 100, 1),
            "win_rate_pct":   round(win_rate, 1),
            "total_wins":     len(wins),
            "total_losses":   len(losses),
            "avg_win":        round(avg_win, 2),
            "avg_loss":       round(avg_loss, 2),
            "profit_factor":  round(profit_factor, 2),
            "expectancy":     round(expectancy, 2),
            "total_net_pnl":  round(total_pnl, 2),
            "max_drawdown":   round(max_dd, 2),
            "max_loss_streak": max_streak,
            "exit_reasons":   exit_counts,
        }

        self._print_report(stats)
        self._save_results(stats)
        return stats

    def _print_report(self, stats: dict):
        print(f"\n{'═'*54}")
        print(f"  BACKTEST REPORT — Strategy V2.0")
        print(f"  {stats['period']}")
        print(f"{'═'*54}")
        print(f"  Total days          : {stats['total_days']}")
        print(f"  Traded days         : {stats['traded_days']}")
        print(f"  Skipped days        : {stats['skip_days']} ({stats['skip_rate_pct']}%)")
        print(f"  Win rate            : {stats['win_rate_pct']}%")
        print(f"  Avg winner          : ₹{stats['avg_win']:,.0f}")
        print(f"  Avg loser           : ₹{stats['avg_loss']:,.0f}")
        print(f"  Profit factor       : {stats['profit_factor']:.2f}")
        print(f"  Expectancy/trade    : ₹{stats['expectancy']:,.0f}")
        print(f"  Total net P&L       : ₹{stats['total_net_pnl']:,.0f}")
        print(f"  Max drawdown        : ₹{stats['max_drawdown']:,.0f}")
        print(f"  Max loss streak     : {stats['max_loss_streak']} trades")
        print(f"\n  Exit reasons:")
        for reason, count in stats['exit_reasons'].items():
            print(f"    {reason:20s}: {count}")
        print(f"{'═'*54}")

        # Verdict
        pf   = stats["profit_factor"]
        wr   = stats["win_rate_pct"]
        exp  = stats["expectancy"]
        verdict = (
            "🟢 PROMISING — proceed to paper trading"  if pf >= 1.5 and wr >= 48 else
            "🟡 BORDERLINE — needs filter improvement" if pf >= 1.2 else
            "🔴 POOR — do not trade live"
        )
        print(f"\n  Verdict: {verdict}")
        print(f"{'═'*54}\n")

    def _save_results(self, stats: dict):
        """Saves trade-level results to CSV and summary to JSON."""
        os.makedirs("logs", exist_ok=True)
        today = date.today().strftime("%Y%m%d")

        # CSV
        csv_file = f"logs/backtest_{today}.csv"
        import csv
        with open(csv_file, "w", newline="") as f:
            if self.trades:
                writer = csv.DictWriter(f, fieldnames=self.trades[0].keys())
                writer.writeheader()
                writer.writerows(self.trades)

        # JSON summary
        json_file = f"logs/backtest_{today}_summary.json"
        with open(json_file, "w") as f:
            json.dump(stats, f, indent=2)

        print(f"  Results saved → {csv_file}")
        print(f"  Summary saved → {json_file}\n")

    # ─── DATA FETCHING ────────────────────────────────────────────────────────

    def _fetch_daily_candles(self, start: date, end: date) -> list:
        if self.groww is None:
            return self._synthetic_daily_candles(start, end)
        try:
            resp = self.groww.get_historical_candle_data(
                trading_symbol="NIFTY",
                exchange=self.groww.EXCHANGE_NSE,
                segment=self.groww.SEGMENT_CASH,
                start_time=start.strftime("%Y-%m-%d 09:15:00"),
                end_time=end.strftime("%Y-%m-%d 15:30:00"),
                interval_in_minutes=375
            )
            return resp.get("candles", [])
        except Exception as e:
            logger.error(f"Daily candle fetch failed: {e}")
            return []

    def _fetch_5min_candles(self, start: date, end: date) -> list:
        """Fetches 5-min candles in monthly chunks (API limit)."""
        if self.groww is None:
            return self._synthetic_5min_candles(start, end)

        all_candles = []
        chunk_start = start
        while chunk_start <= end:
            chunk_end = min(chunk_start + timedelta(days=28), end)
            try:
                resp = self.groww.get_historical_candle_data(
                    trading_symbol="NIFTY",
                    exchange=self.groww.EXCHANGE_NSE,
                    segment=self.groww.SEGMENT_CASH,
                    start_time=chunk_start.strftime("%Y-%m-%d 09:15:00"),
                    end_time=chunk_end.strftime("%Y-%m-%d 15:30:00"),
                    interval_in_minutes=5
                )
                all_candles.extend(resp.get("candles", []))
                print(f"  Fetched {chunk_start} → {chunk_end}: {len(resp.get('candles',[]))} candles")
            except Exception as e:
                logger.error(f"5-min fetch failed for {chunk_start}: {e}")
            chunk_start = chunk_end + timedelta(days=1)
        return all_candles

    def _group_by_date(self, candles: list) -> dict:
        """Groups raw candle list into {date: [candle_dicts]}."""
        grouped = {}
        for c in candles:
            ts, o, h, l, cl, vol = c
            dt   = datetime.fromtimestamp(int(ts))
            d    = dt.date()
            time = dt.strftime("%H:%M")
            if d not in grouped:
                grouped[d] = []
            grouped[d].append({
                "time": time, "open": float(o), "high": float(h),
                "low": float(l), "close": float(cl), "volume": float(vol)
            })
        return grouped

    def _compute_daily_metrics(self, daily_candles: list) -> dict:
        """Computes ATR20, ADX, EMA20, prev_close for each day."""
        metrics = {}
        candles = []

        for c in daily_candles:
            ts, o, h, l, cl, vol = c
            dt = datetime.fromtimestamp(int(ts)).date()
            candles.append({"date": dt, "open": float(o), "high": float(h),
                             "low": float(l), "close": float(cl), "volume": float(vol)})

        for i in range(25, len(candles)):
            window = candles[i-25:i+1]
            today_c = candles[i]

            # ATR20
            trs = []
            for j in range(1, len(window)):
                prev = window[j-1]["close"]
                cur  = window[j]
                tr   = max(cur["high"]-cur["low"],
                           abs(cur["high"]-prev),
                           abs(cur["low"]-prev))
                trs.append(tr)
            atr20 = sum(trs[-20:]) / 20 if len(trs) >= 20 else 0

            # Simplified ADX (use range/ATR as proxy)
            recent_ranges = [abs(c["high"]-c["low"]) for c in window[-14:]]
            adx_proxy = (sum(recent_ranges) / len(recent_ranges)) / atr20 * 20 if atr20 > 0 else 0

            # EMA20 sessions above count (last 5 days)
            closes  = [c["close"] for c in window]
            ema20   = sum(closes[-20:]) / 20
            above5  = sum(1 for c in window[-5:] if c["close"] > ema20)

            # Approximate VIX from 10-day realized vol (annualised)
            # Real Nifty VIX typically 11-20; we use realized vol as a proxy
            ret_10 = [
                abs(window[j]["close"] / window[j-1]["close"] - 1)
                for j in range(max(1, len(window)-10), len(window))
            ]
            realized_vol  = (sum(r**2 for r in ret_10) / len(ret_10)) ** 0.5 * math.sqrt(252) * 100
            # VIX is typically 1.1–1.4× realized vol; clamp to realistic range
            vix_proxy = min(max(realized_vol * 1.2, 11.0), 28.0)

            # Avg 5-min volume (approximated from daily volume)
            avg_daily_vol  = today_c["volume"]
            avg_5min_vol   = avg_daily_vol / 75  # ~75 five-min candles per day

            tomorrow = (today_c["date"] + timedelta(days=1))
            metrics[tomorrow] = {
                "atr20":          round(atr20, 2),
                "adx":            round(adx_proxy, 2),
                "ema20_above":    above5,
                "prev_close":     today_c["close"],
                "today_open":     today_c["open"],   # gap uses daily open vs prev close
                "vix_approx":     round(min(max(vix_proxy, 8), 30), 2),
                "avg_5min_volume": round(avg_5min_vol, 0),
            }

        return metrics

    # ─── SYNTHETIC DATA (for --no-auth testing) ───────────────────────────────

    def _synthetic_daily_candles(self, start: date, end: date) -> list:
        """Generates realistic synthetic Nifty daily candles for testing."""
        import random
        random.seed(42)
        candles = []
        price   = 22000.0
        dt      = start
        while dt <= end:
            if dt.weekday() < 5:  # Mon–Fri
                # Realistic Nifty daily move: ~0.6% std dev
                pct_change = random.gauss(0.0002, 0.006)
                pct_change = max(-0.025, min(0.025, pct_change))   # cap at ±2.5%
                # Gap from prev close to open: small, max ~0.4%
                gap_pct = random.gauss(0, 0.002)
                gap_pct = max(-0.004, min(0.004, gap_pct))
                o = round(price * (1 + gap_pct), 2)
                c = round(o * (1 + pct_change), 2)
                # Intraday range: typically 0.8–1.5% of price
                intraday_range = abs(random.gauss(0, 0.008)) * price
                h = round(max(o, c) + intraday_range * 0.4, 2)
                l = round(min(o, c) - intraday_range * 0.4, 2)
                ts  = int(datetime(dt.year, dt.month, dt.day, 9, 15).timestamp())
                vol = int(random.gauss(2_000_000, 300_000))
                candles.append([ts, o, h, l, c, vol])
                price = c
            dt += timedelta(days=1)
        return candles

    def _synthetic_5min_candles(self, start: date, end: date) -> list:
        """
        Generates synthetic 5-min candles for testing without API.
        Price is continuous across days (no random reset per day).
        Gaps from prev-close to next-open are realistic (< 0.4%).
        """
        import random
        random.seed(99)
        candles = []
        dt    = start
        price = 22000.0   # single continuous price series

        while dt <= end:
            if dt.weekday() < 5 and dt.weekday() != 3:  # skip Thu + weekends
                # Slight gap on open (realistic: ±0.2%)
                gap   = random.gauss(0, 0.002)
                gap   = max(-0.004, min(0.004, gap))
                price = round(price * (1 + gap), 2)

                # Daily directional drift
                daily_drift = random.gauss(0, 0.004)

                current_dt = datetime(dt.year, dt.month, dt.day, 9, 15)
                end_dt     = datetime(dt.year, dt.month, dt.day, 15, 30)

                while current_dt < end_dt:
                    is_opening = current_dt.hour == 9 and current_dt.minute <= 45
                    vol_factor = 2.0 if is_opening else 1.0

                    std_5min = price * 0.0010 * vol_factor
                    drift    = daily_drift * price * 0.02
                    change   = random.gauss(drift, std_5min)

                    o    = round(price, 2)
                    c    = round(price + change, 2)
                    wick = abs(random.gauss(0, std_5min * 0.3))
                    h    = round(max(o, c) + wick, 2)
                    l    = round(min(o, c) - wick, 2)
                    ts   = int(current_dt.timestamp())
                    base_vol = 80_000 if is_opening else 40_000
                    vol  = int(abs(random.gauss(base_vol, base_vol * 0.25)))

                    candles.append([ts, o, h, l, c, max(vol, 5000)])
                    price       = c
                    current_dt += timedelta(minutes=5)

            dt += timedelta(days=1)
        return candles


# ─── ENTRY POINT ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Strategy V2.0 Backtester")
    parser.add_argument("--months",   type=int,  default=6,     help="Months to backtest (default: 6)")
    parser.add_argument("--from",     dest="from_date", default=None, help="Start date YYYY-MM-DD")
    parser.add_argument("--to",       dest="to_date",   default=None, help="End date YYYY-MM-DD")
    parser.add_argument("--no-auth",  action="store_true",            help="Use synthetic data (no Groww API)")
    args = parser.parse_args()

    # Determine date range
    end_date   = date.today() - timedelta(days=1)
    start_date = end_date - timedelta(days=30 * args.months)

    if args.from_date:
        start_date = date.fromisoformat(args.from_date)
    if args.to_date:
        end_date = date.fromisoformat(args.to_date)

    print(f"\n{'═'*54}")
    print(f"  STRATEGY V2.0 BACKTESTER")
    print(f"  Period: {start_date} → {end_date}")
    print(f"  Mode: {'SYNTHETIC DATA' if args.no_auth else 'GROWW API'}")
    print(f"{'═'*54}")

    # Get Groww client unless --no-auth
    groww = None
    if not args.no_auth:
        try:
            import sys; sys.path.insert(0, ".")
            from auth import AuthManager
            auth = AuthManager()
            if auth.refresh_if_needed():
                groww = auth.get_client()
                print("✅ Authenticated with Groww API")
            else:
                print("❌ Auth failed — use --no-auth for synthetic data")
                sys.exit(1)
        except Exception as e:
            print(f"❌ Auth error: {e} — use --no-auth for synthetic data")
            sys.exit(1)

    bt = Backtester(groww=groww)
    bt.run(start_date, end_date)


if __name__ == "__main__":
    import argparse
    main()
