#!/usr/bin/env python3
"""HIP-3 post-only market maker for Hyperliquid testnet or mainnet."""

from __future__ import annotations

import argparse
import logging
import os
import secrets
import signal
import threading
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path
from typing import Any, Callable

from dotenv import dotenv_values
from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants
from hyperliquid.utils.types import Cloid
from hyperliquid.websocket_manager import WebsocketManager

from accounting import SUPPORTED_ACCOUNT_MODES, account_mode, free_usdc, try_add_isolated_margin


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_DIR = SCRIPT_DIR.parent
# Searched in this order when --env-file is not given.
ENV_FILE_CANDIDATES = (SCRIPT_DIR / ".env", REPO_DIR / ".env", REPO_DIR.parent / ".env")
LOG = logging.getLogger("hyperliquid_mm")
STOP = threading.Event()
CLOID_PREFIX = "0x484c4d4d"  # ASCII "HLMM" followed by 12 random bytes.


@dataclass(frozen=True)
class Network:
    url: str
    dex: str
    coin: str
    key_names: tuple[str, ...]
    start_bps: Decimal
    end_bps: Decimal
    notional_usd: Decimal
    min_free_usd: Decimal
    max_position_usd: Decimal


NETWORKS = {
    "testnet": Network(
        constants.TESTNET_API_URL, "bart", "bart:AAPL", ("TEST_HYPERLIQUID_PRIVATE_KEY", "TEST_HYPERLIQUID_KEY"),
        Decimal("2"), Decimal("25"), Decimal("2000"), Decimal("2500"), Decimal("25000"),
    ),
    # The mainnet ladder starts outside BVIV's 3 bp maker fee (no HIP-3 growth
    # mode as of 2026-10-09), so a fill at the inside level is not a sure loss.
    # A separate key name keeps a testnet key from ever signing on mainnet.
    "mainnet": Network(
        constants.MAINNET_API_URL, "mkts", "mkts:BVIV", ("HYPERLIQUID_PRIVATE_KEY", "PROD_HYPERLIQUID_MM_PRIVATE_KEY"),
        Decimal("10"), Decimal("50"), Decimal("500"), Decimal("100"), Decimal("2500"),
    ),
}


class OracleFeed:
    """Reconnectable websocket cache for the HIP-3 active asset context."""

    def __init__(self, info: Info, coin: str, base_url: str):
        self.info = info
        self.coin = coin
        self.base_url = base_url
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.oracle: Decimal | None = None
        self.updated_at = 0.0
        self.thread = threading.Thread(target=self._run, name="oracle-ws", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        manager = getattr(self, "manager", None)
        if manager is not None:
            try:
                manager.stop()
            except Exception:
                pass
        if self.thread.is_alive():
            self.thread.join(timeout=3)

    def _on_message(self, message: dict[str, Any]) -> None:
        try:
            raw = message["data"]["ctx"]["oraclePx"]
            value = Decimal(str(raw))
            if value > 0:
                with self.lock:
                    self.oracle = value
                    self.updated_at = time.monotonic()
        except (KeyError, TypeError, ValueError):
            LOG.warning("ORACLE_WS malformed activeAssetCtx message")

    def latest(self, max_age_seconds: float = 10.0) -> Decimal | None:
        with self.lock:
            if self.oracle is None or time.monotonic() - self.updated_at > max_age_seconds:
                return None
            return self.oracle

    def _run(self) -> None:
        while not self.stop_event.is_set() and not STOP.is_set():
            manager = WebsocketManager(self.base_url)
            self.manager = manager
            self.info.ws_manager = manager
            try:
                manager.start()
                self.info.subscribe({"type": "activeAssetCtx", "coin": self.coin}, self._on_message)
                LOG.info("ORACLE_WS connecting coin=%s", self.coin)
                connected_at = time.monotonic()
                while not self.stop_event.is_set() and not STOP.is_set():
                    if not manager.is_alive() or not manager.ws.keep_running:
                        break
                    if not manager.ws_ready and time.monotonic() - connected_at > 20:
                        break
                    self.stop_event.wait(1.0)
            except Exception as exc:
                LOG.warning("ORACLE_WS connection failed (%s); reconnecting", type(exc).__name__)
            try:
                manager.stop()
                manager.join(timeout=2)
            except Exception:
                pass
            if not self.stop_event.is_set() and not STOP.is_set():
                LOG.warning("ORACLE_WS disconnected; retrying in 2 seconds")
                self.stop_event.wait(2.0)


@dataclass(frozen=True)
class Quote:
    is_buy: bool
    price: Decimal
    size: Decimal
    cloid: Cloid | None = None

    @property
    def notional(self) -> Decimal:
        return self.price * self.size


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="HIP-3 post-only market maker for Hyperliquid testnet or mainnet")
    parser.add_argument(
        "--network",
        choices=tuple(NETWORKS),
        default="testnet",
        help="testnet (default) or mainnet; mainnet signs with HYPERLIQUID_PRIVATE_KEY (or PROD_HYPERLIQUID_MM_PRIVATE_KEY) and trades real funds",
    )
    parser.add_argument("--dex", help="HIP-3 perp DEX (default: bart on testnet, mkts on mainnet)")
    parser.add_argument("--coin", help="fully qualified perp name (default: bart:AAPL on testnet, mkts:BVIV on mainnet)")
    parser.add_argument("--ref", choices=("oracle", "mid"), default="oracle", help="ladder and move-trigger reference")
    parser.add_argument("--dry-run", action="store_true", help="print a ladder; never sign or send exchange actions")
    parser.add_argument(
        "--env-file",
        type=Path,
        help="read the key from this .env file (default: search python-sdk/.env, the repo's .env, then its parent's .env)",
    )
    parser.add_argument("--account-address", help="account address if the signing key is an authorized agent key")
    parser.add_argument("--count", type=int, default=20, help="orders per side (default: 20)")
    parser.add_argument("--start-bps", type=Decimal, help="nearest level offset (default: 2 bps testnet, 10 bps mainnet)")
    parser.add_argument("--end-bps", type=Decimal, help="furthest level offset (default: 25 bps testnet, 50 bps mainnet)")
    parser.add_argument("--fixed-bid", type=Decimal, help="pin the best bid here; remaining bids step down")
    parser.add_argument("--fixed-ask", type=Decimal, help="pin the best ask here; remaining asks step up")
    parser.add_argument("--notional-usd", type=Decimal, help="target resting notional per side (default: $2000 testnet, $500 mainnet)")
    parser.add_argument("--min-free-usd", type=Decimal, help="keep at least this much USDC free after quoting (default: $2500 testnet, $100 mainnet)")
    parser.add_argument("--requote-seconds", type=float, default=60.0, help="timer re-quote interval")
    parser.add_argument("--move-bps", type=Decimal, default=Decimal("5"), help="reference move that triggers a re-quote")
    parser.add_argument("--poll-seconds", type=float, default=2.0, help="reference and position poll interval")
    parser.add_argument("--refill-pct", type=Decimal, default=Decimal("70"), help="re-quote if either side falls below this percent of its guarded target")
    parser.add_argument("--max-position-usd", type=Decimal, help="max absolute target-market position (default: $25000 testnet, $2500 mainnet)")
    parser.add_argument("--liquidation-skew-min-position-usd", type=Decimal, default=Decimal("500"), help="ignore liquidation skew for smaller positions")
    parser.add_argument("--duration-seconds", type=float, default=0.0, help="stop after this many seconds (0 runs until signalled)")
    args = parser.parse_args(argv)

    network = NETWORKS[args.network]
    args.base_url = network.url
    args.key_names = network.key_names
    for name in ("dex", "coin", "start_bps", "end_bps", "notional_usd", "min_free_usd", "max_position_usd"):
        if getattr(args, name) is None:
            setattr(args, name, getattr(network, name))

    if not args.dex or ":" in args.dex or args.coin.split(":", 1)[0] != args.dex or ":" not in args.coin:
        parser.error("--coin must be fully qualified and start with --dex, e.g. --dex mkts --coin mkts:BVIV")
    if (args.fixed_bid is None) != (args.fixed_ask is None):
        parser.error("--fixed-bid and --fixed-ask must be supplied together")
    if args.fixed_bid is not None and (args.fixed_bid <= 0 or args.fixed_ask <= 0):
        parser.error("--fixed-bid and --fixed-ask must be positive")
    if args.count < 1:
        parser.error("--count must be at least 1")
    if args.start_bps < 0 or args.end_bps < args.start_bps:
        parser.error("require 0 <= --start-bps <= --end-bps")
    if args.notional_usd <= 0 or args.min_free_usd <= 0 or args.max_position_usd <= 0 or args.move_bps <= 0 or args.liquidation_skew_min_position_usd < 0:
        parser.error("--notional-usd, --min-free-usd, --max-position-usd, and --move-bps must be positive")
    if args.refill_pct <= 0 or args.refill_pct > 100:
        parser.error("--refill-pct must be greater than 0 and at most 100")
    if args.requote_seconds <= 0 or args.poll_seconds < 1 or args.poll_seconds > 2:
        parser.error("--requote-seconds must be positive and --poll-seconds must be from 1 to 2")
    if args.duration_seconds < 0:
        parser.error("--duration-seconds cannot be negative")
    return args


