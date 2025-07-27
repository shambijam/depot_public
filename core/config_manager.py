# core/config_manager.py

import logging
import json
import sys
import yaml
import numpy as np
import importlib.util
import inspect
import jsonschema
import os
import pandas as pd
import re
import uuid
import requests
import time
from collections import deque
from pathlib import Path
from datetime import datetime, UTC, timedelta
from typing import Dict, Any, List, Optional, Tuple, TYPE_CHECKING # TYPE_CHECKING est déjà là
from functools import lru_cache
from core.config_loader import ConfigLoader
from core.ai_interface import AIInterface
from core.audit_logger import AuditLogger
from core.strategy_manager import StrategyManager
from core.decision_pipeline import DecisionPipeline
from core.utils import CustomJSONEncoder, get_diff, ConfigValidationError, TradeStatus # NOUVEL IMPORT DEPUIS UTILS

# Utilisation de TYPE_CHECKING pour éviter les importations circulaires à l'exécution
if TYPE_CHECKING:
    from core.config_manager import ConfigManager # Importation uniquement pour les hints de type

logger = logging.getLogger(__name__)
class ConfigManager:
    """
    Module Singleton central pour la gestion de la configuration et l'orchestration de SNIPER_X.
    Il orchestre les interactions entre les modules spécialisés de gestion de la configuration.
    """
    _instance: Optional["ConfigManager"] = None
    _initialized: bool = False

    def __new__(cls, *args, **kwargs):
        """Implémente le pattern Singleton."""
        if cls._instance is None:
            cls._instance = super(ConfigManager, cls).__new__(cls)
        return cls._instance

    def __init__(self):
        """
        Initialise le ConfigManager lors de sa toute première création.
        Cette méthode est protégée pour n'être exécutée qu'une seule fois et met
        en place les attributs fondamentaux et l'état initial du Singleton.
        """
        if self._initialized:
            return

        self.logger = logging.getLogger(__name__)

        # Initialisation des attributs de l'état de session qui ne sont pas gérés par les sous-modules
        self._reset_session_state()

        # Instanciation des dépendances. ConfigManager les orchestre.
        self.config_loader = ConfigLoader(config_manager_instance=self)
        self.audit_logger = AuditLogger(config_manager_instance=self)
        self.ai_interface = AIInterface(config_manager_instance=self) # AIDecision sera injecté plus tard
        self.strategy_manager = StrategyManager(config_loader_instance=self.config_loader, config_manager_instance=self)
        self.decision_pipeline = DecisionPipeline(config_manager_instance=self, ai_interface_instance=self.ai_interface, strategy_manager_instance=self.strategy_manager)


        # Chargement des configurations de base immuables (variables d'environnement et comptes brokers)
        base_configs = self.config_loader.load_base_configs()
        self._config = base_configs
        self._broker_accounts_config = base_configs.get("_broker_accounts_config", {"accounts": []})

        # --- Définir le niveau du logger du ConfigManager (sera affiné après initialize_dynamic_config) ---
        log_level_str = self.get("log_level", "INFO").upper()
        self.logger.setLevel(getattr(logging, log_level_str, logging.INFO))

        # L'instance réelle d'AIDecision sera injectée par main.py
        self.ai_decision_instance: Optional[Any] = None

        self._initialized = True
        self.logger.info("ConfigManager initialisé avec succès (Singleton).")


    def _reset_session_state(self) -> None:
        """
        Réinitialise l'état de la session de configuration à ses valeurs par défaut.
        """
        self.logger.debug("Réinitialisation de l'état de la session du ConfigManager.")
        self._dynamic_config_path: Optional[str] = None
        self._dynamic_config: Dict[str, Any] = {}
        self._last_market_regime: Optional[str] = None
        self._asset_blacklist: Dict[str, datetime] = {}
        self._notification_queue: deque = deque()
        self._last_summary_sent_time: datetime = datetime.now(UTC)
        self._daily_trade_count: int = 0
        self._trade_summary_data: Dict[str, Any] = {
            "trades_confirmed": [],
            "trades_closed": [],
            "last_market_phase": None,
            "bot_status_changes": [],
            "critical_events": [],
        }
        # Les caches sont maintenant gérés par les modules dédiés.


    def get_mt5_account_credentials(
        self, account_id: Optional[str] = None, mode: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Récupère les identifiants de connexion et les détails d'un compte MT5 depuis
        `broker_accounts.json` et les variables d'environnement.
        """
        self.logger.info(
            f"Tentative de récupération des identifiants MT5 pour account_id='{account_id}', mode='{mode}'."
        )

        if not self._broker_accounts_config or not self._broker_accounts_config.get(
            "accounts"
        ):
            self.logger.critical(
                "Aucune configuration de comptes brokers chargée. Vérifiez 'broker_accounts.json'. Impossible de récupérer les identifiants MT5."
            )
            raise RuntimeError("Aucune configuration de comptes brokers chargée.")

        selected_account = None
        if account_id:
            selected_account = next(
                (
                    acc
                    for acc in self._broker_accounts_config["accounts"]
                    if acc.get("account_id") == account_id
                ),
                None,
            )
            if selected_account is None:
                self.logger.error(
                    f"Compte MT5 avec account_id '{account_id}' non trouvé dans broker_accounts.json."
                )
                raise ValueError(
                    f"Compte MT5 avec account_id '{account_id}' non trouvé."
                )
        elif mode:
            default_account_id_key = f"default_{mode.lower()}_account"
            default_account_id = self._broker_accounts_config.get(
                default_account_id_key
            )

            if default_account_id:
                selected_account = next(
                    (
                        acc
                        for acc in self._broker_accounts_config["accounts"]
                        if acc.get("account_id") == default_account_id
                    ),
                    None,
                )
                if selected_account is None:
                    self.logger.error(
                        f"Compte MT5 par défaut '{default_account_id}' pour le mode '{mode}' non trouvé dans broker_accounts.json. Veuillez vérifier sa définition."
                    )
                    raise ValueError(
                        f"Compte MT5 par défaut pour le mode '{mode}' non trouvé ou inactif : '{default_account_id}'."
                    )
            else:
                self.logger.error(
                    f"Aucun compte MT5 par défaut défini pour le mode '{mode}' dans broker_accounts.json (clé '{default_account_id_key}' manquante)."
                )
                raise ValueError(
                    f"Aucun compte MT5 par défaut défini pour le mode '{mode}'."
                )
        else:
            self.logger.error(
                "Aucun account_id ni mode fourni. Impossible de récupérer les identifiants MT5."
            )
            raise ValueError(
                "Aucun account_id ni mode fourni pour récupérer les identifiants MT5."
            )

        if not selected_account.get("is_active", False):
            self.logger.warning(
                f"Le compte MT5 '{selected_account.get('account_id')}' n'est pas marqué comme actif. Impossible de l'utiliser."
            )
            return None

        login_env_var = selected_account.get("login_env_var")
        password_env_var = selected_account.get("password_env_var")
        server_type = selected_account.get("server_type")

        if not all([login_env_var, password_env_var, server_type]):
            self.logger.critical(
                f"Informations de connexion incomplètes pour le compte '{selected_account.get('account_id')}'. Les clés 'login_env_var', 'password_env_var' ou 'server_type' sont manquantes dans broker_accounts.json. Impossible de procéder."
            )
            raise RuntimeError(
                f"Configuration du compte broker '{selected_account.get('account_id')}' invalide."
            )

        login_value = self._config.get("env_vars", {}).get(login_env_var)
        password_value = self._config.get("env_vars", {}).get(password_env_var)

        if not login_value or not password_value:
            self.logger.critical(
                f"Variables d'environnement de login/password manquantes pour le compte '{selected_account.get('account_id')}': '{login_env_var}' ou '{password_env_var}'. Vérifiez votre fichier .env. Impossible de se connecter."
            )
            raise RuntimeError(
                f"Identifiants MT5 manquants dans les variables d'environnement pour le compte '{selected_account.get('account_id')}'."
            )

        credentials = {
            "account_id": selected_account.get("account_id"),
            "login": int(login_value),
            "password": password_value,
            "server": server_type,
            "mode": selected_account.get("mode"),
            "broker_name": selected_account.get("broker_name"),
            "allowed_symbols": selected_account.get("allowed_symbols", []),
            "account_type": selected_account.get("account_type"),
            "trade_settings": selected_account.get("trade_settings", {}),
        }
        self.logger.info(
            f"Identifiants MT5 récupérés avec succès pour le compte '{credentials['account_id']}'."
        )
        return credentials

    def get_current_dynamic_config(self) -> Dict[str, Any]:
        """
        Retourne une copie de la configuration dynamique actuellement chargée.
        """
        if not self._dynamic_config:
            self.logger.warning(
                "La configuration dynamique n'est pas chargée. "
                "Veuillez l'initialiser ou la charger au préalable."
            )
        return self._dynamic_config.copy()

    def get(self, key: str, default: Any = None) -> Any:
        """
        Récupère une valeur de configuration depuis la configuration dynamique chargée.
        Prend en charge les clés imbriquées en utilisant une notation par points.
        """
        keys = key.split(".")
        current_level = self._dynamic_config
        for k in keys:
            if isinstance(current_level, dict) and k in current_level:
                current_level = current_level[k]
            else:
                self.logger.debug(
                    f"Clé de configuration '{key}' non trouvée. Retour de la valeur par défaut."
                )
                return default

        if isinstance(current_level, dict) and "value" in current_level:
            return current_level["value"]
        return current_level


    def initialize_dynamic_config(
        self, template_path: str, output_path: str, config_dir: str
    ) -> None:
        """
        Initialise la configuration dynamique en chargeant et fusionnant le fichier principal,
        les configurations modulaires (PhaseObserver, Telegram), et la stratégie par défaut.
        Délègue le chargement et la validation à ConfigLoader.
        """
        self.logger.info(f"Initialisation de la configuration dynamique depuis '{template_path}'...")
        if not Path(template_path).exists():
            raise FileNotFoundError(f"Fichier de configuration de base introuvable: {template_path}")

        # Liste des noms de schémas qui sont censés exister dans config/schemas/
        # et pour lesquels la validation sera tentée.
        # Basé sur votre arborescence, seul strategy_schema.json y est.
        # Les autres schémas ne sont pas présents dans config/schemas/ et ne seront donc pas passés explicitement.
        existing_schemas_in_schemas_dir = {
            "strategy_schema.json" # Seul celui-ci est dans config/schemas/ selon votre arborescence.
        }
        
        try:
            # Pour prod_config.json, nous ne spécifions PAS de schema_name
            # car main_app_schema.json n'existe pas dans config/schemas/.
            # ConfigLoader.validate_config gérera cela en loguant un avertissement.
            self.logger.info(f"Chargement de {template_path} sans validation de schéma explicite (schéma main_app_schema.json non trouvé).")
            base_config = self.config_loader.load_dynamic_config(template_path) # Appel SANS schema_name
                
            self._dynamic_config = base_config
            self._dynamic_config_path = output_path
            self.logger.debug(f"DEBUG_INIT_CONFIG_1: _dynamic_config après chargement base_config (prod_config): {self._dynamic_config.get('strategies', {}).get('default_strategy', 'N/A')} - has strategy_name: {'strategy_name' in self._dynamic_config}")
        except Exception as e:
            self.logger.critical(f"Échec du chargement du fichier de base '{template_path}': {e}", exc_info=True)
            raise

        configs_to_load = {
            "paths.phase_observer_config": "phase_observer_config_schema.json", # Le schéma n'est pas dans config/schemas/
            "paths.telegram_config": "telegram_config_schema.json" # Le schéma n'est pas dans config/schemas/
        }
        for config_key, schema_file_name in configs_to_load.items():
            config_file_path_str = self.get(config_key)
            if config_file_path_str:
                config_file_path = Path(config_file_path_str)
                if config_file_path.exists():
                    try:
                        # Pour phase_observer_config.json et telegram_config.json,
                        # nous ne spécifions PAS de schema_name.
                        # ConfigLoader.validate_config gérera cela en loguant un avertissement.
                        self.logger.info(f"Chargement de {config_file_path.name} sans validation de schéma explicite (schéma {schema_file_name} non trouvé).")
                        supplemental_config = self.config_loader.load_dynamic_config(str(config_file_path)) # Appel SANS schema_name
                            
                        self._dynamic_config = self._merge_dicts(self._dynamic_config, supplemental_config)
                        self.logger.info(f"Configuration modulaire '{config_file_path.name}' chargée et fusionnée.")
                    except Exception as e:
                        self.logger.error(f"Erreur lors du chargement ou de la fusion de '{config_file_path.name}': {e}", exc_info=True)
                else:
                    self.logger.warning(f"Fichier de configuration modulaire non trouvé : '{config_file_path_str}'.")

        if self.get("app.save_on_initial_load", False):
            self.audit_logger.save_dynamic_config(
                self._dynamic_config,
                output_path,
                backup=self.get("app.backup_on_initial_save", False),
            )
        else:
            self.logger.info("La sauvegarde au chargement initial est désactivée.")

        # Les stratégies sont maintenant chargées par StrategyManager lors de son initialisation (dans __init__).

        self.logger.debug(f"DEBUG_INIT_CONFIG_3: _dynamic_config après chargement des modules de config: has strategy_name: {'strategy_name' in self._dynamic_config}, strategy_name: {self._dynamic_config.get('strategy_name', 'N/A')}")

        log_level_str_final = self.get("log_level", "INFO").upper()
        self.logger.setLevel(getattr(logging, log_level_str_final, logging.INFO))
        self.logger.info("Configuration dynamique entièrement initialisée et fusionnée.")

    def _merge_dicts(self, base_dict: Dict[str, Any], new_dict: Dict[str, Any]) -> Dict[str, Any]:
        """
        Fusionne récursivement deux dictionnaires (deep merge).
        Les valeurs de `new_dict` écrasent celles de `base_dict`. Si une clé
        contient un dictionnaire dans les deux, ils sont fusionnés récursivement.
        """
        for k, v in new_dict.items():
            self.logger.debug(f"DEBUG_MERGE: Fusion de clé '{k}'. Dans base_dict: {k in base_dict}, Type base: {type(base_dict.get(k))}, Type nouveau: {type(v)}")
            if k in base_dict and isinstance(base_dict[k], dict) and isinstance(v, dict):
                base_dict[k] = self._merge_dicts(base_dict[k], v)
            else:
                base_dict[k] = v
        self.logger.debug(f"DEBUG_MERGE: Fusion terminée. Resultat base_dict has strategy_name: {'strategy_name' in base_dict}, strategy_name: {base_dict.get('strategy_name', 'N/A')}")
        return base_dict

    def update_dynamic_config(self, updates: Dict[str, Any], source: str = "bot_ai") -> None:
        """
        Met à jour la configuration dynamique en mémoire avec de nouvelles valeurs via une fusion profonde,
        valide le résultat, puis le sauvegarde de manière atomique.
        """
        target_path = self._dynamic_config_path
        if not target_path:
            raise ValueError("Le chemin de la configuration dynamique n'est pas défini. Impossible de mettre à jour.")

        old_config = self.get_current_dynamic_config()

        new_config = self._merge_dicts(old_config, updates)

        try:
            self.config_loader.validate_config(new_config, schema_name="main_app_schema.json")
            self._dynamic_config = new_config

            backup_on_update = self.get("app.backup_on_update", False)
            self.audit_logger.save_dynamic_config(
                self._dynamic_config,
                target_path,
                backup=backup_on_update
            )

            change_info = get_diff(old_config, new_config) # Utilise get_diff de core.utils
            self.audit_logger.log_config_change(
                {"action": "update", "updates": change_info},
                source=source,
                dynamic_config_snapshot=self._dynamic_config.copy()
            )
            self.logger.info(f"Configuration dynamique mise à jour avec succès par '{source}'.")
        except ConfigValidationError as e:
            self.logger.warning(f"La mise à jour de la configuration a échoué à la validation : {e}. Les changements ne sont pas appliqués.")
            raise
        except Exception as e:
            self.logger.error(f"Erreur lors de la mise à jour de la configuration dynamique : {e}", exc_info=True)
            raise


    # --- Section 4: Arbitrage & Décision Institutionnelle ---

    def analyze_context(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Analyse le contexte de marché et système pour enrichir la prise de décision.
        Cette fonction agrège des informations de diverses sources pour fournir une
        vue complète et actionable pour les modules de décision.
        """
        self.logger.info("Analyse du contexte en cours...")
        analyzed_context = context.copy()
        current_time_utc = datetime.now(UTC)
        current_hour_utc = current_time_utc.hour
        current_weekday = current_time_utc.weekday()

        start_hour = self.get("bot_behavior.trading_start_hour_utc", 8)
        end_hour = self.get("bot_behavior.trading_end_hour_utc", 17)
        allowed_weekdays = self.get("bot_behavior.allowed_weekdays", [0, 1, 2, 3, 4])
        default_vix = self.get("market_regime_detection.default_vix_index", 20)

        analyzed_context["is_trading_hours"] = start_hour <= current_hour_utc < end_hour
        analyzed_context["is_trading_day"] = current_weekday in allowed_weekdays
        analyzed_context["is_market_open"] = analyzed_context["is_trading_hours"] and analyzed_context["is_trading_day"]

        analyzed_context["market_volatility_index"] = context.get("vix_index", default_vix)

        current_mt5_login_numeric = context.get("account_info", {}).get("login")
        active_broker_account_details = None

        if current_mt5_login_numeric:
            try:
                for account in self._broker_accounts_config.get("accounts", []):
                    login_value_from_env = self._config.get("env_vars", {}).get(account.get("login_env_var"))
                    if login_value_from_env and int(login_value_from_env) == current_mt5_login_numeric:
                        active_broker_account_details = self.get_mt5_account_credentials(
                            account_id=account.get("account_id")
                        )
                        break

                if active_broker_account_details:
                    analyzed_context["active_broker_account"] = active_broker_account_details
                    self.logger.debug(f"Contexte enrichi avec les détails du compte broker actif : {active_broker_account_details.get('account_id')}.")
                else:
                    self.logger.warning(f"Détails du compte broker (Login MT5: {current_mt5_login_numeric}) introuvables ou inactifs dans 'broker_accounts.json'. Contexte non enrichi avec ces détails.")
            except (ValueError, RuntimeError) as e:
                self.logger.error(f"Erreur lors de la récupération des détails du compte broker actif (Login MT5: {current_mt5_login_numeric}): {e}", exc_info=True)
                analyzed_context["active_broker_account"] = {"error": str(e)}
        else:
            self.logger.debug("Aucun ID de compte MT5 actif dans le contexte pour récupérer les détails du compte broker.")

        all_assets_market_data_from_context = context.get("market_data", {})
        analyzed_context["current_market_regime"] = self.detect_market_regime(
            analyzed_context, all_assets_market_data_from_context
        )


        self.logger.debug(f"Analyse du contexte terminée.")
        return analyzed_context

    def detect_market_regime(self, context: Dict[str, Any], data: Dict[str, Any]) -> str:
        """
        Détecte le régime de marché actuel en agrégeant les signaux de tous les actifs pertinents
        pour obtenir une vue consensuelle et robuste de l'état du marché.
        """
        self.logger.info("Détection du régime de marché par consensus...")

        min_data_points = self.get("market_regime_detection.min_data_points", 20)
        asset_insights = []

        if not data:
            self.logger.warning("Le dictionnaire de données est vide. Régime de marché indéterminé.")
            return "uncertain_no_data_input"

        for asset_symbol, asset_data_dict in data.items():
            if isinstance(asset_data_dict, dict) and "annotated_rates_df" in asset_data_dict:
                df = asset_data_dict.get("annotated_rates_df")
                if isinstance(df, pd.DataFrame) and not df.empty and len(df) >= min_data_points:
                    last_row = df.iloc[-1]
                    asset_insights.append({
                        "symbol": asset_symbol,
                        "phase": last_row.get("phase", "neutral"),
                        "confidence": last_row.get("confidence_score", 0.0), # Garder la confiance ici pour l'analyse du régime
                        "trend": last_row.get("trend", "neutral"),
                        "volume_momentum": last_row.get("volume_momentum", 0.0),
                        "bos_mss_detected": last_row.get("bos_mss_detected", False),
                        "df": df, # Garder le DataFrame pour le calcul de volatilité
                    })

        if not asset_insights:
            self.logger.warning("Aucun actif avec des données suffisantes pour déterminer le régime de marché.")
            return "uncertain_no_valid_data"

        try:
            phase_votes = [insight["phase"].split("_")[0] for insight in asset_insights]
            if not phase_votes:
                return "uncertain_calculation_failed"

            dominant_phase = max(set(phase_votes), key=phase_votes.count)

            avg_confidence = np.mean([insight["confidence"] for insight in asset_insights])

            volatility_percentages = []
            atr_period = self.get("market_regime_detection.atr_period", 14)
            for insight in asset_insights:
                df = insight["df"]
                if len(df) >= atr_period:
                    tr = pd.DataFrame({
                        "tr1": df["high"] - df["low"],
                        "tr2": abs(df["high"] - df["close"].shift(1)),
                        "tr3": abs(df["low"] - df["close"].shift(1)),
                    }).max(axis=1)
                    atr = tr.rolling(window=atr_period).mean().iloc[-1]
                    current_price = df["close"].iloc[-1]
                    if current_price > 0:
                        volatility_percentages.append((atr / current_price) * 100)

            avg_volatility_percent = np.mean(volatility_percentages) if volatility_percentages else 0.0

            high_vol_threshold = self.get("market_regime_detection.high_volatility_percent_threshold", 0.5)
            low_vol_threshold = self.get("market_regime_detection.low_volatility_percent_threshold", 0.1)

            if avg_volatility_percent > high_vol_threshold:
                volatility_level = "high_volatility"
            elif avg_volatility_percent < low_vol_threshold:
                volatility_level = "low_volatility"
            else:
                volatility_level = "normal_volatility"

            detected_regime = f"{dominant_phase}_{volatility_level}"

            self.logger.info(f"Régime de marché par consensus : {detected_regime} (basé sur {len(asset_insights)} actifs, Volatilité moyenne: {avg_volatility_percent:.2f}%)")

            if self._last_market_regime and self._last_market_regime != detected_regime and self.get("telegram.channels.telegram_market_phase", False):
                message = (
                    f"🚨 **Changement de Régime de Marché**\n"
                    f"Ancien: `{self._last_market_regime}`\n"
                    f"Nouveau: `{detected_regime}`"
                )
                self.send_alert(message, "telegram_market_phase")

            self._last_market_regime = detected_regime
            return detected_regime

        except Exception as e:
            self.logger.critical(f"ERREUR CRITIQUE lors du calcul du régime de marché par consensus: {e}", exc_info=True)
            self.send_alert(f"CRITIQUE: Échec calcul régime: {e}", "telegram_critical")
            self._last_market_regime = "uncertain_calculation_failed"
            return "uncertain_calculation_failed"


    def calculate_risk_parameters(
        self,
        context: Dict[str, Any],
        config: Dict[str, Any],
        trade_decision: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Calcule les paramètres de risque dynamiques (taille de lot, etc.) pour un trade.
        Cette fonction est cruciale pour la gestion du risque institutionnelle, en s'assurant
        que chaque trade respecte les limites définies et les spécifications du broker.
        """
        self.logger.info(f"Calcul des paramètres de risque pour {trade_decision.get('asset')}...")

        equity = context.get("account_info", {}).get("equity", self.get("risk_management_settings.default_account_equity", 10000.0))
        if equity <= 0:
            self.logger.error(f"Équité du compte ({equity}) non positive. Impossible de calculer le risque.")
            return {}

        risk_per_trade_percent = config.get("risk_per_trade_percent", 1.0)
        if not (0 < risk_per_trade_percent <= 100):
            self.logger.error(f"Pourcentage de risque par trade invalide ({risk_per_trade_percent}%).")
            return {}
        max_dollar_risk = equity * (risk_per_trade_percent / 100)

        asset = trade_decision.get("asset", "UNKNOWN_ASSET")

        active_broker_account = context.get("active_broker_account", {})
        account_trade_settings = active_broker_account.get("trade_settings", {})

        asset_mt5_info = context.get("market_data", {}).get(asset, {}).get("symbol_info", {})

        point = asset_mt5_info.get("point", self.get("risk_management_settings.default_points_in_pip", 0.00001))
        contract_size = asset_mt5_info.get("trade_contract_size", self.get("risk_management_settings.default_contract_size", 100000))

        if point <= 0 or contract_size <= 0:
            self.logger.error(f"Informations cruciales du symbole manquantes ou invalides (point={point}, contract_size={contract_size}) pour {asset}. Impossible de calculer le risque.")
            return {}

        target_sl_pips = trade_decision.get("target_sl_pips")
        if target_sl_pips is None or target_sl_pips <= 0:
            self.logger.error(f"Stop loss invalide ou nul ({target_sl_pips} pips) pour {asset}. Impossible de calculer le volume. Ordre bloqué pour sécurité.")
            return {"volume": 0.0, "max_dollar_risk": 0.0,}

        sl_distance_in_price = target_sl_pips * point
        dollar_risk_per_lot_estimated = sl_distance_in_price * contract_size

        if dollar_risk_per_lot_estimated <= 0:
            self.logger.warning(f"Le risque par lot estimé est nul ou négatif pour {asset}. Utilisation du volume minimum pour cette estimation.")
            dollar_risk_per_lot_estimated = self.get("risk_management_settings.min_dollar_risk_per_lot_fallback", 1.0)

        calculated_lot_size = max_dollar_risk / dollar_risk_per_lot_estimated

        min_lot_size = account_trade_settings.get("min_lot", self.get("risk_management_settings.min_lot_size_fallback", 0.01))
        max_lot_size = account_trade_settings.get("max_lot", self.get("global_safety.max_allowed_lot_size", 50.0))
        lot_step = account_trade_settings.get("lot_step", self.get("risk_management_settings.default_lot_step_fallback", 0.01))

        if lot_step <= 0:
            self.logger.error(f"Lot step invalide ou nul ({lot_step}) pour {asset}. Utilisation du fallback 0.01.")
            lot_step = 0.01

        volume = max(min_lot_size, calculated_lot_size)
        volume = min(max_lot_size, volume)

        volume = round(volume / lot_step) * lot_step

        lot_size_precision = (len(str(lot_step).split(".")[-1]) if "." in str(lot_step) else 0)
        final_volume = round(volume, lot_size_precision)

        self.logger.info(f"Calcul de risque pour {asset}: Equity=${equity:.2f}, Risque={risk_per_trade_percent}%, Max Dollar Risque=${max_dollar_risk:.2f}, Volume Final={final_volume:.{lot_size_precision}f} (Risque Estimé par Lot=${dollar_risk_per_lot_estimated:.2f}).")

        return {"volume": final_volume, "max_dollar_risk": max_dollar_risk, "risk_per_trade_percent": risk_per_trade_percent,}


    def check_news_schedule(self, context: Dict[str, Any], calendar_data: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """
        Vérifie le calendrier économique pour les événements à fort impact à venir.
        Utilise des datetimes conscients du fuseau horaire (UTC) pour une robustesse maximale.
        """
        self.logger.info("Vérification du calendrier des annonces économiques...")
        current_time_utc = datetime.now(UTC)
        upcoming_news = None
        highest_impact = -1

        window_before_event_minutes = self.get("news_settings.detection_window.before_event_minutes", 15)
        window_after_event_minutes = self.get("news_settings.detection_window.after_event_minutes", 60)
        cooldown_after_event_minutes = self.get("news_settings.detection_window.cooldown_after_event_minutes", 120)
        impact_map = self.get("news_settings.impact_levels", {"low": 1, "medium": 2, "high": 3})

        for event in calendar_data:
            if not all(k in event for k in ["time", "impact", "event"]):
                self.logger.warning(f"Événement de calendrier malformé. Clés manquantes. Ignoré: {event}")
                continue

            try:
                event_time_str = event.get("time")
                event_time = datetime.fromisoformat(event_time_str.replace("Z", "+00:00")).astimezone(UTC)
                time_difference = event_time - current_time_utc

                if timedelta(minutes=-window_before_event_minutes) <= time_difference <= timedelta(minutes=window_after_event_minutes):
                    impact_level = impact_map.get(event.get("impact", "low").lower(), 0)
                    if impact_level > highest_impact:
                        highest_impact, upcoming_news = impact_level, event

                if time_difference < timedelta(minutes=-window_after_event_minutes) and \
                   time_difference > timedelta(minutes=-(window_after_event_minutes + cooldown_after_event_minutes)):
                    impact_level = impact_map.get(event.get("impact", "low").lower(), 0)
                    if impact_level >= self.get("news_settings.min_impact_for_cooldown", 3):
                        self.logger.warning(f"Marché en période de cooldown post-actualité majeure : {event.get('event')} (Impact: {event.get('impact')}).")
                        return event

            except ValueError:
                self.logger.warning(f"Impossible de parser la date de l'événement économique : '{event.get('time')}'. Événement ignoré.")
                continue
            except Exception as e:
                self.logger.error(f"Erreur inattendue lors du traitement d'un événement de calendrier : {e}. Événement: {event}", exc_info=True)
                continue

        if upcoming_news:
            self.logger.warning(f"Annonce économique majeure détectée : {upcoming_news.get('event')} (Impact: {upcoming_news.get('impact')}). Trading potentiellement bloqué.")

        return upcoming_news

    def blacklist_asset_on_bad_conditions(self, asset: str, reason: str, context: Optional[Dict[str, Any]] = None) -> None:
        """
        Ajoute un actif à une liste noire persistante avec une durée d'expiration configurable
        et une raison standardisée.
        """
        self.logger.warning(f"Mise sur liste noire de '{asset}' pour la raison : {reason}")

        if not hasattr(self, "_asset_blacklist"):
            self._asset_blacklist = {}

        blacklist_durations_config = self.get("bot_behavior.blacklist_durations_by_reason", {})
        duration_minutes = blacklist_durations_config.get(reason, self.get("bot_behavior.blacklist_duration_minutes", 60))

        expiration_time = datetime.now(UTC) + timedelta(minutes=duration_minutes)
        self._asset_blacklist[asset] = expiration_time

        self.logger.info(f"Actif '{asset}' mis sur liste noire jusqu'à {expiration_time.strftime('%Y-%m-%d %H:%M:%S UTC')} pour la raison '{reason}'."
        )

        if asset == "ALL_ASSETS":
            self.logger.critical(f"TOUS LES ACTIFS mis sur liste noire. Trading suspendu. Raison : {reason}")
            alert_channel = self.get("alert_settings.global_blacklist_channel", "telegram_critical")
            self.send_alert(f"CRITIQUE: Tous les actifs suspendus en raison de : {reason}", alert_channel)

        self.log_decision(
            config=self.get_current_dynamic_config(),
            trade_decision={
                "action": "BLACKLIST_ASSET",
                "asset": asset,
                "reason": reason,
                "expiration_time": expiration_time.isoformat(),
            },
            context=context or {},
            reason=f"Mise sur liste noire : {asset} - {reason}",
        )

    def feedback_on_trade_result(self, trade_info: Dict[str, Any], result: Dict[str, Any]) -> None:
        """
        Traite le résultat d'un trade pour mettre à jour les métriques de performance de la stratégie
        et fournir une boucle de feedback pour l'apprentissage.
        Les templates de messages Telegram sont externalisés pour une flexibilité accrue.
        """
        self.logger.info(f"Traitement du feedback pour le trade ID: {trade_info.get('order_id', 'N/A')}")

        strategy_name = trade_info.get("strategy_type", "N/A")
        asset = trade_info.get("asset")
        pnl = result.get("pnl_usd", 0.0)

        if pnl > 0:
            status = TradeStatus.PROFIT
        elif pnl < 0:
            status = TradeStatus.LOSS
        else:
            status = TradeStatus.BREAKEVEN

        config_path_to_update = None
        for path, config_data in self._config_knowledge_base.items():
            if (
                isinstance(config_data, dict)
                and isinstance(config_data.get("content"), dict)
                and config_data["content"].get("strategy_name") == strategy_name
            ):
                config_path_to_update = path
                break

        if config_path_to_update:
            perf_metrics = self._config_knowledge_base[
                config_path_to_update
            ].setdefault(
                "performance",
                {
                    "wins": 0,
                    "losses": 0,
                    "total_pnl": 0.0,
                    "sharpe_ratio": 0.0,
                    "max_drawdown_percent": 0.0,
                },
            )

            if status == TradeStatus.PROFIT:
                perf_metrics["wins"] += 1
            elif status == TradeStatus.LOSS:
                perf_metrics["losses"] += 1
            perf_metrics["total_pnl"] += pnl

            self.logger.info(
                f"Mise à jour des métriques de performance pour la stratégie '{strategy_name}'. Nouveau P&L: {perf_metrics['total_pnl']:.2f}."
            )
            self._save_config_knowledge_base()

        message_template = self.get("telegram.templates.trade_closed", "📊 **Trade Clôturé**\nSymbol: `{asset}` | Stratégie: `{strategy}`\nP&L: `${pnl:.2f}` (`{status}`)\nHeure: `{time}`")
        message = message_template.format(
            asset=asset,
            strategy=strategy_name,
            pnl=pnl,
            status=status.value,
            time=datetime.now(UTC).strftime("%H:%M:%S UTC"),
        )
        self.send_alert(message, "telegram_trade_closed")

        if hasattr(self, "ai_decision_instance") and self.ai_decision_instance:
            try:
                self.ai_decision_instance.feedback_on_result(
                    {"trade": trade_info, "result": result}
                )
                self.logger.debug(
                    f"Feedback envoyé à AIDecision pour le trade {trade_info.get('order_id')}."
                )
            except Exception as e:
                self.logger.error(
                    f"Échec de l'envoi du feedback à AIDecision pour le trade {trade_info.get('order_id')}: {e}",
                    exc_info=True,
                )

        self.log_decision(
            config=self.get_current_dynamic_config(),
            trade_decision=trade_info,
            context=result,
            reason=f"Feedback sur trade clôturé: {status.value}",
            ai_input=None,
        )

    def _save_config_knowledge_base(self) -> None:
        """
        Sauvegarde la base de connaissance des configurations (`_config_knowledge_base`)
        vers un fichier JSON (pour la persistance des métriques de performance).
        """
        kb_path = (
            Path(self.get("paths.configs", "config/")) / "config_knowledge_base.json"
        )
        temp_path = kb_path.with_suffix(".tmp")
        try:
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(
                    self._config_knowledge_base, f, indent=4, cls=CustomJSONEncoder
                )
            temp_path.rename(kb_path)
            self.logger.info(
                f"Base de connaissance des configurations sauvegardée avec succès vers '{kb_path}'."
            )
        except Exception as e:
            self.logger.error(
                f"Échec de la sauvegarde de la base de connaissance des configurations vers '{kb_path}': {e}",
                exc_info=True,
            )

    def _load_config_knowledge_base_from_file(self) -> None:
        """
        Charge la base de connaissance des configurations depuis un fichier JSON au démarrage.
        """
        kb_path = (
            Path(self.get("paths.configs", "config/")) / "config_knowledge_base.json"
        )
        if kb_path.exists():
            try:
                with open(kb_path, "r", encoding="utf-8") as f:
                    self._config_knowledge_base = json.load(f)
                self.logger.info(
                    f"Base de connaissance des configurations chargée depuis '{kb_path}'."
                )
            except Exception as e:
                self.logger.error(
                    f"Échec du chargement de la base de connaissance des configurations depuis '{kb_path}': {e}",
                    exc_info=True,
                )
                self._config_knowledge_base = (
                    {}
                )
        else:
            self.logger.info(
                "Fichier de base de connaissance des configurations non trouvé. Initialisation vide."
            )
            self._config_knowledge_base = (
                {}
            )

    def auto_update_knowledge_base(self) -> None:
        """
        Met à jour la base de connaissance en scannant le répertoire de configurations
        pour découvrir de nouvelles stratégies ajoutées "à chaud" (hot-reload),
        et détecter les modifications ou suppressions.
        """
        self.logger.info(
            "Mise à jour automatique de la base de connaissance des stratégies..."
        )

        strategy_config_dir = self.get("paths.strategy_configs", "config/strategies/")

        latest_configs = self.scan_config_files(strategy_config_dir)

        new_configs_found = set(latest_configs.keys()) - set(
            self._config_knowledge_base.keys()
        )
        if new_configs_found:
            self.logger.info(
                f"Nouvelles configurations de stratégies détectées : {list(new_configs_found)}"
            )
            self._config_knowledge_base.update(
                {k: v for k, v in latest_configs.items() if k in new_configs_found}
            )
            self._save_config_knowledge_base()

        modified_configs = []
        for path, new_data in latest_configs.items():
            if (
                path in self._config_knowledge_base
                and new_data["version"] != self._config_knowledge_base[path]["version"]
            ):
                modified_configs.append(path)
                self._config_knowledge_base[path] = new_data
                self.logger.info(
                    f"Configuration de stratégie modifiée détectée : {Path(path).name}. Mise à jour de la base de connaissance."
                )
        if modified_configs:
            self._save_config_knowledge_base()

        deleted_configs = set(self._config_knowledge_base.keys()) - set(
            latest_configs.keys()
        )
        if deleted_configs:
            self.logger.warning(
                f"Configurations de stratégies supprimées détectées : {list(deleted_configs)}. Elles seront retirées de la base de connaissance."
            )
            for path in deleted_configs:
                del self._config_knowledge_base[path]
            self._save_config_knowledge_base()

        if not new_configs_found and not modified_configs and not deleted_configs:
            self.logger.debug(
                "Aucun changement détecté dans les configurations de stratégies. Base de connaissance à jour."
            )

    def backtest_strategy(
        self, config: Dict[str, Any], historical_data: pd.DataFrame
    ) -> Dict[str, Any]:
        """
        Exécute un backtest vectoriel simplifié sur une configuration de stratégie.
        """
        self.logger.info(
            f"Lancement du backtest pour la stratégie : {config.get('strategy_name', 'inconnue')}"
        )

        if historical_data.empty:
            self.logger.warning(
                "Données historiques vides, impossible de lancer le backtest."
            )
            return {"error": "Données historiques manquantes."}

        short_window = self.get("backtest_simulation.ma_short", 10)
        long_window = self.get("backtest_simulation.ma_long", 20)

        if len(historical_data) < long_window:
            self.logger.warning(
                f"Données historiques insuffisantes ({len(historical_data)} barres) pour les fenêtres de MA ({long_window}). Backtest annulé."
            )
            return {"error": "Données historiques insuffisantes pour le backtest."}

        df = historical_data.copy()
        df["short_ma"] = df["close"].rolling(window=short_window, min_periods=1).mean()
        df["long_ma"] = df["close"].rolling(window=long_window, min_periods=1).mean()
        df["signal"] = 0
        df.loc[df["short_ma"] > df["long_ma"], "signal"] = 1
        df.loc[df["short_ma"] < df["long_ma"], "signal"] = -1
        df["position"] = df["signal"].shift(1).fillna(0)
        df["returns"] = df["close"].pct_change()
        df["strategy_returns"] = df["returns"] * df["position"]
        df.dropna(subset=["strategy_returns"], inplace=True)

        if df["strategy_returns"].empty:
            return {
                "error": "Pas de trades générés ou de retours significatifs pour calculer les métriques."
            }

        cumulative_returns = (1 + df["strategy_returns"]).cumprod()
        total_pnl_percent = (cumulative_returns.iloc[-1] - 1) * 100

        peak = cumulative_returns.expanding(min_periods=1).max()
        drawdown = (cumulative_returns - peak) / peak
        max_drawdown = drawdown.min() * 100

        daily_returns_std = df["strategy_returns"].std()
        if daily_returns_std == 0:
            sharpe_ratio = 0.0
        else:
            sharpe_ratio = (df["strategy_returns"].mean() / daily_returns_std) * (
                self.get("backtest_simulation.annualization_factor", 252) ** 0.5
            )

        results = {
            "net_profit_percent": round(total_pnl_percent, 2),
            "max_drawdown_percent": round(abs(max_drawdown), 2),
            "sharpe_ratio": round(sharpe_ratio, 2),
            "number_of_trades": int(df["position"].abs().diff().fillna(0).eq(2).sum()),
            "config_snapshot": config,
        }

        self.logger.info(
            f"Backtest terminé pour la stratégie '{config.get('strategy_name', 'inconnue')}'. Résultats: {results}"
        )
        return results

    def update_config_from_backtest(self, results: Dict[str, Any]) -> None:
        """
        Met à jour activement une configuration en fonction des résultats de son backtest.
        """
        self.logger.info(
            "Mise à jour de la configuration suite aux résultats du backtest..."
        )
        config = results.get("config_snapshot")
        if not config:
            self.logger.warning(
                "Snapshot de configuration manquant dans les résultats du backtest. Mise à jour annulée."
            )
            self.log_decision(
                self.get_current_dynamic_config(),
                {},
                {"backtest_results": results},
                "Mise à jour config annulée: Snapshot manquant.",
            )
            return

        sharpe_ratio = results.get("sharpe_ratio", 0.0)
        max_drawdown = results.get("max_drawdown_percent", 100.0)
        strategy_name = config.get("strategy_name", "inconnue")

        thresholds = self.get(
            "backtest_evaluation.sharpe_thresholds",
            {"poor_performance": 0.5, "suboptimal_ai_intervention": 0.7},
        )
        high_drawdown_threshold = self.get(
            "backtest_evaluation.thresholds.high_drawdown", 10.0
        )

        config_path_in_kb = None
        for path, data in self._config_knowledge_base.items():
            if data["content"].get("strategy_name") == strategy_name:
                config_path_in_kb = path
                break

        if config_path_in_kb:
            perf_metrics = self._config_knowledge_base[config_path_in_kb].setdefault(
                "performance", {"wins": 0, "losses": 0, "total_pnl": 0.0}
            )
            perf_metrics["sharpe_ratio"] = sharpe_ratio
            perf_metrics["max_drawdown_percent"] = max_drawdown
            perf_metrics["last_backtest_timestamp"] = datetime.now(UTC).isoformat()
            self.logger.info(
                f"Métriques de backtest pour stratégie '{strategy_name}' mises à jour dans la base de connaissance."
            )
            self._save_config_knowledge_base()

        update_payload: Dict[str, Any] = {}

        if (
            sharpe_ratio < thresholds.get("poor_performance")
            or max_drawdown > high_drawdown_threshold
        ):
            self.logger.warning(
                f"La stratégie '{strategy_name}' a montré une performance faible en backtest (Sharpe: {sharpe_ratio:.2f}, DD: {max_drawdown:.2f}%)."
            )

            risk_reduction_factor = self.get(
                "backtest_evaluation.risk_reduction_factor", 0.9
            )
            current_risk = config.get("risk_per_trade_percent", 1.0)
            new_risk = current_risk * risk_reduction_factor

            update_payload["risk_per_trade_percent"] = round(new_risk, 3)
            update_payload["notes"] = (
                f"Risque ajusté automatiquement à {new_risk:.3f}% suite à un backtest non performant."
            )

            if (
                self.get("ai.enabled", False)
                and hasattr(self, "ai_decision_instance")
                and self.ai_decision_instance
            ):
                try:
                    ai_advice = self.ai_decision_instance.suggest_trading_improvements(
                        logs=[results],
                        context={
                            "backtest_type": "poor_performance_detected",
                            "strategy_name": strategy_name,
                        },
                    )
                    self.logger.info(
                        f"Recommandation d'optimisation de l'IA reçue suite à un backtest faible pour '{strategy_name}'."
                    )
                    if ai_advice.get("recommended_adjustments"):
                        update_payload = self._merge_dicts(
                            update_payload, ai_advice["recommended_adjustments"]
                        )
                        self.logger.info(
                            "Ajustements IA fusionnés dans le payload de mise à jour de la config."
                        )
                except Exception as e:
                    self.logger.error(
                        f"Échec de l'interaction avec l'IA pour l'optimisation de backtest: {e}",
                        exc_info=True,
                    )
        else:
            self.logger.info(
                f"La stratégie '{strategy_name}' a bien performé en backtest (Sharpe: {sharpe_ratio:.2f}). Aucun ajustement de risque automatique nécessaire."
            )

        if update_payload:
            self.update_dynamic_config(
                updates=update_payload, source="backtest_feedback_optimizer"
            )
        else:
            self.logger.info(
                f"Aucun ajustement de configuration appliqué pour la stratégie '{strategy_name}' suite au backtest."
            )

    def export_config(
        self, config: Dict[str, Any], path: str, fmt: str = "json"
    ) -> None:
        """
        Exporte un dictionnaire de configuration vers un fichier de manière atomique.
        """
        self.logger.info(f"Export de la configuration vers {path} au format {fmt}.")
        output_path = Path(path)
        temp_path = output_path.with_suffix(output_path.suffix + ".tmp")

        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)

            with open(temp_path, "w", encoding="utf-8") as f:
                if fmt == "json":
                    json.dump(config, f, indent=4, cls=CustomJSONEncoder)
                elif fmt == "yaml":
                    yaml.safe_dump(config, f, indent=4)
                elif fmt == "set":
                    self._write_set_file(config, f)
                else:
                    raise ValueError(f"Format d'export non supporté : {fmt}")

            temp_path.rename(output_path)
            self.logger.info(f"Configuration exportée avec succès vers {output_path}.")

            file_permissions = self.get(
                "app.file_permissions_octal", "0o644"
            )
            try:
                os.chmod(
                    output_path, int(file_permissions, 8)
                )
                self.logger.debug(
                    f"Permissions du fichier '{output_path}' définies à {file_permissions}."
                )
            except Exception as e:
                self.logger.warning(
                    f"Impossible de définir les permissions du fichier '{output_path}' à {file_permissions}: {e}"
                )

        except Exception as e:
            self.logger.error(
                f"Erreur lors de l'export de la configuration vers {path}: {e}",
                exc_info=True,
            )
            if temp_path.exists():
                temp_path.unlink()
            self.send_alert(f"CRITIQUE: Échec export config: {e}", "telegram_critical")

    def _write_set_file(self, config: Dict[str, Any], file_object: Any) -> None:
        """
        Écrit un dictionnaire de configuration dans un fichier .set MT5.
        """
        for section, settings in config.items():
            file_object.write(f"[{section}]\n")
            for key, value in settings.items():
                if isinstance(value, bool):
                    file_object.write(f"{key}={int(value)}\n")
                else:
                    file_object.write(f"{key}={value}\n")
        self.logger.debug("Écriture du fichier .set terminée.")

    def import_config(self, path: str, fmt: str = "auto") -> Dict[str, Any]:
        """
        Importe et valide une configuration depuis un fichier.
        """
        self.logger.info(f"Import de la configuration depuis {path} (Format: {fmt}).")

        is_url = path.startswith(("http://", "https://", "ftp://"))
        file_content = None

        if is_url:
            self.logger.info(f"Importation de la configuration depuis l'URL : {path}")
            try:
                response = requests.get(
                    path,
                    timeout=self.get("config_import_settings.url_timeout_seconds", 10),
                )
                response.raise_for_status()
                file_content = response.text
                self.logger.info(
                    "Contenu de la configuration téléchargé avec succès depuis l'URL."
                )
            except requests.exceptions.RequestException as e:
                self.logger.error(
                    f"Échec du téléchargement de la configuration depuis l'URL '{path}': {e}",
                    exc_info=True,
                )
                raise IOError(
                    f"Impossible de télécharger la configuration depuis {path}"
                ) from e
        else:
            config_path = Path(path)
            if not config_path.is_file():
                raise FileNotFoundError(
                    f"Fichier de configuration introuvable : {path}"
                )
            with open(config_path, "r", encoding="utf-8") as f:
                file_content = f.read()

        file_ext = Path(path).suffix.lower().lstrip(".")

        try:
            if fmt == "auto":
                if file_ext == "json":
                    parser_fn = self.parse_json_config
                elif file_ext in ["yaml", "yml"]:
                    parser_fn = self.parse_yaml_config
                elif file_ext == "set":
                    parser_fn = self.parse_set_file
                else:
                    raise ValueError(f"Impossible de déduire le format pour {path}.")
            else:
                parser_method_name = f"parse_{fmt}_config"
                if hasattr(self, parser_method_name) and callable(
                    getattr(self, parser_method_name)
                ):
                    parser_fn = getattr(self, parser_method_name)
                else:
                    raise ValueError(f"Format d'import '{fmt}' non supporté.")

            if is_url:
                temp_import_path = (
                    Path(self.get("paths.logs", "logs/"))
                    / f"temp_import_{uuid.uuid4().hex}.{file_ext}"
                )
                with open(temp_import_path, "w", encoding="utf-8") as f:
                    f.write(file_content)
                imported_config = parser_fn(str(temp_import_path))
                temp_import_path.unlink()
            else:
                imported_config = parser_fn(str(config_path))

            self.validate_config(imported_config)
            self.logger.info(
                f"Configuration importée et validée avec succès depuis {path}."
            )
            return imported_config
        except (ConfigValidationError, jsonschema.ValidationError) as e:
            self.logger.error(
                f"La configuration importée depuis {path} est invalide : {e}",
                exc_info=True,
            )
            raise
        except Exception as e:
            self.logger.error(
                f"Erreur lors de l'import de la configuration depuis {path}: {e}",
                exc_info=True,
            )
            raise IOError(
                f"Impossible d'importer la configuration depuis {path}"
            ) from e

    _schema_cache: Dict[str, Dict[str, Any]] = {}

    def validate_config(self, config: Dict[str, Any]) -> bool:
        """
        Valide un dictionnaire de configuration en utilisant un schéma JSON formel.
        """
        self.logger.debug("Validation de la configuration par schéma...")

        schema_name: Optional[str] = None
        if "project" in config and config.get("project") == "SNIPER_X":
            schema_name = "main_app_schema.json"
        elif "strategy_name" in config:
            schema_name = "strategy_schema.json"
        elif "accounts" in config and isinstance(
            config.get("accounts"), list
        ):
            schema_name = "broker_accounts_schema.json"
        else:
            raise ConfigValidationError(
                "Type de configuration inconnu. 'project', 'strategy_name' ou 'accounts' manquant."
            )

        schema = self._schema_cache.get(schema_name)
        if schema is None:
            schema_path = Path(__file__).parent / "schemas" / schema_name
            if not schema_path.is_file():
                self.logger.critical(
                    f"FATAL: Fichier de schéma de validation '{schema_name}' introuvable à '{schema_path}'. Impossible d'assurer la conformité de la configuration. Le bot ne peut pas démarrer en toute sécurité."
                )
                raise FileNotFoundError(
                    f"Fichier de schéma de validation manquant : {schema_path}"
                )

            try:
                with open(schema_path, "r", encoding="utf-8") as f:
                    schema = json.load(f)
                self._schema_cache[schema_name] = schema
                self.logger.debug(f"Schéma '{schema_name}' chargé et mis en cache.")
            except json.JSONDecodeError as e:
                self.logger.critical(
                    f"FATAL: Erreur de syntaxe JSON dans le fichier de schéma '{schema_name}': {e}. Le bot ne peut pas démarrer.",
                    exc_info=True,
                )
                raise ConfigValidationError(
                    f"Schéma '{schema_name}' invalide : {e.message}"
                ) from e
            except Exception as e:
                self.logger.critical(
                    f"FATAL: Erreur lors du chargement du schéma '{schema_name}': {e}. Le bot ne peut pas démarrer.",
                    exc_info=True,
                )
                raise RuntimeError(
                    f"Erreur lors du chargement du schéma '{schema_name}'"
                ) from e

        try:
            jsonschema.validate(instance=config, schema=schema)
            self.logger.debug(
                f"La configuration a passé la validation avec le schéma '{schema_name}'."
            )
            return True
        except jsonschema.ValidationError as e:
            error_message = f"Échec de la validation par schéma '{schema_name}': {e.message} (sur le champ: `{''.join(e.path)}`)"
            self.logger.error(error_message, exc_info=True)
            raise ConfigValidationError(error_message) from e

    def organize_pipeline_decision(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Orchestre le pipeline de décision en utilisant une configuration transitoire
        pour garantir que chaque cycle est indépendant et basé sur les données les plus récentes.
        Le ConfigManager est désormais l'unique décideur, sans consultation directe de l'IA
        pour les recommandations de stratégie ni délégation de la décision finale à une instance de stratégie.
        """
        self.logger.info("Orchestration du pipeline de décision institutionnel...")

        analyzed_context = self.analyze_context(context)

        all_assets_market_data_from_context = context.get("market_data", {})

        # CORRECTION du problème : le dictionnaire 'data' passé à detect_market_regime est vide
        # car 'market_data' du context n'est pas encore rempli à ce stade de l'exécution dans run_single_pipeline_cycle.
        # Cette logique de détection du régime de marché DOIT utiliser les vraies données de marché,
        # qui ne sont collectées qu'après. La logique d'appel à organize_pipeline_decision a été déplacée plus tôt
        # dans run_single_pipeline_cycle pour permettre la SELECTION de la stratégie,
        # mais la détection du régime a besoin des données réelles.
        # Pour l'instant, on laisse l'appel à detect_market_regime ici, mais il se peut que `all_assets_market_data_from_context`
        # soit vide au premier appel à organize_pipeline_decision (dans run_single_pipeline_cycle)
        # car les données ne sont collectées qu'après le retour de cette fonction.
        # C'est un problème d'ordre d'appel plus global. Pour le moment, nous allons juste faire attention à la `select_optimal_config`.
        analyzed_context["current_market_regime"] = self.detect_market_regime(
            analyzed_context, all_assets_market_data_from_context # Cette donnée peut être vide à ce point
        )

        # CORRECTION : Accéder à select_optimal_config via l'instance du DecisionPipeline.
        # DecisionPipeline est un attribut de ConfigManager.
        if not hasattr(self, 'decision_pipeline') or self.decision_pipeline is None:
            self.logger.critical("ERREUR: DecisionPipeline n'est pas initialisé dans ConfigManager. Impossible de sélectionner une stratégie.")
            raise RuntimeError("DecisionPipeline non initialisé.")

        # Assurez-vous que strategy_manager est initialisé et a chargé les stratégies.
        if not hasattr(self, 'strategy_manager') or self.strategy_manager is None:
            self.logger.critical("ERREUR: StrategyManager n'est pas initialisé dans ConfigManager. Impossible de sélectionner une stratégie.")
            raise RuntimeError("StrategyManager non initialisé.")

        # La base de connaissance des stratégies est dans strategy_manager.strategy_registry
        config_knowledge_base_from_strategy_manager = self.strategy_manager.strategy_registry

        optimal_config_content = self.decision_pipeline.select_optimal_config( # <-- CORRECTION ICI
            analyzed_context, config_knowledge_base_from_strategy_manager
        )
        if not optimal_config_content:
            self.logger.warning("Aucune stratégie optimale sélectionnée. Fin du pipeline.")
            return {
                "final_decision": {},
                "config_used": self.get_current_dynamic_config(),
            }

        # La fusion et l'adaptation de la config se font toujours ici car c'est la config globale qui est affectée.
        config_for_this_cycle = self.get_current_dynamic_config()
        config_for_this_cycle = self._merge_dicts(
            config_for_this_cycle, optimal_config_content
        )
        config_for_this_cycle = self.adapt_config(
            config_for_this_cycle, analyzed_context
        )

        signals = analyzed_context.get("trading_signals", {})
        # decide_trade_to_execute devrait aussi être appelée via decision_pipeline
        trade_decision = self.decision_pipeline.decide_trade_to_execute( # <-- CORRECTION ICI
            analyzed_context, config_for_this_cycle, signals
        )

        return {
            "timestamp_utc": datetime.now(UTC).isoformat(),
            "context": analyzed_context,
            "config_used": config_for_this_cycle,
            "final_decision": trade_decision,
        }

        # NOTE : La fonction issue_trade_order DOIT être une méthode de la classe ConfigManager,
        # et non imbriquée dans organize_pipeline_decision.
        # Je la place ici comme une méthode de la classe ConfigManager.
        
def issue_trade_order(
        self,
        trade_decision: Dict[str, Any],
        config: Dict[str, Any],
        context: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Prépare et transmet l'ordre de trade final au module d'exécution.
        Cette fonction agrège toutes les informations nécessaires pour former un ordre structuré.
        """
        self.logger.info(
            f"Préparation de l'ordre de trade pour {trade_decision.get('asset')} (Stratégie: {trade_decision.get('strategy_type')})..."
        )

        if (
            not trade_decision
            or not trade_decision.get("action")
            or not trade_decision.get("volume", 0) > 0
        ):
            self.logger.warning(
                "Décision de trade invalide ou volume nul. Ordre non émis."
            )
            return {}

        comment_template = self.get(
            "trading.order_comment_template", "SNIPER_X | {strategy} | {rule}"
        )
        max_comment_length = self.get("trading.comment_max_length", 31)
        comment = comment_template.format(
            strategy=config.get("strategy_name", "N/A"),
            rule=trade_decision.get("rule_name", "N/A"),
        )[:max_comment_length]

        magic_number = trade_decision.get("magic_number", config.get("magic_number"))
        if magic_number is None:
            self.logger.warning(
                f"Magic number non trouvé pour le trade {trade_decision.get('asset')}. Utilisation de 0."
            )
            magic_number = 0

        active_broker_account_details = context.get("active_broker_account", {})

        order = {
            "order_id": str(uuid.uuid4()),
            "timestamp": datetime.now(UTC).isoformat(),
            "action": trade_decision["action"],
            "asset": trade_decision["asset"],
            "volume": trade_decision["volume"],
            "order_type": trade_decision["order_type"],
            "entry_price": trade_decision.get("entry_price"),
            "stop_loss_pips": trade_decision.get("target_sl_pips"),
            "take_profit_pips": trade_decision.get("target_tp_pips"),
            "magic_number": magic_number,
            "comment": comment,
            "strategy_name": trade_decision.get("strategy_type"),
            "rule_name": trade_decision.get("rule_name"),
            "account_id": active_broker_account_details.get("account_id"),
            "broker_name": active_broker_account_details.get("broker_name"),
        }

        message_template = self.get(
            "telegram.templates.trade_confirmed",
            "🚀 **Trade Confirmé**\nSymbol: `{asset}` | Action: `{action}`\nVolume: `{volume}` lots | Stratégie: `{strategy}`",
        )
        message = message_template.format(
            asset=order.get("asset"),
            action=order.get("action"),
            volume=order.get("volume"),
            strategy=order.get("strategy_name"),
        )
        self.send_alert(message, "telegram_trade_confirmed")

        self.logger.debug(f"Ordre structuré prêt pour exécution : {order}")
        return order

    def log_decision(
        self,
        config: Dict[str, Any],
        trade_decision: Dict[str, Any],
        context: Dict[str, Any],
        reason: str,
        ai_input: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Enregistre un point de décision clé dans le journal d'audit pour une traçabilité complète.
        """
        summarized_context = context.copy()
        if "market_data" in summarized_context:
            summarized_market_data = {}
            for asset, data_df in summarized_context["market_data"].items():
                if isinstance(data_df, pd.DataFrame) and not data_df.empty:
                    last_row = data_df.iloc[-1].to_dict()
                    summarized_market_data[asset] = {
                        "phase": last_row.get("phase"),
                        "confidence_score": last_row.get("confidence_score"),
                        "current_price": last_row.get("close"),
                        "volume_momentum": last_row.get("volume_momentum"),
                        "nearest_liquidity_level_details": last_row.get(
                            "nearest_liquidity_level_details"
                        ),
                        "entry_confirmation_bullish": last_row.get(
                            "entry_confirmation_bullish", False
                        ),
                        "entry_confirmation_bearish": last_row.get(
                            "entry_confirmation_bearish", False
                        ),
                    }
                else:
                    summarized_market_data[asset] = "Dataframe vide ou invalide"
            summarized_context["market_data_summary"] = summarized_market_data
            del summarized_context["market_data"]

        if "open_positions" in summarized_context and isinstance(
            summarized_context["open_positions"], list
        ):
            summarized_context["open_positions_count"] = len(
                summarized_context["open_positions"]
            )
            summarized_context["open_positions_summary"] = [
                {
                    "ticket": pos.get("ticket"),
                    "symbol": pos.get("symbol"),
                    "type": pos.get("type"),
                    "volume": pos.get("volume"),
                }
                for pos in summarized_context["open_positions"][:5]
            ]
            del summarized_context["open_positions"]

        keys_to_clean_from_context = self.get(
            "app.context_keys_to_clean_for_logs",
            ["economic_calendar", "ai_recommendation_score"],
        )
        for key in keys_to_clean_from_context:
            if key in summarized_context:
                summarized_context[f"{key}_summary"] = (
                    f"Contenu (taille {len(summarized_context[key])}) non logué pour optim. des logs."
                    if isinstance(summarized_context[key], (list, dict))
                    else summarized_context[key]
                )
                del summarized_context[key]

        decision_log_entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "reason": reason,
            "trade_decision": trade_decision,
            "ai_input": ai_input,
            "config_snapshot": config,
            "context_summary": summarized_context,
        }
        self.logger.info(
            f"LOG DÉCISION: {reason} | Actif: {trade_decision.get('asset', 'N/A')} | Action: {trade_decision.get('action', 'N/A')}"
        )
        self._audit_trail.append(decision_log_entry)

    def send_alert(self, message: str, alert_type: str = "telegram_critical") -> None:
        """
        Envoie une alerte via le système d'alerte configuré (par ex. Telegram).
        Cette méthode délègue l'envoi réel à l'AuditLogger ou à un module de notification dédié.
        Elle est un point central pour toutes les alertes du système.

        Args:
            message (str): Le contenu du message d'alerte.
            alert_type (str): Le type d'alerte (ex: 'telegram_critical', 'telegram_trade_confirmed').
                            Utilisé pour déterminer le canal ou le traitement spécifique.
        """
        self.logger.info(f"Tentative d'envoi d'alerte de type '{alert_type}' : {message[:100]}...") # Log les 100 premiers caractères

        if hasattr(self, 'audit_logger') and self.audit_logger is not None:
            # L'AuditLogger aura la logique d'envoi réelle (ex: vers Telegram)
            # Nous assumons que AuditLogger a une méthode pour gérer l'envoi d'alertes.
            # Si AuditLogger n'a pas encore de méthode 'send_telegram_alert' ou similaire,
            # il faudra l'ajouter à AuditLogger dans une étape ultérieure.
            try:
                # Ici, nous appelons une méthode générique de l'AuditLogger pour gérer l'alerte.
                # L'AuditLogger devra être étendu pour router ces alertes vers les bons canaux (ex: Telegram).
                self.audit_logger.queue_or_send_alert(message, alert_type)
                self.logger.debug(f"Alerte '{alert_type}' transmise à l'AuditLogger.")
            except Exception as e:
                self.logger.error(f"Échec de la transmission de l'alerte à l'AuditLogger: {e}", exc_info=True)
        else:
            self.logger.critical("ConfigManager ne peut pas envoyer d'alerte : AuditLogger non initialisé.")


    def process_and_send_summary_alert(self, context: Dict[str, Any]) -> None:
        """
        Traite le contexte pour générer un résumé périodique et l'envoie via le système d'alerte.
        Cette méthode est appelée régulièrement pour fournir une vue d'ensemble du bot.

        Args:
            context (Dict[str, Any]): Le contexte actuel du bot, incluant l'état du compte,
                                    le nombre de trades, etc.
        """
        self.logger.debug("Traitement et envoi du résumé périodique de l'état du bot...")

        # Récupérer les paramètres de résumé depuis la configuration
        summary_interval_minutes = self.get("telegram.summary_interval_minutes", 8)
        
        # Vérifier si l'envoi de résumé est activé dans la configuration Telegram
        telegram_enabled = self.get("telegram.enabled", False)
        summary_channel_enabled = self.get("telegram.channels.telegram_summary", False)

        if not telegram_enabled or not summary_channel_enabled:
            self.logger.debug("Envoi de résumé désactivé (Telegram non activé ou canal de résumé non activé).")
            return

        # Vérifier si l'intervalle de temps est écoulé depuis le dernier envoi
        current_time_utc = datetime.now(UTC)
        if (current_time_utc - self._last_summary_sent_time).total_seconds() < summary_interval_minutes * 60:
            self.logger.debug(f"Prochain envoi de résumé dans {(summary_interval_minutes * 60) - (current_time_utc - self._last_summary_sent_time).total_seconds():.0f} secondes.")
            return

        # Construction du message de résumé
        bot_mode = context.get("bot_mode", "N/A").upper()
        account_id = context.get("account_info", {}).get("account_id", "N/A")
        account_equity = context.get("account_info", {}).get("equity", 0.0)
        daily_trades = context.get("daily_trade_count", 0)
        open_positions = context.get("open_positions_count", 0)
        current_regime = context.get("current_market_regime", "N/A")

        summary_message = self.get("telegram.templates.summary_header", 
            "--- **Résumé Périodique SNIPER_X** ---\n`{time}` | Compte: `{account_id}` ({broker})"
        ).format(
            time=current_time_utc.strftime("%H:%M:%S UTC"),
            account_id=account_id,
            broker=context.get("active_broker_account", {}).get("broker_name", "N/A")
        )
        summary_message += f"\n\nMode: `{bot_mode}`"
        summary_message += f"\nEquity: `${account_equity:.2f}`"
        summary_message += f"\nTrades Jours: `{daily_trades}`"
        summary_message += f"\nPos. Ouvertes: `{open_positions}`"
        summary_message += f"\nRégime Marché: `{current_regime}`"

        # Envoyer le message de résumé via la méthode send_alert
        self.send_alert(summary_message, "telegram_summary")
        self._last_summary_sent_time = current_time_utc # Mettre à jour le timestamp du dernier envoi

        self.logger.info("Résumé périodique de l'état du bot envoyé.")


    def _generate_report_header(self, report_date: datetime) -> List[str]:
        """
        Génère l'en-tête du rapport quotidien, incluant les informations clés du système et de la configuration active.
        """
        config = self.get_current_dynamic_config()
        bot_mode = config.get("mode_execution", "N/A").upper()
        default_strategy = config.get("strategies", {}).get("default_strategy", "N/A")
        ai_enabled = config.get("ai", {}).get("enabled", False)

        header_lines = [
            f"--- Rapport Quotidien SNIPER_X - {report_date.isoformat()} ---",
            f"Généré le: {datetime.now(UTC).isoformat()}",
            "\n## 1. Vue d'Ensemble du Système",
            f"  Fichier de Configuration Actif: {self._dynamic_config_path}",
            f"  Mode du Bot: {bot_mode}",
            f"  Stratégie Par Défaut: {default_strategy}",
            f"  Mode IA Co-pilot: {'Activé' if ai_enabled else 'Désactivé'}",
            f"  Compte MT5 Actif: {self.get_current_mt5_account_id_from_context_or_config()}",
        ]
        return header_lines

    def get_current_mt5_account_id_from_context_or_config(self) -> str:
        """
        Tente de récupérer l'ID du compte MT5 actuellement utilisé ou configuré par défaut.
        """
        if hasattr(
            self, "_dynamic_config"
        ) and "active_broker_account" in self._dynamic_config.get("context", {}):
            return self._dynamic_config["context"]["active_broker_account"].get(
                "account_id", "N/A"
            )

        current_mode = self.get("mode_execution", "DEMO").upper()
        if current_mode == "LIVE":
            return self._broker_accounts_config.get("default_live_account", "N/A")
        elif current_mode == "DEMO":
            return self._broker_accounts_config.get("default_demo_account", "N/A")
        return "N/A"

    def _generate_report_config_changes(self, start_of_day: datetime) -> List[str]:
        """
        Génère la section des changements de configuration pour le rapport quotidien.
        """
        report_lines = ["\n## 2. Changements de Configuration Aujourd'hui"]

        if not hasattr(self, "_config_history_list"):
            self._config_history_list = []

        today_changes = [
            c
            for c in self._config_history_list
            if datetime.fromisoformat(c["timestamp"]).astimezone(UTC).date()
            == start_of_day.date()
        ]

        if not today_changes:
            report_lines.append(
                "  Aucun changement de configuration enregistré aujourd'hui."
            )
            return report_lines

        for change in today_changes:
            info = change["change_info"]
            ts = (
                datetime.fromisoformat(change["timestamp"])
                .astimezone(UTC)
                .strftime("%H:%M:%S UTC")
            )
            action_detail = info.get("action", "N/A")
            source_detail = change["source"]

            line = f"  - [{ts}] Source: {source_detail}, Action: {action_detail}"

            if action_detail == "update" and info.get("updates"):
                line += "\n    Détails des mises à jour :"
                for updated_key, update_details in info["updates"].items():
                    old_val = update_details.get("old_value")
                    new_val = update_details.get("new_value")
                    action_type = update_details.get("action")
                    line += f"\n      - `{updated_key}`: {action_type} (Ancienne: {old_val}, Nouvelle: {new_val})"
            elif action_detail == "load_strategy" and info.get("strategy"):
                line += f", Stratégie: {info['strategy']}, Fichier: {info.get('file')}"
            elif action_detail == "REINITIALIZE" and info.get("new_config_file"):
                line += f", Nouveau fichier de base: {info['new_config_file']}"

            report_lines.append(line)
        return report_lines

    def _generate_report_trade_decisions(
        self, decisions: List[Dict[str, Any]]
    ) -> Tuple[List[str], Dict[str, Any]]:
        """
        Génère la section des décisions de trade et des performances réalisées pour le rapport quotidien.
        """
        report_lines = ["\n## 3. Décisions de Trade et Résultats"]

        executed_trades_data_list = []
        for d in decisions:
            if d.get("final_decision") and d["final_decision"].get("action") in [
                "BUY",
                "SELL",
                "CLOSE",
            ]:
                trade_result = d.get("context", {}).get("result", {})
                if trade_result and trade_result.get("status") == "executed":
                    executed_trades_data_list.append(
                        {
                            "order_id": d["final_decision"].get("order_id"),
                            "asset": d["final_decision"].get("asset"),
                            "action": d["final_decision"].get("action"),
                            "volume": d["final_decision"].get("volume"),
                            "strategy": d["final_decision"].get("strategy_type"),
                            "pnl_usd": trade_result.get("pnl_usd", 0.0),
                            "status": trade_result.get("status"),
                            "entry_time": d["final_decision"].get("timestamp"),
                            "close_time": trade_result.get("timestamp"),
                        }
                    )

        trade_metrics = {
            "total_trades": 0,
            "total_pnl": 0.0,
            "wins": 0,
            "losses": 0,
            "breakeven": 0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "sharpe_ratio_daily": 0.0,
        }

        if not executed_trades_data_list:
            report_lines.append(
                "  Aucun trade exécuté aujourd'hui ou résultats non disponibles."
            )
            return report_lines, trade_metrics

        trades_df = pd.DataFrame(executed_trades_data_list)

        trade_metrics["total_pnl"] = trades_df["pnl_usd"].sum()
        trade_metrics["wins"] = trades_df[trades_df["pnl_usd"] > 0].shape[0]
        trade_metrics["losses"] = trades_df[trades_df["pnl_usd"] < 0].shape[0]
        trade_metrics["breakeven"] = trades_df[trades_df["pnl_usd"] == 0].shape[0]
        trade_metrics["total_trades"] = len(trades_df)
        trade_metrics["win_rate"] = (
            (trade_metrics["wins"] / trade_metrics["total_trades"]) * 100
            if trade_metrics["total_trades"] > 0
            else 0.0
        )

        gross_profit = trades_df[trades_df["pnl_usd"] > 0]["pnl_usd"].sum()
        gross_loss = abs(trades_df[trades_df["pnl_usd"] < 0]["pnl_usd"].sum())

        trade_metrics["profit_factor"] = (
            round(gross_profit / gross_loss, 2)
            if gross_loss > 0
            else (np.inf if gross_profit > 0 else 0.0)
        )

        if trade_metrics["total_trades"] > 1 and trades_df["pnl_usd"].std() > 0:
            avg_pnl_per_trade = trades_df["pnl_usd"].mean()
            std_pnl_per_trade = trades_df["pnl_usd"].std()
            trade_metrics["sharpe_ratio_daily"] = (
                avg_pnl_per_trade / std_pnl_per_trade
            ) * np.sqrt(trade_metrics["total_trades"])
        else:
            trade_metrics["sharpe_ratio_daily"] = 0.0

        report_lines.append(
            f"  Résumé: {trade_metrics['total_trades']} trades exécutés (Dont {trade_metrics['wins']} gagnants, {trade_metrics['losses']} perdants, {trade_metrics['breakeven']} BE)."
        )
        report_lines.append(f"  P&L Total Réalisé: `${trade_metrics['total_pnl']:.2f}`")
        report_lines.append(
            f"  Taux de Gain (Win Rate): `{trade_metrics['win_rate']:.2f}%`"
        )
        report_lines.append(
            f"  Facteur de Profit: `{trade_metrics['profit_factor']:.2f}`"
        )
        report_lines.append(
            f"  Sharpe Ratio (Jour): `{trade_metrics['sharpe_ratio_daily']:.2f}`"
        )

        report_lines.append("\n  Détail des Trades Exécutés :")
        if not trades_df.empty:
            display_cols = [
                "asset",
                "action",
                "volume",
                "strategy",
                "pnl_usd",
                "status",
                "entry_time",
                "close_time",
            ]
            trades_df_display = trades_df[display_cols].copy()
            trades_df_display["pnl_usd"] = trades_df_display["pnl_usd"].apply(
                lambda x: f"${x:.2f}"
            )
            trades_df_display["volume"] = trades_df_display["volume"].apply(
                lambda x: f"{x:.2f}"
            )

            report_lines.append(trades_df_display.to_markdown(index=False))
        else:
            report_lines.append("    Aucun trade exécuté.")

        return (
            report_lines,
            trade_metrics,
        )

    def generate_daily_report(self) -> str:
        """
        Génère un rapport quotidien d'activités en assemblant plusieurs sections.
        """
        self.logger.info("Génération du rapport quotidien.")

        start_of_day_utc = datetime.now(UTC).replace(
            hour=0, minute=0, second=0, microsecond=0
        )

        decisions_today = [
            log
            for log in self._audit_trail
            if datetime.fromisoformat(log["timestamp"]).astimezone(UTC).date()
            == start_of_day_utc.date()
        ]

        trade_decision_lines, trade_metrics_summary = (
            self._generate_report_trade_decisions(decisions_today)
        )

        report_parts = [
            self._generate_report_header(start_of_day_utc.date()),
            self._generate_report_config_changes(start_of_day_utc),
            trade_decision_lines,
        ]

        full_report_content = "\n".join(
            part for parts_list in report_parts for part in parts_list
        )
        full_report_content += "\n\n--- Fin du Rapport ---"

        self.logger.info("Rapport quotidien généré.")

        report_filename = f"daily_report_{start_of_day_utc.strftime('%Y%m%d')}.md"
        report_path = self.get("paths.reports", "output/")
        full_report_filepath = Path(report_path) / report_filename

        temp_filepath = full_report_filepath.with_suffix(".tmp")
        try:
            with open(temp_filepath, "w", encoding="utf-8") as f:
                f.write(full_report_content)
            temp_filepath.rename(full_report_filepath)
            self.logger.info(
                f"Rapport quotidien sauvegardé avec succès dans '{full_report_filepath}'."
            )

            if self.get("telegram.channels.telegram_daily_report", False):
                message_summary = (
                    f"📊 **Rapport Quotidien SNIPER_X - {start_of_day_utc.date()}**\n\n"
                )
                message_summary += (
                    f"Trades exécutés: {trade_metrics_summary.get('total_trades', 0)}\n"
                )
                message_summary += (
                    f"P&L Total: `${trade_metrics_summary.get('total_pnl', 0.0):.2f}`\n"
                )
                message_summary += (
                    f"Win Rate: `{trade_metrics_summary.get('win_rate', 0.0):.2f}%`"
                )

                message_summary = message_summary[:4000]
                self.send_alert(message_summary, "telegram_daily_report")
        except Exception as e:
            self.logger.error(
                f"Échec de la sauvegarde du rapport quotidien vers '{full_report_filepath}': {e}",
                exc_info=True,
            )
            if temp_filepath.exists():
                temp_filepath.unlink()
            self.log_decision(
                self.get_current_dynamic_config(),
                {},
                {"report_date": start_of_day_utc.isoformat()},
                f"Échec de la génération du rapport quotidien: {e}",
            )

        return full_report_content

    def export_audit_trail(self, path: str) -> None:
        """
        Exporte le journal d'audit complet de manière atomique vers un fichier.
        """
        self.logger.info(f"Export du journal d'audit vers {path}")
        if not hasattr(self, "_audit_trail") or not self._audit_trail:
            self.logger.warning(
                "Aucune donnée dans le journal d'audit à exporter. Opération annulée."
            )
            return

        output_path = Path(path)
        temp_path = output_path.with_suffix(output_path.suffix + ".tmp")

        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)

            with open(temp_path, "w", encoding="utf-8") as f:
                file_extension = output_path.suffix.lower()
                if file_extension == ".jsonl":
                    for entry in self._audit_trail:
                        f.write(json.dumps(entry, cls=CustomJSONEncoder) + "\n")
                elif file_extension == ".csv":
                    df = pd.json_normalize(self._audit_trail)
                    df.to_csv(f, index=False, float_format="%.5f")
                else:
                    raise ValueError(
                        f"Format d'export non supporté pour le journal d'audit : {file_extension}"
                    )

            temp_path.rename(output_path)
            self.logger.info(f"Journal d'audit exporté avec succès vers {output_path}.")

            audit_log_rotation_settings = self.get("app.audit_log_rotation", {})
            if audit_log_rotation_settings.get("enabled", False):
                self._rotate_audit_logs(output_path.parent, audit_log_rotation_settings)

        except Exception as e:
            self.logger.error(
                f"Erreur lors de l'export du journal d'audit vers {path}: {e}",
                exc_info=True,
            )
            if temp_path.exists():
                temp_path.unlink()
            self.send_alert(
                f"CRITIQUE: Échec de l'export du journal d'audit: {e}",
                "telegram_critical",
            )

    def _rotate_audit_logs(
        self, log_dir: Path, rotation_settings: Dict[str, Any]
    ) -> None:
        """
        Gère la rotation des fichiers de journal d'audit par jour ou par taille.
        """
        rotation_type = rotation_settings.get("type", "daily")
        max_files = rotation_settings.get("max_files", 30)
        max_size_mb = rotation_settings.get("max_size_mb", 100)

        self.logger.info(
            f"Démarrage de la rotation des logs d'audit dans {log_dir} (Type: {rotation_type})."
        )

        audit_file_name_pattern = (
            self.get(
                "trade_executor_settings.audit_trail_file_name", "trade_audit_trail.log"
            ).split(".")[0]
            + "*"
        )

        audit_files = sorted(
            log_dir.glob(f"{audit_file_name_pattern}*"), key=os.path.getmtime
        )

        if rotation_type == "daily":
            if len(audit_files) > max_files:
                num_to_delete = len(audit_files) - max_files
                for old_log_file in audit_files[:num_to_delete]:
                    old_log_file.unlink()
                    self.logger.info(
                        f"Ancien log d'audit (quotidien) supprimé: {old_log_file.name}"
                    )

        elif rotation_type == "size":
            for log_file in audit_files:
                if log_file.stat().st_size > max_size_mb * 1024 * 1024:
                    self.logger.warning(
                        f"Log d'audit '{log_file.name}' dépasse la taille max ({max_size_mb}MB). Renommage pour rotation..."
                    )
                    rotated_name = log_file.with_suffix(
                        f".{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}{log_file.suffix}.rotated"
                    )
                    log_file.rename(rotated_name)
                    self.logger.info(
                        f"Log d'audit '{log_file.name}' pivoté vers '{rotated_name.name}'."
                    )

        self.logger.info(
            f"Rotation des logs d'audit terminée. Nombre de fichiers restants : {len(list(log_dir.glob(f'{audit_file_name_pattern}*')))}"
        )