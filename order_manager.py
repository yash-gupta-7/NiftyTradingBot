"""
order_manager.py — Order Manager
Handles the full lifecycle of a spread trade:
  1. Place both spread legs
  2. Monitor position P&L every 5-min candle close
  3. Execute Level 1 partial exit (40% at 1× ATR)
  4. Trail stop using 2-bar swing high/low
  5. Execute Level 2/3 exits
  6. Force square-off at 3:10 PM
"""

import time
import logging
from datetime import datetime
import pytz
IST = pytz.timezone('Asia/Kolkata')
from typing import Optional
from enum import Enum
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from growwapi import GrowwAPI
from utils import retry   # FIX 1: was used but never imported

from data import MarketData
from config import (
    NIFTY_LOT_SIZE, LOTS_PER_TRADE,
    SL_PREMIUM_PCT,
    LEVEL1_ATR_MULTIPLE, LEVEL1_EXIT_PCT,
    LEVEL2_TRAIL_CANDLES, LEVEL3_ATR_MULTIPLE,
    SQUAREOFF_TIME, SEGMENT_FNO,
)

logger = logging.getLogger(__name__)




# ─── LIMIT ORDER HELPERS ──────────────────────────────────────────────────────

LIMIT_BUFFER_PCT = 0.002   # 0.2% buffer — buy at LTP×1.002, sell at LTP×0.998
LIMIT_TIMEOUT_S  = 5       # seconds to wait for limit fill before cancelling

def _limit_price(ltp: float, side: str) -> float:
    """
    Converts LTP to a limit price with a small buffer.
    BUY:  slightly above LTP to ensure fill without chasing
    SELL: slightly below LTP to ensure fill without giving away too much
    """
    if side == "BUY":
        return round(ltp * (1 + LIMIT_BUFFER_PCT), 2)
    return round(ltp * (1 - LIMIT_BUFFER_PCT), 2)

# ─── EXCEPTION CLASSIFIER ─────────────────────────────────────────────────────

class ExchangeRejection(Exception):
    """
    Raised when the exchange hard-rejects an order (margin, symbol, token).
    These should NOT be retried — they require immediate halt + alert.
    """

HARD_REJECTION_KEYWORDS = (
    "insufficient margin", "insufficient funds",
    "invalid token", "invalid symbol",
    "scrip suspended", "circuit limit",
    "order limit exceeded",
)

def _classify_error(exc: Exception) -> str:
    """
    Returns "HARD_REJECT" (exchange refusal) or "TRANSIENT" (network/timeout).
    HARD_REJECT → halt immediately.
    TRANSIENT   → retry via @retry decorator.
    """
    msg = str(exc).lower()
    if any(kw in msg for kw in HARD_REJECTION_KEYWORDS):
        return "HARD_REJECT"
    return "TRANSIENT"


class TradeState(Enum):
    IDLE          = "IDLE"
    ENTERING      = "ENTERING"
    OPEN          = "OPEN"
    PARTIAL_EXIT  = "PARTIAL_EXIT"   # After Level 1 exit
    CLOSED        = "CLOSED"


