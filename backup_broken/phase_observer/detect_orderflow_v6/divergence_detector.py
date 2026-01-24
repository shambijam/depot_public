# phase_observer/detect_orderflow_v6/divergence_detector.py
from __future__ import annotations
from typing import Dict, Any, List, Optional, Tuple
import numpy as np
import pandas as pd


def _to_num(
    s: pd.Series | np.ndarray | float, default: float = 0.0, dtype=float
) -> np.ndarray:
    if isinstance(s, np.ndarray):
        a = s.astype(dtype, copy=False)
        a = np.where(np.isfinite(a), a, default)
        return a
    try:
        a = pd.to_numeric(s, errors="coerce").to_numpy(dtype=dtype)
        a = np.where(np.isfinite(a), a, default)
        return a
    except Exception:
        return np.array([], dtype=dtype)


def _local_pivots(x: np.ndarray, window: int = 3) -> Tuple[np.ndarray, np.ndarray]:
    """
    Pivots locaux vectorisés.
      - window = nb de barres de chaque côté (total fenêtre = 2*window+1).
      - Retourne (is_high, is_low) bool arrays.
    """
    n = int(x.size)
    if n == 0 or window < 1:
        return np.zeros(n, dtype=bool), np.zeros(n, dtype=bool)

    # Comparaisons décalées (strictes) pour éviter les plateaux
    gt_all = np.ones(n, dtype=bool)
    lt_all = np.ones(n, dtype=bool)
    for k in range(1, window + 1):
        x_fwd = np.roll(x, -k)
        x_bwd = np.roll(x, +k)
        # bords → False
        fmask = np.ones(n, dtype=bool)
        fmask[-k:] = False
        bmask = np.ones(n, dtype=bool)
        bmask[:k] = False

        gt_all &= (x > x_fwd) & fmask
        gt_all &= (x > x_bwd) & bmask

        lt_all &= (x < x_fwd) & fmask
        lt_all &= (x < x_bwd) & bmask

    # annule les bords où la fenêtre est incomplète
    edge = window
    if edge > 0:
        gt_all[:edge] = False
        gt_all[-edge:] = False
        lt_all[:edge] = False
        lt_all[-edge:] = False

    return gt_all, lt_all


def _pairwise_divergences(
    px: np.ndarray,
    ind: np.ndarray,
    idx_highs: np.ndarray,
    idx_lows: np.ndarray,
    *,
    min_separation: int = 4,
    min_price_move_frac: float = 1e-4,
    min_indicator_move_frac: float = 1e-4,
) -> List[Dict[str, Any]]:
    """
    Scanne paires consécutives de pivots (HH/LL) et détecte divergences:
      - Bearish: prix fait HH (p2>p1) mais indicateur fait LH (i2<i1)
      - Bullish: prix fait LL (p2<p1) mais indicateur fait HL (i2>i1)
    Seuils 'min_*_frac' pour filtrer le bruit (relatifs à |px| et |ind| moyens).
    """
    out: List[Dict[str, Any]] = []
    if px.size == 0 or ind.size == 0:
        return out

    # Échelles relatives
    p_scale = max(1e-12, float(np.nanmean(np.abs(px))))
    i_scale = max(1e-12, float(np.nanmean(np.abs(ind))))

    # --- Highs (bearish divergences) ---
    if idx_highs.size >= 2:
        for i in range(1, idx_highs.size):
            i1, i2 = int(idx_highs[i - 1]), int(idx_highs[i])
            if i2 - i1 < min_separation:
                continue
            p1, p2 = float(px[i1]), float(px[i2])
            v1, v2 = float(ind[i1]), float(ind[i2])

            # prix HH + indicateur LH
            if (p2 > p1) and (v2 < v1):
                dp = (p2 - p1) / p_scale
                di = (v1 - v2) / i_scale
                if dp >= min_price_move_frac and di >= min_indicator_move_frac:
                    strength = float(min(1.0, 0.5 * dp + 0.5 * di))
                    out.append(
                        {
                            "index": i2,
                            "pattern": "bearish_divergence",
                            "pivot_kind": "high",
                            "price_pivot_1": p1,
                            "price_pivot_2": p2,
                            "ind_pivot_1": v1,
                            "ind_pivot_2": v2,
                            "bars_span": int(i2 - i1),
                            "strength": strength,
                        }
                    )

    # --- Lows (bullish divergences) ---
    if idx_lows.size >= 2:
        for i in range(1, idx_lows.size):
            i1, i2 = int(idx_lows[i - 1]), int(idx_lows[i])
            if i2 - i1 < min_separation:
                continue
            p1, p2 = float(px[i1]), float(px[i2])
            v1, v2 = float(ind[i1]), float(ind[i2])

            # prix LL + indicateur HL
            if (p2 < p1) and (v2 > v1):
                dp = (p1 - p2) / p_scale
                di = (v2 - v1) / i_scale
                if dp >= min_price_move_frac and di >= min_indicator_move_frac:
                    strength = float(min(1.0, 0.5 * dp + 0.5 * di))
                    out.append(
                        {
                            "index": i2,
                            "pattern": "bullish_divergence",
                            "pivot_kind": "low",
                            "price_pivot_1": p1,
                            "price_pivot_2": p2,
                            "ind_pivot_1": v1,
                            "ind_pivot_2": v2,
                            "bars_span": int(i2 - i1),
                            "strength": strength,
                        }
                    )
    return out


