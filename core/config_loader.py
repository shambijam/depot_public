# core/config_loader.py

import os
import json
import yaml
import re
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional
from core.config_manager import ConfigValidationError # Maintenue pour la validation

logger = logging.getLogger(__name__)

class ConfigLoader:
    """
    Gère le chargement et le parsing des fichiers de configuration (JSON, YAML, .set)
    et des variables d'environnement.
    """
    def __init__(self, config_manager_instance=None):
        """
        Initialise le ConfigLoader.
        Args:
            config_manager_instance: L'instance du ConfigManager (pour accéder à ses méthodes comme get, send_alert, logger).
                                     Ceci crée une dépendance circulaire qui devra être gérée par injection ou par refonte plus poussée.
                                     Pour l'instant, on la garde pour assurer le fonctionnement des logs et alertes.
        """
        self.config_manager = config_manager_instance
        self.logger = logging.getLogger(__name__)
        # Configurer le logger spécifiquement pour ce module si nécessaire,
        # ou s'appuyer sur la configuration globale définie par main.py
        # ou injecter le logger de config_manager.

    def load_base_configs(self) -> Dict[str, Any]:
        """
        Charge les configurations de base, prioritairement les variables d'environnement
        et les configurations spécifiques des comptes brokers.

        Returns:
            Dict[str, Any]: Un dictionnaire contenant les configurations de base chargées,
                            incluant 'env_vars' et '_broker_accounts_config'.
        Raises:
            FileNotFoundError: Si un fichier de configuration essentiel (schéma ou comptes brokers) est introuvable.
            json.JSONDecodeError: Si un fichier JSON est malformé.
            Exception: Pour toute autre erreur inattendue lors du chargement.
        """
        self.logger.info("Chargement des configurations de base (variables d'environnement et comptes brokers)...")

        base_configs: Dict[str, Any] = {}
        broker_accounts_config: Dict[str, Any] = {"accounts": []}

        # --- Chargement des variables d'environnement (filtrées) ---
        env_schema_path = Path(__file__).parent.parent / "config" / "env_vars_schema.json"
        all_env_vars_from_schema: List[str] = []

        if env_schema_path.exists():
            try:
                with open(env_schema_path, "r", encoding="utf-8") as f:
                    schema = json.load(f)
                    all_env_vars_from_schema = schema.get("required_env_vars", [])
                self.logger.info(f"Noms des variables d'environnement chargés depuis le schéma : {env_schema_path}")
            except json.JSONDecodeError as e:
                self.logger.error(
                    f"Erreur de syntaxe JSON dans le schéma '{env_schema_path}': {e}. Le chargement des variables d'environnement va échouer.",
                    exc_info=True,
                )
                all_env_vars_from_schema = []
        else:
            self.logger.critical(
                f"FATAL: Le schéma 'env_vars_schema.json' est introuvable à '{env_schema_path}'. Impossible de charger les variables d'environnement en production. Le bot ne peut pas démarrer en toute sécurité."
            )
            raise FileNotFoundError(f"Schéma des variables d'environnement manquant : {env_schema_path}")

        env_vars_dict = {}
        mt5_generic_env_vars = [
            "MT5_LOGIN_LIVE", "MT5_PASSWORD_LIVE", "MT5_SERVER_LIVE",
            "MT5_LOGIN_DEMO", "MT5_PASSWORD_DEMO", "MT5_SERVER_DEMO",
        ]

        # Utilisation de os.getenv pour charger les variables d'environnement
        for var_name in all_env_vars_from_schema:
            if var_name in mt5_generic_env_vars:
                self.logger.debug(f"Variable d'environnement '{var_name}' ignorée : gérée via 'broker_accounts.json'.")
                continue

            value = os.getenv(var_name)
            if var_name == "BOT_MODE":
                env_vars_dict[var_name] = value.upper() if value else "DEMO"
            elif value is not None:
                env_vars_dict[var_name] = value

        base_configs["env_vars"] = env_vars_dict
        self.logger.info(f"{len(env_vars_dict)} variables d'environnement chargées dans la configuration de base.")

        # --- Chargement du fichier `broker_accounts.json` ---
        broker_accounts_path = Path(__file__).parent.parent / "config" / "broker_accounts.json"

        if broker_accounts_path.exists():
            try:
                with open(broker_accounts_path, "r", encoding="utf-8") as f:
                    broker_accounts_config = json.load(f)
                self.logger.info(f"Comptes brokers chargés depuis '{broker_accounts_path}'.")
                # TODO: Ajouter une validation de schéma pour broker_accounts.json pour garantir la structure.
            except json.JSONDecodeError as e:
                self.logger.critical(
                    f"FATAL: Erreur de syntaxe JSON dans le fichier des comptes brokers '{broker_accounts_path}': {e}. Impossible de continuer.",
                    exc_info=True,
                )
                raise # Relancer car c'est une erreur bloquante
            except Exception as e:
                self.logger.critical(
                    f"FATAL: Erreur lors du chargement du fichier des comptes brokers '{broker_accounts_path}': {e}. Impossible de continuer.",
                    exc_info=True,
                )
                raise # Relancer car c'est une erreur bloquante
        else:
            self.logger.critical(
                f"FATAL: Le fichier des comptes brokers '{broker_accounts_path}' est introuvable. Impossible de se connecter à MetaTrader. Le bot ne peut pas démarrer."
            )
            raise FileNotFoundError(f"Fichier des comptes brokers manquant : {broker_accounts_path}")

        base_configs["_broker_accounts_config"] = broker_accounts_config
        return base_configs

    # Ces méthodes étaient dans ConfigManager et sont déplacées ici.
    def parse_set_file(self, set_path: str) -> Dict[str, Any]:
        """
        Parse un fichier .set de MetaTrader 5, en gérant les sections et en inférant les types.
        """
        self.logger.debug(f"Parsing du fichier .set MT5 : {set_path}")
        config = {}
        current_section = "general"
        config[current_section] = {}
        with open(set_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith(";"):
                    continue

                if line.startswith("[") and line.endswith("]"):
                    current_section = line[1:-1]
                    if current_section not in config:
                        config[current_section] = {}
                else:
                    parts = line.split("=", 1)
                    if len(parts) == 2:
                        key = parts[0].strip()
                        value_str = parts[1].strip()

                        if value_str.lower() == "true":
                            value = True
                        elif value_str.lower() == "false":
                            value = False
                        elif value_str.isdigit():
                            value = int(value_str)
                        elif re.match(r"^-?\d+\.\d+$", value_str):
                            value = float(value_str)
                        else:
                            value = value_str
                        config[current_section][key] = value
        self.logger.debug(f"Parsing de {set_path} réussi.")
        return config

    def parse_json_config(self, json_path: str) -> Dict[str, Any]:
        """
        Parse un fichier de configuration JSON.
        """
        self.logger.debug(f"Parsing du fichier de configuration JSON : {json_path}")
        with open(json_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        self.logger.debug(f"Parsing de {json_path} réussi.")
        return config

    def parse_yaml_config(self, yaml_path: str) -> Dict[str, Any]:
        """
        Parse un fichier de configuration YAML.
        """
        self.logger.debug(f"Parsing du fichier de configuration YAML : {yaml_path}")
        with open(yaml_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
        self.logger.debug(f"Parsing de {yaml_path} réussi.")
        return config

    def load_dynamic_config(self, config_path: str, schema_name: Optional[str] = None) -> Dict[str, Any]:
        """
        Charge la configuration dynamique depuis un chemin spécifié, la parse et la valide.
        Cette méthode peut également être utilisée pour charger des configurations modulaires.

        Args:
            config_path (str): Le chemin vers le fichier de configuration dynamique.
            schema_name (str, optional): Le nom du schéma JSON à utiliser pour la validation.
                                         Si None, la méthode tente de déterminer le schéma.

        Returns:
            Dict[str, Any]: Le dictionnaire de configuration chargé et validé.

        Raises:
            ConfigValidationError: Si la configuration échoue aux validations critiques.
            FileNotFoundError: Si le fichier de configuration n'est pas trouvé.
        """
        config_path_obj = Path(config_path)
        if not config_path_obj.is_file():
            raise FileNotFoundError(f"Fichier de configuration dynamique introuvable: {config_path}")

        file_extension = config_path_obj.suffix.lower()
        config = {}
        try:
            if file_extension == ".json":
                config = self.parse_json_config(str(config_path_obj))
            elif file_extension in [".yaml", ".yml"]:
                config = self.parse_yaml_config(str(config_path_obj))
            elif file_extension == ".set":
                config = self.parse_set_file(str(config_path_obj))
            else:
                raise ValueError(f"Type de fichier de configuration non supporté: {file_extension}")

            self.validate_config(config, schema_name)

            self.logger.info(f"Configuration dynamique chargée et validée depuis {config_path_obj}.")
            return config
        except (ConfigValidationError, json.JSONDecodeError) as e:
            self.logger.error(f"Validation de la configuration échouée pour {config_path_obj}: {e}")
            raise # Propage l'exception
        except Exception as e:
            self.logger.error(
                f"Erreur lors du chargement de la configuration dynamique depuis {config_path_obj}: {e}",
                exc_info=True,
            )
            raise IOError(f"Impossible de charger la configuration depuis {config_path_obj}") from e

    # Déplacer _schema_cache ici aussi car il est utilisé par validate_config
    _schema_cache: Dict[str, Dict[str, Any]] = {}

    def validate_config(self, config: Dict[str, Any], schema_name: Optional[str] = None) -> bool:
        """
        Valide un dictionnaire de configuration en utilisant un schéma JSON formel.
        Déplacée de ConfigManager.
        """
        import jsonschema # Importation locale pour cette fonction, si non déjà globale
        self.logger.debug("Validation de la configuration par schéma...")

        # Déterminer le type de schéma à appliquer si non fourni
        if schema_name is None:
            if "project" in config and config.get("project") == "SNIPER_X":
                schema_name = "main_app_schema.json"
            elif "strategy_name" in config:
                schema_name = "strategy_schema.json"
            elif "accounts" in config and isinstance(config.get("accounts"), list):
                schema_name = "broker_accounts_schema.json"
            else:
                raise ConfigValidationError("Type de configuration inconnu. 'project', 'strategy_name' ou 'accounts' manquant pour déterminer le schéma.")

        schema = self._schema_cache.get(schema_name)
        if schema is None:
            # CHEMIN CORRIGÉ POUR LES SCHÉMAS
            schema_path = Path(__file__).parent.parent / "config" / "schemas" / schema_name
            if not schema_path.is_file():
                self.logger.critical(f"FATAL: Fichier de schéma de validation '{schema_name}' introuvable à '{schema_path}'. Impossible d'assurer la conformité de la configuration. Le bot ne peut pas démarrer en toute sécurité.")
                raise FileNotFoundError(f"Fichier de schéma de validation manquant : {schema_path}")

            try:
                with open(schema_path, "r", encoding="utf-8") as f:
                    schema = json.load(f)
                self._schema_cache[schema_name] = schema
                self.logger.debug(f"Schéma '{schema_name}' chargé et mis en cache.")
            except json.JSONDecodeError as e:
                self.logger.critical(f"FATAL: Erreur de syntaxe JSON dans le fichier de schéma '{schema_name}': {e}. Le bot ne peut pas démarrer.", exc_info=True)
                raise ConfigValidationError(f"Schéma '{schema_name}' invalide : {e.msg}") from e
            except Exception as e:
                self.logger.critical(f"FATAL: Erreur lors du chargement du schéma '{schema_name}': {e}. Le bot ne peut pas démarrer.", exc_info=True)
                raise RuntimeError(f"Erreur lors du chargement du schéma '{schema_name}'") from e

        try:
            jsonschema.validate(instance=config, schema=schema)
            self.logger.debug(f"La configuration a passé la validation avec le schéma '{schema_name}'.")
            return True
        except jsonschema.ValidationError as e:
            error_message = f"Échec de la validation par schéma '{schema_name}': {e.message} (sur le champ: `{''.join(e.path)}`)"
            self.logger.error(error_message, exc_info=True)
            raise ConfigValidationError(error_message) from e

    # _detect_separator est également déplacé ici.
    def _detect_separator(self, file_path: str) -> str:
        """
        Détecte le séparateur (virgule, point-virgule, tabulation) d'un fichier CSV.
        """
        # Note: Cette méthode dépend maintenant de self.config_manager pour obtenir separators_to_test.
        # Si ConfigLoader est instancié sans ConfigManager, cela posera problème.
        # Pour le moment, on utilise une valeur par défaut.
        separators_to_test = self.config_manager.get("mt5_connector_settings.csv_separators_to_test", [";", ",", "\t"]) if self.config_manager else [";", ",", "\t"]

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                first_line = f.readline()
                for sep in separators_to_test:
                    if sep in first_line:
                        self.logger.debug(f"Séparateur '{sep}' détecté pour '{file_path}'.")
                        return sep
                self.logger.warning(f"Impossible de détecter le séparateur pour '{file_path}'. Utilisation de la virgule par défaut.")
                return ","
        except Exception as e:
            self.logger.error(f"Erreur lors de la détection du séparateur pour '{file_path}': {e}", exc_info=True)
            return ","