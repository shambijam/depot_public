# phase_observer/orchestrator.py
# --- MUST BE FIRST LINE ---
from __future__ import annotations

# Stdlib
import json
import logging
import math
import time
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, Union

# Third-party
import numpy as np
import pandas as pd

# Local
from .types import (
    Direction,
    Phase,
    PhaseSignal,
    PhaseSnapshot,
    MarketFeatures,
    PhaseMemory,
)
from .validators import calculate_confidence_score  # si utilisé quelque part
from .features import (
    _clean_dataframe,
    detect_market_regime,
    detect_fvg_enhanced,
    detect_order_block_ml_enhanced,
    detect_bos_mss_enhanced,
    compute_bollinger_microphase_signals,
)
from .utils import (
    _extract_m1_break_direction,
    _pick_sl_from_structure,
    _pick_tp_from_nearest_liquidity,
    _get_nearest_liquidity_level,
    _build_enhanced_signals,
    _detect_tf_divergences,
    _calculate_advanced_confluence,
)

# Alias pratique si ton code utilise `UTC`
UTC = timezone.utc


class PhaseObserver:
    # annotations au niveau classe (ok pour Pylance)
    signal_weights: Dict[str, float]
    confluence_bonus: Dict[str, float]

    def __init__(self, config_manager: Optional[ConfigManager] = None):
        """
        Initialise le PhaseObserver avec les paramètres de configuration.
        """
        self.config_manager = config_manager
        self.logger = logging.getLogger(__name__)

        # === Bind des helpers module-level en méthodes d'instance ===
        # features.py
        self._clean_dataframe = _clean_dataframe.__get__(self)
        self.detect_market_regime = detect_market_regime.__get__(self)
        self.detect_fvg_enhanced = detect_fvg_enhanced.__get__(self)
        self.detect_order_block_ml_enhanced = detect_order_block_ml_enhanced.__get__(self)
        self.detect_bos_mss_enhanced = detect_bos_mss_enhanced.__get__(self)
        self.compute_bollinger_microphase_signals = compute_bollinger_microphase_signals.__get__(self)
        # utils.py
        self._extract_m1_break_direction = _extract_m1_break_direction.__get__(self)
        self._pick_sl_from_structure = _pick_sl_from_structure.__get__(self)
        self._pick_tp_from_nearest_liquidity = _pick_tp_from_nearest_liquidity.__get__(self)
        self._get_nearest_liquidity_level = _get_nearest_liquidity_level.__get__(self)
        self._build_enhanced_signals = _build_enhanced_signals.__get__(self)
        self._detect_tf_divergences = _detect_tf_divergences.__get__(self)
        self._calculate_advanced_confluence = _calculate_advanced_confluence.__get__(self)

        # === Défauts ===
        self.lookback_window = 12
        self.volatility_threshold = 0.0001
        self.volume_zscore = 1.2
        self.impulse_threshold = 0.0002
        self.min_window_order_block = 4
        self.min_window_fvg = 3
        self.swing_point_order = 3
        self.eq_level_tolerance = 0.0001
        self.min_allowed_spread_for_liquid_check = 10
        self.min_volume_for_liquid_check = 5
        self.base_confidence = 0.25
        self.signal_weights = {}
        self.confluence_bonus = {}
        self.detect_fvg = True
        self.detect_order_block = True
        self.detect_bos_mss = True
        self.detect_liquidity_grab = True
        self.detect_eqh_eql = True
        self.detect_volume_anomaly = True

        # === Config optionnelle (override des défauts) ===
        if getattr(self, "config_manager", None):
            try:
                self.lookback_window = self.config_manager.get(
                    "core_parameters.lookback_window", self.lookback_window
                )
                self.volatility_threshold = self.config_manager.get(
                    "core_parameters.volatility_threshold", self.volatility_threshold
                )
                self.volume_zscore = self.config_manager.get(
                    "core_parameters.volume_zscore", self.volume_zscore
                )
                # ... étends si besoin
            except Exception as e:
                self.logger.warning(
                    f"Impossible de charger config: {e}. Utilisation des valeurs par défaut."
                )

        self.logger.info(
            f"PhaseObserver initialisé. Lookback window: {self.lookback_window}."
        )



    def _load_settings(self, overrides: Optional[Dict[str, Any]] = None):
        """
        Charge tous les paramètres depuis le ConfigManager de manière dynamique.
        Permet la surcharge de paramètres spécifiques via le dictionnaire 'overrides'.
        """
        self.logger.debug("Chargement des paramètres d'analyse pour PhaseObserver...")

        all_settings = {}
        try:
            if getattr(self, "config_manager", None):
                all_settings = (
                    self.config_manager.get("phase_detection_defaults", {}) or {}
                )
        except Exception as e:
            self.logger.warning(f"Lecture config défauts impossible: {e}")
            all_settings = {}

        # Appliquer les surcharges si fournies
        if overrides:
            self.logger.debug(f"Application de surcharges de paramètres : {overrides}")
            try:
                if hasattr(self.config_manager, "_merge_dicts"):
                    all_settings = self.config_manager._merge_dicts(
                        all_settings, overrides
                    )
                else:
                    all_settings.update(overrides)
                    self.logger.warning(
                        "ConfigManager n'a pas _merge_dicts. Surcharge avec update()."
                    )
            except Exception as e:
                self.logger.warning(f"Surcharge paramètres échouée: {e}")

        # Affecter tous les paramètres à l'instance
        for key, value in (all_settings or {}).items():
            try:
                setattr(self, key, value)
            except Exception:
                # on ignore silencieusement les clés non valides
                pass

        # S'assurer que les chemins de sortie sont des objets Path
        try:
            base_reports = "output/"
            base_logs = "logs/"
            if getattr(self, "config_manager", None):
                base_reports = self.config_manager.get("paths.reports", base_reports)
                base_logs = self.config_manager.get("paths.logs", base_logs)

            self.output_path = Path(base_reports)
            self.logs_dir = Path(base_logs)
            self.output_path.mkdir(parents=True, exist_ok=True)
            self.logs_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            self.logger.warning(f"Impossible de préparer les dossiers de sortie: {e}")

        self.logger.debug("Paramètres de PhaseObserver chargés et appliqués.")
        # TODO: Hot-reload si ConfigManager supporte des callbacks.

    def analyze(
        self, df: pd.DataFrame, asset_symbol: Optional[str] = None
    ) -> Optional[pd.DataFrame]:
        """
        🎯 PIPELINE D'ANALYSE OPTIMISÉ - 4 INDICATEURS CORE SEULEMENT

        Architecture Trading Desk:
        1. FVG Enhanced (magnitude + tracking)
        2. Order Blocks ML Enhanced (scoring sophistiqué)
        3. Adaptive Swing Points (régime-aware)
        4. Market Regime Detection (remplace trend basique)

        Performance target: 78%+ win rate, <50ms processing time
        """
        try:
            n_bars = 0 if df is None else len(df)
            self.logger.info(
                f"🚀 SNIPER_X Optimized Pipeline - Processing {n_bars} bars"
            )

            if df is None or df.empty:
                self.logger.error("DataFrame vide fourni à analyze()")
                return None

            # === PHASE 1: PRÉPARATION DONNÉES ===
            current_asset_symbol = asset_symbol or "UNKNOWN_ASSET"

            # NOTE: suppose que _clean_dataframe est bien résolu (même module/classe)
            df_an = self._clean_dataframe(df.copy())
            if df_an is None or df_an.empty:
                self.logger.error("Échec du nettoyage DataFrame")
                return None

            # Force colonnes prix en numérique
            for _col in ("close", "high", "low", "open"):
                if _col in df_an.columns:
                    df_an[_col] = pd.to_numeric(df_an[_col], errors="coerce")
            df_an.replace([np.inf, -np.inf], np.nan, inplace=True)
            df_an.dropna(
                subset=[c for c in ("close", "high", "low") if c in df_an.columns],
                how="any",
                inplace=True,
            )
            if len(df_an) < 5:
                self.logger.error("Trop peu de barres après nettoyage pour analyze()")
                return None

            # Initialisation colonnes requises
            required_columns = {
                "spread": 0.0,
                "point": 0.00001,
                "trade_tick_size": 0.00001,
                "trade_contract_size": 100000.0,
            }
            for col, default_val in required_columns.items():
                if col not in df_an.columns:
                    df_an[col] = float(default_val)
                    self.logger.warning(
                        f"Colonne '{col}' ajoutée avec valeur par défaut"
                    )
                else:
                    df_an[col] = pd.to_numeric(df_an[col], errors="coerce").fillna(
                        float(default_val)
                    )

            # === Volatilité en % ===
            try:
                if "close" in df_an.columns:
                    ret = df_an["close"].pct_change().fillna(0.0)
                    vol_pct = ret.abs().ewm(span=20, adjust=False).mean() * 100.0
                    vol_pct = (
                        vol_pct.replace([np.inf, -np.inf], 0.0)
                        .fillna(0.0)
                        .clip(lower=1e-6)
                    )
                    df_an["volatility_pct"] = vol_pct
                else:
                    df_an["volatility_pct"] = 0.0
            except Exception as e:
                self.logger.warning(
                    f"[{current_asset_symbol}] Échec calcul volatilité_pct: {e}"
                )
                df_an["volatility_pct"] = 0.0

            # === Bloc volume momentum ===
            try:
                volume_ma_period = (
                    int(
                        self.config_manager.get(
                            "phase_detection_defaults.regime_detection_settings.volume_profile.volume_ma_period",
                            20,
                        )
                    )
                    if getattr(self, "config_manager", None)
                    else 20
                )
            except Exception:
                volume_ma_period = 20
            volume_zscore_period = 50

            if "tick_volume" in df_an.columns and len(df_an) > volume_zscore_period:
                df_an["tick_volume"] = pd.to_numeric(
                    df_an["tick_volume"], errors="coerce"
                ).fillna(0.0)
                df_an["volume_ma"] = (
                    df_an["tick_volume"]
                    .rolling(window=volume_ma_period, min_periods=1)
                    .mean()
                )

                volume_mean_z = (
                    df_an["tick_volume"]
                    .rolling(window=volume_zscore_period, min_periods=1)
                    .mean()
                )
                volume_std_z = (
                    df_an["tick_volume"]
                    .rolling(window=volume_zscore_period, min_periods=1)
                    .std(ddof=0)
                    .replace(0, np.nan)
                )
                df_an["volume_zscore"] = (
                    ((df_an["tick_volume"] - volume_mean_z) / volume_std_z)
                    .replace([np.inf, -np.inf], 0.0)
                    .fillna(0.0)
                )

                vol_std_ma = (
                    df_an["tick_volume"]
                    .rolling(window=volume_ma_period, min_periods=1)
                    .std(ddof=0)
                    .replace(0, np.nan)
                )
                df_an["volume_momentum"] = (
                    ((df_an["tick_volume"] - df_an["volume_ma"]) / vol_std_ma)
                    .replace([np.inf, -np.inf], 0.0)
                    .fillna(0.0)
                )
            else:
                self.logger.warning(
                    f"Données volume insuffisantes pour {current_asset_symbol}"
                )
                df_an["volume_zscore"] = 0.0
                df_an["volume_momentum"] = 0.0

            # === PHASE 2: CORE INDICATORS ===
            toggles = {}
            try:
                if getattr(self, "config_manager", None):
                    toggles = (
                        self.config_manager.get(
                            "phase_detection_defaults.detection_toggles", {}
                        )
                        or {}
                    )
            except Exception:
                toggles = {}

            if toggles.get("detect_regime", True):
                df_an["regime"] = self.detect_market_regime(df_an)
                df_an["regime_detected"] = True
            else:
                df_an["regime"] = "unknown"
                df_an["regime_detected"] = False
                df_an["regime_strength"] = 0.5

            if toggles.get("detect_fvg", True):
                df_an["fvg_details"] = self.detect_fvg_enhanced(df_an)
                df_an["fvg_detected"] = df_an["fvg_details"].apply(
                    lambda x: x is not None
                )
            else:
                df_an["fvg_details"] = [None] * len(df_an)
                df_an["fvg_detected"] = False

            if toggles.get("detect_order_block", True):
                df_an["ob_details"] = self.detect_order_block_ml_enhanced(df_an)
                df_an["ob_detected"] = df_an["ob_details"].apply(
                    lambda x: x is not None
                )
            else:
                df_an["ob_details"] = [None] * len(df_an)
                df_an["ob_detected"] = False

            if toggles.get("detect_bos_mss", True):
                df_an["bos_mss_details"] = self.detect_bos_mss_enhanced(df_an)
                df_an["bos_mss_detected"] = df_an["bos_mss_details"].apply(
                    lambda x: x is not None
                )
            else:
                df_an["bos_mss_details"] = [None] * len(df_an)
                df_an["bos_mss_detected"] = False

            # === (NOUVEAU) MICROPHASES BOLLINGER – non intrusif, dernière barre uniquement ===
            if toggles.get("detect_bollinger", True):
                try:
                    # Initialiser les colonnes de sortie
                    init_cols = [
                        ("boll_signal", None),
                        ("boll_band_touch", None),
                        ("boll_in_band", np.nan),
                        ("boll_is_squeeze", np.nan),
                        ("boll_is_expansion", np.nan),
                        ("boll_breakout_score", np.nan),
                        ("boll_mean_revert_score", np.nan),
                        ("boll_z_band", np.nan),
                        ("boll_dist_to_upper_pips", np.nan),
                        ("boll_dist_to_lower_pips", np.nan),
                        ("boll_dist_to_mid_pips", np.nan),
                        ("boll_bb_upper", np.nan),
                        ("boll_bb_lower", np.nan),
                        ("boll_bb_mid", np.nan),
                        ("boll_atr_pips", np.nan),
                    ]
                    for col, default in init_cols:
                        if col not in df_an.columns:
                            df_an[col] = default

                    # Calibrage pip_size via 'point' si dispo
                    pip_size = None
                    try:
                        if "point" in df_an.columns:
                            point_val = float(df_an["point"].iloc[-1])
                            pip_size = point_val * 10.0 if point_val > 0 else None
                    except Exception:
                        pip_size = None

                    boll = self.compute_bollinger_microphase_signals(
                        df_an,
                        price_col="close",
                        period=int(
                            self.config_manager.get(
                                "phase_detection_defaults.bollinger.period", 20
                            )
                        ),
                        std_mult=float(
                            self.config_manager.get(
                                "phase_detection_defaults.bollinger.std_mult", 2.0
                            )
                        ),
                        squeeze_window=int(
                            self.config_manager.get(
                                "phase_detection_defaults.bollinger.squeeze_window", 100
                            )
                        ),
                        squeeze_percentile=float(
                            self.config_manager.get(
                                "phase_detection_defaults.bollinger.squeeze_percentile",
                                0.15,
                            )
                        ),
                        min_bars=int(
                            self.config_manager.get(
                                "phase_detection_defaults.bollinger.min_bars", 200
                            )
                        ),
                        atr_period=int(
                            self.config_manager.get(
                                "phase_detection_defaults.bollinger.atr_period", 14
                            )
                        ),
                        pip_size=pip_size,
                        mode="katana",
                    )

                    if isinstance(boll, dict) and boll.get("ok", False):
                        idx = df_an.index[-1]
                        df_an.loc[idx, "boll_signal"] = boll.get("signal")
                        df_an.loc[idx, "boll_band_touch"] = boll.get("band_touch")
                        df_an.loc[idx, "boll_in_band"] = float(
                            bool(boll.get("in_band"))
                        )
                        df_an.loc[idx, "boll_is_squeeze"] = float(
                            bool(boll.get("is_squeeze"))
                        )
                        df_an.loc[idx, "boll_is_expansion"] = float(
                            bool(boll.get("is_expansion"))
                        )
                        df_an.loc[idx, "boll_breakout_score"] = float(
                            boll.get("breakout_score", np.nan)
                        )
                        df_an.loc[idx, "boll_mean_revert_score"] = float(
                            boll.get("mean_revert_score", np.nan)
                        )
                        df_an.loc[idx, "boll_z_band"] = (
                            float(boll.get("z_band"))
                            if boll.get("z_band") is not None
                            else np.nan
                        )
                        df_an.loc[idx, "boll_dist_to_upper_pips"] = (
                            float(boll.get("dist_to_upper_pips", np.nan))
                            if boll.get("dist_to_upper_pips") is not None
                            else np.nan
                        )
                        df_an.loc[idx, "boll_dist_to_lower_pips"] = (
                            float(boll.get("dist_to_lower_pips", np.nan))
                            if boll.get("dist_to_lower_pips") is not None
                            else np.nan
                        )
                        df_an.loc[idx, "boll_dist_to_mid_pips"] = (
                            float(boll.get("dist_to_mid_pips", np.nan))
                            if boll.get("dist_to_mid_pips") is not None
                            else np.nan
                        )
                        df_an.loc[idx, "boll_bb_upper"] = (
                            float(boll.get("bb_upper", np.nan))
                            if boll.get("bb_upper") is not None
                            else np.nan
                        )
                        df_an.loc[idx, "boll_bb_lower"] = (
                            float(boll.get("bb_lower", np.nan))
                            if boll.get("bb_lower") is not None
                            else np.nan
                        )
                        df_an.loc[idx, "boll_bb_mid"] = (
                            float(boll.get("bb_mid", np.nan))
                            if boll.get("bb_mid") is not None
                            else np.nan
                        )
                        df_an.loc[idx, "boll_atr_pips"] = (
                            float(boll.get("atr_pips", np.nan))
                            if boll.get("atr_pips") is not None
                            else np.nan
                        )
                    else:
                        reason = (
                            (boll or {}).get("reason")
                            if isinstance(boll, dict)
                            else "unknown"
                        )
                        self.logger.debug(
                            f"[{current_asset_symbol}] Bollinger microphase non disponible: {reason}"
                        )
                except Exception as e:
                    self.logger.warning(
                        f"[{current_asset_symbol}] Erreur compute_bollinger_microphase_signals: {e}",
                        exc_info=False,
                    )

            # === PHASE 3: DÉTECTION LIQUIDITÉ ===
            try:
                indices_symbols = (
                    set(
                        self.config_manager.get(
                            "global_safety.indices_symbols", ["US30", "NAS100"]
                        )
                    )
                    if getattr(self, "config_manager", None)
                    else {"US30", "NAS100"}
                )
            except Exception:
                indices_symbols = {"US30", "NAS100"}

            if (asset_symbol or "UNKNOWN_ASSET") in indices_symbols:
                max_spread = (
                    float(
                        self.config_manager.get(
                            "phase_detection_defaults.liquidity_detection.indices_settings.max_allowed_spread_points",
                            50,
                        )
                    )
                    if getattr(self, "config_manager", None)
                    else 50.0
                )
                min_volume = (
                    float(
                        self.config_manager.get(
                            "phase_detection_defaults.liquidity_detection.indices_settings.min_volume_threshold",
                            10,
                        )
                    )
                    if getattr(self, "config_manager", None)
                    else 10.0
                )
            else:
                max_spread = (
                    float(
                        self.config_manager.get(
                            "phase_detection_defaults.liquidity_detection.forex_settings.max_allowed_spread_points",
                            10,
                        )
                    )
                    if getattr(self, "config_manager", None)
                    else 10.0
                )
                min_volume = (
                    float(
                        self.config_manager.get(
                            "phase_detection_defaults.liquidity_detection.forex_settings.min_volume_threshold",
                            1,
                        )
                    )
                    if getattr(self, "config_manager", None)
                    else 1.0
                )

            last_spread = (
                float(df_an["spread"].iloc[-1])
                if "spread" in df_an.columns
                else float("inf")
            )
            last_volume = (
                float(df_an["tick_volume"].iloc[-1])
                if "tick_volume" in df_an.columns
                else 0.0
            )
            df_an["is_liquid"] = (last_spread <= max_spread) and (
                last_volume >= min_volume
            )

            # === PHASE 4: SIGNAUX DE CONFLUENCE ===
            df_an["fvg_ob_confluence"] = df_an["fvg_detected"] & df_an["ob_detected"]
            df_an["high_quality_ob"] = df_an["ob_details"].apply(
                lambda x: isinstance(x, dict) and float(x.get("ml_score", 0.0)) > 0.8
            )
            df_an["confirmed_structure_break"] = df_an["bos_mss_details"].apply(
                lambda x: isinstance(x, dict)
                and float(x.get("volume_ratio", 0.0)) > 2.0
            )
            df_an["institutional_setup"] = df_an["regime"].astype(str).str.contains(
                "institutional", na=False
            ) & (df_an["ob_detected"] | df_an["bos_mss_detected"])

            # === PHASE 5: PHASE OPTIMISÉE + FALLBACK ===
            df_an["phase_primary"] = df_an.apply(self.determine_optimized_phase, axis=1)

            try:
                low_th = (
                    float(
                        self.config_manager.get(
                            "phase_detection_defaults.regime_detection_settings.volatility.thresholds.low_pct",
                            0.03,
                        )
                    )
                    if getattr(self, "config_manager", None)
                    else 0.03
                )
                high_th = (
                    float(
                        self.config_manager.get(
                            "phase_detection_defaults.regime_detection_settings.volatility.thresholds.high_pct",
                            0.15,
                        )
                    )
                    if getattr(self, "config_manager", None)
                    else 0.15
                )
            except Exception:
                low_th, high_th = 0.03, 0.15

            df_an["phase"] = df_an["phase_primary"]
            df_an["phase_rule"] = "primary"

            def _apply_phase_fallback(row: pd.Series):
                p = str(row.get("phase_primary", "no_clear_phase"))
                if p != "no_clear_phase":
                    return p, "primary"
                v = float(row.get("volatility_pct", 0.0))
                if v < low_th:
                    return "range_retail", "fallback_low"
                if v >= high_th:
                    return "range_distribution", "fallback_high"
                return "no_clear_phase", "fallback_mid"

            phase_fallback_vals = df_an.apply(_apply_phase_fallback, axis=1)
            df_an["phase"] = [p for p, _r in phase_fallback_vals]
            df_an["phase_rule"] = [_r for _p, _r in phase_fallback_vals]

            # === PHASE 6: SCORE DE CONFIANCE ===
            df_an["confidence_score"] = df_an.apply(
                self.calculate_optimized_confidence, axis=1
            )

            # === PHASE 7: MÉTRIQUES + LOG FINAL ===
            if not df_an.empty:
                try:
                    total_signals = (
                        df_an[["fvg_detected", "ob_detected", "bos_mss_detected"]]
                        .sum()
                        .sum()
                    )
                except Exception:
                    total_signals = 0
                avg_confidence = (
                    float(df_an["confidence_score"].mean())
                    if "confidence_score" in df_an.columns
                    else 0.0
                )
                last_phase = str(df_an["phase"].iloc[-1])
                last_confidence = float(df_an["confidence_score"].iloc[-1])
                last_regime = (
                    str(df_an["regime"].iloc[-1])
                    if "regime" in df_an.columns
                    else "unknown"
                )
                last_vol = (
                    float(df_an["volatility_pct"].iloc[-1])
                    if "volatility_pct" in df_an.columns
                    else 0.0
                )
                last_rule = (
                    str(df_an["phase_rule"].iloc[-1])
                    if "phase_rule" in df_an.columns
                    else "primary"
                )

                self.logger.info(
                    f"🎯 [{current_asset_symbol}] Pipeline terminé: "
                    f"Phase={last_phase}, Confidence={last_confidence:.3f}, "
                    f"Régime={last_regime}, Signaux totaux={int(total_signals)}, "
                    f"Volatilité={last_vol:.3f}% | Rule={last_rule}"
                )

            return df_an
        except Exception as e:
            self.logger.error(f"analyze() failure: {e}", exc_info=True)
            return None

    def analyze_asset_multi_timeframe(
        self, asset: str, strategy_config: Dict
    ) -> Dict[str, Any]:
        """
        🏛️ ANALYSE MULTI-TIMEFRAME INSTITUTIONNELLE 🏛️
        """
        analysis_start_time = time.perf_counter()

        # === PHASE 1: VALIDATION & INITIALISATION ===
        try:
            multi_tf_config = (strategy_config.get("phase_detection", {}) or {}).get(
                "multi_timeframe", {}
            ) or {}
        except Exception:
            multi_tf_config = {}

        if not bool(multi_tf_config.get("enabled", False)):
            self.logger.debug(
                f"[{asset}] Multi-TF désactivé, fallback analyse standard"
            )
            return self._analyze_single_tf_fallback(asset)

        # Configuration avancée
        timeframes = multi_tf_config.get("timeframes", ["M1", "M5", "M15"]) or [
            "M1",
            "M5",
            "M15",
        ]
        confluence_weights = multi_tf_config.get(
            "confluence_weights", {"M1": 0.5, "M5": 0.3, "M15": 0.2}
        ) or {"M1": 0.5, "M5": 0.3, "M15": 0.2}
        cache_ttl_seconds = int(multi_tf_config.get("cache_ttl_seconds", 30) or 30)
        quality_threshold = float(multi_tf_config.get("min_quality_score", 0.7) or 0.7)

        self.logger.info(f"🎯 [{asset}] KATANA Multi-TF activé: {timeframes}")

        # === PHASE 2: ACQUISITION DONNÉES AVEC CACHE INTELLIGENT ===
        tf_data_cache: Dict[str, pd.DataFrame] = {}
        cache_hits = 0

        now_bucket = int(time.time() // max(1, cache_ttl_seconds))
        for tf in timeframes:
            cache_key = f"{asset}_{tf}_{now_bucket}"

            # Vérification cache
            if hasattr(self, "_tf_data_cache") and cache_key in getattr(
                self, "_tf_data_cache", {}
            ):
                tf_data_cache[tf] = self._tf_data_cache[cache_key]
                cache_hits += 1
                self.logger.debug(f"🚀 [{asset}] Cache HIT pour {tf}")
            else:
                # Acquisition données fraîches
                try:
                    tf_data = self._fetch_timeframe_data(asset, tf, multi_tf_config)
                    if tf_data is not None and not tf_data.empty:
                        tf_data_cache[tf] = tf_data
                        # Mise à jour cache
                        if not hasattr(self, "_tf_data_cache"):
                            self._tf_data_cache = {}
                        self._tf_data_cache[cache_key] = tf_data
                        self.logger.debug(
                            f"📡 [{asset}] Données {tf} acquises: {len(tf_data)} barres"
                        )
                    else:
                        self.logger.warning(f"⚠️ [{asset}] Échec acquisition {tf}")
                        continue
                except Exception as e:
                    self.logger.error(
                        f"💥 [{asset}] Erreur critique {tf}: {e}", exc_info=False
                    )
                    continue

        # Vérification intégrité données
        if len(tf_data_cache) < 2:
            self.logger.warning(
                f"⚠️ [{asset}] Données insuffisantes ({len(tf_data_cache)}/{len(timeframes)}) pour Multi-TF"
            )
            return self._analyze_single_tf_fallback(asset)

        cache_efficiency = (cache_hits / max(1, len(timeframes))) * 100.0
        self.logger.debug(f"📊 [{asset}] Cache efficiency: {cache_efficiency:.1f}%")

        # === PHASE 3: ANALYSE VECTORIELLE PARALLÈLE ===
        tf_analyses: Dict[str, Dict[str, Any]] = {}
        analysis_errors: list[str] = []

        for tf, tf_data in tf_data_cache.items():
            try:
                # Config spécifique TF
                tf_config = self._get_tf_specific_config(tf, multi_tf_config)

                # Override temporaire des paramètres pour ce TF
                original_params = self._backup_current_params()
                self._load_settings(overrides=tf_config)

                # Analyse vectorielle
                analyzed_data = self.analyze(tf_data, asset_symbol=asset)

                # Restauration paramètres system
                self._restore_params(original_params)

                if analyzed_data is not None and not analyzed_data.empty:
                    last_signals = self._extract_last_bar_signals(analyzed_data, tf)
                    tf_analyses[tf] = last_signals
                    self.logger.debug(
                        f"✅ [{asset}] {tf} analysé: Phase={last_signals.get('phase')}"
                    )
                else:
                    raise ValueError(f"Analyse {tf} retournée vide")
            except Exception as e:
                analysis_errors.append(f"{tf}: {str(e)}")
                self.logger.error(
                    f"💥 [{asset}] Erreur analyse {tf}: {e}", exc_info=False
                )

        # === PHASE 4: FUSION INTELLIGENTE & CONFLUENCE ===
        if len(tf_analyses) < 2:
            self.logger.warning(f"⚠️ [{asset}] Analyses insuffisantes pour confluence")
            return self._analyze_single_tf_fallback(asset)

        confluence_result = self._calculate_advanced_confluence(
            tf_analyses, confluence_weights, asset
        )

        # Détection divergences inter-TF
        divergence_analysis = self._detect_tf_divergences(tf_analyses)

        # === PHASE 5: SCORING QUALITÉ & MÉTRIQUES ===
        quality_metrics = self._calculate_quality_metrics(
            tf_analyses, confluence_result, divergence_analysis, analysis_start_time
        )

        # Filtrage qualité
        if float(quality_metrics.get("overall_score", 0.0)) < quality_threshold:
            self.logger.warning(
                f"⚠️ [{asset}] Qualité insuffisante ({float(quality_metrics.get('overall_score', 0.0)):.3f} < {quality_threshold})"
            )
            return self._build_low_quality_response(asset, quality_metrics)

        # === PHASE 6: CONSTRUCTION RÉPONSE FINALE ===
        final_signals = self._build_enhanced_signals(
            confluence_result, quality_metrics, tf_analyses, asset
        )

        execution_time = (time.perf_counter() - analysis_start_time) * 1000.0
        self.logger.info(
            f"🎯 [{asset}] KATANA Multi-TF terminé: "
            f"Phase={final_signals.get('phase')}, "
            f"Qualité={quality_metrics['overall_score']:.3f}, "
            f"Temps={execution_time:.1f}ms"
        )

        return final_signals

    def get_katana_snapshot(self, asset: str, strategy_config: dict) -> dict:
        """
        Snapshot micro-décisionnel prêt pour le pipeline (M1 dirigé par BOS/MSS, alignement M5/M15,
        SL/TP structurels, spread/liquidité, score final, katana_ready).
        Version Katana serrée : ajoute contrôles de fraîcheur/ATR et rejets explicites.
        """
        # --- 0) Appel MTF existant ---
        mtf = (
            self.analyze_asset_multi_timeframe(asset, strategy_config)
            if hasattr(self, "analyze_asset_multi_timeframe")
            else {}
        )
        if not mtf or not mtf.get("multi_tf_enabled", False):
            return {"katana_ready": False, "reason": "insufficient_confluence"}

        # --- 1) Récup/Analyse M1 ---
        pd_cfg = strategy_config.get("phase_detection", {}) or {}
        mtf_cfg = pd_cfg.get("multi_timeframe", {}) or {}
        kat_cfg = pd_cfg.get("katana", {}) or {}

        # paramètres Katana (avec défauts prudents)
        max_age_sec = int(
            kat_cfg.get("max_signal_age_seconds", 30) or 30
        )  # fraicheur du signal M1
        max_bos_age_bars = int(
            kat_cfg.get("max_bos_age_bars", 3) or 3
        )  # BOS/MSS <= N dernières bougies
        min_atr_m1_pips = float(
            kat_cfg.get("min_atr_m1_pips", 0.6) or 0.6
        )  # micro-vol minimal (souhaité)
        hard_min_atr_m1_pips = float(
            kat_cfg.get("hard_min_atr_m1_pips", 0.12) or 0.12
        )  # seuil incompressible
        points_per_pip = 10.0

        m1_raw = (
            self._fetch_timeframe_data(asset, "M1", mtf_cfg)
            if hasattr(self, "_fetch_timeframe_data")
            else None
        )
        m1_df = self.analyze(m1_raw, asset_symbol=asset) if m1_raw is not None else None
        if m1_df is None or m1_df.empty:
            return {"katana_ready": False, "reason": "m1_analysis_failed"}

        last = m1_df.iloc[-1]

        # --- 2) Fraîcheur du signal ---
        now = datetime.now(timezone.utc)
        ts_col = None
        for c in ("timestamp", "time", "datetime", "ts"):
            if c in m1_df.columns:
                ts_col = c
                break
        if ts_col:
            try:
                last_ts = last[ts_col]
                # compat pandas ts / epoch / string
                if hasattr(last_ts, "to_pydatetime"):
                    last_dt = last_ts.to_pydatetime()
                elif isinstance(last_ts, (int, float)) and last_ts > 1e9:
                    last_dt = datetime.fromtimestamp(
                        float(last_ts) / 1000.0, tz=timezone.utc
                    )
                elif isinstance(last_ts, (int, float)):
                    last_dt = datetime.fromtimestamp(float(last_ts), tz=timezone.utc)
                else:
                    last_dt = datetime.fromisoformat(str(last_ts))
                    if last_dt.tzinfo is None:
                        last_dt = last_dt.replace(tzinfo=timezone.utc)
                age_sec = (now - last_dt).total_seconds()
                if age_sec > max_age_sec:
                    return {
                        "katana_ready": False,
                        "reason": f"stale_m1_bar_{int(age_sec)}s",
                    }
            except Exception:
                # si on ne peut pas déterminer l'âge, on ne bloque pas ici
                pass

        # --- 3) Direction strictement depuis BOS/MSS M1 + âge du break ---
        side, break_ok = self._extract_m1_break_direction(last)
        if side is None or not break_ok:
            return {"katana_ready": False, "reason": "no_m1_break"}

        bos_age_bars = None
        for k in ("bos_mss_age_bars", "m1_break_age_bars", "break_age"):
            if k in m1_df.columns:
                try:
                    bos_age_bars = int(m1_df[k].iloc[-1])
                    break
                except Exception:
                    pass
        if bos_age_bars is not None and bos_age_bars > max_bos_age_bars:
            return {"katana_ready": False, "reason": f"stale_break_{bos_age_bars}bars"}

        # --- 4) Alignement HTF (M5/M15) ---
        phase = str(mtf.get("phase", "unknown")).lower()
        htf_alignment_ok = (side == "BUY" and ("bull" in phase or "up" in phase)) or (
            side == "SELL" and ("bear" in phase or "down" in phase)
        )

        # --- 5) Liquidité/Spread soft flag ---
        spread_ok = (
            bool(m1_df["is_liquid"].iloc[-1]) if "is_liquid" in m1_df.columns else True
        )

        # --- 6) ATR M1 minimal (anti SL irréaliste) ---
        def _calc_atr(df: pd.DataFrame, period: int = 14) -> float:
            if df is None or len(df) < period + 2:
                return float("nan")
            high = df["high"].astype(float)
            low = df["low"].astype(float)
            close = df["close"].astype(float)
            prev_close = close.shift(1)
            tr = np.maximum.reduce(
                [
                    (high - low).abs(),
                    (high - prev_close).abs(),
                    (low - prev_close).abs(),
                ]
            )
            atr = tr.rolling(window=period, min_periods=period).mean().iloc[-1]
            return float(atr) if pd.notna(atr) and atr > 0 else float("nan")

        if "atr14" in m1_df.columns and isinstance(
            m1_df["atr14"].iloc[-1], (int, float)
        ):
            atr_m1 = float(m1_df["atr14"].iloc[-1])
        else:
            atr_m1 = _calc_atr(m1_df, 14)

        # point -> pip_size
        point = 0.0
        try:
            # essaie via self.symbol_info.point (objet) puis via dict; sinon fallback colonne 'point'
            si = getattr(self, "symbol_info", None)
            if si is not None and hasattr(si, "point"):
                point = float(getattr(si, "point") or 0.0)
            elif isinstance(si, dict):
                point = float(si.get("point", 0.0) or 0.0)
            if point <= 0 and "point" in m1_df.columns:
                point = float(m1_df["point"].iloc[-1] or 0.0)
        except Exception:
            if "point" in m1_df.columns:
                point = float(m1_df["point"].iloc[-1] or 0.0)

        pip_size = point * points_per_pip if point > 0 else None
        if pip_size and isinstance(atr_m1, float) and atr_m1 > 0:
            atr_m1_pips = atr_m1 / pip_size
            if atr_m1_pips < hard_min_atr_m1_pips:
                return {
                    "katana_ready": False,
                    "reason": f"atr_m1_too_low_{atr_m1_pips:.3f}pips",
                }
            low_atr_flag = atr_m1_pips < min_atr_m1_pips
        else:
            atr_m1_pips = None
            low_atr_flag = False  # inconnu => pas de pénalité dure

        # --- 7) Confiance/Entry/SL/TP structurels ---
        conf = (
            float(m1_df["confidence_score"].iloc[-1])
            if "confidence_score" in m1_df.columns
            else 0.0
        )
        entry = float(last["close"])
        sl = self._pick_sl_from_structure(last, side)
        tp = self._pick_tp_from_nearest_liquidity(m1_df, side)

        if sl is None or tp is None or not math.isfinite(entry) or entry <= 0:
            return {"katana_ready": False, "reason": "invalid_prices"}

        # --- 8) Score & Snapshot ---
        htf_bonus = 0.15 if htf_alignment_ok else 0.0
        atr_penalty = -0.10 if low_atr_flag else 0.0
        katana_score = round(
            max(
                0.0,
                0.6 * float(mtf.get("confidence_score", 0.0))
                + 0.4 * conf
                + htf_bonus
                + atr_penalty,
            ),
            3,
        )

        snapshot = {
            "asset": asset,
            "entry_side": side,  # "BUY" / "SELL"
            "entry_price": entry,
            "sl_price": sl,
            "tp_price": tp,
            "m1_break_ok": bool(break_ok),
            "htf_alignment_ok": bool(htf_alignment_ok),
            "spread_ok": bool(spread_ok),
            "atr_m1_pips": atr_m1_pips,
            "katana_score": katana_score,
            "phase": mtf.get("phase"),
            "dominant_tf": mtf.get("dominant_tf"),
            "signal_agreement": mtf.get("signal_agreement_rates", {}),
            "max_signal_age_seconds": max_age_sec,
            "max_bos_age_bars": max_bos_age_bars,
        }

        # --- 8bis) Micro-phase hint (complément M1, jamais bloquant) ---
        try:
            micro_cfg = self.config_manager.get("features.micro_phase", {}) or {}
            if bool(micro_cfg.get("enabled", True)):
                hint = self.detect_micro_phase_m1(m1_df, params=micro_cfg.get("params"))
                snapshot.setdefault("signals", {})
                snapshot["signals"]["micro_phase_hint"] = hint

                # petit boost de score si la direction micro confirme le side
                try:
                    if hint.get("micro_phase") and hint.get("confidence_boost", 0) > 0:
                        if (
                            snapshot.get("entry_side") == "BUY"
                            and hint.get("direction") == "BUY"
                        ) or (
                            snapshot.get("entry_side") == "SELL"
                            and hint.get("direction") == "SELL"
                        ):
                            snapshot["katana_score"] = float(
                                snapshot.get("katana_score", 0.0)
                            ) + float(hint["confidence_boost"])
                            if snapshot["katana_score"] > 0.98:
                                snapshot["katana_score"] = 0.98
                except Exception:
                    pass

                # suggestions TPSL serrées sans écraser SL/TP structurels
                if hint.get("sl_pips_suggestion") is not None:
                    snapshot["signals"]["micro_phase_sl_pips_suggestion"] = hint[
                        "sl_pips_suggestion"
                    ]
                if hint.get("tp_pips_suggestion") is not None:
                    snapshot["signals"]["micro_phase_tp_pips_suggestion"] = hint[
                        "tp_pips_suggestion"
                    ]
        except Exception as _e:
            self.logger.debug(f"[{asset}] micro_phase hint non appliqué: {_e}")

        snapshot["katana_ready"] = all(
            [
                snapshot["m1_break_ok"],
                snapshot["htf_alignment_ok"],
                snapshot["spread_ok"],
                snapshot["sl_price"] is not None,
                snapshot["tp_price"] is not None,
            ]
        )
        return snapshot

    def process_multi_asset_config(self, config_filepath: Union[str, Path]):
        """
        Lit la configuration multi-actifs et analyse l'historique des trades depuis le journal d'audit centralisé.
        Génère un rapport multi-actifs et un journal d'audit.
        Le chemin du journal d'audit est lu dynamiquement depuis ConfigManager.
        """
        config_filepath = Path(config_filepath)

        # Charger config JSON
        try:
            with open(config_filepath, "r", encoding="utf-8") as f:
                config = json.load(f)
            tradeable_assets = config.get("tradeable_assets", []) or []
            self.logger.info(
                f"Actifs à traiter depuis la configuration : {tradeable_assets}"
            )
            self.log_audit_event(
                "CONFIG_LOADED",
                f"Configuration chargée depuis {config_filepath}",
                message=f"Actifs négociables: {', '.join(tradeable_assets)}",
            )
        except Exception as e:
            self.logger.error(
                f"Erreur de lecture du fichier de configuration {config_filepath}: {e}",
                exc_info=True,
            )
            return

        # Charger journal d'audit centralisé (une seule fois)
        audit_trail_path_str = None
        try:
            audit_trail_path_str = self.config_manager.get("paths.ai_history_log")
        except Exception:
            audit_trail_path_str = None

        all_trades_df = None
        if audit_trail_path_str:
            audit_trail_path = Path(audit_trail_path_str)
            if audit_trail_path.exists():
                try:
                    all_trades_df = self.load_data(audit_trail_path)
                except Exception as e:
                    self.logger.error(
                        f"Impossible de charger le journal d'audit depuis {audit_trail_path}: {e}",
                        exc_info=True,
                    )
            else:
                self.logger.warning(
                    f"Le fichier d'audit '{audit_trail_path}' est introuvable."
                )
        else:
            self.logger.error(
                "Le chemin vers 'paths.ai_history_log' n'est pas défini dans la configuration."
            )

        if all_trades_df is None or all_trades_df.empty:
            self.logger.warning(
                "Journal d'audit vide ou non chargé. L'analyse historique des actifs est impossible."
            )
            return

        self.multi_asset_report_data = []  # Réinitialiser pour chaque exécution

        symbol_column_name = (
            "symbol"  # Nom de la colonne contenant les symboles dans le log
        )

        for asset in tradeable_assets:
            self.logger.info(
                f"Analyse des données pour l'actif '{asset}' depuis le journal d'audit centralisé."
            )

            if symbol_column_name not in all_trades_df.columns:
                self.logger.error(
                    f"Colonne '{symbol_column_name}' introuvable dans le journal d'audit. Impossible de filtrer."
                )
                break

            df_raw_for_asset = all_trades_df[
                all_trades_df[symbol_column_name] == asset
            ].copy()

            if df_raw_for_asset.empty:
                self.logger.warning(
                    f"Aucune donnée historique trouvée pour l'actif '{asset}' dans le journal d'audit."
                )
                continue

            annotated_df = self.analyze(df_raw_for_asset)

            if annotated_df is not None and not annotated_df.empty:
                try:
                    dominant_phase = (
                        annotated_df["phase"].mode()[0]
                        if "phase" in annotated_df.columns
                        and not annotated_df["phase"].empty
                        else "N/A"
                    )
                except Exception:
                    dominant_phase = "N/A"
                total_volume = (
                    float(annotated_df["volume"].sum())
                    if "volume" in annotated_df.columns
                    else 0.0
                )

                self.multi_asset_report_data.append(
                    {
                        "timestamp": pd.Timestamp.now(tz=timezone.utc).isoformat(),
                        "asset": asset,
                        "dominant_phase": dominant_phase,
                        "total_volume": round(total_volume, 2),
                        "log_entries": int(len(annotated_df)),
                        "fvg_detected_count": (
                            int(annotated_df["fvg_detected"].sum())
                            if "fvg_detected" in annotated_df.columns
                            else 0
                        ),
                        "liquidity_grab_detected_count": (
                            int(annotated_df["liquidity_grab_detected"].sum())
                            if "liquidity_grab_detected" in annotated_df.columns
                            else 0
                        ),
                        "bos_mss_detected_count": (
                            int(annotated_df["bos_mss_detected"].sum())
                            if "bos_mss_detected" in annotated_df.columns
                            else 0
                        ),
                        "validated_ob_count": (
                            int(annotated_df["validated_ob"].sum())
                            if "validated_ob" in annotated_df.columns
                            else 0
                        ),
                    }
                )
                self.log_audit_event(
                    "ASSET_PROCESSED",
                    f"Analyse de {len(annotated_df)} entrées de log.",
                    asset=asset,
                )
            else:
                self.log_audit_event(
                    "PROCESSING_ERROR",
                    "L'analyse du DataFrame a échoué ou a retourné un résultat vide.",
                    asset=asset,
                )

        self.log_audit_event(
            "MULTI_ASSET_SCAN_COMPLETE",
            f"Analyse de {len(tradeable_assets)} actifs terminée.",
        )

        # Découverte d'actifs non listés basée sur le journal d'audit
        try:
            if symbol_column_name in all_trades_df.columns:
                discovered_assets = set(
                    all_trades_df[symbol_column_name].dropna().astype(str).unique()
                )
                configured_assets = set(map(str, tradeable_assets))
                unlisted_assets = discovered_assets - configured_assets

                for asset in sorted(unlisted_assets):
                    self.logger.warning(
                        f"Actif non listé '{asset}' découvert dans le journal d'audit mais non présent dans la configuration de la stratégie."
                    )
                    self.log_audit_event(
                        "UNLISTED_ASSET_DISCOVERED",
                        f"L'actif '{asset}' existe dans l'historique mais n'est pas dans la stratégie active.",
                        asset=asset,
                    )
        except Exception as e:
            self.logger.error(
                f"Erreur lors de la découverte d'actifs non listés : {e}", exc_info=True
            )

        # TODO: RAPPORT - Intégrer les résultats de cette fonction dans le rapport multi-actifs lui-même.

    def generate_multi_asset_report(self, filename: Optional[str] = None):
        """
        Génère un rapport résumé multi-actifs consolidé à partir des données traitées.
        """
        # file name de base (fallback robuste)
        final_filename_base = (
            filename
            or getattr(self, "multi_asset_report_file_name", "multi_asset_report")
        ).strip() or "multi_asset_report"

        # horodatage UTC
        timestamp_str = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
        final_filename_with_ts = f"{final_filename_base}_{timestamp_str}"

        # s'assurer que le dossier de sortie existe
        try:
            Path(self.output_path).mkdir(parents=True, exist_ok=True)
        except Exception:
            # si output_path absent, fallback local
            self.output_path = Path("./output")
            self.output_path.mkdir(parents=True, exist_ok=True)

        if not getattr(self, "multi_asset_report_data", None):
            self.logger.warning(
                "No multi-asset report data available to generate a report. Skipping report generation."
            )
            self.log_audit_event(
                "REPORT_EMPTY",
                "Attempted to generate multi-asset report but no data was available.",
                asset="N/A",
                timestamp=datetime.now(tz=timezone.utc),
            )
            return

        report_df = pd.DataFrame(self.multi_asset_report_data)

        # Ajouter un timestamp global au rapport
        report_df["report_generation_time"] = pd.Timestamp.now(
            tz=timezone.utc
        ).isoformat()

        self.logger.info("Generating multi-asset report.")

        # ---------- Export JSONL (écriture atomique) ----------
        filepath_jsonl = Path(self.output_path) / f"{final_filename_with_ts}.jsonl"
        temp_filepath_jsonl = filepath_jsonl.with_suffix(".tmp")
        try:
            report_df.to_json(
                temp_filepath_jsonl, orient="records", lines=True, date_format="iso"
            )
            temp_filepath_jsonl.replace(filepath_jsonl)  # atomic move si même FS
            self.logger.info(
                f"Multi-asset dashboard report exported to {filepath_jsonl}"
            )
            self.log_audit_event(
                "REPORT_EXPORTED",
                f"JSONL multi-asset report exported: {final_filename_with_ts}.jsonl",
                asset="ALL",
                timestamp=datetime.now(tz=timezone.utc),
            )
        except Exception as e:
            if temp_filepath_jsonl.exists():
                temp_filepath_jsonl.unlink()
            self.logger.error(
                f"Failed to export multi-asset report to JSONL: {e}", exc_info=True
            )
            self.log_audit_event(
                "EXPORT_ERROR",
                f"Failed to export JSONL multi-asset report: {e}",
                asset="ALL",
                timestamp=datetime.now(tz=timezone.utc),
            )

        # ---------- Export CSV (écriture atomique) ----------
        filepath_csv = Path(self.output_path) / f"{final_filename_with_ts}.csv"
        temp_filepath_csv = filepath_csv.with_suffix(".tmp")
        try:
            report_df.to_csv(temp_filepath_csv, index=False, float_format="%.5f")
            temp_filepath_csv.replace(filepath_csv)
            self.logger.info(f"Multi-asset dashboard report exported to {filepath_csv}")
            self.log_audit_event(
                "REPORT_EXPORTED",
                f"CSV multi-asset report exported: {final_filename_with_ts}.csv",
                asset="ALL",
                timestamp=datetime.now(tz=timezone.utc),
            )
        except Exception as e:
            if temp_filepath_csv.exists():
                temp_filepath_csv.unlink()
            self.logger.error(
                f"Failed to export multi-asset report to CSV: {e}", exc_info=True
            )
            self.log_audit_event(
                "EXPORT_ERROR",
                f"Failed to export CSV multi-asset report: {e}",
                asset="ALL",
                timestamp=datetime.now(tz=timezone.utc),
            )

        # ---------- Export Markdown (écriture atomique) ----------
        filepath_md = Path(self.output_path) / f"{final_filename_with_ts}.md"
        temp_filepath_md = filepath_md.with_suffix(".tmp")
        try:
            with open(temp_filepath_md, "w", encoding="utf-8") as f:
                f.write(
                    f"# Rapport du Tableau de Bord Multi-Actifs - {pd.Timestamp.now(tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                )
                f.write(
                    "Ce rapport fournit un aperçu consolidé des phases de marché et des anomalies détectées pour chaque actif surveillé.\n\n"
                )
                f.write("## Synthèse par Actif\n")
                # NOTE: to_markdown nécessite 'tabulate'. Si non dispo, remplace par to_string().
                try:
                    f.write(report_df.to_markdown(index=False))
                except Exception:
                    f.write(report_df.to_string(index=False))
                f.write(
                    "\n\n*Score de Confiance Moyen*: Indique la robustesse de la détection de phase. Plus le score est élevé (proche de 1.0), plus la détection est considérée comme fiable.\n"
                )
                f.write(
                    "*Anomalies Détectées*: Signale la présence d'événements inhabituels (e.g., pics de volume, prises de liquidité).\n"
                )
            temp_filepath_md.replace(filepath_md)
            self.logger.info(f"Multi-asset dashboard report exported to {filepath_md}")
            self.log_audit_event(
                "REPORT_EXPORTED",
                f"Markdown multi-asset report exported: {final_filename_with_ts}.md",
                asset="ALL",
                timestamp=datetime.now(tz=timezone.utc),
            )
        except Exception as e:
            if temp_filepath_md.exists():
                temp_filepath_md.unlink()
            self.logger.error(
                f"Failed to export multi-asset report to Markdown: {e}", exc_info=True
            )
            self.log_audit_event(
                "EXPORT_ERROR",
                f"Failed to export Markdown multi-asset report: {e}",
                asset="ALL",
                timestamp=datetime.now(tz=timezone.utc),
            )

    def export_to_csv(self, report_df: pd.DataFrame, filename: str):
        """
        Exporte le rapport final ou un DataFrame donné au format CSV.
        """
        filepath = Path(self.output_path) / f"{filename}.csv"
        try:
            Path(self.output_path).mkdir(parents=True, exist_ok=True)
            report_df.to_csv(filepath, index=False, float_format="%.5f")
            self.logger.info(f"Report successfully exported to {filepath}")
            self.log_audit_event(
                "REPORT_EXPORTED",
                f"CSV report exported: {filename}.csv",
                asset="N/A",
                timestamp=pd.Timestamp.now(tz=timezone.utc),
            )
        except Exception as e:
            self.logger.error(f"Failed to export to CSV: {e}", exc_info=True)
            self.log_audit_event(
                "EXPORT_ERROR",
                f"Failed to export CSV report: {e}",
                asset="N/A",
                timestamp=pd.Timestamp.now(tz=timezone.utc),
            )

    def export_to_jsonl(self, report_df: pd.DataFrame, filename: str):
        """
        Exporte le rapport final ou un DataFrame donné au format JSONL.
        """
        filepath = Path(self.output_path) / f"{filename}.jsonl"
        temp_filepath = filepath.with_suffix(".tmp")
        try:
            Path(self.output_path).mkdir(parents=True, exist_ok=True)
            report_df.to_json(
                temp_filepath, orient="records", lines=True, date_format="iso"
            )
            temp_filepath.replace(filepath)
            self.logger.info(f"Report successfully exported to {filepath}")
            self.log_audit_event(
                "REPORT_EXPORTED",
                f"JSONL report exported: {filename}.jsonl",
                asset="N/A",
                timestamp=pd.Timestamp.now(tz=timezone.utc),
            )
        except Exception as e:
            if temp_filepath.exists():
                temp_filepath.unlink()
            self.logger.error(f"Failed to export to JSONL: {e}", exc_info=True)
            self.log_audit_event(
                "EXPORT_ERROR",
                f"Failed to export JSONL report: {e}",
                asset="N/A",
                timestamp=pd.Timestamp.now(tz=timezone.utc),
            )

    def log_audit_event(
        self,
        event_type: str,
        message: str,
        asset: Optional[str] = None,
        timestamp: Optional[datetime] = None,
    ):
        """
        Enregistre un événement dans le journal d'audit interne.
        """
        ts = (timestamp or datetime.now(tz=timezone.utc)).isoformat()
        event = {
            "timestamp": ts,
            "event_type": event_type,
            "asset": asset,
            "message": message,
        }

        if not hasattr(self, "audit_journal") or self.audit_journal is None:
            self.audit_journal = []

        self.audit_journal.append(event)
        self.logger.info(
            f"ÉVÉNEMENT D'AUDIT [{event_type}] pour {asset or 'N/A'}: {message}"
        )

    def export_audit_journal(self, filename: Optional[str] = None):
        """
        Exporte le journal d'audit de manière atomique dans un fichier JSONL.
        """
        base_name = (
            filename or getattr(self, "audit_journal_file_name", "audit_journal")
        ).strip() or "audit_journal"
        timestamp_str = datetime.now(tz=timezone.utc).strftime("%Y%m%d")
        filepath = Path(self.output_path) / f"{base_name}_{timestamp_str}.jsonl"

        try:
            Path(self.output_path).mkdir(parents=True, exist_ok=True)
        except Exception:
            self.output_path = Path("./output")
            self.output_path.mkdir(parents=True, exist_ok=True)
            filepath = Path(self.output_path) / f"{base_name}_{timestamp_str}.jsonl"

        self.logger.info(f"Exportation du journal d'audit vers {filepath}...")

        temp_path = filepath.with_suffix(".tmp")
        try:
            with open(temp_path, "w", encoding="utf-8") as f:
                for event in getattr(self, "audit_journal", []) or []:
                    f.write(json.dumps(event, ensure_ascii=False) + "\n")
            temp_path.replace(filepath)
            self.logger.info("Journal d'audit exporté avec succès.")
        except Exception as e:
            self.logger.error(
                f"Échec de l'exportation du journal d'audit: {e}", exc_info=True
            )
            if temp_path.exists():
                temp_path.unlink()
