# phase_observer/vwap/signals.py
"""
Générateur de signaux de trading VWAP institutionnel
Scoring 25 points: Trend (15) + Position (10)
"""

import logging
import numpy as np
import pandas as pd
from typing import Dict, Any, Optional, List, Tuple
from datetime import datetime

from .models import (
    VWAPDerivatives,
    VWAPSignal,
    VWAPZone,
    VWAPRegime,
    SignalAction
)
from .config import VWAPConfig
from .regime_mapper import RegimeMapper


logger = logging.getLogger(__name__)


class VWAPSignalGenerator:
    """
    Générateur de signaux de trading basés sur VWAP
    Niveau institutionnel avec scoring 25 points
    """

    def __init__(self, config: VWAPConfig):
        """
        Initialise le générateur de signaux

        Args:
            config: Configuration VWAP
        """
        self.config = config
        self.symbol = config.symbol
        self.logger = logging.getLogger(f"{__name__}.{self.symbol}")

        # Seuils de configuration
        self.slope_threshold = config.get_slope_threshold()
        self.zone_thresholds = config.get_zone_thresholds()
        self.multipliers = config.signals.multipliers
        self.min_confidence = config.signals.min_confidence

    def generate_signal(
        self,
        derivatives: VWAPDerivatives,
        current_price: float,
        context: Optional[Dict[str, Any]] = None
    ) -> VWAPSignal:
        """
        Génère un signal de trading complet

        Args:
            derivatives: Dérivés VWAP calculés
            current_price: Prix actuel
            context: Contexte additionnel (MTF, orderflow, etc.)

        Returns:
            VWAPSignal avec scoring 25 points
        """
        try:
            # 1. Score trend (0-15 points)
            trend_score, trend_bias = self._score_trend(derivatives, context)

            # 2. Score position (0-10 points)
            position_score, position_bias = self._score_position(
                derivatives,
                current_price
            )

            # 3. Bias final et action
            final_bias, action = self._determine_final_bias(
                trend_bias,
                position_bias,
                derivatives
            )

            # 4. Adaptive scoring basé sur régime (✅ NEW)
            # Appliquer les poids adaptatifs selon le régime VWAP
            adjustments = RegimeMapper.get_scoring_adjustment(derivatives.regime)
            trend_weight = adjustments['trend_weight']
            position_weight = adjustments['position_weight']

            # Ajuster les scores selon le régime
            adjusted_trend_score = trend_score * trend_weight
            adjusted_position_score = position_score * position_weight

            # Score total ajusté (0-25 points max)
            # Normaliser pour conserver max 25 points
            total_raw = adjusted_trend_score + adjusted_position_score
            normalization_factor = (trend_weight * 15 + position_weight * 10) / 25.0
            total_score = min(25.0, total_raw / normalization_factor)

            # Log adaptation si significative
            if abs(trend_weight - 1.0) > 0.05 or abs(position_weight - 1.0) > 0.05:
                self.logger.debug(
                    f"[VWAP_SIGNALS] Adaptive scoring | "
                    f"Regime={derivatives.regime.value} | "
                    f"TrendW={trend_weight:.2f} | PositionW={position_weight:.2f} | "
                    f"Raw={trend_score:.1f}+{position_score:.1f}={trend_score+position_score:.1f} → "
                    f"Adjusted={total_score:.1f}"
                )

            # 5. Détection type de signal
            signal_type = self._detect_signal_type(derivatives, current_price)

            # 6. Strength (0-1) et confidence
            # Utiliser les scores ajustés pour strength calculation
            strength = self._calculate_strength(
                adjusted_trend_score,
                adjusted_position_score,
                derivatives,
                trend_weight,
                position_weight
            )
            confidence = derivatives.confidence

            # 7. Triggers détaillés
            triggers = self._build_triggers(
                derivatives,
                adjusted_trend_score,
                position_score,
                signal_type
            )

            # 8. Métadonnées (incluant adaptive scoring info)
            metadata = {
                'slope_20': derivatives.slope_20,
                'slope_50': derivatives.slope_50,
                'curvature': derivatives.curvature,
                'velocity': derivatives.velocity,
                'zone': derivatives.zone.value,
                'regime': derivatives.regime.value,
                'quality_score': derivatives.quality_score,
                # ✅ Adaptive scoring info
                'raw_trend_score': trend_score,
                'raw_position_score': position_score,
                'raw_total_score': trend_score + position_score,
                'trend_weight': trend_weight,
                'position_weight': position_weight,
                'adjusted_trend_score': adjusted_trend_score,
                'adjusted_position_score': adjusted_position_score,
            }

            return VWAPSignal(
                timestamp=derivatives.timestamp,
                symbol=self.symbol,
                signal_type=signal_type,
                action=action,
                strength=strength,
                confidence=confidence,
                vwap_value=current_price - derivatives.distance_pips * self.config.asset.pip_value,
                current_price=current_price,
                distance_pips=derivatives.distance_pips,
                slope=derivatives.slope_20,
                zone=derivatives.zone,
                regime=derivatives.regime,
                trend_score=adjusted_trend_score,  # ✅ Use adjusted scores
                position_score=adjusted_position_score,  # ✅ Use adjusted scores
                total_score=total_score,  # ✅ Already adjusted and normalized
                metadata=metadata,
                triggers=triggers,
            )

        except Exception as e:
            self.logger.error(f"[VWAP_SIGNALS] Erreur génération signal: {e}", exc_info=True)
            # Signal neutre par défaut
            return self._create_neutral_signal(derivatives, current_price)

    def _score_trend(
        self,
        derivatives: VWAPDerivatives,
        context: Optional[Dict[str, Any]] = None
    ) -> Tuple[float, str]:
        """
        Score tendance VWAP (0-15 points)

        Critères:
        - Slope significative (0-6 pts)
        - Cohérence multi-fenêtre (0-4 pts)
        - Régime favorable (0-3 pts)
        - Courbure/accélération (0-2 pts)

        Returns:
            (score, bias) avec bias = 'BUY', 'SELL', 'NEUTRAL'
        """
        score = 0.0
        bias = 'NEUTRAL'

        # 1. Slope significative (0-6 points)
        slope_20 = derivatives.slope_20
        slope_50 = derivatives.slope_50
        slope_100 = derivatives.slope_100

        # Score basé sur magnitude slope principale
        abs_slope = abs(slope_20)
        if abs_slope >= self.slope_threshold * 3:
            score += 6.0
        elif abs_slope >= self.slope_threshold * 2:
            score += 4.0
        elif abs_slope >= self.slope_threshold:
            score += 2.0

        # Détermination bias
        if slope_20 > self.slope_threshold:
            bias = 'BUY'
        elif slope_20 < -self.slope_threshold:
            bias = 'SELL'

        # 2. Cohérence multi-fenêtre (0-4 points)
        # Toutes les slopes alignées
        slopes = [slope_20, slope_50, slope_100]
        if all(s > 0 for s in slopes):
            score += 4.0  # Toutes haussières
        elif all(s < 0 for s in slopes):
            score += 4.0  # Toutes baissières
        elif abs(slope_20 - slope_50) < self.slope_threshold:
            score += 2.0  # Slope 20/50 alignées

        # 3. Régime favorable (0-3 points)
        if derivatives.regime == VWAPRegime.TRENDING:
            score += 3.0  # Trending clair
        elif derivatives.regime == VWAPRegime.BALANCED:
            score += 1.5  # Équilibré
        elif derivatives.regime == VWAPRegime.ACCUMULATION:
            score += 1.0  # Accumulation

        # 4. Courbure/Accélération (0-2 points)
        # Accélération dans le bon sens
        if bias == 'BUY' and derivatives.acceleration > 0:
            score += 2.0
        elif bias == 'SELL' and derivatives.acceleration < 0:
            score += 2.0
        elif abs(derivatives.curvature) < self.slope_threshold * 0.5:
            score += 1.0  # Courbure faible = momentum stable

        # Cap à 15 points
        score = min(15.0, score)

        return score, bias

    def _score_position(
        self,
        derivatives: VWAPDerivatives,
        current_price: float
    ) -> Tuple[float, str]:
        """
        Score position prix/VWAP (0-10 points)

        Critères:
        - Distance favorable (0-5 pts)
        - Zone classification (0-3 pts)
        - Qualité données (0-2 pts)

        Returns:
            (score, bias) avec bias = 'BUY', 'SELL', 'NEUTRAL'
        """
        score = 0.0
        bias = 'NEUTRAL'

        distance_pips = derivatives.distance_pips
        zone = derivatives.zone

        # 1. Distance favorable (0-5 points)
        # Prix au-dessus VWAP = potentiel haussier
        # Prix en-dessous VWAP = potentiel baissier
        abs_distance = abs(distance_pips)

        if abs_distance <= self.zone_thresholds['neutral']:
            # Zone neutre: plein scoring
            score += 5.0
        elif abs_distance <= self.zone_thresholds['strong']:
            # Zone strong: scoring réduit
            score += 3.0
        elif abs_distance <= self.zone_thresholds['extreme']:
            # Zone extreme: scoring très réduit
            score += 1.0

        # Détermination bias selon position
        if distance_pips > 0:
            bias = 'BUY'  # Prix au-dessus VWAP
        elif distance_pips < 0:
            bias = 'SELL'  # Prix en-dessous VWAP

        # 2. Zone classification (0-3 points)
        if zone == VWAPZone.NEUTRAL:
            score += 3.0  # Meilleure zone pour trading
        elif zone == VWAPZone.STRONG:
            score += 1.5  # Zone acceptable
        elif zone == VWAPZone.EXTREME:
            score += 0.5  # Zone risquée

        # 3. Qualité données (0-2 points)
        score += derivatives.quality_score * 2.0

        # Cap à 10 points
        score = min(10.0, score)

        return score, bias

    def _determine_final_bias(
        self,
        trend_bias: str,
        position_bias: str,
        derivatives: VWAPDerivatives
    ) -> Tuple[str, SignalAction]:
        """
        Détermine le bias final et l'action

        Returns:
            (bias, action)
        """
        # Si trend et position alignés
        if trend_bias == position_bias and trend_bias != 'NEUTRAL':
            bias = trend_bias
            action = SignalAction.BUY if bias == 'BUY' else SignalAction.SELL
        # Si trend fort mais position neutre
        elif trend_bias != 'NEUTRAL' and position_bias == 'NEUTRAL':
            bias = trend_bias
            action = SignalAction.BUY if bias == 'BUY' else SignalAction.SELL
        # Si position forte mais trend neutre
        elif position_bias != 'NEUTRAL' and trend_bias == 'NEUTRAL':
            bias = position_bias
            action = SignalAction.BUY if bias == 'BUY' else SignalAction.SELL
        # Si désaccord ou tous neutres
        else:
            bias = 'NEUTRAL'
            action = SignalAction.HOLD

        # Filtre par confidence minimale
        if derivatives.confidence < self.min_confidence:
            action = SignalAction.HOLD

        return bias, action

    def _calculate_strength(
        self,
        trend_score: float,
        position_score: float,
        derivatives: VWAPDerivatives,
        trend_weight: float = 1.0,
        position_weight: float = 1.0
    ) -> float:
        """
        Calcule force du signal (0-1)

        Args:
            trend_score: Score trend (ajusté ou non)
            position_score: Score position (ajusté ou non)
            derivatives: Dérivés VWAP
            trend_weight: Poids appliqué au trend (pour normalisation)
            position_weight: Poids appliqué à la position (pour normalisation)

        Returns:
            Strength normalisée
        """
        # Score total normalisé avec poids
        total_score = trend_score + position_score
        max_possible = trend_weight * 15 + position_weight * 10
        base_strength = total_score / max_possible if max_possible > 0 else 0.0

        # Ajustement par confidence
        strength = base_strength * derivatives.confidence

        # Ajustement par zone
        if derivatives.zone == VWAPZone.EXTREME:
            strength *= 0.7  # Réduction en zone extreme
        elif derivatives.zone == VWAPZone.STRONG:
            strength *= 0.85

        # ✅ REMOVED: Ajustement par régime (déjà fait via adaptive scoring)

        return min(1.0, max(0.0, strength))

    def _detect_signal_type(
        self,
        derivatives: VWAPDerivatives,
        current_price: float
    ) -> str:
        """
        Détecte le type de signal

        Returns:
            Signal type string
        """
        distance_pips = derivatives.distance_pips
        slope = derivatives.slope_20
        zone = derivatives.zone
        bands = derivatives.bands

        # 1. VWAP Cross
        if abs(distance_pips) < 10:  # Très proche du VWAP
            if slope > self.slope_threshold:
                return "VWAP_CROSS_BULLISH"
            elif slope < -self.slope_threshold:
                return "VWAP_CROSS_BEARISH"
            return "VWAP_NEUTRAL"

        # 2. Band Touch
        if bands:
            vwap_value = current_price - distance_pips * self.config.asset.pip_value
            if 'upper_2std' in bands and current_price >= bands['upper_2std']:
                return "BAND_UPPER_TOUCH"
            elif 'lower_2std' in bands and current_price <= bands['lower_2std']:
                return "BAND_LOWER_TOUCH"
            elif 'upper_1std' in bands and current_price >= bands['upper_1std']:
                return "BAND_UPPER_APPROACH"
            elif 'lower_1std' in bands and current_price <= bands['lower_1std']:
                return "BAND_LOWER_APPROACH"

        # 3. Rejection
        if zone == VWAPZone.EXTREME:
            if distance_pips > 0 and slope < 0:
                return "VWAP_REJECTION_HIGH"
            elif distance_pips < 0 and slope > 0:
                return "VWAP_REJECTION_LOW"

        # 4. Trend Following
        if zone == VWAPZone.NEUTRAL or zone == VWAPZone.STRONG:
            if slope > self.slope_threshold and distance_pips > 0:
                return "VWAP_TREND_BULLISH"
            elif slope < -self.slope_threshold and distance_pips < 0:
                return "VWAP_TREND_BEARISH"

        # 5. Regime Change
        if derivatives.regime == VWAPRegime.TRANSITIONAL:
            return "REGIME_CHANGE"

        # Default
        return "VWAP_SIGNAL"

    def _build_triggers(
        self,
        derivatives: VWAPDerivatives,
        trend_score: float,
        position_score: float,
        signal_type: str
    ) -> List[Dict[str, Any]]:
        """
        Construit la liste des triggers détaillés

        Returns:
            Liste de triggers avec détails
        """
        triggers = []

        # Trigger 1: Slope significative
        if abs(derivatives.slope_20) >= self.slope_threshold:
            triggers.append({
                'type': 'slope_significant',
                'value': derivatives.slope_20,
                'threshold': self.slope_threshold,
                'contribution': min(6.0, abs(derivatives.slope_20) / self.slope_threshold * 2),
            })

        # Trigger 2: Position favorable
        if derivatives.zone == VWAPZone.NEUTRAL:
            triggers.append({
                'type': 'zone_neutral',
                'zone': derivatives.zone.value,
                'distance_pips': derivatives.distance_pips,
                'contribution': 5.0,
            })

        # Trigger 3: Régime favorable
        if derivatives.regime == VWAPRegime.TRENDING:
            triggers.append({
                'type': 'regime_trending',
                'regime': derivatives.regime.value,
                'contribution': 3.0,
            })

        # Trigger 4: Multi-timeframe alignement
        if all([
            derivatives.slope_20 > 0,
            derivatives.slope_50 > 0,
            derivatives.slope_100 > 0
        ]) or all([
            derivatives.slope_20 < 0,
            derivatives.slope_50 < 0,
            derivatives.slope_100 < 0
        ]):
            triggers.append({
                'type': 'mtf_aligned',
                'slopes': [derivatives.slope_20, derivatives.slope_50, derivatives.slope_100],
                'contribution': 4.0,
            })

        # Trigger 5: Accélération
        if abs(derivatives.acceleration) > 0.0001:
            triggers.append({
                'type': 'acceleration',
                'value': derivatives.acceleration,
                'contribution': 2.0,
            })

        return triggers

    def _create_neutral_signal(
        self,
        derivatives: VWAPDerivatives,
        current_price: float
    ) -> VWAPSignal:
        """
        Crée un signal neutre (fallback)

        Returns:
            VWAPSignal neutre
        """
        return VWAPSignal(
            timestamp=derivatives.timestamp,
            symbol=self.symbol,
            signal_type="VWAP_NEUTRAL",
            action=SignalAction.HOLD,
            strength=0.0,
            confidence=0.0,
            vwap_value=current_price - derivatives.distance_pips * self.config.asset.pip_value,
            current_price=current_price,
            distance_pips=derivatives.distance_pips,
            slope=derivatives.slope_20,
            zone=derivatives.zone,
            regime=derivatives.regime,
            trend_score=0.0,
            position_score=0.0,
            total_score=0.0,
            metadata={},
            triggers=[],
        )

    def apply_multipliers(
        self,
        signal: VWAPSignal,
        is_aligned: bool
    ) -> float:
        """
        Applique les multiplicateurs selon alignement

        Args:
            signal: Signal VWAP
            is_aligned: True si signal aligné avec trend principal

        Returns:
            Score ajusté
        """
        base_score = signal.total_score
        zone = signal.zone

        # Sélection multiplicateur
        if is_aligned:
            if zone == VWAPZone.NEUTRAL:
                multiplier = self.multipliers['aligned_neutral']
            elif zone == VWAPZone.STRONG:
                multiplier = self.multipliers['aligned_strong']
            else:  # EXTREME
                multiplier = self.multipliers['aligned_extreme']
        else:
            if zone == VWAPZone.NEUTRAL:
                multiplier = self.multipliers['against_neutral']
            elif zone == VWAPZone.STRONG:
                multiplier = self.multipliers['against_strong']
            else:  # EXTREME
                multiplier = self.multipliers['against_extreme']

        adjusted_score = base_score * multiplier

        self.logger.debug(
            f"[VWAP_SIGNALS] Multiplicateur appliqué: {multiplier:.2f} | "
            f"Score: {base_score:.2f} -> {adjusted_score:.2f} | "
            f"Aligned={is_aligned}, Zone={zone.value}"
        )

        return adjusted_score

    def get_statistics(self) -> Dict[str, Any]:
        """
        Retourne statistiques du générateur

        Returns:
            Stats dict
        """
        return {
            'symbol': self.symbol,
            'slope_threshold': self.slope_threshold,
            'zone_thresholds': self.zone_thresholds,
            'min_confidence': self.min_confidence,
            'multipliers': self.multipliers,
        }
