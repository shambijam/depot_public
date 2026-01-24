# phase_observer/features.py
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

# ---------- FOOTPRINT: helpers temps réel -----------------------------------


from collections import defaultdict


def _convert_volume_profile_to_footprint(
    vp: Dict[str, Any],
    df_m1: pd.DataFrame,
    logger: Optional[logging.Logger] = None,
) -> Tuple[pd.DataFrame, dict]:
    """
    Convertit un Volume Profile (OrderFlow V6) en format Footprint (ask_vol/bid_vol/delta).

    Logique:
    - Pour chaque bin du Volume Profile, on détermine le sens dominant (buy/sell)
      en comptant les barres M1 bullish (close>open) vs bearish (close<open) dans ce bin
    - ask_vol = volume total × ratio de barres bullish
    - bid_vol = volume total × ratio de barres bearish
    - delta = ask_vol - bid_vol

    Retourne:
    - df_levels: DataFrame indexé par price avec colonnes [ask_vol, bid_vol, vol, delta, delta_ratio, zscore_vol, is_poc]
    - meta: dict avec tick_rate, spread, window_s, poc_price
    """
    if logger is None:
        logger = logging.getLogger(__name__)

    meta = {"tick_rate": 0.0, "spread": 0.0, "window_s": 0, "poc_price": None}

    # Vérifications
    if not vp or not isinstance(vp, dict):
        logger.warning("[VP→FP] Volume Profile vide ou invalide")
        return pd.DataFrame(columns=["ask_vol", "bid_vol", "vol", "delta", "delta_ratio", "zscore_vol", "is_poc"]), meta

    if df_m1 is None or df_m1.empty:
        logger.warning("[VP→FP] DataFrame M1 vide")
        return pd.DataFrame(columns=["ask_vol", "bid_vol", "vol", "delta", "delta_ratio", "zscore_vol", "is_poc"]), meta

    # Extraire les données du VP (retour de calculate_volume_profile)
    # On n'a pas les bins détaillés, mais on peut reconstruire depuis les barres M1

    # Stratégie alternative: utiliser les barres M1 directement
    # Chaque barre = 1 niveau de prix (on prend close comme prix représentatif)
    rows = []

    for idx, row in df_m1.iterrows():
        try:
            price = float(row.get("close", 0))
            volume = float(row.get("tick_volume", row.get("volume", 1.0)))
            open_price = float(row.get("open", price))

            if volume <= 0:
                volume = 1.0

            # Déterminer sens: bullish si close > open
            is_bullish = price > open_price

            if is_bullish:
                ask_vol = volume
                bid_vol = 0.0
            else:
                ask_vol = 0.0
                bid_vol = volume

            total = ask_vol + bid_vol
            delta = ask_vol - bid_vol
            ratio = (abs(delta) / total) if total > 0 else 0.0

            rows.append((price, ask_vol, bid_vol, total, delta, ratio))

        except Exception as e:
            logger.debug(f"[VP→FP] Erreur traitement barre: {e}")
            continue

    if not rows:
        logger.warning("[VP→FP] Aucun niveau de prix construit")
        return pd.DataFrame(columns=["ask_vol", "bid_vol", "vol", "delta", "delta_ratio", "zscore_vol", "is_poc"]), meta

    # Construire DataFrame
    levels = pd.DataFrame(
        rows, columns=["price", "ask_vol", "bid_vol", "vol", "delta", "delta_ratio"]
    ).sort_values("price").set_index("price")

    # Z-score du volume
    m = float(levels["vol"].mean() or 0.0)
    s = float(levels["vol"].std(ddof=0) or 1.0)
    levels["zscore_vol"] = (levels["vol"] - m) / (s if s != 0 else 1.0)

    # POC
    poc_price = vp.get("vpoc_price") or (float(levels["vol"].idxmax()) if not levels["vol"].empty else None)
    levels["is_poc"] = levels.index == poc_price

    # Meta
    meta["poc_price"] = poc_price
    meta["window_s"] = len(df_m1)  # Nombre de barres M1

    logger.info(f"[VP→FP] Conversion OK | niveaux={len(levels)} | poc={poc_price}")

    return levels, meta


