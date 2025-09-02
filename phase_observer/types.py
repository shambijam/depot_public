# phase_observer/types.py
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from enum import Enum
from datetime import datetime, timezone

UTC = timezone.utc

__all__ = [
    "Direction",
    "Phase",
    "PhaseSignal",
    "PhaseSnapshot",
    "MarketFeatures",
    "PhaseMemory",
]


class Direction(str, Enum):
    """Biais directionnel d'un signal ou snapshot."""

    BUY = "BUY"
    SELL = "SELL"
    NEUTRAL = "NEUTRAL"


class Phase(str, Enum):
    """Phases principales de marché détectables par le PhaseObserver."""

    LIQUIDITY = "LIQUIDITY"
    RANGE = "RANGE"
    TREND_BULL = "TREND_BULL"
    TREND_BEAR = "TREND_BEAR"
    VOL_LOW = "VOL_LOW"
    VOL_NORMAL = "VOL_NORMAL"
    VOL_HIGH = "VOL_HIGH"
    CRISIS = "CRISIS"
    MOMENTUM_IMPULSE = "MOMENTUM_IMPULSE"
    MOMENTUM_EXHAUSTION = "MOMENTUM_EXHAUSTION"
    BREAKOUT = "BREAKOUT"
    FAKEOUT = "FAKEOUT"
    CONSOLIDATION = "CONSOLIDATION"
    SESSION = "SESSION"
    UNKNOWN = "UNKNOWN"


@dataclass(slots=True)
class PhaseSignal:
    """Signal élémentaire produit par un détecteur de phase."""

    kind: str
    label: str
    direction: Direction | str
    quality: float = 0.0
    ttl_bars: int = 1
    meta: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.kind = str(self.kind).strip().lower()
        self.label = str(self.label).strip().upper()
        if isinstance(self.direction, str):
            d = self.direction.upper()
            self.direction = (
                Direction(d) if d in Direction.__members__ else Direction.NEUTRAL
            )
        try:
            self.quality = max(0.0, min(1.0, float(self.quality)))
        except Exception:
            self.quality = 0.0
        try:
            self.ttl_bars = max(0, int(self.ttl_bars))
        except Exception:
            self.ttl_bars = 0


@dataclass(slots=True)
class PhaseSnapshot:
    """État consolidé du marché à un instant donné."""

    phase: Phase | str
    bias: Direction | str
    subphases: List[PhaseSignal] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if isinstance(self.phase, str):
            p = self.phase.upper()
            self.phase = Phase(p) if p in Phase.__members__ else Phase.UNKNOWN
        if isinstance(self.bias, str):
            b = self.bias.upper()
            self.bias = (
                Direction(b) if b in Direction.__members__ else Direction.NEUTRAL
            )
        if self.timestamp.tzinfo is None:
            self.timestamp = self.timestamp.replace(tzinfo=UTC)


@dataclass(slots=True)
class MarketFeatures:
    """Features calculés à partir des données de marché (inputs des détecteurs)."""

    volatility: Dict[str, Any] = field(default_factory=dict)
    trend: Dict[str, Any] = field(default_factory=dict)
    liquidity: Dict[str, Any] = field(default_factory=dict)
    momentum: Dict[str, Any] = field(default_factory=dict)
    session: Dict[str, Any] = field(default_factory=dict)
    microstructure: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class PhaseMemory:
    """Mémoire interne pour stabiliser et lisser les signaux de phase."""

    last_snapshot: Optional[PhaseSnapshot] = None
    last_phase: Optional[str] = None
    recent_signals: List[PhaseSignal] = field(default_factory=list)
    caches: Dict[str, Any] = field(default_factory=dict)
    last_update: Optional[datetime] = None
    phase_transitions: List[Dict[str, Any]] = field(default_factory=list)

    confidence: float = 0.0
    persistence: int = 0
    counters: Dict[str, Any] = field(default_factory=dict)

    # 🔥 Ajout propre pour orchestrator.py
    last_confidence: Optional[float] = None

    def touch(self, snapshot: PhaseSnapshot | None = None) -> None:
        self.last_update = datetime.now(UTC)
        if snapshot is not None:
            self.last_snapshot = snapshot

    def remember(self, signal: PhaseSignal, *, max_buffer: int = 256) -> None:
        self.recent_signals.append(signal)
        if len(self.recent_signals) > max_buffer:
            del self.recent_signals[: len(self.recent_signals) - max_buffer]

    def age_signals(self) -> None:
        kept: list[PhaseSignal] = []
        for s in self.recent_signals:
            if s.ttl_bars > 0:
                s.ttl_bars -= 1
            if s.ttl_bars > 0:
                kept.append(s)
        self.recent_signals = kept

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PhaseMemory":
        obj = cls()
        for k, v in data.items():
            if hasattr(obj, k):
                setattr(obj, k, v)
        return obj

    def to_dict(self) -> Dict[str, Any]:
        return {
            "last_snapshot": self.last_snapshot,
            "last_phase": self.last_phase,
            "recent_signals": self.recent_signals,
            "caches": self.caches,
            "last_update": self.last_update,
            "phase_transitions": self.phase_transitions,
            "confidence": self.confidence,
            "persistence": self.persistence,
            "counters": self.counters,
            "last_confidence": self.last_confidence,
        }
