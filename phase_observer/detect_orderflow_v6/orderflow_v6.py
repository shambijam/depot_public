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
from .divergence_detector import detect_divergences


def detect_orderflow_v6(
    df_m1: pd.DataFrame,
    *,
    imbalance_threshold: float = 0.20,
    cvd_smoothing: float = 0.0,
    price_bins: int = 20,
    vp_options: Optional[
        Dict[str, Any]
    ] = None,  # options Volume Profile avancées (facultatives)
    logger=None,
) -> Dict[str, Any]:
    """
    Interface publique V6 (compatible V5) → retourne:
      {
        "score": float(0..100),
        "status": "VALID" | "SUSPECT",
        "summary": { ...  },        # inclut alias V5: vpoc_price, va_low, va_high
        "patterns": dict | list     # flags ou événements
      }
    + `summary.volume_profile` : bloc complet du Volume Profile (VPOC, VA, HVN/LVN, ...).

    Paramètres clés:
      - imbalance_threshold: 0.20 ≈ 70/30 si usage centré dans detect_patterns
      - cvd_smoothing: alpha EMA ∈ (0,1] pour lisser le CVD (0 = off)
      - price_bins: granularité de base du VP si pas de bin_width
      - vp_options: dict d’options VP (ex: {"coverage":0.7, "body_gain":0.6, "max_bins":400, ...})
    """
    # --- 0) Garde-fou entrée ---
    if df_m1 is None or len(df_m1) == 0:
        return {
            "score": 0.0,
            "status": "SUSPECT",
            "summary": {"rescue_level": 2, "rescue_note": "empty_df"},
            "patterns": {},
        }

    # --- 1) Préparation / validation des données ---
    df, rescue_level, rescue_note = validate_and_prepare_data(df_m1)

    # === PATCH TZ-NORMALIZE (2025-11-03) — neutralise les tz pour éviter .astype sur tz-aware ===
    try:
        # Index → tz-naive
        if isinstance(df.index, pd.DatetimeIndex) and df.index.tz is not None:
            df.index = df.index.tz_convert("UTC").tz_localize(None)

        # Colonnes temporelles usuelles → tz-naive
        for col in ("time", "timestamp", "datetime", "Date"):
            if col in df.columns:
                s = pd.to_datetime(df[col], errors="coerce", utc=True)
                if s.notna().any():
                    # on repasse en tz-naive pour éviter les .astype('datetime64[ns]') qui cassent
                    df[col] = s.dt.tz_convert("UTC").dt.tz_localize(None)
    except Exception as e_tz:
        safe_log(logger, "warning", f"[OF V6][TZ] normalization skipped: {e_tz}")

    # Si tout a été filtré/invalidé, on reste cohérent
    if df is None or len(df) == 0:
        return {
            "score": 0.0,
            "status": "SUSPECT",
            "summary": {
                "rescue_level": max(1, int(rescue_level or 1)),
                "rescue_note": str(rescue_note or "empty_after_prepare"),
            },
            "patterns": {},
        }

    # --- 2) Métriques volume (core) ---
    df, metrics = calculate_volume_metrics(df, cvd_smoothing=cvd_smoothing)
    # garder l'imbalance globale sur df.attrs pour d’éventuels détecteurs en aval
    try:
        df.attrs["imbalance_global"] = metrics.get("imbalance", 0.0)
    except Exception:
        pass

    # --- 2.b) Runtime metrics (rows/coverage/tick_rate) → compléter si absents ---
    try:
        # rows
        metrics.setdefault("rows", int(len(df)))

        # coverage_s & tick_rate (si non fournis par calculate_volume_metrics)
        if (
            ("coverage_s" not in metrics or "tick_rate" not in metrics)
            and "time" in df.columns
            and len(df) >= 2
        ):
            t0 = pd.to_datetime(df["time"].iloc[0], utc=True, errors="coerce")
            t1 = pd.to_datetime(df["time"].iloc[-1], utc=True, errors="coerce")
            if pd.notna(t0) and pd.notna(t1):
                coverage_s = float((t1 - t0).total_seconds())
                if coverage_s > 0:
                    metrics.setdefault("coverage_s", coverage_s)
                    # tick_rate simple par lignes (si tick_volume non fiable)
                    metrics.setdefault("tick_rate", float(len(df) / coverage_s))
    except Exception as e_cov:
        safe_log(logger, "debug", f"[OF V6] coverage computation skipped: {e_cov}")

    # --- 3) Patterns ---
    try:
        patterns = detect_patterns(df, imbalance_threshold=imbalance_threshold)
    except Exception as e_pat:
        safe_log(logger, "warning", f"[OF V6] pattern detection failed: {e_pat}")
        patterns = []  # format neutre (result_builder et scoring gèrent dict|list)

    # --- 3.b) Divergences (prix vs indicateur: CVD/VWAP) ---
    try:
        divergences = detect_divergences(
            df,
            lookback=200,
            pivot_window=3,
            confirm_window=10,
            fallback_indicator="cvd",
        )
    except Exception as e_div:
        safe_log(logger, "warning", f"[OF V6] divergence detection failed: {e_div}")
        divergences = []

    # Fusionne proprement avec le format des patterns existants (liste V5 ou dict)
    if isinstance(patterns, list):
        patterns.extend(divergences)
    elif isinstance(patterns, dict):
        patterns = {"events": patterns, "divergences": divergences}
    else:
        patterns = {"events": [], "divergences": divergences}

    # --- 4) Volume Profile (avec options avancées fusionnées proprement) ---
    vp_kwargs: Dict[str, Any] = {"price_bins": price_bins}
    if isinstance(vp_options, dict):
        # vp_options > paramètres par défaut
        vp_kwargs.update({k: v for k, v in vp_options.items() if v is not None})

    try:
        vp = calculate_volume_profile(df, **vp_kwargs)
    except Exception as e_vp:
        safe_log(logger, "warning", f"[OF V6] volume profile failed: {e_vp}")
        vp = {
            "vpoc_price": None,
            "va_low": None,
            "va_high": None,
            "va_coverage": float(vp_kwargs.get("coverage", 0.70)),
            "hvn": [],
            "lvn": [],
            "modality": "unknown",
            "balance_metrics": {},
            "ib": {},
        }

    # --- 5) Score / statut ---
    score, status, summary = calculate_score(
        metrics, patterns, int(rescue_level or 0), str(rescue_note or "")
    )

    # --- 6) Résultat final (inclut alias V5 + bloc volume_profile) ---
    res = build_result(score, status, summary, patterns, vp)

    # --- 7) Log synthétique (sécurisé) ---
    try:
        safe_log(
            logger,
            "info",
            (
                f"[OF V6] score={res.get('score', 0.0):.1f} status={res.get('status','SUSPECT')} "
                f"Δ={float(res['summary'].get('delta_total', 0.0)):.1f} "
                f"imb={float(res['summary'].get('imbalance', 0.0)):.3f} "
                f"vpoc={res['summary'].get('vpoc_price')}"
            ),
        )
    except Exception:
        # ne bloque jamais le retour pour une erreur de formatage de log
        pass

    return res
