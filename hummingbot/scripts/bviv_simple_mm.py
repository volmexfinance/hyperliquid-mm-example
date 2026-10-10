"""
Minimal BVIV market maker for Hummingbot.

Quotes one bid and one ask around the BVIV index price (the oracle price that
Hyperliquid publishes for mkts:BVIV) and replaces them every
`order_refresh_time` seconds. Once the position reaches `max_position`
contracts it only quotes the side that reduces it.

This is a reference for wiring Hummingbot to BVIV, not a strategy to run
unattended: it does not hedge, skew prices for inventory, or detect a stale
oracle. Try it on testnet first (see hummingbot/README.md).
"""
import logging
import os
from decimal import Decimal
from typing import Dict, List, Optional

from pydantic import Field

from hummingbot.connector.connector_base import ConnectorBase
from hummingbot.core.data_type.common import MarketDict, OrderType, PositionMode
from hummingbot.core.event.events import OrderFilledEvent
from hummingbot.strategy.strategy_v2_base import StrategyV2Base, StrategyV2ConfigBase


class BVIVSimpleMMConfig(StrategyV2ConfigBase):
    script_file_name: str = os.path.basename(__file__)
    controllers_config: List[str] = []
    exchange: str = Field("hyperliquid_perpetual")
    # Hummingbot names HIP-3 markets "<DEX>:<COIN>-USD".
    trading_pair: str = Field("MKTS:BVIV-USD")
    # Contracts per order. One BVIV contract is worth the index level (~$38);
    # Hyperliquid rejects orders under $10.
    order_amount: Decimal = Field(Decimal("1"))
    # Distance from the index on each side: 0.003 = 30 bps.
    spread: Decimal = Field(Decimal("0.003"))
    order_refresh_time: int = Field(15)
    # BVIV allows up to 5x and isolated margin only.
    leverage: int = Field(2)
    max_position: Decimal = Field(Decimal("10"))

    def update_markets(self, markets: MarketDict) -> MarketDict:
        markets[self.exchange] = markets.get(self.exchange, set()) | {self.trading_pair}
        return markets


class BVIVSimpleMM(StrategyV2Base):
    def __init__(self, connectors: Dict[str, ConnectorBase], config: BVIVSimpleMMConfig):
        super().__init__(connectors, config)
        self.config = config
        self.next_refresh = 0

    @property
    def connector(self) -> ConnectorBase:
        return self.connectors[self.config.exchange]

    def apply_initial_setting(self):
        self.connector.set_position_mode(PositionMode.ONEWAY)
        self.connector.set_leverage(self.config.trading_pair, self.config.leverage)

    def index_price(self) -> Optional[Decimal]:
        try:
            price = self.connector.get_funding_info(self.config.trading_pair).index_price
        except KeyError:
            return None
        return price if price > 0 else None

    def position(self) -> Decimal:
        position = self.connector.account_positions.get(self.config.trading_pair)
        return position.amount if position else Decimal("0")

    def on_tick(self):
        if self.current_timestamp < self.next_refresh:
            return
        self.next_refresh = self.current_timestamp + self.config.order_refresh_time

        for order in self.get_active_orders(self.config.exchange):
            self.cancel(self.config.exchange, order.trading_pair, order.client_order_id)

        index = self.index_price()
        if index is None:
            self.logger().warning(f"No index price for {self.config.trading_pair} yet; not quoting.")
            return

        # LIMIT_MAKER is post-only (Hyperliquid "Alo"): it never takes liquidity.
        position = self.position()
        if position < self.config.max_position:
            self.buy(self.config.exchange, self.config.trading_pair, self.config.order_amount,
                     OrderType.LIMIT_MAKER, index * (1 - self.config.spread))
        if position > -self.config.max_position:
            self.sell(self.config.exchange, self.config.trading_pair, self.config.order_amount,
                      OrderType.LIMIT_MAKER, index * (1 + self.config.spread))

    def did_fill_order(self, event: OrderFilledEvent):
        msg = f"{event.trade_type.name} {event.amount} {event.trading_pair} at {event.price}"
        self.log_with_clock(logging.INFO, msg)
        self.notify_hb_app_with_timestamp(msg)

    def format_status(self) -> str:
        if not self.ready_to_trade:
            return "Market connectors are not ready."
        lines = [f"  {self.config.trading_pair}  index {self.index_price()}  position {self.position()}"]
        for order in self.get_active_orders(self.config.exchange):
            lines.append(f"  {'BUY ' if order.is_buy else 'SELL'} {order.quantity} @ {order.price}")
        return "\n".join(lines)
