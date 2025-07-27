# Contenu du fichier mecanique_generale/mecano.py

import os
import time
import json
import csv
import sys
import pandas as pd
import datetime
import logging
import traceback
from contextlib import contextmanager
from typing import List, Dict, Any, Optional
from pathlib import Path  # Ajouté pour la classe Path
from core.config_manager import ConfigManager
from datetime import datetime, timedelta, UTC


# Initialisation du Logger pour ce module (au niveau global pour ce fichier)
logger = logging.getLogger(__name__)

# --- Squelette: Gestion Conditionnelle de la Librairie `psutil` ---
try:
    import psutil
except ImportError:
    # Crée un objet psutil factice si la librairie n'est pas installée.
    # Les méthodes utiliseront ce mock et lèveront une NotImplementedError claire.
    logger.warning(
        "La librairie 'psutil' n'est pas installée. Les fonctions de monitoring des ressources système seront limitées."
    )

    class psutil:
        @staticmethod
        def cpu_percent(interval=None):
            raise NotImplementedError(
                "psutil n'est pas installé. La collecte des métriques CPU est désactivée."
            )

        @staticmethod
        def virtual_memory():
            class MockVirtualMemory:
                percent = 0.0  # Retourne une valeur par défaut pour éviter les erreurs d'attribut

            return MockVirtualMemory()

        @staticmethod
        def boot_time():
            raise NotImplementedError(
                "psutil n'est pas installé. Impossible d'obtenir le temps de démarrage du système."
            )

        @staticmethod
        def Process(pid):
            class MockProcess:
                def __init__(self, pid):
                    pass

                def memory_info(self):
                    class MockMemoryInfo:
                        rss = 0  # Retourne 0 par défaut

                    return MockMemoryInfo()

                def num_threads(self):
                    return 0  # Retourne 0 par défaut

            return MockProcess(pid)


# --- Squelette: Classe Factice pour `AIDecision` (Mock pour les tests de Mecano) ---
# NOTE IMPORTANTE: Cette classe est un MOCK. En production, Mecano utilisera la VRAIE instance
# de AIDecision passée depuis main.py. Cette classe est ici pour permettre à `mecano.py`
# de fonctionner en autonome (par exemple, pour des tests unitaires isolés de Mecano).
class AIDecision:
    """
    Wrapper factice pour simuler l'interaction avec le module `ai_core.ai_decision`.
    Utilisé uniquement lorsque `Mecano` est testé isolément sans l'instance réelle de l'IA.
    """

    def __init__(
        self, config_manager_instance=None
    ):  # config_manager_instance peut être passé au mock
        self.model_name = "local_llama_mock_v1"
        self.config_manager = (
            config_manager_instance  # Permet au mock d'accéder à la config si besoin
        )
        logger.info(f"Mecano: Wrapper IA factice '{self.model_name}' initialisé.")

    def get_analysis(self, prompt: str) -> Dict[str, Any]:
        """
        Simule une analyse de prompt par l'IA et retourne un dictionnaire structuré.
        Le délai de simulation est maintenant lu dynamiquement depuis la configuration de Mecano.
        """
        logger.info(
            f"Mecano: Envoi du prompt (longueur: {len(prompt)}) au modèle IA factice '{self.model_name}'."
        )

        # Le délai de simulation est lu via le ConfigManager dans Mecano.__init__
        # et stocké dans `self.mock_ai_delay_seconds`.
        # Pour ce mock, il faudrait soit le passer en paramètre, soit le lire ici aussi si `config_manager` est dispo.
        # Pour garder ce mock simple, on utilisera un fallback simple ici, car c'est un mock.
        mock_sleep_time = 1.5
        if self.config_manager:
            # Tente de lire le délai depuis la config via le config_manager du mock
            mock_sleep_time = self.config_manager.get(
                "mecano_settings.mock_ai_delay_seconds", mock_sleep_time
            )
        time.sleep(mock_sleep_time)

        analysis = {
            "summary": "Le système semble stable avec des pics de CPU occasionnels.",
            "recommendations": [
                "Investiguer l'étape 'data_processing' qui est la plus lente.",
                "Surveiller l'erreur 'KeyError' qui est la plus fréquente.",
                "Considérer une augmentation de la RAM si l'usage moyen dépasse 70%.",
            ],
            "critical_issues_detected": [
                "Aucun problème critique détecté nécessitant une action immédiate."
            ],
            "model_version": self.model_name,
            "analysis_timestamp": datetime.datetime.now(
                datetime.UTC
            ).isoformat(),  # Utiliser UTC pour la robustesse des fuseaux horaires
        }
        return analysis


