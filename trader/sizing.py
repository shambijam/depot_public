# trader/sizing.py - Module de Dimensionnement des Positions pour le Bot SNIPER_X
from __future__ import annotations

from typing import Any, Dict, Optional
import math
import logging

from trader.errors import TradeExecutionError


# ==============================
# === Utilitaires internes =====
# ==============================


def _qdown(val: float, step: float) -> float:
    """Arrondi plancher au pas 'step' (jamais au-dessus du budget)."""
    return max(0.0, math.floor((float(val) + 1e-12) / float(step)) * float(step))


def _as_float(x, name: str) -> float:
    try:
        # ⚠️ FIX: Fallback si None (surtout pour equity)
        if x is None:
            if "équité" in name.lower() or "equity" in name.lower():
                import os
                fallback = float(os.getenv("SNIPERX_DEFAULT_EQUITY", "10000"))
                # Pas de logger ici, on utilise print ou on laisse silencieux
                print(f"⚠️ [SIZING] {name} manquante → fallback à {fallback}")
                return fallback

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

    # Force BASKET pour burst_scalping (pour bien DIVISER le risque)
    if not sizing_scope:
        sizing_scope = (
            "BASKET"
            if rule_name == "burst_scalping"
            else ("BASKET" if strategy == "scalping" else "SINGLE")
        )
    elif sizing_scope not in {"SINGLE", "BASKET"}:
        sizing_scope = (
            "BASKET"
            if rule_name == "burst_scalping"
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

    # DEBUG LOG
    import logging
    logger = logging.getLogger(__name__)
    logger.critical(f"🔍 [SIZING] {sym_name} | distance={distance:.6f} | entry={entry_price:.6f} | sl={sl_price:.6f}")
    logger.critical(f"🔍 [SIZING] tick_value={tv} | tick_size={ts} | burst_size={burst_size} | scope={sizing_scope}")
    logger.critical(f"🔍 [SIZING] equity={equity:.2f} | risk%={risk_pct} | max_risk_amount={max_risk_amount:.2f} | per_ticket_risk={per_ticket_risk:.2f}")

    # Méthode UNIQUE : tick_value / tick_size (pas de fallback toxique)
    if tv is None or ts is None:
        raise TradeExecutionError(
            f"[SIZING] tick_value ou tick_size manquant pour {sym_name}. "
            f"tick_value={tv}, tick_size={ts}. "
            f"Vérifiez mt5_connector.get_symbol_info() - le SymbolInfoFallback doit contenir trade_tick_value."
        )

    try:
        tv = _as_float(tv, "tick_value")
        ts = _as_float(ts, "tick_size")
        if ts <= 0:
            raise TradeExecutionError(f"[SIZING] tick_size invalide: {ts}")

        per_lot_loss = (distance / ts) * tv
        logger.critical(f"✅ [SIZING] MÉTHODE UNIQUE (tick): per_lot_loss={per_lot_loss:.2f} $")

    except Exception as e:
        logger.critical(f"❌ [SIZING] Erreur calcul per_lot_loss: {e}")
        raise TradeExecutionError(f"[SIZING] Erreur calcul per_lot_loss: {e}")

    if per_lot_loss <= 0 or not math.isfinite(per_lot_loss):
        raise TradeExecutionError(f"[SIZING] Perte/lot invalide: {per_lot_loss}")

    # ===================== Volume brut (par ticket si basket) =====================
    raw_volume = per_ticket_risk / per_lot_loss
    logger.critical(f"📊 [SIZING] CALCUL: {per_ticket_risk:.2f} $ / {per_lot_loss:.2f} $ = {raw_volume:.6f} lots (brut)")
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
    logger.critical(f"🔧 [SIZING] volume_floor={volume_floor:.6f} | min_required={min_required} | step={step}")

    # Si basket et qu'on ne peut pas atteindre le min lot PAR TICKET → on échoue clairement
    if volume_floor + EPS < min_required:
        if sizing_scope == "BASKET" and burst_size > 1:
            total_min = min_required * burst_size
            raise TradeExecutionError(
                f"Budget risque insuffisant pour {burst_size} tickets (min {min_required} chacun, total ≥ {total_min})."
            )
        raise TradeExecutionError("Budget risque trop faible pour le lot minimum")

    volume = min(volume_floor, vol_max_sym, max_lot_account)
    logger.critical(f"✅ [SIZING] FINAL: volume={volume:.6f} lots (decimals={decimals})")

    return round(volume, decimals)
