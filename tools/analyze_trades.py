#!/usr/bin/env python3
"""
Script d'analyse des performances des trades.

Lit le fichier logs/trades_history.jsonl et génère des rapports pour:
1. Win rate par catégorie de score (DIAMANT, PLATINE, OR, etc.)
2. Performance par type de trigger
3. Impact des pénalités qualité (tick_count, coverage_s)
4. Corrélation score vs PnL réel

Usage:
    python tools/analyze_trades.py
    python tools/analyze_trades.py --min-trades 50  # Attendre 50 trades minimum
"""

import json
import sys
from pathlib import Path
from typing import List, Dict, Any
from collections import defaultdict
from datetime import datetime


def load_trades(log_file: Path) -> List[Dict[str, Any]]:
    """Charge tous les trades depuis le fichier JSON Lines."""
    trades = []
    if not log_file.exists():
        print(f"❌ Fichier {log_file} introuvable")
        return trades

    with open(log_file, 'r', encoding='utf-8') as f:
        for line_num, line in enumerate(f, 1):
            try:
                trade = json.loads(line.strip())
                # Ne garder que les trades COMPLÉTÉS (avec outcome)
                if trade.get("outcome") is not None:
                    trades.append(trade)
            except json.JSONDecodeError as e:
                print(f"⚠️ Ligne {line_num} invalide: {e}")

    return trades


def analyze_by_score_category(trades: List[Dict]) -> None:
    """Analyse win rate par catégorie de score."""
    print("\n" + "="*80)
    print("📊 PERFORMANCE PAR CATÉGORIE DE SCORE")
    print("="*80)

    stats_by_category = defaultdict(lambda: {
        "count": 0,
        "wins": 0,
        "losses": 0,
        "be": 0,
        "total_pnl_pips": 0.0,
        "total_pnl_usd": 0.0,
        "total_duration": 0.0
    })

    for trade in trades:
        category = trade.get("score_category", "UNKNOWN")
        outcome = trade.get("outcome")
        pnl_pips = trade.get("pnl_pips", 0.0)
        pnl_usd = trade.get("pnl_usd", 0.0)
        duration = trade.get("duration_minutes", 0.0)

        stats = stats_by_category[category]
        stats["count"] += 1
        stats["total_pnl_pips"] += pnl_pips
        stats["total_pnl_usd"] += pnl_usd
        stats["total_duration"] += duration

        if outcome == "WIN":
            stats["wins"] += 1
        elif outcome == "LOSS":
            stats["losses"] += 1
        elif outcome == "BE":
            stats["be"] += 1

    # Affichage
    categories_order = ["DIAMANT", "PLATINE", "OR", "ARGENT", "BRONZE", "UNKNOWN"]
    for category in categories_order:
        if category not in stats_by_category:
            continue

        stats = stats_by_category[category]
        count = stats["count"]
        wins = stats["wins"]
        losses = stats["losses"]
        be = stats["be"]
        win_rate = (wins / count * 100) if count > 0 else 0
        avg_pnl_pips = stats["total_pnl_pips"] / count if count > 0 else 0
        avg_pnl_usd = stats["total_pnl_usd"] / count if count > 0 else 0
        avg_duration = stats["total_duration"] / count if count > 0 else 0

        # Emoji selon performance
        if win_rate >= 70:
            emoji = "🟢"
        elif win_rate >= 55:
            emoji = "🟡"
        else:
            emoji = "🔴"

        print(f"\n{emoji} {category:10} (score ≥X%)")
        print(f"   Trades      : {count:>4} trades")
        print(f"   Win Rate    : {win_rate:>5.1f}% ({wins}W / {losses}L / {be}BE)")
        print(f"   Avg PnL     : {avg_pnl_pips:>+7.1f} pips ({avg_pnl_usd:>+8.2f} USD)")
        print(f"   Avg Duration: {avg_duration:>6.1f} min")


