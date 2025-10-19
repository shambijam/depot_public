#trader/errors.py - Gestion des erreurs pour le Bot SNIPER_X
from __future__ import annotations

class TradeExecutionError(Exception):
    """Erreur d'exécution trade ; levée pour invalider un sizing/SLTP/etc."""
    pass