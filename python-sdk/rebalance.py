#!/usr/bin/env python3
"""Keep the testnet maker and trader wallets funded from shared Spot USDC."""

from __future__ import annotations

import argparse
import json
import logging
import signal
import threading
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any, Callable

from dotenv import dotenv_values
from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants


TESTNET_URL = constants.TESTNET_API_URL
# The .env in the directory that holds this repo.
DEFAULT_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"
DEFAULT_LOG_FILE = Path("logs/rebalance.log")
DEFAULT_STATE_FILE = Path("logs/rebalance_state.json")
DEX = "bart"
COIN = "bart:AAPL"
USDC_TOKEN_NAME = "USDC"
TRANSFER_ACTION = "sendAsset"
TRANSFER_SOURCE_DEX = "spot"
TRANSFER_DESTINATION_DEX = "spot"
MAKER_ADDRESS = "0x67757f723E067acCd93D48fCa07f44F3E3105534"
TRADER_ADDRESS = "0xcbA40Fc37A76835fA5B0F6bcDb4b067b8Fa405d1"
USDC_QUANTUM = Decimal("0.00000001")
LOG = logging.getLogger("hyperliquid_rebalance")
STOP = threading.Event()


@dataclass(frozen=True)
class WalletConfig:
    name: str
    address: str
    key_names: tuple[str, ...]
    minimum: Decimal
    target: Decimal


@dataclass(frozen=True)
class WalletBalance:
    name: str
    address: str
    total: Decimal
    held: Decimal
    spot_free: Decimal
    transferable_free: Decimal
    active_margin_free: Decimal
    withdrawable: Decimal
    account_value: Decimal
    abstraction: str
    leverage: Decimal


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Testnet-only shared USDC rebalancer for two unified accounts")
    parser.add_argument("--dry-run", action="store_true", help="log the decision but never sign or send a transfer")
    parser.add_argument("--once", action="store_true", help="read balances and make at most one decision, then exit")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE, help=f"key file (default: {DEFAULT_ENV_FILE})")
    parser.add_argument("--interval-seconds", type=float, default=30, help="balance-check interval")
    parser.add_argument("--trader-min-free-usd", type=Decimal, default=Decimal("1500"), help="trader wallet minimum free USDC")
    parser.add_argument("--mm-min-free-usd", "--market-maker-min-free-usd", dest="market_maker_min_free_usd", type=Decimal, default=Decimal("2500"), help="market-maker wallet minimum free USDC")
    parser.add_argument("--trader-target-free-usd", type=Decimal, help="trader target free USDC (defaults to 2x its minimum)")
    parser.add_argument("--mm-target-free-usd", "--market-maker-target-free-usd", dest="market_maker_target_free_usd", type=Decimal, help="maker target free USDC (defaults to 2x its minimum)")
    parser.add_argument("--min-free-usd", type=Decimal, help="override both per-wallet minimums")
    parser.add_argument("--target-free-usd", type=Decimal, help="override both per-wallet targets")
    parser.add_argument("--max-transfer-usd", type=Decimal, default=Decimal("2000"), help="maximum amount in one transfer")
    parser.add_argument("--cooldown-seconds", type=float, default=120, help="minimum delay between confirmed/ambiguous transfers")
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE_FILE, help=f"persist transfer cooldown state (default: {DEFAULT_STATE_FILE})")
    args = parser.parse_args()

    if args.interval_seconds <= 0:
        parser.error("--interval-seconds must be positive")
    if args.cooldown_seconds < 0:
        parser.error("--cooldown-seconds must not be negative")
    if args.max_transfer_usd <= 0:
        parser.error("--max-transfer-usd must be positive")

    if args.min_free_usd is not None:
        if args.min_free_usd <= 0:
            parser.error("--min-free-usd must be positive")
        args.trader_min_free_usd = args.min_free_usd
        args.market_maker_min_free_usd = args.min_free_usd
    if args.trader_min_free_usd <= 0 or args.market_maker_min_free_usd <= 0:
        parser.error("per-wallet minimum free balances must be positive")

    if args.target_free_usd is not None:
        if args.target_free_usd <= 0:
            parser.error("--target-free-usd must be positive")
        args.trader_target_free_usd = args.target_free_usd
        args.market_maker_target_free_usd = args.target_free_usd
    if args.trader_target_free_usd is None:
        args.trader_target_free_usd = args.trader_min_free_usd * 2
    if args.market_maker_target_free_usd is None:
        args.market_maker_target_free_usd = args.market_maker_min_free_usd * 2
    if args.trader_target_free_usd < args.trader_min_free_usd:
        parser.error("--trader-target-free-usd must be at least --trader-min-free-usd")
    if args.market_maker_target_free_usd < args.market_maker_min_free_usd:
        parser.error("--market-maker-target-free-usd must be at least --market-maker-min-free-usd")
    return args


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
            LOG.warning("%s failed (%s); retry in %.1fs (%d/%d)", label, type(exc).__name__, delay, attempt, attempts)
            STOP.wait(delay)
            delay = min(delay * 2, 15.0)
    raise RuntimeError(f"{label} failed after {attempts} attempts ({type(last_error).__name__})") from last_error


