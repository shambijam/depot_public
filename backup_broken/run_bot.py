#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
run_bot.py - FACADE de rétro-compatibilité pour SNIPER_X Bot

Ce fichier est une façade qui re-exporte les fonctions depuis le package orchestration/.
Cela permet de maintenir la compatibilité avec les imports existants dans cli.py et main.py.

Architecture (17 JAN 2026):
- orchestration/bot_main.py       : Fonction main()
- orchestration/pipeline_cycle.py : run_single_pipeline_cycle()
- orchestration/environment.py    : verify_environment_and_config(), load_and_verify_environment()
- orchestration/threading_state.py: GlobalScalpingState
- orchestration/pipeline_helpers.py: Fonctions utilitaires
- orchestration/workers/          : scalping_worker, dashboard_worker, basket_monitor

Usage:
    from run_bot import main  # Pour cli.py
    from run_bot import run_single_pipeline_cycle  # Pour main.py
"""

# =============================================================================
# EXPORTS PUBLICS (rétro-compatibilité avec cli.py et main.py)
# =============================================================================

# Fonction principale (utilisée par cli.py)
from orchestration.bot_main import main

# Pipeline cycle (utilisé par main.py)
from orchestration.pipeline_cycle import run_single_pipeline_cycle

# Vérification environnement (utilisé par main.py)
from orchestration.environment import (
    verify_environment_and_config,
    load_and_verify_environment,
)

# State partagé (usage interne et tests)
from orchestration.threading_state import GlobalScalpingState

# Helpers (usage interne)
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

# Workers (usage interne)
from orchestration.workers.scalping_worker import scalping_worker
from orchestration.workers.dashboard_worker import dashboard_worker
from orchestration.workers.basket_monitor import basket_monitor_thread


__all__ = [
    # Fonctions principales
    "main",
    "run_single_pipeline_cycle",
    "verify_environment_and_config",
    "load_and_verify_environment",
    # Classes
    "GlobalScalpingState",
    # Helpers
    "_deep_merge_dicts",
    "_get_merged_config_for_asset",
    "_is_market_closed",
    "_build_asset_trading_signals",
    "_build_asset_market_data",
    "_build_global_context",
    "_load_po_config_safe",
    "_execute_single_decision",
    # Workers
    "scalping_worker",
    "dashboard_worker",
    "basket_monitor_thread",
]
