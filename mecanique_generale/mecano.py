# core/mecano.py

import os
import time
import json
import csv
import sys
import pandas as pd
import logging
import traceback
from contextlib import contextmanager
from typing import List, Dict, Any, Optional
from pathlib import Path
from core.config_manager import ConfigManager
# === [IA SUPPRIMÉE - Session 23 Nov 2025] ===
# from core.ai_interface import AIInterface (module supprimé)
from tempfile import NamedTemporaryFile
from datetime import datetime, UTC

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
    === [IA SUPPRIMÉE - Session 23 Nov 2025] ===
    Couplage AIInterface retiré (module supprimé)
    """

    def __init__(self, config_manager_instance: Optional[ConfigManager] = None):
        """
        Initialise Mecano avec ConfigManager pour configs dynamiques.
        === [IA SUPPRIMÉE - Session 23 Nov 2025] ===
        Instanciation AIInterface retirée (module supprimé)
        """
        self.logger = logging.getLogger(__name__)
        self.config_manager = config_manager_instance
        if self.config_manager is None:
            self.logger.warning("Mecano sans ConfigManager. Chemins non dynamiques.")

        # === [IA SUPPRIMÉE - Session 23 Nov 2025] ===
        # self.ai_interface = AIInterface(...) (module supprimé)
        # Rapports IA désormais désactivés

        self.logs_dir = Path(self.config_manager.get("paths.logs", "logs/")) if self.config_manager else Path("logs/")
        self.reports_dir = Path(self.config_manager.get("paths.ai_audit", "config/ai_audit")) if self.config_manager else Path("output/")
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

    # === [MÉTHODE IA SUPPRIMÉE - Session 23 Nov 2025] ===
    # def set_ai_analyzer(self, ai_analyzer_instance):
    #     Injection AIDecision retirée (module ai_interface supprimé)

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
        Compile un rapport d'observation (profiling/erreurs/metrics) sur la période configurée.

        - Utilise self.report_period_days (fallback 7) pour définir la fenêtre [now-Δ, now].
        - Tolère différents formats ISO pour les timestamps (avec/without timezone, 'Z', etc.).
        - Robuste aux clés manquantes et aux valeurs None/NaN.
        - Fournit un résumé (summary) + détails (details) prêt à exporter.
        """
        from datetime import datetime, UTC, timedelta
        from math import isnan

        # ---------- Helpers ----------
        def _safe_get(obj, key, default=None):
            try:
                v = obj.get(key, default)
            except Exception:
                v = default
            return v

        def _safe_float(x, default=0.0):
            try:
                if x is None:
                    return default
                v = float(x)
                # gérer NaN
                return default if isnan(v) else v
            except Exception:
                return default

        def _parse_ts(ts_str):
            """
            Parse ISO-8601 en objet datetime timezone-aware (UTC si absent).
            Accepte: '2025-08-24T12:34:56', '2025-08-24T12:34:56Z', '...+00:00'
            Retourne None si parsing impossible.
            """
            if not ts_str:
                return None
            try:
                # Python >=3.11 comprend 'Z' via fromisoformat? Pas toujours -> normaliser.
                ts_norm = str(ts_str).strip().replace("Z", "+00:00")
                dt = datetime.fromisoformat(ts_norm)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=UTC)
                return dt.astimezone(UTC)
            except Exception:
                return None

        # ---------- Fenêtre temporelle ----------
        try:
            period_days = int(getattr(self, "report_period_days", 7) or 7)
            if period_days <= 0:
                period_days = 7
        except Exception:
            period_days = 7

        now_utc = datetime.now(UTC)
        period_start_utc = now_utc - timedelta(days=period_days)

        # ---------- Sources de données (tolérance aux attributs manquants) ----------
        profiling_data = getattr(self, "profiling_data", []) or []
        error_data = getattr(self, "error_data", []) or []
        system_metrics_history = getattr(self, "system_metrics_history", []) or []
        config_snapshots = getattr(self, "config_snapshots", []) or []

        # ---------- Filtrage par période ----------
        def _filter_by_period(items, ts_key="timestamp"):
            out = []
            for d in items:
                if not isinstance(d, dict):
                    continue
                ts = _parse_ts(_safe_get(d, ts_key))
                if ts is None:
                    continue
                if ts >= period_start_utc:
                    out.append(d)
            return out

        filtered_profiling = _filter_by_period(profiling_data, "timestamp")
        filtered_errors = _filter_by_period(error_data, "timestamp")
        filtered_metrics = _filter_by_period(system_metrics_history, "timestamp")

        # ---------- Rapport ----------
        report = {
            "report_generated_at": now_utc.isoformat(),
            "period_start": period_start_utc.isoformat(),
            "period_days": period_days,
            "summary": {},
            "details": {
                "profiling_data": filtered_profiling,
                "error_data": filtered_errors,
                "system_metrics_data": filtered_metrics,
                "config_snapshots_paths": config_snapshots,
            },
        }

        # ---------- Profiling summary ----------
        if filtered_profiling:
            # Durations sécurisées
            durations = [_safe_float(_safe_get(d, "duration_s")) for d in filtered_profiling]
            durations = [x for x in durations if x >= 0]
            total_steps = len(durations)
            avg_duration = (sum(durations) / total_steps) if total_steps else 0.0
            max_duration = max(durations) if durations else 0.0

            # top N (fallback 10)
            try:
                top_n = int(getattr(self, "ia_prompt_top_n_slowest_steps", 10) or 10)
                if top_n <= 0:
                    top_n = 10
            except Exception:
                top_n = 10

            top_slowest = sorted(
                filtered_profiling,
                key=lambda x: _safe_float(_safe_get(x, "duration_s")),
                reverse=True,
            )[:top_n]

            report["summary"]["profiling_summary"] = {
                "total_steps": total_steps,
                "avg_duration_s": round(avg_duration, 6),
                "max_duration_s": round(max_duration, 6),
                "top_slowest_steps": top_slowest,
            }

        # ---------- Error summary ----------
        if filtered_errors:
            types = []
            for e in filtered_errors:
                t = _safe_get(e, "type", "UnknownError")
                types.append(str(t))
            unique_types = sorted(set(types))
            most_frequent = "N/A"
            if types:
                # compter sans collections.Counter pour rester light
                counts = {}
                for t in types:
                    counts[t] = counts.get(t, 0) + 1
                most_frequent = max(counts, key=counts.get)

            report["summary"]["error_summary"] = {
                "total_errors": len(filtered_errors),
                "unique_types": unique_types,
                "most_frequent": most_frequent,
            }

        # ---------- Metrics summary ----------
        if filtered_metrics:
            cpu_vals = [
                _safe_float(_safe_get(m, "cpu_percent", None), default=None)
                for m in filtered_metrics
            ]
            ram_vals = [
                _safe_float(_safe_get(m, "system_ram_percent", None), default=None)
                for m in filtered_metrics
            ]
            cpu_vals = [v for v in cpu_vals if v is not None]
            ram_vals = [v for v in ram_vals if v is not None]

            def _avg(seq):
                return (sum(seq) / len(seq)) if seq else 0.0

            report["summary"]["system_metrics_summary"] = {
                "avg_cpu_percent": round(_avg(cpu_vals), 3),
                "max_cpu_percent": round(max(cpu_vals), 3) if cpu_vals else 0.0,
                "avg_ram_percent": round(_avg(ram_vals), 3),
                "max_ram_percent": round(max(ram_vals), 3) if ram_vals else 0.0,
                "samples": len(filtered_metrics),
            }

        # Totaux pour lecture rapide (utile monitoring)
        report["summary"]["totals"] = {
            "profiling_events": len(filtered_profiling),
            "error_events": len(filtered_errors),
            "metrics_samples": len(filtered_metrics),
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
        === [IA SUPPRIMÉE - Session 23 Nov 2025] ===
        Analyse AIInterface retirée (module supprimé)
        """
        return {"error": "AIInterface supprimée - fonctionnalité désactivée"}

    def export_report(self, report: dict, format: str = "json") -> None:
        """
        Exporte le rapport au format choisi dans self.reports_dir, de manière atomique.
        - format: "json" (défaut). Stubs sûrs pour "md" et "txt".
        - crée le répertoire cible si nécessaire.
        - écriture atomique (temp + replace) pour éviter les fichiers corrompus.
        """
      
        ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")

        # Assure le répertoire de sortie
        try:
            out_dir: Path = getattr(self, "reports_dir", None) or Path("config/ai_audit")
            out_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            out_dir = Path("config/ai_audit")
            out_dir.mkdir(parents=True, exist_ok=True)

        # Nom de fichier (préfixe configurable)
        try:
            prefix = getattr(self, "weekly_report_file_prefix", "weekly_report_") or "weekly_report_"
        except Exception:
            prefix = "weekly_report_"

        # Sanitize basique du format
        fmt = (format or "json").strip().lower()
        if fmt not in {"json", "md", "txt"}:
            try:
                self.logger.warning(f"Format non supporté '{format}', fallback JSON.")
            except Exception:
                pass
            fmt = "json"

        filename = f"{prefix}{ts}.{fmt}"
        filepath = out_dir / filename

        # Écriture atomique dans le même dossier
        tmp_path = None
        try:
            with NamedTemporaryFile("w", delete=False, dir=str(out_dir), encoding="utf-8") as tmp:
                tmp_path = Path(tmp.name)
                if fmt == "json":
                    # Encoder custom si dispo (fallback JSON std)
                    try:
                        CustomJSONEncoder = getattr(self.config_manager, "CustomJSONEncoder", None)
                    except Exception:
                        CustomJSONEncoder = None
                    json.dump(
                        report if isinstance(report, dict) else {"payload": report},
                        tmp,
                        indent=2,
                        ensure_ascii=False,
                        cls=CustomJSONEncoder if CustomJSONEncoder else None,
                    )
                elif fmt == "md":
                    # Rendu markdown simple
                    payload = report if isinstance(report, dict) else {"payload": str(report)}
                    tmp.write(f"# Mecano Weekly Report\n\nGenerated at: {ts} UTC\n\n")
                    tmp.write("## Summary\n\n")
                    for k, v in (payload.get("summary") or {}).items():
                        tmp.write(f"- **{k}**: {v}\n")
                    tmp.write("\n## Details (JSON)\n\n```json\n")
                    tmp.write(json.dumps(payload.get("details") or payload, ensure_ascii=False, indent=2))
                    tmp.write("\n```\n")
                else:  # txt
                    tmp.write(json.dumps(report if isinstance(report, dict) else {"payload": report}, ensure_ascii=False, indent=2))

                tmp.flush()
                os.fsync(tmp.fileno())

            # Remplacement atomique
            tmp_path.replace(filepath)
            try:
                self.logger.info(f"Rapport exporté: '{filepath}'")
            except Exception:
                pass

        except Exception as e:
            # Logging d'exception robuste
            if hasattr(self, "log_exception"):
                try:
                    self.log_exception("export_report", e)
                except Exception:
                    pass
            else:
                try:
                    self.logger.error(f"export_report: échec export '{filepath}': {e}", exc_info=True)
                except Exception:
                    pass
        finally:
            # Nettoyage temp si nécessaire (si le replace n'a pas eu lieu)
            try:
                if tmp_path and tmp_path.exists():
                    tmp_path.unlink(missing_ok=True)
            except Exception:
                pass



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