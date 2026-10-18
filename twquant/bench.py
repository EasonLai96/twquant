"""躺著 vs 動手：同一段時間、同一組成本，把幾種做法攤開比。

存在的理由很單純 —— 大部分人問「怎麼用程式賺錢」的時候，真正該問的是
「我有沒有比買進持有更好」。沒有這個對照，任何策略的回測數字都是斷章取義。

四條線：
  buyhold   期初一筆買進 0050 抱著不動
  dca       每月固定金額買 0050（定期定額），現金沒投入前不計息
  trend_ma  雙均線進出
  breakout  動能突破

全部扣同樣的手續費、證交稅、滑價、整股限制。
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from .engine import CostConfig, PortfolioBacktester, _ann_vol, _cagr, _max_dd, _sharpe
from .loader import Bar
from .strategies import Breakout20, TrendMA


@dataclass
class BenchResult:
    name: str
    total_return: float
    cagr: float
    max_drawdown: float
    vol: float
    sharpe: float
    trades: int
    cost: float
    final: float

    def row(self) -> str:
        return (
            f"  {self.name:<10}{self.total_return * 100:>9.1f}%{self.cagr * 100:>8.2f}%"
            f"{self.max_drawdown * 100:>9.1f}%{self.vol * 100:>8.1f}%{self.sharpe:>8.2f}"
            f"{self.trades:>8}{self.cost:>11,.0f}"
        )


HEADER = (
    f"  {'做法':<10}{'總報酬':>10}{'CAGR':>9}{'最大回撤':>9}{'年化波動':>8}{'Sharpe':>8}"
    f"{'交易':>8}{'成本':>11}"
)


def _metrics(name: str, equity: list[float], dates: list[str], trades: int, cost: float) -> BenchResult:
    rets = [equity[i] / equity[i - 1] - 1 for i in range(1, len(equity)) if equity[i - 1] > 0]
    span = max(1, (dt.date.fromisoformat(dates[-1]) - dt.date.fromisoformat(dates[0])).days)
    return BenchResult(
        name=name,
        total_return=equity[-1] / equity[0] - 1,
        cagr=_cagr(equity[0], equity[-1], span),
        max_drawdown=_max_dd(equity)[0],
        vol=_ann_vol(rets),
        sharpe=_sharpe(rets),
        trades=trades,
        cost=cost,
        final=equity[-1],
    )


def buyhold(
    bars: list[Bar],
    capital: float,
    cost: CostConfig,
    adj: dict[str, float] | None = None,
) -> BenchResult:
    """期初一筆買進、抱到最後一天。

    adj 是 load_adj_returns 的輸出。有分割時原始價格會腰斬（0050 在 2025-06 做 4:1），
    直接乘股數會嚴重低估報酬。
    """
    dates = [b.date for b in bars]
    px0 = bars[0].open * (1 + cost.slippage_bps / 10_000)
    lots = int(capital / (px0 * cost.lot))
    if lots < 1:
        raise ValueError("本金不足一張")
    shares = lots * cost.lot
    fee = max(px0 * shares * cost.fee_rate * cost.fee_discount, cost.fee_min)
    cash = capital - px0 * shares - fee

    equity: list[float] = []
    value = shares * px0
    for i, b in enumerate(bars):
        if i > 0:
            r = (adj or {}).get(b.date)
            value = value * (1 + r) if r is not None else shares * b.close
        equity.append(cash + value)

    # 期末不模擬賣出：這個對照的題目是「抱到最後值多少」，多扣一筆稅費只會讓
    # 基準變難看，反而失去比較意義。買進成本已經算在 cash 裡了。
    return _metrics("buyhold", equity, dates, 1, fee)


def dca(
    bars: list[Bar],
    capital: float,
    cost: CostConfig,
    monthly: float | None = None,
    adj: dict[str, float] | None = None,
) -> BenchResult:
    """定期定額，每月第一天投入固定金額。

    買進單位是「股」不是「張」：每月一萬塊買不到一張 0050，用整張去模擬會變成
    「永遠買不起」，那就不是定期定額了。台股 ETF 本來就可以零股交易。
    buyhold 仍用整張，因為大額單筆是整張買。
    """
    first = dt.date.fromisoformat(bars[0].date)
    last = dt.date.fromisoformat(bars[-1].date)
    months = max(1, (last.year - first.year) * 12 + last.month - first.month + 1)
    monthly = monthly if monthly is not None else capital / months
    dates = [b.date for b in bars]
    cash = capital
    shares = 0.0
    spent_fee = 0.0
    bought: set[tuple[int, int]] = set()
    equity: list[float] = []
    value = 0.0

    for i, b in enumerate(bars):
        d = dt.date.fromisoformat(b.date)
        key = (d.year, d.month)
        if key not in bought:
            bought.add(key)
            px = b.open * (1 + cost.slippage_bps / 10_000)
            budget = min(monthly, cash)
            if budget > px:
                sh = budget / px
                fee = max(budget * cost.fee_rate * cost.fee_discount, cost.fee_min)
                if budget + fee <= cash:
                    cash -= budget + fee
                    shares += sh
                    value += sh * px
                    spent_fee += fee
                    equity.append(cash + value)
                    continue
        if i > 0 and value > 0:
            r = (adj or {}).get(b.date)
            value = value * (1 + r) if r is not None else shares * b.close
        equity.append(cash + (value if value > 0 else shares * b.close))

    # 期末同樣不模擬賣出，理由跟 buyhold 一樣
    return _metrics("dca", equity, dates, len(bought), spent_fee)


def run_bench(
    data: dict[str, list[Bar]],
    index_ticker: str = "0050",
    capital: float = 1_000_000.0,
    cost: CostConfig | None = None,
    start: str | None = None,
    end: str | None = None,
    adj_returns: dict[str, dict[str, float]] | None = None,
    max_positions: int = 5,
) -> list[BenchResult]:
    cost = cost or CostConfig()
    adj = adj_returns or {}
    out: list[BenchResult] = []

    idx = data.get(index_ticker)
    if idx:
        idx = [b for b in idx if (start is None or b.date >= start) and (end is None or b.date <= end)]
        if len(idx) > 1:
            # adj 一定要傳進去，否則 0050 的 4:1 分割會被當成腰斬
            out.append(buyhold(idx, capital, cost, adj.get(index_ticker)))
            out.append(dca(idx, capital, cost, adj=adj.get(index_ticker)))

    trade = {t: v for t, v in data.items() if t != index_ticker}
    if trade:
        for strat, name in (
            (TrendMA(fast=20, slow=60, max_positions=max_positions), "trend_ma"),
            (Breakout20(lookback=20, vol_mult=1.5, max_positions=max_positions), "breakout"),
        ):
            bt = PortfolioBacktester(capital, cost, max_positions=max_positions)
            res = bt.run(
                trade, strat, start=start, end=end, benchmark=None, adj_returns=adj
            )
            m = res.metrics
            out.append(
                BenchResult(
                    name=name,
                    total_return=m["total_return"],
                    cagr=m["cagr"],
                    max_drawdown=m["max_drawdown"],
                    vol=m["vol"],
                    sharpe=m["sharpe"],
                    trades=m["trades"],
                    cost=res.costs.total,
                    final=m["final"],
                )
            )
    return out
