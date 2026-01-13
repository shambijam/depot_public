# trader/sltp.py - Module de Gestion des Stop Loss et Take Profit pour le Bot SNIPER_X
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple, Mapping, Callable

from trader.errors import TradeExecutionError
import time
import threading
import math 

# --- Helpers de normalisation ---


def resolve_side(decision: Mapping[str, Any]) -> str:
    """
    Retourne 'BUY' ou 'SELL' en fouillant tous les alias courants, y compris imbriqués.
    Tolère: action/final_action/side/direction/order_side/entry_side/position_side,
            decision.action, metadata.action, execution.action, setup.action, entry.action
    Synonymes: LONG->BUY, SHORT->SELL, bull/bear, up/down, b/s, +1/-1.
    """
    if not isinstance(decision, dict):
        raise TradeExecutionError(
            "Action invalide pour SL/TP: 'decision' n'est pas un dict."
        )

    # --- utilitaires case-insensitive ---
    def _get_ci(d: Mapping[str, Any], key: str):
        for k, v in d.items():
            if isinstance(k, str) and k.lower() == key.lower():
                return v
        return None

    def _dig_ci(d: Mapping[str, Any], path: str):
        cur = d
        for part in path.split("."):
            if not isinstance(cur, dict):
                return None
            cur = _get_ci(cur, part)
            if cur is None:
                return None
        return cur

    candidates = []
    flat_keys = (
        "action",
        "final_action",
        "side",
        "direction",
        "order_side",
        "entry_side",
        "position_side",
    )
    nested_paths = (
        "decision.action",
        "decision.side",
        "metadata.action",
        "execution.action",
        "setup.action",
        "entry.action",
    )

    for k in flat_keys:
        v = _get_ci(decision, k)
        if v is not None:
            candidates.append(v)
    for p in nested_paths:
        v = _dig_ci(decision, p)
        if v is not None:
            candidates.append(v)

    # Prend le premier candidat textuel non vide
    for v in candidates:
        s = str(v).strip().lower()
        if not s:
            continue
        if s in {"buy", "long", "b", "bull", "bullish", "up", "+", "+1", "1"}:
            return "BUY"
        if s in {"sell", "short", "s", "bear", "bearish", "down", "-", "-1"}:
            return "SELL"

    # Rien trouvé → message d’erreur utile (liste les clés présentes)
    present_keys = ", ".join(map(str, decision.keys()))
    raise TradeExecutionError(
        "Action invalide pour SL/TP: vide ou non reconnue (aucun alias trouvé). "
        f"Clés présentes dans 'decision': [{present_keys}]"
    )


class SLTPError(Exception):
    pass


def _normalize_stops(
    sl_price: float | None, tp_price: float | None, digits: int
) -> tuple[float | None, float | None]:
    """Arrondi propre des stops au nombre de décimales du symbole."""

    def _r(x):
        if x is None:
            return None
        factor = 10**digits
        return round(float(x) * factor) / factor

    return _r(sl_price), _r(tp_price)


def calculate_pip_value_per_lot(
    symbol: str,
    symbol_info: Any = None,
    current_rate: float = None
) -> float:
    """
    Calcule la valeur d'un pip pour 1 lot standard dans la devise du compte (USD).

    Args:
        symbol: Nom du symbole (ex: "XAUUSD", "USDJPY", "EURUSD")
        symbol_info: Infos MT5 du symbole (optionnel, pour méthode précise)
        current_rate: Taux de change actuel (optionnel, pour fallback)

    Returns:
        float: Valeur d'un pip en USD pour 1 lot

    Exemples:
        - XAUUSD: 1.0 USD (100 oz × 0.01 = 1 USD)
        - USDJPY @ 150: 6.67 USD (1000 JPY / 150 = 6.67 USD)
        - EURUSD: 10.0 USD (100,000 × 0.0001 = 10 USD)
    """
    symbol = symbol.upper().strip()

    # Méthode 1: Utiliser trade_tick_value et trade_tick_size de MT5 (PRÉCIS)
    if symbol_info is not None:
        try:
            tick_value = float(getattr(symbol_info, "trade_tick_value", 0) or 0)
            tick_size = float(getattr(symbol_info, "trade_tick_size", 0) or 0)
            digits = int(getattr(symbol_info, "digits", 0) or 0)

            if tick_value > 0 and tick_size > 0:
                # Taille d'un pip selon les décimales
                # digits 3/5 => pip = 10 points, sinon pip = 1 point
                pip_multiplier = 10.0 if digits in (3, 5) else 1.0

                # Valeur d'un pip = (pip_size / tick_size) × tick_value
                pip_value = (pip_multiplier * tick_size / tick_size) * tick_value
                pip_value = pip_multiplier * tick_value

                return float(pip_value)
        except Exception:
            pass

    # Méthode 2: Fallback basé sur des valeurs connues par symbole
    if "XAU" in symbol:  # Or (Gold)
        # XAUUSD: 100 oz × 0.01 USD = 1.00 USD par pip
        return 1.0

    elif "JPY" in symbol:  # Paires avec JPY
        # USDJPY, EURJPY, etc: 100,000 unités × 0.01 JPY
        # Besoin du taux de change pour convertir JPY → USD
        if current_rate and current_rate > 0:
            # 1 pip = 0.01 JPY sur 100,000 unités = 1,000 JPY
            # Converti en USD: 1,000 / rate
            return 1000.0 / float(current_rate)
        else:
            # Fallback conservateur basé sur USD/JPY ~ 150
            return 6.67

    elif symbol in ("EURUSD", "GBPUSD", "AUDUSD", "NZDUSD", "USDCAD", "USDCHF"):
        # Paires forex standard: 100,000 × 0.0001 = 10.00 USD par pip
        return 10.0

    else:
        # Fallback générique (conservateur)
        return 10.0


def _get(sym: dict, *keys, default=None):
    for k in keys:
        if k in sym and sym[k] is not None:
            return sym[k]
    return default


def _min_stop_distance_points(symbol_info: dict) -> int:
    # Compat: différents brokers/structs → plusieurs clés possibles
    return int(
        _get(
            symbol_info,
            "stops_level",
            "min_stop_distance_points",
            "StopLevel",
            default=0,
        )
        or 0
    )


