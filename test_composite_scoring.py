#!/usr/bin/env python3
"""
Test du nouveau système de scoring composite
"""

import sys
sys.path.insert(0, '.')

from phase_observer.fusion_manager import FusionManager

def test_cas_1_forte_pression_sans_trigger():
    """
    CAS 1 : Forte pression BUY sans trigger
    Attendu : Score élevé malgré absence de trigger
    """
    print("\n" + "="*70)
    print("CAS 1 : FORTE PRESSION BUY SANS TRIGGER")
    print("="*70)

    fm = FusionManager()

    # Données simulées
    n_of = {
        "score": 0.85,
        "status": "VALID",
        "dir": 1,
        "delta_total": 1800.0,
        "raw": {
            "summary": {
                "delta_total": 1800.0,
                "volume_total": 25000.0,
                "buy_ratio": 0.68,
                "mean_imbalance": 0.68,
                "cvd_slope": 1.6,
            }
        }
    }

    n_fp = {
        "score": 0.80,
        "status": "VALID",
        "dir": 1,
        "delta_total": 1200.0,
        "raw": {
            "summary": {
                "buy_volume": 8300.0,
                "sell_volume": 4100.0,
                "total_volume": 12400.0,
                "tick_count": 180,
                "coverage_s": 25.0,
                "tick_rate": 72.0,
            }
        }
    }

    n_tr = {
        "score": 0.0,  # Pas de trigger
        "dir": 0,
        "type": "fusion_pretrigger",  # Fallback
        "raw": {}
    }

    coherence = {
        "majority": 1,
        "agreement": 0.85,
        "matrix": {
            "trigger_vs_of": "neutral",
            "trigger_vs_fp": "neutral",
            "of_vs_fp": "aligned"
        }
    }

    quality = {"quality_score": 1.0}

    # Calcul score composite
    composite = fm._calculate_composite_score(n_of, n_fp, n_tr, coherence, quality)

    print(f"\n📊 Score Composite :")
    print(f"  • Base Score    : {composite['base_score']:.3f} (0-1)")
    print(f"  • Pression      : {composite['pression_score']:.3f}")
    print(f"  • Delta         : {composite['delta_score']:.3f}")
    print(f"  • Ratios        : {composite['ratios_score']:.3f}")
    print(f"  • Dynamique     : {composite['dynamique_score']:.3f}")

    print(f"\n📈 Détails :")
    details = composite['details']
    print(f"  • Buy Volume    : {details['buy_vol_combined']:.1f}")
    print(f"  • Sell Volume   : {details['sell_vol_combined']:.1f}")
    print(f"  • Delta Total   : {details['delta_combined']:.1f}")
    print(f"  • Buy Dominance : {details['buy_dominance']:.3f}")

    # Verdict
    score_pct = composite['base_score'] * 100
    if score_pct >= 70:
        verdict = f"✅ MODERATE ({score_pct:.1f}%) - TRADE POSSIBLE sans trigger !"
    elif score_pct >= 55:
        verdict = f"⚠️ CAUTIOUS ({score_pct:.1f}%) - Trade prudent"
    else:
        verdict = f"❌ HOLD ({score_pct:.1f}%) - Trop faible"

    print(f"\n🎯 Verdict : {verdict}")


