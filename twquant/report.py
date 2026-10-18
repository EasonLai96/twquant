"""報表輸出：終端摘要、Markdown、SVG 圖。全部手刻，不拉 matplotlib。"""
from __future__ import annotations

import html
import math
from pathlib import Path

from .engine import BacktestResult

STRAT_COLOR = "#c2410c"  # 磚紅：策略
BENCH_COLOR = "#64748b"  # 石板灰：基準
BG = "#faf9f7"
INK = "#1f2933"


def pct(x: float, digits: int = 2) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "-"
    return f"{x * 100:+.{digits}f}%"


def money(x: float) -> str:
    return f"{x:,.0f}"


def text_summary(res: BacktestResult) -> str:
    m, c = res.metrics, res.config
    cfg = c["cost"]
    lines = [
        "",
        f"  策略 {c['strategy']}  {c['strategy_params']}",
        f"  宇宙 {len(c['universe'])} 檔 | {c['start']} ~ {c['end']} ({c['bars']} 交易日) | 起始 {money(c['capital'])}",
        f"  成本 手續費 {cfg['fee_rate'] * cfg['fee_discount'] * 100:.4f}% (最低 {cfg['fee_min']:.0f}) | "
        f"證交稅 {cfg['tax_sell'] * 100:.2f}% | 滑價 {cfg['slippage_bps']:.0f}bp",
        "",
        f"  期末權益      {money(m['final'])}   ({pct(m['total_return'])})",
        f"  CAGR          {pct(m['cagr'])}",
        f"  最大回撤      {pct(m['max_drawdown'])}   ({m['mdd_peak_date']} -> {m['mdd_trough_date']})",
        f"  年化波動      {pct(m['vol'])}",
        f"  Sharpe        {m['sharpe']:.2f}",
        f"  Calmar        {m['calmar']:.2f}",
        f"  平均曝險      {pct(m['avg_exposure'])}",
        "",
        f"  交易筆數      {m['trades']}   勝率 {pct(m['win_rate'])}   平均持有 {m['avg_hold_days']:.0f} 天",
        f"  平均獲利      {money(m['avg_win'])}    平均虧損 {money(m['avg_loss'])}    獲利因子 {m['profit_factor']:.2f}",
        f"  成本總計      {money(res.costs.total)}"
        f"  (手續費 {money(res.costs.fee)} / 稅 {money(res.costs.tax)} / 滑價 {money(res.costs.slippage)})",
    ]
    b = res.benchmark_metrics
    if b:
        lines += [
            "",
            f"  基準 {res.benchmark_label}",
            f"    報酬 {pct(b['total_return'])}   CAGR {pct(b['cagr'])}   MDD {pct(b['max_drawdown'])}   Sharpe {b['sharpe']:.2f}",
            f"    超額 {pct(m['total_return'] - b['total_return'])}",
        ]
    lines.append("")
    return "\n".join(lines)


