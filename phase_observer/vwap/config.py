# phase_observer/vwap/config.py
"""
Configuration centralisée pour le module VWAP institutionnel
Paramètres par asset avec fallbacks intelligents
"""

from dataclasses import dataclass, field
from typing import Dict, Any, Optional
import logging


@dataclass
class VWAPCalculationConfig:
    """Configuration du calcul VWAP"""
    reset_hour: int = 0                    # Heure reset GMT (0 = minuit)
    reset_timezone: str = "GMT"
    typical_price_formula: str = "(bid + ask + last) / 3"
    volume_source: str = "tick_volume"
    min_ticks_for_valid_vwap: int = 10    # Minimum ticks requis
    decimal_places: int = 5                # Précision calcul


@dataclass
class VWAPDerivativesConfig:
    """Configuration des dérivés VWAP"""
    window_sizes: list = field(default_factory=lambda: [20, 50, 100, 200])
    bands_std_dev: list = field(default_factory=lambda: [1.0, 2.0])
    slope_threshold: float = 0.001         # Seuil pente significative
    curvature_window: int = 50             # Fenêtre calcul courbure


@dataclass
class VWAPSignalsConfig:
    """Configuration des signaux de trading"""
    # Zones distance (en pips pour XAUUSD, adaptées automatiquement pour autres assets)
    neutral_zone_pips: float = 200.0
    strong_zone_pips: float = 500.0
    extreme_zone_pips: float = 800.0

    # Confidence minimale
    min_confidence: float = 0.7

    # Multiplicateurs de scoring (alignement/contre-trend)
    multipliers: Dict[str, float] = field(default_factory=lambda: {
        "aligned_neutral": 1.0,
        "aligned_strong": 1.5,
        "aligned_extreme": 2.0,
        "against_neutral": 0.8,
        "against_strong": 0.5,
        "against_extreme": 0.3,
    })


@dataclass
class VWAPCacheConfig:
    """Configuration du cache"""
    l1_ttl_seconds: int = 1                # Cache mémoire ultra-rapide
    l2_ttl_seconds: int = 300              # Cache Redis/shared
    l3_ttl_days: int = 30                  # Stockage persistant
    max_cache_size: int = 10000            # Entrées max en L1


@dataclass
class VWAPPerformanceConfig:
    """Configuration performance"""
    max_tick_queue_size: int = 10000
    processing_batch_size: int = 1000
    max_processing_time_ms: float = 10.0
    enable_async_processing: bool = False  # Pas d'async pour l'instant
    num_worker_threads: int = 1            # Single-threaded pour simplicité


@dataclass
class VWAPMonitoringConfig:
    """Configuration monitoring"""
    metrics_enabled: bool = True
    health_check_interval: int = 30
    alert_on_latency_ms: float = 100.0
    alert_on_memory_mb: int = 1024
    log_level: str = "INFO"


@dataclass
class VWAPIntegrationConfig:
    """Configuration intégration pipeline"""
    fusion_manager_weight: float = 0.25    # 25% du scoring total
    enable_market_analyzer_hook: bool = True
    enable_decision_pipeline_hook: bool = True
    enable_trade_executor_hook: bool = False  # Pas nécessaire pour l'instant


@dataclass
class VWAPAssetConfig:
    """Configuration spécifique à un asset"""
    symbol: str
    pip_value: float                       # 0.01 pour XAUUSD, 0.0001 pour EURUSD
    pip_position: int                      # 2 pour XAUUSD, 4 pour EURUSD
    decimal_places: int = 5

    # Zones adaptées à la volatilité de l'asset
    neutral_zone_pips: Optional[float] = None
    strong_zone_pips: Optional[float] = None
    extreme_zone_pips: Optional[float] = None

    # Seuils spécifiques
    slope_threshold: Optional[float] = None
    min_ticks: Optional[int] = None


