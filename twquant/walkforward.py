"""Walk-forward 驗證。

單一區間調參 = 過擬合。這裡把時間切成滾動的 IS/OS，每段只用訓練期挑參數，
再套到緊接著的測試期，最後把各段 OS 接起來算績效。報告會同時吐出 IS 跟 OS 的差距，
那個差距才是這組參數到底有沒有用的答案。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .engine import CostConfig, PortfolioBacktester
from .loader import Bar
from .strategies import TrendMA

# 每個 fold 前方多抓的暖機交易日，確保 OS 第一天指標就已經成熟
WARMUP = 120

# 可以拿來挑參數的指標；限定白名單是因為未知的 metric 會靜默選出網格第一組
VALID_METRICS = frozenset(
    {"sharpe", "total_return", "cagr", "calmar", "profit_factor", "win_rate", "max_drawdown"}
)


@dataclass
class Fold:
    is_start: str
    is_end: str
    os_start: str
    os_end: str
    params: dict
    is_metrics: dict
    os_metrics: dict = field(default_factory=dict)


@dataclass
class WalkForwardResult:
    folds: list[Fold]
    oos: dict
    params_used: list[dict]

    def stability(self) -> dict:
        """被選中的參數分佈。一直換參數代表沒有一個穩定的區域。"""
        counts: dict[str, int] = {}
        for f in self.folds:
            key = ",".join(f"{k}={v}" for k, v in sorted(f.params.items()))
            counts[key] = counts.get(key, 0) + 1
        top = sorted(counts.items(), key=lambda kv: -kv[1])
        return {"distinct": len(counts), "top": top[:5]}


def _slice(data: dict[str, list[Bar]], start: str, end: str) -> dict[str, list[Bar]]:
    out = {}
    for t, bars in data.items():
        sel = [b for b in bars if start <= b.date <= end]
        if len(sel) >= 40:
            out[t] = sel
    return out


def _grid(params: dict) -> list[dict]:
    keys = list(params)
    combos = [{}]
    for k in keys:
        combos = [{**c, k: v} for c in combos for v in params[k]]
    return combos


def run(
    data: dict[str, list[Bar]],
    start: str,
    end: str,
    is_days: int = 756,
    os_days: int = 252,
    step_days: int = 252,
    capital: float = 1_000_000.0,
    max_positions: int = 5,
    cost: CostConfig | None = None,
    param_grid: dict | None = None,
    metric: str = "sharpe",
    verbose: bool = True,
    adj_returns: dict[str, dict[str, float]] | None = None,
) -> WalkForwardResult:
    cost = cost or CostConfig()
    grid = param_grid or {"fast": [10, 20, 30], "slow": [40, 60, 90]}
    if metric not in VALID_METRICS:
        # 拼錯 metric 會讓「每組參數都拿哨兵值」→ 第一組永遠勝出，整個 walk-forward 靜默失效
        raise ValueError(f"metric 必須是 {sorted(VALID_METRICS)} 之一，收到 {metric!r}")
    allowed = set(TrendMA(fast=2, slow=3).params()) & {"fast", "slow", "atr_n", "confirm_days", "trail_atr", "max_weight"}
    bad = set(grid) - allowed
    if bad:
        raise ValueError(f"walkforward 目前只支援 {sorted(allowed)}，收到 {sorted(bad)}")
    folds: list[Fold] = []
    all_dates = sorted({b.date for bars in data.values() for b in bars})
    all_dates = [d for d in all_dates if start <= d <= end]
    if len(all_dates) < is_days + os_days:
        raise ValueError(f"交易日不足：{len(all_dates)} 天，至少需要 {is_days + os_days} 天")

    i = 0
    while i + is_days + os_days <= len(all_dates):
        is_start, is_end = all_dates[i], all_dates[i + is_days - 1]
        os_start, os_end = all_dates[i + is_days], all_dates[i + is_days + os_days - 1]

        is_data = _slice(data, is_start, is_end)
        best, best_metrics = None, None
        errors: list[str] = []
        for combo in _grid(grid):
            try:
                strat = TrendMA(**combo, max_positions=max_positions)
                res = PortfolioBacktester(capital, cost, max_positions).run(
                    is_data, strat, benchmark=None, adj_returns=adj_returns
                )
            except Exception as e:  # noqa: BLE001
                # 吞掉例外是刻意的（某些參數在某些區間就是不合法），但必須留下痕跡，
                # 否則「全部失敗」會變成沒有任何線索的空結果
                errors.append(f"{combo}: {type(e).__name__}: {e}")
                continue
            score = res.metrics.get(metric, -9e9)
            if best_metrics is None or score > best_metrics.get(metric, -9e9):
                best, best_metrics = combo, res.metrics

        if best is None:
            if verbose:
                print(f"  fold {len(folds) + 1}: IS {is_start}~{is_end} 全部參數失敗，跳過")
                for e in errors[:3]:
                    print(f"      {e}")
            i += step_days
            continue

        # OS 段前面接暖機資料，指標成熟後只取 OS 範圍的權益
        warm_start = all_dates[max(0, all_dates.index(os_start) - WARMUP)]
        os_data = _slice(data, warm_start, os_end)
        strat = TrendMA(**best, max_positions=max_positions)
        os_res = PortfolioBacktester(capital, cost, max_positions).run(
            os_data, strat, start=os_start, benchmark=None, adj_returns=adj_returns
        )

        folds.append(Fold(is_start, is_end, os_start, os_end, dict(best), dict(best_metrics), dict(os_res.metrics)))
        if verbose:
            print(
                f"  fold {len(folds):>2}  IS {is_start}~{is_end}  OS {os_start}~{os_end}  "
                f"{best}  IS_{metric}={best_metrics.get(metric, 0):.2f}  OS_{metric}={os_res.metrics.get(metric, 0):.2f}"
                f"  OS報酬={os_res.metrics.get('total_return', 0) * 100:+.1f}%"
            )
        i += step_days

    oos = _aggregate(folds)
    if not folds:
        raise ValueError(
            "沒有任何 fold 成功：可能是交易日不足、參數網格全部不合法，或資料切片後不足 40 天。"
            "把 --is-days/--os-days 調小或 --start 往前拉再試。"
        )
    return WalkForwardResult(folds, oos, [f.params for f in folds])


def _aggregate(folds: list[Fold]) -> dict:
    if not folds:
        return {}
    returns = []
    for f in folds:
        # OS 段之間時間不重疊，複利連乘等於連續持有
        returns.append((1 + f.os_metrics.get("total_return", 0.0)))
    prod = 1.0
    for r in returns:
        prod *= r
    wins = sum(1 for r in returns if r > 1.0)
    is_avg = sum(f.is_metrics.get("total_return", 0.0) for f in folds) / len(folds)
    os_avg = sum(f.os_metrics.get("total_return", 0.0) for f in folds) / len(folds)
    return {
        "folds": len(folds),
        "total_return": prod - 1,
        "is_avg_return": is_avg,
        "os_avg_return": os_avg,
        "is_os_gap": is_avg - os_avg,
        "os_win_folds": wins,
        "os_win_rate": wins / len(folds),
        "os_avg_sharpe": sum(f.os_metrics.get("sharpe", 0.0) for f in folds) / len(folds),
        "is_avg_sharpe": sum(f.is_metrics.get("sharpe", 0.0) for f in folds) / len(folds),
        "worst_os_dd": min(f.os_metrics.get("max_drawdown", 0.0) for f in folds),
    }
