# phase_observer/detect_orderflow_v6/volume_analyzer.py
from __future__ import annotations
from typing import Dict, Any, Tuple
import pandas as pd
import numpy as np

def _ema(arr: np.ndarray, alpha: float) -> np.ndarray:
    out = np.empty_like(arr, dtype=float)
    if len(arr) == 0: return out
    out[0] = arr[0]
    for i in range(1, len(arr)):
        out[i] = alpha*arr[i] + (1-alpha)*out[i-1]
    return out

def calculate_volume_metrics(df: pd.DataFrame, cvd_smoothing: float = 0.0) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """
    Métriques volume alignées V5 :
      - Par défaut, s'appuie sur ask/bid si présents (parité V5).
      - Fallback proxy via tick_volume * signe(close-prev) si ask/bid absents.
      - Retourne (df enrichi, metrics dict) avec clés :
        total_volume, delta_total, imbalance_mean, buy_ratio, aggress_ratio, cvd, cvd_slope, imbalance(centrée)
    """
    out: Dict[str, Any] = {}
    df = df.copy()

    # --- Préférence ask/bid (parité V5)
    have_ab = ("ask_volume" in df.columns) and ("bid_volume" in df.columns)
    if have_ab:
        ask = pd.to_numeric(df["ask_volume"], errors="coerce").fillna(0.0).to_numpy(dtype=float)
        bid = pd.to_numeric(df["bid_volume"],  errors="coerce").fillna(0.0).to_numpy(dtype=float)
        total_row = ask + bid
        delta_row = ask - bid

        df["total_volume"] = total_row
        df["delta"] = delta_row
        with np.errstate(divide="ignore", invalid="ignore"):
            df["imbalance_row"] = np.where(total_row > 0.0, ask / total_row, 0.5)

        # Aggressors (fallback sur ask/bid si colonnes absentes)
        ag_buy  = pd.to_numeric(df.get("aggressor_buy_vol", df.get("ask_volume", 0.0)), errors="coerce").fillna(0.0).to_numpy(dtype=float)
        ag_sell = pd.to_numeric(df.get("aggressor_sell_vol", df.get("bid_volume", 0.0)), errors="coerce").fillna(0.0).to_numpy(dtype=float)
        den_ag = ag_buy + ag_sell
        with np.errstate(divide="ignore", invalid="ignore"):
            df["aggressor_ratio"] = np.where(den_ag > 0.0, ag_buy / den_ag, 0.5)

        total_vol = float(np.sum(total_row))
        delta_total = float(np.sum(delta_row))
        imbalance_mean = float(np.sum(ask) / max(1.0, total_vol)) if total_vol > 0.0 else 0.5
        buys  = int(np.sum(delta_row > 0.0))
        sells = int(np.sum(delta_row < 0.0))
        bs = buys + sells
        buy_ratio = float(buys / bs) if bs > 0 else 0.5

    else:
        # --- Fallback proxy (tick_volume * signe close-prev)
        close = pd.to_numeric(df.get("close", 0.0), errors="coerce").fillna(0.0).to_numpy(dtype=float)
        prev  = np.roll(close, 1); prev[0] = close[0]
        vol   = pd.to_numeric(df.get("tick_volume", 0.0), errors="coerce").fillna(0.0).to_numpy(dtype=float)

        sign = np.where(close >= prev, 1.0, -1.0)
        delta_row = sign * vol
        total_row = vol

        df["total_volume"] = total_row
        df["delta"] = delta_row
        # Pas d'imbalance_row fiable sans ask/bid ; on calcule imbalance_mean global par proxy "up-volume"
        up = np.where(delta_row >= 0.0, vol, 0.0)
        denom_vol = float(np.sum(vol))
        imbalance_mean = float(np.sum(up) / max(1.0, denom_vol)) if denom_vol > 0.0 else 0.5
        buys  = int(np.sum(delta_row > 0.0))
        sells = int(np.sum(delta_row < 0.0))
        bs = buys + sells
        buy_ratio = float(buys / bs) if bs > 0 else 0.5
        total_vol = float(np.sum(total_row))
        delta_total = float(np.sum(delta_row))

        # Sans aggressors fiables → neutre
        df["aggressor_ratio"] = 0.5

    # --- CVD + pente (EMA optionnelle)
    cvd = np.cumsum(delta_row.astype(float))
    if cvd_smoothing and float(cvd_smoothing) > 0.0:
        alpha = float(min(0.99, max(0.01, float(cvd_smoothing))))
        cvd = _ema(cvd.astype(float), alpha=alpha)

    N = 20 if len(cvd) >= 20 else max(2, len(cvd))
    x = np.arange(N, dtype=float)
    y = cvd[-N:].astype(float)
    slope = 0.0
    if N >= 2:
        sx, sy = x.sum(), y.sum()
        sxx, sxy = np.dot(x, x), np.dot(x, y)
        slope = float((N * sxy - sx * sy) / ((N * sxx - sx * sx) + 1e-12))

    # --- Agrégats & sortie (parité V5 + additions V6)
    out.update({
        "total_volume": float(total_vol),
        "delta_total": float(delta_total),
        "imbalance_mean": float(imbalance_mean),       # [0..1], 0.5 = neutre (clé V5)
        "buy_ratio": float(buy_ratio),                 # [0..1]
        "aggress_ratio": float(np.nanmean(pd.to_numeric(df["aggressor_ratio"], errors="coerce").fillna(0.5).to_numpy(dtype=float))) if len(df) else 0.5,
        "cvd": float(cvd[-1]) if len(cvd) else 0.0,
        "cvd_slope": float(slope),
        # Clé auxiliaire V6 pour pattern_detector (centrée [-1..1], 0 = neutre)
        "imbalance": float(imbalance_mean * 2.0 - 1.0),
    })

    # Colonnes pour patterns / downstream
    df["delta_proxy"] = delta_row.astype(float)
    df["cvd"] = cvd if len(cvd) == len(df) else np.pad(cvd, (len(df) - len(cvd), 0), mode="edge")

    return df, out
