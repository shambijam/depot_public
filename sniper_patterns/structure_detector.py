#sniper_patterns/structure_detector.py

import pandas as pd
from typing import List, Dict, Any, Optional


def detect_structure(df: pd.DataFrame, i: int) -> Dict[str, Any]:
    """
    Contexte structurel : OB, FVG, BOS, supports/résistances...
    """
    
    return {
        "near_ob": "ob_zone" in df.columns and not pd.isna(df["ob_zone"].iloc[i]),
        "near_fvg": "fvg" in df.columns and not pd.isna(df["fvg"].iloc[i]),
        "near_bos": "bos" in df.columns and not pd.isna(df["bos"].iloc[i]),
    }
    
def enrich_structure(
    df: pd.DataFrame, signals: List[Optional[Dict[str, Any]]]
    ) -> List[Optional[Dict[str, Any]]]:
    """
    Enrichit chaque signal avec la proximité structurelle :
    - OB (Order Block)
    - FVG (Fair Value Gap)
    - BOS (Break of Structure)
    """
    enriched: List[Optional[Dict[str, Any]]] = []

    for i, sig in enumerate(signals):
        if not sig:
            enriched.append(None)
            continue

        try:
            context = {
                "near_ob": "ob_zone" in df.columns and not pd.isna(df["ob_zone"].iloc[i]),
                "near_fvg": "fvg" in df.columns and not pd.isna(df["fvg"].iloc[i]),
                "near_bos": "bos" in df.columns and not pd.isna(df["bos"].iloc[i]),
            }

            new_sig = sig.copy()
            new_sig["structure"] = context
            enriched.append(new_sig)

        except Exception as e:
            print(f"[StructureEnricher] Erreur enrichissement index {i}: {e}")
            enriched.append(sig)

    return enriched
    
