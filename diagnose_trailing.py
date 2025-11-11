#!/usr/bin/env python3
"""
Script de diagnostic complet pour identifier pourquoi le trailing ne s'active pas.

Usage:
    python diagnose_trailing.py

Ce script vérifie :
1. Que toutes les fonctions existent et sont bindées
2. Que la config trailing est correcte
3. Que le thread monitoring peut tourner
4. Simule un basket à +30 pips pour voir ce qui se passe
"""

import sys
import os
import logging

# Setup logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

print("=" * 80)
print("🔍 DIAGNOSTIC TRAILING STOP")
print("=" * 80)
print()

# ===========================
# TEST 1: Imports
# ===========================
print("TEST 1: Vérification des imports...")
try:
    from core.config_manager import ConfigManager
    from trader.trade_executor import TradeExecutor
    from connectors.mt5_connector import MT5Connector
    print("✅ Imports OK")
except Exception as e:
    print(f"❌ ERREUR IMPORTS: {e}")
    sys.exit(1)

# ===========================
# TEST 2: Bindings des Fonctions
# ===========================
print("\nTEST 2: Vérification des bindings...")

try:
    # Charger config
    config_manager = ConfigManager("config/prod_config.json")

    # Créer instances (sans MT5 réel)
    trade_executor = TradeExecutor(
        config_manager=config_manager,
        mt5_connector=None,  # Mock
        mode="DEMO"
    )

    # Vérifier les fonctions bindées
    functions_to_check = [
        "update_basket_sltp_dynamically",
        "_resolve_basket_context_for_sltp",
        "_calculate_dynamic_trailing",
        "apply_dynamic_trailing"
    ]

    missing = []
    for func_name in functions_to_check:
        if not hasattr(trade_executor, func_name):
            missing.append(func_name)
            print(f"  ❌ {func_name} - MANQUANTE")
        else:
            print(f"  ✅ {func_name} - OK")

    if missing:
        print(f"\n❌ {len(missing)} fonction(s) manquante(s): {missing}")
        print("→ Relancer les corrections des Bugs #1, #4, #9")
        sys.exit(1)
    else:
        print("\n✅ Toutes les fonctions sont bindées")

