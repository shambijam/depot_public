"""
🚪 TIMING GATEKEEPER - Filtre pré-trade pour scalping USDJPY

Rôle : Veto binaire (PASS/VETO) basé sur :
- Horaires optimaux (Sessions Asie liquide + London Fix)
- Liquidité (tick rate, coverage)
- Transitions de session (éviter)

PAS de scoring complexe - juste un filtre de survie.

Date : 25 Décembre 2025 - Simplification radicale
"""

from __future__ import annotations
from typing import Dict, Any, Optional
import pandas as pd
import logging
import time

logger = logging.getLogger(__name__)


def evaluate_trading_conditions(
    asset: str,
    current_time: pd.Timestamp,
    ticks_df: pd.DataFrame,
    market_context: Dict[str, Any],
    asset_config: Optional[Dict[str, Any]] = None,
    scalping_config: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    🚪 GATEKEEPER: Évalue si les conditions de trading sont acceptables

    Retourne PASS ou VETO basé sur :
    1. Heure GMT (sessions optimales USDJPY)
    2. Liquidité des ticks (tick rate, coverage)
    3. Transitions de session (à éviter)

    Args:
        asset: Symbole de trading (USDJPY, etc.)
        current_time: Timestamp actuel (pour vérif heure GMT)
        ticks_df: DataFrame des ticks récents
        market_context: Contexte marché (non utilisé pour l'instant)
        asset_config: Config spécifique asset (fallback, optionnel)
        scalping_config: Config stratégie scalping globale (prioritaire)

    Returns:
        {
            "verdict": "PASS" | "VETO",
            "veto_reason": str | None,
            "quality_metrics": {
                "hour_gmt": int,
                "session": str,
                "tick_count": int,
                "tick_rate": float,
                "coverage_s": float,
                "liquidity_score": float  # 0-1
            },
            "timing_analysis_ms": float
        }
    """
    analysis_start = time.perf_counter()

    # ========================================================================
    # 0️⃣ CONFIGURATION (02 JAN 2026 - MERGE ASSET + GLOBAL avec priorité ASSET)
    # ========================================================================
    # 🐛 FIX BUG #1 & #2: Merge granulaire paramètre par paramètre
    # Asset-specific params écrasent les params globaux
    timing_config_global = {}
    timing_config_asset = {}

    # Charger config GLOBALE d'abord (base)
    if scalping_config:
        entry_rules = scalping_config.get("entry_rules", {})
        scalping_rules = entry_rules.get("scalping", {})
        timing_config_global = scalping_rules.get("timing_gatekeeper", {})
        if timing_config_global:
            logger.debug(f"[TIMING_CONFIG][{asset}] Config globale chargée")

    # Charger config ASSET (écrase global)
    if asset_config:
        overrides = asset_config.get("overrides", {})
        scalping_overrides = overrides.get("scalping", {})
        timing_config_asset = scalping_overrides.get("timing_gatekeeper", {})

        if timing_config_asset:
            logger.debug(f"[TIMING_CONFIG][{asset}] ✅ Config ASSET chargée (écrasera global)")
        else:
            logger.warning(f"[TIMING_CONFIG][{asset}] ⚠️ timing_config_asset est VIDE → config globale sera utilisée")

    # MERGE: Start avec global, puis écrase avec asset
    timing_config = dict(timing_config_global)  # Copie base globale
    timing_config.update(timing_config_asset)   # Écrase avec params asset-specific

    enabled = timing_config.get("enabled", True)
    if not enabled:
        return {
            "verdict": "PASS",
            "veto_reason": None,
            "quality_metrics": {},
            "timing_analysis_ms": 0.0,
            "note": "Gatekeeper désactivé"
        }

    # Seuils (avec fallback hardcodé si absent)
    min_tick_rate = timing_config.get("min_tick_rate", 1.0)  # ticks/sec
    min_coverage_s = timing_config.get("min_coverage_s", 40.0)  # secondes
    max_tick_rate = timing_config.get("max_tick_rate", 200.0)  # détection problème feed

    # 🔍 LOG (26 DEC 2025): Afficher les seuils chargés (31 DEC: DEBUG pour console propre)
    logger.debug(
        f"[TIMING_SEUILS][{asset}] min_tick_rate={min_tick_rate} | "
        f"min_coverage_s={min_coverage_s} | max_tick_rate={max_tick_rate}"
    )

    # ========================================================================
    # 1️⃣ VÉRIFICATION HEURE GMT + WHITELIST DYNAMIQUE (29 DEC 2025)
    # ========================================================================
    # Heures autorisées depuis config (dynamique)
    allowed_hours = timing_config.get("allowed_hours_gmt", [0, 1, 2, 3, 4, 5, 14, 15, 16])  # Défaut si config absente

    # Heures optimales et veto (config - pour fine-tuning uniquement)
    optimal_hours_cfg = timing_config.get("optimal_hours_gmt", {})
    asian_liquid_hours = optimal_hours_cfg.get("asian_liquid", [0, 6])
    london_fix_hours = optimal_hours_cfg.get("london_fix", [14, 17])

    hour_gmt = current_time.hour if hasattr(current_time, 'hour') else 12

    # Vérifier si heure autorisée (mais on continue l'analyse !)
    hour_is_allowed = hour_gmt in allowed_hours

    # 🔍 LOG: Config chargée (31 DEC: DEBUG pour console propre)
    logger.debug(
        f"[TIMING_CONFIG_CHECK][{asset}] allowed_hours={allowed_hours} | "
        f"current_hour={hour_gmt} | is_allowed={hour_is_allowed}"
    )

    # Déterminer session
    session = "UNKNOWN"
    session_quality = "UNKNOWN"

    # Sessions optimales
    if asian_liquid_hours[0] <= hour_gmt < asian_liquid_hours[1]:
        session = "ASIAN_LIQUID"
        session_quality = "EXCELLENT"
    elif london_fix_hours[0] <= hour_gmt < london_fix_hours[1]:
        session = "LONDON_FIX"
        session_quality = "EXCELLENT"
    elif 0 <= hour_gmt < 2:  # Asie précoce
        session = "ASIAN_EARLY"
        session_quality = "CONDITIONAL"  # Nécessite bonne liquidité
    elif 7 <= hour_gmt < 11:  # Londres solo
        session = "LONDON"
        session_quality = "GOOD"
    elif 18 <= hour_gmt < 24:  # Off-peak
        session = "OFF_PEAK"
        session_quality = "POOR"
    else:
        session = "OTHER"
        session_quality = "FAIR"

    # ========================================================================
    # 2️⃣ ANALYSE LIQUIDITÉ (Ticks)
    # ========================================================================
    if ticks_df is None or ticks_df.empty:
        return {
            "verdict": "VETO",
            "veto_reason": "Pas de données ticks disponibles pour analyse liquidité",
            "quality_metrics": {
                "hour_gmt": hour_gmt,
                "session": session,
                "session_quality": session_quality
            },
            "timing_analysis_ms": (time.perf_counter() - analysis_start) * 1000.0
        }

    # Calcul métriques ticks
    tick_count = len(ticks_df)

    # Coverage (étendue temporelle)
    if 'time' in ticks_df.columns:
        try:
            time_min = ticks_df['time'].min()
            time_max = ticks_df['time'].max()
            coverage_s = (time_max - time_min).total_seconds() if pd.notna(time_min) and pd.notna(time_max) else 0.0
        except:
            coverage_s = 0.0
    else:
        coverage_s = 0.0

    # Tick rate
    tick_rate = tick_count / max(coverage_s, 1.0)

    # Liquidity score (0-1)
    # Component 1: Tick rate (0-0.6)
    liquidity_score = 0.0
    if tick_rate >= 20.0:
        liquidity_score += 0.6  # Liquidité excellente
    elif tick_rate >= 10.0:
        liquidity_score += 0.4  # Bonne liquidité
    elif tick_rate >= 5.0:
        liquidity_score += 0.2  # Minimum acceptable

    # Component 2: Coverage (0-0.4)
    if coverage_s >= 55.0:
        liquidity_score += 0.4  # Bougie complète
    elif coverage_s >= 40.0:
        liquidity_score += 0.3  # Acceptable
    elif coverage_s >= 30.0:
        liquidity_score += 0.1  # Marginal

    # ========================================================================
    # 3️⃣ SYSTÈME DE VETO PONDÉRÉ (02 JAN 2026 - Fix veto binaire trop strict)
    # ========================================================================
    # Score de veto: 0-100 (0=pas de veto, 100=veto absolu)
    # Permet aux signaux OrderFlow forts de passer outre des vetos modérés
    veto_score = 0.0
    veto_reasons = []

    # 🔍 LOG (26 DEC 2025): Métriques test VETO (01 JAN 2026: DEBUG pour console propre)
    logger.debug(
        f"[TIMING_TEST_VETO][{asset}] "
        f"tick_count={tick_count} | coverage_s={coverage_s:.1f} | tick_rate={tick_rate:.1f} | "
        f"TEST: tick_rate({tick_rate:.1f}) < min_tick_rate({min_tick_rate}) = {tick_rate < min_tick_rate}"
    )

    # 🚨 A) HEURE NON AUTORISÉE (depuis config) - Veto PONDÉRÉ
    if not hour_is_allowed:
        # Veto modéré (peut être surpassé par signal fort)
        veto_score += 50.0
        veto_reasons.append(f"🚫 Heure {hour_gmt:02d}h GMT NON autorisée (whitelist: {allowed_hours})")
        logger.debug(f"[TIMING_HOUR_VETO][{asset}] Veto +50 (heure non autorisée)")

    # B) Coverage insuffisante - Veto FORT
    if coverage_s < min_coverage_s:
        # Veto fort (données insuffisantes)
        veto_score += 70.0
        veto_reasons.append(f"Coverage insuffisante ({coverage_s:.1f}s < {min_coverage_s}s)")

    # C) Tick rate trop bas - Veto PONDÉRÉ selon écart
    if tick_rate < min_tick_rate:
        # Pénalité proportionnelle à l'écart
        gap_pct = (min_tick_rate - tick_rate) / min_tick_rate
        penalty = min(60.0, gap_pct * 80.0)  # Max 60 points
        veto_score += penalty
        veto_reasons.append(f"Tick rate faible ({tick_rate:.1f} < {min_tick_rate} ticks/s)")
        logger.critical(f"[TIMING_VETO][{asset}] Veto +{penalty:.0f} (tick_rate={tick_rate:.1f})")

    # D) Session asiatique précoce - Veto MODÉRÉ
    if session == "ASIAN_EARLY" and tick_rate < 8.0:
        veto_score += 40.0
        veto_reasons.append(f"Session asiatique précoce + tick rate ({tick_rate:.1f} < 8.0)")

    # E) Tick rate anormal - Veto ABSOLU
    if tick_rate > max_tick_rate:
        veto_score = 100.0  # Veto absolu (problème technique)
        veto_reasons.append(f"Tick rate anormal ({tick_rate:.1f} > {max_tick_rate}) - problème feed")

    # F) Liquidité globale faible - Veto MODÉRÉ
    if liquidity_score < 0.3:
        veto_score += 45.0
        veto_reasons.append(f"Liquidité faible ({liquidity_score:.2f} < 0.30)")

    # G) Session Off-Peak - Veto FAIBLE
    if session_quality == "POOR":
        veto_score += 30.0
        veto_reasons.append(f"Session off-peak (GMT {hour_gmt:02d}h)")

    # Plafonnement à 100
    veto_score = min(100.0, veto_score)

    # ========================================================================
    # 4️⃣ VERDICT FINAL (compatibilité binaire + score pondéré)
    # ========================================================================
    # Verdict binaire pour compatibilité (veto si score >= 80)
    verdict = "VETO" if veto_score >= 80.0 else "PASS"
    veto_reason = " | ".join(veto_reasons) if veto_reasons else None

    analysis_ms = (time.perf_counter() - analysis_start) * 1000.0

    result = {
        "verdict": verdict,
        "veto_reason": veto_reason,
        "veto_score": round(veto_score, 1),  # ✅ AJOUTÉ (02 JAN 2026): Score pondéré 0-100
        "quality_metrics": {
            "hour_gmt": hour_gmt,
            "session": session,
            "session_quality": session_quality,
            "tick_count": int(tick_count),
            "tick_rate": round(tick_rate, 2),
            "coverage_s": round(coverage_s, 2),
            "liquidity_score": round(liquidity_score, 2)
        },
        "timing_analysis_ms": round(analysis_ms, 2)
    }

    # 🔍 LOG (26 DEC 2025): Verdict final (31 DEC: DEBUG pour console propre)
    logger.debug(
        f"[TIMING_VERDICT_FINAL][{asset}] verdict={verdict} | "
        f"veto_reason={veto_reason or 'None'} | "
        f"session={session} ({session_quality}) | GMT={hour_gmt:02d}h | "
        f"tick_rate={tick_rate:.1f}/s | min_required={min_tick_rate}/s"
    )

    # Log pour debug
    if verdict == "VETO":
        logger.info(f"[TIMING_VETO] {veto_reason} | Session={session} GMT={hour_gmt:02d}h")
    else:
        logger.debug(f"[TIMING_PASS] Session={session} GMT={hour_gmt:02d}h | Ticks={tick_count} Rate={tick_rate:.1f}/s")

    return result


def _get_default_timing_result(verdict: str = "VETO", reason: str = "Données insuffisantes") -> Dict[str, Any]:
    """Résultat par défaut en cas de données invalides"""
    return {
        "verdict": verdict,
        "veto_reason": reason,
        "quality_metrics": {},
        "timing_analysis_ms": 0.0
    }
