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
from typing import Dict as _Dict, Any as _Any

# Third-party
import numpy as np
import pandas as pd
import os

# Local
from .types import (
    Direction,
    Phase,
    PhaseSignal,
    PhaseSnapshot,
    MarketFeatures,
    PhaseMemory,
)

from .features import FeaturesExtractor  
from .detectors import Detectors
from reporter import PhaseObserverReporter
from datetime import datetime, timezone
from .memory import PhaseMemoryManager


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
        self.features = FeaturesExtractor(
            config_manager=config_manager, logger=self.logger
        )

        # === Instance unique de Detectors ===
        self.detectors = Detectors(config_manager=config_manager)

        # ✅ Buffer pour accumuler les ticks de la bougie courante
        from collections import deque

        self._ticks_current_bar = deque(maxlen=10000)

        self._history_df = None  # sera rempli au démarrage avec 200 barres

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
        # === [ICT DETECTORS DÉSACTIVÉS - 31 DEC 2025] ===
        self.detect_fvg = False  # detect_fvg_enhanced supprimé
        self.detect_order_block = False  # detect_order_block_ml_enhanced supprimé
        self.detect_bos_mss = False  # detect_bos_mss_enhanced supprimé
        self.detect_liquidity_grab = True  # Sweep/absorption vectorisés (pas de détecteur)
        self.detect_eqh_eql = False  # detect_eqh_eql supprimé
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
        # === Config Footprint ===
        try:
            self.footprint_cfg = self.config_manager.get("footprint_settings", {}) or {}
        except Exception:
            self.footprint_cfg = {}

        # Valeurs par défaut
        self.footprint_cfg.setdefault("enable_live_footprint", True)
        self.footprint_cfg.setdefault("enable_final_footprint", True)
        self.footprint_cfg.setdefault("max_buffer_ticks", 500)
        self.footprint_cfg.setdefault("delta_threshold", 0.0)
        self.footprint_cfg.setdefault("poc_min_volume", 0)
        self.footprint_cfg.setdefault("store_to_memory", True)

        # === Ajout mémoire des phases ===
        from .memory import PhaseMemoryManager

        self.memory = PhaseMemoryManager()
      
    def _coerce_val(self, v):
        """
        Normalise une valeur en sortie "propre".
        - None, NaN, NaT, inf → pd.NA
        - Types sûrs (int, float, str, bool, datetime, np.generic) conservés
        - Sinon → pd.NA
        """
        try:
            if v is None:
                return pd.NA
            if isinstance(v, float) and (np.isnan(v) or np.isinf(v)):
                return pd.NA
            if pd.isna(v):
                return pd.NA
        except Exception:
            return pd.NA

        if isinstance(v, (float, int, str, bool, pd.Timestamp, datetime, np.generic)):
            return v

        return pd.NA

    def _ensure_footprint_columns(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Force l’existence des colonnes footprint avec dtype=object.
        Ajoute les colonnes manquantes si besoin.
        """
        needed = (
            "footprint_score",
            "footprint_status",
            "footprint_summary",
            "footprint_live_delta",
            "footprint_live_poc",
        )
        for col in needed:
            if col not in df.columns:
                df[col] = pd.Series(pd.NA, index=df.index, dtype="object")
            elif df[col].dtype != object:
                df[col] = df[col].astype("object")
        return df

    # ================================================================
    # Méthode: load_initial_history
    # ================================================================
    def load_initial_history(
        self, df: pd.DataFrame, asset_symbol: str = "INIT", max_bars: int = 200
    ):
        """
        Charge un historique initial propre (pour démarrage ou resync périodique) :
        - conversion datetime
        - suppression doublons
        - limitation à max_bars (FIFO)
        - colonnes footprint ajoutées
        - recalcul pipeline complet uniquement sur cet historique initial
        """
        try:
            if df is None or not isinstance(df, pd.DataFrame) or df.empty:
                raise ValueError("load_initial_history: DataFrame invalide")

            df = df.copy()
            df.replace([np.inf, -np.inf], pd.NA, inplace=True)
            df = df.where(pd.notna(df), pd.NA)

            # Colonne ou index temps
            if "time" in df.columns:
                df["time"] = pd.to_datetime(df["time"], errors="coerce")
                df = df.dropna(subset=["time"])
                df = df.set_index("time")
            else:
                df.index = pd.to_datetime(df.index, errors="coerce")
                if df.index.isna().any():
                    df.index = pd.date_range(
                        end=pd.Timestamp.utcnow(), periods=len(df), freq="T"
                    )
                    self.logger.warning(
                        "[load_initial_history] index non-datetime -> remplacé par range minute"
                    )

            # Nettoyage index
            df = df.sort_index()
            df = df[~df.index.duplicated(keep="last")]
            df = df.infer_objects(copy=False)

            # Limiter à max_bars (FIFO sur les plus récentes)
            if len(df) > max_bars:
                df = df.tail(max_bars)

            # Colonnes footprint
            df = self._ensure_footprint_columns(df)

            # Sauvegarde + analyse initiale
            self._history_df = self.analyze(df.copy(), asset_symbol=asset_symbol)
            return self._history_df

        except Exception as e:
            self.logger.exception(f"[load_initial_history] erreur: {e}")
            raise

    def calculate_optimized_confidence(self, row) -> float:
        """Score de confiance unifié (core + confluence + bougies + signaux liquidity + qualité + lissage mémoire)."""

        # --- 1) Lecture config ---
        try:
            cfg = (
                self.config_manager.get("confidence_score_calculation", None)
                or self.config_manager.get(
                    "phase_detection_defaults.confidence_score_calculation", None
                )
                or self.config_manager.get(
                    "phase_detection_defaults.confidence_scoring", None
                )
                or {}
            )
            cfg_path_used = "config_loaded"
        except Exception:
            cfg, cfg_path_used = {}, "fallback"

        # --- 2) Normalisation des poids ---
        signal_weights = cfg.get("signal_weights")
        if not isinstance(signal_weights, dict):
            weights = cfg.get("weights", {})
            signal_weights = {
                "fvg_detected": float(
                    weights.get("fvg", weights.get("fvg_detected", 0.25))
                ),
                "ob_detected": float(
                    weights.get("ob", weights.get("ob_detected", 0.35))
                ),
                "bos_mss_detected": float(
                    weights.get("bos_mss", weights.get("bos_mss_detected", 0.25))
                ),
                "regime_alignment": float(
                    weights.get("regime", weights.get("regime_alignment", 0.15))
                ),
            }

        confluence_bonus = cfg.get("confluence_bonus", cfg.get("confluence", {})) or {}
        quality_multipliers = (
            cfg.get("quality_factors", cfg.get("quality_multipliers", {})) or {}
        )

        base_confidence = float(cfg.get("base_confidence", cfg.get("base", 0.2)))
        max_confidence = float(cfg.get("max_confidence_cap", cfg.get("cap", 0.95)))

        # --- 3) Score de base ---
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

        # --- 3bis) Signaux spécifiques liquidity ---
        if bool(row.get("sweep_detected", False)):
            score += float(cfg.get("liquidity_weights", {}).get("sweep_detected", 0.35))
        if bool(row.get("absorption_confirmed", False)):
            score += float(
                cfg.get("liquidity_weights", {}).get("absorption_confirmed", 0.25)
            )
        if bool(row.get("eqh_eql_detected", False)):
            score += float(
                cfg.get("liquidity_weights", {}).get("eqh_eql_detected", 0.20)
            )

        # --- 4) Bonus confluence ---
        if bool(row.get("fvg_ob_confluence", False)):
            score += confluence_bonus.get("fvg_ob_confluence", 0.15)
        if bool(row.get("high_quality_ob", False)) and bool(
            row.get("bos_mss_detected", False)
        ):
            score += confluence_bonus.get("ob_bos_confluence", 0.10)
        if bool(row.get("institutional_setup", False)):
            score += confluence_bonus.get("full_confluence_bonus", 0.20)

        # === [SECTION BOUGIES SUPPRIMÉE - Session 23 Nov 2025] ===
        # Supprimé : scoring basé sur candle_pattern (30 lignes)
        # Raison : Détecteurs de patterns de bougies retirés du système

        # --- 5) Multiplicateurs qualité ---
        if bool(row.get("is_liquid", True)):
            score *= quality_multipliers.get("tight_spread", 1.05)
        if regime_strength > 0.8:
            score *= quality_multipliers.get("regime_strength", 1.10)

        try:
            ob_details = row.get("ob_details")
            bos_details = row.get("bos_mss_details")
            if (
                isinstance(ob_details, dict)
                and float(ob_details.get("volume_spike", 0) or 0) > 1.5
            ):
                score *= quality_multipliers.get("high_volume_confirmation", 1.15)
            elif (
                isinstance(bos_details, dict)
                and float(bos_details.get("volume_ratio", 0) or 0) > 1.5
            ):
                score *= quality_multipliers.get("high_volume_confirmation", 1.15)
        except Exception:
            pass

        # --- 7) Normalisation dynamique ---
        score = max(0.0, min(max_confidence, score))

        # --- 8) EMA smoothing avec mémoire ---
        try:
            prev_conf = getattr(self.memory, "last_confidence", None)
            if prev_conf is not None:
                alpha = 0.3
                score = (alpha * score) + ((1 - alpha) * prev_conf)
            self.memory.last_confidence = score
        except Exception:
            pass

        # --- 9) Logging debug ---
        if getattr(self, "debug_confidence_logging", False):
            try:
                self.logger.debug(
                    f"[CONF-clean] path='{cfg_path_used}' score={score:.3f}"
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
        self,
        df: pd.DataFrame,
        asset_symbol: Optional[str] = None,
        ticks: Optional[pd.DataFrame] = None,
    ) -> Optional[pd.DataFrame]:
        """
        🎯 PIPELINE D'ANALYSE OPTIMISÉ (STRICT / NO FALLBACK)
        - Conserve les 4 indicateurs core
        - Conserve la volatilité pour reporting/diag, mais ne force plus de phase
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
            df_an = self.features.clean_dataframe(df.copy())
            if df_an is None or df_an.empty:
                self.logger.error("Échec du nettoyage DataFrame")
                return None

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

            # Volatilité
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

            if "tick_volume" in df_an.columns and len(df_an) >= volume_zscore_period:
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
                df_an["regime"] = self.detectors.detect_market_regime(df_an)
                df_an["regime_detected"] = True
            else:
                df_an["regime"] = "unknown"
                df_an["regime_detected"] = False
                df_an["regime_strength"] = 0.5

            # === [ICT DETECTORS SUPPRIMÉS - 31 DEC 2025] ===
            # detect_fvg_enhanced, detect_order_block_ml_enhanced, detect_bos_mss_enhanced retirés
            df_an["fvg_details"] = [None] * len(df_an)
            df_an["fvg_detected"] = False
            df_an["ob_details"] = [None] * len(df_an)
            df_an["ob_detected"] = False
            df_an["bos_mss_details"] = [None] * len(df_an)
            df_an["bos_mss_detected"] = False

                # === [CANDLE PATTERNS SUPPRIMÉ - Session 23 Nov 2025] ===
                # Bloc détection candle_patterns retiré (21 lignes)
                # Raison : Détecteurs de patterns de bougies retirés du système
                        
            # === PHASE 2bis: RANGE POSITION (accumulation/distribution en range) ===
            try:
                pos_win = int(
                    self.config_manager.get(
                        "phase_detection_defaults.range_position.position_window", 50
                    )
                ) if getattr(self, "config_manager", None) else 50
            except Exception:
                pos_win = 50

            rolling_high = df_an["high"].rolling(pos_win, min_periods=1).max()
            rolling_low  = df_an["low"].rolling(pos_win, min_periods=1).min()
            rng = (rolling_high - rolling_low).replace(0, np.nan)

            df_an["range_pos_pct"] = ((df_an["close"] - rolling_low) / rng).clip(0.0, 1.0).fillna(0.5)

            lower_thr = float(
                self.config_manager.get("phase_detection_defaults.range_position.lower_threshold", 0.33)
            ) if getattr(self, "config_manager", None) else 0.33
            upper_thr = float(
                self.config_manager.get("phase_detection_defaults.range_position.upper_threshold", 0.67)
            ) if getattr(self, "config_manager", None) else 0.67

            df_an["in_lower_tercile"] = df_an["range_pos_pct"] <= lower_thr
            df_an["in_upper_tercile"] = df_an["range_pos_pct"] >= upper_thr

            # === PHASE 3: LIQUIDITÉ ===
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

            # --- LIQUIDITY: sweep & absorption (détection légère, vectorisée) ---
            try:
                # paramètres (valeurs par défaut si non définies)
                lookback_sweep = (
                    int(
                        self.config_manager.get(
                            "liquidity_sweep.sweep.lookback_bars", 20
                        )
                    )
                    if getattr(self, "config_manager", None)
                    else 20
                )
                wick_to_body_min = (
                    float(
                        self.config_manager.get(
                            "liquidity_sweep.sweep.wick_to_body_min_ratio", 1.5
                        )
                    )
                    if getattr(self, "config_manager", None)
                    else 1.5
                )
                min_distance_pips = (
                    float(
                        self.config_manager.get(
                            "liquidity_sweep.sweep.min_distance_pips", 3.0
                        )
                    )
                    if getattr(self, "config_manager", None)
                    else 3.0
                )
                volume_spike_sigma = (
                    float(
                        self.config_manager.get(
                            "liquidity_sweep.sweep.volume_spike_sigma", 1.5
                        )
                    )
                    if getattr(self, "config_manager", None)
                    else 1.5
                )

                body_to_range_min = (
                    float(
                        self.config_manager.get(
                            "liquidity_sweep.absorption.body_to_range_min", 0.5
                        )
                    )
                    if getattr(self, "config_manager", None)
                    else 0.5
                )
                closes_through_mid = (
                    bool(
                        self.config_manager.get(
                            "liquidity_sweep.absorption.closes_through_mid_of_sweep",
                            True,
                        )
                    )
                    if getattr(self, "config_manager", None)
                    else True
                )

                # tailles
                eps = 1e-12
                pip_size = None
                try:
                    point_val = float(df_an["point"].iloc[-1])
                    pip_size = point_val * 10.0 if point_val > 0 else None
                except Exception:
                    pip_size = None
                pip_size = (
                    pip_size or 1.0
                )  # fallback numérique pour les ratios (pas logique de trading)

                # composantes bougies
                oc_max = df_an[["open", "close"]].max(axis=1)
                oc_min = df_an[["open", "close"]].min(axis=1)
                up_wick = (df_an["high"] - oc_max).clip(lower=0.0)
                dn_wick = (oc_min - df_an["low"]).clip(lower=0.0)
                body = (df_an["close"] - df_an["open"]).abs()
                full_range = (df_an["high"] - df_an["low"]).clip(lower=eps)

                # ratios
                up_wr = up_wick / (body.replace(0, eps))
                dn_wr = dn_wick / (body.replace(0, eps))
                body_ratio = body / full_range

                # niveaux HH/LL récents (exclusifs, décalés d'une barre)
                hh_prev = df_an["high"].rolling(lookback_sweep).max().shift(1)
                ll_prev = df_an["low"].rolling(lookback_sweep).min().shift(1)

                # distances franchies en pips
                dist_up_pips = ((df_an["high"] - hh_prev).clip(lower=0.0)) / pip_size
                dist_dn_pips = ((ll_prev - df_an["low"]).clip(lower=0.0)) / pip_size

                # volume spike (z-score déjà calculé plus haut)
                vol_z = pd.to_numeric(
                    df_an.get("volume_zscore", 0.0), errors="coerce"
                ).fillna(0.0)

                # conditions sweep (mèche > corps, dépassement HH/LL, distance min, volume anormal)
                sweep_up = (
                    (df_an["high"] > hh_prev)
                    & (up_wr >= wick_to_body_min)
                    & (dist_up_pips >= min_distance_pips)
                    & (vol_z >= volume_spike_sigma)
                )
                sweep_dn = (
                    (df_an["low"] < ll_prev)
                    & (dn_wr >= wick_to_body_min)
                    & (dist_dn_pips >= min_distance_pips)
                    & (vol_z >= volume_spike_sigma)
                )
                df_an["sweep_detected"] = (sweep_up | sweep_dn).fillna(False)

                # absorption : clôture qui réintègre/avale la mèche (signal opposé au sweep), corps "suffisant"
                mid_range = (df_an["high"] + df_an["low"]) / 2.0
                absorb_up = (
                    sweep_up
                    & (df_an["close"] < df_an["open"])
                    & (body_ratio >= body_to_range_min)
                    & ((not closes_through_mid) | (df_an["close"] <= mid_range))
                )
                absorb_dn = (
                    sweep_dn
                    & (df_an["close"] > df_an["open"])
                    & (body_ratio >= body_to_range_min)
                    & ((not closes_through_mid) | (df_an["close"] >= mid_range))
                )
                df_an["absorption_confirmed"] = (absorb_up | absorb_dn).fillna(False)

            except Exception as e:
                self.logger.warning(
                    f"[{current_asset_symbol}] Sweep/Absorption light detection failed: {e}"
                )
                df_an["sweep_detected"] = False
                df_an["absorption_confirmed"] = False

            # === PHASE 4: CONFLUENCE ===
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

            # --- LIQUIDITY: Equal Highs / Equal Lows ---
            try:
                # === [ICT DETECTOR SUPPRIMÉ - 31 DEC 2025] ===
                # detect_eqh_eql retiré (Equal Highs/Equal Lows)
                df_an["eqh_eql_details"] = None
                df_an["eqh_eql_detected"] = False
            except Exception as e:
                self.logger.warning(
                    f"[{current_asset_symbol}] Erreur phase detection: {e}"
                )
                df_an["eqh_eql_details"] = None
                df_an["eqh_eql_detected"] = False

                # === PHASE 4bis: MICRO-PHASE BURST SCALPING ===
            try:
                if hasattr(self.detectors, "detect_micro_phase_m1"):
                    micro = self.detectors.detect_micro_phase_m1(df_an)
                    if micro and isinstance(micro, dict):
                        df_an["burst_signal"] = bool(micro.get("burst_signal", False))
                        df_an["burst_side"] = str(micro.get("burst_side", "NEUTRAL"))
                        df_an["burst_strength"] = float(
                            micro.get("burst_strength", 0.0)
                        )
                        df_an["suggested_burst_size"] = int(
                            micro.get("suggested_burst_size", 0)
                        )
                        df_an["burst_sl_pips"] = micro.get("sl_pips_suggestion")
                        df_an["burst_tp_pips"] = micro.get("tp_pips_suggestion")
                    else:
                        df_an["burst_signal"] = False
                        df_an["burst_side"] = "NEUTRAL"
                        df_an["burst_strength"] = 0.0
                        df_an["suggested_burst_size"] = 0
                        df_an["burst_sl_pips"] = None
                        df_an["burst_tp_pips"] = None
            except Exception as e:
                self.logger.warning(
                    f"[{current_asset_symbol}] Erreur detect_micro_phase_m1: {e}"
                )
                df_an["burst_signal"] = False
                df_an["burst_side"] = "NEUTRAL"
                df_an["burst_strength"] = 0.0
                df_an["suggested_burst_size"] = 0
                df_an["burst_sl_pips"] = None
                df_an["burst_tp_pips"] = None

            # === PHASE 4ter: FOOTPRINT VALIDATOR (Dev Desk) ===
            try:
                if (
                    hasattr(self.detectors, "validate_last_candle_footprint")
                    and ticks is not None
                ):
                    fp_res = self.detectors.validate_last_candle_footprint(df_an, ticks)
                    if fp_res:
                        df_an.loc[df_an.index[-1], "footprint_score"] = fp_res.get(
                            "score", 0
                        )
                        df_an.loc[df_an.index[-1], "footprint_status"] = fp_res.get(
                            "status", "UNKNOWN"
                        )
                        # 🔧 FIX (24 Nov 2025): Pandas ne supporte pas les dicts dans DataFrame
                        # Stocker en JSON string (sera lu directement depuis latest, pas depuis df_an)
                        import json
                        df_an.loc[df_an.index[-1], "footprint_summary"] = json.dumps(
                            fp_res.get("summary", {})
                        )
            except Exception as e:
                self.logger.warning(
                    f"[{current_asset_symbol}] Footprint validator failed: {e}"
                )
                df_an.loc[df_an.index[-1], "footprint_score"] = 0
                df_an.loc[df_an.index[-1], "footprint_status"] = "ERROR"
                # 🔧 FIX (24 Nov 2025): Stocker en JSON string
                df_an.loc[df_an.index[-1], "footprint_summary"] = "{}"

            # === PHASE 5: PHASE PRIMAIRE (déterministe) ===
 
            # ✅ MAJ (06 DEC 2025): Ajout phases pour nouveaux régimes (BREAKOUT, STRONG_TRENDING, COMPRESSION)
            ALLOWED_PHASES = {
                # TRENDING (normal + strong)
                "trending_institutional_bull", "trending_institutional_bear",
                "trending_retail_bull", "trending_retail_bear",
                "strong_trending_institutional_bull", "strong_trending_institutional_bear",
                "strong_trending_retail_bull", "strong_trending_retail_bear",

                # CONSOLIDATION (bull/bear flags)
                "consolidation_bull", "consolidation_bear",

                # RANGE
                "range_accumulation", "range_distribution", "range_institutional", "range_retail",

                # VOLATILITÉ & TRANSITIONS
                "high_volatility_chaos", "low_volatility_compression", "compression",
                "breakout_bull", "breakout_bear", "breakout_neutral",

                # LIQUIDITY
                "liquidity_eqh_eql", "liquidity_sweep", "liquidity_absorption",

                # INSTITUTIONAL & AUTRES
                "distribution_breakout", "accumulation_zone",
                "institutional_setup", "institutional_setup_premium",
                "volatility_breakout"
            }
            
            def _fallback_phase_from_regime(row: pd.Series) -> str:
                """
                Fallback déterministe basé sur le régime courant de la ligne.
                Utilisé UNIQUEMENT si un label invalide remonte (sanitizer/PhaseGuard).
                ✅ MAJ (06 DEC 2025): Support nouveaux régimes (BREAKOUT, STRONG_TRENDING, COMPRESSION)
                """
                regime = str(row.get("regime", "")).lower()

                # BREAKOUT (nouveau)
                if "breakout" in regime:
                    if "bull" in regime:
                        return "breakout_bull"
                    elif "bear" in regime:
                        return "breakout_bear"
                    else:
                        return "breakout_neutral"

                # STRONG TRENDING (nouveau)
                if "strong_trending" in regime:
                    is_institutional = "institutional" in regime
                    if "bear" in regime:
                        return "strong_trending_institutional_bear" if is_institutional else "strong_trending_retail_bear"
                    if "bull" in regime:
                        return "strong_trending_institutional_bull" if is_institutional else "strong_trending_retail_bull"

                # CONSOLIDATION (bull/bear flags)
                if "consolidation" in regime:
                    if "bull" in regime:
                        return "consolidation_bull"
                    elif "bear" in regime:
                        return "consolidation_bear"
                    # Fallback si direction non détectée
                    close_ = float(row.get("close", 0.0))
                    open_ = float(row.get("open", 0.0))
                    return "consolidation_bull" if close_ >= open_ else "consolidation_bear"

                # Trending normal (bull/bear)
                if "trending" in regime:
                    is_institutional = "institutional" in regime
                    if "bear" in regime:
                        return "trending_institutional_bear" if is_institutional else "trending_retail_bear"
                    if "bull" in regime:
                        return "trending_institutional_bull" if is_institutional else "trending_retail_bull"

                # Range → tranche acc/dist via la position récente dans le range
                if "range" in regime:
                    pos = float(row.get("range_pos_pct", 0.5))
                    return "range_accumulation" if pos <= 0.5 else "range_distribution"

                # Volatilité
                if "compression" in regime:
                    return "compression"
                if "high_volatility" in regime or "high_vol" in regime:
                    return "high_volatility_chaos"
                if "low_volatility" in regime or "low_vol" in regime:
                    return "low_volatility_compression"

                # Par défaut (jamais 'unknown')
                close_ = float(row.get("close", 0.0))
                open_  = float(row.get("open",  0.0))
                return "range_accumulation" if close_ >= open_ else "range_distribution"


            def _sanitize_phase_label(row: pd.Series, phase: str) -> str:
                """Évite tout label indécis et remappe vers une phase autorisée."""
                p = (phase or "").strip().lower()
                # ✅ FIX (08 DEC 2025): Ajouter "none" string pour gérer str(None) conversion
                if p in ("", "unknown", "uncertain", "no_clear_phase", "none", None):
                    return _fallback_phase_from_regime(row)
                PHASE_SYNONYM_MAP = {
                    "bullish": "trending_institutional_bull",
                    "bearish": "trending_institutional_bear",
                    "range": "range_retail",
                    "vol_high": "high_volatility_chaos",
                    "vol_low": "low_volatility_compression",
                }
                p = PHASE_SYNONYM_MAP.get(p, p)
                return p if p in ALLOWED_PHASES else _fallback_phase_from_regime(row)


            def _liquidity_persist_ok(row: pd.Series, asset: str, *, min_bars: int, conf_thr: float):
                """
                Persistance minimale des signaux Liquidity.
                Retourne (ok: bool, label: str). Utilise memory.caches['liq_persist'].
                """
                mem = self.memory.get_memory(asset)
                mem.caches.setdefault("liq_persist", {"eqh": 0, "sweep": 0, "abs": 0})
                lp = mem.caches["liq_persist"]

                eqh = bool(row.get("eqh_eql_detected"))
                swp = bool(row.get("sweep_detected"))
                absb = bool(row.get("absorption_confirmed"))
                conf = float(row.get("confidence_score", 0.0))
                has_bos = bool(row.get("bos_mss_detected", False))

                lp["eqh"] = lp["eqh"] + 1 if eqh else 0
                lp["sweep"] = lp["sweep"] + 1 if swp else 0
                lp["abs"] = lp["abs"] + 1 if absb else 0

                self.memory.save_memory(asset, mem)

                # Acceptation: persistance OU (confiance élevée OU BOS/MSS)
                if lp["sweep"] >= min_bars or (swp and (conf >= conf_thr or has_bos)):
                    return True, "liquidity_sweep"
                if lp["abs"] >= min_bars or (absb and (conf >= conf_thr or has_bos)):
                    return True, "liquidity_absorption"
                if lp["eqh"] >= min_bars or (eqh and (conf >= conf_thr or has_bos)):
                    return True, "liquidity_eqh_eql"
                return False, ""


            def _determine_phase_no_ncp(row: pd.Series) -> str:
                # ✅ VALIDATION PRÉVENTIVE : Vérifier que row est un pd.Series valide
                if row is None or not isinstance(row, pd.Series):
                    self.logger.warning(
                        f"[PHASE_DETECTION_ERROR] Row invalide (type={type(row)}), "
                        f"fallback vers régime"
                    )
                    return _fallback_phase_from_regime(row) if row is not None else "range_retail"

                # ✅ VALIDATION : Vérifier que les colonnes critiques existent
                if "regime" not in row or row.get("regime") is None or (isinstance(row.get("regime"), float) and pd.isna(row.get("regime"))):
                    self.logger.warning(
                        f"[PHASE_DETECTION_ERROR] Colonne 'regime' manquante ou None, "
                        f"fallback déterministe appliqué"
                    )
                    return "range_retail"

                # 1) détermination brute par la taxonomie existante
                try:
                    raw = self.detectors.determine_optimized_phase(row)
                except Exception as e:
                    # ❌ PROBLÈME : Cette exception ne devrait JAMAIS arriver !
                    # determine_optimized_phase() est conçu pour toujours retourner une phase valide
                    # Si on arrive ici, c'est qu'il y a un problème de données en entrée
                    self.logger.error(
                        f"[PHASE_DETECTION_ERROR] determine_optimized_phase a échoué ! "
                        f"Erreur: {e} | row type={type(row)} | row.index={getattr(row, 'index', 'N/A')}",
                        exc_info=True
                    )
                    raw = None

                # 2) priorité Liquidity MAIS avec persistance minimale configurable
                ok_liq, liq_label = _liquidity_persist_ok(
                    row, current_asset_symbol,
                    min_bars=int(self.config_manager.get("phase_observer.liquidity_persist", default=2))
                    if getattr(self, "config_manager", None) else 2,
                    conf_thr=float(self.config_manager.get("phase_observer.liquidity_confidence", default=0.65))
                    if getattr(self, "config_manager", None) else 0.65,
                )
                if ok_liq:
                    return liq_label

                # 3) sanitize & fallback déterministe par régime → zéro label indécis
                return _sanitize_phase_label(row, raw)

            df_an["phase_primary"] = df_an.apply(_determine_phase_no_ncp, axis=1)


            # === PHASE 6: SCORE DE CONFIANCE ===
            df_an["confidence_score"] = df_an.apply(
                lambda row: self.calculate_optimized_confidence(row),
                axis=1,
            )

            # === PHASE 7: APPLICATION MÉMOIRE ===
            df_an["phase"] = df_an.apply(
                lambda row: self.memory.apply_phase_memory(
                    asset_symbol=current_asset_symbol,
                    current_phase=row["phase_primary"],
                    confidence=row.get("confidence_score", 0.5),
                ),
                axis=1,
            )
            df_an["phase_rule"] = "primary"
            
            # --- PATCH D1: PhaseGuard (clamp + métriques) ---
            # 1) liste blanche centralisée (réutilise ALLOWED_PHASES défini plus haut)
            invalid_mask = ~df_an["phase"].isin(ALLOWED_PHASES)
            if invalid_mask.any():
                bad = sorted(set(df_an.loc[invalid_mask, "phase"].astype(str).tolist()))
                self.logger.debug(f"[PhaseGuard] Phases invalides détectées et corrigées: {bad}")

                # 2) remap déterministe par régime (filet ultime)
                df_an.loc[invalid_mask, "phase"] = df_an.loc[invalid_mask].apply(
                    lambda r: _fallback_phase_from_regime(r),
                    axis=1
                )

            # 3) métriques: nombre de corrections pour monitoring/alerting
            try:
                corrections = int(invalid_mask.sum())
                df_an.attrs["phase_guard_corrections"] = corrections
                # (optionnel) exposer aussi côté objet si tu as un exporter de stats
                setattr(self, "_phase_guard_last_corrections", corrections)
            except Exception:
                pass

            # --- PATCH A3: clamp final (aucun label indésirable ne passe)
            df_an["phase"] = df_an.apply(lambda r: _sanitize_phase_label(r, r.get("phase")), axis=1)


            # === PHASE 7bis: STRATEGY FLAGS ===
            try:

                # 2) Switch Liquidity dès qu'une zone est identifiée (sweep ou absorption)
                has_sweep = (
                    bool(df_an["sweep_detected"].iloc[-1])
                    if "sweep_detected" in df_an
                    else False
                )
                has_absorb = (
                    bool(df_an["absorption_confirmed"].iloc[-1])
                    if "absorption_confirmed" in df_an
                    else False
                )

                # Confluence minimale : BOS/MSS ou FVG/OB aide aussi à prioriser Liquidity
                has_bos = (
                    bool(df_an["bos_mss_detected"].iloc[-1])
                    if "bos_mss_detected" in df_an
                    else False
                )
                has_confluence = (
                    bool(df_an["fvg_ob_confluence"].iloc[-1])
                    if "fvg_ob_confluence" in df_an
                    else False
                )

                df_an["switch_to_liquidity"] = (
                    has_sweep or has_absorb or has_bos or has_confluence
                )

                # 3) MTF bias (placeholder: sera renseigné par l'analyse MTF ; on met False par défaut pour éviter KeyError)
                if "mtf_bias_aligned" not in df_an.columns:
                    df_an["mtf_bias_aligned"] = False

                # Logs explicites

                if bool(df_an["switch_to_liquidity"].iloc[-1]):
                    self.logger.info(
                        f"[{current_asset_symbol}] ⚡ Zone de liquidité détectée → Switch Liquidity."
                    )

            except Exception as e:
                self.logger.warning(
                    f"[{current_asset_symbol}] Impossible de poser les flags scalping/liquidity: {e}"
                )
                df_an["scalping_ok"] = False
                df_an["switch_to_liquidity"] = False
                if "mtf_bias_aligned" not in df_an.columns:
                    df_an["mtf_bias_aligned"] = False

            # === PHASE 8: LOG FINAL ===
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

                # === [LOG BOUGIES SUPPRIMÉ - Session 23 Nov 2025] ===
                # Bloc logging candle_pattern retiré (12 lignes)

                # --- PATCH: ajoute current_price ---
                try:
                    if "close" in df_an.columns and not df_an.empty:
                        last_close = float(df_an["close"].iloc[-1])
                        df_an["current_price"] = last_close
                        self.logger.debug(
                            f"[{current_asset_symbol}] current_price ajouté: {last_close}"
                        )
                    else:
                        df_an["current_price"] = None
                except Exception as e:
                    self.logger.warning(
                        f"[{current_asset_symbol}] Impossible d'ajouter current_price: {e}"
                    )
                    df_an["current_price"] = None

            return df_an

        except Exception as e:
            self.logger.error(f"analyze() failure: {e}", exc_info=True)
            return None

    def analyze_last_bar(
        self, df: pd.DataFrame, asset_symbol: str, ticks: pd.DataFrame | None = None
    ) -> dict | None:
        """
        Analyse uniquement la dernière bougie M1 (et ses ticks) avec TOUS les détecteurs,
        puis renvoie un paquet compact de signaux pour cette bougie.
        """
        if df is None or df.empty:
            return None

        # Nettoyage rapide (réutilise ta pipeline existante)
        df_an = self.features.clean_dataframe(df.copy())
        if df_an is None or df_an.empty:
            return None

        # Sélection stricte dernière bougie
        i_last = len(df_an) - 1
        last_bar_df = df_an.iloc[[i_last]]

        # --- Détecteurs (par bar) ---
        res = {"asset": asset_symbol, "index": int(i_last)}
        # === [CANDLE PATTERNS SUPPRIMÉ - Session 23 Nov 2025] ===
        # Bloc détection candle_pattern dans analyze_last_bar retiré (7 lignes)

        # === [ICT DETECTORS SUPPRIMÉS - 31 DEC 2025] ===
        # detect_fvg_enhanced, detect_order_block_ml_enhanced, detect_bos_mss_enhanced retirés
        res["fvg_details"] = None
        res["fvg_detected"] = False
        res["ob_details"] = None
        res["ob_detected"] = False
        res["bos_mss_details"] = None
        res["bos_mss_detected"] = False

        # Liquidity light (sweep/absorption)
        try:
            res["sweep_detected"] = (
                bool(df_an.get("sweep_detected", [False])[i_last])
                if "sweep_detected" in df_an
                else False
            )
            res["absorption_confirmed"] = (
                bool(df_an.get("absorption_confirmed", [False])[i_last])
                if "absorption_confirmed" in df_an
                else False
            )
        except Exception:
            res["sweep_detected"] = False
            res["absorption_confirmed"] = False

        # Footprint validateur (ticks de la minute)
        try:
            if ticks is not None and hasattr(
                self.detectors, "validate_last_candle_footprint"
            ):
                fp = self.detectors.validate_last_candle_footprint(df_an, ticks)
                res["footprint_score"] = fp.get("score", 0)
                res["footprint_status"] = fp.get("status", "UNKNOWN")
                res["footprint_summary"] = fp.get("summary", {})
            else:
                res["footprint_score"] = 0
                res["footprint_status"] = "N/A"
                res["footprint_summary"] = {}
        except Exception:
            res["footprint_score"] = 0
            res["footprint_status"] = "ERROR"
            res["footprint_summary"] = {}

        # Phase + confiance sur la dernière bougie seulement
        try:
            phase_row = df_an.iloc[i_last].to_dict()
            res["phase_primary"] = self.detectors.determine_optimized_phase(pd.Series(phase_row))
        except Exception:
            pos = float(df_an.get("range_pos_pct", [0.5])[i_last] if "range_pos_pct" in df_an else 0.5)
            res["phase_primary"] = "range_accumulation" if pos <= 0.5 else "range_distribution"

        try:
            res["confidence_score"] = float(
                self.calculate_optimized_confidence(df_an.iloc[i_last])
            )
        except Exception:
            res["confidence_score"] = 0.0

        # 🔥 Ajout footprints en mémoire
        try:
            footprints_hist = self.memory.get_memory(asset_symbol).caches.get(
                "footprints", []
            )
            res["footprints_history"] = footprints_hist[-5:]  # on garde les 5 derniers
        except Exception as e_mem:
            self.logger.warning(
                f"[analyze_last_bar] impossible de récupérer footprints: {e_mem}"
            )
            res["footprints_history"] = []

        return res

    def analyze_live_bar(self, asset_symbol: str) -> Optional[dict]:
        """
        Analyse la bougie en cours (non fermée) avec les ticks accumulés.
        Retourne un pré-signal basé sur le footprint live.
        """
        import pandas as pd

        if not self._ticks_current_bar:
            return None

        try:
            ticks_df = pd.DataFrame(self._ticks_current_bar)

            if ticks_df.empty:
                self.logger.debug(
                    f"[{asset_symbol}] Aucun tick live à analyser (ticks_df vide)."
                )
                return None

            # ✅ Reconstruction microstructurelle du flux MT5
            try:
                from .detectors import (
                    reconstruct_tick_side_mt5,
                )  # chemin relatif correct

                ticks_df = reconstruct_tick_side_mt5(ticks_df)
                self.logger.debug(
                    f"[{asset_symbol}] reconstruction ticks OK (live) -> "
                    f"{len(ticks_df)} lignes ({(ticks_df['side'] == 'buy').sum()} buys / "
                    f"{(ticks_df['side'] == 'sell').sum()} sells)"
                )
            except Exception as e_reb:
                self.logger.warning(
                    f"[{asset_symbol}] reconstruction tick side échouée: {e_reb}",
                    exc_info=True,
                )

            # ✅ Validation footprint live
            footprint_partial = self.detectors.validate_last_candle_footprint(
                self._history_df, ticks_df
            )

            pre_signal = {
                "asset": asset_symbol,
                "footprint_live": footprint_partial.get("summary", {}),
                "footprint_delta": footprint_partial.get("summary", {}).get(
                    "delta_total", 0.0
                ),
                "footprint_poc": footprint_partial.get("summary", {}).get("poc"),
                "timestamp": datetime.utcnow().isoformat(),
            }

            return pre_signal

        except Exception as e:
            self.logger.warning(
                f"[{asset_symbol}] Erreur analyse bougie live: {e}", exc_info=True
            )
            return None

    def recalibrate_full(self, asset_symbol: str) -> Optional[pd.DataFrame]:
        """
        Ré-analyse complète de l'historique (200 barres par défaut)
        pour recalibrer tous les indicateurs/patterns.
        """
        if self._history_df is None or self._history_df.empty:
            self.logger.warning(
                f"[{asset_symbol}] Impossible de recalibrer : historique vide."
            )
            return None

        try:
            df_reanalyzed = self.analyze(
                self._history_df.copy(), asset_symbol=asset_symbol
            )
            self._history_df = df_reanalyzed
            self.logger.info(
                f"[{asset_symbol}] 🔄 Recalibration complète effectuée ({len(df_reanalyzed)} barres)."
            )
            return df_reanalyzed
        except Exception as e:
            self.logger.error(f"[{asset_symbol}] Erreur recalibration complète: {e}")
            return None

