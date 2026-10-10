"""Account and margin helpers shared by mm.py, trader.py and status.py."""

from __future__ import annotations

from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import logging
import math
import time
from typing import Any, Callable


MAKER_ADDRESS = "0x67757f723E067acCd93D48fCa07f44F3E3105534"
TRADER_ADDRESS = "0xcbA40Fc37A76835fA5B0F6bcDb4b067b8Fa405d1"
DEX = "bart"
COIN = "bart:AAPL"
MAX_FILLS_PER_RESPONSE = 2000
MAX_FILLS_WINDOW_MS = 2 * 60 * 60 * 1000


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
            response = getattr(exc, "response", None)
            is_429 = getattr(response, "status_code", None) == 429 or "429" in str(exc)
            if is_429:
                delay = min(max(delay * 2, 20.0), 60.0)
                logging.getLogger("hyperliquid_accounting").warning("%s rate-limited; retrying in %.0fs (%d/%d)", label, delay, attempt, attempts)
            else:
                logging.getLogger("hyperliquid_accounting").warning("%s failed (%s); retrying in %.1fs (%d/%d)", label, type(exc).__name__, delay, attempt, attempts)
                delay = min(delay * 2, 15.0)
            time.sleep(delay)
    raise RuntimeError(f"{label} failed after {attempts} attempts ({type(last_error).__name__})") from last_error


def spot_usdc(info: Any, address: str) -> tuple[Decimal, Decimal, Decimal]:
    state = read_with_retries("spotClearinghouseState", lambda: info.spot_user_state(address))
    usdc = next((item for item in state.get("balances", []) if item.get("coin") == "USDC"), {})
    total = Decimal(str(usdc.get("total", "0")))
    held = Decimal(str(usdc.get("hold", "0")))
    return total, held, max(Decimal(0), total - held)


# Hyperliquid account abstraction modes as `userAbstraction` reports them. Unified
# and portfolio-margin accounts keep all USDC in the Spot balance; standard
# ("default") accounts keep a separate USDC balance per perp DEX.
SHARED_SPOT_MODES = {"unifiedAccount", "portfolioMargin"}
SUPPORTED_ACCOUNT_MODES = SHARED_SPOT_MODES | {"default"}


def account_mode(info: Any, address: str) -> str:
    return str(read_with_retries("userAbstraction", lambda: info.query_user_abstraction_state(address)))


def free_usdc(info: Any, address: str, dex: str, mode: str | None = None) -> Decimal:
    """USDC free to margin `dex` orders: Spot total minus holds for unified and
    portfolio-margin accounts, the DEX's own withdrawable balance otherwise."""
    mode = mode or account_mode(info, address)
    if mode in SHARED_SPOT_MODES:
        return spot_usdc(info, address)[2]
    state = read_with_retries(f"clearinghouseState dex={dex}", lambda: info.user_state(address, dex=dex))
    return max(Decimal(0), Decimal(str(state.get("withdrawable", "0"))))


def position_for(state: dict[str, Any], coin: str) -> dict[str, Any]:
    for entry in state.get("assetPositions", []):
        position = entry.get("position", {})
        if position.get("coin") == coin:
            return position
    return {}


def liquidation_distance_pct(position: dict[str, Any], size: Decimal, oracle: Decimal) -> Decimal | None:
    raw = position.get("liquidationPx")
    if raw in (None, "", "0", 0) or size == 0 or oracle <= 0:
        return None
    liquidation = Decimal(str(raw))
    if liquidation <= 0:
        return None
    distance = (oracle - liquidation) / oracle if size > 0 else (liquidation - oracle) / oracle
    return max(Decimal(0), distance * 100)


