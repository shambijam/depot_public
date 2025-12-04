# phase_observer/vwap/regime_mapper.py
"""
RegimeMapper - Mapping PhaseObserver regimes → VWAP regimes
Évite la duplication de détection de régime (déjà fait par PhaseObserver)
Fournit mapping sémantique validé
"""

import logging
from typing import Dict, Tuple, Any, Optional
from enum import Enum

from .models import VWAPRegime


logger = logging.getLogger(__name__)


# ==================== SEMANTIC MAPPING ====================

class RegimeMapper:
    """
    Mapper les 10 régimes PhaseObserver → 4 régimes VWAP

    Mapping sémantique validé:
    - TRENDING (4→1): trending_institutional_bull/bear, trending_retail_bull/bear
    - ACCUMULATION (2→1): range_accumulation, range_distribution (range avec biais directionnel)
    - BALANCED (2→1): range_institutional, range_retail (range neutre sans biais)
    - TRANSITIONAL (3→1): high_volatility_chaos, low_volatility_compression, transitional
    """

    # Mapping principal PhaseObserver → VWAP
    MAPPING = {
        # TRENDING: Tendance claire, momentum fort
        "trending_institutional_bull": VWAPRegime.TRENDING,
        "trending_institutional_bear": VWAPRegime.TRENDING,
        "trending_retail_bull": VWAPRegime.TRENDING,
        "trending_retail_bear": VWAPRegime.TRENDING,

        # ACCUMULATION: Range avec biais directionnel (positioning institutionnel)
        "range_accumulation": VWAPRegime.ACCUMULATION,     # Range bullish (prix monte)
        "range_distribution": VWAPRegime.ACCUMULATION,     # Range bearish (prix descend)

        # BALANCED: Range neutre sans biais clair
        "range_institutional": VWAPRegime.BALANCED,        # Range neutre institutionnel
        "range_retail": VWAPRegime.BALANCED,               # Range neutre retail

        # TRANSITIONAL: Changement de phase, volatilité extrême
        "high_volatility_chaos": VWAPRegime.TRANSITIONAL,
        "low_volatility_compression": VWAPRegime.TRANSITIONAL,
        "transitional": VWAPRegime.TRANSITIONAL,
    }

    # Descriptions sémantiques de chaque régime PhaseObserver
    SEMANTIC_DESCRIPTIONS = {
        "trending_institutional_bull": {
            "description": "Tendance haussière institutionnelle forte",
            "characteristics": "ADX élevé, DI+ > DI-, volume institutionnel",
            "vwap_behavior": "Prix au-dessus VWAP, slope positive forte",
            "confidence_modifier": 1.0,  # Confiance max en trending
        },
        "trending_institutional_bear": {
            "description": "Tendance baissière institutionnelle forte",
            "characteristics": "ADX élevé, DI- > DI+, volume institutionnel",
            "vwap_behavior": "Prix en-dessous VWAP, slope négative forte",
            "confidence_modifier": 1.0,
        },
        "trending_retail_bull": {
            "description": "Tendance haussière retail",
            "characteristics": "ADX élevé, DI+ > DI-, volume retail",
            "vwap_behavior": "Prix au-dessus VWAP, slope positive",
            "confidence_modifier": 0.9,  # Légèrement moins de confiance que institutional
        },
        "trending_retail_bear": {
            "description": "Tendance baissière retail",
            "characteristics": "ADX élevé, DI- > DI+, volume retail",
            "vwap_behavior": "Prix en-dessous VWAP, slope négative",
            "confidence_modifier": 0.9,
        },
        "range_accumulation": {
            "description": "Range avec accumulation (biais haussier)",
            "characteristics": "ADX faible, prix > moyenne dans range, institutional",
            "vwap_behavior": "Prix proche VWAP avec tests vers le haut",
            "confidence_modifier": 0.7,  # Range moins prévisible
        },
        "range_distribution": {
            "description": "Range avec distribution (biais baissier)",
            "characteristics": "ADX faible, prix < moyenne dans range, institutional",
            "vwap_behavior": "Prix proche VWAP avec tests vers le bas",
            "confidence_modifier": 0.7,
        },
        "range_institutional": {
            "description": "Range neutre institutionnel",
            "characteristics": "ADX faible, équilibre buy/sell, institutional",
            "vwap_behavior": "Prix oscille autour VWAP, slope faible",
            "confidence_modifier": 0.6,  # Neutre = moins de directionnalité
        },
        "range_retail": {
            "description": "Range neutre retail",
            "characteristics": "ADX faible, équilibre buy/sell, retail",
            "vwap_behavior": "Prix oscille autour VWAP, slope faible",
            "confidence_modifier": 0.5,  # Retail + neutre = moins de confiance
        },
        "high_volatility_chaos": {
            "description": "Volatilité extrême, chaos de marché",
            "characteristics": "Volatilité > 90e percentile, mouvements erratiques",
            "vwap_behavior": "Prix éloigné VWAP, slope instable",
            "confidence_modifier": 0.3,  # Faible confiance en chaos
        },
        "low_volatility_compression": {
            "description": "Compression de volatilité, breakout imminent",
            "characteristics": "Volatilité < 10e percentile, range tight",
            "vwap_behavior": "Prix proche VWAP, slope proche de zéro",
            "confidence_modifier": 0.4,  # Attente de breakout
        },
        "transitional": {
            "description": "Transition entre phases",
            "characteristics": "ADX moyen, signaux contradictoires",
            "vwap_behavior": "Comportement VWAP imprévisible",
            "confidence_modifier": 0.5,  # Incertitude transitoire
        },
    }

    # Ajustements de scoring par régime VWAP (pour Phase 2)
    SCORING_ADJUSTMENTS = {
        VWAPRegime.TRENDING: {
            "trend_weight": 1.3,      # Boost trend score en TRENDING
            "position_weight": 0.9,   # Position moins importante
            "description": "Favorise signaux de tendance forte",
        },
        VWAPRegime.ACCUMULATION: {
            "trend_weight": 0.8,      # Réduit trend score
            "position_weight": 1.2,   # Boost position score (range)
            "description": "Favorise signaux de position dans range",
        },
        VWAPRegime.BALANCED: {
            "trend_weight": 0.9,      # Neutre
            "position_weight": 1.0,   # Neutre
            "description": "Scoring équilibré",
        },
        VWAPRegime.TRANSITIONAL: {
            "trend_weight": 0.7,      # Réduit tous les scores
            "position_weight": 0.7,   # Prudence en transition
            "description": "Prudence, signaux moins fiables",
        },
    }

    @classmethod
    def map_regime(
        cls,
        phase_observer_regime: str,
        regime_strength: Optional[float] = None
    ) -> Tuple[VWAPRegime, float]:
        """
        Mappe un régime PhaseObserver vers un régime VWAP

        Args:
            phase_observer_regime: Régime détecté par PhaseObserver
            regime_strength: Force du régime (0-1), optionnel

        Returns:
            Tuple (VWAPRegime, confidence_modifier)
        """
        # Mapping
        vwap_regime = cls.MAPPING.get(
            phase_observer_regime,
            VWAPRegime.BALANCED  # Fallback par défaut
        )

        # Confidence modifier basé sur sémantique
        semantic = cls.SEMANTIC_DESCRIPTIONS.get(phase_observer_regime, {})
        confidence_modifier = semantic.get("confidence_modifier", 0.5)

        # Ajustement si regime_strength fournie
        if regime_strength is not None:
            # Combine semantic confidence avec regime_strength
            confidence_modifier = (confidence_modifier + regime_strength) / 2.0

        logger.debug(
            f"[REGIME_MAPPER] Map | "
            f"PhaseObserver={phase_observer_regime} → "
            f"VWAP={vwap_regime.value} | "
            f"Confidence={confidence_modifier:.2f}"
        )

        return vwap_regime, confidence_modifier

    @classmethod
    def get_regime_description(
        cls,
        phase_observer_regime: str
    ) -> Dict[str, Any]:
        """
        Retourne description sémantique complète d'un régime

        Args:
            phase_observer_regime: Régime PhaseObserver

        Returns:
            Dict avec description, caractéristiques, comportement VWAP
        """
        semantic = cls.SEMANTIC_DESCRIPTIONS.get(
            phase_observer_regime,
            {
                "description": "Régime inconnu",
                "characteristics": "N/A",
                "vwap_behavior": "N/A",
                "confidence_modifier": 0.5,
            }
        )

        vwap_regime = cls.MAPPING.get(
            phase_observer_regime,
            VWAPRegime.BALANCED
        )

        return {
            "phase_observer_regime": phase_observer_regime,
            "vwap_regime": vwap_regime.value,
            **semantic,
        }

    @classmethod
    def get_scoring_adjustment(
        cls,
        vwap_regime: VWAPRegime
    ) -> Dict[str, float]:
        """
        Retourne ajustements de scoring pour un régime VWAP

        Args:
            vwap_regime: Régime VWAP

        Returns:
            Dict avec trend_weight, position_weight
        """
        return cls.SCORING_ADJUSTMENTS.get(
            vwap_regime,
            {"trend_weight": 1.0, "position_weight": 1.0, "description": "Default"}
        )

    @classmethod
    def validate_mapping(cls) -> bool:
        """
        Valide que tous les régimes PhaseObserver sont mappés

        Returns:
            True si mapping complet
        """
        expected_regimes = {
            "trending_institutional_bull",
            "trending_institutional_bear",
            "trending_retail_bull",
            "trending_retail_bear",
            "range_accumulation",
            "range_distribution",
            "range_institutional",
            "range_retail",
            "high_volatility_chaos",
            "low_volatility_compression",
            "transitional",
        }

        mapped_regimes = set(cls.MAPPING.keys())

        if expected_regimes != mapped_regimes:
            missing = expected_regimes - mapped_regimes
            extra = mapped_regimes - expected_regimes

            if missing:
                logger.error(f"[REGIME_MAPPER] Missing mappings: {missing}")
            if extra:
                logger.warning(f"[REGIME_MAPPER] Extra mappings: {extra}")

            return False

        # Vérifier que toutes les descriptions sont présentes
        if set(cls.SEMANTIC_DESCRIPTIONS.keys()) != expected_regimes:
            logger.error("[REGIME_MAPPER] Semantic descriptions incomplete")
            return False

        logger.info("[REGIME_MAPPER] Mapping validation: OK")
        return True

    @classmethod
    def get_all_mappings(cls) -> Dict[str, str]:
        """
        Retourne tous les mappings PhaseObserver → VWAP

        Returns:
            Dict {phase_observer_regime: vwap_regime_value}
        """
        return {
            po_regime: vwap_regime.value
            for po_regime, vwap_regime in cls.MAPPING.items()
        }

    @classmethod
    def get_reverse_mapping(cls) -> Dict[str, list]:
        """
        Retourne mapping inversé VWAP → PhaseObserver regimes

        Returns:
            Dict {vwap_regime: [list of phase_observer_regimes]}
        """
        reverse = {}
        for po_regime, vwap_regime in cls.MAPPING.items():
            vwap_key = vwap_regime.value
            if vwap_key not in reverse:
                reverse[vwap_key] = []
            reverse[vwap_key].append(po_regime)
        return reverse


