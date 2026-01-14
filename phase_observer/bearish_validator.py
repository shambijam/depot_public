"""
🐻 BEARISH TRADE VALIDATOR v1.0
Validation croisée pour améliorer les trades SHORT (BEARISH)

Créé: 14 JAN 2026
Auteur: System

Problème résolu:
- BULLISH: Delta positif fonctionne bien ✅
- BEARISH: Delta négatif pas fiable ❌

Solution:
- BEARISH nécessite validation croisée de 2 analyseurs:
  1. institutional_reversal_detector (détecte QUAND renverser)
  2. price_memory_analyzer (détecte OÙ le prix va réagir)

Architecture:
- 3 niveaux de validation: STRONG / MODERATE / WEAK
- Score composite pour trades BEARISH
- Logs détaillés pour debugging
"""

import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Any
from dataclasses import dataclass
import logging


@dataclass
class BearishValidation:
    """Résultat de validation d'un trade BEARISH"""
    validation_level: str  # STRONG / MODERATE / WEAK / REJECTED
    confidence: float      # 0.0-1.0
    score_boost: float     # -20 à +20 (ajustement du score)
    reasons: List[str]     # Raisons détaillées
    reversal_data: Dict    # Données reversal detector
    memory_data: Dict      # Données price memory
    should_take_trade: bool


