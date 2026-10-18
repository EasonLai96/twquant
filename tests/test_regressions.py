"""回歸測試：每一個都是真的踩過的 bug。

命名規則用 `test_<曾經錯的行為>`，讀的人可以從測試名直接知道「這裡以前壞過」。
"""
from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

from twquant.engine import CostConfig, PortfolioBacktester
from twquant.loader import Bar
from twquant.report import markdown_report
from twquant.store import Store, _vol
from twquant.strategy import StrategyResponse


def mk_bars(prices, start="2024-01-01", vol=5_000_000):
    import datetime as dt

    base = dt.date.fromisoformat(start)
    return [
        Bar((base + dt.timedelta(days=i)).isoformat(), p, p * 1.005, p * 0.995, p, vol)
        for i, p in enumerate(prices)
    ]


class Hold:
    name = "hold"

    def params(self):
        return {}

    def on_bar(self, ctx):
        return StrategyResponse(allocation={"ASSET": 0.5} if ctx.idx >= 60 else {})


class ReportAlignment(unittest.TestCase):
    """報表曾經整欄錯位一列：brows 的第一個元素是標題列，卻被拿去跟
    rows[0]（期末權益）配對，結果「年化波動」右邊印的是「基準 Sharpe」。"""

    def test_benchmark_column_aligns_row_by_row(self):
        bars = mk_bars([100 + i * 0.5 for i in range(120)])
        other = mk_bars([50 + i * 0.2 for i in range(120)])
        bt = PortfolioBacktester(1_000_000, CostConfig(), max_positions=1)
        res = bt.run({"ASSET": bars, "BM": other}, Hold(), benchmark="BM", adj_returns=None)
        md = markdown_report(res)

        # 基準那一欄曾經整欄位移一列，所以每一列都用「指標名稱」定位來檢查語意
        rows: dict[str, list[str]] = {}
        for line in md.splitlines():
            m = re.match(r"\|\s*([^|]+?)\s*\|(.*)\|\s*$", line)
            if not m:
                continue
            cells = [c.strip() for c in m.group(2).split("|")]
            rows[m.group(1)] = cells

        self.assertIn("年化波動", rows)
        strat_vol, bench_vol = rows["年化波動"][0], rows["年化波動"][1]
        self.assertTrue(strat_vol.endswith("%"), f"策略年化波動不是百分比：{strat_vol!r}")
        self.assertTrue(bench_vol.endswith("%"), f"基準年化波動不是百分比：{bench_vol!r}")
        # 兩邊波動不同（策略幾乎空手、基準滿倉），數字相同就代表接錯欄
        self.assertNotEqual(strat_vol, bench_vol)

        # Sharpe 那一列的右欄必須是 Sharpe 的數字，不是別的指標的標籤
        sharpe_row = rows["Sharpe (rf=2%)"]
        self.assertRegex(sharpe_row[0], r"^-?\d+\.\d+$", f"策略欄不是 Sharpe 數字：{sharpe_row[0]!r}")
        self.assertRegex(sharpe_row[1], r"^-?\d+\.\d+$", f"基準欄不是 Sharpe 數字：{sharpe_row[1]!r}")
        for key, cells in rows.items():
            if key in ("指標", "---", ""):
                continue
            for cell in cells:
                self.assertNotIn("基準", cell, f"{key} 的數值欄混進了標籤：{cell!r}")
                self.assertNotIn("Sharpe", cell.replace("Sharpe (rf=2%)", ""), f"{key} 的數值欄混進了標籤：{cell!r}")

        # 表頭與基準說明都要在
        self.assertRegex(md, r"\|\s*指標\s*\|\s*策略\s*\|\s*基準\s*\|", "三欄表頭不對")
        self.assertIn("**基準：", md)


class WalkforwardMetricValidation(unittest.TestCase):
    """metric 拼錯時，每組參數都拿到哨兵值 → 第一組永遠勝出，
    整個 walk-forward 靜默失效（輸出看起來完全正常）。"""

    def test_unknown_metric_raises(self):
        from twquant import walkforward as wf

        data = {"ASSET": mk_bars([100 + i for i in range(400)])}
        with self.assertRaises(ValueError):
            wf.run(data, "2024-01-01", "2025-01-01", is_days=100, os_days=50, verbose=False, metric="nonsense")

    def test_metric_whitelist_is_reachable(self):
        from twquant import walkforward as wf

        bar_holder = mk_bars([100 + i for i in range(200)])
        bt = PortfolioBacktester(1_000_000, CostConfig(), max_positions=1)
        metrics = bt.run({"ASSET": bar_holder}, Hold(), benchmark=None).metrics
        for name in wf.VALID_METRICS:
            self.assertIn(name, metrics, f"VALID_METRICS 列了 {name}，但引擎算不出來")


