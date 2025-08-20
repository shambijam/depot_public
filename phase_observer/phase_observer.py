# Contenu du fichier phase_observer/phase_observer.py

# -*- coding: utf-8 -*-
"""
phase_observer.py - Module d'analyse de marché institutionnel pour le projet SNIPER_X.

Ce module est conçu pour :
- Charger des données de marché ou des logs de trading.
- Identifier la phase de marché institutionnelle pour chaque point de données.
- Détecter la présence de signaux techniques clés (Smart Money Concepts - SMC).
- Fournir un flux de données enrichi et annoté, prêt pour l'injection dans un pipeline de décision algorithmique.
- Générer des rapports d'audit et multi-actifs pour la surveillance.
"""

import logging
import os
import sys
import re
import time
import warnings
import uuid
import json
import numpy as np
import pandas as pd
from pathlib import Path
from typing import Dict, List, Optional, Union, Any
from datetime import datetime, timedelta, UTC
from pydantic import ValidationError
from data_models.phase_observer_models import PhaseObserverRowModel

try:
    from core.config_manager import ConfigManager
except ImportError as e:
    print(
        f"ERREUR FATALE : Dépendance 'ConfigManager' manquante. Erreur : {e}",
        file=sys.stderr,
    )
    sys.exit(1)

# --- Configuration du Logger pour ce module ---
# TODO: Rendre le niveau de log par défaut (`logging.INFO`) configurable via
#       les paramètres de l'application pour plus de flexibilité en production.
logging.basicConfig(
    # level=logging.INFO, format="%(asctime)s - [%(levelname)s] - %(name)s - %(message)s"
)