def candidate_env_files(explicit: Path | None) -> list[Path]:
    if explicit is not None:
        return [explicit.expanduser().resolve()]
    return [path for path in ENV_FILE_CANDIDATES if path.is_file()]


def load_private_key(env_file: Path | None, key_names: tuple[str, ...], required: bool) -> str | None:
    candidates = candidate_env_files(env_file)
    if env_file is not None and required and not candidates[0].is_file():
        raise RuntimeError(f"The requested .env file does not exist: {candidates[0]}")
    loaded: list[dict[str, str | None]] = []
    for path in candidates:
        try:
            loaded.append(dotenv_values(path))
        except OSError:
            loaded.append({})
    for key_name in key_names:
        for values in loaded:
            value = values.get(key_name)
            if value and value.strip():
                return value.strip()
    if required:
        searched = ", ".join(str(path) for path in candidates) or "no .env file found"
        raise RuntimeError(
            f"{' or '.join(key_names)} was not found ({searched}). "
            "Set it in a .env file or pass --env-file."
        )
    return None


def read_with_retries(label: str, request: Callable[[], Any], attempts: int = 5) -> Any:
    delay = 1.0
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return request()
        except Exception as exc:  # SDK uses requests exceptions for HTTP and connection failures.
            last_error = exc
            if attempt == attempts:
                break
            LOG.warning("%s failed (%s); retrying in %.1fs (%d/%d)", label, type(exc).__name__, delay, attempt, attempts)
            STOP.wait(delay)
            delay = min(delay * 2, 15.0)
    raise RuntimeError(f"{label} failed after {attempts} attempts ({type(last_error).__name__})") from last_error


def load_market(info: Info, dex: str, coin: str) -> tuple[dict[str, Any], int, int, int]:
    perp_dexes = read_with_retries("perpDexs", info.perp_dexs)
    dex_index = next((i for i, item in enumerate(perp_dexes) if item and item.get("name") == dex), None)
    if dex_index is None:
        raise RuntimeError(f"Hyperliquid does not list the {dex!r} perp DEX")

    meta = read_with_retries(f"meta dex={dex}", lambda: info.meta(dex=dex))
    meta_and_contexts = read_with_retries(
        f"metaAndAssetCtxs dex={dex}",
        lambda: info.post("/info", {"type": "metaAndAssetCtxs", "dex": dex}),
    )
    collateral_index = meta_and_contexts[0].get("collateralToken")
    spot_meta = read_with_retries("spotMeta", info.spot_meta)
    collateral = next(
        (token.get("name") for token in spot_meta.get("tokens", []) if token.get("index") == collateral_index),
        None,
    )
    if collateral != "USDC":
        raise RuntimeError(f"The {dex} perp DEX collateral is {collateral!r}, not USDC; refusing to quote")

    market_index = next((i for i, asset in enumerate(meta.get("universe", [])) if asset.get("name") == coin), None)
    if market_index is None:
        raise RuntimeError(f"Hyperliquid {dex!r} metadata does not list {coin}")
    asset_id = 100_000 + dex_index * 10_000 + market_index
    return meta["universe"][market_index], dex_index, market_index, asset_id


def refresh_market_metadata(info: Info, dex: str, coin: str) -> tuple[dict[str, Any], int]:
    """Read current leverage/mode metadata while ensuring the SDK asset map remains aligned."""
    meta = read_with_retries(f"meta dex={dex}", lambda: info.meta(dex=dex))
    market_index = next((i for i, asset in enumerate(meta.get("universe", [])) if asset.get("name") == coin), None)
    if market_index is None:
        raise RuntimeError(f"Current {dex} metadata no longer lists {coin}")
    mapped_asset = info.name_to_asset(coin)
    if mapped_asset % 10_000 != market_index:
        raise RuntimeError(f"SDK asset map for {coin} is stale (market index {mapped_asset % 10_000} != {market_index}); refusing to sign")
    return meta["universe"][market_index], market_index


def leverage_settings(market: dict[str, Any]) -> tuple[int, bool]:
    leverage = int(Decimal(str(market.get("maxLeverage", "1"))))
    mode = str(market.get("marginMode", "")).lower()
    isolated = bool(market.get("onlyIsolated", False)) or "isolated" in mode or mode in {"nocross", "no_cross"}
    return leverage, not isolated


def acknowledge_leverage(exchange: Exchange, coin: str, market: dict[str, Any], reason: str) -> bool:
    leverage, is_cross = leverage_settings(market)
    try:
        response = exchange.update_leverage(leverage, coin, is_cross=is_cross)
        ok = isinstance(response, dict) and response.get("status") == "ok"
        if ok:
            LOG.warning(
                "LEVERAGE_ACK reason=%s coin=%s leverage=%sx margin_mode=%s is_cross=%s status=ok",
                reason,
                coin,
                leverage,
                market.get("marginMode", "unknown"),
                is_cross,
            )
        else:
            LOG.error("LEVERAGE_ACK_FAILED reason=%s coin=%s leverage=%sx margin_mode=%s response=%s", reason, coin, leverage, market.get("marginMode", "unknown"), response)
        return ok
    except Exception as exc:
        LOG.error("LEVERAGE_ACK_FAILED reason=%s coin=%s leverage=%sx margin_mode=%s error=%s", reason, coin, leverage, market.get("marginMode", "unknown"), type(exc).__name__)
        return False


def is_margin_tier_reject(value: Any) -> bool:
    text = str(value).lower()
    return "margin tier" in text or "manually set leverage" in text or "automatically accept new margin" in text


def is_margin_reject(value: Any) -> bool:
    text = str(value).lower()
    return is_margin_tier_reject(text) or "margin" in text or "insufficient balance" in text


def position_record(state: dict[str, Any], coin: str) -> dict[str, Any]:
    for entry in state.get("assetPositions", []):
        position = entry.get("position", {})
        if position.get("coin") == coin:
            return position
    return {}


def liquidation_distance(position: dict[str, Any], size: Decimal, oracle: Decimal) -> Decimal | None:
    liq_value = position.get("liquidationPx")
    if liq_value in (None, "", "0", 0) or size == 0 or oracle <= 0:
        return None
    liq = Decimal(str(liq_value))
    if liq <= 0:
        return None
    if size > 0:
        return max(Decimal(0), (oracle - liq) / oracle * Decimal(100))
    return max(Decimal(0), (liq - oracle) / oracle * Decimal(100))


def get_reference_price(info: Info, dex: str, coin: str, ref: str, market_index: int) -> Decimal:
    if ref == "mid":
        mids = read_with_retries(f"allMids dex={dex}", lambda: info.all_mids(dex=dex))
        value = mids.get(coin)
    else:
        meta_contexts = read_with_retries(
            f"metaAndAssetCtxs dex={dex}",
            lambda: info.post("/info", {"type": "metaAndAssetCtxs", "dex": dex}),
        )
        contexts = meta_contexts[1]
        value = contexts[market_index].get("oraclePx") if market_index < len(contexts) else None
    if value is None or Decimal(str(value)) <= 0:
        raise RuntimeError(f"Could not read a positive {ref} reference for {coin} on dex={dex}")
    return Decimal(str(value))


def get_position(info: Info, address: str, dex: str, coin: str, reference: Decimal) -> tuple[Decimal, Decimal, dict[str, Any]]:
    state = read_with_retries(f"clearinghouseState dex={dex}", lambda: info.user_state(address, dex=dex))
    size = Decimal("0")
    for entry in state.get("assetPositions", []):
        position = entry.get("position", {})
        if position.get("coin") == coin:
            size = Decimal(str(position.get("szi", "0")))
            break
    return size, size * reference, state


def format_decimal(value: Decimal) -> str:
    return format(value.normalize(), "f")


