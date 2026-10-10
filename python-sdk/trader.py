#!/usr/bin/env python3
"""Testnet-only randomized IOC activity trader for bart:AAPL."""

from __future__ import annotations

import argparse
import json
import logging
import math
import random
import signal
import threading
import time
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path
from typing import Any, Callable

from dotenv import dotenv_values
from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

from accounting import MAKER_ADDRESS, fees_in_range, liquidation_distance_pct, position_for, spot_usdc, try_add_isolated_margin


DEX = "bart"
COIN = "bart:AAPL"
TESTNET_URL = constants.TESTNET_API_URL
# The .env in the directory that holds this repo (unchanged when the bots moved into python-sdk/).
ENV_FILE = Path(__file__).resolve().parents[2] / ".env"
KEY_NAME = "TEST_HYPERLIQUID_TRADER_PRIVATE_KEY"
MARGIN_RESERVE = Decimal("0.90")
LOG = logging.getLogger("hyperliquid_trader")
STOP = threading.Event()

REGIME_MULTIPLIERS = {"quiet": 0.3, "normal": 1.0, "busy": 2.5}
# Stationary regime shares (48%, 29.6%, 22.4%) make the weighted rate exactly
# 1.0x: .48*.3 + .296*1 + .224*2.5 = 1.0. Durations are 5-20 minutes.
REGIME_WEIGHTS = {"quiet": 48, "normal": 29.6, "busy": 22.4}
REGIME_TRANSITIONS = {
    "quiet": (("normal", 0.575), ("busy", 0.425)),
    "normal": (("quiet", 0.9324324324324325), ("busy", 0.06756756756756757)),
    "busy": (("quiet", 0.9107142857142857), ("normal", 0.08928571428571429)),
}
BURST_PROBABILITY = 0.10
BURST_TRADES_MIN = 2
BURST_TRADES_MAX = 5
BURST_GAP_MIN_SECONDS = 1.0
BURST_GAP_MAX_SECONDS = 8.0
BURST_RATE_COMPENSATION = 1.25  # 1 + P(burst) * (mean burst size - 1)
HUMAN_SIZE_PROBABILITY = 0.20
HUMAN_SIZES = (Decimal("1"), Decimal("2"), Decimal("5"), Decimal("10"))
LOGNORMAL_MEDIAN_USD = 40.0
LOGNORMAL_SIGMA = 0.52
MAKER_FEE_BPS = Decimal("3")
TRADER_FEE_BPS = Decimal("9")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Testnet-only randomized bart:AAPL IOC trader")
    parser.add_argument("--dry-run", action="store_true", help="show a sample order or simulation; do not sign or submit")
    parser.add_argument("--once", action="store_true", help="submit one randomized trade and exit")
    parser.add_argument("--simulate-minutes", type=int, help="simulate organic order flow for N minutes (requires --dry-run)")
    parser.add_argument("--seed", type=int, help="seed random choices for reproducible dry-runs and simulations")
    parser.add_argument("--env-file", type=Path, default=ENV_FILE, help=f"key file (default: {ENV_FILE})")
    parser.add_argument("--account-address", help="trading account if the key is an authorized agent key")
    parser.add_argument("--interval-seconds", type=float, default=30.0, help="target mean interval between orders before the volume regime multiplier")
    parser.add_argument("--min-notional-usd", type=Decimal, default=Decimal("10"), help="lower clip for log-normal order notional")
    parser.add_argument("--max-notional-usd", type=Decimal, default=Decimal("400"), help="upper clip for log-normal order notional")
    parser.add_argument("--flatten-every-min", type=int, default=3, help="minimum random trades per flatten cycle")
    parser.add_argument("--flatten-every-max", type=int, default=12, help="maximum random trades per flatten cycle")
    parser.add_argument("--max-position-usd", type=Decimal, default=Decimal("15000"), help="configured absolute position cap")
    parser.add_argument("--min-free-usd", type=Decimal, default=Decimal("1500"), help="retain this much free Spot USDC when adding isolated margin")
    parser.add_argument("--slippage-percent", type=Decimal, default=Decimal("1"), help="IOC price cap from the touch (default: 1%%)")
    parser.add_argument("--fee-window-hours", type=float, default=2.0, help="measure fees over this trailing window and scale to a daily rate")
    parser.add_argument("--daily-fee-budget-usd", type=Decimal, default=Decimal("250"), help="combined projected daily fee budget for both wallets")
    parser.add_argument("--fee-check-seconds", type=float, default=600.0, help="fee-budget refresh interval")
    parser.add_argument("--fee-pause-multiplier", type=Decimal, default=Decimal("1.5"), help="pause threshold as a multiple of the daily fee budget")
    args = parser.parse_args()
    if args.simulate_minutes is not None and args.simulate_minutes <= 0:
        parser.error("--simulate-minutes must be positive")
    if args.simulate_minutes is not None and not args.dry_run:
        parser.error("--simulate-minutes requires --dry-run")
    if args.interval_seconds <= 0:
        parser.error("--interval-seconds must be positive")
    if args.min_notional_usd <= 0 or args.max_notional_usd < args.min_notional_usd:
        parser.error("require 0 < --min-notional-usd <= --max-notional-usd")
    if args.flatten_every_min < 1 or args.flatten_every_max < args.flatten_every_min:
        parser.error("require 1 <= --flatten-every-min <= --flatten-every-max")
    if args.max_position_usd <= 0:
        parser.error("--max-position-usd must be positive")
    if args.min_free_usd <= 0:
        parser.error("--min-free-usd must be positive")
    if args.slippage_percent <= 0 or args.slippage_percent > 10:
        parser.error("--slippage-percent must be greater than 0 and at most 10")
    if args.fee_window_hours <= 0:
        parser.error("--fee-window-hours must be positive")
    if args.daily_fee_budget_usd <= 0 or args.fee_check_seconds < 60 or args.fee_pause_multiplier <= 1:
        parser.error("fee budget must be positive, fee checks at least 60 seconds, and pause multiplier greater than 1")
    return args


def load_key(path: Path) -> str:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise RuntimeError(f"The requested env file does not exist: {resolved}")
    try:
        value = dotenv_values(resolved).get(KEY_NAME)
    except OSError:
        value = None
    if not value or not value.strip():
        raise RuntimeError(f"{KEY_NAME} was not found in {resolved}")
    return value.strip()


def read_with_retries(label: str, request: Callable[[], Any], attempts: int = 5) -> Any:
    delay = 1.0
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return request()
        except Exception as exc:
            last_error = exc
            if attempt == attempts:
                break
            LOG.warning("%s failed (%s); retrying in %.1fs (%d/%d)", label, type(exc).__name__, delay, attempt, attempts)
            STOP.wait(delay)
            delay = min(delay * 2, 15.0)
    raise RuntimeError(f"{label} failed after {attempts} attempts ({type(last_error).__name__})") from last_error


def load_market(info: Info) -> tuple[dict[str, Any], int, int]:
    dexes = read_with_retries("perpDexs", info.perp_dexs)
    dex_index = next((i for i, item in enumerate(dexes) if item and item.get("name") == DEX), None)
    if dex_index is None:
        raise RuntimeError(f"Testnet does not list the {DEX} perp DEX")
    meta = read_with_retries(f"meta dex={DEX}", lambda: info.meta(dex=DEX))
    meta_contexts = read_with_retries(
        f"metaAndAssetCtxs dex={DEX}",
        lambda: info.post("/info", {"type": "metaAndAssetCtxs", "dex": DEX}),
    )
    collateral_index = meta_contexts[0].get("collateralToken")
    tokens = read_with_retries("spotMeta", info.spot_meta).get("tokens", [])
    collateral = next((token.get("name") for token in tokens if token.get("index") == collateral_index), None)
    if collateral != "USDC":
        raise RuntimeError(f"{DEX} collateral is {collateral!r}, not USDC; refusing to trade")
    market_index = next((i for i, asset in enumerate(meta.get("universe", [])) if asset.get("name") == COIN), None)
    if market_index is None:
        raise RuntimeError(f"Testnet metadata does not list {COIN}")
    asset_id = 100_000 + dex_index * 10_000 + market_index
    if info.name_to_asset(COIN) != asset_id:
        raise RuntimeError(f"SDK mapped {COIN} to {info.name_to_asset(COIN)}, expected {asset_id}")
    return meta["universe"][market_index], dex_index, asset_id


