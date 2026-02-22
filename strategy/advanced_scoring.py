"""
Advanced Scoring - Module de scoring centralise (16 FEV 2026)

RESPONSABILITE UNIQUE: Calcul du score final (bonus/malus).
PAS de decision BUY/SELL/HOLD — ca reste dans decision_pipeline.py.
PAS de vetos — ca reste dans decision_pipeline.py.

Absorbe les bonus/malus de l'ancien apply_pma_adjustments() +
integre fatigue, physics, IRD qui etaient deconnectes.

Fonction principale: calculate_final_score()
Fonction legacy:     calculate_score_integrated() (pour orderflow_v6.py)
"""

import logging
import numpy as np
from typing import Dict, Any, Optional, Tuple

logger = logging.getLogger(__name__)


# ============================================================================
# FONCTION PRINCIPALE — SCORING CENTRALISE (16 FEV 2026)
# ============================================================================

def calculate_final_score(
    # OrderFlow brut
    orderflow_score: float,
    orderflow_bias: str,
    signal_action: str,
    # Les 3 analyseurs
    fatigue_result: dict,
    physics_result: dict,
    inst_result: dict,
    inst_score: float,
    # MTF
    mtf_verdict,
    mtf_direction: str,
    # PMA (Price Memory)
    memory_clarity: float,
    memory_trend_strength: float,
    memory_trend_direction: str,
    micro_resistance_info: dict,
    price_memory_analyzer,
    rates_df_fresh,
    current_price: float,
    # Context
    current_regime: str,
    inst_veto_fatigue: bool,
    # Meta
    asset: str,
    delta_direction: str = "neutral",
    delta_momentum_score: float = 0.0,
    logger_ref=None,
) -> dict:
    """
    Calcule le score final en centralisant TOUS les bonus/malus.

    NE prend PAS de decision BUY/SELL/HOLD (decision_pipeline s'en charge).
    NE gere PAS les vetos (decision_pipeline s'en charge).

    Returns:
        {
            "score_brut": float,
            "score_final": float,
            "bonus_total": float,
            "malus_total": float,
            "adjustments": list[str],
            "ird_reversal_opposed": bool,
            "components": {
                "orderflow": float,
                "fatigue_state": str,
                "fatigue_impact": float,
                "physics_bias": str,
                "physics_impact": float,
                "ird_score": float,
                "ird_impact": float,
                "mtf_impact": float,
                "regime_impact": float,
                "micro_res_impact": float,
                "consensus": str,
            }
        }
    """
    _log = logger_ref or logger

    fatigue_result = fatigue_result or {}
    physics_result = physics_result or {}
    inst_result = inst_result or {}
    micro_resistance_info = micro_resistance_info or {}

    score_brut = orderflow_score
    bonus_total = 0.0
    malus_total = 0.0
    adjustments = []
    ird_reversal_opposed = False

    # Composants tracabilite
    fatigue_impact = 0.0
    physics_impact = 0.0
    ird_impact = 0.0
    mtf_impact = 0.0
    regime_impact = 0.0
    micro_res_impact = 0.0

    # ══════════════════════════════════════════════════════
    # 0. DETECTER ALIGNEMENT MTF (18 FEV 2026)
    # Quand MTF 3/3 confirme la direction du signal (M30 supprimé 20 FEV 2026),
    # les malus physics/fatigue sont reduits de 40%
    # car le macro valide malgre le bruit local.
    # ══════════════════════════════════════════════════════
    mtf_alignment_count = getattr(mtf_verdict, 'alignment_count', 0) if mtf_verdict else 0
    mtf_strong_aligned = (
        mtf_alignment_count >= 3 and
        ((mtf_direction == "BULLISH" and signal_action == "BUY") or
         (mtf_direction == "BEARISH" and signal_action == "SELL"))
    )
    # Facteur de reduction des malus quand MTF macro confirme
    mtf_malus_factor = 0.6 if mtf_strong_aligned else 1.0

    # ══════════════════════════════════════════════════════
    # 1. SCORE DE BASE = orderflow_score
    # ══════════════════════════════════════════════════════

    # ══════════════════════════════════════════════════════
    # 2. MALUS FATIGUE (NOUVEAU — auparavant deconnecte)
    # ══════════════════════════════════════════════════════
    fatigue_state = str(fatigue_result.get('market_state', 'UNKNOWN')).upper()

    if fatigue_state == 'EXHAUSTED':
        raw_malus = 25.0 * mtf_malus_factor
        fatigue_impact = -raw_malus
        malus_total += raw_malus
        adjustments.append(f"MALUS_FATIGUE_EXHAUSTED: -{raw_malus:.0f} (marche epuise{' [MTF reduit]' if mtf_strong_aligned else ''})")
    elif fatigue_state == 'FATIGUED':
        raw_malus = 15.0 * mtf_malus_factor
        fatigue_impact = -raw_malus
        malus_total += raw_malus
        adjustments.append(f"MALUS_FATIGUE: -{raw_malus:.0f} (marche fatigue{' [MTF reduit]' if mtf_strong_aligned else ''})")

    # Fatigue directionnelle supplementaire
    buyer_fatigue = fatigue_result.get('buyer_fatigue', {})
    seller_fatigue = fatigue_result.get('seller_fatigue', {})
    if buyer_fatigue.get('fatigue_level') == 'HIGH' and signal_action == 'BUY':
        raw_malus = 10.0 * mtf_malus_factor
        fatigue_impact -= raw_malus
        malus_total += raw_malus
        adjustments.append(f"MALUS_BUYER_FATIGUE: -{raw_malus:.0f} (acheteurs epuises + signal BUY)")
    if seller_fatigue.get('fatigue_level') == 'HIGH' and signal_action == 'SELL':
        raw_malus = 10.0 * mtf_malus_factor
        fatigue_impact -= raw_malus
        malus_total += raw_malus
        adjustments.append(f"MALUS_SELLER_FATIGUE: -{raw_malus:.0f} (vendeurs epuises + signal SELL)")

    # Momentum fatigue (ATR / volume / body size décroissants)
    momentum_fatigue = fatigue_result.get('momentum_fatigue', {})
    if momentum_fatigue.get('fatigue_level') == 'HIGH':
        raw_malus = 15.0 * mtf_malus_factor
        fatigue_impact -= raw_malus
        malus_total += raw_malus
        adjustments.append(f"MALUS_MOMENTUM_FATIGUE: -{raw_malus:.0f} (momentum epuise: ATR/volume/body decroissants{' [MTF reduit]' if mtf_strong_aligned else ''})")

    # Absorption: prix stagne malgré volume dans le sens du signal
    buyer_absorption = any("Absorption forte" in r for r in buyer_fatigue.get('reasons', []))
    seller_absorption = any("Absorption forte" in r for r in seller_fatigue.get('reasons', []))
    if buyer_absorption and signal_action == 'BUY':
        raw_malus = 20.0 * mtf_malus_factor
        fatigue_impact -= raw_malus
        malus_total += raw_malus
        adjustments.append(f"MALUS_ABSORPTION: -{raw_malus:.0f} (absorption: prix stagne malgre achats)")
    elif seller_absorption and signal_action == 'SELL':
        raw_malus = 20.0 * mtf_malus_factor
        fatigue_impact -= raw_malus
        malus_total += raw_malus
        adjustments.append(f"MALUS_ABSORPTION: -{raw_malus:.0f} (absorption: prix stagne malgre ventes)")

    # ══════════════════════════════════════════════════════
    # 3. MALUS PHYSICS (NOUVEAU — auparavant deconnecte)
    # ══════════════════════════════════════════════════════
    physics_bias = str(physics_result.get('physics_bias', 'NEUTRAL')).upper()
    energy = physics_result.get('energy_conservation', {})
    entropy = physics_result.get('market_entropy', {})
    inertia = physics_result.get('price_inertia', {})
    barriers = physics_result.get('energy_barriers', {})

    if energy.get('energy_deficit'):
        raw_malus = 20.0 * mtf_malus_factor
        physics_impact -= raw_malus
        malus_total += raw_malus
        adjustments.append(f"MALUS_PHYSICS_DEFICIT: -{raw_malus:.0f} (energie insuffisante{' [MTF reduit]' if mtf_strong_aligned else ''})")

    entropy_state = str(entropy.get('market_state', '')).upper()
    if entropy_state == 'CHAOTIC':
        raw_malus = 15.0 * mtf_malus_factor
        physics_impact -= raw_malus
        malus_total += raw_malus
        adjustments.append(f"MALUS_PHYSICS_CHAOS: -{raw_malus:.0f} (entropie chaotique{' [MTF reduit]' if mtf_strong_aligned else ''})")

    # Barriere proche dans la direction du trade
    if signal_action == 'BUY' and barriers.get('distance_to_resistance_pct', 999) < 0.001:
        raw_malus = 10.0 * mtf_malus_factor
        physics_impact -= raw_malus
        malus_total += raw_malus
        adjustments.append(f"MALUS_BARRIER_UP: -{raw_malus:.0f} (resistance < 1 pip)")
    elif signal_action == 'SELL' and barriers.get('distance_to_support_pct', 999) < 0.001:
        raw_malus = 10.0 * mtf_malus_factor
        physics_impact -= raw_malus
        malus_total += raw_malus
        adjustments.append(f"MALUS_BARRIER_DOWN: -{raw_malus:.0f} (support < 1 pip)")

    # Énergie requise pour franchir la barrière vs énergie disponible
    average_energy = energy.get('average_energy', 0.0)
    if average_energy > 0:
        energy_required = barriers.get(
            'energy_required_up' if signal_action == 'BUY' else 'energy_required_down', 0.0
        )
        if energy_required > average_energy * 2:
            raw_malus = 10.0 * mtf_malus_factor
            physics_impact -= raw_malus
            malus_total += raw_malus
            adjustments.append(f"MALUS_ENERGY_BARRIER: -{raw_malus:.0f} (energie requise x2 vs moyenne)")

    # ══════════════════════════════════════════════════════
    # 4. BONUS PHYSICS (NOUVEAU)
    # ══════════════════════════════════════════════════════
    if inertia.get('likely_to_continue'):
        inertia_dir = inertia.get('direction', 'NEUTRAL')
        if (inertia_dir == 'UP' and signal_action == 'BUY') or \
           (inertia_dir == 'DOWN' and signal_action == 'SELL'):
            physics_impact += 10.0
            bonus_total += 10.0
            adjustments.append(f"BONUS_PHYSICS_INERTIE: +10 (inertie {inertia_dir} alignee)")

    # ══════════════════════════════════════════════════════
    # 5. BONUS/FLAG IRD (augmente — avant +10)
    # ══════════════════════════════════════════════════════
    if inst_result and inst_score > 0:
        inst_trend = inst_result.get('new_trend', 'NEUTRAL')
        trend_aligned = (
            (inst_trend == "BULLISH" and signal_action == "BUY") or
            (inst_trend == "BEARISH" and signal_action == "SELL")
        )
        trend_opposed = (
            (inst_trend == "BULLISH" and signal_action == "SELL") or
            (inst_trend == "BEARISH" and signal_action == "BUY")
        )

        if trend_aligned:
            if inst_score >= 80:
                ird_impact = 25.0
                bonus_total += 25.0
                adjustments.append(f"BONUS_IRD_HIGH: +25 (Score={inst_score:.0f}, Trend={inst_trend} aligne)")
            elif inst_score >= 65:
                ird_impact = 15.0
                bonus_total += 15.0
                adjustments.append(f"BONUS_IRD: +15 (Score={inst_score:.0f}, Trend={inst_trend} aligne)")
            elif inst_score >= 40:
                ird_impact = 10.0
                bonus_total += 10.0
                adjustments.append(f"BONUS_IRD_MODERATE: +10 (Score={inst_score:.0f}, Trend={inst_trend} aligne)")
        elif trend_opposed:
            # IRD OPPOSE au signal — malus progressif selon le score
            if inst_result.get('reversal_detected', False):
                ird_reversal_opposed = True
                adjustments.append(
                    f"FLAG_IRD_REVERSAL: Reversal {inst_trend} OPPOSE au signal {signal_action} (Score={inst_score:.0f})"
                )
            # 18 FEV 2026: Malus IRD contradiction (le rapport a raison — IRD doit influencer)
            if inst_score >= 60:
                ird_impact = -20.0
                malus_total += 20.0
                adjustments.append(f"MALUS_IRD_OPPOSE_FORT: -20 (Score={inst_score:.0f}, Trend={inst_trend} CONTRE {signal_action})")
            elif inst_score >= 40:
                ird_impact = -10.0
                malus_total += 10.0
                adjustments.append(f"MALUS_IRD_OPPOSE: -10 (Score={inst_score:.0f}, Trend={inst_trend} CONTRE {signal_action})")

    # ── Bonus divergence CVD dédiée (layer ADVANCED_DIVERGENCE_*) ────────
    if inst_result:
        for _layer in inst_result.get("signals_breakdown", []):
            _layer_name = str(_layer.get("name", "")).upper()
            if "DIVERGENCE" in _layer_name:
                _layer_dir = str(_layer.get("direction", "NEUTRAL")).upper()
                _meta = _layer.get("metadata", {})
                _regular = _meta.get("regular", {})
                if _regular.get("detected", False) and _layer_dir in ("BULLISH", "BEARISH"):
                    _div_aligned = (
                        (_layer_dir == "BULLISH" and signal_action == "BUY") or
                        (_layer_dir == "BEARISH" and signal_action == "SELL")
                    )
                    if _div_aligned:
                        ird_impact += 12.0
                        bonus_total += 12.0
                        adjustments.append(
                            f"BONUS_CVD_DIVERGENCE: +12 (layer={_layer.get('name')}, dir={_layer_dir})"
                        )
                break  # une seule divergence comptée
    # ─────────────────────────────────────────────────────────────────────

    # ══════════════════════════════════════════════════════
    # 6. MALUS IRD FATIGUE (migre depuis PMA)
    # ══════════════════════════════════════════════════════
    if inst_veto_fatigue:
        malus_total += 50.0
        adjustments.append("MALUS_FATIGUE_CIRCUIT_BREAKER: -50 (marche epuise - IRD)")

    # ══════════════════════════════════════════════════════
    # 7. BONUS MTF (migre depuis PMA)
    # ══════════════════════════════════════════════════════
    # 18 FEV 2026: MTF EST ROI — bonus augmentes car le MTF DECIDE
    if mtf_verdict is not None:
        alignment_count = getattr(mtf_verdict, 'alignment_count', 0)
        mtf_aligned = (
            (mtf_direction == "BULLISH" and signal_action == "BUY") or
            (mtf_direction == "BEARISH" and signal_action == "SELL")
        )
        if mtf_aligned:
            if alignment_count == 3:
                # 22 FEV 2026: M30 supprimé — 3/3 est l'alignement parfait (ex 4/4)
                mtf_impact = 30.0
                bonus_total += 30.0
                alignment_str = getattr(mtf_verdict, 'alignment', '3/3')
                adjustments.append(f"BONUS_MTF_3/3: +30 (alignement parfait {alignment_str} {mtf_direction})")

    # ══════════════════════════════════════════════════════
    # 8. BONUS FRESH LEVEL (migre depuis PMA)
    # ══════════════════════════════════════════════════════
    if price_memory_analyzer is not None:
        try:
            fresh_levels = price_memory_analyzer.find_fresh_levels(
                historical_data=rates_df_fresh,
                current_price=current_price,
                lookback=20
            )
            if fresh_levels and len(fresh_levels) > 0:
                for level in fresh_levels:
                    distance_pips = abs(current_price - level) * 10000
                    if distance_pips < 2.0:
                        bonus_total += 10.0
                        adjustments.append(f"BONUS_FRESH_LEVEL: +10 (niveau frais a {distance_pips:.1f} pips)")
                        break
        except Exception:
            pass

    # ══════════════════════════════════════════════════════
    # 9. BONUS TREND CONSISTENCY (migre depuis PMA)
    # ══════════════════════════════════════════════════════
    if memory_clarity >= 0.7 and memory_trend_strength >= 0.6:
        trend_aligned = (
            (memory_trend_direction == "BULLISH" and signal_action == "BUY") or
            (memory_trend_direction == "BEARISH" and signal_action == "SELL")
        )
        if trend_aligned:
            bonus_total += 5.0
            adjustments.append(
                f"BONUS_TREND_CONSISTENCY: +5 (Clarity={memory_clarity:.2f}, Strength={memory_trend_strength:.2f})"
            )

    # ══════════════════════════════════════════════════════
    # 10. MALUS MICRO-RESISTANCE (migre depuis PMA)
    # ══════════════════════════════════════════════════════
    if (signal_action == "BUY" and
        micro_resistance_info.get('strength') in ['STRONG', 'MODERATE'] and
        micro_resistance_info.get('distance_pips', 999) < 1.0 and
        micro_resistance_info.get('bounce_probability', 0) >= 0.7):
        micro_res_impact = -30.0
        malus_total += 30.0
        adjustments.append(
            f"MALUS_MICRO_RES: -30 (Resistance {micro_resistance_info['strength']} a "
            f"{micro_resistance_info['distance_pips']:.2f} pips)"
        )

    # ══════════════════════════════════════════════════════
    # 11. MALUS REGIME (migre depuis PMA)
    # ══════════════════════════════════════════════════════
    current_regime_lower = str(current_regime).lower() if current_regime else "unknown"
    regime_blocked = any(rg in current_regime_lower for rg in ["range", "accumulation", "distribution"])

    if regime_blocked and signal_action in ["BUY", "SELL"]:
        # EXCEPTION: IRD reversal detecte + conviction MODERATE+ → pas de malus regime
        ird_exception = False
        if inst_result and inst_result.get('reversal_detected', False):
            conviction = inst_result.get('conviction_level', 'LOW')
            if conviction in ['MODERATE', 'HIGH']:
                ird_exception = True

        if not ird_exception:
            regime_impact = -20.0
            malus_total += 20.0
            adjustments.append(
                f"MALUS_REGIME: -20 (Regime '{current_regime}' incompatible avec {signal_action})"
            )
        else:
            adjustments.append(
                f"REGIME_EXCEPTION: IRD reversal {inst_result.get('conviction_level', 'N/A')} → malus regime annule"
            )

    # ══════════════════════════════════════════════════════
    # 11b. BONUS/MALUS DELTA DIRECTION — CVD slope (21 FEV 2026)
    # Equivalent au "cvd_slope accélérateur" des rapports doctrinaux.
    # Condition: delta fort (score >= 12) dans le sens du signal → +8
    #            delta fort opposé au signal → -5
    # ══════════════════════════════════════════════════════
    delta_dir_upper = str(delta_direction).upper()
    delta_strong = delta_momentum_score >= 12.0

    if delta_strong:
        if (delta_dir_upper == "BULLISH" and signal_action == "BUY") or \
           (delta_dir_upper == "BEARISH" and signal_action == "SELL"):
            bonus_total += 8.0
            adjustments.append(
                f"BONUS_DELTA_MOMENTUM: +8 (delta={delta_dir_upper} aligné, score={delta_momentum_score:.0f})"
            )
        elif (delta_dir_upper == "BULLISH" and signal_action == "SELL") or \
             (delta_dir_upper == "BEARISH" and signal_action == "BUY"):
            malus_total += 5.0
            adjustments.append(
                f"MALUS_DELTA_OPPOSE: -5 (delta={delta_dir_upper} CONTRE {signal_action}, score={delta_momentum_score:.0f})"
            )

    # ══════════════════════════════════════════════════════
    # 12. CONSENSUS (NOUVEAU)
    # ══════════════════════════════════════════════════════
    stop_count = 0
    go_count = 0

    if fatigue_state == 'EXHAUSTED':
        stop_count += 1
    elif fatigue_state == 'ENERGETIC':
        go_count += 1

    if energy.get('energy_deficit') or entropy_state == 'CHAOTIC':
        stop_count += 1
    elif not energy.get('energy_deficit') and entropy_state == 'ORDERED':
        go_count += 1

    if ird_reversal_opposed:
        stop_count += 1
    elif ird_impact > 0:
        go_count += 1

    consensus = "SPLIT"
    if stop_count >= 2:
        consensus = "BLOCKED"
        malus_total += 15.0
        adjustments.append(f"MALUS_CONSENSUS_BLOCKED: -15 ({stop_count} analyseurs disent STOP)")
    elif go_count >= 3:
        consensus = "ALIGNED"
        bonus_total += 15.0
        adjustments.append(f"BONUS_CONSENSUS_ALIGNED: +15 ({go_count} analyseurs alignes)")

    # ══════════════════════════════════════════════════════
    # 13. CALCUL SCORE FINAL
    # ══════════════════════════════════════════════════════
    score_final = score_brut + bonus_total - malus_total
    score_final = max(0.0, min(100.0, score_final))

    # LOG
    if adjustments:
        adj_emoji = "+" if bonus_total > malus_total else ("-" if malus_total > bonus_total else "=")
        adj_summary = " | ".join(adjustments)
        _log.info(
            f"[SCORING][{asset}] Score: {score_brut:.1f} -> {score_final:.1f} "
            f"(Bonus: +{bonus_total:.0f}, Malus: -{malus_total:.0f}) [{adj_emoji}]"
        )
        _log.info(f"[SCORING_DETAIL][{asset}] {adj_summary}")

    return {
        "score_brut": score_brut,
        "score_final": score_final,
        "bonus_total": bonus_total,
        "malus_total": malus_total,
        "adjustments": adjustments,
        "ird_reversal_opposed": ird_reversal_opposed,
        "components": {
            "orderflow": score_brut,
            "fatigue_state": fatigue_state,
            "fatigue_impact": fatigue_impact,
            "physics_bias": physics_bias,
            "physics_impact": physics_impact,
            "ird_score": inst_score,
            "ird_impact": ird_impact,
            "mtf_impact": mtf_impact,
            "regime_impact": regime_impact,
            "micro_res_impact": micro_res_impact,
            "consensus": consensus,
        }
    }


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
    Calcule le score OrderFlow pur (sans les bonus/malus des analyseurs).
    """
    metrics = metrics or {}
    footprint_data = footprint_data or {}

    # Calcul OrderFlow pur
    of_score = _calculate_orderflow_component(
        metrics,
        patterns if isinstance(patterns, dict) else {},
        footprint_data,
        current_regime,
        rescue_level,
    )

    # Status
    if rescue_level >= 2:
        status = "SUSPECT"
    elif of_score >= 65:
        status = "VALID"
    elif of_score >= 50:
        status = "MARGINAL"
    else:
        status = "SUSPECT"

    # Bias
    if of_score >= 60:
        bias = "BUY"
    elif of_score <= 40:
        bias = "SELL"
    else:
        bias = "HOLD"

    summary = {
        "orderflow_score": of_score,
        "final_score": of_score,
        "status": status,
        "components": {"orderflow": of_score},
        "detected_regime": current_regime,
        "rescue_level": rescue_level,
        "rescue_kind": "none" if rescue_level == 0 else ("soft" if rescue_level == 1 else "hard"),
        "bias": bias,
        # Métriques brutes exposées pour decision_pipeline (cvd_slope était toujours 0.0)
        "cvd_slope": float(metrics.get("cvd_slope", 0.0)),
        "delta": float(metrics.get("delta_total", 0.0)),
        "vol_ratio": float(metrics.get("vol_ratio", 1.0)),
    }

    return of_score, status, summary


# ============================================================================
# COMPOSANT ORDERFLOW (utilise par calculate_score_integrated)
# ============================================================================

def _calculate_orderflow_component(
    metrics: Dict[str, float],
    patterns: Dict[str, Any],
    footprint_data: Dict[str, Any],
    regime: Optional[str],
    rescue_level: int
) -> float:
    """Calcule le score OrderFlow (delta, volume, imbalance, footprint)."""
    if not metrics:
        return 50.0

    def _get_float(d, k, default=0.0):
        try:
            v = float(d.get(k, default))
            return v if np.isfinite(v) else default
        except Exception:
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

    score = delta_pts + volume_pts + imbalance_pts + footprint_pts + pattern_pts - penalty
    return max(0.0, min(100.0, score))


# ============================================================================
# FONCTION LEGACY — scoring candidat (utilisee par scalping.py _score_candidate)
# ============================================================================

def calculate_unified_score(
    candidate: Optional[Dict[str, Any]] = None,
    meta: Optional[Dict[str, Any]] = None,
    asset_signals: Optional[Dict[str, Any]] = None,
    weights: Optional[Dict[str, float]] = None,
    **_kwargs,
) -> Dict[str, Any]:
    """
    Scoring simplifie pour candidats (legacy — utilise par scalping.py).
    Retourne un normalized_score 0-1 base sur confidence + alignment.
    """
    candidate = candidate or {}
    meta = meta or {}
    asset_signals = asset_signals or {}

    # Technical score
    tech = candidate.get("technical_score") or candidate.get("confidence", 0.6)
    try:
        tech_score = float(tech)
    except Exception:
        tech_score = 0.6

    # Context alignment
    phase = str(asset_signals.get("phase", "") or "").lower()
    action = candidate.get("action", "")
    align = 0.5
    if action == "BUY" and any(k in phase for k in ("bull", "up", "accum", "trend")):
        align = 1.0
    elif action == "SELL" and any(k in phase for k in ("bear", "down", "distrib")):
        align = 1.0

    # Risk
    sp = float(meta.get("spread_pips", 0.0) or 0.0)
    risk_factor = 1.0 if sp <= 5 else (0.8 if sp <= 10 else 0.6)

    normalized = (0.5 * tech_score + 0.3 * align + 0.2 * risk_factor)
    normalized = max(0.0, min(1.0, normalized))

    return {
        "final_score": round(normalized * 100, 2),
        "normalized_score": round(normalized, 4),
    }
