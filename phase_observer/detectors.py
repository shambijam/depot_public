# phase_observer/detectors.py
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
from .features import (
    _get_swing_points,
    _get_trend,
)

LOG = logging.getLogger(__name__)

# ============================================================
# 🔹 Candle Detectors (single candle, doji, hammer, marubozu…)
# ============================================================

"""
Détection factuelle de chandeliers individuels.
Catalogue : Doji (et variantes), Hammer, Hanging Man, Inverted Hammer, Shooting Star,
Marubozu, Spinning Top, Engulfing, Belt Hold, Kicker.
Pas de scoring → sortie brute, descriptive et exploitable.
"""


def detect_single_candle(
    df: pd.DataFrame, i: int, patterns: Optional[Dict[str, Any]] = None
) -> Optional[Dict[str, Any]]:

    try:
        o, h, l, c = (
            df["open"].iloc[i],
            df["high"].iloc[i],
            df["low"].iloc[i],
            df["close"].iloc[i],
        )
        body = abs(c - o)
        size = h - l
        upper_wick = h - max(o, c)
        lower_wick = min(o, c) - l
        body_ratio = body / size if size > 0 else 0
        is_bull = c > o

        pattern, pattern_type = None, None

        # === DOJI & VARIANTS ===
        if body_ratio < 0.1:
            if abs(upper_wick - lower_wick) < 0.1 * size:
                pattern, pattern_type = "doji", "indecision"
            elif upper_wick > 2 * body and lower_wick < 0.1 * size:
                pattern, pattern_type = "gravestone_doji", "reversal"
            elif lower_wick > 2 * body and upper_wick < 0.1 * size:
                pattern, pattern_type = "dragonfly_doji", "reversal"
            else:
                pattern, pattern_type = "doji", "indecision"

        # === SPINNING TOP ===
        elif body_ratio < 0.3 and upper_wick > 0.3 * size and lower_wick > 0.3 * size:
            pattern, pattern_type = "spinning_top", "indecision"

        # === HAMMER FAMILY ===
        elif lower_wick > 2 * body and upper_wick < body:
            pattern, pattern_type = ("hammer" if is_bull else "hanging_man", "reversal")
        elif upper_wick > 2 * body and lower_wick < body:
            pattern, pattern_type = (
                "inverted_hammer" if is_bull else "shooting_star",
                "reversal",
            )

        # === MARUBOZU ===
        elif (
            body_ratio > 0.95 and upper_wick < 0.05 * size and lower_wick < 0.05 * size
        ):
            pattern, pattern_type = (
                "marubozu_bull" if is_bull else "marubozu_bear",
                "momentum",
            )

        # === ENGULFING SIMPLE ===
        if i > 0 and body > abs(df["close"].iloc[i - 1] - df["open"].iloc[i - 1]):
            prev_o, prev_c = df["open"].iloc[i - 1], df["close"].iloc[i - 1]
            if is_bull and c > prev_o and o < prev_c:
                pattern, pattern_type = "bullish_engulfing", "reversal"
            elif not is_bull and c < prev_o and o > prev_c:
                pattern, pattern_type = "bearish_engulfing", "reversal"

        # === BELT HOLD ===
        if body_ratio > 0.7 and (upper_wick < 0.05 * size or lower_wick < 0.05 * size):
            pattern, pattern_type = (
                "belt_hold_bull" if is_bull else "belt_hold_bear",
                "continuation",
            )

        # === KICKER (gap fort) ===
        if i > 0:
            prev_c = df["close"].iloc[i - 1]
            if is_bull and o > prev_c and c > o:
                pattern, pattern_type = "bullish_kicker", "reversal"
            elif not is_bull and o < prev_c and c < o:
                pattern, pattern_type = "bearish_kicker", "reversal"

        # === RETOUR FACTUEL ===
        if pattern:
            enriched: Dict[str, Any] = {
                "pattern": pattern,
                "type": pattern_type,
                "is_bullish": is_bull,
                "body_ratio": round(body_ratio, 3),
                "upper_wick": round(upper_wick, 5),
                "lower_wick": round(lower_wick, 5),
                "candle_size": round(size, 5),
            }

            # Ajout de contexte si dispo
            if "volume_zscore" in df.columns:
                enriched["volume_zscore"] = float(df["volume_zscore"].iloc[i])
            if "phase" in df.columns:
                enriched["phase"] = str(df["phase"].iloc[i])

            return enriched

        return None

    except Exception:
        return None


# ============================================================
# 🔹 Multi-Candle Detectors (engulfing, morning star…)
# ============================================================

"""
Détection de patterns multi-bougies :
- Morning Star
- Evening Star
- Three White Soldiers
- Three Black Crows
- Harami
- Tweezers
"""


def is_morning_star(df: pd.DataFrame, i: int) -> Optional[Dict[str, Any]]:
    if i < 2:
        return None
    c1, c2, c3 = df.iloc[i - 2], df.iloc[i - 1], df.iloc[i]
    if (
        c1["close"] < c1["open"]  # 1ère rouge
        and abs(c2["close"] - c2["open"])
        < (c1["open"] - c1["close"]) * 0.5  # petit corps
        and c3["close"] > c3["open"]  # verte
        and c3["close"] > (c1["open"] + c1["close"]) / 2
    ):
        return {"pattern": "morning_star", "type": "reversal", "is_bullish": True}
    return None


def is_evening_star(df: pd.DataFrame, i: int) -> Optional[Dict[str, Any]]:
    if i < 2:
        return None
    c1, c2, c3 = df.iloc[i - 2], df.iloc[i - 1], df.iloc[i]
    if (
        c1["close"] > c1["open"]
        and abs(c2["close"] - c2["open"]) < (c1["close"] - c1["open"]) * 0.5
        and c3["close"] < c3["open"]
        and c3["close"] < (c1["open"] + c1["close"]) / 2
    ):
        return {"pattern": "evening_star", "type": "reversal", "is_bullish": False}
    return None


def is_three_white_soldiers(df: pd.DataFrame, i: int) -> Optional[Dict[str, Any]]:
    if i < 2:
        return None
    c1, c2, c3 = df.iloc[i - 2], df.iloc[i - 1], df.iloc[i]
    if (
        c1["close"] > c1["open"]
        and c2["close"] > c2["open"]
        and c3["close"] > c3["open"]
        and c1["close"] < c2["close"] < c3["close"]
    ):
        return {
            "pattern": "three_white_soldiers",
            "type": "continuation",
            "is_bullish": True,
        }
    return None


def is_three_black_crows(df: pd.DataFrame, i: int) -> Optional[Dict[str, Any]]:
    if i < 2:
        return None
    c1, c2, c3 = df.iloc[i - 2], df.iloc[i - 1], df.iloc[i]
    if (
        c1["close"] < c1["open"]
        and c2["close"] < c2["open"]
        and c3["close"] < c3["open"]
        and c1["close"] > c2["close"] > c3["close"]
    ):
        return {
            "pattern": "three_black_crows",
            "type": "continuation",
            "is_bullish": False,
        }
    return None


def is_harami(df: pd.DataFrame, i: int) -> Optional[Dict[str, Any]]:
    if i < 1:
        return None
    c1, c2 = df.iloc[i - 1], df.iloc[i]
    if c1["close"] > c1["open"] and c2["close"] < c2["open"]:  # bull -> bear
        if c2["open"] < c1["close"] and c2["close"] > c1["open"]:
            return {
                "pattern": "bearish_harami",
                "type": "reversal",
                "is_bullish": False,
            }
    elif c1["close"] < c1["open"] and c2["close"] > c2["open"]:  # bear -> bull
        if c2["open"] > c1["close"] and c2["close"] < c1["open"]:
            return {"pattern": "bullish_harami", "type": "reversal", "is_bullish": True}
    return None


def is_tweezer(df: pd.DataFrame, i: int) -> Optional[Dict[str, Any]]:
    if i < 1:
        return None
    c1, c2 = df.iloc[i - 1], df.iloc[i]
    if abs(c1["high"] - c2["high"]) < 1e-5:  # sommets quasi identiques
        return {"pattern": "tweezer_top", "type": "reversal", "is_bullish": False}
    if abs(c1["low"] - c2["low"]) < 1e-5:  # creux quasi identiques
        return {"pattern": "tweezer_bottom", "type": "reversal", "is_bullish": True}
    return None


# ============================================================
# ===  Orchestrateurs ========================================
# ============================================================


def detect_multi_candle(
    df: pd.DataFrame, i: Optional[int] = None, patterns: Optional[Dict[str, Any]] = None
) -> List[Dict[str, Any]]:
    """
    Détection d’un ensemble de patterns multi-bougies.
    - Si `i` est fourni → détection ponctuelle à l'index i.
    - Si `i` est None → parcourt tout le DataFrame.
    """
    results: List[Dict[str, Any]] = []

    # Cas 1: on parcourt tout le DataFrame
    if i is None:
        for idx in range(len(df)):
            results.extend(detect_multi_candle(df, idx, patterns))
        return results

    # Cas 2: détection ponctuelle
    if i < 2:
        return results

    detectors = [
        is_morning_star,
        is_evening_star,
        is_three_white_soldiers,
        is_three_black_crows,
        is_harami,
        is_tweezer,
    ]

    for detector in detectors:
        try:
            res = detector(df, i)
            if res:
                enriched = res.copy()
                enriched["index"] = i
                enriched["timestamp"] = str(df.index[i])

                if "phase" in df.columns:
                    enriched["phase"] = str(df["phase"].iloc[i])
                if "volume_zscore" in df.columns:
                    enriched["volume_zscore"] = float(df["volume_zscore"].iloc[i])

                results.append(enriched)
        except Exception as e:
            print(f"Erreur {detector.__name__} à l’index {i}: {e}")

    return results


def detect_multi_candle_patterns(
    df: pd.DataFrame, patterns: Optional[Dict[str, Any]] = None
) -> List[Dict[str, Any]]:
    """
    Détection des patterns multi-bougies sur tout le DataFrame.
    Utilise detect_multi_candle(df) en mode global (i=None).
    Retourne une liste à plat de tous les patterns détectés.
    """
    return detect_multi_candle(df, i=None, patterns=patterns)


# ============================================================
# 🔹 Combo Detectors (confluences, MTF confirmations)
# ============================================================


def detect_combos(
    df: pd.DataFrame, patterns: Optional[Dict[str, Any]] = None
) -> List[Optional[List[Dict[str, Any]]]]:
    """
    Détecteur desk-trader brut :
    - Chandeliers individuels (Doji, Hammer, etc.)
    - Patterns multi-bougies (Morning Star, Soldiers, Harami, etc.)
    - Confluences structurelles (OB/FVG/BOS si colonnes dispo)
    - Confirmations multi-timeframe (pattern_m5 / pattern_m15)

    ❌ Aucun scoring → que du factuel.
    ✅ Chaque bougie a 0, 1 ou plusieurs patterns alignés à son index.
    """
    if df is None or len(df) < 5:
        return [None] * (len(df) if df is not None else 0)

    signals: List[Optional[List[Dict[str, Any]]]] = []

    for i in range(len(df)):
        try:
            sigs: List[Dict[str, Any]] = []

            # 1) Pattern individuel
            simple = detect_single_candle(df, i)
            if simple:
                sigs.append({"source": "single", **simple})

            # 2) Pattern(s) multi-bougies (détection ponctuelle à l’index i)
            multi_patterns = detect_multi_candle(df, i, patterns=patterns)
            for m in multi_patterns:
                sigs.append({"source": "multi", **m})

            if not sigs:
                signals.append(None)
                continue

            # 3) Confluences structurelles (OB/FVG/BOS si dispo)
            for s in sigs:
                s["near_ob"] = "ob_zone" in df.columns and not pd.isna(
                    df["ob_zone"].iloc[i]
                )
                s["near_fvg"] = "fvg" in df.columns and not pd.isna(df["fvg"].iloc[i])
                s["near_bos"] = "bos" in df.columns and not pd.isna(df["bos"].iloc[i])

                # 4) Confirmations multi-timeframe
                confirmed_tf = []
                for tf in ["pattern_m5", "pattern_m15"]:
                    if tf in df.columns and df[tf].iloc[i] == s.get("pattern"):
                        confirmed_tf.append(tf.upper())
                if confirmed_tf:
                    s["confirmed_tf"] = confirmed_tf

                # Ajout index + horodatage
                s["index"] = i
                s["timestamp"] = (
                    str(df.index[i]) if hasattr(df.index, "dtype") else None
                )

            signals.append(sigs)

        except Exception as e:
            LOG.error(f"[ComboDetector] Erreur à l'index {i}: {e}")
            signals.append(None)

    return signals


