"""回測引擎正確性測試。用合成資料，不碰網路。

重點在驗證：沒有未來函數（T 日訊號 T+1 開盤成交）、成本有扣、漲跌停不成交、
不買零股、權益恆等式成立。
"""
from __future__ import annotations

import unittest
from dataclasses import dataclass

from twquant.engine import CostConfig, PortfolioBacktester, compute_metrics
from twquant.loader import Bar, atr, ema, rolling_high, sma
from twquant.strategy import MarketContext, OrderIntent, StrategyResponse


def mk_bars(prices: list[float], start_day: int = 1, vol: int = 5_000_000) -> list[Bar]:
    import datetime as dt

    base = dt.date(2024, 1, 1)
    out = []
    for i, p in enumerate(prices):
        d = (base + dt.timedelta(days=start_day - 1 + i)).isoformat()
        out.append(Bar(d, p, p * 1.005, p * 0.995, p, vol))
    return out


@dataclass
class BuyOnce:
    """第 N 天買進後抱住不動，用來驗證成交價與成本。"""

    name: str = "buy_once"
    at_idx: int = 60
    done: bool = False

    def params(self) -> dict:
        return {"at_idx": self.at_idx}

    def on_bar(self, ctx: MarketContext) -> StrategyResponse:
        t = "TEST"
        if ctx.idx == self.at_idx and not self.done:
            self.done = True
            return StrategyResponse(orders=[], allocation={t: 0.5})
        if self.done:
            return StrategyResponse(orders=[], allocation={t: 0.5})
        return StrategyResponse(orders=[], allocation={})


class NoFee(CostConfig):
    def __init__(self, **kw):
        super().__init__(fee_rate=0.0, fee_discount=1.0, fee_min=0.0, tax_sell=0.0, slippage_bps=0.0, **kw)


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.prices = [100 + i * 0.5 for i in range(120)]
        self.data = {"TEST": mk_bars(self.prices)}

    def test_no_lookahead_and_fill_price(self):
        bt = PortfolioBacktester(1_000_000, NoFee(), max_positions=1)
        res = bt.run(self.data, BuyOnce(at_idx=60), benchmark=None)
        buys = [t for t in res.trades]
        self.assertTrue(res.snapshots)
        # 持有到最後，不應該產生平倉紀錄
        self.assertEqual(len(buys), 0)
        # 第 61 根（idx 60）產生訊號 -> 第 62 根的開盤價成交
        pos_day = [s for s in res.snapshots if s.holdings > 1]
        self.assertTrue(pos_day, "應該要有部位")
        first = pos_day[0]
        expect_idx = 61
        self.assertEqual(first.date, self.data["TEST"][expect_idx].date)

    def test_costs_are_charged(self):
        cfg = CostConfig(fee_discount=1.0, slippage_bps=10.0)
        bt = PortfolioBacktester(1_000_000, cfg, max_positions=1)
        res = bt.run(self.data, BuyOnce(at_idx=60), benchmark=None)
        self.assertGreater(res.costs.fee, 0)
        self.assertGreater(res.costs.slippage, 0)
        self.assertEqual(res.costs.tax, 0.0)

    def test_equity_identity(self):
        bt = PortfolioBacktester(1_000_000, CostConfig(), max_positions=1)
        res = bt.run(self.data, BuyOnce(at_idx=60), benchmark=None)
        for s in res.snapshots:
            self.assertAlmostEqual(s.equity, s.cash + s.holdings, places=6)

    def test_lot_size(self):
        # 100~160 元的價格區間、一筆 50 萬部位：任何時刻都不該出現零股
        bt = PortfolioBacktester(500_000, NoFee(), max_positions=1)
        res = bt.run(self.data, BuyOnce(at_idx=60), benchmark=None)
        by_date = {b.date: b for b in self.data["TEST"]}
        checked = 0
        for s in res.snapshots:
            if s.holdings <= 0:
                continue
            shares = s.holdings / by_date[s.date].close
            self.assertAlmostEqual(shares % 1000, 0.0, places=6, msg=f"{s.date} 出現零股 {shares}")
            checked += 1
        self.assertGreater(checked, 0, "整段都沒有部位")

    def test_limit_up_blocks_entry(self):
        # 前 60 根平盤，第 61 根（idx 60）跳空 +10% 開出且全天鎖死在漲停
        prices = [100.0] * 60
        bars = mk_bars(prices)
        import datetime as dt

        d = (dt.date.fromisoformat(bars[-1].date) + dt.timedelta(days=1)).isoformat()
        bars.append(Bar(d, 110.0, 110.0, 110.0, 110.0, 5_000_000))
        # 訊號在 idx 59 產生（該根收盤），訂單在 idx 60 的開盤成交 -> 那天鎖漲停
        bt = PortfolioBacktester(1_000_000, CostConfig(), max_positions=1)
        res = bt.run({"TEST": bars}, BuyOnce(at_idx=59), benchmark=None)
        self.assertEqual(len(res.trades), 0)
        self.assertTrue(all(s.holdings == 0 for s in res.snapshots), "鎖漲停不該成交")

    def test_limit_up_open_but_not_pinned_is_tradable(self):
        # 開盤跳空 +10% 但盤中打開（high 遠高於 open）：實際上買得到
        bars = mk_bars([100.0] * 60)
        import datetime as dt

        d = (dt.date.fromisoformat(bars[-1].date) + dt.timedelta(days=1)).isoformat()
        bars.append(Bar(d, 110.0, 118.0, 109.0, 116.0, 5_000_000))
        bt = PortfolioBacktester(1_000_000, CostConfig(), max_positions=1)
        res = bt.run({"TEST": bars}, BuyOnce(at_idx=59), benchmark=None)
        self.assertGreater(max(s.holdings for s in res.snapshots), 0, "打開的漲停應該成交得到")

    def test_zero_volume_blocks_entry(self):
        bars = mk_bars([100.0] * 60)
        import datetime as dt

        d = (dt.date.fromisoformat(bars[-1].date) + dt.timedelta(days=1)).isoformat()
        bars.append(Bar(d, 100.0, 100.0, 100.0, 100.0, 0))
        bt = PortfolioBacktester(1_000_000, CostConfig(), max_positions=1)
        res = bt.run({"TEST": bars}, BuyOnce(at_idx=59), benchmark=None)
        self.assertTrue(all(s.holdings == 0 for s in res.snapshots), "無量不該成交")

    def test_sell_tax_charged(self):
        up = [100 + i for i in range(80)] + [180 - i for i in range(80, 140)]

        class BuySell:
            name = "buy_sell"

            def params(self):
                return {}

            def on_bar(self, ctx):
                if ctx.idx == 60:
                    return StrategyResponse(allocation={"TEST": 0.5})
                if ctx.idx >= 100:
                    return StrategyResponse(orders=[OrderIntent("TEST", "sell", "test")], allocation={})
                if ctx.idx > 60:
                    return StrategyResponse(allocation={"TEST": 0.5})
                return StrategyResponse(allocation={})

        bt = PortfolioBacktester(1_000_000, CostConfig(), max_positions=1)
        res = bt.run({"TEST": mk_bars(up)}, BuySell(), benchmark=None)
        self.assertEqual(len(res.trades), 1)
        self.assertGreater(res.costs.tax, 0)
        t = res.trades[0]
        self.assertLess(t.exit_date, up and "2100-01-01")


