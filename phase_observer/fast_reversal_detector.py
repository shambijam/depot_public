"""
FastReversalDetector — Détecteur de micro-retournement rapide
12 MAR 2026

RÔLE : Complémentaire à l'IRD (institutionnel, lent — 150-250 min de données).
Détecte les micro-retournements sur M1 (10-15 bougies) pour les marchés
oscillants et les phases range intraday.

POURQUOI L'IRD EST AVEUGLE :
    - Changepoint  : exige 50 barres M5 (250 min) → Bayésien lent
    - Divergence   : exige 30 CVD (données longues)
    - Smart Money  : exige 20 barres M5 (100 min)
    → Un micro-retournement M1 de 5-15 bougies = bruit invisible pour l'IRD

4 SIGNAUX (M1 uniquement, données déjà en cache — 0 appel MT5 supplémentaire) :
    1. Divergence prix/volume  — pente prix vs pente volume sur 10 barres M1
    2. Absorption volumique    — gros tick_volume + petit corps de bougie
    3. Épuisement delta        — delta élevé mais faible mouvement prix (< 2 pips)
    4. Rejet Ichimoku          — high/low touche Tenkan/Kijun + bougie rejetée

VERDICT : reversal_detected si >= 2 signaux concordants (vote pondéré par strength)

COORDINATION MTF (évite le conflit avec MTF ALL-IN Rule V5) :
    - Direction OPPOSÉE au MTF → scoring §14 : malus réduit par mtf_malus_factor
      (pullback dans tendance forte = normal, pas dramatique)
    - Direction IDENTIQUE au MTF → §14 : bonus de confirmation
    - La décision reste dans decide_scalp_action(), ce module ne fait que scorer

INTÉGRATION :
    advanced_scoring.calculate_final_score() → §14 (paramètre fast_reversal_result)
    decision_pipeline.decide_scalp_action()  → paramètre transmis
    run_bot.py scalping_worker()             → instanciation + appel + transmission
"""

from __future__ import annotations

