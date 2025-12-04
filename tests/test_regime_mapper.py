# tests/test_regime_mapper.py
"""
Tests unitaires pour RegimeMapper
Valide le mapping PhaseObserver → VWAP
"""

import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from phase_observer.vwap.regime_mapper import RegimeMapper, validate_regime_mapper
from phase_observer.vwap.models import VWAPRegime


def test_mapping_completeness():
    """Test que tous les régimes PhaseObserver sont mappés"""
    print("\n" + "="*60)
    print("TEST 1: Mapping Completeness")
    print("="*60)

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

    mapped_regimes = set(RegimeMapper.MAPPING.keys())

    assert expected_regimes == mapped_regimes, f"Missing or extra mappings. Expected: {expected_regimes}, Got: {mapped_regimes}"
    print("✅ All 11 PhaseObserver regimes are mapped")


def test_vwap_regimes_coverage():
    """Test que tous les 4 régimes VWAP sont couverts"""
    print("\n" + "="*60)
    print("TEST 2: VWAP Regimes Coverage")
    print("="*60)

    vwap_regimes = set(RegimeMapper.MAPPING.values())
    expected_vwap = {
        VWAPRegime.TRENDING,
        VWAPRegime.ACCUMULATION,
        VWAPRegime.BALANCED,
        VWAPRegime.TRANSITIONAL,
    }

    assert vwap_regimes == expected_vwap, f"Not all VWAP regimes covered. Got: {vwap_regimes}"
    print("✅ All 4 VWAP regimes are covered")


def test_specific_mappings():
    """Test des mappings spécifiques clés"""
    print("\n" + "="*60)
    print("TEST 3: Specific Mappings")
    print("="*60)

    test_cases = [
        ("trending_institutional_bull", VWAPRegime.TRENDING),
        ("trending_institutional_bear", VWAPRegime.TRENDING),
        ("range_accumulation", VWAPRegime.ACCUMULATION),
        ("range_distribution", VWAPRegime.ACCUMULATION),
        ("range_institutional", VWAPRegime.BALANCED),
        ("range_retail", VWAPRegime.BALANCED),
        ("high_volatility_chaos", VWAPRegime.TRANSITIONAL),
        ("transitional", VWAPRegime.TRANSITIONAL),
    ]

    for po_regime, expected_vwap in test_cases:
        vwap_regime, confidence = RegimeMapper.map_regime(po_regime)
        assert vwap_regime == expected_vwap, f"Mapping failed for {po_regime}: expected {expected_vwap}, got {vwap_regime}"
        assert 0.0 <= confidence <= 1.0, f"Invalid confidence {confidence} for {po_regime}"
        print(f"  ✅ {po_regime} → {vwap_regime.value} (confidence: {confidence:.2f})")


def test_confidence_modifiers():
    """Test que les confidence modifiers sont cohérents"""
    print("\n" + "="*60)
    print("TEST 4: Confidence Modifiers")
    print("="*60)

    # Trending devrait avoir la plus haute confidence
    trending_conf = RegimeMapper.map_regime("trending_institutional_bull")[1]
    range_conf = RegimeMapper.map_regime("range_accumulation")[1]
    chaos_conf = RegimeMapper.map_regime("high_volatility_chaos")[1]

    assert trending_conf >= range_conf, "Trending should have higher confidence than range"
    assert range_conf >= chaos_conf, "Range should have higher confidence than chaos"

    print(f"  ✅ trending_conf={trending_conf:.2f} >= range_conf={range_conf:.2f} >= chaos_conf={chaos_conf:.2f}")


def test_scoring_adjustments():
    """Test que tous les régimes VWAP ont des ajustements de scoring"""
    print("\n" + "="*60)
    print("TEST 5: Scoring Adjustments")
    print("="*60)

    for vwap_regime in VWAPRegime:
        adj = RegimeMapper.get_scoring_adjustment(vwap_regime)
        assert "trend_weight" in adj, f"Missing trend_weight for {vwap_regime}"
        assert "position_weight" in adj, f"Missing position_weight for {vwap_regime}"
        assert adj["trend_weight"] > 0, f"Invalid trend_weight for {vwap_regime}"
        assert adj["position_weight"] > 0, f"Invalid position_weight for {vwap_regime}"
        print(f"  ✅ {vwap_regime.value}: trend={adj['trend_weight']:.2f}, position={adj['position_weight']:.2f}")


