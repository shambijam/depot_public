# phase_observer/vwap/derivatives.py
"""
Calculateur de dérivés VWAP institutionnel
Slope, curvature, bands, velocity, régime detection
"""

import logging
import numpy as np
import pandas as pd
from typing import Dict, Any, Optional, Tuple

from .models import VWAPDerivatives, VWAPZone, VWAPRegime
from .config import VWAPConfig, get_regime_windows


logger = logging.getLogger(__name__)


class VWAPDerivativesCalculator:
    """
    Calculateur d'indicateurs dérivés du VWAP
    Niveau institutionnel avec détection de régime
    """

    def __init__(self, config: VWAPConfig):
        """
        Initialise le calculateur de dérivés

        Args:
            config: Configuration VWAP
        """
        self.config = config
        self.symbol = config.symbol
        self.logger = logging.getLogger(f"{__name__}.{self.symbol}")

        # Fenêtres de calcul
        self.windows = config.derivatives.window_sizes
        self.std_devs = config.derivatives.bands_std_dev

    def calculate_all(
        self,
        vwap_array: np.ndarray,
        price_array: np.ndarray,
        timestamp: pd.Timestamp = None,
        market_regime: Optional[str] = None
    ) -> VWAPDerivatives:
        """
        Calcule tous les dérivés VWAP avec fenêtres adaptatives

        Args:
            vwap_array: Array VWAP
            price_array: Array prix
            timestamp: Timestamp actuel
            market_regime: Régime VWAP ("TRENDING", "ACCUMULATION", "BALANCED", "TRANSITIONAL")
                          ✅ MAJ (06 DEC 2025): Fenêtres adaptatives selon régime

        Returns:
            VWAPDerivatives complet
        """
        if timestamp is None:
            timestamp = pd.Timestamp.utcnow()

        try:
            # ✅ MAJ (06 DEC 2025): Récupérer fenêtres adaptatives selon régime
            # Si pas de régime fourni, utiliser BALANCED par défaut
            regime = market_regime or "BALANCED"
            windows = get_regime_windows(regime)

            slope_short_window = windows.get("slope_short", 30)
            slope_medium_window = windows.get("slope_medium", 50)
            slope_long_window = windows.get("slope_long", 100)
            curvature_window = windows.get("curvature", 50)
            velocity_window = windows.get("velocity", 10)

            self.logger.debug(
                f"[VWAP_DERIVATIVES] Regime={regime} | "
                f"Windows: short={slope_short_window} med={slope_medium_window} long={slope_long_window}"
            )

            # 1. Slopes (fenêtres adaptatives)
            slope_20 = self._calculate_slope(vwap_array, window=slope_short_window)
            slope_50 = self._calculate_slope(vwap_array, window=slope_medium_window)
            slope_100 = self._calculate_slope(vwap_array, window=slope_long_window)

            # 2. Curvature (dérivée 2nde) - fenêtre adaptive
            curvature = self._calculate_curvature(vwap_array, window=curvature_window)

            # 3. Velocity & Acceleration - fenêtre adaptive
            velocity = self._calculate_velocity(vwap_array, window=velocity_window)
            acceleration = self._calculate_acceleration(vwap_array, window=velocity_window)

            # 4. Distance prix/VWAP
            distance_pips, distance_percent = self._calculate_distance(
                vwap_array[-1],
                price_array[-1]
            )

            # 5. Zone classification
            zone = self._classify_zone(distance_pips)

            # 6. Régime detection
            regime = self._detect_regime(vwap_array, price_array)

            # 7. Bands (Bollinger-style)
            bands = self._calculate_bands(vwap_array)

            # 8. Confidence & Quality
            confidence = self._calculate_confidence(
                len(vwap_array),
                slope_20,
                distance_pips
            )
            quality_score = self._calculate_quality_score(vwap_array)

            return VWAPDerivatives(
                timestamp=timestamp,
                symbol=self.symbol,
                slope_20=slope_20,
                slope_50=slope_50,
                slope_100=slope_100,
                curvature=curvature,
                velocity=velocity,
                acceleration=acceleration,
                distance_pips=distance_pips,
                distance_percent=distance_percent,
                zone=zone,
                regime=regime,
                bands=bands,
                confidence=confidence,
                quality_score=quality_score,
            )

        except Exception as e:
            self.logger.error(f"[VWAP_DERIVATIVES] Erreur calcul: {e}", exc_info=True)
            # Retourne dérivés par défaut
            return VWAPDerivatives(
                timestamp=timestamp,
                symbol=self.symbol
            )

    def _calculate_slope(self, series: np.ndarray, window: int = 20) -> float:
        """
        Calcule la pente avec régression linéaire (OLS)

        Args:
            series: Array VWAP
            window: Fenêtre de calcul

        Returns:
            Slope (pente)
        """
        if len(series) < window:
            return 0.0

        try:
            # Prend les N derniers points
            y = series[-window:]
            x = np.arange(window)

            # Régression linéaire (formule fermée)
            n = len(x)
            sum_x = np.sum(x)
            sum_y = np.sum(y)
            sum_xy = np.sum(x * y)
            sum_xx = np.sum(x * x)

            denominator = n * sum_xx - sum_x * sum_x
            if abs(denominator) < 1e-12:
                return 0.0

            slope = (n * sum_xy - sum_x * sum_y) / denominator
            return float(slope)

        except Exception:
            return 0.0

    def _calculate_curvature(self, series: np.ndarray, window: int = 50) -> float:
        """
        Calcule la courbure (dérivée 2nde)

        Returns:
            Curvature
        """
        if len(series) < window:
            return 0.0

        try:
            # Calcule slopes sur 2 demi-fenêtres
            mid = window // 2
            y = series[-window:]

            slope1 = self._calculate_slope(y[:mid+5], window=mid)
            slope2 = self._calculate_slope(y[mid:], window=mid)

            # Différence de pentes = courbure
            curvature = slope2 - slope1
            return float(curvature)

        except Exception:
            return 0.0

    def _calculate_velocity(self, series: np.ndarray, window: int = 10) -> float:
        """
        Calcule la vitesse de variation (dérivée 1ère simplifiée)

        Returns:
            Velocity
        """
        if len(series) < window:
            return 0.0

        try:
            y = series[-window:]
            # Vitesse = (dernier - premier) / window
            velocity = (y[-1] - y[0]) / window
            return float(velocity)

        except Exception:
            return 0.0

    def _calculate_acceleration(self, series: np.ndarray, window: int = 10) -> float:
        """
        Calcule l'accélération (dérivée 2nde simplifiée)

        Returns:
            Acceleration
        """
        if len(series) < window * 2:
            return 0.0

        try:
            # Calcule velocity sur 2 périodes
            mid = window
            v1 = self._calculate_velocity(series[:-mid], window=window)
            v2 = self._calculate_velocity(series, window=window)

            # Accélération = changement de velocity
            acceleration = v2 - v1
            return float(acceleration)

        except Exception:
            return 0.0

    def _calculate_distance(
        self,
        vwap: float,
        price: float
    ) -> Tuple[float, float]:
        """
        Calcule distance prix/VWAP

        Returns:
            (distance_pips, distance_percent)
        """
        try:
            # Distance brute
            distance = price - vwap

            # Distance en pips
            pip_value = self.config.asset.pip_value
            distance_pips = distance / pip_value

            # Distance en pourcentage
            distance_percent = (distance / vwap * 100.0) if vwap > 0 else 0.0

            return float(distance_pips), float(distance_percent)

        except Exception:
            return 0.0, 0.0

    def _classify_zone(self, distance_pips: float) -> VWAPZone:
        """
        Classifie la zone selon distance

        Returns:
            VWAPZone
        """
        thresholds = self.config.get_zone_thresholds()
        abs_distance = abs(distance_pips)

        if abs_distance <= thresholds['neutral']:
            return VWAPZone.NEUTRAL
        elif abs_distance <= thresholds['strong']:
            return VWAPZone.STRONG
        else:
            return VWAPZone.EXTREME

    def _detect_regime(
        self,
        vwap_array: np.ndarray,
        price_array: np.ndarray
    ) -> VWAPRegime:
        """
        Détecte le régime de marché

        Returns:
            VWAPRegime
        """
        if len(vwap_array) < 50 or len(price_array) < 50:
            return VWAPRegime.BALANCED

        try:
            # Distance moyenne sur 50 bougies
            distances = np.abs(price_array[-50:] - vwap_array[-50:])
            avg_distance = np.mean(distances)

            # Volatilité relative
            vwap_vol = np.std(vwap_array[-20:])
            price_vol = np.std(price_array[-20:])
            vol_ratio = vwap_vol / price_vol if price_vol > 0 else 1.0

            # Critères décision
            pip_value = self.config.asset.pip_value
            avg_distance_pips = avg_distance / pip_value

            # Classification
            if avg_distance_pips < 50 and vol_ratio < 0.3:
                return VWAPRegime.ACCUMULATION
            elif avg_distance_pips > 200 and vol_ratio > 0.7:
                return VWAPRegime.TRENDING
            elif 0.3 <= vol_ratio <= 0.7:
                return VWAPRegime.BALANCED
            else:
                return VWAPRegime.TRANSITIONAL

        except Exception:
            return VWAPRegime.BALANCED

    def _calculate_bands(self, vwap_array: np.ndarray) -> Dict[str, float]:
        """
        Calcule bandes Bollinger-style autour VWAP

        Returns:
            Dict avec upper/lower bands
        """
        bands = {}

        if len(vwap_array) < 20:
            return bands

        try:
            # Rolling statistics
            series = pd.Series(vwap_array)
            rolling_mean = series.rolling(window=20).mean().iloc[-1]
            rolling_std = series.rolling(window=20).std().iloc[-1]

            if pd.notna(rolling_mean) and pd.notna(rolling_std):
                for std in self.std_devs:
                    bands[f'upper_{int(std)}std'] = rolling_mean + (rolling_std * std)
                    bands[f'lower_{int(std)}std'] = rolling_mean - (rolling_std * std)

        except Exception as e:
            self.logger.debug(f"[VWAP_DERIVATIVES] Erreur bands: {e}")

        return bands

    def _calculate_confidence(
        self,
        data_length: int,
        slope: float,
        distance_pips: float
    ) -> float:
        """
        Calcule confidence score 0.0 - 1.0

        Args:
            data_length: Longueur données
            slope: Pente VWAP
            distance_pips: Distance prix/VWAP

        Returns:
            Confidence 0.0 - 1.0
        """
        confidence = 0.0

        # 1. Score longueur données (0-0.3)
        if data_length >= 100:
            confidence += 0.3
        elif data_length >= 50:
            confidence += 0.2
        elif data_length >= 20:
            confidence += 0.1

        # 2. Score slope significative (0-0.4)
        slope_threshold = self.config.get_slope_threshold()
        if abs(slope) >= slope_threshold * 2:
            confidence += 0.4
        elif abs(slope) >= slope_threshold:
            confidence += 0.2

        # 3. Score distance claire (0-0.3)
        thresholds = self.config.get_zone_thresholds()
        abs_distance = abs(distance_pips)

        if abs_distance >= thresholds['strong']:
            confidence += 0.3
        elif abs_distance >= thresholds['neutral']:
            confidence += 0.15

        return min(1.0, confidence)

    def _calculate_quality_score(self, vwap_array: np.ndarray) -> float:
        """
        Calcule score qualité données

        Returns:
            Quality 0.0 - 1.0
        """
        if len(vwap_array) == 0:
            return 0.0

        try:
            # 1. Longueur suffisante
            length_score = min(1.0, len(vwap_array) / 100.0)

            # 2. Pas trop de NaN
            nan_count = np.isnan(vwap_array).sum()
            nan_rate = nan_count / len(vwap_array)
            nan_score = max(0.0, 1.0 - nan_rate)

            # 3. Variation raisonnable (pas flat)
            std_dev = np.nanstd(vwap_array)
            variation_score = min(1.0, std_dev / 10.0) if std_dev > 0 else 0.0

            # Score final
            quality = (
                0.4 * length_score +
                0.4 * nan_score +
                0.2 * variation_score
            )

            return float(quality)

        except Exception:
            return 0.5  # Fallback neutre
