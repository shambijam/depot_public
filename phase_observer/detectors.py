
#phase_observer/detectors.py
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
    ob_config = self.config_manager.get("phase_detection_defaults.order_block_ml_settings", {}) or {}
    enable_ml = bool(ob_config.get("enable_ml_scoring", True))
    confluence_config = ob_config.get("confluence_requirements", {}) or {}
    impulse_weights = ob_config.get("impulse_strength_weights", {}) or {}

    # Paramètres de base
    impulse_threshold = float(self.config_manager.get("phase_detection_defaults.impulse_threshold", 0.0005))

    # Pré-calcul des features pour ML
    df = df.copy()
    df["candle_move"] = df["close"] - df["open"]
    df["candle_size"] = (df["high"] - df["low"]).replace(0, np.nan)
    df["body_ratio"] = (df["candle_move"].abs() / df["candle_size"]).fillna(0.0)

    vol_ma = df["tick_volume"].rolling(window=20, min_periods=1).mean().replace(0, np.nan)
    df["volume_ma"] = vol_ma
    df["volume_ratio"] = (df["tick_volume"] / vol_ma).fillna(1.0)

    # Identification des OB potentiels
    bullish_ob_mask = (df["candle_move"] > impulse_threshold) & (df["candle_move"].shift(1) < 0)
    bearish_ob_mask = (df["candle_move"] < -impulse_threshold) & (df["candle_move"].shift(1) > 0)
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
            price_movement_strength = float(abs(impulse_candle["candle_move"]) / max(impulse_threshold, 1e-12))
            volume_spike_strength = float(impulse_candle["volume_ratio"])
            body_ratio_strength = float(impulse_candle["body_ratio"])

            # Time compression (placeholder)
            time_compression = 1.0

            # Calcul score impulse pondéré
            impulse_score = (
                price_movement_strength * float(impulse_weights.get("price_movement", 0.4))
                + volume_spike_strength * float(impulse_weights.get("volume_spike", 0.3))
                + time_compression * float(impulse_weights.get("time_compression", 0.3))
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
                (ob_candle.name in swing_highs.index) or (ob_candle.name in swing_lows.index)
            )
            if extreme_confluence:
                confluence_score += 0.30
            confluence_details["extreme_confluence"] = extreme_confluence

            # Volume Confirmation
            volume_confirmation = True
            if confluence_config.get("require_volume_confirmation", True):
                volume_confirmation = volume_spike_strength > 1.2  # 20% au-dessus de la moyenne
                if volume_confirmation:
                    confluence_score += 0.20
            confluence_details["volume_confirmation"] = volume_confirmation

            # Trend Alignment
            ob_is_bullish = bool(bullish_ob_mask.iloc[pos])
            trend_alignment = True
            if confluence_config.get("require_trend_alignment", True):
                current_trend = df["trend"].iloc[pos] if "trend" in df.columns else "neutral"
                if (ob_is_bullish and current_trend == "bullish") or ((not ob_is_bullish) and current_trend == "bearish"):
                    trend_alignment = True
                    confluence_score += 0.15
                else:
                    trend_alignment = False
                # HTF alignment bonus
                if htf_trend and ((ob_is_bullish and htf_trend == "bullish") or ((not ob_is_bullish) and htf_trend == "bearish")):
                    confluence_score += 0.10
            confluence_details["trend_alignment"] = trend_alignment

            # 3. Mitigation Analysis
            unmitigated = True
            future_candles = df.iloc[pos + 1 :]
            if not future_candles.empty:
                mitigated = future_candles[
                    (future_candles["high"] >= ob_zone[0]) & (future_candles["low"] <= ob_zone[1])
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
            min_confluence = float(confluence_config.get("min_confluence_score", 0.6))

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
                    "formation_quality": ("high" if ml_score > 0.8 else "medium" if ml_score > 0.6 else "low"),
                }

        except Exception as e:
            self.logger.warning(f"Erreur processing OB à l'index {ob_timestamp}: {e}", exc_info=False)
            continue

    # Performance logging
    valid_obs = [r for r in results if r is not None]
    if valid_obs:
        avg_ml_score = float(np.mean([ob["ml_score"] for ob in valid_obs]))
        high_quality = len([ob for ob in valid_obs if ob["formation_quality"] == "high"])
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
    fvg_config = self.config_manager.get("phase_detection_defaults.fvg_enhanced_settings", {}) or {}
    min_gap_magnitude = float(fvg_config.get("min_gap_magnitude_percent", 0.15)) / 100.0
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

    bullish_fvg_condition[valid_indices] = low_p0[valid_indices] > high_p2[valid_indices]
    bearish_fvg_condition[valid_indices] = high_p0[valid_indices] < low_p2[valid_indices]

    results: List[Optional[Dict[str, Any]]] = []
    active_gaps: List[Dict[str, Any]] = []  # Tracking des gaps actifs pour remplissage

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
                quality_score = min(1.0, magnitude_percent / max(min_gap_magnitude * 2.0, 1e-12))
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
                quality_score = min(1.0, magnitude_percent / max(min_gap_magnitude * 2.0, 1e-12))
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
        self.logger.debug(f"FVG Enhanced: {len(valid_gaps)} gaps détectés, qualité moyenne: {avg_quality:.3f}")

    return results


