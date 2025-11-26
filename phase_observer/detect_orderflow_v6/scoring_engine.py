from __future__ import annotations
from typing import Dict, Any, Tuple
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

    # -------- score de base --------
    w_imb, w_agr, w_cvd = 0.45, 0.35, 0.20
    base_core = (w_imb * f_imb + w_agr * f_agr + w_cvd * f_cvd) * 100.0

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

    # -------- pénalités & ajustements --------
    penalty = 0.0
    # Rescue (allégé pour permettre trading en heures creuses)
    if rescue_level == 1:
        penalty += 5.0  # réduit de 10 → 5
    elif rescue_level >= 2:
        penalty += 10.0  # réduit de 25 → 10

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
