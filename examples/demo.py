"""零安裝、零網路、零 API 的 demo。

    python examples/demo.py

用合成資料（幾何布朗運動 + 一段刻意的人造趨勢）跑完整流程，
讓你在一分鐘內看懂這個引擎的輸出長什麼樣，不必先申請 FinMind token。
看真實台股的結果請跑 README 的「五分鐘路線」。
"""
from __future__ import annotations

import math
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from twquant.engine import CostConfig, PortfolioBacktester  # noqa: E402
from twquant.loader import Bar  # noqa: E402
from twquant.report import text_summary, write_report  # noqa: E402
from twquant.strategies import TrendMA  # noqa: E402

TICKERS = ["DEMO-A", "DEMO-B", "DEMO-C", "DEMO-D"]
DAYS = 1200
SEED = 20261018


DAILY_VOL = 0.30 / math.sqrt(252)  # 年化波動 30%，接近台股大型股


def synthetic_bars(ticker: str, days: int, rng: random.Random, drift: float) -> list[Bar]:
    """合成日 K。刻意讓不同標的有不同漂移，這樣才有東西可以選。"""
    import datetime as dt

    price = 100.0
    start = dt.date(2019, 1, 2)
    bars: list[Bar] = []
    for i in range(days):
        d = start + dt.timedelta(days=i)
        if d.weekday() >= 5:
            continue
        shock = rng.gauss(0, DAILY_VOL)
        price *= math.exp(drift / 252 + shock)
        o = price * (1 + rng.gauss(0, 0.004))
        h = max(o, price) * (1 + abs(rng.gauss(0, 0.005)))
        lo = min(o, price) * (1 - abs(rng.gauss(0, 0.005)))
        vol = int(abs(rng.gauss(6_000_000, 1_500_000))) + 500_000
        bars.append(Bar(d.isoformat(), round(o, 2), round(h, 2), round(lo, 2), round(price, 2), vol))
    return bars


def main() -> int:
    drifts = {"DEMO-A": 0.18, "DEMO-B": 0.06, "DEMO-C": -0.04, "DEMO-D": 0.12}
    # 不用 hash()：PYTHONHASHSEED 每次都會變，demo 要能重現
    data = {t: synthetic_bars(t, DAYS, random.Random(SEED + i * 977), drifts[t]) for i, t in enumerate(TICKERS)}

    print(f"\n合成資料：{len(TICKERS)} 檔 x {len(data[TICKERS[0]])} 交易日")
    print("（DEMO-A / DEMO-D 是刻意做成上漲趨勢的，用來確認趨勢策略抓得到）")

    strat = TrendMA(fast=20, slow=60, max_positions=2)
    bt = PortfolioBacktester(1_000_000, CostConfig(), max_positions=2)
    res = bt.run(data, strat, benchmark="DEMO-A")

    print(text_summary(res))
    paths = write_report(res, "out", tag="demo")
    print(f"  報表 {paths['md']}")
    print(f"  圖表 {paths['svg']}")
    print("\n  要跑真實台股：python -m twquant fetch --start 2015-01-01 && python -m twquant backtest\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