def detect_bos_mss_enhanced(self, df: pd.DataFrame) -> List[Optional[Dict[str, Any]]]:
    """
    🎯 BOS/MSS Enhanced - Avec confirmation volume et momentum

    Améliorations:
    - Confirmation volume obligatoire
    - Validation momentum
    - Distinction BOS vs MSS plus précise
    - Filtrage des faux breakouts
    """
    self.logger.debug("Détection BOS/MSS Enhanced avec confirmations...")

    if df is None or df.empty:
        return []

    # Configuration
    bos_config = self.config_manager.get("phase_detection_defaults.bos_mss_enhanced_settings", {}) or {}
    volume_config = bos_config.get("volume_confirmation", {}) or {}
    momentum_config = bos_config.get("momentum_confirmation", {}) or {}
    structure_config = bos_config.get("structure_validation", {}) or {}

    # Paramètres de confirmation
    enable_volume_conf = bool(volume_config.get("enable", True))
    volume_multiplier = float(volume_config.get("volume_multiplier_threshold", 1.5))
    volume_lookback = int(volume_config.get("lookback_period", 20))

    enable_momentum_conf = bool(momentum_config.get("enable", True))
    min_momentum = float(momentum_config.get("min_momentum_threshold", 0.0003))

    min_break_distance = float(structure_config.get("min_break_distance", 0.0002))
    require_close_beyond = bool(structure_config.get("require_close_beyond", True))

    # S'assurer que la tendance est calculée
    if "trend" not in df.columns:
        df = df.copy()
        df["trend"] = _get_trend(self, df)

    # Swing points adaptatifs
    swing_highs, swing_lows = _get_adaptive_swing_points(self, df)
    df["last_swing_high"] = swing_highs.reindex(df.index).ffill()
    df["last_swing_low"] = swing_lows.reindex(df.index).ffill()

    # Calcul des moyennes mobiles de volume
    vol_ma = df["tick_volume"].rolling(window=volume_lookback, min_periods=1).mean().replace(0, np.nan)
    df["volume_ma"] = vol_ma
    df["volume_ratio"] = (df["tick_volume"] / vol_ma).fillna(0.0)

    # === CONDITIONS DE BASE ===
    # Breakout haussier: clôture au-dessus du dernier swing high
    bullish_break_basic = df["close"] > df["last_swing_high"].shift(1)
    # Breakout baissier: clôture en dessous du dernier swing low
    bearish_break_basic = df["close"] < df["last_swing_low"].shift(1)

    # === CONFIRMATIONS VOLUME ===
    volume_confirmation = pd.Series(True, index=df.index)  # Default True si désactivé
    if enable_volume_conf:
        volume_confirmation = (df["volume_ratio"] > volume_multiplier).fillna(False)

    # === CONFIRMATIONS MOMENTUM ===
    momentum_confirmation = pd.Series(True, index=df.index)  # Default True si désactivé
    if enable_momentum_conf:
        price_change = df["close"].pct_change().abs()
        momentum_confirmation = (price_change > min_momentum).fillna(False)

    # === FILTRAGE DISTANCE MINIMALE ===
    def validate_break_distance(row: pd.Series, break_type: str) -> bool:
        """Valide que la cassure est suffisamment significative (en proportion du niveau cassé)."""
        try:
            if break_type == "bullish":
                last_high = row["last_swing_high"]
                if pd.isna(last_high):
                    return False
                distance = (row["close"] - last_high) / max(last_high, 1e-12)
                return bool(distance >= min_break_distance)
            else:  # bearish
                last_low = row["last_swing_low"]
                if pd.isna(last_low):
                    return False
                distance = (last_low - row["close"]) / max(last_low, 1e-12)
                return bool(distance >= min_break_distance)
        except Exception:
            return False

    # === CLASSIFICATION BOS vs MSS ===
    previous_trend = df["trend"].shift(1)

    # Conditions finales avec toutes les confirmations
    bullish_break_confirmed = (
        bullish_break_basic
        & volume_confirmation
        & momentum_confirmation
        & df.apply(lambda row: validate_break_distance(row, "bullish"), axis=1)
    )

    bearish_break_confirmed = (
        bearish_break_basic
        & volume_confirmation
        & momentum_confirmation
        & df.apply(lambda row: validate_break_distance(row, "bearish"), axis=1)
    )

    # Classification intelligente BOS vs MSS
    bullish_bos = (previous_trend == "bullish") & bullish_break_confirmed
    bearish_bos = (previous_trend == "bearish") & bearish_break_confirmed
    bullish_mss = (previous_trend == "bearish") & bullish_break_confirmed
    bearish_mss = (previous_trend == "bullish") & bearish_break_confirmed

    # === CONSTRUCTION DES RÉSULTATS ===
    results: List[Optional[Dict[str, Any]]] = []

    # Pré-calcul momentum (pour log)
    price_change = df["close"].pct_change()

    for i in range(len(df)):
        info = None

        vol_ratio_i = float(df["volume_ratio"].iloc[i]) if pd.notna(df["volume_ratio"].iloc[i]) else 0.0
        momentum_i = float(price_change.iloc[i]) if pd.notna(price_change.iloc[i]) else 0.0

        if bool(bullish_bos.iloc[i]):
            info = {
                "type": "bullish_bos",
                "level_broken": float(df["last_swing_high"].shift(1).iloc[i]),
                "confirmation_score": (min(1.0, vol_ratio_i / max(volume_multiplier, 1e-12)) if enable_volume_conf else 1.0),
                "volume_ratio": round(vol_ratio_i, 2),
                "momentum": round(abs(momentum_i), 4),
                "structure_type": "continuation",
                "quality": ("high" if vol_ratio_i > volume_multiplier * 1.5 else "medium"),
            }
        elif bool(bearish_bos.iloc[i]):
            info = {
                "type": "bearish_bos",
                "level_broken": float(df["last_swing_low"].shift(1).iloc[i]),
                "confirmation_score": (min(1.0, vol_ratio_i / max(volume_multiplier, 1e-12)) if enable_volume_conf else 1.0),
                "volume_ratio": round(vol_ratio_i, 2),
                "momentum": round(abs(momentum_i), 4),
                "structure_type": "continuation",
                "quality": ("high" if vol_ratio_i > volume_multiplier * 1.5 else "medium"),
            }
        elif bool(bullish_mss.iloc[i]):
            info = {
                "type": "bullish_mss",
                "level_broken": float(df["last_swing_high"].shift(1).iloc[i]),
                "confirmation_score": (min(1.0, vol_ratio_i / max(volume_multiplier, 1e-12)) if enable_volume_conf else 1.0),
                "volume_ratio": round(vol_ratio_i, 2),
                "momentum": round(abs(momentum_i), 4),
                "structure_type": "reversal",
                "quality": ("high" if vol_ratio_i > volume_multiplier * 1.5 else "medium"),
            }
        elif bool(bearish_mss.iloc[i]):
            info = {
                "type": "bearish_mss",
                "level_broken": float(df["last_swing_low"].shift(1).iloc[i]),
                "confirmation_score": (min(1.0, vol_ratio_i / max(volume_multiplier, 1e-12)) if enable_volume_conf else 1.0),
                "volume_ratio": round(vol_ratio_i, 2),
                "momentum": round(abs(momentum_i), 4),
                "structure_type": "reversal",
                "quality": ("high" if vol_ratio_i > volume_multiplier * 1.5 else "medium"),
            }

        results.append(info)

    # Logging de performance
    valid_breaks = [r for r in results if r is not None]
    if valid_breaks:
        bos_count = len([r for r in valid_breaks if "bos" in r["type"]])
        mss_count = len([r for r in valid_breaks if "mss" in r["type"]])
        high_quality = len([r for r in valid_breaks if r["quality"] == "high"])

        self.logger.debug(
            f"BOS/MSS Enhanced: {len(valid_breaks)} cassures détectées "
            f"(BOS: {bos_count}, MSS: {mss_count}, haute qualité: {high_quality})"
        )

    return results


