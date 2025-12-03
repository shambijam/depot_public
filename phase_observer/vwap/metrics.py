# phase_observer/vwap/metrics.py
"""
Système de métriques et monitoring pour VWAP institutionnel
Tracking performance, qualité, health checks
"""

import logging
import time
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta
from collections import deque
from dataclasses import dataclass, field

from .config import VWAPConfig


logger = logging.getLogger(__name__)


@dataclass
class PerformanceMetrics:
    """Métriques de performance"""
    total_calculations: int = 0
    total_time_ms: float = 0.0
    avg_calc_time_ms: float = 0.0
    min_calc_time_ms: float = float('inf')
    max_calc_time_ms: float = 0.0
    calculations_per_second: float = 0.0
    last_calculation_time: Optional[datetime] = None


@dataclass
class QualityMetrics:
    """Métriques de qualité des données"""
    total_ticks: int = 0
    valid_ticks: int = 0
    invalid_ticks: int = 0
    outliers_detected: int = 0
    validation_rate: float = 0.0
    avg_data_quality: float = 0.0
    quality_scores: List[float] = field(default_factory=list)


@dataclass
class SignalMetrics:
    """Métriques des signaux générés"""
    total_signals: int = 0
    buy_signals: int = 0
    sell_signals: int = 0
    hold_signals: int = 0
    avg_confidence: float = 0.0
    avg_strength: float = 0.0
    avg_score: float = 0.0
    signal_distribution: Dict[str, int] = field(default_factory=dict)


@dataclass
class HealthStatus:
    """État de santé du module"""
    is_healthy: bool = True
    status: str = "HEALTHY"
    last_check: Optional[datetime] = None
    issues: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


