#phase_observer/features.py
# --- MUST BE FIRST LINE ---
from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple, List
import numpy as np
import pandas as pd
import time

try:
    import MetaTrader5 as mt5  # type: ignore
except Exception:
    mt5 = None  # type: ignore


class FeaturesExtractor:
    def __init__(self, logger: Optional[logging.Logger] = None, config_manager: Any = None):
        self.logger = logger or logging.getLogger(__name__)
        self.config_manager = config_manager

    def clean_dataframe(self, df: pd.DataFrame) -> pd.DataFrame:
        return _clean_dataframe(self, df)

    def get_swing_points(self, df: pd.DataFrame, order: Optional[int] = None):
        return _get_swing_points(self, df, order)

    def get_adaptive_swing_points(self, df: pd.DataFrame):
        return _get_adaptive_swing_points(self, df)

    def calculate_volatility_regime(self, df: pd.DataFrame) -> str:
        return _calculate_volatility_regime(self, df)

    def get_trend(self, df: pd.DataFrame) -> pd.Series:
        return _get_trend(self, df)

    def calculate_quality_metrics(self, tf_analyses: Dict, confluence: Dict, divergences: Dict, start_time: float):
        return _calculate_quality_metrics(self, tf_analyses, confluence, divergences, start_time)

    def fetch_timeframe_data(self, asset: str, timeframe: str, config: Dict):
        return _fetch_timeframe_data(self, asset, timeframe, config)



def _clean_dataframe(self, df: pd.DataFrame) -> pd.DataFrame:
    """
    Nettoie et standardise un DataFrame OHLCV (issu MT5) pour le PhaseObserver.
    - Garantit un index datetime UTC trié (index='time')
    - Convertit/valide les colonnes numériques essentielles
    - Supprime les timestamps dupliqués (garde la 1re occurrence)
    """
    self.logger.info("PhaseObserver: Nettoyage et standardisation du DataFrame...")

    if df is None or df.empty:
        self.logger.warning("PhaseObserver: DataFrame vide fourni. Retour vide.")
        return pd.DataFrame()

    # Travailler sur une vue légère pour limiter les effets de bord
    df = df.copy(deep=False)

    # 1) Assurer la présence de 'time'
    if "time" not in df.columns and df.index.name != "time":
        self.logger.error("PhaseObserver: colonne 'time' manquante. Abandon nettoyage.")
        return pd.DataFrame()

    # 2) Convertir 'time' → datetime UTC et le mettre en index si nécessaire
    if df.index.name != "time":
        if not pd.api.types.is_datetime64_any_dtype(df["time"]):
            try:
                # MT5 renvoie souvent des timestamps en secondes
                df["time"] = pd.to_datetime(df["time"], unit="s", utc=True, errors="coerce")
            except Exception as e:
                self.logger.error(f"PhaseObserver: erreur conversion 'time' en datetime: {e}", exc_info=True)
                return pd.DataFrame()

            before = len(df)
            df.dropna(subset=["time"], inplace=True)
            dropped = before - len(df)
            if dropped > 0:
                self.logger.warning(f"PhaseObserver: {dropped} lignes supprimées (timestamps invalides).")
            if df.empty:
                self.logger.warning("PhaseObserver: plus aucune ligne valide après conversion 'time'.")
                return pd.DataFrame()

        df.set_index("time", inplace=True)

    # 3) Tri strict par index (croissant)
    try:
        df.sort_index(inplace=True)
    except Exception:
        df = df.reset_index().sort_values("time").set_index("time")

    # 4) Colonnes numériques essentielles
    numeric_cols_expected = ["open", "high", "low", "close", "tick_volume"]
    for col in numeric_cols_expected:
        if col not in df.columns:
            self.logger.warning(f"PhaseObserver: colonne '{col}' absente. Ajoutée avec 0.0.")
            df[col] = 0.0
        # Conversion numérique robuste
        df[col] = pd.to_numeric(df[col], errors="coerce")
        # Remplir NaN
        if col == "tick_volume":
            df[col] = df[col].fillna(0.0)
            neg_mask = df[col] < 0
            if neg_mask.any():
                n = int(neg_mask.sum())
                df.loc[neg_mask, col] = 0.0
                self.logger.warning(f"PhaseObserver: {n} volumes négatifs corrigés à 0.0 dans '{col}'.")
        else:
            df[col] = df[col].fillna(0.00001)
            nonpos_mask = df[col] <= 0
            if nonpos_mask.any():
                n = int(nonpos_mask.sum())
                df.loc[nonpos_mask, col] = 0.00001
                self.logger.warning(f"PhaseObserver: {n} valeurs ≤ 0 corrigées à 0.00001 dans '{col}'.")

    # 5) Supprimer les timestamps dupliqués (garde la 1re)
    before = len(df)
    df = df[~df.index.duplicated(keep="first")]
    removed = before - len(df)
    if removed > 0:
        self.logger.warning(f"PhaseObserver: {removed} entrées dupliquées supprimées (index time).")

    self.logger.info("PhaseObserver: Nettoyage du DataFrame terminé.")
    return df


