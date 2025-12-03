# phase_observer/vwap/cache.py
"""
Système de cache multi-niveaux pour VWAP
L1: Mémoire ultra-rapide (TTL 1s)
L2: Mémoire partagée (TTL 300s)
L3: Stockage persistant (future)
"""

import logging
import time
from typing import Dict, Any, Optional, List, Tuple
from datetime import datetime, timedelta
from collections import OrderedDict
from threading import Lock

from .models import (
    VWAPAnalysisResult,
    VWAPDerivatives,
    VWAPSignal,
    CacheEntry
)
from .config import VWAPConfig


logger = logging.getLogger(__name__)


class VWAPCache:
    """
    Cache multi-niveaux pour résultats VWAP
    Thread-safe avec éviction LRU
    """

    def __init__(self, config: VWAPConfig):
        """
        Initialise le cache

        Args:
            config: Configuration VWAP
        """
        self.config = config
        self.symbol = config.symbol
        self.logger = logging.getLogger(f"{__name__}.{self.symbol}")

        # Configuration cache
        self.l1_ttl = config.cache.l1_ttl_seconds
        self.l2_ttl = config.cache.l2_ttl_seconds
        self.max_size = config.cache.max_cache_size

        # L1 Cache (ultra-rapide, 1s TTL)
        self.l1_cache: OrderedDict[str, CacheEntry] = OrderedDict()
        self.l1_lock = Lock()

        # L2 Cache (partagé, 300s TTL)
        self.l2_cache: OrderedDict[str, CacheEntry] = OrderedDict()
        self.l2_lock = Lock()

        # Métriques
        self.metrics = {
            'l1_hits': 0,
            'l1_misses': 0,
            'l2_hits': 0,
            'l2_misses': 0,
            'total_hits': 0,
            'total_misses': 0,
            'evictions': 0,
            'stores': 0,
        }

        self.logger.info(
            f"[VWAP_CACHE] Initialisé | "
            f"L1_TTL={self.l1_ttl}s | L2_TTL={self.l2_ttl}s | "
            f"Max_size={self.max_size}"
        )

    def get(
        self,
        key: str,
        default: Any = None
    ) -> Optional[Any]:
        """
        Récupère une entrée du cache (L1 puis L2)

        Args:
            key: Clé de cache
            default: Valeur par défaut si absent

        Returns:
            Valeur ou default
        """
        # 1. Essai L1
        value = self._get_l1(key)
        if value is not None:
            self.metrics['l1_hits'] += 1
            self.metrics['total_hits'] += 1
            return value

        self.metrics['l1_misses'] += 1

        # 2. Essai L2
        value = self._get_l2(key)
        if value is not None:
            self.metrics['l2_hits'] += 1
            self.metrics['total_hits'] += 1
            # Promote to L1
            self._set_l1(key, value, ttl=self.l1_ttl)
            return value

        self.metrics['l2_misses'] += 1
        self.metrics['total_misses'] += 1
        return default

    def set(
        self,
        key: str,
        value: Any,
        ttl: Optional[int] = None
    ) -> None:
        """
        Stocke une entrée dans le cache

        Args:
            key: Clé de cache
            value: Valeur à stocker
            ttl: TTL en secondes (None = L1_TTL)
        """
        ttl = ttl or self.l1_ttl

        # Store dans L1 (toujours)
        self._set_l1(key, value, ttl=ttl)

        # Store dans L2 si TTL > L1_TTL
        if ttl > self.l1_ttl:
            self._set_l2(key, value, ttl=ttl)

        self.metrics['stores'] += 1

    def invalidate(self, key: str) -> None:
        """
        Invalide une entrée du cache (L1 + L2)

        Args:
            key: Clé à invalider
        """
        with self.l1_lock:
            if key in self.l1_cache:
                del self.l1_cache[key]

        with self.l2_lock:
            if key in self.l2_cache:
                del self.l2_cache[key]

        self.logger.debug(f"[VWAP_CACHE] Invalidé: {key}")

    def clear(self) -> None:
        """Vide tous les caches"""
        with self.l1_lock:
            self.l1_cache.clear()

        with self.l2_lock:
            self.l2_cache.clear()

        self.logger.info("[VWAP_CACHE] Cache vidé")

    def cleanup_expired(self) -> int:
        """
        Nettoie les entrées expirées

        Returns:
            Nombre d'entrées supprimées
        """
        count = 0

        # L1 cleanup
        with self.l1_lock:
            expired_keys = [
                k for k, entry in self.l1_cache.items()
                if entry.is_expired
            ]
            for key in expired_keys:
                del self.l1_cache[key]
                count += 1

        # L2 cleanup
        with self.l2_lock:
            expired_keys = [
                k for k, entry in self.l2_cache.items()
                if entry.is_expired
            ]
            for key in expired_keys:
                del self.l2_cache[key]
                count += 1

        if count > 0:
            self.logger.debug(f"[VWAP_CACHE] Nettoyé {count} entrées expirées")

        return count

    def _get_l1(self, key: str) -> Optional[Any]:
        """Récupère depuis L1"""
        with self.l1_lock:
            if key not in self.l1_cache:
                return None

            entry = self.l1_cache[key]

            # Check expiration
            if entry.is_expired:
                del self.l1_cache[key]
                return None

            # LRU: move to end
            self.l1_cache.move_to_end(key)
            entry.hit_count += 1

            return entry.value

    def _get_l2(self, key: str) -> Optional[Any]:
        """Récupère depuis L2"""
        with self.l2_lock:
            if key not in self.l2_cache:
                return None

            entry = self.l2_cache[key]

            # Check expiration
            if entry.is_expired:
                del self.l2_cache[key]
                return None

            # LRU: move to end
            self.l2_cache.move_to_end(key)
            entry.hit_count += 1

            return entry.value

    def _set_l1(self, key: str, value: Any, ttl: int) -> None:
        """Stocke dans L1"""
        with self.l1_lock:
            # Éviction LRU si plein
            if len(self.l1_cache) >= self.max_size:
                # Remove oldest (first item)
                self.l1_cache.popitem(last=False)
                self.metrics['evictions'] += 1

            # Create entry
            entry = CacheEntry(
                key=key,
                value=value,
                timestamp=datetime.utcnow(),
                ttl_seconds=ttl,
                hit_count=0
            )

            self.l1_cache[key] = entry

    def _set_l2(self, key: str, value: Any, ttl: int) -> None:
        """Stocke dans L2"""
        with self.l2_lock:
            # Éviction LRU si plein
            if len(self.l2_cache) >= self.max_size:
                self.l2_cache.popitem(last=False)
                self.metrics['evictions'] += 1

            # Create entry
            entry = CacheEntry(
                key=key,
                value=value,
                timestamp=datetime.utcnow(),
                ttl_seconds=ttl,
                hit_count=0
            )

            self.l2_cache[key] = entry

    def get_metrics(self) -> Dict[str, Any]:
        """
        Retourne métriques du cache

        Returns:
            Dict avec stats
        """
        total_requests = self.metrics['total_hits'] + self.metrics['total_misses']
        hit_rate = (
            self.metrics['total_hits'] / total_requests
            if total_requests > 0 else 0.0
        )

        return {
            **self.metrics,
            'total_requests': total_requests,
            'hit_rate': hit_rate,
            'miss_rate': 1.0 - hit_rate,
            'l1_size': len(self.l1_cache),
            'l2_size': len(self.l2_cache),
        }

    def get_statistics(self) -> Dict[str, Any]:
        """
        Retourne statistiques détaillées

        Returns:
            Stats complètes
        """
        metrics = self.get_metrics()

        # Top entries par hit_count
        with self.l1_lock:
            l1_top = sorted(
                [(k, e.hit_count) for k, e in self.l1_cache.items()],
                key=lambda x: x[1],
                reverse=True
            )[:5]

        with self.l2_lock:
            l2_top = sorted(
                [(k, e.hit_count) for k, e in self.l2_cache.items()],
                key=lambda x: x[1],
                reverse=True
            )[:5]

        return {
            'metrics': metrics,
            'l1_top_entries': l1_top,
            'l2_top_entries': l2_top,
        }