class VWAPMetricsCollector:
    """
    Collecteur de métriques pour le module VWAP
    Niveau institutionnel avec historique et alerting
    """

    def __init__(self, config: VWAPConfig):
        """
        Initialise le collecteur de métriques

        Args:
            config: Configuration VWAP
        """
        self.config = config
        self.symbol = config.symbol
        self.logger = logging.getLogger(f"{__name__}.{self.symbol}")

        # Métriques principales
        self.performance = PerformanceMetrics()
        self.quality = QualityMetrics()
        self.signals = SignalMetrics()
        self.health = HealthStatus()

        # Historique (ring buffers)
        self.calc_times_history = deque(maxlen=1000)
        self.quality_scores_history = deque(maxlen=1000)
        self.signal_scores_history = deque(maxlen=1000)

        # Timestamps
        self.start_time = datetime.utcnow()
        self.last_metric_update = datetime.utcnow()

        # Seuils alertes
        self.alert_latency_ms = config.monitoring.alert_on_latency_ms
        self.health_check_interval = config.monitoring.health_check_interval

        self.logger.info(
            f"[VWAP_METRICS] Initialisé | "
            f"Alert_latency={self.alert_latency_ms}ms"
        )

    def record_calculation(
        self,
        calc_time_ms: float,
        data_quality: float,
        tick_count: int
    ) -> None:
        """
        Enregistre une calculation VWAP

        Args:
            calc_time_ms: Temps de calcul en ms
            data_quality: Score qualité 0-1
            tick_count: Nombre de ticks traités
        """
        # Performance
        self.performance.total_calculations += 1
        self.performance.total_time_ms += calc_time_ms
        self.performance.last_calculation_time = datetime.utcnow()

        # Min/Max
        self.performance.min_calc_time_ms = min(
            self.performance.min_calc_time_ms,
            calc_time_ms
        )
        self.performance.max_calc_time_ms = max(
            self.performance.max_calc_time_ms,
            calc_time_ms
        )

        # Average (EMA)
        alpha = 0.1
        if self.performance.avg_calc_time_ms == 0:
            self.performance.avg_calc_time_ms = calc_time_ms
        else:
            self.performance.avg_calc_time_ms = (
                alpha * calc_time_ms +
                (1 - alpha) * self.performance.avg_calc_time_ms
            )

        # Historique
        self.calc_times_history.append(calc_time_ms)
        self.quality_scores_history.append(data_quality)

        # Qualité
        self.quality.total_ticks += tick_count
        self.quality.quality_scores.append(data_quality)
        self._update_quality_metrics()

        # Alerte si latence excessive
        if calc_time_ms > self.alert_latency_ms:
            self.logger.warning(
                f"[VWAP_METRICS] ⚠️ Latence élevée: {calc_time_ms:.2f}ms "
                f"(seuil: {self.alert_latency_ms}ms)"
            )
            self.health.warnings.append(
                f"High latency: {calc_time_ms:.2f}ms at {datetime.utcnow()}"
            )

    def record_signal(
        self,
        signal_type: str,
        action: str,
        confidence: float,
        strength: float,
        score: float
    ) -> None:
        """
        Enregistre un signal généré

        Args:
            signal_type: Type de signal
            action: BUY/SELL/HOLD
            confidence: Confidence 0-1
            strength: Strength 0-1
            score: Score 0-25
        """
        self.signals.total_signals += 1

        # Count par action
        if action == "BUY":
            self.signals.buy_signals += 1
        elif action == "SELL":
            self.signals.sell_signals += 1
        else:
            self.signals.hold_signals += 1

        # Distribution par type
        if signal_type not in self.signals.signal_distribution:
            self.signals.signal_distribution[signal_type] = 0
        self.signals.signal_distribution[signal_type] += 1

        # Moyennes (EMA)
        alpha = 0.1
        if self.signals.avg_confidence == 0:
            self.signals.avg_confidence = confidence
            self.signals.avg_strength = strength
            self.signals.avg_score = score
        else:
            self.signals.avg_confidence = (
                alpha * confidence + (1 - alpha) * self.signals.avg_confidence
            )
            self.signals.avg_strength = (
                alpha * strength + (1 - alpha) * self.signals.avg_strength
            )
            self.signals.avg_score = (
                alpha * score + (1 - alpha) * self.signals.avg_score
            )

        # Historique
        self.signal_scores_history.append(score)

    def record_validation(
        self,
        total: int,
        valid: int,
        invalid: int,
        outliers: int
    ) -> None:
        """
        Enregistre résultats de validation

        Args:
            total: Total ticks validés
            valid: Ticks valides
            invalid: Ticks invalides
            outliers: Outliers détectés
        """
        self.quality.total_ticks += total
        self.quality.valid_ticks += valid
        self.quality.invalid_ticks += invalid
        self.quality.outliers_detected += outliers

        self._update_quality_metrics()

    def check_health(self) -> HealthStatus:
        """
        Vérifie l'état de santé du module

        Returns:
            HealthStatus avec diagnostic
        """
        issues = []
        warnings = []
        status = "HEALTHY"

        # 1. Check performance
        if self.performance.avg_calc_time_ms > self.alert_latency_ms:
            issues.append(
                f"Average latency too high: {self.performance.avg_calc_time_ms:.2f}ms"
            )
            status = "DEGRADED"

        # 2. Check quality
        if self.quality.validation_rate < 0.8:
            warnings.append(
                f"Low validation rate: {self.quality.validation_rate:.2%}"
            )

        if self.quality.avg_data_quality < 0.5:
            issues.append(
                f"Poor data quality: {self.quality.avg_data_quality:.2f}"
            )
            status = "DEGRADED"

        # 3. Check dernière calculation
        if self.performance.last_calculation_time:
            elapsed = (datetime.utcnow() - self.performance.last_calculation_time).total_seconds()
            if elapsed > 60:  # Pas de calcul depuis 60s
                warnings.append(
                    f"No calculation in {elapsed:.0f}s"
                )

        # 4. Check signal quality
        if self.signals.total_signals > 100:
            hold_rate = self.signals.hold_signals / self.signals.total_signals
            if hold_rate > 0.8:
                warnings.append(
                    f"High HOLD rate: {hold_rate:.2%} (low signal quality)"
                )

        # Update status
        if issues:
            status = "UNHEALTHY" if len(issues) > 2 else "DEGRADED"

        self.health = HealthStatus(
            is_healthy=(status == "HEALTHY"),
            status=status,
            last_check=datetime.utcnow(),
            issues=issues,
            warnings=warnings,
        )

        # Log si problèmes
        if not self.health.is_healthy:
            self.logger.warning(
                f"[VWAP_METRICS] Health check: {status} | "
                f"Issues={len(issues)}, Warnings={len(warnings)}"
            )

        return self.health

    def _update_quality_metrics(self) -> None:
        """Mise à jour métriques qualité"""
        if self.quality.total_ticks > 0:
            self.quality.validation_rate = (
                self.quality.valid_ticks / self.quality.total_ticks
            )

        if self.quality.quality_scores:
            self.quality.avg_data_quality = (
                sum(self.quality.quality_scores) / len(self.quality.quality_scores)
            )

    def get_summary(self) -> Dict[str, Any]:
        """
        Retourne résumé complet des métriques

        Returns:
            Dict avec toutes les métriques
        """
        uptime = (datetime.utcnow() - self.start_time).total_seconds()

        # Calcul throughput
        if uptime > 0:
            self.performance.calculations_per_second = (
                self.performance.total_calculations / uptime
            )

        return {
            'symbol': self.symbol,
            'uptime_seconds': uptime,
            'performance': {
                'total_calculations': self.performance.total_calculations,
                'avg_calc_time_ms': round(self.performance.avg_calc_time_ms, 3),
                'min_calc_time_ms': round(self.performance.min_calc_time_ms, 3),
                'max_calc_time_ms': round(self.performance.max_calc_time_ms, 3),
                'calculations_per_second': round(self.performance.calculations_per_second, 2),
                'last_calculation': (
                    self.performance.last_calculation_time.isoformat()
                    if self.performance.last_calculation_time else None
                ),
            },
            'quality': {
                'total_ticks': self.quality.total_ticks,
                'valid_ticks': self.quality.valid_ticks,
                'invalid_ticks': self.quality.invalid_ticks,
                'outliers_detected': self.quality.outliers_detected,
                'validation_rate': round(self.quality.validation_rate, 3),
                'avg_data_quality': round(self.quality.avg_data_quality, 3),
            },
            'signals': {
                'total_signals': self.signals.total_signals,
                'buy_signals': self.signals.buy_signals,
                'sell_signals': self.signals.sell_signals,
                'hold_signals': self.signals.hold_signals,
                'avg_confidence': round(self.signals.avg_confidence, 3),
                'avg_strength': round(self.signals.avg_strength, 3),
                'avg_score': round(self.signals.avg_score, 2),
                'distribution': self.signals.signal_distribution,
            },
            'health': {
                'is_healthy': self.health.is_healthy,
                'status': self.health.status,
                'last_check': (
                    self.health.last_check.isoformat()
                    if self.health.last_check else None
                ),
                'issues_count': len(self.health.issues),
                'warnings_count': len(self.health.warnings),
            },
        }

    def get_detailed_report(self) -> Dict[str, Any]:
        """
        Retourne rapport détaillé avec historiques

        Returns:
            Rapport complet
        """
        summary = self.get_summary()

        # Ajout historiques
        summary['history'] = {
            'calc_times_recent': list(self.calc_times_history)[-20:],
            'quality_scores_recent': list(self.quality_scores_history)[-20:],
            'signal_scores_recent': list(self.signal_scores_history)[-20:],
        }

        # Ajout health details
        summary['health']['issues'] = self.health.issues
        summary['health']['warnings'] = self.health.warnings

        # Statistics avancées
        if self.calc_times_history:
            import numpy as np
            times = np.array(list(self.calc_times_history))
            summary['performance']['p50_calc_time_ms'] = float(np.percentile(times, 50))
            summary['performance']['p95_calc_time_ms'] = float(np.percentile(times, 95))
            summary['performance']['p99_calc_time_ms'] = float(np.percentile(times, 99))

        return summary

    def reset(self) -> None:
        """Réinitialise toutes les métriques"""
        self.performance = PerformanceMetrics()
        self.quality = QualityMetrics()
        self.signals = SignalMetrics()
        self.health = HealthStatus()

        self.calc_times_history.clear()
        self.quality_scores_history.clear()
        self.signal_scores_history.clear()

        self.start_time = datetime.utcnow()
        self.last_metric_update = datetime.utcnow()

        self.logger.info("[VWAP_METRICS] Métriques réinitialisées")


