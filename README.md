# twquant — 台股量化回測工具包

[![tests](https://github.com/EasonLai96/twquant/actions/workflows/tests.yml/badge.svg)](https://github.com/EasonLai96/twquant/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![dependencies](https://img.shields.io/badge/dependencies-0-brightgreen.svg)](pyproject.toml)

零外部依賴（只吃 Python 3.11+ 標準庫），資料源用 [FinMind](https://finmind.github.io/)，落地 SQLite 快取。
目標不是給你一組會賺錢的參數，是給你一個**不會騙你**的回測環境。

```
python -m twquant backtest --strategy trend_ma --fast 20 --slow 60
```

## 驗證狀態（先講清楚，再談功能）

| 項目 | 狀態 |
|---|---|
| 單元測試（41 個，合成資料） | 通過 |
| GitHub Actions CI（3.11 / 3.12 / 3.13） | 設定完成 |
| 真實台股資料（2015-01-05 ~ 2026-10-16，2872 交易日） | 端到端跑過 |
| 一次獨立稽核（29 個測試 + 逐檔審查） | 完成，抓出 12 個 bug 並修掉 |
| **與第三方回測器逐筆對帳** | **尚未做** |

倒數第二項值得說明：這個專案在 0.1.1 做過一次專門找「安靜地給錯答案」的稽核 —— 那類 bug
不會丟例外、回測照樣跑完、報表照樣產出，只有數字是錯的。抓到的包含分割被當成 -75% 假虧損、
FinMind 回傳價格全為 0 的 bar、交易損益漏算買進手續費、報表基準欄整欄錯位一列。
細節在 [CHANGELOG](CHANGELOG.md)，每一個都有對應的回歸測試。

最後一項仍是重點。這包的數字目前只有內部一致性（41 個測試釘住成交價、成本、漲跌停、零股、
權益恆等式、分割一致性），但**沒有跟 backtrader / vectorbt 跑同一份資料對過**。
在對帳完成之前，把它當成「一個透明、可審計、已知限制寫清楚的框架」，而不是「權威的績效來源」。

如果你要拿去做正式研究，建議先做這件事：用 `--json` 匯出逐筆交易，跟另一套引擎的同參數輸出
比對進出場日期與價位。對不上就是有 bug，回報 issue。

## 為什麼自己寫而不用現成框架

現成的台股 repo 常見三種問題：

1. **未來函數**：用當天收盤價決定當天買進。回測好看，實單做不到。
2. **成本當空氣**：不扣手續費、證交稅、滑價，也不管整股交易與漲跌停。
3. **調參當績效**：拿整段歷史挑最佳參數，然後宣稱那個數字是策略報酬。

這包把三件事都堵住：訊號一律 **T 日收盤產生、T+1 開盤成交**；成本全部建模；內建 walk-forward 把 in-sample 與 out-of-sample 攤開來對照。

## 安裝

不需要 `pip install` 任何東西：

```bash
git clone https://github.com/EasonLai96/twquant.git
cd twquant
python -m twquant --help
```

想當套件用（會裝 `twquant` 指令）：

```bash
pip install -e .
twquant --version
```

先看 demo，不用網路也不用 token：

```bash
python examples/demo.py
```

要更高的 FinMind 額度（免 token 約 300 次/小時）：

```bash
export FINMIND_TOKEN="your_token"      # Windows: $env:FINMIND_TOKEN = "your_token"
```

## 實際跑出來長什麼樣

2015-01-05 ~ 2026-10-16、10 檔台股大型股、100 萬起始資金、trend_ma(20/60)：

```
  期末權益      926,418   (-7.36%)          CAGR -0.65%   最大回撤 -16.78%
   Sharpe        -0.56                     平均曝險 +9.90%
  交易筆數      86   勝率 +34.88%   平均持有 22 天
  成本總計      102,910  (手續費 30,883 / 稅 53,967 / 滑價 18,060)

  基準 buy&hold 0050   報酬 +549.53%   CAGR +26.92%   MDD -36.38%   Sharpe 0.80
```

**這個策略輸給買進持有 0050 超過五倍。** 兩個原因寫在數字裡：平均曝險只有 9.9%（簡單均線交叉在台股大型股上一年只出現幾次，其他時間都在空手），以及 86 筆交易付掉 10.3 萬成本。11 年下來費用就吃掉 10% 本金。

同一組資料跑 walk-forward：

```
  fold 數 8   IS 平均報酬 +10.07%   OS 平均報酬 -0.76%
  IS-OS 落差 +10.83%
  OS 勝率 +50.00%（4/8 段賺錢）   OS 平均 Sharpe -0.63
  OS 串接總報酬 -6.28%   最差單段回撤 -9.37%
  被選中的參數組合數 3（8 個 fold 換了 3 組參數）
```

這才是這包真正要給你的東西：**訓練集 +10%，樣本外 -0.76%。** 任何只報 in-sample 數字的回測都會把這個策略講成賺錢的。`breakout` 策略好一點（11 年 +15.85%，306 筆交易付掉 31.7 萬成本），同樣大輸買進持有。

> 註：`breakout` 的數字在 0.1.1 → 0.1.2 之間從 +14.45% 變成 +15.85%。原因不是策略變好，而是它會產生加碼單，而引擎在加碼後用「第一次買進價」結算損益，把損益算錯了。修掉之後才是真數字。

結論不是「這包沒用」，而是「台股大型股上的簡單技術面擇時沒有 edge」——這件事要花一個下午自己驗證，比看十篇回測 +35% 的貼文有價值。

## 快速開始

```bash
# 1. 抓日 K（存到 data/twstock.sqlite；重複執行不會重打 API）
python -m twquant fetch --start 2015-01-01 --end 2026-10-17

# 2. 先做資料健檢，比事後猜績效為什麼怪快得多
python -m twquant doctor

# 3. 回測
python -m twquant backtest --strategy trend_ma --fast 20 --slow 60 --trades

# 4. 看圖（把 out/ 用 HTTP 開出來，瀏覽器直接讀 SVG）
python -m twquant serve --port 8899

# 5. 誠實檢查：滾動樣本外
python -m twquant walkforward --start 2015-01-01 --end 2026-10-17 --json
```

## 指令

| 指令 | 用途 |
|---|---|
| `fetch` | 抓日 K 進 SQLite。`--force` 強制重抓，`--etf` 換成 ETF 組合，`--tickers 2330,2317` 自訂 |
| `doctor` | 掃價格斷點（分割/減資/還原價不連續）與零成交量日 |
| `backtest` | 跑回測，輸出終端摘要 + `out/<tag>.md` + `out/<tag>.svg`；`--json` 吐完整明細 |
| `sweep` | 參數掃描。**結果是 in-sample 的**，只用來找方向，不要直接拿去用 |
| `walkforward` | 滾動 IS/OS 驗證，看參數到底有沒有樣本外能力 |
| `universe` | 看本地快取了哪些標的、日均量多少 |
| `serve` | 把 `out/` 用 HTTP 開出來看圖 |

## 台股摩擦是怎麼建模的

| 項目 | 預設 | 說明 |
|---|---|---|
| 手續費 | 0.1425% × 6 折，最低 20 元 | `--fee-discount` `--fee-min` 可調；零股最低 1 元請自行改 |
| 證交稅 | 賣出 0.3% | `--tax`（當沖減半可設 0.0015） |
| 滑價 | 5 bp | 訊號用收盤、成交用開盤，開盤市價單不可能拿到完美價 |
| 整股交易 | 1000 股為單位 | 買不滿一張就不買 |
| 漲跌停 | 距前收 ±9.5% + 開盤貼當日極值 + 當日振幅≈0 | 台股漲跌停是「鎖住」而非「跌了 10%」，盤中打開就成交得到。只用幅度判斷會把除權息跳空誤殺 |
| 停牌 | 成交量 0 不成交 | |
| 流動性門檻 | 20 日均量 < 200 萬股跳過 | `--min-volume` |
| 分割/減資 | 單日跳動 > 20% 視為結構性事件，當日報酬歸零 | 台股有漲跌幅限制，正常交易日不可能有這種幅度。0050 在 2025-06-18 做 4:1 分割，不處理的話基準曲線會出現 -75% 的假崩盤 |

除權息的處理放在 `twquant/loader.py`：`extract_factors()` 從原價與還原價兩條序列抽出還原因子並偵測斷點，`load_adj_returns()` 據此產生「分割安全」的日報酬。**FinMind 的 `adjust=1` 目前實際上是 no-op**（回傳原價），所以這件事不能交給 API 做。

## 策略

兩個都寫在 `twquant/strategies.py`，實作 `on_bar` 介面，全新策略大概 30 行就能掛上去。

**`trend_ma`** — 雙均線趨勢
- 進場：快線由下往上穿越慢線，且慢線本身走揚
- 出場：收盤跌破快線 / 慢線上彎轉下且價格在慢線下 / ATR 移動停損 / 不滿最短持有天數不動作
- 參數：`--fast` `--slow` `--atr-n` `--trail-atr` `--confirm-days` `--max-weight`

**`breakout`** — 動能突破
- 進場：突破前 20 日高點且量能 > 20 日均量 1.5 倍
- 出場：跌破 20MA 或持有滿 `--max-hold` 個交易日
- 參數：`--lookback` `--vol-mult` `--max-hold` `--exit-ma`

自訂策略只要回傳：

```python
StrategyResponse(
    orders=[OrderIntent("2330", "sell", "訊號說明")],   # 想賣的
    allocation={"2317": 0.25, "2454": 0.25},            # 想持有的目標權重
)
```

沒列進 `allocation` 的部位會被當成目標 0 而清掉 —— 這是刻意的，避免「忘了列」造成幽靈持股。

## Walk-forward 怎麼讀

實跑輸出（就是上面那組 8 個 fold）：

```
  fold 數 8   IS 平均報酬 +11.10%   OS 平均報酬 -1.55%
  IS-OS 落差 +12.66%
  OS 勝率 +25.00%（2/8 段賺錢）   OS 平均 Sharpe -0.85
  被選中的參數組合數 5（8 個 fold 換了 5 組參數）
```

- **IS-OS 落差**才是重點。這裡落差 12.66 個百分點，等於「訓練期賺的，出了訓練期就還回去」。
- **OS 勝率**低於 50% = 這策略連「大部分時間能賺」都做不到。這裡只有 25%。
- **參數一直換**（8 個 fold 挑了 5 組）代表沒有一個穩定的參數區域，挑到的都是雜訊。
- 判讀順序：先看 IS-OS 落差，再看 OS 勝率，最後才看 OS 的絕對報酬。三者都過關，`sweep` 出來的漂亮數字才值得再看一眼。

## 專案結構

```
twquant/
├── twquant/
│   ├── finmind.py      FinMind v4 client（stdlib urllib，含額度錯誤處理）
│   ├── store.py        SQLite 快取 + fetch_log + 還原因子表 + 資料健檢
│   ├── loader.py       Bar 結構、預設宇宙、技術指標（SMA/EMA/ATR/RSI）、分割安全日報酬
│   ├── strategy.py     策略介面與 MarketContext
│   ├── strategies.py   trend_ma / breakout
│   ├── engine.py       回測引擎（成本、漲跌停、整股、權益快照、績效指標）
│   ├── walkforward.py  滾動 IS/OS 驗證
│   ├── report.py       終端摘要 / Markdown / 手刻 SVG 權益與回撤圖
│   └── cli.py          命令列
└── tests/              17 個單元測試（合成資料，不碰網路）
```

跑測試：

```bash
python -m unittest discover -s tests -t .
```

測的是引擎的正確性而不是策略績效：沒有未來函數（成交價必須是訊號次日的開盤）、成本有扣、鎖死漲停與無量不成交、漲停打開時成交得到、不出現零股、權益恆等式（現金 + 市值 = 權益）成立、最大回撤算得對、日期索引與前一根 bar 的查找正確。

最後一項特別重要：開發過程中 `_idx()` 與 `_prev_bar()` 各有一個 off-by-one，症狀是「回測跑完 0 筆交易」跟「漲跌停判斷永遠失效」——都是那種不會丟例外、只會安靜給你錯答案的錯。它們現在各有一組專門的測試釘住。

## 五分鐘路線

```bash
python -m twquant fetch --start 2015-01-01 --end 2026-10-17   # 抓資料（12 檔約 1 分鐘）
python -m twquant doctor                                       # 看有沒有資料斷點
python -m twquant backtest --strategy trend_ma --fast 20 --slow 60 --trades
python -m twquant serve --port 8899                            # 瀏覽器開 http://127.0.0.1:8899/
python -m twquant walkforward --start 2015-01-01 --end 2026-10-17
```

## 已知限制

- **只做日線**。要當沖或分 K 請自己接 `TaiwanStockKBar`，引擎的日頻假設要改。
- **不做放空**。台股借券成本與券源限制不是能憑空建模的東西，硬做會產生假績效。
- **不含下單**。實單請接 [Shioaji](https://github.com/Sinotrade/Shioaji)，本包輸出的是決策，不是委託。
- FinMind 免 token 有請求上限，`fetch` 失敗會明確告訴你是額度問題而不是默默吞掉。

### 已知但尚未修的問題

這些是稽核後確認存在、但刻意先記下來而不是硬修的東西。列出來是為了讓你踩到時知道那是已知的，不是你在狀況外：

- **資金配置與訂單順序有關**：`_plan` 對每個買進目標都用「當下總現金」估算部位大小，
  所以多標的同一天進場時，先執行到的會先吃掉現金。不會透支（成交時有二次檢查），
  但結果不確定。想精確控制請自己算股數用 `OrderIntent(shares=...)`。
- **結構性斷點當天的漲跌會被丟掉**：分割/減資日直接把該日報酬歸零（保守），
  所以那天真正的價格變動不計入績效。寧可少賺也不製造假崩盤。
- **沒有成交量以外的流動性模型**：不看委買委賣深度、不看零股市場，
  大資金回測的實際滑價會高於設定值。
- **尚未與第三方回測器逐筆對帳**：見上面「驗證狀態」。

## 接下來可以往哪走

1. 把 `sweep` 的最佳參數丟進 `walkforward` 驗，落差超過 20% 就當作沒找到 edge。
2. 接籌碼面（`twchips` 那類）當第二個訊號源，測「技術面 + 法人買超」的組合。
3. 接 Shioaji 模擬環境，用同一份 `StrategyResponse` 下單 —— 訊號邏輯完全不用改。
