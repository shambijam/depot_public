# trader/sizing.py - Module de Dimensionnement des Positions pour le Bot SNIPER_X
from __future__ import annotations

from typing import Any, Dict
import math

from trader.errors import TradeExecutionError


# ==============================
# === Helpers Trading Utils ====
# ==============================


def compute_lot_from_risk(
    symbol_info,
    account_info,
    entry,
    sl,
    risk_pct,
    confidence=1.0,  # ignoré (compat)
    burst_size=1,
    atr=None,  # ignoré (compat)
    atr_ref=10.0,  # ignoré (compat)
) -> float:
    """
    Sizing STRICT basé uniquement sur risk_per_trade_percent.
    - Aucun impact des paramètres 'confidence', 'atr', 'atr_ref' (ignorés).
    - Division du budget si 'burst_size' > 1 (budget PAR TICKET).
    - Floor au pas broker (jamais au-dessus du budget).
    - Cap par marge si infos dispo ; si le budget ne permet pas d'atteindre vmin → 0.0.
    """

    # ----- 0) Entrées -----
    try:
        entry = float(entry)
        sl = float(sl)
        risk_pct = float(risk_pct)
    except Exception:
        return 0.0
    if risk_pct <= 0 or entry <= 0 or sl <= 0:
        return 0.0

    balance = float(getattr(account_info, "balance", 0.0) or 0.0)
    if balance <= 0:
        return 0.0

    # ----- 1) Budget strict (uniquement risk%) -----
    risk_usd = balance * (risk_pct / 100.0)
    try:
        burst_size = int(burst_size or 1)
    except Exception:
        burst_size = 1
    if burst_size > 1:
        risk_usd /= float(burst_size)
    if risk_usd <= 0:
        return 0.0

    # ----- 2) Perte/lot (USD) selon distance Entry–SL -----
    distance = abs(entry - sl)
    if distance <= 0:
        return 0.0

    # tick_value/tick_size si dispo, sinon fallback contract_size
    tv = getattr(symbol_info, "trade_tick_value", None) or getattr(
        symbol_info, "tick_value", None
    )
    ts = getattr(symbol_info, "trade_tick_size", None) or getattr(
        symbol_info, "tick_size", None
    )
    contract_size = float(getattr(symbol_info, "trade_contract_size", 100.0) or 100.0)

    per_lot_loss = None
    try:
        tv = float(tv) if tv is not None else None
        ts = float(ts) if ts is not None else None
        if tv is not None and ts and ts > 0:
            per_lot_loss = (distance / ts) * tv
    except Exception:
        per_lot_loss = None

    if per_lot_loss is None:
        per_lot_loss = distance * contract_size

    if not math.isfinite(per_lot_loss) or per_lot_loss <= 0:
        return 0.0

    # ----- 3) Volume brut -----
    lots_raw = risk_usd / per_lot_loss
    if not math.isfinite(lots_raw) or lots_raw <= 0:
        return 0.0

    # ----- 4) Contraintes broker + quantification FLOOR -----
    vmin = float(getattr(symbol_info, "volume_min", 0.01) or 0.01)
    vstep = float(getattr(symbol_info, "volume_step", 0.01) or 0.01)
    vmax = float(getattr(symbol_info, "volume_max", 100.0) or 100.0)
    if vmin <= 0 or vstep <= 0 or vmax <= 0 or vmax < vmin:
        vmin, vstep, vmax = 0.01, 0.01, 100.0

    try:
        decimals = max(0, int(round(-math.log10(vstep)))) if vstep > 0 else 2
    except Exception:
        decimals = 2

    def _qdown(val: float, step: float) -> float:
        return max(0.0, math.floor((val + 1e-12) / step) * step)

    lots_cap = min(lots_raw, vmax)
    lots_q = _qdown(lots_cap, vstep)
    if lots_q < vmin:
        # budget trop faible pour le min lot — on ne force JAMAIS vers le haut
        return 0.0

    # ----- 5) Cap marge (si infos dispo) -----
    free_margin = float(getattr(account_info, "margin_free", 0.0) or 0.0)
    leverage = float(getattr(account_info, "leverage", 0.0) or 0.0)
    if free_margin > 0 and leverage > 0 and entry > 0:
        margin_per_lot = (entry * contract_size) / max(leverage, 1.0)
        if margin_per_lot > 0:
            max_by_margin = free_margin / margin_per_lot
            lots_q = _qdown(min(lots_q, max_by_margin), vstep)
            if lots_q < vmin:
                return 0.0

    return round(min(max(lots_q, vmin), vmax), decimals)


