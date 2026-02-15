#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
run_bot.py - Lanceur et Orchestrateur Principal pour le Bot SNIPER_X
"""

import argparse
import logging
import os
import sys
import json
import time
import math
import threading
import queue
import re
import signal
import platform
from datetime import datetime, timedelta, timezone
from pathlib import Path
from datetime import UTC
from typing import Any
import pandas as pd
from dotenv import load_dotenv
from typing import Any, Dict, Optional, List, Tuple
from core.diagnostics import DiagnosticTracker, get_tracker_from_context
from core.strategy_manager import StrategyManager
from phase_observer.market_analyzer import MarketAnalyzer
from phase_observer.timing_analyzer import evaluate_trading_conditions
from strategy.advanced_scoring import SimpleAdvancedScorer


load_dotenv()

try:
  
    from phase_observer.orchestrator import PhaseObserver
    from core.config_manager import ConfigManager
    from core.decision_pipeline import DecisionPipeline
    from trader.trade_executor import run_trade_execution_pipeline
    from trader.trade_executor import TradeExecutor, run_trade_execution_pipeline
    from mt5_connector import MT5Connector
    from utils.logger_setup import setup_production_logging
    from mecanique_generale.mecano import Mecano
    import MetaTrader5 as mt5
    from start_dashboard import launch_integrated_dashboard
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
        logger.info("Configuration principale déjà chargée avec succès.")
    except Exception as e:
        logger.critical(
            f"FATAL: Impossible de charger la configuration du bot. Erreur: {e}",
            exc_info=True,
        )
        sys.exit(1)

    # === IA DÉSACTIVÉE - Code commenté (27 Nov 2025) ===
    # L'IA n'est plus utilisée dans le système de décision
    # ai_model_name_from_config = config_manager.get(
    #     "ai.model_name", "llama-2-7b-chat.Q4_K_M.gguf"
    # )
    # models_dir = config_manager.get("paths.models", "models/")
    # ai_model_path_from_config = Path(models_dir) / ai_model_name_from_config
    #
    # if not ai_model_path_from_config.is_file():
    #     logger.critical(
    #         f"FATAL: Modèle IA non trouvé à '{ai_model_path_from_config}'. Veuillez télécharger le modèle GGUF. Le bot ne peut pas démarrer."
    #     )
    #     sys.exit(1)
    # logger.info(f"Modèle IA trouvé : {ai_model_path_from_config}")

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

    # Chercher le token dans telegram.bot_token OU env_vars.TELEGRAM_BOT_TOKEN
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
        logger.info("Identifiants Telegram charges.")

    logger.info(
        "Vérification de la configuration et de l'environnement terminée avec succès."
    )
    return config


def verify_environment_and_config(config_manager, mt5_connector, bot_mode):
    return load_and_verify_environment(config_manager, mt5_connector, bot_mode)


# --- Fonctions d'Aide (Helpers) pour le Cycle de Pipeline ---


def _deep_merge_dicts(base: dict, override: dict) -> dict:
    """
    Merge récursif: pour chaque clé, si les deux valeurs sont des dicts -> merge récursif,
    sinon la valeur 'override' remplace celle de 'base'.
    Les listes sont remplacées (pas concaténées) pour éviter les surprises.
    """
    from collections.abc import Mapping

    if not isinstance(base, Mapping) or not isinstance(override, Mapping):
        return override
    out = dict(base)
    for k, v in override.items():
        if k in out and isinstance(out[k], Mapping) and isinstance(v, Mapping):
            out[k] = _deep_merge_dicts(out[k], v)
        else:
            out[k] = v
    return out


def _get_merged_config_for_asset(
    active_config: dict, config_manager: ConfigManager, asset: str
) -> dict:
    """
    Fusionne la configuration globale avec la configuration spécifique à l'actif.
    - Autorise des overrides par actif pour: phase_detection, risk_management, exit_policy, etc.
    - Merge récursif (deep) pour éviter d'écraser des sous-champs par inadvertance.
    """
    asset_specific_config = config_manager.config_loader.load_asset_config(asset) or {}
    merged_config = dict(active_config or {})
    merged_config["asset_symbol"] = asset  # pratique pour les logs/pipelines

    # Strategy name:
    # - si tu veux autoriser une stratégie différente par actif, dé-commente la ligne suivante
    # if "strategy_name" in asset_specific_config:
    #     merged_config["strategy_name"] = asset_specific_config["strategy_name"]
    # Sinon on garde la logique actuelle (priorité au global) :
    if "strategy_name" in active_config:
        merged_config["strategy_name"] = active_config["strategy_name"]

    # Sections à merger (tu peux en ajouter/retirer selon tes fichiers d'assets)
    sections_to_merge = [
        "phase_detection",
        "volatility",
        "risk_management",
        "smart_targets",
        "temporal_context",
        "institutional_bias",
        "weighting",
        "strategy_toggles",
        "exit_policy",  # <-- important pour tes sorties fallback / BE / trailing
        "trade_limits",
        "data_collection",
        "broker_overrides",
        "position_management",
        "entry_rules",  # <-- CRITIQUE: permet de fusionner les overrides SL/TP par asset
    ]

    for section in sections_to_merge:
        asset_section = asset_specific_config.get(section)
        if asset_section is not None:
            merged_section = _deep_merge_dicts(
                merged_config.get(section, {}), asset_section
            )
            merged_config[section] = merged_section

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
    latest_signals_row: pd.Series,
    symbol_info_mt5: Any,
    asset: str = None,
    mt5_connector: Any = None,
) -> dict:
    """
    Construit le dictionnaire de signaux pour un actif donné en conservant
    TOUTES les colonnes du PhaseObserver et en ajoutant:
      - Spread robuste en points (via broker ou (ask-bid)/point)
      - Micro-timing M1: break HH/LL & écart EMA20/EMA50
    """
    # 1) Tout le contenu PhaseObserver
    if isinstance(latest_signals_row, dict):
        signals = latest_signals_row.copy()
    elif hasattr(latest_signals_row, "to_dict"):
        signals = latest_signals_row.to_dict()
    else:
        raise TypeError(
            "latest_signals_row doit être un dict ou un Series convertible en dict"
        )

    # 2) Infos broker de base
    signals["current_price"] = latest_signals_row.get("close")
    signals["spread"] = (
        getattr(symbol_info_mt5, "spread", float("inf"))
        if symbol_info_mt5
        else float("inf")
    )
    signals["symbol_point_value"] = (
        getattr(symbol_info_mt5, "point", 0.00001) if symbol_info_mt5 else 0.00001
    )
    signals["symbol_trade_contract_size"] = (
        getattr(symbol_info_mt5, "trade_contract_size", 100000.0)
        if symbol_info_mt5
        else 100000.0
    )

    # 3) Timestamp propre
    if hasattr(latest_signals_row, "name") and hasattr(
        latest_signals_row.name, "isoformat"
    ):
        signals["last_update_timestamp"] = latest_signals_row.name.isoformat()
    else:
        from datetime import datetime, UTC

        signals["last_update_timestamp"] = datetime.now(UTC).isoformat()

    # 4) Spread robuste (en points)
    current_spread_points = None
    if isinstance(signals.get("spread"), (int, float)) and float(signals["spread"]) > 0:
        current_spread_points = float(signals["spread"])
    elif mt5_connector and asset:
        current_spread_points = float(mt5_connector.get_symbol_spread_points(asset))
    signals["current_spread_points"] = (
        current_spread_points if current_spread_points is not None else float("inf")
    )

    # 5) Micro-structure M1 uniquement (PAS de fallback MTF SMA/EMA)
    if mt5_connector and asset:
        import numpy as np

        def _ema(x: np.ndarray, n: int) -> np.ndarray:
            k = 2 / (n + 1.0)
            ema = np.empty_like(x, dtype=float)
            ema[0] = x[0]
            for i in range(1, len(x)):
                ema[i] = k * x[i] + (1 - k) * ema[i - 1]
            return ema

        m1 = mt5_connector.get_rates(asset, "M1", 200)
        if m1 is not None and not m1.empty:
            e20_1 = _ema(m1["close"].to_numpy(), 20)[-1]
            e50_1 = _ema(m1["close"].to_numpy(), 50)[-1]
            signals["m1_ema_spread"] = abs(float(e20_1 - e50_1))
            # Break du plus haut/bas des 10 dernières barres
            hh = m1["high"].rolling(10).max()
            ll = m1["low"].rolling(10).min()
            signals["m1_last_hh_break"] = bool(m1["close"].iloc[-1] > hh.iloc[-2])
            signals["m1_last_ll_break"] = bool(m1["close"].iloc[-1] < ll.iloc[-2])
            # Optionnel: ratio volume tick récent vs moyenne
            if "tick_volume" in m1.columns and len(m1) >= 21:
                tv = m1["tick_volume"].to_numpy()
                signals["volume_zscore"] = float(
                    (tv[-1] - tv[-21:-1].mean()) / (tv[-21:-1].std() + 1e-9)
                )

    return signals


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


def _load_po_config_safe(config_manager):
    """
    Lecture directe et tolérante de config/phase_observer_config.json.
    Si quelque chose foire, on renvoie {} pour NE PAS BLOQUER le pipeline.
    """
    try:
        cfg_dir = Path(config_manager.get("paths.configs", "config"))
        po_path = cfg_dir / "phase_observer_config.json"
        with open(po_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _execute_single_decision(
    td,
    trade_executor,
    mt5_connector,
    global_context,
    decision_package,
    execution_mode,
    logger,
):
    """
    Exécute UNE décision (scalping ou liquidity).

    Mode burst_scalping => single_master :
      - AUCUN split par burst_size (pas de boucle)
      - sizing_scope="BASKET" (lot = risk% / burst_size côté prepare_order)
      - TP désactivé (trailing only)
      - Une seule préparation + exécution
    """
    try:
        # --- Sanity side/action ---
        side = str(td.get("action") or td.get("side") or "").upper().strip()
        if side not in {"BUY", "SELL"}:
            logger.debug(f"[EXECUTOR] décision ignorée (side invalide): {td}")
            return False

        # --- Paquet standard pour l'exécuteur (sera ajusté plus bas si burst) ---
        # ✅ FIX (02 JAN 2026): Utiliser decision_package["market_context"] au lieu de global_context
        # pour préserver active_broker_account du worker thread (nécessaire pour risk_per_trade_percent)
        base_pkg = {
            "active_config": decision_package.get("config_used", {}) or {},
            "market_context": decision_package.get("market_context", global_context),
        }

        # --- Détection (et normalisation d'alias) du mode burst ---
        rn = str(td.get("rule_name", "")).lower().strip()
        # Normalisation des anciens alias historiques vers burst_scalping
        if rn in {
            "burst",
            "burst_master",
            "scalping_burst",
            "burst_single",
            "",
        }:
            rn = "burst_scalping"
        is_burst = rn == "burst_scalping" or bool(td.get("burst_enabled", False))

        if is_burst:
            # ===== SINGLE MASTER =====
            # 1) Résolution burst_size (decision -> config -> défaut)
            def _resolve_burst(d, conf):
                try:
                    if d.get("burst_size") is not None:
                        return int(d.get("burst_size"))
                    if d.get("burst_count") is not None:
                        return int(d.get("burst_count"))
                except Exception:
                    pass
                try:
                    return int(
                        (
                            ((conf or {}).get("entry_rules", {}) or {}).get(
                                "scalping", {}
                            )
                            or {}
                        )
                        .get("burst_scalping", {})
                        .get("burst_size", 1)
                    )
                except Exception:
                    return 1

            resolved_burst = _resolve_burst(td, base_pkg["active_config"])
            if resolved_burst < 1:
                resolved_burst = 1

            # 2) Nettoyage des champs hérités LIMIT_FOK / lot par enfant
            td = dict(td)  # on travaille sur une copie
            td["rule_name"] = "burst_scalping"
            td.pop("entry_style", None)  # pas de LIMIT_FOK
            td.pop("burst_volume_each", None)  # pas de lot figé par enfant

            # 3) Paramètres single_master compréhensibles par prepare_order
            td["burst_size"] = int(resolved_burst)
            td["sizing_scope"] = "BASKET"  # lot = risk% / burst_size côté sizing
            td["no_tp"] = True  # trailing only (TP supprimé)

            # 4) Optionnel : attacher un basket_id / métadonnées (si dispo)
            try:
                if hasattr(trade_executor, "_attach_burst_metadata"):
                    td = trade_executor._attach_burst_metadata(td) or td
            except Exception as e:
                logger.debug(f"[EXECUTOR] _attach_burst_metadata: {e}")

            # 5) Préparation & exécution (UNE seule requête)
            order_request = trade_executor.prepare_order(
                {
                    "trade_decision": td,
                    **base_pkg,
                }
            )
            exec_res = trade_executor.execute_order(order_request)
            ok = str(exec_res.get("status", "")).lower() in {"filled", "placed"}
            logger.info(
                f"[BURST single_master] {side} {td.get('asset')} "
                f"| burst_size={resolved_burst} | status={exec_res.get('status')}"
            )
            return ok

        # ===== Non-burst : chemin standard =====
        order_request = trade_executor.prepare_order(
            {
                "trade_decision": td,
                **base_pkg,
            }
        )
        exec_res = trade_executor.execute_order(order_request)
        return str(exec_res.get("status", "")).lower() in {"filled", "placed"}

    except Exception as e:
        logger.error(
            f"[EXECUTOR] Erreur prepare/execute pour {td.get('asset')}: {e}",
            exc_info=True,
        )
        return False


def run_single_pipeline_cycle(
    mt5_connector: MT5Connector,
    decision_pipeline: DecisionPipeline,
    trade_executor: TradeExecutor,
    config_manager: ConfigManager,
    mecano: Mecano,
    strategy_manager: StrategyManager,
    is_dry_run: bool,
    cycle_count: int,
    daily_trade_count: int,
    excluded_symbols: Optional[List[str]] = None,
) -> bool:
    """
    Exécute un cycle complet du pipeline de trading de SNIPER_X.

    Architecture (12 FEV 2026):
      - MarketAnalyzer (PhaseObserver + PatternEngine) → annotated_df + latest
      - Signals consolidés par actif
      - decision_pipeline (institutional_decision_pipeline) pour scalping + liquidity
      - SLTP dynamique (trailing-only pour scalping), maintenance périodique
      - Note: FusionManager/fast-lane supprimés — scalping_worker threads gèrent tout
    """
    import logging, pandas as pd  # noqa

    # --- Safe import diag tracker ---
    try:
        from core.diagnostics import get_tracker_from_context
    except Exception:

        def get_tracker_from_context(_):
            class _N:
                def emit_summary(self, *_args, **_kwargs): ...

            return _N()

    logger = logging.getLogger(__name__)
    trade_executed_successfully = False
    global_context: Dict[str, Any] = {}

    # === Moteurs d'analyse ===
    market_analyzer = MarketAnalyzer(config_manager=config_manager, logger=logger)

    # 12 FEV 2026: FusionManager supprimé — les scalping_worker threads gèrent tout

    # === Préparation exécution ===
    try:
        if not getattr(mt5_connector, "is_connected", False):
            raise RuntimeError("MT5 a perdu la connexion persistante.")

        base_config = config_manager.get_current_dynamic_config()
        execution_mode = str(base_config.get("mode_execution", "DEMO")).upper()

        logger.info(
            f"[EXEC MODE] is_dry_run={bool(base_config.get('trade_execution', {}).get('dry_run', is_dry_run))} | execution_mode={execution_mode}"
        )
        is_dry_run = bool(
            base_config.get("trade_execution", {}).get("dry_run", is_dry_run)
        )

        # Snapshot burst_size (cycle 1)
        if cycle_count == 1:

            def _dig(d, path):
                cur = d or {}
                for k in path:
                    if not isinstance(cur, dict):
                        return None
                    cur = cur.get(k)
                return cur

            g_burst = _dig(
                base_config, ["entry_rules", "scalping", "burst_scalping", "burst_size"]
            )
            try:
                strat_conf = strategy_manager.get_strategy_config("scalping") or {}
                s_burst = _dig(
                    strat_conf,
                    ["entry_rules", "scalping", "burst_scalping", "burst_size"],
                )
            except Exception:
                s_burst = None
            try:
                xa = config_manager.load_asset_config("USDJPY") or {}
            except Exception:
                xa = {}
            xa_entry = _dig(
                xa, ["entry_rules", "scalping", "burst_scalping", "burst_size"]
            )
            xa_override = _dig(
                xa,
                [
                    "overrides",
                    "scalping",
                    "entry_rules",
                    "scalping",
                    "burst_scalping",
                    "burst_size",
                ],
            )
            xa_legacy = _dig(xa, ["overrides", "scalping", "burst", "burst_size"])
            logger.critical(
                f"[CFG@BOOT] burst_size global={g_burst} | strategy={s_burst} | USDJPY.entry={xa_entry} | USDJPY.override={xa_override} | USDJPY.legacy={xa_legacy}"
            )

        active_mt5_account_details = config_manager.get_mt5_account_credentials(
            mode=execution_mode
        )

        # === [CANDLES SWITCH SUPPRIMÉ - Session 23 Nov 2025] ===
        # Variable CANDLES_ENABLED retirée (détecteurs patterns/bougies supprimés)
        CANDLES_ENABLED = False  # Désactivé définitivement

        # Assets autorisés
        global_safety = base_config.get("global_safety", {}) or {}
        all_symbols = list(global_safety.get("global_allowed_symbols", []))
        account_allowed = set(
            (active_mt5_account_details or {}).get("allowed_symbols", [])
        )
        tradeable_assets = (
            [a for a in all_symbols if a in account_allowed]
            if account_allowed
            else all_symbols
        )

        # ✅ PHASE 1: Filtrage symboles exclus (pour LIQUIDITY Thread)
        if excluded_symbols:
            excluded_upper = {s.upper() for s in excluded_symbols}
            tradeable_assets = [
                a for a in tradeable_assets if a.upper() not in excluded_upper
            ]
            if excluded_symbols:
                logger.info(f"🔒 [PIPELINE] Symboles exclus: {excluded_symbols}")

        logger.info(f"🎯 [PIPELINE] Assets tradables: {tradeable_assets}")
        if not tradeable_assets:
            logger.warning("Aucun actif à trader pour ce cycle. Cycle ignoré.")
            return False

        # Collecte/Analyse
        all_assets_market_data: Dict[str, pd.DataFrame] = {}
        all_assets_trading_signals: Dict[str, Dict[str, Any]] = {}

        dcfg = base_config.get("data_collection", {}) or {}
        timeframe_str = dcfg.get("default_timeframe", "M1")
        bars_to_fetch = int(dcfg.get("default_bars_count", 200))  # ✅ RÉDUIT: 500→200 (inutile d'analyser autant)
        min_required_bars = 50

        for asset in tradeable_assets:
            logger.debug(f"📊 [PIPELINE] Analyse de {asset}...")
            try:
                rates_df = mt5_connector.get_rates(asset, timeframe_str, bars_to_fetch)
                if (
                    (rates_df is None)
                    or rates_df.empty
                    or len(rates_df) < min_required_bars
                ):
                    logger.warning(f"Données insuffisantes pour {asset}. Actif ignoré.")
                    continue

                symbol_info_mt5 = mt5_connector.get_symbol_info(asset)
                if symbol_info_mt5:
                    rates_df["point"] = getattr(symbol_info_mt5, "point", 0.0)
                    rates_df["spread"] = getattr(symbol_info_mt5, "spread", 0)

                # Seed history
                if (
                    market_analyzer.phase_observer._history_df is None
                ) or market_analyzer.phase_observer._history_df.empty:
                    market_analyzer.phase_observer.load_initial_history(rates_df.copy())

                # Rolling vs full refresh
                rolling_lookback = int(dcfg.get("rolling_lookback_bars", 50))
                full_refresh_bars = int(dcfg.get("full_refresh_bars", 200))
                recalib_n = int(dcfg.get("recalibration_every_n_cycles", 10))
                do_full_refresh = (cycle_count == 1) or (
                    recalib_n > 0 and cycle_count % recalib_n == 0
                )
                subset_df = rates_df.tail(
                    full_refresh_bars if do_full_refresh else rolling_lookback
                ).copy()

                # 🎯 Récupération des ticks MT5 pour footprint M1
                # 🔧 FIX (20 DEC 2025): Récupérer ticks UNIQUEMENT pour USDJPY (scalping)
                # EURUSD/GBPUSD utilisent la stratégie liquidité (pas de footprint)
                ticks_df = None

                # ✅ FILTRE: Ticks uniquement pour USDJPY (stratégie scalping)
                if asset.upper() == "USDJPY":
                    logger.info(f"[TICKS][DEBUG] Tentative récupération ticks pour {asset}...")
                    try:
                        # Identifier la dernière bougie M1 (en cours)
                        if subset_df is not None and not subset_df.empty:
                            # 🎯 (05 JAN 2026): FENÊTRE GLISSANTE pour scalping sniper (<10s)
                            # Problème: Fenêtre M1 (60s) donne scores identiques pendant 24 cycles (60s / 2.5s)
                            # Solution: Charger derniers N secondes de ticks en temps réel

                            # 🔪 SCALPING SNIPER: Fenêtre ultra-courte 8s pour analyse au scalpel
                            # 8s = assez de ticks pour statistiques + assez rapide pour retournements
                            sliding_window_seconds = 8  # 8s pour scalping sniper (vs 20s ancien)

                            # Calculer fenêtre glissante [NOW - Ns → NOW]
                            now_utc = pd.Timestamp.utcnow()
                            tick_window_start = now_utc - pd.Timedelta(seconds=sliding_window_seconds)
                            tick_window_end = now_utc

                            logger.info(
                                f"[{asset}] 🔄 Chargement ticks [fenêtre glissante {sliding_window_seconds}s] | "
                                f"[{tick_window_start.strftime('%H:%M:%S')} → {tick_window_end.strftime('%H:%M:%S')}]"
                            )

                            # Récupérer les ticks de la fenêtre glissante (non verrouillée à 60s)
                            # Note: get_ticks_for_candle() accepte n'importe quelle fenêtre (le verrou 60s sera contourné)
                            try:
                                ticks_df = mt5_connector.get_ticks_for_candle(
                                    asset,
                                    tick_window_start.to_pydatetime(),
                                    tick_window_end.to_pydatetime()
                                )
                            except Exception as e_ticks:
                                logger.error(f"[{asset}] Erreur chargement ticks fenêtre glissante: {e_ticks}")
                                ticks_df = None

                            if ticks_df is not None and not ticks_df.empty:
                                logger.info(
                                    f"[{asset}] ✅ {len(ticks_df)} ticks chargés (fenêtre glissante {sliding_window_seconds}s)"
                                )
                            else:
                                logger.warning(
                                    f"[{asset}] ⚠️ Aucun tick dans la fenêtre glissante {sliding_window_seconds}s"
                                )
                        else:
                            logger.warning(f"[{asset}] ⚠️ subset_df vide, fenêtre glissante non calculée")
                    except Exception as e:
                        logger.error(f"[TICKS] ❌ Erreur récupération ticks {asset}: {e}")
                else:
                    # EURUSD/GBPUSD : Pas de ticks (stratégie liquidité)
                    logger.info(f"[TICKS] ✅ {asset} utilise stratégie liquidité → Ticks skip")

                market_results = market_analyzer.analyze(asset, subset_df, ticks=ticks_df)

                # === [PURGE PATTERNS SUPPRIMÉ - Session 23 Nov 2025] ===
                # Bloc purge patterns retiré (CANDLES_ENABLED toujours False)
                # market_results ne contient plus de patterns (détecteurs supprimés)

                if cycle_count == 1 or do_full_refresh:
                    market_analyzer.phase_observer.load_initial_history(
                        subset_df.copy()
                    )
                elif hasattr(market_analyzer.phase_observer, "update_with_new_data"):
                    market_analyzer.phase_observer.update_with_new_data(
                        subset_df.copy()
                    )

                logger.debug(
                    f"[{asset}] MarketAnalyzer → keys={list(market_results.keys())}"
                )

                annotated_rates_df = market_results["annotated_df"]
                if annotated_rates_df is None or annotated_rates_df.empty:
                    logger.warning(f"[{asset}] MarketAnalyzer → DF vide. Actif ignoré.")
                    continue

                latest = market_results["latest"]
                logger.info(
                    f"[MarketAnalyzer] Actif: {asset} | Phase: {market_results.get('phase', 'N/A')}"
                )

                # === [LOG 200 BOUGIES SUPPRIMÉ - Session 23 Nov 2025] ===
                # Bloc logging bull/bear candles retiré (30 lignes)
                # Ce log était conditionné par CANDLES_ENABLED (désactivé définitivement)

                # === FOOTPRINT ANALYSE (bougie M1 clôturée, + option live si indispo) ===
                try:
                    # 1) Bougie clôturée prioritaire (évite coverage partiel ~5s)
                    use_idx = -2 if len(annotated_rates_df) >= 2 else -1
                    candle_row = annotated_rates_df.iloc[use_idx]

                    if "time" in annotated_rates_df.columns:
                        start_ts = pd.to_datetime(
                            candle_row["time"], utc=True, errors="coerce"
                        )
                    else:
                        start_ts = pd.to_datetime(
                            candle_row.name, utc=True, errors="coerce"
                        )
                    if pd.isna(start_ts):
                        start_ts = pd.Timestamp.utcnow()
                    end_ts = start_ts + pd.Timedelta(minutes=1)

                    ticks_df = mt5_connector.get_ticks_for_candle(
                        asset, start_ts.to_pydatetime(), end_ts.to_pydatetime()
                    )

                    # Fallback live (si l'historique close est vide)
                    use_idx = -1
                    if ticks_df is None or ticks_df.empty:
                        last_candle = annotated_rates_df.iloc[-1]
                        if "time" in annotated_rates_df.columns:
                            live_start = pd.to_datetime(
                                last_candle["time"], utc=True, errors="coerce"
                            )
                        else:
                            live_start = pd.to_datetime(
                                last_candle.name, utc=True, errors="coerce"
                            )
                        if pd.isna(live_start):
                            live_start = pd.Timestamp.utcnow()
                        live_end = live_start + pd.Timedelta(minutes=1)
                        ticks_df = mt5_connector.get_ticks_for_candle(
                            asset, live_start.to_pydatetime(), live_end.to_pydatetime()
                        )

                    if "time" in annotated_rates_df.columns:
                        annotated_rates_df["time"] = pd.to_datetime(
                            annotated_rates_df["time"], utc=True, errors="coerce"
                        )

                    # ❌ DÉSACTIVÉ (25 DEC 2025): Footprint supprimé - Architecture minimaliste
                    # Ancien code footprint_validator() supprimé (52 lignes)

                except Exception as e:
                    logger.error(
                        f"Erreur collecte données {asset}: {e}", exc_info=True
                    )

                # === ORDERFLOW v6 ANALYSE (5 dernières bougies) ===
                try:

                    last_candles_df = annotated_rates_df.tail(5).copy()
                    if "time" in last_candles_df.columns:
                        last_candles_df["time"] = pd.to_datetime(
                            last_candles_df["time"], utc=True, errors="coerce"
                        )
                        last_candles_df.set_index("time", inplace=True)

                    # === [ORDERFLOW V6 DÉSACTIVÉ - 26 Déc 2025] ===
                    # ❌ SUPPRIMÉ: OrderFlow V6 ne doit PAS être calculé pour EURUSD/GBPUSD
                    # Ces assets utilisent ScalpingStrategy :
                    # - Sweeps de liquidité
                    # - EQH/EQL
                    # - Order Blocks
                    # - FVG
                    # - BOS/MSS
                    # - Absorption
                    # OrderFlow V6 est réservé à USDJPY (ScalpingStrategy) uniquement.
                    latest = dict(latest)

                except Exception as e:
                    logger.error(
                        f"[LIQUIDITY][{asset}] Erreur traitement latest: {e}",
                        exc_info=True,
                    )

                # === Signals unifiés ===
                signals: Dict[str, Any] = (
                    _build_asset_trading_signals(
                        latest,
                        symbol_info_mt5,
                        asset=asset,
                        mt5_connector=mt5_connector,
                    )
                    or {}
                )

                signals["__latest__"] = latest  # pour diag WHY_NO_TRADE

                # === [UPDATE PATTERNS SUPPRIMÉ - Session 23 Nov 2025] ===
                # Bloc signals.update(patterns) retiré (CANDLES_ENABLED=False)
                # market_results ne contient plus de patterns

                signals["phase"] = market_results.get(
                    "phase", signals.get("phase", "neutral")
                )
                signals["confidence_score"] = market_results.get(
                    "confidence", signals.get("confidence_score", 0.5)
                )
                signals["structure"] = market_results.get("structure", {})

                spread_pts = (
                    getattr(symbol_info_mt5, "spread", None)
                    if symbol_info_mt5
                    else None
                )
                if not spread_pts or spread_pts <= 0:
                    # Fallback robuste depuis le tick live
                    try:
                        tk = mt5_connector.get_symbol_tick(asset)
                        ask = (
                            tk.get("ask")
                            if isinstance(tk, dict)
                            else getattr(tk, "ask", None)
                        )
                        bid = (
                            tk.get("bid")
                            if isinstance(tk, dict)
                            else getattr(tk, "bid", None)
                        )
                        pt = (
                            getattr(symbol_info_mt5, "point", 0.0)
                            if symbol_info_mt5
                            else 0.0
                        )
                        if ask and bid and pt:
                            spread_pts = abs(float(ask) - float(bid)) / float(pt)
                        else:
                            spread_pts = None
                    except Exception:
                        spread_pts = None
                signals["current_spread_points"] = (
                    float(spread_pts)
                    if isinstance(spread_pts, (int, float))
                    else float("nan")
                )

                # exposer FP/OF
                signals["footprint_summary"] = latest.get("footprint_summary")
                signals["orderflow_summary"] = latest.get("orderflow_summary")

                # Persist
                all_assets_trading_signals[asset] = signals
                all_assets_market_data[asset] = _build_asset_market_data(
                    annotated_rates_df, symbol_info_mt5
                )

            except Exception as e:
                logger.error(f"Erreur collecte données {asset}: {e}", exc_info=True)
                continue

        if not all_assets_trading_signals:
            logger.warning("Aucun signal valide généré. Fin du cycle.")
            return False

        # === Contexte global ===
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
        global_context["diag_tracker"] = get_tracker_from_context(global_context)
        logger.debug("✅ [PIPELINE] Contexte global construit avec succès !")
        logger.debug(f"2️⃣ CONTEXT KEYS: {list(global_context.keys())}")

        # === Gestion fermetures SL/TP des paniers (sécurité) ===
        try:
            # Fusionner config scalping pour avoir entry_rules.closure_rules
            scalping_config = strategy_manager.get_strategy_config("scalping") or {}
            merged_config = dict(base_config)
            if "entry_rules" in scalping_config:
                merged_config.setdefault("entry_rules", {}).update(
                    scalping_config["entry_rules"]
                )

            closure_cfg = (
                merged_config.get("entry_rules", {})
                .get("scalping", {})
                .get("burst_scalping", {})
                .get("closure_rules", {})
                or {}
            )

            # Log pour debug : vérifier que max_loss_pips est bien transmis
            logger.info(f"🔍 [BURST_EXIT][1ST_CALL] Config lue:")
            logger.info(f"   • enable_loss_guard: {closure_cfg.get('enable_loss_guard')}")
            logger.info(f"   • max_loss_pips: {closure_cfg.get('max_loss_pips')} pips")
            logger.info(f"   • enabled: {closure_cfg.get('enabled')}")

            # 🔍 DEBUG: Vérifier ce que contient merged_config
            test_path = (
                merged_config.get("entry_rules", {})
                .get("scalping", {})
                .get("burst_scalping", {})
                .get("closure_rules", {})
            )
            logger.critical(f"🔍 [BURST_EXIT][1ST_CALL] merged_config path test:")
            logger.critical(f"   • entry_rules exists: {'entry_rules' in merged_config}")
            logger.critical(f"   • scalping exists: {'scalping' in merged_config.get('entry_rules', {})}")
            logger.critical(f"   • burst_scalping exists: {'burst_scalping' in merged_config.get('entry_rules', {}).get('scalping', {})}")
            logger.critical(f"   • closure_rules exists: {'closure_rules' in merged_config.get('entry_rules', {}).get('scalping', {}).get('burst_scalping', {})}")
            logger.critical(f"   • test_path.get('enabled'): {test_path.get('enabled')}")

            trade_executor.monitor_burst_baskets(
                config=merged_config,
                max_loss_pips=float(closure_cfg.get("max_loss_pips", 90.0)),  # Fallback cohérent avec config
            )
        except Exception as e:
            logger.error(f"❌ [BURST EXIT] Erreur CRITIQUE: {e}", exc_info=True)

        # === Pipeline institutionnel (peut produire Liquidity, etc.) ===
        logger.debug("🤖 [PIPELINE] Appel du decision_pipeline...")
        decision_package = (
            decision_pipeline.institutional_decision_pipeline(global_context) or {}
        )

        # === Maintenance périodique SLTP dynamique ===
        # 📊 Surveillance et fermeture automatique des baskets au profit cible
        # Surveille en temps réel (100ms/check) et ferme dès que PnL >= target_profit_pips (défaut: 15 pips)
        try:
            # Fusionner config scalping pour avoir entry_rules.closure_rules
            scalping_config = strategy_manager.get_strategy_config("scalping") or {}
            merged_config = dict(base_config)
            if "entry_rules" in scalping_config:
                merged_config.setdefault("entry_rules", {}).update(
                    scalping_config["entry_rules"]
                )

            # Lire closure_cfg pour logs explicites
            closure_cfg = (
                merged_config.get("entry_rules", {})
                .get("scalping", {})
                .get("burst_scalping", {})
                .get("closure_rules", {})
                or {}
            )

            # Log pour debug : vérifier que max_loss_pips est bien transmis
            logger.info(f"🔍 [BURST_MONITOR][2ND_CALL] Config lue:")
            logger.info(f"   • enable_loss_guard: {closure_cfg.get('enable_loss_guard')}")
            logger.info(f"   • max_loss_pips: {closure_cfg.get('max_loss_pips')} pips")
            logger.info(f"   • enabled: {closure_cfg.get('enabled')}")

            # 🔍 DEBUG: Vérifier ce que contient merged_config
            test_path = (
                merged_config.get("entry_rules", {})
                .get("scalping", {})
                .get("burst_scalping", {})
                .get("closure_rules", {})
            )
            logger.critical(f"🔍 [BURST_MONITOR][2ND_CALL] merged_config path test:")
            logger.critical(f"   • entry_rules exists: {'entry_rules' in merged_config}")
            logger.critical(f"   • scalping exists: {'scalping' in merged_config.get('entry_rules', {})}")
            logger.critical(f"   • burst_scalping exists: {'burst_scalping' in merged_config.get('entry_rules', {}).get('scalping', {})}")
            logger.critical(f"   • closure_rules exists: {'closure_rules' in merged_config.get('entry_rules', {}).get('scalping', {}).get('burst_scalping', {})}")
            logger.critical(f"   • test_path.get('enabled'): {test_path.get('enabled')}")

            trade_executor.monitor_burst_baskets(
                config=merged_config
            )
        except Exception as e:
            logger.error(f"❌ [BURST_MONITOR] Erreur CRITIQUE: {e}", exc_info=True)

        # === Logs décisions ===
        scalping_decisions = decision_package.get("scalping_decisions", []) or []
        liquidity_decisions = decision_package.get("liquidity_decisions", []) or []
        final_decisions = scalping_decisions + liquidity_decisions
        final = decision_package.get("final_decision", {}) or {}
        ctx_out = decision_package.get("context", {}) or {}

        if final_decisions:
            logger.debug("📦 [PIPELINE] Décisions multiples détectées:")
            for d in final_decisions:
                logger.debug(
                    f"   → {d.get('action')} {d.get('asset')} | vol={d.get('volume', 0)}"
                )
        else:
            logger.debug("📦 [PIPELINE] Aucune décision multiple détectée.")

        if not ctx_out.get("__decision_logged"):
            logger.debug("3️⃣ DÉCISION RETOURNÉE:")
            logger.debug(f"   Action: {final.get('action', 'NONE')}")
            logger.debug(f"   Asset: {final.get('asset', 'NONE')}")
            logger.debug(f"   Volume: {final.get('volume', 0)}")
            logger.debug(
                "   ✅ TRADE DÉCIDÉ !"
                if str(final.get("action", "")).upper() in {"BUY", "SELL"}
                else "   ❌ PAS DE TRADE"
            )
            logger.debug("=" * 60 + "\n")

        # === Filtre scalping: FUSION-ONLY + USDJPY + fusion_data valide ===
        _all_scalping = decision_package.get("scalping_decisions") or []
        for d in _all_scalping:
            logger.info(f"[FILTER_DEBUG] Decision: rule={d.get('rule_name')} asset={d.get('asset')} has_fusion_data={bool(d.get('fusion_data'))} fused_conf={d.get('fusion_data', {}).get('fused_confidence', 0.0)}")

        scalping_decisions = [
            d
            for d in _all_scalping
            if str(d.get("rule_name", "")).lower() in ("fusion_scalping", "burst_scalping")
            and str(d.get("asset", "")).upper() == "USDJPY"
            # ✅ BLOQUEUR CRITIQUE: Rejeter si fusion_data vide (pattern désactivé)
            and d.get("fusion_data")  # fusion_data doit exister et ne pas être vide
            and d.get("fusion_data", {}).get("fused_confidence", 0.0) > 0.0  # score > 0%
        ]

        # === Aucune décision ce cycle ===
        if not scalping_decisions and not liquidity_decisions:
            logger.debug("📦 [PIPELINE] Aucune décision détectée.")
            logger.info("Aucun trade décidé ce cycle.")
            return False

        # === Helper: résolution robuste du burst_size ===
        def _resolve_burst_size(sym: str):
            def _dig(d: dict, path: list):
                cur = d or {}
                for k in path:
                    if not isinstance(cur, dict):
                        return None
                    cur = cur.get(k)
                return cur

            prefer_asset = bool(
                ((base_config.get("entry_rules", {}) or {}).get("scalping", {}) or {})
                .get("burst_scalping", {})
                .get("prefer_asset_overrides", False)
            )
            ac = (
                decision_package.get("active_config")
                or decision_package.get("config_used")
                or {}
            )
            asset_cfgs_all = global_context.get("asset_configs") or {}
            asset_cfg = asset_cfgs_all.get(sym, {}) or {}

            def read_active_config():
                v = _dig(
                    ac, ["entry_rules", "scalping", "burst_scalping", "burst_size"]
                )
                return (
                    (int(v), "active_config")
                    if isinstance(v, (int, float)) and v > 0
                    else (None, None)
                )

            def read_asset_entry():
                v = _dig(
                    asset_cfg,
                    ["entry_rules", "scalping", "burst_scalping", "burst_size"],
                )
                return (
                    (int(v), "asset.entry_rules")
                    if isinstance(v, (int, float)) and v > 0
                    else (None, None)
                )

            def read_asset_over_new():
                v = _dig(
                    asset_cfg,
                    [
                        "overrides",
                        "scalping",
                        "entry_rules",
                        "scalping",
                        "burst_scalping",
                        "burst_size",
                    ],
                )
                return (
                    (int(v), "asset.overrides(new)")
                    if isinstance(v, (int, float)) and v > 0
                    else (None, None)
                )

            def read_asset_over_legacy():
                v = _dig(asset_cfg, ["overrides", "scalping", "burst", "burst_size"])
                return (
                    (int(v), "asset.overrides(legacy)")
                    if isinstance(v, (int, float)) and v > 0
                    else (None, None)
                )

            def read_strategy():
                try:
                    strat_conf = strategy_manager.get_strategy_config("scalping") or {}
                except Exception:
                    strat_conf = {}
                v = _dig(
                    strat_conf,
                    ["entry_rules", "scalping", "burst_scalping", "burst_size"],
                )
                return (
                    (int(v), "strategy")
                    if isinstance(v, (int, float)) and v > 0
                    else (None, None)
                )

            def read_global_top():
                v = _dig(
                    base_config,
                    ["entry_rules", "scalping", "burst_scalping", "burst_size"],
                )
                return (
                    (int(v), "global")
                    if isinstance(v, (int, float)) and v > 0
                    else (None, None)
                )

            def read_global_exec():
                v = _dig(
                    base_config,
                    [
                        "trade_executor_settings",
                        "entry_rules",
                        "scalping",
                        "burst_scalping",
                        "burst_size",
                    ],
                )
                return (
                    (int(v), "global.executor_settings")
                    if isinstance(v, (int, float)) and v > 0
                    else (None, None)
                )

            readers = (
                [
                    read_asset_entry,
                    read_asset_over_new,
                    read_asset_over_legacy,
                    read_active_config,
                    read_strategy,
                    read_global_top,
                    read_global_exec,
                ]
                if prefer_asset
                else [
                    read_active_config,
                    read_asset_entry,
                    read_asset_over_new,
                    read_asset_over_legacy,
                    read_strategy,
                    read_global_top,
                    read_global_exec,
                ]
            )
            for fn in readers:
                val, src = fn()
                if val:
                    return val, src
            return 5, "default"

        # === Exécution Scalping (Fusion-only) ===
        if scalping_decisions:
            logger.debug("📦 [PIPELINE] Décisions Scalping détectées:")
            for d in scalping_decisions:
                logger.debug(
                    f"   → {d.get('action')} {d.get('asset')} | vol={d.get('volume', 0)}"
                )

            # Burst guard global (pas de nouveau panier si un existe)
            try:
                burst_cfg = (
                    base_config.get("entry_rules", {})
                    .get("scalping", {})
                    .get("burst_scalping", {})
                    or {}
                )
                guard_cfg = burst_cfg.get("burst_guardrails", {}) or {}
                enforce_closure = bool(guard_cfg.get("enforce_burst_closure", True))
                single_burst_global = bool(guard_cfg.get("single_burst_global", True))
                if enforce_closure and single_burst_global:
                    import re

                    def _field(obj, key, default=None):
                        return (
                            obj.get(key, default)
                            if isinstance(obj, dict)
                            else getattr(obj, key, default)
                        )

                    def _open_burst_ids(positions):
                        ids = set()
                        for p in positions or []:
                            c = str(_field(p, "comment", "") or "")
                            m = re.search(r"bs_([a-f0-9]{8})", c)
                            if m:
                                ids.add(m.group(1))
                        return ids

                    all_open = mt5_connector.get_positions() or []
                    open_bursts = _open_burst_ids(all_open)
                    if open_bursts:
                        logger.info(
                            f"⛔ [BURST GUARD] Panier(s) actif(s): {', '.join(sorted(open_bursts))} → aucune exécution scalping ce cycle."
                        )
                        return False
            except Exception as e:
                logger.warning(f"[BURST GUARD][pipeline] check global échoué: {e}")

            for td in scalping_decisions:
                try:
                    side = str(td.get("action") or td.get("side") or "").upper().strip()
                    if side in {"LONG", "BUY_LONG"}:
                        side = "BUY"
                    if side in {"SHORT", "SELL_SHORT"}:
                        side = "SELL"
                    if side not in {"BUY", "SELL"}:
                        logger.error(
                            f"[SCALPING] action manquante/invalide → skip. payload={td}"
                        )
                        continue

                    sym = str(td.get("asset") or td.get("symbol") or "").upper()
                    if not sym:
                        logger.error(
                            f"[SCALPING] symbole manquant → skip. payload={td}"
                        )
                        continue
                    td["asset"] = sym
                    td["symbol"] = sym

                    _alias = str(td.get("rule_name", "")).lower().strip()
                    # Normalisation des anciens alias historiques vers burst_scalping
                    if _alias in {
                        "burst",
                        "burst_master",
                        "scalping_burst",
                        "burst_single",
                        "",
                    }:
                        td["rule_name"] = "burst_scalping"
                    rn = str(td.get("rule_name") or "burst_scalping").lower()

                    if rn == "burst_scalping":
                        for k in (
                            "tp_price",
                            "tp_pips",
                            "target_tp_pips",
                            "tp_prices",
                            "trailing",
                        ):
                            td.pop(k, None)
                        style = str(td.get("entry_style", "MARKET")).upper()
                        if style in {"LIMIT_FOK", "LIMIT-FOK", "FOK_LIMIT"}:
                            td["order_type"] = "LIMIT"
                            td.setdefault("time_in_force", "FOK")
                            if not td.get("price"):
                                try:
                                    tk = mt5_connector.get_symbol_tick(sym)
                                    ask = (
                                        tk.get("ask")
                                        if isinstance(tk, dict)
                                        else getattr(tk, "ask", None)
                                    )
                                    bid = (
                                        tk.get("bid")
                                        if isinstance(tk, dict)
                                        else getattr(tk, "bid", None)
                                    )
                                    td["price"] = float(ask if side == "BUY" else bid)
                                except Exception:
                                    pass
                        else:
                            td["order_type"] = "MARKET"

                        def _merge(a, b):
                            out = dict(a or {})
                            for k, v in (b or {}).items():
                                if isinstance(v, dict) and isinstance(out.get(k), dict):
                                    out[k] = _merge(out[k], v)
                                else:
                                    out[k] = v
                            return out

                        sltp_global = (
                            (
                                (base_config.get("entry_rules", {}) or {}).get(
                                    "scalping", {}
                                )
                                or {}
                            ).get("burst_scalping", {})
                            or {}
                        ).get("sltp", {}) or {}
                        sltp_asset_block = (
                            (
                                (
                                    (global_context.get("asset_configs", {}) or {}).get(
                                        sym, {}
                                    )
                                    or {}
                                ).get("entry_rules", {})
                                or {}
                            ).get("scalping", {})
                            or {}
                        ).get("burst_scalping", {}) or {}
                        sltp_asset = (
                            sltp_asset_block.get("sltp", {})
                            if isinstance(sltp_asset_block, dict)
                            else {}
                        ) or {}
                        sltp_cfg = _merge(sltp_global, sltp_asset)
                        if isinstance(td.get("sltp"), dict):
                            sltp_cfg = _merge(sltp_cfg, td["sltp"])
                        if isinstance(sltp_cfg, dict) and "dynamic" in sltp_cfg:
                            sltp_cfg["dynamic"] = bool(sltp_cfg.get("dynamic", True))
                        td["sltp"] = sltp_cfg

                    size, source = _resolve_burst_size(sym)
                    td.pop("burst_count", None)
                    td.pop("burst_size", None)
                    resolved_burst = max(int(size), 1)
                    logger.info(
                        f"[BURST][RESOLVE] {sym} → burst_size={resolved_burst} (source={source})"
                    )

                    td.pop("burst_volume_each", None)
                    td["burst_size"] = resolved_burst
                    td["sizing_scope"] = "BASKET"
                    td.setdefault("order_type", "MARKET")

                    od = td.get("order") if isinstance(td.get("order"), dict) else {}
                    od.update(
                        {
                            "action": side,
                            "side": side,
                            "type": td.get("order_type", "MARKET"),
                            "symbol": sym,
                        }
                    )
                    td["order"] = od

                    trade = td.get("trade") if isinstance(td.get("trade"), dict) else {}
                    trade.update({"action": side, "side": side})
                    td["trade"] = trade
                    for k in (
                        "action",
                        "side",
                        "order_action",
                        "order_side",
                        "trade_action",
                    ):
                        td[k] = side

                    logger.info(
                        f"[BURST][PLAN] {side} {sym} style={td.get('order_type','MARKET')} burst_size={resolved_burst} (lot via risk%/burst)"
                    )
                    logger.debug(
                        f"[SLTP-ACTION-CHECK] top.action={td.get('action')} | order.action={td.get('order',{}).get('action')}"
                    )

                    # FIX: Fusionner la config de stratégie scalping avec base_config
                    # pour que sltp.py et sizing.py trouvent les paramètres SL/TP (400 pips)
                    try:
                        scalping_strategy_config = strategy_manager.get_strategy_config("scalping") or {}
                        merged_config = dict(base_config)  # Copie
                        # Fusionner entry_rules de la stratégie scalping
                        if "entry_rules" in scalping_strategy_config:
                            merged_config.setdefault("entry_rules", {}).update(
                                scalping_strategy_config["entry_rules"]
                            )
                    except Exception as e:
                        logger.warning(f"[SCALPING][PIPELINE] Fusion config échouée: {e}")
                        merged_config = base_config

                    decision_pkg = {
                        "final_decision": td,
                        "context": global_context,
                        "active_config": merged_config,
                    }
                    decision_pkg.setdefault("audit_context", {}).update(
                        {
                            "intent_symbol": td.get("symbol"),
                            "intent_side": td.get("side"),
                            "intent_burst": td.get("burst_size"),
                        }
                    )

                    res = run_trade_execution_pipeline(
                        trade_executor, decision_pkg, is_dry_run=is_dry_run
                    )
                    status = (res or {}).get("status", "")
                    if status not in {"failed", ""}:
                        trade_executed_successfully = True

                    try:
                        max_loss = float(
                            (
                                (
                                    merged_config.get("entry_rules", {})
                                    .get("scalping", {})
                                    .get("burst_scalping", {})
                                    .get("closure_rules", {})
                                    or {}
                                ).get("max_loss_pips", 15.0)
                            )
                        )
                        trade_executor.monitor_burst_baskets(
                            config=merged_config, max_loss_pips=max_loss
                        )
                    except Exception as e:
                        logger.warning(f"[BURST EXIT] Post-exec (SLTP): {e}")

                except Exception as e:
                    logger.error(
                        f"[BURST] Erreur bloc scalping/SLTP: {e}", exc_info=True
                    )

        # === Exécution Liquidity ===
        if liquidity_decisions:
            logger.debug("📦 [PIPELINE] Décisions Liquidity détectées:")
            for d in liquidity_decisions:
                logger.debug(
                    f"   → {d.get('action')} {d.get('asset')} | vol={d.get('volume', 0)}"
                )
            for td in liquidity_decisions:
                action = str(td.get("action", "")).upper()
                if action in {"BUY", "SELL"}:
                    res = _execute_single_decision(
                        td,
                        trade_executor,
                        mt5_connector,
                        global_context,
                        decision_package,
                        execution_mode,
                        logger,
                    )
                    # si l'exécution ne remonte pas explicitement "failed", on considère le cycle comme réussi
                    if res is None or res not in ("failed", False):
                        trade_executed_successfully = True

    except Exception as e:  # try GLOBAL (inchangé)
        logger.error(f"Erreur pipeline: {e}", exc_info=True)
        trade_executed_successfully = False
    finally:
        try:
            get_tracker_from_context(global_context).emit_summary(logger)
        except Exception:
            pass
        return trade_executed_successfully


# ═══════════════════════════════════════════════════════════════════════════
# MULTI-THREADING SCALPING: 3 workers + Dashboard (31 DEC 2025)
# ═══════════════════════════════════════════════════════════════════════════

class GlobalScalpingState:
    """
    Classe thread-safe pour gérer l'état partagé entre les 3 threads scalping.

    Chaque thread (USDJPY, EURUSD, GBPUSD) met à jour son state indépendamment.
    Le dashboard thread lit l'état agrégé pour affichage console.

    Attributs:
    - asset_states: Dict[str, dict] - État par asset
    - lock: threading.Lock - Synchronisation thread-safe
    """

    def __init__(self, assets: list):
        """
        Initialise le state global pour tous les assets.

        Args:
            assets: Liste des symboles (ex: ["USDJPY", "EURUSD", "GBPUSD"])
        """
        self.lock = threading.Lock()
        self.asset_states = {}

        for asset in assets:
            self.asset_states[asset] = {
                # Market analysis
                "regime": "UNKNOWN",
                "regime_force": 0.0,

                # OrderFlow V6
                "of_score": 0.0,
                "of_bias": "NEUTRAL",
                "of_quality": "NO_TRADE",

                # Timing gatekeeper
                "timing_status": "UNKNOWN",
                "tick_rate": 0.0,
                "coverage_s": 0.0,

                # Decision
                "action": "HOLD",
                "confidence": 0.0,

                # Performance
                "last_update": None,
                "cycle_count": 0,
                "errors_count": 0,
                "last_error": None,
            }

    def update_asset_state(self, asset: str, updates: dict):
        """
        Met à jour l'état d'un asset (thread-safe).

        Args:
            asset: Symbole (ex: "USDJPY")
            updates: Dict avec clés à mettre à jour
        """
        with self.lock:
            if asset not in self.asset_states:
                return

            self.asset_states[asset].update(updates)
            self.asset_states[asset]["last_update"] = time.time()

    def get_asset_state(self, asset: str) -> dict:
        """
        Récupère l'état d'un asset (thread-safe).

        Args:
            asset: Symbole

        Returns:
            Dict avec état actuel (copie)
        """
        with self.lock:
            if asset not in self.asset_states:
                return {}
            return self.asset_states[asset].copy()

    def get_all_states(self) -> dict:
        """
        Récupère l'état de tous les assets (thread-safe).

        Returns:
            Dict[str, dict] - Copie complète du state
        """
        with self.lock:
            return {
                asset: state.copy()
                for asset, state in self.asset_states.items()
            }

    def increment_cycle(self, asset: str):
        """Incrémente le compteur de cycles pour un asset."""
        with self.lock:
            if asset in self.asset_states:
                self.asset_states[asset]["cycle_count"] += 1

    def record_error(self, asset: str, error_msg: str):
        """Enregistre une erreur pour un asset."""
        with self.lock:
            if asset in self.asset_states:
                self.asset_states[asset]["errors_count"] += 1
                self.asset_states[asset]["last_error"] = error_msg


# ═══════════════════════════════════════════════════════════════════════════
# 🛡️ 08 FEV 2026: PMA ADJUSTMENTS RÉUTILISABLE (Branche 2 + Branche 3)
# Malus/Bonus + Veto Dur + Veto Reversal + Veto Fatigue
# ═══════════════════════════════════════════════════════════════════════════

def apply_pma_adjustments(
    signal_action: str,
    orderflow_score: float,
    mtf_verdict,
    mtf_direction: str,
    micro_resistance_info: dict,
    inst_result: dict,
    inst_score: float,
    inst_veto_fatigue: bool,
    inst_veto_reversal: bool,
    current_regime: str,
    memory_clarity: float,
    memory_trend_strength: float,
    memory_trend_direction: str,
    price_memory_analyzer,
    rates_df_fresh,
    current_price: float,
    asset: str,
    logger_ref=None,
) -> dict:
    """
    Calcule les ajustements PMA (Bonus/Malus) et les vetos.
    Retourne un dict avec toutes les infos d'ajustement.

    Returns:
        {
            "pma_bonus": float,
            "pma_malus": float,
            "pma_adjustment": float,
            "pma_adjustments": list[str],
            "pma_veto_dur": bool,
            "inst_veto_reversal": bool,
            "score_brut": float,
            "score_ajuste": float,
        }
    """
    pma_bonus = 0.0
    pma_malus = 0.0
    pma_adjustments = []
    pma_veto_dur = False
    _inst_veto_reversal = inst_veto_reversal  # copie locale modifiable

    # ══════════════════════════════════════════════════════
    # 📉 CALCUL DES MALUS (Risque & Veto)
    # ══════════════════════════════════════════════════════

    # MALUS 1: Micro-Résistance M1 (-30 pts)
    if (signal_action == "BUY" and
        micro_resistance_info.get('strength') in ['STRONG', 'MODERATE'] and
        micro_resistance_info.get('distance_pips', 999) < 1.0 and
        micro_resistance_info.get('bounce_probability', 0) >= 0.7):
        pma_malus += 30.0
        pma_adjustments.append(
            f"MALUS_MICRO_RES: -30 (Résistance {micro_resistance_info['strength']} à {micro_resistance_info['distance_pips']:.2f} pips)"
        )

    # MALUS 2: Contre-Tendance MTF → SUPPRIMÉ 10 FEV 2026
    # La stratégie (scalping.py) applique désormais le VETO MTF AVANT le bias.
    # Ce malus post-hoc est redondant et inefficace.

    # MALUS 3: Zone Range/Accumulation (-20 pts)
    current_regime_lower = str(current_regime).lower() if current_regime else "unknown"
    if any(rg in current_regime_lower for rg in ["range", "accumulation", "distribution"]):
        if signal_action in ["BUY", "SELL"]:
            pma_malus += 20.0
            pma_adjustments.append(
                f"MALUS_REGIME: -20 (Régime '{current_regime}' incompatible avec {signal_action})"
            )

    # MALUS 4: VETO FATIGUE (Circuit Breaker) (-50 pts)
    if inst_veto_fatigue:
        pma_malus += 50.0
        pma_adjustments.append(
            f"MALUS_FATIGUE: -50 (Circuit Breaker - Marché épuisé)"
        )

    # ══════════════════════════════════════════════════════
    # 📈 CALCUL DES BONUS (Validation & Alignement)
    # ══════════════════════════════════════════════════════

    # BONUS 1: MTF Alignment 4/4 (+15 pts) — 12 FEV 2026: adapté pour M30+M15+M5+M1
    if mtf_verdict is not None and mtf_verdict.alignment_count == 4:
        if (mtf_direction == "BULLISH" and signal_action == "BUY") or \
           (mtf_direction == "BEARISH" and signal_action == "SELL"):
            pma_bonus += 15.0
            pma_adjustments.append(
                f"BONUS_MTF_4/4: +15 (Alignement parfait {mtf_direction} + {signal_action})"
            )
    # BONUS 1b: MTF Alignment 3/4 (+10 pts)
    elif mtf_verdict is not None and mtf_verdict.alignment_count == 3:
        if (mtf_direction == "BULLISH" and signal_action == "BUY") or \
           (mtf_direction == "BEARISH" and signal_action == "SELL"):
            pma_bonus += 10.0
            pma_adjustments.append(
                f"BONUS_MTF_3/4: +10 (Alignement {mtf_verdict.alignment} {mtf_direction} + {signal_action})"
            )

    # BONUS 2: Niveaux Frais (+10 pts)
    if price_memory_analyzer is not None:
        try:
            fresh_levels = price_memory_analyzer.find_fresh_levels(
                historical_data=rates_df_fresh,
                current_price=current_price,
                lookback=20
            )
            if fresh_levels and len(fresh_levels) > 0:
                for level in fresh_levels:
                    distance_pips = abs(current_price - level) * 10000
                    if distance_pips < 2.0:
                        pma_bonus += 10.0
                        pma_adjustments.append(
                            f"BONUS_FRESH_LEVEL: +10 (Niveau frais à {distance_pips:.1f} pips)"
                        )
                        break
        except Exception:
            pass

    # BONUS 3: Trend Consistency (+5 pts)
    if memory_clarity >= 0.7 and memory_trend_strength >= 0.6:
        if (memory_trend_direction == "BULLISH" and signal_action == "BUY") or \
           (memory_trend_direction == "BEARISH" and signal_action == "SELL"):
            pma_bonus += 5.0
            pma_adjustments.append(
                f"BONUS_TREND_CONSISTENCY: +5 (Clarity={memory_clarity:.2f}, Strength={memory_trend_strength:.2f})"
            )

    # BONUS 4: Institutional Reversal Signal (+10 pts)
    if inst_result is not None and inst_score >= 65:
        inst_trend = inst_result.get('new_trend', 'NEUTRAL')
        if (inst_trend == "BULLISH" and signal_action == "BUY") or \
           (inst_trend == "BEARISH" and signal_action == "SELL"):
            pma_bonus += 10.0
            pma_adjustments.append(
                f"BONUS_INSTITUTIONAL: +10 (Score={inst_score:.0f}, Trend={inst_trend})"
            )
        elif inst_result.get('reversal_detected', False):
            # 🚫 VETO DIRECT si reversal détecté en direction opposée
            _inst_veto_reversal = True
            pma_adjustments.append(
                f"🚫 VETO_REVERSAL: Reversal {inst_trend} vs Signal {signal_action} (Score={inst_score:.0f})"
            )
            if logger_ref:
                logger_ref.warning(
                    f"🚫 [VETO_REVERSAL][{asset}] Trade BLOQUÉ | "
                    f"Reversal {inst_trend} détecté vs Signal {signal_action} | "
                    f"Score institutionnel: {inst_score:.0f}"
                )

    # ══════════════════════════════════════════════════════
    # 🧮 CALCUL DU SCORE AJUSTÉ
    # ══════════════════════════════════════════════════════
    score_brut = orderflow_score
    pma_adjustment_total = pma_bonus - pma_malus
    score_ajuste = score_brut + pma_adjustment_total

    # ══════════════════════════════════════════════════════
    # 🚫 RÈGLE DU VETO DUR (score ajusté < 60 → rejeté)
    # ══════════════════════════════════════════════════════
    VETO_DUR_THRESHOLD = 60.0
    if signal_action in ["BUY", "SELL"] and score_ajuste < VETO_DUR_THRESHOLD:
        pma_veto_dur = True

    # LOG
    if logger_ref and (pma_adjustment_total != 0 or pma_veto_dur or _inst_veto_reversal):
        adj_emoji = "📈" if pma_adjustment_total > 0 else ("📉" if pma_adjustment_total < 0 else "⚖️")
        veto_str = ""
        if pma_veto_dur:
            veto_str = " 🚫 VETO_DUR"
        if _inst_veto_reversal:
            veto_str = " 🚫 VETO_REVERSAL"
        logger_ref.info(
            f"{adj_emoji} [PMA_ADJUSTMENT][{asset}] "
            f"Score: {score_brut:.1f} → {score_ajuste:.1f} "
            f"(Bonus: +{pma_bonus:.0f}, Malus: -{pma_malus:.0f}){veto_str}"
        )
        for adj in pma_adjustments:
            logger_ref.debug(f"   └─ {adj}")

    return {
        "pma_bonus": pma_bonus,
        "pma_malus": pma_malus,
        "pma_adjustment": pma_adjustment_total,
        "pma_adjustments": pma_adjustments,
        "pma_veto_dur": pma_veto_dur,
        "inst_veto_reversal": _inst_veto_reversal,
        "score_brut": score_brut,
        "score_ajuste": score_ajuste,
    }


# ═══════════════════════════════════════════════════════════════════════════
# 🎯 WORKER THREAD: Scalping générique multi-asset (31 DEC 2025)
# ═══════════════════════════════════════════════════════════════════════════

def scalping_worker(
    asset: str,
    global_state: GlobalScalpingState,
    display_queue: queue.Queue,
    context_lock: threading.Lock,
    offset_seconds: float,
    mt5_connector,
    decision_pipeline,
    trade_executor,
    config_manager,
    mecano,
    strategy_manager,
    is_dry_run: bool,
    stop_event: threading.Event,
    logger,
    pause_event: threading.Event = None  # 04 FEV 2026: Support pause/resume Telegram
):
    """
    🎯 Worker thread dédié au SCALPING pour un asset spécifique (31 DEC 2025)

    Architecture simplifiée:
    - Analyse M1 pour l'asset fourni (USDJPY, EURUSD, ou GBPUSD)
    - Timing Gatekeeper → PASS/VETO (filtre session + liquidité)
    - OrderFlow V6 → Source unique de signaux (score 0-100)
    - MarketAnalyzer.build_decision() → BUY/SELL/HOLD direct
    - Update GlobalScalpingState (thread-safe)

    Args:
        asset: Symbole forex (ex: "USDJPY", "EURUSD", "GBPUSD")
        global_state: Instance GlobalScalpingState pour state partagé
        offset_seconds: Délai avant démarrage (staggered timing)
        [... autres params identiques]

    ❌ SUPPRIMÉ: FusionManager, VWAP, Footprint, Momentum
    """
    # Import MarketAnalyzer au début pour éviter conflit de portée avec variable locale
    from phase_observer.market_analyzer import MarketAnalyzer
    import pandas as pd  # Import local pour éviter problèmes de portée dans le thread

    # 🎯 OFFSET DE DÉMARRAGE (staggered timing)
    if offset_seconds > 0:
        logger.info(f"[{asset}] ⏱️  Délai démarrage: {offset_seconds:.1f}s")
        time.sleep(offset_seconds)

    # ✅ CONFIGURABLE (02 JAN 2026): Lire cycle depuis prod_config.json
    cycle_interval = config_manager.get("bot_behavior.cycle_interval_seconds", 2.5)
    cycle_count = 0

    logger.info(f"🚀 [{asset}] Worker démarré (cycle {cycle_interval}s) ⚡⚡")

    # ✅ Instancier MarketAnalyzer pour décisions minimalistes
    try:
        market_analyzer_thread = MarketAnalyzer(config_manager=config_manager, logger=logger)
        logger.info(f"✅ [{asset}] MarketAnalyzer instancié")
    except Exception as e:
        logger.error(f"❌ [{asset}] Impossible de créer MarketAnalyzer: {e}")
        global_state.record_error(asset, f"MarketAnalyzer init failed: {e}")
        return  # Arrêt du thread si MarketAnalyzer échoue

    # ✅ Instancier PriceMemoryAnalyzer pour analyse de tendance (08 JAN 2026)
    try:
        from phase_observer.price_memory_analyzer import PriceMemoryAnalyzer
        price_memory_analyzer = PriceMemoryAnalyzer(logger=logger)
        logger.info(f"✅ [{asset}] PriceMemoryAnalyzer instancié")
    except Exception as e:
        logger.error(f"❌ [{asset}] Impossible de créer PriceMemoryAnalyzer: {e}")
        price_memory_analyzer = None  # Continue sans Price Memory

    # ✅ 25 JAN 2026: Instancier InstitutionalReversalDetector
    # Responsable de: Wyckoff, ML Patterns, Divergences CVD, Changepoint
    try:
        from phase_observer.institutional_reversal_detector import InstitutionalReversalDetector
        institutional_detector = InstitutionalReversalDetector(logger=logger)
        logger.info(f"✅ [{asset}] InstitutionalReversalDetector instancié")
    except Exception as e:
        logger.error(f"❌ [{asset}] Impossible de créer InstitutionalReversalDetector: {e}")
        institutional_detector = None

    # ✅ Instancier ScalpingStrategy pour logs de rapport OrderFlow V6
    try:
        from strategy.scalping import ScalpingStrategy
        # Récupérer la config de stratégie scalping
        strat_cfg = strategy_manager.get_strategy_config("scalping") or {}
        scalping_strategy = ScalpingStrategy(
            config_manager=config_manager,
            strategy_config=strat_cfg,
            mt5_connector=mt5_connector,
            logger=logger
        )
        logger.info(f"✅ [{asset}] ScalpingStrategy instanciée")
    except Exception as e:
        logger.warning(f"⚠️ [{asset}] ScalpingStrategy init failed: {e}")
        scalping_strategy = None

    # ✅ NOUVEAU (03 JAN 2026): Instancier SimpleAdvancedScorer pour scoring composite
    advanced_scorer = None
    try:
        # Récupérer poids custom depuis config (optionnel)
        scoring_weights = strat_cfg.get("advanced_scoring", {}).get("weights", None)
        advanced_scorer = SimpleAdvancedScorer(config=scoring_weights)
        logger.info(f"✅ [{asset}] SimpleAdvancedScorer instancié (évolutif)")
    except Exception as e:
        logger.warning(f"⚠️ [{asset}] SimpleAdvancedScorer init failed: {e}, fallback OrderFlow V6 seul")
        advanced_scorer = None

    # ⚡ OPTION 1: PRÉ-CALCUL — Squelette trade decision (parties statiques)
    # Créé UNE FOIS au démarrage, réutilisé à chaque cycle avec valeurs dynamiques
    trade_decision_skeleton = None
    last_config_update = 0

    while not stop_event.is_set():
        cycle_count += 1
        cycle_start = time.time()
        global_state.increment_cycle(asset)

        # ⏸️ PAUSE CHECK (04 FEV 2026): Si en pause, skip le trading mais reste vivant
        if pause_event and pause_event.is_set():
            time.sleep(cycle_interval)
            continue

        try:
            # ⚡ VÉRIFICATION SYMBOL (03 JAN 2026): Protection contre symbol non disponible
            # Vérifier que le symbol est disponible dans MT5 AVANT de tenter fetch
            try:
                symbol_info = mt5_connector.get_symbol_info(asset)
                if not symbol_info:
                    logger.error(f"[{asset}] ❌ Symbol non disponible dans MT5, skip cycle")
                    global_state.record_error(asset, "Symbol unavailable")
                    time.sleep(cycle_interval)
                    continue

                # Vérifier si visible dans Market Watch
                if hasattr(symbol_info, 'visible') and not symbol_info.visible:
                    logger.warning(f"[{asset}] ⚠️ Symbol non visible, tentative activation...")
                    # Tentative d'activation (ne pas bloquer si échec)
                    try:
                        import MetaTrader5 as mt5
                        if not mt5.symbol_select(asset, True):
                            logger.warning(f"[{asset}] ⚠️ Impossible activer symbol, continue quand même...")
                    except Exception as e_select:
                        logger.debug(f"[{asset}] Symbol select failed: {e_select}")

            except Exception as e_symbol:
                logger.warning(f"[{asset}] ⚠️ Vérification symbol failed: {e_symbol}")
                # Continue quand même (ne pas bloquer le cycle)

            # 🔧 FIX (25 JAN 2026): Définir point et digits depuis symbol_info
            point = getattr(symbol_info, "point", 0.00001) if symbol_info else 0.00001
            digits = getattr(symbol_info, "digits", 5) if symbol_info else 5

            # 🔧 FIX (25 JAN 2026): Charger asset_cfg pour momentum_filter
            try:
                asset_cfg = config_manager.config_loader.load_asset_config(asset) or {}
            except Exception as e_asset_cfg:
                logger.warning(f"[{asset}] ⚠️ Erreur chargement asset config: {e_asset_cfg}")
                asset_cfg = {}

            # ✅ PHASE 2: Import cache multi-niveaux
            from core.bars_cache import bars_cache
            from phase_observer.regime_resolver import regime_resolver

            # ✅ PHASE 2: Utiliser cache barres (30 barres au lieu de 200)
            # 95% du temps: récupère 1 barre seulement (bougie courante)
            # Recharge complète toutes les 60s seulement
            rates_df = bars_cache.get_or_fetch(
                symbol=asset,
                timeframe="M1",
                count=50,  # ✅ AJUSTÉ: 30→50 (minimum requis pour PhaseObserver volume_zscore)
                mt5_connector=mt5_connector,
                ttl_seconds=60.0,
            )
            if rates_df is None or rates_df.empty:
                logger.warning(f"[{asset}] Données M1 indisponibles")
                global_state.update_asset_state(asset, {
                    "regime": "NO_DATA",
                    "action": "HOLD"
                })
                time.sleep(cycle_interval)
                continue

            # ═══════════════════════════════════════════════════════════════
            # 🆕 25 JAN 2026: CHARGEMENT M5 et M15 pour analyse MTF
            # Permet verdict multi-timeframe (M15 + M5 + M1)
            # ═══════════════════════════════════════════════════════════════
            rates_df_m5 = None
            rates_df_m15 = None
            rates_df_m30 = None
            try:
                rates_df_m5 = bars_cache.get_or_fetch(
                    symbol=asset,
                    timeframe="M5",
                    count=60,  # 08 FEV 2026: 60 bougies M5 = 300 min (IRD Changepoint en veut 50)
                    mt5_connector=mt5_connector,
                    ttl_seconds=60.0,
                )
                rates_df_m15 = bars_cache.get_or_fetch(
                    symbol=asset,
                    timeframe="M15",
                    count=10,  # 10 bougies M15 = 150 minutes d'historique
                    mt5_connector=mt5_connector,
                    ttl_seconds=60.0,
                )
                # 12 FEV 2026: M30 pour contexte macro (dominant dans pondération MTF)
                rates_df_m30 = bars_cache.get_or_fetch(
                    symbol=asset,
                    timeframe="M30",
                    count=10,  # 10 bougies M30 = 5 heures de contexte
                    mt5_connector=mt5_connector,
                    ttl_seconds=120.0,  # Cache 2 min (M30 change lentement)
                )
                if rates_df_m5 is not None and rates_df_m15 is not None:
                    m30_info = f", M30={len(rates_df_m30)} bars" if rates_df_m30 is not None else ""
                    logger.debug(f"[{asset}] ✅ MTF data loaded: M5={len(rates_df_m5)} bars, M15={len(rates_df_m15)} bars{m30_info}")
            except Exception as e_mtf_load:
                logger.warning(f"[{asset}] ⚠️ Erreur chargement M5/M15/M30 pour MTF: {e_mtf_load}")
                rates_df_m5 = None
                rates_df_m15 = None
                rates_df_m30 = None

            # 🎯 (05 JAN 2026): FENÊTRE GLISSANTE pour scalping sniper
            # Fix: Fenêtre M1 (60s) → scores identiques pendant 24 cycles
            # Solution: Charger derniers 8s de ticks en temps réel (analyse au scalpel)
            ticks_df = None
            try:
                # 🔪 SCALPING SNIPER: Fenêtre ultra-courte 8s (identique au main loop)
                sliding_window_seconds = 8

                # Calculer fenêtre glissante [NOW - 20s → NOW]
                now_utc = pd.Timestamp.utcnow()
                tick_window_start = now_utc - pd.Timedelta(seconds=sliding_window_seconds)
                tick_window_end = now_utc

                # Logging début chargement
                logger.info(
                    f"[{asset}] 🔄 Chargement ticks [fenêtre glissante {sliding_window_seconds}s] | "
                    f"[{tick_window_start.strftime('%H:%M:%S')} → {tick_window_end.strftime('%H:%M:%S')}]"
                )

                # Charger ticks pour fenêtre glissante (avec timeout 5s par défaut)
                ticks_df = mt5_connector.get_ticks_for_candle(
                    asset,
                    tick_window_start.to_pydatetime(),
                    tick_window_end.to_pydatetime(),
                    timeout=5.0  # ⚡ TIMEOUT (03 JAN 2026): Protection contre blocage MT5
                )

                if ticks_df is not None and not ticks_df.empty:
                    logger.info(f"[{asset}] ✅ {len(ticks_df)} ticks chargés (fenêtre glissante {sliding_window_seconds}s)")
                else:
                    logger.warning(f"[{asset}] ⚠️ Aucun tick récupéré dans fenêtre glissante {sliding_window_seconds}s")
                    ticks_df = None

            except TimeoutError as e_timeout:
                logger.error(f"[{asset}] ⏱️ TIMEOUT chargement ticks: {e_timeout}")
                ticks_df = None

            except Exception as e_ticks:
                logger.error(f"[{asset}] ❌ Erreur chargement ticks: {e_ticks}", exc_info=True)
                ticks_df = None

            # MarketAnalyzer (phase + patterns + features)
            # Import déplacé au début de la fonction (ligne 3124)
            # ❌ DÉSACTIVÉ (25 DEC 2025): footprint_cache - Architecture minimaliste
            # from core.footprint_cache import footprint_cache

            # Signature: MarketAnalyzer(config_manager, logger)
            market_analyzer = MarketAnalyzer(config_manager, logger)

            # 🎯 PIPELINE SIMPLIFIÉ (25 DEC 2025): Analyse directe sans cache
            # OrderFlow V6 calculé en direct par market_analyzer.analyze()
            try:
                # Analyser rates_df (OHLC M1) + ticks pour timing_gatekeeper
                market_results = market_analyzer.analyze(
                    asset=asset,
                    df=rates_df,
                    ticks=ticks_df  # ✅ Ticks requis pour timing_gatekeeper liquidité analysis
                )

                logger.debug(f"[{asset}] market_analyzer.analyze() OK")
            except Exception as e_analysis:
                logger.error(f"[{asset}] Erreur analyze(): {e_analysis}")
                global_state.record_error(asset, f"analyze() failed: {e_analysis}")
                # Continuer avec market_results vide pour ne pas crasher le thread
                market_results = {"latest": {}, "annotated_df": rates_df if rates_df is not None else pd.DataFrame()}

            # ⚡ OPTION 1: PRÉ-CALCUL — Mise à jour squelette si config changée
            try:
                current_config_hash = hash(str(config_manager.get_current_dynamic_config()))
                if trade_decision_skeleton is None or current_config_hash != last_config_update:
                    # Obtenir base_config
                    base_config = config_manager.get_current_dynamic_config()
                    strat_cfg = strategy_manager.get_strategy_config("scalping") or {}

                    # Résoudre burst_size
                    strat_cfg_entry = strat_cfg.get("entry_rules", {})
                    scalping_cfg = strat_cfg_entry.get("scalping", {})
                    burst_cfg = scalping_cfg.get("burst_scalping", {})
                    resolved_burst = burst_cfg.get("burst_size", 5)

                    # Copier config SLTP
                    sltp_cfg = burst_cfg.get("sltp", {})
                    if not sltp_cfg:
                        sltp_cfg = (
                            base_config.get("entry_rules", {})
                            .get("scalping", {})
                            .get("burst_scalping", {})
                            .get("sltp", {})
                        ) or {}

                    # Fusionner config scalping avec base_config + ALL asset overrides via ConfigMerger
                    try:
                        merged_config = dict(base_config)

                        # Merge strategy entry_rules
                        scalping_strategy_config = strategy_manager.get_strategy_config("scalping") or {}
                        if "entry_rules" in scalping_strategy_config:
                            merged_config.setdefault("entry_rules", {}).update(
                                scalping_strategy_config["entry_rules"]
                            )

                        # Merge ALL asset overrides via ConfigMerger
                        asset_merged = config_manager.get_merged_config_for_asset(asset, "scalping")
                        if asset_merged and "entry_rules" in asset_merged:
                            merged_config["entry_rules"] = _deep_merge_dicts(
                                merged_config.get("entry_rules", {}),
                                asset_merged["entry_rules"]
                            )

                        # Inject symbol_info
                        if asset_merged and "symbol_info" in asset_merged:
                            merged_config["symbol_info"] = asset_merged["symbol_info"]

                        # Update sltp_cfg pour le skeleton
                        burst_section = (
                            merged_config.get("entry_rules", {})
                            .get("scalping", {})
                            .get("burst_scalping", {})
                        )
                        if "sltp" in burst_section:
                            sltp_cfg = burst_section["sltp"]

                        logger.info(f"✅ [CONFIG_MERGE] ALL asset overrides applied for {asset} via ConfigMerger")
                    except Exception as e:
                        logger.warning(f"[{asset}] ConfigMerger failed: {e}", exc_info=True)
                        merged_config = base_config

                    # ⚡ SQUELETTE PRÉ-CALCULÉ (parties statiques)
                    trade_decision_skeleton = {
                        "static": {
                            "symbol": asset,
                            "rule_name": "burst_scalping",
                            "burst_size": resolved_burst,
                            "burst_enabled": True,
                            "strategy": "scalping",
                            "sltp": sltp_cfg,
                            "safety": {
                                "fat_finger": {"policy": "FLOOR"}
                            },
                        },
                        "merged_config": merged_config,
                        "resolved_burst": resolved_burst,
                    }
                    last_config_update = current_config_hash
                    logger.info(f"⚡ [PRE-CALC] Squelette trade decision mis à jour (burst={resolved_burst})")
            except Exception as e:
                logger.warning(f"[PRE-CALC] Erreur pré-calcul squelette: {e}")

            # 🎯 PIPELINE MINIMALISTE (26 DEC 2025) - S'exécute TOUJOURS si données disponibles
            if market_results:
                # Pipeline minimaliste : extraire latest depuis market_results
                latest = market_results.get("latest")

                # Context minimal pour compatibilité + account_info pour sizing (31 DEC 2025)
                account_info_dict = {}
                try:
                    acct = mt5_connector.get_account_info()
                    if acct:
                        account_info_dict = acct._asdict() if hasattr(acct, '_asdict') else (dict(acct) if hasattr(acct, '__dict__') else {})
                except Exception as e_acct:
                    logger.warning(f"[{asset}] Erreur récupération account_info: {e_acct}")

                # ✅ FIX (02 JAN 2026): Récupérer active_broker_account pour calcul risk_per_trade_percent
                broker_account = {}
                try:
                    broker_account = config_manager.get_mt5_account_credentials(
                        account_id=None,  # None = compte par défaut selon mode
                        mode=config_manager.get("mode_execution", "DEMO")
                    )
                    # 🔍 DEBUG (02 JAN 2026): Vérifier trade_settings
                    trade_settings = broker_account.get("trade_settings", {}) if broker_account else {}
                    logger.debug(f"[{asset}] 🔍 broker_account.trade_settings = {trade_settings}")
                except Exception as e_broker:
                    logger.warning(f"[{asset}] Erreur récupération broker account: {e_broker}")

                ctx = {
                    "asset": asset,
                    "phase": market_results.get("phase", {}),
                    "volatility_pips": market_results.get("volatility_pips", 0.0),
                    "account_info": account_info_dict,  # ✅ FIX (31 DEC): Ajouter account_info pour calcul sizing
                    "active_broker_account": broker_account,  # ✅ FIX (02 JAN 2026): Ajouter pour risk_per_trade_percent
                }

                # ═══════════════════════════════════════════════════════════════
                # 🎯 PIPELINE MINIMALISTE (26 DEC 2025) - OrderFlow V6 seul
                # ═══════════════════════════════════════════════════════════════
                logger.info(f"🎯 [SCALPING_CYCLE_{cycle_count}] Début analyse {asset}")

                # ========== INITIALISATION VARIABLES (pour rapport) ==========
                orderflow_result_mini = {"score": 0.0, "bias": "NEUTRAL", "summary": {}}
                decision_mini = {"action": "HOLD", "confidence": 0.0, "rationale": "Non analysé", "anchor_price": None}

                # ═══════════════════════════════════════════════════════════════
                # ✨ REFACTOR (29 DEC 2025): Calcul OrderFlow AVANT timing gatekeeper
                # OBJECTIF: Scorer TOUJOURS (même hors heures optimales)
                #           Mais TRADER seulement si timing PASS
                # ═══════════════════════════════════════════════════════════════

                # ========== ÉTAPE 1: CALCUL ORDERFLOW V6 (TOUJOURS) ==========
                # 🎯 Calcul OrderFlow V6 en direct - TOUJOURS exécuté pour avoir le scoring
                if scalping_strategy:
                    try:
                        # 🔄 FIX (26 DEC 2025): Utiliser bars_cache pour éviter lectures MT5 répétées
                        from core.bars_cache import bars_cache
                        rates_df_fresh = bars_cache.get_or_fetch(
                            symbol=asset,
                            timeframe="M1",
                            count=50,
                            mt5_connector=mt5_connector
                        )
                        if rates_df_fresh is None or rates_df_fresh.empty:
                            logger.warning("[ORDERFLOW] Impossible de récupérer rates_df M1 depuis cache")
                            raise ValueError("rates_df vide")

                        # 🔍 LOG: Vérifier rafraîchissement bougies
                        now_utc = pd.Timestamp.now(tz='UTC')
                        last_candle_time = rates_df_fresh.iloc[-1]['time'] if 'time' in rates_df_fresh.columns else rates_df_fresh.index[-1]
                        prev_candle_time = rates_df_fresh.iloc[-2]['time'] if 'time' in rates_df_fresh.columns else rates_df_fresh.index[-2]
                        last_candle_color = "🟢" if rates_df_fresh.iloc[-1]['close'] > rates_df_fresh.iloc[-1]['open'] else "🔴"
                        prev_candle_color = "🟢" if rates_df_fresh.iloc[-2]['close'] > rates_df_fresh.iloc[-2]['open'] else "🔴"
                        logger.debug(
                            f"[RATES_REFRESH][{asset}] now={now_utc.strftime('%H:%M:%S')} | "
                            f"last_candle={last_candle_time} {last_candle_color} | "
                            f"prev_candle={prev_candle_time} {prev_candle_color} (analysée)"
                        )

                        # ═══════════════════════════════════════════════════════════════
                        # 12 FEV 2026: MTF VERDICT AVANT l'orderflow
                        # Le verdict MTF (M30+M15+M5+M1) est calculé ICI pour imposer
                        # la direction à scalping.py via asset_signals["mtf_direction"]
                        # ═══════════════════════════════════════════════════════════════
                        # current_price depuis la dernière bougie M1 (disponible ici)
                        current_price_for_mtf = rates_df_fresh.iloc[-1]['close'] if rates_df_fresh is not None and len(rates_df_fresh) > 0 else 0.0
                        mtf_verdict = None
                        mtf_bonus = 0.0
                        mtf_direction = "NEUTRAL"
                        try:
                            if price_memory_analyzer is not None:
                                mtf_verdict = price_memory_analyzer.get_mtf_trend_verdict(
                                    asset=asset,
                                    candles_m30=rates_df_m30,
                                    candles_m15=rates_df_m15,
                                    candles_m5=rates_df_m5,
                                    candles_m1=rates_df_fresh,
                                    current_price=current_price_for_mtf
                                )
                                mtf_bonus = mtf_verdict.bonus
                                mtf_direction = mtf_verdict.direction

                                mtf_emoji = "🐻" if mtf_direction == "BEARISH" else ("🐂" if mtf_direction == "BULLISH" else "⚖️")
                                logger.info(
                                    f"{mtf_emoji} [MTF_VERDICT][{asset}] {mtf_direction} ({mtf_verdict.alignment}) | "
                                    f"M30:{mtf_verdict.m30_direction} M15:{mtf_verdict.m15_direction} M5:{mtf_verdict.m5_direction} M1:{mtf_verdict.m1_direction} | "
                                    f"Bonus: {mtf_bonus:+.0f} pts | Confidence: {mtf_verdict.confidence:.2f}"
                                )
                            else:
                                logger.debug(f"[{asset}] PriceMemoryAnalyzer non disponible pour MTF")
                        except Exception as e_mtf_verdict:
                            logger.error(f"[{asset}] Erreur MTF verdict: {e_mtf_verdict}", exc_info=True)
                            mtf_verdict = None
                            mtf_bonus = 0.0
                            mtf_direction = "NEUTRAL"

                        # Préparer asset_signals avec mtf_direction imposée
                        asset_signals_for_of = {
                            "footprint_summary": {},
                            "orderflow_summary": {},
                            "ticks_df": ticks_df,
                            "df_m15": rates_df_m15,
                            "df_m30": rates_df_m30,
                            "mtf_direction": mtf_direction  # 12 FEV 2026: direction imposée par PMA
                        }

                        # Appel OrderFlow V6
                        of_v6_result = scalping_strategy._analyze_orderflow_v6(
                            asset=asset,
                            df_m1=rates_df_fresh,
                            df_m3=None,
                            df_m5=rates_df_m5,
                            asset_signals=asset_signals_for_of
                        )

                        # Extraire résultats
                        orderflow_result_mini = {
                            "score": of_v6_result.get("total_score", 0.0),
                            "bias": of_v6_result.get("bias", "NEUTRAL"),
                            "summary": of_v6_result
                        }

                        logger.info(
                            f"[ORDERFLOW][{asset}] score={orderflow_result_mini['score']:.1f}/100 | "
                            f"bias={orderflow_result_mini['bias']}"
                        )

                        # ✅ NOUVEAU (03 JAN 2026): Calcul composite score avec SimpleAdvancedScorer
                        # 🆕 06 JAN 2026 PHASE 3: Ajout institutional_analysis!
                        if advanced_scorer:
                            try:
                                # 🔧 FIX CRITIQUE (06 JAN 2026): institutional_analysis est dans of_v6_result, PAS orderflow_result_mini!
                                institutional_analysis = of_v6_result.get('institutional_analysis', {})

                                composite_result = advanced_scorer.calculate_composite_score(
                                    ticks_df=ticks_df,
                                    candles_df=rates_df_fresh,
                                    orderflow_score=orderflow_result_mini['score'],
                                    institutional_analysis=institutional_analysis  # 🆕 PHASE 3!
                                )

                                # Remplacer le score OrderFlow V6 par le composite score
                                orderflow_result_mini['score'] = composite_result['composite_score']
                                orderflow_result_mini['composite_details'] = composite_result
                                orderflow_result_mini['composite_enabled'] = True

                                # 06 JAN 2026 PHASE 3: Ajout INST dans les logs!
                                logger.info(
                                    f"[COMPOSITE_SCORE][{asset}] {composite_result['composite_score']:.1f}/100 | "
                                    f"Decision={composite_result['decision']} ({composite_result['confidence']}) | "
                                    f"Components: OF={composite_result['components']['orderflow']:.0f} "
                                    f"INST={composite_result['components']['institutional']:.0f} "
                                    f"MS={composite_result['components']['microstructure']:.0f} "
                                    f"LQ={composite_result['components']['liquidity']:.0f} "
                                    f"DV={composite_result['components']['divergence']:.0f} "
                                    f"SM={composite_result['components']['smart_money']:.0f}"
                                )
                            except Exception as e_composite:
                                logger.error(f"[COMPOSITE_SCORE_ERROR] Erreur: {e_composite}, fallback OrderFlow V6 seul", exc_info=True)
                                orderflow_result_mini['composite_enabled'] = False
                        else:
                            orderflow_result_mini['composite_enabled'] = False

                    except Exception as e_of:
                        logger.critical(f"[ORDERFLOW_V6_ERROR] Erreur: {e_of}", exc_info=True)
                        orderflow_result_mini = {"score": 0.0, "bias": "NEUTRAL", "summary": {}, "composite_enabled": False}

                # ═══════════════════════════════════════════════════════════════
                # 🧠 PRICE MEMORY TREND ANALYSIS (08 JAN 2026)
                # Détecte la tendance historique sur 15 bougies M1 (réactivité micro-tendances)
                # ═══════════════════════════════════════════════════════════════
                # 🔧 25 JAN 2026: Définir current_price avant le bloc try pour éviter UnboundLocalError
                latest_candle_for_memory = market_results.get("latest", {})
                current_price = latest_candle_for_memory.get('close', 0.0) if latest_candle_for_memory else 0.0

                try:
                    # Vérifier que l'analyseur est disponible
                    if price_memory_analyzer is not None:

                        # 🔧 08 JAN 2026: Utiliser seulement les 15 dernières bougies (au lieu de 50)
                        # Raison: 50 bougies dilue les micro-tendances (4 bougies +12 pips → NET +1 pip)
                        # Avec 15 bougies: 4 bougies +12 pips → NET +8-10 pips (détection claire!)
                        lookback_bars = 15
                        recent_candles = rates_df_fresh.tail(lookback_bars) if len(rates_df_fresh) >= lookback_bars else rates_df_fresh

                        trend_structure = price_memory_analyzer.analyze_trend_structure(
                            historical_data=recent_candles,
                            current_price=current_price
                        )

                        # Extraire les informations clés
                        memory_trend_direction = trend_structure.get('trend_direction', 'RANGE')  # BULLISH/BEARISH/RANGE
                        memory_trend_strength = trend_structure.get('trend_strength', 0.0)         # 0.0-1.0
                        memory_net_disp = trend_structure.get('net_displacement', {})
                        memory_net_pips = memory_net_disp.get('net_pips', 0.0)
                        memory_net_direction = memory_net_disp.get('net_direction', 'FLAT')
                        memory_clarity = memory_net_disp.get('trend_clarity', 0.0)

                        logger.info(
                            f"[PRICE_MEMORY_TREND][{asset}] {memory_trend_direction} "
                            f"(strength={memory_trend_strength:.2f}) | "
                            f"Net: {memory_net_direction} {memory_net_pips:+.1f} pips ({lookback_bars} bars) | "
                            f"Clarity: {memory_clarity:.2f}"
                        )
                    else:
                        # PriceMemoryAnalyzer non disponible
                        memory_trend_direction = "RANGE"
                        memory_trend_strength = 0.0
                        memory_net_pips = 0.0
                        memory_net_direction = "FLAT"
                        memory_clarity = 0.0

                except Exception as e_memory_trend:
                    logger.error(f"[{asset}] Erreur Price Memory Trend: {e_memory_trend}", exc_info=True)
                    # Valeurs par défaut en cas d'erreur
                    memory_trend_direction = "RANGE"
                    memory_trend_strength = 0.0
                    memory_net_pips = 0.0
                    memory_net_direction = "FLAT"
                    memory_clarity = 0.0

                # 12 FEV 2026: MTF verdict déjà calculé AVANT l'orderflow (voir plus haut)
                # Appliquer le bonus MTF au score composite (M30+M15+M5+M1 alignés)
                if mtf_bonus > 0 and orderflow_result_mini.get('composite_enabled'):
                    score_before = orderflow_result_mini['score']
                    orderflow_result_mini['score'] = min(100.0, score_before + mtf_bonus)
                    logger.info(
                        f"[MTF_BONUS][{asset}] +{mtf_bonus:.0f} pts appliqué au composite: "
                        f"{score_before:.1f} → {orderflow_result_mini['score']:.1f}"
                    )

                # ═══════════════════════════════════════════════════════════════
                # 🆕 25 JAN 2026: MICRO-RÉSISTANCES M1 (Scalping)
                # Détecte les niveaux de résistance proches pour éviter trades BUY contre résistance
                # ═══════════════════════════════════════════════════════════════
                micro_resistance = None
                micro_resistance_info = {
                    'micro_resistance': None,
                    'distance_pips': 0.0,
                    'bounce_probability': 0.0,
                    'strength': 'NONE'
                }
                try:
                    if price_memory_analyzer is not None and rates_df_fresh is not None:
                        micro_resistance_info = price_memory_analyzer.detect_micro_resistance_m1(
                            historical_data=rates_df_fresh,
                            current_price=current_price,
                            lookback_minutes=15
                        )
                        micro_resistance = micro_resistance_info.get('micro_resistance')

                        if micro_resistance and micro_resistance_info.get('strength') in ['STRONG', 'MODERATE']:
                            logger.info(
                                f"🚧 [MICRO_RESISTANCE][{asset}] Niveau: {micro_resistance:.5f} | "
                                f"Distance: {micro_resistance_info['distance_pips']:.2f} pips | "
                                f"Bounce prob: {micro_resistance_info['bounce_probability']:.0%} | "
                                f"Strength: {micro_resistance_info['strength']}"
                            )

                except Exception as e_micro_res:
                    logger.debug(f"[{asset}] Erreur micro-résistance: {e_micro_res}")

                # ═══════════════════════════════════════════════════════════════
                # 🏛️ 25 JAN 2026: INSTITUTIONAL REVERSAL DETECTOR
                # Analyse: Wyckoff, ML Patterns, Divergences CVD, Changepoint
                # + VETO FATIGUE (Circuit Breaker)
                # ═══════════════════════════════════════════════════════════════
                inst_result = None
                inst_score = 0.0
                inst_veto_fatigue = False
                inst_veto_reversal = False  # 05 FEV 2026: VETO DIRECT si reversal détecté

                try:
                    if institutional_detector is not None and rates_df_fresh is not None:
                        # 08 FEV 2026: Extraire CVD et Delta depuis les données M1
                        # Le volume_analyzer ajoute 'delta' et 'cvd' au DataFrame
                        # On les calcule ici si absents (fallback tick_volume signé)
                        _ird_cvd_values = []
                        _ird_delta_values = []
                        try:
                            if 'cvd' in rates_df_fresh.columns and 'delta' in rates_df_fresh.columns:
                                # Colonnes déjà calculées par OrderFlow V6
                                _ird_cvd_values = rates_df_fresh['cvd'].dropna().tolist()
                                _ird_delta_values = rates_df_fresh['delta'].dropna().tolist()
                            else:
                                # Fallback: calculer delta et CVD depuis tick_volume
                                import numpy as np
                                _close = rates_df_fresh['close'].values
                                _prev = np.roll(_close, 1)
                                _prev[0] = _close[0]
                                _sign = np.where(_close >= _prev, 1.0, -1.0)
                                _vol_col = 'tick_volume' if 'tick_volume' in rates_df_fresh.columns else 'volume'
                                _vol = rates_df_fresh[_vol_col].fillna(0).values.astype(float)
                                _delta_arr = _sign * _vol
                                _cvd_arr = np.cumsum(_delta_arr)
                                _ird_delta_values = _delta_arr.tolist()
                                _ird_cvd_values = _cvd_arr.tolist()
                            logger.debug(
                                f"[IRD_DATA][{asset}] CVD: {len(_ird_cvd_values)} vals, "
                                f"Delta: {len(_ird_delta_values)} vals"
                            )
                        except Exception as e_ird_data:
                            logger.warning(f"[IRD_DATA][{asset}] Erreur extraction CVD/Delta: {e_ird_data}")

                        # Préparer les données pour le détecteur
                        market_data_for_ird = {
                            'candles_m5': rates_df_m5 if rates_df_m5 is not None else pd.DataFrame(),
                            'candles_m1': rates_df_fresh,
                            'cvd_values': _ird_cvd_values,
                            'delta_values': _ird_delta_values,
                            'volume_values': rates_df_fresh['volume'].tolist() if 'volume' in rates_df_fresh.columns else []
                        }

                        # Appel au détecteur
                        inst_result = institutional_detector.detect_reversal(market_data_for_ird)
                        inst_score = inst_result.get('institutional_score', 0.0)

                        # Log du résultat
                        conviction = inst_result.get('conviction_level', 'LOW')
                        new_trend = inst_result.get('new_trend', 'NEUTRAL')
                        reversal = inst_result.get('reversal_detected', False)

                        emoji = "🏛️" if inst_score >= 60 else "📊"
                        logger.info(
                            f"{emoji} [INSTITUTIONAL][{asset}] Score: {inst_score:.1f}/100 | "
                            f"Conviction: {conviction} | Trend: {new_trend} | "
                            f"Reversal: {'✅' if reversal else '❌'}"
                        )

                        # ════════════════════════════════════════════════════
                        # 🚫 VETO FATIGUE (Circuit Breaker)
                        # Si le marché est épuisé, bloquer le trade
                        # ════════════════════════════════════════════════════
                        fatigue_signal = institutional_detector.get_fatigue_signal(market_data_for_ird)
                        fatigue_strength = fatigue_signal.strength
                        fatigue_threshold = institutional_detector.config.get('thresholds', {}).get('fatigue_veto_threshold', 80)

                        if fatigue_strength >= fatigue_threshold:
                            inst_veto_fatigue = True
                            logger.warning(
                                f"⚡ [CIRCUIT_BREAKER][{asset}] VETO FATIGUE | "
                                f"Marché épuisé (Score: {fatigue_strength:.0f}/{fatigue_threshold}) | "
                                f"Trade bloqué pour sécurité"
                            )

                except Exception as e_inst:
                    logger.error(f"[INSTITUTIONAL][{asset}] Erreur: {e_inst}", exc_info=True)

                # ========== ÉTAPE 2: TIMING GATEKEEPER (GO/NOGO TRADE) ==========
                timing_verdict = None
                # 🔧 FIX (03 JAN 2026): Initialiser fusion_out pour éviter UnboundLocalError
                fusion_out = {
                    "ok": False,
                    "action": "HOLD",
                    "fused_confidence": 0.0,
                    "signal_type": "NOT_INITIALIZED",
                    "veto_reason": "Fusion not completed",
                    "orderflow_score": 0.0
                }
                try:
                    # Récupérer ticks pour analyse liquidité
                    # 🔧 FIX (29 DEC 2025): Simplifier check et logger le résultat
                    ticks_for_timing = ticks_df if (ticks_df is not None and not ticks_df.empty) else None

                    if ticks_for_timing is not None:
                        logger.info(f"[TIMING_PREP] ✅ Passage {len(ticks_for_timing)} ticks au gatekeeper")
                    else:
                        logger.warning(f"[TIMING_PREP] ⚠️ AUCUN tick disponible pour gatekeeper → tick_rate=0")

                    # 🔧 29 DEC 2025: Passer la config scalping GLOBALE (prioritaire) + asset config (fallback)
                    scalping_config_global = strategy_manager.get_strategy_config("scalping") if strategy_manager else {}
                    # 🐛 FIX (02 JAN 2026): Utiliser load_asset_config au lieu de get_asset_config
                    asset_config_timing = config_manager.load_asset_config(asset)

                    # Appel gatekeeper
                    timing_verdict = evaluate_trading_conditions(
                        asset=asset,
                        current_time=pd.Timestamp.now(tz='UTC'),
                        ticks_df=ticks_for_timing,
                        market_context={},
                        asset_config=asset_config_timing,
                        scalping_config=scalping_config_global
                    )

                    verdict_str = "✅ PASS" if timing_verdict['verdict'] == "PASS" else f"❌ VETO ({timing_verdict.get('veto_reason', 'N/A')})"
                    logger.info(
                        f"[TIMING_GATEKEEPER][{asset}] {verdict_str} | "
                        f"session={timing_verdict.get('quality_metrics', {}).get('session', 'N/A')} | "
                        f"tick_rate={timing_verdict.get('quality_metrics', {}).get('tick_rate', 0):.1f}/s"
                    )
                except Exception as e_timing:
                    logger.error(f"[TIMING_GATEKEEPER] Erreur: {e_timing}", exc_info=True)
                    timing_verdict = {"verdict": "VETO", "veto_reason": f"Timing error: {e_timing}", "quality_metrics": {}}

                # ========== ÉTAPE 3: DÉCISION INTELLIGENTE (02 JAN 2026 - Veto pondéré) ==========
                # Système intelligent : Signal OrderFlow fort peut passer outre veto modéré
                veto_score = timing_verdict.get("veto_score", 0.0) if timing_verdict else 0.0
                orderflow_score_raw = orderflow_result_mini['score']

                # ═══════════════════════════════════════════════════════════════
                # Score OrderFlow brut (le PMA ajustera ce score plus tard en post-processing)
                orderflow_score = orderflow_score_raw

                # 🎯 LOGIQUE INTELLIGENTE (02 JAN 2026)
                # Signal exceptionnel (≥85) peut passer outre veto modéré (< 60)
                # Signal très fort (≥90) peut passer outre veto fort (< 70)
                can_override_veto = False
                override_reason = None

                if orderflow_score >= 90.0 and veto_score < 70.0:
                    can_override_veto = True
                    override_reason = f"Signal exceptionnel ({orderflow_score:.0f}/100) > veto ({veto_score:.0f}/100)"
                elif orderflow_score >= 85.0 and veto_score < 60.0:
                    can_override_veto = True
                    override_reason = f"Signal très fort ({orderflow_score:.0f}/100) > veto modéré ({veto_score:.0f}/100)"

                # Décision finale
                timing_blocks_trade = (
                    timing_verdict
                    and timing_verdict.get("verdict") != "PASS"
                    and not can_override_veto  # ✅ NOUVEAU: Signal fort peut passer outre
                )

                # 🔍 LOG DEBUG (05 JAN 2026): Tracer la décision
                logger.critical(
                    f"[DECISION_LOGIC][{asset}] timing_blocks_trade={timing_blocks_trade} | "
                    f"can_override_veto={can_override_veto} | "
                    f"timing_verdict={timing_verdict.get('verdict') if timing_verdict else None} | "
                    f"orderflow_score={orderflow_score:.1f} | "
                    f"veto_score={veto_score:.1f}"
                )

                if timing_blocks_trade:
                    # VETO timing trop fort → HOLD
                    logger.critical(f"[DECISION_BRANCH][{asset}] ➡️ BRANCHE 1: TIMING_VETO (score={orderflow_score:.1f} < 85, veto={veto_score:.1f})")
                    veto_reason = timing_verdict.get('veto_reason', 'Unknown')
                    logger.info(
                        f"⚠️  [TIMING_VETO] {veto_reason} (veto={veto_score:.0f}) "
                        f"→ HOLD (OrderFlow score={orderflow_score:.1f} insuffisant pour override)"
                    )

                    decision_mini = {
                        "action": "HOLD",
                        "confidence": 0.0,
                        "rationale": f"TIMING VETO: {veto_reason} (OrderFlow {orderflow_score:.0f}/100 < override threshold)",
                        "anchor_price": None
                    }

                    fusion_out = {
                        "ok": False,
                        "action": "HOLD",
                        "fused_confidence": 0.0,
                        "signal_type": "TIMING_VETO",
                        "veto_reason": veto_reason,
                        "veto_score": veto_score,
                        "orderflow_score": orderflow_score
                    }
                elif can_override_veto:
                    # ✅ OVERRIDE: Signal fort passe outre veto modéré
                    logger.critical(f"[DECISION_BRANCH][{asset}] ➡️ BRANCHE 2: OVERRIDE_VETO (score={orderflow_score:.1f} ≥ 85)")
                    logger.info(
                        f"🚀 [VETO_OVERRIDE] {override_reason} → Signal autorisé malgré timing non optimal"
                    )
                    # PASS timing → Vérifier phase avant de décider

                    # ========================================================================
                    # 🚨 NIVEAU 2 : VETO PHASE DE MARCHÉ DYNAMIQUE (29 DEC 2025)
                    # ========================================================================
                    # INTERDICTION STRICTE : NE JAMAIS TRADER en phase RANGE ou ACCUMULATION
                    # Lecture depuis config SCALPING GLOBALE (dynamique)
                    blocked_phases_config = scalping_config_global.get("entry_rules", {}).get("scalping", {}).get("blocked_phases", {})
                    blocked_phases_enabled = blocked_phases_config.get("enabled", True)
                    blocked_phases_list = blocked_phases_config.get("phases", ["range", "accumulation", "range_accumulation", "range_distribution"])

                    # 🔧 FIX (29 DEC 2025): Récupérer le régime depuis latest["regime"], pas "phase"
                    # PhaseObserver stocke le régime dans annotated_df["regime"] → latest["regime"]
                    latest_candle = market_results.get("latest", {})
                    current_regime = latest_candle.get("regime", "unknown")
                    phase_str = str(current_regime).lower() if current_regime else "unknown"

                    # Vérifier si régime contient un mot-clé bloqué
                    phase_is_blocked = blocked_phases_enabled and any(blocked in phase_str for blocked in blocked_phases_list)

                    # 🔍 LOG: Config phase veto
                    logger.critical(
                        f"[PHASE_CONFIG_CHECK][{asset}] blocked_phases={blocked_phases_list} | "
                        f"enabled={blocked_phases_enabled} | current_regime={phase_str} | is_blocked={phase_is_blocked}"
                    )

                    if phase_is_blocked:
                        logger.critical(
                            f"[REGIME_CONFIG_VETO][{asset}] 🚫 RÉGIME '{phase_str}' INTERDIT ! "
                            f"Régimes bloqués (config): {blocked_phases_list}"
                        )

                        decision_mini = {
                            "action": "HOLD",
                            "confidence": 0.0,
                            "rationale": f"REGIME VETO: Régime '{phase_str}' interdit (range/accumulation bloqué)",
                            "anchor_price": None
                        }

                        fusion_out = {
                            "ok": False,
                            "action": "HOLD",
                            "fused_confidence": 0.0,
                            "signal_type": "REGIME_VETO",
                            "veto_reason": f"Régime '{phase_str}' interdit",
                            "orderflow_score": orderflow_result_mini["score"]
                        }

                        logger.info(
                            f"[MINIMALIST][{asset}] HOLD | rationale=REGIME VETO: {phase_str}"
                        )
                    else:
                        # PASS timing + PASS phase → Décision basée sur OrderFlow
                        # 🎯 (05 JAN 2026): Lire min_score DYNAMIQUEMENT depuis asset config
                        asset_min_score_worker = 65.0  # Default sniper (si composite: ~65, si OrderFlow seul: ~70)
                        try:
                            aconf_worker = config_manager.config_loader.load_asset_config(asset) or {}
                            asset_min_score_worker = float(
                                (aconf_worker.get("overrides", {}) or {})
                                .get("scalping", {})
                                .get("entry_rules", {})
                                .get("scalping", {})
                                .get("burst_scalping", {})
                                .get("min_score", asset_min_score_worker)
                            )
                            logger.debug(f"[CONFIG_WORKER][{asset}] min_score={asset_min_score_worker} (from asset config)")
                        except Exception as e_min_score_worker:
                            logger.warning(f"[CONFIG_WORKER][{asset}] Erreur lecture min_score: {e_min_score_worker}, using default={asset_min_score_worker}")

                        try:
                            decision_mini = market_analyzer_thread.build_decision(
                                orderflow_result=orderflow_result_mini,
                                min_score=asset_min_score_worker
                            ) if market_analyzer_thread else {"action": "HOLD", "confidence": 0.0, "rationale": "MarketAnalyzer unavailable"}

                            logger.info(
                                f"[DECISION][{asset}] action={decision_mini['action']} | "
                                f"confidence={decision_mini['confidence']:.2f} | "
                                f"rationale={decision_mini['rationale']}"
                            )
                        except Exception as e_decision:
                            logger.error(f"[DECISION] Erreur: {e_decision}", exc_info=True)
                            decision_mini = {"action": "HOLD", "confidence": 0.0, "rationale": f"Decision error: {e_decision}"}

                        # ═══════════════════════════════════════════════════════════════
                        # 🧠 PRICE MEMORY VETO/BOOST (08 JAN 2026) - DÉSACTIVÉ
                        # ⚠️ REMPLACÉ PAR LOGIQUE PRICE-FIRST CI-DESSOUS
                        # Cette section est conservée pour référence historique
                        # ═══════════════════════════════════════════════════════════════
                        # (Ancienne logique où OrderFlow décidait, puis Price Memory faisait VETO)
                        # NOUVELLE LOGIQUE: Price Memory décide d'abord, Delta confirme
                        # Voir section "PRICE-FIRST DECISION" plus bas

                        # ═══════════════════════════════════════════════════════════════
                        # 📊 MOMENTUM CALCULATION (08 JAN 2026)
                        # Calcule le mouvement de prix sur N bougies (utilisé par PRICE-FIRST)
                        # ⚠️ Ne fait PLUS de VETO ici - décision prise par PRICE-FIRST ci-dessous
                        # ═══════════════════════════════════════════════════════════════
                        price_change_pips = 0.0  # Initialiser
                        try:
                            # Lire config momentum_filter depuis asset config
                            momentum_config = asset_cfg.get("overrides", {}).get("scalping", {}).get("orderflow_v6", {}).get("momentum_filter", {})
                            momentum_enabled = momentum_config.get("enabled", False)

                            if momentum_enabled:
                                lookback_bars = momentum_config.get("lookback_bars", 3)

                                # Calculer le mouvement de prix sur les N dernières bougies
                                if rates_df_fresh is not None and len(rates_df_fresh) >= lookback_bars:
                                    # Prix de départ (bougie -N) et prix actuel (bougie -1)
                                    price_start = rates_df_fresh.iloc[-(lookback_bars + 1)]['close']
                                    price_current = rates_df_fresh.iloc[-1]['close']
                                    price_change_points = (price_current - price_start) / point if point > 0 else 0.0

                                    # Convertir en pips selon le nombre de digits
                                    if digits in (3, 5):
                                        price_change_pips = price_change_points / 10.0
                                    else:
                                        price_change_pips = price_change_points

                                    logger.info(
                                        f"[MOMENTUM_CALC][{asset}] Mouvement sur {lookback_bars} bougies: "
                                        f"{price_start:.5f} → {price_current:.5f} = {price_change_pips:+.1f} pips"
                                    )

                        except Exception as e_momentum:
                            logger.error(f"[MOMENTUM_CALC][{asset}] Erreur: {e_momentum}", exc_info=True)
                            price_change_pips = 0.0

                        # ═══════════════════════════════════════════════════════════════
                        # 🎯 PRICE-FIRST DECISION (08 JAN 2026)
                        # Le PRIX décide, le DELTA confirme (bonus/malus)
                        # ═══════════════════════════════════════════════════════════════
                        try:
                            # Lire les seuils depuis config
                            momentum_config = asset_cfg.get("overrides", {}).get("scalping", {}).get("orderflow_v6", {}).get("momentum_filter", {})
                            min_price_change_pips = momentum_config.get("min_price_change_pips", 5.0)

                            # Seuil pour Price Memory strength
                            # ═══════════════════════════════════════════════════════════
                            # 🎯 TRIPLE FILTRE - DELTA CONFIRMÉ (08 JAN 2026 v5 FINALE)
                            # Filtre 1: Delta Pondéré Multi-TF (M1 + M3)
                            # Filtre 2: Microstructure (Volume, Tickrate, Coverage)
                            # Filtre 3: Contexte (Fatigue, Price Memory)
                            # ═══════════════════════════════════════════════════════════

                            # Récupération données
                            delta_m1 = orderflow_result_mini.get("summary", {}).get("delta", 0.0)
                            cvd_slope = orderflow_result_mini.get("summary", {}).get("cvd_slope", 0.0)
                            vol_ratio = orderflow_result_mini.get("summary", {}).get("vol_ratio", 1.0)
                            original_score = orderflow_result_mini.get("score", 0.0)

                            # Footprint data
                            fp_summary = orderflow_result_mini.get("footprint_summary", {})
                            tickrate = fp_summary.get("tickrate", 0.0)
                            coverage_s = fp_summary.get("coverage_s", 0.0)

                            # ═══════════════════════════════════════════════════════════
                            # FILTRE 1: DELTA PONDÉRÉ MULTI-TIMEFRAME
                            # ═══════════════════════════════════════════════════════════
                            delta_weighted = 0.0
                            delta_m3 = 0.0

                            if 'price_change_pips' in locals():
                                # Convertir momentum M3 en "delta equivalent"
                                delta_m3 = price_change_pips
                                # Pondération: M1 (60%) + M3 (40%)
                                delta_weighted = (delta_m1 * 0.6) + (delta_m3 * 0.4)
                            else:
                                delta_weighted = delta_m1

                            # Vérifier cohérence CVD
                            cvd_aligned = False
                            if delta_weighted > 0 and cvd_slope > 0:
                                cvd_aligned = True
                            elif delta_weighted < 0 and cvd_slope < 0:
                                cvd_aligned = True

                            # Seuils pour direction (adaptés par asset via min_price_change_pips)
                            delta_threshold = min_price_change_pips * 3  # Ex: 3 pips → seuil 9

                            filtre1_direction = "HOLD"
                            filtre1_confidence = 0.0

                            # ═══════════════════════════════════════════════════════════════
                            # 🎯 12 FEV 2026: FILTRE 1 DELTA - RESPECTE MTF QUEEN V2
                            # Le bias de scalping.py (filtre par MTF Queen M30+M15+M5+M1)
                            # est la REFERENCE. Le Triple Filter ne peut PAS le contredire.
                            # ═══════════════════════════════════════════════════════════════

                            # Directions MTF pour logging
                            m30_dir = mtf_verdict.m30_direction if mtf_verdict else "NO_DATA"
                            m15_dir = mtf_verdict.m15_direction if mtf_verdict else "NO_DATA"
                            m5_dir = mtf_verdict.m5_direction if mtf_verdict else "NO_DATA"
                            m1_dir = mtf_verdict.m1_direction if mtf_verdict else "NO_DATA"

                            # Bias MTF Queen (vient de scalping.py _analyze_orderflow_v6)
                            of_bias = orderflow_result_mini.get("bias", "NEUTRAL")

                            # Si MTF Queen a dit NEUTRAL → pas de trade, le Triple Filter respecte
                            if of_bias == "NEUTRAL":
                                filtre1_direction = "HOLD"
                                filtre1_confidence = 0.0
                                logger.info(
                                    f"[FILTRE1_DELTA][{asset}] HOLD - MTF Queen VETO (bias=NEUTRAL) | "
                                    f"Delta={delta_weighted:.1f} | MTF: M15:{m15_dir} M5:{m5_dir} M1:{m1_dir}"
                                )

                            # BUY : Bias autorise par MTF Queen ET delta positif au-dessus du seuil
                            elif of_bias == "BUY" and delta_weighted > 0 and abs(delta_weighted) >= delta_threshold:
                                filtre1_direction = "BUY"
                                filtre1_confidence = min(1.0, abs(delta_weighted) / (delta_threshold * 3))
                                if cvd_aligned:
                                    filtre1_confidence = min(1.0, filtre1_confidence + 0.10)
                                logger.info(
                                    f"[FILTRE1_DELTA][{asset}] BUY | Delta={delta_weighted:.1f} | bias={of_bias} | "
                                    f"MTF: M15:{m15_dir} M5:{m5_dir} M1:{m1_dir}"
                                )

                            # SELL : Bias autorise par MTF Queen ET delta negatif au-dessus du seuil
                            elif of_bias == "SELL" and delta_weighted < 0 and abs(delta_weighted) >= delta_threshold:
                                filtre1_direction = "SELL"
                                filtre1_confidence = min(1.0, abs(delta_weighted) / (delta_threshold * 3))
                                if cvd_aligned:
                                    filtre1_confidence = min(1.0, filtre1_confidence + 0.10)
                                logger.info(
                                    f"[FILTRE1_DELTA][{asset}] SELL | Delta={delta_weighted:.1f} | bias={of_bias} | "
                                    f"MTF: M15:{m15_dir} M5:{m5_dir} M1:{m1_dir}"
                                )

                            # HOLD : Delta trop faible ou direction incoherente avec bias
                            else:
                                logger.debug(
                                    f"[FILTRE1_DELTA][{asset}] HOLD - Delta={delta_weighted:.1f} seuil={delta_threshold:.1f} bias={of_bias} | "
                                    f"MTF: M15:{m15_dir} M5:{m5_dir} M1:{m1_dir}"
                                )

                            filtre1_status = "✅ PASS" if filtre1_direction in ["BUY", "SELL"] else "❌ FAIL"
                            filtre1_detail = f"Δw={delta_weighted:.1f} | MTF: M15:{m15_dir} M5:{m5_dir} M1:{m1_dir} | CVD:{cvd_slope:.2f}"

                            # ═══════════════════════════════════════════════════════════
                            # FILTRE 2: MICROSTRUCTURE
                            # ═══════════════════════════════════════════════════════════
                            conditions_micro = []

                            # 1. Volume fort (>150% moyenne)
                            volume_strong = vol_ratio > 1.5
                            conditions_micro.append(("Volume>150%", volume_strong, f"ratio={vol_ratio:.2f}"))

                            # 2. Activité élevée (>2 ticks/sec) - Ajuster selon asset
                            tickrate_min_threshold = 2.0  # Default
                            if asset == "USDCHF":
                                tickrate_min_threshold = 1.8
                            elif asset == "USDJPY":
                                tickrate_min_threshold = 1.5

                            activity_high = tickrate >= tickrate_min_threshold
                            conditions_micro.append(("Ticks>min", activity_high, f"{tickrate:.1f} (min={tickrate_min_threshold})"))

                            # 3. Pas de gaps (coverage > 5s)
                            no_gaps = coverage_s >= 5.0
                            conditions_micro.append(("Coverage>5s", no_gaps, f"{coverage_s:.1f}s"))

                            # 4. CVD aligné
                            conditions_micro.append(("CVD aligned", cvd_aligned, f"slope={cvd_slope:.2f}"))

                            # Score microstructure (3/4 conditions = PASS)
                            micro_passed = sum(1 for _, passed, _ in conditions_micro if passed)
                            filtre2_pass = micro_passed >= 3
                            filtre2_status = f"✅ PASS ({micro_passed}/4)" if filtre2_pass else f"❌ FAIL ({micro_passed}/4)"
                            filtre2_detail = " | ".join([f"{name}:{'✅' if p else '❌'}({d})" for name, p, d in conditions_micro])

                            # ═══════════════════════════════════════════════════════════
                            # FILTRE 3: CONTEXTE
                            # ═══════════════════════════════════════════════════════════
                            conditions_context = []

                            # 1. Fatigue acceptable
                            fatigue_ok = True  # Par défaut OK (on désactive le veto global)
                            conditions_context.append(("Fatigue OK", fatigue_ok, "N/A"))

                            # 2. Price Memory fresh (>50%)
                            memory_fresh = memory_clarity >= 0.5 if 'memory_clarity' in locals() else True
                            conditions_context.append(("Memory fresh", memory_fresh, f"clarity={memory_clarity:.0%}" if 'memory_clarity' in locals() else "N/A"))

                            # 3. Alignement Price Memory (bonus si aligné)
                            memory_aligned = False
                            if filtre1_direction == "BUY" and memory_trend_direction == "BULLISH":
                                memory_aligned = True
                            elif filtre1_direction == "SELL" and memory_trend_direction == "BEARISH":
                                memory_aligned = True
                            conditions_context.append(("Memory aligned", memory_aligned, f"{memory_trend_direction}"))

                            # Score contexte (2/3 = PASS)
                            context_passed = sum(1 for _, passed, _ in conditions_context if passed)
                            filtre3_pass = context_passed >= 2
                            filtre3_status = f"✅ PASS ({context_passed}/3)" if filtre3_pass else f"❌ FAIL ({context_passed}/3)"
                            filtre3_detail = " | ".join([f"{name}:{'✅' if p else '❌'}({d})" for name, p, d in conditions_context])

                            # ═══════════════════════════════════════════════════════════
                            # DÉCISION FINALE: LES 3 FILTRES DOIVENT PASSER
                            # ═══════════════════════════════════════════════════════════
                            all_filters_pass = filtre1_direction in ["BUY", "SELL"] and filtre2_pass and filtre3_pass

                            # Bonus si Price Memory aligné
                            bonus_memory = 30 if memory_aligned else 0
                            adjusted_score = original_score + bonus_memory

                            if all_filters_pass and adjusted_score >= asset_min_score_worker:
                                # ✅ Signal validé - TOUS LES FILTRES PASSENT
                                decision_mini["action"] = filtre1_direction
                                decision_mini["confidence"] = filtre1_confidence
                                decision_mini["rationale"] = (
                                    f"TRIPLE_FILTER: {filtre1_direction} | "
                                    f"F1:{filtre1_status} F2:{filtre2_status} F3:{filtre3_status} | "
                                    f"Score: {original_score:.1f}+{bonus_memory} = {adjusted_score:.1f}"
                                )

                                orderflow_result_mini["score"] = adjusted_score

                                logger.info(
                                    f"[TRIPLE_FILTER][{asset}] ✅ {filtre1_direction} VALIDÉ | "
                                    f"F1: {filtre1_detail} | "
                                    f"F2: {filtre2_detail} | "
                                    f"F3: {filtre3_detail} | "
                                    f"Score: {original_score:.1f} → {adjusted_score:.1f}"
                                )
                            else:
                                # ⏸️ HOLD - AU MOINS UN FILTRE A ÉCHOUÉ
                                decision_mini["action"] = "HOLD"
                                decision_mini["confidence"] = 0.0

                                # Déterminer raison du rejet
                                reject_reasons = []
                                if filtre1_direction == "HOLD":
                                    reject_reasons.append(f"F1_FAIL(Δw={delta_weighted:.1f}<{delta_threshold:.1f})")
                                if not filtre2_pass:
                                    reject_reasons.append(f"F2_FAIL({micro_passed}/4)")
                                if not filtre3_pass:
                                    reject_reasons.append(f"F3_FAIL({context_passed}/3)")
                                if adjusted_score < asset_min_score_worker:
                                    reject_reasons.append(f"SCORE({adjusted_score:.1f}<{asset_min_score_worker})")

                                reject_str = " + ".join(reject_reasons)

                                decision_mini["rationale"] = f"TRIPLE_FILTER: HOLD - {reject_str}"

                                logger.info(
                                    f"[TRIPLE_FILTER][{asset}] ⏸️ HOLD | "
                                    f"Direction: {filtre1_direction} | "
                                    f"F1: {filtre1_detail} | "
                                    f"F2: {filtre2_detail} | "
                                    f"F3: {filtre3_detail} | "
                                    f"Rejet: {reject_str}"
                                )

                        except Exception as e_triple_filter:
                            logger.error(f"[TRIPLE_FILTER][{asset}] Erreur: {e_triple_filter}", exc_info=True)
                            # En cas d'erreur, garder la décision OrderFlow originale

                        # ═══════════════════════════════════════════════════════════════
                        # 🧠 08 FEV 2026: PMA ADJUSTMENTS (via fonction réutilisable)
                        # Identique entre Branche 2 et Branche 3
                        # ═══════════════════════════════════════════════════════════════
                        pma_bonus = 0.0
                        pma_malus = 0.0
                        pma_adjustments = []
                        pma_veto_dur = False

                        try:
                            signal_action = decision_mini.get("action", "HOLD")

                            pma_result = apply_pma_adjustments(
                                signal_action=signal_action,
                                orderflow_score=orderflow_score,
                                mtf_verdict=mtf_verdict,
                                mtf_direction=mtf_direction,
                                micro_resistance_info=micro_resistance_info,
                                inst_result=inst_result,
                                inst_score=inst_score,
                                inst_veto_fatigue=inst_veto_fatigue,
                                inst_veto_reversal=inst_veto_reversal,
                                current_regime=current_regime,
                                memory_clarity=memory_clarity,
                                memory_trend_strength=memory_trend_strength,
                                memory_trend_direction=memory_trend_direction,
                                price_memory_analyzer=price_memory_analyzer,
                                rates_df_fresh=rates_df_fresh,
                                current_price=current_price,
                                asset=asset,
                                logger_ref=logger,
                            )

                            # Extraire les résultats
                            pma_bonus = pma_result["pma_bonus"]
                            pma_malus = pma_result["pma_malus"]
                            pma_adjustments = pma_result["pma_adjustments"]
                            pma_veto_dur = pma_result["pma_veto_dur"]
                            inst_veto_reversal = pma_result["inst_veto_reversal"]
                            score_brut = pma_result["score_brut"]
                            score_ajuste = pma_result["score_ajuste"]
                            pma_adjustment_total = pma_result["pma_adjustment"]

                            # Stocker dans orderflow_result_mini pour traçabilité
                            orderflow_result_mini['pma_bonus'] = pma_bonus
                            orderflow_result_mini['pma_malus'] = pma_malus
                            orderflow_result_mini['pma_adjustment'] = pma_adjustment_total
                            orderflow_result_mini['pma_adjustments'] = pma_adjustments
                            orderflow_result_mini['score_before_pma'] = score_brut
                            orderflow_result_mini['score'] = score_ajuste

                            # Appliquer VETO DUR
                            if pma_veto_dur and signal_action in ["BUY", "SELL"]:
                                old_action = decision_mini["action"]
                                decision_mini["action"] = "HOLD"
                                decision_mini["confidence"] = 0.0
                                decision_mini["rationale"] = (
                                    f"PMA_VETO_DUR: Score {score_ajuste:.1f} < 60 | "
                                    f"Brut={score_brut:.1f} + Bonus={pma_bonus:.0f} - Malus={pma_malus:.0f} | "
                                    f"Original: {old_action}"
                                )

                            # Appliquer VETO REVERSAL
                            if inst_veto_reversal and signal_action in ["BUY", "SELL"]:
                                old_action = decision_mini["action"]
                                decision_mini["action"] = "HOLD"
                                decision_mini["confidence"] = 0.0
                                decision_mini["rationale"] = (
                                    f"🚫 VETO_REVERSAL: Renversement institutionnel détecté | "
                                    f"Original: {old_action}"
                                )

                        except Exception as e_pma:
                            logger.error(f"[PMA_ADJUSTMENT][{asset}] Erreur: {e_pma}", exc_info=True)

                        # Construction fusion_out
                        if decision_mini["action"] in ["BUY", "SELL"]:
                            anchor_price = decision_mini.get("anchor_price") or (latest.get("current_price") if latest else None) or (latest.get("close") if latest else None)

                            fusion_out = {
                                "ok": True,
                                "action": decision_mini["action"],
                                "fused_confidence": decision_mini["confidence"],
                                "signal_type": "MINIMALIST_ORDERFLOW",
                                "rationale": decision_mini["rationale"],
                                "orderflow_score": orderflow_result_mini["score"],
                                "timing_quality": timing_verdict.get("quality_metrics", {}),
                                "price": anchor_price,
                                "context": ctx,
                                # 🧠 25 JAN 2026: Price Memory Analyzer (Post-Processing)
                                "price_memory": {
                                    "score_brut": orderflow_result_mini.get("score_before_pma", orderflow_result_mini["score"]),
                                    "score_ajuste": orderflow_result_mini["score"],
                                    "pma_bonus": pma_bonus,
                                    "pma_malus": pma_malus,
                                    "pma_adjustment": pma_bonus - pma_malus,
                                    "adjustments": pma_adjustments,
                                    "veto_dur": pma_veto_dur,
                                    "mtf": {
                                        "direction": mtf_direction,
                                        "alignment": mtf_verdict.alignment if mtf_verdict else "N/A",
                                        "m30": mtf_verdict.m30_direction if mtf_verdict else "N/A",
                                        "m15": mtf_verdict.m15_direction if mtf_verdict else "N/A",
                                        "m5": mtf_verdict.m5_direction if mtf_verdict else "N/A",
                                        "m1": mtf_verdict.m1_direction if mtf_verdict else "N/A"
                                    },
                                    "micro_resistance": micro_resistance_info
                                },
                                # 🏛️ 25 JAN 2026: Institutional Reversal Detector
                                "institutional": {
                                    "score": inst_score,
                                    "conviction": inst_result.get('conviction_level', 'N/A') if inst_result else 'N/A',
                                    "trend": inst_result.get('new_trend', 'N/A') if inst_result else 'N/A',
                                    "reversal_detected": inst_result.get('reversal_detected', False) if inst_result else False,
                                    "veto_fatigue": inst_veto_fatigue,
                                    "veto_reversal": inst_veto_reversal  # 05 FEV 2026: VETO DIRECT
                                }
                            }

                            logger.info(
                                f"🎯 [MINIMALIST][{asset}] ✅ {fusion_out['action']} | "
                                f"confidence={fusion_out['fused_confidence']:.2f} | "
                                f"Score: {orderflow_result_mini.get('score_before_pma', orderflow_result_mini['score']):.1f} → {orderflow_result_mini['score']:.1f}"
                            )
                        else:
                            fusion_out = {
                                "ok": False,
                                "action": "HOLD",
                                "fused_confidence": 0.0,
                                "signal_type": "MINIMALIST_HOLD",
                                "rationale": decision_mini["rationale"],
                                "orderflow_score": orderflow_result_mini["score"]
                            }

                            logger.info(
                                f"[MINIMALIST][{asset}] HOLD | rationale={decision_mini['rationale']}"
                            )
                else:
                    # ✅ FIX (05 JAN 2026): Cas PASS normal sans override
                    # timing_blocks_trade == False (PASS) ET can_override_veto == False (score < 85)
                    # → Appliquer logique normale: vérifier phase + build_decision()
                    logger.critical(f"[DECISION_BRANCH][{asset}] ➡️ BRANCHE 3: PASS_NORMAL (timing=PASS, score={orderflow_score:.1f} < 85)")

                    # Vérifier REGIME VETO
                    latest_candle = market_results.get("latest", {})
                    current_regime = latest_candle.get("regime", "unknown")
                    phase_str = str(current_regime).lower() if current_regime else "unknown"

                    blocked_phases_config = scalping_config_global.get("entry_rules", {}).get("scalping", {}).get("blocked_phases", {})
                    blocked_phases_enabled = blocked_phases_config.get("enabled", True)
                    blocked_phases_list = blocked_phases_config.get("phases", ["range", "accumulation", "range_accumulation", "range_distribution"])
                    phase_is_blocked = blocked_phases_enabled and any(blocked in phase_str for blocked in blocked_phases_list)

                    if phase_is_blocked:
                        logger.critical(f"[DECISION_BRANCH][{asset}] 🚫 BRANCHE 3.1: REGIME_VETO (phase={phase_str} in {blocked_phases_list})")
                        logger.critical(
                            f"[REGIME_CONFIG_VETO][{asset}] 🚫 RÉGIME '{phase_str}' INTERDIT ! "
                            f"Régimes bloqués (config): {blocked_phases_list}"
                        )

                        decision_mini = {
                            "action": "HOLD",
                            "confidence": 0.0,
                            "rationale": f"REGIME VETO: Régime '{phase_str}' interdit (range/accumulation bloqué)",
                            "anchor_price": None
                        }

                        fusion_out = {
                            "ok": False,
                            "action": "HOLD",
                            "fused_confidence": 0.0,
                            "signal_type": "REGIME_VETO",
                            "veto_reason": f"Régime '{phase_str}' interdit",
                            "orderflow_score": orderflow_result_mini["score"]
                        }

                        logger.info(
                            f"[MINIMALIST][{asset}] HOLD | rationale=REGIME VETO: {phase_str}"
                        )
                    else:
                        # PASS timing + PASS phase → Décision basée sur OrderFlow
                        logger.critical(f"[DECISION_BRANCH][{asset}] ✅ BRANCHE 3.2: BUILD_DECISION (phase={phase_str} OK, score={orderflow_score:.1f})")
                        # 🎯 (05 JAN 2026): Lire min_score DYNAMIQUEMENT depuis asset config
                        asset_min_score_worker = 60.0  # Default sniper (composite scoring)
                        try:
                            aconf_worker = config_manager.config_loader.load_asset_config(asset) or {}
                            asset_min_score_worker = float(
                                (aconf_worker.get("overrides", {}) or {})
                                .get("scalping", {})
                                .get("entry_rules", {})
                                .get("scalping", {})
                                .get("burst_scalping", {})
                                .get("min_score", asset_min_score_worker)
                            )
                            logger.debug(f"[CONFIG_WORKER][{asset}] min_score={asset_min_score_worker} (from asset config)")
                        except Exception as e_min_score_worker:
                            logger.warning(f"[CONFIG_WORKER][{asset}] Erreur lecture min_score: {e_min_score_worker}, using default={asset_min_score_worker}")

                        try:
                            decision_mini = market_analyzer_thread.build_decision(
                                orderflow_result=orderflow_result_mini,
                                min_score=asset_min_score_worker
                            ) if market_analyzer_thread else {"action": "HOLD", "confidence": 0.0, "rationale": "MarketAnalyzer unavailable"}

                            logger.info(
                                f"[DECISION][{asset}] action={decision_mini['action']} | "
                                f"confidence={decision_mini['confidence']:.2f} | "
                                f"rationale={decision_mini['rationale']}"
                            )
                        except Exception as e_decision:
                            logger.error(f"[DECISION] Erreur: {e_decision}", exc_info=True)
                            decision_mini = {"action": "HOLD", "confidence": 0.0, "rationale": f"Decision error: {e_decision}"}

                        # M5M1_BLOCK → SUPPRIMÉ 10 FEV 2026
                        # Le VETO MTF est désormais dans scalping.py (MTF Queen Rule).
                        # La stratégie ne produit plus de BUY/SELL non aligné avec M5+M1.

                        # ═══════════════════════════════════════════════════════════════
                        # 🛡️ 08 FEV 2026: PMA ADJUSTMENTS BRANCHE 3 (identique à Branche 2)
                        # CORRECTION CRITIQUE: Malus/Bonus + Veto Dur + Veto Reversal
                        # étaient ABSENTS de la Branche 3 → BUY passait dans tendance baissière
                        # ═══════════════════════════════════════════════════════════════
                        pma_bonus_b3 = 0.0
                        pma_malus_b3 = 0.0
                        pma_adjustments_b3 = []
                        pma_veto_dur_b3 = False

                        try:
                            signal_action_b3 = decision_mini.get("action", "HOLD")

                            pma_result_b3 = apply_pma_adjustments(
                                signal_action=signal_action_b3,
                                orderflow_score=orderflow_score,
                                mtf_verdict=mtf_verdict,
                                mtf_direction=mtf_direction,
                                micro_resistance_info=micro_resistance_info,
                                inst_result=inst_result,
                                inst_score=inst_score,
                                inst_veto_fatigue=inst_veto_fatigue,
                                inst_veto_reversal=inst_veto_reversal,
                                current_regime=current_regime,
                                memory_clarity=memory_clarity,
                                memory_trend_strength=memory_trend_strength,
                                memory_trend_direction=memory_trend_direction,
                                price_memory_analyzer=price_memory_analyzer,
                                rates_df_fresh=rates_df_fresh,
                                current_price=current_price,
                                asset=asset,
                                logger_ref=logger,
                            )

                            # Extraire les résultats
                            pma_bonus_b3 = pma_result_b3["pma_bonus"]
                            pma_malus_b3 = pma_result_b3["pma_malus"]
                            pma_adjustments_b3 = pma_result_b3["pma_adjustments"]
                            pma_veto_dur_b3 = pma_result_b3["pma_veto_dur"]
                            inst_veto_reversal = pma_result_b3["inst_veto_reversal"]
                            score_brut_b3 = pma_result_b3["score_brut"]
                            score_ajuste_b3 = pma_result_b3["score_ajuste"]

                            # Stocker dans orderflow_result_mini pour traçabilité
                            orderflow_result_mini['pma_bonus'] = pma_bonus_b3
                            orderflow_result_mini['pma_malus'] = pma_malus_b3
                            orderflow_result_mini['pma_adjustment'] = pma_result_b3["pma_adjustment"]
                            orderflow_result_mini['pma_adjustments'] = pma_adjustments_b3
                            orderflow_result_mini['score_before_pma'] = score_brut_b3
                            orderflow_result_mini['score'] = score_ajuste_b3

                            # Appliquer VETO DUR
                            if pma_veto_dur_b3 and signal_action_b3 in ["BUY", "SELL"]:
                                old_action = decision_mini["action"]
                                decision_mini["action"] = "HOLD"
                                decision_mini["confidence"] = 0.0
                                decision_mini["rationale"] = (
                                    f"PMA_VETO_DUR: Score {score_ajuste_b3:.1f} < 60 | "
                                    f"Brut={score_brut_b3:.1f} + Bonus={pma_bonus_b3:.0f} - Malus={pma_malus_b3:.0f} | "
                                    f"Original: {old_action}"
                                )

                            # Appliquer VETO REVERSAL
                            if inst_veto_reversal and signal_action_b3 in ["BUY", "SELL"]:
                                old_action = decision_mini["action"]
                                decision_mini["action"] = "HOLD"
                                decision_mini["confidence"] = 0.0
                                decision_mini["rationale"] = (
                                    f"🚫 VETO_REVERSAL: Renversement institutionnel détecté | "
                                    f"Original: {old_action}"
                                )

                        except Exception as e_pma_b3:
                            logger.error(f"[PMA_ADJUSTMENT_B3][{asset}] Erreur: {e_pma_b3}", exc_info=True)

                        # Construction fusion_out (BRANCHE 3: PASS_NORMAL)
                        if decision_mini["action"] in ["BUY", "SELL"]:
                            anchor_price = decision_mini.get("anchor_price") or (latest.get("current_price") if latest else None) or (latest.get("close") if latest else None)

                            fusion_out = {
                                "ok": True,
                                "action": decision_mini["action"],
                                "fused_confidence": decision_mini["confidence"],
                                "signal_type": "MINIMALIST_ORDERFLOW",
                                "rationale": decision_mini["rationale"],
                                "orderflow_score": orderflow_result_mini["score"],
                                "timing_quality": timing_verdict.get("quality_metrics", {}),
                                "price": anchor_price,
                                "context": ctx,
                                # 🧠 08 FEV 2026: PMA réel (plus de valeurs par défaut à zéro)
                                "price_memory": {
                                    "score_brut": orderflow_result_mini.get("score_before_pma", orderflow_result_mini["score"]),
                                    "score_ajuste": orderflow_result_mini["score"],
                                    "pma_bonus": pma_bonus_b3,
                                    "pma_malus": pma_malus_b3,
                                    "pma_adjustment": pma_bonus_b3 - pma_malus_b3,
                                    "adjustments": pma_adjustments_b3,
                                    "veto_dur": pma_veto_dur_b3,
                                    "mtf": {
                                        "direction": mtf_direction,
                                        "alignment": mtf_verdict.alignment if mtf_verdict else "N/A",
                                        "m30": mtf_verdict.m30_direction if mtf_verdict else "N/A",
                                        "m15": mtf_verdict.m15_direction if mtf_verdict else "N/A",
                                        "m5": mtf_verdict.m5_direction if mtf_verdict else "N/A",
                                        "m1": mtf_verdict.m1_direction if mtf_verdict else "N/A"
                                    },
                                    "micro_resistance": micro_resistance_info
                                },
                                # 🏛️ 08 FEV 2026: Institutional data (était ABSENT de Branche 3)
                                "institutional": {
                                    "score": inst_score,
                                    "conviction": inst_result.get('conviction_level', 'N/A') if inst_result else 'N/A',
                                    "trend": inst_result.get('new_trend', 'N/A') if inst_result else 'N/A',
                                    "reversal_detected": inst_result.get('reversal_detected', False) if inst_result else False,
                                    "veto_fatigue": inst_veto_fatigue,
                                    "veto_reversal": inst_veto_reversal
                                }
                            }

                            logger.info(
                                f"🎯 [MINIMALIST][{asset}] ✅ {fusion_out['action']} | "
                                f"confidence={fusion_out['fused_confidence']:.2f} | "
                                f"Score: {orderflow_result_mini.get('score_before_pma', orderflow_result_mini['score']):.1f} → {orderflow_result_mini['score']:.1f}"
                            )
                        else:
                            fusion_out = {
                                "ok": False,
                                "action": "HOLD",
                                "fused_confidence": 0.0,
                                "signal_type": "MINIMALIST_HOLD",
                                "rationale": decision_mini["rationale"],
                                "orderflow_score": orderflow_result_mini["score"]
                            }

                            logger.info(
                                f"[MINIMALIST][{asset}] HOLD | rationale={decision_mini['rationale']}"
                            )

                # ═══════════════════════════════════════════════════════════════
                # 🔄 UPDATE GLOBAL STATE POUR DASHBOARD (31 DEC 2025)
                # ═══════════════════════════════════════════════════════════════
                try:
                    # Extraire régime
                    latest_candle = market_results.get("latest", {}) if market_results else {}
                    current_regime = latest_candle.get("regime", "UNKNOWN")
                    regime_strength = latest_candle.get("regime_strength", 0.0)

                    # Extraire OrderFlow
                    of_score = orderflow_result_mini.get("score", 0.0)
                    of_bias = orderflow_result_mini.get("bias", "NEUTRAL")
                    of_summary = orderflow_result_mini.get("summary", {})
                    of_quality = of_summary.get("signal_quality", "NO_TRADE")

                    # Extraire Timing
                    timing_status = timing_verdict.get("verdict", "UNKNOWN") if timing_verdict else "UNKNOWN"
                    qm = timing_verdict.get("quality_metrics", {}) if timing_verdict else {}
                    tick_rate = qm.get("tick_rate", 0.0)
                    coverage_s = qm.get("coverage_s", 0.0)

                    # Extraire Décision
                    action = decision_mini.get("action", "HOLD")
                    confidence = decision_mini.get("confidence", 0.0)

                    # UPDATE THREAD-SAFE
                    global_state.update_asset_state(asset, {
                        "regime": str(current_regime).upper() if current_regime else "UNKNOWN",
                        "regime_force": regime_strength,
                        "of_score": of_score,
                        "of_bias": of_bias,
                        "of_quality": of_quality,
                        "timing_status": timing_status,
                        "tick_rate": tick_rate,
                        "coverage_s": coverage_s,
                        "action": action,
                        "confidence": confidence
                    })

                    # LOG COMPACT (1 ligne pour lisibilité console)
                    regime_short = str(current_regime)[:4].upper() if current_regime else "UNKN"
                    bias_short = of_bias[:3] if of_bias else "NEU"
                    timing_short = timing_status[:4] if timing_status else "UNKN"

                    # ✅ (03 JAN 2026): Adapter format score selon mode composite
                    is_composite_log = orderflow_result_mini.get('composite_enabled', False)
                    score_label = "CS" if is_composite_log else "OF"  # CS=Composite Score, OF=OrderFlow
                    score_format_log = f"{of_score:.1f}" if is_composite_log else f"{of_score:.0f}"

                    logger.info(
                        f"[{asset}] "
                        f"R:{regime_short}({regime_strength:.1f}) | "
                        f"{score_label}:{score_format_log}/{bias_short} | "
                        f"T:{timing_short} | "
                        f"→{action}"
                    )

                except Exception as e_update:
                    logger.error(f"[{asset}] Erreur update global_state: {e_update}")

                # ═══════════════════════════════════════════════════════════════
                # 📊 AFFICHAGE TABLEAU CONDENSÉ ORDERFLOW + TIMING (01 JAN 2026)
                # ═══════════════════════════════════════════════════════════════
                try:
                    # Extraire détails OrderFlow pour tableau
                    of_details = orderflow_result_mini.get("summary", {})
                    delta_details = of_details.get("delta_momentum_details", {})
                    volume_details = of_details.get("volume_confirmation_details", {})
                    imbalance_details = of_details.get("imbalance_strength_details", {})

                    delta_total = delta_details.get("delta_total", 0)
                    coherence = delta_details.get("coherence", 0)
                    volume_ratio = volume_details.get("volume_ratio", 0)
                    imbalance_buy = imbalance_details.get("imbalance_buy", 0)
                    imbalance_sell = imbalance_details.get("imbalance_sell", 0)

                    # Extraire métriques timing pour tableau
                    qm = timing_verdict.get("quality_metrics", {})
                    tick_rate = qm.get("tick_rate", 0)
                    coverage_s = qm.get("coverage_s", 0)
                    tick_count_timing = qm.get("tick_count", 0)
                    session = qm.get("session", "UNKNOWN")
                    hour_gmt = pd.Timestamp.now(tz='UTC').hour

                    # 🎯 (05 JAN 2026): Tableau cycle SUPPRIMÉ (pollue console)
                    # Le dashboard affiche tout proprement toutes les 5s
                    # Logs détaillés vont dans le fichier uniquement
                    pass

                except Exception as e_display:
                    logger.warning(f"[{asset}] Erreur affichage tableau: {e_display}")

                # ═══════════════════════════════════════════════════════════════
                # 📊 SOUMETTRE RAPPORT À LA QUEUE (31 DEC 2025 - Solution B)
                # ═══════════════════════════════════════════════════════════════
                try:
                    # Extraire les données importantes
                    latest_candle = market_results.get("latest", {}) if market_results else {}
                    current_regime = latest_candle.get("regime", "UNKNOWN")
                    regime_strength = latest_candle.get("regime_strength", 0.0)

                    of_score = orderflow_result_mini.get("score", 0.0)
                    of_bias = orderflow_result_mini.get("bias", "NEUTRAL")

                    # Extraire delta_total depuis OrderFlow summary (08 JAN 2026: FIX chemin d'accès)
                    of_summary = orderflow_result_mini.get("summary", {})
                    delta_details = of_summary.get("delta_momentum_details", {})
                    delta_total = delta_details.get("delta_total", 0)

                    # Formater trend avec Price Memory + OrderFlow (08 JAN 2026: Fusion Memory + OF)
                    # Combiner OrderFlow (court terme 8s) + PriceMemory (moyen terme 50 bougies)
                    if memory_trend_direction == "BULLISH" and of_bias in ["BULLISH", "BUY"]:
                        # Alignement parfait: tendance haussière + signal BUY
                        trend_str = f"🟢🟢 BULL NET{memory_net_pips:+.0f}"

                    elif memory_trend_direction == "BEARISH" and of_bias in ["BEARISH", "SELL"]:
                        # Alignement parfait: tendance baissière + signal SELL
                        trend_str = f"🔴🔴 BEAR NET{memory_net_pips:+.0f}"

                    elif memory_trend_direction == "BULLISH" and of_bias in ["BEARISH", "SELL"]:
                        # Contre-tendance: signal SELL mais mémoire BULLISH
                        trend_str = f"⚠️ BEAR NET{memory_net_pips:+.0f}"

                    elif memory_trend_direction == "BEARISH" and of_bias in ["BULLISH", "BUY"]:
                        # Contre-tendance: signal BUY mais mémoire BEARISH
                        trend_str = f"⚠️ BULL NET{memory_net_pips:+.0f}"

                    elif memory_trend_direction == "RANGE":
                        # Marché flat/choppy
                        if abs(memory_net_pips) < 5:
                            trend_str = f"⚪ FLAT NET{memory_net_pips:+.0f}"
                        else:
                            # Range mais avec déplacement net
                            if of_bias in ["BULLISH", "BUY"]:
                                trend_str = f"⚪ BULL NET{memory_net_pips:+.0f}"
                            elif of_bias in ["BEARISH", "SELL"]:
                                trend_str = f"⚪ BEAR NET{memory_net_pips:+.0f}"
                            else:
                                trend_str = f"⚪ NEU NET{memory_net_pips:+.0f}"
                    else:
                        # Fallback (ne devrait jamais arriver)
                        if of_bias in ["BULLISH", "BUY"]:
                            trend_str = f"🟢 BULL NET{memory_net_pips:+.0f}"
                        elif of_bias in ["BEARISH", "SELL"]:
                            trend_str = f"🔴 BEAR NET{memory_net_pips:+.0f}"
                        else:
                            trend_str = f"⚪ NEU NET{memory_net_pips:+.0f}"

                    timing_status = timing_verdict.get("verdict", "UNKNOWN")
                    timing_reason = timing_verdict.get("veto_reason", "")
                    qm = timing_verdict.get("quality_metrics", {})
                    tick_count = qm.get("tick_count", 0)

                    action = decision_mini.get("action", "HOLD")
                    confidence = decision_mini.get("confidence", 0.0)
                    rationale = decision_mini.get("rationale", "N/A")

                    # Construire le rapport
                    report_data = {
                        "asset": asset,
                        "cycle": cycle_count,
                        "timestamp": time.time(),
                        "regime": str(current_regime).upper(),
                        "regime_strength": regime_strength,
                        "trend": trend_str,  # 08 JAN 2026: Ajout trend
                        "delta": delta_total,  # 08 JAN 2026: Ajout delta
                        "of_score": of_score,
                        "of_bias": of_bias,
                        "timing": timing_status,
                        "timing_reason": timing_reason,
                        "action": action,
                        "confidence": confidence,
                        "tick_count": tick_count,
                        "rationale": rationale
                    }

                    # Soumettre à la queue (non-bloquant)
                    try:
                        display_queue.put(report_data, block=False)
                    except queue.Full:
                        logger.warning(f"[{asset}] Display queue pleine, rapport ignoré")

                except Exception as e_report:
                    logger.warning(f"[{asset}] Erreur génération rapport: {e_report}", exc_info=True)

                # Si signal valide → Exécution
                if fusion_out.get("ok") and trade_decision_skeleton is not None:
                    # ✅ BURST GUARD: Vérifier si un panier burst est déjà ouvert
                    open_burst_ids = set()
                    try:
                        import re
                        current_positions = mt5_connector.get_positions() or []
                        for p in current_positions:
                            c = p.get("comment") if isinstance(p, dict) else getattr(p, "comment", "")
                            m = re.search(r"bs_([a-f0-9]{8})", str(c or ""))
                            if m:
                                open_burst_ids.add(m.group(1))

                        if open_burst_ids:
                            logger.warning(
                                f"⛔ [{asset}][BURST_GUARD] Panier(s) déjà ouvert(s): {open_burst_ids} | "
                                f"Signal {fusion_out.get('action')} IGNORÉ (single_burst_global)"
                            )
                    except Exception as e_guard:
                        logger.error(f"❌ [{asset}][BURST_GUARD] Erreur vérification: {e_guard}")

                    # Exécuter SEULEMENT si aucun panier ouvert
                    if not open_burst_ids:
                        side = fusion_out["action"]  # BUY ou SELL
                        conf = fusion_out.get("fused_confidence", 0.0)

                        logger.info(f"🎯 [{asset}] Signal {asset} {side} (conf={conf:.2f})")

                        # ⚡ OPTION 1: INJECTION RAPIDE — Utiliser squelette pré-calculé
                        try:
                            # Copier squelette statique
                            skeleton = trade_decision_skeleton["static"]

                            # ⚡ INJECTION valeurs dynamiques UNIQUEMENT (ultra-rapide)
                            td = dict(skeleton)  # Shallow copy rapide
                            td["action"] = side  # Dynamique
                            td["side"] = side  # Dynamique
                            td["confidence"] = conf  # Dynamique
                            td["context"] = ctx  # Dynamique (phase, volatility)
                            td["fusion_data"] = fusion_out  # Dynamique (scores OF/FP/triggers)
                            td["order"] = {
                                "action": side,
                                "side": side,
                                "type": "MARKET",
                                "symbol": asset,
                            }
                            td["trade"] = {"action": side, "side": side}

                            # Package décision - ✅ UTILISER ctx (market context local)
                            with context_lock:
                                ctx_copy = dict(ctx)  # Copie thread-safe du market context

                            decision_pkg = {
                                "final_decision": td,
                                "market_context": ctx_copy,  # ✅ FIX (31 DEC): Utiliser ctx au lieu de global_context
                                "active_config": trade_decision_skeleton["merged_config"],
                            }
                            decision_pkg.setdefault("audit_context", {}).update({
                                "intent_symbol": asset,
                                "intent_side": side,
                                "intent_burst": trade_decision_skeleton["resolved_burst"],
                            })

                            logger.info(f"⚡ [PRE-CALC] Exécution RAPIDE: {side} {asset} burst={trade_decision_skeleton['resolved_burst']}")

                            # Exécution
                            res = run_trade_execution_pipeline(
                                trade_executor, decision_pkg, is_dry_run=is_dry_run
                            )
                            if res:
                                logger.info(f"✅ [{asset}] Trade exécuté: {res.get('status')}")
                            else:
                                logger.warning(f"⚠️ [{asset}] Trade non exécuté (res=None)")

                        except Exception as e:
                            logger.error(f"[{asset}] Erreur exécution trade: {e}", exc_info=True)

            # Note: Surveillance baskets déléguée au basket_monitor_thread dédié

        except Exception as e:
            logger.error(f"[{asset}] Erreur cycle #{cycle_count}: {e}", exc_info=True)
            global_state.record_error(asset, str(e))

        # Sleep dynamique
        elapsed = time.time() - cycle_start
        sleep_time = max(0, cycle_interval - elapsed)
        if sleep_time > 0:
            stop_event.wait(timeout=sleep_time)

    logger.info(f"🛑 [{asset}] Worker arrêté")


# ═══════════════════════════════════════════════════════════════════════════
# 📊 DASHBOARD THREAD: Affichage agrégé (31 DEC 2025)
# ═══════════════════════════════════════════════════════════════════════════

def dashboard_worker(
    display_queue: queue.Queue,
    stop_event: threading.Event,
    logger,
    verbose: bool = False
):
    """
    Thread dashboard: vide la queue et affiche tableau consolidé toutes les 5 secondes.

    🎯 (05 JAN 2026): Draine TOUTE la queue à chaque cycle pour éviter accumulation.
    Garde seulement le dernier rapport par asset (USDJPY, EURUSD, GBPUSD).

    Affiche:
    ═══════════════════════════════════════════════════════════════════════════════
    📊 SCALPING MULTI-ACTIFS - 31 Dec 2025 14:30:05
    ───────────────────────────────────────────────────────────────────────────────
    USDJPY  │ TREND(0.8)   │ 🟢 85/BUY  │ ✅ GO   │ 📈 BUY   │ 75%  │ 137 ticks
    EURUSD  │ RANGE(0.6)   │ 🟡 45/SEL  │ ❌ VETO │ ⏸️ HOLD  │ 30%  │ 148 ticks
    GBPUSD  │ CONS(0.7)    │ 🔴 15/NEU  │ ❌ VETO │ ⏸️ HOLD  │ 10%  │ 162 ticks
    ───────────────────────────────────────────────────────────────────────────────
    📈 Signaux: 1 BUY | ⚠️ Veto: EURUSD, GBPUSD (Heure non autorisée)
    ═══════════════════════════════════════════════════════════════════════════════
    """
    import pandas as pd

    display_interval = 5.0  # 5 secondes entre chaque affichage

    logger.info("📊 [DASHBOARD] Thread démarré (affichage 5s)")

    while not stop_event.is_set():
        try:
            # 🎯 (05 JAN 2026): VIDER la queue complètement pour éviter accumulation
            # Problème: assets produisent 6 rapports/5s, dashboard ne consomme que 3
            # Solution: drainer toute la queue et garder seulement le dernier par asset
            reports = {}

            # Vider la queue complètement (non-bloquant)
            while True:
                try:
                    report = display_queue.get_nowait()
                    reports[report["asset"]] = report  # Écrase ancien rapport du même asset
                    display_queue.task_done()
                except queue.Empty:
                    break  # Queue vidée

            # Afficher seulement si on a au moins 1 rapport
            if reports:
                # Header
                now = pd.Timestamp.now().strftime("%d %b %Y %H:%M:%S")
                print("\n" + "═" * 110)
                print(f"📊 SCALPING MULTI-ACTIFS - {now}")
                print("─" * 110)

                # Table header (08 JAN 2026: Ajout TREND + DELTA)
                print(f"{'ASSET':<7} │ {'RÉGIME':<12} │ {'TREND':<8} │ {'SCORING':<11} │ {'DELTA':<7} │ {'TIMING':<7} │ {'ACTION':<8} │ {'CONF':<4} │ {'TICKS':<10}")
                print("─" * 110)

                # Lignes par asset (ordre fixe)
                for asset_name in ["USDJPY", "USDCHF"]:
                    if asset_name in reports:
                        r = reports[asset_name]

                        # Format régime
                        regime_short = r["regime"][:4] if r["regime"] else "UNKN"
                        regime_str = f"{regime_short}({r['regime_strength']:.1f})"

                        # Icône + score (03 JAN 2026: .1f pour afficher composite avec décimale)
                        of_score = r["of_score"]
                        if of_score >= 70:
                            of_icon = "🟢"
                        elif of_score >= 40:
                            of_icon = "🟡"
                        else:
                            of_icon = "🔴"
                        bias_short = r["of_bias"][:3]
                        of_str = f"{of_icon} {of_score:.1f}/{bias_short}"

                        # Icône timing
                        timing = r["timing"]
                        if timing == "PASS":
                            timing_str = "✅ GO"
                        elif timing == "VETO":
                            timing_str = "❌ VETO"
                        else:
                            timing_str = "⏸️ HOLD"

                        # Icône action
                        action = r["action"]
                        if action == "BUY":
                            action_str = "📈 BUY"
                        elif action == "SELL":
                            action_str = "📉 SELL"
                        else:
                            action_str = "⏸️ HOLD"

                        # Confidence
                        conf_str = f"{r['confidence']*100:.0f}%"

                        # Trend (08 JAN 2026)
                        trend_str = r.get("trend", "⚪ NEU")

                        # Delta (08 JAN 2026)
                        delta_val = r.get("delta", 0)
                        if delta_val > 0:
                            delta_str = f"🟢{delta_val:+.0f}"
                        elif delta_val < 0:
                            delta_str = f"🔴{delta_val:+.0f}"
                        else:
                            delta_str = "⚪0"

                        # Ticks
                        ticks_str = f"{r['tick_count']} ticks"

                        # Affichage ligne principale (08 JAN 2026: Ajout TREND + DELTA)
                        print(f"{asset_name:<7} │ {regime_str:<12} │ {trend_str:<8} │ {of_str:<10} │ {delta_str:<7} │ {timing_str:<7} │ {action_str:<8} │ {conf_str:<4} │ {ticks_str:<10}")

                        # 🔍 MODE VERBOSE: Afficher détails techniques (05 JAN 2026)
                        if verbose:
                            # Rationale/Veto reason
                            rationale = r.get("rationale", "N/A")
                            timing_reason = r.get("timing_reason", "")

                            details_line = f"    └─ "
                            if timing == "VETO" and timing_reason:
                                # Extraire les infos du veto
                                details_line += f"🚫 {timing_reason[:70]}"
                            else:
                                details_line += f"💡 {rationale}"

                            print(details_line)

                # Footer avec résumé
                print("─" * 110)

                # Compter signaux
                buy_count = sum(1 for r in reports.values() if r["action"] == "BUY")
                sell_count = sum(1 for r in reports.values() if r["action"] == "SELL")

                # Lister vetos
                veto_assets = [asset for asset, r in reports.items() if r["timing"] == "VETO"]
                veto_reason = ""
                if veto_assets:
                    # Prendre la raison du premier veto
                    first_veto = reports[veto_assets[0]]
                    reason = first_veto.get("timing_reason", "")
                    if reason:
                        # Extraire juste "Heure Xh GMT NON autorisée"
                        if "Heure" in reason and "GMT" in reason:
                            veto_reason = f" ({reason.split('(')[0].strip()})"
                        else:
                            veto_reason = f" ({reason[:40]}...)" if len(reason) > 40 else f" ({reason})"

                # Résumé
                summary_parts = []
                if buy_count > 0:
                    summary_parts.append(f"📈 {buy_count} BUY")
                if sell_count > 0:
                    summary_parts.append(f"📉 {sell_count} SELL")
                if veto_assets:
                    summary_parts.append(f"⚠️ Veto: {', '.join(veto_assets)}{veto_reason}")

                if summary_parts:
                    print(" | ".join(summary_parts))

                print("═" * 110)

            # Attendre jusqu'au prochain affichage
            time.sleep(display_interval)

        except Exception as e:
            logger.error(f"[DASHBOARD] Erreur affichage: {e}", exc_info=True)
            time.sleep(display_interval)

    logger.info("🛑 [DASHBOARD] Thread arrêté")


def basket_monitor_thread(
    trade_executor,
    config_manager,
    strategy_manager,
    stop_event: threading.Event,
    logger
):
    """
    Thread dédié à la SURVEILLANCE CONTINUE des baskets burst.

    Responsabilités:
    - Surveillance 24/7 avec polling 100ms
    - Fermeture automatique à +15 pips (configurable)
    - Pas de deadline → tourne en continu
    """
    logger.info("🚀 [BASKET_MONITOR_THREAD] Démarré (surveillance continue)")

    # Fusionner la config UNE SEULE FOIS au démarrage
    try:
        base_config = config_manager.get_current_dynamic_config()
        scalping_config = strategy_manager.get_strategy_config("scalping") or {}
        merged_config = dict(base_config)
        if "entry_rules" in scalping_config:
            merged_config.setdefault("entry_rules", {}).update(
                scalping_config["entry_rules"]
            )
        logger.info("✅ [BASKET_MONITOR] Config fusionnée (unique au démarrage)")
    except Exception as merge_err:
        logger.warning(f"⚠️ [BASKET_MONITOR] Fusion config échouée: {merge_err}")
        merged_config = config_manager.get_current_dynamic_config()

    # Vérifier l'état des closure_rules une seule fois au démarrage
    closure_enabled = merged_config.get("entry_rules", {}).get("scalping", {}).get("burst_scalping", {}).get("closure_rules", {}).get("enabled", False)
    if closure_enabled:
        logger.info("✅ [BASKET_MONITOR] closure_rules.enabled=True → Surveillance active")
    else:
        logger.info("⛔ [BASKET_MONITOR] closure_rules.enabled=False → Surveillance désactivée (retour immédiat)")

    while not stop_event.is_set():
        try:
            # Appeler monitor_burst_baskets en mode continu (SANS logging répétitif)
            trade_executor.monitor_burst_baskets(config=merged_config)

        except Exception as e:
            logger.error(f"❌ [BASKET_MONITOR] Erreur: {e}", exc_info=True)
            time.sleep(1)  # Éviter spam en cas d'erreur

    logger.info("🛑 [BASKET_MONITOR_THREAD] Arrêté proprement")


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
                logger.info("🔄 [HOT-RELOAD] Signal de rechargement reçu !")
                logger.info("=" * 80)
                config_manager.reload_all_configs()
                logger.info("=" * 80)
                logger.info("✅ [HOT-RELOAD] Configuration rechargée avec succès")
                logger.info("💡 [HOT-RELOAD] Les prochains cycles utiliseront la nouvelle config")
                logger.info("=" * 80)
            except Exception as e:
                logger.error(f"❌ [HOT-RELOAD] Erreur lors du rechargement: {e}", exc_info=True)

        # Utiliser SIGUSR1 (Linux/Mac) ou SIGBREAK (Windows)
        if platform.system() == "Windows":
            # Windows: SIGBREAK (Ctrl+Break) est le seul signal custom disponible
            logger.info("🔧 [HOT-RELOAD] Système Windows détecté - Handler SIGBREAK configuré")
            logger.info("💡 [HOT-RELOAD] Pour recharger: envoyez SIGBREAK au processus")
            signal.signal(signal.SIGBREAK, reload_config_handler)
        else:
            # Linux/Mac: SIGUSR1
            logger.info("🔧 [HOT-RELOAD] Système Unix détecté - Handler SIGUSR1 configuré")
            logger.info(f"💡 [HOT-RELOAD] Pour recharger: kill -SIGUSR1 {os.getpid()}")
            signal.signal(signal.SIGUSR1, reload_config_handler)

    try:
        setup_hot_reload_handler()
    except Exception as e:
        logger.warning(f"⚠️ [HOT-RELOAD] Impossible de configurer le hot-reload: {e}")

    # 5. Instanciation des Modules Fondamentaux
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
                # ✅ NE PAS DÉCONNECTER - Garder la connexion persistante pour les threads !
                logger.info(f"✅ MT5 connecté et prêt (is_connected={mt5_connector.is_connected})")

                # ✅ LANCEMENT DU DASHBOARD WEB EN TEMPS RÉEL
                try:
                    dashboard_thread = launch_integrated_dashboard(
                        config_manager=config_manager,
                        mt5_connector=mt5_connector,
                        bot_mode=bot_mode,
                        host='0.0.0.0',
                        port=5000
                    )
                    logger.info("📊 Dashboard lancé sur http://localhost:5000")
                except Exception as dash_err:
                    logger.warning(f"⚠️ Dashboard non lancé (non bloquant): {dash_err}")

                # ✅ LANCEMENT DU TELEGRAM BOT CONTROLLER (04 FEV 2026)
                # Permet de contrôler le bot à distance via Telegram
                # Note: les callbacks seront configurés après la création des stop_events
                telegram_controller = None
                try:
                    from core.telegram_bot import create_telegram_controller
                    telegram_controller = create_telegram_controller(
                        config_manager=config_manager,
                        mt5_connector=mt5_connector,
                        logger=logger
                    )
                    if telegram_controller:
                        telegram_controller.start()
                        logger.info("📱 Telegram Bot Controller démarré - Commandes disponibles: /status, /positions, /balance, /stop, /help")
                    else:
                        logger.warning("⚠️ Telegram Controller non configuré (token ou chat_id manquant)")
                except Exception as tg_err:
                    logger.warning(f"⚠️ Telegram Controller non démarré (non bloquant): {tg_err}")
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
    # ✅ CONFIGURABLE (02 JAN 2026): Lire cycle depuis config
    scalping_cycle = config_manager.get("bot_behavior.cycle_interval_seconds", 2.5)

    logger.info("=" * 80)
    logger.info(f"🚀 DÉMARRAGE MULTI-THREADING SCALPING (02 JAN 2026 - Cycle {scalping_cycle}s)")
    logger.info("=" * 80)
    logger.info(f"  • SCALPING USDJPY Thread : Cycle {scalping_cycle}s (offset 0.0s)")
    logger.info(f"  • SCALPING USDCHF Thread : Cycle {scalping_cycle}s (offset 1.5s)")
    logger.info("  • DASHBOARD Thread       : Affichage agrégé 30s")
    logger.info("  • BASKET MONITOR Thread  : Surveillance continue (polling 100ms)")
    logger.info("=" * 80)

    # Global context partagé avec lock
    # ✅ FIX (17 DEC 2025): Initialiser avec active_broker_account pour sizing correct
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

    # ✅ Créer GlobalScalpingState (31 DEC 2025)
    assets = ["USDJPY", "USDCHF"]
    global_scalping_state = GlobalScalpingState(assets)

    # ✅ Créer Display Queue (31 DEC 2025 - Solution B)
    display_queue = queue.Queue(maxsize=100)

    # Events pour arrêt propre
    scalping_stop_event = threading.Event()
    dashboard_stop_event = threading.Event()
    basket_monitor_stop_event = threading.Event()

    # ⏸️ Event pour pause/resume trading via Telegram (04 FEV 2026)
    trading_pause_event = threading.Event()  # Non-set = trading actif, Set = en pause

    # ✅ CONNEXION TELEGRAM CONTROLLER AUX STOP/PAUSE EVENTS (04 FEV 2026)
    if telegram_controller:
        def telegram_stop_callback():
            """Callback appelé par /stop Telegram pour arrêter le bot."""
            logger.info("🛑 Arrêt demandé via Telegram!")
            scalping_stop_event.set()
            dashboard_stop_event.set()
            basket_monitor_stop_event.set()
            config_manager.send_alert("🛑 Bot arrêté via commande Telegram /stop", "telegram_critical")

        def telegram_pause_callback():
            """Callback appelé par /pause Telegram pour mettre en pause le trading."""
            logger.info("⏸️ Pause trading demandée via Telegram!")
            trading_pause_event.set()
            config_manager.send_alert("⏸️ Trading en PAUSE via Telegram", "telegram_critical")

        def telegram_resume_callback():
            """Callback appelé par /resume Telegram pour reprendre le trading."""
            logger.info("▶️ Reprise trading demandée via Telegram!")
            trading_pause_event.clear()
            config_manager.send_alert("▶️ Trading REPRIS via Telegram", "telegram_critical")

        def telegram_is_paused():
            """Retourne True si le trading est en pause."""
            return trading_pause_event.is_set()

        telegram_controller.set_callbacks(
            stop_callback=telegram_stop_callback,
            pause_callback=telegram_pause_callback,
            resume_callback=telegram_resume_callback,
            is_paused_callback=telegram_is_paused
        )
        logger.info("✅ Telegram Controller connecté aux stop/pause events")

    # ✅ Créer les 3 threads scalping (staggered timing)
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
            logger,
            trading_pause_event          # 04 FEV 2026: pause/resume Telegram
        ),
        daemon=True,
        name="ScalpingWorker-USDJPY"
    )

    thread_usdchf = threading.Thread(
        target=scalping_worker,
        args=(
            "USDCHF",                    # asset
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
            logger,
            trading_pause_event          # 04 FEV 2026: pause/resume Telegram
        ),
        daemon=True,
        name="ScalpingWorker-USDCHF"
    )

    # ✅ Créer thread dashboard (31 DEC 2025 - Solution B)
    # 🔍 (05 JAN 2026): Support mode verbose pour déboguer
    verbose_mode = getattr(args, 'verbose', False)
    thread_dashboard = threading.Thread(
        target=dashboard_worker,
        args=(
            display_queue,               # display queue au lieu de global_state
            dashboard_stop_event,
            logger,
            verbose_mode                 # 🔍 Mode DEBUG activable
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

    # ❌ DÉSACTIVÉ (25 DEC 2025): DataEngine - Architecture minimaliste (footprint supprimé)
    # from core.data_engine import DataEngine
    # from phase_observer.market_analyzer import MarketAnalyzer
    #
    # # Créer un MarketAnalyzer dédié pour le DataEngine
    # # Signature: MarketAnalyzer(config_manager, logger)
    # market_analyzer_for_dataengine = MarketAnalyzer(config_manager, logger)
    #
    # data_engine = DataEngine(
    #     symbols=['USDJPY'],  # Symboles prioritaires pour le scalping
    #     mt5_connector=mt5_connector,
    #     market_analyzer=market_analyzer_for_dataengine,  # ✅ MarketAnalyzer dédié
    #     update_interval_seconds=5.0,  # Cycle 5s (plus réactif que cycle scalping 10s)
    #     stop_event=data_engine_stop_event
    # )

    # ✅ Démarrer tous les threads (31 DEC 2025)
    thread_usdjpy.start()
    thread_usdchf.start()
    thread_dashboard.start()
    basket_monitor.start()

    logger.info("✅ Tous les threads démarrés avec succès")
    logger.info("   → Appuyez sur Ctrl+C pour arrêter proprement")
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
        logger.info("🛑 Arrêt des threads en cours...")

        try:
            # ✅ Signal arrêt à tous les threads (31 DEC 2025)
            scalping_stop_event.set()
            dashboard_stop_event.set()
            basket_monitor_stop_event.set()

            logger.info("⏳ Attente arrêt propre des threads...")

            # ✅ Attendre arrêt avec timeout
            thread_usdjpy.join(timeout=5.0)
            thread_usdchf.join(timeout=5.0)
            thread_dashboard.join(timeout=5.0)
            basket_monitor.join(timeout=5.0)

            # ✅ Vérifier threads encore actifs
            for thread in [thread_usdjpy, thread_usdchf, thread_dashboard]:
                if thread.is_alive():
                    logger.warning(f"⚠️ Thread {thread.name} n'a pas terminé dans les 5s")
                else:
                    logger.info(f"✅ Thread {thread.name} arrêté proprement")

            if basket_monitor.is_alive():
                logger.warning("⚠️ Thread basket_monitor n'a pas terminé dans les 5s")
            else:
                logger.info("✅ Thread basket_monitor arrêté proprement")
        except Exception as e:
            logger.error(f"Erreur arrêt threads: {e}")
       

        if "mt5_connector" in locals() and mt5_connector.is_connected():
            mt5_connector.disconnect()

        logger.info("SNIPER_X Bot est arrêté.")
        sys.exit(0)
