# phase_observer/vwap/core.py
"""
Calculateur VWAP institutionnel optimisé
Performance < 1ms par calcul avec gestion session automatique
"""

import logging
import numpy as np
import pandas as pd
from datetime import datetime, date, timedelta
from typing import Optional, List, Dict, Any
from collections import deque

from .models import VWAPTick, VWAPState, DataSource
from .config import VWAPConfig
from .validators import DataValidator


logger = logging.getLogger(__name__)


class VWAPCalculator:
    """
    Calculateur VWAP niveau institutionnel
    - Calcul récursif (évite overflow)
    - Reset session automatique
    - Ring buffer historique
    - Validation intégrée
    """

    def __init__(self, config: VWAPConfig):
        """
        Initialise le calculateur VWAP

        Args:
            config: Configuration VWAP
        """
        self.config = config
        self.symbol = config.symbol
        self.logger = logging.getLogger(f"{__name__}.{self.symbol}")

        # État session
        self.state = VWAPState(
            symbol=self.symbol,
            session_date=datetime.utcnow().date()
        )

        # Validator
        self.validator = DataValidator(self.symbol)

        # Historique (ring buffer) - 24h de données en secondes
        self.history_size = 86400
        self.vwap_history = deque(maxlen=self.history_size)
        self.time_history = deque(maxlen=self.history_size)

        # Métriques performance
        self.metrics = {
            'ticks_processed': 0,
            'ticks_rejected': 0,
            'resets_count': 0,
            'avg_calc_time_ms': 0.0,
        }

        self.logger.info(
            f"[VWAP_CORE] Initialisé | symbol={self.symbol} | "
            f"reset_hour={config.calculation.reset_hour}GMT"
        )

    def on_tick(self, tick: VWAPTick) -> bool:
        """
        Traite un tick et met à jour le VWAP

        Args:
            tick: Tick à traiter

        Returns:
            True si traité avec succès
        """
        import time
        start = time.perf_counter()

        try:
            # 1. Validation
            is_valid, reason = self.validator.validate_tick(tick)
            if not is_valid:
                self.metrics['ticks_rejected'] += 1
                self.logger.debug(f"[VWAP_CORE] Tick rejeté: {reason}")
                return False

            # 2. Check reset session
            self._check_and_reset_session(tick.timestamp)

            # 3. Mise à jour state
            self.state.update(tick)

            # 4. Mise à jour historique
            self._update_history(tick.timestamp, self.state.current_vwap)

            # 5. Métriques
            self.metrics['ticks_processed'] += 1
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            self._update_avg_calc_time(elapsed_ms)

            return True

        except Exception as e:
            self.logger.error(f"[VWAP_CORE] Erreur on_tick: {e}", exc_info=True)
            return False

    def calculate_from_dataframe(self, df: pd.DataFrame) -> Optional[float]:
        """
        Calcule VWAP depuis un DataFrame
        Utilise les données déjà calculées par OrderFlow V6 si disponibles

        Args:
            df: DataFrame avec colonnes OHLC + vwap (optionnel)

        Returns:
            VWAP ou None
        """
        if df is None or df.empty:
            return None

        try:
            # 1. Si VWAP déjà calculé (OrderFlow V6), on le récupère
            if 'vwap' in df.columns:
                vwap_series = df['vwap']
                valid_vwap = vwap_series.dropna()
                if not valid_vwap.empty:
                    return float(valid_vwap.iloc[-1])

            # 2. Sinon calcul from scratch
            return self._calculate_vwap_from_ohlc(df)

        except Exception as e:
            self.logger.error(f"[VWAP_CORE] Erreur calcul DF: {e}", exc_info=True)
            return None

    def _calculate_vwap_from_ohlc(self, df: pd.DataFrame) -> Optional[float]:
        """Calcule VWAP depuis OHLC"""
        try:
            # Prix typique
            typical = (df['high'] + df['low'] + df['close']) / 3.0

            # Volume
            if 'tick_volume' in df.columns:
                volume = df['tick_volume']
            elif 'volume' in df.columns:
                volume = df['volume']
            else:
                # Fallback: volume uniforme
                volume = pd.Series([1] * len(df))

            # VWAP = sum(TP * Vol) / sum(Vol)
            tpv = typical * volume
            cum_tpv = tpv.cumsum()
            cum_vol = volume.cumsum()

            # Dernier VWAP
            if cum_vol.iloc[-1] > 0:
                vwap = cum_tpv.iloc[-1] / cum_vol.iloc[-1]
                return float(vwap)

            return None

        except Exception as e:
            self.logger.error(f"[VWAP_CORE] Erreur calcul OHLC: {e}")
            return None

    def get_vwap_array(self, df: pd.DataFrame) -> Optional[np.ndarray]:
        """
        Calcule array VWAP complet (toutes les bougies)

        Args:
            df: DataFrame OHLC

        Returns:
            Array VWAP ou None
        """
        try:
            # Si vwap existe déjà
            if 'vwap' in df.columns:
                return df['vwap'].values

            # Sinon calcul complet
            typical = (df['high'] + df['low'] + df['close']) / 3.0

            if 'tick_volume' in df.columns:
                volume = df['tick_volume'].values
            elif 'volume' in df.columns:
                volume = df['volume'].values
            else:
                volume = np.ones(len(df))

            # Calcul vectorisé
            tpv = typical.values * volume
            cum_tpv = np.cumsum(tpv)
            cum_vol = np.cumsum(volume)

            # VWAP array
            with np.errstate(divide='ignore', invalid='ignore'):
                vwap_array = np.where(
                    cum_vol > 0,
                    cum_tpv / np.maximum(cum_vol, 1e-12),
                    np.nan
                )

            return vwap_array

        except Exception as e:
            self.logger.error(f"[VWAP_CORE] Erreur calcul array: {e}")
            return None

    def _check_and_reset_session(self, timestamp: datetime) -> None:
        """Vérifie et réinitialise session si nécessaire"""
        current_date = timestamp.date()

        # Reset si changement de jour
        if current_date != self.state.session_date:
            self.logger.info(
                f"[VWAP_CORE] Reset session | "
                f"old={self.state.session_date} | new={current_date}"
            )
            self.state.reset(current_date)
            self.metrics['resets_count'] += 1

    def _update_history(self, timestamp: datetime, vwap: float) -> None:
        """Met à jour l'historique (ring buffer)"""
        self.time_history.append(timestamp)
        self.vwap_history.append(vwap)

    def _update_avg_calc_time(self, elapsed_ms: float) -> None:
        """Mise à jour temps calcul moyen (EMA)"""
        alpha = 0.1  # Lissage
        current_avg = self.metrics['avg_calc_time_ms']
        self.metrics['avg_calc_time_ms'] = (
            alpha * elapsed_ms + (1 - alpha) * current_avg
        )

    def get_current_vwap(self) -> float:
        """Retourne VWAP actuel"""
        return self.state.current_vwap

    def get_session_statistics(self) -> Dict[str, Any]:
        """Retourne statistiques session"""
        return {
            'symbol': self.symbol,
            'session_date': self.state.session_date.isoformat(),
            'vwap': self.state.current_vwap,
            'session_high': self.state.session_high,
            'session_low': self.state.session_low,
            'session_open': self.state.session_open,
            'tick_count': self.state.tick_count,
            'cumulative_volume': self.state.cumulative_volume,
            'last_update': self.state.last_update.isoformat() if self.state.last_update else None,
        }

    def get_history(self, last_n: int = None) -> Dict[str, List]:
        """
        Retourne historique VWAP

        Args:
            last_n: Nombre de points (None = tous)

        Returns:
            {'timestamps': [...], 'vwap': [...]}
        """
        if last_n is None:
            return {
                'timestamps': list(self.time_history),
                'vwap': list(self.vwap_history),
            }
        else:
            n = min(last_n, len(self.time_history))
            return {
                'timestamps': list(self.time_history)[-n:],
                'vwap': list(self.vwap_history)[-n:],
            }

    def get_metrics(self) -> Dict[str, Any]:
        """Retourne métriques performance"""
        total = self.metrics['ticks_processed'] + self.metrics['ticks_rejected']
        return {
            **self.metrics,
            'total_ticks': total,
            'acceptance_rate': (
                self.metrics['ticks_processed'] / total if total > 0 else 0.0
            ),
            'rejection_rate': (
                self.metrics['ticks_rejected'] / total if total > 0 else 0.0
            ),
        }

    def reset(self) -> None:
        """Reset complet du calculateur"""
        current_date = datetime.utcnow().date()
        self.state.reset(current_date)
        self.vwap_history.clear()
        self.time_history.clear()
        self.logger.info(f"[VWAP_CORE] Reset complet effectué")


