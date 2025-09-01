# phase_observer/detectors.py
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
from .features import (
    _get_swing_points,
    _get_adaptive_swing_points,
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
        swing_highs, swing_lows = _get_adaptive_swing_points(self, df)
        df["last_swing_high"] = swing_highs.reindex(df.index).ffill()
        df["last_swing_low"] = swing_lows.reindex(df.index).ffill()

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

    def detect_candle_patterns(
        self,
        df: pd.DataFrame,
        min_long_mult: float = 2.5,
        min_body_ratio: float = 0.65,
        small_body_ratio: float = 0.2,
        wick_ratio: float = 2.0
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
                    signal, score = ("shooting_star" if not is_bull else "bearish_pinbar", 0.7)

                # Engulfing
                if i > 0 and body > df["body_size"].iloc[i - 1]:
                    if is_bull and df["close"].iloc[i] > df["open"].iloc[i - 1]:
                        signal, score = ("bullish_engulfing", 0.9)
                    elif not is_bull and df["close"].iloc[i] < df["open"].iloc[i - 1]:
                        signal, score = ("bearish_engulfing", 0.9)

                # Marubozu
                if body_r > 0.95 and up_wick < 0.05 * size and low_wick < 0.05 * size:
                    signal, score = ("marubozu_bull" if is_bull else "marubozu_bear", 1.0)

                # === Séquences dynamiques ===
                if i >= 2:
                    last_patterns = [s["pattern"] if s else None for s in signals[-2:]]
                    if last_patterns.count("doji") == 2 and signal == "doji":
                        signal, score = ("doji_cluster_consolidation", 0.7)
                    if last_patterns.count("long_bullish") == 2 and signal == "long_bullish":
                        signal, score = ("bullish_momentum_run", 1.0)
                    if last_patterns.count("long_bearish") == 2 and signal == "long_bearish":
                        signal, score = ("bearish_momentum_run", 1.0)
                    if "bullish_engulfing" in last_patterns and signal == "marubozu_bull":
                        signal, score = ("confirmed_bullish_reversal", 1.2)
                    if "bearish_engulfing" in last_patterns and signal == "marubozu_bear":
                        signal, score = ("confirmed_bearish_reversal", 1.2)

                # === Patterns multi-bougies ===
                if i >= 2:
                    o1, c1 = df["open"].iloc[i - 2], df["close"].iloc[i - 2]
                    o2, c2 = df["open"].iloc[i - 1], df["close"].iloc[i - 1]
                    o3, c3 = df["open"].iloc[i], df["close"].iloc[i]

                    # Morning Star
                    if (c1 < o1 and abs(c2 - o2) < 0.3 * avg and c3 > o3 and c3 > (o1 + c1) / 2):
                        signal, score = ("morning_star", 1.2)

                    # Evening Star
                    if (c1 > o1 and abs(c2 - o2) < 0.3 * avg and c3 < o3 and c3 < (o1 + c1) / 2):
                        signal, score = ("evening_star", 1.2)

                    # Three White Soldiers
                    if all(df["close"].iloc[j] > df["open"].iloc[j] for j in [i - 2, i - 1, i]):
                        signal, score = ("three_white_soldiers", 1.3)

                    # Three Black Crows
                    if all(df["close"].iloc[j] < df["open"].iloc[j] for j in [i - 2, i - 1, i]):
                        signal, score = ("three_black_crows", 1.3)

                    # Harami
                    if (c1 > o1 and c2 < o2 and o2 < c1 and c2 > o1):
                        signal, score = ("bearish_harami", 0.9)
                    if (c1 < o1 and c2 > o2 and o2 > c1 and c2 < o1):
                        signal, score = ("bullish_harami", 0.9)

                    # Tweezer Top/Bottom
                    if abs(df["high"].iloc[i] - df["high"].iloc[i - 1]) < 0.1 * avg:
                        signal, score = ("tweezer_top", 0.8) if not is_bull else ("tweezer_bottom", 0.8)

                # Enrichissement
                if signal:
                    signals.append({
                        "index": i,
                        "timestamp": str(df.index[i]),
                        "pattern": signal,
                        "strength_score": score,
                        "body_ratio": round(body_r, 3),
                        "candle_size": round(size, 5),
                        "avg_size": round(avg, 5),
                        "upper_wick": round(up_wick, 5),
                        "lower_wick": round(low_wick, 5),
                        "near_ob": bool("ob_zone" in df.columns and not pd.isna(df["ob_zone"].iloc[i])),
                        "near_fvg": bool("fvg" in df.columns and not pd.isna(df["fvg"].iloc[i])),
                        "near_bos": bool("bos" in df.columns and not pd.isna(df["bos"].iloc[i])),
                    })
                else:
                    signals.append(None)

            except Exception as e:
                self.logger.error(f"Erreur détection bougie: {e}")
                signals.append(None)

        return signals


    def compute_bollinger_microphase_signals(
        self,
        df,
        price_col: str = "close",
        period: int = 20,
        std_mult: float = 2.0,
        squeeze_window: int = 100,
        squeeze_percentile: float = 0.15,
        min_bars: int = 200,
        atr_period: int = 14,
        pip_size: float | None = None,
        mode: str = "katana",
    ) -> dict:
        """
        Calcule des signaux micro-phase basés sur les Bandes de Bollinger pour le scalping Katana.
        - NE PAS MODIFIER LA SIGNATURE ICI (pour intégration sûre).
        - Retourne un dict prêt à consommer par le pipeline (touch, squeeze, breakout_score, mean_revert_score, distances, etc.).
        - ✅ Ajouts/Optimisations:
            * Séries complètes bb_upper/bb_lower/bb_mid (mise à jour continue) -> out["series"]
            * Bandes plus ROBUSTES (EMA + écart-type robustifié par MAD/winsor)
            * range_score + is_range + range_duration_bars
            * midline (bb_mid) + logique d'entrée "médiane" (mid_entry, mid_entry_score)
            * gate d’entrée mediane 'entry_gate_ok' (distance mini à la médiane)
            * critères multi-indicateurs: bandwidth, ADX, pente EMA, largeur RSI
            * hysteresis/débounce basiques pour stabiliser la détection de range
            * ✅ Correction pandas: remplace .fillna(method="ffill") par .ffill()
        """

        out = {
            "ok": False,
            "reason": None,
            "signal": "neutral",  # "buy_revert" | "sell_revert" | "buy_breakout" | "sell_breakout" | "neutral"
            "band_touch": None,  # "upper" | "lower" | None
            "in_band": None,  # True si close ∈ [lower, upper]
            "is_squeeze": None,  # compression vol
            "is_expansion": None,  # expansion post-squeeze
            "squeeze_strength": 0.0,  # 0..1
            "breakout_score": 0.0,  # 0..1
            "mean_revert_score": 0.0,  # 0..1
            "z_band": None,  # distance normalisée au milieu
            "dist_to_upper_pips": None,
            "dist_to_lower_pips": None,
            "dist_to_mid_pips": None,
            "bb_upper": None,
            "bb_lower": None,
            "bb_mid": None,
            "atr_pips": None,
            # ✅ Nouveaux champs
            "range_score": 0.0,  # 0..1
            "is_range": False,
            "range_duration_bars": 0,
            "mid_entry": None,  # "buy" | "sell" | None (idée médiane)
            "mid_entry_score": 0.0,
            # Gate d'entrée (BUY sous mid, SELL au-dessus) + distance mini à la médiane
            "entry_gate_ok": False,
            "mid_distance_ratio": 0.0,  # |price-mid| / half_band (0..1)
            "half_band_pips": None,
            "meta": {
                "period": period,
                "std_mult": std_mult,
                "mode": mode,
                "range": {
                    "weights": {
                        "bandwidth": 0.35,
                        "adx": 0.25,
                        "ema_slope": 0.20,
                        "rsi_width": 0.20,
                    },
                    "threshold_in": 0.62,
                    "threshold_out": 0.52,
                    "debounce_bars": 3,
                    "rsi_period": 14,
                    "ema_slope_window": max(8, period // 2),
                    "adx_period": 14,
                    "rsi_width_window": 14,
                },
                "mid_entry": {
                    "pos_band_min": 0.12,  # distance mini à la médiane (exigence renforcée)
                    "pos_band_max": 0.65,
                    "mom_norm_min": 0.05,
                    "base_threshold": 0.55,
                },
                # Paramètres robustification des bandes
                "robust": {
                    "winsor_alpha": 0.05,  # 5% winsorisation des résidus
                    "mad_blend": 0.40,  # mélange 40% MAD, 60% STD
                },
            },
            # ⚡ Séries historiques complètes des bandes pour traçage/backtest/export
            "series": {"bb_upper": None, "bb_lower": None, "bb_mid": None},
        }

        # --- Guardrails & inputs ---
        if df is None or len(df) < max(min_bars, period + 2):
            out["reason"] = f"insufficient_bars_{len(df) if df is not None else 0}"
            return out
        if price_col not in df.columns:
            out["reason"] = f"missing_price_col_{price_col}"
            return out

        series = pd.to_numeric(df[price_col], errors="coerce").astype(float)
        if series.isna().any():
            series = series.ffill().bfill()
        if not np.isfinite(series.iloc[-1]):
            out["reason"] = "invalid_last_price"
            return out

        # === Bandes de Bollinger ROBUSTES sur tout l'historique ===
        mid = series.ewm(span=period, adjust=False, min_periods=period).mean()
        resid_raw = series - mid

        # Winsorisation simple des résidus (limite l'effet des mèches extrêmes)
        try:
            alpha = float(out["meta"]["robust"]["winsor_alpha"])
            lo = resid_raw.quantile(alpha)
            hi = resid_raw.quantile(1 - alpha)
            resid_w = resid_raw.clip(lower=lo, upper=hi)
        except Exception:
            resid_w = resid_raw

        # Écart-type classique + MAD (écart absolu médian) -> mélange
        rolling_std = resid_w.rolling(window=period, min_periods=period).std(ddof=0)
        med = resid_w.rolling(window=period, min_periods=period).median()
        mad = (resid_w - med).abs().rolling(window=period, min_periods=period).median()
        mad_sigma = 1.4826 * mad  # MAD -> proxy sigma

        blend = float(out["meta"]["robust"]["mad_blend"])
        robust_sigma = (1.0 - blend) * rolling_std + blend * mad_sigma

        upper = mid + std_mult * robust_sigma
        lower = mid - std_mult * robust_sigma

        # Clamp/ffill pour robustesse historique (et suppression FutureWarning)
        bb_mid_series = mid.replace([np.inf, -np.inf], np.nan).ffill()
        bb_upper_series = upper.replace([np.inf, -np.inf], np.nan).ffill()
        bb_lower_series = lower.replace([np.inf, -np.inf], np.nan).ffill()

        # ⚡ Export séries complètes dans la sortie (historique entier)
        out["series"]["bb_mid"] = bb_mid_series
        out["series"]["bb_upper"] = bb_upper_series
        out["series"]["bb_lower"] = bb_lower_series

        # Dernière barre (compat legacy)
        price = float(series.iloc[-1])
        bb_mid = float(bb_mid_series.iloc[-1])
        bb_upper = float(bb_upper_series.iloc[-1])
        bb_lower = float(bb_lower_series.iloc[-1])

        # Sécurité bornes
        if not all(map(np.isfinite, [bb_mid, bb_upper, bb_lower, price])):
            out["reason"] = "nan_in_bbands"
            return out

        out["bb_mid"], out["bb_upper"], out["bb_lower"] = bb_mid, bb_upper, bb_lower

        # --- ATR (pips) pour calibrer les scores et distances ---
        def _atr(df_in: pd.DataFrame, p: int = 14) -> float:
            try:
                h = pd.to_numeric(df_in["high"], errors="coerce").astype(float)
                l = pd.to_numeric(df_in["low"], errors="coerce").astype(float)
                c = pd.to_numeric(df_in["close"], errors="coerce").astype(float)
                tr = pd.concat(
                    [(h - l).abs(), (h - c.shift()).abs(), (l - c.shift()).abs()],
                    axis=1,
                ).max(axis=1)
                a = tr.rolling(window=p, min_periods=p).mean().iloc[-1]
                return float(a) if np.isfinite(a) else float("nan")
            except Exception:
                return float("nan")

        atr = _atr(df, atr_period)
        # point->pip (essaie depuis df sinon symbol_info/Config)
        if pip_size is None:
            point = (
                float(df["point"].iloc[-1])
                if "point" in df.columns
                else float(
                    getattr(getattr(self, "symbol_info", None), "point", 0.0) or 0.0
                )
            )
            pip_size = point * 10.0 if point > 0 else None
        atr_pips = (atr / pip_size) if (pip_size and atr and atr > 0) else None
        out["atr_pips"] = (
            float(atr_pips) if atr_pips is not None and np.isfinite(atr_pips) else None
        )

        # --- Distances en pips ---
        def _to_pips(delta: float) -> float | None:
            if pip_size and pip_size > 0 and np.isfinite(delta):
                return float(delta / pip_size)
            return None

        out["dist_to_upper_pips"] = _to_pips(bb_upper - price)
        out["dist_to_lower_pips"] = _to_pips(price - bb_lower)
        out["dist_to_mid_pips"] = _to_pips(abs(price - bb_mid))

        # --- Touch / In-band ---
        eps = 1e-12
        in_band = (price <= bb_upper + eps) and (price >= bb_lower - eps)
        band_touch = (
            "upper"
            if price >= bb_upper - eps
            else ("lower" if price <= bb_lower + eps else None)
        )
        out["in_band"] = bool(in_band)
        out["band_touch"] = band_touch

        # --- Squeeze / Expansion via bande-width percentile ---
        width = (upper - lower) / (mid.replace(0, np.nan).abs())
        w_non_na = width.dropna()

        if len(w_non_na) >= min(squeeze_window, len(width)):
            w_hist = w_non_na.tail(squeeze_window)
            if not w_hist.empty:
                thresh = np.nanpercentile(w_hist.values, squeeze_percentile * 100.0)
                is_squeeze = bool(width.iloc[-1] <= thresh)
                if len(width) >= 2 and np.isfinite(width.iloc[-2]):
                    is_expansion = bool(
                        (width.iloc[-1] > width.iloc[-2]) and (not is_squeeze)
                    )
                else:
                    is_expansion = False
            else:
                is_squeeze = False
                is_expansion = False
                thresh = np.nan
        else:
            is_squeeze = False
            is_expansion = False
            thresh = np.nan

        out["is_squeeze"] = is_squeeze
        out["is_expansion"] = is_expansion
        if np.isfinite(thresh) and thresh > 0:
            squeeze_strength = 1.0 - float(width.iloc[-1] / (thresh + 1e-12))
            out["squeeze_strength"] = max(0.0, min(1.0, squeeze_strength))
        else:
            out["squeeze_strength"] = 0.0

        # --- Z-band: position du prix dans le canal (-inf..+inf), 0=milieu ---
        last_std_val = robust_sigma.iloc[-1]
        last_std = float(last_std_val) if np.isfinite(last_std_val) else 0.0
        z_band = (price - bb_mid) / (last_std if last_std > 0 else np.nan)
        out["z_band"] = float(z_band) if np.isfinite(z_band) else None

        # --- Momentum (EWM diffs) + normalisation ATR ---
        close = pd.to_numeric(df["close"], errors="coerce").astype(float)
        mom_fast = (
            close.diff().ewm(span=max(2, period // 5), adjust=False).mean().iloc[-1]
        )
        mom_slow = (
            close.diff().ewm(span=max(3, period // 2), adjust=False).mean().iloc[-1]
        )
        momentum = (
            float(mom_fast - mom_slow)
            if all(map(np.isfinite, [mom_fast, mom_slow]))
            else 0.0
        )
        norm_mom = float(momentum / atr) if atr and atr > 0 else 0.0
        norm_mom = max(-3.0, min(3.0, norm_mom))  # clip

        outside_upper = price > bb_upper
        outside_lower = price < bb_lower

        # =========================================================
        # ✅ DÉTECTION DE RANGE MULTI-INDICATEURS (SCORING)
        # =========================================================
        def _ema_slope_norm(mid_series: pd.Series, win: int) -> float:
            try:
                ema_smooth = mid_series.ewm(
                    span=win, adjust=False, min_periods=win
                ).mean()
                slope = ema_smooth.diff().iloc[-1]
                denom = (upper - lower).ewm(span=win, adjust=False).mean().iloc[
                    -1
                ] / 2.0
                denom = (
                    float(denom) if np.isfinite(denom) and denom != 0 else float("nan")
                )
                val = abs(float(slope) / denom) if np.isfinite(denom) else float("nan")
                return float(
                    max(0.0, min(1.0, 1.0 - min(val, 1.0)))
                )  # pente faible -> 1
            except Exception:
                return 0.5

        def _rsi(series_in: pd.Series, p: int) -> pd.Series:
            delta = series_in.diff()
            up = delta.clip(lower=0).ewm(alpha=1 / p, adjust=False).mean()
            dn = (-delta.clip(upper=0)).ewm(alpha=1 / p, adjust=False).mean()
            rs = up / (dn.replace(0, np.nan))
            rsi = 100 - (100 / (1 + rs))
            return rsi

        def _adx(df_in: pd.DataFrame, p: int) -> float:
            try:
                h = pd.to_numeric(df_in["high"], errors="coerce").astype(float)
                l = pd.to_numeric(df_in["low"], errors="coerce").astype(float)
                c = pd.to_numeric(df_in["close"], errors="coerce").astype(float)

                plus_dm = (h.diff()).clip(lower=0)
                minus_dm = (-l.diff()).clip(lower=0)
                plus_dm[plus_dm < minus_dm] = 0
                minus_dm[minus_dm <= plus_dm] = 0

                tr = pd.concat(
                    [(h - l).abs(), (h - c.shift()).abs(), (l - c.shift()).abs()],
                    axis=1,
                ).max(axis=1)

                atr_x = tr.rolling(window=p, min_periods=p).mean()
                plus_di = 100 * (plus_dm.ewm(span=p, adjust=False).mean() / atr_x)
                minus_di = 100 * (minus_dm.ewm(span=p, adjust=False).mean() / atr_x)
                dx = (
                    abs(plus_di - minus_di) / (plus_di + minus_di).replace(0, np.nan)
                ) * 100
                adx = dx.ewm(span=p, adjust=False, min_periods=p).mean().iloc[-1]
                return float(adx) if np.isfinite(adx) else float("nan")
            except Exception:
                return float("nan")

        cfg_r = out["meta"]["range"]
        rsi_period = cfg_r["rsi_period"]
        ema_win = cfg_r["ema_slope_window"]
        adx_p = cfg_r["adx_period"]
        rsi_width_win = cfg_r["rsi_width_window"]

        # Composantes
        width_now = float(width.iloc[-1]) if np.isfinite(width.iloc[-1]) else np.nan
        if np.isfinite(width_now) and len(w_non_na) >= 10:
            rank = float((w_non_na <= width_now).mean())  # 0..1
            comp_bandwidth = 1.0 - rank  # faible largeur => proche de 1
        else:
            comp_bandwidth = 0.5

        adx_val = _adx(df, adx_p)
        comp_adx = (
            max(0.0, min(1.0, 1.0 - (adx_val / 50.0))) if np.isfinite(adx_val) else 0.5
        )
        comp_slope = _ema_slope_norm(mid, ema_win)

        rsi = _rsi(series, rsi_period)
        rsi_win = rsi.tail(rsi_width_win).dropna()
        if len(rsi_win) >= max(5, rsi_period // 2):
            rsi_width = float(rsi_win.max() - rsi_win.min())
            comp_rsiw = max(0.0, min(1.0, 1.0 - (rsi_width / 30.0)))
        else:
            comp_rsiw = 0.5

        wts = cfg_r["weights"]
        range_score = (
            wts["bandwidth"] * comp_bandwidth
            + wts["adx"] * comp_adx
            + wts["ema_slope"] * comp_slope
            + wts["rsi_width"] * comp_rsiw
        )
        range_score = float(max(0.0, min(1.0, range_score)))
        out["range_score"] = round(range_score, 3)

        # Hysteresis + debounce
        key_state = f"_micro_range_state_{getattr(self, 'symbol', 'UNKNOWN')}"
        prev = getattr(self, key_state, {"is_range": False, "counter": 0})
        is_range_now = prev["is_range"]

        thr_in = cfg_r["threshold_in"]
        thr_out = cfg_r["threshold_out"]
        debounce = int(cfg_r["debounce_bars"])

        if not is_range_now:
            if range_score >= thr_in:
                prev["counter"] = prev["counter"] + 1
                if prev["counter"] >= debounce:
                    is_range_now = True
                    prev["counter"] = 0
            else:
                prev["counter"] = 0
        else:
            if range_score <= thr_out:
                prev["counter"] = prev["counter"] + 1
                if prev["counter"] >= debounce:
                    is_range_now = False
                    prev["counter"] = 0
            else:
                prev["counter"] = 0

        prev["is_range"] = is_range_now
        setattr(self, key_state, prev)
        out["is_range"] = bool(is_range_now)

        # Durée récente passée en "range" (approximation)
        try:
            recent_scores = []
            win_est = min(50, len(series))
            for _ in range(win_est):
                recent_scores.append(range_score)
            out["range_duration_bars"] = int(
                sum(1 for s in recent_scores if s >= thr_out)
            )
        except Exception:
            out["range_duration_bars"] = 0

        # =========================================================
        # ✅ SCORING REVERSIONS / BREAKOUTS
        # =========================================================
        mean_revert = 0.0
        breakout = 0.0

        if band_touch == "upper":
            mean_revert += 0.55
            mean_revert += 0.20 if out["is_squeeze"] else 0.05
            mean_revert += 0.10 if norm_mom <= 0 else -0.10
        elif band_touch == "lower":
            mean_revert += 0.55
            mean_revert += 0.20 if out["is_squeeze"] else 0.05
            mean_revert += 0.10 if norm_mom >= 0 else -0.10

        if outside_upper:
            breakout += 0.60
            breakout += 0.20 if out["is_expansion"] else 0.05
            breakout += 0.10 if norm_mom > 0 else -0.05
        if outside_lower:
            breakout += 0.60
            breakout += 0.20 if out["is_expansion"] else 0.05
            breakout += 0.10 if norm_mom < 0 else -0.05

        if out["atr_pips"] is not None:
            if out["atr_pips"] < 0.15:
                breakout *= 0.7
            elif out["atr_pips"] > 0.8:
                breakout *= 1.05
                mean_revert *= 0.95

        mean_revert = max(0.0, min(1.0, mean_revert))
        breakout = max(0.0, min(1.0, breakout))

        out["mean_revert_score"] = round(mean_revert, 3)
        out["breakout_score"] = round(breakout, 3)

        # =========================================================
        # ✅ LOGIQUE D'ENTRÉE "MÉDIANE" + GATE
        # =========================================================
        half_band = (bb_upper - bb_lower) / 2.0
        pos = (price - bb_mid) / (half_band if half_band != 0 else np.nan)
        pos = float(pos) if np.isfinite(pos) else 0.0
        out["half_band_pips"] = (
            _to_pips(half_band) if half_band and np.isfinite(half_band) else None
        )
        out["mid_distance_ratio"] = abs(pos) if np.isfinite(pos) else 0.0

        mid_cfg = out["meta"]["mid_entry"]
        pos_min = mid_cfg["pos_band_min"]
        pos_max = mid_cfg["pos_band_max"]
        base_thr = mid_cfg["base_threshold"]

        mid_entry = None
        mid_score = 0.0
        entry_gate_ok = False

        if out["is_range"] and in_band and np.isfinite(pos):
            # Conditions d'idée d'entrée
            bandwidth_comp_bonus = comp_bandwidth
            if (-pos_max <= pos <= -pos_min) and (norm_mom > mid_cfg["mom_norm_min"]):
                mid_score = (
                    0.45
                    + 0.25 * bandwidth_comp_bonus
                    + 0.15 * max(0.0, min(1.0, range_score))
                )
                mid_entry = "buy"
            elif (pos_min <= pos <= pos_max) and (norm_mom < -mid_cfg["mom_norm_min"]):
                mid_score = (
                    0.45
                    + 0.25 * bandwidth_comp_bonus
                    + 0.15 * max(0.0, min(1.0, range_score))
                )
                mid_entry = "sell"

            # Pénalité ATR très faible
            if out["atr_pips"] is not None and out["atr_pips"] < 0.12:
                mid_score *= 0.8

            mid_score = max(0.0, min(1.0, mid_score))
            if mid_score < base_thr:
                mid_entry, mid_score = None, 0.0

            # ✅ Gate strict: distance mini à la médiane (évite les entrées "au milieu")
            if mid_entry is not None and (abs(pos) >= pos_min):
                entry_gate_ok = True

        out["mid_entry"] = mid_entry
        out["mid_entry_score"] = round(float(mid_score), 3)
        out["entry_gate_ok"] = bool(entry_gate_ok)

        # =========================================================
        # ✅ Signal final (mode katana conservé)
        # =========================================================
        signal = "neutral"
        if mode == "katana":
            if outside_upper and breakout >= 0.55:
                signal = "buy_breakout"
            elif outside_lower and breakout >= 0.55:
                signal = "sell_breakout"
            elif band_touch == "upper" and mean_revert >= 0.55:
                signal = "sell_revert"
            elif band_touch == "lower" and mean_revert >= 0.55:
                signal = "buy_revert"
            else:
                signal = "neutral"
        else:
            if (breakout - mean_revert) >= 0.15:
                signal = (
                    "buy_breakout"
                    if (out["z_band"] is not None and out["z_band"] > 0)
                    else "sell_breakout"
                )
            elif (mean_revert - breakout) >= 0.15:
                signal = (
                    "sell_revert"
                    if (out["z_band"] is not None and out["z_band"] > 0)
                    else "buy_revert"
                )
            else:
                signal = "neutral"

        out["signal"] = signal
        out["ok"] = True
        return out

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
        Détecteur *complémentaire* de micro-phase sur M1 (zéro gating).
        Utilise un mini-window pour repérer une compression + petite impulsion.
        Retourne un paquet informatif et des suggestions TPSL serrées.
        """
        try:
            if df_m1 is None or len(df_m1) < 60:
                return {
                    "micro_phase": False,
                    "quality": 0.0,
                    "direction": "NEUTRAL",
                    "confidence_boost": 0.0,
                    "sl_pips_suggestion": None,
                    "tp_pips_suggestion": None,
                    "diagnostics": {"reason": "not_enough_bars"},
                }

            p = params or {}
            # fenêtres courtes par défaut (micro)
            w_core = int(p.get("window_core", 20))  # cœur de range
            w_env = int(p.get("window_env", 60))  # environnement pour normaliser
            k_range = float(
                p.get("max_range_pips", 8.0)
            )  # range max pour compter "micro"
            k_imp = float(
                p.get("min_impulse_pips", 3.0)
            )  # impulsion min post-compression
            max_spread_p = float(p.get("max_spread_pips", 2.0))
            boost = float(p.get("confidence_boost", 0.08))
            tp_sl = float(p.get("tp_over_sl", 1.2))  # tp = 1.2 * sl
            sl_floor = float(p.get("sl_min_pips", 5.0))
            sl_cap = float(p.get("sl_max_pips", 10.0))

            last = df_m1.iloc[-1]
            price = float(last["close"])
            pip_size = 0.01 if price > 10 else 0.0001

            try:
                spread_pips = (
                    float(df_m1.get("spread_points", pd.Series([0])).iloc[-1]) / 10.0
                )
            except Exception:
                spread_pips = 0.0

            core = df_m1.tail(w_core)
            env = df_m1.tail(w_env)

            core_high = float(core["high"].max())
            core_low = float(core["low"].min())
            core_range_price = core_high - core_low
            core_range_pips = core_range_price / pip_size

            env_high = float(env["high"].max())
            env_low = float(env["low"].min())
            env_range_pips = (
                (env_high - env_low) / pip_size
                if (env_high > env_low)
                else core_range_pips
            )

            compressed = (core_range_pips <= k_range) and (
                core_range_pips <= 0.35 * env_range_pips
            )

            recent = df_m1.tail(3)
            recent_move_up = (
                float(recent["close"].iloc[-1]) - float(recent["open"].iloc[0])
            ) / pip_size
            recent_move_abs = abs(recent_move_up)
            impulse_ok = recent_move_abs >= k_imp

            direction = "NEUTRAL"
            if impulse_ok:
                direction = "BUY" if recent_move_up > 0 else "SELL"

            spread_penalty = 0.0
            if spread_pips > max_spread_p:
                spread_penalty = min(0.4, (spread_pips - max_spread_p) * 0.1)

            quality = 0.0
            if compressed and impulse_ok:
                q_range = max(0.0, 1.0 - (core_range_pips / max(k_range, 1e-6)))
                q_imp = max(0.0, min(1.0, recent_move_abs / max(k_imp * 2.0, 1e-6)))
                quality = 0.6 * q_range + 0.4 * q_imp
                quality = max(0.0, min(1.0, quality - spread_penalty))

            micro = bool(quality >= 0.35)
            conf_boost = boost if micro else 0.0

            base_sl = max(
                sl_floor, min(sl_cap, 0.5 * core_range_pips + 1.0 * spread_pips)
            )
            sl_pips = float(base_sl)
            tp_pips = float(max(sl_floor, min(sl_cap * tp_sl, sl_pips * tp_sl)))

            return {
                "micro_phase": micro,
                "quality": round(quality, 3),
                "direction": direction,
                "confidence_boost": round(conf_boost, 3),
                "sl_pips_suggestion": round(sl_pips, 2),
                "tp_pips_suggestion": round(tp_pips, 2),
                "diagnostics": {
                    "core_range_pips": round(core_range_pips, 2),
                    "env_range_pips": round(env_range_pips, 2),
                    "recent_move_pips": round(recent_move_abs, 2),
                    "spread_pips": round(spread_pips, 2),
                    "compressed": bool(compressed),
                    "impulse_ok": bool(impulse_ok),
                },
            }
        except Exception as e:
            # jamais bloquant
            return {
                "micro_phase": False,
                "quality": 0.0,
                "direction": "NEUTRAL",
                "confidence_boost": 0.0,
                "sl_pips_suggestion": None,
                "tp_pips_suggestion": None,
                "diagnostics": {"error": str(e)},
            }

    def determine_optimized_phase(self, row: Dict[str, Any]) -> str:
        """Classification de phase basée sur les 4 indicateurs core (+ lecture Bollinger si dispo, non bloquante)."""
        regime = str(row.get("regime", "unknown"))

        # --- Vars Bollinger (optionnelles, robustes si absentes) ---
        boll_signal = row.get(
            "boll_signal"
        )  # "buy_revert" | "sell_revert" | "buy_breakout" | "sell_breakout" | None
        boll_breakout = float(row.get("boll_breakout_score", 0.0) or 0.0)
        boll_revert = float(row.get("boll_mean_revert_score", 0.0) or 0.0)
        boll_squeeze = bool(row.get("boll_is_squeeze", False))
        boll_expansion = bool(row.get("boll_is_expansion", False))

        # --- Institutional trending regimes ---
        if "trending_institutional" in regime:
            if row.get("fvg_ob_confluence", False):
                return "institutional_setup_premium"
            elif row.get("ob_detected", False):
                return "institutional_setup"
            elif "bull" in regime:
                if boll_signal in ("buy_breakout",) and boll_breakout >= 0.6:
                    return "volatility_breakout"
                return "trending_institutional_bull"
            else:
                if boll_signal in ("sell_breakout",) and boll_breakout >= 0.6:
                    return "volatility_breakout"
                return "trending_institutional_bear"

        # --- Ranges (accumulation/distribution) ---
        elif "range_accumulation" in regime:
            if boll_signal == "buy_revert" and (boll_revert >= 0.55 or boll_squeeze):
                return "range_accumulation"
            if row.get("high_quality_ob", False):
                return "accumulation_zone"
            return "range_accumulation"

        elif "range_distribution" in regime:
            if boll_signal == "sell_revert" and (boll_revert >= 0.55 or boll_squeeze):
                return "range_distribution"
            if row.get("confirmed_structure_break", False) or (
                boll_signal in ("buy_breakout", "sell_breakout")
                and boll_breakout >= 0.6
            ):
                return "distribution_breakout"
            return "range_distribution"

        # --- High vol / chaos : privilégier breakouts Bollinger si expansion ---
        elif "high_volatility" in regime:
            if row.get("bos_mss_detected", False):
                return "volatility_breakout"
            if boll_expansion and boll_breakout >= 0.6:
                return "volatility_breakout"
            return "high_volatility_chaos"

        # --- Low vol / compression : attendre expansion, sinon compression pure ---
        elif "low_volatility" in regime:
            if boll_expansion and boll_breakout >= 0.6:
                return "volatility_breakout"
            return "low_volatility_compression"

        # --- Fallback divers hors régimes majeurs ---
        else:
            if row.get("institutional_setup", False):
                return "smc_setup"
            elif row.get("fvg_detected", False):
                return "fvg_opportunity"
            if boll_signal in ("buy_revert", "sell_revert") and boll_revert >= 0.6:
                return (
                    "range_accumulation"
                    if boll_signal == "buy_revert"
                    else "range_distribution"
                )
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
