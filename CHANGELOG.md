# Changelog

本專案採用 [Semantic Versioning](https://semver.org/lang/zh-TW/)。

## [0.1.1] - 2026-10-18

一次針對「安靜地給錯答案」的稽核與修正。這一輪的 bug 有一個共同特徵：**都不會丟例外**，
回測照樣跑完、報表照樣產出，只是數字是錯的。

### 修正 — 會直接汙染績效的

- **分割／減資被當成 -75% 的假虧損**（`engine.py`）：`last_close` 用「連乘日報酬」推導，
  分割日報酬是 0 會讓它停在舊價位，之後每天重複套用同一個換算比例，
  `entry_price` 被逐日乘 4 倍、最後炸成 `inf`。改成每個部位自己記「價格基準」，
  在重新定價那天把進場價與成本搬過去，並加上明確的有限性檢查。
  在此之前，任何帶分割的標的在回測裡都是假的。
- **FinMind 會回傳價格全是 0 的 bar**（`loader.py`、`store.py`）：006208 的早期資料有
  152 根這種 bar，2317 有 2 根。這種 bar 進了引擎會讓「以價格為分母」的計算
  爆掉或算出無限大的部位。現在在載入時就濾掉。
- **交易損益沒有包含買進手續費**（`engine.py`）：交易紀錄的損益總和對不上權益曲線的變化，
  差額剛好是去程手續費。勝率、獲利因子因此被高估。
- **`upsert_bars` 只要成交量轉不成整數就丟掉整根價格 bar**（`store.py`）：
  成交量是 `"1,234"`、`"-"`、`None` 時，OHLC 明明是好的卻整筆消失，形成沒有訊息的資料缺口。
  現在成交量獨立解析，壞值寫 0 但保留價格。
- **Markdown 報表基準欄整欄錯位一列**（`report.py`）：`brows` 的第一個元素是標題列，
  卻被拿去跟 `rows[0]`（期末權益）配對，於是「年化波動」右邊印的是「基準 Sharpe」。
  改成用指標名稱配對，不再依賴索引對齊。
- **指定基準抓不到時靜默降級成等權**（`engine.py`、`cli.py`）：`--benchmark 0050`
  在 `--tickers` 模式下不會自動補上基準檔，卻默默改成等權買進持有，
  報表仍寫著「基準 0050」。現在標籤跟著實際用的基準走，並且會對 stderr 發警告。

### 修正 — 會讓工具失效的

- **walkforward 的 `--metric` 拼錯時，第一組參數永遠勝出**（`walkforward.py`）：
  每組都拿到哨兵值 `-9e9`，而 `best_metrics is None` 讓第一組無條件成為 best。
  整個 walk-forward 的賣點直接失效，輸出看起來完全正常。現在驗證 metric 白名單，
  CLI 也加了 `choices`。
- **sweep／walkforward 與 backtest 用不同的交易宇宙**（`cli.py`）：
  前兩者把 0050、006208 也當成可交易標的，同一組參數在三個指令會給出三種績效，
  而且策略可以買賣自己的基準。現在共用 `_trade_universe()`。
- **一次抓失敗會被永久記成「已抓」**（`store.py`）：`fetch_log.rows = 0` 時
  `_needs_fetch` 仍可能回報已覆蓋，之後每次執行都重打同一段 API，默默吃掉免 token 的
  300 req/hr 額度。現在只看「有沒有真的入庫的資料」。
- **`_covered()` 與 `_needs_fetch()` 對同一筆紀錄給出相反答案**：移除已無人呼叫的 `_covered()`。
- **HTTP 402/403 沒有轉成 `QuotaExceeded`**（`finmind.py`）：額度用盡時 CLI 的
  「設定 FINMIND_TOKEN」提示不會出現。順便讓 429 尊重 `Retry-After`，
  不再在被限流時連補三槍。
- **`--tickers ""` 被當成沒填**（`cli.py`）：靜默換成 12 檔預設宇宙，現在直接報錯。
- **walkforward 全滅時回傳空 dict，讓 CLI 噴 `KeyError: 'folds'`**（`walkforward.py`）：
  使用者看到與根因無關的錯誤。現在記錄第一筆失敗原因並拋出帶摘要的例外。
- **`quality()` 永遠不檢查第一根 bar**（`store.py`）：零成交量天數少算 1。
- **部分出場沒辦法表達**（`strategy.py`、`engine.py`）：任何 sell 都會整筆清掉。
  `OrderIntent` 新增 `shares` 欄位支援部分出場，引擎也正確按比例分攤去程手續費。
- **同一檔被「清倉」與「策略賣出」同時命中時產生兩筆賣單**（`engine.py`）：
  權益不會錯（第二筆會被跳過），但訂單清單與日誌會出現幽靈訂單，接實單就是重複委託。

### 測試

- 從 17 個增加到 41 個。新增兩組：
  - `tests/test_consistency.py` — 跨機制對帳（交易損益總和 vs 權益曲線變化、權益恆等式）、
    分割一致性、漲跌停邊界、現金不得為負、部分出場、髒資料
  - `tests/test_regressions.py` — 上述每一個 bug 各一支回歸測試

### 已知問題（尚未修，已記錄）

- 配置資金時對每個目標都用「當下總現金」估部位，所以多標的同時進場時，
  先執行到的會先吃掉現金。不會透支（執行時有二次檢查），但配置結果與順序有關。
- 分割日直接丟棄當日報酬（保守），因此那天真正的漲跌不會計入績效。
- 沒有基準標的又同時在交易宇宙裡時，基準曲線會等於該標的；請用 `--tickers` 自行排除。

## [0.1.0] - 2026-10-18

首個可用的版本。核心目標：把台股的真實摩擦建模進去，並且內建能拆自己台的驗證工具。

### 加入

- **回測引擎**（`twquant/engine.py`）
  - 訊號 T 日收盤產生、T+1 開盤成交，架構上無法寫出未來函數
  - 手續費（可設折扣與最低收費）、證交稅、滑價、整股 1000 股、流動性門檻
  - 漲跌停判定：幅度 + 開盤貼當日極值 + 當日振幅≈0，避免把除權息跳空誤判成鎖死
  - 成交量 0 不成交；多標的組合、目標權重配置、清倉語意明確
  - 績效指標：CAGR、最大回撤（含峰谷日期）、年化波動、Sharpe、Calmar、曝險、勝率、獲利因子
  - 基準曲線用日報酬連乘，分割/減資不會讓基準整條歪掉
- **資料層**（`twquant/finmind.py`、`twquant/store.py`、`twquant/loader.py`）
  - FinMind v4 client（純 stdlib urllib），額度不足有明確錯誤而非靜默失敗
  - SQLite 快取 + fetch log，重複執行不重打 API
  - 還原因子抽取與斷點偵測；`load_adj_returns()` 產生「分割安全」日報酬
  - 技術指標：SMA / EMA / ATR / RSI / rolling high / rolling std
- **策略**（`twquant/strategies.py`）：`trend_ma`、`breakout`，共用 `on_bar` 介面
- **Walk-forward**（`twquant/walkforward.py`）：滾動 IS/OS、參數穩定度統計
- **報表**（`twquant/report.py`）：終端摘要、Markdown、手繪 SVG 權益與回撤圖
- **CLI**：`fetch` / `doctor` / `backtest` / `sweep` / `walkforward` / `universe` / `serve`
- **測試**：17 個單元測試（合成資料、不碰網路）與 GitHub Actions CI
- **範例**：`examples/demo.py` 零安裝零網路的完整流程示範

### 修正（開發過程中發現，都是不會丟例外的靜默錯誤）

- `_idx()` 的 off-by-one 讓訊號日永遠對不上，回測跑出 0 筆交易
- `_prev_bar()` 誤用 `bisect_right`，回傳「當天」而非「前一根」，導致漲跌停判定永遠失效
- FinMind 的 `adjust=1` 實測為 no-op（回傳原價），原本依賴它處理除權息會產生假的 -75% 崩盤

### 已知限制

- 只支援日線，無分 K 與當沖
- 不支援放空（借券成本與券源無法憑空建模）
- 不含下單，實單需自行接券商 API
- 尚未與第三方回測器（backtrader / vectorbt）做逐筆對帳