def requote_reason(
    reference: Decimal,
    quote_reference: Decimal,
    elapsed_seconds: float,
    move_threshold_bps: Decimal,
    timer_seconds: float,
    ref: str,
    fixed_mode: bool,
) -> str | None:
    moved_bps = abs(reference - quote_reference) / quote_reference * Decimal(10_000)
    if fixed_mode:
        if elapsed_seconds >= timer_seconds:
            return f"timer elapsed_seconds={int(elapsed_seconds)} fixed_price_mode=true"
        return None
    if moved_bps > move_threshold_bps:
        label = "oracle_move" if ref == "oracle" else "mid_move"
        return f"{label} delta_bps={format_decimal(moved_bps)}"
    if elapsed_seconds >= timer_seconds:
        return f"timer elapsed_seconds={int(elapsed_seconds)}"
    return None


def normalize_price(price: Decimal, sz_decimals: int, is_buy: bool) -> Decimal:
    """Apply perp five-significant-figure and (6 - szDecimals) decimal limits."""
    if price <= 0:
        raise ValueError("price must be positive")
    significant_places = max(0, 4 - price.adjusted())
    decimal_places = min(significant_places, 6 - sz_decimals)
    quantum = Decimal(1).scaleb(-decimal_places)
    return price.quantize(quantum, rounding=ROUND_FLOOR if is_buy else ROUND_CEILING)


def make_ladder(
    reference: Decimal,
    oracle_price: Decimal,
    is_buy: bool,
    count: int,
    start_bps: Decimal,
    end_bps: Decimal,
    target_notional: Decimal,
    sz_decimals: int,
    best_bid: Decimal | None,
    best_ask: Decimal | None,
    fixed_anchor: Decimal | None = None,
) -> tuple[list[Quote], int, int]:
    if target_notional <= 0:
        return [], 0, 0
    if fixed_anchor is not None:
        offsets = [Decimal(0)]
        if count > 1:
            away_offsets = [end_bps] if count == 2 else [
                start_bps + (end_bps - start_bps) * Decimal(i) / Decimal(count - 2) for i in range(count - 1)
            ]
            offsets.extend(away_offsets)
        raw_anchor = fixed_anchor
        anchor = normalize_price(raw_anchor, sz_decimals, is_buy)
        anchor_adjustments = 1 if anchor != raw_anchor else 0
        if anchor != raw_anchor:
            LOG.warning("PRICE_TICK_ADJUST side=%s requested=%s valid=%s", "BID" if is_buy else "ASK", raw_anchor, anchor)
        fixed_anchor = anchor
    else:
        offsets = [start_bps] if count == 1 else [
            start_bps + (end_bps - start_bps) * Decimal(i) / Decimal(count - 1) for i in range(count)
        ]
        anchor_adjustments = 0

    max_bid = normalize_price(oracle_price * (Decimal(1) + Decimal(1000) / Decimal(10_000)), sz_decimals, True)
    min_ask = normalize_price(oracle_price * (Decimal(1) - Decimal(1000) / Decimal(10_000)), sz_decimals, False)
    if fixed_anchor is not None:
        original_anchor = fixed_anchor
        if is_buy and fixed_anchor > max_bid:
            fixed_anchor = max_bid
        elif not is_buy and fixed_anchor < min_ask:
            fixed_anchor = min_ask
        if fixed_anchor != original_anchor:
            anchor_adjustments += 1
            LOG.warning(
                "ORACLE_BAND_CLAMP side=%s requested_anchor=%s clamped_anchor=%s oracle=%s band_bps=1000",
                "BID" if is_buy else "ASK",
                original_anchor,
                fixed_anchor,
                oracle_price,
            )

    prices: list[Decimal] = []
    skipped_crossing = 0
    clamped_levels = anchor_adjustments
    for offset in offsets:
        direction = Decimal(-1) if is_buy else Decimal(1)
        base = fixed_anchor if fixed_anchor is not None else reference
        price = normalize_price(base * (Decimal(1) + direction * offset / Decimal(10_000)), sz_decimals, is_buy)
        unclamped_price = price
        if is_buy and price > max_bid:
            price = max_bid
        elif not is_buy and price < min_ask:
            price = min_ask
        if price != unclamped_price:
            clamped_levels += 1
            LOG.warning(
                "ORACLE_BAND_CLAMP side=%s level_px=%s clamped_px=%s oracle=%s band_bps=1000",
                "BID" if is_buy else "ASK",
                unclamped_price,
                price,
                oracle_price,
            )
        if price <= 0 or price in prices:
            continue
        crosses = (is_buy and best_ask is not None and price >= best_ask) or (
            not is_buy and best_bid is not None and price <= best_bid
        )
        if crosses:
            skipped_crossing += 1
            LOG.warning("ALO_CROSS_SKIP side=%s px=%s best_bid=%s best_ask=%s", "BID" if is_buy else "ASK", price, best_bid, best_ask)
            continue
        prices.append(price)

    if not prices:
        return [], skipped_crossing, clamped_levels
    lot = Decimal(1).scaleb(-sz_decimals)
    per_level = target_notional / len(prices)
    remaining = target_notional
    quotes: list[Quote] = []
    for index, price in enumerate(prices):
        allocation = remaining if index == len(prices) - 1 else per_level
        size = (allocation / price).quantize(lot, rounding=ROUND_FLOOR)
        if size <= 0:
            continue
        # Perp orders below $10 are rejected by the exchange. Risk skew can
        # intentionally reduce one side to 10% of the target, so round those
        # levels up to the smallest valid lot instead of losing that side.
        min_size = (Decimal("10") / price).quantize(lot, rounding=ROUND_CEILING)
        if size < min_size:
            size = min_size
            LOG.warning(
                "MIN_ORDER_NOTIONAL_ADJUST side=%s px=%s size=%s notional=$%s minimum_usd=10",
                "BID" if is_buy else "ASK",
                price,
                size,
                format_decimal(size * price),
            )
        quote = Quote(is_buy=is_buy, price=price, size=size)
        quotes.append(quote)
        remaining = max(Decimal(0), remaining - quote.notional)
    return quotes, skipped_crossing, clamped_levels


def book_snapshot(info: Info, coin: str) -> tuple[Decimal | None, Decimal | None, str]:
    book = read_with_retries(f"l2Book coin={coin}", lambda: info.l2_snapshot(coin))
    levels = book.get("levels", [[], []])
    bids = levels[0] if len(levels) > 0 else []
    asks = levels[1] if len(levels) > 1 else []
    best_bid = Decimal(str(bids[0]["px"])) if bids else None
    best_ask = Decimal(str(asks[0]["px"])) if asks else None
    summary = f"best_bid={best_bid}, best_ask={best_ask}"
    return best_bid, best_ask, summary


def log_ladder(bid_quotes: list[Quote], ask_quotes: list[Quote]) -> None:
    for side, quotes in (("BID", bid_quotes), ("ASK", ask_quotes)):
        total = sum((quote.notional for quote in quotes), Decimal(0))
        LOG.info("LADDER side=%s levels=%d notional=$%s", side, len(quotes), format_decimal(total))
        for quote in quotes:
            LOG.info(
                "  %s px=%s sz=%s notional=$%s",
                side,
                format_decimal(quote.price),
                format_decimal(quote.size),
                format_decimal(quote.notional),
            )


def current_market_orders(info: Info, address: str, dex: str, coin: str) -> list[dict[str, Any]]:
    orders = read_with_retries(f"openOrders dex={dex}", lambda: info.open_orders(address, dex=dex))
    return [order for order in orders if order.get("coin") == coin]


def cancel_market_orders(exchange: Exchange, address: str, dex: str, coin: str, attempts: int = 4) -> bool:
    for attempt in range(1, attempts + 1):
        orders = current_market_orders(exchange.info, address, dex, coin)
        if not orders:
            return True
        cancels = [{"coin": coin, "oid": int(order["oid"])} for order in orders]
        try:
            response = exchange.bulk_cancel(cancels)
            if response.get("status") != "ok":
                LOG.warning("CANCEL response status=%s; reconciling orders", response.get("status"))
            else:
                LOG.info("CANCEL requested dex=%s coin=%s count=%d", dex, coin, len(cancels))
        except Exception as exc:
            LOG.warning("CANCEL request failed (%s); reconciling orders", type(exc).__name__)
        STOP.wait(min(0.5 * attempt, 2.0))
    remaining = current_market_orders(exchange.info, address, dex, coin)
    if remaining:
        LOG.error("CANCEL incomplete; %d %s order(s) remain", len(remaining), coin)
        return False
    return True


def account_margin(state: dict[str, Any]) -> tuple[Decimal, Decimal]:
    margin = state.get("marginSummary", {})
    return Decimal(str(margin.get("accountValue", "0"))), Decimal(str(state.get("withdrawable", "0")))


