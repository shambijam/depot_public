#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
run_bot.py - Lanceur et Orchestrateur Principal pour le Bot SNIPER_X
"""

import argparse
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from datetime import UTC
from typing import Any
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

try:
    from phase_observer.phase_observer import PhaseObserver
    from core.config_manager import ConfigManager
    from core.decision_pipeline import DecisionPipeline
    from trader.trade_executor import run_trade_execution_pipeline
    from trader.trade_executor import TradeExecutor, run_trade_execution_pipeline
    from ai_core.ai_decision import AIDecision
    from mt5_connector import MT5Connector
    from utils.logger_setup import setup_production_logging
    from mecanique_generale.mecano import Mecano
    import MetaTrader5 as mt5
except ImportError as e:
    logging.critical(
        f"ERREUR FATALE: Échec de l'importation d'un module de SNIPER_X. Assurez-vous que l'architecture des dossiers est correcte. Erreur: {e}",
        exc_info=True,
    )
    sys.exit(1)


def verify_environment_and_config(
    config_manager: ConfigManager, mt5_connector: MT5Connector, bot_mode: str
) -> dict:
    """
    Charge la configuration principale et vérifie les composants critiques de l'environnement.
    (Nom aligné avec main.py)
    """
    logger = logging.getLogger(__name__)
    logger.info(
        "Chargement et vérification de la configuration et de l'environnement..."
    )

    try:
        config = config_manager.get_current_dynamic_config()
        if not config:
            raise ValueError(
                "La configuration chargée est vide ou invalide après l'initialisation."
            )
        logger.info(
            f"Configuration principale déjà chargée : {config_manager.dynamic_config_path}."
        )
    except Exception as e:
        logger.critical(
            f"FATAL: Impossible de charger la configuration du bot. Erreur: {e}",
            exc_info=True,
        )
        sys.exit(1)

    ai_model_name_from_config = config_manager.get(
        "ai.model_name", "llama-2-7b-chat.Q4_K_M.gguf"
    )
    models_dir = config_manager.get("paths.models", "models/")
    ai_model_path_from_config = Path(models_dir) / ai_model_name_from_config

    if not ai_model_path_from_config.is_file():
        logger.critical(
            f"FATAL: Modèle IA non trouvé à '{ai_model_path_from_config}'. Veuillez télécharger le modèle GGUF. Le bot ne peut pas démarrer."
        )
        sys.exit(1)
    logger.info(f"Modèle IA trouvé : {ai_model_path_from_config}")

    try:
        active_mt5_account_details = config_manager.get_mt5_account_credentials(
            mode=bot_mode
        )
        if active_mt5_account_details is None:
            raise RuntimeError(
                f"Aucun compte MT5 actif par défaut trouvé pour le mode '{bot_mode}'. Vérifiez 'broker_accounts.json'."
            )

        logger.info(
            f"Compte MT5 actif sélectionné pour vérification : '{active_mt5_account_details['account_id']}' (Login: {active_mt5_account_details['login']})."
        )

        if not mt5_connector.connect(active_mt5_account_details):
            raise RuntimeError(
                f"La connexion initiale à MetaTrader 5 a échoué pour le compte '{active_mt5_account_details['account_id']}'. Vérifiez les identifiants et le statut du terminal."
            )
        logger.info("Connexion MT5 vérifiée avec succès. La connexion sera maintenue.")

    except (ValueError, RuntimeError) as e:
        logger.critical(
            f"FATAL: Échec de la configuration ou de la connexion MT5 : {e}",
            exc_info=True,
        )
        sys.exit(1)

    telegram_token = config_manager.get("env_vars.TELEGRAM_BOT_TOKEN")
    telegram_chat_id = config_manager.get("env_vars.TELEGRAM_CHAT_ID")

    if not (telegram_token and telegram_chat_id):
        telegram_globally_enabled = config_manager.get("telegram.enabled", False)
        if telegram_globally_enabled:
            logger.critical(
                "FATAL: Le bot token ou l'ID de chat Telegram est manquant. Les notifications sont critiques pour le monitoring quand activées. Sortie du bot."
            )
            sys.exit(1)
        else:
            logger.warning(
                "Les notifications Telegram sont globalement désactivées et les identifiants ne sont pas définis. Le bot continue sans alertes Telegram."
            )
    else:
        logger.info("Identifiants Telegram chargés.")

    logger.info(
        "Vérification de la configuration et de l'environnement terminée avec succès."
    )
    return config


# --- Fonctions d'Aide (Helpers) pour le Cycle de Pipeline ---


def _get_merged_config_for_asset(
    active_config: dict, config_manager: ConfigManager, asset: str
) -> dict:
    """Fusionne la configuration globale avec la configuration spécifique à l'actif."""
    asset_specific_config = config_manager.config_loader.load_asset_config(asset)
    merged_config = active_config.copy()
    if "strategy_name" in active_config:
        merged_config["strategy_name"] = active_config["strategy_name"]
    if "phase_detection" in active_config:
        merged_config["phase_detection"] = {
            **merged_config.get("phase_detection", {}),
            **asset_specific_config.get("phase_detection", {}),
        }
    for section in [
        "volatility",
        "risk_management",
        "smart_targets",
        "temporal_context",
        "institutional_bias",
        "weighting",
        "strategy_toggles",
    ]:
        if section in asset_specific_config:
            merged_config[section] = {
                **merged_config.get(section, {}),
                **asset_specific_config[section],
            }
    return merged_config


