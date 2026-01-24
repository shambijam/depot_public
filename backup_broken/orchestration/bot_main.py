#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
orchestration/bot_main.py - Fonction main() du bot SNIPER_X

Contient:
- main(args): Point d'entrée principal pour lancer le bot
"""

import argparse
import logging
import os
import sys
import time
import threading
import queue
import signal
import platform
from pathlib import Path

# Imports SNIPER_X (chargés dynamiquement dans main)
# Les modules lourds sont importés dans main() pour optimiser le temps de démarrage


def main(args: argparse.Namespace) -> None:
    """
    Fonction principale pour initialiser le bot, gérer les arguments de la CLI,
    et lancer la boucle de trading infinie.

    Args:
        args (argparse.Namespace): Les arguments parsés de la ligne de commande.
    """
    # 1. Configuration du Logging de Production (Appelé en premier)
    from utils.logger_setup import setup_production_logging

    log_level = getattr(args, "log_level", "INFO")
    setup_production_logging(log_level=log_level)
    logger = logging.getLogger(__name__)

    # 2. Initialisation du ConfigManager
    from core.config_manager import ConfigManager

    config_manager = ConfigManager()

    # 3. Définir le mode d'exécution du bot (CLI > Config > Défaut)
    bot_mode = (
        args.mode if args.mode else config_manager.get("mode_execution", "DEMO").upper()
    )
    is_dry_run = args.dry_run

    # Message de démarrage critique pour alerter l'opérateur
    logger.critical(
        f"Le bot démarre en mode {'DRY RUN' if is_dry_run else bot_mode}. "
        f"{'LES TRADES RÉELS SERONT EXÉCUTÉS. SOYEZ EXTRÊMEMENT PRUDENT !' if bot_mode == 'LIVE' and not is_dry_run else 'Aucun trade réel : Mode DÉMO ou DRY RUN.'}"
    )

    startup_delay_seconds = config_manager.get("app.startup_delay_seconds", 3)
    time.sleep(startup_delay_seconds)

    # 4. Configuration du Hot-Reload (rechargement config sans redémarrage)
    def setup_hot_reload_handler():
        """
        Configure le handler de signal pour rechargement config à chaud.
        Usage: kill -SIGUSR1 <PID> ou kill -SIGUSR2 <PID> (Windows)
        """
        def reload_config_handler(signum, frame):
            """Handler appelé lors de la réception du signal de rechargement."""
            try:
                logger.info("=" * 80)
                logger.info("[HOT-RELOAD] Signal de rechargement reçu !")
                logger.info("=" * 80)
                config_manager.reload_all_configs()
                logger.info("=" * 80)
                logger.info("[HOT-RELOAD] Configuration rechargée avec succès")
                logger.info("[HOT-RELOAD] Les prochains cycles utiliseront la nouvelle config")
                logger.info("=" * 80)
            except Exception as e:
                logger.error(f"[HOT-RELOAD] Erreur lors du rechargement: {e}", exc_info=True)

        # Utiliser SIGUSR1 (Linux/Mac) ou SIGBREAK (Windows)
        if platform.system() == "Windows":
            # Windows: SIGBREAK (Ctrl+Break) est le seul signal custom disponible
            logger.info("[HOT-RELOAD] Système Windows détecté - Handler SIGBREAK configuré")
            logger.info("[HOT-RELOAD] Pour recharger: envoyez SIGBREAK au processus")
            signal.signal(signal.SIGBREAK, reload_config_handler)
        else:
            # Linux/Mac: SIGUSR1
            logger.info("[HOT-RELOAD] Système Unix détecté - Handler SIGUSR1 configuré")
            logger.info(f"[HOT-RELOAD] Pour recharger: kill -SIGUSR1 {os.getpid()}")
            signal.signal(signal.SIGUSR1, reload_config_handler)

    try:
        setup_hot_reload_handler()
    except Exception as e:
        logger.warning(f"[HOT-RELOAD] Impossible de configurer le hot-reload: {e}")

    # 5. Instanciation des Modules Fondamentaux
    try:
        # Imports lourds (chargés ici pour optimiser le temps de démarrage)
        from phase_observer.orchestrator import PhaseObserver
        from core.decision_pipeline import DecisionPipeline
        from trader.trade_executor import TradeExecutor
        from mt5_connector import MT5Connector
        from mecanique_generale.mecano import Mecano
        from core.strategy_manager import StrategyManager

        config_dir_path = Path(config_manager.get("paths.configs", "config/"))
        main_config_file_name = config_manager.get(
            "paths.main_config_file_name", "prod_config.json"
        )
        config_file_path = config_dir_path / main_config_file_name
        config_file_path.parent.mkdir(parents=True, exist_ok=True)

        strategy_configs_path = config_manager.get(
            "paths.strategy_configs", str(config_dir_path / "strategies")
        )

        config_manager.initialize_dynamic_config(
            template_path=str(config_file_path),
            output_path=str(config_file_path),
            config_dir=strategy_configs_path,
        )

        # Instancier MT5Connector
        mt5_connector = MT5Connector()

        # Instancier PhaseObserver
        phase_observer = PhaseObserver(config_manager=config_manager)

        # Sécurité supplémentaire : vérifier que PhaseObserver est initialisé
        if not phase_observer or not hasattr(phase_observer, "lookback_window"):
            logger.critical("PhaseObserver non initialisé correctement -> arrêt.")
            sys.exit(1)

        # Instancier TradeExecutor
        trade_executor = TradeExecutor(
            config_manager=config_manager, mt5_connector=mt5_connector, mode=bot_mode
        )

        # Instancier Mecano
        mecano = Mecano(config_manager_instance=config_manager)

        # Instancier StrategyManager
        strategy_manager = StrategyManager(
            config_loader_instance=config_manager.config_loader,
            config_manager_instance=config_manager,
        )
        strategy_manager.initialize_strategies()

        # Instancier DecisionPipeline
        decision_pipeline = DecisionPipeline(
            config_manager_instance=config_manager,
            strategy_manager_instance=strategy_manager,
        )
        decision_pipeline.extra_context = {}

    except Exception as e:
        logger.critical(
            f"FATAL: Erreur lors de l'initialisation des modules fondamentaux: {e}",
            exc_info=True,
        )
        sys.exit(1)

    # 6. Vérification Finale de l'Environnement et des Modules
    try:
        from orchestration.environment import verify_environment_and_config
        verify_environment_and_config(config_manager, mt5_connector, bot_mode)

        default_cycle_interval_from_config = config_manager.get(
            "bot_behavior.cycle_interval_seconds", 23
        )
        cycle_interval = (
            args.interval
            if args.interval is not None
            else default_cycle_interval_from_config
        )

        if args.interval is not None:
            logger.info(
                f"Utilisation de l'intervalle de cycle spécifié par la CLI ({args.interval}s)."
            )
        elif cycle_interval != default_cycle_interval_from_config:
            logger.info(
                f"Utilisation de l'intervalle de cycle configuré ({cycle_interval}s) depuis la config."
            )
        else:
            logger.info(
                f"Utilisation de l'intervalle de cycle par défaut ({default_cycle_interval_from_config}s)."
            )

        config_manager.send_alert(
            message=f"**SNIPER_X Bot Démarré!**\nMode: {'DRY RUN' if is_dry_run else bot_mode}\nIntervalle de Cycle: {cycle_interval}s",
            alert_type="telegram_critical",
        )

    except SystemExit:
        logger.critical(
            "Le démarrage du bot a été avorté en raison de problèmes critiques."
        )
        config_manager.send_alert(
            "**SNIPER_X BOT N'A PAS DÉMARRÉ !**\nProblème critique configuration/environnement.",
            "telegram_critical",
        )
        sys.exit(1)
    except Exception as e:
        logger.critical(
            f"FATAL: Erreur non gérée lors du chargement de la configuration: {e}",
            exc_info=True,
        )
        config_manager.send_alert(
            f"**SNIPER_X BOT CRASH AU DÉMARRAGE !**\nErreur: {type(e).__name__}",
            "telegram_critical",
        )
        sys.exit(1)

    logger.info(f"SNIPER_X Bot prêt. Intervalle de cycle: {cycle_interval} secondes.")
    cycle_count = 0
    daily_trade_count = 0

    # Réconciliation initiale TradeExecutor ET connexion persistante MT5
    try:
        account_details_for_reconciliation = config_manager.get_mt5_account_credentials(
            mode=bot_mode
        )
        if account_details_for_reconciliation:
            if mt5_connector.connect(account_details_for_reconciliation):
                trade_executor.reconcile_state_with_broker()
                logger.info("Réconciliation TradeExecutor OK.")
                # NE PAS DÉCONNECTER - Garder la connexion persistante pour les threads !
                logger.info(f"MT5 connecté et prêt (is_connected={mt5_connector.is_connected})")
            else:
                logger.critical("MT5 non connecté pour la réconciliation - ARRÊT DU BOT")
                sys.exit(1)
        else:
            logger.critical("Aucun compte MT5 dispo pour la réconciliation - ARRÊT DU BOT")
            sys.exit(1)
    except Exception as e:
        logger.critical(f"Échec réconciliation TradeExecutor: {e}", exc_info=True)
        config_manager.send_alert(
            f"ALERTE: Réconciliation TradeExecutor échouée: {e}",
            "telegram_critical",
        )
        sys.exit(1)


    # 9. Lancement des Threads Séparés (Scalping 10s + Liquidity 60s + Basket Monitor)
    # CONFIGURABLE (02 JAN 2026): Lire cycle depuis config
    scalping_cycle = config_manager.get("bot_behavior.cycle_interval_seconds", 2.5)

    logger.info("=" * 80)
    logger.info(f"DÉMARRAGE MULTI-THREADING SCALPING (02 JAN 2026 - Cycle {scalping_cycle}s)")
    logger.info("=" * 80)
    logger.info(f"  - SCALPING USDJPY Thread : Cycle {scalping_cycle}s (offset 0.0s)")
    logger.info(f"  - SCALPING NAS100 Thread : Cycle {scalping_cycle}s (offset 1.5s)")
    logger.info(f"  - SCALPING GBPUSD Thread : Cycle {scalping_cycle}s (offset 3.0s)")
    logger.info("  - DASHBOARD Thread       : Affichage agrégé 30s")
    logger.info("  - BASKET MONITOR Thread  : Surveillance continue (polling 100ms)")
    logger.info("=" * 80)

    # Global context partagé avec lock
    # FIX (17 DEC 2025): Initialiser avec active_broker_account pour sizing correct
    try:
        broker_account = config_manager.get_mt5_account_credentials(
            account_id=None,  # None = utilise compte par défaut selon bot_mode
            mode=bot_mode
        )
    except Exception as e:
        logger.warning(f"[INIT] Impossible de récupérer active_broker_account: {e}")
        broker_account = {}

    global_context_shared = {
        "active_broker_account": broker_account,
        "account_info": {},  # Sera mis à jour par les threads
        "open_positions": [],
    }
    context_lock = threading.Lock()

    # Créer GlobalScalpingState (31 DEC 2025)
    from orchestration.threading_state import GlobalScalpingState
    assets = ["USDJPY", "NAS100", "GBPUSD"]
    global_scalping_state = GlobalScalpingState(assets)

    # Créer Display Queue (31 DEC 2025 - Solution B)
    display_queue = queue.Queue(maxsize=100)

    # Events pour arrêt propre
    scalping_stop_event = threading.Event()
    dashboard_stop_event = threading.Event()
    basket_monitor_stop_event = threading.Event()

    # Import workers
    from orchestration.workers.scalping_worker import scalping_worker
    from orchestration.workers.dashboard_worker import dashboard_worker
    from orchestration.workers.basket_monitor import basket_monitor_thread

    # Créer les 3 threads scalping (staggered timing)
    thread_usdjpy = threading.Thread(
        target=scalping_worker,
        args=(
            "USDJPY",                    # asset
            global_scalping_state,       # global state
            display_queue,               # display queue (31 DEC 2025)
            context_lock,                # context lock (31 DEC 2025)
            0.0,                         # offset: démarre immédiatement
            mt5_connector,
            decision_pipeline,
            trade_executor,
            config_manager,
            mecano,
            strategy_manager,
            is_dry_run,
            scalping_stop_event,
            logger
        ),
        daemon=True,
        name="ScalpingWorker-USDJPY"
    )

    thread_nas100 = threading.Thread(
        target=scalping_worker,
        args=(
            "NAS100",                    # asset
            global_scalping_state,       # global state
            display_queue,               # display queue (31 DEC 2025)
            context_lock,                # context lock (31 DEC 2025)
            1.5,                         # offset: 1.5s après USDJPY
            mt5_connector,
            decision_pipeline,
            trade_executor,
            config_manager,
            mecano,
            strategy_manager,
            is_dry_run,
            scalping_stop_event,
            logger
        ),
        daemon=True,
        name="ScalpingWorker-NAS100"
    )

    thread_gbpusd = threading.Thread(
        target=scalping_worker,
        args=(
            "GBPUSD",                    # asset
            global_scalping_state,       # global state
            display_queue,               # display queue (31 DEC 2025)
            context_lock,                # context lock (31 DEC 2025)
            3.0,                         # offset: 3.0s après USDJPY
            mt5_connector,
            decision_pipeline,
            trade_executor,
            config_manager,
            mecano,
            strategy_manager,
            is_dry_run,
            scalping_stop_event,
            logger
        ),
        daemon=True,
        name="ScalpingWorker-GBPUSD"
    )

    # Créer thread dashboard (31 DEC 2025 - Solution B)
    # (05 JAN 2026): Support mode verbose pour déboguer
    verbose_mode = getattr(args, 'verbose', False)
    thread_dashboard = threading.Thread(
        target=dashboard_worker,
        args=(
            display_queue,               # display queue au lieu de global_state
            dashboard_stop_event,
            logger,
            verbose_mode                 # Mode DEBUG activable
        ),
        daemon=True,
        name="Dashboard"
    )

    basket_monitor = threading.Thread(
        target=basket_monitor_thread,
        args=(
            trade_executor,
            config_manager,
            strategy_manager,
            basket_monitor_stop_event,
            logger
        ),
        daemon=True,
        name="BasketMonitorThread"
    )

    # Démarrer tous les threads (31 DEC 2025)
    thread_usdjpy.start()
    thread_nas100.start()
    thread_gbpusd.start()
    thread_dashboard.start()
    basket_monitor.start()

    logger.info("Tous les threads démarrés avec succès")
    logger.info("   -> Appuyez sur Ctrl+C pour arrêter proprement")
    logger.info("=" * 80)

    # Attendre interruption
    try:
        while True:
            time.sleep(1)

    except KeyboardInterrupt:
        logger.warning("Interruption manuelle détectée (Ctrl+C).")
        config_manager.send_alert(
            message="**SNIPER_X Bot Arrêté Manuellement.**",
            alert_type="telegram_critical",
        )
    except Exception as e:
        logger.critical(
            f"Crash dans la boucle principale : {e}",
            exc_info=True,
        )
        config_manager.send_alert(
            f"**SNIPER_X BOT CRASH EN COURS D'EXÉCUTION !**\nErreur: {type(e).__name__}",
            "telegram_critical",
        )
    finally:
        # Arrêt propre des threads
        logger.info("Arrêt des threads en cours...")

        try:
            # Signal arrêt à tous les threads (31 DEC 2025)
            scalping_stop_event.set()
            dashboard_stop_event.set()
            basket_monitor_stop_event.set()

            logger.info("Attente arrêt propre des threads...")

            # Attendre arrêt avec timeout
            thread_usdjpy.join(timeout=5.0)
            thread_nas100.join(timeout=5.0)
            thread_gbpusd.join(timeout=5.0)
            thread_dashboard.join(timeout=5.0)
            basket_monitor.join(timeout=5.0)

            # Vérifier threads encore actifs
            for thread in [thread_usdjpy, thread_nas100, thread_gbpusd, thread_dashboard]:
                if thread.is_alive():
                    logger.warning(f"Thread {thread.name} n'a pas terminé dans les 5s")
                else:
                    logger.info(f"Thread {thread.name} arrêté proprement")

            if basket_monitor.is_alive():
                logger.warning("Thread basket_monitor n'a pas terminé dans les 5s")
            else:
                logger.info("Thread basket_monitor arrêté proprement")
        except Exception as e:
            logger.error(f"Erreur arrêt threads: {e}")


        if "mt5_connector" in locals() and mt5_connector.is_connected():
            mt5_connector.disconnect()

        logger.info("SNIPER_X Bot est arrêté.")
        sys.exit(0)