class BearishValidator:
    """
    🐻 VALIDATEUR DE TRADES BEARISH

    Combine 2 analyseurs pour améliorer les shorts:
    1. Reversal Detector: Détecte épuisement + renversement BEARISH
    2. Price Memory: Vérifie qu'on est proche d'une résistance historique

    Trade BEARISH validé si:
    - Reversal score >= 65 ET direction = BEARISH
    - Prix proche d'un swing high (< 10 pips)
    - Réactions passées montrent bounce_down (≥ 70%)
    """

    def __init__(
        self,
        reversal_detector,
        price_memory_analyzer,
        logger=None
    ):
        """
        Args:
            reversal_detector: Instance de InstitutionalReversalDetector
            price_memory_analyzer: Instance de PriceMemoryAnalyzer
            logger: Logger pour debug
        """
        self.reversal_detector = reversal_detector
        self.price_memory = price_memory_analyzer
        self.logger = logger or logging.getLogger(__name__)

        # Config validation
        self.config = {
            "strong_validation": {
                "min_reversal_score": 75,
                "min_memory_confidence": 0.7,
                "max_distance_pips": 10.0,
                "min_bounce_ratio": 0.7,
                "score_boost": +20.0
            },
            "moderate_validation": {
                "min_reversal_score": 60,
                "min_memory_confidence": 0.5,
                "max_distance_pips": 15.0,
                "min_bounce_ratio": 0.6,
                "score_boost": +10.0
            },
            "weak_validation": {
                "min_reversal_score": 50,
                "min_memory_confidence": 0.3,
                "max_distance_pips": 20.0,
                "min_bounce_ratio": 0.5,
                "score_boost": +3.0
            }
        }

    def validate_bearish_trade(
        self,
        market_data: Dict,
        current_price: float,
        historical_data: pd.DataFrame
    ) -> BearishValidation:
        """
        Valide un trade BEARISH par validation croisée

        Args:
            market_data: Dict pour reversal_detector {
                'candles_m5': DataFrame,
                'candles_m1': DataFrame,
                'cvd_values': List[float],
                'delta_values': List[float],
                'volume_values': List[float]
            }
            current_price: Prix actuel
            historical_data: DataFrame M5 pour price_memory (minimum 50 bougies)

        Returns:
            BearishValidation avec niveau, confidence, score_boost
        """

        # === ÉTAPE 1: REVERSAL DETECTOR ===
        try:
            reversal_result = self.reversal_detector.detect_reversal(market_data)
        except Exception as e:
            self.logger.error(f"Erreur reversal_detector: {e}")
            return self._rejected_validation("REVERSAL_DETECTOR_ERROR", str(e))

        reversal_score = reversal_result.get('institutional_score', 0.0)
        reversal_trend = reversal_result.get('new_trend', 'NEUTRAL')
        reversal_conviction = reversal_result.get('conviction_level', 'LOW')

        # === ÉTAPE 2: PRICE MEMORY ANALYZER ===
        try:
            memory_result = self.price_memory.analyze_price_memory(
                historical_data=historical_data,
                current_price=current_price
            )
        except Exception as e:
            self.logger.error(f"Erreur price_memory: {e}")
            return self._rejected_validation("PRICE_MEMORY_ERROR", str(e))

        memory_signals = memory_result.get('memory_signals', [])
        closest_memory = memory_result.get('closest_memory', None)

        # === ÉTAPE 3: ANALYSE DE CONVERGENCE ===

        # 3.1 Vérifier direction BEARISH du reversal
        if reversal_trend != 'BEARISH':
            return self._rejected_validation(
                "WRONG_DIRECTION",
                f"Reversal trend is {reversal_trend}, not BEARISH"
            )

        # 3.2 Trouver résistances proches (swing highs)
        nearby_resistances = self._find_nearby_resistances(
            historical_data,
            current_price,
            max_distance_pips=20.0
        )

        if not nearby_resistances:
            return self._rejected_validation(
                "NO_RESISTANCE",
                "No resistance level nearby (within 20 pips)"
            )

        # 3.3 Analyser réactions passées aux résistances
        best_resistance = nearby_resistances[0]  # La plus proche
        bounce_ratio = self._calculate_bounce_ratio(
            historical_data,
            best_resistance['level']
        )

        # === ÉTAPE 4: CALCUL DU NIVEAU DE VALIDATION ===
        validation_level, confidence, score_boost, reasons = self._determine_validation_level(
            reversal_score=reversal_score,
            reversal_conviction=reversal_conviction,
            distance_pips=best_resistance['distance_pips'],
            bounce_ratio=bounce_ratio,
            memory_confidence=closest_memory['confidence'] if closest_memory else 0.0
        )

        # === ÉTAPE 5: LOG DÉTAILLÉ ===
        self.logger.critical(
            f"🐻 [BEARISH_VALIDATION] Level={validation_level} | "
            f"Confidence={confidence:.2f} | "
            f"Boost={score_boost:+.1f} | "
            f"Reversal={reversal_score:.0f}/100 ({reversal_conviction}) | "
            f"Resistance={best_resistance['distance_pips']:.1f} pips | "
            f"Bounce ratio={bounce_ratio:.0%}"
        )

        return BearishValidation(
            validation_level=validation_level,
            confidence=confidence,
            score_boost=score_boost,
            reasons=reasons,
            reversal_data=reversal_result,
            memory_data=memory_result,
            should_take_trade=(validation_level in ['STRONG', 'MODERATE'])
        )

    def _find_nearby_resistances(
        self,
        historical_data: pd.DataFrame,
        current_price: float,
        max_distance_pips: float = 20.0
    ) -> List[Dict]:
        """
        Trouve les swing highs (résistances) proches du prix actuel

        Args:
            historical_data: DataFrame M5
            current_price: Prix actuel
            max_distance_pips: Distance max en pips

        Returns:
            List[Dict]: Résistances triées par distance (la plus proche d'abord)
        """
        if len(historical_data) < 10:
            return []

        # Trouver swing highs (fenêtre 3)
        window = 3
        swing_highs = []

        for i in range(window, len(historical_data) - window):
            current_high = historical_data.iloc[i]['high']

            # Vérifier si c'est le plus haut dans la fenêtre
            is_swing_high = True
            for j in range(i - window, i + window + 1):
                if j != i and historical_data.iloc[j]['high'] >= current_high:
                    is_swing_high = False
                    break

            if is_swing_high:
                # Calculer distance en pips
                distance_pips = abs(current_high - current_price) * 10000

                if distance_pips <= max_distance_pips:
                    swing_highs.append({
                        'level': current_high,
                        'distance_pips': distance_pips,
                        'index': i
                    })

        # Trier par distance (plus proche d'abord)
        swing_highs.sort(key=lambda x: x['distance_pips'])

        return swing_highs

    def _calculate_bounce_ratio(
        self,
        historical_data: pd.DataFrame,
        level: float,
        tolerance_pips: float = 5.0
    ) -> float:
        """
        Calcule le ratio de bounces vs breaks à un niveau

        Args:
            historical_data: DataFrame M5
            level: Niveau à tester
            tolerance_pips: Tolérance en pips

        Returns:
            float: Ratio 0.0-1.0 (1.0 = 100% bounces)
        """
        tolerance = tolerance_pips / 10000
        bounces = 0
        breaks = 0

        for i in range(1, len(historical_data)):
            prev_close = historical_data.iloc[i - 1]['close']
            curr_low = historical_data.iloc[i]['low']
            curr_high = historical_data.iloc[i]['high']
            curr_close = historical_data.iloc[i]['close']

            # Le prix a touché le niveau ?
            level_touched = (curr_low <= level + tolerance and curr_high >= level - tolerance)

            if level_touched:
                # Bounce down : Prix monte vers niveau et redescend
                if prev_close < level and curr_high >= level - tolerance and curr_close < level:
                    bounces += 1

                # Break up : Prix traverse le niveau vers le haut
                elif prev_close < level and curr_close > level + tolerance:
                    breaks += 1

        total = bounces + breaks
        if total == 0:
            return 0.5  # Incertitude si aucune donnée

        return bounces / total

    def _determine_validation_level(
        self,
        reversal_score: float,
        reversal_conviction: str,
        distance_pips: float,
        bounce_ratio: float,
        memory_confidence: float
    ) -> tuple:
        """
        Détermine le niveau de validation basé sur les critères

        Returns:
            tuple: (validation_level, confidence, score_boost, reasons)
        """
        reasons = []

        # === STRONG VALIDATION ===
        strong_cfg = self.config['strong_validation']

        if (reversal_score >= strong_cfg['min_reversal_score'] and
            distance_pips <= strong_cfg['max_distance_pips'] and
            bounce_ratio >= strong_cfg['min_bounce_ratio'] and
            memory_confidence >= strong_cfg['min_memory_confidence']):

            reasons.append(f"✅ Reversal score HIGH: {reversal_score:.0f}/100")
            reasons.append(f"✅ Resistance très proche: {distance_pips:.1f} pips")
            reasons.append(f"✅ Bounce ratio élevé: {bounce_ratio:.0%}")
            reasons.append(f"✅ Memory confidence: {memory_confidence:.2f}")

            return ('STRONG', 0.9, strong_cfg['score_boost'], reasons)

        # === MODERATE VALIDATION ===
        moderate_cfg = self.config['moderate_validation']

        if (reversal_score >= moderate_cfg['min_reversal_score'] and
            distance_pips <= moderate_cfg['max_distance_pips'] and
            bounce_ratio >= moderate_cfg['min_bounce_ratio']):

            reasons.append(f"✓ Reversal score MODERATE: {reversal_score:.0f}/100")
            reasons.append(f"✓ Resistance proche: {distance_pips:.1f} pips")
            reasons.append(f"✓ Bounce ratio acceptable: {bounce_ratio:.0%}")

            return ('MODERATE', 0.7, moderate_cfg['score_boost'], reasons)

        # === WEAK VALIDATION ===
        weak_cfg = self.config['weak_validation']

        if (reversal_score >= weak_cfg['min_reversal_score'] and
            distance_pips <= weak_cfg['max_distance_pips']):

            reasons.append(f"⚠ Reversal score LOW: {reversal_score:.0f}/100")
            reasons.append(f"⚠ Resistance éloignée: {distance_pips:.1f} pips")
            reasons.append(f"⚠ Bounce ratio faible: {bounce_ratio:.0%}")

            return ('WEAK', 0.5, weak_cfg['score_boost'], reasons)

        # === REJECTED ===
        reasons.append(f"❌ Reversal score insuffisant: {reversal_score:.0f}/100")
        reasons.append(f"❌ Pas de résistance proche ou bounce ratio faible")

        return ('REJECTED', 0.0, -15.0, reasons)

    def _rejected_validation(self, reason: str, details: str = "") -> BearishValidation:
        """Retourne une validation rejetée"""
        return BearishValidation(
            validation_level='REJECTED',
            confidence=0.0,
            score_boost=-15.0,
            reasons=[f"❌ {reason}: {details}"],
            reversal_data={},
            memory_data={},
            should_take_trade=False
        )
