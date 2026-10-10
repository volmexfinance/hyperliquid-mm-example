# Hummingbot example: quoting BVIV on Hyperliquid

> Example code only. Use at your own risk; see the [disclaimer](../README.md#disclaimer).

`scripts/bviv_simple_mm.py` is a minimal Hummingbot script that market-makes the BVIV perpetual (`mkts:BVIV`, Kinetiq's HIP-3 dex). Every 15 seconds it cancels its orders and places one post-only bid and one post-only ask a fixed spread around the BVIV index. The index is the oracle price Hyperliquid publishes for the market. Once the position reaches `max_position` contracts, the script quotes only the side that reduces it.

It shows how to wire Hummingbot to BVIV. It is not a strategy to leave running: it does not hedge, skew quotes for inventory, or notice a stale oracle.

## Requirements

- Hummingbot 2.17 or later. Its `hyperliquid_perpetual` connector supports HIP-3 markets and names them `<DEX>:<COIN>-USD`, so BVIV is `MKTS:BVIV-USD`.
- A Hyperliquid account with USDC. BVIV is isolated-margin only, up to 5x.

## Run it

1. Copy the files into your Hummingbot install:

   ```sh
   cp scripts/bviv_simple_mm.py           <hummingbot>/scripts/
   cp conf/scripts/conf_bviv_simple_mm.yml <hummingbot>/conf/scripts/
   ```

2. In the Hummingbot client, connect Hyperliquid. Choose `api_wallet` mode and use an API wallet created at https://app.hyperliquid.xyz/API, so the bot never holds your main key:

   ```
   connect hyperliquid_perpetual
   ```

3. Start the script, check it, and stop it. `stop` cancels the bot's open orders.

   ```
   start --script bviv_simple_mm.py --conf conf_bviv_simple_mm.yml
   status
   stop
   ```

With Docker, mount the two files into the official image (`hummingbot/hummingbot:latest`) at `/home/hummingbot/scripts/` and `/home/hummingbot/conf/scripts/`, then run the same commands in the client.

## Settings (`conf/scripts/conf_bviv_simple_mm.yml`)

| Setting | Default | Meaning |
|---|---|---|
| `exchange` | `hyperliquid_perpetual` | Connector; `hyperliquid_perpetual_testnet` for testnet |
| `trading_pair` | `MKTS:BVIV-USD` | Market |
| `order_amount` | `1` | Contracts per order. One contract is worth the index level (~$38); Hyperliquid's minimum order is $10 |
| `spread` | `0.003` | Distance from the index on each side (0.003 = 30 bps) |
| `order_refresh_time` | `15` | Seconds between cancel-and-replace cycles |
| `leverage` | `2` | Isolated leverage, set once at start (BVIV max 5) |
| `max_position` | `10` | Contracts; above this the script quotes only the reducing side |

## Costs to know before running on mainnet

- BVIV is not in Hyperliquid's HIP-3 growth mode. Makers pay a fee on every fill (0.030% before staking discounts on 2026-10-09). A spread narrower than about 2× that fee loses money even on round trips that never move.
- Hummingbot adds the Hummingbot Foundation builder code to mainnet orders. Its fee is 0 unless you approved that builder (then 1 bp).
- Longs have been paying most of the funding since launch. Check it before holding a long.

## Testnet

Kinetiq's testnet dex publishes the BVIV feed under `bart:AAPL`, so `conf/scripts/conf_bviv_simple_mm_testnet.yml` points at `hyperliquid_perpetual_testnet` / `BART:AAPL-USD`.

Stock Hummingbot 2.17 cannot run on Hyperliquid testnet today. Mainnet is not affected.

1. A testnet dex lists a market named `tndex:A B:C`. The connector splits names with `split(':')`, so building the market list fails and the connector never becomes ready. The fix is `split(':', 1)` in `_initialize_trading_pair_symbols_from_exchange_info`.
2. The connector requests market data and positions separately for every HIP-3 dex. Testnet has 268 dexes, so start-up gets rate-limited (HTTP 429). Mainnet has about 10.

The testnet run on 2026-10-09 used a copy of the connector patched for both. It hydrated only the `bart` dex, keeping asset IDs computed from the full dex list. The script itself was unchanged.

Result of the 10-minute run:
- The bot connected and set 2x isolated leverage.
- It replaced a 1-contract bid and ask at ±30 bps around the ~39.0 oracle every 15 seconds: 82 orders placed, 80 cancelled by the bot.
- Hyperliquid showed exactly the two post-only orders resting at any time.
- No fills and no rejected orders.
- One cancel came back "duplicate nonce" because two cancels went out in the same millisecond. The next cycle retried it.

Stopping the Docker container (SIGTERM) does not cancel orders in headless mode; two orders were left resting and had to be cancelled separately. Run `stop` in the client before shutting Hummingbot down.
