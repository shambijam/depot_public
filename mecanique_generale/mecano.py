# core/mecano.py

import os
import time
import json
import csv
import sys
import pandas as pd
from datetime import datetime, timedelta, UTC
import logging
import traceback
from contextlib import contextmanager
from typing import List, Dict, Any, Optional
from pathlib import Path
from core.config_manager import ConfigManager
from core.ai_interface import AIInterface  # Import pour couplage avec AIInterface

# Initialisation du Logger pour ce module
logger = logging.getLogger(__name__)

# Gestion Conditionnelle de psutil
try:
    import psutil
except ImportError:
    logger.warning("psutil non installé. Monitoring ressources limité.")
    class psutil:
        @staticmethod
        def cpu_percent(interval=None):
            return 0.0

        @staticmethod
        def virtual_memory():
            class MockVirtualMemory:
                percent = 0.0
            return MockVirtualMemory()

        @staticmethod
        def boot_time():
            return time.time()

        @staticmethod
        def Process(pid):
            class MockProcess:
                def memory_info(self):
                    class MockMemoryInfo:
                        rss = 0
                    return MockMemoryInfo()

                def num_threads(self):
                    return 0
            return MockProcess(pid)

class Mecano:
    """
    Observateur silencieux pour monitoring de performance, erreurs et ressources.
    Collecte métriques et génère rapports sans interférer avec le pipeline.
    Couplage asynchrone avec AIInterface pour analyses consultatives.
    """

    def __init__(self, config_manager_instance: Optional[ConfigManager] = None, ai_interface: Optional[AIInterface] = None):
        """
        Initialise Mecano avec ConfigManager et AIInterface pour rapports.

        Args:
            config_manager_instance: ConfigManager pour configs dynamiques.
            ai_interface: AIInterface pour analyses consultatives (injectée).
        """
        self.logger = logging.getLogger(__name__)
        self.config_manager = config_manager_instance
        self.ai_interface = ai_interface  # Injectée pour couplage IA
        if self.config_manager is None:
            self.logger.warning("Mecano sans ConfigManager. Chemins non dynamiques.")
        if self.ai_interface is None:
            self.logger.warning("Mecano sans AIInterface. Rapports IA désactivés.")

        self.logs_dir = Path(self.config_manager.get("paths.logs", "logs/")) if self.config_manager else Path("logs/")
        self.reports_dir = Path(self.config_manager.get("paths.reports", "output/")) if self.config_manager else Path("output/")
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.reports_dir.mkdir(parents=True, exist_ok=True)

        self.performance_log_file_name = self.config_manager.get("mecano_settings.performance_log_file_name", "performance.log") if self.config_manager else "performance.log"
        self.errors_log_file_name = self.config_manager.get("mecano_settings.errors_log_file_name", "errors.log") if self.config_manager else "errors.log"

        self._setup_loggers()

        try:
            self.process = psutil.Process(os.getpid())
            self.logger.info("psutil.Process initialisé.")
        except Exception as e:
            self.logger.warning(f"Échec init psutil.Process: {e}. Métriques limitées.")
            self.process = None

        self.config_snapshot_file_prefix = self.config_manager.get("mecano_settings.config_snapshot_file_prefix", "config_snapshot_") if self.config_manager else "config_snapshot_"
        self.ia_analysis_log_file_prefix = self.config_manager.get("mecano_settings.ia_analysis_log_file_prefix", "ia_analysis_") if self.config_manager else "ia_analysis_"
        self.weekly_report_file_prefix = self.config_manager.get("mecano_settings.weekly_report_file_prefix", "weekly_report_") if self.config_manager else "weekly_report_"

        self.cpu_threshold = self.config_manager.get("mecano_settings.resource_alerts.cpu_threshold", 80.0) if self.config_manager else 80.0
        self.ram_threshold = self.config_manager.get("mecano_settings.resource_alerts.ram_threshold", 80.0) if self.config_manager else 80.0
        self.ia_prompt_max_length = self.config_manager.get("mecano_settings.ai_prompt_settings.max_length", 3500) if self.config_manager else 3500
        self.ia_prompt_top_n_slowest_steps = self.config_manager.get("mecano_settings.ia_prompt_top_n_slowest_steps", 5) if self.config_manager else 5
        self.report_period_days = self.config_manager.get("mecano_settings.report_period_days", 7) if self.config_manager else 7

        self.profiling_data: List[Dict[str, Any]] = []
        self.error_data: List[Dict[str, Any]] = []
        self.system_metrics_history: List[Dict[str, Any]] = []
        self.config_snapshots: List[str] = []

        self.logger.info("Mecano initialisé. Prêt pour monitoring et rapports.")

    def _setup_loggers(self) -> None:
        """
        Configure loggers pour performance et erreurs, sans duplication.
        """
        if not self.logger.handlers:
            self.logger.setLevel(logging.INFO)
            ch = logging.StreamHandler(sys.stdout)
            ch.setFormatter(logging.Formatter("%(asctime)s - [Mecano] - %(levelname)s - %(message)s"))
            self.logger.addHandler(ch)

        self.performance_logger = logging.getLogger("MecanoPerformance")
        perf_path = str(self.logs_dir / self.performance_log_file_name)
        if not any(isinstance(h, logging.FileHandler) and h.baseFilename == perf_path for h in self.performance_logger.handlers):
            self.performance_logger.setLevel(logging.INFO)
            fh_perf = logging.FileHandler(perf_path, mode="a", encoding="utf-8")
            fh_perf.setFormatter(logging.Formatter("%(asctime)s - %(message)s"))
            self.performance_logger.addHandler(fh_perf)
            self.performance_logger.propagate = False

        self.error_logger = logging.getLogger("MecanoErrors")
        err_path = str(self.logs_dir / self.errors_log_file_name)
        if not any(isinstance(h, logging.FileHandler) and h.baseFilename == err_path for h in self.error_logger.handlers):
            self.error_logger.setLevel(logging.WARNING)
            fh_err = logging.FileHandler(err_path, mode="a", encoding="utf-8")
            fh_err.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
            self.error_logger.addHandler(fh_err)
            self.error_logger.propagate = False

    @contextmanager
    def profile_step(self, step_name: str):
        """
        Context manager pour mesurer temps d'exécution d'une étape.
        """
        start_time = time.perf_counter()
        try:
            yield
        finally:
            duration = time.perf_counter() - start_time
            self.performance_logger.info(f"PROFILING - Step: '{step_name}' - Duration: {duration:.6f} seconds")
            self.profiling_data.append({"step": step_name, "duration_s": duration, "timestamp": datetime.now(UTC).isoformat()})

    def log_exception(self, step_name: str, exception: Exception):
        """
        Enregistre exception avec détails.
        """
        exc_type = type(exception).__name__
        exc_msg = str(exception)
        stack_trace = traceback.format_exc()
        log_message = f"EXCEPTION in step '{step_name}'\nType: {exc_type}\nMessage: {exc_msg}\nStackTrace:\n{stack_trace}\n----------------------------------------"
        self.error_logger.error(log_message)
        self.error_data.append({"step": step_name, "type": exc_type, "message": exc_msg, "timestamp": datetime.now(UTC).isoformat()})

        if self.config_manager and self.config_manager.get("telegram.alerts_enabled", False):
            alert_type = self.config_manager.get("mecano_settings.alert_on_exception_channel", "telegram_critical")
            alert_message = f"🚨 MECANO ERREUR dans `{step_name}`\nType: `{exc_type}`\nMessage: `{exc_msg}`"
            self.config_manager.send_alert(alert_message, alert_type)

    def snapshot_config(self, config: dict):
        """
        Sauvegarde snapshot de config.
        """
        try:
            ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
            filename = f"{self.config_snapshot_file_prefix}{ts}.json"
            filepath = self.reports_dir / filename
            temp_filepath = filepath.with_suffix(".tmp")
            with open(temp_filepath, "w", encoding="utf-8") as f:
                from core.config_manager import CustomJSONEncoder
                json.dump(config, f, indent=4, cls=CustomJSONEncoder)
            temp_filepath.rename(filepath)
            self.logger.info(f"Snapshot config sauvegardé: '{filepath}'")
            self.config_snapshots.append(str(filepath))
        except Exception as e:
            self.log_exception("snapshot_config", e)

    def collect_system_metrics(self) -> dict:
        """
        Collecte métriques système.
        """
        metrics = {
            "timestamp": datetime.now(UTC).isoformat(),
            "cpu_percent": psutil.cpu_percent(interval=0.1),
            "system_ram_percent": psutil.virtual_memory().percent,
            "process_ram_mb": self.process.memory_info().rss / (1024 * 1024) if self.process else 0,
            "process_threads": self.process.num_threads() if self.process else 0,
            "system_uptime_hours": (time.time() - psutil.boot_time()) / 3600,
        }
        self.system_metrics_history.append(metrics)

        if metrics["cpu_percent"] > self.cpu_threshold and self.config_manager:
            self.config_manager.send_alert("ALERTE_CPU", f"CPU élevé ({metrics['cpu_percent']:.1f}%)!", "telegram_critical")
        if metrics["system_ram_percent"] > self.ram_threshold and self.config_manager:
            self.config_manager.send_alert("ALERTE_RAM", f"RAM élevée ({metrics['system_ram_percent']:.1f}%)!", "telegram_critical")

        return metrics

    def build_weekly_report(self) -> dict:
        """
        Compile rapport pour période configurable.
        """
        period_start_utc = datetime.now(UTC) - timedelta(days=self.report_period_days)
        filtered_profiling = [d for d in self.profiling_data if datetime.fromisoformat(d["timestamp"]) >= period_start_utc]
        filtered_errors = [d for d in self.error_data if datetime.fromisoformat(d["timestamp"]) >= period_start_utc]
        filtered_metrics = [d for d in self.system_metrics_history if datetime.fromisoformat(d["timestamp"]) >= period_start_utc]

        report = {
            "report_generated_at": datetime.now(UTC).isoformat(),
            "period_start": period_start_utc.isoformat(),
            "summary": {},
            "details": {
                "profiling_data": filtered_profiling,
                "error_data": filtered_errors,
                "system_metrics_data": filtered_metrics,
                "config_snapshots_paths": self.config_snapshots,
            },
        }

        # Profiling summary
        if filtered_profiling:
            durations = [d["duration_s"] for d in filtered_profiling]
            report["summary"]["profiling_summary"] = {
                "total_steps": len(durations),
                "avg_duration_s": sum(durations) / len(durations) if durations else 0,
                "max_duration_s": max(durations) if durations else 0,
                "top_slowest_steps": sorted(filtered_profiling, key=lambda x: x["duration_s"], reverse=True)[:self.ia_prompt_top_n_slowest_steps],
            }

        # Error summary
        if filtered_errors:
            error_types = [e["type"] for e in filtered_errors]
            report["summary"]["error_summary"] = {
                "total_errors": len(filtered_errors),
                "unique_types": list(set(error_types)),
                "most_frequent": max(set(error_types), key=error_types.count) if error_types else "N/A",
            }

        # Metrics summary
        if filtered_metrics:
            cpu_vals = [m["cpu_percent"] for m in filtered_metrics if m["cpu_percent"] is not None]
            ram_vals = [m["system_ram_percent"] for m in filtered_metrics if m["system_ram_percent"] is not None]
            report["summary"]["system_metrics_summary"] = {
                "avg_cpu_percent": sum(cpu_vals) / len(cpu_vals) if cpu_vals else 0,
                "max_cpu_percent": max(cpu_vals) if cpu_vals else 0,
                "avg_ram_percent": sum(ram_vals) / len(ram_vals) if ram_vals else 0,
                "max_ram_percent": max(ram_vals) if ram_vals else 0,
            }

        return report

    def build_ia_prompt(self, report: dict) -> str:
        """
        Construit prompt pour IA à partir de rapport.
        """
        prompt = []
        prompt.append("ANALYSE PERFORMANCE SNIPER_X")
        prompt.append(f"Rapport: {report['period_start'].split('T')[0]} à {report['report_generated_at'].split('T')[0]}")
        prompt.append("CONTEXTE: Expert en bot trading. Analyse goulots, risques, recommandations.")

        # Ajout sections comme avant, tronqué si > max_length

        final_prompt = "\n".join(prompt)
        if len(final_prompt) > self.ia_prompt_max_length:
            final_prompt = final_prompt[:self.ia_prompt_max_length] + "... (tronqué)"
        return final_prompt

    def analyze_report_with_ia(self, prompt: str) -> dict:
        """
        Envoie prompt à AIInterface pour analyse consultative.
        """
        if not self.ai_interface:
            return {"error": "AIInterface non injectée."}
        try:
            analysis = self.ai_interface.get_decision(prompt)  # Adaptez à méthode réelle
            ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
            filename = f"{self.ia_analysis_log_file_prefix}{ts}.json"
            filepath = self.reports_dir / filename
            temp_filepath = filepath.with_suffix(".tmp")
            with open(temp_filepath, "w", encoding="utf-8") as f:
                json.dump(analysis, f, indent=4)
            temp_filepath.rename(filepath)
            self.logger.info(f"Analyse IA sauvegardée: '{filepath}'")
            return analysis
        except Exception as e:
            self.log_exception("analyze_report_with_ia", e)
            return {"error": str(e)}

    def export_report(self, report: dict, format: str = "json") -> None:
        """
        Exporte rapport en format choisi.
        """
        ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        filename = f"{self.weekly_report_file_prefix}{ts}.{format}"
        filepath = self.reports_dir / filename
        temp_filepath = filepath.with_suffix(".tmp")
        try:
            if format == "json":
                with open(temp_filepath, "w", encoding="utf-8") as f:
                    json.dump(report, f, indent=4)
            # Ajoutez autres formats comme avant
            temp_filepath.rename(filepath)
            self.logger.info(f"Rapport exporté: '{filepath}'")
        except Exception as e:
            self.log_exception("export_report", e)

    def check_resource_alerts(self) -> None:
        """
        Vérifie ressources et alerte si seuils dépassés.
        """
        try:
            cpu = psutil.cpu_percent(interval=0.1)
            ram = psutil.virtual_memory().percent
            if cpu > self.cpu_threshold and self.config_manager:
                self.config_manager.send_alert("ALERTE_CPU", f"CPU élevé ({cpu:.1f}%)!", "telegram_critical")
            if ram > self.ram_threshold and self.config_manager:
                self.config_manager.send_alert("ALERTE_RAM", f"RAM élevée ({ram:.1f}%)!", "telegram_critical")
        except Exception as e:
            self.log_exception("check_resource_alerts", e)