"""
FootprintCache - Cache Thread-Safe pour Données Footprint

Ce module fournit un cache partagé entre threads pour les données footprint,
permettant au thread SCALPING de lire les données sans bloquer sur l'analyse.

Architecture:
- DataEngine Thread → Calcule footprint → update(symbol, data)
- SCALPING Thread  → Lit cache      → get(symbol)

Date: 26 Novembre 2025
"""

import threading
import time
import logging
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


class FootprintCache:
    """
    Cache thread-safe pour stocker les données footprint par symbole.

    Utilise un verrou (Lock) pour garantir la cohérence entre threads.
    Chaque entrée contient:
    - data: Les données footprint complètes
    - timestamp: Moment de la mise à jour (pour détection données obsolètes)
    """

    def __init__(self):
        """Initialise le cache vide avec un verrou thread-safe."""
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        logger.info("✅ FootprintCache initialisé")

    def update(self, symbol: str, data: Dict[str, Any]) -> None:
        """
        Met à jour les données footprint pour un symbole.

        Args:
            symbol: Symbole (ex: XAUUSD, EURUSD)
            data: Dictionnaire contenant les données footprint
                  {
                      'footprint_df': DataFrame,
                      'footprint_summary': {...},
                      'trigger': {...},
                      'analysis_time_ms': float
                  }
        """
        with self._lock:
            self._cache[symbol] = {
                'data': data,
                'timestamp': time.time()
            }

            # Log de debug
            ticks = data.get('footprint_summary', {}).get('tick_count', 0)
            coverage = data.get('footprint_summary', {}).get('coverage_s', 0)
            logger.debug(
                f"📦 [CACHE_UPDATE] {symbol} | ticks={ticks} | coverage={coverage:.1f}s"
            )

    def get(self, symbol: str, max_age_seconds: float = 15.0) -> Optional[Dict[str, Any]]:
        """
        Récupère les données footprint pour un symbole.

        Args:
            symbol: Symbole (ex: XAUUSD)
            max_age_seconds: Âge maximum acceptable des données (défaut: 15s)
                            Si données plus vieilles, retourne None

        Returns:
            Dict contenant les données footprint si disponibles et récentes,
            None sinon
        """
        with self._lock:
            if symbol not in self._cache:
                logger.debug(f"⚠️ [CACHE_MISS] {symbol} | raison=pas_de_donnees")
                return None

            entry = self._cache[symbol]
            age_seconds = time.time() - entry['timestamp']

            if age_seconds > max_age_seconds:
                logger.debug(
                    f"⚠️ [CACHE_STALE] {symbol} | age={age_seconds:.1f}s > max={max_age_seconds}s"
                )
                return None

            # Log succès
            logger.debug(
                f"✅ [CACHE_HIT] {symbol} | age={age_seconds:.1f}s | fresh=True"
            )
            return entry['data']

    def get_age(self, symbol: str) -> Optional[float]:
        """
        Retourne l'âge des données en secondes (pour diagnostic).

        Args:
            symbol: Symbole

        Returns:
            Âge en secondes, ou None si pas de données
        """
        with self._lock:
            if symbol not in self._cache:
                return None
            return time.time() - self._cache[symbol]['timestamp']

    def get_all_ages(self) -> Dict[str, float]:
        """
        Retourne l'âge de toutes les données en cache (pour monitoring).

        Returns:
            Dict {symbol: age_seconds}
        """
        with self._lock:
            current_time = time.time()
            return {
                symbol: current_time - entry['timestamp']
                for symbol, entry in self._cache.items()
            }

    def clear(self, symbol: Optional[str] = None) -> None:
        """
        Nettoie le cache.

        Args:
            symbol: Si fourni, nettoie seulement ce symbole
                   Si None, nettoie tout le cache
        """
        with self._lock:
            if symbol:
                if symbol in self._cache:
                    del self._cache[symbol]
                    logger.info(f"🗑️ [CACHE_CLEAR] {symbol}")
            else:
                self._cache.clear()
                logger.info("🗑️ [CACHE_CLEAR] Tout le cache nettoyé")

    def get_stats(self) -> Dict[str, Any]:
        """
        Retourne des statistiques sur le cache (pour monitoring).

        Returns:
            Dict contenant:
            - symbols: Liste des symboles en cache
            - count: Nombre d'entrées
            - ages: Âge de chaque entrée
            - oldest: Entrée la plus vieille
            - newest: Entrée la plus récente
        """
        with self._lock:
            if not self._cache:
                return {
                    'symbols': [],
                    'count': 0,
                    'ages': {},
                    'oldest': None,
                    'newest': None
                }

            current_time = time.time()
            ages = {
                symbol: current_time - entry['timestamp']
                for symbol, entry in self._cache.items()
            }

            return {
                'symbols': list(self._cache.keys()),
                'count': len(self._cache),
                'ages': ages,
                'oldest': max(ages.values()) if ages else None,
                'newest': min(ages.values()) if ages else None
            }


# Instance globale partagée entre tous les threads
footprint_cache = FootprintCache()
