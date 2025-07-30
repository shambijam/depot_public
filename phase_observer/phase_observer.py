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
    #level=logging.INFO, format="%(asctime)s - [%(levelname)s] - %(name)s - %(message)s"
)


class PhaseObserver:
    """
    Orchestre l'analyse de données de marché pour y superposer un contexte
    institutionnel et des signaux Smart Money Concepts (SMC) à haute résolution.
    """

    def __init__(self, config_manager: ConfigManager):
        """
        Initialise l'observateur de phases en chargeant dynamiquement tous les
        paramètres d'analyse depuis le ConfigManager.

        Args:
            config_manager (ConfigManager): Instance du ConfigManager.
        """
        self.logger = logging.getLogger(__name__)
        self.config_manager = config_manager

        # Attributs d'état pour le module
        self.audit_journal: List[Dict] = []
        self._liquidity_levels_cache: Dict[str, Any] = (
            {}
        )  # Cache pour les niveaux de liquidité

        # AMÉLIORATION : La logique de chargement est maintenant dans une méthode dédiée
        self._load_settings()

        self.logger.info(
            f"PhaseObserver initialisé. Lookback window par défaut: {self.lookback_window}."
        )
        # TODO: Implémenter une validation des paramètres chargés pour s'assurer de leur cohérence
        #       (ex: `trend_sma_fast_ratio` < `trend_sma_slow_ratio`).

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

    def detect_order_block(
        self, df: pd.DataFrame, df_htf: Optional[pd.DataFrame] = None
    ) -> List[Optional[Dict[str, Any]]]:
        """
        Détecte les Order Blocks (OB) et fournit les critères de leur détection.
        Le score de fiabilité est supprimé pour laisser le ConfigManager évaluer la confluence.
        """
        self.logger.debug(
            "Détection des Order Blocks (sans scoring de fiabilité direct)..."
        )

        # Étape 1: Identifier les OB potentiels
        df["candle_move"] = df["close"] - df["open"]
        impulse_threshold = self.config_manager.get(
            "phase_detection_defaults.impulse_threshold", 0.0008
        )

        bullish_ob_mask = (df["candle_move"] > impulse_threshold) & (
            df["candle_move"].shift(1) < 0
        )
        bearish_ob_mask = (df["candle_move"] < -impulse_threshold) & (
            df["candle_move"].shift(1) > 0
        )
        potential_ob_mask = bullish_ob_mask | bearish_ob_mask

        swing_highs, swing_lows = self._get_swing_points(df)

        # Pré-calculer la tendance du timeframe supérieur UNE SEULE FOIS si les données sont fournies
        htf_trend = None
        if df_htf is not None and not df_htf.empty:
            htf_trend = self._get_trend(df_htf).iloc[-1]

        # Étape 2: Détecter les OB et enregistrer les critères (sans calculer de score ici)
        results = [None] * len(df)

        ob_positions = df.index.get_indexer(df.index[potential_ob_mask])

        for pos in ob_positions:
            if pos == 0:
                continue

            ob_candle_pos = pos - 1

            if ob_candle_pos < 0 or pos >= len(df):
                continue

            ob_candle = df.iloc[ob_candle_pos]
            impulse_candle = df.iloc[pos]
            ob_zone = (ob_candle["low"], ob_candle["high"])

            # Les critères sont enregistrés, mais pas utilisés pour un score immédiat ici.
            # C'est au ConfigManager de juger la confluence de ces critères.
            criteria_details = {
                "fvg_confluence": False,
                "liquidity_confluence": False,
                "market_extreme_confluence": False,
                "unmitigated": True,
                "multi_tf_alignment": False,
            }

            if pd.notna(impulse_candle.get("fvg_details")):
                criteria_details["fvg_confluence"] = True
            if pd.notna(ob_candle.get("liquidity_grab_details")) or pd.notna(
                impulse_candle.get("liquidity_grab_details")
            ):
                criteria_details["liquidity_confluence"] = True
            if (ob_candle.name in swing_highs.index) or (
                ob_candle.name in swing_lows.index
            ):
                criteria_details["market_extreme_confluence"] = True

            future_candles = df.iloc[pos + 1 :]
            mitigated = future_candles[
                (future_candles["high"] >= ob_zone[0])
                & (future_candles["low"] <= ob_zone[1])
            ]
            if not mitigated.empty:
                criteria_details["unmitigated"] = False

            ob_type_is_bullish = bullish_ob_mask.iloc[pos]
            if htf_trend:
                if (ob_type_is_bullish and htf_trend == "bullish") or (
                    not ob_type_is_bullish and htf_trend == "bearish"
                ):
                    criteria_details["multi_tf_alignment"] = True

            # L'OB est détecté s'il y a au moins un critère de confluence.
            # La décision de la "qualité" ou de la "force" de cet OB revient au ConfigManager
            # qui évaluera les 'criteria_details' via les règles JSON.
            if any(
                criteria_details.values()
            ):  # Si au moins un critère est vrai, l'OB est "validé" pour l'exportation
                results[pos] = {
                    "type": "bullish" if ob_type_is_bullish else "bearish",
                    "zone": list(ob_zone),
                    "criteria_details": criteria_details,  # Exporte les détails des critères sans un score aggrégé
                }

        return results

    def detect_fvg(self, df: pd.DataFrame) -> List[Optional[Dict[str, float]]]:
        """
        Détecte les Fair Value Gaps (FVG) de manière vectorielle.

        Un FVG est un déséquilibre créé par un pattern de 3 bougies.

        Args:
            df (pd.DataFrame): DataFrame avec les colonnes OHLC.

        Returns:
            List[Optional[Dict]]: Une liste de dictionnaires (un par ligne du df),
                                  contenant les infos du FVG détecté, ou None.
        """
        self.logger.debug("Détection des Fair Value Gaps (FVG)...")

        # AMÉLIORATION : Logique vectorielle et techniquement correcte.
        low_p0 = df["low"]
        high_p2 = df["high"].shift(2)

        high_p0 = df["high"]
        low_p2 = df["low"].shift(2)

        # FVG Haussier: le bas de la bougie actuelle est plus haut que le haut de la bougie d'il y a 2 périodes.
        bullish_fvg_condition = low_p0 > high_p2

        # FVG Baissier: le haut de la bougie actuelle est plus bas que le bas de la bougie d'il y a 2 périodes.
        bearish_fvg_condition = high_p0 < low_p2

        results = []
        for i in range(len(df)):
            fvg_info = None
            if bullish_fvg_condition.iloc[i]:
                fvg_info = {
                    "type": "bullish",
                    "top": df["low"].iloc[i],
                    "bottom": df["high"].iloc[i - 2],
                }
            elif bearish_fvg_condition.iloc[i]:
                fvg_info = {
                    "type": "bearish",
                    "top": df["high"].iloc[i - 2],
                    "bottom": df["low"].iloc[i],
                }
            results.append(fvg_info)

        # TODO: Calculer la "magnitude" du FVG (sa taille en pips) pour évaluer son importance.
        # TODO: Ajouter une logique pour marquer un FVG comme "rempli" une fois que le prix
        #       a comblé le déséquilibre.
        return results

    def _get_swing_points(
        self, df: pd.DataFrame, order: Optional[int] = None
    ) -> tuple[pd.Series, pd.Series]:
        """
        Identifie les points de swing (hauts et bas) dans un DataFrame.
        Le paramètre 'order' peut être passé directement par la fonction appelante
        ou chargé dynamiquement depuis la configuration si non fourni.

        Args:
            df (pd.DataFrame): Données de marché avec colonnes 'high' et 'low'.
            order (Optional[int]): L'ordre des points de swing. Si non fourni, il est lu depuis la configuration.

        Returns:
            tuple[pd.Series, pd.Series]: Deux Series pandas contenant les prix des swing highs et swing lows.
        """
        # CORRECTION MAJEURE: Le paramètre 'order' est maintenant accepté dans la signature.
        # Il est utilisé en priorité s'il est fourni (non None).
        # Sinon, il est lu depuis phase_observer_config.json.
        if order is None:  # Si 'order' n'a pas été passé en argument
            order_param_value = self.config_manager.get(
                "phase_detection_defaults.swing_point_order", 5
            )
            self.logger.debug(
                f"[_get_swing_points] 'order' non fourni, lecture depuis config: {order_param_value}"
            )
        else:  # Si 'order' a été passé en argument (comme dans detect_eqh_eql)
            order_param_value = order
            self.logger.debug(
                f"[_get_swing_points] 'order' fourni en argument: {order_param_value}"
            )

        # Validation pour s'assurer que le paramètre est utilisable
        if not isinstance(order_param_value, int) or order_param_value < 1:
            self.logger.warning(
                f"Paramètre 'order' invalide ({order_param_value}). Utilisation de la valeur par défaut 5."
            )
            order_param_value = 5

        # La logique de détection reste la même, elle est robuste et vectorisée.
        window_size = 2 * order_param_value + 1  # Utilisation de order_param_value

        # S'assurer qu'il y a suffisamment de données pour la fenêtre de calcul
        if len(df) < window_size:
            self.logger.warning(
                f"Insuffisant de barres ({len(df)}) pour calculer les points de swing avec une fenêtre de {window_size}. Retourne des Series vides."
            )
            return pd.Series([], dtype=float), pd.Series([], dtype=float)

        highs = df["high"][
            (
                df["high"]
                == df["high"]
                .rolling(window=window_size, center=True, min_periods=window_size)
                .max()
            )
        ]
        lows = df["low"][
            (
                df["low"]
                == df["low"]
                .rolling(window=window_size, center=True, min_periods=window_size)
                .min()
            )
        ]

        return highs, lows

    def detect_liquidity_grab(self, df: pd.DataFrame) -> List[Optional[Dict[str, Any]]]:
        """
        Détecte les prises de liquidité (sweeps) de manière vectorielle.

        Un sweep se produit lorsqu'une mèche de bougie dépasse un swing high/low précédent,
        mais que le corps de la bougie clôture en dessous/au-dessus de ce niveau.

        Args:
            df (pd.DataFrame): Données de marché avec colonnes OHLC.

        Returns:
            List[Optional[Dict]]: Une liste de dictionnaires pour chaque bougie,
                                  décrivant le sweep s'il a eu lieu.
        """
        self.logger.debug("Détection des prises de liquidité (Sweeps)...")
        swing_highs, swing_lows = self._get_swing_points(df)

        # Propage le dernier swing high/low pour la comparaison
        df["last_swing_high"] = swing_highs.ffill()
        df["last_swing_low"] = swing_lows.ffill()

        # Conditions vectorielles
        bullish_sweep = (df["low"] < df["last_swing_low"].shift(1)) & (
            df["close"] > df["last_swing_low"].shift(1)
        )
        bearish_sweep = (df["high"] > df["last_swing_high"].shift(1)) & (
            df["close"] < df["last_swing_high"].shift(1)
        )

        results = []
        for i in range(len(df)):
            info = None
            if bullish_sweep.iloc[i]:
                info = {
                    "type": "bullish_sweep",
                    "level_swept": df["last_swing_low"].shift(1).iloc[i],
                }
            elif bearish_sweep.iloc[i]:
                info = {
                    "type": "bearish_sweep",
                    "level_swept": df["last_swing_high"].shift(1).iloc[i],
                }
            results.append(info)

        # TODO: Calculer la "force" du sweep en mesurant la distance de la mèche au-delà du niveau
        #       et la force de la clôture dans la direction opposée.
        return results

    def detect_breaker_block(self, df: pd.DataFrame) -> List[Optional[Dict[str, Any]]]:
        """
        Détecte les Breaker Blocks (BB) de manière conceptuelle. (Implémentation simplifiée)

        Un Breaker est un Order Block qui échoue à retenir le prix, se fait traverser,
        puis est re-testé comme support/résistance.

        Args:
            df (pd.DataFrame): Données de marché avec colonnes OHLC.

        Returns:
            List[Optional[Dict]]: Informations sur le Breaker Block détecté.
        """
        self.logger.debug("Détection des Breaker Blocks...")
        # L'implémentation d'un détecteur de Breaker Block vectoriel est très complexe.
        # Elle nécessite de suivre l'état des OB, leur mitigation, et les cassures de structure.
        # TODO: Implémenter la logique complète en chaînant les signaux :
        # 1. Détecter un Order Block.
        # 2. Détecter une cassure de cet Order Block (BOS).
        # 3. Détecter un retour du prix vers la zone de l'ancien OB.
        # Pour l'instant, nous retournons une liste vide.
        return [None] * len(df)

    def detect_bos_mss(self, df: pd.DataFrame) -> List[Optional[Dict[str, Any]]]:
        """
        Détecte les Breaks of Structure (BOS) et les Market Structure Shifts (MSS)
        en analysant la tendance de fond au moment de la cassure d'un swing point.

        Args:
            df (pd.DataFrame): Données de marché avec colonnes OHLC.

        Returns:
            List[Optional[Dict]]: Informations sur la cassure de structure,
                                distinguant 'bos' (continuation) de 'mss' (retournement).
        """
        self.logger.debug("Détection intelligente des Breaks of Structure (BOS/MSS)...")

        # S'assurer que la tendance est calculée sur le DataFrame
        if "trend" not in df.columns:
            df["trend"] = self._get_trend(df)

        swing_highs, swing_lows = self._get_swing_points(df)
        df["last_swing_high"] = swing_highs.ffill()
        df["last_swing_low"] = swing_lows.ffill()

        # --- AMÉLIORATION MAJEURE : Distinction BOS vs MSS ---
        # On regarde la tendance *avant* la cassure pour la contextualiser.
        previous_trend = df["trend"].shift(1)

        # Condition de base : la clôture de la bougie doit être au-delà du dernier swing.
        is_bullish_break = df["close"] > df["last_swing_high"].shift(1)
        is_bearish_break = df["close"] < df["last_swing_low"].shift(1)

        # Logique de classification
        # BOS : Cassure dans le sens de la tendance existante (continuation)
        bullish_bos = (previous_trend == "bullish") & is_bullish_break
        bearish_bos = (previous_trend == "bearish") & is_bearish_break

        # MSS : Cassure à l'encontre de la tendance existante (signe de retournement)
        bullish_mss = (previous_trend == "bearish") & is_bullish_break
        bearish_mss = (previous_trend == "bullish") & is_bearish_break

        results = []
        for i in range(len(df)):
            info = None
            if bullish_bos.iloc[i]:
                info = {
                    "type": "bullish_bos",
                    "level_broken": df["last_swing_high"].shift(1).iloc[i],
                }
            elif bearish_bos.iloc[i]:
                info = {
                    "type": "bearish_bos",
                    "level_broken": df["last_swing_low"].shift(1).iloc[i],
                }
            elif bullish_mss.iloc[i]:
                info = {
                    "type": "bullish_mss",
                    "level_broken": df["last_swing_high"].shift(1).iloc[i],
                }
            elif bearish_mss.iloc[i]:
                info = {
                    "type": "bearish_mss",
                    "level_broken": df["last_swing_low"].shift(1).iloc[i],
                }
            results.append(info)

        return results

    def detect_eqh_eql(self, df: pd.DataFrame) -> List[Optional[Dict[str, Any]]]:
        """
        Détecte les Equal Highs (EQH) et Equal Lows (EQL) de manière vectorielle.
        Ces niveaux indiquent des zones où la liquidité pourrait être ciblée par les acteurs institutionnels.

        Args:
            df (pd.DataFrame): Données de marché avec colonnes OHLC.

        Returns:
            List[Optional[Dict]]: Informations sur les niveaux d'égalité détectés (type, niveau, indices concernés).
        """
        self.logger.debug("Détection des Equal Highs / Equal Lows...")

        # S'assurer que 'min_window_eqh_eql' est chargé via _load_settings.
        min_window_eqh_eql = getattr(
            self,
            "min_window_eqh_eql",
            self.config_manager.get("phase_detection_defaults.min_window_eqh_eql", 10),
        )

        if len(df) < min_window_eqh_eql:
            self.logger.debug(
                f"EQH/EQL: Fenêtre trop petite ({len(df)} barres), min requis: {min_window_eqh_eql}."
            )
            return [None] * len(df)

        # Correction de l'appel à _get_swing_points : Ne PAS passer 'order' en argument,
        # car _get_swing_points lit déjà 'order' de la configuration.
        swing_highs_series, swing_lows_series = self._get_swing_points(
            df
        )  # <-- CORRECTION ICI

        # Convertir les Series de swing points en listes d'indices et de valeurs pour une manipulation plus facile
        swing_highs_list = [
            (idx, val) for idx, val in swing_highs_series.dropna().items()
        ]
        swing_lows_list = [
            (idx, val) for idx, val in swing_lows_series.dropna().items()
        ]

        results = [None] * len(df)

        # CORRECTION ICI: S'assurer que EQ_LEVEL_TOLERANCE est chargé.
        # Il devrait être chargé via _load_settings. Sinon, utiliser un fallback configurable.
        tolerance_abs = getattr(
            self,
            "eq_level_tolerance",
            self.config_manager.get(
                "phase_detection_defaults.eq_level_tolerance", 0.0001
            ),
        )

        # Détection des Equal Highs (EQH)
        for i in range(len(swing_highs_list)):
            idx1, val1 = swing_highs_list[i]
            confluence_indices = [idx1]
            for j in range(i + 1, len(swing_highs_list)):
                idx2, val2 = swing_highs_list[j]
                # Vérifier si les deux highs sont "égaux" selon la tolérance
                if np.isclose(val1, val2, atol=tolerance_abs):
                    # Vérifier s'ils sont suffisamment éloignés pour être considérés comme distincts EQH
                    # et non juste un plateau
                    if (
                        abs(df.index.get_loc(idx1) - df.index.get_loc(idx2)) >= 2
                    ):  # Utiliser les positions numériques pour la distance
                        confluence_indices.append(idx2)

            if len(confluence_indices) >= 2:
                # Calculer la moyenne des niveaux des points de confluence pour le niveau EQH
                eqh_level = np.mean(
                    [swing_highs_series.loc[k] for k in confluence_indices]
                )
                # Marquer toutes les bougies entre le premier et le dernier point de confluence
                # et les points eux-mêmes comme faisant partie de l'EQH
                # Utiliser get_loc pour obtenir les positions numériques si l'index est un Timestamp
                start_marker_idx_loc = df.index.get_loc(min(confluence_indices))
                end_marker_idx_loc = df.index.get_loc(max(confluence_indices))

                for k_loc in range(start_marker_idx_loc, end_marker_idx_loc + 1):
                    # Reconvertir l'index numérique en Timestamp pour l'accès aux résultats
                    k = df.index[k_loc]  # Récupérer le Timestamp de l'index
                    if results[k_loc] is None or (
                        results[k_loc]["type"] == "EQH"
                        and results[k_loc]["confluence_count"] < len(confluence_indices)
                    ):
                        results[k_loc] = (
                            {  # Utiliser k_loc pour l'indexation du tableau de résultats
                                "type": "EQH",
                                "level": round(
                                    float(eqh_level), 5
                                ),  # Convertir en float pour la sérialisation JSON
                                "confluence_count": len(confluence_indices),
                                "indices_involved": sorted(
                                    [str(x) for x in confluence_indices]
                                ),  # Stocker les Timestamps comme string ISO format
                            }
                        )

        # Détection des Equal Lows (EQL) - Logique similaire
        for i in range(len(swing_lows_list)):
            idx1, val1 = swing_lows_list[i]
            confluence_indices = [idx1]
            for j in range(i + 1, len(swing_lows_list)):
                idx2, val2 = swing_lows_list[j]
                if np.isclose(val1, val2, atol=tolerance_abs):
                    if (
                        abs(df.index.get_loc(idx1) - df.index.get_loc(idx2)) >= 2
                    ):  # Utiliser les positions numériques pour la distance
                        confluence_indices.append(idx2)

            if len(confluence_indices) >= 2:
                eql_level = np.mean(
                    [swing_lows_series.loc[k] for k in confluence_indices]
                )
                start_marker_idx_loc = df.index.get_loc(min(confluence_indices))
                end_marker_idx_loc = df.index.get_loc(max(confluence_indices))

                for k_loc in range(start_marker_idx_loc, end_marker_idx_loc + 1):
                    k = df.index[k_loc]  # Récupérer le Timestamp de l'index
                    if results[k_loc] is None or (
                        results[k_loc]["type"] == "EQL"
                        and results[k_loc]["confluence_count"] < len(confluence_indices)
                    ):
                        results[k_loc] = {
                            "type": "EQL",
                            "level": round(float(eql_level), 5),
                            "confluence_count": len(confluence_indices),
                            "indices_involved": sorted(
                                [str(x) for x in confluence_indices]
                            ),
                        }

        # TODO: Stocker ces niveaux d'EQH/EQL détectés dans un cache (`self._liquidity_levels_cache`)
        #       pour référence future et pour calculer `nearest_major_liquidity_level_details` dans `analyze`.
        #       Le cache devrait gérer la mitigation des niveaux ou leur expiration après un certain temps.

        return results

    def detect_volume_anomaly(self, df: pd.DataFrame) -> List[Optional[Dict[str, Any]]]:
        """
        Détecte les anomalies de volume (pics et creux) et calcule le momentum du volume de manière vectorielle.

        Args:
            df (pd.DataFrame): Données de marché avec la colonne 'tick_volume'.

        Returns:
            List[Optional[Dict]]: Une liste d'informations détaillées sur l'anomalie de volume
                                et le momentum pour chaque bougie.
        """
        self.logger.debug("Détection des anomalies de volume et calcul du momentum...")

        # S'assurer que les attributs sont chargés. Ils doivent être chargés via _load_settings.
        # Si pour une raison quelconque ils ne le sont pas, utiliser des valeurs par défaut configurables.
        min_window_volume_anomaly = getattr(
            self,
            "min_window_volume_anomaly",
            self.config_manager.get(
                "phase_detection_defaults.min_window_volume_anomaly", 10
            ),
        )
        min_std_dev_volume_anomaly = getattr(
            self,
            "min_std_dev_volume_anomaly",
            self.config_manager.get(
                "phase_detection_defaults.min_std_dev_volume_anomaly", 1e-6
            ),
        )
        volume_zscore_threshold = getattr(
            self,
            "volume_zscore",
            self.config_manager.get("phase_detection_defaults.volume_zscore", 2.0),
        )

        # Vérifications initiales pour assurer la présence des données nécessaires
        if "tick_volume" not in df.columns or len(df) < min_window_volume_anomaly:
            self.logger.warning(
                f"Volume Anomaly: Données insuffisantes ou colonne 'tick_volume' manquante "
                f"({len(df)} barres, min requis: {min_window_volume_anomaly}). Retourne None."
            )
            return [None] * len(df)

        volumes = df["tick_volume"]

        # Calcul du volume moyen et de l'écart-type sur une fenêtre glissante, décalée d'une période
        # pour éviter la contamination par la bougie actuelle.
        rolling_mean = (
            volumes.rolling(window=min_window_volume_anomaly, min_periods=1)
            .mean()
            .shift(1)
        )
        rolling_std = (
            volumes.rolling(window=min_window_volume_anomaly, min_periods=1)
            .std()
            .shift(1)
        )

        # Gestion robuste de la division par zéro ou par un écart-type trop faible.
        # Remplacer les valeurs NaN et zéro par une valeur minimale sûre (min_std_dev_volume_anomaly).
        rolling_std_safe = rolling_std.fillna(min_std_dev_volume_anomaly)
        rolling_std_safe[rolling_std_safe < min_std_dev_volume_anomaly] = (
            min_std_dev_volume_anomaly
        )

        # Calcul du Z-score
        z_scores = (volumes - rolling_mean) / rolling_std_safe

        # Détection des pics (spike) et des creux (drought) de volume
        spike_condition = z_scores > volume_zscore_threshold
        drought_condition = z_scores < -volume_zscore_threshold

        # Calcul du Momentum du Volume
        # Le momentum peut être le taux de changement du volume, ou la pente d'une régression
        # ou simplement la différence par rapport à une moyenne mobile rapide.
        # Ici, nous utilisons une simple différence par rapport à la moyenne roulante, normalisée.
        # Un momentum_window pourrait être un nouveau paramètre de configuration.
        momentum_window = 3  # Par exemple, sur les 3 dernières barres
        volume_change = volumes.diff(momentum_window).fillna(
            0
        )  # Changement de volume sur X périodes

        # Normaliser le momentum du volume entre -1 et 1 (peut être ajusté)
        # Éviter la division par zéro si le range de volume est nul
        volume_range = (
            volumes.rolling(window=momentum_window).max()
            - volumes.rolling(window=momentum_window).min()
        )

        # CORRECTION ICI: Utiliser pd.Series pour s'assurer d'avoir la méthode fillna,
        # ou np.nan_to_num() si le résultat est censé rester un ndarray.
        # Pour rester cohérent avec Pandas et pouvoir utiliser fillna(), on le convertit.
        volume_momentum = pd.Series(
            np.where(volume_range > 0, volume_change / volume_range, 0), index=df.index
        ).fillna(0)

        results = []
        for i in range(len(df)):
            info = None
            current_z_score = z_scores.iloc[i] if pd.notna(z_scores.iloc[i]) else 0
            current_volume_momentum = (
                volume_momentum.iloc[i] if pd.notna(volume_momentum.iloc[i]) else 0
            )

            if spike_condition.iloc[i]:
                info = {
                    "type": "spike",
                    "z_score": round(current_z_score, 2),
                    "volume_momentum": round(current_volume_momentum, 2),
                }
            elif drought_condition.iloc[i]:
                info = {
                    "type": "drought",
                    "z_score": round(current_z_score, 2),
                    "volume_momentum": round(current_volume_momentum, 2),
                }
            else:
                # Même sans anomalie, le momentum du volume reste une information précieuse
                if (
                    abs(current_volume_momentum) > 0.1
                ):  # Seuil pour considérer le momentum significatif
                    info = {
                        "type": "normal",
                        "z_score": round(current_z_score, 2),
                        "volume_momentum": round(current_volume_momentum, 2),
                    }

            results.append(info)

        # TODO: Implémenter des méthodes de détection d'anomalies plus avancées, comme l'algorithme
        #       "Isolation Forest" (nécessiterait Scikit-learn), pour identifier des patterns de volume inhabituels plus subtils.
        #       Cela pourrait être une fonction séparée appelée `detect_advanced_volume_patterns`.
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

   
    def analyze(self, df: pd.DataFrame) -> Optional[pd.DataFrame]:
        """
        Orchestre le pipeline d'analyse complet de manière vectorielle, performante et configurable.
        Cette méthode lit les "detection_toggles" pour n'exécuter que les analyses activées.
        Elle ne calcule et n'ajoute plus de 'confidence_score' au DataFrame.
        Intègre désormais une validation Pydantic stricte du DataFrame annoté.
        """
        self.logger.info(
            f"Démarrage du pipeline d'analyse vectoriel sur {len(df)} barres."
        )
        if df is None or df.empty:
            self.logger.error("Le DataFrame fourni à analyze() est vide ou None.")
            return None

        # --- AJOUT / AMÉLIORATION : Récupération du symbole de l'actif ---
        # Cette information est essentielle pour différencier Forex et Crypto.
        # Il est préférable que le symbole soit passé en argument à analyze()
        # si ce DataFrame peut contenir des données pour différents symboles,
        # ou s'il n'y a pas de colonne 'symbol' fiable.
        # Pour l'instant, je vais chercher le symbole dans le ConfigManager qui gère le contexte.
        # Il serait idéal que le symbole soit un argument de cette fonction, comme ceci:
        # def analyze(self, df: pd.DataFrame, asset_symbol: str) -> Optional[pd.DataFrame]:
        # Mais pour rester fidèle à la signature que tu m'as donnée, je vais essayer de le déduire.

        # Option 1: Essayer de récupérer le symbole de la dernière ligne du DF si une colonne 'symbol' existe
        asset_symbol = df.get("symbol", "").iloc[-1] if "symbol" in df.columns and not df.empty else "UNKNOWN_ASSET"
        # Option 2: Si analyze est toujours appelée dans une boucle par actif, le symbole est implicite dans le cycle.
        # Le ConfigManager est le meilleur endroit pour connaître le symbole courant.
        # self.config_manager.get_current_asset_being_processed() # Ceci est une fonction hypothétique à créer si besoin.
        
        # Pour l'exemple, nous allons temporairement utiliser un placeholder ou le premier symbole connu.
        # Dans un environnement réel, assurez-vous que `asset_symbol` est correctement défini ici.
        if asset_symbol == "UNKNOWN_ASSET":
            # Tentative de déduire à partir des logs précédents si possible, sinon on alerte.
            # En production, ce symbole devrait être passé explicitement.
            self.logger.warning("Symbole de l'actif non trouvé ou inconnu dans analyze(). La détection de liquidité pourrait être globale.")
            # Pour une démo, on pourrait prendre le premier symbole du df si l'on est sûr.
            # Ou, si `analyze` est appelée dans une boucle pour chaque actif, le symbole est géré par l'appelant.


        # ... (le code précédent reste inchangé jusqu'à la détection de liquidité) ...

        if "spread" not in df.columns:
            df["spread"] = self.config_manager.get(
                "phase_detection_defaults.default_spread_points", 0
            )
            self.logger.warning(
                "Colonne 'spread' manquante dans le DataFrame. Initialisée à la valeur par défaut."
            )
        if "point" not in df.columns:
            df["point"] = self.config_manager.get(
                "phase_detection_defaults.default_point_value", 0.00001
            )
            self.logger.warning(
                "Colonne 'point' manquante dans le DataFrame. Initialisée à la valeur par défaut."
            )

        df["spread"] = pd.to_numeric(df["spread"], errors="coerce").fillna(0)
        df["point"] = pd.to_numeric(df["point"], errors="coerce").fillna(0.00001)

        # Assurez-vous que trade_tick_size et trade_contract_size sont également disponibles pour Pydantic
        # Si elles ne sont pas dans le DataFrame initial, elles doivent être ajoutées avec des valeurs par défaut.
        if "trade_tick_size" not in df.columns:
            df["trade_tick_size"] = self.config_manager.get(
                "phase_detection_defaults.default_trade_tick_size", 0.00001
            )
            self.logger.warning(
                "Colonne 'trade_tick_size' manquante. Initialisée à la valeur par défaut."
            )
        if "trade_contract_size" not in df.columns:
            df["trade_contract_size"] = self.config_manager.get(
                "phase_detection_defaults.default_trade_contract_size", 100000.0
            )
            self.logger.warning(
                "Colonne 'trade_contract_size' manquante. Initialisée à la valeur par défaut."
            )

        df["trade_tick_size"] = pd.to_numeric(
            df["trade_tick_size"], errors="coerce"
        ).fillna(0.00001)
        df["trade_contract_size"] = pd.to_numeric(
            df["trade_contract_size"], errors="coerce"
        ).fillna(100000.0)

        df_an = self._clean_dataframe(df.copy())
        if df_an is None or df_an.empty:
            self.logger.error(
                "Le nettoyage du DataFrame a échoué ou a abouti à un DataFrame vide."
            )
            return None

        toggles = self.config_manager.get(
            "phase_detection_defaults.detection_toggles", {}
        )
        self.logger.debug(f"Utilisation des interrupteurs de détection : {toggles}")

        # --- Pipeline d'Analyse Vectoriel (Contrôlé par les Toggles) ---

        # 1. Détection primaire des signaux SMC
        if toggles.get("detect_fvg", True):
            df_an["fvg_details"] = self.detect_fvg(df_an)
        else:
            df_an["fvg_details"] = [None] * len(df_an)

        if toggles.get("detect_order_block", True):
            df_an["ob_details"] = self.detect_order_block(df_an)
        else:
            df_an["ob_details"] = [None] * len(df_an)

        if toggles.get("detect_bos_mss", True):
            df_an["bos_mss_details"] = self.detect_bos_mss(df_an)
        else:
            df_an["bos_mss_details"] = [None] * len(df_an)

        if toggles.get("detect_liquidity_grab", True):
            df_an["liquidity_grab_details"] = self.detect_liquidity_grab(df_an)
        else:
            df_an["liquidity_grab_details"] = [None] * len(df_an)

        if toggles.get("detect_volume_anomaly", True):
            df_an["volume_anomaly_details"] = self.detect_volume_anomaly(df_an)
        else:
            df_an["volume_anomaly_details"] = [None] * len(df_an)

        if toggles.get("detect_eqh_eql", True):
            df_an["eqh_eql_details"] = self.detect_eqh_eql(df_an)
        else:
            df_an["eqh_eql_details"] = [None] * len(df_an)

        df_an["fvg_detected"] = df_an["fvg_details"].apply(lambda x: x is not None)
        df_an["ob_detected"] = df_an["ob_details"].apply(lambda x: x is not None)
        df_an["bos_mss_detected"] = df_an["bos_mss_details"].apply(
            lambda x: x is not None
        )
        df_an["liquidity_grab_detected"] = df_an["liquidity_grab_details"].apply(
            lambda x: x is not None
        )
        df_an["volume_anomaly_detected"] = df_an["volume_anomaly_details"].apply(
            lambda x: x is not None
        )
        df_an["eqh_eql_detected"] = df_an["eqh_eql_details"].apply(
            lambda x: x is not None
        )

        df_an["trend"] = self._get_trend(df_an)
        df_an["volume_momentum"] = df_an["volume_anomaly_details"].apply(
            lambda x: x.get("volume_momentum", 0.0) if isinstance(x, dict) else 0.0
        )

        df_an["nearest_liquidity_level_details"] = self._get_nearest_liquidity_level(
            df_an
        )

        # --- DÉBUT DE LA LOGIQUE DE LIQUIDITÉ AMÉLIORÉE ---
        max_allowed_spread_points = self.config_manager.get(
            "phase_detection_defaults.max_allowed_spread_for_liquid_check", 7 # Valeur par défaut pour le Forex
        )
        min_volume_for_liquid_check = self.config_manager.get(
            "phase_detection_defaults.min_volume_for_liquid_check", 1
        )

        # Récupérer la liste des symboles crypto depuis prod_config.json
        crypto_symbols = self.config_manager.get("global_safety.crypto_symbols", [])

        # Déterminer si l'actif courant est une crypto et ajuster les seuils
        # L'asset_symbol doit être disponible ici. Si la colonne 'symbol' est fiable:
        current_asset_symbol = df.get("symbol", "").iloc[-1] if "symbol" in df.columns and not df.empty else "UNKNOWN_ASSET"
        
        if current_asset_symbol != "UNKNOWN_ASSET" and current_asset_symbol in crypto_symbols:
            crypto_liquidity_settings = self.config_manager.get("phase_detection_defaults.crypto_liquidity_check", {})
            # Utilise les valeurs spécifiques aux cryptos si elles existent dans la config, sinon les valeurs Forex par défaut.
            max_allowed_spread_points = crypto_liquidity_settings.get("min_allowed_spread_points_crypto", max_allowed_spread_points)
            min_volume_for_liquid_check = crypto_liquidity_settings.get("min_volume_for_liquid_check_crypto", min_volume_for_liquid_check)
            self.logger.debug(f"Détection liquidité CRYPTO pour {current_asset_symbol}: Application des seuils spécifiques. Spread Max={max_allowed_spread_points}, Volume Min={min_volume_for_liquid_check}")
        else:
            self.logger.debug(f"Détection liquidité FOREX/AUTRE pour {current_asset_symbol}: Application des seuils par défaut. Spread Max={max_allowed_spread_points}, Volume Min={min_volume_for_liquid_check}")


        if "spread" in df_an.columns and "tick_volume" in df_an.columns:
            last_spread = df_an["spread"].iloc[-1]
            last_tick_volume = df_an["tick_volume"].iloc[-1]

            is_liquid_condition = (last_spread <= max_allowed_spread_points) and (
                last_tick_volume >= min_volume_for_liquid_check
            )
            df_an["is_liquid"] = is_liquid_condition
            self.logger.debug(
                f"Détection liquidité: Spread={last_spread} (Max:{max_allowed_spread_points}), Volume={last_tick_volume} (Min:{min_volume_for_liquid_check}). Est liquide: {is_liquid_condition}"
            )
        else:
            df_an["is_liquid"] = True
            self.logger.warning(
                "Colonnes 'spread' ou 'tick_volume' manquantes pour la détection de liquidité dans PhaseObserver. 'is_liquid' par défaut à True."
            )
        # --- FIN DE LA LOGIQUE DE LIQUIDITÉ AMÉLIORÉE ---

        # 2. Détection des signaux de confirmation "chirurgicaux"
        is_bullish_fvg_tapped = (
            (
                df_an["fvg_details"]
                .shift(1)
                .apply(lambda x: isinstance(x, dict) and x.get("type") == "bullish")
            )
            & (
                df_an["low"]
                <= df_an["fvg_details"]
                .shift(1)
                .apply(lambda x: x.get("top") if isinstance(x, dict) else np.inf)
            )
            & (df_an["fvg_details"].shift(1).notna())
        )
        is_bullish_rejection_candle = df_an["close"] > df_an["open"]
        df_an["entry_confirmation_bullish"] = (
            is_bullish_fvg_tapped & is_bullish_rejection_candle
        )

        is_bearish_fvg_tapped = (
            (
                df_an["fvg_details"]
                .shift(1)
                .apply(lambda x: isinstance(x, dict) and x.get("type") == "bearish")
            )
            & (
                df_an["high"]
                >= df_an["fvg_details"]
                .shift(1)
                .apply(lambda x: x.get("bottom") if isinstance(x, dict) else -np.inf)
            )
            & (df_an["fvg_details"].shift(1).notna())
        )
        is_bearish_rejection_candle = df_an["close"] < df_an["open"]
        df_an["entry_confirmation_bearish"] = (
            is_bearish_fvg_tapped & is_bearish_rejection_candle
        )

        # 3. Validation des setups (ex: Order Block validé)
        df_an["breaker_block_details"] = self.detect_breaker_block(df_an)

        is_ob = df_an["ob_detected"].shift(1).astype(bool).fillna(False)
        ob_is_bullish = (
            df_an["ob_details"]
            .shift(1)
            .apply(
                lambda x: (
                    isinstance(x, dict) and x.get("type") == "bullish"
                    if x is not None
                    else False
                )
            )
        )
        fvg_after = df_an["fvg_detected"]

        bos_after = (
            (df_an["bos_mss_detected"] | df_an["bos_mss_detected"].shift(-1))
            .astype(bool)
            .fillna(False)
        )

        trend_aligned = ((df_an["trend"] == "bullish") & ob_is_bullish) | (
            (df_an["trend"] == "bearish") & ~ob_is_bullish
        )
        not_mitigated = ~df_an["breaker_block_details"].apply(lambda x: x is not None)
        df_an["validated_ob"] = (
            is_ob & fvg_after & bos_after & trend_aligned & not_mitigated
        )

        df_an["validated_ob_fvg"] = is_ob & fvg_after
        df_an["validated_ob_bos"] = is_ob & bos_after
        df_an["validated_ob_trend"] = is_ob & trend_aligned
        df_an["validated_ob_mitigated"] = is_ob & not_mitigated

        # 4. Détermination de la phase de marché
        volatility_threshold = getattr(
            self,
            "volatility_threshold",
            self.config_manager.get(
                "phase_detection_defaults.volatility_threshold", 0.0005
            ),
        )
        scalp_burst_volatility_multiplier = getattr(
            self,
            "scalp_burst_volatility_multiplier",
            self.config_manager.get(
                "phase_detection_defaults.scalp_burst_volatility_multiplier", 0.5
            ),
        )
        consolidation_volatility_multiplier = getattr(
            self,
            "consolidation_volatility_multiplier",
            self.config_manager.get(
                "phase_detection_defaults.consolidation_volatility_multiplier", 2.0
            ),
        )

        conditions = [
            df_an["liquidity_grab_detected"],
            df_an["validated_ob"],
            (df_an["bos_mss_detected"])
            & (
                df_an["volume_anomaly_details"].apply(
                    lambda x: (
                        isinstance(x, dict) and x.get("type") == "spike"
                        if x is not None
                        else False
                    )
                )
            ),
            (df_an["trend"] == "bullish"),
            (df_an["trend"] == "bearish"),
            (
                (
                    df_an["close"].diff().abs()
                    > volatility_threshold * scalp_burst_volatility_multiplier
                )
                .rolling(window=2)
                .min()
                .fillna(False)
            ).astype(bool),
            (
                (
                    df_an["high"].rolling(window=self.lookback_window).max()
                    - df_an["low"].rolling(window=self.lookback_window).min()
                )
                < (volatility_threshold * consolidation_volatility_multiplier)
            )
            .fillna(False)
            .astype(bool),
            (
                (
                    df_an["high"].rolling(window=self.lookback_window).max()
                    - df_an["low"].rolling(window=self.lookback_window).min()
                )
                >= (volatility_threshold * consolidation_volatility_multiplier)
            )
            .fillna(False)
            .astype(bool),
        ]
        outcomes = [
            "manipulation",
            "institutional_setup",
            "expansion",
            "trending_bullish",
            "trending_bearish",
            "scalp_burst",
            "consolidation",
            "range",
        ]
        df_an["phase"] = np.select(conditions, outcomes, default="micro_phase")
        df_an["phase"] = df_an.apply(self._refine_phase_direction, axis=1)

        # 5. Suppression du Calcul du Score de Confiance (Déjà fait)
        # Toutes les lignes liées au calcul et à l'affectation de 'confidence_score' ont été supprimées.

        # --- NOUVEAU : Validation Pydantic du DataFrame annoté ---
        # Préparer le DataFrame pour la validation Pydantic (lignes sous forme de dictionnaire)
        validated_rows_data = []
        for index, row_series in df_an.iterrows():
            try:
                row_dict = row_series.to_dict()
                # --- CORRECTION ICI : Gérer explicitement les NaN pour les champs Optional[Dict] ---
                # Pydantic s'attend à None si le dictionnaire est absent, pas à NaN (float).
                for detail_col in [
                    "fvg_details",
                    "ob_details",
                    "bos_mss_details",
                    "liquidity_grab_details",
                    "volume_anomaly_details",
                    "eqh_eql_details",
                    "nearest_liquidity_level_details",
                ]:
                    if detail_col in row_dict and pd.isna(row_dict[detail_col]):
                        row_dict[detail_col] = None
                # --- FIN CORRECTION ---

                # Assurez-vous que 'timestamp' est bien une chaîne ISO pour Pydantic
                if hasattr(index, "isoformat"):
                    row_dict["timestamp"] = index.isoformat()
                else:
                    # Fallback si l'index n'est pas un Timestamp (improbable après _clean_dataframe)
                    row_dict["timestamp"] = datetime.now(UTC).isoformat()

                # Valider la ligne avec le modèle Pydantic
                validated_row = PhaseObserverRowModel(**row_dict)
                # Si la validation réussit, nous n'avons pas besoin de reconstruire le DF
                # La liste validated_rows_data n'est pas utilisée après la boucle, c'est juste pour le processus de validation
                validated_rows_data.append(validated_row.model_dump())

            except ValidationError as e:
                self.logger.critical(
                    f"ERREUR CRITIQUE Pydantic: Validation du DataFrame annoté échouée pour la ligne {index}: {e}",
                    exc_info=True,
                )
                # --- CORRECTION ICI : Appel correct à send_alert ---
                self.config_manager.send_alert(
                    f"PhaseObserver: Validation données échouée pour {index}. Erreur: {e}",
                    "telegram_critical",
                )
                # --- FIN CORRECTION ---
                return None  # Bloque le pipeline si une donnée cruciale est invalide.
            except Exception as e:
                self.logger.critical(
                    f"ERREUR CRITIQUE: Erreur inattendue lors de la validation Pydantic de la ligne {index}: {e}",
                    exc_info=True,
                )
                # --- CORRECTION ICI : Appel correct à send_alert ---
                self.config_manager.send_alert(
                    f"PhaseObserver: Erreur validation inattendue pour {index}. Erreur: {e}",
                    "telegram_critical",
                )
                # --- FIN CORRECTION ---
                return None
        # --- FIN NOUVEAU ---

        self.logger.info(
            "Pipeline d'analyse terminé avec succès et DataFrame validé par schéma Pydantic."
        )

        # Suppression des colonnes '_details' sauf 'volume_anomaly_details' si c'est le comportement désiré
        columns_to_drop = [
            col
            for col in df_an.columns
            if col.endswith("_details") and not col.startswith("volume_anomaly_")
        ]
        df_an.drop(columns=columns_to_drop, errors="ignore", inplace=True)

        # Log final : Indiquer la dernière phase SANS la confiance (déjà corrigé)
        if not df_an.empty:
            last_row = df_an.iloc[-1]
            last_time = (
                last_row.name.isoformat()
                if hasattr(last_row.name, "isoformat")
                else "N/A"
            )
            self.logger.debug(
                f"PhaseObserver.analyze() a terminé. Dernière barre ({last_time}): Phase={last_row.get('phase', 'N/A')}. Total barres analysées: {len(df_an)}."
            )
        else:
            self.logger.debug(
                "PhaseObserver.analyze() a terminé, mais le DataFrame analysé est vide."
            )

        return df_an

    def _refine_phase_direction(self, row: pd.Series) -> str:
        """
        Affine la phase de marché en ajoutant une direction (up/down) si applicable.
        Utilisée après la détermination initiale de la phase par np.select.

        Args:
            row (pd.Series): Une ligne du DataFrame annoté par PhaseObserver.

        Returns:
            str: La phase de marché affinée.
        """
        phase = row.get("phase", "unknown")
        trend = row.get("trend", "neutral")

        # Si la phase est "expansion", "consolidation", "range", ou "manipulation",
        # on peut y ajouter la direction de la tendance pour plus de granularité.
        if phase in ["expansion", "consolidation", "range", "manipulation"]:
            if trend == "bullish":
                return f"{phase}_up"
            elif trend == "bearish":
                return f"{phase}_down"

        # Pour les phases déjà directionnelles (trending_bullish/bearish),
        # ou les phases qui n'ont pas de direction (micro_phase, institutional_setup),
        # on retourne la phase telle quelle.
        return phase

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
            self.logger.error(f"Erreur de lecture du fichier de configuration {config_filepath}: {e}", exc_info=True)
            return

        # --- CORRECTION MAJEURE : Charger le journal d'audit centralisé UNE SEULE FOIS ---
        audit_trail_path_str = self.config_manager.get("paths.ai_history_log") # Utilise la clé de prod_config.json
        all_trades_df = None
        if audit_trail_path_str:
            audit_trail_path = Path(audit_trail_path_str)
            if audit_trail_path.exists():
                try:
                    all_trades_df = self.load_data(audit_trail_path)
                except Exception as e:
                    self.logger.error(f"Impossible de charger le journal d'audit depuis {audit_trail_path}: {e}", exc_info=True)
            else:
                self.logger.warning(f"Le fichier d'audit '{audit_trail_path}' est introuvable.")
        else:
            self.logger.error("Le chemin vers 'paths.ai_history_log' n'est pas défini dans la configuration.")

        if all_trades_df is None or all_trades_df.empty:
            self.logger.warning("Journal d'audit vide ou non chargé. L'analyse historique des actifs est impossible.")
            return

        self.multi_asset_report_data = []  # Réinitialiser pour chaque exécution

        # --- CORRECTION : Itérer sur les actifs et filtrer le DataFrame principal ---
        symbol_column_name = "symbol" # Nom de la colonne contenant les symboles dans le log

        for asset in tradeable_assets:
            self.logger.info(f"Analyse des données pour l'actif '{asset}' depuis le journal d'audit centralisé.")
            
            if symbol_column_name not in all_trades_df.columns:
                self.logger.error(f"Colonne '{symbol_column_name}' introuvable dans le journal d'audit. Impossible de filtrer.")
                break

            df_raw_for_asset = all_trades_df[all_trades_df[symbol_column_name] == asset].copy()

            if df_raw_for_asset.empty:
                self.logger.warning(f"Aucune donnée historique trouvée pour l'actif '{asset}' dans le journal d'audit.")
                continue

            annotated_df = self.analyze(df_raw_for_asset)

            if annotated_df is not None and not annotated_df.empty:
                dominant_phase = annotated_df["phase"].mode()[0] if not annotated_df["phase"].empty else "N/A"
                total_volume = annotated_df["volume"].sum() if "volume" in annotated_df.columns else 0

                self.multi_asset_report_data.append({
                    "timestamp": pd.Timestamp.now(UTC).isoformat(),
                    "asset": asset,
                    "dominant_phase": dominant_phase,
                    "total_volume": round(total_volume, 2),
                    "log_entries": len(annotated_df),
                    "fvg_detected_count": int(annotated_df["fvg_detected"].sum()),
                    "liquidity_grab_detected_count": int(annotated_df["liquidity_grab_detected"].sum()),
                    "bos_mss_detected_count": int(annotated_df["bos_mss_detected"].sum()),
                    "validated_ob_count": int(annotated_df["validated_ob"].sum()),
                })
                self.log_audit_event("ASSET_PROCESSED", f"Analyse de {len(annotated_df)} entrées de log.", asset=asset)
            else:
                self.log_audit_event("PROCESSING_ERROR", "L'analyse du DataFrame a échoué ou a retourné un résultat vide.", asset=asset)

        self.log_audit_event("MULTI_ASSET_SCAN_COMPLETE", f"Analyse de {len(tradeable_assets)} actifs terminée.")

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
                    self.log_audit_event("UNLISTED_ASSET_DISCOVERED", f"L'actif '{asset}' existe dans l'historique mais n'est pas dans la stratégie active.", asset=asset)
        except Exception as e:
            self.logger.error(f"Erreur lors de la découverte d'actifs non listés : {e}", exc_info=True)
            
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