def analyze_by_trigger_type(trades: List[Dict]) -> None:
    """Analyse win rate par type de trigger."""
    print("\n" + "="*80)
    print("🎯 PERFORMANCE PAR TYPE DE TRIGGER")
    print("="*80)

    stats_by_trigger = defaultdict(lambda: {
        "count": 0,
        "wins": 0,
        "losses": 0,
        "total_pnl_pips": 0.0,
        "avg_trigger_conf": []
    })

    for trade in trades:
        trigger_type = trade.get("trigger_type", "none")
        outcome = trade.get("outcome")
        pnl_pips = trade.get("pnl_pips", 0.0)
        trigger_conf = trade.get("trigger_confidence", 0.0)

        stats = stats_by_trigger[trigger_type]
        stats["count"] += 1
        stats["total_pnl_pips"] += pnl_pips
        stats["avg_trigger_conf"].append(trigger_conf)

        if outcome == "WIN":
            stats["wins"] += 1
        elif outcome == "LOSS":
            stats["losses"] += 1

    # Trier par nombre de trades
    sorted_triggers = sorted(stats_by_trigger.items(), key=lambda x: x[1]["count"], reverse=True)

    for trigger_type, stats in sorted_triggers:
        count = stats["count"]
        wins = stats["wins"]
        losses = stats["losses"]
        win_rate = (wins / count * 100) if count > 0 else 0
        avg_pnl = stats["total_pnl_pips"] / count if count > 0 else 0
        avg_conf = sum(stats["avg_trigger_conf"]) / len(stats["avg_trigger_conf"]) if stats["avg_trigger_conf"] else 0

        emoji = "🟢" if win_rate >= 65 else "🟡" if win_rate >= 50 else "🔴"

        print(f"\n{emoji} {trigger_type:20}")
        print(f"   Trades   : {count:>4}")
        print(f"   Win Rate : {win_rate:>5.1f}% ({wins}W / {losses}L)")
        print(f"   Avg PnL  : {avg_pnl:>+7.1f} pips")
        print(f"   Avg Conf : {avg_conf:>5.1%}")


def analyze_quality_impact(trades: List[Dict]) -> None:
    """Analyse l'impact des métriques de qualité sur la performance."""
    print("\n" + "="*80)
    print("📈 IMPACT QUALITÉ DONNÉES (tick_count, coverage_s)")
    print("="*80)

    # Segmentation par tick_count
    tick_segments = {
        "< 50 ticks": [],
        "50-100 ticks": [],
        "> 100 ticks": []
    }

    for trade in trades:
        tick_count = trade.get("tick_count", 0)
        outcome = trade.get("outcome")
        pnl_pips = trade.get("pnl_pips", 0.0)

        if outcome not in ["WIN", "LOSS"]:
            continue

        if tick_count < 50:
            tick_segments["< 50 ticks"].append((outcome, pnl_pips))
        elif tick_count < 100:
            tick_segments["50-100 ticks"].append((outcome, pnl_pips))
        else:
            tick_segments["> 100 ticks"].append((outcome, pnl_pips))

    print("\n🔍 Segmentation par tick_count:")
    for segment_name, segment_trades in tick_segments.items():
        if not segment_trades:
            continue

        count = len(segment_trades)
        wins = sum(1 for outcome, _ in segment_trades if outcome == "WIN")
        win_rate = (wins / count * 100) if count > 0 else 0
        avg_pnl = sum(pnl for _, pnl in segment_trades) / count if count > 0 else 0

        emoji = "🟢" if win_rate >= 60 else "🟡" if win_rate >= 50 else "🔴"
        print(f"  {emoji} {segment_name:15} : {count:>3} trades | Win Rate: {win_rate:>5.1f}% | Avg PnL: {avg_pnl:>+7.1f} pips")

    # Segmentation par coverage_s
    coverage_segments = {
        "< 20s": [],
        "20-40s": [],
        "> 40s": []
    }

    for trade in trades:
        coverage_s = trade.get("coverage_s", 0)
        outcome = trade.get("outcome")
        pnl_pips = trade.get("pnl_pips", 0.0)

        if outcome not in ["WIN", "LOSS"]:
            continue

        if coverage_s < 20:
            coverage_segments["< 20s"].append((outcome, pnl_pips))
        elif coverage_s < 40:
            coverage_segments["20-40s"].append((outcome, pnl_pips))
        else:
            coverage_segments["> 40s"].append((outcome, pnl_pips))

    print("\n🔍 Segmentation par coverage_s:")
    for segment_name, segment_trades in coverage_segments.items():
        if not segment_trades:
            continue

        count = len(segment_trades)
        wins = sum(1 for outcome, _ in segment_trades if outcome == "WIN")
        win_rate = (wins / count * 100) if count > 0 else 0
        avg_pnl = sum(pnl for _, pnl in segment_trades) / count if count > 0 else 0

        emoji = "🟢" if win_rate >= 60 else "🟡" if win_rate >= 50 else "🔴"
        print(f"  {emoji} {segment_name:15} : {count:>3} trades | Win Rate: {win_rate:>5.1f}% | Avg PnL: {avg_pnl:>+7.1f} pips")


