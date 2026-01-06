# phase_observer/detect_orderflow_v6/orderflow_v6.py
from __future__ import annotations
from typing import Dict, Any, Optional, Tuple
import pandas as pd
import numpy as np

from .logging_manager import safe_log
from .data_preparator import validate_and_prepare_data
from .volume_analyzer import calculate_volume_metrics
from .pattern_detector import detect_patterns
from .institutional_metrics import calculate_volume_profile
from .scoring_engine import calculate_score_integrated
from .result_builder import build_result
from .divergence_detector import detect_divergences


def _estimate_current_regime(
    df: pd.DataFrame, 
    metrics: Dict[str, float],
    vp_options: Optional[Dict[str, Any]] = None
) -> str:
    """
    🎯 ESTIMATION DU RÉGIME COURANT - Optimisé pour scalping
    
    Règles pour votre stratégie:
    - trending: Forte pente CVD + volume élevé
    - consolidation: Volume modéré + range prix serré
    - range: Volume faible + pas de direction claire
    """
    if df is None or len(df) < 5:
        return "unknown"
    
    # Récupération des métriques
    cvd_slope = metrics.get("cvd_slope", 0.0)
    total_vol = metrics.get("total_volume", 0.0)
    delta_total = metrics.get("delta_total", 0.0)
    vol_ratio = metrics.get("vol_ratio", 1.0)
    
    # Calculs supplémentaires si nécessaires
    if "vol_ratio" not in metrics and "rows" in metrics:
        rows = metrics.get("rows", 1)
        avg_vol = total_vol / max(rows, 1)
        vol_ratio = total_vol / max(avg_vol, 1.0) if avg_vol > 0 else 1.0
    
    # Calcul range prix (ATR-like simplifié)
    price_range_pct = 0.0
    if len(df) > 1:
        high_max = df['high'].max() if 'high' in df.columns else df['close'].max()
        low_min = df['low'].min() if 'low' in df.columns else df['close'].min()
        mean_price = df['close'].mean() if 'close' in df.columns else 0
        if mean_price > 0:
            price_range_pct = (high_max - low_min) / mean_price * 100
    
    # ✅ RÈGLES OPTIMISÉES POUR SCALPING LONDON/NY
    # 1. TRENDING: Fort mouvement directionnel
    if (abs(cvd_slope) > 0.3 and 
        abs(delta_total) > total_vol * 0.25 and 
        vol_ratio > 1.1):
        return "trending"
    
    # 2. RANGE: Pas de direction + volume bas
    elif (abs(cvd_slope) < 0.15 and 
          price_range_pct < 0.05 and  # Range serré < 0.05%
          vol_ratio < 0.9):
        return "range"
    
    # 3. CONSOLIDATION: Volume présent mais sans forte direction
    elif (abs(cvd_slope) < 0.25 and 
          price_range_pct < 0.08 and  # Range modéré
          vol_ratio >= 0.9):
        return "consolidation"
    
    # 4. BREAKOUT_POTENTIAL: Fort volume sans direction établie
    elif (vol_ratio > 1.2 and 
          abs(cvd_slope) < 0.2 and 
          price_range_pct < 0.06):
        return "breakout_potential"
    
    # Fallback
    return "consolidation"


