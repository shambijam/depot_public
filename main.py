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
    from phase_observer.phase_observer import PhaseObserver
    from core.config_manager import ConfigManager
    importlib.reload(core.strategy_manager)
    from trader.trade_executor import TradeExecutor
    from ai_core.ai_decision import AIDecision
    from mt5_connector import MT5Connector
    from mecanique_generale.mecano import Mecano
    from run_bot import setup_production_logging, run_single_pipeline_cycle
    from utils.logger_setup import setup_production_logging
    from core.audit_logger import AuditLogger
    from core.strategy_manager import StrategyManager
    from core.ai_interface import AIInterface
    from core.decision_pipeline import DecisionPipeline
    import MetaTrader5 as mt5

except ImportError as e:
    logging.critical(
        f"FATAL ERROR: Failed to import a core SNIPER_X module. Error: {e}",
        exc_info=True,
    )
    sys.exit(1)

    # AJOUTEZ CE FLAG GLOBAL
    FORCE_TRADE_MODE = True  # ← METTRE À True POUR FORCER
    FORCE_TRADE_COUNTER = 0


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
    # AMÉLIORATION MAJEURE : Utilisation de get_mt5_account_credentials pour la vérification
    active_mt5_account_details = None
    try:
        # Tenter de récupérer le compte par défaut pour le mode actuel (DEMO/LIVE)
        active_mt5_account_details = config_manager.get_mt5_account_credentials(
            mode=bot_mode
        )
        if active_mt5_account_details is None:
            # get_mt5_account_credentials lève déjà une ValueError/RuntimeError si elle ne trouve rien
            # Donc, si elle retourne None, cela signifie généralement que le compte n'est pas actif.
            # On peut donc se contenter d'un message d'erreur plus générique ici.
            logger.critical(
                f"FATAL: Aucun compte MT5 actif ou valide n'a pu être trouvé pour le mode '{bot_mode}'. Vérifiez la configuration dans 'broker_accounts.json' et les variables d'environnement."
            )
            sys.exit(1)

        logger.info(
            f"Compte MT5 actif sélectionné pour vérification : '{active_mt5_account_details['account_id']}' (Login: {active_mt5_account_details['login']})."
        )

        # Vérification proactive de la connexion MT5 avec le compte sélectionné
        # Utilise mt5_connector.connect qui accepte maintenant un dictionnaire account_details
        if not mt5_connector.connect(active_mt5_account_details):
            # mt5_connector.connect logue déjà les erreurs et alerte via ConfigManager
            raise RuntimeError(
                f"La connexion initiale à MetaTrader 5 a échoué pour le compte '{active_mt5_account_details['account_id']}'. Veuillez vérifier les identifiants et le statut du terminal."
            )
        logger.info(
            "Connexion MT5 vérifiée avec succès (connexion/déconnexion initiale)."
        )
    except (
        ValueError,
        RuntimeError,
    ) as e:  # Capturer les erreurs spécifiques de get_mt5_account_credentials et connect
        logger.critical(
            f"FATAL: Échec de la configuration ou de la connexion MT5 : {e}",
            exc_info=True,
        )
        sys.exit(1)
    finally:
        # S'assurer de la déconnexion après la vérification
        # CORRECTION: Ajouter un try-except autour de disconnect() pour plus de robustesse
        if mt5_connector.is_connected:  # Utilise la propriété is_connected
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
        # Vérifier si les alertes Telegram sont activées globalement dans la config
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
    # 1. Configuration initiale
    setup_production_logging(log_level=args.log_level)
    logger = logging.getLogger(__name__)

    # Initialiser les variables pour le bloc finally
    config_manager = None
    mt5_connector = None
    ai_decision = None

    try:
        # --- Étape A : Charger la Configuration ---
        config_manager = ConfigManager()
        config_dir_path = Path(config_manager.get("paths.configs", "config/"))
        main_config_file_name = config_manager.get("paths.main_config_file_name", "prod_config.json")
        config_file_path = config_dir_path / main_config_file_name
        
        strategy_configs_path = Path(config_manager.get("paths.strategy_configs", "config/strategy/"))
        
        config_manager.initialize_dynamic_config(
            template_path=str(config_file_path),
            output_path=str(config_file_path),
            config_dir=str(strategy_configs_path)
        )

        # --- Étape B : Créer et Assembler toutes les "Briques" dans le bon ordre ---
        logger.info("Assemblage des modules principaux de l'application...")

        audit_logger = AuditLogger(config_manager_instance=config_manager)
        mt5_connector = MT5Connector()
        
        strategy_manager = StrategyManager(
            config_loader_instance=config_manager.config_loader,
            config_manager_instance=config_manager
        )
        strategy_manager.initialize_strategies()

        models_dir = config_manager.get("paths.models", "models/")
        ai_model_name = config_manager.get("ai.model_name", "llama-2-7b-chat.Q4_K_M.gguf")
        ai_decision = AIDecision(
            model_path=str(Path(models_dir) / ai_model_name),
            config_manager_instance=config_manager,
        )
        ai_interface = AIInterface(config_manager_instance=config_manager, ai_decision_instance=ai_decision)

        decision_pipeline = DecisionPipeline(
            config_manager_instance=config_manager,
            ai_interface_instance=ai_interface,
            strategy_manager_instance=strategy_manager
        )
        phase_observer = PhaseObserver(config_manager=config_manager)
        mecano = Mecano(config_manager_instance=config_manager)
        mecano.set_ai_analyzer(ai_decision)
        
        config_manager.ai_decision_instance = ai_decision

        # --- Étape C : Établir les connexions et faire les vérifications finales ---
        bot_mode = args.mode.upper() if args.mode else config_manager.get("mode_execution", "DEMO").upper()
        if config_manager.get("mode_execution") != bot_mode:
             config_manager.update_dynamic_config({"mode_execution": bot_mode}, source="mode_startup_correction")
        
        is_dry_run = args.dry_run
        logger.critical(
            f"Le bot démarre en mode {'DRY RUN' if is_dry_run else bot_mode}. {'LES TRADES RÉELS SERONT EXÉCUTÉS. SOYEZ PRUDENT !' if bot_mode == 'LIVE' and not is_dry_run else 'Aucun trade réel.'}"
        )
        time.sleep(config_manager.get("app.startup_delay_seconds", 3))

        # ✅ CORRECTION 1: Vérification que active_account_details n'est pas None
        active_account_details = config_manager.get_mt5_account_credentials(mode=bot_mode)
        if not active_account_details:
            logger.critical(f"FATAL: Aucun compte MT5 configuré pour le mode {bot_mode}")
            raise RuntimeError(f"Aucun compte MT5 disponible pour le mode {bot_mode}")
        
        # ✅ CORRECTION 2: Vérification sécurisée de l'account_id
        account_id = active_account_details.get('account_id', 'Unknown') if active_account_details else 'Unknown'
        
        if not mt5_connector.connect(active_account_details):
            raise RuntimeError(f"Échec de la connexion MT5 persistante pour '{account_id}'.")
        
        logger.info(f"Connexion MT5 persistante établie pour '{account_id}'.")
        
        trade_executor = TradeExecutor(config_manager=config_manager, mt5_connector=mt5_connector, mode=bot_mode)
        trade_executor.reconcile_state_with_broker()

    except (SystemExit, RuntimeError, Exception) as e:
        logger.critical(f"FATAL: Erreur critique lors du démarrage du bot: {e}", exc_info=True)
        if config_manager:
            config_manager.send_alert(f"**SNIPER_X BOT - CRASH AU DÉMARRAGE !**\nErreur: {e}", "telegram_critical")
        if mt5_connector and mt5_connector.is_connected:
            mt5_connector.disconnect()
        sys.exit(1)

    # --- Étape D : Lancer la Boucle de Trading ---
    cycle_interval = args.interval or config_manager.get("bot_behavior.cycle_interval_seconds", 5)
    config_manager.send_alert(f"**SNIPER_X Bot Démarré!**\nMode: {'DRY RUN' if is_dry_run else bot_mode}", "telegram_critical")
    logger.info("SNIPER_X Bot prêt. Démarrage de la boucle de trading...")
    
    cycle_count = 0
    daily_trade_count = 0
    try:
        while True:
            cycle_count += 1
            print(f"🔄 SNIPER_X CYCLE #{cycle_count} - {datetime.now().strftime('%H:%M:%S')}")
            # FORCER UN TRADE AU CYCLE 10
            if cycle_count == 10:
                print("🔥🔥🔥 FORCING TRADE - CYCLE 10 🔥🔥🔥")
                
                # Court-circuiter TOUT et appeler directement l'exécuteur
                if mt5_connector and mt5_connector.is_connected:
                    tick = mt5_connector.mt5.symbol_info_tick("EURUSD")
                    if tick:
                        request = {
                            "action": mt5_connector.mt5.TRADE_ACTION_DEAL,
                            "symbol": "EURUSD",
                            "volume": 0.01,
                            "type": mt5_connector.mt5.ORDER_TYPE_BUY,
                            "price": tick.ask,
                            "deviation": 20,
                            "magic": 123456,
                            "comment": "FORCED",
                        }
                        result = mt5_connector.mt5.order_send(request)
                        print(f"🔥 RESULT: {result}")
                        if result and result.retcode == 10009:
                            print("✅✅✅ TRADE FORCÉ RÉUSSI!")
                            daily_trade_count += 1
            cycle_start_time = time.time()
            
            print(f"📊 Lancement du pipeline de décision...")
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
            logger.info(f"[PERF] Cycle #{cycle_count} exécuté en {cycle_duration:.2f} secondes.")

            # ✅ CORRECTION 3: Gestion sécurisée de get_account_info()
            current_account_info = {}
            if mt5_connector and mt5_connector.is_connected:
                account_info_raw = mt5_connector.get_account_info()
                if account_info_raw is not None:
                    try:
                        current_account_info = account_info_raw._asdict()
                    except AttributeError:
                        logger.warning("Impossible de convertir account_info en dictionnaire")
                        current_account_info = {}

            # ✅ CORRECTION 4: Vérification que _open_positions existe
            open_positions_count = len(getattr(trade_executor, '_open_positions', {}))
            
            config_manager.process_and_send_summary_alert(
                context={
                    "bot_mode": bot_mode,
                    "bot_status": "Running",
                    "account_info": current_account_info,
                    "daily_trade_count": daily_trade_count,
                    "open_positions_count": open_positions_count
                }
            )

            sleep_time = max(0, cycle_interval - cycle_duration)
            if sleep_time > 0:
                time.sleep(sleep_time)

    except KeyboardInterrupt:
        logger.warning("\nInterruption clavier détectée. Arrêt progressif...")
        if config_manager:
            config_manager.send_alert(message="**SNIPER_X Bot Arrêté Manuellement.**", alert_type="telegram_critical")
    except Exception as e:
        logger.critical(f"Une erreur critique non gérée a entraîné la terminaison de la boucle principale : {e}", exc_info=True)
        if config_manager:
            config_manager.send_alert(f"**SNIPER_X BOT S'EST ARRÊTÉ (CRASH) !**\nErreur: {type(e).__name__} : {e}", "telegram_critical")
    finally:
        if config_manager and config_manager.get("ai.enabled", False) and ai_decision:
            logger.info("Sauvegarde de l'historique des suggestions de l'IA avant l'arrêt...")
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