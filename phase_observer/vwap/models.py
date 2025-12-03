# phase_observer/vwap/models.py
"""
Modèles de données pour le module VWAP institutionnel
Structures rigoureuses avec validation intégrée
"""

from dataclasses import dataclass, field, asdict
from datetime import datetime, date
from decimal import Decimal
from typing import Dict, Any, Optional, List
from enum import Enum


# ==================== ENUMS ====================

class VWAPZone(Enum):
    """Classification de la distance prix/VWAP"""
    NEUTRAL = "NEUTRAL"       # ±200 pips
    STRONG = "STRONG"         # ±500 pips
    EXTREME = "EXTREME"       # >±500 pips


class VWAPRegime(Enum):
    """Régime de marché détecté"""
    ACCUMULATION = "ACCUMULATION"    # Prix proche VWAP, faible volatilité
    TRENDING = "TRENDING"            # Prix éloigné VWAP, forte volatilité
    BALANCED = "BALANCED"            # Équilibre
    TRANSITIONAL = "TRANSITIONAL"   # Changement de régime


class SignalAction(Enum):
    """Action recommandée"""
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class DataSource(Enum):
    """Source de données"""
    MT5_TICKS = "MT5_TICKS"
    MT5_RATES = "MT5_RATES"
    CACHE = "CACHE"
    FALLBACK = "FALLBACK"


# ==================== DATA STRUCTURES ====================

@dataclass
class VWAPTick:
    """
    Structure d'un tick pour le calcul VWAP
    Niveau institutionnel avec validation
    """
    timestamp: datetime
    symbol: str
    bid: float
    ask: float
    last: float
    volume: int
    source: DataSource = DataSource.MT5_TICKS
    sequence_id: int = 0
    is_valid: bool = True

    @property
    def typical_price(self) -> float:
        """Prix typique (bid + ask + last) / 3"""
        return (self.bid + self.ask + self.last) / 3.0

    @property
    def spread(self) -> float:
        """Spread en pips"""
        return self.ask - self.bid

    def validate(self) -> bool:
        """Validation rigoureuse du tick"""
        checks = [
            self.bid > 0,
            self.ask > 0,
            self.last > 0,
            self.ask > self.bid,  # Spread positif
            abs(self.last - (self.bid + self.ask)/2) < 100,  # Pas d'outlier
            self.volume >= 0,
        ]
        self.is_valid = all(checks)
        return self.is_valid

    def to_dict(self) -> Dict[str, Any]:
        """Export en dict"""
        d = asdict(self)
        d['source'] = self.source.value
        return d


@dataclass
class VWAPState:
    """
    État complet du calcul VWAP pour une session
    Cumulative values avec précision Decimal
    """
    symbol: str
    session_date: date
    cumulative_tpv: float = 0.0          # Cumulative Typical Price × Volume
    cumulative_volume: float = 0.0       # Cumulative Volume
    current_vwap: float = 0.0            # VWAP actuel
    session_high: float = 0.0            # Plus haut de session
    session_low: float = float('inf')    # Plus bas de session
    session_open: float = 0.0            # Ouverture de session
    tick_count: int = 0
    last_update: Optional[datetime] = None
    is_valid: bool = True

    def reset(self, new_date: date, open_price: float = 0.0) -> None:
        """Réinitialise pour une nouvelle session"""
        self.session_date = new_date
        self.cumulative_tpv = 0.0
        self.cumulative_volume = 0.0
        self.current_vwap = 0.0
        self.session_high = 0.0
        self.session_low = float('inf')
        self.session_open = open_price
        self.tick_count = 0
        self.last_update = None
        self.is_valid = True

    def update(self, tick: VWAPTick) -> None:
        """Met à jour avec un nouveau tick"""
        if not tick.is_valid:
            return

        # Cumulative updates
        typical = tick.typical_price
        self.cumulative_tpv += typical * tick.volume
        self.cumulative_volume += tick.volume

        # VWAP calculation
        if self.cumulative_volume > 0:
            self.current_vwap = self.cumulative_tpv / self.cumulative_volume

        # Session statistics
        self.session_high = max(self.session_high, tick.last)
        self.session_low = min(self.session_low, tick.last)
        if self.session_open == 0.0:
            self.session_open = tick.last

        self.tick_count += 1
        self.last_update = tick.timestamp

    def to_dict(self) -> Dict[str, Any]:
        """Export en dict"""
        return {
            'symbol': self.symbol,
            'session_date': self.session_date.isoformat(),
            'vwap': self.current_vwap,
            'session_high': self.session_high,
            'session_low': self.session_low,
            'session_open': self.session_open,
            'tick_count': self.tick_count,
            'last_update': self.last_update.isoformat() if self.last_update else None,
            'is_valid': self.is_valid,
        }


