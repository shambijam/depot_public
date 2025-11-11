#!/usr/bin/env python3
"""
Script de diagnostic pour identifier pourquoi le trailing ne s'active pas.
À exécuter sur le Windows VPS.

Usage:
    python diagnose_trailing_vps.py
"""

import os
import sys
import re
from pathlib import Path

print("=" * 80)
print("🔍 DIAGNOSTIC TRAILING STOP - WINDOWS VPS")
print("=" * 80)
print()

# ===========================
# TEST 1: Vérifier que les logs DEBUG existent dans sltp.py
# ===========================
print("TEST 1: Vérification du patch debug dans sltp.py...")
print()

sltp_file = Path(__file__).parent / "trader" / "sltp.py"

if not sltp_file.exists():
    print(f"❌ ERREUR: Fichier {sltp_file} introuvable")
    sys.exit(1)

with open(sltp_file, "r", encoding="utf-8") as f:
    sltp_content = f.read()

# Chercher les 5 logs debug
debug_patterns = [
    r'🔥 \[DEBUG_TRAILING\] DÉBUT update_basket_sltp_dynamically',
    r'🔥 \[DEBUG_TRAILING\] Contexte récupéré:',
    r'🔥 \[DEBUG_TRAILING\] MODE PROFIT activé',
    r'🔥 \[DEBUG_TRAILING\] apply_dynamic_trailing returned:',
    r'🔥 \[DEBUG_TRAILING\] RÉSULTAT FINAL',
]

found_count = 0
for i, pattern in enumerate(debug_patterns, 1):
    if re.search(pattern, sltp_content):
        print(f"  ✅ LOG #{i} trouvé: {pattern[:50]}...")
        found_count += 1
    else:
        print(f"  ❌ LOG #{i} MANQUANT: {pattern[:50]}...")

if found_count == 5:
    print()
    print("✅ RÉSULTAT: Tous les 5 logs debug sont présents dans sltp.py")
else:
    print()
    print(f"❌ RÉSULTAT: Seulement {found_count}/5 logs debug trouvés")
    print("→ Le fichier sltp.py n'a PAS été correctement mis à jour")
    print("→ Il faut copier le fichier depuis Ubuntu ou re-pull depuis GitHub")
    sys.exit(1)

# ===========================
# TEST 2: Vérifier que la fonction update_basket_sltp_dynamically est bindée
# ===========================
print()
print("TEST 2: Vérification du binding de la fonction...")
print()

try:
    from trader.trade_executor import TradeExecutor

    if hasattr(TradeExecutor, "update_basket_sltp_dynamically"):
        print("  ✅ TradeExecutor.update_basket_sltp_dynamically existe")
    else:
        print("  ❌ TradeExecutor.update_basket_sltp_dynamically MANQUANT")
        print("  → Bug #1 pas corrigé: fonction pas bindée")
        sys.exit(1)

    if hasattr(TradeExecutor, "_resolve_basket_context_for_sltp"):
        print("  ✅ TradeExecutor._resolve_basket_context_for_sltp existe")
    else:
        print("  ❌ TradeExecutor._resolve_basket_context_for_sltp MANQUANT")
        print("  → Bug #4 pas corrigé: fonction pas bindée")
        sys.exit(1)

    if hasattr(TradeExecutor, "_calculate_dynamic_trailing"):
        print("  ✅ TradeExecutor._calculate_dynamic_trailing existe")
    else:
        print("  ❌ TradeExecutor._calculate_dynamic_trailing MANQUANT")
        print("  → Bug #9 pas corrigé: fonction pas bindée")
        sys.exit(1)

    print()
    print("✅ RÉSULTAT: Toutes les fonctions sont bindées correctement")