def check_margin(
    info: Info,
    address: str,
    dex: str,
    coin: str,
    state: dict[str, Any],
    leverage: Decimal,
    target_per_side: Decimal,
    max_position_usd: Decimal,
    min_free_usd: Decimal,
    position_usd: Decimal,
) -> Decimal:
    abstraction = read_with_retries("userAbstraction", lambda: info.query_user_abstraction_state(address))
    dex_abstraction = read_with_retries("userDexAbstraction", lambda: info.query_user_dex_abstraction_state(address))
    active = read_with_retries(
        f"activeAssetData coin={coin}",
        lambda: info.post("/info", {"type": "activeAssetData", "user": address, "coin": coin}),
    )
    available = active.get("availableToTrade")
    if not isinstance(available, (list, tuple)) or len(available) != 2:
        raise RuntimeError(f"Could not read both-side {coin} trade capacity; refusing to place orders")
    active_leverage_data = active.get("leverage", {})
    active_leverage = Decimal(str(active_leverage_data.get("value", leverage))) if isinstance(active_leverage_data, dict) else leverage
    active_leverage = min(active_leverage, leverage)
    if active_leverage <= 0:
        raise RuntimeError(f"activeAssetData returned no valid leverage for {coin}; refusing to place orders")

    # availableToTrade is available collateral in quote units; leverage converts it to notional capacity.
    buy_available = Decimal(str(available[0])) * active_leverage
    sell_available = Decimal(str(available[1])) * active_leverage
    account_value, withdrawable = account_margin(state)
    usdc_free = free_usdc(info, address, dex, abstraction)
    active_margin_free = max(Decimal(0), min(Decimal(str(available[0])), Decimal(str(available[1]))))
    free_before_quote = min(usdc_free, active_margin_free)
    reserve_notional = min_free_usd * active_leverage
    # Budget collateral for both resting sides, conservatively assuming both
    # sides can reserve initial margin at once. The post-quote spot check below
    # is the final guard and cancels the ladder if the reserve is breached.
    quote_margin_capacity = max(Decimal(0), free_before_quote - min_free_usd) * active_leverage / 2
    available_total = min(buy_available, sell_available)
    largest_per_side = min(quote_margin_capacity, available_total)
    args_notional = min(target_per_side, largest_per_side).quantize(Decimal("1"), rounding=ROUND_FLOOR)
    LOG.info(
        "MARGIN dex=%s abstraction=%s dex_abstraction=%s account_value=$%s withdrawable=$%s "
        "usdc_free=$%s active_margin_free=$%s transferable_free_before_quote=$%s reserve_free_usd=$%s "
        "buy_notional_capacity=$%s sell_notional_capacity=$%s position_guard=$%s position_notional=$%s "
        "active_leverage=%sx reserve_notional=$%s max_per_side=$%s",
        dex,
        abstraction,
        dex_abstraction,
        format_decimal(account_value),
        format_decimal(withdrawable),
        format_decimal(usdc_free),
        format_decimal(active_margin_free),
        format_decimal(free_before_quote),
        format_decimal(min_free_usd),
        format_decimal(buy_available),
        format_decimal(sell_available),
        format_decimal(max_position_usd),
        format_decimal(position_usd),
        format_decimal(active_leverage),
        format_decimal(reserve_notional),
        format_decimal(largest_per_side),
    )
    if args_notional <= 0:
        LOG.error(
            "MARGIN_NO_QUOTE reason=insufficient_free_collateral target_per_side=$%s free_before_quote=$%s reserve_free=$%s; "
            "existing orders are retained and margin will be checked on the next scheduled re-quote",
            format_decimal(target_per_side),
            format_decimal(free_before_quote),
            format_decimal(min_free_usd),
        )
        return Decimal(0)
    if args_notional < target_per_side:
        LOG.warning(
            "MARGIN_NOTIONAL_CAP requested_per_side=$%s fitting_per_side=$%s reason=free_collateral_reserve position_guard=$%s",
            format_decimal(target_per_side),
            format_decimal(args_notional),
            format_decimal(max_position_usd),
        )
    return args_notional


def open_order_totals(orders: list[dict[str, Any]]) -> tuple[Decimal, Decimal]:
    bid = Decimal(0)
    ask = Decimal(0)
    for order in orders:
        amount = Decimal(str(order.get("limitPx", "0"))) * Decimal(str(order.get("sz", "0")))
        if order.get("side") == "B":
            bid += amount
        elif order.get("side") == "A":
            ask += amount
    return bid, ask


def position_guard_targets(position_usd: Decimal, args: argparse.Namespace) -> tuple[Decimal, Decimal]:
    """Do not let one fully filled side exceed the configured absolute position cap."""
    bid_headroom = max(Decimal(0), args.max_position_usd - position_usd)
    ask_headroom = max(Decimal(0), args.max_position_usd + position_usd)
    bid_target = min(args.notional_usd, bid_headroom)
    ask_target = min(args.notional_usd, ask_headroom)
    return bid_target, ask_target


def new_cloid() -> Cloid:
    return Cloid.from_str(CLOID_PREFIX + secrets.token_hex(12))


def note_reject(args: argparse.Namespace, reason: str) -> None:
    args.last_reject_reason = reason
    args.last_reject_at = time.monotonic()


def send_ladder(
    exchange: Exchange,
    args: argparse.Namespace,
    market: dict[str, Any],
    bid_quotes: list[Quote],
    ask_quotes: list[Quote],
) -> bool:
    coin = args.coin
    quotes = [Quote(q.is_buy, q.price, q.size, new_cloid()) for q in bid_quotes + ask_quotes]
    if not quotes:
        LOG.warning("QUOTE skipped: position guard or crossing book leaves no valid orders")
        return False
    requests = [
        {
            "coin": coin,
            "is_buy": quote.is_buy,
            "sz": float(quote.size),
            "limit_px": float(quote.price),
            "order_type": {"limit": {"tif": "Alo"}},
            "reduce_only": False,
            "cloid": quote.cloid,
        }
        for quote in quotes
    ]
    tier_retry_done = False
    try:
        response = exchange.bulk_orders(requests)
    except Exception as exc:
        LOG.error("ORDER batch transport/API error (%s); will reconcile from openOrders", type(exc).__name__)
        note_reject(args, str(exc))
        if is_margin_tier_reject(exc):
            if acknowledge_leverage(exchange, coin, market, "margin_tier_reject"):
                tier_retry_done = True
                try:
                    response = exchange.bulk_orders(requests)
                    LOG.warning("MARGIN_TIER_RETRY batch=true status=%s", response.get("status"))
                except Exception as retry_exc:
                    note_reject(args, str(retry_exc))
                    LOG.error("MARGIN_TIER_RETRY_FAILED error=%s", type(retry_exc).__name__)
                    return is_margin_reject(retry_exc)
            else:
                return True
        elif is_margin_reject(exc):
            LOG.error("MARGIN_REJECTED batch transport response; retaining current orders")
            return True
        else:
            return False

    statuses = response.get("response", {}).get("data", {}).get("statuses", [])
    if response.get("status") != "ok" and not tier_retry_done and is_margin_tier_reject(response) and acknowledge_leverage(exchange, coin, market, "margin_tier_reject"):
        tier_retry_done = True
        try:
            response = exchange.bulk_orders(requests)
            statuses = response.get("response", {}).get("data", {}).get("statuses", [])
            LOG.warning("MARGIN_TIER_RETRY batch=true status=%s", response.get("status"))
        except Exception as retry_exc:
            note_reject(args, str(retry_exc))
            LOG.error("MARGIN_TIER_RETRY_FAILED batch=true error=%s", type(retry_exc).__name__)
            return is_margin_reject(retry_exc)
    if response.get("status") != "ok" or len(statuses) != len(requests):
        if response.get("status") != "ok":
            note_reject(args, str(response))
        LOG.error("ORDER batch status=%s with %d/%d per-order statuses", response.get("status"), len(statuses), len(requests))
    margin_rejected = False
    tier_indexes = [i for i, status in enumerate(statuses) if isinstance(status, dict) and is_margin_tier_reject(status.get("error", ""))]
    if tier_indexes and not tier_retry_done:
        for index in tier_indexes:
            note_reject(args, str(statuses[index].get("error")))
        if acknowledge_leverage(exchange, coin, market, "margin_tier_reject"):
            for index in tier_indexes:
                try:
                    retry_response = exchange.bulk_orders([requests[index]])
                    retry_statuses = retry_response.get("response", {}).get("data", {}).get("statuses", [])
                    if retry_response.get("status") == "ok" and len(retry_statuses) == 1:
                        statuses[index] = retry_statuses[0]
                        LOG.warning("MARGIN_TIER_RETRY side=%s index=%d status=ok", "BID" if quotes[index].is_buy else "ASK", index)
                    else:
                        statuses[index] = {"error": str(retry_response)}
                        note_reject(args, str(retry_response))
                        LOG.error("MARGIN_TIER_RETRY_FAILED side=%s index=%d response=%s", "BID" if quotes[index].is_buy else "ASK", index, retry_response)
                except Exception as retry_exc:
                    statuses[index] = {"error": str(retry_exc)}
                    note_reject(args, str(retry_exc))
                    LOG.error("MARGIN_TIER_RETRY_FAILED side=%s index=%d error=%s", "BID" if quotes[index].is_buy else "ASK", index, type(retry_exc).__name__)
        else:
            margin_rejected = True
    for quote, status in zip(quotes, statuses):
        side = "BID" if quote.is_buy else "ASK"
        cloid = str(quote.cloid)
        if "error" in status:
            reason = str(status["error"])
            note_reject(args, reason)
            if is_margin_reject(reason):
                margin_rejected = True
                LOG.error("ORDER rejected side=%s px=%s sz=%s cloid=%s reason=%s", side, quote.price, quote.size, cloid, reason)
            else:
                LOG.warning("ORDER rejected side=%s px=%s sz=%s cloid=%s reason=%s", side, quote.price, quote.size, cloid, reason)
        elif "resting" in status:
            LOG.info("ORDER resting side=%s px=%s sz=%s oid=%s cloid=%s", side, quote.price, quote.size, status["resting"].get("oid"), cloid)
        elif "filled" in status:
            LOG.warning("ORDER unexpectedly filled side=%s px=%s sz=%s cloid=%s", side, quote.price, quote.size, cloid)
        else:
            LOG.info("ORDER status side=%s cloid=%s result=%s", side, cloid, status)
    return margin_rejected