# ==================== VALIDATION & TESTS ====================

def validate_regime_mapper() -> bool:
    """
    Valide le RegimeMapper complet

    Returns:
        True si tous les tests passent
    """
    logger.info("[REGIME_MAPPER] Starting validation...")

    # Test 1: Mapping complet
    if not RegimeMapper.validate_mapping():
        logger.error("[REGIME_MAPPER] Test 1 FAILED: Mapping incomplete")
        return False
    logger.info("[REGIME_MAPPER] Test 1 PASSED: Mapping complete")

    # Test 2: Tous les régimes VWAP sont couverts
    vwap_regimes_covered = set(RegimeMapper.MAPPING.values())
    expected_vwap_regimes = {
        VWAPRegime.TRENDING,
        VWAPRegime.ACCUMULATION,
        VWAPRegime.BALANCED,
        VWAPRegime.TRANSITIONAL,
    }

    if vwap_regimes_covered != expected_vwap_regimes:
        logger.error(
            f"[REGIME_MAPPER] Test 2 FAILED: VWAP regimes not fully covered. "
            f"Expected={expected_vwap_regimes}, Got={vwap_regimes_covered}"
        )
        return False
    logger.info("[REGIME_MAPPER] Test 2 PASSED: All VWAP regimes covered")

    # Test 3: Mapping fonctionnel
    test_cases = [
        ("trending_institutional_bull", VWAPRegime.TRENDING),
        ("range_accumulation", VWAPRegime.ACCUMULATION),
        ("range_institutional", VWAPRegime.BALANCED),
        ("high_volatility_chaos", VWAPRegime.TRANSITIONAL),
    ]

    for po_regime, expected_vwap in test_cases:
        vwap_regime, confidence = RegimeMapper.map_regime(po_regime)
        if vwap_regime != expected_vwap:
            logger.error(
                f"[REGIME_MAPPER] Test 3 FAILED: "
                f"{po_regime} → {vwap_regime} (expected {expected_vwap})"
            )
            return False
        if not (0.0 <= confidence <= 1.0):
            logger.error(
                f"[REGIME_MAPPER] Test 3 FAILED: "
                f"Invalid confidence {confidence} for {po_regime}"
            )
            return False

    logger.info("[REGIME_MAPPER] Test 3 PASSED: Mapping functional")

    # Test 4: Scoring adjustments existent
    for vwap_regime in VWAPRegime:
        adj = RegimeMapper.get_scoring_adjustment(vwap_regime)
        if "trend_weight" not in adj or "position_weight" not in adj:
            logger.error(
                f"[REGIME_MAPPER] Test 4 FAILED: "
                f"Missing scoring adjustment for {vwap_regime}"
            )
            return False

    logger.info("[REGIME_MAPPER] Test 4 PASSED: Scoring adjustments complete")

    # Test 5: Reverse mapping
    reverse = RegimeMapper.get_reverse_mapping()
    if len(reverse) != 4:  # 4 VWAP regimes
        logger.error(
            f"[REGIME_MAPPER] Test 5 FAILED: "
            f"Reverse mapping should have 4 entries, got {len(reverse)}"
        )
        return False

    total_po_regimes = sum(len(regimes) for regimes in reverse.values())
    if total_po_regimes != 11:  # 11 PhaseObserver regimes
        logger.error(
            f"[REGIME_MAPPER] Test 5 FAILED: "
            f"Should map 11 PO regimes, got {total_po_regimes}"
        )
        return False

    logger.info("[REGIME_MAPPER] Test 5 PASSED: Reverse mapping correct")

    logger.info("[REGIME_MAPPER] ✅ All validation tests PASSED")
    return True


# ==================== MAIN ====================

if __name__ == "__main__":
    # Configuration logging pour tests
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    # Run validation
    success = validate_regime_mapper()

    if success:
        print("\n" + "="*60)
        print("REGIME MAPPER VALIDATION: SUCCESS")
        print("="*60)

        # Print mapping summary
        print("\n📊 Mapping Summary:")
        print("-" * 60)
        reverse = RegimeMapper.get_reverse_mapping()
        for vwap_regime, po_regimes in reverse.items():
            print(f"\n{vwap_regime} ({len(po_regimes)} sources):")
            for po in po_regimes:
                desc = RegimeMapper.get_regime_description(po)
                print(f"  • {po}")
                print(f"    → {desc['description']}")

        print("\n" + "="*60)
    else:
        print("\n" + "="*60)
        print("REGIME MAPPER VALIDATION: FAILED")
        print("="*60)
        exit(1)
