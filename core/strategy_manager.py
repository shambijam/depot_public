# core/strategy_manager.py

import logging
import importlib.util
import inspect
from pathlib import Path
from typing import Dict, Any, Optional, Type, TYPE_CHECKING
from core.config_loader import ConfigLoader, ConfigValidationError
import sys  # Nécessaire pour StreamHandler
import traceback  # Import local nécessaire pour capturer les traces d'erreur
from datetime import (
    datetime,
    UTC,
)  # Imports locaux pour les timestamps dans le log de debug
from pathlib import Path  # Import local pour manipuler les chemins de fichiers

# Utilisation de TYPE_CHECKING pour éviter les importations circulaires à l'exécution
if TYPE_CHECKING:
    from core.config_manager import (
        ConfigManager,
    )  # Importation uniquement pour les hints de type

# Importer BaseStrategy depuis la structure du projet
try:
    from strategy.base_strategy import BaseStrategy
except ImportError:
    # Si BaseStrategy n'est pas trouvée, c'est une erreur critique
    logging.critical(
        "FATAL: BaseStrategy introuvable. Vérifiez que le module 'strategy.base_strategy' existe et est accessible."
    )
    raise ImportError("BaseStrategy introuvable. Le système ne peut pas démarrer.")


class StrategyManager:
    """
    Gère le chargement dynamique des stratégies, leur mappage
    et leur intégration dans la configuration du bot.
    """


    def __init__(
        self,
        config_loader_instance: ConfigLoader,
        config_manager_instance: "ConfigManager",
    ):
        """
        Initialise le StrategyManager.

        Args:
            config_loader_instance (ConfigLoader): Instance de ConfigLoader pour parser les fichiers.
            config_manager_instance (ConfigManager): Instance de ConfigManager pour accéder
                                                    à la configuration globale et mettre à jour
                                                    la configuration dynamique.
        """
        self.config_loader = config_loader_instance
        self.config_manager = config_manager_instance
        self.logger = logging.getLogger(__name__)

        # FORCE la configuration du logger pour StrategyManager.
        # Cela garantit que les messages DEBUG sont vus, quelle que soit la configuration globale.
        self.logger.setLevel(logging.DEBUG)
        # Supprimer les handlers existants pour éviter la duplication des logs.
        if self.logger.hasHandlers():
            self.logger.handlers.clear()
        # Ajouter un StreamHandler pour envoyer les logs à la console.
        console_handler = logging.StreamHandler(sys.stdout)
        formatter = logging.Formatter(
            "%(asctime)s - [%(name)s] - %(levelname)s - %(message)s"
        )
        console_handler.setFormatter(formatter)
        self.logger.addHandler(console_handler)
        self.logger.propagate = False  # Empêche la double propagation au logger racine.

        self.strategy_registry: Dict[str, Dict[str, Any]] = {}
       

        # self.load_all_strategies() # RETIRÉ : L'appel est prématuré et cause l'erreur de chargement.

        self.logger.info(
            "StrategyManager initialisé (les stratégies ne sont pas encore chargées)."
        )
    def get_strategy(self, name: str):
        """
        Retourne l'instance de stratégie par son nom.
        """
        if not hasattr(self, "loaded_strategies"):
            return None
        return self.loaded_strategies.get(name)
 

    def initialize_strategies(self) -> None:
        """
        Charge toutes les stratégies après que la configuration principale ait été initialisée.
        Cette méthode sert de point d'entrée pour le chargement post-initialisation.
        """
        self.load_all_strategies()
        self.logger.info(
            "StrategyManager a maintenant chargé toutes les stratégies disponibles."
        )

    def load_all_strategies(self) -> None:
        """
        Charge toutes les stratégies définies dans le mapping de configuration au démarrage
        et les met en cache dans `strategy_registry`.
        """
        self.logger.info(
            "Chargement de toutes les stratégies définies dans le mapping..."
        )
        config_dir_path = Path(
            self.config_manager.get("paths.strategy_configs", "config/strategy/")
        )
        config_mapping = self.config_manager.get("strategies.config_mapping", {})

        if not config_dir_path.is_dir():
            self.logger.warning(
                f"[load_all_strategies] Le répertoire '{config_dir_path}' n'existe pas. Aucune stratégie à charger."
            )
            return

        loaded_count = 0
        for strategy_key, config_file in config_mapping.items():
            config_path = config_dir_path / config_file
            if not config_path.is_file():
                self.logger.warning(
                    f"[load_all_strategies] Fichier de configuration '{config_path}' manquant pour '{strategy_key}'."
                )
                continue

            try:
                config = self.config_loader.load_dynamic_config(
                    str(config_path), schema_name="strategy_schema.json"
                )

                self.logger.debug(
                    f"[load_all_strategies] Tente de charger la classe Python pour la clé '{strategy_key}' associée au fichier '{config_file}'."
                )

                strategy_class = self._load_strategy_class(strategy_key)
                if strategy_class is None:
                    self.logger.error(
                        f"[load_all_strategies] Impossible de charger la classe pour '{strategy_key}'. Stratégie ignorée."
                    )
                    continue

                strategy_name_from_config = config.get("strategy_name")
                if not strategy_name_from_config:
                    self.logger.error(
                        f"[load_all_strategies] La configuration '{config_file}' pour la clé '{strategy_key}' ne contient pas de 'strategy_name'. Elle ne sera pas ajoutée au registre."
                    )
                    continue

                if strategy_name_from_config in self.strategy_registry:
                    self.logger.warning(
                        f"[load_all_strategies] Stratégie '{strategy_name_from_config}' déjà présente dans le registre. Mise à jour forcée."
                    )

                self.strategy_registry[strategy_name_from_config] = {
                    "config": config,
                    "class": strategy_class,
                    "last_modified": config_path.stat().st_mtime,
                }
               
                self.logger.debug(
                    f"[load_all_strategies] Stratégie '{strategy_name_from_config}' (clé de mapping: '{strategy_key}') chargée depuis '{config_file}'. Classe: {strategy_class}."
                )
                loaded_count += 1

            except ConfigValidationError as e:
                self.logger.error(
                    f"[load_all_strategies] Erreur de validation pour la stratégie '{strategy_key}' : {str(e)}"
                )
            except FileNotFoundError:
                self.logger.error(
                    f"[load_all_strategies] Fichier de configuration introuvable : {config_path}"
                )
            except Exception as e:
                self.logger.error(
                    f"[load_all_strategies] Erreur lors du chargement de '{strategy_key}' : {str(e)}",
                    exc_info=True,
                )

        self.logger.info(
            f"[load_all_strategies] {loaded_count} stratégies chargées avec succès sur {len(config_mapping)} mappées."
        )

    def _load_strategy_class(self, strategy_key: str) -> Optional[Type]:
        """
        Charge dynamiquement la classe Python d'une stratégie à partir de sa clé.

        Args:
            strategy_key (str): Clé unique de la stratégie.

        Returns:
            Optional[Type]: La classe Python de la stratégie, ou None si introuvable.
        """

        # Chemin du fichier de log temporaire pour le diagnostic
        debug_log_file_path = Path("logs/strategy_manager_debug.log")

        # Assurez-vous que le répertoire 'logs' existe
        debug_log_file_path.parent.mkdir(parents=True, exist_ok=True)

        # Fonction imbriquée pour écrire les logs de debug dans le fichier
        # Indentation corrigée pour cette fonction imbriquée
        def write_debug_log(message: str):
            """Écrit un message de débogage directement dans le fichier temporaire."""
            try:
                with open(debug_log_file_path, "a", encoding="utf-8") as f:
                    f.write(
                        f"[{datetime.now(UTC).isoformat()}] [{strategy_key}] {message}\n"
                    )
            except Exception as e:
                # Fallback pour logger en console si l'écriture fichier échoue (problème de permissions, etc.)
                self.logger.error(
                    f"Échec de l'écriture dans le fichier de debug de stratégie : {e}. Message : {message}"
                )

        write_debug_log(f"Début _load_strategy_class pour clé '{strategy_key}'.")

        try:
            python_module_name = strategy_key.lower().replace(" ", "_")
            module_path = (
                Path(__file__).parent.parent / "strategy" / f"{python_module_name}.py"
            )

            self.logger.debug(
                f"[_load_strategy_class] Recherche du module Python pour la clé '{strategy_key}'. Chemin attendu: '{module_path}'."
            )
            write_debug_log(
                f"Recherche du module Python. Chemin attendu: '{module_path}'."
            )

            if not module_path.is_file():
                self.logger.warning(
                    f"[_load_strategy_class] Module Python '{python_module_name}.py' introuvable à '{module_path}' pour la stratégie '{strategy_key}'."
                )
                write_debug_log(
                    f"Module Python '{python_module_name}.py' introuvable à '{module_path}'."
                )
                return None

            module_name = f"strategy.{python_module_name}"
            spec = importlib.util.find_spec(module_name)

            if spec is None or spec.loader is None:
                self.logger.error(
                    f"[_load_strategy_class] Spécification ou chargeur de module introuvable pour '{module_name}'. Impossible d'importer la stratégie '{strategy_key}'."
                )
                write_debug_log(
                    f"Spécification ou chargeur de module introuvable pour '{module_name}'."
                )
                return None

            module = importlib.util.module_from_spec(spec)

            try:
                spec.loader.exec_module(module)
                self.logger.debug(
                    f"[_load_strategy_class] Module '{module_name}' exécuté avec succès."
                )
                write_debug_log(f"Module '{module_name}' exécuté avec succès.")
            except Exception as module_exec_e:
                error_trace = traceback.format_exc()
                self.logger.error(
                    f"[_load_strategy_class] Erreur lors de l'exécution du module '{module_name}' pour la stratégie '{strategy_key}': {module_exec_e}",
                    exc_info=True,
                )
                write_debug_log(
                    f"ERREUR lors de l'exécution du module '{module_name}': {module_exec_e}. Trace: {error_trace}"
                )
                if self.config_manager:
                    self.config_manager.send_alert(
                        f"CRITIQUE: Erreur chargement module stratégie '{strategy_key}': {module_exec_e}",
                        "telegram_critical",
                    )
                return None

            strategy_class_name = (
                "".join([s.capitalize() for s in python_module_name.split("_")])
                + "Strategy"
            )
            strategy_class = getattr(module, strategy_class_name, None)

            self.logger.debug(
                f"[_load_strategy_class] Nom de classe attendu pour '{strategy_key}': '{strategy_class_name}'. Classe trouvée via getattr: '{strategy_class}'"
            )
            write_debug_log(
                f"Nom de classe attendu: '{strategy_class_name}'. Classe trouvée via getattr: '{strategy_class}'"
            )

            if (
                strategy_class
                and issubclass(strategy_class, BaseStrategy)
                and strategy_class is not BaseStrategy
            ):
                self.logger.debug(
                    f"[_load_strategy_class] Classe '{strategy_class_name}' chargée et validée avec succès pour '{strategy_key}'."
                )
                write_debug_log(
                    f"Classe '{strategy_class_name}' chargée et validée avec succès."
                )
                return strategy_class
            else:
                if strategy_class is None:
                    self.logger.error(
                        f"[_load_strategy_class] Classe '{strategy_class_name}' introuvable dans le module '{module_name}' pour la stratégie '{strategy_key}'. Vérifiez le nom de la classe dans le fichier Python ou si elle est bien exportée dans __init__.py du package 'strategy'."
                    )
                    write_debug_log(
                        f"Classe '{strategy_class_name}' introuvable dans le module '{module_name}'."
                    )
                elif not issubclass(strategy_class, BaseStrategy):
                    self.logger.error(
                        f"[_load_strategy_class] Classe '{strategy_class_name}' trouvée pour '{strategy_key}' mais elle n'est pas une sous-classe de BaseStrategy. Impossible de l'utiliser."
                    )
                    write_debug_log(
                        f"Classe '{strategy_class_name}' trouvée mais n'est PAS une sous-classe de BaseStrategy."
                    )
                elif strategy_class is BaseStrategy:
                    self.logger.error(
                        f"[_load_strategy_class] La classe trouvée pour '{strategy_key}' est BaseStrategy elle-même. Ceci n'est pas une stratégie implémentée."
                    )
                    write_debug_log(
                        f"La classe trouvée est BaseStrategy elle-même (non implémentée)."
                    )
                return None
        except ImportError as e:
            error_trace = traceback.format_exc()
            self.logger.error(
                f"[_load_strategy_class] Erreur d'importation générale pour la stratégie '{strategy_key}' : {str(e)}",
                exc_info=True,
            )
            write_debug_log(f"ERREUR ImportError: {e}. Trace: {error_trace}")
            return None
        except Exception as e:
            error_trace = traceback.format_exc()
            self.logger.error(
                f"[_load_strategy_class] Erreur inattendue lors du chargement de la stratégie '{strategy_key}' : {str(e)}",
                exc_info=True,
            )
            write_debug_log(f"ERREUR INATTENDUE: {e}. Trace: {error_trace}")
            return None

    def load_strategy(self, strategy_key: str) -> bool:
        """
        Charge une stratégie spécifique dans la configuration dynamique.

        Args:
            strategy_key (str): Clé unique de la stratégie.

        Returns:
            bool: True si la stratégie a été chargée, False sinon.
        """
        self.logger.info(f"Chargement de la stratégie '{strategy_key}'...")
        config_mapping = self.config_manager.get("strategies.config_mapping", {})
        config_file = config_mapping.get(strategy_key)
        if not config_file:
            self.logger.error(f"Aucun fichier mappé pour '{strategy_key}'.")
            return False

        config_dir_path = self.config_manager.get(
            "paths.strategy_configs", "config/strategy/"
        )
        config_path = Path(config_dir_path) / config_file
        if not config_path.is_file():
            self.logger.error(f"Fichier '{config_path}' introuvable.")
            return False

        if strategy_key in self.strategy_registry:
            try:
                current_strategy_config = self.config_loader.load_dynamic_config(
                    str(config_path), schema_name="strategy_schema.json"
                )
                current_strategy_name = current_strategy_config.get("strategy_name")

                if (
                    current_strategy_name in self.strategy_registry
                    and config_path.stat().st_mtime
                    <= self.strategy_registry[current_strategy_name]["last_modified"]
                ):
                    self.logger.debug(
                        f"Stratégie '{strategy_key}' (nom: '{current_strategy_name}') utilisée depuis le cache."
                    )
                    return True
            except FileNotFoundError:
                self.logger.warning(
                    f"Fichier '{config_path}' introuvable. Rechargement forcé."
                )
                pass
            except Exception as e:
                self.logger.error(
                    f"Erreur de vérification pour '{strategy_key}' : {str(e)}",
                    exc_info=True,
                )
                pass

        try:
            config = self.config_loader.load_dynamic_config(
                str(config_path), schema_name="strategy_schema.json"
            )
            strategy_class = self._load_strategy_class(strategy_key)

            strategy_name_from_config = config.get("strategy_name")
            if strategy_name_from_config:
                self.strategy_registry[strategy_name_from_config] = {
                    "config": config,
                    "class": strategy_class,
                    "last_modified": config_path.stat().st_mtime,
                }
             
            else:
                self.logger.error(
                    f"[load_strategy] La configuration '{config_file}' pour la clé '{strategy_key}' ne contient pas de 'strategy_name'. Elle ne sera pas ajoutée au registre."
                )
                return False

            self.config_manager.update_dynamic_config(config, source="strategy_load")
            self.logger.info(f"Stratégie '{strategy_key}' chargée avec succès.")

            if hasattr(self.config_manager, "audit_logger"):
                self.config_manager.audit_logger.log_config_change(
                    change_info={
                        "action": "load_strategy",
                        "strategy": strategy_key,
                        "file": str(config_path),
                        "class_loaded": strategy_class is not None,
                    },
                    source="system_load_strategy",
                    dynamic_config_snapshot=self.config_manager.get_current_dynamic_config().copy(),
                )
            return True
        except ConfigValidationError as e:
            self.logger.error(f"Erreur de validation pour '{strategy_key}' : {str(e)}")
            return False
        except Exception as e:
            self.logger.error(
                f"Échec du chargement de '{strategy_key}' : {str(e)}", exc_info=True
            )
            return False
  

    def redefine_strategy(
        self, strategy_key: str, config_path: str, python_module: Optional[str] = None
    ) -> bool:
        """
        Redéfinit une stratégie dynamiquement avec un nouveau fichier de configuration.

        Args:
            strategy_key (str): Clé unique de la stratégie.
            config_path (str): Chemin vers le nouveau fichier de configuration.
            python_module (Optional[str]): Nom du module Python personnalisé.

        Returns:
            bool: True si la redéfinition a réussi, False sinon.
        """
        self.logger.info(f"Redéfinition de la stratégie '{strategy_key}'...")
        config_path_obj = Path(config_path)
        if not config_path_obj.is_file():
            self.logger.error(f"Fichier '{config_path}' introuvable.")
            return False

        try:
            config = self.config_loader.load_dynamic_config(
                str(config_path_obj), schema_name="strategy_schema.json"
            )
            strategy_class = (
                self._load_strategy_class(strategy_key)
                if python_module is None
                else self._load_custom_python_module(python_module)
            )
            if strategy_class is None:
                self.logger.warning(f"Classe Python non chargée pour '{strategy_key}'.")

            strategy_name_from_config = config.get("strategy_name")
            if strategy_name_from_config:
                self.strategy_registry[strategy_name_from_config] = {
                    "config": config,
                    "class": strategy_class,
                    "last_modified": config_path_obj.stat().st_mtime,
                }
               
            else:
                self.logger.error(
                    f"[redefine_strategy] La configuration '{config_path_obj.name}' pour la clé '{strategy_key}' ne contient pas de 'strategy_name'. La redéfinition échoue."
                )
                return False

            self.config_manager.update_dynamic_config(
                {
                    "strategies": {
                        "config_mapping": {strategy_key: config_path_obj.name}
                    }
                },
                source="strategy_redefinition",
            )
            self.logger.info(f"Stratégie '{strategy_key}' redéfinie avec succès.")

            if hasattr(self.config_manager, "audit_logger"):
                self.config_manager.audit_logger.log_config_change(
                    change_info={
                        "action": "redefine_strategy",
                        "strategy": strategy_key,
                        "file": str(config_path_obj),
                        "python_module": python_module,
                        "class_loaded": strategy_class is not None,
                    },
                    source="manual_redefinition",
                    dynamic_config_snapshot=self.config_manager.get_current_dynamic_config().copy(),
                )
            return True
        except ConfigValidationError as e:
            self.logger.error(f"Erreur de validation pour '{strategy_key}' : {str(e)}")
            return False
        except Exception as e:
            self.logger.error(
                f"Échec de la redéfinition de '{strategy_key}' : {str(e)}",
                exc_info=True,
            )
            return False

    def _load_custom_python_module(self, module_path: str) -> Optional[Type]:
        """
        Charge un module Python personnalisé.

        Args:
            module_path (str): Chemin du module (ex: "custom_strategies.my_strategy").

        Returns:
            Optional[Type]: La classe Python chargée, ou None si introuvable.
        """
        if not module_path or not all(c.isalnum() or c in "._:" for c in module_path):
            self.logger.error(f"Chemin de module invalide : '{module_path}'.")
            return None

        try:
            if ":" in module_path:
                module_name, class_name = module_path.split(":")
            else:
                module_name = module_path
                class_name = None

            spec = importlib.util.find_spec(module_name)
            if spec is None or spec.loader is None:
                self.logger.error(f"Module '{module_name}' introuvable.")
                return None

            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            if class_name:
                strategy_class = getattr(module, class_name, None)
            else:
                deduced_class_name = (
                    "".join(
                        [s.capitalize() for s in module_name.split(".")[-1].split("_")]
                    )
                    + "Strategy"
                )
                strategy_class = getattr(module, deduced_class_name, None)
                if strategy_class is None:
                    for name, obj in inspect.getmembers(module, inspect.isclass):
                        if issubclass(obj, BaseStrategy) and obj is not BaseStrategy:
                            strategy_class = obj
                            self.logger.info(
                                f"Classe '{name}' trouvée dans '{module_name}'."
                            )
                            break

            if (
                strategy_class
                and issubclass(strategy_class, BaseStrategy)
                and strategy_class is not BaseStrategy
            ):
                self.logger.debug(
                    f"Classe '{strategy_class.__name__}' chargée depuis '{module_name}'."
                )
                return strategy_class
            else:
                self.logger.error(f"Classe invalide dans '{module_path}'.")
                return None
        except ImportError as e:
            self.logger.error(
                f"Erreur d'importation pour '{module_path}' : {str(e)}", exc_info=True
            )
            return None
        except Exception as e:
            self.logger.error(
                f"Erreur inattendue pour '{module_path}' : {str(e)}", exc_info=True
            )
            return None

    def get_magic_to_strategy_map(self) -> Dict[int, Dict[str, Any]]:
        """
        Construit un mappage des magic numbers aux informations de stratégie.

        Returns:
            Dict[int, Dict[str, Any]]: Mappage des magic numbers aux stratégies.
        """
        magic_map = {}
        for strategy_key, strategy_info in self.strategy_registry.items():
            magic_number = strategy_info["config"].get("magic_number")
            if magic_number is not None:
                magic_map[magic_number] = {
                    "class": strategy_info["class"],
                    "config": strategy_info["config"],
                }
        return magic_map

    def get_strategy_class(self, strategy_name: str) -> Optional[Type]:
        """
        Retourne la classe Python d'une stratégie via son nom.

        Args:
            strategy_name (str): Nom de la stratégie.

        Returns:
            Optional[Type]: La classe Python, ou None si introuvable.
        """
        for strategy_key, strategy_info in self.strategy_registry.items():
            if strategy_info["config"].get("strategy_name", "") == strategy_name:
                return strategy_info["class"]
        self.logger.warning(f"Classe introuvable pour la stratégie '{strategy_name}'.")
        return None

    def get_strategy_config(self, strategy_name: str) -> Optional[Dict[str, Any]]:
        """
        Récupère la configuration d'une stratégie par son nom.
        Si strategy_name est None ou vide, on retourne None sans warning.
        """
        if not strategy_name or strategy_name == "None":
            self.logger.debug(
                "[StrategyManager] Aucune stratégie sélectionnée (strategy_name=None)."
            )
            return None

        for strategy_key, strategy_info in self.strategy_registry.items():
            cfg_name = strategy_info["config"].get("strategy_name", "")
            if cfg_name == strategy_name:
                return strategy_info["config"]

        # Ici seulement, on log un warning car c'est un vrai problème : stratégie non trouvée
        self.logger.warning(
            f"[StrategyManager] Configuration introuvable pour la stratégie '{strategy_name}'."
        )
        return None

    def get_strategy_by_key(self, strategy_key: str) -> Optional[Dict[str, Any]]:
        """
        Retourne les informations complètes d'une stratégie via sa clé.

        Args:
            strategy_key (str): Clé unique de la stratégie.

        Returns:
            Optional[Dict[str, Any]]: Informations de la stratégie, ou None si introuvable.
        """
        return self.strategy_registry.get(strategy_key)