def _get_swing_points(
    self, df: pd.DataFrame, order: Optional[int] = None
) -> Tuple[pd.Series, pd.Series]:
    """
    Rétrocompatibilité : avec 'order' → fenêtre fixe, sinon délègue à _get_adaptive_swing_points.
    Retourne deux Series indexées (swing_highs, swing_lows).
    """
    if df is None or df.empty:
        return pd.Series([], dtype=float), pd.Series([], dtype=float)

    if order is not None:
        window_size = int(2 * order + 1)
        if len(df) < window_size:
            return pd.Series([], dtype=float), pd.Series([], dtype=float)

        highs_condition = (
            df["high"] == df["high"].rolling(window=window_size, center=True, min_periods=window_size).max()
        )
        lows_condition = (
            df["low"] == df["low"].rolling(window=window_size, center=True, min_periods=window_size).min()
        )

        swing_highs = df.loc[highs_condition, "high"]
        swing_lows = df.loc[lows_condition, "low"]
        return swing_highs, swing_lows

    # Par défaut : version adaptative
    return _get_adaptive_swing_points(self, df)


def _get_adaptive_swing_points(self, df: pd.DataFrame) -> Tuple[pd.Series, pd.Series]:
    """
    🎯 Adaptive Swing Points – s'adapte au régime de volatilité
    - Détection automatique du régime de volatilité (Garman–Klass par défaut)
    - Paramètres adaptatifs selon le régime
    - Filtrage par distance minimale (évite le bruit)
    """
    self.logger.debug("Calcul des Swing Points adaptatifs...")

    if df is None or df.empty:
        return pd.Series([], dtype=float), pd.Series([], dtype=float)

    swing_config = {}
    try:
        swing_config = self.config_manager.get("phase_detection_defaults.adaptive_swing_settings", {}) or {}
    except Exception:
        swing_config = {}

    volatility_regimes = swing_config.get("volatility_regimes", {}) or {}
    vol_config = swing_config.get("volatility_calculation", {}) or {}

    # === 1) CALCUL RÉGIME DE VOLATILITÉ ===
    vol_method = str(vol_config.get("method", "garman_klass")).lower()
    vol_period = int(vol_config.get("period", 20))
    high_threshold = float(vol_config.get("high_threshold", 75))
    low_threshold = float(vol_config.get("low_threshold", 25))

    if vol_method == "garman_klass":
        # Garman–Klass (plus précis que close-to-close)
        try:
            ln_high_low = np.log((df["high"] / df["low"]).clip(lower=1e-12))
            ln_close_open = np.log((df["close"] / df["open"]).clip(lower=1e-12))
            gk_vol = 0.5 * ln_high_low**2 - (2 * np.log(2) - 1) * ln_close_open**2
            volatility_series = np.sqrt(gk_vol.rolling(window=vol_period).mean())
        except Exception:
            returns = df["close"].pct_change()
            volatility_series = returns.rolling(window=vol_period).std()
    else:
        returns = df["close"].pct_change()
        volatility_series = returns.rolling(window=vol_period).std()

    # Classification par percentiles
    if len(volatility_series.dropna()) < vol_period:
        self.logger.warning("Données insuffisantes pour la vol adaptative. Mode 'normal_vol'.")
        current_regime = "normal_vol"
    else:
        current_vol = float(volatility_series.iloc[-1])
        vol_percentile = float((volatility_series <= current_vol).mean() * 100)
        if vol_percentile >= high_threshold:
            current_regime = "high_vol"
        elif vol_percentile <= low_threshold:
            current_regime = "low_vol"
        else:
            current_regime = "normal_vol"

    self.logger.debug(f"Régime de volatilité détecté: {current_regime}")

    # === 2) PARAMÈTRES ADAPTATIFS ===
    regime_params = volatility_regimes.get(current_regime, {}) or {}
    swing_order = int(regime_params.get("swing_order", 3))
    min_swing_distance = float(regime_params.get("min_swing_distance", 0.0005))

    # === 3) DÉTECTION SWING POINTS ===
    window_size = 2 * swing_order + 1
    if len(df) < window_size:
        self.logger.warning(f"DataFrame trop petit ({len(df)}) pour fenêtre swing {window_size}")
        return pd.Series([], dtype=float), pd.Series([], dtype=float)

    highs_condition = (
        df["high"] == df["high"].rolling(window=window_size, center=True, min_periods=window_size).max()
    )
    lows_condition = (
        df["low"] == df["low"].rolling(window=window_size, center=True, min_periods=window_size).min()
    )

    swing_highs = df.loc[highs_condition, "high"]
    swing_lows = df.loc[lows_condition, "low"]

    # === 4) FILTRAGE PAR DISTANCE MINIMALE (évite le bruit) ===
    def _filter_by_distance(series: pd.Series, min_dist: float, kind: str) -> pd.Series:
        if series.empty:
            return series
        kept_idx = []
        last_price = None
        for idx, price in series.items():
            if last_price is None or abs(float(price) - float(last_price)) >= min_dist:
                kept_idx.append(idx)
                last_price = float(price)
        filtered = series.loc[kept_idx]
        if len(filtered) != len(series):
            self.logger.debug(f"Swing {kind}: {len(series) - len(filtered)} points filtrés (< min_dist).")
        return filtered

    swing_highs = _filter_by_distance(swing_highs, min_swing_distance, "high")
    swing_lows = _filter_by_distance(swing_lows, min_swing_distance, "low")

    return swing_highs, swing_lows


