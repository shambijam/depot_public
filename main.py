#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
main.py - Cœur de l'Orchestration et du Lancement du Bot SNIPER_X

Ce fichier initialise les modules clés, gère la configuration globale,
et lance la boucle de trading. Il est destiné à être appelé par cli.py
pour un contrôle structuré et auditable.
"""

import argparse
import logging
import json
import sys
import time
from datetime import datetime
from pathlib import Path
import importlib
import core.strategy_manager

# Charger les variables d'environnement dès le début pour les chemins critiques/secrets
from dotenv import load_dotenv

load_dotenv()

try:
    from phase_observer.orchestrator import PhaseObserver
    from core.config_manager import ConfigManager

    importlib.reload(core.strategy_manager)
    from trader.trade_executor import TradeExecutor
    from ai_core.ai_decision import AIDecision
    from mt5_connector import MT5Connector
    from mecanique_generale.mecano import Mecano
    from run_bot import (
        run_single_pipeline_cycle,
    )  # ← on garde uniquement la fonction de run
    from utils.logger_setup import (
        setup_production_logging,
    )  # ← source unique pour le logging
    from core.audit_logger import AuditLogger
    from core.strategy_manager import StrategyManager
    from core.ai_interface import AIInterface
    from core.decision_pipeline import DecisionPipeline
    

    from run_bot import (
        _mtf_readiness_gate,
        verify_environment_and_config,
        run_single_pipeline_cycle,
    )

    import MetaTrader5 as mt5


except ImportError as e:
    logging.critical(
        f"FATAL ERROR: Failed to import a core SNIPER_X module. Error: {e}",
        exc_info=True,
    )
    sys.exit(1)


# === Helper: déclenchement des rapports au démarrage (IA quotidien & Mecano hebdo) ===


def _trigger_ai_and_mecano_reports_on_start(ai_decision, mecano, config_manager):
    """
    Déclenche au DÉMARRAGE :
      - Rapport IA quotidien (si pas encore fait aujourd'hui ET si activé dans la conf)
      - Rapport Mecano hebdo le dimanche (si pas encore fait aujourd'hui ET si activé dans la conf)
    Persiste l'état dans <ai_audit>/.last_runs.json pour éviter les doublons.
    ⚠️ Ne dépend ni de MT5 ni du pipeline : sûr à appeler juste après les instanciations.
    """

    # ---- Résolution dossier ai_audit ----
    try:
        base_cfg = config_manager.get("paths.configs", "config")
    except Exception:
        base_cfg = "config"
    try:
        ai_audit_dir_cfg = config_manager.get("paths.ai_audit", None)
    except Exception:
        ai_audit_dir_cfg = None
    ai_audit_dir = Path(ai_audit_dir_cfg or (Path(base_cfg) / "ai_audit"))
    ai_audit_dir.mkdir(parents=True, exist_ok=True)

    state_path = ai_audit_dir / ".last_runs.json"

    # ---- Load state (safe) ----
    state = {"last_daily_date": None, "last_weekly_date": None}
    try:
        if state_path.exists():
            loaded = json.loads(state_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                state.update(loaded)
    except Exception:
        pass

    # ---- Flags de configuration (interrupteurs) ----
    # IA (daily) : on respecte ai.audit_mode.enabled et ai.audit_mode.daily_report_enabled
    ai_global_enabled = bool(config_manager.get("ai.audit_mode.enabled", True))
    ai_daily_enabled = bool(
        config_manager.get("ai.audit_mode.daily_report_enabled", True)
    )
    # Mecano (weekly) : compat deux chemins possibles
    mecano_weekly_enabled = bool(
        config_manager.get(
            "mecano.weekly_report_enabled",
            config_manager.get("mecano.audit_mode.weekly_report_enabled", True),
        )
    )

    # ---- Date/weekday (locale machine) ----
    now_local = datetime.now()
    today_str = now_local.strftime("%Y-%m-%d")
    weekday = now_local.weekday()  # Monday=0 ... Sunday=6

    # ---- Helper: collecte de logs IA du jour (best-effort) ----
    def _collect_daily_logs():
        """
        Essaie de charger les logs IA pertinents (ai_supervisor_logs.jsonl) du jour.
        Si indisponible, renvoie une liste vide.
        """
        import json
        from datetime import datetime

        try:
            logs_dir = Path(config_manager.get("paths.logs", "logs"))
        except Exception:
            logs_dir = Path("logs")
        src = logs_dir / "ai_supervisor_logs.jsonl"
        if not src.exists():
            return []
        out = []
        cutoff = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        try:
            with src.open("r", encoding="utf-8") as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                        ts = rec.get("timestamp")
                        if not ts:
                            out.append(rec)
                            continue
                        ts_norm = str(ts).replace("Z", "+00:00")
                        try:
                            dt = datetime.fromisoformat(ts_norm)
                        except Exception:
                            dt = None
                        if dt and dt.date() == cutoff.date():
                            out.append(rec)
                    except Exception:
                        continue
        except Exception:
            return []
        return out

    # ---- DAILY IA ----
    if ai_decision:
        if not ai_global_enabled or not ai_daily_enabled:
            try:
                ai_decision.logger.info(
                    "[Reports] Daily IA report disabled by config (ai.audit_mode.daily_report_enabled=false or ai.audit_mode.enabled=false)."
                )
            except Exception:
                pass
        elif state.get("last_daily_date") != today_str:
            try:
                logs_today = _collect_daily_logs()
                ai_result = ai_decision.audit_trading_performance(
                    logs=logs_today, period="last_day", current_context=None
                )
                # Marquer comme fait seulement si succès IA
                if isinstance(ai_result, dict) and "error" not in ai_result:
                    state["last_daily_date"] = today_str
                    try:
                        ai_decision.logger.info(
                            "[Reports] Daily IA report generated on start."
                        )
                    except Exception:
                        pass
                else:
                    try:
                        ai_decision.logger.warning(
                            "[Reports] Daily IA report FAILED on start."
                        )
                    except Exception:
                        pass
            except Exception as e:
                try:
                    ai_decision.logger.error(
                        f"[Reports] Daily IA report exception: {e}", exc_info=True
                    )
                except Exception:
                    pass

    # ---- WEEKLY MECANO (Dimanche=6) ----
    if mecano and weekday == 6:
        if not mecano_weekly_enabled:
            try:
                mecano.logger.info(
                    "[Reports] Weekly Mecano report disabled by config (mecano.weekly_report_enabled=false)."
                )
            except Exception:
                pass
        elif state.get("last_weekly_date") != today_str:
            try:
                weekly = mecano.build_weekly_report()
                mecano.export_report(weekly, format="json")
                state["last_weekly_date"] = today_str
                try:
                    mecano.logger.info(
                        "[Reports] Weekly Mecano report generated on Sunday start."
                    )
                except Exception:
                    pass
            except Exception as e:
                try:
                    mecano.logger.error(
                        f"[Reports] Weekly Mecano report exception: {e}", exc_info=True
                    )
                except Exception:
                    pass

    # ---- Persist state (atomique simple) ----
    try:
        tmp = state_path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(state_path)
    except Exception:
        pass


def verify_environment_and_config(
    config_manager: ConfigManager, mt5_connector: MT5Connector, bot_mode: str
) -> None:
    """
    Vérifie les composants critiques de l'environnement (modèle AI, identifiants MT5, Telegram)
    et s'assure que la configuration est valide.

    Args:
        config_manager (ConfigManager): Une instance de ConfigManager avec la configuration chargée.
        mt5_connector (MT5Connector): Une instance de MT5Connector pour les tests de connexion MT5.
        bot_mode (str): Le mode d'exécution du bot ('DEMO' ou 'LIVE').

    Raises:
        SystemExit: Si des composants critiques sont manquants ou invalides.
        RuntimeError: Si la connexion MT5 échoue pendant la vérification initiale.
    """
    logger = logging.getLogger(__name__)  # Utilise le logger local
    logger.info(
        "Vérification de l'environnement de production et de la configuration chargée..."
    )

    try:
        # Accéder à la configuration dynamique déjà chargée
        current_config = config_manager.get_current_dynamic_config()
        if not current_config:
            logger.critical(
                "FATAL: La configuration dynamique est vide après l'initialisation. Le bot ne peut pas continuer."
            )
            sys.exit(1)
        logger.info("Configuration dynamique accédée avec succès pour vérification.")
    except Exception as e:
        logger.critical(
            f"FATAL: Impossible de charger la configuration du bot. Erreur: {e}",
            exc_info=True,
        )
        sys.exit(1)

    # Vérifier la présence du modèle AI (chemin et nom lus dynamiquement)
    models_dir = config_manager.get("paths.models", "models/")
    ai_model_name = config_manager.get("ai.model_name", "llama-2-7b-chat.Q4_K_M.gguf")

    model_path = Path(models_dir) / ai_model_name
    if not model_path.is_file():
        logger.critical(
            f"FATAL: Modèle IA non trouvé à '{model_path}'. Le bot ne peut pas démarrer sans modèle IA. Veuillez télécharger le modèle GGUF."
        )
        sys.exit(1)
    logger.info(f"Modèle IA trouvé : {model_path}")

    # Vérification des identifiants MT5 via ConfigManager (qui les a chargés depuis .env)
    active_mt5_account_details = None
    try:
        active_mt5_account_details = config_manager.get_mt5_account_credentials(
            mode=bot_mode
        )
        if active_mt5_account_details is None:
            logger.critical(
                f"FATAL: Aucun compte MT5 actif ou valide n'a pu être trouvé pour le mode '{bot_mode}'. Vérifiez la configuration dans 'broker_accounts.json' et les variables d'environnement."
            )
            sys.exit(1)

        logger.info(
            f"Compte MT5 actif sélectionné pour vérification : '{active_mt5_account_details['account_id']}' (Login: {active_mt5_account_details['login']})."
        )

        # Vérification proactive de la connexion MT5 avec le compte sélectionné
        if not mt5_connector.connect(active_mt5_account_details):
            raise RuntimeError(
                f"La connexion initiale à MetaTrader 5 a échoué pour le compte '{active_mt5_account_details['account_id']}'. Veuillez vérifier les identifiants et le statut du terminal."
            )
        logger.info(
            "Connexion MT5 vérifiée avec succès (connexion/déconnexion initiale)."
        )
    except (ValueError, RuntimeError) as e:
        logger.critical(
            f"FATAL: Échec de la configuration ou de la connexion MT5 : {e}",
            exc_info=True,
        )
        sys.exit(1)
    finally:
        if mt5_connector.is_connected:
            try:
                mt5_connector.disconnect()
                logger.info("Déconnecté de MetaTrader 5 après vérification initiale.")
            except Exception as e:
                logger.warning(
                    f"Erreur lors de la déconnexion de MetaTrader 5 après vérification: {e}"
                )

    # Vérifier les identifiants Telegram (crucial pour le monitoring en production si activé)
    telegram_token = config_manager.get("env_vars.TELEGRAM_BOT_TOKEN")
    telegram_chat_id = config_manager.get("env_vars.TELEGRAM_CHAT_ID")

    if not (telegram_token and telegram_chat_id):
        telegram_globally_enabled = config_manager.get("telegram.enabled", False)
        if telegram_globally_enabled:
            logger.critical(
                "FATAL: Le bot token ou l'ID de chat Telegram est manquant dans les variables d'environnement chargées par ConfigManager. Les notifications Telegram sont critiques pour le monitoring en production quand activées. Sortie du bot."
            )
            sys.exit(1)
        else:
            logger.warning(
                "Les notifications Telegram sont globalement désactivées et les identifiants ne sont pas définis. Le bot continue sans alertes Telegram."
            )
    else:
        logger.info(
            "Identifiants Telegram chargés (via ConfigManager depuis les variables d'environnement)."
        )

    logger.info(
        "Vérification de la configuration et de l'environnement terminée avec succès."
    )


def main(args: argparse.Namespace) -> None:
    """
    Fonction principale (Racine de Composition).
    1. Initialise les configurations et le logging.
    2. Crée toutes les instances des modules.
    3. Injecte les dépendances entre les modules.
    4. Lance la boucle de trading.
    """
    # 1. Configuration initiale (robuste aux args partiels)
    log_level = getattr(args, "log_level", "INFO")
    setup_production_logging(log_level=log_level)
    logger = logging.getLogger(__name__)

    # Helper local pour envoyer une alerte Telegram (compatibilité de signature)
    def _safe_alert(cm, msg: str, channel: str = "telegram_critical"):
        if not cm:
            return
        try:
            cm.send_alert(msg, channel)
        except TypeError:
            try:
                cm.send_alert(message=msg, alert_type=channel)
            except Exception:
                logger.warning("Échec send_alert (toutes variantes).")

    # Initialiser les variables pour le bloc finally
    config_manager = None
    mt5_connector = None
    ai_decision = None

    try:
        # --- Étape A : Charger la Configuration ---
        config_manager = ConfigManager()
        config_dir_path = Path(config_manager.get("paths.configs", "config/"))
        main_config_file_name = config_manager.get(
            "paths.main_config_file_name", "prod_config.json"
        )
        config_file_path = config_dir_path / main_config_file_name

        strategy_configs_path = Path(
            config_manager.get("paths.strategy_configs", "config/strategy/")
        )

        config_manager.initialize_dynamic_config(
            template_path=str(config_file_path),
            output_path=str(config_file_path),
            config_dir=str(strategy_configs_path),
        )

        # --- Étape B : Créer et Assembler toutes les "Briques" dans le bon ordre ---
        logger.info("Assemblage des modules principaux de l'application...")

        audit_logger = AuditLogger(config_manager_instance=config_manager)
        mt5_connector = MT5Connector()

        strategy_manager = StrategyManager(
            config_loader_instance=config_manager.config_loader,
            config_manager_instance=config_manager,
        )
        strategy_manager.initialize_strategies()

        models_dir = config_manager.get("paths.models", "models/")
        ai_model_name = config_manager.get(
            "ai.model_name", "llama-2-7b-chat.Q4_K_M.gguf"
        )
        ai_decision = AIDecision(
            model_path=str(Path(models_dir) / ai_model_name),
            config_manager_instance=config_manager,
        )
        ai_interface = AIInterface(
            config_manager_instance=config_manager, ai_decision_instance=ai_decision
        )

        decision_pipeline = DecisionPipeline(
            config_manager_instance=config_manager,
            ai_interface_instance=ai_interface,
            strategy_manager_instance=strategy_manager,
        )
        phase_observer = PhaseObserver(config_manager=config_manager)
        mecano = Mecano(config_manager_instance=config_manager)
        mecano.set_ai_analyzer(ai_decision)

        config_manager.ai_decision_instance = ai_decision

        # === Déclenchement des rapports au démarrage (Daily IA + Weekly Mecano) ===
        try:
            _trigger_fn = globals().get("_trigger_ai_and_mecano_reports_on_start")
            if callable(_trigger_fn):
                _trigger_fn(ai_decision, mecano, config_manager)
            else:
                logger.debug(
                    "Helper '_trigger_ai_and_mecano_reports_on_start' introuvable : saut du déclenchement auto des rapports."
                )
        except Exception as e:
            logger.warning(
                f"Échec déclenchement auto rapports (démarrage): {e}", exc_info=True
            )

        # --- Étape C : Établir les connexions et faire les vérifications finales ---
        bot_mode_cfg = str(config_manager.get("mode_execution", "DEMO")).upper()
        bot_mode = str(getattr(args, "mode", bot_mode_cfg) or bot_mode_cfg).upper()

        if config_manager.get("mode_execution") != bot_mode:
            config_manager.update_dynamic_config(
                {"mode_execution": bot_mode}, source="mode_startup_correction"
            )

        is_dry_run = bool(getattr(args, "dry_run", False))
        logger.critical(
            f"Le bot démarre en mode {'DRY RUN' if is_dry_run else bot_mode}. "
            f"{'LES TRADES RÉELS SERONT EXÉCUTÉS. SOYEZ PRUDENT !' if bot_mode == 'LIVE' and not is_dry_run else 'Aucun trade réel.'}"
        )
        time.sleep(int(config_manager.get("app.startup_delay_seconds", 3)))

        # ✅ Vérification centralisée
        verify_environment_and_config(config_manager, mt5_connector, bot_mode)

        # ✅ Connexion MT5 persistante (post-vérification)
        active_account_details = config_manager.get_mt5_account_credentials(
            mode=bot_mode
        )
        if not active_account_details:
            logger.critical(
                f"FATAL: Aucun compte MT5 configuré pour le mode {bot_mode}"
            )
            raise RuntimeError(f"Aucun compte MT5 disponible pour le mode {bot_mode}")

        account_id = active_account_details.get("account_id", "Unknown")
        if not mt5_connector.connect(active_account_details):
            raise RuntimeError(
                f"Échec de la connexion MT5 persistante pour '{account_id}'."
            )

        logger.info(f"Connexion MT5 persistante établie pour '{account_id}'.")

        trade_executor = TradeExecutor(
            config_manager=config_manager, mt5_connector=mt5_connector, mode=bot_mode
        )
        trade_executor.reconcile_state_with_broker()

        logger.info("🔒 Gate readiness MTF activé : il sera vérifié à chaque cycle.")

    except (SystemExit, RuntimeError, Exception) as e:
        logger.critical(
            f"FATAL: Erreur critique lors du démarrage du bot: {e}", exc_info=True
        )
        _safe_alert(
            config_manager, f"**SNIPER_X BOT - CRASH AU DÉMARRAGE !**\nErreur: {e}"
        )
        if mt5_connector and mt5_connector.is_connected:
            mt5_connector.disconnect()
        sys.exit(1)

    # --- Étape D : Lancer la Boucle de Trading ---
    try:
        cycle_interval = getattr(args, "interval", None)
        if cycle_interval is None:
            cycle_interval = config_manager.get(
                "bot_behavior.cycle_interval_seconds", 5
            )
        cycle_interval = max(0.5, float(cycle_interval))  # clamp doux
    except Exception:
        cycle_interval = 5.0

    _safe_alert(
        config_manager,
        f"**SNIPER_X Bot Démarré!**\nMode: {'DRY RUN' if is_dry_run else bot_mode}",
    )
    logger.info("SNIPER_X Bot prêt. Démarrage de la boucle de trading...")

    cycle_count = 0
    daily_trade_count = 0

    # 🚀 NOUVEAU: construire une *seule fois* la liste d'actifs du gate de readiness
    # depuis la même source que le pipeline (cohérence des actifs traités).
    try:
        base_config = config_manager.get_current_dynamic_config()
        global_safety = base_config.get("global_safety", {}) or {}
        all_symbols = list(global_safety.get("global_allowed_symbols", []))

        active_mt5_account_details = config_manager.get_mt5_account_credentials(
            mode=bot_mode
        )
        account_allowed = set(
            (active_mt5_account_details or {}).get("allowed_symbols", [])
        )

        readiness_symbols = [
            a for a in all_symbols if not account_allowed or a in account_allowed
        ]
        if not readiness_symbols:
            readiness_symbols = all_symbols  # filet de sécurité
    except Exception as e:
        logger.warning(
            f"Impossible de construire readiness_symbols dynamiques, fallback statique. Détail: {e}"
        )
        readiness_symbols = ["EURUSD", "GBPUSD", "XAUUSD", "NAS100"]  # fallback ultime

    try:
        while True:
            cycle_count += 1
            print(f"[Cycle] SNIPER_X CYCLE #{cycle_count} - {datetime.now().strftime('%H:%M:%S')}")
            cycle_start_time = time.time()

            # 🔒 Gate readiness MTF vérifié à chaque cycle (avec liste dynamique cohérente)
            if not _mtf_readiness_gate(
                mt5_connector,
                phase_observer,
                config_manager,
                readiness_symbols,
                cycle_count,
            ):
                logger.info(
                    f"Cycle #{cycle_count}: readiness non validé, pas de trade ce tour."
                )
                time.sleep(cycle_interval)
                continue

            print("[Pipeline] Lancement du pipeline de décision...")
            trade_executed_in_cycle = run_single_pipeline_cycle(
                mt5_connector,
                phase_observer,
                decision_pipeline,
                trade_executor,
                config_manager,
                mecano,
                is_dry_run,
                cycle_count,
                daily_trade_count,
            )

            if trade_executed_in_cycle:
                daily_trade_count += 1

            cycle_duration = time.time() - cycle_start_time
            logger.info(
                f"[PERF] Cycle #{cycle_count} exécuté en {cycle_duration:.2f} secondes."
            )

            # ✅ Gestion sécurisée de get_account_info()
            current_account_info = {}
            if mt5_connector and mt5_connector.is_connected:
                account_info_raw = mt5_connector.get_account_info()
                if account_info_raw is not None:
                    try:
                        current_account_info = account_info_raw._asdict()
                    except AttributeError:
                        logger.warning(
                            "Impossible de convertir account_info en dictionnaire"
                        )
                        current_account_info = {}

            # ✅ Vérification que _open_positions existe
            open_positions_count = len(getattr(trade_executor, "_open_positions", {}))

            config_manager.process_and_send_summary_alert(
                context={
                    "bot_mode": bot_mode,
                    "bot_status": "Running",
                    "account_info": current_account_info,
                    "daily_trade_count": daily_trade_count,
                    "open_positions_count": open_positions_count,
                }
            )

            sleep_time = max(0.0, float(cycle_interval) - cycle_duration)
            if sleep_time > 0:
                time.sleep(sleep_time)

    except KeyboardInterrupt:
        logger.warning("\nInterruption clavier détectée. Arrêt progressif...")
        _safe_alert(config_manager, "**SNIPER_X Bot Arrêté Manuellement.**")
    except Exception as e:
        logger.critical(
            f"Une erreur critique non gérée a entraîné la terminaison de la boucle principale : {e}",
            exc_info=True,
        )
        _safe_alert(
            config_manager,
            f"**SNIPER_X BOT S'EST ARRÊTÉ (CRASH) !**\nErreur: {type(e).__name__} : {e}",
        )
    finally:
        if config_manager and config_manager.get("ai.enabled", False) and ai_decision:
            logger.info(
                "Sauvegarde de l'historique des suggestions de l'IA avant l'arrêt..."
            )
            try:
                ai_decision._save_suggestion_history()
            except Exception as e:
                logger.error(f"Erreur lors de la sauvegarde de l'historique IA: {e}")

        if mt5_connector and mt5_connector.is_connected:
            try:
                mt5_connector.disconnect()
            except Exception as e:
                logger.error(f"Erreur lors de la déconnexion MT5: {e}")

        logger.info("SNIPER_X Bot est arrêté.")
        sys.exit(0)
