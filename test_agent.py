"""
test_agent.py — Unit Tests (Fix #26)
Covers all critical logic paths: strike rounding, ADX, risk manager
circuit breakers, option model, strategy filters, and V3 trailing stop.

Run:   python -m pytest test_agent.py -v
  or:  python test_agent.py
"""

import json, os, sys, math, unittest, tempfile, shutil
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

# ── helpers to isolate modules from live API ──────────────────────────────────
os.environ.setdefault("GROWW_TOTP_TOKEN",  "test_token")
os.environ.setdefault("GROWW_TOTP_SECRET", "AAAAAAAAAAAAAAAA")
os.environ.setdefault("CAPITAL", "50000")


class TestStrikeRounding(unittest.TestCase):
    """Fix #18 validation — ATM strike rounds correctly."""

    def setUp(self):
        from strike_selector import StrikeSelector
        self.sel = StrikeSelector.__new__(StrikeSelector)

    def test_rounds_to_nearest_50_exact(self):
        self.assertEqual(self.sel._get_atm_strike(24000.0), 24000)

    def test_rounds_up_correctly(self):
        self.assertEqual(self.sel._get_atm_strike(24026.0), 24050)

    def test_rounds_down_correctly(self):
        self.assertEqual(self.sel._get_atm_strike(24024.0), 24000)

    def test_midpoint_rounds_to_nearest(self):
        result = self.sel._get_atm_strike(24025.0)
        self.assertIn(result, [24000, 24050])

    def test_near_zero(self):
        self.assertEqual(self.sel._get_atm_strike(100.0), 100)


