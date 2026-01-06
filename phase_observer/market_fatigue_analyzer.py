"""
😫 MARKET FATIGUE ANALYZER (PRIORITY_2)
Mesure la fatigue des acheteurs/vendeurs - Quand le mouvement est épuisé

Impact attendu: Élimine 80% trades contraires selon rapport
Raison: "Vous achetez quand acheteurs épuisés. Problème #1."

Source: DEBUG_LOGS.txt lignes 816-891
Date: 03 Janvier 2026
"""

import pandas as pd
import numpy as np
from typing import Dict, Optional, Any


class MarketFatigueAnalyzer:
    """
    Mesure la fatigue des acheteurs/vendeurs - Quand le mouvement est épuisé

    Détecte:
    - Fatigue des acheteurs (volume/fréquence décroissante)
    - Fatigue des vendeurs
    - Fatigue du momentum
    - Patterns d'épuisement (climax, divergence)
    """

    def __init__(self, logger=None):
        """
        Args:
            logger: Logger pour debug
        """
        self.logger = logger

    def calculate_fatigue_indicators(
        self,
        ticks_df: Optional[pd.DataFrame],
        recent_candles: pd.DataFrame
    ) -> Dict[str, Any]:
        """
        Quand les acheteurs/vendeurs n'en peuvent plus

        Args:
            ticks_df: DataFrame ticks avec colonnes ['time', 'side', 'volume', 'price']
            recent_candles: DataFrame OHLCV (10-20 dernières bougies)

        Returns:
            dict: {
                'fatigue_score': float (0-10),
                'buyer_fatigue': dict,
                'seller_fatigue': dict,
                'momentum_fatigue': dict,
                'exhaustion_signals': dict,
                'market_state': str
            }
        """
        if recent_candles is None or len(recent_candles) < 5:
            # 06 JAN 2026 FIX: Log WARNING pour diagnostiquer pourquoi N/A
            if self.logger:
                candles_count = len(recent_candles) if recent_candles is not None else 0
                self.logger.warning(
                    f"⚠️ MarketFatigueAnalyzer: Données insuffisantes (candles={candles_count}, besoin>=5) → Retourne UNKNOWN"
                )
            return {
                'fatigue_score': 0.0,
                'buyer_fatigue': {'score': 0, 'reason': 'Pas assez de données'},
                'seller_fatigue': {'score': 0, 'reason': 'Pas assez de données'},
                'momentum_fatigue': {'score': 0, 'reason': 'Pas assez de données'},
                'exhaustion_signals': {'score': 0, 'signals': []},
                'market_state': 'UNKNOWN'
            }

        # 1. Fatigue des acheteurs
        buyer_fatigue = self._calculate_buyer_fatigue(ticks_df) if ticks_df is not None else {'score': 0}

        # 2. Fatigue des vendeurs
        seller_fatigue = self._calculate_seller_fatigue(ticks_df) if ticks_df is not None else {'score': 0}

        # 3. Fatigue du momentum
        momentum_fatigue = self._calculate_momentum_fatigue(recent_candles)

        # 4. Signes d'épuisement
        exhaustion_signals = self._detect_exhaustion_patterns(ticks_df, recent_candles)

        # 5. Score de fatigue composite
        fatigue_score = (
            buyer_fatigue['score'] * 0.3 +
            seller_fatigue['score'] * 0.3 +
            momentum_fatigue['score'] * 0.2 +
            exhaustion_signals['score'] * 0.2
        )

        return {
            'fatigue_score': fatigue_score,
            'buyer_fatigue': buyer_fatigue,
            'seller_fatigue': seller_fatigue,
            'momentum_fatigue': momentum_fatigue,
            'exhaustion_signals': exhaustion_signals,
            'market_state': self._determine_market_state(fatigue_score)
        }

    def _calculate_buyer_fatigue(self, ticks_df: pd.DataFrame) -> Dict[str, Any]:
        """
        Les acheteurs sont-ils épuisés ?

        Args:
            ticks_df: DataFrame ticks

        Returns:
            dict: {'score': 0-10, 'fatigue_level': str, 'reasons': List[str]}
        """
        if ticks_df is None or len(ticks_df) < 5:
            return {'score': 0, 'reason': 'Pas assez de données'}

        recent_buys = ticks_df[ticks_df['side'] == 'buy'].tail(20)

        if len(recent_buys) < 5:
            return {'score': 0, 'reason': 'Pas assez d\'achats récents'}

        # Signes de fatigue des acheteurs
        fatigue_points = 0
        reasons = []

        # 1. Volume décroissant (3 derniers achats)
        volumes = recent_buys['volume'].values
        if len(volumes) >= 3:
            if volumes[-1] < volumes[-2] < volumes[-3]:
                fatigue_points += 3
                reasons.append('Volume buy décroissant')

        # 2. Fréquence décroissante (intervalle croissant entre achats)
        if 'time' in recent_buys.columns and len(recent_buys) >= 3:
            timestamps = pd.to_datetime(recent_buys['time'])
            intervals = timestamps.diff().dt.total_seconds()

            if len(intervals) >= 3:
                # Si dernier intervalle > avant-dernier → achats ralentissent
                last_interval = intervals.iloc[-1]
                prev_interval = intervals.iloc[-2]

                if pd.notna(last_interval) and pd.notna(prev_interval) and last_interval > prev_interval:
                    fatigue_points += 2
                    reasons.append('Fréquence buy décroissante')

        # 3. Prix n'augmente plus malgré les achats (absorption)
        if 'price' in recent_buys.columns and len(recent_buys) >= 3:
            prices = recent_buys['price'].values
            if len(prices) >= 3 and prices[0] > 0:
                price_change = (prices[-1] - prices[0]) / prices[0]

                # Si prix stagne (<1 pip) malgré volume buy élevé → absorption
                if price_change < 0.0001 and np.sum(volumes) > np.mean(volumes) * 15:
                    fatigue_points += 4  # Forte absorption = forte fatigue
                    reasons.append('Absorption forte (prix stagne malgré achats)')

        return {
            'score': min(10, fatigue_points),
            'fatigue_level': 'HIGH' if fatigue_points >= 6 else 'MEDIUM' if fatigue_points >= 3 else 'LOW',
            'reasons': reasons
        }

    def _calculate_seller_fatigue(self, ticks_df: pd.DataFrame) -> Dict[str, Any]:
        """
        Les vendeurs sont-ils épuisés ?

        Args:
            ticks_df: DataFrame ticks

        Returns:
            dict: {'score': 0-10, 'fatigue_level': str, 'reasons': List[str]}
        """
        if ticks_df is None or len(ticks_df) < 5:
            return {'score': 0, 'reason': 'Pas assez de données'}

        recent_sells = ticks_df[ticks_df['side'] == 'sell'].tail(20)

        if len(recent_sells) < 5:
            return {'score': 0, 'reason': 'Pas assez de ventes récentes'}

        # Signes de fatigue des vendeurs (symétrique aux acheteurs)
        fatigue_points = 0
        reasons = []

        # 1. Volume décroissant
        volumes = recent_sells['volume'].values
        if len(volumes) >= 3:
            if volumes[-1] < volumes[-2] < volumes[-3]:
                fatigue_points += 3
                reasons.append('Volume sell décroissant')

        # 2. Fréquence décroissante
        if 'time' in recent_sells.columns and len(recent_sells) >= 3:
            timestamps = pd.to_datetime(recent_sells['time'])
            intervals = timestamps.diff().dt.total_seconds()

            if len(intervals) >= 3:
                last_interval = intervals.iloc[-1]
                prev_interval = intervals.iloc[-2]

                if pd.notna(last_interval) and pd.notna(prev_interval) and last_interval > prev_interval:
                    fatigue_points += 2
                    reasons.append('Fréquence sell décroissante')

        # 3. Prix ne baisse plus malgré les ventes (absorption)
        if 'price' in recent_sells.columns and len(recent_sells) >= 3:
            prices = recent_sells['price'].values
            if len(prices) >= 3 and prices[0] > 0:
                price_change = (prices[-1] - prices[0]) / prices[0]

                # Si prix stagne malgré volume sell élevé → absorption
                if abs(price_change) < 0.0001 and np.sum(volumes) > np.mean(volumes) * 15:
                    fatigue_points += 4
                    reasons.append('Absorption forte (prix stagne malgré ventes)')

        return {
            'score': min(10, fatigue_points),
            'fatigue_level': 'HIGH' if fatigue_points >= 6 else 'MEDIUM' if fatigue_points >= 3 else 'LOW',
            'reasons': reasons
        }

    def _calculate_momentum_fatigue(self, recent_candles: pd.DataFrame) -> Dict[str, Any]:
        """
        Le momentum est-il épuisé ?

        Args:
            recent_candles: DataFrame OHLCV (10-20 bougies)

        Returns:
            dict: {'score': 0-10, 'fatigue_level': str, 'reasons': List[str]}
        """
        if len(recent_candles) < 10:
            return {'score': 0, 'reason': 'Pas assez de bougies'}

        fatigue_points = 0
        reasons = []

        # 1. ATR décroissant (volatilité diminue)
        highs = recent_candles['high'].values
        lows = recent_candles['low'].values
        closes = recent_candles['close'].values

        if len(highs) >= 10:
            # Calculer ATR sur 2 fenêtres
            recent_tr = []
            for i in range(-5, 0):  # 5 dernières bougies
                tr = max(
                    highs[i] - lows[i],
                    abs(highs[i] - closes[i-1]) if i > -len(closes) else 0,
                    abs(lows[i] - closes[i-1]) if i > -len(closes) else 0
                )
                recent_tr.append(tr)

            older_tr = []
            for i in range(-10, -5):  # 5 bougies avant
                tr = max(
                    highs[i] - lows[i],
                    abs(highs[i] - closes[i-1]) if i > -len(closes) else 0,
                    abs(lows[i] - closes[i-1]) if i > -len(closes) else 0
                )
                older_tr.append(tr)

            avg_recent_tr = np.mean(recent_tr)
            avg_older_tr = np.mean(older_tr)

            # Si ATR récent < 70% ATR ancien → momentum fatigué
            if avg_older_tr > 0 and avg_recent_tr < avg_older_tr * 0.7:
                fatigue_points += 4
                reasons.append('ATR décroissant (volatilité chute)')

        # 2. Volume décroissant
        volumes = recent_candles['volume'].values
        if len(volumes) >= 10:
            recent_vol = np.mean(volumes[-5:])
            older_vol = np.mean(volumes[-10:-5])

            # Si volume récent < 60% volume ancien
            if older_vol > 0 and recent_vol < older_vol * 0.6:
                fatigue_points += 3
                reasons.append('Volume décroissant')

        # 3. Body size décroissant (bougies de plus en plus petites)
        if len(closes) >= 10:
            recent_bodies = []
            older_bodies = []

            for i in range(-5, 0):
                open_price = recent_candles.iloc[i].get('open', closes[i])
                body = abs(closes[i] - open_price)
                recent_bodies.append(body)

            for i in range(-10, -5):
                open_price = recent_candles.iloc[i].get('open', closes[i])
                body = abs(closes[i] - open_price)
                older_bodies.append(body)

            avg_recent_body = np.mean(recent_bodies)
            avg_older_body = np.mean(older_bodies)

            # Si bodies récents < 50% bodies anciens
            if avg_older_body > 0 and avg_recent_body < avg_older_body * 0.5:
                fatigue_points += 3
                reasons.append('Body size décroissant')

        return {
            'score': min(10, fatigue_points),
            'fatigue_level': 'HIGH' if fatigue_points >= 6 else 'MEDIUM' if fatigue_points >= 3 else 'LOW',
            'reasons': reasons
        }

    def _detect_exhaustion_patterns(
        self,
        ticks_df: Optional[pd.DataFrame],
        recent_candles: pd.DataFrame
    ) -> Dict[str, Any]:
        """
        Détecte les patterns d'épuisement (climax, divergence volume/prix)

        Args:
            ticks_df: DataFrame ticks
            recent_candles: DataFrame OHLCV

        Returns:
            dict: {'score': 0-10, 'signals': List[str]}
        """
        exhaustion_score = 0
        signals = []

        if len(recent_candles) < 5:
            return {'score': 0, 'signals': []}

        # Pattern 1: Climax volume (volume spike + petit mouvement prix)
        volumes = recent_candles['volume'].values
        if len(volumes) >= 5:
            avg_vol = np.mean(volumes[:-1])  # Volume moyen sauf dernière bougie
            last_vol = volumes[-1]

            # Volume spike > 200% moyenne
            if avg_vol > 0 and last_vol > avg_vol * 2.0:
                # Mais mouvement prix faible
                last_candle = recent_candles.iloc[-1]
                open_price = last_candle.get('open', last_candle['close'])
                body = abs(last_candle['close'] - open_price)
                candle_range = last_candle['high'] - last_candle['low']

                # Body < 30% range = absorption
                if candle_range > 0 and body < candle_range * 0.3:
                    exhaustion_score += 5
                    signals.append('Climax volume (spike + absorption)')

        # Pattern 2: Divergence volume/prix
        if len(recent_candles) >= 10:
            # Prix fait nouveau high/low mais volume diminue
            closes = recent_candles['close'].values
            volumes = recent_candles['volume'].values

            # Uptrend exhaustion
            if closes[-1] > closes[-5]:  # Prix monte
                recent_vol_avg = np.mean(volumes[-3:])
                older_vol_avg = np.mean(volumes[-8:-5])

                # Mais volume diminue
                if older_vol_avg > 0 and recent_vol_avg < older_vol_avg * 0.7:
                    exhaustion_score += 3
                    signals.append('Divergence haussière (prix up, volume down)')

            # Downtrend exhaustion
            elif closes[-1] < closes[-5]:  # Prix baisse
                recent_vol_avg = np.mean(volumes[-3:])
                older_vol_avg = np.mean(volumes[-8:-5])

                if older_vol_avg > 0 and recent_vol_avg < older_vol_avg * 0.7:
                    exhaustion_score += 3
                    signals.append('Divergence baissière (prix down, volume down)')

        return {
            'score': min(10, exhaustion_score),
            'signals': signals
        }

    def _determine_market_state(self, fatigue_score: float) -> str:
        """
        Détermine l'état du marché basé sur le score de fatigue

        Args:
            fatigue_score: Score composite 0-10

        Returns:
            str: 'EXHAUSTED' | 'FATIGUED' | 'NORMAL' | 'ENERGETIC'
        """
        if fatigue_score >= 7.0:
            return 'EXHAUSTED'  # Mouvement sur le point de s'inverser
        elif fatigue_score >= 4.0:
            return 'FATIGUED'   # Mouvement s'affaiblit
        elif fatigue_score >= 2.0:
            return 'NORMAL'     # Marché sain
        else:
            return 'ENERGETIC'  # Momentum fort
