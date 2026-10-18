# Changelog

本專案採用 [Semantic Versioning](https://semver.org/lang/zh-TW/)。

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
