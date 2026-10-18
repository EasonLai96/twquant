"""實單的持久化與稽核。

state.sqlite 存「現在的狀態」，events.jsonl 存「發生過什麼」。兩者都要，缺一不可：
狀態用來重啟後續跑，事件用來事後查帳。事件是 append-only，任何一次寫入都不會覆蓋歷史。
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
from dataclasses import asdict, is_dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT
);
CREATE TABLE IF NOT EXISTS positions (
  ticker TEXT PRIMARY KEY,
  shares INTEGER NOT NULL,
  cost_basis REAL NOT NULL,
  entry_date TEXT,
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS orders (
  order_id TEXT PRIMARY KEY,
  ts TEXT, ticker TEXT, side TEXT, shares INTEGER,
  status TEXT, filled_shares INTEGER, filled_price REAL,
  fee REAL, tax REAL, reason TEXT, message TEXT
);
"""


class State:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "state.sqlite")
        self.db.executescript(SCHEMA)
        self.events_path = self.root / "events.jsonl"

    def close(self) -> None:
        self.db.close()

    # --- 稽核事件 ----------------------------------------------------------

    def log(self, event: str, **fields) -> None:
        rec = {"ts": dt.datetime.now().isoformat(timespec="seconds"), "event": event}
        for k, v in fields.items():
            rec[k] = asdict(v) if is_dataclass(v) else v
        with self.events_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")

    def tail(self, n: int = 20) -> list[dict]:
        if not self.events_path.exists():
            return []
        lines = self.events_path.read_text(encoding="utf-8").splitlines()[-n:]
        out = []
        for ln in lines:
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                continue
        return out

    # --- 狀態 --------------------------------------------------------------

    def set_meta(self, key: str, value) -> None:
        self.db.execute(
            "REPLACE INTO meta(key,value) VALUES(?,?)",
            (key, json.dumps(value, ensure_ascii=False, default=str)),
        )
        self.db.commit()

    def get_meta(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row[0])
        except json.JSONDecodeError:
            return default

    def save_positions(self, broker_holdings: dict, entry_dates: dict[str, str]) -> None:
        now = dt.datetime.now().isoformat(timespec="seconds")
        self.db.execute("DELETE FROM positions")
        rows = [
            (t, h.shares, h.avg_price * h.shares, entry_dates.get(t, ""), now)
            for t, h in broker_holdings.items()
            if h.shares > 0
        ]
        self.db.executemany(
            "INSERT OR REPLACE INTO positions(ticker,shares,cost_basis,entry_date,updated_at) "
            "VALUES(?,?,?,?,?)",
            rows,
        )
        self.db.commit()

    def load_positions(self) -> list[dict]:
        cur = self.db.execute(
            "SELECT ticker,shares,cost_basis,entry_date FROM positions ORDER BY ticker"
        )
        return [
            {"ticker": r[0], "shares": r[1], "cost_basis": r[2], "entry_date": r[3]} for r in cur
        ]

    def record_orders(self, orders: list) -> None:
        now = dt.datetime.now().isoformat(timespec="seconds")
        rows = [
            (
                o.order_id, o.ts or now, o.ticker, o.side, o.shares, o.status,
                o.filled_shares, o.filled_price, o.fee, o.tax, o.reason, o.message,
            )
            for o in orders
        ]
        self.db.executemany(
            "INSERT OR REPLACE INTO orders(order_id,ts,ticker,side,shares,status,filled_shares,"
            "filled_price,fee,tax,reason,message) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        self.db.commit()

    def orders(self, limit: int = 50) -> list[dict]:
        cur = self.db.execute(
            "SELECT ts,ticker,side,shares,status,filled_price,reason,message FROM orders "
            "ORDER BY ts DESC LIMIT ?",
            (limit,),
        )
        return [
            {
                "ts": r[0], "ticker": r[1], "side": r[2], "shares": r[3],
                "status": r[4], "price": r[5], "reason": r[6], "message": r[7],
            }
            for r in cur
        ]
