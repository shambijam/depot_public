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


# trader/sizing.py - Module de Dimensionnement des Positions pour le Bot SNIPER_X
from __future__ import annotations

from typing import Any, Dict, Optional
import math

from trader.errors import TradeExecutionError


# ------------------------------------------------------------
# Utilitaires internes
# ------------------------------------------------------------


def _qdown(val: float, step: float) -> float:
    """Arrondi plancher au pas 'step' (jamais au-dessus du budget)."""
    return max(0.0, math.floor((float(val) + 1e-12) / float(step)) * float(step))


def _as_float(x, name: str) -> float:
    try:
        v = float(x)
        if not math.isfinite(v):
            raise ValueError
        return v
    except Exception:
        raise TradeExecutionError(f"{name} invalide")


def _sget(obj, *names, default=None):
    """getattr/[] tolérant sur dict/obj."""
    for n in names:
        if hasattr(obj, n):
            v = getattr(obj, n)
            if v is not None:
                return v
        if isinstance(obj, dict) and obj.get(n) is not None:
            return obj[n]
    return default


# ------------------------------------------------------------
# Sizing compact (utilitaire)
# ------------------------------------------------------------


def compute_lot_from_risk(
    symbol_info: Any,
    account_info: Any,
    entry: float,
    sl: float,
    risk_pct: float,
    confidence: float = 1.0,  # ignoré (compat)
    burst_size: int = 1,
    atr: Optional[float] = None,  # ignoré (compat)
    atr_ref: Optional[float] = 10.0,  # ignoré (compat)
) -> float:
    """
    Sizing STRICT basé uniquement sur risk_per_trade_percent.
    - Division du budget si 'burst_size' > 1 (budget PAR TICKET).
    - FLOOR au pas broker (jamais au-dessus du budget).
    - Cap par marge si infos dispo ; si le budget ne permet pas d'atteindre vmin → 0.0.
    """
    # 0) Entrées
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

    # 1) Budget strict
    risk_amt = balance * (risk_pct / 100.0)
    try:
        burst_size = int(burst_size or 1)
    except Exception:
        burst_size = 1
    if burst_size > 1:
        risk_amt /= float(burst_size)
    if risk_amt <= 0:
        return 0.0

    # 2) Perte/lot (devise compte) par distance Entry–SL
    distance = abs(entry - sl)
    if distance <= 0:
        return 0.0

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

    # 3) Volume brut
    lots_raw = risk_amt / per_lot_loss
    if not math.isfinite(lots_raw) or lots_raw <= 0:
        return 0.0

    # 4) Contraintes broker + FLOOR
    vmin = float(getattr(symbol_info, "volume_min", 0.01) or 0.01)
    vstep = float(getattr(symbol_info, "volume_step", 0.01) or 0.01)
    vmax = float(getattr(symbol_info, "volume_max", 100.0) or 100.0)
    if vmin <= 0 or vstep <= 0 or vmax <= 0 or vmax < vmin:
        vmin, vstep, vmax = 0.01, 0.01, 100.0

    try:
        decimals = max(0, int(round(-math.log10(vstep)))) if vstep > 0 else 2
    except Exception:
        decimals = 2

    lots_cap = min(lots_raw, vmax)
    lots_q = _qdown(lots_cap, vstep)
    if lots_q < vmin:
        return 0.0

    # 5) Cap marge (si dispo)
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


# ------------------------------------------------------------
# Sizing principal utilisé par l'exécuteur
# ------------------------------------------------------------


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
    - Scope: SINGLE / BASKET (si BASKET, retourne volume PAR TICKET = risque_total/burst_size)
    """
    EPS = 1e-9

    # ----- Action -----
    action = str(trade_decision.get("action", "")).upper()
    action = {"LONG": "BUY", "SHORT": "SELL"}.get(action, action)
    if action not in ("BUY", "SELL"):
        raise TradeExecutionError(f"Action invalide pour sizing: '{action}'")

    # ----- Compte / budget -----
    acct = (context or {}).get("account_info") or {}
    equity = _as_float(acct.get("equity"), "Équité du compte")

    risk_pct = _as_float(
        (account_trade_settings or {}).get("risk_per_trade_percent"),
        "risk_per_trade_percent",
    )
    if risk_pct <= 0:
        raise TradeExecutionError("risk_per_trade_percent doit être > 0")

    max_risk_amount = equity * (risk_pct / 100.0)
    if max_risk_amount <= 0:
        raise TradeExecutionError("Budget de risque nul")

    # ----- Scope / burst -----
    rule_name = str(trade_decision.get("rule_name", "")).lower()
    strategy = str(
        trade_decision.get("strategy") or trade_decision.get("strategy_type") or ""
    ).lower()

    sizing_scope = str(trade_decision.get("sizing_scope") or "").upper()
    burst_size = int(float(trade_decision.get("burst_size") or 1))
    if burst_size < 1:
        burst_size = 1

    # Force BASKET pour single_master/burst_scalping (pour bien DIVISER le risque)
    if not sizing_scope:
        sizing_scope = (
            "BASKET"
            if rule_name in {"burst_scalping", "burst_single_master"}
            else ("BASKET" if strategy == "scalping" else "SINGLE")
        )
    elif sizing_scope not in {"SINGLE", "BASKET"}:
        sizing_scope = (
            "BASKET"
            if rule_name in {"burst_scalping", "burst_single_master"}
            else "SINGLE"
        )

    # → si BASKET ⇒ risque PAR TICKET = risque_total / burst_size
    per_ticket_risk = (
        (max_risk_amount / burst_size) if sizing_scope == "BASKET" else max_risk_amount
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

    # =========================================================
    # Perte par lot (devise compte) — sans dépendances globales
    # =========================================================
    per_lot_loss: Optional[float] = None

    # 1) tick_value/tick_size (MT5 exprime le tick_value en devise compte)
    tv = _sget(symbol_info, "trade_tick_value", "tick_value", default=None)
    ts = _sget(symbol_info, "trade_tick_size", "tick_size", default=None)
    try:
        if tv is not None and ts is not None:
            tv = _as_float(tv, "tick_value")
            ts = _as_float(ts, "tick_size")
            if ts > 0:
                per_lot_loss = (distance / ts) * tv
    except Exception:
        per_lot_loss = None

    # 2) fallback contract_size si besoin
    if per_lot_loss is None:
        contract_size = float(
            _sget(symbol_info, "trade_contract_size", "contract_size", default=100.0)
            or 100.0
        )
        per_lot_loss = distance * contract_size

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

    return round(volume, decimals)
