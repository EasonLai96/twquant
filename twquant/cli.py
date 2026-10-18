"""twquant CLI。

python -m twquant fetch   --start 2020-01-01 --end 2026-10-17
python -m twquant backtest --strategy trend_ma --fast 20 --slow 60 --start 2020-01-01 --days 5
python -m twquant walkforward --start 2020-01-01 --end 2026-10-17
python -m twquant sweep --strategy breakout --param lookback=10,20,40 --param vol_mult=1.2,1.5
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path

from . import __version__
from . import walkforward as wf
from .engine import CostConfig, PortfolioBacktester
from .finmind import Client, QuotaExceeded
from .loader import (
    BENCH_PREF,
    DEFAULT_UNIVERSE,
    INDEX_REF,
    INDEX_TICKERS,
    fetch_universe,
    load_adj_returns,
    load_universe,
)
from .report import pct, text_summary, write_report
from .store import Store
from .strategies import REGISTRY, build

DEFAULT_DB = "data/twstock.sqlite"
DEFAULT_OUT = "out"


def _cost(a) -> CostConfig:
    return CostConfig(
        fee_discount=a.fee_discount,
        fee_min=a.fee_min,
        tax_sell=a.tax,
        slippage_bps=a.slippage,
        lot=a.lot,
        min_volume=a.min_volume,
    )


def _strategy_kwargs(a) -> dict:
    """只挑策略認得的參數。未提供的選項在 argparse 用 SUPPRESS，所以不會出現在 namespace。"""
    return {k: v for k, v in vars(a).items() if k in _STRATEGY_KEYS}


_STRATEGY_KEYS = {
    "fast", "slow", "atr_n", "max_weight", "confirm_days", "trail_atr",
    "lookback", "vol_mult", "max_hold", "exit_ma",
}


def _tickers(a) -> list[str]:
    if a.tickers is not None:
        # 空字串是「使用者明確給了清單但清單是空的」，不是「沒填」，不該靜默換成預設宇宙
        picked = [t.strip() for t in a.tickers.split(",") if t.strip()]
        if not picked:
            raise SystemExit("--tickers 是空的：要嘛不要給，要嘛給至少一檔（例如 --tickers 2330,2317）")
        return picked
    if a.etf:
        return [INDEX_REF, "006208", "0056", "00878", "00713", "2330", "2308", "2412", "2884", "1301"]
    # 交易宇宙 + 基準 ETF，兩者都抓才能算超額報酬
    return [*DEFAULT_UNIVERSE, INDEX_REF, "006208"]


def _trade_universe(data: dict) -> dict:
    """把基準 ETF 從交易宇宙剔除。

    sweep 與 walkforward 一定要跟 backtest 用同一個宇宙，
    否則同一組參數在兩邊會給出不同績效，而且策略可以買賣自己的基準。
    """
    return {t: v for t, v in data.items() if t not in INDEX_TICKERS}


def cmd_fetch(a) -> int:
    end = a.end or dt.date.today().isoformat()
    tickers = _tickers(a)
    with Store(a.db, Client()) as st:
        print(f"抓取 {len(tickers)} 檔，{a.start} ~ {end}")
        summary = fetch_universe(st, tickers, a.start, end, adj=True, force=a.force)
    ok = sum(1 for v in summary.values() if v >= 0)
    print(f"完成：{ok}/{len(tickers)} 檔；資料庫 {Path(a.db).resolve()}")
    return 0 if ok else 1


def _load(a, tickers) -> tuple[dict, dict]:
    end = a.end or dt.date.today().isoformat()
    with Store(a.db, Client()) as st:
        data = load_universe(st, tickers, a.start, end)
        adj = {t: load_adj_returns(st, t, a.start, end) for t in data}
    return data, adj


def _bench_of(a, data) -> str | None:
    want = getattr(a, "benchmark", None)
    if want and want.lower() in ("none", "equal"):
        return None
    if want and want in data:
        return want
    if want:
        print(
            f"  警告：基準 {want} 不在本次載入的資料裡（用 --tickers 指定時不會自動補基準），"
            f"改用 {'/'.join(t for t in BENCH_PREF if t in data) or '等權買進持有'}",
            file=sys.stderr,
        )
    for t in BENCH_PREF:
        if t in data:
            return t
    return None


def _run_backtest(a) -> tuple:
    tickers = _tickers(a)
    data, adj = _load(a, tickers)
    if not data:
        print("本地沒有資料，先跑 fetch", file=sys.stderr)
        return None, None, None
    strat = build(a.strategy, max_positions=a.max_positions, **_strategy_kwargs(a))
    bt = PortfolioBacktester(a.cash, _cost(a), a.max_positions)
    # 交易宇宙要剔除基準 ETF，但基準本身仍要留在資料裡，否則算不出超額報酬
    bench = _bench_of(a, data)
    tradable = _trade_universe(data) or data
    res = bt.run(tradable, strat, benchmark=bench, progress=a.verbose, adj_returns=adj,
                 benchmark_data=data if bench else None)
    return res, data, tickers


def cmd_backtest(a) -> int:
    res, _, _ = _run_backtest(a)
    if res is None:
        return 1
    print(text_summary(res))
    tag = a.tag or f"{a.strategy}_{res.config['start']}_{res.config['end']}"
    paths = write_report(res, a.out, tag)
    print(f"  報表 {paths['md']}")
    print(f"  圖表 {paths['svg']}")
    if a.json:
        jp = Path(a.out) / f"{tag}.json"
        jp.parent.mkdir(parents=True, exist_ok=True)
        jp.write_text(json.dumps(res.as_json(), ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  明細 {jp}")
    if a.trades:
        print("\n  交易明細")
        print(f"  {'標的':<8}{'進場':<12}{'出場':<12}{'進場價':>10}{'出場價':>10}{'股數':>10}{'損益':>14}{'報酬':>10}{'天數':>6}  原因")
        for t in res.trades:
            print(
                f"  {t.ticker:<8}{t.entry_date:<12}{t.exit_date:<12}{t.entry_price:>10.2f}{t.exit_price:>10.2f}"
                f"{t.shares:>10,}{t.pnl:>14,.0f}{t.ret * 100:>9.2f}%{t.days:>6}  {t.reason}"
            )
    return 0


def cmd_sweep(a) -> int:
    grid: dict[str, list] = {}
    for spec in a.param or []:
        if "=" not in spec:
            continue
        k, v = spec.split("=", 1)
        vals = []
        for x in v.split(","):
            try:
                vals.append(int(x))
            except ValueError:
                vals.append(float(x))
        grid[k.strip()] = vals
    if not grid:
        grid = {"fast": [10, 20, 30], "slow": [40, 60, 90]}

    end = a.end or dt.date.today().isoformat()
    tickers = _tickers(a)
    data, adj = _load(a, tickers)
    if not data:
        print("本地沒有資料，先跑 fetch", file=sys.stderr)
        return 1
    universe = _trade_universe(data) or data

    combos = [{}]
    for k, vals in grid.items():
        combos = [{**c, k: v} for c in combos for v in vals]

    bench = _bench_of(a, data)
    rows = []
    for combo in combos:
        strat = build(a.strategy, max_positions=a.max_positions, **combo)
        res = PortfolioBacktester(a.cash, _cost(a), a.max_positions).run(
            universe, strat, benchmark=bench, adj_returns=adj
        )
        m = res.metrics
        rows.append((m["sharpe"], m["cagr"], m["max_drawdown"], m["trades"], m["win_rate"], combo, m["total_return"]))

    rows.sort(key=lambda r: -r[0])
    print(f"\n  {a.strategy} 參數掃描，{len(rows)} 組，依 Sharpe 排序（{len(universe)} 檔標的）\n")
    print(f"  {'Sharpe':>8}{'CAGR':>10}{'MDD':>9}{'筆數':>7}{'勝率':>8}{'總報酬':>10}   參數")
    for s, c, d, n, w, combo, tot in rows[: a.top]:
        print(f"  {s:>8.2f}{c * 100:>9.1f}%{d * 100:>8.1f}%{n:>7}{w * 100:>7.1f}%{tot * 100:>9.1f}%   {combo}")
    print("\n  提醒：上面是 in-sample 最佳解，直接拿去用就是過擬合。請跑 walkforward 看 out-of-sample。\n")
    return 0


def cmd_walkforward(a) -> int:
    end = a.end or dt.date.today().isoformat()
    tickers = _tickers(a)
    data, adj = _load(a, tickers)
    if not data:
        print("本地沒有資料，先跑 fetch", file=sys.stderr)
        return 1
    grid = None
    if a.grid:
        grid = {}
        for spec in a.grid:
            k, v = spec.split("=", 1)
            vals = []
            for x in v.split(","):
                try:
                    vals.append(int(x))
                except ValueError:
                    vals.append(float(x))
            grid[k] = vals
    res = wf.run(
        _trade_universe(data) or data,
        a.start,
        end,
        is_days=a.is_days,
        os_days=a.os_days,
        step_days=a.step,
        capital=a.cash,
        max_positions=a.max_positions,
        cost=_cost(a),
        param_grid=grid,
        metric=a.metric,
        adj_returns=adj,
    )
    o = res.oos
    print("\n  Walk-forward 結果\n")
    print(f"  fold 數 {o['folds']}   IS 平均報酬 {pct(o['is_avg_return'])}   OS 平均報酬 {pct(o['os_avg_return'])}")
    print(f"  IS-OS 落差 {pct(o['is_os_gap'])}   （這個數字越大代表參數越不穩）")
    print(f"  OS 勝率 {pct(o['os_win_rate'])}（{o['os_win_folds']}/{o['folds']} 段賺錢）   OS 平均 Sharpe {o['os_avg_sharpe']:.2f}")
    print(f"  OS 串接總報酬 {pct(o['total_return'])}   最差單段回撤 {pct(o['worst_os_dd'])}")
    st = res.stability()
    print(f"  被選中的參數組合數 {st['distinct']}，最常出現：")
    for k, n in st["top"]:
        print(f"    {n} 次  {k}")
    if a.json:
        p = Path(a.out) / "walkforward.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(
                {
                    "oos": o,
                    "stability": res.stability(),
                    "folds": [
                        {
                            "is": [f.is_start, f.is_end],
                            "os": [f.os_start, f.os_end],
                            "params": f.params,
                            "is_sharpe": f.is_metrics.get("sharpe"),
                            "os_sharpe": f.os_metrics.get("sharpe"),
                            "os_return": f.os_metrics.get("total_return"),
                            "os_dd": f.os_metrics.get("max_drawdown"),
                        }
                        for f in res.folds
                    ],
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )
        print(f"  明細 {p}")
    print()
    return 0


def cmd_universe(a) -> int:
    end = a.end or dt.date.today().isoformat()
    st = Store(a.db, Client())
    rows = st.db.execute(
        "SELECT ticker, COUNT(*) n, MIN(date), MAX(date), AVG(volume) v FROM bars "
        "GROUP BY ticker ORDER BY v DESC"
    ).fetchall()
    print(f"\n  本地資料庫：{Path(a.db).resolve()}\n")
    print(f"  {'標的':<8}{'筆數':>7}{'起始':>14}{'結束':>14}{'日均量(股)':>16}")
    for t, n, d0, d1, v in rows[: a.top]:
        print(f"  {t:<8}{n:>7}{d0:>14}{d1:>14}{v:>16,.0f}")
    if not rows:
        print("  （空，先跑 fetch）")
    print()
    return 0


def cmd_serve(a) -> int:
    """極簡本機報表伺服器：把 out/ 目錄用 HTTP 丟出來，SVG 直接看。"""
    import http.server
    import socketserver

    root = Path(a.out).resolve()
    if not root.exists():
        print(f"{root} 不存在，先跑 backtest", file=sys.stderr)
        return 1

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kw):
            super().__init__(*args, directory=str(root), **kw)

        def log_message(self, fmt, *args):
            pass

    with socketserver.TCPServer(("127.0.0.1", a.port), Handler) as httpd:
        print(f"http://127.0.0.1:{a.port}/  (Ctrl+C 結束)")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


def cmd_doctor(a) -> int:
    """檢查本地快取的資料品質。回測之前先跑這個，比事後猜績效為什麼怪快得多。"""
    st = Store(a.db)
    q = st.quality(jump_threshold=a.jump)
    tickers = st.tickers()
    print(f"\n  資料健檢：{len(tickers)} 檔，{Path(a.db).resolve()}\n")
    breaks = {t: st.factor_breaks(t) for t in tickers}
    breaks = {t: v for t, v in breaks.items() if v}
    if breaks:
        print("  還原因子斷點（分割/減資，日報酬會被歸零而不是算成假崩盤）：")
        for t, v in sorted(breaks.items()):
            for d, f0, f1 in v[:4]:
                print(f"      {t}  {d}  因子 {f0:.4f} -> {f1:.4f}  (相當於 1:{f1 / f0:.2f})")
        print()
    if not q:
        print("  沒發現可疑價格斷點。\n")
        return 0
    for t, info in sorted(q.items()):
        print(f"  {t}  {info['rows']} 根，{len(info['jumps'])} 個 >{a.jump * 100:.0f}% 斷點，"
              f"{info['zero_volume_days']} 個零成交量日")
        for j in info["jumps"][:6]:
            print(f"      {j['date']}  {j['from']:.2f} -> {j['to']:.2f}  ({j['chg'] * 100:+.1f}%)")
        if len(info["jumps"]) > 6:
            print(f"      ...其餘 {len(info['jumps']) - 6} 筆")
    print("\n  說明：>30% 的單日跳動幾乎都是分割/減資/換股等結構性事件，不是行情。")
    print("  回測引擎在這些日子會把日報酬歸零（寧可少賺也不製造假績效），")
    print("  但該標的的實際報酬仍會有落差，介意就 --tickers 排除。\n")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="twquant", description="台股量化回測工具（零依賴）")
    p.add_argument("--version", action="version", version=f"twquant {__version__}")
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--out", default=DEFAULT_OUT)
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        # --db / --out 也放進各子命令，免得擋在 subcommand 後面被 argparse 判成非法參數。
        # SUPPRESS 讓沒填時不會覆蓋掉寫在前面的 --db。
        sp.add_argument("--db", default=argparse.SUPPRESS)
        sp.add_argument("--out", default=argparse.SUPPRESS)
        sp.add_argument("--start", default="2015-01-01")
        sp.add_argument("--end", default=None)
        sp.add_argument("--tickers", default=None, help="逗號分隔，預設用內建宇宙")
        sp.add_argument("--etf", action="store_true", help="改用 ETF + 大型股組合")
        sp.add_argument("--cash", type=float, default=1_000_000.0)
        sp.add_argument("--max-positions", dest="max_positions", type=int, default=5)
        sp.add_argument("--fee-discount", dest="fee_discount", type=float, default=0.6)
        sp.add_argument("--fee-min", dest="fee_min", type=float, default=20.0)
        sp.add_argument("--tax", type=float, default=0.003)
        sp.add_argument("--slippage", type=float, default=5.0, help="bp")
        sp.add_argument("--lot", type=int, default=1000)
        sp.add_argument("--min-volume", dest="min_volume", type=int, default=2_000_000)
        sp.add_argument("--strategy", default="trend_ma", choices=list(REGISTRY))
        sp.add_argument("--verbose", action="store_true")

    def strat_args(sp, with_positions=True):
        # 用 SUPPRESS：沒給的參數直接不進 namespace，讓策略自己的預設值說話
        if with_positions:
            sp.add_argument("--max-weight", dest="max_weight", type=float, default=argparse.SUPPRESS)
        sp.add_argument("--fast", type=int, default=argparse.SUPPRESS)
        sp.add_argument("--slow", type=int, default=argparse.SUPPRESS)
        sp.add_argument("--atr-n", dest="atr_n", type=int, default=argparse.SUPPRESS)
        sp.add_argument("--confirm-days", dest="confirm_days", type=int, default=argparse.SUPPRESS)
        sp.add_argument("--trail-atr", dest="trail_atr", type=float, default=argparse.SUPPRESS)
        sp.add_argument("--lookback", type=int, default=argparse.SUPPRESS)
        sp.add_argument("--vol-mult", dest="vol_mult", type=float, default=argparse.SUPPRESS)
        sp.add_argument("--max-hold", dest="max_hold", type=int, default=argparse.SUPPRESS)
        sp.add_argument("--exit-ma", dest="exit_ma", type=int, default=argparse.SUPPRESS)

    f = sub.add_parser("fetch", help="抓取並快取日 K")
    common(f)
    f.add_argument("--force", action="store_true")
    f.set_defaults(func=cmd_fetch)

    b = sub.add_parser("backtest", help="跑回測並輸出報表")
    common(b)
    strat_args(b)
    b.add_argument("--benchmark", default="0050", help="基準標的，'none' 表示等權買進持有")
    b.add_argument("--tag", default=None)
    b.add_argument("--json", action="store_true")
    b.add_argument("--trades", action="store_true")
    b.set_defaults(func=cmd_backtest)

    s = sub.add_parser("sweep", help="參數掃描（in-sample，僅供找方向）")
    common(s)
    strat_args(s)
    s.add_argument("--param", action="append", help="例：--param fast=10,20,30")
    s.add_argument("--top", type=int, default=20)
    s.set_defaults(func=cmd_sweep)

    w = sub.add_parser("walkforward", help="滾動樣本外驗證")
    common(w)
    strat_args(w)
    w.add_argument("--is-days", dest="is_days", type=int, default=756)
    w.add_argument("--os-days", dest="os_days", type=int, default=252)
    w.add_argument("--step", type=int, default=252)
    w.add_argument("--metric", default="sharpe", choices=sorted(wf.VALID_METRICS))
    w.add_argument("--grid", action="append", help="例：--grid fast=10,20,30")
    w.add_argument("--json", action="store_true")
    w.set_defaults(func=cmd_walkforward)

    u = sub.add_parser("universe", help="看本地已快取哪些標的")
    common(u)
    u.add_argument("--top", type=int, default=40)
    u.set_defaults(func=cmd_universe)

    d = sub.add_parser("doctor", help="檢查快取資料的價格斷點與零量日")
    common(d)
    d.add_argument("--jump", type=float, default=0.3, help="單日跳動超過幾成要列出來（0.3 = 30%%）")
    d.set_defaults(func=cmd_doctor)

    sv = sub.add_parser("serve", help="把 out/ 用 HTTP 開出來看 SVG")
    sv.add_argument("--port", type=int, default=8899)
    sv.set_defaults(func=cmd_serve)

    return p


def main(argv=None) -> int:
    p = build_parser()
    a = p.parse_args(argv)
    a.db = getattr(a, "db", None) or DEFAULT_DB
    a.out = getattr(a, "out", None) or DEFAULT_OUT
    try:
        return a.func(a)
    except QuotaExceeded as e:
        print(f"FinMind 額度不足：{e}\n提示：設定環境變數 FINMIND_TOKEN 可提高額度", file=sys.stderr)
        return 2
    except sqlite3.OperationalError as e:
        print(f"資料庫錯誤：{e}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