def refresh_market_metadata(info: Info) -> tuple[dict[str, Any], int]:
    meta = read_with_retries(f"meta dex={DEX}", lambda: info.meta(dex=DEX))
    market_index = next((i for i, asset in enumerate(meta.get("universe", [])) if asset.get("name") == COIN), None)
    if market_index is None:
        raise RuntimeError(f"Current {DEX} metadata no longer lists {COIN}")
    mapped_asset = info.name_to_asset(COIN)
    if mapped_asset % 10_000 != market_index:
        raise RuntimeError(f"SDK asset map for {COIN} is stale (market index {mapped_asset % 10_000} != {market_index}); refusing to trade")
    return meta["universe"][market_index], market_index


def leverage_settings(market: dict[str, Any]) -> tuple[int, bool]:
    leverage = int(Decimal(str(market.get("maxLeverage", "1"))))
    mode = str(market.get("marginMode", "")).lower()
    isolated = bool(market.get("onlyIsolated", False)) or "isolated" in mode or mode in {"nocross", "no_cross"}
    return leverage, not isolated


def acknowledge_leverage(exchange: Exchange, market: dict[str, Any], reason: str) -> bool:
    leverage, is_cross = leverage_settings(market)
    try:
        response = exchange.update_leverage(leverage, COIN, is_cross=is_cross)
        ok = isinstance(response, dict) and response.get("status") == "ok"
        if ok:
            LOG.warning("LEVERAGE_ACK reason=%s coin=%s leverage=%sx margin_mode=%s is_cross=%s status=ok", reason, COIN, leverage, market.get("marginMode", "unknown"), is_cross)
        else:
            LOG.error("LEVERAGE_ACK_FAILED reason=%s coin=%s leverage=%sx margin_mode=%s response=%s", reason, COIN, leverage, market.get("marginMode", "unknown"), response)
        return ok
    except Exception as exc:
        LOG.error("LEVERAGE_ACK_FAILED reason=%s coin=%s leverage=%sx margin_mode=%s error=%s", reason, COIN, leverage, market.get("marginMode", "unknown"), type(exc).__name__)
        return False


def is_margin_tier_reject(value: Any) -> bool:
    text = str(value).lower()
    return "margin tier" in text or "manually set leverage" in text or "automatically accept new margin" in text


def get_position(info: Info, address: str) -> Decimal:
    state = read_with_retries(f"clearinghouseState dex={DEX}", lambda: info.user_state(address, dex=DEX))
    for entry in state.get("assetPositions", []):
        position = entry.get("position", {})
        if position.get("coin") == COIN:
            return Decimal(str(position.get("szi", "0")))
    return Decimal(0)


def best_prices(info: Info) -> tuple[Decimal, Decimal]:
    for attempt in range(2):
        book = read_with_retries(f"l2Book coin={COIN}", lambda: info.l2_snapshot(COIN))
        levels = book.get("levels", [[], []])
        bids = levels[0] if levels else []
        asks = levels[1] if len(levels) > 1 else []
        if bids and asks:
            return Decimal(str(bids[0]["px"])), Decimal(str(asks[0]["px"]))
        if attempt == 0:
            LOG.warning("BOOK_ONE_SIDED coin=%s retry=2 wait_seconds=1", COIN)
            STOP.wait(1.0)
    raise RuntimeError(f"l2Book for {COIN} remained one-sided after one 1-second retry")


def execution_touch(info: Info, is_buy: bool) -> tuple[Decimal, str]:
    """Read the needed book side for a reducing IOC; oracle fallback keeps flattening attemptable."""
    for attempt in range(2):
        book = read_with_retries(f"l2Book coin={COIN}", lambda: info.l2_snapshot(COIN))
        levels = book.get("levels", [[], []])
        bids = levels[0] if levels else []
        asks = levels[1] if len(levels) > 1 else []
        executable = asks if is_buy else bids
        if executable:
            return Decimal(str(executable[0]["px"])), "book"
        if attempt == 0:
            LOG.warning("FLATTEN_BOOK_SIDE_MISSING side=%s retry=2 wait_seconds=1", "BUY" if is_buy else "SELL")
            STOP.wait(1.0)

    _market, market_index = refresh_market_metadata(info)
    contexts = read_with_retries(
        f"metaAndAssetCtxs dex={DEX}",
        lambda: info.post("/info", {"type": "metaAndAssetCtxs", "dex": DEX}),
    )
    context_list = contexts[1]
    value = context_list[market_index].get("oraclePx") if market_index < len(context_list) else None
    if value is None or Decimal(str(value)) <= 0:
        raise RuntimeError(f"No executable book side or valid oracle fallback for reduce-only {COIN} order")
    oracle = Decimal(str(value))
    LOG.critical("FLATTEN_ORACLE_FALLBACK side=%s oracle=%s book_side_missing=true", "BUY" if is_buy else "SELL", oracle)
    return oracle, "oracle_fallback"


def normalize_price(price: Decimal, sz_decimals: int, is_buy: bool) -> Decimal:
    if price <= 0:
        raise ValueError("price must be positive")
    significant_places = max(0, 4 - price.adjusted())
    decimal_places = min(significant_places, 6 - sz_decimals)
    quantum = Decimal(1).scaleb(-decimal_places)
    return price.quantize(quantum, rounding=ROUND_FLOOR if is_buy else ROUND_CEILING)


def quantize_size(size: Decimal, sz_decimals: int) -> Decimal:
    return size.quantize(Decimal(1).scaleb(-sz_decimals), rounding=ROUND_FLOOR)