def test_semantic_descriptions():
    """Test que toutes les descriptions sémantiques existent"""
    print("\n" + "="*60)
    print("TEST 6: Semantic Descriptions")
    print("="*60)

    for po_regime in RegimeMapper.MAPPING.keys():
        desc = RegimeMapper.get_regime_description(po_regime)
        assert "description" in desc, f"Missing description for {po_regime}"
        assert "characteristics" in desc, f"Missing characteristics for {po_regime}"
        assert "vwap_behavior" in desc, f"Missing vwap_behavior for {po_regime}"
        assert "confidence_modifier" in desc, f"Missing confidence_modifier for {po_regime}"

    print(f"  ✅ All {len(RegimeMapper.MAPPING)} regimes have semantic descriptions")


def test_reverse_mapping():
    """Test le mapping inversé"""
    print("\n" + "="*60)
    print("TEST 7: Reverse Mapping")
    print("="*60)

    reverse = RegimeMapper.get_reverse_mapping()

    # Doit avoir 4 entrées (4 régimes VWAP)
    assert len(reverse) == 4, f"Expected 4 VWAP regimes, got {len(reverse)}"

    # Total doit être 11 régimes PhaseObserver
    total = sum(len(regimes) for regimes in reverse.values())
    assert total == 11, f"Expected 11 PhaseObserver regimes total, got {total}"

    print("  Reverse mapping:")
    for vwap_regime, po_regimes in reverse.items():
        print(f"    {vwap_regime} ← {len(po_regimes)} regimes: {', '.join(po_regimes[:2])}...")

    print("  ✅ Reverse mapping is correct")


def test_adaptive_scoring_logic():
    """Test la logique de scoring adaptatif"""
    print("\n" + "="*60)
    print("TEST 8: Adaptive Scoring Logic")
    print("="*60)

    # TRENDING devrait booster trend, réduire position
    trending_adj = RegimeMapper.get_scoring_adjustment(VWAPRegime.TRENDING)
    assert trending_adj["trend_weight"] > 1.0, "TRENDING should boost trend score"
    assert trending_adj["position_weight"] < 1.0, "TRENDING should reduce position score"

    # ACCUMULATION devrait booster position, réduire trend
    accum_adj = RegimeMapper.get_scoring_adjustment(VWAPRegime.ACCUMULATION)
    assert accum_adj["trend_weight"] < 1.0, "ACCUMULATION should reduce trend score"
    assert accum_adj["position_weight"] > 1.0, "ACCUMULATION should boost position score"

    # BALANCED devrait être neutre
    balanced_adj = RegimeMapper.get_scoring_adjustment(VWAPRegime.BALANCED)
    assert abs(balanced_adj["trend_weight"] - 1.0) < 0.2, "BALANCED should be close to neutral"
    assert abs(balanced_adj["position_weight"] - 1.0) < 0.2, "BALANCED should be close to neutral"

    print(f"  ✅ TRENDING: trend={trending_adj['trend_weight']:.2f}, position={trending_adj['position_weight']:.2f}")
    print(f"  ✅ ACCUMULATION: trend={accum_adj['trend_weight']:.2f}, position={accum_adj['position_weight']:.2f}")
    print(f"  ✅ BALANCED: trend={balanced_adj['trend_weight']:.2f}, position={balanced_adj['position_weight']:.2f}")


def run_all_tests():
    """Exécute tous les tests"""
    print("\n" + "="*60)
    print("REGIME MAPPER - UNIT TESTS")
    print("="*60)

    tests = [
        ("Mapping Completeness", test_mapping_completeness),
        ("VWAP Regimes Coverage", test_vwap_regimes_coverage),
        ("Specific Mappings", test_specific_mappings),
        ("Confidence Modifiers", test_confidence_modifiers),
        ("Scoring Adjustments", test_scoring_adjustments),
        ("Semantic Descriptions", test_semantic_descriptions),
        ("Reverse Mapping", test_reverse_mapping),
        ("Adaptive Scoring Logic", test_adaptive_scoring_logic),
    ]

    passed = 0
    failed = 0

    for name, test_func in tests:
        try:
            test_func()
            passed += 1
        except AssertionError as e:
            print(f"\n❌ FAILED: {name}")
            print(f"   Error: {e}")
            failed += 1
        except Exception as e:
            print(f"\n❌ ERROR: {name}")
            print(f"   Exception: {e}")
            failed += 1

    # Run validation finale
    print("\n" + "="*60)
    print("VALIDATION COMPLETE")
    print("="*60)

    try:
        validation_ok = validate_regime_mapper()
        if validation_ok:
            passed += 1
        else:
            failed += 1
    except Exception as e:
        print(f"❌ Validation failed: {e}")
        failed += 1

    # Résumé
    print("\n" + "="*60)
    print("TEST RESULTS")
    print("="*60)
    print(f"Passed: {passed}/{passed+failed}")
    print(f"Failed: {failed}/{passed+failed}")

    if failed == 0:
        print("\n✅ ALL TESTS PASSED")
        return True
    else:
        print(f"\n❌ {failed} TEST(S) FAILED")
        return False


if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)