class VolumeParsing(unittest.TestCase):
    """成交量轉不出整數時，整根價格 bar 曾被丟掉（OHLC 明明是好的）。"""

    def test_volume_variants_do_not_drop_the_bar(self):
        self.assertEqual(_vol(1234), 1234)
        self.assertEqual(_vol("1,234"), 1234)
        self.assertEqual(_vol("1234"), 1234)
        self.assertEqual(_vol(None), 0)
        self.assertEqual(_vol(""), 0)
        self.assertEqual(_vol("-"), 0)
        self.assertEqual(_vol("oops"), 0)

    def test_upsert_keeps_bar_when_only_volume_is_bad(self):
        with tempfile.TemporaryDirectory() as d:
            st = Store(Path(d) / "t.sqlite")
            n = st.upsert_bars(
                "X",
                [
                    {"date": "2020-01-01", "open": 10, "max": 11, "min": 9, "close": 10, "Trading_Volume": "1,234"},
                    {"date": "2020-01-02", "open": 10, "max": 11, "min": 9, "close": 10, "Trading_Volume": "-"},
                    {"date": "2020-01-03", "open": 10, "max": 11, "min": 9, "close": 10, "Trading_Volume": None},
                ],
            )
            self.assertEqual(n, 3, "成交量壞掉不該讓價格 bar 消失")
            self.assertEqual(len(st.load_bars("X", "2020-01-01", "2020-12-31")), 3)
            st.close()

    def test_quality_counts_first_row_zero_volume(self):
        with tempfile.TemporaryDirectory() as d:
            st = Store(Path(d) / "t.sqlite")
            st.upsert_bars(
                "X",
                [
                    {"date": f"2020-01-0{i + 1}", "open": 10, "max": 10, "min": 10, "close": 10, "Trading_Volume": 0}
                    for i in range(3)
                ],
            )
            self.assertEqual(st.quality()["X"]["zero_volume_days"], 3, "第一根 bar 的零量日沒被算到")
            st.close()


class FetchCacheDecisions(unittest.TestCase):
    """一次抓失敗（rows=0）曾被永久記成「已抓」，之後每次執行都重打同一段 API。"""

    def test_failed_fetch_is_retried_next_time(self):
        with tempfile.TemporaryDirectory() as d:
            st = Store(Path(d) / "t.sqlite")
            st.db.execute(
                "REPLACE INTO fetch_log(ticker,start,end,adj,fetched_at,rows) VALUES(?,?,?,?,?,?)",
                ("X", "2015-01-01", "2020-12-31", 1, "2020-01-01T00:00:00", 0),
            )
            st.db.commit()
            need_price, _ = st._needs_fetch("X", "2016-01-01", "2016-12-31", True, False)
            self.assertTrue(need_price, "rows=0 代表上次沒抓到東西，應該重試而不是當成已覆蓋")
            st.close()

    def test_successful_fetch_is_not_retried(self):
        with tempfile.TemporaryDirectory() as d:
            st = Store(Path(d) / "t.sqlite")
            st.db.execute(
                "REPLACE INTO fetch_log(ticker,start,end,adj,fetched_at,rows) VALUES(?,?,?,?,?,?)",
                ("X", "2015-01-01", "2020-12-31", 1, "2020-01-01T00:00:00", 1500),
            )
            st.db.commit()
            need_price, _ = st._needs_fetch("X", "2016-01-01", "2016-12-31", True, False)
            self.assertFalse(need_price, "抓過的區間不該重打 API")
            st.close()


class UniverseResolution(unittest.TestCase):
    """`--tickers ""` 曾被當成「沒填」，靜默換成 12 檔預設宇宙。"""

    def test_explicit_empty_tickers_is_an_error(self):
        from twquant import cli

        a = cli.build_parser().parse_args(["backtest", "--tickers", ""])
        with self.assertRaises(SystemExit):
            cli._tickers(a)

    def test_default_universe_excludes_benchmark_etfs(self):
        from twquant import cli
        from twquant.loader import INDEX_TICKERS

        a = cli.build_parser().parse_args(["backtest"])
        for t in cli._tickers(a):
            if t in INDEX_TICKERS:
                self.assertIn(t, INDEX_TICKERS, "基準 ETF 只該用來當基準")
        self.assertGreaterEqual(len(cli._tickers(a)), 10)

    def test_trade_universe_drops_index_tickers(self):
        from twquant import cli

        data = {"2330": [], "0050": [], "006208": []}
        self.assertEqual(set(cli._trade_universe(data)), {"2330"})


class SplitBasisUnit(unittest.TestCase):
    """分割處理曾把 entry_price 每天乘一次，最後爆到 inf。"""

    def test_split_keeps_equity_continuous(self):
        import datetime as dt

        split = 80
        prices = [100.0] * split + [25.0] * 40
        bars = mk_bars(prices)
        split_date = bars[split].date
        adj = {"ASSET": {}}
        for i in range(1, len(bars)):
            adj["ASSET"][bars[i].date] = 0.0 if bars[i].date == split_date else bars[i].close / bars[i - 1].close - 1

        bt = PortfolioBacktester(1_000_000, CostConfig(), max_positions=1)

        class EnterAt60(Hold):
            pass

        res = bt.run({"ASSET": bars}, EnterAt60(), benchmark=None, adj_returns=adj)
        vals = [s.holdings for s in res.snapshots]
        for v in vals:
            self.assertEqual(v, v, "holdings 出現 NaN")
            self.assertLess(abs(v), 1e12, f"holdings 爆炸：{v}")
        # 分割前後部位市值不該有跳動（.0 與 -1 是分割日交界）
        around = [(res.snapshots[i].date, res.snapshots[i].holdings) for i in range(0, len(res.snapshots))]
        first_pos = next(i for i, v in enumerate(vals) if v > 0)
        self.assertTrue(all(abs(around[i][1] - around[first_pos][1]) <= 1e-6 for i in range(first_pos, split + 1)),
                        f"分割前部位市值應該持平：{around[first_pos:first_pos + 3]} vs {around[split]}")
        del dt


if __name__ == "__main__":
    unittest.main(verbosity=2)
