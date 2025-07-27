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
from datetime import UTC  # Importation correcte de UTC
from typing import Any
import pandas as pd
from dotenv import load_dotenv

load_dotenv()


# Imports des modules du projet ("Briques LEGO")

try:
    from phase_observer.phase_observer import PhaseObserver
    from core.config_manager import ConfigManager

    # run_trade_execution_pipeline est une fonction, pas une classe. Importez-la en tant que telle.
    from trader.trade_executor import TradeExecutor, run_trade_execution_pipeline
    from ai_core.ai_decision import AIDecision
    from mt5_connector import MT5Connector
    from utils.logger_setup import setup_production_logging
    from mecanique_generale.mecano import (
        Mecano,
    )  # Garder ici car utilisé par run_single_pipeline_cycle
    import MetaTrader5 as mt5  # Import pour les constantes mt5.TIMEFRAME_*
except ImportError as e:
    logging.critical(
        f"ERREUR FATALE: Échec de l'importation d'un module de SNIPER_X. Assurez-vous que l'architecture des dossiers est correcte. Erreur: {e}",
        exc_info=True,
    )
    sys.exit(1)

def load_and_verify_environment(
    config_manager: ConfigManager, mt5_connector: MT5Connector, bot_mode: str
) -> dict:
    """
    Charge la configuration principale et vérifie les composants critiques de l'environnement.
    Cette fonction est appelée une seule fois au démarrage du bot.
    Gère la sélection et la vérification du compte broker actif.

    Args:
        config_manager (ConfigManager): Une instance de ConfigManager avec la configuration chargée.
        mt5_connector (MT5Connector): Une instance de MT5Connector pour les tests de connexion MT5.
        bot_mode (str): Le mode d'exécution du bot ('DEMO' ou 'LIVE').

    Returns:
        dict: La configuration globale chargée et validée.

    Raises:
        SystemExit: Si des composants critiques sont manquants ou invalides.
        RuntimeError: Si la connexion MT5 échoue pendant la vérification initiale.
    """
    logger = logging.getLogger(__name__)  # Utilise le logger local
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

    # --- Vérification du Modèle AI (INCHANGÉ) ---
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

    # --- Sélection et Vérification du Compte MT5 Actif (CORRIGÉ) ---
    # Le but est de vérifier que la connexion est possible et de la laisser active.
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

        # Vérification proactive de la connexion MT5. Si elle échoue, le bot s'arrête.
        if not mt5_connector.connect(active_mt5_account_details):
            raise RuntimeError(
                f"La connexion initiale à MetaTrader 5 a échoué pour le compte '{active_mt5_account_details['account_id']}'. Vérifiez les identifiants et le statut du terminal."
            )

        # Le message est mis à jour pour indiquer que la connexion est maintenue.
        logger.info("Connexion MT5 vérifiée avec succès. La connexion sera maintenue.")

    except (ValueError, RuntimeError) as e:
        logger.critical(
            f"FATAL: Échec de la configuration ou de la connexion MT5 : {e}",
            exc_info=True,
        )
        sys.exit(1)
    # Le bloc 'finally' qui contenait 'mt5_connector.disconnect()' a été SUPPRIMÉ.

    # --- Vérification des identifiants Telegram (INCHANGÉ) ---
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
    """
    Fusionne la configuration globale avec la configuration spécifique à l'actif.
    Garantit que les paramètres essentiels du ConfigManager (comme le strategy_name)
    sont toujours propagés.
    """
    # CORRECTION : Accéder à load_asset_config via l'instance de ConfigLoader
    # qui est un attribut du ConfigManager.
    asset_specific_config = config_manager.config_loader.load_asset_config(asset)
    merged_config = active_config.copy()

    # S'assurer que le strategy_name de la stratégie active est toujours propagé.
    # Il est crucial pour le logging du PhaseObserver et potentiellement d'autres modules.
    if "strategy_name" in active_config:
        merged_config["strategy_name"] = active_config["strategy_name"]

    # S'assurer que la section phase_detection de la stratégie active est correctement fusionnée.
    # Elle doit être présente dans merged_config même si asset_specific_config ne l'a pas.
    if "phase_detection" in active_config:
        merged_config["phase_detection"] = {
            **merged_config.get("phase_detection", {}),
            **asset_specific_config.get("phase_detection", {}),
        }

    # Fusionner les autres sections spécifiques à l'actif.
    # Cette liste doit correspondre aux sections configurables par actif.
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