# ============================================================
# 🔹 Orderflow Detectors (absorptions, imbalances, exhaustion)
# ============================================================
def reconstruct_tick_side_mt5(ticks: pd.DataFrame) -> pd.DataFrame:
    """
    🏦 Dev Desk Banque Privée – Reconstruction microstructurelle MT5
    ----------------------------------------------------------------
    Objectif :
        Transformer le flux brut MT5 (bid/ask/last/volume_real) en données
        directionnelles exploitables par footprint_validator() et detect_orderflow_v5().

    Principe :
        - Recalcule le midprice, déduit le côté (buy/sell) du tick.
        - Affecte les volumes directionnels (bid_volume / ask_volume).
        - Construit les métriques d’agressivité (aggressor_buy_vol / aggressor_sell_vol).
        - Garantit la compatibilité structurelle avec le pipeline orderflow.

    Entrée :
        ticks : DataFrame issu de mt5.copy_ticks_range() ou copy_ticks_from()
            Colonnes minimales attendues : ["time", "bid", "ask", "last", "volume_real"]

    Sortie :
        DataFrame enrichi :
            ["time","price","size","side","bid_volume","ask_volume",
             "aggressor_buy_vol","aggressor_sell_vol","mid","spread"]
    """

    LOG = logging.getLogger("MT5TickRebuilder")
    if ticks is None or not isinstance(ticks, pd.DataFrame) or ticks.empty:
        LOG.warning("[Rebuilder] Flux ticks vide/invalide – retour DataFrame neutre.")
        return pd.DataFrame(
            columns=[
                "time",
                "price",
                "size",
                "side",
                "bid_volume",
                "ask_volume",
                "aggressor_buy_vol",
                "aggressor_sell_vol",
                "mid",
                "spread",
            ]
        )

    df = ticks.copy().reset_index(drop=True)

    # --- Normalisation des colonnes essentielles
    for col in ("bid", "ask", "last", "volume_real"):
        if col not in df.columns:
            df[col] = np.nan
    if "time" not in df.columns:
        df["time"] = pd.Timestamp.utcnow()

    # --- Nettoyage et typage
    df["bid"] = pd.to_numeric(df["bid"], errors="coerce")
    df["ask"] = pd.to_numeric(df["ask"], errors="coerce")
    df["last"] = pd.to_numeric(df["last"], errors="coerce")
    df["volume_real"] = pd.to_numeric(df["volume_real"], errors="coerce").fillna(0.0)

    if not pd.api.types.is_datetime64_any_dtype(df["time"]):
        df["time"] = pd.to_datetime(df["time"], errors="coerce").fillna(
            pd.Timestamp.utcnow()
        )

    # --- Recalcul du midprice et du spread
    df["mid"] = (df["bid"] + df["ask"]) / 2
    df["spread"] = df["ask"] - df["bid"]

    # --- Reconstruction du côté d’agresseur
    # Si last >= ask → acheteur agressif
    # Si last <= bid → vendeur agressif
    # Sinon → neutre/inconnu
    df["side"] = np.where(
        df["last"] >= df["ask"],
        "buy",
        np.where(df["last"] <= df["bid"], "sell", "unknown"),
    )

    # --- Volume directionnel
    df["size"] = df["volume_real"].replace(0, np.nan).fillna(1.0)
    df["bid_volume"] = np.where(df["side"] == "sell", df["size"], 0.0)
    df["ask_volume"] = np.where(df["side"] == "buy", df["size"], 0.0)
    df["aggressor_buy_vol"] = df["ask_volume"]
    df["aggressor_sell_vol"] = df["bid_volume"]

    # --- Sécurité : si tout est neutre, tenter une heuristique de direction
    if (df["side"] == "unknown").all():
        price_diff = df["last"].diff().fillna(0.0)
        df.loc[price_diff > 0, "side"] = "buy"
        df.loc[price_diff < 0, "side"] = "sell"
        LOG.info("[Rebuilder] Côté reconstruit par dérivée du prix (diff successive).")

    # --- Nettoyage final
    cols = [
        "time",
        "last",
        "size",
        "side",
        "bid_volume",
        "ask_volume",
        "aggressor_buy_vol",
        "aggressor_sell_vol",
        "mid",
        "spread",
    ]
    df.rename(columns={"last": "price"}, inplace=True)
    df = df[cols]

    # --- Statistiques rapides pour log
    total = len(df)
    buys = int((df["side"] == "buy").sum())
    sells = int((df["side"] == "sell").sum())
    LOG.info(
        f"[Rebuilder] Flux reconstruit : {total} ticks → {buys} buys / {sells} sells "
        f"(spread médian={df['spread'].median():.1e})"
    )

    return df