def apply_broker_constraints(
    price: float,
    side: int,
    sl_price: float | None,
    tp_price: float | None,
    symbol_info: dict,
) -> tuple[float | None, float | None]:
    """
    Applique la distance minimale broker (en points) et corrige la position
    relative SL/TPh vs prix selon BUY/SELL.
    """
    point = float(_get(symbol_info, "point", "tick_size", default=0.0001))
    digits = int(_get(symbol_info, "digits", "precision", default=5))
    min_pts = _min_stop_distance_points(symbol_info)
    min_dist = float(min_pts) * point

    if side not in (1, -1):
        raise SLTPError(f"side invalide: {side}")

    if side == 1:  # BUY
        if sl_price is not None:
            sl_price = min(sl_price, price - min_dist)
        if tp_price is not None:
            tp_price = max(tp_price, price + min_dist)
    else:  # SELL
        if sl_price is not None:
            sl_price = max(sl_price, price + min_dist)
        if tp_price is not None:
            tp_price = min(tp_price, price - min_dist)

    return _normalize_stops(sl_price, tp_price, digits)


def _safe_rr(
    price: float, side: int, sl_price: float | None, tp_price: float | None
) -> float | None:
    """
    Calcule RR = reward/risk si possible.
    """
    try:
        if sl_price is None or tp_price is None:
            return None
        risk = abs(price - sl_price)
        if risk <= 0:
            return None
        reward = abs(tp_price - price)
        return (reward / risk) if reward > 0 else None
    except Exception:
        return None


