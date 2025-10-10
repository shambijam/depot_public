# strategy/__init__.py
"""
Ce package expose les classes de stratégies actives.
CryptoStrategy a été retirée du projet.
"""

from .base_strategy import BaseStrategy
from .scalping import ScalpingStrategy
from .liquidity import LiquidityStrategy
# --- export pipeline pour import propre depuis 'strategy' ---
try:
    from .pipeline import ScalpingPipeline  # noqa: F401
except Exception:
    ScalpingPipeline = None  # fallback safe en cas d'env partiel



__all__ = [
    "BaseStrategy",
    "ScalpingStrategy",
    "LiquidityStrategy",
    ]
