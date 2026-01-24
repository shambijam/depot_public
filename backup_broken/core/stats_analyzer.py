#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
stats_analyzer.py - Analyseur de Statistiques de Trading

Analyse les trades depuis trades_history.jsonl pour fournir des statistiques
complètes au dashboard : win rate, R/R, profits par symbole, évolution capital, etc.

Créé le: 04 Janvier 2026
"""

import json
import logging
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Any
from collections import defaultdict

logger = logging.getLogger(__name__)


class TradesStatsAnalyzer:
    """
    Analyseur de statistiques de trading en temps réel.

    Lit trades_history.jsonl et calcule toutes les métriques nécessaires
    pour le dashboard : win rate, R/R, plus gros gain/perte, profit par symbole,
    évolution du capital, performance horaire.

    Implémente un cache intelligent basé sur le timestamp du fichier pour
    optimiser les performances.
    """

    def __init__(self, trades_file: str = "logs/trades_history.jsonl"):
        """
        Initialise l'analyseur.

        Args:
            trades_file: Chemin vers le fichier JSON Lines des trades
        """
        self.trades_file = Path(trades_file)
        self._cache: Optional[Dict] = None
        self._last_modified: Optional[float] = None

        logger.info(f"TradesStatsAnalyzer initialisé: {self.trades_file}")

    def load_trades(self, days: Optional[int] = None) -> List[Dict]:
        """
        Charge les trades depuis le fichier JSONL.

        Args:
            days: Si spécifié, charge uniquement les N derniers jours

        Returns:
            Liste de dictionnaires représentant les trades
        """
        if not self.trades_file.exists():
            logger.warning(f"Fichier {self.trades_file} non trouvé")
            return []

        if self.trades_file.stat().st_size == 0:
            logger.warning(f"Fichier {self.trades_file} vide")
            return []

        trades = []
        cutoff_date = None

        if days is not None:
            cutoff_date = datetime.now(timezone.utc) - timedelta(days=days)

        try:
            with open(self.trades_file, 'r', encoding='utf-8') as f:
                for line_num, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue

                    try:
                        trade = json.loads(line)

                        # Filtrer uniquement trades complétés
                        if trade.get('outcome') not in ['WIN', 'LOSS', 'BE']:
                            continue

                        # Filtrer par date si nécessaire
                        if cutoff_date and 'exit_time' in trade:
                            try:
                                exit_time = datetime.fromisoformat(trade['exit_time'].replace('Z', '+00:00'))
                                if exit_time < cutoff_date:
                                    continue
                            except (ValueError, AttributeError):
                                pass

                        trades.append(trade)

                    except json.JSONDecodeError as e:
                        logger.warning(f"Ligne {line_num} invalide: {e}")
                        continue

            logger.info(f"Chargé {len(trades)} trades depuis {self.trades_file}")
            return trades

        except Exception as e:
            logger.error(f"Erreur lecture fichier {self.trades_file}: {e}")
            return []

    def calculate_global_stats(self, trades: List[Dict]) -> Dict:
        """
        Calcule les statistiques globales.

        Args:
            trades: Liste des trades

        Returns:
            Dictionnaire avec win_rate, avg_rr, biggest_win/loss, etc.
        """
        if not trades:
            return self._get_empty_global_stats()

        total_trades = len(trades)
        wins = sum(1 for t in trades if t.get('outcome') == 'WIN')
        losses = sum(1 for t in trades if t.get('outcome') == 'LOSS')
        be = sum(1 for t in trades if t.get('outcome') == 'BE')

        # Win rate (excluant BE)
        tradable_count = wins + losses
        win_rate = (wins / tradable_count) if tradable_count > 0 else 0.0

        # Ratio Risk/Reward moyen
        rr_values = [float(t.get('risk_reward_ratio', 0)) for t in trades if t.get('risk_reward_ratio')]
        avg_rr = (sum(rr_values) / len(rr_values)) if rr_values else 0.0

        # Plus gros gain et perte
        pnl_pips_values = [float(t.get('pnl_pips', 0)) for t in trades]
        pnl_usd_values = [float(t.get('pnl_usd', 0)) for t in trades]

        biggest_win_pips = max(pnl_pips_values) if pnl_pips_values else 0.0
        biggest_loss_pips = min(pnl_pips_values) if pnl_pips_values else 0.0

        # Trouver les USD correspondants
        biggest_win_trade = max(trades, key=lambda t: float(t.get('pnl_pips', 0))) if trades else {}
        biggest_loss_trade = min(trades, key=lambda t: float(t.get('pnl_pips', 0))) if trades else {}

        biggest_win_usd = float(biggest_win_trade.get('pnl_usd', 0))
        biggest_loss_usd = float(biggest_loss_trade.get('pnl_usd', 0))

        # Total PnL
        total_pnl_usd = sum(pnl_usd_values)
        avg_pnl_usd = total_pnl_usd / total_trades if total_trades > 0 else 0.0

        # Durée moyenne
        durations = [float(t.get('duration_minutes', 0)) for t in trades if t.get('duration_minutes')]
        avg_duration_minutes = (sum(durations) / len(durations)) if durations else 0.0

        return {
            'total_trades': total_trades,
            'wins': wins,
            'losses': losses,
            'be': be,
            'win_rate': win_rate,
            'avg_rr': avg_rr,
            'biggest_win_pips': biggest_win_pips,
            'biggest_win_usd': biggest_win_usd,
            'biggest_loss_pips': biggest_loss_pips,
            'biggest_loss_usd': biggest_loss_usd,
            'total_pnl_usd': total_pnl_usd,
            'avg_pnl_usd': avg_pnl_usd,
            'avg_duration_minutes': avg_duration_minutes
        }

    def calculate_profit_by_symbol(self, trades: List[Dict]) -> Dict[str, Dict]:
        """
        Calcule le profit par symbole.

        Args:
            trades: Liste des trades

        Returns:
            Dict avec stats par symbole: {
                'EURUSD': {'trades': N, 'win_rate': X, 'total_pnl_usd': Y, ...}
            }
        """
        if not trades:
            return {}

        by_symbol = defaultdict(lambda: {
            'trades': [],
            'wins': 0,
            'losses': 0,
            'total_pnl_usd': 0.0,
            'total_pnl_pips': 0.0
        })

        # Grouper par symbole
        for trade in trades:
            symbol = trade.get('symbol', 'UNKNOWN')
            outcome = trade.get('outcome')
            pnl_usd = float(trade.get('pnl_usd', 0))
            pnl_pips = float(trade.get('pnl_pips', 0))

            by_symbol[symbol]['trades'].append(trade)
            by_symbol[symbol]['total_pnl_usd'] += pnl_usd
            by_symbol[symbol]['total_pnl_pips'] += pnl_pips

            if outcome == 'WIN':
                by_symbol[symbol]['wins'] += 1
            elif outcome == 'LOSS':
                by_symbol[symbol]['losses'] += 1

        # Calculer stats finales
        result = {}
        for symbol, data in by_symbol.items():
            trade_count = len(data['trades'])
            wins = data['wins']
            losses = data['losses']
            tradable = wins + losses

            result[symbol] = {
                'trades': trade_count,
                'win_rate': (wins / tradable) if tradable > 0 else 0.0,
                'total_pnl_usd': data['total_pnl_usd'],
                'avg_pnl_pips': (data['total_pnl_pips'] / trade_count) if trade_count > 0 else 0.0,
                'wins': wins,
                'losses': losses
            }

        return result

    def calculate_equity_evolution(self, trades: List[Dict], days: int = 7, initial_balance: float = 0.0) -> Dict:
        """
        Calcule l'évolution du capital sur N jours.

        Args:
            trades: Liste des trades
            days: Nombre de jours à afficher
            initial_balance: Balance initiale (optionnel, sinon PnL cumulé depuis 0)

        Returns:
            Dict avec dates, equity, cumulative_pnl
        """
        if not trades:
            return {'dates': [], 'equity': [], 'cumulative_pnl': []}

        # Grouper trades par date
        by_date = defaultdict(list)

        for trade in trades:
            exit_time_str = trade.get('exit_time')
            if not exit_time_str:
                continue

            try:
                exit_time = datetime.fromisoformat(exit_time_str.replace('Z', '+00:00'))
                date_key = exit_time.date()
                by_date[date_key].append(trade)
            except (ValueError, AttributeError) as e:
                logger.warning(f"Impossible de parser exit_time: {exit_time_str}")
                continue

        # Trier les dates
        sorted_dates = sorted(by_date.keys())

        # Limiter aux N derniers jours
        if len(sorted_dates) > days:
            cutoff_date = sorted_dates[-days]
            sorted_dates = [d for d in sorted_dates if d >= cutoff_date]

        # Calculer PnL cumulé
        dates = []
        equity = []
        cumulative_pnl = []

        current_equity = initial_balance
        current_pnl = 0.0

        for date in sorted_dates:
            daily_trades = by_date[date]
            daily_pnl = sum(float(t.get('pnl_usd', 0)) for t in daily_trades)

            current_equity += daily_pnl
            current_pnl += daily_pnl

            dates.append(date.strftime('%Y-%m-%d'))
            equity.append(round(current_equity, 2))
            cumulative_pnl.append(round(current_pnl, 2))

        return {
            'dates': dates,
            'equity': equity,
            'cumulative_pnl': cumulative_pnl
        }

    def calculate_hourly_performance(self, trades: List[Dict]) -> Dict[int, Dict]:
        """
        Calcule la performance par heure de la journée (0-23).

        Args:
            trades: Liste des trades

        Returns:
            Dict: {0: {'trades': N, 'win_rate': X, 'avg_pnl': Y}, ...}
        """
        if not trades:
            return {}

        by_hour = defaultdict(lambda: {
            'trades': [],
            'wins': 0,
            'losses': 0,
            'total_pnl_pips': 0.0
        })

        for trade in trades:
            entry_time_str = trade.get('entry_time')
            if not entry_time_str:
                continue

            try:
                entry_time = datetime.fromisoformat(entry_time_str.replace('Z', '+00:00'))
                hour = entry_time.hour

                by_hour[hour]['trades'].append(trade)
                by_hour[hour]['total_pnl_pips'] += float(trade.get('pnl_pips', 0))

                outcome = trade.get('outcome')
                if outcome == 'WIN':
                    by_hour[hour]['wins'] += 1
                elif outcome == 'LOSS':
                    by_hour[hour]['losses'] += 1

            except (ValueError, AttributeError):
                continue

        # Calculer stats finales
        result = {}
        for hour, data in by_hour.items():
            trade_count = len(data['trades'])
            wins = data['wins']
            losses = data['losses']
            tradable = wins + losses

            result[hour] = {
                'trades': trade_count,
                'win_rate': (wins / tradable) if tradable > 0 else 0.0,
                'avg_pnl': (data['total_pnl_pips'] / trade_count) if trade_count > 0 else 0.0,
                'wins': wins,
                'losses': losses
            }

        return result

    def get_all_stats(self, days_equity: int = 7, days_filter: Optional[int] = None) -> Dict:
        """
        Retourne toutes les statistiques en une seule fois.

        Utilise le cache si le fichier n'a pas été modifié.

        Args:
            days_equity: Nombre de jours pour le graphique d'évolution
            days_filter: Si spécifié, filtre uniquement les N derniers jours

        Returns:
            Dict complet avec toutes les stats
        """
        # Vérifier si refresh nécessaire
        if not self._needs_refresh():
            if self._cache:
                return self._cache

        # Charger les trades
        trades = self.load_trades(days=days_filter)

        if not trades:
            return self._get_empty_stats()

        # Calculer toutes les stats
        stats = {
            'global': self.calculate_global_stats(trades),
            'by_symbol': self.calculate_profit_by_symbol(trades),
            'equity_evolution': self.calculate_equity_evolution(trades, days=days_equity),
            'hourly_performance': self.calculate_hourly_performance(trades),
            'has_data': True,
            'last_updated': datetime.now(timezone.utc).isoformat()
        }

        # Mettre à jour le cache
        self._cache = stats

        return stats

    def _needs_refresh(self) -> bool:
        """Vérifie si le fichier a été modifié depuis le dernier chargement."""
        if not self.trades_file.exists():
            return False

        try:
            current_mtime = self.trades_file.stat().st_mtime

            if self._last_modified is None or current_mtime > self._last_modified:
                self._last_modified = current_mtime
                return True

            return False

        except Exception as e:
            logger.warning(f"Erreur vérification mtime: {e}")
            return True

    def _get_empty_stats(self) -> Dict:
        """Retourne une structure vide si aucun trade."""
        return {
            'global': self._get_empty_global_stats(),
            'by_symbol': {},
            'equity_evolution': {'dates': [], 'equity': [], 'cumulative_pnl': []},
            'hourly_performance': {},
            'has_data': False,
            'last_updated': datetime.now(timezone.utc).isoformat()
        }

    def _get_empty_global_stats(self) -> Dict:
        """Retourne des stats globales vides."""
        return {
            'total_trades': 0,
            'wins': 0,
            'losses': 0,
            'be': 0,
            'win_rate': 0.0,
            'avg_rr': 0.0,
            'biggest_win_pips': 0.0,
            'biggest_win_usd': 0.0,
            'biggest_loss_pips': 0.0,
            'biggest_loss_usd': 0.0,
            'total_pnl_usd': 0.0,
            'avg_pnl_usd': 0.0,
            'avg_duration_minutes': 0.0
        }


# Test standalone
if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)

    analyzer = TradesStatsAnalyzer()
    stats = analyzer.get_all_stats(days_equity=7)

    print(json.dumps(stats, indent=2))