def _is_market_closed(rates_df: pd.DataFrame, active_config: dict) -> bool:
    """Vérifie si le marché pour un actif semble fermé en semaine."""
    closed_market_check_bars = active_config.get("bot_behavior", {}).get(
        "closed_market_check_bars", 15
    )
    if (
        len(rates_df) > closed_market_check_bars
        and rates_df["close"].iloc[-1]
        == rates_df["close"].iloc[-closed_market_check_bars]
    ):
        return True
    return False


# ------------------- FONCTION CORRIGÉE -------------------
def _build_asset_trading_signals(
    latest_signals_row: pd.Series, symbol_info_mt5: Any
) -> dict:
    """
    CORRIGÉ : Construit le dictionnaire de signaux en convertissant TOUTES les données
    du PhaseObserver et en ajoutant les informations critiques du symbole MT5.
    Ceci est le pont parfait qui ne perd aucune donnée.
    """
    signals = latest_signals_row.to_dict()
    signals["current_price"] = latest_signals_row.get("close")
    signals["spread"] = symbol_info_mt5.spread if symbol_info_mt5 else float("inf")
    signals["symbol_point_value"] = (
        symbol_info_mt5.point if symbol_info_mt5 else 0.00001
    )
    signals["symbol_trade_contract_size"] = (
        symbol_info_mt5.trade_contract_size if symbol_info_mt5 else 100000
    )
    if hasattr(latest_signals_row.name, "isoformat"):
        signals["last_update_timestamp"] = latest_signals_row.name.isoformat()
    else:
        signals["last_update_timestamp"] = datetime.now(UTC).isoformat()
    return signals


# ------------------- FIN DE LA CORRECTION -------------------


def _build_asset_market_data(
    annotated_rates_df: pd.DataFrame, symbol_info_mt5: Any
) -> dict:
    """Construit le dictionnaire de données de marché pour un actif."""
    latest_signals_row = annotated_rates_df.iloc[-1]
    return {
        "annotated_rates_df": annotated_rates_df,
        "current_price": latest_signals_row.get("close"),
        "current_spread_points": symbol_info_mt5.spread if symbol_info_mt5 else 0,
        "is_liquid": latest_signals_row.get("is_liquid", True),
        "last_update_timestamp": (
            latest_signals_row.name.isoformat()
            if hasattr(latest_signals_row.name, "isoformat")
            else datetime.now(UTC).isoformat()
        ),
        "symbol_info": symbol_info_mt5._asdict() if symbol_info_mt5 else {},
    }


def _build_global_context(
    mt5: MT5Connector,
    market_data: dict,
    signals: dict,
    cycle: int,
    trades: int,
    cfg: ConfigManager,
    assets: list,
    account: dict,
) -> dict:
    """Assemble le contexte global pour le pipeline de décision."""
    return {
        "market_data": market_data,
        "account_info": (
            mt5.get_account_info()._asdict() if mt5.get_account_info() else {}
        ),
        "open_positions": [p._asdict() for p in mt5.get_positions() or []],
        "trading_signals": signals,
        "current_time_utc": datetime.now(UTC),
        "cycle_count": cycle,
        "daily_trade_count": trades,
        "asset_configs": {
            asset: cfg.config_loader.load_asset_config(asset) for asset in assets
        },
        "active_broker_account": account,
    }