def _build_asset_trading_signals(
    latest_signals_row: pd.Series, symbol_info_mt5: Any
) -> dict:
    """
    Construit le dictionnaire de signaux pour un actif, en agrégeant les résultats du PhaseObserver
    et les informations critiques du symbole MT5.
    """
    signals = {
        "confidence_score": latest_signals_row.get("confidence_score", 0.0),
        "phase": latest_signals_row.get("phase", "unknown"),
        "volume_momentum": latest_signals_row.get("volume_momentum", 0.0),
        "nearest_liquidity_level_details": latest_signals_row.get(
            "nearest_liquidity_level_details"
        ),
        "entry_confirmation_bullish": latest_signals_row.get(
            "entry_confirmation_bullish", False
        ),
        "entry_confirmation_bearish": latest_signals_row.get(
            "entry_confirmation_bearish", False
        ),
        "validated_ob": latest_signals_row.get("validated_ob", False),
        "is_liquid": latest_signals_row.get(
            "is_liquid", True
        ),  # Maintenant calculé par PhaseObserver
        "current_price": latest_signals_row.get("close"),
        # --- AJOUTS ICI : Informations critiques du symbole MT5 ---
        "current_spread_points": symbol_info_mt5.spread if symbol_info_mt5 else 0,
        "symbol_point_value": (
            symbol_info_mt5.point if symbol_info_mt5 else 0.00001
        ),  # Valeur d'un point
        "symbol_trade_tick_size": (
            symbol_info_mt5.trade_tick_size if symbol_info_mt5 else 0.0
        ),  # Taille minimale du tick pour le trading
        "symbol_trade_contract_size": (
            symbol_info_mt5.trade_contract_size if symbol_info_mt5 else 100000
        ),  # Taille du contrat pour le calcul de lot
        # (Vous pouvez ajouter d'autres champs de symbol_info_mt5 si vos stratégies en ont besoin)
        "last_update_timestamp": (
            latest_signals_row.get("time").isoformat()
            if latest_signals_row.get("time")
            else datetime.now(UTC).isoformat()
        ),
    }

    # AJOUT/CORRECTION : Utiliser 'time' du DataFrame plutôt que 'timestamp' (qui est l'index)
    # L'index du DataFrame est 'time', et 'timestamp' n'est plus une colonne après set_index.
    # Latest_signals_row est une série, donc l'accès direct via .name (l'index) ou .get('time') est correct.

    # Assurez-vous que toutes les colonnes '_detected' et '_details' du PhaseObserver sont incluses
    # Le PhaseObserver les aura ajoutées au DataFrame, et elles seront dans latest_signals_row.
    # Cette boucle est une bonne pratique pour inclure les détails structurés.
    for col in latest_signals_row.index:
        if col.endswith("_detected") or col.endswith("_details"):
            # Vérifiez que la valeur n'est pas None avant d'assigner (pour éviter les TypeError dans le dictionnaire)
            val = latest_signals_row.get(col)
            if val is not None:
                signals[col] = val
    return signals


def _build_asset_market_data(
    annotated_rates_df: pd.DataFrame, symbol_info_mt5: Any
) -> dict:
    """
    Construit le dictionnaire de données de marché pour un actif, incluant le DataFrame annoté
    complet du PhaseObserver et les informations détaillées du symbole MT5.
    """
    latest_signals_row = annotated_rates_df.iloc[-1]
    return {
        "annotated_rates_df": annotated_rates_df,  # Le DataFrame complet est crucial pour l'IA et certaines stratégies
        "current_price": latest_signals_row.get("close"),
        "current_spread_points": symbol_info_mt5.spread if symbol_info_mt5 else 0,
        "is_liquid": latest_signals_row.get("is_liquid", True),
        # --- CORRECTION ICI : Utiliser 'time' du DataFrame annoté ---
        # `latest_signals_row` est une pd.Series dont l'index est l'horodatage ('time').
        # Il n'y a pas de colonne 'timestamp' après df.set_index('time') dans PhaseObserver.load_data.
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
        "asset_configs": {asset: cfg.load_asset_config(asset) for asset in assets},
        "active_broker_account": account,
    }


