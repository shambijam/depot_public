#sniper_patterns/multi_tf_confirmer.py


"""
Multi-timeframe confirmer
-------------------------
Objectif : Vérifier si un pattern détecté sur une timeframe principale
est confirmé par une ou plusieurs timeframes supérieures.
"""

import pandas as pd
from typing import List, Dict, Any, Optional
from core.utils import normalize_signals



def confirm_multi_tf(
    df: pd.DataFrame,
    signals: List[Optional[Dict[str, Any]]] | Dict[str, Any] | None,
    tfs: List[str] = ["pattern_m5", "pattern_m15", "pattern_h1"]
) -> List[Optional[Dict[str, Any]]]:
    """
    Enrichit les signaux avec des confirmations cross-timeframe.

    Exemple :
      - Si pattern = "hammer" en M1 ET "hammer" en M5 → confirmation.
      - Sinon, tagué comme isolé.

    :param df: DataFrame principal (doit contenir colonnes pattern_m5, pattern_m15, etc.)
    :param signals: Liste des signaux détectés (ou un seul dict, ou None)
    :param tfs: Liste des colonnes à comparer
    """

    # 🔹 Patch pour accepter dict / None / liste
    if signals is None:
        signals = []
    elif isinstance(signals, dict):
        signals = [signals]

    enriched: List[Optional[Dict[str, Any]]] = []

    for i, sig in enumerate(signals):
        if not sig:
            enriched.append(None)
            continue

        try:
            confirmations = []
            for tf in tfs:
                if tf in df.columns and df[tf].iloc[i] == sig.get("pattern"):
                    confirmations.append(tf.upper())

            new_sig = sig.copy()
            new_sig["confirmed_tf"] = confirmations if confirmations else []
            new_sig["is_multi_tf_confirmed"] = len(confirmations) > 0

            enriched.append(new_sig)

        except Exception as e:
            print(f"[MultiTFConfirmer] Erreur à l'index {i}: {e}")
            enriched.append(sig)

    return enriched