def _mtf_readiness_gate(
    mt5_connector: MT5Connector,
    phase_observer: PhaseObserver,
    config_manager: ConfigManager,
    tradeable_assets: list,
    cycle_count: int,
) -> bool:
    """
    Gate MTF BLOQUANT : retourne True si on peut continuer, False si on bloque le cycle.
    """
    logger = logging.getLogger(__name__)
    try:
        po_cfg = config_manager.config_loader.load_json_config(
            "phase_observer_config.json"
        )
        mtf_cfg = po_cfg.get("multi_timeframe_settings", {})
        data_req = po_cfg.get("data_requirements", {})
        gate_cfg = po_cfg.get("readiness_gate", {})

        required_tfs = mtf_cfg.get("timeframes", ["M1", "M5", "M15"])
        confluence_required = int(mtf_cfg.get("confluence_required", 2))
        min_bars_by_tf = data_req.get(
            "min_bars_by_timeframe", {"M1": 500, "M5": 300, "M15": 200}
        )
        require_all = bool(data_req.get("require_all_timeframes", True))
        block_first_cycles = int(gate_cfg.get("block_signals_first_n_cycles", 12))

        # 1) Gate de démarrage (cycles)
        if cycle_count <= block_first_cycles:
            logger.info(
                f"[READINESS] skip -> startup gate ({cycle_count}/{block_first_cycles})"
            )
            return False

        # 2) Historique par TF sur les actifs
        for asset in tradeable_assets:
            for tf in required_tfs:
                df = mt5_connector.get_rates(asset, tf, min_bars_by_tf.get(tf, 200))
                have = len(df) if df is not None else 0
                need = min_bars_by_tf.get(tf, 0)
                if have < need:
                    logger.info(
                        f"[READINESS] skip -> {asset} {tf}={have}/{need} (historique insuffisant)"
                    )
                    return False

        # 3) Confluence via PhaseObserver si dispo
        if hasattr(phase_observer, "ready_and_confluence_ok"):
            ok, reason = phase_observer.ready_and_confluence_ok(
                confluence_required=confluence_required
            )
            if not ok:
                logger.info(f"[READINESS] skip -> {reason}")
                return False

        return True

    except Exception as e:
        logging.getLogger(__name__).warning(f"[READINESS] check failed, safe-skip: {e}")
        return False


