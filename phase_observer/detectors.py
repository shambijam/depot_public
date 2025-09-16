# phase_observer/detectors.py
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
from .features import (
    _get_swing_points,
    _get_trend,
)


class Detectors:
    """
    Classe regroupant tous les détecteurs de phases de marché.
    Chaque méthode correspond à une logique de détection spécifique.
    """

    def __init__(self, logger=None, config_manager=None):
        self.logger = logger or logging.getLogger(__name__)
        self.config_manager = config_manager

    def detect_order_block_ml_enhanced(
        self, df: pd.DataFrame, df_htf: Optional[pd.DataFrame] = None
    ) -> List[Optional[Dict[str, Any]]]:
        """
        🎯 Order Blocks ML Enhanced - Scoring sophistiqué avec confluence (non bloquant)

        Features ML:
        - Impulse strength scoring
        - Volume confirmation weighting
        - Temporal context analysis
        - Multi-factor confluence scoring
        """
        self.logger.debug("Détection Order Blocks ML Enhanced...")

        if df is None or df.empty:
            return [None] * (0 if df is None else len(df))

        # Configuration ML
        ob_config = (
            self.config_manager.get(
                "phase_detection_defaults.order_block_ml_settings", {}
            )
            or {}
        )
        enable_ml = bool(ob_config.get("enable_ml_scoring", True))
        confluence_config = ob_config.get("confluence_requirements", {}) or {}
        impulse_weights = ob_config.get("impulse_strength_weights", {}) or {}

        # Paramètres de base
        impulse_threshold = float(
            self.config_manager.get(
                "phase_detection_defaults.impulse_threshold", 0.0005
            )
        )

        # Pré-calcul des features pour ML
        df = df.copy()
        df["candle_move"] = df["close"] - df["open"]
        df["candle_size"] = (df["high"] - df["low"]).replace(0, np.nan)
        df["body_ratio"] = (df["candle_move"].abs() / df["candle_size"]).fillna(0.0)

        vol_ma = (
            df["tick_volume"]
            .rolling(window=20, min_periods=1)
            .mean()
            .replace(0, np.nan)
        )
        df["volume_ma"] = vol_ma
        df["volume_ratio"] = (df["tick_volume"] / vol_ma).fillna(1.0)

        # Identification des OB potentiels
        bullish_ob_mask = (df["candle_move"] > impulse_threshold) & (
            df["candle_move"].shift(1) < 0
        )
        bearish_ob_mask = (df["candle_move"] < -impulse_threshold) & (
            df["candle_move"].shift(1) > 0
        )
        potential_ob_mask = bullish_ob_mask | bearish_ob_mask

        # Swing points pour confluence
        swing_highs, swing_lows = _get_swing_points(self, df)

        # Trend HTF si disponible
        htf_trend = None
        if df_htf is not None and not df_htf.empty:
            try:
                htf_trend = _get_trend(self, df_htf).iloc[-1]
            except Exception:
                htf_trend = None

        results: List[Optional[Dict[str, Any]]] = [None] * len(df)
        ob_positions = df.index[potential_ob_mask].tolist()

        for ob_timestamp in ob_positions:
            try:
                pos = int(df.index.get_loc(ob_timestamp))
                if pos <= 0:
                    continue

                ob_candle_pos = pos - 1
                if ob_candle_pos < 0 or pos >= len(df):
                    continue

                ob_candle = df.iloc[ob_candle_pos]
                impulse_candle = df.iloc[pos]
                ob_zone = (float(ob_candle["low"]), float(ob_candle["high"]))

                # === ML FEATURE EXTRACTION ===

                # 1. Impulse Strength Score
                price_movement_strength = float(
                    abs(impulse_candle["candle_move"]) / max(impulse_threshold, 1e-12)
                )
                volume_spike_strength = float(impulse_candle["volume_ratio"])
                body_ratio_strength = float(impulse_candle["body_ratio"])

                # Time compression (placeholder)
                time_compression = 1.0

                # Calcul score impulse pondéré
                impulse_score = (
                    price_movement_strength
                    * float(impulse_weights.get("price_movement", 0.4))
                    + volume_spike_strength
                    * float(impulse_weights.get("volume_spike", 0.3))
                    + time_compression
                    * float(impulse_weights.get("time_compression", 0.3))
                )

                # 2. Confluence Factors Scoring
                confluence_score = 0.0
                confluence_details: Dict[str, Any] = {}

                # FVG Confluence
                fvg_confluence = bool(pd.notna(impulse_candle.get("fvg_details")))
                if fvg_confluence:
                    confluence_score += 0.25
                confluence_details["fvg_confluence"] = fvg_confluence

                # Market Extreme Confluence (Swing points)
                extreme_confluence = bool(
                    (ob_candle.name in swing_highs.index)
                    or (ob_candle.name in swing_lows.index)
                )
                if extreme_confluence:
                    confluence_score += 0.30
                confluence_details["extreme_confluence"] = extreme_confluence

                # Volume Confirmation
                volume_confirmation = True
                if confluence_config.get("require_volume_confirmation", True):
                    volume_confirmation = (
                        volume_spike_strength > 1.2
                    )  # 20% au-dessus de la moyenne
                    if volume_confirmation:
                        confluence_score += 0.20
                confluence_details["volume_confirmation"] = volume_confirmation

                # Trend Alignment
                ob_is_bullish = bool(bullish_ob_mask.iloc[pos])
                trend_alignment = True
                if confluence_config.get("require_trend_alignment", True):
                    current_trend = (
                        df["trend"].iloc[pos] if "trend" in df.columns else "neutral"
                    )
                    if (ob_is_bullish and current_trend == "bullish") or (
                        (not ob_is_bullish) and current_trend == "bearish"
                    ):
                        trend_alignment = True
                        confluence_score += 0.15
                    else:
                        trend_alignment = False
                    # HTF alignment bonus
                    if htf_trend and (
                        (ob_is_bullish and htf_trend == "bullish")
                        or ((not ob_is_bullish) and htf_trend == "bearish")
                    ):
                        confluence_score += 0.10
                confluence_details["trend_alignment"] = trend_alignment

                # 3. Mitigation Analysis
                unmitigated = True
                future_candles = df.iloc[pos + 1 :]
                if not future_candles.empty:
                    mitigated = future_candles[
                        (future_candles["high"] >= ob_zone[0])
                        & (future_candles["low"] <= ob_zone[1])
                    ]
                    if not mitigated.empty:
                        unmitigated = False

                # 4. ML Score Final
                if enable_ml:
                    base_ml_score = min(1.0, (impulse_score + confluence_score) / 2.0)
                    if unmitigated:
                        base_ml_score *= 1.1
                    if volume_confirmation and trend_alignment:
                        base_ml_score *= 1.15
                    ml_score = min(0.95, base_ml_score)  # Cap à 95%
                else:
                    ml_score = float(confluence_score)

                # 5. Filtrage par seuil de confluence
                min_confluence = float(
                    confluence_config.get("min_confluence_score", 0.6)
                )

                if ml_score >= min_confluence:
                    results[pos] = {
                        "type": "bullish" if ob_is_bullish else "bearish",
                        "zone": [ob_zone[0], ob_zone[1]],
                        "ml_score": round(ml_score, 3),
                        "impulse_strength": round(impulse_score, 3),
                        "confluence_score": round(confluence_score, 3),
                        "confluence_details": confluence_details,
                        "unmitigated": bool(unmitigated),
                        "volume_spike": round(volume_spike_strength, 2),
                        "formation_quality": (
                            "high"
                            if ml_score > 0.8
                            else "medium" if ml_score > 0.6 else "low"
                        ),
                    }

            except Exception as e:
                self.logger.warning(
                    f"Erreur processing OB à l'index {ob_timestamp}: {e}",
                    exc_info=False,
                )
                continue

        # Performance logging
        valid_obs = [r for r in results if r is not None]
        if valid_obs:
            avg_ml_score = float(np.mean([ob["ml_score"] for ob in valid_obs]))
            high_quality = len(
                [ob for ob in valid_obs if ob["formation_quality"] == "high"]
            )
            self.logger.debug(
                f"OB ML Enhanced: {len(valid_obs)} OB détectés, score ML moyen: {avg_ml_score:.3f}, haute qualité: {high_quality}"
            )

        return results

    def detect_fvg_enhanced(self, df: pd.DataFrame) -> List[Optional[Dict[str, Any]]]:
        """
        🚀 FVG Enhanced - Version Trading Desk avec magnitude et tracking

        Améliorations:
        - Filtrage par magnitude minimale (en % du prix)
        - Tracking du remplissage en temps réel
        - Scoring de qualité du gap
        - Expiration basée sur l'âge
        """
        self.logger.debug("Détection FVG Enhanced avec magnitude et tracking...")

        if df is None or df.empty:
            return []

        # Récupération des paramètres enhanced
        fvg_config = (
            self.config_manager.get(
                "phase_detection_defaults.fvg_enhanced_settings", {}
            )
            or {}
        )
        min_gap_magnitude = (
            float(fvg_config.get("min_gap_magnitude_percent", 0.15)) / 100.0
        )
        gap_fill_threshold = float(fvg_config.get("gap_fill_threshold", 0.8))
        enable_tracking = bool(fvg_config.get("enable_gap_tracking", True))
        max_gap_age = int(fvg_config.get("max_gap_age_bars", 50))

        # Détection vectorielle de base (optimisée)
        low_p0 = df["low"].to_numpy()
        high_p2 = df["high"].shift(2).to_numpy()
        high_p0 = df["high"].to_numpy()
        low_p2 = df["low"].shift(2).to_numpy()

        valid_indices = ~(np.isnan(high_p2) | np.isnan(low_p2))

        bullish_fvg_condition = np.zeros(len(df), dtype=bool)
        bearish_fvg_condition = np.zeros(len(df), dtype=bool)

        bullish_fvg_condition[valid_indices] = (
            low_p0[valid_indices] > high_p2[valid_indices]
        )
        bearish_fvg_condition[valid_indices] = (
            high_p0[valid_indices] < low_p2[valid_indices]
        )

        results: List[Optional[Dict[str, Any]]] = []
        active_gaps: List[Dict[str, Any]] = (
            []
        )  # Tracking des gaps actifs pour remplissage

        for i in range(len(df)):
            fvg_info = None
            current_price = float(df["close"].iloc[i])

            # === DÉTECTION NOUVEAUX FVG ===
            if bullish_fvg_condition[i] and i >= 2:
                gap_bottom = float(df["high"].iloc[i - 2])
                gap_top = float(df["low"].iloc[i])
                gap_size = gap_top - gap_bottom

                magnitude_percent = gap_size / max(current_price, 1e-12)
                if magnitude_percent >= min_gap_magnitude:
                    quality_score = min(
                        1.0, magnitude_percent / max(min_gap_magnitude * 2.0, 1e-12)
                    )
                    fvg_info = {
                        "type": "bullish",
                        "top": gap_top,
                        "bottom": gap_bottom,
                        "magnitude": gap_size,
                        "magnitude_percent": magnitude_percent,
                        "quality_score": quality_score,
                        "formation_index": i,
                        "is_filled": False,
                        "fill_percentage": 0.0,
                    }
                    if enable_tracking:
                        active_gaps.append(fvg_info.copy())

            elif bearish_fvg_condition[i] and i >= 2:
                gap_top = float(df["low"].iloc[i - 2])
                gap_bottom = float(df["high"].iloc[i])
                gap_size = gap_top - gap_bottom

                magnitude_percent = gap_size / max(current_price, 1e-12)
                if magnitude_percent >= min_gap_magnitude:
                    quality_score = min(
                        1.0, magnitude_percent / max(min_gap_magnitude * 2.0, 1e-12)
                    )
                    fvg_info = {
                        "type": "bearish",
                        "top": gap_top,
                        "bottom": gap_bottom,
                        "magnitude": gap_size,
                        "magnitude_percent": magnitude_percent,
                        "quality_score": quality_score,
                        "formation_index": i,
                        "is_filled": False,
                        "fill_percentage": 0.0,
                    }
                    if enable_tracking:
                        active_gaps.append(fvg_info.copy())

            # === TRACKING REMPLISSAGE DES GAPS ACTIFS ===
            if enable_tracking and active_gaps:
                current_high = float(df["high"].iloc[i])
                current_low = float(df["low"].iloc[i])

                for gap in active_gaps[:]:  # copie pour suppression in-loop
                    age = i - int(gap["formation_index"])

                    # Expiration par âge
                    if age > max_gap_age:
                        active_gaps.remove(gap)
                        continue

                    if gap["type"] == "bullish":
                        if current_low <= gap["top"]:
                            penetration = gap["top"] - current_low
                            fill_percent = penetration / max(gap["magnitude"], 1e-12)
                            gap["fill_percentage"] = min(1.0, fill_percent)
                            if fill_percent >= gap_fill_threshold:
                                gap["is_filled"] = True
                                active_gaps.remove(gap)

                    else:  # bearish
                        if current_high >= gap["bottom"]:
                            penetration = current_high - gap["bottom"]
                            fill_percent = penetration / max(gap["magnitude"], 1e-12)
                            gap["fill_percentage"] = min(1.0, fill_percent)
                            if fill_percent >= gap_fill_threshold:
                                gap["is_filled"] = True
                                active_gaps.remove(gap)

            results.append(fvg_info)

        # Log de performance
        valid_gaps = [r for r in results if r is not None]
        if valid_gaps:
            avg_quality = float(np.mean([g["quality_score"] for g in valid_gaps]))
            self.logger.debug(
                f"FVG Enhanced: {len(valid_gaps)} gaps détectés, qualité moyenne: {avg_quality:.3f}"
            )

        return results

    def detect_bos_mss_enhanced(
        self, df: pd.DataFrame
    ) -> List[Optional[Dict[str, Any]]]:
        """
        🎯 BOS/MSS Enhanced - Avec confirmation volume et momentum (version vectorisée, sans .apply)

        Améliorations:
        - Vectorisation complète des validations (volume, momentum, distance de break) → perf M1+++
        - Confirmation volume obligatoire (configurable)
        - Validation momentum (configurable)
        - Distinction BOS vs MSS plus précise via la tendance précédente
        - Filtrage des faux breakouts par distance minimale relative
        - Respect de 'require_close_beyond' (clôture au-delà du niveau)
        """

        self.logger.debug(
            "Détection BOS/MSS Enhanced (vectorisée) avec confirmations..."
        )

        # === GUARDRAILS ===
        if df is None or df.empty:
            return []

        # --- Config ---
        bos_config = (
            self.config_manager.get(
                "phase_detection_defaults.bos_mss_enhanced_settings", {}
            )
            or {}
        )
        volume_config = bos_config.get("volume_confirmation", {}) or {}
        momentum_config = bos_config.get("momentum_confirmation", {}) or {}
        structure_config = bos_config.get("structure_validation", {}) or {}

        enable_volume_conf = bool(volume_config.get("enable", True))
        volume_multiplier = float(volume_config.get("volume_multiplier_threshold", 1.5))
        volume_lookback = int(volume_config.get("lookback_period", 20))

        enable_momentum_conf = bool(momentum_config.get("enable", True))
        min_momentum = float(momentum_config.get("min_momentum_threshold", 0.0003))

        min_break_distance = float(structure_config.get("min_break_distance", 0.0002))
        require_close_beyond = bool(structure_config.get("require_close_beyond", True))

        # === Préparation colonnes requises ===
        df = df.copy()

        # Tendance si absente
        if "trend" not in df.columns:
            df["trend"] = _get_trend(self, df)

        # Swing points adaptatifs (séries alignées)
        df["last_swing_high"] = df["high"].shift(1).ffill()
        df["last_swing_low"] = df["low"].shift(1).ffill()


        # Sanitisation prix/volume
        for col in ("close", "high", "low"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce").astype(float)
        if "tick_volume" not in df.columns:
            df["tick_volume"] = 0.0
        df["tick_volume"] = (
            pd.to_numeric(df["tick_volume"], errors="coerce").astype(float).fillna(0.0)
        )

        # Moyenne mobile volume + ratio (vectorisé)
        vol_ma = (
            df["tick_volume"]
            .rolling(window=max(1, volume_lookback), min_periods=1)
            .mean()
            .replace(0, np.nan)
        )
        df["volume_ma"] = vol_ma
        df["volume_ratio"] = (
            (df["tick_volume"] / vol_ma).replace([np.inf, -np.inf], np.nan).fillna(0.0)
        )

        # === Conditions de base: break au-delà du dernier swing (décalé) ===
        # on compare la close courante au swing de la barre précédente
        last_high_shift = df["last_swing_high"].shift(1)
        last_low_shift = df["last_swing_low"].shift(1)

        # close au-delà du niveau (strictement) ou non (si require_close_beyond=False, on tolère >= / <=)
        if require_close_beyond:
            bullish_break_basic = df["close"] > last_high_shift
            bearish_break_basic = df["close"] < last_low_shift
        else:
            bullish_break_basic = df["close"] >= last_high_shift
            bearish_break_basic = df["close"] <= last_low_shift

        # === Confirmations Volume (vectorisé) ===
        if enable_volume_conf:
            volume_confirmation = df["volume_ratio"] > volume_multiplier
        else:
            volume_confirmation = pd.Series(True, index=df.index)

        # === Confirmation Momentum (vectorisé) ===
        # momentum en valeur absolue de la variation relative (pct_change) pour robustesse
        if enable_momentum_conf:
            price_change_abs = df["close"].pct_change().abs()
            momentum_confirmation = price_change_abs > min_momentum
        else:
            price_change_abs = df["close"].pct_change().abs()  # utile pour logs
            momentum_confirmation = pd.Series(True, index=df.index)

        # === Filtrage distance minimale (vectorisé) ===
        # distance relative à partir du niveau cassé (sécurisé avec epsilon)
        eps = 1e-12
        # bullish: (close - last_high) / last_high >= min_break_distance
        # bearish: (last_low - close) / last_low >= min_break_distance
        denom_high = np.maximum(last_high_shift.astype(float), eps)
        denom_low = np.maximum(last_low_shift.astype(float), eps)

        bullish_dist_ok = ((df["close"] - last_high_shift) / denom_high) >= float(
            min_break_distance
        )
        bearish_dist_ok = ((last_low_shift - df["close"]) / denom_low) >= float(
            min_break_distance
        )

        # === Masques finaux break confirmés ===
        bullish_break_confirmed = (
            bullish_break_basic
            & volume_confirmation
            & momentum_confirmation
            & bullish_dist_ok
        )
        bearish_break_confirmed = (
            bearish_break_basic
            & volume_confirmation
            & momentum_confirmation
            & bearish_dist_ok
        )

        # === Classification BOS vs MSS (via tendance précédente) ===
        previous_trend = df["trend"].shift(1).astype(str).str.lower()
        bullish_bos = (previous_trend == "bullish") & bullish_break_confirmed
        bearish_bos = (previous_trend == "bearish") & bearish_break_confirmed
        bullish_mss = (previous_trend == "bearish") & bullish_break_confirmed
        bearish_mss = (previous_trend == "bullish") & bearish_break_confirmed

        # === Construction des résultats (liste alignée sur df) ===
        results: List[Optional[Dict[str, Any]]] = []
        vol_ratio_arr = df["volume_ratio"].to_numpy()
        price_change_arr = price_change_abs.to_numpy()

        # niveaux cassés pour logs
        level_broken_high = last_high_shift.to_numpy(dtype=float)
        level_broken_low = last_low_shift.to_numpy(dtype=float)

        # helper qualité
        def _quality_from_volratio(vr: float) -> str:
            try:
                return "high" if vr > (volume_multiplier * 1.5) else "medium"
            except Exception:
                return "medium"

        # vector -> liste d'infos
        bbos = bullish_bos.to_numpy(dtype=bool)
        bbss = bearish_bos.to_numpy(dtype=bool)
        bmss = bullish_mss.to_numpy(dtype=bool)
        bmss_bear = bearish_mss.to_numpy(dtype=bool)

        for i in range(len(df)):
            info = None
            vr_i = float(vol_ratio_arr[i]) if np.isfinite(vol_ratio_arr[i]) else 0.0
            mom_i = (
                float(price_change_arr[i]) if np.isfinite(price_change_arr[i]) else 0.0
            )

            if bbos[i]:
                lvl = (
                    float(level_broken_high[i])
                    if np.isfinite(level_broken_high[i])
                    else np.nan
                )
                info = {
                    "type": "bullish_bos",
                    "level_broken": lvl,
                    "confirmation_score": (
                        min(1.0, vr_i / max(volume_multiplier, eps))
                        if enable_volume_conf
                        else 1.0
                    ),
                    "volume_ratio": round(vr_i, 3),
                    "momentum": round(mom_i, 6),
                    "structure_type": "continuation",
                    "quality": _quality_from_volratio(vr_i),
                }
            elif bbss[i]:
                lvl = (
                    float(level_broken_low[i])
                    if np.isfinite(level_broken_low[i])
                    else np.nan
                )
                info = {
                    "type": "bearish_bos",
                    "level_broken": lvl,
                    "confirmation_score": (
                        min(1.0, vr_i / max(volume_multiplier, eps))
                        if enable_volume_conf
                        else 1.0
                    ),
                    "volume_ratio": round(vr_i, 3),
                    "momentum": round(mom_i, 6),
                    "structure_type": "continuation",
                    "quality": _quality_from_volratio(vr_i),
                }
            elif bmss[i]:
                lvl = (
                    float(level_broken_high[i])
                    if np.isfinite(level_broken_high[i])
                    else np.nan
                )
                info = {
                    "type": "bullish_mss",
                    "level_broken": lvl,
                    "confirmation_score": (
                        min(1.0, vr_i / max(volume_multiplier, eps))
                        if enable_volume_conf
                        else 1.0
                    ),
                    "volume_ratio": round(vr_i, 3),
                    "momentum": round(mom_i, 6),
                    "structure_type": "reversal",
                    "quality": _quality_from_volratio(vr_i),
                }
            elif bmss_bear[i]:
                lvl = (
                    float(level_broken_low[i])
                    if np.isfinite(level_broken_low[i])
                    else np.nan
                )
                info = {
                    "type": "bearish_mss",
                    "level_broken": lvl,
                    "confirmation_score": (
                        min(1.0, vr_i / max(volume_multiplier, eps))
                        if enable_volume_conf
                        else 1.0
                    ),
                    "volume_ratio": round(vr_i, 3),
                    "momentum": round(mom_i, 6),
                    "structure_type": "reversal",
                    "quality": _quality_from_volratio(vr_i),
                }

            results.append(info)

        # --- Logging synthétique ---
        valid_breaks = [r for r in results if r is not None]
        if valid_breaks:
            bos_count = sum(1 for r in valid_breaks if "bos" in r["type"])
            mss_count = sum(1 for r in valid_breaks if "mss" in r["type"])
            high_quality = sum(1 for r in valid_breaks if r.get("quality") == "high")
            self.logger.debug(
                f"[BOS/MSS vX] breaks={len(valid_breaks)} (BOS={bos_count}, MSS={mss_count}, highQ={high_quality}) | "
                f"vol_thr={volume_multiplier} lookback={volume_lookback} dist_min={min_break_distance} "
                f"require_close_beyond={require_close_beyond}"
            )

        return results

    def detect_liquidity_sweeps(
        self, df: pd.DataFrame, sweep_config: Optional[Dict[str, Any]] = None
    ) -> List[Optional[Dict[str, Any]]]:
        """
        💧 Détection Liquidity Sweeps (stop hunts institutionnels).
        Retourne une liste alignée sur df avec détails ou None.
        """

        if df is None or df.empty:
            return []

        cfg = sweep_config or {}
        lookback = int(cfg.get("sweep", {}).get("lookback_bars", 20))
        wick_min = float(cfg.get("sweep", {}).get("wick_to_body_min_ratio", 1.5))
        min_dist = float(cfg.get("sweep", {}).get("min_distance_pips", 3.0))
        vol_sigma = float(cfg.get("sweep", {}).get("volume_spike_sigma", 1.5))

        eps = 1e-12
        pip_size = None
        try:
            point_val = float(df["point"].iloc[-1])
            pip_size = point_val * 10.0 if point_val > 0 else None
        except Exception:
            pip_size = None
        pip_size = pip_size or 1.0

        oc_max = df[["open", "close"]].max(axis=1)
        oc_min = df[["open", "close"]].min(axis=1)
        up_wick = (df["high"] - oc_max).clip(lower=0.0)
        dn_wick = (oc_min - df["low"]).clip(lower=0.0)
        body = (df["close"] - df["open"]).abs().replace(0, eps)

        up_wr = up_wick / body
        dn_wr = dn_wick / body

        hh_prev = df["high"].rolling(lookback).max().shift(1)
        ll_prev = df["low"].rolling(lookback).min().shift(1)

        dist_up = ((df["high"] - hh_prev).clip(lower=0.0)) / pip_size
        dist_dn = ((ll_prev - df["low"]).clip(lower=0.0)) / pip_size

        vol_z = pd.to_numeric(df.get("volume_zscore", 0.0), errors="coerce").fillna(0.0)

        sweep_up = (
            (df["high"] > hh_prev)
            & (up_wr >= wick_min)
            & (dist_up >= min_dist)
            & (vol_z >= vol_sigma)
        )
        sweep_dn = (
            (df["low"] < ll_prev)
            & (dn_wr >= wick_min)
            & (dist_dn >= min_dist)
            & (vol_z >= vol_sigma)
        )

        results: List[Optional[Dict[str, Any]]] = []
        for i in range(len(df)):
            info = None
            if sweep_up.iloc[i] or sweep_dn.iloc[i]:
                info = {
                    "index": i,
                    "timestamp": str(df.index[i]),
                    "side": "sell" if sweep_up.iloc[i] else "buy",
                    "wick_ratio": float(
                        up_wr.iloc[i] if sweep_up.iloc[i] else dn_wr.iloc[i]
                    ),
                    "dist_pips": float(
                        dist_up.iloc[i] if sweep_up.iloc[i] else dist_dn.iloc[i]
                    ),
                    "volume_z": float(vol_z.iloc[i]),
                    "present": True,
                }
            results.append(info)
        return results

    def detect_absorption(
        self, df: pd.DataFrame, abs_config: Optional[Dict[str, Any]] = None
    ) -> List[Optional[Dict[str, Any]]]:
        """
        🛡️ Détection Absorption institutionnelle après sweep.
        Retourne une liste alignée sur df avec détails ou None.
        """

        if df is None or df.empty:
            return []

        cfg = abs_config or {}
        body_min = float(cfg.get("body_to_range_min", 0.5))
        closes_mid = bool(cfg.get("closes_through_mid_of_sweep", True))
        eps = 1e-12

        body = (df["close"] - df["open"]).abs()
        full_range = (df["high"] - df["low"]).replace(0, eps)
        body_ratio = body / full_range
        mid_range = (df["high"] + df["low"]) / 2.0

        absorb_up = (
            (df["close"] < df["open"])
            & (body_ratio >= body_min)
            & ((not closes_mid) | (df["close"] <= mid_range))
        )
        absorb_dn = (
            (df["close"] > df["open"])
            & (body_ratio >= body_min)
            & ((not closes_mid) | (df["close"] >= mid_range))
        )

        results: List[Optional[Dict[str, Any]]] = []
        for i in range(len(df)):
            info = None
            if absorb_up.iloc[i] or absorb_dn.iloc[i]:
                info = {
                    "index": i,
                    "timestamp": str(df.index[i]),
                    "confirmed": True,
                    "side": "buy" if absorb_dn.iloc[i] else "sell",
                    "body_ratio": float(body_ratio.iloc[i]),
                }
            results.append(info)
        return results

    def detect_eqh_eql(
        self, df: pd.DataFrame, eqh_config: Optional[Dict[str, Any]] = None
    ) -> List[Optional[Dict[str, Any]]]:
        """
        🎯 Détection Equal Highs / Equal Lows (EQH/EQL).
        Retourne une liste alignée sur df (longueur = len(df)).
        Améliorations :
        - Alignement garanti avec df.index
        - Qualité ajoutée (low / medium / high)
        - Tolérance dynamique en pips
        """

        if df is None or len(df) < 5:
            return [None] * (len(df) if df is not None else 0)

        cfg = eqh_config or {}
        tolerance_pips = float(cfg.get("tolerance_pips", 2.0))
        min_touches = int(cfg.get("min_touches", 2))

        # Détermination taille pip
        try:
            point_val = float(df["point"].iloc[-1])
            pip_size = point_val * 10.0 if point_val > 0 else 1.0
        except Exception:
            pip_size = 1.0

        highs = df["high"].round(5)
        lows = df["low"].round(5)

        results: List[Optional[Dict[str, Any]]] = [None] * len(df)

        for i in range(min_touches - 1, len(df)):
            info = None

            # Equal Highs
            recent_highs = highs.iloc[i - min_touches + 1 : i + 1]
            if recent_highs.max() - recent_highs.min() <= tolerance_pips * pip_size:
                info = {
                    "index": int(i),
                    "timestamp": str(df.index[i]),
                    "type": "eqh",
                    "level": float(recent_highs.mean()),
                    "touches": len(recent_highs),
                    "quality": "high" if len(recent_highs) >= min_touches + 1 else "medium",
                }

            # Equal Lows
            recent_lows = lows.iloc[i - min_touches + 1 : i + 1]
            if recent_lows.max() - recent_lows.min() <= tolerance_pips * pip_size:
                info = {
                    "index": int(i),
                    "timestamp": str(df.index[i]),
                    "type": "eql",
                    "level": float(recent_lows.mean()),
                    "touches": len(recent_lows),
                    "quality": "high" if len(recent_lows) >= min_touches + 1 else "medium",
                }

            results[i] = info

        return results


    def detect_candle_patterns(
        self,
        df: pd.DataFrame,
        min_long_mult: float = 2.5,
        min_body_ratio: float = 0.65,
        small_body_ratio: float = 0.2,
        wick_ratio: float = 2.0,
    ) -> List[Optional[Dict[str, Any]]]:
        """
        🔮 Détection avancée des chandeliers et patterns multi-bougies.

        Couvre :
        - Bougies : longues, petites, doji, pinbar, marubozu, engulfing
        - Séquences : clusters, momentum runs, confirmations
        - Patterns : Morning Star, Evening Star, Soldiers, Crows, Harami, Tweezer

        Retourne une liste enrichie avec : pattern, strength_score, contexte (OB/FVG/BOS).
        """

        if df is None or len(df) < 30:
            return []

        df = df.copy()
        df["candle_size"] = df["high"] - df["low"]
        df["body_size"] = (df["close"] - df["open"]).abs()
        df["upper_wick"] = df["high"] - df[["open", "close"]].max(axis=1)
        df["lower_wick"] = df[["open", "close"]].min(axis=1) - df["low"]
        df["body_ratio"] = df["body_size"] / df["candle_size"].replace(0, np.nan)

        avg_size = df["candle_size"].rolling(20).mean()
        signals: List[Optional[Dict[str, Any]]] = []

        for i in range(len(df)):
            try:
                signal = None
                score = 0.0
                size = df["candle_size"].iloc[i]
                body = df["body_size"].iloc[i]
                body_r = df["body_ratio"].iloc[i]
                up_wick = df["upper_wick"].iloc[i]
                low_wick = df["lower_wick"].iloc[i]
                avg = avg_size.iloc[i] if pd.notna(avg_size.iloc[i]) else size
                is_bull = df["close"].iloc[i] > df["open"].iloc[i]

                # === Bougies individuelles ===
                if size > min_long_mult * avg and body_r >= min_body_ratio:
                    signal, score = ("long_bullish" if is_bull else "long_bearish", 0.8)

                elif body_r < small_body_ratio and size < 0.5 * avg:
                    signal, score = ("small_accumulation", 0.3)

                elif body <= 0.1 * size:  # Doji
                    signal, score = ("doji", 0.5)

                elif low_wick > wick_ratio * body and up_wick < body:
                    signal, score = ("hammer" if is_bull else "bullish_pinbar", 0.7)
                elif up_wick > wick_ratio * body and low_wick < body:
                    signal, score = (
                        "shooting_star" if not is_bull else "bearish_pinbar",
                        0.7,
                    )

                # Engulfing
                if i > 0 and body > df["body_size"].iloc[i - 1]:
                    if is_bull and df["close"].iloc[i] > df["open"].iloc[i - 1]:
                        signal, score = ("bullish_engulfing", 0.9)
                    elif not is_bull and df["close"].iloc[i] < df["open"].iloc[i - 1]:
                        signal, score = ("bearish_engulfing", 0.9)

                # Marubozu
                if body_r > 0.95 and up_wick < 0.05 * size and low_wick < 0.05 * size:
                    signal, score = (
                        "marubozu_bull" if is_bull else "marubozu_bear",
                        1.0,
                    )

                # === Séquences dynamiques ===
                if i >= 2:
                    last_patterns = [s["pattern"] if s else None for s in signals[-2:]]
                    if last_patterns.count("doji") == 2 and signal == "doji":
                        signal, score = ("doji_cluster_consolidation", 0.7)
                    if (
                        last_patterns.count("long_bullish") == 2
                        and signal == "long_bullish"
                    ):
                        signal, score = ("bullish_momentum_run", 1.0)
                    if (
                        last_patterns.count("long_bearish") == 2
                        and signal == "long_bearish"
                    ):
                        signal, score = ("bearish_momentum_run", 1.0)
                    if (
                        "bullish_engulfing" in last_patterns
                        and signal == "marubozu_bull"
                    ):
                        signal, score = ("confirmed_bullish_reversal", 1.2)
                    if (
                        "bearish_engulfing" in last_patterns
                        and signal == "marubozu_bear"
                    ):
                        signal, score = ("confirmed_bearish_reversal", 1.2)

                # === Patterns multi-bougies ===
                if i >= 2:
                    o1, c1 = df["open"].iloc[i - 2], df["close"].iloc[i - 2]
                    o2, c2 = df["open"].iloc[i - 1], df["close"].iloc[i - 1]
                    o3, c3 = df["open"].iloc[i], df["close"].iloc[i]

                    # Morning Star
                    if (
                        c1 < o1
                        and abs(c2 - o2) < 0.3 * avg
                        and c3 > o3
                        and c3 > (o1 + c1) / 2
                    ):
                        signal, score = ("morning_star", 1.2)

                    # Evening Star
                    if (
                        c1 > o1
                        and abs(c2 - o2) < 0.3 * avg
                        and c3 < o3
                        and c3 < (o1 + c1) / 2
                    ):
                        signal, score = ("evening_star", 1.2)

                    # Three White Soldiers
                    if all(
                        df["close"].iloc[j] > df["open"].iloc[j]
                        for j in [i - 2, i - 1, i]
                    ):
                        signal, score = ("three_white_soldiers", 1.3)

                    # Three Black Crows
                    if all(
                        df["close"].iloc[j] < df["open"].iloc[j]
                        for j in [i - 2, i - 1, i]
                    ):
                        signal, score = ("three_black_crows", 1.3)

                    # Harami
                    if c1 > o1 and c2 < o2 and o2 < c1 and c2 > o1:
                        signal, score = ("bearish_harami", 0.9)
                    if c1 < o1 and c2 > o2 and o2 > c1 and c2 < o1:
                        signal, score = ("bullish_harami", 0.9)

                    # Tweezer Top/Bottom
                    if abs(df["high"].iloc[i] - df["high"].iloc[i - 1]) < 0.1 * avg:
                        signal, score = (
                            ("tweezer_top", 0.8)
                            if not is_bull
                            else ("tweezer_bottom", 0.8)
                        )

                # Enrichissement
                if signal:
                    signals.append(
                        {
                            "index": i,
                            "timestamp": str(df.index[i]),
                            "pattern": signal,
                            "strength_score": score,
                            "body_ratio": round(body_r, 3),
                            "candle_size": round(size, 5),
                            "avg_size": round(avg, 5),
                            "upper_wick": round(up_wick, 5),
                            "lower_wick": round(low_wick, 5),
                            "near_ob": bool(
                                "ob_zone" in df.columns
                                and not pd.isna(df["ob_zone"].iloc[i])
                            ),
                            "near_fvg": bool(
                                "fvg" in df.columns and not pd.isna(df["fvg"].iloc[i])
                            ),
                            "near_bos": bool(
                                "bos" in df.columns and not pd.isna(df["bos"].iloc[i])
                            ),
                        }
                    )

                    # === ENRICHISSEMENT AVANCÉ (ajout par-dessus la logique existante) ===
                    enriched_signals = []
                    for i, sig in enumerate(signals):
                        if not sig:
                            enriched_signals.append(None)
                            continue

                        enriched = sig.copy()

                        # 1) PhaseObserver si dispo
                        phase = df.iloc[i]["phase"] if "phase" in df.columns else None
                        if phase:
                            enriched["context_phase"] = str(phase)
                            if "impulsion" in str(phase):
                                enriched["strength_score"] = round(
                                    enriched["strength_score"] * 1.2, 3
                                )
                            elif "range" in str(phase):
                                enriched["strength_score"] = round(
                                    enriched["strength_score"] * 0.9, 3
                                )

                        # 2) Volatilité / Volume
                        vol = (
                            df.iloc[i]["volatility_pct"]
                            if "volatility_pct" in df.columns
                            else None
                        )
                        if vol is not None:
                            enriched["volatility"] = float(vol)
                            if vol < 0.005:  # marché trop plat
                                enriched["strength_score"] *= 0.8
                            elif vol > 0.05:  # marché trop violent
                                enriched["strength_score"] *= 0.9

                        if "volume_zscore" in df.columns:
                            vz = df.iloc[i]["volume_zscore"]
                            enriched["volume_zscore"] = float(vz)
                            if vz > 2:
                                enriched["strength_score"] *= 1.1

                        # 3) Confluence structurelle (OB/FVG/BOS déjà présents)
                        if (
                            enriched.get("near_ob")
                            or enriched.get("near_fvg")
                            or enriched.get("near_bos")
                        ):
                            enriched["strength_score"] *= 1.15
                            enriched["confluence"] = True
                        else:
                            enriched["confluence"] = False

                        # 4) Multi-timeframe confirmation (si colonnes M5/M15 présentes dans df)
                        confirmed_tf = []
                        for tf in ["pattern_m5", "pattern_m15"]:
                            if (
                                tf in df.columns
                                and df[tf].iloc[i] == enriched["pattern"]
                            ):
                                confirmed_tf.append(tf.upper())
                        if confirmed_tf:
                            enriched["strength_score"] *= 1.2
                            enriched["confirmed_tf"] = confirmed_tf

                        # 5) Normalisation finale du score
                        enriched["strength_score"] = round(
                            min(2.0, enriched["strength_score"]), 3
                        )

                        enriched_signals.append(enriched)

                    return enriched_signals

                else:
                    signals.append(None)

            except Exception as e:
                self.logger.error(f"Erreur détection bougie: {e}")
                signals.append(None)

        return signals

   
    def detect_market_regime(self, df: pd.DataFrame) -> pd.Series:
        """
        🏛️ Market Regime Detection - Version améliorée avec mémoire de phase.

        Régimes détectés:
        - trending_institutional_bull/bear | trending_retail_bull/bear
        - range_accumulation/distribution | range_institutional | range_retail
        - high_volatility_chaos | low_volatility_compression | transitional

        Changements :
        - Conserve la dernière phase si les signaux actuels sont ambigus
        - Ne tombe pas dans "unknown" sauf données invalides
        - Le changement de phase n'est validé que si les signaux dépassent un seuil de clarté
        """

        self.logger.debug("Détection du régime de marché sophistiquée...")

        if df is None or df.empty:
            self.logger.warning("DataFrame vide, impossible de détecter un régime.")
            return pd.Series(dtype=object)

        # Charger config
        regime_config = (
            self.config_manager.get(
                "phase_detection_defaults.regime_detection_settings", {}
            )
            or {}
        )
        adx_config = regime_config.get("adx_settings", {}) or {}
        vol_config = regime_config.get("volatility_regimes", {}) or {}
        volume_config = regime_config.get("volume_profile", {}) or {}

        # === 1. CALCUL ADX ===
        adx_period = int(adx_config.get("period", 14))
        trending_threshold = float(adx_config.get("trending_threshold", 25))
        ranging_threshold = float(adx_config.get("ranging_threshold", 20))

        def calculate_adx(_df: pd.DataFrame, period: int = 14):
            high, low, close = _df["high"], _df["low"], _df["close"]
            tr1 = high - low
            tr2 = (high - close.shift()).abs()
            tr3 = (low - close.shift()).abs()
            tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

            up_move, down_move = high.diff(), -low.diff()
            dm_plus = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
            dm_minus = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

            atr = tr.rolling(window=period, min_periods=1).mean().replace(0, np.nan)
            dm_plus_smooth = (
                pd.Series(dm_plus, index=_df.index)
                .rolling(window=period, min_periods=1)
                .mean()
            )
            dm_minus_smooth = (
                pd.Series(dm_minus, index=_df.index)
                .rolling(window=period, min_periods=1)
                .mean()
            )

            di_plus = 100.0 * dm_plus_smooth / atr
            di_minus = 100.0 * dm_minus_smooth / atr

            denom = (di_plus + di_minus).replace(0, np.nan)
            dx = (100.0 * (di_plus - di_minus).abs() / denom).fillna(0.0)
            adx = dx.rolling(window=period, min_periods=1).mean()
            return adx, di_plus.fillna(0.0), di_minus.fillna(0.0)

        adx, di_plus, di_minus = calculate_adx(df, adx_period)

        # === 2. VOLATILITÉ GARMAN-KLASS ===
        vol_period = int(vol_config.get("calculation_period", 20))
        high_vol_percentile = float(vol_config.get("high_vol_percentile", 75))
        low_vol_percentile = float(vol_config.get("low_vol_percentile", 25))

        ln_high_low = np.log((df["high"] / df["low"]).clip(lower=1e-12))
        ln_close_open = np.log((df["close"] / df["open"]).clip(lower=1e-12))
        gk_vol = 0.5 * ln_high_low**2 - (2 * np.log(2) - 1) * ln_close_open**2
        volatility = np.sqrt(gk_vol.rolling(window=vol_period, min_periods=1).mean())

        vol_percentiles = volatility.rolling(
            window=max(100, vol_period * 5), min_periods=1
        ).apply(lambda x: (x <= x.iloc[-1]).mean() * 100.0, raw=False)

        # === 3. VOLUME PROFILE ===
        enable_institutional = bool(
            volume_config.get("enable_institutional_detection", True)
        )
        volume_ma_period = int(volume_config.get("volume_ma_period", 20))
        institutional_threshold = float(
            volume_config.get("institutional_threshold", 1.8)
        )

        institutional_activity = pd.Series(False, index=df.index)
        if enable_institutional and "tick_volume" in df.columns:
            volume_ma = (
                df["tick_volume"]
                .rolling(window=volume_ma_period, min_periods=1)
                .mean()
                .replace(0, np.nan)
            )
            volume_ratio = (df["tick_volume"] / volume_ma).fillna(0.0)
            institutional_activity = volume_ratio > institutional_threshold

        # === 4. DÉTERMINATION DU RÉGIME ===
        regimes = pd.Series("unknown", index=df.index, dtype=object)

        for i in range(len(df)):
            current_adx = float(adx.iloc[i]) if pd.notna(adx.iloc[i]) else 0.0
            current_di_plus = (
                float(di_plus.iloc[i]) if pd.notna(di_plus.iloc[i]) else 0.0
            )
            current_di_minus = (
                float(di_minus.iloc[i]) if pd.notna(di_minus.iloc[i]) else 0.0
            )
            current_vol_percentile = (
                float(vol_percentiles.iloc[i])
                if pd.notna(vol_percentiles.iloc[i])
                else 50.0
            )
            is_institutional = bool(institutional_activity.iloc[i])

            # --- Phase trending
            if current_adx > trending_threshold:
                if current_di_plus > current_di_minus:
                    regimes.iloc[i] = (
                        "trending_institutional_bull"
                        if is_institutional
                        else "trending_retail_bull"
                    )
                else:
                    regimes.iloc[i] = (
                        "trending_institutional_bear"
                        if is_institutional
                        else "trending_retail_bear"
                    )

            # --- Phase range
            elif current_adx < ranging_threshold:
                if is_institutional:
                    recent_closes = df["close"].iloc[max(0, i - 10) : i + 1]
                    if len(recent_closes) > 5:
                        regimes.iloc[i] = (
                            "range_accumulation"
                            if (recent_closes.iloc[-1] > recent_closes.mean())
                            else "range_distribution"
                        )
                    else:
                        regimes.iloc[i] = "range_institutional"
                else:
                    regimes.iloc[i] = "range_retail"

            # --- Volatilité / Transition
            else:
                if current_vol_percentile >= high_vol_percentile:
                    regimes.iloc[i] = "high_volatility_chaos"
                elif current_vol_percentile <= low_vol_percentile:
                    regimes.iloc[i] = "low_volatility_compression"
                else:
                    regimes.iloc[i] = "transitional"

            # --- AMÉLIORATION : conserver la phase précédente si ambigu
            if regimes.iloc[i] == "unknown":
                if hasattr(self, "_last_regime") and self._last_regime:
                    regimes.iloc[i] = self._last_regime
                    self.logger.debug(
                        f"Ambigu → on conserve l'ancien régime: {self._last_regime}"
                    )

            # Mettre à jour la mémoire
            self._last_regime = regimes.iloc[i]

        # === 5. QUALITÉ DU RÉGIME ===
        def calculate_regime_strength(
            regime_series: pd.Series, adx_series: pd.Series
        ) -> pd.Series:
            strength = pd.Series(0.5, index=regime_series.index, dtype=float)
            for i in range(len(regime_series)):
                regime, adx_val = str(regime_series.iloc[i]), (
                    float(adx_series.iloc[i]) if pd.notna(adx_series.iloc[i]) else 0.0
                )
                if "trending" in regime:
                    if adx_val > 40:
                        strength.iloc[i] = 0.9
                    elif adx_val > 30:
                        strength.iloc[i] = 0.8
                    elif adx_val > 25:
                        strength.iloc[i] = 0.7
                    else:
                        strength.iloc[i] = 0.6
                elif "range" in regime:
                    if adx_val < 15:
                        strength.iloc[i] = 0.9
                    elif adx_val < 20:
                        strength.iloc[i] = 0.8
                    else:
                        strength.iloc[i] = 0.6
                elif "volatility" in regime:
                    strength.iloc[i] = 0.8
            return strength

        regime_strength = calculate_regime_strength(regimes, adx)

        # Ajouter au DF
        df["regime"] = regimes
        df["regime_strength"] = regime_strength
        df["adx"] = adx
        df["volatility_percentile"] = vol_percentiles
        df["institutional_activity"] = institutional_activity

        # === 6. LOGGING ===
        if len(regimes) > 0:
            dominant_regime = regimes.value_counts().idxmax()
            avg_strength = float(regime_strength.mean())
            self.logger.info(
                f"📊 Régime dominant: {dominant_regime} | force moyenne: {avg_strength:.2f}"
            )
            self.logger.debug(
                f"Distribution régimes: {dict(regimes.value_counts().head(3))}"
            )

        return regimes

    def detect_micro_phase_m1(
        self, df_m1: pd.DataFrame, params: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Détecteur de micro-phase M1 (spécial Burst Scalping).
        - Détecte une compression suivie d'une impulsion directionnelle.
        - Retourne un signal utilisable pour déclencher un burst.
        """
        try:
            if df_m1 is None or len(df_m1) < 60:
                return {
                    "burst_signal": False,
                    "burst_side": "NEUTRAL",
                    "burst_strength": 0.0,
                    "suggested_burst_size": 0,
                    "sl_pips_suggestion": None,
                    "tp_pips_suggestion": None,
                    "diagnostics": {"reason": "not_enough_bars"},
                }

            p = params or {}
            # Fenêtres d’analyse
            w_core = int(p.get("window_core", 20))   # cœur de compression
            w_env = int(p.get("window_env", 60))     # environnement
            k_range = float(p.get("max_range_pips", 8.0))
            k_imp = float(p.get("min_impulse_pips", 3.0))

            # Paramètres burst
            boost = float(p.get("confidence_boost", 0.1))
            burst_base = int(p.get("burst_base_size", 3))  # taille par défaut du burst
            burst_max = int(p.get("burst_max_size", 10))

            sl_floor = float(p.get("sl_min_pips", 5.0))
            sl_cap = float(p.get("sl_max_pips", 12.0))
            tp_sl_ratio = float(p.get("tp_over_sl", 1.5))

            last = df_m1.iloc[-1]
            price = float(last["close"])
            pip_size = 0.01 if price > 10 else 0.0001

            core = df_m1.tail(w_core)
            env = df_m1.tail(w_env)

            # Compression
            core_range_pips = (core["high"].max() - core["low"].min()) / pip_size
            env_range_pips = max(
                (env["high"].max() - env["low"].min()) / pip_size, core_range_pips
            )
            compressed = (core_range_pips <= k_range) and (core_range_pips <= 0.35 * env_range_pips)

            # Impulsion récente
            recent = df_m1.tail(3)
            recent_move = (float(recent["close"].iloc[-1]) - float(recent["open"].iloc[0])) / pip_size
            impulse_ok = abs(recent_move) >= k_imp
            direction = "BUY" if recent_move > 0 else "SELL" if recent_move < 0 else "NEUTRAL"

            # Qualité du setup
            quality = 0.0
            if compressed and impulse_ok:
                q_range = max(0.0, 1.0 - (core_range_pips / max(k_range, 1e-6)))
                q_imp = min(1.0, abs(recent_move) / max(k_imp * 2.0, 1e-6))
                quality = 0.6 * q_range + 0.4 * q_imp

            # Décision Burst
            burst_signal = bool(quality >= 0.4)
            burst_strength = round(min(1.0, quality + boost), 3)
            suggested_burst_size = int(min(burst_max, max(burst_base, int(burst_strength * burst_max))))

            # SL/TP suggestions (scalp serré)
            base_sl = max(sl_floor, min(sl_cap, 0.5 * core_range_pips))
            sl_pips = round(base_sl, 2)
            tp_pips = round(sl_pips * tp_sl_ratio, 2)

            return {
                "burst_signal": burst_signal,
                "burst_side": direction,
                "burst_strength": burst_strength,
                "suggested_burst_size": suggested_burst_size if burst_signal else 0,
                "sl_pips_suggestion": sl_pips if burst_signal else None,
                "tp_pips_suggestion": tp_pips if burst_signal else None,
                "diagnostics": {
                    "compressed": compressed,
                    "core_range_pips": round(core_range_pips, 2),
                    "env_range_pips": round(env_range_pips, 2),
                    "recent_move_pips": round(recent_move, 2),
                    "impulse_ok": impulse_ok,
                },
            }
        except Exception as e:
            return {
                "burst_signal": False,
                "burst_side": "NEUTRAL",
                "burst_strength": 0.0,
                "suggested_burst_size": 0,
                "sl_pips_suggestion": None,
                "tp_pips_suggestion": None,
                "diagnostics": {"error": str(e)},
            }


    def determine_optimized_phase(self, row: Dict[str, Any]) -> str:
        """
        Classification de phase basée uniquement sur les 4 indicateurs core
        + signaux liquidity (sweep, absorption, eqh/eql).
        Nettoyée de toute dépendance Bollinger.
        """
        regime = str(row.get("regime", "unknown"))

        # --- Institutional trending regimes ---
        if "trending_institutional" in regime:
            if row.get("fvg_ob_confluence", False):
                return "institutional_setup_premium"
            elif row.get("ob_detected", False):
                return "institutional_setup"
            elif "bull" in regime:
                return "trending_institutional_bull"
            else:
                return "trending_institutional_bear"

        # --- Ranges (accumulation/distribution) ---
        elif "range_accumulation" in regime:
            if row.get("high_quality_ob", False):
                return "accumulation_zone"
            return "range_accumulation"

        elif "range_distribution" in regime:
            if row.get("confirmed_structure_break", False):
                return "distribution_breakout"
            return "range_distribution"

        # --- High vol / chaos ---
        elif "high_volatility" in regime:
            if row.get("bos_mss_detected", False):
                return "volatility_breakout"
            return "high_volatility_chaos"

        # --- Low vol / compression ---
        elif "low_volatility" in regime:
            return "low_volatility_compression"

        # --- Liquidity-driven signals ---
        if row.get("sweep_detected", False):
            return "liquidity_sweep"
        if row.get("absorption_confirmed", False):
            return "liquidity_absorption"
        if row.get("eqh_eql_detected", False):
            return "liquidity_eqh_eql"

        # --- Fallback divers ---
        if row.get("institutional_setup", False):
            return "smc_setup"
        elif row.get("fvg_detected", False):
            return "fvg_opportunity"

        return "no_clear_phase"


    def determine_phase(self, market_data: pd.DataFrame) -> str:
        """
        Détermine la phase de marché de la dernière bougie via un appel à `analyze`.

        Cette fonction sert d'interface simple pour obtenir l'état le plus récent du marché
        sans retourner le DataFrame complet.
        """
        try:
            annotated_data = self.analyze(market_data)
            if annotated_data is None or annotated_data.empty:
                return "uncertain"
            return str(annotated_data.iloc[-1].get("phase", "uncertain"))
        except Exception as e:
            self.logger.error(f"Erreur dans determine_phase : {e}", exc_info=True)
            return "uncertain"
