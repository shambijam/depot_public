#!/usr/bin/env python3
"""
Test du nouveau système de scoring composite - VERSION 2
Avec données plus réalistes
"""

import sys
sys.path.insert(0, '.')

from phase_observer.fusion_manager import FusionManager

def test_signal_ultra_fort():
    """
    Signal ULTRA FORT :
    - Delta combiné : 6000 (très fort)
    - Dominance buy : 75% (très forte)
    - Imbalance : 0.75
    - CVD slope : 2.5 (momentum extrême)
    """
    print("\n" + "="*70)
    print("SIGNAL ULTRA FORT - Sans Trigger")
    print("="*70)

    fm = FusionManager()

    n_of = {
        "score": 0.90,
        "status": "VALID",
        "dir": 1,
        "delta_total": 3500.0,
        "raw": {
            "summary": {
                "delta_total": 3500.0,
                "volume_total": 40000.0,
                "buy_ratio": 0.75,
                "mean_imbalance": 0.75,
                "cvd_slope": 2.5,
            }
        }
    }

    n_fp = {
        "score": 0.85,
        "status": "VALID",
        "dir": 1,
        "delta_total": 2500.0,
        "raw": {
            "summary": {
                "buy_volume": 15000.0,
                "sell_volume": 5000.0,
                "total_volume": 20000.0,
                "tick_count": 250,
                "coverage_s": 30.0,
                "tick_rate": 83.0,
            }
        }
    }

    n_tr = {
        "score": 0.0,
        "dir": 0,
        "type": "fusion_pretrigger",
        "raw": {}
    }

    coherence = {
        "majority": 1,
        "agreement": 1.0,
        "matrix": {
            "trigger_vs_of": "neutral",
            "trigger_vs_fp": "neutral",
            "of_vs_fp": "aligned"
        }
    }

    quality = {"quality_score": 1.0}
    rules_eval = {"aligned3": False}

    composite = fm._calculate_composite_score(n_of, n_fp, n_tr, coherence, quality)
    cfg = {}
    ctx = {}
    final = fm._calculate_fused_confidence(n_of, n_fp, n_tr, coherence, quality, cfg, ctx, rules_eval)

    print(f"\n📊 Score Composite :")
    print(f"  • Base Score    : {composite['base_score']:.3f}")
    print(f"  • Pression      : {composite['pression_score']:.3f} (40%)")
    print(f"  • Delta         : {composite['delta_score']:.3f} (20%)")
    print(f"  • Ratios        : {composite['ratios_score']:.3f} (30%)")
    print(f"  • Dynamique     : {composite['dynamique_score']:.3f} (10%)")

    print(f"\n📈 Détails :")
    details = composite['details']
    print(f"  • Buy Volume    : {details['buy_vol_combined']:.1f}")
    print(f"  • Sell Volume   : {details['sell_vol_combined']:.1f}")
    print(f"  • Delta Total   : {details['delta_combined']:.1f}")
    print(f"  • Buy Dominance : {details['buy_dominance']:.1%}")

    print(f"\n📊 Score Final (après qualité) : {final:.3f} ({final*100:.1f}%)")

    if final >= 0.80:
        verdict = "🔷 PLATINE - HIGH CONVICTION"
    elif final >= 0.70:
        verdict = "🟡 OR - MODERATE"
    elif final >= 0.55:
        verdict = "🔘 ARGENT - CAUTIOUS"
    else:
        verdict = "❌ HOLD"

    print(f"🎯 Verdict : {verdict}")

if __name__ == "__main__":
    test_signal_ultra_fort()
