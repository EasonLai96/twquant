"""台股回測引擎。

台股摩擦全部建模進去：手續費（含電子折扣）、證交稅、滑價、漲跌停無法成交、整股交易。
訊號 T 日收盤產生，T+1 開盤成交 —— 沒有未來函數。
"""
from __future__ import annotations

import datetime as dt
import math
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field

from .loader import Bar
from .strategy import MarketContext, PositionView, Strategy

# 台股法規：賣出證交稅 0.3%；券商手續費公定價 0.1425%
TAX_SELL = 0.003
FEE_RATE = 0.001425
FEE_MIN = 20.0
LOT = 1000


@dataclass
class CostConfig:
    fee_rate: float = FEE_RATE
    fee_discount: float = 0.6  # 電子下單常見 6 折
    fee_min: float = FEE_MIN
    tax_sell: float = TAX_SELL
    slippage_bps: float = 5.0  # 開盤市價單抓 5bp
    limit_move: float = 0.095  # 漲跌停判定門檻
    pin_tol: float = 0.01  # 開盤價要貼在當日極值多少 % 內才算「鎖住」
    lot: int = LOT
    min_volume: int = 2_000_000  # 20 日均量門檻，避免買到冷門股


@dataclass
class Position:
    ticker: str
    shares: int
    entry_date: str
    last_close: float
    # 現金成本（含買進手續費）。這是損益的唯一權威來源 —— 現金流出多少就是多少。
    # 分割/減資不改變這個數字（你付出的現金沒變），改的是股數。
    cash_cost: float

    @property
    def cost_per_share(self) -> float:
        """每股綜合成本（含費），給報表看的。分割後股數變多，這個數字會跟著降。"""
        return self.cash_cost / self.shares if self.shares else 0.0


def _finite(x: float) -> bool:
    return x == x and x not in (float("inf"), float("-inf"))


def is_rebase_date(adj_returns, ticker: str, date: str, prev_close: float, close: float) -> bool:
    """這一天是不是「重新定價日」（分割/減資/除權息）。

    判定：adj_returns 明確標記為 0（loader 的結構性斷點訊號），
    但原始價格其實有明顯變動 —— 那就代表價格被換了基準，而不是真的漲跌停。
    沒有 adj_returns 時回 False（合成資料或非 FinMind 來源不走這條路）。
    """
    if adj_returns is None:
        return False
    r = adj_returns.get(ticker, {}).get(date)
    if r is None or r != 0.0:
        return False
    if prev_close <= 0 or close <= 0:
        return False
    return abs(prev_close / close - 1.0) > REBASE_TRIGGER


REBASE_TRIGGER = 0.15


@dataclass
class Trade:
    ticker: str
    entry_date: str
    exit_date: str
    entry_price: float
    exit_price: float
    shares: int
    pnl: float
    ret: float
    days: int
    reason: str


@dataclass
class Snapshot:
    date: str
    cash: float
    holdings: float
    equity: float
    ret: float
    exposure: float


@dataclass
class CostLedger:
    fee: float = 0.0
    tax: float = 0.0
    slippage: float = 0.0

    @property
    def total(self) -> float:
        return self.fee + self.tax + self.slippage


@dataclass
class BacktestResult:
    config: dict
    snapshots: list[Snapshot]
    trades: list[Trade]
    costs: CostLedger
    metrics: dict = field(default_factory=dict)
    benchmark_curve: list[tuple[str, float]] = field(default_factory=list)
    benchmark_metrics: dict = field(default_factory=dict)
    benchmark_label: str = ""

    def equity_curve(self) -> list[tuple[str, float]]:
        return [(s.date, s.equity) for s in self.snapshots]

    def as_json(self) -> dict:
        return {
            "config": self.config,
            "metrics": self.metrics,
            "benchmark_label": self.benchmark_label,
            "benchmark_metrics": self.benchmark_metrics,
            "costs": {
                "fee": round(self.costs.fee, 2),
                "tax": round(self.costs.tax, 2),
                "slippage": round(self.costs.slippage, 2),
                "total": round(self.costs.total, 2),
            },
            "curve": self.equity_curve(),
            "benchmark_curve": self.benchmark_curve,
            "trades": [t.__dict__ for t in self.trades],
        }


