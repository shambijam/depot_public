# strategy/__init__.py
"""
Ce package expose les classes de stratégies actives.
CryptoStrategy a été retirée du projet.
LiquidityStrategy a été retirée du projet (31 DEC 2025).
"""

from .base_strategy import BaseStrategy
from .scalping import ScalpingStrategy
# --- export pipeline pour import propre depuis 'strategy' ---
try:
    from .pipeline import ScalpingPipeline  # noqa: F401
except Exception:
    ScalpingPipeline = None  # fallback safe en cas d'env partiel



__all__ = [
    "BaseStrategy",
    "ScalpingStrategy",
    ]
