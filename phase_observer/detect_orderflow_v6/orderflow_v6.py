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
    footprint_df: Optional[pd.DataFrame] = None,  # ⚡ NOUVEAU: Footprint construit (ask/bid_volume)
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
    # ⚡ ANALYSE DOUBLE-NIVEAU (footprint dernière barre + tendance 30 barres)
    #
    # NIVEAU 1: Footprint dernière barre M1 (mouvement immédiat avec ask/bid_volume)
    # NIVEAU 2: 30 dernières barres M1 (tendance court terme)
    #
    # Si footprint disponible: analyser LES DEUX et fusionner
    # Sinon: fallback sur 30 barres seulement

    df_footprint = None
    df_bars = None
    has_footprint = footprint_df is not None and not footprint_df.empty

    if has_footprint:
        # NIVEAU 1: Footprint dernière barre (avec ask/bid_volume déjà calculés depuis ticks)
        # footprint_df contient UNE seule barre (la dernière) enrichie avec ask_volume/bid_volume
        safe_log(logger, "info", f"[OF V6] 📊 NIVEAU 1: Analyse FOOTPRINT dernière barre M1 - mouvement immédiat (ask/bid_volume depuis ticks)")
        df_footprint, rescue_level_fp, rescue_note_fp = validate_and_prepare_data(footprint_df)

        # NIVEAU 2: 30 dernières barres M1 OHLC (tendance court terme)
        # On utilise df_m1 (qui contient les 30+ barres OHLC sans footprint)
        df_bars_30 = df_m1.iloc[-30:] if len(df_m1) >= 30 else df_m1
        safe_log(logger, "info", f"[OF V6] 📈 NIVEAU 2: Analyse {len(df_bars_30)} barres OHLC - tendance court terme")
        df_bars, rescue_level_bars, rescue_note_bars = validate_and_prepare_data(df_bars_30)

        # Rescue level = max des deux (le plus restrictif)
        rescue_level = max(rescue_level_fp, rescue_level_bars)
        rescue_note = f"dual_analysis_footprint({rescue_note_fp})_bars({rescue_note_bars})"

        # Pour la suite, on va analyser le footprint dernière barre comme df principal
        df = df_footprint
    else:
        # Fallback: analyser uniquement les 30 dernières barres M1 OHLC (sans footprint)
        safe_log(logger, "info", f"[OF V6] Analyse 30 barres M1 OHLC (fallback, pas de footprint)")
        df_bars_30 = df_m1.iloc[-30:] if len(df_m1) >= 30 else df_m1
        df, rescue_level, rescue_note = validate_and_prepare_data(df_bars_30)
        df_bars = df

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
    # ⚡ DOUBLE-NIVEAU: Calculer métriques pour footprint dernière barre ET tendance 30 barres
    if has_footprint and df_bars is not None:
        # NIVEAU 1: Métriques footprint dernière barre (mouvement immédiat)
        df, metrics_footprint = calculate_volume_metrics(df, cvd_smoothing=cvd_smoothing)

        # NIVEAU 2: Métriques 30 barres footprint (tendance court terme)
        df_bars, metrics_bars = calculate_volume_metrics(df_bars, cvd_smoothing=cvd_smoothing)

        # Pour l'instant, on garde metrics_footprint comme metrics principal
        # (on fusionnera les scores plus tard)
        metrics = metrics_footprint
    else:
        # Mode simple: une seule analyse
        df, metrics = calculate_volume_metrics(df, cvd_smoothing=cvd_smoothing)
        metrics_bars = None
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
    # ⚡ FUSION DOUBLE-NIVEAU: Si on a analysé footprint + barres, fusionner les scores
    if has_footprint and metrics_bars is not None:
        # Score NIVEAU 1: Footprint dernière barre (mouvement immédiat)
        score_fp, status_fp, summary_fp = calculate_score(
            metrics, patterns, int(rescue_level or 0), str(rescue_note or "")
        )

        # Score NIVEAU 2: 30 barres footprint (tendance court terme)
        score_bars, status_bars, summary_bars = calculate_score(
            metrics_bars, patterns, int(rescue_level or 0), str(rescue_note or "")
        )

        # FUSION: Score pondéré + bonus cohérence
        # - Footprint dernière barre (70%): mouvement immédiat prioritaire
        # - 30 barres (30%): tendance court terme
        score_weighted = (score_fp * 0.70) + (score_bars * 0.30)

        # Bonus cohérence: Si les deux sont alignés (même direction)
        delta_fp = metrics.get("delta_total", 0.0)
        delta_bars = metrics_bars.get("delta_total", 0.0)
        same_direction = (delta_fp > 0 and delta_bars > 0) or (delta_fp < 0 and delta_bars < 0)

        if same_direction and abs(delta_fp) > 0 and abs(delta_bars) > 0:
            # Bonus +10% si mouvement immédiat ET tendance alignés
            coherence_bonus = 10.0
            score_weighted += coherence_bonus
            safe_log(logger, "info", f"[OF V6] ✅ Cohérence footprint/barres | bonus +{coherence_bonus}pts")

        score = float(min(100.0, max(0.0, score_weighted)))
        status = "VALID" if score >= 70.0 else "SUSPECT"

        # Summary enrichi avec les deux niveaux
        summary = summary_fp.copy()
        summary["dual_level"] = {
            "footprint_score": float(score_fp),
            "bars_score": float(score_bars),
            "coherence": same_direction,
            "delta_fp": float(delta_fp),
            "delta_bars": float(delta_bars),
        }

        safe_log(
            logger,
            "info",
            f"[OF V6] 📊 FUSION: Footprint={score_fp:.1f}% | Bars={score_bars:.1f}% | "
            f"Final={score:.1f}% | Cohérence={'✅' if same_direction else '❌'}"
        )
    else:
        # Mode simple: un seul score
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
