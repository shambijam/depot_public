# -*- coding: utf-8 -*-
"""
RegimeResolver - Système hybride régime marché.

Combine :
- RegimeDetectorLite (30 barres, rapide, SCALPING 5s)
- Régime complet cached (200 barres, lent, LIQUIDITY 60s)

Logique :
1. SCALPING Thread (5s) :
   - Calcule régime LITE (30 barres) si cache FULL expiré
   - Combine régime LITE + cache FULL si disponible
   - Retourne régime hybride avec confiance pondérée

2. LIQUIDITY Thread (60s) :
   - Calcule régime FULL (200 barres)
   - Update cache pour SCALPING

Avantages :
- Latence minimale SCALPING (régime lite ultra-rapide)
- Précision régime FULL (200 barres) quand disponible
- Fallback régime LITE si cache expiré

Date : 2025-12-11
Version : Phase 2 - Optimisation Cache Multi-Niveaux
"""

from typing import Dict, Any, Optional
import pandas as pd

from phase_observer.regime_detector_lite import RegimeDetectorLite
from phase_observer.regime_cache import regime_cache


class RegimeResolver:
    """
    Résolveur hybride régime marché.

    Combine détection rapide (30 bars) + cache précis (200 bars).
    """

    def __init__(self, logger=None):
        """
        Initialise le resolver.

        Args:
            logger: Logger optionnel
        """
        self.logger = logger
        self.detector_lite = RegimeDetectorLite(logger=logger)

    def resolve_regime(
        self,
        symbol: str,
        df_lite: pd.DataFrame,
        prefer_cache: bool = True,
        cache_max_age: float = 60.0,
    ) -> Dict[str, Any]:
        """
        Résout le régime marché (système hybride).

        Logique :
        1. Si cache FULL valide (< 60s) :
           → Retourner cache FULL (précis)

        2. Si cache FULL expiré ou inexistant :
           → Calculer régime LITE (30 barres)
           → Retourner régime LITE

        3. Si prefer_cache=False :
           → Toujours calculer LITE (ignore cache)

        Args:
            symbol: Symbole (ex: XAUUSD)
            df_lite: DataFrame 30 barres M1
            prefer_cache: Préférer cache FULL si disponible (défaut True)
            cache_max_age: Âge maximum cache accepté (défaut 60s)

        Returns:
            {
                "regime": str,              # TRENDING / BALANCED / etc.
                "confidence": float,        # 0.0-1.0
                "source": str,              # "FULL" / "LITE" / "HYBRID"
                "age_seconds": float        # Âge cache (0.0 si LITE)
            }
        """
        # --- OPTION 1: Préférer cache FULL ---
        if prefer_cache:
            cached_regime = regime_cache.get(symbol, max_age_seconds=cache_max_age)

            if cached_regime:
                # Cache FULL valide → Retourner directement
                return {
                    "regime": cached_regime["regime"],
                    "confidence": cached_regime["confidence"],
                    "source": cached_regime["source"],  # "FULL"
                    "age_seconds": cached_regime["age_seconds"],
                }

        # --- OPTION 2: Calculer régime LITE (30 barres) ---
        regime_lite = self.detector_lite.detect_regime(df_lite, min_bars=20)

        return {
            "regime": regime_lite["regime"],
            "confidence": regime_lite["confidence"],
            "source": "LITE",
            "age_seconds": 0.0,
        }

    def update_cache_full(
        self,
        symbol: str,
        regime: str,
        confidence: float,
    ):
        """
        Mise à jour cache régime FULL (depuis LIQUIDITY Thread).

        Args:
            symbol: Symbole (ex: XAUUSD)
            regime: Régime (TRENDING / BALANCED / etc.)
            confidence: Confiance 0.0-1.0
        """
        regime_cache.update(
            symbol=symbol,
            regime=regime,
            confidence=confidence,
            source="FULL",
        )

        if self.logger:
            self.logger.info(
                f"[RegimeResolver] Cache FULL mis à jour: {symbol} → {regime} (conf={confidence:.2f})"
            )

    def get_cached_regime(
        self, symbol: str, max_age_seconds: float = 60.0
    ) -> Optional[Dict[str, Any]]:
        """
        Récupère régime depuis cache uniquement (sans calcul).

        Args:
            symbol: Symbole
            max_age_seconds: Âge maximum accepté

        Returns:
            {"regime": str, "confidence": float, "source": str, "age_seconds": float}
            ou None si cache inexistant/expiré
        """
        return regime_cache.get(symbol, max_age_seconds=max_age_seconds)

    def invalidate_cache(self, symbol: str):
        """Invalide cache régime pour un symbole."""
        regime_cache.invalidate(symbol)


# Singleton global
regime_resolver = RegimeResolver()
