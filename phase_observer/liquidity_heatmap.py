"""
💧 LIQUIDITY HEATMAP (Phase 2)
Carte de chaleur de liquidité en temps réel - Comme les pros

Détecte:
- Pression buy/sell en temps réel
- Liquidity grabs (stop hunts institutionnels)
- Durée et profondeur de la pression

Source: DEBUG_LOGS.txt lignes 118-211
Date: 03 Janvier 2026
"""

import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Any


class LiquidityHeatmap:
    """
    Carte de chaleur de liquidité en temps réel - Comme les pros

    Mesure:
    - Pressure ratio: force acheteurs vs vendeurs
    - Liquidity grabs: stop hunts institutionnels
    - Durée pression: combien de temps ça dure
    """

    def __init__(self, depth_levels: int = 10, logger=None):
        """
        Args:
            depth_levels: Nombre de niveaux de profondeur (pas utilisé pour l'instant)
            logger: Logger pour debug
        """
        self.depth_levels = depth_levels
        self.liquidity_snapshots = []
        self.logger = logger

    def calculate_pressure_ratio(
        self,
        ticks_df: pd.DataFrame,
        window_seconds: float = 5.0
    ) -> Dict[str, Any]:
        """
        Ratio de pression : Force des acheteurs vs vendeurs

        Args:
            ticks_df: DataFrame ticks avec colonnes ['time', 'side', 'volume']
            window_seconds: Fenêtre temporelle en secondes (défaut: 5s)

        Returns:
            dict: {
                'raw_pressure': float (buy_vol - sell_vol),
                'normalized_pressure': float (-1 à +1),
                'direction': str,
                'duration_seconds': float,
                'confidence': float (0-1)
            }
        """
        if ticks_df is None or len(ticks_df) < 5:
            return {
                'raw_pressure': 0,
                'normalized_pressure': 0,
                'direction': 'BALANCED',
                'duration_seconds': 0,
                'confidence': 0
            }

        # Récupérer ticks récents dans la fenêtre
        recent_ticks = self._get_recent_ticks(ticks_df, window_seconds)

        if len(recent_ticks) < 2:
            return {
                'raw_pressure': 0,
                'normalized_pressure': 0,
                'direction': 'BALANCED',
                'duration_seconds': 0,
                'confidence': 0
            }

        # Volume normalisé par tick
        buy_volume = recent_ticks[recent_ticks['side'] == 'buy']['volume'].sum()
        sell_volume = recent_ticks[recent_ticks['side'] == 'sell']['volume'].sum()

        # Pression brute
        raw_pressure = buy_volume - sell_volume

        # Pression normalisée (0-1)
        total_volume = buy_volume + sell_volume
        if total_volume > 0:
            normalized_pressure = raw_pressure / total_volume
        else:
            normalized_pressure = 0

        # Direction de la pression
        if normalized_pressure > 0.3:
            pressure_direction = "STRONG_BUY_PRESSURE"
        elif normalized_pressure > 0.1:
            pressure_direction = "MODERATE_BUY_PRESSURE"
        elif normalized_pressure < -0.3:
            pressure_direction = "STRONG_SELL_PRESSURE"
        elif normalized_pressure < -0.1:
            pressure_direction = "MODERATE_SELL_PRESSURE"
        else:
            pressure_direction = "BALANCED"

        # Profondeur de la pression (combien de temps ça dure)
        pressure_duration = self._calculate_pressure_duration(recent_ticks)

        # Confiance basée sur l'intensité et la durée
        confidence = min(1.0, abs(normalized_pressure) * 2 + pressure_duration / 10)

        return {
            'raw_pressure': raw_pressure,
            'normalized_pressure': normalized_pressure,
            'direction': pressure_direction,
            'duration_seconds': pressure_duration,
            'confidence': confidence,
            'buy_volume': buy_volume,
            'sell_volume': sell_volume
        }

    def _get_recent_ticks(
        self,
        ticks_df: pd.DataFrame,
        window_seconds: float
    ) -> pd.DataFrame:
        """
        Récupère les ticks dans la fenêtre temporelle

        Args:
            ticks_df: DataFrame ticks
            window_seconds: Fenêtre en secondes

        Returns:
            DataFrame: Ticks récents
        """
        if ticks_df is None or len(ticks_df) == 0:
            return pd.DataFrame()

        if 'time' not in ticks_df.columns:
            # Fallback: retourner les N derniers ticks (approximation)
            n_ticks = max(5, int(window_seconds * 2))  # Assume ~2 ticks/sec
            return ticks_df.tail(n_ticks)

        # Filtrer par timestamp
        ticks_copy = ticks_df.copy()
        ticks_copy['time'] = pd.to_datetime(ticks_copy['time'])

        # Timestamp le plus récent
        last_time = ticks_copy['time'].max()

        # Fenêtre temporelle
        cutoff_time = last_time - pd.Timedelta(seconds=window_seconds)

        # Filtrer
        recent = ticks_copy[ticks_copy['time'] >= cutoff_time]

        return recent

    def _calculate_pressure_duration(self, recent_ticks: pd.DataFrame) -> float:
        """
        Calcule combien de temps la pression dure (en secondes)

        Args:
            recent_ticks: DataFrame ticks récents

        Returns:
            float: Durée en secondes
        """
        if len(recent_ticks) < 2:
            return 0.0

        if 'time' not in recent_ticks.columns:
            # Fallback: nombre de ticks comme proxy
            return float(len(recent_ticks) * 0.5)  # Assume 0.5s par tick

        # Calculer durée réelle
        times = pd.to_datetime(recent_ticks['time'])
        duration = (times.max() - times.min()).total_seconds()

        return duration

    def detect_liquidity_grab(
        self,
        ticks_df: pd.DataFrame,
        price_data: Optional[pd.DataFrame] = None
    ) -> List[Dict[str, Any]]:
        """
        Détecte les "grabs" de liquidité (stop hunts institutionnels)

        Pattern: Rapide mouvement vers un niveau puis retour

        Args:
            ticks_df: DataFrame ticks
            price_data: DataFrame OHLCV (optionnel, pas utilisé pour l'instant)

        Returns:
            List[dict]: Liste des signaux grab {
                'type': 'BEARISH_LIQUIDITY_GRAB' | 'BULLISH_LIQUIDITY_GRAB',
                'level': float,
                'confidence': float,
                'reason': str
            }
        """
        if ticks_df is None or len(ticks_df) < 20:
            return []

        # Pattern: Rapide mouvement vers un niveau puis retour
        recent_ticks = ticks_df.tail(50)

        if 'price' not in recent_ticks.columns:
            return []

        price_extremes = {
            'high': recent_ticks['price'].max(),
            'low': recent_ticks['price'].min(),
            'current': recent_ticks['price'].iloc[-1]
        }

        grab_signals = []

        # Check for liquidity above (stop buys grabbed)
        # Si prix a touché un haut puis est redescendu de 2+ pips
        if price_extremes['current'] < price_extremes['high'] * 0.9998:  # 2 pips en dessous du haut
            # Regarder le volume pendant le mouvement vers le haut
            upward_ticks = recent_ticks[recent_ticks['price'] > price_extremes['current']]

            if len(upward_ticks) > 0:
                # Ratio vente/achat pendant la montée
                total_upward = len(upward_ticks)
                sell_upward = len(upward_ticks[upward_ticks['side'] == 'sell'])
                sell_ratio = sell_upward / total_upward if total_upward > 0 else 0

                # Si 70%+ de vente pendant la montée = absorption par vendeurs
                if sell_ratio > 0.7:
                    grab_signals.append({
                        'type': 'BEARISH_LIQUIDITY_GRAB',
                        'level': price_extremes['high'],
                        'confidence': 0.8,
                        'reason': 'Stop buys liquidés, absorption par vendeurs',
                        'sell_ratio_during_move': sell_ratio
                    })

        # Check for liquidity below (stop sells grabbed)
        # Si prix a touché un bas puis est remonté de 2+ pips
        if price_extremes['current'] > price_extremes['low'] * 1.0002:  # 2 pips au dessus du bas
            # Regarder le volume pendant le mouvement vers le bas
            downward_ticks = recent_ticks[recent_ticks['price'] < price_extremes['current']]

            if len(downward_ticks) > 0:
                # Ratio achat/vente pendant la descente
                total_downward = len(downward_ticks)
                buy_downward = len(downward_ticks[downward_ticks['side'] == 'buy'])
                buy_ratio = buy_downward / total_downward if total_downward > 0 else 0

                # Si 70%+ d'achat pendant la descente = absorption par acheteurs
                if buy_ratio > 0.7:
                    grab_signals.append({
                        'type': 'BULLISH_LIQUIDITY_GRAB',
                        'level': price_extremes['low'],
                        'confidence': 0.8,
                        'reason': 'Stop sells liquidés, absorption par acheteurs',
                        'buy_ratio_during_move': buy_ratio
                    })

        return grab_signals
