# phase_observer/detect_orderflow_v6/data_preparator.py
from __future__ import annotations
from typing import Tuple
import pandas as pd
import numpy as np

# Colonnes minimales attendues + colonnes garanties en sortie
REQ_BASE_COLS = ["time", "open", "high", "low", "close", "tick_volume", "real_volume"]
REQ_OUT_COLS  = ["ask_volume", "bid_volume", "aggressor_buy_vol", "aggressor_sell_vol"]

def _to_num(s, default=0.0) -> pd.Series:
    return pd.to_numeric(s, errors="coerce").fillna(default)

def _ensure_cols(df: pd.DataFrame, cols: list[str], default=np.nan) -> None:
    for c in cols:
        if c not in df.columns:
            df[c] = default

def _alias_volumes(df: pd.DataFrame) -> None:
    """Normalise tick/real volume via alias courants."""
    if "tick_volume" not in df.columns:
        for a in ("volume", "vol", "Volume"):
            if a in df.columns:
                df["tick_volume"] = _to_num(df[a], 0.0)
                break
    if "tick_volume" not in df.columns:
        df["tick_volume"] = 0.0

    if "real_volume" not in df.columns:
        # fallback réaliste: tick_volume si pas de vraie taille
        df["real_volume"] = _to_num(df.get("real_volume", df["tick_volume"]), 0.0)

def _normalize_tick_counters(df: pd.DataFrame) -> None:
    """Normalise toutes les variantes de compteurs de ticks BUY/SELL."""
    for _col in (
        "BUY","SELL","buy_ticks","sell_ticks","ticks_buy","ticks_sell",
        "t_buy","t_sell","buys","sells"
    ):
        if _col in df.columns:
            df[_col] = _to_num(df[_col], 0.0)