def detect_orderflow_v5(
    df: pd.DataFrame,
    imbalance_threshold: float = 0.7,
    cvd_smoothing: int = 5,
) -> Dict[str, Any]:
    """
    🏦 Orderflow V5 (Dev Desk Banque Privée)
    ---------------------------------------------------
    - Analyse multi-métriques : delta, CVD, absorption, agressivité.
    - Sortie unifiée (score + patterns + stats).
    - Compatible footprint_validator et DecisionPipeline.
    """

    LOG = logging.getLogger("OrderflowDetector")
    # --- FLAGS RESCUE ---
    rescue_mode: bool = False
    rescue_note: str = ""

    # ---------- 0) VALIDATION ----------
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        
        return {
            "score": 0,
            "status": "SUSPECT",
            "patterns": [],
            "summary": {"comment": "DataFrame vide ou invalide"},
            "df": pd.DataFrame(),
        }

    # On travaille sur une copie, index propre
    df = df.copy()
    df.reset_index(drop=True, inplace=True)

    # ---------- Helper: toujours renvoyer une Series float ----------
    def _safe_series(
        primary: str, aliases: tuple[str, ...] = (), default: float = 0.0
    ) -> pd.Series:
        # Trouve la première colonne existante parmi primary + aliases
        name = None
        for col in (primary, *aliases):
            if col in df.columns:
                name = col
                break
        if name is None:
            # Pas de colonne → Series constante
            return pd.Series(default, index=df.index, dtype="float64")
        col = df[name]
        # Si jamais c'est un scalaire (défensif)
        if not isinstance(col, pd.Series):
            return pd.Series(col, index=df.index, dtype="float64")
        return pd.to_numeric(col, errors="coerce").fillna(default).astype("float64")

    # Helper alias simple (case-sensitive, volontairement minimal)
    def _alias_series(aliases: tuple[str, ...], default: float = np.nan) -> pd.Series:
        for col in aliases:
            if col in df.columns:
                return pd.to_numeric(df[col], errors="coerce").fillna(default)
        return pd.Series(default, index=df.index, dtype="float64")

    # ---------- 1) CONSTRUCTION SÛRE DES COLONNES VOLUME ----------
    ask = _safe_series("ask_volume", aliases=("buy",))
    bid = _safe_series("bid_volume", aliases=("sell",))

    df["ask_volume"] = ask
    df["bid_volume"] = bid

    # Aggresseurs → fallback sur ask/bid si absents
    aggr_buy = _safe_series("aggressor_buy_vol", aliases=(), default=np.nan)
    aggr_sell = _safe_series("aggressor_sell_vol", aliases=(), default=np.nan)

    # Si NaN (colonne manquante), remplace par ask/bid
    if aggr_buy.isna().all():
        aggr_buy = ask.copy()
    else:
        aggr_buy = aggr_buy.fillna(ask)

    if aggr_sell.isna().all():
        aggr_sell = bid.copy()
    else:
        aggr_sell = aggr_sell.fillna(bid)

    df["aggressor_buy_vol"] = aggr_buy.astype("float64")
    df["aggressor_sell_vol"] = aggr_sell.astype("float64")

    # ---------- 1.bis) ZERO-VOLUME RESCUE (évite Vol=0, Δ=0, Imb=0.50) ----------
    # Si la somme ask+bid est nulle sur la fenêtre, on reconstruit des volumes proxy.
    if float((df["ask_volume"].sum() + df["bid_volume"].sum())) == 0.0:
        # 1) Si on a des compteurs de ticks buy/sell
        buy_ticks  = _safe_series("buy_ticks",  aliases=("ticks_buy", "t_buy",  "buys",  "BUY"),  default=np.nan)
        sell_ticks = _safe_series("sell_ticks", aliases=("ticks_sell","t_sell", "sells", "SELL"), default=np.nan)

        if not buy_ticks.isna().all() or not sell_ticks.isna().all():
            df["ask_volume"]  = buy_ticks.fillna(0.0).astype("float64")
            df["bid_volume"]  = sell_ticks.fillna(0.0).astype("float64")
            df["aggressor_buy_vol"]  = df["ask_volume"].copy()
            df["aggressor_sell_vol"] = df["bid_volume"].copy()
            rescue_mode, rescue_note = True, "tick_counters"
            LOG.info("[OrderflowV5] zero-volume rescue: tick counters utilisés.")

        else:
            # 2) Sinon, si on a un volume total par ligne (ex: MT5 'tick_volume')
            vol_total_row = _safe_series("tick_volume", aliases=("volume", "vol"), default=np.nan)

            # Déterminer un prix de référence pour le sens (close ou mid bid/ask ou last)
            price = None
            if "close" in df.columns:
                price = pd.to_numeric(df["close"], errors="coerce")
            elif {"bid", "ask"}.issubset(df.columns):
                price = (pd.to_numeric(df["bid"], errors="coerce") + pd.to_numeric(df["ask"], errors="coerce")) / 2.0
            elif "last" in df.columns:
                price = pd.to_numeric(df["last"], errors="coerce")

            if price is not None and not vol_total_row.isna().all():
                # Split dynamique : si OHLC dispo => pondération par taille de corps,
                # sinon fallback directionnel (↑=0.80 / ↓=0.20 / =0.50)
                dyn_w = None
                if {"open", "high", "low", "close"}.issubset(df.columns):
                    o = pd.to_numeric(df["open"],  errors="coerce")
                    h = pd.to_numeric(df["high"],  errors="coerce")
                    l = pd.to_numeric(df["low"],   errors="coerce")
                    c = pd.to_numeric(df["close"], errors="coerce")
                    rng = (h - l).replace(0.0, np.nan)
                    body_frac = ((c - o) / rng).clip(-1.0, 1.0).fillna(0.0)   # ∈ [-1..1]
                    dyn_w = (0.5 + 0.4 * body_frac).clip(0.10, 0.90)          # ∈ [0.10..0.90]

                if dyn_w is None:
                    up = price.diff().fillna(0.0)
                    dyn_w = pd.Series(
                        np.where(up > 0, 0.80, np.where(up < 0, 0.20, 0.50)),
                        index=df.index
                    )

                long_w  = dyn_w
                short_w = (1.0 - dyn_w)

                df["ask_volume"]  = (vol_total_row.fillna(0.0) * long_w).astype("float64")
                df["bid_volume"]  = (vol_total_row.fillna(0.0) * short_w).astype("float64")
                df["aggressor_buy_vol"]  = df["ask_volume"].copy()
                df["aggressor_sell_vol"] = df["bid_volume"].copy()

                rescue_mode, rescue_note = True, "split_dynamic_price_ohlc"
                LOG.info("[OrderflowV5] zero-volume rescue: split dynamique via price/ohlc.")

            else:
                # 3) Fallback neutre si rien d'exploitable : 1 unité / ligne, 50/50
                proxy = pd.Series(1.0, index=df.index, dtype="float64")
                df["ask_volume"]  = (proxy * 0.5).astype("float64")
                df["bid_volume"]  = (proxy * 0.5).astype("float64")
                df["aggressor_buy_vol"]  = df["ask_volume"].copy()
                df["aggressor_sell_vol"] = df["bid_volume"].copy()
                rescue_mode, rescue_note = True, "proxy_50_50"
                LOG.info("[OrderflowV5] zero-volume rescue: proxy neutre 50/50.")



    # ---------- 2) MÉTRIQUES DE BASE ----------
    df["total_volume"] = (df["bid_volume"] + df["ask_volume"]).astype("float64")
    df["delta"] = (df["ask_volume"] - df["bid_volume"]).astype("float64")

    denom = df["total_volume"].replace(0.0, np.nan)
    df["imbalance"] = (df["ask_volume"] / denom).fillna(0.5)

    df["dominance"] = np.where(
        df["delta"] > 0, "buyers", np.where(df["delta"] < 0, "sellers", "neutral")
    )

    # ---------- 3) CVD (Cumulative Volume Delta) ----------
    df["cvd"] = df["delta"].cumsum()
    win = max(int(cvd_smoothing), 1)
    df["cvd_smoothed"] = df["cvd"].rolling(window=win, min_periods=1).mean()

    # ---------- 4) Aggressivité footprint ----------
    aggr_denom = (df["aggressor_buy_vol"] + df["aggressor_sell_vol"]).replace(
        0.0, np.nan
    )
    df["aggressor_ratio"] = (df["aggressor_buy_vol"] / aggr_denom).fillna(0.5)

    # ---------- 5) DÉTECTION DE PATTERNS (multi-label / ligne) ----------
    patterns: List[Dict[str, Any]] = []
    for i in range(len(df)):
        try:
            labels: List[str] = []
            extras: List[Dict[str, Any]] = []

            imb = float(df.at[i, "imbalance"])
            delta = float(df.at[i, "delta"])
            bidv = float(df.at[i, "bid_volume"])
            askv = float(df.at[i, "ask_volume"])
            total = float(df.at[i, "total_volume"])
            aggr_ratio = float(df.at[i, "aggressor_ratio"])

            # Déséquilibres structurels
            if imb > imbalance_threshold:
                labels.append("buy_imbalance")
            if imb < (1.0 - imbalance_threshold):
                labels.append("sell_imbalance")

            # Absorptions (heuristique simple)
            if askv > 2.0 * (bidv + 1e-9):
                labels.append("buy_absorption")
            if bidv > 2.0 * (askv + 1e-9):
                labels.append("sell_absorption")

            # Agressivité
            if aggr_ratio >= 0.75:
                labels.append("aggressive_buying")
                extras.append({"footprint": f"buy_ratio={aggr_ratio:.2f}"})
            if aggr_ratio <= 0.25:
                labels.append("aggressive_selling")
                extras.append({"footprint": f"buy_ratio={aggr_ratio:.2f}"})

            # Icebergs (si colonnes présentes)
            if "executions_count" in df.columns and "avg_exec_size" in df.columns:
                exec_count = pd.to_numeric(
                    df.at[i, "executions_count"], errors="coerce"
                )
                avg_size = pd.to_numeric(df.at[i, "avg_exec_size"], errors="coerce")
                if pd.notna(exec_count) and pd.notna(avg_size):
                    if float(exec_count) > 50.0 and float(avg_size) < 0.2 * max(
                        total, 1.0
                    ):
                        labels.append("iceberg_order")
                        extras.append(
                            {
                                "iceberg": {
                                    "exec_count": int(float(exec_count)),
                                    "avg_size": float(avg_size),
                                }
                            }
                        )

            # Timestamp (priorité 'time' > 'timestamp' > index)
            if "time" in df.columns:
                ts = str(df.at[i, "time"])
            elif "timestamp" in df.columns:
                ts = str(df.at[i, "timestamp"])
            else:
                ts = str(i)

            price_level = None
            if "price_level" in df.columns and pd.notna(df.at[i, "price_level"]):
                try:
                    price_level = float(df.at[i, "price_level"])
                except Exception:
                    price_level = None

            for k, label in enumerate(labels):
                event = {
                    "index": i,
                    "timestamp": ts,
                    "pattern": label,
                    "price_level": price_level,
                    "imbalance": imb,
                    "delta": delta,
                    "dominance": (
                        "buyers"
                        if delta > 0
                        else ("sellers" if delta < 0 else "neutral")
                    ),
                    "bid_volume": bidv,
                    "ask_volume": askv,
                    "row_total": total,
                }
                if k < len(extras):
                    event.update(extras[k])
                patterns.append(event)

        except Exception as e:
            LOG.error(f"[OrderflowV5] Erreur à la ligne {i}: {e}", exc_info=False)

    # ---------- 6) MÉTRIQUES GLOBALES ----------
    buys = int((df["dominance"] == "buyers").sum())
    sells = int((df["dominance"] == "sellers").sum())
    buy_ratio = buys / max(1, buys + sells)

    if (df["total_volume"] > 0).any():
        imbalance_mean = float(
            df["ask_volume"].sum() / (df["total_volume"].sum() + 1e-9)
        )
    else:
        imbalance_mean = 0.5

    delta_total = float(df["delta"].sum())
    vol_total = float(df["total_volume"].sum())

    # ---------- 7) SCORE GLOBAL ----------
    score = 50

    # Intensité directionnelle
    if vol_total > 0:
        score += min(30, abs(delta_total) / (vol_total + 1e-6) * 100.0)  # +0..30

    # Déséquilibre moyen net
    if abs(imbalance_mean - 0.5) > 0.15:
        score += 10

    # Patterns détectés
    if len(patterns) >= 3:
        score += 10

    # Pénalités/bonus échantillon selon rows + coverage/tick_rate si dispo
    rows = len(df)
    coverage_s = None
    tick_rate  = None
    try:
        if "coverage_s" in df.columns and pd.notna(df["coverage_s"]).any():
            coverage_s = float(pd.to_numeric(df["coverage_s"], errors="coerce").iloc[-1])
        if "tick_rate" in df.columns and pd.notna(df["tick_rate"]).any():
            tick_rate = float(pd.to_numeric(df["tick_rate"], errors="coerce").iloc[-1])
    except Exception:
        pass

    # Pénalité échantillon court, adoucie si bonne couverture
    row_pen = max(0.0, (10 - rows) * 1.5) if rows < 10 else 0.0  # max ~15
    if coverage_s is not None and coverage_s >= 30.0:
        row_pen *= 0.5  # demi-pénalité si la fenêtre couvre >= 30s
    score -= row_pen

    # Très faible volume total → grosse pénalité
    if vol_total < 1e-6:
        score -= 25

    # Bonus activité si burst élevé malgré peu de lignes
    if tick_rate is not None and tick_rate >= 2.0:
        score += 5
    elif coverage_s is not None and coverage_s >= 45.0:
        score += 3

    # Prudence si volumes reconstruits (rescue_mode)
    if rescue_mode:
        # malus léger + plafonnement
        score -= 5
        score = min(score, 85)

        # adoucir la pénalité d'échantillon si <10 lignes (on rend la pénalité moitié moins sévère)
        if rows < 10:
            score += 0.5 * row_pen  # on rend une partie de la pénalité

        # bonus si signal vraiment clair malgré rescue
        if (abs(imbalance_mean - 0.5) >= 0.20) or (vol_total > 0 and abs(delta_total) >= 0.20 * vol_total):
            score += 5


    score = int(np.clip(round(score), 0, 100))
    status = "VALID" if score >= 70 else "SUSPECT"


    # ---------- 8) SORTIE ----------
    summary = {
        "delta_total": delta_total,
        "volume_total": vol_total,
        "mean_imbalance": imbalance_mean,
        "cvd_final": float(df["cvd_smoothed"].iloc[-1]) if len(df) else 0.0,
        "buy_ratio": float(buy_ratio),
        "pattern_count": len(patterns),
        "rescue": bool(rescue_mode),
        "rescue_note": rescue_note,
    }
    # Ajouts opportunistes si colonnes présentes (pour logger comme ton FOOTPRINT)
    if "coverage_s" in df.columns and pd.notna(df["coverage_s"]).any():
        summary["coverage_s"] = float(pd.to_numeric(df["coverage_s"], errors="coerce").iloc[-1])
    if "tick_rate" in df.columns and pd.notna(df["tick_rate"]).any():
        summary["tick_rate"] = float(pd.to_numeric(df["tick_rate"], errors="coerce").iloc[-1])

    # --- LOG [ORDERFLOW] ICI (à l'intérieur de la fonction) ---
    try:
        _symbol = None
        if "symbol" in df.columns and pd.notna(df["symbol"]).any():
            _symbol = str(df["symbol"].iloc[-1])
        elif hasattr(df, "attrs") and "symbol" in df.attrs:
            _symbol = str(df.attrs["symbol"])
        else:
            _symbol = "UNKNOWN"

        _cov  = f" | coverage_s={float(summary['coverage_s']):.1f}" if 'coverage_s' in summary else ""
        _rate = f" | tick_rate={float(summary['tick_rate']):.2f}"    if 'tick_rate'  in summary else ""

        LOG.info(
            f"[ORDERFLOW][{_symbol}] "
            f"Score={int(score)} | Status={status} | "
            f"Δ={float(summary['delta_total']):.2f} | "
            f"Vol={float(summary['volume_total']):.2f} | "
            f"ImbMoy={float(summary['mean_imbalance']):.2f} | "
            f"CVD={float(summary['cvd_final']):.2f} | "
            f"Patterns={int(summary['pattern_count'])} | "
            f"rescue={bool(summary['rescue'])} note={str(summary['rescue_note'])}"
            f"{_cov}{_rate}"
        )
    except Exception:
        # on ne bloque jamais la détection si le log échoue
        pass

    return {
        "score": score,
        "status": status,
        "summary": summary,
        "patterns": patterns,
        "df": df,
    }