@dataclass
class VWAPDerivatives:
    """
    Dérivés calculés du VWAP
    Indicateurs techniques niveau institutionnel
    """
    timestamp: datetime
    symbol: str

    # Slopes (différentes fenêtres)
    slope_20: float = 0.0
    slope_50: float = 0.0
    slope_100: float = 0.0

    # Métriques dérivées
    curvature: float = 0.0         # Courbure (dérivée 2nde)
    velocity: float = 0.0          # Vitesse de variation
    acceleration: float = 0.0      # Accélération

    # Distance et position
    distance_pips: float = 0.0     # Prix - VWAP en pips
    distance_percent: float = 0.0  # En pourcentage

    # Classification
    zone: VWAPZone = VWAPZone.NEUTRAL
    regime: VWAPRegime = VWAPRegime.BALANCED

    # Bandes (Bollinger-style)
    bands: Dict[str, float] = field(default_factory=dict)

    # Confidence
    confidence: float = 0.0
    quality_score: float = 0.0

    @property
    def is_bullish(self) -> bool:
        """Signal haussier"""
        return self.slope_20 > 0 and self.velocity > 0

    @property
    def is_bearish(self) -> bool:
        """Signal baissier"""
        return self.slope_20 < 0 and self.velocity < 0

    @property
    def is_neutral(self) -> bool:
        """Signal neutre"""
        return abs(self.slope_20) < 0.001

    def to_dict(self) -> Dict[str, Any]:
        """Export en dict"""
        d = asdict(self)
        d['zone'] = self.zone.value
        d['regime'] = self.regime.value
        d['is_bullish'] = self.is_bullish
        d['is_bearish'] = self.is_bearish
        d['is_neutral'] = self.is_neutral
        return d


@dataclass
class VWAPSignal:
    """
    Signal de trading généré par le module VWAP
    Format standardisé pour intégration
    """
    timestamp: datetime
    symbol: str
    signal_type: str                    # VWAP_CROSS, VWAP_REJECTION, etc.
    action: SignalAction
    strength: float                     # 0.0 - 1.0
    confidence: float                   # 0.0 - 1.0

    # Contexte VWAP
    vwap_value: float
    current_price: float
    distance_pips: float
    slope: float
    zone: VWAPZone
    regime: VWAPRegime

    # Scoring (25 points total)
    trend_score: float = 0.0           # 0-15 pts
    position_score: float = 0.0        # 0-10 pts
    total_score: float = 0.0           # 0-25 pts

    # Métadonnées
    metadata: Dict[str, Any] = field(default_factory=dict)
    triggers: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def normalized_score(self) -> float:
        """Score normalisé 0-1 pour fusion"""
        return self.total_score / 25.0

    def to_dict(self) -> Dict[str, Any]:
        """Export en dict pour API/logs"""
        d = asdict(self)
        d['action'] = self.action.value
        d['zone'] = self.zone.value
        d['regime'] = self.regime.value
        d['normalized_score'] = self.normalized_score
        return d


@dataclass
class VWAPAnalysisResult:
    """
    Résultat complet d'une analyse VWAP
    Format standardisé pour intégration FusionManager
    """
    symbol: str
    timestamp: datetime

    # Score principal (compatible OrderFlow/Footprint)
    score: float                       # 0.0 - 1.0
    status: str                        # VALID, SUSPECT, INVALID
    bias: str                          # BUY, SELL, NEUTRAL

    # Détails VWAP
    vwap_value: float
    distance_pips: float
    slope: float
    zone: str
    regime: str

    # Scoring détaillé
    summary: Dict[str, Any] = field(default_factory=dict)
    derivatives: Optional[VWAPDerivatives] = None
    signal: Optional[VWAPSignal] = None

    # Métriques qualité
    tick_count: int = 0
    coverage_seconds: float = 0.0
    data_quality: float = 0.0

    # Performance
    calculation_time_ms: float = 0.0
    cache_hit: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """Export en dict pour logs/API"""
        return {
            'symbol': self.symbol,
            'timestamp': self.timestamp.isoformat(),
            'score': self.score,
            'status': self.status,
            'bias': self.bias,
            'vwap': {
                'value': self.vwap_value,
                'distance_pips': self.distance_pips,
                'slope': self.slope,
                'zone': self.zone,
                'regime': self.regime,
            },
            'summary': self.summary,
            'quality': {
                'tick_count': self.tick_count,
                'coverage_seconds': self.coverage_seconds,
                'data_quality': self.data_quality,
            },
            'performance': {
                'calculation_time_ms': self.calculation_time_ms,
                'cache_hit': self.cache_hit,
            }
        }


# ==================== CACHE STRUCTURES ====================

@dataclass
class CacheEntry:
    """Entrée de cache avec métadonnées"""
    key: str
    value: Any
    timestamp: datetime
    ttl_seconds: int
    hit_count: int = 0

    @property
    def is_expired(self) -> bool:
        """Vérifie si l'entrée est expirée"""
        age = (datetime.utcnow() - self.timestamp).total_seconds()
        return age > self.ttl_seconds

    def to_dict(self) -> Dict[str, Any]:
        """Export pour Redis/storage"""
        return {
            'key': self.key,
            'value': self.value,
            'timestamp': self.timestamp.isoformat(),
            'ttl_seconds': self.ttl_seconds,
            'hit_count': self.hit_count,
        }
