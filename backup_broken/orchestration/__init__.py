#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
orchestration/ - Package pour l'orchestration du bot SNIPER_X

Ce package contient les modules extraits de run_bot.py pour améliorer
la maintenabilité et la testabilité du code.

Exports publics (lazy loading pour performance):
- main: Fonction principale pour lancer le bot
- run_single_pipeline_cycle: Exécute un cycle complet du pipeline
- verify_environment_and_config: Vérifie l'environnement et la config
- load_and_verify_environment: Charge et vérifie l'environnement
- GlobalScalpingState: Classe de state partagé entre threads
"""

# Lazy imports pour éviter les imports circulaires et améliorer les temps de démarrage


def __getattr__(name):
    """Lazy loading des modules pour optimiser le temps de démarrage."""

    if name == "main":
        from orchestration.bot_main import main
        return main

    if name == "run_single_pipeline_cycle":
        from orchestration.pipeline_cycle import run_single_pipeline_cycle
        return run_single_pipeline_cycle

    if name in ("verify_environment_and_config", "load_and_verify_environment"):
        from orchestration import environment
        return getattr(environment, name)

    if name == "GlobalScalpingState":
        from orchestration.threading_state import GlobalScalpingState
        return GlobalScalpingState

    # Helpers (usage interne principalement)
    if name in (
        "_deep_merge_dicts",
        "_get_merged_config_for_asset",
        "_is_market_closed",
        "_build_asset_trading_signals",
        "_build_asset_market_data",
        "_build_global_context",
        "_load_po_config_safe",
        "_execute_single_decision",
    ):
        from orchestration import pipeline_helpers
        return getattr(pipeline_helpers, name)

    raise AttributeError(f"module 'orchestration' has no attribute '{name}'")


# Définir __all__ pour l'autocomplétion et la documentation
__all__ = [
    # Fonctions principales
    "main",
    "run_single_pipeline_cycle",
    "verify_environment_and_config",
    "load_and_verify_environment",
    # Classes
    "GlobalScalpingState",
    # Helpers (usage interne)
    "_deep_merge_dicts",
    "_get_merged_config_for_asset",
    "_is_market_closed",
    "_build_asset_trading_signals",
    "_build_asset_market_data",
    "_build_global_context",
    "_load_po_config_safe",
    "_execute_single_decision",
]