def test_cas_2_trigger_excellent():
    """
    CAS 2 : Bonne base + Trigger excellent
    Attendu : Score très élevé (DIAMANT)
    """
    print("\n" + "="*70)
    print("CAS 2 : BONNE BASE + TRIGGER EXCELLENT")
    print("="*70)

    fm = FusionManager()

    # Données simulées
    n_of = {
        "score": 0.85,
        "status": "VALID",
        "dir": 1,
        "delta_total": 1500.0,
        "raw": {
            "summary": {
                "delta_total": 1500.0,
                "volume_total": 20000.0,
                "buy_ratio": 0.65,
                "mean_imbalance": 0.65,
                "cvd_slope": 1.2,
            }
        }
    }

    n_fp = {
        "score": 0.80,
        "status": "VALID",
        "dir": 1,
        "delta_total": 1000.0,
        "raw": {
            "summary": {
                "buy_volume": 7000.0,
                "sell_volume": 3500.0,
                "total_volume": 10500.0,
                "tick_count": 150,
                "coverage_s": 22.0,
                "tick_rate": 68.0,
            }
        }
    }

    n_tr = {
        "score": 0.88,  # Trigger excellent
        "dir": 1,
        "type": "stacking",  # Pattern fort
        "raw": {
            "meta": {
                "snapshot_stats": {
                    "volume_zscore_max": 2.8  # Volume exceptionnel
                }
            }
        }
    }

    coherence = {
        "majority": 1,
        "agreement": 1.0,
        "matrix": {
            "trigger_vs_of": "aligned",
            "trigger_vs_fp": "aligned",
            "of_vs_fp": "aligned"
        }
    }

    quality = {"quality_score": 1.0}
    rules_eval = {"aligned3": True}

    # Calcul score composite
    composite = fm._calculate_composite_score(n_of, n_fp, n_tr, coherence, quality)

    # Calcul score final (avec bonus trigger)
    cfg = {}
    ctx = {}
    final = fm._calculate_fused_confidence(n_of, n_fp, n_tr, coherence, quality, cfg, ctx, rules_eval)

    print(f"\n📊 Score Composite (base) : {composite['base_score']:.3f}")
    print(f"📊 Score Final (avec bonus) : {final:.3f}")

    print(f"\n🚀 Amplification par trigger :")
    boost = final - composite['base_score']
    print(f"  • Bonus total : +{boost:.3f} (+{boost*100:.1f}%)")

    # Verdict
    score_pct = final * 100
    if score_pct >= 90:
        verdict = f"💎 DIAMANT ({score_pct:.1f}%) - HIGH CONVICTION IMMEDIATE !"
    elif score_pct >= 80:
        verdict = f"🔷 PLATINE ({score_pct:.1f}%) - HIGH CONVICTION"
    elif score_pct >= 70:
        verdict = f"🟡 OR ({score_pct:.1f}%) - MODERATE"
    else:
        verdict = f"🔘 ARGENT ({score_pct:.1f}%) - CAUTIOUS"

    print(f"\n🎯 Verdict : {verdict}")


def test_cas_3_base_faible_trigger_fort():
    """
    CAS 3 : Base faible + Trigger fort
    Attendu : Filtré malgré le trigger
    """
    print("\n" + "="*70)
    print("CAS 3 : BASE FAIBLE + TRIGGER FORT (doit être filtré)")
    print("="*70)

    fm = FusionManager()

    # Données simulées
    n_of = {
        "score": 0.45,
        "status": "SUSPECT",  # Qualité faible
        "dir": -1,
        "delta_total": 300.0,  # Delta faible
        "raw": {
            "summary": {
                "delta_total": 300.0,
                "volume_total": 5000.0,
                "buy_ratio": 0.45,
                "mean_imbalance": 0.45,
                "cvd_slope": 0.3,
            }
        }
    }

    n_fp = {
        "score": 0.50,
        "status": "VALID",
        "dir": 1,  # Conflit !
        "delta_total": 200.0,
        "raw": {
            "summary": {
                "buy_volume": 2800.0,
                "sell_volume": 2600.0,
                "total_volume": 5400.0,
                "tick_count": 45,  # Trop peu
                "coverage_s": 8.0,  # Trop court
                "tick_rate": 25.0,
            }
        }
    }

    n_tr = {
        "score": 0.82,  # Trigger fort
        "dir": 1,
        "type": "climax",
        "raw": {}
    }

    coherence = {
        "majority": 1,
        "agreement": 0.5,
        "matrix": {
            "trigger_vs_of": "conflict",  # Conflit
            "trigger_vs_fp": "aligned",
            "of_vs_fp": "conflict"
        }
    }

    quality = {"quality_score": 0.5}
    rules_eval = {"aligned3": False}

    # Calcul score composite
    composite = fm._calculate_composite_score(n_of, n_fp, n_tr, coherence, quality)

    # Calcul score final (avec bonus trigger)
    cfg = {}
    ctx = {}
    final = fm._calculate_fused_confidence(n_of, n_fp, n_tr, coherence, quality, cfg, ctx, rules_eval)

    print(f"\n📊 Score Composite (base) : {composite['base_score']:.3f}")
    print(f"📊 Score Final (après filtres) : {final:.3f}")

    # Verdict
    score_pct = final * 100
    if score_pct >= 55:
        verdict = f"⚠️ TRADE ({score_pct:.1f}%) - Risqué mais accepté"
    else:
        verdict = f"✅ FILTRÉ ({score_pct:.1f}%) - Base trop faible, trigger ne sauve pas"

    print(f"\n🎯 Verdict : {verdict}")


if __name__ == "__main__":
    print("\n" + "="*70)
    print("TEST DU NOUVEAU SYSTÈME DE SCORING COMPOSITE")
    print("="*70)

    test_cas_1_forte_pression_sans_trigger()
    test_cas_2_trigger_excellent()
    test_cas_3_base_faible_trigger_fort()

    print("\n" + "="*70)
    print("✅ TOUS LES TESTS TERMINÉS")
    print("="*70)
