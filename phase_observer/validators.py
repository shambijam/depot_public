# phase_observer/validators.py
from __future__ import annotations

from typing import List
import pandas as pd

from .types import Phase, PhaseSignal, PhaseMemory, PhaseSnapshot
from .memory import stability_filter


def calculate_confidence_score(self, df_row: pd.Series) -> float:
    """
    Calcule un score de confiance basé sur la convergence des signaux SMC détectés.
    Note : score purement INFORMATIF (non bloquant).
    """
    # 1) Lecture config tolérante
    try:
        config_weights = self.config_manager.get(
            "phase_detection_defaults.confidence_score_calculation", {}
        ) or {}
    except Exception:
        config_weights = {}

    base_confidence = float(config_weights.get("base_confidence", 0.1))

    signal_weights = config_weights.get("signal_weights") or {
        "fvg_detected": 0.15,
        "ob_detected": 0.20,
        "bos_mss_detected": 0.15,
        "liquidity_grab_detected": 0.20,
        "eqh_eql_detected": 0.10,
        "volume_anomaly_detected": 0.10,
        "trend_alignment": 0.10,
    }

    convergence_bonus = config_weights.get("convergence_bonus") or {
        "multiple_signals_bonus": 0.2,
        "min_signals_for_bonus": 2,
        "max_confidence_cap": 1.0,
    }

    quality_factors = config_weights.get("quality_factors") or {
        "volume_confirmation_bonus": 0.1,
        "trend_strength_bonus": 0.1,
        "spread_quality_bonus": 0.05,
    }

    # 2) Accumulateur
    confidence = float(base_confidence)
    detected_signals = []

    # 3) Poids par signal
    for signal_name, weight in signal_weights.items():
        w = float(weight)
        if signal_name == "trend_alignment":
            trend = str(df_row.get("trend", "neutral") or "neutral").lower()
            phase = str(df_row.get("phase", "") or "").lower()
            aligned = (
                (trend == "bullish" and ("bull" in phase or "up" in phase)) or
                (trend == "bearish" and ("bear" in phase or "down" in phase))
            )
            if aligned:
                confidence += w
                detected_signals.append("trend_alignment")
        else:
            if bool(df_row.get(signal_name, False)):
                confidence += w
                detected_signals.append(signal_name)

    # 4) Bonus de convergence
    try:
        if len(detected_signals) >= int(convergence_bonus.get("min_signals_for_bonus", 2)):
            confidence += float(convergence_bonus.get("multiple_signals_bonus", 0.2))
    except Exception:
        pass

    # 5) Facteurs de qualité
    try:
        if float(df_row.get("volume_momentum", 0) or 0.0) > 0.1:
            confidence += float(quality_factors.get("volume_confirmation_bonus", 0.1))
    except Exception:
        pass

    # 6) Cap et borne
    max_cap = float(convergence_bonus.get("max_confidence_cap", 1.0))
    confidence = max(0.0, min(max_cap, confidence))
    return float(confidence)



