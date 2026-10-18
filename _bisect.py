import sys, random, datetime as dt
sys.path.insert(0, ".")
from twquant.engine import CostConfig, PortfolioBacktester
from twquant.loader import Bar
from twquant.strategy import StrategyResponse, OrderIntent
import twquant.engine as E

def mk(prices, vol=5_000_000):
    base = dt.date(2024,1,1)
    return [Bar((base+dt.timedelta(days=i)).isoformat(), p, p*1.005, p*0.995, p, vol) for i,p in enumerate(prices)]

# 先找出一個最小失敗情境（無分割）
rng = random.Random(7)
for trial in range(200):
    n = 400
    prices = [100.0]
    for _ in range(n-1):
        prices.append(max(5.0, prices[-1] * (1 + rng.gauss(0.0006, 0.02))))
    bars = mk(prices)
    adj = {"ASSET": {}}
    for i in range(1, len(bars)):
        adj["ASSET"][bars[i].date] = bars[i].close/bars[i-1].close - 1
    events = {}
    for _ in range(rng.randint(2, 4)):
        events.setdefault(rng.randint(60, 380), rng.choice(["buy","sell"]))
    plan = []
    seen = {}
    for i in sorted(events):
        seen[i] = events[i]
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
    res = PortfolioBacktester(1_000_000, CostConfig(), max_positions=1).run(
        {"ASSET": bars}, S(), benchmark=None, adj_returns=adj)
    if res.snapshots[-1].holdings != 0:
        continue
    delta = res.metrics["final"] - res.metrics["initial"]
    pnl = sum(t.pnl for t in res.trades)
    if abs(pnl - delta) > 3:
        print(f"trial {trial}: events={sorted(seen.items())} gap={pnl-delta:,.2f}")
        print(f"  trades={len(res.trades)} delta={delta:,.2f} pnl={pnl:,.2f}")
        for t in res.trades:
            print(f"    {t.entry_date}->{t.exit_date} sh={t.shares} entry={t.entry_price:.4f} exit={t.exit_price:.4f} pnl={t.pnl:,.2f} ret={t.ret}")
        break
else:
    print("無分割情境：200 組都沒重現")
