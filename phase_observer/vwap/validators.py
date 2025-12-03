# phase_observer/vwap/validators.py
"""
Validateurs de données pour le module VWAP institutionnel
Vérifications rigoureuses avec logging détaillé
"""

import logging
import numpy as np
import pandas as pd
from typing import Tuple, Optional, Dict, Any
from datetime import datetime, timedelta

from .models import VWAPTick, DataSource


logger = logging.getLogger(__name__)


class DataValidator:
    """Validateur de données niveau institutionnel"""

    def __init__(self, symbol: str):
        self.symbol = symbol
        self.logger = logging.getLogger(f"{__name__}.{symbol}")

        # Compteurs statistiques
        self.stats = {
            'total_validated': 0,
            'valid': 0,
            'invalid_price': 0,
            'invalid_spread': 0,
            'invalid_volume': 0,
            'outliers': 0,
            'timestamp_errors': 0,
        }

    def validate_tick(self, tick: VWAPTick) -> Tuple[bool, str]:
        """
        Valide un tick avec checks rigoureux

        Returns:
            (is_valid, reason)
        """
        self.stats['total_validated'] += 1

        # 1. Validation timestamp
        if not self._validate_timestamp(tick.timestamp):
            self.stats['timestamp_errors'] += 1
            return False, "invalid_timestamp"

        # 2. Validation prix
        if not self._validate_prices(tick.bid, tick.ask, tick.last):
            self.stats['invalid_price'] += 1
            return False, "invalid_prices"

        # 3. Validation spread
        if not self._validate_spread(tick.bid, tick.ask):
            self.stats['invalid_spread'] += 1
            return False, "invalid_spread"

        # 4. Validation volume
        if not self._validate_volume(tick.volume):
            self.stats['invalid_volume'] += 1
            return False, "invalid_volume"

        # 5. Détection outliers
        if self._is_outlier(tick):
            self.stats['outliers'] += 1
            return False, "outlier"

        self.stats['valid'] += 1
        return True, "valid"

    def _validate_timestamp(self, timestamp: datetime) -> bool:
        """Valide timestamp"""
        if not isinstance(timestamp, datetime):
            return False

        # Pas dans le futur
        if timestamp > datetime.utcnow() + timedelta(seconds=10):
            return False

        # Pas trop vieux (>24h)
        if timestamp < datetime.utcnow() - timedelta(hours=24):
            return False

        return True

    def _validate_prices(self, bid: float, ask: float, last: float) -> bool:
        """Valide les prix"""
        # Prix positifs
        if bid <= 0 or ask <= 0 or last <= 0:
            return False

        # Pas de valeurs extrêmes
        if bid > 100000 or ask > 100000 or last > 100000:
            return False

        # Ask > Bid (spread positif)
        if ask <= bid:
            return False

        return True

    def _validate_spread(self, bid: float, ask: float) -> bool:
        """Valide le spread"""
        spread = ask - bid

        # Spread positif
        if spread <= 0:
            return False

        # Spread raisonnable pour XAUUSD (< 10$)
        if self.symbol == "XAUUSD" and spread > 10.0:
            return False

        # Spread raisonnable pour EURUSD (< 0.01 = 100 pips)
        if self.symbol in ["EURUSD", "GBPUSD"] and spread > 0.01:
            return False

        return True

    def _validate_volume(self, volume: int) -> bool:
        """Valide le volume"""
        # Volume >= 0
        if volume < 0:
            return False

        # Volume raisonnable (< 1 million)
        if volume > 1_000_000:
            return False

        return True

    def _is_outlier(self, tick: VWAPTick) -> bool:
        """
        Détecte les outliers (prix aberrants)
        Utilise distance au mid-price
        """
        mid = (tick.bid + tick.ask) / 2.0
        distance = abs(tick.last - mid)

        # Pour XAUUSD: distance > 100$ est suspect
        if self.symbol == "XAUUSD" and distance > 100.0:
            return True

        # Pour EURUSD: distance > 0.01 (100 pips) est suspect
        if self.symbol in ["EURUSD", "GBPUSD"] and distance > 0.01:
            return True

        return False

    def validate_dataframe(self, df: pd.DataFrame) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Valide un DataFrame complet

        Returns:
            (is_valid, reason, metrics)
        """
        metrics = {
            'row_count': len(df),
            'valid_rows': 0,
            'invalid_rows': 0,
            'missing_columns': [],
            'data_quality_score': 0.0,
        }

        # 1. Vérification colonnes requises
        required_cols = ['time', 'close', 'high', 'low']
        missing = [col for col in required_cols if col not in df.columns]

        if missing:
            metrics['missing_columns'] = missing
            return False, f"missing_columns: {missing}", metrics

        # 2. Vérification données vides
        if df.empty:
            return False, "empty_dataframe", metrics

        # 3. Vérification OHLC cohérence
        try:
            invalid_ohlc = (
                (df['high'] < df['low']) |
                (df['high'] < df['close']) |
                (df['low'] > df['close'])
            )
            invalid_count = int(invalid_ohlc.sum())

            if invalid_count > 0:
                metrics['invalid_rows'] = invalid_count
                if invalid_count > len(df) * 0.1:  # >10% invalides
                    return False, f"too_many_invalid_ohlc: {invalid_count}", metrics

        except Exception as e:
            return False, f"validation_error: {e}", metrics

        # 4. Calcul qualité données
        valid_count = len(df) - invalid_count
        metrics['valid_rows'] = valid_count
        metrics['data_quality_score'] = valid_count / len(df)

        # 5. Vérification minimum de lignes
        if valid_count < 10:
            return False, f"insufficient_data: {valid_count} rows", metrics

        return True, "valid", metrics

    def get_stats(self) -> Dict[str, Any]:
        """Retourne statistiques de validation"""
        total = self.stats['total_validated']
        return {
            **self.stats,
            'valid_rate': self.stats['valid'] / total if total > 0 else 0.0,
            'invalid_rate': (total - self.stats['valid']) / total if total > 0 else 0.0,
        }

    def reset_stats(self) -> None:
        """Réinitialise statistiques"""
        for key in self.stats:
            self.stats[key] = 0


class DataNormalizer:
    """Normalisation des données pour calculs VWAP"""

    def __init__(self, symbol: str):
        self.symbol = symbol
        self.logger = logging.getLogger(f"{__name__}.{symbol}")

    def normalize_dataframe(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Normalise un DataFrame pour calcul VWAP
        - Convertit types
        - Trie par timestamp
        - Supprime duplicates
        - Remplit trous
        """
        if df is None or df.empty:
            return df

        df = df.copy()

        # 1. Conversion timestamp
        if 'time' in df.columns:
            df['time'] = pd.to_datetime(df['time'], errors='coerce', utc=True)
            # Supprime timezone pour simplicité
            if df['time'].dt.tz is not None:
                df['time'] = df['time'].dt.tz_localize(None)

        # 2. Conversion numeric
        numeric_cols = ['close', 'high', 'low', 'open', 'tick_volume']
        for col in numeric_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')

        # 3. Tri par timestamp
        if 'time' in df.columns:
            df = df.sort_values('time', ascending=True)

        # 4. Suppression duplicates
        if 'time' in df.columns:
            df = df.drop_duplicates(subset=['time'], keep='last')

        # 5. Suppression NaN critiques
        df = df.dropna(subset=['close'])

        # 6. Reset index
        df = df.reset_index(drop=True)

        return df

    def extract_vwap_from_df(self, df: pd.DataFrame) -> Optional[float]:
        """
        Extrait VWAP d'un DataFrame OrderFlow V6
        (si déjà calculé par volume_analyzer)
        """
        if df is None or df.empty:
            return None

        # Vérifie si colonne vwap existe
        if 'vwap' in df.columns:
            vwap_series = df['vwap']
            # Prend dernière valeur valide
            valid_vwap = vwap_series.dropna()
            if not valid_vwap.empty:
                return float(valid_vwap.iloc[-1])

        return None

    def calculate_coverage_seconds(self, df: pd.DataFrame) -> float:
        """Calcule la couverture temporelle en secondes"""
        if df is None or df.empty or 'time' not in df.columns:
            return 0.0

        try:
            times = pd.to_datetime(df['time'], errors='coerce')
            times = times.dropna()

            if len(times) < 2:
                return 0.0

            t0, t1 = times.iloc[0], times.iloc[-1]
            coverage = (t1 - t0).total_seconds()
            return max(1.0, coverage)

        except Exception as e:
            self.logger.warning(f"[VWAP_NORMALIZER] Erreur coverage: {e}")
            return 0.0


