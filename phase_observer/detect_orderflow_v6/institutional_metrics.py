# phase_observer/detect_orderflow_v6/institutional_metrics.py
from __future__ import annotations
from typing import Dict, Any, Tuple, List, Optional
import numpy as np
import pandas as pd
import math


def _accumulate_profile_ohlc_overlap(
    o: np.ndarray,
    h: np.ndarray,
    l: np.ndarray,
    c: np.ndarray,
    vol: np.ndarray,
    edges: np.ndarray,
    *,
    body_gain: float = 0.0,  # ignoré ici (uniforme), conservé pour compat
    eps: float = 1e-12,
) -> np.ndarray:
    """
    Accumulation vectorisée du Volume Profile avec distribution UNIFORME par barre.
    - Compat signature (o,h,l,c,vol,edges[, body_gain, eps]) attendue par le reste du code.
    - Ignore body/wiсks: volume réparti uniformément sur [low, high] de la barre.
    - Chunking automatique pour limiter la mémoire quand N×B est grand.

    Retour: profil (len(edges)-1,)
    """
    # --- Préparation & garde-fous ---
    o = np.asarray(o, dtype=float)
    h = np.asarray(h, dtype=float)
    l = np.asarray(l, dtype=float)
    c = np.asarray(c, dtype=float)
    vol = np.asarray(vol, dtype=float)
    edges = np.asarray(edges, dtype=float)

    # borne low/high par barre (on se fiche de o/c ici: uniforme sur le range)
    lo = np.minimum(l, h)
    hi = np.maximum(l, h)

    ok = np.isfinite(lo) & np.isfinite(hi) & np.isfinite(vol) & (vol > 0.0) & (hi > lo)
    if not np.any(ok):
        return np.zeros(len(edges) - 1, dtype=float)

    lo = lo[ok]
    hi = hi[ok]
    vol = vol[ok]

    B = len(edges) - 1
    if B <= 0:
        return np.zeros(0, dtype=float)

    bin_lo = edges[:-1][None, :]  # (1, B)
    bin_hi = edges[1:][None, :]  # (1, B)

    profile = np.zeros(B, dtype=float)

    # --- Chunking pour éviter N×B massif en RAM ---
    # cible ~5e6 cellules par bloc (≈ 40 Mo en float64 avec matrices intermédiaires)
    target_cells = 5_000_000
    N = lo.shape[0]
    rows_per_chunk = max(1, int(target_cells // max(1, B)))

    for start in range(0, N, rows_per_chunk):
        end = min(N, start + rows_per_chunk)

        lo_blk = lo[start:end][:, None]  # (n,1)
        hi_blk = hi[start:end][:, None]  # (n,1)

        left = np.maximum(lo_blk, bin_lo)  # (n,B)
        right = np.minimum(hi_blk, bin_hi)  # (n,B)
        overlap = np.maximum(0.0, right - left)

        width = np.maximum(hi_blk - lo_blk, eps)  # (n,1)
        density = vol[start:end][:, None] / width  # (n,1)

        profile += np.sum(overlap * density, axis=0)  # (B,)

    return profile


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
    return float(2 * iqr * (n ** (-1 / 3)))


def _build_edges(
    pmin: float,
    pmax: float,
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
    Distribution du volume sur 3 segments par barre (wick bas, corps, wick haut),
    pondérée (corps sur-pondéré) et conservant le volume exact.
    Implémentation O(N+M) mémoire-sûre (sans broadcast 2D), via sweep-line + add.at.
    """

    # -- utilitaires --
    def _accum_segments(
        seg_lo: np.ndarray, seg_hi: np.ndarray, dens: np.ndarray
    ) -> np.ndarray:
        """
        Accumule ∑ D * overlap([seg_lo, seg_hi], bins) sans matrices nb_bars×nb_bins.
        dens = "masse du segment" (pas densité) → dens_per_unit = dens / width.
        """
        nbins = edges.size - 1
        if nbins <= 0:
            return np.zeros(0, dtype=float)

        prof = np.zeros(nbins, dtype=float)
        diff = np.zeros(nbins + 1, dtype=float)  # pour les pleins (inter-bins)

        # clamp & filtrage des segments utiles
        a = np.asarray(seg_lo, dtype=float)
        b = np.asarray(seg_hi, dtype=float)
        w = np.maximum(b - a, eps)
        d = np.asarray(dens, dtype=float)

        # hors plage → aucun recouvrement
        in_range = (b > edges[0]) & (a < edges[-1]) & (d > 0.0) & (w > 0.0)
        if not np.any(in_range):
            return prof

        a = np.maximum(a[in_range], edges[0])
        b = np.minimum(b[in_range], edges[-1])
        w = np.maximum(w[in_range], eps)
        d = d[in_range]
        dpu = d / w  # densité par unité de prix (constante sur le segment)

        # indices des bins
        i0 = np.searchsorted(edges, a, side="right") - 1
        i1 = np.searchsorted(edges, b, side="left") - 1
        i0 = np.clip(i0, 0, nbins - 1)
        i1 = np.clip(i1, 0, nbins - 1)

        # cas mono-bin
        same = i0 == i1
        if np.any(same):
            idx = i0[same]
            val = dpu[same] * (b[same] - a[same])  # intégrale exacte
            np.add.at(prof, idx, val)

        # cas multi-bins
        multi = ~same
        if np.any(multi):
            i0m = i0[multi]
            i1m = i1[multi]
            am = a[multi]
            bm = b[multi]
            dpum = dpu[multi]

            # contributions partielles aux bords
            left_len = edges[i0m + 1] - am
            right_len = bm - edges[i1m]
            np.add.at(prof, i0m, dpum * np.maximum(0.0, left_len))
            np.add.at(prof, i1m, dpum * np.maximum(0.0, right_len))

            # contributions pleines (bins intérieurs) via diff
            start = i0m + 1
            stop = i1m
            has_range = start < stop
            if np.any(has_range):
                s = start[has_range]
                t = stop[has_range]
                wv = dpum[has_range]
                np.add.at(diff, s, wv)
                np.add.at(diff, t, -wv)

        # finalise pleins: densité active × largeur du bin
        active_dens = np.cumsum(diff[:-1])
        prof += active_dens * (edges[1:] - edges[:-1])
        return prof

    # -- données propres --
    o = _to_num(o, np.nan)
    h = _to_num(h, np.nan)
    l = _to_num(l, np.nan)
    c = _to_num(c, np.nan)
    vol = _to_num(vol, 0.0)

    ok = np.isfinite(o) & np.isfinite(h) & np.isfinite(l) & np.isfinite(c) & (vol > 0.0)
    if not np.any(ok) or (edges is None) or (edges.size < 2):
        return (
            np.zeros(max(edges.size - 1, 0), dtype=float)
            if edges is not None
            else np.zeros(0, dtype=float)
        )

    o, h, l, c, vol = o[ok], h[ok], l[ok], c[ok], vol[ok]

    # bornes réelles de la barre (sécurité même si data sale)
    lo = np.minimum(l, h)
    hi = np.maximum(l, h)

    # corps
    bl = np.minimum(o, c)  # body low
    bh = np.maximum(o, c)  # body high
    bl = np.clip(bl, lo, hi)
    bh = np.clip(bh, lo, hi)

    # longueurs segments
    len_wl = np.maximum(0.0, bl - lo)
    len_b = np.maximum(0.0, bh - bl)
    len_wh = np.maximum(0.0, hi - bh)

    # pondérations (corps sur-pondéré)
    f_w = 1.0
    f_b = 1.0 + max(0.0, float(body_gain))

    # masse totale par barre (pour conserver exactement le volume)
    mass = f_w * (len_wl + len_wh) + f_b * len_b
    mass = np.where(mass > 0.0, mass, 1.0)

    # "masses de segments" (pas densités) : seront converties en densité unitaire dans _accum_segments
    dens_w = vol * (f_w / mass)
    dens_b = vol * (f_b / mass)

    profile = np.zeros(edges.size - 1, dtype=float)

    # wick bas
    m_wl = len_wl > 0.0
    if np.any(m_wl):
        profile += _accum_segments(lo[m_wl], bl[m_wl], dens_w[m_wl])

    # corps
    m_b = len_b > 0.0
    if np.any(m_b):
        profile += _accum_segments(bl[m_b], bh[m_b], dens_b[m_b])

    # wick haut
    m_wh = len_wh > 0.0
    if np.any(m_wh):
        profile += _accum_segments(bh[m_wh], hi[m_wh], dens_w[m_wh])

    return profile


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
    Volume Profile institutionnel (avec LRU cache):
      - Distribution par chevauchement OHLC (corps surpondéré si use_ohlc_overlap)
      - VPOC, VA (coverage), HVN/LVN, modalité, shape metrics (skew/kurt/entropy)
      - Initial Balance (ib_bars premières barres)
    """
    import math
    import hashlib
    from collections import OrderedDict
    import numpy as np
    import pandas as pd

    # ---- LRU cache module-global (créé à la volée) ----
    cache: "OrderedDict[tuple, Dict[str, Any]]" = globals().setdefault(
        "_VP_CACHE", OrderedDict()
    )
    MAX_CACHE = 128

    def _cache_get(key: tuple):
        v = cache.get(key)
        if v is not None:
            cache.move_to_end(key)
        return v

    def _cache_put(key: tuple, value: Dict[str, Any]):
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > MAX_CACHE:
            cache.popitem(last=False)

    def _fingerprint_df(d: pd.DataFrame, sample_rows: int = 2048) -> str:
        """Empreinte légère sur colonnes clés (OHLC + volumes), tronquée aux N dernières lignes."""
        cols_bytes = []
        for col in (
            "open",
            "high",
            "low",
            "close",
            "ask_volume",
            "bid_volume",
            "real_volume",
            "tick_volume",
        ):
            if col in d.columns:
                a = (
                    pd.to_numeric(d[col], errors="coerce")
                    .fillna(0.0)
                    .to_numpy(dtype=np.float64)
                )
                if a.size > sample_rows:
                    a = a[-sample_rows:]
                cols_bytes.append(a.tobytes())
        h = hashlib.blake2b(digest_size=16)
        for b in cols_bytes:
            h.update(b)
        h.update(str(len(d)).encode())
        return h.hexdigest()

    # ---- Clé de cache (données + paramètres) ----
    fp = _fingerprint_df(df) if (df is not None and len(df) > 0) else "empty"
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
        # copie superficielle (évite mutation du cache par l'appelant)
        return dict(cached)

    # =======================
    #  Calcul principal
    # =======================
    if df is None or len(df) == 0:
        res = {
            "vpoc_price": None,
            "va_low": None,
            "va_high": None,
            "va_coverage": coverage,
        }
        _cache_put(key, res)
        return dict(res)

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
        res = {
            "vpoc_price": None,
            "va_low": None,
            "va_high": None,
            "va_coverage": coverage,
        }
        _cache_put(key, res)
        return dict(res)

    prices = c[mask]
    pmin = float(np.nanmin(np.minimum.reduce([o, h, l, c])))
    pmax = float(np.nanmax(np.maximum.reduce([o, h, l, c])))
    if not (np.isfinite(pmin) and np.isfinite(pmax)) or pmax <= pmin:
        pmin, pmax = float(np.nanmin(prices)), float(np.nanmax(prices))
    if not (np.isfinite(pmin) and np.isfinite(pmax)) or pmax <= pmin:
        # fallback ultime: histogramme du close
        hist, edges = np.histogram(prices, bins=price_bins or 20, weights=vol_src[mask])
        i = int(np.argmax(hist))
        vpoc_price = float((edges[i] + edges[i + 1]) * 0.5)
        va_low, va_high, _ = _expand_va(hist, edges, coverage=coverage)
        res = {
            "vpoc_price": vpoc_price,
            "va_low": va_low,
            "va_high": va_high,
            "va_coverage": coverage,
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

    # ----------- Construction des bins -----------
    ts = (
        float(tick_size)
        if (tick_size is not None and tick_size > 0)
        else _infer_tick_size(prices)
    )
    if bin_width and bin_width > 0:
        width = float(bin_width)
    elif price_bins and price_bins > 0:
        width = (pmax - pmin) / float(price_bins)
    else:
        fd = _freedman_diaconis_bin_width(prices)
        width = max(ts, fd)
    edges = _build_edges(pmin, pmax, width)
    if edges.size > max_bins + 1:
        k = int(math.ceil((edges.size - 1) / max_bins))
        width *= k
        edges = _build_edges(pmin, pmax, width)

    centers = (edges[:-1] + edges[1:]) * 0.5
    nbins = edges.size - 1

    # ----------- Accumulation du profil -----------
    if use_ohlc_overlap:
        vol_hist = _accumulate_profile_ohlc_overlap(
            o, h, l, c, vol_src, edges, body_gain=body_gain
        )
    else:
        vol_hist, _ = np.histogram(prices, bins=edges, weights=vol_src[mask])

    vol_hist = np.nan_to_num(vol_hist, nan=0.0, posinf=0.0, neginf=0.0).astype(float)

    # ----------- VPOC + VA -----------
    va_low, va_high, i_poc = _expand_va(vol_hist, edges, coverage=coverage)
    if i_poc < 0:
        res = {
            "vpoc_price": None,
            "va_low": None,
            "va_high": None,
            "va_coverage": coverage,
        }
        _cache_put(key, res)
        return dict(res)

    vpoc_price = float(centers[i_poc])

    # ----------- HVN / LVN -----------
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
        {"price": float(centers[i]), "volume": float(vol_hist[i])} for i in strong_peaks
    ]
    lvn = [
        {"price": float(centers[i]), "volume": float(vol_hist[i])}
        for i in lvn_candidates
    ]

    modality = (
        "uni"
        if len(strong_peaks) <= 1
        else ("bi" if len(strong_peaks) == 2 else "multi")
    )

    # ----------- Shape metrics -----------
    w = vol_hist / max(1e-12, vol_hist.sum())
    mu = float(np.sum(w * centers))
    var = float(np.sum(w * (centers - mu) ** 2))
    std_p = math.sqrt(max(var, 1e-12))
    skew = float(np.sum(w * ((centers - mu) / std_p) ** 3))
    kurt = float(np.sum(w * ((centers - mu) / std_p) ** 4)) - 3.0
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

    left_tail = float(np.sum(vol_hist[centers < va_low]))
    right_tail = float(np.sum(vol_hist[centers > va_high]))
    tail_left_frac = float(left_tail / max(1e-12, vol_hist.sum()))
    tail_right_frac = float(right_tail / max(1e-12, vol_hist.sum()))

    # ----------- Initial Balance -----------
    ib_info: Dict[str, Any] = {}
    if ib_bars and ib_bars > 1 and len(df) >= 2:
        n = int(min(ib_bars, len(df)))
        hi_ib = float(np.nanmax(_to_num(df["high"].iloc[:n], np.nan)))
        lo_ib = float(np.nanmin(_to_num(df["low"].iloc[:n], np.nan)))
        ib_width = (
            float(hi_ib - lo_ib)
            if np.isfinite(hi_ib) and np.isfinite(lo_ib)
            else np.nan
        )
        vpoc_in_ib = (
            bool(lo_ib <= vpoc_price <= hi_ib) if np.isfinite(ib_width) else False
        )
        ib_info = {
            "bars": n,
            "high": hi_ib,
            "low": lo_ib,
            "width": ib_width,
            "vpoc_in_ib": vpoc_in_ib,
        }

    # ----------- Sortie -----------
    res: Dict[str, Any] = {
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
                "volume": float(np.sum(vol_hist[centers <= va_low])),
            }
        )
        nodes.append(
            {
                "type": "VAH",
                "price": va_high,
                "volume": float(np.sum(vol_hist[centers >= va_high])),
            }
        )
        res["nodes"] = nodes

    _cache_put(key, res)
    return dict(res)
