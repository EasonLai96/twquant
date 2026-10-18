import sys, random, datetime as dt
sys.path.insert(0, ".")
from twquant.engine import CostConfig, PortfolioBacktester
from twquant.loader import Bar
from twquant.strategy import StrategyResponse, OrderIntent

def mk(prices, vol=5_000_000):
    base = dt.date(2024,1,1)
    return [Bar((base+dt.timedelta(days=i)).isoformat(), p, p*1.005, p*0.995, p, vol) for i,p in enumerate(prices)]

rng = random.Random(7)
n = 400
prices = [100.0]
for _ in range(n-1):
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

res = PortfolioBacktester(1_000_000, CostConfig(), max_positions=1).run({"ASSET": bars}, S(), benchmark=None, adj_returns=adj)
t = res.trades[0]
print(f"進場 {t.entry_date} 出場 {t.exit_date} 股數 {t.shares}")
print(f"entry_price {t.entry_price:.6f}  exit_price {t.exit_price:.6f}")
print(f"原始收盤 進場日 {[b.close for b in bars if b.date==t.entry_date][0]:.6f}  出場日 {[b.close for b in bars if b.date==t.exit_date][0]:.6f}")
print(f"pnl {t.pnl:,.2f}   delta {res.metrics['final']-res.metrics['initial']:,.2f}   gap {t.pnl-(res.metrics['final']-res.metrics['initial']):,.2f}")
print()
# 逐日權益 vs 現金+持倉，找出會計缺口
print("權益恆等式（現金 + 持倉市值 = 權益）檢查：")
bad = [s for s in res.snapshots if abs(s.cash + s.holdings - s.equity) > 0.01]
print(f"  違反筆數 {len(bad)} / {len(res.snapshots)}")
print()
snaps = res.snapshots
i_in  = next(i for i,s in enumerate(snaps) if s.date == t.entry_date)
i_out = next(i for i,s in enumerate(snaps) if s.date == t.exit_date)
print("進場當天前後：")
for i in range(i_in-1, i_in+2):
    s = snaps[i]
    print(f"  {s.date} cash={s.cash:,.2f} holdings={s.holdings:,.2f} equity={s.equity:,.2f} ret={s.ret*100:+.4f}%")
print("出場當天前後：")
for i in range(i_out-1, min(i_out+2, len(snaps))):
    s = snaps[i]
    print(f"  {s.date} cash={s.cash:,.2f} holdings={s.holdings:,.2f} equity={s.equity:,.2f} ret={s.ret*100:+.4f}%")
print()
# 用報酬連乘重建權益，跟 equity 對照
eq = 1_000_000.0
chk = None
for s in snaps:
    eq *= (1 + s.ret)
    if chk is None:
        chk = eq
print(f"用 ret 連乘重建的期末：{eq:,.2f}   引擎期末：{snaps[-1].equity:,.2f}   差異 {eq-snaps[-1].equity:,.2f}")
print(f"總報酬（引擎）{(snaps[-1].equity/1_000_000-1)*100:+.4f}%   （ret 連乘）{(eq/1_000_000-1)*100:+.4f}%")
