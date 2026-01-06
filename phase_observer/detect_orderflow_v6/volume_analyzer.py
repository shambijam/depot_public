# phase_observer/detect_orderflow_v6/volume_analyzer.py
from __future__ import annotations
from typing import Dict, Any, Tuple, Optional
import pandas as pd
import numpy as np
from collections import OrderedDict

# --- Mini cache LRU (Phase 2) ---
_VOL_METRICS_CACHE: "OrderedDict[tuple, Dict[str, Any]]" = OrderedDict()
_VOL_METRICS_CACHE_MAX = 8


def _vm_cache_get(key: tuple) -> Dict[str, Any] | None:
    val = _VOL_METRICS_CACHE.get(key)
    if val is not None:
        _VOL_METRICS_CACHE.move_to_end(key)
    return val


def _vm_cache_put(key: tuple, value: Dict[str, Any]) -> None:
    _VOL_METRICS_CACHE[key] = value
    _VOL_METRICS_CACHE.move_to_end(key)
    while len(_VOL_METRICS_CACHE) > _VOL_METRICS_CACHE_MAX:
        _VOL_METRICS_CACHE.popitem(last=False)


def _vm_cache_key(df: pd.DataFrame) -> tuple:
    """Clé stable et peu coûteuse : (len, time[0], time[-1], sums de colonnes clés)."""
    n = len(df)
    try:
        t0 = (
            str(pd.to_datetime(df["time"].iloc[0], utc=True, errors="coerce"))
            if n
            else ""
        )
        t1 = (
            str(pd.to_datetime(df["time"].iloc[-1], utc=True, errors="coerce"))
            if n
            else ""
        )
    except Exception:
        t0, t1 = "", ""
    sums = []
    for col in (
        "close",
        "high",
        "low",
        "ask_volume",
        "bid_volume",
        "tick_volume",
        "aggressor_buy_vol",
        "aggressor_sell_vol",
    ):
        if col in df.columns:
            try:
                s = pd.to_numeric(df[col], errors="coerce").sum(skipna=True)
                s = float(s) if np.isfinite(s) else 0.0
            except Exception:
                s = 0.0
        else:
            s = 0.0
        sums.append(round(s, 6))  # arrondi pour stabilité
    return (n, t0, t1, *sums)


def _ols_slope_lastN(y: np.ndarray, N: int) -> float:
    """Pente OLS (slope) sur les N derniers points (x = 0..N-1)."""
    y = np.asarray(y, dtype=float)
    n = int(min(max(N, 2), y.size))
    if n < 2:
        return 0.0
    x = np.arange(n, dtype=float)
    y = y[-n:]
    sx, sy = x.sum(), y.sum()
    sxx, sxy = np.dot(x, x), np.dot(x, y)
    den = n * sxx - sx * sx
    if den == 0:
        return 0.0
    return float((n * sxy - sx * sy) / (den + 1e-12))