def detect_market_regime(self, df: pd.DataFrame) -> pd.Series:
    """
    🏛️ Market Regime Detection - Remplace la détection de tendance basique

    Régimes détectés:
    - trending_institutional_bull/bear | trending_retail_bull/bear
    - range_accumulation/distribution | range_institutional | range_retail
    - high_volatility_chaos | low_volatility_compression | transitional

    Basé sur:
    - ADX pour force de tendance
    - Volume Profile pour activité institutionnelle
    - Volatilité Garman-Klass
    """
    self.logger.debug("Détection du régime de marché sophistiquée...")

    if df is None or df.empty:
        return pd.Series(dtype=object)

    regime_config = self.config_manager.get("phase_detection_defaults.regime_detection_settings", {}) or {}
    adx_config = regime_config.get("adx_settings", {}) or {}
    vol_config = regime_config.get("volatility_regimes", {}) or {}
    volume_config = regime_config.get("volume_profile", {}) or {}

    # === 1. CALCUL ADX (AVERAGE DIRECTIONAL INDEX) ===
    adx_period = int(adx_config.get("period", 14))
    trending_threshold = float(adx_config.get("trending_threshold", 25))
    ranging_threshold = float(adx_config.get("ranging_threshold", 20))

    def calculate_adx(_df: pd.DataFrame, period: int = 14):
        """Calcul ADX pour mesurer la force de la tendance."""
        high = _df["high"]
        low = _df["low"]
        close = _df["close"]

        tr1 = high - low
        tr2 = (high - close.shift()).abs()
        tr3 = (low - close.shift()).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

        up_move = high.diff()
        down_move = -low.diff()
        dm_plus = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
        dm_minus = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

        atr = tr.rolling(window=period, min_periods=1).mean().replace(0, np.nan)
        dm_plus_smooth = pd.Series(dm_plus, index=_df.index).rolling(window=period, min_periods=1).mean()
        dm_minus_smooth = pd.Series(dm_minus, index=_df.index).rolling(window=period, min_periods=1).mean()

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

    # Percentile dynamique sur fenêtre glissante
    vol_percentiles = volatility.rolling(window=max(100, vol_period * 5), min_periods=1).apply(
        lambda x: (x <= x.iloc[-1]).mean() * 100.0, raw=False
    )

    # === 3. VOLUME PROFILE INSTITUTIONNEL ===
    enable_institutional = bool(volume_config.get("enable_institutional_detection", True))
    volume_ma_period = int(volume_config.get("volume_ma_period", 20))
    institutional_threshold = float(volume_config.get("institutional_threshold", 1.8))

    institutional_activity = pd.Series(False, index=df.index)
    if enable_institutional and "tick_volume" in df.columns:
        volume_ma = df["tick_volume"].rolling(window=volume_ma_period, min_periods=1).mean().replace(0, np.nan)
        volume_ratio = (df["tick_volume"] / volume_ma).fillna(0.0)
        institutional_activity = volume_ratio > institutional_threshold

    # === 4. DÉTERMINATION DU RÉGIME ===
    regimes = pd.Series("unknown", index=df.index, dtype=object)

    for i in range(len(df)):
        current_adx = float(adx.iloc[i]) if pd.notna(adx.iloc[i]) else 0.0
        current_di_plus = float(di_plus.iloc[i]) if pd.notna(di_plus.iloc[i]) else 0.0
        current_di_minus = float(di_minus.iloc[i]) if pd.notna(di_minus.iloc[i]) else 0.0
        current_vol_percentile = float(vol_percentiles.iloc[i]) if pd.notna(vol_percentiles.iloc[i]) else 50.0
        is_institutional = bool(institutional_activity.iloc[i])

        if current_adx > trending_threshold:
            if current_di_plus > current_di_minus:
                regimes.iloc[i] = "trending_institutional_bull" if is_institutional else "trending_retail_bull"
            else:
                regimes.iloc[i] = "trending_institutional_bear" if is_institutional else "trending_retail_bear"

        elif current_adx < ranging_threshold:
            if is_institutional:
                recent_closes = df["close"].iloc[max(0, i - 10) : i + 1]
                if len(recent_closes) > 5:
                    regimes.iloc[i] = "range_accumulation" if (recent_closes.iloc[-1] > recent_closes.mean()) else "range_distribution"
                else:
                    regimes.iloc[i] = "range_institutional"
            else:
                regimes.iloc[i] = "range_retail"
        else:
            if current_vol_percentile >= high_vol_percentile:
                regimes.iloc[i] = "high_volatility_chaos"
            elif current_vol_percentile <= low_vol_percentile:
                regimes.iloc[i] = "low_volatility_compression"
            else:
                regimes.iloc[i] = "transitional"

    # === 5. CALCUL MÉTRIQUES DE QUALITÉ DU RÉGIME ===
    def calculate_regime_strength(regime_series: pd.Series, adx_series: pd.Series) -> pd.Series:
        """Calcule la force/confiance du régime détecté."""
        regime_strength = pd.Series(0.5, index=regime_series.index, dtype=float)  # Base 50%

        for i in range(len(regime_series)):
            regime = str(regime_series.iloc[i])
            adx_val = float(adx_series.iloc[i]) if pd.notna(adx_series.iloc[i]) else 0.0

            if "trending" in regime:
                if adx_val > 40:
                    regime_strength.iloc[i] = 0.9
                elif adx_val > 30:
                    regime_strength.iloc[i] = 0.8
                elif adx_val > 25:
                    regime_strength.iloc[i] = 0.7
                else:
                    regime_strength.iloc[i] = 0.6
            elif "range" in regime:
                if adx_val < 15:
                    regime_strength.iloc[i] = 0.9
                elif adx_val < 20:
                    regime_strength.iloc[i] = 0.8
                else:
                    regime_strength.iloc[i] = 0.6
            elif "volatility" in regime:
                regime_strength.iloc[i] = 0.8

        return regime_strength

    regime_strength = calculate_regime_strength(regimes, adx)

    # Ajout au DataFrame pour usage ultérieur
    df["regime"] = regimes
    df["regime_strength"] = regime_strength
    df["adx"] = adx
    df["volatility_percentile"] = vol_percentiles
    df["institutional_activity"] = institutional_activity

    # === 6. LOGGING DE PERFORMANCE ===
    if len(regimes) > 0:
        regime_counts = regimes.value_counts()
        dominant_regime = regime_counts.index[0] if len(regime_counts) > 0 else "unknown"
        avg_strength = float(regime_strength.mean())
        self.logger.debug(f"Régime de marché: {dominant_regime} (force moyenne: {avg_strength:.2f})")
        self.logger.debug(f"Distribution régimes: {dict(regime_counts.head(3))}")

    return regimes


