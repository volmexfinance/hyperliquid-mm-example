# Hyperliquid bots on the official Python SDK

> Example code only. Use at your own risk; see the [disclaimer](../README.md#disclaimer).

These scripts trade HIP-3 perps with [hyperliquid-python-sdk](https://github.com/hyperliquid-dex/hyperliquid-python-sdk). No Hummingbot is needed. For the Hummingbot version, see [../hummingbot](../hummingbot/README.md).

| Script | Networks | What it does |
|---|---|---|
| `mm.py` | testnet (default), mainnet | Post-only ladder market maker around the oracle (index) price. |
| `trader.py` | testnet only | Sends randomized orders against `mm.py` from a second wallet, to test the maker. |
| `rebalance.py` | testnet only | Moves USDC between the two test wallets. |
| `status.py` | testnet only | Read-only health summary of the two test wallets. |

`trader.py` and `rebalance.py` stay testnet-only on purpose. They run counter-flow against our own maker from a wallet we also control. On mainnet that is wash trading.

## Setup

From this directory, create and use the local virtual environment. It needs Python 3.10 or later (`python-dotenv` 1.2 does not install on 3.9):

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Keys are read into process memory and are never logged or written by these scripts.

- `mm.py` reads its key from `--env-file`, or else from the first of `python-sdk/.env`, the repo's `.env`, and the `.env` in the repo's parent directory that has it.
  - On testnet it reads `TEST_HYPERLIQUID_PRIVATE_KEY`, falling back to `TEST_HYPERLIQUID_KEY`.
  - On mainnet it reads `HYPERLIQUID_PRIVATE_KEY`, falling back to `PROD_HYPERLIQUID_MM_PRIVATE_KEY`. It never reads the `TEST_` names, so a testnet key can never sign a mainnet order.
- `trader.py` and `rebalance.py` read the `.env` in the repo's parent directory. `trader.py` uses `TEST_HYPERLIQUID_TRADER_PRIVATE_KEY`.

For mainnet, use an API (agent) wallet from https://app.hyperliquid.xyz/API as `HYPERLIQUID_PRIVATE_KEY`, and pass your main address with `--account-address`. The bot then never holds the key that can withdraw funds.

## Market maker: `mm.py`

`--network` picks the network and the default market:

| | `--network testnet` (default) | `--network mainnet` |
|---|---|---|
| Endpoint | `api.hyperliquid-testnet.xyz` | `api.hyperliquid.xyz` |
| Market | `bart:AAPL` (testnet BVIV feed) | `mkts:BVIV` |
| Key | `TEST_HYPERLIQUID_PRIVATE_KEY` | `HYPERLIQUID_PRIVATE_KEY` or `PROD_HYPERLIQUID_MM_PRIVATE_KEY` |
| Ladder | 2–25 bps, $2,000 per side | 10–50 bps, $500 per side |
| Reserve / max position | $2,500 / $25,000 | $100 / $2,500 |

The mainnet ladder starts at 10 bps because BVIV is not in HIP-3 growth mode. Makers paid 0.030% (3 bps) per fill before staking discounts on 2026-10-09, so a 2 bps quote loses money on every fill. Every default can be overridden with its flag.

Collateral depends on the account mode Hyperliquid reports for the account:
- Unified and portfolio-margin accounts budget from Spot USDC.
- Standard (`default`) accounts budget from the USDC already in the market's DEX. Move USDC to the `mkts` DEX first.
- Any other mode is refused at startup.

On mainnet the `mkts:BVIV` perp often trades above the index. When the oracle-referenced asks would cross the best bid, they are skipped and the remaining asks carry the side's notional. Pass `--ref mid` to quote around the order book instead.

On testnet the market maker defaults to `--dex bart --coin bart:AAPL`. It takes over existing orders for that account and market by modifying them in place, then adds missing levels in a single batch and cancels only surplus levels. Normal re-quotes do not clear the book first. It re-quotes every 60 seconds or when its selected reference moves more than 5 bps. Shutdown on Ctrl-C, SIGTERM, or SIGHUP cancels its open orders for that market. It queries position only on the selected DEX, so the `bart` configuration leaves any position on `xyz` alone.

Preview the ladder using public market data and the selected account's position guard, without signing or submitting orders:

```sh
.venv/bin/python mm.py --dry-run                     # testnet bart:AAPL
.venv/bin/python mm.py --network mainnet --dry-run   # mainnet mkts:BVIV
```

Run continuously:

```sh
.venv/bin/python mm.py                                                  # testnet
.venv/bin/python mm.py --network mainnet --account-address 0xYOURMAIN    # mainnet, real funds
```

Testnet defaults: 20 ALO levels per side, 2 to 25 bps from the oracle index price, and a requested $2,000 resting notional per side (about $100 per level). The effective ladder is reduced automatically to fit current free collateral while retaining at least `--min-free-usd` USDC ($2,500 on testnet). Margin is re-read before each quote update; the fit budgets collateral for both resting sides using live `maxLeverage`. If a proposed ALO level would cross the current opposite best price, that level is skipped. Since free collateral changes with positions and fills, the effective notional may be below the request.

In addition to the 60-second timer and 5 bps oracle-move trigger, the maker checks open-order notional on every poll and refreshes the ladder if either side falls below `--refill-pct` (70% by default) of its guarded target. It re-reads `maxLeverage`, `marginMode`, and `onlyIsolated` from current DEX metadata and acknowledges leverage with `update_leverage` at startup and after a margin-tier rejection. Re-quotes modify existing orders in place. If liquidation distance drops below 10%, it first tries to add isolated margin to put the liquidation price at least 15% away while retaining `--min-free-usd`. If that cannot be done, it cuts the risk-increasing side to 10% at wider offsets; positions below `$500` are ignored by this maker guard. It logs a CRITICAL line if margin cannot be added or the book remains one-sided or empty for over 60 seconds following an order reject.

Hyperliquid enforces a `$10` minimum order notional for this market. When the fallback liquidation guard reduces a side to 10% of the target (the testnet `$2,000`), each of its 20 levels is rounded up to the smallest valid lot, so that side rests at about `$200` (slightly above from lot rounding). The refill check compares against this same liquidation-adjusted target to avoid repeatedly rebuilding a correctly reduced side.

To pin the best bid and ask, pass both `--fixed-bid PX --fixed-ask PX`. The first level uses each fixed price; the remaining levels step down for bids and up for asks, using `--start-bps` and `--end-bps`. Fixed-price mode re-quotes on the timer only; oracle movement does not trigger a re-quote. The bot still reads `oraclePx` to enforce the empirical 10% rejection band: bids are clamped to at most 110% of oracle and asks to at least 90% of oracle, with a warning. ALO levels that would cross the current book are skipped.

Where two defaults are shown, the first is testnet and the second mainnet.

| Flag | Default | Meaning |
|---|---:|---|
| `--network testnet\|mainnet` | `testnet` | Network; sets the endpoint, key name, default market and default ladder. |
| `--dex NAME` | `bart` / `mkts` | HIP-3 DEX name. |
| `--coin NAME` | `bart:AAPL` / `mkts:BVIV` | Fully qualified perp name; its prefix must match `--dex`. |
| `--ref oracle\|mid` | `oracle` | Ladder and move-trigger reference. |
| `--dry-run` | off | Print the ladder without signing or sending exchange actions. |
| `--env-file PATH` | searched | Read the key from this file instead of searching `python-sdk/.env`, the repo's `.env`, then its parent's `.env`. |
| `--account-address ADDRESS` | signer address | Trading account when the key is an authorized agent key. |
| `--count N` | `20` | Levels per side. |
| `--start-bps N` | `2` / `10` | Nearest level offset from the reference. |
| `--end-bps N` | `25` / `50` | Furthest level offset from the reference. |
| `--fixed-bid PX` | unset | Fixed best bid; requires `--fixed-ask`. |
| `--fixed-ask PX` | unset | Fixed best ask; requires `--fixed-bid`. |
| `--notional-usd N` | `2000` / `500` | Target resting notional per side before a margin cap. |
| `--min-free-usd N` | `2500` / `100` | Minimum free USDC to retain after placing both sides; lower fitting ladder sizes automatically. |
| `--requote-seconds N` | `60` | Timer re-quote interval. |
| `--move-bps N` | `5` | Re-quote when the reference moves more than this from the ladder's reference. |
| `--poll-seconds N` | `2` | Reference and position polling interval; accepts 1 to 2 seconds. |
| `--refill-pct N` | `70` | Re-quote when either side falls below this percent of its target. |
| `--max-position-usd N` | `25000` / `2500` | Maximum absolute position notional; at the cap, the side that would add risk is stopped. |
| `--liquidation-skew-min-position-usd N` | `500` | Ignore the maker liquidation-margin/skew guard below this position notional. |
| `--duration-seconds N` | `0` | Optional bounded run; zero runs until signalled. |

Each bot order has a client order ID starting with `0x484c4d4d` (`HLMM`) for identification. At live startup, the script takes over existing orders for the selected account and market and removes only surplus levels after replacing the kept orders. It resolves the DEX/market indexes, checks USDC collateral and available trade capacity, rounds price to the perp five-significant-figure and decimal-place limits, and rounds size to `szDecimals`. It never moves funds. See [API_NOTES.md](API_NOTES.md) for the Bart API details and sources.

## Periodic trader: `bart:AAPL`

`trader.py` uses the separate `TEST_HYPERLIQUID_TRADER_PRIVATE_KEY` wallet. It first checks the combined projected daily fees for both wallets, estimated from the trailing `--fee-window-hours` window; if the fee guard is paused, it sends no risk-increasing order. Otherwise it sends log-normal notional clips with a $40 median, clipped to `$10–$400`, on Poisson arrivals averaging the configured `--interval-seconds` over time. About 10% of arrivals start a 2–5-order burst with 1–8-second gaps. Volume regimes switch every 5–20 minutes between quiet (`0.3x`), normal (`1x`), and busy (`2.5x`) rates; their long-run weighted mean is `1x`. A directional streak carries the previous side forward with 65% persistence, while flatten cycles and position limits keep inventory in check. About 20% of orders use a human contract size (1, 2, 5, or 10 AAPL) when the margin and notional caps permit it. After a random 3 to 12 orders, it sends a reduce-only IOC order to flatten the whole position. If liquidation distance drops below 10%, it first tries to add isolated margin to move the liquidation price at least 15% away while retaining `--min-free-usd`; it flattens reduce-only if the margin top-up is unavailable. The IOC limit is set 1% from the current best ask for buys or best bid for sells, so any unfilled remainder is cancelled. A flatten uses the available executable side directly and falls back to an oracle-priced IOC attempt if the needed book side is empty.

```sh
.venv/bin/python trader.py --dry-run --seed 7
.venv/bin/python trader.py --dry-run --simulate-minutes 1440 --seed 7
.venv/bin/python trader.py --once
.venv/bin/python trader.py --interval-seconds 30 --min-notional-usd 10 --max-notional-usd 400 \
  --flatten-every-min 3 --flatten-every-max 12 --max-position-usd 15000
```

The live process checks and logs Bart USDC collateral, active buy/sell capacity, spot USDC, market leverage/margin mode, position, and liquidation distance. It re-reads leverage metadata at each trade check and calls `update_leverage` at startup or after a margin-tier reject, retrying that order once. If the L2 book is one-sided, a normal trade waits one second and retries once before skipping; a reducing flatten is not blocked by that check. On a unified account, it computes a margin-supported position cap from shared USDC and the current market leverage, then reduces a risk-increasing clip to available headroom. A combined fee guard checks both wallets' trailing `--fee-window-hours` of `userFillsByTime` fees (default 2 hours) every 10 minutes and scales the observed fees to a projected daily rate. Above the `$250` daily budget it stretches the interval in proportion to that rate; above `$375` it pauses risk-increasing trades until the rate falls back under `$250`. Liquidation safety checks continue while paused, and a reduce-only flatten remains allowed. `FEE_BUDGET` logs include the projected daily rate, budget action, and estimated runway. `--once` submits one trade and exits; `--dry-run` only previews the next trade.

| Flag | Default | Meaning |
|---|---:|---|
| `--interval-seconds N` | `30` | Mean order-arrival interval before volume-regime scaling and fee-budget stretching. |
| `--min-notional-usd N` | `10` | Lower clip for the log-normal size distribution. |
| `--max-notional-usd N` | `400` | Upper clip for the log-normal size distribution, capped further by available margin and position headroom. |
| `--simulate-minutes N` | unset | With `--dry-run`, simulate sizes, flow regimes, flatten turnover, and projected fees without loading a key or submitting orders. |
| `--seed N` | unset | Reproduce a dry-run sample or simulated stream. |
| `--flatten-every-min N` | `3` | Minimum random trade count before a reduce-only flatten. |
| `--flatten-every-max N` | `12` | Maximum random trade count before a reduce-only flatten. |
| `--max-position-usd N` | `15000` | Configured absolute position limit; the margin-supported limit may be lower. |
| `--min-free-usd N` | `1500` | Free Spot USDC reserve that isolated-margin safety top-ups must preserve. |
| `--slippage-percent N` | `1` | IOC limit distance from the touch. |
| `--fee-window-hours N` | `2` | Trailing fill-fee window, scaled to a projected daily rate by the fee guard. |
| `--daily-fee-budget-usd N` | `250` | Combined projected daily fee budget for both wallets. |
| `--fee-check-seconds N` | `600` | Fee-budget refresh interval. |
| `--fee-pause-multiplier N` | `1.5` | Pause threshold as a multiple of the daily fee budget. |
| `--env-file PATH` | `../../.env` | Read `TEST_HYPERLIQUID_TRADER_PRIVATE_KEY`. |
| `--account-address ADDRESS` | signer address | Trading account when the key is an authorized agent key. |
| `--dry-run` / `--once` | off | Preview only / submit one randomized order and exit. |

## Account health: `status.py`

`status.py` is read-only and uses public testnet data. It prints both wallets' Spot USDC totals, holds and free balances; positions and liquidation distance; current Bart AAPL L2 level counts and notional per side; per-wallet and combined fees over a trailing two-hour window; and the projected daily fee rate and runway. It accepts `--fee-window-hours` to match a different trader window.

```sh
.venv/bin/python status.py
.venv/bin/python status.py --fee-window-hours 4
```

## Shared USDC rebalancer: `rebalance.py`

This testnet-only process checks both unified wallets' Spot USDC total and hold every 30 seconds. It defines free USDC as `total - hold`; perps' `withdrawable` field is logged for reference but is not used as free collateral. Before sending, it also caps the transfer by the donor's available Bart AAPL margin, so held/margined funds are not sent. If either wallet falls below its minimum, it transfers enough to target twice that wallet's minimum, limited by donor headroom, target gap, and `$2,000` per transfer. It waits 120 seconds between transfers. If both wallets are below their minima, it logs a warning and waits for a faucet top-up.

Defaults are `$2,500` minimum / `$5,000` target for the maker and `$1,500` minimum / `$3,000` target for the trader. These corrected thresholds mean the previously supplied 21:21 UTC figures (maker free `$5,218`, trader free `$4,203`) do not call for a transfer; `--dry-run --once` always uses a fresh live snapshot and explains any decision.

In live mode, the script reads both private keys from the `.env` in the repo's parent directory, verifies their derived addresses, and signs only an eligible transfer. It dynamically resolves the Spot USDC token ID. Cross-wallet Spot collateral uses the SDK `send_asset` method, which submits Hyperliquid's `sendAsset` action from `spot` to `spot` on testnet. `spotSend` is disabled for unified accounts. See the [exchange endpoint docs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/exchange-endpoint) and [SDK implementation](https://github.com/hyperliquid-dex/hyperliquid-python-sdk/blob/master/hyperliquid/exchange.py).

Preview the current decision without loading or signing keys, then run a single live decision only when a wallet is below its minimum:

```sh
.venv/bin/python rebalance.py --dry-run --once
.venv/bin/python rebalance.py --once
```

Run continuously with the documented defaults or override thresholds:

```sh
.venv/bin/python rebalance.py --interval-seconds 30 \
  --mm-min-free-usd 2500 --trader-min-free-usd 1500 \
  --max-transfer-usd 2000 --cooldown-seconds 120
```

| Flag | Default | Meaning |
|---|---:|---|
| `--dry-run` | off | Log the plan; never sign or submit a transfer. |
| `--once` | off | Read once and make at most one transfer decision. |
| `--env-file PATH` | `../../.env` | Read and validate both testnet keys for live mode. |
| `--interval-seconds N` | `30` | Balance-check interval. |
| `--mm-min-free-usd N` | `2500` | Maker minimum Spot free balance. |
| `--trader-min-free-usd N` | `1500` | Trader minimum Spot free balance. |
| `--mm-target-free-usd N` | `2x maker minimum` | Maker target after top-up. |
| `--trader-target-free-usd N` | `2x trader minimum` | Trader target after top-up. |
| `--max-transfer-usd N` | `2000` | Maximum sent in one transfer. |
| `--cooldown-seconds N` | `120` | Minimum time between transfers. |

Logs and cooldown state are written to `logs/rebalance.log` and `logs/rebalance_state.json`.

## Detached runs

`restart_loop.sh` restarts a bot after a crash with exponential backoff and writes each restart to its log. These are the detached testnet commands used for the default market maker and trader configurations; run them from `python-sdk/`:

```sh
mkdir -p logs
tmux new -d -s hl-mm -c "$PWD" './restart_loop.sh mm.py logs/mm.log --ref oracle'
tmux new -d -s hl-trader -c "$PWD" './restart_loop.sh trader.py logs/trader.log'
tmux new -d -s hl-rebalance -c "$PWD" './restart_loop.sh rebalance.py logs/rebalance.log'
```

Stop with `tmux kill-session -t hl-mm`, `tmux kill-session -t hl-trader`, or `tmux kill-session -t hl-rebalance`. The restart wrapper forwards termination to the child and stops its loop. `mm.py` handles SIGTERM by cancelling all open orders for the configured account and market, then verifies that none remain. `trader.py` has no resting orders because it uses IOC. `rebalance.py` stops its polling loop and does not have resting orders.
