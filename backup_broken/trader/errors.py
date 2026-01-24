#trader/errors.py - Gestion des erreurs pour le Bot SNIPER_X
from __future__ import annotations

class TradeExecutionError(Exception):
    """Erreur d'exécution trade ; levée pour invalider un sizing/SLTP/etc."""
    pass

class InvalidDecisionPackageError(TradeExecutionError):
    """Le decision_package est manquant/invalide (clés requises absentes, types incohérents, etc.)."""
    pass

__all__ = ["TradeExecutionError", "InvalidDecisionPackageError"]

