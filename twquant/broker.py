"""券商抽象層。

設計原則：介面只認「我可以查什麼、我可以送什麼」，不認是哪一家券商。
PaperBroker 用它重播歷史資料，Shioaji adapter 用它接實單。
兩者共用同一份策略與同一份風控，所以策略邏輯不需要為了上線改一行。
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field


class BrokerError(RuntimeError):
    pass


class NotConnected(BrokerError):
    pass


@dataclass
class Account:
    cash: float
    equity: float
    holdings: float
    margin: float = 0.0

    @property
    def exposure(self) -> float:
        return self.holdings / self.equity if self.equity > 0 else 0.0


@dataclass
class Holding:
    ticker: str
    shares: int
    avg_price: float
    last_price: float = 0.0

    @property
    def market_value(self) -> float:
        return self.shares * self.last_price

    @property
    def unrealized(self) -> float:
        return (self.last_price - self.avg_price) * self.shares


@dataclass
class Order:
    order_id: str
    ticker: str
    side: str  # buy | sell
    shares: int
    status: str = "pending"  # pending | filled | rejected | cancelled
    price: float = 0.0
    filled_shares: int = 0
    filled_price: float = 0.0
    fee: float = 0.0
    tax: float = 0.0
    reason: str = ""
    message: str = ""
    ts: str = ""


@dataclass
class Quote:
    ticker: str
    price: float
    ts: str = ""
    bid: float = 0.0
    ask: float = 0.0


@dataclass
class Session:
    """一個交易時段的邊界資訊。日線模式一天一個。"""

    ts: str
    is_trading_day: bool = True
    note: str = ""


class Broker(abc.ABC):
    """券商介面。

    實作單位是「一次呼叫 = 一個明確動作」，不要在裡面藏重試或補單邏輯，
    那些屬於 runner 的職責，否則出錯時很難判斷是誰重複送了單。
    """

    name = "broker"

    @property
    @abc.abstractmethod
    def connected(self) -> bool: ...

    @abc.abstractmethod
    def connect(self) -> None: ...

    @abc.abstractmethod
    def disconnect(self) -> None: ...

    @abc.abstractmethod
    def account(self) -> Account: ...

    @abc.abstractmethod
    def holdings(self) -> dict[str, Holding]: ...

    @abc.abstractmethod
    def quote(self, ticker: str) -> Quote: ...

    @abc.abstractmethod
    def place_order(self, ticker: str, side: str, shares: int, reason: str = "") -> Order:
        """送出委託。回傳的 Order 可能是 pending（等開盤成交）或 rejected。"""

    @abc.abstractmethod
    def cancel(self, order_id: str) -> bool: ...

    @abc.abstractmethod
    def open_orders(self) -> list[Order]: ...

    def poll(self) -> list[Order]:
        """推進內部時鐘／撮合佇列，回傳這次有狀態變化的訂單。

        日線模式呼叫時機是「新的一天開始」，實單則是被動等待回報。
        """
        return []