class MetricsFormatter:
    """
    Formatage des métriques pour logs/dashboards
    """

    @staticmethod
    def format_performance(metrics: PerformanceMetrics) -> str:
        """
        Formate métriques de performance

        Returns:
            String formaté
        """
        return (
            f"Calculations: {metrics.total_calculations} | "
            f"Avg: {metrics.avg_calc_time_ms:.2f}ms | "
            f"Min: {metrics.min_calc_time_ms:.2f}ms | "
            f"Max: {metrics.max_calc_time_ms:.2f}ms | "
            f"Throughput: {metrics.calculations_per_second:.1f}/s"
        )

    @staticmethod
    def format_quality(metrics: QualityMetrics) -> str:
        """
        Formate métriques de qualité

        Returns:
            String formaté
        """
        return (
            f"Ticks: {metrics.total_ticks} | "
            f"Valid: {metrics.valid_ticks} ({metrics.validation_rate:.1%}) | "
            f"Invalid: {metrics.invalid_ticks} | "
            f"Outliers: {metrics.outliers_detected} | "
            f"Quality: {metrics.avg_data_quality:.2f}"
        )

    @staticmethod
    def format_signals(metrics: SignalMetrics) -> str:
        """
        Formate métriques de signaux

        Returns:
            String formaté
        """
        total = metrics.total_signals
        if total == 0:
            return "No signals yet"

        buy_pct = metrics.buy_signals / total * 100
        sell_pct = metrics.sell_signals / total * 100
        hold_pct = metrics.hold_signals / total * 100

        return (
            f"Signals: {total} | "
            f"BUY: {metrics.buy_signals} ({buy_pct:.0f}%) | "
            f"SELL: {metrics.sell_signals} ({sell_pct:.0f}%) | "
            f"HOLD: {metrics.hold_signals} ({hold_pct:.0f}%) | "
            f"Avg Score: {metrics.avg_score:.1f}/25 | "
            f"Confidence: {metrics.avg_confidence:.2f}"
        )

    @staticmethod
    def format_health(health: HealthStatus) -> str:
        """
        Formate état de santé

        Returns:
            String formaté avec émoji
        """
        emoji = "✅" if health.status == "HEALTHY" else "⚠️" if health.status == "DEGRADED" else "❌"
        return (
            f"{emoji} Status: {health.status} | "
            f"Issues: {len(health.issues)} | "
            f"Warnings: {len(health.warnings)}"
        )