def calculate_optimized_confidence(self, row) -> float:
    """Score de confiance basé sur 4 signaux core, confluence et lecture Bollinger optionnelle (non bloquant)."""
    # --- 1) Lecture DYNAMIQUE de la config (plusieurs chemins compatibles) ---
    try:
        cfg = self.config_manager.get("confidence_score_calculation", None)
        cfg_path_used = "confidence_score_calculation"

        if not cfg:
            cfg = self.config_manager.get("phase_detection_defaults.confidence_score_calculation", None)
            if cfg:
                cfg_path_used = "phase_detection_defaults.confidence_score_calculation"

        if not cfg:
            cfg = self.config_manager.get("phase_detection_defaults.confidence_scoring", None)
            if cfg:
                cfg_path_used = "phase_detection_defaults.confidence_scoring"

        if not cfg:
            cfg = {}
    except Exception:
        cfg, cfg_path_used = {}, "fallback"

    # --- 2) Normalisation douce des clés ------------------------------------
    signal_weights = cfg.get("signal_weights")
    if signal_weights is None:
        weights = cfg.get("weights")  # ex: { "fvg": 0.2, "ob": 0.3, ... }
        if isinstance(weights, dict):
            signal_weights = {
                "fvg_detected": float(weights.get("fvg", weights.get("fvg_detected", 0.25))),
                "ob_detected": float(weights.get("ob", weights.get("ob_detected", 0.35))),
                "bos_mss_detected": float(weights.get("bos_mss", weights.get("bos_mss_detected", 0.25))),
                "regime_alignment": float(weights.get("regime", weights.get("regime_alignment", 0.15))),
            }
        else:
            signal_weights = {
                "fvg_detected": 0.25,
                "ob_detected": 0.35,
                "bos_mss_detected": 0.25,
                "regime_alignment": 0.15,
            }

    boll_weights = cfg.get("bollinger_weights") or {}
    boll_mean_revert_w   = float(boll_weights.get("mean_revert_score", 0.10))
    boll_breakout_w      = float(boll_weights.get("breakout_score", 0.10))
    boll_squeeze_bonus   = float(boll_weights.get("squeeze_bonus", 0.05))
    boll_expansion_bonus = float(boll_weights.get("expansion_bonus", 0.05))

    confluence_bonus    = cfg.get("confluence_bonus") or cfg.get("confluence", {}) or {}
    quality_multipliers = cfg.get("quality_factors") or cfg.get("quality_multipliers", {}) or {}

    base_confidence = float(cfg.get("base_confidence", cfg.get("base", 0.2)))
    max_confidence  = float(cfg.get("max_confidence_cap", cfg.get("cap", 0.95)))

    # --- 3) Score core -------------------------------------------------------
    score = float(base_confidence)

    if bool(row.get("fvg_detected", False)):
        score += float(signal_weights.get("fvg_detected", 0.25))
    if bool(row.get("ob_detected", False)):
        score += float(signal_weights.get("ob_detected", 0.35))
    if bool(row.get("bos_mss_detected", False)):
        score += float(signal_weights.get("bos_mss_detected", 0.25))

    regime_strength = float(row.get("regime_strength", 0.5) or 0.5)
    if regime_strength > 0.7:
        score += float(signal_weights.get("regime_alignment", 0.15))

    if bool(row.get("fvg_ob_confluence", False)):
        score += float(confluence_bonus.get("fvg_ob_confluence", 0.15))
    if bool(row.get("high_quality_ob", False)) and bool(row.get("bos_mss_detected", False)):
        score += float(confluence_bonus.get("ob_bos_confluence", 0.10))
    if bool(row.get("institutional_setup", False)):
        score += float(confluence_bonus.get("full_confluence_bonus", 0.20))

    # --- 4) Lecture Bollinger (optionnelle) ---------------------------------
    try:
        boll_revert = float(row.get("boll_mean_revert_score", 0.0) or 0.0)
        boll_break  = float(row.get("boll_breakout_score", 0.0) or 0.0)
        boll_sig    = (row.get("boll_signal") or "").strip().lower()
        is_squeeze  = bool(row.get("boll_is_squeeze", False))
        is_expansion = bool(row.get("boll_is_expansion", False))

        regime = str(row.get("regime", "unknown") or "unknown").lower()
        in_range_regime = ("range_" in regime) or ("low_volatility" in regime)

        if boll_revert > 0.0:
            local_w = boll_mean_revert_w * (1.15 if in_range_regime else 1.0)
            score += local_w * max(0.0, min(1.0, boll_revert))

        if boll_break > 0.0:
            in_high_vol = ("high_volatility" in regime)
            local_w = boll_breakout_w * (1.15 if (is_expansion or in_high_vol) else 1.0)
            score += local_w * max(0.0, min(1.0, boll_break))

        if is_squeeze and in_range_regime:
            score += boll_squeeze_bonus
        if is_expansion and ("high_volatility" in regime):
            score += boll_expansion_bonus

        if boll_sig in {"buy_breakout", "sell_breakout"} and (is_expansion or "high_volatility" in regime):
            score += min(0.05, boll_break * 0.05)
        if boll_sig in {"buy_revert", "sell_revert"} and in_range_regime and is_squeeze:
            score += min(0.05, boll_revert * 0.05)
    except Exception:
        pass

    # --- 5) Multiplicateurs qualité/liquidité --------------------------------
    if bool(row.get("is_liquid", True)):
        score *= float(quality_multipliers.get("tight_spread", 1.05))
    if regime_strength > 0.8:
        score *= float(quality_multipliers.get("regime_strength", 1.10))

    try:
        ob_details = row.get("ob_details")
        bos_details = row.get("bos_mss_details")
        if isinstance(ob_details, dict) and float(ob_details.get("volume_spike", 0) or 0.0) > 1.5:
            score *= float(quality_multipliers.get("high_volume_confirmation", 1.15))
        elif isinstance(bos_details, dict) and float(bos_details.get("volume_ratio", 0) or 0.0) > 1.5:
            score *= float(quality_multipliers.get("high_volume_confirmation", 1.15))
    except Exception:
        pass

    # --- 6) Clamp & debug ----------------------------------------------------
    score = max(0.0, min(1.0, score))

    if getattr(self, "debug_confidence_logging", False):
        try:
            self.logger.debug(
                f"[CONF(v2)] path='{cfg_path_used}' base={base_confidence} cap={max_confidence} "
                f"weights={signal_weights} -> score={score:.3f}"
            )
        except Exception:
            pass

    return min(max_confidence, score)


