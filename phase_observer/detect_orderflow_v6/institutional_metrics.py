# phase_observer/detect_orderflow_v6/institutional_metrics.py
from __future__ import annotations
from typing import Dict, Any, Tuple, List, Optional
import numpy as np
import pandas as pd
import math

# ============================
# Helpers numériques
# ============================

def _to_num(s, default=0.0) -> np.ndarray:
    return pd.to_numeric(s, errors="coerce").fillna(default).to_numpy(dtype=float)

def _infer_tick_size(prices: np.ndarray) -> float:
    """Déduit un pas de prix plausible à partir des incréments observés."""
    if prices.size < 3:
        return max(1e-6, np.nanstd(prices) * 1e-3) or 1e-4
    diffs = np.diff(np.sort(prices))
    diffs = diffs[diffs > 0]
    if diffs.size == 0:
        return max(1e-6, np.nanstd(prices) * 1e-3)
    # On prend un petit quantile pour éviter les artefacts → 10e centile
    return float(max(1e-6, np.quantile(diffs, 0.10)))

def _freedman_diaconis_bin_width(prices: np.ndarray) -> float:
    """Largeur de bin par règle de Freedman–Diaconis."""
    if prices.size < 3:
        return max(1e-6, np.nanstd(prices) * 0.2)
    iqr = np.subtract(*np.nanpercentile(prices, [75, 25]))
    if iqr <= 0:
        return max(1e-6, np.nanstd(prices) * 0.2)
    n = prices.size
    return float(2 * iqr * (n ** (-1/3)))

def _build_edges(
    pmin: float, pmax: float,
    bin_width: float,
) -> np.ndarray:
    if not np.isfinite(bin_width) or bin_width <= 0:
        bin_width = (pmax - pmin) / 50.0 if pmax > pmin else 1e-3
    # marge minime pour inclure le dernier prix
    lo = pmin
    hi = pmax + 1e-9
    nbins = max(5, int(math.ceil((hi - lo) / bin_width)))
    edges = lo + bin_width * np.arange(nbins + 1, dtype=float)
    return edges

def _expand_va(vol_hist: np.ndarray, edges: np.ndarray, coverage: float = 0.70) -> Tuple[float, float, int]:
    """Greedy VA autour du POC jusqu'à 'coverage' du volume."""
    coverage = float(min(0.98, max(0.50, coverage)))
    total = float(vol_hist.sum())
    if total <= 0:
        return np.nan, np.nan, -1
    i_poc = int(np.argmax(vol_hist))
    cum = float(vol_hist[i_poc])
    L = i_poc - 1
    R = i_poc + 1
    while cum < coverage * total and (L >= 0 or R < len(vol_hist)):
        left = vol_hist[L] if L >= 0 else -1.0
        right = vol_hist[R] if R < len(vol_hist) else -1.0
        if right >= left:
            cum += max(0.0, right)
            R += 1
        else:
            cum += max(0.0, left)
            L -= 1
    va_low = edges[max(L + 1, 0)]
    va_high = edges[min(R, len(edges) - 1)]
    return float(va_low), float(va_high), i_poc

def _local_extrema(vol: np.ndarray) -> Tuple[List[int], List[int]]:
    """Indices des pics/creux locaux simples (stricts) sur un histogramme."""
    peaks, valleys = [], []
    for i in range(1, len(vol) - 1):
        if vol[i] > vol[i - 1] and vol[i] >= vol[i + 1]:
            peaks.append(i)
        if vol[i] < vol[i - 1] and vol[i] <= vol[i + 1]:
            valleys.append(i)
    return peaks, valleys

def _suppress_close(peaks: List[int], min_sep: int) -> List[int]:
    """Supprime les pics trop proches (garde le plus gros)."""
    if not peaks:
        return []
    peaks = sorted(set(peaks))
    keep = [peaks[0]]
    for idx in peaks[1:]:
        if idx - keep[-1] >= min_sep:
            keep.append(idx)
    return keep

# ============================
# Profil par chevauchement OHLC (institutionnel)
# ============================

