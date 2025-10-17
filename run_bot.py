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


load_dotenv()

try:
    from phase_observer.detectors import detect_orderflow_v5
    from phase_observer.detectors import footprint_validator
    from phase_observer.orchestrator import PhaseObserver
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

    # Sections à merger (tu peux en ajouter/retirer selon tes fichiers d’assets)
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
    """Exécute une décision unique (scalping ou liquidity)."""
    decision_package_for_executor = {
        "trade_decision": td,
        "active_config": decision_package.get("config_used", {}) or {},
        "market_context": global_context,
    }

    try:
        if td.get("rule_name") == "burst_scalping" or td.get("burst_enabled", False):
            burst_size = int(td.get("burst_size", 3))
            trade_decision = trade_executor._attach_burst_metadata(td)
            requests = []
            for i in range(burst_size):
                req = trade_executor.prepare_order(
                    {
                        "trade_decision": dict(trade_decision),
                        "market_context": global_context,
                        "active_config": decision_package.get("config_used", {}) or {},
                    }
                )
                req["comment"] = f"{req.get('comment','')}|BURST|{i+1}/{burst_size}"
                requests.append(req)
            results = [trade_executor.execute_order(r) for r in requests]
            return all(
                str(res.get("status", "")).lower() in {"filled", "placed"}
                for res in results
            )
        else:
            order_request = trade_executor.prepare_order(decision_package_for_executor)
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
) -> bool:
    """
    Exécute un cycle complet du pipeline de trading de SNIPER_X.

    Nouveautés :
    - ✅ Utilise MarketAnalyzer (fusion PhaseObserver + PatternEngine)
    - ✅ Cycle basé sur un seul scan lourd par actif
    - ✅ Simplification des signaux consolidés
    """

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

    # ✅ MarketAnalyzer unifié
    market_analyzer = MarketAnalyzer(config_manager=config_manager, logger=logger)

    try:
        if not getattr(mt5_connector, "is_connected", False):
            raise RuntimeError("MT5 a perdu la connexion persistante.")

        base_config = config_manager.get_current_dynamic_config()
        execution_mode = str(base_config.get("mode_execution", "DEMO")).upper()
        
        # --- EXEC MODE OVERRIDE (force depuis config) ---
        is_dry_run = bool(base_config.get("trade_execution", {}).get("dry_run", is_dry_run))
        logger.info(f"[EXEC MODE] is_dry_run={is_dry_run} | execution_mode={execution_mode}")

        active_mt5_account_details = config_manager.get_mt5_account_credentials(
            mode=execution_mode
        )
        # [PATCH-CANDLES] Master switch lu une fois pour le cycle
        try:
            _cs = (
                config_manager.get("phase_detection_defaults.candlestick_analysis", {})
                or {}
            )
            CANDLES_ENABLED = bool(_cs.get("enabled", True))
        except Exception:
            CANDLES_ENABLED = True

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

        print(f"🎯 [PIPELINE] Assets tradables: {tradeable_assets}")
        if not tradeable_assets:
            logger.warning("Aucun actif à trader pour ce cycle. Cycle ignoré.")
            return False

        # === Nouveau bloc collecte + analyse unifiée ===
        all_assets_market_data: Dict[str, pd.DataFrame] = {}
        all_assets_trading_signals: Dict[str, Dict[str, Any]] = {}
        
        # ⚡ Décisions Footprint (Scalping Burst) collectées pendant la boucle actifs
        footprint_scalping_decisions: List[Dict[str, Any]] = []


        dcfg = base_config.get("data_collection", {}) or {}
        timeframe_str = dcfg.get("default_timeframe", "M1")
        bars_to_fetch = int(dcfg.get("default_bars_count", 500))
        min_required_bars = 50

        for asset in tradeable_assets:
            print(f"📊 [PIPELINE] Analyse de {asset}...")
            try:
                rates_df = mt5_connector.get_rates(asset, timeframe_str, bars_to_fetch)
                if (
                    rates_df is None
                    or rates_df.empty
                    or len(rates_df) < min_required_bars
                ):
                    logger.warning(f"Données insuffisantes pour {asset}. Actif ignoré.")
                    continue

                symbol_info_mt5 = mt5_connector.get_symbol_info(asset)
                if symbol_info_mt5:
                    rates_df["point"] = getattr(symbol_info_mt5, "point", 0.0)
                    rates_df["spread"] = getattr(symbol_info_mt5, "spread", 0)
                # ✅ Initialiser l’historique du PhaseObserver si vide
                if (
                    market_analyzer.phase_observer._history_df is None
                    or market_analyzer.phase_observer._history_df.empty
                ):
                    market_analyzer.phase_observer.load_initial_history(rates_df.copy())

                # ✅ Mode "horloge suisse" — 200 au 1er cycle, puis rolling 50, avec recalibration périodique
                dcfg = base_config.get("data_collection", {}) or {}
                rolling_lookback = int(dcfg.get("rolling_lookback_bars", 50))  # ex: 50
                full_refresh_bars = int(dcfg.get("full_refresh_bars", 200))  # ex: 200
                recalib_n = int(
                    dcfg.get("recalibration_every_n_cycles", 10)
                )  # ex: toutes les 10 itérations

                do_full_refresh = (cycle_count == 1) or (
                    recalib_n > 0 and cycle_count % recalib_n == 0
                )
                subset_df = rates_df.tail(
                    full_refresh_bars if do_full_refresh else rolling_lookback
                ).copy()

                market_results = market_analyzer.analyze(subset_df, asset)
                # [PATCH-CANDLES] Purge patterns & traces chandeliers si OFF (anti-effet de bord)
                if not CANDLES_ENABLED:
                    try:
                        market_results.pop("patterns", None)
                        lat = market_results.get("latest")
                        if isinstance(lat, dict):
                            lat.pop("candles", None)
                    except Exception:
                        pass

                # ⚠️ Ne réinitialise pas l’historique à chaque cycle → limite les doublons de logs
                if cycle_count == 1 or do_full_refresh:
                    market_analyzer.phase_observer.load_initial_history(
                        subset_df.copy()
                    )
                elif hasattr(market_analyzer.phase_observer, "update_with_new_data"):
                    market_analyzer.phase_observer.update_with_new_data(
                        subset_df.copy()
                    )

                # 🔍 Debug : log des clés retournées par MarketAnalyzer
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
                # === LOG DIAGNOSTIQUE HISTORIQUE 200 bougies ===
                if CANDLES_ENABLED:
                    try:
                        last_200 = annotated_rates_df.tail(200)
                        avg_vol = (
                            last_200["tick_volume"].mean()
                            if "tick_volume" in last_200
                            else None
                        )
                        avg_range = (
                            (last_200["high"] - last_200["low"]).mean()
                            if {"high", "low"} <= set(last_200.columns)
                            else None
                        )
                        bull_candles = (
                            int((last_200["close"] > last_200["open"]).sum())
                            if {"close", "open"} <= set(last_200.columns)
                            else 0
                        )
                        bear_candles = (
                            int((last_200["close"] < last_200["open"]).sum())
                            if {"close", "open"} <= set(last_200.columns)
                            else 0
                        )

                        logger.info(
                            f"[{asset}] Historique(200 bougies) → "
                            f"Bull={bull_candles}, Bear={bear_candles}, "
                            f"VolMoy={avg_vol:.2f} | RangeMoy={avg_range:.5f}"
                        )
                    except Exception as e:
                        logger.warning(
                            f"[{asset}] Impossible de résumer l’historique 200 bougies: {e}"
                        )
                else:
                    logger.debug(
                        f"[{asset}] Skip log 200 bougies (candlestick_analysis disabled)."
                    )

                # === PATCH FOOTPRINT ANALYSE (ciblage ticks dernière bougie) ===
                try:
                    # ✅ Récupération de la dernière bougie fermée
                    last_candle = annotated_rates_df.iloc[-1]

                    # ✅ Gestion robuste du timestamp
                    if "time" in annotated_rates_df.columns:
                        start_ts = pd.to_datetime(
                            last_candle["time"], utc=True, errors="coerce"
                        )
                    else:
                        start_ts = pd.to_datetime(
                            last_candle.name, utc=True, errors="coerce"
                        )

                    if pd.isna(start_ts):
                        start_ts = pd.Timestamp.utcnow()

                    # ✅ Calcul end_ts cohérent (M1 = +1 minute)
                    end_ts = start_ts + pd.Timedelta(minutes=1)

                    # ✅ Récupération des ticks pour cette bougie
                    ticks_df = mt5_connector.get_ticks_for_candle(
                        asset,
                        start_ts.to_pydatetime(),
                        end_ts.to_pydatetime(),
                    )

                    # Normalisation du temps
                    if "time" in annotated_rates_df.columns:
                        annotated_rates_df["time"] = pd.to_datetime(
                            annotated_rates_df["time"], utc=True, errors="coerce"
                        )

                    if ticks_df is not None and not ticks_df.empty:
                        ticks_df["time"] = pd.to_datetime(
                            ticks_df["time"], utc=True, errors="coerce"
                        )

                        fp_res = footprint_validator(
                            annotated_rates_df,
                            ticks_df,
                            candle_index=None,
                            price_step=None,
                            imbalance_threshold=0.7,
                        )

                        logger.info(
                            f"[FOOTPRINT][{asset}] Score={fp_res.get('score', 0)} | "
                            f"Status={fp_res.get('status', 'N/A')} | "
                            f"Summary={fp_res.get('summary', {})}"
                        )

                        latest = dict(latest)
                        latest["footprint_score"] = fp_res.get("score", 0)
                        latest["footprint_status"] = fp_res.get("status", "N/A")
                        latest["footprint_summary"] = fp_res.get("summary", {})

                    else:
                        logger.warning(f"[FOOTPRINT][{asset}] Aucun tick reçu → skip.")

                except Exception as e:
                    logger.error(
                        f"[FOOTPRINT][{asset}] Erreur analyse ticks: {e}", exc_info=True
                    )
                    
                # === FOOTPRINT TRIGGERS → Décision Scalping Burst (LIMIT+FOK) ===
                try:
                    # 1) Ticks récents pour snapshot footprint (5–8s)
                    ticks_recent_df = None
                    try:
                        # Si tu as une API range/now → privilégier 8s récents
                        _now = pd.Timestamp.utcnow()
                        start_recent = _now - pd.Timedelta(seconds=8)
                        if hasattr(mt5_connector, "get_ticks_range"):
                            ticks_recent_df = mt5_connector.get_ticks_range(
                                asset, start_recent.to_pydatetime(), _now.to_pydatetime()
                            )
                        elif hasattr(mt5_connector, "get_ticks_last_seconds"):
                            ticks_recent_df = mt5_connector.get_ticks_last_seconds(asset, seconds=8)
                    except Exception:
                        ticks_recent_df = None

                    # Fallback: réutiliser ticks_df de la bougie (si pas de better API)
                    if (ticks_recent_df is None or ticks_recent_df.empty) and ("ticks_df" in locals()):
                        ticks_recent_df = ticks_df

                    if ticks_recent_df is not None and not ticks_recent_df.empty:
                        ok_fp, dec_fp = market_analyzer.analyze_footprint_triggers(
                            asset=asset,
                            ticks=ticks_recent_df,
                            bars=annotated_rates_df,         # historique M1 (>= 20 barres)
                            strategy_config=base_config,     # fallback si self.footprint_triggers manquant
                        )
                    else:
                        ok_fp, dec_fp = False, {"reason": "no recent ticks"}

                    if ok_fp:
                        # Construire la décision pour ton exécuteur actuel
                        # - rule_name=burst_scalping → tes gardes/trailing existants se branchent
                        entry = dec_fp.get("entry", {}) or {}
                        action = str(dec_fp.get("action", "")).upper()
                        if action not in {"BUY", "SELL"}:
                            logger.debug(f"[FOOTPRINT→DECISION][{asset}] skip: action invalide ({action})")
                            raise RuntimeError("action invalid")

                        burst_count = int(entry.get("burst_count", 5))
                        burst_each = float(entry.get("burst_volume_each", 0.02))
                        total_volume = round(burst_count * burst_each, 5)

                        # 2) Prix d'entrée — fallback Ask/Bid si manquant (critique pour FOK)
                        price_val = float(entry.get("price", 0.0) or 0.0)
                        if price_val <= 0.0:
                            price_val = None
                            # a) via wrapper éventuel
                            try:
                                if hasattr(mt5_connector, "get_symbol_tick"):
                                    tk = mt5_connector.get_symbol_tick(asset)
                                    if tk is not None:
                                        if isinstance(tk, dict):
                                            ask = tk.get("ask")
                                            bid = tk.get("bid")
                                        else:
                                            ask = getattr(tk, "ask", None)
                                            bid = getattr(tk, "bid", None)
                                        if action == "BUY" and ask:
                                            price_val = float(ask)
                                        elif action == "SELL" and bid:
                                            price_val = float(bid)
                            except Exception:
                                price_val = None
                            # b) fallback direct via module MT5 natif
                            if price_val is None:
                                try:
                                    mt5mod = getattr(mt5_connector, "mt5", None)
                                    if mt5mod:
                                        tk = mt5mod.symbol_info_tick(asset)
                                        if tk:
                                            price_val = float(tk.ask if action == "BUY" else tk.bid)
                                except Exception:
                                    price_val = None

                        if not price_val:
                            logger.error(f"[FOOTPRINT→DECISION][{asset}] impossible d'obtenir le prix (Ask/Bid) → skip.")
                            # On n’ajoute PAS de décision invalide (évite '[BURST] Paramètres d'entrée invalides.')
                        else:
                            fp_decision = {
                                "rule_name": "burst_scalping",
                                "action": action,
                                "asset": dec_fp.get("asset", asset),
                                "volume": total_volume,                      # compat affichage pipeline
                                "entry_style": entry.get("style", "LIMIT_FOK"),
                                "price": float(price_val),
                                "burst_count": burst_count,
                                "burst_volume_each": burst_each,
                                "validity_ms": int(entry.get("validity_ms", 800)),
                                # Important pour exécuteur: pas de fallback, pas de TP
                                "no_fallback": True,
                                "no_tp": True,
                                # On garde l’info exit phases pour l’intégration du trailing avancé plus tard
                                "footprint_exit": dec_fp.get("exit", {}),
                                # Télémétrie contextuelle
                                "trigger": dec_fp.get("trigger"),
                                "confidence": float(dec_fp.get("confidence", 0.7)),
                                "footprint_meta": dec_fp.get("meta", {}),
                            }
                            footprint_scalping_decisions.append(fp_decision)

                            logger.info(
                                f"[FOOTPRINT→DECISION][{asset}] "
                                f"{fp_decision['action']} burst x{burst_count}@{fp_decision['price']} "
                                f"(trigger={fp_decision.get('trigger')}, conf={fp_decision.get('confidence'):.2f})"
                            )
                    else:
                        logger.debug(f"[FOOTPRINT→DECISION][{asset}] skip: {dec_fp.get('reason')}")
                except Exception as e:
                    logger.error(f"[FOOTPRINT→DECISION][{asset}] erreur: {e}", exc_info=True)

                # === PATCH ORDERFLOW V5 ANALYSE (avant footprint) ===
                try:
                    # ✅ On récupère les 5 dernières bougies pour l'analyse d'orderflow
                    last_candles_df = annotated_rates_df.tail(5).copy()

                    # Normalisation du temps
                    if "time" in last_candles_df.columns:
                        last_candles_df["time"] = pd.to_datetime(
                            last_candles_df["time"], utc=True, errors="coerce"
                        )
                        last_candles_df.set_index("time", inplace=True)

                    # ✅ Appel du nouvel analyseur OrderFlow V5
                    of_res = detect_orderflow_v5(last_candles_df)

                    # ✅ Log complet et formaté
                    logger.info(
                        f"[ORDERFLOW][{asset}] Score={of_res.get('score', 0)} | "
                        f"Status={of_res.get('status', 'N/A')} | "
                        f"Δ={of_res['summary'].get('delta_total', 0):.2f} | "
                        f"Vol={of_res['summary'].get('volume_total', 0):.2f} | "
                        f"ImbMoy={of_res['summary'].get('mean_imbalance', 0):.2f} | "
                        f"CVD={of_res['summary'].get('cvd_final', 0):.2f} | "
                        f"Patterns={of_res.get('summary', {}).get('pattern_count', 0)}"
                    )

                    # ✅ Log des patterns si existants
                    patterns = of_res.get("patterns", [])
                    if patterns:
                        logger.debug(f"[ORDERFLOW][{asset}] Patterns détectés:")
                        for p in patterns:
                            logger.debug(
                                f"   ↳ {p.get('timestamp', '?')} | {p.get('pattern', '?')} | "
                                f"Δ={p.get('delta', 0)} | Imb={p.get('imbalance', 0):.2f} | "
                                f"Dom={p.get('dominance', '?')}"
                            )
                    else:
                        logger.debug(f"[ORDERFLOW][{asset}] Aucun pattern détecté.")

                    # ✅ Enregistrement dans latest (pour exploitation décisionnelle)
                    latest = dict(latest)
                    latest["orderflow_score"] = of_res.get("score", 0)
                    latest["orderflow_status"] = of_res.get("status", "N/A")
                    latest["orderflow_summary"] = of_res.get("summary", {})
                    latest["orderflow_patterns"] = of_res.get("patterns", [])

                except Exception as e:
                    logger.error(
                        f"[ORDERFLOW][{asset}] Erreur analyse Orderflow: {e}",
                        exc_info=True,
                    )

                # Signaux unifiés
                signals: Dict[str, Any] = (
                    _build_asset_trading_signals(
                        latest,
                        symbol_info_mt5,
                        asset=asset,
                        mt5_connector=mt5_connector,
                    )
                    or {}
                )
                # --- WNT-1: attacher 'latest' pour diagnostic WHY_NO_TRADE (lecture seule)
                signals["__latest"] = (
                    latest  # permet d'accéder à footprint/orderflow summaries si non recopiés par _build_asset_trading_signals
                )

                if CANDLES_ENABLED:
                    _pat = market_results.get("patterns", {})
                    if _pat:
                        signals.update(_pat)

                signals["phase"] = market_results.get(
                    "phase", signals.get("phase", "neutral")
                )
                signals["confidence_score"] = market_results.get(
                    "confidence", signals.get("confidence_score", 0.5)
                )
                signals["structure"] = market_results.get("structure", {})

                # Spread robuste
                spread_pts = (
                    getattr(symbol_info_mt5, "spread", None)
                    if symbol_info_mt5
                    else None
                )
                if not spread_pts or spread_pts <= 0:
                    spread_pts = mt5_connector.get_symbol_spread_points(asset) or float(
                        "inf"
                    )
                signals["current_spread_points"] = float(spread_pts)
                # === PATCH A: expose FP/OF dans les signaux pour debug ultérieur ===
                signals["footprint_summary"] = latest.get("footprint_summary")
                signals["orderflow_summary"] = latest.get("orderflow_summary")

                # Sauvegarde
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

        # Trace pipeline synthétique
        print("\n" + "=" * 60)
        print("🔍 TRACE COMPLÈTE DU PIPELINE:")
        for asset, sig in all_assets_trading_signals.items():
            print(
                f"   {asset}: phase={sig.get('phase')} conf={sig.get('confidence_score')}"
            )
        print("=" * 60)
        # === PATCH B: GATECHECK (XAUUSD only) — imprime métriques vs seuils ===
        try:
            sig = (all_assets_trading_signals or {}).get("XAUUSD", {})
            if sig:
                fp = sig.get("footprint_summary") or {}
                of = sig.get("orderflow_summary") or {}

                xcfg = (
                    ((asset_configs or {}).get("XAUUSD", {}) or {}).get("overrides", {})
                    or {}
                ).get("scalping", {}) or {}
                fpc = xcfg.get("footprint", {}) or {}
                bt = fpc.get("burst_tolerance", {}) or {}
                ncp = (xcfg.get("phase_detection", {}) or {}).get(
                    "allow_no_clear_phase_if_strong", {}
                ) or {}

                phase = sig.get("phase")
                conf = sig.get("confidence_score")
                spread = sig.get("current_spread_points")
                ticks = fp.get("tick_count")
                cov = fp.get("coverage_s")
                trate = fp.get("tick_rate")
                dlt = (
                    of.get("delta_total")
                    if isinstance(of.get("delta_total"), (int, float))
                    else None
                )
                imb = (
                    of.get("mean_imbalance")
                    if isinstance(of.get("mean_imbalance"), (int, float))
                    else None
                )

                print(
                    "[GATECHECK][XAUUSD] "
                    f"phase={phase} conf={conf:.3f if isinstance(conf,(int,float)) else conf} spread={spread} | "
                    f"FP ticks={ticks} cov={cov}s rate={round(trate,2) if isinstance(trate,(int,float)) else trate}/s "
                    f"(TH: ticks≥{fpc.get('m1_min_ticks','?')}, cov≥{fpc.get('m1_min_coverage_s','?')}s "
                    f"OR burst≥{bt.get('tickrate_min','?')}/s & ≥{bt.get('coverage_s_min_burst','?')}s) | "
                    f"OF Δ={dlt} imb={round(imb,2) if isinstance(imb,(int,float)) else imb} "
                    f"(NCP-strong: Δ≥{ncp.get('of_delta_abs_min','?')} & rate≥{ncp.get('tickrate_min','?')}/s)"
                )
        except Exception as _e:
            logger.debug(f"[GATECHECK][XAUUSD] skip: {_e}")

        # Charger configs des assets (une seule fois via cache du ConfigManager)
        asset_configs = {}
        for asset in tradeable_assets:
            try:
                cfg = config_manager.load_asset_config(asset)
                if cfg:
                    asset_configs[asset] = cfg
            except Exception as e:
                logger.warning(f"[{asset}] Impossible de charger la config: {e}")

        # Contexte global
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
        # ✅ diag_tracker sûr (plus de NameError sur DiagnosticTracker)
        global_context["diag_tracker"] = get_tracker_from_context(global_context)
        print("✅ [PIPELINE] Contexte global construit avec succès !")
        print(f"2️⃣ CONTEXT KEYS: {list(global_context.keys())}")

        # === [BURST EXIT MANAGEMENT] Fermer les paniers avant toute nouvelle décision ===
        try:
            burst_cfg = (
                base_config.get("entry_rules", {})
                .get("scalping", {})
                .get("burst_scalping", {})
                or {}
            )
            trail_cfg = burst_cfg.get("trailing", {}) or {}
            closure_cfg = burst_cfg.get("closure_rules", {}) or {}

            # 2) Monitoring collectif (perte max + trailing collectif)
            trade_executor.monitor_burst_baskets(
                config=base_config,
                max_loss_pips=float(closure_cfg.get("max_loss_pips", 15.0)),
                trail_trigger=float(trail_cfg.get("trigger_pips", 10.0)),
                trail_step=float(trail_cfg.get("step_pips", 5.0)),
            )
        except Exception as e:
            logger.warning(f"[BURST EXIT] Contrôle fermeture panier: {e}")

        # Appel pipeline de décision
        print("🤖 [PIPELINE] Appel du decision_pipeline...")
        decision_package = (
            decision_pipeline.institutional_decision_pipeline(global_context) or {}
        )
        
        # === MERGE: décisions Footprint (Scalping Burst) dans le package ===
        try:
            if footprint_scalping_decisions:
                decision_package.setdefault("scalping_decisions", [])
                decision_package["scalping_decisions"].extend(footprint_scalping_decisions)
                # Si aucune décision finale n'a été posée, on promeut la première footprint
                decision_package.setdefault("final_decision", decision_package.get("final_decision") or {})
                if not decision_package["final_decision"] and decision_package["scalping_decisions"]:
                    decision_package["final_decision"] = decision_package["scalping_decisions"][0]
                logger.info(f"[MERGE] {len(footprint_scalping_decisions)} décision(s) Footprint intégrée(s).")
        except Exception as e:
            logger.warning(f"[MERGE] Échec intégration décisions Footprint: {e}")


        # ====== LOG DÉCISION (anti-doublon) ======
        scalping_decisions = decision_package.get("scalping_decisions", []) or []
        liquidity_decisions = decision_package.get("liquidity_decisions", []) or []

        # Compatibilité : fusion pour les logs globaux
        final_decisions = scalping_decisions + liquidity_decisions

        final = decision_package.get("final_decision", {}) or {}
        ctx_out = decision_package.get("context", {}) or {}

        # 🔥 si plusieurs décisions, on les log toutes
        if final_decisions:
            print("📦 [PIPELINE] Décisions multiples détectées:")
            for d in final_decisions:
                print(
                    f"   → {d.get('action')} {d.get('asset')} | vol={d.get('volume', 0)}"
                )
        else:
            print("📦 [PIPELINE] Aucune décision multiple détectée.")

        # Seulement si la pipeline n'a PAS déjà loggué elle-même
        if not ctx_out.get("__decision_logged"):
            print("3️⃣ DÉCISION RETOURNÉE:")
            print(f"   Action: {final.get('action', 'NONE')}")
            print(f"   Asset: {final.get('asset', 'NONE')}")
            print(f"   Volume: {final.get('volume', 0)}")
            print(
                "   ✅ TRADE DÉCIDÉ !"
                if str(final.get("action", "")).upper() in {"BUY", "SELL"}
                else "   ❌ PAS DE TRADE"
            )
            print("=" * 60 + "\n")

        # === EXÉCUTION DES DÉCISIONS ===

        scalping_decisions = decision_package.get("scalping_decisions", []) or []
        liquidity_decisions = decision_package.get("liquidity_decisions", []) or []

        if not scalping_decisions and not liquidity_decisions:
            # --- WNT-2: WHY_NO_TRADE (une ligne par actif) ---
            try:
                SPREAD_MAX = {"EURUSD": 12, "GBPUSD": 18, "XAUUSD": 40}
                # seuils minimums footprint M1 + tolérance burst (lecture seule, ne bloque rien)
                FP_MIN = {
                    "EURUSD": (15, 20),
                    "GBPUSD": (15, 20),
                    "XAUUSD": (20, 10),
                }  # (ticks_min, coverage_s_min)
                BURST_TR_MIN = 2.0
                BURST_COV_MIN = 6.0  # 5–6s ok pour XAUUSD burst court

                for asset, sig in all_assets_trading_signals.items():
                    try:
                        phase = sig.get("phase")
                        conf = float(sig.get("confidence_score", 0.0))
                        spread = float(sig.get("current_spread_points", float("inf")))

                        # récupérer résumés footprint/orderflow (depuis signals ou fallback __latest)
                        latest = sig.get("__latest", {}) or {}
                        fp_sum = (
                            sig.get("footprint_summary")
                            or latest.get("footprint_summary")
                            or {}
                        )
                        of_sum = (
                            sig.get("orderflow_summary")
                            or latest.get("orderflow_summary")
                            or {}
                        )

                        ticks = int(fp_sum.get("tick_count", 0) or 0)
                        cov = float(fp_sum.get("coverage_s", 0.0) or 0.0)
                        tr = float(fp_sum.get("tick_rate", 0.0) or 0.0)

                        # 1) qualité footprint M1
                        tmin, cmin = FP_MIN.get(asset, (15, 20))
                        reasons = []
                        if ticks < 3:
                            reasons.append("FP_HARD_FAIL")
                        elif (ticks < tmin or cov < cmin) and not (
                            tr >= BURST_TR_MIN and cov >= BURST_COV_MIN
                        ):
                            reasons.append("FP_LOW_SAMPLE")

                        # 2) spread
                        if spread > SPREAD_MAX.get(asset, 999):
                            reasons.append("SPREAD_TOO_WIDE")

                        # 3) confiance phase
                        if conf < 0.52:
                            reasons.append("CONF_LOW")

                        # 4) direction (vote simple CVD/Δ OF + Δ FP)
                        votes = 0
                        try:
                            cvd = float(
                                of_sum.get("cvd_final", of_sum.get("CVD", 0.0)) or 0.0
                            )
                            if cvd != 0:
                                votes += 1 if cvd > 0 else -1
                        except Exception:
                            pass
                        try:
                            delt_of = float(
                                of_sum.get("delta_total", of_sum.get("Δ", 0.0)) or 0.0
                            )
                            if delt_of != 0:
                                votes += 1 if delt_of > 0 else -1
                        except Exception:
                            pass
                        try:
                            delt_fp = float(fp_sum.get("delta_total", 0.0) or 0.0)
                            if delt_fp != 0:
                                votes += 1 if delt_fp > 0 else -1
                        except Exception:
                            pass
                        if abs(votes) < 2:
                            reasons.append("DIR_UNCLEAR")

                        if not reasons:
                            reasons = ["NO_SETUP"]

                        logger.info(
                            f"[WHY_NO_TRADE][{asset}] phase={phase} conf={conf:.3f} spread={spread} | "
                            f"FP(ticks={ticks},win={cov:.0f}s,tr={tr:.2f}/s) | reasons="
                            + ",".join(reasons)
                        )
                    except Exception as _e:
                        logger.info(f"[WHY_NO_TRADE][{asset}] DIAG_ERROR: {_e}")
            except Exception:
                pass
            # --- /WNT-2 ---

            print("📦 [PIPELINE] Aucune décision détectée.")
            logger.info("Aucun trade décidé ce cycle.")
            return False

        # --- Exécution Scalping ---
        if scalping_decisions:
            print("📦 [PIPELINE] Décisions Scalping détectées:")
            for d in scalping_decisions:
                print(f"   → {d.get('action')} {d.get('asset')} | vol={d.get('volume', 0)}")

            # === [BURST GUARD PIPELINE] bloque tout nouveau burst si un panier est actif (scope global) ===
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
                            m = re.search(r"burst_scalping\|basket=([A-Za-z0-9_]+)", c)
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
                # ✅ Toujours valider/normaliser le side en 1er (évite UnboundLocalError)
                side = str(td.get("action") or td.get("side") or "").upper().strip()
                if side not in {"BUY", "SELL"}:
                    logger.debug(f"[SCALPING] décision ignorée (side invalide): {td}")
                    continue

                # 🔧 Standardiser le rule_name + activer trailing/No-TP pour burst
                try:
                    if str(td.get("rule_name", "")).lower() in ("burst", "burst_master", "scalping_burst", ""):
                        td["rule_name"] = "burst_scalping"
                    rn = str(td.get("rule_name") or "burst_scalping").lower()

                    if rn == "burst_scalping":
                        for k in ("tp_price", "tp_pips", "target_tp_pips", "tp_prices"):
                            td.pop(k, None)
                        td["no_tp"] = True
                        trailing_cfg = (
                            (td.get("trailing") or {}) if td.get("trailing") else {}
                        ) or (
                            (global_context.get("asset_configs", {}) or {})
                            .get(td.get("asset", ""), {})
                            .get("entry_rules", {})
                            .get("scalping", {})
                            .get("burst_scalping", {})
                            .get("trailing", {})
                            or {}
                        )
                        if trailing_cfg.get("enabled", True):
                            td["trailing"] = {
                                "enabled": True,
                                "activate_after_rr": float(trailing_cfg.get("activate_after_rr", 1.0)),
                                "step_pips": float(trailing_cfg.get("step_pips", 5)),
                            }
                except Exception:
                    pass

                rn = str(td.get("rule_name", "burst_scalping")).lower()
                # On FORCE le style LIMIT_FOK pour tout burst, pour garantir le split multi-ordres
                td["entry_style"] = "LIMIT_FOK"
                entry_style = "LIMIT_FOK"


                burst_cfg = (
                    base_config.get("entry_rules", {})
                    .get("scalping", {})
                    .get("burst_scalping", {})
                    or {}
                )
                order_cfg = (burst_cfg.get("order", {}) or {})
                default_count = int(order_cfg.get("burst_count", 5))
                default_each  = float(order_cfg.get("burst_volume_each", 0.02))

                burst_count = int(td.get("burst_count") or default_count)
                burst_each  = float(td.get("burst_volume_each") or (
                                    float(td.get("volume", 0) or 0) / max(1, burst_count)
                                ) or default_each)

                sym = str(td.get("asset") or td.get("symbol") or "").upper()

                # 🎯 Prix d'entrée (Ask pour BUY, Bid pour SELL)
                price_val = float(td.get("price") or 0.0)
                if price_val <= 0.0:
                    try:
                        tk = mt5_connector.get_symbol_tick(sym) or {}
                        ask = tk.get("ask", getattr(tk, "ask", None))
                        bid = tk.get("bid", getattr(tk, "bid", None))
                        price_val = float(ask if side == "BUY" else bid) if (ask or bid) else 0.0
                    except Exception:
                        price_val = 0.0
                # Fallback natif MT5 si le wrapper ne renvoie rien
                if price_val <= 0.0:
                    try:
                        mt5mod = getattr(mt5_connector, "mt5", None)
                        if mt5mod:
                            tk = mt5mod.symbol_info_tick(sym)
                            if tk:
                                price_val = float(tk.ask if side == "BUY" else tk.bid)
                    except Exception:
                        price_val = 0.0
                
                # 🚀 CAS 1 — burst : split dès qu'on a un prix
                logger.info(f"[BURST][PLAN] {side} {sym} style={entry_style} count={burst_count} each={burst_each} price={price_val}")
                if rn == "burst_scalping" and price_val > 0:

                    basket_id = td.get("basket_id") or td.get("comment") or f"burst_{sym}"
                    if is_dry_run:
                        logger.info(f"[BURST][DRY] {side} {sym} LIMIT+FOK x{burst_count} @ {price_val:.2f} (each={burst_each})")
                        trade_executed_successfully = True
                        continue

                    for i in range(burst_count):
                        child = {
                            "action": side,
                            "asset": sym,
                            "order_type": "BUY_LIMIT" if side == "BUY" else "SELL_LIMIT",
                            "price": price_val,
                            "volume": burst_each,
                            "time_in_force": "FOK",
                            "validity_ms": int(td.get("validity_ms", 800)),
                            "rule_name": "burst_scalping",
                            "comment": f"burst_scalping|basket={basket_id}|child={i+1}/{burst_count}",
                            "no_tp": True,
                        }
                        exec_pkg = {
                            "final_decision": child,
                            "context": global_context,
                            "active_config": base_config,
                        }
                        res = run_trade_execution_pipeline(trade_executor, exec_pkg, is_dry_run=False)
                        status = str((res or {}).get("status", "")).lower()
                        if status in {"ok", "success", "filled"}:
                            trade_executed_successfully = True
                        else:
                            logger.warning(f"[BURST][{sym}] enfant {i+1}/{burst_count} non rempli (ret={res}).")

                    # Trailing/guard panier après envois
                    try:
                        tr_cfg = (burst_cfg.get("trailing", {}) or {})
                        cl_cfg = (burst_cfg.get("closure_rules", {}) or {})
                        trade_executor.monitor_burst_baskets(
                            config=base_config,
                            max_loss_pips=float(cl_cfg.get("max_loss_pips", 15.0)),
                            trail_trigger=float(tr_cfg.get("trigger_pips", 10.0)),
                            trail_step=float(tr_cfg.get("step_pips", 5.0)),
                        )
                    except Exception as e:
                        logger.warning(f"[BURST EXIT] Post-exec trailing setup: {e}")
                    continue

                # 🪂 CAS 2 — fallback (MARKET / non-burst) via exécuteur unifié
                decision_pkg = {
                    "final_decision": td,
                    "context": global_context,
                    "active_config": base_config,
                }
                res = run_trade_execution_pipeline(
                    trade_executor, decision_pkg, is_dry_run=is_dry_run
                )
                status = (res or {}).get("status", "")
                if status not in {"failed", ""}:
                    trade_executed_successfully = True
                    try:
                        tr_cfg = (burst_cfg.get("trailing", {}) or {})
                        trade_executor.monitor_burst_baskets(
                            config=base_config,
                            max_loss_pips=15.0,
                            trail_trigger=float(tr_cfg.get("trigger_pips", 10.0)),
                            trail_step=float(tr_cfg.get("step_pips", 5.0)),
                        )
                    except Exception as e:
                        logger.warning(f"[BURST EXIT] Post-exec trailing setup: {e}")



        # --- Exécution Liquidity ---
        if liquidity_decisions:
            print("📦 [PIPELINE] Décisions Liquidity détectées:")
            for d in liquidity_decisions:
                print(
                    f"   → {d.get('action')} {d.get('asset')} | vol={d.get('volume', 0)}"
                )

            for td in liquidity_decisions:
                action = str(td.get("action", "")).upper()
                if action in {"BUY", "SELL"}:
                    _execute_single_decision(
                        td,
                        trade_executor,
                        mt5_connector,
                        global_context,
                        decision_package,
                        execution_mode,
                        logger,
                    )

        # --- Vérification des EXIT Liquidity (sorties forcées) ---
        try:
            current_positions = mt5_connector.get_positions()
            if current_positions:
                from strategy.liquidity import LiquidityStrategy

                liq_cfg = (
                    config_manager.get("strategies", {}).get("liquidity", {}) or {}
                )
                liqui = LiquidityStrategy(config_manager, liq_cfg)

                exit_decisions = liqui.evaluate_exit(global_context, current_positions)
                if exit_decisions:
                    trade_executor.execute_exit_orders(
                        exit_decisions, is_dry_run=is_dry_run
                    )
                    logger.info(
                        f"[LIQUIDITY] {len(exit_decisions)} sortie(s) exécutée(s)."
                    )
                    print(
                        f"💧 [PIPELINE] EXIT Liquidity exécuté: {len(exit_decisions)} trades fermés."
                    )
        except Exception as e:
            logger.error(f"[PIPELINE] Erreur exit Liquidity: {e}", exc_info=True)

    except Exception as e:
        logger.error(f"Erreur pipeline: {e}", exc_info=True)
        trade_executed_successfully = False
    finally:
        try:
            get_tracker_from_context(global_context).emit_summary(logger)
        except Exception:
            pass
        return trade_executed_successfully


