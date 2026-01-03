"""
⚛️ MARKET PHYSICS ANALYZER (PRIORITY_3)
Applique les lois de la physique aux marchés

Impact attendu: +30% détection retournements selon rapport
Raison: "Vous ignorez physique du volume. C'est fondamental."

Source: DEBUG_LOGS.txt lignes 893-957
Date: 03 Janvier 2026
"""

import pandas as pd
import numpy as np
from typing import Dict, Optional, Any


class MarketPhysicsAnalyzer:
    """
    Applique les lois de la physique aux marchés

    Principes:
    1. Conservation de l'énergie (volume = énergie)
    2. Inertie du prix (objet en mouvement reste en mouvement)
    3. Accélération centripète (mouvement circulaire autour d'un point)
    4. Barrières énergétiques (support/resistance)
    5. Entropie du marché (désordre)
    """

    def __init__(self, logger=None):
        """
        Args:
            logger: Logger pour debug
        """
        self.logger = logger

    def apply_physics_principles(
        self,
        ticks_df: Optional[pd.DataFrame],
        candles_df: pd.DataFrame
    ) -> Dict[str, Any]:
        """
        Applique les 5 principes physiques

        Args:
            ticks_df: DataFrame ticks
            candles_df: DataFrame OHLCV

        Returns:
            dict: {
                'energy_conservation': dict,
                'price_inertia': dict,
                'centripetal_acceleration': dict,
                'energy_barriers': dict,
                'market_entropy': dict,
                'physics_bias': str ('BUY' | 'SELL' | 'NEUTRAL')
            }
        """
        if candles_df is None or len(candles_df) < 10:
            return {
                'energy_conservation': {},
                'price_inertia': {},
                'centripetal_acceleration': {},
                'energy_barriers': {},
                'market_entropy': {},
                'physics_bias': 'NEUTRAL'
            }

        # PRINCIPE 1: Conservation de l'énergie (volume = énergie)
        energy_conservation = self._analyze_energy_conservation(ticks_df, candles_df)

        # PRINCIPE 2: Inertie du prix
        price_inertia = self._calculate_price_inertia(candles_df)

        # PRINCIPE 3: Accélération centripète
        centripetal_acceleration = self._detect_centripetal_movement(candles_df)

        # PRINCIPE 4: Résistance et support comme "barrières énergétiques"
        energy_barriers = self._identify_energy_barriers(candles_df)

        # PRINCIPE 5: Équilibre thermodynamique (entropie du marché)
        market_entropy = self._calculate_market_entropy(ticks_df)

        # Déterminer bias global basé sur la physique
        physics_bias = self._derive_physics_bias(energy_conservation, price_inertia, market_entropy)

        return {
            'energy_conservation': energy_conservation,
            'price_inertia': price_inertia,
            'centripetal_acceleration': centripetal_acceleration,
            'energy_barriers': energy_barriers,
            'market_entropy': market_entropy,
            'physics_bias': physics_bias
        }

    def _analyze_energy_conservation(
        self,
        ticks_df: Optional[pd.DataFrame],
        candles_df: pd.DataFrame
    ) -> Dict[str, Any]:
        """
        PRINCIPE 1: Conservation de l'énergie
        Volume = énergie cinétique du marché

        Un mouvement fort DOIT avoir de l'énergie (volume).
        Si prix bouge sans volume = mouvement non durable.

        Args:
            ticks_df: DataFrame ticks
            candles_df: DataFrame OHLCV

        Returns:
            dict: {
                'current_energy': float,
                'average_energy': float,
                'energy_deficit': bool,
                'energy_surplus': bool
            }
        """
        if len(candles_df) < 10:
            return {'current_energy': 0, 'energy_deficit': False}

        # Énergie = Volume × Range
        last_candle = candles_df.iloc[-1]
        last_volume = last_candle.get('volume', 0)
        last_range = last_candle['high'] - last_candle['low']
        current_energy = last_volume * last_range

        # Énergie moyenne sur 10 dernières bougies
        recent_candles = candles_df.tail(10)
        energies = []
        for idx, row in recent_candles.iterrows():
            vol = row.get('volume', 0)
            rng = row['high'] - row['low']
            energies.append(vol * rng)

        average_energy = np.mean(energies) if energies else 0

        # Détection déficit/surplus
        energy_deficit = False
        energy_surplus = False

        if average_energy > 0:
            ratio = current_energy / average_energy

            if ratio < 0.5:  # Énergie < 50% moyenne
                energy_deficit = True
            elif ratio > 2.0:  # Énergie > 200% moyenne
                energy_surplus = True

        return {
            'current_energy': current_energy,
            'average_energy': average_energy,
            'energy_ratio': current_energy / average_energy if average_energy > 0 else 0,
            'energy_deficit': energy_deficit,
            'energy_surplus': energy_surplus
        }

    def _calculate_price_inertia(self, candles_df: pd.DataFrame) -> Dict[str, Any]:
        """
        PRINCIPE 2: Inertie du prix
        Un objet en mouvement tend à rester en mouvement (Newton)

        Momentum = Masse × Vitesse
        Masse = Volume
        Vitesse = Variation de prix

        Args:
            candles_df: DataFrame OHLCV

        Returns:
            dict: {
                'momentum': float,
                'direction': str,
                'inertia_strength': float,
                'likely_to_continue': bool
            }
        """
        if len(candles_df) < 5:
            return {'momentum': 0, 'likely_to_continue': False}

        recent_candles = candles_df.tail(5)

        # Calculer momentum pour chaque bougie
        momentums = []
        for i in range(1, len(recent_candles)):
            prev_row = recent_candles.iloc[i-1]
            curr_row = recent_candles.iloc[i]

            # Vitesse = changement de prix
            velocity = curr_row['close'] - prev_row['close']

            # Masse = volume
            mass = curr_row.get('volume', 1)

            # Momentum = masse × vitesse
            momentum = mass * velocity
            momentums.append(momentum)

        # Momentum moyen récent
        avg_momentum = np.mean(momentums) if momentums else 0

        # Direction
        direction = 'UP' if avg_momentum > 0 else 'DOWN' if avg_momentum < 0 else 'NEUTRAL'

        # Force d'inertie (valeur absolue normalisée)
        inertia_strength = abs(avg_momentum)

        # Cohérence du momentum (tous dans même direction ?)
        if len(momentums) >= 3:
            same_direction = all(m > 0 for m in momentums[-3:]) or all(m < 0 for m in momentums[-3:])
            likely_to_continue = same_direction and inertia_strength > 0
        else:
            likely_to_continue = False

        return {
            'momentum': avg_momentum,
            'direction': direction,
            'inertia_strength': inertia_strength,
            'likely_to_continue': likely_to_continue
        }

    def _detect_centripetal_movement(self, candles_df: pd.DataFrame) -> Dict[str, Any]:
        """
        PRINCIPE 3: Accélération centripète
        Mouvement circulaire autour d'un point (mean reversion)

        Prix s'éloigne trop de la moyenne → force de rappel augmente

        Args:
            candles_df: DataFrame OHLCV

        Returns:
            dict: {
                'mean_price': float,
                'current_distance': float,
                'centripetal_force': float,
                'reversal_likely': bool
            }
        """
        if len(candles_df) < 20:
            return {'reversal_likely': False}

        recent_candles = candles_df.tail(20)

        # Moyenne mobile sur 20 bougies (centre de gravité)
        mean_price = recent_candles['close'].mean()

        # Prix actuel
        current_price = recent_candles.iloc[-1]['close']

        # Distance du centre
        distance = abs(current_price - mean_price)

        # Distance en % du prix
        distance_pct = distance / mean_price if mean_price > 0 else 0

        # Force centripète = f(distance²) (loi inverse du carré)
        centripetal_force = distance_pct ** 2

        # Reversal probable si distance > 0.3% (30 pips)
        reversal_likely = distance_pct > 0.003

        return {
            'mean_price': mean_price,
            'current_price': current_price,
            'distance_pct': distance_pct,
            'centripetal_force': centripetal_force,
            'reversal_likely': reversal_likely
        }

    def _identify_energy_barriers(self, candles_df: pd.DataFrame) -> Dict[str, Any]:
        """
        PRINCIPE 4: Barrières énergétiques
        Support/Resistance = barrières qui nécessitent énergie pour franchir

        Args:
            candles_df: DataFrame OHLCV

        Returns:
            dict: {
                'resistance_level': float,
                'support_level': float,
                'energy_required_up': float,
                'energy_required_down': float
            }
        """
        if len(candles_df) < 20:
            return {}

        recent_candles = candles_df.tail(20)

        # Résistance = max récent
        resistance_level = recent_candles['high'].max()

        # Support = min récent
        support_level = recent_candles['low'].min()

        # Prix actuel
        current_price = recent_candles.iloc[-1]['close']

        # Distance aux barrières (en %)
        if current_price > 0:
            distance_to_resistance = (resistance_level - current_price) / current_price
            distance_to_support = (current_price - support_level) / current_price
        else:
            distance_to_resistance = 0
            distance_to_support = 0

        # Énergie requise = distance × volume moyen récent
        avg_volume = recent_candles['volume'].mean()

        energy_required_up = distance_to_resistance * avg_volume
        energy_required_down = distance_to_support * avg_volume

        return {
            'resistance_level': resistance_level,
            'support_level': support_level,
            'distance_to_resistance_pct': distance_to_resistance,
            'distance_to_support_pct': distance_to_support,
            'energy_required_up': energy_required_up,
            'energy_required_down': energy_required_down
        }

    def _calculate_market_entropy(self, ticks_df: Optional[pd.DataFrame]) -> Dict[str, Any]:
        """
        PRINCIPE 5: Entropie du marché
        Entropie = mesure du désordre

        Marché ordonné: Ticks majoritairement d'un côté
        Marché chaotique: Ticks mélangés buy/sell

        Args:
            ticks_df: DataFrame ticks

        Returns:
            dict: {
                'entropy': float (0-1, 0=ordre parfait, 1=chaos total),
                'market_state': str
            }
        """
        if ticks_df is None or len(ticks_df) < 10:
            return {'entropy': 0.5, 'market_state': 'UNKNOWN'}

        recent_ticks = ticks_df.tail(30)

        # Compter buy vs sell
        buy_count = len(recent_ticks[recent_ticks['side'] == 'buy'])
        sell_count = len(recent_ticks[recent_ticks['side'] == 'sell'])
        total = buy_count + sell_count

        if total == 0:
            return {'entropy': 0.5, 'market_state': 'NO_DATA'}

        # Probabilités
        p_buy = buy_count / total
        p_sell = sell_count / total

        # Entropie de Shannon: H = -Σ(p × log2(p))
        entropy = 0.0
        if p_buy > 0:
            entropy -= p_buy * np.log2(p_buy)
        if p_sell > 0:
            entropy -= p_sell * np.log2(p_sell)

        # Normaliser (entropie max = 1 pour distribution uniforme)
        # Pour 2 événements: H_max = 1
        entropy_normalized = entropy  # Déjà normalisé pour 2 événements

        # État du marché
        if entropy_normalized < 0.5:
            market_state = 'ORDERED'  # Direction claire
        elif entropy_normalized < 0.8:
            market_state = 'SEMI_ORDERED'
        else:
            market_state = 'CHAOTIC'  # Pas de direction

        return {
            'entropy': entropy_normalized,
            'market_state': market_state,
            'buy_ratio': p_buy,
            'sell_ratio': p_sell
        }

    def _derive_physics_bias(
        self,
        energy_conservation: Dict[str, Any],
        price_inertia: Dict[str, Any],
        market_entropy: Dict[str, Any]
    ) -> str:
        """
        Détermine le bias global basé sur les principes physiques

        Args:
            energy_conservation: Résultat analyse énergie
            price_inertia: Résultat analyse inertie
            market_entropy: Résultat analyse entropie

        Returns:
            str: 'BUY' | 'SELL' | 'NEUTRAL'
        """
        bias_points = 0

        # Énergie: surplus = continuation, déficit = stop
        if energy_conservation.get('energy_surplus'):
            # Surplus énergie → mouvement continue
            if price_inertia.get('direction') == 'UP':
                bias_points += 2  # BUY
            elif price_inertia.get('direction') == 'DOWN':
                bias_points -= 2  # SELL

        elif energy_conservation.get('energy_deficit'):
            # Déficit énergie → mouvement s'arrête (inverser inertie)
            if price_inertia.get('direction') == 'UP':
                bias_points -= 1  # Probable reversal → SELL
            elif price_inertia.get('direction') == 'DOWN':
                bias_points += 1  # Probable reversal → BUY

        # Inertie: mouvement cohérent continue
        if price_inertia.get('likely_to_continue'):
            if price_inertia.get('direction') == 'UP':
                bias_points += 1
            elif price_inertia.get('direction') == 'DOWN':
                bias_points -= 1

        # Entropie: ordre = tendance, chaos = range
        if market_entropy.get('market_state') == 'ORDERED':
            # Marché ordonné → suivre la direction
            if market_entropy.get('buy_ratio', 0.5) > 0.6:
                bias_points += 1
            elif market_entropy.get('sell_ratio', 0.5) > 0.6:
                bias_points -= 1

        # Décision finale
        if bias_points >= 2:
            return 'BUY'
        elif bias_points <= -2:
            return 'SELL'
        else:
            return 'NEUTRAL'
