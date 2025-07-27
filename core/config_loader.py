# core/config_loader.py

import os
import json
import yaml
import re
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional
from core.utils import ConfigValidationError

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

        # --- Chargement du fichier `broker_accounts.json` EN PREMIER ---
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
            # Note : C'est une erreur FATALE si env_vars_schema.json est manquant en production.
            self.logger.critical(
                f"FATAL: Le schéma 'env_vars_schema.json' est introuvable à '{env_schema_path}'. Impossible de charger les variables d'environnement en production. Le bot ne peut pas démarrer en toute sécurité."
            )
            raise FileNotFoundError(f"Schéma des variables d'environnement manquant : {env_schema_path}")

        env_vars_dict = {}
        # Collecter tous les noms de variables d'environnement nécessaires
        # Inclure ceux du schéma et ceux des comptes brokers
        vars_to_load_from_env = set(all_env_vars_from_schema)

        # AJOUT : Récupérer dynamiquement les noms des variables MT5_LOGIN/PASSWORD_ENV_VAR depuis broker_accounts_config
        for account in broker_accounts_config.get("accounts", []):
            login_var = account.get("login_env_var")
            password_var = account.get("password_env_var")
            server_var = account.get("server_env_var") # Si vous avez des variables pour le serveur aussi

            if login_var:
                vars_to_load_from_env.add(login_var)
            if password_var:
                vars_to_load_from_env.add(password_var)
            if server_var: # Ajouter si pertinent
                vars_to_load_from_env.add(server_var)

        # Utilisation de os.getenv pour charger les variables d'environnement
        for var_name in vars_to_load_from_env:
            value = os.getenv(var_name)
            if var_name == "BOT_MODE": # Gérer BOT_MODE spécifiquement si nécessaire
                env_vars_dict[var_name] = value.upper() if value else "DEMO"
            elif value is not None:
                env_vars_dict[var_name] = value
            else:
                self.logger.debug(f"Variable d'environnement '{var_name}' n'est pas définie dans l'environnement.")


        base_configs["env_vars"] = env_vars_dict
        self.logger.info(f"{len(env_vars_dict)} variables d'environnement (incluant les MT5) chargées dans la configuration de base.")

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
        determined_schema_name: Optional[str] = schema_name
        if determined_schema_name is None:
            if "project" in config and config.get("project") == "SNIPER_X":
                # Le schéma main_app_schema.json n'existe pas, donc nous ne le recherchons pas.
                # Suppression de la ligne: determined_schema_name = "main_app_schema.json"
                pass # Ne tente rien ici si le schéma n'est pas censé exister.
            elif "strategy_name" in config:
                determined_schema_name = "strategy_schema.json" # Celui-ci existe dans config/schemas/
            elif "accounts" in config and isinstance(config.get("accounts"), list):
                # Le schéma broker_accounts_schema.json n'existe pas, donc nous ne le recherchons pas.
                # Suppression de la ligne: determined_schema_name = "broker_accounts_schema.json"
                pass # Ne tente rien ici.
            # Aucune détermination automatique pour phase_observer_config ou telegram_config ici
            # car ils sont chargés via leur chemin spécifique dans ConfigManager.initialize_dynamic_config.

        # Liste des schémas qui DOIVENT exister et être validés.
        # Tout autre nom sera ignoré sans avertissement car il n'est pas censé exister.
        known_existing_schemas = [
            "strategy_schema.json", # Existe bien dans config/schemas/
            "env_vars_schema.json"  # Existe bien dans config/
            # Ajoutez ici d'autres schémas réels si vous les créez plus tard.
        ]

        # Si le schéma déterminé n'est pas dans la liste des schémas connus existants,
        # ou si aucun schéma n'a été déterminé (determined_schema_name est toujours None après la détermination),
        # alors on ignore la validation sans erreur ni avertissement.
        if determined_schema_name not in known_existing_schemas:
            self.logger.debug(f"Schéma '{determined_schema_name}' n'est pas un schéma connu pour la validation. Validation ignorée.")
            return True # Ne pas lever d'erreur ni d'avertissement, car il n'est pas censé exister.

        # Si le schéma est dans known_existing_schemas, on procède au chargement et à la validation.
        schema_to_use = self._schema_cache.get(determined_schema_name)
        schema_path: Path # Déclaration pour garantir qu'elle est définie.

        if determined_schema_name == "strategy_schema.json":
            schema_path = Path(__file__).parent.parent / "config" / "schemas" / determined_schema_name
        elif determined_schema_name == "env_vars_schema.json":
            schema_path = Path(__file__).parent.parent / "config" / determined_schema_name
        else:
            # Cette branche ne devrait pas être atteinte avec la logique ci-dessus.
            # Si par un cas inattendu un determined_schema_name non reconnu parvient ici,
            # on considère cela comme une erreur de logique.
            self.logger.critical(f"Erreur logique: Chemin du schéma non géré pour '{determined_schema_name}'.")
            return False # Ne devrait pas arriver avec la logique ci-dessus.


        if schema_to_use is None:
            if not schema_path.is_file():
                # Cette erreur se produit si un SCHEMA CENSÉ EXISTER est manquant.
                self.logger.critical(
                    f"FATAL: Fichier de schéma de validation '{determined_schema_name}' introuvable à '{schema_path}'. Impossible d'assurer la conformité de la configuration. Le bot ne peut pas démarrer en toute sécurité."
                )
                raise FileNotFoundError(f"Fichier de schéma de validation manquant : {schema_path}")

            try:
                with open(schema_path, "r", encoding="utf-8") as f:
                    schema_to_use = json.load(f)
                self._schema_cache[determined_schema_name] = schema_to_use
                self.logger.debug(f"Schéma '{determined_schema_name}' chargé et mis en cache.")
            except json.JSONDecodeError as e:
                # Si le schéma existe mais est malformé
                self.logger.critical(
                    f"FATAL: Erreur de syntaxe JSON dans le fichier de schéma '{determined_schema_name}': {e}. Le bot ne peut pas démarrer. Veuillez corriger le schéma."
                )
                raise ConfigValidationError(f"Schéma '{determined_schema_name}' invalide : {e}") from e
            except Exception as e:
                # Si une autre erreur survient lors du chargement d'un schéma existant
                self.logger.critical(
                    f"FATAL: Erreur lors du chargement du schéma '{determined_schema_name}': {e}. Le bot ne peut pas démarrer."
                )
                raise RuntimeError(f"Erreur lors du chargement du schéma '{determined_schema_name}'") from e
        
        # Si le schéma a été chargé avec succès ou était en cache, procéder à la validation
        try:
            jsonschema.validate(instance=config, schema=schema_to_use)
            self.logger.debug(f"La configuration a passé la validation avec le schéma '{determined_schema_name}'.")
            return True
        except jsonschema.ValidationError as e:
            # Si la validation échoue contre un schéma EXISTANT et valide
            error_message = f"Échec de la validation par schéma '{determined_schema_name}': {e.message} (sur le champ: `{''.join(e.path)}`)"
            self.logger.critical(
                f"FATAL: La configuration a échoué à la validation du schéma '{determined_schema_name}'. "
                "Ceci indique une configuration incorrecte. Le bot ne peut pas démarrer: {error_message}",
                exc_info=True
            )
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

    def load_asset_config(self, asset_symbol: str) -> Dict[str, Any]:
        """
        Charge la configuration spécifique à un actif (symbol) depuis le dossier `config/assets_config/`.

        Args:
            asset_symbol (str): Le symbole de l'actif (ex: "EURUSD", "BTCUSD").

        Returns:
            Dict[str, Any]: Le dictionnaire de configuration de l'actif.

        Raises:
            FileNotFoundError: Si le fichier de configuration de l'actif n'est pas trouvé.
            IOError: Pour d'autres erreurs de lecture ou de parsing.
        """
        if not self.config_manager:
            self.logger.error("ConfigManager non disponible dans ConfigLoader. Impossible de charger les chemins d'actifs.")
            raise RuntimeError("ConfigManager est requis pour load_asset_config.")

        # Récupérer le chemin de base des configurations d'actifs depuis ConfigManager
        asset_configs_dir = Path(self.config_manager.get("paths.asset_configs", "config/assets_config/"))
        
        # Construire le chemin complet du fichier de configuration de l'actif
        asset_config_path = asset_configs_dir / f"{asset_symbol}.json"

        self.logger.info(f"Tentative de chargement de la configuration pour l'actif '{asset_symbol}' depuis '{asset_config_path}'...")

        if not asset_config_path.is_file():
            self.logger.critical(f"Fichier de configuration d'actif introuvable: {asset_config_path}. Impossible de charger la configuration pour cet actif.")
            raise FileNotFoundError(f"Fichier de configuration d'actif manquant pour '{asset_symbol}': {asset_config_path}")

        try:
            # Utiliser la méthode générique load_dynamic_config pour charger et valider le JSON
            # Note: Il n'y a pas de schéma spécifique pour les configs d'actifs dans la structure fournie.
            # La validation sera donc ignorée (log WARNING dans validate_config) si aucun schéma n'est trouvé.
            config = self.load_dynamic_config(str(asset_config_path))
            self.logger.info(f"Configuration pour l'actif '{asset_symbol}' chargée avec succès.")
            return config
        except ConfigValidationError as e:
            self.logger.error(f"Validation de la configuration de l'actif '{asset_symbol}' échouée: {e}")
            raise # Propage l'exception de validation
        except Exception as e:
            self.logger.error(f"Erreur inattendue lors du chargement de la configuration pour l'actif '{asset_symbol}': {e}", exc_info=True)
            raise IOError(f"Impossible de charger la configuration pour l'actif '{asset_symbol}'") from e