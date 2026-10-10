#!/usr/bin/env python3
"""Read-only testnet health summary for the Bart AAPL market and both wallets."""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal

from hyperliquid.info import Info
from hyperliquid.utils import constants

from accounting import COIN, DEX, MAKER_ADDRESS, TRADER_ADDRESS, fees_in_range, liquidation_distance_pct, position_for, read_with_retries, spot_usdc


def money(value: Decimal) -> str:
    return f"${value:,.2f}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only testnet health summary for Bart AAPL")
    parser.add_argument(
        "--fee-window-hours",
        type=float,
        default=2.0,
        help="measure fees over this trailing window and project them to a daily rate (default: 2 hours)",
    )
    args = parser.parse_args()
    if args.fee_window_hours <= 0:
        parser.error("--fee-window-hours must be positive")
    return args


def run_status(fee_window_hours: float) -> int:
    # No constructor-time network lookup: every query below is retried explicitly.
    info = Info(base_url=constants.TESTNET_API_URL, skip_ws=True, spot_meta={"tokens": [], "universe": []}, timeout=10)
    now_ms = int(time.time() * 1000)
    metadata = read_with_retries("metaAndAssetCtxs", lambda: info.post("/info", {"type": "metaAndAssetCtxs", "dex": DEX}))
    meta, contexts = metadata
    market_index = next((i for i, asset in enumerate(meta.get("universe", [])) if asset.get("name") == COIN), None)
    if market_index is None or market_index >= len(contexts):
        raise RuntimeError(f"{COIN} metadata/context is unavailable on testnet")
    oracle = Decimal(str(contexts[market_index].get("oraclePx", "0")))
    if oracle <= 0:
        raise RuntimeError(f"{COIN} oracle is unavailable")

    wallet_rows: list[tuple[str, str, Decimal, Decimal, Decimal]] = []
    print(
        f"STATUS endpoint={constants.TESTNET_API_URL} dex={DEX} coin={COIN} fee_window_hours={fee_window_hours:g} "
        f"utc={datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        flush=True,
    )
    print(f"MARKET oracle={oracle} maxLeverage={meta['universe'][market_index].get('maxLeverage')} marginMode={meta['universe'][market_index].get('marginMode')} szDecimals={meta['universe'][market_index].get('szDecimals')}", flush=True)

    for label, address in (("maker", MAKER_ADDRESS), ("trader", TRADER_ADDRESS)):
        total, held, free = spot_usdc(info, address)
        state = read_with_retries(f"clearinghouseState {label}", lambda address=address: info.user_state(address, dex=DEX))
        position = position_for(state, COIN)
        size = Decimal(str(position.get("szi", "0")))
        mark = Decimal(str(contexts[market_index].get("markPx", oracle)))
        notional = size * mark
        liq_px = Decimal(str(position.get("liquidationPx", "0")))
        distance = liquidation_distance_pct(position, size, oracle)
        distance_text = "n/a" if distance is None else f"{distance:.2f}%"
        print(
            f"WALLET name={label} address={address} spot_usdc_total={money(total)} held={money(held)} free={money(free)} "
            f"position={size} notional={money(notional)} liquidation_px={liq_px} liquidation_distance={distance_text}",
            flush=True,
        )
        wallet_rows.append((label, address, total, held, free))

    # Info.l2_snapshot resolves coin names through SDK metadata; the read-only
    # client intentionally skipped constructor lookups, so use the raw HIP-3 API.
    book = read_with_retries("l2Book", lambda: info.post("/info", {"type": "l2Book", "coin": COIN}))
    levels = book.get("levels", [[], []])
    bids = levels[0] if levels else []
    asks = levels[1] if len(levels) > 1 else []
    bid_notional = sum((Decimal(str(level["px"])) * Decimal(str(level["sz"])) for level in bids), Decimal(0))
    ask_notional = sum((Decimal(str(level["px"])) * Decimal(str(level["sz"])) for level in asks), Decimal(0))
    best_bid = Decimal(str(bids[0]["px"])) if bids else Decimal(0)
    best_ask = Decimal(str(asks[0]["px"])) if asks else Decimal(0)
    bid_bps = (best_bid - oracle) / oracle * Decimal(10_000) if best_bid else Decimal(0)
    ask_bps = (best_ask - oracle) / oracle * Decimal(10_000) if best_ask else Decimal(0)
    print(
        f"BOOK oracle={oracle} best_bid={best_bid} bid_vs_oracle_bps={bid_bps:.2f} best_ask={best_ask} ask_vs_oracle_bps={ask_bps:.2f} "
        f"bid_levels={len(bids)} ask_levels={len(asks)} bid_notional={money(bid_notional)} ask_notional={money(ask_notional)}",
        flush=True,
    )

    window_ms = int(fee_window_hours * 60 * 60 * 1000)
    start_ms = now_ms - window_ms
    daily_scale = Decimal(str(24 / fee_window_hours))
    observed_fees: dict[str, Decimal] = {}
    projected_daily_fees: dict[str, Decimal] = {}
    for label, address in (("maker", MAKER_ADDRESS), ("trader", TRADER_ADDRESS)):
        amount, count = fees_in_range(info, address, start_ms, now_ms)
        observed_fees[label] = amount
        projected_daily_fees[label] = amount * daily_scale
        print(
            f"FEES_WINDOW window_hours={fee_window_hours:g} wallet={label} observed={money(amount)} fills={count}",
            flush=True,
        )
        print(
            f"PROJECTED_DAILY_FEES window_hours={fee_window_hours:g} wallet={label} rate={money(projected_daily_fees[label])}",
            flush=True,
        )

    combined_observed = sum(observed_fees.values(), Decimal(0))
    combined_daily_rate = sum(projected_daily_fees.values(), Decimal(0))
    print(
        f"FEES_WINDOW window_hours={fee_window_hours:g} wallet=combined observed={money(combined_observed)}",
        flush=True,
    )
    print(
        f"PROJECTED_DAILY_FEES window_hours={fee_window_hours:g} wallet=combined rate={money(combined_daily_rate)}",
        flush=True,
    )

    combined_free = sum((row[4] for row in wallet_rows), Decimal(0))
    runway = "infinite" if combined_daily_rate <= 0 else f"{combined_free / combined_daily_rate:.2f}"
    print(
        f"RUNWAY combined_free_usdc={money(combined_free)} projected_daily_fee_rate={money(combined_daily_rate)} projected_days={runway}",
        flush=True,
    )
    return 0


def main() -> int:
    try:
        args = parse_args()
        return run_status(args.fee_window_hours)
    except Exception as exc:
        print(f"STATUS_ERROR type={type(exc).__name__} message={exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