def _micro_atr_from_ticks(ticks: pd.DataFrame, window_s: int = 10) -> float:
    """
    Micro-ATR sur 'window_s' dernières secondes en points monétaires.
    Attend colonnes: time (ns/epoch/ts), bid, ask. Robuste aux manques.
    """
    if ticks is None or len(ticks) < 3:
        return 0.0
    df = ticks.copy()
    # coercition temps
    if not np.issubdtype(df["time"].dtype, np.datetime64):
        df["time"] = pd.to_datetime(
            df["time"], errors="coerce", unit="s", utc=True
        ).fillna(pd.Timestamp.utcnow())
    cutoff = df["time"].max() - pd.Timedelta(seconds=window_s)
    df = df[df["time"] >= cutoff].copy()
    if df.empty:
        return 0.0
    # range micro
    mid = (
        pd.to_numeric(df.get("bid", df.get("last", df["price"])), errors="coerce")
        + pd.to_numeric(df.get("ask", df.get("last", df["price"])), errors="coerce")
    ) / 2.0
    rng = mid.max() - mid.min()
    return float(rng)


def _tick_rate(ticks: pd.DataFrame, window_s: int = 5) -> float:
    if ticks is None or ticks.empty:
        return 0.0
    if not np.issubdtype(ticks["time"].dtype, np.datetime64):
        ts = pd.to_datetime(ticks["time"], errors="coerce", unit="s", utc=True)
    else:
        ts = ticks["time"]
    cutoff = ts.max() - pd.Timedelta(seconds=window_s)
    return float((ts >= cutoff).sum()) / float(window_s)


def _last_spread(ticks: pd.DataFrame) -> float:
    if ticks is None or ticks.empty:
        return 0.0
    bid = pd.to_numeric(ticks.get("bid"), errors="coerce")
    ask = pd.to_numeric(ticks.get("ask"), errors="coerce")
    if bid is not None and ask is not None and bid.notna().any() and ask.notna().any():
        return float(ask.iloc[-1] - bid.iloc[-1])
    # fallback: approx via last price jitter
    last = pd.to_numeric(ticks.get("last", ticks.get("price")), errors="coerce")
    if last is None or last.empty:
        return 0.0
    return float(last.diff().abs().median() or 0.0)


