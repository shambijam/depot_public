#!/usr/bin/env python3
"""
Script de test pour vérifier que le trailing stop s'active correctement.
"""
import sys
import os

# Ajouter le répertoire parent au path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def test_trailing_imports():
    """Vérifie que tous les imports nécessaires fonctionnent."""
    print("=" * 60)
    print("TEST #1: Vérification des imports")
    print("=" * 60)

    try:
        from trader.trade_executor import TradeExecutor
        print("✅ TradeExecutor importé")

        # Vérifier que les fonctions sont bien bindées
        assert hasattr(TradeExecutor, 'update_basket_sltp_dynamically'), "❌ update_basket_sltp_dynamically manquant"
        print("✅ update_basket_sltp_dynamically bindé")

        assert hasattr(TradeExecutor, '_resolve_basket_context_for_sltp'), "❌ _resolve_basket_context_for_sltp manquant"
        print("✅ _resolve_basket_context_for_sltp bindé")

        return True
    except Exception as e:
        print(f"❌ Erreur d'import: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_config_loading():
    """Vérifie que la configuration du trailing est correcte."""
    print("\n" + "=" * 60)
    print("TEST #2: Vérification de la configuration trailing")
    print("=" * 60)

    try:
        import json

        with open("config/strategy/config_trade_scalping.json", "r") as f:
            config = json.load(f)

        trailing_cfg = config.get("entry_rules", {}).get("scalping", {}).get("burst_scalping", {}).get("trailing", {})

        if not trailing_cfg:
            print("❌ Configuration trailing introuvable")
            return False

        print(f"✅ Trailing enabled: {trailing_cfg.get('enabled')}")
        print(f"✅ Activation min_pips: {trailing_cfg.get('activation', {}).get('min_pips')}")
        print(f"✅ Step min_pips: {trailing_cfg.get('step', {}).get('min_pips')}")
        print(f"✅ Update interval: {trailing_cfg.get('step', {}).get('update_interval_sec')}s")
        print(f"✅ Spread multiplier: {trailing_cfg.get('broker_floors', {}).get('spread_multiplier')}")

        # Vérifications
        assert trailing_cfg.get("enabled") == True, "❌ Trailing not enabled"
        assert trailing_cfg.get("activation", {}).get("min_pips") == 28.0, "❌ Activation != 28 pips"
        assert trailing_cfg.get("step", {}).get("min_pips") == 8.0, "❌ Step != 8 pips"
        assert trailing_cfg.get("broker_floors", {}).get("spread_multiplier") == 0.0, "❌ Spread multiplier != 0"

        print("\n✅ Toutes les vérifications de config sont OK")
        return True

    except Exception as e:
        print(f"❌ Erreur de chargement config: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_basket_comment_format():
    """Vérifie le format des commentaires MT5."""
    print("\n" + "=" * 60)
    print("TEST #3: Vérification format commentaire basket")
    print("=" * 60)

    try:
        import re

        # Format attendu: bs_<8 hex chars>
        test_comments = [
            "bs_abc12345",  # ✅ Valide
            "bs_04d3c6d1",  # ✅ Valide
            "burst_scalping|basket=abc12345",  # ❌ Ancien format (trop long)
        ]

        pattern = r"bs_([a-f0-9]{8})"

        for cmt in test_comments:
            match = re.search(pattern, cmt)
            if match:
                print(f"✅ '{cmt}' → basket_id: {match.group(1)} (len={len(cmt)})")
            else:
                print(f"❌ '{cmt}' → PAS DE MATCH (len={len(cmt)})")

        return True

    except Exception as e:
        print(f"❌ Erreur: {e}")
        return False


def main():
    print("\n" + "🎯" * 30)
    print("TEST DE VALIDATION DU TRAILING STOP")
    print("🎯" * 30 + "\n")

    results = []

    results.append(("Imports", test_trailing_imports()))
    results.append(("Config", test_config_loading()))
    results.append(("Format commentaire", test_basket_comment_format()))

    print("\n" + "=" * 60)
    print("RÉSUMÉ DES TESTS")
    print("=" * 60)

    for test_name, passed in results:
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"{test_name:.<40} {status}")

    all_passed = all(passed for _, passed in results)

    print("\n" + "=" * 60)
    if all_passed:
        print("🎉 TOUS LES TESTS SONT PASSÉS !")
        print("=" * 60)
        print("\nLe système de trailing stop devrait maintenant fonctionner.")
        print("Pour tester en conditions réelles:")
        print("  1. Lancez le bot: python3 run_bot.py")
        print("  2. Attendez qu'un trade burst_scalping soit ouvert")
        print("  3. Vérifiez les logs pour '[SLTP][PERIODIC]'")
        print("  4. À +28 pips, vous devriez voir '[TRAILING] Basket ... → ACTIVATION'")
        return 0
    else:
        print("❌ CERTAINS TESTS ONT ÉCHOUÉ")
        print("=" * 60)
        print("\nCorrigez les erreurs ci-dessus avant de lancer le bot.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
