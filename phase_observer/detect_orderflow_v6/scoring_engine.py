# phase_observer/detect_orderflow_v6/scoring_engine.py
from __future__ import annotations
from typing import Dict, Any, Tuple
# --- ajoute ceci avec les imports du fichier ---
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
      - Bonus: patterns (count)
      - Pénalités: rescue, volume très faible, échantillon court
      - Compat V5: summary complet (mean_imbalance, buy_ratio, cvd_final, pattern_count, bias, conviction, rescue_*)
    """
    # --- Lecture robuste des métriques (avec défauts sûrs) ---
    imb_mean   = float(metrics.get("imbalance_mean", 0.5))       # [0..1], 0.5 neutre
    agr        = float(metrics.get("aggress_ratio", 0.5))        # [0..1], 0.5 neutre
    cvd_slope  = float(metrics.get("cvd_slope", 0.0))
    total_vol  = float(metrics.get("total_volume", 0.0))
    delta_tot  = float(metrics.get("delta_total", 0.0))
    buy_ratio  = float(metrics.get("buy_ratio", 0.5))
    cvd_final  = float(metrics.get("cvd", 0.0))
    imb_center = float(metrics.get("imbalance", (imb_mean * 2.0 - 1.0)))  # [-1..1]

    # Optionnels, s'ils sont déjà calculés en amont (sinon ignorés)
    rows       = int(metrics.get("rows", 0))
    coverage_s = float(metrics.get("coverage_s", float("nan")))
    tick_rate  = float(metrics.get("tick_rate", float("nan")))

    # --- Normalisations (features 0..1) ---
    f_imb = min(1.0, abs(imb_mean - 0.5) / 0.5)     # 0 si neutre, 1 si 0/1
    f_agr = min(1.0, abs(agr - 0.5) / 0.5)
    f_cvd = min(1.0, abs(cvd_slope) / 10.0)         # 10 = pente de référence soft

    # --- Base score (poids stables) ---
    base = (0.45 * f_imb + 0.35 * f_agr + 0.20 * f_cvd) * 100.0

    # --- Bonus patterns (compat liste ou dict de flags) ---
    if isinstance(patterns, dict):
        pattern_count = int(sum(1 for v in patterns.values() if bool(v)))
    elif isinstance(patterns, list):
        pattern_count = int(len(patterns))
    else:
        pattern_count = 0
    bonus_patterns = min(12.0, 2.0 * pattern_count)  # +2 par pattern, cap +12
    base += bonus_patterns

    # --- Pénalités & ajustements ---
    penalty = 0.0
    # Rescue (strict)
    if rescue_level == 1:
        penalty += 10.0
    elif rescue_level >= 2:
        penalty += 25.0
    # Volume trop faible
    if total_vol < 1e-6:
        penalty += 25.0
    # Échantillon court (si rows dispo) avec adoucissement via coverage_s
    if rows > 0 and rows < 10:
        row_pen = (10 - rows) * 1.5  # max ~15
        if not np.isnan(coverage_s) and coverage_s >= 30.0:
            row_pen *= 0.5
        penalty += row_pen
    # Activité (tick_rate) → petit bonus si élevé
    if not np.isnan(tick_rate) and tick_rate >= 2.0:
        base += 5.0
    elif not np.isnan(coverage_s) and coverage_s >= 45.0:
        base += 3.0

    # --- Score borné ---
    score = float(max(0.0, min(100.0, base - penalty)))

    # --- Statut (garde-fous type V5) ---
    status = "VALID" if score >= 70.0 else "SUSPECT"
    if rescue_level >= 2:
        status = "SUSPECT"
        score = min(score, 69.0)

    # --- Dominance simple pour logs/UX ---
    dominance = "buyers" if delta_tot > 0 else ("sellers" if delta_tot < 0 else "neutral")

    # --- Biais & conviction (parité V5) ---
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
        "rescue_kind": "none" if rescue_level == 0 else ("soft" if rescue_level == 1 else "hard"),
        "bias": bias,
        "conviction": round(conviction, 3),
        # V6 enrichi
        "cvd_slope": float(cvd_slope),
        "imbalance": float(imb_center),
        "aggress_ratio": float(agr),
        "dominance": dominance,
    }
    # Ajouts optionnels si fournis par l'amont
    if rows:
        summary["rows"] = int(rows)
    if not np.isnan(coverage_s):
        summary["coverage_s"] = float(coverage_s)
    if not np.isnan(tick_rate):
        summary["tick_rate"] = float(tick_rate)

    return score, status, summary

