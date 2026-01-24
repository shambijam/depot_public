#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
orchestration/threading_state.py - État partagé entre threads scalping

Contient:
- GlobalScalpingState: Classe thread-safe pour gérer l'état partagé entre les 3 threads scalping
"""

import threading
import time


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