def cancel_orders(exchange: Exchange, dex: str, coin: str, orders: list[dict[str, Any]], reason: str) -> None:
    if not orders:
        return
    cancels = [{"coin": coin, "oid": int(order["oid"])} for order in orders]
    try:
        response = exchange.bulk_cancel(cancels)
        LOG.info("CANCEL_SUBSET reason=%s dex=%s coin=%s count=%d status=%s", reason, dex, coin, len(cancels), response.get("status"))
    except Exception as exc:
        LOG.error("CANCEL_SUBSET_FAILED reason=%s count=%d error=%s", reason, len(cancels), type(exc).__name__)


def side_orders(orders: list[dict[str, Any]], side: str) -> list[dict[str, Any]]:
    selected = [order for order in orders if order.get("side") == side]
    reverse = side == "B"
    return sorted(selected, key=lambda order: Decimal(str(order.get("limitPx", "0"))), reverse=reverse)


def place_or_modify_ladder(
    exchange: Exchange,
    address: str,
    args: argparse.Namespace,
    market: dict[str, Any],
    bid_quotes: list[Quote],
    ask_quotes: list[Quote],
    bid_target: Decimal,
    ask_target: Decimal,
) -> bool:
    """Refresh quotes in place so a normal re-quote never empties either side."""
    desired = {"B": bid_quotes, "A": ask_quotes}
    targets = {"B": bid_target, "A": ask_target}
    current = current_market_orders(exchange.info, address, args.dex, args.coin)
    modifications: list[dict[str, Any]] = []
    desired_by_side: dict[str, list[Quote]] = {}

    for side in ("B", "A"):
        existing = side_orders(current, side)
        quotes = desired[side]
        desired_by_side[side] = quotes
        pair_count = min(len(existing), len(quotes))
        for order, quote in zip(existing[:pair_count], quotes[:pair_count]):
            quote = Quote(quote.is_buy, quote.price, quote.size, new_cloid())
            modifications.append({
                "oid": int(order["oid"]),
                "order": {
                    "coin": args.coin,
                    "is_buy": quote.is_buy,
                    "sz": float(quote.size),
                    "limit_px": float(quote.price),
                    "order_type": {"limit": {"tif": "Alo"}},
                    "reduce_only": False,
                    "cloid": quote.cloid,
                },
            })

    margin_rejected = False
    if modifications:
        try:
            response = exchange.bulk_modify_orders_new(modifications)
            statuses = response.get("response", {}).get("data", {}).get("statuses", [])
            LOG.info("BATCH_MODIFY dex=%s coin=%s count=%d status=%s per_order_statuses=%d", args.dex, args.coin, len(modifications), response.get("status"), len(statuses))
            for index, status in enumerate(statuses):
                if isinstance(status, dict) and "error" in status:
                    reason = str(status["error"])
                    note_reject(args, reason)
                    if is_margin_reject(reason):
                        margin_rejected = True
                    LOG.warning("BATCH_MODIFY_REJECT index=%d reason=%s", index, reason)
        except Exception as exc:
            LOG.error("BATCH_MODIFY_FAILED count=%d error=%s; retaining and reconciling existing orders", len(modifications), type(exc).__name__)
            note_reject(args, str(exc))
            if is_margin_tier_reject(exc) and acknowledge_leverage(exchange, args.coin, market, "margin_tier_reject"):
                retry_failed = False
                for index, modification in enumerate(modifications):
                    try:
                        retry = exchange.bulk_modify_orders_new([modification])
                        retry_statuses = retry.get("response", {}).get("data", {}).get("statuses", [])
                        if retry.get("status") != "ok" or len(retry_statuses) != 1 or (isinstance(retry_statuses[0], dict) and "error" in retry_statuses[0]):
                            retry_failed = True
                            note_reject(args, str(retry_statuses[0] if retry_statuses else retry))
                            LOG.error("MARGIN_TIER_RETRY_FAILED modify_index=%d result=%s", index, retry_statuses[0] if retry_statuses else retry)
                    except Exception as retry_exc:
                        retry_failed = True
                        note_reject(args, str(retry_exc))
                        LOG.error("MARGIN_TIER_RETRY_FAILED modify_index=%d error=%s", index, type(retry_exc).__name__)
                return retry_failed
            return is_margin_reject(exc)

        tier_indexes = [i for i, status in enumerate(statuses) if isinstance(status, dict) and is_margin_tier_reject(status.get("error", ""))]
        if tier_indexes:
            if acknowledge_leverage(exchange, args.coin, market, "margin_tier_reject"):
                for index in tier_indexes:
                    try:
                        retry = exchange.bulk_modify_orders_new([modifications[index]])
                        retry_statuses = retry.get("response", {}).get("data", {}).get("statuses", [])
                        if retry.get("status") == "ok" and len(retry_statuses) == 1 and not (isinstance(retry_statuses[0], dict) and "error" in retry_statuses[0]):
                            statuses[index] = retry_statuses[0]
                            LOG.warning("MARGIN_TIER_RETRY modify_index=%d status=ok", index)
                        else:
                            statuses[index] = {"error": str(retry_statuses[0] if retry_statuses else retry)}
                            note_reject(args, statuses[index]["error"])
                            LOG.error("MARGIN_TIER_RETRY_FAILED modify_index=%d result=%s", index, statuses[index]["error"])
                    except Exception as retry_exc:
                        statuses[index] = {"error": str(retry_exc)}
                        note_reject(args, str(retry_exc))
                        LOG.error("MARGIN_TIER_RETRY_FAILED modify_index=%d error=%s", index, type(retry_exc).__name__)
                margin_rejected = any(isinstance(status, dict) and "error" in status and is_margin_reject(status.get("error")) for status in statuses)

        if response.get("status") != "ok" and is_margin_tier_reject(response):
            if acknowledge_leverage(exchange, args.coin, market, "margin_tier_reject"):
                try:
                    retry = exchange.bulk_modify_orders_new(modifications)
                    retry_statuses = retry.get("response", {}).get("data", {}).get("statuses", [])
                    LOG.warning("MARGIN_TIER_RETRY batch_modify=true status=%s", retry.get("status"))
                    if retry.get("status") == "ok" and len(retry_statuses) == len(modifications):
                        statuses = retry_statuses
                        margin_rejected = any(isinstance(status, dict) and "error" in status and is_margin_reject(status.get("error")) for status in statuses)
                    else:
                        note_reject(args, str(retry))
                        LOG.error("MARGIN_TIER_RETRY_FAILED batch_modify=true response=%s", retry)
                        return True
                except Exception as retry_exc:
                    note_reject(args, str(retry_exc))
                    LOG.error("MARGIN_TIER_RETRY_FAILED batch_modify=true error=%s", type(retry_exc).__name__)
                    return is_margin_reject(retry_exc)
            else:
                return True
        if response.get("status") != "ok" or len(statuses) != len(modifications):
            # A partial/ambiguous response can leave only some orders changed.
            # Reconcile, but do not add or cancel anything in this pass.
            LOG.error("BATCH_MODIFY_AMBIGUOUS status=%s; deferring additions and surplus cancellation", response.get("status"))
            return margin_rejected
        if any(isinstance(status, dict) and "error" in status for status in statuses):
            # Preserve the other successful modifications and the untouched
            # original orders. A later timer/refill pass will reconcile again.
            return margin_rejected

    current = current_market_orders(exchange.info, address, args.dex, args.coin)
    # Retire surplus only after the in-place updates have completed, retaining
    # the best old/updated order on each side until the new ladder is present.
    add_bids: list[Quote] = []
    add_asks: list[Quote] = []
    for side in ("B", "A"):
        quotes = desired_by_side[side]
        existing = side_orders(current, side)
        if targets[side] <= 0 or not quotes:
            if targets[side] <= 0:
                cancel_orders(exchange, args.dex, args.coin, existing, f"position_guard_{side}")
            elif existing:
                LOG.warning("LADDER_RETAIN side=%s reason=no_valid_alo_levels existing=%d", side, len(existing))
            continue

        if len(existing) > len(quotes):
            # For bids the farthest level is the lowest price; for asks it is
            # the highest. Keep the nearer levels while reducing margin use.
            sorted_near = side_orders(existing, side)
            cancel_orders(exchange, args.dex, args.coin, sorted_near[len(quotes):], "surplus_after_modify")
            current = current_market_orders(exchange.info, address, args.dex, args.coin)
            existing = side_orders(current, side)

        if len(existing) < len(quotes):
            live_prices = {Decimal(str(order.get("limitPx", "0"))) for order in existing}
            missing = [quote for quote in quotes if quote.price not in live_prices]
            needed = max(0, len(quotes) - len(existing))
            if missing and needed:
                additions = missing[:needed]
                if side == "B":
                    add_bids.extend(additions)
                else:
                    add_asks.extend(additions)

    # Submit additions for both sides in one order action. On an empty startup
    # book this avoids exposing a one-sided interval between two HTTP calls.
    if add_bids or add_asks:
        margin_rejected = send_ladder(exchange, args, market, add_bids, add_asks) or margin_rejected
        current = current_market_orders(exchange.info, address, args.dex, args.coin)
        for side in ("B", "A"):
            actual = len(side_orders(current, side))
            target_count = len(desired_by_side[side])
            if targets[side] > 0 and actual < target_count:
                LOG.warning("LADDER_INCOMPLETE side=%s levels=%d target_levels=%d; current orders retained", side, actual, target_count)
    return margin_rejected