def _exhaustion_events(
    df: pd.DataFrame,
    total: np.ndarray,
    *,
    vol_q: float = 0.98,
    max_body_frac: float = 0.15,
) -> List[Dict[str, Any]]:
    """
    Exhaustion/Climax: volume extrême mais déplacement de prix faible (corps court).
      - volume >= quantile(vol_q)
      - |close-open| <= max_body_frac * (high-low) (si range>0)
    """
    out: List[Dict[str, Any]] = []
    n = len(df)
    if n == 0 or not np.any(total > 0):
        return out

    try:
        qv = float(np.quantile(total, vol_q))
    except Exception:
        qv = 0.0

    o = _to_num(df.get("open", np.nan), np.nan)
    h = _to_num(df.get("high", np.nan), np.nan)
    l = _to_num(df.get("low", np.nan), np.nan)
    c = _to_num(df.get("close", np.nan), np.nan)
    rng = np.maximum(h - l, 0.0)
    body = np.abs(c - o)

    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = np.divide(body, rng, out=np.zeros_like(rng, dtype=float), where=(rng > 0.0))
    small_body = ratio <= float(max_body_frac)

    is_climax = (total >= qv) & small_body

    if "time" in df.columns:
        ts = df["time"].astype(str).to_numpy()
    elif "timestamp" in df.columns:
        ts = df["timestamp"].astype(str).to_numpy()
    else:
        ts = np.arange(n).astype(str)

    idx = np.where(is_climax)[0]
    for i in idx.tolist():
        out.append(
            {
                "index": int(i),
                "timestamp": str(ts[i]),
                "pattern": "exhaustion",
                "row_total": float(total[i]),
                "body_frac": float(body[i] / rng[i]) if rng[i] > 0 else 0.0,
            }
        )
    return out


def _confirmation_flags(
    df: pd.DataFrame,
    events: List[Dict[str, Any]],
    *,
    confirm_window: int = 10,
) -> None:
    """
    Marque les divergences comme 'confirmed' si, dans les confirm_window barres suivantes:
      - Bullish: close croise au-dessus de VWAP (si vwap dispo) OU cvd_slope devient > 0
      - Bearish: close passe sous VWAP OU cvd_slope < 0
    (in-place: ajoute confirmed: bool)
    """
    n = len(df)
    if n == 0 or not events:
        return

    close = _to_num(df.get("close", np.nan), np.nan)
    vwap = df["vwap"].to_numpy(dtype=float) if "vwap" in df.columns else None
    cvd_slope = (
        df["cvd_slope"].to_numpy(dtype=float) if "cvd_slope" in df.columns else None
    )

    for ev in events:
        i = int(ev["index"])
        j = min(n - 1, i + confirm_window)
        sl = slice(i, j + 1)

        bullish = ev["pattern"] == "bullish_divergence"
        bearish = ev["pattern"] == "bearish_divergence"

        cond = False
        if vwap is not None and np.isfinite(vwap[i]):
            if bullish:
                cond |= bool(np.any(close[sl] > vwap[sl]))
            if bearish:
                cond |= bool(np.any(close[sl] < vwap[sl]))
        if cvd_slope is not None:
            if bullish:
                cond |= bool(np.any(cvd_slope[sl] > 0.0))
            if bearish:
                cond |= bool(np.any(cvd_slope[sl] < 0.0))

        ev["confirmed"] = bool(cond)


