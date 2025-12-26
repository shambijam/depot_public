# phase_observer/detect_orderflow_v6/institutional_metrics.py
from __future__ import annotations
from typing import Dict, Any, Tuple, List, Optional
import numpy as np
import pandas as pd
import math
import hashlib
from collections import OrderedDict


def _value_area_from_profile(
    profile: np.ndarray, edges: np.ndarray, coverage: float = 0.70
) -> tuple[float | None, float | None, int | None]:
    """
    Détermine VA (coverage ex: 70%) en étendant autour du POC, bin par bin, côté le plus volumineux.
    Retourne (va_low, va_high, poc_index).
    """
    if profile is None or len(profile) == 0:
        return None, None, None
    total = float(profile.sum())
    if total <= 0:
        return None, None, None

    i_poc = int(np.argmax(profile))
    cum = float(profile[i_poc])
    l = i_poc - 1
    r = i_poc + 1

    while cum < coverage * total and (l >= 0 or r < len(profile)):
        left = profile[l] if l >= 0 else -1.0
        right = profile[r] if r < len(profile) else -1.0
        if right >= left:
            if r < len(profile):
                cum += max(0.0, profile[r])
                r += 1
        else:
            if l >= 0:
                cum += max(0.0, profile[l])
                l -= 1

    va_low = edges[max(l + 1, 0)]
    va_high = edges[min(r, len(edges) - 1)]
    return float(va_low), float(va_high), i_poc


# ============================
# Helpers numériques
# ============================
def _to_num(x, fallback=np.nan) -> np.ndarray:
    a = np.asarray(pd.to_numeric(x, errors="coerce"), dtype=float)
    if np.isnan(fallback):
        return a
    return np.where(np.isfinite(a), a, float(fallback))


def _infer_tick_size(prices: np.ndarray) -> float:
    p = np.asarray(prices, dtype=float)
    p = p[np.isfinite(p)]
    if p.size < 2:
        return 1e-4
    u = np.unique(np.round(p, 10))
    if u.size < 2:
        return max(1e-9, abs(u[0]) * 1e-6)
    d = np.diff(u)
    d = d[d > 0]
    return float(d.min()) if d.size else max(1e-9, (u.max() - u.min()) / 1e6)


def _freedman_diaconis_bin_width(prices: np.ndarray) -> float:
    p = np.asarray(prices, dtype=float)
    p = p[np.isfinite(p)]
    if p.size < 2:
        return 1e-4
    q75, q25 = np.percentile(p, [75, 25])
    iqr = max(1e-12, q75 - q25)
    n = p.size
    bw = 2.0 * iqr * (n ** (-1.0 / 3.0))
    return float(max(1e-9, bw))


def _build_edges(pmin: float, pmax: float, width: float) -> np.ndarray:
    width = float(max(1e-12, width))
    if not (math.isfinite(pmin) and math.isfinite(pmax)) or pmax <= pmin:
        return np.array([0.0, 1.0], dtype=float)
    n_bins = int(max(1, math.ceil((pmax - pmin) / width)))
    edges = pmin + np.arange(n_bins + 1, dtype=float) * width
    # assure l’inclusion de pmax
    if edges[-1] < pmax:
        edges = np.append(edges, edges[-1] + width)
    return edges


