from __future__ import annotations
from typing import Dict, Any, Tuple, Optional
import numpy as np


def calculate_score(
    metrics: Dict[str, float],
    patterns: Dict[str, Any] | list,
    rescue_level: int,
    rescue_note: str,
) -> Tuple[float, str, Dict[str, Any]]:
    """
    Score institutionnel (0..100) + statut:
      - Signal: |imbalance_mean-0.5|, |aggressor_ratio-0.5|, |cvd_slope|
      - Bonus: patterns (comptage intelligent), activité (tick_rate), VWAP (si dispo)
      - Pénalités: rescue, volume très faible, échantillon court
      - Compat V5: summary complet (mean_imbalance, buy_ratio, cvd_final, pattern_count, bias, conviction, rescue_*)
    """

    # -------- utils robustes --------
    def _get_float(d: Dict[str, Any], k: str, default: float) -> float:
        try:
            v = d.get(k, default)
            f = float(v)
            if not np.isfinite(f):
                return default
            return f
        except Exception:
            return default

    def _get_int(d: Dict[str, Any], k: str, default: int) -> int:
        try:
            v = d.get(k, default)
            i = int(v)
            return i
        except Exception:
            return default

    # -------- lecture métriques (blindée) --------
    imb_mean = _get_float(metrics, "imbalance_mean", 0.5)  # [0..1], 0.5 neutre
    agr = _get_float(metrics, "aggress_ratio", 0.5)  # [0..1], 0.5 neutre
    cvd_slope = _get_float(metrics, "cvd_slope", 0.0)
    total_vol = _get_float(metrics, "total_volume", 0.0)
    delta_tot = _get_float(metrics, "delta_total", 0.0)
    buy_ratio = _get_float(metrics, "buy_ratio", 0.5)
    cvd_final = _get_float(metrics, "cvd", 0.0)
    imb_center = _get_float(metrics, "imbalance", (imb_mean * 2.0 - 1.0))  # [-1..1]

    rows = _get_int(metrics, "rows", 0)
    coverage_s = _get_float(metrics, "coverage_s", float("nan"))
    tick_rate = _get_float(metrics, "tick_rate", float("nan"))

    # (optionnel) métriques VWAP ajoutées en V6
    vwap_slope = _get_float(metrics, "vwap_slope", 0.0)
    price_minus_vwap = _get_float(metrics, "price_minus_vwap", 0.0)

    # -------- features normalisées --------
    # clamp neutre-centre → [0..1]
    f_imb = min(1.0, abs(imb_mean - 0.5) / 0.5)
    f_agr = min(1.0, abs(agr - 0.5) / 0.5)
    f_cvd = min(1.0, abs(cvd_slope) / 10.0)  # 10 = pente de ref "soft"
    # bonus (optionnel) vwap_slope, capé
    f_vwap = min(1.0, abs(vwap_slope) / 10.0) if np.isfinite(vwap_slope) else 0.0

    # ⚡ NOUVEAU: Delta absolu normalisé (critique pour détecter mouvements institutionnels)
    # Échelle adaptative basée sur le volume total
    # Ratio Delta/Volume indique la force du déséquilibre
    delta_ratio = abs(delta_tot) / max(total_vol, 1.0) if total_vol > 1e-6 else 0.0
    # Normaliser: ratio 0.3+ = mouvement très fort
    f_delta = min(1.0, delta_ratio / 0.3)

    # -------- score de base --------
    # ⚡ Poids équilibrés: Delta et imbalance sont égaux (35% chacun)
    # Car Delta capture la force cumulative, imbalance capture le déséquilibre instantané
    w_imb, w_agr, w_cvd, w_delta = 0.35, 0.25, 0.15, 0.25
    base_core = (w_imb * f_imb + w_agr * f_agr + w_cvd * f_cvd + w_delta * f_delta) * 100.0

    # VWAP en bonus doux (max +6 pts) pour rester rétro-compatible
    base = base_core + (6.0 * f_vwap)

    # -------- bonus patterns --------
    # - dict de flags → somme des True
    # - liste d'events → compte les types *uniques* (évite multi-compte sur même barre)
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
            uniq.discard("")  # retire éventuelles chaînes vides
            pattern_count = len(uniq)
        except Exception:
            pattern_count = len(patterns)

    bonus_patterns = min(12.0, 2.0 * pattern_count)  # +2 par pattern, cap +12
    base += bonus_patterns

    # ⚡ NOUVEAU: Bonus léger pour mouvements directionnels forts
    # Si delta_ratio > 0.45 (45%+ du volume dans une direction) = mouvement directionnel clair
    if delta_ratio > 0.45:
        # Bonus modéré: évite de "tricher" mais récompense les vrais mouvements
        directional_bonus = min(15.0, (delta_ratio - 0.45) * 40.0)  # max +15pts
        base += directional_bonus

    # -------- pénalités & ajustements --------
    penalty = 0.0
    # Rescue (ultra-allégé pour permettre trading 24/7)
    # Note: En heures creuses, rescue_level=2 est NORMAL et ne doit pas tuer le score
    if rescue_level == 1:
        penalty += 2.0  # ⚡ réduit de 5 → 2
    elif rescue_level == 2:
        penalty += 5.0  # ⚡ réduit de 10 → 5 (heures creuses acceptables)
    elif rescue_level >= 3:
        penalty += 15.0  # ⚡ rescue_level 3+ = vraiment problématique

    # Volume trop faible (allégé)
    if total_vol < 1e-6:
        penalty += 10.0  # réduit de 25 → 10

    # Échantillon court
    if rows > 0 and rows < 10:
        row_pen = (10 - rows) * 1.5  # max ~15
        if np.isfinite(coverage_s) and coverage_s >= 30.0:
            row_pen *= 0.5
        penalty += row_pen

    # Activité (tick_rate) → petit bonus si élevé
    if np.isfinite(tick_rate) and tick_rate >= 2.0:
        base += 5.0
    elif np.isfinite(coverage_s) and coverage_s >= 45.0:
        base += 3.0

    # -------- score borné --------
    score = float(max(0.0, min(100.0, base - penalty)))

    # -------- statut (garde-fous) --------
    status = "VALID" if score >= 70.0 else "SUSPECT"
    # ✅ Suppression du cap à 69% - on garde juste le statut SUSPECT
    # Cela permet au score de contribuer correctement à la fusion même avec rescue_level=2
    if rescue_level >= 2:
        status = "SUSPECT"
        # score = min(score, 69.0)  # ❌ SUPPRIMÉ - trop restrictif

    # -------- dominance simple --------
    dominance = (
        "buyers" if delta_tot > 0 else ("sellers" if delta_tot < 0 else "neutral")
    )

    # -------- biais & conviction (parité V5) --------
    bias = "SELL" if imb_mean <= 0.48 else ("BUY" if imb_mean >= 0.52 else "NEUTRAL")
    conviction = float(min(1.0, abs(imb_mean - 0.5) / 0.25))  # 0..1

    summary: Dict[str, Any] = {
        # Parité V5
        "delta_total": float(delta_tot),
        "volume_total": float(total_vol),
        "mean_imbalance": float(imb_mean),
        "cvd_final": float(cvd_final),
        "buy_ratio": float(buy_ratio),
        "pattern_count": int(pattern_count),
        "rescue": bool(rescue_level > 0),
        "rescue_note": str(rescue_note),
        "rescue_level": int(rescue_level),
        "rescue_kind": (
            "none" if rescue_level == 0 else ("soft" if rescue_level == 1 else "hard")
        ),
        "bias": bias,
        "conviction": round(conviction, 3),
        # V6 enrichi
        "cvd_slope": float(cvd_slope),
        "imbalance": float(imb_center),
        "aggress_ratio": float(agr),
        "dominance": dominance,
        "delta_ratio": round(delta_ratio, 3),  # ⚡ Ratio Delta/Volume (nouveau)
    }
    if rows:
        summary["rows"] = int(rows)
    if np.isfinite(coverage_s):
        summary["coverage_s"] = float(coverage_s)
    if np.isfinite(tick_rate):
        summary["tick_rate"] = float(tick_rate)
    # Ajouts optionnels si fournis par l'amont (info-debug)
    if np.isfinite(vwap_slope):
        summary["vwap_slope"] = float(vwap_slope)
    if np.isfinite(price_minus_vwap):
        summary["price_minus_vwap"] = float(price_minus_vwap)

    return score, status, summary