def _calculate_volatility_regime(self, df: pd.DataFrame) -> str:
    """
    Calcule le régime de volatilité actuel (low/normal/high) pour usage dans d'autres fonctions.
    Méthode Garman–Klass par défaut, fallback sur std des returns.
    """
    if df is None or df.empty:
        return "normal_vol"

    try:
        swing_config = self.config_manager.get("phase_detection_defaults.adaptive_swing_settings", {}) or {}
    except Exception:
        swing_config = {}

    vol_config = swing_config.get("volatility_calculation", {}) or {}
    vol_period = int(vol_config.get("period", 20))
    high_threshold = float(vol_config.get("high_threshold", 75))
    low_threshold = float(vol_config.get("low_threshold", 25))

    try:
        ln_high_low = np.log((df["high"] / df["low"]).clip(lower=1e-12))
        ln_close_open = np.log((df["close"] / df["open"]).clip(lower=1e-12))
        gk_vol = 0.5 * ln_high_low**2 - (2 * np.log(2) - 1) * ln_close_open**2
        volatility_series = np.sqrt(gk_vol.rolling(window=vol_period).mean())
    except Exception:
        returns = df["close"].pct_change()
        volatility_series = returns.rolling(window=vol_period).std()

    if len(volatility_series.dropna()) < vol_period:
        return "normal_vol"

    current_vol = float(volatility_series.iloc[-1])
    vol_percentile = float((volatility_series <= current_vol).mean() * 100)

    if vol_percentile >= high_threshold:
        return "high_vol"
    elif vol_percentile <= low_threshold:
        return "low_vol"
    else:
        return "normal_vol"


def _get_trend(self, df: pd.DataFrame) -> pd.Series:
    """
    Détermine la tendance dominante pour chaque point (vectoriel).
    Retourne une série: 'bullish' | 'bearish' | 'neutral'.
    """
    # Paramètres depuis config (fallbacks sûrs)
    try:
        trend_window = int(getattr(self, "TREND_WINDOW",
                                   self.config_manager.get("phase_detection_defaults.trend_window", 20)))
        trend_sma_fast_ratio = float(getattr(self, "trend_sma_fast_ratio",
                                             self.config_manager.get("phase_detection_defaults.trend_sma_fast_ratio", 0.3)))
        trend_sma_slow_ratio = float(getattr(self, "trend_sma_slow_ratio",
                                             self.config_manager.get("phase_detection_defaults.trend_sma_slow_ratio", 0.7)))
    except Exception:
        trend_window = 20
        trend_sma_fast_ratio = 0.3
        trend_sma_slow_ratio = 0.7

    fast_window = max(1, int(trend_window * trend_sma_fast_ratio))
    slow_window = max(fast_window + 1, int(trend_window * trend_sma_slow_ratio))

    if len(df) < slow_window:
        self.logger.warning(
            f"Données insuffisantes ({len(df)}) pour tendance (slow_window={slow_window}). Tendance 'neutral'."
        )
        return pd.Series("neutral", index=df.index)

    sma_fast = df["close"].rolling(window=fast_window, min_periods=1).mean()
    sma_slow = df["close"].rolling(window=slow_window, min_periods=1).mean()

    trend_conditions = [sma_fast > sma_slow, sma_fast < sma_slow]
    trend_outcomes = ["bullish", "bearish"]

    return pd.Series(np.select(trend_conditions, trend_outcomes, default="neutral"), index=df.index)