def _expand_va(
    vol_hist: np.ndarray, edges: np.ndarray, coverage: float = 0.70
) -> Tuple[float, float, int]:
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
    o: np.ndarray,
    h: np.ndarray,
    l: np.ndarray,
    c: np.ndarray,
    vol: np.ndarray,
    edges: np.ndarray,
    *,
    body_gain: float = 0.6,
    eps: float = 1e-12,
) -> np.ndarray:
    """
    Volume Profile par chevauchement OHLC (wick bas / corps / wick haut), avec
    sur-pondération du corps et conservation exacte du volume.

    Implémentation vectorisée sans matrice N×M :
      - découpe chaque barre en 3 segments [lo, bl], [bl, bh], [bh, hi]
      - intègre l'overlap segment↔bin via sweep-line + np.add.at
      - mémoire O(M) (M = nb de bins), pas de broadcasting 2D

    Paramètres
    ----------
    o,h,l,c : arrays
        Séries OHLC (float).
    vol : array
        Volume par barre (float, >= 0).
    edges : array
        Bornes des bins strictement croissantes (taille M+1).
    body_gain : float
        Poids supplémentaire appliqué au corps (densité × (1 + body_gain)).
    eps : float
        Petite constante de stabilité numérique (évite division par 0).

    Retour
    ------
    profile : np.ndarray (M,)
        Volume par bin.
    """

    # --- Garde-fous edges ---
    if edges is None:
        return np.zeros(0, dtype=float)
    edges = np.asarray(edges, dtype=float)
    # nettoie et ordonne
    edges = np.unique(edges[np.isfinite(edges)])
    if edges.size < 2:
        return np.zeros(0, dtype=float)
    # après unique(), diff > 0 garanti si >=2
    if not np.all(np.diff(edges) > 0):
        # sécurité supplémentaire (très rare après unique) :
        order = np.argsort(edges)
        edges = edges[order]
        edges = np.unique(edges)
        if edges.size < 2 or not np.all(np.diff(edges) > 0):
            return np.zeros(0, dtype=float)

    nbins = edges.size - 1

    # --- utilitaire local : tolérant si _to_num n'existe pas ---
    def _as_num(arr, default):
        try:
            return _to_num(arr, default)
        except NameError:
            a = np.asarray(arr, dtype=float)
            if default is not np.nan:
                a = np.nan_to_num(a, nan=default, posinf=default, neginf=default)
            return a

    # --- Données nettoyées ---
    o = _as_num(o, np.nan)
    h = _as_num(h, np.nan)
    l = _as_num(l, np.nan)
    c = _as_num(c, np.nan)
    vol = _as_num(vol, 0.0)

    ok = np.isfinite(o) & np.isfinite(h) & np.isfinite(l) & np.isfinite(c) & (vol > 0.0)
    if not np.any(ok):
        return np.zeros(nbins, dtype=float)

    o, h, l, c, vol = o[ok], h[ok], l[ok], c[ok], vol[ok]

    # bornes réelles de la barre
    lo = np.minimum(l, h)
    hi = np.maximum(l, h)

    # corps (bornes clampées dans [lo, hi])
    bl = np.clip(np.minimum(o, c), lo, hi)
    bh = np.clip(np.maximum(o, c), lo, hi)

    # longueurs des segments
    len_wl = np.maximum(0.0, bl - lo)
    len_b = np.maximum(0.0, bh - bl)
    len_wh = np.maximum(0.0, hi - bh)

    # pondérations
    f_w = 1.0
    f_b = 1.0 + max(0.0, float(body_gain))

    # masse totale par barre (assure conservation du volume)
    mass = f_w * (len_wl + len_wh) + f_b * len_b
    mass = np.where(mass > 0.0, mass, 1.0)

    # "masse segment" (convertie en densité unitaire plus bas)
    dens_w = vol * (f_w / mass)
    dens_b = vol * (f_b / mass)

    profile = np.zeros(nbins, dtype=float)

    # --- accumulateur par segments (sweep-line) ---
    def _accum_segments(
        seg_lo: np.ndarray, seg_hi: np.ndarray, dens: np.ndarray
    ) -> np.ndarray:
        """
        Version optimisée - remplace l'ancienne implémentation
        """
        out = np.zeros(nbins, dtype=float)
        diff = np.zeros(nbins + 1, dtype=float)

        # Filtrage initial
        valid_mask = (seg_hi > edges[0]) & (seg_lo < edges[-1]) & (dens > 1e-12)
        if not np.any(valid_mask):
            return out

        # Extraction directe
        a = seg_lo[valid_mask]
        b = seg_hi[valid_mask]
        d = dens[valid_mask]

        # Clamping et calcul de largeurs
        a_clipped = np.maximum(a, edges[0])
        b_clipped = np.minimum(b, edges[-1])
        widths = np.maximum(b_clipped - a_clipped, 1e-12)

        dpu = d / widths

        # Calcul des indices
        i0 = np.clip(np.searchsorted(edges, a_clipped, side="right") - 1, 0, nbins - 1)
        i1 = np.clip(np.searchsorted(edges, b_clipped, side="left") - 1, 0, nbins - 1)

        # Traitement par lots
        batch_size = min(10000, len(a))
        n_batches = (len(a) + batch_size - 1) // batch_size

        for batch_idx in range(n_batches):
            start_idx = batch_idx * batch_size
            end_idx = min((batch_idx + 1) * batch_size, len(a))

            batch_i0 = i0[start_idx:end_idx]
            batch_i1 = i1[start_idx:end_idx]
            batch_a = a_clipped[start_idx:end_idx]
            batch_b = b_clipped[start_idx:end_idx]
            batch_dpu = dpu[start_idx:end_idx]

            # Mono-bin vs Multi-bin
            same_bin = batch_i0 == batch_i1
            multi_bin = ~same_bin

            # Traitement mono-bin avec bincount
            if np.any(same_bin):
                mono_indices = batch_i0[same_bin]
                mono_lengths = batch_b[same_bin] - batch_a[same_bin]
                mono_contrib = batch_dpu[same_bin] * mono_lengths

                unique_indices, inverse = np.unique(mono_indices, return_inverse=True)
                sums = np.bincount(inverse, weights=mono_contrib)
                np.add.at(out, unique_indices, sums)

            # Traitement multi-bin
            if np.any(multi_bin):
                i0m, i1m = batch_i0[multi_bin], batch_i1[multi_bin]
                am, bm = batch_a[multi_bin], batch_b[multi_bin]
                dpum = batch_dpu[multi_bin]

                # Bords gauche
                left_edges = edges[i0m + 1]
                left_lengths = np.maximum(0.0, left_edges - am)
                left_contrib = dpum * left_lengths
                np.add.at(out, i0m, left_contrib)

                # Bords droit
                right_edges = edges[i1m]
                right_lengths = np.maximum(0.0, bm - right_edges)
                right_contrib = dpum * right_lengths
                np.add.at(out, i1m, right_contrib)

                # Bins intérieurs (différentiel)
                has_interior = (i0m + 1) < i1m
                if np.any(has_interior):
                    start_indices = i0m[has_interior] + 1
                    end_indices = i1m[has_interior]
                    dpu_values = dpum[has_interior]

                    for start, end, val in zip(start_indices, end_indices, dpu_values):
                        if start < end:
                            diff[start] += val
                            diff[end] -= val

        # Application finale du différentiel
        active = np.cumsum(diff[:-1])
        bin_widths = edges[1:] - edges[:-1]
        out += active * bin_widths

        return out


