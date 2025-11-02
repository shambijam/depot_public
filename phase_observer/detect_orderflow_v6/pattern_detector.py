from __future__ import annotations
from typing import Dict, Any, List
import pandas as pd
import numpy as np


def detect_patterns(
    df: pd.DataFrame, imbalance_threshold: float = 0.7
) -> List[Dict[str, Any]]:
    """
    Détection de patterns (compat V5) – version vectorisée.
    Patterns:
      - buy_imbalance / sell_imbalance
      - aggressive_buying / aggressive_selling  (via aggressor_ratio)
      - buy_absorption / sell_absorption        (ask ≫ bid ∧ (delta<0 ou close<open), symétrique)
      - climax                                  (volume total >= quantile 97%)
      - iceberg_order                           (si colonnes executions présentes)

    Paramètre:
      imbalance_threshold:
        - Si >= 0.5 → seuil direct sur imbalance_row (dans [0..1]) (ex: 0.7 ~ 70/30)
        - Si <  0.5 → seuil centré sur [-1..1] (ex: 0.2 ≈ 70/30)

    Retour: liste d'événements {index, timestamp, pattern, price_level, imbalance, delta,
                                dominance, bid_volume, ask_volume, row_total, (extras)}.
    """
    events: List[Dict[str, Any]] = []
    if df is None or len(df) == 0:
        return events

    # ---------- Helpers ----------
    def _num(s, default=0.0, dtype=float):
        # Conversion robuste -> numpy (évite copies inutiles plus tard)
        return pd.to_numeric(s, errors="coerce").fillna(default).to_numpy(dtype=dtype)

    n = len(df)

    # ---------- Séries de base (numpy) ----------
    close = _num(df.get("close", np.nan), default=np.nan)
    open_ = _num(df.get("open", np.nan), default=np.nan)
    ask = _num(df.get("ask_volume", 0.0), default=0.0)
    bid = _num(df.get("bid_volume", 0.0), default=0.0)

    have_ab = (ask.sum() + bid.sum()) > 0.0

    if have_ab:
        total = ask + bid
        with np.errstate(divide="ignore", invalid="ignore"):
            imbalance_row = np.where(total > 0.0, ask / total, 0.5)
        delta = _num(df.get("delta", ask - bid), default=0.0)
    else:
        total = _num(df.get("tick_volume", 0.0), default=0.0)
        imbalance_row = np.full(n, 0.5, dtype=float)
        if "delta" in df.columns:
            delta = _num(df["delta"], default=0.0)
        else:
            prev = np.roll(close, 1)
            prev[0] = close[0]
            sign = np.where(close >= prev, 1.0, -1.0)
            delta = sign * total

    agr = _num(df.get("aggressor_ratio", 0.5), default=0.5)

    # Timestamps (chaînes prêtes)
    if "time" in df.columns:
        ts = (
            pd.to_datetime(df["time"], errors="coerce", utc=True)
            .astype("datetime64[ns]")
            .astype(str)
            .to_numpy()
        )
    elif "timestamp" in df.columns:
        ts = df["timestamp"].astype(str).to_numpy()
    else:
        ts = np.arange(n).astype(str)

    # Price level optionnel
    if "price_level" in df.columns:
        pl_raw = pd.to_numeric(df["price_level"], errors="coerce").to_numpy(dtype=float)
        price_level = np.where(np.isfinite(pl_raw), pl_raw, np.nan)
    else:
        price_level = np.full(n, np.nan, dtype=float)

    # Dominance vectorisée
    dominance = np.where(delta > 0, "buyers", np.where(delta < 0, "sellers", "neutral"))

    # ---------- Masques vectorisés ----------
    # Imbalance
    if imbalance_threshold >= 0.5:
        thr = float(imbalance_threshold)
        buy_imb_mask = imbalance_row >= thr
        sell_imb_mask = imbalance_row <= (1.0 - thr)
    else:
        thr_c = float(imbalance_threshold)
        centered = 2.0 * imbalance_row - 1.0
        buy_imb_mask = centered >= thr_c
        sell_imb_mask = centered <= -thr_c

    # Aggressivité
    ag_buy_mask = agr >= 0.75
    ag_sell_mask = agr <= 0.25

    # Absorption (si ask/bid dispo)
    if have_ab:
        close_lt_open = np.where(
            np.isfinite(close) & np.isfinite(open_), close < open_, False
        )
        close_gt_open = np.where(
            np.isfinite(close) & np.isfinite(open_), close > open_, False
        )
        buy_abs_mask = (ask > 2.0 * (bid + 1e-9)) & ((delta < 0.0) | close_lt_open)
        sell_abs_mask = (bid > 2.0 * (ask + 1e-9)) & ((delta > 0.0) | close_gt_open)
    else:
        buy_abs_mask = np.zeros(n, dtype=bool)
        sell_abs_mask = np.zeros(n, dtype=bool)

    # Climax (Q97)
    if np.any(total > 0.0):
        try:
            q97 = float(np.quantile(total, 0.97))
        except Exception:
            q97 = 0.0
        climax_mask = (q97 > 0.0) & (total >= q97)
    else:
        climax_mask = np.zeros(n, dtype=bool)

    # Iceberg (si colonnes présentes)
    if ("executions_count" in df.columns) and ("avg_exec_size" in df.columns):
        execs = _num(df["executions_count"], default=0.0)
        avgsz = _num(df["avg_exec_size"], default=0.0)
        iceberg_mask = (execs > 50.0) & (avgsz < 0.2 * np.maximum(total, 1.0))
    else:
        execs = avgsz = None
        iceberg_mask = np.zeros(n, dtype=bool)

    # ---------- Émissions groupées (minimise aller-retour Python) ----------
    def _pack(
        indices: np.ndarray, label: str, add_fp=False, add_ice=False
    ) -> List[Dict[str, Any]]:
        if indices.size == 0:
            return []
        i = indices  # alias
        # Slices numpy prêts
        pl = price_level[i]
        imbr = imbalance_row[i]
        delt = delta[i]
        dom = dominance[i]
        bids = bid[i] if bid.size else np.zeros_like(i, dtype=float)
        asks = ask[i] if ask.size else np.zeros_like(i, dtype=float)
        tot = total[i]
        tstamp = ts[i]
        out = []
        if add_ice and (execs is not None) and (avgsz is not None):
            # Prépare extras iceberg en slices
            ex = execs[i]
            av = avgsz[i]
            out.extend(
                {
                    "index": int(ii),
                    "timestamp": str(tt),
                    "pattern": label,
                    "price_level": (float(p) if np.isfinite(p) else None),
                    "imbalance": float(imb),
                    "delta": float(de),
                    "dominance": str(dm),
                    "bid_volume": float(bv),
                    "ask_volume": float(avol),
                    "row_total": float(tv),
                    "iceberg": {"exec_count": int(ec), "avg_size": float(asz)},
                }
                for ii, tt, p, imb, de, dm, bv, avol, tv, ec, asz in zip(
                    i, tstamp, pl, imbr, delt, dom, bids, asks, tot, ex, av
                )
            )
            return out

        if add_fp:
            out.extend(
                {
                    "index": int(ii),
                    "timestamp": str(tt),
                    "pattern": label,
                    "price_level": (float(p) if np.isfinite(p) else None),
                    "imbalance": float(imb),
                    "delta": float(de),
                    "dominance": str(dm),
                    "bid_volume": float(bv),
                    "ask_volume": float(avol),
                    "row_total": float(tv),
                    "footprint": f"buy_ratio={agr_val:.2f}",
                }
                for ii, tt, p, imb, de, dm, bv, avol, tv, agr_val in zip(
                    i, tstamp, pl, imbr, delt, dom, bids, asks, tot, agr[i]
                )
            )
            return out

        # cas générique
        out.extend(
            {
                "index": int(ii),
                "timestamp": str(tt),
                "pattern": label,
                "price_level": (float(p) if np.isfinite(p) else None),
                "imbalance": float(imb),
                "delta": float(de),
                "dominance": str(dm),
                "bid_volume": float(bv),
                "ask_volume": float(avol),
                "row_total": float(tv),
            }
            for ii, tt, p, imb, de, dm, bv, avol, tv in zip(
                i, tstamp, pl, imbr, delt, dom, bids, asks, tot
            )
        )
        return out

    # Calcul des index une seule fois
    nz_buy_imb = np.flatnonzero(buy_imb_mask)
    nz_sell_imb = np.flatnonzero(sell_imb_mask)
    nz_ag_buy = np.flatnonzero(ag_buy_mask)
    nz_ag_sell = np.flatnonzero(ag_sell_mask)
    nz_buy_abs = np.flatnonzero(buy_abs_mask)
    nz_sell_abs = np.flatnonzero(sell_abs_mask)
    nz_climax = np.flatnonzero(climax_mask)
    nz_iceberg = np.flatnonzero(iceberg_mask)

    # Émissions (extension en blocs)
    events.extend(_pack(nz_buy_imb, "buy_imbalance"))
    events.extend(_pack(nz_sell_imb, "sell_imbalance"))
    events.extend(_pack(nz_ag_buy, "aggressive_buying", add_fp=True))
    events.extend(_pack(nz_ag_sell, "aggressive_selling", add_fp=True))
    events.extend(_pack(nz_buy_abs, "buy_absorption"))
    events.extend(_pack(nz_sell_abs, "sell_absorption"))
    events.extend(_pack(nz_climax, "climax"))
    events.extend(_pack(nz_iceberg, "iceberg_order", add_ice=True))

    return events
