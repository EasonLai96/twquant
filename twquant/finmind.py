"""FinMind v4 API client (stdlib only).

Doc: https://finmind.github.io/
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.finmindtrade.com/api/v4/data"

# 免 token 約 300 req/hr，超過會回 402；有 token 走 env 不寫進版控
TOKEN_ENV = "FINMIND_TOKEN"


class FinMindError(RuntimeError):
    pass


class QuotaExceeded(FinMindError):
    pass


class Client:
    def __init__(self, token: str | None = None, retries: int = 3, pause: float = 1.0):
        self.token = token or os.environ.get(TOKEN_ENV) or None
        self.retries = retries
        self.pause = pause

    def get(self, dataset: str, **params) -> list[dict]:
        q = {"dataset": dataset, **{k: v for k, v in params.items() if v is not None}}
        if self.token:
            q["token"] = self.token
        url = f"{API}?{urllib.parse.urlencode(q)}"

        last_err = None
        for attempt in range(self.retries):
            try:
                with urllib.request.urlopen(url, timeout=45) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                status = payload.get("status")
                if status == 200:
                    return payload.get("data", [])
                if status in (402, 403):
                    raise QuotaExceeded(f"FinMind 額度用盡或無權限: {payload.get('msg')}")
                if status == 400:
                    raise FinMindError(f"參數錯誤 dataset={dataset} {params}: {payload.get('msg')}")
                last_err = FinMindError(f"status={status} msg={payload.get('msg')}")
            except QuotaExceeded:
                raise
            except urllib.error.HTTPError as e:
                body = ""
                try:
                    body = e.read().decode("utf-8")[:200]
                except Exception:
                    pass
                if e.code in (400, 401, 402, 403):
                    raise FinMindError(f"HTTP {e.code} dataset={dataset}: {body}") from e
                last_err = e
            except Exception as e:  # noqa: BLE001 - 網路層什麼都可能
                last_err = e
            time.sleep(self.pause * (attempt + 1))
        raise FinMindError(f"{dataset} 取得失敗: {last_err}")

    def stock_info(self) -> list[dict]:
        return self.get("TaiwanStockInfo")

    def price(self, ticker: str, start: str, end: str, adj: bool = True) -> list[dict]:
        return self.get(
            "TaiwanStockPrice",
            data_id=ticker,
            start_date=start,
            end_date=end,
            adjust=1 if adj else None,
        )

    def institutional(self, ticker: str, start: str, end: str) -> list[dict]:
        return self.get(
            "TaiwanStockInstitutionalInvestorsBuySell",
            data_id=ticker,
            start_date=start,
            end_date=end,
        )

    def per(self, ticker: str, start: str, end: str) -> list[dict]:
        return self.get("TaiwanStockPER", data_id=ticker, start_date=start, end_date=end)

    def revenue(self, ticker: str, start: str, end: str) -> list[dict]:
        return self.get("TaiwanStockMonthRevenue", data_id=ticker, start_date=start, end_date=end)