class OrderManager:
    """
    Manages a single active spread trade.
    One instance per day — reset at market open.
    """

    def __init__(self, groww: GrowwAPI, market_data: MarketData):
        self.groww = groww
        self.md    = market_data

        # Trade state
        self.state = TradeState.IDLE
        self.signal_direction: Optional[str] = None   # "BUY_CALL" or "BUY_PUT"

        # Spread details (from StrikeSelector)
        self.spread_info: Optional[dict] = None

        # Order IDs
        self.buy_order_id:  Optional[str] = None
        self.sell_order_id: Optional[str] = None

        # Entry prices (actual fill prices)
        self.buy_entry_price:  Optional[float] = None
        self.sell_entry_price: Optional[float] = None
        self.net_entry_cost:   Optional[float] = None  # per unit

        # Position tracking
        self.total_units     = NIFTY_LOT_SIZE * LOTS_PER_TRADE   # 75
        self.remaining_units = self.total_units
        self.exited_units    = 0

        # Stop loss
        self.current_sl_spread_value: Optional[float] = None  # spread value below which we exit
        self.at_breakeven: bool = False

        # Trailing stop (2-bar swing)
        self.trailing_sl_price: Optional[float] = None  # Nifty price level for trail
        self.swing_highs = []
        self.swing_lows  = []

        # P&L tracking
        self.realised_pnl:   float = 0.0
        self.unrealised_pnl: float = 0.0
        self.entry_time: Optional[datetime] = None
        self.exit_time:  Optional[datetime] = None
        self.exit_reason: Optional[str] = None

    # ─── ENTRY ────────────────────────────────────────────────────────────────


    @retry(max_attempts=2, base_delay=1.0)
    def _check_sufficient_margin(self, required_amount: float) -> tuple:
        """
        FIX B: Proactively checks available margin BEFORE placing orders.
        Returns (sufficient: bool, available: float).
        Prevents wasted order attempts / API hammering when margin is tight.
        """
        try:
            margin_resp = self.groww.get_available_margin_details(
                segment=self.groww.SEGMENT_FNO
            )
            available = float(
                margin_resp.get("available_margin")
                or margin_resp.get("net_margin_available")
                or 0
            )
            # Require a 10% buffer above the exact cost to absorb slippage
            buffer_required = required_amount * 1.10
            sufficient = available >= buffer_required

            if not sufficient:
                logger.warning(
                    f"⚠️ Insufficient margin: need ₹{buffer_required:.0f} "
                    f"(incl. 10% buffer), available ₹{available:.0f}"
                )
            return sufficient, available

        except Exception as e:
            logger.warning(
                f"Could not check margin proactively: {e}. "
                f"Proceeding — exchange will reject if truly insufficient."
            )
            return True, -1.0   # fail-open here only — exchange is the final guard

    def enter_trade(self, signal_direction: str, spread_info: dict) -> bool:
        """
        Places both legs of the spread trade.
        Returns True if both legs filled successfully.
        """
        if self.state != TradeState.IDLE:
            logger.warning("⚠️ Attempted to enter while trade already active")
            return False

        self.signal_direction = signal_direction
        self.spread_info      = spread_info
        self.state            = TradeState.ENTERING

        logger.info(
            f"🚀 ENTERING TRADE: {signal_direction} | "
            f"Buy {spread_info['buy_symbol']} | "
            f"Sell {spread_info['sell_symbol']} | "
            f"Net cost/unit: ₹{spread_info['net_spread_cost']}"
        )

        # FIX B: proactive margin check before attempting any order
        required = spread_info.get("total_cost", 0)
        margin_ok, available = self._check_sufficient_margin(required)
        if not margin_ok:
            logger.error(
                f"🚫 Skipping entry — insufficient margin "
                f"(need ₹{required*1.1:.0f}, have ₹{available:.0f})"
            )
            self.state = TradeState.IDLE
            return False

        try:
            # ── Leg 1: BUY the ATM option ──────────────────────────────────────
            buy_resp = self.groww.place_order(
                trading_symbol=spread_info["buy_symbol"],
                quantity=self.total_units,
                validity=self.groww.VALIDITY_DAY,
                exchange=self.groww.EXCHANGE_NSE,
                segment=self.groww.SEGMENT_FNO,
                product=self.groww.PRODUCT_MIS,   # Intraday
                order_type=self.groww.ORDER_TYPE_MARKET,
                transaction_type=self.groww.TRANSACTION_TYPE_BUY,
                order_reference_id=self._gen_ref_id("BUY_LEG1")
            )

            self.buy_order_id = buy_resp.get("groww_order_id")
            logger.info(f"📋 Leg 1 (BUY) placed: order_id={self.buy_order_id}")

            # ── Leg 2: SELL the OTM option ─────────────────────────────────────
            sell_resp = self.groww.place_order(
                trading_symbol=spread_info["sell_symbol"],
                quantity=self.total_units,
                validity=self.groww.VALIDITY_DAY,
                exchange=self.groww.EXCHANGE_NSE,
                segment=self.groww.SEGMENT_FNO,
                product=self.groww.PRODUCT_MIS,
                order_type=self.groww.ORDER_TYPE_MARKET,
                transaction_type=self.groww.TRANSACTION_TYPE_SELL,
                order_reference_id=self._gen_ref_id("SELL_LEG2")
            )

            self.sell_order_id = sell_resp.get("groww_order_id")
            logger.info(f"📋 Leg 2 (SELL) placed: order_id={self.sell_order_id}")

            # FIX #1: Poll both legs for confirmed COMPLETE status
            if not self._wait_for_fills(timeout=15):
                logger.error("❌ Fill timeout — emergency exit of filled legs")
                self._emergency_exit_filled_legs()
                return False
            self._record_entry_prices()

            # Set initial stop loss
            self._set_initial_stop_loss()

            self.state      = TradeState.OPEN
            self.entry_time = datetime.now(IST)

            logger.info(
                f"✅ TRADE OPEN | Entry cost/unit=₹{self.net_entry_cost:.2f} | "
                f"Total capital at risk=₹{self.net_entry_cost * self.total_units:.0f} | "
                f"SL spread value=₹{self.current_sl_spread_value:.2f}/unit"
            )
            return True

        except Exception as e:
            logger.error(f"❌ Trade entry failed: {e}")
            self._handle_partial_entry()
            return False

    # ─── MONITORING LOOP (call on every 5-min candle close) ──────────────────

    def monitor(self, nifty_price: float, candles_df) -> Optional[str]:
        """
        Core monitoring function. Call on every new 5-min candle close.
        Returns exit reason string if trade was exited, None if still open.
        """
        if self.state not in (TradeState.OPEN, TradeState.PARTIAL_EXIT):
            return None

        now = datetime.now(IST)

        # ── Force square-off check ─────────────────────────────────────────────
        if now.strftime("%H:%M") >= SQUAREOFF_TIME:
            logger.info("⏰ 3:10 PM — FORCE SQUARE-OFF")
            return self._exit_all("SQUAREOFF_TIME")

        # ── Get current spread value ───────────────────────────────────────────
        current_spread_value = self._get_current_spread_value()
        if current_spread_value is None:
            logger.warning("⚠️ Could not get spread value — skipping this candle")
            return None

        # Compute unrealised P&L
        self.unrealised_pnl = (
            (current_spread_value - self.net_entry_cost) * self.remaining_units
        )

        logger.info(
            f"📊 Monitor | Nifty={nifty_price:.1f} | "
            f"Spread value=₹{current_spread_value:.1f} | "
            f"Entry cost=₹{self.net_entry_cost:.1f} | "
            f"Unrealised P&L=₹{self.unrealised_pnl:.0f}"
        )

        # ── Stop loss check ────────────────────────────────────────────────────
        if current_spread_value <= self.current_sl_spread_value:
            logger.info(
                f"🛑 STOP LOSS HIT: spread value ₹{current_spread_value:.1f} "
                f"<= SL ₹{self.current_sl_spread_value:.1f}"
            )
            return self._exit_all("STOP_LOSS")

        # ── Price crosses back into range (structural SL) ──────────────────────
        structural_sl = self._check_structural_sl(nifty_price)  # FIX 4: removed extra arg
        if structural_sl:
            logger.info(f"🛑 STRUCTURAL SL: Nifty re-entered opening range")
            return self._exit_all("STRUCTURAL_SL")

        # ── Level 1: Partial exit at 1× ATR ───────────────────────────────────
        if self.state == TradeState.OPEN:
            target_l1 = self._get_level1_target(nifty_price)
            if target_l1:
                logger.info(f"💰 LEVEL 1 TARGET HIT: Nifty at {nifty_price:.1f}")
                self._execute_level1_exit()

        # ── Update trailing stop ───────────────────────────────────────────────
        if self.state == TradeState.PARTIAL_EXIT and candles_df is not None:
            self._update_trailing_stop(candles_df, nifty_price)

            # Check if trailing stop hit
            if self._is_trailing_sl_hit(nifty_price):
                logger.info(f"🛑 TRAILING SL HIT at Nifty={nifty_price:.1f}")
                return self._exit_all("TRAILING_SL")

        # ── Level 3: Hard target at 1.8× ATR ──────────────────────────────────
        if self._get_level3_target_hit(nifty_price):
            logger.info(f"💰 LEVEL 3 TARGET HIT: 1.8× ATR move confirmed")
            return self._exit_all("TARGET_L3")

        return None  # Trade still open

    # ─── LEVEL 1 EXIT ─────────────────────────────────────────────────────────

    def _execute_level1_exit(self):
        """Exits 40% of position and moves SL to breakeven."""
        # FIX 1B: partial exit requires integer lots
        # With LOTS_PER_TRADE=1 and 25-unit lots, 40% = 10 units (valid).
        # Guard: if result < 1 lot (25 units), skip partial exit and
        # wait for full target instead of creating a sub-lot order.
        from config import NIFTY_LOT_SIZE as _LOT
        raw_units = int(self.total_units * LEVEL1_EXIT_PCT)
        # Round DOWN to nearest complete lot
        units_to_exit = (raw_units // _LOT) * _LOT
        if units_to_exit < _LOT:
            logger.warning(
                f"Partial exit skipped: {raw_units} units < 1 lot ({_LOT}). "
                f"Increase LOTS_PER_TRADE to enable partial exits."
            )
            return

        try:
            # FIX 2: define AND call _l1_exit_buy — previously defined but never invoked
            @retry(max_attempts=3, base_delay=0.5)
            def _l1_exit_buy():
                return self.groww.place_order(
                    trading_symbol=self.spread_info["buy_symbol"],
                    quantity=units_to_exit,
                    validity=self.groww.VALIDITY_DAY,
                    exchange=self.groww.EXCHANGE_NSE,
                    segment=self.groww.SEGMENT_FNO,
                    product=self.groww.PRODUCT_MIS,
                    order_type=self.groww.ORDER_TYPE_MARKET,
                    transaction_type=self.groww.TRANSACTION_TYPE_SELL,
                    order_reference_id=self._gen_ref_id("EXIT_L1_BUY")
                )
            _l1_exit_buy()   # FIX 2: actually call it

            # FIX 3: sell leg also gets retry protection
            @retry(max_attempts=3, base_delay=0.5)
            def _l1_exit_sell():
                return self.groww.place_order(
                    trading_symbol=self.spread_info["sell_symbol"],
                    quantity=units_to_exit,
                    validity=self.groww.VALIDITY_DAY,
                    exchange=self.groww.EXCHANGE_NSE,
                    segment=self.groww.SEGMENT_FNO,
                    product=self.groww.PRODUCT_MIS,
                    order_type=self.groww.ORDER_TYPE_MARKET,
                    transaction_type=self.groww.TRANSACTION_TYPE_BUY,
                    order_reference_id=self._gen_ref_id("EXIT_L1_SELL")
                )
            _l1_exit_sell()  # FIX 3: call the retried sell leg

            # FIX #2: Poll actual fill price from positions, not LTP snapshot
            l1_spread_value = self._get_fill_price_from_positions() or \
                              self._get_current_spread_value() or self.net_entry_cost
            l1_pnl = (l1_spread_value - self.net_entry_cost) * units_to_exit
            self.realised_pnl += l1_pnl

            self.exited_units    += units_to_exit
            self.remaining_units -= units_to_exit

            # Move SL to breakeven
            self.current_sl_spread_value = self.net_entry_cost
            self.at_breakeven = True
            self.state = TradeState.PARTIAL_EXIT

            logger.info(
                f"✅ LEVEL 1 EXIT: {units_to_exit} units @ ₹{l1_spread_value:.1f} | "
                f"L1 P&L=₹{l1_pnl:.0f} | "
                f"SL moved to breakeven (₹{self.net_entry_cost:.1f}) | "
                f"Remaining: {self.remaining_units} units"
            )

        except Exception as e:
            logger.error(f"❌ Level 1 exit failed: {e}")

    # ─── FULL EXIT ────────────────────────────────────────────────────────────

    def _exit_all(self, reason: str) -> str:
        """Exits all remaining units of both spread legs at market price."""
        if self.remaining_units <= 0:
            self.state = TradeState.CLOSED
            return reason

        try:
            # FIX 2+3: define, fix indentation, CALL _exit_buy(), wrap sell with retry
            @retry(max_attempts=3, base_delay=0.5)
            def _exit_buy():
                return self.groww.place_order(
                    trading_symbol=self.spread_info["buy_symbol"],
                    quantity=self.remaining_units,
                    validity=self.groww.VALIDITY_DAY,
                    exchange=self.groww.EXCHANGE_NSE,
                    segment=self.groww.SEGMENT_FNO,
                    product=self.groww.PRODUCT_MIS,
                    order_type=self.groww.ORDER_TYPE_MARKET,
                    transaction_type=self.groww.TRANSACTION_TYPE_SELL,
                    order_reference_id=self._gen_ref_id(f"EXIT_{reason}_BUY")
                )
            _exit_buy()   # FIX 2: actually call it

            @retry(max_attempts=3, base_delay=0.5)
            def _exit_sell():
                return self.groww.place_order(
                    trading_symbol=self.spread_info["sell_symbol"],
                    quantity=self.remaining_units,
                    validity=self.groww.VALIDITY_DAY,
                    exchange=self.groww.EXCHANGE_NSE,
                    segment=self.groww.SEGMENT_FNO,
                    product=self.groww.PRODUCT_MIS,
                    order_type=self.groww.ORDER_TYPE_MARKET,
                    transaction_type=self.groww.TRANSACTION_TYPE_BUY,
                    order_reference_id=self._gen_ref_id(f"EXIT_{reason}_SELL")
                )
            _exit_sell()  # FIX 3: retried sell leg

            # FIX #2: Use actual fill price not LTP
            final_spread_value = self._get_fill_price_from_positions() or \
                                 self._get_current_spread_value() or self.net_entry_cost
            final_pnl = (final_spread_value - self.net_entry_cost) * self.remaining_units
            self.realised_pnl += final_pnl
            self.exited_units  += self.remaining_units
            self.remaining_units = 0

            self.state       = TradeState.CLOSED
            self.exit_time   = datetime.now(IST)
            self.exit_reason = reason

            total_pnl = self.realised_pnl
            logger.info(
                f"{'💰' if total_pnl >= 0 else '🔴'} TRADE CLOSED | "
                f"Reason={reason} | "
                f"Total P&L=₹{total_pnl:.0f} | "
                f"Duration={(self.exit_time - self.entry_time).seconds // 60} min"
            )

        except Exception as e:
            logger.error(f"❌ Exit order failed for reason={reason}: {e}")
            logger.error("⚠️ MANUAL INTERVENTION REQUIRED — check Groww app immediately")

        return reason

    # ─── TRAILING STOP ────────────────────────────────────────────────────────

    def _update_trailing_stop(self, candles_df, nifty_price: float):
        """
        Updates the trailing stop using the 2-bar swing high/low method.
        For calls: trail using the last 2 candle lows.
        For puts:  trail using the last 2 candle highs.
        """
        if len(candles_df) < LEVEL2_TRAIL_CANDLES:
            return

        last_n = candles_df.tail(LEVEL2_TRAIL_CANDLES)

        if self.signal_direction == "BUY_CALL":
            new_trail = float(last_n["low"].min())
            if self.trailing_sl_price is None or new_trail > self.trailing_sl_price:
                self.trailing_sl_price = new_trail
                logger.info(f"📐 Trailing SL updated: {self.trailing_sl_price:.1f} (2-bar swing low)")
        else:
            new_trail = float(last_n["high"].max())
            if self.trailing_sl_price is None or new_trail < self.trailing_sl_price:
                self.trailing_sl_price = new_trail
                logger.info(f"📐 Trailing SL updated: {self.trailing_sl_price:.1f} (2-bar swing high)")

    def _is_trailing_sl_hit(self, nifty_price: float) -> bool:
        if self.trailing_sl_price is None:
            return False
        if self.signal_direction == "BUY_CALL":
            return nifty_price < self.trailing_sl_price
        else:
            return nifty_price > self.trailing_sl_price

    # ─── TARGET CHECKS ────────────────────────────────────────────────────────

    def _get_level1_target(self, nifty_price: float) -> bool:
        """Returns True if Nifty has moved 1× ATR20 in trade direction."""
        if not self.md.atr20 or not self.md.opening_high:
            return False
        target_move = self.md.atr20 * LEVEL1_ATR_MULTIPLE

        if self.signal_direction == "BUY_CALL":
            breakout_pt = self.md.opening_high
            return nifty_price >= breakout_pt + target_move
        else:
            breakout_pt = self.md.opening_low
            return nifty_price <= breakout_pt - target_move

    def _get_level3_target_hit(self, nifty_price: float) -> bool:
        """Returns True if Nifty has moved 1.8× ATR20 in trade direction."""
        if not self.md.atr20 or not self.md.opening_high:
            return False
        target_move = self.md.atr20 * LEVEL3_ATR_MULTIPLE

        if self.signal_direction == "BUY_CALL":
            return nifty_price >= self.md.opening_high + target_move
        else:
            return nifty_price <= self.md.opening_low - target_move

    # ─── STRUCTURAL SL ────────────────────────────────────────────────────────

    def _check_structural_sl(self, nifty_price: float) -> bool:
        """
        Returns True if price has closed back INSIDE the opening range
        — confirming a false breakout.
        """
        if not self.md.opening_high or not self.md.opening_low:
            return False
        if self.signal_direction == "BUY_CALL":
            return nifty_price < self.md.opening_high
        else:
            return nifty_price > self.md.opening_low

    # ─── HELPERS ──────────────────────────────────────────────────────────────

    def _set_initial_stop_loss(self):
        """Sets the premium-based stop loss at 25% of entry cost."""
        self.current_sl_spread_value = round(
            self.net_entry_cost * SL_PREMIUM_PCT, 2
        )
        logger.info(
            f"🛡️ Initial SL set: spread value drops below "
            f"₹{self.current_sl_spread_value:.2f}/unit "
            f"(25% of ₹{self.net_entry_cost:.2f})"
        )

    def _record_entry_prices(self):
        """
        Uses get_order_details(order_id) for exact fill prices —
        not get_positions_for_user() which can grab wrong average
        if user has pre-existing positions in the same symbol.
        """
        try:
            buy_details  = self.groww.get_order_details(
                groww_order_id=self.buy_order_id,
                segment=self.groww.SEGMENT_FNO
            )
            sell_details = self.groww.get_order_details(
                groww_order_id=self.sell_order_id,
                segment=self.groww.SEGMENT_FNO
            )

            buy_fill  = float(buy_details.get("average_price")  or 0)
            sell_fill = float(sell_details.get("average_price") or 0)

            if buy_fill > 0 and sell_fill > 0:
                self.buy_entry_price  = buy_fill
                self.sell_entry_price = sell_fill
                self.net_entry_cost   = round(buy_fill - sell_fill, 2)
                logger.info(
                    f"📋 Exact fill prices | BUY=₹{buy_fill} | "
                    f"SELL=₹{sell_fill} | Net=₹{self.net_entry_cost}"
                )
            else:
                logger.warning("fill prices not yet available — using LTP estimate")
                self.net_entry_cost = self.spread_info["net_spread_cost"]

        except Exception as e:
            logger.warning(f"Could not fetch fill prices via order_details: {e}")
            self.net_entry_cost = self.spread_info["net_spread_cost"]

    def _get_current_spread_value(self) -> Optional[float]:
        """
        Gets current spread value = buy_leg_ltp - sell_leg_ltp.
        """
        try:
            buy_ltp  = self.md.get_option_ltp(self.spread_info["buy_symbol"])
            sell_ltp = self.md.get_option_ltp(self.spread_info["sell_symbol"])
            if buy_ltp and sell_ltp:
                return round(max(buy_ltp - sell_ltp, 0), 2)
        except Exception as e:
            logger.warning(f"Could not get spread value: {e}")
        return None


    def _wait_for_fills(self, timeout: int = 15) -> bool:
        """
        FIX #1: Polls Groww order status until both legs show COMPLETE.
        Returns True if both filled within timeout, False otherwise.
        """
        import time as _time
        deadline = _time.time() + timeout
        filled = {"buy": False, "sell": False}

        while _time.time() < deadline:
            try:
                for oid, key in [(self.buy_order_id, "buy"), (self.sell_order_id, "sell")]:
                    if filled[key] or not oid:
                        continue
                    resp   = self.groww.get_order_details(groww_order_id=oid,
                                                          segment=self.groww.SEGMENT_FNO)
                    status = resp.get("status", "").upper()
                    if status in ("COMPLETE", "FILLED", "TRADED"):
                        filled[key] = True
                        logger.info(f"✅ {key.upper()} leg filled: {oid}")
                    elif "REJECT" in status or "CANCEL" in status:
                        # Exchange hard-rejected — no point polling further
                        logger.error(f"🚫 Order {oid} {status} — exchange rejection")
                        return False

                if filled["buy"] and filled["sell"]:
                    return True

            except Exception as e:
                err_type = _classify_error(e)
                if err_type == "HARD_REJECT":
                    logger.error(f"🚫 Hard rejection during fill poll: {e}")
                    return False
                logger.warning(f"Fill poll transient error (retrying): {e}")

            _time.sleep(1)

        logger.error(
            f"⏱ Fill timeout after {timeout}s | buy_filled={filled['buy']} | "
            f"sell_filled={filled['sell']}"
        )
        return False

    def _emergency_exit_filled_legs(self):
        """
        Exits only the exact filled_quantity from exchange — not self.total_units.
        Prevents accidentally creating a naked position in the wrong direction
        if a leg only partially filled before the error occurred.
        """
        logger.error("🚨 EMERGENCY EXIT — closing any filled leg")
        try:
            # Fetch actual filled quantity for BUY leg
            if self.buy_order_id:
                try:
                    d = self.groww.get_order_details(
                        groww_order_id=self.buy_order_id,
                        segment=self.groww.SEGMENT_FNO
                    )
                    filled_buy = int(d.get("filled_quantity") or 0)
                except Exception:
                    filled_buy = self.total_units  # conservative fallback

                if filled_buy > 0:
                    self.groww.place_order(
                        trading_symbol=self.spread_info["buy_symbol"],
                        quantity=filled_buy,
                    validity=self.groww.VALIDITY_DAY,
                    exchange=self.groww.EXCHANGE_NSE,
                    segment=self.groww.SEGMENT_FNO,
                    product=self.groww.PRODUCT_MIS,
                    order_type=self.groww.ORDER_TYPE_MARKET,
                    transaction_type=self.groww.TRANSACTION_TYPE_SELL,
                    order_reference_id=self._gen_ref_id("EMRG_BUY")
                )
        except Exception as e:
            logger.error(f"Emergency BUY leg exit failed: {e}")

        try:
            if self.sell_order_id:
                try:
                    d2 = self.groww.get_order_details(
                        groww_order_id=self.sell_order_id,
                        segment=self.groww.SEGMENT_FNO
                    )
                    filled_sell = int(d2.get("filled_quantity") or 0)
                except Exception:
                    filled_sell = self.total_units

                if filled_sell > 0:
                    self.groww.place_order(
                        trading_symbol=self.spread_info["sell_symbol"],
                        quantity=filled_sell,
                    validity=self.groww.VALIDITY_DAY,
                    exchange=self.groww.EXCHANGE_NSE,
                    segment=self.groww.SEGMENT_FNO,
                    product=self.groww.PRODUCT_MIS,
                    order_type=self.groww.ORDER_TYPE_MARKET,
                    transaction_type=self.groww.TRANSACTION_TYPE_BUY,
                    order_reference_id=self._gen_ref_id("EMRG_SELL")
                )
        except Exception as e:
            logger.error(f"Emergency SELL leg exit failed: {e}")

        self.state = TradeState.IDLE
        logger.error("⚠️ Emergency exit complete — CHECK GROWW APP IMMEDIATELY")

    def _get_fill_price_from_positions(self) -> Optional[float]:
        """
        FIX #2: Fetches actual fill prices from positions after an exit order.
        Returns spread value based on real fill prices, not LTP snapshot.
        """
        try:
            import time as _time
            _time.sleep(0.8)  # brief wait for positions to update
            positions = self.groww.get_positions_for_user(
                segment=self.groww.SEGMENT_FNO
            )
            pos_list = positions.get("positions", [])
            buy_sym  = self.spread_info["buy_symbol"]
            sell_sym = self.spread_info["sell_symbol"]

            buy_sell_price  = None
            sell_buy_price  = None

            for pos in pos_list:
                sym = pos.get("trading_symbol", "")
                # After exit, the sell-price of the buy leg is the exit fill
                if sym == buy_sym:
                    buy_sell_price = float(pos.get("sell_average_price") or 0) or None
                if sym == sell_sym:
                    sell_buy_price = float(pos.get("buy_average_price") or 0) or None

            if buy_sell_price and sell_buy_price:
                return round(buy_sell_price - sell_buy_price, 2)

        except Exception as e:
            logger.warning(f"Could not get fill price from positions: {e}")
        return None


    def _verify_sell_leg_filled(self) -> bool:
        """
        FIX 2B: Confirms the SELL leg reached COMPLETE status.
        If it was rejected (insufficient margin, strike out of range, etc.)
        we must flatten the BUY leg to avoid holding a naked long option.
        Returns True only if sell leg is confirmed filled.
        """
        if not self.sell_order_id:
            return False
        try:
            resp   = self.groww.get_order_details(
                groww_order_id=self.sell_order_id,
                segment=self.groww.SEGMENT_FNO,
            )
            status = resp.get("status", "").upper()
            if status in ("COMPLETE", "FILLED", "TRADED"):
                return True
            reason = resp.get("rejection_reason") or resp.get("status_message", "")
            logger.error(
                f"🚨 SELL leg status={status} | reason={reason} | "
                f"order_id={self.sell_order_id}"
            )
            return False
        except Exception as e:
            logger.error(f"❌ Could not verify sell leg: {e}")
            return False   # conservative — assume not filled

    def _handle_partial_entry(self):
        """Emergency: if one leg failed, exit the other immediately."""
        logger.error("🚨 PARTIAL ENTRY — attempting emergency exit of filled leg")
        try:
            if self.buy_order_id:
                self.groww.cancel_order(
                    segment=self.groww.SEGMENT_FNO,
                    groww_order_id=self.buy_order_id
                )
            if self.sell_order_id:
                self.groww.cancel_order(
                    segment=self.groww.SEGMENT_FNO,
                    groww_order_id=self.sell_order_id
                )
        except Exception as e:
            logger.error(f"Emergency cancel failed: {e} — check Groww app manually")
        self._emergency_exit_filled_legs()
        self.state = TradeState.IDLE

    def _gen_ref_id(self, tag: str) -> str:
        """Generates a unique order reference ID."""
        ts = datetime.now(IST).strftime("%H%M%S%f")[:13]
        return f"ORB-{tag[:8]}-{ts}"

    # ─── SUMMARY ──────────────────────────────────────────────────────────────

    def get_trade_summary(self) -> dict:
        """Returns a summary dict for logging."""
        return {
            "signal":        self.signal_direction,
            "state":         self.state.value,
            "buy_symbol":    self.spread_info["buy_symbol"] if self.spread_info else None,
            "sell_symbol":   self.spread_info["sell_symbol"] if self.spread_info else None,
            "entry_cost":    self.net_entry_cost,
            "total_units":   self.total_units,
            "exited_units":  self.exited_units,
            "remaining":     self.remaining_units,
            "realised_pnl":  round(self.realised_pnl, 2),
            "unrealised_pnl": round(self.unrealised_pnl, 2),
            "entry_time":    self.entry_time.strftime("%H:%M:%S") if self.entry_time else None,
            "exit_time":     self.exit_time.strftime("%H:%M:%S") if self.exit_time else None,
            "exit_reason":   self.exit_reason,
        }