def _compute_footprint_snapshot(
    ticks: pd.DataFrame,
    price_step: float,
    window_s: int = 5,
    dynamic_price_step: bool = False,  # Nouveau paramètre pour activer un price_step dynamique
    logger: Optional[logging.Logger] = None,  # ✅ AJOUTÉ pour debug
) -> Tuple[pd.DataFrame, dict]:
    """
    Construit un snapshot footprint sur 'window_s' dernières secondes.
    Retourne (df_levels, meta) où:
      - df_levels indexé par 'price' (sorted), colonnes:
        ['ask_vol','bid_vol','vol','delta','delta_ratio','zscore_vol','is_poc']
      - meta: {'tick_rate','spread','window_s','poc_price'}
    """
    if logger is None:
        logger = logging.getLogger(__name__)

    logger.info(f"[SNAPSHOT][DEBUG] Début | window_s={window_s} | ticks_input={len(ticks) if ticks is not None else 0}")

    meta = {"tick_rate": 0.0, "spread": 0.0, "window_s": window_s, "poc_price": None}
    if ticks is None or len(ticks) == 0:
        logger.warning(f"[SNAPSHOT][DEBUG] Ticks vide ou None → retour vide")
        return (
            pd.DataFrame(
                columns=[
                    "ask_vol",
                    "bid_vol",
                    "vol",
                    "delta",
                    "delta_ratio",
                    "zscore_vol",
                    "is_poc",
                ]
            ),
            meta,
        )

    df = ticks.copy()
    logger.info(f"[SNAPSHOT][DEBUG] Après copy | len={len(df)} | columns={list(df.columns)}")

    # time coercition
    if not np.issubdtype(df["time"].dtype, np.datetime64):
        df["time"] = pd.to_datetime(
            df["time"], errors="coerce", unit="s", utc=True
        ).fillna(pd.Timestamp.utcnow())

    time_min = df["time"].min()
    time_max = df["time"].max()
    logger.info(f"[SNAPSHOT][DEBUG] time_range | min={time_min} | max={time_max}")

    cutoff = df["time"].max() - pd.Timedelta(seconds=window_s)
    logger.info(f"[SNAPSHOT][DEBUG] cutoff={cutoff} | window_s={window_s}")

    df = df[df["time"] >= cutoff].copy()
    logger.info(f"[SNAPSHOT][DEBUG] Après filtrage temporel | len={len(df)} | cutoff={cutoff}")

    if df.empty:
        logger.warning(f"[SNAPSHOT][DEBUG] DataFrame vide après filtrage temporel → retour vide")
        return (
            pd.DataFrame(
                columns=[
                    "ask_vol",
                    "bid_vol",
                    "vol",
                    "delta",
                    "delta_ratio",
                    "zscore_vol",
                    "is_poc",
                ]
            ),
            meta,
        )

    # side / prix / volume robustes
    price = pd.to_numeric(df.get("last", df.get("price")), errors="coerce")
    bid = pd.to_numeric(df.get("bid", price), errors="coerce")
    ask = pd.to_numeric(df.get("ask", price), errors="coerce")

    # ✅ FIX: Prioriser volume_real (MT5 ticks) puis volume, puis vol, puis fallback 1.0
    vol = pd.to_numeric(df.get("volume_real", df.get("volume", df.get("vol", 1.0))), errors="coerce").fillna(1.0)

    # ✅ FIX BROKER: Si broker ne fournit pas de volume (moyenne=0), utiliser volume synthétique=1.0 par tick
    if vol.mean() == 0.0 or (vol == 0.0).all():
        logger.warning(f"[SNAPSHOT][DEBUG] Broker ne fournit pas de volume (vol_mean=0.00) → Fallback volume synthétique=1.0 par tick")
        vol = pd.Series(1.0, index=df.index)

    logger.info(f"[SNAPSHOT][DEBUG] Extraction colonnes | price_na={price.isna().sum()} | bid_na={bid.isna().sum()} | ask_na={ask.isna().sum()} | vol_mean={vol.mean():.2f}")

    # tentative de side: si 'side' absent, inférer vs mid
    if "side" in df.columns:
        side = df["side"].astype(str).str.lower()
        is_buy = side.isin(["buy", "ask", "a", "b"])  # tolérance
        logger.info(f"[SNAPSHOT][DEBUG] side depuis colonne | buy={is_buy.sum()} | sell={(~is_buy).sum()}")
    else:
        mid = (bid + ask) / 2.0
        is_buy = price >= mid
        logger.info(f"[SNAPSHOT][DEBUG] side inféré vs mid | buy={is_buy.sum()} | sell={(~is_buy).sum()}")

    # gestion du price_step dynamique ou statique
    if price_step <= 0 or dynamic_price_step:
        # Déduire un pas moyen (fallback dynamique)
        price_step = (
            float(np.nanmedian(np.abs(price.diff().dropna()).replace(0.0, np.nan)))
            or 0.1
        )
        logger.info(f"[SNAPSHOT][DEBUG] price_step dynamique calculé={price_step}")
    else:
        logger.info(f"[SNAPSHOT][DEBUG] price_step statique={price_step}")

    # re-binner les prix au pas
    rounded = np.round(price / price_step) * price_step
    logger.info(f"[SNAPSHOT][DEBUG] Binning | price_step={price_step} | rounded_unique={len(rounded.dropna().unique())}")

    agg = defaultdict(lambda: [0.0, 0.0])
    for p, v, b in zip(rounded, vol, is_buy):
        if np.isnan(p) or np.isnan(v):
            continue
        if b:
            agg[p][0] += float(v)
        else:
            agg[p][1] += float(v)

    logger.info(f"[SNAPSHOT][DEBUG] Agrégation | niveaux_prix={len(agg)}")

    rows = []
    for p, (ask_vol, bid_vol) in agg.items():
        total = ask_vol + bid_vol
        delta = ask_vol - bid_vol
        ratio = (abs(delta) / total) if total > 0 else 0.0
        rows.append((p, ask_vol, bid_vol, total, delta, ratio))

    logger.info(f"[SNAPSHOT][DEBUG] Construction rows | rows_count={len(rows)}")

    if not rows:
        logger.warning(f"[SNAPSHOT][DEBUG] Aucun row construit → retour vide")
        return (
            pd.DataFrame(
                columns=["ask_vol", "bid_vol", "vol", "delta", "delta_ratio", "is_poc"]
            ),
            meta,
        )

    levels = (
        pd.DataFrame(
            rows, columns=["price", "ask_vol", "bid_vol", "vol", "delta", "delta_ratio"]
        )
        .sort_values("price")
        .set_index("price")
    )

    logger.info(f"[SNAPSHOT][DEBUG] DataFrame levels créé | len={len(levels)} | index={levels.index.min():.2f}→{levels.index.max():.2f}")

    # z-score du volume par niveau (dans la fenêtre)
    m = float(levels["vol"].mean() or 0.0)
    s = float(levels["vol"].std(ddof=0) or 1.0)
    levels["zscore_vol"] = (levels["vol"] - m) / (s if s != 0 else 1.0)

    # POC du snapshot
    poc_price = float(levels["vol"].idxmax()) if not levels["vol"].empty else None
    levels["is_poc"] = levels.index == poc_price

    # meta
    meta["tick_rate"] = _tick_rate(df, window_s=5)
    meta["spread"] = _last_spread(df)
    meta["poc_price"] = poc_price

    logger.info(f"[SNAPSHOT][DEBUG] Fin OK | levels={len(levels)} | poc={poc_price} | tick_rate={meta['tick_rate']:.2f}")

    return levels, meta


