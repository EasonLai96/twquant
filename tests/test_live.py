"""PaperBroker 與回測引擎的對帳。

兩個實作用同一份資料、同一組參數跑，逐筆交易（日期、標的、股數、成交價）必須相同。
這是唯一能驗證「實單路徑沒有偷偷跟回測分歧」的方法 —— 兩邊各自都通過自己的測試
並不代表它們算的是同一件事。

不一樣就是其中一個有 bug，而且通常是那種不會丟例外、只會讓你在實單上虧錢的那種。
"""
from __future__ import annotations

import datetime as dt
import math
import random
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from twquant.engine import CostConfig, PortfolioBacktester  # noqa: E402
from twquant.loader import Bar  # noqa: E402
from twquant.live.runner import LiveRunner  # noqa: E402
from twquant.paper import PaperBroker  # noqa: E402
from twquant.strategies import TrendMA  # noqa: E402

SEED = 20261019


def synth(ticker: str, days: int, drift: float, vol: float, seed: int) -> list[Bar]:
    rng = random.Random(seed)
    px = 100.0
    d0 = dt.date(2020, 1, 2)
    out: list[Bar] = []
    i = 0
    while len(out) < days:
        d = d0 + dt.timedelta(days=i)
        i += 1
        if d.weekday() >= 5:
            continue
        px *= math.exp(drift / 252 + rng.gauss(0, vol / math.sqrt(252)))
        o = px * (1 + rng.gauss(0, 0.003))
        h = max(o, px) * (1 + abs(rng.gauss(0, 0.006)))
        lo = min(o, px) * (1 - abs(rng.gauss(0, 0.006)))
        vol_shares = int(abs(rng.gauss(8_000_000, 2_000_000))) + 1_000_000
        out.append(Bar(d.isoformat(), round(o, 2), round(h, 2), round(lo, 2), round(px, 2), vol_shares))
    return out


def make_data(n: int = 3, days: int = 900) -> dict[str, list[Bar]]:
    drifts = [0.22, 0.05, -0.06, 0.12, 0.01]
    return {
        f"T{i}": synth(f"T{i}", days, drifts[i % len(drifts)], 0.28, SEED + i * 7919)
        for i in range(n)
    }


def adj_identity(bars: dict[str, list[Bar]]) -> dict[str, dict[str, float]]:
    out = {}
    for t, bs in bars.items():
        m = {}
        for i in range(1, len(bs)):
            prev, cur = bs[i - 1].close, bs[i].close
            m[bs[i].date] = (cur / prev - 1) if prev > 0 else 0.0
        out[t] = m
    return out


class PaperMatchesBacktest(unittest.TestCase):
    def _run_both(self, data, strat_kw):
        cost = CostConfig()
        adj = adj_identity(data)

        engine = PortfolioBacktester(1_000_000, cost, max_positions=strat_kw["max_positions"])
        res = engine.run(data, TrendMA(**strat_kw), benchmark=None, adj_returns=adj)

        broker = PaperBroker(data, capital=1_000_000, cost=cost, adj_returns=adj)
        with tempfile.TemporaryDirectory() as d:
            runner = LiveRunner(
                broker, TrendMA(**strat_kw), data, state_dir=d,
                capital=1_000_000, cost=cost, adj_returns=adj, verbose=False,
            )
            rep = runner.run()
            runner.state.close()
        return res, broker, rep

    def test_trade_sets_are_identical(self):
        data = make_data(n=3, days=900)
        strat_kw = dict(fast=20, slow=60, max_positions=2)
        res, broker, rep = self._run_both(data, strat_kw)

        eng = [
            (t.exit_date, t.ticker, t.shares, round(t.exit_price, 4))
            for t in res.trades
        ]
        pap = [
            (f.ts, f.ticker, f.shares, round(f.price, 4))
            for f in broker.fills
            if f.side == "sell"
        ]

        self.assertTrue(eng, "回測沒有任何平倉，這個測試沒有驗證到東西")
        self.assertTrue(pap, "PaperBroker 沒有任何賣出成交")

        # 最後一天下的單在回測裡永遠不會成交（沒有隔天的開盤價），PaperBroker 也一樣，
        # 所以筆數本來就可能差 0~1 筆，其餘必須完全一致
        n = min(len(eng), len(pap))
        self.assertGreaterEqual(n, 3, f"可比較的成交太少：引擎 {len(eng)} 筆，模擬 {len(pap)} 筆")
        for i in range(n):
            self.assertEqual(
                eng[i], pap[i],
                f"第 {i} 筆成交不一致\n  引擎: {eng[i]}\n  模擬: {pap[i]}",
            )

    def test_costs_match(self):
        data = make_data(n=3, days=900)
        strat_kw = dict(fast=20, slow=60, max_positions=2)
        res, broker, rep = self._run_both(data, strat_kw)

        eng_cost = res.costs.fee + res.costs.tax
        pap_cost = broker.costs_total()
        # 引擎在最後一天不會成交，模擬也是，但引擎的成本帳只累計已成交的部分
        self.assertAlmostEqual(
            eng_cost, pap_cost, delta=max(500.0, eng_cost * 0.01),
            msg=f"成本不一致：引擎 {eng_cost:,.0f} vs 模擬 {pap_cost:,.0f}",
        )

    def test_cash_is_never_negative(self):
        data = make_data(n=5, days=700)
        _, broker, _ = self._run_both(data, dict(fast=10, slow=40, max_positions=3))
        self.assertGreaterEqual(broker.account().cash, 0.0)

    def test_rerun_is_idempotent_after_restart(self):
        """中斷再用同一份 state 跑一次，不該產生重複的部位。"""
        data = make_data(n=3, days=700)
        cost = CostConfig()
        adj = adj_identity(data)
        strat_kw = dict(fast=20, slow=60, max_positions=2)

        with tempfile.TemporaryDirectory() as d:
            b1 = PaperBroker(data, capital=1_000_000, cost=cost, adj_returns=adj)
            r1 = LiveRunner(b1, TrendMA(**strat_kw), data, state_dir=d, capital=1_000_000,
                            cost=cost, adj_returns=adj, verbose=False)
            rep1 = r1.run()
            n1 = len(b1.fills)
            r1.state.close()

            b2 = PaperBroker(data, capital=1_000_000, cost=cost, adj_returns=adj)
            r2 = LiveRunner(b2, TrendMA(**strat_kw), data, state_dir=d, capital=1_000_000,
                            cost=cost, adj_returns=adj, verbose=False)
            rep2 = r2.run()
            n2 = len(b2.fills)
            r2.state.close()

            self.assertEqual(n1, n2, "同一個 state 重跑，成交筆數不該改變")
            self.assertAlmostEqual(rep1.end_equity, rep2.end_equity, delta=1.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