class PortfolioBacktester:
    def __init__(
        self,
        capital: float = 1_000_000.0,
        cost: CostConfig | None = None,
        max_positions: int = 5,
        cash_buffer: float = 0.03,
    ):
        self.capital = capital
        self.cost = cost or CostConfig()
        self.max_positions = max_positions
        self.cash_buffer = cash_buffer

    # --- 費用 -----------------------------------------------------------------

    def _fee(self, gross: float) -> float:
        return max(gross * self.cost.fee_rate * self.cost.fee_discount, self.cost.fee_min)

    def _buy_price(self, px: float) -> float:
        return px * (1 + self.cost.slippage_bps / 10_000)

    def _sell_price(self, px: float) -> float:
        return px * (1 - self.cost.slippage_bps / 10_000)

    # --- 主流程 ---------------------------------------------------------------

    def run(
        self,
        data: dict[str, list[Bar]],
        strategy: Strategy,
        start: str | None = None,
        end: str | None = None,
        benchmark: str | None = "0050",
        progress: bool = False,
        adj_returns: dict[str, dict[str, float]] | None = None,
        benchmark_data: dict[str, list[Bar]] | None = None,
    ) -> BacktestResult:
        dates = self._trading_dates(data, start, end)
        if len(dates) < 40:
            raise ValueError(f"交易日太少（{len(dates)}），把區間拉長或檢查資料是否抓到")

        bars_of: dict[str, list[Bar]] = {t: bars for t, bars in data.items()}
        by_date: dict[str, dict[str, Bar]] = {
            t: {b.date: b for b in bars} for t, bars in data.items()
        }
        idx_of: dict[str, dict[str, int]] = {
            t: {b.date: i for i, b in enumerate(bars)} for t, bars in data.items()
        }

        cash = self.capital
        positions: dict[str, Position] = {}
        trades: list[Trade] = []
        snaps: list[Snapshot] = []
        costs = CostLedger()
        pending: list = []
        min_vol: dict[str, float] = {}

        prev_equity = self.capital
        for di, d in enumerate(dates):
            today = {t: by_date[t][d] for t in by_date if d in by_date[t]}
            if today:
                for t, b in today.items():
                    v = min_vol.get(t)
                    min_vol[t] = b.volume if v is None else v * 0.95 + b.volume * 0.05

            # 1) 先成交昨天收盤後排的單（今日開盤價）
            if pending:
                cash = self._execute_orders(pending, positions, today, bars_of, cash, trades, costs, adj_returns)
                pending = []

            # 2) 收盤後評價
            #
            # 全部留在「原始價格座標」：last_close = 當日原始收盤，市值 = 股數 × 原始價。
            # 分割/減資（價格被重新定價）不換算價格，而是換算「股數」——
            # 4:1 分割就是把 4000 股變成 16000 股，現金成本不變（你付出的現金沒變）。
            #
            # 這樣做的理由：換算價格會讓「市值座標」與「賣出收入座標」分家，
            # 平倉時就會出現假虧損或假獲利，而 _plan 用原始價算部位市值、卻和
            # 換算過的 equity 相比，還會誤加碼把權益灌爆。
            # 換算股數則讓市值、賣出、損益、配置全部同一個座標，不需要任何特例。
            holdings_val = 0.0
            for p in positions.values():
                b = today.get(p.ticker)
                if b:
                    prev_close = self._prev_bar(bars_of[p.ticker], d)
                    if prev_close is not None and is_rebase_date(adj_returns, p.ticker, d, prev_close.close, b.close):
                        k = prev_close.close / b.close
                        if _finite(k) and k > 0 and abs(k - 1.0) > 1e-9:
                            # 股數換算，現金成本不動。畸零股（分割比例不是整數時必然出現）
                            # 留給後續平倉的 frac 分攤吸收，不會漏價值。
                            p.shares = int(round(p.shares * k))
                    p.last_close = b.close
                holdings_val += p.shares * p.last_close
            equity = cash + holdings_val
            ret = (equity / prev_equity - 1) if prev_equity > 0 else 0.0
            snaps.append(
                Snapshot(d, cash, holdings_val, equity, ret, (holdings_val / equity) if equity > 0 else 0.0)
            )
            prev_equity = equity

            # 3) 產生隔日訂單
            if di < len(dates) - 1:
                ctx = self._context(d, di, data, positions, equity, cash, bars_of, idx_of)
                resp = strategy.on_bar(ctx)
                pending = self._plan(resp, positions, today, equity, cash, min_vol, bars_of, idx_of, d, di)
                if progress and di % 100 == 0:
                    print(f"  {d}  equity={equity:,.0f}  pos={len(positions)}")

        result = BacktestResult(
            config={
                "capital": self.capital,
                "max_positions": self.max_positions,
                "cash_buffer": self.cash_buffer,
                "cost": self.cost.__dict__,
                "strategy": strategy.name,
                "strategy_params": strategy.params(),
                "universe": sorted(data.keys()),
                "start": dates[0],
                "end": dates[-1],
                "bars": len(dates),
            },
            snapshots=snaps,
            trades=trades,
            costs=costs,
        )
        result.metrics = compute_metrics(snaps, trades)
        # 基準可能不在交易宇宙裡（例如 0050 被刻意排除），所以允許另外給一份資料
        self._add_benchmark(result, benchmark_data or data, dates, benchmark, adj_returns)
        return result

    # --- 訂單規劃 -------------------------------------------------------------

    def _plan(
        self,
        resp,
        positions: dict[str, Position],
        today: dict[str, Bar],
        equity: float,
        cash: float,
        min_vol: dict[str, float],
        bars_of: dict[str, dict[str, Bar]],
        idx_of: dict[str, dict[str, int]],
        date: str,
        day_no: int,
    ) -> list[dict]:
        orders: list[dict] = []
        intents = {o.ticker: o for o in resp.orders}
        sells = {t for t, o in intents.items() if o.action == "sell"}
        keep = set(resp.allocation) | sells

        # 清倉：持有但沒被列入 allocation。這輪已經處理過的 ticker 不再進第二次，
        # 否則「清倉 + 策略明確賣出」會產生兩筆同樣的賣單（實單會變成重複委託）
        handled: set[str] = set()
        for t, p in positions.items():
            if t not in keep and p.shares > 0:
                orders.append({"side": "sell", "ticker": t, "shares": p.shares, "reason": "rebalance"})
                handled.add(t)
        for t in sells:
            if t in handled:
                continue
            p = positions.get(t)
            if not p or p.shares <= 0:
                continue
            want = intents[t].shares
            if want is None:
                want = p.shares
            # 部分出場要湊整張，湊不出來就清掉，避免留下無法處理的零股
            shares = min(p.shares, (max(want, 0) // self.cost.lot) * self.cost.lot)
            if shares < self.cost.lot:
                shares = p.shares
            orders.append(
                {"side": "sell", "ticker": t, "shares": shares, "reason": intents[t].reason or "signal"}
            )

        free = max(self.max_positions - len([t for t in positions if t not in sells]), 0)
        targets = {t: w for t, w in resp.allocation.items() if w > 0}
        for t, w in targets.items():
            bars = bars_of.get(t)
            if not bars or date not in (idx_of.get(t) or {}):
                continue
            i = (idx_of.get(t) or {})[date]
            if day_no < 20:
                continue
            if min_vol.get(t, 0) < self.cost.min_volume:
                continue
            prev = bars_of[t][i]
            if prev.close <= 0:
                continue
            held = positions[t].shares if t in positions else 0
            budget = equity * w
            if budget <= 0:
                continue
            if held > 0:
                current = held * prev.close
                if prev.close <= 0:
                    continue
                delta = int((budget - current) / prev.close / self.cost.lot) * self.cost.lot
                if delta >= self.cost.lot:
                    orders.append({"side": "buy", "ticker": t, "shares": delta, "reason": "add"})
            else:
                if free <= 0:
                    continue
                if prev.close * self.cost.lot > cash * (1 - self.cash_buffer):
                    continue
                lots = int(budget / (prev.close * self.cost.lot))
                lots = min(lots, int(cash * (1 - self.cash_buffer) / (prev.close * self.cost.lot)))
                if lots >= 1:
                    orders.append({"side": "buy", "ticker": t, "shares": lots * self.cost.lot, "reason": "entry"})
                    free -= 1
        return orders

    # --- 成交 -----------------------------------------------------------------

    def _execute_orders(self, orders, positions, today, bars_of, cash, trades, costs, adj_returns=None) -> float:
        execs: list[dict] = []
        for o in orders:
            t = o["ticker"]
            b = today.get(t)
            bars = bars_of.get(t)
            if b is None or not bars:
                continue  # 停牌或無資料
            prev = self._prev_bar(bars, b.date)
            if prev is None or prev.close <= 0:
                continue
            r = (adj_returns or {}).get(t, {}).get(b.date)
            is_rebase = is_rebase_date(adj_returns, t, b.date, prev.close, b.close)
            can, _ = self._tradable(b, prev.close, o["side"], is_rebase)
            if not can:
                continue
            execs.append({**o, "bar": b})

        for o in execs:
            t, b = o["ticker"], o["bar"]
            if o["side"] == "sell":
                p = positions.get(t)
                if p is None or p.shares <= 0:
                    continue
                sold = min(int(o["shares"]), p.shares)
                if sold <= 0:
                    continue
                px = self._sell_price(b.open)
                gross = px * sold
                fee = self._fee(gross)
                tax = gross * self.cost.tax_sell
                cash += gross - fee - tax
                costs.fee += fee
                costs.tax += tax
                costs.slippage += (b.open - px) * sold
                # 損益直接用現金算：賣出淨收入 − 這批股數分攤到的現金成本。
                # 這條等式保證 Σtrade.pnl 與權益曲線的變化完全一致（部分出場也成立）。
                cost_share = p.cash_cost * (sold / p.shares)
                pnl = gross - fee - tax - cost_share
                base = p.cash_cost / p.shares
                avg_cost = p.cost_per_share
                trades.append(
                    Trade(
                        t,
                        p.entry_date,
                        b.date,
                        round(avg_cost, 4),
                        round(px, 4),
                        sold,
                        round(pnl, 2),
                        round(pnl / (cost_share or 1.0), 5),
                        self._days_between(p.entry_date, b.date),
                        o.get("reason", ""),
                    )
                )
                if sold >= p.shares:
                    positions.pop(t, None)
                else:
                    p.shares -= sold
                    p.cash_cost -= cost_share
            else:
                px = self._buy_price(b.open)
                shares = int(o["shares"])
                gross = px * shares
                fee = self._fee(gross)
                if gross + fee > cash:
                    continue
                cash -= gross + fee
                costs.fee += fee
                costs.slippage += (px - b.open) * shares
                pos = positions.get(t)
                if pos is not None and pos.shares > 0:
                    pos.shares += shares
                    pos.cash_cost += gross + fee
                    pos.last_close = px
                else:
                    # 新建部位：last_close 從成交價起算，當天不套用報酬，
                    # 否則開盤買進會被套上「昨收至今收」的價差
                    positions[t] = Position(t, shares, b.date, px, gross + fee)
        return cash

    def _tradable(self, bar: Bar, prev_close: float, side: str, is_rebase: bool = False) -> tuple[bool, str]:
        """判斷這根 bar 的開盤價能不能真的成交。

        台股漲跌停是「鎖住」而不是「跌了 10%」：只要盤中有打開，開盤價就成交得到。
        所以判定條件是 gap 夠大 + 開盤價貼在當日極值 + 當日振幅几乎為零。
        這樣才不會把除權息的跳空、或是開低走高真的跌停打開誤判成不能交易。

        is_rebase：這天是分割/減資的重新定價日。那天的 gap 必然很大（4:1 就是 -75%），
        但 ±10% 漲跌幅限制的推論在重新定價日不成立，直接跳過判定。
        """
        if bar.volume <= 0:
            return False, "no-trade"
        if prev_close <= 0 or bar.open <= 0:
            return False, "bad-ref"
        if is_rebase:
            return True, ""
        gap = bar.open / prev_close - 1
        pinned = (bar.high - bar.low) / bar.open <= 0.002 if bar.open > 0 else True
        if side == "buy":
            at_high = bar.open >= bar.high * (1 - self.cost.pin_tol)
            if gap >= self.cost.limit_move and at_high and pinned:
                return False, "limit-up"
        else:
            at_low = bar.open <= bar.low * (1 + self.cost.pin_tol)
            if gap <= -self.cost.limit_move and at_low and pinned:
                return False, "limit-down"
        return True, ""

    @staticmethod
    def _prev_bar(bars: list[Bar], date: str) -> Bar | None:
        """嚴格早於 date 的最後一根 bar。用 bisect_left：bisect_right 在日期完全相同時
        會回同一根，漲跌停的 gap 就會永遠算成 0。"""
        i = bisect_left([b.date for b in bars], date) - 1
        return bars[i] if i >= 0 else None

    @staticmethod
    def _days_between(a: str, b: str) -> int:
        return (dt.date.fromisoformat(b) - dt.date.fromisoformat(a)).days

    # --- 上下文 ---------------------------------------------------------------

    def _context(self, date, di, data, positions, equity, cash, bars_of, idx_of) -> MarketContext:
        views = {}
        holding_days = {}
        for t, p in positions.items():
            views[t] = PositionView(
                t, p.shares, p.cost_per_share, p.entry_date, p.last_close,
                (p.last_close - p.cost_per_share) * p.shares,
            )
            i = idx_of.get(t, {}).get(date)
            holding_days[t] = i if i is not None else 0
        return MarketContext(date, di, bars_of, views, equity, cash, holding_days)

    @staticmethod
    def _trading_dates(data, start, end) -> list[str]:
        s = set()
        for bars in data.values():
            s.update(b.date for b in bars)
        ds = sorted(d for d in s if (start is None or d >= start) and (end is None or d <= end))
        return ds

    # --- 基準 -----------------------------------------------------------------

    def _add_benchmark(self, result: BacktestResult, data, dates, benchmark, adj_returns=None) -> None:
        curve, label = self._bh_curve(data, dates, benchmark, adj_returns)
        result.benchmark_label = label
        if not curve:
            return
        result.benchmark_curve = curve
        eq = [v for _, v in curve]
        rets = [eq[i] / eq[i - 1] - 1 for i in range(1, len(eq)) if eq[i - 1] > 0]
        result.benchmark_metrics = {
            "total_return": eq[-1] / eq[0] - 1,
            "cagr": _cagr(eq[0], eq[-1], len(eq)),
            "max_drawdown": _max_dd(eq)[0],
            "vol": _ann_vol(rets),
            "sharpe": _sharpe(rets),
        }

    def _bh_curve(self, data, dates, benchmark, adj_returns=None) -> tuple[list[tuple[str, float]], str]:
        """買進持有基準曲線。回傳 (曲線, 這個曲線到底是什麼)。

        標籤一定要跟著回傳，不能沿用「使用者要求的 benchmark」——
        指定標的沒抓到時會降級成等權基準，那時候報表上寫「buy&hold 0050」就是在說謊。

        用「日報酬連乘」而不是價格相對第一天的比值。差別在減資/分割：
        價格法會把一次 4:1 分割當成 -75% 的永久虧損，日報酬法只影響那一天。
        （0050 在 2025-06-18 做過 4:1 分割，就是這條路徑最容易踩到的雷。）
        """
        if benchmark and benchmark in data:
            picked = {benchmark: 1.0}
            label = f"buy&hold {benchmark}"
        else:
            picks = [t for t in data if data[t]]
            if not picks:
                return [], "無基準"
            w = 1.0 / len(picks)
            picked = {t: w for t in picks}
            label = f"等權買進持有（{len(picks)} 檔）"

        maps = {t: {b.date: b for b in data[t]} for t in picked}
        adj = adj_returns or {}
        equity = self.capital
        out: list[tuple[str, float]] = []
        prev: dict[str, float] = {}
        started = False
        for d in dates:
            r = 0.0
            day_ok = False
            for t, w in picked.items():
                b = maps[t].get(d)
                if b is None or b.close <= 0:
                    continue
                day_ok = True
                p = prev.get(t)
                if p is not None and p > 0:
                    a = adj.get(t, {}).get(d)
                    r += w * (a if a is not None else b.close / p - 1)
                prev[t] = b.close
            if not day_ok:
                continue
            if not started:
                started = True
                out.append((d, equity))
                continue
            equity *= 1 + r
            out.append((d, equity))
        return out, label


# --- 績效指標 -----------------------------------------------------------------

def _cagr(v0: float, v1: float, days: int) -> float:
    if v0 <= 0 or days <= 0:
        return 0.0
    years = days / 365.25
    return (v1 / v0) ** (1 / years) - 1 if years > 0 else 0.0


def _max_dd(eq: list[float]) -> tuple[float, int, int]:
    peak = eq[0]
    peak_i = 0
    worst = 0.0
    wi = wj = 0
    for i, v in enumerate(eq):
        if v > peak:
            peak, peak_i = v, i
        dd = v / peak - 1 if peak > 0 else 0.0
        if dd < worst:
            worst, wi, wj = dd, peak_i, i
    return worst, wi, wj


def _ann_vol(rets: list[float]) -> float:
    n = len(rets)
    if n < 2:
        return 0.0
    m = sum(rets) / n
    var = sum((r - m) ** 2 for r in rets) / (n - 1)
    return math.sqrt(var) * math.sqrt(252)


def _sharpe(rets: list[float], rf: float = 0.02) -> float:
    vol = _ann_vol(rets)
    if vol <= 0:
        return 0.0
    n = len(rets)
    ann = (math.prod(1 + r for r in rets) ** (252 / n) - 1) if n else 0.0
    return (ann - rf) / vol


def compute_metrics(snaps: list[Snapshot], trades: list[Trade], rf: float = 0.02) -> dict:
    if not snaps:
        return {}
    eq = [s.equity for s in snaps]
    rets = [s.ret for s in snaps]
    dd, pi, di = _max_dd(eq)
    days = len(snaps)
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    gp = sum(t.pnl for t in wins)
    gl = abs(sum(t.pnl for t in losses))
    span_days = max(1, (dt.date.fromisoformat(snaps[-1].date) - dt.date.fromisoformat(snaps[0].date)).days)
    cagr = _cagr(eq[0], eq[-1], span_days)
    return {
        "start": snaps[0].date,
        "end": snaps[-1].date,
        "trading_days": days,
        "calendar_days": span_days,
        "initial": eq[0],
        "final": eq[-1],
        "total_return": eq[-1] / eq[0] - 1,
        "cagr": cagr,
        "max_drawdown": dd,
        "mdd_peak_date": snaps[pi].date,
        "mdd_trough_date": snaps[di].date,
        "vol": _ann_vol(rets),
        "sharpe": _sharpe(rets, rf),
        "calmar": (cagr / abs(dd)) if dd < 0 else 0.0,
        "avg_exposure": sum(s.exposure for s in snaps) / days,
        "trades": len(trades),
        "win_rate": len(wins) / len(trades) if trades else 0.0,
        "profit_factor": (gp / gl) if gl > 0 else (float("inf") if gp > 0 else 0.0),
        "avg_win": (gp / len(wins)) if wins else 0.0,
        "avg_loss": (-gl / len(losses)) if losses else 0.0,
        "avg_hold_days": (sum(t.days for t in trades) / len(trades)) if trades else 0.0,
    }
