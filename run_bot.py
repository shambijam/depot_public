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
import re
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
from phase_observer.fusion_manager import FusionManager


load_dotenv()

try:
    from phase_observer.detect_orderflow_v6.orderflow_v6 import detect_orderflow_v6
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
        base_pkg = {
            "active_config": decision_package.get("config_used", {}) or {},
            "market_context": global_context,
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
) -> bool:
    """
    Exécute un cycle complet du pipeline de trading de SNIPER_X.

    Architecture:
      - MarketAnalyzer (PhaseObserver + PatternEngine) → annotated_df + latest
      - Analyses d'entrée pour la fusion: Footprint M1 (ticks bougie) + Orderflow v5
      - Signals consolidés par actif
      - FusionManager → décisions scalping (XAUUSD uniquement)
      - Fast-lane d'exécution si décision Fusion valide (avec gardes panier/slippage)
      - decision_pipeline pour le reste (ex: Liquidity), mais scalping filtré Fusion-only
      - SLTP dynamique (trailing-only pour scalping), maintenance périodique
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

    # === FusionManager requis pour scalping XAUUSD ===
    try:
        from phase_observer.fusion_manager import FusionManager

        _fusion_mgr = FusionManager(logger=logger)
    except Exception:
        _fusion_mgr = None

    # --- Fallback vote simple (jamais utilisé si REQUIRE_FUSION_MGR=True) ---
    def _quick_vote_fusion(
        signals: dict, latest: dict, base_cfg: dict, sym: str, mt5c: MT5Connector
    ):
        """
        Vote rapide (secours) en l'absence de décision FusionManager.
        - Garde spread robuste (ignore NaN/inf).
        - Garde footprint assouplie + mode dégradé si Orderflow V6 est très fort.
        - Score combiné (confiance, FP, OF) + bonus d'alignement.
        - Retourne un paquet compatible avec la fast-lane.
        """
        import math, time

        try:
            # --- Helpers ---
            def _dig(d, path, default=None):
                cur = d or {}
                for k in path:
                    if not isinstance(cur, dict):
                        return default
                    cur = cur.get(k)
                return cur if cur is not None else default

            def _safe_float(x, default=0.0):
                try:
                    v = float(x)
                    if math.isnan(v) or math.isinf(v):
                        return default
                    return v
                except Exception:
                    return default

            # --- Inputs ---
            spread = _safe_float(
                signals.get("current_spread_points"), default=float("nan")
            )
            phase = str(signals.get("phase", "neutral") or "neutral").lower()
            conf = _safe_float(signals.get("confidence_score"), default=0.0)

            # Résumés (signals prioritaire, sinon latest)
            fp = (
                signals.get("footprint_summary")
                or latest.get("footprint_summary")
                or {}
            ) or {}
            of = (
                signals.get("orderflow_summary")
                or latest.get("orderflow_summary")
                or {}
            ) or {}

            # --- Seuils depuis conf (avec défauts prudents) ---
            sym_spread_max = {"EURUSD": 12.0, "GBPUSD": 18.0, "XAUUSD": 40.0}.get(
                str(sym).upper(), 999.0
            )

            m1_min_ticks = int(
                _dig(
                    base_cfg,
                    ["entry_rules", "scalping", "footprint", "m1_min_ticks"],
                    30,
                )
            )
            m1_min_cov_s = int(
                _dig(
                    base_cfg,
                    ["entry_rules", "scalping", "footprint", "m1_min_coverage_s"],
                    8,
                )
            )
            tickrate_min = _safe_float(
                _dig(
                    base_cfg,
                    ["entry_rules", "scalping", "footprint", "tickrate_min"],
                    1.5,
                ),
                1.5,
            )

            of_delta_min = _safe_float(
                _dig(
                    base_cfg,
                    ["entry_rules", "scalping", "orderflow", "delta_abs_min"],
                    30.0,
                ),
                30.0,
            )

            ttl_ms = int(
                _dig(base_cfg, ["entry_rules", "scalping", "fusion", "ttl_ms"], 1500)
            )
            slippage_pts = _safe_float(
                _dig(
                    base_cfg,
                    ["entry_rules", "scalping", "fusion", "max_slippage_points"],
                    20.0,
                ),
                20.0,
            )

            allow_degraded = bool(
                _dig(
                    base_cfg,
                    ["entry_rules", "scalping", "fusion", "allow_degraded_vote"],
                    True,
                )
            )
            degr_min_of_abs = _safe_float(
                _dig(
                    base_cfg,
                    [
                        "entry_rules",
                        "scalping",
                        "fusion",
                        "degraded_vote_conditions",
                        "min_of_delta_abs",
                    ],
                    150.0,
                ),
                150.0,
            )
            degr_min_of_sc = _safe_float(
                _dig(
                    base_cfg,
                    [
                        "entry_rules",
                        "scalping",
                        "fusion",
                        "degraded_vote_conditions",
                        "min_of_score",
                    ],
                    15.0,
                ),
                15.0,
            )

            # --- Mesures FP/OF ---
            ticks = int(_safe_float(fp.get("tick_count"), 0))
            cov = _safe_float(fp.get("coverage_s"), 0.0)
            tr = _safe_float(fp.get("tick_rate"), 0.0)

            dlt = _safe_float(of.get("delta_total"), 0.0)
            of_sc = _safe_float(latest.get("orderflow_score"), 0.0)
            # Normalisations pour le scoring/meta (évite les NameError)
            fp_dir_raw = _safe_float(fp.get("delta_total"), 0.0)

            # Plus de gates hard → fp_ok = True ; on conserve of_strong pour diag
            fp_ok = True
            of_strong = (abs(dlt) >= degr_min_of_abs) or (of_sc >= degr_min_of_sc)

            # --- JAM PATCH: SOFT VOTE (aucun veto hard) --------------------------
            # 1) plus de garde spread
            # 2) plus de gate footprints (m1_min_ticks / coverage / tickrate)
            # 3) plus de gate delta OF minimal
            # 4) plus de règle 2-sur-3 bloquante

            # Directions élémentaires (inchangé pour détection de sens)
            vote, trig_dir = 0, "NEUTRAL"
            if ("bull" in phase) or ("up" in phase):
                vote += 1
                trig_dir = "BUY"
            elif ("bear" in phase) or ("down" in phase):
                vote -= 1
                trig_dir = "SELL"

            # Direction FP priorisée: aggressor_ratio -> counts -> delta_total
            ar = fp.get("aggressor_ratio", None)
            if isinstance(ar, (int, float)):
                fp_dir = "BUY" if ar > 0.5 else ("SELL" if ar < 0.5 else "NEUTRAL")
            else:
                bt = fp.get("buy_ticks") or fp.get("buy_count")
                st = fp.get("sell_ticks") or fp.get("sell_count")
                if isinstance(bt, (int, float)) and isinstance(st, (int, float)):
                    fp_dir = "BUY" if bt > st else ("SELL" if st > bt else "NEUTRAL")
                else:
                    fp_dir = (
                        "BUY"
                        if _safe_float(fp.get("delta_total"), 0.0) > 0
                        else (
                            "SELL"
                            if _safe_float(fp.get("delta_total"), 0.0) < 0
                            else "NEUTRAL"
                        )
                    )

            dlt = _safe_float(of.get("delta_total"), 0.0)
            of_dir = "BUY" if dlt > 0 else ("SELL" if dlt < 0 else "NEUTRAL")

            # Résolution d'action **sans veto** (priorité OF > FP > phase > coin toss conf)
            if of_dir in {"BUY", "SELL"}:
                action = of_dir
            elif fp_dir in {"BUY", "SELL"}:
                action = fp_dir
            elif trig_dir in {"BUY", "SELL"}:
                action = trig_dir
            else:
                action = "BUY" if conf >= 0.5 else "SELL"

            # Prix d'ancrage pour la fast-lane (inchangé)
            price = None
            try:
                tkfun = getattr(mt5c, "get_symbol_tick", None)
                if callable(tkfun):
                    t = tkfun(sym)
                    ask = (
                        t.get("ask") if isinstance(t, dict) else getattr(t, "ask", None)
                    )
                    bid = (
                        t.get("bid") if isinstance(t, dict) else getattr(t, "bid", None)
                    )
                else:
                    t = getattr(getattr(mt5c, "mt5", None), "symbol_info_tick", None)
                    t = t(sym) if callable(t) else None
                    ask = getattr(t, "ask", None)
                    bid = getattr(t, "bid", None)
                price = (
                    float(ask if action == "BUY" else bid) if (ask and bid) else None
                )
            except Exception:
                price = None
            # ----------------------------------------------------------------------

            # --- Scoring (0..1) ---
            # Force FP : combine Δ(M1) relatif et tickrate relatif
            denom_delta = max(of_delta_min, 1.0)
            fp_strength = 0.0
            try:
                fp_strength = min(
                    1.0,
                    max(
                        0.0,
                        (abs(fp_dir_raw) / denom_delta) * 0.75
                        + (tr / max(tickrate_min, 0.1)) * 0.25,
                    ),
                )
            except Exception:
                fp_strength = 0.0

            # Force OF : Δ relatif (ou seuil dégradé si > plus grand)
            denom_of = max(max(of_delta_min, degr_min_of_abs), 1.0)
            of_strength = min(1.0, max(0.0, abs(dlt) / denom_of))

            # Bonus d’alignement strict (phase, FP, OF convergent)
            align_bonus = (
                0.1
                if (trig_dir == fp_dir == of_dir and trig_dir in {"BUY", "SELL"})
                else 0.0
            )

            score = min(
                1.0, 0.25 * conf + 0.35 * fp_strength + 0.40 * of_strength + align_bonus
            )

            # --- Prix d’ancrage pour la fast-lane ---
            price = None
            try:
                tkfun = getattr(mt5c, "get_symbol_tick", None)
                if callable(tkfun):
                    t = tkfun(sym)
                    ask = (
                        t.get("ask") if isinstance(t, dict) else getattr(t, "ask", None)
                    )
                    bid = (
                        t.get("bid") if isinstance(t, dict) else getattr(t, "bid", None)
                    )
                else:
                    t = getattr(getattr(mt5c, "mt5", None), "symbol_info_tick", None)
                    t = t(sym) if callable(t) else None
                    ask = getattr(t, "ask", None)
                    bid = getattr(t, "bid", None)
                price = (
                    float(ask if action == "BUY" else bid) if (ask and bid) else None
                )
            except Exception:
                price = None

            # --- Sortie normalisée ---
            return {
                "ok": True,
                "action": action,
                "score": float(score),
                "price": price,
                "ttl_ms": int(ttl_ms),
                "rule_name": "fusion_scalping",
                "no_fallback": True,
                "no_tp": True,
                "meta": {
                    "triggers_dir": (
                        "BUY"
                        if ("bull" in phase or "up" in phase)
                        else (
                            "SELL"
                            if ("bear" in phase or "down" in phase)
                            else "NEUTRAL"
                        )
                    ),
                    "fp_dir": fp_dir,
                    "of_dir": of_dir,
                    "fp": {
                        "ticks": ticks,
                        "cov_s": cov,
                        "tickrate": tr,
                        "delta_total": fp_dir_raw,
                    },
                    "of": {"delta_total": dlt, "score": of_sc},
                    "footprint_ok": bool(fp_ok),
                    "degraded_used": bool(allow_degraded and (not fp_ok) and of_strong),
                },
                "slippage_guard_points": float(slippage_pts),
                "ts_created": __import__("pandas").Timestamp.utcnow().value
                // 1_000_000,
            }

        except Exception as e:
            return {"ok": False, "reason": f"fusion_error:{e}"}

    # --- Helper: normaliser les inputs pour FusionManager ---
    def _scale100(x):
        try:
            v = float(x)
            return v * 100.0 if 0.0 <= v <= 1.0 else v
        except Exception:
            return None

    def _mk_fusion_inputs(
        signals: dict, latest: dict, symbol_info, mt5c: MT5Connector, sym: str
    ):
        of_score = _scale100(latest.get("orderflow_score"))
        fp_score = _scale100(latest.get("footprint_score"))

        of_summary = latest.get("orderflow_summary") or {}
        fp_summary = latest.get("footprint_summary") or {}

        # bias OF à partir de delta_total si dispo
        try:
            dlt = of_summary.get("delta_total")
        except Exception:
            dlt = None
        if isinstance(dlt, (int, float)):
            of_bias = "BUY" if dlt > 0 else ("SELL" if dlt < 0 else "NEUTRAL")
        else:
            of_bias = "NEUTRAL"

        orderflow = {
            "score": of_score,
            "status": latest.get("orderflow_status", "SUSPECT"),
            "summary": of_summary,
            "bias": of_bias,
        }

        footprint = {
            "score": fp_score,
            "status": latest.get("footprint_status", "SUSPECT"),
            "summary": fp_summary,
        }

        # Déduire un trigger minimal (optionnel). Ici on reprend la direction OF.
        trig_dir = of_bias if of_bias in {"BUY", "SELL"} else None
        anchor = None
        try:
            tk = mt5c.get_symbol_tick(sym)
            ask = tk.get("ask") if isinstance(tk, dict) else getattr(tk, "ask", None)
            bid = tk.get("bid") if isinstance(tk, dict) else getattr(tk, "bid", None)
            if trig_dir == "BUY" and ask:
                anchor = float(ask)
            if trig_dir == "SELL" and bid:
                anchor = float(bid)
        except Exception:
            pass

        triggers = {
            "direction": trig_dir,  # peut être None → mode dégradé autorisé
            "confidence": float(signals.get("confidence_score", 0.5) or 0.5),
            "anchor_price": anchor,
            "trigger_type": "fusion_pretrigger",
        }

        ctx = {
            "now_ts": __import__("time").time(),
            "spread_points": float(signals.get("current_spread_points", float("nan"))),
            # tu peux brancher ici regime/volatility/session si tu les as
            "regime": str(signals.get("phase", "")) or None,
        }

        # price_step utilisé par _suggest_trailing
        step = getattr(symbol_info, "point", None) if symbol_info else None
        strat_cfg = {
            "price_step": float(step) if isinstance(step, (int, float)) else 0.01
        }
        return orderflow, footprint, triggers, strat_cfg, ctx

    # === Préparation exécution ===
    try:
        if not getattr(mt5_connector, "is_connected", False):
            raise RuntimeError("MT5 a perdu la connexion persistante.")

        base_config = config_manager.get_current_dynamic_config()
        execution_mode = str(base_config.get("mode_execution", "DEMO")).upper()

        # Logging & Fusion policy (durcies)
        LOG_FUSION_ONLY: bool = True
        REQUIRE_FUSION_MGR: bool = True
        REQUIRE_FUSION_MGR: bool = False  # DEBUG: autorise _quick_vote_fusion si Fusion reste en HOLD
        FUSION_ASSETS = {"XAUUSD"}

        def _fusion_applies(asset: str) -> bool:
            return asset.upper() in FUSION_ASSETS

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
                xa = config_manager.load_asset_config("XAUUSD") or {}
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
                f"[CFG@BOOT] burst_size global={g_burst} | strategy={s_burst} | XAUUSD.entry={xa_entry} | XAUUSD.override={xa_override} | XAUUSD.legacy={xa_legacy}"
            )

        active_mt5_account_details = config_manager.get_mt5_account_credentials(
            mode=execution_mode
        )

        # Candles switch
        try:
            _cs = (
                config_manager.get("phase_detection_defaults.candlestick_analysis", {})
                or {}
            )
            CANDLES_ENABLED = bool(_cs.get("enabled", True))
        except Exception:
            CANDLES_ENABLED = True

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

        # Hard-gate: FusionManager requis pour XAUUSD
        if REQUIRE_FUSION_MGR and (_fusion_mgr is None):
            if "XAUUSD" in {a.upper() for a in tradeable_assets}:
                logger.critical(
                    "[FUSION] FusionManager requis pour le scalping XAUUSD — XAUUSD retiré du cycle (aucun fallback)."
                )
                tradeable_assets = [
                    a for a in tradeable_assets if a.upper() != "XAUUSD"
                ]

        print(f"🎯 [PIPELINE] Assets tradables: {tradeable_assets}")
        if not tradeable_assets:
            logger.warning("Aucun actif à trader pour ce cycle. Cycle ignoré.")
            return False

        # Collecte/Analyse
        all_assets_market_data: Dict[str, pd.DataFrame] = {}
        all_assets_trading_signals: Dict[str, Dict[str, Any]] = {}
        fusion_scalping_decisions: List[Dict[str, Any]] = []

        dcfg = base_config.get("data_collection", {}) or {}
        timeframe_str = dcfg.get("default_timeframe", "M1")
        bars_to_fetch = int(dcfg.get("default_bars_count", 500))
        min_required_bars = 50

        for asset in tradeable_assets:
            print(f"📊 [PIPELINE] Analyse de {asset}...")
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

                market_results = market_analyzer.analyze(subset_df, asset)

                # Option: purge patterns si OFF
                if not CANDLES_ENABLED:
                    try:
                        market_results.pop("patterns", None)
                        lat = market_results.get("latest")
                        if isinstance(lat, dict):
                            lat.pop("candles", None)
                    except Exception:
                        pass

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

                # Log 200 bougies
                if CANDLES_ENABLED:
                    try:
                        last_200 = annotated_rates_df.tail(200)
                        avg_vol = (
                            last_200["tick_volume"].mean()
                            if "tick_volume" in last_200
                            else None
                        )
                        avg_range = (
                            ((last_200["high"] - last_200["low"]).mean())
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
                            f"[{asset}] Historique(200) → Bull={bull_candles}, Bear={bear_candles}, VolMoy={avg_vol:.2f} | RangeMoy={avg_range:.5f}"
                        )
                    except Exception as e:
                        logger.warning(f"[{asset}] Résumé 200 bougies impossible: {e}")
                else:
                    logger.debug(f"[{asset}] Skip résumé 200 (candles disabled).")

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

                    if ticks_df is not None and not ticks_df.empty:
                        ticks_df["time"] = pd.to_datetime(
                            ticks_df["time"], utc=True, errors="coerce"
                        )
                        fp_res = footprint_validator(
                            annotated_rates_df,
                            ticks_df,
                            candle_index=use_idx,
                            price_step=(
                                getattr(symbol_info_mt5, "point", None) or None
                            ),
                            imbalance_threshold=float(
                                (
                                    (
                                        base_config.get("phase_detection_defaults", {})
                                        or {}
                                    ).get("footprint_settings", {})
                                    or {}
                                ).get("imbalance_threshold", 0.7)
                            ),
                            fp_conf=base_config,  # ← branche les seuils existants (min_ticks, coverage, tickrate, POC, delta, burst…)
                            asset=asset,  # ← applique les overrides XAUUSD
                        )

                        latest = dict(latest)
                        latest["footprint_score"] = fp_res.get("score", 0)
                        latest["footprint_status"] = fp_res.get("status", "N/A")
                        latest["footprint_summary"] = fp_res.get("summary", {})
                    else:
                        logger.warning(
                            f"[FOOTPRINT][{asset}] Aucun tick reçu (close+live) → skip."
                        )
                        # PATCH footprint: fallback neutre si aucun tick
                        latest = dict(latest)
                        latest.setdefault("footprint_score", 0.0)
                        latest.setdefault("footprint_status", "MISSING_OK")  # ← toléré par Fusion
                        latest.setdefault("footprint_summary", {"tick_count": 0, "coverage_s": 0.0, "tick_rate": 0.0, "delta_total": 0.0})

                except Exception as e:
                    logger.error(
                        f"[FOOTPRINT][{asset}] Erreur analyse ticks: {e}", exc_info=True
                    )

                # === ORDERFLOW v6 ANALYSE (5 dernières bougies) ===
                try:

                    last_candles_df = annotated_rates_df.tail(5).copy()
                    if "time" in last_candles_df.columns:
                        last_candles_df["time"] = pd.to_datetime(
                            last_candles_df["time"], utc=True, errors="coerce"
                        )
                        last_candles_df.set_index("time", inplace=True)

                    # Paramètres V6 pilotés par la conf (phase_observer_config.json)
                    try:
                        _ph = config_manager.get("phase_observer", {}) or {}
                        _of6 = _ph.get("orderflow_v6", {}) or {}
                        _imb = float(_of6.get("imbalance_threshold", 0.20))
                        _cvd = float(_of6.get("cvd_smoothing", 0.0))
                        _bins = int(_of6.get("price_bins", 24))
                    except Exception:
                        _imb, _cvd, _bins = 0.20, 0.0, 24

                    of_res = detect_orderflow_v6(
                        last_candles_df,
                        imbalance_threshold=_imb,
                        cvd_smoothing=_cvd,
                        price_bins=_bins,
                        logger=logger,
                    )

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

                # === Décision Fusion (XAUUSD seulement) ===
                try:
                    if _fusion_applies(asset):
                        if _fusion_mgr and hasattr(_fusion_mgr, "fuse"):
                            of, fp, trig, strat_cfg, ctx = _mk_fusion_inputs(
                                signals, latest, symbol_info_mt5, mt5_connector, asset
                            )
                            out = _fusion_mgr.fuse(
                                orderflow=of,
                                footprint=fp,
                                triggers=trig,
                                strategy_config=strat_cfg,
                                context=ctx,
                            )
                            try:
                                _fc = float(out.get("fused_confidence") or 0.0)
                                # échelle 0..1 attendue par Fusion
                                if 0.0 <= _fc <= 1.0:
                                    pass
                                else:
                                    _fc = _fc / 100.0  # si jamais 0..100

                                _fusion_cfg = ((base_config.get("entry_rules", {}) or {}).get("scalping", {}) or {}).get("fusion", {}) or {}
                                allow_degraded = bool(_fusion_cfg.get("allow_degraded_vote", True))
                                fire_th = float((_fusion_cfg.get("min_score_to_fire", 0.15) or 0.15))

                                # Si Fusion dit HOLD mais la confiance dépasse le seuil “fire_th” et le mode dégradé est autorisé → OK
                                if (not out.get("ok")) and (out.get("signal_type") == "WAIT_CONFIRMATION") and allow_degraded and (_fc >= fire_th):
                                    # choisir une direction cohérente (triggers.direction sinon biais OF)
                                    _dir = (trig.get("direction") if isinstance(trig, dict) else None) or of.get("bias") or out.get("action")
                                    if _dir in {"BUY", "SELL"}:
                                        out["ok"] = True
                                        out["action"] = _dir
                                        out["signal_type"] = "DIRECT"  # promote
                            except Exception:
                                pass

                            try:
                                logger.info(
                                    "[TRACE] FUSION fused=%.3f | action=%s | signal=%s | consensus=%s",
                                    float(out.get("fused_confidence") or 0.0),
                                    out.get("action"),
                                    out.get("signal_type"),
                                    (out.get("consensus") or {}).get("maj"),
                                )

                            except Exception:
                                pass

                            # TTL & slippage depuis la conf
                            _fusion_cfg = (
                                (base_config.get("entry_rules", {}) or {}).get(
                                    "scalping", {}
                                )
                                or {}
                            ).get("fusion", {}) or {}
                            ttl_ms = int(_fusion_cfg.get("ttl_ms", 800))
                            slippage_pts = float(
                                _fusion_cfg.get("max_slippage_points", 10.0)
                            )

                            # Confiance fusion → toujours 0..100 pour cohérence
                            try:
                                _fc = float(out.get("fused_confidence", 0.0) or 0.0)
                                _score100 = _fc * 100.0 if 0.0 <= _fc <= 1.0 else _fc
                            except Exception:
                                _score100 = 0.0

                            fdec = {
                                "ok": bool(out.get("ok")),
                                "action": out.get("action"),
                                "score": float(_score100),
                                "price": (
                                    trig.get("anchor_price")
                                    if isinstance(trig, dict)
                                    else None
                                ),
                                "ttl_ms": ttl_ms,
                                "slippage_guard_points": slippage_pts,
                                "ts_created": __import__("pandas")
                                .Timestamp.utcnow()
                                .value
                                // 1_000_000,
                                "meta": {
                                    "signal_type": out.get("signal_type"),
                                    "consensus": out.get("consensus"),
                                    "trail": out.get("suggested_trailing"),
                                    "quality": out.get("quality"),
                                },
                            }

                        elif REQUIRE_FUSION_MGR:
                            fdec = {"ok": False, "reason": "fusion_manager_missing"}
                        else:
                            fdec = _quick_vote_fusion(
                                signals=signals,
                                latest=latest,
                                base_cfg=base_config,
                                sym=asset,
                                mt5c=mt5_connector,
                            )

                        if (
                            fdec
                            and fdec.get("ok")
                            and fdec.get("action") in {"BUY", "SELL"}
                        ):
                            fusion_scalping_decisions.append(
                                {
                                    "rule_name": "fusion_scalping",
                                    "action": fdec["action"],
                                    "asset": asset,
                                    "price": fdec.get("price"),
                                    "confidence": fdec.get("score", 0.7),
                                    "no_fallback": True,
                                    "entry_style": "MARKET",
                                    "validity_ms": int(fdec.get("ttl_ms", 800)),
                                    "slippage_guard_points": float(
                                        fdec.get("slippage_guard_points", 10.0)
                                    ),
                                    "ts_created": int(fdec.get("ts_created")),
                                    "fusion_meta": fdec.get("meta", {}),
                                }
                            )
                            logger.info(
                                "[FUSION][%s] %s score=%.2f ttl=%dms | price=%s | meta=%s",
                                asset,
                                fdec.get("action", "?"),
                                float(fdec.get("score", 0.0)),
                                int(fdec.get("ttl_ms", 0)),
                                fdec.get("price"),
                                fdec.get("meta", {}),
                            )
                        else:
                            logger.info(
                                "[FUSION][%s] hold: %s",
                                asset,
                                (out if isinstance(out, dict) else {}).get(
                                    "signal_type",
                                    (fdec or {}).get("reason", "WAIT_CONFIRMATION"),
                                ),
                            )

                except Exception as _e:
                    logger.warning(f"[FUSION] erreur: {_e}")

            except Exception as e:
                logger.error(f"Erreur collecte données {asset}: {e}", exc_info=True)
                continue

        if not all_assets_trading_signals:
            logger.warning("Aucun signal valide généré. Fin du cycle.")
            return False

        # === FUSION SUMMARY (affichage) ===
        print("\n" + "=" * 58)
        print("🔎 FUSION SUMMARY (par actif)")

        def _fmt_float(x):
            try:
                v = float(x)
                return f"{v:.2f}"
            except Exception:
                return str(x)

        for asset, sig in all_assets_trading_signals.items():
            try:
                # Fusion uniquement pour XAUUSD
                if asset.upper() != "XAUUSD":
                    print(f"   {asset:<7} → n/a (fusion off)")
                    continue

                # Récupération robuste du 'latest' (clé normalisée)
                _latest = sig.get("__latest__") or {}

                # 1) Snapshot "maintenant" (recompute)
                if _fusion_mgr and hasattr(_fusion_mgr, "fuse"):
                    _syminfo = mt5_connector.get_symbol_info(asset)
                    of, fp, trig, strat_cfg, ctx = _mk_fusion_inputs(
                        sig, _latest, _syminfo, mt5_connector, asset
                    )
                    fdec_syn = _fusion_mgr.fuse(
                        orderflow=of,
                        footprint=fp,
                        triggers=trig,
                        strategy_config=strat_cfg,
                        context=ctx,
                    )
                elif REQUIRE_FUSION_MGR:
                    fdec_syn = {"ok": False, "reason": "fusion_manager_missing"}
                else:
                    fdec_syn = _quick_vote_fusion(
                        signals=sig,
                        latest=_latest,
                        base_cfg=base_config,
                        sym=asset,
                        mt5c=mt5_connector,
                    )

            except Exception as _e:
                fdec_syn = {"ok": False, "reason": f"fusion_error:{_e}"}

            # Construire la ligne "snapshot"
            if fdec_syn and (
                fdec_syn.get("ok") or fdec_syn.get("action") in {"BUY", "SELL"}
            ):
                act_now = fdec_syn.get("action", "—")
                sc_now = (
                    fdec_syn.get("fused_confidence", None)
                    if isinstance(fdec_syn, dict)
                    else None
                )
                if sc_now is None:
                    sc_now = fdec_syn.get("score", None)  # fallback (quick_vote)
                snap_line = f"snapshot: action={act_now:<4} score={_fmt_float(sc_now)}"
            else:
                rz = (fdec_syn or {}).get("signal_type") or (fdec_syn or {}).get(
                    "reason", "WAIT_CONFIRMATION"
                )
                snap_line = f"snapshot: action=—   score=—   hold={rz}"

            # 2) Dernière décision effectivement retenue dans ce cycle (si présente)
            lasts = [
                d
                for d in (fusion_scalping_decisions or [])
                if str(d.get("asset", "")).upper() == asset.upper()
            ]
            if lasts:
                # On prend la meilleure par 'confidence'
                try:
                    best = max(lasts, key=lambda d: float(d.get("confidence", 0.0)))
                except Exception:
                    best = lasts[-1]
                act_used = best.get("action", "—")
                sc_used = best.get("confidence", None)
                used_line = (
                    f"used:     action={act_used:<4} score={_fmt_float(sc_used)}"
                )
                print(f"   {asset:<7} → {used_line} | {snap_line}")
            else:
                print(f"   {asset:<7} → {snap_line}")

        # === GATECHECK XAUUSD (diagnostic) ===
        try:

            sig = (all_assets_trading_signals or {}).get("XAUUSD", {})
            if sig:
                # helpers
                def _dig(d, path, default=None):
                    cur = d or {}
                    for k in path:
                        if not isinstance(cur, dict):
                            return default
                        cur = cur.get(k)
                    return cur if cur is not None else default

                latest = sig.get("__latest__") or {}
                fp = sig.get("footprint_summary") or {}
                of = sig.get("orderflow_summary") or {}

                # métriques mesurées
                ticks = fp.get("tick_count", None)
                cov = fp.get("coverage_s", None)
                trate = fp.get("tick_rate", None)
                dlt = of.get("delta_total", None)
                of_sc = latest.get("orderflow_score", None)
                spread = sig.get("current_spread_points", float("nan"))

                # seuils depuis conf (priorité: asset overrides -> global)
                xcfg = config_manager.load_asset_config("XAUUSD") or {}
                base = base_config

                # Footprint M1
                m1_min_ticks = int(
                    _dig(
                        xcfg,
                        ["overrides", "scalping", "footprint", "m1_min_ticks"],
                        _dig(
                            base,
                            ["entry_rules", "scalping", "footprint", "m1_min_ticks"],
                            30,
                        ),
                    )
                )
                m1_min_cov_s = float(
                    _dig(
                        xcfg,
                        ["overrides", "scalping", "footprint", "m1_min_coverage_s"],
                        _dig(
                            base,
                            [
                                "entry_rules",
                                "scalping",
                                "footprint",
                                "m1_min_coverage_s",
                            ],
                            8,
                        ),
                    )
                )
                tickrate_min = float(
                    _dig(
                        xcfg,
                        ["overrides", "scalping", "footprint", "tickrate_min"],
                        _dig(
                            base,
                            ["entry_rules", "scalping", "footprint", "tickrate_min"],
                            1.5,
                        ),
                    )
                )
                cov_burst_min = float(
                    _dig(
                        xcfg,
                        ["overrides", "scalping", "footprint", "coverage_s_min_burst"],
                        _dig(
                            base,
                            [
                                "entry_rules",
                                "scalping",
                                "footprint",
                                "coverage_s_min_burst",
                            ],
                            5.0,
                        ),
                    )
                )

                # Orderflow
                of_delta_min = float(
                    _dig(
                        xcfg,
                        ["overrides", "scalping", "orderflow", "delta_abs_min"],
                        _dig(
                            base,
                            ["entry_rules", "scalping", "orderflow", "delta_abs_min"],
                            30.0,
                        ),
                    )
                )
                degr_min_of_sc = float(
                    _dig(
                        base,
                        [
                            "entry_rules",
                            "scalping",
                            "fusion",
                            "degraded_vote_conditions",
                            "min_of_score",
                        ],
                        15.0,
                    )
                )

                # Spread (si tu as un max en conf, sinon on ignore)
                spread_max = float(
                    _dig(
                        base,
                        ["entry_rules", "scalping", "fusion", "max_spread_points"],
                        float("inf"),
                    )
                )

                # News blackout
                news_blackout = bool(
                    _dig(base, ["guardrails", "sessions", "news_blackout"], False)
                )

                # Évaluation des portes (sans bloquer l’exécution ; juste diag)
                blocks = []

                # 1) news blackout
                if news_blackout:
                    blocks.append("news_blackout=True")

                # 2) spread
                if (
                    math.isfinite(float(spread))
                    and math.isfinite(float(spread_max))
                    and spread_max != float("inf")
                ):
                    if float(spread) > float(spread_max):
                        blocks.append(f"spread {spread:.1f}>{spread_max:.1f}")

                # 3) footprint M1
                try:
                    fp_ticks_ok = (ticks is not None) and (
                        int(ticks) >= int(m1_min_ticks)
                    )
                    fp_cov_ok = (cov is not None) and (
                        float(cov) >= float(m1_min_cov_s)
                    )
                    fp_burst_ok = (
                        (trate is not None)
                        and (float(trate) >= float(tickrate_min))
                        and (float(cov or 0) >= float(cov_burst_min))
                    )
                    fp_gate_ok = (fp_ticks_ok and fp_cov_ok) or fp_burst_ok
                    if not fp_gate_ok:
                        blocks.append(
                            f"footprint_m1: ticks={ticks}/{m1_min_ticks} cov={cov}/{m1_min_cov_s}s rate={trate}/{tickrate_min}/s"
                        )
                except Exception:
                    pass

                # 4) orderflow
                try:
                    of_ok = (
                        dlt is not None and abs(float(dlt)) >= float(of_delta_min)
                    ) or (of_sc is not None and float(of_sc) >= float(degr_min_of_sc))
                    if not of_ok:
                        blocks.append(
                            f"orderflow: |Δ|={abs(dlt) if dlt is not None else 'NA'} < {of_delta_min} & of_score={of_sc} < {degr_min_of_sc}"
                        )
                except Exception:
                    pass

                # 5) FusionManager diagnostic (pas de veto : on log l’état HOLD/SIGNAL)
                try:
                    if _fusion_mgr and hasattr(_fusion_mgr, "fuse"):
                        _syminfo = mt5_connector.get_symbol_info("XAUUSD")
                        of_i, fp_i, trig_i, strat_cfg_i, ctx_i = _mk_fusion_inputs(
                            sig, latest, _syminfo, mt5_connector, "XAUUSD"
                        )
                        fdec_diag = _fusion_mgr.fuse(
                            orderflow=of_i,
                            footprint=fp_i,
                            triggers=trig_i,
                            strategy_config=strat_cfg_i,
                            context=ctx_i,
                        )
                        if not fdec_diag.get("ok"):
                            blocks.append(
                                f"fusion={fdec_diag.get('signal_type','WAIT_CONFIRMATION')}"
                            )
                except Exception:
                    pass

                # 6) Burst guard global (panier existant) déjà checké plus bas, mais on log ici aussi
                try:
                    burst_cfg = (
                        base.get("entry_rules", {})
                        .get("scalping", {})
                        .get("burst_scalping", {})
                        or {}
                    )
                    g = burst_cfg.get("burst_guardrails", {}) or {}
                    if bool(g.get("enforce_burst_closure", True)) and bool(
                        g.get("single_burst_global", True)
                    ):
                        # s’il existe un panier ouvert, c’est bloquant
                        if "_open_burst_ids" in locals():
                            if _open_burst_ids():  # défini plus haut dans la fast-lane
                                blocks.append("burst_guard: open_basket_present")
                except Exception:
                    pass

                if blocks:
                    print("[BLOCKERS][XAUUSD] " + " | ".join(blocks))
                else:
                    print("[BLOCKERS][XAUUSD] none")
        except Exception as _e:
            logger.debug(f"[GATECHECK][XAUUSD] skip: {_e}")

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
        print("✅ [PIPELINE] Contexte global construit avec succès !")
        print(f"2️⃣ CONTEXT KEYS: {list(global_context.keys())}")

        # === FAST-LANE (exécuter immédiatement la meilleure décision Fusion valide) ===
        try:
            if fusion_scalping_decisions:
                import re, time

                _positions_cache = None

                def _open_burst_ids():
                    nonlocal _positions_cache
                    try:
                        if _positions_cache is None:
                            _positions_cache = mt5_connector.get_positions() or []
                        ids = set()
                        for p in _positions_cache:
                            c = (
                                p.get("comment")
                                if isinstance(p, dict)
                                else getattr(p, "comment", "")
                            ) or ""
                            m = re.search(
                                r"bs_([a-f0-9]{8})", str(c)
                            )
                            if m:
                                ids.add(m.group(1))
                        return ids
                    except Exception:
                        return set()

                burst_cfg = (
                    base_config.get("entry_rules", {})
                    .get("scalping", {})
                    .get("burst_scalping", {})
                    or {}
                )
                guard_cfg = burst_cfg.get("burst_guardrails", {}) or {}
                enforce_closure = bool(guard_cfg.get("enforce_burst_closure", True))
                single_burst_global = bool(guard_cfg.get("single_burst_global", True))

                if enforce_closure and single_burst_global and _open_burst_ids():
                    logger.info(
                        "⛔ [FUSION][FAST-LANE] Panier actif détecté → fast-lane annulée."
                    )
                else:
                    now_ms = int(pd.Timestamp.utcnow().value // 1_000_000)

                    def _score_fd(fd):
                        age = max(0, now_ms - int(fd.get("ts_created", now_ms)))
                        ttl = int(fd.get("validity_ms", 800))
                        alive = 1 if age < ttl else 0
                        return (alive, float(fd.get("confidence", 0.0)))

                    fusion_scalping_decisions.sort(key=_score_fd, reverse=True)
                    best = fusion_scalping_decisions[0]
                    age = max(0, now_ms - int(best.get("ts_created", now_ms)))
                    if age < int(best.get("validity_ms", 800)):
                        sym = str(best.get("asset", "")).upper()
                        side = str(best.get("action", "")).upper()
                        slipp_pts = float(
                            best.get("slippage_guard_points", 10.0) or 0.0
                        )
                        ok_price = True
                        try:
                            ref = float(best.get("price") or 0.0)
                            if ref > 0.0 and hasattr(mt5_connector, "get_symbol_tick"):
                                t = mt5_connector.get_symbol_tick(sym)
                                ask = (
                                    t.get("ask")
                                    if isinstance(t, dict)
                                    else getattr(t, "ask", None)
                                )
                                bid = (
                                    t.get("bid")
                                    if isinstance(t, dict)
                                    else getattr(t, "bid", None)
                                )
                                now_price = float(ask if side == "BUY" else bid)
                                pts = abs(now_price - ref) / getattr(
                                    mt5_connector.get_symbol_info(sym), "point", 1.0
                                )
                                if pts > slipp_pts:
                                    ok_price = False
                                    logger.info(
                                        f"[FUSION][FAST-LANE] slippage guard: ref={ref} now={now_price} pts>{slipp_pts} → abort."
                                    )
                        except Exception:
                            pass

                        if ok_price and sym and side in {"BUY", "SELL"}:

                            def _resolve(sym_):
                                def _dig(d, path):
                                    cur = d or {}
                                    for k in path:
                                        if not isinstance(cur, dict):
                                            return None
                                        cur = cur.get(k)
                                    return cur

                                prefer_asset = bool(
                                    (
                                        (
                                            (
                                                base_config.get("entry_rules", {}) or {}
                                            ).get("scalping", {})
                                            or {}
                                        ).get("burst_scalping", {})
                                        or {}
                                    ).get("prefer_asset_overrides", False)
                                )
                                try:
                                    aconf = config_manager.load_asset_config(sym_) or {}
                                except Exception:
                                    aconf = {}
                                if prefer_asset:
                                    reads = [
                                        # 1. Asset override (XAUUSD.json)
                                        _dig(
                                            aconf,
                                            [
                                                "entry_rules",
                                                "scalping",
                                                "burst_scalping",
                                                "burst_size",
                                            ],
                                        ),
                                        _dig(
                                            aconf,
                                            [
                                                "overrides",
                                                "entry_rules",
                                                "scalping",
                                                "burst_scalping",
                                                "burst_size",
                                            ],
                                        ),
                                        _dig(
                                            aconf,
                                            [
                                                "overrides",
                                                "scalping",
                                                "entry_rules",
                                                "scalping",
                                                "burst_scalping",
                                                "burst_size",
                                            ],
                                        ),
                                        _dig(
                                            base_config,
                                            [
                                                "entry_rules",
                                                "scalping",
                                                "burst_scalping",
                                                "burst_size",
                                            ],
                                        ),
                                        _dig(
                                            base_config,
                                            [
                                                "trade_executor_settings",
                                                "entry_rules",
                                                "scalping",
                                                "burst_scalping",
                                                "burst_size",
                                            ],
                                        ),
                                    ]
                                else:
                                    # Lire depuis la stratégie scalping (config_trade_scalping.json)
                                    scalping_strat_cfg = strategy_manager.get_strategy_config("scalping") or {}
                                    reads = [
                                        # 1. Stratégie scalping (config_trade_scalping.json) - SOURCE PRINCIPALE
                                        _dig(
                                            scalping_strat_cfg,
                                            [
                                                "entry_rules",
                                                "scalping",
                                                "burst_scalping",
                                                "burst_size",
                                            ],
                                        ),
                                        # 2. Base config (prod_config.json) - DEPRECATED, n'a plus entry_rules
                                        _dig(
                                            base_config,
                                            [
                                                "entry_rules",
                                                "scalping",
                                                "burst_scalping",
                                                "burst_size",
                                            ],
                                        ),
                                        _dig(
                                            aconf,
                                            [
                                                "entry_rules",
                                                "scalping",
                                                "burst_scalping",
                                                "burst_size",
                                            ],
                                        ),
                                        _dig(
                                            aconf,
                                            [
                                                "overrides",
                                                "entry_rules",
                                                "scalping",
                                                "burst_scalping",
                                                "burst_size",
                                            ],
                                        ),
                                        _dig(
                                            aconf,
                                            [
                                                "overrides",
                                                "scalping",
                                                "entry_rules",
                                                "scalping",
                                                "burst_scalping",
                                                "burst_size",
                                            ],
                                        ),
                                        _dig(
                                            base_config,
                                            [
                                                "trade_executor_settings",
                                                "entry_rules",
                                                "scalping",
                                                "burst_scalping",
                                                "burst_size",
                                            ],
                                        ),
                                    ]
                                for v in reads:
                                    if isinstance(v, (int, float)) and v > 0:
                                        return int(v)
                                return 5

                            resolved_burst = max(int(_resolve(sym)), 1)

                            td = {
                                "rule_name": "burst_scalping",
                                "action": side,
                                "side": side,
                                "asset": sym,
                                "symbol": sym,
                                "order_type": "MARKET",
                                "burst_size": resolved_burst,
                                "sizing_scope": "BASKET",
                                "no_fallback": True,
                                "order": {
                                    "action": side,
                                    "side": side,
                                    "type": "MARKET",
                                    "symbol": sym,
                                },
                                "trade": {"action": side, "side": side},
                            }
                            # --- Harmoniser fat-finger policy avec la voie normale ---
                            safety = td.setdefault("safety", {})
                            ff = safety.setdefault("fat_finger", {})
                            ff.setdefault("policy", "FLOOR")  # clamp plutôt qu'abandon
                            # (le cap max volume/order reste lu de la conf actif)

                            sltp_cfg = (
                                (
                                    (global_context.get("asset_configs", {}) or {}).get(
                                        sym, {}
                                    )
                                    or {}
                                ).get("entry_rules", {})
                                or {}
                            ).get("scalping", {}) or {}
                            sltp_cfg = (sltp_cfg.get("burst_scalping", {}) or {}).get(
                                "sltp", {}
                            ) or (
                                (
                                    base_config.get("entry_rules", {})
                                    .get("scalping", {})
                                    .get("burst_scalping", {})
                                    .get("sltp", {})
                                )
                                or {}
                            )
                            if sltp_cfg:
                                td["sltp"] = sltp_cfg

                            logger.info(
                                f"[FUSION][FAST-LANE] {side} {sym} burst={resolved_burst} (market, trailing-only)"
                            )

                            # FIX: Fusionner la config de stratégie scalping avec base_config
                            # pour que sltp.py trouve les paramètres SL/TP (400 pips)
                            try:
                                scalping_strategy_config = strategy_manager.get_strategy_config("scalping") or {}
                                merged_config = dict(base_config)  # Copie
                                # Fusionner entry_rules de la stratégie scalping
                                if "entry_rules" in scalping_strategy_config:
                                    merged_config.setdefault("entry_rules", {}).update(
                                        scalping_strategy_config["entry_rules"]
                                    )
                            except Exception as e:
                                logger.warning(f"[FUSION][FAST-LANE] Fusion config échouée: {e}")
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
                            # ✅ FIX: Ne pas return pour permettre la maintenance SLTP
                            # if (res or {}).get("status") not in {"failed", ""}:
                            #     return True
        except Exception as e:
            logger.warning(f"[FUSION][FAST-LANE] erreur: {e}")

        # === Gestion fermetures SL/TP des paniers (sécurité) ===
        try:
            closure_cfg = (
                base_config.get("entry_rules", {})
                .get("scalping", {})
                .get("burst_scalping", {})
                .get("closure_rules", {})
                or {}
            )
            trade_executor.monitor_burst_baskets(
                config=base_config,
                max_loss_pips=float(closure_cfg.get("max_loss_pips", 15.0)),
            )
        except Exception as e:
            logger.warning(f"[BURST EXIT] Contrôle fermeture panier (SLTP): {e}")

        # === Pipeline institutionnel (peut produire Liquidity, etc.) ===
        print("🤖 [PIPELINE] Appel du decision_pipeline...")
        decision_package = (
            decision_pipeline.institutional_decision_pipeline(global_context) or {}
        )

        # === Maintenance périodique SLTP dynamique ===
        # ⚠️ DÉSACTIVÉ : Remplacé par le thread dédié trailing_stop_monitor_thread (toutes les 2s en temps réel)
        # Ce code tournait dans le cycle principal (toutes les 23-60s) et ratait les activations.
        # Le nouveau thread surveille en continu et active le trailing dès que PnL >= 28 pips.
        # Voir section "# 8. Démarrage du Thread de Surveillance Trailing Stop" dans main()
        pass

        # === Intégrer les décisions Fusion dans le package ===
        try:
            if fusion_scalping_decisions:
                decision_package.setdefault("scalping_decisions", [])
                decision_package["scalping_decisions"].extend(fusion_scalping_decisions)
                decision_package.setdefault(
                    "final_decision", decision_package.get("final_decision") or {}
                )
                if not decision_package["final_decision"]:
                    now_ms = int(pd.Timestamp.utcnow().value // 1_000_000)

                    def _score_fd(fd):
                        age = max(0, now_ms - int(fd.get("ts_created", now_ms)))
                        ttl = int(fd.get("validity_ms", 800))
                        alive = 1 if age < ttl else 0
                        return (alive, float(fd.get("confidence", 0.0)))

                    best = sorted(
                        fusion_scalping_decisions, key=_score_fd, reverse=True
                    )[0]
                    decision_package["final_decision"] = best
                logger.info(
                    f"[MERGE] {len(fusion_scalping_decisions)} décision(s) Fusion intégrée(s)."
                )
        except Exception as e:
            logger.warning(f"[MERGE] Échec intégration décisions Fusion: {e}")

        # === Logs décisions ===
        scalping_decisions = decision_package.get("scalping_decisions", []) or []
        liquidity_decisions = decision_package.get("liquidity_decisions", []) or []
        final_decisions = scalping_decisions + liquidity_decisions
        final = decision_package.get("final_decision", {}) or {}
        ctx_out = decision_package.get("context", {}) or {}

        if final_decisions:
            print("📦 [PIPELINE] Décisions multiples détectées:")
            for d in final_decisions:
                print(
                    f"   → {d.get('action')} {d.get('asset')} | vol={d.get('volume', 0)}"
                )
        else:
            print("📦 [PIPELINE] Aucune décision multiple détectée.")

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

        # === Filtre scalping: FUSION-ONLY + XAUUSD ===
        scalping_decisions = [
            d
            for d in (decision_package.get("scalping_decisions") or [])
            if str(d.get("rule_name", "")).lower() == "fusion_scalping"
            and str(d.get("asset", "")).upper() == "XAUUSD"
        ]

        # === WHY_NO_TRADE si rien à exécuter ===
        if not scalping_decisions and not liquidity_decisions:
            try:
                for asset, sig in all_assets_trading_signals.items():
                    if asset.upper() != "XAUUSD":
                        logger.info(f"[WHY_NO_TRADE][{asset}] fusion_off")
                        continue
                    if _fusion_mgr and hasattr(_fusion_mgr, "fuse"):
                        _latest = sig.get("__latest__") or {}

                        _syminfo = mt5_connector.get_symbol_info(asset)
                        of, fp, trig, strat_cfg, ctx = _mk_fusion_inputs(
                            sig, _latest, _syminfo, mt5_connector, asset
                        )
                        fdec_diag = _fusion_mgr.fuse(
                            orderflow=of,
                            footprint=fp,
                            triggers=trig,
                            strategy_config=strat_cfg,
                            context=ctx,
                        )
                    else:
                        fdec_diag = {"ok": False, "reason": "fusion_manager_missing"}

                    if fdec_diag and fdec_diag.get("ok"):
                        logger.info(f"[WHY_NO_TRADE][{asset}] fusion_ok")
                    else:
                        logger.info(
                            f"[WHY_NO_TRADE][{asset}] hold={(fdec_diag or {}).get('signal_type','WAIT_CONFIRMATION')}"
                        )

            except Exception as _e:
                logger.debug(f"[WHY_NO_TRADE] diagnostic skip: {_e}")

            print("📦 [PIPELINE] Aucune décision détectée.")
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
            print("📦 [PIPELINE] Décisions Scalping détectées:")
            for d in scalping_decisions:
                print(
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
                                    base_config.get("entry_rules", {})
                                    .get("scalping", {})
                                    .get("burst_scalping", {})
                                    .get("closure_rules", {})
                                    or {}
                                ).get("max_loss_pips", 15.0)
                            )
                        )
                        trade_executor.monitor_burst_baskets(
                            config=base_config, max_loss_pips=max_loss
                        )
                    except Exception as e:
                        logger.warning(f"[BURST EXIT] Post-exec (SLTP): {e}")

                except Exception as e:
                    logger.error(
                        f"[BURST] Erreur bloc scalping/SLTP: {e}", exc_info=True
                    )

        # === Exécution Liquidity ===
        if liquidity_decisions:
            print("📦 [PIPELINE] Décisions Liquidity détectées:")
            for d in liquidity_decisions:
                print(
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

        # === EXIT Liquidity forcés ===
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
        except Exception as e:  # <-- AJOUT
            logger.error(f"[PIPELINE] Erreur exit Liquidity: {e}", exc_info=True)

    except Exception as e:  # try GLOBAL (inchangé)
        logger.error(f"Erreur pipeline: {e}", exc_info=True)
        trade_executed_successfully = False
    finally:
        try:
            get_tracker_from_context(global_context).emit_summary(logger)
        except Exception:
            pass
        return trade_executed_successfully


def trailing_stop_monitor_thread(
    trade_executor,
    mt5_connector,
    stop_event: threading.Event,
    update_interval_sec: float = 2.0,
    logger=None
):
    """
    Thread dédié à la surveillance en temps réel des trailing stops.

    S'exécute toutes les `update_interval_sec` secondes (défaut: 2s) pour :
    - Récupérer les baskets actifs depuis MT5
    - Vérifier leur PnL
    - Activer le trailing stop si PnL >= 28 pips
    - Mettre à jour les SL dynamiquement

    Args:
        trade_executor: Instance de TradeExecutor avec la fonction update_basket_sltp_dynamically
        mt5_connector: Connexion MT5 pour récupérer les positions
        stop_event: Event pour arrêter proprement le thread
        update_interval_sec: Intervalle de surveillance (défaut 2s)
        logger: Logger pour les messages
    """
    if logger is None:
        logger = logging.getLogger(__name__)

    # Logs forcés au démarrage (print + logger)
    print(f"🚀 [TRAILING_MONITOR] Thread démarré ! interval={update_interval_sec}s", flush=True)
    logger.info(f"🚀 [TRAILING_MONITOR] Thread de surveillance démarré (interval={update_interval_sec}s)")

    # Pattern pour extraire basket_id du commentaire MT5
    BASKET_PATTERN = re.compile(r"bs_([a-f0-9]{8})")

    # 📊 HISTORIQUE PnL: Dictionnaire pour tracker l'évolution du PnL de chaque basket
    # Format: { basket_id: { "history": [pnl1, pnl2, ...], "max": float, "min": float } }
    pnl_history = {}

    print(f"🔍 [TRAILING_MONITOR] Entrée dans la boucle while...", flush=True)

    while not stop_event.is_set():
        print(f"🔄 [TRAILING_MONITOR] Début d'itération...", flush=True)
        try:
            # Récupérer toutes les positions ouvertes
            try:
                positions = mt5_connector.get_open_positions()
                print(f"📊 [TRAILING_MONITOR] Positions récupérées: {len(positions) if positions else 0}", flush=True)
            except Exception as e:
                print(f"❌ [TRAILING_MONITOR] Erreur get_open_positions: {e}", flush=True)
                logger.debug(f"[TRAILING_MONITOR] Erreur get_open_positions: {e}")
                positions = []

            if not positions:
                # Pas de positions ouvertes, attendre
                stop_event.wait(update_interval_sec)
                continue

            # Extraire les basket_ids uniques des commentaires
            basket_ids = set()
            for pos in positions:
                try:
                    comment = pos.get("comment", "") if isinstance(pos, dict) else getattr(pos, "comment", "")
                    match = BASKET_PATTERN.search(str(comment))
                    if match:
                        basket_ids.add(match.group(1))
                except Exception:
                    continue

            print(f"🎯 [TRAILING_MONITOR] Baskets détectés: {basket_ids}", flush=True)

            if not basket_ids:
                # Aucun basket trouvé
                print(f"⏸️ [TRAILING_MONITOR] Aucun basket, attente {update_interval_sec}s...", flush=True)
                stop_event.wait(update_interval_sec)
                continue

            # === 🧹 NETTOYAGE: Supprimer l'historique des baskets fermés ===
            closed_baskets = set(pnl_history.keys()) - basket_ids
            if closed_baskets:
                for closed_id in closed_baskets:
                    print(f"🔚 [TRAILING_MONITOR] Basket {closed_id} fermé → Suppression historique", flush=True)
                    del pnl_history[closed_id]

            # Mettre à jour chaque basket
            for basket_id in basket_ids:
                if stop_event.is_set():
                    break

                try:
                    # === CALCUL PNL DU BASKET AVANT UPDATE (pour suivi évolution) ===
                    basket_positions = [p for p in positions if basket_id in str(p.get("comment", "") if isinstance(p, dict) else getattr(p, "comment", ""))]
                    total_profit_usd = 0.0
                    total_pnl_pips = 0.0

                    for p in basket_positions:
                        profit = float(p.get("profit", 0.0) if isinstance(p, dict) else getattr(p, "profit", 0.0))
                        volume = float(p.get("volume", 0.0) if isinstance(p, dict) else getattr(p, "volume", 0.0))
                        total_profit_usd += profit

                        # Conversion pips (XAUUSD: 1 pip = 10$ par lot)
                        pip_value = 10.0 * volume
                        if pip_value > 0:
                            total_pnl_pips += profit / pip_value

                    # === 📊 HISTORIQUE PnL: Tracker l'évolution ===
                    if basket_id not in pnl_history:
                        # Nouveau basket: initialiser l'historique
                        pnl_history[basket_id] = {
                            "history": [total_pnl_pips],
                            "max": total_pnl_pips,
                            "min": total_pnl_pips
                        }
                        variation_str = "🆕 NEW"
                        trend_emoji = "➡️"
                    else:
                        # Basket existant: mettre à jour l'historique
                        hist = pnl_history[basket_id]["history"]
                        previous_pnl = hist[-1] if hist else total_pnl_pips

                        # Ajouter le nouveau PnL (garder les 10 dernières valeurs)
                        hist.append(total_pnl_pips)
                        if len(hist) > 10:
                            hist.pop(0)

                        # Mettre à jour min/max
                        pnl_history[basket_id]["max"] = max(pnl_history[basket_id]["max"], total_pnl_pips)
                        pnl_history[basket_id]["min"] = min(pnl_history[basket_id]["min"], total_pnl_pips)

                        # Calculer la variation depuis le dernier check (2s)
                        variation = total_pnl_pips - previous_pnl

                        # Déterminer la tendance (emoji)
                        if variation > 0.1:
                            trend_emoji = "📈"  # En hausse
                            variation_str = f"+{variation:.2f}p"
                        elif variation < -0.1:
                            trend_emoji = "📉"  # En baisse
                            variation_str = f"{variation:.2f}p"
                        else:
                            trend_emoji = "➡️"  # Stable
                            variation_str = "~0.00p"

                    # Récupérer min/max pour affichage
                    pnl_max = pnl_history[basket_id]["max"]
                    pnl_min = pnl_history[basket_id]["min"]

                    # === 📊 AFFICHAGE ENRICHI AVEC HISTORIQUE ===
                    print(f"", flush=True)  # Ligne vide pour la lisibilité
                    print(f"{'='*100}", flush=True)
                    print(f"📊 [TRAILING_MONITOR] Basket {basket_id} | {len(basket_positions)} positions", flush=True)
                    print(f"   💰 PnL actuel: {total_pnl_pips:+.2f} pips (${total_profit_usd:+.2f}) {trend_emoji}", flush=True)
                    print(f"   📈 Variation 2s: {variation_str}", flush=True)
                    print(f"   📊 Range session: [{pnl_min:.2f}p → {pnl_max:.2f}p] (amplitude: {pnl_max - pnl_min:.2f}p)", flush=True)
                    print(f"   🎯 Seuil activation trailing: 28.00 pips", flush=True)

                    # Afficher l'historique des 5 dernières valeurs
                    if len(pnl_history[basket_id]["history"]) >= 2:
                        recent_history = pnl_history[basket_id]["history"][-5:]
                        history_str = " → ".join([f"{pnl:.1f}p" for pnl in recent_history])
                        print(f"   📜 Historique 10s: {history_str}", flush=True)

                    print(f"{'='*100}", flush=True)
                    print(f"", flush=True)

                    logger.info(f"📊 [TRAILING_MONITOR] Basket {basket_id}: {len(basket_positions)} pos | PnL={total_pnl_pips:.2f} pips (${total_profit_usd:.2f}) | Variation={variation_str} {trend_emoji}")

                    # Appeler la fonction de mise à jour du trailing
                    # force_refresh=True pour éviter le skip "too_soon"
                    result = trade_executor.update_basket_sltp_dynamically(
                        basket_id=basket_id,
                        reason="realtime_monitor",
                        force_refresh=True,  # ✅ FIX: Bypass "too_soon" check
                    )

                    # Logger TOUS les résultats (pas seulement success)
                    status = result.get("status", "unknown")
                    reason = result.get("reason", "N/A")
                    pnl = result.get("pnl_pips", total_pnl_pips)

                    if status == "success":
                        print(f"✅ [TRAILING_MONITOR] Basket {basket_id}: trailing mis à jour | PnL={pnl:.1f}p", flush=True)
                        logger.info(f"✅ [TRAILING_MONITOR] Basket {basket_id}: trailing mis à jour (PnL={pnl:.1f}p)")
                    elif status == "skipped":
                        print(f"⏭️ [TRAILING_MONITOR] Basket {basket_id}: SKIPPED | reason={reason} | PnL={pnl:.1f}p", flush=True)
                        logger.debug(f"⏭️ [TRAILING_MONITOR] Basket {basket_id}: skipped (reason={reason})")
                    elif status == "error":
                        print(f"❌ [TRAILING_MONITOR] Basket {basket_id}: ERROR | reason={reason} | PnL={pnl:.1f}p", flush=True)
                        logger.warning(f"❌ [TRAILING_MONITOR] Basket {basket_id}: error (reason={reason})")
                    else:
                        print(f"❓ [TRAILING_MONITOR] Basket {basket_id}: UNKNOWN | status={status} | reason={reason} | PnL={pnl:.1f}p", flush=True)
                        logger.debug(f"❓ [TRAILING_MONITOR] Basket {basket_id}: unknown status={status} reason={reason}")

                except Exception as e:
                    logger.debug(f"[TRAILING_MONITOR] Erreur update basket {basket_id}: {e}")

        except Exception as e:
            logger.error(f"[TRAILING_MONITOR] Erreur dans la boucle: {e}", exc_info=True)

        # Attendre avant le prochain cycle
        stop_event.wait(update_interval_sec)

    logger.info("🛑 [TRAILING_MONITOR] Thread de surveillance arrêté")


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

    # 8. Démarrage du Thread de Surveillance Trailing Stop
    print("=" * 80, flush=True)
    print("🚀 DÉMARRAGE DU THREAD DE SURVEILLANCE TRAILING STOP", flush=True)
    print("=" * 80, flush=True)

    try:
        trailing_stop_event = threading.Event()
        trailing_monitor_interval = config_manager.get(
            "entry_rules.scalping.burst_scalping.trailing.step.update_interval_sec", 2.0
        )

        print(f"📋 Configuration: interval={trailing_monitor_interval}s", flush=True)
        print(f"📋 trade_executor: {trade_executor}", flush=True)
        print(f"📋 mt5_connector: {mt5_connector}", flush=True)

        trailing_thread = threading.Thread(
            target=trailing_stop_monitor_thread,
            args=(trade_executor, mt5_connector, trailing_stop_event, trailing_monitor_interval, logger),
            daemon=True,
            name="TrailingStopMonitor"
        )

        print(f"📋 Thread créé: {trailing_thread}", flush=True)
        trailing_thread.start()
        print(f"✅ Thread.start() appelé", flush=True)

        logger.info(f"✅ Thread de surveillance trailing stop démarré (interval={trailing_monitor_interval}s)")
        print(f"✅ Thread de surveillance trailing stop démarré (interval={trailing_monitor_interval}s)", flush=True)
        print("=" * 80, flush=True)

    except Exception as e:
        print(f"❌ ERREUR CRITIQUE: Impossible de démarrer le thread trailing: {e}", flush=True)
        import traceback
        traceback.print_exc()
        logger.error(f"ERREUR CRITIQUE: Thread trailing non démarré: {e}", exc_info=True)

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
        # Arrêt propre du thread de surveillance trailing stop
        try:
            logger.info("🛑 Arrêt du thread de surveillance trailing stop...")
            trailing_stop_event.set()
            trailing_thread.join(timeout=5.0)
            if trailing_thread.is_alive():
                logger.warning("⚠️ Thread trailing stop n'a pas terminé dans les 5s")
            else:
                logger.info("✅ Thread trailing stop arrêté proprement")
        except Exception as e:
            logger.error(f"Erreur arrêt thread trailing: {e}")

        if "ai_decision" in locals() and ai_decision:
            logger.info("Sauvegarde historique IA avant arrêt...")
            ai_decision._save_suggestion_history()

        if "mt5_connector" in locals() and mt5_connector.is_connected():
            mt5_connector.disconnect()

        logger.info("SNIPER_X Bot est arrêté.")
        sys.exit(0)