def build_final_sl_tp(
    price: float,
    action: str,
    hints: dict,
    symbol_info: dict,
) -> dict:
    """
    Fusionne les 'hints' du pipeline avec les contraintes broker et produit
    un SL/TP final cohérent (dynamique).
    Priorités:
    1) sl_price/tp_price explicites (si fournis)
    2) sl_pips/tp_pips → conversion en prix
    3) rr (tp_rr_ratio_hint) avec SL connu → dérive TP
    4) fallback sur distance minimale broker (si rien d’autre)
    """
    if not action:
        raise SLTPError("Action manquante pour SL/TP.")
    act = str(action).strip().upper()
    side = 1 if act == "BUY" else -1

    point = float(_get(symbol_info, "point", "tick_size", default=0.0001))
    digits = int(_get(symbol_info, "digits", "precision", default=5))
    min_pts = _min_stop_distance_points(symbol_info)
    min_dist = float(min_pts) * point

    sl_price = hints.get("sl_price")
    tp_price = hints.get("tp_price")
    sl_pips = hints.get("sl_pips")
    tp_pips = hints.get("tp_pips")
    rr_hint = hints.get("rr") or hints.get("tp_rr_ratio_hint") or hints.get("rr_hint")

    # 1) Prix explicites → on les respecte d’abord
    if sl_price is None and sl_pips:
        # Convention: on traite 'pips' comme nombre de points si pas d’info différente
        sl_price = price - side * float(sl_pips) * point
    if tp_price is None and tp_pips:
        tp_price = price + side * float(tp_pips) * point

    # 3) Si SL connu + RR fourni mais TP manquant → dérive TP
    if tp_price is None and rr_hint and sl_price is not None:
        risk = abs(price - float(sl_price))
        if risk > 0:
            tp_price = price + side * float(rr_hint) * risk

    # 4) Fallback si on n’a vraiment rien: on met du mini broker
    if sl_price is None and tp_price is None:
        # on pousse de 2x la distance mini pour éviter le bord
        sl_price = price - side * (2.0 * min_dist)
        tp_price = price + side * (2.0 * min_dist)

    # Contraintes broker + arrondis
    sl_price, tp_price = apply_broker_constraints(
        price, side, sl_price, tp_price, symbol_info
    )
    rr = _safe_rr(price, side, sl_price, tp_price)

    return {
        "action": act,
        "side": side,
        "price": float(price),
        "sl_price": sl_price,
        "tp_price": tp_price,
        "rr": rr,
        "digits": digits,
        "point": point,
        "min_stop_points": min_pts,
    }


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
    basket_context: Optional[dict] = None,
) -> tuple[float, Optional[float]]:
    """
    Calcule SL/TP à partir de SWING/ATR/PIPS pour le SL, et RR/ATR_MULTIPLE/PIPS pour le TP.
    - Normalise l'action via resolve_side() (BUY/SELL) et l'écrit dans trade_decision["action"]
    - Respecte stops_level broker (+ soft buffer 2 ticks)
    - RR dynamique (tp_rr_ratio_hint) + modulation optionnelle par facteurs de contexte
    - Trailing géré ailleurs : ici calcul des PRIX SL/TP seulement (burst_scalping = SL/TP only)
    - Contexte panier lu pour logging/metadata, SANS impacter les prix
    Retour: (stop_loss_price, take_profit_price|None)
    """

    # ---------- 0) Direction & entrées ----------
    try:
        action = resolve_side(trade_decision)  # "BUY" / "SELL"
        trade_decision["action"] = action
    except Exception as e:
        raise TradeExecutionError(
            f"Action invalide pour SL/TP: vide ou non reconnue ({e})"
        )

    if not (isinstance(entry_price, (int, float)) and entry_price > 0):
        raise TradeExecutionError("Prix d'entrée invalide.")

    # ---------- 0.1) Contexte panier (metadata only) ----------
    if isinstance(basket_context, dict) and basket_context:
        try:
            bc = basket_context

            def _to_int(x, d=None):
                try:
                    xi = int(x)
                    return xi if (d is None or xi >= 0) else d
                except Exception:
                    return d

            def _to_float(x, d=None):
                try:
                    xf = float(x)
                    return xf if (d is None or (xf == xf)) else d  # NaN check
                except Exception:
                    return d

            def _clamp(v, lo, hi):
                try:
                    return max(lo, min(hi, float(v)))
                except Exception:
                    return None

            basket_id = (
                str(bc.get("basket_id")) if bc.get("basket_id") is not None else None
            )
            current_positions = _to_int(bc.get("current_positions"))
            target_burst_size = _to_int(bc.get("target_burst_size"))
            avg_entry_price = _to_float(bc.get("avg_entry_price"))
            basket_pnl_pips = _to_float(bc.get("basket_pnl_pips"))
            basket_age_minutes = _to_float(bc.get("basket_age_minutes"))
            phase_provided = (
                str(bc.get("basket_phase")).upper() if bc.get("basket_phase") else None
            )

            fill_ratio = None
            valid = (
                isinstance(target_burst_size, int)
                and target_burst_size > 0
                and isinstance(current_positions, int)
                and current_positions >= 0
            )
            if valid:
                fill_ratio = _clamp(
                    current_positions / float(target_burst_size), 0.0, 1.0
                )

            allowed_phases = {"ACCUMULATION", "TARGETING", "SECURING"}
            if phase_provided in allowed_phases:
                phase, phase_source = phase_provided, "provided"
            else:
                if fill_ratio is None:
                    phase, phase_source = None, None
                elif fill_ratio <= 0.50:
                    phase, phase_source = "ACCUMULATION", "derived"
                elif fill_ratio <= 0.80:
                    phase, phase_source = "TARGETING", "derived"
                else:
                    phase, phase_source = "SECURING", "derived"

            basket_summary = {
                "basket_id": basket_id,
                "current_positions": current_positions,
                "target_burst_size": target_burst_size,
                "avg_entry_price": avg_entry_price,
                "basket_pnl_pips": basket_pnl_pips,
                "basket_age_minutes": basket_age_minutes,
                "fill_ratio": fill_ratio,
                "phase": phase,
                "phase_source": phase_source,
                "valid": bool(valid),
            }
            extras = trade_decision.setdefault("extras", {})
            extras["basket_context"] = basket_summary
            try:
                self.logger.debug(f"[SLTP][BasketCtx] {basket_summary}")
            except Exception:
                pass
        except Exception:
            pass

    # ---------- 1) Paramètres broker ----------
    point = float(getattr(symbol_info, "point", 0.0) or 0.0)
    if point <= 0:
        raise TradeExecutionError("symbol_info.point invalide (<=0).")
    digits = int(getattr(symbol_info, "digits", 0) or 0)
    tick_size = float(getattr(symbol_info, "trade_tick_size", 0.0) or point)
    stops_lvl = int(
        getattr(symbol_info, "trade_stops_level", 0)
        or getattr(symbol_info, "stops_level", 0)
        or 0
    )
    min_stop_price = stops_lvl * point

    # soft buffer si broker annonce 0 → ~2 ticks
    min_ticks_soft = 2
    soft_min_price = max(min_stop_price, min_ticks_soft * tick_size)

    # pips (digits 3/5 => 10 points/pip, sinon 1)
    points_per_pip = 10.0 if digits in (3, 5) else 1.0
    pip_size = point * points_per_pip

    # ---------- 2) Données marché, overrides ----------
    sl_pips_override = trade_decision.get("target_sl_pips")
    tp_pips_override = trade_decision.get("target_tp_pips")
    spread_pips = float(trade_decision.get("spread_pips", 0.0) or 0.0)

    symbol = str(
        trade_decision.get("asset") or trade_decision.get("symbol") or ""
    ).upper()
    try:
        rates_df = ((market_context or {}).get("market_data") or {}).get(symbol)
    except Exception:
        rates_df = None

    # ---------- 3) ATR helper (optionnel) ----------
    try:
        import pandas as pd  # type: ignore
        import numpy as np  # type: ignore
    except Exception:
        pd = None
        np = None

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

    # ---------- 4) Lecture configuration (dynamique > legacy) ----------
    sltp_cfg = (
        ((config.get("entry_rules") or {}).get("scalping") or {})
        .get("burst_scalping", {})
        .get("sltp", {})
    ) or {}

    # DEBUG: Tracer config path
    try:
        self.logger.debug(
            f"[SLTP_CONFIG] sltp_cfg found={bool(sltp_cfg)} | "
            f"keys={list(sltp_cfg.keys()) if sltp_cfg else 'EMPTY'}"
        )
    except Exception:
        pass

    rr_base = float(sltp_cfg.get("rr_base", 1.5) or 1.5)
    rr_floor = float(sltp_cfg.get("rr_floor", 1.0) or 1.0)
    rr_cap = float(sltp_cfg.get("rr_cap", 3.0) or 3.0)
    sl_method = str(sltp_cfg.get("sl_method", "") or "").upper()

    dyn_sl = (sltp_cfg.get("sl") or {}) if isinstance(sltp_cfg.get("sl"), dict) else {}
    dyn_tp = (sltp_cfg.get("tp") or {}) if isinstance(sltp_cfg.get("tp"), dict) else {}

    legacy = (
        (config.get("smart_sl_tp_settings") or {})
        if isinstance(config.get("smart_sl_tp_settings"), dict)
        else {}
    )
    prefer_dynamic = bool(sltp_cfg)

    # Trailing floors (pour le plancher anti-cisaille)
    trailing_cfg = ((config.get("entry_rules") or {}).get("scalping") or {}).get(
        "trailing", {}
    ) or {}
    floors_cfg = trailing_cfg.get("broker_floors", {}) or {}
    broker_min_sl_pips = float(floors_cfg.get("min_sl_distance_pips", 0.0) or 0.0)
    spread_mult = float(floors_cfg.get("spread_multiplier", 0.0) or 0.0)
    extra_buffer_pips = float(floors_cfg.get("extra_buffer_pips", 0.0) or 0.0)

    # Exécution TP: min gap TP↔SL
    tp_exec = (
        dyn_tp.get("execution", {}) if isinstance(dyn_tp.get("execution"), dict) else {}
    )
    min_sl_tp_distance_pips = float(tp_exec.get("min_sl_tp_distance_pips", 0.0) or 0.0)

    # Méthode TP: dynamique prioritaire, sinon legacy, défaut RR
    tp_method = str(dyn_tp.get("tp_method", "") or "").upper()
    if not tp_method:
        tp_method = str(legacy.get("tp_placement_method", "RR") or "RR").upper()

    # Paramètres SL dyn -> legacy -> défauts
    sl_atr_period = int(dyn_sl.get("atr_period", legacy.get("sl_atr_period", 14)))
    sl_atr_multiplier = float(
        dyn_sl.get("atr_multiplier", legacy.get("sl_atr_multiplier", 1.8))
    )
    sl_buffer_pips = float(dyn_sl.get("buffer_pips", legacy.get("sl_buffer_pips", 2.0)))
    sl_swing_lookback = int(
        dyn_sl.get("swing_lookback", legacy.get("sl_swing_lookback_period", 10))
    )
    sl_pips_default = float(
        dyn_sl.get("pips") or legacy.get("stop_loss_pips")
    )

    tp_atr_period = int(dyn_tp.get("atr_period", legacy.get("tp_atr_period", 14)))
    tp_atr_multiplier = float(
        dyn_tp.get("atr_multiplier", legacy.get("tp_atr_multiplier", 2.0))
    )
    tp_pips_default = float(
        dyn_tp.get("pips") or legacy.get("take_profit_pips")
    )

    # RR effectif (hint + facteurs)
    rr_default = float(
        rr_base if prefer_dynamic else (legacy.get("tp_rr_ratio", rr_base) or rr_base)
    )
    rr_hint = trade_decision.get("tp_rr_ratio_hint")
    if isinstance(rr_hint, (int, float)) and math.isfinite(rr_hint) and rr_hint > 0:
        rr_ratio = float(rr_hint)
    else:
        rr_ratio = float(rr_default)
        vol_factor = trade_decision.get("volatility_factor")
        trig_factor = trade_decision.get("trigger_factor")
        if (
            isinstance(vol_factor, (int, float))
            and math.isfinite(vol_factor)
            and vol_factor > 0
        ):
            rr_ratio *= float(vol_factor)
        if (
            isinstance(trig_factor, (int, float))
            and math.isfinite(trig_factor)
            and trig_factor > 0
        ):
            rr_ratio *= float(trig_factor)
        else:
            conf = trade_decision.get("confidence")
            if isinstance(conf, (int, float)) and 0 <= float(conf) <= 1:
                rr_ratio *= 0.9 + 0.2 * float(conf)  # 0→0.9 ; 0.5→1.0 ; 1→1.1
    rr_ratio = max(rr_floor, min(rr_cap, rr_ratio if rr_ratio > 0 else rr_default))

    # Hard limits historiques (points)
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

    # ---------- 5) Helpers arrondis ----------
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

    # ---------- 6) SL brut : SWING -> ATR -> PIPS (overrides prioritaire) ----------
    stop_loss_price: float = 0.0
    if isinstance(sl_pips_override, (int, float)) and sl_pips_override > 0:
        sl_dist = float(sl_pips_override) * pip_size
        stop_loss_price = (
            entry_price - sl_dist if action == "BUY" else entry_price + sl_dist
        )
    else:
        method_sl = (sl_method or "PIPS").upper()

        # SWING
        if method_sl == "SWING":
            lookback = int(sl_swing_lookback)
            buffer_pips = float(sl_buffer_pips)
            if not (hasattr(rates_df, "tail") and len(rates_df or []) >= lookback):
                method_sl = "ATR"
            else:
                buf = buffer_pips * pip_size
                if action == "BUY":
                    stop_loss_price = float(rates_df.tail(lookback)["low"].min()) - buf
                else:
                    stop_loss_price = float(rates_df.tail(lookback)["high"].max()) + buf

        # ATR
        if method_sl == "ATR" and stop_loss_price == 0.0:
            atr_p = int(sl_atr_period)
            atr_mult = float(sl_atr_multiplier)
            atr = _compute_atr(rates_df, atr_p)
            if atr == atr and atr > 0:
                sl_dist = atr_mult * atr
                stop_loss_price = (
                    entry_price - sl_dist if action == "BUY" else entry_price + sl_dist
                )
            else:
                method_sl = "PIPS"

        # PIPS (fallback final)
        if method_sl == "PIPS" and stop_loss_price == 0.0:
            sl_pips = float(sl_pips_default)
            sl_dist = sl_pips * pip_size
            stop_loss_price = (
                entry_price - sl_dist if action == "BUY" else entry_price + sl_dist
            )

    # ---------- 7) TP brut : RR / ATR_MULTIPLE / PIPS / NONE ----------
    take_profit_price: Optional[float] = None
    if isinstance(tp_pips_override, (int, float)) and tp_pips_override > 0:
        tp_dist = float(tp_pips_override) * pip_size
        take_profit_price = (
            entry_price + tp_dist if action == "BUY" else entry_price - tp_dist
        )
    else:
        method_tp = (tp_method or "RR").upper()
        if method_tp != "NONE":
            if method_tp == "RR":
                risk = abs(entry_price - stop_loss_price)
                if risk > 0:
                    tp_dist = risk * rr_ratio
                    take_profit_price = (
                        entry_price + tp_dist
                        if action == "BUY"
                        else entry_price - tp_dist
                    )
                else:
                    method_tp = "PIPS"

            if method_tp == "ATR_MULTIPLE" and take_profit_price is None:
                atr_p = int(tp_atr_period)
                atr_mult = float(tp_atr_multiplier)
                atr = _compute_atr(rates_df, atr_p)
                if atr == atr and atr > 0:
                    tp_dist = atr_mult * atr
                    take_profit_price = (
                        entry_price + tp_dist
                        if action == "BUY"
                        else entry_price - tp_dist
                    )

            if take_profit_price is None and method_tp == "PIPS":
                tp_pips = float(tp_pips_default)
                tp_dist = tp_pips * pip_size
                take_profit_price = (
                    entry_price + tp_dist if action == "BUY" else entry_price - tp_dist
                )
        else:
            take_profit_price = None  # trailing-only

    # ---------- 8) Distances brutes (prix) ----------
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

    # ---------- A) Min broker + soft 2 ticks ----------
    sl_dist_price = max(sl_dist_price, soft_min_price)
    if tp_dist_price is not None:
        tp_dist_price = max(tp_dist_price, soft_min_price)

    # ---------- B) Hard limits (points) ----------
    sl_dist_points = sl_dist_price / point
    sl_dist_points = max(sl_dist_points, sl_hard_min_points)
    sl_dist_points = min(sl_dist_points, sl_hard_max_points)
    sl_dist_price = sl_dist_points * point

    tp_dist_points = None
    if tp_dist_price is not None:
        tp_dist_points = tp_dist_price / point
        tp_dist_points = min(tp_dist_points, tp_hard_max_points)
        tp_dist_price = tp_dist_points * point

    # ---------- (NEW) Floors anti-cisaille (Ajout B) ----------
    # Appliqués APRÈS B) (hard limits) et AVANT C) (min gap TP)
    exec_floor_pts = max(0.0, float(min_sl_tp_distance_pips)) * points_per_pip
    broker_floor_pts = max(0.0, float(broker_min_sl_pips)) * points_per_pip
    spread_floor_pts = (
        max(0.0, (float(spread_mult) * float(spread_pips) + float(extra_buffer_pips)))
        * points_per_pip
    )

    sl_dist_points = max(
        sl_dist_points, exec_floor_pts, broker_floor_pts, spread_floor_pts
    )
    sl_dist_price = sl_dist_points * point

    # ---------- C) Min gap TP: TP ≥ SL + max(spread, min_sl_tp_distance_pips) ----------
    if tp_dist_price is not None:
        sl_pips_now = sl_dist_points / points_per_pip
        tp_pips_now = (
            (tp_dist_points / points_per_pip) if tp_dist_points is not None else None
        )
        min_gap_pips = max(
            float(spread_pips or 0.0), float(min_sl_tp_distance_pips or 0.0)
        )
        if tp_pips_now is not None and tp_pips_now < (sl_pips_now + min_gap_pips):
            tp_dist_points = (sl_pips_now + min_gap_pips) * points_per_pip
            tp_dist_price = tp_dist_points * point

    # ---------- D) Positionnement côté BID/ASK (si tick dispo) ----------
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
            if tp_dist_price is not None:
                take_profit_price = entry_price + tp_dist_price
                if take_profit_price < (ask + soft_min_price - 1e-12):
                    take_profit_price = ask + soft_min_price
        else:  # SELL
            stop_loss_price = entry_price + sl_dist_price
            if tp_dist_price is not None:
                take_profit_price = entry_price - tp_dist_price
                if take_profit_price > (bid - soft_min_price + 1e-12):
                    take_profit_price = bid - soft_min_price
    else:
        # fallback sans bid/ask
        stop_loss_price = (
            (entry_price - sl_dist_price)
            if action == "BUY"
            else (entry_price + sl_dist_price)
        )
        if tp_dist_price is not None:
            take_profit_price = (
                (entry_price + tp_dist_price)
                if action == "BUY"
                else (entry_price - tp_dist_price)
            )

    # ---------- E) Arrondi grille de ticks + cohérences directionnelles ----------
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

    # ---------- 9) Sortie ----------
    stop_loss_price = round(float(stop_loss_price), digits)
    take_profit_price = (
        None if take_profit_price is None else round(float(take_profit_price), digits)
    )

    # 🆕 10 JAN 2026: DEBUG - Log final SL/TP calculés
    try:
        sl_dist_final = abs(entry_price - stop_loss_price)
        tp_dist_final = abs(take_profit_price - entry_price) if take_profit_price else None
        sl_points_final = sl_dist_final / point if point > 0 else 0
        tp_points_final = (tp_dist_final / point) if (tp_dist_final and point > 0) else None
        rr_final = (tp_dist_final / sl_dist_final) if (tp_dist_final and sl_dist_final > 0) else None

        self.logger.critical(
            f"✅ [SLTP_FINAL] {symbol} {action} | Entry={entry_price:.5f} | "
            f"SL={stop_loss_price:.5f} (dist={sl_points_final:.1f}pts) | "
            f"TP={take_profit_price:.5f if take_profit_price else 'None'} "
            f"(dist={tp_points_final:.1f if tp_points_final else 0}pts) | "
            f"RR={rr_final:.2f if rr_final else 'N/A'}"
        )
    except Exception:
        pass

    return float(stop_loss_price), take_profit_price