def _expand_va(profile: np.ndarray, edges: np.ndarray, *, coverage: float = 0.70):
    if profile is None or profile.size == 0:
        return (None, None, None)
    total = float(profile.sum())
    if total <= 0:
        return (None, None, None)
    i_poc = int(np.argmax(profile))
    cum = float(profile[i_poc])
    l = i_poc - 1
    r = i_poc + 1
    while cum < coverage * total and (l >= 0 or r < len(profile)):
        left = profile[l] if l >= 0 else -1.0
        right = profile[r] if r < len(profile) else -1.0
        if right >= left:
            if r < len(profile):
                cum += max(0.0, profile[r])
                r += 1
        else:
            if l >= 0:
                cum += max(0.0, profile[l])
                l -= 1
    va_low = edges[max(l + 1, 0)]
    va_high = edges[min(r, len(edges) - 1)]
    return (float(va_low), float(va_high), i_poc)


def _local_extrema(y: np.ndarray):
    y = np.asarray(y, dtype=float)
    n = y.size
    if n < 3:
        return [], []
    peaks = np.where((y[1:-1] > y[:-2]) & (y[1:-1] >= y[2:]))[0] + 1
    valleys = np.where((y[1:-1] < y[:-2]) & (y[1:-1] <= y[2:]))[0] + 1
    return list(peaks), list(valleys)


def _suppress_close(indices: List[int], min_sep: int) -> List[int]:
    selected: List[int] = []
    for idx in indices:
        if all(abs(idx - j) >= min_sep for j in selected):
            selected.append(idx)
    return selected


# ============================
# API publique
# ============================


