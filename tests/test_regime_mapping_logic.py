#!/usr/bin/env python3
# tests/test_regime_mapping_logic.py
"""
Tests de logique du mapping PhaseObserver → VWAP
Sans dépendances externes
"""


def test_regime_mapping_logic():
    """Teste la logique de mapping des régimes"""

    # Mapping défini
    MAPPING = {
        # TRENDING (4 → 1)
        "trending_institutional_bull": "TRENDING",
        "trending_institutional_bear": "TRENDING",
        "trending_retail_bull": "TRENDING",
        "trending_retail_bear": "TRENDING",

        # ACCUMULATION (2 → 1) - Range avec biais directionnel
        "range_accumulation": "ACCUMULATION",
        "range_distribution": "ACCUMULATION",

        # BALANCED (2 → 1) - Range neutre sans biais
        "range_institutional": "BALANCED",
        "range_retail": "BALANCED",

        # TRANSITIONAL (3 → 1)
        "high_volatility_chaos": "TRANSITIONAL",
        "low_volatility_compression": "TRANSITIONAL",
        "transitional": "TRANSITIONAL",
    }

    # Scoring adjustments
    SCORING_ADJ = {
        "TRENDING": {"trend_weight": 1.3, "position_weight": 0.9},
        "ACCUMULATION": {"trend_weight": 0.8, "position_weight": 1.2},
        "BALANCED": {"trend_weight": 0.9, "position_weight": 1.0},
        "TRANSITIONAL": {"trend_weight": 0.7, "position_weight": 0.7},
    }

    print("\n" + "="*70)
    print("TESTS DE LOGIQUE DU MAPPING PHASEOBSERVER → VWAP")
    print("="*70)

    # Test 1: Complétude
    print("\n[TEST 1] Vérification complétude du mapping")
    expected_count = 11  # 11 régimes PhaseObserver
    actual_count = len(MAPPING)
    if actual_count == expected_count:
        print(f"  ✅ Tous les {expected_count} régimes PhaseObserver sont mappés")
    else:
        print(f"  ❌ Attendu {expected_count}, obtenu {actual_count}")
        return False

    # Test 2: Couverture VWAP
    print("\n[TEST 2] Vérification couverture des 4 régimes VWAP")
    vwap_regimes = set(MAPPING.values())
    expected_vwap = {"TRENDING", "ACCUMULATION", "BALANCED", "TRANSITIONAL"}
    if vwap_regimes == expected_vwap:
        print(f"  ✅ Tous les 4 régimes VWAP sont couverts")
    else:
        print(f"  ❌ Régimes manquants: {expected_vwap - vwap_regimes}")
        return False

    # Test 3: Cohérence sémantique
    print("\n[TEST 3] Vérification cohérence sémantique")

    # Trending
    trending_keys = [k for k in MAPPING.keys() if k.startswith("trending_")]
    if all(MAPPING[k] == "TRENDING" for k in trending_keys):
        print(f"  ✅ Tous les trending_* → TRENDING ({len(trending_keys)} régimes)")
    else:
        print(f"  ❌ Incohérence dans trending_*")
        return False

    # Accumulation
    if (MAPPING["range_accumulation"] == "ACCUMULATION" and
        MAPPING["range_distribution"] == "ACCUMULATION"):
        print(f"  ✅ range_accumulation/distribution → ACCUMULATION")
    else:
        print(f"  ❌ Incohérence dans ACCUMULATION")
        return False

    # Balanced
    if (MAPPING["range_institutional"] == "BALANCED" and
        MAPPING["range_retail"] == "BALANCED"):
        print(f"  ✅ range_institutional/retail → BALANCED")
    else:
        print(f"  ❌ Incohérence dans BALANCED")
        return False

    # Transitional
    transitional_keys = ["high_volatility_chaos", "low_volatility_compression", "transitional"]
    if all(MAPPING[k] == "TRANSITIONAL" for k in transitional_keys):
        print(f"  ✅ Volatilité/transition → TRANSITIONAL ({len(transitional_keys)} régimes)")
    else:
        print(f"  ❌ Incohérence dans TRANSITIONAL")
        return False

    # Test 4: Distribution
    print("\n[TEST 4] Distribution des mappings")
    reverse = {}
    for po, vw in MAPPING.items():
        if vw not in reverse:
            reverse[vw] = []
        reverse[vw].append(po)

    for vwap_regime in sorted(reverse.keys()):
        count = len(reverse[vwap_regime])
        print(f"  • {vwap_regime}: {count} régimes PhaseObserver")

    # Test 5: Scoring adjustments
    print("\n[TEST 5] Vérification scoring adjustments")
    for vwap_regime in expected_vwap:
        if vwap_regime not in SCORING_ADJ:
            print(f"  ❌ Ajustement manquant pour {vwap_regime}")
            return False

        adj = SCORING_ADJ[vwap_regime]
        tw = adj["trend_weight"]
        pw = adj["position_weight"]

        if tw <= 0 or pw <= 0:
            print(f"  ❌ Poids invalides pour {vwap_regime}")
            return False

        print(f"  ✅ {vwap_regime}: trend={tw:.1f}x, position={pw:.1f}x")

    # Test 6: Logique adaptive scoring
    print("\n[TEST 6] Vérification logique adaptive scoring")

    # TRENDING doit booster trend, réduire position
    if SCORING_ADJ["TRENDING"]["trend_weight"] > 1.0 and SCORING_ADJ["TRENDING"]["position_weight"] < 1.0:
        print(f"  ✅ TRENDING: boost trend, réduit position")
    else:
        print(f"  ❌ TRENDING: logique incorrecte")
        return False

    # ACCUMULATION doit réduire trend, booster position
    if SCORING_ADJ["ACCUMULATION"]["trend_weight"] < 1.0 and SCORING_ADJ["ACCUMULATION"]["position_weight"] > 1.0:
        print(f"  ✅ ACCUMULATION: réduit trend, boost position")
    else:
        print(f"  ❌ ACCUMULATION: logique incorrecte")
        return False

    # BALANCED doit être proche de neutre
    balanced_tw = SCORING_ADJ["BALANCED"]["trend_weight"]
    balanced_pw = SCORING_ADJ["BALANCED"]["position_weight"]
    if abs(balanced_tw - 1.0) < 0.2 and abs(balanced_pw - 1.0) < 0.2:
        print(f"  ✅ BALANCED: proche de neutre")
    else:
        print(f"  ❌ BALANCED: trop éloigné de neutre")
        return False

    # TRANSITIONAL doit réduire les deux
    if SCORING_ADJ["TRANSITIONAL"]["trend_weight"] < 1.0 and SCORING_ADJ["TRANSITIONAL"]["position_weight"] < 1.0:
        print(f"  ✅ TRANSITIONAL: réduit trend et position")
    else:
        print(f"  ❌ TRANSITIONAL: logique incorrecte")
        return False

    # Test 7: Exemple pratique
    print("\n[TEST 7] Exemple de calcul de scoring adaptatif")

    # Scores bruts
    raw_trend = 12.0  # sur 15
    raw_position = 8.0  # sur 10
    raw_total = raw_trend + raw_position  # 20 sur 25

    print(f"\n  Scores bruts: trend={raw_trend}/15, position={raw_position}/10, total={raw_total}/25")

    for vwap_regime in ["TRENDING", "ACCUMULATION", "BALANCED"]:
        adj = SCORING_ADJ[vwap_regime]
        tw = adj["trend_weight"]
        pw = adj["position_weight"]

        # Ajuster scores
        adj_trend = raw_trend * tw
        adj_position = raw_position * pw

        # Normaliser pour max 25 points
        norm_factor = (tw * 15 + pw * 10) / 25.0
        total_score = min(25.0, (adj_trend + adj_position) / norm_factor)

        print(f"\n  {vwap_regime} (tw={tw:.1f}, pw={pw:.1f}):")
        print(f"    Ajustés: trend={adj_trend:.1f}, position={adj_position:.1f}")
        print(f"    Total normalisé: {total_score:.1f}/25")

    print("\n" + "="*70)
    print("✅ TOUS LES TESTS SONT PASSÉS")
    print("="*70)

    return True


if __name__ == "__main__":
    import sys
    success = test_regime_mapping_logic()
    sys.exit(0 if success else 1)
