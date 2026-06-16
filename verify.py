"""
verify.py — Pre-Live System Verification
Run this BEFORE going live to confirm every system component works.

Checks:
  1. Groww API authentication
  2. Live Nifty + BankNifty price feeds
  3. Option chain fetch + symbol format inspection
  4. Historical candle data (ATR20 computation)
  5. Volume cache build
  6. Risk manager state
  7. Log directory writability
  8. Config sanity

Usage:
    python verify.py

All checks must show ✅ before running main.py with real money.
"""

import os
import sys
import logging
from datetime import date, timedelta

logging.basicConfig(
    level=logging.WARNING,   # suppress routine logs during verify
    format="%(levelname)s | %(message)s"
)

# Use a simple print-based UI for clarity
def ok(msg):  print(f"  ✅  {msg}")
def fail(msg): print(f"  ❌  {msg}")
def warn(msg): print(f"  ⚠️   {msg}")
def header(msg): print(f"\n── {msg} {'─'*(50-len(msg))}")


def main():
    print("=" * 54)
    print("  NIFTY ORB AGENT — PRE-LIVE VERIFICATION")
    print(f"  {date.today().strftime('%d %b %Y')}")
    print("=" * 54)

    passed = 0
    failed = 0

    # ─── 1. CONFIG SANITY ─────────────────────────────────────────────────────
    header("Config")
    try:
        import config
        ok(f"CAPITAL = ₹{config.CAPITAL:,.0f}")
        ok(f"Entry window: {config.ENTRY_WINDOW_START} – {config.ENTRY_WINDOW_END}")
        ok(f"SL = {config.SL_PREMIUM_PCT*100:.0f}% of premium")
        ok(f"L1 target = {config.LEVEL1_ATR_MULTIPLE}× ATR | L3 = {config.LEVEL3_ATR_MULTIPLE}× ATR")
        ok(f"Max risk/trade = {config.MAX_RISK_PER_TRADE_PCT*100:.1f}% = ₹{config.CAPITAL*config.MAX_RISK_PER_TRADE_PCT:,.0f}")

        if not config.GROWW_TOTP_TOKEN:
            fail("GROWW_TOTP_TOKEN is not set in .env")
            failed += 1
        else:
            ok("GROWW_TOTP_TOKEN is set")
            passed += 1

        if not config.GROWW_TOTP_SECRET:
            fail("GROWW_TOTP_SECRET is not set in .env")
            failed += 1
        else:
            ok("GROWW_TOTP_SECRET is set")
            passed += 1

    except Exception as e:
        fail(f"Config import failed: {e}")
        failed += 1

    # ─── 2. AUTHENTICATION ────────────────────────────────────────────────────
    header("Authentication")
    groww = None
    try:
        from auth import AuthManager
        auth = AuthManager()
        if auth.refresh_if_needed():
            groww = auth.get_client()
            ok("Groww API authentication successful")
            passed += 1
        else:
            fail("Authentication failed — check TOTP credentials in .env")
            failed += 1
    except Exception as e:
        fail(f"Auth error: {e}")
        failed += 1

    if groww is None:
        print("\n❌ Cannot continue without valid auth. Fix auth first.")
        _print_summary(passed, failed)
        return

    # ─── 3. LIVE PRICE FEEDS ──────────────────────────────────────────────────
    header("Live Price Feeds")
    try:
        nifty_quote = groww.get_ltp(
            trading_symbol="NIFTY",
            exchange=groww.EXCHANGE_NSE,
            segment=groww.SEGMENT_CASH
        )
        nifty_ltp = nifty_quote.get("ltp")
        if nifty_ltp:
            ok(f"Nifty LTP = {nifty_ltp}")
            passed += 1
        else:
            fail("Nifty LTP returned empty")
            failed += 1
    except Exception as e:
        fail(f"Nifty LTP failed: {e}")
        failed += 1

    try:
        bnf_quote = groww.get_ltp(
            trading_symbol="BANKNIFTY",
            exchange=groww.EXCHANGE_NSE,
            segment=groww.SEGMENT_CASH
        )
        bnf_ltp = bnf_quote.get("ltp")
        if bnf_ltp:
            ok(f"BankNifty LTP = {bnf_ltp}")
            passed += 1
        else:
            fail("BankNifty LTP returned empty")
            failed += 1
    except Exception as e:
        fail(f"BankNifty LTP failed: {e}")
        failed += 1

    # ─── 4. HISTORICAL CANDLE DATA ────────────────────────────────────────────
    header("Historical Data + ATR20")
    try:
        from data import MarketData
        md = MarketData(groww)
        success = md.fetch_atr20()
        if success and md.atr20:
            ok(f"ATR20 = {md.atr20} pts")
            ok(f"ADX (prev day) = {md.adx_value}")
            ok(f"Breakout buffer = {md.breakout_buffer} pts")
            ok(f"EMA20 above count = {md.ema20_above_count}/5 sessions")
            passed += 4
        else:
            fail("ATR20 computation failed")
            failed += 1
    except Exception as e:
        fail(f"Historical data failed: {e}")
        failed += 1

    # ─── 5. OPTION CHAIN + SYMBOL FORMAT ─────────────────────────────────────
    header("Option Chain + Symbol Format")
    try:
        today    = date.today()
        days_to_thu = (3 - today.weekday()) % 7
        if days_to_thu == 0:
            days_to_thu = 7
        expiry   = (today + timedelta(days=days_to_thu)).strftime("%Y-%m-%d")

        chain_resp = groww.get_option_chain(
            exchange=groww.EXCHANGE_NSE,
            underlying="NIFTY",
            expiry_date=expiry
        )
        chain = chain_resp.get("data", [])
        if chain:
            ok(f"Option chain fetched: {len(chain)} strikes for expiry {expiry}")
            passed += 1

            # Show first few symbols so you can validate format
            print()
            print("    ┌─ SAMPLE OPTION SYMBOLS (verify format in strike_selector.py) ─")
            for item in chain[:6]:
                sym    = item.get("trading_symbol", "?")
                strike = item.get("strike_price", "?")
                otype  = item.get("option_type", "?")
                ltp    = item.get("ltp", "?")
                print(f"    │  {sym:30s}  strike={strike}  type={otype}  ltp={ltp}")
            print("    └" + "─"*60)
            print()

            warn("ACTION REQUIRED: Compare symbols above against _build_symbol() in strike_selector.py")
            warn("If format differs, update the _build_symbol() method before going live")
        else:
            fail(f"Option chain empty for expiry {expiry}")
            failed += 1
    except Exception as e:
        fail(f"Option chain failed: {e}")
        failed += 1

    # ─── 6. VOLUME CACHE ──────────────────────────────────────────────────────
    header("Volume Cache")
    try:
        from volume_cache import VolumeCache
        vc = VolumeCache(groww)
        success = vc.build()
        if success:
            avg_945 = vc.get_avg_volume("09:45")
            avg_1000 = vc.get_avg_volume("10:00")
            ok(f"Volume cache built | 09:45 avg={avg_945:,.0f} | 10:00 avg={avg_1000:,.0f}")
            passed += 1
        else:
            warn("Volume cache build failed — volume filter will use fallback")
    except Exception as e:
        warn(f"Volume cache error: {e}")

    # ─── 7. RISK MANAGER STATE ────────────────────────────────────────────────
    header("Risk Manager")
    try:
        from risk_manager import RiskManager
        rm = RiskManager()
        stats = rm.get_stats()
        allowed, reason = rm.is_trading_allowed()
        ok(f"Risk state loaded: {stats['total_trades']} trades | {stats['win_rate_pct']:.1f}% win rate")
        if allowed:
            ok(f"Trading allowed: {reason}")
        else:
            warn(f"Trading currently blocked: {reason}")
        passed += 1
    except Exception as e:
        fail(f"Risk manager error: {e}")
        failed += 1

    # ─── 8. LOG DIRECTORY ─────────────────────────────────────────────────────
    header("Log Directory")
    try:
        os.makedirs("logs", exist_ok=True)
        test_file = "logs/.write_test"
        with open(test_file, "w") as f:
            f.write("test")
        os.remove(test_file)
        ok("logs/ directory is writable")
        passed += 1
    except Exception as e:
        fail(f"Cannot write to logs/: {e}")
        failed += 1

    # ─── 9. POSITIONS CHECK ───────────────────────────────────────────────────
    header("Open Positions Check")
    try:
        positions = groww.get_positions_for_user(segment=groww.SEGMENT_FNO)
        pos_list  = positions.get("positions", [])
        open_pos  = [p for p in pos_list if int(p.get("quantity", 0)) != 0]
        if open_pos:
            warn(f"{len(open_pos)} open F&O position(s) found — ensure these are not from a stuck trade")
            for p in open_pos:
                print(f"    → {p.get('trading_symbol')} qty={p.get('quantity')}")
        else:
            ok("No open F&O positions (clean slate)")
            passed += 1
    except Exception as e:
        warn(f"Could not check positions: {e}")

    # ─── FINAL SUMMARY ────────────────────────────────────────────────────────
    _print_summary(passed, failed)


def _print_summary(passed, failed):
    total = passed + failed
    print("\n" + "═" * 54)
    print(f"  RESULT: {passed}/{total} checks passed | {failed} failed")
    if failed == 0:
        print("  🟢 All checks passed — system is READY")
        print("  Next: python main.py --paper  (4+ weeks paper first)")
    else:
        print("  🔴 Fix all ❌ failures before going live")
    print("═" * 54 + "\n")


if __name__ == "__main__":
    main()
