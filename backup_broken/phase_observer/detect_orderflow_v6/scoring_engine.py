from __future__ import annotations
from typing import Dict, Any, Tuple, Optional
import numpy as np


def calculate_score_integrated(
    metrics: Dict[str, float],
    patterns: Dict[str, Any] | list,
    rescue_level: int,
    rescue_note: str,
    footprint_data: Optional[Dict[str, Any]] = None,
    scoring_weights: Optional[Dict[str, float]] = None,
    # NOUVEAU: paramètre pour adaptation régime
    current_regime: Optional[str] = None,
) -> Tuple[float, str, Dict[str, Any]]:
    """
    🎯 SYSTÈME DE SCORING OPTIMISÉ POUR SCALPING TRENDING/CONSOLIDATION
    
    Optimisations pour votre stratégie:
    1. Seuils adaptés au scalping M1
    2. Poids ajustés pour trending/consolidation
    3. Réduction pénalités pour données courtes
    4. Bonus patterns renforcés
    """

    # ========================================================================
    # CONFIGURATION ADAPTATIVE PAR RÉGIME
    # ========================================================================
    
    # Détection du régime depuis les métriques ou paramètre
    regime = current_regime
    if not regime:
        # Estimation basée sur les métriques
        vol_ratio = metrics.get("vol_ratio", 1.0)
        cvd_slope = metrics.get("cvd_slope", 0.0)
        if abs(cvd_slope) > 0.5 and vol_ratio > 1.2:
            regime = "trending"
        elif vol_ratio < 0.8:
            regime = "range"
        else:
            regime = "consolidation"
    
    # Poids par défaut ADAPTÉS au régime
    if regime in ["trending", "consolidation"]:
        # OPTIMISÉ POUR VOTRE STRATÉGIE
        default_weights = {
            "delta_momentum_max": 30.0,   # ↑ +20% importance delta
            "volume_confirm_max": 20.0,   # ↑ +33% importance volume
            "imbalance_strength_max": 10.0,  # ↓ -50% importance imbalance
            "absorption_max": 10.0,      # ↓ Réduit (moins critique en trending)
            "clustering_max": 10.0,
            "rejection_max": 5.0,
            "triggers_max": 25.0,        # ↑ +25% bonus patterns
        }
    else:
        # Pour range (non tradé) - scores naturellement bas
        default_weights = {
            "delta_momentum_max": 15.0,   # ↓ Moins important en range
            "volume_confirm_max": 10.0,
            "imbalance_strength_max": 5.0,
            "absorption_max": 10.0,
            "clustering_max": 10.0,
            "rejection_max": 5.0,
            "triggers_max": 20.0,
        }

    # Fusion avec poids fournis
    weights = default_weights.copy()
    if scoring_weights and isinstance(scoring_weights, dict):
        for key, value in scoring_weights.items():
            if key in weights and isinstance(value, (int, float)) and value > 0:
                weights[key] = float(value)

    # -------- Utils --------
    def _get_float(d: Dict[str, Any], k: str, default: float) -> float:
        try:
            v = d.get(k, default)
            f = float(v)
            return f if np.isfinite(f) else default
        except Exception:
            return default

    def _get_int(d: Dict[str, Any], k: str, default: int) -> int:
        try:
            return int(d.get(k, default))
        except Exception:
            return default

    # -------- Lecture métriques --------
    delta_of = _get_float(metrics, "delta_total", 0.0)
    total_vol = _get_float(metrics, "total_volume", 0.0)
    imb_mean = _get_float(metrics, "imbalance_mean", 0.5)
    cvd_slope = _get_float(metrics, "cvd_slope", 0.0)
    buy_ratio = _get_float(metrics, "buy_ratio", 0.5)
    rows = _get_int(metrics, "rows", 0)

    # -------- Footprint (si disponible) --------
    fp_available = footprint_data is not None and isinstance(footprint_data, dict)
    if fp_available:
        fp_buy_vol = _get_float(footprint_data, "buy_volume", 0.0)
        fp_sell_vol = _get_float(footprint_data, "sell_volume", 0.0)
        fp_delta = _get_float(footprint_data, "delta_total", 0.0)
        fp_absorption = bool(footprint_data.get("absorption_flag", False))
        fp_imb_buy = _get_int(footprint_data, "imbalance_buy", 0)
        fp_imb_sell = _get_int(footprint_data, "imbalance_sell", 0)
        fp_total_vol = fp_buy_vol + fp_sell_vol
    else:
        fp_buy_vol = fp_sell_vol = fp_delta = fp_total_vol = 0.0
        fp_absorption = False
        fp_imb_buy = fp_imb_sell = 0

    # ========================================================================
    # 1️⃣ ORDERFLOW SCORE - OPTIMISÉ POUR SCALPING
    # ========================================================================

    # --- Delta Momentum (30 pts max en trending) ---
    if fp_available and abs(fp_delta) > 0:
        delta_combined = (fp_delta * 0.7) + (delta_of * 0.3)
        vol_combined = max(fp_total_vol, total_vol)
    else:
        delta_combined = delta_of
        vol_combined = total_vol

    delta_ratio = abs(delta_combined) / max(vol_combined, 1.0) if vol_combined > 1e-6 else 0.0

    # ✅ NOUVEAUX SEUILS ADAPTÉS AU SCALPING
    delta_max = weights["delta_momentum_max"]
    if regime in ["trending", "consolidation"]:
        # SEUILS PLUS ACCESSIBLES pour scalping
        if delta_ratio >= 0.3:          # ↓ 0.3 au lieu de 0.5
            delta_momentum_pts = delta_max
        elif delta_ratio >= 0.2:        # 20-30% → 75-100%
            delta_momentum_pts = (delta_max * 0.75) + ((delta_ratio - 0.2) / 0.1) * (delta_max * 0.25)
        elif delta_ratio >= 0.1:        # 10-20% → 50-75%
            delta_momentum_pts = (delta_max * 0.5) + ((delta_ratio - 0.1) / 0.1) * (delta_max * 0.25)
        else:                           # 0-10% → 0-50%
            delta_momentum_pts = (delta_ratio / 0.1) * (delta_max * 0.5)
    else:
        # Pour range - seuils plus stricts (garder scores bas)
        if delta_ratio >= 0.4:
            delta_momentum_pts = delta_max
        elif delta_ratio >= 0.25:
            delta_momentum_pts = (delta_max * 0.6) + ((delta_ratio - 0.25) / 0.15) * (delta_max * 0.4)
        elif delta_ratio >= 0.1:
            delta_momentum_pts = (delta_max * 0.3) + ((delta_ratio - 0.1) / 0.15) * (delta_max * 0.3)
        else:
            delta_momentum_pts = (delta_ratio / 0.1) * (delta_max * 0.3)

    # --- Volume Confirmation (20 pts max en trending) ---
    avg_vol = total_vol / max(rows, 1) if rows > 0 else total_vol
    vol_ratio = total_vol / max(avg_vol, 1.0) if avg_vol > 0 else 1.0

    volume_max = weights["volume_confirm_max"]
    if regime in ["trending", "consolidation"]:
        # ✅ SEUILS VOLUME PLUS ACCESSIBLES
        if vol_ratio >= 1.2:          # ↓ 1.2 au lieu de 1.5
            volume_confirm_pts = volume_max
        elif vol_ratio >= 1.0:        # 1.0-1.2 → 75-100%
            volume_confirm_pts = (volume_max * 0.75) + ((vol_ratio - 1.0) / 0.2) * (volume_max * 0.25)
        elif vol_ratio >= 0.8:        # 0.8-1.0 → 50-75%
            volume_confirm_pts = (volume_max * 0.5) + ((vol_ratio - 0.8) / 0.2) * (volume_max * 0.25)
        else:                         # 0-0.8 → 0-50%
            volume_confirm_pts = (vol_ratio / 0.8) * (volume_max * 0.5)
    else:
        # Pour range
        if vol_ratio >= 1.5:
            volume_confirm_pts = volume_max
        elif vol_ratio >= 1.2:
            volume_confirm_pts = (volume_max * 0.667) + ((vol_ratio - 1.2) / 0.3) * (volume_max * 0.333)
        elif vol_ratio >= 1.0:
            volume_confirm_pts = (volume_max * 0.333) + ((vol_ratio - 1.0) / 0.2) * (volume_max * 0.333)
        else:
            volume_confirm_pts = vol_ratio * (volume_max * 0.333)

    # Bonus cohérence Footprint (augmenté)
    if fp_available and fp_total_vol > 100:
        volume_confirm_pts = min(volume_max, volume_confirm_pts + (volume_max * 0.3))  # ↑ 0.3 au lieu de 0.2

    # --- Imbalance Strength (10 pts max) ---
    imb_strength_of = abs(imb_mean - 0.5) / 0.5

    if fp_available:
        fp_total_imb = fp_imb_buy + fp_imb_sell
        if fp_total_imb > 0:
            fp_imb_ratio = abs(fp_imb_buy - fp_imb_sell) / fp_total_imb
            imb_strength = (imb_strength_of * 0.3) + (fp_imb_ratio * 0.7)  # ↑ Poids footprint
        else:
            imb_strength = imb_strength_of
    else:
        imb_strength = imb_strength_of

    imbalance_max = weights["imbalance_strength_max"]
    imbalance_strength_pts = imb_strength * imbalance_max

    # Total OrderFlow Score
    orderflow_score = delta_momentum_pts + volume_confirm_pts + imbalance_strength_pts
    orderflow_max_total = weights["delta_momentum_max"] + weights["volume_confirm_max"] + weights["imbalance_strength_max"]
    orderflow_score = min(orderflow_max_total, max(0.0, orderflow_score))

    # ========================================================================
    # 2️⃣ FOOTPRINT SCORE - ADAPTÉ POUR SCALPING
    # ========================================================================

    footprint_score = 0.0

    if fp_available:
        # ✅ BONUS AUGMENTÉS pour scalping
        if fp_absorption:
            absorption_pts = 15.0
        elif delta_ratio > 0.25:  # ↓ Seuil 0.25 au lieu de 0.4
            absorption_pts = 10.0
        else:
            absorption_pts = 5.0

        # Clustering - seuils réduits
        total_imb_levels = fp_imb_buy + fp_imb_sell
        if total_imb_levels >= 80:    # ↓ 80 au lieu de 100
            clustering_pts = 10.0
        elif total_imb_levels >= 40:  # ↓ 40 au lieu de 50
            clustering_pts = 5.0 + ((total_imb_levels - 40) / 40) * 5.0
        elif total_imb_levels >= 20:
            clustering_pts = 2.0 + ((total_imb_levels - 20) / 20) * 3.0
        else:
            clustering_pts = (total_imb_levels / 20) * 2.0

        # Price Rejection - plus sensible
        rejection_pts = min(5.0, delta_ratio * 12.0)  # ↑ Multiplicateur

        footprint_score = absorption_pts + clustering_pts + rejection_pts
        footprint_score = min(30.0, max(0.0, footprint_score))

    # ========================================================================
    # 3️⃣ TRIGGERS BONUS - RENFORCÉ
    # ========================================================================

    triggers_bonus = 0.0

    # Pattern Count
    pattern_count = 0
    if isinstance(patterns, dict):
        try:
            pattern_count = int(sum(1 for v in patterns.values() if bool(v)))
        except Exception:
            pattern_count = 0
    elif isinstance(patterns, list):
        try:
            uniq = {
                (ev.get("pattern") or "").strip()
                for ev in patterns
                if isinstance(ev, dict)
            }
            uniq.discard("")
            pattern_count = len(uniq)
        except Exception:
            pattern_count = len(patterns)

    # ✅ BONUS PATTERNS AUGMENTÉS
    # +6 pts par pattern, max +18 pts (au lieu de +4 max +12)
    triggers_bonus += min(18.0, pattern_count * 6.0)

    # Multi-trigger Confluence - bonus augmentés
    if pattern_count >= 3:
        triggers_bonus += 6.0  # ↑ 6 au lieu de 4
    elif pattern_count >= 2:
        triggers_bonus += 3.0  # ↑ 3 au lieu de 2

    # Alignement Timeframes
    if fp_available and abs(fp_delta) > 0 and abs(delta_of) > 0:
        same_direction = (fp_delta > 0 and delta_of > 0) or (fp_delta < 0 and delta_of < 0)
        if same_direction:
            triggers_bonus += 4.0  # ↑ 4 au lieu de 3

    triggers_bonus = min(weights["triggers_max"], max(0.0, triggers_bonus))

    # ========================================================================
    # 4️⃣ SCORE FINAL - PÉNALITÉS RÉDUITES
    # ========================================================================

    base_score = orderflow_score + footprint_score + triggers_bonus

    # ✅ PÉNALITÉS ADAPTÉES AU SCALPING
    penalty = 0.0
    
    # Rescue level - pénalités augmentées pour qualité données
    if rescue_level == 1:
        penalty = 5.0  # ↑ 5 au lieu de 2
    elif rescue_level == 2:
        penalty = 15.0  # ↑ 15 au lieu de 5
    elif rescue_level >= 3:
        penalty = 30.0  # ↑ 30 au lieu de 15

    # Volume faible - seuil réduit
    if total_vol < 50 and not fp_available:  # ↑ Seuil 50 au lieu de 1e-6
        penalty += 5.0  # ↓ 5 au lieu de 10

    # ✅ PÉNALITÉ ROWS RÉDUITE POUR SCALPING
    # En scalping M1, avoir 8-9 barres est acceptable
    if rows > 0 and rows < 7:  # ↑ Seuil 7 au lieu de 10
        row_pen = (7 - rows) * 0.5  # ↓ 0.5 au lieu de 1.5
        penalty += row_pen

    final_score = float(max(0.0, min(100.0, base_score - penalty)))

    # ✅ STATUT ADAPTÉ AU SCALPING
    if regime in ["trending", "consolidation"]:
        # En trending, plus tolérant sur le score
        status = "VALID" if final_score >= 60.0 else "SUSPECT"  # ↓ 60 au lieu de 70
    else:
        # En range, plus strict (garder VETO)
        status = "VALID" if final_score >= 75.0 else "SUSPECT"

    if rescue_level >= 2:
        status = "SUSPECT"

    # Dominance
    dominance = (
        "buyers" if delta_combined > 0 else ("sellers" if delta_combined < 0 else "neutral")
    )

    # Biais & Conviction
    bias = "SELL" if imb_mean <= 0.48 else ("BUY" if imb_mean >= 0.52 else "NEUTRAL")
    conviction = float(min(1.0, abs(imb_mean - 0.5) / 0.25))

    # ========================================================================
    # SUMMARY AMÉLIORÉ
    # ========================================================================

    summary: Dict[str, Any] = {
        "orderflow_score": round(orderflow_score, 2),
        "footprint_score": round(footprint_score, 2),
        "triggers_bonus": round(triggers_bonus, 2),
        "base_score": round(base_score, 2),
        "penalty": round(penalty, 2),
        "final_score": round(final_score, 2),
        
        # NOUVEAU: Scores par composant
        "delta_score_breakdown": {
            "delta_ratio": round(delta_ratio, 3),
            "delta_points": round(delta_momentum_pts, 2),
            "delta_max": round(delta_max, 2)
        },
        "volume_score_breakdown": {
            "vol_ratio": round(vol_ratio, 3),
            "volume_points": round(volume_confirm_pts, 2),
            "volume_max": round(volume_max, 2)
        },
        "imbalance_score_breakdown": {
            "imb_strength": round(imb_strength, 3),
            "imbalance_points": round(imbalance_strength_pts, 2),
            "imbalance_max": round(imbalance_max, 2)
        },
        
        # Métriques principales
        "delta_total": float(delta_combined),
        "volume_total": float(total_vol),
        "delta_ratio_value": round(delta_ratio, 3),
        "vol_ratio_value": round(vol_ratio, 3),
        "mean_imbalance": float(imb_mean),
        "cvd_slope": float(cvd_slope),
        "buy_ratio": float(buy_ratio),
        "pattern_count": int(pattern_count),
        
        # Régime détecté
        "detected_regime": regime,
        
        # Rescue
        "rescue_level": int(rescue_level),
        "rescue_kind": "none" if rescue_level == 0 else ("soft" if rescue_level == 1 else "hard"),
        
        # Direction
        "bias": bias,
        "conviction": round(conviction, 3),
        "dominance": dominance,
    }

    if rows:
        summary["rows"] = int(rows)

    if fp_available:
        summary["footprint_available"] = True
        summary["fp_buy_volume"] = float(fp_buy_vol)
        summary["fp_sell_volume"] = float(fp_sell_vol)
        summary["fp_absorption"] = bool(fp_absorption)
        summary["fp_imbalance_levels"] = int(fp_imb_buy + fp_imb_sell)

    return final_score, status, summary