def perform_quote(
    exchange: Exchange,
    address: str,
    args: argparse.Namespace,
    market: dict[str, Any],
    market_index: int,
    reason: str,
) -> Decimal:
    market, market_index = refresh_market_metadata(exchange.info, args.dex, args.coin)
    leverage_config = leverage_settings(market)
    if getattr(args, "leverage_config", None) != leverage_config:
        if acknowledge_leverage(exchange, args.coin, market, "market_metadata_refresh"):
            args.leverage_config = leverage_config
        else:
            LOG.error("QUOTE_RETAIN reason=leverage_ack_failed existing_orders_preserved=true")
            return get_reference_price(exchange.info, args.dex, args.coin, "oracle", market_index)
    oracle_price = get_reference_price(exchange.info, args.dex, args.coin, "oracle", market_index)
    reference = oracle_price if args.ref == "oracle" else get_reference_price(exchange.info, args.dex, args.coin, "mid", market_index)
    size, position_usd, state = get_position(exchange.info, address, args.dex, args.coin, reference)
    leverage = Decimal(str(market.get("maxLeverage", "1")))
    requested_per_side = getattr(args, "requested_notional_usd", args.notional_usd)
    args.notional_usd = check_margin(
        exchange.info,
        address,
        args.dex,
        args.coin,
        state,
        leverage,
        requested_per_side,
        args.max_position_usd,
        args.min_free_usd,
        position_usd,
    )
    bid_target, ask_target = position_guard_targets(position_usd, args)
    position = position_record(state, args.coin)
    liq_distance = liquidation_distance(position, size, oracle_price)
    risk_side = None
    if (
        liq_distance is not None
        and liq_distance < Decimal("10")
        and abs(position_usd) >= args.liquidation_skew_min_position_usd
    ):
        retry_after = getattr(args, "liquidation_margin_retry_after", 0.0)
        if time.monotonic() >= retry_after:
            adjustment = try_add_isolated_margin(
                exchange,
                exchange.info,
                address,
                args.dex,
                args.coin,
                market,
                args.min_free_usd,
                LOG,
                minimum_position_usd=args.liquidation_skew_min_position_usd,
            )
            if adjustment.get("fixed"):
                args.liquidation_margin_retry_after = 0.0
                oracle_price = get_reference_price(exchange.info, args.dex, args.coin, "oracle", market_index)
                reference = oracle_price if args.ref == "oracle" else get_reference_price(exchange.info, args.dex, args.coin, "mid", market_index)
                size, position_usd, state = get_position(exchange.info, address, args.dex, args.coin, reference)
                position = position_record(state, args.coin)
                liq_distance = liquidation_distance(position, size, oracle_price)
                args.notional_usd = check_margin(
                    exchange.info,
                    address,
                    args.dex,
                    args.coin,
                    state,
                    leverage,
                    requested_per_side,
                    args.max_position_usd,
                    args.min_free_usd,
                    position_usd,
                )
                bid_target, ask_target = position_guard_targets(position_usd, args)
            else:
                args.liquidation_margin_retry_after = time.monotonic() + 60.0
                LOG.critical(
                    "LIQUIDATION_GUARD action=skew position=%s size=%s liquidation_px=%s oracle=%s distance_pct=%s reason=%s",
                    "long" if size > 0 else "short",
                    format_decimal(size),
                    position.get("liquidationPx"),
                    format_decimal(oracle_price),
                    format_decimal(liq_distance),
                    adjustment.get("reason", "unknown"),
                )
                risk_side = "long" if size > 0 else "short"
        else:
            risk_side = "long" if size > 0 else "short"
    if risk_side is not None:
        reduced_target = args.notional_usd * Decimal("0.10")
        if size > 0:
            bid_target = min(bid_target, reduced_target)
        elif size < 0:
            ask_target = min(ask_target, reduced_target)
    args.liquidation_risk_side = risk_side
    best_bid, best_ask, summary = book_snapshot(exchange.info, args.coin)
    sz_decimals = int(market["szDecimals"])
    bid_start = max(args.start_bps, Decimal("10")) if risk_side == "long" else args.start_bps
    ask_start = max(args.start_bps, Decimal("10")) if risk_side == "short" else args.start_bps
    bids, skipped_bids, clamped_bids = make_ladder(
        reference, oracle_price, True, args.count, bid_start, args.end_bps, bid_target, sz_decimals, best_bid, best_ask,
        fixed_anchor=args.fixed_bid,
    )
    asks, skipped_asks, clamped_asks = make_ladder(
        reference, oracle_price, False, args.count, ask_start, args.end_bps, ask_target, sz_decimals, best_bid, best_ask,
        fixed_anchor=args.fixed_ask,
    )
    LOG.info(
        "REQUOTE reason=%s ref=%s price=%s position=%s (%s, $%s); guard=$%s; leverage=%sx margin_mode=%s liquidation_distance_pct=%s; %s",
        reason,
        "fixed_prices" if args.fixed_bid is not None else args.ref,
        format_decimal(reference),
        format_decimal(size),
        "long" if size > 0 else "short" if size < 0 else "flat",
        format_decimal(position_usd),
        format_decimal(args.max_position_usd),
        leverage,
        market.get("marginMode", "unknown"),
        "unknown" if liq_distance is None else format_decimal(liq_distance),
        summary,
    )
    if skipped_bids or skipped_asks or clamped_bids or clamped_asks:
        LOG.warning(
            "PRICE_FILTER skipped_crossing_bids=%d skipped_crossing_asks=%d clamped_bid_levels=%d clamped_ask_levels=%d",
            skipped_bids,
            skipped_asks,
            clamped_bids,
            clamped_asks,
        )
    if bid_target < args.notional_usd:
        LOG.warning("POSITION_GUARD side=BID target=$%s of $%s position_notional=$%s", format_decimal(bid_target), format_decimal(args.notional_usd), format_decimal(position_usd))
    if ask_target < args.notional_usd:
        LOG.warning("POSITION_GUARD side=ASK target=$%s of $%s position_notional=$%s", format_decimal(ask_target), format_decimal(args.notional_usd), format_decimal(position_usd))
    log_ladder(bids, asks)
    if args.notional_usd <= 0:
        LOG.error("QUOTE_RETAIN reason=no_margin_capacity existing_orders_preserved=true")
        margin_rejected = False
    else:
        margin_rejected = place_or_modify_ladder(exchange, address, args, market, bids, asks, bid_target, ask_target)
    if margin_rejected:
        args.margin_retry_after = time.monotonic() + max(60.0, args.requote_seconds)
        args.margin_pause_logged = False
        LOG.error(
            "MARGIN_REJECTED action=retain_live_orders retry_after_seconds=%.0f; suppressing refill retries",
            max(60.0, args.requote_seconds),
        )

    orders = current_market_orders(exchange.info, address, args.dex, args.coin)
    bid_resting, ask_resting = open_order_totals(orders)
    free_after = free_usdc(exchange.info, address, args.dex, getattr(args, "account_mode", None))
    if orders and free_after < args.min_free_usd:
        LOG.error(
            "MARGIN_RESERVE_BREACH free_after_orders=$%s minimum=$%s; retaining existing quotes and pausing retries until next scheduled re-quote",
            format_decimal(free_after),
            format_decimal(args.min_free_usd),
        )
        args.margin_retry_after = time.monotonic() + max(60.0, args.requote_seconds)
        args.margin_pause_logged = False
    LOG.info(
        "OPEN_ORDERS dex=%s coin=%s count=%d bid_notional=$%s ask_notional=$%s",
        args.dex,
        args.coin,
        len(orders),
        format_decimal(bid_resting),
        format_decimal(ask_resting),
    )
    return reference


