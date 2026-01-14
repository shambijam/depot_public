# -*- coding: utf-8 -*-
"""
RegimeDetectorLite - Détection régime marché avec 20-30 barres.

Alternative ultra-rapide au système complet (200 barres).
Utilisé par SCALPING Thread pour adaptation VWAP temps réel.

Méthodes :
- Slope linéaire (remplace ADX 200 barres)
- ATR 14 (remplace Garman-Klass)
- Volume ratio (volume courant / MA 14)
- Momentum 20 barres

Régimes détectés :
- TRENDING : Slope fort + momentum fort
- BALANCED : Slope faible + volatilité moyenne
- ACCUMULATION : Volume élevé + range serré
- TRANSITIONAL : Aucun critère dominant

Date : 2025-12-11
Version : Phase 2 - Optimisation Cache Multi-Niveaux
"""

from typing import Dict, Any, Optional
import pandas as pd
import numpy as np


class RegimeDetectorLite:
    """
    Détection régime marché simplifié (20-30 barres).

    Compatible SCALPING haute fréquence (cycle 5s).
    """

    def __init__(self, logger=None):
        """
        Initialise le détecteur.

        Args:
            logger: Logger optionnel
        """
        self.logger = logger

    def detect_regime(
        self, df: pd.DataFrame, min_bars: int = 20
    ) -> Dict[str, Any]:
        """
        Détecte le régime marché depuis DataFrame.

        Args:
            df: DataFrame barres OHLCV (minimum 20 barres)
            min_bars: Nombre minimum barres requis (défaut 20)

        Returns:
            {
                "regime": str,          # TRENDING / BALANCED / ACCUMULATION / TRANSITIONAL
                "confidence": float,    # 0.0-1.0
                "metrics": {
                    "slope": float,
                    "atr_pct": float,
                    "volume_ratio": float,
                    "momentum_pct": float
                }
            }
        """
        # Validation
        if df is None or df.empty or len(df) < min_bars:
            return {
                "regime": "BALANCED",  # Défaut neutre
                "confidence": 0.0,
                "metrics": {},
            }

        try:
            # Calculer métriques
            metrics = self._calculate_metrics(df)

            # Classification
            regime, confidence = self._classify_regime(metrics)

            return {
                "regime": regime,
                "confidence": confidence,
                "metrics": metrics,
            }

        except Exception as e:
            if self.logger:
                self.logger.warning(
                    f"[RegimeDetectorLite] Erreur détection: {e}"
                )
            return {
                "regime": "BALANCED",
                "confidence": 0.0,
                "metrics": {},
            }

    def _calculate_metrics(self, df: pd.DataFrame) -> Dict[str, float]:
        """
        Calcule métriques régime.

        Args:
            df: DataFrame barres

        Returns:
            {
                "slope": float,           # Pente linéaire (-1.0 à 1.0)
                "atr_pct": float,         # ATR en % du prix (0.0-10.0)
                "volume_ratio": float,    # Volume courant / MA 14 (0.5-3.0)
                "momentum_pct": float     # Momentum 20 barres en % (-10.0 à 10.0)
            }
        """
        # --- 1. SLOPE LINÉAIRE (3 barres) ---
        # Régression linéaire sur close - RÉDUIT à 3 pour réactivité (14 JAN 2026)
        window = min(3, len(df))
        close_series = df["close"].iloc[-window:].values
        x = np.arange(len(close_series))

        # Coefficients régression linéaire
        if len(x) > 1:
            slope_raw = np.polyfit(x, close_series, 1)[0]
            # Normaliser : slope / (prix moyen)
            avg_price = close_series.mean()
            slope = slope_raw / avg_price if avg_price > 0 else 0.0
        else:
            slope = 0.0

        # --- 2. ATR 5 (Average True Range) ---
        # RÉDUIT à 5 pour réactivité (14 JAN 2026)
        atr_period = min(5, len(df))
        if len(df) >= atr_period:
            high = df["high"].values
            low = df["low"].values
            close_prev = df["close"].shift(1).values

            tr1 = high - low
            tr2 = np.abs(high - close_prev)
            tr3 = np.abs(low - close_prev)
            tr = np.maximum(tr1, np.maximum(tr2, tr3))

            # ATR = moyenne mobile TR
            atr = pd.Series(tr).rolling(window=atr_period, min_periods=1).mean().iloc[-1]
            current_price = df["close"].iloc[-1]
            atr_pct = (atr / current_price * 100) if current_price > 0 else 0.0
        else:
            atr_pct = 0.0

        # --- 3. VOLUME RATIO ---
        # RÉDUIT à 5 pour réactivité (14 JAN 2026)
        volume_period = min(5, len(df))
        if len(df) >= volume_period:
            volume_ma = df["tick_volume"].rolling(window=volume_period, min_periods=1).mean().iloc[-1]
            current_volume = df["tick_volume"].iloc[-1]
            volume_ratio = current_volume / volume_ma if volume_ma > 0 else 1.0
        else:
            volume_ratio = 1.0

        # --- 4. MOMENTUM 3 BARRES ---
        # RÉDUIT à 3 pour détecter mouvements rapides (14 JAN 2026)
        momentum_period = min(3, len(df))
        if len(df) >= momentum_period:
            price_start = df["close"].iloc[-momentum_period]
            price_end = df["close"].iloc[-1]
            momentum_pct = ((price_end - price_start) / price_start * 100) if price_start > 0 else 0.0
        else:
            momentum_pct = 0.0

        return {
            "slope": slope,
            "atr_pct": atr_pct,
            "volume_ratio": volume_ratio,
            "momentum_pct": momentum_pct,
        }

    def _classify_regime(self, metrics: Dict[str, float]) -> tuple[str, float]:
        """
        Classifie le régime depuis métriques.

        Args:
            metrics: Dictionnaire métriques

        Returns:
            (regime: str, confidence: float)
        """
        slope = metrics.get("slope", 0.0)
        atr_pct = metrics.get("atr_pct", 0.0)
        volume_ratio = metrics.get("volume_ratio", 1.0)
        momentum_pct = metrics.get("momentum_pct", 0.0)

        # Normaliser slope en score 0-1 (absolu)
        slope_abs = abs(slope)
        slope_score = min(slope_abs * 100, 1.0)  # slope ~0.01 = fort

        # Seuils
        STRONG_SLOPE = 0.003       # 0.3% par barre
        WEAK_SLOPE = 0.001         # 0.1% par barre
        HIGH_VOLUME = 1.5          # 1.5x volume normal
        LOW_VOLATILITY = 0.5       # ATR < 0.5%
        HIGH_VOLATILITY = 1.5      # ATR > 1.5%

        # --- RÈGLES DE CLASSIFICATION ---

        # 1. TRENDING : Slope fort + momentum cohérent
        if slope_abs >= STRONG_SLOPE and abs(momentum_pct) >= 1.0:
            confidence = min(slope_score + (abs(momentum_pct) / 10), 1.0)
            return "TRENDING", confidence

        # 2. ACCUMULATION : Volume élevé + faible volatilité + momentum faible
        if volume_ratio >= HIGH_VOLUME and atr_pct <= LOW_VOLATILITY and abs(momentum_pct) < 0.5:
            confidence = min((volume_ratio - 1.0) / 2.0, 1.0)
            return "ACCUMULATION", confidence

        # 3. BALANCED : Slope faible + volatilité modérée
        if slope_abs <= WEAK_SLOPE and atr_pct >= LOW_VOLATILITY and atr_pct <= HIGH_VOLATILITY:
            confidence = 0.7
            return "BALANCED", confidence

        # 4. TRANSITIONAL : Cas intermédiaires (aucun critère dominant)
        confidence = 0.5
        return "TRANSITIONAL", confidence


# Singleton global (optionnel)
regime_detector_lite = RegimeDetectorLite()
