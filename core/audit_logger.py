# core/audit_logger.py

import logging
import json
import sys
import yaml
import os
import shutil
import pandas as pd
from pathlib import Path
from datetime import datetime, UTC, timedelta
from typing import Dict, Any, List, Optional, Tuple

# Importation de CustomJSONEncoder depuis core.utils
from core.utils import CustomJSONEncoder

logger = logging.getLogger(__name__)

class AuditLogger:
    """
    Gère l'historique des configurations, les journaux d'audit et la rotation des sauvegardes.
    Cette classe est responsable de la journalisation des changements de configuration,
    de la gestion des journaux d'audit et de l'exécution de la rotation des fichiers
    pour maintenir un historique propre et performant.
    """

    def __init__(self, config_manager_instance=None):
        """
        Initialise l'AuditLogger.

        Args:
            config_manager_instance: L'instance du ConfigManager pour accéder aux paramètres
                                     de chemins, de rotation et pour envoyer des alertes.
        """
        self.config_manager = config_manager_instance
        self.logger = logging.getLogger(__name__)

        self._config_history_list: List[Dict] = []
        self._audit_trail: List[Dict] = [] # Ce sera le journal des décisions (log_decision)

        # Les chemins doivent être récupérés via config_manager
        # Fallback pour les tests unitaires si config_manager_instance est None
        self.logs_dir = Path(self.config_manager.get("paths.logs", "logs/")) if self.config_manager else Path("logs/")
        self.reports_dir = Path(self.config_manager.get("paths.reports", "output/")) if self.config_manager else Path("output/")

        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.reports_dir.mkdir(parents=True, exist_ok=True)

        # Initialisation du logger propre à AuditLogger
        self._setup_logger()

        # Attributs pour la rotation des logs (qui seront dynamiques via config_manager.get)
        self.audit_log_rotation_settings = self.config_manager.get("app.audit_log_rotation", {}) if self.config_manager else {"enabled": False}
        self.max_config_backups = self.config_manager.get("app.max_config_backups", 20) if self.config_manager else 20
        self.backup_dir = Path(self.config_manager.get("paths.backup_dir", self.reports_dir / "backups")) if self.config_manager else self.reports_dir / "backups"
        self.backup_dir.mkdir(parents=True, exist_ok=True)


        self.logger.info("AuditLogger initialisé.")

    def _setup_logger(self) -> None:
        """
        Configure le logger spécifique à AuditLogger pour écrire dans un fichier dédié.
        """
        audit_logger = logging.getLogger(__name__) # Récupérer le logger de ce module
        
        # Supprimer les handlers existants pour éviter les duplications
        if audit_logger.hasHandlers():
            audit_logger.handlers.clear()

        # Chemin complet du fichier de log (peut être configuré si nécessaire)
        log_file_path = self.logs_dir / "audit_logger.log"

        # File handler
        file_handler = logging.FileHandler(log_file_path, mode="a", encoding="utf-8")
        file_formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        file_handler.setFormatter(file_formatter)
        audit_logger.addHandler(file_handler)

        # Console handler (pour le développement/débogage)
        console_handler = logging.StreamHandler(sys.stdout)
        console_formatter = logging.Formatter("%(asctime)s - [AuditLogger] - %(levelname)s - %(message)s")
        console_handler.setFormatter(console_formatter)
        audit_logger.addHandler(console_handler)

        audit_logger.setLevel(logging.INFO) # Niveau par défaut pour AuditLogger
        audit_logger.propagate = False # Désactiver la propagation pour éviter le double logging

        self.logger = audit_logger
        self.logger.info(f"Logger AuditLogger configuré pour écrire dans: {log_file_path}")


    def log_config_change(self, change_info: Dict[str, Any], source: str, dynamic_config_snapshot: Dict[str, Any]) -> None:
        """
        Enregistre un changement de configuration dans une liste pour l'historique et le journal d'audit.

        Args:
            change_info (Dict): Dictionnaire décrivant le changement.
            source (str): Entité ayant initié le changement ('manual', 'bot_ai', etc.).
            dynamic_config_snapshot (Dict[str, Any]): Le snapshot complet de la configuration dynamique au moment du changement.
        """
        timestamp = datetime.now(UTC)
        entry = {
            "timestamp": timestamp.isoformat(), # Convertir en string ISO pour la sérialisation future
            "source": source,
            "change_info": change_info,
            "full_config_snapshot": dynamic_config_snapshot,
        }

        self._config_history_list.append(entry)

        # Enregistrer également dans le journal d'audit général si une décision de ConfigManager y est loggée.
        # Pour l'instant, on l'ajoute à _audit_trail qui est le journal de toutes les "décisions"
        # de ConfigManager (incluant les changements de config).
        # Cette logique sera consolidée quand on déplacera log_decision du ConfigManager.
        self._audit_trail.append(entry) # Ajoute la même entrée au journal des décisions

        self.logger.info(
            f"Changement de configuration enregistré. Source='{source}', Action='{change_info.get('action', 'unknown')}'"
        )
        # TODO: Implémenter l'écriture asynchrone pour ne pas bloquer le thread principal.

    def get_config_history(self, filter_by: Optional[Dict[str, Any]] = None) -> pd.DataFrame:
        """
        Retourne l'historique des changements de configuration appliqués sous forme de DataFrame.

        Permet de filtrer l'historique par source ou par période.

        Args:
            filter_by (Dict, optional): Un dictionnaire de filtres.
                                        Ex: {'source': 'manual', 'timestamp_after': '2023-01-01'}.

        Returns:
            pd.DataFrame: Un DataFrame contenant l'historique des configurations.
        """
        self.logger.info(f"Récupération de l'historique de configuration avec le filtre : {filter_by}")

        history_df = pd.DataFrame(self._config_history_list) # Construire le DataFrame à la demande

        if history_df.empty:
            return history_df

        # Assurez-vous que la colonne timestamp est bien au format datetime pour le filtrage
        if not pd.api.types.is_datetime64_any_dtype(history_df["timestamp"]):
            history_df["timestamp"] = pd.to_datetime(history_df["timestamp"], utc=True)

        if filter_by:
            if "source" in filter_by:
                history_df = history_df[history_df["source"] == filter_by["source"]]
            if "timestamp_after" in filter_by:
                history_df = history_df[history_df["timestamp"] >= pd.to_datetime(filter_by["timestamp_after"], utc=True)]
            if "timestamp_before" in filter_by:
                history_df = history_df[history_df["timestamp"] <= pd.to_datetime(filter_by["timestamp_before"], utc=True)]

        self.logger.info(f"{len(history_df)} entrées retournées de l'historique de configuration.")
        return history_df

    def _rotate_backups(self, backup_dir: Path, max_backups: int) -> None:
        """
        Gère la rotation des backups pour éviter l'accumulation de fichiers.

        Args:
            backup_dir (Path): Le répertoire contenant les backups.
            max_backups (int): Le nombre maximum de backups à conserver.
        """
        if max_backups <= 0:
            self.logger.info(f"La rotation des backups est désactivée car 'max_config_backups' est défini à {max_backups} ou moins. Aucun backup ne sera conservé.")
            return

        try:
            # Filtrer par l'extension .bak et trier par date de modification (les plus anciens d'abord)
            backups = sorted(backup_dir.glob("*.bak"), key=os.path.getmtime)
            
            if len(backups) > max_backups:
                num_to_delete = len(backups) - max_backups
                for old_backup in backups[:num_to_delete]:
                    old_backup.unlink() # Supprimer le fichier
                    self.logger.info(f"Ancien backup supprimé : {old_backup.name}")
        except Exception as e:
            self.logger.error(f"Erreur lors de la rotation des backups dans {backup_dir}: {e}", exc_info=True)
            if self.config_manager: # Tenter d'envoyer une alerte si config_manager est dispo
                self.config_manager.send_alert(
                    "CRITIQUE",
                    f"Échec rotation backups: {e}",
                    alert_type="telegram_critical",
                )

    def save_dynamic_config(self, config: Dict[str, Any], config_path: str, backup: bool = True) -> None:
        """
        Sauvegarde la configuration de manière sécurisée et atomique.
        Implémente un système de verrouillage, de backup et de rotation.
        Déplacée de ConfigManager.

        Args:
            config (Dict[str, Any]): Le dictionnaire de configuration à sauvegarder.
            config_path (str): Le chemin de sauvegarde.
            backup (bool): Si True, crée un backup avant de sauvegarder.
        """
        config_path_obj = Path(config_path)
        lock_path = config_path_obj.with_suffix(config_path_obj.suffix + ".lock")

        # Récupérer les paramètres de backup via config_manager
        # Fallback pour les tests unitaires si config_manager est None
        backup_dir = self.backup_dir
        max_config_backups = self.max_config_backups
        file_permissions_octal = self.config_manager.get("app.file_permissions_octal", "0o644") if self.config_manager else "0o644"


        try:
            # 1. Verrouillage de fichier pour la sécurité en cas de concurrence
            # Utilise os.open avec O_EXCL pour une création exclusive et atomique du lock
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd) # Ferme le descripteur, le fichier lock existe maintenant

        except FileExistsError:
            self.logger.warning(f"Le fichier de configuration est déjà en cours d'écriture (lock trouvé à {lock_path}). Opération annulée.")
            raise InterruptedError("Sauvegarde de la configuration annulée, verrou détecté.")
        except Exception as e:
            self.logger.error(f"Erreur inattendue lors de la création du fichier de verrouillage: {e}", exc_info=True)
            if self.config_manager:
                self.config_manager.send_alert("CRITIQUE", f"Erreur création lock config: {e}", alert_type="telegram_critical")
            raise

        original_file_exists = config_path_obj.exists()
        backup_created = False
        temp_path = None # Initialiser pour le bloc finally

        try:
            # 2. Logique de backup et de rotation
            backup_path = None
            if backup and original_file_exists:
                backup_dir.mkdir(parents=True, exist_ok=True)
                timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S") # Utiliser UTC
                backup_path = backup_dir / f"{config_path_obj.name}.{timestamp}.bak"
                
                # Utiliser shutil.copy2 pour préserver les métadonnées (permissions, timestamps)
                # puis unlink l'original après une copie réussie, et rename le backup
                shutil.copy2(config_path_obj, backup_path)
                # Une fois la copie de backup faite, on peut supprimer l'original en toute sécurité
                config_path_obj.unlink()
                self.logger.info(f"Backup créé à : {backup_path}")
                backup_created = True

                # Rotation des backups après la création du nouveau backup
                self._rotate_backups(backup_dir, max_config_backups)

            # 3. Écriture atomique (écrire dans un fichier temporaire puis renommer)
            temp_path = config_path_obj.with_suffix(config_path_obj.suffix + ".tmp")
            file_extension = config_path_obj.suffix.lower()

            with open(temp_path, "w", encoding="utf-8") as f:
                if file_extension == ".json":
                    json.dump(config, f, indent=4, cls=CustomJSONEncoder)
                elif file_extension in [".yaml", ".yml"]:
                    yaml.safe_dump(config, f, indent=4)
                # Note: La méthode _write_set_file n'est pas dans AuditLogger.
                # Si elle est nécessaire, elle devra être passée comme dépendance ou extraite dans core/config_loader.
                # Pour l'instant, on suppose que save_dynamic_config est principalement pour JSON/YAML.
                elif file_extension == ".set":
                    # Temporairement, pour éviter une dépendance directe sur un ConfigLoader non encore refactorisé
                    raise NotImplementedError("La sauvegarde au format .set n'est pas encore implémentée dans AuditLogger.")
                else:
                    raise ValueError(f"Type de fichier non supporté pour la sauvegarde: {file_extension}")

            # Remplacer le fichier original par le fichier temporaire (atomique)
            temp_path.replace(config_path_obj)
            self.logger.info(f"Configuration sauvegardée avec succès à {config_path_obj}.")

            # Gérer les permissions de fichiers
            try:
                os.chmod(config_path_obj, int(file_permissions_octal, 8))
                self.logger.debug(f"Permissions du fichier '{config_path_obj}' définies à {file_permissions_octal}.")
            except Exception as e:
                self.logger.warning(f"Impossible de définir les permissions du fichier '{config_path_obj}' à {file_permissions_octal}: {e}")

        except Exception as e:
            self.logger.error(f"Erreur lors de la sauvegarde de la configuration vers {config_path_obj}: {e}", exc_info=True)
            # Tentative de restauration de l'original si la sauvegarde a échoué après suppression
            if backup_created and backup_path and backup_path.exists():
                try:
                    shutil.copy2(backup_path, config_path_obj) # Restaurer l'original à partir du backup
                    self.logger.warning(f"Tentative de restauration de la configuration depuis le backup {backup_path}.")
                except Exception as restore_e:
                    self.logger.critical(f"ÉCHEC CRITIQUE: Impossible de restaurer le fichier de configuration original après une erreur de sauvegarde. Le fichier peut être corrompu ou manquant. Erreur: {restore_e}", exc_info=True)
                    if self.config_manager:
                        self.config_manager.send_alert("FATAL", f"ÉCHEC RESTAURATION CONFIG: {restore_e}", alert_type="telegram_critical")
            
            # Nettoyer le fichier temporaire en cas d'erreur
            if temp_path and temp_path.exists():
                temp_path.unlink()

            if self.config_manager:
                self.config_manager.send_alert("CRITIQUE", f"Échec sauvegarde config: {e}", alert_type="telegram_critical")
            raise # Re-lancer l'exception pour que l'appelant puisse la gérer
        finally:
            # S'assurer que le fichier de verrouillage est toujours supprimé
            if lock_path.exists():
                lock_path.unlink()


    def export_audit_trail(self, path: str) -> None:
        """
        Exporte le journal d'audit complet de manière atomique vers un fichier.
        Implémente la rotation des journaux pour éviter les fichiers excessivement volumineux.
        Déplacée de ConfigManager.

        Args:
            path (str): Le chemin complet du fichier de destination.
        """
        self.logger.info(f"Export du journal d'audit vers {path}")
        if not hasattr(self, "_audit_trail") or not self._audit_trail:
            self.logger.warning("Aucune donnée dans le journal d'audit à exporter. Opération annulée.")
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
                    # pandas.json_normalize peut gérer les structures imbriquées
                    df = pd.json_normalize(self._audit_trail)
                    df.to_csv(f, index=False, float_format="%.5f")
                else:
                    raise ValueError(f"Format d'export non supporté pour le journal d'audit : {file_extension}")

            temp_path.rename(output_path)
            self.logger.info(f"Journal d'audit exporté avec succès vers {output_path}.")

            # Rotation des journaux après l'export
            if self.audit_log_rotation_settings.get("enabled", False):
                self._rotate_audit_logs(output_path.parent, self.audit_log_rotation_settings)

        except Exception as e:
            self.logger.error(f"Erreur lors de l'export du journal d'audit vers {path}: {e}", exc_info=True)
            if temp_path.exists():
                temp_path.unlink()
            if self.config_manager:
                self.config_manager.send_alert("CRITIQUE", f"Échec de l'export du journal d'audit: {e}", "telegram_critical")


    def _rotate_audit_logs(self, log_dir: Path, rotation_settings: Dict[str, Any]) -> None:
        """
        Gère la rotation des fichiers de journal d'audit par jour ou par taille.
        Déplacée de ConfigManager.
        """
        rotation_type = rotation_settings.get("type", "daily")
        max_files = rotation_settings.get("max_files", 30)
        max_size_mb = rotation_settings.get("max_size_mb", 100)

        self.logger.info(f"Démarrage de la rotation des logs d'audit dans {log_dir} (Type: {rotation_type}).")

        # Le nom du fichier est audit_trail_file_name dans trade_executor_settings
        # Pour une meilleure généralisation, on peut utiliser un préfixe commun pour les logs d'audit.
        # Ici, on assume un préfixe commun ou on se base sur le nom du fichier configuré.
        # Récupérer le nom du fichier d'audit trail depuis la config (ex: "trade_audit_trail.log")
        audit_file_name_pattern_from_config = self.config_manager.get("trade_executor_settings.audit_trail_file_name", "trade_audit_trail.log").split(".")[0]
        audit_file_name_pattern = audit_file_name_pattern_from_config + "*"

        audit_files = sorted(log_dir.glob(f"{audit_file_name_pattern}*"), key=os.path.getmtime)

        if rotation_type == "daily":
            if len(audit_files) > max_files:
                num_to_delete = len(audit_files) - max_files
                for old_log_file in audit_files[:num_to_delete]:
                    old_log_file.unlink()
                    self.logger.info(f"Ancien log d'audit (quotidien) supprimé : {old_log_file.name}")

        elif rotation_type == "size":
            for log_file in audit_files:
                if log_file.stat().st_size > max_size_mb * 1024 * 1024:
                    self.logger.warning(f"Log d'audit '{log_file.name}' dépasse la taille max ({max_size_mb}MB). Renommage pour rotation...")
                    # Ajouter un timestamp au nom du fichier pivoté pour éviter les conflits
                    rotated_name = log_file.with_suffix(f".{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}{log_file.suffix}.rotated")
                    log_file.rename(rotated_name)
                    self.logger.info(f"Log d'audit '{log_file.name}' pivoté vers '{rotated_name.name}'.")
                    # Après avoir pivoté un fichier par taille, le système devra écrire un nouveau log propre.

        self.logger.info(f"Rotation des logs d'audit terminée. Nombre de fichiers restants : {len(list(log_dir.glob(f'{audit_file_name_pattern}*')))}")