def calculate_volume_profile(
    df: pd.DataFrame,
    *,
    price_bins: Optional[int] = None,  # si fourni → force nb de bins
    bin_width: Optional[float] = None,  # sinon width déterminée par FD + tick_size
    tick_size: Optional[float] = None,  # si None → inféré
    use_ohlc_overlap: bool = True,  # True = profil institutionnel par overlap OHLC
    body_gain: float = 0.6,  # poids relatif du corps (0..1)
    coverage: float = 0.70,  # % de VA
    peak_std: float = 1.0,  # seuil de proéminence pour HVN (en σ)
    min_separation_ticks: int = 5,  # séparation min HVN/LVN en ticks
    max_bins: int = 400,  # garde-fou perf/mémoire
    ib_bars: int = 30,  # Initial Balance (premières N barres)
    return_nodes: bool = False,  # renvoyer les “nodes” (liste détaillée)
) -> Dict[str, Any]:
    """
    Volume Profile institutionnel (avec LRU cache durci):
      - Distribution par chevauchement OHLC (corps surpondéré si use_ohlc_overlap)
      - VPOC, VA (coverage), HVN/LVN, modalité, shape metrics (skew/kurt/entropy)
      - Initial Balance (ib_bars premières barres)
    """

    # =======================
    #  ❌ CACHE DÉSACTIVÉ (26 DEC 2025 - Rapport Institutionnel)
    # =======================
    # Raison : En scalping M1, 90% des données sont nouvelles → cache hit rate < 10%
    # Recommandation : Désactiver _VP_CACHE complexe (BLAKE2b fingerprint inutile)
    # Garder seulement _VOL_METRICS_CACHE basique dans volume_analyzer.py
    #
    # cache: "OrderedDict[tuple, Dict[str, Any]]" = globals().setdefault(
    #     "_VP_CACHE", OrderedDict()
    # )
    # MAX_CACHE = 128

    def _cache_get(key: tuple):
        # ❌ DÉSACTIVÉ : Retourne toujours None (pas de cache)
        return None

    def _cache_put(key: tuple, value: Dict[str, Any]):
        # ❌ DÉSACTIVÉ : Ne stocke rien
        pass

    # def _fingerprint_df(d: pd.DataFrame, sample_rows: int = 2048) -> str:
    #     """Empreinte légère sur colonnes clés (OHLC + volumes), tronquée aux N dernières lignes."""
    #     cols_bytes = []
    #     for col in (
    #         "open",
    #         "high",
    #         "low",
    #         "close",
    #         "ask_volume",
    #         "bid_volume",
    #         "real_volume",
    #         "tick_volume",
    #     ):
    #         if col in d.columns:
    #             a = (
    #                 pd.to_numeric(d[col], errors="coerce")
    #                 .fillna(0.0)
    #                 .to_numpy(dtype=np.float64)
    #             )
    #             if a.size > sample_rows:
    #                 a = a[-sample_rows:]
    #             cols_bytes.append(a.tobytes())
    #     h = hashlib.blake2b(digest_size=16)
    #     for b in cols_bytes:
    #         h.update(b)
    #     h.update(str(len(d)).encode())
    #     return h.hexdigest()

    # ❌ FINGERPRINT DÉSACTIVÉ : Plus besoin sans cache
    def _fingerprint_df(d: pd.DataFrame, sample_rows: int = 2048) -> str:
        """Stub - Fingerprint désactivé (cache _VP_CACHE supprimé)"""
        return "no_cache"

    # =======================
    #  Clé de cache
    # =======================
    if not isinstance(df, pd.DataFrame) or df is None or len(df) == 0:
        # réponse minimale cohérente (et cachable)
        fp = "empty"
        key = (
            fp,
            int(price_bins or 0),
            float(bin_width or 0.0),
            float(tick_size or 0.0),
            bool(use_ohlc_overlap),
            round(float(body_gain), 4),
            round(float(coverage), 4),
            round(float(peak_std), 4),
            int(min_separation_ticks),
            int(max_bins),
            int(ib_bars),
            bool(return_nodes),
        )
        cached = _cache_get(key)
        if cached is not None:
            return dict(cached)
        res = {
            "vpoc_price": None,
            "va_low": None,
            "va_high": None,
            "va_coverage": float(coverage),
        }
        _cache_put(key, res)
        return dict(res)

    fp = _fingerprint_df(df)
    key = (
        fp,
        int(price_bins or 0),
        float(bin_width or 0.0),
        float(tick_size or 0.0),
        bool(use_ohlc_overlap),
        round(float(body_gain), 4),
        round(float(coverage), 4),
        round(float(peak_std), 4),
        int(min_separation_ticks),
        int(max_bins),
        int(ib_bars),
        bool(return_nodes),
    )
    cached = _cache_get(key)
    if cached is not None:
        return dict(cached)

    # =======================
    #  Préparation des séries
    # =======================
    # NB: _to_num, _infer_tick_size, _freedman_diaconis_bin_width, _build_edges,
    #     _accumulate_profile_ohlc_overlap, _expand_va, _local_extrema, _suppress_close
    #     sont supposés présents dans le module (comme avant).
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

    # masque validité prix (close)
    mask = np.isfinite(c)
    if not np.any(mask):
        res = {
            "vpoc_price": None,
            "va_low": None,
            "va_high": None,
            "va_coverage": float(coverage),
        }
        _cache_put(key, res)
        return dict(res)

    # Sélections valides
    prices = c[mask]
    vols_for_hist = (
        vol_src[mask]
        if (isinstance(vol_src, np.ndarray) and vol_src.shape == c.shape)
        else None
    )

    # bornes de prix robustes
    pmin_raw = np.minimum.reduce([o, h, l, c])
    pmax_raw = np.maximum.reduce([o, h, l, c])
    pmin = (
        float(np.nanmin(pmin_raw))
        if np.isfinite(pmin_raw).any()
        else float(np.nanmin(prices))
    )
    pmax = (
        float(np.nanmax(pmax_raw))
        if np.isfinite(pmax_raw).any()
        else float(np.nanmax(prices))
    )
    if (not np.isfinite(pmin)) or (not np.isfinite(pmax)) or pmax <= pmin:
        pmin, pmax = float(np.nanmin(prices)), float(np.nanmax(prices))
    if (not np.isfinite(pmin)) or (not np.isfinite(pmax)) or pmax <= pmin:
        # fallback ultime: histogramme du close non-pondéré
        hist, edges = np.histogram(prices, bins=int(price_bins or 20))
        i = int(np.argmax(hist)) if hist.size else -1
        vpoc_price = float((edges[i] + edges[i + 1]) * 0.5) if i >= 0 else None
        va_low, va_high, _ = (
            _expand_va(
                hist.astype(float) if hist.size else np.array([0.0]),
                edges,
                coverage=coverage,
            )
            if hist.size
            else (None, None, -1)
        )
        res = {
            "vpoc_price": vpoc_price,
            "va_low": va_low,
            "va_high": va_high,
            "va_coverage": float(coverage),
            "bins_count": int(hist.size),
            "bin_width": float(edges[1] - edges[0]) if edges.size > 1 else None,
            "price_min": float(edges[0]) if edges.size > 0 else None,
            "price_max": float(edges[-1]) if edges.size > 0 else None,
            "hvn": [],
            "lvn": [],
            "modality": "unknown",
            "balance_metrics": {},
            "ib": {},
        }
        _cache_put(key, res)
        return dict(res)

    # =======================
    #  Construction des bins
    # =======================
    ts = (
        float(tick_size)
        if (tick_size is not None and tick_size > 0)
        else _infer_tick_size(prices)
    )
    # largeur
    if bin_width and bin_width > 0:
        width = float(bin_width)
    elif price_bins and price_bins > 0:
        width = max(ts, (pmax - pmin) / float(price_bins))
    else:
        fd = max(1e-12, _freedman_diaconis_bin_width(prices))  # garde-fou
        width = max(ts, fd)

    # edges robustes
    edges = _build_edges(pmin, pmax, max(width, ts))
    if edges is None or not isinstance(edges, np.ndarray) or edges.size < 2:
        # fallback minimal 20 bins
        edges = np.linspace(
            pmin, pmax, num=int(min(max_bins, max(2, price_bins or 20))) + 1
        )

    # cap max bins
    if edges.size - 1 > max_bins:
        k = int(math.ceil((edges.size - 1) / max_bins))
        width = max(width * k, ts)
        edges = _build_edges(pmin, pmax, width)
        if edges.size - 1 > max_bins:
            # ultime fallback linéaire
            edges = np.linspace(pmin, pmax, num=max_bins + 1)

    centers = (edges[:-1] + edges[1:]) * 0.5
    nbins = edges.size - 1

    # =======================
    #  Accumulation du profil
    # =======================
    vol_hist = None
    if use_ohlc_overlap:
        try:
            vol_hist = _accumulate_profile_ohlc_overlap(
                o, h, l, c, vol_src, edges, body_gain=body_gain
            )
        except Exception:
            vol_hist = None

    if vol_hist is None:
        # fallback: histogramme sur close (pondéré si possible)
        try:
            if vols_for_hist is not None and vols_for_hist.shape[0] == prices.shape[0]:
                vol_hist, _ = np.histogram(prices, bins=edges, weights=vols_for_hist)
            else:
                vol_hist, _ = np.histogram(prices, bins=edges)
        except Exception:
            # dernier filet: zeros
            vol_hist = np.zeros(nbins, dtype=float)

    # **Garde-fou central**: empêcher "'NoneType'.astype"
    vol_hist = (
        np.asarray(vol_hist, dtype=float)
        if vol_hist is not None
        else np.zeros(nbins, dtype=float)
    )
    # alignement taille
    if vol_hist.shape[0] != nbins:
        # pad/troncature pour correspondre aux bins
        if vol_hist.shape[0] < nbins:
            vol_hist = np.pad(vol_hist, (0, nbins - vol_hist.shape[0]), mode="constant")
        else:
            vol_hist = vol_hist[:nbins]

    vol_hist = np.nan_to_num(vol_hist, nan=0.0, posinf=0.0, neginf=0.0)

    # si tout zéro → impossible d'extraire VA/VPOC de façon fiable
    if float(vol_hist.sum()) <= 0.0:
        # on renvoie un profil “vide” mais cohérent
        res = {
            "vpoc_price": None,
            "va_low": None,
            "va_high": None,
            "va_coverage": float(coverage),
            "bins_count": int(nbins),
            "bin_width": float(edges[1] - edges[0]) if edges.size > 1 else None,
            "price_min": float(pmin),
            "price_max": float(pmax),
            "hvn": [],
            "lvn": [],
            "modality": "unknown",
            "balance_metrics": {},
            "ib": {},
        }
        _cache_put(key, res)
        return dict(res)

    # =======================
    #  VPOC + VA
    # =======================
    va_low, va_high, i_poc = _expand_va(vol_hist, edges, coverage=coverage)
    if (i_poc is None) or (i_poc < 0) or (i_poc >= nbins):
        res = {
            "vpoc_price": None,
            "va_low": None,
            "va_high": None,
            "va_coverage": float(coverage),
        }
        _cache_put(key, res)
        return dict(res)
    vpoc_price = float(centers[i_poc])

    # =======================
    #  HVN / LVN
    # =======================
    peaks, valleys = _local_extrema(vol_hist)
    mean = float(np.mean(vol_hist))
    std = float(np.std(vol_hist)) or 1e-9

    strong_peaks = [i for i in peaks if vol_hist[i] >= (mean + peak_std * std)]
    strong_peaks = _suppress_close(
        sorted(strong_peaks, key=lambda i: vol_hist[i], reverse=True),
        min_sep=max(1, min_separation_ticks),
    )[:3]

    lvn_candidates = [i for i in valleys if vol_hist[i] <= (mean - 0.25 * std)]
    lvn_candidates = _suppress_close(
        sorted(lvn_candidates), min_sep=max(1, min_separation_ticks)
    )[:3]

    hvn = [
        {"price": float(centers[i]), "volume": float(vol_hist[i])}
        for i in (strong_peaks or [])
    ]
    lvn = [
        {"price": float(centers[i]), "volume": float(vol_hist[i])}
        for i in (lvn_candidates or [])
    ]

    modality = (
        "uni"
        if len(strong_peaks) <= 1
        else ("bi" if len(strong_peaks) == 2 else "multi")
    )

    # =======================
    #  Shape metrics
    # =======================
    w_sum = float(vol_hist.sum())
    w = vol_hist / (w_sum if w_sum > 0 else 1.0)
    mu = float(np.sum(w * centers))
    var = float(np.sum(w * (centers - mu) ** 2))
    std_p = math.sqrt(max(var, 1e-12))
    skew = float(np.sum(w * ((centers - mu) / std_p) ** 3))
    kurt = float(np.sum(w * ((centers - mu) / std_p) ** 4)) - 3.0
    # entropie normalisée par log(nbins) (évite -0/0)
    entropy = float(-np.sum(w[w > 0] * np.log(w[w > 0])) / math.log(max(2, nbins)))

    va_width = (
        float(va_high - va_low)
        if (np.isfinite(va_high) and np.isfinite(va_low))
        else np.nan
    )
    full_width = float(pmax - pmin) if pmax > pmin else np.nan
    va_width_frac = (
        float(va_width / full_width)
        if (np.isfinite(va_width) and np.isfinite(full_width) and full_width > 0)
        else np.nan
    )

    left_tail = (
        float(np.sum(vol_hist[centers < va_low])) if np.isfinite(va_low) else 0.0
    )
    right_tail = (
        float(np.sum(vol_hist[centers > va_high])) if np.isfinite(va_high) else 0.0
    )
    tail_left_frac = float(left_tail / (w_sum if w_sum > 0 else 1.0))
    tail_right_frac = float(right_tail / (w_sum if w_sum > 0 else 1.0))

    # =======================
    #  Initial Balance
    # =======================
    ib_info: Dict[str, Any] = {}
    if ib_bars and ib_bars > 1 and len(df) >= 2:
        try:
            n = int(min(ib_bars, len(df)))
            hi_ib = float(np.nanmax(_to_num(df.get("high", np.nan).iloc[:n], np.nan)))
            lo_ib = float(np.nanmin(_to_num(df.get("low", np.nan).iloc[:n], np.nan)))
            ib_width = (
                float(hi_ib - lo_ib)
                if np.isfinite(hi_ib) and np.isfinite(lo_ib)
                else np.nan
            )
            vpoc_in_ib = (
                bool(lo_ib <= vpoc_price <= hi_ib)
                if (np.isfinite(ib_width) and np.isfinite(vpoc_price))
                else False
            )
            ib_info = {
                "bars": n,
                "high": hi_ib,
                "low": lo_ib,
                "width": ib_width,
                "vpoc_in_ib": vpoc_in_ib,
            }
        except Exception:
            ib_info = {}

    # =======================
    #  Sortie
    # =======================
    res: Dict[str, Any] = {
        "vpoc_price": vpoc_price,
        "va_low": va_low,
        "va_high": va_high,
        "va_coverage": float(coverage),
        "bins_count": int(nbins),
        "bin_width": float(edges[1] - edges[0]) if edges.size > 1 else None,
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
        nodes: list[dict[str, Any]] = []
        for i in strong_peaks or []:
            nodes.append(
                {
                    "type": "HVN",
                    "price": float(centers[i]),
                    "volume": float(vol_hist[i]),
                    "strength": float((vol_hist[i] - mean) / (std or 1e-9)),
                }
            )
        for i in lvn_candidates or []:
            nodes.append(
                {
                    "type": "LVN",
                    "price": float(centers[i]),
                    "volume": float(vol_hist[i]),
                    "strength": float((mean - vol_hist[i]) / (std or 1e-9)),
                }
            )
        nodes.append(
            {"type": "VPOC", "price": vpoc_price, "volume": float(vol_hist[i_poc])}
        )
        nodes.append(
            {
                "type": "VAL",
                "price": va_low,
                "volume": (
                    float(np.sum(vol_hist[centers <= va_low]))
                    if np.isfinite(va_low)
                    else 0.0
                ),
            }
        )
        nodes.append(
            {
                "type": "VAH",
                "price": va_high,
                "volume": (
                    float(np.sum(vol_hist[centers >= va_high]))
                    if np.isfinite(va_high)
                    else 0.0
                ),
            }
        )
        res["nodes"] = nodes

    _cache_put(key, res)
    return dict(res)
