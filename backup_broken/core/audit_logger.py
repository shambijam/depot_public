# core/audit_logger.py

import logging
import json
import sys
import yaml
import os
import shutil
import time
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
        # PATCH G1.2: registres internes
        self._named_loggers: Dict[str, logging.Logger] = {}
        self._dedup_cache: Dict[str, float] = {}  # key -> last_ts (monotonic)


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
            
    def log_trade_execution(self, order_info: Dict[str, Any], context: Dict[str, Any]) -> None:
        """
        Journalise une exécution (ou tentative d'exécution) de trade au format audit (JSONL).
        Conçu pour Katana : trace spread_pips, RR projeté, ATR M1, confluences (OB/FVG/MTF/BOS),
        et les paramètres clés de l'ordre (symbol, action, volume, SL/TP, entry).
        ➕ Enrichi Bollinger : signal, scores, états (squeeze/expansion), distances, preset SL/TP pressenti.

        Args:
            order_info: Détails de l'ordre au moment de l'envoi/réponse broker.
                Champs typiques acceptés (tous optionnels, robustesse aux manquements) :
                - "order_id", "ticket", "request", "response", "status", "error_code"
                - "symbol", "action", "volume", "entry_price", "sl_price", "tp_price"
                - "rr_projected", "spread_pips", "order_type", "magic_number"
                - "decision_id", "strategy_type", "rule_name"
                - (optionnel) "target_sl_pips", "target_tp_pips", "override_source"
            context: Instantané décisionnel/marché utilisé (signaux/ATR/etc.).
                Clés utiles si disponibles :
                - "signals" (dict par actif) ou "signals_snapshot"
                - "katana_snapshot" (dict : katana_ready, katana_score, mtf_hits, etc.)
                - "market_metrics" (ex: {"atr_m1_pips": 0.7})
                - "account_info", "env"
        """
        from datetime import datetime, UTC
        import json

        try:
            # ✅ FIX: Extraction depuis request/result si payload structuré
            req = order_info.get("request") or {}
            res = order_info.get("result") or {}

            # Champs de base (ordre) - cherche dans request, result, puis racine
            symbol = (
                req.get("symbol") or req.get("asset") or
                order_info.get("symbol") or order_info.get("asset")
            )
            action = (
                req.get("action") or req.get("type") or
                order_info.get("action")
            )
            volume = (
                res.get("volume") or req.get("volume") or
                order_info.get("volume")
            )
            entry = (
                res.get("price") or req.get("price") or
                order_info.get("entry_price")
            )
            sl = (
                req.get("sl") or
                order_info.get("sl_price") or order_info.get("sl")
            )
            tp = (
                req.get("tp") or
                order_info.get("tp_price") or order_info.get("tp")
            )
            rr = order_info.get("rr_projected")
            spread = order_info.get("spread_pips")
            order_type = req.get("type") or order_info.get("order_type")
            ticket = (
                res.get("order") or res.get("deal") or res.get("ticket") or
                order_info.get("ticket") or order_info.get("order_id")
            )
            status = (
                res.get("status") or
                order_info.get("status")
            )
            error_code = order_info.get("error_code")

            # Contextes (signaux / katana / marché)
            kat = context.get("katana_snapshot") or {}
            sig_all = context.get("signals_snapshot") or context.get("signals") or {}
            # Certains appelleurs stockent les signaux par symbole :
            sig = (sig_all.get(symbol) if isinstance(sig_all, dict) and symbol in sig_all else sig_all) or {}
            mkt = context.get("market_metrics") or {}
            acct = context.get("account_info") or {}

            # Confluences Katana (robustes aux clés manquantes)
            confluences = {
                "mtf_hits":      kat.get("mtf_hits") or kat.get("signal_agreement", {}).get("total_agree") or 0,
                "m1_break_ok":   bool(kat.get("m1_break_ok", False)),
                "htf_alignment": bool(kat.get("htf_alignment_ok", False)),
                "ob_detected":   bool(kat.get("ob_detected", False) or sig.get("ob_detected", False)),
                "fvg_detected":  bool(kat.get("fvg_detected", False) or sig.get("fvg_detected", False)),
            }

            # Indicateurs micro-phase
            atr_m1_pips = (
                kat.get("atr_m1_pips")
                or mkt.get("atr_m1_pips")
                or sig.get("atr_m1_pips")
            )

            # ====== Enrichissement Bollinger pour audit ======
            # On lit d'abord dans sig (snapshot dernier bar), fallback katana_snapshot le cas échéant.
            boll_signal = sig.get("boll_signal") or kat.get("boll_signal")
            boll_break  = sig.get("boll_breakout_score", kat.get("boll_breakout_score"))
            boll_revert = sig.get("boll_mean_revert_score", kat.get("boll_mean_revert_score"))
            boll = {
                "signal": boll_signal,
                "breakout_score": boll_break,
                "mean_revert_score": boll_revert,
                "is_squeeze": bool(sig.get("boll_is_squeeze", kat.get("boll_is_squeeze", False))),
                "is_expansion": bool(sig.get("boll_is_expansion", kat.get("boll_is_expansion", False))),
                "squeeze_strength": sig.get("boll_squeeze_strength", sig.get("squeeze_strength", kat.get("squeeze_strength"))),
                "band_touch": sig.get("boll_band_touch", kat.get("boll_band_touch")),
                "in_band": sig.get("boll_in_band", kat.get("boll_in_band")),
                "z_band": sig.get("boll_z_band", kat.get("boll_z_band")),
                "dist_to_upper_pips": sig.get("boll_dist_to_upper_pips"),
                "dist_to_lower_pips": sig.get("boll_dist_to_lower_pips"),
                "dist_to_mid_pips":   sig.get("boll_dist_to_mid_pips"),
                "bb_upper": sig.get("boll_bb_upper"),
                "bb_lower": sig.get("boll_bb_lower"),
                "bb_mid":   sig.get("boll_bb_mid"),
            }

            # Deviner le preset SL/TP pressenti selon le signal (à des fins d'audit uniquement)
            preset_guess = None
            if isinstance(boll_signal, str):
                s = boll_signal.lower()
                if s == "buy_breakout":   preset_guess = "on_buy_breakout"
                elif s == "sell_breakout": preset_guess = "on_sell_breakout"
                elif s == "buy_revert":    preset_guess = "on_buy_revert"
                elif s == "sell_revert":   preset_guess = "on_sell_revert"

            # Overrides SL/TP éventuellement passés au moteur (ex: depuis _calculate_sl_tp_prices)
            overrides_meta = {
                "target_sl_pips": order_info.get("target_sl_pips"),
                "target_tp_pips": order_info.get("target_tp_pips"),
                "override_source": order_info.get("override_source"),  # ex: "bollinger_preset" si l'appelant le renseigne
                "preset_key_guess": preset_guess,
            }

            entry_meta = {
                "katana_ready": bool(kat.get("katana_ready", False)),
                "katana_score": kat.get("katana_score"),
                "phase":        kat.get("phase") or sig.get("phase"),
                "dominant_tf":  kat.get("dominant_tf") or sig.get("dominant_tf"),
                "atr_m1_pips":  atr_m1_pips,
            }

            # Payload audit
            audit_entry = {
                "timestamp": datetime.now(UTC).isoformat(),
                "category": "trade_execution",
                "symbol": symbol,
                "action": action,
                "order_type": order_type,
                "volume": volume,
                "entry_price": entry,
                "sl_price": sl,
                "tp_price": tp,
                "rr_projected": rr,
                "spread_pips": spread,
                "ticket": ticket,
                "status": status,
                "error_code": error_code,
                "strategy_type": order_info.get("strategy_type"),
                "rule_name": order_info.get("rule_name"),
                "magic_number": order_info.get("magic_number"),
                "decision_id": order_info.get("decision_id"),
                "katana": {
                    **entry_meta,
                    **confluences,
                    "bollinger": boll,
                    "sl_tp_overrides": overrides_meta,
                },
                "account": {
                    "equity": acct.get("equity"),
                    "balance": acct.get("balance"),
                    "margin_free": acct.get("margin_free"),
                },
            }

            # Ajout en mémoire (journal interne)
            if not hasattr(self, "_audit_trail"):
                self._audit_trail = []
            self._audit_trail.append(audit_entry)

            # Écriture JSONL immédiate si configurée
            file_name = "trade_audit_trail.log"
            try:
                file_name = self.config_manager.get("trade_executor_settings.audit_trail_file_name", file_name)
            except Exception:
                pass

            out_path = self.logs_dir / file_name
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with open(out_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(audit_entry, cls=CustomJSONEncoder) + "\n")

            self.logger.info(
                f"Audit trade consigné: {symbol} {action} vol={volume} rr={rr} spread={spread} "
                f"katana(ready={entry_meta['katana_ready']}, score={entry_meta['katana_score']}, boll_sig={boll_signal})"
            )
        except Exception as e:
            self.logger.error(f"Échec log_trade_execution: {e}", exc_info=True)



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

    def queue_or_send_alert(self, message: str, alert_type: str) -> None:
        """
        Met en file d'attente ou envoie immédiatement une alerte, selon le type.
        Cette méthode centralise la logique d'envoi d'alertes vers des services externes (ex: Telegram).

        Args:
            message (str): Le message de l'alerte.
            alert_type (str): Le type de l'alerte (ex: 'telegram_critical', 'telegram_market_phase').
        """
        self.logger.debug(f"AuditLogger reçu alerte de type '{alert_type}': {message[:100]}...")

        if not self.config_manager:
            self.logger.warning("AuditLogger ne peut pas envoyer d'alertes : ConfigManager n'est pas lié.")
            return

        telegram_config = self.config_manager.get("telegram", {}) # Récupère la config Telegram complète
        telegram_enabled = telegram_config.get("enabled", False)
        
        if not telegram_enabled:
            self.logger.debug(f"Alerte de type '{alert_type}' ignorée : Telegram est désactivé dans la configuration.")
            return

        channels = telegram_config.get("channels", {})
        target_channel_id = channels.get(alert_type) # Ex: "telegram_critical" -> "YOUR_CRITICAL_CHAT_ID"

        if not target_channel_id:
            self.logger.warning(f"Aucun ID de canal Telegram configuré pour le type d'alerte '{alert_type}'. Alerte non envoyée.")
            return

        # Ici, la logique réelle d'envoi à Telegram doit être implémentée.
        # Pour l'instant, c'est un placeholder.
        # Vous devrez connecter ceci à un module d'envoi Telegram réel.
        try:
            # Exemple de placeholder pour l'envoi (vous devrez remplacer ceci par l'intégration Telegram réelle)
            # if self.telegram_sender_instance: # Si vous aviez une instance d'un client Telegram ici
            #    self.telegram_sender_instance.send_message(target_channel_id, message, parse_mode=telegram_config.get("parse_mode", "Markdown"))
            self.logger.info(f"Alerte envoyée (simulée) vers Telegram Channel ID '{target_channel_id}' pour type '{alert_type}'. Message: {message[:100]}...")
            # Si c'est une alerte immédiate, ne pas la mettre en file
            if alert_type in telegram_config.get("immediate_alert_types", []):
                # Envoyer immédiatement
                pass # L'envoi réel irait ici
            else:
                # Mettre en file pour un résumé périodique (si implémenté)
                pass # La mise en file irait ici

        except Exception as e:
            self.logger.error(f"Échec de l'envoi de l'alerte Telegram pour le type '{alert_type}': {e}", exc_info=True)


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
                self.config_manager.send_alert( # L'appel send_alert est maintenant géré par la nouvelle méthode dans ConfigManager
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
        
         # === PATCH G1.3: Fabrique de loggers dédiés (pas de nouveau module) ===
    def get_named_logger(self, name: str, relative_path: str, level: str = "INFO") -> logging.Logger:
        """
        Retourne (et crée si nécessaire) un logger dédié écrivant dans logs/<relative_path>.
        Exemple: get_named_logger("SCALPING", "scalping/scalping_pipeline.log")
        """
        if name in self._named_loggers:
            return self._named_loggers[name]

        lg = logging.getLogger(f"AuditLogger.{name}")
        # éviter les handlers dupliqués
        if lg.hasHandlers():
            lg.handlers.clear()

        target_path = self.logs_dir / relative_path
        target_path.parent.mkdir(parents=True, exist_ok=True)

        fh = logging.FileHandler(target_path, mode="a", encoding="utf-8")
        fmt = logging.Formatter("%(asctime)s - %(levelname)s - [" + name + "] %(message)s")
        fh.setFormatter(fmt)
        lg.addHandler(fh)

        lg.setLevel(getattr(logging, level.upper(), logging.INFO))
        lg.propagate = False

        self._named_loggers[name] = lg
        self.logger.info(f"[NamedLogger] '{name}' prêt → {target_path}")
        return lg

    def allow_once(self, key: str, ttl_s: int = 10) -> bool:
        """
        Anti-spam simple: retourne True si on doit loguer maintenant, False si un log identique
        a déjà eu lieu dans les ttl_s dernières secondes.
        """
        now = time.monotonic()
        last = self._dedup_cache.get(key, 0.0)
        if now - last >= ttl_s:
            self._dedup_cache[key] = now
            return True
        return False
   