def margin_snapshot(
    info: Info,
    address: str,
    max_leverage: Decimal,
    configured_max_position: Decimal,
    market: dict[str, Any] | None = None,
    market_index: int | None = None,
) -> dict[str, Decimal | str]:
    if market is None or market_index is None:
        market, market_index = refresh_market_metadata(info)
    max_leverage = Decimal(str(market.get("maxLeverage", "1")))
    state = read_with_retries(f"clearinghouseState dex={DEX}", lambda: info.user_state(address, dex=DEX))
    spot_state = read_with_retries("spotClearinghouseState", lambda: info.spot_user_state(address))
    usdc = next((item for item in spot_state.get("balances", []) if item.get("coin") == "USDC"), {})
    spot_total = Decimal(str(usdc.get("total", "0")))
    spot_hold = Decimal(str(usdc.get("hold", "0")))
    spot_free = max(Decimal(0), spot_total - spot_hold)
    summary = state.get("marginSummary", {})
    account_value = Decimal(str(summary.get("accountValue", "0")))
    withdrawable = Decimal(str(state.get("withdrawable", "0")))
    abstraction = read_with_retries("userAbstraction", lambda: info.query_user_abstraction_state(address))
    active = read_with_retries(
        f"activeAssetData coin={COIN}",
        lambda: info.post("/info", {"type": "activeAssetData", "user": address, "coin": COIN}),
    )
    available = active.get("availableToTrade")
    if not isinstance(available, (list, tuple)) or len(available) != 2:
        raise RuntimeError(f"Could not verify {DEX} USDC margin capacity for {COIN}; refusing to trade")
    active_leverage_data = active.get("leverage", {})
    active_leverage = Decimal(str(active_leverage_data.get("value", max_leverage))) if isinstance(active_leverage_data, dict) else max_leverage
    active_leverage = min(active_leverage, max_leverage)
    if active_leverage <= 0:
        raise RuntimeError(f"Invalid active leverage for {COIN}")
    if abstraction in ("unifiedAccount", "portfolioMargin"):
        collateral_total = spot_total
        collateral_free = spot_free
        collateral_source = "shared_spot_usdc"
    else:
        collateral_total = account_value
        collateral_free = max(Decimal(0), min(withdrawable, account_value))
        collateral_source = "bart_perp_usdc"
    buy_capacity = Decimal(str(available[0])) * active_leverage
    sell_capacity = Decimal(str(available[1])) * active_leverage
    effective_position_limit = min(
        collateral_total * max_leverage * MARGIN_RESERVE,
        max_leverage * collateral_free * MARGIN_RESERVE + Decimal(0),
    )
    # Used margin is included in spot holds; add current position value back when computing gross support.
    mark = Decimal(str(active.get("markPx", "0")))
    position_state = position_for(state, COIN)
    position = Decimal(str(position_state.get("szi", "0")))
    position_notional = abs(position * mark)
    effective_position_limit = min(
        collateral_total * max_leverage * MARGIN_RESERVE,
        position_notional + collateral_free * max_leverage * MARGIN_RESERVE,
    )
    if mark <= 0:
        raise RuntimeError(f"activeAssetData returned no valid mark price for {COIN}")
    contexts = read_with_retries(
        f"metaAndAssetCtxs dex={DEX}",
        lambda: info.post("/info", {"type": "metaAndAssetCtxs", "dex": DEX}),
    )
    context_list = contexts[1]
    oracle_value = context_list[market_index].get("oraclePx") if market_index < len(context_list) else None
    if oracle_value is None or Decimal(str(oracle_value)) <= 0:
        raise RuntimeError(f"metaAndAssetCtxs returned no valid oraclePx for {COIN}")
    oracle = Decimal(str(oracle_value))
    liq_distance = liquidation_distance_pct(position_state, position, oracle)
    leverage_mode = market.get("marginMode", "unknown")
    liq_px = Decimal(str(position_state.get("liquidationPx", "0")))
    LOG.info(
        "MARGIN dex=%s abstraction=%s source=%s account_value=$%s withdrawable=$%s spot_usdc_total=$%s "
        "spot_usdc_hold=$%s spot_usdc_free=$%s leverage=%sx margin_mode=%s liquidation_px=%s liquidation_distance_pct=%s "
        "active_buy_capacity=$%s active_sell_capacity=$%s max_position_configured=$%s max_position_margin_supported=$%s reserve_pct=%s",
        DEX,
        abstraction,
        collateral_source,
        account_value,
        withdrawable,
        spot_total,
        spot_hold,
        spot_free,
        max_leverage,
        leverage_mode,
        liq_px,
        "unknown" if liq_distance is None else liq_distance,
        buy_capacity,
        sell_capacity,
        configured_max_position,
        effective_position_limit,
        MARGIN_RESERVE * 100,
    )
    if collateral_total <= 0 or effective_position_limit <= 0 or min(buy_capacity, sell_capacity) <= 0:
        raise RuntimeError(
            f"No verified {DEX} USDC margin available: Spot USDC=${spot_total} held=${spot_hold}, "
            f"Bart account value=${account_value}, active notional capacity buy=${buy_capacity} sell=${sell_capacity}"
        )
    return {
        "collateral_total": collateral_total,
        "collateral_free": collateral_free,
        "spot_total": spot_total,
        "spot_hold": spot_hold,
        "spot_free": spot_free,
        "buy_capacity": buy_capacity,
        "sell_capacity": sell_capacity,
        "effective_position_limit": effective_position_limit,
        "mark": mark,
        "active_leverage": active_leverage,
        "position_notional": position_notional,
        "position": position,
        "liquidation_px": liq_px,
        "liquidation_distance_pct": liq_distance if liq_distance is not None else Decimal("-1"),
        "oracle": oracle,
        "max_leverage": max_leverage,
        "margin_mode": str(leverage_mode),
        "abstraction": abstraction,
        "collateral_source": collateral_source,
    }


def signed_order_headroom(position_notional: Decimal, side: str, position_limit: Decimal) -> Decimal:
    return max(Decimal(0), position_limit - position_notional if side == "BUY" else position_limit + position_notional)


def make_ioc_price(best_bid: Decimal, best_ask: Decimal, is_buy: bool, slippage_percent: Decimal, sz_decimals: int) -> Decimal:
    touch = best_ask if is_buy else best_bid
    factor = Decimal(1) + slippage_percent / Decimal(100) if is_buy else Decimal(1) - slippage_percent / Decimal(100)
    return normalize_price(touch * factor, sz_decimals, is_buy)


def make_ioc_touch_price(touch: Decimal, is_buy: bool, slippage_percent: Decimal, sz_decimals: int) -> Decimal:
    factor = Decimal(1) + slippage_percent / Decimal(100) if is_buy else Decimal(1) - slippage_percent / Decimal(100)
    return normalize_price(touch * factor, sz_decimals, is_buy)


def is_rate_limit(value: Any) -> bool:
    if isinstance(value, Exception):
        response = getattr(value, "response", None)
        if response is not None and getattr(response, "status_code", None) == 429:
            return True
    payload = json.dumps(value, default=str).lower()
    return "429" in payload or "rate limit" in payload or "too many requests" in payload


def contains_margin_tier_reject(value: Any) -> bool:
    try:
        payload = json.dumps(value, default=str).lower()
    except Exception:
        payload = str(value).lower()
    return is_margin_tier_reject(payload)


def submit_ioc(
    exchange: Exchange,
    is_buy: bool,
    size: Decimal,
    limit_price: Decimal,
    reduce_only: bool,
    market: dict[str, Any],
) -> dict[str, Any] | None:
    delay = 1.0
    rate_attempt = 0
    tier_retried = False
    while rate_attempt < 4:
        try:
            response = exchange.order(
                COIN,
                is_buy,
                float(size),
                float(limit_price),
                {"limit": {"tif": "Ioc"}},
                reduce_only=reduce_only,
            )
        except Exception as exc:
            if contains_margin_tier_reject(exc) and not tier_retried:
                fresh_market, _ = refresh_market_metadata(exchange.info)
                if acknowledge_leverage(exchange, fresh_market, "margin_tier_reject"):
                    tier_retried = True
                    market = fresh_market
                    LOG.warning("MARGIN_TIER_RETRY side=%s reduce_only=%s", "BUY" if is_buy else "SELL", reduce_only)
                    continue
                return None
            if is_rate_limit(exc) and rate_attempt < 3:
                rate_attempt += 1
                LOG.warning("RATE_LIMIT status=429 retry=%d backoff_seconds=%.1f", rate_attempt, delay)
                STOP.wait(delay)
                delay = min(delay * 2, 8.0)
                continue
            if is_rate_limit(exc):
                LOG.error("RATE_LIMIT status=429 retries_exhausted=4")
            else:
                LOG.error("ORDER transport/API error (%s); not retrying ambiguous submission", type(exc).__name__)
            return None
        if contains_margin_tier_reject(response) and not tier_retried:
            fresh_market, _ = refresh_market_metadata(exchange.info)
            if acknowledge_leverage(exchange, fresh_market, "margin_tier_reject"):
                tier_retried = True
                market = fresh_market
                LOG.warning("MARGIN_TIER_RETRY side=%s reduce_only=%s", "BUY" if is_buy else "SELL", reduce_only)
                continue
            return response
        if contains_margin_tier_reject(response):
            LOG.error("MARGIN_TIER_RETRY_FAILED side=%s response=%s", "BUY" if is_buy else "SELL", response)
        if is_rate_limit(response):
            if rate_attempt < 3:
                rate_attempt += 1
                LOG.warning("RATE_LIMIT status=429 retry=%d backoff_seconds=%.1f", rate_attempt, delay)
                STOP.wait(delay)
                delay = min(delay * 2, 8.0)
                continue
            LOG.error("RATE_LIMIT status=429 retries_exhausted=4")
            return None
        return response
    return None


def fill_totals(response: dict[str, Any] | None) -> tuple[Decimal, Decimal, list[str]]:
    if not response:
        return Decimal(0), Decimal(0), ["no response"]
    filled_size = Decimal(0)
    filled_notional = Decimal(0)
    errors: list[str] = []
    statuses = response.get("response", {}).get("data", {}).get("statuses", [])
    if response.get("status") != "ok":
        errors.append(str(response.get("response", response.get("status"))))
    for status in statuses:
        if "filled" in status:
            fill = status["filled"]
            size = Decimal(str(fill.get("totalSz", "0")))
            avg_price = Decimal(str(fill.get("avgPx", "0")))
            filled_size += size
            filled_notional += size * avg_price
        elif "error" in status:
            errors.append(str(status["error"]))
    return filled_size, filled_notional, errors


