"""
UnifiedScorer - Systeme de scoring unifie

Fusion de 3 systemes en 1 (24 Janvier 2026):
- calculate_score_integrated (OrderFlow V6)
- calculate_composite_score (multi-dimensionnel)
- _score_candidate (contexte + risque)

Architecture finale:
- OrderFlow (35%): Delta, Volume, Imbalance, Footprint
- Institutional (25%): 5 analyseurs (Memory, Fatigue, Physics, Tape, Pressure)
- Context (20%): Phase, Alignment, Confidence
- Technical (15%): Setup score, Patterns
- Risk (5%): Spread, Volatility
"""

import logging
import pandas as pd
import numpy as np
from typing import Dict, Any, Optional, Tuple

logger = logging.getLogger(__name__)


# ============================================================================
# FONCTION UNIFIEE DE SCORING (24 Jan 2026)
# ============================================================================

def calculate_unified_score(
    # OrderFlow params
    metrics: Optional[Dict[str, float]] = None,
    patterns: Optional[Dict[str, Any]] = None,
    footprint_data: Optional[Dict[str, Any]] = None,
    current_regime: Optional[str] = None,
    rescue_level: int = 0,
    # Data params
    ticks_df: Optional[pd.DataFrame] = None,
    candles_df: Optional[pd.DataFrame] = None,
    # Institutional params
    institutional_analysis: Optional[Dict[str, Any]] = None,
    # Candidate params
    candidate: Optional[Dict[str, Any]] = None,
    meta: Optional[Dict[str, Any]] = None,
    asset_signals: Optional[Dict[str, Any]] = None,
    # Weights (optionnel)
    weights: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    """
    Fonction de scoring unifiee - combine OrderFlow + Composite + Candidate.

    Args:
        metrics: Metriques OrderFlow (delta_total, total_volume, imbalance_mean, etc.)
        patterns: Patterns detectes
        footprint_data: Donnees footprint (buy_volume, sell_volume, etc.)
        current_regime: "trending", "consolidation", "range"
        rescue_level: Niveau de rescue (0=normal, 1=soft, 2+=hard)
        ticks_df: DataFrame des ticks
        candles_df: DataFrame M1
        institutional_analysis: Resultats des 5 analyseurs
        candidate: Candidat d'entree (action, technical_score, etc.)
        meta: Metadata (spread_pips, etc.)
        asset_signals: Signaux de l'asset (phase, confidence_score, etc.)
        weights: Poids custom

    Returns:
        {
            'final_score': float (0-100),
            'normalized_score': float (0-1),
            'status': str (VALID/SUSPECT),
            'decision': str (BUY/SELL/HOLD),
            'confidence': str (STRONG/GOOD/WEAK/NONE),
            'components': {orderflow, institutional, context, technical, risk},
            'details': {...}
        }
    """
    # Defaults
    metrics = metrics or {}
    patterns = patterns or {}
    footprint_data = footprint_data or {}
    candidate = candidate or {}
    meta = meta or {}
    asset_signals = asset_signals or {}

    # Poids par defaut
    default_weights = {
        'orderflow': 0.35,
        'institutional': 0.25,
        'context': 0.20,
        'technical': 0.15,
        'risk': 0.05
    }
    w = {**default_weights, **(weights or {})}

    # Normaliser les poids
    total_w = sum(w.values())
    if total_w > 0:
        w = {k: v/total_w for k, v in w.items()}

    # ========================================================================
    # 1. ORDERFLOW SCORE (0-100)
    # ========================================================================
    orderflow_score = _calculate_orderflow_component(
        metrics, patterns, footprint_data, current_regime, rescue_level
    )

    # ========================================================================
    # 2. INSTITUTIONAL SCORE (0-100)
    # ========================================================================
    institutional_score = _calculate_institutional_component(
        institutional_analysis, ticks_df, candles_df
    )

    # ========================================================================
    # 3. CONTEXT SCORE (0-100)
    # ========================================================================
    context_score = _calculate_context_component(
        candidate, asset_signals
    )

    # ========================================================================
    # 4. TECHNICAL SCORE (0-100)
    # ========================================================================
    technical_score = _calculate_technical_component(
        candidate, patterns
    )

    # ========================================================================
    # 5. RISK SCORE (0-100)
    # ========================================================================
    risk_score = _calculate_risk_component(meta)

    # ========================================================================
    # SCORE FINAL
    # ========================================================================
    components = {
        'orderflow': round(orderflow_score, 2),
        'institutional': round(institutional_score, 2),
        'context': round(context_score, 2),
        'technical': round(technical_score, 2),
        'risk': round(risk_score, 2)
    }

    final_score = (
        w['orderflow'] * orderflow_score +
        w['institutional'] * institutional_score +
        w['context'] * context_score +
        w['technical'] * technical_score +
        w['risk'] * risk_score
    )
    final_score = max(0.0, min(100.0, final_score))

    # Status
    if rescue_level >= 2:
        status = "SUSPECT"
    elif final_score >= 65:
        status = "VALID"
    elif final_score >= 50:
        status = "MARGINAL"
    else:
        status = "SUSPECT"

    # Decision et Confidence
    decision, confidence = _determine_decision(final_score, components, candidate)

    return {
        'final_score': round(final_score, 2),
        'normalized_score': round(final_score / 100.0, 4),
        'status': status,
        'decision': decision,
        'confidence': confidence,
        'components': components,
        'weights': w,
        'details': {
            'regime': current_regime or 'unknown',
            'rescue_level': rescue_level,
            'has_footprint': bool(footprint_data),
            'has_ticks': ticks_df is not None,
            'has_institutional': bool(institutional_analysis)
        }
    }


def _calculate_orderflow_component(
    metrics: Dict[str, float],
    patterns: Dict[str, Any],
    footprint_data: Dict[str, Any],
    regime: Optional[str],
    rescue_level: int
) -> float:
    """Calcule le score OrderFlow (delta, volume, imbalance, footprint)."""
    if not metrics:
        return 50.0  # Neutre si pas de donnees

    # Helpers
    def _get_float(d, k, default=0.0):
        try:
            v = float(d.get(k, default))
            return v if np.isfinite(v) else default
        except:
            return default

    delta_of = _get_float(metrics, "delta_total", 0.0)
    total_vol = _get_float(metrics, "total_volume", 0.0)
    imb_mean = _get_float(metrics, "imbalance_mean", 0.5)
    rows = int(metrics.get("rows", 0) or 0)

    # Footprint
    fp_available = bool(footprint_data)
    fp_delta = _get_float(footprint_data, "delta_total", 0.0)
    fp_buy_vol = _get_float(footprint_data, "buy_volume", 0.0)
    fp_sell_vol = _get_float(footprint_data, "sell_volume", 0.0)
    fp_total_vol = fp_buy_vol + fp_sell_vol

    # Delta combined
    if fp_available and abs(fp_delta) > 0:
        delta_combined = (fp_delta * 0.7) + (delta_of * 0.3)
        vol_combined = max(fp_total_vol, total_vol)
    else:
        delta_combined = delta_of
        vol_combined = total_vol

    delta_ratio = abs(delta_combined) / max(vol_combined, 1.0) if vol_combined > 1e-6 else 0.0

    # Delta score (0-30)
    if delta_ratio >= 0.3:
        delta_pts = 30.0
    elif delta_ratio >= 0.2:
        delta_pts = 22.5 + ((delta_ratio - 0.2) / 0.1) * 7.5
    elif delta_ratio >= 0.1:
        delta_pts = 15.0 + ((delta_ratio - 0.1) / 0.1) * 7.5
    else:
        delta_pts = (delta_ratio / 0.1) * 15.0

    # Volume score (0-20)
    avg_vol = total_vol / max(rows, 1) if rows > 0 else total_vol
    vol_ratio = total_vol / max(avg_vol, 1.0) if avg_vol > 0 else 1.0

    if vol_ratio >= 1.2:
        volume_pts = 20.0
    elif vol_ratio >= 1.0:
        volume_pts = 15.0 + ((vol_ratio - 1.0) / 0.2) * 5.0
    elif vol_ratio >= 0.8:
        volume_pts = 10.0 + ((vol_ratio - 0.8) / 0.2) * 5.0
    else:
        volume_pts = (vol_ratio / 0.8) * 10.0

    # Imbalance score (0-10)
    imb_strength = abs(imb_mean - 0.5) / 0.5
    imbalance_pts = imb_strength * 10.0

    # Footprint bonus (0-15)
    footprint_pts = 0.0
    if fp_available:
        fp_absorption = bool(footprint_data.get("absorption_flag", False))
        if fp_absorption:
            footprint_pts = 15.0
        elif delta_ratio > 0.25:
            footprint_pts = 10.0
        else:
            footprint_pts = 5.0

    # Pattern bonus (0-15)
    pattern_count = 0
    if isinstance(patterns, dict):
        pattern_count = sum(1 for v in patterns.values() if bool(v))
    elif isinstance(patterns, list):
        pattern_count = len(patterns)
    pattern_pts = min(15.0, pattern_count * 5.0)

    # Penalties
    penalty = 0.0
    if rescue_level == 1:
        penalty = 5.0
    elif rescue_level >= 2:
        penalty = 15.0
    if total_vol < 50 and not fp_available:
        penalty += 5.0

    # Total (max 90 avant penalty)
    score = delta_pts + volume_pts + imbalance_pts + footprint_pts + pattern_pts - penalty
    return max(0.0, min(100.0, score))


def _calculate_institutional_component(
    institutional_analysis: Optional[Dict[str, Any]],
    ticks_df: Optional[pd.DataFrame],
    candles_df: Optional[pd.DataFrame]
) -> float:
    """Calcule le score Institutional (5 analyseurs + microstructure)."""
    if not institutional_analysis and ticks_df is None:
        return 50.0  # Neutre

    scores = []

    # 1. Price Memory
    if institutional_analysis:
        price_memory = institutional_analysis.get('price_memory', {})
        memory_signals = price_memory.get('memory_signals', [])
        fresh_levels = price_memory.get('fresh_levels', [])
        if memory_signals or fresh_levels:
            fresh_ratio = len(fresh_levels) / max(1, len(memory_signals) + len(fresh_levels))
            scores.append(50.0 + (fresh_ratio - 0.5) * 50.0)

    # 2. Market Fatigue
    if institutional_analysis:
        fatigue = institutional_analysis.get('market_fatigue', {})
        fatigue_state = str(fatigue.get('market_state', '')).upper()
        if fatigue_state == 'EXHAUSTED':
            scores.append(30.0)
        elif fatigue_state == 'FATIGUED':
            scores.append(40.0)
        elif fatigue_state == 'NORMAL':
            scores.append(50.0)
        elif fatigue_state == 'ENERGETIC':
            scores.append(65.0)

    # 3. Market Physics
    if institutional_analysis:
        physics = institutional_analysis.get('market_physics', {})
        physics_bias = str(physics.get('physics_bias', '')).upper()
        if 'BULLISH' in physics_bias or 'BUY' in physics_bias:
            scores.append(75.0)
        elif 'BEARISH' in physics_bias or 'SELL' in physics_bias:
            scores.append(25.0)
        elif physics_bias:
            scores.append(50.0)

    # 4. Tape Speed
    if institutional_analysis:
        tape = institutional_analysis.get('tape_speed', {})
        speed_ratio = float(tape.get('speed_ratio', 1.0) or 1.0)
        if speed_ratio >= 2.0:
            scores.append(70.0)
        elif speed_ratio >= 1.5:
            scores.append(60.0)
        elif speed_ratio >= 0.8:
            scores.append(50.0)
        else:
            scores.append(35.0)

    # 5. Pressure
    if institutional_analysis:
        pressure = institutional_analysis.get('pressure_ratio', {})
        pressure_norm = float(pressure.get('normalized_pressure', 0.0) or 0.0)
        scores.append(50.0 + (pressure_norm * 50.0))

    # 6. Microstructure from ticks
    if ticks_df is not None and len(ticks_df) >= 10:
        try:
            if 'time' in ticks_df.columns:
                duration = (ticks_df['time'].max() - ticks_df['time'].min())
                if hasattr(duration, 'total_seconds'):
                    duration = duration.total_seconds()
                if duration > 0:
                    tape_speed = len(ticks_df) / duration
                    speed_score = min(100.0, (tape_speed / 5.0) * 100.0)
                    scores.append(speed_score)
        except:
            pass

    if scores:
        return sum(scores) / len(scores)
    return 50.0


def _calculate_context_component(
    candidate: Dict[str, Any],
    asset_signals: Dict[str, Any]
) -> float:
    """Calcule le score Context (phase, alignment, confidence)."""
    phase = str(asset_signals.get("phase", "") or "").lower()
    conf = float(asset_signals.get("confidence_score", 0.5) or 0.5)
    action = candidate.get("action", "")

    # Alignment
    align = 0.5
    if action == "BUY" and any(k in phase for k in ("bull", "up", "accum", "trend")):
        align = 1.0
    elif action == "SELL" and any(k in phase for k in ("bear", "down", "distrib")):
        align = 1.0
    elif action == "SELL" and "trend" in phase:
        align = 0.8

    # Score 0-100
    context_score = (0.5 * conf + 0.5 * align) * 100.0
    return max(0.0, min(100.0, context_score))


def _calculate_technical_component(
    candidate: Dict[str, Any],
    patterns: Dict[str, Any]
) -> float:
    """Calcule le score Technical (setup score, patterns)."""
    # Technical score du candidat
    tech = candidate.get("technical_score")
    if tech is None:
        tech = candidate.get("confidence", 0.6)
    try:
        tech_score = float(tech) * 100.0
    except:
        tech_score = 60.0

    # Pattern bonus
    pattern_count = 0
    if isinstance(patterns, dict):
        pattern_count = sum(1 for v in patterns.values() if bool(v))
    elif isinstance(patterns, list):
        pattern_count = len(patterns)

    pattern_bonus = min(20.0, pattern_count * 5.0)

    return max(0.0, min(100.0, tech_score + pattern_bonus))


def _calculate_risk_component(meta: Dict[str, Any]) -> float:
    """Calcule le score Risk (spread, volatility)."""
    sp = float(meta.get("spread_pips", 0.0) or 0.0)

    if sp <= 5:
        risk_score = 100.0
    elif sp <= 10:
        risk_score = 80.0
    elif sp <= 15:
        risk_score = 60.0
    else:
        risk_score = 30.0

    return risk_score


def _determine_decision(
    final_score: float,
    components: Dict[str, float],
    candidate: Dict[str, Any]
) -> Tuple[str, str]:
    """Determine decision (BUY/SELL/HOLD) et confidence."""
    action = candidate.get("action", "")

    # Confidence
    if final_score >= 75:
        confidence = "STRONG"
    elif final_score >= 65:
        confidence = "GOOD"
    elif final_score >= 55:
        confidence = "WEAK"
    else:
        confidence = "NONE"

    # Decision
    if confidence == "NONE":
        decision = "HOLD"
    elif action in ("BUY", "SELL"):
        decision = action
    else:
        # Infer from orderflow
        of_score = components.get('orderflow', 50)
        if of_score >= 60:
            decision = "BUY"
        elif of_score <= 40:
            decision = "SELL"
        else:
            decision = "HOLD"

    return decision, confidence


# ============================================================================
# FONCTION LEGACY (compatibilite avec orderflow_v6.py)
# ============================================================================

def calculate_score_integrated(
    metrics: Dict[str, float],
    patterns,
    rescue_level: int,
    rescue_note: str,
    footprint_data: Optional[Dict[str, Any]] = None,
    scoring_weights: Optional[Dict[str, float]] = None,
    current_regime: Optional[str] = None,
) -> Tuple[float, str, Dict[str, Any]]:
    """
    Legacy wrapper pour compatibilite avec orderflow_v6.py.
    Redirige vers calculate_unified_score.
    """
    result = calculate_unified_score(
        metrics=metrics,
        patterns=patterns if isinstance(patterns, dict) else {},
        footprint_data=footprint_data,
        current_regime=current_regime,
        rescue_level=rescue_level
    )

    # Format legacy
    summary = {
        "orderflow_score": result['components']['orderflow'],
        "final_score": result['final_score'],
        "status": result['status'],
        "components": result['components'],
        "detected_regime": current_regime,
        "rescue_level": rescue_level,
        "rescue_kind": "none" if rescue_level == 0 else ("soft" if rescue_level == 1 else "hard"),
        "bias": result['decision'],
    }

    return result['final_score'], result['status'], summary


# ============================================================================
# CLASSE WRAPPER (compatibilite avec run_bot.py)
# ============================================================================

class SimpleAdvancedScorer:
    """Wrapper classe pour compatibilite avec run_bot.py."""

    def __init__(self, config=None, thresholds=None):
        self.weights = config or {}
        self.thresholds = thresholds or {}

    def calculate_composite_score(
        self,
        ticks_df=None,
        candles_df=None,
        orderflow_score=0.0,
        institutional_analysis=None
    ):
        """Redirige vers calculate_unified_score."""
        result = calculate_unified_score(
            ticks_df=ticks_df,
            candles_df=candles_df,
            institutional_analysis=institutional_analysis,
            weights=self.weights
        )
        # Ajouter orderflow_score si fourni
        if orderflow_score > 0:
            result['components']['orderflow'] = orderflow_score

        return result


# ============================================================================
# TEST
# ============================================================================

if __name__ == "__main__":
    print("Test calculate_unified_score:")
    result = calculate_unified_score(
        metrics={'delta_total': 100, 'total_volume': 1000, 'imbalance_mean': 0.6},
        candidate={'action': 'BUY', 'technical_score': 0.7},
        meta={'spread_pips': 3}
    )
    print(f"  Score: {result['final_score']}/100")
    print(f"  Status: {result['status']}")
    print(f"  Decision: {result['decision']} ({result['confidence']})")
    print(f"  Components: {result['components']}")
