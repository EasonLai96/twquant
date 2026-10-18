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
# 這裡刻意「不」給 adj_returns，排除校正路徑
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

res = PortfolioBacktester(1_000_000, CostConfig(), max_positions=1).run({"ASSET": bars}, S(), benchmark=None)
t = res.trades[0]
by_date = {b.date: b for b in bars}
snaps = res.snapshots
print("=== A) 現金帳 ===")
cash_after_buy = next(s.cash for s in snaps if s.date == t.entry_date)
print(f"  買進後現金 {cash_after_buy:,.4f}  最終現金 {snaps[-1].cash:,.4f}")
cash_flow = snaps[-1].cash - 1_000_000
print(f"  現金流變化 {cash_flow:,.2f}   Trade.pnl {t.pnl:,.2f}   gap {cash_flow - t.pnl:,.2f}")

print("=== B) 持倉估值 ===")
worst = 0.0; worst_day = None
for s in snaps:
    if s.holdings <= 0: continue
    c = by_date[s.date].close
    implied = t.shares * c
    gap = s.holdings - implied
    if abs(gap) > abs(worst):
        worst, worst_day = gap, s.date
print(f"  holdings vs 股數*當日原始收盤 的最大差 {worst:,.2f}（{worst_day}）")

print("=== C) 手算現金流 ===")
px_buy = t.entry_price
px_sell = t.exit_price
fee = lambda g: max(g * 0.001425 * 0.6, 20.0)
cost_buy = t.shares * px_buy + fee(t.shares * px_buy)
proceeds = t.shares * px_sell - fee(t.shares * px_sell) - t.shares * px_sell * 0.003
print(f"  買進總付出 {cost_buy:,.4f}  賣出淨收入 {proceeds:,.4f}")
print(f"  手算損益 {proceeds - cost_buy:,.4f}")
print(f"  Trade.pnl {t.pnl:,.4f}  差 {proceeds - cost_buy - t.pnl:,.4f}")
print()
bar_in = by_date[t.entry_date]
print(f"  進場日 open={bar_in.open:.4f} close={bar_in.close:.4f}  entry_price 應等於 open*1.0005={bar_in.open*1.0005:.4f}")
