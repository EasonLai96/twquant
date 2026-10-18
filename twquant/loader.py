"""K線載入與技術指標。

Bar 用 dataclass 不用 DataFrame：這包要零依賴，3000 根以內純 Python 跑起來一點都不慢。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .store import Store

# 預設交易宇宙：台股市值前段，流動性夠、資料乾淨
DEFAULT_UNIVERSE = [
    "2330",  # 台積電
    "2317",  # 鴻海
    "2454",  # 聯發科
    "2308",  # 台達電
    "2382",  # 廣達
    "2891",  # 中信金
    "2884",  # 玉山金
    "2412",  # 中華電
    "1301",  # 台塑
    "2603",  # 長榮
]

# 指數/ETF：拿來當基準，不進交易宇宙（否則策略會拿基準自己去交易）
INDEX_TICKERS = ["0050", "006208"]
BENCH_PREF = ("0050", "006208", "TAIEX")
INDEX_REF = "0050"


@dataclass(slots=True)
class Bar:
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: int


def bars_from_rows(rows) -> list[Bar]:
    """把 SQLite 的列轉成 Bar，順手濾掉壞資料。

    FinMind 對部分標的會回傳 open/close 為 0 的 bar（006208 的早期資料有 152 根這種），
    這種 bar 進了回測會讓「以價格為分母」的計算爆掉或算出無限大的部位，
    所以一律在載入時就擋掉，而不是等到引擎裡四處散落防禦性檢查。
    """
    out: list[Bar] = []
    for r in rows:
        try:
            o, h, lo, c = float(r[1]), float(r[2]), float(r[3]), float(r[4])
        except (TypeError, ValueError):
            continue
        if o <= 0 or h <= 0 or lo <= 0 or c <= 0:
            continue
        out.append(Bar(str(r[0])[:10], o, h, lo, c, int(r[5] or 0)))
    return out


def load_bars(store: Store, ticker: str, start: str, end: str) -> list[Bar]:
    return bars_from_rows(store.load_bars(ticker, start, end))


BREAK_TOL = 0.02
# 單日收盤跳動超過這個幅度就當成結構性斷點（台股有漲跌幅限制，正常交易日碰不到）
DEFAULT_JUMP = 0.2


def extract_factors(raw: dict[str, float], adj: dict[str, float], tol: float = BREAK_TOL) -> dict[str, float]:
    """從「原價 vs 還原價」兩條序列找出還原因子，並偵測因子何時跳動。

    台股還原價的問題不是有沒有因子，而是因子會在分割/減資日突然改變
    （0050 在 2025-06-18 做 4:1 分割，還原序列卻混著分割前後兩種基準）。
    把因子抽出來存成時間序列，之後才能算出「分割安全」的日報酬。
    """
    if not raw or not adj:
        return {}
    common = sorted(set(raw) & set(adj))
    if not common:
        return {}
    picks: dict[str, float] = {}
    for d in common:
        r, a = raw[d], adj[d]
        if r and r > 0 and a and a > 0:
            picks[d] = a / r
    if not picks:
        return {}
    anchors: list[tuple[str, float]] = []
    for d in sorted(picks):
        if not anchors or abs(picks[d] / anchors[-1][1] - 1) > tol:
            anchors.append((d, picks[d]))
    out = {d: anchors[0][1] for d in common}
    for i in range(1, len(anchors)):
        start, factor = anchors[i]
        for d in common:
            if d >= start:
                out[d] = factor
    return out


def load_adj_returns(
    store: Store,
    ticker: str,
    start: str,
    end: str,
    jump: float = DEFAULT_JUMP,
) -> dict[str, float]:
    """「重新定價日」的標記與其餘日子的日報酬。

    正常交易日回傳真實日報酬；分割/減資/除權息這種價格被重新定價的日子回傳 0.0，
    引擎看到 0.0 就換算股數（而不是把 -75% 當成真跌）。

    判定方式：優先信任還原因子表；不可用時（FinMind 的 adjust=1 實測是 no-op，
    因子整條都是 1.0）用「單日跳動 >= jump」當結構性斷點。門檻放一點浮點容差，
    不然剛好 20%（80/100-1 = -0.19999999999999996）會漏判。
    """
    rows = bars_from_rows(store.load_bars(ticker, start, end))
    factors = store.load_factors(ticker, start, end)
    limit = jump * (1 - 1e-9)
    out: dict[str, float] = {}
    for i in range(1, len(rows)):
        d0, c0 = rows[i - 1].date, rows[i - 1].close
        d1, c1 = rows[i].date, rows[i].close
        f0, f1 = factors.get(d0, 1.0), factors.get(d1, 1.0)
        if f1 != f0:
            out[d1] = 0.0
            continue
        if c0 <= 0 or c1 <= 0:
            continue
        r = c1 / c0 - 1
        out[d1] = 0.0 if abs(r) >= limit else r
    return out


def fetch_universe(
    store: Store,
    tickers: list[str],
    start: str,
    end: str,
    adj: bool = True,
    force: bool = False,
    verbose: bool = True,
) -> dict[str, int]:
    out: dict[str, int] = {}
    for i, t in enumerate(tickers, 1):
        try:
            out[t] = store.fetch_bars(t, start, end, adj=adj, force=force)
        except Exception as e:  # noqa: BLE001
            out[t] = -1
            if verbose:
                print(f"  [{i}/{len(tickers)}] {t} 失敗: {e}")
            continue
        if verbose:
            print(f"  [{i}/{len(tickers)}] {t} +{out[t]} rows")
    return out


def load_universe(store: Store, tickers: list[str], start: str, end: str) -> dict[str, list[Bar]]:
    data: dict[str, list[Bar]] = {}
    for t in tickers:
        bars = load_bars(store, t, start, end)
        if len(bars) >= 60:
            data[t] = bars
    return data


# --- 指標 -------------------------------------------------------------------
# 全部回傳與輸入等長的 list，前段不足期用 None 補。這樣 index 對齊，不用另外記 offset。

def sma(xs: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(xs)
    if n <= 0:
        return out
    run = 0.0
    for i, x in enumerate(xs):
        run += x
        if i >= n:
            run -= xs[i - n]
        if i >= n - 1:
            out[i] = run / n
    return out


def ema(xs: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(xs)
    if n <= 0 or len(xs) < n:
        return out
    k = 2.0 / (n + 1)
    seed = sum(xs[:n]) / n
    out[n - 1] = seed
    prev = seed
    for i in range(n, len(xs)):
        prev = xs[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def atr(bars: list[Bar], n: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(bars)
    if len(bars) < n + 1:
        return out
    trs: list[float] = [0.0]
    for i in range(1, len(bars)):
        pc = bars[i - 1].close
        trs.append(max(bars[i].high - bars[i].low, abs(bars[i].high - pc), abs(bars[i].low - pc)))
    run = sum(trs[1 : n + 1])
    out[n] = run / n
    for i in range(n + 1, len(bars)):
        run = run - run / n + trs[i]
        out[i] = run / n
    return out


def rsi(xs: list[float], n: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(xs)
    if len(xs) < n + 1:
        return out
    gains = 0.0
    losses = 0.0
    for i in range(1, n + 1):
        d = xs[i] - xs[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    ag, al = gains / n, losses / n
    out[n] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(n + 1, len(xs)):
        d = xs[i] - xs[i - 1]
        ag = (ag * (n - 1) + max(d, 0.0)) / n
        al = (al * (n - 1) + max(-d, 0.0)) / n
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def rolling_high(xs: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(xs)
    for i in range(len(xs)):
        if i >= n:
            out[i] = max(xs[i - n : i])
    return out


def rolling_std(xs: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(xs)
    for i in range(n - 1, len(xs)):
        w = xs[i - n + 1 : i + 1]
        m = sum(w) / n
        out[i] = math.sqrt(sum((v - m) ** 2 for v in w) / n)
    return out
