"""twquant — 台股量化回測工具包（零外部依賴）。"""
from __future__ import annotations

__version__ = "0.2.1"

from .engine import CostConfig, PortfolioBacktester, Trade
from .loader import DEFAULT_UNIVERSE, Bar, load_bars, load_universe
from .store import Store
from .strategies import Breakout20, TrendMA, build

__all__ = [
    "Bar",
    "Breakout20",
    "CostConfig",
    "DEFAULT_UNIVERSE",
    "PortfolioBacktester",
    "Store",
    "Trade",
    "TrendMA",
    "build",
    "load_bars",
    "load_universe",
]
