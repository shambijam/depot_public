from __future__ import annotations

"""
Package `trader` — init léger sans effets de bord.
- N'importe rien de lourd (MT5, config…) au chargement du package.
- Exporte `TradeExecutor` en lazy pour éviter les imports circulaires/couts init.
"""

from typing import TYPE_CHECKING

__all__ = ["TradeExecutor"]

if TYPE_CHECKING:
    # Hints pour l'IDE sans coût runtime
    from .trade_executor import TradeExecutor


def __getattr__(name: str):
    # Export paresseux des symboles publics
    if name == "TradeExecutor":
        from .trade_executor import TradeExecutor
        return TradeExecutor
    raise AttributeError(f"module 'trader' has no attribute {name!r}")


def __dir__():
    # Pour que dir(trader) liste aussi les exports lazy
    return sorted(list(globals().keys()) + __all__)
