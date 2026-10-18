"""一致性與邊界測試。

跟 test_engine.py 分開：那邊測「單一機制對不對」，這邊測「跨機制的數字彼此對不對」。
最典型的例子是交易紀錄的損益總和，必須等於權益曲線的變化 —— 這兩條路徑在程式裡
完全獨立，對不上就是有 bug，而且通常是那種不會丟例外的。
"""
from __future__ import annotations

import datetime as dt
import unittest

from twquant.engine import CostConfig, PortfolioBacktester, Position
from twquant.loader import Bar
from twquant.strategy import OrderIntent, StrategyResponse

VOL = 5_000_000


def mk_bars(prices: list[float], start: str = "2024-01-01", vol: int = VOL) -> list[Bar]:
    base = dt.date.fromisoformat(start)
    out = []
    for i, p in enumerate(prices):
        d = (base + dt.timedelta(days=i)).isoformat()
        out.append(Bar(d, p, p * 1.005, p * 0.995, p, vol))
    return out


class HoldThenSell:
    """第 enter_idx 天收盤後建立部位，第 exit_idx 天收盤後出場。"""

    name = "hold_then_sell"

    def __init__(self, enter_idx: int, exit_idx: int | None = None, weight: float = 0.5):
        self.enter_idx = enter_idx
        self.exit_idx = exit_idx
        self.weight = weight

    def params(self) -> dict:
        return {"enter_idx": self.enter_idx, "exit_idx": self.exit_idx, "weight": self.weight}

    def on_bar(self, ctx):
        if self.exit_idx is not None and ctx.idx >= self.exit_idx:
            return StrategyResponse(orders=[OrderIntent("ASSET", "sell", "test_exit")], allocation={})
        if ctx.idx >= self.enter_idx:
            return StrategyResponse(allocation={"ASSET": self.weight})
        return StrategyResponse()


class TradePnlConsistency(unittest.TestCase):
    """交易紀錄的損益總和 vs 權益曲線的變化。

    兩者用的是完全不同段程式碼（Trade 在成交時算、equity 在每日收盤重算），
    所以它們互相對帳是最有力的自我檢查。
    """

    def test_closed_trade_pnl_matches_equity_delta(self):
        prices = [100.0 + i * 0.5 for i in range(120)]
        bt = PortfolioBacktester(1_000_000, CostConfig(), max_positions=1)
        res = bt.run({"ASSET": mk_bars(prices)}, HoldThenSell(60, 100), benchmark=None)

        self.assertEqual(len(res.trades), 1, "應該剛好一筆平倉")
        trade = res.trades[0]
        equity_delta = res.metrics["final"] - res.metrics["initial"]

        # 全部平倉後，唯一還在帳上的就是這筆交易的損益
        self.assertAlmostEqual(
            trade.pnl,
            equity_delta,
            delta=1.0,  # 允許 1 元的浮點尾差
            msg=f"交易損益 {trade.pnl:,.2f} 與權益變化 {equity_delta:,.2f} 不一致",
        )

    def test_roundtrip_includes_entry_fee(self):
        """去程與回程的損益要包含買進手續費，不能只算賣出成本。"""
        prices = [100.0] * 120  # 完全不動，損益應該恰好是「買 + 賣」的總成本
        bt = PortfolioBacktester(1_000_000, CostConfig(), max_positions=1)
        res = bt.run({"ASSET": mk_bars(prices)}, HoldThenSell(60, 100), benchmark=None)

        trade = res.trades[0]
        self.assertLess(trade.pnl, 0, "價格沒動，扣掉成本必須是虧的")
        self.assertAlmostEqual(
            -trade.pnl,
            res.costs.total,
            delta=1.0,
            msg=f"來回成本 {res.costs.total:,.2f} 應該等於這筆的虧損 {-trade.pnl:,.2f}",
        )


