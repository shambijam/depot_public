"""
🐻 BEARISH SCALPING VALIDATOR M1 v1.0
Validation croisée ultra-rapide pour trades BEARISH en scalping burst mode

Créé: 14 JAN 2026
Auteur: System
Budget: < 150ms par validation

Problème résolu:
- BULLISH: Delta positif fonctionne bien ✅
- BEARISH: Delta négatif pas fiable ❌ (score orderflow 3x plus faible)

Solution:
- BEARISH nécessite validation croisée de 3 analyseurs:
  1. institutional_reversal_detector (détecte QUAND renverser)
  2. price_memory_analyzer (détecte OÙ le prix va réagir - micro-résistances M1)
  3. timing_validator (détecte le MOMENT optimal 30-45s)

Architecture:
- FAST-TRACK: < 5s (reversal élevé + résistance très proche)
- STANDARD: 10-15s (validation complète)
- 3 niveaux: STRONG (+20 pts) / MODERATE (+10 pts) / WEAK (+3 pts) / REJECTED (0 pts)
"""

import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Any
from dataclasses import dataclass
import logging
from datetime import datetime


@dataclass
class BearishValidationResult:
    """Résultat de validation d'un trade BEARISH"""
    validation_level: str  # STRONG / MODERATE / WEAK / REJECTED
    confidence: float      # 0.0-1.0
    score_boost: float     # 0 à +20 (ajustement du score composite)
    reasons: List[str]     # Raisons détaillées
    reversal_score: float  # 0-100
    reversal_conviction: str  # HIGH / MODERATE / CAUTION / LOW
    micro_resistance: Optional[Dict]  # Données micro-résistance M1
    timing_quality: str    # OPTIMAL / ACCEPTABLE / LATE
    candle_age_seconds: float  # Âge de la bougie actuelle
    should_take_trade: bool