def footprint_validator(
    candles: pd.DataFrame,
    ticks: pd.DataFrame,
    candle_index: Optional[int] = None,
    price_step: Optional[float] = None,
    imbalance_threshold: float = 0.7,
) -> Dict[str, Any]:
    """
    🏦 Footprint Validator (strict M1)
    - Fenêtre strictement [start_ts, end_ts) ; si pas de bougie suivante → end_ts = start_ts + 1min
    - price: utilise 'price' sinon 'last' → 'mid' → 'bid'/'ask'
    - size: si manquant/0 → 1.0 (tick-count proxy) — correction ligne par ligne
    - side: utilise 'side' fourni ; si 'unknown' et 'flags' dispo → decode (1/16 buy, 2/32 sell)
    - Pas de fallback temporel
    """
    import numpy as np
    import pandas as pd

    # ---------- 0) VALIDATIONS & COPIES ----------
    if candles is None or not isinstance(candles, pd.DataFrame) or candles.empty:
        return {
            "summary": {"comment": "Candles vide/invalide."},
            "score": 0,
            "status": "SUSPECT",
            "footprint_df": pd.DataFrame(),
            "candle": {},
        }
    if ticks is None or not isinstance(ticks, pd.DataFrame):
        ticks = pd.DataFrame(columns=["time", "price", "size", "side"])  # safe default

    candles = candles.copy()
    ticks = ticks.copy()

    # ---------- 1) NORMALISATION DES TEMPS (candles) ----------
    if candle_index is None:
        candle_index = len(candles) - 1
    candle_index = max(0, min(candles.index.size - 1, candle_index))

    if "time" in candles.columns:
        if not pd.api.types.is_datetime64_any_dtype(candles["time"]):
            candles["time"] = pd.to_datetime(candles["time"], errors="coerce", utc=True)
    else:
        if not isinstance(candles.index, pd.DatetimeIndex):
            try:
                candles.index = pd.to_datetime(candles.index, errors="coerce", utc=True)
            except Exception:
                pass
        candles["time"] = candles.index

    if candles["time"].isna().all():
        candles["time"] = pd.Timestamp.now(tz="UTC")

    candle = candles.iloc[candle_index]
    start_ts = pd.to_datetime(
        candle.get("time", candle.name), utc=True, errors="coerce"
    )
    if pd.isna(start_ts):
        start_ts = pd.Timestamp.now(tz="UTC")

    # fenêtre M1 stricte
    if candle_index + 1 < len(candles):
        nxt = candles.iloc[candle_index + 1]
        end_ts = pd.to_datetime(
            nxt.get("time", candles.index[candle_index + 1]), utc=True, errors="coerce"
        )
        if pd.isna(end_ts) or end_ts <= start_ts:
            end_ts = start_ts + pd.Timedelta(minutes=1)
    else:
        end_ts = start_ts + pd.Timedelta(minutes=1)

    # ---------- 2) NORMALISATION DES TICKS ----------
    # Colonnes minimales
    for col in ("time", "price", "size", "side"):
        if col not in ticks.columns:
            if col == "time":
                ticks[col] = pd.NaT
            elif col in ("price", "size"):
                ticks[col] = np.nan
            else:  # side
                ticks[col] = "unknown"

    # Time -> UTC
    if not pd.api.types.is_datetime64_any_dtype(ticks["time"]):
        ticks["time"] = pd.to_datetime(ticks["time"], errors="coerce", utc=True)
    ticks["time"] = ticks["time"].fillna(pd.Timestamp.now(tz="UTC"))

    # Prix de secours si 'price' inexploitable
    price_raw = pd.to_numeric(ticks["price"], errors="coerce")
    if price_raw.isna().all() or (price_raw.fillna(0) == 0).all():
        if (
            "last" in ticks.columns
            and not pd.to_numeric(ticks["last"], errors="coerce").isna().all()
        ):
            ticks["price"] = pd.to_numeric(ticks["last"], errors="coerce")
        elif (
            "mid" in ticks.columns
            and not pd.to_numeric(ticks["mid"], errors="coerce").isna().all()
        ):
            ticks["price"] = pd.to_numeric(ticks["mid"], errors="coerce")
        elif {"bid", "ask"}.issubset(ticks.columns):
            ticks["price"] = (
                pd.to_numeric(ticks["bid"], errors="coerce")
                + pd.to_numeric(ticks["ask"], errors="coerce")
            ) / 2.0
        elif "bid" in ticks.columns:
            ticks["price"] = pd.to_numeric(ticks["bid"], errors="coerce")
        elif "ask" in ticks.columns:
            ticks["price"] = pd.to_numeric(ticks["ask"], errors="coerce")
        else:
            ticks["price"] = 0.0

    # Normalisation 1 (pré-fallback fin)
    ticks["price"] = pd.to_numeric(ticks["price"], errors="coerce").fillna(0.0)

    # Calcule 'mid' si absent mais bid/ask présents (pour fallback)
    if "mid" not in ticks.columns and {"bid", "ask"}.issubset(ticks.columns):
        ticks["mid"] = (
            pd.to_numeric(ticks["bid"], errors="coerce")
            + pd.to_numeric(ticks["ask"], errors="coerce")
        ) / 2.0

    # Remplissage robuste des prix nuls/invalides (priorité: last > mid > bid > ask)
    zero_mask = (~np.isfinite(ticks["price"])) | (ticks["price"] <= 0)
    if zero_mask.any():
        filler = None
        for col in ("last", "mid", "bid", "ask"):
            if col in ticks.columns:
                s = pd.to_numeric(ticks[col], errors="coerce")
                # ignore valeurs non positives
                s = s.where(s > 0)
                filler = s if filler is None else filler.combine_first(s)
        if filler is not None:
            ticks.loc[zero_mask, "price"] = filler.loc[zero_mask]

    # Normalisation 2 (post-fallback) + filtre prix valides
    ticks["price"] = pd.to_numeric(ticks["price"], errors="coerce").fillna(0.0)
    valid_price_mask = np.isfinite(ticks["price"]) & (ticks["price"] > 0)
    ticks = ticks.loc[valid_price_mask].copy()
    if ticks.empty:
        return {
            "summary": {
                "comment": "Aucun tick exploitable (prix <= 0 ou NaN).",
                "window_start": pd.Timestamp(start_ts).isoformat(),
                "window_end": pd.Timestamp(end_ts).isoformat(),
            },
            "score": 0,
            "status": "SUSPECT",
            "footprint_df": pd.DataFrame(),
            "candle": candle.dropna().to_dict(),
        }

    # Taille (proxy) — corrige ligne par ligne
    if "size" not in ticks.columns:
        ticks["size"] = 1.0
    ticks["size"] = pd.to_numeric(ticks["size"], errors="coerce")
    bad_sz = ~np.isfinite(ticks["size"]) | (ticks["size"] <= 0)
    ticks.loc[bad_sz, "size"] = 1.0

    # Side + flags (évite de transformer NaN en "nan")
    ticks["side"] = ticks["side"].astype("string").str.lower()
    ticks["side"] = ticks["side"].replace({"b": "buy", "s": "sell"}).fillna("unknown")

    if "flags" in ticks.columns:
        flags = pd.to_numeric(ticks["flags"], errors="coerce").fillna(0).astype(int)
        unk_mask = ticks["side"].eq("unknown")
        if unk_mask.any():
            buy_mask = ((flags & 1) > 0) | ((flags & 16) > 0)
            sell_mask = ((flags & 2) > 0) | ((flags & 32) > 0)
            ticks.loc[unk_mask & buy_mask, "side"] = "buy"
            ticks.loc[unk_mask & sell_mask, "side"] = "sell"

    # ---------- 3) FENÊTRE STRICTE ----------
    try:
        mask = (ticks["time"] >= start_ts) & (ticks["time"] < end_ts)
        df = ticks.loc[mask].copy()
    except Exception:
        df = ticks.copy()

    if df.empty:
        return {
            "summary": {
                "comment": "Aucun tick trouvé pour la bougie (fenêtre stricte).",
                "window_start": pd.Timestamp(start_ts).isoformat(),
                "window_end": pd.Timestamp(end_ts).isoformat(),
            },
            "score": 0,
            "status": "SUSPECT",
            "footprint_df": pd.DataFrame(),
            "candle": candle.dropna().to_dict(),
        }
    # --- PATCH 2.A: granularité des ticks (nb & couverture) ---
    tick_count = int(df.shape[0])
    # sécurité: si un seul tick → couverture = 0s
    coverage_s = (
        float((df["time"].max() - df["time"].min()).total_seconds())
        if tick_count > 1
        else 0.0
    )

    # ---------- 4) AGRÉGATION & MÉTRIQUES ----------
    df["side_norm"] = df["side"].map({"buy": "buy", "sell": "sell"}).fillna("unknown")

    if price_step is None or price_step <= 0:
        uniq = np.sort(df["price"].dropna().unique())
        if uniq.size >= 2:
            diffs = np.diff(uniq)
            pos = diffs[diffs > 0]
            price_step = float(np.min(pos)) if pos.size else 1e-5
        else:
            price_step = 1e-5
    df["price_level"] = (df["price"] / price_step).round() * price_step

    agg = df.pivot_table(
        index="price_level",
        columns="side_norm",
        values="size",
        aggfunc="sum",
        fill_value=0.0,
    )
    for col in ("buy", "sell", "unknown"):
        if col not in agg.columns:
            agg[col] = 0.0

    agg["total"] = agg["buy"] + agg["sell"] + agg["unknown"]
    agg["delta"] = agg["buy"] - agg["sell"]
    denom = (agg["buy"] + agg["sell"]).replace(0.0, np.nan)
    agg["buy_pct"] = (agg["buy"] / denom).fillna(0.5)
    agg = (
        agg.reset_index()
        .sort_values("price_level", ascending=False)
        .reset_index(drop=True)
    )

    # --- PATCH: quantification propre aux ticks (arrondis stables) ---
    if price_step is None or price_step <= 0:
        # garde un fallback raisonnable si le pas a été inféré
        step_for_dec = float(
            agg["price_level"].diff().abs().replace(0, np.nan).min() or 1e-5
        )
    else:
        step_for_dec = float(price_step)

    step_str = f"{step_for_dec:.10f}".rstrip("0")
    decimals = len(step_str.split(".")[1]) if "." in step_str else 0

    # arrondis harmonisés
    agg["price_level"] = agg["price_level"].round(decimals)
    poc = (
        float(agg.loc[agg["total"].idxmax(), "price_level"])
        if (agg["total"] > 0).any()
        else float(agg.loc[0, "price_level"])
    )
    poc = round(poc, decimals)
    delta_total = float(agg["delta"].sum())
    total_volume = float(agg["total"].sum())
    imbalance_buy = int((agg["buy_pct"] >= imbalance_threshold).sum())
    imbalance_sell = int((agg["buy_pct"] <= (1.0 - imbalance_threshold)).sum())

    absorption_flag = False
    try:
        if agg.iloc[0]["delta"] < 0:
            absorption_flag = True
        if agg.iloc[-1]["delta"] > 0:
            absorption_flag = True
    except Exception:
        pass

    score = 100
    comments = []
    # --- CONFIG QUALITÉ TICKS (paramétrable) ---
    MIN_TICKS = 10  # ex. 10 ticks
    MIN_COVERAGE_S = 30.0  # ex. 30 secondes
    PEN_TICKS = 15  # -15 points si tick_count < MIN_TICKS
    PEN_COVER = 10  # -10 points si coverage_s < MIN_COVERAGE_S

    # --- PATCH 2.B: pénalités faible granularité (paramétrées) ---
    if tick_count < MIN_TICKS:
        score -= PEN_TICKS
        comments.append(f"Peu de ticks (<{MIN_TICKS}).")
        # Atténuation si burst élevé (ticks/s)
    HIGH_BURST_TICK_RATE = 2.0  # ex. ≥ 2 ticks/seconde
    if coverage_s < MIN_COVERAGE_S:
        tick_rate = tick_count / max(coverage_s, 1.0)
        if tick_count >= MIN_TICKS and tick_rate >= HIGH_BURST_TICK_RATE:
            score -= max(PEN_COVER // 2, 1)
            comments.append(
                f"Couverture courte mais burst élevé (≥{HIGH_BURST_TICK_RATE:.1f} t/s) — malus réduit."
            )
        else:
            score -= PEN_COVER
            comments.append(f"Couverture temporelle faible (<{MIN_COVERAGE_S:.0f}s).")

    # --- GARDE-FOU: échantillon trop court ---
    if tick_count < 3 or coverage_s < 2:
        score = min(score, 60)  # forcera status="SUSPECT" plus bas
        comments.append("Échantillon trop court — statut dégradé.")

    if total_volume <= 0.0:
        score -= 60
        comments.append("Volume nul/négligeable.")
    if abs(delta_total) < 0.01 * max(1.0, total_volume):
        score -= 15
        comments.append("Delta trop neutre (peu de conviction).")
    if (imbalance_buy + imbalance_sell) == 0:
        score -= 10
        comments.append("Aucun déséquilibre détecté.")
    if absorption_flag:
        score -= 20
        comments.append("Absorption détectée aux extrêmes.")

    status = "VALID" if score >= 70 else "SUSPECT"

    return {
        "summary": {
            "delta_total": delta_total,
            "total_volume": total_volume,
            "poc": poc,
            "imbalance_buy": imbalance_buy,
            "imbalance_sell": imbalance_sell,
            "absorption_flag": bool(absorption_flag),
            "comments": "; ".join(comments),
            "window_start": pd.Timestamp(start_ts).isoformat(),
            "window_end": pd.Timestamp(end_ts).isoformat(),
            "tick_count": int(tick_count),
            "coverage_s": float(coverage_s),
            "tick_rate": float(tick_count / max(coverage_s, 1.0)),  # ticks par seconde
        },
        "score": max(int(score), 0),
        "status": status,
        "footprint_df": agg,
        "candle": candle.dropna().to_dict(),
    }


class Detectors:
    """
    Classe regroupant tous les détecteurs de phases de marché.
    Chaque méthode correspond à une logique de détection spécifique.
    """

    def __init__(self, logger=None, config_manager=None):
        self.logger = logger or logging.getLogger(__name__)
        self.config_manager = config_manager

    def validate_last_candle_footprint(
        self, candles: pd.DataFrame, ticks: pd.DataFrame
    ) -> Dict[str, Any]:
        try:
            return footprint_validator(candles, ticks, candle_index=None)
        except Exception as e:
            self.logger.error(f"[FootprintValidator] erreur: {e}")
            return {
                "summary": {"comment": f"error: {e}"},
                "score": 0,
                "status": "SUSPECT",
                "footprint_df": pd.DataFrame(),
                "candle": {},
            }

    def detect_order_block_ml_enhanced(
        self, df: pd.DataFrame, df_htf: Optional[pd.DataFrame] = None
    ) -> List[Optional[Dict[str, Any]]]:
        """
        🎯 Order Blocks ML Enhanced - Scoring sophistiqué avec confluence (non bloquant)

        Features ML:
        - Impulse strength scoring
        - Volume confirmation weighting
        - Temporal context analysis
        - Multi-factor confluence scoring
        """
        self.logger.debug("Détection Order Blocks ML Enhanced...")

        if df is None or df.empty:
            return [None] * (0 if df is None else len(df))

        # Configuration ML
        ob_config = (
            self.config_manager.get(
                "phase_detection_defaults.order_block_ml_settings", {}
            )
            or {}
        )
        enable_ml = bool(ob_config.get("enable_ml_scoring", True))
        confluence_config = ob_config.get("confluence_requirements", {}) or {}
        impulse_weights = ob_config.get("impulse_strength_weights", {}) or {}

        # Paramètres de base
        impulse_threshold = float(
            self.config_manager.get(
                "phase_detection_defaults.impulse_threshold", 0.0005
            )
        )

        # Pré-calcul des features pour ML
        df = df.copy()
        df["candle_move"] = df["close"] - df["open"]
        df["candle_size"] = (df["high"] - df["low"]).replace(0, np.nan)
        df["body_ratio"] = (df["candle_move"].abs() / df["candle_size"]).fillna(0.0)

        vol_ma = (
            df["tick_volume"]
            .rolling(window=20, min_periods=1)
            .mean()
            .replace(0, np.nan)
        )
        df["volume_ma"] = vol_ma
        df["volume_ratio"] = (df["tick_volume"] / vol_ma).fillna(1.0)

        # Identification des OB potentiels
        bullish_ob_mask = (df["candle_move"] > impulse_threshold) & (
            df["candle_move"].shift(1) < 0
        )
        bearish_ob_mask = (df["candle_move"] < -impulse_threshold) & (
            df["candle_move"].shift(1) > 0
        )
        potential_ob_mask = bullish_ob_mask | bearish_ob_mask

        # Swing points pour confluence
        swing_highs, swing_lows = _get_swing_points(self, df)

        # Trend HTF si disponible
        htf_trend = None
        if df_htf is not None and not df_htf.empty:
            try:
                htf_trend = _get_trend(self, df_htf).iloc[-1]
            except Exception:
                htf_trend = None

        results: List[Optional[Dict[str, Any]]] = [None] * len(df)
        ob_positions = df.index[potential_ob_mask].tolist()

        for ob_timestamp in ob_positions:
            try:
                pos = int(df.index.get_loc(ob_timestamp))
                if pos <= 0:
                    continue

                ob_candle_pos = pos - 1
                if ob_candle_pos < 0 or pos >= len(df):
                    continue

                ob_candle = df.iloc[ob_candle_pos]
                impulse_candle = df.iloc[pos]
                ob_zone = (float(ob_candle["low"]), float(ob_candle["high"]))

                # === ML FEATURE EXTRACTION ===

                # 1. Impulse Strength Score
                price_movement_strength = float(
                    abs(impulse_candle["candle_move"]) / max(impulse_threshold, 1e-12)
                )
                volume_spike_strength = float(impulse_candle["volume_ratio"])
                body_ratio_strength = float(impulse_candle["body_ratio"])

                # Time compression (placeholder)
                time_compression = 1.0

                # Calcul score impulse pondéré
                impulse_score = (
                    price_movement_strength
                    * float(impulse_weights.get("price_movement", 0.4))
                    + volume_spike_strength
                    * float(impulse_weights.get("volume_spike", 0.3))
                    + time_compression
                    * float(impulse_weights.get("time_compression", 0.3))
                )

                # 2. Confluence Factors Scoring
                confluence_score = 0.0
                confluence_details: Dict[str, Any] = {}

                # FVG Confluence
                fvg_confluence = bool(pd.notna(impulse_candle.get("fvg_details")))
                if fvg_confluence:
                    confluence_score += 0.25
                confluence_details["fvg_confluence"] = fvg_confluence

                # Market Extreme Confluence (Swing points)
                extreme_confluence = bool(
                    (ob_candle.name in swing_highs.index)
                    or (ob_candle.name in swing_lows.index)
                )
                if extreme_confluence:
                    confluence_score += 0.30
                confluence_details["extreme_confluence"] = extreme_confluence

                # Volume Confirmation
                volume_confirmation = True
                if confluence_config.get("require_volume_confirmation", True):
                    volume_confirmation = (
                        volume_spike_strength > 1.2
                    )  # 20% au-dessus de la moyenne
                    if volume_confirmation:
                        confluence_score += 0.20
                confluence_details["volume_confirmation"] = volume_confirmation

                # Trend Alignment
                ob_is_bullish = bool(bullish_ob_mask.iloc[pos])
                trend_alignment = True
                if confluence_config.get("require_trend_alignment", True):
                    current_trend = (
                        df["trend"].iloc[pos] if "trend" in df.columns else "neutral"
                    )
                    if (ob_is_bullish and current_trend == "bullish") or (
                        (not ob_is_bullish) and current_trend == "bearish"
                    ):
                        trend_alignment = True
                        confluence_score += 0.15
                    else:
                        trend_alignment = False
                    # HTF alignment bonus
                    if htf_trend and (
                        (ob_is_bullish and htf_trend == "bullish")
                        or ((not ob_is_bullish) and htf_trend == "bearish")
                    ):
                        confluence_score += 0.10
                confluence_details["trend_alignment"] = trend_alignment

                # 3. Mitigation Analysis
                unmitigated = True
                future_candles = df.iloc[pos + 1 :]
                if not future_candles.empty:
                    mitigated = future_candles[
                        (future_candles["high"] >= ob_zone[0])
                        & (future_candles["low"] <= ob_zone[1])
                    ]
                    if not mitigated.empty:
                        unmitigated = False

                # 4. ML Score Final
                if enable_ml:
                    base_ml_score = min(1.0, (impulse_score + confluence_score) / 2.0)
                    if unmitigated:
                        base_ml_score *= 1.1
                    if volume_confirmation and trend_alignment:
                        base_ml_score *= 1.15
                    ml_score = min(0.95, base_ml_score)  # Cap à 95%
                else:
                    ml_score = float(confluence_score)

                # 5. Filtrage par seuil de confluence
                min_confluence = float(
                    confluence_config.get("min_confluence_score", 0.6)
                )

                if ml_score >= min_confluence:
                    results[pos] = {
                        "type": "bullish" if ob_is_bullish else "bearish",
                        "zone": [ob_zone[0], ob_zone[1]],
                        "ml_score": round(ml_score, 3),
                        "impulse_strength": round(impulse_score, 3),
                        "confluence_score": round(confluence_score, 3),
                        "confluence_details": confluence_details,
                        "unmitigated": bool(unmitigated),
                        "volume_spike": round(volume_spike_strength, 2),
                        "formation_quality": (
                            "high"
                            if ml_score > 0.8
                            else "medium" if ml_score > 0.6 else "low"
                        ),
                    }

            except Exception as e:
                self.logger.warning(
                    f"Erreur processing OB à l'index {ob_timestamp}: {e}",
                    exc_info=False,
                )
                continue

        # Performance logging
        valid_obs = [r for r in results if r is not None]
        if valid_obs:
            avg_ml_score = float(np.mean([ob["ml_score"] for ob in valid_obs]))
            high_quality = len(
                [ob for ob in valid_obs if ob["formation_quality"] == "high"]
            )
            self.logger.debug(
                f"OB ML Enhanced: {len(valid_obs)} OB détectés, score ML moyen: {avg_ml_score:.3f}, haute qualité: {high_quality}"
            )

        return results

    def detect_fvg_enhanced(self, df: pd.DataFrame) -> List[Optional[Dict[str, Any]]]:
        """
        🚀 FVG Enhanced - Version Trading Desk avec magnitude et tracking

        Améliorations:
        - Filtrage par magnitude minimale (en % du prix)
        - Tracking du remplissage en temps réel
        - Scoring de qualité du gap
        - Expiration basée sur l'âge
        """
        self.logger.debug("Détection FVG Enhanced avec magnitude et tracking...")

        if df is None or df.empty:
            return []

        # Récupération des paramètres enhanced
        fvg_config = (
            self.config_manager.get(
                "phase_detection_defaults.fvg_enhanced_settings", {}
            )
            or {}
        )
        min_gap_magnitude = (
            float(fvg_config.get("min_gap_magnitude_percent", 0.15)) / 100.0
        )
        gap_fill_threshold = float(fvg_config.get("gap_fill_threshold", 0.8))
        enable_tracking = bool(fvg_config.get("enable_gap_tracking", True))
        max_gap_age = int(fvg_config.get("max_gap_age_bars", 50))

        # Détection vectorielle de base (optimisée)
        low_p0 = df["low"].to_numpy()
        high_p2 = df["high"].shift(2).to_numpy()
        high_p0 = df["high"].to_numpy()
        low_p2 = df["low"].shift(2).to_numpy()

        valid_indices = ~(np.isnan(high_p2) | np.isnan(low_p2))

        bullish_fvg_condition = np.zeros(len(df), dtype=bool)
        bearish_fvg_condition = np.zeros(len(df), dtype=bool)

        bullish_fvg_condition[valid_indices] = (
            low_p0[valid_indices] > high_p2[valid_indices]
        )
        bearish_fvg_condition[valid_indices] = (
            high_p0[valid_indices] < low_p2[valid_indices]
        )

        results: List[Optional[Dict[str, Any]]] = []
        active_gaps: List[Dict[str, Any]] = (
            []
        )  # Tracking des gaps actifs pour remplissage

        for i in range(len(df)):
            fvg_info = None
            current_price = float(df["close"].iloc[i])

            # === DÉTECTION NOUVEAUX FVG ===
            if bullish_fvg_condition[i] and i >= 2:
                gap_bottom = float(df["high"].iloc[i - 2])
                gap_top = float(df["low"].iloc[i])
                gap_size = gap_top - gap_bottom

                magnitude_percent = gap_size / max(current_price, 1e-12)
                if magnitude_percent >= min_gap_magnitude:
                    quality_score = min(
                        1.0, magnitude_percent / max(min_gap_magnitude * 2.0, 1e-12)
                    )
                    fvg_info = {
                        "type": "bullish",
                        "top": gap_top,
                        "bottom": gap_bottom,
                        "magnitude": gap_size,
                        "magnitude_percent": magnitude_percent,
                        "quality_score": quality_score,
                        "formation_index": i,
                        "is_filled": False,
                        "fill_percentage": 0.0,
                    }
                    if enable_tracking:
                        active_gaps.append(fvg_info.copy())

            elif bearish_fvg_condition[i] and i >= 2:
                gap_top = float(df["low"].iloc[i - 2])
                gap_bottom = float(df["high"].iloc[i])
                gap_size = gap_top - gap_bottom

                magnitude_percent = gap_size / max(current_price, 1e-12)
                if magnitude_percent >= min_gap_magnitude:
                    quality_score = min(
                        1.0, magnitude_percent / max(min_gap_magnitude * 2.0, 1e-12)
                    )
                    fvg_info = {
                        "type": "bearish",
                        "top": gap_top,
                        "bottom": gap_bottom,
                        "magnitude": gap_size,
                        "magnitude_percent": magnitude_percent,
                        "quality_score": quality_score,
                        "formation_index": i,
                        "is_filled": False,
                        "fill_percentage": 0.0,
                    }
                    if enable_tracking:
                        active_gaps.append(fvg_info.copy())

            # === TRACKING REMPLISSAGE DES GAPS ACTIFS ===
            if enable_tracking and active_gaps:
                current_high = float(df["high"].iloc[i])
                current_low = float(df["low"].iloc[i])

                for gap in active_gaps[:]:  # copie pour suppression in-loop
                    age = i - int(gap["formation_index"])

                    # Expiration par âge
                    if age > max_gap_age:
                        active_gaps.remove(gap)
                        continue

                    if gap["type"] == "bullish":
                        if current_low <= gap["top"]:
                            penetration = gap["top"] - current_low
                            fill_percent = penetration / max(gap["magnitude"], 1e-12)
                            gap["fill_percentage"] = min(1.0, fill_percent)
                            if fill_percent >= gap_fill_threshold:
                                gap["is_filled"] = True
                                active_gaps.remove(gap)

                    else:  # bearish
                        if current_high >= gap["bottom"]:
                            penetration = current_high - gap["bottom"]
                            fill_percent = penetration / max(gap["magnitude"], 1e-12)
                            gap["fill_percentage"] = min(1.0, fill_percent)
                            if fill_percent >= gap_fill_threshold:
                                gap["is_filled"] = True
                                active_gaps.remove(gap)

            results.append(fvg_info)

        # Log de performance
        valid_gaps = [r for r in results if r is not None]
        if valid_gaps:
            avg_quality = float(np.mean([g["quality_score"] for g in valid_gaps]))
            self.logger.debug(
                f"FVG Enhanced: {len(valid_gaps)} gaps détectés, qualité moyenne: {avg_quality:.3f}"
            )

        return results

    def detect_bos_mss_enhanced(
        self, df: pd.DataFrame
    ) -> List[Optional[Dict[str, Any]]]:
        """
        🎯 BOS/MSS Enhanced - Avec confirmation volume et momentum (version vectorisée, sans .apply)

        Améliorations:
        - Vectorisation complète des validations (volume, momentum, distance de break) → perf M1+++
        - Confirmation volume obligatoire (configurable)
        - Validation momentum (configurable)
        - Distinction BOS vs MSS plus précise via la tendance précédente
        - Filtrage des faux breakouts par distance minimale relative
        - Respect de 'require_close_beyond' (clôture au-delà du niveau)
        """

        self.logger.debug(
            "Détection BOS/MSS Enhanced (vectorisée) avec confirmations..."
        )

        # === GUARDRAILS ===
        if df is None or df.empty:
            return []

        # --- Config ---
        bos_config = (
            self.config_manager.get(
                "phase_detection_defaults.bos_mss_enhanced_settings", {}
            )
            or {}
        )
        volume_config = bos_config.get("volume_confirmation", {}) or {}
        momentum_config = bos_config.get("momentum_confirmation", {}) or {}
        structure_config = bos_config.get("structure_validation", {}) or {}

        enable_volume_conf = bool(volume_config.get("enable", True))
        volume_multiplier = float(volume_config.get("volume_multiplier_threshold", 1.5))
        volume_lookback = int(volume_config.get("lookback_period", 20))

        enable_momentum_conf = bool(momentum_config.get("enable", True))
        min_momentum = float(momentum_config.get("min_momentum_threshold", 0.0003))

        min_break_distance = float(structure_config.get("min_break_distance", 0.0002))
        require_close_beyond = bool(structure_config.get("require_close_beyond", True))

        # === Préparation colonnes requises ===
        df = df.copy()

        # Tendance si absente
        if "trend" not in df.columns:
            df["trend"] = _get_trend(self, df)

        # Swing points adaptatifs (séries alignées)
        df["last_swing_high"] = df["high"].shift(1).ffill()
        df["last_swing_low"] = df["low"].shift(1).ffill()

        # Sanitisation prix/volume
        for col in ("close", "high", "low"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce").astype(float)
        if "tick_volume" not in df.columns:
            df["tick_volume"] = 0.0
        df["tick_volume"] = (
            pd.to_numeric(df["tick_volume"], errors="coerce").astype(float).fillna(0.0)
        )

        # Moyenne mobile volume + ratio (vectorisé)
        vol_ma = (
            df["tick_volume"]
            .rolling(window=max(1, volume_lookback), min_periods=1)
            .mean()
            .replace(0, np.nan)
        )
        df["volume_ma"] = vol_ma
        df["volume_ratio"] = (
            (df["tick_volume"] / vol_ma).replace([np.inf, -np.inf], np.nan).fillna(0.0)
        )

        # === Conditions de base: break au-delà du dernier swing (décalé) ===
        # on compare la close courante au swing de la barre précédente
        last_high_shift = df["last_swing_high"].shift(1)
        last_low_shift = df["last_swing_low"].shift(1)

        # close au-delà du niveau (strictement) ou non (si require_close_beyond=False, on tolère >= / <=)
        if require_close_beyond:
            bullish_break_basic = df["close"] > last_high_shift
            bearish_break_basic = df["close"] < last_low_shift
        else:
            bullish_break_basic = df["close"] >= last_high_shift
            bearish_break_basic = df["close"] <= last_low_shift

        # === Confirmations Volume (vectorisé) ===
        if enable_volume_conf:
            volume_confirmation = df["volume_ratio"] > volume_multiplier
        else:
            volume_confirmation = pd.Series(True, index=df.index)

        # === Confirmation Momentum (vectorisé) ===
        # momentum en valeur absolue de la variation relative (pct_change) pour robustesse
        if enable_momentum_conf:
            price_change_abs = df["close"].pct_change().abs()
            momentum_confirmation = price_change_abs > min_momentum
        else:
            price_change_abs = df["close"].pct_change().abs()  # utile pour logs
            momentum_confirmation = pd.Series(True, index=df.index)

        # === Filtrage distance minimale (vectorisé) ===
        # distance relative à partir du niveau cassé (sécurisé avec epsilon)
        eps = 1e-12
        # bullish: (close - last_high) / last_high >= min_break_distance
        # bearish: (last_low - close) / last_low >= min_break_distance
        denom_high = np.maximum(last_high_shift.astype(float), eps)
        denom_low = np.maximum(last_low_shift.astype(float), eps)

        bullish_dist_ok = ((df["close"] - last_high_shift) / denom_high) >= float(
            min_break_distance
        )
        bearish_dist_ok = ((last_low_shift - df["close"]) / denom_low) >= float(
            min_break_distance
        )

        # === Masques finaux break confirmés ===
        bullish_break_confirmed = (
            bullish_break_basic
            & volume_confirmation
            & momentum_confirmation
            & bullish_dist_ok
        )
        bearish_break_confirmed = (
            bearish_break_basic
            & volume_confirmation
            & momentum_confirmation
            & bearish_dist_ok
        )

        # === Classification BOS vs MSS (via tendance précédente) ===
        previous_trend = df["trend"].shift(1).astype(str).str.lower()
        bullish_bos = (previous_trend == "bullish") & bullish_break_confirmed
        bearish_bos = (previous_trend == "bearish") & bearish_break_confirmed
        bullish_mss = (previous_trend == "bearish") & bullish_break_confirmed
        bearish_mss = (previous_trend == "bullish") & bearish_break_confirmed

        # === Construction des résultats (liste alignée sur df) ===
        results: List[Optional[Dict[str, Any]]] = []
        vol_ratio_arr = df["volume_ratio"].to_numpy()
        price_change_arr = price_change_abs.to_numpy()

        # niveaux cassés pour logs
        level_broken_high = last_high_shift.to_numpy(dtype=float)
        level_broken_low = last_low_shift.to_numpy(dtype=float)

        # helper qualité
        def _quality_from_volratio(vr: float) -> str:
            try:
                return "high" if vr > (volume_multiplier * 1.5) else "medium"
            except Exception:
                return "medium"

        # vector -> liste d'infos
        bbos = bullish_bos.to_numpy(dtype=bool)
        bbss = bearish_bos.to_numpy(dtype=bool)
        bmss = bullish_mss.to_numpy(dtype=bool)
        bmss_bear = bearish_mss.to_numpy(dtype=bool)

        for i in range(len(df)):
            info = None
            vr_i = float(vol_ratio_arr[i]) if np.isfinite(vol_ratio_arr[i]) else 0.0
            mom_i = (
                float(price_change_arr[i]) if np.isfinite(price_change_arr[i]) else 0.0
            )

            if bbos[i]:
                lvl = (
                    float(level_broken_high[i])
                    if np.isfinite(level_broken_high[i])
                    else np.nan
                )
                info = {
                    "type": "bullish_bos",
                    "level_broken": lvl,
                    "confirmation_score": (
                        min(1.0, vr_i / max(volume_multiplier, eps))
                        if enable_volume_conf
                        else 1.0
                    ),
                    "volume_ratio": round(vr_i, 3),
                    "momentum": round(mom_i, 6),
                    "structure_type": "continuation",
                    "quality": _quality_from_volratio(vr_i),
                }
            elif bbss[i]:
                lvl = (
                    float(level_broken_low[i])
                    if np.isfinite(level_broken_low[i])
                    else np.nan
                )
                info = {
                    "type": "bearish_bos",
                    "level_broken": lvl,
                    "confirmation_score": (
                        min(1.0, vr_i / max(volume_multiplier, eps))
                        if enable_volume_conf
                        else 1.0
                    ),
                    "volume_ratio": round(vr_i, 3),
                    "momentum": round(mom_i, 6),
                    "structure_type": "continuation",
                    "quality": _quality_from_volratio(vr_i),
                }
            elif bmss[i]:
                lvl = (
                    float(level_broken_high[i])
                    if np.isfinite(level_broken_high[i])
                    else np.nan
                )
                info = {
                    "type": "bullish_mss",
                    "level_broken": lvl,
                    "confirmation_score": (
                        min(1.0, vr_i / max(volume_multiplier, eps))
                        if enable_volume_conf
                        else 1.0
                    ),
                    "volume_ratio": round(vr_i, 3),
                    "momentum": round(mom_i, 6),
                    "structure_type": "reversal",
                    "quality": _quality_from_volratio(vr_i),
                }
            elif bmss_bear[i]:
                lvl = (
                    float(level_broken_low[i])
                    if np.isfinite(level_broken_low[i])
                    else np.nan
                )
                info = {
                    "type": "bearish_mss",
                    "level_broken": lvl,
                    "confirmation_score": (
                        min(1.0, vr_i / max(volume_multiplier, eps))
                        if enable_volume_conf
                        else 1.0
                    ),
                    "volume_ratio": round(vr_i, 3),
                    "momentum": round(mom_i, 6),
                    "structure_type": "reversal",
                    "quality": _quality_from_volratio(vr_i),
                }

            results.append(info)

        # --- Logging synthétique ---
        valid_breaks = [r for r in results if r is not None]
        if valid_breaks:
            bos_count = sum(1 for r in valid_breaks if "bos" in r["type"])
            mss_count = sum(1 for r in valid_breaks if "mss" in r["type"])
            high_quality = sum(1 for r in valid_breaks if r.get("quality") == "high")
            self.logger.debug(
                f"[BOS/MSS vX] breaks={len(valid_breaks)} (BOS={bos_count}, MSS={mss_count}, highQ={high_quality}) | "
                f"vol_thr={volume_multiplier} lookback={volume_lookback} dist_min={min_break_distance} "
                f"require_close_beyond={require_close_beyond}"
            )

        return results

    def detect_liquidity_sweeps(
        self, df: pd.DataFrame, sweep_config: Optional[Dict[str, Any]] = None
    ) -> List[Optional[Dict[str, Any]]]:
        """
        💧 Détection Liquidity Sweeps (stop hunts institutionnels).
        Retourne une liste alignée sur df avec détails ou None.
        """

        if df is None or df.empty:
            return []

        cfg = sweep_config or {}
        lookback = int(cfg.get("sweep", {}).get("lookback_bars", 20))
        wick_min = float(cfg.get("sweep", {}).get("wick_to_body_min_ratio", 1.5))
        min_dist = float(cfg.get("sweep", {}).get("min_distance_pips", 3.0))
        vol_sigma = float(cfg.get("sweep", {}).get("volume_spike_sigma", 1.5))

        eps = 1e-12
        pip_size = None
        try:
            point_val = float(df["point"].iloc[-1])
            pip_size = point_val * 10.0 if point_val > 0 else None
        except Exception:
            pip_size = None
        pip_size = pip_size or 1.0

        oc_max = df[["open", "close"]].max(axis=1)
        oc_min = df[["open", "close"]].min(axis=1)
        up_wick = (df["high"] - oc_max).clip(lower=0.0)
        dn_wick = (oc_min - df["low"]).clip(lower=0.0)
        body = (df["close"] - df["open"]).abs().replace(0, eps)

        up_wr = up_wick / body
        dn_wr = dn_wick / body

        hh_prev = df["high"].rolling(lookback).max().shift(1)
        ll_prev = df["low"].rolling(lookback).min().shift(1)

        dist_up = ((df["high"] - hh_prev).clip(lower=0.0)) / pip_size
        dist_dn = ((ll_prev - df["low"]).clip(lower=0.0)) / pip_size

        vol_z = pd.to_numeric(df.get("volume_zscore", 0.0), errors="coerce").fillna(0.0)

        sweep_up = (
            (df["high"] > hh_prev)
            & (up_wr >= wick_min)
            & (dist_up >= min_dist)
            & (vol_z >= vol_sigma)
        )
        sweep_dn = (
            (df["low"] < ll_prev)
            & (dn_wr >= wick_min)
            & (dist_dn >= min_dist)
            & (vol_z >= vol_sigma)
        )

        results: List[Optional[Dict[str, Any]]] = []
        for i in range(len(df)):
            info = None
            if sweep_up.iloc[i] or sweep_dn.iloc[i]:
                info = {
                    "index": i,
                    "timestamp": str(df.index[i]),
                    "side": "sell" if sweep_up.iloc[i] else "buy",
                    "wick_ratio": float(
                        up_wr.iloc[i] if sweep_up.iloc[i] else dn_wr.iloc[i]
                    ),
                    "dist_pips": float(
                        dist_up.iloc[i] if sweep_up.iloc[i] else dist_dn.iloc[i]
                    ),
                    "volume_z": float(vol_z.iloc[i]),
                    "present": True,
                }
            results.append(info)
        return results

    def detect_absorption(
        self, df: pd.DataFrame, abs_config: Optional[Dict[str, Any]] = None
    ) -> List[Optional[Dict[str, Any]]]:
        """
        🛡️ Détection Absorption institutionnelle après sweep.
        Retourne une liste alignée sur df avec détails ou None.
        """

        if df is None or df.empty:
            return []

        cfg = abs_config or {}
        body_min = float(cfg.get("body_to_range_min", 0.5))
        closes_mid = bool(cfg.get("closes_through_mid_of_sweep", True))
        eps = 1e-12

        body = (df["close"] - df["open"]).abs()
        full_range = (df["high"] - df["low"]).replace(0, eps)
        body_ratio = body / full_range
        mid_range = (df["high"] + df["low"]) / 2.0

        absorb_up = (
            (df["close"] < df["open"])
            & (body_ratio >= body_min)
            & ((not closes_mid) | (df["close"] <= mid_range))
        )
        absorb_dn = (
            (df["close"] > df["open"])
            & (body_ratio >= body_min)
            & ((not closes_mid) | (df["close"] >= mid_range))
        )

        results: List[Optional[Dict[str, Any]]] = []
        for i in range(len(df)):
            info = None
            if absorb_up.iloc[i] or absorb_dn.iloc[i]:
                info = {
                    "index": i,
                    "timestamp": str(df.index[i]),
                    "confirmed": True,
                    "side": "buy" if absorb_dn.iloc[i] else "sell",
                    "body_ratio": float(body_ratio.iloc[i]),
                }
            results.append(info)
        return results

    def detect_eqh_eql(
        self, df: pd.DataFrame, eqh_config: Optional[Dict[str, Any]] = None
    ) -> List[Optional[Dict[str, Any]]]:
        """
        🎯 Détection Equal Highs / Equal Lows (EQH/EQL).
        Retourne une liste alignée sur df (longueur = len(df)).
        Améliorations :
        - Alignement garanti avec df.index
        - Qualité ajoutée (low / medium / high)
        - Tolérance dynamique en pips
        """

        if df is None or len(df) < 5:
            return [None] * (len(df) if df is not None else 0)

        cfg = eqh_config or {}
        tolerance_pips = float(cfg.get("tolerance_pips", 2.0))
        min_touches = int(cfg.get("min_touches", 2))

        # Détermination taille pip
        try:
            point_val = float(df["point"].iloc[-1])
            pip_size = point_val * 10.0 if point_val > 0 else 1.0
        except Exception:
            pip_size = 1.0

        highs = df["high"].round(5)
        lows = df["low"].round(5)

        results: List[Optional[Dict[str, Any]]] = [None] * len(df)

        for i in range(min_touches - 1, len(df)):
            info = None

            # Equal Highs
            recent_highs = highs.iloc[i - min_touches + 1 : i + 1]
            if recent_highs.max() - recent_highs.min() <= tolerance_pips * pip_size:
                info = {
                    "index": int(i),
                    "timestamp": str(df.index[i]),
                    "type": "eqh",
                    "level": float(recent_highs.mean()),
                    "touches": len(recent_highs),
                    "quality": (
                        "high" if len(recent_highs) >= min_touches + 1 else "medium"
                    ),
                }

            # Equal Lows
            recent_lows = lows.iloc[i - min_touches + 1 : i + 1]
            if recent_lows.max() - recent_lows.min() <= tolerance_pips * pip_size:
                info = {
                    "index": int(i),
                    "timestamp": str(df.index[i]),
                    "type": "eql",
                    "level": float(recent_lows.mean()),
                    "touches": len(recent_lows),
                    "quality": (
                        "high" if len(recent_lows) >= min_touches + 1 else "medium"
                    ),
                }

            results[i] = info

        return results

    def detect_market_regime(self, df: pd.DataFrame) -> pd.Series:
        """
        🏛️ Market Regime Detection - Version améliorée avec mémoire de phase.

        Régimes détectés:
        - trending_institutional_bull/bear | trending_retail_bull/bear
        - range_accumulation/distribution | range_institutional | range_retail
        - high_volatility_chaos | low_volatility_compression | transitional

        Changements :
        - Conserve la dernière phase si les signaux actuels sont ambigus
        - Ne tombe pas dans "unknown" sauf données invalides
        - Le changement de phase n'est validé que si les signaux dépassent un seuil de clarté
        """

        self.logger.debug("Détection du régime de marché sophistiquée...")

        if df is None or df.empty:
            self.logger.warning("DataFrame vide, impossible de détecter un régime.")
            return pd.Series(dtype=object)

        # Charger config
        regime_config = (
            self.config_manager.get(
                "phase_detection_defaults.regime_detection_settings", {}
            )
            or {}
        )
        adx_config = regime_config.get("adx_settings", {}) or {}
        vol_config = regime_config.get("volatility_regimes", {}) or {}
        volume_config = regime_config.get("volume_profile", {}) or {}

        # === 1. CALCUL ADX ===
        adx_period = int(adx_config.get("period", 14))
        trending_threshold = float(adx_config.get("trending_threshold", 25))
        ranging_threshold = float(adx_config.get("ranging_threshold", 20))

        def calculate_adx(_df: pd.DataFrame, period: int = 14):
            high, low, close = _df["high"], _df["low"], _df["close"]
            tr1 = high - low
            tr2 = (high - close.shift()).abs()
            tr3 = (low - close.shift()).abs()
            tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

            up_move, down_move = high.diff(), -low.diff()
            dm_plus = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
            dm_minus = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

            atr = tr.rolling(window=period, min_periods=1).mean().replace(0, np.nan)
            dm_plus_smooth = (
                pd.Series(dm_plus, index=_df.index)
                .rolling(window=period, min_periods=1)
                .mean()
            )
            dm_minus_smooth = (
                pd.Series(dm_minus, index=_df.index)
                .rolling(window=period, min_periods=1)
                .mean()
            )

            di_plus = 100.0 * dm_plus_smooth / atr
            di_minus = 100.0 * dm_minus_smooth / atr

            denom = (di_plus + di_minus).replace(0, np.nan)
            dx = (100.0 * (di_plus - di_minus).abs() / denom).fillna(0.0)
            adx = dx.rolling(window=period, min_periods=1).mean()
            return adx, di_plus.fillna(0.0), di_minus.fillna(0.0)

        adx, di_plus, di_minus = calculate_adx(df, adx_period)

        # === 2. VOLATILITÉ GARMAN-KLASS ===
        vol_period = int(vol_config.get("calculation_period", 20))
        high_vol_percentile = float(vol_config.get("high_vol_percentile", 75))
        low_vol_percentile = float(vol_config.get("low_vol_percentile", 25))

        ln_high_low = np.log((df["high"] / df["low"]).clip(lower=1e-12))
        ln_close_open = np.log((df["close"] / df["open"]).clip(lower=1e-12))
        gk_vol = 0.5 * ln_high_low**2 - (2 * np.log(2) - 1) * ln_close_open**2
        volatility = np.sqrt(gk_vol.rolling(window=vol_period, min_periods=1).mean())

        vol_percentiles = volatility.rolling(
            window=max(100, vol_period * 5), min_periods=1
        ).apply(lambda x: (x <= x.iloc[-1]).mean() * 100.0, raw=False)

        # === 3. VOLUME PROFILE ===
        enable_institutional = bool(
            volume_config.get("enable_institutional_detection", True)
        )
        volume_ma_period = int(volume_config.get("volume_ma_period", 20))
        institutional_threshold = float(
            volume_config.get("institutional_threshold", 1.8)
        )

        institutional_activity = pd.Series(False, index=df.index)
        if enable_institutional and "tick_volume" in df.columns:
            volume_ma = (
                df["tick_volume"]
                .rolling(window=volume_ma_period, min_periods=1)
                .mean()
                .replace(0, np.nan)
            )
            volume_ratio = (df["tick_volume"] / volume_ma).fillna(0.0)
            institutional_activity = volume_ratio > institutional_threshold

        # === 4. DÉTERMINATION DU RÉGIME ===
        regimes = pd.Series("unknown", index=df.index, dtype=object)

        for i in range(len(df)):
            current_adx = float(adx.iloc[i]) if pd.notna(adx.iloc[i]) else 0.0
            current_di_plus = (
                float(di_plus.iloc[i]) if pd.notna(di_plus.iloc[i]) else 0.0
            )
            current_di_minus = (
                float(di_minus.iloc[i]) if pd.notna(di_minus.iloc[i]) else 0.0
            )
            current_vol_percentile = (
                float(vol_percentiles.iloc[i])
                if pd.notna(vol_percentiles.iloc[i])
                else 50.0
            )
            is_institutional = bool(institutional_activity.iloc[i])

            # --- Phase trending
            if current_adx > trending_threshold:
                if current_di_plus > current_di_minus:
                    regimes.iloc[i] = (
                        "trending_institutional_bull"
                        if is_institutional
                        else "trending_retail_bull"
                    )
                else:
                    regimes.iloc[i] = (
                        "trending_institutional_bear"
                        if is_institutional
                        else "trending_retail_bear"
                    )

            # --- Phase range
            elif current_adx < ranging_threshold:
                if is_institutional:
                    recent_closes = df["close"].iloc[max(0, i - 10) : i + 1]
                    if len(recent_closes) > 5:
                        regimes.iloc[i] = (
                            "range_accumulation"
                            if (recent_closes.iloc[-1] > recent_closes.mean())
                            else "range_distribution"
                        )
                    else:
                        regimes.iloc[i] = "range_institutional"
                else:
                    regimes.iloc[i] = "range_retail"

            # --- Volatilité / Transition
            else:
                if current_vol_percentile >= high_vol_percentile:
                    regimes.iloc[i] = "high_volatility_chaos"
                elif current_vol_percentile <= low_vol_percentile:
                    regimes.iloc[i] = "low_volatility_compression"
                else:
                    regimes.iloc[i] = "transitional"

            # --- AMÉLIORATION : conserver la phase précédente si ambigu
            if regimes.iloc[i] == "unknown":
                if hasattr(self, "_last_regime") and self._last_regime:
                    regimes.iloc[i] = self._last_regime
                    self.logger.debug(
                        f"Ambigu → on conserve l'ancien régime: {self._last_regime}"
                    )

            # Mettre à jour la mémoire
            self._last_regime = regimes.iloc[i]

        # === 5. QUALITÉ DU RÉGIME ===
        def calculate_regime_strength(
            regime_series: pd.Series, adx_series: pd.Series
        ) -> pd.Series:
            strength = pd.Series(0.5, index=regime_series.index, dtype=float)
            for i in range(len(regime_series)):
                regime, adx_val = str(regime_series.iloc[i]), (
                    float(adx_series.iloc[i]) if pd.notna(adx_series.iloc[i]) else 0.0
                )
                if "trending" in regime:
                    if adx_val > 40:
                        strength.iloc[i] = 0.9
                    elif adx_val > 30:
                        strength.iloc[i] = 0.8
                    elif adx_val > 25:
                        strength.iloc[i] = 0.7
                    else:
                        strength.iloc[i] = 0.6
                elif "range" in regime:
                    if adx_val < 15:
                        strength.iloc[i] = 0.9
                    elif adx_val < 20:
                        strength.iloc[i] = 0.8
                    else:
                        strength.iloc[i] = 0.6
                elif "volatility" in regime:
                    strength.iloc[i] = 0.8
            return strength

        regime_strength = calculate_regime_strength(regimes, adx)

        # Ajouter au DF
        df["regime"] = regimes
        df["regime_strength"] = regime_strength
        df["adx"] = adx
        df["volatility_percentile"] = vol_percentiles
        df["institutional_activity"] = institutional_activity

        # === 6. LOGGING ===
        if len(regimes) > 0:
            dominant_regime = regimes.value_counts().idxmax()
            avg_strength = float(regime_strength.mean())
            self.logger.info(
                f"📊 Régime dominant: {dominant_regime} | force moyenne: {avg_strength:.2f}"
            )
            self.logger.debug(
                f"Distribution régimes: {dict(regimes.value_counts().head(3))}"
            )

        return regimes

    def detect_micro_phase_m1(
        self, df_m1: pd.DataFrame, params: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Détecteur de micro-phase M1 (spécial Burst Scalping).
        - Détecte une compression suivie d'une impulsion directionnelle.
        - Retourne un signal utilisable pour déclencher un burst.
        """
        try:
            if df_m1 is None or len(df_m1) < 60:
                return {
                    "burst_signal": False,
                    "burst_side": "NEUTRAL",
                    "burst_strength": 0.0,
                    "suggested_burst_size": 0,
                    "sl_pips_suggestion": None,
                    "tp_pips_suggestion": None,
                    "diagnostics": {"reason": "not_enough_bars"},
                }

            p = params or {}
            # Fenêtres d’analyse
            w_core = int(p.get("window_core", 20))  # cœur de compression
            w_env = int(p.get("window_env", 60))  # environnement
            k_range = float(p.get("max_range_pips", 8.0))
            k_imp = float(p.get("min_impulse_pips", 3.0))

            # Paramètres burst
            boost = float(p.get("confidence_boost", 0.1))
            burst_base = int(p.get("burst_base_size", 3))  # taille par défaut du burst
            burst_max = int(p.get("burst_max_size", 10))

            sl_floor = float(p.get("sl_min_pips", 5.0))
            sl_cap = float(p.get("sl_max_pips", 12.0))
            tp_sl_ratio = float(p.get("tp_over_sl", 1.5))

            last = df_m1.iloc[-1]
            price = float(last["close"])
            pip_size = 0.01 if price > 10 else 0.0001

            core = df_m1.tail(w_core)
            env = df_m1.tail(w_env)

            # Compression
            core_range_pips = (core["high"].max() - core["low"].min()) / pip_size
            env_range_pips = max(
                (env["high"].max() - env["low"].min()) / pip_size, core_range_pips
            )
            compressed = (core_range_pips <= k_range) and (
                core_range_pips <= 0.35 * env_range_pips
            )

            # Impulsion récente
            recent = df_m1.tail(3)
            recent_move = (
                float(recent["close"].iloc[-1]) - float(recent["open"].iloc[0])
            ) / pip_size
            impulse_ok = abs(recent_move) >= k_imp
            direction = (
                "BUY" if recent_move > 0 else "SELL" if recent_move < 0 else "NEUTRAL"
            )

            # Qualité du setup
            quality = 0.0
            if compressed and impulse_ok:
                q_range = max(0.0, 1.0 - (core_range_pips / max(k_range, 1e-6)))
                q_imp = min(1.0, abs(recent_move) / max(k_imp * 2.0, 1e-6))
                quality = 0.6 * q_range + 0.4 * q_imp

            # Décision Burst
            burst_signal = bool(quality >= 0.4)
            burst_strength = round(min(1.0, quality + boost), 3)
            suggested_burst_size = int(
                min(burst_max, max(burst_base, int(burst_strength * burst_max)))
            )

            # SL/TP suggestions (scalp serré)
            base_sl = max(sl_floor, min(sl_cap, 0.5 * core_range_pips))
            sl_pips = round(base_sl, 2)
            tp_pips = round(sl_pips * tp_sl_ratio, 2)

            return {
                "burst_signal": burst_signal,
                "burst_side": direction,
                "burst_strength": burst_strength,
                "suggested_burst_size": suggested_burst_size if burst_signal else 0,
                "sl_pips_suggestion": sl_pips if burst_signal else None,
                "tp_pips_suggestion": tp_pips if burst_signal else None,
                "diagnostics": {
                    "compressed": compressed,
                    "core_range_pips": round(core_range_pips, 2),
                    "env_range_pips": round(env_range_pips, 2),
                    "recent_move_pips": round(recent_move, 2),
                    "impulse_ok": impulse_ok,
                },
            }
        except Exception as e:
            return {
                "burst_signal": False,
                "burst_side": "NEUTRAL",
                "burst_strength": 0.0,
                "suggested_burst_size": 0,
                "sl_pips_suggestion": None,
                "tp_pips_suggestion": None,
                "diagnostics": {"error": str(e)},
            }

    def determine_optimized_phase(self, row: Dict[str, Any]) -> str:
        """
        Classification de phase basée uniquement sur les 4 indicateurs core
        + signaux liquidity (sweep, absorption, eqh/eql).
        Nettoyée de toute dépendance Bollinger.
        """
        regime = str(row.get("regime", "unknown"))

        # --- Institutional trending regimes ---
        if "trending_institutional" in regime:
            if row.get("fvg_ob_confluence", False):
                return "institutional_setup_premium"
            elif row.get("ob_detected", False):
                return "institutional_setup"
            elif "bull" in regime:
                return "trending_institutional_bull"
            else:
                return "trending_institutional_bear"

        # --- Ranges (accumulation/distribution) ---
        elif "range_accumulation" in regime:
            if row.get("high_quality_ob", False):
                return "accumulation_zone"
            return "range_accumulation"

        elif "range_distribution" in regime:
            if row.get("confirmed_structure_break", False):
                return "distribution_breakout"
            return "range_distribution"

        # --- High vol / chaos ---
        elif "high_volatility" in regime:
            if row.get("bos_mss_detected", False):
                return "volatility_breakout"
            return "high_volatility_chaos"

        # --- Low vol / compression ---
        elif "low_volatility" in regime:
            return "low_volatility_compression"

        # --- Liquidity-driven signals ---
        if row.get("sweep_detected", False):
            return "liquidity_sweep"
        if row.get("absorption_confirmed", False):
            return "liquidity_absorption"
        if row.get("eqh_eql_detected", False):
            return "liquidity_eqh_eql"

        # --- Fallback divers ---
        if row.get("institutional_setup", False):
            return "smc_setup"
        elif row.get("fvg_detected", False):
            return "fvg_opportunity"

        return "no_clear_phase"

    def determine_phase(self, market_data: pd.DataFrame) -> str:
        """
        Détermine la phase de marché de la dernière bougie via un appel à `analyze`.

        Cette fonction sert d'interface simple pour obtenir l'état le plus récent du marché
        sans retourner le DataFrame complet.
        """
        try:
            annotated_data = self.analyze(market_data)
            if annotated_data is None or annotated_data.empty:
                return "uncertain"
            return str(annotated_data.iloc[-1].get("phase", "uncertain"))
        except Exception as e:
            self.logger.error(f"Erreur dans determine_phase : {e}", exc_info=True)
            return "uncertain"
