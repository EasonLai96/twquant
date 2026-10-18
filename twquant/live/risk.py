"""風控閘門。

這是整條自動化鏈上唯一「有權說不」的地方，所以它必須在策略之後、下單之前執行，
而且每一次攔阻都要留下紀錄 —— 沒有紀錄的風控等於沒有風控。

檢查項目的取捨：只擋「已知會造成無法挽回損失」的事，不去猜策略好壞。
策略該不該跑是研究問題，這裡只管帳戶還撐不撐得住。
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path

# 存在這個檔案就停止下單。要人工解除，程式不會自己刪。
KILL_FILE = "HALT"


@dataclass
class RiskLimits:
    max_positions: int = 5
    max_weight: float = 0.30  # 單一標的佔權益上限
    max_exposure: float = 1.00  # 總持股佔權益上限
    daily_loss_limit: float = 0.05  # 單日虧損超過這個比例就停到隔天
    min_cash: float = 0.0
    # 連續攔阻幾次後強制停止，避免策略壞掉時每天照樣送單
    max_consecutive_blocks: int = 3


@dataclass
class Decision:
    allow: bool
    reasons: list[str] = field(default_factory=list)
    advisories: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.allow


class RiskGate:
    """風控閘門。

    兩個層次要分開，混在一起會造成永久停機：

    hard（真的停手）—— 單日虧損超限、HALT 檔存在、連續多次無法下單。
    這些是「今天不要再動作比較好」的情況。

    soft（只擋新倉）—— 單檔佔比超標、總曝險超標、持股檔數超標。
    這些只代表「不要再加碼」，既有的部位該由策略自己決定何時減。
    如果把它們當成 hard，會出現「部位漲太多所以永遠停止下單」的死結，
    而停止下單完全不會讓部位變小 —— 這是實單自動化最容易踩的坑。
    """

    def __init__(self, limits: RiskLimits | None = None, state_dir: str | Path = "."):
        self.limits = limits or RiskLimits()
        self.state_dir = Path(state_dir)
        self._blocks_in_a_row = 0
        self._day_equity_open: float | None = None

    def start_day(self, equity: float) -> None:
        self._day_equity_open = equity

    def _day_return(self, equity: float) -> float:
        base = self._day_equity_open
        if not base or base <= 0:
            return 0.0
        return equity / base - 1

    def check(self, account, held: dict[str, int], prices: dict[str, float], equity_prev: float | None) -> Decision:
        L = self.limits
        hard: list[str] = []
        soft: list[str] = []

        if (self.state_dir / KILL_FILE).exists():
            hard.append(f"偵測到 {KILL_FILE} 檔，停止下單")

        day_ret = self._day_return(account.equity)
        if day_ret <= -abs(L.daily_loss_limit):
            hard.append(f"單日虧損 {day_ret * 100:.2f}% 已達上限 {L.daily_loss_limit * 100:.1f}%")

        n_pos = sum(1 for s in held.values() if s > 0)
        if n_pos > L.max_positions:
            soft.append(f"持股 {n_pos} 檔超過上限 {L.max_positions}，只允許減碼")

        if account.equity > 0:
            for t, sh in held.items():
                if sh <= 0:
                    continue
                px = prices.get(t, 0.0)
                if px <= 0:
                    continue
                w = sh * px / account.equity
                if w > L.max_weight:
                    soft.append(f"{t} 佔比 {w * 100:.1f}% 超過單檔上限 {L.max_weight * 100:.1f}%")
            if account.exposure > L.max_exposure:
                soft.append(f"總曝險 {account.exposure * 100:.1f}% 超過上限 {L.max_exposure * 100:.1f}%")

        if account.cash < L.min_cash:
            soft.append(f"現金 {account.cash:,.0f} 低於下限 {L.min_cash:,.0f}")

        if self._blocks_in_a_row >= L.max_consecutive_blocks:
            hard.append(f"已連續 {self._blocks_in_a_row} 天無法下單，停止到人工處理")

        d = Decision(allow=not hard, reasons=hard, advisories=soft)
        self._blocks_in_a_row = 0 if d.allow else self._blocks_in_a_row + 1
        return d

    def halt(self, reason: str) -> None:
        """人工或程式觸發停止。寫入 HALT 檔後只有人能解除。"""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / KILL_FILE).write_text(
            f"{dt.datetime.now().isoformat(timespec='seconds')} {reason}\n", encoding="utf-8"
        )

    def resume(self) -> bool:
        p = self.state_dir / KILL_FILE
        if p.exists():
            p.unlink()
            return True
        return False

    @property
    def halted(self) -> bool:
        return (self.state_dir / KILL_FILE).exists()

    # --- 下單前的最後一道 ------------------------------------------------

    def filter_orders(self, orders: list[dict], account, held: dict[str, int], prices: dict[str, float]) -> tuple[list[dict], list[str]]:
        """把會讓部位超限的買單擋掉或縮小。賣單一律放行 —— 出場永遠優先。"""
        L = self.limits
        kept: list[dict] = []
        notes: list[str] = []
        n_pos = sum(1 for s in held.values() if s > 0)
        exposure_val = sum(held.get(t, 0) * prices.get(t, 0.0) for t in held)

        for o in orders:
            if o["side"] == "sell":
                kept.append(o)
                continue
            t = o["ticker"]
            px = prices.get(t, 0.0)
            if px <= 0:
                notes.append(f"{t} 無報價，略過買單")
                continue
            is_new = held.get(t, 0) <= 0
            if is_new and n_pos >= L.max_positions:
                notes.append(f"{t} 已達持股檔數上限 {L.max_positions}，略過")
                continue

            room_w = L.max_weight * account.equity - held.get(t, 0) * px
            room_e = L.max_exposure * account.equity - exposure_val
            room_c = account.cash - L.min_cash
            budget = min(room_w, room_e, room_c)
            want = o["shares"] * px
            if budget <= 0:
                notes.append(f"{t} 沒有可用額度（權重/曝險/現金）")
                continue
            if want > budget:
                shrunk = int(budget / px // 1000) * 1000
                if shrunk <= 0:
                    notes.append(f"{t} 額度不足一張，略過")
                    continue
                notes.append(f"{t} 買單由 {o['shares']} 股縮到 {shrunk} 股")
                o = {**o, "shares": shrunk}
            kept.append(o)
            n_pos += 1 if is_new else 0
            exposure_val += o["shares"] * px
        return kept, notes