def analyze_score_correlation(trades: List[Dict]) -> None:
    """Analyse la corrélation entre score final et PnL réel."""
    print("\n" + "="*80)
    print("🔗 CORRÉLATION SCORE vs PnL RÉEL")
    print("="*80)

    # Regrouper par tranches de score
    score_ranges = {
        "90-99%": [],
        "80-89%": [],
        "70-79%": [],
        "60-69%": [],
        "50-59%": []
    }

    for trade in trades:
        score = trade.get("score_final", 0.0) * 100
        outcome = trade.get("outcome")
        pnl_pips = trade.get("pnl_pips", 0.0)

        if outcome not in ["WIN", "LOSS"]:
            continue

        if score >= 90:
            score_ranges["90-99%"].append((outcome, pnl_pips))
        elif score >= 80:
            score_ranges["80-89%"].append((outcome, pnl_pips))
        elif score >= 70:
            score_ranges["70-79%"].append((outcome, pnl_pips))
        elif score >= 60:
            score_ranges["60-69%"].append((outcome, pnl_pips))
        elif score >= 50:
            score_ranges["50-59%"].append((outcome, pnl_pips))

    for range_name, range_trades in score_ranges.items():
        if not range_trades:
            continue

        count = len(range_trades)
        wins = sum(1 for outcome, _ in range_trades if outcome == "WIN")
        win_rate = (wins / count * 100) if count > 0 else 0
        avg_pnl = sum(pnl for _, pnl in range_trades) / count if count > 0 else 0

        emoji = "🟢" if win_rate >= 65 else "🟡" if win_rate >= 50 else "🔴"
        print(f"  {emoji} Score {range_name:8} : {count:>3} trades | Win Rate: {win_rate:>5.1f}% | Avg PnL: {avg_pnl:>+7.1f} pips")


def generate_recommendations(trades: List[Dict]) -> None:
    """Génère des recommandations d'optimisation basées sur les données."""
    print("\n" + "="*80)
    print("💡 RECOMMANDATIONS D'OPTIMISATION")
    print("="*80)

    # Analyser les pénalités appliquées
    penalized_trades = [t for t in trades if t.get("quality_multiplier", 1.0) < 1.0]
    if penalized_trades:
        penalized_wins = sum(1 for t in penalized_trades if t.get("outcome") == "WIN")
        penalized_win_rate = (penalized_wins / len(penalized_trades) * 100) if penalized_trades else 0

        print(f"\n📉 Pénalités qualité appliquées: {len(penalized_trades)} trades ({penalized_win_rate:.1f}% win rate)")

        if penalized_win_rate > 60:
            print("   ⚠️ ATTENTION: Les trades pénalisés ont un bon win rate !")
            print("   → Recommandation: ASSOUPLIR les pénalités (tick_count, coverage_s)")
        elif penalized_win_rate < 45:
            print("   ✅ Les pénalités semblent justifiées (faible win rate)")
            print("   → Recommandation: MAINTENIR ou RENFORCER les pénalités")

    # Analyser l'impact du trigger
    with_trigger = [t for t in trades if t.get("has_real_trigger", False)]
    without_trigger = [t for t in trades if not t.get("has_real_trigger", False)]

    if with_trigger and without_trigger:
        with_trigger_wins = sum(1 for t in with_trigger if t.get("outcome") == "WIN")
        without_trigger_wins = sum(1 for t in without_trigger if t.get("outcome") == "WIN")

        with_trigger_wr = (with_trigger_wins / len(with_trigger) * 100) if with_trigger else 0
        without_trigger_wr = (without_trigger_wins / len(without_trigger) * 100) if without_trigger else 0

        print(f"\n🎯 Impact du Trigger:")
        print(f"   Avec trigger    : {len(with_trigger):>3} trades | {with_trigger_wr:>5.1f}% win rate")
        print(f"   Sans trigger    : {len(without_trigger):>3} trades | {without_trigger_wr:>5.1f}% win rate")

        if with_trigger_wr > without_trigger_wr + 10:
            print("   ✅ Le trigger AMÉLIORE significativement la sélection (+10%+)")
            print("   → Recommandation: AUGMENTER le bonus trigger")
        elif with_trigger_wr < without_trigger_wr:
            print("   ⚠️ Le trigger DÉGRADE la sélection")
            print("   → Recommandation: RÉDUIRE le poids du trigger ou revoir les patterns")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Analyse des performances des trades")
    parser.add_argument("--min-trades", type=int, default=10, help="Nombre minimum de trades requis")
    parser.add_argument("--log-file", type=str, default="logs/trades_history.jsonl", help="Fichier de log")
    args = parser.parse_args()

    log_file = Path(args.log_file)
    trades = load_trades(log_file)

    print(f"\n📁 Fichier: {log_file}")
    print(f"📊 Trades chargés: {len(trades)}")

    if len(trades) < args.min_trades:
        print(f"\n⚠️ Nombre de trades insuffisant ({len(trades)} < {args.min_trades})")
        print(f"   → Attendre {args.min_trades - len(trades)} trades supplémentaires")
        sys.exit(0)

    # Analyses
    analyze_by_score_category(trades)
    analyze_by_trigger_type(trades)
    analyze_quality_impact(trades)
    analyze_score_correlation(trades)
    generate_recommendations(trades)

    print("\n" + "="*80)
    print("✅ Analyse terminée")
    print("="*80 + "\n")


if __name__ == "__main__":
    main()
