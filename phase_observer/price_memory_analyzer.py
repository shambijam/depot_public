"""
🧠 PRICE MEMORY ANALYZER (PRIORITY_1)
Analyse la mémoire du prix - Où le prix a-t-il déjà été et qu'a-t-il fait ?

Impact attendu: +20% précision selon rapport
Raison: "Bot trade sans savoir où prix a déjà été. C'est suicidaire."

Source: DEBUG_LOGS.txt lignes 771-813
Date: 03 Janvier 2026

🆕 16 JAN 2026: Ajout MTF Analysis + Mémoire Persistante
- Analyse constante M15 + M5 + M1
- Historique des analyses (mémoire persistante)
- get_mtf_trend_verdict() pour verdict BEARISH/BULLISH
- Logique asymétrique: MTF = direction pour BEARISH, delta = timing uniquement
"""

import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Any
from collections import deque
from dataclasses import dataclass
from datetime import datetime


@dataclass
class MTFAnalysisResult:
    """Résultat d'analyse d'un timeframe"""
    timeframe: str  # M15, M5, M1
    direction: str  # BEARISH, BULLISH, NEUTRAL
    net_pips: float
    trend_clarity: float  # 0.0-1.0
    candles_analyzed: int
    timestamp: datetime


@dataclass
class MTFTrendVerdict:
    """Verdict MTF pour décision de trade"""
    direction: str  # BEARISH, BULLISH, NEUTRAL
    alignment: str  # "4/4", "3/4", "2/4", "1/4", "0/4"
    alignment_count: int  # 0, 1, 2, 3, 4
    bonus: float  # +30, +20, +10, +5, 0
    m30_direction: str
    m15_direction: str
    m5_direction: str
    m1_direction: str
    confidence: float  # 0.0-1.0
    should_override_delta: bool  # True si MTF doit ignorer le delta
    details: Dict[str, Any]


