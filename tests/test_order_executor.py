"""
Tests de OrderExecutor.buy() con un cliente CLOB falso.

En py-clob-client-v2, OrderArgs.size son SHARES: un BUY cuesta size × price.
Estos tests verifican que el bot convierte el monto en USD a shares y que la
Position guardada coincide con la orden enviada.

Correr:  python -m unittest discover -s tests
"""
import os
import sys
import tempfile
import unittest

# Aislar el import: sin claves reales, log/estado en una carpeta temporal.
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="pm-test-")
os.environ["DRY_RUN"] = "true"
os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import polymarket_agent as pa  # noqa: E402


class FakeClob:
    def __init__(self, min_order_size="5"):
        self.min_order_size = min_order_size
        self.orders = []

    def get_tick_size(self, token_id):
        return "0.001"

    def get_order_book(self, token_id):
        return {"asset_id": token_id, "min_order_size": self.min_order_size}

    def create_and_post_order(self, order):
        self.orders.append(order)
        return {"success": True}


def make_opp(price=0.40, bet=5.0):
    market = pa.Market(
        condition_id="0xabc", question="Will it happen?", category="test",
        volume=1000.0, liquidity=1000.0, end_date="2026-12-31", outcomes=[],
    )
    return pa.Opportunity(
        market=market, outcome_name="Yes", token_id="123", market_price=price,
        ai_probability=0.6, edge=0.2, kelly_fraction=0.1, bet_size_usd=bet,
        reasoning="", confidence="HIGH",
    )


@unittest.skipUnless(pa.CLOB_AVAILABLE, "py-clob-client-v2 no instalado")
class BuySizingTest(unittest.TestCase):
    def setUp(self):
        self._dry = pa.CONFIG["DRY_RUN"]
        pa.CONFIG["DRY_RUN"] = False
        self.executor = pa.OrderExecutor()  # sin PRIVATE_KEY → clob None
        self.clob = FakeClob()
        self.executor.clob = self.clob

    def tearDown(self):
        pa.CONFIG["DRY_RUN"] = self._dry

    def test_size_is_shares_not_usd(self):
        pos = self.executor.buy(make_opp(price=0.40, bet=5.0))
        self.assertEqual(len(self.clob.orders), 1)
        order = self.clob.orders[0]
        self.assertEqual(order.side, "BUY")
        self.assertAlmostEqual(order.price, 0.401)
        self.assertAlmostEqual(order.size, 12.47)            # 5 / 0.401
        self.assertAlmostEqual(order.price * order.size, 5.0, delta=0.01)

        # La Position refleja exactamente lo enviado
        self.assertAlmostEqual(pos.shares, order.size)
        self.assertAlmostEqual(pos.entry_price, order.price)
        self.assertAlmostEqual(pos.size_usd, order.price * order.size, places=4)

    def test_larger_bet_various_prices(self):
        for price, bet in [(0.05, 7.5), (0.73, 20.0), (0.97, 5.0)]:
            self.clob.orders.clear()
            pos = self.executor.buy(make_opp(price=price, bet=bet))
            order = self.clob.orders[0]
            self.assertAlmostEqual(order.price * order.size, bet, delta=0.01)
            self.assertAlmostEqual(pos.shares, order.size)

    def test_below_min_order_size_is_skipped(self):
        self.clob.min_order_size = "15"
        pos = self.executor.buy(make_opp(price=0.40, bet=5.0))  # 12.47 shares
        self.assertIsNone(pos)
        self.assertEqual(self.clob.orders, [])

    def test_orderbook_failure_does_not_block(self):
        def boom(_):
            raise RuntimeError("network")
        self.clob.get_order_book = boom
        pos = self.executor.buy(make_opp())
        self.assertIsNotNone(pos)
        self.assertEqual(len(self.clob.orders), 1)


class DryRunUnchangedTest(unittest.TestCase):
    def test_dry_run_position(self):
        self.assertTrue(pa.CONFIG["DRY_RUN"])
        pos = pa.OrderExecutor().buy(make_opp(price=0.40, bet=5.0))
        self.assertAlmostEqual(pos.size_usd, 5.0)
        self.assertAlmostEqual(pos.shares, 12.5)
        self.assertAlmostEqual(pos.entry_price, 0.40)


if __name__ == "__main__":
    unittest.main()
