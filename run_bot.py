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
from datetime import datetime,timedelta, timezone
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


def _mtf_readiness_gate(
    mt5_connector, market_analyzer, config_manager, tradeable_assets, cycle_count
) -> bool:
    """
    Gate MTF BLOQUANT… mais *gracieux* :
    - Si 'readiness_gate.enabled' est False (ou absent) -> ON LAISSE PASSER.
    - Overrides possibles via prod_config.runtime_flags :
        * readiness_gate_enabled (bool)       : force on/off global
        * startup_skip_cycles (int >= 0)      : remplace block_signals_first_n_cycles
        * max_allowed_data_age_seconds (int)  : remplace la fraîcheur de gate_cfg
    - Si une erreur survient (lecture config, etc.) -> ON LAISSE PASSER.
    - Sinon on vérifie:
        * N premiers cycles bloqués,
        * historique minimum par TF (incluant le plancher 'bars_min' de prod_config.timeframe_mapping),
        * fraîcheur des données (max_allowed_data_age_seconds) si défini,
        * confluence MTF via PhaseObserver si dispo.
    """
    logger = logging.getLogger(__name__)
    try:
        po_cfg = _load_po_config_safe(
            config_manager
        )  # phase_observer_config.json (souple)

        gate_cfg = po_cfg.get("readiness_gate") or {}
        mtf_cfg = po_cfg.get("multi_timeframe_settings") or {}
        data_req = po_cfg.get("data_requirements") or {}

        # ---------- Overrides prod_config ----------
        rf = {}
        try:
            rf = config_manager.get("runtime_flags", {}) or {}
        except Exception:
            rf = {}

        # enabled: prod_config override > phase_observer config > default True
        rf_enabled = rf.get("readiness_gate_enabled")
        if rf_enabled is not None:
            gate_enabled = bool(rf_enabled)
        else:
            gate_enabled = bool(gate_cfg.get("enabled", False))

        if not gate_enabled:
            return True  # gate désactivé globalement

        # timeframes requis
        required_tfs = [
            str(tf).upper() for tf in mtf_cfg.get("timeframes", ["M1", "M5", "M15"])
        ]
        confluence_required = int(mtf_cfg.get("confluence_required", 2))

        # N cycles de blocage (override par prod_config.runtime_flags.startup_skip_cycles si présent)
        block_first_cycles = int(gate_cfg.get("block_signals_first_n_cycles", 12))
        if isinstance(rf.get("startup_skip_cycles"), (int, float)):
            try:
                rf_skip = int(rf.get("startup_skip_cycles"))
                if rf_skip >= 0:
                    block_first_cycles = rf_skip
            except Exception:
                pass

        # Fraîcheur (override par prod_config.runtime_flags.max_allowed_data_age_seconds si présent)
        max_age_sec = int(gate_cfg.get("max_allowed_data_age_seconds", 0) or 0)
        if isinstance(rf.get("max_allowed_data_age_seconds"), (int, float)):
            try:
                rf_age = int(rf.get("max_allowed_data_age_seconds"))
                if rf_age >= 0:
                    max_age_sec = rf_age
            except Exception:
                pass

        # (1) Gate de démarrage
        if cycle_count <= block_first_cycles:
            logger.info(
                f"[READINESS] skip -> startup gate ({cycle_count}/{block_first_cycles})"
            )
            return False

        # (2) Construire le min bars par TF en combinant config PhaseObserver + prod_config.timeframe_mapping.bars_min
        min_bars_by_tf = {
            k.upper(): int(v)
            for k, v in (
                data_req.get(
                    "min_bars_by_timeframe", {"M1": 500, "M5": 300, "M15": 200}
                ).items()
            )
        }
        try:
            tf_map = config_manager.get("timeframe_mapping", {}) or {}
            for tfk, obj in tf_map.items():
                tfu = str(tfk).upper()
                bars_min = int((obj or {}).get("bars_min", 0) or 0)
                if bars_min > 0:
                    min_bars_by_tf[tfu] = max(min_bars_by_tf.get(tfu, 0), bars_min)
        except Exception:
            pass  # on ignore si non présent

        # Si pas d'actifs, ne pas bloquer
        if not tradeable_assets:
            return True

        # (3) Historique minimum + fraîcheur par TF / actif
        from datetime import datetime, timezone

        def _to_utc_dt(ts_val):
            """Convertit de manière robuste le dernier 'time' en datetime UTC."""
            try:
                # Pandas Timestamp
                if hasattr(ts_val, "to_pydatetime"):
                    dt = ts_val.to_pydatetime()
                else:
                    dt = ts_val
                # Numérique epoch (sec)
                if isinstance(dt, (int, float)):
                    dt = datetime.utcfromtimestamp(int(dt))
                # datetime naïf -> UTC
                if isinstance(dt, datetime) and dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except Exception:
                return None

        for asset in tradeable_assets:
            for tf in required_tfs:
                need = int(min_bars_by_tf.get(tf, 200))
                df = mt5_connector.get_rates(asset, tf, need)
                have = len(df) if df is not None else 0
                if have < need:
                    logger.info(
                        f"[READINESS] skip -> {asset} {tf}={have}/{need} (historique insuffisant)"
                    )
                    return False

                # Fraîcheur des données (optionnelle)
                if (
                    max_age_sec > 0
                    and df is not None
                    and not df.empty
                    and "time" in df.columns
                ):
                    try:
                        last_ts = df["time"].iloc[-1]
                        last_dt = _to_utc_dt(last_ts)
                        if last_dt is not None:
                            age = (datetime.now(timezone.utc) - last_dt).total_seconds()
                            if age > max_age_sec:
                                logger.info(
                                    f"[READINESS] skip -> {asset} {tf} data too old ({int(age)}s > {max_age_sec}s)"
                                )
                                return False
                    except Exception:
                        # on reste permissif si le parse de temps pose souci
                        pass
                    # (4) Confluence via MarketAnalyzer
                    if hasattr(market_analyzer, "ready_and_confluence_ok"):
                        ok, reason = market_analyzer.ready_and_confluence_ok(
                            confluence_required=confluence_required
                        )
                        if not ok:
                            logger.info(f"[READINESS] skip -> {reason}")
                            return False

        return True

    except Exception as e:
        # Ne jamais bloquer si une erreur inattendue survient
        logger.warning(f"[READINESS] erreur inattendue -> passage permissif: {e}")
        return True


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
    print(f"🔍 [PIPELINE] Cycle #{cycle_count} - Début de run_single_pipeline_cycle")
    logger.info(
        f"--- Démarrage du Cycle de Pipeline #{cycle_count} (Trades Aujourd'hui: {daily_trade_count}) ---"
    )

    trade_executed_successfully = False
    global_context: Dict[str, Any] = {}

    # ✅ MarketAnalyzer unifié
    market_analyzer = MarketAnalyzer(config_manager=config_manager, logger=logger)

    try:
        if not getattr(mt5_connector, "is_connected", False):
            raise RuntimeError("MT5 a perdu la connexion persistante.")

        base_config = config_manager.get_current_dynamic_config()
        execution_mode = str(base_config.get("mode_execution", "DEMO")).upper()
        active_mt5_account_details = config_manager.get_mt5_account_credentials(
            mode=execution_mode
        )

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

                # ✅ Mode "horloge suisse"
                if cycle_count == 1:
                    # 1️⃣ Premier cycle : on fait une analyse complète (200 barres)
                    market_results = market_analyzer.analyze(rates_df.copy(), asset)
                    # Initialiser l'historique dans PhaseObserver
                    market_analyzer.phase_observer.load_initial_history(rates_df.copy())
                else:
                    # 2️⃣ Cycles suivants : analyse incrémentale dernière bougie
                    last_bar = rates_df.iloc[-1].to_dict()
                    last_signals = market_analyzer.phase_observer.on_bar_close(
                        last_bar, asset_symbol=asset
                    )

                    # ⚡ Corrigé : construire un market_results complet
                    market_results = {
                        "latest": last_signals,
                        "annotated_df": market_analyzer.phase_observer._history_df.copy(),
                        "patterns": {},  # à remplir si besoin (détecteurs patterns)
                        "phase": last_signals.get("phase_primary", "neutral"),
                        "confidence": last_signals.get("confidence_score", 0.5),
                        "structure": {},  # placeholder si tu veux garder la cohérence
                    }

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
                # === LOG DIAGNOSTIQUE HISTORIQUE 200 BOUgies ===
                try:
                    last_200 = annotated_rates_df.tail(200)
                    avg_vol = last_200["tick_volume"].mean() if "tick_volume" in last_200 else None
                    avg_range = (last_200["high"] - last_200["low"]).mean() if {"high","low"} <= set(last_200.columns) else None
                    bull_candles = int((last_200["close"] > last_200["open"]).sum()) if {"close","open"} <= set(last_200.columns) else 0
                    bear_candles = int((last_200["close"] < last_200["open"]).sum()) if {"close","open"} <= set(last_200.columns) else 0

                    logger.info(
                        f"[{asset}] Historique(200 bougies) → "
                        f"Bull={bull_candles}, Bear={bear_candles}, "
                        f"VolMoy={avg_vol:.2f} | RangeMoy={avg_range:.5f}"
                    )
                except Exception as e:
                    logger.warning(f"[{asset}] Impossible de résumer l’historique 200 bougies: {e}")
                
                # === PATCH FOOTPRINT ANALYSE (corrigé UTC + copy-safe + fallback nearest) ===
                try:
                    ticks_df = mt5_connector.get_ticks(asset, count=2000)

                    # ✅ Forcer UTC sur candles & ticks pour éviter mismatch
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
                            candle_index=None,          # dernière bougie
                            price_step=None,            # auto-calcul
                            imbalance_threshold=0.7     # param ajustable
                        )

                        # ✅ Logging détaillé footprint
                        logger.info(
                            f"[FOOTPRINT][{asset}] Score={fp_res.get('score', 0)} | "
                            f"Status={fp_res.get('status', 'N/A')} | "
                            f"Summary={fp_res.get('summary', {})}"
                        )

                        if fp_res.get("summary", {}).get("comments", "").startswith("Fallback nearest"):
                            logger.warning(f"[FOOTPRINT][{asset}] ⚠️ Aucun tick dans la fenêtre — fallback sur ticks voisins.")

                        # ✅ Copy-safe (évite SettingWithCopyWarning)
                        latest = dict(latest)
                        latest["footprint_score"] = fp_res.get("score", 0)
                        latest["footprint_status"] = fp_res.get("status", "N/A")
                        latest["footprint_summary"] = fp_res.get("summary", {})

                    else:
                        logger.warning(f"[FOOTPRINT][{asset}] Aucun tick reçu → skip.")

                except Exception as e:
                    logger.error(f"[FOOTPRINT][{asset}] Erreur analyse ticks: {e}", exc_info=True)



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
                signals.update(market_results.get("patterns", {}))
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

        # Appel pipeline de décision
        print("🤖 [PIPELINE] Appel du decision_pipeline...")
        decision_package = (
            decision_pipeline.institutional_decision_pipeline(global_context) or {}
        )

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
            print("📦 [PIPELINE] Aucune décision détectée.")
            logger.info("Aucun trade décidé ce cycle.")
            return False

        # --- Exécution Scalping ---
        if scalping_decisions:
            print("📦 [PIPELINE] Décisions Scalping détectées:")
            for d in scalping_decisions:
                print(
                    f"   → {d.get('action')} {d.get('asset')} | vol={d.get('volume', 0)}"
                )

            for td in scalping_decisions:
                action = str(td.get("action", "")).upper()

                # 🔧 PATCH (NO TP pour BURST) — à INSÉRER AVANT l'appel _execute_single_decision
                try:
                    rule_name = str(td.get("rule_name", "")).lower()
                    if rule_name == "burst_scalping":
                        # 1) on supprime TOUT ce qui peut (ré)injecter un TP
                        for k in ("tp_price", "tp_pips", "target_tp_pips", "tp_prices"):
                            if k in td:
                                td.pop(k, None)

                        # 2) drapeau clair pour l’exécuteur
                        td["no_tp"] = True

                        # 3) trailing forcé si la config burst le prévoit
                        trailing_cfg = (
                            (td.get("trailing") or {}) if td.get("trailing") else {}
                        ) or (  # déjà présent ?
                            (
                                (global_context.get("asset_configs", {}) or {})
                                .get(td.get("asset", ""), {})
                                .get("entry_rules", {})
                                .get("scalping", {})
                                .get("burst_scalping", {})
                                .get("trailing", {})
                            )
                            or {}
                        )
                        if trailing_cfg.get("enabled", True):
                            td["trailing"] = {
                                "enabled": True,
                                "activate_after_rr": float(
                                    trailing_cfg.get("activate_after_rr", 1.0)
                                ),
                                "step_pips": float(trailing_cfg.get("step_pips", 5)),
                            }
                except Exception:
                    pass
                # /PATCH

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

        # 🔒 Gate MTF : s’assurer qu’on a l’historique avant de lancer le premier cycle
        if not _mtf_readiness_gate(config_manager, mt5_connector, logger):
            logger.critical("Readiness MTF non validé -> arrêt du bot.")
            sys.exit(1)

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
