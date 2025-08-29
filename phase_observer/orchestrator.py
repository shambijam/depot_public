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
import os

# Local
from .types import Direction, Phase, PhaseSignal, PhaseSnapshot, MarketFeatures, PhaseMemory
from .validators import calculate_confidence_score
from .features import FeaturesExtractor   # ✅ on importe la classe, plus les fonctions
from .detectors import Detectors
from phase_observer.reporter import PhaseObserverReporter
from datetime import datetime, timezone

    
# Alias UTC
UTC = timezone.utc


class PhaseObserver:
    signal_weights: Dict[str, float]
    confluence_bonus: Dict[str, float]

    def __init__(self, config_manager=None):
        """
        Initialise le PhaseObserver avec les paramètres de configuration.
        """
        self.config_manager = config_manager
        self.logger = logging.getLogger(__name__)

        # === Instance unique de FeaturesExtractor ===
        self.features = FeaturesExtractor(config_manager=config_manager, logger=self.logger)
        
        # === Instance unique de Detectors ===
        self.detectors = Detectors(config_manager=config_manager)

        # DÉFINIR LES VALEURS PAR DÉFAUT D'ABORD
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

        # CHARGER LA CONFIG SI DISPONIBLE (écrasera les valeurs par défaut)
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
                # ... etc pour les autres paramètres
            except Exception as e:
                self.logger.warning(
                    f"Impossible de charger config: {e}. Utilisation des valeurs par défaut."
                )

        self.logger.info(
            f"PhaseObserver initialisé. Lookback window: {self.lookback_window}."
        )
        
    def calculate_optimized_confidence(self, row) -> float:
        """Score de confiance unique et unifié (signaux core + confluence + Bollinger + qualité)."""

        # --- 1) Lecture robuste de la config ---
        try:
            cfg = (
                self.config_manager.get("confidence_score_calculation", None)
                or self.config_manager.get("phase_detection_defaults.confidence_score_calculation", None)
                or self.config_manager.get("phase_detection_defaults.confidence_scoring", None)
                or {}
            )
            cfg_path_used = "config_loaded"
        except Exception:
            cfg, cfg_path_used = {}, "fallback"

        # --- 2) Normalisation des clés (fallbacks inclus) ---
        signal_weights = cfg.get("signal_weights")
        if not isinstance(signal_weights, dict):
            weights = cfg.get("weights", {})
            signal_weights = {
                "fvg_detected": float(weights.get("fvg", weights.get("fvg_detected", 0.25))),
                "ob_detected": float(weights.get("ob", weights.get("ob_detected", 0.35))),
                "bos_mss_detected": float(weights.get("bos_mss", weights.get("bos_mss_detected", 0.25))),
                "regime_alignment": float(weights.get("regime", weights.get("regime_alignment", 0.15))),
            }

        confluence_bonus    = cfg.get("confluence_bonus", cfg.get("confluence", {})) or {}
        quality_multipliers = cfg.get("quality_factors", cfg.get("quality_multipliers", {})) or {}

        boll_weights = cfg.get("bollinger_weights") or {}
        boll_mean_revert_w   = float(boll_weights.get("mean_revert_score", 0.10))
        boll_breakout_w      = float(boll_weights.get("breakout_score", 0.10))
        boll_squeeze_bonus   = float(boll_weights.get("squeeze_bonus", 0.05))
        boll_expansion_bonus = float(boll_weights.get("expansion_bonus", 0.05))

        base_confidence = float(cfg.get("base_confidence", cfg.get("base", 0.2)))
        max_confidence  = float(cfg.get("max_confidence_cap", cfg.get("cap", 0.95)))

        # --- 3) Score core ---
        score = float(base_confidence)

        if bool(row.get("fvg_detected", False)):
            score += signal_weights.get("fvg_detected", 0.25)
        if bool(row.get("ob_detected", False)):
            score += signal_weights.get("ob_detected", 0.35)
        if bool(row.get("bos_mss_detected", False)):
            score += signal_weights.get("bos_mss_detected", 0.25)

        regime_strength = float(row.get("regime_strength", 0.5) or 0.5)
        if regime_strength > 0.7:
            score += signal_weights.get("regime_alignment", 0.15)

        # --- 4) Bonus confluence ---
        if bool(row.get("fvg_ob_confluence", False)):
            score += confluence_bonus.get("fvg_ob_confluence", 0.15)
        if bool(row.get("high_quality_ob", False)) and bool(row.get("bos_mss_detected", False)):
            score += confluence_bonus.get("ob_bos_confluence", 0.10)
        if bool(row.get("institutional_setup", False)):
            score += confluence_bonus.get("full_confluence_bonus", 0.20)

        # --- 5) Lecture Bollinger (optionnelle, avec renfort sur l’historique) ---
        try:
            boll_revert   = float(row.get("boll_mean_revert_score", 0.0) or 0.0)
            boll_break    = float(row.get("boll_breakout_score", 0.0) or 0.0)
            boll_sig      = (row.get("boll_signal") or "").strip().lower()
            is_squeeze    = bool(row.get("boll_is_squeeze", False))
            is_expansion  = bool(row.get("boll_is_expansion", False))

            regime = str(row.get("regime", "unknown") or "unknown").lower()
            in_range_regime = ("range_" in regime) or ("low_volatility" in regime)

            if boll_revert > 0:
                local_w = boll_mean_revert_w * (1.15 if in_range_regime else 1.0)
                score += local_w * min(1.0, max(0.0, boll_revert))

            if boll_break > 0:
                in_high_vol = "high_volatility" in regime
                local_w = boll_breakout_w * (1.15 if (is_expansion or in_high_vol) else 1.0)
                score += local_w * min(1.0, max(0.0, boll_break))

            if is_squeeze and in_range_regime:
                score += boll_squeeze_bonus
            if is_expansion and "high_volatility" in regime:
                score += boll_expansion_bonus

            if boll_sig in {"buy_breakout", "sell_breakout"} and (is_expansion or "high_volatility" in regime):
                score += min(0.05, boll_break * 0.05)
            if boll_sig in {"buy_revert", "sell_revert"} and in_range_regime and is_squeeze:
                score += min(0.05, boll_revert * 0.05)
        except Exception:
            pass

        # --- 6) Multiplicateurs qualité/liquidité ---
        if bool(row.get("is_liquid", True)):
            score *= quality_multipliers.get("tight_spread", 1.05)
        if regime_strength > 0.8:
            score *= quality_multipliers.get("regime_strength", 1.10)

        try:
            ob_details = row.get("ob_details")
            bos_details = row.get("bos_mss_details")
            if isinstance(ob_details, dict) and float(ob_details.get("volume_spike", 0) or 0) > 1.5:
                score *= quality_multipliers.get("high_volume_confirmation", 1.15)
            elif isinstance(bos_details, dict) and float(bos_details.get("volume_ratio", 0) or 0) > 1.5:
                score *= quality_multipliers.get("high_volume_confirmation", 1.15)
        except Exception:
            pass

        # --- 7) Clamp final & log ---
        score = max(0.0, min(max_confidence, score))

        if getattr(self, "debug_confidence_logging", False):
            try:
                self.logger.debug(
                    f"[CONF] path='{cfg_path_used}' base={base_confidence} cap={max_confidence} "
                    f"-> score={score:.3f} | signals={signal_weights} | confluence={confluence_bonus}"
                )
            except Exception:
                pass

        return score



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
        🎯 PIPELINE D'ANALYSE OPTIMISÉ (STRICT / NO FALLBACK)
        - Conserve les 4 indicateurs core
        - Supprime tout mappage 'fallback_*' : si pas de phase → 'no_clear_phase'
        - Conserve la volatilité pour reporting/diag, mais ne force plus de phase
        """

        try:
            n_bars = 0 if df is None else len(df)
            self.logger.info(f"🚀 SNIPER_X Optimized Pipeline - Processing {n_bars} bars")

            if df is None or df.empty:
                self.logger.error("DataFrame vide fourni à analyze()")
                return None

            # === PHASE 1: PRÉPARATION DONNÉES ===
            current_asset_symbol = asset_symbol or "UNKNOWN_ASSET"
            df_an = self.features.clean_dataframe(df.copy())
            if df_an is None or df_an.empty:
                self.logger.error("Échec du nettoyage DataFrame")
                return None

            # Nettoyage des colonnes prix
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

            # Colonnes requises
            required_columns = {
                "spread": 0.0,
                "point": 0.00001,
                "trade_tick_size": 0.00001,
                "trade_contract_size": 100000.0,
            }
            for col, default_val in required_columns.items():
                if col not in df_an.columns:
                    df_an[col] = float(default_val)
                    self.logger.warning(f"Colonne '{col}' ajoutée avec valeur par défaut")
                else:
                    df_an[col] = pd.to_numeric(df_an[col], errors="coerce").fillna(float(default_val))

            # Volatilité (utile au reporting mais ne force plus la phase)
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
                self.logger.warning(f"[{current_asset_symbol}] Échec calcul volatilité_pct: {e}")
                df_an["volatility_pct"] = 0.0

            # Volume momentum
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
                df_an["tick_volume"] = pd.to_numeric(df_an["tick_volume"], errors="coerce").fillna(0.0)
                df_an["volume_ma"] = df_an["tick_volume"].rolling(window=volume_ma_period, min_periods=1).mean()
                volume_mean_z = df_an["tick_volume"].rolling(window=volume_zscore_period, min_periods=1).mean()
                volume_std_z = df_an["tick_volume"].rolling(window=volume_zscore_period, min_periods=1).std(ddof=0).replace(0, np.nan)
                df_an["volume_zscore"] = ((df_an["tick_volume"] - volume_mean_z) / volume_std_z).replace([np.inf, -np.inf], 0.0).fillna(0.0)
                vol_std_ma = df_an["tick_volume"].rolling(window=volume_ma_period, min_periods=1).std(ddof=0).replace(0, np.nan)
                df_an["volume_momentum"] = ((df_an["tick_volume"] - df_an["volume_ma"]) / vol_std_ma).replace([np.inf, -np.inf], 0.0).fillna(0.0)
            else:
                self.logger.warning(f"Données volume insuffisantes pour {current_asset_symbol}")
                df_an["volume_zscore"] = 0.0
                df_an["volume_momentum"] = 0.0

            # === PHASE 2: CORE INDICATORS ===
            toggles = {}
            try:
                if getattr(self, "config_manager", None):
                    toggles = self.config_manager.get("phase_detection_defaults.detection_toggles", {}) or {}
            except Exception:
                toggles = {}

            if toggles.get("detect_regime", True):
                df_an["regime"] = self.detectors.detect_market_regime(df_an)
                df_an["regime_detected"] = True
            else:
                df_an["regime"] = "unknown"
                df_an["regime_detected"] = False
                df_an["regime_strength"] = 0.5

            if toggles.get("detect_fvg", True):
                df_an["fvg_details"] = self.detectors.detect_fvg_enhanced(df_an)
                df_an["fvg_detected"] = df_an["fvg_details"].apply(lambda x: x is not None)
            else:
                df_an["fvg_details"] = [None] * len(df_an)
                df_an["fvg_detected"] = False

            if toggles.get("detect_order_block", True):
                df_an["ob_details"] = self.detectors.detect_order_block_ml_enhanced(df_an)
                df_an["ob_detected"] = df_an["ob_details"].apply(lambda x: x is not None)
            else:
                df_an["ob_details"] = [None] * len(df_an)
                df_an["ob_detected"] = False

            if toggles.get("detect_bos_mss", True):
                df_an["bos_mss_details"] = self.detectors.detect_bos_mss_enhanced(df_an)
                df_an["bos_mss_detected"] = df_an["bos_mss_details"].apply(lambda x: x is not None)
            else:
                df_an["bos_mss_details"] = [None] * len(df_an)
                df_an["bos_mss_detected"] = False

            # === (NOUVEAU) MICROPHASES BOLLINGER – non intrusif, dernière barre uniquement ===
            if toggles.get("detect_bollinger", True):
                try:
                    # Initialiser colonnes
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

                    # pip_size via 'point' si dispo
                    pip_size = None
                    try:
                        if "point" in df_an.columns:
                            point_val = float(df_an["point"].iloc[-1])
                            pip_size = point_val * 10.0 if point_val > 0 else None
                    except Exception:
                        pip_size = None

                    boll = self.detectors.compute_bollinger_microphase_signals(
                        df_an,
                        price_col="close",
                        period=int(self.config_manager.get("phase_detection_defaults.bollinger.period", 20)),
                        std_mult=float(self.config_manager.get("phase_detection_defaults.bollinger.std_mult", 2.0)),
                        squeeze_window=int(self.config_manager.get("phase_detection_defaults.bollinger.squeeze_window", 100)),
                        squeeze_percentile=float(self.config_manager.get("phase_detection_defaults.bollinger.squeeze_percentile", 0.15)),
                        min_bars=int(self.config_manager.get("phase_detection_defaults.bollinger.min_bars", 200)),
                        atr_period=int(self.config_manager.get("phase_detection_defaults.bollinger.atr_period", 14)),
                        pip_size=pip_size,
                        mode="katana",
                    )

                    if isinstance(boll, dict) and boll.get("ok", False):
                        idx = df_an.index[-1]
                        df_an.loc[idx, "boll_signal"] = boll.get("signal")
                        df_an.loc[idx, "boll_band_touch"] = boll.get("band_touch")
                        df_an.loc[idx, "boll_in_band"] = float(bool(boll.get("in_band")))
                        df_an.loc[idx, "boll_is_squeeze"] = float(bool(boll.get("is_squeeze")))
                        df_an.loc[idx, "boll_is_expansion"] = float(bool(boll.get("is_expansion")))
                        df_an.loc[idx, "boll_breakout_score"] = float(boll.get("breakout_score", np.nan))
                        df_an.loc[idx, "boll_mean_revert_score"] = float(boll.get("mean_revert_score", np.nan))
                        df_an.loc[idx, "boll_z_band"] = (float(boll.get("z_band")) if boll.get("z_band") is not None else np.nan)
                        df_an.loc[idx, "boll_dist_to_upper_pips"] = (float(boll.get("dist_to_upper_pips", np.nan)) if boll.get("dist_to_upper_pips") is not None else np.nan)
                        df_an.loc[idx, "boll_dist_to_lower_pips"] = (float(boll.get("dist_to_lower_pips", np.nan)) if boll.get("dist_to_lower_pips") is not None else np.nan)
                        df_an.loc[idx, "boll_dist_to_mid_pips"] = (float(boll.get("dist_to_mid_pips", np.nan)) if boll.get("dist_to_mid_pips") is not None else np.nan)
                        df_an.loc[idx, "boll_bb_upper"] = (float(boll.get("bb_upper", np.nan)) if boll.get("bb_upper") is not None else np.nan)
                        df_an.loc[idx, "boll_bb_lower"] = (float(boll.get("bb_lower", np.nan)) if boll.get("bb_lower") is not None else np.nan)
                        df_an.loc[idx, "boll_bb_mid"] = (float(boll.get("bb_mid", np.nan)) if boll.get("bb_mid") is not None else np.nan)
                        df_an.loc[idx, "boll_atr_pips"] = (float(boll.get("atr_pips", np.nan)) if boll.get("atr_pips") is not None else np.nan)
                    else:
                        reason = ((boll or {}).get("reason") if isinstance(boll, dict) else "unknown")
                        self.logger.debug(f"[{current_asset_symbol}] Bollinger microphase non disponible: {reason}")
                except Exception as e:
                    self.logger.warning(
                        f"[{current_asset_symbol}] Erreur compute_bollinger_microphase_signals: {e}",
                        exc_info=False,
                    )

            # === PHASE 3: DÉTECTION LIQUIDITÉ ===
            try:
                indices_symbols = (
                    set(self.config_manager.get("global_safety.indices_symbols", ["US30", "NAS100"]))
                    if getattr(self, "config_manager", None)
                    else {"US30", "NAS100"}
                )
            except Exception:
                indices_symbols = {"US30", "NAS100"}

            if (asset_symbol or "UNKNOWN_ASSET") in indices_symbols:
                max_spread = float(self.config_manager.get("phase_detection_defaults.liquidity_detection.indices_settings.max_allowed_spread_points", 50)) if getattr(self, "config_manager", None) else 50.0
                min_volume = float(self.config_manager.get("phase_detection_defaults.liquidity_detection.indices_settings.min_volume_threshold", 10)) if getattr(self, "config_manager", None) else 10.0
            else:
                max_spread = float(self.config_manager.get("phase_detection_defaults.liquidity_detection.forex_settings.max_allowed_spread_points", 10)) if getattr(self, "config_manager", None) else 10.0
                min_volume = float(self.config_manager.get("phase_detection_defaults.liquidity_detection.forex_settings.min_volume_threshold", 1)) if getattr(self, "config_manager", None) else 1.0

            last_spread = float(df_an["spread"].iloc[-1]) if "spread" in df_an.columns else float("inf")
            last_volume = float(df_an["tick_volume"].iloc[-1]) if "tick_volume" in df_an.columns else 0.0
            df_an["is_liquid"] = (last_spread <= max_spread) and (last_volume >= min_volume)

            # === PHASE 4: SIGNAUX DE CONFLUENCE ===
            df_an["fvg_ob_confluence"] = df_an["fvg_detected"] & df_an["ob_detected"]
            df_an["high_quality_ob"] = df_an["ob_details"].apply(
                lambda x: isinstance(x, dict) and float(x.get("ml_score", 0.0)) > 0.8
            )
            df_an["confirmed_structure_break"] = df_an["bos_mss_details"].apply(
                lambda x: isinstance(x, dict) and float(x.get("volume_ratio", 0.0)) > 2.0
            )
            df_an["institutional_setup"] = df_an["regime"].astype(str).str.contains("institutional", na=False) & (df_an["ob_detected"] | df_an["bos_mss_detected"])

            # === PHASE 5: PHASE OPTIMISÉE — AUCUN FALLBACK ===
            df_an["phase_primary"] = df_an.apply(self.detectors.determine_optimized_phase, axis=1)
            df_an["phase"] = df_an["phase_primary"]
            df_an["phase_rule"] = "primary"
            df_an["phase_is_uncertain"] = (df_an["phase"] == "no_clear_phase")

            # === PHASE 6: SCORE DE CONFIANCE ===
            df_an["confidence_score"] = df_an.apply(
                lambda row: self.calculate_optimized_confidence(row),
                axis=1,
            )

            # === PHASE 7: MÉTRIQUES + LOG FINAL ===
            if not df_an.empty:
                try:
                    total_signals = df_an[["fvg_detected", "ob_detected", "bos_mss_detected"]].sum().sum()
                except Exception:
                    total_signals = 0
                avg_confidence = float(df_an["confidence_score"].mean()) if "confidence_score" in df_an.columns else 0.0
                last_phase = str(df_an["phase"].iloc[-1])
                last_confidence = float(df_an["confidence_score"].iloc[-1])
                last_regime = (str(df_an["regime"].iloc[-1]) if "regime" in df_an.columns else "unknown")
                last_vol = (float(df_an["volatility_pct"].iloc[-1]) if "volatility_pct" in df_an.columns else 0.0)
                last_rule = str(df_an["phase_rule"].iloc[-1]) if "phase_rule" in df_an.columns else "primary"

                self.logger.info(
                    f"🎯 [{current_asset_symbol}] Pipeline terminé: "
                    f"Phase={last_phase}, Confidence={last_confidence:.3f}, "
                    f"Régime={last_regime}, Signaux totaux={int(total_signals)}, "
                    f"Volatilité={last_vol:.3f}% | Rule={last_rule}"
                )

                # Info supplémentaire utile en mode strict
                if last_phase == "no_clear_phase":
                    self.logger.info(f"[{current_asset_symbol}] Phase indécise (no_clear_phase) — aucune règle de secours appliquée (strict).")

            return df_an
        except Exception as e:
            self.logger.error(f"analyze() failure: {e}", exc_info=True)
            return None


    def analyze_asset_multi_timeframe(
        self, asset: str, strategy_config: Dict
    ) -> Dict[str, Any]:
        """
        🏛️ ANALYSE MULTI-TIMEFRAME INSTITUTIONNELLE 🏛️
        Version "desk banque privée" :
        - Acquisition robuste + cache TTL par TF (avec contrôle de fraîcheur par TF)
        - Analyse par TF avec sauvegarde/restauration systématique des paramètres (try/finally)
        - Confluence & divergences + métriques de qualité
        - Poids de confluence normalisés et traçables
        - Journalisation et diagnostics (cache, fraîcheur, erreurs)
        - Fallbacks maîtrisés si données insuffisantes/obsolètes
        Signature et dépendances internes (helpers) conservées.
        """
        import time
        from typing import Dict as _Dict, Any as _Any
        import numpy as np
        import pandas as pd

        analysis_start_time = time.perf_counter()

        # ========== PHASE 1: VALIDATION & INITIALISATION ==========
        try:
            multi_tf_config = (strategy_config.get("phase_detection", {}) or {}).get("multi_timeframe", {}) or {}
        except Exception:
            multi_tf_config = {}

        if not bool(multi_tf_config.get("enabled", False)):
            self.logger.debug(f"[{asset}] Multi-TF désactivé, fallback analyse standard")
            return self._analyze_single_tf_fallback(asset)

        # Config avancée
        timeframes = multi_tf_config.get("timeframes", ["M1", "M5", "M15"]) or ["M1", "M5", "M15"]
        # Sanitize/unique
        timeframes = [str(tf).upper().strip() for tf in timeframes if str(tf).strip()]
        timeframes = list(dict.fromkeys(timeframes))  # preserve order, unique

        confluence_weights = multi_tf_config.get("confluence_weights", {"M1": 0.5, "M5": 0.3, "M15": 0.2}) or {"M1": 0.5, "M5": 0.3, "M15": 0.2}
        # Normalisation des poids (traçable)
        def _normalize_weights(d: _Dict[str, float]) -> _Dict[str, float]:
            w = {k.upper(): float(v) for k, v in d.items() if k}
            total = sum(max(0.0, v) for v in w.values())
            if total <= 0:
                # défaut proportionnel simple si erroné
                n = max(1, len(w))
                return {k: 1.0 / n for k in w}
            return {k: max(0.0, v) / total for k, v in w.items()}
        confluence_weights = _normalize_weights(confluence_weights)

        cache_ttl_seconds  = int(multi_tf_config.get("cache_ttl_seconds", 30) or 30)
        quality_threshold  = float(multi_tf_config.get("min_quality_score", 0.7) or 0.7)

        # Fraîcheur max par TF (défauts sensés) : si non fournie, ~2 bougies par TF
        tf_max_stale = multi_tf_config.get("max_tf_staleness_seconds", {}) or {}
        def _tf_to_seconds(tf: str) -> int:
            m = tf.upper()
            if m.endswith("MIN"):  # ex "1MIN"
                try:
                    return int(m[:-3]) * 60
                except Exception:
                    return 60
            if m.startswith("M"):
                try:
                    return int(m[1:]) * 60
                except Exception:
                    return 60
            if m.startswith("H"):
                try:
                    return int(m[1:]) * 3600
                except Exception:
                    return 3600
            return 60
        default_max_stale_by_tf = {tf: 2 * _tf_to_seconds(tf) for tf in timeframes}
        # merge override user
        for k, v in list(tf_max_stale.items()):
            try:
                default_max_stale_by_tf[str(k).upper()] = int(v)
            except Exception:
                pass

        self.logger.info(f"🎯 [{asset}] KATANA Multi-TF activé: {timeframes}")

        # ========== PHASE 2: ACQUISITION DONNÉES AVEC CACHE INTELLIGENT ==========
        tf_data_cache: Dict[str, pd.DataFrame] = {}
        cache_hits = 0
        data_freshness: Dict[str, _Dict[str, _Any]] = {}

        now_epoch = int(time.time())
        now_bucket = int(now_epoch // max(1, cache_ttl_seconds))

        for tf in timeframes:
            cache_key = f"{asset}_{tf}_{now_bucket}"

            # Vérification cache
            cached_ok = False
            if hasattr(self, "_tf_data_cache"):
                cached_item = getattr(self, "_tf_data_cache", {}).get(cache_key)
                if cached_item is not None:
                    # accepter pd.DataFrame direct (compat) ou dict {"data": df, "fetched_at": ts}
                    if isinstance(cached_item, pd.DataFrame):
                        tf_data_cache[tf] = cached_item
                        cache_hits += 1
                        cached_ok = True
                        self.logger.debug(f"🚀 [{asset}] Cache HIT pour {tf}")
                    elif isinstance(cached_item, dict) and isinstance(cached_item.get("data"), pd.DataFrame):
                        tf_data_cache[tf] = cached_item["data"]
                        cache_hits += 1
                        cached_ok = True
                        self.logger.debug(f"🚀 [{asset}] Cache HIT(meta) pour {tf}")

            if not cached_ok:
                # Acquisition données fraîches
                try:
                    tf_data = self._fetch_timeframe_data(asset, tf, multi_tf_config)
                    if tf_data is not None and not tf_data.empty:
                        tf_data_cache[tf] = tf_data
                        # Mise à jour cache
                        if not hasattr(self, "_tf_data_cache"):
                            self._tf_data_cache = {}
                        self._tf_data_cache[cache_key] = {"data": tf_data, "fetched_at": now_epoch}
                        self.logger.debug(f"📡 [{asset}] Données {tf} acquises: {len(tf_data)} barres")
                    else:
                        self.logger.warning(f"⚠️ [{asset}] Échec acquisition {tf}")
                        continue
                except Exception as e:
                    self.logger.error(f"💥 [{asset}] Erreur critique fetch {tf}: {e}", exc_info=False)
                    continue

            # Contrôle de fraîcheur de la DERNIÈRE barre par TF
            try:
                df_tf = tf_data_cache[tf]
                ts_col = None
                for c in ("timestamp", "time", "datetime", "ts"):
                    if c in df_tf.columns:
                        ts_col = c
                        break
                last_dt = None
                if ts_col is not None:
                    last_ts = df_tf[ts_col].iloc[-1]
                    from datetime import datetime, timezone
                    if hasattr(last_ts, "to_pydatetime"):
                        last_dt = last_ts.to_pydatetime()
                    elif isinstance(last_ts, (int, float)) and last_ts > 1e9:
                        last_dt = datetime.fromtimestamp(float(last_ts) / 1000.0, tz=timezone.utc)
                    elif isinstance(last_ts, (int, float)):
                        last_dt = datetime.fromtimestamp(float(last_ts), tz=timezone.utc)
                    else:
                        last_dt = datetime.fromisoformat(str(last_ts))
                        if last_dt.tzinfo is None:
                            last_dt = last_dt.replace(tzinfo=timezone.utc)
                    age_sec = max(0.0, (datetime.now(timezone.utc) - last_dt).total_seconds())
                else:
                    # fallback via index si datetime-like
                    idx_last = df_tf.index[-1]
                    try:
                        from datetime import datetime, timezone
                        if hasattr(idx_last, "to_pydatetime"):
                            last_dt = idx_last.to_pydatetime()
                            age_sec = max(0.0, (datetime.now(timezone.utc) - last_dt).total_seconds())
                        else:
                            age_sec = 0.0  # inconnu => on n’invalide pas
                    except Exception:
                        age_sec = 0.0

                max_stale = int(default_max_stale_by_tf.get(tf, 120))
                data_freshness[tf] = {"age_sec": float(age_sec), "max_allowed": float(max_stale)}
                if age_sec > max_stale:
                    self.logger.warning(f"⚠️ [{asset}] {tf} stale: {age_sec:.1f}s > {max_stale}s (skipped)")
                    # retire ce TF trop vieux
                    tf_data_cache.pop(tf, None)
            except Exception as e:
                self.logger.debug(f"[{asset}] Freshness check fail {tf}: {e}")

        # Vérification intégrité données
        if len(tf_data_cache) < 2:
            self.logger.warning(f"⚠️ [{asset}] Données insuffisantes ({len(tf_data_cache)}/{len(timeframes)}) pour Multi-TF")
            return self._analyze_single_tf_fallback(asset)

        cache_efficiency = (cache_hits / max(1, len(timeframes))) * 100.0
        self.logger.debug(f"📊 [{asset}] Cache efficiency: {cache_efficiency:.1f}%")

        # ========== PHASE 3: ANALYSE VECTORIELLE PAR TF ==========
        tf_analyses: Dict[str, Dict[str, Any]] = {}
        analysis_errors: list[str] = []

        for tf, tf_data in tf_data_cache.items():
            # Config spécifique TF
            tf_config = self._get_tf_specific_config(tf, multi_tf_config)
            original_params = self._backup_current_params()
            try:
                # Override temporaire
                self._load_settings(overrides=tf_config)

                # Analyse
                analyzed_data = self.analyze(tf_data, asset_symbol=asset)
                if analyzed_data is None or analyzed_data.empty:
                    raise ValueError(f"Analyse {tf} retournée vide")

                # Extraction signaux dernière barre
                last_signals = self._extract_last_bar_signals(analyzed_data, tf)
                tf_analyses[tf] = last_signals
                self.logger.debug(f"✅ [{asset}] {tf} analysé: Phase={last_signals.get('phase')}")
            except Exception as e:
                analysis_errors.append(f"{tf}: {str(e)}")
                self.logger.error(f"💥 [{asset}] Erreur analyse {tf}: {e}", exc_info=False)
            finally:
                # Restauration systématique
                try:
                    self._restore_params(original_params)
                except Exception as e:
                    self.logger.error(f"💥 [{asset}] Restore params {tf} échoué: {e}", exc_info=False)

        # ========== PHASE 4: FUSION & CONFLUENCE ==========
        if len(tf_analyses) < 2:
            self.logger.warning(f"⚠️ [{asset}] Analyses insuffisantes pour confluence")
            return self._analyze_single_tf_fallback(asset)

        # Harmoniser les poids aux TF réellement présents
        present_weights = {tf: confluence_weights.get(tf, 0.0) for tf in tf_analyses.keys()}
        present_weights = _normalize_weights(present_weights)

        confluence_result = self._calculate_advanced_confluence(tf_analyses, present_weights, asset)

        # Détection divergences inter-TF
        divergence_analysis = self._detect_tf_divergences(tf_analyses)

        # ========== PHASE 5: SCORING QUALITÉ & MÉTRIQUES ==========
        quality_metrics = self._calculate_quality_metrics(tf_analyses, confluence_result, divergence_analysis, analysis_start_time)

        # Filtrage qualité
        if float(quality_metrics.get("overall_score", 0.0)) < quality_threshold:
            self.logger.warning(
                f"⚠️ [{asset}] Qualité insuffisante ({float(quality_metrics.get('overall_score', 0.0)):.3f} < {quality_threshold})"
            )
            lowq_resp = self._build_low_quality_response(asset, quality_metrics)
            # enrichir d’un minimum d’infos MTF pour transparence
            try:
                lowq_resp.update({
                    "multi_tf_enabled": True,
                    "timeframes_used": list(tf_analyses.keys()),
                    "confluence_weights": present_weights,
                    "analysis_errors": analysis_errors or None,
                    "cache_efficiency_pct": round(cache_efficiency, 1),
                    "data_freshness": data_freshness,
                })
            except Exception:
                pass
            return lowq_resp

        # ========== PHASE 6: CONSTRUCTION RÉPONSE FINALE ==========
        final_signals = self._build_enhanced_signals(confluence_result, quality_metrics, tf_analyses, asset)

        # Enrichissements diagnostics (audit-ready)
        try:
            final_signals.update({
                "multi_tf_enabled": True,
                "timeframes_used": list(tf_analyses.keys()),
                "confluence_weights": present_weights,
                "analysis_errors": analysis_errors or None,
                "cache_efficiency_pct": round(cache_efficiency, 1),
                "data_freshness": data_freshness,
            })
        except Exception:
            pass

        execution_time = (time.perf_counter() - analysis_start_time) * 1000.0
        self.logger.info(
            f"🎯 [{asset}] KATANA Multi-TF terminé: "
            f"Phase={final_signals.get('phase')} | "
            f"Qualité={quality_metrics.get('overall_score', 0.0):.3f} | "
            f"TFs={final_signals.get('timeframes_used')} | "
            f"Temps={execution_time:.1f}ms"
        )

        return final_signals


    def get_katana_snapshot(self, asset: str, strategy_config: dict) -> dict:
        """
        Snapshot micro-décisionnel prêt pour le pipeline (M1 dirigé par BOS/MSS, alignement M5/M15,
        SL/TP structurels, spread/liquidité, score final, katana_ready).

        Version "desk banque privée" : contrôles renforcés, gates explicites, scoring stable, logs propres.
        Signature conservée à l’identique.
        """
        # =========================
        # 0) CONFIGS & GUARDRAILS
        # =========================
        pd_cfg   = (strategy_config.get("phase_detection") or {}) if isinstance(strategy_config, dict) else {}
        mtf_cfg  = (pd_cfg.get("multi_timeframe") or {})
        kat_cfg  = (pd_cfg.get("katana") or {})

        # Paramètres Katana (défauts prudents)
        max_age_sec          = int(kat_cfg.get("max_signal_age_seconds", 30) or 30)
        max_bos_age_bars     = int(kat_cfg.get("max_bos_age_bars", 3) or 3)
        min_atr_m1_pips      = float(kat_cfg.get("min_atr_m1_pips", 0.60) or 0.60)
        hard_min_atr_m1_pips = float(kat_cfg.get("hard_min_atr_m1_pips", 0.12) or 0.12)
        min_final_score      = float(kat_cfg.get("min_final_score", 0.55) or 0.55)
        min_rr_required      = float(kat_cfg.get("min_rr_required", 1.20) or 1.20)

        # Poids score final
        score_weights = kat_cfg.get("score_weights", {"mtf": 0.6, "m1": 0.4})
        w_mtf = float(score_weights.get("mtf", 0.6))
        w_m1  = float(score_weights.get("m1", 0.4))

        # Bonus / pénalités
        htf_alignment_bonus = float(kat_cfg.get("htf_alignment_bonus", 0.15) or 0.15)
        atr_low_penalty     = float(kat_cfg.get("atr_low_penalty", -0.10) or -0.10)
        spread_penalty      = float(kat_cfg.get("spread_penalty", -0.05) or -0.05)

        points_per_pip = 10.0  # standard FX

        # ===============================
        # 1) ANALYSE MTF (confluence HTF)
        # ===============================
        mtf = (
            self.analyze_asset_multi_timeframe(asset, strategy_config)
            if hasattr(self, "analyze_asset_multi_timeframe")
            else {}
        )
        if not mtf or not mtf.get("multi_tf_enabled", False):
            return {"katana_ready": False, "reason": "insufficient_confluence"}

        # =======================================
        # 2) ACQUISITION & ANALYSE M1 FRAÎCHE
        # =======================================
        m1_raw = self._fetch_timeframe_data(asset, "M1", mtf_cfg) if hasattr(self, "_fetch_timeframe_data") else None
        m1_df  = self.analyze(m1_raw, asset_symbol=asset) if m1_raw is not None else None
        if m1_df is None or m1_df.empty:
            return {"katana_ready": False, "reason": "m1_analysis_failed"}

        last = m1_df.iloc[-1]

        # Fraîcheur du signal
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        ts_col = None
        for c in ("timestamp", "time", "datetime", "ts"):
            if c in m1_df.columns:
                ts_col = c
                break
        if ts_col:
            try:
                last_ts = last[ts_col]
                if hasattr(last_ts, "to_pydatetime"):
                    last_dt = last_ts.to_pydatetime()
                elif isinstance(last_ts, (int, float)) and last_ts > 1e9:
                    last_dt = datetime.fromtimestamp(float(last_ts) / 1000.0, tz=timezone.utc)
                elif isinstance(last_ts, (int, float)):
                    last_dt = datetime.fromtimestamp(float(last_ts), tz=timezone.utc)
                else:
                    last_dt = datetime.fromisoformat(str(last_ts))
                    if last_dt.tzinfo is None:
                        last_dt = last_dt.replace(tzinfo=timezone.utc)
                age_sec = (now - last_dt).total_seconds()
                if age_sec > max_age_sec:
                    return {"katana_ready": False, "reason": f"stale_m1_bar_{int(age_sec)}s"}
            except Exception:
                # si on ne peut pas déterminer l'âge, on ne bloque pas ici
                pass

        # ====================================================
        # 3) DIRECTION M1 (BOS/MSS) + ÂGE DU BREAK EN BARRES
        # ====================================================
        side, break_ok = self._extract_m1_break_direction(last)
        if side is None or not break_ok:
            return {"katana_ready": False, "reason": "no_m1_break"}

        bos_age_bars = None
        # 3.a) colonnes dédiées si présentes
        for k in ("bos_mss_age_bars", "m1_break_age_bars", "break_age"):
            if k in m1_df.columns:
                try:
                    bos_age_bars = int(m1_df[k].iloc[-1])
                    break
                except Exception:
                    pass
        # 3.b) fallback: cherche le dernier index avec bos_mss_detected True ou bos_mss_details non-nul
        if bos_age_bars is None:
            try:
                if "bos_mss_detected" in m1_df.columns:
                    idx_last_break = m1_df.index[m1_df["bos_mss_detected"]].max()
                elif "bos_mss_details" in m1_df.columns:
                    idx_last_break = m1_df["bos_mss_details"].last_valid_index()
                else:
                    idx_last_break = None
                if idx_last_break is not None:
                    bos_age_bars = int(len(m1_df) - 1 - m1_df.index.get_loc(idx_last_break))
            except Exception:
                bos_age_bars = None

        if bos_age_bars is not None and bos_age_bars > max_bos_age_bars:
            return {"katana_ready": False, "reason": f"stale_break_{bos_age_bars}bars"}

        # ==========================================
        # 4) ALIGNEMENT HTF (M5/M15) SUR LA PHASE
        # ==========================================
        phase = str(mtf.get("phase", "unknown") or "unknown").lower()
        htf_alignment_ok = (side == "BUY" and ("bull" in phase or "up" in phase)) or \
                        (side == "SELL" and ("bear" in phase or "down" in phase))

        # ===================================
        # 5) GATE LIQUIDITÉ / SPREAD / VOLUME
        # ===================================
        # Déduction de la classe d’actif (indices vs forex) pour seuils par défaut
        try:
            indices_symbols = set(self.config_manager.get("global_safety.indices_symbols", ["US30", "NAS100"])) if getattr(self, "config_manager", None) else {"US30", "NAS100"}
        except Exception:
            indices_symbols = {"US30", "NAS100"}
        is_index = (asset in indices_symbols)

        # Seuils (prennent la config si dispo)
        if is_index:
            max_spread_points = float(self.config_manager.get(
                "phase_detection_defaults.liquidity_detection.indices_settings.max_allowed_spread_points", 50
            )) if getattr(self, "config_manager", None) else 50.0
            min_volume_th = float(self.config_manager.get(
                "phase_detection_defaults.liquidity_detection.indices_settings.min_volume_threshold", 10
            )) if getattr(self, "config_manager", None) else 10.0
        else:
            max_spread_points = float(self.config_manager.get(
                "phase_detection_defaults.liquidity_detection.forex_settings.max_allowed_spread_points", 10
            )) if getattr(self, "config_manager", None) else 10.0
            min_volume_th = float(self.config_manager.get(
                "phase_detection_defaults.liquidity_detection.forex_settings.min_volume_threshold", 1
            )) if getattr(self, "config_manager", None) else 1.0

        # Volume zscore minimal (si dispo)
        try:
            vol_z_min = float(self.config_manager.get(
                "phase_detection_defaults.regime_detection_settings.volume_profile.min_volume_zscore_for_scalp", 1.2
            )) if getattr(self, "config_manager", None) else 1.2
        except Exception:
            vol_z_min = 1.2

        # Valeurs dernières
        last_spread = float(last["spread"]) if "spread" in m1_df.columns and math.isfinite(last["spread"]) else float("inf")
        last_volume = float(last["tick_volume"]) if "tick_volume" in m1_df.columns and math.isfinite(last["tick_volume"]) else 0.0
        last_volz   = float(last.get("volume_zscore", float("nan"))) if "volume_zscore" in m1_df.columns else float("nan")

        # Si pas de zscore présent, calcule rapide (fenêtre 50)
        if not math.isfinite(last_volz):
            try:
                vol = m1_df["tick_volume"].astype(float)
                mean50 = vol.rolling(50, min_periods=5).mean()
                std50  = vol.rolling(50, min_periods=5).std(ddof=0).replace(0, math.nan)
                last_volz = float(((vol.iloc[-1] - mean50.iloc[-1]) / std50.iloc[-1])) if math.isfinite(std50.iloc[-1]) else 0.0
            except Exception:
                last_volz = 0.0

        spread_ok = last_spread <= max_spread_points
        volume_ok = (last_volume >= min_volume_th) and (last_volz >= vol_z_min)

        # ============================
        # 6) ATR M1 (anti marchés morts)
        # ============================
        def _calc_atr(df_in: pd.DataFrame, period: int = 14) -> float:
            if df_in is None or len(df_in) < period + 2:
                return float("nan")
            high = df_in["high"].astype(float)
            low  = df_in["low"].astype(float)
            close= df_in["close"].astype(float)
            prev_close = close.shift(1)
            tr = np.maximum.reduce([(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()])
            atr = tr.rolling(window=period, min_periods=period).mean().iloc[-1]
            return float(atr) if pd.notna(atr) and atr > 0 else float("nan")

        if "atr14" in m1_df.columns and isinstance(m1_df["atr14"].iloc[-1], (int, float)):
            atr_m1 = float(m1_df["atr14"].iloc[-1])
        else:
            atr_m1 = _calc_atr(m1_df, 14)

        # point -> pip_size
        point = 0.0
        try:
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
                return {"katana_ready": False, "reason": f"atr_m1_too_low_{atr_m1_pips:.3f}pips"}
            low_atr_flag = atr_m1_pips < min_atr_m1_pips
        else:
            atr_m1_pips = None
            low_atr_flag = False  # inconnu => pas de pénalité dure

        # ============================
        # 7) PRIX D’ENTRÉE / SL / TP
        # ============================
        entry = float(last["close"])
        sl = self._pick_sl_from_structure(last, side)
        tp = self._pick_tp_from_nearest_liquidity(m1_df, side)

        if sl is None or tp is None or not math.isfinite(entry) or entry <= 0:
            return {"katana_ready": False, "reason": "invalid_prices"}

        # R:R (reward/risk) + mesures en pips si possible
        if side == "BUY":
            risk   = entry - float(sl)
            reward = float(tp) - entry
        else:
            risk   = float(sl) - entry
            reward = entry - float(tp)

        rr = float(reward / risk) if (risk is not None and risk > 0) else float("nan")

        risk_pips = (risk / pip_size) if (pip_size and math.isfinite(risk)) else None
        reward_pips = (reward / pip_size) if (pip_size and math.isfinite(reward)) else None

        # ============================
        # 8) SCORE FINAL & FLAGS
        # ============================
        conf_m1  = float(last.get("confidence_score", 0.0)) if "confidence_score" in m1_df.columns else 0.0
        conf_mtf = float(mtf.get("confidence_score", 0.0))

        katana_score = (w_mtf * conf_mtf) + (w_m1 * conf_m1)
        if htf_alignment_ok:
            katana_score += htf_alignment_bonus
        if low_atr_flag:
            katana_score += atr_low_penalty
        if not spread_ok:
            katana_score += spread_penalty

        # clamp
        katana_score = round(max(0.0, min(0.98, katana_score)), 3)

        # ============================
        # 9) MICRO-PHASE (optionnel)
        # ============================
        snapshot_signals = {}
        try:
            micro_cfg = self.config_manager.get("features.micro_phase", {}) or {}
            if bool(micro_cfg.get("enabled", True)) and hasattr(self, "detect_micro_phase_m1"):
                hint = self.detect_micro_phase_m1(m1_df, params=micro_cfg.get("params"))
                snapshot_signals["micro_phase_hint"] = hint
                # petit boost si la direction micro confirme
                try:
                    if hint.get("micro_phase") and hint.get("confidence_boost", 0) > 0:
                        if (side == "BUY" and hint.get("direction") == "BUY") or (side == "SELL" and hint.get("direction") == "SELL"):
                            katana_score = min(0.98, float(katana_score) + float(hint["confidence_boost"]))
                except Exception:
                    pass
        except Exception as _e:
            self.logger.debug(f"[{asset}] micro_phase hint non appliqué: {_e}")

        # ======================================
        # 10) DÉCISION TRADABLE & RAISONS CLAIRES
        # ======================================
        reasons = []
        is_tradable = True

        if not htf_alignment_ok:
            is_tradable = False
            reasons.append("htf_misaligned")

        if not spread_ok:
            is_tradable = False
            reasons.append(f"spread_too_wide_{last_spread:.2f}>{max_spread_points}")

        if not volume_ok:
            is_tradable = False
            reasons.append(f"volume_weak_vz{last_volz:.2f}<min{vol_z_min:.2f}")

        if math.isnan(rr) or rr < min_rr_required:
            is_tradable = False
            reasons.append(f"rr_too_low_{0 if math.isnan(rr) else round(rr,2)}<min{min_rr_required}")

        if katana_score < min_final_score:
            is_tradable = False
            reasons.append(f"score_below_min_{katana_score:.2f}<min{min_final_score}")

        # ============================
        # 11) SNAPSHOT FINAL
        # ============================
        snapshot = {
            "asset": asset,
            "entry_side": side,                         # "BUY" / "SELL"
            "entry_price": entry,
            "sl_price": float(sl),
            "tp_price": float(tp),
            "risk_reward": None if math.isnan(rr) else round(rr, 3),
            "risk_pips": float(risk_pips) if risk_pips is not None else None,
            "reward_pips": float(reward_pips) if reward_pips is not None else None,

            "m1_break_ok": bool(break_ok),
            "bos_age_bars": int(bos_age_bars) if bos_age_bars is not None else None,

            "htf_alignment_ok": bool(htf_alignment_ok),
            "spread_ok": bool(spread_ok),
            "volume_ok": bool(volume_ok),

            "atr_m1_pips": float(atr_m1_pips) if atr_m1_pips is not None else None,
            "katana_score": float(katana_score),

            "phase": mtf.get("phase"),
            "dominant_tf": mtf.get("dominant_tf"),
            "signal_agreement": mtf.get("signal_agreement_rates", {}),

            "max_signal_age_seconds": max_age_sec,
            "max_bos_age_bars": max_bos_age_bars,

            "last_spread_points": float(last_spread) if math.isfinite(last_spread) else None,
            "last_volume": float(last_volume),
            "last_volume_zscore": float(last_volz),

            "volatility_pct": float(last.get("volatility_pct", float("nan"))) if "volatility_pct" in m1_df.columns else None,

            "signals": snapshot_signals if snapshot_signals else None,
            "reasons": reasons if reasons else None,
        }

        snapshot["katana_ready"] = all(
            [
                snapshot["m1_break_ok"],
                snapshot["htf_alignment_ok"],
                snapshot["spread_ok"],
                snapshot["volume_ok"],
                snapshot["sl_price"] is not None,
                snapshot["tp_price"] is not None,
                snapshot["katana_score"] >= min_final_score,
                (snapshot["risk_reward"] is not None and snapshot["risk_reward"] >= min_rr_required),
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
                
      
        
    def end_of_day_phase_report(self, symbol: str, frames_by_tf: dict):
        """
        frames_by_tf: dict { "M1": df_m1, "M5": df_m5, "M15": df_m15 }
        """
        reporter = PhaseObserverReporter(config_manager=self.config_manager, logger=self.logger)
        report = reporter.run_daily_report_for_asset(symbol, frames_by_tf, tz=timezone.utc)

        date_tag = str(datetime.now(timezone.utc).date())
        base_dir = self.config_manager.get("reporting.base_dir", "reports")
        os.makedirs(base_dir, exist_ok=True)

        md_path = os.path.join(base_dir, f"{symbol}_{date_tag}_phase_report.md")
        jsonl_path = os.path.join(base_dir, f"{symbol}_{date_tag}_phase_report.jsonl")

        reporter.export_markdown(report, md_path)
        reporter.export_jsonl(report, jsonl_path)
        self.logger.info(f"[REPORT] {symbol} → {md_path} | {jsonl_path}")
        
