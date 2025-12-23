# scalping.py — version Burst-Only (no Bollinger / no Katana)
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
import math
import time
import uuid
from .base_strategy import BaseStrategy
import numpy as np
import pandas as pd
from phase_observer.vwap.config import (
    get_regime_weights,
)  # ✅ AJOUTÉ: Poids VWAP dynamiques


class USDJPYTimingOptimizer:
    """
    ⏱️ Optimiseur spécifique pour USDJPY Scalping Timing Analyzer

    Date: 18 Décembre 2025
    Objectif: Transformer le timing analyzer en filtre décisif via système multiplicateur + veto

    Problème résolu:
    - Impact linéaire trop faible (+1.9pts = +6% seulement)
    - Absence de veto pour timing catastrophique
    - Incohérence: trades exécutés avec POOR timing, rejetés avec EXCELLENT timing
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        """Initialise avec config depuis config_trade_scalping.json"""
        if config:
            self.config = config
        else:
            self.config = self._load_config()

    def _load_config(self) -> Dict[str, Any]:
        """Charge config depuis fichier JSON"""
        import json
        from pathlib import Path

        config_path = Path(__file__).parent.parent / "config" / "strategy" / "config_trade_scalping.json"

        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                full_config = json.load(f)
            return full_config.get("timing_optimizer_config", {})
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f"[TIMING_OPTIMIZER] Erreur config: {e}")
            # Fallback
            return {
                "concentration": {"excellent": 0.50, "good": 0.35, "warning": 0.25, "veto": 0.20},
                "velocity": {"buy_dominant": 1.8, "sell_dominant": 0.6, "extreme": 3.0},
                "multipliers": {"EXCELLENT": 1.20, "GOOD": 1.10, "FAIR": 1.00, "POOR": 0.80, "VETO": 0.50},
                "veto_conditions": {"timing_score_threshold": 1.5, "concentration_threshold": 0.20, "velocity_ratio_extreme": 4.0, "poor_score_threshold": 1.0},
                "bonuses_malus": {"concentration_strong_threshold": 0.50, "concentration_strong_bonus": 0.05, "concentration_weak_threshold": 0.25, "concentration_weak_malus": -0.05, "velocity_buy_coherent": 1.5, "velocity_sell_coherent": 0.7, "coherence_bonus": 0.03, "velocity_extreme": 2.5, "velocity_extreme_malus": -0.02},
                "multiplier_clamp": {"min": 0.5, "max": 1.5}
            }

    def calculate_impact(self, timing_metrics: Dict[str, Any]) -> Dict[str, Any]:
        """
        Calcule l'impact réel du timing sur le score via système multiplicateur.

        Args:
            timing_metrics: Métriques du timing analyzer

        Returns:
            Dict avec multiplier, veto_triggered, raison
        """
        timing_score = timing_metrics.get("timing_score", 0)
        timing_quality = timing_metrics.get("timing_quality", "FAIR")
        buy_conc = timing_metrics.get("buy_concentration_q1", 0)
        sell_conc = timing_metrics.get("sell_concentration_q1", 0)
        velocity_ratio = timing_metrics.get("velocity_ratio", 1.0)

        # ========================================================================
        # 1️⃣ VÉRIFICATION VETO (conditions catastrophiques) - depuis config
        # ========================================================================
        veto_cfg = self.config.get("veto_conditions", {})
        veto_conditions = [
            timing_score < veto_cfg.get("timing_score_threshold", 1.5) and max(buy_conc, sell_conc) < veto_cfg.get("concentration_threshold", 0.20),
            timing_score < 2.0 and velocity_ratio > veto_cfg.get("velocity_ratio_extreme", 4.0),
            timing_quality == "POOR" and timing_score < veto_cfg.get("poor_score_threshold", 1.0)
        ]

        multipliers_cfg = self.config.get("multipliers", {})
        if any(veto_conditions):
            return {
                "multiplier": multipliers_cfg.get("VETO", 0.50),
                "veto_triggered": True,
                "veto_reason": "Timing catastrophique: score faible + concentration insuffisante",
                "adjustment_details": f"VETO appliqué (×{multipliers_cfg.get('VETO', 0.50)})"
            }

        # ========================================================================
        # 2️⃣ MULTIPLICATEUR DE BASE selon qualité - depuis config
        # ========================================================================
        base_multiplier = multipliers_cfg.get(timing_quality, 1.0)

        # ========================================================================
        # 3️⃣ BONUS/MALUS SUPPLÉMENTAIRES - depuis config
        # ========================================================================
        adjustment = 0.0
        adjustment_reasons = []
        bonus_malus = self.config.get("bonuses_malus", {})

        # Bonus pour concentration forte
        conc_strong = bonus_malus.get("concentration_strong_threshold", 0.50)
        conc_strong_bonus = bonus_malus.get("concentration_strong_bonus", 0.05)
        if max(buy_conc, sell_conc) > conc_strong:
            adjustment += conc_strong_bonus
            adjustment_reasons.append(f"Concentration forte (>{conc_strong:.0%}): {conc_strong_bonus:+.0%}")

        # Bonus pour cohérence directionnelle (concentration + velocity alignées)
        vel_buy_coh = bonus_malus.get("velocity_buy_coherent", 1.5)
        vel_sell_coh = bonus_malus.get("velocity_sell_coherent", 0.7)
        coh_bonus = bonus_malus.get("coherence_bonus", 0.03)
        if buy_conc > sell_conc and velocity_ratio > vel_buy_coh:
            adjustment += coh_bonus
            adjustment_reasons.append(f"Cohérence buy (conc={buy_conc:.0%}, vel={velocity_ratio:.2f}): {coh_bonus:+.0%}")
        elif sell_conc > buy_conc and velocity_ratio < vel_sell_coh:
            adjustment += coh_bonus
            adjustment_reasons.append(f"Cohérence sell (conc={sell_conc:.0%}, vel={velocity_ratio:.2f}): {coh_bonus:+.0%}")

        # Malus pour concentration faible
        conc_weak = bonus_malus.get("concentration_weak_threshold", 0.25)
        conc_weak_malus = bonus_malus.get("concentration_weak_malus", -0.05)
        if max(buy_conc, sell_conc) < conc_weak:
            adjustment += conc_weak_malus
            adjustment_reasons.append(f"Concentration faible (<{conc_weak:.0%}): {conc_weak_malus:+.0%}")

        # Malus pour velocity ratio extrême (possible anomalie)
        vel_extreme = bonus_malus.get("velocity_extreme", 2.5)
        vel_extreme_malus = bonus_malus.get("velocity_extreme_malus", -0.02)
        if velocity_ratio > vel_extreme:
            adjustment += vel_extreme_malus
            adjustment_reasons.append(f"Velocity ratio extrême ({velocity_ratio:.2f}): {vel_extreme_malus:+.0%}")

        # ========================================================================
        # 4️⃣ CALCUL FINAL - avec clamp depuis config
        # ========================================================================
        final_multiplier = base_multiplier + adjustment

        # Clamp depuis config
        clamp_cfg = self.config.get("multiplier_clamp", {})
        min_mult = clamp_cfg.get("min", 0.5)
        max_mult = clamp_cfg.get("max", 1.5)
        final_multiplier = max(min_mult, min(max_mult, final_multiplier))

        adjustment_summary = (
            f"Base: {timing_quality}(×{base_multiplier:.2f}), "
            f"Ajustements: {adjustment:+.2f} → Final: ×{final_multiplier:.2f}"
        )
        if adjustment_reasons:
            adjustment_summary += f" | {', '.join(adjustment_reasons)}"

        return {
            "multiplier": final_multiplier,
            "veto_triggered": False,
            "veto_reason": None,
            "adjustment_details": adjustment_summary
        }


class MomentumAnalyzerInstitutional:
    """
    📊 Analyseur de Momentum Institutionnel M1 (Scalping uniquement)

    Date: 22 Décembre 2025
    Objectif: Momentum de qualité institutionnelle pour filtrer les trades incohérents

    Problème résolu:
    - Momentum pathétique (juste comptage bougies vertes)
    - Pas de volume, pas de force, pas d'accélération
    - Trades pris contre le momentum (BUY sur 3 bougies rouges)
    - Seuils hardcodés → maintenant dans config/momentum_institutional_config.json

    Score sur 100 points:
    - Candle Strength (30pts): Force des bougies, body ratio, position close
    - Volume Confirmation (25pts): Volume relatif, momentum volume
    - Price Acceleration (25pts): Accélération du mouvement
    - Multi-Timeframe Alignment (20pts): Concordance M1/M5/M15
    """

    def __init__(self, asset: str = "DEFAULT"):
        """Initialise avec config spécifique à l'asset."""
        self.asset = asset
        self.config = self._load_config(asset)

    def _load_config(self, asset: str) -> Dict[str, Any]:
        """Charge config depuis config_trade_scalping.json"""
        import json
        from pathlib import Path

        config_path = Path(__file__).parent.parent / "config" / "strategy" / "config_trade_scalping.json"

        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                full_config = json.load(f)

            # Charger config momentum_institutional_config (unique pour USDJPY actuellement)
            return full_config.get("momentum_institutional_config", {})

        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(
                f"[MOMENTUM] Erreur chargement config: {e}. Fallback."
            )
            # Fallback universel
            return {
                "candle_strength": {"body_ratio_multiplier": 18.0, "last_3_candles_multiplier": 7.5},
                "volume_confirmation": {"volume_ratio_multiplier": 18.75, "volume_momentum_multiplier": 20.0},
                "price_acceleration": {"move_pct_multiplier": 66.67, "acceleration_multiplier": 10.0},
                "mtf_alignment": {"bullish_threshold": 0.67, "bearish_threshold": 0.33},
                "quality_levels": {"excellent": 65, "good": 50, "fair": 35, "poor": 0}
            }

    def analyze(
        self,
        df_m1: pd.DataFrame,
        df_m3: Optional[pd.DataFrame] = None,
        df_m5: Optional[pd.DataFrame] = None,
    ) -> Dict[str, Any]:
        """
        Analyse le momentum institutionnel sur M1 avec confirmation M3+M5 (burst scalping).

        Args:
            df_m1: DataFrame M1 (minimum 20 bougies)
            df_m3: DataFrame M3 optionnel (confirmation burst - prioritaire pour USDJPY)
            df_m5: DataFrame M5 optionnel (contexte additionnel)

        Returns:
            Dict avec score total, direction, force, détails
        """
        import logging
        logger = logging.getLogger(__name__)

        logger.info(f"[MOMENTUM][{self.asset}] 🔍 analyze() appelé | df_m1={'None' if df_m1 is None else f'len={len(df_m1)}'} | df_m3={'None' if df_m3 is None else f'len={len(df_m3)}'} | df_m5={'None' if df_m5 is None else f'len={len(df_m5)}'}")

        if df_m1 is None:
            logger.warning(f"[MOMENTUM][{self.asset}] df_m1 is None → returning default result")
            return self._get_default_result()

        if len(df_m1) < 20:
            logger.warning(f"[MOMENTUM][{self.asset}] df_m1 too short (len={len(df_m1)} < 20) → returning default result")
            return self._get_default_result()

        try:
            # ====================================================================
            # 1️⃣ CANDLE STRENGTH ANALYSIS (0-30 pts)
            # ====================================================================
            candle_score, candle_details = self._analyze_candle_strength(df_m1)

            # ====================================================================
            # 2️⃣ VOLUME CONFIRMATION (0-25 pts)
            # ====================================================================
            volume_score, volume_details = self._analyze_volume_confirmation(df_m1)

            # ====================================================================
            # 3️⃣ PRICE ACCELERATION (0-25 pts)
            # ====================================================================
            acceleration_score, acceleration_details = self._analyze_price_acceleration(
                df_m1
            )

            # ====================================================================
            # 4️⃣ MULTI-TIMEFRAME ALIGNMENT (0-20 pts)
            # ====================================================================
            mtf_score, mtf_details = self._analyze_mtf_alignment(df_m1, df_m3, df_m5)

            # ====================================================================
            # 5️⃣ CALCUL SCORE TOTAL & DIRECTION
            # ====================================================================
            total_score = candle_score + volume_score + acceleration_score + mtf_score

            # Déterminer direction dominante
            direction = candle_details.get("direction", "NEUTRAL")

            # Déterminer qualité (depuis config)
            quality_cfg = self.config.get("quality_levels", {})
            if total_score >= quality_cfg.get("excellent", 65):
                quality = "EXCELLENT"
            elif total_score >= quality_cfg.get("good", 50):
                quality = "GOOD"
            elif total_score >= quality_cfg.get("fair", 35):
                quality = "FAIR"
            else:
                quality = "POOR"

            return {
                "total_score": round(total_score, 1),
                "direction": direction,
                "quality": quality,
                "candle_score": round(candle_score, 1),
                "volume_score": round(volume_score, 1),
                "acceleration_score": round(acceleration_score, 1),
                "mtf_score": round(mtf_score, 1),
                "candle_details": candle_details,
                "volume_details": volume_details,
                "acceleration_details": acceleration_details,
                "mtf_details": mtf_details,
            }

        except Exception as e:
            logger.error(f"[MOMENTUM] Exception during analysis: {type(e).__name__}: {e}", exc_info=True)
            return self._get_default_result()

    def _analyze_candle_strength(
        self, df_m1: pd.DataFrame
    ) -> Tuple[float, Dict[str, Any]]:
        """
        Analyse la force des bougies récentes (8 dernières).

        Critères:
        - Body ratio (corps vs mèches)
        - Range absolu
        - Position de la fermeture
        - Cohérence directionnelle
        """
        recent = df_m1.tail(8).copy()

        # Calculs de base
        recent["body"] = abs(recent["close"] - recent["open"])
        recent["range"] = recent["high"] - recent["low"]
        recent["body_ratio"] = recent["body"] / recent["range"].replace(0, 1e-9)
        recent["is_green"] = recent["close"] > recent["open"]

        # 1. Body Ratio moyen (0-10 pts)
        avg_body_ratio = recent["body_ratio"].mean()
        candle_cfg = self.config.get("candle_strength", {})
        body_multiplier = candle_cfg.get("body_ratio_multiplier", 18.0)
        body_ratio_score = min(10.0, avg_body_ratio * body_multiplier)

        # 2. Cohérence directionnelle (0-10 pts)
        green_count = recent["is_green"].sum()
        coherence = abs(green_count - 4) / 4.0  # Distance de 50%
        coherence_score = coherence * 10  # Max quand tout vert ou tout rouge

        # 3. Position de fermeture (0-5 pts)
        # Close près du high (bullish) ou low (bearish)
        def close_position(row):
            rng = row["high"] - row["low"]
            if rng < 1e-9:
                return 0.5
            if row["is_green"]:
                return (row["close"] - row["low"]) / rng
            else:
                return (row["high"] - row["close"]) / rng

        recent["close_pos"] = recent.apply(close_position, axis=1)
        avg_close_pos = recent["close_pos"].mean()
        close_pos_score = avg_close_pos * 5  # Max 5 pts

        # 4. Force des 3 dernières bougies (0-5 pts)
        last_3 = recent.tail(3)
        last_3_strength = last_3["body_ratio"].mean()
        last_3_multiplier = candle_cfg.get("last_3_candles_multiplier", 7.5)
        last_3_score = min(5.0, last_3_strength * last_3_multiplier)

        total_candle_score = (
            body_ratio_score + coherence_score + close_pos_score + last_3_score
        )

        # Direction
        if green_count >= 6:
            direction = "BULLISH"
        elif green_count <= 2:
            direction = "BEARISH"
        else:
            direction = "NEUTRAL"

        details = {
            "green_candles": int(green_count),
            "total_candles": 8,
            "avg_body_ratio": round(avg_body_ratio, 2),
            "coherence": round(coherence, 2),
            "avg_close_position": round(avg_close_pos, 2),
            "direction": direction,
        }

        return total_candle_score, details

    def _analyze_volume_confirmation(
        self, df_m1: pd.DataFrame
    ) -> Tuple[float, Dict[str, Any]]:
        """
        Analyse la confirmation par le volume.

        Critères:
        - Volume relatif (vs moyenne)
        - Momentum volume (croissant/décroissant)
        - Volume sur dernières bougies
        """
        recent = df_m1.tail(20).copy()

        # Volume moyen sur 20 bougies
        if "tick_volume" in recent.columns:
            vol_col = "tick_volume"
        elif "real_volume" in recent.columns:
            vol_col = "real_volume"
        else:
            # Pas de volume disponible
            return 0.0, {"volume_available": False}

        avg_volume = recent[vol_col].mean()
        last_8 = recent.tail(8)
        last_8_avg = last_8[vol_col].mean()

        # 1. Volume relatif (0-15 pts)
        volume_ratio = last_8_avg / max(avg_volume, 1.0)
        vol_cfg = self.config.get("volume_confirmation", {})
        vol_multiplier = vol_cfg.get("volume_ratio_multiplier", 18.75)
        volume_relative_score = min(15.0, (volume_ratio - 1.0) * vol_multiplier)
        volume_relative_score = max(0.0, volume_relative_score)

        # 2. Momentum volume (0-10 pts)
        # Volume croissant = bon signe
        first_4_vol = last_8.iloc[:4][vol_col].mean()
        last_4_vol = last_8.iloc[4:][vol_col].mean()
        vol_acceleration = (last_4_vol / max(first_4_vol, 1.0)) - 1.0
        vol_momentum_mult = vol_cfg.get("volume_momentum_multiplier", 20.0)
        vol_momentum_score = min(10.0, max(0.0, vol_acceleration * vol_momentum_mult))

        total_volume_score = volume_relative_score + vol_momentum_score

        details = {
            "volume_available": True,
            "volume_ratio": round(volume_ratio, 2),
            "volume_acceleration": round(vol_acceleration, 2),
            "avg_volume_20": round(avg_volume, 1),
            "avg_volume_8": round(last_8_avg, 1),
        }

        return total_volume_score, details

    def _analyze_price_acceleration(
        self, df_m1: pd.DataFrame
    ) -> Tuple[float, Dict[str, Any]]:
        """
        Analyse l'accélération du prix.

        Critères:
        - Mouvement total
        - Accélération (augmentation de vitesse)
        - Régularité
        """
        recent = df_m1.tail(12).copy()

        # Calcul mouvement cumulé
        recent["price_change"] = recent["close"] - recent["open"]

        # 1. Mouvement total (0-10 pts)
        total_move = recent["price_change"].sum()
        avg_price = recent["close"].mean()
        move_pct = abs(total_move) / max(avg_price, 1.0) * 100  # En %

        # Mouvement % (depuis config)
        accel_cfg = self.config.get("price_acceleration", {})
        move_multiplier = accel_cfg.get("move_pct_multiplier", 66.67)
        move_score = min(10.0, move_pct * move_multiplier)

        # 2. Accélération (0-10 pts)
        first_6_move = abs(recent.iloc[:6]["price_change"].sum())
        last_6_move = abs(recent.iloc[6:]["price_change"].sum())
        acceleration_ratio = last_6_move / max(first_6_move, 1e-9)
        accel_mult = accel_cfg.get("acceleration_multiplier", 10.0)
        accel_score = min(10.0, max(0.0, (acceleration_ratio - 1.0) * accel_mult))

        # 3. Régularité (0-5 pts)
        # Mouvement dans la même direction
        same_direction = (
            (recent["price_change"] > 0).sum()
            if total_move > 0
            else (recent["price_change"] < 0).sum()
        )
        regularity = same_direction / 12.0
        regularity_score = regularity * 5

        total_acceleration_score = move_score + accel_score + regularity_score

        details = {
            "total_move_pct": round(move_pct, 4),
            "acceleration_ratio": round(acceleration_ratio, 2),
            "regularity": round(regularity, 2),
            "direction_consistent": same_direction,
        }

        return total_acceleration_score, details

    def _analyze_mtf_alignment(
        self,
        df_m1: pd.DataFrame,
        df_m3: Optional[pd.DataFrame],
        df_m5: Optional[pd.DataFrame],
    ) -> Tuple[float, Dict[str, Any]]:
        """
        Analyse l'alignement multi-timeframe pour BURST SCALPING (M1+M3+M5).

        Critères:
        - Direction M1 (timeframe primaire, 50% weight = 10 pts max)
        - Direction M3 (confirmation burst, 35% weight = 7 pts max)
        - Direction M5 (contexte, 15% weight = 3 pts max)
        - Concordance entre timeframes

        Total: 20 pts max
        """
        # Charger config MTF une seule fois
        mtf_cfg = self.config.get("mtf_alignment", {})
        bullish_threshold = mtf_cfg.get("bullish_threshold", 0.67)
        bearish_threshold = mtf_cfg.get("bearish_threshold", 0.33)

        def get_direction(df, n_candles=6):
            if df is None or len(df) < n_candles:
                return "NEUTRAL"
            recent = df.tail(n_candles)
            green = (recent["close"] > recent["open"]).sum()
            if green >= n_candles * bullish_threshold:
                return "BULLISH"
            elif green <= n_candles * bearish_threshold:
                return "BEARISH"
            return "NEUTRAL"

        # Directions par timeframe (adapté burst scalping)
        m1_dir = get_direction(df_m1, 8)   # M1: 8 bougies
        m3_dir = get_direction(df_m3, 6) if df_m3 is not None else "N/A"  # M3: 6 bougies
        m5_dir = get_direction(df_m5, 5) if df_m5 is not None else "N/A"  # M5: 5 bougies

        # Calcul score avec pondération burst scalping
        score = 0.0

        # ✅ M1 Direction (50% = 10 pts max)
        if m1_dir in ["BULLISH", "BEARISH"]:
            score += 10.0  # M1 doit montrer une direction claire

        # ✅ M3 Concordance (35% = 7 pts max)
        if m3_dir != "N/A":
            if m3_dir == m1_dir:
                score += 7.0   # Alignement parfait M1-M3
            elif m3_dir == "NEUTRAL":
                score += 3.0   # M3 neutre acceptable (pas contre M1)

        # ✅ M5 Concordance (15% = 3 pts max)
        if m5_dir != "N/A":
            if m5_dir == m1_dir:
                score += 3.0   # M5 confirmé
            elif m5_dir == "NEUTRAL":
                score += 1.5   # M5 neutre acceptable

        # 🎯 Bonus alignement PARFAIT M1 = M3 = M5
        if m1_dir != "NEUTRAL" and m1_dir == m3_dir == m5_dir:
            score = 20.0  # Perfect alignment = score max

        details = {
            "m1_direction": m1_dir,
            "m3_direction": m3_dir,
            "m5_direction": m5_dir,
            "alignment": "FULL" if score >= 18 else "PARTIAL" if score >= 10 else "WEAK",
        }

        return score, details

    def _get_default_result(self) -> Dict[str, Any]:
        """Résultat par défaut en cas d'erreur."""
        return {
            "total_score": 0.0,
            "direction": "N/A",
            "quality": "N/A",
            "candle_score": 0.0,
            "volume_score": 0.0,
            "acceleration_score": 0.0,
            "mtf_score": 0.0,
            "candle_details": {},
            "volume_details": {},
            "acceleration_details": {},
            "mtf_details": {},
        }