def log_trade_result(
    response: dict[str, Any] | None,
    side: str,
    requested_size: Decimal,
    position_after: Decimal,
    mark: Decimal,
    *,
    flatten: bool = False,
    reason: str = "",
) -> bool:
    filled_size, notional, errors = fill_totals(response)
    position_usd = position_after * mark
    prefix = "FLATTEN" if flatten else "FILL"
    if filled_size > 0:
        avg_price = notional / filled_size
        LOG.info(
            "%s side=%s size=%s notional=$%s avg_px=%s position_after=%s position_notional=$%s requested_size=%s reason=%s",
            prefix,
            side,
            filled_size,
            notional,
            avg_price,
            position_after,
            position_usd,
            requested_size,
            reason,
        )
        return True
    if errors:
        LOG.warning("%s_REJECT side=%s requested_size=%s position_after=%s reason=%s errors=%s", prefix, side, requested_size, position_after, reason, "; ".join(errors))
    else:
        LOG.warning("%s_NO_FILL side=%s requested_size=%s position_after=%s reason=%s", prefix, side, requested_size, position_after, reason)
    return False


def flatten_position(
    exchange: Exchange,
    address: str,
    args: argparse.Namespace,
    sz_decimals: int,
    reason: str,
) -> Decimal:
    info = exchange.info
    LOG.warning("FLATTEN_START reason=%s", reason)
    for attempt in range(1, 6):
        position = get_position(info, address)
        if position == 0:
            LOG.info("FLATTEN_COMPLETE reason=%s position_after=0 attempt=%d", reason, attempt - 1)
            return Decimal(0)
        is_buy = position < 0
        side = "BUY" if is_buy else "SELL"
        size = quantize_size(abs(position), sz_decimals)
        market, _market_index = refresh_market_metadata(info)
        touch, source = execution_touch(info, is_buy)
        limit_price = make_ioc_touch_price(touch, is_buy, args.slippage_percent, sz_decimals)
        LOG.info("FLATTEN_ORDER reason=%s attempt=%d side=%s size=%s touch=%s touch_source=%s ioc_limit=%s reduce_only=true", reason, attempt, side, size, touch, source, limit_price)
        response = submit_ioc(exchange, is_buy, size, limit_price, reduce_only=True, market=market)
        after = get_position(info, address)
        log_trade_result(response, side, size, after, touch, flatten=True, reason=reason)
        if after == 0:
            LOG.info("FLATTEN_COMPLETE reason=%s position_after=0 attempt=%d", reason, attempt)
            return after
        if after == position:
            LOG.warning("FLATTEN_REMAINS reason=%s position=%s; retrying", reason, after)
        STOP.wait(min(float(attempt), 4.0))
    remaining = get_position(info, address)
    LOG.error("FLATTEN_INCOMPLETE reason=%s remaining_position=%s", reason, remaining)
    return remaining


def random_notional(
    minimum: Decimal,
    maximum: Decimal,
    rng: Any = random,
) -> Decimal:
    """Sample a clipped log-normal clip with a $40 median."""
    sampled = rng.lognormvariate(math.log(LOGNORMAL_MEDIAN_USD), LOGNORMAL_SIGMA)
    return Decimal(str(min(float(maximum), max(float(minimum), sampled))))


def choose_side(
    rng: Any,
    previous_side: str,
    position_notional: Decimal,
    max_position_usd: Decimal,
) -> str:
    """Use short directional streaks, with stronger mean reversion near the cap."""
    if position_notional >= max_position_usd * Decimal("0.25") and rng.random() < 0.75:
        return "SELL"
    if position_notional <= -max_position_usd * Decimal("0.25") and rng.random() < 0.75:
        return "BUY"
    return previous_side if rng.random() < 0.65 else ("SELL" if previous_side == "BUY" else "BUY")


def order_size_from_notional(
    requested_notional: Decimal,
    permitted_notional: Decimal,
    price: Decimal,
    sz_decimals: int,
    minimum_notional: Decimal,
    rng: Any,
) -> tuple[Decimal, str]:
    """Apply the USD clip, occasional human-sized contracts, and szDecimals rounding."""
    if price <= 0 or permitted_notional < minimum_notional:
        return Decimal(0), "skipped"

    desired = min(requested_notional, permitted_notional)
    style = "lot"
    size = quantize_size(desired / price, sz_decimals)
    if rng.random() < HUMAN_SIZE_PROBABILITY:
        human_candidates = [candidate for candidate in HUMAN_SIZES if candidate * price <= permitted_notional]
        if human_candidates:
            size = min(human_candidates, key=lambda candidate: abs(candidate * price - desired))
            style = "human"

    lot = Decimal(1).scaleb(-sz_decimals)
    minimum_size = (minimum_notional / price / lot).to_integral_value(rounding=ROUND_CEILING) * lot
    if size * price < minimum_notional:
        size = minimum_size
    if size <= 0 or size * price > permitted_notional:
        return Decimal(0), "skipped"
    return size, style


def initial_regime(rng: Any) -> str:
    names = tuple(REGIME_WEIGHTS)
    weights = tuple(REGIME_WEIGHTS[name] for name in names)
    return rng.choices(names, weights=weights, k=1)[0]


def following_regime(rng: Any, current: str) -> str:
    options = REGIME_TRANSITIONS[current]
    names = tuple(name for name, _weight in options)
    weights = tuple(weight for _name, weight in options)
    return rng.choices(names, weights=weights, k=1)[0]


def regime_duration_seconds(rng: Any) -> float:
    return rng.uniform(5 * 60, 20 * 60)


def refresh_live_regime(args: argparse.Namespace, rng: Any) -> None:
    now = time.monotonic()
    while now >= args.regime_ends_at:
        previous = args.regime
        args.regime = following_regime(rng, previous)
        duration = regime_duration_seconds(rng)
        args.regime_ends_at += duration
        rate = 60.0 / args.effective_interval_seconds * REGIME_MULTIPLIERS[args.regime]
        LOG.info(
            "REGIME from=%s to=%s multiplier=%.1fx duration_minutes=%.1f target_trades_per_min=%.2f",
            previous,
            args.regime,
            REGIME_MULTIPLIERS[args.regime],
            duration / 60.0,
            rate,
        )


def next_flow_delay(args: argparse.Namespace, rng: Any) -> tuple[float, str]:
    refresh_live_regime(args, rng)
    if args.burst_remaining > 0:
        args.burst_remaining -= 1
        args.pending_burst_id = args.active_burst_id
        return rng.uniform(BURST_GAP_MIN_SECONDS, BURST_GAP_MAX_SECONDS), "burst"
    if rng.random() < BURST_PROBABILITY:
        total = rng.randint(BURST_TRADES_MIN, BURST_TRADES_MAX)
        args.burst_sequence += 1
        args.active_burst_id = args.burst_sequence
        args.burst_remaining = total - 2
        args.pending_burst_id = args.active_burst_id
        LOG.info("BURST_START id=%d trades=%d gap_seconds=1-8", args.active_burst_id, total)
        return rng.uniform(BURST_GAP_MIN_SECONDS, BURST_GAP_MAX_SECONDS), "burst"
    args.pending_burst_id = None
    mean_parent_gap = args.effective_interval_seconds * BURST_RATE_COMPENSATION / REGIME_MULTIPLIERS[args.regime]
    return rng.expovariate(1.0 / mean_parent_gap), "poisson"


def wait_for_flow_arrival(
    args: argparse.Namespace,
    rng: Any,
    wait_seconds: float,
    flow_kind: str,
    trade_started: float,
) -> bool:
    """Wait for the next arrival, resampling a Poisson gap when a regime changes."""
    remaining = max(0.0, wait_seconds - (time.monotonic() - trade_started))
    while not STOP.is_set():
        refresh_live_regime(args, rng)
        if remaining <= 0:
            return True
        to_regime_change = max(0.0, args.regime_ends_at - time.monotonic())
        if to_regime_change <= 0:
            continue
        segment = min(remaining, to_regime_change)
        if STOP.wait(segment):
            return False
        remaining -= segment
        if flow_kind == "poisson" and segment >= to_regime_change:
            refresh_live_regime(args, rng)
            mean_parent_gap = args.effective_interval_seconds * BURST_RATE_COMPENSATION / REGIME_MULTIPLIERS[args.regime]
            remaining = rng.expovariate(1.0 / mean_parent_gap)
    return False