def _accumulate_profile_ohlc_overlap(
    o: np.ndarray, h: np.ndarray, l: np.ndarray, c: np.ndarray,
    vol_src: np.ndarray,
    edges: np.ndarray,
    body_gain: float = 0.6,
) -> np.ndarray:
    """
    Distribution volumique par chevauchement [low, high] avec surpondération
    de la zone de corps (entre open et close). body_gain∈[0..1] → multiplicateur 1+body_gain.
    Complexité ~ O(n_bars * n_bins), robuste et déterministe.
    """
    nbins = edges.size - 1
    out = np.zeros(nbins, dtype=float)
    if nbins <= 0 or o.size == 0:
        return out

    body_gain = float(min(1.0, max(0.0, body_gain)))
    body_mult = 1.0 + body_gain  # p.ex. 1.6 si gain=0.6

    for i in range(o.size):
        lo = float(min(l[i], h[i]))
        hi = float(max(l[i], h[i]))
        if not (np.isfinite(lo) and np.isfinite(hi)) or hi <= lo:
            # bar plate → on reporte au bin du close
            price = float(c[i]) if np.isfinite(c[i]) else float(o[i])
            if not np.isfinite(price):
                continue
            idx = int(np.searchsorted(edges, price, side="right") - 1)
            if 0 <= idx < nbins:
                out[idx] += float(vol_src[i])
            continue

        # Overlaps longueur par bin
        lefts = edges[:-1]
        rights = edges[1:]
        overlap = np.minimum(hi, rights) - np.maximum(lo, lefts)
        overlap = np.maximum(overlap, 0.0)

        if not np.any(overlap):
            continue

        # Surpondération du corps (zone [min(o,c), max(o,c)])
        b_lo = float(min(o[i], c[i]))
        b_hi = float(max(o[i], c[i]))
        body_overlap = np.minimum(b_hi, rights) - np.maximum(b_lo, lefts)
        body_overlap = np.maximum(body_overlap, 0.0)

        # Densité piècewise: base 1.0 + bonus sur les bins traversant le corps
        density = np.ones_like(overlap)
        density = density + body_mult - 1.0  # set to body_mult everywhere
        # Mais on neutralise hors corps
        density[body_overlap <= 0.0] = 1.0

        weights = overlap * density
        sw = float(weights.sum())
        if sw <= 0.0:
            continue

        out += float(vol_src[i]) * (weights / sw)

    return out

# ============================
# API publique
# ============================

