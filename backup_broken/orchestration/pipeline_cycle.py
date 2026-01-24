#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
orchestration/pipeline_cycle.py - Cycle principal du pipeline de trading

Contient run_single_pipeline_cycle() qui exécute un cycle complet du pipeline
de trading de SNIPER_X.

Architecture:
  - MarketAnalyzer (PhaseObserver + PatternEngine) → annotated_df + latest
  - Analyses d'entrée pour la fusion: Footprint M1 (ticks bougie) + Orderflow v5
  - Signals consolidés par actif
  - FusionManager → décisions scalping (USDJPY uniquement)
  - Fast-lane d'exécution si décision Fusion valide (avec gardes panier/slippage)
  - decision_pipeline pour le reste (ex: Liquidity), mais scalping filtré Fusion-only
  - SLTP dynamique (trailing-only pour scalping), maintenance périodique
"""

import json
import logging
import math
import re
import time
from datetime import datetime, UTC
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from core.config_manager import ConfigManager
from core.decision_pipeline import DecisionPipeline
from core.diagnostics import get_tracker_from_context
from core.strategy_manager import StrategyManager
from mecanique_generale.mecano import Mecano
from mt5_connector import MT5Connector
from phase_observer.market_analyzer import MarketAnalyzer
from phase_observer.timing_analyzer import evaluate_trading_conditions
from trader.trade_executor import TradeExecutor, run_trade_execution_pipeline

from orchestration.pipeline_helpers import (
    _deep_merge_dicts,
    _get_merged_config_for_asset,
    _is_market_closed,
    _build_asset_trading_signals,
    _build_asset_market_data,
    _build_global_context,
    _load_po_config_safe,
    _execute_single_decision,
)


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
    logger = logging.getLogger(__name__)
    trade_executed_successfully = False
    global_context: Dict[str, Any] = {}

    # === Moteurs d'analyse ===
    market_analyzer = MarketAnalyzer(config_manager=config_manager, logger=logger)

    # DÉSACTIVÉ (25 DEC 2025): FusionManager - Architecture minimaliste OrderFlow seul
    _fusion_mgr = None

    # --- Fallback vote simple (jamais utilisé si REQUIRE_FUSION_MGR=True) ---
    def _quick_vote_fusion(
        signals: dict, latest: dict, base_cfg: dict, sym: str, mt5c: MT5Connector
    ):
        """
        Vote rapide (secours) en l'absence de décision FusionManager.
        """
        try:
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

            spread = _safe_float(signals.get("current_spread_points"), default=float("nan"))
            phase = str(signals.get("phase", "neutral") or "neutral").lower()
            conf = _safe_float(signals.get("confidence_score"), default=0.0)

            fp = (signals.get("footprint_summary") or latest.get("footprint_summary") or {}) or {}
            of = (signals.get("orderflow_summary") or latest.get("orderflow_summary") or {}) or {}

            sym_spread_max = {"NAS100": 80.0, "GBPUSD": 18.0, "USDJPY": 40.0}.get(str(sym).upper(), 999.0)

            m1_min_ticks = int(_dig(base_cfg, ["entry_rules", "scalping", "footprint", "m1_min_ticks"], 30))
            m1_min_cov_s = int(_dig(base_cfg, ["entry_rules", "scalping", "footprint", "m1_min_coverage_s"], 8))
            tickrate_min = _safe_float(_dig(base_cfg, ["entry_rules", "scalping", "footprint", "tickrate_min"], 1.5), 1.5)
            of_delta_min = _safe_float(_dig(base_cfg, ["entry_rules", "scalping", "orderflow", "delta_abs_min"], 30.0), 30.0)
            ttl_ms = int(_dig(base_cfg, ["entry_rules", "scalping", "fusion", "ttl_ms"], 1500))
            slippage_pts = _safe_float(_dig(base_cfg, ["entry_rules", "scalping", "fusion", "max_slippage_points"], 20.0), 20.0)
            allow_degraded = bool(_dig(base_cfg, ["entry_rules", "scalping", "fusion", "allow_degraded_vote"], True))
            degr_min_of_abs = _safe_float(_dig(base_cfg, ["entry_rules", "scalping", "fusion", "degraded_vote_conditions", "min_of_delta_abs"], 150.0), 150.0)
            degr_min_of_sc = _safe_float(_dig(base_cfg, ["entry_rules", "scalping", "fusion", "degraded_vote_conditions", "min_of_score"], 15.0), 15.0)

            ticks = int(_safe_float(fp.get("tick_count"), 0))
            cov = _safe_float(fp.get("coverage_s"), 0.0)
            tr = _safe_float(fp.get("tick_rate"), 0.0)
            dlt = _safe_float(of.get("delta_total"), 0.0)
            of_sc = _safe_float(latest.get("orderflow_score"), 0.0)
            fp_dir_raw = _safe_float(fp.get("delta_total"), 0.0)

            fp_ok = True
            of_strong = (abs(dlt) >= degr_min_of_abs) or (of_sc >= degr_min_of_sc)

            vote, trig_dir = 0, "NEUTRAL"
            if ("bull" in phase) or ("up" in phase):
                vote += 1
                trig_dir = "BUY"
            elif ("bear" in phase) or ("down" in phase):
                vote -= 1
                trig_dir = "SELL"

            ar = fp.get("aggressor_ratio", None)
            if isinstance(ar, (int, float)):
                fp_dir = "BUY" if ar > 0.5 else ("SELL" if ar < 0.5 else "NEUTRAL")
            else:
                bt = fp.get("buy_ticks") or fp.get("buy_count")
                st = fp.get("sell_ticks") or fp.get("sell_count")
                if isinstance(bt, (int, float)) and isinstance(st, (int, float)):
                    fp_dir = "BUY" if bt > st else ("SELL" if st > bt else "NEUTRAL")
                else:
                    fp_dir = "BUY" if _safe_float(fp.get("delta_total"), 0.0) > 0 else ("SELL" if _safe_float(fp.get("delta_total"), 0.0) < 0 else "NEUTRAL")

            dlt = _safe_float(of.get("delta_total"), 0.0)
            of_dir = "BUY" if dlt > 0 else ("SELL" if dlt < 0 else "NEUTRAL")

            if of_dir in {"BUY", "SELL"}:
                action = of_dir
            elif fp_dir in {"BUY", "SELL"}:
                action = fp_dir
            elif trig_dir in {"BUY", "SELL"}:
                action = trig_dir
            else:
                action = "BUY" if conf >= 0.5 else "SELL"

            price = None
            try:
                tkfun = getattr(mt5c, "get_symbol_tick", None)
                if callable(tkfun):
                    t = tkfun(sym)
                    ask = t.get("ask") if isinstance(t, dict) else getattr(t, "ask", None)
                    bid = t.get("bid") if isinstance(t, dict) else getattr(t, "bid", None)
                else:
                    t = getattr(getattr(mt5c, "mt5", None), "symbol_info_tick", None)
                    t = t(sym) if callable(t) else None
                    ask = getattr(t, "ask", None)
                    bid = getattr(t, "bid", None)
                price = float(ask if action == "BUY" else bid) if (ask and bid) else None
            except Exception:
                price = None

            denom_delta = max(of_delta_min, 1.0)
            fp_strength = 0.0
            try:
                fp_strength = min(1.0, max(0.0, (abs(fp_dir_raw) / denom_delta) * 0.75 + (tr / max(tickrate_min, 0.1)) * 0.25))
            except Exception:
                fp_strength = 0.0

            denom_of = max(max(of_delta_min, degr_min_of_abs), 1.0)
            of_strength = min(1.0, max(0.0, abs(dlt) / denom_of))

            align_bonus = 0.1 if (trig_dir == fp_dir == of_dir and trig_dir in {"BUY", "SELL"}) else 0.0
            score = min(1.0, 0.25 * conf + 0.35 * fp_strength + 0.40 * of_strength + align_bonus)

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
                    "triggers_dir": "BUY" if ("bull" in phase or "up" in phase) else ("SELL" if ("bear" in phase or "down" in phase) else "NEUTRAL"),
                    "fp_dir": fp_dir,
                    "of_dir": of_dir,
                    "fp": {"ticks": ticks, "cov_s": cov, "tickrate": tr, "delta_total": fp_dir_raw},
                    "of": {"delta_total": dlt, "score": of_sc},
                    "footprint_ok": bool(fp_ok),
                    "degraded_used": bool(allow_degraded and (not fp_ok) and of_strong),
                },
                "slippage_guard_points": float(slippage_pts),
                "ts_created": pd.Timestamp.utcnow().value // 1_000_000,
            }

        except Exception as e:
            return {"ok": False, "reason": f"fusion_error:{e}"}

    def _scale100(x):
        try:
            v = float(x)
            return v * 100.0 if 0.0 <= v <= 1.0 else v
        except Exception:
            return None

    def _mk_fusion_inputs(signals: dict, latest: dict, symbol_info, mt5c: MT5Connector, sym: str, footprint_trigger: Optional[Dict] = None):
        of_score = _scale100(latest.get("orderflow_score"))
        fp_score = _scale100(latest.get("footprint_score"))

        of_summary = latest.get("orderflow_summary") or {}
        fp_summary = latest.get("footprint_summary") or {}

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

        if footprint_trigger:
            triggers = footprint_trigger
        else:
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
                "direction": trig_dir,
                "confidence": float(signals.get("confidence_score", 0.5) or 0.5),
                "anchor_price": anchor,
                "trigger_type": "fusion_pretrigger",
            }

        ctx = {
            "now_ts": time.time(),
            "spread_points": float(signals.get("current_spread_points", float("nan"))),
            "regime": str(signals.get("phase", "")) or None,
        }

        if latest is not None:
            try:
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

        step = getattr(symbol_info, "point", None) if symbol_info else None
        strat_cfg = {"price_step": float(step) if isinstance(step, (int, float)) else 0.01}

        try:
            scalping_full_config = strategy_manager.get_strategy_config("scalping") or {}

            if "fusion" in scalping_full_config:
                strat_cfg["fusion"] = scalping_full_config["fusion"]

            if "scoring_thresholds" in scalping_full_config:
                strat_cfg["scoring_thresholds"] = scalping_full_config["scoring_thresholds"]

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

        LOG_FUSION_ONLY: bool = True
        REQUIRE_FUSION_MGR: bool = False
        FUSION_ASSETS = {"USDJPY"}

        def _fusion_applies(asset: str) -> bool:
            return asset.upper() in FUSION_ASSETS

        logger.info(
            f"[EXEC MODE] is_dry_run={bool(base_config.get('trade_execution', {}).get('dry_run', is_dry_run))} | execution_mode={execution_mode}"
        )
        is_dry_run = bool(base_config.get("trade_execution", {}).get("dry_run", is_dry_run))

        if cycle_count == 1:
            def _dig(d, path):
                cur = d or {}
                for k in path:
                    if not isinstance(cur, dict):
                        return None
                    cur = cur.get(k)
                return cur

            g_burst = _dig(base_config, ["entry_rules", "scalping", "burst_scalping", "burst_size"])
            try:
                strat_conf = strategy_manager.get_strategy_config("scalping") or {}
                s_burst = _dig(strat_conf, ["entry_rules", "scalping", "burst_scalping", "burst_size"])
            except Exception:
                s_burst = None
            try:
                xa = config_manager.load_asset_config("USDJPY") or {}
            except Exception:
                xa = {}
            xa_entry = _dig(xa, ["entry_rules", "scalping", "burst_scalping", "burst_size"])
            xa_override = _dig(xa, ["overrides", "scalping", "entry_rules", "scalping", "burst_scalping", "burst_size"])
            xa_legacy = _dig(xa, ["overrides", "scalping", "burst", "burst_size"])
            logger.critical(
                f"[CFG@BOOT] burst_size global={g_burst} | strategy={s_burst} | USDJPY.entry={xa_entry} | USDJPY.override={xa_override} | USDJPY.legacy={xa_legacy}"
            )

        active_mt5_account_details = config_manager.get_mt5_account_credentials(mode=execution_mode)

        CANDLES_ENABLED = False

        global_safety = base_config.get("global_safety", {}) or {}
        all_symbols = list(global_safety.get("global_allowed_symbols", []))
        account_allowed = set((active_mt5_account_details or {}).get("allowed_symbols", []))
        tradeable_assets = [a for a in all_symbols if a in account_allowed] if account_allowed else all_symbols

        if REQUIRE_FUSION_MGR and (_fusion_mgr is None):
            if "USDJPY" in {a.upper() for a in tradeable_assets}:
                logger.critical("[FUSION] FusionManager requis pour le scalping USDJPY — USDJPY retiré du cycle (aucun fallback).")
                tradeable_assets = [a for a in tradeable_assets if a.upper() != "USDJPY"]

        if excluded_symbols:
            excluded_upper = {s.upper() for s in excluded_symbols}
            tradeable_assets = [a for a in tradeable_assets if a.upper() not in excluded_upper]
            if excluded_symbols:
                logger.info(f"🔒 [PIPELINE] Symboles exclus: {excluded_symbols}")

        logger.info(f"🎯 [PIPELINE] Assets tradables: {tradeable_assets}")
        if not tradeable_assets:
            logger.warning("Aucun actif à trader pour ce cycle. Cycle ignoré.")
            return False

        all_assets_market_data: Dict[str, pd.DataFrame] = {}
        all_assets_trading_signals: Dict[str, Dict[str, Any]] = {}
        fusion_scalping_decisions: List[Dict[str, Any]] = []

        dcfg = base_config.get("data_collection", {}) or {}
        timeframe_str = dcfg.get("default_timeframe", "M1")
        bars_to_fetch = int(dcfg.get("default_bars_count", 200))
        min_required_bars = 50

        for asset in tradeable_assets:
            logger.debug(f"📊 [PIPELINE] Analyse de {asset}...")
            try:
                rates_df = mt5_connector.get_rates(asset, timeframe_str, bars_to_fetch)
                if (rates_df is None) or rates_df.empty or len(rates_df) < min_required_bars:
                    logger.warning(f"Données insuffisantes pour {asset}. Actif ignoré.")
                    continue

                symbol_info_mt5 = mt5_connector.get_symbol_info(asset)
                if symbol_info_mt5:
                    rates_df["point"] = getattr(symbol_info_mt5, "point", 0.0)
                    rates_df["spread"] = getattr(symbol_info_mt5, "spread", 0)

                if (market_analyzer.phase_observer._history_df is None) or market_analyzer.phase_observer._history_df.empty:
                    market_analyzer.phase_observer.load_initial_history(rates_df.copy())

                rolling_lookback = int(dcfg.get("rolling_lookback_bars", 50))
                full_refresh_bars = int(dcfg.get("full_refresh_bars", 200))
                recalib_n = int(dcfg.get("recalibration_every_n_cycles", 10))
                do_full_refresh = (cycle_count == 1) or (recalib_n > 0 and cycle_count % recalib_n == 0)
                subset_df = rates_df.tail(full_refresh_bars if do_full_refresh else rolling_lookback).copy()

                ticks_df = None

                if asset.upper() == "USDJPY":
                    logger.info(f"[TICKS][DEBUG] Tentative récupération ticks pour {asset}...")
                    try:
                        if subset_df is not None and not subset_df.empty:
                            sliding_window_seconds = 8

                            now_utc = pd.Timestamp.utcnow()
                            tick_window_start = now_utc - pd.Timedelta(seconds=sliding_window_seconds)
                            tick_window_end = now_utc

                            logger.info(
                                f"[{asset}] 🔄 Chargement ticks [fenêtre glissante {sliding_window_seconds}s] | "
                                f"[{tick_window_start.strftime('%H:%M:%S')} → {tick_window_end.strftime('%H:%M:%S')}]"
                            )

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
                                logger.info(f"[{asset}] ✅ {len(ticks_df)} ticks chargés (fenêtre glissante {sliding_window_seconds}s)")
                            else:
                                logger.warning(f"[{asset}] ⚠️ Aucun tick dans la fenêtre glissante {sliding_window_seconds}s")
                        else:
                            logger.warning(f"[{asset}] ⚠️ subset_df vide, fenêtre glissante non calculée")
                    except Exception as e:
                        logger.error(f"[TICKS] ❌ Erreur récupération ticks {asset}: {e}")
                else:
                    logger.info(f"[TICKS] ✅ {asset} utilise stratégie liquidité → Ticks skip")

                market_results = market_analyzer.analyze(asset, subset_df, ticks=ticks_df)
                footprint_trigger_result = None

                if cycle_count == 1 or do_full_refresh:
                    market_analyzer.phase_observer.load_initial_history(subset_df.copy())
                elif hasattr(market_analyzer.phase_observer, "update_with_new_data"):
                    market_analyzer.phase_observer.update_with_new_data(subset_df.copy())

                logger.debug(f"[{asset}] MarketAnalyzer → keys={list(market_results.keys())}")

                annotated_rates_df = market_results["annotated_df"]
                if annotated_rates_df is None or annotated_rates_df.empty:
                    logger.warning(f"[{asset}] MarketAnalyzer → DF vide. Actif ignoré.")
                    continue

                latest = market_results["latest"]
                logger.info(f"[MarketAnalyzer] Actif: {asset} | Phase: {market_results.get('phase', 'N/A')}")

                try:
                    use_idx = -2 if len(annotated_rates_df) >= 2 else -1
                    candle_row = annotated_rates_df.iloc[use_idx]

                    if "time" in annotated_rates_df.columns:
                        start_ts = pd.to_datetime(candle_row["time"], utc=True, errors="coerce")
                    else:
                        start_ts = pd.to_datetime(candle_row.name, utc=True, errors="coerce")
                    if pd.isna(start_ts):
                        start_ts = pd.Timestamp.utcnow()
                    end_ts = start_ts + pd.Timedelta(minutes=1)

                    ticks_df = mt5_connector.get_ticks_for_candle(asset, start_ts.to_pydatetime(), end_ts.to_pydatetime())

                    use_idx = -1
                    if ticks_df is None or ticks_df.empty:
                        last_candle = annotated_rates_df.iloc[-1]
                        if "time" in annotated_rates_df.columns:
                            live_start = pd.to_datetime(last_candle["time"], utc=True, errors="coerce")
                        else:
                            live_start = pd.to_datetime(last_candle.name, utc=True, errors="coerce")
                        if pd.isna(live_start):
                            live_start = pd.Timestamp.utcnow()
                        live_end = live_start + pd.Timedelta(minutes=1)
                        ticks_df = mt5_connector.get_ticks_for_candle(asset, live_start.to_pydatetime(), live_end.to_pydatetime())

                    if "time" in annotated_rates_df.columns:
                        annotated_rates_df["time"] = pd.to_datetime(annotated_rates_df["time"], utc=True, errors="coerce")

                except Exception as e:
                    logger.error(f"Erreur collecte données {asset}: {e}", exc_info=True)

                try:
                    last_candles_df = annotated_rates_df.tail(5).copy()
                    if "time" in last_candles_df.columns:
                        last_candles_df["time"] = pd.to_datetime(last_candles_df["time"], utc=True, errors="coerce")
                        last_candles_df.set_index("time", inplace=True)

                    latest = dict(latest)

                except Exception as e:
                    logger.error(f"[LIQUIDITY][{asset}] Erreur traitement latest: {e}", exc_info=True)

                signals: Dict[str, Any] = _build_asset_trading_signals(
                    latest, symbol_info_mt5, asset=asset, mt5_connector=mt5_connector
                ) or {}

                signals["__latest__"] = latest

                signals["phase"] = market_results.get("phase", signals.get("phase", "neutral"))
                signals["confidence_score"] = market_results.get("confidence", signals.get("confidence_score", 0.5))
                signals["structure"] = market_results.get("structure", {})

                spread_pts = getattr(symbol_info_mt5, "spread", None) if symbol_info_mt5 else None
                if not spread_pts or spread_pts <= 0:
                    try:
                        tk = mt5_connector.get_symbol_tick(asset)
                        ask = tk.get("ask") if isinstance(tk, dict) else getattr(tk, "ask", None)
                        bid = tk.get("bid") if isinstance(tk, dict) else getattr(tk, "bid", None)
                        pt = getattr(symbol_info_mt5, "point", 0.0) if symbol_info_mt5 else 0.0
                        if ask and bid and pt:
                            spread_pts = abs(float(ask) - float(bid)) / float(pt)
                        else:
                            spread_pts = None
                    except Exception:
                        spread_pts = None
                signals["current_spread_points"] = float(spread_pts) if isinstance(spread_pts, (int, float)) else float("nan")

                signals["footprint_summary"] = latest.get("footprint_summary")
                signals["orderflow_summary"] = latest.get("orderflow_summary")

                all_assets_trading_signals[asset] = signals
                all_assets_market_data[asset] = _build_asset_market_data(annotated_rates_df, symbol_info_mt5)

                # PIPELINE MINIMALISTE (25 DEC 2025)
                try:
                    if _fusion_applies(asset):
                        timing_verdict = None
                        try:
                            ticks_for_timing = ticks_df if ticks_df is not None and not ticks_df.empty else None

                            scalping_config_global = strategy_manager.get_strategy_config("scalping") if strategy_manager else {}
                            asset_config_timing = config_manager.load_asset_config(asset)

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

                        if timing_verdict and timing_verdict.get("verdict") != "PASS":
                            logger.info(f"[TIMING_VETO][{asset}] {timing_verdict.get('veto_reason', 'Unknown')} - Skip trade cycle")
                            continue

                        orderflow_result = {
                            "score": latest.get("orderflow_score", 0.0),
                            "bias": latest.get("orderflow_bias", "NEUTRAL"),
                            "summary": latest.get("orderflow_summary", {})
                        }

                        logger.info(f"[ORDERFLOW][{asset}] score={orderflow_result['score']:.1f}/100 | bias={orderflow_result['bias']}")

                        try:
                            asset_min_score = 70.0
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

                            decision = market_analyzer.build_decision(orderflow_result=orderflow_result, min_score=asset_min_score)

                            logger.info(
                                f"[DECISION][{asset}] action={decision['action']} | "
                                f"confidence={decision['confidence']:.2f} | "
                                f"rationale={decision['rationale']}"
                            )
                        except Exception as e_decision:
                            logger.error(f"[DECISION][{asset}] Erreur build_decision: {e_decision}", exc_info=True)
                            decision = {"action": "HOLD", "confidence": 0.0, "rationale": f"Decision error: {e_decision}"}

                        if decision["action"] in ["BUY", "SELL"]:
                            anchor_price = decision.get("anchor_price") or latest.get("current_price") or latest.get("close")

                            scalping_cfg_entry = base_config.get("entry_rules", {}).get("scalping", {})
                            decision_cfg = scalping_cfg_entry.get("decision", {})
                            ttl_ms = int(decision_cfg.get("validity_ms", 800))
                            slippage_pts = float(decision_cfg.get("max_spread_pts", 15))

                            fdec = {
                                "ok": True,
                                "action": decision["action"],
                                "fused_confidence": decision["confidence"],
                                "score": decision["confidence"] * 100.0,
                                "price": anchor_price,
                                "ttl_ms": ttl_ms,
                                "slippage_guard_points": slippage_pts,
                                "ts_created": pd.Timestamp.utcnow().value // 1_000_000,
                                "signal_type": "MINIMALIST_ORDERFLOW",
                                "rationale": decision["rationale"],
                                "orderflow_score": orderflow_result["score"],
                                "timing_quality": timing_verdict.get("quality_metrics", {})
                            }

                            fusion_scalping_decisions.append({
                                "rule_name": "minimalist_scalping",
                                "action": fdec["action"],
                                "asset": asset,
                                "price": fdec.get("price"),
                                "confidence": fdec.get("fused_confidence", 0.0),
                                "no_fallback": True,
                                "entry_style": "MARKET",
                                "validity_ms": int(fdec.get("ttl_ms", 800)),
                                "slippage_guard_points": float(fdec.get("slippage_guard_points", 15.0)),
                                "ts_created": int(fdec.get("ts_created")),
                                "fusion_data": fdec,
                                "fusion_full": fdec
                            })

                            logger.info(
                                f"[MINIMALIST][{asset}] ✅ {fdec['action']} | "
                                f"score={fdec['score']:.1f}/100 | "
                                f"OF={orderflow_result['score']:.1f} | "
                                f"price={fdec.get('price')} | "
                                f"rationale={fdec.get('rationale', 'N/A')}"
                            )
                        else:
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
        global_context["diag_tracker"] = get_tracker_from_context(global_context)
        logger.debug("✅ [PIPELINE] Contexte global construit avec succès !")

        # FAST-LANE
        try:
            filtered_fusion_decisions = []
            for fd in fusion_scalping_decisions:
                sym = str(fd.get("asset", "")).upper()
                confidence = float(fd.get("confidence", 0.0))

                try:
                    aconf = config_manager.load_asset_config(sym) or {}
                    entry_threshold = float(
                        ((aconf.get("entry_rules", {}) or {}).get("scalping", {}).get("burst_scalping", {}).get("entry_threshold", 0.70)) or 0.70
                    )
                except Exception:
                    entry_threshold = 0.70

                if confidence >= entry_threshold:
                    filtered_fusion_decisions.append(fd)
                else:
                    logger.info(f"⛔ [FUSION][FAST-LANE] {sym} signal rejeté (confidence={confidence:.1%} < seuil={entry_threshold:.1%})")

            if filtered_fusion_decisions:
                _positions_cache = None

                def _open_burst_ids():
                    nonlocal _positions_cache
                    try:
                        if _positions_cache is None:
                            _positions_cache = mt5_connector.get_positions() or []
                        ids = set()
                        for p in _positions_cache:
                            c = (p.get("comment") if isinstance(p, dict) else getattr(p, "comment", "")) or ""
                            m = re.search(r"bs_([a-f0-9]{8})", str(c))
                            if m:
                                ids.add(m.group(1))
                        return ids
                    except Exception:
                        return set()

                burst_cfg = (base_config.get("entry_rules", {}).get("scalping", {}).get("burst_scalping", {}) or {})
                guard_cfg = burst_cfg.get("burst_guardrails", {}) or {}
                enforce_closure = bool(guard_cfg.get("enforce_burst_closure", True))
                single_burst_global = bool(guard_cfg.get("single_burst_global", True))

                if enforce_closure and single_burst_global and _open_burst_ids():
                    logger.info("⛔ [FUSION][FAST-LANE] Panier actif détecté → fast-lane annulée.")
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
                        slipp_pts = float(best.get("slippage_guard_points", 10.0) or 0.0)
                        ok_price = True
                        try:
                            ref = float(best.get("price") or 0.0)
                            if ref > 0.0 and hasattr(mt5_connector, "get_symbol_tick"):
                                t = mt5_connector.get_symbol_tick(sym)
                                ask = t.get("ask") if isinstance(t, dict) else getattr(t, "ask", None)
                                bid = t.get("bid") if isinstance(t, dict) else getattr(t, "bid", None)
                                now_price = float(ask if side == "BUY" else bid)
                                pts = abs(now_price - ref) / getattr(mt5_connector.get_symbol_info(sym), "point", 1.0)
                                if pts > slipp_pts:
                                    ok_price = False
                                    logger.info(f"[FUSION][FAST-LANE] slippage guard: ref={ref} now={now_price} pts>{slipp_pts} → abort.")
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

                                prefer_asset = bool(((base_config.get("entry_rules", {}) or {}).get("scalping", {}) or {}).get("burst_scalping", {}).get("prefer_asset_overrides", False))
                                try:
                                    aconf = config_manager.load_asset_config(sym_) or {}
                                except Exception:
                                    aconf = {}

                                scalping_strat_cfg = strategy_manager.get_strategy_config("scalping") or {}
                                reads = [
                                    _dig(scalping_strat_cfg, ["entry_rules", "scalping", "burst_scalping", "burst_size"]),
                                    _dig(base_config, ["entry_rules", "scalping", "burst_scalping", "burst_size"]),
                                    _dig(aconf, ["entry_rules", "scalping", "burst_scalping", "burst_size"]),
                                    _dig(aconf, ["overrides", "entry_rules", "scalping", "burst_scalping", "burst_size"]),
                                    _dig(aconf, ["overrides", "scalping", "entry_rules", "scalping", "burst_scalping", "burst_size"]),
                                    _dig(base_config, ["trade_executor_settings", "entry_rules", "scalping", "burst_scalping", "burst_size"]),
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
                                "order": {"action": side, "side": side, "type": "MARKET", "symbol": sym},
                                "trade": {"action": side, "side": side},
                                "confidence": best.get("confidence"),
                                "fusion_data": best.get("fusion_full", {}),
                            }
                            safety = td.setdefault("safety", {})
                            ff = safety.setdefault("fat_finger", {})
                            ff.setdefault("policy", "FLOOR")

                            cache_key = f"_sltp_cfg_{sym}"
                            sltp_cfg = global_context.get(cache_key)

                            if sltp_cfg is None:
                                sltp_cfg = (((global_context.get("asset_configs", {}) or {}).get(sym, {}) or {}).get("entry_rules", {}) or {}).get("scalping", {}) or {}
                                sltp_cfg = (sltp_cfg.get("burst_scalping", {}) or {}).get("sltp", {}) or (base_config.get("entry_rules", {}).get("scalping", {}).get("burst_scalping", {}).get("sltp", {}) or {})
                                global_context[cache_key] = sltp_cfg

                            if sltp_cfg:
                                td["sltp"] = sltp_cfg

                            logger.info(f"[FUSION][FAST-LANE] {side} {sym} burst={resolved_burst} (market, trailing-only)")

                            try:
                                scalping_strategy_config = strategy_manager.get_strategy_config("scalping") or {}
                                merged_config = dict(base_config)
                                if "entry_rules" in scalping_strategy_config:
                                    merged_config.setdefault("entry_rules", {}).update(scalping_strategy_config["entry_rules"])
                            except Exception as e:
                                logger.warning(f"[FUSION][FAST-LANE] Fusion config échouée: {e}")
                                merged_config = base_config

                            decision_pkg = {
                                "final_decision": td,
                                "context": global_context,
                                "active_config": merged_config,
                            }
                            decision_pkg.setdefault("audit_context", {}).update({
                                "intent_symbol": td.get("symbol"),
                                "intent_side": td.get("side"),
                                "intent_burst": td.get("burst_size"),
                            })

                            res = run_trade_execution_pipeline(trade_executor, decision_pkg, is_dry_run=is_dry_run)
        except Exception as e:
            logger.warning(f"[FUSION][FAST-LANE] erreur: {e}")

        # Gestion fermetures SL/TP des paniers
        try:
            scalping_config = strategy_manager.get_strategy_config("scalping") or {}
            merged_config = dict(base_config)
            if "entry_rules" in scalping_config:
                merged_config.setdefault("entry_rules", {}).update(scalping_config["entry_rules"])

            closure_cfg = merged_config.get("entry_rules", {}).get("scalping", {}).get("burst_scalping", {}).get("closure_rules", {}) or {}

            # 🔧 FIX 23 JAN 2026: Passer config_manager_instance pour lecture per-asset
            trade_executor.monitor_burst_baskets(
                config=merged_config,
                max_loss_pips=float(closure_cfg.get("max_loss_pips", 90.0)),
                config_manager_instance=config_manager
            )
        except Exception as e:
            logger.error(f"❌ [BURST EXIT] Erreur CRITIQUE: {e}", exc_info=True)

        # Pipeline institutionnel
        logger.debug("🤖 [PIPELINE] Appel du decision_pipeline...")
        decision_package = decision_pipeline.institutional_decision_pipeline(global_context) or {}

        # Maintenance périodique SLTP dynamique
        try:
            scalping_config = strategy_manager.get_strategy_config("scalping") or {}
            merged_config = dict(base_config)
            if "entry_rules" in scalping_config:
                merged_config.setdefault("entry_rules", {}).update(scalping_config["entry_rules"])

            # 🔧 FIX 23 JAN 2026: Passer config_manager_instance pour lecture per-asset
            trade_executor.monitor_burst_baskets(
                config=merged_config,
                config_manager_instance=config_manager
            )
        except Exception as e:
            logger.error(f"❌ [BURST_MONITOR] Erreur CRITIQUE: {e}", exc_info=True)

        # Intégrer les décisions Fusion dans le package
        try:
            if fusion_scalping_decisions:
                decision_package.setdefault("scalping_decisions", [])
                decision_package["scalping_decisions"].extend(fusion_scalping_decisions)
                decision_package.setdefault("final_decision", decision_package.get("final_decision") or {})
                if not decision_package["final_decision"]:
                    now_ms = int(pd.Timestamp.utcnow().value // 1_000_000)

                    def _score_fd(fd):
                        age = max(0, now_ms - int(fd.get("ts_created", now_ms)))
                        ttl = int(fd.get("validity_ms", 800))
                        alive = 1 if age < ttl else 0
                        return (alive, float(fd.get("confidence", 0.0)))

                    best = sorted(fusion_scalping_decisions, key=_score_fd, reverse=True)[0]
                    decision_package["final_decision"] = best
                logger.info(f"[MERGE] {len(fusion_scalping_decisions)} décision(s) Fusion intégrée(s).")
        except Exception as e:
            logger.warning(f"[MERGE] Échec intégration décisions Fusion: {e}")

        # Logs décisions
        scalping_decisions = decision_package.get("scalping_decisions", []) or []
        liquidity_decisions = decision_package.get("liquidity_decisions", []) or []
        final_decisions = scalping_decisions + liquidity_decisions
        final = decision_package.get("final_decision", {}) or {}
        ctx_out = decision_package.get("context", {}) or {}

        if not scalping_decisions and not liquidity_decisions:
            logger.debug("📦 [PIPELINE] Aucune décision détectée.")
            logger.info("Aucun trade décidé ce cycle.")
            return False

        # Exécution Liquidity
        if liquidity_decisions:
            logger.debug("📦 [PIPELINE] Décisions Liquidity détectées:")
            for d in liquidity_decisions:
                logger.debug(f"   → {d.get('action')} {d.get('asset')} | vol={d.get('volume', 0)}")
            for td in liquidity_decisions:
                action = str(td.get("action", "")).upper()
                if action in {"BUY", "SELL"}:
                    res = _execute_single_decision(
                        td, trade_executor, mt5_connector, global_context, decision_package, execution_mode, logger
                    )
                    if res is None or res not in ("failed", False):
                        trade_executed_successfully = True

    except Exception as e:
        logger.error(f"Erreur pipeline: {e}", exc_info=True)
        trade_executed_successfully = False
    finally:
        try:
            get_tracker_from_context(global_context).emit_summary(logger)
        except Exception:
            pass
        return trade_executed_successfully