except Exception as e:
    print(f"❌ ERREUR BINDINGS: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

# ===========================
# TEST 3: Configuration Trailing
# ===========================
print("\nTEST 3: Vérification de la configuration trailing...")

try:
    config = config_manager.get_current_dynamic_config()

    # Extraire config trailing
    trailing_cfg = (
        config.get("entry_rules", {})
        .get("scalping", {})
        .get("burst_scalping", {})
        .get("trailing", {})
    )

    if not trailing_cfg:
        print("❌ Config trailing VIDE ou inexistante")
        sys.exit(1)

    enabled = trailing_cfg.get("enabled", False)
    activation_pips = trailing_cfg.get("activation_pips", 0)
    step_pips = trailing_cfg.get("step", {}).get("min_distance_pips", 0)
    update_interval = trailing_cfg.get("step", {}).get("update_interval_sec", 0)

    print(f"  • Enabled: {enabled}")
    print(f"  • Activation: {activation_pips} pips")
    print(f"  • Step: {step_pips} pips")
    print(f"  • Update interval: {update_interval}s")

    if not enabled:
        print("\n❌ Trailing DÉSACTIVÉ dans la config !")
        print("→ Activer dans config/strategy/config_trade_scalping.json")
        sys.exit(1)

    if activation_pips != 28.0:
        print(f"\n⚠️ WARNING: Activation = {activation_pips} pips (attendu: 28.0)")

    if step_pips != 8.0:
        print(f"\n⚠️ WARNING: Step = {step_pips} pips (attendu: 8.0)")

    print("\n✅ Configuration trailing correcte")

except Exception as e:
    print(f"❌ ERREUR CONFIG: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

# ===========================
# TEST 4: Format Commentaire Basket
# ===========================
print("\nTEST 4: Vérification format commentaire basket...")

import re

BASKET_PATTERN = re.compile(r"bs_([a-f0-9]{8})")

test_comments = [
    ("bs_12345678", True, "Format correct"),
    ("bs_abcdef12", True, "Format correct (hex)"),
    ("burst_scalping|basket=abc", False, "Ancien format (tronqué)"),
    ("bs_", False, "ID manquant"),
    ("bs_1234567", False, "ID trop court (7 chars)"),
    ("bs_123456789", False, "ID trop long (9 chars)"),
    ("BS_12345678", False, "Majuscules (ne matche pas)"),
]

print("  Test du pattern: r\"bs_([a-f0-9]{8})\"")
print()

all_ok = True
for comment, should_match, desc in test_comments:
    match = BASKET_PATTERN.search(comment)
    matched = match is not None

    status = "✅" if matched == should_match else "❌"
    result = f"Match: {match.group(1)}" if match else "No match"
    print(f"  {status} '{comment}' → {result} ({desc})")

    if matched != should_match:
        all_ok = False

if all_ok:
    print("\n✅ Pattern regex correct")
else:
    print("\n❌ Problème avec le pattern regex")
    sys.exit(1)

# ===========================
# TEST 5: Simulation Basket à +30 Pips
# ===========================
print("\nTEST 5: Simulation d'un basket à +30 pips...")
print("(Test sans connexion MT5 réelle)")

try:
    # Mock d'un basket context
    mock_basket_context = {
        "basket_id": "abc12345",
        "symbol": "XAUUSD",
        "direction": "BUY",
        "entry_price": 4100.00,
        "current_positions": 8,
        "target_burst_size": 8,
        "basket_pnl_pips": 30.5,  # Au-dessus du seuil
        "positions_details": [
            {"ticket": 123456, "sl": 4096.0, "tp": 4104.0}
        ]
    }

    # Mock symbol info
    class MockSymbolInfo:
        point = 0.01
        digits = 2
        trade_tick_size = 0.01
        trade_stops_level = 0

    symbol_info = MockSymbolInfo()

    # Test de _calculate_dynamic_trailing
    print("  Appel de _calculate_dynamic_trailing()...")

    new_sl = trade_executor._calculate_dynamic_trailing(
        current_price=4100.305,  # +30.5 pips
        entry_price=4100.00,
        current_sl=4096.0,  # -400 pips
        basket_context=mock_basket_context,
        volatility=None,
        symbol_info=symbol_info,
        min_distance_pips=8.0,
        activation_pips=28.0,
        min_update_interval_sec=0,  # Pas de throttling pour le test
    )

    if new_sl is None:
        print("  ❌ _calculate_dynamic_trailing a retourné None")
        print("  → Le trailing ne s'activera PAS")
        print("  → Regarder les logs DEBUG pour comprendre pourquoi")
    else:
        expected_sl = 4100.305 - (8.0 * 0.01)  # Prix - 8 pips
        print(f"  ✅ Nouveau SL calculé: {new_sl:.2f}")
        print(f"  Expected SL: {expected_sl:.2f} (prix - 8 pips)")
        print(f"  → Le trailing DEVRAIT s'activer !")

        if abs(new_sl - expected_sl) < 0.02:
            print(f"  ✅ Calcul correct !")
        else:
            print(f"  ⚠️ Différence détectée: {abs(new_sl - expected_sl):.2f} pips")

except Exception as e:
    print(f"  ❌ ERREUR durant le test: {e}")
    import traceback
    traceback.print_exc()

# ===========================
# RÉSUMÉ
# ===========================
print()
print("=" * 80)
print("📊 RÉSUMÉ DU DIAGNOSTIC")
print("=" * 80)
print()
print("Si tous les tests sont ✅, le problème est probablement:")
print("1. Le thread monitoring ne démarre pas sur Windows VPS")
print("2. Les commentaires MT5 sont incorrects")
print("3. Les trades n'atteignent jamais +28 pips")
print()
print("PROCHAINES ÉTAPES:")
print("1. Lancer le bot sur Windows VPS")
print("2. Récupérer les logs (chercher '[TRAILING_MONITOR]')")
print("3. Ouvrir manuellement un trade avec commentaire 'bs_12345678'")
print("4. Le faire monter à +30 pips")
print("5. Observer si le SL bouge")
print()
print("=" * 80)
