# sniper_patterns/combo_detector.py

import pandas as pd
from typing import List, Dict, Any, Optional

from .candle_detector import detect_single_candle
from .multi_candle_detector import detect_multi_candle_patterns


def detect_combos(
    df: pd.DataFrame,
    patterns: Optional[Dict[str, Any]] = None
) -> List[Optional[Dict[str, Any]]]:
    """
    Détecteur desk-trader brut :
      - Chandeliers individuels (Doji, Hammer, etc.)
      - Patterns multi-bougies (Morning Star, Soldiers, Harami, etc.)
      - Confluences structurelles (OB/FVG/BOS si colonnes dispo)
      - Confirmations multi-timeframe (pattern_m5 / pattern_m15)

    ❌ Aucun scoring → que du factuel.
    ✅ Chaque bougie peut avoir plusieurs patterns détectés.
    """

    if df is None or len(df) < 5:
        return []

    signals: List[Optional[Dict[str, Any]]] = []

    # Détection brute multi-bougies (déjà renvoie une liste avec potentiels multiples)
    multi_signals = detect_multi_candle_patterns(df)

    for i in range(len(df)):
        try:
            sigs: List[Dict[str, Any]] = []

            # 1) Pattern individuel
            simple = detect_single_candle(df, i)
            if simple:
                sigs.append({"source": "single", **simple})

            # 2) Pattern(s) multi-bougies
            if i < len(multi_signals) and multi_signals[i]:
                if isinstance(multi_signals[i], list):
                    for m in multi_signals[i]:
                        sigs.append({"source": "multi", **m})
                else:
                    sigs.append({"source": "multi", **multi_signals[i]})

            if not sigs:
                signals.append(None)
                continue

            # 3) Confluences structurelles (OB/FVG/BOS si dispo)
            for s in sigs:
                s["near_ob"] = "ob_zone" in df.columns and not pd.isna(df["ob_zone"].iloc[i])
                s["near_fvg"] = "fvg" in df.columns and not pd.isna(df["fvg"].iloc[i])
                s["near_bos"] = "bos" in df.columns and not pd.isna(df["bos"].iloc[i])

                # 4) Confirmations multi-timeframe
                confirmed_tf = []
                for tf in ["pattern_m5", "pattern_m15"]:
                    if tf in df.columns and df[tf].iloc[i] == s.get("pattern"):
                        confirmed_tf.append(tf.upper())
                if confirmed_tf:
                    s["confirmed_tf"] = confirmed_tf

                # Ajout index + horodatage
                s["index"] = i
                s["timestamp"] = str(df.index[i]) if hasattr(df.index, "dtype") else None

            signals.append(sigs)

        except Exception as e:
            print(f"Erreur detect_combos à l'index {i}: {e}")
            signals.append(None)

    return signals
