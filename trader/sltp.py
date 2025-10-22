# trader/sltp.py - Module de Gestion des Stop Loss et Take Profit pour le Bot SNIPER_X
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from trader.errors import TradeExecutionError


# ==============================
# === Helpers Trading Utils ====
# ==============================


def _normalize_stops(symbol_info, price, sl, tp):
    """
    Arrondit SL/TP aux digits du symbole et applique un minimum broker (stops_level) si nécessaire.
    """
    point = float(getattr(symbol_info, "point", 0.0) or 0.0)
    digits = int(getattr(symbol_info, "digits", 0) or 0)

    # Arrondis sûrs si présents
    if isinstance(sl, (int, float)):
        sl = round(float(sl), digits)
    else:
        sl = None

    if isinstance(tp, (int, float)):
        tp = round(float(tp), digits)
    else:
        tp = None

    # ⚠️ Ne fais des diffs que si la valeur existe
    sl_dist = (price - sl) if (sl is not None) else None
    tp_dist = (tp - price) if (tp is not None) else None

    # Stops level broker
    stops_lvl_pts = float(
        getattr(symbol_info, "trade_stops_level", 0)
        or getattr(symbol_info, "stops_level", 0)
        or 0
    )
    min_stop = stops_lvl_pts * point

    # Ajustement SL si présent (BUY: sl < price | SELL: sl > price)
    if sl is not None and min_stop > 0:
        if sl < price and (price - sl) < min_stop:
            sl = round(price - min_stop, digits)
        elif sl > price and (sl - price) < min_stop:
            sl = round(price + min_stop, digits)

    # TP : ne rien faire si None (mais le moteur SL/TP pour burst n'utilise plus de trailing-only)
    return sl, tp


def _attach_sl_tp(self, symbol: str, ticket: int, sl: float | None, tp: float | None):
    """Attache (ou ré-attache) SL/TP à une position existante via MT5."""
    mt5c = getattr(self, "mt5_connector", None)
    mt5 = getattr(mt5c, "mt5", None) if mt5c else None
    if not (mt5c and mt5):
        self.logger.warning(
            "[EXECUTOR] Impossible d’attacher SL/TP: mt5_connector absent."
        )
        return None

    req = {
        "action": getattr(mt5, "TRADE_ACTION_SLTP", None),
        "symbol": symbol,
        "position": int(ticket),
    }
    if sl is not None:
        req["sl"] = float(sl)
    if tp is not None:
        req["tp"] = float(tp)

    try:
        res = mt5c.order_send(req)
        rc = getattr(res, "retcode", None) if res else None
        if rc == getattr(mt5, "TRADE_RETCODE_DONE", None):
            self.logger.info(
                f"[EXECUTOR] SL/TP attachés pour pos#{ticket} ({symbol}) → SL={req.get('sl')} TP={req.get('tp')}"
            )
        else:
            self.logger.warning(
                f"[EXECUTOR] Attache SL/TP échec pos#{ticket} ({symbol}) retcode={rc}"
            )
        return res
    except Exception as e:
        self.logger.warning(
            f"[EXECUTOR] Attache SL/TP exception pos#{ticket} ({symbol}): {e}"
        )
        return None


# ==============================
# === Calcul SL / TP (RR dyn) ===
# ==============================