def simulate_flow(args: argparse.Namespace, price: Decimal, sz_decimals: int, price_source: str) -> None:
    """Offline order-flow and fee projection; this function never signs or sends."""
    rng = random.Random(args.seed)
    duration_seconds = float(args.simulate_minutes * 60)
    base_interval = args.interval_seconds
    regime_minutes = {name: 0.0 for name in REGIME_MULTIPLIERS}
    regime_trades = {name: 0 for name in REGIME_MULTIPLIERS}
    histogram = {
        "$10-24": 0,
        "$25-49": 0,
        "$50-99": 0,
        "$100-199": 0,
        "$200-399": 0,
        "$400": 0,
    }
    state = {
        "position": Decimal(0),
        "previous_side": rng.choice(("BUY", "SELL")),
        "trades_in_cycle": 0,
        "cycle_target": rng.randint(args.flatten_every_min, args.flatten_every_max),
        "total_trades": 0,
        "human_sizes": 0,
        "order_notional": Decimal(0),
        "flatten_notional": Decimal(0),
        "flatten_orders": 0,
        "burst_clusters": 0,
        "burst_trades": 0,
    }
    regime = initial_regime(rng)
    regime_end = regime_duration_seconds(rng)
    simulated_time = 0.0
    LOG.info(
        "SIMULATION market=%s price=%s price_source=%s szDecimals=%d minutes=%d seed=%s interval_seconds=%.1f fee_window_hours=%.2f",
        COIN,
        price,
        price_source,
        sz_decimals,
        args.simulate_minutes,
        "random" if args.seed is None else args.seed,
        base_interval,
        args.fee_window_hours,
    )

    def switch_regime() -> None:
        nonlocal regime, regime_end
        previous = regime
        regime = following_regime(rng, previous)
        regime_end += regime_duration_seconds(rng)

    def record_trade(regime_name: str, burst: bool = False) -> None:
        sample = random_notional(args.min_notional_usd, args.max_notional_usd, rng)
        position_notional = state["position"] * price
        side = choose_side(rng, state["previous_side"], position_notional, args.max_position_usd)
        state["previous_side"] = side
        headroom = signed_order_headroom(position_notional, side, args.max_position_usd)
        size, style = order_size_from_notional(
            sample,
            min(args.max_notional_usd, headroom),
            price,
            sz_decimals,
            args.min_notional_usd,
            rng,
        )
        if size <= 0:
            return
        notional = size * price
        state["order_notional"] += notional
        state["total_trades"] += 1
        regime_trades[regime_name] += 1
        if burst:
            state["burst_trades"] += 1
        if style == "human":
            state["human_sizes"] += 1
        if notional < 25:
            histogram["$10-24"] += 1
        elif notional < 50:
            histogram["$25-49"] += 1
        elif notional < 100:
            histogram["$50-99"] += 1
        elif notional < 200:
            histogram["$100-199"] += 1
        elif notional < 400:
            histogram["$200-399"] += 1
        else:
            histogram["$400"] += 1
        state["position"] += size if side == "BUY" else -size
        position_usd = state["position"] * price
        if abs(position_usd) >= args.max_position_usd:
            state["flatten_notional"] += abs(position_usd)
            state["flatten_orders"] += 1
            state["position"] = Decimal(0)
            state["trades_in_cycle"] = 0
            state["cycle_target"] = rng.randint(args.flatten_every_min, args.flatten_every_max)
            return
        state["trades_in_cycle"] += 1
        if state["trades_in_cycle"] >= state["cycle_target"]:
            state["flatten_notional"] += abs(position_usd)
            state["flatten_orders"] += 1
            state["position"] = Decimal(0)
            state["trades_in_cycle"] = 0
            state["cycle_target"] = rng.randint(args.flatten_every_min, args.flatten_every_max)

    while simulated_time < duration_seconds:
        if simulated_time >= regime_end:
            switch_regime()
            continue
        rate_multiplier = REGIME_MULTIPLIERS[regime]
        mean_parent_gap = base_interval * BURST_RATE_COMPENSATION / rate_multiplier
        gap = rng.expovariate(1.0 / mean_parent_gap)
        next_boundary = min(regime_end, duration_seconds)
        if simulated_time + gap >= next_boundary:
            segment = next_boundary - simulated_time
            regime_minutes[regime] += segment / 60.0
            simulated_time = next_boundary
            if simulated_time >= duration_seconds:
                break
            switch_regime()
            continue
        regime_minutes[regime] += gap / 60.0
        simulated_time += gap
        event_regime = regime
        record_trade(event_regime)
        if rng.random() < BURST_PROBABILITY:
            cluster_size = rng.randint(BURST_TRADES_MIN, BURST_TRADES_MAX)
            state["burst_clusters"] += 1
            for _ in range(cluster_size - 1):
                burst_gap = rng.uniform(BURST_GAP_MIN_SECONDS, BURST_GAP_MAX_SECONDS)
                while simulated_time + burst_gap >= min(regime_end, duration_seconds) and simulated_time < duration_seconds:
                    next_boundary = min(regime_end, duration_seconds)
                    segment = next_boundary - simulated_time
                    regime_minutes[regime] += segment / 60.0
                    simulated_time = next_boundary
                    burst_gap = max(0.0, burst_gap - segment)
                    if simulated_time >= duration_seconds:
                        break
                    switch_regime()
                if simulated_time >= duration_seconds:
                    break
                regime_minutes[regime] += burst_gap / 60.0
                simulated_time += burst_gap
                record_trade(regime, burst=True)
            if simulated_time >= duration_seconds:
                break

    if simulated_time < duration_seconds:
        regime_minutes[regime] += (duration_seconds - simulated_time) / 60.0
    elif sum(regime_minutes.values()) < args.simulate_minutes:
        regime_minutes[regime] += args.simulate_minutes - sum(regime_minutes.values())

    total_gross = state["order_notional"] + state["flatten_notional"]
    combined_fee_rate = (MAKER_FEE_BPS + TRADER_FEE_BPS) / Decimal(10_000)
    fees_for_window = total_gross * combined_fee_rate
    projected_daily_fee = fees_for_window * Decimal(1440) / Decimal(args.simulate_minutes)
    actual_per_min = state["total_trades"] / args.simulate_minutes
    expected_per_min = 60.0 / args.interval_seconds
    regime_weight_total = sum(REGIME_WEIGHTS.values())
    weighted_rate_multiplier = sum(
        REGIME_WEIGHTS[name] / regime_weight_total * REGIME_MULTIPLIERS[name]
        for name in REGIME_MULTIPLIERS
    )
    print(
        f"REGIME_CALIBRATION weighted_multiplier={weighted_rate_multiplier:.3f} "
        f"burst_probability={BURST_PROBABILITY:.2f} burst_rate_compensation={BURST_RATE_COMPENSATION:.2f} "
        f"expected_long_run_trades_per_min={expected_per_min:.3f}",
    )
    print(
        f"SIM_SUMMARY duration_minutes={args.simulate_minutes} seed={args.seed} price={price} szDecimals={sz_decimals} "
        f"trades={state['total_trades']} trades_per_min={actual_per_min:.3f} target_trades_per_min={expected_per_min:.3f} "
        f"bursts={state['burst_clusters']} burst_trades={state['burst_trades']} human_size_pct="
        f"{(100 * state['human_sizes'] / max(1, state['total_trades'])):.1f}",
    )
    for name in REGIME_MULTIPLIERS:
        minutes = regime_minutes[name]
        trades = regime_trades[name]
        expected = expected_per_min * REGIME_MULTIPLIERS[name]
        observed = trades / minutes if minutes else 0.0
        print(
            f"REGIME_SUMMARY name={name} multiplier={REGIME_MULTIPLIERS[name]:.1f}x minutes={minutes:.1f} "
            f"trades={trades} trades_per_min={observed:.3f} target_trades_per_min={expected:.3f}",
        )
    total_histogram = sum(histogram.values())
    for bucket, count in histogram.items():
        print(f"SIZE_HISTOGRAM bucket_usd={bucket} count={count} share_pct={100 * count / max(1, total_histogram):.1f}")
    print(
        f"FEE_PROJECTION_WINDOW observed_fee_estimate=${fees_for_window:.2f} fee_model_maker_bps={MAKER_FEE_BPS} "
        f"fee_model_trader_bps={TRADER_FEE_BPS} combined_bps={MAKER_FEE_BPS + TRADER_FEE_BPS} "
        f"regular_order_volume=${state['order_notional']:.2f} flatten_volume=${state['flatten_notional']:.2f} "
        f"flatten_orders={state['flatten_orders']} projected_fees_per_day=${projected_daily_fee:.2f} "
        f"budget=${args.daily_fee_budget_usd:.2f} "
        f"within_budget={'yes' if projected_daily_fee <= args.daily_fee_budget_usd else 'no'}",
    )


