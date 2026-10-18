"""分割／減資的檔案級測試。

用真實的 store + load_adj_returns 產生校正資料（不是手工造對照表），
這樣測到的就是線上那條路徑 —— 包含結構性斷點偵測與股數換算。
"""
from __future__ import annotations

import datetime as dt
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from twquant.engine import CostConfig, PortfolioBacktester  # noqa: E402
from twquant.loader import Bar, load_adj_returns  # noqa: E402
from twquant.store import Store  # noqa: E402
from twquant.strategy import OrderIntent, StrategyResponse  # noqa: E402


def mk_bars(prices, start="2024-01-01", vol=5_000_000):
    base = dt.date.fromisoformat(start)
    return [
        Bar((base + dt.timedelta(days=i)).isoformat(), p, p * 1.002, p * 0.998, p, vol)
        for i, p in enumerate(prices)
    ]


def adj_from_bars(bars, ticker="ASSET"):
    """走真正的 loader，而不是手工造對照表。"""
    with tempfile.TemporaryDirectory() as d:
        st = Store(Path(d) / "t.sqlite")
        st.upsert_bars(
            ticker,
            [
                {
                    "date": b.date,
                    "open": b.open,
                    "max": b.high,
                    "min": b.low,
                    "close": b.close,
                    "Trading_Volume": b.volume,
                }
                for b in bars
            ],
        )
        out = load_adj_returns(st, ticker, "2000-01-01", "2100-01-01")
        st.close()
    return {ticker: out}


class Hold:
    def __init__(self, enter_idx, exit_idx, weight=0.4):
        self.enter_idx = enter_idx
        self.exit_idx = exit_idx
        self.weight = weight
        self.name = "hold"

    def params(self):
        return {"enter_idx": self.enter_idx, "exit_idx": self.exit_idx}

    def on_bar(self, ctx):
        if ctx.idx >= self.exit_idx:
            return StrategyResponse(orders=[OrderIntent("ASSET", "sell", "exit")], allocation={})
        if ctx.idx >= self.enter_idx:
            return StrategyResponse(allocation={"ASSET": self.weight})
        return StrategyResponse()


class SplitShareConversion(unittest.TestCase):
    SPLIT_IDX = 60

    def _setup(self, ratio: float = 4.0, extra: int = 0):
        prices = [100.0] * self.SPLIT_IDX + [100.0 / ratio] * (60 + extra)
        bars = mk_bars(prices)
        return bars, adj_from_bars(bars)

    def test_shares_are_multiplied_and_wealth_is_preserved(self):
        bars, adj = self._setup(4.0)
        bt = PortfolioBacktester(1_000_000, CostConfig(), max_positions=1)
        res = bt.run({"ASSET": bars}, Hold(20, 100), benchmark=None, adj_returns=adj)

        snaps = res.snapshots
        i_before = self.SPLIT_IDX - 1
        i_after = self.SPLIT_IDX
        before = snaps[i_before].holdings
        after = snaps[i_after].holdings

        self.assertGreater(before, 0, "分割前應該要有部位")
        self.assertAlmostEqual(
            after / before,
            1.0,
            delta=0.02,
            msg=f"4:1 分割當天財富不該變動：{before:,.0f} -> {after:,.0f}",
        )
        self.assertLess(
            abs(snaps[i_after].ret),
            0.02,
            f"分割當天的日報酬應該接近 0，實際 {snaps[i_after].ret * 100:.2f}%",
        )

        trade = res.trades[0]
        self.assertGreater(trade.shares, 1000 * 4, f"股數應該變 4 倍以上，實際 {trade.shares}")

    def test_pnl_matches_equity_across_split(self):
        bars, adj = self._setup(4.0)
        bt = PortfolioBacktester(1_000_000, CostConfig(), max_positions=1)
        res = bt.run({"ASSET": bars}, Hold(20, 100), benchmark=None, adj_returns=adj)
        self.assertAlmostEqual(res.snapshots[-1].holdings, 0.0, places=6, msg="最後應該空手")
        pnl_sum = sum(t.pnl for t in res.trades)
        delta = res.metrics["final"] - res.metrics["initial"]
        self.assertAlmostEqual(
            pnl_sum, delta, delta=3.0, msg=f"跨分割的損益總和 {pnl_sum:,.2f} vs 權益變化 {delta:,.2f}"
        )

    def test_wealth_preserved_for_odd_ratio(self):
        """不是整數倍的分割（例如 1.25 倍減資）也要保持財富連續。"""
        bars, adj = self._setup(1.25)
        bt = PortfolioBacktester(1_000_000, CostConfig(), max_positions=1)
        res = bt.run({"ASSET": bars}, Hold(20, 100), benchmark=None, adj_returns=adj)
        snaps = res.snapshots
        before = snaps[self.SPLIT_IDX - 1].holdings
        after = snaps[self.SPLIT_IDX].holdings
        self.assertGreater(before, 0)
        self.assertAlmostEqual(
            after / before, 1.0, delta=0.05, msg=f"1.25 倍減資：{before:,.0f} -> {after:,.0f}"
        )

    def test_no_position_no_damage(self):
        """分割當天完全空手時，不該沒事找事。"""
        bars, adj = self._setup(4.0)
        bt = PortfolioBacktester(1_000_000, CostConfig(), max_positions=1)

        class Never:
            name = "never"

            def params(self):
                return {}

            def on_bar(self, ctx):
                return StrategyResponse()

        res = bt.run({"ASSET": bars}, Never(), benchmark=None, adj_returns=adj)
        for s in res.snapshots:
            self.assertEqual(s.holdings, 0.0)
            self.assertAlmostEqual(s.equity, s.cash, places=6)


class RebaseTradability(unittest.TestCase):
    """重新定價日的 gap 必然很大，不能被誤判成跌停而拒單。"""

    def test_can_buy_and_sell_on_rebase_day(self):
        price_after = 25.0
        prices = [100.0] * 60 + [price_after] * 40
        bars = mk_bars(prices)
        for i in range(60, len(bars)):
            b = bars[i]
            bars[i] = Bar(b.date, price_after, price_after, price_after, price_after, b.volume)
        adj = adj_from_bars(bars)

        bt = PortfolioBacktester(1_000_000, CostConfig(), max_positions=1)
        # 60 產生賣出訊號（空手時無效），61 買進：61 就是分割日
        class BuyOnRebase:
            name = "buy_on_rebase"

            def params(self):
                return {}

            def on_bar(self, ctx):
                return StrategyResponse(allocation={"ASSET": 0.4} if ctx.idx <= 60 else {})

        res = bt.run({"ASSET": bars}, BuyOnRebase(), benchmark=None, adj_returns=adj)
        snaps = {s.date: s for s in res.snapshots}
        target = bars[61].date
        self.assertGreater(
            snaps[target].holdings, 0, "分割日的買單不該被誤判成漲跌停而拒單"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