# (A) >>> PATCH: helper de nettoyage du cache panier
def _clean_cache_if_needed(self):
    """
    Nettoie le cache des paniers anciens ou fermés (sécurité mémoire et stabilité).
    - Taille max: 50 paniers
    - Âge max: 5 minutes
    - Tolérant aux erreurs (ne doit jamais crasher)
    """
    
    try:
        cache = getattr(self, "_basket_ctx_cache", {})
        if not isinstance(cache, dict) or not cache:
            return

        now = time.time()
        max_cache_size = 50
        max_cache_age = 300.0  # seconds

        # Nettoyage par taille (garde les plus récents)
        if len(cache) > max_cache_size:
            sorted_items = sorted(
                cache.items(),
                key=lambda kv: (
                    kv[1].get("_ts", 0.0) if isinstance(kv[1], dict) else 0.0
                ),
                reverse=True,
            )
            cache.clear()
            for k, v in sorted_items[:max_cache_size]:
                cache[k] = v

        # Nettoyage par âge
        stale_keys = []
        for basket_id, item in list(cache.items()):
            try:
                item_ts = float(item.get("_ts", 0.0))
            except Exception:
                item_ts = 0.0
            if now - item_ts > max_cache_age:
                stale_keys.append(basket_id)

        for k in stale_keys:
            cache.pop(k, None)
    except Exception:
        # Ne jamais crasher sur du nettoyage
        pass


