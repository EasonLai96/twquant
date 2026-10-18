"""SQLite 快取。台股資料打 API 有額度，回測一定要落地。

Freshness 規則：抓過的日期區間記在 fetch_log，之後只要 requested end <= 已抓且資料已定案
（end 早於今天 - SETTLE_DAYS）就完全不重打。
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

from .finmind import Client

SCHEMA = """
CREATE TABLE IF NOT EXISTS bars (
  ticker TEXT NOT NULL,
  date   TEXT NOT NULL,
  open REAL, high REAL, low REAL, close REAL,
  volume INTEGER,
  PRIMARY KEY (ticker, date)
);
CREATE TABLE IF NOT EXISTS fetch_log (
  ticker TEXT NOT NULL,
  start  TEXT NOT NULL,
  end    TEXT NOT NULL,
  adj    INTEGER NOT NULL,
  fetched_at TEXT NOT NULL,
  rows INTEGER,
  PRIMARY KEY (ticker, start, end, adj)
);
CREATE TABLE IF NOT EXISTS factors (
  ticker TEXT NOT NULL,
  date   TEXT NOT NULL,
  factor REAL NOT NULL,
  PRIMARY KEY (ticker, date)
);
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT
);
"""

SETTLE_DAYS = 1


def _vol(x) -> int:
    """成交量轉整數。轉不出來就當 0，但**不要**因此丟掉整根價格 bar。

    FinMind 偶爾回傳 "1,234"、"−"、"" 這類值；價格欄位是好的，只因成交量就
    讓該交易日消失，會變成一個沒有任何訊息的資料缺口。
    """
    if x is None or x == "" or x == "-":
        return 0
    if isinstance(x, str):
        x = x.replace(",", "").strip()
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return 0


def _today() -> str:
    return dt.date.today().isoformat()


class Store:
    def __init__(self, path: str | Path, client: Client | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.executescript(SCHEMA)
        self.client = client or Client()

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _needs_fetch(self, ticker: str, start: str, end: str, adj: bool, force: bool) -> tuple[bool, bool]:
        """回傳 (要不要抓價, 要不要抽因子)。

        判斷依據只有一個：這段區間有沒有真的入庫過（fetch_log.rows > 0）。
        不能用「end 是否已定案」當條件 —— 那會讓一次抓失敗（rows=0，例如被限流）
        在之後每次執行都重打同一段 API，默默吃掉免 token 的 300 req/hr 額度。
        """
        if force:
            return True, True
        adj = bool(adj)
        row = self.db.execute(
            "SELECT rows FROM fetch_log WHERE ticker=? AND start<=? AND end>=? AND adj=? "
            "ORDER BY end DESC LIMIT 1",
            (ticker, start, end, int(adj)),
        ).fetchone()
        covered = bool(row) and (row[0] or 0) > 0
        have_factors = self.db.execute(
            "SELECT 1 FROM factors WHERE ticker=? LIMIT 1", (ticker,)
        ).fetchone()
        return (not covered), (not have_factors)

    def fetch_bars(self, ticker: str, start: str, end: str, adj: bool = True, force: bool = False) -> int:
        need_price, need_factor = self._needs_fetch(ticker, start, end, adj, force)
        if not need_price and not need_factor:
            return 0
        rows = self.client.price(ticker, start, end, adj=adj) if (need_price or need_factor) else []
        if rows:
            self.upsert_bars(ticker, rows)
        if need_factor and rows:
            try:
                raw = self.client.price(ticker, start, end, adj=False)
            except Exception:  # noqa: BLE001 - 沒因子頂多少一次校正，不該讓整檔抓失敗
                raw = []
            if raw:
                self.upsert_factors(ticker, raw, rows)
        self.db.execute(
            "REPLACE INTO fetch_log(ticker,start,end,adj,fetched_at,rows) VALUES(?,?,?,?,?,?)",
            (ticker, start, end, int(adj), dt.datetime.now().isoformat(timespec="seconds"), len(rows)),
        )
        self.db.commit()
        return len(rows)

    def upsert_factors(self, ticker: str, raw_rows: list[dict], adj_rows: list[dict]) -> int:
        from .loader import extract_factors

        raw = {str(r["date"])[:10]: float(r["close"]) for r in raw_rows if r.get("close")}
        adjc = {str(r["date"])[:10]: float(r["close"]) for r in adj_rows if r.get("close")}
        factors = extract_factors(raw, adjc)
        if not factors:
            return 0
        self.db.executemany(
            "INSERT OR REPLACE INTO factors(ticker,date,factor) VALUES(?,?,?)",
            [(ticker, d, f) for d, f in factors.items()],
        )
        self.db.commit()
        return len(factors)

    def load_factors(self, ticker: str, start: str, end: str) -> dict[str, float]:
        return {
            d: f
            for d, f in self.db.execute(
                "SELECT date,factor FROM factors WHERE ticker=? AND date>=? AND date<=? ORDER BY date",
                (ticker, start, end),
            )
        }

    def factor_breaks(self, ticker: str) -> list[tuple[str, float, float]]:
        rows = self.db.execute(
            "SELECT date,factor FROM factors WHERE ticker=? ORDER BY date", (ticker,)
        ).fetchall()
        out = []
        for i in range(1, len(rows)):
            if rows[i][1] != rows[i - 1][1]:
                out.append((rows[i][0], rows[i - 1][1], rows[i][1]))
        return out

    def upsert_bars(self, ticker: str, rows: list[dict]) -> int:
        payload = []
        for r in rows:
            try:
                payload.append(
                    (
                        ticker,
                        str(r["date"])[:10],
                        float(r["open"]),
                        float(r["max"]),
                        float(r["min"]),
                        float(r["close"]),
                        _vol(r.get("Trading_Volume")),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        self.db.executemany(
            "INSERT OR REPLACE INTO bars(ticker,date,open,high,low,close,volume) VALUES(?,?,?,?,?,?,?)",
            payload,
        )
        self.db.commit()
        return len(payload)

    def load_bars(self, ticker: str, start: str, end: str) -> list[tuple]:
        return self.db.execute(
            "SELECT date,open,high,low,close,volume FROM bars "
            "WHERE ticker=? AND date>=? AND date<=? ORDER BY date",
            (ticker, start, end),
        ).fetchall()

    def tickers(self) -> list[str]:
        return [r[0] for r in self.db.execute("SELECT DISTINCT ticker FROM bars ORDER BY ticker")]

    def quality(self, jump_threshold: float = 0.3, top: int = 12) -> dict:
        """掃出可疑的價格斷點。

        FinMind 的還原價對少數標的會出現不連續（曾看過某檔憑空 +109%），
        這種資料餵進回測會直接產生假的績效，所以寧可先掃出來人工判斷。
        """
        report: dict[str, dict] = {}
        for t in self.tickers():
            rows = self.db.execute("SELECT date,close,volume FROM bars WHERE ticker=? ORDER BY date", (t,)).fetchall()
            jumps = []
            zero_vol = 0
            for i in range(len(rows)):
                if (rows[i][2] or 0) <= 0:
                    zero_vol += 1
                if i == 0:
                    continue
                p, c = rows[i - 1][1], rows[i][1]
                if p > 0 and c > 0:
                    r = c / p - 1
                    if abs(r) >= jump_threshold:
                        jumps.append({"date": rows[i][0], "from": p, "to": c, "chg": r})
            if jumps or zero_vol:
                report[t] = {"rows": len(rows), "jumps": jumps, "zero_volume_days": zero_vol}
        return report

    def set_meta(self, key: str, value: str) -> None:
        self.db.execute("REPLACE INTO meta(key,value) VALUES(?,?)", (key, value))
        self.db.commit()

    def get_meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None