def run_single_pipeline_cycle(
    mt5_connector: MT5Connector,
    phase_observer: PhaseObserver,
    decision_pipeline: DecisionPipeline,
    trade_executor: TradeExecutor,
    config_manager: ConfigManager,
    mecano: Mecano,
    is_dry_run: bool,
    cycle_count: int,
    daily_trade_count: int,
) -> bool:
    """Exécute un cycle complet du pipeline de trading de SNIPER_X (version sans crypto + attente historique)."""
    logger = logging.getLogger(__name__)
    print(f"🔍 [PIPELINE] Cycle #{cycle_count} - Début de run_single_pipeline_cycle")
    logger.info(
        f"--- Démarrage du Cycle de Pipeline #{cycle_count} (Trades Aujourd'hui: {daily_trade_count}) ---"
    )
    trade_executed_successfully = False

    try:
        if not mt5_connector.is_connected:
            raise RuntimeError("MT5 a perdu la connexion persistante.")

        base_config = config_manager.get_current_dynamic_config()
        active_mt5_account_details = config_manager.get_mt5_account_credentials(
            mode=base_config.get("mode_execution", "DEMO").upper()
        )

        # === Construction de la liste des actifs tradables (crypto retiré) ===
        global_safety = base_config.get("global_safety", {}) or {}
        all_symbols = list(global_safety.get("global_allowed_symbols", []))

        account_allowed = set(active_mt5_account_details.get("allowed_symbols", []))
        if account_allowed:
            tradeable_assets = [a for a in all_symbols if a in account_allowed]
        else:
            tradeable_assets = all_symbols

        print(f"🎯 [PIPELINE] Assets tradables: {tradeable_assets}")

        if not tradeable_assets:
            logger.warning("Aucun actif à trader pour ce cycle. Cycle ignoré.")
            return False

        # 🔒 Gate MTF BLOQUANT — avant TOUTE collecte/signaux/scoring
        if not _mtf_readiness_gate(
            mt5_connector=mt5_connector,
            phase_observer=phase_observer,
            config_manager=config_manager,
            tradeable_assets=tradeable_assets,
            cycle_count=cycle_count,
        ):
            return False

        all_assets_market_data = {}
        all_assets_trading_signals = {}
        timeframe_str = base_config.get("data_collection", {}).get(
            "default_timeframe", "M1"
        )
        bars_to_fetch = base_config.get("data_collection", {}).get(
            "default_bars_count", 500
        )

        # Garde-fou local minimal (au‑delà du gate MTF)
        min_required_bars = 50

        for asset in tradeable_assets:
            print(f"📊 [PIPELINE] Analyse de {asset}...")
            try:
                rates_df = mt5_connector.get_rates(asset, timeframe_str, bars_to_fetch)
                if rates_df is None or rates_df.empty:
                    logger.warning(
                        f"Aucune donnée historique pour '{asset}'. Actif ignoré."
                    )
                    continue

                if len(rates_df) < min_required_bars:
                    logger.warning(
                        f"Historique insuffisant pour {asset} ({len(rates_df)} barres < {min_required_bars}). "
                        f"Trade bloqué pour cet actif."
                    )
                    continue

                symbol_info_mt5 = mt5_connector.get_symbol_info(asset)
                if symbol_info_mt5:
                    rates_df["point"] = getattr(symbol_info_mt5, "point", 0.0)
                    rates_df["spread"] = getattr(symbol_info_mt5, "spread", 0)
                    rates_df["trade_tick_size"] = getattr(
                        symbol_info_mt5, "trade_tick_size", 0.0
                    )
                    rates_df["trade_contract_size"] = getattr(
                        symbol_info_mt5, "trade_contract_size", 0.0
                    )

                annotated_rates_df = phase_observer.analyze(
                    rates_df.copy(), asset_symbol=asset
                )
                if annotated_rates_df is None or annotated_rates_df.empty:
                    continue

                latest_signals_row = annotated_rates_df.iloc[-1]
                logger.info(
                    f"[PhaseObserver] Actif: {asset} | Phase: {latest_signals_row.get('phase', 'N/A')}"
                )

                all_assets_trading_signals[asset] = _build_asset_trading_signals(
                    latest_signals_row, symbol_info_mt5
                )
                all_assets_market_data[asset] = _build_asset_market_data(
                    annotated_rates_df, symbol_info_mt5
                )

            except Exception as e:
                logger.error(
                    f"Erreur lors de la collecte de données pour l'actif '{asset}': {e}",
                    exc_info=True,
                )
                continue

        if not all_assets_trading_signals:
            logger.warning("Aucun signal valide généré pour aucun actif. Fin du cycle.")
            return False

        print("\n" + "=" * 60)
        print("🔍 TRACE COMPLÈTE DU PIPELINE:")
        print(f"1️⃣ SIGNAUX COLLECTÉS: {len(all_assets_trading_signals)} assets")
        for asset, sig in all_assets_trading_signals.items():
            print(
                f"   {asset}: phase={sig.get('phase')} conf={sig.get('confidence_score')}"
            )
        print("=" * 60)

        print(f"🌍 [PIPELINE] Construction du contexte global...")
        try:
            global_context = _build_global_context(
                mt5_connector,
                all_assets_market_data,
                all_assets_trading_signals,
                cycle_count,
                daily_trade_count,
                config_manager,
                tradeable_assets,
                active_mt5_account_details,
            )
            print(f"✅ [PIPELINE] Contexte global construit avec succès !")

            print(f"2️⃣ CONTEXT KEYS: {list(global_context.keys())}")
            print(
                f"   Account equity: {global_context.get('account_info', {}).get('equity', 'N/A')}"
            )

        except Exception as e:
            print(f"💥 [PIPELINE] ERREUR lors de la construction du contexte : {e}")
            logger.error(f"Erreur construction contexte: {e}", exc_info=True)
            return False

        print(f"🤖 [PIPELINE] Appel du decision_pipeline...")
        decision_package = decision_pipeline.institutional_decision_pipeline(
            global_context
        )

        print(f"3️⃣ DÉCISION RETOURNÉE:")
        if decision_package and "final_decision" in decision_package:
            final = decision_package["final_decision"]
            print(f"   Action: {final.get('action', 'NONE')}")
            print(f"   Asset: {final.get('asset', 'NONE')}")
            print(f"   Volume: {final.get('volume', 0)}")
            if final.get("action") in ["BUY", "SELL"]:
                print(f"   ✅ TRADE DÉCIDÉ !")
            else:
                print(f"   ❌ PAS DE TRADE")
        else:
            print("   ❌ AUCUNE DÉCISION (dict vide)")
        print("=" * 60 + "\n")

        active_config = decision_package.get("config_used", base_config)
        trade_decision = decision_package.get("final_decision", {})

        current_open_positions = trade_executor.get_open_positions()
        if current_open_positions:
            logger.info(
                f"Vérification des {len(current_open_positions)} positions ouvertes pour sortie."
            )
            exit_decisions = decision_pipeline.decide_exit_trades(
                context=global_context,
                open_positions=current_open_positions,
                active_config=active_config,
                strategy_manager_instance=decision_pipeline.strategy_manager,
            )
            if exit_decisions:
                trade_executor.execute_exit_orders(
                    exit_decisions, is_dry_run=is_dry_run
                )
                trade_executed_successfully = True

        if daily_trade_count >= active_config.get("max_trades_per_day", 999):
            logger.warning("Limite de trades quotidiens atteinte.")
            return trade_executed_successfully

        if trade_decision and trade_decision.get("action") in ["BUY", "SELL"]:
            logger.info(
                f"EXÉCUTION: {trade_decision.get('action')} {trade_decision.get('asset')}"
            )
            feedback = run_trade_execution_pipeline(trade_executor, decision_package)
            if feedback and feedback.get("status") == "executed":
                trade_executed_successfully = True
        else:
            regime = decision_package.get("context", {}).get(
                "current_market_regime", "inconnu"
            )
            logger.info(f"Aucune opportunité. Régime: {regime}.")

    except Exception as e:
        logger.error(f"Erreur pipeline: {e}", exc_info=True)
        trade_executed_successfully = False
    finally:
        logger.info(f"--- Fin du Cycle de Pipeline #{cycle_count} ---")
        return trade_executed_successfully


