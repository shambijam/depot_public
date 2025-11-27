# trader/order_builder_parallel.py
"""
Module de parallélisation des validations pour order_builder.

Architecture:
- GROUPE 1: Validations indépendantes (parallèle)
- GROUPE 2: Calculs semi-dépendants (parallèle après Groupe 1)
- GROUPE 3: Validations finales (séquentiel)

Gains attendus: 200ms → 80ms (60% réduction)
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, Optional, Callable
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)

# Thread pool global pour exécution parallèle
_executor = ThreadPoolExecutor(max_workers=10)


async def run_validations_parallel(
    validate_fn: Callable,
    decision_package: dict,
    config_manager: Any,
    mt5_connector: Any,
) -> dict:
    """
    Exécute les validations de prepare_order() en parallèle.

    Interface wrapper qui orchestre les 3 groupes de validations.

    Args:
        validate_fn: Fonction prepare_order originale (pour fallback)
        decision_package: Package de décision
        config_manager: ConfigManager
        mt5_connector: MT5Connector

    Returns:
        dict: Requête MT5 prête à envoyer
    """
    start_time = time.time()

    try:
        # Pour cette première version, on garde une approche simple:
        # On va paralléliser UNIQUEMENT les appels MT5 les plus lents

        # ÉTAPE 1: Extraction données de base (rapide, séquentiel)
        trade_decision = (decision_package or {}).get("trade_decision", {}) or {}
        market_context = (decision_package or {}).get("market_context", {}) or {}

        # Extraire symbole
        raw_symbol = (
            trade_decision.get("asset")
            or trade_decision.get("symbol")
            or trade_decision.get("instrument")
            or ""
        ).strip().upper()

        if not raw_symbol or raw_symbol == "UNKNOWN":
            # Fallback synchrone si données manquantes
            return validate_fn(decision_package)

        # ÉTAPE 2: Appels MT5 en parallèle (GAIN PRINCIPAL)
        loop = asyncio.get_event_loop()

        async def get_symbol_info():
            """Récupère symbol_info MT5 (slow ~20-50ms)."""
            return await loop.run_in_executor(
                _executor,
                mt5_connector.get_symbol_info,
                raw_symbol
            )

        async def get_current_tick():
            """Récupère tick actuel MT5 (slow ~10-30ms)."""
            return await loop.run_in_executor(
                _executor,
                mt5_connector.get_tick,
                raw_symbol
            )

        # Lancer les 2 appels MT5 en PARALLÈLE
        symbol_info_task = get_symbol_info()
        tick_task = get_current_tick()

        # Attendre les 2 résultats simultanément
        symbol_info, tick = await asyncio.gather(
            symbol_info_task,
            tick_task,
            return_exceptions=False  # Propager erreurs
        )

        # ÉTAPE 3: Le reste du traitement en séquentiel
        # (pour cette v1, on optimise juste les appels MT5 qui sont le goulot)

        # Stocker dans decision_package pour que validate_fn les utilise
        if not market_context.get("_preloaded_symbol_info"):
            market_context["_preloaded_symbol_info"] = symbol_info
        if not market_context.get("_preloaded_tick"):
            market_context["_preloaded_tick"] = tick

        decision_package["market_context"] = market_context

        # Appeler la fonction originale qui va utiliser les données pré-chargées
        result = validate_fn(decision_package)

        elapsed = (time.time() - start_time) * 1000
        logger.info(f"⚡ [PARALLEL_VALID] Préparation terminée en {elapsed:.1f}ms")

        return result

    except Exception as e:
        elapsed = (time.time() - start_time) * 1000
        logger.error(f"❌ [PARALLEL_VALID] Erreur après {elapsed:.1f}ms: {e}")

        # FALLBACK: Si parallèle échoue, revenir à l'ancien système
        logger.warning(f"⚠️ [PARALLEL_VALID] Fallback mode séquentiel")
        return validate_fn(decision_package)


def prepare_order_parallel_wrapper(
    original_prepare_order: Callable,
    self,
    decision_package: dict
) -> dict:
    """
    Wrapper synchrone pour prepare_order qui lance la version parallèle.

    Interface identique à prepare_order() original, mais optimisé en interne.

    Args:
        original_prepare_order: Fonction prepare_order originale
        self: Instance TradeExecutor
        decision_package: Package de décision

    Returns:
        dict: Requête MT5
    """
    # Vérifier si parallélisation activée
    try:
        parallel_enabled = self.config_manager.get("order_builder_parallel", True)
    except Exception:
        parallel_enabled = True  # Par défaut = activé

    if not parallel_enabled:
        # Mode séquentiel classique
        return original_prepare_order(decision_package)

    # Mode parallèle
    try:
        # Lancer event loop async
        result = asyncio.run(
            run_validations_parallel(
                validate_fn=lambda pkg: original_prepare_order(pkg),
                decision_package=decision_package,
                config_manager=self.config_manager,
                mt5_connector=self.mt5_connector,
            )
        )
        return result

    except RuntimeError as e:
        # Si déjà dans un event loop, fallback synchrone
        if "already running" in str(e).lower():
            logger.warning(
                "⚠️ [PARALLEL_VALID] Event loop déjà actif, fallback séquentiel"
            )
            return original_prepare_order(decision_package)
        else:
            raise

    except Exception as e:
        logger.error(f"❌ [PARALLEL_VALID] Erreur critique: {e}", exc_info=True)
        # Fallback synchrone en cas d'erreur
        return original_prepare_order(decision_package)
