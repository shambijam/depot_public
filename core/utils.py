# core/utils.py

import json
from datetime import datetime
import pandas as pd
from typing import Dict, Any, List # Importations nécessaires

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