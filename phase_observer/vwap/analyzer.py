# phase_observer/vwap/analyzer.py
"""
Analyseur VWAP institutionnel - Orchestrateur principal
Intègre: Core, Derivatives, Signals, Cache, Metrics
Format de sortie compatible FusionManager
"""

import logging
import time
import numpy as np
import pandas as pd
from typing import Dict, Any, Optional, Tuple
from datetime import datetime

from .models import (
    VWAPAnalysisResult,
    VWAPDerivatives,
    VWAPSignal,
    DataSource
)
from .config import VWAPConfig, create_vwap_config
from .core import VWAPCalculator
from .derivatives import VWAPDerivativesCalculator
from .signals import VWAPSignalGenerator
from .cache import VWAPCacheManager
from .metrics import VWAPMetricsCollector
from .validators import DataValidator, DataNormalizer, QualityScorer
from .regime_mapper import RegimeMapper


logger = logging.getLogger(__name__)


class VWAPAnalyzer:
    """
    Analyseur VWAP institutionnel complet
    Point d'entrée unique pour analyse VWAP
    Compatible intégration FusionManager
    """

    def __init__(
        self,
        symbol: str,
        strategy_config: Optional[Dict] = None
    ):
        """
        Initialise l'analyseur VWAP

        Args:
            symbol: Symbol (XAUUSD, EURUSD, etc.)
            strategy_config: Config depuis config_trade_scalping.json
        """
        self.symbol = symbol
        self.logger = logging.getLogger(f"{__name__}.{symbol}")

        # Configuration
        self.config = create_vwap_config(symbol, strategy_config)

        # Composants core
        self.calculator = VWAPCalculator(self.config)
        self.derivatives_calc = VWAPDerivativesCalculator(self.config)
        self.signal_generator = VWAPSignalGenerator(self.config)

        # Infrastructure
        self.cache = VWAPCacheManager(self.config)
        self.metrics = VWAPMetricsCollector(self.config)
        self.validator = DataValidator(symbol)
        self.normalizer = DataNormalizer(symbol)

        # État
        self.last_analysis: Optional[VWAPAnalysisResult] = None
        self.is_initialized = True

        self.logger.info(
            f"[VWAP_ANALYZER] Initialisé | symbol={symbol} | "
            f"pip_value={self.config.asset.pip_value}"
        )

    def analyze(
        self,
        df: pd.DataFrame,
        current_price: float,
        context: Optional[Dict[str, Any]] = None
    ) -> VWAPAnalysisResult:
        """
        Analyse VWAP complète avec scoring 25 points

        Args:
            df: DataFrame OHLC (depuis OrderFlow V6 ou MarketData)
            current_price: Prix actuel
            context: Contexte additionnel (MTF, orderflow, phase_observer_regime, etc.)
                     - phase_observer_regime: Régime PhaseObserver (string)
                     - regime_strength: Force du régime PhaseObserver (0-1, optionnel)

        Returns:
            VWAPAnalysisResult avec score normalisé 0-1
        """
        start_time = time.perf_counter()
        timestamp = datetime.utcnow()

        try:
            # 1. Vérification cache
            cached = self.cache.get_analysis_result()
            if cached and self._is_cache_valid(cached, timestamp):
                self.logger.debug("[VWAP_ANALYZER] Cache hit")
                cached.cache_hit = True
                return cached

            # 2. Validation et normalisation données
            is_valid, reason, val_metrics = self.validator.validate_dataframe(df)
            if not is_valid:
                self.logger.warning(f"[VWAP_ANALYZER] Données invalides: {reason}")
                return self._create_invalid_result(timestamp, reason)

            df = self.normalizer.normalize_dataframe(df)

            # 3. Calcul VWAP
            vwap_value = self.calculator.calculate_from_dataframe(df)
            if vwap_value is None:
                return self._create_invalid_result(timestamp, "vwap_calculation_failed")

            vwap_array = self.calculator.get_vwap_array(df)
            if vwap_array is None or len(vwap_array) == 0:
                return self._create_invalid_result(timestamp, "vwap_array_empty")

            price_array = df['close'].values

            # 4. Mapping régime PhaseObserver → VWAP (si fourni)
            phase_observer_regime = None
            regime_confidence = None
            mapped_vwap_regime = None

            if context:
                phase_observer_regime = context.get("phase_observer_regime")
                regime_strength = context.get("regime_strength")

                if phase_observer_regime:
                    # Map PhaseObserver regime → VWAP regime
                    mapped_vwap_regime, regime_confidence = RegimeMapper.map_regime(
                        phase_observer_regime=phase_observer_regime,
                        regime_strength=regime_strength
                    )

                    self.logger.info(
                        f"[VWAP_REGIME_MAPPER] 🔄 PhaseObserver '{phase_observer_regime}' → "
                        f"VWAP '{mapped_vwap_regime.value}' | "
                        f"Confiance={regime_confidence:.2%}"
                    )

            # 5. Calcul dérivés (avec régime mappé si disponible)
            derivatives = self.derivatives_calc.calculate_all(
                vwap_array=vwap_array,
                price_array=price_array,
                timestamp=timestamp
            )

            # Override regime si mappé depuis PhaseObserver
            if mapped_vwap_regime is not None:
                derivatives.regime = mapped_vwap_regime
                # Ajuster confidence basée sur mapping
                if regime_confidence is not None:
                    derivatives.confidence = (derivatives.confidence + regime_confidence) / 2.0

            # 6. Génération signal
            signal = self.signal_generator.generate_signal(
                derivatives=derivatives,
                current_price=current_price,
                context=context
            )

            # 7. Calcul score normalisé (0-1 pour FusionManager)
            # Signal: 0-25 points -> normaliser à 0-1
            normalized_score = signal.total_score / 25.0

            # 8. Détermination status et bias
            status = self._determine_status(derivatives, signal)
            bias = self._determine_bias(signal)

            # 9. Qualité données
            coverage_seconds = self.normalizer.calculate_coverage_seconds(df)
            data_quality = QualityScorer.score_data_quality(
                tick_count=len(df),
                coverage_seconds=coverage_seconds,
                valid_rate=val_metrics['data_quality_score'],
                has_vwap=True
            )

            # 10. Construction résultat
            result = VWAPAnalysisResult(
                symbol=self.symbol,
                timestamp=timestamp,
                score=normalized_score,
                status=status,
                bias=bias,
                vwap_value=vwap_value,
                distance_pips=derivatives.distance_pips,
                slope=derivatives.slope_20,
                zone=derivatives.zone.value,
                regime=derivatives.regime.value,
                summary=self._build_summary(derivatives, signal),
                derivatives=derivatives,
                signal=signal,
                tick_count=len(df),
                coverage_seconds=coverage_seconds,
                data_quality=data_quality,
                calculation_time_ms=(time.perf_counter() - start_time) * 1000.0,
                cache_hit=False,
            )

            # 11. Stockage cache
            self.cache.set_analysis_result(result)
            self.cache.set_derivatives(derivatives)
            self.cache.set_signal(signal)
            self.cache.set_vwap_value(vwap_value)

            # 12. Métriques
            self.metrics.record_calculation(
                calc_time_ms=result.calculation_time_ms,
                data_quality=data_quality,
                tick_count=len(df)
            )
            self.metrics.record_signal(
                signal_type=signal.signal_type,
                action=signal.action.value,
                confidence=signal.confidence,
                strength=signal.strength,
                score=signal.total_score
            )

            # 13. Sauvegarde
            self.last_analysis = result

            # Log résultat (avec régime PhaseObserver si présent)
            self._log_analysis_result(result, phase_observer_regime)

            return result

        except Exception as e:
            self.logger.error(f"[VWAP_ANALYZER] Erreur analyse: {e}", exc_info=True)
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            return self._create_error_result(timestamp, str(e), elapsed_ms)

    def get_quick_vwap(self, df: pd.DataFrame) -> Optional[float]:
        """
        Récupère VWAP rapidement (depuis cache ou calcul simple)

        Args:
            df: DataFrame OHLC

        Returns:
            Valeur VWAP ou None
        """
        # Essai cache
        cached = self.cache.get_vwap_value()
        if cached is not None:
            return cached

        # Calcul simple
        return self.calculator.calculate_from_dataframe(df)

    def get_derivatives(self, df: pd.DataFrame) -> Optional[VWAPDerivatives]:
        """
        Calcule ou récupère dérivés VWAP

        Args:
            df: DataFrame OHLC

        Returns:
            VWAPDerivatives ou None
        """
        # Essai cache
        cached = self.cache.get_derivatives()
        if cached is not None:
            return cached

        # Calcul
        vwap_array = self.calculator.get_vwap_array(df)
        if vwap_array is None:
            return None

        price_array = df['close'].values
        return self.derivatives_calc.calculate_all(
            vwap_array=vwap_array,
            price_array=price_array,
            timestamp=datetime.utcnow()
        )

    def _is_cache_valid(self, cached: VWAPAnalysisResult, now: datetime) -> bool:
        """Vérifie validité du cache"""
        age_seconds = (now - cached.timestamp).total_seconds()
        return age_seconds < self.config.cache.l1_ttl_seconds

    def _determine_status(
        self,
        derivatives: VWAPDerivatives,
        signal: VWAPSignal
    ) -> str:
        """
        Détermine status d'analyse

        Returns:
            'VALID', 'SUSPECT', 'INVALID'
        """
        # Invalide si confidence trop basse
        if derivatives.confidence < 0.3:
            return "INVALID"

        # Suspect si quality score faible
        if derivatives.quality_score < 0.5:
            return "SUSPECT"

        # Suspect si zone extreme
        if derivatives.zone.value == "EXTREME":
            return "SUSPECT"

        return "VALID"

    def _determine_bias(self, signal: VWAPSignal) -> str:
        """
        Détermine bias de trading

        Returns:
            'BUY', 'SELL', 'NEUTRAL'
        """
        if signal.action.value == "BUY":
            return "BUY"
        elif signal.action.value == "SELL":
            return "SELL"
        else:
            return "NEUTRAL"

    def _build_summary(
        self,
        derivatives: VWAPDerivatives,
        signal: VWAPSignal
    ) -> Dict[str, Any]:
        """
        Construit résumé détaillé

        Returns:
            Summary dict
        """
        return {
            'signal_type': signal.signal_type,
            'action': signal.action.value,
            'strength': signal.strength,
            'confidence': signal.confidence,
            'trend_score': signal.trend_score,
            'position_score': signal.position_score,
            'total_score': signal.total_score,
            'slope_20': derivatives.slope_20,
            'slope_50': derivatives.slope_50,
            'curvature': derivatives.curvature,
            'velocity': derivatives.velocity,
            'zone': derivatives.zone.value,
            'regime': derivatives.regime.value,
            'triggers_count': len(signal.triggers),
            'triggers': signal.triggers,
        }

    def _create_invalid_result(
        self,
        timestamp: datetime,
        reason: str
    ) -> VWAPAnalysisResult:
        """Crée résultat invalide"""
        return VWAPAnalysisResult(
            symbol=self.symbol,
            timestamp=timestamp,
            score=0.0,
            status="INVALID",
            bias="NEUTRAL",
            vwap_value=0.0,
            distance_pips=0.0,
            slope=0.0,
            zone="NEUTRAL",
            regime="BALANCED",
            summary={'reason': reason},
            tick_count=0,
            coverage_seconds=0.0,
            data_quality=0.0,
            calculation_time_ms=0.0,
            cache_hit=False,
        )

    def _create_error_result(
        self,
        timestamp: datetime,
        error: str,
        calc_time_ms: float
    ) -> VWAPAnalysisResult:
        """Crée résultat en erreur"""
        return VWAPAnalysisResult(
            symbol=self.symbol,
            timestamp=timestamp,
            score=0.0,
            status="INVALID",
            bias="NEUTRAL",
            vwap_value=0.0,
            distance_pips=0.0,
            slope=0.0,
            zone="NEUTRAL",
            regime="BALANCED",
            summary={'error': error},
            tick_count=0,
            coverage_seconds=0.0,
            data_quality=0.0,
            calculation_time_ms=calc_time_ms,
            cache_hit=False,
        )

    def _log_analysis_result(
        self,
        result: VWAPAnalysisResult,
        phase_observer_regime: Optional[str] = None
    ) -> None:
        """Log résultat d'analyse"""
        log_msg = (
            f"[VWAP_ANALYZER] 📊 Analyse | "
            f"Score={result.score:.3f} | "
            f"Status={result.status} | "
            f"Bias={result.bias} | "
            f"VWAP={result.vwap_value:.5f} | "
            f"Distance={result.distance_pips:.1f} pips | "
            f"Slope={result.slope:.6f} | "
            f"Zone={result.zone} | "
            f"Regime={result.regime}"
        )

        # Ajouter régime PhaseObserver si présent
        if phase_observer_regime:
            log_msg += f" (PO={phase_observer_regime})"

        log_msg += f" | Time={result.calculation_time_ms:.2f}ms"

        self.logger.info(log_msg)

        # Log détails si score significatif
        if result.score > 0.5 and result.signal:
            self.logger.info(
                f"[VWAP_ANALYZER] 🎯 Signal fort | "
                f"Type={result.signal.signal_type} | "
                f"Action={result.signal.action.value} | "
                f"Trend={result.signal.trend_score:.1f}/15 | "
                f"Position={result.signal.position_score:.1f}/10 | "
                f"Confidence={result.signal.confidence:.2f}"
            )

    def get_metrics(self) -> Dict[str, Any]:
        """
        Retourne métriques complètes

        Returns:
            Métriques dict
        """
        return {
            'vwap': self.metrics.get_summary(),
            'cache': self.cache.get_metrics(),
            'validator': self.validator.get_stats(),
        }

    def get_health(self) -> Dict[str, Any]:
        """
        Retourne état de santé

        Returns:
            Health status dict
        """
        health = self.metrics.check_health()
        return {
            'is_healthy': health.is_healthy,
            'status': health.status,
            'issues': health.issues,
            'warnings': health.warnings,
            'last_check': health.last_check.isoformat() if health.last_check else None,
        }

    def reset(self) -> None:
        """Réinitialise l'analyseur"""
        self.calculator.reset()
        self.cache.clear()
        self.metrics.reset()
        self.validator.reset_stats()
        self.last_analysis = None

        self.logger.info("[VWAP_ANALYZER] Reset complet effectué")

    def cleanup(self) -> None:
        """Nettoyage périodique"""
        expired = self.cache.cleanup_expired()
        if expired > 0:
            self.logger.debug(f"[VWAP_ANALYZER] Nettoyé {expired} entrées cache expirées")


# ==================== FACTORY ====================

def create_vwap_analyzer(
    symbol: str,
    strategy_config: Optional[Dict] = None
) -> VWAPAnalyzer:
    """
    Factory pour créer un analyseur VWAP

    Args:
        symbol: Symbol (XAUUSD, etc.)
        strategy_config: Config depuis config_trade_scalping.json

    Returns:
        VWAPAnalyzer initialisé
    """
    return VWAPAnalyzer(symbol=symbol, strategy_config=strategy_config)