class SplitConsistency(unittest.TestCase):
    """4:1 分割。價格砍半再砍半，但持有人的財富不該變。"""

    SPLIT_DAY = 80

    def _bars_with_split(self) -> list[Bar]:
        prices = [100.0] * self.SPLIT_DAY + [25.0] * 40
        return mk_bars(prices)

    def _adj_returns(self, bars: list[Bar]) -> dict[str, dict[str, float]]:
        split_date = bars[self.SPLIT_DAY].date
        out: dict[str, float] = {}
        for i in range(1, len(bars)):
            if bars[i].date == split_date:
                out[bars[i].date] = 0.0  # 分割日報酬歸零，這是 loader 的職責
            elif bars[i - 1].close > 0:
                out[bars[i].date] = bars[i].close / bars[i - 1].close - 1
        return {"ASSET": out}

    def test_split_does_not_create_fake_loss(self):
        """持有一檔股票遇到 4:1 分割，若沒有部位調整，
        平倉時會回報約 -75% 的假虧損。"""
        bars = self._bars_with_split()
        bt = PortfolioBacktester(1_000_000, CostConfig(), max_positions=1)
        res = bt.run(
            {"ASSET": bars},
            HoldThenSell(60, 110),
            benchmark=None,
            adj_returns=self._adj_returns(bars),
        )

        self.assertEqual(len(res.trades), 1)
        trade = res.trades[0]
        # 分割前後價格水準完全沒動（100 -> 100、25 -> 25），
        # 所以實際損益只該是手續費與證交稅，不該出現 -75% 這種假虧損
        self.assertGreater(
            trade.ret,
            -0.02,
            f"分割被當成虧損了：報酬 {trade.ret * 100:.1f}%（價格完全沒動，這不該發生）",
        )
        # 新語意：價格不換算，改換算股數。4:1 分割 -> 持股數變成 4 倍、
        # 每股成本降到 1/4，而進場價與出場價都留在原始價格座標、可以直接比。
        self.assertAlmostEqual(
            trade.entry_price,
            trade.exit_price,
            delta=trade.exit_price * 0.02,
            msg=f"進場價 {trade.entry_price:.4f} 與出場價 {trade.exit_price:.4f} 應該都在原始價格座標上",
        )
        self.assertGreater(trade.entry_price, 20, f"進場價 {trade.entry_price:.2f} 不該被除過分割")
        self.assertGreater(
            trade.shares,
            1000 * 4 - 1,
            f"4:1 分割後股數應該變成 4 倍以上，實際 {trade.shares}",
        )


class LimitUpThreshold(unittest.TestCase):
    """漲跌停判定：台股鎖漲停時 high == low，但便宜的計算方式會誤判。"""

    def test_pinned_limit_up_is_recognized_for_pricey_stock(self):
        """高價股的當日振幅換算成「幾塊錢」比較大，用固定百分比判定會誤放行。"""
        bars = mk_bars([1000.0] * 60)
        d = (dt.date.fromisoformat(bars[-1].date) + dt.timedelta(days=1)).isoformat()
        # 開 1100 鎖漲停，但當日 high/low 因為 tick 級別的微差而差 1.2 元
        bars.append(Bar(d, 1100.0, 1100.0, 1098.8, 1100.0, VOL))

        bt = PortfolioBacktester(1_000_000, CostConfig(), max_positions=1)
        res = bt.run({"ASSET": bars}, HoldThenSell(58), benchmark=None)
        self.assertEqual(
            max(s.holdings for s in res.snapshots),
            0.0,
            "鎖漲停（開盤即最高）不該成交，但被放行了",
        )

    def test_huge_range_is_tradable(self):
        """反過來：開盤跳空 +10% 但盤中大幅震盪，真的買得到，不該擋。"""
        bars = mk_bars([100.0] * 60)
        d = (dt.date.fromisoformat(bars[-1].date) + dt.timedelta(days=1)).isoformat()
        bars.append(Bar(d, 110.0, 118.0, 109.0, 116.0, VOL))
        bt = PortfolioBacktester(1_000_000, CostConfig(), max_positions=1)
        res = bt.run({"ASSET": bars}, HoldThenSell(58), benchmark=None)
        self.assertGreater(max(s.holdings for s in res.snapshots), 0, "有振幅的漲停應該成交得到")


