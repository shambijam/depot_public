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
# ❌ SUPPRIMÉ (25 DEC 2025): FusionManager, VWAP - Architecture minimaliste OrderFlow seul
# from phase_observer.fusion_manager import FusionManager
# from phase_observer.vwap import create_vwap_analyzer
# ✅ AJOUTÉ (25 DEC 2025): Timing Gatekeeper pour filtrage binaire PASS/VETO
from phase_observer.timing_analyzer import evaluate_trading_conditions
# ✅ AJOUTÉ (03 JAN 2026): SimpleAdvancedScorer pour scoring composite évolutif
from strategy.advanced_scoring import SimpleAdvancedScorer


load_dotenv()

try:
    # === [ORDERFLOW V6 SUPPRIMÉ - Session 28 Nov 2025] ===
    # detect_orderflow_v6 supprimé → analyse intégrée dans ScalpingStrategy
    # from phase_observer.detect_orderflow_v6.orderflow_v6 import detect_orderflow_v6
    # ❌ SUPPRIMÉ (25 DEC 2025): footprint_validator - Non utilisé dans pipeline minimaliste
    # from phase_observer.detectors import footprint_validator
    from phase_observer.orchestrator import PhaseObserver
    from core.config_manager import ConfigManager
    from core.decision_pipeline import DecisionPipeline
    from trader.trade_executor import run_trade_execution_pipeline
    from trader.trade_executor import TradeExecutor, run_trade_execution_pipeline
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
        "overrides",    # <-- FIX 14 JAN 2026: CRITIQUE pour NAS100 (config dans overrides.scalping.entry_rules)
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

    Architecture:
      - MarketAnalyzer (PhaseObserver + PatternEngine) → annotated_df + latest
      - Analyses d'entrée pour la fusion: Footprint M1 (ticks bougie) + Orderflow v5
      - Signals consolidés par actif
      - FusionManager → décisions scalping (USDJPY uniquement)
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

    # ❌ DÉSACTIVÉ (25 DEC 2025): FusionManager - Architecture minimaliste OrderFlow seul
    # === FusionManager requis pour scalping USDJPY ===
    # try:
    #     from phase_observer.fusion_manager import FusionManager
    #     _fusion_mgr = FusionManager(logger=logger)
    # except Exception:
    #     _fusion_mgr = None
    _fusion_mgr = None  # Désactivé - OrderFlow V6 seul maintenant

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
            sym_spread_max = {"NAS100": 80.0, "GBPUSD": 18.0, "USDJPY": 40.0}.get(
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
        signals: dict, latest: dict, symbol_info, mt5c: MT5Connector, sym: str,
        footprint_trigger: Optional[Dict] = None  # ✅ AJOUT: vrai trigger footprint
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

        # Utiliser le vrai footprint trigger si disponible, sinon fallback
        if footprint_trigger:
            # Vrai trigger détecté (climax/stacking/absorption)
            triggers = footprint_trigger
        else:
            # Fallback: trigger minimal déduit de l'orderflow
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

        # ✅ AJOUT (08 DEC 2025): Extraire données de range depuis latest pour logique de retournement
        if latest is not None:
            try:
                import pandas as pd

                # Extraire valeurs avec fallback robuste (gérer pd.NA et None)
                range_pos = latest.get("range_pos_pct")
                if range_pos is None or (isinstance(range_pos, float) and pd.isna(range_pos)):
                    range_pos = 0.5

                in_upper = latest.get("in_upper_tercile")
                if in_upper is None or (hasattr(pd, 'isna') and pd.isna(in_upper)):
                    in_upper = False

                in_lower = latest.get("in_lower_tercile")
                if in_lower is None or (hasattr(pd, 'isna') and pd.isna(in_lower)):
                    in_lower = False

                regime = latest.get("regime")
                if regime is None or (hasattr(pd, 'isna') and pd.isna(regime)):
                    regime = "unknown"

                ctx["range_pos_pct"] = float(range_pos)
                ctx["in_upper_tercile"] = bool(in_upper)
                ctx["in_lower_tercile"] = bool(in_lower)
                ctx["phase_observer_regime"] = str(regime)

                # Log pour diagnostic
                asset_name = signals.get("asset") or sym or "UNKNOWN"
                logger.info(
                    f"[RANGE_CONTEXT][RUN_BOT] {asset_name} | regime={regime} | pos={float(range_pos):.0%} | "
                    f"upper={bool(in_upper)} | lower={bool(in_lower)}"
                )
            except Exception as e:
                logger.warning(f"[RANGE_CONTEXT][RUN_BOT] Failed to extract range data: {e}")
                ctx["range_pos_pct"] = 0.5
                ctx["in_upper_tercile"] = False
                ctx["in_lower_tercile"] = False
                ctx["phase_observer_regime"] = "unknown"

        # ✅ FIX: Charger la config scalping complète (pas juste price_step)
        # pour que FusionManager ait accès aux scoring_thresholds configurés
        step = getattr(symbol_info, "point", None) if symbol_info else None
        strat_cfg = {
            "price_step": float(step) if isinstance(step, (int, float)) else 0.01
        }

        # Charger la config scalping depuis strategy_manager
        try:
            scalping_full_config = strategy_manager.get_strategy_config("scalping") or {}

            # Merger les sections importantes dans strat_cfg
            if "fusion" in scalping_full_config:
                strat_cfg["fusion"] = scalping_full_config["fusion"]

            if "scoring_thresholds" in scalping_full_config:
                strat_cfg["scoring_thresholds"] = scalping_full_config["scoring_thresholds"]

            # Log pour debug (si scoring_thresholds présent)
            if "scoring_thresholds" in strat_cfg:
                logger.info(
                    f"[FUSION_CONFIG] Seuils chargés: "
                    f"high={strat_cfg['scoring_thresholds'].get('high', 'N/A')} "
                    f"moderate={strat_cfg['scoring_thresholds'].get('moderate', 'N/A')} "
                    f"cautious={strat_cfg['scoring_thresholds'].get('cautious', 'N/A')}"
                )
        except Exception as e:
            logger.warning(f"[FUSION_CONFIG] Erreur chargement config scalping: {e}")

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
        FUSION_ASSETS = {"USDJPY"}

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

        # Hard-gate: FusionManager requis pour USDJPY
        if REQUIRE_FUSION_MGR and (_fusion_mgr is None):
            if "USDJPY" in {a.upper() for a in tradeable_assets}:
                logger.critical(
                    "[FUSION] FusionManager requis pour le scalping USDJPY — USDJPY retiré du cycle (aucun fallback)."
                )
                tradeable_assets = [
                    a for a in tradeable_assets if a.upper() != "USDJPY"
                ]

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
        fusion_scalping_decisions: List[Dict[str, Any]] = []

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

                # 🎯 Analyse footprint triggers SUPPRIMÉE (03 DEC 2025)
                # Les triggers ont été supprimés du pipeline de décision.
                # Le système utilisera automatiquement le fallback "fusion_pretrigger"
                # dans _mk_fusion_inputs() qui déduit la direction depuis l'orderflow.
                footprint_trigger_result = None

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

                # ═══════════════════════════════════════════════════════════════
                # 🎯 NOUVEAU PIPELINE MINIMALISTE (25 DEC 2025)
                # OrderFlow V6 + Timing Gatekeeper SEULEMENT
                # ═══════════════════════════════════════════════════════════════
                try:
                    if _fusion_applies(asset):
                        # ========== ÉTAPE 1: TIMING GATEKEEPER (PASS/VETO) ==========
                        timing_verdict = None
                        try:
                            # Récupérer ticks récents pour analyse liquidité
                            ticks_for_timing = ticks_df if ticks_df is not None and not ticks_df.empty else None

                            # 🔧 29 DEC 2025: Passer la config scalping GLOBALE (prioritaire) + asset config (fallback)
                            scalping_config_global = strategy_manager.get_strategy_config("scalping") if strategy_manager else {}
                            # 🐛 FIX (02 JAN 2026): Utiliser load_asset_config au lieu de get_asset_config
                            asset_config_timing = config_manager.load_asset_config(asset)

                            # Appel timing gatekeeper
                            timing_verdict = evaluate_trading_conditions(
                                asset=asset,
                                current_time=pd.Timestamp.now(tz='UTC'),
                                ticks_df=ticks_for_timing,
                                market_context={},
                                asset_config=asset_config_timing,
                                scalping_config=scalping_config_global
                            )

                            logger.info(
                                f"[TIMING_GATEKEEPER][{asset}] {timing_verdict['verdict']} | "
                                f"session={timing_verdict.get('quality_metrics', {}).get('session', 'N/A')} | "
                                f"tick_rate={timing_verdict.get('quality_metrics', {}).get('tick_rate', 0):.1f}/s"
                            )
                        except Exception as e_timing:
                            logger.error(f"[TIMING_GATEKEEPER][{asset}] Erreur: {e_timing}", exc_info=True)
                            timing_verdict = {"verdict": "VETO", "veto_reason": f"Timing analysis error: {e_timing}"}

                        # VETO immédiat si timing n'est pas PASS
                        if timing_verdict and timing_verdict.get("verdict") != "PASS":
                            logger.info(
                                f"[TIMING_VETO][{asset}] {timing_verdict.get('veto_reason', 'Unknown')} - Skip trade cycle"
                            )
                            continue  # Passe au prochain asset (ou termine la boucle)

                        # ========== ÉTAPE 2: RÉCUPÉRATION ORDERFLOW V6 ==========
                        # OrderFlow est déjà calculé par ScalpingStrategy et stocké dans latest
                        orderflow_result = {
                            "score": latest.get("orderflow_score", 0.0),
                            "bias": latest.get("orderflow_bias", "NEUTRAL"),
                            "summary": latest.get("orderflow_summary", {})
                        }

                        logger.info(
                            f"[ORDERFLOW][{asset}] score={orderflow_result['score']:.1f}/100 | "
                            f"bias={orderflow_result['bias']}"
                        )

                        # ========== ÉTAPE 3: DÉCISION DIRECTE via MarketAnalyzer ==========
                        # 🎯 (05 JAN 2026): Lire min_score depuis config asset-specific (dynamique)
                        # Hiérarchie: USDJPY.json → config_trade_scalping.json → défaut (70.0)
                        try:
                            # Charger min_score depuis asset config
                            asset_min_score = 70.0  # Défaut global
                            try:
                                aconf = config_manager.config_loader.load_asset_config(asset) or {}
                                asset_min_score = float(
                                    (aconf.get("entry_rules", {}) or {})
                                    .get("scalping", {})
                                    .get("burst_scalping", {})
                                    .get("min_score", asset_min_score)
                                )
                                logger.debug(f"[CONFIG][{asset}] min_score={asset_min_score} (from asset config)")
                            except Exception as e_min_score:
                                logger.warning(f"[CONFIG][{asset}] Erreur lecture min_score: {e_min_score}, using default={asset_min_score}")

                            decision = market_analyzer.build_decision(
                                orderflow_result=orderflow_result,
                                min_score=asset_min_score
                            )

                            logger.info(
                                f"[DECISION][{asset}] action={decision['action']} | "
                                f"confidence={decision['confidence']:.2f} | "
                                f"rationale={decision['rationale']}"
                            )
                        except Exception as e_decision:
                            logger.error(f"[DECISION][{asset}] Erreur build_decision: {e_decision}", exc_info=True)
                            decision = {"action": "HOLD", "confidence": 0.0, "rationale": f"Decision error: {e_decision}"}

                        # ========== ÉTAPE 4: CONSTRUCTION SCALPING DECISION (si BUY/SELL) ==========
                        if decision["action"] in ["BUY", "SELL"]:
                            # Prix d'ancrage depuis OrderFlow VPOC
                            anchor_price = decision.get("anchor_price") or latest.get("current_price") or latest.get("close")

                            # TTL et slippage depuis config
                            scalping_cfg_entry = base_config.get("entry_rules", {}).get("scalping", {})
                            decision_cfg = scalping_cfg_entry.get("decision", {})
                            ttl_ms = int(decision_cfg.get("validity_ms", 800))
                            slippage_pts = float(decision_cfg.get("max_spread_pts", 15))

                            # Construire décision scalping compatible avec fast-lane
                            fdec = {
                                "ok": True,
                                "action": decision["action"],
                                "fused_confidence": decision["confidence"],  # 0.0-1.0
                                "score": decision["confidence"] * 100.0,  # 0-100 pour compatibilité
                                "price": anchor_price,
                                "ttl_ms": ttl_ms,
                                "slippage_guard_points": slippage_pts,
                                "ts_created": pd.Timestamp.utcnow().value // 1_000_000,
                                "signal_type": "MINIMALIST_ORDERFLOW",
                                "rationale": decision["rationale"],
                                "orderflow_score": orderflow_result["score"],
                                "timing_quality": timing_verdict.get("quality_metrics", {})
                            }

                            # ========== ÉTAPE 5: AJOUTER À FUSION_SCALPING_DECISIONS ==========
                            fusion_scalping_decisions.append({
                                "rule_name": "minimalist_scalping",  # Nouveau nom pour différencier
                                "action": fdec["action"],
                                "asset": asset,
                                "price": fdec.get("price"),
                                "confidence": fdec.get("fused_confidence", 0.0),  # 🔧 (05 JAN 2026): 0-1 scale pour compatibilité avec entry_threshold
                                "no_fallback": True,
                                "entry_style": "MARKET",
                                "validity_ms": int(fdec.get("ttl_ms", 800)),
                                "slippage_guard_points": float(fdec.get("slippage_guard_points", 15.0)),
                                "ts_created": int(fdec.get("ts_created")),
                                "fusion_data": fdec,  # Pour compatibilité avec fast-lane
                                "fusion_full": fdec   # Pour compatibilité avec logging
                            })

                            logger.info(
                                f"[MINIMALIST][{asset}] ✅ {fdec['action']} | "
                                f"score={fdec['score']:.1f}/100 | "
                                f"OF={orderflow_result['score']:.1f} | "
                                f"price={fdec.get('price')} | "
                                f"rationale={fdec.get('rationale', 'N/A')}"
                            )
                        else:
                            # HOLD - OrderFlow score insuffisant
                            logger.info(
                                f"[MINIMALIST][{asset}] HOLD | "
                                f"action={decision['action']} | "
                                f"confidence={decision['confidence']:.2f} | "
                                f"rationale={decision['rationale']}"
                            )

                except Exception as _e:
                    logger.warning(f"[MINIMALIST_PIPELINE] erreur: {_e}", exc_info=True)

            except Exception as e:
                logger.error(f"Erreur collecte données {asset}: {e}", exc_info=True)
                continue

        if not all_assets_trading_signals:
            logger.warning("Aucun signal valide généré. Fin du cycle.")
            return False

        # === FUSION SUMMARY (fichier log uniquement) ===
        logger.debug("=" * 58)
        logger.debug("🔎 FUSION SUMMARY (par actif)")

        def _fmt_float(x):
            try:
                v = float(x)
                return f"{v:.2f}"
            except Exception:
                return str(x)

        for asset, sig in all_assets_trading_signals.items():
            try:
                # Fusion uniquement pour USDJPY
                if asset.upper() != "USDJPY":
                    logger.debug(f"   {asset:<7} → n/a (fusion off)")
                    continue

                # Récupération robuste du 'latest' (clé normalisée)
                _latest = sig.get("__latest__") or {}

                # 1) Snapshot "maintenant" (recompute)
                if _fusion_mgr and hasattr(_fusion_mgr, "fuse"):
                    _syminfo = mt5_connector.get_symbol_info(asset)
                    of, fp, trig, strat_cfg, ctx = _mk_fusion_inputs(
                        sig, _latest, _syminfo, mt5_connector, asset,
                        footprint_trigger=None  # Snapshot: pas de trigger temps réel
                    )

                    # === VWAP Analysis pour snapshot (cohérence avec décision réelle) ===
                    vwap_snapshot = None
                    try:
                        market_data = all_assets_market_data.get(asset, {})
                        df_snap = market_data.get("annotated_rates_df")
                        price_snap = market_data.get("current_price") or _latest.get("current_price") or _latest.get("close")

                        if df_snap is not None and price_snap is not None:
                            scalping_config = strategy_manager.get_strategy_config("scalping") or strat_cfg or {}

                            # ✅ FIX: Reset index pour avoir 'time' en colonne (VWAP le requiert)
                            df_snap_with_time = df_snap.copy()
                            if 'time' not in df_snap_with_time.columns and df_snap_with_time.index.name in ['time', None]:
                                df_snap_with_time = df_snap_with_time.reset_index()
                                if df_snap_with_time.columns[0] != 'time':
                                    df_snap_with_time = df_snap_with_time.rename(columns={df_snap_with_time.columns[0]: 'time'})

                            # ✅ Enrichir contexte avec régime PhaseObserver pour snapshot
                            vwap_snap_ctx = ctx.copy() if ctx else {}

                            # Extraire régime depuis df_snap
                            if not df_snap.empty and 'regime' in df_snap.columns:
                                try:
                                    phase_observer_regime = str(df_snap['regime'].iloc[-1])
                                    vwap_snap_ctx['phase_observer_regime'] = phase_observer_regime
                                except Exception:
                                    pass

                            vwap_analyzer = create_vwap_analyzer(asset, scalping_config)
                            vwap_analysis = vwap_analyzer.analyze(df_snap_with_time, price_snap, vwap_snap_ctx)
                            vwap_snapshot = vwap_analysis.to_dict()
                        else:
                            vwap_snapshot = {"score": 0.0, "status": "INVALID", "bias": "NEUTRAL", "reason": "snapshot_missing_data"}
                    except Exception as e:
                        logger.debug(f"[VWAP][{asset}] Snapshot analysis skipped: {e}")
                        vwap_snapshot = {"score": 0.0, "status": "INVALID", "bias": "NEUTRAL", "error": str(e)}

                    fdec_syn = _fusion_mgr.fuse(
                        orderflow=of,
                        footprint=fp,
                        vwap=vwap_snapshot,  # ✅ VWAP pour snapshot
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
                logger.debug(f"   {asset:<7} → {used_line} | {snap_line}")
            else:
                logger.debug(f"   {asset:<7} → {snap_line}")

        # === GATECHECK USDJPY (diagnostic) ===
        try:

            sig = (all_assets_trading_signals or {}).get("USDJPY", {})
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
                fp_raw = sig.get("footprint_summary") or {}
                of_raw = sig.get("orderflow_summary") or {}

                # Parser footprint_summary si c'est une string
                if isinstance(fp_raw, str):
                    try:
                        import ast
                        fp = ast.literal_eval(fp_raw)
                    except Exception:
                        fp = {}
                else:
                    fp = fp_raw if isinstance(fp_raw, dict) else {}

                # Parser orderflow_summary si c'est une string
                if isinstance(of_raw, str):
                    try:
                        import ast
                        of = ast.literal_eval(of_raw)
                    except Exception:
                        of = {}
                else:
                    of = of_raw if isinstance(of_raw, dict) else {}

                # métriques mesurées
                ticks = fp.get("tick_count", None)
                cov = fp.get("coverage_s", None)
                trate = fp.get("tick_rate", None)
                dlt = of.get("delta_total", None)
                of_sc = latest.get("orderflow_score", None)
                spread = sig.get("current_spread_points", float("nan"))

                # seuils depuis conf (priorité: asset overrides -> strategy scalping -> fallback)
                xcfg = config_manager.load_asset_config("USDJPY") or {}
                scalping_cfg = strategy_manager.get_strategy_config("scalping") or {}

                # Footprint M1 (depuis config_trade_scalping.json)
                m1_min_ticks = int(
                    _dig(
                        xcfg,
                        ["overrides", "scalping", "footprint", "m1_min_ticks"],
                        _dig(
                            scalping_cfg,
                            ["entry_rules", "scalping", "footprint", "m1_min_ticks"],
                            50,  # Fallback aligné avec config_trade_scalping.json (seuils relevés)
                        ),
                    )
                )
                m1_min_cov_s = float(
                    _dig(
                        xcfg,
                        ["overrides", "scalping", "footprint", "m1_min_coverage_s"],
                        _dig(
                            scalping_cfg,
                            ["entry_rules", "scalping", "footprint", "m1_min_coverage_s"],
                            15.0,  # Fallback aligné avec config_trade_scalping.json (seuils relevés)
                        ),
                    )
                )
                tickrate_min = float(
                    _dig(
                        xcfg,
                        ["overrides", "scalping", "footprint", "tickrate_min"],
                        _dig(
                            scalping_cfg,
                            ["entry_rules", "scalping", "footprint", "tickrate_min"],
                            2.0,  # Fallback aligné avec config_trade_scalping.json (seuils relevés)
                        ),
                    )
                )
                cov_burst_min = float(
                    _dig(
                        xcfg,
                        ["overrides", "scalping", "footprint", "coverage_s_min_burst"],
                        _dig(
                            scalping_cfg,
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
                            scalping_cfg,
                            ["entry_rules", "scalping", "orderflow", "delta_abs_min"],
                            30.0,
                        ),
                    )
                )
                degr_min_of_sc = float(
                    _dig(
                        scalping_cfg,
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
                        scalping_cfg,
                        ["entry_rules", "scalping", "fusion", "max_spread_points"],
                        float("inf"),
                    )
                )

                # News blackout
                news_blackout = bool(
                    _dig(scalping_cfg, ["guardrails", "sessions", "news_blackout"], False)
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

                # 5) FusionManager diagnostic (pas de veto : on log l'état HOLD/SIGNAL)
                # ⚠️ DÉSACTIVÉ: Ce diagnostic bloquait les trades car il ne passait pas le VWAP
                # try:
                #     if _fusion_mgr and hasattr(_fusion_mgr, "fuse"):
                #         _syminfo = mt5_connector.get_symbol_info("USDJPY")
                #         of_i, fp_i, trig_i, strat_cfg_i, ctx_i = _mk_fusion_inputs(
                #             sig, latest, _syminfo, mt5_connector, "USDJPY"
                #         )
                #         fdec_diag = _fusion_mgr.fuse(
                #             orderflow=of_i,
                #             footprint=fp_i,
                #             strategy_config=strat_cfg_i,
                #             context=ctx_i,
                #         )
                #         if not fdec_diag.get("ok"):
                #             blocks.append(
                #                 f"fusion={fdec_diag.get('signal_type','WAIT_CONFIRMATION')}"
                #             )
                # except Exception:
                #     pass

                # 6) Burst guard global (panier existant) déjà checké plus bas, mais on log ici aussi
                try:
                    burst_cfg = (
                        base_config.get("entry_rules", {})
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
                    logger.debug("[BLOCKERS][USDJPY] " + " | ".join(blocks))
                else:
                    logger.debug("[BLOCKERS][USDJPY] none")
        except Exception as _e:
            logger.debug(f"[GATECHECK][USDJPY] skip: {_e}")

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

        # === FAST-LANE (exécuter immédiatement la meilleure décision Fusion valide) ===
        try:
            # ✅ FIX Bug #2: Filtrer par confidence minimum (respecte entry_threshold configuré)
            # Lecture du seuil configuré (ex: entry_threshold: 0.70 dans USDJPY.json)
            # Si pas configuré, utilise seuil MODERATE par défaut (0.70)
            filtered_fusion_decisions = []
            for fd in fusion_scalping_decisions:
                sym = str(fd.get("asset", "")).upper()
                confidence = float(fd.get("confidence", 0.0))

                # Lire entry_threshold depuis asset config
                try:
                    aconf = config_manager.load_asset_config(sym) or {}
                    entry_threshold = float(
                        (
                            (aconf.get("entry_rules", {}) or {})
                            .get("scalping", {})
                            .get("burst_scalping", {})
                            .get("entry_threshold", 0.70)  # Fallback MODERATE
                        ) or 0.70
                    )
                except Exception:
                    entry_threshold = 0.70  # Fallback MODERATE si erreur lecture config

                # Filtrer les signaux avec confidence >= seuil configuré
                if confidence >= entry_threshold:
                    filtered_fusion_decisions.append(fd)
                else:
                    logger.info(
                        f"⛔ [FUSION][FAST-LANE] {sym} signal rejeté (confidence={confidence:.1%} < seuil={entry_threshold:.1%})"
                    )

            if filtered_fusion_decisions:
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

                    filtered_fusion_decisions.sort(key=_score_fd, reverse=True)
                    best = filtered_fusion_decisions[0]
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
                                        # 1. Asset override (USDJPY.json)
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
                                # ✅ AJOUT: Données FusionManager complètes
                                "confidence": best.get("confidence"),
                                "fusion_data": best.get("fusion_full", {}),  # Données complètes de FusionManager
                            }
                            # --- Harmoniser fat-finger policy avec la voie normale ---
                            safety = td.setdefault("safety", {})
                            ff = safety.setdefault("fat_finger", {})
                            ff.setdefault("policy", "FLOOR")  # clamp plutôt qu'abandon
                            # (le cap max volume/order reste lu de la conf actif)

                            # ⚡ OPTIMISATION LATENCE: Cache config SLTP (gain ~5-10ms)
                            # Évite de re-parser la config à chaque trade
                            cache_key = f"_sltp_cfg_{sym}"
                            sltp_cfg = global_context.get(cache_key)

                            if sltp_cfg is None:
                                # Premier calcul: parser la config (coûteux)
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
                                # Stocker dans le cache
                                global_context[cache_key] = sltp_cfg

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

        # === WHY_NO_TRADE si rien à exécuter ===
        if not scalping_decisions and not liquidity_decisions:
            try:
                for asset, sig in all_assets_trading_signals.items():
                    # ✅ FIX (20 DEC 2025): EURUSD/GBPUSD utilisent stratégie liquidité (pas fusion)
                    if asset.upper() != "USDJPY":
                        # Ne PAS logger "fusion_off" - c'est NORMAL pour liquidité
                        logger.debug(f"[WHY_NO_TRADE][{asset}] Stratégie liquidité - pas de diagnostic fusion")
                        continue

                    # Diagnostic fusion UNIQUEMENT pour USDJPY
                    if _fusion_mgr and hasattr(_fusion_mgr, "fuse"):
                        _latest = sig.get("__latest__") or {}

                        _syminfo = mt5_connector.get_symbol_info(asset)
                        of, fp, trig, strat_cfg, ctx = _mk_fusion_inputs(
                            sig, _latest, _syminfo, mt5_connector, asset
                        )
                        fdec_diag = _fusion_mgr.fuse(
                            orderflow=of,
                            footprint=fp,
                            strategy_config=strat_cfg,
                            context=ctx,
                        )
                    else:
                        fdec_diag = {"ok": False, "reason": "fusion_manager_missing"}

                    if fdec_diag and fdec_diag.get("ok"):
                        logger.info(f"[WHY_NO_TRADE][USDJPY] fusion_ok")
                    else:
                        logger.info(
                            f"[WHY_NO_TRADE][USDJPY] hold={(fdec_diag or {}).get('signal_type','WAIT_CONFIRMATION')}"
                        )

            except Exception as _e:
                logger.debug(f"[WHY_NO_TRADE] diagnostic skip: {_e}")

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
    logger
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

    # ❌ DÉSACTIVÉ (25 DEC 2025): FusionManager - Architecture minimaliste
    # # ✅ Instancier FusionManager pour ce thread
    # try:
    #     from phase_observer.fusion_manager import FusionManager
    #     fusion_mgr = FusionManager(logger=logger)
    #     logger.info("✅ [{asset}] FusionManager instancié")
    # except Exception as e:
    #     logger.error(f"❌ [{asset}] Impossible de créer FusionManager: {e}")
    #     fusion_mgr = None
    fusion_mgr = None  # Désactivé - OrderFlow V6 seul

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

    # 🐻 NOUVEAU (14 JAN 2026): Instancier InstitutionalReversalDetector pour validation BEARISH
    reversal_detector = None
    try:
        from phase_observer.institutional_reversal_detector import InstitutionalReversalDetector
        reversal_detector = InstitutionalReversalDetector(config=None, logger=logger)
        logger.info(f"✅ [{asset}] InstitutionalReversalDetector instancié (standalone v2.1)")
    except Exception as e:
        logger.error(f"❌ [{asset}] Impossible de créer InstitutionalReversalDetector: {e}")
        reversal_detector = None  # Continue sans reversal detector

    # 🐻 NOUVEAU (14 JAN 2026): Instancier BearishScalpingValidator pour trades BEARISH
    bearish_validator = None
    try:
        from phase_observer.bearish_scalping_validator import BearishScalpingValidator
        if reversal_detector and price_memory_analyzer:
            bearish_validator = BearishScalpingValidator(
                reversal_detector=reversal_detector,
                price_memory_analyzer=price_memory_analyzer,
                config=None,  # Utilise config par défaut
                logger=logger
            )
            logger.info(f"✅ [{asset}] BearishScalpingValidator instancié (M1 ultra-rapide)")
        else:
            logger.warning(f"⚠️ [{asset}] BearishScalpingValidator skip: reversal_detector ou price_memory manquant")
    except Exception as e:
        logger.error(f"❌ [{asset}] Impossible de créer BearishScalpingValidator: {e}")
        bearish_validator = None  # Continue sans bearish validator

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

    # 🐻 NOUVEAU (14 JAN 2026): Buffers historiques pour reversal detector
    from collections import deque
    cvd_history = deque(maxlen=100)
    delta_history = deque(maxlen=100)
    volume_history = deque(maxlen=100)

    # Cache du dernier résultat reversal detector (éviter recalculs)
    last_reversal_check = None
    reversal_check_counter = 0

    logger.info(f"✅ [{asset}] Buffers historiques CVD/Delta/Volume initialisés (100 valeurs max)")

    while not stop_event.is_set():
        cycle_count += 1
        cycle_start = time.time()
        global_state.increment_cycle(asset)

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

                    # Fusionner config scalping avec base_config + asset overrides (31 DEC 2025)
                    try:
                        scalping_strategy_config = strategy_manager.get_strategy_config("scalping") or {}
                        merged_config = dict(base_config)
                        if "entry_rules" in scalping_strategy_config:
                            merged_config.setdefault("entry_rules", {}).update(
                                scalping_strategy_config["entry_rules"]
                            )

                        # ✅ FIX (31 DEC 2025): Fusionner asset-specific overrides (target_profit_pips, etc.)
                        # 🐛 FIX (02 JAN 2026): Utiliser load_asset_config au lieu de get_asset_config
                        asset_config = config_manager.load_asset_config(asset)
                        asset_overrides = asset_config.get("overrides", {}).get("scalping", {})
                        if asset_overrides:
                            # Fusionner deep les overrides d'actif dans entry_rules.scalping.burst_scalping
                            burst_scalping_path = merged_config.setdefault("entry_rules", {}).setdefault("scalping", {}).setdefault("burst_scalping", {})

                            # Fusionner closure_rules si présent dans asset overrides
                            if "closure_rules" in asset_overrides:
                                burst_scalping_path.setdefault("closure_rules", {}).update(asset_overrides["closure_rules"])

                            # Fusionner sltp si présent dans asset overrides
                            if "sltp" in asset_overrides:
                                burst_scalping_path.setdefault("sltp", {}).update(asset_overrides["sltp"])

                            logger.info(f"✅ [CONFIG_MERGE] Asset overrides appliqués pour {asset}")
                    except Exception as e:
                        logger.warning(f"[{asset}] Fusion config échouée: {e}")
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

                        # Préparer asset_signals (03 JAN 2026: Ajouter ticks pour analyseurs institutionnels)
                        asset_signals_for_of = {
                            "footprint_summary": {},
                            "orderflow_summary": {},
                            "ticks_df": ticks_df  # 🆕 Pour analyseurs institutionnels (Phase 1+2)
                        }

                        # Appel OrderFlow V6
                        of_v6_result = scalping_strategy._analyze_orderflow_v6(
                            asset=asset,
                            df_m1=rates_df_fresh,
                            df_m3=None,
                            df_m5=None,
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

                        # 🐻 NOUVEAU (14 JAN 2026): Alimentation buffers historiques pour reversal detector
                        try:
                            # Récupérer CVD, delta, volume depuis of_v6_result
                            current_cvd = of_v6_result.get('cvd', 0.0)
                            current_delta = of_v6_result.get('delta', 0.0)
                            current_volume = rates_df_fresh.iloc[-1]['tick_volume'] if 'tick_volume' in rates_df_fresh.columns else 0.0

                            # Ajouter aux buffers
                            cvd_history.append(current_cvd)
                            delta_history.append(current_delta)
                            volume_history.append(current_volume)

                            logger.debug(
                                f"[BUFFER_FEED][{asset}] CVD={current_cvd:.2f} | "
                                f"Delta={current_delta:.0f} | Volume={current_volume:.0f} | "
                                f"Buffer size: CVD={len(cvd_history)}, Delta={len(delta_history)}, Vol={len(volume_history)}"
                            )
                        except Exception as e_buffer:
                            logger.warning(f"[BUFFER_FEED][{asset}] Erreur alimentation buffers: {e_buffer}")

                        # 🐻 NOUVEAU (14 JAN 2026): Appel reversal detector (1x/10 cycles = 25s)
                        reversal_check_counter += 1
                        if reversal_check_counter >= 10 and reversal_detector:
                            reversal_check_counter = 0

                            # Vérifier qu'on a assez de données (minimum 30 valeurs)
                            if len(cvd_history) >= 30:
                                try:
                                    # Préparer market_data pour reversal detector
                                    # Note: On utilise rates_df_fresh (M1) pour les deux (M1 et M5)
                                    # Le reversal detector peut fonctionner avec M1 seulement
                                    market_data_reversal = {
                                        'candles_m5': rates_df_fresh,  # Utilise M1 (pas de M5 dans scalping_worker)
                                        'candles_m1': rates_df_fresh,
                                        'cvd_values': list(cvd_history),
                                        'delta_values': list(delta_history),
                                        'volume_values': list(volume_history)
                                    }

                                    # Appeler le détecteur
                                    last_reversal_check = reversal_detector.detect_reversal(market_data_reversal)

                                    # Log du résultat
                                    logger.critical(
                                        f"🏛️ [REVERSAL_CHECK][{asset}] "
                                        f"Score={last_reversal_check['institutional_score']:.1f}/100 | "
                                        f"Conviction={last_reversal_check['conviction_level']} | "
                                        f"Trend={last_reversal_check['new_trend']} | "
                                        f"Reversal={last_reversal_check['reversal_detected']}"
                                    )

                                except Exception as e_reversal:
                                    logger.error(f"[REVERSAL_DETECTOR][{asset}] Erreur: {e_reversal}")
                                    last_reversal_check = None
                            else:
                                logger.debug(
                                    f"[REVERSAL_DETECTOR][{asset}] Pas assez de données "
                                    f"({len(cvd_history)} < 30), skip ce cycle"
                                )

                    except Exception as e_of:
                        logger.critical(f"[ORDERFLOW_V6_ERROR] Erreur: {e_of}", exc_info=True)
                        orderflow_result_mini = {"score": 0.0, "bias": "NEUTRAL", "summary": {}, "composite_enabled": False}

                # ═══════════════════════════════════════════════════════════════
                # 🧠 PRICE MEMORY TREND ANALYSIS (08 JAN 2026)
                # Détecte la tendance historique sur 15 bougies M1 (réactivité micro-tendances)
                # ═══════════════════════════════════════════════════════════════
                try:
                    # Vérifier que l'analyseur est disponible
                    if price_memory_analyzer is not None:
                        # Analyser la structure de tendance
                        latest_candle_for_memory = market_results.get("latest", {})
                        current_price = latest_candle_for_memory.get('close', 0.0) if latest_candle_for_memory else 0.0

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
                orderflow_score = orderflow_result_mini['score']

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

                            if abs(delta_weighted) >= delta_threshold:
                                if delta_weighted > 0:
                                    filtre1_direction = "BUY"
                                    filtre1_confidence = min(1.0, abs(delta_weighted) / (delta_threshold * 3))
                                else:
                                    filtre1_direction = "SELL"
                                    filtre1_confidence = min(1.0, abs(delta_weighted) / (delta_threshold * 3))

                            filtre1_status = "✅ PASS" if filtre1_direction in ["BUY", "SELL"] else "❌ FAIL"
                            filtre1_detail = f"Δw={delta_weighted:.1f} (M1:{delta_m1:.0f}×0.6 + M3:{delta_m3:.1f}×0.4) CVD:{cvd_slope:.2f} {'✅' if cvd_aligned else '❌'}"

                            # ═══════════════════════════════════════════════════════════
                            # FILTRE 2: MICROSTRUCTURE
                            # ═══════════════════════════════════════════════════════════
                            conditions_micro = []

                            # 1. Volume fort (>150% moyenne)
                            volume_strong = vol_ratio > 1.5
                            conditions_micro.append(("Volume>150%", volume_strong, f"ratio={vol_ratio:.2f}"))

                            # 2. Activité élevée (>2 ticks/sec) - Ajuster selon asset
                            tickrate_min_threshold = 2.0  # Default
                            if asset == "NAS100":
                                tickrate_min_threshold = 5.0
                            elif asset == "GBPUSD":
                                tickrate_min_threshold = 3.5
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
                            # 🐻 NOUVEAU (14 JAN 2026): VALIDATION BEARISH (MODE LOG ONLY)
                            # Ajoute un boost/malus pour les trades BEARISH basé sur validation croisée
                            # ═══════════════════════════════════════════════════════════
                            bearish_boost = 0.0
                            bearish_validation_result = None

                            if filtre1_direction == "SELL" and bearish_validator and last_reversal_check:
                                try:
                                    # Récupérer le temps de la bougie actuelle (pour timing validation)
                                    from datetime import datetime
                                    current_time_dt = datetime.now()
                                    candle_open_time_dt = pd.to_datetime(rates_df_fresh.iloc[-1]['time'])

                                    # Appeler le validateur BEARISH
                                    bearish_validation_result = bearish_validator.validate_bearish_trade(
                                        symbol=asset,
                                        current_price=rates_df_fresh.iloc[-1]['close'],
                                        current_time=current_time_dt,
                                        candle_open_time=candle_open_time_dt,
                                        historical_data_m1=rates_df_fresh,
                                        reversal_result=last_reversal_check
                                    )

                                    # Récupérer le boost (mais NE PAS l'appliquer en mode LOG ONLY)
                                    bearish_boost = bearish_validation_result.score_boost

                                    # 🧪 MODE LOG ONLY: Log le boost théorique sans l'appliquer
                                    logger.critical(
                                        f"🧪 [BEARISH_TEST][{asset}] "
                                        f"Level={bearish_validation_result.validation_level} | "
                                        f"Confidence={bearish_validation_result.confidence:.2f} | "
                                        f"Boost théorique={bearish_boost:+.1f} | "
                                        f"Score actuel={original_score:.1f} | "
                                        f"Score ajusté théorique={original_score + bearish_boost:.1f}"
                                    )

                                    # Afficher les raisons
                                    for reason in bearish_validation_result.reasons:
                                        logger.info(f"  {reason}")

                                    # 🚫 MODE LOG ONLY: NE PAS appliquer le boost (pour test)
                                    # bearish_boost = 0.0  # ← Déjà 0.0 par défaut, on ne l'applique pas

                                except Exception as e_bearish:
                                    logger.error(f"[BEARISH_VALIDATOR][{asset}] Erreur: {e_bearish}")
                                    bearish_boost = 0.0

                            # ═══════════════════════════════════════════════════════════
                            # DÉCISION FINALE: LES 3 FILTRES DOIVENT PASSER
                            # ═══════════════════════════════════════════════════════════
                            all_filters_pass = filtre1_direction in ["BUY", "SELL"] and filtre2_pass and filtre3_pass

                            # Bonus si Price Memory aligné
                            bonus_memory = 30 if memory_aligned else 0

                            # 🐻 NOTE: En mode LOG ONLY, bearish_boost n'est PAS appliqué (reste 0.0)
                            # Pour activer, décommenter la ligne ci-dessous:
                            # adjusted_score = original_score + bonus_memory + bearish_boost
                            adjusted_score = original_score + bonus_memory  # MODE LOG ONLY

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
                                "context": ctx
                            }

                            logger.info(
                                f"🎯 [MINIMALIST][{asset}] ✅ {fusion_out['action']} | "
                                f"confidence={fusion_out['fused_confidence']:.2f} | "
                                f"OF_score={orderflow_result_mini['score']:.1f}"
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
                                "context": ctx
                            }

                            logger.info(
                                f"🎯 [MINIMALIST][{asset}] ✅ {fusion_out['action']} | "
                                f"confidence={fusion_out['fused_confidence']:.2f} | "
                                f"OF_score={orderflow_result_mini['score']:.1f}"
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
                for asset_name in ["USDJPY", "NAS100", "GBPUSD"]:
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

        # Instancier AI Decision
        
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
    logger.info(f"  • SCALPING NAS100 Thread : Cycle {scalping_cycle}s (offset 1.5s)")
    logger.info(f"  • SCALPING GBPUSD Thread : Cycle {scalping_cycle}s (offset 3.0s)")
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
    assets = ["USDJPY", "NAS100", "GBPUSD"]
    global_scalping_state = GlobalScalpingState(assets)

    # ✅ Créer Display Queue (31 DEC 2025 - Solution B)
    display_queue = queue.Queue(maxsize=100)

    # Events pour arrêt propre
    scalping_stop_event = threading.Event()
    dashboard_stop_event = threading.Event()
    basket_monitor_stop_event = threading.Event()

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
    thread_nas100.start()
    thread_gbpusd.start()
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
            thread_nas100.join(timeout=5.0)
            thread_gbpusd.join(timeout=5.0)
            thread_dashboard.join(timeout=5.0)
            basket_monitor.join(timeout=5.0)

            # ✅ Vérifier threads encore actifs
            for thread in [thread_usdjpy, thread_nas100, thread_gbpusd, thread_dashboard]:
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