class VWAPCacheManager:
    """
    Gestionnaire de cache VWAP avec clés typées
    Simplifie l'usage pour les composants VWAP
    """

    def __init__(self, config: VWAPConfig):
        """
        Initialise le gestionnaire de cache

        Args:
            config: Configuration VWAP
        """
        self.config = config
        self.symbol = config.symbol
        self.cache = VWAPCache(config)
        self.logger = logging.getLogger(f"{__name__}.{self.symbol}")

    def get_analysis_result(
        self,
        timeframe: str = "M1"
    ) -> Optional[VWAPAnalysisResult]:
        """
        Récupère dernier résultat d'analyse

        Args:
            timeframe: Timeframe (M1, M5, etc.)

        Returns:
            VWAPAnalysisResult ou None
        """
        key = self._make_key("analysis", timeframe)
        return self.cache.get(key)

    def set_analysis_result(
        self,
        result: VWAPAnalysisResult,
        timeframe: str = "M1"
    ) -> None:
        """
        Stocke résultat d'analyse

        Args:
            result: Résultat VWAP
            timeframe: Timeframe
        """
        key = self._make_key("analysis", timeframe)
        self.cache.set(key, result, ttl=self.config.cache.l1_ttl_seconds)

    def get_derivatives(
        self,
        timeframe: str = "M1"
    ) -> Optional[VWAPDerivatives]:
        """
        Récupère derniers dérivés

        Args:
            timeframe: Timeframe

        Returns:
            VWAPDerivatives ou None
        """
        key = self._make_key("derivatives", timeframe)
        return self.cache.get(key)

    def set_derivatives(
        self,
        derivatives: VWAPDerivatives,
        timeframe: str = "M1"
    ) -> None:
        """
        Stocke dérivés

        Args:
            derivatives: Dérivés VWAP
            timeframe: Timeframe
        """
        key = self._make_key("derivatives", timeframe)
        self.cache.set(key, derivatives, ttl=self.config.cache.l1_ttl_seconds)

    def get_signal(
        self,
        timeframe: str = "M1"
    ) -> Optional[VWAPSignal]:
        """
        Récupère dernier signal

        Args:
            timeframe: Timeframe

        Returns:
            VWAPSignal ou None
        """
        key = self._make_key("signal", timeframe)
        return self.cache.get(key)

    def set_signal(
        self,
        signal: VWAPSignal,
        timeframe: str = "M1"
    ) -> None:
        """
        Stocke signal

        Args:
            signal: Signal VWAP
            timeframe: Timeframe
        """
        key = self._make_key("signal", timeframe)
        self.cache.set(key, signal, ttl=self.config.cache.l1_ttl_seconds)

    def get_vwap_value(
        self,
        timeframe: str = "M1"
    ) -> Optional[float]:
        """
        Récupère valeur VWAP courante

        Args:
            timeframe: Timeframe

        Returns:
            Valeur VWAP ou None
        """
        key = self._make_key("vwap_value", timeframe)
        return self.cache.get(key)

    def set_vwap_value(
        self,
        vwap: float,
        timeframe: str = "M1"
    ) -> None:
        """
        Stocke valeur VWAP

        Args:
            vwap: Valeur VWAP
            timeframe: Timeframe
        """
        key = self._make_key("vwap_value", timeframe)
        # TTL plus long pour valeur VWAP (stable)
        self.cache.set(key, vwap, ttl=self.config.cache.l2_ttl_seconds)

    def invalidate_all(self, timeframe: Optional[str] = None) -> None:
        """
        Invalide toutes les entrées pour un timeframe

        Args:
            timeframe: Timeframe (None = tous)
        """
        if timeframe is None:
            self.cache.clear()
        else:
            # Invalide toutes les clés du timeframe
            for data_type in ["analysis", "derivatives", "signal", "vwap_value"]:
                key = self._make_key(data_type, timeframe)
                self.cache.invalidate(key)

    def cleanup_expired(self) -> int:
        """
        Nettoie les entrées expirées

        Returns:
            Nombre d'entrées supprimées
        """
        return self.cache.cleanup_expired()

    def _make_key(self, data_type: str, timeframe: str) -> str:
        """
        Crée une clé de cache typée

        Args:
            data_type: Type de données
            timeframe: Timeframe

        Returns:
            Clé formatée
        """
        return f"{self.symbol}:{timeframe}:{data_type}"

    def get_metrics(self) -> Dict[str, Any]:
        """Retourne métriques"""
        return self.cache.get_metrics()

    def get_statistics(self) -> Dict[str, Any]:
        """Retourne statistiques détaillées"""
        return self.cache.get_statistics()
