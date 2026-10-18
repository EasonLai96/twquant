"""策略介面。

約定（很重要，決定回測可不可信）：
  1. on_bar 在「當日收盤後」被呼叫，只能看到 <= 今日的 bar。
  2. 回傳的 OrderIntent 在「隔日開盤」成交，所以策略內部不該假設成交價。
  3. 要賣出時必須同時把它放進 allocation，否則部位會一直被 engine 持有。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from .loader import Bar


@dataclass(slots=True)
class OrderIntent:
    ticker: str
    action: str  # buy | sell
    reason: str = ""


@dataclass(slots=True)
class StrategyResponse:
    orders: list[OrderIntent] = field(default_factory=list)
    # ticker -> 目標資金權重（0~1）。沒列到的 ticker 會被視為目標 0，等於清倉。
    allocation: dict[str, float] = field(default_factory=dict)


@dataclass(slots=True)
class PositionView:
    ticker: str
    shares: int
    cost: float
    entry_date: str
    last_close: float
    unrealized: float


@dataclass(slots=True)
class MarketContext:
    date: str
    # 注意：idx 是這根 bar 在「全宇宙交易日聯集」中的位置，不等於單一 ticker 的 index。
    # 要拿某檔的 index 請用 strategies._idx(bars, ctx.date)。
    idx: int
    bars: dict[str, list[Bar]]
    positions: dict[str, PositionView]
    equity: float
    cash: float
    holding_days: dict[str, int]
    cache: dict = field(default_factory=dict)
    _series: dict = field(default_factory=dict)

    def series(self, ticker: str, key: str, make) -> list:
        """Per-ticker 指標快取。策略每個交易日都會被呼叫，指標不能每次重算。"""
        slot = self._series.setdefault(ticker, {})
        if key not in slot:
            slot[key] = make(self.bars[ticker])
        return slot[key]


class Strategy(Protocol):
    name: str

    def params(self) -> dict: ...

    def on_bar(self, ctx: MarketContext) -> StrategyResponse: ...