def detect_divergences(
    df: pd.DataFrame,
    *,
    price_col: str = "close",
    indicator_col: Optional[str] = None,  # priorité: 'cvd' sinon 'delta' cumulée
    fallback_indicator: str = "cvd",
    pivot_window: int = 3,
    lookback: int = 200,
    min_separation: int = 4,
    min_price_move_frac: float = 1e-4,
    min_indicator_move_frac: float = 1e-4,
    confirm_window: int = 10,
    add_exhaustion: bool = True,
) -> List[Dict[str, Any]]:
    """
    Détecte divergences prix/indicateur + (optionnel) barres d'épuisement.
    - price_col: série de prix (close par défaut)
    - indicator_col: si None → 'cvd' sinon fallback delta cumulée
    - pivot_window: taille demi-fenêtre pour pivots (3 = 7-bar swing)
    - lookback: nb de dernières barres à considérer
    - min_separation: séparation min en barres entre deux pivots de même type
    - *_frac: seuils relatifs (anti-bruit)
    - confirm_window: fenêtre de confirmation (VWAP / cvd_slope)
    """
    events: List[Dict[str, Any]] = []
    if df is None or df.empty:
        return events

    d = df.iloc[-int(lookback) :] if lookback and len(df) > lookback else df

    px = _to_num(d.get(price_col, d.get("close", np.nan)), np.nan)
    if px.size == 0:
        return events

    # Indicateur: cvd prioritaire; sinon delta cumulée; sinon total_volume (faute de mieux)
    if indicator_col and (indicator_col in d.columns):
        ind = _to_num(d[indicator_col], 0.0)
    elif fallback_indicator in d.columns:
        ind = _to_num(d[fallback_indicator], 0.0)
    elif "delta" in d.columns:
        ind = np.cumsum(_to_num(d["delta"], 0.0))
    else:
        ind = _to_num(d.get("total_volume", 0.0), 0.0)

    # Pivots
    highs_mask, lows_mask = _local_pivots(px, window=max(1, int(pivot_window)))
    idx_highs = np.where(highs_mask)[0]
    idx_lows = np.where(lows_mask)[0]

    # Divergences
    divs = _pairwise_divergences(
        px,
        ind,
        idx_highs,
        idx_lows,
        min_separation=int(min_separation),
        min_price_move_frac=float(min_price_move_frac),
        min_indicator_move_frac=float(min_indicator_move_frac),
    )

    # Enrichissement timestamps & dominance
    if "time" in d.columns:
        ts = d["time"].astype(str).to_numpy()
    elif "timestamp" in d.columns:
        ts = d["timestamp"].astype(str).to_numpy()
    else:
        ts = np.arange(len(d)).astype(str)

    # Remap indices relatifs (lookback) → indices globaux du df d’origine
    start_idx = len(df) - len(d)
    for ev in divs:
        i_rel = int(ev["index"])
        i_abs = start_idx + i_rel
        ev["index"] = i_abs
        ev["timestamp"] = str(ts[i_rel])
        ev["dominance"] = "buyers" if ev["pattern"].startswith("bullish") else "sellers"

    events.extend(divs)

    # Exhaustion optionnelle
    if add_exhaustion:
        total = _to_num(d.get("total_volume", d.get("tick_volume", 0.0)), 0.0)
        ex = _exhaustion_events(d, total)
        for ev in ex:
            i_rel = int(ev["index"])
            ev["index"] = start_idx + i_rel
        events.extend(ex)

    # Confirmation flags (utilise df entier pour vwap/cvd_slope)
    _confirmation_flags(df, events, confirm_window=int(confirm_window))

    return events