# (B) >>> PATCH: version enrichie de _resolve_basket_context_for_sltp
def _resolve_basket_context_for_sltp(
    self,
    trade_decision: dict,
    basket_context: Optional[dict] = None,
    burst_manager: Optional[Any] = None,
    ttl_sec: float = 2.0,
) -> Optional[dict]:
    """
    Résout un basket_context frais et cohérent pour SLTP :
      - Si basket_context fourni → le nettoie/complète (fill_ratio/phase/performance_score) et retourne.
      - Sinon, tente de le récupérer via burst_manager.get_basket_context(basket_id) avec cache TTL.
      - Loggue HIT/MISS et métriques utiles (phase, fill).
    Jamais d'exception : retourne None en cas d’impossibilité.
    """
    import time

    # 0) Prépare cache léger local
    try:
        cache = getattr(self, "_basket_ctx_cache", None)
        if cache is None or not isinstance(cache, dict):
            cache = {}
            setattr(self, "_basket_ctx_cache", cache)
    except Exception:
        cache = {}

    def _derive_phase(ctx: dict) -> dict:
        """Enrichit le contexte: fill_ratio, phase (si manquante), performance_score."""
        try:
            # Calcul fill_ratio
            cur = ctx.get("current_positions")
            tgt = ctx.get("target_burst_size")
            fill_ratio = None
            if (
                isinstance(cur, (int, float))
                and isinstance(tgt, (int, float))
                and float(tgt) > 0
            ):
                fill_ratio = max(0.0, min(1.0, float(cur) / float(tgt)))

            # Détermination phase
            phase = (
                str(ctx.get("basket_phase")).upper()
                if ctx.get("basket_phase")
                else None
            )
            if (
                phase not in {"ACCUMULATION", "TARGETING", "SECURING"}
                and fill_ratio is not None
            ):
                if fill_ratio <= 0.50:
                    phase = "ACCUMULATION"
                elif fill_ratio <= 0.80:
                    phase = "TARGETING"
                else:
                    phase = "SECURING"

            # Performance score (en fonction du PnL global en pips)
            performance_score = None
            pnl_pips = ctx.get("basket_pnl_pips", 0)
            if isinstance(pnl_pips, (int, float)):
                if pnl_pips > 10:
                    performance_score = "HIGH"
                elif pnl_pips > 5:
                    performance_score = "MEDIUM"
                elif pnl_pips > 0:
                    performance_score = "LOW"
                else:
                    performance_score = "NEGATIVE"
                ctx["performance_score"] = performance_score

            # Application douce
            if fill_ratio is not None and "fill_ratio" not in ctx:
                ctx["fill_ratio"] = fill_ratio
            if phase:
                ctx["basket_phase"] = phase
        except Exception:
            pass
        return ctx

    # 1) Si déjà fourni → normaliser, logguer, retourner (source=external)
    if isinstance(basket_context, dict) and basket_context:
        try:
            if "basket_id" not in basket_context and trade_decision:
                bid = trade_decision.get("basket_id")
                if bid:
                    basket_context["basket_id"] = bid
        except Exception:
            pass
        basket_context = _derive_phase(basket_context)

        # Logging stratégique
        try:
            bid = basket_context.get("basket_id", trade_decision.get("basket_id"))
            fr = basket_context.get("fill_ratio", 0.0)
            ph = basket_context.get("basket_phase", "NA")
            self.logger.debug(
                f"[BASKET_CTX] MISS(EXTERNAL) | {bid} | Phase: {ph} | Fill: {fr:.0%}"
            )
        except Exception:
            pass

        # Hygiène cache (facultatif) : on n’écrit pas en cache ce contexte externe
        try:
            self._clean_cache_if_needed()
        except Exception:
            pass
        return basket_context

    # 2) Sinon, résolution via cache / burst_manager
    try:
        basket_id = (
            str(trade_decision.get("basket_id"))
            if trade_decision.get("basket_id")
            else None
        )
    except Exception:
        basket_id = None
    if not basket_id:
        return None

    now = time.time()
    from_cache = False
    ctx = None

    # 2.1) Tentative cache (TTL)
    try:
        cval = cache.get(basket_id)
        if cval and isinstance(cval, dict):
            ts = cval.get("_ts")
            if isinstance(ts, (int, float)) and (now - float(ts)) <= float(ttl_sec):
                ctx = dict(cval)
                ctx.pop("_ts", None)
                from_cache = True
    except Exception:
        ctx = None
        from_cache = False

    # 2.2) Si MISS, appeler le manager
    if ctx is None:
        bm = burst_manager or getattr(self, "burst_manager", None)
        get_ctx = getattr(bm, "get_basket_context", None) if bm is not None else None
        if callable(get_ctx):
            try:
                ctx = get_ctx(basket_id)
            except Exception as e:
                try:
                    self.logger.debug(f"[SLTP] get_basket_context error: {e}")
                except Exception:
                    pass
                ctx = None

    # 2.2.1) Fallback: construire contexte minimal depuis MT5 si burst_manager absent
    if ctx is None:
        # DEBUG: Fallback MT5 activé
        print(f"🔄 [BASKET_CTX][FALLBACK] Tentative récupération basket {basket_id} depuis MT5...", flush=True)

        try:
            conn = getattr(self, "mt5_connector", None)
            if conn:
                positions = []
                for mname in ("get_open_positions", "positions", "list_positions", "get_positions"):
                    meth = getattr(conn, mname, None)
                    if callable(meth):
                        try:
                            positions = meth() or []
                            break
                        except Exception:
                            positions = []

                # Filtrer positions par basket_id dans le commentaire
                basket_positions = []
                for p in positions:
                    cmt = (p.get("comment") if isinstance(p, dict) else getattr(p, "comment", "")) or ""
                    if basket_id in str(cmt):
                        basket_positions.append(p)

                # DEBUG: Positions trouvées
                print(f"🔍 [BASKET_CTX][FALLBACK] {len(basket_positions)} positions trouvées pour basket {basket_id}", flush=True)

                if basket_positions:
                    # Extraire info du premier ticket
                    first = basket_positions[0]
                    symbol = (first.get("symbol") if isinstance(first, dict) else getattr(first, "symbol", "")).upper()
                    direction = "BUY" if (first.get("type") if isinstance(first, dict) else getattr(first, "type", 0)) == 0 else "SELL"

                    # Calculer PnL total en pips
                    total_pnl_pips = 0.0

                    # Récupérer symbol_info pour calcul précis
                    symbol_info_obj = None
                    current_rate = None
                    try:
                        symbol_info_obj = conn.get_symbol_info(symbol)
                        # Obtenir le taux actuel pour les paires JPY
                        if basket_positions:
                            current_rate = float(basket_positions[0].get("price_current") if isinstance(basket_positions[0], dict) else getattr(basket_positions[0], "price_current", 0.0) or 0.0)
                    except Exception:
                        pass

                    for p in basket_positions:
                        profit = float(p.get("profit") if isinstance(p, dict) else getattr(p, "profit", 0.0) or 0.0)
                        volume = float(p.get("volume") if isinstance(p, dict) else getattr(p, "volume", 0.0) or 0.0)

                        # Calcul CORRECT de la valeur par pip (spécifique au symbole)
                        pip_value_per_lot = calculate_pip_value_per_lot(
                            symbol=symbol,
                            symbol_info=symbol_info_obj,
                            current_rate=current_rate
                        )
                        pip_value_total = pip_value_per_lot * volume
                        pnl_pips = profit / pip_value_total if pip_value_total > 0 else 0.0
                        total_pnl_pips += pnl_pips

                    # Construire contexte minimal
                    ctx = {
                        "basket_id": basket_id,
                        "symbol": symbol,
                        "direction": direction,
                        "current_positions": len(basket_positions),
                        "target_burst_size": len(basket_positions),  # Assume all positions are filled
                        "basket_pnl_pips": total_pnl_pips,
                        "positions_details": [
                            {
                                "ticket": p.get("ticket") if isinstance(p, dict) else getattr(p, "ticket", None),
                                "entry_price": p.get("price_open") if isinstance(p, dict) else getattr(p, "price_open", None),
                                "sl": p.get("sl") if isinstance(p, dict) else getattr(p, "sl", None),
                                "tp": p.get("tp") if isinstance(p, dict) else getattr(p, "tp", None),
                                "volume": p.get("volume") if isinstance(p, dict) else getattr(p, "volume", None),
                            }
                            for p in basket_positions
                        ],
                        "_source": "mt5_fallback"
                    }

                    # DEBUG: Contexte créé avec PnL
                    print(f"✅ [BASKET_CTX][FALLBACK] Contexte créé | {symbol} {direction} | {len(basket_positions)} pos | PnL={total_pnl_pips:.2f} pips", flush=True)

                    try:
                        self.logger.debug(f"[BASKET_CTX] MT5_FALLBACK | {basket_id} | {symbol} {direction} | {len(basket_positions)} pos | {total_pnl_pips:.1f} pips")
                    except Exception:
                        pass
        except Exception as e:
            try:
                self.logger.debug(f"[BASKET_CTX] MT5_FALLBACK error: {e}")
            except Exception:
                pass

    # 2.3) Enrichissement, cache & logs
    if isinstance(ctx, dict) and ctx:
        ctx = _derive_phase(ctx)
        # (re)Cache uniquement si source interne (cache / burst)
        try:
            ccopy = dict(ctx)
            ccopy["_ts"] = now
            cache[basket_id] = ccopy
        except Exception:
            pass

        # Logging HIT/MISS
        try:
            fr = ctx.get("fill_ratio", 0.0)
            ph = ctx.get("basket_phase", "NA")
            cache_flag = "HIT" if from_cache else "MISS"
            self.logger.debug(
                f"[BASKET_CTX] {cache_flag} | {basket_id} | Phase: {ph} | Fill: {fr:.0%}"
            )
        except Exception:
            pass

        # Hygiène cache
        try:
            self._clean_cache_if_needed()
        except Exception:
            pass
        return ctx

    # 2.4) Rien trouvé → log léger, hygiène cache et None
    try:
        self.logger.debug(f"[BASKET_CTX] MISS(NULL) | {basket_id}")
    except Exception:
        pass
    try:
        self._clean_cache_if_needed()
    except Exception:
        pass
    return None


