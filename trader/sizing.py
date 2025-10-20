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
    STRICT RISK% — Fonction unique de sizing (sans garde-fous ni modulations).
    - Source unique du risque%: account_trade_settings["risk_per_trade_percent"] (obligatoire).
    - Aucun clip min/max "vers le haut", aucune modulation (confidence/quality/volatility).
    - Distance basée sur le SL réel (pas de floors ATR/spread).
    - Heuristique pip-value utilisée uniquement si autorisée par conf.
    - Quantification FLOOR au pas lot pour ne jamais dépasser le budget.
    - 'sizing_scope': SINGLE (Liquidity) / BASKET (Scalping, volume retourné = PAR TICKET).
    Lève TradeExecutionError si non calculable.
    """
    import math

    # --- Helpers locals ---
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

    # --- Action ---
    action = str(trade_decision.get("action", "")).upper()
    action = {"LONG": "BUY", "SHORT": "SELL"}.get(action, action)
    if action not in ("BUY", "SELL"):
        raise TradeExecutionError(f"Action invalide pour sizing: '{action}'")

    # --- Données compte ---
    equity = _as_float(
        ((context or {}).get("account_info") or {}).get("equity"), "Équité du compte"
    )
    risk_pct = _as_float(
        (account_trade_settings or {}).get("risk_per_trade_percent"),
        "risk_per_trade_percent",
    )
    if risk_pct <= 0:
        raise TradeExecutionError("risk_per_trade_percent doit être > 0")

    # --- Budget STRICT (aucune modulation) ---
    max_dollar_risk = equity * (risk_pct / 100.0)
    if max_dollar_risk <= 0:
        raise TradeExecutionError("Budget de risque nul")

    # --- Scope (Scalping vs Liquidity) ---
    strategy = str(
        trade_decision.get("strategy") or trade_decision.get("strategy_type") or ""
    ).lower()
    sizing_scope = str(trade_decision.get("sizing_scope") or "").upper()
    burst_size = int(float(trade_decision.get("burst_size") or 1))
    if burst_size < 1:
        burst_size = 1
    if sizing_scope not in ("SINGLE", "BASKET"):
        sizing_scope = "BASKET" if strategy == "scalping" else "SINGLE"
    if sizing_scope == "BASKET" and burst_size > 1:
        max_dollar_risk = max_dollar_risk / burst_size

    # --- Distances/prix (SL réel) ---
    entry_price = _as_float(entry_price, "entry_price")
    sl_price = _as_float(sl_price, "sl_price")
    distance = abs(entry_price - sl_price)
    if distance <= 0:
        raise TradeExecutionError("Distance Entry–SL nulle")

    # --- Perte $ par lot ---
    sym_name = _sget(
        symbol_info, "name", default=str(trade_decision.get("asset", "")).upper()
    )
    per_lot_loss = None

    # 1) MT5 order_calc_profit
    mt5_mod = getattr(self, "mt5", None) or getattr(
        getattr(self, "mt5_connector", None), "mt5", None
    )
    if mt5_mod:
        try:
            order_type = (
                getattr(mt5_mod, "ORDER_TYPE_BUY", 0)
                if action == "BUY"
                else getattr(mt5_mod, "ORDER_TYPE_SELL", 1)
            )
            profit = mt5_mod.order_calc_profit(
                order_type, sym_name, 1.0, entry_price, sl_price
            )
            if isinstance(profit, (tuple, list)) and profit:
                profit = profit[-1]
            profit = float(profit)
            if math.isfinite(profit) and profit != 0.0:
                per_lot_loss = abs(profit)
        except Exception:
            per_lot_loss = None

    # 2) tick_value / tick_size
    if per_lot_loss is None:
        tv = _sget(symbol_info, "trade_tick_value", "tick_value", default=None)
        ts = _sget(symbol_info, "trade_tick_size", "tick_size", default=None)
        if tv is not None and ts is not None:
            tv = _as_float(tv, "tick_value")
            ts = _as_float(ts, "tick_size")
            if ts > 0:
                per_lot_loss = (distance / ts) * tv

    # 3) heuristique pip-value (optionnelle)
    if per_lot_loss is None:
        allow_pip = bool(
            (config.get("risk_management_settings") or {}).get(
                "allow_heuristic_pip_fallback", False
            )
        )
        if not allow_pip:
            raise TradeExecutionError(
                "Impossible de calculer la perte/lot (pas d'heuristique autorisée)"
            )
        point = _as_float(_sget(symbol_info, "point", default=0.00001), "point")
        digits = int(float(_sget(symbol_info, "digits", default=5)))
        pip_size = point * 10.0 if digits in (3, 5) else point
        contract = _as_float(
            _sget(
                symbol_info, "trade_contract_size", "contract_size", default=100000.0
            ),
            "contract_size",
        )
        if pip_size <= 0:
            raise TradeExecutionError("pip_size invalide")
        per_pip_value_per_lot = contract * pip_size  # devise de cotation supposée USD
        per_lot_loss = (distance / pip_size) * per_pip_value_per_lot

    if per_lot_loss is None or per_lot_loss <= 0 or not math.isfinite(per_lot_loss):
        raise TradeExecutionError("Perte/lot invalide")

    # --- Volume brut ---
    raw_volume = max_dollar_risk / per_lot_loss
    if raw_volume <= 0 or not math.isfinite(raw_volume):
        raise TradeExecutionError("Volume brut nul")

    # --- Contraintes symbole/compte + quantification FLOOR ---
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

    # Step et décimales
    step = max(lot_step_account, vol_step_sym)
    if step <= 0:
        step = 0.01
    try:
        decimals = max(0, int(round(-math.log10(step)))) if step > 0 else 2
        decimals = min(decimals, 8)
    except Exception:
        decimals = 2

    # Sanity sur bornes
    if max_lot_account < min_lot_account:
        max_lot_account = min_lot_account
    if vol_max_sym < vol_min_sym:
        vol_max_sym = vol_min_sym

    # Floor au pas lot (NE JAMAIS AUGMENTER)
    volume_floor = math.floor((raw_volume + EPS) / step) * step

    # Si en-dessous des minima (symbole/compte), on ne force pas vers le haut → insuffisant
    min_required = max(vol_min_sym, min_lot_account)
    if volume_floor + EPS < min_required:
        raise TradeExecutionError("Budget risque trop faible pour le lot minimum")

    # Clamp DOWN vers les maxima autorisés
    volume = min(volume_floor, vol_max_sym, max_lot_account)

    # --- Cap marge (si dispo) ---
    try:
        if mt5_mod and hasattr(mt5_mod, "order_calc_margin"):
            order_type = (
                getattr(mt5_mod, "ORDER_TYPE_BUY", 0)
                if action == "BUY"
                else getattr(mt5_mod, "ORDER_TYPE_SELL", 1)
            )
            margin_required = float(
                mt5_mod.order_calc_margin(order_type, sym_name, volume, entry_price)
                or 0.0
            )
            free_margin = float(
                ((context or {}).get("account_info") or {}).get("margin_free") or 0.0
            )
            if (
                margin_required > 0
                and free_margin > 0
                and margin_required > free_margin
            ):
                ratio = max(free_margin / margin_required, 0.0)
                capped = math.floor(((ratio * volume) + EPS) / step) * step
                if capped + EPS < min_required:
                    raise TradeExecutionError("Marge insuffisante pour le lot minimum")
                volume = min(capped, vol_max_sym, max_lot_account)
    except Exception:
        # En cas d'erreur MT5, on retombe sur le volume déjà flooré (conservateur)
        pass

    # --- Vérif finale : ne pas dépasser le budget ---
    actual_risk = volume * per_lot_loss
    if actual_risk > max_dollar_risk + 1e-6:
        vol2 = math.floor(((volume - step) + EPS) / step) * step
        if vol2 + EPS < min_required:
            raise TradeExecutionError("Arrondi impossible sous budget avec min lot")
        volume = vol2

    # --- Log synthétique (tolérant) ---
    try:
        self.logger.info(
            f"[SIZING] {sym_name} | strategy={strategy or '-'} scope={sizing_scope} burst={burst_size} "
            f"| equity={equity:.2f} risk%={risk_pct:.4f} -> risk$={max_dollar_risk:.2f} "
            f"| per_lot_loss={per_lot_loss:.6f} -> vol={volume:.{max(2, decimals)}f}"
        )
    except Exception:
        pass

    return float(round(volume, decimals))