def run_single_pipeline_cycle(
    mt5_connector: MT5Connector,
    phase_observer: PhaseObserver,
    ai_decision: AIDecision,
    trade_executor: TradeExecutor,
    config_manager: ConfigManager,
    mecano: Mecano,
    is_dry_run: bool,
    cycle_count: int,
    daily_trade_count: int,
) -> bool:
    """
    Exécute un cycle complet du pipeline de trading de SNIPER_X en utilisant
    une connexion MT5 persistante. Ce cycle inclut désormais l'évaluation
    des conditions de sortie pour les positions ouvertes avant toute nouvelle entrée.

    Args:
        mt5_connector (MT5Connector): Instance du connecteur MT5.
        phase_observer (PhaseObserver): Instance de l'observateur de phases.
        ai_decision (AIDecision): Instance du module de décision IA (consultatif).
        trade_executor (TradeExecutor): Instance de l'exécuteur de trades.
        config_manager (ConfigManager): Instance du gestionnaire de configuration.
        mecano (Mecano): Instance du module Méca. Générale.
        is_dry_run (bool): Indique si le bot est en mode simulation.
        cycle_count (int): Compteur du cycle actuel.
        daily_trade_count (int): Nombre de trades effectués aujourd'hui.

    Returns:
        bool: True si un trade (entrée ou sortie) a été exécuté avec succès dans ce cycle, False sinon.
    """
    logger = logging.getLogger(__name__)
    logger.info(
        f"--- Démarrage du Cycle de Pipeline #{cycle_count} (Trades Aujourd'hui: {daily_trade_count}) ---"
    )
    trade_executed_successfully = (
        False  # Indique si une action de trading (entrée ou sortie) a eu lieu
    )

    # Injection des dépendances (pratique défensive, déjà fait dans main.py mais sécurise les tests unitaires)
    config_manager.ai_decision_instance = ai_decision
    trade_executor.config_manager = config_manager
    trade_executor.mt5_connector = mt5_connector

    try:
        # Vérification de la connexion MT5 (critique)
        if not mt5_connector.is_connected:
            logger.critical(
                "MT5 n'est pas connecté au début du cycle. Le cycle est annulé pour sécurité."
            )
            config_manager.send_alert(
                "CRITIQUE",
                "MT5 Déconnecté: Cycle Annulé.",
                alert_type="telegram_critical",
            )
            raise RuntimeError("MT5 a perdu la connexion persistante.")

        # Récupérer la configuration dynamique active pour ce cycle.
        # Contient les paramètres de la stratégie sélectionnée et les adaptations.
        # CORRECTION MAJEURE ICI : Appeler organize_pipeline_decision plus tôt pour obtenir la 'config_used'
        # qui contient déjà la stratégie sélectionnée et adaptée.
        global_context_base = _build_global_context(
            mt5_connector,
            {}, # Market data sera rempli après
            {}, # Signals seront remplis après
            cycle_count,
            daily_trade_count,
            config_manager,
            [], # Tradeable assets sera rempli après
            config_manager.get_mt5_account_credentials(mode=config_manager.get("mode_execution", "DEMO").upper()) # Obtenir les détails du compte plus tôt.
        )
        # Ceci est le cœur de la décision, la sélection de stratégie est ici.
        pipeline_output = config_manager.organize_pipeline_decision(global_context_base)
        active_config = pipeline_output.get("config_used", config_manager.get_current_dynamic_config())

        logger.info(
            f"Active Config strategy_name: {active_config.get('strategy_name', 'NON_DEFINI')}"
        )

        # Vérification de la limite de trades quotidiens (sécurité globale)
        max_trades_per_day = active_config.get("max_trades_per_day", 999)
        if daily_trade_count >= max_trades_per_day:
            logger.warning(
                f"Limite de {max_trades_per_day} trades quotidiens atteinte. Le trading est suspendu pour aujourd'hui."
            )
            config_manager.send_alert(
                "ALERTE",
                f"Limite de {max_trades_per_day} trades atteinte.",
                alert_type="telegram_info",
            )
            time.sleep(60)
            return False

        # CORRECTION ICI : Récupérer le mode d'exécution réel qui a été validé et corrigé dans main.py.
        # Ce mode est garanti d'être 'DEMO' ou 'LIVE'.
        validated_bot_mode = config_manager.get("mode_execution", "DEMO").upper() # Récupère le mode final du config_manager

        active_mt5_account_details = config_manager.get_mt5_account_credentials(
            mode=validated_bot_mode # Utilise le mode validé et corrigé.
        )
        if active_mt5_account_details is None:
            logger.critical(
                f"Aucun compte MT5 actif ou valide trouvé pour le mode '{validated_bot_mode}'. Cycle annulé."
            )
            config_manager.send_alert(
                "CRITIQUE",
                f"Compte MT5 invalide pour '{validated_bot_mode}'. Cycle annulé.",
                alert_type="telegram_critical",
            )
            return False

        # Détermination des actifs négociables pour ce cycle (Week-end vs Semaine)
        all_symbols_from_config = active_config.get("global_safety", {}).get(
            "global_allowed_symbols", []
        )
        crypto_symbols_from_config = active_config.get("global_safety", {}).get(
            "crypto_symbols", []
        )
        is_weekend = datetime.now(UTC).weekday() >= 5

        tradeable_assets = (
            crypto_symbols_from_config if is_weekend else all_symbols_from_config
        )
        tradeable_assets = [
            asset
            for asset in tradeable_assets
            if asset in active_mt5_account_details.get("allowed_symbols", [])
        ]

        logger.info(
            f"Mode {'week-end' if is_weekend else 'semaine'} activé. Trading sur actifs autorisés : {tradeable_assets}"
        )

        if not tradeable_assets:
            logger.warning("Aucun actif à trader pour ce cycle. Cycle ignoré.")
            return False

        # --- Collecte de Données de Marché et Génération des Signaux (PhaseObserver) ---
        all_assets_market_data = {}
        all_assets_trading_signals = {}
        timeframe_str = active_config.get("data_collection", {}).get(
            "default_timeframe", "M1"
        )
        bars_to_fetch = active_config.get("data_collection", {}).get(
            "default_bars_count", 200
        )

        for asset in tradeable_assets:
            try:
                # La merged_config_for_phase_observer est maintenant basée sur la STRATÉGIE ACTIVE
                merged_config_for_phase_observer = _get_merged_config_for_asset(
                    active_config, config_manager, asset
                )

                # C'est ici que l'update_parameters_from_config est appelé AVEC la config de stratégie
                phase_observer.update_parameters_from_config(
                    merged_config_for_phase_observer
                )

                symbol_info_mt5 = mt5_connector.get_symbol_info(asset)
                if symbol_info_mt5 is None:
                    logger.warning(
                        f"Informations de symbole MT5 non trouvées pour '{asset}'. Actif ignoré."
                    )
                    continue

                rates_df = mt5_connector.get_rates(asset, timeframe_str, bars_to_fetch)
                if rates_df is None or rates_df.empty:
                    logger.warning(
                        f"Aucune donnée historique récupérée pour '{asset}' ({timeframe_str}). Actif ignoré."
                    )
                    continue

                if symbol_info_mt5:
                    rates_df["spread"] = symbol_info_mt5.spread
                    rates_df["point"] = symbol_info_mt5.point
                    rates_df["trade_tick_size"] = symbol_info_mt5.trade_tick_size
                    rates_df["trade_contract_size"] = symbol_info_mt5.trade_contract_size

                if not is_weekend and _is_market_closed(rates_df, active_config):
                    logger.info(f"Marché pour {asset} semble fermé. Actif ignoré.")
                    continue

                annotated_rates_df = phase_observer.analyze(rates_df.copy())
                if annotated_rates_df is None or annotated_rates_df.empty:
                    logger.critical(
                        f"Analyse PhaseObserver a échoué ou a retourné un DataFrame vide pour '{asset}'. Actif ignoré, ou bot potentiellement arrêté par alerte CRITIQUE."
                    )
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
                    f"Erreur inattendue lors du traitement de l'actif '{asset}': {e}",
                    exc_info=True,
                )
                config_manager.send_alert(
                    "ERREUR",
                    f"Erreur traitement actif '{asset}': {e}",
                    alert_type="telegram_error",
                )
                continue

        if not all_assets_trading_signals:
            logger.warning(
                "Aucun signal valide généré pour aucun actif. Pipeline ignoré."
            )
            return False

        # Reconstruire global_context avec les données de marché et de signaux maintenant disponibles
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

        current_open_positions = trade_executor.get_open_positions()

        if current_open_positions:
            logger.info(
                f"Vérification des {len(current_open_positions)} positions ouvertes pour des opportunités de sortie."
            )

            # NOTE: decide_exit_trades utilise aussi la config_manager.decide_exit_trades
            # La active_config passée ici est la config de stratégie correcte.
            exit_decisions = config_manager.decide_exit_trades(
                context=global_context,
                open_positions=current_open_positions,
                active_config=active_config,
            )

            if exit_decisions:
                logger.info(
                    f"Le ConfigManager a recommandé {len(exit_decisions)} ordre(s) de sortie."
                )
                trade_executor.execute_exit_orders(
                    exit_decisions, is_dry_run=is_dry_run
                )
                trade_executed_successfully = True
            else:
                logger.info(
                    "Aucune opportunité de sortie de position trouvée par le ConfigManager."
                )
        else:
            logger.info("Aucune position ouverte à vérifier.")

        logger.info("Évaluation des opportunités pour de nouvelles entrées de trade.")
        # La pipeline_output est déjà obtenue plus tôt et contient la config_used correcte
        # On ne l'appelle pas une seconde fois pour la décision d'entrée.
        # On utilise directement trade_decision et config_used de pipeline_output.
        trade_decision = pipeline_output.get("final_decision", {})

        if trade_decision and trade_decision.get("action") in ["BUY", "SELL", "CLOSE"]:
            decision_package = {
                "market_context": global_context,
                "active_config": active_config, # Utilise la active_config mise à jour.
                "trade_decision": trade_decision,
            }

            logger.info(
                f"Le ConfigManager a décidé une entrée: {trade_decision.get('action')} {trade_decision.get('asset')}"
            )
            feedback = run_trade_execution_pipeline(
                trade_executor, decision_package, is_dry_run=is_dry_run
            )

            if feedback:
                trade_executed_successfully = (
                    feedback.get("execution_status") == "executed"
                )
                config_manager.feedback_on_trade_result(trade_decision, feedback)
                if trade_executed_successfully and trade_decision.get("action") in [
                    "BUY",
                    "SELL",
                ]:
                    daily_trade_count += 1
            else:
                logger.warning(
                    "L'exécution du trade d'entrée a échoué ou n'a pas retourné de feedback valide."
                )
        else:
            regime = pipeline_output.get("context", {}).get(
                "current_market_regime", "inconnu"
            )
            logger.info(
                f"Aucune opportunité de trade trouvée ce cycle. Régime de marché: {regime}."
            )

    except Exception as e:
        mecano.log_exception("Pipeline Cycle", e)
        config_manager.send_alert(
            f"CRITIQUE: Erreur dans le cycle du pipeline: {e}", "telegram_critical"
        )
        trade_executed_successfully = False
    finally:
        logger.info(f"--- Fin du Cycle de Pipeline #{cycle_count} ---")
        return trade_executed_successfully
    
