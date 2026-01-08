"""
🧠 PRICE MEMORY ANALYZER (PRIORITY_1)
Analyse la mémoire du prix - Où le prix a-t-il déjà été et qu'a-t-il fait ?

Impact attendu: +20% précision selon rapport
Raison: "Bot trade sans savoir où prix a déjà été. C'est suicidaire."

Source: DEBUG_LOGS.txt lignes 771-813
Date: 03 Janvier 2026
"""

import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Any


class PriceMemoryAnalyzer:
    """
    Les prix se souviennent - L'analyse la plus puissante

    Détecte :
    - Pivots historiques (swing highs/lows)
    - Volume nodes (niveaux où volume s'est concentré)
    - Réactions passées aux niveaux
    - Niveaux frais (jamais testés)
    """

    def __init__(self, logger=None):
        """
        Args:
            logger: Logger pour debug
        """
        self.logger = logger

    def analyze_price_memory(
        self,
        historical_data: pd.DataFrame,
        current_price: float
    ) -> Dict[str, Any]:
        """
        Où le prix a-t-il déjà été ? Et qu'a-t-il fait à ces niveaux ?

        Args:
            historical_data: DataFrame avec colonnes ['time', 'open', 'high', 'low', 'close', 'volume']
            current_price: Prix actuel

        Returns:
            dict: {
                'memory_signals': List[dict] - Signaux mémoire actifs,
                'fresh_levels': List[float] - Niveaux frais,
                'closest_memory': dict - Signal mémoire le plus proche
            }
        """
        if historical_data is None or len(historical_data) < 10:
            return {
                'memory_signals': [],
                'fresh_levels': [],
                'closest_memory': None
            }

        # 1. Points de retournement historiques
        pivot_points = self.find_pivots(historical_data)

        # 2. Niveaux de volume historique (où le volume s'est concentré)
        volume_nodes = self.find_volume_nodes(historical_data)

        # 3. Réactions passées aux niveaux actuels
        memory_signals = []

        for level in pivot_points + volume_nodes:
            distance = abs(current_price - level) / current_price

            if distance < 0.0005:  # 5 pips (0.05%)
                # Voir comment le prix a réagi à ce niveau dans le passé
                past_reactions = self.get_past_reactions(historical_data, level)

                if past_reactions:
                    memory_signals.append({
                        'level': level,
                        'distance_pips': distance * 10000,
                        'past_reactions': past_reactions,
                        'expected_reaction': self.predict_reaction(past_reactions),
                        'confidence': min(0.9, 1 - distance * 1000)
                    })

        # 4. Détection des "niveaux frais" (jamais testés)
        fresh_levels = self.find_fresh_levels(historical_data, current_price)

        return {
            'memory_signals': memory_signals,
            'fresh_levels': fresh_levels,
            'closest_memory': min(memory_signals, key=lambda x: x['distance_pips']) if memory_signals else None
        }

    def find_pivots(self, historical_data: pd.DataFrame, window: int = 3) -> List[float]:
        """
        Trouve les pivots (swing highs/lows) dans l'historique

        Args:
            historical_data: DataFrame OHLCV
            window: Fenêtre pour détection pivots (3 = regarde 1 bougie avant/après)

        Returns:
            List[float]: Liste des prix pivots
        """
        if len(historical_data) < window * 2 + 1:
            return []

        pivots = []

        # Swing highs
        for i in range(window, len(historical_data) - window):
            current_high = historical_data.iloc[i]['high']

            # Vérifier si c'est le plus haut dans la fenêtre
            is_swing_high = True
            for j in range(i - window, i + window + 1):
                if j != i and historical_data.iloc[j]['high'] >= current_high:
                    is_swing_high = False
                    break

            if is_swing_high:
                pivots.append(current_high)

        # Swing lows
        for i in range(window, len(historical_data) - window):
            current_low = historical_data.iloc[i]['low']

            # Vérifier si c'est le plus bas dans la fenêtre
            is_swing_low = True
            for j in range(i - window, i + window + 1):
                if j != i and historical_data.iloc[j]['low'] <= current_low:
                    is_swing_low = False
                    break

            if is_swing_low:
                pivots.append(current_low)

        # Dédupliquer et trier
        pivots = sorted(list(set(pivots)))

        return pivots

    def find_volume_nodes(self, historical_data: pd.DataFrame, num_bins: int = 20) -> List[float]:
        """
        Trouve les niveaux où le volume s'est concentré (Volume Profile)

        Args:
            historical_data: DataFrame OHLCV
            num_bins: Nombre de bins pour l'histogramme de prix

        Returns:
            List[float]: Niveaux de prix avec volume concentré (top 30%)
        """
        if len(historical_data) < 10:
            return []

        # Créer bins de prix
        price_range_min = historical_data['low'].min()
        price_range_max = historical_data['high'].max()

        if price_range_max <= price_range_min:
            return []

        bins = np.linspace(price_range_min, price_range_max, num_bins + 1)

        # Calculer volume par bin
        volume_by_bin = {}

        for idx, row in historical_data.iterrows():
            # Pour chaque bougie, distribuer le volume sur son range
            candle_low = row['low']
            candle_high = row['high']
            candle_volume = row.get('volume', 0)

            # Trouver bins touchés par cette bougie
            for i in range(len(bins) - 1):
                bin_low = bins[i]
                bin_high = bins[i + 1]
                bin_mid = (bin_low + bin_high) / 2

                # Si la bougie touche ce bin
                if not (candle_high < bin_low or candle_low > bin_high):
                    # Ajouter volume proportionnel
                    if bin_mid not in volume_by_bin:
                        volume_by_bin[bin_mid] = 0
                    volume_by_bin[bin_mid] += candle_volume

        if not volume_by_bin:
            return []

        # Trouver top 30% des niveaux par volume
        sorted_bins = sorted(volume_by_bin.items(), key=lambda x: x[1], reverse=True)
        num_top = max(1, int(len(sorted_bins) * 0.3))
        top_bins = sorted_bins[:num_top]

        # Retourner les niveaux de prix (triés)
        volume_nodes = sorted([price for price, vol in top_bins])

        return volume_nodes

    def get_past_reactions(
        self,
        historical_data: pd.DataFrame,
        level: float,
        tolerance_pips: float = 5.0
    ) -> List[Dict[str, Any]]:
        """
        Récupère les réactions passées du prix à un niveau donné

        Args:
            historical_data: DataFrame OHLCV
            level: Niveau de prix à tester
            tolerance_pips: Tolérance en pips (5 pips par défaut)

        Returns:
            List[dict]: Liste des réactions {
                'time': timestamp,
                'reaction_type': 'bounce' | 'break',
                'strength': 0.0-1.0
            }
        """
        tolerance = tolerance_pips / 10000  # Convertir pips en prix
        reactions = []

        for i in range(1, len(historical_data)):
            prev_row = historical_data.iloc[i - 1]
            curr_row = historical_data.iloc[i]

            prev_close = prev_row['close']
            curr_open = curr_row['open']
            curr_close = curr_row['close']
            curr_high = curr_row['high']
            curr_low = curr_row['low']

            # Le prix a touché le niveau ?
            level_touched = (curr_low <= level + tolerance and curr_high >= level - tolerance)

            if level_touched:
                # Déterminer type de réaction
                reaction_type = None
                strength = 0.0

                # BOUNCE : Prix touche le niveau et repart
                if prev_close < level and curr_low <= level + tolerance and curr_close > level:
                    reaction_type = 'bounce_up'
                    strength = abs(curr_close - level) / abs(curr_high - curr_low) if (curr_high - curr_low) > 0 else 0.5

                elif prev_close > level and curr_high >= level - tolerance and curr_close < level:
                    reaction_type = 'bounce_down'
                    strength = abs(level - curr_close) / abs(curr_high - curr_low) if (curr_high - curr_low) > 0 else 0.5

                # BREAK : Prix traverse le niveau
                elif prev_close < level and curr_close > level:
                    reaction_type = 'break_up'
                    strength = abs(curr_close - level) / abs(curr_high - curr_low) if (curr_high - curr_low) > 0 else 0.5

                elif prev_close > level and curr_close < level:
                    reaction_type = 'break_down'
                    strength = abs(level - curr_close) / abs(curr_high - curr_low) if (curr_high - curr_low) > 0 else 0.5

                if reaction_type:
                    reactions.append({
                        'time': curr_row.get('time', i),
                        'reaction_type': reaction_type,
                        'strength': min(1.0, strength)
                    })

        return reactions

    def predict_reaction(self, past_reactions: List[Dict[str, Any]]) -> Optional[str]:
        """
        Prédire la prochaine réaction basée sur l'historique

        Args:
            past_reactions: Liste des réactions passées

        Returns:
            str: 'bounce' | 'break' | None
        """
        if not past_reactions:
            return None

        # Compter bounces vs breaks
        bounces = sum(1 for r in past_reactions if 'bounce' in r['reaction_type'])
        breaks = sum(1 for r in past_reactions if 'break' in r['reaction_type'])

        # Si 70%+ bounces → expect bounce
        total = bounces + breaks
        if total == 0:
            return None

        bounce_ratio = bounces / total

        if bounce_ratio >= 0.7:
            return 'bounce'
        elif bounce_ratio <= 0.3:
            return 'break'
        else:
            return 'uncertain'

    def find_fresh_levels(
        self,
        historical_data: pd.DataFrame,
        current_price: float,
        lookback: int = 20
    ) -> List[float]:
        """
        Trouve les niveaux "frais" (jamais testés dans le lookback)

        Args:
            historical_data: DataFrame OHLCV
            current_price: Prix actuel
            lookback: Nombre de bougies à regarder en arrière

        Returns:
            List[float]: Niveaux frais proches du prix actuel
        """
        if len(historical_data) < lookback:
            lookback = len(historical_data)

        recent_data = historical_data.tail(lookback)

        # Trouver tous les pivots dans l'historique complet
        all_pivots = self.find_pivots(historical_data, window=3)

        # Vérifier quels pivots n'ont PAS été retouchés récemment
        fresh_levels = []

        for pivot in all_pivots:
            # Le pivot est-il proche du prix actuel ? (< 20 pips)
            if abs(current_price - pivot) / current_price > 0.002:  # Plus de 20 pips
                continue

            # Le pivot a-t-il été retouché dans le lookback ?
            was_retouched = False
            tolerance = 5.0 / 10000  # 5 pips

            for idx, row in recent_data.iterrows():
                if row['low'] <= pivot + tolerance and row['high'] >= pivot - tolerance:
                    was_retouched = True
                    break

            if not was_retouched:
                fresh_levels.append(pivot)

        return sorted(fresh_levels)

    def analyze_trend_structure(
        self,
        historical_data: pd.DataFrame,
        current_price: float
    ) -> Dict[str, Any]:
        """
        🆕 08 JAN 2026: Analyse la structure de tendance basée sur les pivots historiques

        Détecte:
        - Higher Highs + Higher Lows = TENDANCE HAUSSIÈRE
        - Lower Highs + Lower Lows = TENDANCE BAISSIÈRE
        - Structure mixte = RANGE
        - Déplacement net du prix (malgré oscillations)

        Args:
            historical_data: DataFrame OHLCV (minimum 20 bougies recommandé)
            current_price: Prix actuel

        Returns:
            dict: {
                'trend_direction': 'BULLISH' | 'BEARISH' | 'RANGE',
                'trend_strength': 0.0-1.0,
                'structure': {
                    'higher_highs': bool,
                    'higher_lows': bool,
                    'lower_highs': bool,
                    'lower_lows': bool
                },
                'net_displacement': {
                    'net_pips': float,
                    'net_direction': 'UP' | 'DOWN' | 'FLAT',
                    'avg_pips_per_candle': float,
                    'trend_clarity': 0.0-1.0
                },
                'last_pivots': {
                    'highs': List[float],
                    'lows': List[float]
                }
            }
        """
        if historical_data is None or len(historical_data) < 10:
            return self._default_trend_structure()

        try:
            # 1. Trouver tous les pivots (swing highs/lows)
            all_pivots = self.find_pivots(historical_data, window=3)

            if len(all_pivots) < 4:
                return self._default_trend_structure()

            # 2. Séparer en highs et lows
            swing_highs = []
            swing_lows = []

            for i in range(3, len(historical_data) - 3):
                current_high = historical_data.iloc[i]['high']
                current_low = historical_data.iloc[i]['low']

                # Swing high check
                is_swing_high = True
                for j in range(i - 3, i + 4):
                    if j != i and historical_data.iloc[j]['high'] >= current_high:
                        is_swing_high = False
                        break
                if is_swing_high:
                    swing_highs.append(current_high)

                # Swing low check
                is_swing_low = True
                for j in range(i - 3, i + 4):
                    if j != i and historical_data.iloc[j]['low'] <= current_low:
                        is_swing_low = False
                        break
                if is_swing_low:
                    swing_lows.append(current_low)

            # Garder les 3-5 derniers pivots de chaque type
            recent_highs = sorted(swing_highs)[-5:] if swing_highs else []
            recent_lows = sorted(swing_lows)[-5:] if swing_lows else []

            # 3. Analyser la structure (Higher Highs, Lower Lows, etc.)
            structure = self._analyze_pivot_structure(recent_highs, recent_lows)

            # 4. Analyser le déplacement net
            net_displacement = self._analyze_net_displacement(historical_data, current_price)

            # 5. Déterminer la tendance finale
            trend_direction, trend_strength = self._determine_trend(structure, net_displacement)

            result = {
                'trend_direction': trend_direction,
                'trend_strength': round(trend_strength, 2),
                'structure': structure,
                'net_displacement': net_displacement,
                'last_pivots': {
                    'highs': [round(h, 5) for h in recent_highs[-3:]] if len(recent_highs) >= 3 else recent_highs,
                    'lows': [round(l, 5) for l in recent_lows[-3:]] if len(recent_lows) >= 3 else recent_lows
                }
            }

            if self.logger:
                self.logger.debug(
                    f"[PRICE_MEMORY_TREND] {trend_direction} (strength={trend_strength:.2f}) | "
                    f"Net: {net_displacement['net_direction']} {net_displacement['net_pips']:+.1f} pips | "
                    f"Clarity: {net_displacement['trend_clarity']:.2f}"
                )

            return result

        except Exception as e:
            if self.logger:
                self.logger.error(f"[PRICE_MEMORY_TREND] Erreur analyse: {e}", exc_info=True)
            return self._default_trend_structure()

    def _analyze_pivot_structure(
        self,
        recent_highs: list,
        recent_lows: list
    ) -> Dict[str, bool]:
        """
        Analyse si les pivots forment une structure haussière ou baissière

        Returns:
            {
                'higher_highs': bool,
                'higher_lows': bool,
                'lower_highs': bool,
                'lower_lows': bool
            }
        """
        structure = {
            'higher_highs': False,
            'higher_lows': False,
            'lower_highs': False,
            'lower_lows': False
        }

        # Besoin d'au moins 2 pivots de chaque type
        if len(recent_highs) < 2 or len(recent_lows) < 2:
            return structure

        # Analyser les highs (prendre les 3 derniers)
        if len(recent_highs) >= 3:
            highs = recent_highs[-3:]
            # Higher highs: chaque high est plus haut que le précédent
            if highs[-1] > highs[-2] and highs[-2] > highs[-3]:
                structure['higher_highs'] = True
            # Lower highs: chaque high est plus bas que le précédent
            elif highs[-1] < highs[-2] and highs[-2] < highs[-3]:
                structure['lower_highs'] = True
        elif len(recent_highs) == 2:
            if recent_highs[-1] > recent_highs[-2]:
                structure['higher_highs'] = True
            else:
                structure['lower_highs'] = True

        # Analyser les lows (prendre les 3 derniers)
        if len(recent_lows) >= 3:
            lows = recent_lows[-3:]
            # Higher lows: chaque low est plus haut que le précédent
            if lows[-1] > lows[-2] and lows[-2] > lows[-3]:
                structure['higher_lows'] = True
            # Lower lows: chaque low est plus bas que le précédent
            elif lows[-1] < lows[-2] and lows[-2] < lows[-3]:
                structure['lower_lows'] = True
        elif len(recent_lows) == 2:
            if recent_lows[-1] > recent_lows[-2]:
                structure['higher_lows'] = True
            else:
                structure['lower_lows'] = True

        return structure

    def _analyze_net_displacement(
        self,
        historical_data: pd.DataFrame,
        current_price: float
    ) -> Dict[str, Any]:
        """
        Analyse le déplacement NET du prix (ignorant les oscillations)

        Répond à: "Malgré les bougies vertes/rouges alternées,
                   où le prix est-il VRAIMENT allé?"

        Returns:
            {
                'net_pips': float,
                'net_direction': 'UP' | 'DOWN' | 'FLAT',
                'avg_pips_per_candle': float,
                'trend_clarity': 0.0-1.0,
                'price_path': dict
            }
        """
        if len(historical_data) < 5:
            return {
                'net_pips': 0.0,
                'net_direction': 'FLAT',
                'avg_pips_per_candle': 0.0,
                'trend_clarity': 0.0,
                'price_path': {}
            }

        # Prix de départ (première bougie)
        start_price = historical_data.iloc[0]['close']

        # Prix actuel
        end_price = current_price

        # Plus haut/bas dans la période
        highest = historical_data['high'].max()
        lowest = historical_data['low'].min()

        # DÉPLACEMENT NET (clé!)
        net_displacement = end_price - start_price

        # Déterminer le nombre de décimales (5 pour forex majeur, 3 pour JPY)
        if abs(start_price) > 100:  # Probablement JPY
            pip_multiplier = 100  # 157.50 → 15750 pips
        else:
            pip_multiplier = 10000  # 1.0500 → 10500 pips

        net_pips = net_displacement * pip_multiplier

        # Direction nette
        if abs(net_pips) < 3:
            net_direction = "FLAT"
        elif net_pips > 0:
            net_direction = "UP"
        else:
            net_direction = "DOWN"

        # Moyenne de déplacement par bougie
        num_candles = len(historical_data)
        avg_pips_per_candle = net_pips / num_candles if num_candles > 0 else 0.0

        # CLARTÉ DE LA TENDANCE
        # Compare le déplacement net vs le range total
        total_range = (highest - lowest) * pip_multiplier
        if total_range > 0:
            trend_clarity = abs(net_pips) / total_range
            # 1.0 = déplacement net = tout le range (tendance pure)
            # 0.0 = déplacement net = 0 (range pur)
        else:
            trend_clarity = 0.0

        return {
            'net_pips': round(net_pips, 1),
            'net_direction': net_direction,
            'avg_pips_per_candle': round(avg_pips_per_candle, 2),
            'trend_clarity': round(min(1.0, trend_clarity), 2),
            'price_path': {
                'start': round(start_price, 5),
                'current': round(end_price, 5),
                'highest': round(highest, 5),
                'lowest': round(lowest, 5),
                'total_range_pips': round(total_range, 1)
            }
        }

    def _determine_trend(
        self,
        structure: Dict[str, bool],
        net_displacement: Dict[str, Any]
    ) -> tuple:
        """
        Détermine la tendance finale et sa force

        Combine:
        - Structure des pivots (Higher Highs, etc.)
        - Déplacement net du prix

        Returns:
            (trend_direction, trend_strength)
        """
        # Extraire les infos
        higher_highs = structure['higher_highs']
        higher_lows = structure['higher_lows']
        lower_highs = structure['lower_highs']
        lower_lows = structure['lower_lows']

        net_direction = net_displacement['net_direction']
        net_pips = abs(net_displacement['net_pips'])
        clarity = net_displacement['trend_clarity']

        # LOGIQUE DE DÉCISION

        # CAS 1: TENDANCE HAUSSIÈRE CLAIRE
        if higher_highs and higher_lows and net_direction == "UP":
            # Structure + déplacement alignés = tendance forte
            strength = min(1.0, 0.7 + (clarity * 0.3))  # 0.7-1.0
            return "BULLISH", strength

        # CAS 2: TENDANCE BAISSIÈRE CLAIRE
        if lower_highs and lower_lows and net_direction == "DOWN":
            # Structure + déplacement alignés = tendance forte
            strength = min(1.0, 0.7 + (clarity * 0.3))  # 0.7-1.0
            return "BEARISH", strength

        # CAS 3: Structure haussière mais déplacement faible/neutre
        if higher_highs and higher_lows:
            if net_pips > 10:
                return "BULLISH", 0.6  # Modéré
            else:
                return "RANGE", 0.4  # Structure sans mouvement = range

        # CAS 4: Structure baissière mais déplacement faible/neutre
        if lower_highs and lower_lows:
            if net_pips > 10:
                return "BEARISH", 0.6  # Modéré
            else:
                return "RANGE", 0.4

        # CAS 5: Déplacement net significatif SANS structure claire
        if net_pips > 20:
            if net_direction == "UP":
                return "BULLISH", 0.5  # Faible (pas de confirmation structure)
            elif net_direction == "DOWN":
                return "BEARISH", 0.5

        # CAS 6: Structure mixte (higher highs + lower lows, etc.)
        if (higher_highs and lower_lows) or (lower_highs and higher_lows):
            return "RANGE", 0.3  # Choppy

        # CAS 7: Déplacement net faible + pas de structure
        if net_pips < 10:
            return "RANGE", 0.2  # Flat

        # DEFAULT: Range avec force selon clarté
        return "RANGE", round(clarity * 0.5, 2)

    def _default_trend_structure(self) -> Dict[str, Any]:
        """
        Retourne une structure par défaut quand l'analyse n'est pas possible
        """
        return {
            'trend_direction': 'RANGE',
            'trend_strength': 0.0,
            'structure': {
                'higher_highs': False,
                'higher_lows': False,
                'lower_highs': False,
                'lower_lows': False
            },
            'net_displacement': {
                'net_pips': 0.0,
                'net_direction': 'FLAT',
                'avg_pips_per_candle': 0.0,
                'trend_clarity': 0.0,
                'price_path': {}
            },
            'last_pivots': {
                'highs': [],
                'lows': []
            }
        }