class VWAPBatchCalculator:
    """
    Calculateur batch optimisé pour historique
    Utilisé pour backtesting ou chargement initial
    """

    def __init__(self, symbol: str):
        self.symbol = symbol
        self.logger = logging.getLogger(f"{__name__}.{symbol}")

    def calculate_batch(
        self,
        df: pd.DataFrame,
        session_reset: bool = True
    ) -> pd.DataFrame:
        """
        Calcule VWAP pour tout un DataFrame

        Args:
            df: DataFrame OHLC
            session_reset: Reset par jour

        Returns:
            DataFrame enrichi avec colonne 'vwap'
        """
        if df is None or df.empty:
            return df

        df = df.copy()

        try:
            # Prix typique
            df['_typical'] = (df['high'] + df['low'] + df['close']) / 3.0

            # Volume
            if 'tick_volume' in df.columns:
                vol_col = 'tick_volume'
            elif 'volume' in df.columns:
                vol_col = 'volume'
            else:
                df['_volume'] = 1
                vol_col = '_volume'

            # Session grouping si reset quotidien
            if session_reset and 'time' in df.columns:
                df['_session'] = pd.to_datetime(df['time']).dt.date

                # Calcul VWAP par session
                df['_tpv'] = df['_typical'] * df[vol_col]

                # Cumsum par groupe session
                df['_cum_tpv'] = df.groupby('_session')['_tpv'].cumsum()
                df['_cum_vol'] = df.groupby('_session')[vol_col].cumsum()

                # Cleanup
                df.drop(columns=['_session'], inplace=True)
            else:
                # Calcul global sans reset
                df['_tpv'] = df['_typical'] * df[vol_col]
                df['_cum_tpv'] = df['_tpv'].cumsum()
                df['_cum_vol'] = df[vol_col].cumsum()

            # VWAP final
            df['vwap'] = df['_cum_tpv'] / df['_cum_vol'].replace(0, np.nan)

            # Cleanup colonnes temporaires
            df.drop(columns=['_typical', '_tpv', '_cum_tpv', '_cum_vol'], inplace=True, errors='ignore')

            return df

        except Exception as e:
            self.logger.error(f"[VWAP_BATCH] Erreur calcul: {e}", exc_info=True)
            return df