def dry_run(
    args: argparse.Namespace,
    info: Info,
    market: dict[str, Any],
    dex_index: int,
    market_index: int,
    asset_id: int,
    address: str | None,
) -> None:
    oracle_price = get_reference_price(info, args.dex, args.coin, "oracle", market_index)
    reference = oracle_price if args.ref == "oracle" else get_reference_price(info, args.dex, args.coin, "mid", market_index)
    best_bid, best_ask, summary = book_snapshot(info, args.coin)
    sz_decimals = int(market["szDecimals"])
    risk_side = None
    if address:
        size, position_usd, state = get_position(info, address, args.dex, args.coin, reference)
        args.notional_usd = check_margin(
            info,
            address,
            args.dex,
            args.coin,
            state,
            Decimal(str(market.get("maxLeverage", "1"))),
            getattr(args, "requested_notional_usd", args.notional_usd),
            args.max_position_usd,
            args.min_free_usd,
            position_usd,
        )
        bid_target, ask_target = position_guard_targets(position_usd, args)
        position = position_record(state, args.coin)
        liq_distance = liquidation_distance(position, size, oracle_price)
        risk_side = None
        if (
            liq_distance is not None
            and liq_distance < Decimal("10")
            and abs(position_usd) >= args.liquidation_skew_min_position_usd
        ):
            risk_side = "long" if size > 0 else "short"
            reduced_target = args.notional_usd * Decimal("0.10")
            if size > 0:
                bid_target = min(bid_target, reduced_target)
            elif size < 0:
                ask_target = min(ask_target, reduced_target)
            LOG.warning("DRY_RUN LIQUIDATION_GUARD position=%s liquidation_px=%s oracle=%s distance_pct=%s reduce_side=%s", risk_side, position.get("liquidationPx"), oracle_price, liq_distance, "ASK" if size > 0 else "BID")
        LOG.info("DRY_RUN position dex=%s coin=%s size=%s notional=$%s liquidation_distance_pct=%s", args.dex, args.coin, format_decimal(size), format_decimal(position_usd), "unknown" if liq_distance is None else format_decimal(liq_distance))
    else:
        bid_target = ask_target = args.notional_usd
        LOG.info("DRY_RUN no key available for position lookup; showing unguarded target notionals")
    bid_start = max(args.start_bps, Decimal("10")) if risk_side == "long" else args.start_bps
    ask_start = max(args.start_bps, Decimal("10")) if risk_side == "short" else args.start_bps
    bids, skipped_bids, clamped_bids = make_ladder(
        reference, oracle_price, True, args.count, bid_start, args.end_bps, bid_target, sz_decimals, best_bid, best_ask,
        fixed_anchor=args.fixed_bid,
    )
    asks, skipped_asks, clamped_asks = make_ladder(
        reference, oracle_price, False, args.count, ask_start, args.end_bps, ask_target, sz_decimals, best_bid, best_ask,
        fixed_anchor=args.fixed_ask,
    )
    LOG.info(
        "DRY_RUN endpoint=%s dex=%s coin=%s dex_index=%d market_index=%d asset_id=%d collateral=USDC szDecimals=%d",
        args.base_url,
        args.dex,
        args.coin,
        dex_index,
        market_index,
        asset_id,
        sz_decimals,
    )
    LOG.info(
        "DRY_RUN ref=%s price=%s oracle=%s max_leverage=%s book=%s",
        "fixed_prices" if args.fixed_bid is not None else args.ref,
        format_decimal(reference),
        format_decimal(oracle_price),
        market.get("maxLeverage"),
        summary,
    )
    if skipped_bids or skipped_asks or clamped_bids or clamped_asks:
        LOG.info(
            "DRY_RUN price_filter skipped_crossing_bids=%d skipped_crossing_asks=%d clamped_bid_levels=%d clamped_ask_levels=%d",
            skipped_bids,
            skipped_asks,
            clamped_bids,
            clamped_asks,
        )
    if bid_target < args.notional_usd:
        LOG.warning("DRY_RUN POSITION_GUARD side=BID target=$%s of $%s", format_decimal(bid_target), format_decimal(args.notional_usd))
    if ask_target < args.notional_usd:
        LOG.warning("DRY_RUN POSITION_GUARD side=ASK target=$%s of $%s", format_decimal(ask_target), format_decimal(args.notional_usd))
    log_ladder(bids, asks)
    LOG.info("DRY_RUN complete: no wallet was created and no exchange action was sent")


def handle_stop(signum: int, _frame: Any) -> None:
    LOG.info("STOP signal=%s received; beginning cleanup", signum)
    STOP.set()


