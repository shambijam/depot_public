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
from typing import Dict, Any
import pandas as pd
import numpy as np
import logging
import time

logger = logging.getLogger(__name__)


def calculate_timing_metrics(
    ticks_df: pd.DataFrame,
    start_ts: pd.Timestamp,
    coverage_s: float
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
    # 6️⃣ SCORING TIMING (0-5 points)
    # ========================================================================
    timing_score = 0.0

    # --- 6.1 Concentration (0-2 pts) ---
    # Détecter le quartile dominant (buy ou sell)
    max_buy_conc = max(buy_q_distribution.values()) if buy_q_distribution else 0.0
    max_sell_conc = max(sell_q_distribution.values()) if sell_q_distribution else 0.0
    dominant_concentration = max(max_buy_conc, max_sell_conc)

    if dominant_concentration >= 0.70:
        concentration_pts = 2.0  # Très concentré (bon signe)
    elif dominant_concentration >= 0.50:
        concentration_pts = 1.5
    elif dominant_concentration >= 0.35:
        concentration_pts = 1.0
    else:
        concentration_pts = 0.5  # Trop dispersé

    timing_score += concentration_pts

    # --- 6.2 Velocity Ratio (0-2 pts) ---
    # Ratio élevé = mouvement directionnel fort
    abs_velocity_ratio = max(velocity_ratio, 1/velocity_ratio) if velocity_ratio > 0 else 1.0

    if abs_velocity_ratio >= 2.0:
        velocity_pts = 2.0  # Mouvement très fort
    elif abs_velocity_ratio >= 1.5:
        velocity_pts = 1.5
    elif abs_velocity_ratio >= 1.2:
        velocity_pts = 1.0
    else:
        velocity_pts = 0.5  # Trop équilibré

    timing_score += velocity_pts

    # --- 6.3 Distribution (0-1 pt) ---
    # Bonus si mouvement concentré (pas uniforme sur 4 quartiles)
    # Compter quartiles significatifs (>15% du volume)
    buy_significant_quartiles = sum(1 for pct in buy_q_distribution.values() if pct > 0.15)
    sell_significant_quartiles = sum(1 for pct in sell_q_distribution.values() if pct > 0.15)
    min_significant = min(buy_significant_quartiles, sell_significant_quartiles)

    if min_significant <= 2:
        distribution_pts = 1.0  # Concentré sur 1-2 quartiles
    elif min_significant == 3:
        distribution_pts = 0.5  # Modérément concentré
    else:
        distribution_pts = 0.0  # Trop uniforme (4 quartiles)

    timing_score += distribution_pts

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

        # Scores détaillés
        "concentration_pts": round(concentration_pts, 1),
        "velocity_pts": round(velocity_pts, 1),
        "distribution_pts": round(distribution_pts, 1),

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
        "concentration_pts": 0.0,
        "velocity_pts": 0.0,
        "distribution_pts": 0.0,
        "timing_score": 0.0,
        "timing_quality": "N/A",
        "tick_count_buy": 0,
        "tick_count_sell": 0,
        "timing_analysis_ms": round(analysis_ms, 2)
    }