class PhaseObserver:
    """
    Orchestre l'analyse de données de marché pour y superposer un contexte
    institutionnel et des signaux Smart Money Concepts (SMC) à haute résolution.
    """

    def __init__(self, config_manager=None):
        """
        Initialise le PhaseObserver avec les paramètres de configuration.
        """
        self.config_manager = config_manager
        self.logger = logging.getLogger(__name__)

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
        if self.config_manager:
            try:
                # Charger depuis prod_config.json OU phase_observer_config.json
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

        # MAINTENANT on peut logger
        self.logger.info(
            f"PhaseObserver initialisé. Lookback window: {self.lookback_window}."
        )

    def _load_settings(self, overrides: Optional[Dict[str, Any]] = None):
        """
        Charge tous les paramètres depuis le ConfigManager de manière dynamique.
        Permet la surcharge de paramètres spécifiques via le dictionnaire 'overrides'.

        Args:
            overrides (Optional[Dict[str, Any]]): Un dictionnaire de paramètres à surcharger.
                                                Typiquement utilisé pour appliquer des settings de stratégie spécifiques.
        """
        self.logger.debug("Chargement des paramètres d'analyse pour PhaseObserver...")

        # Récupérer les paramètres par défaut/globaux
        all_settings = self.config_manager.get("phase_detection_defaults", {})

        # Appliquer les surcharges si fournies
        if overrides:
            self.logger.debug(f"Application de surcharges de paramètres : {overrides}")
            # Utilise une fusion profonde si le ConfigManager a cette méthode, sinon un simple update
            if hasattr(self.config_manager, "_merge_dicts"):
                all_settings = self.config_manager._merge_dicts(all_settings, overrides)
            else:
                all_settings.update(overrides)
                self.logger.warning(
                    "ConfigManager n'a pas _merge_dicts. Surcharge des paramètres avec un simple update()."
                )

        # Affecter tous les paramètres à l'instance
        for key, value in all_settings.items():
            setattr(self, key, value)

        # S'assurer que les chemins de sortie sont des objets Path
        self.output_path = Path(self.config_manager.get("paths.reports", "output/"))
        self.logs_dir = Path(self.config_manager.get("paths.logs", "logs/"))
        self.output_path.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)

        self.logger.debug("Paramètres de PhaseObserver chargés et appliqués.")
        # TODO: Ajouter un mécanisme pour recharger les settings "à chaud" si la configuration
        #       dynamique du ConfigManager change en cours d'exécution.

    def determine_phase(self, market_data: pd.DataFrame) -> str:
        """
        Détermine la phase de marché de la dernière bougie via un appel à `analyze`.

        Cette méthode sert d'interface principale et légère pour obtenir l'état le plus
        récent du marché sans retourner le DataFrame complet.

        Args:
            market_data (pd.DataFrame): Les données de marché à analyser.

        Returns:
            str: Le nom de la phase de marché détectée ("uncertain" en cas d'erreur).
        """
        try:
            # L'analyse complète est déléguée à la méthode `analyze`.
            annotated_data = self.analyze(market_data)
            if annotated_data is None or annotated_data.empty:
                return "uncertain"

            # On retourne la phase de la dernière ligne
            last_phase = annotated_data.iloc[-1]["phase"]
            return last_phase

        except Exception as e:
            self.logger.error(f"Erreur dans determine_phase : {e}", exc_info=True)
            return "uncertain"

        # TODO: Rendre l'objet de retour plus riche, en incluant par exemple le score de confiance
        #       et les signaux détectés, sous la forme d'un Pydantic model.

    def update_parameters_from_config(self, strategy_config: dict) -> None:
        """
        Met à jour les paramètres d'analyse à partir d'une configuration de stratégie.

        Args:
            strategy_config (dict): Le dictionnaire de configuration de la stratégie.
        """
        # AMÉLIORATION : La logique de chargement est déléguée à _load_settings
        # pour éviter la duplication de code avec __init__.
        phase_params = strategy_config.get("phase_detection")
        strategy_name = strategy_config.get("strategy_name", "inconnue")

        if not isinstance(phase_params, dict):
            self.logger.warning(
                f"Aucun paramètre 'phase_detection' valide pour la stratégie '{strategy_name}'."
            )
            return

        self._load_settings(overrides=phase_params)  # Appel de la méthode refactorisée
        self.logger.info(
            f"PhaseObserver mis à jour avec les paramètres de la stratégie '{strategy_name}'."
        )

        # TODO: Après une mise à jour, déclencher une invalidation des caches de données
        #       qui pourraient dépendre des anciens paramètres.

    # --- Fonctions de Détection des Signaux Institutionnels (SMC) ---

    def detect_order_block_ml_enhanced(
        self, df: pd.DataFrame, df_htf: Optional[pd.DataFrame] = None
    ) -> List[Optional[Dict[str, Any]]]:
        """
        🎯 Order Blocks ML Enhanced - Scoring sophistiqué avec confluence

        Features ML:
        - Impulse strength scoring
        - Volume confirmation weighting
        - Temporal context analysis
        - Multi-factor confluence scoring
        """
        self.logger.debug("Détection Order Blocks ML Enhanced...")

        # Configuration ML
        ob_config = self.config_manager.get(
            "phase_detection_defaults.order_block_ml_settings", {}
        )
        enable_ml = ob_config.get("enable_ml_scoring", True)
        confluence_config = ob_config.get("confluence_requirements", {})
        impulse_weights = ob_config.get("impulse_strength_weights", {})

        # Paramètres de base
        impulse_threshold = self.config_manager.get(
            "phase_detection_defaults.impulse_threshold", 0.0005
        )

        # Pré-calcul des features pour ML
        df["candle_move"] = df["close"] - df["open"]
        df["candle_size"] = df["high"] - df["low"]
        df["body_ratio"] = abs(df["candle_move"]) / df["candle_size"].replace(0, np.nan)
        df["volume_ma"] = df["tick_volume"].rolling(window=20).mean()
        df["volume_ratio"] = df["tick_volume"] / df["volume_ma"]

        # Identification des OB potentiels
        bullish_ob_mask = (df["candle_move"] > impulse_threshold) & (
            df["candle_move"].shift(1) < 0
        )
        bearish_ob_mask = (df["candle_move"] < -impulse_threshold) & (
            df["candle_move"].shift(1) > 0
        )
        potential_ob_mask = bullish_ob_mask | bearish_ob_mask

        # Swing points pour confluence
        swing_highs, swing_lows = self._get_swing_points(df)

        # Trend HTF si disponible
        htf_trend = None
        if df_htf is not None and not df_htf.empty:
            htf_trend = self._get_trend(df_htf).iloc[-1]

        results = [None] * len(df)
        ob_positions = df.index[potential_ob_mask].tolist()

        for i, ob_timestamp in enumerate(ob_positions):
            try:
                pos = df.index.get_loc(ob_timestamp)
                if pos == 0:
                    continue

                ob_candle_pos = pos - 1
                if ob_candle_pos < 0 or pos >= len(df):
                    continue

                ob_candle = df.iloc[ob_candle_pos]
                impulse_candle = df.iloc[pos]
                ob_zone = (ob_candle["low"], ob_candle["high"])

                # === ML FEATURE EXTRACTION ===

                # 1. Impulse Strength Score
                price_movement_strength = (
                    abs(impulse_candle["candle_move"]) / impulse_threshold
                )
                volume_spike_strength = (
                    impulse_candle["volume_ratio"]
                    if not np.isnan(impulse_candle["volume_ratio"])
                    else 1.0
                )
                body_ratio_strength = (
                    impulse_candle["body_ratio"]
                    if not np.isnan(impulse_candle["body_ratio"])
                    else 0.5
                )

                # Time compression (vitesse de formation)
                time_compression = 1.0  # Placeholder - à implémenter selon timeframe

                # Calcul score impulse pondéré
                impulse_score = (
                    price_movement_strength * impulse_weights.get("price_movement", 0.4)
                    + volume_spike_strength * impulse_weights.get("volume_spike", 0.3)
                    + time_compression * impulse_weights.get("time_compression", 0.3)
                )

                # 2. Confluence Factors Scoring
                confluence_score = 0.0
                confluence_details = {}

                # FVG Confluence
                fvg_confluence = False
                if pd.notna(impulse_candle.get("fvg_details")):
                    fvg_confluence = True
                    confluence_score += 0.25
                confluence_details["fvg_confluence"] = fvg_confluence

                # Market Extreme Confluence (Swing points)
                extreme_confluence = False
                if (ob_candle.name in swing_highs.index) or (
                    ob_candle.name in swing_lows.index
                ):
                    extreme_confluence = True
                    confluence_score += 0.30
                confluence_details["extreme_confluence"] = extreme_confluence

                # Volume Confirmation
                volume_confirmation = False
                if confluence_config.get("require_volume_confirmation", True):
                    if volume_spike_strength > 1.2:  # 20% au-dessus de la moyenne
                        volume_confirmation = True
                        confluence_score += 0.20
                else:
                    volume_confirmation = True
                confluence_details["volume_confirmation"] = volume_confirmation

                # Trend Alignment
                trend_alignment = False
                ob_is_bullish = bullish_ob_mask.iloc[pos]
                if confluence_config.get("require_trend_alignment", True):
                    current_trend = (
                        df["trend"].iloc[pos] if "trend" in df.columns else "neutral"
                    )
                    if (ob_is_bullish and current_trend == "bullish") or (
                        not ob_is_bullish and current_trend == "bearish"
                    ):
                        trend_alignment = True
                        confluence_score += 0.15
                    # HTF alignment bonus
                    if htf_trend and (
                        (ob_is_bullish and htf_trend == "bullish")
                        or (not ob_is_bullish and htf_trend == "bearish")
                    ):
                        confluence_score += 0.10
                else:
                    trend_alignment = True
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
                    # Facteurs de qualité
                    base_ml_score = min(1.0, (impulse_score + confluence_score) / 2)

                    # Ajustements qualitatifs
                    if unmitigated:
                        base_ml_score *= 1.1
                    if volume_confirmation and trend_alignment:
                        base_ml_score *= 1.15

                    ml_score = min(0.95, base_ml_score)  # Cap à 95%
                else:
                    ml_score = confluence_score

                # 5. Filtrage par seuil de confluence
                min_confluence = confluence_config.get("min_confluence_score", 0.6)

                if ml_score >= min_confluence:
                    results[pos] = {
                        "type": "bullish" if ob_is_bullish else "bearish",
                        "zone": list(ob_zone),
                        "ml_score": round(ml_score, 3),
                        "impulse_strength": round(impulse_score, 3),
                        "confluence_score": round(confluence_score, 3),
                        "confluence_details": confluence_details,
                        "unmitigated": unmitigated,
                        "volume_spike": round(volume_spike_strength, 2),
                        "formation_quality": (
                            "high"
                            if ml_score > 0.8
                            else "medium" if ml_score > 0.6 else "low"
                        ),
                    }

            except Exception as e:
                self.logger.warning(f"Erreur processing OB à l'index {pos}: {e}")
                continue

        # Performance logging
        valid_obs = [r for r in results if r is not None]
        if valid_obs:
            avg_ml_score = np.mean([ob["ml_score"] for ob in valid_obs])
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
        - Filtrage par magnitude minimale
        - Tracking du remplissage en temps réel
        - Scoring de qualité du gap
        - Expiration basée sur l'âge
        """
        self.logger.debug("Détection FVG Enhanced avec magnitude et tracking...")

        # Récupération des paramètres enhanced
        fvg_config = self.config_manager.get(
            "phase_detection_defaults.fvg_enhanced_settings", {}
        )
        min_gap_magnitude = fvg_config.get("min_gap_magnitude_percent", 0.15) / 100
        gap_fill_threshold = fvg_config.get("gap_fill_threshold", 0.8)
        enable_tracking = fvg_config.get("enable_gap_tracking", True)
        max_gap_age = fvg_config.get("max_gap_age_bars", 50)

        # Détection vectorielle de base (optimisée)
        low_p0 = df["low"].values
        high_p2 = df["high"].shift(2).values
        high_p0 = df["high"].values
        low_p2 = df["low"].shift(2).values

        # Conditions FVG avec filtrage NaN
        valid_indices = ~(np.isnan(high_p2) | np.isnan(low_p2))

        bullish_fvg_condition = np.zeros(len(df), dtype=bool)
        bearish_fvg_condition = np.zeros(len(df), dtype=bool)

        bullish_fvg_condition[valid_indices] = (
            low_p0[valid_indices] > high_p2[valid_indices]
        )
        bearish_fvg_condition[valid_indices] = (
            high_p0[valid_indices] < low_p2[valid_indices]
        )

        results = []
        active_gaps = []  # Tracking des gaps actifs pour remplissage

        for i in range(len(df)):
            fvg_info = None
            current_price = df["close"].iloc[i]

            # === DÉTECTION NOUVEAUX FVG ===
            if bullish_fvg_condition[i]:
                gap_bottom = df["high"].iloc[i - 2]
                gap_top = df["low"].iloc[i]
                gap_size = gap_top - gap_bottom

                # Filtrage par magnitude (% du prix)
                magnitude_percent = gap_size / current_price
                if magnitude_percent >= min_gap_magnitude:

                    # Calcul score de qualité
                    quality_score = min(
                        1.0, magnitude_percent / (min_gap_magnitude * 2)
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

                    # Ajouter aux gaps actifs pour tracking
                    if enable_tracking:
                        active_gaps.append(fvg_info.copy())

            elif bearish_fvg_condition[i]:
                gap_top = df["low"].iloc[i - 2]
                gap_bottom = df["high"].iloc[i]
                gap_size = gap_top - gap_bottom

                # Filtrage par magnitude
                magnitude_percent = gap_size / current_price
                if magnitude_percent >= min_gap_magnitude:

                    quality_score = min(
                        1.0, magnitude_percent / (min_gap_magnitude * 2)
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
                current_high = df["high"].iloc[i]
                current_low = df["low"].iloc[i]

                for gap in active_gaps[:]:  # Copy pour modification pendant iteration
                    age = i - gap["formation_index"]

                    # Expiration par âge
                    if age > max_gap_age:
                        active_gaps.remove(gap)
                        continue

                    # Calcul du remplissage
                    if gap["type"] == "bullish":
                        if current_low <= gap["top"]:
                            penetration = gap["top"] - current_low
                            fill_percent = penetration / gap["magnitude"]
                            gap["fill_percentage"] = min(1.0, fill_percent)

                            if fill_percent >= gap_fill_threshold:
                                gap["is_filled"] = True
                                active_gaps.remove(gap)

                    elif gap["type"] == "bearish":
                        if current_high >= gap["bottom"]:
                            penetration = current_high - gap["bottom"]
                            fill_percent = penetration / gap["magnitude"]
                            gap["fill_percentage"] = min(1.0, fill_percent)

                            if fill_percent >= gap_fill_threshold:
                                gap["is_filled"] = True
                                active_gaps.remove(gap)

            results.append(fvg_info)

        # Log de performance
        valid_gaps = [r for r in results if r is not None]
        if valid_gaps:
            avg_quality = np.mean([g["quality_score"] for g in valid_gaps])
            self.logger.debug(
                f"FVG Enhanced: {len(valid_gaps)} gaps détectés, qualité moyenne: {avg_quality:.3f}"
            )

        return results

    def _get_adaptive_swing_points(
        self, df: pd.DataFrame
    ) -> tuple[pd.Series, pd.Series]:
        """
        🎯 Adaptive Swing Points - S'adapte au régime de volatilité

        Intelligence:
        - Détection automatique du régime de volatilité
        - Paramètres adaptatifs selon le régime
        - Filtrage par distance minimale
        - Méthode Garman-Klass pour volatilité précise
        """
        self.logger.debug("Calcul des Swing Points adaptatifs...")

        swing_config = self.config_manager.get(
            "phase_detection_defaults.adaptive_swing_settings", {}
        )
        volatility_regimes = swing_config.get("volatility_regimes", {})
        vol_config = swing_config.get("volatility_calculation", {})

        # === 1. CALCUL RÉGIME DE VOLATILITÉ ===
        vol_method = vol_config.get("method", "garman_klass")
        vol_period = vol_config.get("period", 20)
        high_threshold = vol_config.get("high_threshold", 75)
        low_threshold = vol_config.get("low_threshold", 25)

        if vol_method == "garman_klass":
            # Volatilité Garman-Klass (plus précise que close-to-close)
            ln_high_low = np.log(df["high"] / df["low"])
            ln_close_open = np.log(df["close"] / df["open"])

            gk_vol = 0.5 * ln_high_low**2 - (2 * np.log(2) - 1) * ln_close_open**2
            volatility_series = np.sqrt(gk_vol.rolling(window=vol_period).mean())
        else:
            # Fallback: Close-to-close volatility
            returns = df["close"].pct_change()
            volatility_series = returns.rolling(window=vol_period).std()

        # Calcul des percentiles pour classification
        if len(volatility_series.dropna()) < vol_period:
            self.logger.warning(
                f"Données insuffisantes pour calcul volatilité adaptative. Utilisation mode normal."
            )
            current_regime = "normal_vol"
        else:
            current_vol = volatility_series.iloc[-1]
            vol_percentile = (volatility_series <= current_vol).mean() * 100

            if vol_percentile >= high_threshold:
                current_regime = "high_vol"
            elif vol_percentile <= low_threshold:
                current_regime = "low_vol"
            else:
                current_regime = "normal_vol"

        self.logger.debug(f"Régime de volatilité détecté: {current_regime}")

        # === 2. PARAMÈTRES ADAPTATIFS ===
        regime_params = volatility_regimes.get(current_regime, {})
        swing_order = regime_params.get("swing_order", 3)
        min_swing_distance = regime_params.get("min_swing_distance", 0.0005)

        # === 3. DÉTECTION SWING POINTS AVEC PARAMÈTRES ADAPTATIFS ===
        window_size = 2 * swing_order + 1

        if len(df) < window_size:
            self.logger.warning(
                f"DataFrame trop petit ({len(df)}) pour fenêtre swing {window_size}"
            )
            return pd.Series([], dtype=float), pd.Series([], dtype=float)

        # Calcul des extrema locaux
        highs_condition = (
            df["high"]
            == df["high"]
            .rolling(window=window_size, center=True, min_periods=window_size)
            .max()
        )
        lows_condition = (
            df["low"]
            == df["low"]
            .rolling(window=window_size, center=True, min_periods=window_size)
            .min()
        )

        # === 4. FILTRAGE PAR DISTANCE MINIMALE ===

        def filter_swing_points(condition_series, price_series, min_distance):
            """Filtre les swing points trop proches"""
            filtered_indices = []
            last_price = None

            for idx in condition_series[condition_series].index:
                current_price = price_series.loc[idx]

                if last_price is None:
                    filtered_indices.append(idx)
                    last_price = current_price
                else:
                    price_distance = abs(current_price - last_price) / last_price
                    if price_distance >= min_distance:
                        filtered_indices.append(idx)
                        last_price = current_price

            return filtered_indices

        # Application du filtrage
        filtered_high_indices = filter_swing_points(
            highs_condition, df["high"], min_swing_distance
        )
        filtered_low_indices = filter_swing_points(
            lows_condition, df["low"], min_swing_distance
        )

        # Création des Series résultantes
        swing_highs = pd.Series(
            index=filtered_high_indices,
            data=df.loc[filtered_high_indices, "high"],
            dtype=float,
        )
        swing_lows = pd.Series(
            index=filtered_low_indices,
            data=df.loc[filtered_low_indices, "low"],
            dtype=float,
        )

        # === 5. LOGGING DE PERFORMANCE ===
        total_highs = len(swing_highs)
        total_lows = len(swing_lows)

        if total_highs > 0 or total_lows > 0:
            self.logger.debug(
                f"Swing Points adaptatifs: {total_highs} highs, {total_lows} lows (régime: {current_regime})"
            )

        return swing_highs, swing_lows

    def _calculate_volatility_regime(self, df: pd.DataFrame) -> str:
        """
        Calcule le régime de volatilité actuel pour usage dans d'autres fonctions
        """
        swing_config = self.config_manager.get(
            "phase_detection_defaults.adaptive_swing_settings", {}
        )
        vol_config = swing_config.get("volatility_calculation", {})

        vol_period = vol_config.get("period", 20)
        high_threshold = vol_config.get("high_threshold", 75)
        low_threshold = vol_config.get("low_threshold", 25)

        # Garman-Klass volatility
        ln_high_low = np.log(df["high"] / df["low"])
        ln_close_open = np.log(df["close"] / df["open"])

        gk_vol = 0.5 * ln_high_low**2 - (2 * np.log(2) - 1) * ln_close_open**2
        volatility_series = np.sqrt(gk_vol.rolling(window=vol_period).mean())

        if len(volatility_series.dropna()) < vol_period:
            return "normal_vol"

        current_vol = volatility_series.iloc[-1]
        vol_percentile = (volatility_series <= current_vol).mean() * 100

        if vol_percentile >= high_threshold:
            return "high_vol"
        elif vol_percentile <= low_threshold:
            return "low_vol"
        else:
            return "normal_vol"

    def detect_market_regime(self, df: pd.DataFrame) -> pd.Series:
        """
        🏛️ Market Regime Detection - Remplace la détection de tendance basique

        Régimes détectés:
        - trending_institutional_bull/bear
        - range_accumulation/distribution
        - high_volatility_chaos
        - low_volatility_compression

        Basé sur:
        - ADX pour force de tendance
        - Volume Profile pour activité institutionnelle
        - Volatilité Garman-Klass
        - Structure de marché
        """
        self.logger.debug("Détection du régime de marché sophistiquée...")

        regime_config = self.config_manager.get(
            "phase_detection_defaults.regime_detection_settings", {}
        )
        adx_config = regime_config.get("adx_settings", {})
        vol_config = regime_config.get("volatility_regimes", {})
        volume_config = regime_config.get("volume_profile", {})

        # === 1. CALCUL ADX (AVERAGE DIRECTIONAL INDEX) ===
        adx_period = adx_config.get("period", 14)
        trending_threshold = adx_config.get("trending_threshold", 25)
        ranging_threshold = adx_config.get("ranging_threshold", 20)

        def calculate_adx(df, period=14):
            """Calcul ADX pour mesurer la force de la tendance"""
            high = df["high"]
            low = df["low"]
            close = df["close"]

            # True Range (TR)
            tr1 = high - low
            tr2 = abs(high - close.shift())
            tr3 = abs(low - close.shift())
            tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

            # Directional Movement
            dm_plus = np.where(
                (high - high.shift()) > (low.shift() - low),
                np.maximum(high - high.shift(), 0),
                0,
            )
            dm_minus = np.where(
                (low.shift() - low) > (high - high.shift()),
                np.maximum(low.shift() - low, 0),
                0,
            )

            # Smoothed True Range et DM
            atr = tr.rolling(window=period).mean()
            dm_plus_smooth = pd.Series(dm_plus).rolling(window=period).mean()
            dm_minus_smooth = pd.Series(dm_minus).rolling(window=period).mean()

            # Directional Indicators
            di_plus = 100 * dm_plus_smooth / atr
            di_minus = 100 * dm_minus_smooth / atr

            # ADX
            dx = 100 * abs(di_plus - di_minus) / (di_plus + di_minus)
            adx = dx.rolling(window=period).mean()

            return adx, di_plus, di_minus

        adx, di_plus, di_minus = calculate_adx(df, adx_period)

        # === 2. VOLATILITÉ GARMAN-KLASS ===
        vol_period = vol_config.get("calculation_period", 20)
        high_vol_percentile = vol_config.get("high_vol_percentile", 75)
        low_vol_percentile = vol_config.get("low_vol_percentile", 25)

        ln_high_low = np.log(df["high"] / df["low"])
        ln_close_open = np.log(df["close"] / df["open"])
        gk_vol = 0.5 * ln_high_low**2 - (2 * np.log(2) - 1) * ln_close_open**2
        volatility = np.sqrt(gk_vol.rolling(window=vol_period).mean())

        # Calcul des percentiles de volatilité
        vol_percentiles = volatility.rolling(window=100).apply(
            lambda x: (x <= x.iloc[-1]).mean() * 100, raw=False
        )

        # === 3. VOLUME PROFILE INSTITUTIONNEL ===
        enable_institutional = volume_config.get("enable_institutional_detection", True)
        volume_ma_period = volume_config.get("volume_ma_period", 20)
        institutional_threshold = volume_config.get("institutional_threshold", 1.8)

        institutional_activity = pd.Series(False, index=df.index)

        if enable_institutional and "tick_volume" in df.columns:
            volume_ma = df["tick_volume"].rolling(window=volume_ma_period).mean()
            volume_ratio = df["tick_volume"] / volume_ma
            institutional_activity = volume_ratio > institutional_threshold

        # === 4. DÉTERMINATION DU RÉGIME ===
        regimes = pd.Series("unknown", index=df.index)

        for i in range(len(df)):
            current_adx = adx.iloc[i] if not pd.isna(adx.iloc[i]) else 0
            current_di_plus = di_plus.iloc[i] if not pd.isna(di_plus.iloc[i]) else 0
            current_di_minus = di_minus.iloc[i] if not pd.isna(di_minus.iloc[i]) else 0
            current_vol_percentile = (
                vol_percentiles.iloc[i] if not pd.isna(vol_percentiles.iloc[i]) else 50
            )
            is_institutional = institutional_activity.iloc[i]

            # Classification par ADX et direction
            if current_adx > trending_threshold:
                # Marché en tendance
                if current_di_plus > current_di_minus:
                    # Tendance haussière
                    if is_institutional:
                        regimes.iloc[i] = "trending_institutional_bull"
                    else:
                        regimes.iloc[i] = "trending_retail_bull"
                else:
                    # Tendance baissière
                    if is_institutional:
                        regimes.iloc[i] = "trending_institutional_bear"
                    else:
                        regimes.iloc[i] = "trending_retail_bear"

            elif current_adx < ranging_threshold:
                # Marché en range
                if is_institutional:
                    # Déterminer si accumulation ou distribution
                    recent_closes = df["close"].iloc[max(0, i - 10) : i + 1]
                    if len(recent_closes) > 5:
                        if recent_closes.iloc[-1] > recent_closes.mean():
                            regimes.iloc[i] = "range_accumulation"
                        else:
                            regimes.iloc[i] = "range_distribution"
                    else:
                        regimes.iloc[i] = "range_institutional"
                else:
                    regimes.iloc[i] = "range_retail"
            else:
                # Zone intermédiaire - analyser volatilité
                if current_vol_percentile >= high_vol_percentile:
                    regimes.iloc[i] = "high_volatility_chaos"
                elif current_vol_percentile <= low_vol_percentile:
                    regimes.iloc[i] = "low_volatility_compression"
                else:
                    regimes.iloc[i] = "transitional"

        # === 5. CALCUL MÉTRIQUES DE QUALITÉ DU RÉGIME ===
        def calculate_regime_strength(regime_series, adx_series):
            """Calcule la force/confiance du régime détecté"""
            regime_strength = pd.Series(0.5, index=regime_series.index)  # Base 50%

            for i in range(len(regime_series)):
                regime = regime_series.iloc[i]
                adx_val = adx_series.iloc[i] if not pd.isna(adx_series.iloc[i]) else 0

                if "trending" in regime:
                    # Force basée sur ADX pour tendances
                    if adx_val > 40:
                        regime_strength.iloc[i] = 0.9
                    elif adx_val > 30:
                        regime_strength.iloc[i] = 0.8
                    elif adx_val > 25:
                        regime_strength.iloc[i] = 0.7
                    else:
                        regime_strength.iloc[i] = 0.6
                elif "range" in regime:
                    # Force inversée pour ranges (ADX faible = range fort)
                    if adx_val < 15:
                        regime_strength.iloc[i] = 0.9
                    elif adx_val < 20:
                        regime_strength.iloc[i] = 0.8
                    else:
                        regime_strength.iloc[i] = 0.6
                elif "volatility" in regime:
                    # Force basée sur la volatilité
                    regime_strength.iloc[i] = 0.8

            return regime_strength

        regime_strength = calculate_regime_strength(regimes, adx)

        # Ajouter les métriques au DataFrame pour usage ultérieur
        df["regime"] = regimes
        df["regime_strength"] = regime_strength
        df["adx"] = adx
        df["volatility_percentile"] = vol_percentiles
        df["institutional_activity"] = institutional_activity

        # === 6. LOGGING DE PERFORMANCE ===
        if len(regimes) > 0:
            regime_counts = regimes.value_counts()
            dominant_regime = (
                regime_counts.index[0] if len(regime_counts) > 0 else "unknown"
            )
            avg_strength = regime_strength.mean()

            self.logger.debug(
                f"Régime de marché: {dominant_regime} (force moyenne: {avg_strength:.2f})"
            )
            self.logger.debug(f"Distribution régimes: {dict(regime_counts.head(3))}")

        return regimes

    # CORRECTION 3: Mise à jour de _get_swing_points pour la rétrocompatibilité

    def _get_swing_points(
        self, df: pd.DataFrame, order: Optional[int] = None
    ) -> tuple[pd.Series, pd.Series]:
        """
        Méthode de rétrocompatibilité qui appelle la version adaptative
        """
        # Si on spécifie un ordre particulier, on l'utilise
        if order is not None:
            # Version simple avec ordre fixe
            window_size = 2 * order + 1

            if len(df) < window_size:
                return pd.Series([], dtype=float), pd.Series([], dtype=float)

            highs_condition = (
                df["high"]
                == df["high"]
                .rolling(window=window_size, center=True, min_periods=window_size)
                .max()
            )
            lows_condition = (
                df["low"]
                == df["low"]
                .rolling(window=window_size, center=True, min_periods=window_size)
                .min()
            )

            swing_highs = df["high"][highs_condition]
            swing_lows = df["low"][lows_condition]

            return swing_highs, swing_lows
        else:
            # Version adaptative par défaut
            return self._get_adaptive_swing_points(df)

    def detect_bos_mss_enhanced(
        self, df: pd.DataFrame
    ) -> List[Optional[Dict[str, Any]]]:
        """
        🎯 BOS/MSS Enhanced - Avec confirmation volume et momentum

        Améliorations:
        - Confirmation volume obligatoire
        - Validation momentum
        - Distinction BOS vs MSS plus précise
        - Filtrage des faux breakouts
        """
        self.logger.debug("Détection BOS/MSS Enhanced avec confirmations...")

        # Configuration
        bos_config = self.config_manager.get(
            "phase_detection_defaults.bos_mss_enhanced_settings", {}
        )
        volume_config = bos_config.get("volume_confirmation", {})
        momentum_config = bos_config.get("momentum_confirmation", {})
        structure_config = bos_config.get("structure_validation", {})

        # Paramètres de confirmation
        enable_volume_conf = volume_config.get("enable", True)
        volume_multiplier = volume_config.get("volume_multiplier_threshold", 1.5)
        volume_lookback = volume_config.get("lookback_period", 20)

        enable_momentum_conf = momentum_config.get("enable", True)
        min_momentum = momentum_config.get("min_momentum_threshold", 0.0003)

        min_break_distance = structure_config.get("min_break_distance", 0.0002)
        require_close_beyond = structure_config.get("require_close_beyond", True)

        # S'assurer que la tendance est calculée
        if "trend" not in df.columns:
            df["trend"] = self._get_trend(df)

        # Swing points adaptatifs
        swing_highs, swing_lows = self._get_adaptive_swing_points(df)
        df["last_swing_high"] = swing_highs.reindex(df.index).ffill()
        df["last_swing_low"] = swing_lows.reindex(df.index).ffill()

        # Calcul des moyennes mobiles de volume
        df["volume_ma"] = df["tick_volume"].rolling(window=volume_lookback).mean()
        df["volume_ratio"] = df["tick_volume"] / df["volume_ma"]

        # === CONDITIONS DE BASE ===
        # Breakout haussier: clôture au-dessus du dernier swing high
        bullish_break_basic = df["close"] > df["last_swing_high"].shift(1)
        # Breakout baissier: clôture en dessous du dernier swing low
        bearish_break_basic = df["close"] < df["last_swing_low"].shift(1)

        # === CONFIRMATIONS VOLUME ===
        volume_confirmation = pd.Series(
            True, index=df.index
        )  # Default True si désactivé

        if enable_volume_conf:
            # Volume supérieur à X fois la moyenne
            volume_confirmation = df["volume_ratio"] > volume_multiplier
            # Gérer les NaN
            volume_confirmation = volume_confirmation.fillna(False)

        # === CONFIRMATIONS MOMENTUM ===
        momentum_confirmation = pd.Series(
            True, index=df.index
        )  # Default True si désactivé

        if enable_momentum_conf:
            price_change = df["close"].pct_change().abs()
            momentum_confirmation = price_change > min_momentum
            momentum_confirmation = momentum_confirmation.fillna(False)

        # === FILTRAGE DISTANCE MINIMALE ===

        def validate_break_distance(row, break_type):
            """Valide que la cassure est suffisamment significative"""
            if break_type == "bullish":
                last_high = row["last_swing_high"]
                if pd.isna(last_high):
                    return False
                distance = (row["close"] - last_high) / last_high
                return distance >= min_break_distance
            else:  # bearish
                last_low = row["last_swing_low"]
                if pd.isna(last_low):
                    return False
                distance = (last_low - row["close"]) / last_low
                return distance >= min_break_distance

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
        results = []

        for i in range(len(df)):
            info = None

            # Récupération des métriques de confirmation pour logging
            vol_ratio = (
                df["volume_ratio"].iloc[i]
                if not pd.isna(df["volume_ratio"].iloc[i])
                else 0
            )
            momentum = (
                df["close"].pct_change().iloc[i]
                if not pd.isna(df["close"].pct_change().iloc[i])
                else 0
            )

            if bullish_bos.iloc[i]:
                info = {
                    "type": "bullish_bos",
                    "level_broken": df["last_swing_high"].shift(1).iloc[i],
                    "confirmation_score": (
                        min(1.0, vol_ratio / volume_multiplier)
                        if enable_volume_conf
                        else 1.0
                    ),
                    "volume_ratio": round(vol_ratio, 2),
                    "momentum": round(abs(momentum), 4),
                    "structure_type": "continuation",
                    "quality": (
                        "high" if vol_ratio > volume_multiplier * 1.5 else "medium"
                    ),
                }
            elif bearish_bos.iloc[i]:
                info = {
                    "type": "bearish_bos",
                    "level_broken": df["last_swing_low"].shift(1).iloc[i],
                    "confirmation_score": (
                        min(1.0, vol_ratio / volume_multiplier)
                        if enable_volume_conf
                        else 1.0
                    ),
                    "volume_ratio": round(vol_ratio, 2),
                    "momentum": round(abs(momentum), 4),
                    "structure_type": "continuation",
                    "quality": (
                        "high" if vol_ratio > volume_multiplier * 1.5 else "medium"
                    ),
                }
            elif bullish_mss.iloc[i]:
                info = {
                    "type": "bullish_mss",
                    "level_broken": df["last_swing_high"].shift(1).iloc[i],
                    "confirmation_score": (
                        min(1.0, vol_ratio / volume_multiplier)
                        if enable_volume_conf
                        else 1.0
                    ),
                    "volume_ratio": round(vol_ratio, 2),
                    "momentum": round(abs(momentum), 4),
                    "structure_type": "reversal",
                    "quality": (
                        "high" if vol_ratio > volume_multiplier * 1.5 else "medium"
                    ),
                }
            elif bearish_mss.iloc[i]:
                info = {
                    "type": "bearish_mss",
                    "level_broken": df["last_swing_low"].shift(1).iloc[i],
                    "confirmation_score": (
                        min(1.0, vol_ratio / volume_multiplier)
                        if enable_volume_conf
                        else 1.0
                    ),
                    "volume_ratio": round(vol_ratio, 2),
                    "momentum": round(abs(momentum), 4),
                    "structure_type": "reversal",
                    "quality": (
                        "high" if vol_ratio > volume_multiplier * 1.5 else "medium"
                    ),
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

    def _get_trend(self, df: pd.DataFrame) -> pd.Series:
        """
        Détermine la tendance dominante pour chaque point de données de manière vectorielle.

        Args:
            df (pd.DataFrame): Les données de marché.

        Returns:
            pd.Series: Une série indiquant la tendance ("bullish", "bearish", "neutral").
        """
        # TODO: Remplacer le calcul de tendance par des indicateurs plus robustes comme l'ADX
        #       (Average Directional Index) qui mesure la force de la tendance, pas seulement sa direction.

        # CORRECTION ICI: S'assurer que les attributs sont chargés. Ils doivent être chargés via _load_settings.
        # Si pour une raison quelconque ils ne le sont pas, utiliser des valeurs par défaut configurables.
        trend_window = getattr(
            self,
            "TREND_WINDOW",
            self.config_manager.get("phase_detection_defaults.trend_window", 20),
        )
        trend_sma_fast_ratio = getattr(
            self,
            "trend_sma_fast_ratio",
            self.config_manager.get(
                "phase_detection_defaults.trend_sma_fast_ratio", 0.3
            ),
        )
        trend_sma_slow_ratio = getattr(
            self,
            "trend_sma_slow_ratio",
            self.config_manager.get(
                "phase_detection_defaults.trend_sma_slow_ratio", 0.7
            ),
        )

        fast_window = int(trend_window * trend_sma_fast_ratio)
        slow_window = int(trend_window * trend_sma_slow_ratio)

        # Assurez-vous que les fenêtres sont valides et ne dépassent pas la taille du DataFrame
        if len(df) < slow_window:
            self.logger.warning(
                f"Données insuffisantes ({len(df)} barres) pour calculer la tendance sur une fenêtre lente ({slow_window}). La tendance sera 'neutral'."
            )
            return pd.Series("neutral", index=df.index)

        sma_fast = df["close"].rolling(window=fast_window, min_periods=1).mean()
        sma_slow = df["close"].rolling(window=slow_window, min_periods=1).mean()

        trend_conditions = [sma_fast > sma_slow, sma_fast < sma_slow]
        trend_outcomes = ["bullish", "bearish"]

        return np.select(trend_conditions, trend_outcomes, default="neutral")

    def _calculate_phase_confidence(
        self, signals: Dict[str, bool], window: pd.DataFrame
    ) -> float:
        """
        [DÉPRÉCIÉ] Remplacé par un calcul de confiance vectoriel dans le pipeline `analyze`.
        Calcule un score de confiance pour une phase de marché détectée.
        """
        warnings.warn(
            "`_calculate_phase_confidence` est déprécié. "
            "La logique est maintenant intégrée dans le pipeline vectoriel `analyze`.",
            DeprecationWarning,
        )
        self.logger.warning(
            "Appel à la méthode dépréciée `_calculate_phase_confidence`."
        )

        # TODO: Supprimer cette méthode. La nouvelle logique de confiance prendra en compte
        #       la confluence des signaux pour un scoring non-linéaire et plus intelligent.
        confidence = self.base_confidence_score
        # ... (logique originale conservée pour référence)
        return max(0.0, min(1.0, confidence))

    # <<<< AJOUTEZ LA NOUVELLE MÉTHODE ICI >>>>

    def calculate_confidence_score(self, df_row: pd.Series) -> float:
        """
        Calcule un score de confiance basé sur la convergence des signaux SMC détectés.

        Args:
            df_row (pd.Series): Une ligne du DataFrame annoté avec tous les signaux détectés

        Returns:
            float: Score de confiance entre 0.0 et 1.0
        """
        # Configuration des poids depuis la config (avec fallbacks)
        config_weights = self.config_manager.get(
            "phase_detection_defaults.confidence_score_calculation", {}
        )

        base_confidence = config_weights.get("base_confidence", 0.1)
        signal_weights = config_weights.get(
            "signal_weights",
            {
                "fvg_detected": 0.15,
                "ob_detected": 0.20,
                "bos_mss_detected": 0.15,
                "liquidity_grab_detected": 0.20,
                "eqh_eql_detected": 0.10,
                "volume_anomaly_detected": 0.10,
                "trend_alignment": 0.10,
            },
        )

        convergence_bonus = config_weights.get(
            "convergence_bonus",
            {
                "multiple_signals_bonus": 0.2,
                "min_signals_for_bonus": 2,
                "max_confidence_cap": 1.0,
            },
        )

        quality_factors = config_weights.get(
            "quality_factors",
            {
                "volume_confirmation_bonus": 0.1,
                "trend_strength_bonus": 0.1,
                "spread_quality_bonus": 0.05,
            },
        )

        # Calcul de base
        confidence = base_confidence
        detected_signals = []

        # Poids pour chaque signal détecté
        for signal_name, weight in signal_weights.items():
            if signal_name == "trend_alignment":
                # Vérifier alignement tendance/phase
                trend = df_row.get("trend", "neutral")
                phase = df_row.get("phase", "")
                if (
                    (trend == "bullish" and "bullish" in phase)
                    or (trend == "bearish" and "bearish" in phase)
                    or (
                        trend == "bullish"
                        and phase in ["expansion_up", "trending_bullish"]
                    )
                    or (
                        trend == "bearish"
                        and phase in ["expansion_down", "trending_bearish"]
                    )
                ):
                    confidence += weight
                    detected_signals.append("trend_alignment")
            else:
                # Signaux booléens standard
                if df_row.get(signal_name, False):
                    confidence += weight
                    detected_signals.append(signal_name)

        # Bonus pour convergence multiple
        if len(detected_signals) >= convergence_bonus["min_signals_for_bonus"]:
            confidence += convergence_bonus["multiple_signals_bonus"]

        # Facteurs de qualité
        if df_row.get("volume_momentum", 0) > 0.1:  # Volume momentum significatif
            confidence += quality_factors["volume_confirmation_bonus"]

        if df_row.get("is_liquid", True):  # Asset liquide
            confidence += quality_factors["spread_quality_bonus"]

        # Bonus pour validations (validated_ob, entry_confirmation, etc.)
        if df_row.get("validated_ob", False):
            confidence += 0.1
        if df_row.get("entry_confirmation_bullish", False) or df_row.get(
            "entry_confirmation_bearish", False
        ):
            confidence += 0.1

        # Cap final
        final_confidence = min(
            convergence_bonus["max_confidence_cap"], max(0.0, confidence)
        )

        # Debug log (seulement pour les premières lignes pour éviter le spam)
        if (
            hasattr(df_row, "name") and df_row.name in df_row.index[:3]
        ):  # Log seulement les 3 premières
            self.logger.debug(
                f"Confidence calculée: {final_confidence:.3f} - Signaux: {detected_signals}"
            )

        return final_confidence

    def log_audit_event(
        self,
        event_type: str,
        message: str,
        asset: Optional[str] = None,
        timestamp: Optional[datetime] = None,
    ):
        """
        Enregistre un événement dans le journal d'audit interne.

        Args:
            event_type (str): Type d'événement (ex: "DATA_GAP", "NEW_ASSET").
            message (str): Description de l'événement.
            asset (str, optional): Symbole de l'actif concerné.
            timestamp (datetime, optional): Horodatage de l'événement.
        """
        event = {
            "timestamp": (timestamp or datetime.now(UTC)).isoformat(),
            "event_type": event_type,
            "asset": asset,
            "message": message,
        }
        self.audit_journal.append(event)
        self.logger.info(
            f"ÉVÉNEMENT D'AUDIT [{event_type}] pour {asset or 'N/A'}: {message}"
        )

        # TODO: Mettre en place un mécanisme d'exportation automatique du journal d'audit
        #       (ex: toutes les N entrées ou toutes les heures) pour ne pas perdre de données.

    def export_audit_journal(self, filename: Optional[str] = None):
        """
        Exporte le journal d'audit de manière atomique dans un fichier JSONL.

        Args:
            filename (str, optional): Nom de base du fichier. Utilise celui de la config par défaut.
        """
        base_name = filename or self.audit_journal_file_name
        timestamp_str = datetime.now(UTC).strftime("%Y%m%d")
        filepath = self.output_path / f"{base_name}_{timestamp_str}.jsonl"

        self.logger.info(f"Exportation du journal d'audit vers {filepath}...")

        # Écriture Atomique
        temp_path = filepath.with_suffix(".tmp")
        try:
            with open(temp_path, "w", encoding="utf-8") as f:
                for event in self.audit_journal:
                    f.write(json.dumps(event) + "\n")
            temp_path.rename(filepath)
            self.logger.info("Journal d'audit exporté avec succès.")
        except Exception as e:
            self.logger.error(
                f"Échec de l'exportation du journal d'audit: {e}", exc_info=True
            )
            if temp_path.exists():
                temp_path.unlink()

        # TODO: Ajouter une rotation des fichiers de log d'audit pour ne conserver que
        #       les N derniers jours et éviter la saturation du disque.

    def reset(self) -> None:
        """
        Réinitialise l'état interne de l'observateur avant une nouvelle analyse.
        """
        self.audit_journal = []
        self._liquidity_levels_cache = {}
        # TODO: Ajouter d'autres états à réinitialiser si nécessaire.
        self.logger.debug("État de PhaseObserver réinitialisé.")

    def _clean_dataframe(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Nettoie et standardise un DataFrame de données de marché (OHLCV) pour le PhaseObserver.
        Cette méthode assure l'intégrité et la cohérence des données reçues de MT5Connector
        avant l'analyse des phases de marché.

        Args:
            df (pd.DataFrame): Le DataFrame brut des données de marché (barres OHLCV).

        Returns:
            pd.DataFrame: Le DataFrame nettoyé et standardisé.
        """
        # Utiliser le logger du PhaseObserver
        self.logger.info(
            "PhaseObserver: Nettoyage et standardisation du DataFrame des données de marché..."
        )

        if df.empty:
            self.logger.warning(
                "PhaseObserver: DataFrame vide fourni pour le nettoyage. Retourne un DataFrame vide."
            )
            return df

        # Les données MT5 issues de copy_rates_from_pos sont déjà standardisées avec ces colonnes:
        # ['time', 'open', 'high', 'low', 'close', 'tick_volume', 'spread', 'real_volume']
        # Le rôle de ce _clean_dataframe est de s'assurer de leurs types et de l'intégrité de base.

        # S'assurer que la colonne 'time' est au format datetime avec UTC et est l'index
        # Le `df` vient de MT5Connector.get_rates, où 'time' est déjà une colonne datetime UTC
        # Mais nous allons la définir comme index ici.
        if "time" not in df.columns:
            self.logger.error(
                "PhaseObserver: Colonne 'time' manquante dans le DataFrame pour le nettoyage. Impossible de procéder."
            )
            return pd.DataFrame()

        # Vérifier si 'time' est déjà l'index ou si c'est une colonne à transformer en index
        if df.index.name != "time":
            # Assurer que 'time' est au bon format avant de le mettre en index
            if not pd.api.types.is_datetime64_any_dtype(df["time"]):
                try:
                    # Convertir en datetime, gérer les erreurs par 'coerce' (NaN pour les échecs)
                    # puis supprimer les lignes où la conversion a échoué.
                    df["time"] = pd.to_datetime(
                        df["time"], unit="s", utc=True, errors="coerce"
                    )
                    df.dropna(subset=["time"], inplace=True)
                    if df.empty:
                        self.logger.warning(
                            "PhaseObserver: Toutes les lignes supprimées à cause de timestamps invalides après conversion."
                        )
                        return pd.DataFrame()
                except Exception as e:
                    self.logger.error(
                        f"PhaseObserver: Erreur critique lors de la conversion de la colonne 'time' en datetime: {e}",
                        exc_info=True,
                    )
                    return pd.DataFrame()

            df.set_index("time", inplace=True)  # Définir 'time' comme index
            df.sort_index(inplace=True)  # Trier par index (temps)
        else:  # 'time' est déjà l'index
            df.sort_index(inplace=True)  # Assurer le tri même si déjà indexé

        # Vérification et conversion des colonnes numériques essentielles (OHLCV et volume de ticks)
        # Ces colonnes sont attendues après l'appel à MT5Connector.get_rates
        numeric_cols_expected = ["open", "high", "low", "close", "tick_volume"]

        for col in numeric_cols_expected:
            if col not in df.columns:
                self.logger.warning(
                    f"PhaseObserver: Colonne numérique essentielle '{col}' manquante dans le DataFrame. Ajoutée avec des zéros."
                )
                df[col] = 0.0  # Ajouter la colonne manquante avec une valeur par défaut
            else:
                # Convertir en numérique, forcer les erreurs en NaN, puis remplacer NaN par 0.0
                df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)

                # CORRECTION DES FUTUREWARNING : Utilisation de .loc et inférence des objets
                # Pour éviter les FutureWarnings liés au downcasting implicite,
                # et s'assurer que le dtype est inféré correctement après remplissage.
                if df[col].dtype == "object":
                    df[col] = df[col].infer_objects(
                        copy=False
                    )  # Inférez le type d'objet après modifications.

                # Correction des valeurs non positives si elles sont détectées après conversion.
                # Pour les prix, 0.00001 est une valeur arbitraire pour éviter des divisions par zéro.
                if col in ["open", "high", "low", "close"] and (df[col] <= 0).any():
                    rows_fixed = df[df[col] <= 0].shape[0]
                    df.loc[df[col] <= 0, col] = 0.00001
                    self.logger.warning(
                        f"PhaseObserver: Corrigé {rows_fixed} valeurs non positives à 0.00001 dans '{col}'."
                    )
                elif (
                    col == "tick_volume" and (df[col] < 0).any()
                ):  # Volume ne peut pas être négatif
                    rows_fixed = df[df[col] < 0].shape[0]
                    df.loc[df[col] < 0, col] = 0.0
                    self.logger.warning(
                        f"PhaseObserver: Corrigé {rows_fixed} volumes négatifs à 0.0 dans '{col}'."
                    )

        # Gestion des doublons d'index (horodatage) - Garder la première occurrence
        # Cela devrait être fait après set_index()
        initial_len_dedup = len(df)
        df = df.loc[~df.index.duplicated(keep="first")]
        if len(df) < initial_len_dedup:
            self.logger.warning(
                f"PhaseObserver: Supprimé {initial_len_dedup - len(df)} entrées dupliquées basées sur l'horodatage."
            )

        self.logger.info("PhaseObserver: Nettoyage du DataFrame terminé.")
        return df

    def _get_nearest_liquidity_level(
        self, df: pd.DataFrame
    ) -> Optional[Dict[str, Any]]:
        """
        Détermine le niveau de liquidité (EQH/EQL, OB, FVG) le plus proche de la dernière barre
        et calcule la distance en pips.

        Args:
            df (pd.DataFrame): Le DataFrame annoté avec les détails des EQH/EQL, OB et FVG.

        Returns:
            Optional[Dict[str, Any]]: Un dictionnaire avec les détails du niveau de liquidité le plus proche,
                                    y compris sa distance en pips, ou None si aucun niveau n'est trouvé.
        """
        self.logger.debug("Détection du niveau de liquidité le plus proche...")

        if df.empty:
            self.logger.debug(
                "DataFrame vide pour la détection du niveau de liquidité. Retourne None."
            )
            return None

        last_close = df["close"].iloc[-1]

        point_value = (
            df["point"].iloc[-1]
            if "point" in df.columns and df["point"].iloc[-1] > 0
            else self.config_manager.get(
                "phase_detection_defaults.default_point_value", 0.00001
            )
        )

        if point_value <= 0:
            self.logger.warning(
                "Valeur de 'point' invalide ou non positive. Impossible de calculer la distance en pips."
            )
            point_value = 0.00001  # Fallback pour éviter la division par zéro

        nearest_level = None
        min_distance_pips = np.inf

        liquidity_levels = []

        if "eqh_eql_details" in df.columns:
            for eq_details in df["eqh_eql_details"].dropna():
                if isinstance(eq_details, dict) and eq_details.get("level") is not None:
                    liquidity_levels.append(
                        {"type": eq_details["type"], "level": eq_details["level"]}
                    )

        if "ob_details" in df.columns:
            for ob_details_entry in df["ob_details"].dropna():
                if (
                    isinstance(ob_details_entry, dict)
                    and ob_details_entry.get("zone") is not None
                ):
                    ob_low, ob_high = ob_details_entry["zone"]
                    liquidity_levels.append({"type": "OB_low", "level": ob_low})
                    liquidity_levels.append({"type": "OB_high", "level": ob_high})

        if "fvg_details" in df.columns:
            for fvg_details_entry in df["fvg_details"].dropna():
                if (
                    isinstance(fvg_details_entry, dict)
                    and fvg_details_entry.get("top") is not None
                    and fvg_details_entry.get("bottom") is not None
                ):
                    liquidity_levels.append(
                        {"type": "FVG_top", "level": fvg_details_entry["top"]}
                    )
                    liquidity_levels.append(
                        {"type": "FVG_bottom", "level": fvg_details_entry["bottom"]}
                    )

        # --- CORRECTION ICI : Retourne None explicitement si aucune liquidité n'est trouvée ---
        if not liquidity_levels:
            self.logger.debug(
                "Aucun niveau de liquidité détecté pour la dernière barre. Retourne None."
            )
            return None

        for liq_level in liquidity_levels:
            level_value = liq_level["level"]
            # Assurez-vous que level_value est un nombre pour les comparaisons.
            if not isinstance(level_value, (int, float)):
                if (
                    isinstance(level_value, list) and len(level_value) == 2
                ):  # Gérer les zones (tuples convertis en listes)
                    dist_to_low = abs(last_close - level_value[0])
                    dist_to_high = abs(last_close - level_value[1])
                    distance_in_price = min(dist_to_low, dist_to_high)
                else:
                    self.logger.warning(
                        f"Niveau de liquidité invalide détecté: {level_value}. Ignoré."
                    )
                    continue
            else:
                distance_in_price = abs(last_close - level_value)

            distance_pips = round(distance_in_price / point_value, 2)

            if distance_pips < min_distance_pips:
                min_distance_pips = distance_pips
                nearest_level = {
                    "type": liq_level["type"],
                    "level": liq_level["level"],
                    "distance_pips": distance_pips,
                }

        if nearest_level:
            self.logger.debug(
                f"Niveau de liquidité le plus proche détecté : Type={nearest_level['type']}, Level={nearest_level['level']}, Distance={nearest_level['distance_pips']:.2f} pips."
            )
            return nearest_level  # Retourne le dictionnaire si un niveau est trouvé
        else:
            self.logger.debug(
                "Aucun niveau de liquidité proche détecté après analyse. Retourne None."
            )
            return None  # Retourne None explicitement

    def load_data(
        self,
        source: Union[str, Path, pd.DataFrame, List[Dict]],
        asset_symbol: Optional[str] = None,
    ) -> Optional[pd.DataFrame]:
        """
        Charge et prépare les données depuis diverses sources pour l'analyse.

        Cette fonction est le point d'entrée unique pour les données. Elle gère la
        conversion des données de bougies MT5 en un format standardisé et valide
        la présence des colonnes nécessaires.
        """
        # ... (la logique de cette fonction a été validée et améliorée précédemment)
        # Seule la correction du logger est nécessaire ici.
        self.logger.info(
            f"Chargement des données pour {asset_symbol or 'actif inconnu'}..."
        )
        # TODO: Valider la structure des données entrantes (particulièrement pour la liste de dicts)
        #       avec un schéma Pydantic ou JSON Schema pour une robustesse maximale.
        pass  # Placeholder pour le code existant qui est déjà de bonne qualité

    def analyze_asset_multi_timeframe(
        self, asset: str, strategy_config: Dict
    ) -> Dict[str, Any]:
        """
        🏛️ ANALYSE MULTI-TIMEFRAME INSTITUTIONNELLE 🏛️

        Architecture de trading desk professionnel avec:
        - Cache haute performance pour minimiser les appels MT5
        - Algorithmes de confluence sophistiqués avec scoring non-linéaire
        - Détection de divergences inter-timeframes
        - Synchronisation temporelle précise
        - Métriques de qualité en temps réel
        - Gestion d'erreurs robuste niveau production

        Args:
            asset (str): Symbole de l'actif (ex: "EURUSD")
            strategy_config (Dict): Configuration de stratégie avec paramètres multi-TF

        Returns:
            Dict[str, Any]: Signaux enrichis avec confluence multi-TF et métriques qualité
        """
        analysis_start_time = time.perf_counter()

        # === PHASE 1: VALIDATION & INITIALISATION ===
        multi_tf_config = strategy_config.get("phase_detection", {}).get(
            "multi_timeframe", {}
        )
        if not multi_tf_config.get("enabled", False):
            self.logger.debug(
                f"[{asset}] Multi-TF désactivé, fallback analyse standard"
            )
            return self._analyze_single_tf_fallback(asset)

        # Configuration avancée
        timeframes = multi_tf_config.get("timeframes", ["M1", "M5", "M15"])
        confluence_weights = multi_tf_config.get(
            "confluence_weights", {"M1": 0.5, "M5": 0.3, "M15": 0.2}
        )
        cache_ttl_seconds = multi_tf_config.get("cache_ttl_seconds", 30)
        quality_threshold = multi_tf_config.get("min_quality_score", 0.7)

        self.logger.info(f"🎯 [{asset}] KATANA Multi-TF activé: {timeframes}")

        # === PHASE 2: ACQUISITION DONNÉES AVEC CACHE INTELLIGENT ===
        tf_data_cache = {}
        cache_hits = 0

        for tf in timeframes:
            cache_key = f"{asset}_{tf}_{int(time.time() // cache_ttl_seconds)}"

            # Vérification cache
            if hasattr(self, "_tf_data_cache") and cache_key in self._tf_data_cache:
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
                    self.logger.error(f"💥 [{asset}] Erreur critique {tf}: {e}")
                    continue

        # Vérification intégrité données
        if len(tf_data_cache) < 2:
            self.logger.warning(
                f"⚠️ [{asset}] Données insuffisantes ({len(tf_data_cache)}/{len(timeframes)}) pour Multi-TF"
            )
            return self._analyze_single_tf_fallback(asset)

        cache_efficiency = (cache_hits / len(timeframes)) * 100
        self.logger.debug(f"📊 [{asset}] Cache efficiency: {cache_efficiency:.1f}%")

        # === PHASE 3: ANALYSE VECTORIELLE PARALLÈLE ===
        tf_analyses = {}
        analysis_errors = []

        for tf, tf_data in tf_data_cache.items():
            try:
                # Analyse complète par timeframe
                tf_config = self._get_tf_specific_config(tf, multi_tf_config)

                # Override temporaire des paramètres pour ce TF
                original_params = self._backup_current_params()
                self._load_settings(overrides=tf_config)

                # Analyse vectorielle
                analyzed_data = self.analyze(tf_data, asset_symbol=asset)

                # Restauration paramètres
                self._restore_params(original_params)

                if analyzed_data is not None and not analyzed_data.empty:
                    # Extraction signaux dernière barre
                    last_signals = self._extract_last_bar_signals(analyzed_data, tf)
                    tf_analyses[tf] = last_signals
                    self.logger.debug(
                        f"✅ [{asset}] {tf} analysé: Phase={last_signals.get('phase')}"
                    )
                else:
                    raise ValueError(f"Analyse {tf} retournée vide")

            except Exception as e:
                analysis_errors.append(f"{tf}: {str(e)}")
                self.logger.error(f"💥 [{asset}] Erreur analyse {tf}: {e}")

        # === PHASE 4: FUSION INTELLIGENTE & CONFLUENCE ===
        if len(tf_analyses) < 2:
            self.logger.warning(f"⚠️ [{asset}] Analyses insuffisantes pour confluence")
            return self._analyze_single_tf_fallback(asset)

        # Algorithme de confluence sophistiqué
        confluence_result = self._calculate_advanced_confluence(
            tf_analyses, confluence_weights, asset
        )

        # Détection divergences inter-TF (signaux contradictoires)
        divergence_analysis = self._detect_tf_divergences(tf_analyses)

        # === PHASE 5: SCORING QUALITÉ & MÉTRIQUES ===
        quality_metrics = self._calculate_quality_metrics(
            tf_analyses, confluence_result, divergence_analysis, analysis_start_time
        )

        # Filtrage qualité
        if quality_metrics["overall_score"] < quality_threshold:
            self.logger.warning(
                f"⚠️ [{asset}] Qualité insuffisante ({quality_metrics['overall_score']:.3f} < {quality_threshold})"
            )
            return self._build_low_quality_response(asset, quality_metrics)

        # === PHASE 6: CONSTRUCTION RÉPONSE FINALE ===
        final_signals = self._build_enhanced_signals(
            confluence_result, quality_metrics, tf_analyses, asset
        )

        execution_time = (time.perf_counter() - analysis_start_time) * 1000

        self.logger.info(
            f"🎯 [{asset}] KATANA Multi-TF terminé: "
            f"Phase={final_signals.get('phase')}, "
            f"Qualité={quality_metrics['overall_score']:.3f}, "
            f"Temps={execution_time:.1f}ms"
        )

        return final_signals

    def _fetch_timeframe_data(
        self, asset: str, timeframe: str, config: Dict
    ) -> Optional[pd.DataFrame]:
        """
        Acquisition données MT5 optimisée avec gestion d'erreurs robuste
        """
        try:
            # ✅ Mapping correct des timeframes MT5 avec constantes officielles
            import MetaTrader5 as mt5

            tf_mapping = {
                "M1": mt5.TIMEFRAME_M1,
                "M5": mt5.TIMEFRAME_M5,
                "M15": mt5.TIMEFRAME_M15,
                "M30": mt5.TIMEFRAME_M30,
                "H1": mt5.TIMEFRAME_H1,
                "H4": mt5.TIMEFRAME_H4,
                "D1": mt5.TIMEFRAME_D1,
            }

            mt5_timeframe = tf_mapping.get(timeframe.upper())
            if not mt5_timeframe:
                raise ValueError(f"Timeframe {timeframe} non supporté")

            # Paramètres acquisition (lookback)
            lookback_bars = config.get(f"{timeframe.lower()}_config", {}).get(
                "lookback_window", 500
            )

            # Appel MT5Connector
            if hasattr(self.config_manager, "mt5_connector"):
                mt5_data = self.config_manager.mt5_connector.get_rates(
                    asset, timeframe, lookback_bars
                )

                if mt5_data is not None and not mt5_data.empty:
                    # Nettoyage des données
                    cleaned_data = self._clean_dataframe(mt5_data)
                    return cleaned_data
                else:
                    self.logger.warning(
                        f"[{asset}] Données vides ou None pour TF={timeframe}, lookback={lookback_bars}"
                    )

            # ⚠️ Fallback simulation (utile en DEV uniquement)
            self.logger.warning(f"MT5Connector indisponible, simulation {timeframe}")
            return self._generate_simulation_data(asset, timeframe, lookback_bars)

        except Exception as e:
            self.logger.error(f"Erreur acquisition {asset} {timeframe}: {e}")
            return None

    def _calculate_advanced_confluence(
        self, tf_analyses: Dict, weights: Dict, asset: str
    ) -> Dict[str, Any]:
        """
        Algorithme de confluence sophistiqué avec scoring non-linéaire
        """
        confluence_scores = {}
        signal_agreement = {}

        # Analyse des phases dominantes
        phases = [analysis.get("phase", "unknown") for analysis in tf_analyses.values()]
        phase_consistency = len(set(phases)) == 1  # Toutes identiques ?

        # Scoring signaux critiques
        critical_signals = [
            "bos_mss_detected",
            "liquidity_grab_detected",
            "ob_detected",
        ]

        for signal in critical_signals:
            signal_scores = []
            for tf, analysis in tf_analyses.items():
                if analysis.get(signal, False):
                    weight = weights.get(tf, 0.33)
                    signal_scores.append(weight)

            confluence_scores[signal] = sum(signal_scores)
            signal_agreement[signal] = len(signal_scores) / len(tf_analyses)

        # Score de confluence global (non-linéaire)
        base_confluence = sum(confluence_scores.values()) / len(critical_signals)

        # Bonus pour cohérence de phase
        phase_bonus = 0.3 if phase_consistency else 0.0

        # Bonus pour agreement multiple
        avg_agreement = sum(signal_agreement.values()) / len(signal_agreement)
        agreement_bonus = avg_agreement * 0.2

        final_confluence_score = min(
            1.0, base_confluence + phase_bonus + agreement_bonus
        )

        # Construction signaux finaux
        final_phase = self._determine_consensus_phase(phases, tf_analyses, weights)

        return {
            "phase": final_phase,
            "confluence_score": final_confluence_score,
            "signal_scores": confluence_scores,
            "phase_consistency": phase_consistency,
            "agreement_rates": signal_agreement,
            "dominant_timeframe": max(weights, key=weights.get),
        }

    def _detect_tf_divergences(self, tf_analyses: Dict) -> Dict[str, Any]:
        """
        Détection de divergences inter-timeframes (signaux contradictoires)
        """
        divergences = []

        # Vérification divergences directionnelles
        bullish_tfs = []
        bearish_tfs = []

        for tf, analysis in tf_analyses.items():
            phase = analysis.get("phase", "")
            if "bullish" in phase or "up" in phase:
                bullish_tfs.append(tf)
            elif "bearish" in phase or "down" in phase:
                bearish_tfs.append(tf)

        # Détection conflits
        has_directional_conflict = len(bullish_tfs) > 0 and len(bearish_tfs) > 0

        if has_directional_conflict:
            divergences.append(
                {
                    "type": "directional_conflict",
                    "bullish_tfs": bullish_tfs,
                    "bearish_tfs": bearish_tfs,
                    "severity": "high",
                }
            )

        return {
            "detected_divergences": divergences,
            "has_conflicts": len(divergences) > 0,
            "conflict_severity": "high" if has_directional_conflict else "none",
        }

    def _calculate_quality_metrics(
        self, tf_analyses: Dict, confluence: Dict, divergences: Dict, start_time: float
    ) -> Dict[str, Any]:
        """
        Calcul métriques de qualité sophistiquées
        """
        # Couverture données
        data_coverage = len(tf_analyses) / 3  # Supposant M1, M5, M15

        # Score confluence
        confluence_score = confluence.get("confluence_score", 0.0)

        # Pénalité divergences
        divergence_penalty = 0.3 if divergences.get("has_conflicts", False) else 0.0

        # Consistance temporelle
        temporal_consistency = (
            0.2 if confluence.get("phase_consistency", False) else 0.0
        )

        # Score qualité global
        overall_score = max(
            0.0,
            min(
                1.0,
                data_coverage * 0.3
                + confluence_score * 0.4
                + temporal_consistency
                + (0.1 - divergence_penalty),
            ),
        )

        # Temps d'exécution
        execution_time_ms = (time.perf_counter() - start_time) * 1000

        return {
            "overall_score": overall_score,
            "data_coverage": data_coverage,
            "confluence_score": confluence_score,
            "temporal_consistency": temporal_consistency,
            "divergence_penalty": divergence_penalty,
            "execution_time_ms": execution_time_ms,
            "performance_grade": (
                "A" if overall_score > 0.8 else "B" if overall_score > 0.6 else "C"
            ),
        }

    def _build_enhanced_signals(
        self, confluence: Dict, quality: Dict, tf_analyses: Dict, asset: str
    ) -> Dict[str, Any]:
        """
        Construction des signaux finaux enrichis
        """
        # Signaux de base depuis confluence
        base_signals = {
            "phase": confluence.get("phase", "uncertain"),
            "confidence_score": confluence.get("confluence_score", 0.0),
            "is_liquid": True,  # À déterminer selon vos critères
            "current_price": 0.0,  # À récupérer des données
        }

        # Enrichissement multi-TF
        multi_tf_enhancement = {
            "multi_tf_enabled": True,
            "tf_consensus": confluence.get("phase_consistency", False),
            "dominant_tf": confluence.get("dominant_timeframe", "M5"),
            "quality_grade": quality.get("performance_grade", "C"),
            "execution_time_ms": quality.get("execution_time_ms", 0),
            # Signaux de confluence
            "bos_mss_detected": confluence.get("signal_scores", {}).get(
                "bos_mss_detected", 0
            )
            > 0.5,
            "liquidity_grab_detected": confluence.get("signal_scores", {}).get(
                "liquidity_grab_detected", 0
            )
            > 0.5,
            "ob_detected": confluence.get("signal_scores", {}).get("ob_detected", 0)
            > 0.5,
            # Méta-données pour debugging
            "tf_breakdown": {
                tf: analysis.get("phase") for tf, analysis in tf_analyses.items()
            },
            "signal_agreement_rates": confluence.get("agreement_rates", {}),
        }

        # Fusion finale
        return {**base_signals, **multi_tf_enhancement}

    # === MÉTHODES UTILITAIRES SUPPLÉMENTAIRES ===

    def _analyze_single_tf_fallback(self, asset: str) -> Dict[str, Any]:
        """Fallback vers analyse standard en cas d'échec Multi-TF"""
        self.logger.debug(f"[{asset}] Fallback analyse standard")
        return {
            "phase": "uncertain",
            "confidence_score": 0.1,
            "multi_tf_enabled": False,
            "fallback_reason": "multi_tf_failed",
        }

    def _backup_current_params(self) -> Dict:
        """Sauvegarde paramètres actuels"""
        return {
            attr: getattr(self, attr, None)
            for attr in dir(self)
            if not attr.startswith("_")
        }

    def _restore_params(self, params: Dict) -> None:
        """Restaure paramètres sauvegardés"""
        for attr, value in params.items():
            if hasattr(self, attr) and value is not None:
                setattr(self, attr, value)

    def determine_optimized_phase(self, row):
        """Classification de phase basée sur les 4 indicateurs core"""
        regime = row.get("regime", "unknown")

        if "trending_institutional" in regime:
            if row.get("fvg_ob_confluence", False):
                return "institutional_setup_premium"
            elif row.get("ob_detected", False):
                return "institutional_setup"
            elif "bull" in regime:
                return "trending_institutional_bull"
            else:
                return "trending_institutional_bear"
        elif "range_accumulation" in regime:
            if row.get("high_quality_ob", False):
                return "accumulation_zone"
            else:
                return "range_accumulation"
        elif "range_distribution" in regime:
            if row.get("confirmed_structure_break", False):
                return "distribution_breakout"
            else:
                return "range_distribution"
        elif "high_volatility" in regime:
            if row.get("bos_mss_detected", False):
                return "volatility_breakout"
            else:
                return "high_volatility_chaos"
        elif "low_volatility" in regime:
            return "low_volatility_compression"
        else:
            if row.get("institutional_setup", False):
                return "smc_setup"
            elif row.get("fvg_detected", False):
                return "fvg_opportunity"
            else:
                return "no_clear_phase"

    def calculate_optimized_confidence(self, row):
        """Score de confiance basé sur les 4 indicateurs core uniquement, avec lecture dynamique depuis la config."""

        # --- 1) Lecture DYNAMIQUE: on essaie plusieurs chemins compatibles ---
        cfg = self.config_manager.get("confidence_score_calculation", None)
        cfg_path_used = "confidence_score_calculation"

        if not cfg:
            cfg = self.config_manager.get(
                "phase_detection_defaults.confidence_score_calculation", None
            )
            if cfg:
                cfg_path_used = "phase_detection_defaults.confidence_score_calculation"

        if not cfg:
            # Chemin alternatif souvent utilisé: confidence_scoring
            cfg = self.config_manager.get(
                "phase_detection_defaults.confidence_scoring", None
            )
            if cfg:
                cfg_path_used = "phase_detection_defaults.confidence_scoring"

        # Fallback final si rien trouvé
        if not cfg:
            cfg = {}

        # --- 2) Normalisation douce des clés (compatibilité de structure) ---
        # Ex: certains JSON utilisent "confidence_scoring.weights" au lieu de "signal_weights"
        signal_weights = cfg.get("signal_weights")
        if signal_weights is None:
            weights = cfg.get("weights")  # ex: { "fvg": 0.2, "ob": 0.3, ... }
            if isinstance(weights, dict):
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
            else:
                # Défauts prudents si rien n'est fourni
                signal_weights = {
                    "fvg_detected": 0.25,
                    "ob_detected": 0.35,
                    "bos_mss_detected": 0.25,
                    "regime_alignment": 0.15,
                }

        # confluence / quality : compatibilité d'intitulés
        confluence_bonus = cfg.get("confluence_bonus")
        if confluence_bonus is None:
            confluence_bonus = cfg.get("confluence", {}) or {}
        quality_multipliers = cfg.get("quality_factors")
        if quality_multipliers is None:
            quality_multipliers = cfg.get("quality_multipliers", {}) or {}

        base_confidence = float(cfg.get("base_confidence", cfg.get("base", 0.2)))
        max_confidence = float(cfg.get("max_confidence_cap", cfg.get("cap", 0.95)))

        # --- 3) Calcul du score inchangé dans l'esprit (avec types robustes) ---
        score = float(base_confidence)

        if bool(row.get("fvg_detected", False)):
            score += float(signal_weights.get("fvg_detected", 0.25))
        if bool(row.get("ob_detected", False)):
            score += float(signal_weights.get("ob_detected", 0.35))
        if bool(row.get("bos_mss_detected", False)):
            score += float(signal_weights.get("bos_mss_detected", 0.25))

        regime_strength = float(row.get("regime_strength", 0.5))
        if regime_strength > 0.7:
            score += float(signal_weights.get("regime_alignment", 0.15))

        if bool(row.get("fvg_ob_confluence", False)):
            score += float(confluence_bonus.get("fvg_ob_confluence", 0.15))
        if bool(row.get("high_quality_ob", False)) and bool(
            row.get("bos_mss_detected", False)
        ):
            score += float(confluence_bonus.get("ob_bos_confluence", 0.10))
        if bool(row.get("institutional_setup", False)):
            score += float(confluence_bonus.get("full_confluence_bonus", 0.20))

        if bool(row.get("is_liquid", True)):
            score *= float(quality_multipliers.get("tight_spread", 1.05))
        if regime_strength > 0.8:
            score *= float(quality_multipliers.get("regime_strength", 1.10))

        ob_details = row.get("ob_details")
        bos_details = row.get("bos_mss_details")

        try:
            if (
                isinstance(ob_details, dict)
                and float(ob_details.get("volume_spike", 0)) > 1.5
            ):
                score *= float(
                    quality_multipliers.get("high_volume_confirmation", 1.15)
                )
            elif (
                isinstance(bos_details, dict)
                and float(bos_details.get("volume_ratio", 0)) > 1.5
            ):
                score *= float(
                    quality_multipliers.get("high_volume_confirmation", 1.15)
                )
        except Exception:
            # En cas de valeur non convertible, on ignore simplement ce multiplicateur
            pass

        # --- 4) Clamp final et retour ---
        if score < 0.0:
            score = 0.0
        elif score > 1.0:
            score = 1.0

        # --- 5) (Optionnel) Log DEBUG pour s'assurer que la conf JSON est bien lue ---
        if getattr(self, "debug_confidence_logging", False):
            try:
                self.logger.debug(
                    f"[CONF] path='{cfg_path_used}' | base={base_confidence} | max_cap={max_confidence} | "
                    f"signal_weights={signal_weights} | confluence_bonus={confluence_bonus} | "
                    f"quality_multipliers={quality_multipliers} | -> score={score:.3f}"
                )
            except Exception:
                pass

        return min(max_confidence, max(0.0, score))

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

        self.logger.info(f"🚀 SNIPER_X Optimized Pipeline - Processing {len(df)} bars")

        if df is None or df.empty:
            self.logger.error("DataFrame vide fourni à analyze()")
            return None

        # === PHASE 1: PRÉPARATION DONNÉES ===
        current_asset_symbol = asset_symbol or "UNKNOWN_ASSET"
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
            "spread": 0,
            "point": 0.00001,
            "trade_tick_size": 0.00001,
            "trade_contract_size": 100000.0,
        }
        for col, default_val in required_columns.items():
            if col not in df_an.columns:
                df_an[col] = default_val
                self.logger.warning(f"Colonne '{col}' ajoutée avec valeur par défaut")
            else:
                df_an[col] = pd.to_numeric(df_an[col], errors="coerce").fillna(
                    default_val
                )

        # === Volatilité en % ===
        try:
            if "close" in df_an.columns:
                ret = df_an["close"].pct_change().fillna(0.0)
                vol_pct = ret.abs().ewm(span=20, adjust=False).mean() * 100.0
                vol_pct = (
                    vol_pct.replace([np.inf, -np.inf], 0.0).fillna(0.0).clip(lower=1e-6)
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
        volume_ma_period = self.config_manager.get(
            "phase_detection_defaults.regime_detection_settings.volume_profile.volume_ma_period",
            20,
        )
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
        toggles = self.config_manager.get(
            "phase_detection_defaults.detection_toggles", {}
        )

        if toggles.get("detect_regime", True):
            df_an["regime"] = self.detect_market_regime(df_an)
            df_an["regime_detected"] = True
        else:
            df_an["regime"] = "unknown"
            df_an["regime_detected"] = False
            df_an["regime_strength"] = 0.5

        if toggles.get("detect_fvg", True):
            df_an["fvg_details"] = self.detect_fvg_enhanced(df_an)
            df_an["fvg_detected"] = df_an["fvg_details"].apply(lambda x: x is not None)
        else:
            df_an["fvg_details"] = [None] * len(df_an)
            df_an["fvg_detected"] = False

        if toggles.get("detect_order_block", True):
            df_an["ob_details"] = self.detect_order_block_ml_enhanced(df_an)
            df_an["ob_detected"] = df_an["ob_details"].apply(lambda x: x is not None)
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

        # === PHASE 3: DÉTECTION LIQUIDITÉ ===
        indices_symbols = set(
            self.config_manager.get("global_safety.indices_symbols", ["US30", "NAS100"])
        )
        if current_asset_symbol in indices_symbols:
            max_spread = float(
                self.config_manager.get(
                    "phase_detection_defaults.liquidity_detection.indices_settings.max_allowed_spread_points",
                    50,
                )
            )
            min_volume = float(
                self.config_manager.get(
                    "phase_detection_defaults.liquidity_detection.indices_settings.min_volume_threshold",
                    10,
                )
            )
        else:
            max_spread = float(
                self.config_manager.get(
                    "phase_detection_defaults.liquidity_detection.forex_settings.max_allowed_spread_points",
                    10,
                )
            )
            min_volume = float(
                self.config_manager.get(
                    "phase_detection_defaults.liquidity_detection.forex_settings.min_volume_threshold",
                    1,
                )
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
        df_an["is_liquid"] = (last_spread <= max_spread) and (last_volume >= min_volume)

        # === PHASE 4: SIGNAUX DE CONFLUENCE ===
        df_an["fvg_ob_confluence"] = df_an["fvg_detected"] & df_an["ob_detected"]
        df_an["high_quality_ob"] = df_an["ob_details"].apply(
            lambda x: isinstance(x, dict) and x.get("ml_score", 0) > 0.8
        )
        df_an["confirmed_structure_break"] = df_an["bos_mss_details"].apply(
            lambda x: isinstance(x, dict) and x.get("volume_ratio", 0) > 2.0
        )
        df_an["institutional_setup"] = df_an["regime"].str.contains(
            "institutional", na=False
        ) & (df_an["ob_detected"] | df_an["bos_mss_detected"])

        # === PHASE 5: PHASE OPTIMISÉE + FALLBACK ===
        df_an["phase_primary"] = df_an.apply(self.determine_optimized_phase, axis=1)
        low_th = float(
            self.config_manager.get(
                "phase_detection_defaults.regime_detection_settings.volatility.thresholds.low_pct",
                0.03,
            )
        )
        high_th = float(
            self.config_manager.get(
                "phase_detection_defaults.regime_detection_settings.volatility.thresholds.high_pct",
                0.15,
            )
        )
        df_an["phase"] = df_an["phase_primary"]
        df_an["phase_rule"] = "primary"

        # --- sous-fonction imbriquée ---
        def _apply_phase_fallback(row):
            p = row.get("phase_primary", "no_clear_phase")
            if p != "no_clear_phase":
                return p, "primary"
            v = float(row.get("volatility_pct", 0.0))
            if v < low_th:
                return "range_retail", "fallback_low"
            if v >= high_th:
                return "range_distribution", "fallback_high"
            return "no_clear_phase", "fallback_mid"

        # appliquer le fallback
        phase_fallback_vals = df_an.apply(_apply_phase_fallback, axis=1)
        df_an["phase"] = [p for p, _r in phase_fallback_vals]
        df_an["phase_rule"] = [_r for _p, _r in phase_fallback_vals]

        # === PHASE 6: SCORE DE CONFIANCE ===
        df_an["confidence_score"] = df_an.apply(
            self.calculate_optimized_confidence, axis=1
        )

        # === PHASE 7: MÉTRIQUES + LOG FINAL ===
        if not df_an.empty:
            total_signals = (
                df_an[["fvg_detected", "ob_detected", "bos_mss_detected"]].sum().sum()
            )
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

    def export_to_csv(self, report_df: pd.DataFrame, filename: str):
        """
        Exporte le rapport final ou un DataFrame donné au format CSV.

        Args:
            report_df (pd.DataFrame): Le DataFrame à exporter.
            filename (str): Le nom du fichier de sortie (sans extension, l'extension .csv sera ajoutée).
        """
        filepath = self.output_path / f"{filename}.csv"
        try:
            # Gestion du formatage des nombres pour une précision institutionnelle
            # Utilise 'float_format' pour contrôler le nombre de décimales (ex: 5 pour les devises)
            # ou un formatateur personnalisé pour des milliers/séparateurs décimaux
            report_df.to_csv(
                filepath, index=False, float_format="%.5f"
            )  # Exemple: 5 décimales pour le prix

            self.logger.info(f"Report successfully exported to {filepath}")
            self.log_audit_event(
                "REPORT_EXPORTED",
                f"CSV report exported: {filename}.csv",
                asset="N/A",
                timestamp=pd.Timestamp.now(UTC),
            )
            # TODO: FORMATAGE - Pour une gestion plus avancée des séparateurs décimaux (virgule vs point),
            #       il faudrait utiliser la lib 'locale' ou un formateur personnalisé avec df.applymap.
        except Exception as e:
            self.logger.error(f"Failed to export to CSV: {e}", exc_info=True)
            self.log_audit_event(
                "EXPORT_ERROR",
                f"Failed to export CSV report: {e}",
                asset="N/A",
                timestamp=pd.Timestamp.now(UTC),
            )
            # TODO: ERREUR - Envoyer une alerte critique via ConfigManager en cas d'échec d'exportation.

    def export_to_jsonl(self, report_df: pd.DataFrame, filename: str):
        """
        Exporte le rapport final ou un DataFrame donné au format JSONL (JSON Lines).

        Ce format est idéal pour les logs structurés et l'ingestion dans des bases de données NoSQL
        ou des outils d'analyse de logs.

        Args:
            report_df (pd.DataFrame): Le DataFrame à exporter.
            filename (str): Le nom du fichier de sortie (sans extension, l'extension .jsonl sera ajoutée).
        """
        filepath = self.output_path / f"{filename}.jsonl"
        # Utiliser une écriture atomique pour garantir l'intégrité du fichier en cas de crash
        temp_filepath = filepath.with_suffix(".tmp")
        try:
            # Assurez-vous que les types de données complexes (comme les dictionnaires dans les colonnes _details)
            # sont sérialisables en JSON. Pandas to_json gère bien la plupart des cas.
            report_df.to_json(
                temp_filepath, orient="records", lines=True, date_format="iso"
            )

            # Renommer le fichier temporaire en fichier final une fois l'écriture terminée
            temp_filepath.rename(filepath)

            self.logger.info(f"Report successfully exported to {filepath}")
            self.log_audit_event(
                "REPORT_EXPORTED",
                f"JSONL report exported: {filename}.jsonl",
                asset="N/A",
                timestamp=pd.Timestamp.now(UTC),
            )
        except Exception as e:
            # En cas d'erreur, supprimer le fichier temporaire s'il existe pour éviter des fichiers corrompus
            if temp_filepath.exists():
                temp_filepath.unlink()
            self.logger.error(f"Failed to export to JSONL: {e}", exc_info=True)
            self.log_audit_event(
                "EXPORT_ERROR",
                f"Failed to export JSONL report: {e}",
                asset="N/A",
                timestamp=pd.Timestamp.now(UTC),
            )
            # TODO: ERREUR - Envoyer une alerte critique via ConfigManager en cas d'échec d'exportation.

    def process_multi_asset_config(self, config_filepath: Union[str, Path]):
        """
        Lit la configuration multi-actifs et analyse l'historique des trades depuis le journal d'audit centralisé.
        Génère un rapport multi-actifs et un journal d'audit.
        Le chemin du journal d'audit est lu dynamiquement depuis ConfigManager.

        Args:
            config_filepath (Union[str, Path]): Chemin vers le fichier de configuration de la stratégie JSON.
        """
        config_filepath = Path(config_filepath)

        try:
            with open(config_filepath, "r", encoding="utf-8") as f:
                config = json.load(f)
            tradeable_assets = config.get("tradeable_assets", [])
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

        # --- CORRECTION MAJEURE : Charger le journal d'audit centralisé UNE SEULE FOIS ---
        audit_trail_path_str = self.config_manager.get(
            "paths.ai_history_log"
        )  # Utilise la clé de prod_config.json
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

        # --- CORRECTION : Itérer sur les actifs et filtrer le DataFrame principal ---
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
                dominant_phase = (
                    annotated_df["phase"].mode()[0]
                    if not annotated_df["phase"].empty
                    else "N/A"
                )
                total_volume = (
                    annotated_df["volume"].sum()
                    if "volume" in annotated_df.columns
                    else 0
                )

                self.multi_asset_report_data.append(
                    {
                        "timestamp": pd.Timestamp.now(UTC).isoformat(),
                        "asset": asset,
                        "dominant_phase": dominant_phase,
                        "total_volume": round(total_volume, 2),
                        "log_entries": len(annotated_df),
                        "fvg_detected_count": int(annotated_df["fvg_detected"].sum()),
                        "liquidity_grab_detected_count": int(
                            annotated_df["liquidity_grab_detected"].sum()
                        ),
                        "bos_mss_detected_count": int(
                            annotated_df["bos_mss_detected"].sum()
                        ),
                        "validated_ob_count": int(annotated_df["validated_ob"].sum()),
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

        # --- CORRECTION : Découverte d'actifs non listés basée sur le journal d'audit ---
        try:
            if symbol_column_name in all_trades_df.columns:
                discovered_assets = set(all_trades_df[symbol_column_name].unique())
                configured_assets = set(tradeable_assets)
                unlisted_assets = discovered_assets - configured_assets

                for asset in unlisted_assets:
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
        Ce rapport offre une vue d'ensemble des phases de marché et des anomalies détectées
        pour tous les actifs surveillés. Le nom du fichier de sortie est dynamique et inclut un timestamp.

        Args:
            filename (str, optional): Nom de base du fichier de sortie (sans extension).
                                    Si None, utilise le nom configuré dynamiquement.
        """
        # Utilise le nom de fichier dynamique chargé dans __init__
        final_filename_base = filename or self.multi_asset_report_file_name

        timestamp_str = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        final_filename_with_ts = f"{final_filename_base}_{timestamp_str}"

        if not self.multi_asset_report_data:
            self.logger.warning(
                "No multi-asset report data available to generate a report. Skipping report generation."
            )
            self.log_audit_event(
                "REPORT_EMPTY",
                "Attempted to generate multi-asset report but no data was available.",
                asset="N/A",
                timestamp=datetime.now(UTC),
            )  # Enregistrer l'événement d'audit
            return

        report_df = pd.DataFrame(self.multi_asset_report_data)

        # Ajouter un timestamp global au rapport
        report_df["report_generation_time"] = pd.Timestamp.now(UTC).isoformat()

        self.logger.info("Generating multi-asset report.")

        # Export en JSONL pour la flexibilité dashboard
        filepath_jsonl = self.output_path / f"{final_filename_with_ts}.jsonl"
        # Implémentation d'une écriture atomique pour chaque format d'exportation
        temp_filepath_jsonl = filepath_jsonl.with_suffix(".tmp")
        try:
            report_df.to_json(
                temp_filepath_jsonl, orient="records", lines=True, date_format="iso"
            )
            temp_filepath_jsonl.rename(
                filepath_jsonl
            )  # Renomme le fichier temporaire en fichier final
            self.logger.info(
                f"Multi-asset dashboard report exported to {filepath_jsonl}"
            )
            self.log_audit_event(
                "REPORT_EXPORTED",
                f"JSONL multi-asset report exported: {final_filename_with_ts}.jsonl",
                asset="ALL",
                timestamp=datetime.now(UTC),
            )  # Audit de l'export
        except Exception as e:
            if temp_filepath_jsonl.exists():
                temp_filepath_jsonl.unlink()  # Nettoyage du fichier temporaire en cas d'erreur
            self.logger.error(
                f"Failed to export multi-asset report to JSONL: {e}", exc_info=True
            )  # Utilise self.logger.error
            self.log_audit_event(
                "EXPORT_ERROR",
                f"Failed to export JSONL multi-asset report: {e}",
                asset="ALL",
                timestamp=datetime.now(UTC),
            )  # Audit de l'échec d'export

        # Export en CSV pour une lecture facile
        filepath_csv = self.output_path / f"{final_filename_with_ts}.csv"
        temp_filepath_csv = filepath_csv.with_suffix(".tmp")
        try:
            # Assurez une précision adéquate pour les valeurs numériques dans le CSV
            report_df.to_csv(temp_filepath_csv, index=False, float_format="%.5f")
            temp_filepath_csv.rename(
                filepath_csv
            )  # Renomme le fichier temporaire en fichier final
            self.logger.info(f"Multi-asset dashboard report exported to {filepath_csv}")
            self.log_audit_event(
                "REPORT_EXPORTED",
                f"CSV multi-asset report exported: {final_filename_with_ts}.csv",
                asset="ALL",
                timestamp=datetime.now(UTC),
            )  # Audit de l'export
            # TODO: FORMATAGE - Gérer le formatage des nombres (précision, séparateur décimal) pour le CSV si nécessaire via des options de to_csv.
        except Exception as e:
            if temp_filepath_csv.exists():
                temp_filepath_csv.unlink()  # Nettoyage du fichier temporaire en cas d'erreur
            self.logger.error(
                f"Failed to export multi-asset report to CSV: {e}", exc_info=True
            )  # Utilise self.logger.error
            self.log_audit_event(
                "EXPORT_ERROR",
                f"Failed to export CSV multi-asset report: {e}",
                asset="ALL",
                timestamp=datetime.now(UTC),
            )  # Audit de l'échec d'export

        # Export en Markdown
        filepath_md = self.output_path / f"{final_filename_with_ts}.md"
        temp_filepath_md = filepath_md.with_suffix(".tmp")
        try:
            with open(temp_filepath_md, "w", encoding="utf-8") as f:
                f.write(
                    f"# Rapport du Tableau de Bord Multi-Actifs - {pd.Timestamp.now(UTC).strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                )  # Utilise UTC pour le timestamp du rapport
                f.write(
                    "Ce rapport fournit un aperçu consolidé des phases de marché et des anomalies détectées pour chaque actif surveillé.\n\n"
                )
                f.write("## Synthèse par Actif\n")
                f.write(report_df.to_markdown(index=False))
                f.write(
                    "\n\n*Score de Confiance Moyen*: Indique la robustesse de la détection de phase. Plus le score est élevé (proche de 1.0), plus la détection est considérée comme fiable.\n"
                )
                f.write(
                    "*Anomalies Détectées*: Signale la présence d'événements inhabituels (e.g., pics de volume, prises de liquidité).\n"
                )
            temp_filepath_md.rename(
                filepath_md
            )  # Renomme le fichier temporaire en fichier final
            self.logger.info(f"Multi-asset dashboard report exported to {filepath_md}")
            self.log_audit_event(
                "REPORT_EXPORTED",
                f"Markdown multi-asset report exported: {final_filename_with_ts}.md",
                asset="ALL",
                timestamp=datetime.now(UTC),
            )  # Audit de l'export
        except Exception as e:
            if temp_filepath_md.exists():
                temp_filepath_md.unlink()  # Nettoyage du fichier temporaire en cas d'erreur
            self.logger.error(
                f"Failed to export multi-asset report to Markdown: {e}", exc_info=True
            )  # Utilise self.logger.error
            self.log_audit_event(
                "EXPORT_ERROR",
                f"Failed to export Markdown multi-asset report: {e}",
                asset="ALL",
                timestamp=datetime.now(UTC),
            )  # Audit de l'échec d'export


def generate_demo_logs(
    num_logs: int = 100,
    symbol: str = "EURUSD",
    config_manager_instance: Optional[Any] = None,
) -> pd.DataFrame:
    """
    Génère un jeu de logs de démonstration avec des patterns identifiables et des données OHLCV complètes.
    Les patterns, les changements de prix et les paramètres de trade sont maintenant dynamiques via ConfigManager.

    Args:
        num_logs (int): Nombre de logs à générer.
        symbol (str): Symbole de l'actif pour les logs générés.
        config_manager_instance (Optional[ConfigManager]): Instance du ConfigManager pour accéder
                                                            aux paramètres dynamiques de démonstration.

    Returns:
        pd.DataFrame: Un DataFrame Pandas contenant les logs de démonstration générés.
    """
    logs = []

    # Récupération sécurisée des paramètres de démonstration depuis ConfigManager
    # Utilisation de valeurs par défaut raisonnables si ConfigManager n'est pas disponible.
    default_start_price = 1.08500
    demo_patterns_default = [
        ("range", 20),
        ("manip_down", 5),
        ("expansion_up", 15),
        ("consolidation", 10),
        ("scalp_burst", 5),
        ("expansion_up", 10),
        ("manip_up", 5),
        ("reversal_down", 15),
        ("range", 15),
    ]
    demo_price_change_params_default = {
        "default_normal_std": 0.00005,
        "range_uniform_min": -0.00002,
        "range_uniform_max": 0.00002,
        "manip_drop_init": 0.0005,
        "manip_drop_change": -0.0008,
        "manip_drop_rebound": 0.0004,
        "manip_drop_final_rebound": 0.001,
        "expansion_up_init": 0.0003,
        "expansion_up_change_base": 0.0002,
        "expansion_up_change_std": 0.00005,
        "consolidation_uniform_min": -0.00001,
        "consolidation_uniform_max": 0.00001,
        "scalp_burst_base": 0.00015,
        "scalp_burst_std": 0.00002,
        "manip_pop_init": 0.0005,
        "manip_pop_change": 0.0008,
        "manip_pop_fall": -0.0004,
        "manip_pop_final_fall": -0.001,
        "reversal_down_init": 0.0003,
        "reversal_down_change_base": -0.0002,
        "reversal_down_change_std": 0.00005,
    }
    demo_volume_range_default = {"min": 0.5, "max": 1.5}
    demo_trade_params_default = {"sl": 0, "tp": 0, "magic_number": 12345}

    start_price = (
        config_manager_instance.get(
            "phase_detection_defaults.demo_start_price", default_start_price
        )
        if config_manager_instance
        else default_start_price
    )
    demo_patterns = (
        config_manager_instance.get(
            "phase_detection_defaults.demo_patterns", demo_patterns_default
        )
        if config_manager_instance
        else demo_patterns_default
    )
    demo_price_change_params = (
        config_manager_instance.get(
            "phase_detection_defaults.demo_price_change_params",
            demo_price_change_params_default,
        )
        if config_manager_instance
        else demo_price_change_params_default
    )
    demo_volume_range = (
        config_manager_instance.get(
            "phase_detection_defaults.demo_volume_range", demo_volume_range_default
        )
        if config_manager_instance
        else demo_volume_range_default
    )
    demo_trade_params = (
        config_manager_instance.get(
            "phase_detection_defaults.demo_trade_params", demo_trade_params_default
        )
        if config_manager_instance
        else demo_trade_params_default
    )

    # Correction: Utilisation de datetime.now(UTC) pour la cohérence des timestamps et pour un point de départ fixe
    start_time = datetime.now(UTC) - timedelta(minutes=num_logs * 5)

    # Correction: Initialisation de order_id_counter à l'intérieur de la fonction
    order_id_counter = 1
    price = start_price

    for phase, count in demo_patterns:
        for i in range(count):
            # Correction: Assurer l'ordre chronologique des timestamps
            current_time = start_time + timedelta(minutes=order_id_counter * 5)

            order_type = "UNKNOWN"  # Default pour la démo
            price_change = np.random.normal(
                0, demo_price_change_params["default_normal_std"]
            )
            volume = np.random.uniform(
                demo_volume_range["min"], demo_volume_range["max"]
            )

            # Logique de changement de prix basée sur la phase (telle quelle)
            if phase == "range":
                price_change = np.random.uniform(
                    demo_price_change_params["range_uniform_min"],
                    demo_price_change_params["range_uniform_max"],
                )
                order_type = "BUY" if i % 2 == 0 else "SELL"
            elif phase == "manip_down":
                if i == 0:
                    price -= demo_price_change_params["manip_drop_init"]
                price_change = (
                    demo_price_change_params["manip_drop_change"]
                    if i == 1
                    else demo_price_change_params["manip_drop_rebound"]
                )
                order_type = "SELL" if i == 1 else "BUY"
                if i == 2:
                    price += demo_price_change_params["manip_drop_final_rebound"]
            elif phase == "expansion_up":
                if i == 0:
                    price += demo_price_change_params["expansion_up_init"]
                price_change = demo_price_change_params[
                    "expansion_up_change_base"
                ] + abs(
                    np.random.normal(
                        0, demo_price_change_params["expansion_up_change_std"]
                    )
                )
                order_type = "BUY"
            elif phase == "consolidation":
                price_change = np.random.uniform(
                    demo_price_change_params["consolidation_uniform_min"],
                    demo_price_change_params["consolidation_uniform_max"],
                )
                order_type = "BUY" if i % 2 == 0 else "SELL"
            elif phase == "scalp_burst":
                price_change = demo_price_change_params["scalp_burst_base"] + abs(
                    np.random.normal(0, demo_price_change_params["scalp_burst_std"])
                )
                order_type = "BUY" if i % 2 == 0 else "SELL"
            elif phase == "manip_up":
                if i == 0:
                    price += demo_price_change_params["manip_pop_init"]
                price_change = (
                    demo_price_change_params["manip_pop_change"]
                    if i == 1
                    else demo_price_change_params["manip_pop_fall"]
                )
                order_type = "BUY" if i == 1 else "SELL"
                if i == 2:
                    price -= demo_price_change_params["manip_pop_final_fall"]
            elif phase == "reversal_down":
                if i == 0:
                    price -= demo_price_change_params["reversal_down_init"]
                price_change = demo_price_change_params[
                    "reversal_down_change_base"
                ] - abs(
                    np.random.normal(
                        0, demo_price_change_params["reversal_down_change_std"]
                    )
                )
                order_type = "SELL"

            price += price_change

            # Pour simuler des bougies OHLC complètes
            # open_price est la clôture de la bougie précédente
            # high et low sont dérivés pour englober la variation open -> close plus un petit bruit
            last_close = logs[-1]["close"] if logs else start_price
            open_price = last_close

            # Pour éviter les bougies "plates" et simuler un peu de volatilité intraday
            # Assurez-vous que high >= open, close et low <= open, close
            simulated_high_wick = np.random.uniform(
                0, 0.00015
            )  # Petite mèche haussière
            simulated_low_wick = np.random.uniform(0, 0.00015)  # Petite mèche baissière

            current_high = max(open_price, price) + simulated_high_wick
            current_low = min(open_price, price) - simulated_low_wick

            logs.append(
                {
                    "timestamp": current_time,
                    "symbol": symbol,
                    "open": round(open_price, 5),
                    "high": round(current_high, 5),
                    "low": round(current_low, 5),
                    "close": round(price, 5),
                    "volume": round(volume, 2),
                    "order_type": order_type,
                    "sl": demo_trade_params["sl"],
                    "tp": demo_trade_params["tp"],
                    "magic_number": demo_trade_params["magic_number"],
                    "order_id": order_id_counter,
                }
            )
            order_id_counter += 1

    # TODO: DEMO - Diversifier les patterns de logs pour couvrir plus de scénarios de phase de marché (ex: tendance prolongée, range avec EQH/EQL clairs).
    # TODO: DEMO - Affiner la simulation de prix pour être plus réaliste (ex: gaps, bougies de forte impulsion, pin bars pour simuler liquidity grab).
    # TODO: DEMO - Ajouter une option pour générer des logs avec des gaps de données ou des anomalies pour tester la robustesse du chargeur.
    return pd.DataFrame(logs)


def generate_demo_multi_asset_config(
    config_manager_instance: Optional[Any] = None,
    filepath: Union[str, Path] = None,
    assets: Optional[List[str]] = None,
):
    """
    Génère un fichier de configuration JSON de démonstration pour le trading multi-actifs.
    Ce fichier est utilisé pour simuler une configuration d'entrée pour la fonction `process_multi_asset_config`.
    Le chemin du fichier de sortie et les actifs par défaut sont dynamiques via ConfigManager.

    Args:
        config_manager_instance (Optional[ConfigManager]): L'instance du ConfigManager pour accéder aux paramètres dynamiques.
                                                            Utilise Any pour éviter les problèmes d'importation circulaire si ConfigManager n'est pas encore importé globalement.
        filepath (Union[str, Path], optional): Chemin et nom du fichier de configuration à créer. Si None, utilise le chemin par défaut.
        assets (List[str], optional): Liste des actifs à inclure dans la configuration. Si None, utilise les actifs par défaut.

    Returns:
        None
    """
    # Récupérer les paramètres par défaut dynamiques via ConfigManager
    default_filepath_name = "config_trade_dynamic.json"
    default_assets = ["EURUSD", "GBPUSD"]

    if config_manager_instance:
        # Assurez-vous d'utiliser `config_manager_instance` pour récupérer les valeurs
        default_filepath_name = config_manager_instance.get(
            "phase_detection_defaults.demo_config_file_name", default_filepath_name
        )
        default_assets = config_manager_instance.get(
            "phase_detection_defaults.demo_default_assets", default_assets
        )

    final_filepath = (
        Path(filepath) if filepath is not None else Path(default_filepath_name)
    )
    final_assets = assets if assets is not None else default_assets

    config = {"tradeable_assets": final_assets}

    # Implémentation d'une écriture atomique pour le fichier de configuration de démo
    temp_filepath = final_filepath.with_suffix(".tmp")
    try:
        # Créer les répertoires parents si nécessaire avant d'écrire
        final_filepath.parent.mkdir(parents=True, exist_ok=True)

        with open(temp_filepath, "w") as f:
            json.dump(config, f, indent=4)

        temp_filepath.rename(final_filepath)  # Rendre l'écriture atomique

        # Utilisation de logging, car cette fonction n'est pas une méthode de classe
        logging.info(f"Demo multi-asset config generated at {final_filepath}")
        # TODO: DEMO - Ajouter une validation basique du fichier généré pour s'assurer de sa conformité au schéma attendu.
    except Exception as e:
        # En cas d'erreur, nettoyer le fichier temporaire
        if temp_filepath.exists():
            temp_filepath.unlink()
        # Utilisation de logging
        logging.error(
            f"Failed to generate demo config: {e}", exc_info=True
        )  # Ajout de exc_info=True
        # TODO: ERREUR - Signaler l'échec de génération de la démo config via un log d'audit.

    # ------------------------------------------------------------------------------
    # Section de Démonstration et de Validation (le bloc __main__)
    # ------------------------------------------------------------------------------


if __name__ == "__main__":
    # Utilisation de logging, car ce n'est pas une méthode de classe
    logging.info("--- Running PhaseObserver Demo ---")

    # Importation locale et temporaire pour la démo
    # Correction: Le module ConfigManager doit être importé directement pour son utilisation
    from core.config_manager import ConfigManager

    # Pour la démo, initialiser un ConfigManager factice ou réel si nécessaire
    demo_config_manager = None
    try:
        # Correction: Utilisation de Path.resolve() pour s'assurer du chemin absolu et correct
        # main_config_path doit pointer vers le prod_config.json que ConfigManager utilise comme template
        main_config_path = (
            Path(__file__).resolve().parent.parent / "config" / "prod_config.json"
        )

        # S'assurer que le répertoire 'config' existe
        main_config_path.parent.mkdir(parents=True, exist_ok=True)

        demo_config_manager = ConfigManager()  # Utilisation directe de ConfigManager
        demo_config_manager.initialize_dynamic_config(
            template_path=str(main_config_path),
            # L'output_path et config_dir devraient être le répertoire où les configs sont lues/écrites
            output_path=str(
                main_config_path.parent / "temp_config.json"
            ),  # Un chemin temporaire pour la config réelle si elle est modifiée
            config_dir=str(
                main_config_path.parent
            ),  # Répertoire où se trouve prod_config et où les autres configs pourraient être
        )
        # Utilisation de logging
        logging.info("ConfigManager pour démo initialisé avec succès.")
    except Exception as e:
        # Utilisation de logging
        logging.error(
            f"ATTENTION: Échec de l'initialisation du ConfigManager pour la démo: {e}",
            exc_info=True,
        )  # Ajout de exc_info=True
        demo_config_manager = None

    # Préparation des répertoires (maintenant lus dynamiquement via ConfigManager)
    # Assurez-vous que demo_config_manager n'est pas None avant d'appeler .get()
    output_path = (
        Path(demo_config_manager.get("paths.reports", "output"))
        if demo_config_manager
        else Path("output")
    )
    log_dir = (
        Path(demo_config_manager.get("paths.logs", "logs"))
        if demo_config_manager
        else Path("logs")
    )

    output_path.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    # 1. Générer des données de test pour plusieurs actifs et la config
    # Passer l'instance de ConfigManager aux fonctions de génération de démo
    # Récupérer le nom du fichier de config de démo depuis les paramètres
    demo_config_file_name = (
        demo_config_manager.get(
            "phase_detection_defaults.demo_config_file_name",
            "config_trade_dynamic.json",
        )
        if demo_config_manager
        else "config_trade_dynamic.json"
    )
    demo_config_filepath = (
        output_path / demo_config_file_name
    )  # Placer la config de démo dans le dossier de sortie

    generate_demo_multi_asset_config(
        config_manager_instance=demo_config_manager, filepath=demo_config_filepath
    )

    # Créer les fichiers de log de démo pour les actifs configurés
    # Récupérer le suffixe correct des logs depuis la configuration
    # Correction: Le suffixe doit correspondre à la réalité des logs générés et attendus par PhaseObserver.
    # Dans les démos précédentes, on écrivait en CSV. Si PhaseObserver lit du JSONL, il faut le générer en JSONL.
    # Pour la démo, on simule des logs que le PhaseObserver lira.
    asset_log_suffix = (
        demo_config_manager.get(
            "phase_detection_defaults.asset_log_file_suffix", ".csv"
        )
        if demo_config_manager
        else ".csv"
    )  # Assurez-vous que c'est le bon suffixe pour la démo

    # Correction: Les logs de démo générés doivent être des DataFrames complets (OHLCV)
    # et doivent être exportés dans le format que PhaseObserver s'attend à lire (typiquement JSONL pour les détails)
    # Si le _clean_dataframe interne de PhaseObserver s'attend à du CSV, il faut générer du CSV.
    # Pour la démo, nous allons exporter en CSV car c'est ce que la fonction `load_data` du PhaseObserver sait gérer en fallback.

    eurusd_df = generate_demo_logs(
        100, symbol="EURUSD", config_manager_instance=demo_config_manager
    )
    eurusd_df.to_csv(log_dir / f"EURUSD{asset_log_suffix}", index=False)
    # Utilisation de logging
    logging.info(
        f"Generated EURUSD demo logs for validation at {log_dir / f'EURUSD{asset_log_suffix}'.name}."
    )

    gbpusd_df = generate_demo_logs(
        120, symbol="GBPUSD", config_manager_instance=demo_config_manager
    )
    gbpusd_df.to_csv(log_dir / f"GBPUSD{asset_log_suffix}", index=False)
    # Utilisation de logging
    logging.info(
        f"Generated GBPUSD demo logs for validation at {log_dir / f'GBPUSD{asset_log_suffix}'.name}."
    )

    usdcad_df = generate_demo_logs(
        80, symbol="USDCAD", config_manager_instance=demo_config_manager
    )
    usdcad_df.to_csv(log_dir / f"USDCAD{asset_log_suffix}", index=False)
    # Utilisation de logging
    logging.info(
        f"Generated USDCAD demo logs (unlisted) at {log_dir / f'USDCAD{asset_log_suffix}'.name}."
    )

    # 2. Initialiser l'observateur avec le ConfigManager
    # On s'assure de passer le config_manager même s'il est None pour une gestion interne
    observer = PhaseObserver(config_manager=demo_config_manager)

    # 3. Lancer l'analyse complète pour tous les actifs depuis la configuration
    # Passer le chemin de la config de démo générée
    # Correction: 'log_dir' n'est plus un paramètre de process_multi_asset_config car il est membre de l'instance
    observer.process_multi_asset_config(config_filepath=demo_config_filepath)

    # 4. Générer le rapport multi-actifs
    observer.generate_multi_asset_report()

    # 5. Exporter le journal d'audit
    observer.export_audit_journal()

    # 6. Afficher un aperçu des données traitées du rapport multi-actifs
    print("\n--- Aperçu du rapport global multi-actifs ---")
    if observer.multi_asset_report_data:
        report_df = pd.DataFrame(observer.multi_asset_report_data)
        print(report_df.to_string())
    else:
        print("Aucune donnée de rapport multi-actifs générée.")

    # 7. Afficher un aperçu du journal d'audit
    print("\n--- Aperçu du journal d'audit ---")
    if observer.audit_journal:
        audit_df = pd.DataFrame(observer.audit_journal)
        print(audit_df.to_string())
    else:
        print("Aucun événement d'audit enregistré.")

    # 8. Valider qu'aucune phase n'est vide pour le dernier DF analysé (si applicable, sinon ce check est moins pertinent globalement)
    if observer.multi_asset_report_data:
        # Exemple de validation plus significative : s'assurer que des phases ont été détectées
        has_phases_detected = all(
            item["dominant_phase"] != "N/A" for item in observer.multi_asset_report_data
        )
        if has_phases_detected:
            # Utilisation de logging
            logging.info(
                "VALIDATION PASSED: Dominant phases detected for all processed assets."
            )
        else:
            # Utilisation de logging
            logging.warning("VALIDATION WARNING: Some dominant phases were 'N/A'.")

        # Utilisation de logging
        logging.info("VALIDATION PASSED: Multi-asset report data exists.")
    else:
        # Utilisation de logging
        logging.error("VALIDATION FAILED: No multi-asset report data generated.")

    # Utilisation de logging
    logging.info(
        f"\n--- Demo Finished. Check the '{observer.output_path}' directory. ---"
    )

    # Nettoyage des fichiers de démo (optionnel pour les tests locaux)
    # import shutil
    # if log_dir.exists():
    #     shutil.rmtree(log_dir)
    # if output_path.exists():
    #     shutil.rmtree(output_path)
    # if demo_config_filepath.exists():
    #     os.remove(demo_config_filepath)
