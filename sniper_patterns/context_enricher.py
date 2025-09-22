# sniper_patterns/context_enricher.py

"""
Enrichissement contextuel des patterns détectés.
------------------------------------------------
Objectif : Ajouter une lecture "desk-trader" aux signaux bruts :
- Volatilité relative
- Position dans le range
- Volume
- Confluences structurelles (OB/FVG/BOS)
- Phase de marché

⚡ Zéro scoring → factuel uniquement.
"""

import pandas as pd
from typing import List, Dict, Any, Optional


def classify_volatility(candle_size: float, atr: float) -> str:
    """Retourne une classification qualitative de la volatilité."""
    if atr <= 0:
        return "inconnu"
    ratio = candle_size / atr
    if ratio < 0.8:
        return "faible"
    elif ratio < 1.5:
        return "normale"
    else:
        return "élevée"


def position_in_range(close: float, high: float, low: float) -> str:
    """Situe le close dans la bougie du jour."""
    if high == low:
        return "inconnu"
    rel = (close - low) / (high - low)
    if rel < 0.33:
        return "bas"
    elif rel < 0.66:
        return "milieu"
    else:
        return "haut"


def enrich_context(df: pd.DataFrame, signals: List[Optional[Dict[str, Any]]]) -> List[Optional[Dict[str, Any]]]:

    """
    Enrichit chaque signal brut avec :
      - volatilité
      - position dans la bougie
      - volume
      - OB/FVG/BOS
      - phase si dispo
    """
    enriched: List[Optional[Dict[str, Any]]] = []

    for i, sig in enumerate(signals):
        if not sig:
            enriched.append(None)
            continue

        try:
            candle_size = df["high"].iloc[i] - df["low"].iloc[i]
            atr = df["atr"].iloc[i] if "atr" in df.columns else candle_size

            context: Dict[str, Any] = {}

            # 1. Volatilité
            context["volatility"] = classify_volatility(candle_size, atr)

            # 2. Position relative
            context["range_position"] = position_in_range(
                df["close"].iloc[i], df["high"].iloc[i], df["low"].iloc[i]
            )

            # 3. Volume
            if "volume_zscore" in df.columns:
                vz = df["volume_zscore"].iloc[i]
                context["volume_zscore"] = float(vz)
                context["volume_anomaly"] = abs(vz) > 2
            else:
                context["volume_zscore"] = None
                context["volume_anomaly"] = False

            # 4. Structure (OB/FVG/BOS)
            for key in ["ob_zone", "fvg", "bos"]:
                context[f"near_{key}"] = (
                    key in df.columns and not pd.isna(df[key].iloc[i])
                )

            context["high_confluence"] = (
                sum(1 for k in ["ob_zone", "fvg", "bos"] if context[f"near_{k}"]) >= 2
            )

            # 5. Phase de marché
            if "phase" in df.columns:
                context["phase"] = str(df["phase"].iloc[i])

            enriched_sig = sig.copy()
            enriched_sig["context"] = context

            enriched.append(enriched_sig)

        except Exception as e:
            print(f"[ContextEnricher] Erreur enrichissement index {i}: {e}")
            enriched.append(sig)

    return enriched