def calculate_volume_profile(
    df: pd.DataFrame,
    *,
    price_bins: Optional[int] = None,     # si fourni → force nb de bins
    bin_width: Optional[float] = None,    # sinon width déterminée par FD + tick_size
    tick_size: Optional[float] = None,    # si None → inféré
    use_ohlc_overlap: bool = True,        # True = profil institutionnel par overlap OHLC
    body_gain: float = 0.6,               # poids relatif du corps (0..1)
    coverage: float = 0.70,               # % de VA
    peak_std: float = 1.0,                # seuil de proéminence pour HVN (en σ)
    min_separation_ticks: int = 5,        # séparation min HVN/LVN en ticks
    max_bins: int = 400,                  # garde-fou perf/mémoire
    ib_bars: int = 30,                    # Initial Balance (premières N barres)
    return_nodes: bool = False,           # renvoyer les “nodes” (liste détaillée)
) -> Dict[str, Any]:
    """
    Calcule un Volume Profile institutionnel sur la fenêtre fournie.
    - Distribution par chevauchement OHLC avec surpondération du corps
    - VPOC, VA (coverage), HVN/LVN, modalité, métriques shape (skew/kurt/entropy)
    - IB (Initial Balance) sur les premières `ib_bars`

    Sortie (principales clés):
      vpoc_price, va_low, va_high, va_coverage
      bins_count, bin_width, price_min, price_max
      hvn (top 3), lvn (top 3), modality
      balance_metrics{va_width, va_width_frac, skewness, kurtosis, entropy, tail_left_frac, tail_right_frac}
      ib{high, low, width, vpoc_in_ib}
      (optionnel) nodes: [{type, price, vol, strength}]
    """
    if df is None or len(df) == 0:
        return { "vpoc_price": None, "va_low": None, "va_high": None, "va_coverage": coverage }

    # ----------- Séries prix / volumes robustes -----------
    o = _to_num(df.get("open", np.nan), np.nan)
    h = _to_num(df.get("high", np.nan), np.nan)
    l = _to_num(df.get("low", np.nan), np.nan)
    c = _to_num(df.get("close", np.nan), np.nan)

    # volume source: priorité real_volume > ask+bid > tick_volume
    if "real_volume" in df.columns and np.nan_to_num(df["real_volume"]).sum() > 0:
        vol_src = _to_num(df["real_volume"], 0.0)
    elif {"ask_volume", "bid_volume"}.issubset(df.columns):
        vol_src = _to_num(df["ask_volume"], 0.0) + _to_num(df["bid_volume"], 0.0)
    else:
        vol_src = _to_num(df.get("tick_volume", 0.0), 0.0)

    mask = np.isfinite(c)
    if not np.any(mask):
        return { "vpoc_price": None, "va_low": None, "va_high": None, "va_coverage": coverage }

    prices = c[mask]
    pmin = float(np.nanmin(np.minimum.reduce([o, h, l, c])))
    pmax = float(np.nanmax(np.maximum.reduce([o, h, l, c])))
    if not (np.isfinite(pmin) and np.isfinite(pmax)) or pmax <= pmin:
        pmin, pmax = float(np.nanmin(prices)), float(np.nanmax(prices))
    if not (np.isfinite(pmin) and np.isfinite(pmax)) or pmax <= pmin:
        # fallback ultime: histogramme du close
        hist, edges = np.histogram(prices, bins=price_bins or 20, weights=vol_src[mask])
        i = int(np.argmax(hist))
        vpoc_price = float((edges[i] + edges[i + 1]) / 2.0)
        va_low, va_high, _ = _expand_va(hist, edges, coverage=coverage)
        return {
            "vpoc_price": vpoc_price,
            "va_low": va_low,
            "va_high": va_high,
            "va_coverage": coverage,
            "bins_count": int(hist.size),
            "bin_width": float(edges[1]-edges[0]) if edges.size > 1 else None,
            "price_min": float(edges[0]) if edges.size > 0 else None,
            "price_max": float(edges[-1]) if edges.size > 0 else None,
            "hvn": [], "lvn": [], "modality": "unknown",
            "balance_metrics": {},
            "ib": {},
        }

    # ----------- Construction des bins -----------
    ts = float(tick_size) if (tick_size is not None and tick_size > 0) else _infer_tick_size(prices)
    if bin_width and bin_width > 0:
        width = float(bin_width)
    elif price_bins and price_bins > 0:
        width = (pmax - pmin) / float(price_bins)
    else:
        fd = _freedman_diaconis_bin_width(prices)
        width = max(ts, fd)
    edges = _build_edges(pmin, pmax, width)
    if edges.size > max_bins + 1:
        # Garde-fou: réduis la granularité (fusionne implicitement)
        k = int(math.ceil((edges.size - 1) / max_bins))
        width *= k
        edges = _build_edges(pmin, pmax, width)

    centers = (edges[:-1] + edges[1:]) * 0.5
    nbins = edges.size - 1

    # ----------- Accumulation du profil -----------
    if use_ohlc_overlap:
        vol_hist = _accumulate_profile_ohlc_overlap(o, h, l, c, vol_src, edges, body_gain=body_gain)
    else:
        # simple histogramme pondéré par le close
        vol_hist, _ = np.histogram(prices, bins=edges, weights=vol_src[mask])

    vol_hist = np.nan_to_num(vol_hist, nan=0.0, posinf=0.0, neginf=0.0).astype(float)

    # ----------- VPOC + VA -----------
    va_low, va_high, i_poc = _expand_va(vol_hist, edges, coverage=coverage)
    if i_poc < 0:
        return { "vpoc_price": None, "va_low": None, "va_high": None, "va_coverage": coverage }

    vpoc_price = float(centers[i_poc])

    # ----------- HVN / LVN (peaks/valleys robustes) -----------
    peaks, valleys = _local_extrema(vol_hist)
    mean = float(np.mean(vol_hist))
    std = float(np.std(vol_hist)) or 1e-9
    # proéminence: garder pics > mean + peak_std*std
    strong_peaks = [i for i in peaks if vol_hist[i] >= (mean + peak_std * std)]
    # séparation min en "ticks" (≈ nb de bins car largeur ~ bin_width)
    strong_peaks = _suppress_close(sorted(strong_peaks, key=lambda i: vol_hist[i], reverse=True), min_sep=max(1, min_separation_ticks))
    strong_peaks = strong_peaks[:3]  # top 3

    # LVN = creux sous mean - 0.25*std, entre deux pics ou en bord VA
    lvn_candidates = [i for i in valleys if vol_hist[i] <= (mean - 0.25 * std)]
    lvn_candidates = _suppress_close(sorted(lvn_candidates), min_sep=max(1, min_separation_ticks))
    lvn_candidates = lvn_candidates[:3]

    hvn = [{"price": float(centers[i]), "volume": float(vol_hist[i])} for i in strong_peaks]
    lvn = [{"price": float(centers[i]), "volume": float(vol_hist[i])} for i in lvn_candidates]

    # Modalité (nombre de pics forts)
    modality = "uni" if len(strong_peaks) <= 1 else ("bi" if len(strong_peaks) == 2 else "multi")

    # ----------- Shape metrics (skew/kurt/entropy/tails) -----------
    w = vol_hist / max(1e-12, vol_hist.sum())
    mu = float(np.sum(w * centers))
    var = float(np.sum(w * (centers - mu) ** 2))
    std_p = math.sqrt(max(var, 1e-12))
    skew = float(np.sum(w * ((centers - mu) / std_p) ** 3))
    kurt = float(np.sum(w * ((centers - mu) / std_p) ** 4)) - 3.0  # excès
    entropy = float(-np.sum(w[w > 0] * np.log(w[w > 0])) / math.log(max(2, nbins)))

    va_width = float(va_high - va_low) if (np.isfinite(va_high) and np.isfinite(va_low)) else np.nan
    full_width = float(pmax - pmin) if pmax > pmin else np.nan
    va_width_frac = float(va_width / full_width) if (np.isfinite(va_width) and np.isfinite(full_width) and full_width > 0) else np.nan

    # Tails: part de volume hors VA
    left_tail = float(np.sum(vol_hist[centers < va_low]))
    right_tail = float(np.sum(vol_hist[centers > va_high]))
    tail_left_frac = float(left_tail / max(1e-12, vol_hist.sum()))
    tail_right_frac = float(right_tail / max(1e-12, vol_hist.sum()))

    # ----------- Initial Balance (IB) sur les N premières barres -----------
    ib_info: Dict[str, Any] = {}
    if ib_bars and ib_bars > 1 and len(df) >= 2:
        n = int(min(ib_bars, len(df)))
        hi_ib = float(np.nanmax(_to_num(df["high"].iloc[:n], np.nan)))
        lo_ib = float(np.nanmin(_to_num(df["low"].iloc[:n], np.nan)))
        ib_width = float(hi_ib - lo_ib) if np.isfinite(hi_ib) and np.isfinite(lo_ib) else np.nan
        vpoc_in_ib = bool(lo_ib <= vpoc_price <= hi_ib) if np.isfinite(ib_width) else False
        ib_info = {"bars": n, "high": hi_ib, "low": lo_ib, "width": ib_width, "vpoc_in_ib": vpoc_in_ib}

    # ----------- Sortie -----------
    result = {
        "vpoc_price": vpoc_price,
        "va_low": va_low,
        "va_high": va_high,
        "va_coverage": float(coverage),
        "bins_count": int(nbins),
        "bin_width": float(width),
        "price_min": float(pmin),
        "price_max": float(pmax),
        "hvn": hvn,
        "lvn": lvn,
        "modality": modality,
        "balance_metrics": {
            "va_width": va_width,
            "va_width_frac": va_width_frac,
            "skewness": skew,
            "kurtosis": kurt,
            "entropy": entropy,
            "tail_left_frac": tail_left_frac,
            "tail_right_frac": tail_right_frac,
        },
        "ib": ib_info,
    }

    if return_nodes:
        nodes: List[Dict[str, Any]] = []
        for i in (strong_peaks or []):
            nodes.append({"type": "HVN", "price": float(centers[i]), "volume": float(vol_hist[i]), "strength": float((vol_hist[i]-mean)/(std or 1e-9))})
        for i in (lvn_candidates or []):
            nodes.append({"type": "LVN", "price": float(centers[i]), "volume": float(vol_hist[i]), "strength": float((mean - vol_hist[i])/(std or 1e-9))})
        # VAL/VAH/VPOC comme niveaux
        nodes.append({"type": "VPOC", "price": vpoc_price, "volume": float(vol_hist[i_poc])})
        nodes.append({"type": "VAL", "price": va_low, "volume": float(np.sum(vol_hist[centers <= va_low]))})
        nodes.append({"type": "VAH", "price": va_high, "volume": float(np.sum(vol_hist[centers >= va_high]))})
        result["nodes"] = nodes

    return result
