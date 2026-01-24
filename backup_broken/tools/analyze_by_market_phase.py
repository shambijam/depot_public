#!/usr/bin/env python3
"""
Analyse des performances de trading par phase de marché

Ce script analyse le fichier trades_history.jsonl pour identifier:
- Les phases de marché les plus profitables
- Les phases où le scoring est optimal
- Les régimes VWAP les plus performants
- Statistiques WIN/LOSS par phase

Usage:
    python tools/analyze_by_market_phase.py

Output:
    - Rapport Markdown: logs/performance_by_phase.md
    - Statistiques console

Date: 06 DEC 2025
Objectif: Optimiser le scoring adaptatif selon les phases de marché
"""

import json
import logging
from pathlib import Path
from collections import defaultdict
from typing import Dict, List, Any
from datetime import datetime

logging.basicConfig(level=logging.INFO, format='%(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class MarketPhaseAnalyzer:
    """Analyseur de performance par phase de marché"""

    def __init__(self, trades_file: str = "logs/trades_history.jsonl"):
        self.trades_file = Path(trades_file)
        self.trades: List[Dict[str, Any]] = []
        self.stats_by_vwap_regime: Dict[str, Dict] = defaultdict(lambda: {
            "trades": [],
            "wins": 0,
            "losses": 0,
            "be": 0,
            "total_pnl_pips": 0.0,
            "total_pnl_usd": 0.0,
            "avg_score": 0.0,
            "scores": [],
        })
        self.stats_by_market_regime: Dict[str, Dict] = defaultdict(lambda: {
            "trades": [],
            "wins": 0,
            "losses": 0,
            "be": 0,
            "total_pnl_pips": 0.0,
            "total_pnl_usd": 0.0,
            "avg_score": 0.0,
            "scores": [],
        })
        self.stats_by_market_phase: Dict[str, Dict] = defaultdict(lambda: {
            "trades": [],
            "wins": 0,
            "losses": 0,
            "be": 0,
            "total_pnl_pips": 0.0,
            "total_pnl_usd": 0.0,
            "avg_score": 0.0,
            "scores": [],
        })

    def load_trades(self) -> int:
        """Charge les trades depuis le fichier JSONL"""
        if not self.trades_file.exists():
            logger.error(f"❌ Fichier {self.trades_file} introuvable")
            return 0

        count = 0
        with open(self.trades_file, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    trade = json.loads(line)
                    # Ne garder que les trades avec outcome (terminés)
                    if trade.get("outcome"):
                        self.trades.append(trade)
                        count += 1
                except json.JSONDecodeError as e:
                    logger.warning(f"⚠️ Ligne JSON invalide: {e}")

        logger.info(f"✅ {count} trades chargés depuis {self.trades_file}")
        return count

    def analyze(self):
        """Analyse les trades par phase de marché"""
        for trade in self.trades:
            outcome = trade.get("outcome", "UNKNOWN")
            pnl_pips = float(trade.get("pnl_pips", 0.0))
            pnl_usd = float(trade.get("pnl_usd", 0.0))
            score = float(trade.get("score_final", 0.0))

            # === Analyse par VWAP Regime ===
            vwap_regime = trade.get("vwap_regime")
            if vwap_regime:
                stats = self.stats_by_vwap_regime[vwap_regime]
                stats["trades"].append(trade)
                stats["scores"].append(score)
                stats["total_pnl_pips"] += pnl_pips
                stats["total_pnl_usd"] += pnl_usd

                if outcome == "WIN":
                    stats["wins"] += 1
                elif outcome == "LOSS":
                    stats["losses"] += 1
                elif outcome == "BE":
                    stats["be"] += 1

            # === Analyse par Market Regime ===
            market_regime = trade.get("market_regime")
            if market_regime:
                stats = self.stats_by_market_regime[market_regime]
                stats["trades"].append(trade)
                stats["scores"].append(score)
                stats["total_pnl_pips"] += pnl_pips
                stats["total_pnl_usd"] += pnl_usd

                if outcome == "WIN":
                    stats["wins"] += 1
                elif outcome == "LOSS":
                    stats["losses"] += 1
                elif outcome == "BE":
                    stats["be"] += 1

            # === Analyse par Market Phase ===
            market_phase = trade.get("market_phase")
            if market_phase:
                stats = self.stats_by_market_phase[market_phase]
                stats["trades"].append(trade)
                stats["scores"].append(score)
                stats["total_pnl_pips"] += pnl_pips
                stats["total_pnl_usd"] += pnl_usd

                if outcome == "WIN":
                    stats["wins"] += 1
                elif outcome == "LOSS":
                    stats["losses"] += 1
                elif outcome == "BE":
                    stats["be"] += 1

        # Calculer moyennes
        for stats_dict in [self.stats_by_vwap_regime, self.stats_by_market_regime, self.stats_by_market_phase]:
            for stats in stats_dict.values():
                if stats["scores"]:
                    stats["avg_score"] = sum(stats["scores"]) / len(stats["scores"])
                total_trades = stats["wins"] + stats["losses"] + stats["be"]
                stats["total_trades"] = total_trades
                stats["win_rate"] = stats["wins"] / total_trades if total_trades > 0 else 0.0
                stats["avg_pnl_pips"] = stats["total_pnl_pips"] / total_trades if total_trades > 0 else 0.0
                stats["avg_pnl_usd"] = stats["total_pnl_usd"] / total_trades if total_trades > 0 else 0.0

    def generate_report(self, output_file: str = "logs/performance_by_phase.md"):
        """Génère un rapport Markdown"""
        output_path = Path(output_file)
        output_path.parent.mkdir(exist_ok=True)

        with open(output_path, 'w', encoding='utf-8') as f:
            f.write("# 📊 Analyse de Performance par Phase de Marché\n\n")
            f.write(f"*Généré le {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*\n\n")
            f.write(f"**Total trades analysés:** {len(self.trades)}\n\n")
            f.write("---\n\n")

            # === Régimes VWAP ===
            f.write("## 📈 Performance par Régime VWAP\n\n")
            if self.stats_by_vwap_regime:
                f.write("| Régime VWAP | Trades | Win Rate | PnL Moy (pips) | PnL Moy (USD) | Score Moy | W/L/BE |\n")
                f.write("|-------------|--------|----------|----------------|---------------|-----------|--------|\n")

                # Trier par win_rate décroissant
                sorted_vwap = sorted(self.stats_by_vwap_regime.items(), key=lambda x: x[1]["win_rate"], reverse=True)

                for regime, stats in sorted_vwap:
                    emoji = {"TRENDING": "📈", "ACCUMULATION": "📊", "BALANCED": "⚖️", "TRANSITIONAL": "🔄"}.get(regime, "❓")
                    f.write(f"| {emoji} **{regime}** | {stats['total_trades']} | "
                           f"{stats['win_rate']:.1%} | "
                           f"{stats['avg_pnl_pips']:+.1f} | "
                           f"{stats['avg_pnl_usd']:+.2f} | "
                           f"{stats['avg_score']:.1%} | "
                           f"{stats['wins']}/{stats['losses']}/{stats['be']} |\n")
                f.write("\n")
            else:
                f.write("*Aucune donnée de régime VWAP disponible*\n\n")

            # === Régimes PhaseObserver ===
            f.write("## 🎯 Performance par Régime PhaseObserver\n\n")
            if self.stats_by_market_regime:
                f.write("| Régime PhaseObserver | Trades | Win Rate | PnL Moy (pips) | PnL Moy (USD) | Score Moy | W/L/BE |\n")
                f.write("|----------------------|--------|----------|----------------|---------------|-----------|--------|\n")

                sorted_regime = sorted(self.stats_by_market_regime.items(), key=lambda x: x[1]["win_rate"], reverse=True)

                for regime, stats in sorted_regime:
                    f.write(f"| **{regime}** | {stats['total_trades']} | "
                           f"{stats['win_rate']:.1%} | "
                           f"{stats['avg_pnl_pips']:+.1f} | "
                           f"{stats['avg_pnl_usd']:+.2f} | "
                           f"{stats['avg_score']:.1%} | "
                           f"{stats['wins']}/{stats['losses']}/{stats['be']} |\n")
                f.write("\n")
            else:
                f.write("*Aucune donnée de régime PhaseObserver disponible*\n\n")

            # === Phases Optimisées ===
            f.write("## 🚀 Performance par Phase Optimisée\n\n")
            if self.stats_by_market_phase:
                f.write("| Phase Optimisée | Trades | Win Rate | PnL Moy (pips) | PnL Moy (USD) | Score Moy | W/L/BE |\n")
                f.write("|-----------------|--------|----------|----------------|---------------|-----------|--------|\n")

                sorted_phase = sorted(self.stats_by_market_phase.items(), key=lambda x: x[1]["win_rate"], reverse=True)

                for phase, stats in sorted_phase:
                    f.write(f"| **{phase}** | {stats['total_trades']} | "
                           f"{stats['win_rate']:.1%} | "
                           f"{stats['avg_pnl_pips']:+.1f} | "
                           f"{stats['avg_pnl_usd']:+.2f} | "
                           f"{stats['avg_score']:.1%} | "
                           f"{stats['wins']}/{stats['losses']}/{stats['be']} |\n")
                f.write("\n")
            else:
                f.write("*Aucune donnée de phase optimisée disponible*\n\n")

            # === Recommandations ===
            f.write("## 💡 Recommandations\n\n")
            self._write_recommendations(f)

            f.write("---\n\n")
            f.write("*Rapport généré par analyze_by_market_phase.py*\n")

        logger.info(f"✅ Rapport généré: {output_path}")

    def _write_recommendations(self, f):
        """Écrit les recommandations basées sur l'analyse"""
        # Meilleur régime VWAP
        if self.stats_by_vwap_regime:
            best_vwap = max(self.stats_by_vwap_regime.items(), key=lambda x: x[1]["win_rate"])
            worst_vwap = min(self.stats_by_vwap_regime.items(), key=lambda x: x[1]["win_rate"])

            f.write(f"### 📈 Régimes VWAP\n\n")
            f.write(f"- ✅ **Meilleur régime**: {best_vwap[0]} (Win Rate: {best_vwap[1]['win_rate']:.1%}, {best_vwap[1]['total_trades']} trades)\n")
            f.write(f"- ❌ **Pire régime**: {worst_vwap[0]} (Win Rate: {worst_vwap[1]['win_rate']:.1%}, {worst_vwap[1]['total_trades']} trades)\n\n")

            if best_vwap[1]["win_rate"] > 0.60:
                f.write(f"💡 **Action suggérée**: Augmenter le poids VWAP en régime {best_vwap[0]}\n\n")
            if worst_vwap[1]["win_rate"] < 0.40:
                f.write(f"⚠️ **Action suggérée**: Réduire le poids VWAP en régime {worst_vwap[0]} ou filtrer les trades\n\n")

        # Meilleur régime PhaseObserver
        if self.stats_by_market_regime:
            best_regime = max(self.stats_by_market_regime.items(), key=lambda x: x[1]["win_rate"])
            f.write(f"### 🎯 Régimes PhaseObserver\n\n")
            f.write(f"- ✅ **Meilleur régime**: {best_regime[0]} (Win Rate: {best_regime[1]['win_rate']:.1%})\n\n")

    def print_summary(self):
        """Affiche un résumé dans la console"""
        print("\n" + "="*80)
        print("📊 ANALYSE DE PERFORMANCE PAR PHASE DE MARCHÉ")
        print("="*80 + "\n")

        print(f"Total trades analysés: {len(self.trades)}\n")

        if self.stats_by_vwap_regime:
            print("📈 RÉGIMES VWAP (triés par Win Rate):")
            print("-" * 80)
            sorted_vwap = sorted(self.stats_by_vwap_regime.items(), key=lambda x: x[1]["win_rate"], reverse=True)
            for regime, stats in sorted_vwap:
                print(f"  {regime:20s} | Trades: {stats['total_trades']:3d} | "
                      f"Win Rate: {stats['win_rate']:5.1%} | "
                      f"PnL Moy: {stats['avg_pnl_pips']:+6.1f} pips | "
                      f"Score: {stats['avg_score']:5.1%}")
            print()

        print("="*80 + "\n")


def main():
    """Point d'entrée principal"""
    analyzer = MarketPhaseAnalyzer()

    trades_count = analyzer.load_trades()
    if trades_count == 0:
        logger.warning("⚠️ Aucun trade à analyser")
        return

    analyzer.analyze()
    analyzer.print_summary()
    analyzer.generate_report()

    logger.info("✅ Analyse terminée!")


if __name__ == "__main__":
    main()
