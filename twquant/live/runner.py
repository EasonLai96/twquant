"""實單／模擬單的主迴圈。

跟回測引擎的差別只有三件事，其餘刻意共用：
  1. 部位狀態向券商查，不自己記（自己記的版本一定會跟券商不同步）
  2. 每一輪都要經過風控閘門，而且攔阻要留紀錄
  3. 隨時可以中斷再啟動 —— 狀態寫進 SQLite，重啟後從券商實際部位接手

順序固定為：券商撮合 → 查部位 → 風控 → 策略 → 下單。
先撮合再問部位，才不會拿昨天的部位算今天的配置。
"""
from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..broker import Account, Broker, BrokerError, Holding
from ..engine import CostConfig, PortfolioBacktester
from ..loader import Bar
from ..strategy import MarketContext, PositionView, Strategy
from .risk import RiskGate, RiskLimits
from .state import State

WARMUP_BARS = 120


@dataclass
class RunReport:
    start: str = ""
    end: str = ""
    days: int = 0
    orders_placed: int = 0
    fills: int = 0
    rejected: int = 0
    blocked_days: int = 0
    days_in_market: int = 0
    block_reasons: list[str] = field(default_factory=list)
    start_equity: float = 0.0
    end_equity: float = 0.0
    exposure_sum: float = 0.0
    cost_sum: float = 0.0

    @property
    def total_return(self) -> float:
        return self.end_equity / self.start_equity - 1 if self.start_equity else 0.0

    @property
    def exposure(self) -> float:
        return self.exposure_sum / self.days if self.days else 0.0

    def summary(self) -> str:
        lines = [
            f"期間        {self.start} ~ {self.end}（實際跑 {self.days} 個交易日）",
            f"權益        {self.start_equity:,.0f} -> {self.end_equity:,.0f}"
            f"  ({self.total_return * 100:+.2f}%)",
            f"平均曝險    {self.exposure * 100:.2f}%（有持股 {self.days_in_market} 天）",
            f"下單/成交    {self.orders_placed} / {self.fills}    被拒 {self.rejected}",
            f"成本合計    {self.cost_sum:,.0f}",
            f"風控攔阻     {self.blocked_days} 天",
        ]
        for r in self.block_reasons[:5]:
            lines.append(f"  - {r}")
        return "\n".join(lines)