def svg_chart(
    res: BacktestResult,
    width: int = 960,
    height: int = 460,
    title: str = "",
) -> str:
    curve = res.equity_curve()
    if len(curve) < 2:
        return "<svg xmlns='http://www.w3.org/2000/svg'/>"
    bench = res.benchmark_curve
    all_v = [v for _, v in curve] + [v for _, v in bench]
    lo, hi = min(all_v), max(all_v)
    pad = (hi - lo) * 0.06 or 1
    lo, hi = lo - pad, hi + pad

    ml, mr, mt, mb = 68, 18, 34, 54
    inner_w, inner_h = width - ml - mr, height - mt - mb
    dd_h = 96
    curve_h = inner_h - dd_h - 14

    def x_of(i: int, n: int) -> float:
        return ml + (inner_w * i / max(1, n - 1))

    def y_of(v: float) -> float:
        return mt + curve_h * (1 - (v - lo) / (hi - lo))

    def path(series: list[tuple[str, float]]) -> str:
        step = max(1, len(series) // 900)
        pts = [series[i] for i in range(0, len(series), step)]
        if pts[-1] != series[-1]:
            pts.append(series[-1])
        return "M" + " L".join(f"{x_of(i, len(pts)):.1f},{y_of(v):.1f}" for i, (_, v) in enumerate(pts))

    # 回撤序列（以策略自己的峰值算）
    peaks = []
    cur = -1e18
    for _, v in curve:
        cur = max(cur, v)
        peaks.append(cur)
    dds = [(curve[i][0], curve[i][1] / peaks[i] - 1 if peaks[i] > 0 else 0.0) for i in range(len(curve))]
    dd_min = min(d for _, d in dds)
    dd_min = dd_min if dd_min < 0 else -0.01

    def y_dd(v: float) -> float:
        base = mt + curve_h + 14
        return base + dd_h * (v / dd_min)

    n = len(curve)
    ticks_y = 5
    grid = []
    for k in range(ticks_y + 1):
        v = lo + (hi - lo) * k / ticks_y
        y = y_of(v)
        grid.append(
            f"<line x1='{ml}' y1='{y:.1f}' x2='{ml + inner_w}' y2='{y:.1f}' stroke='#e5e3df' stroke-width='1'/>"
            f"<text x='{ml - 10}' y='{y + 4:.1f}' text-anchor='end' class='ax'>{v / 10000:.0f}萬</text>"
        )
    xticks = []
    for k in range(6):
        i = min(n - 1, int(n * k / 5))
        xticks.append(
            f"<text x='{x_of(i, n):.1f}' y='{mt + curve_h + 16:.1f}' text-anchor='middle' class='ax'>{curve[i][0]}</text>"
        )

    dd_area = (
        f"M{ml},{y_dd(0):.1f} "
        + " L".join(f"{x_of(i, n):.1f},{y_dd(d):.1f}" for i, (_, d) in enumerate(dds))
        + f" L{ml + inner_w},{y_dd(0):.1f} Z"
    )
    dd_label = (
        f"<text x='{ml}' y='{mt + curve_h + 8:.0f}' class='ax'>回撤 {dd_min * 100:.1f}%</text>"
    )

    bench_path = (
        f"<path d='{path(bench)}' fill='none' stroke='{BENCH_COLOR}' stroke-width='1.6' stroke-dasharray='5 4'/>"
        if bench
        else ""
    )
    bench_legend = (
        f"<g transform='translate({ml + 190},18)'>"
        f"<line x1='0' y1='0' x2='22' y2='0' stroke='{BENCH_COLOR}' stroke-width='1.6' stroke-dasharray='5 4'/>"
        f"<text x='28' y='4' class='lg'>{html.escape(res.benchmark_label)}</text></g>"
        if bench
        else ""
    )

    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}" font-family="system-ui, -apple-system, 'Segoe UI', 'Noto Sans TC', sans-serif">
