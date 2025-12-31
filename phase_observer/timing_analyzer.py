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
    # 0️⃣ CONFIGURATION (31 DEC 2025 - Priorité CONFIG ASSET)
    # ========================================================================
    timing_config = {}

    # PRIORITÉ 1: Config asset spécifique (EURUSD.json, GBPUSD.json, etc.)
    if asset_config:
        overrides = asset_config.get("overrides", {})
        scalping_overrides = overrides.get("scalping", {})
        timing_config = scalping_overrides.get("timing_gatekeeper", {})
        if timing_config:
            logger.debug(f"[TIMING_CONFIG_SOURCE][{asset}] ✅ Config ASSET utilisée (prioritaire)")
            # 🔍 DEBUG: Log pour tracer le chargement de la config asset
            logger.critical(
                f"[TIMING_GATEKEEPER_CONFIG][{asset}] "
                f"asset_config present={asset_config is not None} | "
                f"overrides present={bool(overrides)} | "
                f"scalping present={bool(scalping_overrides)} | "
                f"timing_gatekeeper present={bool(timing_config)} | "
                f"allowed_hours_gmt={timing_config.get('allowed_hours_gmt', 'NOT_FOUND')}"
            )

    # PRIORITÉ 2: Config scalping globale (fallback si asset n'a pas de config)
    if not timing_config and scalping_config:
        entry_rules = scalping_config.get("entry_rules", {})
        scalping_rules = entry_rules.get("scalping", {})
        timing_config = scalping_rules.get("timing_gatekeeper", {})
        logger.debug(f"[TIMING_CONFIG_SOURCE][{asset}] Config SCALPING GLOBALE utilisée (fallback)")

    enabled = timing_config.get("enabled", True)
    if not enabled:
        return {
            "verdict": "PASS",
            "veto_reason": None,
            "quality_metrics": {},
            "timing_analysis_ms": 0.0,
            "note": "Gatekeeper désactivé"
        }

    # Seuils
    min_tick_rate = timing_config.get("min_tick_rate", 1.0)  # ticks/sec (26 DEC: 5.0 → 1.0)
    min_coverage_s = timing_config.get("min_coverage_s", 40.0)  # secondes
    max_tick_rate = timing_config.get("max_tick_rate", 200.0)  # détection problème feed

    # 🔍 LOG (26 DEC 2025): Afficher les seuils chargés
    logger.critical(
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

    # 🔍 LOG: Config chargée
    logger.critical(
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
    # 3️⃣ CONDITIONS DE VETO (29 DEC 2025 - Analyse complète PUIS veto)
    # ========================================================================
    veto_reason = None

    # 🔍 LOG (26 DEC 2025): Afficher les métriques avant test VETO
    logger.critical(
        f"[TIMING_TEST_VETO][{asset}] "
        f"tick_count={tick_count} | coverage_s={coverage_s:.1f} | tick_rate={tick_rate:.1f} | "
        f"TEST: tick_rate({tick_rate:.1f}) < min_tick_rate({min_tick_rate}) = {tick_rate < min_tick_rate}"
    )

    # 🚨 A) HEURE NON AUTORISÉE (depuis config) - Priorité #1
    if not hour_is_allowed:
        veto_reason = f"🚫 Heure {hour_gmt:02d}h GMT NON autorisée (whitelist config: {allowed_hours})"
        logger.critical(f"[TIMING_HOUR_VETO][{asset}] {veto_reason}")

    # B) Coverage insuffisante
    elif coverage_s < min_coverage_s:
        veto_reason = f"Coverage insuffisante ({coverage_s:.1f}s < {min_coverage_s}s)"

    # C) Tick rate trop bas (toute session)
    elif tick_rate < min_tick_rate:
        veto_reason = f"Tick rate trop faible ({tick_rate:.1f} < {min_tick_rate} ticks/sec)"
        logger.critical(f"[TIMING_VETO_TRIGGERED][{asset}] VETO déclenché: {veto_reason}")

    # D) Session asiatique précoce avec faible liquidité
    elif session == "ASIAN_EARLY" and tick_rate < 8.0:
        veto_reason = f"Session asiatique précoce + tick rate insuffisant ({tick_rate:.1f} < 8.0)"

    # E) Tick rate suspicieusement élevé (problème feed)
    elif tick_rate > max_tick_rate:
        veto_reason = f"Tick rate anormalement élevé ({tick_rate:.1f} > {max_tick_rate}) - possible problème feed"

    # F) Liquidity score global trop faible
    elif liquidity_score < 0.3:
        veto_reason = f"Score de liquidité trop faible ({liquidity_score:.2f} < 0.30)"

    # G) Session Off-Peak
    elif session_quality == "POOR":
        veto_reason = f"Session off-peak (GMT {hour_gmt:02d}h) - liquidité généralement insuffisante"

    # ========================================================================
    # 4️⃣ VERDICT FINAL
    # ========================================================================
    verdict = "VETO" if veto_reason else "PASS"

    analysis_ms = (time.perf_counter() - analysis_start) * 1000.0

    result = {
        "verdict": verdict,
        "veto_reason": veto_reason,
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

    # 🔍 LOG (26 DEC 2025): Verdict final CRITICAL
    logger.critical(
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
