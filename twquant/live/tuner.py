"""參數自動重訓。

這是最危險的一個模組，所以它預設關閉，而且要通過三道閘門才會換參數。
理由很簡單：walk-forward 本身就會過擬合，拿它去自動改參數等於把過擬合直接變成虧損。
沒有閘門的自動調參，長期結果比固定參數更差。

三道閘門：
  1. fold 數要夠（樣本太少等於在挑雜訊）
  2. 樣本外的 Sharpe 必須 >= 門檻
  3. IS-OS 落差不得超過上限（落差大代表挑到的是雜訊，不是規律）

沒過就維持原參數。任何一次替換都寫進 events.jsonl，不覆蓋歷史。
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from .. import walkforward as wf
from ..engine import CostConfig
from ..loader import Bar
from .state import State


@dataclass
class TuneGate:
    min_folds: int = 3
    min_os_sharpe: float = 0.0
    max_is_os_gap: float = 0.15
    cooldown_days: int = 30


@dataclass
class TuneResult:
    applied: bool
    params_before: dict = field(default_factory=dict)
    params_after: dict = field(default_factory=dict)
    oos: dict = field(default_factory=dict)
    rejections: list[str] = field(default_factory=list)

    def summary(self) -> str:
        head = "採用新參數" if self.applied else "維持原參數"
        lines = [head, f"  前 {self.params_before}", f"  後 {self.params_after}"]
        if self.oos:
            lines.append(
                f"  OS Sharpe {self.oos.get('os_avg_sharpe', 0):.2f}"
                f"  OS 勝率 {self.oos.get('os_win_rate', 0) * 100:.0f}%"
                f"  IS-OS 落差 {self.oos.get('is_os_gap', 0) * 100:.1f}%"
                f"  fold {self.oos.get('folds', 0)}"
            )
        for r in self.rejections:
            lines.append(f"  未通過：{r}")
        return "\n".join(lines)


def _days_since(state: State) -> int | None:
    last = state.get_meta("last_tune_date")
    if not last:
        return None
    try:
        return (dt.date.today() - dt.date.fromisoformat(str(last))).days
    except ValueError:
        return None


def tune(
    data: dict[str, list[Bar]],
    state: State,
    current: dict,
    start: str,
    end: str,
    capital: float = 1_000_000.0,
    cost: CostConfig | None = None,
    gate: TuneGate | None = None,
    param_grid: dict | None = None,
    adj_returns: dict[str, dict[str, float]] | None = None,
    max_positions: int = 5,
    force: bool = False,
) -> TuneResult:
    g = gate or TuneGate()
    res = TuneResult(applied=False, params_before=dict(current), params_after=dict(current))

    since = _days_since(state)
    if not force and since is not None and since < g.cooldown_days:
        res.rejections.append(f"冷卻期內（上次調參 {since} 天前，需 {g.cooldown_days} 天）")
        state.log("tune_skipped", reason="cooldown", days=since)
        return res

    try:
        out = wf.run(
            data, start, end,
            capital=capital, cost=cost, param_grid=param_grid,
            adj_returns=adj_returns, max_positions=max_positions, verbose=False,
        )
    except ValueError as e:
        res.rejections.append(f"walk-forward 無法執行：{e}")
        state.log("tune_skipped", reason="walkforward_failed", error=str(e))
        return res

    res.oos = out.oos
    if out.oos.get("folds", 0) < g.min_folds:
        res.rejections.append(f"fold 數 {out.oos.get('folds', 0)} 少於 {g.min_folds}")
    if out.oos.get("os_avg_sharpe", 0.0) < g.min_os_sharpe:
        res.rejections.append(
            f"OS Sharpe {out.oos.get('os_avg_sharpe', 0):.2f} 低於 {g.min_os_sharpe}"
        )
    if out.oos.get("is_os_gap", 1.0) > g.max_is_os_gap:
        res.rejections.append(
            f"IS-OS 落差 {out.oos.get('is_os_gap', 0) * 100:.1f}% 超過 {g.max_is_os_gap * 100:.0f}%"
        )

    st = out.stability()
    if st["distinct"] > 1 and st["top"] and st["top"][0][1] < max(2, out.oos.get("folds", 0) // 2):
        res.rejections.append(f"參數不穩定（{st['distinct']} 種組合，最常見只出現 {st['top'][0][1]} 次）")

    if res.rejections:
        state.log("tune_rejected", rejections=res.rejections, oos=out.oos)
        return res

    newest = out.params_used[-1] if out.params_used else {}
    candidate = {**current, **newest}
    res.params_after = candidate
    res.applied = True
    state.set_meta("params", candidate)
    state.set_meta("last_tune_date", dt.date.today().isoformat())
    state.log("tune_applied", params=candidate, oos=out.oos, stability=st)
    return res