def calculate_volume_metrics(
    df: pd.DataFrame,
    cvd_smoothing: float = 0.0,
    cvd_slope_window: Optional[int] = None
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """
    Métriques volume alignées V5 + ajouts V6 (sans Numba) :
      - ask/bid prioritaire ; fallback proxy via tick_volume * signe(close-prev)
      - CVD avec lissage EMA via pandas.ewm(alpha=..)
      - VWAP (TP*Vol cumulé / Vol cumulé), pente VWAP, écart prix–VWAP
      - Expose des infos utiles au scoring: rows, coverage_s, tick_rate

    Args:
      cvd_slope_window: Fenêtre pour calcul pente CVD (défaut: 20 ou taille CVD)

    Retour: (df_enrichi, metrics) avec clés:
      total_volume, delta_total, imbalance_mean, buy_ratio, aggress_ratio,
      cvd, cvd_slope, imbalance, vwap, vwap_slope, price_minus_vwap,
      rows, coverage_s, tick_rate
    """
    out: Dict[str, Any] = {}
    df = df.copy()

    # --- cache key (avant conversions coûteuses) ---
    key = _vm_cache_key(df)
    cached = _vm_cache_get(key)

    # ---- Séries prix (pour VWAP / proxy delta) ----
    close = (
        pd.to_numeric(df.get("close", 0.0), errors="coerce")
        .fillna(0.0)
        .to_numpy(dtype=float)
    )
    high = (
        pd.to_numeric(df.get("high", 0.0), errors="coerce")
        .fillna(0.0)
        .to_numpy(dtype=float)
    )
    low = (
        pd.to_numeric(df.get("low", 0.0), errors="coerce")
        .fillna(0.0)
        .to_numpy(dtype=float)
    )
    prev = np.roll(close, 1)
    if close.size:
        prev[0] = close[0]

    have_ab = ("ask_volume" in df.columns) and ("bid_volume" in df.columns)

    # --- Si cache hit, reconstituer à coût marginal ---
    if cached is not None:
        arr = cached["arrays"]
        # Colonnes enrichies minimales
        df["total_volume"] = arr["total_row"]
        df["delta"] = arr["delta_row"]
        df["cvd"] = arr["cvd"]
        df["vwap"] = arr["vwap"]
        df["aggressor_ratio"] = arr["aggressor_ratio"]
        if arr.get("imbalance_row") is not None:
            df["imbalance_row"] = arr["imbalance_row"]

        out = dict(cached["out"])  # shallow copy suffisant
        # (On n’a pas besoin de recalculer slopes; ils sont dans out)
        return df, out

    # ---- Calcul “frais” (pas dans le cache) ----
    if have_ab:
        ask = (
            pd.to_numeric(df["ask_volume"], errors="coerce")
            .fillna(0.0)
            .to_numpy(dtype=float)
        )
        bid = (
            pd.to_numeric(df["bid_volume"], errors="coerce")
            .fillna(0.0)
            .to_numpy(dtype=float)
        )
        total_row = ask + bid
        delta_row = ask - bid

        df["total_volume"] = total_row
        df["delta"] = delta_row
        with np.errstate(divide="ignore", invalid="ignore"):
            imbalance_row = np.where(total_row > 0.0, ask / total_row, 0.5)
            df["imbalance_row"] = imbalance_row

        # Aggressors (fallback ask/bid si colonnes dédiées absentes)
        ag_buy = (
            pd.to_numeric(
                df.get("aggressor_buy_vol", df.get("ask_volume", 0.0)), errors="coerce"
            )
            .fillna(0.0)
            .to_numpy(dtype=float)
        )
        ag_sell = (
            pd.to_numeric(
                df.get("aggressor_sell_vol", df.get("bid_volume", 0.0)), errors="coerce"
            )
            .fillna(0.0)
            .to_numpy(dtype=float)
        )
        den_ag = ag_buy + ag_sell
        with np.errstate(divide="ignore", invalid="ignore"):
            aggressor_ratio = np.where(den_ag > 0.0, ag_buy / den_ag, 0.5)
        df["aggressor_ratio"] = aggressor_ratio

        total_vol = float(np.sum(total_row))
        delta_total = float(np.sum(delta_row))
        imbalance_mean = (
            float(np.sum(ask) / max(1.0, total_vol)) if total_vol > 0.0 else 0.5
        )

        buys = int(np.sum(delta_row > 0.0))
        sells = int(np.sum(delta_row < 0.0))
        bs = buys + sells
        buy_ratio = float(buys / bs) if bs > 0 else 0.5

        vol_for_vwap = total_row
    else:
        vol = (
            pd.to_numeric(df.get("tick_volume", 0.0), errors="coerce")
            .fillna(0.0)
            .to_numpy(dtype=float)
        )
        sign = np.where(close >= prev, 1.0, -1.0)
        delta_row = sign * vol
        total_row = vol

        df["total_volume"] = total_row
        df["delta"] = delta_row

        up = np.where(delta_row >= 0.0, vol, 0.0)
        denom_vol = float(np.sum(vol))
        imbalance_mean = (
            float(np.sum(up) / max(1.0, denom_vol)) if denom_vol > 0.0 else 0.5
        )

        buys = int(np.sum(delta_row > 0.0))
        sells = int(np.sum(delta_row < 0.0))
        bs = buys + sells
        buy_ratio = float(buys / bs) if bs > 0 else 0.5

        total_vol = float(np.sum(total_row))
        delta_total = float(np.sum(delta_row))

        aggressor_ratio = np.full_like(total_row, 0.5, dtype=float)  # neutre
        df["aggressor_ratio"] = aggressor_ratio
        imbalance_row = None  # pas pertinent en fallback
        vol_for_vwap = total_row

    # ---- CVD + lissage EMA via pandas.ewm ----
    cvd = np.cumsum(delta_row.astype(float))
    if cvd_smoothing and float(cvd_smoothing) > 0.0:
        alpha = float(min(0.99, max(0.01, float(cvd_smoothing))))
        cvd = (
            pd.Series(cvd, copy=False).ewm(alpha=alpha, adjust=False).mean().to_numpy()
        )
    df["cvd"] = cvd

    # pente CVD (06 JAN 2026: fenêtre configurable pour cohérence avec lookback)
    if cvd_slope_window is not None and cvd_slope_window > 0:
        N = min(cvd_slope_window, cvd.size) if cvd.size >= cvd_slope_window else max(2, cvd.size)
    else:
        N = 20 if cvd.size >= 20 else max(2, cvd.size)
    cvd_slope = _ols_slope_lastN(cvd, N)

    # ---- VWAP (typical price * vol cumulé / vol cumulé) ----
    typical = (high + low + close) / 3.0
    pv = typical * vol_for_vwap
    cum_pv = np.cumsum(pv)
    cum_vol = np.cumsum(vol_for_vwap)
    with np.errstate(divide="ignore", invalid="ignore"):
        vwap = np.where(cum_vol > 0.0, cum_pv / np.maximum(cum_vol, 1e-12), np.nan)
    df["vwap"] = vwap

    vwap_clean = np.nan_to_num(
        vwap, nan=np.nanmean(vwap) if np.isfinite(np.nanmean(vwap)) else 0.0
    )
    vwap_slope = _ols_slope_lastN(vwap_clean, N)
    last_vwap = vwap[-1] if vwap.size else np.nan
    price_minus_vwap = (
        float(close[-1] - (last_vwap if np.isfinite(last_vwap) else close[-1]))
        if close.size
        else float("nan")
    )

    # ---- Infos optionnelles scoring ----
    rows = int(len(df))
    coverage_s = float("nan")
    tick_rate = float("nan")
    try:
        if "time" in df.columns and rows >= 2:
            t = pd.to_datetime(df["time"], errors="coerce", utc=True)
            t0, t1 = t.iloc[0], t.iloc[-1]
            if pd.notna(t0) and pd.notna(t1):
                coverage_s = max(1.0, (t1 - t0).total_seconds())
                if "tick_volume" in df.columns:
                    tick_rate = float(
                        np.nansum(
                            pd.to_numeric(df["tick_volume"], errors="coerce").to_numpy(
                                dtype=float
                            )
                        )
                        / coverage_s
                    )
    except Exception:
        pass

    # ---- Agrégats de sortie ----
    out.update(
        {
            "total_volume": float(total_vol),
            "delta_total": float(delta_total),
            "imbalance_mean": float(imbalance_mean),
            "buy_ratio": float(buy_ratio),
            "aggress_ratio": (
                float(
                    np.nanmean(
                        pd.to_numeric(df["aggressor_ratio"], errors="coerce")
                        .fillna(0.5)
                        .to_numpy(dtype=float)
                    )
                )
                if rows
                else 0.5
            ),
            "cvd": float(cvd[-1]) if cvd.size else 0.0,
            "cvd_slope": float(cvd_slope),
            "imbalance": float(imbalance_mean * 2.0 - 1.0),
            "vwap": float(last_vwap) if np.isfinite(last_vwap) else float("nan"),
            "vwap_slope": float(vwap_slope),
            "price_minus_vwap": float(price_minus_vwap),
            "rows": rows,
            "coverage_s": float(coverage_s),
            "tick_rate": float(tick_rate),
        }
    )

    # Colonnes downstream
    df["delta_proxy"] = delta_row.astype(float)

    # --- store in cache ---
    _vm_cache_put(
        key,
        {
            "out": out,
            "arrays": {
                "total_row": total_row,
                "delta_row": delta_row,
                "aggressor_ratio": aggressor_ratio if have_ab else aggressor_ratio,
                "imbalance_row": imbalance_row,
                "cvd": cvd,
                "vwap": vwap,
            },
        },
    )

    return df, out
