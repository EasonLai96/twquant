import sys, random, datetime as dt
sys.path.insert(0, ".")
from twquant.engine import CostConfig, PortfolioBacktester
from twquant.loader import Bar
from twquant.strategy import StrategyResponse, OrderIntent

def mk(prices, vol=5_000_000):
    base = dt.date(2024,1,1)
    return [Bar((base+dt.timedelta(days=i)).isoformat(), p, p*1.005, p*0.995, p, vol) for i,p in enumerate(prices)]

rng = random.Random(7)
prices = [100.0]
for _ in range(399):
    prices.append(max(5.0, prices[-1] * (1 + rng.gauss(0.0006, 0.02))))
bars = mk(prices)
adj = {"ASSET": {}}
for i in range(1, len(bars)):
    adj["ASSET"][bars[i].date] = bars[i].close/bars[i-1].close - 1

seen = {280: "buy", 380: "sell"}
class S:
    name="s"
    def params(self): return {}
    def on_bar(self, ctx):
        lb = max([i for i,k in seen.items() if k=="buy" and i<=ctx.idx], default=None)
        ls = max([i for i,k in seen.items() if k=="sell" and i<=ctx.idx], default=None)
        if ls is not None and (lb is None or ls > lb):
            return StrategyResponse(orders=[OrderIntent("ASSET","sell","x")], allocation={})
        if lb is not None:
            return StrategyResponse(allocation={"ASSET": 0.3})
        return StrategyResponse()

import twquant.engine as E
trace = []
orig_run = E.PortfolioBacktester.run
res = PortfolioBacktester(1_000_000, CostConfig(), max_positions=1).run({"ASSET": bars}, S(), benchmark=None, adj_returns=adj)
t = res.trades[0]
print(f"trade: entry {t.entry_date} @ {t.entry_price:.4f}  exit {t.exit_date} @ {t.exit_price:.4f} sh={t.shares}")
print(f"買進淨收入/付出 => pnl {t.pnl:,.2f}")
print()
byd = {b.date: b for b in bars}
i0 = [i for i,b in enumerate(bars) if b.date == t.entry_date][0]
print("進場後 12 天的 basis 演算（模擬引擎邏輯）：")
basis = 1.0
last_close = bars[i0].close
for k in range(i0, i0+13):
    b = bars[k]
    r = adj["ASSET"].get(b.date)
    if r is None or b.close <= 0:
        last_close = b.close
        print(f"  {b.date} r=None -> last={last_close:.4f}")
        continue
    implied = last_close * (1 + r)
    raw_now = b.close * basis
    ratio = implied / raw_now if raw_now > 0 else 1.0
    flag = ""
    if abs(ratio - 1.0) > 1e-9:
        basis *= ratio
        raw_now = b.close * basis
        flag = f"  <== 觸發換算 ratio={ratio:.10f}  basis={basis:.10f}"
    last_close = raw_now
    print(f"  {b.date} close={b.close:.4f} r={r:+.8f} implied={implied:.4f} raw_now={raw_now:.4f} ratio={ratio:.10f}{flag}")