def _calculate_sl_tp_prices(
    self,
    trade_decision: dict,
    config: dict,
    symbol_info: Any,
    entry_price: float,
    market_context: dict,
) -> tuple[float, Optional[float]]:
    """
    Calcule SL/TP à partir des méthodes SWING / ATR / PIPS pour le SL,
    et RR / ATR_MULTIPLE / PIPS pour le TP, avec **RR dynamique** si fourni.

    ✅ Changement majeur:
      - **Burst_scalping utilise désormais SL & TP** (plus de désactivation TP).
      - Le **RR** peut être poussé par le pipeline via `tp_rr_ratio_hint`
        (sinon fallback RR de la conf + modulation simple par vol/trigger si dispo).
      - Les distances respectent stops_level broker + buffer soft (2 ticks).

    Retourne: (sl_price: float, tp_price: Optional[float])
    """
    import math

    try:
        import pandas as pd  # type: ignore
        import numpy as np  # type: ignore
    except Exception:
        pd = None  # type: ignore
        np = None  # type: ignore

    self.logger.info(
        "Calcul du SL/TP (SWING/ATR/PIPS + RR/ATR_MULTIPLE/PIPS, RR dynamique supporté)..."
    )

    # --- Action ---
    action_raw = str(trade_decision.get("action", "")).strip().upper()
    action = {"LONG": "BUY", "SHORT": "SELL"}.get(action_raw, action_raw)
    if action not in ("BUY", "SELL"):
        raise TradeExecutionError(f"Action invalide pour SL/TP: '{action_raw}'")

    # --- Symbole/broker ---
    point = float(getattr(symbol_info, "point", 0.0) or 0.0)
    if point <= 0:
        raise TradeExecutionError("symbol_info.point invalide (<=0).")
    digits = int(getattr(symbol_info, "digits", 0) or 0)
    tick_size = float(getattr(symbol_info, "trade_tick_size", 0.0) or point)
    min_stop_points = int(getattr(symbol_info, "trade_stops_level", 0) or 0)
    min_stop_price = min_stop_points * point

    # soft buffer si broker annonce 0 → ~2 ticks
    min_ticks_soft = 2
    soft_min_price = max(min_stop_price, min_ticks_soft * tick_size)

    # pips (digits 3/5 => 10 points/pip, sinon 1)
    points_per_pip = 10.0 if digits in (3, 5) else 1.0
    pip_size = point * points_per_pip

    # --- Helpers arrondis ---
    def _ceil_to_tick(x: float) -> float:
        if tick_size <= 0:
            return round(float(x), digits)
        steps = math.ceil(float(x) / tick_size - 1e-12)
        return round(steps * tick_size, digits)

    def _floor_to_tick(x: float) -> float:
        if tick_size <= 0:
            return round(float(x), digits)
        steps = math.floor(float(x) / tick_size + 1e-12)
        return round(steps * tick_size, digits)

    # --- Overrides PIPS / spread ---
    sl_pips_override = trade_decision.get("target_sl_pips")
    tp_pips_override = trade_decision.get("target_tp_pips")
    spread_pips = float(trade_decision.get("spread_pips", 0.0) or 0.0)

    # --- Paramètres produit / stratégie ---
    # Chemin 1 (privilégié): entry_rules.scalping.burst_scalping.sltp
    sltp_cfg = (
        ((config.get("entry_rules") or {}).get("scalping") or {})
        .get("burst_scalping", {})
        .get("sltp", {})
    ) or {}
    rr_base = float(sltp_cfg.get("rr_base", 1.5) or 1.5)
    rr_floor = float(sltp_cfg.get("rr_floor", 1.0) or 1.0)
    rr_cap = float(sltp_cfg.get("rr_cap", 3.0) or 3.0)
    sl_method = str(sltp_cfg.get("sl_method", "") or "").upper()

    # Fallback (historique): smart_sl_tp_settings
    prod_st = config.get("smart_sl_tp_settings", {}) or {}
    if not sl_method:
        sl_method = str(prod_st.get("sl_placement_method", "PIPS") or "PIPS").upper()
    tp_method = str(prod_st.get("tp_placement_method", "RR") or "RR").upper()
    rr_default = float(prod_st.get("tp_rr_ratio", rr_base) or rr_base)

    # === RR dynamique depuis le pipeline (hint) ===
    rr_hint = trade_decision.get("tp_rr_ratio_hint")
    if isinstance(rr_hint, (int, float)) and math.isfinite(rr_hint) and rr_hint > 0:
        rr_ratio = float(rr_hint)
    else:
        # Modulation simple si le pipeline a fourni des facteurs (facultatif)
        rr_ratio = float(rr_default)
        vol_factor = trade_decision.get("volatility_factor")
        trig_factor = trade_decision.get("trigger_factor")
        if (
            isinstance(vol_factor, (int, float))
            and math.isfinite(vol_factor)
            and vol_factor > 0
        ):
            rr_ratio *= float(vol_factor)
        # Si pas de trigger_factor explicite, on permet un léger boost via confidence [0..1] → [0.9..1.1]
        if (
            isinstance(trig_factor, (int, float))
            and math.isfinite(trig_factor)
            and trig_factor > 0
        ):
            rr_ratio *= float(trig_factor)
        else:
            conf = trade_decision.get("confidence")
            if isinstance(conf, (int, float)) and 0 <= float(conf) <= 1:
                rr_ratio *= 0.9 + 0.2 * float(conf)  # 0→0.9 ; 0.5→1.0 ; 1.0→1.1

    # Clamp final
    rr_ratio = max(
        rr_floor, min(rr_cap, float(rr_ratio if rr_ratio > 0 else rr_default))
    )

    # --- Hard limits historiques (points) ---
    strat_st = config.get("smart_targets") or {}
    st_sl = strat_st.get("stop_loss") or {}
    st_tp = strat_st.get("take_profit") or {}
    sl_hard_min_points = float(st_sl.get("hard_min_points", 0) or 0.0)
    sl_hard_max_points = float(
        st_sl.get("hard_max_points", float("inf")) or float("inf")
    )
    tp_hard_max_points = float(
        st_tp.get("hard_max_points", float("inf")) or float("inf")
    )

    # --- Données marché (pour ATR/SWING) ---
    symbol = str(
        trade_decision.get("asset") or trade_decision.get("symbol") or ""
    ).upper()
    rates_df = None
    try:
        md = (market_context or {}).get("market_data") or {}
        rates_df = md.get(symbol)
    except Exception:
        rates_df = None

    def _compute_atr(df, period: int) -> float:
        if (
            pd is None
            or np is None
            or not hasattr(pd, "DataFrame")
            or not isinstance(df, pd.DataFrame)
            or len(df) < period + 2
        ):
            return float("nan")
        try:
            high = df["high"].astype(float)
            low = df["low"].astype(float)
            close = df["close"].astype(float)
            pc = close.shift(1)
            tr = np.maximum.reduce(
                [
                    (high - low).abs().values,
                    (high - pc).abs().values,
                    (low - pc).abs().values,
                ]
            )
            atr = (
                pd.Series(tr).rolling(window=period, min_periods=period).mean().iloc[-1]
            )
            return float(atr) if pd.notna(atr) and atr > 0 else float("nan")
        except Exception:
            return float("nan")

    # ================= SL =================
    stop_loss_price: float = 0.0

    if isinstance(sl_pips_override, (int, float)) and sl_pips_override > 0:
        sl_dist = float(sl_pips_override) * pip_size
        stop_loss_price = (
            entry_price - sl_dist if action == "BUY" else entry_price + sl_dist
        )
    else:
        method = sl_method or "PIPS"
        if method == "SWING":
            lookback = int(prod_st.get("sl_swing_lookback_period", 10) or 10)
            buffer_pips = float(prod_st.get("sl_buffer_pips", 2) or 2.0)
            if not (
                rates_df is not None
                and hasattr(rates_df, "tail")
                and len(rates_df) >= lookback
            ):
                method = "ATR"
            else:
                buf = buffer_pips * pip_size
                if action == "BUY":
                    stop_loss_price = float(rates_df.tail(lookback)["low"].min()) - buf
                else:
                    stop_loss_price = float(rates_df.tail(lookback)["high"].max()) + buf

        if method == "ATR" and stop_loss_price == 0.0:
            atr_p = int(
                prod_st.get(
                    "sl_atr_period", prod_st.get("atr_settings", {}).get("period", 14)
                )
                or 14
            )
            atr_mult = float(prod_st.get("sl_atr_multiplier", 1.2) or 1.2)
            atr = _compute_atr(rates_df, atr_p)
            if not (atr == atr and atr > 0):
                method = "PIPS"
            else:
                sl_dist = atr_mult * atr
                stop_loss_price = (
                    entry_price - sl_dist if action == "BUY" else entry_price + sl_dist
                )

        if method == "PIPS" and stop_loss_price == 0.0:
            sl_pips = float(config.get("stop_loss_pips", 10) or 10.0)
            sl_dist = sl_pips * pip_size
            stop_loss_price = (
                entry_price - sl_dist if action == "BUY" else entry_price + sl_dist
            )

    # ================= TP =================
    # ❗️Plus de neutralisation du TP pour burst: on calcule le TP normalement (RR par défaut)
    take_profit_price: Optional[float] = None

    if isinstance(tp_pips_override, (int, float)) and tp_pips_override > 0:
        tp_dist = float(tp_pips_override) * pip_size
        take_profit_price = (
            entry_price + tp_dist if action == "BUY" else entry_price - tp_dist
        )
    else:
        method = tp_method or "RR"
        if method == "RR":
            risk = abs(entry_price - stop_loss_price)
            if risk > 0:
                tp_dist = risk * rr_ratio
                take_profit_price = (
                    entry_price + tp_dist if action == "BUY" else entry_price - tp_dist
                )
            else:
                method = "PIPS"

        if method == "ATR_MULTIPLE" and take_profit_price is None:
            atr_p = int(
                prod_st.get("tp_atr_period", prod_st.get("sl_atr_period", 14)) or 14
            )
            atr_mult = float(prod_st.get("tp_atr_multiplier", 2.0) or 2.0)
            atr = _compute_atr(rates_df, atr_p)
            if atr == atr and atr > 0:
                tp_dist = atr_mult * atr
                take_profit_price = (
                    entry_price + tp_dist if action == "BUY" else entry_price - tp_dist
                )

        if take_profit_price is None:  # fallback PIPS
            tp_pips = float(config.get("take_profit_pips", 20) or 20.0)
            tp_dist = tp_pips * pip_size
            take_profit_price = (
                entry_price + tp_dist if action == "BUY" else entry_price - tp_dist
            )

    # ================= Validations & ajustements =================
    if not (isinstance(entry_price, (int, float)) and entry_price > 0):
        raise TradeExecutionError("Prix d'entrée invalide.")

    # Distances brutes
    sl_dist_price = (
        (entry_price - stop_loss_price)
        if action == "BUY"
        else (stop_loss_price - entry_price)
    )
    if not (sl_dist_price and sl_dist_price > 0):
        raise TradeExecutionError("Distance SL invalide (<=0).")

    tp_dist_price = None
    if take_profit_price is not None:
        tp_dist_price = (
            (take_profit_price - entry_price)
            if action == "BUY"
            else (entry_price - take_profit_price)
        )
        if not (tp_dist_price and tp_dist_price > 0):
            raise TradeExecutionError("Distance TP invalide (<=0).")

    # A) min broker + soft 2 ticks
    sl_dist_price = max(sl_dist_price, soft_min_price)
    if tp_dist_price is not None:
        tp_dist_price = max(tp_dist_price, soft_min_price)

    # B) Hard limits (en points)
    sl_dist_points = sl_dist_price / point
    sl_dist_points = max(sl_dist_points, sl_hard_min_points)
    sl_dist_points = min(sl_dist_points, sl_hard_max_points)
    sl_dist_price = sl_dist_points * point

    tp_dist_points = None
    if tp_dist_price is not None:
        tp_dist_points = tp_dist_price / point
        tp_dist_points = min(tp_dist_points, tp_hard_max_points)
        tp_dist_price = tp_dist_points * point

    # C) Ajustement spread (si fourni) : impose TP >= SL + spread (en pips)
    if tp_dist_price is not None and spread_pips > 0:
        sl_pips_now = sl_dist_points / points_per_pip
        tp_pips_now = (
            (tp_dist_points / points_per_pip) if tp_dist_points is not None else None
        )
        if tp_pips_now is not None and tp_pips_now < (sl_pips_now + spread_pips):
            tp_dist_points = (sl_pips_now + spread_pips) * points_per_pip
            tp_dist_price = tp_dist_points * point

    # D) Positionnement côté BID/ASK (si tick disponible)
    try:
        tick_map = (market_context or {}).get("last_tick") or {}
        tick = tick_map.get(symbol) or {}
        bid = float(tick.get("bid") or 0.0)
        ask = float(tick.get("ask") or 0.0)
    except Exception:
        bid = ask = 0.0

    if bid > 0 and ask > 0 and ask > bid:
        if action == "BUY":
            stop_loss_price = entry_price - sl_dist_price
            if take_profit_price is not None:
                take_profit_price = entry_price + tp_dist_price
                # assure TP > ASK + min
                if take_profit_price < (ask + soft_min_price - 1e-12):
                    take_profit_price = ask + soft_min_price
        else:  # SELL
            stop_loss_price = entry_price + sl_dist_price
            if take_profit_price is not None:
                take_profit_price = entry_price - tp_dist_price
                # assure TP < BID - min
                if take_profit_price > (bid - soft_min_price + 1e-12):
                    take_profit_price = bid - soft_min_price
    else:
        # fallback sans bid/ask
        stop_loss_price = (
            entry_price - sl_dist_price
            if action == "BUY"
            else entry_price + sl_dist_price
        )
        if take_profit_price is not None:
            take_profit_price = (
                entry_price + (tp_dist_price or 0.0)
                if action == "BUY"
                else entry_price - (tp_dist_price or 0.0)
            )

    # E) Arrondi à la grille des ticks + dernières cohérences directionnelles
    if action == "BUY":
        stop_loss_price = _floor_to_tick(
            min(stop_loss_price, entry_price - soft_min_price)
        )
        if take_profit_price is not None:
            take_profit_price = _ceil_to_tick(
                max(take_profit_price, entry_price + soft_min_price)
            )
            if not (take_profit_price > entry_price):
                take_profit_price = _ceil_to_tick(entry_price + soft_min_price)
        if not (stop_loss_price < entry_price):
            stop_loss_price = _floor_to_tick(entry_price - soft_min_price)
    else:  # SELL
        stop_loss_price = _ceil_to_tick(
            max(stop_loss_price, entry_price + soft_min_price)
        )
        if take_profit_price is not None:
            take_profit_price = _floor_to_tick(
                min(take_profit_price, entry_price - soft_min_price)
            )
            if not (take_profit_price < entry_price):
                take_profit_price = _floor_to_tick(entry_price - soft_min_price)
        if not (stop_loss_price > entry_price):
            stop_loss_price = _ceil_to_tick(entry_price + soft_min_price)

    # Sortie finale
    stop_loss_price = round(float(stop_loss_price), digits)
    take_profit_price = (
        None if take_profit_price is None else round(float(take_profit_price), digits)
    )
    return float(stop_loss_price), take_profit_price