class NoNegativeCash(unittest.TestCase):
    def test_cash_never_goes_negative(self):
        """多標的搶同一筆現金時，總花費不能超過可用現金。"""
        prices = [100.0 + i for i in range(120)]
        data = {f"A{i}": mk_bars(prices) for i in range(4)}

        class BuyAll:
            name = "buy_all"

            def params(self):
                return {}

            def on_bar(self, ctx):
                if ctx.idx < 60:
                    return StrategyResponse()
                return StrategyResponse(allocation={t: 0.3 for t in ctx.bars})

        bt = PortfolioBacktester(500_000, CostConfig(), max_positions=4)
        res = bt.run(data, BuyAll(), benchmark=None)
        for s in res.snapshots:
            self.assertGreaterEqual(s.cash, -1e-6, f"{s.date} 現金變成負的：{s.cash:,.2f}")


class DuplicateOrders(unittest.TestCase):
    def test_rebalance_and_signal_do_not_double_sell(self):
        """同一檔同時被『清倉』與『策略賣出』命中時，只該產生一筆賣單。

        兩筆的話第二筆會在執行時被 pop 成 None 而跳過，權益不會錯，
        但下單清單與日誌會出現幽靈訂單 —— 實單接上去就是重複委託。
        """
        bars = mk_bars([100.0 + i * 0.5 for i in range(120)])
        engine = PortfolioBacktester(1_000_000, CostConfig(), max_positions=1)
        positions = {"ASSET": Position("ASSET", 1000, bars[60].date, bars[60].close, 101_000.0)}
        today = {bars[60].date: bars[60]}
        bars_by_date = {"ASSET": {b.date: b for b in bars}}

        resp = StrategyResponse(orders=[OrderIntent("ASSET", "sell", "signal")], allocation={})
        orders = engine._plan(
            resp,
            positions,
            {bars[60].date: bars[60]},
            equity=1_000_000.0,
            cash=500_000.0,
            min_vol={"ASSET": 9e9},
            bars_of={"ASSET": bars},
            idx_of={"ASSET": {b.date: i for i, b in enumerate(bars)}},
            date=bars[60].date,
            day_no=60,
        )
        sells = [o for o in orders if o["side"] == "sell" and o["ticker"] == "ASSET"]
        self.assertEqual(len(sells), 1, f"同一檔產生 {len(sells)} 筆賣單：{orders}")

    def test_partial_exit_is_reported(self):
        """部分出場：策略賣掉一半，剩下的部位要留在帳上。"""
        bars = mk_bars([100.0 + i * 0.5 for i in range(120)])
        engine = PortfolioBacktester(1_000_000, CostConfig(), max_positions=1)
        positions = {"ASSET": Position("ASSET", 2000, bars[60].date, bars[60].close, 202_000.0)}
        resp = StrategyResponse(orders=[OrderIntent("ASSET", "sell", "half", shares=1000)], allocation={"ASSET": 0.15})
        orders = engine._plan(
            resp,
            positions,
            {bars[60].date: bars[60]},
            equity=1_000_000.0,
            cash=500_000.0,
            min_vol={"ASSET": 9e9},
            bars_of={"ASSET": bars},
            idx_of={"ASSET": {b.date: i for i, b in enumerate(bars)}},
            date=bars[60].date,
            day_no=60,
        )
        sells = [o for o in orders if o["side"] == "sell"]
        self.assertEqual(len(sells), 1)
        self.assertLess(sells[0]["shares"], 2000, "應該只賣一部分，不是整筆清掉")


