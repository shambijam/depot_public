# phase_observer/detect_orderflow_v6/orderflow_v6.py
from __future__ import annotations
from typing import Dict, Any, Optional
import pandas as pd

from .logging_manager import safe_log
from .data_preparator import validate_and_prepare_data
from .volume_analyzer import calculate_volume_metrics
from .pattern_detector import detect_patterns
from .institutional_metrics import calculate_volume_profile
from .scoring_engine import calculate_score
from .result_builder import build_result

def detect_orderflow_v6(
    df_m1: pd.DataFrame,
    *,
    imbalance_threshold: float = 0.20,
    cvd_smoothing: float = 0.0,
    price_bins: int = 20,
    vp_options: Optional[Dict[str, Any]] = None,  # options Volume Profile avancées (facultatives)
    logger=None,
) -> Dict[str, Any]:
    """
    Interface publique V6 (compatible V5): retourne {score, status, summary, patterns}
    + ajoute un volume_profile complet (et alias vpoc/va_* dans summary).
    """
    # --- 0) Garde-fou entrée ---
    if df_m1 is None or len(df_m1) == 0:
        return {
            "score": 0.0,
            "status": "SUSPECT",
            "summary": {"rescue_level": 2, "rescue_note": "empty_df"},
            "patterns": {},
        }

    # --- 1) Préparation / validation ---
    df, rescue_level, rescue_note = validate_and_prepare_data(df_m1)

    # --- 2) Métriques volume (core) ---
    df, metrics = calculate_volume_metrics(df, cvd_smoothing=cvd_smoothing)
    # on garde l'imbalance globale (utile à certains détecteurs)
    df.attrs["imbalance_global"] = metrics.get("imbalance", 0.0)

    # --- 2.b) Runtime metrics (rows/coverage/tick_rate) pour scoring institutionnel ---
    try:
        rows = int(len(df))
        metrics["rows"] = rows
        coverage_s = None
        if "time" in df.columns and rows >= 2:
            t0 = pd.to_datetime(df["time"].iloc[0], utc=True, errors="coerce")
            t1 = pd.to_datetime(df["time"].iloc[-1], utc=True, errors="coerce")
            if pd.notna(t0) and pd.notna(t1):
                coverage_s = float((t1 - t0).total_seconds())
        if coverage_s is not None and coverage_s > 0:
            metrics["coverage_s"] = coverage_s
            metrics["tick_rate"] = float(rows / coverage_s)
    except Exception as e_cov:
        safe_log(logger, "debug", f"[OF V6] coverage computation skipped: {e_cov}")

    # --- 3) Patterns ---
    try:
        patterns = detect_patterns(df, imbalance_threshold=imbalance_threshold)
    except Exception as e_pat:
        safe_log(logger, "warning", f"[OF V6] pattern detection failed: {e_pat}")
        patterns = {}

    # --- 4) Volume Profile (options avancées) ---
    vp_kwargs = {
        "price_bins": price_bins,
    }
    # options facultatives (ne casse rien si non fournies)
    if vp_options:
        # seule règle: vp_options > paramètre simple
        vp_kwargs.update({k: v for k, v in vp_options.items() if v is not None})
    try:
        vp = calculate_volume_profile(df, **vp_kwargs)
    except Exception as e_vp:
        safe_log(logger, "warning", f"[OF V6] volume profile failed: {e_vp}")
        vp = {"vpoc_price": None, "va_low": None, "va_high": None, "va_coverage": vp_kwargs.get("coverage", 0.70)}

    # --- 5) Score / statut ---
    score, status, summary = calculate_score(metrics, patterns, rescue_level, rescue_note)

    # --- 6) Résultat final ---
    res = build_result(score, status, summary, patterns, vp)

    safe_log(
        logger,
        "info",
        f"[OF V6] score={res['score']:.1f} status={res['status']} "
        f"Δ={res['summary'].get('delta_total', 0.0):.1f} "
        f"imb={res['summary'].get('imbalance', 0.0):.3f} "
        f"vpoc={res['summary'].get('vpoc_price')}"
    )
    return res