class IndicatorTest(unittest.TestCase):
    def test_sma(self):
        xs = [1, 2, 3, 4, 5]
        self.assertEqual(sma(xs, 3), [None, None, 2.0, 3.0, 4.0])

    def test_ema_seed(self):
        xs = [float(i) for i in range(10)]
        e = ema(xs, 3)
        self.assertIsNone(e[1])
        self.assertAlmostEqual(e[2], 1.0)
        self.assertGreater(e[-1], e[-2])

    def test_atr_positive(self):
        bars = mk_bars([100 + i for i in range(40)])
        a = atr(bars, 14)
        self.assertIsNone(a[5])
        self.assertIsNotNone(a[14])
        self.assertGreater(a[20], 0)

    def test_rolling_high_excludes_today(self):
        xs = [1, 5, 3]
        self.assertEqual(rolling_high(xs, 2), [None, None, 5])


class MetricsTest(unittest.TestCase):
    def test_max_drawdown(self):
        from twquant.engine import Snapshot

        snaps = [
            Snapshot("2024-01-01", 100, 0, 100, 0.0, 0.0),
            Snapshot("2024-01-02", 100, 0, 120, 0.2, 0.0),
            Snapshot("2024-01-03", 100, 0, 90, -0.25, 0.0),
            Snapshot("2024-01-04", 100, 0, 95, 0.05, 0.0),
        ]
        m = compute_metrics(snaps, [])
        self.assertAlmostEqual(m["max_drawdown"], 90 / 120 - 1, places=6)
        self.assertEqual(m["mdd_peak_date"], "2024-01-02")
        self.assertEqual(m["mdd_trough_date"], "2024-01-03")

    def test_profit_factor(self):
        from twquant.engine import Snapshot, Trade

        snaps = [Snapshot("2024-01-01", 0, 0, 100, 0.0, 0.0), Snapshot("2024-01-02", 0, 0, 110, 0.1, 0.0)]
        trades = [
            Trade("A", "2024-01-01", "2024-01-02", 10, 12, 100, 200, 0.2, 1, "x"),
            Trade("B", "2024-01-01", "2024-01-02", 10, 9, 100, -100, -0.1, 1, "x"),
        ]
        m = compute_metrics(snaps, trades)
        self.assertAlmostEqual(m["profit_factor"], 2.0)
        self.assertAlmostEqual(m["win_rate"], 0.5)


class IndexHelperTest(unittest.TestCase):
    """這支曾經 off-by-one，讓所有訊號都對不上日期，單獨釘住它。"""

    def test_idx_returns_exact_match(self):
        from twquant.strategies import _idx

        bars = mk_bars([100 + i for i in range(30)])
        for i, b in enumerate(bars):
            self.assertEqual(_idx(bars, b.date), i, f"{b.date} 應該回 {i}")

    def test_idx_non_trading_date_falls_back(self):
        from twquant.strategies import _idx

        bars = mk_bars([100 + i for i in range(10)])
        # mk_bars 產生的是連續日曆日，idx 5 是 2024-01-06
        self.assertEqual(bars[5].date, "2024-01-06")
        self.assertEqual(_idx(bars, "2024-01-06"), 5)
        # 早於第一根的日子要夾在 0，不能回 -1
        self.assertEqual(_idx(bars, "2023-12-31"), 0)

    def test_engine_prev_bar(self):
        from twquant.engine import PortfolioBacktester

        bars = mk_bars([100 + i for i in range(10)])
        # 必須嚴格早於 date：日期相同時回同一根會讓漲跌停 gap 永遠是 0
        self.assertEqual(PortfolioBacktester._prev_bar(bars, bars[5].date).date, bars[4].date)
        self.assertEqual(PortfolioBacktester._prev_bar(bars, bars[5].date).close, bars[4].close)
        self.assertEqual(PortfolioBacktester._prev_bar(bars, "2024-01-06.5").date, bars[5].date)
        self.assertIsNone(PortfolioBacktester._prev_bar(bars, bars[0].date))


if __name__ == "__main__":
    unittest.main(verbosity=2)
