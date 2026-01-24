#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
orchestration/pipeline_helpers.py - Fonctions d'aide pour le cycle de pipeline

Contient les helpers utilisés par run_single_pipeline_cycle:
- _deep_merge_dicts(): Merge récursif de dictionnaires
- _get_merged_config_for_asset(): Fusionne config globale + config asset
- _is_market_closed(): Vérifie si le marché est fermé
- _build_asset_trading_signals(): Construit les signaux de trading
- _build_asset_market_data(): Construit les données de marché
- _build_global_context(): Assemble le contexte global
- _load_po_config_safe(): Charge la config PhaseObserver
- _execute_single_decision(): Exécute une décision de trading
"""

import json
import logging
from datetime import datetime, UTC
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd

from core.config_manager import ConfigManager
from mt5_connector import MT5Connector


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

    16 JAN 2026: Utilise maintenant ConfigMerger (core/config_merge.py)
    au lieu du merge manuel précédent.
    """
    logger = logging.getLogger(__name__)

    # Utiliser le nouveau ConfigMerger
    try:
        merged_config = config_manager.get_merged_config(asset, "scalping")
        if merged_config:
            # Ajouter asset_symbol et conserver strategy_name de active_config
            merged_config["asset_symbol"] = asset
            if "strategy_name" in active_config:
                merged_config["strategy_name"] = active_config["strategy_name"]
            logger.debug(f"[{asset}] Config fusionnée via ConfigMerger")
            return merged_config
    except Exception as e:
        logger.warning(f"[{asset}] ConfigMerger erreur: {e}, fallback sur merge manuel")

    # Fallback: merge simple si ConfigMerger échoue
    merged_config = dict(active_config or {})
    merged_config["asset_symbol"] = asset
    if "strategy_name" in active_config:
        merged_config["strategy_name"] = active_config["strategy_name"]

    asset_specific_config = config_manager.load_asset_config(asset) or {}
    for section in ["entry_rules", "overrides", "risk_management", "volatility"]:
        asset_section = asset_specific_config.get(section)
        if asset_section is not None:
            merged_config[section] = _deep_merge_dicts(
                merged_config.get(section, {}), asset_section
            )

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
        # FIX (02 JAN 2026): Utiliser decision_package["market_context"] au lieu de global_context
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