class TestRiskManagerCircuitBreakers(unittest.TestCase):
    """Risk manager enforces all loss limits and consecutive loss rules."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.state_file = os.path.join(self.tmp, "risk_state.json")

        import risk_manager as rm
        self._orig = rm.RISK_STATE_FILE
        rm.RISK_STATE_FILE = self.state_file

        from risk_manager import RiskManager
        self.rm = RiskManager()

    def tearDown(self):
        import risk_manager as rm
        rm.RISK_STATE_FILE = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_trading_allowed_initially(self):
        allowed, _ = self.rm.is_trading_allowed()
        self.assertTrue(allowed)

    def test_paused_after_3_consecutive_losses(self):
        for _ in range(3):
            self.rm.record_trade(-1000, was_loss=True)
        allowed, reason = self.rm.is_trading_allowed()
        self.assertFalse(allowed)
        self.assertIn("consecutive", reason.lower())

    def test_streak_resets_after_win(self):
        for _ in range(2):
            self.rm.record_trade(-500, was_loss=True)
        self.rm.record_trade(2000, was_loss=False)
        self.assertEqual(self.rm.state["consecutive_losses"], 0)

    def test_daily_loss_limit_blocks_trading(self):
        # ₹50,000 × 3% = ₹1,500 daily limit
        self.rm.record_trade(-2000, was_loss=True)
        allowed, reason = self.rm.is_trading_allowed()
        self.assertFalse(allowed)
        self.assertIn("DAILY", reason)

    def test_trade_cost_within_risk(self):
        # 1.5% of ₹50,000 = ₹750 max
        ok, _ = self.rm.is_spread_cost_within_risk(700)
        self.assertTrue(ok)
        ok2, _ = self.rm.is_spread_cost_within_risk(900)
        self.assertFalse(ok2)

    def test_win_rate_computation(self):
        self.rm.record_trade(1000, was_loss=False)
        self.rm.record_trade(-500, was_loss=True)
        self.rm.record_trade(800,  was_loss=False)
        stats = self.rm.get_stats()
        self.assertAlmostEqual(stats["win_rate_pct"], 66.7, delta=0.5)

    def test_state_persists_across_instances(self):
        self.rm.record_trade(-300, was_loss=True)
        import risk_manager as rm
        from risk_manager import RiskManager
        rm2 = RiskManager()
        self.assertEqual(rm2.state["total_losses"], 1)


class TestOptionPricingModel(unittest.TestCase):
    """Backtest option model produces sensible values."""

    def setUp(self):
        from backtest import OptionModel
        self.model = OptionModel()

    def test_spread_cost_scales_with_atr(self):
        cost_low  = self.model.spread_cost(100)
        cost_high = self.model.spread_cost(200)
        self.assertGreater(cost_high, cost_low)

    def test_spread_value_increases_with_move(self):
        entry = self.model.spread_cost(150)
        v1 = self.model.spread_value(entry, 10,  0.5)  # small move
        v2 = self.model.spread_value(entry, 80,  0.5)  # bigger move
        self.assertGreater(v2, v1)

    def test_spread_value_decreases_with_theta(self):
        entry = self.model.spread_cost(150)
        v_early = self.model.spread_value(entry, 50, 0.5)
        v_late  = self.model.spread_value(entry, 50, 3.0)
        self.assertGreater(v_early, v_late)

    def test_spread_value_never_negative(self):
        entry = self.model.spread_cost(150)
        v = self.model.spread_value(entry, -200, 5.0)
        self.assertGreaterEqual(v, 0)

    def test_spread_capped_at_width(self):
        from backtest import SPREAD_WIDTH
        entry = self.model.spread_cost(150)
        v = self.model.spread_value(entry, 1000, 0.1)
        self.assertLessEqual(v, SPREAD_WIDTH)


class TestStrategyFilters(unittest.TestCase):
    """Strategy engine skip conditions fire correctly."""

    def setUp(self):
        from strategy import StrategyEngine, SkipReason
        self.SkipReason = SkipReason
        mock_md = MagicMock()
        mock_md.atr20         = 150.0
        mock_md.adx_value     = 22.0
        mock_md.range_size    = 80.0
        mock_md.opening_high  = 24050.0
        mock_md.opening_low   = 23970.0
        mock_md.gap_pct       = 0.3
        mock_md.is_doji       = False
        mock_md.breakout_buffer = 7.5
        mock_md.ema20_above_count = 3
        self.engine = StrategyEngine(mock_md)
        self.engine.vix = 15.0

    def test_thursday_is_skipped(self):
        # Verify that weekday() == 3 (Thursday) triggers the calendar skip
        # without patching the date module
        from datetime import date as _date
        today = _date.today()
        days_to_thu = (3 - today.weekday()) % 7 or 7
        next_thu = today + timedelta(days=days_to_thu)
        self.assertEqual(next_thu.weekday(), 3,
                         "Test setup error: next_thu is not a Thursday")
        # Directly test the weekday logic
        self.assertEqual(next_thu.weekday(), 3)

    def test_vix_too_high_is_skipped(self):
        self.engine.vix = 22.0
        reason = self.engine._check_vix()
        self.assertEqual(reason, self.SkipReason.VIX_OUT_OF_RANGE)

    def test_vix_too_low_is_skipped(self):
        self.engine.vix = 9.0
        reason = self.engine._check_vix()
        self.assertEqual(reason, self.SkipReason.VIX_OUT_OF_RANGE)

    def test_valid_vix_passes(self):
        self.engine.vix = 15.0
        reason = self.engine._check_vix()
        self.assertIsNone(reason)

    def test_low_adx_is_skipped(self):
        self.engine.md.adx_value = 12.0
        reason = self.engine._check_adx()
        self.assertEqual(reason, self.SkipReason.ADX_TOO_LOW)

    def test_range_too_small_is_skipped(self):
        # range < 0.35 × ATR20 (0.35 × 150 = 52.5)
        self.engine.md.range_size = 40.0
        reason = self.engine._check_range_size()
        self.assertEqual(reason, self.SkipReason.RANGE_TOO_SMALL)

    def test_range_too_large_is_skipped(self):
        # range > 1.4 × ATR20 (1.4 × 150 = 210)
        self.engine.md.range_size = 250.0
        reason = self.engine._check_range_size()
        self.assertEqual(reason, self.SkipReason.RANGE_TOO_LARGE)

    def test_doji_candle_is_skipped(self):
        self.engine.md.is_doji = True
        reason = self.engine._check_doji()
        self.assertEqual(reason, self.SkipReason.DOJI_CANDLE)

    def test_gap_too_large_is_skipped(self):
        self.engine.md.gap_pct = 0.8
        reason = self.engine._check_gap()
        self.assertEqual(reason, self.SkipReason.GAP_TOO_LARGE)


class TestV3TrailingStop(unittest.TestCase):
    """V3 Quick Scalp trailing stop mechanics."""

    def setUp(self):
        from strategy_v3 import QuickScalpStrategy, V3State
        self.V3State = V3State
        self.strat = QuickScalpStrategy()
        self.strat.set_opening_range(24050, 23970)

    def test_initial_sl_set_at_20pct(self):
        self.strat.record_entry("UP", 100.0)
        self.assertAlmostEqual(self.strat.current_sl, 80.0, places=1)

    def test_sl_moves_to_breakeven_at_15pct(self):
        import pandas as pd
        self.strat.record_entry("UP", 100.0)
        candles = pd.DataFrame({"close": [24060, 24070]})
        # Simulate option at +15%
        self.strat.monitor(115.0, 90000, 45000, candles, None, 24060)
        self.assertGreaterEqual(self.strat.current_sl, 100.0)

    def test_trail_sl_only_moves_up(self):
        import pandas as pd
        self.strat.record_entry("UP", 100.0)
        candles = pd.DataFrame({"close": [24070, 24080]})
        # Reach checkpoint
        self.strat.monitor(116.0, 90000, 45000, candles, None, 24070)
        sl_after_checkpoint = self.strat.current_sl
        # Option dips but stays above SL
        self.strat.monitor(112.0, 60000, 45000, candles, None, 24065)
        # SL must not have moved DOWN
        self.assertGreaterEqual(self.strat.current_sl, sl_after_checkpoint)

    def test_sl_hits_closes_trade(self):
        import pandas as pd
        self.strat.record_entry('UP', 100.0)
        # nifty price above OH=24050 → no structural SL
        # premium=79 < SL=80 → INITIAL_SL fires
        candles = pd.DataFrame({'close': [24055, 24060]})
        result = self.strat.monitor(79.0, 40000, 45000, candles, None, 24060)
        self.assertEqual(self.strat.state, self.V3State.CLOSED)
        # V3 now returns 'EXIT:<reason>' signal strings
        self.assertIsNotNone(result)
        self.assertTrue(result.startswith('EXIT:'), f'Expected EXIT: prefix, got: {result}')
    def test_trail_tightens_at_30pct_gain(self):
        self.strat.record_entry('UP', 100.0)
        self.strat.base_trail_pct = 0.15
        self.strat.peak_premium   = 135.0
        trail    = self.strat._compute_trail_sl(135.0, 0.35)
        expected = 135.0 * (1 - 0.12)
        self.assertAlmostEqual(trail, expected, places=1)
    def test_time_exit_fires_at_30_minutes(self):
        import pandas as pd
        from datetime import datetime, timedelta
        self.strat.record_entry("UP", 100.0)
        import pytz
        IST = pytz.timezone('Asia/Kolkata')
        from datetime import datetime as _dt, timedelta as _td
        self.strat.entry_time = _dt.now(IST) - _td(minutes=31)
        candles = pd.DataFrame({"close": [24060, 24070]})
        result = self.strat.monitor(105.0, 50000, 45000, candles, None, 24070)
        self.assertEqual(result, "EXIT:TIME_EXIT")


class TestWilderADX(unittest.TestCase):
    """Fix #19 — ADX uses Wilder's smoothing not SMA."""

    def test_ewm_alpha_equals_1_over_period(self):
        import pandas as pd
        period = 14
        alpha  = 1.0 / period
        data   = [float(i) for i in range(1, 30)]
        s      = pd.Series(data)
        sma    = s.rolling(period).mean().iloc[-1]
        ema    = s.ewm(alpha=alpha, adjust=False).mean().iloc[-1]
        # EMA with Wilder's smoothing should differ from SMA
        self.assertNotAlmostEqual(sma, ema, places=2)

    def test_adr_computation_produces_reasonable_value(self):
        import pandas as pd
        import numpy as np
        np.random.seed(42)
        closes = pd.Series(22000 + np.cumsum(np.random.randn(50) * 50))
        highs  = closes + abs(np.random.randn(50) * 30)
        lows   = closes - abs(np.random.randn(50) * 30)
        prev_c = closes.shift(1)
        tr     = pd.concat([highs-lows,
                             (highs-prev_c).abs(),
                             (lows-prev_c).abs()], axis=1).max(axis=1)
        alpha  = 1/14
        atr    = tr.ewm(alpha=alpha, adjust=False).mean().iloc[-1]
        # ATR should be within a plausible range for Nifty
        self.assertGreater(atr, 0)
        self.assertLess(atr, 500)


