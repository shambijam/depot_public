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

    # --- Helpers numériques (évite copies inutiles) ---
    def _num(s, default=0.0, dtype=float):
        return pd.to_numeric(s, errors="coerce").fillna(default).to_numpy(dtype=dtype)

    n = len(df)

    # --- Colonnes de base ---
    close = _num(df.get("close", np.nan), default=np.nan)
    open_ = _num(df.get("open", np.nan), default=np.nan)
    ask = _num(df.get("ask_volume", 0.0), default=0.0)
    bid = _num(df.get("bid_volume", 0.0), default=0.0)
    have_ab = (ask.sum() + bid.sum()) > 0.0

    # total volume (par ligne)
    if have_ab:
        total = ask + bid
        # imbalance_row ∈ [0..1]
        with np.errstate(divide="ignore", invalid="ignore"):
            imbalance_row = np.where(total > 0.0, ask / total, 0.5)
    else:
        total = _num(df.get("tick_volume", 0.0), default=0.0)
        imbalance_row = np.full(
            n, 0.5, dtype=float
        )  # sans ask/bid → neutre pour l'imbalance

    # delta: priorité à la colonne, sinon fallback
    if "delta" in df.columns:
        delta = _num(df["delta"], default=0.0)
    elif have_ab:
        delta = ask - bid
    else:
        # fallback proxy: signe(close - prev) * tick_volume
        prev = np.roll(close, 1)
        prev[0] = close[0]
        sign = np.where(close >= prev, 1.0, -1.0)
        delta = sign * total

    # aggressor ratio
    agr = _num(df.get("aggressor_ratio", 0.5), default=0.5)

    # timestamps (compat)
    if "time" in df.columns:
        timestamps = df["time"].astype(str).to_numpy()
    elif "timestamp" in df.columns:
        timestamps = df["timestamp"].astype(str).to_numpy()
    else:
        timestamps = np.arange(n).astype(str)

    # price_level (optionnel)
    if "price_level" in df.columns:
        pl_raw = pd.to_numeric(df["price_level"], errors="coerce").to_numpy(dtype=float)
        price_level = np.where(np.isfinite(pl_raw), pl_raw, np.nan)
    else:
        price_level = np.full(n, np.nan, dtype=float)

    # dominance (buyers/sellers/neutral) vectorisé
    dominance = np.where(delta > 0, "buyers", np.where(delta < 0, "sellers", "neutral"))

    # ==========================
    #    Masques vectorisés
    # ==========================
    # Imbalance (seuil direct 0..1 vs centred [-1..1])
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

    # Absorption (besoin ask/bid). On accepte close/open NaN (on teste seulement si dispo).
    close_lt_open = np.where(
        np.isfinite(close) & np.isfinite(open_), close < open_, False
    )
    close_gt_open = np.where(
        np.isfinite(close) & np.isfinite(open_), close > open_, False
    )

    if have_ab:
        # ask >> bid ET (delta<0 OU close<open)
        buy_abs_mask = (ask > 2.0 * (bid + 1e-9)) & ((delta < 0.0) | close_lt_open)
        # bid >> ask ET (delta>0 OU close>open)
        sell_abs_mask = (bid > 2.0 * (ask + 1e-9)) & ((delta > 0.0) | close_gt_open)
    else:
        buy_abs_mask = np.zeros(n, dtype=bool)
        sell_abs_mask = np.zeros(n, dtype=bool)

    # Climax (quantile 97% si du volume existe)
    if np.any(total > 0.0):
        try:
            q = float(np.quantile(total, 0.97))
        except Exception:
            q = 0.0
        climax_mask = (q > 0.0) & (total >= q)
    else:
        climax_mask = np.zeros(n, dtype=bool)

    # Iceberg (optionnel)
    if ("executions_count" in df.columns) and ("avg_exec_size" in df.columns):
        execs = _num(df["executions_count"], default=0.0)
        avgsz = _num(df["avg_exec_size"], default=0.0)
        iceberg_mask = (execs > 50.0) & (avgsz < 0.2 * np.maximum(total, 1.0))
    else:
        iceberg_mask = np.zeros(n, dtype=bool)

    # ==========================
    #    Émission des events
    # ==========================
    def _emit(
        indices: np.ndarray,
        label: str,
        *,
        add_footprint: bool = False,
        add_iceberg: bool = False,
    ):
        for i in indices.tolist():
            ev: Dict[str, Any] = {
                "index": int(i),
                "timestamp": str(timestamps[i]),
                "pattern": label,
                "price_level": (
                    float(price_level[i]) if np.isfinite(price_level[i]) else None
                ),
                "imbalance": float(imbalance_row[i]),
                "delta": float(delta[i]),
                "dominance": str(dominance[i]),
                "bid_volume": float(bid[i]) if i < len(bid) else 0.0,
                "ask_volume": float(ask[i]) if i < len(ask) else 0.0,
                "row_total": float(total[i]),
            }
            if add_footprint:
                ev["footprint"] = f"buy_ratio={agr[i]:.2f}"
            if add_iceberg:
                ev["iceberg"] = {
                    "exec_count": (
                        int(df["executions_count"].iloc[i])
                        if "executions_count" in df.columns
                        else None
                    ),
                    "avg_size": (
                        float(df["avg_exec_size"].iloc[i])
                        if "avg_exec_size" in df.columns
                        else None
                    ),
                }
            events.append(ev)

    _emit(np.where(buy_imb_mask)[0], "buy_imbalance")
    _emit(np.where(sell_imb_mask)[0], "sell_imbalance")
    _emit(np.where(ag_buy_mask)[0], "aggressive_buying", add_footprint=True)
    _emit(np.where(ag_sell_mask)[0], "aggressive_selling", add_footprint=True)
    _emit(np.where(buy_abs_mask)[0], "buy_absorption")
    _emit(np.where(sell_abs_mask)[0], "sell_absorption")
    _emit(np.where(climax_mask)[0], "climax")
    _emit(np.where(iceberg_mask)[0], "iceberg_order", add_iceberg=True)

    return events