class DirtyDataTest(unittest.TestCase):
    """真實資料裡真的有「價格全是 0」的 bar。

    FinMind 對 006208 的早期資料回傳過 152 根 open/close 都是 0 的 bar，
    這種東西進到引擎會讓「以價格為分母」的計算直接爆掉（OverflowError），
    合成資料測不到，所以要有專門的測試。
    """

    def test_zero_price_rows_are_dropped(self):
        from twquant.loader import bars_from_rows

        rows = [
            ("2024-01-01", 100.0, 101.0, 99.0, 100.5, 5_000_000),
            ("2024-01-02", 0.0, 0.0, 0.0, 0.0, 0),  # 壞 bar
            ("2024-01-03", 100.5, 102.0, 100.0, 101.0, 4_000_000),
            ("2024-01-04", 101.0, 101.0, 101.0, 0.0, 1_000),  # 收盤 0 也算壞
            ("2024-01-05", 101.0, 103.0, 100.5, 102.0, 6_000_000),
        ]
        bars = bars_from_rows(rows)
        self.assertEqual([b.date for b in bars], ["2024-01-01", "2024-01-03", "2024-01-05"])

    def test_all_dirty_returns_empty(self):
        from twquant.loader import bars_from_rows

        self.assertEqual(bars_from_rows([("2024-01-01", 0, 0, 0, 0, 0)]), [])

    def test_null_volume_does_not_crash(self):
        from twquant.loader import bars_from_rows

        bars = bars_from_rows([("2024-01-01", 100.0, 101.0, 99.0, 100.0, None)])
        self.assertEqual(bars[0].volume, 0)

    def test_engine_survives_zero_price_bar_in_feed(self):
        """就算有壞 bar 溜進 data，引擎也不該炸 —— 只該忽略它。"""
        bars = mk_bars([100.0 + i for i in range(120)])
        d = (dt.date.fromisoformat(bars[70].date) + dt.timedelta(hours=12)).isoformat()[:10]
        bars[70] = Bar(d, 0.0, 0.0, 0.0, 0.0, 0)
        bt = PortfolioBacktester(1_000_000, CostConfig(), max_positions=1)
        res = bt.run({"ASSET": bars}, HoldThenSell(60, 110), benchmark=None)
        self.assertTrue(res.snapshots)


