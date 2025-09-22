# sniper_patterns/multi_candle_detector.py

import pandas as pd
from typing import List, Dict, Any, Optional

"""
Détection de patterns multi-bougies :
- Morning Star
- Evening Star
- Three White Soldiers
- Three Black Crows
- Harami
- Tweezers
"""

# ============================================================
# ===  Détecteurs individuels ================================
# ============================================================

def is_morning_star(df: pd.DataFrame, i: int) -> Optional[Dict[str, Any]]:
    if i < 2:
        return None
    c1, c2, c3 = df.iloc[i - 2], df.iloc[i - 1], df.iloc[i]
    if (
        c1["close"] < c1["open"]  # 1ère rouge
        and abs(c2["close"] - c2["open"]) < (c1["open"] - c1["close"]) * 0.5  # petit corps
        and c3["close"] > c3["open"]  # verte
        and c3["close"] > (c1["open"] + c1["close"]) / 2
    ):
        return {"pattern": "morning_star", "type": "reversal", "is_bullish": True}
    return None


def is_evening_star(df: pd.DataFrame, i: int) -> Optional[Dict[str, Any]]:
    if i < 2:
        return None
    c1, c2, c3 = df.iloc[i - 2], df.iloc[i - 1], df.iloc[i]
    if (
        c1["close"] > c1["open"]
        and abs(c2["close"] - c2["open"]) < (c1["close"] - c1["open"]) * 0.5
        and c3["close"] < c3["open"]
        and c3["close"] < (c1["open"] + c1["close"]) / 2
    ):
        return {"pattern": "evening_star", "type": "reversal", "is_bullish": False}
    return None


def is_three_white_soldiers(df: pd.DataFrame, i: int) -> Optional[Dict[str, Any]]:
    if i < 2:
        return None
    c1, c2, c3 = df.iloc[i - 2], df.iloc[i - 1], df.iloc[i]
    if (
        c1["close"] > c1["open"]
        and c2["close"] > c2["open"]
        and c3["close"] > c3["open"]
        and c1["close"] < c2["close"] < c3["close"]
    ):
        return {"pattern": "three_white_soldiers", "type": "continuation", "is_bullish": True}
    return None


def is_three_black_crows(df: pd.DataFrame, i: int) -> Optional[Dict[str, Any]]:
    if i < 2:
        return None
    c1, c2, c3 = df.iloc[i - 2], df.iloc[i - 1], df.iloc[i]
    if (
        c1["close"] < c1["open"]
        and c2["close"] < c2["open"]
        and c3["close"] < c3["open"]
        and c1["close"] > c2["close"] > c3["close"]
    ):
        return {"pattern": "three_black_crows", "type": "continuation", "is_bullish": False}
    return None


def is_harami(df: pd.DataFrame, i: int) -> Optional[Dict[str, Any]]:
    if i < 1:
        return None
    c1, c2 = df.iloc[i - 1], df.iloc[i]
    if c1["close"] > c1["open"] and c2["close"] < c2["open"]:  # bull -> bear
        if c2["open"] < c1["close"] and c2["close"] > c1["open"]:
            return {"pattern": "bearish_harami", "type": "reversal", "is_bullish": False}
    elif c1["close"] < c1["open"] and c2["close"] > c2["open"]:  # bear -> bull
        if c2["open"] > c1["close"] and c2["close"] < c1["open"]:
            return {"pattern": "bullish_harami", "type": "reversal", "is_bullish": True}
    return None


def is_tweezer(df: pd.DataFrame, i: int) -> Optional[Dict[str, Any]]:
    if i < 1:
        return None
    c1, c2 = df.iloc[i - 1], df.iloc[i]
    if abs(c1["high"] - c2["high"]) < 1e-5:  # sommets quasi identiques
        return {"pattern": "tweezer_top", "type": "reversal", "is_bullish": False}
    if abs(c1["low"] - c2["low"]) < 1e-5:  # creux quasi identiques
        return {"pattern": "tweezer_bottom", "type": "reversal", "is_bullish": True}
    return None


# ============================================================
# ===  Orchestrateurs ========================================
# ============================================================

def detect_multi_candle(
    df: pd.DataFrame,
    i: Optional[int] = None,
    patterns: Optional[Dict[str, Any]] = None
) -> List[Dict[str, Any]]:
    """
    Détection d’un ensemble de patterns multi-bougies.
    - Si `i` est fourni → détection ponctuelle à l'index i.
    - Si `i` est None → parcourt tout le DataFrame.
    """
    results: List[Dict[str, Any]] = []

    # Cas 1: on parcourt tout le DataFrame
    if i is None:
        for idx in range(len(df)):
            results.extend(detect_multi_candle(df, idx, patterns))
        return results

    # Cas 2: détection ponctuelle
    if i < 2:
        return results

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

                if "phase" in df.columns:
                    enriched["phase"] = str(df["phase"].iloc[i])
                if "volume_zscore" in df.columns:
                    enriched["volume_zscore"] = float(df["volume_zscore"].iloc[i])

                results.append(enriched)
        except Exception as e:
            print(f"Erreur {detector.__name__} à l’index {i}: {e}")

    return results


def detect_multi_candle_patterns(
    df: pd.DataFrame,
    patterns: Optional[Dict[str, Any]] = None
) -> List[Dict[str, Any]]:
    """
    Détection des patterns multi-bougies sur tout le DataFrame.
    Utilise detect_multi_candle(df) en mode global (i=None).
    Retourne une liste à plat de tous les patterns détectés.
    """
    return detect_multi_candle(df, i=None, patterns=patterns)

