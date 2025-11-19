#!/usr/bin/env python3
"""
Script de test pour valider le calcul de sizing
Vérifie que le volume change correctement avec risk% et equity
"""

import sys
from typing import NamedTuple

# Mock de symbol_info
class SymbolInfo(NamedTuple):
    name: str
    trade_tick_value: float
    trade_tick_size: float
    volume_min: float
    volume_max: float
    volume_step: float


def test_sizing_calculation():
    """Test le calcul de volume avec différents paramètres"""

    print("=" * 80)
    print("🧪 TEST DU SYSTÈME DE SIZING")
    print("=" * 80)
    print()

    # Import du module sizing
    sys.path.insert(0, '/home/workdev/sniper_x_dev')
    from trader import sizing

    # Mock de self (minimal)
    class MockTrader:
        pass

    trader = MockTrader()

    # Symbol info XAUUSD
    symbol_info = SymbolInfo(
        name="XAUUSD",
        trade_tick_value=1.0,
        trade_tick_size=0.01,
        volume_min=0.01,
        volume_max=100.0,
        volume_step=0.01
    )

    # Prix d'exemple
    entry_price = 2095.67
    sl_price = 2091.67  # SL à 400 pips (4.0 points)

    # Tests avec différents paramètres
    test_cases = [
        # (equity, risk%, burst_size, description)
        (10000, 0.73, 8, "Config actuelle (AVANT fix)"),
        (10000, 1.5, 8, "Config après fix (risk% doublé)"),
        (20000, 1.5, 8, "Capital doublé (equity × 2)"),
        (10000, 1.5, 5, "Burst_size réduit (8→5)"),
        (50000, 1.5, 8, "Capital réel utilisateur"),
    ]

    print(f"📊 Configuration de base:")
    print(f"   • Symbol:        XAUUSD")
    print(f"   • Entry:         {entry_price}")
    print(f"   • SL:            {sl_price}")
    print(f"   • Distance SL:   {abs(entry_price - sl_price):.2f} points (400 pips)")
    print(f"   • Tick value:    {symbol_info.trade_tick_value}")
    print(f"   • Tick size:     {symbol_info.trade_tick_size}")
    print()
    print("=" * 80)
    print()

    results = []

    for equity, risk_pct, burst_size, description in test_cases:
        print(f"🔬 Test: {description}")
        print(f"   Equity: {equity}$ | Risk: {risk_pct}% | Burst: {burst_size}")

        trade_decision = {
            "action": "BUY",
            "asset": "XAUUSD",
            "rule_name": "burst_scalping",
            "sizing_scope": "BASKET",
            "burst_size": burst_size,
        }

        account_trade_settings = {
            "equity": equity,
            "risk_per_trade_percent": risk_pct,
            "min_lot": 0.01,
            "max_lot": 100.0,
            "lot_step": 0.01,
        }

        try:
            volume = sizing._calculate_risk_based_volume(
                trader,
                trade_decision,
                {},  # config
                {},  # context
                symbol_info,
                entry_price,
                sl_price,
                account_trade_settings
            )

            budget = equity * (risk_pct / 100)
            risk_per_pos = budget / burst_size

            print(f"   ✅ Volume calculé: {volume:.2f} lots")
            print(f"   → Budget total: {budget:.2f}$")
            print(f"   → Risk/position: {risk_per_pos:.2f}$")
            print(f"   → Volume total basket: {volume * burst_size:.2f} lots")
            print()

            results.append({
                "description": description,
                "equity": equity,
                "risk_pct": risk_pct,
                "burst_size": burst_size,
                "volume": volume,
                "budget": budget,
                "success": True
            })

        except Exception as e:
            print(f"   ❌ ERREUR: {e}")
            print()
            results.append({
                "description": description,
                "equity": equity,
                "risk_pct": risk_pct,
                "burst_size": burst_size,
                "error": str(e),
                "success": False
            })

    # Résumé comparatif
    print("=" * 80)
    print("📊 RÉSUMÉ COMPARATIF")
    print("=" * 80)
    print()

    print(f"{'Description':<40} {'Volume':<12} {'Budget':<12} {'Status'}")
    print("-" * 80)

    for r in results:
        if r["success"]:
            print(f"{r['description']:<40} {r['volume']:>8.2f} lots {r['budget']:>8.2f} $ ✅")
        else:
            print(f"{r['description']:<40} {'ERROR':<12} {'N/A':<12} ❌")

    print()
    print("=" * 80)
    print("🎯 VÉRIFICATIONS CRITIQUES")
    print("=" * 80)
    print()

    # Vérifier que le volume change avec risk%
    if results[0]["success"] and results[1]["success"]:
        vol_before = results[0]["volume"]
        vol_after = results[1]["volume"]
        ratio = vol_after / vol_before if vol_before > 0 else 0
        expected_ratio = results[1]["risk_pct"] / results[0]["risk_pct"]

        print(f"✅ Volume change avec risk% ?")
        print(f"   • Avant (0.73%): {vol_before:.2f} lots")
        print(f"   • Après (1.5%):  {vol_after:.2f} lots")
        print(f"   • Ratio:         {ratio:.2f}x (attendu: {expected_ratio:.2f}x)")

        if abs(ratio - expected_ratio) < 0.01:
            print(f"   ✅ CORRECT: Le volume est proportionnel au risk%")
        else:
            print(f"   ❌ PROBLÈME: Le ratio ne correspond pas !")
        print()

    # Vérifier que le volume change avec equity
    if results[1]["success"] and results[2]["success"]:
        vol_equity1 = results[1]["volume"]
        vol_equity2 = results[2]["volume"]
        ratio = vol_equity2 / vol_equity1 if vol_equity1 > 0 else 0
        expected_ratio = results[2]["equity"] / results[1]["equity"]

        print(f"✅ Volume change avec equity ?")
        print(f"   • Equity 10k:    {vol_equity1:.2f} lots")
        print(f"   • Equity 20k:    {vol_equity2:.2f} lots")
        print(f"   • Ratio:         {ratio:.2f}x (attendu: {expected_ratio:.2f}x)")

        if abs(ratio - expected_ratio) < 0.01:
            print(f"   ✅ CORRECT: Le volume est proportionnel à l'equity")
        else:
            print(f"   ❌ PROBLÈME: Le ratio ne correspond pas !")
        print()

    print("=" * 80)
    print("🏁 FIN DU TEST")
    print("=" * 80)


if __name__ == "__main__":
    test_sizing_calculation()
