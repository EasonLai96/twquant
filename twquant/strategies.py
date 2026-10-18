"""內建策略。

兩支都不預測方向，只做「趨勢跟隨 + 風險控制」——這類策略在台股的真實 edge 很薄，
重點是讓你把成本、滑價、停損接上去後看到誠實的權益曲線，而不是看到一個漂亮數字。
"""
from __future__ import annotations

import datetime as dt
from bisect import bisect_right

from .loader import Bar, atr, rolling_high, sma
from .strategy import MarketContext, OrderIntent, StrategyResponse


def _idx(bars: list[Bar], date: str) -> int:
    """回傳 <= date 的最後一根 bar 的 index（日期為 ISO 字串，可直接比大小）。"""
    i = bisect_right(_dates(bars), date)
    return max(0, i - 1)


def _dates(bars: list[Bar]) -> list[str]:
    cached = getattr(bars, "_twq_dates", None)
    if cached is None:
        cached = [b.date for b in bars]
        try:
            bars._twq_dates = cached  # list 可以掛屬性，省下每次重建
        except AttributeError:
            pass
    return cached


def _hold_days(ctx: MarketContext, t: str) -> int:
    pos = ctx.positions.get(t)
    if pos is None:
        return 0
    return (dt.date.fromisoformat(ctx.date) - dt.date.fromisoformat(pos.entry_date)).days


def _ma(bars: list[Bar], n: int) -> list[float | None]:
    return sma([b.close for b in bars], n)


def _hi(bars: list[Bar], n: int) -> list[float | None]:
    return rolling_high([b.high for b in bars], n)


class TrendMA:
    """雙均線趨勢：快線上穿慢線進場，跌破快線 / 慢線下彎 / ATR 移動停損出場。

    fast/slow 均線週期，max_weight 單一標的資金上限，
    confirm_days 最短持有天數（台股隔日沖成本很高，這條擋掉雜訊進出），
    trail_atr ATR 移動停損倍數，0 表示關閉。
    """

    name = "trend_ma"

    def __init__(
        self,
        fast: int = 20,
        slow: int = 60,
        atr_n: int = 14,
        max_weight: float = 0.25,
        confirm_days: int = 3,
        trail_atr: float = 3.0,
        max_positions: int = 5,
    ):
        self.fast = int(fast)
        self.slow = int(slow)
        self.atr_n = int(atr_n)
        self.max_weight = float(max_weight)
        self.confirm_days = int(confirm_days)
        self.trail_atr = float(trail_atr)
        self.max_positions = int(max_positions)
        if self.fast >= self.slow:
            raise ValueError("fast 必須小於 slow")
        self._peak: dict[str, float] = {}

    def params(self) -> dict:
        return {
            "fast": self.fast,
            "slow": self.slow,
            "atr_n": self.atr_n,
            "max_weight": self.max_weight,
            "confirm_days": self.confirm_days,
            "trail_atr": self.trail_atr,
            "max_positions": self.max_positions,
        }

    def on_bar(self, ctx: MarketContext) -> StrategyResponse:
        orders: list[OrderIntent] = []
        alloc: dict[str, float] = {}
        today = set(ctx.bars)

        for t in list(ctx.positions):
            bars = ctx.bars.get(t)
            if not bars:
                continue
            i = _idx(bars, ctx.date)
            if i < self.slow:
                alloc[t] = min(self.max_weight, 0.2)
                continue
            f = ctx.series(t, f"ma{self.fast}", lambda b, n=self.fast: _ma(b, n))
            s = ctx.series(t, f"ma{self.slow}", lambda b, n=self.slow: _ma(b, n))
            last = bars[i].close
            ma_f, ma_s = f[i], s[i]
            reason = None
            if ma_f is None or ma_s is None:
                reason = "insufficient_history"
            elif last < ma_f:
                reason = "below_fast_ma"
            elif ma_s < s[i - 1] and last < ma_s:
                reason = "slow_turned_down"
            else:
                peak = max(self._peak.get(t, last), last)
                self._peak[t] = peak
                if self.trail_atr > 0:
                    a = ctx.series(t, f"atr{self.atr_n}", lambda b, n=self.atr_n: atr(b, n))
                    if a[i] and last < peak - self.trail_atr * a[i]:
                        reason = "trail_stop"

            if reason and _hold_days(ctx, t) >= self.confirm_days:
                orders.append(OrderIntent(t, "sell", reason))
                self._peak.pop(t, None)
            else:
                alloc[t] = min(self.max_weight, 0.2)

        picks: list[tuple[float, str]] = []
        for t, bars in ctx.bars.items():
            if t in ctx.positions or t not in today:
                continue
            i = _idx(bars, ctx.date)
            if bars[i].date != ctx.date or i < self.slow + 1:
                continue
            f = ctx.series(t, f"ma{self.fast}", lambda b, n=self.fast: _ma(b, n))
            s = ctx.series(t, f"ma{self.slow}", lambda b, n=self.slow: _ma(b, n))
            if None in (f[i], s[i], f[i - 1], s[i - 1]):
                continue
            if not (f[i - 1] <= s[i - 1] and f[i] > s[i]):
                continue
            if s[i] < s[i - 1]:
                continue
            last = bars[i].close
            if last <= 0:
                continue
            picks.append((last / s[i] - 1, t))

        for _, t in sorted(picks, reverse=True)[: self.max_positions]:
            alloc[t] = self.max_weight

        if sum(alloc.values()) > 1.0:
            k = 1.0 / sum(alloc.values())
            alloc = {t: w * k for t, w in alloc.items()}
        return StrategyResponse(orders=orders, allocation=alloc)