def read_balance(info: Info, config: WalletConfig) -> WalletBalance:
    spot = read_with_retries(f"spotClearinghouseState wallet={config.name}", lambda: info.spot_user_state(config.address))
    perp = read_with_retries(f"clearinghouseState wallet={config.name} dex={DEX}", lambda: info.user_state(config.address, dex=DEX))
    active = read_with_retries(
        f"activeAssetData wallet={config.name} coin={COIN}",
        lambda: info.post("/info", {"type": "activeAssetData", "user": config.address, "coin": COIN}),
    )
    abstraction = read_with_retries(f"userAbstraction wallet={config.name}", lambda: info.query_user_abstraction_state(config.address))

    usdc = next((entry for entry in spot.get("balances", []) if entry.get("coin") == USDC_TOKEN_NAME), {})
    total = Decimal(str(usdc.get("total", "0")))
    held = Decimal(str(usdc.get("hold", "0")))
    spot_free = max(Decimal(0), total - held)
    available = active.get("availableToTrade")
    if not isinstance(available, (list, tuple)) or len(available) != 2:
        raise RuntimeError(f"Could not verify free Bart margin for {config.name}; refusing any transfer")
    active_margin_free = max(Decimal(0), min(Decimal(str(available[0])), Decimal(str(available[1]))))
    # For a unified account, spot total/hold is the shared collateral ledger;
    # activeAssetData further limits it to collateral not already committed to perp margin.
    transferable_free = min(spot_free, active_margin_free)
    summary = perp.get("marginSummary", {})
    account_value = Decimal(str(summary.get("accountValue", "0")))
    withdrawable = Decimal(str(perp.get("withdrawable", "0")))
    lev_obj = active.get("leverage", {})
    leverage = Decimal(str(lev_obj.get("value", "0"))) if isinstance(lev_obj, dict) else Decimal(0)
    if abstraction not in ("unifiedAccount", "portfolioMargin"):
        raise RuntimeError(f"{config.name} reports {abstraction!r}, not a unified account; refusing to transfer")
    return WalletBalance(
        config.name,
        config.address,
        total,
        held,
        spot_free,
        transferable_free,
        active_margin_free,
        withdrawable,
        account_value,
        str(abstraction),
        leverage,
    )


def log_balance(balance: WalletBalance, config: WalletConfig) -> None:
    LOG.info(
        "BALANCE wallet=%s abstraction=%s total=$%.8f held=$%.8f spot_free=$%.8f "
        "active_margin_free=$%.8f transferable_free=$%.8f withdrawable=$%.8f account_value=$%.8f "
        "leverage=%sx min_free=$%.2f target_free=$%.2f",
        balance.name,
        balance.abstraction,
        balance.total,
        balance.held,
        balance.spot_free,
        balance.active_margin_free,
        balance.transferable_free,
        balance.withdrawable,
        balance.account_value,
        balance.leverage,
        config.minimum,
        config.target,
    )


def read_last_transfer_time(path: Path) -> float | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return float(value["last_transfer_time"])
    except FileNotFoundError:
        return None
    except Exception as exc:
        # A corrupt cooldown file must not accidentally permit back-to-back transfers.
        LOG.error("COOLDOWN_STATE_INVALID path=%s error=%s; enforcing a fresh cooldown", path, type(exc).__name__)
        return time.time()