# --- Classe principale Mecano ---
class Mecano:
    """
    Observateur silencieux pour le monitoring de performance, d'erreurs et de ressources du bot.
    Totalement passif, il collecte des métriques et génère des rapports sans interférer
    avec le pipeline de trading principal. Il est conçu pour l'audit et l'optimisation continue.
    """

    def __init__(self, config_manager_instance: Optional[ConfigManager] = None):
        """
        Initialise le module Mecano et configure ses loggers et attributs.

        Args:
            config_manager_instance (Optional[ConfigManager]): Instance du ConfigManager
                                                            pour accéder aux paramètres dynamiques.
                                                            Doit être la vraie instance de ConfigManager.
        """
        self.logger = logging.getLogger(__name__)  # Logger d'instance
        self.config_manager = config_manager_instance
        if self.config_manager is None:
            self.logger.warning(
                "Mecano initialisé sans ConfigManager. Les chemins de logs et rapports ne seront pas dynamiques."
            )

        # Récupération des chemins des dossiers logs et reports depuis ConfigManager
        self.logs_dir = (
            Path(self.config_manager.get("paths.logs", "logs/"))
            if self.config_manager
            else Path("logs/")
        )
        self.reports_dir = (
            Path(self.config_manager.get("paths.reports", "output/"))
            if self.config_manager
            else Path("output/")
        )

        # Création des dossiers si nécessaire
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.reports_dir.mkdir(parents=True, exist_ok=True)

        # AJOUT / CORRECTION: Récupération des noms de fichiers de log spécifiques à Mecano
        # Ces attributs DOIVENT être initialisés AVANT l'appel à _setup_loggers()
        self.performance_log_file_name = (
            self.config_manager.get(
                "mecano_settings.performance_log_file_name", "performance.log"
            )
            if self.config_manager
            else "performance.log"
        )
        self.errors_log_file_name = (
            self.config_manager.get(
                "mecano_settings.errors_log_file_name", "errors.log"
            )
            if self.config_manager
            else "errors.log"
        )
        # Ajout de l'attribut `error_logger`
        self.error_logger = logging.getLogger(
            "Mecano.Errors"
        )  # Logger spécifique pour les erreurs

        # Configuration des loggers spécifiques de Mecano
        self._setup_loggers()

        # Initialisation du process psutil pour le profiling mémoire
        try:
            import psutil  # Import local de psutil

            # os est importé globalement
            self.process = psutil.Process(os.getpid())
            self.logger.info(
                "Mecano: psutil.Process initialisé pour la collecte de métriques système."
            )
        except NotImplementedError as e:
            self.logger.warning(
                f"Mecano: Impossible d'initialiser psutil.Process : {e}. Les métriques de processus ne seront pas collectées."
            )
            self.process = None
        except Exception as e:
            self.error_logger.error(
                f"Mecano: Erreur lors de l'initialisation de psutil.Process : {e}",
                exc_info=True,
            )
            self.process = None

        # Récupération des préfixes de fichiers pour les rapports Mecano
        self.config_snapshot_file_prefix = (
            self.config_manager.get(
                "mecano_settings.config_snapshot_file_prefix", "config_snapshot_"
            )
            if self.config_manager
            else "config_snapshot_"
        )
        self.ia_analysis_log_file_prefix = (
            self.config_manager.get(
                "mecano_settings.ia_analysis_log_file_prefix", "ia_analysis_"
            )
            if self.config_manager
            else "ia_analysis_"
        )
        self.weekly_report_file_prefix = (
            self.config_manager.get(
                "mecano_settings.weekly_report_file_prefix", "weekly_report_"
            )
            if self.config_manager
            else "weekly_report_"
        )

        # Récupération des seuils d'alerte et des paramètres AI pour Mecano (section mecano_settings)
        self.cpu_threshold = (
            self.config_manager.get(
                "mecano_settings.resource_alerts.cpu_threshold", 80.0
            )
            if self.config_manager
            else 80.0
        )
        self.ram_threshold = (
            self.config_manager.get(
                "mecano_settings.resource_alerts.ram_threshold", 80.0
            )
            if self.config_manager
            else 80.0
        )
        self.mock_ai_delay_seconds = (
            self.config_manager.get("mecano_settings.mock_ai_delay_seconds", 1.5)
            if self.config_manager
            else 1.5
        )
        self.ia_prompt_max_length = (
            self.config_manager.get(
                "mecano_settings.ai_prompt_settings.max_length", 3500
            )
            if self.config_manager
            else 3500
        )
        self.ia_prompt_top_n_slowest_steps = (
            self.config_manager.get("mecano_settings.ia_prompt_top_n_slowest_steps", 5)
            if self.config_manager
            else 5
        )
        self.report_period_days = (
            self.config_manager.get("mecano_settings.report_period_days", 7)
            if self.config_manager
            else 7
        )

        # `self.ai_analyzer` sera l'instance réelle de `ai_core.ai_decision.AIDecision`
        # injectée par `main.py` au démarrage du bot.
        self.ai_analyzer = None  # Attendre l'injection de l'instance réelle.

        # Structures de données pour la compilation des rapports (initialisation)
        # Ces listes seront remplies par les méthodes de collecte.
        self.profiling_data: List[Dict[str, Any]] = []
        self.error_data: List[Dict[str, Any]] = []
        self.system_metrics_history: List[Dict[str, Any]] = []
        self.config_snapshots: List[str] = (
            []
        )  # Contiendra les chemins des snapshots sauvegardés

        self.logger.info(
            "Mecano initialisé. Prêt à observer le système et générer des rapports intelligents."
        )

    def set_ai_analyzer(
        self, ai_analyzer_instance: "AIDecision"
    ) -> None:  # Ajout de type hint pour AIDecision
        """
        Injecte l'instance de l'analyseur IA (AIDecision) dans Mecano.
        Cela permet à Mecano de demander des analyses IA pour ses rapports.

        Args:
            ai_analyzer_instance (AIDecision): L'instance du module AIDecision.
        """
        self.ai_analyzer = ai_analyzer_instance
        self.logger.info("Mecano: Instance AIDecision injectée.")

    def _setup_loggers(self) -> None:
        """
        Configure les loggers spécifiques pour la performance et les erreurs du module Mecano.
        Crée des FileHandlers dédiés pour 'MecanoPerformance' et 'MecanoErrors'
        afin de les séparer des logs généraux du bot.
        Les loggers sont configurés pour ne pas propager leurs messages au logger racine
        afin d'éviter le double logging.
        """
        # Logger principal pour les infos générales de Mecano
        # self.logger est déjà défini dans __init__
        self.logger = logging.getLogger(
            "Mecano"
        )  # Récupère l'instance du logger principal de Mecano
        # S'assure que le logger n'a pas déjà de handlers pour éviter les duplications
        if not self.logger.handlers:
            self.logger.setLevel(
                logging.INFO
            )  # Niveau par défaut pour les infos générales de Mecano
            ch = logging.StreamHandler(sys.stdout)  # sys est importé globalement
            ch.setFormatter(
                logging.Formatter(
                    "%(asctime)s - [Mecano] - %(levelname)s - %(message)s"
                )
            )
            self.logger.addHandler(ch)

        # Logger pour la performance (profiling)
        self.performance_logger = logging.getLogger("MecanoPerformance")
        # Vérifie si un FileHandler avec le bon chemin existe déjà pour éviter les duplications
        if not any(
            isinstance(h, logging.FileHandler)
            and h.baseFilename == str(self.logs_dir / self.performance_log_file_name)
            for h in self.performance_logger.handlers
        ):
            self.performance_logger.setLevel(
                logging.INFO
            )  # Niveau par défaut pour la performance
            fh_perf = logging.FileHandler(
                self.logs_dir / self.performance_log_file_name,
                mode="a",
                encoding="utf-8",
            )
            fh_perf.setFormatter(
                logging.Formatter("%(asctime)s - %(message)s")
            )  # Format plus simple pour les logs de performance
            self.performance_logger.addHandler(fh_perf)
            self.performance_logger.propagate = (
                False  # Évite la duplication avec le logger racine
            )

        # Logger pour les erreurs spécifiques de Mecano
        self.error_logger = logging.getLogger("MecanoErrors")
        # Vérifie si un FileHandler avec le bon chemin existe déjà pour éviter les duplications
        if not any(
            isinstance(h, logging.FileHandler)
            and h.baseFilename == str(self.logs_dir / self.errors_log_file_name)
            for h in self.error_logger.handlers
        ):
            self.error_logger.setLevel(
                logging.WARNING
            )  # Capture aussi les WARNINGs par défaut
            fh_err = logging.FileHandler(
                self.logs_dir / self.errors_log_file_name, mode="a", encoding="utf-8"
            )
            fh_err.setFormatter(
                logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
            )
            self.error_logger.addHandler(fh_err)
            self.error_logger.propagate = (
                False  # Évite la duplication avec le logger racine
            )

    @contextmanager
    def profile_step(self, step_name: str):
        """
        Context manager pour mesurer précisément le temps d'exécution d'une étape spécifique du bot.
        La durée est loguée et stockée pour le rapport de performance.

        Usage:
            with mecano.profile_step("nom_de_l_etape"):
                # Code dont le temps d'exécution doit être mesuré
                pass

        Args:
            step_name (str): Nom unique de l'étape à profiler.
        """
        start_time = (
            time.perf_counter()
        )  # Utilise time.perf_counter pour une haute résolution
        try:
            yield
        finally:
            duration = time.perf_counter() - start_time
            log_message = (
                f"PROFILING - Step: '{step_name}' - Duration: {duration:.6f} seconds"
            )
            self.performance_logger.info(
                log_message
            )  # Log dans le logger de performance dédié
            # Stocke les données pour le rapport
            # datetime est importé globalement
            self.profiling_data.append(
                {
                    "step": step_name,
                    "duration_s": duration,
                    "timestamp": datetime.now(UTC).isoformat(),
                }
            )

    def log_exception(self, step_name: str, exception: Exception):
        """
        Enregistre une exception formatée de manière détaillée dans le journal d'erreurs de Mecano.
        Inclut le nom de l'étape où l'erreur s'est produite, le type d'exception, le message,
        et le stacktrace complet pour un débogage facilité.

        Args:
            step_name (str): Le nom de l'étape du pipeline où l'exception a été rencontrée.
            exception (Exception): L'objet exception capturé.
        """
        try:
            exc_type = type(exception).__name__
            exc_msg = str(exception)
            stack_trace = traceback.format_exc()  # Capture le stacktrace complet

            log_message = (
                f"EXCEPTION in step '{step_name}'\n"
                f"Type: {exc_type}\n"
                f"Message: {exc_msg}\n"
                f"StackTrace:\n{stack_trace}"
                f"\n"  # Ajoute une ligne vide pour la séparation dans le fichier de log
                f"----------------------------------------"  # Ligne de séparation pour chaque exception
            )
            self.error_logger.error(log_message)  # Log dans le logger d'erreurs dédié

            # Stocke les données pour le rapport (timestamp en UTC)
            self.error_data.append(
                {
                    "step": step_name,
                    "type": exc_type,
                    "message": exc_msg,
                    "timestamp": datetime.now(
                        UTC
                    ).isoformat(),  # Utiliser datetime.now(UTC)
                }
            )

            # Envoyer une alerte Telegram si la configuration le permet (configurable via ConfigManager)
            if self.config_manager and self.config_manager.get(
                "telegram.alerts_enabled", False
            ):
                alert_type = self.config_manager.get(
                    "mecano_settings.alert_on_exception_channel", "telegram_critical"
                )
                alert_message = f"🚨 **MECANO ERREUR** dans `{step_name}`\nType: `{exc_type}`\nMessage: `{exc_msg}`"
                self.config_manager.send_alert(alert_message, alert_type)

        except Exception as e:
            # Failsafe : si le logging lui-même d'une exception échoue, on ne veut pas que le bot plante.
            # On logue sur le logger principal (qui est toujours configuré) avec un niveau CRITICAL.
            # logger est le logger global, pas self.logger.
            logging.critical(
                f"CRITICAL: Failed to log an exception within Mecano. Reason: {e}",
                exc_info=True,
            )

    def snapshot_config(self, config: dict):
        """
        Sauvegarde un snapshot horodaté de la configuration fournie (généralement la configuration active du bot).
        Ces snapshots sont stockés dans le répertoire des rapports pour l'auditabilité.

        Args:
            config (dict): Le dictionnaire de configuration à sauvegarder.
        """
        try:
            # datetime est importé globalement
            ts = datetime.now(UTC).strftime(
                "%Y%m%d_%H%M%S"
            )  # Utiliser datetime.now(UTC)
            # Le préfixe du nom de fichier est lu dynamiquement dans __init__
            filename = f"{self.config_snapshot_file_prefix}{ts}.json"
            filepath = self.reports_dir / filename  # Construction du chemin avec Path

            # Utiliser l'écriture atomique avec un fichier temporaire pour la robustesse
            temp_filepath = filepath.with_suffix(".tmp")

            with open(temp_filepath, "w", encoding="utf-8") as f:
                # Utiliser CustomJSONEncoder pour gérer les types de données non sérialisables (datetime, pandas)
                # ConfigManager fournit CustomJSONEncoder, s'assurer qu'il est accessible ici
                from core.config_manager import (
                    CustomJSONEncoder,
                )  # Import local pour cette fonction

                json.dump(
                    config, f, indent=4, cls=CustomJSONEncoder
                )  # Utiliser l'encodeur personnalisé

            temp_filepath.rename(filepath)  # Rendre l'écriture atomique

            self.logger.info(
                f"Mecano: Snapshot de configuration sauvegardé dans '{filepath}'"
            )
            self.config_snapshots.append(
                str(filepath)
            )  # Stocke le chemin pour référence
        except Exception as e:
            self.log_exception(
                "snapshot_config", e
            )  # Log l'exception via la méthode dédiée
            # En cas d'échec de snapshot, envoyer une alerte
            if self.config_manager and self.config_manager.get(
                "telegram.alerts_enabled", False
            ):
                self.config_manager.send_alert(
                    "ERREUR_CONFIG_SNAPSHOT",
                    f"Mecano: Échec sauvegarde snapshot config: {e}",
                    alert_type="telegram_critical",
                )

    def collect_system_metrics(self) -> dict:
        """
        Collecte les métriques système actuelles (utilisation CPU, RAM, threads du processus, temps de fonctionnement du système)
        via la librairie `psutil`.

        Returns:
            dict: Un dictionnaire structuré des métriques collectées, avec des valeurs `None`
                  si `psutil` n'est pas disponible ou si la collecte échoue.
        """
        metrics = {
            "timestamp": datetime.now(UTC).isoformat(),  # Utiliser datetime.now(UTC)
            "cpu_percent": None,
            "system_ram_percent": None,
            "process_ram_mb": None,
            "process_threads": None,
            "system_uptime_hours": None,
        }
        try:
            # Récupérer les seuils d'alerte CPU/RAM depuis self (chargés dynamiquement dans __init__)
            # psutil.cpu_percent avec interval=0.1 pour obtenir l'usage depuis le dernier appel
            metrics["cpu_percent"] = psutil.cpu_percent(interval=0.1)
            metrics["system_ram_percent"] = psutil.virtual_memory().percent

            # self.process est l'objet psutil.Process du bot
            if self.process:
                metrics["process_ram_mb"] = self.process.memory_info().rss / (
                    1024 * 1024
                )  # RAM utilisée par le processus en MB
                metrics["process_threads"] = (
                    self.process.num_threads()
                )  # Nombre de threads du processus

            # psutil.boot_time() est le temps de démarrage du système (timestamp Unix)
            # time.time() est le timestamp Unix actuel.
            uptime_seconds = time.time() - psutil.boot_time()
            metrics["system_uptime_hours"] = uptime_seconds / 3600

            self.system_metrics_history.append(
                metrics
            )  # Ajoute aux données de l'historique

            # Vérifier les seuils d'alerte CPU/RAM et envoyer des alertes
            if (
                metrics["cpu_percent"] is not None
                and metrics["cpu_percent"] > self.cpu_threshold
            ):
                self.logger.warning(
                    f"Mecano: ALERTE RESSOURCE: Usage CPU ({metrics['cpu_percent']:.1f}%) dépasse le seuil configuré de {self.cpu_threshold}%."
                )
                if self.config_manager and self.config_manager.get(
                    "telegram.alerts_enabled", False
                ):
                    self.config_manager.send_alert(
                        "ALERTE_CPU",
                        f"Mecano: CPU élevé ({metrics['cpu_percent']:.1f}%)!",
                        alert_type="telegram_critical",
                    )

            if (
                metrics["system_ram_percent"] is not None
                and metrics["system_ram_percent"] > self.ram_threshold
            ):
                self.logger.warning(
                    f"Mecano: ALERTE RESSOURCE: Usage RAM ({metrics['system_ram_percent']:.1f}%) dépasse le seuil configuré de {self.ram_threshold}%."
                )
                if self.config_manager and self.config_manager.get(
                    "telegram.alerts_enabled", False
                ):
                    self.config_manager.send_alert(
                        "ALERTE_RAM",
                        f"Mecano: RAM élevée ({metrics['system_ram_percent']:.1f}%)!",
                        alert_type="telegram_critical",
                    )

        except NotImplementedError as nie:
            self.logger.warning(
                f"Mecano: La collecte des métriques système est limitée car psutil n'est pas complètement fonctionnel : {nie}."
            )
        except Exception as e:
            self.log_exception(
                "collect_system_metrics", e
            )  # Log l'exception via la méthode dédiée

        return metrics

    def build_weekly_report(self) -> dict:
        """
        Compile les données collectées (profiling, erreurs, métriques système, snapshots de config)
        en un rapport structuré pour une période configurable (généralement hebdomadaire).
        Ce rapport agrège les données et prépare un résumé pour l'analyse par l'IA.

        Returns:
            dict: Un dictionnaire structuré contenant le rapport complet, y compris les résumés
                et les détails.
        """
        self.logger.info("Mecano: Compilation du rapport hebdomadaire...")
        # La période du rapport est lue dynamiquement dans __init__
        report_period_days = self.report_period_days

        # Calculer la date de début de la période de rapport
        period_start_utc = datetime.now(UTC) - timedelta(days=report_period_days)

        # Filtrer les données collectées pour la période du rapport
        filtered_profiling_data = [
            d
            for d in self.profiling_data
            if datetime.fromisoformat(d["timestamp"]).astimezone(UTC)
            >= period_start_utc
        ]
        filtered_error_data = [
            d
            for d in self.error_data
            if datetime.fromisoformat(d["timestamp"]).astimezone(UTC)
            >= period_start_utc
        ]
        filtered_system_metrics_history = [
            d
            for d in self.system_metrics_history
            if datetime.fromisoformat(d["timestamp"]).astimezone(UTC)
            >= period_start_utc
        ]

        # Note: config_snapshots contient des chemins de fichiers, pas des objets date.
        # Le filtrage des snapshots peut être fait en externe si nécessaire.

        report = {
            "report_generated_at": datetime.now(UTC).isoformat(),
            "period_start": period_start_utc.isoformat(),
            "summary": {},  # Contient les résumés des sections (profiling, erreurs, métriques)
            "details": {  # Contient les données brutes ou plus détaillées pour l'IA
                "profiling_data": filtered_profiling_data,
                "error_data": filtered_error_data,
                "system_metrics_data": filtered_system_metrics_history,
                "config_snapshots_paths": self.config_snapshots,  # Fournir les chemins, l'IA pourrait les demander si besoin
            },
        }

        # --- Section Summary (pour le rapport et le prompt IA) ---

        # Profiling Summary
        if filtered_profiling_data:
            durations = [d["duration_s"] for d in filtered_profiling_data]
            if durations:
                # Utilise time.perf_counter pour un tri précis
                top_slow = sorted(
                    filtered_profiling_data, key=lambda x: x["duration_s"], reverse=True
                )
                report["summary"]["profiling_summary"] = {
                    "total_steps_profiled": len(durations),
                    "avg_duration_s": sum(durations) / len(durations),
                    "max_duration_s": max(durations),
                    "top_slowest_steps": top_slow[: self.ia_prompt_top_n_slowest_steps],
                }
            else:
                report["summary"][
                    "profiling_summary"
                ] = "Aucune donnée de profiling disponible pour cette période."
        else:
            report["summary"][
                "profiling_summary"
            ] = "Aucune donnée de profiling disponible pour cette période."

        # Errors Summary
        if filtered_error_data:
            error_types = [e["type"] for e in filtered_error_data]
            report["summary"]["error_summary"] = {
                "total_errors": len(filtered_error_data),
                "unique_error_types": list(set(error_types)),
                "most_frequent_error": (
                    max(set(error_types), key=error_types.count)
                    if error_types
                    else "N/A"
                ),
            }
        else:
            report["summary"][
                "error_summary"
            ] = "Aucune erreur enregistrée pour cette période."

        # System Metrics Summary
        if filtered_system_metrics_history:
            # S'assurer que les calculs gèrent le cas de liste vide pour éviter ZeroDivisionError
            # et que les valeurs sont numériques (None a été filtré lors de la collecte).
            cpu_values = [
                m["cpu_percent"]
                for m in filtered_system_metrics_history
                if m["cpu_percent"] is not None
            ]
            ram_values = [
                m["system_ram_percent"]
                for m in filtered_system_metrics_history
                if m["system_ram_percent"] is not None
            ]

            avg_cpu = sum(cpu_values) / len(cpu_values) if cpu_values else 0.0
            max_cpu = max(cpu_values) if cpu_values else 0.0
            avg_ram = sum(ram_values) / len(ram_values) if ram_values else 0.0
            max_ram = max(ram_values) if ram_values else 0.0

            report["summary"]["system_metrics_summary"] = {
                "avg_cpu_percent": f"{avg_cpu:.2f}",
                "max_cpu_percent": f"{max_cpu:.2f}",
                "avg_system_ram_percent": f"{avg_ram:.2f}",
                "max_system_ram_percent": f"{max_ram:.2f}",
            }
        else:
            report["summary"][
                "system_metrics_summary"
            ] = "Aucune métrique système disponible pour cette période."

        self.logger.info("Mecano: Rapport hebdomadaire compilé.")
        return report

    def build_ia_prompt(self, report: dict) -> str:
        """
        Construit un prompt compact et structuré pour l'analyse par l'IA à partir d'un rapport de performance.
        Priorise les informations critiques et tronque le prompt si sa longueur dépasse la limite configurée.

        Args:
            report (dict): Le rapport de performance généré par `build_weekly_report`.

        Returns:
            str: Le prompt formaté, prêt à être envoyé à un modèle d'IA.
        """
        # La longueur maximale du prompt est lue dynamiquement dans __init__
        max_length = self.ia_prompt_max_length

        prompt = []
        prompt.append("ANALYSE DE PERFORMANCE SYSTÈME - SNIPER_X")
        prompt.append("=" * 40)
        # Accéder aux dates via le dictionnaire 'report'
        report_generated_at_str = report.get("report_generated_at", "N/A")
        period_start_str = report.get("period_start", "N/A")

        prompt.append(f"Rapport généré le: {report_generated_at_str.split('T')[0]}")
        # Correction: Utilisation des chaînes de caractères directement pour le formatage
        prompt.append(
            f"Période du rapport: {period_start_str.split('T')[0]} à {report_generated_at_str.split('T')[0]}\n"
        )
        prompt.append(
            "CONTEXTE: Tu es un expert en analyse de performance pour un bot de trading haute fréquence. Analyse les données suivantes, identifie les goulots d'étranglement, les risques de stabilité et fournis des recommandations concises.\n"
        )

        # 1. Résumé des erreurs (priorité haute)
        error_summary = report.get("summary", {}).get("error_summary")
        if error_summary and error_summary.get("total_errors", 0) > 0:
            prompt.append("## 1. Erreurs Critiques")
            prompt.append(
                f"- Nombre total d'erreurs: {error_summary.get('total_errors')}"
            )
            prompt.append(
                f"- Types d'erreurs uniques: {', '.join(error_summary.get('unique_error_types', []))}"
            )
            prompt.append(
                f"- Erreur la plus fréquente: {error_summary.get('most_frequent_error', 'N/A')}\n"
            )
            # Ajouter un échantillon des messages d'erreur détaillés pour le contexte de l'IA
            error_details_data = report.get("details", {}).get("error_data")
            if error_details_data:
                error_sample_size = self.config_manager.get(
                    "mecano_settings.error_sample_size_for_ai_prompt", 3
                )
                prompt.append("  Exemples d'erreurs récentes:")
                # datetime est importé globalement
                for err in error_details_data[-error_sample_size:]:
                    ts_part = (
                        datetime.fromisoformat(err.get("timestamp", ""))
                        .astimezone(UTC)
                        .strftime("%Y-%m-%d %H:%M:%S UTC")
                        if err.get("timestamp")
                        else "N/A"
                    )
                    prompt.append(
                        f"  - [{ts_part}] Type: {err.get('type')}, Message: {err.get('message', '')[:100]}..."
                    )
                prompt.append("")

        # 2. Résumé du profiling (priorité moyenne)
        profiling_summary = report.get("summary", {}).get("profiling_summary")
        if profiling_summary and profiling_summary.get("total_steps_profiled", 0) > 0:
            prompt.append("## 2. Goulots d'Étranglement (Top Étapes les plus lentes)")
            for step in profiling_summary.get("top_slowest_steps", [])[
                : self.ia_prompt_top_n_slowest_steps
            ]:
                prompt.append(
                    f"- Étape: {step['step']}, Durée: {step['duration_s']:.4f}s"
                )
            prompt.append("")

        # 3. Résumé des métriques système
        system_metrics_summary = report.get("summary", {}).get("system_metrics_summary")
        if system_metrics_summary:
            prompt.append("## 3. Utilisation des Ressources")
            prompt.append(
                f"- Usage CPU moyen: {system_metrics_summary.get('avg_cpu_percent')}% (Max: {system_metrics_summary.get('max_cpu_percent')})"
            )
            prompt.append(
                f"- Usage RAM système moyen: {system_metrics_summary.get('avg_system_ram_percent')}% (Max: {system_metrics_summary.get('max_system_ram_percent')})\n"
            )

        # 4. Ajout de la section sur les performances de trading du bot (en lien avec l'IA)
        if self.config_manager:  # S'assurer que config_manager est disponible
            # Filtrer les logs de décision pour la période du rapport
            # La clé 'period_start' dans le rapport est déjà un string ISO

            # Correction: Utilisation de `report.get('period_start')` pour le filtre
            # Convertir la date de début de période du rapport en datetime pour la comparaison
            try:
                period_start_dt_utc = datetime.fromisoformat(
                    period_start_str
                ).astimezone(UTC)
            except ValueError:
                self.logger.error(
                    f"Mecano: Date de début de période '{period_start_str}' invalide. Impossible de filtrer les logs de trading pour l'IA."
                )
                period_start_dt_utc = datetime.min.replace(
                    tzinfo=UTC
                )  # Fallback à une date très ancienne

            all_decision_logs = self.config_manager.get_config_history(
                filter_by={
                    "source": "ai_supervisor_log",
                    "timestamp_after": period_start_dt_utc.isoformat(),
                }  # Utilise isoformat pour le filtre
            ).to_dict("records")

            trading_summary_by_asset = {}

            for log_entry in all_decision_logs:
                if log_entry.get("reason", "").startswith("Feedback sur trade clôturé"):
                    decision_item = log_entry.get("generated_item", {})
                    outcome_result = log_entry.get("context_at_gen", {}).get(
                        "outcome", {}
                    )

                    asset = decision_item.get("asset", "UNKNOWN")
                    pnl_usd = outcome_result.get("pnl_usd", 0.0)

                    if asset not in trading_summary_by_asset:
                        trading_summary_by_asset[asset] = {
                            "total_pnl": 0.0,
                            "total_trades": 0,
                            "wins": 0,
                            "losses": 0,
                        }

                    asset_summary = trading_summary_by_asset[asset]
                    asset_summary["total_trades"] += 1
                    asset_summary["total_pnl"] += pnl_usd
                    if pnl_usd > 0:
                        asset_summary["wins"] += 1
                    elif pnl_usd < 0:
                        asset_summary["losses"] += 1

            if trading_summary_by_asset:
                prompt.append("## 4. Performance de Trading du Bot")
                for asset, summary in trading_summary_by_asset.items():
                    win_rate = (
                        (summary["wins"] / summary["total_trades"] * 100)
                        if summary["total_trades"] > 0
                        else 0.0
                    )
                    prompt.append(
                        f"- **{asset}**: Trades: {summary['total_trades']}, P&L: ${summary['total_pnl']:.2f}, WinRate: {win_rate:.2f}%"
                    )
                prompt.append("\n")
                # TODO: Inclure des détails sur les phases de marché dominantes lors des trades,
                #       les actifs les plus tradés, les win rates par stratégie/actif.
                #       Ceci nécessiterait un pré-traitement plus poussé des `log_decision` dans Mecano.

        # Vérification de la longueur et troncature pour respecter la limite de tokens de l'IA
        final_prompt = "\n".join(prompt)
        if len(final_prompt) > max_length:
            self.logger.warning(
                f"Mecano: Le prompt généré ({len(final_prompt)} chars) dépasse la limite configurée de {max_length}. Il sera tronqué."
            )
            final_prompt = final_prompt[:max_length] + "\n... (contenu tronqué)"

        self.logger.info(
            f"Mecano: Prompt IA généré (longueur: {len(final_prompt)} caractères)."
        )
        return final_prompt

    def analyze_report_with_llama(self, prompt: str) -> dict:
        """
        Envoie un prompt formaté à l'IA (instance réelle injectée) pour analyse du rapport de performance.
        Archive la réponse structurée de l'IA pour audit.

        Args:
            prompt (str): Le prompt textuel à envoyer à l'IA.

        Returns:
            dict: L'analyse structurée retournée par l'IA. Inclut un champ 'error' si l'analyse échoue.
        """
        self.logger.info("Mecano: Envoi du rapport pour analyse par l'IA...")
        try:
            if self.ai_analyzer is None:
                # Cette situation ne devrait pas se produire en production si main.py injecte correctement l'AI.
                # Pour les tests unitaires de Mecano, cela pourrait arriver.
                self.logger.error(
                    "Mecano: L'instance de l'analyseur IA n'a pas été injectée. Impossible de demander une analyse de rapport."
                )
                raise RuntimeError(
                    "L'instance de l'analyseur IA n'a pas été injectée dans Mecano."
                )

            # Appel à la vraie instance de AIDecision (passée via config_manager_instance dans __init__)
            # Assurez-vous que AIDecision.py a une méthode pour analyser un prompt
            analysis = self.ai_analyzer.get_structured_advice_from_prompt(prompt)

            # Archivage de la réponse de l'IA
            ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
            filename = f"{self.ia_analysis_log_file_prefix}{ts}.json"
            filepath = self.reports_dir / filename

            # Utilisation de l'écriture atomique pour la sauvegarde de l'analyse IA
            temp_filepath = filepath.with_suffix(".tmp")
            with open(temp_filepath, "w", encoding="utf-8") as f:
                # Utiliser CustomJSONEncoder pour gérer les types de données non sérialisables
                from core.config_manager import CustomJSONEncoder

                json.dump(analysis, f, indent=4, cls=CustomJSONEncoder)
            temp_filepath.rename(filepath)

            self.logger.info(
                f"Mecano: Analyse IA du rapport sauvegardée dans '{filepath}'"
            )

            # Envoyer une alerte si l'analyse IA est jugée critique ou contient des recommandations importantes
            if analysis.get("critical_issues_detected") or (
                analysis.get("recommendations")
                and self.config_manager.get("telegram.alerts_enabled", False)
            ):
                alert_message = f"🤖 **Analyse IA du Rapport**\nRésumé: {analysis.get('audit_summary', analysis.get('summary', 'N/A'))}\nRecommandations clés: {', '.join(analysis.get('recommendations', [])[:2])}"
                self.config_manager.send_alert(
                    alert_message, "telegram_critical"
                )  # Ou un nouveau canal "telegram_ai_report"

            return analysis
        except Exception as e:
            self.log_exception("analyze_report_with_llama", e)
            # Envoyer une alerte critique en cas d'échec de l'analyse IA du rapport
            if self.config_manager:
                self.config_manager.send_alert(
                    "CRITIQUE",
                    f"Mecano: Échec Analyse IA Rapport: {e}",
                    alert_type="telegram_critical",
                )
            return {"error": "L'analyse IA a échoué.", "details": str(e)}

    def export_report(self, report: dict, format: str = "json") -> None:
        """
        Exporte un rapport structuré (généralement le rapport hebdomadaire) vers un fichier.
        Les formats supportés sont JSON, CSV, et potentiellement Markdown ou HTML.
        Implémente une écriture atomique.

        Args:
            report (dict): Le dictionnaire du rapport à exporter.
            format (str): Le format d'export souhaité ('json', 'csv', 'md').

        Raises:
            ValueError: Si un format d'export non supporté est spécifié.
        """
        ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")  # Utiliser UTC
        # Le préfixe du nom de fichier est lu dynamiquement dans __init__
        filename = f"{self.weekly_report_file_prefix}{ts}.{format}"
        filepath = self.reports_dir / filename  # Construction du chemin avec Path

        # Utilisation de l'écriture atomique avec un fichier temporaire
        temp_filepath = filepath.with_suffix(".tmp")

        try:
            # S'assurer que le répertoire existe
            filepath.parent.mkdir(parents=True, exist_ok=True)

            if format == "json":
                with open(temp_filepath, "w", encoding="utf-8") as f:
                    from core.config_manager import CustomJSONEncoder

                    json.dump(report, f, indent=4, cls=CustomJSONEncoder)
            elif format == "csv":
                with open(temp_filepath, "w", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f)
                    # Écrire l'en-tête CSV
                    writer.writerow(["section", "key", "value"])
                    for section, data in report.items():
                        # Gérer les types complexes pour le CSV
                        if isinstance(data, dict):
                            for key, value in data.items():
                                # Convertir les valeurs complexes en chaîne JSON pour le CSV
                                writer.writerow(
                                    [
                                        section,
                                        key,
                                        json.dumps(value, cls=CustomJSONEncoder),
                                    ]
                                )
                        else:
                            writer.writerow(
                                [section, "", json.dumps(data, cls=CustomJSONEncoder)]
                            )
            elif format == "md":  # Export en Markdown
                with open(temp_filepath, "w", encoding="utf-8") as f:
                    f.write(
                        f"# Rapport Hebdomadaire SNIPER_X - {report.get('report_generated_at', 'N/A').split('T')[0]}\n\n"
                    )
                    f.write(
                        f"Période: {report.get('period_start').split('T')[0]} au {report.get('report_generated_at').split('T')[0]}\n\n"
                    )

                    # Résumé
                    f.write("## 1. Résumé Global\n")
                    for section, summary_data in report.get("summary", {}).items():
                        f.write(f"### {section.replace('_', ' ').title()}\n")
                        if isinstance(summary_data, dict):
                            for key, value in summary_data.items():
                                f.write(f"- {key.replace('_', ' ').title()}: {value}\n")
                        else:
                            f.write(f"- {summary_data}\n")
                        f.write("\n")

                    # Détails
                    f.write("## 2. Détails du Rapport\n")
                    for detail_section, detail_data in report.get(
                        "details", {}
                    ).items():
                        f.write(f"### {detail_section.replace('_', ' ').title()}\n")
                        if detail_section == "profiling_data":
                            df_profiling = pd.DataFrame(detail_data)
                            if not df_profiling.empty:
                                f.write(df_profiling.to_markdown(index=False) + "\n\n")
                        elif detail_section == "error_data":
                            df_errors = pd.DataFrame(detail_data)
                            if not df_errors.empty:
                                f.write(df_errors.to_markdown(index=False) + "\n\n")
                        elif detail_section == "system_metrics_data":
                            df_metrics = pd.DataFrame(detail_data)
                            if not df_metrics.empty:
                                f.write(df_metrics.to_markdown(index=False) + "\n\n")
                        elif detail_section == "config_snapshots_paths":
                            f.write("- Chemins des Snapshots de Configuration :\n")
                            for path in detail_data:
                                f.write(f"  - `{Path(path).name}`\n")
                            f.write("\n")
                        else:
                            f.write(
                                f"```json\n{json.dumps(detail_data, indent=2, cls=CustomJSONEncoder)}\n```\n\n"
                            )

            else:
                raise ValueError(
                    f"Mecano: Format d'export '{format}' non supporté. Formats acceptés: 'json', 'csv', 'md'."
                )

            temp_filepath.rename(filepath)  # Rendre atomique
            self.logger.info(f"Mecano: Rapport exporté avec succès vers '{filepath}'")

            # Envoyer une alerte Telegram si le canal est activé
            if self.config_manager and self.config_manager.get(
                "telegram.alerts_enabled", False
            ):
                alert_type = self.config_manager.get(
                    "mecano_settings.report_export_alert_channel", "telegram_info"
                )
                alert_message = (
                    f"📊 **Rapport Hebdomadaire SNIPER_X Généré**\n"
                    f"Période: {report.get('period_start').split('T')[0]} au {report.get('report_generated_at').split('T')[0]}\n"
                    f"Fichier: `{filepath.name}`\n"
                    f"Résumé: CPU Moy: {report['summary']['system_metrics_summary'].get('avg_cpu_percent')}% | RAM Moy: {report['summary']['system_metrics_summary'].get('avg_system_ram_percent')}% | Erreurs: {report['summary']['error_summary'].get('total_errors', 0)}"
                )
                self.config_manager.send_alert(alert_message, alert_type)

        except Exception as e:
            self.log_exception(
                "export_report", e
            )  # Log l'exception via la méthode dédiée
            # Envoyer une alerte critique en cas d'échec de l'exportation
            if self.config_manager:
                self.config_manager.send_alert(
                    "CRITIQUE",
                    f"Mecano: Échec Export Rapport: {e}",
                    alert_type="telegram_critical",
                )

    def check_resource_alerts(self) -> None:
        """
        Vérifie l'utilisation actuelle des ressources système (CPU et RAM) et logue un avertissement
        si les seuils configurés sont dépassés. Les seuils sont lus dynamiquement.
        """
        try:
            cpu = psutil.cpu_percent(interval=0.1)
            ram = psutil.virtual_memory().percent

            if cpu > self.cpu_threshold:
                self.error_logger.warning(
                    f"Mecano: ALERTE RESSOURCE: Usage CPU ({cpu:.1f}%) dépasse le seuil configuré de {self.cpu_threshold}%."
                )
                if self.config_manager and self.config_manager.get(
                    "telegram.alerts_enabled", False
                ):
                    self.config_manager.send_alert(
                        "ALERTE_CPU",
                        f"Mecano: CPU élevé ({cpu:.1f}%)!",
                        alert_type="telegram_critical",
                    )

            if ram > self.ram_threshold:
                self.error_logger.warning(
                    f"Mecano: ALERTE RESSOURCE: Usage RAM ({ram:.1f}%) dépasse le seuil configuré de {self.ram_threshold}%."
                )
                if self.config_manager and self.config_manager.get(
                    "telegram.alerts_enabled", False
                ):
                    self.config_manager.send_alert(
                        "ALERTE_RAM",
                        f"Mecano: RAM élevée ({ram:.1f}%)!",
                        alert_type="telegram_critical",
                    )

        except NotImplementedError as nie:
            self.logger.warning(
                f"Mecano: La vérification des ressources est désactivée car psutil n'est pas complètement fonctionnel: {nie}."
            )
        except Exception as e:
            self.log_exception("check_resource_alerts", e)


