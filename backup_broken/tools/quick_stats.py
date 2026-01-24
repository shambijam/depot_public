#!/usr/bin/env python3
"""
Script de statistiques rapides pour suivi quotidien.

Usage:
    python tools/quick_stats.py

Affiche:
    - Nombre de trades collectés
    - Win rate global
    - Distribution par catégorie
    - Trades par jour
"""

import json
from pathlib import Path
from datetime import datetime
from collections import Counter


def main():
    log_file = Path("logs/trades_history.jsonl")

    if not log_file.exists():
        print("❌ Aucun fichier de trades trouvé.")
        print("   Lancez le bot pour commencer à collecter des données.")
        return

    # Charger tous les trades
    trades = []
    with open(log_file, 'r') as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    trades.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

    if not trades:
        print("⚠️  Fichier de trades vide.")
        return

    # Statistiques
    total_trades = len(trades)

    # Résultats
    outcomes = [t.get("outcome") for t in trades if t.get("outcome")]
    wins = outcomes.count("WIN")
    losses = outcomes.count("LOSS")
    be = outcomes.count("BE")
    pending = total_trades - len(outcomes)

    # Win rate
    completed = wins + losses
    win_rate = (wins / completed * 100) if completed > 0 else 0.0

    # Catégories
    categories = [t.get("score_category") for t in trades if t.get("score_category")]
    cat_counts = Counter(categories)

    # PnL
    pnl_usd = [t.get("pnl_usd", 0.0) for t in trades if t.get("outcome") in ["WIN", "LOSS", "BE"]]
    total_pnl = sum(pnl_usd)
    avg_pnl = (total_pnl / len(pnl_usd)) if pnl_usd else 0.0

    # Dates
    entry_times = [t.get("entry_time") for t in trades if t.get("entry_time")]
    if entry_times:
        first_date = entry_times[0].split("T")[0] if entry_times else "N/A"
        last_date = entry_times[-1].split("T")[0] if entry_times else "N/A"

        # Calcul jours
        try:
            first_dt = datetime.fromisoformat(entry_times[0].replace("Z", "+00:00"))
            last_dt = datetime.fromisoformat(entry_times[-1].replace("Z", "+00:00"))
            days = max(1, (last_dt - first_dt).days + 1)
            trades_per_day = total_trades / days
        except:
            days = 1
            trades_per_day = total_trades
    else:
        first_date = "N/A"
        last_date = "N/A"
        days = 1
        trades_per_day = 0

    # Affichage
    print("=" * 70)
    print("📊 STATISTIQUES RAPIDES - COLLECTE DONNÉES")
    print("=" * 70)
    print()
    print(f"📅 Période : {first_date} → {last_date} ({days} jours)")
    print(f"📈 Trades Collectés : {total_trades} trades")
    print(f"⚡ Cadence : {trades_per_day:.1f} trades/jour")
    print()
    print("=" * 70)
    print("🎯 RÉSULTATS")
    print("=" * 70)
    print(f"  ✅ WIN     : {wins:3d} trades")
    print(f"  ❌ LOSS    : {losses:3d} trades")
    print(f"  🟡 BE      : {be:3d} trades")
    print(f"  ⏳ PENDING : {pending:3d} trades")
    print()
    if completed > 0:
        print(f"  📊 Win Rate : {win_rate:.1f}% ({wins}W / {losses}L)")
    else:
        print(f"  📊 Win Rate : N/A (aucun trade terminé)")
    print()
    if pnl_usd:
        print(f"  💰 PnL Total : {total_pnl:+.2f} USD")
        print(f"  💵 PnL Moyen : {avg_pnl:+.2f} USD/trade")
    print()
    print("=" * 70)
    print("📊 DISTRIBUTION PAR CATÉGORIE")
    print("=" * 70)

    # Ordre des catégories
    ordered_cats = ["DIAMANT", "PLATINE", "OR", "ARGENT", "BRONZE"]
    for cat in ordered_cats:
        count = cat_counts.get(cat, 0)
        if count > 0:
            pct = count / total_trades * 100
            emoji = {
                "DIAMANT": "💎",
                "PLATINE": "🔷",
                "OR": "🟡",
                "ARGENT": "🔘",
                "BRONZE": "🟤"
            }.get(cat, "⚪")
            print(f"  {emoji} {cat:8s} : {count:3d} trades ({pct:5.1f}%)")

    print()
    print("=" * 70)
    print("🎯 OBJECTIFS")
    print("=" * 70)

    # Calcul objectifs
    objective = 100
    remaining = max(0, objective - total_trades)
    progress = min(100, total_trades / objective * 100)

    # Barre de progression
    bar_length = 50
    filled = int(bar_length * progress / 100)
    bar = "█" * filled + "░" * (bar_length - filled)

    print(f"  Objectif Phase 1 : {objective} trades")
    print(f"  Progression      : [{bar}] {progress:.1f}%")
    print(f"  Restant          : {remaining} trades")

    if remaining > 0 and trades_per_day > 0:
        days_remaining = remaining / trades_per_day
        print(f"  Estimation       : ~{days_remaining:.1f} jours restants")

    print()

    # Conseils
    if total_trades < 50:
        print("💡 Conseil : Continuez la collecte (minimum 50 trades recommandé)")
    elif total_trades < 100:
        print("💡 Conseil : Bientôt prêt pour première analyse (objectif 100 trades)")
        print("            Commande : python tools/analyze_trades.py --min-trades 50")
    else:
        print("✅ Conseil : Assez de données ! Lancez l'analyse complète")
        print("            Commande : python tools/analyze_trades.py --min-trades 50")

    print()
    print("=" * 70)


if __name__ == "__main__":
    main()
