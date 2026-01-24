#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
orchestration/workers/basket_monitor.py - Thread de surveillance des baskets burst
"""

import logging
import time
import threading


def basket_monitor_thread(
    trade_executor,
    config_manager,
    strategy_manager,
    stop_event: threading.Event,
    logger
):
    """
    Thread dédié à la SURVEILLANCE CONTINUE des baskets burst.
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
        
        # ✅ CORRECTION AJOUTÉE : DEBUG CONFIG CRITIQUE
        logger.info("=" * 80)
        logger.info("🔍 [BASKET_MONITOR_DEBUG] Test lecture closure_rules:")
        
        test_symbols = ["USDJPY", "GBPUSD", "NAS100"]
        for symbol in test_symbols:
            try:
                rules = config_manager.get_closure_rules_config(symbol, "scalping")
                if rules:
                    target = rules.get("target_profit_pips", "NON TROUVÉ")
                    enabled = rules.get("enabled", False)
                    logger.info(f"   {symbol}: enabled={enabled}, target_profit_pips={target}")
                else:
                    logger.warning(f"   {symbol}: AUCUNE RÈLE TROUVÉE!")
            except Exception as e:
                logger.error(f"   {symbol}: ERREUR: {e}")
        logger.info("=" * 80)

    # ✅ CORRECTION: Vérifier closure_rules avec config_manager
    try:
        # Tester avec un symbole connu (USDJPY)
        test_symbol = "USDJPY"
        test_closure = config_manager.get_closure_rules_config(test_symbol, "scalping")
        
        closure_enabled = test_closure.get("enabled", False)
        target_profit = test_closure.get("target_profit_pips", "N/A")
        max_loss = test_closure.get("max_loss_pips", "N/A")
        
        logger.info(f"✅ [BASKET_MONITOR] Lecture closure_rules depuis config_manager:")
        logger.info(f"   - Symbol test: {test_symbol}")
        logger.info(f"   - enabled: {closure_enabled}")
        logger.info(f"   - target_profit_pips: {target_profit}")
        logger.info(f"   - max_loss_pips: {max_loss}")
        
        if closure_enabled:
            logger.info("✅ [BASKET_MONITOR] closure_rules.enabled=True → Surveillance active")
        else:
            logger.info("⛔ [BASKET_MONITOR] closure_rules.enabled=False → Surveillance désactivée (mais thread continue)")
            
    except Exception as e:
        logger.error(f"❌ [BASKET_MONITOR] Erreur lecture closure_rules: {e}")
        closure_enabled = False

    poll_counter = 0
    while not stop_event.is_set():
        try:
            poll_counter += 1
            
            # DEBUG: Log toutes les 100 itérations (environ 10 secondes)
            if poll_counter % 100 == 0:
                logger.info(f"🔁 [BASKET_MONITOR] Polling #{poll_counter} (thread actif)")
            
            # ✅ CORRECTION: Passer config_manager_instance
            trade_executor.monitor_burst_baskets(
                config=merged_config,
                config_manager_instance=config_manager  # ⚠️ CRITIQUE !
            )

        except Exception as e:
            logger.error(f"❌ [BASKET_MONITOR] Erreur: {e}", exc_info=True)
            time.sleep(1)  # Éviter spam en cas d'erreur
            
        # Intervalle de polling
        time.sleep(0.1)  # 100ms

    logger.info("🛑 [BASKET_MONITOR_THREAD] Arrêté proprement")