class LiveRunner:
    def __init__(
        self,
        broker: Broker,
        strategy: Strategy,
        bars: dict[str, list[Bar]],
        state_dir: str | Path,
        capital: float = 1_000_000.0,
        cost: CostConfig | None = None,
        limits: RiskLimits | None = None,
        adj_returns: dict[str, dict[str, float]] | None = None,
        verbose: bool = True,
        sleep: float = 0.0,
        warmup: int = WARMUP_BARS,
    ):
        self.broker = broker
        self.strategy = strategy
        self.bars = bars
        self.cost = cost or CostConfig()
        self.adj_returns = adj_returns or {}
        self.state = State(state_dir)
        self.risk = RiskGate(limits, state_dir)
        self.verbose = verbose
        self.sleep = sleep
        self.warmup = warmup

        # 這兩個只是為了沿用引擎已測試過的訂單規劃與成本公式，不參與成交
        self._planner = PortfolioBacktester(
            capital, self.cost, max_positions=(limits.max_positions if limits else 5)
        )
        self._idx = {t: {b.date: i for i, b in enumerate(bs)} for t, bs in bars.items()}
        self._by_date = {t: {b.date: b for b in bs} for t, bs in bars.items()}
        self._entry_dates: dict[str, str] = dict(self.state.get_meta("entry_dates", {}) or {})
        self._equity_prev: float | None = self.state.get_meta("equity_prev")

    # --- 主流程 ------------------------------------------------------------

    def run(self, start: str | None = None, end: str | None = None, max_days: int | None = None) -> RunReport:
        dates = self._dates(start, end)
        if not dates:
            raise BrokerError("沒有可跑的交易日")

        if not self.broker.connected:
            self.broker.connect()

        rep = RunReport(start=dates[0], end=dates[-1])
        rep.start_equity = self.broker.account().equity
        self._log("session_start", tickers=sorted(self.bars), start=dates[0], end=dates[-1])
        last = dates[0]

        for n, d in enumerate(dates):
            if max_days is not None and n >= max_days:
                break
            last = d
            rep.days += 1
            if self.risk.halted:
                self._log("halted", date=d)
                if self.verbose:
                    print(f"  {d} 偵測到 HALT，停止")
                break

            # 1) 讓券商撮合昨天的單
            filled = self.broker.advance(d) if hasattr(self.broker, "advance") else self.broker.poll()
            for o in filled:
                self._record_fill(o, d)
                if o.status == "filled":
                    rep.fills += 1
                else:
                    rep.rejected += 1

            # 2) 查部位與權益（一律以券商為準）
            account = self.broker.account()
            holdings = self.broker.holdings()
            held = {t: h.shares for t, h in holdings.items()}
            prices = self._prices(d, held)
            self.risk.start_day(account.equity)
            rep.exposure_sum += account.exposure
            if any(s > 0 for s in held.values()):
                rep.days_in_market += 1
            if hasattr(self.broker, "costs_total"):
                rep.cost_sum = self.broker.costs_total()

            # 3) 風控：hard 才停手，soft 只記錄並交由 filter_orders 擋新倉
            decision = self.risk.check(account, held, prices, self._equity_prev)
            if decision.advisories:
                for adv in decision.advisories:
                    self._log("risk_advisory", date=d, note=adv)
                if self.verbose and decision.advisories:
                    print(f"  {d} 風控提示：{'；'.join(decision.advisories[:2])}")
            if not decision.allow:
                rep.blocked_days += 1
                rep.block_reasons.extend(decision.reasons)
                self._log("risk_block", date=d, reasons=decision.reasons, equity=account.equity)
                if self.verbose:
                    print(f"  {d} 風控停止：{'；'.join(decision.reasons)}")
                self._snapshot(account, holdings, d, held)
                self._equity_prev = account.equity
                continue

            # 4) 策略
            ctx = self._context(d, account, holdings)
            resp = self.strategy.on_bar(ctx)

            # 5) 規劃訂單 → 風控再過一次 → 送出
            day_no = self._day_number(d)
            min_vol = {t: self._avg_volume(t, d) for t in self.bars}
            orders = self._planner.plan_orders(
                resp, held, {t: self._by_date[t][d] for t in self.bars if d in self._by_date[t]},
                account.equity, account.cash, min_vol, self.bars, self._idx, d, day_no,
            )
            orders, notes = self.risk.filter_orders(orders, account, held, prices)
            for nmsg in notes:
                self._log("risk_adjust", date=d, note=nmsg)

            for o in orders:
                placed = self.broker.place_order(o["ticker"], o["side"], o["shares"], o.get("reason", ""))
                rep.orders_placed += 1
                self._log("order", date=d, order=placed, planned=o)
                if placed.status == "rejected":
                    rep.rejected += 1
                    self._log("order_rejected", date=d, order=placed)
                if o["side"] == "buy" and placed.status != "rejected":
                    self._entry_dates.setdefault(o["ticker"], d)
                if o["side"] == "sell" and o["shares"] >= held.get(o["ticker"], 0):
                    self._entry_dates.pop(o["ticker"], None)

            self._snapshot(account, holdings, d, held)
            self._equity_prev = account.equity
            if self.verbose:
                npos = sum(1 for s in held.values() if s > 0)
                print(
                    f"  {d}  權益 {account.equity:,.0f}  現金 {account.cash:,.0f}  "
                    f"持股 {npos} 檔  下單 {len(orders)}"
                )
            if self.sleep:
                time.sleep(self.sleep)

        rep.end_equity = self.broker.account().equity
        rep.end = last
        if hasattr(self.broker, "costs_total"):
            rep.cost_sum = self.broker.costs_total()
        self._log("session_end", **{"return": rep.total_return, "days": rep.days})
        return rep

    # --- 輔助 --------------------------------------------------------------

    def _dates(self, start: str | None, end: str | None) -> list[str]:
        s: set[str] = set()
        for bs in self.bars.values():
            s.update(b.date for b in bs)
        ds = sorted(d for d in s if (start is None or d >= start) and (end is None or d <= end))
        return ds

    def _day_number(self, date: str) -> int:
        for bs in self.bars.values():
            i = self._idx.get(next(iter(self.bars)), {}).get(date)
            if i is not None:
                return i
            break
        return 0

    def _avg_volume(self, ticker: str, date: str, window: int = 20) -> float:
        bars = self.bars.get(ticker) or []
        i = self._idx.get(ticker, {}).get(date)
        if i is None or i < window:
            return 0.0
        w = bars[i - window : i]
        return sum(b.volume for b in w) / len(w) if w else 0.0

    def _prices(self, date: str, held: dict[str, int]) -> dict[str, float]:
        """當天所有有 bar 的標的收盤價。

        不能只給已持有的標的：風控要用這份價格來判斷「即將買進」的部位大小，
        只給持倉會讓新標的查無報價而被當成不可交易，結果就是永遠不會進場。
        """
        out: dict[str, float] = {}
        for t in self.bars:
            b = self._by_date.get(t, {}).get(date)
            if b is not None:
                out[t] = b.close
        return out

    def _context(self, date: str, account: Account, holdings: dict[str, Holding]) -> MarketContext:
        views: dict[str, PositionView] = {}
        holding_days: dict[str, int] = {}
        for t, h in holdings.items():
            if h.shares <= 0:
                continue
            views[t] = PositionView(t, h.shares, h.avg_price, self._entry_dates.get(t, ""), h.last_price, h.unrealized)
            holding_days[t] = self._idx.get(t, {}).get(date, 0)
        # 訊號要 61 根以上才成立（slow=60），從 start 當天才開始餵會全部無訊號。
        # 往前多給 warmup 根當冷卻期，起點由 start 決定、資料由更早就開始。
        #
        # 注意 idx 的語意：策略拿到的 window 長度是 min(i+1, warmup+1)，所以當天在
        # window 裡的位置就是 len(window)-1，不是全量資料的 i。
        window = {}
        for t, bs in self.bars.items():
            i = self._idx.get(t, {}).get(date)
            if i is None:
                continue
            window[t] = bs[max(0, i - self.warmup) : i + 1]
        widx = max((len(w) for w in window.values()), default=0) - 1
        return MarketContext(date, max(0, widx), window, views, account.equity, account.cash, holding_days)

    def _record_fill(self, order, date: str) -> None:
        self._log("fill", date=date, order=order)
        self.state.record_orders([order])
        if order.status == "filled" and order.side == "sell" and order.filled_shares:
            pass

    def _snapshot(self, account: Account, holdings: dict[str, Holding], date: str, held: dict[str, int]) -> None:
        self.state.save_positions(holdings, self._entry_dates)
        self.state.set_meta("entry_dates", self._entry_dates)
        self.state.set_meta("equity_prev", account.equity)
        self.state.set_meta("last_date", date)
        self._log("equity", date=date, equity=account.equity, cash=account.cash, exposure=account.exposure)

    def _log(self, event: str, **fields) -> None:
        self.state.log(event, **fields)