class Breakout20:
    """動能突破：突破 lookback 日高點且量能放大 vol_mult 倍進場，
    跌破 exit_ma 或持有滿 max_hold 個交易日出場。
    """

    name = "breakout"

    def __init__(
        self,
        lookback: int = 20,
        vol_mult: float = 1.5,
        max_hold: int = 40,
        max_weight: float = 0.25,
        exit_ma: int = 20,
        max_positions: int = 5,
    ):
        self.lookback = int(lookback)
        self.vol_mult = float(vol_mult)
        self.max_hold = int(max_hold)
        self.max_weight = float(max_weight)
        self.exit_ma = int(exit_ma)
        self.max_positions = int(max_positions)

    def params(self) -> dict:
        return {
            "lookback": self.lookback,
            "vol_mult": self.vol_mult,
            "max_hold": self.max_hold,
            "max_weight": self.max_weight,
            "exit_ma": self.exit_ma,
            "max_positions": self.max_positions,
        }

    def on_bar(self, ctx: MarketContext) -> StrategyResponse:
        orders: list[OrderIntent] = []
        alloc: dict[str, float] = {}
        today = set(ctx.bars)

        for t in list(ctx.positions):
            bars = ctx.bars.get(t)
            if not bars:
                continue
            i = _idx(bars, ctx.date)
            if i < self.lookback + 1:
                alloc[t] = self.max_weight
                continue
            ma = ctx.series(t, f"ma{self.exit_ma}", lambda b, n=self.exit_ma: _ma(b, n))
            last = bars[i].close
            if _hold_days(ctx, t) >= self.max_hold:
                orders.append(OrderIntent(t, "sell", "max_hold"))
            elif ma[i] is not None and last < ma[i]:
                orders.append(OrderIntent(t, "sell", "below_ma"))
            else:
                alloc[t] = self.max_weight

        picks: list[tuple[float, str]] = []
        for t, bars in ctx.bars.items():
            if t in ctx.positions or t not in today:
                continue
            i = _idx(bars, ctx.date)
            if bars[i].date != ctx.date or i < self.lookback + 2:
                continue
            hh = ctx.series(t, f"hh{self.lookback}", lambda b, n=self.lookback: _hi(b, n))
            if hh[i] is None:
                continue
            last = bars[i].close
            if last <= hh[i]:
                continue
            window = bars[max(0, i - self.lookback) : i]
            avg_vol = sum(b.volume for b in window) / len(window)
            if avg_vol <= 0 or bars[i].volume < self.vol_mult * avg_vol:
                continue
            picks.append((last / hh[i] - 1, t))

        for _, t in sorted(picks, reverse=True)[: self.max_positions]:
            alloc[t] = self.max_weight

        if sum(alloc.values()) > 1.0:
            k = 1.0 / sum(alloc.values())
            alloc = {t: w * k for t, w in alloc.items()}
        return StrategyResponse(orders=orders, allocation=alloc)


REGISTRY = {
    TrendMA.name: TrendMA,
    Breakout20.name: Breakout20,
}


def build(name: str, **kw):
    if name not in REGISTRY:
        raise KeyError(f"未知策略 {name}，可用: {', '.join(REGISTRY)}")
    return REGISTRY[name](**{k: v for k, v in kw.items() if v is not None})