# (B) <<< PATCH





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
    tp_prices: list[float],
    symbol_info: Any,
    trigger_price: Optional[float] = None,
    order_type_str: str = "MARKET",
    *,
    basket_context: Optional[dict] = None,  # ← existant
    burst_manager: Optional[Any] = None,  # ← NOUVEAU
) -> list[dict]:
    """
    Multi-TP builder (desk-grade) avec prise en charge du TP dynamique pour le burst.

    - Non-burst : conserve le comportement historique (split du volume selon tp_prices et 'multi_tp.weights').
    - Burst (scalping/burst_scalping) :
        * Si basket_context est fourni : calcule un TP UNIQUE mais DYNAMIQUE en fonction du remplissage, de la performance
          et (optionnellement) de la volatilité ; puis construit 1 seul ordre avec ce TP.
        * Si basket_context est absent : comportement précédent (TP unique basé sur tp_prices[0]).

    Args:
        trade_decision : décision enrichie (action/asset/…).
        config         : configuration dynamique courante.
        volume         : volume total affecté à CE trade.
        entry_price_market : prix de référence (market) utilisé pour la cohérence SL/TP.
        sl_price       : stop-loss (déjà validé en amont).
        tp_prices      : liste des TP bruts proposés.
        symbol_info    : infos broker (digits/point/levels).
        trigger_price  : prix de déclenchement pour pending.
        order_type_str : "MARKET" | "LIMIT" | "STOP".
        basket_context : contexte panier optionnel (voir spéc. panier).
                         Clés attendues (toutes optionnelles) :
                           - basket_id: str
                           - current_positions: int
                           - target_burst_size: int (>0)
                           - avg_entry_price: float
                           - basket_pnl_pips: float
                           - basket_age_minutes: float
                           - basket_phase: str in {"ACCUMULATION","TARGETING","SECURING"}
                           - volatility_pips: float (ATR en pips si dispo)
    Returns:
        list[dict] : requêtes MT5 construites.
    """
   

    # --- 0) Validations / normalisations de base -----------------------------------
    action = str(trade_decision.get("action", "")).upper()
    asset = str(
        trade_decision.get("asset", "") or trade_decision.get("symbol", "")
    ).upper()
    if action not in {"BUY", "SELL"} or not asset:
        raise TradeExecutionError(
            "Action ou symbole invalide pour _split_multi_tp_orders."
        )

    if not isinstance(volume, (int, float)) or not math.isfinite(volume) or volume <= 0:
        raise TradeExecutionError(f"Volume invalide ({volume}).")

    if not isinstance(entry_price_market, (int, float)) or entry_price_market <= 0:
        raise TradeExecutionError("Prix d'entrée marché invalide.")

    if (not isinstance(tp_prices, list)) or (len(tp_prices) == 0):
        raise TradeExecutionError("tp_prices vide pour _split_multi_tp_orders.")

    # Broker units
    digits = int(getattr(symbol_info, "digits", 0) or 0)
    point = float(getattr(symbol_info, "point", 0.0) or 0.0)
    if point <= 0:
        raise TradeExecutionError("symbol_info.point invalide (<=0).")

    # PIP sizing
    points_per_pip = 10.0 if digits in (3, 5) else 1.0
    pip_size = point * points_per_pip

    # [PATCH] Résolution opportuniste du contexte panier si absent
    if basket_context is None:
        try:
            basket_context = self._resolve_basket_context_for_sltp(
                trade_decision=trade_decision,
                basket_context=None,
                burst_manager=burst_manager or getattr(self, "burst_manager", None),
                ttl_sec=2.0,
            )
        except Exception:
            basket_context = None

    # Rule canonical
    rule_name = str(
        trade_decision.get("rule_name", "") or trade_decision.get("strategy_rule", "")
    ).lower()
    canonical_rule = "burst_scalping" if "burst" in rule_name else rule_name

    # Config SLTP (bornes RR)
    sltp_cfg = (
        ((config.get("entry_rules") or {}).get("scalping") or {})
        .get("burst_scalping", {})
        .get("sltp", {})
    ) or {}
    rr_floor = float(sltp_cfg.get("rr_floor", 0.5) or 0.5)
    rr_cap = float(sltp_cfg.get("rr_cap", 3.0) or 3.0)
    rr_base = float(sltp_cfg.get("rr_base", 1.5) or 1.5)

    # --- 1) BRANCHE BURST — TP dynamique de panier ---------------------------------
    if canonical_rule == "burst_scalping":
        # a) Base TP distance
        #    - priorité : distance du premier TP fourni
        #    - fallback : RR base * risk
        #    - fallback ultime : 20 pips
        try:
            base_tp_price = float(tp_prices[0])
        except Exception:
            base_tp_price = None

        risk_dist = (
            abs(float(entry_price_market) - float(sl_price))
            if isinstance(sl_price, (int, float))
            else 0.0
        )
        base_dist = (
            abs(float(base_tp_price) - float(entry_price_market))
            if base_tp_price
            else 0.0
        )
        if not (math.isfinite(base_dist) and base_dist > 0):
            if math.isfinite(risk_dist) and risk_dist > 0:
                base_dist = max(pip_size, rr_base * risk_dist)
            else:
                base_dist = max(pip_size, 20.0 * pip_size)  # défense ultime

        # b) Multiplicateurs (Performance × Remplissage × Volatilité)
        mult_perf = 1.0
        mult_fill = 1.0
        mult_vol = 1.0

        if isinstance(basket_context, dict) and basket_context:
            # --- Performance (PnL global en pips) ---
            pnl_pips = basket_context.get("basket_pnl_pips")
            try:
                pnl_pips = float(pnl_pips)
            except Exception:
                pnl_pips = None
            if isinstance(pnl_pips, float) and math.isfinite(pnl_pips):
                if pnl_pips > 10.0:
                    mult_perf = 1.30
                elif pnl_pips > 5.0:
                    mult_perf = 1.15
                elif pnl_pips > 0.0:
                    mult_perf = 1.05
                else:
                    mult_perf = (
                        0.90  # panier en difficulté → objectif plus conservateur
                    )

            # --- Remplissage (0-25 / 25-75 / 75-100) ---
            cur = max(0, int(basket_context.get("current_positions", 0) or 0))
            tgt = max(0, int(basket_context.get("target_burst_size", 0) or 0))
            fill_ratio = float(cur) / float(tgt) if tgt > 0 else 0.0
            if fill_ratio >= 0.75:
                mult_fill = 1.20
            elif fill_ratio >= 0.25:
                mult_fill = 1.10
            else:
                mult_fill = 1.00

            # --- Volatilité ---
            # Sources possibles :
            #   - basket_context["volatility_pips"]  (ATR en pips)
            #   - trade_decision["atr_pips"]
            #   - trade_decision["volatility_factor"] (dimensionless ~1.0)
            vol_pips = basket_context.get(
                "volatility_pips", trade_decision.get("atr_pips")
            )
            vf_hint = trade_decision.get("volatility_factor")
            vol_level = "normal"

            if isinstance(vol_pips, (int, float)) and math.isfinite(float(vol_pips)):
                vp = float(vol_pips)
                # Heuristique simple : seuils relatifs
                if vp >= 12.0:
                    vol_level = "high"
                elif vp <= 4.0:
                    vol_level = "low"
                else:
                    vol_level = "normal"
            elif isinstance(vf_hint, (int, float)) and math.isfinite(float(vf_hint)):
                vf = float(vf_hint)
                if vf >= 1.20:
                    vol_level = "high"
                elif vf <= 0.80:
                    vol_level = "low"
                else:
                    vol_level = "normal"

            if vol_level == "high":
                mult_vol = 0.90
            elif vol_level == "low":
                mult_vol = 1.10
            else:
                mult_vol = 1.00

        total_mult = float(mult_perf) * float(mult_fill) * float(mult_vol)
        dyn_dist = max(
            pip_size, base_dist * max(0.10, min(total_mult, 3.0))
        )  # garde-fou

        # c) Bornes RR (0.5 ↔ 3.0 par défaut)
        if math.isfinite(risk_dist) and risk_dist > 0:
            rr_now = dyn_dist / risk_dist
            if rr_now < rr_floor:
                dyn_dist = rr_floor * risk_dist
            elif rr_now > rr_cap:
                dyn_dist = rr_cap * risk_dist

        # d) Prix final cohérent avec la direction
        if action == "BUY":
            tp_dyn = float(entry_price_market) + float(dyn_dist)
            # protection minimale côté ask/bid sera refaite dans _build_mt5_request
            if tp_dyn <= entry_price_market:
                tp_dyn = entry_price_market + pip_size
        else:  # SELL
            tp_dyn = float(entry_price_market) - float(dyn_dist)
            if tp_dyn >= entry_price_market:
                tp_dyn = entry_price_market - pip_size

        # e) Construction de la requête unique
        req = self._build_mt5_request(
            trade_decision=trade_decision,
            config=config,
            volume=float(volume),
            entry_price_market=float(entry_price_market),
            sl_price=float(sl_price),
            tp_price=float(tp_dyn),
            symbol_info=symbol_info,
            trigger_price=trigger_price,
            order_type_str=order_type_str,
        )
        return [req]

    # --- 2) NON-BURST — logique historique de split multi-TP -----------------------
    # Validation des longueurs (équilibrage 1:1 si weights absent)
    weights = None
    try:
        weights = (
            ((config.get("entry_rules") or {}).get("scalping") or {})
            .get("multi_tp", {})
            .get("weights")
        )
        if isinstance(weights, list) and len(weights) != len(tp_prices):
            # mismatch → on ignore les poids
            weights = None
    except Exception:
        weights = None

    # Normalisation des poids
    if isinstance(weights, list) and weights:
        try:
            raw = [max(0.0, float(w)) for w in weights]
            s = sum(raw)
            if s <= 0:
                weights = None
            else:
                weights = [w / s for w in raw]
        except Exception:
            weights = None

    # Sans poids => split égal
    if weights is None:
        weights = [1.0 / float(len(tp_prices)) for _ in tp_prices]

    # Construction des requêtes fractionnées
    requests: list[dict] = []
    for idx, tp in enumerate(tp_prices):
        try:
            w = float(weights[idx])
        except Exception:
            w = 0.0
        if w <= 0:
            continue

        leg_vol = float(volume) * w
        req = self._build_mt5_request(
            trade_decision=trade_decision,
            config=config,
            volume=leg_vol,
            entry_price_market=float(entry_price_market),
            sl_price=float(sl_price),
            tp_price=float(tp),
            symbol_info=symbol_info,
            trigger_price=trigger_price,
            order_type_str=order_type_str,
        )
        requests.append(req)

    if not requests:
        # sécurité : au moins 1 ordre avec le 1er TP
        req = self._build_mt5_request(
            trade_decision=trade_decision,
            config=config,
            volume=float(volume),
            entry_price_market=float(entry_price_market),
            sl_price=float(sl_price),
            tp_price=float(tp_prices[0]),
            symbol_info=symbol_info,
            trigger_price=trigger_price,
            order_type_str=order_type_str,
        )
        requests.append(req)

    return requests


