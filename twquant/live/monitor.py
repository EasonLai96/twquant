"""盤中停損／停利監控。

策略本來就會決定出場，這個模組處理的是策略來不及反應的情況 ——
例如早上跳空跌 6%，等到收盤才處理就已經多虧一趟。

兩種模式：
  monitor_intraday()  實單：跟券商要即時報價，命中就送出場單
  monitor_from_bars() 模擬：用日 K 的當日開盤／最低價推估是否觸價

它只會「提早出場」，不會進場、不會加碼。進場永遠由策略決定，
這樣盤中的雜訊不可能變成新的部位。
"""
from __future__ import annotations

from dataclasses import dataclass

from ..broker import Broker
from ..loader import Bar


@dataclass
class StopRule:
    stop_loss: float = 0.08  # 相對進場價
    take_profit: float = 0.0  # 0 = 不停利
    trailing: float = 0.0  # 0 = 不啟用移動停損


@dataclass
class StopHit:
    ticker: str
    reason: str
    price: float
    entry_price: float
    pnl_pct: float


def _check(ticker: str, entry: float, price: float, peak: float, rule: StopRule) -> StopHit | None:
    if entry <= 0 or price <= 0:
        return None
    pnl = price / entry - 1
    if rule.stop_loss > 0 and pnl <= -abs(rule.stop_loss):
        return StopHit(ticker, "stop_loss", price, entry, pnl)
    if rule.take_profit > 0 and pnl >= abs(rule.take_profit):
        return StopHit(ticker, "take_profit", price, entry, pnl)
    if rule.trailing > 0 and peak > 0 and price <= peak * (1 - abs(rule.trailing)):
        return StopHit(ticker, "trailing", price, entry, pnl)
    return None


def monitor_intraday(broker: Broker, entries: dict[str, float], rule: StopRule, peaks: dict[str, float] | None = None) -> list[StopHit]:
    """實單：拉即時報價比對。只回傳「該出場」的清單，不對券商送單。

    送單留給呼叫端，因為那裡才知道今天已經處理過哪些標的、是否已達風控上限。
    """
    peaks = peaks if peaks is not None else {}
    hits: list[StopHit] = []
    for t, entry in entries.items():
        q = broker.quote(t)
        if q.price <= 0:
            continue
        peaks[t] = max(peaks.get(t, q.price), q.price)
        hit = _check(t, entry, q.price, peaks[t], rule)
        if hit:
            hits.append(hit)
    return hits


def monitor_from_bars(
    bars: dict[str, list[Bar]],
    date: str,
    entries: dict[str, float],
    rule: StopRule,
    peaks: dict[str, float] | None = None,
) -> list[StopHit]:
    """模擬：用當日最低價判斷是否觸價（保守，最壞情況），出場價取觸價價位。

    用最低價而不是收盤價，是因為盤中停損本來就會在低點附近成交；
    用收盤價會高估這個模組的效益。
    """
    peaks = peaks if peaks is not None else {}
    hits: list[StopHit] = []
    for t, entry in entries.items():
        bs = [b for b in bars.get(t, []) if b.date == date]
        if not bs or entry <= 0:
            continue
        b = bs[0]
        peaks[t] = max(peaks.get(t, b.high), b.high)
        hit = _check(t, entry, b.low, peaks[t], rule)
        if hit:
            hits.append(hit)
    return hits
