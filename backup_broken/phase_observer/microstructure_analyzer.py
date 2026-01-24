"""
🔬 MICROSTRUCTURE ANALYZER (Phase 2)
Analyse la microstructure du flux d'ordres - Niveau institutionnel

Détecte:
- Vitesse du ruban (tape speed)
- Déséquilibre par niveau de prix
- Allumage du momentum (momentum ignition)

Source: DEBUG_LOGS.txt lignes 22-116
Date: 03 Janvier 2026
"""

import pandas as pd
import numpy as np
from typing import Dict, List, Tuple, Optional, Any


class MicrostructureAnalyzer:
    """
    Analyse la microstructure du flux d'ordres - Niveau institutionnel

    Mesure:
    - Tape speed: vitesse réaction buy vs sell
    - Order imbalance: déséquilibre à chaque niveau de prix
    - Momentum ignition: détection début séries directionnelles
    """

    def __init__(self, logger=None):
        """
        Args:
            logger: Logger pour debug
        """
        self.logger = logger

    def analyze_tape_speed(self, ticks_df: pd.DataFrame) -> Dict[str, Any]:
        """
        Vitesse du ruban - Mesure combien de temps les ordres restent au bid/ask

        Args:
            ticks_df: DataFrame ticks avec colonnes ['time', 'side']

        Returns:
            dict: {
                'tape_speed_buy': float (ticks/seconde),
                'tape_speed_sell': float (ticks/seconde),
                'speed_ratio': float (sell_interval / buy_interval),
                'interpretation': str
            }
        """
        if ticks_df is None or len(ticks_df) < 10:
            return {
                'tape_speed_buy': 0,
                'tape_speed_sell': 0,
                'speed_ratio': 1.0,
                'interpretation': 'PAS_ASSEZ_DONNEES'
            }

        # Séparer buy et sell ticks
        buy_ticks = ticks_df[ticks_df['side'] == 'buy'].copy()
        sell_ticks = ticks_df[ticks_df['side'] == 'sell'].copy()

        if len(buy_ticks) < 2 or len(sell_ticks) < 2:
            return {
                'tape_speed_buy': 0,
                'tape_speed_sell': 0,
                'speed_ratio': 1.0,
                'interpretation': 'PAS_ASSEZ_DONNEES'
            }

        # Convertir timestamps
        if 'time' in buy_ticks.columns:
            buy_times = pd.to_datetime(buy_ticks['time'])
            sell_times = pd.to_datetime(sell_ticks['time'])
        else:
            # Fallback: utiliser index comme proxy temporel
            return {
                'tape_speed_buy': 0,
                'tape_speed_sell': 0,
                'speed_ratio': 1.0,
                'interpretation': 'TIMESTAMP_MANQUANT'
            }

        # Temps moyen entre les ticks du même côté
        buy_intervals = buy_times.diff().dt.total_seconds()
        sell_intervals = sell_times.diff().dt.total_seconds()

        # Moyennes (en ignorant NaN)
        avg_buy_interval = buy_intervals.mean()
        avg_sell_interval = sell_intervals.mean()

        # Éviter division par zéro
        if pd.isna(avg_buy_interval) or avg_buy_interval <= 0:
            avg_buy_interval = 1.0
        if pd.isna(avg_sell_interval) or avg_sell_interval <= 0:
            avg_sell_interval = 1.0

        # Vitesse = 1/intervalle (ticks par seconde)
        tape_speed_buy = 1.0 / avg_buy_interval
        tape_speed_sell = 1.0 / avg_sell_interval

        # Ratio de vitesse : qui réagit plus vite ?
        speed_ratio = avg_sell_interval / avg_buy_interval

        # Interprétation
        interpretation = self._interpret_speed_ratio(speed_ratio)

        return {
            'tape_speed_buy': tape_speed_buy,
            'tape_speed_sell': tape_speed_sell,
            'speed_ratio': speed_ratio,
            'avg_buy_interval_seconds': avg_buy_interval,
            'avg_sell_interval_seconds': avg_sell_interval,
            'interpretation': interpretation
        }

    def _interpret_speed_ratio(self, speed_ratio: float) -> str:
        """
        Interprète le ratio de vitesse

        Args:
            speed_ratio: sell_interval / buy_interval

        Returns:
            str: Interprétation
        """
        if speed_ratio > 1.5:
            # Sell interval > buy interval → buyers plus actifs/rapides
            return 'BUYERS_AGGRESSIVE'
        elif speed_ratio > 1.2:
            return 'BUYERS_MODERATE'
        elif speed_ratio < 0.67:
            # Buy interval > sell interval → sellers plus actifs/rapides
            return 'SELLERS_AGGRESSIVE'
        elif speed_ratio < 0.83:
            return 'SELLERS_MODERATE'
        else:
            return 'BALANCED'

    def analyze_order_imbalance_at_price(
        self,
        ticks_df: pd.DataFrame,
        current_bid: float,
        current_ask: float
    ) -> Tuple[Dict[float, Dict[str, Any]], Dict[str, Any]]:
        """
        Déséquilibre à chaque niveau de prix (comme dans une vraie salle)

        Args:
            ticks_df: DataFrame ticks avec colonnes ['price', 'side', 'volume']
            current_bid: Bid actuel
            current_ask: Ask actuel

        Returns:
            Tuple[Dict, Dict]:
                - imbalance_by_level: {price: {'imbalance', 'buy_volume', 'sell_volume', 'distance_from_bidask'}}
                - hot_spots: {'strong_buy_wall', 'strong_sell_wall', 'closest_imbalance'}
        """
        if ticks_df is None or len(ticks_df) < 10:
            return {}, {}

        if 'price' not in ticks_df.columns:
            return {}, {}

        # Copier pour ne pas modifier l'original
        ticks_copy = ticks_df.copy()

        # Regrouper par niveau de prix (0.1 pip de précision = 1 point)
        ticks_copy['price_level'] = (ticks_copy['price'] * 10000).round() / 10000

        imbalance_by_level = {}

        for level, group in ticks_copy.groupby('price_level'):
            buy_volume = group[group['side'] == 'buy']['volume'].sum()
            sell_volume = group[group['side'] == 'sell']['volume'].sum()

            # Déséquilibre en pourcentage
            total = buy_volume + sell_volume
            if total > 0:
                imbalance = (buy_volume - sell_volume) / total
            else:
                imbalance = 0

            # Distance du bid/ask
            distance_from_bid = abs(level - current_bid) if current_bid > 0 else 0
            distance_from_ask = abs(level - current_ask) if current_ask > 0 else 0
            distance_from_bidask = min(distance_from_bid, distance_from_ask)

            imbalance_by_level[level] = {
                'imbalance': imbalance,
                'buy_volume': buy_volume,
                'sell_volume': sell_volume,
                'distance_from_bidask': distance_from_bidask
            }

        # Trouver les points chauds
        hot_spots = {}

        if imbalance_by_level:
            # Mur d'achat le plus fort (imbalance > 0)
            buy_walls = [(k, v) for k, v in imbalance_by_level.items() if v['imbalance'] > 0]
            if buy_walls:
                strong_buy_wall = max(buy_walls, key=lambda x: x[1]['imbalance'])
                hot_spots['strong_buy_wall'] = strong_buy_wall
            else:
                hot_spots['strong_buy_wall'] = (None, None)

            # Mur de vente le plus fort (imbalance < 0)
            sell_walls = [(k, v) for k, v in imbalance_by_level.items() if v['imbalance'] < 0]
            if sell_walls:
                strong_sell_wall = min(sell_walls, key=lambda x: x[1]['imbalance'])
                hot_spots['strong_sell_wall'] = strong_sell_wall
            else:
                hot_spots['strong_sell_wall'] = (None, None)

            # Déséquilibre le plus proche du bid/ask (< 2 pips)
            close_imbalances = [(k, v) for k, v in imbalance_by_level.items()
                                if v['distance_from_bidask'] < 0.0002]  # 2 pips

            if close_imbalances:
                closest_imbalance = min(close_imbalances, key=lambda x: x[1]['distance_from_bidask'])
                hot_spots['closest_imbalance'] = closest_imbalance
            else:
                hot_spots['closest_imbalance'] = (None, None)
        else:
            hot_spots = {
                'strong_buy_wall': (None, None),
                'strong_sell_wall': (None, None),
                'closest_imbalance': (None, None)
            }

        return imbalance_by_level, hot_spots

    def calculate_tick_acceleration_zscore(
        self,
        current_tick_rate: float,
        tick_rate_history: list
    ) -> Dict[str, Any]:
        """
        Calcule le Z-score de l'accélération des ticks.

        Args:
            current_tick_rate: Taux actuel (ticks/seconde)
            tick_rate_history: Historique des taux (30+ valeurs recommandées)

        Returns:
            {
                'zscore': float,
                'interpretation': str (BRUIT/LEGER/MODERE/FORT/EXTREME),
                'bonus': int (0/5/10/15/20)
            }
        """
        if not tick_rate_history or len(tick_rate_history) < 10:
            return {
                'zscore': 0.0,
                'interpretation': 'PAS_ASSEZ_DONNEES',
                'bonus': 0
            }

        # Calcul Z-score: (x - moyenne) / écart-type
        history_array = np.array(tick_rate_history)
        mean = np.mean(history_array)
        std = np.std(history_array)

        # Éviter division par zéro
        if std <= 0:
            return {
                'zscore': 0.0,
                'interpretation': 'BRUIT',
                'bonus': 0
            }

        zscore = (current_tick_rate - mean) / std

        # Table de bonus selon Z-score
        # | Z-SCORE | INTERPRÉTATION          | BONUS |
        # |---------|-------------------------|-------|
        # | < 1.0   | Bruit                   | 0 pts |
        # | 1.0-2.0 | Léger                   | 5 pts |
        # | 2.0-3.0 | Modéré (significatif)   | 10 pts|
        # | 3.0-4.0 | Fort                    | 15 pts|
        # | > 4.0   | Extrême                 | 20 pts|
        if zscore < 1.0:
            interpretation = 'BRUIT'
            bonus = 0
        elif zscore < 2.0:
            interpretation = 'LEGER'
            bonus = 5
        elif zscore < 3.0:
            interpretation = 'MODERE'
            bonus = 10
        elif zscore < 4.0:
            interpretation = 'FORT'
            bonus = 15
        else:
            interpretation = 'EXTREME'
            bonus = 20

        return {
            'zscore': float(zscore),
            'interpretation': interpretation,
            'bonus': bonus
        }

    def detect_momentum_ignition(self, ticks_df: pd.DataFrame) -> List[Dict[str, Any]]:
        """
        Détecte l'allumage du momentum (début d'une série directionnelle)

        Pattern: 3 ticks consécutifs du même côté avec volume croissant

        Args:
            ticks_df: DataFrame ticks avec colonnes ['side', 'volume', 'price', 'time']

        Returns:
            List[dict]: Liste des signaux d'ignition {
                'side': 'buy' | 'sell',
                'strength': float (somme volumes),
                'acceleration': float (ratio accélération prix),
                'time': timestamp
            }
        """
        if ticks_df is None or len(ticks_df) < 3:
            return []

        ignition_signals = []

        # Cherche 3 ticks consécutifs du même côté avec volume croissant
        for i in range(len(ticks_df) - 2):
            window = ticks_df.iloc[i:i+3]

            # Tous du même côté ?
            sides = window['side'].unique()
            if len(sides) != 1:
                continue

            side = sides[0]

            # Volume croissant ?
            volumes = window['volume'].values
            if not (len(volumes) == 3 and volumes[0] < volumes[1] < volumes[2]):
                continue

            # Prix accélère ?
            if 'price' not in window.columns or len(window) < 3:
                continue

            prices = window['price'].values
            price_changes = np.abs(np.diff(prices))

            if len(price_changes) >= 2 and price_changes[0] > 0:
                acceleration = price_changes[1] / price_changes[0]

                # Accélération significative (> 1.1)
                if acceleration > 1.1:
                    signal = {
                        'side': side,
                        'strength': float(np.sum(volumes)),
                        'acceleration': float(acceleration),
                        'time': window.iloc[-1].get('time', i+2) if 'time' in window.columns else i+2
                    }
                    ignition_signals.append(signal)

        return ignition_signals