<style>
 .t {{ font-size:15px; fill:{INK}; font-weight:600 }}
 .ax {{ font-size:11px; fill:#8b8b8b }}
 .lg {{ font-size:12px; fill:{INK} }}
 .note {{ font-size:11px; fill:#8b8b8b }}
</style>
<rect width="{width}" height="{height}" fill="{BG}"/>
<text x="{ml}" y="22" class="t">{html.escape(title or res.config['strategy'])}</text>
<g transform="translate({ml + 60},18)">
  <line x1="0" y1="0" x2="22" y2="0" stroke="{STRAT_COLOR}" stroke-width="2"/>
  <text x="28" y="4" class="lg">策略淨值</text>
</g>
{bench_legend}
{''.join(grid)}
{xticks and ''.join(xticks)}
{bench_path}
<path d="{path(curve)}" fill="none" stroke="{STRAT_COLOR}" stroke-width="2"/>
<path d="{dd_area}" fill="rgba(194,65,12,0.16)" stroke="{STRAT_COLOR}" stroke-width="1"/>
{dd_label}
<text x="{ml + inner_w}" y="{height - 6}" text-anchor="end" class="note">成本 {money(res.costs.total)}　超額報酬 {pct(res.metrics['total_return'] - res.benchmark_metrics.get('total_return', 0))}</text>
</svg>"""


def markdown_report(res: BacktestResult, image_rel: str | None = None) -> str:
    m, c = res.metrics, res.config
    rows = [
        ("期末權益", money(m["final"])),
        ("總報酬", pct(m["total_return"])),
        ("CAGR", pct(m["cagr"])),
        ("最大回撤", pct(m["max_drawdown"])),
        ("年化波動", pct(m["vol"])),
        ("Sharpe (rf=2%)", f"{m['sharpe']:.2f}"),
        ("Calmar", f"{m['calmar']:.2f}"),
        ("平均曝險", pct(m["avg_exposure"])),
        ("交易筆數", str(m["trades"])),
        ("勝率", pct(m["win_rate"])),
        ("獲利因子", f"{m['profit_factor']:.2f}"),
        ("平均持有天數", f"{m['avg_hold_days']:.0f}"),
    ]
    b = res.benchmark_metrics
    cl = c["cost"]
    out = [
        f"# {c['strategy']} 台股回測報告",
        "",
        f"- 期間：`{c['start']}` ~ `{c['end']}`（{c['bars']} 交易日，{len(c['universe'])} 檔標的）",
        f"- 起始資金：{money(c['capital'])}，最大同時持有 {c['max_positions']} 檔",
        f"- 參數：`{c['strategy_params']}`",
        f"- 成本假設：手續費 {cl['fee_rate'] * cl['fee_discount']:.6f}（最低 {cl['fee_min']:.0f} 元）、"
        f"證交稅 {cl['tax_sell']:.4f}、滑價 {cl['slippage_bps']:.0f} bp、整股 {cl['lot']} 股、"
        f"20 日均量門檻 {cl['min_volume']:,} 股",
        "",
    ]
    if image_rel:
        out += [f"![權益曲線]({image_rel})", ""]
    if b:
        out += ["## 績效", "", f"**基準：{res.benchmark_label}**（buy & hold，期初同額投入）", "",
                "| 指標 | 策略 | 基準 |", "|---|---|---|"]
        # 用指標名稱配對，不用索引對齊 —— 索引對齊只要 rows 增減一項就會整欄位移
        bench_cells = {
            "總報酬": pct(b.get("total_return", 0)),
            "CAGR": pct(b.get("cagr", 0)),
            "最大回撤": pct(b.get("max_drawdown", 0)),
            "年化波動": pct(b.get("vol", 0)),
            "Sharpe (rf=2%)": f"{b.get('sharpe', 0):.2f}",
        }
        for k, v in rows:
            out.append(f"| {k} | {v} | {bench_cells.get(k, '')} |")
    else:
        out += ["## 績效", "", "| 指標 | 值 |", "|---|---|"]
        for k, v in rows:
            out.append(f"| {k} | {v} |")
    out += [
        "",
        "## 成本",
        "",
        f"- 手續費合計 {money(res.costs.fee)}",
        f"- 證交稅合計 {money(res.costs.tax)}",
        f"- 滑價成本約 {money(res.costs.slippage)}",
        f"- **總計 {money(res.costs.total)}**（占期初資金 {pct(res.costs.total / c['capital'])}）",
        "",
        "## 交易明細（末 25 筆）",
        "",
        "| 標的 | 進場 | 出場 | 進場價 | 出場價 | 股數 | 損益 | 報酬 | 天數 | 原因 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for t in res.trades[-25:]:
        out.append(
            f"| {t.ticker} | {t.entry_date} | {t.exit_date} | {t.entry_price:.2f} | {t.exit_price:.2f} | "
            f"{t.shares:,} | {t.pnl:,.0f} | {t.ret * 100:+.2f}% | {t.days} | {t.reason} |"
        )
    out += ["", f"共 {len(res.trades)} 筆，僅列末 25 筆。", ""]
    return "\n".join(out)


def write_report(res: BacktestResult, outdir: str | Path, tag: str = "run") -> dict:
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    svg = outdir / f"{tag}.svg"
    svg.write_text(svg_chart(res, title=f"{res.config['strategy']}  {res.config['start']} ~ {res.config['end']}"), encoding="utf-8")
    md = outdir / f"{tag}.md"
    md.write_text(markdown_report(res, image_rel=svg.name), encoding="utf-8")
    return {"svg": svg, "md": md}