def validate_and_prepare_data(df_in: pd.DataFrame) -> Tuple[pd.DataFrame, int, str]:
    """
    Retourne (df_prêt, rescue_level, rescue_note)
    rescue_level: 0=none, 1=soft (réparations mineures), 2=hard (proxy)
    Colonnes garanties en sortie: REQ_BASE_COLS + REQ_OUT_COLS
    """
    rescue_level, rescue_note = 0, "ok"

    # Cas bord → DF vide normalisé
    if df_in is None or len(df_in) == 0:
        empty = pd.DataFrame(columns=REQ_BASE_COLS + REQ_OUT_COLS)
        return empty, 2, "empty_df"

    df = df_in.copy()

    # -- 0) TIME: parsing UTC, tri, dé-doublonnage
    if "time" in df.columns:
        try:
            df["time"] = pd.to_datetime(df["time"], errors="coerce", utc=True)
            df = df.dropna(subset=["time"]).sort_values("time").drop_duplicates(subset=["time"])
        except Exception:
            rescue_level, rescue_note = max(rescue_level, 1), "time_parse_warn"

    # -- 1) Aliases & présence des colonnes minimales
    _alias_volumes(df)
    _ensure_cols(df, ["open","high","low","close"], np.nan)

    # -- 2) Casting numeric des colonnes OHLC & volumes
    for c in ("open","high","low","close","tick_volume","real_volume"):
        df[c] = _to_num(df[c], 0.0)

    # -- 3) Réparation des trous OHLC (ffill/bfill)
    before = int(df[["open","high","low","close"]].isna().sum().sum())
    df[["open","high","low","close"]] = (
        df[["open","high","low","close"]].fillna(method="ffill").fillna(method="bfill")
    )
    after = int(df[["open","high","low","close"]].isna().sum().sum())
    if after < before:
        rescue_level = max(rescue_level, 1)

    # -- 4) Normalisation compteurs ticks
    _normalize_tick_counters(df)

    # -- 5) Construction ask/bid/aggressors (hiérarchie de rescues)
    # 5.a) Si ask/bid déjà fournis de manière exploitable → on garde
    ask, bid = None, None
    if "ask_volume" in df.columns and "bid_volume" in df.columns:
        a = _to_num(df["ask_volume"], 0.0)
        b = _to_num(df["bid_volume"], 0.0)
        if float((a + b).sum()) > 0.0:
            ask, bid = a, b

    # 5.b) Sinon, essaie les compteurs de ticks BUY/SELL
    if ask is None or bid is None:
        buy_ticks = None; sell_ticks = None
        for name in ("buy_ticks","ticks_buy","t_buy","buys","BUY"):
            if name in df.columns:
                buy_ticks = _to_num(df[name], 0.0); break
        for name in ("sell_ticks","ticks_sell","t_sell","sells","SELL"):
            if name in df.columns:
                sell_ticks = _to_num(df[name], 0.0); break
        if buy_ticks is not None or sell_ticks is not None:
            # si une seule série présente, l'autre vaut 0
            if buy_ticks is None:
                buy_ticks = pd.Series(0.0, index=df.index, dtype="float64")
            if sell_ticks is None:
                sell_ticks = pd.Series(0.0, index=df.index, dtype="float64")
            ask, bid = buy_ticks, sell_ticks
            rescue_level, rescue_note = max(rescue_level, 1), "tick_counters"

    # 5.c) Sinon, split dynamique depuis tick_volume
    if ask is None or bid is None:
        vol_row = _to_num(df.get("tick_volume", 0.0), 0.0)
        if float(vol_row.sum()) > 0.0:
            # Poids directionnels via corps OHLC si dispo, sinon variation close
            if {"open","high","low","close"}.issubset(df.columns):
                o = _to_num(df["open"], 0.0); h = _to_num(df["high"], 0.0)
                l = _to_num(df["low"], 0.0);  c = _to_num(df["close"], 0.0)
                rng = (h - l).replace(0.0, np.nan)
                body_frac = ((c - o) / rng).clip(-1.0, 1.0).fillna(0.0)
                w_long = (0.5 + 0.4 * body_frac).clip(0.10, 0.90)
                rescue_level, rescue_note = max(rescue_level, 1), "split_dynamic_price_ohlc"
            else:
                close_like = _to_num(df.get("close", df.get("last", 0.0)), 0.0)
                up = close_like.diff().fillna(0.0)
                w_long = pd.Series(
                    np.where(up > 0, 0.80, np.where(up < 0, 0.20, 0.50)),
                    index=df.index, dtype="float64"
                )
                rescue_level, rescue_note = max(rescue_level, 1), "split_dynamic_directional"

            w_short = (1.0 - w_long).astype("float64")
            ask = (vol_row * w_long).astype("float64")
            bid = (vol_row * w_short).astype("float64")

    # 5.d) Dernier recours : proxy neutre 50/50
    if ask is None or bid is None:
        proxy = pd.Series(1.0, index=df.index, dtype="float64")
        ask = (proxy * 0.5).astype("float64")
        bid = (proxy * 0.5).astype("float64")
        rescue_level, rescue_note = 2, "proxy_50_50"

    df["ask_volume"] = ask
    df["bid_volume"] = bid

    # -- 6) Aggressors: fallback sur ask/bid si colonnes manquantes
    ag_buy  = _to_num(df.get("aggressor_buy_vol", ask), 0.0)
    ag_sell = _to_num(df.get("aggressor_sell_vol", bid), 0.0)
    df["aggressor_buy_vol"]  = ag_buy.astype("float64")
    df["aggressor_sell_vol"] = ag_sell.astype("float64")

    # -- 7) Assure la présence des colonnes et dtype final
    _ensure_cols(df, REQ_BASE_COLS, np.nan)
    _ensure_cols(df, REQ_OUT_COLS, 0.0)
    for c in (REQ_BASE_COLS[1:] + REQ_OUT_COLS):  # exclut 'time'
        df[c] = _to_num(df[c], 0.0).astype("float64")

    return df, rescue_level, rescue_note
