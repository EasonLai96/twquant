"""PaperBroker：用歷史日 K 重播的模擬券商。

存在的理由有兩個：
  1. 上實單前先把整條迴圈跑通（下單、狀態、風控、對帳、斷線重啟）
  2. 它跟回測引擎吃同一份資料時應該產出逐筆相同的交易 —— 做不到就是其中一個有 bug

成交邏輯刻意跟 engine.PortfolioBacktester 對齊（同樣的成本公式、同樣用前一根
收盤判斷漲跌停、同樣在結構性斷點日放行），差異只在不做配置最佳化：
PaperBroker 只負責「收到單、照規則成交」，部位大小由策略決定。
"""
from __future__ import annotations

import itertools
import uuid
from dataclasses import dataclass

from .broker import Account, Broker, BrokerError, Holding, Order, Quote
from .engine import CostConfig, is_rebase_date
from .loader import Bar


@dataclass
class Fill:
    ts: str
    order_id: str
    ticker: str
    side: str
    shares: int
    price: float
    fee: float
    tax: float
    reason: str

    @property
    def cash_delta(self) -> float:
        gross = self.price * self.shares
        return -(gross + self.fee) if self.side == "buy" else gross - self.fee - self.tax


class PaperBroker(Broker):
    name = "paper"

    def __init__(
        self,
        bars: dict[str, list[Bar]],
        capital: float = 1_000_000.0,
        cost: CostConfig | None = None,
        adj_returns: dict[str, dict[str, float]] | None = None,
    ):
        self.cost = cost or CostConfig()
        self.bars = bars
        self.adj_returns = adj_returns or {}
        self._by_date = {t: {b.date: b for b in bs} for t, bs in bars.items()}
        self._idx = {t: {b.date: i for i, b in enumerate(bs)} for t, bs in bars.items()}

        self._cash = capital
        self._capital = capital
        self._shares: dict[str, int] = {}
        self._cost_basis: dict[str, float] = {}
        self._pending: list[Order] = []
        self._counter = itertools.count(1)
        self._date: str | None = None
        self._fills: list[Fill] = []
        self._rejected: list[Order] = []

    # --- 連線 --------------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._date is not None

    def connect(self) -> None:
        if not self.bars:
            raise BrokerError("PaperBroker 需要至少一檔的日 K 才能運作")

    def disconnect(self) -> None:
        self._date = None

    # --- 時間推進 ----------------------------------------------------------

    def advance(self, date: str) -> list[Order]:
        """進入新的一天：先在今日開盤價撮合昨天排隊的單，再更新報價。"""
        self._date = date
        filled: list[Order] = []
        queue, self._pending = self._pending, []
        for o in queue:
            self._try_fill(o, date)
            filled.append(o)
        return filled

    def _try_fill(self, o: Order, date: str) -> None:
        bars = self.bars.get(o.ticker) or []
        i = self._idx.get(o.ticker, {}).get(date)
        if i is None:
            o.status = "rejected"
            o.message = "no-bar"
            self._rejected.append(o)
            return
        bar = bars[i]
        prev = bars[i - 1] if i > 0 else None
        if prev is None:
            o.status = "rejected"
            o.message = "no-reference"
            self._rejected.append(o)
            return

        why = self._block_reason(bar, prev, o.side, o.ticker, date)
        if why:
            o.status = "rejected"
            o.message = why
            self._rejected.append(o)
            return

        px = self._buy_price(bar.open) if o.side == "buy" else self._sell_price(bar.open)
        shares = o.shares
        if o.side == "buy":
            gross = px * shares
            fee = self._fee(gross)
            if gross + fee > self._cash:
                # 現金不夠就縮到買得起的整張數量，而不是整筆丟掉
                affordable = int(self._cash / (px * self.cost.lot)) * self.cost.lot
                if affordable < self.cost.lot:
                    o.status = "rejected"
                    o.message = "insufficient-cash"
                    self._rejected.append(o)
                    return
                shares = min(shares, affordable)
                gross = px * shares
                fee = self._fee(gross)
            self._cash -= gross + fee
            self._shares[o.ticker] = self._shares.get(o.ticker, 0) + shares
            self._cost_basis[o.ticker] = self._cost_basis.get(o.ticker, 0.0) + gross + fee
            tax = 0.0
        else:
            held = self._shares.get(o.ticker, 0)
            if held <= 0:
                o.status = "rejected"
                o.message = "no-position"
                self._rejected.append(o)
                return
            shares = min(shares, held)
            gross = px * shares
            fee = self._fee(gross)
            tax = gross * self.cost.tax_sell
            self._cash += gross - fee - tax
            basis = self._cost_basis.get(o.ticker, 0.0)
            self._cost_basis[o.ticker] = basis * (1 - shares / held)
            self._shares[o.ticker] = held - shares
            if self._shares[o.ticker] <= 0:
                self._shares.pop(o.ticker)
                self._cost_basis.pop(o.ticker, None)

        o.status = "filled"
        o.filled_shares = shares
        o.filled_price = px
        o.price = px
        o.fee = fee
        o.tax = tax
        o.ts = date
        if o.side == "sell":
            self._rebase_shares_if_needed(o.ticker, date)
        self._fills.append(Fill(date, o.order_id, o.ticker, o.side, shares, px, fee, tax, o.reason))

    def _rebase_shares_if_needed(self, ticker: str, date: str) -> None:
        """成交後才換算股數，避免與成交當下的股數混淆（跟引擎的順序一致）。"""
        bars = self.bars.get(ticker) or []
        i = self._idx.get(ticker, {}).get(date)
        if i is None or i == 0:
            return
        prev = bars[i - 1]
        bar = bars[i]
        if not is_rebase_date(self.adj_returns, ticker, date, prev.close, bar.close):
            return
        held = self._shares.get(ticker, 0)
        if held <= 0 or bar.close <= 0:
            return
        k = prev.close / bar.close
        if k > 0 and abs(k - 1.0) > 1e-9:
            self._shares[ticker] = int(round(held * k))

    def apply_rebases(self, date: str) -> None:
        """收盤後對所有持倉做一次重新定價換算（引擎是在評價迴圈裡做）。"""
        for t in list(self._shares):
            self._rebase_shares_if_needed(t, date)

    def _block_reason(self, bar: Bar, prev: Bar, side: str, ticker: str, date: str) -> str:
        if bar.volume <= 0:
            return "no-volume"
        if prev.close <= 0 or bar.open <= 0:
            return "bad-reference"
        if is_rebase_date(self.adj_returns, ticker, date, prev.close, bar.close):
            return ""
        gap = bar.open / prev.close - 1
        pinned = (bar.high - bar.low) / bar.open <= 0.002 if bar.open > 0 else True
        if side == "buy":
            if gap >= self.cost.limit_move and bar.open >= bar.high * (1 - self.cost.pin_tol) and pinned:
                return "limit-up"
        else:
            if gap <= -self.cost.limit_move and bar.open <= bar.low * (1 + self.cost.pin_tol) and pinned:
                return "limit-down"
        return ""

    # --- 費用 --------------------------------------------------------------

    def _fee(self, gross: float) -> float:
        return max(gross * self.cost.fee_rate * self.cost.fee_discount, self.cost.fee_min)

    def _buy_price(self, px: float) -> float:
        return px * (1 + self.cost.slippage_bps / 10_000)

    def _sell_price(self, px: float) -> float:
        return px * (1 - self.cost.slippage_bps / 10_000)

    # --- 查詢 --------------------------------------------------------------

    def quote(self, ticker: str) -> Quote:
        if self._date is None:
            return Quote(ticker, 0.0)
        b = self._by_date.get(ticker, {}).get(self._date)
        return Quote(ticker, b.close if b else 0.0, self._date)

    def holdings(self) -> dict[str, Holding]:
        out: dict[str, Holding] = {}
        for t, sh in self._shares.items():
            if sh <= 0:
                continue
            q = self.quote(t)
            out[t] = Holding(t, sh, self._cost_basis.get(t, 0.0) / sh, q.price or 0.0)
        return out

    def account(self) -> Account:
        hold = self.holdings()
        mv = sum(h.market_value for h in hold.values())
        equity = self._cash + mv
        return Account(cash=self._cash, equity=equity, holdings=mv)

    def place_order(self, ticker: str, side: str, shares: int, reason: str = "") -> Order:
        o = Order(
            order_id=f"P{uuid.uuid4().hex[:10]}",
            ticker=ticker,
            side=side,
            shares=int(shares),
            reason=reason,
            ts=self._date or "",
        )
        if shares <= 0:
            o.status = "rejected"
            o.message = "zero-shares"
            self._rejected.append(o)
            return o
        if ticker not in self.bars:
            o.status = "rejected"
            o.message = "unknown-ticker"
            self._rejected.append(o)
            return o
        self._pending.append(o)
        return o

    def cancel(self, order_id: str) -> bool:
        for i, o in enumerate(self._pending):
            if o.order_id == order_id:
                o.status = "cancelled"
                self._pending.pop(i)
                return True
        return False

    def open_orders(self) -> list[Order]:
        return list(self._pending)

    # --- 稽核 --------------------------------------------------------------

    @property
    def fills(self) -> list[Fill]:
        return list(self._fills)

    @property
    def rejected(self) -> list[Order]:
        return list(self._rejected)

    def equity_curve(self) -> list[tuple[str, float]]:
        return [(f.ts, self.account().equity) for f in self._fills]

    def costs_total(self) -> float:
        return sum(f.fee + f.tax for f in self._fills)
