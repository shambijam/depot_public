# -*- coding: utf-8 -*-
"""
Cache intelligent pour régimes marché.

Principe :
- Régime change rarement (1 bougie M1 = changement minimal)
- Recalculer seulement toutes les N secondes (configurable)
- Entre-temps : retourner régime cached

Utilisateurs :
- LIQUIDITY Thread : Update cache (régime 200 barres, toutes les 60s)
- SCALPING Thread : Read cache + régime lite (30 barres, toutes les 5s)

Date : 2025-12-11
Version : Phase 2 - Optimisation Cache Multi-Niveaux
"""

import threading
import time
from typing import Any, Callable, Dict, Optional


class RegimeCache:
    """
    Cache thread-safe pour régimes marché.

    TTL configurable (défaut 60s = 1 bougie M1).
    """

    def __init__(self):
        """
        Initialise le cache vide.

        Structure interne :
        {
            symbol: {
                "regime": str,              # TRENDING / BALANCED / etc.
                "confidence": float,        # 0.0-1.0
                "timestamp": float,         # time.time() dernière màj
                "source": str               # "LITE" ou "FULL"
            }
        }
        """
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def get(
        self, symbol: str, max_age_seconds: float = 60.0
    ) -> Optional[Dict[str, Any]]:
        """
        Récupère régime depuis cache.

        Args:
            symbol: Symbole (ex: XAUUSD)
            max_age_seconds: Âge maximum accepté (défaut 60s)

        Returns:
            {
                "regime": str,
                "confidence": float,
                "age_seconds": float,
                "source": str
            }
            ou None si cache expiré/inexistant
        """
        with self._lock:
            cached = self._cache.get(symbol)

            if not cached:
                return None

            now = time.time()
            age = now - cached["timestamp"]

            # Cache expiré
            if age > max_age_seconds:
                return None

            return {
                "regime": cached["regime"],
                "confidence": cached["confidence"],
                "age_seconds": age,
                "source": cached.get("source", "UNKNOWN"),
            }

    def update(
        self, symbol: str, regime: str, confidence: float, source: str = "FULL"
    ):
        """
        Mise à jour cache régime.

        Args:
            symbol: Symbole (ex: XAUUSD)
            regime: Régime (TRENDING / BALANCED / etc.)
            confidence: Confiance 0.0-1.0
            source: Source régime ("LITE" ou "FULL")
        """
        with self._lock:
            self._cache[symbol] = {
                "regime": regime,
                "confidence": confidence,
                "timestamp": time.time(),
                "source": source,
            }

    def get_or_calculate(
        self,
        symbol: str,
        df: Any,
        detector_func: Callable,
        recalc_interval: float = 60.0,
        source: str = "LITE",
    ) -> Dict[str, Any]:
        """
        Retourne régime depuis cache ou calcule si nécessaire.

        Args:
            symbol: Symbole (ex: XAUUSD)
            df: DataFrame barres M1
            detector_func: Fonction calcul régime (ex: RegimeDetectorLite.detect)
            recalc_interval: Intervalle recalcul (défaut 60s)
            source: Source régime ("LITE" ou "FULL")

        Returns:
            {
                "regime": str,
                "confidence": float,
                "age_seconds": float,
                "source": str
            }
        """
        with self._lock:
            now = time.time()
            cached = self._cache.get(symbol)

            # Cache MISS ou EXPIRÉ
            if not cached or (now - cached["timestamp"]) > recalc_interval:
                regime_result = detector_func(df)  # Calcul régime
                self._cache[symbol] = {
                    "regime": regime_result["regime"],
                    "confidence": regime_result["confidence"],
                    "timestamp": now,
                    "source": source,
                }
                return {
                    "regime": regime_result["regime"],
                    "confidence": regime_result["confidence"],
                    "age_seconds": 0.0,
                    "source": source,
                }

            # Cache HIT → Retourner régime cached
            age = now - cached["timestamp"]
            return {
                "regime": cached["regime"],
                "confidence": cached["confidence"],
                "age_seconds": age,
                "source": cached.get("source", "UNKNOWN"),
            }

    def invalidate(self, symbol: str):
        """Force rechargement au prochain appel."""
        with self._lock:
            if symbol in self._cache:
                del self._cache[symbol]

    def invalidate_all(self):
        """Invalide tout le cache."""
        with self._lock:
            self._cache.clear()

    def get_stats(self) -> Dict[str, Any]:
        """
        Retourne statistiques cache (pour monitoring).

        Returns:
            {
                "symbols": List[str],
                "count": int,
                "ages": Dict[str, float]
            }
        """
        with self._lock:
            now = time.time()
            return {
                "symbols": list(self._cache.keys()),
                "count": len(self._cache),
                "ages": {
                    sym: now - cached["timestamp"]
                    for sym, cached in self._cache.items()
                },
            }


# Singleton global
regime_cache = RegimeCache()
