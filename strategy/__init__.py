# strategy/__init__.py
"""
Ce package expose les classes de stratégies actives.
CryptoStrategy a été retirée du projet.
"""

from .base_strategy import BaseStrategy
from .scalping import ScalpingStrategy
from .liquidity import LiquidityStrategy


__all__ = [
    "BaseStrategy",
    "ScalpingStrategy",
    "LiquidityStrategy",
    ]
