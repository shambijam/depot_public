# phase_observer/vwap/__init__.py
"""
Module VWAP Institutionnel pour SNIPER-X
Architecture multi-composants avec scoring 25 points

Point d'entrée principal: VWAPAnalyzer
Compatible intégration FusionManager

Usage:
    from phase_observer.vwap import VWAPAnalyzer, create_vwap_analyzer

    analyzer = create_vwap_analyzer("XAUUSD", strategy_config)
    result = analyzer.analyze(df, current_price)

    # result.score: 0.0 - 1.0 (normalisé pour FusionManager)
    # result.status: VALID, SUSPECT, INVALID
    # result.bias: BUY, SELL, NEUTRAL
"""

__version__ = "1.0.0"
__author__ = "SNIPER-X Trading System"
__description__ = "Module VWAP institutionnel avec derivatives, signals, cache, metrics"

import logging


# ==================== CORE EXPORTS ====================

# Analyseur principal (point d'entrée)
from .analyzer import (
    VWAPAnalyzer,
    create_vwap_analyzer,
)

# Configuration
from .config import (
    VWAPConfig,
    create_vwap_config,
    VWAPCalculationConfig,
    VWAPDerivativesConfig,
    VWAPSignalsConfig,
    VWAPAssetConfig,
)

# Modèles de données
from .models import (
    # Results
    VWAPAnalysisResult,
    VWAPDerivatives,
    VWAPSignal,

    # Data structures
    VWAPTick,
    VWAPState,

    # Enums
    VWAPZone,
    VWAPRegime,
    SignalAction,
    DataSource,
)

# Calculateurs
from .core import (
    VWAPCalculator,
    VWAPBatchCalculator,
)

from .derivatives import (
    VWAPDerivativesCalculator,
)

from .signals import (
    VWAPSignalGenerator,
)

# Infrastructure
from .cache import (
    VWAPCache,
    VWAPCacheManager,
)

from .metrics import (
    VWAPMetricsCollector,
    MetricsFormatter,
)

from .validators import (
    DataValidator,
    DataNormalizer,
    QualityScorer,
)

# Regime mapping
from .regime_mapper import (
    RegimeMapper,
    validate_regime_mapper,
)


# ==================== PUBLIC API ====================

__all__ = [
    # Main entry point
    "VWAPAnalyzer",
    "create_vwap_analyzer",

    # Configuration
    "VWAPConfig",
    "create_vwap_config",
    "VWAPCalculationConfig",
    "VWAPDerivativesConfig",
    "VWAPSignalsConfig",
    "VWAPAssetConfig",

    # Models
    "VWAPAnalysisResult",
    "VWAPDerivatives",
    "VWAPSignal",
    "VWAPTick",
    "VWAPState",

    # Enums
    "VWAPZone",
    "VWAPRegime",
    "SignalAction",
    "DataSource",

    # Calculators
    "VWAPCalculator",
    "VWAPBatchCalculator",
    "VWAPDerivativesCalculator",
    "VWAPSignalGenerator",

    # Infrastructure
    "VWAPCache",
    "VWAPCacheManager",
    "VWAPMetricsCollector",
    "MetricsFormatter",
    "DataValidator",
    "DataNormalizer",
    "QualityScorer",

    # Regime mapping
    "RegimeMapper",
    "validate_regime_mapper",
]


# ==================== MODULE INFO ====================

def get_module_info():
    """
    Retourne informations sur le module VWAP

    Returns:
        Dict avec version, description, composants
    """
    return {
        'name': 'VWAP Institutionnel',
        'version': __version__,
        'description': __description__,
        'author': __author__,
        'components': {
            'analyzer': 'Orchestrateur principal',
            'core': 'Calcul VWAP avec session management',
            'derivatives': 'Slopes, curvature, bands, régime detection',
            'signals': 'Génération signaux avec scoring 25pts',
            'cache': 'Cache multi-niveaux (L1/L2)',
            'metrics': 'Monitoring et health checks',
            'validators': 'Validation et normalisation données',
            'config': 'Configuration multi-asset',
            'models': 'Dataclasses et enums',
        },
        'supported_assets': ['XAUUSD', 'EURUSD', 'GBPUSD'],
        'scoring': {
            'max_score': 25,
            'trend_score': '0-15 points',
            'position_score': '0-10 points',
            'normalized': '0.0 - 1.0 pour FusionManager',
        },
        'integration': {
            'fusion_manager_weight': 0.25,
            'compatible_with': ['OrderFlow V6', 'Footprint M1'],
        },
    }


# ==================== QUICK START ====================

def quick_start_guide():
    """
    Affiche guide de démarrage rapide
    """
    guide = """
    ═══════════════════════════════════════════════════════════
    📊 VWAP MODULE INSTITUTIONNEL - QUICK START
    ═══════════════════════════════════════════════════════════

    1. IMPORT
    ─────────────────────────────────────────────────────────
    from phase_observer.vwap import create_vwap_analyzer

    2. CRÉATION ANALYSEUR
    ─────────────────────────────────────────────────────────
    analyzer = create_vwap_analyzer("XAUUSD", strategy_config)

    3. ANALYSE
    ─────────────────────────────────────────────────────────
    result = analyzer.analyze(df, current_price)

    # Résultat compatible FusionManager:
    # - result.score: 0.0 - 1.0 (normalisé)
    # - result.status: VALID, SUSPECT, INVALID
    # - result.bias: BUY, SELL, NEUTRAL
    # - result.vwap_value: Valeur VWAP
    # - result.distance_pips: Distance en pips
    # - result.slope: Pente VWAP
    # - result.zone: NEUTRAL, STRONG, EXTREME
    # - result.regime: ACCUMULATION, TRENDING, BALANCED, TRANSITIONAL

    4. DÉTAILS SIGNAL
    ─────────────────────────────────────────────────────────
    if result.signal:
        print(f"Signal: {result.signal.signal_type}")
        print(f"Action: {result.signal.action.value}")
        print(f"Trend score: {result.signal.trend_score}/15")
        print(f"Position score: {result.signal.position_score}/10")
        print(f"Confidence: {result.signal.confidence:.2f}")

    5. MÉTRIQUES
    ─────────────────────────────────────────────────────────
    metrics = analyzer.get_metrics()
    health = analyzer.get_health()

    6. INTÉGRATION FUSIONMANAGER
    ─────────────────────────────────────────────────────────
    # Dans fusion_manager.py:
    vwap_result = self.vwap_analyzer.analyze(df, current_price)
    vwap_score = vwap_result.score  # 0.0 - 1.0

    # Pondération: 25% du scoring total
    # OrderFlow: 50% + Footprint: 25% + VWAP: 25% = 100%

    ═══════════════════════════════════════════════════════════
    """
    print(guide)


# ==================== LOGGING ====================

logger = logging.getLogger(__name__)
logger.info(
    f"[VWAP_MODULE] Chargé | Version={__version__} | "
    f"Composants={len(get_module_info()['components'])}"
)
