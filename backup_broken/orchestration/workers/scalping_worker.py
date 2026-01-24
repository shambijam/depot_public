#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
orchestration/workers/scalping_worker.py - Worker thread de scalping par asset

Worker thread dédié au SCALPING pour un asset spécifique (31 DEC 2025)

Architecture simplifiée:
- Analyse M1 pour l'asset fourni (USDJPY, EURUSD, ou GBPUSD)
- Timing Gatekeeper → PASS/VETO (filtre session + liquidité)
- OrderFlow V6 → Source unique de signaux (score 0-100)
- MarketAnalyzer.build_decision() → BUY/SELL/HOLD direct
- Update GlobalScalpingState (thread-safe)

SUPPRIMÉ: FusionManager, VWAP, Footprint, Momentum
"""

import logging
import queue
import re
import time
import threading
from collections import deque
from datetime import datetime

import pandas as pd

from phase_observer.market_analyzer import MarketAnalyzer
from phase_observer.timing_analyzer import evaluate_trading_conditions
from strategy.advanced_scoring import SimpleAdvancedScorer
from trader.trade_executor import run_trade_execution_pipeline


def scalping_worker(
    asset: str,
    global_state,  # GlobalScalpingState
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
):
    """
    Worker thread dédié au SCALPING pour un asset spécifique (31 DEC 2025)

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

    SUPPRIMÉ: FusionManager, VWAP, Footprint, Momentum
    """

    # OFFSET DE DÉMARRAGE (staggered timing)
    if offset_seconds > 0:
        logger.info(f"[{asset}] ⏱️  Délai démarrage: {offset_seconds:.1f}s")
        time.sleep(offset_seconds)

    # =========================================================================
    # 🔍 DEBUG CRITIQUE : Vérification de la configuration closure_rules
    # =========================================================================
    # Ajoutez cette section IMMÉDIATEMENT APRÈS le délai de démarrage
    try:
        logger.info("=" * 80)
        logger.info(f"🔍 [DEBUG_CLOSURE_RULES_INIT] {asset}")
        logger.info("=" * 80)

        # Test de la méthode debug_merge_process si elle existe
        if hasattr(config_manager, "debug_merge_process"):
            config_manager.debug_merge_process(asset, "scalping")
        else:
            logger.warning("⚠️ config_manager n'a pas de méthode debug_merge_process")

        # Test direct de get_closure_rules_config
        closure_rules = config_manager.get_closure_rules_config(asset, "scalping")
        logger.info(f"📋 [CLOSURE_RULES_RAW] {asset}: {closure_rules}")

        if closure_rules:
            logger.info(f"✅ [CLOSURE_RULES_DETAILS] {asset}:")
            logger.info(f"   • enabled: {closure_rules.get('enabled')}")
            logger.info(
                f"   • target_profit_pips: {closure_rules.get('target_profit_pips')}p"
            )
            logger.info(f"   • max_loss_pips: {closure_rules.get('max_loss_pips')}p")
            logger.info(
                f"   • enable_profit_close: {closure_rules.get('enable_profit_close')}"
            )
            logger.info(
                f"   • enable_loss_guard: {closure_rules.get('enable_loss_guard')}"
            )
        else:
            logger.error(f"❌ [CLOSURE_RULES] Dictionnaire VIDE pour {asset}!")

        logger.info("=" * 80)

    except Exception as e:
        logger.error(f"❌ [DEBUG_CLOSURE_RULES_INIT] Erreur: {e}", exc_info=True)
    # =========================================================================

    # CONFIGURABLE (02 JAN 2026): Lire cycle depuis prod_config.json
    cycle_interval = config_manager.get("bot_behavior.cycle_interval_seconds", 2.5)
    cycle_count = 0

    logger.info(f"🚀 [{asset}] Worker démarré (cycle {cycle_interval}s) ⚡⚡")

    # DÉSACTIVÉ (25 DEC 2025): FusionManager - Architecture minimaliste
    fusion_mgr = None

    # Instancier MarketAnalyzer pour décisions minimalistes
    try:
        market_analyzer_thread = MarketAnalyzer(
            config_manager=config_manager, logger=logger
        )
        logger.info(f"✅ [{asset}] MarketAnalyzer instancié")
    except Exception as e:
        logger.error(f"❌ [{asset}] Impossible de créer MarketAnalyzer: {e}")
        global_state.record_error(asset, f"MarketAnalyzer init failed: {e}")
        return  # Arrêt du thread si MarketAnalyzer échoue

    # Instancier PriceMemoryAnalyzer pour analyse de tendance (08 JAN 2026)
    price_memory_analyzer = None
    try:
        from phase_observer.price_memory_analyzer import PriceMemoryAnalyzer

        price_memory_analyzer = PriceMemoryAnalyzer(logger=logger)
        logger.info(f"✅ [{asset}] PriceMemoryAnalyzer instancié")
    except Exception as e:
        logger.error(f"❌ [{asset}] Impossible de créer PriceMemoryAnalyzer: {e}")

    # NOUVEAU (14 JAN 2026): Instancier InstitutionalReversalDetector pour validation BEARISH
    reversal_detector = None
    try:
        from phase_observer.institutional_reversal_detector import (
            InstitutionalReversalDetector,
        )

        reversal_detector = InstitutionalReversalDetector(config=None, logger=logger)
        logger.info(
            f"✅ [{asset}] InstitutionalReversalDetector instancié (standalone v2.1)"
        )
    except Exception as e:
        logger.error(
            f"❌ [{asset}] Impossible de créer InstitutionalReversalDetector: {e}"
        )

    # NOUVEAU (14 JAN 2026): Instancier BearishScalpingValidator pour trades BEARISH
    bearish_validator = None
    try:
        from phase_observer.bearish_scalping_validator import BearishScalpingValidator

        if reversal_detector and price_memory_analyzer:
            bearish_validator = BearishScalpingValidator(
                reversal_detector=reversal_detector,
                price_memory_analyzer=price_memory_analyzer,
                config=None,
                logger=logger,
            )
            logger.info(
                f"✅ [{asset}] BearishScalpingValidator instancié (M1 ultra-rapide)"
            )
        else:
            logger.warning(
                f"⚠️ [{asset}] BearishScalpingValidator skip: reversal_detector ou price_memory manquant"
            )
    except Exception as e:
        logger.error(f"❌ [{asset}] Impossible de créer BearishScalpingValidator: {e}")

    # NOUVEAU (14 JAN 2026): Instancier MicrostructureAnalyzer pour détection accélérations
    microstructure_analyzer = None
    try:
        from phase_observer.microstructure_analyzer import MicrostructureAnalyzer

        microstructure_analyzer = MicrostructureAnalyzer(logger=logger)
        logger.info(
            f"✅ [{asset}] MicrostructureAnalyzer instancié (tape speed + momentum ignition)"
        )
    except Exception as e:
        logger.error(f"❌ [{asset}] Impossible de créer MicrostructureAnalyzer: {e}")

    # Instancier ScalpingStrategy pour logs de rapport OrderFlow V6
    scalping_strategy = None
    strat_cfg = {}
    try:
        from strategy.scalping import ScalpingStrategy

        strat_cfg = strategy_manager.get_strategy_config("scalping") or {}
        scalping_strategy = ScalpingStrategy(
            config_manager=config_manager,
            strategy_config=strat_cfg,
            mt5_connector=mt5_connector,
            logger=logger,
        )
        logger.info(f"✅ [{asset}] ScalpingStrategy instanciée")
    except Exception as e:
        logger.warning(f"⚠️ [{asset}] ScalpingStrategy init failed: {e}")

    # NOUVEAU (03 JAN 2026): Instancier SimpleAdvancedScorer pour scoring composite
    advanced_scorer = None
    try:
        scoring_weights = strat_cfg.get("advanced_scoring", {}).get("weights", None)
        advanced_scorer = SimpleAdvancedScorer(config=scoring_weights)
        logger.info(f"✅ [{asset}] SimpleAdvancedScorer instancié (évolutif)")
    except Exception as e:
        logger.warning(
            f"⚠️ [{asset}] SimpleAdvancedScorer init failed: {e}, fallback OrderFlow V6 seul"
        )

    # OPTION 1: PRÉ-CALCUL — Squelette trade decision (parties statiques)
    trade_decision_skeleton = None
    last_config_update = 0

    # NOUVEAU (14 JAN 2026): Buffers historiques pour reversal detector
    cvd_history = deque(maxlen=100)
    delta_history = deque(maxlen=100)
    volume_history = deque(maxlen=100)

    # Cache du dernier résultat reversal detector
    last_reversal_check = None
    reversal_check_counter = 0

    logger.info(
        f"✅ [{asset}] Buffers historiques CVD/Delta/Volume initialisés (100 valeurs max)"
    )

    # NOUVEAU (17 JAN 2026): Buffer historique tick_rate pour Z-score accélération
    tick_rate_history = deque(maxlen=100)
    tick_acceleration_bonus = 0
    zscore_result = None
    logger.info(f"✅ [{asset}] Buffer tick_rate_history initialisé (100 valeurs max)")

    # Variables pour Price Memory (initialisées avant la boucle)
    memory_trend_direction = "RANGE"
    memory_trend_strength = 0.0
    memory_net_pips = 0.0
    memory_net_direction = "FLAT"
    memory_clarity = 0.0

    while not stop_event.is_set():
        cycle_count += 1
        cycle_start = time.time()
        global_state.increment_cycle(asset)

        try:
            # VÉRIFICATION SYMBOL (03 JAN 2026): Protection contre symbol non disponible
            try:
                symbol_info = mt5_connector.get_symbol_info(asset)
                if not symbol_info:
                    logger.error(
                        f"[{asset}] ❌ Symbol non disponible dans MT5, skip cycle"
                    )
                    global_state.record_error(asset, "Symbol unavailable")
                    time.sleep(cycle_interval)
                    continue

                # Vérifier si visible dans Market Watch
                if hasattr(symbol_info, "visible") and not symbol_info.visible:
                    logger.warning(
                        f"[{asset}] ⚠️ Symbol non visible, tentative activation..."
                    )
                    try:
                        import MetaTrader5 as mt5

                        if not mt5.symbol_select(asset, True):
                            logger.warning(
                                f"[{asset}] ⚠️ Impossible activer symbol, continue quand même..."
                            )
                    except Exception as e_select:
                        logger.debug(f"[{asset}] Symbol select failed: {e_select}")

            except Exception as e_symbol:
                logger.warning(f"[{asset}] ⚠️ Vérification symbol failed: {e_symbol}")

            # PHASE 2: Import cache multi-niveaux
            from core.bars_cache import bars_cache
            from phase_observer.regime_resolver import regime_resolver

            # PHASE 2: Utiliser cache barres (50 barres)
            rates_df = bars_cache.get_or_fetch(
                symbol=asset,
                timeframe="M1",
                count=50,
                mt5_connector=mt5_connector,
                ttl_seconds=60.0,
            )
            if rates_df is None or rates_df.empty:
                logger.warning(f"[{asset}] Données M1 indisponibles")
                global_state.update_asset_state(
                    asset, {"regime": "NO_DATA", "action": "HOLD"}
                )
                time.sleep(cycle_interval)
                continue

            # ═══════════════════════════════════════════════════════════════════
            # ÉTAPE 1/3: PRICE MEMORY TREND ANALYSIS (RÉORDONNÉ 17 JAN 2026)
            # ═══════════════════════════════════════════════════════════════════
            try:
                if price_memory_analyzer is not None:
                    current_price_pm = (
                        rates_df.iloc[-1]["close"] if len(rates_df) > 0 else 0.0
                    )

                    lookback_bars = 15
                    recent_candles_pm = (
                        rates_df.tail(lookback_bars)
                        if len(rates_df) >= lookback_bars
                        else rates_df
                    )

                    trend_structure = price_memory_analyzer.analyze_trend_structure(
                        historical_data=recent_candles_pm,
                        current_price=current_price_pm,
                    )

                    memory_trend_direction = trend_structure.get(
                        "trend_direction", "RANGE"
                    )
                    memory_trend_strength = trend_structure.get("trend_strength", 0.0)
                    memory_net_disp = trend_structure.get("net_displacement", {})
                    memory_net_pips = memory_net_disp.get("net_pips", 0.0)
                    memory_net_direction = memory_net_disp.get("net_direction", "FLAT")
                    memory_clarity = memory_net_disp.get("trend_clarity", 0.0)

                    logger.info(
                        f"[1/3][PRICE_MEMORY][{asset}] {memory_trend_direction} "
                        f"(strength={memory_trend_strength:.2f}) | "
                        f"Net: {memory_net_direction} {memory_net_pips:+.1f} pips ({lookback_bars} bars) | "
                        f"Clarity: {memory_clarity:.2f}"
                    )
                else:
                    memory_trend_direction = "RANGE"
                    memory_trend_strength = 0.0
                    memory_net_pips = 0.0
                    memory_net_direction = "FLAT"
                    memory_clarity = 0.0

            except Exception as e_memory_trend:
                logger.error(
                    f"[{asset}] Erreur Price Memory Trend: {e_memory_trend}",
                    exc_info=True,
                )
                memory_trend_direction = "RANGE"
                memory_trend_strength = 0.0
                memory_net_pips = 0.0
                memory_net_direction = "FLAT"
                memory_clarity = 0.0

            # ═══════════════════════════════════════════════════════════════════
            # ÉTAPE 2/3: TAPE SPEED + Z-SCORE (RÉORDONNÉ 17 JAN 2026)
            # ═══════════════════════════════════════════════════════════════════
            ticks_df = None
            tape_speed_result = None
            try:
                sliding_window_seconds = 8

                now_utc = pd.Timestamp.utcnow()
                tick_window_start = now_utc - pd.Timedelta(
                    seconds=sliding_window_seconds
                )
                tick_window_end = now_utc

                logger.info(
                    f"[{asset}] 🔄 Chargement ticks [fenêtre glissante {sliding_window_seconds}s] | "
                    f"[{tick_window_start.strftime('%H:%M:%S')} → {tick_window_end.strftime('%H:%M:%S')}]"
                )

                ticks_df = mt5_connector.get_ticks_for_candle(
                    asset,
                    tick_window_start.to_pydatetime(),
                    tick_window_end.to_pydatetime(),
                    timeout=5.0,
                )

                if ticks_df is not None and not ticks_df.empty:
                    logger.info(
                        f"[{asset}] ✅ {len(ticks_df)} ticks chargés (fenêtre glissante {sliding_window_seconds}s)"
                    )

                    # NOUVEAU (14 JAN 2026): Analyser tape speed (accélérations)
                    if microstructure_analyzer:
                        try:
                            tape_speed_result = (
                                microstructure_analyzer.analyze_tape_speed(ticks_df)
                            )
                            logger.critical(
                                f"⚡ [TAPE_SPEED][{asset}] "
                                f"Buy={tape_speed_result['tape_speed_buy']:.2f} ticks/s | "
                                f"Sell={tape_speed_result['tape_speed_sell']:.2f} ticks/s | "
                                f"Ratio={tape_speed_result['speed_ratio']:.2f} | "
                                f"Signal={tape_speed_result['interpretation']}"
                            )
                        except Exception as e_tape:
                            logger.warning(
                                f"[{asset}] ⚠️ Erreur tape speed analysis: {e_tape}"
                            )

                    # NOUVEAU (17 JAN 2026): Calculer tick_rate total et Z-score accélération
                    if (
                        tape_speed_result
                        and tape_speed_result.get("interpretation")
                        != "PAS_ASSEZ_DONNEES"
                    ):
                        try:
                            total_tick_rate = (
                                tape_speed_result["tape_speed_buy"]
                                + tape_speed_result["tape_speed_sell"]
                            )
                            tick_rate_history.append(total_tick_rate)

                            # Calculer Z-score si historique suffisant (30+ valeurs)
                            if len(tick_rate_history) >= 30 and microstructure_analyzer:
                                zscore_result = microstructure_analyzer.calculate_tick_acceleration_zscore(
                                    current_tick_rate=total_tick_rate,
                                    tick_rate_history=list(tick_rate_history),
                                )
                                tick_acceleration_bonus = zscore_result["bonus"]

                                if tick_acceleration_bonus > 0:
                                    logger.critical(
                                        f"⚡ [ZSCORE_BONUS][{asset}] +{tick_acceleration_bonus} pts "
                                        f"(Z={zscore_result['zscore']:.2f}) → {zscore_result['interpretation']}"
                                    )
                                else:
                                    logger.debug(
                                        f"[ZSCORE][{asset}] Z={zscore_result['zscore']:.2f} → {zscore_result['interpretation']} (pas de bonus)"
                                    )
                            else:
                                tick_acceleration_bonus = 0
                                zscore_result = None

                        except Exception as e_zscore:
                            logger.warning(
                                f"[{asset}] ⚠️ Erreur calcul Z-score: {e_zscore}"
                            )
                            tick_acceleration_bonus = 0
                            zscore_result = None
                else:
                    logger.warning(
                        f"[{asset}] ⚠️ Aucun tick récupéré dans fenêtre glissante {sliding_window_seconds}s"
                    )
                    ticks_df = None

            except TimeoutError as e_timeout:
                logger.error(f"[{asset}] ⏱️ TIMEOUT chargement ticks: {e_timeout}")
                ticks_df = None

            except Exception as e_ticks:
                logger.error(
                    f"[{asset}] ❌ Erreur chargement ticks: {e_ticks}", exc_info=True
                )
                ticks_df = None

            # MarketAnalyzer (phase + patterns + features)
            market_analyzer = MarketAnalyzer(config_manager, logger)

            # PIPELINE SIMPLIFIÉ (25 DEC 2025): Analyse directe sans cache
            try:
                market_results = market_analyzer.analyze(
                    asset=asset, df=rates_df, ticks=ticks_df
                )
                logger.debug(f"[{asset}] market_analyzer.analyze() OK")
            except Exception as e_analysis:
                logger.error(f"[{asset}] Erreur analyze(): {e_analysis}")
                global_state.record_error(asset, f"analyze() failed: {e_analysis}")
                market_results = {
                    "latest": {},
                    "annotated_df": (
                        rates_df if rates_df is not None else pd.DataFrame()
                    ),
                }

            # OPTION 1: PRÉ-CALCUL — Mise à jour squelette si config changée
            try:
                current_config_hash = hash(
                    str(config_manager.get_current_dynamic_config())
                )
                if (
                    trade_decision_skeleton is None
                    or current_config_hash != last_config_update
                ):
                    base_config = config_manager.get_current_dynamic_config()
                    strat_cfg = strategy_manager.get_strategy_config("scalping") or {}

                    strat_cfg_entry = strat_cfg.get("entry_rules", {})
                    scalping_cfg = strat_cfg_entry.get("scalping", {})
                    burst_cfg = scalping_cfg.get("burst_scalping", {})
                    resolved_burst = burst_cfg.get("burst_size", 5)

                    sltp_cfg = burst_cfg.get("sltp", {})
                    if not sltp_cfg:
                        sltp_cfg = (
                            base_config.get("entry_rules", {})
                            .get("scalping", {})
                            .get("burst_scalping", {})
                            .get("sltp", {})
                        ) or {}

                    # CONFIG_MERGER (16 JAN 2026): Utilisation du nouveau système de fusion
                    try:
                        import copy

                        merged_config = config_manager.get_merged_config(
                            asset, "scalping"
                        )
                        if not merged_config:
                            merged_config = copy.deepcopy(base_config)
                            logger.warning(
                                f"[{asset}] ConfigMerger a retourné vide, fallback sur base_config"
                            )

                        sltp_flat = config_manager.get_sltp_config(asset, "scalping")
                        if sltp_flat and sltp_flat.get("sl_pips"):
                            sltp_cfg = {
                                "sl": {
                                    "pips": sltp_flat.get("sl_pips"),
                                    "buffer_pips": sltp_flat.get("sl_buffer_pips", 0),
                                },
                                "tp": {"pips": sltp_flat.get("tp_pips")},
                                "sl_method": sltp_flat.get("sl_method", "PIPS"),
                                "tp_method": sltp_flat.get("tp_method", "PIPS"),
                                "rr_base": sltp_flat.get("rr_base", 1.5),
                                "rr_floor": sltp_flat.get("rr_floor", 1.0),
                                "rr_cap": sltp_flat.get("rr_cap", 3.0),
                                "exit_mode": sltp_flat.get(
                                    "exit_mode", "sl_tp_then_trail"
                                ),
                            }
                            logger.info(
                                f"[CONFIG_MERGER][{asset}] SLTP chargé | "
                                f"SL={sltp_flat.get('sl_pips')} pips | TP={sltp_flat.get('tp_pips')} pips"
                            )
                        else:
                            logger.warning(
                                f"[{asset}] ConfigMerger n'a pas trouvé de SLTP, utilisation fallback"
                            )

                    except Exception as e:
                        logger.warning(
                            f"[{asset}] ConfigMerger erreur: {e}, fallback sur base_config"
                        )
                        import copy

                        merged_config = copy.deepcopy(base_config)

                    # SQUELETTE PRÉ-CALCULÉ (parties statiques)
                    trade_decision_skeleton = {
                        "static": {
                            "symbol": asset,
                            "rule_name": "burst_scalping",
                            "burst_size": resolved_burst,
                            "burst_enabled": True,
                            "strategy": "scalping",
                            "sltp": sltp_cfg,
                            "safety": {"fat_finger": {"policy": "FLOOR"}},
                        },
                        "merged_config": merged_config,
                        "resolved_burst": resolved_burst,
                    }
                    last_config_update = current_config_hash
                    logger.info(
                        f"⚡ [PRE-CALC] Squelette trade decision mis à jour (burst={resolved_burst})"
                    )
            except Exception as e:
                logger.warning(f"[PRE-CALC] Erreur pré-calcul squelette: {e}")

            # PIPELINE MINIMALISTE (26 DEC 2025) - S'exécute TOUJOURS si données disponibles
            if market_results:
                latest = market_results.get("latest")

                # Context minimal pour compatibilité + account_info pour sizing
                account_info_dict = {}
                try:
                    acct = mt5_connector.get_account_info()
                    if acct:
                        account_info_dict = (
                            acct._asdict()
                            if hasattr(acct, "_asdict")
                            else (dict(acct) if hasattr(acct, "__dict__") else {})
                        )
                except Exception as e_acct:
                    logger.warning(
                        f"[{asset}] Erreur récupération account_info: {e_acct}"
                    )

                # FIX (02 JAN 2026): Récupérer active_broker_account pour calcul risk_per_trade_percent
                broker_account = {}
                try:
                    broker_account = config_manager.get_mt5_account_credentials(
                        account_id=None,
                        mode=config_manager.get("mode_execution", "DEMO"),
                    )
                    trade_settings = (
                        broker_account.get("trade_settings", {})
                        if broker_account
                        else {}
                    )
                    logger.debug(
                        f"[{asset}] 🔍 broker_account.trade_settings = {trade_settings}"
                    )
                except Exception as e_broker:
                    logger.warning(
                        f"[{asset}] Erreur récupération broker account: {e_broker}"
                    )

                ctx = {
                    "asset": asset,
                    "phase": market_results.get("phase", {}),
                    "volatility_pips": market_results.get("volatility_pips", 0.0),
                    "account_info": account_info_dict,
                    "active_broker_account": broker_account,
                }

                # PIPELINE MINIMALISTE (26 DEC 2025) - OrderFlow V6 seul
                logger.info(f"🎯 [SCALPING_CYCLE_{cycle_count}] Début analyse {asset}")

                # INITIALISATION VARIABLES (pour rapport)
                orderflow_result_mini = {"score": 0.0, "bias": "NEUTRAL", "summary": {}}
                decision_mini = {
                    "action": "HOLD",
                    "confidence": 0.0,
                    "rationale": "Non analysé",
                    "anchor_price": None,
                }

                # ═══════════════════════════════════════════════════════════════════
                # ÉTAPE 3/3: ORDERFLOW V6 + COMPOSITE SCORE (RÉORDONNÉ 17 JAN 2026)
                # ═══════════════════════════════════════════════════════════════════
                if scalping_strategy:
                    try:
                        from core.bars_cache import bars_cache

                        rates_df_fresh = bars_cache.get_or_fetch(
                            symbol=asset,
                            timeframe="M1",
                            count=50,
                            mt5_connector=mt5_connector,
                        )
                        if rates_df_fresh is None or rates_df_fresh.empty:
                            logger.warning(
                                "[ORDERFLOW] Impossible de récupérer rates_df M1 depuis cache"
                            )
                            raise ValueError("rates_df vide")

                        # LOG: Vérifier rafraîchissement bougies
                        now_utc = pd.Timestamp.now(tz="UTC")
                        last_candle_time = (
                            rates_df_fresh.iloc[-1]["time"]
                            if "time" in rates_df_fresh.columns
                            else rates_df_fresh.index[-1]
                        )
                        prev_candle_time = (
                            rates_df_fresh.iloc[-2]["time"]
                            if "time" in rates_df_fresh.columns
                            else rates_df_fresh.index[-2]
                        )
                        last_candle_color = (
                            "🟢"
                            if rates_df_fresh.iloc[-1]["close"]
                            > rates_df_fresh.iloc[-1]["open"]
                            else "🔴"
                        )
                        prev_candle_color = (
                            "🟢"
                            if rates_df_fresh.iloc[-2]["close"]
                            > rates_df_fresh.iloc[-2]["open"]
                            else "🔴"
                        )
                        logger.debug(
                            f"[RATES_REFRESH][{asset}] now={now_utc.strftime('%H:%M:%S')} | "
                            f"last_candle={last_candle_time} {last_candle_color} | "
                            f"prev_candle={prev_candle_time} {prev_candle_color} (analysée)"
                        )

                        # Préparer asset_signals
                        asset_signals_for_of = {
                            "footprint_summary": {},
                            "orderflow_summary": {},
                            "ticks_df": ticks_df,
                        }

                        # Appel OrderFlow V6
                        of_v6_result = scalping_strategy._analyze_orderflow_v6(
                            asset=asset,
                            df_m1=rates_df_fresh,
                            df_m3=None,
                            df_m5=None,
                            asset_signals=asset_signals_for_of,
                        )

                        # Extraire résultats
                        orderflow_result_mini = {
                            "score": of_v6_result.get("total_score", 0.0),
                            "bias": of_v6_result.get("bias", "NEUTRAL"),
                            "summary": of_v6_result,
                        }

                        logger.info(
                            f"[ORDERFLOW][{asset}] score={orderflow_result_mini['score']:.1f}/100 | "
                            f"bias={orderflow_result_mini['bias']}"
                        )

                        # NOUVEAU (03 JAN 2026): Calcul composite score avec SimpleAdvancedScorer
                        if advanced_scorer:
                            try:
                                institutional_analysis = of_v6_result.get(
                                    "institutional_analysis", {}
                                )

                                composite_result = (
                                    advanced_scorer.calculate_composite_score(
                                        ticks_df=ticks_df,
                                        candles_df=rates_df_fresh,
                                        orderflow_score=orderflow_result_mini["score"],
                                        institutional_analysis=institutional_analysis,
                                    )
                                )

                                orderflow_result_mini["score"] = composite_result[
                                    "composite_score"
                                ]
                                orderflow_result_mini["composite_details"] = (
                                    composite_result
                                )
                                orderflow_result_mini["composite_enabled"] = True

                                # NOUVEAU (17 JAN 2026): Ajouter bonus Z-score accélération
                                score_before_zscore = orderflow_result_mini["score"]
                                if tick_acceleration_bonus > 0:
                                    orderflow_result_mini[
                                        "score"
                                    ] += tick_acceleration_bonus
                                    orderflow_result_mini["score"] = min(
                                        100.0, orderflow_result_mini["score"]
                                    )
                                    orderflow_result_mini["zscore_bonus"] = (
                                        tick_acceleration_bonus
                                    )
                                    orderflow_result_mini["zscore_details"] = (
                                        zscore_result
                                    )

                                    logger.info(
                                        f"[COMPOSITE_SCORE][{asset}] {score_before_zscore:.1f} → {orderflow_result_mini['score']:.1f}/100 "
                                        f"(+{tick_acceleration_bonus} Z-score) | "
                                        f"Decision={composite_result['decision']} ({composite_result['confidence']}) | "
                                        f"Components: OF={composite_result['components']['orderflow']:.0f} "
                                        f"INST={composite_result['components']['institutional']:.0f} "
                                        f"MS={composite_result['components']['microstructure']:.0f} "
                                        f"LQ={composite_result['components']['liquidity']:.0f} "
                                        f"DV={composite_result['components']['divergence']:.0f} "
                                        f"SM={composite_result['components']['smart_money']:.0f}"
                                    )
                                else:
                                    orderflow_result_mini["zscore_bonus"] = 0
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
                                logger.error(
                                    f"[COMPOSITE_SCORE_ERROR] Erreur: {e_composite}, fallback OrderFlow V6 seul",
                                    exc_info=True,
                                )
                                orderflow_result_mini["composite_enabled"] = False
                        else:
                            orderflow_result_mini["composite_enabled"] = False

                        # NOUVEAU (14 JAN 2026): Alimentation buffers historiques pour reversal detector
                        try:
                            current_cvd = of_v6_result.get("cvd", 0.0)
                            current_delta = of_v6_result.get("delta", 0.0)
                            current_volume = (
                                rates_df_fresh.iloc[-1]["tick_volume"]
                                if "tick_volume" in rates_df_fresh.columns
                                else 0.0
                            )

                            cvd_history.append(current_cvd)
                            delta_history.append(current_delta)
                            volume_history.append(current_volume)

                            logger.debug(
                                f"[BUFFER_FEED][{asset}] CVD={current_cvd:.2f} | "
                                f"Delta={current_delta:.0f} | Volume={current_volume:.0f} | "
                                f"Buffer size: CVD={len(cvd_history)}, Delta={len(delta_history)}, Vol={len(volume_history)}"
                            )
                        except Exception as e_buffer:
                            logger.warning(
                                f"[BUFFER_FEED][{asset}] Erreur alimentation buffers: {e_buffer}"
                            )

                        # NOUVEAU (14 JAN 2026): Appel reversal detector (1x/10 cycles = 25s)
                        reversal_check_counter += 1
                        if reversal_check_counter >= 10 and reversal_detector:
                            reversal_check_counter = 0

                            if len(cvd_history) >= 30:
                                try:
                                    market_data_reversal = {
                                        "candles_m5": rates_df_fresh,
                                        "candles_m1": rates_df_fresh,
                                        "cvd_values": list(cvd_history),
                                        "delta_values": list(delta_history),
                                        "volume_values": list(volume_history),
                                    }

                                    last_reversal_check = (
                                        reversal_detector.detect_reversal(
                                            market_data_reversal
                                        )
                                    )

                                    logger.critical(
                                        f"🏛️ [REVERSAL_CHECK][{asset}] "
                                        f"Score={last_reversal_check['institutional_score']:.1f}/100 | "
                                        f"Conviction={last_reversal_check['conviction_level']} | "
                                        f"Trend={last_reversal_check['new_trend']} | "
                                        f"Reversal={last_reversal_check['reversal_detected']}"
                                    )

                                except Exception as e_reversal:
                                    logger.error(
                                        f"[REVERSAL_DETECTOR][{asset}] Erreur: {e_reversal}"
                                    )
                                    last_reversal_check = None
                            else:
                                logger.debug(
                                    f"[REVERSAL_DETECTOR][{asset}] Pas assez de données "
                                    f"({len(cvd_history)} < 30), skip ce cycle"
                                )

                    except Exception as e_of:
                        logger.critical(
                            f"[ORDERFLOW_V6_ERROR] Erreur: {e_of}", exc_info=True
                        )
                        orderflow_result_mini = {
                            "score": 0.0,
                            "bias": "NEUTRAL",
                            "summary": {},
                            "composite_enabled": False,
                        }

                # NOTE: Price Memory Trend déplacé en ÉTAPE 1/3 (avant Tape Speed)

                # 16 JAN 2026: MTF TREND VERDICT (M15 + M5 + M1)
                mtf_verdict = None
                mtf_bonus = 0.0
                try:
                    if price_memory_analyzer is not None:
                        df_m5_mtf = None
                        df_m15_mtf = None

                        try:
                            df_m5_mtf = mt5_connector.get_rates(asset, "M5", 1)
                        except Exception:
                            df_m5_mtf = None

                        try:
                            df_m15_mtf = mt5_connector.get_rates(asset, "M15", 1)
                        except Exception:
                            df_m15_mtf = None

                        # FIX (17 JAN 2026): M1 = 1 bougie seulement (comme M5 et M15)
                        df_m1_mtf = (
                            rates_df_fresh.tail(1) if len(rates_df_fresh) > 0 else None
                        )
                        current_price_mtf = (
                            rates_df_fresh.iloc[-1]["close"]
                            if len(rates_df_fresh) > 0
                            else 0
                        )

                        m5_len = len(df_m5_mtf) if df_m5_mtf is not None else 0
                        m15_len = len(df_m15_mtf) if df_m15_mtf is not None else 0
                        m1_len = len(df_m1_mtf) if df_m1_mtf is not None else 0
                        logger.debug(
                            f"[MTF_DATA][{asset}] M15:{m15_len} M5:{m5_len} M1:{m1_len} candles"
                        )

                        mtf_verdict = price_memory_analyzer.get_mtf_trend_verdict(
                            asset=asset,
                            candles_m15=df_m15_mtf,
                            candles_m5=df_m5_mtf,
                            candles_m1=df_m1_mtf,
                            current_price=current_price_mtf,
                        )

                        if mtf_verdict:
                            mtf_bonus = mtf_verdict.bonus

                            emoji = "🐻" if mtf_verdict.direction == "BEARISH" else "🐂"

                            def _tf_emoji(d):
                                return (
                                    "🔴"
                                    if d == "BEARISH"
                                    else ("🟢" if d == "BULLISH" else "⚫")
                                )

                            logger.critical(
                                f"{emoji} [MTF_VERDICT][{asset}] "
                                f"{mtf_verdict.direction} ({mtf_verdict.alignment}) | "
                                f"M15:{_tf_emoji(mtf_verdict.m15_direction)} "
                                f"M5:{_tf_emoji(mtf_verdict.m5_direction)} "
                                f"M1:{_tf_emoji(mtf_verdict.m1_direction)} | "
                                f"Bonus: {mtf_bonus:+.0f} pts"
                            )

                            # AJOUT BONUS MTF AU SCORE (FIX 17 JAN 2026) - SÉCURISÉ
                            if mtf_bonus > 0:
                                # --- 🛡️ DÉBUT CORRECTIF SÉCURITÉ ---
                                # On vérifie si la direction du MTF (Rouge/Vert) est cohérente avec le trade
                                # Note: On n'a pas encore l'action finale ici (BUY/SELL), mais on a le 'bias' de l'OrderFlow

                                current_bias = str(
                                    orderflow_result_mini.get("bias", "NEUTRAL")
                                ).upper()
                                mtf_dir = str(mtf_verdict.direction).upper()
                                is_safe = True

                                # Si OrderFlow dit BUY mais MTF dit BEARISH (Rouge) -> DANGER
                                if current_bias == "BUY" and mtf_dir == "BEARISH":
                                    is_safe = False
                                    logger.warning(
                                        f"🛡️ [MTF_GUARD] Bonus +{mtf_bonus} ANNULÉ: Bias BUY vs MTF BEARISH"
                                    )

                                # Si OrderFlow dit SELL mais MTF dit BULLISH (Vert) -> DANGER
                                elif current_bias == "SELL" and mtf_dir == "BULLISH":
                                    is_safe = False
                                    logger.warning(
                                        f"🛡️ [MTF_GUARD] Bonus +{mtf_bonus} ANNULÉ: Bias SELL vs MTF BULLISH"
                                    )

                                if is_safe:
                                    # C'est bon, on applique le bonus
                                    score_before_mtf = orderflow_result_mini["score"]
                                    orderflow_result_mini["score"] += mtf_bonus
                                    orderflow_result_mini["score"] = min(
                                        100.0, orderflow_result_mini["score"]
                                    )
                                    orderflow_result_mini["mtf_bonus"] = mtf_bonus
                                    orderflow_result_mini["mtf_details"] = {
                                        "direction": mtf_verdict.direction,
                                        "alignment": mtf_verdict.alignment,
                                        "m15": mtf_verdict.m15_direction,
                                        "m5": mtf_verdict.m5_direction,
                                        "m1": mtf_verdict.m1_direction,
                                    }
                                    logger.info(
                                        f"[MTF_BONUS][{asset}] Score: {score_before_mtf:.1f} → {orderflow_result_mini['score']:.1f}/100 "
                                        f"(+{mtf_bonus:.0f} MTF {mtf_verdict.alignment})"
                                    )
                                else:
                                    # On force le bonus à 0 pour éviter le drame
                                    mtf_bonus = 0.0
                                # --- 🛡️ FIN CORRECTIF SÉCURITÉ ---

                except Exception as e_mtf:
                    logger.warning(f"[{asset}] MTF Verdict error: {e_mtf}")
                    mtf_verdict = None
                    mtf_bonus = 0.0

                # ÉTAPE 2: TIMING GATEKEEPER (GO/NOGO TRADE)
                timing_verdict = None
                fusion_out = {
                    "ok": False,
                    "action": "HOLD",
                    "fused_confidence": 0.0,
                    "signal_type": "NOT_INITIALIZED",
                    "veto_reason": "Fusion not completed",
                    "orderflow_score": 0.0,
                }
                try:
                    ticks_for_timing = (
                        ticks_df
                        if (ticks_df is not None and not ticks_df.empty)
                        else None
                    )

                    if ticks_for_timing is not None:
                        logger.info(
                            f"[TIMING_PREP] ✅ Passage {len(ticks_for_timing)} ticks au gatekeeper"
                        )
                    else:
                        logger.warning(
                            f"[TIMING_PREP] ⚠️ AUCUN tick disponible pour gatekeeper → tick_rate=0"
                        )

                    scalping_config_global = (
                        strategy_manager.get_strategy_config("scalping")
                        if strategy_manager
                        else {}
                    )
                    asset_config_timing = config_manager.load_asset_config(asset)

                    timing_verdict = evaluate_trading_conditions(
                        asset=asset,
                        current_time=pd.Timestamp.now(tz="UTC"),
                        ticks_df=ticks_for_timing,
                        market_context={},
                        asset_config=asset_config_timing,
                        scalping_config=scalping_config_global,
                    )

                    verdict_str = (
                        "✅ PASS"
                        if timing_verdict["verdict"] == "PASS"
                        else f"❌ VETO ({timing_verdict.get('veto_reason', 'N/A')})"
                    )
                    logger.info(
                        f"[TIMING_GATEKEEPER][{asset}] {verdict_str} | "
                        f"session={timing_verdict.get('quality_metrics', {}).get('session', 'N/A')} | "
                        f"tick_rate={timing_verdict.get('quality_metrics', {}).get('tick_rate', 0):.1f}/s"
                    )
                except Exception as e_timing:
                    logger.error(
                        f"[TIMING_GATEKEEPER] Erreur: {e_timing}", exc_info=True
                    )
                    timing_verdict = {
                        "verdict": "VETO",
                        "veto_reason": f"Timing error: {e_timing}",
                        "quality_metrics": {},
                    }

                # ÉTAPE 3: DÉCISION INTELLIGENTE (02 JAN 2026 - Veto pondéré)
                veto_score = (
                    timing_verdict.get("veto_score", 0.0) if timing_verdict else 0.0
                )
                orderflow_score = orderflow_result_mini["score"]

                # LOGIQUE INTELLIGENTE (02 JAN 2026)
                can_override_veto = False
                override_reason = None

                if orderflow_score >= 90.0 and veto_score < 70.0:
                    can_override_veto = True
                    override_reason = f"Signal exceptionnel ({orderflow_score:.0f}/100) > veto ({veto_score:.0f}/100)"
                elif orderflow_score >= 85.0 and veto_score < 60.0:
                    can_override_veto = True
                    override_reason = f"Signal très fort ({orderflow_score:.0f}/100) > veto modéré ({veto_score:.0f}/100)"

                timing_blocks_trade = (
                    timing_verdict
                    and timing_verdict.get("verdict") != "PASS"
                    and not can_override_veto
                )

                logger.critical(
                    f"[DECISION_LOGIC][{asset}] timing_blocks_trade={timing_blocks_trade} | "
                    f"can_override_veto={can_override_veto} | "
                    f"timing_verdict={timing_verdict.get('verdict') if timing_verdict else None} | "
                    f"orderflow_score={orderflow_score:.1f} | "
                    f"veto_score={veto_score:.1f}"
                )

                if timing_blocks_trade:
                    # VETO timing trop fort → HOLD
                    logger.critical(
                        f"[DECISION_BRANCH][{asset}] ➡️ BRANCHE 1: TIMING_VETO (score={orderflow_score:.1f} < 85, veto={veto_score:.1f})"
                    )
                    veto_reason = timing_verdict.get("veto_reason", "Unknown")
                    logger.info(
                        f"⚠️  [TIMING_VETO] {veto_reason} (veto={veto_score:.0f}) "
                        f"→ HOLD (OrderFlow score={orderflow_score:.1f} insuffisant pour override)"
                    )

                    decision_mini = {
                        "action": "HOLD",
                        "confidence": 0.0,
                        "rationale": f"TIMING VETO: {veto_reason} (OrderFlow {orderflow_score:.0f}/100 < override threshold)",
                        "anchor_price": None,
                    }

                    fusion_out = {
                        "ok": False,
                        "action": "HOLD",
                        "fused_confidence": 0.0,
                        "signal_type": "TIMING_VETO",
                        "veto_reason": veto_reason,
                        "veto_score": veto_score,
                        "orderflow_score": orderflow_score,
                    }

                else:
                    # PASS timing OU override → Vérifier phase avant de décider
                    if can_override_veto:
                        logger.critical(
                            f"[DECISION_BRANCH][{asset}] ➡️ BRANCHE 2: OVERRIDE_VETO (score={orderflow_score:.1f} ≥ 85)"
                        )
                        logger.info(
                            f"🚀 [VETO_OVERRIDE] {override_reason} → Signal autorisé malgré timing non optimal"
                        )
                    else:
                        logger.critical(
                            f"[DECISION_BRANCH][{asset}] ➡️ BRANCHE 3: PASS_NORMAL (timing=PASS, score={orderflow_score:.1f} < 85)"
                        )

                    # VETO PHASE DE MARCHÉ DYNAMIQUE
                    blocked_phases_config = (
                        scalping_config_global.get("entry_rules", {})
                        .get("scalping", {})
                        .get("blocked_phases", {})
                    )
                    blocked_phases_enabled = blocked_phases_config.get("enabled", True)
                    blocked_phases_list = blocked_phases_config.get(
                        "phases",
                        [
                            "range",
                            "accumulation",
                            "range_accumulation",
                            "range_distribution",
                        ],
                    )

                    latest_candle = market_results.get("latest", {})
                    current_regime = latest_candle.get("regime", "unknown")
                    phase_str = (
                        str(current_regime).lower() if current_regime else "unknown"
                    )

                    phase_is_blocked = blocked_phases_enabled and any(
                        blocked in phase_str for blocked in blocked_phases_list
                    )

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
                            "anchor_price": None,
                        }

                        fusion_out = {
                            "ok": False,
                            "action": "HOLD",
                            "fused_confidence": 0.0,
                            "signal_type": "REGIME_VETO",
                            "veto_reason": f"Régime '{phase_str}' interdit",
                            "orderflow_score": orderflow_result_mini["score"],
                        }

                        logger.info(
                            f"[MINIMALIST][{asset}] HOLD | rationale=REGIME VETO: {phase_str}"
                        )
                    else:
                        # PASS timing + PASS phase → Décision basée sur OrderFlow
                        asset_min_score_worker = 65.0
                        try:
                            aconf_worker = (
                                config_manager.config_loader.load_asset_config(asset)
                                or {}
                            )
                            asset_min_score_worker = float(
                                (aconf_worker.get("overrides", {}) or {})
                                .get("scalping", {})
                                .get("entry_rules", {})
                                .get("scalping", {})
                                .get("burst_scalping", {})
                                .get("min_score", asset_min_score_worker)
                            )
                            logger.debug(
                                f"[CONFIG_WORKER][{asset}] min_score={asset_min_score_worker} (from asset config)"
                            )
                        except Exception as e_min_score_worker:
                            logger.warning(
                                f"[CONFIG_WORKER][{asset}] Erreur lecture min_score: {e_min_score_worker}, using default={asset_min_score_worker}"
                            )

                        try:
                            decision_mini = (
                                market_analyzer_thread.build_decision(
                                    orderflow_result=orderflow_result_mini,
                                    min_score=asset_min_score_worker,
                                )
                                if market_analyzer_thread
                                else {
                                    "action": "HOLD",
                                    "confidence": 0.0,
                                    "rationale": "MarketAnalyzer unavailable",
                                }
                            )

                            logger.info(
                                f"[DECISION][{asset}] action={decision_mini['action']} | "
                                f"confidence={decision_mini['confidence']:.2f} | "
                                f"rationale={decision_mini['rationale']}"
                            )
                        except Exception as e_decision:
                            logger.error(
                                f"[DECISION] Erreur: {e_decision}", exc_info=True
                            )
                            decision_mini = {
                                "action": "HOLD",
                                "confidence": 0.0,
                                "rationale": f"Decision error: {e_decision}",
                            }

                        # Construction fusion_out
                        if decision_mini["action"] in ["BUY", "SELL"]:
                            anchor_price = (
                                decision_mini.get("anchor_price")
                                or (latest.get("current_price") if latest else None)
                                or (latest.get("close") if latest else None)
                            )

                            fusion_out = {
                                "ok": True,
                                "action": decision_mini["action"],
                                "fused_confidence": decision_mini["confidence"],
                                "signal_type": "MINIMALIST_ORDERFLOW",
                                "rationale": decision_mini["rationale"],
                                "orderflow_score": orderflow_result_mini["score"],
                                "timing_quality": timing_verdict.get(
                                    "quality_metrics", {}
                                ),
                                "price": anchor_price,
                                "context": ctx,
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
                                "orderflow_score": orderflow_result_mini["score"],
                            }

                            logger.info(
                                f"[MINIMALIST][{asset}] HOLD | rationale={decision_mini['rationale']}"
                            )

                # UPDATE GLOBAL STATE POUR DASHBOARD (31 DEC 2025)
                try:
                    latest_candle = (
                        market_results.get("latest", {}) if market_results else {}
                    )
                    current_regime = latest_candle.get("regime", "UNKNOWN")
                    regime_strength = latest_candle.get("regime_strength", 0.0)

                    of_score = orderflow_result_mini.get("score", 0.0)
                    of_bias = orderflow_result_mini.get("bias", "NEUTRAL")
                    of_summary = orderflow_result_mini.get("summary", {})
                    of_quality = of_summary.get("signal_quality", "NO_TRADE")

                    timing_status = (
                        timing_verdict.get("verdict", "UNKNOWN")
                        if timing_verdict
                        else "UNKNOWN"
                    )
                    qm = (
                        timing_verdict.get("quality_metrics", {})
                        if timing_verdict
                        else {}
                    )
                    tick_rate = qm.get("tick_rate", 0.0)
                    coverage_s = qm.get("coverage_s", 0.0)

                    action = decision_mini.get("action", "HOLD")
                    confidence = decision_mini.get("confidence", 0.0)

                    global_state.update_asset_state(
                        asset,
                        {
                            "regime": (
                                str(current_regime).upper()
                                if current_regime
                                else "UNKNOWN"
                            ),
                            "regime_force": regime_strength,
                            "of_score": of_score,
                            "of_bias": of_bias,
                            "of_quality": of_quality,
                            "timing_status": timing_status,
                            "tick_rate": tick_rate,
                            "coverage_s": coverage_s,
                            "action": action,
                            "confidence": confidence,
                        },
                    )

                    regime_short = (
                        str(current_regime)[:4].upper() if current_regime else "UNKN"
                    )
                    bias_short = of_bias[:3] if of_bias else "NEU"
                    timing_short = timing_status[:4] if timing_status else "UNKN"

                    is_composite_log = orderflow_result_mini.get(
                        "composite_enabled", False
                    )
                    score_label = "CS" if is_composite_log else "OF"
                    score_format_log = (
                        f"{of_score:.1f}" if is_composite_log else f"{of_score:.0f}"
                    )

                    logger.info(
                        f"[{asset}] "
                        f"R:{regime_short}({regime_strength:.1f}) | "
                        f"{score_label}:{score_format_log}/{bias_short} | "
                        f"T:{timing_short} | "
                        f"→{action}"
                    )

                except Exception as e_update:
                    logger.error(f"[{asset}] Erreur update global_state: {e_update}")

                # SOUMETTRE RAPPORT À LA QUEUE (31 DEC 2025 - Solution B)
                try:
                    latest_candle = (
                        market_results.get("latest", {}) if market_results else {}
                    )
                    current_regime = latest_candle.get("regime", "UNKNOWN")
                    regime_strength = latest_candle.get("regime_strength", 0.0)

                    of_score = orderflow_result_mini.get("score", 0.0)
                    of_bias = orderflow_result_mini.get("bias", "NEUTRAL")

                    of_summary = orderflow_result_mini.get("summary", {})
                    delta_details = of_summary.get("delta_momentum_details", {})
                    delta_total = delta_details.get("delta_total", 0)

                    # Formater trend avec Price Memory + OrderFlow
                    if memory_trend_direction == "BULLISH" and of_bias in [
                        "BULLISH",
                        "BUY",
                    ]:
                        trend_str = f"🟢🟢 BULL NET{memory_net_pips:+.0f}"
                    elif memory_trend_direction == "BEARISH" and of_bias in [
                        "BEARISH",
                        "SELL",
                    ]:
                        trend_str = f"🔴🔴 BEAR NET{memory_net_pips:+.0f}"
                    elif memory_trend_direction == "BULLISH" and of_bias in [
                        "BEARISH",
                        "SELL",
                    ]:
                        trend_str = f"⚠️ BEAR NET{memory_net_pips:+.0f}"
                    elif memory_trend_direction == "BEARISH" and of_bias in [
                        "BULLISH",
                        "BUY",
                    ]:
                        trend_str = f"⚠️ BULL NET{memory_net_pips:+.0f}"
                    elif memory_trend_direction == "RANGE":
                        if abs(memory_net_pips) < 5:
                            trend_str = f"⚪ FLAT NET{memory_net_pips:+.0f}"
                        else:
                            if of_bias in ["BULLISH", "BUY"]:
                                trend_str = f"⚪ BULL NET{memory_net_pips:+.0f}"
                            elif of_bias in ["BEARISH", "SELL"]:
                                trend_str = f"⚪ BEAR NET{memory_net_pips:+.0f}"
                            else:
                                trend_str = f"⚪ NEU NET{memory_net_pips:+.0f}"
                    else:
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

                    report_data = {
                        "asset": asset,
                        "cycle": cycle_count,
                        "timestamp": time.time(),
                        "regime": str(current_regime).upper(),
                        "regime_strength": regime_strength,
                        "trend": trend_str,
                        "delta": delta_total,
                        "of_score": of_score,
                        "of_bias": of_bias,
                        "timing": timing_status,
                        "timing_reason": timing_reason,
                        "action": action,
                        "confidence": confidence,
                        "tick_count": tick_count,
                        "rationale": rationale,
                    }

                    try:
                        display_queue.put(report_data, block=False)
                    except queue.Full:
                        logger.warning(
                            f"[{asset}] Display queue pleine, rapport ignoré"
                        )

                except Exception as e_report:
                    logger.warning(
                        f"[{asset}] Erreur génération rapport: {e_report}",
                        exc_info=True,
                    )

                # Si signal valide → Exécution
                if fusion_out.get("ok") and trade_decision_skeleton is not None:
                    # BURST GUARD: Vérifier si un panier burst est déjà ouvert
                    open_burst_ids = set()
                    try:
                        current_positions = mt5_connector.get_positions() or []
                        for p in current_positions:
                            c = (
                                p.get("comment")
                                if isinstance(p, dict)
                                else getattr(p, "comment", "")
                            )
                            m = re.search(r"bs_([a-f0-9]{8})", str(c or ""))
                            if m:
                                open_burst_ids.add(m.group(1))

                        if open_burst_ids:
                            logger.warning(
                                f"⛔ [{asset}][BURST_GUARD] Panier(s) déjà ouvert(s): {open_burst_ids} | "
                                f"Signal {fusion_out.get('action')} IGNORÉ (single_burst_global)"
                            )
                    except Exception as e_guard:
                        logger.error(
                            f"❌ [{asset}][BURST_GUARD] Erreur vérification: {e_guard}"
                        )

                    # Exécuter SEULEMENT si aucun panier ouvert
                    if not open_burst_ids:
                        side = fusion_out["action"]
                        conf = fusion_out.get("fused_confidence", 0.0)

                        logger.info(
                            f"🎯 [{asset}] Signal {asset} {side} (conf={conf:.2f})"
                        )

                        # OPTION 1: INJECTION RAPIDE — Utiliser squelette pré-calculé
                        try:
                            skeleton = trade_decision_skeleton["static"]

                            td = dict(skeleton)
                            td["action"] = side
                            td["side"] = side
                            td["confidence"] = conf
                            td["context"] = ctx
                            td["fusion_data"] = fusion_out
                            td["order"] = {
                                "action": side,
                                "side": side,
                                "type": "MARKET",
                                "symbol": asset,
                            }
                            td["trade"] = {"action": side, "side": side}

                            with context_lock:
                                ctx_copy = dict(ctx)

                            decision_pkg = {
                                "final_decision": td,
                                "market_context": ctx_copy,
                                "active_config": trade_decision_skeleton[
                                    "merged_config"
                                ],
                            }
                            decision_pkg.setdefault("audit_context", {}).update(
                                {
                                    "intent_symbol": asset,
                                    "intent_side": side,
                                    "intent_burst": trade_decision_skeleton[
                                        "resolved_burst"
                                    ],
                                }
                            )

                            logger.info(
                                f"⚡ [PRE-CALC] Exécution RAPIDE: {side} {asset} burst={trade_decision_skeleton['resolved_burst']}"
                            )

                            res = run_trade_execution_pipeline(
                                trade_executor, decision_pkg, is_dry_run=is_dry_run
                            )
                            if res:
                                logger.info(
                                    f"✅ [{asset}] Trade exécuté: {res.get('status')}"
                                )
                            else:
                                logger.warning(
                                    f"⚠️ [{asset}] Trade non exécuté (res=None)"
                                )

                        except Exception as e:
                            logger.error(
                                f"[{asset}] Erreur exécution trade: {e}", exc_info=True
                            )

        except Exception as e:
            # Gestion des erreurs du cycle principal (Analyse, Décision, etc.)
            logger.error(f"[{asset}] Erreur cycle #{cycle_count}: {e}", exc_info=True)
            global_state.record_error(asset, str(e))

        # =========================================================================
        # 🛡️ MONITORING PERMANENT
        # =========================================================================
        try:
            if trade_executor:
                # 1. Lecture intelligente via ConfigMerger (Base + Overrides)
                closure_rules = config_manager.get_closure_rules_config(
                    asset, "scalping"
                )

                # 2. Vérification que closure_rules existe et est activé
                if not closure_rules:
                    logger.warning(f"[{asset}] ⚠️ closure_rules non trouvé pour {asset}")
                    closure_rules = {"enabled": False}

                enabled = closure_rules.get("enabled", False)

                if not enabled:
                    logger.debug(
                        f"[{asset}] ⚠️ closure_rules désactivé, skip monitoring"
                    )
                    # On peut quand même appeler monitor_burst_baskets avec valeurs par défaut
                    # ou simplement return si vous voulez éviter l'appel
                    # continue  # selon votre logique

                # 3. Extraction des valeurs avec sécurité
                target_val = float(closure_rules.get("target_profit_pips", 15.0))
                loss_val = float(closure_rules.get("max_loss_pips", 90.0))

                # 🔍 DEBUG CRITIQUE : Afficher les valeurs lues
                logger.info(f"🎯 [CLOSURE_CONFIG] {asset}:")
                logger.info(f"   • enabled: {enabled}")
                logger.info(f"   • target_profit_pips: {target_val} pips")
                logger.info(f"   • max_loss_pips: {loss_val} pips")

                # 4. Préparation de la config - CORRECTION ICI !
                # On doit définir base_config si trade_decision_skeleton n'existe pas
                if (
                    trade_decision_skeleton
                    and "merged_config" in trade_decision_skeleton
                ):
                    current_config = trade_decision_skeleton["merged_config"]
                    logger.debug(
                        f"[{asset}] ✅ Utilisation config depuis trade_decision_skeleton"
                    )
                else:
                    # Charger la config de base dynamiquement
                    try:
                        current_config = config_manager.get_current_dynamic_config()
                        logger.debug(
                            f"[{asset}] ✅ Utilisation config depuis config_manager"
                        )
                    except Exception as e:
                        logger.warning(f"[{asset}] ⚠️ Impossible de charger config: {e}")
                        current_config = {}  # Config vide comme fallback

                if not current_config:
                    logger.warning(
                        f"[{asset}] ⚠️ Aucune configuration disponible pour monitor_burst_baskets"
                    )
                    current_config = {}

                # 5. Envoi à burst.py avec TOUS les paramètres nécessaires
                # 🔥 AJOUTEZ CE DEBUG POUR CONFIRMER L'APPEL
                logger.critical("🔴🔴🔴 [MONITOR_CALL] 🔴🔴🔴")
                logger.critical(f"   Asset: {asset}")
                logger.critical(f"   target_profit_pips: {target_val}")
                logger.critical(f"   max_loss_pips: {loss_val}")
                logger.critical(f"   config utilisée: {bool(current_config)}")
                logger.critical("🔴🔴🔴🔴🔴🔴🔴🔴🔴")

                trade_executor.monitor_burst_baskets(
                    config=current_config,
                    max_loss_pips=loss_val,  # Paramètre de protection perte
                    target_profit_pips=target_val,  # Paramètre de prise de profit
                    config_manager_instance=config_manager,  # Pour lecture config spécifique
                    # Autres paramètres optionnels :
                    trail_trigger=10.0,  # Valeur par défaut
                    trail_step=5.0,  # Valeur par défaut
                )

                logger.debug(
                    f"[{asset}] ✅ monitor_burst_baskets appelé avec target={target_val}p, loss={loss_val}p"
                )

        except Exception as e_mon:
            # On log juste un warning pour ne pas crasher la boucle worker
            logger.warning(
                f"[{asset}] ⚠️ Erreur Monitor Baskets: {e_mon}", exc_info=True
            )
        # =========================================================================

        # Sleep dynamique
        elapsed = time.time() - cycle_start
        sleep_time = max(0, cycle_interval - elapsed)
        if sleep_time > 0:
            stop_event.wait(timeout=sleep_time)

        logger.info(f"🛑 [{asset}] Worker arrêté")