def main(args: argparse.Namespace) -> None:
    """
    Fonction principale pour initialiser le bot, gérer les arguments de la CLI,
    et lancer la boucle de trading infinie.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    logger = logging.getLogger(__name__)

    from core.config_manager import ConfigManager

    config_manager = ConfigManager.get_instance()

    bot_mode = (
        args.mode if args.mode else config_manager.get("mode_execution", "DEMO").upper()
    )
    is_dry_run = args.dry_run

    logger.critical(
        f"Le bot démarre en mode {'DRY RUN' if is_dry_run else bot_mode}. {'LES TRADES RÉELS SERONT EXÉCUTÉS. SOYEZ EXTRÊMEMENT PRUDENT !' if bot_mode == 'LIVE' and not is_dry_run else 'Aucun trade réel : Mode DÉMO ou DRY RUN.'}"
    )

    startup_delay_seconds = config_manager.get("app.startup_delay_seconds", 3)
    time.sleep(startup_delay_seconds)

    try:
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

        mt5_connector = MT5Connector()
        phase_observer = PhaseObserver(config_manager=config_manager)

        models_dir = config_manager.get("paths.models", "models/")
        ai_model_name_for_init = config_manager.get(
            "ai.model_name", "llama-2-7b-chat.Q4_K_M.gguf"
        )
        ai_decision_model_full_path = Path(models_dir) / ai_model_name_for_init
        ai_decision = AIDecision(
            model_path=str(ai_decision_model_full_path),
            config_manager_instance=config_manager,
        )

        trade_executor = TradeExecutor(
            config_manager=config_manager, mt5_connector=mt5_connector, mode=bot_mode
        )

        mecano = Mecano(config_manager_instance=config_manager)
        mecano.set_ai_analyzer(ai_decision)

    except Exception as e:
        logger.critical(
            f"FATAL: Erreur lors de l'initialisation des modules fondamentaux: {e}",
            exc_info=True,
        )
        sys.exit(1)

    try:
        from run_bot import (
            verify_environment_and_config,
            run_single_pipeline_cycle,
        )

        verify_environment_and_config(config_manager, mt5_connector, bot_mode)

        default_cycle_interval_from_config = config_manager.get(
            "bot_behavior.cycle_interval_seconds", 23
        )
        cycle_interval = (
            args.interval
            if args.interval is not None
            else default_cycle_interval_from_config
        )

        config_manager.send_alert(
            message=f"**SNIPER_X Bot Démarré!**\nMode: {'DRY RUN' if is_dry_run else bot_mode}\nIntervalle de Cycle: {cycle_interval}s",
            alert_type="telegram_critical",
        )

    except SystemExit:
        logger.critical(
            "Le démarrage du bot a été avorté en raison de problèmes critiques de configuration/environnement."
        )
        config_manager.send_alert(
            f"**SNIPER_X BOT N'A PAS DÉMARRÉ !**\nProblème critique lors de la vérification de l'environnement.",
            "telegram_critical",
        )
        sys.exit(1)
    except Exception as e:
        logger.critical(
            f"FATAL: Erreur non gérée lors du chargement de la configuration ou de la vérification de l'environnement: {e}",
            exc_info=True,
        )
        config_manager.send_alert(
            f"**SNIPER_X BOT S'EST ARRÊTÉ (CRASH AU DÉMARRAGE) !**\nErreur: {type(e).__name__}",
            "telegram_critical",
        )
        sys.exit(1)

    logger.info(f"SNIPER_X Bot prêt. Intervalle de cycle: {cycle_interval} secondes.")
    cycle_count = 0
    daily_trade_count = 0

    try:
        while True:
            cycle_count += 1
            cycle_start_time = time.time()

            # ⚠️ Appel corrigé: passer le DecisionPipeline (pas ai_decision)
            # Ici on crée une instance légère si nécessaire
            decision_pipeline = DecisionPipeline(
                config_manager_instance=config_manager,
                ai_interface_instance=None,
                strategy_manager_instance=None,
            )
            # Branchement (si setter indisponible, on assigne)
            if hasattr(decision_pipeline, "set_phase_observer"):
                decision_pipeline.set_phase_observer(phase_observer)
            else:
                decision_pipeline.phase_observer = phase_observer

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
                f"[PERF] Cycle de Pipeline #{cycle_count} exécuté en {cycle_duration:.2f} secondes."
            )

            config_manager.process_and_send_summary_alert(
                context={
                    "bot_mode": bot_mode,
                    "bot_status": "Running",
                    "current_market_regime": config_manager.get(
                        "current_market_regime", "N/A"
                    ),
                    "account_info": (
                        mt5_connector.get_account_info()._asdict()
                        if mt5_connector.is_connected()
                        and mt5_connector.get_account_info()
                        else {}
                    ),
                    "daily_trade_count": daily_trade_count,
                    "open_positions_count": len(trade_executor._open_positions),
                }
            )

            sleep_time = max(0, cycle_interval - cycle_duration)
            if sleep_time > 0:
                logger.info(f"Prochain cycle dans {sleep_time:.2f} secondes...")
                time.sleep(sleep_time)

    except KeyboardInterrupt:
        logger.warning(
            "\nInterruption clavier détectée (Ctrl+C). Démarrage de l'arrêt progressif..."
        )
        config_manager.send_alert(
            message="**SNIPER_X Bot Arrêté Manuellement.**",
            alert_type="telegram_critical",
        )
    except Exception as e:
        logger.critical(
            f"Une erreur critique non gérée a entraîné la terminaison de la boucle principale : {e}",
            exc_info=True,
        )
        config_manager.send_alert(
            f"**SNIPER_X BOT S'EST ARRÊTÉ (CRASH) !**\nErreur: {type(e).__name__}",
            "telegram_critical",
        )
    finally:
        if "ai_decision" in locals() and ai_decision:
            logger.info(
                "Sauvegarde de l'historique des suggestions de l'IA avant l'arrêt..."
            )
            ai_decision._save_suggestion_history()

        if "mt5_connector" in locals() and mt5_connector.is_connected():
            mt5_connector.disconnect()

        logger.info("SNIPER_X Bot est arrêté.")
        sys.exit(0)
