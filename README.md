# twquant — 台股量化回測工具包

[![tests](https://github.com/EasonLai96/twquant/actions/workflows/tests.yml/badge.svg)](https://github.com/EasonLai96/twquant/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![dependencies](https://img.shields.io/badge/dependencies-0-brightgreen.svg)](pyproject.toml)

零外部依賴，只吃 Python 3.11+ 標準庫。資料從 [FinMind](https://finmind.github.io/) 抓，落 SQLite。

```bash
python -m twquant backtest --strategy trend_ma --fast 20 --slow 60
```

## 先講清楚這包的狀態

50 個單元測試、CI 跑 3.11/3.12/3.13、11 年真實台股資料端到端跑過。另外做過兩輪獨立的
對抗式複核，修掉 18 個 bug——那些 bug 的共同點是不會丟例外，回測照樣跑完、報表照樣產出，
只有數字是錯的。細節在 [CHANGELOG](CHANGELOG.md)，每個都有回歸測試。

還沒有做的是跟第三方回測器逐筆對帳。所以這包的數字目前只有內部一致性（測試釘住成交價、
成本、漲跌停、零股、權益恆等式、跨分割的損益守恆），沒有跟 backtrader 或 vectorbt
跑同一份資料對過。要拿去正式研究的話，先用 `--json` 匯出逐筆交易跟另一套引擎比對
進出場日期與價位，對不上就是有 bug。

## 安裝

```bash
git clone https://github.com/EasonLai96/twquant.git
cd twquant
python -m twquant --help
```

想裝成指令：`pip install -e .`

不想先申請 token 的話，`python examples/demo.py` 用合成資料跑完整流程，不連網。

FinMind 免 token 約 300 次/小時，要更多就設 `FINMIND_TOKEN`。

## 跑起來長什麼樣

2015-01-05 到 2026-10-16、10 檔台股大型股、100 萬本金、trend_ma(20/60)：

```
期末權益      926,045   (-7.40%)       CAGR -0.65%   最大回撤 -16.78%
Sharpe        -0.56                  平均曝險 +9.90%
交易筆數      86   勝率 +34.88%   平均持有 22 天
成本總計      103,055

基準 buy&hold 0050   報酬 +549.53%   CAGR +26.92%   MDD -36.38%   Sharpe 0.80
```

策略輸給買進持有 0050 超過五倍。原因寫在數字裡：平均曝險只有 9.9%，因為簡單均線交叉
在大型股上一年只出現幾次，其他時間都在空手；86 筆交易付掉 10.3 萬成本，11 年吃掉 10% 本金。

同一組資料的 walk-forward：

```
fold 數 8   IS 平均報酬 +9.76%   OS 平均報酬 -1.12%
IS-OS 落差 +10.89%
OS 勝率 +50.00%（4/8 段賺錢）   OS 平均 Sharpe -0.82
OS 串接總報酬 -8.93%   最差單段回撤 -9.00%
被選中的參數組合數 4
```

訓練期賺 9.76%，出了訓練期變成 -1.12%。判斷這個落差比看 in-sample 報酬重要得多：
先看 IS-OS 落差，再看 OS 勝率有沒有過半，最後才看 OS 的絕對數字。三者都過關，
`sweep` 挑出來的漂亮數字才值得再看一眼。

`breakout` 好一點，11 年 +12.89%，但 306 筆交易付掉 33.5 萬成本，同樣大輸買進持有。
`breakout` 的數字在 0.1.1 到 0.1.3 之間從 +14.45% 修正到 +12.89%，因為它會產生加碼單，
而引擎當時在加碼後用第一次買進價結算損益、跨分割的成本帳又用錯座標。

這兩個策略都沒有 edge。台股大型股上的簡單技術面擇時就是這樣，要花一個下午自己驗證，
比看十篇回測 +35% 的貼文有價值。

## 快速開始

```bash
python -m twquant fetch --start 2015-01-01 --end 2026-10-17   # 抓資料，重跑不會重打 API
python -m twquant doctor                                       # 先看資料有沒有斷點
python -m twquant backtest --strategy trend_ma --fast 20 --slow 60 --trades
python -m twquant serve --port 8899                            # http://127.0.0.1:8899/
python -m twquant walkforward --start 2015-01-01 --end 2026-10-17
```

| 指令 | 用途 |
|---|---|
| `fetch` | 抓日 K 進 SQLite。`--force` 重抓，`--etf` 換 ETF 組合，`--tickers 2330,2317` 自訂 |
| `doctor` | 掃價格斷點（分割/減資）與零成交量日 |
| `backtest` | 回測，輸出終端摘要加 `out/<tag>.md`、`out/<tag>.svg`，`--json` 吐完整明細 |
| `sweep` | 參數掃描。結果是 in-sample 的，只用來找方向 |
| `walkforward` | 滾動 IS/OS 驗證 |
| `universe` | 看本地快取了哪些標的、日均量多少 |
| `serve` | 把 `out/` 用 HTTP 開出來看圖 |

## 台股摩擦怎麼建模

手續費 0.1425% 乘 6 折、最低 20 元（`--fee-discount` `--fee-min`）。證交稅賣出 0.3%
（當沖減半設 `--tax 0.0015`）。滑價預設 5bp，因為訊號用收盤、成交用開盤。整股 1000 股為單位，
買不滿一張就不買。

漲跌停判定看三件事：距前收 ±9.5%、開盤價是否貼在當日極值、當日振幅是否幾乎為零。
台股漲跌停是「鎖住」而不是「跌了 10%」，盤中打開就成交得到，只看幅度會把除權息跳空誤殺。
成交量 0 不成交。20 日均量低於 200 萬股跳過（`--min-volume`）。

分割和減資是這包花最多力氣的地方。FinMind 的 `adjust=1` 實測是 no-op（回傳原價），
所以還原要自己做：`loader.py` 偵測單日跳動超過 20% 的結構性斷點，引擎在那些日子
換算股數而不是換算價格——4:1 分割就是 4000 股變 16000 股，現金成本不動。
換算價格會讓市值、賣出收入、損益各站一個座標，平倉時就對不上現金。
0050 在 2025-06-18 做過 4:1 分割，不處理的話基準曲線會出現 -75% 的假崩盤。

損益一律用現金算：賣出淨收入減掉這批股數分攤到的現金成本。這條等式保證
交易紀錄的損益總和永遠等於權益曲線的變化，部分出場也成立。

## 策略

兩個內建策略在 `twquant/strategies.py`，實作 `on_bar` 介面，新策略大概 30 行。

`trend_ma`：快線由下往上穿越慢線且慢線走揚時進場；收盤跌破快線、慢線下彎、或
ATR 移動停損時出場，不滿最短持有天數不動作。參數 `--fast` `--slow` `--atr-n`
`--trail-atr` `--confirm-days` `--max-weight`。

`breakout`：突破前 20 日高點且量能超過 20 日均量 1.5 倍時進場；跌破 20MA 或持有滿
`--max-hold` 個交易日時出場。參數 `--lookback` `--vol-mult` `--max-hold` `--exit-ma`。

自訂策略只要回傳：

```python
StrategyResponse(
    orders=[OrderIntent("2330", "sell", "訊號說明")],   # 想賣的，可加 shares 做部分出場
    allocation={"2317": 0.25, "2454": 0.25},            # 想持有的目標權重
)
```

沒列進 `allocation` 的部位會被當成目標 0 清掉，避免忘了列造成幽靈持股。

## 專案結構

```
twquant/
├── twquant/
│   ├── finmind.py      FinMind v4 client（stdlib urllib，含額度錯誤處理）
│   ├── store.py        SQLite 快取、fetch_log、還原因子表、資料健檢
│   ├── loader.py       Bar、預設宇宙、技術指標、分割安全日報酬
│   ├── strategy.py     策略介面與 MarketContext
│   ├── strategies.py   trend_ma / breakout
│   ├── engine.py       回測引擎（成本、漲跌停、整股、權益快照、績效指標）
│   ├── walkforward.py  滾動 IS/OS 驗證
│   ├── report.py       終端摘要、Markdown、手刻 SVG
│   └── cli.py          命令列
└── tests/              50 個測試，合成資料，不碰網路
```

`python -m unittest discover -s tests -t .`

測的是引擎不是策略績效：成交價必須是訊號次日的開盤、成本有扣、鎖死漲停不成交但打開要成交、
不出現零股、現金加市值等於權益、最大回撤算得對、跨分割的損益守恆、日期索引正確。

最後一項不是客套。開發過程中 `_idx()` 和 `_prev_bar()` 各有一個 off-by-one，症狀是
「回測跑完 0 筆交易」和「漲跌停判斷永遠失效」，兩個都不會丟例外。它們現在各有專門的測試。

## 已知限制

只做日線，要分 K 得自己接 `TaiwanStockKBar` 並改引擎的日頻假設。不做放空，因為借券成本
和券源限制沒辦法憑空建模。不含下單，實單請接 [Shioaji](https://github.com/Sinotrade/Shioaji)，
這包輸出的是決策不是委託。

還有幾個確認存在但先記著沒修的問題：

- 資金配置跟訂單順序有關。`_plan` 對每個買進目標都用當下總現金估算部位大小，所以多標的
  同一天進場時先執行到的會先吃掉現金。不會透支，但結果不確定。要精確控制就用
  `OrderIntent(shares=...)` 自己算股數。
- 結構性斷點當天真正的漲跌會被丟掉。分割日直接歸零是保守做法，寧可少賺也不製造假崩盤。
- 沒有成交量以外的流動性模型。不看委買委賣深度也不看零股市場，大資金的實際滑價會高於設定值。
- 還沒跟第三方回測器對帳。