def isolated_margin_needed(
    position_size: Decimal,
    liquidation_px: Decimal,
    oracle: Decimal,
    target_distance_pct: Decimal = Decimal("16"),
) -> Decimal:
    """Estimate isolated USDC needed to move liq price beyond the target band."""
    if position_size == 0 or liquidation_px <= 0 or oracle <= 0:
        return Decimal(0)
    target_px = oracle * (
        Decimal(1) + target_distance_pct / Decimal(100)
        if position_size < 0
        else Decimal(1) - target_distance_pct / Decimal(100)
    )
    price_gap = target_px - liquidation_px if position_size < 0 else liquidation_px - target_px
    if price_gap <= 0:
        return Decimal(0)
    # Add a 5% buffer for maintenance-margin and rounding differences; verify
    # the actual liquidation price after each signed update.
    return (abs(position_size) * price_gap * Decimal("1.05")).quantize(
        Decimal("0.000001"), rounding=ROUND_CEILING
    )


def try_add_isolated_margin(
    exchange: Any,
    info: Any,
    address: str,
    dex: str,
    coin: str,
    market: dict[str, Any],
    min_free_usd: Decimal,
    logger: logging.Logger,
    *,
    target_distance_pct: Decimal = Decimal("16"),
    minimum_position_usd: Decimal = Decimal(0),
) -> dict[str, Any]:
    """Add isolated margin only when it can preserve the caller's free-USDC reserve."""
    margin_mode = str(market.get("marginMode", "")).lower()
    if not ("isolated" in margin_mode or market.get("onlyIsolated")):
        return {"fixed": False, "reason": f"not_isolated:{market.get('marginMode', 'unknown')}"}

    metadata = read_with_retries(
        f"metaAndAssetCtxs dex={dex}",
        lambda: info.post("/info", {"type": "metaAndAssetCtxs", "dex": dex}),
    )
    meta, contexts = metadata
    market_index = next((i for i, item in enumerate(meta.get("universe", [])) if item.get("name") == coin), None)
    if market_index is None or market_index >= len(contexts):
        return {"fixed": False, "reason": "market_context_unavailable"}

    last_amount = Decimal(0)
    for attempt in range(1, 3):
        state = read_with_retries(f"clearinghouseState dex={dex}", lambda: info.user_state(address, dex=dex))
        position = position_for(state, coin)
        size = Decimal(str(position.get("szi", "0")))
        oracle = Decimal(str(contexts[market_index].get("oraclePx", "0")))
        distance = liquidation_distance_pct(position, size, oracle)
        if size == 0 or distance is None or distance >= target_distance_pct:
            if last_amount > 0 and distance is not None:
                verified_free = free_usdc(info, address, dex)
                logger.warning(
                    "ISOLATED_MARGIN_VERIFIED dex=%s coin=%s amount_added=$%s distance_after=%s free_after=$%s",
                    dex,
                    coin,
                    last_amount,
                    distance,
                    verified_free,
                )
            return {"fixed": True, "state": state, "size": size, "distance": distance, "amount": last_amount}
        position_usd = abs(size * oracle)
        if position_usd < minimum_position_usd:
            return {"fixed": False, "reason": f"below_tolerance:${position_usd}"}

        liquidation_px = Decimal(str(position.get("liquidationPx", "0")))
        required = isolated_margin_needed(size, liquidation_px, oracle, target_distance_pct)
        free = free_usdc(info, address, dex)
        available = max(Decimal(0), free - min_free_usd).quantize(Decimal("0.000001"), rounding=ROUND_FLOOR)
        if required <= 0 or required > available:
            return {
                "fixed": False,
                "reason": f"insufficient_free:required=${required} available=${available} free=${free} reserve=${min_free_usd}",
                "state": state,
                "size": size,
                "distance": distance,
                "free": free,
            }

        try:
            response = exchange.update_isolated_margin(float(required), coin)
        except Exception as exc:
            return {
                "fixed": False,
                "reason": f"update_failed:{type(exc).__name__}",
                "state": state,
                "size": size,
                "distance": distance,
                "free": free,
            }
        if not isinstance(response, dict) or response.get("status") != "ok":
            return {
                "fixed": False,
                "reason": f"update_rejected:{response.get('response', response.get('status')) if isinstance(response, dict) else type(response).__name__}",
                "state": state,
                "size": size,
                "distance": distance,
                "free": free,
            }

        last_amount += required
        logger.warning(
            "ISOLATED_MARGIN_ADDED dex=%s coin=%s attempt=%d amount=$%s distance_before=%s target_distance_pct=%s free_before=$%s reserve=$%s",
            dex,
            coin,
            attempt,
            required,
            distance,
            target_distance_pct,
            free,
            min_free_usd,
        )
        time.sleep(0.25)
        metadata = read_with_retries(
            f"metaAndAssetCtxs dex={dex}",
            lambda: info.post("/info", {"type": "metaAndAssetCtxs", "dex": dex}),
        )
        meta, contexts = metadata
        market_index = next((i for i, item in enumerate(meta.get("universe", [])) if item.get("name") == coin), None)
        if market_index is None or market_index >= len(contexts):
            break

    verified_state = read_with_retries(f"clearinghouseState dex={dex}", lambda: info.user_state(address, dex=dex))
    verified_position = position_for(verified_state, coin)
    verified_size = Decimal(str(verified_position.get("szi", "0")))
    verified_oracle = Decimal(str(contexts[market_index].get("oraclePx", "0")))
    verified_distance = liquidation_distance_pct(verified_position, verified_size, verified_oracle)
    if verified_distance is not None and verified_distance >= Decimal("15"):
        verified_free = free_usdc(info, address, dex)
        logger.warning(
            "ISOLATED_MARGIN_VERIFIED dex=%s coin=%s amount_added=$%s distance_after=%s free_after=$%s",
            dex,
            coin,
            last_amount,
            verified_distance,
            verified_free,
        )
        return {"fixed": True, "state": verified_state, "size": verified_size, "distance": verified_distance, "amount": last_amount}

    return {
        "fixed": False,
        "reason": f"distance_after_topup={verified_distance}",
        "state": verified_state,
        "size": verified_size,
        "distance": verified_distance,
        "amount": last_amount,
    }