# ---------------------------------------------------------------------------


class FeaturesExtractor:
    def __init__(
        self, logger: Optional[logging.Logger] = None, config_manager: Any = None
    ):
        self.logger = logger or logging.getLogger(__name__)
        self.config_manager = config_manager

    def clean_dataframe(self, df: pd.DataFrame) -> pd.DataFrame:
        return _clean_dataframe(self, df)

    def get_swing_points(self, df: pd.DataFrame, order: Optional[int] = None):
        return _get_swing_points(self, df, order)

    def calculate_volatility_regime(self, df: pd.DataFrame) -> str:
        return _calculate_volatility_regime(self, df)

    def get_trend(self, df: pd.DataFrame) -> pd.Series:
        return _get_trend(self, df)

    def calculate_quality_metrics(
        self, tf_analyses: Dict, confluence: Dict, divergences: Dict, start_time: float
    ):
        return _calculate_quality_metrics(
            self, tf_analyses, confluence, divergences, start_time
        )

    def fetch_timeframe_data(self, asset: str, timeframe: str, config: Dict):
        return _fetch_timeframe_data(self, asset, timeframe, config)


def _clean_dataframe(self, df: pd.DataFrame, asset: str = "UNKNOWN") -> pd.DataFrame:
    """
    Nettoie et standardise un DataFrame OHLCV (issu MT5) pour le PhaseObserver.
    - Garantit un index datetime UTC trié (index='time')
    - Convertit/valide les colonnes numériques essentielles
    - Supprime les timestamps dupliqués (FIFO : garde la plus récente)
    - Ajoute contract_size / tick_size si manquants
    """
    # Log supprimé - trop verbeux (appelé à chaque cycle)

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
                df["time"] = pd.to_datetime(
                    df["time"], unit="s", utc=True, errors="coerce"
                )
            except Exception as e:
                self.logger.error(
                    f"PhaseObserver: erreur conversion 'time' en datetime: {e}",
                    exc_info=True,
                )
                return pd.DataFrame()

            before = len(df)
            df.dropna(subset=["time"], inplace=True)
            dropped = before - len(df)
            if dropped > 0:
                self.logger.warning(
                    f"PhaseObserver: {dropped} lignes supprimées (timestamps invalides)."
                )
            if df.empty:
                self.logger.warning(
                    "PhaseObserver: plus aucune ligne valide après conversion 'time'."
                )
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
            self.logger.warning(
                f"PhaseObserver: colonne '{col}' absente. Ajoutée avec 0.0."
            )
            df[col] = 0.0
        df[col] = pd.to_numeric(df[col], errors="coerce")

        if col == "tick_volume":
            df[col] = df[col].fillna(0.0)
            neg_mask = df[col] < 0
            if neg_mask.any():
                n = int(neg_mask.sum())
                df.loc[neg_mask, col] = 0.0
                self.logger.warning(
                    f"PhaseObserver: {n} volumes négatifs corrigés à 0.0 dans '{col}'."
                )
        else:
            df[col] = df[col].fillna(0.00001)
            nonpos_mask = df[col] <= 0
            if nonpos_mask.any():
                n = int(nonpos_mask.sum())
                df.loc[nonpos_mask, col] = 0.00001
                self.logger.warning(
                    f"PhaseObserver: {n} valeurs ≤ 0 corrigées à 0.00001 dans '{col}'."
                )

    # 5) Supprimer les timestamps dupliqués (FIFO)
    before = len(df)
    df = df[~df.index.duplicated(keep="last")]
    removed = before - len(df)
    if removed > 0:
        self.logger.debug(
            f"PhaseObserver: {removed} entrées dupliquées supprimées (index time, FIFO)."
        )

    # 6) Ajout contract_size / tick_size si absents
    if "trade_contract_size" not in df.columns:
        if asset.upper().startswith("XAU"):
            df["trade_contract_size"] = 100.0
        else:
            df["trade_contract_size"] = 100000.0
        self.logger.debug(f"[CLEAN] contract_size injecté pour {asset}")

    if "trade_tick_size" not in df.columns:
        tick_size = None
        if "point" in df.columns:
            try:
                tick_size = float(df["point"].iloc[-1])
            except Exception:
                pass
        if not tick_size or tick_size <= 0:
            tick_size = 0.00001
        df["trade_tick_size"] = tick_size
        self.logger.debug(f"[CLEAN] tick_size injecté pour {asset}")

    # Log supprimé - trop verbeux (appelé à chaque cycle)
    return df


