"""
paper_analyzer.py — Paper Trade Performance Analyzer
Reads the paper trade log (logs/trades.csv) and produces:
  - Full performance statistics
  - Monthly breakdown
  - Exit reason breakdown
  - Drawdown analysis
  - Go/no-go verdict for live trading

Usage:
    python paper_analyzer.py
    python paper_analyzer.py --csv logs/trades.csv
    python paper_analyzer.py --min-trades 30    # require at least 30 trades
"""

import argparse
import csv
import os
import sys
from collections import defaultdict
from datetime import datetime
from typing import Optional

LIVE_TRADING_THRESHOLDS = {
    "min_trades":        20,    # minimum trades to draw conclusions
    "min_win_rate":      45.0,  # % win rate required
    "min_profit_factor": 1.35,  # minimum profit factor
    "min_expectancy":    500,   # ₹ minimum expectancy per trade
    "max_drawdown_pct":  20.0,  # % of capital — max acceptable drawdown
    "max_loss_streak":   5,     # consecutive losses limit
}

CAPITAL = 50_000  # from config — used for drawdown % calculation


def load_trades(csv_path: str) -> list[dict]:
    """Loads all traded days (not skips) from the CSV."""
    if not os.path.exists(csv_path):
        print(f"❌ Trade log not found: {csv_path}")
        sys.exit(1)

    trades = []
    with open(csv_path, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("signal", "SKIP") != "SKIP":
                try:
                    row["realised_pnl"] = float(row.get("realised_pnl", 0))
                    trades.append(row)
                except (ValueError, KeyError):
                    continue
    return trades


def load_all_days(csv_path: str) -> list[dict]:
    """Loads all days including skips."""
    days = []
    with open(csv_path, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            days.append(row)
    return days


def compute_stats(trades: list[dict], all_days: list[dict]) -> dict:
    if not trades:
        return {}

    pnls      = [t["realised_pnl"] for t in trades]
    wins      = [p for p in pnls if p > 0]
    losses    = [p for p in pnls if p <= 0]

    win_rate  = len(wins) / len(pnls) * 100 if pnls else 0
    avg_win   = sum(wins) / len(wins)   if wins   else 0
    avg_loss  = sum(losses) / len(losses) if losses else 0
    total_pnl = sum(pnls)

    profit_factor = (
        sum(wins) / abs(sum(losses))
        if losses and sum(losses) != 0 else float("inf")
    )
    expectancy = (win_rate / 100 * avg_win) + ((1 - win_rate / 100) * avg_loss)

    # Drawdown
    running = 0.0
    peak    = 0.0
    max_dd  = 0.0
    for p in pnls:
        running += p
        peak     = max(peak, running)
        max_dd   = min(max_dd, running - peak)

    # Max consecutive loss streak
    max_streak = cur_streak = 0
    for p in pnls:
        if p <= 0:
            cur_streak += 1
            max_streak  = max(max_streak, cur_streak)
        else:
            cur_streak  = 0

    # Exit reason breakdown
    exit_counts = defaultdict(int)
    for t in trades:
        exit_counts[t.get("exit_reason", "UNKNOWN")] += 1

    # Monthly breakdown
    monthly = defaultdict(list)
    for t in trades:
        try:
            month = t["date"][:7]   # "YYYY-MM"
            monthly[month].append(t["realised_pnl"])
        except (KeyError, IndexError):
            pass

    monthly_summary = {}
    for month, mpnls in sorted(monthly.items()):
        mwins = [p for p in mpnls if p > 0]
        monthly_summary[month] = {
            "trades":   len(mpnls),
            "wins":     len(mwins),
            "win_rate": round(len(mwins) / len(mpnls) * 100, 1) if mpnls else 0,
            "total_pnl": round(sum(mpnls), 2),
        }

    # Skip analysis
    total_days  = len(all_days)
    traded_days = len(trades)
    skipped     = total_days - traded_days
    skip_rate   = skipped / total_days * 100 if total_days else 0

    return {
        "total_trades":    len(trades),
        "total_days":      total_days,
        "skipped_days":    skipped,
        "skip_rate_pct":   round(skip_rate, 1),
        "wins":            len(wins),
        "losses":          len(losses),
        "win_rate_pct":    round(win_rate, 1),
        "avg_win":         round(avg_win, 2),
        "avg_loss":        round(avg_loss, 2),
        "profit_factor":   round(profit_factor, 2),
        "expectancy":      round(expectancy, 2),
        "total_pnl":       round(total_pnl, 2),
        "max_drawdown":    round(max_dd, 2),
        "max_dd_pct":      round(abs(max_dd) / CAPITAL * 100, 1),
        "max_loss_streak": max_streak,
        "exit_reasons":    dict(exit_counts),
        "monthly":         monthly_summary,
    }


def verdict(stats: dict, min_trades: int) -> tuple[bool, list[str]]:
    """
    Returns (go_live, list_of_issues).
    go_live = True only if ALL thresholds are met.
    """
    issues = []
    t      = LIVE_TRADING_THRESHOLDS

    if stats["total_trades"] < min_trades:
        issues.append(
            f"Not enough trades: {stats['total_trades']} < {min_trades} required"
        )
    if stats["win_rate_pct"] < t["min_win_rate"]:
        issues.append(
            f"Win rate too low: {stats['win_rate_pct']}% < {t['min_win_rate']}%"
        )
    if stats["profit_factor"] < t["min_profit_factor"]:
        issues.append(
            f"Profit factor too low: {stats['profit_factor']} < {t['min_profit_factor']}"
        )
    if stats["expectancy"] < t["min_expectancy"]:
        issues.append(
            f"Expectancy too low: ₹{stats['expectancy']} < ₹{t['min_expectancy']}/trade"
        )
    if stats["max_dd_pct"] > t["max_drawdown_pct"]:
        issues.append(
            f"Drawdown too large: {stats['max_dd_pct']}% > {t['max_drawdown_pct']}%"
        )
    if stats["max_loss_streak"] > t["max_loss_streak"]:
        issues.append(
            f"Loss streak too long: {stats['max_loss_streak']} > {t['max_loss_streak']} consecutive"
        )

    return (len(issues) == 0), issues


def print_report(stats: dict, issues: list[str], go_live: bool):
    w = 56
    print("\n" + "═" * w)
    print("  PAPER TRADE PERFORMANCE ANALYSIS")
    print("═" * w)

    print(f"\n{'OVERVIEW':}")
    print("─" * 40)
    print(f"  Total days logged   : {stats['total_days']}")
    print(f"  Traded days         : {stats['total_trades']}")
    print(f"  Skipped days        : {stats['skipped_days']} ({stats['skip_rate_pct']}%)")
    print(f"  Total net P&L       : ₹{stats['total_pnl']:,.2f}")

    print(f"\n{'TRADE STATISTICS':}")
    print("─" * 40)
    print(f"  Win rate            : {stats['win_rate_pct']:.1f}%   "
          f"({'✅' if stats['win_rate_pct'] >= 45 else '❌'} need ≥45%)")
    print(f"  Avg winner          : ₹{stats['avg_win']:,.0f}")
    print(f"  Avg loser           : ₹{stats['avg_loss']:,.0f}")
    print(f"  Profit factor       : {stats['profit_factor']:.2f}   "
          f"({'✅' if stats['profit_factor'] >= 1.35 else '❌'} need ≥1.35)")
    print(f"  Expectancy/trade    : ₹{stats['expectancy']:,.0f}   "
          f"({'✅' if stats['expectancy'] >= 500 else '❌'} need ≥₹500)")

    print(f"\n{'RISK METRICS':}")
    print("─" * 40)
    print(f"  Max drawdown        : ₹{abs(stats['max_drawdown']):,.0f} "
          f"({stats['max_dd_pct']}% of capital)   "
          f"({'✅' if stats['max_dd_pct'] <= 20 else '❌'} need ≤20%)")
    print(f"  Max loss streak     : {stats['max_loss_streak']} trades   "
          f"({'✅' if stats['max_loss_streak'] <= 5 else '❌'} need ≤5)")

    print(f"\n{'EXIT REASON BREAKDOWN':}")
    print("─" * 40)
    total_trades = stats["total_trades"] or 1
    for reason, count in sorted(stats["exit_reasons"].items(), key=lambda x: -x[1]):
        pct = count / total_trades * 100
        print(f"  {reason:22s}: {count:3d} ({pct:.0f}%)")

    print(f"\n{'MONTHLY BREAKDOWN':}")
    print("─" * 40)
    print(f"  {'Month':8}  {'Trades':7} {'Win%':6} {'P&L':>12}")
    for month, m in stats["monthly"].items():
        icon = "🟢" if m["total_pnl"] > 0 else "🔴"
        print(f"  {month:8}  {m['trades']:7}  {m['win_rate']:5.1f}%  {icon} ₹{m['total_pnl']:>9,.0f}")

    print(f"\n{'LIVE TRADING VERDICT':}")
    print("─" * 40)
    if go_live:
        print("  🟢 ALL THRESHOLDS MET — Ready for live trading")
        print("  ✅ Start with 1 lot. Review again after 3 months live.")
    else:
        print("  🔴 NOT READY — Issues to resolve:")
        for issue in issues:
            print(f"     ✗ {issue}")
        print()
        print("  Continue paper trading until all issues are resolved.")
        print("  Do NOT go live until every threshold passes.")

    print("═" * w + "\n")


def main():
    parser = argparse.ArgumentParser(description="Paper Trade Analyzer")
    parser.add_argument("--csv",        default="logs/trades.csv",
                        help="Path to trade log CSV")
    parser.add_argument("--min-trades", type=int, default=20,
                        help="Minimum trades required for verdict (default: 20)")
    args = parser.parse_args()

    print(f"📂 Loading trades from: {args.csv}")
    trades   = load_trades(args.csv)
    all_days = load_all_days(args.csv)

    if not trades:
        print("⚠️  No traded days found in log. Keep paper trading.")
        sys.exit(0)

    print(f"✅ Loaded {len(trades)} trades across {len(all_days)} logged days")

    stats    = compute_stats(trades, all_days)
    go_live, issues = verdict(stats, args.min_trades)
    print_report(stats, issues, go_live)


if __name__ == "__main__":
    main()