# ================================================
# === Split multi-TP (non utilisé en burst) ======
# ================================================


def _split_multi_tp_orders(
    self,
    trade_decision: dict,
    config: dict,
    volume: float,
    entry_price_market: float,
    sl_price: float,
    tp_prices: list,
    symbol_info: Any,
    trigger_price: Optional[float] = None,
    order_type_str: str = "MARKET",
) -> list[dict]:
    """
    Si la stratégie fournit plusieurs TP (ex: [tp1, tp2]),
    on split le volume en plusieurs ordres (répartition égale).
    Chaque ordre est construit via _build_mt5_request.

    ⚠️ En mode burst_scalping, on **n'utilise pas** de multi-TP:
       → on garde un **TP unique** (SL/TP identiques pour tous les legs du panier).
    """
    rule = str(trade_decision.get("rule_name", "")).lower()

    # Cas BURST: forcer un seul TP (soit unique dans la liste, soit le premier)
    if rule == "burst_scalping":
        single_tp = None
        if isinstance(tp_prices, list) and len(tp_prices) >= 1 and tp_prices[0]:
            single_tp = float(tp_prices[0])
        # construit une requête unique
        return [
            self._build_mt5_request(
                trade_decision,
                config,
                float(volume),
                entry_price_market,
                float(sl_price),
                float(single_tp) if single_tp else None,
                symbol_info,
                trigger_price,
                order_type_str,
            )
        ]

    # Standard (non-burst): 0 ou 1 TP → requête unique
    if not isinstance(tp_prices, list) or len(tp_prices) <= 1:
        return [
            self._build_mt5_request(
                trade_decision,
                config,
                float(volume),
                entry_price_market,
                float(sl_price),
                float(tp_prices[0]) if tp_prices else None,
                symbol_info,
                trigger_price,
                order_type_str,
            )
        ]

    # === Split multi-TP (non-burst) ===
    parts = max(
        1, len([tp for tp in tp_prices if isinstance(tp, (int, float)) and tp > 0])
    )
    sub_vol = round(float(volume) / parts, 2)
    requests = []

    for tp in tp_prices:
        if not isinstance(tp, (int, float)) or tp <= 0:
            continue
        req = self._build_mt5_request(
            trade_decision,
            config,
            sub_vol,
            entry_price_market,
            float(sl_price),
            float(tp),
            symbol_info,
            trigger_price,
            order_type_str,
        )
        # Annotation facultative (si besoin d’audit)
        req["comment"] = f"{req.get('comment','')}|TP@{tp:.5f}"[:31]
        requests.append(req)

    return requests
