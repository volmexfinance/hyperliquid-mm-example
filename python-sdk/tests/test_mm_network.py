"""Offline tests for mm.py network selection, key loading and collateral by account mode.

Run from python-sdk/: .venv/bin/python -m unittest discover tests
"""

import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import mm  # noqa: E402
from accounting import free_usdc  # noqa: E402
from hyperliquid.utils import constants  # noqa: E402


class NetworkArgsTest(unittest.TestCase):
    def test_testnet_is_default(self):
        args = mm.parse_args([])
        self.assertEqual(args.network, "testnet")
        self.assertEqual(args.base_url, constants.TESTNET_API_URL)
        self.assertEqual((args.dex, args.coin), ("bart", "bart:AAPL"))
        self.assertEqual(args.key_names, ("TEST_HYPERLIQUID_PRIVATE_KEY", "TEST_HYPERLIQUID_KEY"))
        self.assertEqual((args.start_bps, args.end_bps), (Decimal("2"), Decimal("25")))
        self.assertEqual((args.notional_usd, args.min_free_usd, args.max_position_usd), (Decimal("2000"), Decimal("2500"), Decimal("25000")))

    def test_mainnet_defaults_to_bviv_with_wider_smaller_ladder(self):
        args = mm.parse_args(["--network", "mainnet"])
        self.assertEqual(args.base_url, constants.MAINNET_API_URL)
        self.assertEqual((args.dex, args.coin), ("mkts", "mkts:BVIV"))
        self.assertEqual(args.key_names, ("HYPERLIQUID_PRIVATE_KEY", "PROD_HYPERLIQUID_MM_PRIVATE_KEY"))
        self.assertEqual((args.start_bps, args.end_bps), (Decimal("10"), Decimal("50")))
        self.assertEqual((args.notional_usd, args.min_free_usd, args.max_position_usd), (Decimal("500"), Decimal("100"), Decimal("2500")))

    def test_explicit_flags_override_network_defaults(self):
        args = mm.parse_args(["--network", "mainnet", "--start-bps", "4", "--notional-usd", "100", "--dex", "km", "--coin", "km:AAPL"])
        self.assertEqual((args.start_bps, args.notional_usd, args.dex, args.coin), (Decimal("4"), Decimal("100"), "km", "km:AAPL"))
        self.assertEqual(args.end_bps, Decimal("50"))

    def test_coin_must_match_dex(self):
        with self.assertRaises(SystemExit):
            mm.parse_args(["--network", "mainnet", "--coin", "bart:AAPL"])


class KeyLoadingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = Path(self.tmp.name) / ".env"
        self.env.write_text("TEST_HYPERLIQUID_PRIVATE_KEY=0xtestnetkey\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_testnet_reads_test_key(self):
        keys = mm.NETWORKS["testnet"].key_names
        self.assertEqual(mm.load_private_key(self.env, keys, required=True), "0xtestnetkey")

    def test_mainnet_never_falls_back_to_test_key(self):
        keys = mm.NETWORKS["mainnet"].key_names
        self.assertIsNone(mm.load_private_key(self.env, keys, required=False))
        with self.assertRaises(RuntimeError):
            mm.load_private_key(self.env, keys, required=True)

    def test_missing_explicit_file_is_an_error_for_live_runs(self):
        with self.assertRaises(RuntimeError):
            mm.load_private_key(Path(self.tmp.name) / "missing.env", mm.NETWORKS["mainnet"].key_names, required=True)


class FakeInfo:
    def spot_user_state(self, address):
        return {"balances": [{"coin": "USDC", "total": "1000", "hold": "250"}]}

    def user_state(self, address, dex=""):
        return {"withdrawable": "40" if dex == "mkts" else "0"}


class FreeUsdcTest(unittest.TestCase):
    def test_unified_and_portfolio_accounts_use_spot_balance(self):
        for mode in ("unifiedAccount", "portfolioMargin"):
            self.assertEqual(free_usdc(FakeInfo(), "0xabc", "mkts", mode), Decimal("750"))

    def test_standard_account_uses_the_dex_balance(self):
        self.assertEqual(free_usdc(FakeInfo(), "0xabc", "mkts", "default"), Decimal("40"))


if __name__ == "__main__":
    unittest.main()