def submit_random_trade(
    exchange: Exchange,
    address: str,
    args: argparse.Namespace,
    sz_decimals: int,
    max_leverage: Decimal,
    trade_number: int,
    market: dict[str, Any],
    market_index: int,
    margin: dict[str, Any] | None = None,
) -> tuple[Decimal, str]:
    info = exchange.info
    best_bid, best_ask = best_prices(info)
    mid = (best_bid + best_ask) / Decimal(2)
    position = get_position(info, address)
    position_notional = position * mid
    if margin is None:
        margin = margin_snapshot(info, address, max_leverage, args.max_position_usd, market, market_index)
    position_limit = min(args.max_position_usd, Decimal(margin["effective_position_limit"]))
    if position_limit < args.max_position_usd:
        LOG.warning("POSITION_MARGIN_CAP configured=$%s supported=$%s; using_supported_cap=true", args.max_position_usd, position_limit)
    if abs(position_notional) >= position_limit:
        reason = "max_position" if abs(position_notional) >= args.max_position_usd else "available_margin_position_cap"
        remaining = flatten_position(exchange, address, args, sz_decimals, reason)
        return remaining, "FLATTEN"

    rng = getattr(args, "rng", random)
    side = choose_side(rng, args.previous_side, position_notional, args.max_position_usd)
    args.previous_side = side
    is_buy = side == "BUY"
    requested_notional = random_notional(args.min_notional_usd, args.max_notional_usd, rng)
    active_capacity_key = "buy_capacity" if is_buy else "sell_capacity"
    side_margin_capacity = Decimal(margin[active_capacity_key])
    position_headroom = signed_order_headroom(position_notional, side, position_limit)
    permitted_notional = min(args.max_notional_usd, side_margin_capacity, position_headroom)
    actual_request = min(requested_notional, permitted_notional)
    if actual_request < requested_notional:
        LOG.warning(
            "SIZE_CAP trade=%d side=%s sampled=$%s permitted=$%s max_clip=$%s margin_capacity=$%s position_headroom=$%s",
            trade_number,
            side,
            requested_notional,
            actual_request,
            args.max_notional_usd,
            side_margin_capacity,
            position_headroom,
        )
    size, size_style = order_size_from_notional(
        requested_notional,
        permitted_notional,
        mid,
        sz_decimals,
        args.min_notional_usd,
        rng,
    )
    if size <= 0:
        LOG.warning("TRADE_SKIPPED trade=%d reason=clip_below_minimum_or_lot_cap sampled_notional=$%s", trade_number, requested_notional)
        return position, side
    actual_notional = size * mid
    limit_price = make_ioc_price(best_bid, best_ask, is_buy, args.slippage_percent, sz_decimals)
    LOG.info(
        "ORDER_INTENT trade=%d side=%s size=%s size_style=%s notional=$%s sampled_notional=$%s position_before=%s "
        "max_position_effective=$%s touch_bid=%s touch_ask=%s ioc_limit=%s slippage_cap=%.2f%% regime=%s burst_id=%s",
        trade_number,
        side,
        size,
        size_style,
        actual_notional,
        requested_notional,
        position,
        position_limit,
        best_bid,
        best_ask,
        limit_price,
        args.slippage_percent,
        args.regime,
        args.current_burst_id if args.current_burst_id is not None else "none",
    )
    response = submit_ioc(exchange, is_buy, size, limit_price, reduce_only=False, market=market)
    after = get_position(info, address)
    log_trade_result(response, side, size, after, mid)
    return after, side


def handle_stop(signum: int, _frame: Any) -> None:
    LOG.info("STOP signal=%s received", signum)
    STOP.set()


def fee_budget_check(args: argparse.Namespace, info: Info, trader_address: str) -> dict[str, Any]:
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - int(args.fee_window_hours * 60 * 60 * 1000)
    maker_fee, maker_count = fees_in_range(info, MAKER_ADDRESS, start_ms, now_ms)
    trader_fee, trader_count = fees_in_range(info, trader_address, start_ms, now_ms)
    scale = Decimal(str(24 / args.fee_window_hours))
    maker_fee, trader_fee = maker_fee * scale, trader_fee * scale
    maker_total, _maker_hold, maker_free = spot_usdc(info, MAKER_ADDRESS)
    trader_total, _trader_hold, trader_free = spot_usdc(info, trader_address)
    fees_24h = maker_fee + trader_fee
    combined_free = maker_free + trader_free
    runway_days = Decimal("Infinity") if fees_24h <= 0 else combined_free / fees_24h

    was_paused = bool(getattr(args, "fee_paused", False))
    if fees_24h > args.daily_fee_budget_usd * args.fee_pause_multiplier:
        args.fee_paused = True
        action = "pause"
        effective_interval = args.interval_seconds
    elif was_paused and fees_24h > args.daily_fee_budget_usd:
        args.fee_paused = True
        action = "pause_hysteresis_until_under_budget"
        effective_interval = args.interval_seconds
    elif fees_24h > args.daily_fee_budget_usd:
        args.fee_paused = False
        effective_interval = args.interval_seconds * float(fees_24h / args.daily_fee_budget_usd)
        action = "stretch_interval"
    else:
        args.fee_paused = False
        effective_interval = args.interval_seconds
        action = "normal"

    args.effective_interval_seconds = effective_interval
    args.fee_last_check = time.monotonic()
    args.fee_last_value = fees_24h
    runway = "infinite" if fees_24h <= 0 else f"{runway_days:.2f}"
    LOG.warning(
        "FEE_BUDGET fees_24h=$%s budget=$%s runway_days=%s action=%s effective_interval_seconds=%.1f "
        "maker_fees=$%s/%d_fills trader_fees=$%s/%d_fills combined_free_usdc=$%s maker_total=$%s trader_total=$%s",
        fees_24h,
        args.daily_fee_budget_usd,
        runway,
        action,
        effective_interval,
        maker_fee,
        maker_count,
        trader_fee,
        trader_count,
        combined_free,
        maker_total,
        trader_total,
    )
    return {"fees_24h": fees_24h, "runway_days": runway_days, "action": action, "paused": args.fee_paused}


def try_liquidation_margin_topup(
    exchange: Exchange,
    address: str,
    args: argparse.Namespace,
    market: dict[str, Any],
    margin: dict[str, Any],
) -> bool:
    """Return true only after isolated collateral moves liquidation beyond 15%."""
    result = try_add_isolated_margin(
        exchange,
        exchange.info,
        address,
        DEX,
        COIN,
        market,
        args.min_free_usd,
        LOG,
    )
    if result.get("fixed"):
        return True
    LOG.critical(
        "LIQUIDATION_GUARD action=flatten reason=isolated_margin_topup_failed position=%s size=%s distance_pct=%s detail=%s",
        "long" if Decimal(margin["position"]) > 0 else "short",
        margin["position"],
        margin["liquidation_distance_pct"],
        result.get("reason", "unknown"),
    )
    return False


