# sniper_patterns/multi_candle_detector.py

"""
Détection avancée des patterns multi-bougies (mode desk brut).
Catalogue : Morning Star, Evening Star, Three Soldiers/Crows, Harami, Tweezer.
Chaque index peut retourner plusieurs patterns simultanément.
"""

import pandas as pd
from typing import Dict, Any, Optional, List


# === PATTERN DETECTORS ===
def is_morning_star(df: pd.DataFrame, i: int) -> Optional[Dict[str, Any]]:
    o1, c1 = df["open"].iloc[i - 2], df["close"].iloc[i - 2]
    o2, c2 = df["open"].iloc[i - 1], df["close"].iloc[i - 1]
    o3, c3 = df["open"].iloc[i], df["close"].iloc[i]

    if (
        c1 < o1
        and abs(c2 - o2) < 0.3 * (df["high"].iloc[i - 1] - df["low"].iloc[i - 1])
        and c3 > o3
        and c3 > (o1 + c1) / 2
    ):
        return {"pattern": "morning_star", "signal_type": "reversal"}
    return None


def is_evening_star(df: pd.DataFrame, i: int) -> Optional[Dict[str, Any]]:
    o1, c1 = df["open"].iloc[i - 2], df["close"].iloc[i - 2]
    o2, c2 = df["open"].iloc[i - 1], df["close"].iloc[i - 1]
    o3, c3 = df["open"].iloc[i], df["close"].iloc[i]

    if (
        c1 > o1
        and abs(c2 - o2) < 0.3 * (df["high"].iloc[i - 1] - df["low"].iloc[i - 1])
        and c3 < o3
        and c3 < (o1 + c1) / 2
    ):
        return {"pattern": "evening_star", "signal_type": "reversal"}
    return None


def is_three_white_soldiers(df: pd.DataFrame, i: int) -> Optional[Dict[str, Any]]:
    if all(df["close"].iloc[j] > df["open"].iloc[j] for j in [i - 2, i - 1, i]):
        return {"pattern": "three_white_soldiers", "signal_type": "continuation"}
    return None


def is_three_black_crows(df: pd.DataFrame, i: int) -> Optional[Dict[str, Any]]:
    if all(df["close"].iloc[j] < df["open"].iloc[j] for j in [i - 2, i - 1, i]):
        return {"pattern": "three_black_crows", "signal_type": "continuation"}
    return None


def is_harami(df: pd.DataFrame, i: int) -> Optional[Dict[str, Any]]:
    o1, c1 = df["open"].iloc[i - 2], df["close"].iloc[i - 2]
    o2, c2 = df["open"].iloc[i - 1], df["close"].iloc[i - 1]

    if c1 > o1 and c2 < o2 and o2 < c1 and c2 > o1:
        return {"pattern": "bearish_harami", "signal_type": "reversal"}
    if c1 < o1 and c2 > o2 and o2 > c1 and c2 < o1:
        return {"pattern": "bullish_harami", "signal_type": "reversal"}
    return None


def is_tweezer(df: pd.DataFrame, i: int) -> Optional[Dict[str, Any]]:
    if abs(df["high"].iloc[i] - df["high"].iloc[i - 1]) < 0.1 * (
        df["high"].iloc[i] - df["low"].iloc[i]
    ):
        return {"pattern": "tweezer_top", "signal_type": "reversal"}
    if abs(df["low"].iloc[i] - df["low"].iloc[i - 1]) < 0.1 * (
        df["high"].iloc[i] - df["low"].iloc[i]
    ):
        return {"pattern": "tweezer_bottom", "signal_type": "reversal"}
    return None


# === MAIN ENTRYPOINT (multi-patterns par index) ===
def detect_multi_candle(
    df: pd.DataFrame,
    i: int,
    patterns: Optional[Dict[str, Any]] = None
) -> List[Dict[str, Any]]:
    """
    Détecteur de patterns multi-bougies pour l'index i.
    - patterns : dictionnaire optionnel chargé depuis sniper_patterns.json
                 (ex: {"multi_candle": {"enabled": True, "allowed": ["three_inside","morning_star"]}})
    Retourne une liste (potentiellement vide) de dicts {pattern, type, is_bullish, ...}
    """
    results: List[Dict[str, Any]] = []
    if i < 2:
        return []

    patterns = []
    detectors = [
        is_morning_star,
        is_evening_star,
        is_three_white_soldiers,
        is_three_black_crows,
        is_harami,
        is_tweezer,
    ]

    for detector in detectors:
        try:
            res = detector(df, i)
            if res:
                enriched = res.copy()
                enriched["index"] = i
                enriched["timestamp"] = str(df.index[i])

                # Contexte optionnel
                if "phase" in df.columns:
                    enriched["phase"] = str(df["phase"].iloc[i])
                if "volume_zscore" in df.columns:
                    enriched["volume_zscore"] = float(df["volume_zscore"].iloc[i])

                patterns.append(enriched)
        except Exception as e:
            print(f"Erreur {detector.__name__} à l’index {i}: {e}")

    return patterns


def detect_multi_candle_patterns(
    df: pd.DataFrame,
) -> List[Optional[List[Dict[str, Any]]]]:
    """
    Détection complète multi-bougies pour tout le DataFrame.
    Retourne une liste (alignée avec l’index du df) de listes de patterns détectés.
    Exemple : [None, None, [pattern1, pattern2], None, ...]
    """
    if df is None or len(df) < 3:
        return []

    results: List[Optional[List[Dict[str, Any]]]] = []
    for i in range(len(df)):
        try:
            patterns = detect_multi_candle(df, i)
            results.append(patterns if patterns else None)
        except Exception as e:
            print(f"Erreur detect_multi_candle_patterns à l’index {i}: {e}")
            results.append(None)

    return results
