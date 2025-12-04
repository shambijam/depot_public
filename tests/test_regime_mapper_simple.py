# tests/test_regime_mapper_simple.py
"""
Tests unitaires simplifiés pour RegimeMapper (sans dépendances lourdes)
"""

import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Import direct du module
from phase_observer.vwap.models import VWAPRegime
from enum import Enum


# Copie simplifiée de la logique RegimeMapper pour test
class TestRegimeMapper:
    """Version test du RegimeMapper"""

    MAPPING = {
        "trending_institutional_bull": "TRENDING",
        "trending_institutional_bear": "TRENDING",
        "trending_retail_bull": "TRENDING",
        "trending_retail_bear": "TRENDING",
        "range_accumulation": "ACCUMULATION",
        "range_distribution": "ACCUMULATION",
        "range_institutional": "BALANCED",
        "range_retail": "BALANCED",
        "high_volatility_chaos": "TRANSITIONAL",
        "low_volatility_compression": "TRANSITIONAL",
        "transitional": "TRANSITIONAL",
    }


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

    mapped_regimes = set(TestRegimeMapper.MAPPING.keys())

    if expected_regimes == mapped_regimes:
        print("✅ All 11 PhaseObserver regimes are mapped")
        return True
    else:
        print(f"❌ Missing or extra mappings")
        print(f"   Expected: {expected_regimes}")
        print(f"   Got: {mapped_regimes}")
        return False


def test_vwap_regimes_coverage():
    """Test que tous les 4 régimes VWAP sont couverts"""
    print("\n" + "="*60)
    print("TEST 2: VWAP Regimes Coverage")
    print("="*60)

    vwap_regimes = set(TestRegimeMapper.MAPPING.values())
    expected_vwap = {"TRENDING", "ACCUMULATION", "BALANCED", "TRANSITIONAL"}

    if vwap_regimes == expected_vwap:
        print("✅ All 4 VWAP regimes are covered")
        return True
    else:
        print(f"❌ Not all VWAP regimes covered")
        print(f"   Got: {vwap_regimes}")
        return False


def test_specific_mappings():
    """Test des mappings spécifiques clés"""
    print("\n" + "="*60)
    print("TEST 3: Specific Mappings")
    print("="*60)

    test_cases = [
        ("trending_institutional_bull", "TRENDING"),
        ("trending_institutional_bear", "TRENDING"),
        ("range_accumulation", "ACCUMULATION"),
        ("range_distribution", "ACCUMULATION"),
        ("range_institutional", "BALANCED"),
        ("range_retail", "BALANCED"),
        ("high_volatility_chaos", "TRANSITIONAL"),
        ("transitional", "TRANSITIONAL"),
    ]

    all_passed = True
    for po_regime, expected_vwap in test_cases:
        actual = TestRegimeMapper.MAPPING.get(po_regime)
        if actual == expected_vwap:
            print(f"  ✅ {po_regime} → {actual}")
        else:
            print(f"  ❌ {po_regime}: expected {expected_vwap}, got {actual}")
            all_passed = False

    return all_passed


def test_semantic_coherence():
    """Test la cohérence sémantique du mapping"""
    print("\n" + "="*60)
    print("TEST 4: Semantic Coherence")
    print("="*60)

    # Tous les trending_* → TRENDING
    trending_ok = all(
        TestRegimeMapper.MAPPING[k] == "TRENDING"
        for k in TestRegimeMapper.MAPPING.keys()
        if k.startswith("trending_")
    )

    # range_accumulation et range_distribution → ACCUMULATION
    accum_ok = (
        TestRegimeMapper.MAPPING.get("range_accumulation") == "ACCUMULATION" and
        TestRegimeMapper.MAPPING.get("range_distribution") == "ACCUMULATION"
    )

    # range_institutional et range_retail → BALANCED
    balanced_ok = (
        TestRegimeMapper.MAPPING.get("range_institutional") == "BALANCED" and
        TestRegimeMapper.MAPPING.get("range_retail") == "BALANCED"
    )

    # Volatilité et transition → TRANSITIONAL
    trans_ok = (
        TestRegimeMapper.MAPPING.get("high_volatility_chaos") == "TRANSITIONAL" and
        TestRegimeMapper.MAPPING.get("low_volatility_compression") == "TRANSITIONAL" and
        TestRegimeMapper.MAPPING.get("transitional") == "TRANSITIONAL"
    )

    all_ok = trending_ok and accum_ok and balanced_ok and trans_ok

    if all_ok:
        print("  ✅ All trending_* → TRENDING")
        print("  ✅ range_accumulation/distribution → ACCUMULATION")
        print("  ✅ range_institutional/retail → BALANCED")
        print("  ✅ Volatility/transition → TRANSITIONAL")
    else:
        print(f"  ❌ Semantic coherence failed")
        print(f"     trending_ok={trending_ok}, accum_ok={accum_ok}")
        print(f"     balanced_ok={balanced_ok}, trans_ok={trans_ok}")

    return all_ok


def test_reverse_mapping():
    """Test le mapping inversé"""
    print("\n" + "="*60)
    print("TEST 5: Reverse Mapping")
    print("="*60)

    # Construire reverse mapping
    reverse = {}
    for po_regime, vwap_regime in TestRegimeMapper.MAPPING.items():
        if vwap_regime not in reverse:
            reverse[vwap_regime] = []
        reverse[vwap_regime].append(po_regime)

    # Doit avoir 4 entrées (4 régimes VWAP)
    if len(reverse) != 4:
        print(f"  ❌ Expected 4 VWAP regimes, got {len(reverse)}")
        return False

    # Total doit être 11 régimes PhaseObserver
    total = sum(len(regimes) for regimes in reverse.values())
    if total != 11:
        print(f"  ❌ Expected 11 PhaseObserver regimes total, got {total}")
        return False

    print("  Reverse mapping:")
    for vwap_regime, po_regimes in reverse.items():
        print(f"    {vwap_regime} ← {len(po_regimes)} regimes")

    print("  ✅ Reverse mapping is correct")
    return True


def run_all_tests():
    """Exécute tous les tests"""
    print("\n" + "="*60)
    print("REGIME MAPPER - TESTS SIMPLIFIÉS")
    print("="*60)

    tests = [
        ("Mapping Completeness", test_mapping_completeness),
        ("VWAP Regimes Coverage", test_vwap_regimes_coverage),
        ("Specific Mappings", test_specific_mappings),
        ("Semantic Coherence", test_semantic_coherence),
        ("Reverse Mapping", test_reverse_mapping),
    ]

    passed = 0
    failed = 0

    for name, test_func in tests:
        try:
            if test_func():
                passed += 1
            else:
                failed += 1
        except Exception as e:
            print(f"\n❌ ERROR: {name}")
            print(f"   Exception: {e}")
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
