# sniper_patterns/candle_detector.py

"""
Détection factuelle de chandeliers individuels.
Catalogue : Doji (et variantes), Hammer, Hanging Man, Inverted Hammer, Shooting Star,
Marubozu, Spinning Top, Engulfing, Belt Hold, Kicker.
Pas de scoring → sortie brute, descriptive et exploitable.
"""

import pandas as pd
from typing import Dict, Any, Optional


def detect_single_candle(df: pd.DataFrame, i: int) -> Optional[Dict[str, Any]]:
    try:
        o, h, l, c = df["open"].iloc[i], df["high"].iloc[i], df["low"].iloc[i], df["close"].iloc[i]
        body = abs(c - o)
        size = h - l
        upper_wick = h - max(o, c)
        lower_wick = min(o, c) - l
        body_ratio = body / size if size > 0 else 0
        is_bull = c > o

        pattern, pattern_type = None, None

        # === DOJI & VARIANTS ===
        if body_ratio < 0.1:
            if abs(upper_wick - lower_wick) < 0.1 * size:
                pattern, pattern_type = "doji", "indecision"
            elif upper_wick > 2 * body and lower_wick < 0.1 * size:
                pattern, pattern_type = "gravestone_doji", "reversal"
            elif lower_wick > 2 * body and upper_wick < 0.1 * size:
                pattern, pattern_type = "dragonfly_doji", "reversal"
            else:
                pattern, pattern_type = "doji", "indecision"

        # === SPINNING TOP ===
        elif body_ratio < 0.3 and upper_wick > 0.3 * size and lower_wick > 0.3 * size:
            pattern, pattern_type = "spinning_top", "indecision"

        # === HAMMER FAMILY ===
        elif lower_wick > 2 * body and upper_wick < body:
            pattern, pattern_type = ("hammer" if is_bull else "hanging_man", "reversal")
        elif upper_wick > 2 * body and lower_wick < body:
            pattern, pattern_type = ("inverted_hammer" if is_bull else "shooting_star", "reversal")

        # === MARUBOZU ===
        elif body_ratio > 0.95 and upper_wick < 0.05 * size and lower_wick < 0.05 * size:
            pattern, pattern_type = ("marubozu_bull" if is_bull else "marubozu_bear", "momentum")

        # === ENGULFING SIMPLE ===
        if i > 0 and body > abs(df["close"].iloc[i - 1] - df["open"].iloc[i - 1]):
            prev_o, prev_c = df["open"].iloc[i - 1], df["close"].iloc[i - 1]
            if is_bull and c > prev_o and o < prev_c:
                pattern, pattern_type = "bullish_engulfing", "reversal"
            elif not is_bull and c < prev_o and o > prev_c:
                pattern, pattern_type = "bearish_engulfing", "reversal"

        # === BELT HOLD ===
        if body_ratio > 0.7 and (upper_wick < 0.05 * size or lower_wick < 0.05 * size):
            pattern, pattern_type = ("belt_hold_bull" if is_bull else "belt_hold_bear", "continuation")

        # === KICKER (gap fort) ===
        if i > 0:
            prev_c = df["close"].iloc[i - 1]
            if is_bull and o > prev_c and c > o:
                pattern, pattern_type = "bullish_kicker", "reversal"
            elif not is_bull and o < prev_c and c < o:
                pattern, pattern_type = "bearish_kicker", "reversal"

        # === RETOUR FACTUEL ===
        if pattern:
            enriched: Dict[str, Any] = {
                "pattern": pattern,
                "type": pattern_type,
                "is_bullish": is_bull,
                "body_ratio": round(body_ratio, 3),
                "upper_wick": round(upper_wick, 5),
                "lower_wick": round(lower_wick, 5),
                "candle_size": round(size, 5),
            }

            # Ajout de contexte si dispo
            if "volume_zscore" in df.columns:
                enriched["volume_zscore"] = float(df["volume_zscore"].iloc[i])
            if "phase" in df.columns:
                enriched["phase"] = str(df["phase"].iloc[i])

            return enriched

        return None

    except Exception:
        return None
