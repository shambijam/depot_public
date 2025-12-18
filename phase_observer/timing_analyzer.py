"""
⏱️ TIMING ANALYZER - Analyse temporelle du Footprint M1

Objectif:
    Améliorer la qualité du signal footprint en analysant la distribution
    temporelle des ticks sur la bougie M1 (60 secondes).

Métriques calculées:
    1. Concentration temporelle (% volume dans chaque quartile 15s)
    2. Vitesse relative (ticks/sec buy vs sell)
    3. Distribution par quartiles
    4. Score timing (0-5 points) ajouté au footprint_score_brut

Architecture:
    footprint_validator() → calculate_timing_metrics() → timing_score (0-5 pts)

Date: 17 Décembre 2025
"""

from __future__ import annotations
from typing import Dict, Any, Optional
import pandas as pd
import numpy as np
import logging
import time
from datetime import datetime

logger = logging.getLogger(__name__)


def calculate_timing_metrics(
    ticks_df: pd.DataFrame,
    start_ts: pd.Timestamp,
    coverage_s: float,
    asset: Optional[str] = None,
    asset_config: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    🎯 Calcule les métriques de timing pour une bougie M1

    Args:
        ticks_df: DataFrame des ticks filtrés [start_ts, end_ts)
                  Colonnes: ['time', 'price', 'size', 'side_norm']
        start_ts: Timestamp de début de la bougie M1
        coverage_s: Durée réelle de couverture des ticks (0-60s)

    Returns:
        Dict contenant:
        {
            "buy_concentration_q1": float,  # % volume buy dans Q1 (0-15s)
            "sell_concentration_q1": float,
            "buy_velocity": float,          # ticks buy/sec
            "sell_velocity": float,
            "velocity_ratio": float,        # buy_velocity / sell_velocity
            "buy_q1_pct": float,            # Distribution quartiles (0-1)
            "buy_q2_pct": float,
            "buy_q3_pct": float,
            "buy_q4_pct": float,
            "sell_q1_pct": float,
            "sell_q2_pct": float,
            "sell_q3_pct": float,
            "sell_q4_pct": float,
            "timing_score": float,          # 0-5 points
            "timing_quality": str,          # POOR / FAIR / GOOD / EXCELLENT
            "timing_analysis_ms": float     # Temps de calcul
        }
    """
    analysis_start = time.perf_counter()

    # ========================================================================
    # 0️⃣ CHARGEMENT CONFIGURATION TIMING ANALYZER
    # ========================================================================
    timing_config = {}
    if asset_config:
        # Charger depuis overrides.scalping.timing_analyzer
        overrides = asset_config.get("overrides", {})
        scalping_overrides = overrides.get("scalping", {})
        timing_config = scalping_overrides.get("timing_analyzer", {})

    # Paramètres par défaut si config absente
    enabled = timing_config.get("enabled", True)
    if not enabled:
        return _get_default_timing_metrics(0.0)

    # Charger les sections de config
    concentration_cfg = timing_config.get("concentration_analysis", {})
    velocity_cfg = timing_config.get("velocity_analysis", {})
    distribution_cfg = timing_config.get("distribution_scoring", {})
    time_of_day_cfg = timing_config.get("time_of_day_adjustments", {})
    scoring_cfg = timing_config.get("scoring_system", {})
    performance_cfg = timing_config.get("performance_optimization", {})

    # Extraire les paramètres avec valeurs par défaut
    # Concentration
    q1_weight = concentration_cfg.get("q1_weight", 0.60)
    q2_weight = concentration_cfg.get("q2_weight", 0.25)
    q3_q4_weight = concentration_cfg.get("q3_q4_weight", 0.15)
    q1_strong = concentration_cfg.get("q1_strong", 0.80)
    q1_expected_min = concentration_cfg.get("q1_expected_min", 0.60)
    q1_weak = concentration_cfg.get("q1_weak", 0.40)
    buy_conc_bonus = concentration_cfg.get("buy_concentration_bonus", {})
    sell_conc_bonus = concentration_cfg.get("sell_concentration_bonus", {})

    # Velocity
    vel_thresholds = velocity_cfg.get("scoring_thresholds", {})
    vel_very_strong = vel_thresholds.get("very_strong", 0.3)
    vel_strong = vel_thresholds.get("strong", 0.5)
    vel_moderate = vel_thresholds.get("moderate", 0.8)
    vel_neutral = vel_thresholds.get("neutral", 1.2)
    vel_reversal = vel_thresholds.get("reversal_alert", 1.5)
    velocity_weight = velocity_cfg.get("velocity_weight", 0.25)

    # Distribution
    ideal_quartile_count = distribution_cfg.get("ideal_quartile_count", 2)
    max_quartile_count = distribution_cfg.get("max_quartile_count", 3)
    penalty_4_quartiles = distribution_cfg.get("penalty_4_quartiles", 0.7)
    quartile_significance_threshold = distribution_cfg.get("quartile_significance_threshold", 0.15)

    # Scoring system weights
    component_weights = scoring_cfg.get("component_weights", {})
    weight_concentration = component_weights.get("concentration_score", 0.50)
    weight_velocity = component_weights.get("velocity_score", 0.25)
    weight_distribution = component_weights.get("distribution_score", 0.15)
    weight_consistency = component_weights.get("consistency_score", 0.10)

    # Performance
    min_ticks = performance_cfg.get("min_ticks_for_analysis", 5)

    # ========================================================================
    # 1️⃣ VALIDATION DONNÉES
    # ========================================================================
    if ticks_df is None or ticks_df.empty:
        return _get_default_timing_metrics(0.0)

    if not isinstance(ticks_df, pd.DataFrame):
        return _get_default_timing_metrics(0.0)

    # Vérifier colonnes requises
    required_cols = {'time', 'side_norm', 'size'}
    if not required_cols.issubset(ticks_df.columns):
        logger.debug(f"[TIMING] Colonnes manquantes: {required_cols - set(ticks_df.columns)}")
        return _get_default_timing_metrics(0.0)

    df = ticks_df.copy()

    # ========================================================================
    # 2️⃣ CALCUL TEMPS RELATIF (0-60s depuis start_ts)
    # ========================================================================
    try:
        # Temps relatif en secondes depuis start_ts
        df['time_relative'] = (df['time'] - start_ts).dt.total_seconds()

        # Filtrer valeurs valides (0-60s)
        df = df[(df['time_relative'] >= 0) & (df['time_relative'] < 60)].copy()

        if df.empty:
            return _get_default_timing_metrics(0.0)

        # Assigner quartile (Q1=0-15s, Q2=15-30s, Q3=30-45s, Q4=45-60s)
        df['quartile'] = pd.cut(
            df['time_relative'],
            bins=[0, 15, 30, 45, 60],
            labels=['Q1', 'Q2', 'Q3', 'Q4'],
            include_lowest=True
        )

    except Exception as e:
        logger.debug(f"[TIMING] Erreur calcul temps relatif: {e}")
        return _get_default_timing_metrics(0.0)

    # ========================================================================
    # 3️⃣ SÉPARATION BUY / SELL
    # ========================================================================
    df_buy = df[df['side_norm'] == 'buy'].copy()
    df_sell = df[df['side_norm'] == 'sell'].copy()

    total_buy_volume = float(df_buy['size'].sum())
    total_sell_volume = float(df_sell['size'].sum())

    buy_tick_count = len(df_buy)
    sell_tick_count = len(df_sell)

    # ========================================================================
    # 4️⃣ CONCENTRATION PAR QUARTILE (% du volume dans chaque quartile)
    # ========================================================================
    buy_q_distribution = {}
    sell_q_distribution = {}

    for q in ['Q1', 'Q2', 'Q3', 'Q4']:
        # Buy
        buy_q_vol = float(df_buy[df_buy['quartile'] == q]['size'].sum())
        buy_q_pct = buy_q_vol / total_buy_volume if total_buy_volume > 0 else 0.0
        buy_q_distribution[q] = buy_q_pct

        # Sell
        sell_q_vol = float(df_sell[df_sell['quartile'] == q]['size'].sum())
        sell_q_pct = sell_q_vol / total_sell_volume if total_sell_volume > 0 else 0.0
        sell_q_distribution[q] = sell_q_pct

    # Concentration Q1 (première 15 secondes)
    buy_concentration_q1 = buy_q_distribution.get('Q1', 0.0)
    sell_concentration_q1 = sell_q_distribution.get('Q1', 0.0)

    # ========================================================================
    # 5️⃣ VITESSE (ticks/sec)
    # ========================================================================
    # Calculer la durée réelle par side (pour éviter division par zéro)
    buy_duration_s = coverage_s if buy_tick_count > 0 else 1.0
    sell_duration_s = coverage_s if sell_tick_count > 0 else 1.0

    buy_velocity = buy_tick_count / max(buy_duration_s, 1.0)
    sell_velocity = sell_tick_count / max(sell_duration_s, 1.0)

    velocity_ratio = buy_velocity / sell_velocity if sell_velocity > 0 else 1.0

    # ========================================================================
    # 6️⃣ SCORING TIMING (0-5 points normalisé par pondération)
    # ========================================================================

    # --- 6.1 Concentration Score (pondéré selon concentration_analysis) ---
    # Analyse Q1 concentration (premier quartile = critique)
    max_buy_conc_q1 = buy_q_distribution.get('Q1', 0.0)
    max_sell_conc_q1 = sell_q_distribution.get('Q1', 0.0)
    dominant_q1_concentration = max(max_buy_conc_q1, max_sell_conc_q1)

    # Score de base selon seuils configurés
    if dominant_q1_concentration >= q1_strong:
        concentration_score_raw = 5.0  # Très concentré (excellent)
    elif dominant_q1_concentration >= q1_expected_min:
        concentration_score_raw = 3.5  # Bon niveau
    elif dominant_q1_concentration >= q1_weak:
        concentration_score_raw = 2.0  # Faible
    else:
        concentration_score_raw = 0.5  # Très dispersé

    # Appliquer les bonus de concentration buy/sell
    buy_bonus_threshold = buy_conc_bonus.get("threshold", 0.70)
    buy_bonus_multiplier = buy_conc_bonus.get("multiplier", 1.3)
    sell_bonus_threshold = sell_conc_bonus.get("threshold", 0.70)
    sell_bonus_multiplier = sell_conc_bonus.get("multiplier", 1.4)

    if max_buy_conc_q1 >= buy_bonus_threshold:
        concentration_score_raw *= buy_bonus_multiplier
    elif max_sell_conc_q1 >= sell_bonus_threshold:
        concentration_score_raw *= sell_bonus_multiplier

    # Normaliser sur 5.0 max
    concentration_score = min(5.0, concentration_score_raw)

    # --- 6.2 Velocity Score (pondéré selon velocity_analysis) ---
    # Pour XAUUSD: velocity_ratio < 1.0 est normal (sell dominant)
    # Utiliser la distance depuis 1.0 comme indicateur de force directionnelle

    # Calculer l'asymétrie (distance de 1.0 = équilibre)
    if velocity_ratio < 1.0:
        # Sell dominant
        velocity_asymmetry = 1.0 / velocity_ratio if velocity_ratio > 0 else 999.0
        direction_bias = "sell"
    else:
        # Buy dominant
        velocity_asymmetry = velocity_ratio
        direction_bias = "buy"

    # Scoring selon thresholds configurés
    # very_strong (0.3) signifie ratio <= 0.3 ou >= 1/0.3 = très déséquilibré
    if velocity_ratio <= vel_very_strong or velocity_ratio >= (1.0 / vel_very_strong):
        velocity_score = 5.0  # Très fort déséquilibre
    elif velocity_ratio <= vel_strong or velocity_ratio >= (1.0 / vel_strong):
        velocity_score = 4.0  # Fort déséquilibre
    elif velocity_ratio <= vel_moderate or velocity_ratio >= (1.0 / vel_moderate):
        velocity_score = 3.0  # Déséquilibre modéré
    elif vel_moderate < velocity_ratio < vel_neutral:
        velocity_score = 2.0  # Léger déséquilibre
    else:
        velocity_score = 1.0  # Quasi-équilibre (neutre)

    # --- 6.3 Distribution Score (pondéré selon distribution_scoring) ---
    # Bonus si mouvement concentré (pas uniforme sur 4 quartiles)
    # Utiliser quartile_significance_threshold configuré
    buy_significant_quartiles = sum(
        1 for pct in buy_q_distribution.values()
        if pct > quartile_significance_threshold
    )
    sell_significant_quartiles = sum(
        1 for pct in sell_q_distribution.values()
        if pct > quartile_significance_threshold
    )
    # Prendre le max pour détecter concentration directionnelle
    max_significant = max(buy_significant_quartiles, sell_significant_quartiles)

    if max_significant <= ideal_quartile_count:
        distribution_score = 5.0  # Idéal: concentré sur 1-2 quartiles
    elif max_significant <= max_quartile_count:
        distribution_score = 3.0  # Acceptable: 3 quartiles
    else:
        # Appliquer pénalité pour 4 quartiles (trop dispersé)
        distribution_score = 5.0 * penalty_4_quartiles  # Ex: 5.0 * 0.7 = 3.5

    # --- 6.4 Consistency Score (nouveau) ---
    # Mesurer cohérence entre concentration et velocity
    # Si Q1 concentré ET velocity alignée = bon signal
    consistency_score = 2.5  # Score neutre par défaut

    # Vérifier alignement direction
    if dominant_q1_concentration >= q1_expected_min:
        # Q1 est bien concentré
        if (max_buy_conc_q1 > max_sell_conc_q1 and direction_bias == "buy") or \
           (max_sell_conc_q1 > max_buy_conc_q1 and direction_bias == "sell"):
            # Direction Q1 et velocity sont alignées
            consistency_score = 5.0
        else:
            # Divergence entre Q1 et velocity
            consistency_score = 1.0

    # ========================================================================
    # 6️⃣.5 CALCUL SCORE FINAL PONDÉRÉ
    # ========================================================================
    # Appliquer les poids configurés (total = 1.0)
    timing_score_raw = (
        concentration_score * weight_concentration +
        velocity_score * weight_velocity +
        distribution_score * weight_distribution +
        consistency_score * weight_consistency
    )

    # ========================================================================
    # 6️⃣.6 AJUSTEMENTS TIME OF DAY
    # ========================================================================
    time_of_day_multiplier = 1.0

    # Extraire l'heure GMT du timestamp start_ts
    if start_ts is not None and hasattr(start_ts, 'hour'):
        current_hour_gmt = start_ts.hour

        # Londres (7-11 GMT)
        london_cfg = time_of_day_cfg.get("london_open", {})
        london_hours = london_cfg.get("hours_gmt", [7, 11])
        if london_hours[0] <= current_hour_gmt < london_hours[1]:
            london_multiplier = london_cfg.get("scoring_adjustment", 1.1)
            time_of_day_multiplier = london_multiplier
            logger.debug(f"[TIMING] Session Londres détectée (heure {current_hour_gmt} GMT) - Multiplier: {london_multiplier}")

        # US Session (13-17 GMT)
        us_cfg = time_of_day_cfg.get("us_session", {})
        us_hours = us_cfg.get("hours_gmt", [13, 17])
        if us_hours[0] <= current_hour_gmt < us_hours[1]:
            # Pour l'instant pas de multiplier pour US, juste marquage
            logger.debug(f"[TIMING] Session US détectée (heure {current_hour_gmt} GMT)")

        # Asian Session (0-6 GMT)
        asian_cfg = time_of_day_cfg.get("asian_session", {})
        asian_hours = asian_cfg.get("hours_gmt", [0, 6])
        if asian_hours[0] <= current_hour_gmt < asian_hours[1]:
            # Vérifier si concentration respecte min_concentration
            asian_min_conc = asian_cfg.get("min_concentration", 0.50)
            if dominant_q1_concentration < asian_min_conc:
                # Pénalité pour concentration insuffisante en session asiatique
                time_of_day_multiplier = 0.8
                logger.debug(
                    f"[TIMING] Session Asie (heure {current_hour_gmt} GMT) - "
                    f"Concentration Q1 {dominant_q1_concentration:.2f} < {asian_min_conc} - "
                    f"Pénalité appliquée"
                )

    # Appliquer le multiplicateur
    timing_score = timing_score_raw * time_of_day_multiplier

    # Clamp final (0-5 pts)
    timing_score = float(max(0.0, min(5.0, timing_score)))

    # ========================================================================
    # 7️⃣ QUALITÉ TIMING
    # ========================================================================
    if timing_score >= 4.0:
        timing_quality = "EXCELLENT"
    elif timing_score >= 3.0:
        timing_quality = "GOOD"
    elif timing_score >= 2.0:
        timing_quality = "FAIR"
    else:
        timing_quality = "POOR"

    # ========================================================================
    # 8️⃣ RÉSULTAT FINAL
    # ========================================================================
    analysis_ms = (time.perf_counter() - analysis_start) * 1000.0

    return {
        # Concentration Q1
        "buy_concentration_q1": round(buy_concentration_q1, 3),
        "sell_concentration_q1": round(sell_concentration_q1, 3),

        # Vitesse
        "buy_velocity": round(buy_velocity, 2),
        "sell_velocity": round(sell_velocity, 2),
        "velocity_ratio": round(velocity_ratio, 2),

        # Distribution quartiles
        "buy_q1_pct": round(buy_q_distribution.get('Q1', 0.0), 3),
        "buy_q2_pct": round(buy_q_distribution.get('Q2', 0.0), 3),
        "buy_q3_pct": round(buy_q_distribution.get('Q3', 0.0), 3),
        "buy_q4_pct": round(buy_q_distribution.get('Q4', 0.0), 3),
        "sell_q1_pct": round(sell_q_distribution.get('Q1', 0.0), 3),
        "sell_q2_pct": round(sell_q_distribution.get('Q2', 0.0), 3),
        "sell_q3_pct": round(sell_q_distribution.get('Q3', 0.0), 3),
        "sell_q4_pct": round(sell_q_distribution.get('Q4', 0.0), 3),

        # Scores détaillés (sur 5.0 chacun avant pondération)
        "concentration_score": round(concentration_score, 2),
        "velocity_score": round(velocity_score, 2),
        "distribution_score": round(distribution_score, 2),
        "consistency_score": round(consistency_score, 2),
        "time_of_day_multiplier": round(time_of_day_multiplier, 2),

        # Score final
        "timing_score": round(timing_score, 2),
        "timing_quality": timing_quality,

        # Métadonnées
        "tick_count_buy": int(buy_tick_count),
        "tick_count_sell": int(sell_tick_count),
        "timing_analysis_ms": round(analysis_ms, 2)
    }


def _get_default_timing_metrics(analysis_ms: float = 0.0) -> Dict[str, Any]:
    """
    Retourne des métriques timing par défaut (données insuffisantes)
    """
    return {
        "buy_concentration_q1": 0.0,
        "sell_concentration_q1": 0.0,
        "buy_velocity": 0.0,
        "sell_velocity": 0.0,
        "velocity_ratio": 1.0,
        "buy_q1_pct": 0.0,
        "buy_q2_pct": 0.0,
        "buy_q3_pct": 0.0,
        "buy_q4_pct": 0.0,
        "sell_q1_pct": 0.0,
        "sell_q2_pct": 0.0,
        "sell_q3_pct": 0.0,
        "sell_q4_pct": 0.0,
        "concentration_score": 0.0,
        "velocity_score": 0.0,
        "distribution_score": 0.0,
        "consistency_score": 0.0,
        "time_of_day_multiplier": 1.0,
        "timing_score": 0.0,
        "timing_quality": "N/A",
        "tick_count_buy": 0,
        "tick_count_sell": 0,
        "timing_analysis_ms": round(analysis_ms, 2)
    }