def main(args: argparse.Namespace) -> None:
    """
    Fonction principale pour initialiser le bot, gérer les arguments de la CLI,
    et lancer la boucle de trading infinie.

    Args:
        args (argparse.Namespace): Les arguments parsés de la ligne de commande.
    """
    # 1. Configuration du Logging de Production (Appelé en premier)
    # L'implémentation de setup_production_logging est dans run_bot.py
    # Il est préférable que main.py n'ait pas sa propre implémentation de setup_production_logging.
    # Le logger sera configuré par `run_bot.py` lorsque `run_main_bot_logic` sera appelée (via cli.py).
    # Pour s'assurer qu'il y a un logger pour les messages initiaux de main.py, on peut le configurer
    # de manière très basique ici, qui sera ensuite surchargée.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    logger = logging.getLogger(__name__)

    # 2. Initialisation du ConfigManager
    # ConfigManager est un singleton, on récupère son instance.
    from core.config_manager import ConfigManager

    config_manager = ConfigManager.get_instance()

    # 3. Définir le mode d'exécution du bot (CLI > Config > Défaut)
    # Le mode "LIVE" ou "DEMO" de la CLI a priorité sur le mode dans prod_config.json
    bot_mode = (
        args.mode if args.mode else config_manager.get("mode_execution", "DEMO").upper()
    )
    is_dry_run = args.dry_run

    # Message de démarrage critique pour alerter l'opérateur
    logger.critical(
        f"Le bot démarre en mode {'DRY RUN' if is_dry_run else bot_mode}. {'LES TRADES RÉELS SERONT EXÉCUTÉS. SOYEZ EXTRÊMEMENT PRUDENT !' if bot_mode == 'LIVE' and not is_dry_run else 'Aucun trade réel : Mode DÉMO ou DRY RUN.'}"
    )

    startup_delay_seconds = config_manager.get("app.startup_delay_seconds", 3)
    time.sleep(startup_delay_seconds)  # Pause configurable au démarrage

    # 5. Instanciation des Modules Fondamentaux ("Briques LEGO")
    try:
        # Construire les chemins dynamiquement pour l'initialisation du ConfigManager
        config_dir_path = Path(config_manager.get("paths.configs", "config/"))
        main_config_file_name = config_manager.get(
            "paths.main_config_file_name", "prod_config.json"
        )
        config_file_path = config_dir_path / main_config_file_name

        # S'assurer que le dossier 'config' existe
        config_file_path.parent.mkdir(parents=True, exist_ok=True)

        # Initialiser/Réinitialiser la configuration dynamique avec le chemin principal
        # config_dir doit pointer vers le répertoire des stratégies
        strategy_configs_path = config_manager.get(
            "paths.strategy_configs", str(config_dir_path / "strategies")
        )

        config_manager.initialize_dynamic_config(
            template_path=str(config_file_path),
            output_path=str(
                config_file_path
            ),  # Sauve la config dynamique dans le fichier prod_config.json lui-même
            config_dir=strategy_configs_path,  # Passer le répertoire des stratégies
        )
        # global_config n'est plus nécessaire ici après initialize_dynamic_config, config_manager est la source unique.
        # global_config = config_manager.get_current_dynamic_config()

        # Instancier le connecteur MT5
        mt5_connector = MT5Connector()
        # Instancier l'observateur de phases de marché
        phase_observer = PhaseObserver(
            config_manager=config_manager
        )  # Passer config_manager à PhaseObserver
        # L'instance de PhaseObserver va charger ses paramètres depuis le config_manager

        # Construire le chemin complet du modèle AI (lu dynamiquement)
        models_dir = config_manager.get("paths.models", "models/")
        ai_model_name_for_init = config_manager.get(
            "ai.model_name", "llama-2-7b-chat.Q4_K_M.gguf"
        )
        ai_decision_model_full_path = Path(models_dir) / ai_model_name_for_init
        # Instancier le module de décision AI en lui passant ConfigManager
        ai_decision = AIDecision(
            model_path=str(ai_decision_model_full_path),
            config_manager_instance=config_manager,
        )

        # Instancier l'exécuteur de trades en lui passant ConfigManager, MT5Connector et le mode
        trade_executor = TradeExecutor(
            config_manager=config_manager, mt5_connector=mt5_connector, mode=bot_mode
        )

        # Instancier le module Mecano en lui passant ConfigManager
        mecano = Mecano(config_manager_instance=config_manager)
        # Injecter les instances de dépendances dans les modules si nécessaire
        # Mecano a besoin de l'instance d'AI_Decision pour l'analyse de rapport
        mecano.set_ai_analyzer(ai_decision)

    except Exception as e:
        logger.critical(
            f"FATAL: Erreur lors de l'initialisation des modules fondamentaux: {e}",
            exc_info=True,
        )
        sys.exit(1)

    # 6. Vérification Finale de l'Environnement et des Modules
    try:
        # Importer les fonctions nécessaires de run_bot.py
        from run_bot import (
            verify_environment_and_config,
            run_single_pipeline_cycle,
        )  # Import local des fonctions

        # La fonction verify_environment_and_config effectue des vérifications critiques
        verify_environment_and_config(config_manager, mt5_connector, bot_mode)

        # 7. Déterminer l'intervalle de cycle EFFECTIF (CLI > Config > Défaut)
        # default_cycle_interval est maintenant récupéré du config_manager
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
        elif (
            cycle_interval != default_cycle_interval_from_config
        ):  # Si la config a une valeur différente du default hardcodé
            logger.info(
                f"Utilisation de l'intervalle de cycle configuré ({cycle_interval}s) depuis le fichier de config."
            )
        else:  # Si l'argument CLI n'est pas fourni et la config est à sa valeur par défaut
            logger.info(
                f"Utilisation de l'intervalle de cycle par défaut ({default_cycle_interval_from_config}s)."
            )

        # 8. Alerte de Démarrage du Bot via Telegram
        config_manager.send_alert(
            message=f"**SNIPER_X Bot Démarré!**\nMode: {'DRY RUN' if is_dry_run else bot_mode}\nIntervalle de Cycle: {cycle_interval}s",
            alert_type="telegram_critical",
        )

    except (
        SystemExit
    ):  # Capturer SystemExit pour s'assurer que le message de crash est envoyé
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

    # Réconciliation initiale des positions ouvertes de TradeExecutor au démarrage
    # Elle doit être appelée après que MT5Connector est initialisé et potentiellement connecté une première fois
    try:
        # Re-connecter MT5 juste pour la réconciliation si nécessaire, puis déconnecter
        # C'est une vérification plus robuste que de juste vérifier is_connected()
        account_details_for_reconciliation = config_manager.get_mt5_account_credentials(
            mode=bot_mode
        )
        if account_details_for_reconciliation:
            if mt5_connector.connect(account_details_for_reconciliation):
                trade_executor.reconcile_state_with_broker()
                logger.info(
                    "Réconciliation initiale de l'état du TradeExecutor avec le broker effectuée."
                )
                mt5_connector.disconnect()  # Déconnecter après réconciliation
            else:
                logger.warning(
                    "MT5 n'a pas pu se connecter pour la réconciliation initiale du TradeExecutor. Ignorée."
                )
        else:
            logger.warning(
                "Aucun détail de compte MT5 pour la réconciliation initiale du TradeExecutor. Ignorée."
            )
    except Exception as e:
        logger.error(
            f"Échec de la réconciliation initiale du TradeExecutor: {e}", exc_info=True
        )
        config_manager.send_alert(
            f"ALERTE: Réconciliation TradeExecutor échouée: {e}", "telegram_critical"
        )

    # 9. Boucle Principale de Trading
    try:
        while True:
            cycle_count += 1
            cycle_start_time = time.time()

            # L'objet `active_mt5_account_details` est récupéré par `run_single_pipeline_cycle` maintenant
            trade_executed_in_cycle = run_single_pipeline_cycle(
                mt5_connector,
                phase_observer,
                ai_decision,
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
                    "open_positions_count": len(
                        trade_executor._open_positions
                    ),  # Nombre de positions ouvertes
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
        # S'assurer de la sauvegarde de l'historique des suggestions AI si l'objet existe
        if (
            "ai_decision" in locals() and ai_decision
        ):  # Vérifier si ai_decision n'est pas None
            logger.info(
                "Sauvegarde de l'historique des suggestions de l'IA avant l'arrêt..."
            )
            ai_decision._save_suggestion_history()

        # S'assurer de la déconnexion de MT5 si l'objet existe et est connecté
        if "mt5_connector" in locals() and mt5_connector.is_connected():
            mt5_connector.disconnect()

        logger.info("SNIPER_X Bot est arrêté.")
        sys.exit(0)
