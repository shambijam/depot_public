# core/utils.py

import json
from datetime import datetime
import pandas as pd
from typing import Dict, Any, List
from enum import Enum # Importation nécessaire pour les Énumérations

# Définition de l'exception ConfigValidationError
class ConfigValidationError(ValueError):
    """Exception levée lorsqu'une validation de configuration échoue."""
    pass

# Définition de l'énumération TradeStatus
class TradeStatus(Enum):
    """Énumération pour standardiser les statuts de résultat de trade."""
    PROFIT = "PROFIT"
    LOSS = "LOSS"
    BREAKEVEN = "BREAKEVEN"

class CustomJSONEncoder(json.JSONEncoder):
    """
    Encodeur JSON personnalisé pour gérer la sérialisation des types de données
    non-standards que l'on retrouve dans le projet SNIPER_X.
    """
    def default(self, o: Any) -> Any:
        if isinstance(o, (datetime, pd.Timestamp)):
            return o.isoformat()
        if isinstance(o, set):
            return list(o)
        try:
            return super().default(o)
        except TypeError:
            return str(o)

def get_diff(old_dict: Dict[str, Any], new_dict: Dict[str, Any], path: str = "") -> Dict[str, Any]:
    """
    Calcule récursivement les différences entre deux dictionnaires.

    Args:
        old_dict (Dict): L'ancien dictionnaire pour comparaison.
        new_dict (Dict): Le nouveau dictionnaire pour comparaison.
        path (str): Le chemin actuel dans le dictionnaire pour la récursion.

    Returns:
        Dict: Un dictionnaire des différences, où chaque clé indique le chemin de la modification
              et la valeur est un dictionnaire décrivant l'ancienne valeur, la nouvelle valeur et l'action.
    """
    diff = {}
    for key in new_dict:
        new_path = f"{path}.{key}" if path else key
        if key not in old_dict:
            diff[new_path] = {"old_value": None, "new_value": new_dict[key], "action": "added"}
        elif isinstance(new_dict[key], dict) and isinstance(old_dict.get(key), dict):
            nested_diff = get_diff(old_dict[key], new_dict[key], new_path)
            if nested_diff:
                diff.update(nested_diff)
        elif new_dict[key] != old_dict.get(key):
            diff[new_path] = {"old_value": old_dict.get(key), "new_value": new_dict[key], "action": "modified"}

    for key in old_dict:
        if key not in new_dict:
            new_path = f"{path}.{key}" if path else key
            diff[new_path] = {"old_value": old_dict[key], "new_value": None, "action": "removed"}
    return diff

def normalize_levels(
    entry_price: float,
    action: str,
    pip_size: float,
    sl_pips: float | None = None,
    tp_pips: float | None = None,
    sl_price: float | None = None,
    tp_price: float | None = None,
) -> dict:
    """
    Normalise SL/TP pour garantir des niveaux cohérents en prix absolus.
    - Si sl_price/tp_price sont donnés → priorité.
    - Sinon, conversion à partir des pips.
    - Retourne un dict {"sl": float|None, "tp": float|None}

    Exemple :
        normalize_levels(1930.50, "BUY", 0.10, sl_pips=20, tp_pips=40)
        => {"sl": 1928.50, "tp": 1934.50}
    """
    if not isinstance(entry_price, (int, float)) or entry_price <= 0:
        return {"sl": None, "tp": None}

    action = str(action).upper()
    sl, tp = None, None

    # --- SL
    if isinstance(sl_price, (int, float)) and sl_price > 0:
        sl = float(sl_price)
    elif isinstance(sl_pips, (int, float)) and sl_pips > 0 and pip_size > 0:
        if action == "BUY":
            sl = entry_price - sl_pips * pip_size
        elif action == "SELL":
            sl = entry_price + sl_pips * pip_size

    # --- TP
    if isinstance(tp_price, (int, float)) and tp_price > 0:
        tp = float(tp_price)
    elif isinstance(tp_pips, (int, float)) and tp_pips > 0 and pip_size > 0:
        if action == "BUY":
            tp = entry_price + tp_pips * pip_size
        elif action == "SELL":
            tp = entry_price - tp_pips * pip_size

    return {"sl": sl, "tp": tp}
