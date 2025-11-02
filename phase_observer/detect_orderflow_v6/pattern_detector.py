# phase_observer/detect_orderflow_v6/pattern_detector.py
from __future__ import annotations
from typing import Dict, Any, List
import pandas as pd
import numpy as np

def detect_patterns(df: pd.DataFrame, imbalance_threshold: float = 0.7) -> List[Dict, Any]:
    """
    Détecte les patterns au format V5 (liste d'événements par ligne).
    - buy_imbalance / sell_imbalance (par ligne)
    - absorption (ask>>bid mais delta<0 ou close<open) et inverse
    - agressivité (aggressor_ratio)
    - climax (top quantile volume)
    - iceberg (si executions_count / avg_exec_size présents)
    
    Paramètre:
      imbalance_threshold:
        - Si >= 0.5 → seuil direct sur imbalance_row (0..1) (ex: 0.7 comme V5)
        - Si <  0.5 → seuil centré sur [-1..1] (0.2 ≈ 70/30)
    """
    events: List[Dict[str, Any]] = []
    if df is None or len(df) == 0:
        return events

    # Helpers numériques rapides
    def _num(s, default=0.0):
        return pd.to_numeric(s, errors="coerce").fillna(default)

    # Séries de travail (protégées)
    ask = _num(df.get("ask_volume", 0.0))
    bid = _num(df.get("bid_volume", 0.0))
    if (ask.sum() + bid.sum()) > 0:
        total = ask + bid
        imbalance_row = np.where(total.to_numpy() > 0, ask.to_numpy() / total.to_numpy(), 0.5)
    else:
        # fallback si ask/bid absents: tick_volume
        total = _num(df.get("tick_volume", 0.0))
        imbalance_row = np.full(len(df), 0.5, dtype=float)

    delta = _num(df.get("delta", ask - bid))
    agr   = _num(df.get("aggressor_ratio", 0.5))
    close = _num(df.get("close", np.nan))
    open_ = _num(df.get("open", np.nan))

    # Climax (top 3% par défaut, sinon désactivé si pas de volume)
    try:
        q = float(total.quantile(0.97)) if hasattr(total, "quantile") and len(total) else 0.0
    except Exception:
        q = 0.0

    # Iceberg inputs (optionnels)
    has_execs = ("executions_count" in df.columns) and ("avg_exec_size" in df.columns)
    execs = _num(df.get("executions_count")) if has_execs else None
    avg_sz = _num(df.get("avg_exec_size")) if has_execs else None

    # Normalisation du seuil d'imbalance
    use_direct = imbalance_threshold >= 0.5  # V5-like (0..1)
    thr_direct = float(imbalance_threshold)
    thr_center = float(imbalance_threshold)   # si <0.5 → centré [-1..1]
    # boucle principale
    for i in range(len(df)):
        labels: List[str] = []
        extras: List[Dict[str, Any]] = []

        row_total = float(total.iloc[i]) if hasattr(total, "iloc") else float(total[i])
        row_imb   = float(imbalance_row[i])
        row_delta = float(delta.iloc[i]) if hasattr(delta, "iloc") else float(delta[i])
        row_agr   = float(agr.iloc[i]) if hasattr(agr, "iloc") else float(agr[i])
        row_ask   = float(ask.iloc[i]) if hasattr(ask, "iloc") else float(ask[i]) if len(ask) else 0.0
        row_bid   = float(bid.iloc[i]) if hasattr(bid, "iloc") else float(bid[i]) if len(bid) else 0.0

        # 1) Imbalance (par ligne)
        if use_direct:
            if row_imb >= thr_direct:        # ex: 0.7
                labels.append("buy_imbalance")
            if row_imb <= (1.0 - thr_direct):
                labels.append("sell_imbalance")
        else:
            # seuil centré [-1..1] ; 0.2 ≈ 70/30
            centered = 2.0 * row_imb - 1.0
            if centered >= thr_center:
                labels.append("buy_imbalance")
            if centered <= -thr_center:
                labels.append("sell_imbalance")

        # 2) Agressivité
        if row_agr >= 0.75:
            labels.append("aggressive_buying")
            extras.append({"footprint": f"buy_ratio={row_agr:.2f}"})
        if row_agr <= 0.25:
            labels.append("aggressive_selling")
            extras.append({"footprint": f"buy_ratio={row_agr:.2f}"})

        # 3) Absorption institutionnelle (heuristique robuste)
        #   - buy_absorption: ask >> bid MAIS delta<0 OU close<open
        #   - sell_absorption: bid >> ask MAIS delta>0 OU close>open
        try:
            # ratio > 2x (paramétrable ultérieurement)
            if row_ask > 2.0 * (row_bid + 1e-9) and (row_delta < 0.0 or (not np.isnan(close[i]) and not np.isnan(open_[i]) and close[i] < open_[i])):
                labels.append("buy_absorption")
            if row_bid > 2.0 * (row_ask + 1e-9) and (row_delta > 0.0 or (not np.isnan(close[i]) and not np.isnan(open_[i]) and close[i] > open_[i])):
                labels.append("sell_absorption")
        except Exception:
            pass

        # 4) Climax bar
        try:
            if q > 0.0 and row_total >= q:
                labels.append("climax")
        except Exception:
            pass

        # 5) Iceberg (si colonnes présentes)
        if has_execs:
            try:
                ec = float(execs.iloc[i])
                av = float(avg_sz.iloc[i])
                if (ec > 50.0) and (av < 0.2 * max(row_total, 1.0)):
                    labels.append("iceberg_order")
                    extras.append({"iceberg": {"exec_count": int(ec), "avg_size": float(av)}})
            except Exception:
                pass

        # Timestamp & price level (compat V5)
        if "time" in df.columns:
            ts = str(df.at[df.index[i], "time"])
        elif "timestamp" in df.columns:
            ts = str(df.at[df.index[i], "timestamp"])
        else:
            ts = str(i)

        price_level = None
        if "price_level" in df.columns and pd.notna(df.at[df.index[i], "price_level"]):
            try:
                price_level = float(df.at[df.index[i], "price_level"])
            except Exception:
                price_level = None

        # Dominance (compat V5)
        dominance = "buyers" if row_delta > 0 else ("sellers" if row_delta < 0 else "neutral")

        # Émettre un événement par label (même format que V5)
        for k, label in enumerate(labels):
            ev = {
                "index": i,
                "timestamp": ts,
                "pattern": label,
                "price_level": price_level,
                "imbalance": row_imb,
                "delta": row_delta,
                "dominance": dominance,
                "bid_volume": row_bid,
                "ask_volume": row_ask,
                "row_total": row_total,
            }
            if k < len(extras):
                ev.update(extras[k])
            events.append(ev)

    return events