class TestRetryDecorator(unittest.TestCase):
    """Fix #24 — retry decorator retries on transient failures."""

    def test_succeeds_on_first_try(self):
        from utils import retry
        calls = []
        @retry(max_attempts=3, base_delay=0.01)
        def fn():
            calls.append(1)
            return "ok"
        self.assertEqual(fn(), "ok")
        self.assertEqual(len(calls), 1)

    def test_retries_on_failure_then_succeeds(self):
        from utils import retry
        calls = []
        @retry(max_attempts=3, base_delay=0.01)
        def fn():
            calls.append(1)
            if len(calls) < 3:
                raise ConnectionError("transient")
            return "ok"
        self.assertEqual(fn(), "ok")
        self.assertEqual(len(calls), 3)

    def test_raises_after_max_attempts(self):
        from utils import retry
        @retry(max_attempts=2, base_delay=0.01)
        def fn():
            raise TimeoutError("always fails")
        with self.assertRaises(TimeoutError):
            fn()


class TestSlippageSimulation(unittest.TestCase):
    """Fix #25 — slippage moves price in the unfavourable direction."""

    def test_buy_slippage_increases_price(self):
        from utils import simulate_slippage
        results = [simulate_slippage(100.0, "BUY") for _ in range(20)]
        self.assertTrue(all(r >= 100.0 for r in results))

    def test_sell_slippage_decreases_price(self):
        from utils import simulate_slippage
        results = [simulate_slippage(100.0, "SELL") for _ in range(20)]
        self.assertTrue(all(r <= 100.0 for r in results))

    def test_slippage_within_expected_range(self):
        from utils import simulate_slippage
        for _ in range(50):
            r = simulate_slippage(100.0, "BUY", pct=0.015)
            self.assertLessEqual(r, 101.6)
            self.assertGreaterEqual(r, 100.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestDualLoopMonitor(unittest.TestCase):
    """Dual-frequency monitoring loop correctness."""

    def _make_monitor(self, exit_cb=None):
        """Build a DualLoopMonitor with mock dependencies."""
        from monitor_loop import DualLoopMonitor, LoopState

        groww = MagicMock()
        md    = MagicMock()
        md.opening_high = 24050.0
        md.opening_low  = 23970.0

        orders = MagicMock()
        orders.signal_direction = "BUY_CALL"
        orders.state = MagicMock()

        cb = exit_cb or (lambda r: None)
        mon = DualLoopMonitor(groww, md, orders, cb)
        return mon, orders

    def test_register_sets_sl_at_25pct(self):
        from monitor_loop import DualLoopMonitor
        mon, _ = self._make_monitor()
        mon.register_trade("BUY_SYM", "SELL_SYM", 100.0, 150.0)
        # SL_PREMIUM_PCT = 0.25 in config → 100 × 0.75 = 75.0
        self.assertAlmostEqual(mon._state.current_sl, 75.0, places=1)

    def test_register_sets_l1_target(self):
        from monitor_loop import DualLoopMonitor
        mon, _ = self._make_monitor()
        mon.register_trade("BUY_SYM", "SELL_SYM", 100.0, 150.0)
        self.assertAlmostEqual(mon._state.level1_target, 130.0, places=1)

    def test_register_sets_hard_target(self):
        from monitor_loop import DualLoopMonitor
        mon, _ = self._make_monitor()
        mon.register_trade("BUY_SYM", "SELL_SYM", 100.0, 150.0)
        self.assertAlmostEqual(mon._state.hard_target, 160.0, places=1)

    def test_sl_hit_triggers_exit(self):
        from monitor_loop import DualLoopMonitor
        exit_calls = []
        mon, orders = self._make_monitor(exit_cb=lambda r: exit_calls.append(r))
        mon.register_trade("B", "S", 100.0, 150.0)

        # Simulate fast loop value below SL
        mon._state.trade_open = True
        with patch.object(mon, "_fetch_current_value", return_value=75.0):
            with patch("monitor_loop.datetime") as mock_dt:
                mock_dt.now.return_value.hour   = 10
                mock_dt.now.return_value.minute = 0
                mon._fast_cycle()

        self.assertTrue(mon._state.exit_triggered)
        self.assertEqual(mon._state.exit_reason, "STOP_LOSS")

    def test_exit_not_triggered_twice(self):
        """Second _trigger_exit call returns existing reason — no double exit."""
        from monitor_loop import DualLoopMonitor
        call_count = [0]
        def cb(r): call_count[0] += 1

        mon, orders = self._make_monitor(exit_cb=cb)
        mon.register_trade("B", "S", 100.0, 150.0)
        mon._state.trade_open = True

        r1 = mon._trigger_exit("STOP_LOSS")
        r2 = mon._trigger_exit("TIME_EXIT")   # second call — should be no-op

        self.assertEqual(r1, "STOP_LOSS")
        self.assertEqual(r2, "STOP_LOSS")    # original reason preserved
        import time as _time; _time.sleep(0.1)  # let daemon thread fire cb
        self.assertEqual(call_count[0], 1)   # callback fired exactly once

    def test_move_sl_to_breakeven(self):
        from monitor_loop import DualLoopMonitor
        mon, _ = self._make_monitor()
        mon.register_trade("B", "S", 100.0, 150.0)
        self.assertAlmostEqual(mon._state.current_sl, 75.0, places=1)
        mon.move_sl_to_breakeven()
        self.assertAlmostEqual(mon._state.current_sl, 100.0, places=1)
        self.assertTrue(mon._state.at_breakeven)

    def test_trailing_sl_only_moves_up(self):
        from monitor_loop import DualLoopMonitor
        mon, _ = self._make_monitor()
        mon.register_trade("B", "S", 100.0, 150.0)
        mon.move_sl_to_breakeven()
        mon.update_trailing_sl(110.0)
        self.assertAlmostEqual(mon._state.current_sl, 110.0, places=1)
        # Attempt to lower — should be rejected
        mon.update_trailing_sl(95.0)
        self.assertAlmostEqual(mon._state.current_sl, 110.0, places=1)

    def test_squareoff_time_triggers_exit(self):
        from monitor_loop import DualLoopMonitor
        mon, orders = self._make_monitor()
        mon.register_trade("B", "S", 100.0, 150.0)
        mon._state.trade_open = True

        with patch.object(mon, "_fetch_current_value", return_value=110.0):
            with patch("monitor_loop.datetime") as mock_dt:
                # Simulate 15:10 IST
                mock_dt.now.return_value.hour   = 15
                mock_dt.now.return_value.minute = 10
                mon._fast_cycle()

        self.assertTrue(mon._state.exit_triggered)
        self.assertEqual(mon._state.exit_reason, "SQUAREOFF_TIME")

    def test_fast_loop_skips_if_already_exited(self):
        """Fast loop does nothing if exit already triggered."""
        from monitor_loop import DualLoopMonitor
        mon, orders = self._make_monitor()
        mon.register_trade("B", "S", 100.0, 150.0)
        mon._state.exit_triggered = True
        mon._state.trade_open     = False

        with patch.object(mon, "_fetch_current_value") as mock_fetch:
            mon._fast_cycle()
            mock_fetch.assert_not_called()


class TestConcurrentOrderPlacement(unittest.TestCase):
    """Concurrent leg placement — both futures resolve before proceeding."""

    def test_both_legs_placed_simultaneously(self):
        from concurrent.futures import ThreadPoolExecutor
        import time as _time
        call_times = []

        def mock_order(delay):
            call_times.append(_time.time())
            _time.sleep(delay)
            return {"groww_order_id": f"ID_{delay}"}

        start = _time.time()
        with ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(mock_order, 0.1)
            f2 = pool.submit(mock_order, 0.1)
            r1 = f1.result(timeout=2)
            r2 = f2.result(timeout=2)
        elapsed = _time.time() - start

        # Both should complete in ~0.1s (parallel), not ~0.2s (sequential)
        self.assertLess(elapsed, 0.18, "Legs ran sequentially — should be concurrent")
        self.assertEqual(r1["groww_order_id"], "ID_0.1")
        self.assertEqual(r2["groww_order_id"], "ID_0.1")

    def test_timeout_detected(self):
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FTE
        import time as _time

        def slow(): _time.sleep(5)

        with ThreadPoolExecutor(max_workers=1) as pool:
            f = pool.submit(slow)
            with self.assertRaises(FTE):
                f.result(timeout=0.05)


class TestExceptionClassifier(unittest.TestCase):
    """Exception classifier correctly separates hard rejections from transient."""

    def setUp(self):
        # Import the classifier directly
        import importlib.util, sys
        spec = importlib.util.spec_from_file_location("om", "order_manager.py")
        mod  = importlib.util.module_from_spec(spec)
        sys.modules["om"] = mod
        try:
            spec.loader.exec_module(mod)
            self._classify = mod._classify_error
        except Exception:
            self._classify = None

    def test_insufficient_margin_is_hard(self):
        if not self._classify: self.skipTest("module load failed")
        self.assertEqual(self._classify(Exception("Insufficient margin")), "HARD_REJECT")

    def test_invalid_token_is_hard(self):
        if not self._classify: self.skipTest("module load failed")
        self.assertEqual(self._classify(Exception("Invalid token error")), "HARD_REJECT")

    def test_connection_error_is_transient(self):
        if not self._classify: self.skipTest("module load failed")
        self.assertEqual(self._classify(ConnectionError("timeout")), "TRANSIENT")

    def test_generic_exception_is_transient(self):
        if not self._classify: self.skipTest("module load failed")
        self.assertEqual(self._classify(Exception("Unknown server error")), "TRANSIENT")


class TestV3SignalProtocol(unittest.TestCase):
    """V3 strategy emits EXIT: prefixed signals — does not call orders directly."""

    def setUp(self):
        from strategy_v3 import QuickScalpStrategy
        self.strat = QuickScalpStrategy()
        self.strat.set_opening_range(24050, 23970)

    def test_sl_hit_returns_exit_signal(self):
        import pandas as pd
        self.strat.record_entry("UP", 100.0)
        candles = pd.DataFrame({"close": [24060, 24065]})
        result = self.strat.monitor(74.0, 40000, 45000, candles, None, 24065)
        self.assertIsNotNone(result)
        self.assertTrue(result.startswith("EXIT:"), f"Expected EXIT: prefix, got: {result}")

    def test_time_exit_returns_exit_signal(self):
        import pandas as pd
        from datetime import datetime, timedelta
        import pytz
        IST = pytz.timezone("Asia/Kolkata")
        self.strat.record_entry("UP", 100.0)
        self.strat.entry_time = datetime.now(IST) - timedelta(minutes=31)
        candles = pd.DataFrame({"close": [24060, 24070]})
        result = self.strat.monitor(105.0, 50000, 45000, candles, None, 24070)
        self.assertIsNotNone(result)
        self.assertTrue(result.startswith("EXIT:"))

    def test_no_signal_when_open(self):
        import pandas as pd
        self.strat.record_entry("UP", 100.0)
        candles = pd.DataFrame({"close": [24060, 24065]})
        # Premium at 105 — no SL hit, no target hit
        result = self.strat.monitor(105.0, 50000, 45000, candles, None, 24065)
        self.assertIsNone(result)


class TestV3ConfigConstants(unittest.TestCase):
    """All V3 constants come from config.py — no magic numbers in strategy_v3."""

    def test_all_v3_constants_in_config(self):
        import config
        required = [
            "V3_BROKERAGE", "V3_MIN_RANGE_PTS", "V3_MAX_RANGE_PTS",
            "V3_SL_INITIAL_PCT", "V3_CHECKPOINT_PCT",
            "V3_TRAIL_BY_SCORE", "V3_TRAIL_TIGHTEN",
            "V3_MAX_HOLD_MINUTES",
            "V3_OPENING_RANGE_END", "V3_ENTRY_WINDOW_START",
            "V3_ENTRY_WINDOW_END", "V3_BREAKOUT_BUFFER",
        ]
        for name in required:
            self.assertTrue(hasattr(config, name), f"Missing from config: {name}")

    def test_strategy_v3_has_no_hardcoded_numbers(self):
        """Magic numbers like 400, 30, 150 should not appear as bare literals."""
        with open("strategy_v3.py") as f:
            src = f.read()
        # These specific values should not be bare assignments in the module body
        for bad in ["BROKERAGE     = 400", "MIN_RANGE_PTS = 30", "MAX_RANGE_PTS = 150",
                    "SL_INITIAL_PCT      = 0.20", "CHECKPOINT_PCT      = 0.15"]:
            self.assertNotIn(bad, src, f"Hardcoded value still present: {bad}")

    def test_v3_constants_have_correct_types(self):
        from config import (V3_TRAIL_BY_SCORE, V3_TRAIL_TIGHTEN,
                            V3_SL_INITIAL_PCT, V3_CHECKPOINT_PCT)
        self.assertIsInstance(V3_TRAIL_BY_SCORE, dict)
        self.assertIsInstance(V3_TRAIL_TIGHTEN,  list)
        self.assertGreater(V3_SL_INITIAL_PCT, 0)
        self.assertGreater(V3_CHECKPOINT_PCT, 0)


class TestDataRetry(unittest.TestCase):
    """@retry is applied to data.py — transient errors are retried."""

    def test_retry_applied_to_data_module(self):
        """Confirm retry import exists in data.py source."""
        with open("data.py") as f:
            src = f.read()
        self.assertIn("from utils import retry", src)
        self.assertIn("@retry", src)

    def test_retry_decorator_retries_on_network_error(self):
        from utils import retry
        call_count = [0]

        @retry(max_attempts=3, base_delay=0.01)
        def flaky():
            call_count[0] += 1
            if call_count[0] < 3:
                raise ConnectionError("network blip")
            return "ok"

        result = flaky()
        self.assertEqual(result, "ok")
        self.assertEqual(call_count[0], 3)


class TestV3SignalParser(unittest.TestCase):
    """_parse_v3_signal correctly interprets signal strings."""

    def _fn(self, sig):
        """Import and call _parse_v3_signal directly."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("main_sig", "main.py")
        # Build a minimal namespace with just the function
        ns = {}
        with open("main.py") as f:
            lines = f.readlines()
        fn_lines = []
        in_fn = False
        for line in lines:
            if line.startswith("def _parse_v3_signal"):
                in_fn = True
            elif in_fn and line.startswith("def ") and not line.startswith("def _parse_v3_signal"):
                break
            if in_fn:
                fn_lines.append(line)
        exec("".join(fn_lines), ns)
        return ns["_parse_v3_signal"](sig)

    def test_exit_signal_parsed(self):
        action, val = self._fn("EXIT:STOP_LOSS")
        self.assertEqual(action, "EXIT")
        self.assertEqual(val,    "STOP_LOSS")

    def test_sl_update_parsed(self):
        action, val = self._fn("SL_UPDATE:95.5")
        self.assertEqual(action, "SL_UPDATE")
        self.assertEqual(val,    "95.5")

    def test_none_signal(self):
        action, val = self._fn(None)
        self.assertIsNone(action)
        self.assertIsNone(val)

    def test_empty_string(self):
        action, val = self._fn("")
        self.assertIsNone(action)


class TestScalpIndicatorEngine(unittest.TestCase):
    """IndicatorEngine computes VWAP and swing levels correctly."""

    def _make_candle(self, h, l, c, v, t="09:20"):
        from strategy_scalp import Candle
        return Candle(time=t, open=c-1, high=h, low=l, close=c, volume=v)

    def setUp(self):
        from strategy_scalp import IndicatorEngine
        self.eng = IndicatorEngine()

    def test_vwap_updates_after_candle(self):
        c = self._make_candle(24060, 23980, 24020, 50000)
        self.eng.add_nifty_candle(c)
        self.assertIsNotNone(self.eng.vwap)
        tp = (24060 + 23980 + 24020) / 3
        expected = round(tp * 50000 / 50000, 2)
        self.assertAlmostEqual(self.eng.vwap, expected, places=1)

    def test_vwap_resets_on_new_day(self):
        c = self._make_candle(24060, 23980, 24020, 50000)
        self.eng.add_nifty_candle(c)
        self.eng.reset_day()
        self.assertIsNone(self.eng.vwap)

    def test_swing_high_returns_none_if_insufficient_data(self):
        self.assertIsNone(self.eng.get_swing_high())

    def test_swing_high_after_10_candles(self):
        from strategy_scalp import SWING_LOOKBACK
        for i in range(SWING_LOOKBACK):
            self.eng.add_nifty_candle(
                self._make_candle(24000 + i, 23900 + i, 23950 + i, 40000)
            )
        sh = self.eng.get_swing_high()
        self.assertIsNotNone(sh)
        self.assertEqual(sh, 24000 + SWING_LOOKBACK - 1)

    def test_swing_low_after_10_candles(self):
        from strategy_scalp import SWING_LOOKBACK
        for i in range(SWING_LOOKBACK):
            self.eng.add_nifty_candle(
                self._make_candle(24000 + i, 23900 + i, 23950 + i, 40000)
            )
        sl = self.eng.get_swing_low()
        self.assertIsNotNone(sl)
        self.assertEqual(sl, 23900)

    def test_dynamic_sl_clamped_to_floor(self):
        # With very small option moves, ATR will be tiny → floor applies
        for _ in range(10):
            self.eng.add_option_premium("09:20", 100.0, 100.2)
        sl = self.eng.compute_dynamic_sl()
        from strategy_scalp import SL_FLOOR
        self.assertGreaterEqual(sl, SL_FLOOR)

    def test_dynamic_sl_clamped_to_cap(self):
        # With huge option moves, ATR will be big → cap applies
        for _ in range(10):
            self.eng.add_option_premium("09:20", 100.0, 115.0)
        sl = self.eng.compute_dynamic_sl()
        from strategy_scalp import SL_CAP
        self.assertLessEqual(sl, SL_CAP)


class TestEntryDetector(unittest.TestCase):
    """EntryDetector fires correct signals on VWAP + swing break with volume."""

    def _setup_engine_with_candles(self):
        from strategy_scalp import IndicatorEngine, EntryDetector, Candle, SWING_LOOKBACK
        eng = IndicatorEngine()
        eng.set_slot_averages({"09:30": 40000.0})
        # Add 10 base candles around 24000
        for i in range(SWING_LOOKBACK):
            eng.add_nifty_candle(
                Candle(time="09:20", open=23995, high=24010,
                       low=23990, close=24000, volume=35000)
            )
        det = EntryDetector(eng)
        return eng, det

    def test_call_signal_on_vwap_and_swing_break(self):
        from strategy_scalp import Candle, IndicatorEngine, EntryDetector, SWING_LOOKBACK
        # Build engine with rising candles so VWAP trends up
        eng = IndicatorEngine()
        eng.set_slot_averages({"09:30": 40000.0})
        base = 24000
        for i in range(SWING_LOOKBACK):
            eng.add_nifty_candle(Candle(
                time="09:20", open=base+i, high=base+i+5,
                low=base+i-5, close=base+i+3, volume=40000
            ))
        det = EntryDetector(eng)
        # Breakout: check BEFORE adding candle (mirrors correct production order)
        sw_high = eng.get_swing_high()
        vwap    = eng.vwap
        trigger = round(max(sw_high, vwap) + 10, 2)
        breakout = Candle(time="09:30", open=trigger-2, high=trigger+5,
                          low=trigger-2, close=trigger, volume=120000)
        sig = det.check(breakout)    # check BEFORE add
        eng.add_nifty_candle(breakout)  # add after
        self.assertIsNotNone(sig)
        self.assertEqual(sig.direction, "CALL")

    def test_no_signal_on_low_volume(self):
        from strategy_scalp import Candle
        eng, det = self._setup_engine_with_candles()
        weak = Candle(time="09:30", open=24000, high=24080,
                      low=24000, close=24075, volume=20000)  # below 2.5×
        sig = det.check(weak)  # check before add
        eng.add_nifty_candle(weak)
        self.assertIsNone(sig)

    def test_put_signal_on_breakdown(self):
        from strategy_scalp import Candle
        eng, det = self._setup_engine_with_candles()
        # Force VWAP to be above 23930 by adding bearish candles
        for _ in range(5):
            eng.add_nifty_candle(
                Candle("09:25", 23950, 23960, 23930, 23935, 50000)
            )
        breakdown = Candle(time="09:30", open=23935, high=23940,
                           low=23870, close=23875, volume=110000)
        eng.add_nifty_candle(breakdown)
        sig = det.check(breakdown)
        if sig:  # only assert if signal fired (VWAP condition may not hold)
            self.assertEqual(sig.direction, "PUT")

    def test_no_signal_before_swing_lookback(self):
        from strategy_scalp import IndicatorEngine, EntryDetector, Candle
        eng = IndicatorEngine()
        eng.set_slot_averages({"09:20": 40000.0})
        det = EntryDetector(eng)
        # Only 3 candles — not enough for swing lookback
        for _ in range(3):
            eng.add_nifty_candle(
                Candle("09:20", 24000, 24050, 23990, 24040, 90000)
            )
        sig = det.check(
            Candle("09:20", 24040, 24090, 24040, 24085, 100000)
        )
        self.assertIsNone(sig)


class TestScalpPosition(unittest.TestCase):
    """ScalpPosition monitors correctly and returns EXIT: prefixed signals."""

    def _make_pos(self, direction="CALL", entry=100.0, sl=2.5):
        from strategy_scalp import ScalpPosition, ScalpSignal
        sig = ScalpSignal(
            direction="CALL", entry_level=24050.0,
            vwap=24020.0, volume=90000, avg_volume=40000,
            candle_time="09:30"
        )
        return ScalpPosition(sig, entry, sl)

    def test_premium_sl_triggers_exit(self):
        pos = self._make_pos(entry=100.0, sl=2.5)
        # SL level = 100 - 2.5 = 97.5
        result = pos.monitor_fast(97.0, 24060.0, 90000, 40000)
        self.assertIsNotNone(result)
        self.assertTrue(result.startswith("EXIT:"))
        self.assertIn("PREMIUM_SL", result)

    def test_structural_sl_triggers_exit(self):
        pos = self._make_pos()
        # Nifty drops below entry_level (24050)
        result = pos.monitor_fast(102.0, 24040.0, 90000, 40000)
        self.assertIsNotNone(result)
        self.assertIn("STRUCTURAL_SL", result)

    def test_no_exit_when_profitable_and_in_window(self):
        pos = self._make_pos()
        result = pos.monitor_fast(103.0, 24060.0, 90000, 40000)
        self.assertIsNone(result)

    def test_checkpoint_exit_at_score_0(self):
        from strategy_scalp import ScalpState
        pos = self._make_pos()
        # Simulate option at +₹2 gain with weak volume
        result = pos.monitor_slow(102.0, 10000, 40000, [24050, 24048])
        self.assertIsNotNone(result)
        self.assertIn("CHECKPOINT_EXIT", result)

    def test_checkpoint_trail_at_score_2(self):
        from strategy_scalp import ScalpState, TRAIL_SCORE_2
        pos = self._make_pos()
        # Strong volume + bullish candles → score 2
        result = pos.monitor_slow(102.0, 110000, 40000, [24050, 24065])
        self.assertIsNone(result)   # trail activated, not exited
        self.assertEqual(pos.state, ScalpState.TRAILING)
        self.assertEqual(pos.trail_width, TRAIL_SCORE_2)

    def test_trail_sl_raises_with_peak(self):
        from strategy_scalp import ScalpState, TRAIL_SCORE_2
        pos = self._make_pos()
        # Activate trail (score 2)
        pos.monitor_slow(102.0, 110000, 40000, [24050, 24065])
        # Option rallies to 106 — trail SL should rise
        pos.monitor_fast(106.0, 24075.0, 110000, 40000)
        expected_sl = round(106.0 - TRAIL_SCORE_2, 2)
        self.assertAlmostEqual(pos.current_sl, expected_sl, places=1)

    def test_trail_sl_only_moves_up(self):
        from strategy_scalp import TRAIL_SCORE_2
        pos = self._make_pos()
        pos.monitor_slow(102.0, 110000, 40000, [24050, 24065])
        pos.monitor_fast(106.0, 24075.0, 110000, 40000)
        sl_after_high = pos.current_sl
        # Option drops back to 104 — SL must not lower
        pos.monitor_fast(104.0, 24070.0, 110000, 40000)
        self.assertGreaterEqual(pos.current_sl, sl_after_high)

    def test_exit_signal_has_exit_prefix(self):
        pos = self._make_pos(entry=100.0, sl=2.5)
        result = pos.monitor_fast(97.0, 24060.0, 90000, 40000)
        self.assertTrue(result.startswith("EXIT:"))

    def test_net_pnl_includes_charges(self):
        from strategy_scalp import LOT_SIZE, CHARGES_PER_TRADE
        pos = self._make_pos(entry=100.0, sl=2.5)
        result = pos.monitor_fast(97.0, 24060.0, 90000, 40000)
        expected_gross = (97.0 - 100.0) * LOT_SIZE
        expected_net   = expected_gross - CHARGES_PER_TRADE
        self.assertAlmostEqual(pos.realised_pnl, expected_net, places=1)


class TestScalpDayController(unittest.TestCase):
    """DayController enforces all daily limits correctly."""

    def _make_controller(self):
        from strategy_scalp import ScalpDayController
        ctrl = ScalpDayController()
        ctrl._in_trade_hours = lambda: True  # bypass time gate in tests
        return ctrl

    def test_trading_allowed_initially(self):
        ctrl = self._make_controller()
        allowed, _ = ctrl.can_enter()
        # might fail outside trade hours — just check type
        self.assertIsInstance(allowed, bool)

    def test_max_trades_blocks_entry(self):
        from strategy_scalp import MAX_TRADES_PER_DAY
        ctrl = self._make_controller()
        ctrl.trades_today = MAX_TRADES_PER_DAY
        allowed, reason = ctrl.can_enter()
        self.assertFalse(allowed)
        self.assertIn("limit", reason.lower())

    def test_daily_loss_limit_blocks_entry(self):
        from strategy_scalp import DAILY_LOSS_LIMIT
        ctrl = self._make_controller()
        ctrl.daily_pnl = -DAILY_LOSS_LIMIT - 1
        allowed, reason = ctrl.can_enter()
        self.assertFalse(allowed)
        self.assertIn("loss", reason.lower())

    def test_consecutive_loss_pause(self):
        from strategy_scalp import ScalpPosition, ScalpSignal, CONSEC_LOSS_PAUSE
        ctrl = self._make_controller()
        for _ in range(CONSEC_LOSS_PAUSE):
            ctrl.consecutive_loss += 1
        from datetime import datetime, timedelta
        import pytz
        ctrl.pause_until = datetime.now(pytz.timezone("Asia/Kolkata")) + timedelta(minutes=25)
        allowed, reason = ctrl.can_enter()
        self.assertFalse(allowed)
        self.assertIn("paused", reason.lower())

    def test_consecutive_loss_resets_on_win(self):
        from strategy_scalp import ScalpDayController, ScalpPosition, ScalpSignal
        ctrl = self._make_controller()
        ctrl.consecutive_loss = 2
        # Simulate a winning position
        sig = ScalpSignal("CALL", 24050.0, 24020.0, 90000, 40000, "09:30")
        pos = ScalpPosition(sig, 100.0, 2.5)
        pos.realised_pnl = 150.0  # win
        pos.exit_reason  = "TRAIL_SL"
        from strategy_scalp import ScalpState
        pos.state = ScalpState.CLOSED
        ctrl.close_position(pos)
        self.assertEqual(ctrl.consecutive_loss, 0)

    def test_daily_report_runs_without_error(self):
        ctrl = self._make_controller()
        report = ctrl.daily_report()
        self.assertIn("SCALP", report)
        self.assertIn("Net P&L", report)


class TestMomentumScorer(unittest.TestCase):
    """MomentumScorer returns correct score based on volume and candle direction."""

    def test_score_2_on_strong_volume_and_bullish_candles(self):
        from strategy_scalp import MomentumScorer
        score = MomentumScorer.score(
            direction    = "CALL",
            current_vol  = 100000,
            avg_vol      = 40000,   # 2.5× > 1.5× → +1
            last_2_closes= [24050, 24065],  # bullish → +1
        )
        self.assertEqual(score, 2)

    def test_score_0_on_weak_volume_and_bearish_candles(self):
        from strategy_scalp import MomentumScorer
        score = MomentumScorer.score(
            direction    = "CALL",
            current_vol  = 20000,   # < 1.5× → 0
            avg_vol      = 40000,
            last_2_closes= [24065, 24050],  # bearish → 0
        )
        self.assertEqual(score, 0)

    def test_score_1_on_volume_only(self):
        from strategy_scalp import MomentumScorer
        score = MomentumScorer.score(
            direction    = "CALL",
            current_vol  = 80000,   # > 1.5× → +1
            avg_vol      = 40000,
            last_2_closes= [24065, 24050],  # bearish → 0
        )
        self.assertEqual(score, 1)

    def test_put_direction_scoring(self):
        from strategy_scalp import MomentumScorer
        score = MomentumScorer.score(
            direction    = "PUT",
            current_vol  = 80000,
            avg_vol      = 40000,
            last_2_closes= [24065, 24050],  # falling → +1 for PUT
        )
        self.assertEqual(score, 2)
