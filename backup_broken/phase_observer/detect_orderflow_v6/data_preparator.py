# phase_observer/detect_orderflow_v6/data_preparator.py
from __future__ import annotations
from typing import Tuple
import pandas as pd
import numpy as np

# Colonnes minimales attendues + colonnes garanties en sortie
REQ_BASE_COLS = ["time", "open", "high", "low", "close", "tick_volume", "real_volume"]
REQ_OUT_COLS = ["ask_volume", "bid_volume", "aggressor_buy_vol", "aggressor_sell_vol"]


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
        "BUY",
        "SELL",
        "buy_ticks",
        "sell_ticks",
        "ticks_buy",
        "ticks_sell",
        "t_buy",
        "t_sell",
        "buys",
        "sells",
    ):
        if _col in df.columns:
            df[_col] = _to_num(df[_col], 0.0)


def validate_and_prepare_data(df_in: pd.DataFrame) -> Tuple[pd.DataFrame, int, str]:
    """
    Retourne (df_prêt, rescue_level, rescue_note)
    rescue_level: 0=none, 1=soft (réparations mineures), 2=hard (proxy)
    Colonnes garanties en sortie: REQ_BASE_COLS + REQ_OUT_COLS
    """
    import numpy as np
    import pandas as pd

    # --- Constantes (fallback si absentes dans le module) ---
    req_base = globals().get(
        "REQ_BASE_COLS",
        [
            "time",
            "open",
            "high",
            "low",
            "close",
            "tick_volume",
            "real_volume",
            "ask_volume",
            "bid_volume",
            "aggressor_buy_vol",
            "aggressor_sell_vol",
        ],
    )
    req_out = globals().get(
        "REQ_OUT_COLS",
        ["delta", "total_volume", "imbalance_row", "aggressor_ratio"],
    )

    def _num(s, default=0.0):
        return pd.to_numeric(s, errors="coerce").fillna(default)

    def _ensure(df: pd.DataFrame, cols: list[str], default):
        for c in cols:
            if c not in df.columns:
                df[c] = default

    def _alias_volumes_inline(df: pd.DataFrame):
        # Normalisation douce des alias courants → colonnes cibles
        alias_map = {
            "tick_volume": ["ticks", "volume", "vol", "tickVol", "tick-vol"],
            "real_volume": ["realVol", "real-vol", "volume_real"],
            "ask_volume": ["askVol", "ask-vol", "vol_buy", "buy_vol", "buyVolume"],
            "bid_volume": ["bidVol", "bid-vol", "vol_sell", "sell_vol", "sellVolume"],
        }
        for target, aliases in alias_map.items():
            if target not in df.columns:
                for a in aliases:
                    if a in df.columns:
                        df[target] = df[a]
                        break
        return df

    def _normalize_tick_counters_inline(df: pd.DataFrame):
        # Uniformise les compteurs BUY/SELL si présents (sans forcer leur existence)
        buy_names = ("buy_ticks", "ticks_buy", "t_buy", "buys", "BUY")
        sell_names = ("sell_ticks", "ticks_sell", "t_sell", "sells", "SELL")
        bt, st = None, None
        for name in buy_names:
            if name in df.columns:
                bt = _num(df[name], 0.0)
                break
        for name in sell_names:
            if name in df.columns:
                st = _num(df[name], 0.0)
                break
        # Clamp >=0
        if bt is not None:
            df["buy_ticks"] = np.maximum(0.0, bt.to_numpy(dtype=float))
        if st is not None:
            df["sell_ticks"] = np.maximum(0.0, st.to_numpy(dtype=float))

    rescue_level, notes = 0, []

    # Cas bord → DF vide normalisé
    if df_in is None or len(df_in) == 0:
        empty = pd.DataFrame(columns=req_base + req_out)
        return empty, 2, "empty_df"

    df = df_in.copy()

    # -- 0) TIME: parsing UTC, tri, dé-doublonnage
    if "time" in df.columns:
        try:
            df["time"] = pd.to_datetime(df["time"], errors="coerce", utc=True)
            df = (
                df.dropna(subset=["time"])
                .sort_values("time")
                .drop_duplicates(subset=["time"])
            )
        except Exception:
            rescue_level = max(rescue_level, 1)
            notes.append("time_parse_warn")

    # -- 1) Aliases & présence des colonnes minimales
    df = _alias_volumes_inline(df)
    _ensure(df, ["open", "high", "low", "close"], np.nan)
    _ensure(df, ["tick_volume", "real_volume"], 0.0)

    # -- 2) Casting numeric des colonnes OHLC & volumes
    for c in ("open", "high", "low", "close", "tick_volume", "real_volume"):
        df[c] = _num(df[c], 0.0)

    # -- 3) Réparation des trous OHLC (ffill/bfill)
    before = int(df[["open", "high", "low", "close"]].isna().sum().sum())
    if before:
        df[["open", "high", "low", "close"]] = (
            df[["open", "high", "low", "close"]].ffill().bfill()
        )
        after = int(df[["open", "high", "low", "close"]].isna().sum().sum())
        if after < before:
            rescue_level = max(rescue_level, 1)
            notes.append("ohlc_ffill_bfill")

    # -- 4) Normalisation compteurs ticks (si présents)
    try:
        _normalize_tick_counters_inline(df)
    except Exception:
        pass

    # -- 5) Construction ask/bid/aggressors (hiérarchie de rescues)
    ask, bid = None, None

    # 5.a) Si ask/bid déjà fournis et exploitables → on garde
    if "ask_volume" in df.columns and "bid_volume" in df.columns:
        a = _num(df["ask_volume"], 0.0).to_numpy(dtype=float)
        b = _num(df["bid_volume"], 0.0).to_numpy(dtype=float)
        a = np.maximum(0.0, a)
        b = np.maximum(0.0, b)
        if float((a + b).sum()) > 0.0:
            ask, bid = a, b

    # 5.b) Sinon, essaie les compteurs de ticks BUY/SELL
    if ask is None or bid is None:
        bt = (
            df["buy_ticks"].to_numpy(dtype=float) if "buy_ticks" in df.columns else None
        )
        st = (
            df["sell_ticks"].to_numpy(dtype=float)
            if "sell_ticks" in df.columns
            else None
        )
        if bt is not None or st is not None:
            if bt is None:
                bt = np.zeros(len(df), dtype=float)
            if st is None:
                st = np.zeros(len(df), dtype=float)
            ask, bid = np.maximum(0.0, bt), np.maximum(0.0, st)
            rescue_level = max(rescue_level, 1)
            notes.append("tick_counters")

    # 5.c) Sinon, split dynamique depuis tick_volume
    if ask is None or bid is None:
        vol_row = _num(df.get("tick_volume", 0.0), 0.0).to_numpy(dtype=float)
        if float(np.nansum(vol_row)) > 0.0:
            if {"open", "high", "low", "close"}.issubset(df.columns):
                o = _num(df["open"], 0.0).to_numpy(dtype=float)
                h = _num(df["high"], 0.0).to_numpy(dtype=float)
                l = _num(df["low"], 0.0).to_numpy(dtype=float)
                c = _num(df["close"], 0.0).to_numpy(dtype=float)
                rng = h - l
                rng[rng == 0.0] = np.nan
                body_frac = np.clip((c - o) / rng, -1.0, 1.0)
                body_frac = np.nan_to_num(body_frac, nan=0.0)
                w_long = np.clip(0.5 + 0.4 * body_frac, 0.10, 0.90)
                notes.append("split_dynamic_price_ohlc")
            else:
                close_like = _num(df.get("close", df.get("last", 0.0)), 0.0).to_numpy(
                    dtype=float
                )
                up = np.diff(close_like, prepend=close_like[:1])
                w_long = np.where(up > 0, 0.80, np.where(up < 0, 0.20, 0.50))
                notes.append("split_dynamic_directional")
            w_short = 1.0 - w_long
            ask = (vol_row * w_long).astype(float)
            bid = (vol_row * w_short).astype(float)
            rescue_level = max(rescue_level, 1)

    # 5.d) Dernier recours : proxy neutre 50/50
    if ask is None or bid is None:
        n = len(df)
        ask = np.full(n, 0.5, dtype=float)
        bid = np.full(n, 0.5, dtype=float)
        rescue_level = 2
        notes.append("proxy_50_50")

    # Clamp >= 0, supprime NaN/Inf
    ask = np.nan_to_num(np.maximum(0.0, ask), nan=0.0, posinf=0.0, neginf=0.0)
    bid = np.nan_to_num(np.maximum(0.0, bid), nan=0.0, posinf=0.0, neginf=0.0)

    df["ask_volume"] = ask
    df["bid_volume"] = bid

    # -- 6) Aggressors: fallback sur ask/bid si colonnes manquantes
    ag_buy = _num(df.get("aggressor_buy_vol", df["ask_volume"]), 0.0).to_numpy(
        dtype=float
    )
    ag_sell = _num(df.get("aggressor_sell_vol", df["bid_volume"]), 0.0).to_numpy(
        dtype=float
    )
    df["aggressor_buy_vol"] = np.nan_to_num(
        np.maximum(0.0, ag_buy), nan=0.0, posinf=0.0, neginf=0.0
    )
    df["aggressor_sell_vol"] = np.nan_to_num(
        np.maximum(0.0, ag_sell), nan=0.0, posinf=0.0, neginf=0.0
    )

    # -- 7) Assure la présence des colonnes et dtype final
    _ensure(df, req_base, np.nan)
    _ensure(df, req_out, 0.0)

    # dtypes finaux (float64 sauf 'time')
    numeric_cols = [
        c for c in (req_base[1:] + req_out) if c in df.columns
    ]  # exclut 'time'
    for c in numeric_cols:
        df[c] = _num(df[c], 0.0).astype("float64")

    # Note finale
    rescue_note = (
        "ok" if not notes else "|".join(dict.fromkeys(notes))
    )  # dédoublonne et conserve l'ordre

    return df, rescue_level, rescue_note