class ScalpingStrategy(BaseStrategy):
    """
    Stratégie SCALPING focalisée sur :
    1) Burst Scalping (basket d’ordres simultanés) — priorité
    2) Liquidity Sweep (cassures HH/LL récentes) — optionnel

    ➤ AUCUNE règle basée sur la lecture de chandeliers (marubozu, patterns, etc.).
    ➤ Les tailles (volume) sont déléguées au TradeExecutor (risk-based).
    ➤ Les SL/TP sont gérés par le moteur SL/TP (RR dynamique) côté exécuteur.
    """

    def __init__(
        self,
        config_manager,
        strategy_config: Optional[Dict[str, Any]] = None,
        mt5_connector=None,  # 👈 ajouté ici
        logger=None,
    ):
        """
        Initialise la stratégie Scalping.
        """
        super().__init__(config_manager, strategy_config or {})

        self.config_manager = config_manager
        self.strategy_config = strategy_config or {}
        self.mt5_connector = mt5_connector  # ✅ plus d'erreur
        self.logger = logger or getattr(config_manager, "logger", None)

        # === CODE MORT SUPPRIMÉ (Session 23 Nov 2025) ===
        # L'instance Detectors n'était JAMAIS utilisée (grep "self.detectors." → 0 résultats)
        # Scalping utilise FootprintAnalyzer qui importe les 7 fonctions standalone
        # Les 8 méthodes de classe Detectors sont réservées à LiquidityStrategy

        # ⏱️ Timing Optimizer pour USDJPY (18 Dec 2025)
        self.USDJPY_timing_optimizer = USDJPYTimingOptimizer()

        # 📊 Momentum Analyzer Institutionnel (22 Dec 2025) - Cache par asset
        self.momentum_analyzers = {}  # Dict[asset] = MomentumAnalyzerInstitutional(asset)

        self.logger.info("Moteur de stratégie Scalping initialisé.")

    # ==========================================================
    # =============   API PRINCIPALE (ENTRÉE)   ================
    # ==========================================================
    # --- Dans class ScalpingStrategy(BaseStrategy): ---
    def _finalize_decision(
        self, decision: Dict[str, Any], analyzed_context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Normalise la décision avant retour au pipeline.
        Évite que le pipeline écrase une décision valide faute de champs attendus.
        """
        if not isinstance(decision, dict):
            return {}

        # Champs standard attendus par l'executor / pipeline
        decision.setdefault("strategy_type", "scalping")
        decision.setdefault("rule_name", decision.get("rule_name", "burst_scalping"))
        decision.setdefault("execution_status", "ready")  # prêt à exécuter
        decision.setdefault("confidence", float(decision.get("confidence", 0.0) or 0.0))

        # Optionnel: petit snapshot de contexte utile au debug
        ctx = analyzed_context or {}
        decision.setdefault(
            "context_snapshot",
            {
                "cycle": ctx.get("cycle_count"),
                "daily_trade_count": ctx.get("daily_trade_count"),
                "market_regime": ctx.get("market_regime"),
            },
        )

        return decision

    # ==========================================================
    # =========   ORDERFLOW V6 SCORING SYSTEM   ================
    # ==========================================================

    def _analyze_orderflow_v6(
        self,
        asset: str,
        df_m1: pd.DataFrame,
        df_m3: Optional[pd.DataFrame],
        df_m5: Optional[pd.DataFrame],
        asset_signals: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        📈 OrderFlow Analysis V6 - Multi-Timeframe

        Périodes STRICTES :
        • M1 : 8 bougies → Momentum immédiat
        • M3 : 6 bougies → Structure burst scalping
        • M5 : 4 bougies → Contexte court terme

        Focus principal :
        • Volume Profile : 15 bougies M1
        • Delta Analysis : 10 bougies M1
        • Imbalances : 5 bougies M1 + 8 bougies M3

        Retourne :
        {
            "delta_momentum_score": 0-25,
            "volume_confirmation_score": 0-15,
            "imbalance_strength_score": 0-10,
            "total_score": 0-50,
            "mtf_alignment": {"m1": "", "m3": "", "m5": ""},
            "details": {...}
        }
        """
        result = {
            "delta_momentum_score": 0.0,
            "volume_confirmation_score": 0.0,
            "imbalance_strength_score": 0.0,
            "total_score": 0.0,
            "mtf_alignment": {"m1": "neutral", "m5": "neutral", "m15": "neutral"},
            "details": {},
        }

        # Charger config OrderFlow V6
        of_config = self.strategy_config.get("orderflow_v6_config", {})
        coherence_thresholds = of_config.get("coherence_thresholds", {
            "strong": 0.75,
            "moderate": 0.65,
            "weak": 0.55
        })
        volume_classification = of_config.get("volume_ratio_classification", {
            "spike": 1.8,
            "elevated": 1.4,
            "above_average": 1.15,
            "normal": 0.85,
            "moderate": 0.6
        })
        score_status = of_config.get("total_score_status", {
            "strong_threshold": 28.0,
            "moderate_threshold": 14.0,
            "max_score": 50.0
        })
        absorption_ratios = of_config.get("absorption_ratios", {
            "very_bullish": 0.72,
            "bullish": 0.58,
            "slightly_bullish": 0.52,
            "neutral": 0.50,
            "slightly_bearish": 0.48,
            "bearish": 0.42,
            "very_bearish": 0.28
        })

        try:
            # ================================================================
            # 0. ANALYSE MULTI-TIMEFRAME (M1/M5/M15)
            # ================================================================
            mtf_details = {}

            # M1 : 8 bougies → Momentum immédiat
            if df_m1 is not None and len(df_m1) >= 8:
                m1_closes = df_m1["close"].tail(8).values
                m1_opens = df_m1["open"].tail(8).values
                m1_bullish = sum(
                    1 for i in range(len(m1_closes)) if m1_closes[i] > m1_opens[i]
                )
                m1_bearish = 8 - m1_bullish

                if m1_bullish >= 6:  # 6/8 haussier (75%)
                    result["mtf_alignment"]["m1"] = "bullish"
                elif m1_bearish >= 6:  # 6/8 baissier (75%)
                    result["mtf_alignment"]["m1"] = "bearish"
                else:
                    result["mtf_alignment"]["m1"] = "neutral"

                mtf_details["m1"] = {
                    "bullish_bars": m1_bullish,
                    "bearish_bars": m1_bearish,
                    "direction": result["mtf_alignment"]["m1"],
                }

            # M5 : 6 bougies → Structure court terme
            if df_m5 is not None and len(df_m5) >= 6:
                m5_closes = df_m5["close"].tail(6).values
                m5_opens = df_m5["open"].tail(6).values
                m5_bullish = sum(
                    1 for i in range(len(m5_closes)) if m5_closes[i] > m5_opens[i]
                )
                m5_bearish = 6 - m5_bullish

                if m5_bullish >= 5:  # 5/6 haussier (83%)
                    result["mtf_alignment"]["m5"] = "bullish"
                elif m5_bearish >= 5:  # 5/6 baissier (83%)
                    result["mtf_alignment"]["m5"] = "bearish"
                else:
                    result["mtf_alignment"]["m5"] = "neutral"

                mtf_details["m5"] = {
                    "bullish_bars": m5_bullish,
                    "bearish_bars": m5_bearish,
                    "direction": result["mtf_alignment"]["m5"],
                }

            # M15 : 4 bougies → Contexte moyen terme
            if df_m15 is not None and len(df_m15) >= 4:
                m15_closes = df_m15["close"].tail(4).values
                m15_opens = df_m15["open"].tail(4).values
                m15_bullish = sum(
                    1 for i in range(len(m15_closes)) if m15_closes[i] > m15_opens[i]
                )
                m15_bearish = 4 - m15_bullish

                if m15_bullish >= 3:  # 3/4 haussier
                    result["mtf_alignment"]["m15"] = "bullish"
                elif m15_bearish >= 3:  # 3/4 baissier
                    result["mtf_alignment"]["m15"] = "bearish"
                else:
                    result["mtf_alignment"]["m15"] = "neutral"

                mtf_details["m15"] = {
                    "bullish_bars": m15_bullish,
                    "bearish_bars": m15_bearish,
                    "direction": result["mtf_alignment"]["m15"],
                }

            result["details"]["mtf"] = mtf_details

            # Vérifier alignement multi-timeframe (bonus potentiel)
            mtf_aligned = False
            if (
                result["mtf_alignment"]["m1"]
                == result["mtf_alignment"]["m5"]
                == result["mtf_alignment"]["m15"]
                and result["mtf_alignment"]["m1"] != "neutral"
            ):
                mtf_aligned = True
                result["mtf_aligned"] = True
                self.logger.debug(
                    f"[{asset}] 🎯 MTF Alignment: {result['mtf_alignment']['m1'].upper()}"
                )

            # ================================================================
            # 1. DELTA MOMENTUM (25 points max)
            # ================================================================
            delta_momentum_score = 0.0
            delta_details = {}

            # ✅ FIX (1er Décembre 2025): orchestrator stocke SEULEMENT "summary" (pas la structure complète)
            # Donc fp_raw contient directement {delta_total, buy_volume, ...} sans imbrication
            fp_raw = asset_signals.get("footprint_summary", {})

            # Vérifier si c'est une structure imbriquée (avec "summary") ou directe
            if isinstance(fp_raw, dict):
                if "summary" in fp_raw:
                    # Structure complète (depuis fusion_manager ou autre source)
                    fp_summary = fp_raw["summary"]
                else:
                    # Structure directe (depuis orchestrator)
                    fp_summary = fp_raw
            else:
                fp_summary = {}

            # Extraire delta_total
            delta_total = 0
            if isinstance(fp_summary, dict):
                delta_total = float(fp_summary.get("delta_total", 0))

            # Stocker delta_total TOUJOURS (pour le rapport)
            delta_details["delta_total"] = delta_total

            # Analyser cohérence delta sur 10 bougies M1
            if df_m1 is not None and len(df_m1) >= 10:
                # Compter bougies avec delta cohérent
                closes = df_m1["close"].tail(10).values
                opens = df_m1["open"].tail(10).values
                bullish_count = sum(
                    1 for i in range(len(closes)) if closes[i] > opens[i]
                )
                bearish_count = sum(
                    1 for i in range(len(closes)) if closes[i] < opens[i]
                )

                coherence = max(bullish_count, bearish_count) / 10.0  # 0.0 à 1.0
                delta_details["coherence"] = coherence
                delta_details["bullish_bars"] = bullish_count
                delta_details["bearish_bars"] = bearish_count

                # Scoring Delta Momentum
                if abs(delta_total) > 0:
                    delta_direction = "bullish" if delta_total > 0 else "bearish"
                    delta_details["delta_total"] = delta_total
                    delta_details["direction"] = delta_direction

                    # ✅ FIX (03 DEC 2025): Seuils adaptés SCALPING M1 (ticks temps réel sur 60s)
                    # Delta fort cohérent → 15-25 pts
                    if coherence >= coherence_thresholds["strong"]:  # Strong coherence (config)
                        if (
                            abs(delta_total) >= 50
                        ):  # ~28% déséquilibre (ex: 114 buy / 66 sell sur 180 ticks)
                            delta_momentum_score = 25.0  # Très fort
                        elif (
                            abs(delta_total) >= 30
                        ):  # ~17% déséquilibre (ex: 105 buy / 75 sell)
                            delta_momentum_score = 20.0  # Fort
                        elif (
                            abs(delta_total) >= 15
                        ):  # ~8% déséquilibre (ex: 97 buy / 83 sell)
                            delta_momentum_score = 18.0  # Moyen-Fort
                        elif (
                            abs(delta_total) >= 5
                        ):  # ~3% déséquilibre (ex: 92 buy / 88 sell)
                            delta_momentum_score = 15.0  # Moyen
                    # Delta modéré → 10-15 pts
                    elif coherence >= coherence_thresholds["moderate"]:  # Moderate coherence (config)
                        if abs(delta_total) >= 30:
                            delta_momentum_score = 15.0
                        elif abs(delta_total) >= 15:
                            delta_momentum_score = 12.0
                        elif abs(delta_total) >= 5:
                            delta_momentum_score = 10.0
                    # Delta faible cohérence → 5-10 pts
                    elif coherence >= coherence_thresholds["weak"]:  # Weak coherence (config)
                        if abs(delta_total) >= 15:
                            delta_momentum_score = 10.0
                        else:
                            delta_momentum_score = 7.0
                    else:
                        delta_momentum_score = 5.0
                else:
                    delta_details["direction"] = "neutral"
                    delta_momentum_score = 5.0

            result["delta_momentum_score"] = delta_momentum_score
            result["delta_momentum_details"] = (
                delta_details  # FIX: Nom correct pour le rapport
            )

            # ================================================================
            # 2. VOLUME CONFIRMATION (15 points max)
            # ================================================================
            volume_confirmation_score = 0.0
            volume_details = {}

            # ✅ FIX (03 DEC 2025): Utiliser tick_count TEMPS RÉEL du footprint au lieu du DataFrame
            # Le DataFrame tick_volume est obsolète, le footprint tick_count est calculé en temps réel
            current_tick_count = 0
            if isinstance(fp_summary, dict):
                current_tick_count = int(fp_summary.get("tick_count", 0))

            # Calculer moyenne tick_count sur 14 bougies précédentes (via DataFrame)
            vol_col = None
            if df_m1 is not None and len(df_m1) >= 15:
                if "tick_volume" in df_m1.columns:
                    vol_col = "tick_volume"
                elif "volume" in df_m1.columns:
                    vol_col = "volume"

            if vol_col is not None and current_tick_count > 0:
                # Prendre 14 bougies COMPLÈTES pour moyenne (exclure la dernière qui pourrait être en cours)
                historical_volumes = (
                    df_m1[vol_col].tail(15).values[:-1]
                )  # 14 dernières complètes
                avg_volume = (
                    np.mean(historical_volumes) if len(historical_volumes) > 0 else 1.0
                )

                volume_ratio = (
                    current_tick_count / avg_volume if avg_volume > 0 else 1.0
                )
                volume_details["current_volume"] = float(current_tick_count)
                volume_details["avg_volume"] = float(avg_volume)
                volume_details["ratio"] = volume_ratio

                # ✅ POC depuis footprint_summary (VRAI POC calculé depuis profil de volume)
                poc_price = fp_summary.get("poc")
                if poc_price is not None and isinstance(poc_price, (int, float)):
                    volume_details["poc"] = float(poc_price)

                # Scoring Volume (seuils depuis config)
                if volume_ratio >= volume_classification["spike"]:  # Spike significatif
                    volume_confirmation_score = 15.0
                    volume_details["spike_detected"] = True
                elif volume_ratio >= volume_classification["elevated"]:  # Volume élevé
                    volume_confirmation_score = 12.0
                elif volume_ratio >= volume_classification["above_average"]:  # Volume au-dessus moyenne
                    volume_confirmation_score = 10.0
                elif volume_ratio >= volume_classification["normal"]:  # Volume normal
                    volume_confirmation_score = 7.0
                elif volume_ratio >= volume_classification["moderate"]:  # Volume modéré
                    volume_confirmation_score = 3.0
                else:  # Volume très faible
                    volume_confirmation_score = 0.0

            result["volume_confirmation_score"] = volume_confirmation_score
            result["volume_confirmation_details"] = (
                volume_details  # FIX: Nom correct pour le rapport
            )

            # ================================================================
            # 3. IMBALANCE STRENGTH (10 points max)
            # ================================================================
            imbalance_strength_score = 0.0
            imbalance_details = {}

            # ✅ Récupérer imbalances depuis fp_summary (DÉJÀ calculées par footprint_validator)
            if isinstance(fp_summary, dict):
                imbalance_buy = int(fp_summary.get("imbalance_buy", 0))
                imbalance_sell = int(fp_summary.get("imbalance_sell", 0))
                total_imbalances = imbalance_buy + imbalance_sell

                imbalance_details["imbalance_buy"] = imbalance_buy
                imbalance_details["imbalance_sell"] = imbalance_sell
                imbalance_details["m1_count"] = (
                    total_imbalances  # Les imbalances footprint sont M1
                )
                imbalance_details["m5_count"] = (
                    0  # TODO: Si besoin M5 séparé, ajouter au footprint_validator
                )
                imbalance_details["total_count"] = total_imbalances

                # Scoring basé sur les imbalances totales
                if total_imbalances >= 5:  # Beaucoup d'imbalances
                    imbalance_strength_score = 10.0
                elif total_imbalances >= 3:  # Imbalances significatives
                    imbalance_strength_score = 8.0
                elif total_imbalances >= 1:  # Imbalances mineures
                    imbalance_strength_score = 5.0
                else:
                    imbalance_strength_score = 0.0

            result["imbalance_strength_score"] = imbalance_strength_score
            result["imbalance_strength_details"] = (
                imbalance_details  # FIX: Nom correct pour le rapport
            )

            # ================================================================
            # TOTAL ORDERFLOW SCORE
            # ================================================================
            result["total_score"] = (
                delta_momentum_score
                + volume_confirmation_score
                + imbalance_strength_score
            )
            result["mtf_aligned"] = mtf_aligned

            self.logger.debug(
                f"[{asset}] OrderFlow V6: Delta={delta_momentum_score:.1f} "
                f"Volume={volume_confirmation_score:.1f} "
                f"Imbalance={imbalance_strength_score:.1f} "
                f"MTF={mtf_aligned} "
                f"→ Total={result['total_score']:.1f}/50"
            )

        except Exception as e:
            self.logger.error(
                f"[{asset}] OrderFlow V6 analysis error: {e}", exc_info=True
            )

        return result

    def calculate_orderflow_v6_standalone(
        self,
        asset: str,
        df_m1: pd.DataFrame,
        df_m5: Optional[pd.DataFrame],
        df_m15: Optional[pd.DataFrame],
        asset_signals: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        📈 Calcul OrderFlow V6 en mode standalone pour FusionManager.

        Appelle _analyze_orderflow_v6() et convertit le résultat en format
        compatible avec FusionManager.fuse().

        Returns:
            Dict compatible FusionManager:
            {
                "score": 0-100,  # Pourcentage pour FusionManager
                "status": "VALID"/"WEAK"/"SUSPECT",
                "summary": {
                    "delta_total": float,
                    "bias": "BUY"/"SELL"/"NEUTRAL",
                    "poc": float,
                    "imbalance_count": int,
                    "volume_ratio": float,
                    "mtf_alignment": dict
                },
                "total_score": 0-50,  # Score points pour rapport consolidé
                "details": dict  # Détails complets pour debugging
            }
        """
        # Charger config OrderFlow V6 pour les seuils de statut
        of_config = self.strategy_config.get("orderflow_v6_config", {})
        score_status = of_config.get("total_score_status", {
            "strong_threshold": 28.0,
            "moderate_threshold": 14.0,
            "max_score": 50.0
        })

        # Appeler la fonction d'analyse existante
        result = self._analyze_orderflow_v6(
            asset=asset,
            df_m1=df_m1,
            df_m5=df_m5,
            df_m15=df_m15,
            asset_signals=asset_signals,
        )

        # Extraire le score total (0-50 points)
        total_score = result.get("total_score", 0.0)

        # Convertir en pourcentage pour FusionManager (0-100)
        score_pct = (total_score / 50.0) * 100.0

        # Extraire détails pour construire summary
        details = result.get("details", {})
        delta_details = details.get("delta_momentum", {})
        volume_details = details.get("volume_confirmation", {})
        imbalance_details = details.get("imbalance_strength", {})

        # Construire summary pour FusionManager
        delta_total = delta_details.get("delta_total", 0.0)
        summary = {
            "delta_total": delta_total,
            "bias": (
                "BUY" if delta_total > 0 else "SELL" if delta_total < 0 else "NEUTRAL"
            ),
            "poc": volume_details.get("poc"),
            "vpoc_price": volume_details.get("poc"),  # Alias pour compatibilité
            "imbalance_count": imbalance_details.get("m1_count", 0),
            "volume_ratio": volume_details.get("ratio", 1.0),
            "mtf_alignment": result.get("mtf_alignment", {}),
            "spike_detected": volume_details.get("spike_detected", False),
        }

        # Déterminer status selon qualité du score (depuis config)
        if total_score >= score_status["strong_threshold"]:  # Strong status (config)
            status = "VALID"
        elif total_score >= score_status["moderate_threshold"]:  # Moderate status (config)
            status = "WEAK"
        else:
            status = "SUSPECT"

        # Format final pour FusionManager
        # ✅ FIX (12 Dec 2025): Aplatir raw_result pour que le rapport puisse lire les clés directement
        return {
            **result,  # Aplatir toutes les clés de _analyze_orderflow_v6 (scores, details, etc.)
            "score": score_pct,  # 0-100 pour FusionManager._normalize_orderflow()
            "status": status,
            "summary": summary,
            "total_score": total_score,  # 0-50 pour rapport consolidé (écrase result["total_score"] si identique)
            "details": details,  # Détails complets
            "raw_result": result,  # Résultat brut si besoin (pour debug)
        }

    def _analyze_footprint_v6(
        self, asset: str, df_m1: pd.DataFrame, asset_signals: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        👣 Footprint Analysis V6 - Ticks Temps Réel + ⏱️ Timing Analyzer

        Concentration sur bougie courante :
        • Analyse ticks en temps réel
        • Détection clusters d'ordres
        • Niveaux d'absorption critiques
        • ⏱️ Qualité timing (distribution temporelle)

        Contexte immédiat :
        • 3 bougies précédentes pour confirmation
        • Focus sur la bougie en cours (0-59 secondes)

        Retourne :
        {
            "absorption_levels_score": 0-12.5,
            "order_clustering_score": 0-8.5,
            "price_rejection_score": 0-4.0,
            "timing_score": 0-5.0,  # ⏱️ NOUVEAU (17 DEC 2025)
            "total_score": 0-30,    # ⏱️ MODIFIÉ (17 DEC 2025): 25→30
            "details": {...}
        }
        """
        result = {
            "absorption_levels_score": 0.0,
            "order_clustering_score": 0.0,
            "price_rejection_score": 0.0,
            "total_score": 0.0,
            "details": {},
        }

        # Charger config OrderFlow V6 pour absorption_ratios
        of_config = self.strategy_config.get("orderflow_v6_config", {})
        absorption_ratios = of_config.get("absorption_ratios", {
            "very_bullish": 0.72,
            "bullish": 0.58,
            "slightly_bullish": 0.52,
            "neutral": 0.50,
            "slightly_bearish": 0.48,
            "bearish": 0.42,
            "very_bearish": 0.28
        })

        try:
            # ✅ FIX (1er Décembre 2025): orchestrator stocke SEULEMENT "summary" (pas la structure complète)
            # Donc fp_raw contient directement {delta_total, buy_volume, ...} sans imbrication
            fp_raw = asset_signals.get("footprint_summary", {})

            # Vérifier si c'est une structure imbriquée (avec "summary") ou directe
            if isinstance(fp_raw, dict):
                if "summary" in fp_raw:
                    # Structure complète (depuis fusion_manager ou autre source)
                    fp_summary = fp_raw["summary"]
                else:
                    # Structure directe (depuis orchestrator)
                    fp_summary = fp_raw
            else:
                fp_summary = {}

            fp_status = asset_signals.get("footprint_status", "N/A").upper()

            # ================================================================
            # 1. ABSORPTION LEVELS (12.5 points max)
            # ================================================================
            absorption_score = 0.0
            absorption_details = {}

            # Récupérer les niveaux d'absorption depuis footprint
            buy_vol = 0
            sell_vol = 0

            # ✅ MÊME APPROCHE que fusion_manager.py (ligne 1008-1009) qui FONCTIONNAIT
            if isinstance(fp_summary, dict):
                buy_vol = float(fp_summary.get("buy_volume", 0))
                sell_vol = float(fp_summary.get("sell_volume", 0))

            total_vol = buy_vol + sell_vol

            # Stocker TOUJOURS les volumes (pour le rapport)
            absorption_details["buy_volume"] = buy_vol
            absorption_details["sell_volume"] = sell_vol

            if total_vol > 0:
                buy_ratio = buy_vol / total_vol
                sell_ratio = sell_vol / total_vol

                absorption_details["buy_ratio"] = buy_ratio
                absorption_details["sell_ratio"] = sell_ratio

                # ✅ SEUILS DEPUIS CONFIG (absorption_ratios)
                # Déterminer absorption basé sur buy_ratio
                if buy_ratio >= absorption_ratios["very_bullish"]:  # Fort bullish
                    absorption_score = 12.5
                    absorption_details["bias"] = "STRONG BULLISH"
                elif buy_ratio >= absorption_ratios["bullish"]:  # Bullish
                    absorption_score = 10.0
                    absorption_details["bias"] = "BULLISH"
                elif buy_ratio >= absorption_ratios["slightly_bullish"]:  # Légèrement bullish
                    absorption_score = 7.5
                    absorption_details["bias"] = "SLIGHTLY_BULLISH"
                elif buy_ratio <= absorption_ratios["very_bearish"]:  # Fort bearish (sell_ratio >= very_bullish)
                    absorption_score = 12.5
                    absorption_details["bias"] = "STRONG BEARISH"
                elif buy_ratio <= absorption_ratios["bearish"]:  # Bearish (sell_ratio >= bullish)
                    absorption_score = 10.0
                    absorption_details["bias"] = "BEARISH"
                elif buy_ratio <= absorption_ratios["slightly_bearish"]:  # Légèrement bearish (sell_ratio >= slightly_bullish)
                    absorption_score = 7.5
                    absorption_details["bias"] = "SLIGHTLY_BEARISH"
                else:
                    absorption_score = 4.0
                    absorption_details["bias"] = "NEUTRAL"

                # ✅ BONUS VOLUME : Forte activité → signal plus fiable
                if total_vol > 100:  # Fort volume
                    absorption_score = min(12.5, absorption_score * 1.3)  # +30%
                    absorption_details["volume_bonus"] = "high_volume"
                elif total_vol > 50:
                    absorption_score = min(12.5, absorption_score * 1.15)  # +15%
                    absorption_details["volume_bonus"] = "medium_volume"

                # ✅ BONUS TICK RATE : Activité récente forte
                tick_rate = fp_summary.get("tick_rate", 0)
                if isinstance(tick_rate, (int, float)):
                    if tick_rate > 2.0:  # Forte activité ticks
                        absorption_score = min(12.5, absorption_score * 1.2)  # +20%
                        absorption_details["tick_bonus"] = "high_activity"
                    elif tick_rate > 1.0:
                        absorption_score = min(12.5, absorption_score * 1.1)  # +10%
                        absorption_details["tick_bonus"] = "medium_activity"

            # ✅ FIX (2 Décembre 2025): Log de debug pour comprendre le calcul
            self.logger.debug(
                f"[{asset}] Absorption: buy_vol={buy_vol:.1f} sell_vol={sell_vol:.1f} "
                f"total_vol={total_vol:.1f} buy_ratio={absorption_details.get('buy_ratio', 0):.2%} "
                f"bias={absorption_details.get('bias', 'N/A')} absorption_score={absorption_score:.1f}"
            )

            result["absorption_levels_score"] = absorption_score
            result["absorption_details"] = (
                absorption_details  # FIX: Nom correct pour le rapport
            )

            # ================================================================
            # 2. ORDER CLUSTERING (8.5 points max)
            # ================================================================
            clustering_score = 0.0
            clustering_details = {}

            # Analyser 3 bougies précédentes pour confirmation
            if df_m1 is not None and len(df_m1) >= 4:
                # Compter clusters (approximation via volume concentré)
                volumes = df_m1["tick_volume"].tail(4).values
                ranges = (df_m1["high"] - df_m1["low"]).tail(4).values

                # ✅ FIX (2 Décembre 2025): Calcul sécurisé évitant division par 0
                # Détect clusters : volume élevé + range faible = orders concentrés
                cluster_count = 0
                ranges_safe = np.maximum(ranges, 1e-9)  # Évite division by 0
                vol_per_pip_arr = volumes / ranges_safe
                median_vol_per_pip = np.median(vol_per_pip_arr)

                for i in range(len(volumes)):
                    if vol_per_pip_arr[i] > median_vol_per_pip:
                        cluster_count += 1

                clustering_details["cluster_count"] = cluster_count
                clustering_details["distribution"] = (
                    "concentrated" if cluster_count >= 2 else "dispersed"
                )

                # Log de debug
                self.logger.debug(
                    f"[{asset}] Clustering: cluster_count={cluster_count} "
                    f"median_vol_per_pip={median_vol_per_pip:.2f} "
                    f"vol_per_pip={[f'{v:.1f}' for v in vol_per_pip_arr]}"
                )

                # Scoring
                if cluster_count >= 3:
                    clustering_score = 8.5
                elif cluster_count >= 2:
                    clustering_score = 6.0
                elif cluster_count >= 1:
                    clustering_score = 3.5
                else:
                    clustering_score = 0.0

            result["order_clustering_score"] = clustering_score
            result["clustering_details"] = (
                clustering_details  # FIX: Nom correct pour le rapport
            )

            # ================================================================
            # 3. PRICE REJECTION (4.0 points max)
            # ================================================================
            rejection_score = 0.0
            rejection_details = {}

            # Analyser wicks pour détecter rejets
            if df_m1 is not None and len(df_m1) >= 3:
                last_bars = df_m1.tail(3)

                rejection_count = 0
                for idx, row in last_bars.iterrows():
                    high_val = row["high"]
                    low_val = row["low"]
                    open_val = row["open"]
                    close_val = row["close"]

                    body = abs(close_val - open_val)
                    full_range = high_val - low_val

                    if full_range > 0:
                        upper_wick = high_val - max(open_val, close_val)
                        lower_wick = min(open_val, close_val) - low_val

                        wick_ratio = (
                            max(upper_wick, lower_wick) / body if body > 0 else 0
                        )

                        # Rejet net si wick > 2x body
                        if wick_ratio >= 2.0:
                            rejection_count += 1

                rejection_details["rejection_bars"] = rejection_count

                # Scoring
                if rejection_count >= 3:
                    rejection_score = 4.0
                    rejection_details["strength"] = "strong"
                elif rejection_count >= 2:
                    rejection_score = 2.5
                    rejection_details["strength"] = "moderate"
                elif rejection_count >= 1:
                    rejection_score = 1.5
                    rejection_details["strength"] = "weak"
                else:
                    rejection_score = 0.0
                    rejection_details["strength"] = "none"

            result["price_rejection_score"] = rejection_score
            result["rejection_details"] = (
                rejection_details  # FIX: Nom correct pour le rapport
            )

            # ================================================================
            # TOTAL FOOTPRINT SCORE avec système multiplicateur timing (18 Dec 2025)
            # ================================================================
            # ⏱️ Récupérer timing_score depuis footprint_summary
            timing_metrics = fp_summary.get("timing_metrics", {})
            timing_score = float(timing_metrics.get("timing_score", 0.0))
            timing_quality = timing_metrics.get("timing_quality", "N/A")

            # Score de base (0-25 pts)
            base_score = absorption_score + clustering_score + rejection_score

            # ========================================================================
            # ⏱️ SYSTÈME MULTIPLICATEUR TIMING (USDJPY uniquement)
            # ========================================================================
            # Problème résolu: Impact linéaire trop faible (avant: +1.9pts = +6%)
            # Nouveau: Système multiplicateur avec veto (impact: ±15-25%)
            timing_impact = None
            timing_decision = "NO_TIMING"  # Par défaut

            if asset == "USDJPY" and timing_metrics:
                # Calculer impact timing via optimizer
                timing_impact = self.USDJPY_timing_optimizer.calculate_impact(timing_metrics)

                # Appliquer multiplicateur
                multiplier = timing_impact.get("multiplier", 1.0)
                veto_triggered = timing_impact.get("veto_triggered", False)

                if veto_triggered:
                    # VETO: Timing catastrophique → score drastiquement réduit
                    adjusted_score = base_score * multiplier
                    timing_decision = "VETO"
                    self.logger.warning(
                        f"[{asset}] ⚠️ TIMING VETO | Base={base_score:.1f} × {multiplier:.2f} = {adjusted_score:.1f} | "
                        f"Raison: {timing_impact.get('veto_reason', 'N/A')}"
                    )
                else:
                    # Impact normal via multiplicateur
                    adjusted_score = base_score * multiplier
                    timing_decision = "MULTIPLIER"

                # Cap à 30 points max
                final_score = min(adjusted_score, 30.0)

                self.logger.info(
                    f"[{asset}] ⏱️ TIMING IMPACT | Base={base_score:.1f} × Mult={multiplier:.2f} = {final_score:.1f}/30 | "
                    f"Quality={timing_quality} | {timing_impact.get('adjustment_details', 'N/A')}"
                )
            else:
                # Pas de timing analyzer pour cet asset → score = base
                final_score = base_score

            result["total_score"] = final_score

            # Stocker timing dans result pour le rapport (⚠️ GARDER POUR AFFICHAGE!)
            result["timing_score"] = timing_score
            result["timing_quality"] = timing_quality
            result["timing_metrics"] = timing_metrics
            result["timing_impact"] = timing_impact or {}
            result["timing_decision"] = timing_decision
            result["base_score_before_timing"] = base_score

            self.logger.debug(
                f"[{asset}] Footprint V6: Absorption={absorption_score:.1f} "
                f"Clustering={clustering_score:.1f} "
                f"Rejection={rejection_score:.1f} "
                f"Timing={timing_score:.1f} "
                f"→ Total={result['total_score']:.1f}/30"
            )

            # ✅ FIX (12 Dec 2025): Ajouter champs footprint_summary pour FusionManager
            # FusionManager._normalize_footprint() attend delta_total, buy_volume, sell_volume, poc
            # Ces valeurs sont déjà lues depuis fp_summary (ligne 514), il faut juste les retourner
            result["delta_total"] = fp_summary.get("delta_total", 0.0)
            result["buy_volume"] = buy_vol
            result["sell_volume"] = sell_vol
            result["poc"] = fp_summary.get("poc")

        except Exception as e:
            self.logger.error(
                f"[{asset}] Footprint V6 analysis error: {e}", exc_info=True
            )

        return result

    def _log_orderflow_consolidated_report(
        self,
        asset: str,
        orderflow_result: Dict[str, Any],
        footprint_result: Dict[str, Any],
        final_score: float,  # ⚠️ Ce paramètre ne sera PLUS utilisé pour le total 100pts
        action: Optional[str],
        momentum_result: Optional[Dict[str, Any]] = None,  # 📊 AJOUTÉ (18 DEC 2025): Optionnel!
        vwap_score_pct: float = 0.0,
        vwap_status: str = "N/A",
        vwap_regime: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
        df_m1: Optional[pd.DataFrame] = None,
    ) -> None:
        """
        📋 RAPPORT CONSOLIDÉ ORDERFLOW V6 + MOMENTUM - BURST SCALPING

        Affiche un bilan formaté OrderFlow + Footprint + Momentum + VWAP et du score final
        ✅ CORRIGÉ (19 DIC 2025): Normalisation des scores pour échelle cohérente 0-100pts
        ⏱️ MODIFIÉ (17 DEC 2025): Ajout Timing Analyzer au Footprint (25→30 pts max)
        📊 MODIFIÉ (18 DEC 2025): Ajout Momentum Institutionnel - NOUVEAUX POIDS
        🔒 RESTRICTION (18 DEC 2025): USDJPY UNIQUEMENT

        SCORES BRUTS :
        - OrderFlow V6: 0-50 points (Delta 0-25, Volume 0-15, Imbalance 0-10)
        - Footprint V6: 0-30 points (Absorption 0-12.5, Clustering 0-8.5, Rejection 0-4, ⏱️ Timing 0-5)
        - Momentum V1: 0-100 points (Candle 30, Volume 25, Acceleration 25, MTF 20)
        - VWAP: 0-100% → converti en 0-w_vw points

        SCORES NORMALISÉS (pour total 100pts) - NOUVEAUX POIDS 18 DEC 2025:
        - OrderFlow_norm = (score_brut / 50) * 30%  # 📉 RÉDUIT: 40%→30%
        - Footprint_norm = (score_brut / 30) * 30%  # 📉 RÉDUIT: 40%→30%
        - Momentum_norm = (score_brut / 100) * 20%  # 📊 NOUVEAU: 20%
        - VWAP_norm = (score_pct / 100) * 20%       # 📉 RÉDUIT: 20%→20% (inchangé)
        - TOTAL = OF_norm + FP_norm + MOM_norm + VWAP_norm (0-100pts)
        """

        # 🔒 RESTRICTION: Ce rapport détaillé est UNIQUEMENT pour USDJPY
        if asset != "USDJPY":
            return

        try:
            # ================================================================
            # 1. CALCUL DES POIDS - NOUVEAUX POIDS 18 DEC 2025
            # ================================================================
            # 📊 NOUVEAUX POIDS AVEC MOMENTUM:
            # OrderFlow: 30% | Footprint: 30% | Momentum: 20% | VWAP: 20%
            w_of = 30.0  # 📉 RÉDUIT: 40%→30%
            w_fp = 30.0  # 📉 RÉDUIT: 40%→30%
            w_mom = 20.0  # 📊 NOUVEAU: 20%
            w_vw = 20.0  # Inchangé: 20%

            # Note: Les poids dynamiques get_regime_weights ne connaissent pas encore momentum
            # Donc on force les poids fixes pour l'instant
            # TODO: Mettre à jour get_regime_weights dans vwap/config.py pour inclure momentum

            # Vérification de cohérence (poids totaux = 100%)
            total_weight = w_of + w_fp + w_mom + w_vw
            if abs(total_weight - 100.0) > 0.1:  # Tolérance 0.1%
                self.logger.warning(
                    f"[{asset}] Somme des poids anormale: {total_weight:.1f}% != 100%. "
                    f"Normalisation automatique."
                )
                w_of = (w_of / total_weight) * 100
                w_fp = (w_fp / total_weight) * 100
                w_mom = (w_mom / total_weight) * 100
                w_vw = (w_vw / total_weight) * 100

            # ================================================================
            # 2. EXTRACTION DES SCORES BRUTS
            # ================================================================
            # OrderFlow V6 - Scores bruts (échelle 0-50)
            of_score_brut = orderflow_result.get("total_score", 0.0)  # 0-50 points
            delta_score = orderflow_result.get("delta_momentum_score", 0.0)  # 0-25
            volume_score = orderflow_result.get(
                "volume_confirmation_score", 0.0
            )  # 0-15

            # 📊 Momentum Institutionnel - Score brut (échelle 0-100)
            # Si momentum_result est None (appel depuis run_bot.py), utiliser valeurs par défaut
            if momentum_result is None:
                momentum_result = {
                    "total_score": 0.0,
                    "candle_score": 0.0,
                    "volume_score": 0.0,
                    "acceleration_score": 0.0,
                    "mtf_score": 0.0,
                    "direction": "N/A",
                    "quality": "N/A",
                    "candle_details": {},
                    "volume_details": {},
                    "acceleration_details": {},
                    "mtf_details": {},
                }

            mom_score_brut = momentum_result.get("total_score", 0.0)  # 0-100 points
            mom_candle_score = momentum_result.get("candle_score", 0.0)  # 0-30
            mom_volume_score = momentum_result.get("volume_score", 0.0)  # 0-25
            mom_accel_score = momentum_result.get("acceleration_score", 0.0)  # 0-25
            mom_mtf_score = momentum_result.get("mtf_score", 0.0)  # 0-20
            mom_direction = momentum_result.get("direction", "NEUTRAL")
            mom_quality = momentum_result.get("quality", "N/A")
            imbalance_score = orderflow_result.get(
                "imbalance_strength_score", 0.0
            )  # 0-10

            # Footprint V6 - Scores bruts (échelle 0-25)
            fp_score_brut = footprint_result.get("total_score", 0.0)  # 0-25 points
            absorption_score = footprint_result.get(
                "absorption_levels_score", 0.0
            )  # 0-12.5
            clustering_score = footprint_result.get(
                "order_clustering_score", 0.0
            )  # 0-8.5
            rejection_score = footprint_result.get("price_rejection_score", 0.0)  # 0-4

            # VWAP - Score déjà en pourcentage (0-100%)
            vwap_score_pct_clamped = max(0.0, min(100.0, vwap_score_pct))

            # ================================================================
            # 3. NORMALISATION DES SCORES (pour total 100pts)
            # ================================================================
            # Conversion des scores bruts vers l'échelle des poids
            of_score_norm = (
                (of_score_brut / 50.0) * w_of if w_of > 0 else 0.0
            )  # 0-30 points
            fp_score_norm = (
                (fp_score_brut / 30.0) * w_fp if w_fp > 0 else 0.0
            )  # 0-30 points (⏱️ /30 car inclut timing)
            mom_score_norm = (
                (mom_score_brut / 100.0) * w_mom if w_mom > 0 else 0.0
            )  # 0-20 points (📊 NOUVEAU 18 DEC 2025)
            vwap_score_norm = (
                (vwap_score_pct_clamped / 100.0) * w_vw if w_vw > 0 else 0.0
            )  # 0-20 points

            # Calcul du TOTAL NORMALISÉ (0-100 points)
            total_normalise = of_score_norm + fp_score_norm + mom_score_norm + vwap_score_norm

            # ================================================================
            # 4. LOGGING DU RAPPORT CONSOLIDÉ
            # ================================================================
            sep = "=" * 70

            self.logger.info(f"\n{sep}")
            self.logger.info(f"📊 ORDERFLOW V6 - ANALYSE BURST SCALPING [{asset}]")
            self.logger.info(f"{sep}")

            # 4.1 ANALYSE MULTI-TIMEFRAME (BURST SCALPING M1+M3+M5)
            mtf_alignment = orderflow_result.get("mtf_alignment", {})
            m1_dir = mtf_alignment.get("m1", "N/A")
            m3_dir = mtf_alignment.get("m3", "N/A")  # ✅ M3 pour burst
            m5_dir = mtf_alignment.get("m5", "N/A")
            mtf_aligned = orderflow_result.get("mtf_aligned", False)

            self.logger.info(f"\n⏱️ PÉRIODES MULTI-TIMEFRAME (BURST SCALPING) :")
            self.logger.info(f"   • M1 (8 bougies) → Momentum  : {m1_dir.upper()}")
            self.logger.info(f"   • M3 (6 bougies) → Burst     : {m3_dir.upper()}")
            self.logger.info(f"   • M5 (5 bougies) → Contexte  : {m5_dir.upper()}")

            # ✅ CORRIGÉ (15 DEC 2025): Momentum réel basé sur bougies
            momentum_m1 = "N/A"
            if context and isinstance(context, dict):
                momentum_m1 = context.get("momentum_m1", "N/A")
            else:
                # Essayer de récupérer depuis asset_signals
                momentum_m1 = "NEUTRAL"  # Valeur par défaut

            self.logger.info(f"\n📊 MOMENTUM BOUGIES M1 :")
            self.logger.info(f"   • Momentum calculé  : {momentum_m1}")

            # Afficher détails des bougies si disponible
            if df_m1 is not None and len(df_m1) >= 8:
                recent = df_m1.tail(8)
                green_candles = sum(
                    1 for _, row in recent.iterrows() if row["close"] > row["open"]
                )
                self.logger.info(
                    f"   • Bougies vertes   : {green_candles}/8 ({green_candles/8:.0%})"
                )
                self.logger.info(
                    f"   • Dernière bougie : {'🟢 VERTE' if recent.iloc[-1]['close'] > recent.iloc[-1]['open'] else '🔴 ROUGE'}"
                )

            if mtf_aligned:
                self.logger.info(f"   ✅ ALIGNEMENT MTF DÉTECTÉ")
            else:
                self.logger.info(f"   ⚠️  Pas d'alignement multi-timeframe")

            # 4.2 ORDERFLOW ANALYSIS (avec scores bruts ET normalisés)
            delta_details = orderflow_result.get("delta_momentum_details", {})
            volume_details = orderflow_result.get("volume_confirmation_details", {})
            imbalance_details = orderflow_result.get("imbalance_strength_details", {})

            self.logger.info(f"\n📈 ORDERFLOW ANALYSIS ({w_of:.0f}% du total) :")
            self.logger.info(
                f"   Score brut: {of_score_brut:.1f}/50 pts → Normalisé: {of_score_norm:.1f}/{w_of:.0f} pts"
            )
            self.logger.info(f"   ├─ Delta Momentum      : {delta_score:.1f}/25 pts")
            self.logger.info(
                f"   │  • Delta total       : {delta_details.get('delta_total', 0)}"
            )
            self.logger.info(
                f"   │  • Cohérence         : {delta_details.get('coherence', 0)*100:.0f}%"
            )
            self.logger.info(
                f"   │  • Direction         : {delta_details.get('direction', 'N/A').upper()}"
            )

            self.logger.info(f"   ├─ Volume Confirmation : {volume_score:.1f}/15 pts")
            self.logger.info(
                f"   │  • Volume ratio      : {volume_details.get('ratio', 0):.2f}x"
            )
            self.logger.info(
                f"   │  • Spike détecté     : {'OUI' if volume_details.get('spike_detected') else 'NON'}"
            )
            poc_val = volume_details.get("poc")
            poc_str = f"{poc_val:.2f}" if poc_val is not None else "N/A"
            self.logger.info(f"   │  • POC (Point of Control) : {poc_str}")

            self.logger.info(
                f"   └─ Imbalance Strength  : {imbalance_score:.1f}/10 pts"
            )
            m1_count = imbalance_details.get("m1_count", 0)
            m5_count = imbalance_details.get("m5_count", 0)
            self.logger.info(f"      • Imbalances M1    : {m1_count} détectées")
            self.logger.info(f"      • Imbalances M5    : {m5_count} détectées")

            # 4.3 FOOTPRINT ANALYSIS (avec scores bruts ET normalisés)
            absorption_details = footprint_result.get("absorption_details", {})
            clustering_details = footprint_result.get("clustering_details", {})
            rejection_details = footprint_result.get("rejection_details", {})

            self.logger.info(f"\n👣 FOOTPRINT ANALYSIS ({w_fp:.0f}% du total) :")
            self.logger.info(
                f"   Score brut: {fp_score_brut:.1f}/30 pts → Normalisé: {fp_score_norm:.1f}/{w_fp:.0f} pts"
            )
            self.logger.info(
                f"   ├─ Absorption Levels   : {absorption_score:.1f}/12.5 pts"
            )
            self.logger.info(
                f"   │  • Biais absorption  : {absorption_details.get('bias', 'N/A')}"
            )
            self.logger.info(
                f"   │  • Buy ratio         : {absorption_details.get('buy_ratio', 0)*100:.0f}%"
            )
            self.logger.info(
                f"   │  • Sell ratio        : {absorption_details.get('sell_ratio', 0)*100:.0f}%"
            )

            self.logger.info(
                f"   ├─ Order Clustering    : {clustering_score:.1f}/8.5 pts"
            )
            self.logger.info(
                f"   │  • Clusters détectés : {clustering_details.get('cluster_count', 0)}"
            )
            self.logger.info(
                f"   │  • Distribution      : {clustering_details.get('distribution', 'N/A')}"
            )

            self.logger.info(
                f"   └─ Price Rejection     : {rejection_score:.1f}/4.0 pts"
            )
            self.logger.info(
                f"      • Rejets détectés   : {rejection_details.get('rejection_bars', 0)}/3"
            )
            self.logger.info(
                f"      • Force rejet       : {rejection_details.get('strength', 'N/A')}"
            )

            # ⏱️ 4.3bis TIMING QUALITY ANALYSIS avec système multiplicateur (18 DEC 2025)
            timing_score = footprint_result.get("timing_score", 0.0)
            timing_quality = footprint_result.get("timing_quality", "N/A")
            timing_metrics_full = footprint_result.get("timing_metrics", {})
            timing_impact = footprint_result.get("timing_impact", {})
            timing_decision = footprint_result.get("timing_decision", "NO_TIMING")
            base_score_before_timing = footprint_result.get("base_score_before_timing", 0.0)

            if timing_score > 0:
                self.logger.info(f"\n⏱️  TIMING QUALITY ({timing_score:.1f}/5.0 pts) :")
                self.logger.info(f"   Score timing      : {timing_score:.1f}/5.0 pts")
                self.logger.info(f"   Qualité           : {timing_quality}")

                # Détails des métriques
                buy_conc_q1 = timing_metrics_full.get("buy_concentration_q1", 0.0)
                sell_conc_q1 = timing_metrics_full.get("sell_concentration_q1", 0.0)
                vel_ratio = timing_metrics_full.get("velocity_ratio", 1.0)

                if buy_conc_q1 > 0 or sell_conc_q1 > 0:
                    self.logger.info(f"   ├─ Concentration Q1 :")
                    self.logger.info(f"   │  • Buy  : {buy_conc_q1*100:.0f}%")
                    self.logger.info(f"   │  • Sell : {sell_conc_q1*100:.0f}%")

                if vel_ratio != 1.0:
                    buy_vel = timing_metrics_full.get("buy_velocity", 0.0)
                    sell_vel = timing_metrics_full.get("sell_velocity", 0.0)
                    self.logger.info(f"   └─ Vitesse :")
                    self.logger.info(f"      • Buy  : {buy_vel:.1f} ticks/sec")
                    self.logger.info(f"      • Sell : {sell_vel:.1f} ticks/sec")
                    self.logger.info(f"      • Ratio: {vel_ratio:.2f}x")

                # ⏱️ TIMING IMPACT (système multiplicateur 18 Dec 2025)
                if timing_impact and timing_decision != "NO_TIMING":
                    multiplier = timing_impact.get("multiplier", 1.0)
                    veto_triggered = timing_impact.get("veto_triggered", False)
                    adjustment_details = timing_impact.get("adjustment_details", "N/A")

                    self.logger.info(f"\n   🎯 IMPACT SUR SCORE FOOTPRINT :")
                    self.logger.info(f"      Score base       : {base_score_before_timing:.1f}/25 pts")

                    if veto_triggered:
                        veto_reason = timing_impact.get("veto_reason", "N/A")
                        self.logger.warning(f"      ⚠️  VETO APPLIQUÉ  : ×{multiplier:.2f} (pénalité sévère)")
                        self.logger.warning(f"      Raison           : {veto_reason}")
                        self.logger.warning(f"      Score final      : {footprint_result.get('total_score', 0):.1f}/30 pts")
                    else:
                        impact_pct = (multiplier - 1.0) * 100
                        impact_sign = "+" if impact_pct > 0 else ""
                        self.logger.info(f"      Multiplicateur   : ×{multiplier:.2f} ({impact_sign}{impact_pct:.0f}%)")
                        self.logger.info(f"      Score ajusté     : {footprint_result.get('total_score', 0):.1f}/30 pts")

                    self.logger.info(f"      Détails          : {adjustment_details}")

            # 4.4 MOMENTUM INSTITUTIONNEL ANALYSIS (18 DEC 2025)
            self.logger.info(f"\n📊 MOMENTUM INSTITUTIONNEL ({w_mom:.0f}% du total) :")
            self.logger.info(
                f"   Score brut: {mom_score_brut:.1f}/100 pts → Normalisé: {mom_score_norm:.1f}/{w_mom:.0f} pts"
            )
            self.logger.info(f"   Direction       : {mom_direction}")
            self.logger.info(f"   Qualité         : {mom_quality}")
            self.logger.info(f"   ├─ Candle Strength     : {mom_candle_score:.1f}/30 pts")

            # Détails candles si disponibles
            candle_details = momentum_result.get("candle_details", {})
            if candle_details:
                green_candles = candle_details.get("green_candles", 0)
                total_candles = candle_details.get("total_candles", 8)
                self.logger.info(f"   │  • Bougies vertes  : {green_candles}/{total_candles} ({green_candles/total_candles*100:.0f}%)")
                self.logger.info(f"   │  • Body ratio moyen: {candle_details.get('avg_body_ratio', 0):.2f}")

            self.logger.info(f"   ├─ Volume Confirmation : {mom_volume_score:.1f}/25 pts")

            # Détails volume si disponibles
            volume_details = momentum_result.get("volume_details", {})
            if volume_details and volume_details.get("volume_available"):
                self.logger.info(f"   │  • Volume ratio    : {volume_details.get('volume_ratio', 1.0):.2f}x")
                self.logger.info(f"   │  • Accélération vol: {volume_details.get('volume_acceleration', 0)*100:+.0f}%")

            self.logger.info(f"   ├─ Price Acceleration  : {mom_accel_score:.1f}/25 pts")

            # Détails accélération si disponibles
            accel_details = momentum_result.get("acceleration_details", {})
            if accel_details:
                self.logger.info(f"   │  • Move total      : {accel_details.get('total_move_pct', 0)*100:.2f}%")
                self.logger.info(f"   │  • Accel ratio     : {accel_details.get('acceleration_ratio', 1.0):.2f}x")

            self.logger.info(f"   └─ MTF Alignment       : {mom_mtf_score:.1f}/20 pts")

            # Détails MTF si disponibles (BURST SCALPING M1+M3+M5)
            mtf_details = momentum_result.get("mtf_details", {})
            if mtf_details:
                self.logger.info(f"      • M1 direction   : {mtf_details.get('m1_direction', 'N/A')}")
                self.logger.info(f"      • M3 direction   : {mtf_details.get('m3_direction', 'N/A')}")
                self.logger.info(f"      • M5 direction   : {mtf_details.get('m5_direction', 'N/A')}")
                self.logger.info(f"      • Alignment      : {mtf_details.get('alignment', 'N/A')}")

            # 4.5 VWAP MODULE - INSTITUTIONNEL
            self.logger.info(f"\n📊 VWAP INSTITUTIONNEL ({w_vw:.0f}% du scoring)")
            self.logger.info(
                f"   Score VWAP      : {vwap_score_norm:.1f}/{w_vw:.0f} pts ({vwap_score_pct_clamped:.1f}%)"
            )
            self.logger.info(f"   Status          : {vwap_status}")
            if vwap_regime:
                self.logger.info(f"   Régime détecté  : {vwap_regime.upper()}")

            # ================================================================
            # 5. SCORE FINAL NORMALISÉ & DÉCISION
            # ================================================================
            self.logger.info(f"\n{sep}")
            self.logger.info(f"🎯 SCORE FINAL BURST SCALPING (Normalisé sur 100pts)")
            self.logger.info(f"{sep}")
            self.logger.info(
                f"   OrderFlow ({w_of:.0f}%) : {of_score_norm:.1f}/{w_of:.0f} pts"
            )
            self.logger.info(
                f"   Footprint ({w_fp:.0f}%) : {fp_score_norm:.1f}/{w_fp:.0f} pts"
            )
            self.logger.info(
                f"   Momentum ({w_mom:.0f}%)  : {mom_score_norm:.1f}/{w_mom:.0f} pts"
            )
            self.logger.info(
                f"   VWAP ({w_vw:.0f}%)      : {vwap_score_norm:.1f}/{w_vw:.0f} pts"
            )
            self.logger.info(f"   {'─' * 50}")
            self.logger.info(f"   TOTAL (OF+FP+MOM+VWAP) : {total_normalise:.1f}/100 pts")

            # Ancien total (pour référence debug - à supprimer après validation)
            ancien_total_erroné = final_score + vwap_score_norm
            if abs(ancien_total_erroné - total_normalise) > 1.0:
                self.logger.debug(
                    f"[DEBUG] Ancien calcul (obsolète): {ancien_total_erroné:.1f}/100 pts"
                )

            # Direction recommandée
            if action:
                action_emoji = "🟢" if action == "BUY" else "🔴"
                self.logger.info(
                    f"\n   {action_emoji} Direction recommandée : {action}"
                )

            self.logger.info(f"{sep}\n")

        except ZeroDivisionError as e:
            self.logger.error(
                f"[{asset}] Erreur division par zéro dans normalisation: {e}"
            )
        except Exception as e:
            self.logger.error(
                f"[{asset}] Erreur rapport consolidé OrderFlow V6: {e}", exc_info=True
            )

    def evaluate_entry(
        self,
        asset: str,
        analyzed_context: Dict[str, Any],
        asset_signals: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Version 'desk pro' compatible pipeline:
        - Pas de fallback implicite → uniquement des règles explicites
        - Priorité:
            1. Marubozu (MarketAnalyzer + Playbook)
            2. Range Accumulation MTF
            3. Range Accumulation simple
            4. Burst single_master
        - ATR/Spread n'affecte que le burst single_master si des seuils sont configurés
        """
        try:
            # --- 0) Données & config ---
            ctx_md = (analyzed_context.get("market_data") or {}).get(asset, {}) or {}

            # ✅ Résolution sûre (pas d'évaluation booléenne de DataFrame)
            df_m1 = None
            for __k in ("annotated_rates_df_m1", "annotated_rates_df", "df_m1", "df"):
                __v = ctx_md.get(__k)
                if isinstance(__v, pd.DataFrame):
                    df_m1 = __v
                    break

            df_work = (
                df_m1.copy()
                if isinstance(df_m1, pd.DataFrame) and len(df_m1) >= 50
                else None
            )
            # ✅ AJOUT (15 DEC 2025): Calcul du momentum M1 basé sur bougies récentes
            momentum_m1 = "NEUTRAL"
            if df_work is not None and len(df_work) >= 8:
                momentum_m1 = self._calculate_m1_momentum_from_candles(df_work)
                self.logger.debug(f"[{asset}] Momentum M1 calculé: {momentum_m1}")

            # ✅ AJOUTER au contexte pour FusionManager
            if "context" not in analyzed_context:
                analyzed_context["context"] = {}

            analyzed_context["momentum_m1"] = momentum_m1

            # ✅ AJOUTER aussi dans asset_signals pour usage immédiat
            asset_signals["momentum_m1"] = momentum_m1

            # --- 0b) Action hint (BUY/SELL) par défaut ---
            action = None

            def _norm_dir(x):
                if not x:
                    return None
                x = str(x).upper()
                if x in ("BUY", "LONG", "BULL", "BULLISH"):
                    return "BUY"
                if x in ("SELL", "SHORT", "BEAR", "BEARISH"):
                    return "SELL"
                return None

            # --- 0a) Config stratégie (pour meta & règles) ---
            try:
                strat_cfg = self.config_manager.get_strategy_config("scalping") or {}
            except Exception:
                strat_cfg = self.strategy_config or {}

            # --- 0c) Intégration footprint ---
            try:
                fp_score = float(asset_signals.get("footprint_score", 0.0))
                fp_status = str(asset_signals.get("footprint_status", "N/A")).upper()
                fp_summary = asset_signals.get("footprint_summary", {})

                # Boost de confiance si footprint cohérent avec le biais
                if (
                    fp_status == "BULLISH"
                    and fp_score > 0
                    and asset_signals.get("phase", "").lower().startswith("bull")
                ):
                    asset_signals["confidence_score"] = min(
                        1.0, float(asset_signals.get("confidence_score", 0.5)) + 0.15
                    )
                    self.logger.info(
                        f"[{asset}] 📊 Footprint bullish → confiance renforcée"
                    )

                if (
                    fp_status == "BEARISH"
                    and fp_score > 0
                    and asset_signals.get("phase", "").lower().startswith("bear")
                ):
                    asset_signals["confidence_score"] = min(
                        1.0, float(asset_signals.get("confidence_score", 0.5)) + 0.15
                    )
                    self.logger.info(
                        f"[{asset}] 📊 Footprint bearish → confiance renforcée"
                    )

                # Early entry si déséquilibre extrême (optionnel)
                try:
                    delta = (
                        float(fp_summary.get("delta_total", 0))
                        if isinstance(fp_summary, dict)
                        else 0
                    )
                except Exception:
                    delta = None
                if isinstance(delta, (int, float)) and abs(delta) >= 300:
                    asset_signals["early_entry_allowed"] = True
                    self.logger.info(
                        f"[{asset}] ⚡ Early entry activé (Δ={delta}) via footprint"
                    )
                else:
                    asset_signals["early_entry_allowed"] = False

                # --- 0d) Déduction robuste de l'action ---
                # 1) indices directs depuis les signaux
                action = (
                    _norm_dir(asset_signals.get("action"))
                    or _norm_dir(asset_signals.get("bias"))
                    or _norm_dir(asset_signals.get("direction"))
                    or action
                )

                # 2) fallback via la phase quand aucun indice direct
                if action is None:
                    ph = str(asset_signals.get("phase", "")).lower()
                    if ph.startswith("trend_bull") or ph.startswith("breakout_bull"):
                        action = "BUY"
                    elif ph.startswith("trend_bear") or ph.startswith("breakout_bear"):
                        action = "SELL"

                # 3) dernier filet via le footprint (si résumé dispo)
                try:
                    if action is None and isinstance(fp_summary, dict):
                        d = float(fp_summary.get("delta_total", 0))
                        if d > 0:
                            action = "BUY"
                        elif d < 0:
                            action = "SELL"
                except Exception:
                    pass

            except Exception as e:
                self.logger.warning(f"[{asset}] Footprint integration skipped: {e}")

            # --- 1) Métadonnées (pip_size, spread, etc.) ---
            meta = self._safe_asset_meta(
                asset, asset_signals, analyzed_context, strat_cfg
            )
            pip_size = meta["pip_size"]
            if pip_size <= 0:
                self.logger.warning(f"[{asset}] pip_size invalide.")
                return {}

            # --- 2) Prix courant ---
            price = self._safe_price_from_signals(asset_signals)
            if not price:
                self.logger.info(f"[{asset}] Pas de prix exploitable dans les signaux.")
                return {}

            # --- 5) Range Accumulation MTF ---
            try:
                mtf_cfg = strat_cfg.get("range_accumulation_mtf") or {}
                if mtf_cfg.get("enabled", False):  # ✅ Vérifier enabled
                    mtf_decision = self._rule_range_accumulation_mtf(
                        df_m1=df_work,
                        asset=asset,
                        price=price,
                        meta=meta,
                        cfg=mtf_cfg,
                        analyzed_context=analyzed_context,
                    )
                    if mtf_decision:
                        return self._finalize_decision(mtf_decision, analyzed_context)

                        # --- 3) Momentum & Breakout (nouvelles règles) ---
                try:
                    mom_cfg = (
                        (strat_cfg.get("momentum") or {})
                        if isinstance(strat_cfg, dict)
                        else {}
                    )
                    pat_cfg = (
                        (strat_cfg.get("patterns") or {})
                        if isinstance(strat_cfg, dict)
                        else {}
                    )

                    # ✅ Vérifier que momentum ET patterns sont enabled
                    if not mom_cfg.get("enabled", False) and not pat_cfg.get(
                        "enabled", False
                    ):
                        self.logger.debug(
                            f"[{asset}] Momentum/Pattern désactivés dans config"
                        )
                    else:
                        weights = strat_cfg.get("scoring_weights") or {
                            "context": 0.3,
                            "technical": 0.4,
                            "orderflow": 0.2,
                            "risk": 0.1,
                        }
                        thresholds = strat_cfg.get("scoring_thresholds") or {
                            "direct": 0.70,
                            "conditional": 0.50,
                        }

                        candidates: List[Dict[str, Any]] = []

                        # Evaluer momentum patterns seulement si enabled
                        if mom_cfg.get("enabled", False):
                            rb = self._rule_breakout_consolidation(
                                df_work,
                                asset,
                                price,
                                meta,
                                mom_cfg.get("breakout", {}) or {},
                            )
                            if rb:
                                candidates.append(rb)

                            tpull = self._rule_trend_pullback(
                                df_work,
                                asset,
                                price,
                                meta,
                                mom_cfg.get("trend_pullback", {}) or {},
                            )
                            if tpull:
                                candidates.append(tpull)

                            ign = self._rule_momentum_ignition(
                                df_work,
                                asset,
                                price,
                                meta,
                                mom_cfg.get("ignition", {}) or {},
                            )
                            if ign:
                                candidates.append(ign)

                        # Evaluer patterns seulement si enabled
                        if pat_cfg.get("enabled", False):
                            ib = self._rule_inside_bar_breakout(
                                df_work,
                                asset,
                                price,
                                meta,
                                (pat_cfg.get("inside_bar", {}) or {}),
                            )
                            if ib:
                                candidates.append(ib)

                        if candidates:
                            best = self._choose_best_candidate(
                                candidates=candidates,
                                asset=asset,
                                meta=meta,
                                asset_signals=asset_signals,
                                analyzed_context=analyzed_context,
                                weights=weights,
                                thresholds=thresholds,
                            )
                            if best and float(best.get("score", 0.0)) >= float(
                                thresholds.get("direct", 0.70)
                            ):
                                return self._finalize_decision(best, analyzed_context)
                except Exception as e:
                    self.logger.debug(f"[{asset}] Momentum/Pattern block skipped: {e}")

            except Exception as e:
                self.logger.debug(f"[{asset}] MTF range-accum skipped: {e}")

            # --- 6) Range Accumulation simple ---
            try:
                range_cfg = strat_cfg.get("range_accumulation") or {}
                if range_cfg.get("enabled", False):  # ✅ Vérifier enabled
                    range_decision = self._rule_range_accumulation(
                        df=df_work,
                        asset=asset,
                        price=price,
                        action=action,
                        meta=meta,
                        cfg=range_cfg,
                    )
                    if range_decision:
                        range_decision.setdefault("strategy_type", "scalping")
                        range_decision.setdefault("rule_name", "range_accumulation")
                        range_decision.setdefault("execution_status", "ready")
                        return self._finalize_decision(range_decision, analyzed_context)
            except Exception as e:
                self.logger.debug(f"[{asset}] Range accumulation simple skipped: {e}")

            # --- 7) Burst single_master ---
            # ATR M1 (optionnel, pour guardrails si tu configures un seuil)
            atr_m1_pips = None
            if isinstance(df_work, pd.DataFrame):
                atr_m1 = self._atr(df_work, period=14)
                atr_m1_pips = (
                    (atr_m1 / pip_size)
                    if isinstance(atr_m1, (int, float)) and atr_m1 > 0 and pip_size > 0
                    else None
                )

            # Config guardrails (seuils globaux si présents)
            guardrails_cfg = {}
            try:
                guardrails_cfg = self.config_manager.get("guardrails", {}) or {}
            except Exception:
                guardrails_cfg = getattr(self.config_manager, "guardrails", {}) or {}

            # Config burst_scalping
            sm_cfg = ((strat_cfg.get("entry_rules") or {}).get("scalping") or {}).get(
                "burst_scalping", {}
            ) or {}
            if not bool(sm_cfg.get("enabled", True)):
                self.logger.info(f"[{asset}] single_master désactivé en config.")
                self.logger.info(
                    f"[DEBUG][{asset}] evaluate_entry terminé → AUCUN setup retenu."
                )
                return {}

            # Seuils dynamiques (optionnels)
            try:
                max_spread_sm = float(
                    sm_cfg.get(
                        "max_spread_pips",
                        (guardrails_cfg.get("volatility", {}) or {}).get(
                            "max_spread_pips", 0.0
                        ),
                    )
                    or 0.0
                )
            except Exception:
                max_spread_sm = 0.0

            try:
                min_atr_req = float(
                    sm_cfg.get(
                        "min_atr_m1_pips",
                        (guardrails_cfg.get("volatility", {}) or {}).get(
                            "min_atr_m1_pips", 0.0
                        ),
                    )
                    or 0.0
                )
            except Exception:
                min_atr_req = 0.0

            # Gating simple selon les seuils si fournis
            if max_spread_sm > 0 and meta.get("spread_pips", 0.0) > max_spread_sm:
                self.logger.info(
                    f"[{asset}] REFUS single_master → spread {meta['spread_pips']:.2f}p > seuil {max_spread_sm:.2f}p"
                )
                self.logger.info(
                    f"[DEBUG][{asset}] evaluate_entry terminé → AUCUN setup retenu."
                )
                return {}

            if min_atr_req > 0.0 and (atr_m1_pips is None or atr_m1_pips < min_atr_req):
                self.logger.info(
                    f"[{asset}] REFUS single_master → ATR M1 {atr_m1_pips or 0:.2f}p < seuil {min_atr_req:.2f}p"
                )
                self.logger.info(
                    f"[DEBUG][{asset}] evaluate_entry terminé → AUCUN setup retenu."
                )
                return {}

            # ================================================================
            # ORDERFLOW V6 - ANALYSE MULTI-COMPOSANTS (M1/M5/M15)
            # 🔒 RESTRICTION (18 DEC 2025): USDJPY UNIQUEMENT
            # ================================================================
            # ⚠️ OrderFlow V6 + Footprint V6 + VWAP = USDJPY SEULEMENT
            if asset != "USDJPY":
                self.logger.debug(
                    f"[{asset}] OrderFlow V6 analysis skipped (USDJPY only)"
                )
                return {}  # Pas d'analyse pour les autres symboles

            try:
                # Récupération des DataFrames multi-timeframe
                df_m5 = None
                df_m15 = None

                # ✅ DEBUG: Log structure analyzed_context
                ctx_md = (analyzed_context.get("market_data") or {}).get(
                    asset, {}
                ) or {}
                self.logger.debug(
                    f"[OF V6][{asset}] market_data keys: {list(ctx_md.keys())}"
                )

                # Essayer de récupérer M5 depuis analyzed_context
                try:
                    for k in ("annotated_rates_df_m5", "df_m5", "rates_m5"):
                        v = ctx_md.get(k)
                        if isinstance(v, pd.DataFrame) and len(v) >= 4:
                            df_m5 = v
                            self.logger.debug(
                                f"[OF V6][{asset}] M5 trouvé via clé '{k}' | len={len(df_m5)}"
                            )
                            break
                except Exception as e:
                    self.logger.debug(f"[OF V6][{asset}] Erreur récupération M5: {e}")

                # Essayer de récupérer M15 depuis analyzed_context
                try:
                    for k in ("annotated_rates_df_m15", "df_m15", "rates_m15"):
                        v = ctx_md.get(k)
                        if isinstance(v, pd.DataFrame) and len(v) >= 4:
                            df_m15 = v
                            self.logger.debug(
                                f"[OF V6][{asset}] M15 trouvé via clé '{k}' | len={len(df_m15)}"
                            )
                            break
                except Exception as e:
                    self.logger.debug(f"[OF V6][{asset}] Erreur récupération M15: {e}")

                # Si M5/M15 non trouvés, essayer de les récupérer via MT5
                if df_m5 is None and self.mt5_connector:
                    try:
                        import MetaTrader5 as mt5

                        df_m5 = self.mt5_connector.get_rates(
                            asset, mt5.TIMEFRAME_M5, count=20
                        )
                        if df_m5 is not None and len(df_m5) >= 4:
                            self.logger.debug(
                                f"[OF V6][{asset}] M5 récupéré via MT5 | len={len(df_m5)}"
                            )
                    except Exception as e:
                        self.logger.debug(
                            f"[OF V6][{asset}] Impossible récupérer M5 via MT5: {e}"
                        )

                if df_m15 is None and self.mt5_connector:
                    try:
                        import MetaTrader5 as mt5

                        df_m15 = self.mt5_connector.get_rates(
                            asset, mt5.TIMEFRAME_M15, count=15
                        )
                        if df_m15 is not None and len(df_m15) >= 4:
                            self.logger.debug(
                                f"[OF V6][{asset}] M15 récupéré via MT5 | len={len(df_m15)}"
                            )
                    except Exception as e:
                        self.logger.debug(
                            f"[OF V6][{asset}] Impossible récupérer M15 via MT5: {e}"
                        )

                # ⚡ 1. OrderFlow Analysis (50% du score)
                orderflow_result = self._analyze_orderflow_v6(
                    asset=asset,
                    df_m1=df_work,
                    df_m5=df_m5,
                    df_m15=df_m15,
                    asset_signals=asset_signals,
                )

                # 👣 2. Footprint Analysis (25% du score)
                footprint_result = self._analyze_footprint_v6(
                    asset=asset, df_m1=df_work, asset_signals=asset_signals
                )

                # 📊 3. Momentum Institutionnel Analysis (20% du score) - 22 DEC 2025
                # Lazy-load l'analyseur spécifique à l'asset
                if asset not in self.momentum_analyzers:
                    self.momentum_analyzers[asset] = MomentumAnalyzerInstitutional(asset)

                self.logger.info(f"[{asset}] 🔍 Appel Momentum | df_work={'None' if df_work is None else f'len={len(df_work)}'} | df_m5={'None' if df_m5 is None else f'len={len(df_m5)}'} | df_m15={'None' if df_m15 is None else f'len={len(df_m15)}'}")
                momentum_result = self.momentum_analyzers[asset].analyze(
                    df_m1=df_work, df_m5=df_m5, df_m15=df_m15
                )
                self.logger.info(
                    f"[{asset}] 📊 MOMENTUM INSTITUTIONNEL | Score={momentum_result['total_score']:.1f}/100 | "
                    f"Direction={momentum_result['direction']} | Quality={momentum_result['quality']}"
                )

                # ✅ PRÉPARATION DU CONTEXTE POUR FUSIONMANAGER (18 DEC 2025)
                # Créer un dictionnaire context avec TOUS les éléments nécessaires
                fusion_context = {
                    "momentum_m1": momentum_m1,
                    "momentum_result": momentum_result,  # 📊 AJOUTÉ: Momentum institutionnel complet
                    "phase_observer_regime": asset_signals.get("phase", ""),
                    "range_pos_pct": asset_signals.get("range_position", 0.5),
                    "in_upper_tercile": asset_signals.get("in_upper_tercile", False),
                    "in_lower_tercile": asset_signals.get("in_lower_tercile", False),
                    "orderflow_bias": orderflow_result.get("mtf_alignment", {}).get(
                        "m1", "NEUTRAL"
                    ),
                    "footprint_data": {
                        "buy_ratio": footprint_result.get("absorption_details", {}).get(
                            "buy_ratio", 0.5
                        ),
                        "sell_ratio": footprint_result.get(
                            "absorption_details", {}
                        ).get("sell_ratio", 0.5),
                    },
                    "orderflow_data": {
                        "delta_total": orderflow_result.get(
                            "delta_momentum_details", {}
                        ).get("delta_total", 0),
                        "buy_ratio": orderflow_result.get(
                            "delta_momentum_details", {}
                        ).get("buy_ratio", 0.5),
                    },
                    "asset": asset,
                    "timestamp": time.time(),
                }

                # Stocker pour usage dans _log_orderflow_consolidated_report
                analyzed_context["fusion_context"] = fusion_context

                # 📊 3. CALCUL DES POIDS ET NORMALISATION POUR LA DÉCISION
                # Récupération des poids identique à _log_orderflow_consolidated_report
                fusion_cfg = self.strategy_config.get("fusion", {})
                ponderations = fusion_cfg.get("ponderations", {})
                w_of = float(ponderations.get("orderflow_weight", 0.30)) * 100
                w_fp = float(ponderations.get("footprint_weight", 0.35)) * 100
                w_vw = float(ponderations.get("vwap_weight", 0.35)) * 100

                # Normalisation des scores pour la décision (sans VWAP)
                orderflow_score = orderflow_result.get("total_score", 0.0)  # 0-50
                footprint_score = footprint_result.get("total_score", 0.0)  # 0-25
                score_normalized = (orderflow_score / 50.0) * w_of + (
                    footprint_score / 25.0
                ) * w_fp  # 0-100 (OF+FP seulement)

                # Score final pour la décision (OF+FP+VWAP)
                final_score_normalized = (
                    score_normalized + (vwap_score_pct / 100.0) * w_vw
                )  # 0-100

                # 📋 5. RAPPORT CONSOLIDÉ
                # Récupérer le score VWAP depuis asset_signals (stocké par run_bot.py)
                vwap_score_pct = 0.0
                vwap_status = "N/A"
                vwap_regime = None
                try:
                    latest_signals = asset_signals.get("__latest__", {})
                    vwap_score_pct = (
                        float(latest_signals.get("vwap_score", 0.0)) * 100.0
                    )  # Convertir 0-1 → 0-100
                    vwap_status = str(latest_signals.get("vwap_status", "N/A"))
                    vwap_regime = latest_signals.get(
                        "vwap_regime"
                    )  # ✅ AJOUTÉ: Régime VWAP pour poids dynamiques
                except Exception:
                    pass

                # ✅ CORRECTION (15 DEC 2025): Préparer le contexte AVANT l'appel
                report_context = {"momentum_m1": momentum_m1}
                if "fusion_context" in locals() and isinstance(fusion_context, dict):
                    report_context.update(fusion_context)

                self._log_orderflow_consolidated_report(
                    asset=asset,
                    orderflow_result=orderflow_result,
                    footprint_result=footprint_result,
                    final_score=0.0,
                    action=action,
                    momentum_result=momentum_result,  # 📊 AJOUTÉ: Momentum institutionnel
                    vwap_score_pct=vwap_score_pct,
                    vwap_status=vwap_status,
                    vwap_regime=vwap_regime,
                    context=report_context,
                    df_m1=df_work,  # ✅ AJOUT IMPORTANT
                )

                # ✅ AUCUN SEUIL ICI - FusionManager gère TOUT avec scoring_thresholds
                # (high: 0.80, moderate: 0.75, cautious: 0.70, conditional: 0.40)

            except Exception as e:
                self.logger.warning(
                    f"[{asset}] OrderFlow V6 analysis failed: {e}", exc_info=True
                )
                # Continuer sans OrderFlow V6 si erreur (fallback)

            # ================================================================
            # DÉCISION BURST SCALPING
            # ================================================================
            # Décision single_master (aucun volume/SL ici → calculés en aval dans prepare_order)
            entry_mode = str(sm_cfg.get("entry_mode", "MARKET")).upper()
            burst_sz = int(sm_cfg.get("burst_size", 5) or 5)

            sm_decision = {
                "strategy_type": "scalping",
                "rule_name": "burst_scalping",
                "execution_status": "ready",
                "action": action,
                "asset": asset,
                "order_type": entry_mode,
                "entry_price": (float(price) if entry_mode != "MARKET" else None),
                "burst_size": burst_sz,
                "fusion_data": {
                    "fused_confidence": final_score_normalized
                    / 100.0,  # ✅ Normalisé 0-1
                    "orderflow": orderflow_result,
                    "footprint": footprint_result,
                    "score_details": {  # ✅ AJOUT : Détails pour debugging
                        "orderflow_brut": orderflow_score,
                        "footprint_brut": footprint_score,
                        "vwap_pct": vwap_score_pct,
                        "weights": {"of": w_of, "fp": w_fp, "vw": w_vw},
                        "normalized": final_score_normalized,
                    },
                },
                "meta": {
                    "burst": True,
                    "entry_source": "core_decision",
                    "per_leg_virtual": bool(sm_cfg.get("per_leg_virtual", True)),
                    "atr_m1_pips": atr_m1_pips,
                    "orderflow_v6_score": final_score_normalized,  # ✅ Score normalisé 0-100
                },
            }

            return self._finalize_decision(sm_decision, analyzed_context)

            # --- Aucun setup valide ---
            # (Jamais atteint car on retourne au-dessus pour SM ; gardé par sécurité)
            self.logger.info(
                f"[DEBUG][{asset}] evaluate_entry terminé → AUCUN setup retenu."
            )
            return {}

        except Exception as e:
            self.logger.error(f"[{asset}] evaluate_entry error: {e}", exc_info=True)
            return {}

    # ==========================================================
    # =============       RÈGLES D’ENTRÉE       ================
    # ==========================================================
    # === [SCORING] Multi-critères Contexte/Technique/OrderFlow/Risque ========
    def _choose_best_candidate(
        self,
        candidates: List[Dict[str, Any]],
        asset: str,
        meta: Dict[str, Any],
        asset_signals: Dict[str, Any],
        analyzed_context: Dict[str, Any],
        weights: Dict[str, float],
        thresholds: Dict[str, float],
    ) -> Optional[Dict[str, Any]]:
        best = None
        best_score = -1.0
        for c in candidates:
            s = self._score_candidate(c, meta, asset_signals, analyzed_context, weights)
            c["score"] = float(max(0.0, min(1.0, s)))
            if c["score"] > best_score:
                best, best_score = c, c["score"]
        return best

    def _score_candidate(
        self,
        c: Dict[str, Any],
        meta: Dict[str, Any],
        asset_signals: Dict[str, Any],
        analyzed_context: Dict[str, Any],
        weights: Dict[str, float],
    ) -> float:
        # --- 1) Contexte (phase/MTF & confiance globale signaux) ---
        phase = str(asset_signals.get("phase", "") or "").lower()
        conf = float(asset_signals.get("confidence_score", 0.5) or 0.5)
        align = 0.5
        if c.get("action") == "BUY" and any(
            k in phase for k in ("bull", "up", "accum", "trend")
        ):
            align = 1.0
        if c.get("action") == "SELL" and any(
            k in phase for k in ("bear", "down", "distrib", "trend")
        ):
            align = 1.0 if "trend" in phase or "bear" in phase else 0.8
        context_score = 0.5 * conf + 0.5 * align
        context_score = max(0.0, min(1.0, context_score))

        # --- 2) Technique (score interne du setup) ---
        tech = c.get("technical_score")
        if tech is None:
            tech = c.get("confidence", 0.6)
        technical_score = max(0.0, min(1.0, float(tech)))

        # --- 3) Order Flow (footprint/delta si dispo) ---
        of = 0.0
        try:
            fp = float(asset_signals.get("footprint_score", 0.0) or 0.0) / 100.0
            of = max(of, fp)
            d = (asset_signals.get("footprint_summary") or {}).get("delta_total")
            if isinstance(d, (int, float)):
                of = max(of, min(abs(d) / 500.0, 1.0) * 0.7)  # plafonné
        except Exception:
            pass
        orderflow_score = max(0.0, min(1.0, of))

        # --- 4) Risque (spread bas = mieux ; heuristique simple) ---
        sp = float(meta.get("spread_pips", 0.0) or 0.0)
        if sp <= 5:
            risk_score = 1.0
        elif sp <= 10:
            risk_score = 0.8
        elif sp <= 15:
            risk_score = 0.6
        else:
            risk_score = 0.3

        w = lambda k, d: float(weights.get(k, d))
        score = (
            w("context", 0.3) * context_score
            + w("technical", 0.4) * technical_score
            + w("orderflow", 0.2) * orderflow_score
            + w("risk", 0.1) * risk_score
        )
        return float(score)

    # === [PATTERNS MOMENTUM SUPPRIMÉS - Session 23 Nov 2025] ===
    # Les fonctions suivantes ont été supprimées car désactivées dans la config :
    # - _rule_breakout_consolidation (45 lignes)
    # - _rule_trend_pullback (47 lignes)
    # - _rule_inside_bar_breakout (40 lignes)
    # - _rule_momentum_ignition (106 lignes)
    # Total supprimé : ~180 lignes de code mort

    def _get_atr_m1_pips(
        self,
        asset: str,
        signals: Dict[str, Any],
        context: Dict[str, Any],
        pip_size_value: float,
    ) -> Optional[float]:
        """
        Essaie de récupérer et convertir l'ATR M1 en pips à partir de plusieurs sources.
        Retourne None si toutes les conversions échouent.
        """
        atr_sources: List[Tuple[str, Any, bool]] = [
            ("signals['atr_m1_pips']", signals.get("atr_m1_pips"), False),
            (
                "context['atr_m1_pips']",
                (
                    (context or {}).get("atr_m1_pips")
                    if isinstance(context, dict)
                    else None
                ),
                False,
            ),
            ("signals['atr_m1']", signals.get("atr_m1"), True),
        ]

        conversion_errors: List[str] = []

        for label, raw_value, needs_division in atr_sources:
            try:
                numeric_value = float(raw_value)
            except (TypeError, ValueError) as exc:
                conversion_errors.append(f"{label}: {exc}")
                continue

            if needs_division:
                if pip_size_value <= 0:
                    conversion_errors.append(
                        f"{label}: pip_size {pip_size_value} invalide pour conversion"
                    )
                    continue
                candidate = numeric_value / pip_size_value
            else:
                candidate = numeric_value

            if candidate > 0:
                return candidate
            conversion_errors.append(
                f"{label}: valeur <= 0 après conversion ({candidate})"
            )

        if conversion_errors:
            self.logger.debug(
                f"[{asset}] Burst scalping: conversions ATR M1 pips échouées ({'; '.join(conversion_errors)})"
            )
        return None

    # =====================================================
    # Tes règles scalping/liquidity existantes commencent ici
    # =====================================================

    def _rule_burst_scalping(
        self,
        asset: str,
        action: str,
        entry_price: float,
        meta: Dict[str, Any],
        sm_cfg: Dict[str, Any],
        context: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """
        Burst Scalping:
        - 1 seule position broker (volume unique calculé plus tard)
        - Pas de TP (trailing global)
        - burst_size est virtuel (logique interne)
        """
        import uuid

        if action not in ("BUY", "SELL"):
            return None

        entry_mode = str(
            sm_cfg.get("entry_mode", "MARKET")
        ).upper()  # MARKET / BUY_LIMIT / SELL_LIMIT
        burst_size = int(sm_cfg.get("burst_size", 8) or 8)
        basket_id = f"burst_{asset.upper()}_{uuid.uuid4().hex[:8]}"

        return {
            "strategy_type": "scalping",
            "rule_name": "burst_scalping",
            "execution_status": "ready",
            "action": action,
            "asset": asset,
            "order_type": entry_mode,
            "entry_price": float(entry_price) if entry_mode != "MARKET" else None,
            "burst_size": burst_size,  # virtuel (utile pour ta logique interne)
            "basket_id": basket_id,
            "fusion_data": {
                "fused_confidence": 1.0,  # Fallback pour ancienne règle sans fusion
            },
            "meta": {"burst": True, "entry_source": "core_decision"},
        }

    def _get_bars(self, asset: str, timeframe: str, count: int):
        """
        Récupère un DataFrame OHLC pour `asset` et `timeframe`.
        Essaie d'abord phase_observer (si dispo), sinon MT5Connector.
        Doit renvoyer un df avec colonnes: ['open','high','low','close','time'] indexé par time.
        """
        try:
            # 1) PhaseObserver (si ton app remonte déjà MTF dans le contexte)
            if hasattr(self, "phase_observer") and hasattr(
                self.phase_observer, "get_bars"
            ):
                df = self.phase_observer.get_bars(asset, timeframe, count)
                if df is not None and len(df) >= min(10, count):
                    return df

            # 2) MT5Connector (fallback standard)
            if hasattr(self, "mt5_connector") and hasattr(
                self.mt5_connector, "get_recent_bars"
            ):
                df = self.mt5_connector.get_recent_bars(asset, timeframe, count)
                return df
        except Exception as e:
            self.logger.warning(f"[{asset}] _get_bars({timeframe}) failed: {e}")

        return None

    def _is_range_environment(self, df15, df5, params) -> bool:
        """
        Confirme un 'vrai' range plat via M15 + M5.
        - M15 définit le couloir (HH/LL sur lookback_m15)
        - M5 doit rester majoritairement à l’intérieur du couloir M15
        et représenter une amplitude 'suffisamment contenue'
        (range5 / range15 < max_consolidation_ratio)
        - Optionnel: vérifier qu'il n'y a pas eu de close > HH15 ou < LL15
        au-delà d'une petite tolérance (anti-breakout).
        """
        if df15 is None or df5 is None:
            return False
        if len(df15) < params["lookback_m15"] or len(df5) < params["lookback_m5"]:
            return False

        recent15 = df15.tail(params["lookback_m15"])
        recent5 = df5.tail(params["lookback_m5"])

        hh15 = float(recent15["high"].max())
        ll15 = float(recent15["low"].min())
        range15 = hh15 - ll15
        if range15 <= 0:
            return False

        hh5 = float(recent5["high"].max())
        ll5 = float(recent5["low"].min())
        range5 = hh5 - ll5

        # M5 doit être "concentré" par rapport à M15
        if (range5 / range15) >= params["max_consolidation_ratio"]:
            return False

        # Anti-breakout: peu (ou pas) de clôtures qui sortent franchement du couloir M15
        tol = params["breakout_tolerance_frac"] * range15
        closes = recent5["close"]
        if (closes > (hh15 + tol)).sum() > params["max_breakout_closes"]:
            return False
        if (closes < (ll15 - tol)).sum() > params["max_breakout_closes"]:
            return False

        return True

    def _rule_liquidity_sweep(
        self,
        df: pd.DataFrame,
        meta: Dict[str, Any],
        lookback: int = 20,
    ) -> Optional[str]:
        """
        Détecte un sweep simple des HH/LL sur 'lookback' barres.
        Contrarian:
        - SELL si le close casse le plus haut récent (sweep au-dessus)
        - BUY  si le close casse le plus bas récent (sweep en-dessous)
        Retourne "BUY" / "SELL" / None.
        """
        try:
            if df is None or lookback is None or int(lookback) < 2:
                return None
            if len(df) < int(lookback):
                return None

            recent = df.iloc[-int(lookback) :]

            # Cast robustes
            hi = pd.to_numeric(recent["high"], errors="coerce")
            lo = pd.to_numeric(recent["low"], errors="coerce")
            cl = pd.to_numeric(df["close"].iloc[-1], errors="coerce")

            hh = float(hi.max()) if np.isfinite(hi.max()) else float("nan")
            ll = float(lo.min()) if np.isfinite(lo.min()) else float("nan")
            close = float(cl) if np.isfinite(cl) else float("nan")

            if not (np.isfinite(hh) and np.isfinite(ll) and np.isfinite(close)):
                return None

            # Heuristique sweep (contrarian)
            if close >= hh:
                return "SELL"
            if close <= ll:
                return "BUY"
            return None
        except Exception:
            return None

    # === [PATTERN MARUBOZU SUPPRIMÉ - Session 23 Nov 2025] ===
    # Fonction _rule_marubozu_range supprimée (215 lignes)
    # Raison : Pattern de bougie jamais utilisé, code mort

    def _rule_range_accumulation(
        self,
        df: pd.DataFrame,
        asset: str,
        price: float,
        action: str,
        meta: Dict[str, Any],
        cfg: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """
        Détecte un range plat (accumulation) et prend un trade
        sur les extrêmes (haut/bas du range).
        Patché pour intégrer le dernier pattern bougie détecté.
        """
        lookback = int(cfg.get("lookback_bars", 20))
        tolerance = float(cfg.get("tolerance_frac", 0.15))
        if len(df) < lookback:
            return None

        recent = df.tail(lookback)
        hh, ll = float(recent["high"].max()), float(recent["low"].min())
        rng = hh - ll
        if rng <= 0:
            return None

        top_zone = hh - tolerance * rng
        bot_zone = ll + tolerance * rng
        latest_pat = meta.get("latest_pattern")

        dec = None
        if price >= top_zone:
            dec = {
                "action": "SELL",
                "asset": asset,
                "entry_price": price,
                "target_sl_pips": float(cfg.get("sl_pips", 30)),
                "target_tp_pips": float(cfg.get("tp_pips", 30)),
                "rule_name": "range_accumulation_top",
                "strategy_type": "scalping",
                "confidence": 0.7,
            }
        elif price <= bot_zone:
            dec = {
                "action": "BUY",
                "asset": asset,
                "entry_price": price,
                "target_sl_pips": float(cfg.get("sl_pips", 30)),
                "target_tp_pips": float(cfg.get("tp_pips", 30)),
                "rule_name": "range_accumulation_low",
                "strategy_type": "scalping",
                "confidence": 0.7,
            }

        if dec and latest_pat:
            dec["rule_name"] += f"+pattern:{latest_pat.get('pattern')}"
            dec.setdefault("meta", {})["pattern"] = latest_pat

        return dec

    def get_parameters(self) -> Dict[str, any]:
        """
        Retourne un snapshot des paramètres runtime de la stratégie (pour logs/diagnostic).
        """
        try:
            sca_cfg = (self.config_manager.get_strategy_config("scalping") or {}).copy()
        except Exception:
            sca_cfg = {}
        return {
            "name": "scalping",
            "configured": bool(sca_cfg),
            "config_keys": list(sca_cfg.keys()),
        }

    def update_strategy_parameters(self, **kwargs) -> None:
        """
        Mise à jour dynamique de quelques paramètres légers (ex: seuils).
        On reste défensif: on ne casse rien si une clé n’existe pas.
        """
        try:
            sca_cfg = self.config_manager.get_strategy_config("scalping") or {}
            changed = []
            for k, v in kwargs.items():
                if k in sca_cfg:
                    sca_cfg[k] = v
                    changed.append(k)
            if changed:
                # si tu as une API pour renvoyer la config modifiée dans le ConfigManager, appelle-la ici.
                self.logger.info(f"[SCALPING] Params mis à jour: {changed}")
        except Exception as e:
            self.logger.warning(f"[SCALPING] update_strategy_parameters skipped: {e}")

    def evaluate_exit(
        self, context: Dict[str, Any], open_positions: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Politique de sortie par défaut (no-op) — renvoie une liste vide si pas de conditions spécifiques.
        L’executor ou d’autres modules peuvent fermer les positions via trailing/SL/TP.
        """
        try:
            # Exemple de garde-fou: on pourrait fermer des paniers "burst" sur condition extrême.
            # Ici, on garde un comportement neutre : pas de force-close.
            return []
        except Exception as e:
            self.logger.warning(f"[SCALPING] evaluate_exit skipped: {e}")
            return []

    # ==========================================================
    # =============      ADAPTATION TP/SL BASE     =============
    # ==========================================================
    def _dynamic_tp_sl_from_vol_atr(
        self, signals: Dict[str, Any], meta: Dict[str, Any], config: Dict[str, Any]
    ) -> Tuple[float, float, str]:
        """
        Calcule SL/TP (en pips) selon la vol% et l’ATR (fallback propre).
        """
        base_sl = float(config.get("stop_loss_pips", 12) or 12)
        base_tp = float(config.get("take_profit_pips", 18) or 18)

        vol_pct = self._extract_vol_pct(signals)
        atr_m5 = float(signals.get("atr_m5", 0.0) or 0.0)
        atr_m5_p = (atr_m5 / meta["pip_size"]) if meta["pip_size"] > 0 else 0.0

        adapt = self.config_manager.get("adaptation_settings", {}) or {}
        vols = adapt.get("volatility_thresholds", {}) or {}
        low, high = float(vols.get("low", 0.05)), float(vols.get("high", 0.5))

        scalping_adapt = adapt.get("scalping", {}) or {}
        sl_high = float(scalping_adapt.get("stop_loss_pips_high_vol", base_sl))
        tp_high = float(scalping_adapt.get("take_profit_pips_high_vol", base_tp))
        sl_low = float(scalping_adapt.get("stop_loss_pips_low_vol", base_sl))
        tp_low = float(scalping_adapt.get("take_profit_pips_low_vol", base_tp))

        if vol_pct >= high:
            sl_pips = max(sl_high, atr_m5_p * 0.8)
            tp_pips = tp_high
            tag = "high_vol"
        elif vol_pct <= low:
            sl_pips = max(sl_low, atr_m5_p * 0.6)
            tp_pips = tp_low
            tag = "low_vol"
        else:
            sl_pips = max(base_sl, atr_m5_p * 0.7)
            tp_pips = base_tp
            tag = "normal_vol"

        # légère correction spread
        spread_pips = meta["spread_pips"]
        tp_pips = max(1.0, tp_pips - spread_pips)
        sl_pips = max(1.0, sl_pips + spread_pips * 0.5)
        return float(sl_pips), float(tp_pips), tag

    # ==========================================================
    # =============            HELPERS            =============
    # ==========================================================

    def _has_blocking_news(self, context: Dict[str, Any]) -> bool:
        try:
            return bool(
                self.config_manager.check_news_schedule(
                    context, context.get("economic_calendar", [])
                )
            )
        except Exception:
            return False

    def _infer_action_from_signals(self, signals: Dict[str, Any]) -> Optional[str]:
        mtf_dir = str(signals.get("mtf_direction", "none")).lower()
        if mtf_dir in {"up", "down"}:
            return "BUY" if mtf_dir == "up" else "SELL"

        phase = str(
            signals.get("phase_memory_stabilized", signals.get("phase", ""))
        ).lower()
        if any(
            k in phase for k in ["bull", "up", "accumulation", "expansion", "trend"]
        ):
            return "BUY"
        if any(k in phase for k in ["bear", "down", "distribution"]):
            return "SELL"
        return None

    def _safe_price_from_signals(self, signals: Dict[str, Any]) -> Optional[float]:
        for k in ("current_price", "last_close", "close", "entry_price"):
            v = signals.get(k)
            try:
                if isinstance(v, (int, float)) and v > 0:
                    return float(v)
            except Exception:
                continue
        return None

    def _safe_asset_meta(
        self,
        asset: str,
        signals: Dict[str, Any],
        context: Dict[str, Any],
        config: Dict[str, Any],
    ) -> Dict[str, Any]:
        md_asset = (context.get("market_data", {}) or {}).get(asset, {}) or {}
        si = (md_asset.get("symbol_info") or config.get("symbol_info") or {}) or {}

        def _num(x, d=0.0):
            try:
                v = float(x)
                return v if math.isfinite(v) else d
            except Exception:
                return d

        point = _num(si.get("point"), 0.00001)
        digits = int(si.get("digits", 5) or 5)
        pip_points = 10.0 if digits in (3, 5) else 1.0
        pip_size = point * pip_points

        spread_points = _num(
            md_asset.get("current_spread_points", signals.get("spread", 0.0)), 0.0
        )
        spread_pips = spread_points / pip_points

        return {
            "point": point,
            "digits": digits,
            "pip_points": pip_points,
            "pip_size": pip_size,
            "spread_pips": spread_pips,
        }

    def _extract_vol_pct(self, signals: Dict[str, Any]) -> float:
        if isinstance(signals.get("volatility_pct"), (int, float)):
            return float(signals["volatility_pct"])
        if isinstance(signals.get("volatility_percentage"), (int, float)):
            return float(signals["volatility_percentage"])
        v = signals.get("volatility")
        if isinstance(v, (int, float)):
            v = float(v)
            return v * 100.0 if v <= 1.0 else v
        return 0.0

    def _calculate_m1_momentum_from_candles(self, df_m1: pd.DataFrame) -> str:
        """
        ✅ CORRIGÉ (15 DEC 2025): Calcule le momentum basé sur les 8 dernières bougies M1

        Règles:
        - 8 dernières bougies M1
        - Compter bougies vertes (close > open)
        - 5+ bougies vertes → BULLISH
        - 3- bougies vertes → BEARISH
        - 4 bougies vertes → NEUTRAL
        """
        if df_m1 is None or len(df_m1) < 8:
            return "NEUTRAL"

        # Analyser 8 dernières bougies
        recent = df_m1.tail(8)
        green_candles = 0

        # Compter bougies vertes
        for idx, row in recent.iterrows():
            if row["close"] > row["open"]:
                green_candles += 1

        # 🔍 DEBUG CRITIQUE
        self.logger.debug(
            f"[M1_MOMENTUM] {green_candles}/8 bougies vertes "
            f"→ ratio={green_candles/8:.0%}"
        )

        # Logique de décision
        if green_candles >= 5:  # 62.5%+ bougies vertes
            return "BULLISH"
        elif green_candles <= 3:  # 37.5%- bougies vertes
            return "BEARISH"
        return "NEUTRAL"

    # --- indicateurs génériques (utiles si tu veux enrichir plus tard)
    @staticmethod
    def _sma(series: pd.Series, period: int) -> pd.Series:
        if series is None or period <= 1:
            return series
        return series.rolling(window=period, min_periods=1).mean()

    @staticmethod
    def _std(series: pd.Series, period: int) -> pd.Series:
        if series is None or period <= 1:
            return series * 0
        return series.rolling(window=period, min_periods=1).std(ddof=0)

    @staticmethod
    def _atr(df: pd.DataFrame, period: int = 14) -> float:
        """
        ATR robuste sur Series Pandas (pas de numpy.reduce pour conserver .rolling()).
        Retourne le dernier ATR (float) ou NaN si données insuffisantes.
        """
        try:
            if df is None or len(df) < max(2, int(period)):
                return float("nan")

            h = pd.to_numeric(df["high"], errors="coerce")
            l = pd.to_numeric(df["low"], errors="coerce")
            c = pd.to_numeric(df["close"], errors="coerce")
            pc = c.shift(1)

            # True Range en Pandas → on garde une Series pour pouvoir .rolling()
            tr = pd.concat([(h - l).abs(), (h - pc).abs(), (l - pc).abs()], axis=1).max(
                axis=1
            )

            atr_series = tr.rolling(window=int(period), min_periods=int(period)).mean()
            atr = atr_series.iloc[-1]
            return float(atr) if pd.notna(atr) and atr > 0 else float("nan")
        except Exception:
            return float("nan")
