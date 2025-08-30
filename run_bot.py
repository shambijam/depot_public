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
from datetime import datetime
from pathlib import Path
from datetime import UTC
from typing import Any
import pandas as pd
from dotenv import load_dotenv
from typing import Any, Dict, Optional, List, Tuple
from core.diagnostics import DiagnosticTracker, get_tracker_from_context


load_dotenv()

try:
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


# ------------------- FONCTION CORRIGÉE -------------------
def _build_asset_trading_signals(
    latest_signals_row: pd.Series,
    symbol_info_mt5: Any,
    asset: str = None,
    mt5_connector: Any = None,
) -> dict:
    """
    Construit le dictionnaire de signaux pour un actif donné en conservant
    TOUTES les colonnes du PhaseObserver et en ajoutant:
      - Spread robuste en points (fallback via (ask-bid)/point)
      - Features MTF utiles au scalping: alignement M5/M15, direction, ATR M5
      - Micro-timing M1: break HH/LL & écart EMA20/EMA50
    """
    # 1) Tout le contenu PhaseObserver
    signals = latest_signals_row.to_dict()

    # 2) Infos broker de base (compatibilité avec le code existant)
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
    #    - si 'spread' vaut 0 / None, on calcule via MT5Connector (ask-bid)/point
    current_spread_points = None
    try:
        if (
            isinstance(signals.get("spread"), (int, float))
            and float(signals["spread"]) > 0
        ):
            current_spread_points = float(signals["spread"])
        elif mt5_connector and asset:
            current_spread_points = float(mt5_connector.get_symbol_spread_points(asset))
    except Exception:
        current_spread_points = None
    signals["current_spread_points"] = (
        current_spread_points if current_spread_points is not None else float("inf")
    )

    # 5) Features MTF (si on a le connector et l'asset)
    if mt5_connector and asset:
        import numpy as np
        import pandas as pd

        def _ema(x: np.ndarray, n: int) -> np.ndarray:
            k = 2 / (n + 1.0)
            ema = np.empty_like(x, dtype=float)
            ema[0] = x[0]
            for i in range(1, len(x)):
                ema[i] = k * x[i] + (1 - k) * ema[i - 1]
            return ema

        def _atr(df: pd.DataFrame, n: int = 14) -> float:
            h = df["high"].to_numpy()
            l = df["low"].to_numpy()
            c = df["close"].to_numpy()
            prev = np.r_[c[0], c[:-1]]
            tr = np.maximum.reduce([h - l, np.abs(h - prev), np.abs(l - prev)])
            atr = np.empty_like(tr)
            atr[0] = tr[0]
            for i in range(1, len(tr)):
                atr[i] = (atr[i - 1] * (n - 1) + tr[i]) / n
            return float(atr[-1])

        # M5 / M15 : sens et alignement
        try:
            m5 = mt5_connector.get_rates(asset, "M5", 200)
            m15 = mt5_connector.get_rates(asset, "M15", 200)
            if m5 is not None and not m5.empty and m15 is not None and not m15.empty:
                e20_5 = _ema(m5["close"].to_numpy(), 20)[-1]
                e50_5 = _ema(m5["close"].to_numpy(), 50)[-1]
                e20_15 = _ema(m15["close"].to_numpy(), 20)[-1]
                e50_15 = _ema(m15["close"].to_numpy(), 50)[-1]
                up = (e20_5 > e50_5) and (e20_15 > e50_15)
                down = (e20_5 < e50_5) and (e20_15 < e50_15)
                signals["mtf_ema_align"] = bool(up or down)
                signals["mtf_direction"] = "up" if up else ("down" if down else "none")
                signals["atr_m5"] = _atr(m5, 14)
        except Exception:
            # En cas de souci data, on n'écrase rien
            pass

        # M1 : micro-structure & écart EMA
        try:
            m1 = mt5_connector.get_rates(asset, "M1", 200)
            if m1 is not None and not m1.empty:
                e20_1 = _ema(m1["close"].to_numpy(), 20)[-1]
                e50_1 = _ema(m1["close"].to_numpy(), 50)[-1]
                signals["m1_ema_spread"] = abs(float(e20_1 - e50_1))
                # Break du plus haut/bas des 10 dernières barres (micro timing)
                hh = m1["high"].rolling(10).max()
                ll = m1["low"].rolling(10).min()
                signals["m1_last_hh_break"] = bool(m1["close"].iloc[-1] > hh.iloc[-2])
                signals["m1_last_ll_break"] = bool(m1["close"].iloc[-1] < ll.iloc[-2])
                # Optionnel: ratio volume tick récent vs moyenne
                if "tick_volume" in m1.columns and len(m1) >= 21:
                    tv = m1["tick_volume"].to_numpy()
                    signals.setdefault(
                        "volume_zscore",
                        float((tv[-1] - tv[-21:-1].mean()) / (tv[-21:-1].std() + 1e-9)),
                    )
        except Exception:
            pass

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
    mt5_connector, phase_observer, config_manager, tradeable_assets, cycle_count
) -> bool:
    """
    Gate MTF BLOQUANT… mais *gracieux* :
    - Si 'readiness_gate.enabled' est False (ou absent) -> ON LAISSE PASSER.
    - Si une erreur survient (lecture config, etc.) -> ON LAISSE PASSER.
    - Sinon on vérifie:
        * N premiers cycles bloqués,
        * historique minimum par TF (incluant le plancher 'bars_min' de prod_config.timeframe_mapping),
        * fraîcheur des données (max_allowed_data_age_seconds) si défini,
        * confluence MTF via PhaseObserver si dispo.
    """
    logger = logging.getLogger(__name__)
    try:
        po_cfg = _load_po_config_safe(config_manager)

        gate_cfg = po_cfg.get("readiness_gate") or {}
        if not gate_cfg.get("enabled", False):
            return True  # gate désactivé

        mtf_cfg = po_cfg.get("multi_timeframe_settings") or {}
        data_req = po_cfg.get("data_requirements") or {}

        required_tfs = [
            str(tf).upper() for tf in mtf_cfg.get("timeframes", ["M1", "M5", "M15"])
        ]
        confluence_required = int(mtf_cfg.get("confluence_required", 2))
        block_first_cycles = int(gate_cfg.get("block_signals_first_n_cycles", 12))
        max_age_sec = int(
            gate_cfg.get("max_allowed_data_age_seconds", 0) or 0
        )  # 0 = pas de check fraîcheur

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

        # (3) Historique minimum + fraîcheur par TF / actif
        from datetime import datetime, timezone

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
                        if not getattr(last_ts, "tzinfo", None):
                            # sécurité: on force UTC si la colonne n’est pas timezone-aware
                            last_ts = last_ts.tz_localize("UTC")
                        age = (
                            datetime.now(timezone.utc) - last_ts.to_pydatetime()
                        ).total_seconds()
                        if age > max_age_sec:
                            logger.info(
                                f"[READINESS] skip -> {asset} {tf} data too old ({int(age)}s > {max_age_sec}s)"
                            )
                            return False
                    except Exception:
                        # on reste permissif si le parse de temps pose souci
                        pass

        # (4) Confluence via PhaseObserver (si dispo)
        if hasattr(phase_observer, "ready_and_confluence_ok"):
            ok, reason = phase_observer.ready_and_confluence_ok(
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
    """
    Exécute un cycle complet du pipeline de trading de SNIPER_X (version sans crypto + attente historique).

    Corrections & améliorations intégrées dans CE BLOC :
    - ✅ SUPPRIME la duplication du bloc (bug: le code recommençait après le `return`).
    - ✅ Import local sécurisé de `get_tracker_from_context` pour éviter NameError au `finally`.
    - ✅ Injection NORMALISÉE des infos Bollinger/Micro-phase dans `signals["boll"]` + alias `signals["m1_boll"]`.
    - ✅ Injection robuste de `signals["point"]` et `signals["close"]` (fallbacks) pour le calcul de pips en aval.
    - ✅ Injection de `signals["phase"]` et `signals["confidence_score"]` depuis l’annotated DF (cohérence décisionnelle).
    - ✅ Calcul du spread en points consolidé (déjà présent) conservé, avec fallback infini en cas d’échec.
    """

    # import DIAG local (sécurisé)
    try:
        from core.diagnostics import get_tracker_from_context
    except Exception:

        def get_tracker_from_context(_):  # no-op fallback
            class _N:
                def emit_summary(self, *_args, **_kwargs): ...

            return _N()

    logger = logging.getLogger(__name__)
    print(f"🔍 [PIPELINE] Cycle #{cycle_count} - Début de run_single_pipeline_cycle")
    logger.info(
        f"--- Démarrage du Cycle de Pipeline #{cycle_count} (Trades Aujourd'hui: {daily_trade_count}) ---"
    )
    trade_executed_successfully = False
    global_context: Dict[str, Any] = (
        {}
    )  # évite NameError dans le finally si erreur avant construction

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
            tradeable_assets = (
                all_symbols  # fallback si la liste du compte est vide/non fournie
            )

        print(f"🎯 [PIPELINE] Assets tradables: {tradeable_assets}")

        if not tradeable_assets:
            logger.warning("Aucun actif à trader pour ce cycle. Cycle ignoré.")
            return False

        all_assets_market_data: Dict[str, pd.DataFrame] = {}
        all_assets_trading_signals: Dict[str, Dict[str, Any]] = {}

        timeframe_str = (base_config.get("data_collection", {}) or {}).get(
            "default_timeframe", "M1"
        )
        bars_to_fetch = int(
            (base_config.get("data_collection", {}) or {}).get(
                "default_bars_count", 500
            )
        )

        # 🔑 Vérifier un historique minimum avant d'autoriser l'actif
        min_required_bars = 50  # nombre minimum de bougies

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
                    # getattr pour robustesse si certains champs n'existent pas selon le broker
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
                    logger.warning(f"[{asset}] Annotated DF vide. Actif ignoré.")
                    continue

                # === LIGNE ACTIVE → on lit la dernière ligne annotée par le PhaseObserver
                latest = annotated_rates_df.iloc[-1]
                logger.info(
                    f"[PhaseObserver] Actif: {asset} | Phase: {latest.get('phase', 'N/A')}"
                )

                # === Builder des signaux de l'actif (fonction utilitaire locale au run_bot)
                signals: Dict[str, Any] = (
                    _build_asset_trading_signals(
                        latest,
                        symbol_info_mt5,
                        asset=asset,
                        mt5_connector=mt5_connector,
                    )
                    or {}
                )

                # -- Injection d'un close fiable si manquant/<=0
                try:
                    close_val = signals.get("close", 0.0)
                    if not isinstance(close_val, (int, float)) or close_val <= 0:
                        signals["close"] = float(annotated_rates_df["close"].iloc[-1])
                except Exception:
                    pass  # ne bloque pas le cycle

                # -- Injection du point (utile pour le sizing pips en aval)
                try:
                    point_val = signals.get("point")
                    if not isinstance(point_val, (int, float)) or point_val <= 0:
                        if "point" in annotated_rates_df.columns:
                            signals["point"] = float(
                                annotated_rates_df["point"].iloc[-1]
                            )
                        elif symbol_info_mt5 and getattr(symbol_info_mt5, "point", 0.0):
                            signals["point"] = float(getattr(symbol_info_mt5, "point"))
                except Exception:
                    pass

                # -- Injection NORMALISÉE des infos Bollinger/Micro-phase
                try:
                    boll = {
                        "bb_mid": (
                            float(latest.get("bb_mid"))
                            if pd.notna(latest.get("bb_mid"))
                            else None
                        ),
                        "bb_upper": (
                            float(latest.get("bb_upper"))
                            if pd.notna(latest.get("bb_upper"))
                            else None
                        ),
                        "bb_lower": (
                            float(latest.get("bb_lower"))
                            if pd.notna(latest.get("bb_lower"))
                            else None
                        ),
                        "is_range": (
                            bool(latest.get("is_range"))
                            if latest.get("is_range") is not None
                            else None
                        ),
                        "is_expansion": (
                            bool(latest.get("is_expansion"))
                            if latest.get("is_expansion") is not None
                            else None
                        ),
                        "mid_entry": (
                            str(latest.get("mid_entry")).lower()
                            if latest.get("mid_entry") is not None
                            else None
                        ),
                        "entry_gate_ok": (
                            bool(latest.get("entry_gate_ok"))
                            if latest.get("entry_gate_ok") is not None
                            else None
                        ),
                        "mid_distance_ratio": (
                            float(latest.get("mid_distance_ratio"))
                            if latest.get("mid_distance_ratio") is not None
                            and pd.notna(latest.get("mid_distance_ratio"))
                            else None
                        ),
                    }
                    # Nettoyage léger: ne garder que les clés non-None
                    boll = {k: v for k, v in boll.items() if v is not None}
                    if boll:
                        signals["boll"] = boll
                        signals["m1_boll"] = dict(
                            boll
                        )  # alias compatible avec les lecteurs alternatifs
                except Exception:
                    # on n'interrompt pas le cycle si une clé manque
                    pass

                # -- Phase & score de confiance (cohérence décisionnelle)
                try:
                    if "phase" not in signals and latest.get("phase") is not None:
                        signals["phase"] = str(latest.get("phase"))
                    if (
                        "confidence_score" not in signals
                        and latest.get("confidence_score") is not None
                    ):
                        signals["confidence_score"] = float(
                            latest.get("confidence_score")
                        )
                except Exception:
                    pass

                # -- Détection Big Reversal Candle (nouvelle règle)
                try:
                    br_cfg = (base_config.get("scalping") or {}).get("big_reversal", {})
                    if br_cfg.get("enabled", False):
                        br_signals = (
                            phase_observer.detectors.detect_big_reversal_candle(
                                annotated_rates_df,
                                min_body_ratio=float(
                                    br_cfg.get("min_body_ratio", 0.65)
                                ),
                                min_size_mult=float(br_cfg.get("min_size_mult", 2.5)),
                            )
                        )
                        if br_signals and br_signals[-1]:
                            signals["big_reversal"] = br_signals[
                                -1
                            ]  # on garde la dernière bougie détectée
                except Exception as e:
                    logger.warning(f"[{asset}] Big Reversal detection skipped: {e}")

                # -- Injection d'un spread en points ROBUSTE (évite les "inf")
                try:
                    spread_pts = None
                    if hasattr(mt5_connector, "get_symbol_spread_points"):
                        spread_pts = mt5_connector.get_symbol_spread_points(asset)

                    if not isinstance(spread_pts, (int, float)) or spread_pts <= 0:
                        # fallback 1: attribut spread du symbole s'il est >0
                        sp_attr = (
                            float(getattr(symbol_info_mt5, "spread", 0) or 0.0)
                            if symbol_info_mt5
                            else 0.0
                        )
                        if sp_attr > 0:
                            spread_pts = sp_attr
                        else:
                            # fallback 2: recalcul via ask/bid / point
                            point = (
                                float(getattr(symbol_info_mt5, "point", 0.0) or 0.0)
                                if symbol_info_mt5
                                else 0.0
                            )
                            if point > 0 and hasattr(
                                mt5_connector, "get_current_price"
                            ):
                                ask = mt5_connector.get_current_price(asset, "BUY")
                                bid = mt5_connector.get_current_price(asset, "SELL")
                                if (
                                    isinstance(ask, (int, float))
                                    and isinstance(bid, (int, float))
                                    and ask > bid > 0
                                ):
                                    spread_pts = (ask - bid) / point

                    if not isinstance(spread_pts, (int, float)) or spread_pts <= 0:
                        spread_pts = float("inf")

                    signals["current_spread_points"] = float(spread_pts)
                except Exception:
                    signals["current_spread_points"] = float("inf")

                all_assets_trading_signals[asset] = signals
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
            # DIAG: attache un tracker au contexte du cycle
            global_context["diag_tracker"] = DiagnosticTracker(cycle_count)
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

        # ✅ Sécuriser l'accès même si decision_package == None
        decision_package = decision_pipeline.institutional_decision_pipeline(
            global_context
        )
        decision_package = decision_package or {}
        active_config = decision_package.get("config_used", base_config) or base_config
        trade_decision = decision_package.get("final_decision", {}) or {}

        # === Compteurs de trade ===
        risk_cfg = active_config.get("risk_management") or {}
        max_trades_per_day = int(risk_cfg.get("max_trades_per_day", 999))
        max_trades_per_asset = int(risk_cfg.get("max_trades_per_asset_per_day", 999))

        if "trade_counters" not in global_context:
            global_context["trade_counters"] = {
                "daily_total": daily_trade_count,
                "per_asset": {},
            }

        asset_name = trade_decision.get("asset")
        if asset_name:
            asset_count = global_context["trade_counters"]["per_asset"].get(
                asset_name, 0
            )

            if global_context["trade_counters"]["daily_total"] >= max_trades_per_day:
                logger.warning(
                    f"Limite journalière {max_trades_per_day} atteinte -> PAS DE TRADE"
                )
                return False

            if asset_count >= max_trades_per_asset:
                logger.warning(
                    f"Limite journalière atteinte pour {asset_name} ({max_trades_per_asset}) -> PAS DE TRADE"
                )
                return False

        # ---- Enrichissement Katana pour l'exécution/audit ----
        exec_ctx = (
            decision_package.get("execution_context")
            or global_context.get("execution_context")
            or {}
        )
        chosen_asset = trade_decision.get("asset")
        if chosen_asset:
            # spread pips pour l'actif choisi (si connu)
            sp_map = exec_ctx.get("spreads_pips", {}) or {}
            trade_decision["meta_spread_pips"] = sp_map.get(chosen_asset)
            # snapshot katana pour l'actif choisi (si existant)
            snap_map = exec_ctx.get("katana_snapshots", {}) or {}
            chosen_snap = snap_map.get(chosen_asset, {})
            # métriques utiles pour audit/exécution
            trade_decision.setdefault(
                "meta_atr_m1_pips", chosen_snap.get("atr_m1_pips")
            )
            trade_decision.setdefault(
                "meta_katana_score", chosen_snap.get("katana_score")
            )

            # Exposer un contexte d'exécution au TradeExecutor (pour audit_logger)
            try:
                trade_executor.execution_context = {
                    "signals_snapshot": (
                        global_context.get("trading_signals", {}) or {}
                    ).get(chosen_asset, {}),
                    "katana_snapshot": chosen_snap,
                    "market_metrics": {
                        "atr_m1_pips": trade_decision.get("meta_atr_m1_pips"),
                    },
                    "account_info": global_context.get("account_info", {}),
                }
            except Exception:
                pass

        # réinjecter la décision enrichie dans le package
        decision_package["final_decision"] = trade_decision
        decision_package.setdefault("execution_context", exec_ctx)

        # Sorties partielles si positions ouvertes
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

        # Limite journalière
        if daily_trade_count >= int(
            (active_config or {}).get("max_trades_per_day", 999)
        ):
            logger.warning("Limite de trades quotidiens atteinte.")
            return trade_executed_successfully

        # Exécution d'entrée
        if trade_decision and str(trade_decision.get("action", "")).upper() in [
            "BUY",
            "SELL",
        ]:
            logger.info(
                f"EXÉCUTION: {trade_decision.get('action')} {trade_decision.get('asset')}"
            )
            feedback = run_trade_execution_pipeline(trade_executor, decision_package)
            if feedback and feedback.get("status") == "executed":
                trade_executed_successfully = True

                # ✅ Incrémenter les compteurs
            global_context["trade_counters"]["daily_total"] += 1
            if asset_name:
                global_context["trade_counters"]["per_asset"][asset_name] = (
                    global_context["trade_counters"]["per_asset"].get(asset_name, 0) + 1
                )

        else:
            regime = (decision_package.get("context", {}) or {}).get(
                "current_market_regime", "inconnu"
            )
            logger.info(f"Aucune opportunité. Régime: {regime}.")

    except Exception as e:
        logger.error(f"Erreur pipeline: {e}", exc_info=True)
        trade_executed_successfully = False
    finally:
        # DIAG: imprime le résumé des blocages / sélections AVANT le log de fin de cycle
        try:
            get_tracker_from_context(global_context).emit_summary(
                logging.getLogger(__name__)
            )
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
                phase_observer,
                config_manager.decision_pipeline,
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
