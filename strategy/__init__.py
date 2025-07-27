# strategy/__init__.py

# Ce fichier marque le répertoire 'strategy' comme un paquet Python.
# Il est utilisé pour exposer les classes de stratégie principales pour une importation facile.

# Exposer les classes de stratégie actives pour une importation facile
from .base_strategy import BaseStrategy
from .scalping import ScalpingStrategy
from .crypto import CryptoStrategy
from .liquidity import LiquidityStrategy # <-- Ajout de l'importation manquante
from .dynamic import DynamicStrategy     # <-- Ajout de l'importation manquante

__all__ = [
    "BaseStrategy",
    "ScalpingStrategy",
    "CryptoStrategy",
    "LiquidityStrategy", # <-- Ajout à la liste __all__
    "DynamicStrategy"    # <-- Ajout à la liste __all__
]