if __name__ == "__main__":
    # Les imports nécessaires pour ce bloc __main__ (ceux qui devraient être au top niveau du fichier)
    # pandas, datetime, timedelta, UTC, Path, os, sys, logging, time, json, csv, shutil
    # ConfigManager et CustomJSONEncoder

    print("--- Démonstration de la classe Mecano ---")

    # Configuration minimale du logging pour la démo autonome
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
    )
    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(logging.StreamHandler(sys.stdout))

    # Définition des fonctions d'aide déplacées DANS le bloc __main__ pour résoudre les problèmes de portée Pylance.
    # Cela les rend locales et autonomes pour ce script de démonstration.

    def _setup_demo_environment(config_dir: str, schemas_dir: str):
        Path(config_dir).mkdir(parents=True, exist_ok=True)
        Path(schemas_dir).mkdir(parents=True, exist_ok=True)

        # Création du schéma principal (main_app_schema.json)
        main_app_schema = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "title": "Main App Configuration Schema",
            "type": "object",
            "properties": {
                "project": {"type": "string", "const": "SNIPER_X"},
                "log_level": {
                    "type": "string",
                    "enum": ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
                },
                "paths": {
                    "type": "object",
                    "properties": {
                        "broker_accounts_config": {"type": "string"},
                        "strategy_configs": {"type": "string"},
                    },
                    "required": ["broker_accounts_config", "strategy_configs"],
                },
                "strategies": {
                    "type": "object",
                    "properties": {
                        "default_strategy": {"type": "string"},
                        "config_mapping": {
                            "type": "object",
                            "patternProperties": {".*\\.json$": {"type": "string"}},
                        },
                    },
                    "required": ["default_strategy", "config_mapping"],
                },
                "ai": {
                    "type": "object",
                    "properties": {"enabled": {"type": "boolean"}},
                },
                "telegram": {
                    "type": "object",
                    "properties": {"enabled": {"type": "boolean"}},
                },
                "bot_behavior": {
                    "type": "object",
                    "properties": {"cycle_interval_seconds": {"type": "integer"}},
                },
                "app": {
                    "type": "object",
                    "properties": {"audit_log_rotation": {"type": "object"}},
                },
            },
            "required": ["project", "log_level", "paths", "strategies"],
        }
        with open(Path(schemas_dir) / "main_app_schema.json", "w") as f:
            json.dump(main_app_schema, f, indent=4)

        # Création du schéma de stratégie (strategy_schema.json)
        strategy_schema = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "title": "Strategy Schema",
            "type": "object",
            "properties": {
                "strategy_name": {"type": "string"},
                "strategy_tags": {"type": "array", "items": {"type": "string"}},
                "risk_per_trade_percent": {"type": "number", "minimum": 0},
                "max_drawdown_percent": {"type": "number", "minimum": 0},
                "magic_number": {"type": "integer"},
            },
            "required": [
                "strategy_name",
                "strategy_tags",
                "risk_per_trade_percent",
                "max_drawdown_percent",
                "magic_number",
            ],
        }
        with open(Path(schemas_dir) / "strategy_schema.json", "w") as f:
            json.dump(strategy_schema, f, indent=4)

        # Création du schéma pour broker_accounts.json
        broker_accounts_schema = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "title": "Broker Accounts Configuration Schema",
            "type": "object",
            "properties": {
                "accounts": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "account_id": {"type": "string"},
                            "mode": {"type": "string", "enum": ["LIVE", "DEMO"]},
                            "broker_name": {"type": "string"},
                            "server_type": {"type": "string"},
                            "login_env_var": {"type": "string"},
                            "password_env_var": {"type": "string"},
                            "is_active": {"type": "boolean"},
                            "priority": {"type": "integer"},
                            "allowed_symbols": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "account_type": {"type": "string"},
                            "trade_settings": {"type": "object"},
                        },
                        "required": [
                            "account_id",
                            "mode",
                            "broker_name",
                            "server_type",
                            "login_env_var",
                            "password_env_var",
                            "is_active",
                            "priority",
                        ],
                    },
                },
                "default_live_account": {"type": "string"},
                "default_demo_account": {"type": "string"},
            },
            "required": ["accounts"],
        }
        with open(Path(schemas_dir) / "broker_accounts_schema.json", "w") as f:
            json.dump(broker_accounts_schema, f, indent=4)

        # Create mock strategy files
        valid_strategy = {
            "strategy_name": "Demo Scalping Strategy",
            "strategy_tags": ["scalping", "demo_strategy"],
            "risk_per_trade_percent": 0.1,
            "max_drawdown_percent": 2.0,
            "magic_number": 12345,
        }
        with open(Path(config_dir) / "scalping_demo.json", "w") as f:
            json.dump(valid_strategy, f, indent=4)

        invalid_strategy = {"risk_per_trade_percent": 1.0}
        with open(Path(config_dir) / "invalid_strategy.json", "w") as f:
            json.dump(invalid_strategy, f, indent=4)

        # Create mock prod_config.json
        main_config = {
            "project": "SNIPER_X",
            "log_level": "INFO",
            "paths": {
                "broker_accounts_config": str(
                    Path(config_dir) / "broker_accounts.json"
                ),
                "strategy_configs": str(config_dir),
            },
            "strategies": {
                "default_strategy": "scalping_demo",
                "config_mapping": {"scalping_demo": "scalping_demo.json"},
            },
            "ai": {"enabled": False},
            "telegram": {"enabled": False},
            "bot_behavior": {
                "cycle_interval_seconds": 1,
                "blacklist_duration_minutes": 1,
            },
            "app": {
                "audit_log_rotation": {"enabled": True, "type": "daily", "max_files": 3}
            },
        }
        with open(Path(config_dir) / "prod_config.json", "w") as f:
            json.dump(main_config, f, indent=4)

        # Create mock broker_accounts.json
        broker_accounts_demo = {
            "accounts": [
                {
                    "account_id": "demo_test_account_A",
                    "mode": "DEMO",
                    "broker_name": "DemoBroker",
                    "server_type": "DemoServer",
                    "login_env_var": "DEMO_MT5_LOGIN_A",
                    "password_env_var": "DEMO_MT5_PASSWORD_A",
                    "is_active": True,
                    "priority": 1,
                }
            ],
            "default_demo_account": "demo_test_account_A",
        }
        with open(Path(config_dir) / "broker_accounts.json", "w") as f:
            json.dump(broker_accounts_demo, f, indent=4)

        # Create mock .env.local
        with open(Path(config_dir).parent / ".env.local", "w") as f:
            f.write("DEMO_MT5_LOGIN_A=12345\n")
            f.write("DEMO_MT5_PASSWORD_A=demo_pass\n")
            f.write("TELEGRAM_BOT_TOKEN=YOUR_DEMO_TELEGRAM_BOT_TOKEN\n")
            f.write("TELEGRAM_CHAT_ID=YOUR_DEMO_TELEGRAM_CHAT_ID\n")
            f.write("GEMINI_API_KEY=YOUR_DEMO_GEMINI_API_KEY\n")

    def _get_demo_context_for_mecano_test() -> dict:
        mock_df_eurusd = pd.DataFrame(
            {
                "open": [1.0800, 1.0805, 1.0810, 1.0808, 1.0815],
                "high": [1.0810, 1.0815, 1.0820, 1.0818, 1.0825],
                "low": [1.0795, 1.0800, 1.0805, 1.0803, 1.0810],
                "close": [1.0805, 1.0810, 1.0808, 1.0815, 1.0820],
                "tick_volume": [100, 120, 90, 110, 130],
                "timestamp": pd.to_datetime(
                    [
                        "2025-01-01 10:00:00",
                        "2025-01-01 10:01:00",
                        "2025-01-01 10:02:00",
                        "2025-01-01 10:03:00",
                        "2025-01-01 10:04:00",
                    ],
                    utc=True,
                ),
                "phase": [
                    "trending_up",
                    "trending_up",
                    "consolidation",
                    "expansion_up",
                    "scalp_burst",
                ],
                "confidence_score": [0.7, 0.75, 0.5, 0.8, 0.9],
                "volume_momentum": [0.1, 0.2, -0.1, 0.6, 0.8],
                "nearest_liquidity_level_details": [
                    {"type": "EQH", "level": 1.0830, "distance_pips": 10}
                    for _ in range(5)
                ],
                "entry_confirmation_bullish": [False, False, False, True, True],
                "entry_confirmation_bearish": [False, False, False, False, False],
                "ob_details": [
                    None,
                    None,
                    None,
                    {"type": "bullish", "top": 1.0800, "bottom": 1.0790},
                    None,
                ],
                "fvg_details": [
                    None,
                    None,
                    {"type": "bullish", "top": 1.0798, "bottom": 1.0790},
                    None,
                    None,
                ],
                "bos_mss_details": [
                    None,
                    {"type": "bullish_bos", "level_broken": 1.0800},
                    None,
                    None,
                    None,
                ],
                "liquidity_grab_details": [
                    None,
                    None,
                    None,
                    {"type": "bullish_sweep", "level_swept": 1.0790},
                    None,
                ],
                "is_liquid": [True, True, True, True, True],
                "current_spread_points": [1.0, 1.1, 1.0, 1.2, 1.1],
                "last_update_timestamp": [
                    pd.Timestamp("2025-01-01 10:04:00", tz=UTC).isoformat()
                ]
                * 5,
            }
        )

        mock_df_gbpusd = pd.DataFrame(
            {
                "open": [1.2500, 1.2502, 1.2505, 1.2503, 1.2508],
                "high": [1.2505, 1.2508, 1.2510, 1.2509, 1.2515],
                "low": [1.2498, 1.2500, 1.2503, 1.2501, 1.2505],
                "close": [1.2502, 1.2505, 1.2503, 1.2508, 1.2512],
                "tick_volume": [80, 90, 70, 100, 110],
                "timestamp": pd.to_datetime(
                    [
                        "2025-01-01 10:00:00",
                        "2025-01-01 10:01:00",
                        "2025-01-01 10:02:00",
                        "2025-01-01 10:03:00",
                        "2025-01-01 10:04:00",
                    ],
                    utc=True,
                ),
                "phase": [
                    "range",
                    "range",
                    "consolidation",
                    "trending_down",
                    "manipulation",
                ],
                "confidence_score": [0.6, 0.65, 0.55, 0.7, 0.85],
                "volume_momentum": [0.0, 0.1, -0.2, -0.3, 0.7],
                "nearest_liquidity_level_details": [
                    None,
                    None,
                    None,
                    None,
                    {"type": "EQL", "level": 1.2495, "distance_pips": 5},
                ],
                "entry_confirmation_bullish": [False, False, True, False, False],
                "entry_confirmation_bearish": [False, False, True, False, False],
                "ob_details": [
                    None,
                    None,
                    None,
                    None,
                    {"type": "bearish", "top": 1.2510, "bottom": 1.2500},
                ],
                "fvg_details": [None, None, None, None, None],
                "bos_mss_details": [
                    None,
                    None,
                    None,
                    {"type": "bearish_bos", "level_broken": 1.2505},
                    None,
                ],
                "liquidity_grab_details": [
                    None,
                    None,
                    None,
                    None,
                    {"type": "bearish_sweep", "level_swept": 1.2498},
                ],
                "is_liquid": [True, True, True, True, True],
                "current_spread_points": [1.5, 1.6, 1.4, 1.7, 1.5],
                "last_update_timestamp": [
                    pd.Timestamp("2025-01-01 10:04:00", tz=UTC).isoformat()
                ]
                * 5,
            }
        )

        return {
            "market_data": {"EURUSD": mock_df_eurusd, "GBPUSD": mock_df_gbpusd},
            "economic_calendar": [
                {
                    "time": "2025-01-01T10:30:00Z",
                    "event": "NFP",
                    "impact": "High",
                    "currency": "USD",
                }
            ],
            "account_info": {
                "equity": 10500.0,
                "balance": 10000.0,
                "profit": 500.0,
                "login": 12345,
                "account_id": "demo_test_account_A",
            },
            "open_positions": [],
            "vix_index": 18.5,
            "trading_signals": {
                "EURUSD": {
                    "confidence_score": mock_df_eurusd.iloc[-1]["confidence_score"],
                    "phase": mock_df_eurusd.iloc[-1]["phase"],
                    "buy_signal": mock_df_eurusd.iloc[-1]["entry_confirmation_bullish"],
                    "sell_signal": mock_df_eurusd.iloc[-1][
                        "entry_confirmation_bearish"
                    ],
                    "is_liquid": mock_df_eurusd.iloc[-1]["is_liquid"],
                    "current_price": mock_df_eurusd.iloc[-1]["close"],
                    "current_spread_points": mock_df_eurusd.iloc[-1][
                        "current_spread_points"
                    ],
                    "last_update_timestamp": mock_df_eurusd.iloc[-1][
                        "last_update_timestamp"
                    ],
                    "volume_momentum": mock_df_eurusd.iloc[-1]["volume_momentum"],
                    "nearest_liquidity_level_details": mock_df_eurusd.iloc[-1][
                        "nearest_liquidity_level_details"
                    ],
                    "volume_anomaly_details": mock_df_eurusd.iloc[-1][
                        "volume_anomaly_details"
                    ],
                    "ob_details": mock_df_eurusd.iloc[-1]["ob_details"],
                    "fvg_details": mock_df_eurusd.iloc[-1]["fvg_details"],
                    "bos_mss_details": mock_df_eurusd.iloc[-1]["bos_mss_details"],
                    "liquidity_grab_details": mock_df_eurusd.iloc[-1][
                        "liquidity_grab_details"
                    ],
                    "eqh_eql_details": mock_df_eurusd.iloc[-1]["eqh_eql_details"],
                    "validated_ob": mock_df_eurusd.iloc[-1]["validated_ob"],
                },
                "GBPUSD": {
                    "confidence_score": mock_df_gbpusd.iloc[-1]["confidence_score"],
                    "phase": mock_df_gbpusd.iloc[-1]["phase"],
                    "buy_signal": mock_df_gbpusd.iloc[-1]["entry_confirmation_bullish"],
                    "sell_signal": mock_df_gbpusd.iloc[-1][
                        "entry_confirmation_bearish"
                    ],
                    "is_liquid": mock_df_gbpusd.iloc[-1]["is_liquid"],
                    "current_price": mock_df_gbpusd.iloc[-1]["close"],
                    "current_spread_points": mock_df_gbpusd.iloc[-1][
                        "current_spread_points"
                    ],
                    "last_update_timestamp": mock_df_gbpusd.iloc[-1][
                        "last_update_timestamp"
                    ],
                    "volume_momentum": mock_df_gbpusd.iloc[-1]["volume_momentum"],
                    "nearest_liquidity_level_details": mock_df_gbpusd.iloc[-1][
                        "nearest_liquidity_level_details"
                    ],
                    "volume_anomaly_details": mock_df_gbpusd.iloc[-1][
                        "volume_anomaly_details"
                    ],
                    "ob_details": mock_df_gbpusd.iloc[-1]["ob_details"],
                    "fvg_details": mock_df_gbpusd.iloc[-1]["fvg_details"],
                    "bos_mss_details": mock_df_gbpusd.iloc[-1]["bos_mss_details"],
                    "liquidity_grab_details": mock_df_gbpusd.iloc[-1][
                        "liquidity_grab_details"
                    ],
                    "eqh_eql_details": mock_df_gbpusd.iloc[-1]["eqh_eql_details"],
                    "validated_ob": mock_df_gbpusd.iloc[-1]["validated_ob"],
                },
            },
            "current_market_regime": "trending_up_normal_volatility",
            "market_volatility_percentage": 0.85,
            "current_time_utc": pd.Timestamp("2025-01-01 10:04:00", tz=UTC),
        }