import logging
from typing import Optional, Dict, List

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class FastReversalDetector:
    """Détecteur de micro-retournement M1 — complémentaire à l'IRD institutionnel."""

    MIN_BARS = 10           # Barres M1 minimum requises
    LOOKBACK_DIV = 10       # Fenêtre divergence prix/volume
    VOL_LOOKBACK = 20       # Fenêtre calcul volume moyen dynamique
    ABSORPTION_VOL_MULT = 1.5   # Seuil: volume > 1.5× moy = potentielle absorption
    ABSORPTION_BODY_RATIO = 0.35  # Seuil: corps < 35% range = absorption
    DELTA_MOVE_MIN_PIPS = 2.0   # Mouvement min (pips) pour delta "efficace"
    TENKAN_THRESHOLD_PIPS = 1.5  # Distance max (pips) pour rejet Tenkan
    KIJUN_THRESHOLD_PIPS = 2.0   # Distance max (pips) pour rejet Kijun

    def __init__(self, logger_ref=None):
        self._log = logger_ref or logger

    def analyze(
        self,
        df_m1: pd.DataFrame,
        ichimoku_result: Optional[dict] = None,
        delta_value: float = 0.0,
        point: float = 0.00001,
        mtf_direction: str = "NEUTRAL",
    ) -> dict:
        """
        Analyse les 4 signaux de micro-retournement sur M1.

        Args:
            df_m1:           DataFrame M1 avec colonnes close, open, high, low, tick_volume
            ichimoku_result: résultat IchimokuAnalyzer déjà calculé (peut être None)
            delta_value:     delta net depuis orderflow_result_mini['summary']['delta']
            point:           taille du point (USDJPY=0.01, USDCHF/EURUSD=0.0001)
            mtf_direction:   direction MTF actuelle (BULLISH/BEARISH/NEUTRAL)

        Returns:
            {
                'available':         bool,
                'reversal_detected': bool,
                'direction':         str,    # BULLISH / BEARISH / NEUTRAL
                'confidence':        float,  # 0.0–1.0
                'signal_count':      int,
                'signals':           list[str],  # raisons lisibles (logs)
                'raw_signals':       list[dict], # données brutes
            }
        """
        _empty = {
            'available': False,
            'reversal_detected': False,
            'direction': 'NEUTRAL',
            'confidence': 0.0,
            'signal_count': 0,
            'signals': [],
            'raw_signals': [],
        }

        if df_m1 is None or len(df_m1) < self.MIN_BARS:
            return _empty

        required_cols = {'close', 'open', 'high', 'low', 'tick_volume'}
        if not required_cols.issubset(df_m1.columns):
            return _empty

        detected: List[dict] = []

        # ─── Signal 1 : Divergence prix/volume ──────────────────────────────
        s1 = self._detect_price_volume_divergence(df_m1, point)
        if s1.get('detected'):
            detected.append(s1)

        # ─── Signal 2 : Absorption volumique ────────────────────────────────
        s2 = self._detect_absorption(df_m1)
        if s2.get('detected'):
            detected.append(s2)

        # ─── Signal 3 : Épuisement delta ────────────────────────────────────
        s3 = self._detect_delta_exhaustion(df_m1, delta_value, point)
        if s3.get('detected'):
            detected.append(s3)

        # ─── Signal 4 : Rejet Ichimoku ──────────────────────────────────────
        s4 = self._detect_ichimoku_rejection(df_m1, ichimoku_result, point)
        if s4.get('detected'):
            detected.append(s4)

        signal_count = len(detected)
        direction = self._compute_direction(detected)
        reversal_detected = signal_count >= 2

        confidence = 0.0
        if reversal_detected:
            strengths = [s.get('strength', 0.5) for s in detected]
            avg_strength = sum(strengths) / len(strengths)
            # Confidence augmente avec le nombre de signaux (max 4)
            confidence = min(1.0, avg_strength * (signal_count / 3.0))

        return {
            'available': True,
            'reversal_detected': reversal_detected,
            'direction': direction,
            'confidence': round(confidence, 3),
            'signal_count': signal_count,
            'signals': [s['reason'] for s in detected],
            'raw_signals': detected,
        }

    # ═══════════════════════════════════════════════════════════════════════
    # SIGNAL 1 — Divergence prix/volume
    # ═══════════════════════════════════════════════════════════════════════

    def _detect_price_volume_divergence(
        self, df_m1: pd.DataFrame, point: float
    ) -> dict:
        """
        Régression linéaire sur 10 barres M1 : pente prix vs pente volume.

        BEARISH : prix monte (slope > 0) + volume baisse (slope < 0) + vol < 75% moy
            → le mouvement haussier s'essouffle, les acheteurs perdent force
        BULLISH : prix baisse (slope < 0) + volume baisse (slope < 0) + vol < 75% moy
            → le mouvement baissier s'essouffle, les vendeurs perdent force
        """
        n = min(self.LOOKBACK_DIV, len(df_m1))
        sl = df_m1.tail(n)

        prices = sl['close'].values.astype(float)
        volumes = sl['tick_volume'].values.astype(float)

        x = np.arange(n, dtype=float)
        price_slope = float(np.polyfit(x, prices, 1)[0])
        vol_slope = float(np.polyfit(x, volumes, 1)[0])

        # Mouvement total en pips (filtrer bruit < 1.5 pips)
        price_move_pips = abs(prices[-1] - prices[0]) / point
        if price_move_pips < 1.5:
            return {'detected': False}

        # Volume moyen dynamique (30 barres ou tout le df)
        avg_vol = df_m1['tick_volume'].tail(
            min(self.VOL_LOOKBACK, len(df_m1))
        ).mean()
        if avg_vol <= 0:
            return {'detected': False}

        vol_ratio = float(np.mean(volumes)) / avg_vol  # < 1 = volume faible

        DIVERGENCE_VOL_RATIO = 0.75  # volume < 75% de la moyenne

        if price_slope > 0 and vol_slope < 0 and vol_ratio < DIVERGENCE_VOL_RATIO:
            strength = min(1.0, (price_move_pips / 5.0) * (1.0 - vol_ratio))
            return {
                'detected': True,
                'direction': 'BEARISH',
                'strength': round(strength, 3),
                'reason': (
                    f"DIV_PV_BEARISH: prix+{price_move_pips:.1f}p "
                    f"vol={vol_ratio:.0%}moy"
                ),
            }

        if price_slope < 0 and vol_slope < 0 and vol_ratio < DIVERGENCE_VOL_RATIO:
            strength = min(1.0, (price_move_pips / 5.0) * (1.0 - vol_ratio))
            return {
                'detected': True,
                'direction': 'BULLISH',
                'strength': round(strength, 3),
                'reason': (
                    f"DIV_PV_BULLISH: prix-{price_move_pips:.1f}p "
                    f"vol={vol_ratio:.0%}moy"
                ),
            }

        return {'detected': False}

    # ═══════════════════════════════════════════════════════════════════════
    # SIGNAL 2 — Absorption volumique
    # ═══════════════════════════════════════════════════════════════════════

    def _detect_absorption(self, df_m1: pd.DataFrame) -> dict:
        """
        Détecte absorption : gros tick_volume + petit corps de bougie.

        Le volume moyen est calculé DYNAMIQUEMENT sur les 20 barres précédentes
        (pas de valeur hardcodée — fix du bug critique de la spec originale).

        Absorption BEARISH :
            Prix montait (pré-tendance) → grosse bougie à gros volume + petit corps
            → vendeurs institutionnels absorbent les achats → risque retournement bas

        Absorption BULLISH :
            Prix baissait → grosse bougie à gros volume + petit corps
            → acheteurs institutionnels absorbent les ventes → risque retournement haut
        """
        N_RECENT = 3   # Bougies récentes à inspecter
        N_PRE = 4      # Bougies précédentes pour détecter la pré-tendance

        if len(df_m1) < N_RECENT + N_PRE + 5:
            return {'detected': False}

        # Volume moyen dynamique (excluant les N_RECENT barres courantes)
        avg_vol = df_m1['tick_volume'].iloc[-(N_RECENT + self.VOL_LOOKBACK):-N_RECENT].mean()
        if avg_vol <= 0:
            return {'detected': False}

        recent = df_m1.tail(N_RECENT)

        for idx in range(len(recent)):
            row = recent.iloc[idx]
            vol = float(row['tick_volume'])
            o = float(row['open'])
            h = float(row['high'])
            l = float(row['low'])
            c = float(row['close'])

            bar_range = h - l
            if bar_range <= 0:
                continue

            body_ratio = abs(c - o) / bar_range

            if vol > avg_vol * self.ABSORPTION_VOL_MULT and body_ratio < self.ABSORPTION_BODY_RATIO:
                # Pré-tendance : 4 barres avant cette bougie
                offset = len(recent) - idx
                pre = df_m1.iloc[-(offset + N_PRE):-offset] if offset > 0 else df_m1.iloc[-N_PRE - N_RECENT:-N_RECENT]
                if len(pre) < 2:
                    continue

                pre_slope = float(pre['close'].iloc[-1]) - float(pre['close'].iloc[0])
                strength = min(1.0, (vol / avg_vol) * (1.0 - body_ratio) * 0.7)

                if pre_slope > 0:
                    # Prix montait → absorption BEARISH
                    return {
                        'detected': True,
                        'direction': 'BEARISH',
                        'strength': round(strength, 3),
                        'reason': (
                            f"ABSORPTION_BEARISH: vol={vol:.0f}/{avg_vol:.0f} "
                            f"corps={body_ratio:.0%}"
                        ),
                    }
                elif pre_slope < 0:
                    # Prix baissait → absorption BULLISH
                    return {
                        'detected': True,
                        'direction': 'BULLISH',
                        'strength': round(strength, 3),
                        'reason': (
                            f"ABSORPTION_BULLISH: vol={vol:.0f}/{avg_vol:.0f} "
                            f"corps={body_ratio:.0%}"
                        ),
                    }

        return {'detected': False}

    # ═══════════════════════════════════════════════════════════════════════
    # SIGNAL 3 — Épuisement delta
    # ═══════════════════════════════════════════════════════════════════════

    def _detect_delta_exhaustion(
        self, df_m1: pd.DataFrame, delta_value: float, point: float
    ) -> dict:
        """
        Delta net fort mais mouvement de prix insuffisant → épuisement.

        Ex : delta = +200 (net achats) mais prix bouge < 2 pips sur 5 barres
        → les vendeurs absorbent les achats → signal BEARISH

        Le seuil delta est calculé DYNAMIQUEMENT (p70 de l'historique)
        si la colonne 'delta' est présente dans df_m1 (ajoutée par OrderFlow V6).
        Sinon, fallback sur seuil absolu.
        """
        if abs(delta_value) < 1e-6:
            return {'detected': False}

        # Mouvement prix sur les 5 dernières barres M1
        n = min(5, len(df_m1))
        price_move_pips = abs(
            float(df_m1['close'].iloc[-1]) - float(df_m1['close'].iloc[-n])
        ) / point

        # Seuil delta dynamique
        delta_threshold = 30.0  # fallback
        if 'delta' in df_m1.columns:
            hist_delta_abs = df_m1['delta'].dropna().abs()
            if len(hist_delta_abs) >= 10:
                delta_threshold = float(hist_delta_abs.quantile(0.70))

        delta_is_strong = abs(delta_value) >= delta_threshold
        price_is_weak = price_move_pips < self.DELTA_MOVE_MIN_PIPS

        if delta_is_strong and price_is_weak:
            direction = 'BEARISH' if delta_value > 0 else 'BULLISH'
            strength = min(1.0, abs(delta_value) / max(delta_threshold * 2.0, 1.0))
            return {
                'detected': True,
                'direction': direction,
                'strength': round(strength, 3),
                'reason': (
                    f"DELTA_EXHAUSTION_{direction}: delta={delta_value:.0f} "
                    f"mvt={price_move_pips:.1f}p (seuil={delta_threshold:.0f})"
                ),
            }

        return {'detected': False}

    # ═══════════════════════════════════════════════════════════════════════
    # SIGNAL 4 — Rejet Ichimoku
    # ═══════════════════════════════════════════════════════════════════════

    def _detect_ichimoku_rejection(
        self,
        df_m1: pd.DataFrame,
        ichimoku_result: Optional[dict],
        point: float,
    ) -> dict:
        """
        Détecte un rejet sur Tenkan ou Kijun en lisant ichimoku_result existant.

        Rejet de RÉSISTANCE (BEARISH) :
            - Le HIGH de la dernière bougie M1 a touché le Tenkan/Kijun (± seuil pips)
            - La bougie est ROUGE (close < open) → rejetée vers le bas

        Rejet de SUPPORT (BULLISH) :
            - Le LOW de la dernière bougie M1 a touché le Tenkan/Kijun (± seuil pips)
            - La bougie est VERTE (close > open) → rejetée vers le haut

        Priorité : M5 > M1 (M5 = niveau plus fiable pour le scalping)
        """
        if not ichimoku_result or not ichimoku_result.get('available'):
            return {'detected': False}

        last = df_m1.iloc[-1]
        o = float(last['open'])
        h = float(last['high'])
        l = float(last['low'])
        c = float(last['close'])
        is_green = c > o

        for tf in ('m5', 'm1'):
            tf_data = ichimoku_result.get(tf)
            if not tf_data:
                continue

            tenkan = tf_data.get('tenkan')
            kijun = tf_data.get('kijun')

            # ── Rejet sur Kijun (niveau le plus fort) ──────────────────────
            if kijun is not None:
                dist_h = (h - kijun) / point  # positif si le HIGH est au-dessus
                dist_l = (kijun - l) / point  # positif si le LOW est en-dessous

                # BEARISH rejection : high a touché Kijun, bougie rouge, close sous Kijun
                if (0 <= dist_h < self.KIJUN_THRESHOLD_PIPS
                        and not is_green and c < kijun):
                    return {
                        'detected': True,
                        'direction': 'BEARISH',
                        'strength': 0.85,
                        'reason': (
                            f"ICHIMOKU_REJET_KIJUN_BEARISH ({tf.upper()}, "
                            f"h-kijun={dist_h:.1f}p)"
                        ),
                    }

                # BULLISH rejection : low a touché Kijun, bougie verte, close au-dessus Kijun
                if (0 <= dist_l < self.KIJUN_THRESHOLD_PIPS
                        and is_green and c > kijun):
                    return {
                        'detected': True,
                        'direction': 'BULLISH',
                        'strength': 0.85,
                        'reason': (
                            f"ICHIMOKU_REJET_KIJUN_BULLISH ({tf.upper()}, "
                            f"kijun-l={dist_l:.1f}p)"
                        ),
                    }

            # ── Rejet sur Tenkan (niveau secondaire) ───────────────────────
            if tenkan is not None:
                dist_h = (h - tenkan) / point
                dist_l = (tenkan - l) / point

                # BEARISH rejection
                if (0 <= dist_h < self.TENKAN_THRESHOLD_PIPS
                        and not is_green and c < tenkan):
                    return {
                        'detected': True,
                        'direction': 'BEARISH',
                        'strength': 0.70,
                        'reason': (
                            f"ICHIMOKU_REJET_TENKAN_BEARISH ({tf.upper()}, "
                            f"h-tenkan={dist_h:.1f}p)"
                        ),
                    }

                # BULLISH rejection
                if (0 <= dist_l < self.TENKAN_THRESHOLD_PIPS
                        and is_green and c > tenkan):
                    return {
                        'detected': True,
                        'direction': 'BULLISH',
                        'strength': 0.70,
                        'reason': (
                            f"ICHIMOKU_REJET_TENKAN_BULLISH ({tf.upper()}, "
                            f"tenkan-l={dist_l:.1f}p)"
                        ),
                    }

        return {'detected': False}

    # ═══════════════════════════════════════════════════════════════════════
    # UTILITAIRES
    # ═══════════════════════════════════════════════════════════════════════

    def _compute_direction(self, signals: List[dict]) -> str:
        """
        Vote pondéré par strength pour déterminer la direction majoritaire.
        Seuil ×1.2 pour éviter les égalités fragiles (héritage de la V8 IRD).
        """
        bullish_score = 0.0
        bearish_score = 0.0

        for s in signals:
            strength = s.get('strength', 0.5)
            d = s.get('direction', 'NEUTRAL')
            if d == 'BULLISH':
                bullish_score += strength
            elif d == 'BEARISH':
                bearish_score += strength

        if bullish_score == 0.0 and bearish_score == 0.0:
            return 'NEUTRAL'

        if bullish_score > bearish_score * 1.2:
            return 'BULLISH'
        elif bearish_score > bullish_score * 1.2:
            return 'BEARISH'

        return 'NEUTRAL'