def _calculate_quality_metrics(
    self, tf_analyses: Dict, confluence: Dict, divergences: Dict, start_time: float
) -> Dict[str, Any]:
    """
    Calcul métriques de qualité globales pour l'analyse multi-TF.
    """
    # Couverture données
    data_coverage = len(tf_analyses) / 3.0  # Suppose M1, M5, M15

    # Score confluence
    confluence_score = float(confluence.get("confluence_score", 0.0) or 0.0)

    # Pénalité divergences
    divergence_penalty = 0.3 if bool(divergences.get("has_conflicts", False)) else 0.0

    # Consistance temporelle
    temporal_consistency = 0.2 if bool(confluence.get("phase_consistency", False)) else 0.0

    # Score qualité global (borné)
    overall_score = max(
        0.0,
        min(
            1.0,
            data_coverage * 0.3
            + confluence_score * 0.4
            + temporal_consistency
            + (0.1 - divergence_penalty),
        ),
    )

    # Temps d'exécution
    execution_time_ms = (time.perf_counter() - float(start_time)) * 1000.0

    return {
        "overall_score": overall_score,
        "data_coverage": data_coverage,
        "confluence_score": confluence_score,
        "temporal_consistency": temporal_consistency,
        "divergence_penalty": divergence_penalty,
        "execution_time_ms": execution_time_ms,
        "performance_grade": ("A" if overall_score > 0.8 else "B" if overall_score > 0.6 else "C"),
    }


def _fetch_timeframe_data(
    self, asset: str, timeframe: str, config: Dict
) -> Optional[pd.DataFrame]:
    """
    Acquisition données MT5 optimisée avec gestion d'erreurs robuste.

    ⚙️ Utilise d'abord le mapping dynamique défini dans prod_config.json -> "timeframe_mapping",
    sinon fallback sur le mapping interne (mt5.TIMEFRAME_*).
    Garantit un lookback minimum via "bars_min" du mapping JSON (s'il existe), en plus du
    lookback paramétré par TF (ex: m1_config.lookback_window).
    """
    try:
        tf_key = str(timeframe).upper()

        # 1) Mapping dynamique depuis la config (prod_config.json)
        cfg_map: Dict[str, Any] = {}
        try:
            if hasattr(self, "config_manager") and self.config_manager:
                cfg_map = self.config_manager.get("timeframe_mapping", {}) or {}
        except Exception:
            cfg_map = {}

        mt5_timeframe = None
        bars_min = 0

        if tf_key in cfg_map:
            mt5_name = str(cfg_map[tf_key].get("mt5_name", "")).strip()
            bars_min = int(cfg_map[tf_key].get("bars_min", 0) or 0)
            if mt5_name and mt5 is not None:
                mt5_timeframe = getattr(mt5, mt5_name, None)

        # 2) Fallback mapping interne si le JSON n'est pas utilisable
        if mt5_timeframe is None and mt5 is not None:
            fallback_tf_mapping = {
                "M1": mt5.TIMEFRAME_M1,
                "M5": mt5.TIMEFRAME_M5,
                "M15": mt5.TIMEFRAME_M15,
                "M30": mt5.TIMEFRAME_M30,
                "H1": mt5.TIMEFRAME_H1,
                "H4": mt5.TIMEFRAME_H4,
                "D1": mt5.TIMEFRAME_D1,
            }
            mt5_timeframe = fallback_tf_mapping.get(tf_key)

        if mt5 is None:
            self.logger.warning("MetaTrader5 (mt5) non disponible dans l'environnement.")
        if mt5_timeframe is None and mt5 is not None:
            raise ValueError(f"Timeframe {timeframe} non supporté")

        # 3) Détermination du lookback
        lookback_key = f"{tf_key.lower()}_config"
        tf_cfg = config.get(lookback_key, {}) if isinstance(config, dict) else {}
        lookback_bars = int(tf_cfg.get("lookback_window", 500) or 500)

        # Respecte un plancher "bars_min" venant du JSON
        if bars_min and bars_min > 0:
            lookback_bars = max(lookback_bars, bars_min)

        # 4) Appel MT5Connector si disponible
        if hasattr(self.config_manager, "mt5_connector") and self.config_manager.mt5_connector:
            mt5_data = self.config_manager.mt5_connector.get_rates(asset, tf_key, lookback_bars)
            if mt5_data is not None and not mt5_data.empty:
                cleaned_data = _clean_dataframe(self, mt5_data)
                self.logger.debug(
                    f"[TFMAP] {asset} {tf_key} -> lookback={lookback_bars} | "
                    f"source={'JSON' if tf_key in cfg_map else 'fallback'}"
                )
                return cleaned_data
            else:
                self.logger.warning(f"[{asset}] Données vides/None pour TF={tf_key}, lookback={lookback_bars}")

       
    except Exception as e:
        self.logger.error(f"Erreur acquisition {asset} {timeframe}: {e}", exc_info=True)
        return None