def _calculate_risk_based_volume(
    self,
    trade_decision: dict,
    config: dict,
    context: dict,
    symbol_info: Any,
    entry_price: float,
    sl_price: float,
    account_trade_settings: Dict[str, Any],
) -> float:
    """
    STRICT RISK% — sizing unique, sans modulations.
    - Risque% unique: account_trade_settings["risk_per_trade_percent"] (obligatoire)
    - Distance = |entry - SL| réelle
    - Quantification FLOOR au pas lot (jamais au-dessus du budget)
    - Scope: SINGLE / BASKET (si BASKET, retourne volume PAR TICKET)
    - Lève TradeExecutionError si une étape critique n'est pas calculable
    """
    import math

    # ---- Fallback local si l'exception n'existe pas dans le scope module ----
    try:
        _ = TradeExecutionError  # type: ignore
    except NameError:  # pragma: no cover

        class TradeExecutionError(Exception):  # type: ignore
            pass

    # ----- Helpers -----
    def _sget(obj, *names, default=None):
        for n in names:
            if hasattr(obj, n):
                v = getattr(obj, n)
                if v is not None:
                    return v
            if isinstance(obj, dict) and obj.get(n) is not None:
                return obj[n]
        return default

    def _as_float(x, name):
        try:
            v = float(x)
            if not math.isfinite(v):
                raise ValueError
            return v
        except Exception:
            raise TradeExecutionError(f"{name} invalide")

    EPS = 1e-9

    # Connecteurs
    conn = getattr(self, "mt5_connector", None)
    mt5_mod = getattr(self, "mt5", None) or getattr(conn, "mt5", None)

    # Types d’ordre numériques (fallback 0/1 si constants absents)
    ORDER_TYPE_BUY = getattr(mt5_mod or conn, "ORDER_TYPE_BUY", 0)
    ORDER_TYPE_SELL = getattr(mt5_mod or conn, "ORDER_TYPE_SELL", 1)

    # ----- Action -----
    action = str(trade_decision.get("action", "")).upper()
    action = {"LONG": "BUY", "SHORT": "SELL"}.get(action, action)
    if action not in ("BUY", "SELL"):
        raise TradeExecutionError(f"Action invalide pour sizing: '{action}'")
    order_type_i = ORDER_TYPE_BUY if action == "BUY" else ORDER_TYPE_SELL

    # ----- Compte / budget -----
    acct = (context or {}).get("account_info") or {}
    equity = _as_float(acct.get("equity"), "Équité du compte")
    acct_ccy = str(acct.get("currency") or "EUR").upper()

    risk_pct = _as_float(
        (account_trade_settings or {}).get("risk_per_trade_percent"),
        "risk_per_trade_percent",
    )
    if risk_pct <= 0:
        raise TradeExecutionError("risk_per_trade_percent doit être > 0")

    max_risk_amount = equity * (risk_pct / 100.0)  # en devise du compte (ex: EUR)
    if max_risk_amount <= 0:
        raise TradeExecutionError("Budget de risque nul")

    # ----- Scope / burst -----
    strategy = str(
        trade_decision.get("strategy") or trade_decision.get("strategy_type") or ""
    ).lower()
    sizing_scope = str(trade_decision.get("sizing_scope") or "").upper()
    burst_size = int(float(trade_decision.get("burst_size") or 1))
    if burst_size < 1:
        burst_size = 1
    if sizing_scope not in ("SINGLE", "BASKET"):
        sizing_scope = "BASKET" if strategy == "scalping" else "SINGLE"
    # si BASKET → on dimensionne PAR TICKET
    per_ticket_risk = (
        max_risk_amount / burst_size if sizing_scope == "BASKET" else max_risk_amount
    )

    # ----- Prix / distance -----
    entry_price = _as_float(entry_price, "entry_price")
    sl_price = _as_float(sl_price, "sl_price")
    distance = abs(entry_price - sl_price)
    if distance <= 0:
        raise TradeExecutionError("Distance Entry–SL nulle")

    sym_name = (
        _sget(symbol_info, "name", default=str(trade_decision.get("asset", "")).upper())
        or str(trade_decision.get("asset", "")).upper()
    )

    # ===================== Perte par lot (devise compte) =====================
    per_lot_loss = None

    # a) via connecteur sûr
    if conn and hasattr(conn, "safe_order_calc_profit"):
        try:
            p = conn.safe_order_calc_profit(
                order_type_i, sym_name, 1.0, entry_price, sl_price
            )
            if p is not None:
                p = float(p)
                if math.isfinite(p) and p != 0.0:
                    per_lot_loss = abs(p)  # déjà en devise du compte (ex: EUR)
        except Exception:
            per_lot_loss = None

    # b) via MT5 natif
    if per_lot_loss is None and mt5_mod and hasattr(mt5_mod, "order_calc_profit"):
        try:
            p = mt5_mod.order_calc_profit(
                order_type_i, sym_name, 1.0, entry_price, sl_price
            )
            if isinstance(p, (tuple, list)) and p:
                p = p[-1]
            p = float(p)
            if math.isfinite(p) and p != 0.0:
                per_lot_loss = abs(p)
        except Exception:
            per_lot_loss = None

    # c) tick_value / tick_size (MT5 exprime le tick_value en devise compte)
    if per_lot_loss is None:
        tv = _sget(symbol_info, "trade_tick_value", "tick_value", default=None)
        ts = _sget(symbol_info, "trade_tick_size", "tick_size", default=None)
        if tv is not None and ts is not None:
            tv = _as_float(tv, "tick_value")
            ts = _as_float(ts, "tick_size")
            if ts > 0:
                per_lot_loss = (distance / ts) * tv

    # d) heuristique pip-value SÉCURISÉE (on déduit la valeur du pip via order_calc_profit si possible)
    if per_lot_loss is None:
        allow_pip = bool(
            (config.get("risk_management_settings") or {}).get(
                "allow_heuristic_pip_fallback", False
            )
        )
        if not allow_pip:
            raise TradeExecutionError("Impossible de calculer la perte/lot")

        # calc pip_size
        point = _as_float(_sget(symbol_info, "point", default=0.00001), "point")
        digits = int(float(_sget(symbol_info, "digits", default=5)))
        pip_size = point * 10.0 if digits in (3, 5) else point
        if pip_size <= 0:
            raise TradeExecutionError("pip_size invalide")

        # si on dispose d'un calc profit, on mesure la valeur d'1 pip en devise compte
        per_pip_value = None
        try:
            sl_for_1pip = (
                entry_price - pip_size if action == "BUY" else entry_price + pip_size
            )
            if conn and hasattr(conn, "safe_order_calc_profit"):
                pp = conn.safe_order_calc_profit(
                    order_type_i, sym_name, 1.0, entry_price, sl_for_1pip
                )
                if pp is not None:
                    per_pip_value = abs(float(pp))
            elif mt5_mod and hasattr(mt5_mod, "order_calc_profit"):
                pp = mt5_mod.order_calc_profit(
                    order_type_i, sym_name, 1.0, entry_price, sl_for_1pip
                )
                if isinstance(pp, (tuple, list)) and pp:
                    pp = pp[-1]
                per_pip_value = abs(float(pp))
        except Exception:
            per_pip_value = None

        if per_pip_value and math.isfinite(per_pip_value) and per_pip_value > 0:
            per_lot_loss = (distance / pip_size) * per_pip_value
        else:
            # dernier recours : bloquer (pas de conversion devises implicite)
            raise TradeExecutionError(
                "Heuristique pip-value indisponible (pas de calc profit fiable pour convertir en devise compte)"
            )

    if per_lot_loss is None or per_lot_loss <= 0 or not math.isfinite(per_lot_loss):
        raise TradeExecutionError("Perte/lot invalide")

    # ===================== Volume brut (par ticket si basket) =====================
    raw_volume = per_ticket_risk / per_lot_loss
    if raw_volume <= 0 or not math.isfinite(raw_volume):
        raise TradeExecutionError("Volume brut nul")

    # ----- Contraintes & quantification -----
    vol_min_sym = float(_sget(symbol_info, "volume_min", default=0.01) or 0.01)
    vol_max_sym = float(_sget(symbol_info, "volume_max", default=100.0) or 100.0)
    vol_step_sym = float(_sget(symbol_info, "volume_step", default=0.01) or 0.01)

    min_lot_account = float(
        (account_trade_settings or {}).get("min_lot", vol_min_sym) or vol_min_sym
    )
    max_lot_account = float(
        (account_trade_settings or {}).get("max_lot", vol_max_sym) or vol_max_sym
    )
    lot_step_account = float(
        (account_trade_settings or {}).get("lot_step", vol_step_sym) or vol_step_sym
    )

    step = max(lot_step_account, vol_step_sym) or 0.01
    try:
        decimals = max(0, int(round(-math.log10(step)))) if step > 0 else 2
        decimals = min(decimals, 8)
    except Exception:
        decimals = 2

    if max_lot_account < min_lot_account:
        max_lot_account = min_lot_account
    if vol_max_sym < vol_min_sym:
        vol_max_sym = vol_min_sym

    volume_floor = math.floor((raw_volume + EPS) / step) * step
    min_required = max(vol_min_sym, min_lot_account)

    # Si basket et qu'on ne peut pas atteindre le min lot PAR TICKET → on échoue clairement
    if volume_floor + EPS < min_required:
        if sizing_scope == "BASKET" and burst_size > 1:
            total_min = min_required * burst_size
            raise TradeExecutionError(
                f"Budget risque insuffisant pour {burst_size} tickets (min {min_required} chacun, total ≥ {total_min})."
            )
        raise TradeExecutionError("Budget risque trop faible pour le lot minimum")

    volume = min(volume_floor, vol_max_sym, max_lot_account)

    # ===================== Cap marge (après volume) =====================
    try:

        def _calc_margin(vol: float) -> float:
            if conn and hasattr(conn, "safe_order_calc_margin"):
                return float(
                    conn.safe_order_calc_margin(
                        order_type_i, sym_name, vol, entry_price
                    )
                    or 0.0
                )
            if mt5_mod and hasattr(mt5_mod, "order_calc_margin"):
                return float(
                    mt5_mod.order_calc_margin(order_type_i, sym_name, vol, entry_price)
                    or 0.0
                )
            return 0.0  # pas de cap si API indisponible

        free_margin = float(acct.get("margin_free") or 0.0)
        if free_margin > 0:
            need = _calc_margin(volume)
            if need > free_margin > 0:
                ratio = max(free_margin / max(need, 1e-9), 0.0)
                capped = math.floor(((ratio * volume) + EPS) / step) * step
                if capped + EPS < min_required:
                    msg = "Marge insuffisante pour le lot minimum"
                    if sizing_scope == "BASKET" and burst_size > 1:
                        msg += f" (par ticket, basket={burst_size})."
                    raise TradeExecutionError(msg)
                volume = min(capped, vol_max_sym, max_lot_account)
    except Exception:
        # en cas d'erreur marge, on conserve 'volume' (conservateur)
        pass

    # ----- Vérif finale : sous budget (par ticket) -----
    if volume * per_lot_loss > per_ticket_risk + 1e-6:
        vol2 = math.floor(((volume - step) + EPS) / step) * step
        if vol2 + EPS < min_required:
            raise TradeExecutionError("Arrondi impossible sous budget avec min lot")
        volume = vol2

    # ----- Log -----
    try:
        self.logger.info(
            f"[SIZING] {sym_name} | strat={strategy or '-'} scope={sizing_scope} burst={burst_size} "
            f"| equity={equity:.2f} {acct_ccy} risk%={risk_pct:.4f} → risk[{acct_ccy}]={max_risk_amount:.2f} "
            f"| risk_ticket={per_ticket_risk:.2f} {acct_ccy} | per_lot_loss={per_lot_loss:.6f} {acct_ccy} "
            f"→ vol_ticket={volume:.{max(2, decimals)}f}"
        )
    except Exception:
        pass

    return float(round(volume, decimals))