class VWAPConfig:
    """
    Configuration globale du module VWAP
    Gère les configurations par asset avec fallbacks
    """

    # Configurations par défaut
    DEFAULT_CALCULATION = VWAPCalculationConfig()
    DEFAULT_DERIVATIVES = VWAPDerivativesConfig()
    DEFAULT_SIGNALS = VWAPSignalsConfig()
    DEFAULT_CACHE = VWAPCacheConfig()
    DEFAULT_PERFORMANCE = VWAPPerformanceConfig()
    DEFAULT_MONITORING = VWAPMonitoringConfig()
    DEFAULT_INTEGRATION = VWAPIntegrationConfig()

    # Configurations spécifiques par asset
    ASSET_CONFIGS = {
        "XAUUSD": VWAPAssetConfig(
            symbol="XAUUSD",
            pip_value=0.01,
            pip_position=2,
            decimal_places=2,
            neutral_zone_pips=200.0,
            strong_zone_pips=500.0,
            extreme_zone_pips=800.0,
            slope_threshold=0.001,
            min_ticks=10,
        ),
        "EURUSD": VWAPAssetConfig(
            symbol="EURUSD",
            pip_value=0.0001,
            pip_position=4,
            decimal_places=5,
            neutral_zone_pips=20.0,     # Adaptées à EUR volatilité
            strong_zone_pips=50.0,
            extreme_zone_pips=80.0,
            slope_threshold=0.00001,
            min_ticks=15,
        ),
        "GBPUSD": VWAPAssetConfig(
            symbol="GBPUSD",
            pip_value=0.0001,
            pip_position=4,
            decimal_places=5,
            neutral_zone_pips=25.0,     # Plus volatile que EUR
            strong_zone_pips=60.0,
            extreme_zone_pips=100.0,
            slope_threshold=0.00001,
            min_ticks=15,
        ),
    }

    def __init__(self, symbol: str, custom_config: Optional[Dict[str, Any]] = None):
        """
        Initialise la configuration pour un symbol donné

        Args:
            symbol: Symbol (XAUUSD, EURUSD, etc.)
            custom_config: Configuration custom (override defaults)
        """
        self.symbol = symbol
        self.logger = logging.getLogger(f"{__name__}.{symbol}")

        # Charge config asset
        self.asset = self.ASSET_CONFIGS.get(
            symbol,
            VWAPAssetConfig(
                symbol=symbol,
                pip_value=0.01,
                pip_position=2,
                decimal_places=2
            )
        )

        # Charge configs standards
        self.calculation = self.DEFAULT_CALCULATION
        self.derivatives = self.DEFAULT_DERIVATIVES
        self.signals = self.DEFAULT_SIGNALS
        self.cache = self.DEFAULT_CACHE
        self.performance = self.DEFAULT_PERFORMANCE
        self.monitoring = self.DEFAULT_MONITORING
        self.integration = self.DEFAULT_INTEGRATION

        # Apply custom config si fourni
        if custom_config:
            self._apply_custom_config(custom_config)

        self.logger.info(
            f"[VWAP_CONFIG] Initialisé pour {symbol} | "
            f"pip_value={self.asset.pip_value} | "
            f"neutral_zone={self.asset.neutral_zone_pips or self.signals.neutral_zone_pips} pips"
        )

    def _apply_custom_config(self, custom_config: Dict[str, Any]) -> None:
        """Applique configuration custom"""
        for section, config in custom_config.items():
            if hasattr(self, section) and isinstance(config, dict):
                target = getattr(self, section)
                for key, value in config.items():
                    if hasattr(target, key):
                        setattr(target, key, value)
                        self.logger.debug(f"[VWAP_CONFIG] Override {section}.{key} = {value}")

    def get_zone_thresholds(self) -> Dict[str, float]:
        """Retourne les seuils de zones pour l'asset"""
        return {
            'neutral': self.asset.neutral_zone_pips or self.signals.neutral_zone_pips,
            'strong': self.asset.strong_zone_pips or self.signals.strong_zone_pips,
            'extreme': self.asset.extreme_zone_pips or self.signals.extreme_zone_pips,
        }

    def get_slope_threshold(self) -> float:
        """Retourne le seuil de pente pour l'asset"""
        return self.asset.slope_threshold or self.derivatives.slope_threshold

    def get_min_ticks(self) -> int:
        """Retourne le minimum de ticks requis"""
        return self.asset.min_ticks or self.calculation.min_ticks_for_valid_vwap

    def to_dict(self) -> Dict[str, Any]:
        """Export configuration complète"""
        return {
            'symbol': self.symbol,
            'asset': {
                'pip_value': self.asset.pip_value,
                'pip_position': self.asset.pip_position,
                'decimal_places': self.asset.decimal_places,
            },
            'calculation': {
                'reset_hour': self.calculation.reset_hour,
                'min_ticks': self.get_min_ticks(),
            },
            'signals': {
                'zones': self.get_zone_thresholds(),
                'slope_threshold': self.get_slope_threshold(),
                'min_confidence': self.signals.min_confidence,
            },
            'cache': {
                'l1_ttl': self.cache.l1_ttl_seconds,
                'l2_ttl': self.cache.l2_ttl_seconds,
            },
            'integration': {
                'weight': self.integration.fusion_manager_weight,
            }
        }


# ==================== FACTORY ====================

def create_vwap_config(symbol: str, strategy_config: Optional[Dict] = None) -> VWAPConfig:
    """
    Factory pour créer une configuration VWAP

    Args:
        symbol: Symbol (XAUUSD, etc.)
        strategy_config: Config depuis config_trade_scalping.json

    Returns:
        VWAPConfig configurée
    """
    custom_config = None

    # Extraction config VWAP depuis strategy config si disponible
    if strategy_config:
        vwap_section = (
            strategy_config
            .get("entry_rules", {})
            .get("scalping", {})
            .get("vwap", {})
        )
        if vwap_section:
            custom_config = vwap_section

    return VWAPConfig(symbol=symbol, custom_config=custom_config)