def run_live(
    args: argparse.Namespace,
    private_key: str,
    market: dict[str, Any],
    dex_index: int,
    market_index: int,
    asset_id: int,
) -> None:
    try:
        wallet = Account.from_key(private_key)
    except Exception as exc:
        raise RuntimeError(f"The configured {args.network} private key could not be parsed ({type(exc).__name__})") from None
    address = args.account_address or wallet.address
    exchange = Exchange(
        wallet,
        base_url=args.base_url,
        account_address=address,
        perp_dexs=[args.dex],
        timeout=10,
    )
    info = exchange.info
    mapped_asset = info.name_to_asset(args.coin)
    if mapped_asset != asset_id:
        raise RuntimeError(f"SDK asset map for {args.coin} is {mapped_asset}, expected dynamic HIP-3 asset ID {asset_id}")
    args.account_mode = account_mode(info, address)
    if args.account_mode not in SUPPORTED_ACCOUNT_MODES:
        raise RuntimeError(
            f"Account {address} uses the {args.account_mode!r} account mode; "
            "mm.py supports unified, portfolio-margin and standard (default) accounts"
        )

    if args.network == "mainnet":
        LOG.warning("MAINNET live quoting with real funds on %s; Ctrl-C cancels this bot's orders and exits", args.coin)
    LOG.info(
        "LIVE network=%s endpoint=%s signer=%s account=%s account_mode=%s dex=%s coin=%s dex_index=%d asset_id=%d ref=%s collateral=USDC",
        args.network,
        args.base_url,
        wallet.address,
        address,
        args.account_mode,
        args.dex,
        args.coin,
        dex_index,
        asset_id,
        args.ref,
    )
    oracle_feed = OracleFeed(info, args.coin, args.base_url)
    args.oracle_feed = oracle_feed
    # Take over existing orders in place to keep both sides present during a
    # process restart; orders on other DEXes and markets are untouched.
    existing = current_market_orders(info, address, args.dex, args.coin)
    bid_existing, ask_existing = open_order_totals(existing)
    LOG.info(
        "STARTUP takeover dex=%s coin=%s existing_count=%d bid_notional=$%s ask_notional=$%s action=batchModify",
        args.dex,
        args.coin,
        len(existing),
        format_decimal(bid_existing),
        format_decimal(ask_existing),
    )

    oracle_price = get_reference_price(info, args.dex, args.coin, "oracle", market_index)
    reference = oracle_price if args.ref == "oracle" else get_reference_price(info, args.dex, args.coin, "mid", market_index)
    size, position_usd, _state = get_position(info, address, args.dex, args.coin, reference)
    LOG.info("PREFLIGHT dex=%s coin=%s position_size=%s position_notional=$%s", args.dex, args.coin, format_decimal(size), format_decimal(position_usd))

    started = time.monotonic()
    try:
        oracle_feed.start()
        if STOP.is_set():
            return
        quote_reference = perform_quote(exchange, address, args, market, market_index, "initial")
        quoted_at = time.monotonic()
        last_position = Decimal("NaN")
        degraded_since: float | None = None
        degraded_critical_logged = False
        while not STOP.is_set():
            if args.duration_seconds and time.monotonic() - started >= args.duration_seconds:
                LOG.info("STOP duration_seconds=%s reached", args.duration_seconds)
                break
            STOP.wait(args.poll_seconds)
            if STOP.is_set():
                break
            try:
                oracle_price = oracle_feed.latest()
                if oracle_price is not None:
                    args.last_oracle_http_price = oracle_price
                    args.last_oracle_http_at = time.monotonic()
                elif (
                    getattr(args, "last_oracle_http_price", None) is None
                    or time.monotonic() - getattr(args, "last_oracle_http_at", 0.0) >= 5.0
                ):
                    oracle_price = get_reference_price(info, args.dex, args.coin, "oracle", market_index)
                    args.last_oracle_http_price = oracle_price
                    args.last_oracle_http_at = time.monotonic()
                    LOG.warning("ORACLE_WS stale_or_unavailable=true source=http_fallback")
                else:
                    oracle_price = args.last_oracle_http_price
                reference = oracle_price if args.ref == "oracle" else get_reference_price(info, args.dex, args.coin, "mid", market_index)
                size, position_usd, state = get_position(info, address, args.dex, args.coin, reference)
            except Exception as exc:
                LOG.warning("POLL failed (%s); retaining current quotes", type(exc).__name__)
                continue

            risk_distance = liquidation_distance(position_record(state, args.coin), size, oracle_price)
            risk_side = "long" if size > 0 else "short" if size < 0 else None
            if (
                risk_distance is None
                or risk_distance >= Decimal("10")
                or abs(position_usd) < args.liquidation_skew_min_position_usd
            ):
                risk_side = None
            risk_changed = risk_side != getattr(args, "liquidation_risk_side", None)

            if size != last_position:
                if last_position.is_nan():
                    LOG.info("POSITION dex=%s coin=%s size=%s notional=$%s", args.dex, args.coin, format_decimal(size), format_decimal(position_usd))
                else:
                    LOG.info("POSITION changed old_size=%s new_size=%s notional=$%s", format_decimal(last_position), format_decimal(size), format_decimal(position_usd))
                last_position = size

            if risk_changed:
                reason = f"liquidation_risk_change distance_pct={risk_distance}"
                quote_reference = perform_quote(exchange, address, args, market, market_index, reason)
                quoted_at = time.monotonic()
                continue

            try:
                live_orders = current_market_orders(info, address, args.dex, args.coin)
            except Exception as exc:
                LOG.warning("OPEN_ORDERS poll failed (%s); retaining current quotes", type(exc).__name__)
                continue
            bid_present = bool(side_orders(live_orders, "B"))
            ask_present = bool(side_orders(live_orders, "A"))
            if not bid_present or not ask_present:
                if degraded_since is None:
                    degraded_since = time.monotonic()
                last_reject_at = getattr(args, "last_reject_at", None)
                if (
                    not degraded_critical_logged
                    and getattr(args, "last_reject_reason", None)
                    and last_reject_at is not None
                    and time.monotonic() - degraded_since >= 60
                    and time.monotonic() - last_reject_at >= 60
                ):
                    LOG.critical(
                        "BOOK_DEGRADED duration_seconds=%.0f bid_present=%s ask_present=%s last_reject=%s",
                        time.monotonic() - degraded_since,
                        bid_present,
                        ask_present,
                        getattr(args, "last_reject_reason", "unknown"),
                    )
                    degraded_critical_logged = True
            else:
                degraded_since = None
                degraded_critical_logged = False
            resting_bid, resting_ask = open_order_totals(live_orders)
            guarded_bid_target, guarded_ask_target = position_guard_targets(position_usd, args)
            if (
                risk_distance is not None
                and risk_distance < Decimal("10")
                and abs(position_usd) >= args.liquidation_skew_min_position_usd
            ):
                reduced_target = args.notional_usd * Decimal("0.10")
                if size > 0:
                    guarded_bid_target = min(guarded_bid_target, reduced_target)
                elif size < 0:
                    guarded_ask_target = min(guarded_ask_target, reduced_target)
            refill_fraction = args.refill_pct / Decimal(100)
            refill_side = None
            if guarded_bid_target == 0 and resting_bid > 0:
                refill_side = "BID_GUARD"
            elif guarded_bid_target > 0 and resting_bid < guarded_bid_target * refill_fraction:
                refill_side = "BID"
            if guarded_ask_target == 0 and resting_ask > 0:
                refill_side = "BOTH_GUARD" if refill_side else "ASK_GUARD"
            elif guarded_ask_target > 0 and resting_ask < guarded_ask_target * refill_fraction:
                if refill_side:
                    refill_side = "BOTH"
                else:
                    refill_side = "ASK"
            if refill_side:
                if time.monotonic() < getattr(args, "margin_retry_after", 0.0):
                    if not getattr(args, "margin_pause_logged", False):
                        LOG.error("MARGIN_RETRY_SUPPRESSED seconds_remaining=%.1f reason=recent_margin_reject", args.margin_retry_after - time.monotonic())
                        args.margin_pause_logged = True
                    continue
                LOG.warning(
                    "REFILL_TRIGGER side=%s bid_resting=$%s bid_target=$%s ask_resting=$%s ask_target=$%s threshold_pct=%s",
                    refill_side,
                    format_decimal(resting_bid),
                    format_decimal(guarded_bid_target),
                    format_decimal(resting_ask),
                    format_decimal(guarded_ask_target),
                    format_decimal(args.refill_pct),
                )
                quote_reference = perform_quote(exchange, address, args, market, market_index, f"refill side={refill_side}")
                quoted_at = time.monotonic()
                continue

            elapsed = time.monotonic() - quoted_at
            reason = requote_reason(
                reference,
                quote_reference,
                elapsed,
                args.move_bps,
                args.requote_seconds,
                args.ref,
                fixed_mode=args.fixed_bid is not None,
            )
            if reason is None:
                continue
            if time.monotonic() < getattr(args, "margin_retry_after", 0.0):
                if not getattr(args, "margin_pause_logged", False):
                    LOG.error("MARGIN_RETRY_SUPPRESSED seconds_remaining=%.1f reason=recent_margin_reject", args.margin_retry_after - time.monotonic())
                    args.margin_pause_logged = True
                continue
            args.margin_pause_logged = False
            quote_reference = perform_quote(exchange, address, args, market, market_index, reason)
            quoted_at = time.monotonic()
    finally:
        oracle_feed.stop()
        LOG.info("CLEANUP cancelling all open %s orders on dex=%s", args.coin, args.dex)
        if cancel_market_orders(exchange, address, args.dex, args.coin):
            LOG.info("CLEANUP verified no open %s orders remain", args.coin)
        else:
            LOG.error("CLEANUP could not verify all %s orders were cancelled", args.coin)


def main() -> int:
    logging.Formatter.converter = time.gmtime
    logging.basicConfig(level=logging.INFO, format="%(asctime)sZ %(levelname)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S")
    args = parse_args()
    args.requested_notional_usd = args.notional_usd
    private_key = load_private_key(args.env_file, args.key_names, required=not args.dry_run)

    # Public read-only data works without credentials, keeping dry-run non-signing.
    if args.dry_run:
        info = Info(base_url=args.base_url, skip_ws=True, perp_dexs=[args.dex], timeout=10)
        market, dex_index, market_index, asset_id = load_market(info, args.dex, args.coin)
        account = None
        if private_key:
            try:
                account = Account.from_key(private_key)
            except Exception as exc:
                raise RuntimeError(f"The configured {args.network} private key could not be parsed ({type(exc).__name__})") from None
        address = args.account_address or (account.address if account else None)
        dry_run(args, info, market, dex_index, market_index, asset_id, address)
        return 0

    if private_key is None:
        raise RuntimeError(f"Missing {args.network} signing key")
    market_info = Info(base_url=args.base_url, skip_ws=True, perp_dexs=[args.dex], timeout=10)
    market, dex_index, market_index, asset_id = load_market(market_info, args.dex, args.coin)
    signal.signal(signal.SIGINT, handle_stop)
    signal.signal(signal.SIGTERM, handle_stop)
    signal.signal(signal.SIGHUP, handle_stop)
    run_live(args, private_key, market, dex_index, market_index, asset_id)
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