class PriceMemoryAnalyzer:
    """
    Les prix se souviennent - L'analyse la plus puissante

    Détecte :
    - Pivots historiques (swing highs/lows)
    - Volume nodes (niveaux où volume s'est concentré)
    - Réactions passées aux niveaux
    - Niveaux frais (jamais testés)

    🆕 16 JAN 2026: MTF Analysis
    - Analyse constante M15 + M5 + M1
    - Mémoire persistante des analyses
    - Verdict MTF pour trades BEARISH
    """

    def __init__(self, logger=None):
        """
        Args:
            logger: Logger pour debug
        """
        self.logger = logger

        # 🆕 16 JAN 2026: Mémoire persistante par asset et timeframe
        self._mtf_history: Dict[str, Dict[str, deque]] = {}
        self._history_maxlen = 100  # Garder les 100 dernières analyses par TF

        # Cache du dernier verdict par asset
        self._last_verdict: Dict[str, MTFTrendVerdict] = {}

        if self.logger:
            self.logger.info("✅ [PRICE_MEMORY] Initialisé avec MTF Analysis + Mémoire Persistante")

    def _get_point_size(self, asset: str) -> float:
        """
        Retourne la taille du point pour un asset.
        Utilisé pour convertir le mouvement de prix en pips.
        """
        asset_upper = asset.upper() if asset else ""
        # Métaux précieux
        if "XAG" in asset_upper:
            return 0.001  # 3 décimales pour XAGUSD (1 point = 0.001)
        elif "XAU" in asset_upper:
            return 0.01  # 2 décimales pour XAUUSD
        # Indices
        elif "US30" in asset_upper or "DOW" in asset_upper:
            return 0.01
        elif "SP500" in asset_upper or "US500" in asset_upper:
            return 0.01
        # Forex JPY pairs
        elif "JPY" in asset_upper:
            return 0.001  # 3 décimales
        # Forex standard (5 décimales)
        else:
            return 0.00001

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

    def detect_micro_resistance_m1(
        self,
        historical_data: pd.DataFrame,
        current_price: float,
        lookback_minutes: int = 15
    ) -> Dict[str, Any]:
        """
        🆕 14 JAN 2026: SCALPING - Détecte micro-résistances M1 (< 1 pip)

        Optimisé pour burst scalping:
        - Lookback ultra-court (15 minutes M1 = 15 bougies)
        - Distance en ticks (pas en pips)
        - Bounce probability basée sur réactions récentes

        Args:
            historical_data: DataFrame M1 (minimum 15-20 bougies)
            current_price: Prix actuel
            lookback_minutes: Minutes à regarder en arrière (défaut: 15)

        Returns:
            dict: {
                'micro_resistance': float | None - Prix du dernier high M1
                'distance_ticks': float - Écart en ticks
                'distance_pips': float - Écart en pips
                'bounce_probability': float (0-1) - % de rebonds
                'age_minutes': int - Ancienneté du niveau
                'rejection_detected': bool - Wick de rejet présent
                'strength': str - 'STRONG' | 'MODERATE' | 'WEAK'
            }
        """
        if historical_data is None or len(historical_data) < 10:
            return {
                'micro_resistance': None,
                'distance_ticks': 0.0,
                'distance_pips': 0.0,
                'bounce_probability': 0.0,
                'age_minutes': 0,
                'rejection_detected': False,
                'strength': 'NONE'
            }

        # Prendre seulement les N dernières minutes (lookback)
        recent_data = historical_data.tail(lookback_minutes)

        if len(recent_data) < 5:
            return {
                'micro_resistance': None,
                'distance_ticks': 0.0,
                'distance_pips': 0.0,
                'bounce_probability': 0.0,
                'age_minutes': 0,
                'rejection_detected': False,
                'strength': 'NONE'
            }

        # 1. Trouver le dernier swing high M1 (fenêtre réduite = 2)
        window = 2
        last_micro_resistance = None
        resistance_index = None

        for i in range(len(recent_data) - 1, window - 1, -1):
            if i < window or i >= len(recent_data) - window:
                continue

            current_high = recent_data.iloc[i]['high']

            # Vérifier si c'est un swing high local
            is_swing_high = True
            for j in range(i - window, i + window + 1):
                if j != i and j >= 0 and j < len(recent_data):
                    if recent_data.iloc[j]['high'] >= current_high:
                        is_swing_high = False
                        break

            if is_swing_high and current_high > current_price:
                last_micro_resistance = current_high
                resistance_index = i
                break

        if last_micro_resistance is None:
            return {
                'micro_resistance': None,
                'distance_ticks': 0.0,
                'distance_pips': 0.0,
                'bounce_probability': 0.0,
                'age_minutes': 0,
                'rejection_detected': False,
                'strength': 'NONE'
            }

        # 2. Calculer distance en ticks et pips
        distance_price = abs(last_micro_resistance - current_price)
        distance_pips = distance_price * 10000
        distance_ticks = distance_pips * 10  # 1 pip = 10 ticks approx

        # 3. Calculer bounce probability (réactions passées à ce niveau)
        tolerance = 0.5 / 10000  # 0.5 pip de tolérance
        bounces = 0
        breaks = 0

        for i in range(1, len(recent_data)):
            prev_close = recent_data.iloc[i - 1]['close']
            curr_low = recent_data.iloc[i]['low']
            curr_high = recent_data.iloc[i]['high']
            curr_close = recent_data.iloc[i]['close']

            # Le prix a touché le niveau ?
            level_touched = (
                curr_low <= last_micro_resistance + tolerance and
                curr_high >= last_micro_resistance - tolerance
            )

            if level_touched:
                # Bounce down : Prix monte vers niveau et redescend
                if prev_close < last_micro_resistance and curr_high >= last_micro_resistance - tolerance and curr_close < last_micro_resistance:
                    bounces += 1

                # Break up : Prix traverse le niveau
                elif prev_close < last_micro_resistance and curr_close > last_micro_resistance + tolerance:
                    breaks += 1

        total_tests = bounces + breaks
        bounce_probability = bounces / total_tests if total_tests > 0 else 0.5

        # 4. Calculer âge du niveau (en minutes)
        age_minutes = len(recent_data) - resistance_index - 1

        # 5. Détecter rejection (wick de rejet sur bougie actuelle)
        last_candle = historical_data.iloc[-1]
        upper_wick = last_candle['high'] - max(last_candle['open'], last_candle['close'])
        body_size = abs(last_candle['close'] - last_candle['open'])

        rejection_detected = (
            upper_wick > body_size * 1.5 and  # Wick > 1.5x body
            abs(last_candle['high'] - last_micro_resistance) < 0.5 / 10000  # Wick a touché résistance
        )

        # 6. Déterminer strength
        if distance_pips < 0.5 and bounce_probability >= 0.7:
            strength = 'STRONG'
        elif distance_pips < 1.0 and bounce_probability >= 0.6:
            strength = 'MODERATE'
        elif distance_pips < 1.5:
            strength = 'WEAK'
        else:
            strength = 'NONE'

        return {
            'micro_resistance': last_micro_resistance,
            'distance_ticks': distance_ticks,
            'distance_pips': distance_pips,
            'bounce_probability': bounce_probability,
            'age_minutes': age_minutes,
            'rejection_detected': rejection_detected,
            'strength': strength
        }

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

    # ═══════════════════════════════════════════════════════════════
    # 🆕 16 JAN 2026: MTF ANALYSIS (M15 + M5 + M1)
    # ═══════════════════════════════════════════════════════════════

    def _init_asset_history(self, asset: str) -> None:
        """Initialise l'historique pour un asset s'il n'existe pas"""
        if asset not in self._mtf_history:
            self._mtf_history[asset] = {
                'M15': deque(maxlen=self._history_maxlen),
                'M5': deque(maxlen=self._history_maxlen),
                'M1': deque(maxlen=self._history_maxlen)
            }

    def analyze_single_timeframe(
        self,
        asset: str,
        timeframe: str,
        candles: pd.DataFrame,
        current_price: float
    ) -> MTFAnalysisResult:
        """
        Analyse un seul timeframe via TENDANCE NETTE sur N bougies

        16 FEV 2026: CORRECTION CRITIQUE — l'ancienne version regardait
        1 seule bougie (LOOKBACK_CANDLES=1), ce qui confondait un pullback
        de 4 bougies rouges avec un retournement bearish.

        Maintenant: direction = mouvement net sur N bougies fermées.
        4 bougies rouges dans une tendance bullish = toujours BULLISH
        si le net reste positif.

        Args:
            asset: Symbole
            timeframe: M30, M15, M5, M1
            candles: DataFrame OHLCV
            current_price: Prix actuel

        Returns:
            MTFAnalysisResult avec direction basée sur tendance nette
        """
        self._init_asset_history(asset)

        # 20 FEV 2026: MTF scalping — M30 supprimé, lookback adapté scalping
        # 27 FEV 2026: M5 passé à 3 bougies = 15min (test réactivité momentum)
        # M15: 2 bougies = 30min de direction
        # M5:  3 bougies = 15min de momentum
        # M1:  5 bougies = 5min de micro-tendance
        LOOKBACK_MAP = {'M15': 2, 'M5': 3, 'M1': 5}
        lookback = LOOKBACK_MAP.get(timeframe, 5)

        if candles is None or len(candles) < 2:
            direction = 'NEUTRAL'
            net_pips = 0.0
            trend_clarity = 0.0
            if self.logger:
                self.logger.warning(f"[MTF][{asset}][{timeframe}] PAS DE DONNÉES!")
        else:
            point = self._get_point_size(asset)

            # Bougies fermées = tout sauf la dernière (en cours)
            closed = candles.iloc[:-1]
            if len(closed) < 1:
                direction = 'NEUTRAL'
                net_pips = 0.0
                trend_clarity = 0.0
            else:
                # Prendre les N dernières bougies fermées
                window = closed.iloc[-lookback:] if len(closed) >= lookback else closed

                # Mouvement net = close de la dernière fermée - open de la première du window
                window_open = float(window.iloc[0]['open'])
                window_close = float(window.iloc[-1]['close'])
                net_pips = (window_close - window_open) / point

                # Direction basée sur le mouvement net
                # Seuil minimum : 1 pip pour éviter le bruit
                MIN_NET_PIPS = 1.0
                if net_pips > MIN_NET_PIPS:
                    direction = 'BULLISH'
                elif net_pips < -MIN_NET_PIPS:
                    direction = 'BEARISH'
                else:
                    direction = 'NEUTRAL'

                # Trend clarity : ratio mouvement net / range total du window
                total_range = float(window['high'].max()) - float(window['low'].min())
                net_move = abs(window_close - window_open)
                trend_clarity = min(1.0, net_move / total_range) if total_range > 0 else 0.5

            if self.logger:
                n_used = min(lookback, len(closed))
                color = "🔴" if direction == 'BEARISH' else ("🟢" if direction == 'BULLISH' else "⚪")
                self.logger.info(
                    f"[MTF_CANDLE][{asset}][{timeframe}] {color} "
                    f"{net_pips:+.1f} pips ({n_used} bougies) | → {direction}"
                )

        # Créer résultat
        result = MTFAnalysisResult(
            timeframe=timeframe,
            direction=direction,
            net_pips=net_pips,
            trend_clarity=trend_clarity,
            candles_analyzed=len(candles) if candles is not None else 0,
            timestamp=datetime.now()
        )

        # Stocker dans l'historique
        self._mtf_history[asset][timeframe].append(result)

        if self.logger:
            self.logger.debug(
                f"[MTF_ANALYSIS][{asset}][{timeframe}] "
                f"{direction} | net={net_pips:+.1f} pips | clarity={trend_clarity:.2f}"
            )

        return result

    def get_mtf_trend_verdict(
        self,
        asset: str,
        candles_m30: Optional[pd.DataFrame],
        candles_m15: Optional[pd.DataFrame],
        candles_m5: Optional[pd.DataFrame],
        candles_m1: Optional[pd.DataFrame],
        current_price: float,
        filter_action: str = None
    ) -> MTFTrendVerdict:
        """
        🎯 MÉTHODE PRINCIPALE - Verdict MTF pour décision BEARISH/BULLISH

        12 FEV 2026: Analyse M30 + M15 + M5 + M1 avec pondération macro-dominante.
        M30+M15 bearish = TOUJOURS BEARISH, même si M5+M1 bullish.

        Args:
            asset: Symbole (NAS100, EURUSD, etc.)
            candles_m30: DataFrame M30 (5-10 bougies = contexte macro)
            candles_m15: DataFrame M15 (10+ bougies)
            candles_m5: DataFrame M5 (50-100 bougies recommandé)
            candles_m1: DataFrame M1 (50-100 bougies recommandé)
            current_price: Prix actuel

        Returns:
            MTFTrendVerdict avec direction, alignment, bonus, etc.
        """
        self._init_asset_history(asset)

        # 20 FEV 2026: M30 SUPPRIMÉ — scalping sur M15+M5+M1 uniquement
        m15_result = None
        m5_result = None
        m1_result = None

        if candles_m15 is not None and len(candles_m15) >= 1:
            m15_result = self.analyze_single_timeframe(asset, 'M15', candles_m15, current_price)

        if candles_m5 is not None and len(candles_m5) >= 1:
            m5_result = self.analyze_single_timeframe(asset, 'M5', candles_m5, current_price)

        if candles_m1 is not None and len(candles_m1) >= 1:
            m1_result = self.analyze_single_timeframe(asset, 'M1', candles_m1, current_price)

        # Extraire les directions
        available_directions = []

        m30_dir = 'NO_DATA'  # M30 supprimé

        if m15_result:
            m15_dir = m15_result.direction
            available_directions.append(m15_dir)
        else:
            m15_dir = 'NO_DATA'

        if m5_result:
            m5_dir = m5_result.direction
            available_directions.append(m5_dir)
        else:
            m5_dir = 'NO_DATA'

        if m1_result:
            m1_dir = m1_result.direction
            available_directions.append(m1_dir)
        else:
            m1_dir = 'NO_DATA'

        bearish_count = sum(1 for d in available_directions if d == 'BEARISH')
        bullish_count = sum(1 for d in available_directions if d == 'BULLISH')

        # 20 FEV 2026: PONDÉRATION M15>M5>M1 (M30 supprimé)
        # M15 = 50% (direction principale scalping)
        # M5  = 30% (momentum)
        # M1  = 20% (micro-tendance)
        direction = 'NEUTRAL'
        alignment_count = 0

        weight_m15 = 0.50
        weight_m5 = 0.30
        weight_m1 = 0.20

        weighted_score = 0.0

        if m15_result and m15_result.direction != "NO_DATA":
            if m15_result.direction == "BULLISH":
                weighted_score += weight_m15
            elif m15_result.direction == "BEARISH":
                weighted_score -= weight_m15

        if m5_result and m5_result.direction != "NO_DATA":
            if m5_result.direction == "BULLISH":
                weighted_score += weight_m5
            elif m5_result.direction == "BEARISH":
                weighted_score -= weight_m5

        if m1_result and m1_result.direction != "NO_DATA":
            if m1_result.direction == "BULLISH":
                weighted_score += weight_m1
            elif m1_result.direction == "BEARISH":
                weighted_score -= weight_m1

        WEIGHTED_THRESHOLD = 0.20
        weighted_score = round(weighted_score, 10)

        if weighted_score >= WEIGHTED_THRESHOLD:
            direction = "BULLISH"
            alignment_count = bullish_count
        elif weighted_score <= -WEIGHTED_THRESHOLD:
            direction = "BEARISH"
            alignment_count = bearish_count
        else:
            direction = "NEUTRAL"
            alignment_count = 0

        if self.logger:
            self.logger.info(
                f"[MTF_WEIGHTED][{asset}] Score pondéré: {weighted_score:+.2f} "
                f"(seuil={WEIGHTED_THRESHOLD}) → {direction} | "
                f"M15:{m15_dir} M5:{m5_dir} M1:{m1_dir}"
            )

        alignment = f"{alignment_count}/3"

        # Bonus pour 3 TFs
        if alignment_count == 3:
            bonus = 30.0
        elif alignment_count == 2:
            bonus = 15.0
        elif alignment_count == 1:
            bonus = 5.0
        else:
            bonus = 0.0

        should_override_delta = (direction == 'BEARISH')
        confidence = alignment_count / 3.0 if alignment_count > 0 else 0.1

        if self.logger:
            self.logger.debug(f"[MTF_BONUS] {direction} {alignment} → +{bonus:.0f} pts")

        # Construire le détail
        details = {
            'm15': {
                'direction': m15_dir,
                'net_pips': m15_result.net_pips if m15_result else 0.0,
                'clarity': m15_result.trend_clarity if m15_result else 0.0
            },
            'm5': {
                'direction': m5_dir,
                'net_pips': m5_result.net_pips if m5_result else 0.0,
                'clarity': m5_result.trend_clarity if m5_result else 0.0
            },
            'm1': {
                'direction': m1_dir,
                'net_pips': m1_result.net_pips if m1_result else 0.0,
                'clarity': m1_result.trend_clarity if m1_result else 0.0
            },
            'bearish_count': bearish_count,
            'bullish_count': bullish_count,
            'weighted_score': weighted_score,
            'history_size': {
                'M15': len(self._mtf_history[asset]['M15']),
                'M5': len(self._mtf_history[asset]['M5']),
                'M1': len(self._mtf_history[asset]['M1'])
            }
        }

        verdict = MTFTrendVerdict(
            direction=direction,
            alignment=alignment,
            alignment_count=alignment_count,
            bonus=bonus,
            m30_direction=m30_dir,
            m15_direction=m15_dir,
            m5_direction=m5_dir,
            m1_direction=m1_dir,
            confidence=confidence,
            should_override_delta=should_override_delta,
            details=details
        )

        # Cacher le verdict
        self._last_verdict[asset] = verdict

        # Log le verdict avec couleur de bougie
        if self.logger:
            emoji = "🐻" if direction == 'BEARISH' else "🐂"

            def _tf_display(tf_dir):
                if tf_dir == 'BEARISH':
                    return "🔴"
                elif tf_dir == 'BULLISH':
                    return "🟢"
                else:
                    return "⚫"  # NO_DATA

            self.logger.info(
                f"{emoji} [MTF_VERDICT][{asset}] "
                f"{direction} ({alignment}) | "
                f"M15:{_tf_display(m15_dir)} M5:{_tf_display(m5_dir)} M1:{_tf_display(m1_dir)} | "
                f"Bonus: {bonus:+.0f} pts"
            )

        return verdict

    def get_last_verdict(self, asset: str) -> Optional[MTFTrendVerdict]:
        """Retourne le dernier verdict caché pour un asset"""
        return self._last_verdict.get(asset)

    def get_mtf_history(self, asset: str, timeframe: str, limit: int = 10) -> List[MTFAnalysisResult]:
        """
        Retourne l'historique des analyses pour un asset/timeframe

        Args:
            asset: Symbole
            timeframe: M15, M5, M1
            limit: Nombre d'entrées à retourner

        Returns:
            Liste des dernières analyses (plus récente en premier)
        """
        self._init_asset_history(asset)
        history = list(self._mtf_history[asset].get(timeframe, []))
        return list(reversed(history[-limit:]))

    def get_trend_consistency(self, asset: str, timeframe: str, lookback: int = 10) -> Dict[str, Any]:
        """
        Analyse la consistance de la tendance sur les N dernières analyses

        Args:
            asset: Symbole
            timeframe: M15, M5, M1
            lookback: Nombre d'analyses à considérer

        Returns:
            Dict avec ratio bearish/bullish et tendance dominante
        """
        history = self.get_mtf_history(asset, timeframe, lookback)

        if not history:
            return {
                'dominant_trend': 'NEUTRAL',
                'bearish_ratio': 0.0,
                'bullish_ratio': 0.0,
                'consistency': 0.0,
                'sample_size': 0
            }

        bearish_count = sum(1 for h in history if h.direction == 'BEARISH')
        bullish_count = sum(1 for h in history if h.direction == 'BULLISH')
        total = len(history)

        bearish_ratio = bearish_count / total
        bullish_ratio = bullish_count / total

        if bearish_ratio > bullish_ratio:
            dominant = 'BEARISH'
            consistency = bearish_ratio
        elif bullish_ratio > bearish_ratio:
            dominant = 'BULLISH'
            consistency = bullish_ratio
        else:
            dominant = 'NEUTRAL'
            consistency = 0.5

        return {
            'dominant_trend': dominant,
            'bearish_ratio': round(bearish_ratio, 2),
            'bullish_ratio': round(bullish_ratio, 2),
            'consistency': round(consistency, 2),
            'sample_size': total
        }

    def get_mtf_stability(self, asset: str, lookback: int = 5) -> int:
        """
        Compte le nombre de cycles consécutifs où M15 est resté dans la même direction.

        Utilisé pour rendre le mtf_malus_factor progressif :
        - 1  = fraîchement tourné (peu fiable)
        - 2-3 = stable (fiabilité modérée)
        - 4+  = très stable (haute confiance dans la tendance)

        Args:
            asset:   Symbole (USDJPY, USDCHF, etc.)
            lookback: Nombre max de cycles à remonter

        Returns:
            int: Nombre de cycles consécutifs (minimum 1)
        """
        history = self.get_mtf_history(asset, 'M15', lookback)
        if not history:
            return 1

        # get_mtf_history retourne le plus récent en premier
        current_dir = history[0].direction
        if current_dir == 'NEUTRAL':
            return 1

        count = 1
        for i in range(1, len(history)):
            if history[i].direction == current_dir:
                count += 1
            else:
                break

        return count

    def should_take_bearish_trade(
        self,
        asset: str,
        candles_m30: Optional[pd.DataFrame],
        candles_m15: Optional[pd.DataFrame],
        candles_m5: Optional[pd.DataFrame],
        candles_m1: Optional[pd.DataFrame],
        current_price: float,
        orderflow_score: float = 0.0,
        delta_value: float = 0.0
    ) -> Dict[str, Any]:
        """
        🎯 DÉCISION FINALE: Doit-on prendre un trade BEARISH ?

        Logique asymétrique:
        - MTF = autorité pour la DIRECTION (pas le delta)
        - Delta = indicateur de TIMING uniquement
        - Si MTF dit SELL → on vend, peu importe le delta

        Args:
            asset: Symbole
            candles_m30/m15/m5/m1: DataFrames OHLCV
            current_price: Prix actuel
            orderflow_score: Score OrderFlow actuel (optionnel)
            delta_value: Valeur du delta actuel (optionnel)

        Returns:
            Dict avec décision, bonus, et justification
        """
        # Obtenir le verdict MTF
        verdict = self.get_mtf_trend_verdict(
            asset, candles_m30, candles_m15, candles_m5, candles_m1, current_price
        )

        # Décision basée sur MTF (pas sur delta pour BEARISH)
        should_trade = verdict.direction == 'BEARISH' and verdict.alignment_count >= 2

        # Calculer le score final avec bonus
        final_score = orderflow_score + verdict.bonus

        # Timing quality basé sur delta (mais pas sur décision)
        if delta_value < 0:
            timing_quality = 'OPTIMAL'
            timing_bonus = 5.0  # Petit bonus timing
        elif delta_value < 10:
            timing_quality = 'ACCEPTABLE'
            timing_bonus = 0.0
        else:
            timing_quality = 'SUBOPTIMAL'
            timing_bonus = 0.0  # Pas de malus, MTF prime

        final_score += timing_bonus

        result = {
            'should_trade': should_trade,
            'direction': 'SELL' if should_trade else 'NO_TRADE',
            'mtf_verdict': verdict.direction,
            'mtf_alignment': verdict.alignment,
            'mtf_bonus': verdict.bonus,
            'timing_quality': timing_quality,
            'timing_bonus': timing_bonus,
            'orderflow_score_original': orderflow_score,
            'final_score': final_score,
            'delta_value': delta_value,
            'delta_ignored_for_direction': True,  # Toujours True pour BEARISH
            'confidence': verdict.confidence,
            'details': {
                'm15': verdict.m15_direction,
                'm5': verdict.m5_direction,
                'm1': verdict.m1_direction
            },
            'reason': self._build_bearish_reason(verdict, timing_quality, delta_value)
        }

        if self.logger:
            emoji = "✅" if should_trade else "❌"
            self.logger.info(
                f"{emoji} [BEARISH_DECISION][{asset}] "
                f"Trade={should_trade} | "
                f"MTF={verdict.alignment} {verdict.direction} | "
                f"Score: {orderflow_score:.1f} + {verdict.bonus:+.0f} + {timing_bonus:+.0f} = {final_score:.1f} | "
                f"Delta={delta_value:.1f} ({timing_quality})"
            )

        return result

    def _build_bearish_reason(
        self,
        verdict: MTFTrendVerdict,
        timing_quality: str,
        delta_value: float
    ) -> str:
        """Construit une explication lisible de la décision"""
        if verdict.direction != 'BEARISH':
            return f"MTF non aligné BEARISH ({verdict.alignment})"

        if verdict.alignment_count == 3:
            reason = f"MTF parfaitement aligné BEARISH (M15↓ M5↓ M1↓) → +{verdict.bonus:.0f} pts"
        elif verdict.alignment_count == 2:
            aligned = []
            if verdict.m15_direction == 'BEARISH':
                aligned.append('M15↓')
            if verdict.m5_direction == 'BEARISH':
                aligned.append('M5↓')
            if verdict.m1_direction == 'BEARISH':
                aligned.append('M1↓')
            reason = f"MTF aligné 2/3 BEARISH ({' '.join(aligned)}) → +{verdict.bonus:.0f} pts"
        else:
            reason = "Alignement MTF insuffisant"

        # Ajouter info timing
        if timing_quality == 'OPTIMAL':
            reason += f" | Delta négatif ({delta_value:.1f}) = timing optimal"
        elif timing_quality == 'SUBOPTIMAL':
            reason += f" | Delta positif ({delta_value:.1f}) mais MTF prime → on trade quand même"

        return reason
