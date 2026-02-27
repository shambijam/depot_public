"""
IchimokuAnalyzer — Analyse Ichimoku Light (Tenkan + Kijun)

27 FEV 2026 — Garde-fou + Scoring

Fournit :
  1. Garde-fou : détection zones critiques (hauts/bas récents 5 bougies)
  2. Scoring   : position, cross, rebond Kijun (bonus/malus via advanced_scoring §13)

Seuls Tenkan (9 périodes) et Kijun (26 périodes) sont utilisés.
Données minimales : 26 bougies (KIJUN_PERIOD). Si < 26 bougies → tf: None.

Pip universel : distance_pips = abs(price - level) / point
    → fonctionne USDJPY (point=0.01), EURUSD (point=0.0001), USDCHF (point=0.0001)
"""

from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)


class IchimokuAnalyzer:
    TENKAN_PERIOD = 9
    KIJUN_PERIOD = 26
    ZONE_LOOKBACK = 5  # Nombre de bougies pour détecter zone critique

    def __init__(self, logger=None):
        self._log = logger or logging.getLogger(__name__)

    def analyze(
        self,
        df_m5: Optional[pd.DataFrame],
        df_m1: Optional[pd.DataFrame],
        current_price: float,
        point: float,
        asset: str = "",
    ) -> dict:
        """
        Analyse Ichimoku Light sur M5 et M1.

        Returns:
            {
              'm5': {tenkan, kijun, position, cross_signal,
                     kijun_distance_pips, kijun_bounce, zone} | None,
              'm1': {tenkan, kijun, position, cross_signal,
                     kijun_distance_pips, kijun_bounce} | None,
              'available': bool
            }
        """
        result = {"m5": None, "m1": None, "available": False}

        # ── Analyse M5 ──────────────────────────────────────────────────────
        m5_result = None
        if df_m5 is not None and len(df_m5) >= self.KIJUN_PERIOD:
            try:
                m5_result = self._analyze_tf(df_m5, current_price, point)
                if m5_result:
                    m5_result["zone"] = self._detect_critical_zone(df_m5, current_price, point)
                    self._log.info(
                        f"[ICHIMOKU][{asset}][M5] "
                        f"Tenkan={m5_result['tenkan']:.5f} Kijun={m5_result['kijun']:.5f} "
                        f"Pos={m5_result['position']} Cross={m5_result['cross_signal']} "
                        f"Kijun_dist={m5_result['kijun_distance_pips']:.1f}p "
                        f"Zone={m5_result['zone']['type']}(lvl{m5_result['zone']['level']})"
                    )
            except Exception as e:
                self._log.debug(f"[ICHIMOKU][{asset}] Erreur M5: {e}")
                m5_result = None

        # ── Analyse M1 ──────────────────────────────────────────────────────
        m1_result = None
        if df_m1 is not None and len(df_m1) >= self.KIJUN_PERIOD:
            try:
                m1_result = self._analyze_tf(df_m1, current_price, point)
                if m1_result:
                    self._log.info(
                        f"[ICHIMOKU][{asset}][M1] "
                        f"Tenkan={m1_result['tenkan']:.5f} Kijun={m1_result['kijun']:.5f} "
                        f"Pos={m1_result['position']} Cross={m1_result['cross_signal']}"
                    )
            except Exception as e:
                self._log.debug(f"[ICHIMOKU][{asset}] Erreur M1: {e}")
                m1_result = None

        result["m5"] = m5_result
        result["m1"] = m1_result
        result["available"] = (m5_result is not None) or (m1_result is not None)
        return result

    # ════════════════════════════════════════════════════════════════════════
    # Méthodes internes
    # ════════════════════════════════════════════════════════════════════════

    def _analyze_tf(
        self, df: pd.DataFrame, current_price: float, point: float
    ) -> Optional[dict]:
        """Calcule les indicateurs Ichimoku sur un timeframe."""
        if df is None or len(df) < self.KIJUN_PERIOD:
            return None

        tenkan = self._tenkan(df)
        kijun = self._kijun(df)
        if tenkan is None or kijun is None:
            return None

        position = self._get_position(current_price, tenkan, kijun)
        cross_signal = self._detect_cross(df, point)
        kijun_bounce = self._detect_kijun_bounce(df, kijun, current_price)
        kijun_distance_pips = abs(current_price - kijun) / point if point > 0 else 0.0

        return {
            "tenkan": tenkan,
            "kijun": kijun,
            "position": position,
            "cross_signal": cross_signal,
            "kijun_distance_pips": round(kijun_distance_pips, 1),
            "kijun_bounce": kijun_bounce,
        }

    def _tenkan(self, df: pd.DataFrame) -> Optional[float]:
        """Tenkan-sen = (highest high + lowest low) / 2 sur TENKAN_PERIOD périodes."""
        if len(df) < self.TENKAN_PERIOD:
            return None
        h_col = "high" if "high" in df.columns else None
        l_col = "low" if "low" in df.columns else None
        if h_col is None or l_col is None:
            return None
        period_df = df.tail(self.TENKAN_PERIOD)
        return (period_df[h_col].max() + period_df[l_col].min()) / 2.0

    def _kijun(self, df: pd.DataFrame) -> Optional[float]:
        """Kijun-sen = (highest high + lowest low) / 2 sur KIJUN_PERIOD périodes."""
        if len(df) < self.KIJUN_PERIOD:
            return None
        h_col = "high" if "high" in df.columns else None
        l_col = "low" if "low" in df.columns else None
        if h_col is None or l_col is None:
            return None
        period_df = df.tail(self.KIJUN_PERIOD)
        return (period_df[h_col].max() + period_df[l_col].min()) / 2.0

    def _get_position(self, price: float, tenkan: float, kijun: float) -> str:
        """
        Position du prix par rapport à Tenkan et Kijun.

        ABOVE_BOTH        : prix > tenkan ET prix > kijun
        BELOW_BOTH        : prix < tenkan ET prix < kijun
        BETWEEN_ABOVE_T   : prix > tenkan mais prix < kijun
        BETWEEN_ABOVE_K   : prix < tenkan mais prix > kijun
        """
        above_tenkan = price > tenkan
        above_kijun = price > kijun

        if above_tenkan and above_kijun:
            return "ABOVE_BOTH"
        elif not above_tenkan and not above_kijun:
            return "BELOW_BOTH"
        elif above_tenkan and not above_kijun:
            return "BETWEEN_ABOVE_T"
        else:
            return "BETWEEN_ABOVE_K"

    def _detect_cross(self, df: pd.DataFrame, point: float) -> str:
        """
        Détecte un croisement Tenkan/Kijun entre l'avant-dernière et la dernière bougie.

        GOLDEN_CROSS : Tenkan vient de passer au-dessus de Kijun.
        DEATH_CROSS  : Tenkan vient de passer en-dessous de Kijun.
        NO_CROSS     : Aucun croisement récent.

        Seuil anti-bruit : 1 pip minimum.
        """
        if len(df) < self.KIJUN_PERIOD + 1:
            return "NO_CROSS"

        df_prev = df.iloc[:-1]
        tenkan_prev = self._tenkan(df_prev)
        kijun_prev = self._kijun(df_prev)
        tenkan_curr = self._tenkan(df)
        kijun_curr = self._kijun(df)

        if None in (tenkan_prev, kijun_prev, tenkan_curr, kijun_curr):
            return "NO_CROSS"

        threshold = point  # 1 pip minimum pour filtrer le bruit

        prev_above = (tenkan_prev - kijun_prev) > threshold
        prev_below = (kijun_prev - tenkan_prev) > threshold
        curr_above = (tenkan_curr - kijun_curr) > threshold
        curr_below = (kijun_curr - tenkan_curr) > threshold

        if not prev_above and curr_above:
            return "GOLDEN_CROSS"
        elif not prev_below and curr_below:
            return "DEATH_CROSS"
        return "NO_CROSS"

    def _detect_kijun_bounce(
        self, df: pd.DataFrame, kijun: float, current_price: float
    ) -> bool:
        """
        Détecte un rebond sur la Kijun :
        - BUY bounce  : bougie précédente close < kijun, prix actuel > kijun
        - SELL bounce : bougie précédente close > kijun, prix actuel < kijun
        """
        if len(df) < 2:
            return False
        c_col = "close" if "close" in df.columns else None
        if c_col is None:
            return False
        prev_close = df.iloc[-2][c_col]
        return (
            (prev_close < kijun and current_price > kijun) or
            (prev_close > kijun and current_price < kijun)
        )

    def _detect_critical_zone(
        self, df: pd.DataFrame, current_price: float, point: float
    ) -> dict:
        """
        Détecte si le prix est proche d'un plus haut ou plus bas récent
        (ZONE_LOOKBACK dernières bougies).

        Niveaux :
            3 = < 2 pips de recent_high OU recent_low
            2 = 2-5 pips de recent_high OU recent_low
            1 = prix déjà au-dessus du high (BREAKOUT) ou en-dessous du low (BREAKDOWN)
            0 = NONE

        Types : RESISTANCE_MAJEURE | RESISTANCE | BREAKOUT |
                SUPPORT_MAJEUR | SUPPORT | BREAKDOWN | NONE

        Retourne :
            {level, type, distance_high_pips, distance_low_pips, recent_high, recent_low}
        """
        empty = {
            "level": 0,
            "type": "NONE",
            "distance_high_pips": 999.0,
            "distance_low_pips": 999.0,
            "recent_high": 0.0,
            "recent_low": 0.0,
        }

        if df is None or len(df) < self.ZONE_LOOKBACK or point <= 0:
            return empty

        h_col = "high" if "high" in df.columns else None
        l_col = "low" if "low" in df.columns else None
        if h_col is None or l_col is None:
            return empty

        recent = df.tail(self.ZONE_LOOKBACK)
        recent_high = recent[h_col].max()
        recent_low = recent[l_col].min()

        # Distances en pips (>0 : prix en-dessous du high / au-dessus du low)
        distance_high_pips = (recent_high - current_price) / point
        distance_low_pips = (current_price - recent_low) / point

        base = {
            "distance_high_pips": round(distance_high_pips, 1),
            "distance_low_pips": round(distance_low_pips, 1),
            "recent_high": recent_high,
            "recent_low": recent_low,
        }

        # Prix déjà au-dessus du high → BREAKOUT (pas de veto BUY)
        if distance_high_pips < 0:
            return {**base, "level": 1, "type": "BREAKOUT",
                    "distance_high_pips": round(abs(distance_high_pips), 1)}

        # Prix déjà en-dessous du low → BREAKDOWN (pas de veto SELL)
        if distance_low_pips < 0:
            return {**base, "level": 1, "type": "BREAKDOWN",
                    "distance_low_pips": round(abs(distance_low_pips), 1)}

        # RESISTANCE_MAJEURE : < 2 pips du high (priorité sur SUPPORT)
        if distance_high_pips < 2.0:
            return {**base, "level": 3, "type": "RESISTANCE_MAJEURE"}

        # SUPPORT_MAJEUR : < 2 pips du low
        if distance_low_pips < 2.0:
            return {**base, "level": 3, "type": "SUPPORT_MAJEUR"}

        # RESISTANCE : 2-5 pips du high
        if distance_high_pips < 5.0:
            return {**base, "level": 2, "type": "RESISTANCE"}

        # SUPPORT : 2-5 pips du low
        if distance_low_pips < 5.0:
            return {**base, "level": 2, "type": "SUPPORT"}

        # NONE : > 5 pips des deux extrémités
        return {**base, "level": 0, "type": "NONE"}