def _get_swing_points(
    self, df: pd.DataFrame, order: Optional[int] = None
) -> Tuple[pd.Series, pd.Series]:
    """
    Swing Points simplifiés pour Burst Scalping.
    - Avec 'order' → calcule les swings fixes.
    - Sans 'order' → retourne vide (on ne délègue plus à l'adaptatif).
    """
    if df is None or df.empty:
        return pd.Series([], dtype=float), pd.Series([], dtype=float)

    if order is not None:
        window_size = int(2 * order + 1)
        if len(df) < window_size:
            return pd.Series([], dtype=float), pd.Series([], dtype=float)

        highs_condition = (
            df["high"]
            == df["high"]
            .rolling(window=window_size, center=True, min_periods=window_size)
            .max()
        )
        lows_condition = (
            df["low"]
            == df["low"]
            .rolling(window=window_size, center=True, min_periods=window_size)
            .min()
        )

        swing_highs = df.loc[highs_condition, "high"]
        swing_lows = df.loc[lows_condition, "low"]
        return swing_highs, swing_lows

    # Si aucun paramètre 'order' n'est fourni → pas de calcul swing en mode Burst
    return pd.Series([], dtype=float), pd.Series([], dtype=float)


def _calculate_volatility_regime(self, df: pd.DataFrame) -> str:
    """
    ⚡ Version avancée pour Burst Scalping & Liquidity
    Calcule le régime de volatilité actuel (low/normal/high) pour adapter la stratégie :
    - Utilise ATR + retour log-normalisé (robuste aux outliers)
    - Combine un score relatif (percentiles) et absolu (seuils dynamiques)
    - Peut être enrichi par config (phase_detection_defaults.volatility_regime)
    """
    if df is None or df.empty or len(df) < 20:
        return "normal_vol"

    try:
        # --- 1) Config flexible ------------------------------------------------
        cfg = {}
        try:
            cfg = (
                self.config_manager.get(
                    "phase_detection_defaults.volatility_regime", {}
                )
                or {}
            )
        except Exception:
            pass

        atr_period = int(cfg.get("atr_period", 14))
        lookback = int(cfg.get("lookback", 100))
        high_pct = float(cfg.get("high_percentile", 75))  # top 25%
        low_pct = float(cfg.get("low_percentile", 25))  # bottom 25%
        min_samples = max(atr_period * 2, lookback // 2)

        if len(df) < min_samples:
            self.logger.warning(
                "Pas assez de données pour calcul volatilité → normal_vol"
            )
            return "normal_vol"

        # --- 2) ATR (Average True Range) --------------------------------------
        high, low, close = df["high"], df["low"], df["close"]
        prev_close = close.shift(1)

        tr_components = pd.concat(
            [(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()],
            axis=1,
        )

        true_range = tr_components.max(axis=1)
        atr = true_range.rolling(window=atr_period, min_periods=1).mean()

        # --- 3) Log returns vol (complément) ----------------------------------
        returns = close.pct_change().apply(
            lambda x: np.log(1 + x) if pd.notna(x) else 0.0
        )
        ret_vol = returns.rolling(window=atr_period, min_periods=1).std()

        # --- 4) Score composite -----------------------------------------------
        vol_series = (atr / close) + ret_vol  # normalisé
        vol_series = vol_series.dropna().tail(lookback)

        if vol_series.empty:
            return "normal_vol"

        current = float(vol_series.iloc[-1])
        pct_rank = float((vol_series <= current).mean() * 100)  # position percentile

        # --- 5) Classification -----------------------------------------------
        if pct_rank >= high_pct:
            regime = "high_vol"
        elif pct_rank <= low_pct:
            regime = "low_vol"
        else:
            regime = "normal_vol"

        # --- 6) Debug optionnel -----------------------------------------------
        if getattr(self, "debug_volatility_logging", False):
            try:
                self.logger.debug(
                    f"[VOL] current={current:.6f} pct={pct_rank:.1f} "
                    f"thr=({low_pct}..{high_pct}) regime={regime}"
                )
            except Exception:
                pass

        return regime

    except Exception as e:
        self.logger.error(f"[VOL] Erreur calcul régime: {e}", exc_info=True)
        return "normal_vol"


def _get_trend(self, df: pd.DataFrame) -> pd.Series:
    """
    Détermine la tendance dominante pour chaque point (vectoriel).
    Retourne une série: 'bullish' | 'bearish' | 'neutral'.
    """
    # Paramètres depuis config (fallbacks sûrs)
    try:
        trend_window = int(
            getattr(
                self,
                "TREND_WINDOW",
                self.config_manager.get("phase_detection_defaults.trend_window", 20),
            )
        )
        trend_sma_fast_ratio = float(
            getattr(
                self,
                "trend_sma_fast_ratio",
                self.config_manager.get(
                    "phase_detection_defaults.trend_sma_fast_ratio", 0.3
                ),
            )
        )
        trend_sma_slow_ratio = float(
            getattr(
                self,
                "trend_sma_slow_ratio",
                self.config_manager.get(
                    "phase_detection_defaults.trend_sma_slow_ratio", 0.7
                ),
            )
        )
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

    return pd.Series(
        np.select(trend_conditions, trend_outcomes, default="neutral"), index=df.index
    )


def _calculate_quality_metrics(
    self, tf_analyses: Dict, confluence: Dict, divergences: Dict, start_time: float
) -> Dict[str, Any]:
    """
    Calcul 'desk' des métriques de qualité globales pour l'analyse multi-TF.
    - Pondérations explicites et stables
    - Couverture réelle vs TF attendus (d'après weights_used si dispo)
    - Cohérence phase/biais (accord pondéré)
    - Pénalité proportionnelle aux divergences
    - Bonus/Malus de performance (latence)
    Signature conservée.
    """

    # ---------- Sécurisation des inputs ----------
    tf_analyses = tf_analyses or {}
    confluence = confluence or {}
    divergences = divergences or {}

    # ---------- Couverture (0..1) ----------
    # Si confluence fournit 'weights_used', on s'en sert pour définir l'ensemble des TF attendus.
    weights_used = confluence.get("weights_used") or {}
    expected_tfs = (
        set(map(str, weights_used.keys())) if weights_used else {"M1", "M5", "M15"}
    )
    present_tfs = set(map(str, tf_analyses.keys()))
    denom_cov = max(1, len(expected_tfs))
    data_coverage = min(1.0, len(present_tfs.intersection(expected_tfs)) / denom_cov)

    # ---------- Confluence (0..1) ----------
    confluence_score = float(confluence.get("confluence_score", 0.0) or 0.0)
    base_confluence = float(
        confluence.get("base_confluence", confluence_score) or confluence_score
    )

    # ---------- Cohérence temporelle (phase/biais) (0..1) ----------
    # Phase: strict + bucket; Biais: poids du biais gagnant
    phase_consistency_strict = bool(confluence.get("phase_consistency", False))
    phase_bucket_agreement = float(confluence.get("phase_bucket_agreement", 0.0) or 0.0)
    bias = str(confluence.get("bias", "NEUTRAL") or "NEUTRAL")
    bias_weights = confluence.get("bias_weights", {}) or {}
    bias_agreement = float(bias_weights.get(bias, 0.0) or 0.0)

    # Combinaison cohérence: on valorise l'accord "large" (bucket) + biais
    temporal_consistency = max(
        0.0, min(1.0, 0.6 * phase_bucket_agreement + 0.4 * bias_agreement)
    )
    # Petit supplément si strictement identiques (capé)
    if phase_consistency_strict:
        temporal_consistency = min(1.0, temporal_consistency + 0.05)

    # ---------- Divergences (pénalités 0..0.35) ----------
    # On prend en compte le booléen, le nombre de conflits et une sévérité si disponible.
    has_conflicts = bool(divergences.get("has_conflicts", False))
    conflict_count = int(divergences.get("conflict_count", 0) or 0)
    severity = float(divergences.get("severity", 0.0) or 0.0)  # 0..1
    # Barème: chaque conflit coûte 0.05 jusqu'à 0.25 + sévérité jusqu'à 0.10 → cap 0.35
    divergence_penalty = 0.0
    if has_conflicts or conflict_count > 0 or severity > 0:
        divergence_penalty = min(
            0.35, 0.05 * max(1, conflict_count) + 0.10 * max(0.0, min(1.0, severity))
        )

    # ---------- Performance (latence) (0..1) ----------
    execution_time_ms = (time.perf_counter() - float(start_time)) * 1000.0
    # Score runtime: 1 à 120ms, décroissance linéaire jusqu'à 0.2 à 600ms, <0.2 au-delà capé à 0.1
    if execution_time_ms <= 120:
        runtime_score = 1.0
    elif execution_time_ms >= 600:
        runtime_score = 0.1
    else:
        # map 120..600 ms -> 1.0..0.2
        runtime_score = 1.0 - (execution_time_ms - 120.0) * (0.8 / 480.0)
        runtime_score = max(0.2, runtime_score)

    # ---------- Agrégation (poids explicites, somme≈1, puis pénalité divergences) ----------
    # Poids 'desk' (stables) :
    w_cov = 0.20
    w_conf = 0.45
    w_temp = 0.20
    w_rt = 0.15

    raw_score = (
        w_cov * data_coverage
        + w_conf * confluence_score
        + w_temp * temporal_consistency
        + w_rt * runtime_score
    )

    overall_score = max(0.0, min(1.0, raw_score - divergence_penalty))

    # ---------- Grading ----------
    if overall_score >= 0.90:
        grade = "S"  # superb
    elif overall_score >= 0.80:
        grade = "A"
    elif overall_score >= 0.70:
        grade = "B"
    elif overall_score >= 0.60:
        grade = "C"
    else:
        grade = "D"

    return {
        "overall_score": round(overall_score, 3),
        "components": {
            "data_coverage": round(float(data_coverage), 3),
            "confluence_score": round(float(confluence_score), 3),
            "base_confluence": round(float(base_confluence), 3),
            "temporal_consistency": round(float(temporal_consistency), 3),
            "runtime_score": round(float(runtime_score), 3),
            "divergence_penalty": round(float(divergence_penalty), 3),
        },
        "weights": {
            "coverage": w_cov,
            "confluence": w_conf,
            "temporal": w_temp,
            "runtime": w_rt,
        },
        "flags": {
            "phase_consistency_strict": phase_consistency_strict,
            "has_conflicts": has_conflicts,
        },
        "execution_time_ms": float(round(execution_time_ms, 2)),
        "performance_grade": grade,
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
            self.logger.warning(
                "MetaTrader5 (mt5) non disponible dans l'environnement."
            )
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
        if (
            hasattr(self.config_manager, "mt5_connector")
            and self.config_manager.mt5_connector
        ):
            mt5_data = self.config_manager.mt5_connector.get_rates(
                asset, tf_key, lookback_bars
            )
            if mt5_data is not None and not mt5_data.empty:
                cleaned_data = _clean_dataframe(self, mt5_data)
                self.logger.debug(
                    f"[TFMAP] {asset} {tf_key} -> lookback={lookback_bars} | "
                    f"source={'JSON' if tf_key in cfg_map else 'fallback'}"
                )
                return cleaned_data
            else:
                self.logger.warning(
                    f"[{asset}] Données vides/None pour TF={tf_key}, lookback={lookback_bars}"
                )

    except Exception as e:
        self.logger.error(f"Erreur acquisition {asset} {timeframe}: {e}", exc_info=True)
        return None
