# Trading BVIV on Hyperliquid: examples

Market-making examples for HIP-3 perps on Hyperliquid, centred on BVIV (`mkts:BVIV`, Volmex's Bitcoin implied volatility index). There are two ways in:

| Folder | Built on | Start here if |
|---|---|---|
| [`python-sdk/`](python-sdk/README.md) | the official [hyperliquid-python-sdk](https://github.com/hyperliquid-dex/hyperliquid-python-sdk) | You want a standalone bot with a ladder of quotes, inventory and liquidation guards. `mm.py` runs on testnet or mainnet (`--network mainnet`). |
| [`hummingbot/`](hummingbot/README.md) | [Hummingbot](https://hummingbot.org) 2.17+ | You already run Hummingbot. `bviv_simple_mm.py` is a minimal one-bid, one-ask script. |

Both quote around the BVIV index, which is the oracle price Hyperliquid publishes for the market. Both use post-only orders, so they never take liquidity.

On testnet, Kinetiq's `bart:AAPL` market carries the BVIV feed. Try it there before using real funds.

These are examples only; read the [disclaimer](#disclaimer) before using them. They are reference implementations, not strategies. They don't hedge, and BVIV's maker fee (0.030% before staking discounts on 2026-10-09) eats any quote tighter than about 3 bps from the index.

## Disclaimer

This repository is example code, provided for illustration only. It is not financial, investment, legal or tax advice, and it is not a recommendation to trade any product.

The software is provided "as is", without warranty of any kind. Trading perpetual futures is risky, and you can lose all the funds you deposit. Automated trading can lose money quickly through bugs, market moves, fees, funding payments, or exchange or oracle outages.

You are responsible for reviewing and testing the code, keeping your keys safe, following the laws and exchange terms that apply to you, and every trade you make. Use it at your own risk. To the fullest extent the law allows, Volmex Labs and the contributors to this repository are not liable for any loss or damage that comes from using this example.