def fills_in_range(info: Any, address: str, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
    """Query all fills in a window, subdividing windows at the API's 2,000-fill cap."""
    if end_ms - start_ms > MAX_FILLS_WINDOW_MS:
        fills: list[dict[str, Any]] = []
        chunk_start = start_ms
        while chunk_start <= end_ms:
            chunk_end = min(end_ms, chunk_start + MAX_FILLS_WINDOW_MS - 1)
            fills.extend(fills_in_range(info, address, chunk_start, chunk_end))
            chunk_start = chunk_end + 1
        return fills
    fills = read_with_retries(
        "userFillsByTime",
        lambda: info.user_fills_by_time(address, start_ms, end_ms, aggregate_by_time=True),
    )
    if not isinstance(fills, list):
        raise RuntimeError("userFillsByTime returned an unexpected payload")
    request_weight = 20 + math.ceil(len(fills) / 20)
    time.sleep(request_weight / 1200 * 60 + 0.25)
    if len(fills) < MAX_FILLS_PER_RESPONSE:
        return fills
    if end_ms - start_ms <= 1:
        raise RuntimeError("userFillsByTime reached its response cap for a 1 ms window")
    midpoint = (start_ms + end_ms) // 2
    return fills_in_range(info, address, start_ms, midpoint) + fills_in_range(info, address, midpoint + 1, end_ms)


def fees_in_range(info: Any, address: str, start_ms: int, end_ms: int) -> tuple[Decimal, int]:
    fills = fills_in_range(info, address, start_ms, end_ms)
    fees = Decimal(0)
    for fill in fills:
        token = str(fill.get("feeToken", "USDC"))
        if token != "USDC":
            raise RuntimeError(f"fee token {token} is not USD-denominated; cannot apply the USD fee budget safely")
        fees += Decimal(str(fill.get("fee", "0")))
    return fees, len(fills)