except Exception as e:
    print(f"  ❌ ERREUR lors de l'import: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

# ===========================
# TEST 3: Chercher les logs dans le fichier de logs récent
# ===========================
print()
print("TEST 3: Recherche des logs dans les fichiers récents...")
print()

logs_dir = Path(__file__).parent / "logs"

if not logs_dir.exists():
    print("⚠️ Répertoire logs/ introuvable, création...")
    logs_dir.mkdir(exist_ok=True)

# Chercher le fichier de log le plus récent
log_files = sorted(logs_dir.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)

if not log_files:
    print("⚠️ Aucun fichier de log trouvé dans logs/")
    print("→ Le bot a-t-il été lancé récemment ?")
else:
    latest_log = log_files[0]
    print(f"📄 Fichier de log le plus récent: {latest_log.name}")
    print()

    with open(latest_log, "r", encoding="utf-8", errors="ignore") as f:
        log_content = f.read()

    # Chercher les logs du thread
    thread_logs = re.findall(r'.*\[TRAILING_MONITOR\].*', log_content)
    debug_logs = re.findall(r'.*🔥 \[DEBUG_TRAILING\].*', log_content)

    if thread_logs:
        print(f"  ✅ Logs [TRAILING_MONITOR] trouvés: {len(thread_logs)} lignes")
        print("  Exemples:")
        for log in thread_logs[:3]:
            print(f"    {log.strip()}")
    else:
        print("  ❌ Aucun log [TRAILING_MONITOR] trouvé")
        print("  → Le thread de surveillance NE DÉMARRE PAS")

    print()

    if debug_logs:
        print(f"  ✅ Logs 🔥 [DEBUG_TRAILING] trouvés: {len(debug_logs)} lignes")
        print("  Exemples:")
        for log in debug_logs[:3]:
            print(f"    {log.strip()}")
    else:
        print("  ❌ Aucun log 🔥 [DEBUG_TRAILING] trouvé")
        print("  → La fonction update_basket_sltp_dynamically N'EST JAMAIS APPELÉE")

# ===========================
# TEST 4: Vérifier que le thread est défini dans run_bot.py
# ===========================
print()
print("TEST 4: Vérification du thread dans run_bot.py...")
print()

run_bot_file = Path(__file__).parent / "run_bot.py"

if not run_bot_file.exists():
    print(f"❌ ERREUR: Fichier {run_bot_file} introuvable")
    sys.exit(1)

with open(run_bot_file, "r", encoding="utf-8") as f:
    run_bot_content = f.read()

# Chercher la fonction de thread
if "def trailing_stop_monitor_thread(" in run_bot_content:
    print("  ✅ Fonction trailing_stop_monitor_thread() définie")
else:
    print("  ❌ Fonction trailing_stop_monitor_thread() MANQUANTE")
    sys.exit(1)

# Chercher le démarrage du thread
if "threading.Thread(target=trailing_stop_monitor_thread" in run_bot_content:
    print("  ✅ Thread lancé avec threading.Thread()")
else:
    print("  ⚠️ Thread peut-être lancé différemment")

# Chercher les logs de démarrage
if "Thread de surveillance trailing stop démarré" in run_bot_content:
    print("  ✅ Log de démarrage du thread présent dans le code")
else:
    print("  ⚠️ Log de démarrage non trouvé")

print()
print("✅ RÉSULTAT: Thread correctement défini dans run_bot.py")

# ===========================
# RÉSUMÉ ET DIAGNOSTIC
# ===========================
print()
print("=" * 80)
print("📊 RÉSUMÉ DU DIAGNOSTIC")
print("=" * 80)
print()

# Analyser la situation
if found_count == 5:
    print("✅ Patch debug appliqué: OUI")
else:
    print("❌ Patch debug appliqué: NON")

if log_files and thread_logs:
    print("✅ Thread de surveillance: ACTIF")
elif log_files and not thread_logs:
    print("❌ Thread de surveillance: INACTIF")
    print()
    print("🔍 PROBLÈME IDENTIFIÉ:")
    print("   Le thread trailing_stop_monitor_thread NE DÉMARRE PAS")
    print()
    print("📋 CAUSES POSSIBLES:")
    print("   1. Erreur au démarrage du thread (exception capturée)")
    print("   2. Condition trailing_enabled=False dans la config")
    print("   3. trade_executor ou mt5_connector est None au démarrage")
    print()
    print("🔧 PROCHAINE ÉTAPE:")
    print("   1. Chercher 'trailing_enabled' dans config/prod_config.json")
    print("   2. Vérifier qu'il est à 'true'")
    print("   3. Chercher des erreurs au démarrage du bot dans les logs")
    print("   4. Chercher '[TRAILING_MONITOR] Thread NE PEUT PAS démarrer' dans les logs")
else:
    print("⚠️ Thread de surveillance: IMPOSSIBLE À VÉRIFIER (aucun log)")

if log_files and debug_logs:
    print("✅ Fonction update_basket_sltp_dynamically: APPELÉE")
elif log_files and thread_logs and not debug_logs:
    print("⚠️ Fonction update_basket_sltp_dynamically: PAS APPELÉE")
    print()
    print("🔍 PROBLÈME IDENTIFIÉ:")
    print("   Le thread tourne mais la fonction n'est jamais appelée")
    print()
    print("📋 CAUSES POSSIBLES:")
    print("   1. Aucun basket détecté (commentaire MT5 incorrect)")
    print("   2. Format commentaire ne matche pas le regex bs_([a-f0-9]{8})")
    print("   3. trade_executor.update_basket_sltp_dynamically pas bindé (Bug #1)")
else:
    print("⚠️ Fonction update_basket_sltp_dynamically: IMPOSSIBLE À VÉRIFIER")

print()
print("=" * 80)
print("📞 ENVOYER CES RÉSULTATS À CLAUDE")
print("=" * 80)
print()
print("Copiez TOUTE la sortie de ce script et envoyez-la pour analyse.")
print()