class BearishScalpingValidator:
    """
    🐻 VALIDATEUR BEARISH M1 ULTRA-RAPIDE

    Optimisé pour scalping burst (< 22s total):
    - Phase 1 (2-3s): Signal rapide (delta négatif)
    - Phase 2 (5-8s): Validation sniper (micro-résistance M1)
    - Phase 3 (10-15s): Confirmation institutionnelle

    Budget: < 150ms par validation
    """

    def __init__(
        self,
        reversal_detector,
        price_memory_analyzer,
        config: Optional[Dict] = None,
        logger=None
    ):
        """
        Args:
            reversal_detector: Instance de InstitutionalReversalDetector
            price_memory_analyzer: Instance de PriceMemoryAnalyzer
            config: Config custom (optionnel)
            logger: Logger pour debug
        """
        self.reversal_detector = reversal_detector
        self.price_memory = price_memory_analyzer
        self.logger = logger or logging.getLogger(__name__)

        # Config par défaut (M1 ultra-rapide)
        self.config = config or self._default_config()

        self.logger.info("✅ [BEARISH_SCALPING_VALIDATOR] Initialisé (M1 ultra-rapide)")

    def _default_config(self) -> Dict:
        """Configuration par défaut"""
        return {
            # === STRONG VALIDATION (Fast-track < 5s) ===
            "strong": {
                "min_reversal_score": 70,
                "min_reversal_conviction": "MODERATE",  # HIGH ou MODERATE
                "min_micro_resistance_distance_pips": 2.0,
                "max_micro_resistance_distance_pips": 10.0,
                "min_bounce_probability": 0.65,
                "max_resistance_age_minutes": 10,
                "timing_window": (30, 45),  # 30-45s optimal
                "score_boost": +20.0
            },

            # === MODERATE VALIDATION (Standard 10-15s) ===
            "moderate": {
                "min_reversal_score": 55,
                "min_reversal_conviction": "CAUTION",  # MODERATE ou CAUTION
                "min_micro_resistance_distance_pips": 3.0,
                "max_micro_resistance_distance_pips": 15.0,
                "min_bounce_probability": 0.55,
                "max_resistance_age_minutes": 15,
                "timing_window": (20, 48),  # 20-48s acceptable
                "score_boost": +10.0
            },

            # === WEAK VALIDATION (Minimal) ===
            "weak": {
                "min_reversal_score": 45,
                "min_reversal_conviction": "LOW",  # Accepte tous niveaux
                "min_micro_resistance_distance_pips": 5.0,
                "max_micro_resistance_distance_pips": 20.0,
                "min_bounce_probability": 0.45,
                "max_resistance_age_minutes": 20,
                "timing_window": (10, 52),  # 10-52s large window
                "score_boost": +3.0
            },

            # === ASSET-SPECIFIC THRESHOLDS ===
            "assets": {
                "NAS100": {
                    "pip_multiplier": 0.25,  # NAS100 points × 0.25
                    "min_reversal_score_override": 65,  # Plus strict
                    "strong_boost_override": +25.0,  # Boost plus important
                    "moderate_boost_override": +12.0
                },
                "USDJPY": {
                    "pip_multiplier": 1.0,
                    "min_reversal_score_override": 60,
                    "strong_boost_override": +20.0,
                    "moderate_boost_override": +10.0
                },
                "GBPUSD": {
                    "pip_multiplier": 1.0,
                    "min_reversal_score_override": 60,
                    "strong_boost_override": +20.0,
                    "moderate_boost_override": +10.0
                }
            }
        }

    def validate_bearish_trade(
        self,
        symbol: str,
        current_price: float,
        current_time: datetime,
        candle_open_time: datetime,
        historical_data_m1: pd.DataFrame,
        reversal_result: Optional[Dict] = None
    ) -> BearishValidationResult:
        """
        🎯 VALIDATION BEARISH PRINCIPALE

        Args:
            symbol: Asset (NAS100, USDJPY, GBPUSD)
            current_price: Prix actuel
            current_time: Timestamp actuel
            candle_open_time: Ouverture bougie M1 actuelle
            historical_data_m1: DataFrame M1 (minimum 50 bougies)
            reversal_result: Résultat du reversal detector (optionnel si déjà calculé)

        Returns:
            BearishValidationResult avec niveau, confidence, score_boost
        """

        start_time = datetime.now()

        # === ÉTAPE 1: REVERSAL DETECTOR ===
        if not reversal_result:
            self.logger.warning(
                f"[BEARISH_VALIDATOR][{symbol}] Pas de reversal_result fourni, "
                "validation BEARISH impossible"
            )
            return self._rejected("NO_REVERSAL_DATA", "Reversal detector pas exécuté")

        reversal_score = reversal_result.get('institutional_score', 0.0)
        reversal_trend = reversal_result.get('new_trend', 'NEUTRAL')
        reversal_conviction = reversal_result.get('conviction_level', 'LOW')

        # Vérifier direction BEARISH
        if reversal_trend != 'BEARISH':
            return self._rejected(
                "WRONG_DIRECTION",
                f"Reversal trend is {reversal_trend}, not BEARISH"
            )

        self.logger.debug(
            f"[BEARISH_VALIDATOR][{symbol}] Reversal: "
            f"score={reversal_score:.1f}, trend={reversal_trend}, conviction={reversal_conviction}"
        )

        # === ÉTAPE 2: MICRO-RÉSISTANCE M1 ===
        micro_resistance = None

        try:
            # Vérifier que price_memory a la méthode detect_micro_resistance_m1
            if hasattr(self.price_memory, 'detect_micro_resistance_m1'):
                micro_resistance = self.price_memory.detect_micro_resistance_m1(
                    historical_data=historical_data_m1,
                    current_price=current_price,
                    lookback_minutes=15
                )

                if micro_resistance:
                    self.logger.debug(
                        f"[BEARISH_VALIDATOR][{symbol}] Micro-résistance: "
                        f"distance={micro_resistance.get('distance_pips', 0):.1f}p, "
                        f"bounce={micro_resistance.get('bounce_probability', 0):.0%}, "
                        f"age={micro_resistance.get('age_minutes', 0)}min"
                    )
                else:
                    self.logger.debug(f"[BEARISH_VALIDATOR][{symbol}] Aucune micro-résistance proche")
            else:
                self.logger.warning(
                    f"[BEARISH_VALIDATOR][{symbol}] "
                    "PriceMemoryAnalyzer.detect_micro_resistance_m1() non disponible"
                )

        except Exception as e:
            self.logger.error(f"[BEARISH_VALIDATOR][{symbol}] Erreur micro-résistance: {e}")
            micro_resistance = None

        # === ÉTAPE 3: TIMING QUALITY ===
        candle_age_seconds = (current_time - candle_open_time).total_seconds()
        timing_quality = self._evaluate_timing(candle_age_seconds)

        self.logger.debug(
            f"[BEARISH_VALIDATOR][{symbol}] Timing: "
            f"age={candle_age_seconds:.0f}s, quality={timing_quality}"
        )

        # === ÉTAPE 4: DÉTERMINER NIVEAU DE VALIDATION ===
        validation_level, confidence, score_boost, reasons = self._determine_validation_level(
            symbol=symbol,
            reversal_score=reversal_score,
            reversal_conviction=reversal_conviction,
            micro_resistance=micro_resistance,
            timing_quality=timing_quality,
            candle_age_seconds=candle_age_seconds
        )

        # === ÉTAPE 5: LOG DÉTAILLÉ ===
        elapsed_ms = (datetime.now() - start_time).total_seconds() * 1000

        self.logger.critical(
            f"🐻 [BEARISH_VALIDATION][{symbol}] "
            f"Level={validation_level} | "
            f"Confidence={confidence:.2f} | "
            f"Boost={score_boost:+.1f} | "
            f"Reversal={reversal_score:.0f}/100 ({reversal_conviction}) | "
            f"Resistance={micro_resistance.get('distance_pips', 0) if micro_resistance else 0:.1f}p | "
            f"Timing={timing_quality} ({candle_age_seconds:.0f}s) | "
            f"Elapsed={elapsed_ms:.0f}ms"
        )

        # Afficher les raisons
        for reason in reasons:
            self.logger.info(f"  {reason}")

        return BearishValidationResult(
            validation_level=validation_level,
            confidence=confidence,
            score_boost=score_boost,
            reasons=reasons,
            reversal_score=reversal_score,
            reversal_conviction=reversal_conviction,
            micro_resistance=micro_resistance,
            timing_quality=timing_quality,
            candle_age_seconds=candle_age_seconds,
            should_take_trade=(validation_level in ['STRONG', 'MODERATE'])
        )

    def _evaluate_timing(self, candle_age_seconds: float) -> str:
        """
        Évalue la qualité du timing dans la bougie M1

        Args:
            candle_age_seconds: Âge de la bougie en secondes

        Returns:
            str: OPTIMAL / ACCEPTABLE / LATE
        """
        if 30 <= candle_age_seconds <= 45:
            return "OPTIMAL"  # Sweet spot
        elif 20 <= candle_age_seconds <= 50:
            return "ACCEPTABLE"
        else:
            return "LATE"

    def _determine_validation_level(
        self,
        symbol: str,
        reversal_score: float,
        reversal_conviction: str,
        micro_resistance: Optional[Dict],
        timing_quality: str,
        candle_age_seconds: float
    ) -> tuple:
        """
        Détermine le niveau de validation et le boost à appliquer

        Args:
            symbol: Asset (NAS100, USDJPY, GBPUSD)
            reversal_score: Score reversal detector (0-100)
            reversal_conviction: Conviction level (HIGH/MODERATE/CAUTION/LOW)
            micro_resistance: Données micro-résistance M1 (ou None)
            timing_quality: OPTIMAL / ACCEPTABLE / LATE
            candle_age_seconds: Âge de la bougie

        Returns:
            tuple: (validation_level, confidence, score_boost, reasons)
        """
        reasons = []

        # Config asset-specific
        asset_config = self.config['assets'].get(symbol, {})
        pip_multiplier = asset_config.get('pip_multiplier', 1.0)

        # === STRONG VALIDATION (Fast-track) ===
        strong_cfg = self.config['strong']
        min_reversal_strong = asset_config.get('min_reversal_score_override', strong_cfg['min_reversal_score'])
        strong_boost = asset_config.get('strong_boost_override', strong_cfg['score_boost'])

        # Vérifier reversal score et conviction
        reversal_ok_strong = (
            reversal_score >= min_reversal_strong and
            reversal_conviction in ['HIGH', 'MODERATE']
        )

        if reversal_ok_strong and micro_resistance:
            distance_pips = micro_resistance.get('distance_pips', 999) * pip_multiplier
            bounce_prob = micro_resistance.get('bounce_probability', 0.0)
            age_minutes = micro_resistance.get('age_minutes', 999)

            if (strong_cfg['min_micro_resistance_distance_pips'] <= distance_pips <= strong_cfg['max_micro_resistance_distance_pips'] and
                bounce_prob >= strong_cfg['min_bounce_probability'] and
                age_minutes <= strong_cfg['max_resistance_age_minutes'] and
                timing_quality in ['OPTIMAL', 'ACCEPTABLE']):

                reasons.append(f"✅ Reversal score ÉLEVÉ: {reversal_score:.0f}/100 ({reversal_conviction})")
                reasons.append(f"✅ Micro-résistance PROCHE: {distance_pips:.1f} pips")
                reasons.append(f"✅ Bounce probability: {bounce_prob:.0%}")
                reasons.append(f"✅ Résistance FRAÎCHE: {age_minutes} minutes")
                reasons.append(f"✅ Timing: {timing_quality} ({candle_age_seconds:.0f}s)")

                return ('STRONG', 0.9, strong_boost, reasons)

        # === MODERATE VALIDATION (Standard) ===
        moderate_cfg = self.config['moderate']
        min_reversal_moderate = asset_config.get('min_reversal_score_override', moderate_cfg['min_reversal_score'])
        moderate_boost = asset_config.get('moderate_boost_override', moderate_cfg['score_boost'])

        reversal_ok_moderate = (
            reversal_score >= min_reversal_moderate and
            reversal_conviction in ['HIGH', 'MODERATE', 'CAUTION']
        )

        if reversal_ok_moderate and micro_resistance:
            distance_pips = micro_resistance.get('distance_pips', 999) * pip_multiplier
            bounce_prob = micro_resistance.get('bounce_probability', 0.0)
            age_minutes = micro_resistance.get('age_minutes', 999)

            if (moderate_cfg['min_micro_resistance_distance_pips'] <= distance_pips <= moderate_cfg['max_micro_resistance_distance_pips'] and
                bounce_prob >= moderate_cfg['min_bounce_probability'] and
                age_minutes <= moderate_cfg['max_resistance_age_minutes']):

                reasons.append(f"✓ Reversal score MODÉRÉ: {reversal_score:.0f}/100 ({reversal_conviction})")
                reasons.append(f"✓ Micro-résistance ACCEPTABLE: {distance_pips:.1f} pips")
                reasons.append(f"✓ Bounce probability: {bounce_prob:.0%}")
                reasons.append(f"✓ Timing: {timing_quality} ({candle_age_seconds:.0f}s)")

                return ('MODERATE', 0.7, moderate_boost, reasons)

        # === WEAK VALIDATION (Minimal) ===
        weak_cfg = self.config['weak']

        if reversal_score >= weak_cfg['min_reversal_score']:
            reasons.append(f"⚠ Reversal score FAIBLE: {reversal_score:.0f}/100 ({reversal_conviction})")

            if micro_resistance:
                distance_pips = micro_resistance.get('distance_pips', 999) * pip_multiplier
                reasons.append(f"⚠ Résistance éloignée: {distance_pips:.1f} pips")
            else:
                reasons.append(f"⚠ Aucune résistance proche détectée")

            reasons.append(f"⚠ Timing: {timing_quality}")

            return ('WEAK', 0.5, weak_cfg['score_boost'], reasons)

        # === REJECTED ===
        reasons.append(f"❌ Reversal score INSUFFISANT: {reversal_score:.0f}/100 (min {weak_cfg['min_reversal_score']})")

        if not micro_resistance:
            reasons.append(f"❌ Aucune micro-résistance M1 détectée")
        else:
            distance_pips = micro_resistance.get('distance_pips', 999) * pip_multiplier
            bounce_prob = micro_resistance.get('bounce_probability', 0.0)
            reasons.append(f"❌ Résistance inadéquate: {distance_pips:.1f}p (bounce {bounce_prob:.0%})")

        return ('REJECTED', 0.0, 0.0, reasons)

    def _rejected(self, reason: str, details: str) -> BearishValidationResult:
        """
        Retourne une validation rejetée

        Args:
            reason: Code de rejet
            details: Détails du rejet

        Returns:
            BearishValidationResult avec validation_level=REJECTED
        """
        return BearishValidationResult(
            validation_level='REJECTED',
            confidence=0.0,
            score_boost=0.0,
            reasons=[f"❌ {reason}: {details}"],
            reversal_score=0.0,
            reversal_conviction='N/A',
            micro_resistance=None,
            timing_quality='N/A',
            candle_age_seconds=0.0,
            should_take_trade=False
        )
