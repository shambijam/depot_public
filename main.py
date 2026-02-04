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
from datetime import datetime, timezone
import time
from pathlib import Path
import importlib
import core.strategy_manager

# Charger les variables d'environnement dès le début pour les chemins critiques/secrets
from dotenv import load_dotenv
from phase_observer.market_analyzer import MarketAnalyzer
from phase_observer.market_analyzer import MarketAnalyzer
from phase_observer.orchestrator import PhaseObserver


load_dotenv()

try:

    from core.config_manager import ConfigManager

    importlib.reload(core.strategy_manager)
    from trader.trade_executor import TradeExecutor
    from mt5_connector import MT5Connector
    from mecanique_generale.mecano import Mecano
    from run_bot import (
        run_single_pipeline_cycle,
    )
    from utils.logger_setup import (
        setup_production_logging,
    )  # ← source unique pour le logging
    from core.audit_logger import AuditLogger
    from core.strategy_manager import StrategyManager
    from core.decision_pipeline import DecisionPipeline
    from core.telegram_bot import create_telegram_controller

    from run_bot import (
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


def sleep_until_next_minute():
    now = datetime.now(timezone.utc)
    to_sleep = 60.0 - (now.second + now.microsecond / 1e6)
    if to_sleep > 0:
        time.sleep(to_sleep)


# === [FONCTION IA SUPPRIMÉE - Session 23 Nov 2025] ===
# Fonction _trigger_ai_and_mecano_reports_on_start supprimée (169 lignes)
# Raison : Module IA (ai_core, ai_interface) complètement retiré du système


def verify_environment_and_config(
    config_manager: ConfigManager, mt5_connector: MT5Connector, bot_mode: str
) -> None:
    """
    Vérifie les composants critiques de l'environnement (modèle AI, identifiants MT5, Telegram)
    et s'assure que la configuration est valide.

    Args:
        config_manager (ConfigManager): Une instance de ConfigManager avec la configuration chargée.
        mt5_connector (MT5Connector): Une instance de MT5Connector (utilisé plus tard pour la connexion persistante).
        bot_mode (str): Le mode d'exécution du bot ('DEMO' ou 'LIVE').

    Raises:
        SystemExit: Si des composants critiques sont manquants ou invalides.
    """
    logger = logging.getLogger(__name__)
    logger.info(
        "Vérification de l'environnement de production et de la configuration chargée..."
    )

    # --- Vérification de la configuration dynamique ---
    try:
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

    # --- Vérification des identifiants MT5 (sans tentative de connexion ici) ---
    try:
        active_mt5_account_details = config_manager.get_mt5_account_credentials(
            mode=bot_mode
        )
        if active_mt5_account_details is None:
            logger.critical(
                f"FATAL: Aucun compte MT5 actif ou valide n'a pu être trouvé pour le mode '{bot_mode}'. "
                f"Vérifiez la configuration dans 'broker_accounts.json' et les variables d'environnement."
            )
            sys.exit(1)

        logger.info(
            f"Compte MT5 actif détecté pour le mode '{bot_mode}' : "
            f"'{active_mt5_account_details['account_id']}' (Login: {active_mt5_account_details['login']})."
        )
    except Exception as e:
        logger.critical(
            f"FATAL: Erreur lors du chargement des identifiants MT5 : {e}",
            exc_info=True,
        )
        sys.exit(1)

    # --- Vérification des identifiants Telegram ---
    telegram_token = config_manager.get("telegram.bot_token") or config_manager.get("env_vars.TELEGRAM_BOT_TOKEN")
    telegram_chat_ids = config_manager.get("telegram.authorized_chat_ids") or []
    telegram_chat_id = config_manager.get("env_vars.TELEGRAM_CHAT_ID") or (telegram_chat_ids[0] if telegram_chat_ids else None)

    if not (telegram_token and telegram_chat_id):
        telegram_globally_enabled = config_manager.get("telegram.enabled", False)
        if telegram_globally_enabled:
            logger.warning(
                "Telegram est active mais token/chat_id manquant. Verifiez telegram_config.json. Le bot continue sans Telegram."
            )
        else:
            logger.warning(
                "Les notifications Telegram sont globalement desactivees. Le bot continue sans alertes Telegram."
            )
    else:
        logger.info("Identifiants Telegram charges depuis telegram_config.json.")

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
        # --- SNAPSHOT CFG BURST (complet) ---
        base_cfg = config_manager.get_current_dynamic_config() or {}
        g_burst = (
            ((base_cfg.get("entry_rules") or {}).get("scalping") or {}).get(
                "burst_scalping"
            )
            or {}
        ).get("burst_size")

        # stratégie (config_trade_scalping.json via StrategyManager)
        try:
            strat_cfg = strategy_manager.get_strategy_config("scalping") or {}
        except Exception:
            strat_cfg = {}
        s_burst = (
            ((strat_cfg.get("entry_rules") or {}).get("scalping") or {}).get(
                "burst_scalping"
            )
            or {}
        ).get("burst_size")

        # asset XAUUSD : entry_rules + overrides (deux chemins possibles) + legacy
        try:
            xau = config_manager.load_asset_config("XAUUSD") or {}
        except Exception:
            xau = {}

        a_entry = (
            ((xau.get("entry_rules") or {}).get("scalping") or {}).get("burst_scalping")
            or {}
        ).get("burst_size")
        a_override = (
            (
                (
                    ((xau.get("overrides") or {}).get("scalping") or {}).get(
                        "entry_rules"
                    )
                    or {}
                ).get("scalping")
                or {}
            ).get("burst_scalping")
            or {}
        ).get("burst_size")
        a_legacy = (
            ((xau.get("overrides") or {}).get("scalping") or {}).get("burst") or {}
        ).get("burst_size")

        logger.critical(
            f"[CFG@BOOT] burst_size global={g_burst} | strategy={s_burst} | "
            f"XAUUSD.entry={a_entry} | XAUUSD.override={a_override} | XAUUSD.legacy={a_legacy}"
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

        decision_pipeline = DecisionPipeline(
            config_manager_instance=config_manager,
            strategy_manager_instance=strategy_manager,
        )
        decision_pipeline.extra_context = {}
        mecano = Mecano(config_manager_instance=config_manager)

        phase_observer = PhaseObserver(config_manager=config_manager)
        market_analyzer = MarketAnalyzer(config_manager=config_manager, logger=logger)

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

        # --- Initialisation Telegram Bot Controller ---
        telegram_controller = None
        try:
            telegram_controller = create_telegram_controller(
                config_manager=config_manager,
                mt5_connector=mt5_connector,
                logger=logger
            )
            if telegram_controller:
                telegram_controller.start()
                logger.info("Telegram Bot Controller demarre avec succes")
        except Exception as e_telegram:
            logger.warning(f"Telegram Bot Controller non demarre: {e_telegram}")

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
        readiness_symbols = ["USDCHF", "USDJPY"]  # fallback ultime

    try:

        # 🔔 Calage initial : premier cycle à la minute exacte
        sleep_until_next_minute()

        # ═══════════════════════════════════════════════════════════════════════
        while True:
            cycle_count += 1
            print(
                f"[Cycle] SNIPER_X CYCLE #{cycle_count} - {datetime.now().strftime('%H:%M:%S')}"
            )
            cycle_start_time = time.time()

            # Préparer un dictionnaire de pré-signaux live pour ce cycle
            live_pre_signals = {}
            for asset in readiness_symbols:
                try:
                    live_signal = phase_observer.analyze_live_bar(asset)
                    if live_signal:
                        live_pre_signals[asset] = live_signal
                except Exception as e:
                    logger.warning(
                        f"[{asset}] Impossible d'analyser la bougie live: {e}"
                    )

            _current_ctx = getattr(decision_pipeline, "extra_context", {})
            decision_pipeline.extra_context = {
                **_current_ctx,
                "live_pre_signals": live_pre_signals,
            }

            trade_executed_in_cycle = run_single_pipeline_cycle(
                mt5_connector,
                decision_pipeline,
                trade_executor,
                config_manager,
                mecano,
                strategy_manager,
                is_dry_run,
                cycle_count,
                daily_trade_count,
            )

            if trade_executed_in_cycle:
                daily_trade_count += 1

            # 🔍 Surveillance des baskets burst (fermeture à +15 pips)
            try:
                base_config = config_manager.get_current_dynamic_config()

                # Fusionner la config de stratégie scalping pour avoir entry_rules
                try:
                    scalping_config = (
                        strategy_manager.get_strategy_config("scalping") or {}
                    )
                    merged_config = dict(base_config)
                    if "entry_rules" in scalping_config:
                        merged_config.setdefault("entry_rules", {}).update(
                            scalping_config["entry_rules"]
                        )
                except Exception as merge_err:
                    logger.warning(
                        f"[BASKET_MONITOR] Fusion config scalping échouée: {merge_err}"
                    )
                    merged_config = base_config

                trade_executor.monitor_burst_baskets(config=merged_config)
            except Exception as e:
                logger.debug(f"[BASKET_MONITOR] Erreur surveillance baskets: {e}")

            cycle_duration = time.time() - cycle_start_time
            logger.info(
                f"[PERF] Cycle #{cycle_count} exécuté en {cycle_duration:.2f} secondes."
            )
            # 🔍 Analyse live de la bougie en cours (pré-signal)
            for asset in readiness_symbols:
                try:
                    live_signal = phase_observer.analyze_live_bar(asset)
                    if live_signal:
                        logger.debug(
                            f"[{asset}] Pré-signal live: Δ={live_signal['footprint_delta']} | POC={live_signal['footprint_poc']}"
                        )
                except Exception as e:
                    logger.warning(
                        f"[{asset}] Impossible d'analyser la bougie live: {e}"
                    )

            # 🔄 Recalibration périodique toutes les 30 minutes
            if cycle_count % 30 == 0:
                for asset in readiness_symbols:
                    try:
                        phase_observer.recalibrate_full(asset)
                    except Exception as e:
                        logger.warning(f"[{asset}] Recalibration échouée: {e}")

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

            # ⏱️ Horloge suisse : attendre exactement la prochaine minute
            sleep_until_next_minute()

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

        if mt5_connector and mt5_connector.is_connected:
            try:
                mt5_connector.disconnect()
            except Exception as e:
                logger.error(f"Erreur lors de la déconnexion MT5: {e}")

        logger.info("SNIPER_X Bot est arrêté.")
        sys.exit(0)
