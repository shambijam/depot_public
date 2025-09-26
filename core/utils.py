# core/utils.py

import json
from datetime import datetime
import pandas as pd
from typing import Dict, Any, List, Optional
from enum import Enum  # Importation nécessaire pour les Énumérations


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


# ================== 🔧 UTILITAIRES GLOBAUX ==================

def get_diff(
    old_dict: Dict[str, Any], new_dict: Dict[str, Any], path: str = ""
) -> Dict[str, Any]:
    """
    Calcule récursivement les différences entre deux dictionnaires.
    """
    diff = {}
    for key in new_dict:
        new_path = f"{path}.{key}" if path else key
        if key not in old_dict:
            diff[new_path] = {
                "old_value": None,
                "new_value": new_dict[key],
                "action": "added",
            }
        elif isinstance(new_dict[key], dict) and isinstance(old_dict.get(key), dict):
            nested_diff = get_diff(old_dict[key], new_dict[key], new_path)
            if nested_diff:
                diff.update(nested_diff)
        elif new_dict[key] != old_dict.get(key):
            diff[new_path] = {
                "old_value": old_dict.get(key),
                "new_value": new_dict[key],
                "action": "modified",
            }

    for key in old_dict:
        if key not in new_dict:
            new_path = f"{path}.{key}" if path else key
            diff[new_path] = {
                "old_value": old_dict[key],
                "new_value": None,
                "action": "removed",
            }
    return diff


def normalize_levels(
    entry_price: float,
    action: str,
    pip_size: float,
    sl_pips: Optional[float] = None,
    tp_pips: Optional[float] = None,
    sl_price: float | None = None,
    tp_price: float | None = None,
    rule_name: str | None = None,
) -> dict:
    """
    Normalise SL/TP pour garantir des niveaux cohérents en prix absolus.
    - Si sl_price/tp_price sont donnés → priorité.
    - Sinon, conversion à partir des pips.
    - Burst Scalping → pas de TP.
    """
    if not isinstance(entry_price, (int, float)) or entry_price <= 0:
        return {"sl": None, "tp": None}

    action = str(action).upper()
    sl, tp = None, None

    # --- SL
    if isinstance(sl_price, (int, float)) and sl_price > 0:
        sl = float(sl_price)
    elif isinstance(sl_pips, (int, float)) and sl_pips > 0 and pip_size > 0:
        sl = entry_price - sl_pips * pip_size if action == "BUY" else entry_price + sl_pips * pip_size

    # --- TP (sauf Burst)
    if str(rule_name).lower() != "burst_scalping":
        if isinstance(tp_price, (int, float)) and tp_price > 0:
            tp = float(tp_price)
        elif isinstance(tp_pips, (int, float)) and tp_pips > 0 and pip_size > 0:
            tp = entry_price + tp_pips * pip_size if action == "BUY" else entry_price - tp_pips * pip_size

    return {"sl": sl, "tp": tp}

def enforce_no_tp_for_burst(request: dict) -> dict:
    """
    DEV DESK RULE:
    Supprime physiquement tout TP si la stratégie = burst_scalping.
    - Efface la clé 'tp' dans la requête MT5.
    - Neutralise tout champ associé au TP.
    """
    try:
        rn = str(request.get("rule_name", "")).lower()
        if rn == "burst_scalping":
            if "tp" in request:
                del request["tp"]          # ✅ enlève le champ
            request["_meta_tp_removed"] = True  # tag pour logs/debug
    except Exception:
        pass
    return request



def normalize_signals(
    signals: List[Dict[str, Any]] | Dict[str, Any] | None,
) -> List[Optional[Dict[str, Any]]]:
    """
    Normalise l'entrée 'signals' pour garantir une liste cohérente :
      - None -> []
      - Dict -> [Dict]
      - List de Dict -> inchangé
      - List imbriquée -> aplatie
    """
    if signals is None:
        return []
    if isinstance(signals, dict):
        return [signals]
    if isinstance(signals, list):
        flat = []
        for s in signals:
            if isinstance(s, list):  # cas liste imbriquée
                flat.extend(s)
            else:
                flat.append(s)
        return flat
    raise TypeError(f"[normalize_signals] Format inattendu: {type(signals)}")