def detect_micro_phase_m1(self, df_m1: pd.DataFrame, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Détecteur *complémentaire* de micro-phase sur M1 (zéro gating).
    Utilise un mini-window pour repérer une compression + petite impulsion.
    Retourne un paquet informatif et des suggestions TPSL serrées.
    """
    try:
        if df_m1 is None or len(df_m1) < 60:
            return {
                "micro_phase": False, "quality": 0.0, "direction": "NEUTRAL",
                "confidence_boost": 0.0, "sl_pips_suggestion": None, "tp_pips_suggestion": None,
                "diagnostics": {"reason": "not_enough_bars"}
            }

        p = (params or {})
        # fenêtres courtes par défaut (micro)
        w_core   = int(p.get("window_core", 20))        # cœur de range
        w_env    = int(p.get("window_env", 60))         # environnement pour normaliser
        k_range  = float(p.get("max_range_pips", 8.0))  # range max pour compter "micro"
        k_imp    = float(p.get("min_impulse_pips", 3.0))# impulsion min post-compression
        max_spread_p = float(p.get("max_spread_pips", 2.0))
        boost    = float(p.get("confidence_boost", 0.08))
        tp_sl    = float(p.get("tp_over_sl", 1.2))      # tp = 1.2 * sl
        sl_floor = float(p.get("sl_min_pips", 5.0))
        sl_cap   = float(p.get("sl_max_pips", 10.0))

        last = df_m1.iloc[-1]
        price = float(last["close"])
        pip_size = 0.01 if price > 10 else 0.0001

        try:
            spread_pips = float(df_m1.get("spread_points", pd.Series([0])).iloc[-1]) / 10.0
        except Exception:
            spread_pips = 0.0

        core = df_m1.tail(w_core)
        env  = df_m1.tail(w_env)

        core_high = float(core["high"].max())
        core_low  = float(core["low"].min())
        core_range_price = core_high - core_low
        core_range_pips  = core_range_price / pip_size

        env_high = float(env["high"].max())
        env_low  = float(env["low"].min())
        env_range_pips = (env_high - env_low) / pip_size if (env_high > env_low) else core_range_pips

        compressed = (core_range_pips <= k_range) and (core_range_pips <= 0.35 * env_range_pips)

        recent = df_m1.tail(3)
        recent_move_up   = (float(recent["close"].iloc[-1]) - float(recent["open"].iloc[0])) / pip_size
        recent_move_abs  = abs(recent_move_up)
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
            q_imp   = max(0.0, min(1.0, recent_move_abs / max(k_imp * 2.0, 1e-6)))
            quality = 0.6 * q_range + 0.4 * q_imp
            quality = max(0.0, min(1.0, quality - spread_penalty))

        micro = bool(quality >= 0.35)
        conf_boost = boost if micro else 0.0

        base_sl = max(sl_floor, min(sl_cap, 0.5 * core_range_pips + 1.0 * spread_pips))
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
                "impulse_ok": bool(impulse_ok)
            }
        }
    except Exception as e:
        # jamais bloquant
        return {
            "micro_phase": False, "quality": 0.0, "direction": "NEUTRAL",
            "confidence_boost": 0.0, "sl_pips_suggestion": None, "tp_pips_suggestion": None,
            "diagnostics": {"error": str(e)}
        }


def determine_optimized_phase(self, row: Dict[str, Any]) -> str:
    """Classification de phase basée sur les 4 indicateurs core (+ lecture Bollinger si dispo, non bloquante)."""
    regime = str(row.get("regime", "unknown"))

    # --- Vars Bollinger (optionnelles, robustes si absentes) ---
    boll_signal = row.get("boll_signal")  # "buy_revert" | "sell_revert" | "buy_breakout" | "sell_breakout" | None
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
            boll_signal in ("buy_breakout", "sell_breakout") and boll_breakout >= 0.6
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
            return "range_accumulation" if boll_signal == "buy_revert" else "range_distribution"
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
