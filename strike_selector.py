"""
strike_selector.py — Strike Selection Module
Finds the correct ATM strike and spread legs (buy + sell) from
the live Groww option chain. Validates liquidity before returning.
"""

import logging
from datetime import date, timedelta
from typing import Optional, Tuple
from growwapi import GrowwAPI

from config import (
    NIFTY_SYMBOL, EXCHANGE,
    STRIKE_INTERVAL, SPREAD_WIDTH,
    MAX_SPREAD_COST, NIFTY_LOT_SIZE,
)

logger = logging.getLogger(__name__)


class StrikeSelector:
    """
    Selects the ATM strike and spread leg for a given signal.

    For BUY_CALL:
        Leg 1 (BUY):  ATM Call (CE)
        Leg 2 (SELL): ATM+50 Call (CE)

    For BUY_PUT:
        Leg 1 (BUY):  ATM Put (PE)
        Leg 2 (SELL): ATM-50 Put (PE)
    """

    def __init__(self, groww: GrowwAPI):
        self.groww = groww

    # ─── PUBLIC ───────────────────────────────────────────────────────────────

    def get_spread_strikes(
        self,
        signal: str,             # "BUY_CALL" or "BUY_PUT"
        spot_price: float,
    ) -> Optional[dict]:
        """
        Returns a dict with both spread leg symbols, strikes, and estimated cost.
        Returns None if no valid spread found or cost too high.

        Example return:
        {
            "expiry":          "2026-06-12",
            "atm_strike":      24000,
            "otm_strike":      24050,
            "buy_symbol":      "NIFTY26JUN24000CE",
            "sell_symbol":     "NIFTY26JUN24050CE",
            "buy_ltp":         145.0,
            "sell_ltp":        95.0,
            "net_spread_cost": 50.0,       # per unit
            "total_cost":      3750.0,     # per lot (75 units)
            "max_loss":        3750.0,     # same as total cost for long spread
        }
        """
        try:
            atm_strike = self._get_atm_strike(spot_price)
            expiry     = self._get_nearest_weekly_expiry()
            option_type = "CE" if signal == "BUY_CALL" else "PE"

            if signal == "BUY_CALL":
                buy_strike  = atm_strike
                sell_strike = atm_strike + SPREAD_WIDTH
            else:
                buy_strike  = atm_strike
                sell_strike = atm_strike - SPREAD_WIDTH

            # Build trading symbols
            buy_symbol  = self._build_symbol(expiry, buy_strike,  option_type)
            sell_symbol = self._build_symbol(expiry, sell_strike, option_type)

            logger.info(f"🎯 Spread: BUY {buy_symbol} | SELL {sell_symbol}")

            # Get live LTPs from option chain
            chain = self._fetch_option_chain(expiry)
            if chain is None:
                return None

            buy_ltp  = self._get_ltp_from_chain(chain, buy_strike,  option_type)
            sell_ltp = self._get_ltp_from_chain(chain, sell_strike, option_type)

            if buy_ltp is None or sell_ltp is None:
                logger.error(f"❌ Could not get LTP for spread legs")
                return None

            net_cost   = buy_ltp - sell_ltp
            total_cost = round(net_cost * NIFTY_LOT_SIZE, 2)

            # Validate spread cost is within budget
            if total_cost > MAX_SPREAD_COST:
                logger.warning(
                    f"⚠️ Spread too expensive: ₹{total_cost} > max ₹{MAX_SPREAD_COST}. "
                    f"Skipping trade."
                )
                return None

            if net_cost <= 0:
                logger.warning(f"⚠️ Invalid spread: net cost = ₹{net_cost} (buy_ltp <= sell_ltp)")
                return None

            spread_info = {
                "expiry":          expiry,
                "atm_strike":      buy_strike,
                "otm_strike":      sell_strike,
                "option_type":     option_type,
                "buy_symbol":      buy_symbol,
                "sell_symbol":     sell_symbol,
                "buy_ltp":         buy_ltp,
                "sell_ltp":        sell_ltp,
                "net_spread_cost": round(net_cost, 2),
                "total_cost":      total_cost,
                "max_loss":        total_cost,   # max loss = total cost paid (long spread)
            }

            logger.info(
                f"✅ Spread ready: Buy {buy_symbol}@{buy_ltp} | "
                f"Sell {sell_symbol}@{sell_ltp} | "
                f"Net cost/unit=₹{net_cost:.1f} | "
                f"Total=₹{total_cost}"
            )

            return spread_info

        except Exception as e:
            logger.error(f"❌ Strike selection failed: {e}")
            return None

    # ─── PRIVATE ──────────────────────────────────────────────────────────────

    def _get_atm_strike(self, spot_price: float) -> int:
        """Rounds spot price to nearest STRIKE_INTERVAL (50 for Nifty)."""
        return int(round(spot_price / STRIKE_INTERVAL) * STRIKE_INTERVAL)

    def _get_nearest_weekly_expiry(self) -> str:
        """
        Returns the nearest Thursday that is NOT today.
        Format: YYYY-MM-DD
        """
        today = date.today()
        days_until_thursday = (3 - today.weekday()) % 7  # Thursday = weekday 3

        if days_until_thursday == 0:
            # Today is Thursday (expiry) — skip to next week
            days_until_thursday = 7

        expiry = today + timedelta(days=days_until_thursday)
        logger.info(f"📅 Using expiry: {expiry}")
        return expiry.strftime("%Y-%m-%d")

    def _build_symbol(self, expiry: str, strike: int, option_type: str) -> str:
        """
        FIX #18: Fetch the actual symbol from the option chain instead of
        constructing it manually. Manual construction risks missing the DD
        component or using a wrong format for weekly vs monthly expiries.
        Falls back to manual construction only if chain lookup fails.
        """
        try:
            chain = self._fetch_option_chain(expiry)
            if chain:
                for item in chain:
                    if (int(item.get('strike_price', 0)) == strike and
                            item.get('option_type', '').upper() == option_type):
                        sym = item.get('trading_symbol')
                        if sym:
                            logger.info(f'Symbol from chain: {sym}')
                            return sym
        except Exception as e:
            logger.warning(f'Chain symbol lookup failed: {e} — using manual build')

        # Fallback: manual construction (verify format with verify.py first)
        expiry_date = date.fromisoformat(expiry)
        year  = expiry_date.strftime('%y')
        month = expiry_date.strftime('%b').upper()
        day   = expiry_date.strftime('%d')
        symbol = f'NIFTY{year}{month}{day}{strike}{option_type}'
        logger.warning(f'Manual symbol (VERIFY FORMAT): {symbol}')
        return symbol

    def _fetch_option_chain(self, expiry: str) -> Optional[list]:
        """Fetches full option chain from Groww for Nifty."""
        try:
            chain_response = self.groww.get_option_chain(
                exchange=self.groww.EXCHANGE_NSE,
                underlying=NIFTY_SYMBOL,
                expiry_date=expiry
            )
            return chain_response.get("data", [])
        except Exception as e:
            logger.error(f"❌ Failed to fetch option chain: {e}")
            return None

    def _get_ltp_from_chain(
        self,
        chain: list,
        strike: int,
        option_type: str
    ) -> Optional[float]:
        """
        Finds the LTP for a specific strike+type from the option chain.
        """
        for item in chain:
            if (int(item.get("strike_price", 0)) == strike and
                    item.get("option_type", "").upper() == option_type):
                ltp = item.get("ltp") or item.get("last_price")
                if ltp:
                    return float(ltp)

        logger.warning(f"⚠️ Strike {strike}{option_type} not found in chain")
        return None