def check_paused_liquidation_safety(
    exchange: Exchange,
    address: str,
    args: argparse.Namespace,
) -> bool:
    """Allow reduce-only liquidation protection to run while fee-paused."""
    info = exchange.info
    market, market_index = refresh_market_metadata(info)
    current_config = leverage_settings(market)
    if current_config != getattr(args, "leverage_config", None):
        if not acknowledge_leverage(exchange, market, "paused_risk_check"):
            raise RuntimeError("could not acknowledge current margin mode for paused risk check")
        args.leverage_config = current_config
    leverage = Decimal(str(market.get("maxLeverage", "1")))
    margin = margin_snapshot(info, address, leverage, args.max_position_usd, market, market_index)
    position = Decimal(margin["position"])
    distance = Decimal(margin["liquidation_distance_pct"])
    if position == 0 or distance < 0 or distance >= Decimal("10"):
        return False
    if not try_liquidation_margin_topup(exchange, address, args, market, margin):
        flatten_position(exchange, address, args, int(market["szDecimals"]), "liquidation_distance_under_10pct_fee_paused")
    return True


def run(args: argparse.Namespace, exchange: Exchange, address: str, market: dict[str, Any]) -> None:
    info = exchange.info
    market, market_index = refresh_market_metadata(info)
    sz_decimals = int(market["szDecimals"])
    max_leverage = Decimal(str(market.get("maxLeverage", "1")))
    config = leverage_settings(market)
    if not args.dry_run:
        if not acknowledge_leverage(exchange, market, "startup"):
            raise RuntimeError("Could not acknowledge current market leverage/margin mode; refusing to trade")
    args.leverage_config = config
    initial_position = get_position(info, address)
    margin = margin_snapshot(info, address, max_leverage, args.max_position_usd, market, market_index)
    initial_distance = Decimal(margin["liquidation_distance_pct"])
    if initial_position != 0 and initial_distance >= 0 and initial_distance < Decimal("10") and not args.dry_run:
        if not try_liquidation_margin_topup(exchange, address, args, market, margin):
            flatten_position(exchange, address, args, int(market["szDecimals"]), "liquidation_distance_under_10pct_startup")
        initial_position = get_position(info, address)
        margin = margin_snapshot(info, address, max_leverage, args.max_position_usd, market, market_index)
    effective_position_cap = min(args.max_position_usd, Decimal(margin["effective_position_limit"]))
    if effective_position_cap < args.max_position_usd:
        LOG.warning(
            "MARGIN_POSITION_CAP configured=$%s supported=$%s; effective_guard=$%s. The configured cap is not fully collateralized.",
            args.max_position_usd,
            Decimal(margin["effective_position_limit"]),
            effective_position_cap,
        )
    if args.max_notional_usd > effective_position_cap:
        LOG.warning("MARGIN_CLIP_CAP max_clip=$%s supported_initial_position=$%s", args.max_notional_usd, effective_position_cap)
    else:
        LOG.info("MARGIN_CLIP_OK max_clip=$%s supported_initial_position=$%s", args.max_notional_usd, effective_position_cap)

    rng = random.Random(args.seed)
    args.rng = rng
    args.previous_side = rng.choice(("BUY", "SELL"))
    args.effective_interval_seconds = args.interval_seconds
    args.regime = initial_regime(rng)
    args.regime_ends_at = time.monotonic() + regime_duration_seconds(rng)
    args.burst_remaining = 0
    args.burst_sequence = 0
    args.active_burst_id = 0
    args.pending_burst_id = None
    args.current_burst_id = None
    LOG.info(
        "REGIME_START name=%s multiplier=%.1fx duration_minutes=%.1f target_trades_per_min=%.2f",
        args.regime,
        REGIME_MULTIPLIERS[args.regime],
        (args.regime_ends_at - time.monotonic()) / 60.0,
        60.0 / args.interval_seconds * REGIME_MULTIPLIERS[args.regime],
    )

    cycle = 1
    cycle_target = rng.randint(args.flatten_every_min, args.flatten_every_max)
    trades_in_cycle = 0
    trade_number = 0
    LOG.info(
        "CYCLE_START cycle=%d flatten_after_trades=%d position=%s interval_seconds=%.1f arrival=poisson "
        "min_notional=$%s max_notional=$%s max_position_configured=$%s flatten_cycles=enabled seed=%s",
        cycle,
        cycle_target,
        initial_position,
        args.interval_seconds,
        args.min_notional_usd,
        args.max_notional_usd,
        args.max_position_usd,
        "random" if args.seed is None else args.seed,
    )

    if args.dry_run:
        best_bid, best_ask = best_prices(info)
        mid = (best_bid + best_ask) / Decimal(2)
        side = choose_side(rng, args.previous_side, initial_position * mid, args.max_position_usd)
        sampled = random_notional(args.min_notional_usd, args.max_notional_usd, rng)
        capacity_key = "buy_capacity" if side == "BUY" else "sell_capacity"
        headroom = signed_order_headroom(initial_position * mid, side, effective_position_cap)
        permitted = min(args.max_notional_usd, Decimal(margin[capacity_key]), headroom)
        size, style = order_size_from_notional(sampled, permitted, mid, sz_decimals, args.min_notional_usd, rng)
        LOG.info(
            "DRY_RUN_ORDER side=%s size=%s size_style=%s notional=$%s sampled_notional=$%s touch_bid=%s touch_ask=%s "
            "ioc_limit=%s flatten_target=%d regime=%s seed=%s; no signed request was sent",
            side,
            size,
            style,
            size * mid,
            sampled,
            best_bid,
            best_ask,
            make_ioc_price(best_bid, best_ask, side == "BUY", args.slippage_percent, sz_decimals),
            cycle_target,
            args.regime,
            "random" if args.seed is None else args.seed,
        )
        return

    args.fee_paused = True
    args.fee_check_pending = True
    args.fee_last_check = time.monotonic()

    def initial_fee_check() -> None:
        # Use a separate read-only client so the risk monitor can continue
        # querying position state while the configured trailing fee-window scan runs.
        fee_info = Info(
            base_url=TESTNET_URL,
            skip_ws=True,
            spot_meta={"tokens": [], "universe": []},
            timeout=10,
        )
        try:
            fee_budget_check(args, fee_info, address)
        except Exception as exc:
            args.fee_paused = True
            args.fee_last_check = time.monotonic()
            LOG.critical(
                "FEE_BUDGET fees_24h=unknown budget=$%s runway_days=unknown action=pause_data_unavailable error=%s",
                args.daily_fee_budget_usd,
                type(exc).__name__,
            )
        finally:
            args.fee_check_pending = False

    threading.Thread(target=initial_fee_check, name="initial-fee-budget", daemon=True).start()
    while not STOP.is_set():
        refresh_live_regime(args, rng)
        trade_started = time.monotonic()
        args.current_burst_id = None
        if getattr(args, "fee_check_pending", False):
            try:
                if check_paused_liquidation_safety(exchange, address, args):
                    trades_in_cycle = 0
                    cycle += 1
                    cycle_target = rng.randint(args.flatten_every_min, args.flatten_every_max)
                    LOG.info("CYCLE_START cycle=%d flatten_after_trades=%d reason=paused_liquidation_guard", cycle, cycle_target)
            except Exception as exc:
                LOG.error("INITIAL_FEE_SCAN_RISK_CHECK failed (%s); no risk-increasing orders until fee scan completes", type(exc).__name__)
            STOP.wait(15.0)
            continue
        if args.fee_paused and not args.once:
            try:
                if check_paused_liquidation_safety(exchange, address, args):
                    trades_in_cycle = 0
                    cycle += 1
                    cycle_target = rng.randint(args.flatten_every_min, args.flatten_every_max)
                    LOG.info("CYCLE_START cycle=%d flatten_after_trades=%d reason=paused_liquidation_guard", cycle, cycle_target)
            except Exception as exc:
                LOG.error("PAUSED_RISK_CHECK failed (%s); remaining paused", type(exc).__name__)
        if time.monotonic() - getattr(args, "fee_last_check", 0.0) >= args.fee_check_seconds:
            try:
                fee_budget_check(args, info, address)
            except Exception as exc:
                args.fee_paused = True
                args.fee_last_check = time.monotonic()
                LOG.critical("FEE_BUDGET fees_24h=unknown budget=$%s runway_days=unknown action=pause_data_unavailable error=%s", args.daily_fee_budget_usd, type(exc).__name__)

        if args.fee_paused:
            if args.once:
                LOG.warning("ONCE skipped: fee budget guard is paused")
                break
            remaining = max(1.0, args.fee_check_seconds - (time.monotonic() - args.fee_last_check))
            LOG.warning("FEE_BUDGET_PAUSED wait_seconds=%.1f", min(remaining, 60.0))
            STOP.wait(min(remaining, 60.0))
            continue

        args.current_burst_id = args.pending_burst_id
        if args.current_burst_id is not None:
            LOG.info("BURST_TRADE id=%d trade=%d", args.current_burst_id, trade_number + 1)
        args.pending_burst_id = None
        trade_number += 1
        try:
            fresh_market, fresh_market_index = refresh_market_metadata(info)
            fresh_config = leverage_settings(fresh_market)
            if fresh_config != getattr(args, "leverage_config", None):
                if not acknowledge_leverage(exchange, fresh_market, "market_metadata_change"):
                    raise RuntimeError("could not acknowledge changed max leverage/margin mode")
                args.leverage_config = fresh_config
            market = fresh_market
            market_index = fresh_market_index
            sz_decimals = int(market["szDecimals"])
            max_leverage = Decimal(str(market.get("maxLeverage", "1")))
            margin = margin_snapshot(info, address, max_leverage, args.max_position_usd, market, market_index)
            risk_distance = Decimal(margin["liquidation_distance_pct"])
            if risk_distance >= 0 and risk_distance < Decimal("10") and Decimal(margin["position"]) != 0:
                if not try_liquidation_margin_topup(exchange, address, args, market, margin):
                    flatten_position(exchange, address, args, sz_decimals, "liquidation_distance_under_10pct")
                else:
                    margin = margin_snapshot(info, address, max_leverage, args.max_position_usd, market, market_index)
                trades_in_cycle = 0
                cycle += 1
                cycle_target = rng.randint(args.flatten_every_min, args.flatten_every_max)
                LOG.info("CYCLE_START cycle=%d flatten_after_trades=%d reason=liquidation_guard", cycle, cycle_target)
            else:
                after, _side = submit_random_trade(
                    exchange,
                    address,
                    args,
                    sz_decimals,
                    max_leverage,
                    trade_number,
                    market,
                    market_index,
                    margin,
                )
                post_market, post_market_index = refresh_market_metadata(info)
                post_leverage = Decimal(str(post_market.get("maxLeverage", "1")))
                post_margin = margin_snapshot(info, address, post_leverage, args.max_position_usd, post_market, post_market_index)
                post_distance = Decimal(post_margin["liquidation_distance_pct"])
                if post_distance >= 0 and post_distance < Decimal("10") and Decimal(post_margin["position"]) != 0:
                    if not try_liquidation_margin_topup(exchange, address, args, post_market, post_margin):
                        flatten_position(exchange, address, args, sz_decimals, "liquidation_distance_under_10pct")
                    trades_in_cycle = 0
                    cycle += 1
                    cycle_target = rng.randint(args.flatten_every_min, args.flatten_every_max)
                    LOG.info("CYCLE_START cycle=%d flatten_after_trades=%d reason=liquidation_guard", cycle, cycle_target)
                else:
                    current_bid, current_ask = best_prices(info)
                    position_notional = after * ((current_bid + current_ask) / Decimal(2))
                    effective_position_cap = min(args.max_position_usd, Decimal(post_margin["effective_position_limit"]))
                    if abs(position_notional) > args.max_position_usd:
                        flatten_position(exchange, address, args, sz_decimals, "max_position")
                        trades_in_cycle = 0
                    elif abs(position_notional) > effective_position_cap:
                        flatten_position(exchange, address, args, sz_decimals, "available_margin_position_cap")
                        trades_in_cycle = 0
                    else:
                        trades_in_cycle += 1
                        if trades_in_cycle >= cycle_target:
                            flatten_position(exchange, address, args, sz_decimals, f"cycle_{cycle}_trades_{trades_in_cycle}")
                            trades_in_cycle = 0
                    if trades_in_cycle == 0:
                        cycle += 1
                        cycle_target = rng.randint(args.flatten_every_min, args.flatten_every_max)
                        LOG.info("CYCLE_START cycle=%d flatten_after_trades=%d", cycle, cycle_target)
        except Exception as exc:
            LOG.error("TRADE number=%d failed (%s): %s; continuing", trade_number, type(exc).__name__, exc)

        if args.once or STOP.is_set():
            break
        wait_seconds, flow_kind = next_flow_delay(args, rng)
        if flow_kind == "burst":
            LOG.info(
                "NEXT_TRADE flow=burst gap_seconds=%.2f regime=%s cycle=%d trades_in_cycle=%d flatten_target=%d",
                wait_seconds,
                args.regime,
                cycle,
                trades_in_cycle,
                cycle_target,
            )
        else:
            LOG.info(
                "NEXT_TRADE flow=poisson wait_seconds=%.2f mean_interval_seconds=%.2f regime=%s multiplier=%.1fx "
                "cycle=%d trades_in_cycle=%d flatten_target=%d",
                wait_seconds,
                args.effective_interval_seconds,
                args.regime,
                REGIME_MULTIPLIERS[args.regime],
                cycle,
                trades_in_cycle,
                cycle_target,
            )
        wait_for_flow_arrival(args, rng, wait_seconds, flow_kind, trade_started)
    LOG.info("STOP trader loop complete")