class AddThenExitConsistency(unittest.TestCase):
    """加碼之後平倉。

    entry_price 只在「新建部位」的分支被設定，加碼分支只更新 cost。
    而平倉損益用的是 (px - entry_price)，於是整個部位（含加碼的股數）
    都用「第一次買進價」結算 —— 損益虛增，而且虛增幅度剛好等於
    Σ 加碼股數 × (第一次買價 − 該批真實成交價)。
    """

    def _run(self, weight_late: float = 0.9):
        prices = [100.0 + i for i in range(140)]
        bars = mk_bars(prices)

        class AddThenExit:
            name = "add_then_exit"

            def params(self):
                return {}

            def on_bar(self, ctx):
                if ctx.idx >= 100:
                    return StrategyResponse(orders=[OrderIntent("ASSET", "sell", "exit")], allocation={})
                if ctx.idx == 60:
                    return StrategyResponse(allocation={"ASSET": 0.25})
                if ctx.idx == 80:
                    # 拉高權重會讓引擎發出 'add' 單，走上加碼分支
                    return StrategyResponse(allocation={"ASSET": weight_late})
                if ctx.idx > 60:
                    return StrategyResponse(allocation={"ASSET": 0.25})
                return StrategyResponse()

        bt = PortfolioBacktester(1_000_000, CostConfig(), max_positions=1)
        return bt.run({"ASSET": bars}, AddThenExit(), benchmark=None)

    def test_add_path_is_actually_exercised(self):
        """先確認這個測試真的會走到加碼分支，否則它只是在測別的東西。"""
        res = self._run()
        buys = [t for t in res.trades]
        self.assertTrue(buys, "應該有平倉紀錄")
        # 部位應在 60 進場、80 加碼，所以最終持股大於單次進場能買到的量
        self.assertGreater(res.trades[0].shares, 1000 * 2, "加碼單沒有成交，這個測試失去意義")

    def test_add_then_exit_pnl_matches_equity(self):
        res = self._run()
        equity_delta = res.metrics["final"] - res.metrics["initial"]
        pnl_sum = sum(t.pnl for t in res.trades)

        self.assertEqual(max(s.holdings for s in res.snapshots) > 0, True)
        self.assertAlmostEqual(
            res.snapshots[-1].holdings, 0.0, places=6, msg="最後應該完全空手，否則等式不能用"
        )
        self.assertAlmostEqual(
            pnl_sum,
            equity_delta,
            delta=2.0,
            msg=(
                f"加碼後平倉：交易損益總和 {pnl_sum:,.2f} 與權益變化 {equity_delta:,.2f} 不一致"
                f"（差 {pnl_sum - equity_delta:,.2f}）"
            ),
        )

    def test_entry_price_is_weighted_average(self):
        """進場價必須是加權平均成交價，不能停在第一次買進的價格。"""
        res = self._run()
        trade = res.trades[0]
        # 價格從 100 一路漲到 240，加碼在 80 附近（約 180 元）。
        # 若 entry_price 停在第一次買價（約 160），會明顯低於加碼後的真實均價。
        self.assertGreater(
            trade.entry_price,
            170,
            f"entry_price {trade.entry_price:.2f} 看起來是第一次買進價，不是加權平均",
        )


    def test_multi_add_then_partial_exits_conserve_pnl(self):
        """多次加碼 + 分批出場（加碼與部分出場的組合，先前是兩者各自的盲區）。

        這裡同時考驗兩件事：
          - entry_price 在每次加碼後都是加權平均（不是第一次買價）
          - entry_fee 在部分出場時按股數比例分攤，不能漏扣或重複扣
        最後一次賣光後，Σtrade.pnl 必須等於權益變化。
        """
        prices = [100.0 + i for i in range(200)]
        bars = mk_bars(prices)

        class MultiAddPartialExit:
            name = "multi_add_partial_exit"

            def params(self):
                return {}

            def on_bar(self, ctx):
                if ctx.idx >= 150:
                    return StrategyResponse(allocation={})
                if ctx.idx == 150:
                    return StrategyResponse(
                        orders=[OrderIntent("ASSET", "sell", "half", shares=1000)], allocation={"ASSET": 0.0}
                    )
                if ctx.idx == 130:
                    return StrategyResponse(
                        orders=[OrderIntent("ASSET", "sell", "quarter", shares=1000)], allocation={"ASSET": 0.6}
                    )
                if ctx.idx == 100:
                    return StrategyResponse(allocation={"ASSET": 0.8})
                if ctx.idx == 80:
                    return StrategyResponse(allocation={"ASSET": 0.5})
                if ctx.idx == 60:
                    return StrategyResponse(allocation={"ASSET": 0.2})
                if ctx.idx > 60:
                    return StrategyResponse(allocation={"ASSET": 0.2})
                return StrategyResponse()

        bt = PortfolioBacktester(1_000_000, CostConfig(), max_positions=1)
        res = bt.run({"ASSET": bars}, MultiAddPartialExit(), benchmark=None)

        self.assertGreaterEqual(len(res.trades), 2, "應該有多筆平倉紀錄（部分出場）")
        self.assertAlmostEqual(res.snapshots[-1].holdings, 0.0, places=6, msg="最後必須完全空手")
        pnl_sum = sum(t.pnl for t in res.trades)
        equity_delta = res.metrics["final"] - res.metrics["initial"]
        self.assertAlmostEqual(
            pnl_sum,
            equity_delta,
            delta=3.0,
            msg=f"多次加碼+部分出場：損益總和 {pnl_sum:,.2f} vs 權益變化 {equity_delta:,.2f}",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
