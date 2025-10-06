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

from .features import FeaturesExtractor  # ✅ on importe la classe, plus les fonctions
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

        self.logger.info(
            f"PhaseObserver initialisé. Lookback window: {self.lookback_window}."
        )

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
    # Méthode 1: on_tick
    # ================================================================
    def on_tick(self, tick: dict) -> None:
        """
        Ajoute un tick dans le buffer courant et tente une validation 'live'.
        tick: {"time": Timestamp/str, "price": float, "size": float, "side": "buy"/"sell"}
        """
        try:
            if not isinstance(tick, dict):
                self.logger.warning("[on_tick] tick non-dict ignoré")
                return

            # Normaliser time
            t_time = tick.get("time", pd.Timestamp.utcnow())
            try:
                t_time = pd.to_datetime(t_time)
            except Exception:
                t_time = pd.Timestamp.utcnow()

            tick_safe = {
                "time": t_time,
                "price": float(tick.get("price")) if tick.get("price") is not None else np.nan,
                "size":  float(tick.get("size"))  if tick.get("size")  is not None else np.nan,
                "side":  str(tick.get("side") or "").lower(),
            }

            # Buffer ticks limité par config
            if not hasattr(self, "_ticks_current_bar") or self._ticks_current_bar is None:
                self._ticks_current_bar = []
            self._ticks_current_bar.append(tick_safe)

            max_buf = int(self.footprint_cfg.get("max_buffer_ticks", 500))
            if len(self._ticks_current_bar) > max_buf:
                self._ticks_current_bar = self._ticks_current_bar[-max_buf:]

            # Footprint live activé ?
            if self.footprint_cfg.get("enable_live_footprint", True):
                footprint_partial = None
                try:
                    hist_copy = None if getattr(self, "_history_df", None) is None else self._history_df.copy()
                    ticks_df = pd.DataFrame(self._ticks_current_bar)
                    footprint_partial = self.detectors.validate_last_candle_footprint(hist_copy, ticks_df)
                except Exception as e_val:
                    self.logger.debug(f"[on_tick] validate_last_candle_footprint: {e_val}", exc_info=True)

                if footprint_partial and self._history_df is not None and not self._history_df.empty:
                    self._history_df = self._ensure_footprint_columns(self._history_df)
                    last_idx = self._history_df.index[-1]
                    delta = footprint_partial["summary"].get("delta_total", pd.NA)
                    poc = footprint_partial["summary"].get("poc", pd.NA)

                    # Appliquer seuil delta
                    if abs(delta) >= self.footprint_cfg.get("delta_threshold", 0.0):
                        self._history_df.at[last_idx, "footprint_live_delta"] = delta
                        self._history_df.at[last_idx, "footprint_live_poc"]   = poc

                        if self.footprint_cfg.get("store_to_memory", True):
                            try:
                                self.memory.store_footprint(
                                    asset_symbol=getattr(self, "current_asset_symbol", "LIVE_ASSET"),
                                    delta=delta,
                                    poc=poc,
                                    is_live=True,
                                )
                            except Exception as e_mem:
                                self.logger.warning(f"[on_tick] store_footprint live failed: {e_mem}", exc_info=True)

        except Exception as e:
            self.logger.exception(f"[on_tick] erreur générale: {e}")


    # ================================================================
    # Méthode 2: on_bar_close
    # ================================================================
    def on_bar_close(self, new_bar: dict, asset_symbol: str):
        """
        Clôture une bougie M1 :
        - nettoie la ligne
        - met à jour ou ajoute la bougie dans l’historique (FIFO max 200)
        - calcule footprint final
        - vide le buffer de ticks
        - lance l’analyse de la dernière bougie uniquement
        """
        try:
            if not isinstance(new_bar, dict):
                raise ValueError("on_bar_close: new_bar doit être un dict")

            # Horodatage
            ts = pd.to_datetime(new_bar.get("time", pd.Timestamp.utcnow()), errors="coerce")
            if ts is pd.NaT:
                ts = pd.Timestamp.utcnow()

            # Nettoyage des valeurs
            safe_row = {k: self._coerce_val(v) for k, v in new_bar.items()}
            safe_row_df = pd.DataFrame([safe_row], index=[ts])

            # Init historique
            if self._history_df is None or self._history_df.empty:
                self._history_df = safe_row_df.copy()
            else:
                # Update si timestamp déjà existant
                if ts in self._history_df.index:
                    for col, val in safe_row.items():
                        self._history_df.at[ts, col] = val
                else:
                    self._history_df = pd.concat([self._history_df, safe_row_df])
                    # FIFO → garder seulement les 200 dernières barres
                    self._history_df = self._history_df.tail(200)

            # Colonnes footprint
            self._history_df = self._ensure_footprint_columns(self._history_df)

            # Ticks associés
            ticks_df = pd.DataFrame(
                list(self._ticks_current_bar) or [], columns=["time", "price", "size", "side"]
            )
            ticks_df["time"] = pd.to_datetime(ticks_df["time"], errors="coerce")
            
            # ✅ Reconstruction microstructurelle MT5 avant validation footprint
            try:
                from .detectors import reconstruct_tick_side_mt5              
                # Reconstruit le côté (achat/vente) + volumes directionnels
                ticks_df = reconstruct_tick_side_mt5(ticks_df)

                self.logger.debug(
                    f"[on_bar_close] reconstruction ticks OK -> {len(ticks_df)} lignes reconstruites "
                    f"({(ticks_df['side'] == 'buy').sum()} buys / {(ticks_df['side'] == 'sell').sum()} sells)"
                )
            except Exception as e_rebuild:
                self.logger.warning(f"[on_bar_close] reconstruction ticks échouée: {e_rebuild}", exc_info=True)

            # Validation footprint final (si activé)
            if self.footprint_cfg.get("enable_final_footprint", True):
                footprint_final = None
                try:
                    footprint_final = self.detectors.validate_last_candle_footprint(
                        self._history_df.copy(), ticks_df.copy()
                    )
                except Exception as e_val:
                    self.logger.debug(f"[on_bar_close] validate_last_candle_footprint: {e_val}", exc_info=True)

                last_idx = self._history_df.index[-1]
                if footprint_final:
                    delta = footprint_final.get("summary", {}).get("delta_total", 0.0)
                    poc = footprint_final.get("summary", {}).get("poc", None)

                    if abs(delta) >= self.footprint_cfg.get("delta_threshold", 0.0):
                        self._history_df.at[last_idx, "footprint_score"] = footprint_final.get("score", pd.NA)
                        self._history_df.at[last_idx, "footprint_status"] = footprint_final.get("status", pd.NA)
                        self._history_df.at[last_idx, "footprint_summary"] = str(footprint_final.get("summary", {}))

                        if self.footprint_cfg.get("store_to_memory", True):
                            try:
                                self.memory.store_footprint(
                                    asset_symbol=asset_symbol,
                                    delta=delta,
                                    poc=poc,
                                    is_live=False,
                                )
                            except Exception as e_mem:
                                self.logger.warning(f"[on_bar_close] store_footprint final failed: {e_mem}", exc_info=True)
                else:
                    self._history_df.at[last_idx, "footprint_status"] = "SUSPECT"
                    self._history_df.at[last_idx, "footprint_summary"] = str({"comment": "validator error or no ticks"})

            # Clear buffer ticks
            self._ticks_current_bar.clear()

            # Analyse uniquement la dernière bougie
            return self.analyze_last_bar(self._history_df.copy(), asset_symbol=asset_symbol, ticks=ticks_df.copy())

        except Exception as e:
            self.logger.exception(f"[on_bar_close] erreur générale: {e}")
            return None


    # ================================================================
    # Méthode 3: load_initial_history
    # ================================================================
    def load_initial_history(self, df: pd.DataFrame, asset_symbol: str = "INIT", max_bars: int = 200):
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
                    df.index = pd.date_range(end=pd.Timestamp.utcnow(), periods=len(df), freq="T")
                    self.logger.warning("[load_initial_history] index non-datetime -> remplacé par range minute")

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
                "candle_pattern": 0.15,
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

        # --- 5) Bougies ---
        candle_type = str(row.get("candle_pattern", "")).lower()
        candle_score = float(row.get("candle_pattern_strength", 0.0) or 0.0)
        if candle_score > 0:
            base_bonus = signal_weights.get("candle_pattern", 0.15) * min(
                1.0, candle_score
            )
            if candle_type in {
                "bullish_engulfing",
                "morning_star",
                "three_white_soldiers",
            }:
                score += base_bonus * 1.3
            elif candle_type in {
                "bearish_engulfing",
                "evening_star",
                "three_black_crows",
            }:
                score += base_bonus * 1.3
            elif candle_type in {"doji", "doji_cluster_consolidation"}:
                score += base_bonus * 0.7
            elif candle_type in {
                "hammer",
                "shooting_star",
                "bullish_pinbar",
                "bearish_pinbar",
            }:
                score += base_bonus * 1.0
            else:
                score += base_bonus

        # --- 6) Multiplicateurs qualité ---
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
                    f"[CONF-clean] path='{cfg_path_used}' score={score:.3f} | candle={candle_score}"
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
        - Supprime tout mappage 'fallback_*' : si pas de phase → 'no_clear_phase'
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
                df_an["regime"] = self.detectors.detect_market_regime(df_an)
                df_an["regime_detected"] = True
            else:
                df_an["regime"] = "unknown"
                df_an["regime_detected"] = False
                df_an["regime_strength"] = 0.5

            if toggles.get("detect_fvg", True):
                df_an["fvg_details"] = self.detectors.detect_fvg_enhanced(df_an)
                df_an["fvg_detected"] = df_an["fvg_details"].apply(
                    lambda x: x is not None
                )
            else:
                df_an["fvg_details"] = [None] * len(df_an)
                df_an["fvg_detected"] = False

            if toggles.get("detect_order_block", True):
                df_an["ob_details"] = self.detectors.detect_order_block_ml_enhanced(
                    df_an
                )
                df_an["ob_detected"] = df_an["ob_details"].apply(
                    lambda x: x is not None
                )
            else:
                df_an["ob_details"] = [None] * len(df_an)
                df_an["ob_detected"] = False

            if toggles.get("detect_bos_mss", True):
                df_an["bos_mss_details"] = self.detectors.detect_bos_mss_enhanced(df_an)
                df_an["bos_mss_detected"] = df_an["bos_mss_details"].apply(
                    lambda x: x is not None
                )
            else:
                df_an["bos_mss_details"] = [None] * len(df_an)
                df_an["bos_mss_detected"] = False

                # === (NOUVEAU) CANDLE PATTERNS ===
                if toggles.get("detect_candles", True):
                    try:
                        candle_signals = self.detectors.detect_candle_patterns(df_an)
                        if candle_signals and isinstance(candle_signals, list):
                            df_an["candle_pattern"] = [
                                c.get("pattern") if c else None for c in candle_signals
                            ]
                            df_an["candle_pattern_score"] = [  # ✅ nouveau nom
                                c.get("strength_score") if c else 0.0
                                for c in candle_signals
                            ]
                        else:
                            df_an["candle_pattern"] = None
                            df_an["candle_pattern_score"] = 0.0
                    except Exception as e:
                        self.logger.warning(
                            f"[{current_asset_symbol}] Erreur detect_candle_patterns: {e}"
                        )
                        df_an["candle_pattern"] = None
                        df_an["candle_pattern_score"] = 0.0

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
                    & ((~closes_through_mid) | (df_an["close"] <= mid_range))
                )
                absorb_dn = (
                    sweep_dn
                    & (df_an["close"] > df_an["open"])
                    & (body_ratio >= body_to_range_min)
                    & ((~closes_through_mid) | (df_an["close"] >= mid_range))
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
                eqh_cfg = (
                    self.config_manager.get("eqh_eql_settings", {})
                    if getattr(self, "config_manager", None)
                    else {}
                )
                eqh_signals = self.detectors.detect_eqh_eql(df_an, eqh_cfg)
                if eqh_signals:
                    df_an["eqh_eql_details"] = eqh_signals
                    df_an["eqh_eql_detected"] = [s is not None for s in eqh_signals]
                else:
                    df_an["eqh_eql_details"] = None
                    df_an["eqh_eql_detected"] = False
            except Exception as e:
                self.logger.warning(
                    f"[{current_asset_symbol}] Erreur detect_eqh_eql: {e}"
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
                        df_an.loc[df_an.index[-1], "footprint_summary"] = str(
                            fp_res.get("summary", {})
                        )
            except Exception as e:
                self.logger.warning(
                    f"[{current_asset_symbol}] Footprint validator failed: {e}"
                )
                df_an.loc[df_an.index[-1], "footprint_score"] = 0
                df_an.loc[df_an.index[-1], "footprint_status"] = "ERROR"
                df_an.loc[df_an.index[-1], "footprint_summary"] = "{}"

            # === PHASE 5: PHASE PRIMAIRE ===
            df_an["phase_primary"] = df_an.apply(
                self.detectors.determine_optimized_phase, axis=1
            )

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
            df_an["phase_is_uncertain"] = df_an["phase"] == "no_clear_phase"

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

                # 🔍 Bougies (nouveau log)
                if "candle_pattern" in df_an.columns:
                    last_candle = str(df_an["candle_pattern"].iloc[-1])
                    last_candle_strength = float(
                        df_an.get("candle_pattern_score", [0.0])[-1]
                    )

                    if last_candle and last_candle != "None":
                        self.logger.info(
                            f"🕯️ [{current_asset_symbol}] Dernier pattern détecté: {last_candle} "
                            f"(strength={last_candle_strength:.2f})"
                        )

                if last_phase == "no_clear_phase":
                    self.logger.info(
                        f"[{current_asset_symbol}] Phase indécise (no_clear_phase) — aucune règle de secours appliquée (strict)."
                    )

                if last_phase == "no_clear_phase":
                    self.logger.info(
                        f"[{current_asset_symbol}] Phase indécise (no_clear_phase) — aucune règle de secours appliquée (strict)."
                    )
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
        try:
            # Patterns chandeliers / combos (ex: tes fonctions savent accepter un index)
            res["candle_pattern"] = (
                self.detectors.detect_candle_patterns(last_bar_df) or [None]
            )[-1]
        except Exception:
            res["candle_pattern"] = None

        try:
            fvg_details = self.detectors.detect_fvg_enhanced(df_an)
            res["fvg_details"] = (
                fvg_details[i_last] if isinstance(fvg_details, list) else None
            )
            res["fvg_detected"] = bool(res["fvg_details"])
        except Exception:
            res["fvg_details"] = None
            res["fvg_detected"] = False

        try:
            ob_details = self.detectors.detect_order_block_ml_enhanced(df_an)
            res["ob_details"] = (
                ob_details[i_last] if isinstance(ob_details, list) else None
            )
            res["ob_detected"] = bool(res["ob_details"])
        except Exception:
            res["ob_details"] = None
            res["ob_detected"] = False

        try:
            bos_details = self.detectors.detect_bos_mss_enhanced(df_an)
            res["bos_mss_details"] = (
                bos_details[i_last] if isinstance(bos_details, list) else None
            )
            res["bos_mss_detected"] = bool(res["bos_mss_details"])
        except Exception:
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
            res["phase_primary"] = self.detectors.determine_optimized_phase(
                pd.Series(phase_row)
            )
        except Exception:
            res["phase_primary"] = "no_clear_phase"

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

            # --- Reconstruction du flux tick MT5 (ajout des sides buy/sell) ---
            try:
                from detectors import reconstruct_tick_side_mt5  # adapter si besoin

                if not ticks_df.empty:
                    ticks_df = reconstruct_tick_side_mt5(ticks_df)
                    self.logger.debug(
                        f"[{asset_symbol}] reconstruct_tick_side_mt5 appliqué sur {len(ticks_df)} ticks (live)"
                    )
                else:
                    self.logger.debug(f"[{asset_symbol}] Aucun tick live à reconstruire (ticks_df vide).")
            except Exception as e_recon:
                self.logger.warning(f"[analyze_live_bar] reconstruction tick side échouée: {e_recon}")

            # --- Validation footprint live ---
            footprint_partial = self.detectors.validate_last_candle_footprint(
                self._history_df, ticks_df
            )

            pre_signal = {
                "asset": asset_symbol,
                "footprint_live": footprint_partial.get("summary", {}),
                "footprint_delta": footprint_partial.get("summary", {}).get("delta_total", 0),
                "footprint_poc": footprint_partial.get("summary", {}).get("poc"),
                "timestamp": datetime.utcnow().isoformat(),
            }

            return pre_signal
        except Exception as e:
            self.logger.warning(f"[{asset_symbol}] Erreur analyse bougie live: {e}")
            return None

    def analyze_asset_multi_timeframe(
        self, asset: str, strategy_config: Dict
    ) -> Dict[str, Any]:
        """
        🏛️ ANALYSE MULTI-TIMEFRAME INSTITUTIONNELLE (STRICT, NO FALLBACK) 🏛️
        - Pas d'analyse single-TF de secours
        - Si MTF non exploitable → paquet 'no_clear_phase' + raisons + diagnostics
        """

        analysis_start_time = time.perf_counter()

        def _neutral(
            asset_sym: str, reason: str, extra: dict | None = None
        ) -> Dict[str, Any]:
            base = {
                "asset": asset_sym,
                "phase": "no_clear_phase",
                "multi_tf_enabled": True,
                "quality": {"overall_score": 0.0},
                "reason": reason,
                "timeframes_used": [],
                "confluence_weights": {},
                "analysis_errors": None,
                "cache_efficiency_pct": 0.0,
                "data_freshness": {},
            }
            if extra:
                base.update(extra)
            # log synthétique
            self.logger.info(f"⛔ [{asset_sym}] MTF neutre (no trade): {reason}")
            return base

        # ========== PHASE 1: VALIDATION & INIT ==========
        try:
            multi_tf_config = (strategy_config.get("phase_detection", {}) or {}).get(
                "multi_timeframe", {}
            ) or {}
        except Exception:
            multi_tf_config = {}

        if not bool(multi_tf_config.get("enabled", False)):
            self.logger.debug(f"[{asset}] Multi-TF désactivé (strict, no fallback).")
            return _neutral(asset, "mtf_disabled")

        timeframes = multi_tf_config.get("timeframes", ["M1", "M5", "M15"]) or [
            "M1",
            "M5",
            "M15",
        ]
        timeframes = [str(tf).upper().strip() for tf in timeframes if str(tf).strip()]
        timeframes = list(dict.fromkeys(timeframes))

        confluence_weights = multi_tf_config.get(
            "confluence_weights", {"M1": 0.5, "M5": 0.3, "M15": 0.2}
        ) or {"M1": 0.5, "M5": 0.3, "M15": 0.2}

        def _normalize_weights(d: _Dict[str, float]) -> _Dict[str, float]:
            w = {k.upper(): float(v) for k, v in d.items() if k}
            total = sum(max(0.0, v) for v in w.values())
            if total <= 0:
                n = max(1, len(w)) or 1
                return {k: 1.0 / n for k in (w or {"M1": 1.0})}
            return {k: max(0.0, v) / total for k, v in w.items()}

        confluence_weights = _normalize_weights(confluence_weights)

        cache_ttl_seconds = int(multi_tf_config.get("cache_ttl_seconds", 30) or 30)
        quality_threshold = float(multi_tf_config.get("min_quality_score", 0.7) or 0.7)

        # Fraîcheur max par TF (~2 bougies)
        tf_max_stale = multi_tf_config.get("max_tf_staleness_seconds", {}) or {}

        def _tf_to_seconds(tf: str) -> int:
            m = tf.upper()
            if m.endswith("MIN"):
                try:
                    return int(m[:-3]) * 60
                except:
                    return 60
            if m.startswith("M"):
                try:
                    return int(m[1:]) * 60
                except:
                    return 60
            if m.startswith("H"):
                try:
                    return int(m[1:]) * 3600
                except:
                    return 3600
            return 60

        default_max_stale_by_tf = {tf: 2 * _tf_to_seconds(tf) for tf in timeframes}
        for k, v in list(tf_max_stale.items()):
            try:
                default_max_stale_by_tf[str(k).upper()] = int(v)
            except Exception:
                pass

        self.logger.info(f"🎯 [{asset}] KATANA Multi-TF (STRICT) activé: {timeframes}")

        # ========== PHASE 2: ACQUISITION + FRAÎCHEUR ==========
        tf_data_cache: Dict[str, pd.DataFrame] = {}
        cache_hits = 0
        data_freshness: Dict[str, _Dict[str, _Any]] = {}

        now_epoch = int(time.time())
        now_bucket = int(now_epoch // max(1, cache_ttl_seconds))

        for tf in timeframes:
            cache_key = f"{asset}_{tf}_{now_bucket}"
            cached_ok = False
            if hasattr(self, "_tf_data_cache"):
                cached_item = getattr(self, "_tf_data_cache", {}).get(cache_key)
                if isinstance(cached_item, pd.DataFrame):
                    tf_data_cache[tf] = cached_item
                    cache_hits += 1
                    cached_ok = True
                    self.logger.debug(f"🚀 [{asset}] Cache HIT pour {tf}")
                elif isinstance(cached_item, dict) and isinstance(
                    cached_item.get("data"), pd.DataFrame
                ):
                    tf_data_cache[tf] = cached_item["data"]
                    cache_hits += 1
                    cached_ok = True
                    self.logger.debug(f"🚀 [{asset}] Cache HIT(meta) pour {tf}")

            if not cached_ok:
                try:
                    tf_data = self._fetch_timeframe_data(asset, tf, multi_tf_config)
                    if tf_data is not None and not tf_data.empty:
                        tf_data_cache[tf] = tf_data
                        if not hasattr(self, "_tf_data_cache"):
                            self._tf_data_cache = {}
                        self._tf_data_cache[cache_key] = {
                            "data": tf_data,
                            "fetched_at": now_epoch,
                        }
                        self.logger.debug(
                            f"📡 [{asset}] Données {tf} acquises: {len(tf_data)} barres"
                        )
                    else:
                        self.logger.warning(f"⚠️ [{asset}] Échec acquisition {tf}")
                        continue
                except Exception as e:
                    self.logger.error(
                        f"💥 [{asset}] Erreur fetch {tf}: {e}", exc_info=False
                    )
                    continue

            # Fraîcheur dernière barre
            try:
                df_tf = tf_data_cache[tf]
                ts_col = next(
                    (
                        c
                        for c in ("timestamp", "time", "datetime", "ts")
                        if c in df_tf.columns
                    ),
                    None,
                )

                age_sec = 0.0
                if ts_col:
                    last_ts = df_tf[ts_col].iloc[-1]
                    if hasattr(last_ts, "to_pydatetime"):
                        last_dt = last_ts.to_pydatetime()
                    elif isinstance(last_ts, (int, float)) and last_ts > 1e9:
                        last_dt = datetime.fromtimestamp(
                            float(last_ts) / 1000.0, tz=timezone.utc
                        )
                    elif isinstance(last_ts, (int, float)):
                        last_dt = datetime.fromtimestamp(
                            float(last_ts), tz=timezone.utc
                        )
                    else:
                        last_dt = datetime.fromisoformat(str(last_ts))
                        if last_dt.tzinfo is None:
                            last_dt = last_dt.replace(tzinfo=timezone.utc)
                    age_sec = max(
                        0.0, (datetime.now(timezone.utc) - last_dt).total_seconds()
                    )
                max_stale = int(default_max_stale_by_tf.get(tf, 120))
                data_freshness[tf] = {
                    "age_sec": float(age_sec),
                    "max_allowed": float(max_stale),
                }
                if age_sec > max_stale:
                    self.logger.warning(
                        f"⚠️ [{asset}] {tf} stale: {age_sec:.1f}s > {max_stale}s (skipped)"
                    )
                    tf_data_cache.pop(tf, None)
            except Exception as e:
                self.logger.debug(f"[{asset}] Freshness check {tf} fail: {e}")

        if len(tf_data_cache) < 2:
            return _neutral(
                asset,
                "not_enough_tf_data",
                {
                    "timeframes_used": list(tf_data_cache.keys()),
                    "data_freshness": data_freshness,
                },
            )

        cache_efficiency = (cache_hits / max(1, len(timeframes))) * 100.0
        self.logger.debug(f"📊 [{asset}] Cache efficiency: {cache_efficiency:.1f}%")

        # ========== PHASE 3: ANALYSE PAR TF ==========
        tf_analyses: Dict[str, Dict[str, Any]] = {}
        analysis_errors: list[str] = []

        for tf, tf_data in tf_data_cache.items():
            tf_config = self._get_tf_specific_config(tf, multi_tf_config)
            original_params = self._backup_current_params()
            try:
                self._load_settings(overrides=tf_config)
                last_signals = self.analyze_last_bar(tf_data, asset_symbol=asset)
                if not last_signals:
                    raise ValueError(f"Analyse {tf} vide")

                # 🔥 Enrichissement Candle Patterns
                try:
                    # On relance le détecteur de patterns chandeliers sur le DataFrame
                    candle_signals = self.detectors.detect_candle_patterns(tf_data)

                    if candle_signals and isinstance(candle_signals, list):
                        last_candle = candle_signals[-1] if candle_signals else None
                        if last_candle:
                            # On enrichit last_signals (déjà produit par analyze_last_bar)
                            last_signals.update(
                                {
                                    "candle_pattern": last_candle.get("pattern"),
                                    "candle_strength": last_candle.get(
                                        "strength_score"
                                    ),
                                }
                            )
                        else:
                            # Pas de pattern → on laisse vide
                            last_signals.update(
                                {
                                    "candle_pattern": None,
                                    "candle_strength": 0.0,
                                }
                            )
                    else:
                        last_signals.update(
                            {
                                "candle_pattern": None,
                                "candle_strength": 0.0,
                            }
                        )

                except Exception as e:
                    self.logger.warning(f"[{asset}] Erreur candle_patterns {tf}: {e}")
                    last_signals.update(
                        {
                            "candle_pattern": None,
                            "candle_strength": 0.0,
                        }
                    )

                    tf_analyses[tf] = last_signals

                # 🔥 Ajout footprints en mémoire (FIFO)
                try:
                    footprints_hist = self.memory.get_memory(asset).caches.get(
                        "footprints", []
                    )
                    tf_analyses[tf]["footprints_history"] = footprints_hist[-5:]
                except Exception as e_mem:
                    self.logger.warning(
                        f"[{asset}] {tf} impossible de récupérer footprints: {e_mem}"
                    )
                    tf_analyses[tf]["footprints_history"] = []

                # 🔥 Sauvegarde des zones HTF (OB/FVG) pour usage Liquidity
                try:
                    if tf in (
                        "M15",
                        "H1",
                    ):  # tu peux adapter selon ce que tu veux comme HTF
                        if "ob_details" in last_signals and last_signals["ob_details"]:
                            last_signals[f"ob_details_{tf}"] = last_signals[
                                "ob_details"
                            ]
                        if (
                            "fvg_details" in last_signals
                            and last_signals["fvg_details"]
                        ):
                            last_signals[f"fvg_details_{tf}"] = last_signals[
                                "fvg_details"
                            ]
                except Exception as e:
                    self.logger.warning(
                        f"[{asset}] Erreur stockage zones HTF {tf}: {e}"
                    )

                self.logger.debug(
                    f"✅ [{asset}] {tf} analysé: Phase={last_signals.get('phase')}"
                )

            except Exception as e:
                analysis_errors.append(f"{tf}: {str(e)}")
                self.logger.error(
                    f"💥 [{asset}] Erreur analyse {tf}: {e}", exc_info=False
                )
            finally:
                try:
                    self._restore_params(original_params)
                except Exception as e:
                    self.logger.error(
                        f"💥 [{asset}] Restore params {tf} échoué: {e}", exc_info=False
                    )

        if len(tf_analyses) < 2:
            return _neutral(
                asset,
                "insufficient_tf_analyses",
                {
                    "timeframes_used": list(tf_analyses.keys()),
                    "analysis_errors": analysis_errors,
                    "data_freshness": data_freshness,
                    "cache_efficiency_pct": round(cache_efficiency, 1),
                },
            )

        # ========== PHASE 4: CONFLUENCE & DIVERGENCES ==========
        present_weights = _normalize_weights(
            {tf: confluence_weights.get(tf, 0.0) for tf in tf_analyses.keys()}
        )
        confluence_result = self._calculate_advanced_confluence(
            tf_analyses, present_weights, asset
        )
        divergence_analysis = self._detect_tf_divergences(tf_analyses)

        # ========== PHASE 5: QUALITÉ ==========
        quality_metrics = self._calculate_quality_metrics(
            tf_analyses, confluence_result, divergence_analysis, analysis_start_time
        )

        if float(quality_metrics.get("overall_score", 0.0)) < quality_threshold:
            # STRICT: pas de fallback ; on sort neutre (no trade)
            return _neutral(
                asset,
                "quality_below_threshold",
                {
                    "timeframes_used": list(tf_analyses.keys()),
                    "confluence_weights": present_weights,
                    "analysis_errors": analysis_errors or None,
                    "cache_efficiency_pct": round(cache_efficiency, 1),
                    "data_freshness": data_freshness,
                    "quality": quality_metrics,
                    "confluence": {
                        "phase": confluence_result.get("phase"),
                        "confluence_score": confluence_result.get("confluence_score"),
                        "phase_consistency": confluence_result.get("phase_consistency"),
                    },
                },
            )

        # ========== PHASE 6: RÉPONSE FINALE ==========
        final_signals = self._build_enhanced_signals(
            confluence_result, quality_metrics, tf_analyses, asset
        )
        try:
            final_signals.update(
                {
                    "multi_tf_enabled": True,
                    "timeframes_used": list(tf_analyses.keys()),
                    "confluence_weights": present_weights,
                    "analysis_errors": analysis_errors or None,
                    "cache_efficiency_pct": round(cache_efficiency, 1),
                    "data_freshness": data_freshness,
                }
            )
        except Exception:
            pass

        execution_time = (time.perf_counter() - analysis_start_time) * 1000.0
        self.logger.info(
            f"🎯 [{asset}] KATANA Multi-TF (STRICT) terminé: "
            f"Phase={final_signals.get('phase')} | "
            f"Qualité={quality_metrics.get('overall_score', 0.0):.3f} | "
            f"TFs={final_signals.get('timeframes_used')} | "
            f"Temps={execution_time:.1f}ms"
        )
        return final_signals

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
        reporter = PhaseObserverReporter(
            config_manager=self.config_manager, logger=self.logger
        )
        report = reporter.run_daily_report_for_asset(
            symbol, frames_by_tf, tz=timezone.utc
        )

        date_tag = str(datetime.now(timezone.utc).date())
        base_dir = self.config_manager.get("reporting.base_dir", "reports")
        os.makedirs(base_dir, exist_ok=True)

        md_path = os.path.join(base_dir, f"{symbol}_{date_tag}_phase_report.md")
        jsonl_path = os.path.join(base_dir, f"{symbol}_{date_tag}_phase_report.jsonl")

        reporter.export_markdown(report, md_path)
        reporter.export_jsonl(report, jsonl_path)
        self.logger.info(f"[REPORT] {symbol} → {md_path} | {jsonl_path}")