def calculate_score_integrated(
    metrics: Dict[str, float],
    patterns: Dict[str, Any] | list,
    rescue_level: int,
    rescue_note: str,
    footprint_data: Optional[Dict[str, Any]] = None,
) -> Tuple[float, str, Dict[str, Any]]:
    """
    🎯 SYSTÈME DE SCORING INTÉGRÉ (0-100 points)

    Architecture: OrderFlow (50%) + Footprint (30%) + Triggers (20%)

    1. ORDERFLOW SCORE (50 points max):
       - Delta Momentum: 25 pts
       - Volume Confirmation: 15 pts
       - Imbalance Strength: 10 pts

    2. FOOTPRINT SCORE (30 points max):
       - Absorption Levels: 15 pts
       - Order Clustering: 10 pts
       - Price Rejection: 5 pts

    3. TRIGGERS BONUS (20 points max):
       - Patterns détectés
       - Multi-trigger confluence
       - Alignement timeframes

    Args:
        metrics: Métriques OrderFlow (delta, volume, imbalance, cvd...)
        patterns: Patterns détectés (liste ou dict)
        rescue_level: Niveau de rescue (0=bon, 1=soft, 2+=hard)
        rescue_note: Note explicative rescue
        footprint_data: Données Footprint M1 (buy_volume, sell_volume, delta, poc, absorption...)

    Returns:
        (score, status, summary)
    """

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

    # -------- Lecture métriques OrderFlow --------
    delta_of = _get_float(metrics, "delta_total", 0.0)
    total_vol = _get_float(metrics, "total_volume", 0.0)
    imb_mean = _get_float(metrics, "imbalance_mean", 0.5)  # [0..1]
    cvd_slope = _get_float(metrics, "cvd_slope", 0.0)
    buy_ratio = _get_float(metrics, "buy_ratio", 0.5)
    rows = _get_int(metrics, "rows", 0)

    # -------- Lecture métriques Footprint (si disponible) --------
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
    # 1️⃣ ORDERFLOW SCORE (50 points max)
    # ========================================================================

    # --- 1.1 Delta Momentum (25 pts max) ---
    # Combine delta OrderFlow + delta Footprint pour momentum global
    if fp_available and abs(fp_delta) > 0:
        # Fusion intelligente: Footprint (dernière minute) + OrderFlow (tendance)
        # Pondération: 70% Footprint (mouvement récent) + 30% OrderFlow (contexte)
        delta_combined = (fp_delta * 0.7) + (delta_of * 0.3)
        vol_combined = max(fp_total_vol, total_vol)
    else:
        # Fallback: utiliser seulement OrderFlow
        delta_combined = delta_of
        vol_combined = total_vol

    delta_ratio = abs(delta_combined) / max(vol_combined, 1.0) if vol_combined > 1e-6 else 0.0

    # Scoring Delta Momentum:
    # - delta_ratio > 0.5 = mouvement très fort → 25 pts
    # - delta_ratio 0.3-0.5 = mouvement fort → 15-25 pts
    # - delta_ratio 0.15-0.3 = mouvement moyen → 8-15 pts
    # - delta_ratio < 0.15 = mouvement faible → 0-8 pts
    if delta_ratio >= 0.5:
        delta_momentum_pts = 25.0
    elif delta_ratio >= 0.3:
        delta_momentum_pts = 15.0 + ((delta_ratio - 0.3) / 0.2) * 10.0
    elif delta_ratio >= 0.15:
        delta_momentum_pts = 8.0 + ((delta_ratio - 0.15) / 0.15) * 7.0
    else:
        delta_momentum_pts = (delta_ratio / 0.15) * 8.0

    # --- 1.2 Volume Confirmation (15 pts max) ---
    # Volume OrderFlow vs moyenne (si disponible)
    avg_vol = total_vol / max(rows, 1) if rows > 0 else total_vol
    vol_ratio = total_vol / max(avg_vol, 1.0) if avg_vol > 0 else 1.0

    if vol_ratio >= 1.5:
        volume_confirm_pts = 15.0
    elif vol_ratio >= 1.2:
        volume_confirm_pts = 10.0 + ((vol_ratio - 1.2) / 0.3) * 5.0
    elif vol_ratio >= 1.0:
        volume_confirm_pts = 5.0 + ((vol_ratio - 1.0) / 0.2) * 5.0
    else:
        volume_confirm_pts = vol_ratio * 5.0

    # Bonus si Footprint volume élevé (cohérence)
    if fp_available and fp_total_vol > 150:
        volume_confirm_pts = min(15.0, volume_confirm_pts + 3.0)

    # --- 1.3 Imbalance Strength (10 pts max) ---
    # Déséquilibre buy/sell (OrderFlow + Footprint)
    imb_strength_of = abs(imb_mean - 0.5) / 0.5  # [0..1]

    if fp_available:
        # Imbalance Footprint
        fp_total_imb = fp_imb_buy + fp_imb_sell
        if fp_total_imb > 0:
            fp_imb_ratio = abs(fp_imb_buy - fp_imb_sell) / fp_total_imb
            imb_strength = (imb_strength_of + fp_imb_ratio) / 2.0  # Moyenne
        else:
            imb_strength = imb_strength_of
    else:
        imb_strength = imb_strength_of

    imbalance_strength_pts = imb_strength * 10.0

    # Total OrderFlow Score
    orderflow_score = delta_momentum_pts + volume_confirm_pts + imbalance_strength_pts
    orderflow_score = min(50.0, max(0.0, orderflow_score))

    # ========================================================================
    # 2️⃣ FOOTPRINT SCORE (30 points max)
    # ========================================================================

    footprint_score = 0.0

    if fp_available:
        # --- 2.1 Absorption Levels (15 pts max) ---
        if fp_absorption:
            # Absorption forte détectée
            absorption_pts = 15.0
        elif delta_ratio > 0.4:
            # Pas d'absorption marquée mais fort déséquilibre = absorption partielle
            absorption_pts = 8.0
        else:
            absorption_pts = 3.0

        # --- 2.2 Order Clustering (10 pts max) ---
        # Basé sur le nombre de niveaux avec imbalance forte
        total_imb_levels = fp_imb_buy + fp_imb_sell
        if total_imb_levels >= 100:
            clustering_pts = 10.0
        elif total_imb_levels >= 50:
            clustering_pts = 5.0 + ((total_imb_levels - 50) / 50) * 5.0
        elif total_imb_levels >= 20:
            clustering_pts = 2.0 + ((total_imb_levels - 20) / 30) * 3.0
        else:
            clustering_pts = (total_imb_levels / 20) * 2.0

        # --- 2.3 Price Rejection (5 pts max) ---
        # Si delta fort dans une direction = rejet de l'autre côté
        rejection_pts = min(5.0, delta_ratio * 10.0)

        footprint_score = absorption_pts + clustering_pts + rejection_pts
        footprint_score = min(30.0, max(0.0, footprint_score))

    # ========================================================================
    # 3️⃣ TRIGGERS BONUS (20 points max)
    # ========================================================================

    triggers_bonus = 0.0

    # --- 3.1 Pattern Count ---
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

    # Bonus patterns: +4 pts par pattern, max +12 pts
    triggers_bonus += min(12.0, pattern_count * 4.0)

    # --- 3.2 Multi-trigger Confluence ---
    if pattern_count >= 3:
        triggers_bonus += 4.0  # Confluence forte
    elif pattern_count >= 2:
        triggers_bonus += 2.0  # Confluence modérée

    # --- 3.3 Alignement Timeframes ---
    # Si delta OrderFlow et Footprint alignés = bonus
    if fp_available and abs(fp_delta) > 0 and abs(delta_of) > 0:
        same_direction = (fp_delta > 0 and delta_of > 0) or (fp_delta < 0 and delta_of < 0)
        if same_direction:
            triggers_bonus += 3.0

    triggers_bonus = min(20.0, max(0.0, triggers_bonus))

    # ========================================================================
    # 4️⃣ SCORE FINAL
    # ========================================================================

    base_score = orderflow_score + footprint_score + triggers_bonus

    # --- Pénalités rescue ---
    penalty = 0.0
    if rescue_level == 1:
        penalty = 2.0
    elif rescue_level == 2:
        penalty = 5.0
    elif rescue_level >= 3:
        penalty = 15.0

    # --- Pénalité volume trop faible ---
    if total_vol < 1e-6 and not fp_available:
        penalty += 10.0

    # --- Pénalité échantillon court ---
    if rows > 0 and rows < 10:
        row_pen = (10 - rows) * 1.5
        penalty += row_pen

    final_score = float(max(0.0, min(100.0, base_score - penalty)))

    # --- Statut ---
    status = "VALID" if final_score >= 70.0 else "SUSPECT"
    if rescue_level >= 2:
        status = "SUSPECT"

    # --- Dominance ---
    dominance = (
        "buyers" if delta_combined > 0 else ("sellers" if delta_combined < 0 else "neutral")
    )

    # --- Biais & Conviction ---
    bias = "SELL" if imb_mean <= 0.48 else ("BUY" if imb_mean >= 0.52 else "NEUTRAL")
    conviction = float(min(1.0, abs(imb_mean - 0.5) / 0.25))

    # ========================================================================
    # SUMMARY
    # ========================================================================

    summary: Dict[str, Any] = {
        # Scores détaillés
        "orderflow_score": round(orderflow_score, 2),
        "footprint_score": round(footprint_score, 2),
        "triggers_bonus": round(triggers_bonus, 2),
        "base_score": round(base_score, 2),
        "penalty": round(penalty, 2),

        # Détail OrderFlow
        "delta_momentum_pts": round(delta_momentum_pts, 2),
        "volume_confirm_pts": round(volume_confirm_pts, 2),
        "imbalance_strength_pts": round(imbalance_strength_pts, 2),

        # Détail Footprint (si disponible)
        "footprint_available": fp_available,

        # Métriques principales
        "delta_total": float(delta_combined),
        "delta_of": float(delta_of),
        "delta_fp": float(fp_delta) if fp_available else None,
        "volume_total": float(total_vol),
        "delta_ratio": round(delta_ratio, 3),
        "mean_imbalance": float(imb_mean),
        "imbalance_mean": float(imb_mean),  # Alias pour compatibilité
        "cvd_slope": float(cvd_slope),
        "buy_ratio": float(buy_ratio),
        "pattern_count": int(pattern_count),

        # Rescue
        "rescue": bool(rescue_level > 0),
        "rescue_note": str(rescue_note),
        "rescue_level": int(rescue_level),
        "rescue_kind": (
            "none" if rescue_level == 0 else ("soft" if rescue_level == 1 else "hard")
        ),

        # Direction & Conviction
        "bias": bias,
        "conviction": round(conviction, 3),
        "dominance": dominance,
    }

    if rows:
        summary["rows"] = int(rows)

    if fp_available:
        summary["fp_buy_volume"] = float(fp_buy_vol)
        summary["fp_sell_volume"] = float(fp_sell_vol)
        summary["fp_absorption"] = bool(fp_absorption)
        summary["fp_imbalance_levels"] = int(fp_imb_buy + fp_imb_sell)

    return final_score, status, summary