def main(args: argparse.Namespace) -> None:
    """
    Fonction principale pour initialiser le bot, gérer les arguments de la CLI,
    et lancer la boucle de trading infinie.

    Args:
        args (argparse.Namespace): Les arguments parsés de la ligne de commande.
    """
    # 1. Configuration du Logging de Production (Appelé en premier)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    logger = logging.getLogger(__name__)

    # 2. Initialisation du ConfigManager
    from core.config_manager import ConfigManager

    config_manager = ConfigManager.get_instance()

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

        # Instancier AI Decision
        models_dir = config_manager.get("paths.models", "models/")
        ai_model_name_for_init = config_manager.get(
            "ai.model_name", "llama-2-7b-chat.Q4_K_M.gguf"
        )
        ai_decision_model_full_path = Path(models_dir) / ai_model_name_for_init
        ai_decision = AIDecision(
            model_path=str(ai_decision_model_full_path),
            config_manager_instance=config_manager,
        )

        # Instancier TradeExecutor
        trade_executor = TradeExecutor(
            config_manager=config_manager, mt5_connector=mt5_connector, mode=bot_mode
        )

        # Instancier Mecano
        mecano = Mecano(config_manager_instance=config_manager)
        mecano.set_ai_analyzer(ai_decision)

        # Instancier StrategyManager
        strategy_manager = StrategyManager(
            config_loader_instance=config_manager.config_loader,
            config_manager_instance=config_manager,
        )
        strategy_manager.initialize_strategies()

    except Exception as e:
        logger.critical(
            f"FATAL: Erreur lors de l'initialisation des modules fondamentaux: {e}",
            exc_info=True,
        )
        sys.exit(1)

    # 6. Vérification Finale de l'Environnement et des Modules
    try:
        from run_bot import verify_environment_and_config, run_single_pipeline_cycle

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

    # Réconciliation initiale TradeExecutor
    try:
        account_details_for_reconciliation = config_manager.get_mt5_account_credentials(
            mode=bot_mode
        )
        if account_details_for_reconciliation:
            if mt5_connector.connect(account_details_for_reconciliation):
                trade_executor.reconcile_state_with_broker()
                logger.info("Réconciliation TradeExecutor OK.")
                mt5_connector.disconnect()
            else:
                logger.warning("MT5 non connecté pour la réconciliation. Ignorée.")
        else:
            logger.warning("Aucun compte MT5 dispo pour la réconciliation.")
    except Exception as e:
        logger.error(f"Échec réconciliation TradeExecutor: {e}", exc_info=True)
        config_manager.send_alert(
            f"ALERTE: Réconciliation TradeExecutor échouée: {e}",
            "telegram_critical",
        )

    # 9. Boucle Principale
    try:
        while True:
            cycle_count += 1
            cycle_start_time = time.time()

            trade_executed_in_cycle = run_single_pipeline_cycle(
                mt5_connector,
                config_manager.decision_pipeline,
                trade_executor,
                config_manager,
                mecano,
                strategy_manager,  # ✅ ici tu passes l’instance
                is_dry_run,
                cycle_count,
                daily_trade_count,
            )

            if trade_executed_in_cycle:
                daily_trade_count += 1

                # === Surveillance des ordres LIMIT Liquidity ===
            try:
                trade_executor.monitor_pending_orders()
            except Exception as e:
                logger.warning(
                    f"[LIQUIDITY] Erreur lors du monitor_pending_orders: {e}"
                )

            cycle_duration = time.time() - cycle_start_time
            logger.info(
                f"[PERF] Cycle #{cycle_count} exécuté en {cycle_duration:.2f}s."
            )

            cycle_duration = time.time() - cycle_start_time
            logger.info(
                f"[PERF] Cycle #{cycle_count} exécuté en {cycle_duration:.2f}s."
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
                logger.info(f"Prochain cycle dans {sleep_time:.2f}s...")
                time.sleep(sleep_time)

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
        if "ai_decision" in locals() and ai_decision:
            logger.info("Sauvegarde historique IA avant arrêt...")
            ai_decision._save_suggestion_history()

        if "mt5_connector" in locals() and mt5_connector.is_connected():
            mt5_connector.disconnect()

        logger.info("SNIPER_X Bot est arrêté.")
        sys.exit(0)