class QualityScorer:
    """Calcule des scores de qualité pour les données"""

    @staticmethod
    def score_data_quality(
        tick_count: int,
        coverage_seconds: float,
        valid_rate: float,
        has_vwap: bool = True
    ) -> float:
        """
        Score qualité données 0.0 - 1.0

        Args:
            tick_count: Nombre de ticks
            coverage_seconds: Couverture temporelle
            valid_rate: Taux de validité
            has_vwap: VWAP disponible

        Returns:
            Score 0.0 - 1.0
        """
        score = 0.0

        # 1. Score tick count (0-0.3)
        if tick_count >= 100:
            score += 0.3
        elif tick_count >= 50:
            score += 0.2
        elif tick_count >= 20:
            score += 0.1

        # 2. Score coverage (0-0.3)
        if coverage_seconds >= 30:
            score += 0.3
        elif coverage_seconds >= 15:
            score += 0.2
        elif coverage_seconds >= 5:
            score += 0.1

        # 3. Score validité (0-0.3)
        score += 0.3 * valid_rate

        # 4. Bonus VWAP disponible (0-0.1)
        if has_vwap:
            score += 0.1

        return min(1.0, score)

    @staticmethod
    def classify_quality(score: float) -> str:
        """
        Classifie qualité données

        Returns:
            'EXCELLENT', 'GOOD', 'FAIR', 'POOR', 'INVALID'
        """
        if score >= 0.9:
            return 'EXCELLENT'
        elif score >= 0.7:
            return 'GOOD'
        elif score >= 0.5:
            return 'FAIR'
        elif score >= 0.3:
            return 'POOR'
        else:
            return 'INVALID'