def write_last_transfer_time(path: Path, timestamp: float, amount: Decimal, source: str, destination: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps({"last_transfer_time": timestamp, "amount": str(amount), "from": source, "to": destination}) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def spot_usdc_token(info: Info) -> str:
    meta = read_with_retries("spotMeta", info.spot_meta)
    token = next((item for item in meta.get("tokens", []) if item.get("name") == USDC_TOKEN_NAME), None)
    if not token or token.get("tokenId") is None:
        raise RuntimeError("Testnet spotMeta did not resolve a USDC token ID; refusing to sign a transfer")
    return f"{USDC_TOKEN_NAME}:{token['tokenId']}"


def validate_signers(env_file: Path) -> dict[str, Any]:
    resolved = env_file.expanduser().resolve()
    if not resolved.is_file():
        raise RuntimeError(f"The requested env file does not exist: {resolved}")
    try:
        values = dotenv_values(resolved)
    except OSError as exc:
        raise RuntimeError(f"Could not read the requested env file ({type(exc).__name__})") from None
    key_values = {
        "market_maker": next((values.get(key) for key in ("TEST_HYPERLIQUID_PRIVATE_KEY", "TEST_HYPERLIQUID_KEY") if values.get(key)), None),
        "trader": values.get("TEST_HYPERLIQUID_TRADER_PRIVATE_KEY"),
    }
    expected = {"market_maker": MAKER_ADDRESS, "trader": TRADER_ADDRESS}
    wallets: dict[str, Any] = {}
    for name, key in key_values.items():
        if not key or not key.strip():
            raise RuntimeError(f"Required {name} testnet key is absent from {resolved}")
        try:
            wallet = Account.from_key(key.strip())
        except Exception as exc:
            raise RuntimeError(f"Could not parse the {name} testnet key ({type(exc).__name__})") from None
        if wallet.address.lower() != expected[name].lower():
            raise RuntimeError(f"The {name} key does not match the configured wallet address; refusing to transfer")
        wallets[name] = wallet
    return wallets


def is_rate_limit(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None) == 429 or "429" in str(exc).lower() or "rate limit" in str(exc).lower()


def send_unified_usdc(exchange: Exchange, destination: str, amount: Decimal, token: str) -> Any:
    # sendAsset supports user-to-user transfers from Spot to Spot. spotSend is
    # disabled when the source account uses unified-account abstraction.
    delay = 1.0
    for attempt in range(1, 5):
        try:
            return exchange.send_asset(
                destination,
                TRANSFER_SOURCE_DEX,
                TRANSFER_DESTINATION_DEX,
                token,
                float(amount),
            )
        except Exception as exc:
            if not is_rate_limit(exc) or attempt == 4:
                raise
            LOG.warning("RATE_LIMIT status=429 retry=%d backoff_seconds=%.1f", attempt, delay)
            STOP.wait(delay)
            delay = min(delay * 2, 8.0)
    raise RuntimeError("sendAsset failed after retries")


def status_is_ok(response: Any) -> bool:
    return isinstance(response, dict) and response.get("status") == "ok"


def amount_for_decision(
    recipient: WalletConfig,
    recipient_balance: WalletBalance,
    donor: WalletConfig,
    donor_balance: WalletBalance,
    max_transfer: Decimal,
) -> Decimal:
    # Free is Spot total minus hold. activeAssetData separately caps the send
    # to collateral not already committed to perp margin.
    donor_spare = min(
        max(Decimal(0), donor_balance.spot_free - donor.minimum),
        donor_balance.active_margin_free,
    )
    recipient_gap = max(Decimal(0), recipient.target - recipient_balance.spot_free)
    return min(donor_spare, recipient_gap, max_transfer).quantize(USDC_QUANTUM, rounding=ROUND_DOWN)


def confirm_balances(
    info: Info,
    recipient: WalletConfig,
    donor: WalletConfig,
    amount: Decimal,
    before_recipient: WalletBalance,
    before_donor: WalletBalance,
) -> tuple[WalletBalance, WalletBalance, bool]:
    tolerance = max(Decimal("0.02"), amount * Decimal("0.005"))
    deadline = time.monotonic() + 30
    latest_recipient = before_recipient
    latest_donor = before_donor
    while time.monotonic() <= deadline and not STOP.is_set():
        latest_donor = read_balance(info, donor)
        latest_recipient = read_balance(info, recipient)
        donor_delta = before_donor.total - latest_donor.total
        recipient_delta = latest_recipient.total - before_recipient.total
        LOG.info(
            "TRANSFER_VERIFY amount=$%.8f donor_delta=$%.8f recipient_delta=$%.8f tolerance=$%.8f",
            amount,
            donor_delta,
            recipient_delta,
            tolerance,
        )
        if donor_delta >= amount - tolerance and recipient_delta >= amount - tolerance:
            return latest_recipient, latest_donor, True
        STOP.wait(1.0)
    return latest_recipient, latest_donor, False


def check_once(
    args: argparse.Namespace,
    info: Info,
    wallets: dict[str, Any] | None,
    configs: dict[str, WalletConfig],
    token: str | None,
    last_transfer: float | None,
) -> float | None:
    maker_config = configs["market_maker"]
    trader_config = configs["trader"]
    balances = {
        "market_maker": read_balance(info, maker_config),
        "trader": read_balance(info, trader_config),
    }
    for name in ("market_maker", "trader"):
        log_balance(balances[name], configs[name])

    low = [name for name in ("market_maker", "trader") if balances[name].spot_free < configs[name].minimum]
    if not low:
        LOG.info("SKIP reason=both_wallets_above_minimum")
        return last_transfer
    if len(low) == 2:
        LOG.critical(
            "WARNING both wallets low, top up from the faucet maker_free=$%.2f maker_min=$%.2f "
            "trader_free=$%.2f trader_min=$%.2f",
            balances["market_maker"].spot_free,
            maker_config.minimum,
            balances["trader"].spot_free,
            trader_config.minimum,
        )
        return last_transfer

    recipient_name = low[0]
    donor_name = "trader" if recipient_name == "market_maker" else "market_maker"
    recipient = configs[recipient_name]
    donor = configs[donor_name]
    recipient_balance = balances[recipient_name]
    donor_balance = balances[donor_name]
    LOG.warning(
        "REBALANCE_TRIGGER recipient=%s free=$%.8f min=$%.2f target=$%.2f donor=%s free=$%.8f min=$%.2f",
        recipient.name,
        recipient_balance.spot_free,
        recipient.minimum,
        recipient.target,
        donor.name,
        donor_balance.spot_free,
        donor.minimum,
    )

    if donor_balance.spot_free < donor.minimum:
        LOG.warning(
            "SKIP reason=donor_below_own_minimum donor=%s transferable_free=$%.8f donor_min=$%.2f recipient=%s",
            donor.name,
            donor_balance.spot_free,
            donor.minimum,
            recipient.name,
        )
        return last_transfer

    if last_transfer is not None:
        remaining = args.cooldown_seconds - (time.time() - last_transfer)
        if remaining > 0:
            LOG.info("SKIP reason=cooldown seconds_remaining=%.1f", remaining)
            return last_transfer

    amount = amount_for_decision(recipient, recipient_balance, donor, donor_balance, args.max_transfer_usd)
    if amount <= 0:
        LOG.warning(
            "SKIP reason=no_donor_spare recipient_gap=$%.8f donor_spare_after_margin=$%.8f active_margin_free=$%.8f",
            max(Decimal(0), recipient.target - recipient_balance.spot_free),
            max(Decimal(0), donor_balance.spot_free - donor.minimum),
            donor_balance.active_margin_free,
        )
        return last_transfer

    LOG.warning(
        "TRANSFER_PLAN mode=%s action=%s source_dex=%s destination_dex=%s from=%s to=%s amount=$%.8f token=%s donor_free_before=$%.8f recipient_free_before=$%.8f",
        "DRY_RUN" if args.dry_run else "LIVE",
        TRANSFER_ACTION,
        TRANSFER_SOURCE_DEX,
        TRANSFER_DESTINATION_DEX,
        donor.name,
        recipient.name,
        amount,
        token or f"{USDC_TOKEN_NAME}:<resolved-at-runtime>",
        donor_balance.spot_free,
        recipient_balance.spot_free,
    )
    if args.dry_run:
        LOG.info("DRY_RUN no transfer was signed or submitted")
        return last_transfer
    if wallets is None or token is None:
        raise RuntimeError("Live transfer requires both validated signers and the resolved USDC token ID")

    try:
        exchange = Exchange(wallets[donor_name], base_url=TESTNET_URL, perp_dexs=[DEX], timeout=10)
        response = send_unified_usdc(exchange, recipient.address, amount, token)
    except Exception as exc:
        LOG.error("TRANSFER_SUBMIT_ERROR from=%s to=%s amount=$%.8f error=%s", donor.name, recipient.name, amount, type(exc).__name__)
        # A non-429 timeout may be ambiguous; wait out the cooldown and reconcile before any later attempt.
        if not is_rate_limit(exc):
            timestamp = time.time()
            write_last_transfer_time(args.state_file, timestamp, amount, donor.name, recipient.name)
            return timestamp
        return last_transfer
    if not status_is_ok(response):
        response_text = json.dumps(response, default=str)
        LOG.error("TRANSFER_REJECT action=%s from=%s to=%s amount=$%.8f response=%s", TRANSFER_ACTION, donor.name, recipient.name, amount, response_text)
        if "action disabled when unified account is active" in response_text.lower():
            LOG.error("TRANSFER_REJECT_DETERMINISTIC action=%s no_cooldown=true", TRANSFER_ACTION)
            return last_transfer
        timestamp = time.time()
        write_last_transfer_time(args.state_file, timestamp, amount, donor.name, recipient.name)
        return timestamp

    timestamp = time.time()
    write_last_transfer_time(args.state_file, timestamp, amount, donor.name, recipient.name)
    LOG.info("TRANSFER_ACCEPTED action=%s from=%s to=%s amount=$%.8f", TRANSFER_ACTION, donor.name, recipient.name, amount)
    after_recipient, after_donor, confirmed = confirm_balances(info, recipient, donor, amount, recipient_balance, donor_balance)
    log_balance(after_donor, donor)
    log_balance(after_recipient, recipient)
    if confirmed:
        LOG.info(
            "TRANSFER_CONFIRMED from=%s to=%s amount=$%.8f donor_total_before=$%.8f donor_total_after=$%.8f "
            "donor_free_before=$%.8f donor_free_after=$%.8f recipient_total_before=$%.8f recipient_total_after=$%.8f "
            "recipient_free_before=$%.8f recipient_free_after=$%.8f",
            donor.name,
            recipient.name,
            amount,
            donor_balance.total,
            after_donor.total,
            donor_balance.spot_free,
            after_donor.spot_free,
            recipient_balance.total,
            after_recipient.total,
            recipient_balance.spot_free,
            after_recipient.spot_free,
        )
    else:
        LOG.error("TRANSFER_BALANCE_NOT_CONFIRMED from=%s to=%s amount=$%.8f; no automatic retry will be sent", donor.name, recipient.name, amount)
    return timestamp


def handle_stop(signum: int, _frame: Any) -> None:
    LOG.info("STOP signal=%s received", signum)
    STOP.set()


def build_configs(args: argparse.Namespace) -> dict[str, WalletConfig]:
    return {
        "market_maker": WalletConfig(
            "market_maker",
            MAKER_ADDRESS,
            ("TEST_HYPERLIQUID_PRIVATE_KEY", "TEST_HYPERLIQUID_KEY"),
            args.market_maker_min_free_usd,
            args.market_maker_target_free_usd,
        ),
        "trader": WalletConfig(
            "trader",
            TRADER_ADDRESS,
            ("TEST_HYPERLIQUID_TRADER_PRIVATE_KEY",),
            args.trader_min_free_usd,
            args.trader_target_free_usd,
        ),
    }


def main() -> int:
    logging.Formatter.converter = time.gmtime
    logging.basicConfig(level=logging.INFO, format="%(asctime)sZ %(levelname)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S")
    args = parse_args()
    args.state_file = args.state_file.expanduser().resolve()
    configs = build_configs(args)
    info = Info(base_url=TESTNET_URL, skip_ws=True, perp_dexs=[DEX], timeout=10)
    token = spot_usdc_token(info)
    wallets = None if args.dry_run else validate_signers(args.env_file)
    if not args.dry_run:
        LOG.info("ACCOUNT mode=LIVE endpoint=%s market_maker=%s trader=%s collateral=shared_spot_USDC transfer_action=%s source_dex=%s destination_dex=%s token=%s", TESTNET_URL, MAKER_ADDRESS, TRADER_ADDRESS, TRANSFER_ACTION, TRANSFER_SOURCE_DEX, TRANSFER_DESTINATION_DEX, token)
    else:
        LOG.info("ACCOUNT mode=DRY_RUN endpoint=%s market_maker=%s trader=%s collateral=shared_spot_USDC transfer_action=%s source_dex=%s destination_dex=%s token=%s", TESTNET_URL, MAKER_ADDRESS, TRADER_ADDRESS, TRANSFER_ACTION, TRANSFER_SOURCE_DEX, TRANSFER_DESTINATION_DEX, token)

    signal.signal(signal.SIGINT, handle_stop)
    signal.signal(signal.SIGTERM, handle_stop)
    signal.signal(signal.SIGHUP, handle_stop)
    last_transfer = read_last_transfer_time(args.state_file)
    while not STOP.is_set():
        try:
            last_transfer = check_once(args, info, wallets, configs, token, last_transfer)
        except Exception as exc:
            LOG.error("CHECK_ERROR error=%s; continuing", type(exc).__name__)
        if args.once or STOP.is_set():
            break
        STOP.wait(args.interval_seconds)
    LOG.info("STOP rebalancer loop complete")
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