def detect_orderflow_v6(
    df_m1: pd.DataFrame,
    *,
    imbalance_threshold: float = 0.20,
    cvd_smoothing: float = 0.0,
    price_bins: int = 20,
    vp_options: Optional[Dict[str, Any]] = None,
    logger=None,
    footprint_data: Optional[Dict[str, Any]] = None,
    # NOUVEAU: Paramètres scalping
    current_regime: Optional[str] = None,  # Si déjà déterminé ailleurs
    lookback_override: Optional[int] = None,  # Override manuel
) -> Dict[str, Any]:
    """
    🚀 ORDERFLOW V6 OPTIMISÉ POUR SCALPING TRENDING/CONSOLIDATION
    
    Optimisations majeures:
    1. Lookback configurable et adaptatif
    2. Divergences adaptées au scalping
    3. Détection de régime intégrée
    4. Paramètres optimisés pour London/NY sessions
    """
    # --- 0) Garde-fou entrée ---
    if df_m1 is None or len(df_m1) == 0:
        return {
            "score": 0.0,
            "status": "SUSPECT",
            "summary": {"rescue_level": 2, "rescue_note": "empty_df"},
            "patterns": {},
        }

    # --- 1) DÉTERMINATION LOOKBACK OPTIMAL ---
    # Priorité: override > vp_options > défaut adaptatif
    if lookback_override is not None and lookback_override > 0:
        lookback = lookback_override
    elif vp_options and "lookback_bars" in vp_options:
        lookback = int(vp_options["lookback_bars"])
    else:
        # ✅ LOOKBACK ADAPTATIF selon disponibilité données
        total_bars = len(df_m1)
        if total_bars >= 30:
            lookback = 15  # Assez de données pour analyse robuste
        elif total_bars >= 20:
            lookback = 12
        elif total_bars >= 15:
            lookback = 10
        elif total_bars >= 10:
            lookback = 8
        else:
            lookback = max(5, total_bars - 2)  # Minimum 5 barres
    
    safe_log(logger, "info", f"[OF V6] 📈 Lookback adaptatif: {lookback} barres")
    
    # Extraction des barres pour analyse
    df_bars = df_m1.iloc[-lookback:] if len(df_m1) >= lookback else df_m1
    
    # --- 2) Préparation / validation des données ---
    has_footprint = footprint_data is not None and isinstance(footprint_data, dict)
    
    if has_footprint:
        safe_log(logger, "info", f"[OF V6] 🎯 Analyse OrderFlow avec intégration Footprint M1")
    else:
        safe_log(logger, "info", f"[OF V6] 📊 Analyse OrderFlow standard (sans Footprint)")
    
    df, rescue_level, rescue_note = validate_and_prepare_data(df_bars)
    
    # === PATCH TZ-NORMALIZE ===
    try:
        if isinstance(df.index, pd.DatetimeIndex) and df.index.tz is not None:
            df.index = df.index.tz_convert("UTC").tz_localize(None)
        
        for col in ("time", "timestamp", "datetime", "Date"):
            if col in df.columns:
                s = pd.to_datetime(df[col], errors="coerce", utc=True)
                if s.notna().any():
                    df[col] = s.dt.tz_convert("UTC").dt.tz_localize(None)
    except Exception as e_tz:
        safe_log(logger, "warning", f"[OF V6][TZ] normalization skipped: {e_tz}")
    
    # Si tout a été filtré/invalidé
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
    
    # --- 3) Métriques volume (core) ---
    # Extraction cvd_slope_window depuis vp_options (06 JAN 2026 - Problème #4 fix)
    cvd_slope_window = None
    if isinstance(vp_options, dict) and "cvd_slope_window" in vp_options:
        cvd_slope_window = vp_options.get("cvd_slope_window")
        if cvd_slope_window is not None:
            cvd_slope_window = int(cvd_slope_window)

    df, metrics = calculate_volume_metrics(
        df,
        cvd_smoothing=cvd_smoothing,
        cvd_slope_window=cvd_slope_window
    )
    
    try:
        df.attrs["imbalance_global"] = metrics.get("imbalance", 0.0)
    except Exception:
        pass
    
    # --- 3.b) Runtime metrics ---
    try:
        metrics.setdefault("rows", int(len(df)))
        
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
                    metrics.setdefault("tick_rate", float(len(df) / coverage_s))
                    
                    # ✅ NOUVEAU: Calcul ratio volume pour régime
                    if "total_volume" in metrics and "rows" in metrics:
                        rows = metrics["rows"]
                        total_vol = metrics["total_volume"]
                        avg_vol = total_vol / max(rows, 1)
                        metrics.setdefault("vol_ratio", total_vol / max(avg_vol, 1.0))
    except Exception as e_cov:
        safe_log(logger, "debug", f"[OF V6] coverage computation skipped: {e_cov}")
    
    # --- 4) DÉTECTION RÉGIME (critique pour scoring adaptatif) ---
    if current_regime:
        regime = current_regime
        safe_log(logger, "info", f"[OF V6] 📊 Régime fourni: {regime}")
    else:
        regime = _estimate_current_regime(df, metrics, vp_options)
        safe_log(logger, "info", f"[OF V6] 📊 Régime détecté: {regime}")
    
    # --- 5) Patterns ---
    try:
        patterns = detect_patterns(df, imbalance_threshold=imbalance_threshold)
    except Exception as e_pat:
        safe_log(logger, "warning", f"[OF V6] pattern detection failed: {e_pat}")
        patterns = []
    
    # --- 5.b) DIVERGENCES ADAPTÉES AU SCALPING ---
    # ✅ PARAMÈTRES OPTIMISÉS pour scalping London/NY
    if regime in ["trending", "breakout_potential"]:
        # En trending: divergences plus courtes pour réactivité
        div_lookback = min(50, lookback * 3)  # Max 50 barres (50 min)
        div_pivot = 2
        div_confirm = 5
    elif regime == "consolidation":
        # En consolidation: divergences moyennes
        div_lookback = min(80, lookback * 4)  # Max 80 barres
        div_pivot = 2
        div_confirm = 6
    else:  # range ou unknown
        # En range: divergences très courtes (évite faux signaux)
        div_lookback = min(30, lookback * 2)
        div_pivot = 2
        div_confirm = 4
    
    safe_log(logger, "debug", 
             f"[OF V6] 🔍 Divergences: lookback={div_lookback}, pivot={div_pivot}, confirm={div_confirm}")
    
    try:
        divergences = detect_divergences(
            df,
            lookback=div_lookback,      # ✅ Adapté au régime
            pivot_window=div_pivot,     # ✅ Adapté
            confirm_window=div_confirm, # ✅ Adapté
            fallback_indicator="cvd",
        )
    except Exception as e_div:
        safe_log(logger, "warning", f"[OF V6] divergence detection failed: {e_div}")
        divergences = []
    
    # Fusion patterns + divergences
    if isinstance(patterns, list):
        patterns.extend(divergences)
    elif isinstance(patterns, dict):
        patterns = {"events": patterns, "divergences": divergences}
    else:
        patterns = {"events": [], "divergences": divergences}
    
    # --- 6) Volume Profile (avec cache optimisé) ---
    vp_kwargs: Dict[str, Any] = {"price_bins": price_bins}
    if isinstance(vp_options, dict):
        vp_kwargs.update({k: v for k, v in vp_options.items() if v is not None})
    
    # ✅ OPTIMISATION: Ajout régime dans options VP pour cache adaptatif
    vp_kwargs["current_regime"] = regime
    
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
    
    # --- 7) SCORING ADAPTATIF PAR RÉGIME ---
    # Extraction poids de scoring
    scoring_weights = None
    if isinstance(vp_options, dict) and "scoring_weights" in vp_options:
        scoring_weights = vp_options.get("scoring_weights")
    
    # ✅ SCORING AVEC RÉGIME (CRITIQUE)
    score, status, summary = calculate_score_integrated(
        metrics,
        patterns,
        int(rescue_level or 0),
        str(rescue_note or ""),
        footprint_data=footprint_data,
        scoring_weights=scoring_weights,
        current_regime=regime,  # ✅ NOUVEAU: Passe le régime au scoring
    )
    
    # --- 8) LOGS AMÉLIORÉS ---
    if score >= 70:
        safe_log(logger, "info", f"[OF V6] 🟢 Score: {score:.1f}% | Status: {status} | Régime: {regime}")
    elif score >= 50:
        safe_log(logger, "info", f"[OF V6] 🟡 Score: {score:.1f}% | Status: {status} | Régime: {regime}")
    else:
        safe_log(logger, "info", f"[OF V6] 🔴 Score: {score:.1f}% | Status: {status} | Régime: {regime}")
    
    # --- 9) Résultat final avec métadonnées enrichies ---
    res = build_result(score, status, summary, patterns, vp)
    
    # Ajout métadonnées supplémentaires
    res["metadata"] = {
        "lookback_used": lookback,
        "regime_detected": regime,
        "divergence_params": {
            "lookback": div_lookback,
            "pivot_window": div_pivot,
            "confirm_window": div_confirm
        },
        "analysis_timestamp": pd.Timestamp.now().isoformat()
    }
    
    return res


# ✅ FONCTION UTILITAIRE: Détection régime depuis scalping.py
def extract_regime_from_scalping_logs(log_line: str) -> Optional[str]:
    """
    Extrait le régime depuis les logs de scalping.py
    Ex: "R:RANG(0.9)" → "range"
    """
    import re
    
    patterns = {
        r"R:RANG\([^)]+\)": "range",
        r"R:TRAN\([^)]+\)": "trending", 
        r"R:CONS\([^)]+\)": "consolidation",
        r"Phase=range_[^,]+": "range",
        r"Phase=trending_[^,]+": "trending",
        r"Phase=consolidation_[^,]+": "consolidation",
    }
    
    for pattern, regime in patterns.items():
        if re.search(pattern, log_line):
            return regime
    
    return None