def main() -> int:
    logging.Formatter.converter = time.gmtime
    logging.basicConfig(level=logging.INFO, format="%(asctime)sZ %(levelname)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S")
    args = parse_args()
    if args.dry_run and args.simulate_minutes is not None:
        info = Info(base_url=TESTNET_URL, skip_ws=True, perp_dexs=[DEX], timeout=10)
        market, _dex_index, _asset_id = load_market(info)
        try:
            best_bid, best_ask = best_prices(info)
            simulation_price = (best_bid + best_ask) / Decimal(2)
            price_source = "mid"
        except RuntimeError:
            meta, contexts = read_with_retries(
                f"metaAndAssetCtxs dex={DEX}",
                lambda: info.post("/info", {"type": "metaAndAssetCtxs", "dex": DEX}),
            )
            index = next((i for i, asset in enumerate(meta.get("universe", [])) if asset.get("name") == COIN), None)
            if index is None or index >= len(contexts):
                raise RuntimeError(f"No testnet price available for simulation market {COIN}")
            simulation_price = Decimal(str(contexts[index].get("oraclePx", "0")))
            if simulation_price <= 0:
                raise RuntimeError(f"No two-sided book or oracle price available for simulation market {COIN}")
            price_source = "oracle_fallback"
            LOG.warning("SIM_PRICE_FALLBACK source=oracle price=%s reason=book_one_sided", simulation_price)
        simulate_flow(args, simulation_price, int(market["szDecimals"]), price_source)
        return 0

    private_key = load_key(args.env_file)
    try:
        wallet = Account.from_key(private_key)
    except Exception as exc:
        raise RuntimeError(f"The configured testnet trader private key could not be parsed ({type(exc).__name__})") from None
    address = args.account_address or wallet.address
    if address.lower() == MAKER_ADDRESS.lower():
        raise RuntimeError("The trader account address resolves to the maker wallet; refusing to self-trade")
    signal.signal(signal.SIGINT, handle_stop)
    signal.signal(signal.SIGTERM, handle_stop)
    signal.signal(signal.SIGHUP, handle_stop)
    exchange = Exchange(wallet, base_url=TESTNET_URL, account_address=address, perp_dexs=[DEX], timeout=10)
    market, dex_index, asset_id = load_market(exchange.info)
    LOG.info(
        "ACCOUNT mode=%s endpoint=%s signer=%s account=%s dex=%s coin=%s dex_index=%d asset_id=%d collateral=USDC szDecimals=%s",
        "DRY_RUN" if args.dry_run else "LIVE",
        TESTNET_URL,
        wallet.address,
        address,
        DEX,
        COIN,
        dex_index,
        asset_id,
        market.get("szDecimals"),
    )
    run(args, exchange, address, market)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        STOP.set()
        raise SystemExit(130)
    except Exception as exc:
        LOG.error("ERROR %s: %s", type(exc).__name__, exc)
        raise SystemExit(1)
