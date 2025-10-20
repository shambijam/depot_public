# trader/burst.py - Module de Gestion du salping_basket

from __future__ import annotations

import logging
import re
import time
import pandas as pd
import math
import hashlib
import random
from typing import Any, Dict, Any, Optional
from datetime import datetime, timedelta, UTC, timezone


# ==============================
# === Helpers Trading Utils ====
# ==============================


def _attach_burst_metadata(self, trade_decision: dict) -> dict:
    """
    Attache des métadonnées de burst (basket_id, horodatage, etc.)
    à une décision de trade unique.
    """
    import time, uuid

    if not trade_decision:
        return trade_decision

    basket_id = trade_decision.get("basket_id") or f"burst_{uuid.uuid4().hex[:8]}"
    trade_decision["basket_id"] = basket_id
    trade_decision["burst_timestamp"] = int(time.time())
    trade_decision.setdefault("meta", {})["burst"] = True

    return trade_decision


def close_burst_basket(self, basket_id: str):
    """
    Ferme immédiatement toutes les positions appartenant au même burst `basket_id`.

    • Trouve les positions du panier (basket_id/burst_id/comment avec motif strict).
    • Tente une fermeture 'bulk' ; post-vérifie que tout est fermé.
    • Fallback: ferme ticket par ticket via self.close_position(ticket=...), avec post-vérif.
    • Annule UNIQUEMENT les ordres en attente qui portent le `basket_id` dans le commentaire.
    • Mode urgence: pousse un SL au marché (buffer = max(tick, stops_level*point, freeze*point)+1*tick) sans jamais détendre un SL existant.
    • Purge des états internes (trailing/lock) + cooldown par symbole. Déverrouillage assuré (finally).
    """
    import re, time

    # ---------- Guards ----------
    if not basket_id or not str(basket_id).strip():
        self.logger.warning("[CLOSE] appelé sans basket_id")
        return {"closed": False, "reason": "no_basket_id"}

    mt5c = getattr(self, "mt5_connector", None)
    if not mt5c:
        self.logger.error("[CLOSE] mt5_connector indisponible.")
        return {"closed": False, "reason": "no_connector"}
    mt5 = getattr(mt5c, "mt5", None)
    if not mt5:
        self.logger.error("[CLOSE] module MT5 indisponible sur le connecteur.")
        return {"closed": False, "reason": "no_mt5_module"}

    # ---------- Budget de latence (option) ----------
    try:
        max_close_ms = int(
            self.config_manager.get("dynamic_trailing.max_latency_ms_on_close", 0) or 0
        )
    except Exception:
        max_close_ms = 0
    start_ms = int(time.time() * 1000)

    def _time_left_ok() -> bool:
        if max_close_ms <= 0:
            return True
        return (int(time.time() * 1000) - start_ms) < max_close_ms

    # ---------- State structs ----------
    if not hasattr(self, "_basket_peak_pips"):
        self._basket_peak_pips = {}
    if not hasattr(self, "_basket_trail_armed"):
        self._basket_trail_armed = {}
    if not hasattr(self, "_burst_trailing_state"):
        self._burst_trailing_state = {}
    if not hasattr(self, "_active_burst_locks"):
        self._active_burst_locks = {}
    if not hasattr(self, "_closing_baskets"):
        self._closing_baskets = set()
    if not hasattr(self, "_cooldown_until"):
        self._cooldown_until = {}

    # ---------- Lock concurrent ----------
    if basket_id in self._closing_baskets:
        self.logger.info(f"[CLOSE] Ignoré: '{basket_id}' déjà en fermeture.")
        return {"closed": False, "reason": "already_closing"}
    self._closing_baskets.add(basket_id)

    # ---------- Helpers ----------
    def _v(p, key, d=None):
        if isinstance(p, dict):
            return p.get(key, d)
        return getattr(p, key, d)

    def _safe_float(x, d=None):
        try:
            return float(x)
        except Exception:
            return d

    def _entry_price(p):
        ep = _safe_float(_v(p, "entry_price"))
        return ep if ep is not None else _safe_float(_v(p, "price_open"))

    def _extract_basket_id(pos):
        bid = _v(pos, "basket_id") or _v(pos, "burst_id")
        if bid:
            return str(bid)
        c = str(_v(pos, "comment", "") or "")
        m = re.search(r"burst_scalping\|(?:[^|]*\|){0,5}?basket=([A-Za-z0-9_]+)", c)
        if m:
            return m.group(1)
        m = re.search(r"(burst_[A-Z]{3,6}_[a-f0-9]{6,})", c, re.IGNORECASE)
        if m:
            return m.group(1)
        sym = str(_v(pos, "symbol", "") or "").upper()
        magic = _v(pos, "magic") or ""
        ep = _safe_float(_entry_price(pos), 0.0)
        ep_key = f"{ep:.2f}" if ep is not None else "na"
        return f"synthetic|{sym}|{magic}|{ep_key}"

    def _list_open_positions():
        try:
            return mt5c.get_positions() or []
        except Exception as e:
            self.logger.error(f"[CLOSE] impossible de lire les positions: {e}")
            return []

    def _list_pending_orders():
        try:
            if hasattr(mt5c, "get_orders"):
                return mt5c.get_orders() or []
            if hasattr(mt5, "orders_get"):
                return mt5.orders_get() or []
        except Exception:
            pass
        return []

    def _cancel_pending_orders_for_basket(bid: str):
        """Annule SEULEMENT les ordres dont le commentaire contient explicitement le basket_id (substring)."""
        pend = _list_pending_orders()
        if not pend:
            return 0
        cancelled = 0
        for od in pend:
            if not _time_left_ok():
                self.logger.warning(
                    "[CLOSE] Budget de latence atteint pendant l'annulation des pendings."
                )
                break
            try:
                comment = str(_v(od, "comment", "") or "")
                if bid and (bid in comment):
                    order_id = _v(od, "order") or _v(od, "ticket")
                    if order_id is None:
                        continue
                    req = {
                        "action": mt5.TRADE_ACTION_REMOVE,
                        "order": int(order_id),
                    }
                    res = mt5c.order_send(req)
                    if res and getattr(res, "retcode", None) == mt5.TRADE_RETCODE_DONE:
                        cancelled += 1
                        self.logger.info(
                            f"[CLOSE] Pending #{order_id} annulé (basket={bid})."
                        )
                    else:
                        self.logger.warning(
                            f"[CLOSE] Annulation ordre #{order_id} échec retcode={getattr(res,'retcode',None)}"
                        )
            except Exception as e:
                self.logger.error(f"[CLOSE] Annulation ordre KO: {e}")
        return cancelled

    def _force_sl_sweep(symbol: str, positions: list):
        """
        Pousse un SL quasi-au-marché pour forcer la sortie (sans détendre).
        Buffer = max(tick_size, stops_level*point, freeze_level*point) + 1*tick.
        """
        try:
            si = mt5.symbol_info(symbol)
        except Exception:
            si = None
        if not si:
            return False

        digits = int(getattr(si, "digits", 5) or 5)
        point = _safe_float(getattr(si, "point", None), 0.0001) or 0.0001
        tick = _safe_float(getattr(si, "trade_tick_size", None), point) or point
        stops = int(getattr(si, "trade_stops_level", 0) or 0)
        freeze = int(getattr(si, "trade_freeze_level", 0) or 0)

        try:
            t = mt5.symbol_info_tick(symbol)
            bid = _safe_float(getattr(t, "bid", None))
            ask = _safe_float(getattr(t, "ask", None))
        except Exception:
            bid = ask = None

        buf = max(tick, stops * point, freeze * point) + tick

        ok = ko = 0
        for p in positions:
            if not _time_left_ok():
                self.logger.warning("[EMERGENCY-SL] Budget de latence atteint.")
                break
            try:
                tk = _v(p, "ticket")
                typ = _v(p, "type")  # 0=BUY / 1=SELL
                if tk is None or typ not in (0, 1):
                    continue

                if typ == 0:  # BUY
                    ref = bid if bid is not None else _safe_float(_v(p, "bid"))
                    if ref is None:
                        continue
                    new_sl = ref - buf
                else:  # SELL
                    ref = ask if ask is not None else _safe_float(_v(p, "ask"))
                    if ref is None:
                        continue
                    new_sl = ref + buf

                cur_sl = _safe_float(_v(p, "sl"))

                # ne pas détendre
                if typ == 0 and cur_sl is not None and new_sl <= cur_sl:
                    continue
                if typ == 1 and cur_sl is not None and new_sl >= cur_sl:
                    continue

                req = {
                    "action": mt5.TRADE_ACTION_SLTP,
                    "symbol": symbol,
                    "position": int(tk),
                    "sl": round(float(new_sl), digits),
                    "tp": _safe_float(_v(p, "tp"), 0.0) or 0.0,
                }
                res = mt5c.order_send(req)
                if res and getattr(res, "retcode", None) == mt5.TRADE_RETCODE_DONE:
                    ok += 1
                else:
                    ko += 1
            except Exception as e:
                ko += 1
                self.logger.error(f"[EMERGENCY-SL] pos#{_v(p,'ticket')} KO: {e}")

        if ok > 0:
            self.logger.warning(
                f"[EMERGENCY-SL] {symbol} SL poussés ({ok} ok / {ko} ko)."
            )
        return ok > 0

    def _purge_states(bid: str, symbol_hint: str = None):
        self._basket_peak_pips.pop(bid, None)
        self._basket_trail_armed.pop(bid, None)
        self._burst_trailing_state.pop(bid, None)

        # COOLDOWN post-exit par symbole
        try:
            cd = float(self.config_manager.get("cooldown_after_exit_s", 0) or 0.0)
            if cd <= 0:
                cd = float(
                    self.config_manager.get(
                        "entry_rules.scalping.burst_scalping.cooldown_after_exit_s",
                        0,
                    )
                    or 0.0
                )
            if cd > 0 and symbol_hint:
                import time as _t

                self._cooldown_until[str(symbol_hint).upper()] = _t.time() + cd
                self.logger.info(
                    f"[COOLDOWN] {str(symbol_hint).upper()} bloqué {int(cd)}s après fermeture panier '{bid}'."
                )
        except Exception as _e:
            self.logger.warning(f"[COOLDOWN] set KO: {_e}")

        if symbol_hint:
            self._active_burst_locks.pop(str(symbol_hint).upper(), None)
        else:
            to_del = None
            for k, v in list(self._active_burst_locks.items()):
                try:
                    if (v or {}).get("basket_id") == bid:
                        to_del = k
                        break
                except Exception:
                    pass
            if to_del is not None:
                self._active_burst_locks.pop(to_del, None)

    # ---------- Core with guaranteed unlock ----------
    cancelled = 0
    try:
        # 1) positions du panier
        all_pos = _list_open_positions()
        basket_pos = [p for p in all_pos if _extract_basket_id(p) == basket_id]

        if not basket_pos:
            self.logger.info(f"[CLOSE] Aucune position pour basket '{basket_id}'")
            _purge_states(basket_id, None)
            return {
                "closed": True,
                "tickets_total": 0,
                "tickets_closed": 0,
                "pending_cancelled": 0,
                "forced_sl": False,
            }

        symbol_hint = str(_v(basket_pos[0], "symbol", "") or "").upper()

        # 2) annule pendings attachés au basket (avant de fermer)
        if _time_left_ok():
            cancelled = _cancel_pending_orders_for_basket(basket_id)
        else:
            self.logger.warning(
                "[CLOSE] Budget de latence atteint avant l'annulation des pendings."
            )

        # 3) Bulk close si dispo + post-check rapide
        tickets = []
        for p in basket_pos:
            tk = _v(p, "ticket")
            if tk is not None:
                try:
                    tickets.append(int(tk))
                except Exception:
                    self.logger.warning(f"[CLOSE] Ticket invalide: {tk}")

        tickets_closed = 0
        tickets_failed = 0
        forced_sl = False

        if (
            tickets
            and hasattr(mt5c, "close_positions")
            and callable(getattr(mt5c, "close_positions"))
            and _time_left_ok()
        ):
            try:
                mt5c.close_positions(tickets=tickets)
                time.sleep(0.05)  # petit poll
                left = [
                    p
                    for p in _list_open_positions()
                    if _extract_basket_id(p) == basket_id
                ]
                if not left:
                    self.logger.info(
                        f"[CLOSE] Panier '{basket_id}' fermé (bulk). {len(tickets)} tickets."
                    )
                    _purge_states(basket_id, symbol_hint)
                    return {
                        "closed": True,
                        "tickets_total": len(tickets),
                        "tickets_closed": len(tickets),
                        "pending_cancelled": cancelled,
                        "forced_sl": False,
                    }
                else:
                    self.logger.warning(
                        f"[CLOSE] Bulk partielle: {len(left)} restants → fallback tickets."
                    )
                    tickets = []
                    for p in left:
                        tk = _v(p, "ticket")
                        if tk is not None:
                            try:
                                tickets.append(int(tk))
                            except Exception:
                                pass
            except Exception as e:
                self.logger.error(f"[CLOSE] bulk close_positions KO: {e}")

        # 4) Fallback ticket par ticket via TA fonction robuste
        if tickets and _time_left_ok():
            for tk in tickets:
                if not _time_left_ok():
                    self.logger.warning(
                        "[CLOSE] Budget de latence atteint pendant le fallback ticket-by-ticket."
                    )
                    break
                try:
                    ret = self.close_position(
                        ticket=int(tk)
                    )  # ✅ utilise ta version avec retry/slippage
                    if ret and ret.get("success"):
                        # post-vérif ticket
                        time.sleep(0.02)
                        still = [
                            p for p in _list_open_positions() if _v(p, "ticket") == tk
                        ]
                        if still:
                            tickets_failed += 1
                            self.logger.warning(
                                f"[CLOSE] ticket {tk} non fermé (fallback)."
                            )
                        else:
                            tickets_closed += 1
                    else:
                        tickets_failed += 1
                except Exception as e:
                    tickets_failed += 1
                    self.logger.error(f"[CLOSE] Échec clôture ticket {tk}: {e}")

        # 5) Si il reste quelque chose → mode urgence SL balai
        left_now = [
            p for p in _list_open_positions() if _extract_basket_id(p) == basket_id
        ]
        if left_now and _time_left_ok():
            forced_sl = _force_sl_sweep(symbol_hint, left_now)
            if forced_sl:
                self.logger.warning(
                    f"[CLOSE] Fermeture forcée par SL lancée (basket '{basket_id}')."
                )

        # 6) post état final
        final_left = [
            p for p in _list_open_positions() if _extract_basket_id(p) == basket_id
        ]
        all_closed = len(final_left) == 0

        if all_closed:
            self.logger.info(
                f"[CLOSE] Panier '{basket_id}' fermé. closed={tickets_closed} failed={tickets_failed} "
                f"cancelled={cancelled} forced_sl={forced_sl}"
            )
        else:
            self.logger.warning(
                f"[CLOSE] Panier '{basket_id}' PARTIEL. restants={len(final_left)} | closed={tickets_closed} "
                f"failed={tickets_failed} cancelled={cancelled} forced_sl={forced_sl}"
            )

        _purge_states(basket_id, symbol_hint)
        return {
            "closed": all_closed,
            "tickets_total": len(basket_pos),
            "tickets_closed": tickets_closed,
            "tickets_failed": tickets_failed,
            "pending_cancelled": cancelled,
            "forced_sl": forced_sl,
        }

    finally:
        # déverrouillage inconditionnel
        self._closing_baskets.discard(basket_id)
        
def _get_ref_price(self, symbol: str, is_buy: bool, entry_style: str):
    """
    Donne un prix de référence POSITIF et cohérent avec le côté.
    - Pour FOK (market-like) : BUY → ask, SELL → bid
    - Pour LIMIT (pending) : on place au meilleur côté (BUY → bid, SELL → ask)
    Fallback: dernière close valide si tick absent.
    """
    mt5 = getattr(self.mt5_connector, "mt5", None)
    tick = mt5.symbol_info_tick(symbol) if mt5 else None
    bid = float(getattr(tick, "bid", 0.0) or 0.0)
    ask = float(getattr(tick, "ask", 0.0) or 0.0)

    if entry_style.endswith("FOK"):
        ref = ask if is_buy else bid
    else:
        ref = bid if is_buy else ask

    if ref <= 0.0:
        # petit filet de sécurité : on tente la dernière close M1
        rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M1, 0, 1) if mt5 else None
        if rates and len(rates) and float(rates[0]["close"]) > 0:
            ref = float(rates[0]["close"])

    return ref if ref > 0.0 else None


def execute_burst_scalping_order(self, decision: dict, config: dict) -> bool:
    """
    Burst scalping AVEC SL obligatoire (jamais sans SL).
    - Sizing = (equity * risk_per_trade_percent/100) / burst_size  → cash/child
    - Volume/enfant = cash/child / (|entry - SL| * $/prix/lot)
    - Tous les enfants même volume, quantifiés au pas lot.
    - Si INVALID_STOPS: on élargit le SL (buffer) et on re-tente (max 2). JAMAIS d'envoi sans SL.
    - Si un enfant échoue → rollback (annule pendings + ferme positions de ce panier).
    """
    import math, time, uuid

    mt5 = getattr(self.mt5_connector, "mt5", None)
    if not mt5:
        self.logger.error("[BURST] MT5 indisponible.")
        return False

    # ---------------- helpers ----------------
    def _safe_f(x, d=0.0):
        try:
            v = float(x)
            return v if math.isfinite(v) else d
        except Exception:
            return d

    def _tick(symbol):
        try:
            t = mt5.symbol_info_tick(symbol)
            return float(getattr(t, "bid", 0) or 0), float(getattr(t, "ask", 0) or 0)
        except Exception:
            return 0.0, 0.0

    def _si(symbol):
        try:
            s = mt5.symbol_info(symbol)
        except Exception:
            s = None
        if not s:
            return None
        return {
            "digits": int(getattr(s, "digits", 5) or 5),
            "point": float(getattr(s, "point", 10**-5) or 10**-5),
            "vol_min": float(getattr(s, "trade_min_volume", getattr(s, "volume_min", 0.01)) or 0.01),
            "vol_step": float(getattr(s, "trade_volume_step", getattr(s, "volume_step", 0.01)) or 0.01),
            "vol_max": float(getattr(s, "trade_max_volume", getattr(s, "volume_max", 100.0)) or 100.0),
            "stops": int(getattr(s, "trade_stops_level", getattr(s, "stops_level", 0)) or 0),
            "freeze": int(getattr(s, "trade_freeze_level", getattr(s, "freeze_level", 0)) or 0),
            "spread": int(getattr(s, "spread", 0) or 0),
            "tick_value": float(getattr(s, "trade_tick_value", getattr(s, "tick_value", 0.0)) or 0.0),
            "tick_size": float(getattr(s, "trade_tick_size", getattr(s, "tick_size", 0.0)) or 0.0),
            "contract": float(getattr(s, "trade_contract_size", getattr(s, "contract_size", 0.0)) or 0.0),
        }

    def _value_per_1price_per_lot(si):
        tv, ts, cs = si["tick_value"], si["tick_size"], si["contract"]
        if tv > 0 and ts > 0:      # $ par tick_size → $ par 1.0 prix
            return tv / ts
        if cs > 0:
            return cs
        return 0.0

    def _quant(v, step, vmin, vmax):
        if step <= 0: return max(vmin, min(vmax, v))
        n = math.floor(max(0.0, v - vmin) / step + 1e-12)
        return max(vmin, min(vmax, round(vmin + n * step, 8)))

    def _enforce_sl(side, ref_px, sl, si, extra_pts):
        pt = si["point"]; digits = si["digits"]
        min_pts = max(si["stops"], si["freeze"], si["spread"]) + max(0, int(extra_pts))
        min_dist = min_pts * pt
        adj = float(sl)
        if side == "BUY":
            lim = ref_px - min_dist
            if adj >= lim: adj = lim
        else:
            lim = ref_px + min_dist
            if adj <= lim: adj = lim
        return round(adj, digits), min_pts

    def _remove_orders(symbol, ids):
        for oid in ids:
            try:
                mt5.order_send({"action": mt5.TRADE_ACTION_REMOVE, "order": int(oid), "symbol": symbol})
            except Exception:
                pass

    def _close_positions_by_tag(symbol, tag):
        try:
            poss = list(mt5.positions_get(symbol=symbol) or [])
        except Exception:
            poss = []
        for p in poss:
            try:
                if tag not in str(getattr(p, "comment", "") or ""):
                    continue
                vol = float(getattr(p, "volume", 0) or 0)
                if vol <= 0: 
                    continue
                ptype = int(getattr(p, "type", 0))  # 0=BUY, 1=SELL
                close_type = mt5.ORDER_TYPE_SELL if ptype == 0 else mt5.ORDER_TYPE_BUY
                mt5.order_send({
                    "action": mt5.TRADE_ACTION_DEAL,
                    "symbol": symbol,
                    "position": int(getattr(p, "ticket", 0) or 0),
                    "type": close_type,
                    "volume": vol,
                    "deviation": 50,
                    "type_filling": getattr(mt5, "ORDER_FILLING_IOC", None),
                    "comment": f"{tag}_rollback"[:31],
                })
            except Exception:
                pass

    # ---------------- lecture config & décision ----------------
    symbol = str(decision.get("asset") or decision.get("symbol") or "").upper()
    action = str(decision.get("action", "")).upper()         # BUY / SELL
    style  = str(decision.get("entry_style") or decision.get("style") or "LIMIT_FOK").upper()
    if not symbol or action not in ("BUY", "SELL"):
        self.logger.error("[BURST] paramètres invalides (symbol/action).")
        return False

    burst_cfg = (((config or {}).get("entry_rules") or {}).get("scalping") or {}).get("burst_scalping", {}) or {}
    burst_size = int(_safe_f(burst_cfg.get("burst_size", 5), 5)) or 5

    # Tou-jours config → risk_per_trade_percent
    rptp = _safe_f(((config or {}).get("risk") or {}).get("risk_per_trade_percent"), 0.0)
    if rptp <= 0:
        self.logger.error("[BURST] risk_per_trade_percent (config.risk) manquant/<=0.")
        return False
    risk_frac_burst = rptp / 100.0

    validity_ms = int(_safe_f(decision.get("validity_ms"), 800))

    try: mt5.symbol_select(symbol, True)
    except Exception: pass

    si = _si(symbol)
    if not si:
        self.logger.error(f"[BURST] symbol_info indisponible pour {symbol}.")
        return False
    bid, ask = _tick(symbol)
    digits = si["digits"]

    entry_price = _safe_f(decision.get("price"), 0.0)
    if entry_price <= 0:
        entry_price = (ask if action == "BUY" else bid) or 0.0
    entry_price = round(entry_price, digits)
    if entry_price <= 0:
        self.logger.error("[BURST] prix d'entrée indisponible.")
        return False

    stop_pips = int(_safe_f(((config or {}).get("risk") or {}).get("stop_distance_pips", 60), 60))
    sl_price = decision.get("sl") or decision.get("sl_price")
    if sl_price is None:
        pip_size = si["point"] * (10.0 if digits in (3,5) else 1.0)
        if stop_pips <= 0 or pip_size <= 0:
            self.logger.error("[BURST] stop_distance_pips invalide.")
            return False
        sl_price = entry_price - stop_pips * pip_size if action == "BUY" else entry_price + stop_pips * pip_size
    sl_price = _safe_f(sl_price, 0.0)
    if sl_price <= 0:
        self.logger.error("[BURST] SL invalide.")
        return False

    ref_px = (bid if action == "BUY" else ask) or entry_price
    sl_price, _ = _enforce_sl(action, ref_px, sl_price, si, extra_pts=2)  # SL toujours dans la requête

    try:
        acc = mt5.account_info()
        equity = float(getattr(acc, "equity", getattr(acc, "balance", 0.0)) or 0.0)
    except Exception:
        equity = 0.0
    if equity <= 0:
        self.logger.error("[BURST] equity indisponible.")
        return False

    total_cash_burst = equity * risk_frac_burst
    cash_per_child   = total_cash_burst / float(burst_size)

    val1 = _value_per_1price_per_lot(si)
    if val1 <= 0:
        self.logger.error(f"[BURST] impossible d'estimer $/prix/lot pour {symbol}.")
        return False

    dist = abs(entry_price - sl_price)
    if dist <= 0:
        self.logger.error("[BURST] distance SL nulle.")
        return False

    vol_child = cash_per_child / (dist * val1)
    vol_child_q = _quant(vol_child, si["vol_step"], si["vol_min"], si["vol_max"])
    if vol_child_q < si["vol_min"] - 1e-12:
        self.logger.error(f"[BURST] cash/child trop faible → vol {vol_child_q:.6f} < min {si['vol_min']}.")
        return False

    child_vols = [vol_child_q] * burst_size

    basket_id = f"burst_{symbol}_{uuid.uuid4().hex[:8]}"
    self.logger.info(
        f"[BURST] Plan: {action} {symbol} x{burst_size} @ {entry_price:.{digits}f} | "
        f"risk%/burst={rptp:.5f}% → cash_total={total_cash_burst:.2f} → cash/child={cash_per_child:.2f} | "
        f"vol/child={vol_child_q:.4f} | SL={sl_price:.{digits}f} | style={style} | id={basket_id}"
    )

    # ---------------- envoi ----------------
    BUY, SELL = mt5.ORDER_TYPE_BUY, mt5.ORDER_TYPE_SELL
    BUY_LIM, SELL_LIM = mt5.ORDER_TYPE_BUY_LIMIT, mt5.ORDER_TYPE_SELL_LIMIT
    DEAL, PEND = mt5.TRADE_ACTION_DEAL, mt5.TRADE_ACTION_PENDING
    FOK = getattr(mt5, "ORDER_FILLING_FOK", None)
    IOC = getattr(mt5, "ORDER_FILLING_IOC", None)
    RET = getattr(mt5, "ORDER_FILLING_RETURN", None)
    RET_DONE         = getattr(mt5, "TRADE_RETCODE_DONE", None)
    RET_PLACED       = getattr(mt5, "TRADE_RETCODE_PLACED", None)
    RET_DONE_PARTIAL = getattr(mt5, "TRADE_RETCODE_DONE_PARTIAL", None)
    RET_INVALID_FILL = getattr(mt5, "TRADE_RETCODE_INVALID_FILL", 10030)
    RET_INVALID_STOPS= getattr(mt5, "TRADE_RETCODE_INVALID_STOPS", 10016)
    OK = {x for x in (RET_DONE, RET_PLACED, RET_DONE_PARTIAL) if x is not None}

    is_market = "MARKET" in style
    is_limit  = "LIMIT"  in style

    if is_market:
        otype = BUY if action == "BUY" else SELL
        act_code = DEAL
        fillings = [FOK, IOC]  # FOK → fallback IOC
    else:
        otype = BUY_LIM if action == "BUY" else SELL_LIM
        act_code = PEND
        fillings = [RET]       # RETURN pour pendings

    base_req = {
        "action": act_code,
        "symbol": symbol,
        "type": otype,
        "price": entry_price,
        "deviation": int(_safe_f(decision.get("deviation"), 20)),
        "type_time": getattr(mt5, "ORDER_TIME_GTC", 0),
        "sl": float(sl_price),                 # SL TOUJOURS PRÉSENT
        "comment": f"{basket_id}"[:31],
    }

    placed_ids, sent_ok = [], []

    def _send_one(i, vol):
        # On envoie DIRECTEMENT via mt5.order_send pour être sûrs que SL reste dans la requête
        req = dict(base_req)
        if is_market:
            b, a = _tick(symbol)
            req["price"] = round(a if action == "BUY" else b, digits)
        req["volume"] = float(vol)

        for fill in fillings:
            req["type_filling"] = fill
            # 2 tentatives d'élargissement SL si INVALID_STOPS (toujours avec SL)
            extra = 2
            for attempt in range(0, 3):
                if attempt > 0:
                    # élargir SL et mettre à jour la requête, sans jamais le retirer
                    ref_px = (_tick(symbol)[0] if action == "BUY" else _tick(symbol)[1]) or req["price"]
                    new_sl, used_min = _enforce_sl(action, ref_px, req["sl"], si, extra_pts=extra)
                    req["sl"] = new_sl
                    self.logger.warning(f"[BURST] élargissement SL (try={attempt}) → SL={new_sl} (min_pts={used_min})")
                    extra += 5  # élargit davantage si encore refus

                try:
                    res = mt5.order_send(req)
                except Exception as ex:
                    self.logger.exception(f"[BURST] order_send exception (child {i}, fill={fill}, try={attempt}): {ex}")
                    res = None

                ret = getattr(res, "retcode", None) if res else None
                self.logger.info(f"[BURST] enfant {i}/{burst_size} fill={fill} ret={ret} vol={vol} sl_in_req={req.get('sl', None) is not None}")
                if ret in OK:
                    oid = int(getattr(res, "order", 0) or 0)
                    if oid: placed_ids.append(oid)
                    return res
                if ret == RET_INVALID_STOPS:
                    # on boucle pour élargir encore (toujours avec SL)
                    continue
                if is_market and fill == FOK and ret == RET_INVALID_FILL:
                    self.logger.warning("[BURST] FOK non supporté → fallback IOC.")
                    break  # sort de la boucle attempts, passera au IOC
                # autre échec non retryable
                return res
        return None

    for idx, v in enumerate(child_vols, 1):
        res = _send_one(idx, v)
        if not (res and getattr(res, "retcode", None) in OK):
            self.logger.warning(f"[BURST] enfant {idx} KO → rollback.")
            _remove_orders(symbol, placed_ids)
            _close_positions_by_tag(symbol, basket_id)
            return False
        sent_ok.append(res)

    # LIMIT_FOK “soft”: purge des pendings restants après validity_ms
    if is_limit and validity_ms > 0:
        time.sleep(validity_ms / 1000.0)
        try:
            opens = list(mt5.orders_get(symbol=symbol) or [])
        except Exception:
            opens = []
        kill = []
        for o in opens:
            try:
                if basket_id in str(getattr(o, "comment", "") or ""):
                    kill.append(int(getattr(o, "ticket", 0) or 0))
            except Exception:
                pass
        if kill:
            self.logger.info(f"[BURST] LIMIT_FOK: suppression {len(kill)} ordres non exécutés.")
            _remove_orders(symbol, kill)

    # housekeeping
    now = time.time()
    self._last_burst_time = now
    self._last_any_trade_ts = now
    if not hasattr(self, "_last_trade_ts_by_asset") or not isinstance(self._last_trade_ts_by_asset, dict):
        self._last_trade_ts_by_asset = {}
    self._last_trade_ts_by_asset[symbol] = now
    self._cycle_new_trades = getattr(self, "_cycle_new_trades", 0) + 1

    self.logger.info(f"[BURST] ✅ Panier OK ({len(sent_ok)}/{burst_size}) | vol/child={vol_child_q:.4f}")
    return True


def monitor_burst_baskets(
    self,
    config: dict,
    max_loss_pips: float = 15.0,
    trail_trigger: float = 10.0,
    trail_step: float = 5.0,
) -> None:
    """
    Watchdog burst en temps réel :
    - FAST loop:
        • close immédiat si panier PLEIN & TOUT VERT (every ticket >= min_green_pnl_pips)
        • trailing de panier: armement à trail_trigger_pips, close si retracement >= trail_distance_pips
    - Filet de perte : close si pnl panier <= -max_loss_pips
    - Phase B (une passe) : même logique en secours

    Lit les clés depuis config.entry_rules.scalping.burst_scalping.closure_rules :
    close_on_full_profit (bool)
    require_full_count_for_profit_close (bool)
    min_green_pnl_pips (float)
    rt_fast_window_ms (int)
    rt_poll_interval_ms (int)
    max_loss_pips (float)
    trail_trigger_pips (float)
    trail_distance_pips (float)
    trail_require_full_count (bool)
    trail_update_min_interval_ms (int)   # <- NOUVEAU (rate limit trailing), défaut 120
    """
    import re, time

    # ---- Conf ----
    burst_cfg = (
        config.get("entry_rules", {}).get("scalping", {}).get("burst_scalping", {})
    ) or {}
    closure = burst_cfg.get("closure_rules", {}) or {}
    require_all_seen_once = bool(closure.get("require_all_seen_green_once", False))
    all_seen_green_pips = float(closure.get("all_seen_green_pips", 3.0))
    loss_guard_arming_ms = int(closure.get("loss_guard_arming_ms", 3000))

    close_on_full_profit = bool(closure.get("close_on_full_profit", True))
    require_full_count = bool(closure.get("require_full_count_for_profit_close", True))
    min_green_pnl_pips = float(closure.get("min_green_pnl_pips", 0.0))
    rt_fast_window_ms = int(closure.get("rt_fast_window_ms", 2500))
    rt_poll_interval_ms = int(closure.get("rt_poll_interval_ms", 100))
    max_loss_pips = float(closure.get("max_loss_pips", float(max_loss_pips)))
    trail_trigger_pips = float(closure.get("trail_trigger_pips", float(trail_trigger)))
    trail_distance_pips = float(
        closure.get(
            "trail_distance_pips", (trail_step if float(trail_step) > 0 else 5.0)
        )
    )
    trail_require_full = bool(closure.get("trail_require_full_count", False))
    # NOUVEAU: intervalle mini entre deux updates de trailing broker (anti-spam)
    trail_update_min_interval_ms = int(closure.get("trail_update_min_interval_ms", 120))

    # Garde-fous
    trail_trigger_pips = max(0.0, trail_trigger_pips)
    trail_distance_pips = max(0.0, trail_distance_pips)

    # ---- Connexion / états ----
    mt5c = getattr(self, "mt5_connector", None)
    if not mt5c:
        return
    mt5 = getattr(mt5c, "mt5", None)

    if not hasattr(self, "_basket_peak_pips"):
        self._basket_peak_pips = {}
    if not hasattr(self, "_basket_trail_armed"):
        self._basket_trail_armed = {}
    if not hasattr(self, "_burst_trailing_state"):
        self._burst_trailing_state = {}
    if not hasattr(self, "_last_trail_update_ms"):
        self._last_trail_update_ms = {}
    if not hasattr(self, "_basket_seen_green"):
        self._basket_seen_green = {}  # basket_id -> set(ticket)
    if not hasattr(self, "_basket_all_seen"):
        self._basket_all_seen = {}  # basket_id -> bool
    if not hasattr(self, "_basket_first_seen_ts"):
        self._basket_first_seen_ts = {}  # basket_id -> float(ts)

    # ---------- Helpers ----------
    def _v(pos, key, default=None):
        if isinstance(pos, dict):
            return pos.get(key, default)
        return getattr(pos, key, default)

    def _safe_float(x, d=None):
        try:
            return float(x)
        except Exception:
            return d

    def _symbol_info(sym: str):
        try:
            return mt5c.get_symbol_info(sym) or {}
        except Exception:
            return {}

    def _gv(si, key, default=None):
        """Tolérant: dict ou objet."""
        if si is None:
            return default
        if isinstance(si, dict):
            return si.get(key, default)
        return getattr(si, key, default)

    def _pip_size_for_symbol(sym: str) -> float:
        """
        EURUSD/GBPUSD (digits=5) -> 1 pip = 10 points
        XAUUSD (digits=2)       -> 1 pip = 1 point
        """
        si = _symbol_info(sym)
        point = _safe_float(_gv(si, "point", 0.0001), 0.0001) or 0.0001
        digits = int(_gv(si, "digits", 5) or 5)
        points_per_pip = 10.0 if digits in (3, 5) else 1.0
        return point * points_per_pip

    def _digits_for_symbol(sym: str) -> int:
        si = _symbol_info(sym)
        return int(_gv(si, "digits", 5) or 5)  # FIX anti 'SymbolInfoFallback.get'

    def _current_price(pos):
        cp = _safe_float(_v(pos, "current_price"))
        if cp is not None:
            return cp
        cp = _safe_float(_v(pos, "price_current"))
        if cp is not None:
            return cp
        bid = _safe_float(_v(pos, "bid"))
        ask = _safe_float(_v(pos, "ask"))
        t = _v(pos, "type")  # 0=BUY 1=SELL
        if str(_v(pos, "action", "")).upper() == "BUY" or t == 0:
            return ask if ask is not None else bid
        return bid if bid is not None else ask

    def _entry_price(pos):
        ep = _safe_float(_v(pos, "entry_price"))
        if ep is not None:
            return ep
        return _safe_float(_v(pos, "price_open"))

    def _direction(pos):
        d = str(_v(pos, "action", "") or "").upper()
        if d in ("BUY", "SELL"):
            return d
        return "BUY" if _v(pos, "type") == 0 else "SELL"

    def _extract_basket_id(pos):
        bid = _v(pos, "basket_id") or _v(pos, "burst_id")
        if bid:
            return str(bid)
        c = str(_v(pos, "comment", "") or "")
        m = re.search(r"burst_scalping\|(?:[^|]*\|){0,3}?basket=([A-Za-z0-9_]+)", c)
        if m:
            return m.group(1)
        m = re.search(r"(burst_[A-Z]{3,6}_[a-f0-9]{6,})", c, re.IGNORECASE)
        if m:
            return m.group(1)
        sym = str(_v(pos, "symbol", "") or "").upper()
        magic = _v(pos, "magic") or ""
        ep = _safe_float(_entry_price(pos), 0.0)
        ep_key = f"{ep:.2f}" if ep is not None else "na"
        return f"synthetic|{sym}|{magic}|{ep_key}"

    def _snapshot_positions():
        try:
            return mt5c.get_positions() or []
        except Exception:
            return []

    def _update_seen_green(basket_id: str, positions, min_seen_pips: float):
        """
        Marque un ticket comme 'déjà vert' dès qu'il a atteint min_seen_pips au moins une fois.
        Met self._basket_all_seen[basket_id] = True si tous les tickets ont été verts au moins une fois.
        """
        seen = self._basket_seen_green.setdefault(basket_id, set())
        expected = _expected_count_from(positions)
        if expected is None:
            expected = len(positions)

        for p in positions:
            tk = _v(p, "ticket")
            ep = _safe_float(_entry_price(p))
            cp = _safe_float(_current_price(p))
            if tk is None or ep is None or cp is None:
                continue
            sym = str(_v(p, "symbol", "") or "").upper()
            d = _direction(p)
            pip = _pip_size_for_symbol(sym)
            pp = ((cp - ep) / pip) if d == "BUY" else ((ep - cp) / pip)
            if pp >= float(min_seen_pips):
                seen.add(int(tk))

        # Tous déjà vus 'verts' ?
        self._basket_all_seen[basket_id] = len(seen) >= expected

    def _group_baskets(positions):
        buckets = {}
        for p in positions:
            bid = _extract_basket_id(p)
            if not bid:
                continue
            buckets.setdefault(bid, []).append(p)
        return buckets

    def _basket_stats(positions):
        """Retourne (symbol, direction, pip_size, avg_entry, avg_price, pnl_pips)."""
        if not positions:
            return None
        sym = str(_v(positions[0], "symbol", "") or "").upper()
        direction = _direction(positions[0])
        pip_size = _pip_size_for_symbol(sym) or 1e-6
        entries = [_safe_float(_entry_price(p)) for p in positions]
        currents = [_safe_float(_current_price(p)) for p in positions]
        entries = [x for x in entries if x is not None]
        currents = [x for x in currents if x is not None]
        if not entries or not currents:
            return None
        avg_entry = sum(entries) / max(1, len(entries))
        avg_price = sum(currents) / max(1, len(currents))
        pnl_pips = (
            ((avg_price - avg_entry) / pip_size)
            if direction == "BUY"
            else ((avg_entry - avg_price) / pip_size)
        )
        return sym, direction, pip_size, avg_entry, avg_price, pnl_pips

    def _expected_count_from(positions):
        # 1) chercher sur les positions
        exp = 0
        for p in positions:
            bs = _safe_float(_v(p, "burst_size"))
            if bs and int(bs) > 0:
                exp = max(exp, int(bs))
        # 2) sinon, fallback conf globale (burst_size)
        if exp == 0:
            try:
                cfg_bs = int(burst_cfg.get("burst_size", 0) or 0)
                if cfg_bs > 0:
                    exp = cfg_bs
            except Exception:
                pass
        # 3) sinon, motif "x/y" éventuel dans le commentaire
        if exp == 0 and positions:
            c0 = str(_v(positions[0], "comment", "") or "")
            m = re.search(r"\|(\d+)/(\d+)", c0)
            if m:
                try:
                    exp = int(m.group(2))
                except:
                    exp = 0
        return exp if exp > 0 else None

    def _all_green_and_full(positions) -> bool:
        """Vrai si (optionnellement) panier plein ET chaque ticket >= min_green_pnl_pips."""
        expected = _expected_count_from(positions) if require_full_count else None
        if expected is not None and len(positions) < expected:
            return False
        for p in positions:
            ep = _safe_float(_entry_price(p))
            cp = _safe_float(_current_price(p))
            if ep is None or cp is None:
                return False
            sym = str(_v(p, "symbol", "") or "").upper()
            d = _direction(p)
            pip = _pip_size_for_symbol(sym)
            pp = ((cp - ep) / pip) if d == "BUY" else ((ep - cp) / pip)
            if pp < min_green_pnl_pips:
                return False
        return True

    def _close_basket(basket_id, positions):
        """Ferme et vérifie vraiment que tout est fermé; sinon fallback par ticket."""
        # 1) bulk
        try:
            if hasattr(mt5c, "close_positions"):
                tickets = []
                for p in positions:
                    tk = _v(p, "ticket")
                    if tk is not None:
                        tickets.append(int(tk))
                if tickets:
                    mt5c.close_positions(tickets=tickets)
                    time.sleep(0.05)
                    left = [
                        p
                        for p in _snapshot_positions()
                        if _extract_basket_id(p) == basket_id
                    ]
                    if not left:
                        # purges d'état
                        self._basket_peak_pips.pop(basket_id, None)
                        self._basket_trail_armed.pop(basket_id, None)
                        self._burst_trailing_state.pop(basket_id, None)
                        self._last_trail_update_ms.pop(basket_id, None)  # NOUVEAU
                        self.logger.info(f"[CLOSE] Panier '{basket_id}' fermé (bulk).")
                        # === COOLDOWN POST-EXIT (par symbole) ===
                        try:
                            if not hasattr(self, "_cooldown_until"):
                                self._cooldown_until = {}
                            cd = float(
                                self.config_manager.get("cooldown_after_exit_s", 0)
                                or 0.0
                            )
                            if cd <= 0:
                                cd = float(
                                    self.config_manager.get(
                                        "entry_rules.scalping.burst_scalping.cooldown_after_exit_s",
                                        0,
                                    )
                                    or 0.0
                                )
                            if cd > 0:
                                import time as _t

                                sym_from_positions = (
                                    str(_v(positions[0], "symbol", "") or "").upper()
                                    if positions
                                    else ""
                                )
                                if sym_from_positions:
                                    self._cooldown_until[sym_from_positions] = (
                                        _t.time() + cd
                                    )
                                    self.logger.info(
                                        f"[COOLDOWN] {sym_from_positions} bloqué {int(cd)}s après fermeture panier '{basket_id}'."
                                    )
                        except Exception as _e:
                            self.logger.warning(f"[COOLDOWN] set KO: {_e}")

                        return True
        except Exception as e:
            self.logger.error(f"[CLOSE] close_positions bulk KO: {e}")

        # 2) fallback ticket par ticket
        ok, ko = 0, 0
        for p in positions:
            try:
                tk = _v(p, "ticket")
                if tk is None:
                    continue
                mt5c.close_position(int(tk))
                ok += 1
            except Exception as e:
                ko += 1
                self.logger.error(f"[CLOSE] ticket #{_v(p,'ticket')} KO: {e}")

        time.sleep(0.05)
        left = [p for p in _snapshot_positions() if _extract_basket_id(p) == basket_id]
        if not left:
            self._basket_peak_pips.pop(basket_id, None)
            self._basket_trail_armed.pop(basket_id, None)
            self._burst_trailing_state.pop(basket_id, None)
            self._last_trail_update_ms.pop(basket_id, None)  # NOUVEAU
            self.logger.info(f"[CLOSE] Panier '{basket_id}' fermé (fallback tickets).")
            # === COOLDOWN POST-EXIT (par symbole) ===
            try:
                if not hasattr(self, "_cooldown_until"):
                    self._cooldown_until = {}
                cd = float(self.config_manager.get("cooldown_after_exit_s", 0) or 0.0)
                if cd <= 0:
                    cd = float(
                        self.config_manager.get(
                            "entry_rules.scalping.burst_scalping.cooldown_after_exit_s",
                            0,
                        )
                        or 0.0
                    )
                if cd > 0:
                    import time as _t

                    sym_from_positions = (
                        str(_v(positions[0], "symbol", "") or "").upper()
                        if positions
                        else ""
                    )
                    if sym_from_positions:
                        self._cooldown_until[sym_from_positions] = _t.time() + cd
                        self.logger.info(
                            f"[COOLDOWN] {sym_from_positions} bloqué {int(cd)}s après fermeture panier '{basket_id}'."
                        )
            except Exception as _e:
                self.logger.warning(f"[COOLDOWN] set KO: {_e}")

            return True

        # 3) dernier recours : on laisse la phase B/emergency gérer
        self.logger.warning(
            f"[CLOSE] Fermeture partielle '{basket_id}' ({ok}/{ok+ko})."
        )
        return False

    def _push_broker_trailing_sl(
        basket_id,
        positions,
        direction,
        sym,
        avg_entry,
        pip_size,
        peak_pips,
        distance_pips,
    ):
        """Monte (BUY) / descend (SELL) les SL individuels au niveau peak±distance (sans jamais détendre)."""
        if not mt5:
            return
        digits = _digits_for_symbol(sym)
        peak_price = (
            (avg_entry + (peak_pips * pip_size))
            if direction == "BUY"
            else (avg_entry - (peak_pips * pip_size))
        )
        target_sl = (
            (peak_price - distance_pips * pip_size)
            if direction == "BUY"
            else (peak_price + distance_pips * pip_size)
        )

        for p in positions:
            try:
                tk = _v(p, "ticket")
                cur_sl = _safe_float(_v(p, "sl"))
                if tk is None:
                    continue
                # ne jamais détendre
                if direction == "BUY" and cur_sl is not None and target_sl <= cur_sl:
                    continue
                if direction == "SELL" and cur_sl is not None and target_sl >= cur_sl:
                    continue

                req = {
                    "action": mt5.TRADE_ACTION_SLTP,
                    "symbol": sym,
                    "position": int(tk),
                    "sl": round(float(target_sl), digits),
                    "tp": _safe_float(_v(p, "tp"), 0.0) or 0.0,
                }
                res = mt5c.order_send(req)
                if res and getattr(res, "retcode", None) == mt5.TRADE_RETCODE_DONE:
                    self.logger.info(f"[TRAIL→SL] {sym} pos#{tk} SL => {req['sl']}")
                else:
                    self.logger.warning(
                        f"[TRAIL→SL] ❌ pos#{tk} retcode={getattr(res,'retcode',None)}"
                    )
            except Exception as e:
                self.logger.error(f"[TRAIL→SL] err pos SL update: {e}")

    # ==============
    # Phase A — FAST (boucle courte, décision immédiate)
    # ==============
    if rt_fast_window_ms > 0 and rt_poll_interval_ms > 0:
        deadline = time.monotonic() + (rt_fast_window_ms / 1000.0)  # M1 monotonic
        while True:
            open_positions = _snapshot_positions()
            if not open_positions:
                break
            baskets = _group_baskets(open_positions)
            if not baskets:
                break

            any_action = False
            for basket_id, pos in baskets.items():

                # --- 3b: age panier + mémoire 'ever green' ---
                if basket_id not in self._basket_first_seen_ts:
                    self._basket_first_seen_ts[basket_id] = time.time()
                age_ms = int(
                    (time.time() - self._basket_first_seen_ts[basket_id]) * 1000
                )

                # met à jour la mémoire: quels tickets ont déjà été verts au moins une fois
                _update_seen_green(basket_id, pos, all_seen_green_pips)
                all_seen_ok = bool(self._basket_all_seen.get(basket_id, False))

                # 1) CLOSE INSTANTANÉ : Panier plein & TOUT VERT
                if close_on_full_profit and _all_green_and_full(pos):
                    if (
                        require_all_seen_once
                        and not all_seen_ok
                        and age_ms < loss_guard_arming_ms
                    ):
                        # On attend que chaque ticket ait été vert au moins une fois
                        pass
                    else:
                        self.logger.info(
                            f"🎯 [FAST] {basket_id} PLEIN & TOUT VERT → CLOSE"
                        )
                        if _close_basket(basket_id, pos):
                            any_action = True
                            continue

                # 2) Trailing de panier (armement & retracement)
                stats = _basket_stats(pos)
                if not stats:
                    continue
                sym, direction, pip_size, avg_entry, avg_price, pnl_pips = stats

                expected = _expected_count_from(pos)
                is_full = expected is not None and len(pos) >= expected
                if pnl_pips >= trail_trigger_pips and (
                    (not trail_require_full) or is_full
                ):
                    if not self._basket_trail_armed.get(basket_id, False):
                        self._basket_trail_armed[basket_id] = True
                        self._basket_peak_pips[basket_id] = pnl_pips
                        self.logger.info(
                            f"🛡️ [FAST] {basket_id} ARMÉ à {pnl_pips:.1f}p (trigger={trail_trigger_pips:.1f})"
                        )

                if self._basket_trail_armed.get(basket_id, False):
                    # peak
                    if pnl_pips > float(
                        self._basket_peak_pips.get(basket_id, pnl_pips)
                    ):
                        self._basket_peak_pips[basket_id] = pnl_pips
                    # --- M2: RATE LIMIT TRAILING (Phase A) ---
                    _now_ms = int(time.monotonic() * 1000)
                    _last_ms = int(self._last_trail_update_ms.get(basket_id, 0) or 0)
                    if _now_ms - _last_ms >= trail_update_min_interval_ms:
                        _push_broker_trailing_sl(
                            basket_id,
                            pos,
                            direction,
                            sym,
                            avg_entry,
                            pip_size,
                            peak_pips=float(
                                self._basket_peak_pips.get(basket_id, pnl_pips)
                            ),
                            distance_pips=trail_distance_pips,
                        )
                        self._last_trail_update_ms[basket_id] = _now_ms
                    # retracement ⇒ close
                    dd = (
                        float(self._basket_peak_pips.get(basket_id, pnl_pips))
                        - pnl_pips
                    )
                    if dd >= trail_distance_pips and pnl_pips > 0.0:
                        if (
                            require_all_seen_once
                            and not all_seen_ok
                            and age_ms < loss_guard_arming_ms
                        ):
                            self.logger.info(
                                f"⏸️ [FAST] {basket_id} retrace {dd:.1f}p mais pas 'all_seen' (age={age_ms}ms)"
                            )
                        else:
                            self.logger.warning(
                                f"🔒 [FAST] {basket_id} retrace {dd:.1f}p ≥ {trail_distance_pips:.1f}p → CLOSE"
                            )
                            if _close_basket(basket_id, pos):
                                any_action = True
                                continue

                # état debug
                self._burst_trailing_state[basket_id] = {
                    "armed": self._basket_trail_armed.get(basket_id, False),
                    "peak_pips": self._basket_peak_pips.get(basket_id, 0.0),
                    "trigger": trail_trigger_pips,
                    "distance": trail_distance_pips,
                }

            if time.monotonic() >= deadline:
                break
            if not any_action:
                time.sleep(rt_poll_interval_ms / 1000.0)
            else:
                continue

    # ==============
    # Phase B — passe de secours + filet de perte
    # ==============
    open_positions = _snapshot_positions()
    if not open_positions:
        return
    baskets = _group_baskets(open_positions)
    if not baskets:
        return

    for basket_id, pos in baskets.items():
        # --- 3c: age panier + mémoire 'ever green' (Phase B) ---
        if basket_id not in self._basket_first_seen_ts:
            self._basket_first_seen_ts[basket_id] = time.time()
        age_ms = int((time.time() - self._basket_first_seen_ts[basket_id]) * 1000)
        _update_seen_green(basket_id, pos, all_seen_green_pips)
        all_seen_ok = bool(self._basket_all_seen.get(basket_id, False))

        try:
            # 1) all-green encore (au cas où)
            if close_on_full_profit and _all_green_and_full(pos):
                if (
                    require_all_seen_once
                    and not all_seen_ok
                    and age_ms < loss_guard_arming_ms
                ):
                    pass
                else:
                    self.logger.info(
                        f"🎯 {basket_id} PLEIN & TOUT VERT (Phase B) → CLOSE"
                    )
                    _close_basket(basket_id, pos)
                    continue

            stats = _basket_stats(pos)
            if not stats:
                continue
            sym, direction, pip_size, avg_entry, avg_price, pnl_pips = stats

            # 2) filet de perte
            if pnl_pips <= -abs(max_loss_pips):
                if age_ms < loss_guard_arming_ms and (
                    require_all_seen_once and not all_seen_ok
                ):
                    self.logger.warning(
                        f"⏸️ {basket_id} perte {pnl_pips:.1f}p mais guard non armé (age={age_ms}ms<{loss_guard_arming_ms}ms)"
                    )
                else:
                    self.logger.warning(
                        f"❌ {basket_id} perte {pnl_pips:.1f}p ≤ -{abs(max_loss_pips):.1f}p → CLOSE"
                    )
                    _close_basket(basket_id, pos)
                    continue

            # 3) trailing (armement / peak / retrace)
            expected = _expected_count_from(pos)
            is_full = expected is not None and len(pos) >= expected
            if pnl_pips >= trail_trigger_pips and ((not trail_require_full) or is_full):
                if not self._basket_trail_armed.get(basket_id, False):
                    self._basket_trail_armed[basket_id] = True
                    self._basket_peak_pips[basket_id] = pnl_pips
                    self.logger.info(f"🛡️ {basket_id} ARMÉ (Phase B) à {pnl_pips:.1f}p")

            if self._basket_trail_armed.get(basket_id, False):
                if pnl_pips > float(self._basket_peak_pips.get(basket_id, pnl_pips)):
                    self._basket_peak_pips[basket_id] = pnl_pips

                # --- M2: RATE LIMIT TRAILING (Phase B) ---
                _now_ms = int(time.monotonic() * 1000)
                _last_ms = int(self._last_trail_update_ms.get(basket_id, 0) or 0)
                if _now_ms - _last_ms >= trail_update_min_interval_ms:
                    _push_broker_trailing_sl(
                        basket_id,
                        pos,
                        direction,
                        sym,
                        avg_entry,
                        pip_size,
                        peak_pips=float(
                            self._basket_peak_pips.get(basket_id, pnl_pips)
                        ),
                        distance_pips=trail_distance_pips,
                    )
                    self._last_trail_update_ms[basket_id] = _now_ms

                dd = float(self._basket_peak_pips.get(basket_id, pnl_pips)) - pnl_pips
                if dd >= trail_distance_pips and pnl_pips > 0.0:
                    if (
                        require_all_seen_once
                        and not all_seen_ok
                        and age_ms < loss_guard_arming_ms
                    ):
                        self.logger.info(
                            f"⏸️ {basket_id} retrace {dd:.1f}p mais pas 'all_seen' (age={age_ms}ms)"
                        )
                    else:
                        self.logger.warning(
                            f"🔒 {basket_id} retrace {dd:.1f}p ≥ {trail_distance_pips:.1f}p → CLOSE"
                        )
                        _close_basket(basket_id, pos)
                        continue

            # état debug
            self._burst_trailing_state[basket_id] = {
                "armed": self._basket_trail_armed.get(basket_id, False),
                "peak_pips": self._basket_peak_pips.get(basket_id, 0.0),
                "trigger": trail_trigger_pips,
                "distance": trail_distance_pips,
            }

        except Exception as e:
            self.logger.error(
                f"[MONITOR] Erreur basket {basket_id}: {e}", exc_info=True
            )


def build_burst_trailing_request(
    self,
    trade_decision: dict,
    config: dict,
    volume: float,
    entry_price: float,
    sl_price: float,
    symbol_info: Any,
) -> dict:
    """
    Construction robuste d'une requête MT5 spécifique à la stratégie "burst_scalping".
    - ❌ Aucun TP logique (TP=0.0 côté MT5, trailing géré par le moteur)
    - ✅ SL obligatoire, arrondi aux digits et respect des distances stops_level
    - Volume normalisé (FLOOR sur volume_step) et clampé [min, max]
    - Commentaire court & canonique (<=31, ASCII), compatible extracteur de panier
    - Trailing config stockée en meta (_meta_trailing)
    - Remontées d’erreurs via TradeExecutionError
    """
    import math, re, secrets
    from trader.errors import TradeExecutionError  # <- plus de fallback local

    # --------- Validations d'entrée ---------
    if not isinstance(trade_decision, dict):
        raise TradeExecutionError("trade_decision invalide (attendu dict).")

    action = str((trade_decision.get("action") or "").upper()).strip()
    if action not in {"BUY", "SELL"}:
        raise TradeExecutionError(f"[BURST] Action invalide: '{action}'.")

    if not symbol_info:
        raise TradeExecutionError("[BURST] symbol_info manquant.")

    try:
        digits = int(getattr(symbol_info, "digits", 0) or 0)
        point = float(getattr(symbol_info, "point", 0.0) or 0.0)
    except Exception:
        raise TradeExecutionError("[BURST] symbol_info illisible (digits/point).")
    if digits <= 0 or point <= 0.0:
        raise TradeExecutionError("[BURST] symbol_info invalide: digits/point <= 0.")

    symbol_name = str(
        getattr(symbol_info, "name", "") or getattr(symbol_info, "symbol", "")
    ).strip()
    if not symbol_name:
        symbol_name = str(
            trade_decision.get("asset") or trade_decision.get("symbol") or ""
        ).strip()
    if not symbol_name:
        raise TradeExecutionError(
            "[BURST] Impossible de déterminer le symbole (name/symbol/asset)."
        )
    symbol_name = symbol_name.upper()

    # --------- Volume (VALIDATION ONLY — aucun sizing ici) ---------
    if not isinstance(volume, (int, float)) or not math.isfinite(volume) or volume <= 0:
        raise TradeExecutionError(f"[BURST] Volume invalide ({volume}).")
    vol = float(volume)  # le volume est déjà pré-quantifié par le sizing amont

    # --------- Entry & SL (arrondis + stops_level) ---------
    if not isinstance(entry_price, (int, float)) or entry_price <= 0:
        raise TradeExecutionError("[BURST] entry_price invalide.")
    if not isinstance(sl_price, (int, float)) or sl_price <= 0:
        raise TradeExecutionError("[BURST] sl_price invalide.")
    entry_price = round(float(entry_price), digits)
    sl_price = round(float(sl_price), digits)

    stops_lvl_points = float(
        getattr(symbol_info, "trade_stops_level", 0)
        or getattr(symbol_info, "stops_level", 0)
        or 0
    )
    min_stop_distance_price = stops_lvl_points * point
    if min_stop_distance_price > 0:
        if action == "BUY" and (entry_price - sl_price) < min_stop_distance_price:
            sl_price = round(entry_price - min_stop_distance_price, digits)
        elif action == "SELL" and (sl_price - entry_price) < min_stop_distance_price:
            sl_price = round(entry_price + min_stop_distance_price, digits)

    if action == "BUY":
        if not (sl_price < entry_price):
            raise TradeExecutionError(
                f"[BURST] Cohérence BUY : SL({sl_price}) doit être < entry({entry_price})."
            )
    else:
        if not (sl_price > entry_price):
            raise TradeExecutionError(
                f"[BURST] Cohérence SELL : SL({sl_price}) doit être > entry({entry_price})."
            )

    # --------- MT5: constantes & mapping ---------
    mt5 = getattr(getattr(self, "mt5_connector", None), "mt5", None)
    if mt5 is None:
        try:
            import MetaTrader5 as _mt5  # type: ignore

            mt5 = _mt5
        except Exception:
            mt5 = None
    if mt5 is None:
        raise TradeExecutionError("[BURST] Module/constantes MT5 indisponibles.")

    action_const = getattr(mt5, "TRADE_ACTION_DEAL", None)
    order_type_const = getattr(mt5, f"ORDER_TYPE_{action}", None)
    if action_const is None or order_type_const is None:
        raise TradeExecutionError("[BURST] Constantes MT5 (action/type) introuvables.")

    # --------- Deviation & magic ---------
    deviation_points = int(
        (config.get("execution_policy", {}) or {}).get(
            "max_deviation_points", config.get("max_slippage_points", 20)
        )
        or 20
    )
    if deviation_points < 0:
        deviation_points = 0

    magic = int(
        config.get(
            "magic_number",
            (self.config_manager.get("trade_executor_settings", {}) or {}).get(
                "magic", 0
            ),
        )
        or 0
    )

    # --------- Basket/Comment ---------
    basket_id = str(trade_decision.get("basket_id") or "").strip()
    if not basket_id:
        hex6 = secrets.token_hex(3)
        sym_up = re.sub(r"[^A-Z]", "", str(symbol_name).upper())[:6] or "ASSET"
        basket_id = f"burst_{sym_up}_{hex6}"

    comment = basket_id.replace(" ", "").replace("|", "")
    comment = re.sub(r"[^A-Za-z0-9._-]", "", comment)[:31]

    # --------- Trailing & Closure ---------
    bs_cfg = ((config.get("entry_rules", {}) or {}).get("scalping", {}) or {}).get(
        "burst_scalping", {}
    ) or {}
    tp_sl_cfg = bs_cfg.get("tp_sl", {}) or {}

    trailing_cfg_raw = (
        trade_decision.get("trailing") or tp_sl_cfg.get("trailing") or {"enabled": True}
    )
    trailing_defaults = {
        "enabled": True,
        "mode": "ATR",
        "atr_period": 14,
        "atr_mult": 0.6,
        "min_trail_points": 30,
        "arm_profit_points": 60,
        "arm_atr_multiple": 0.8,
        "max_pullback_points": 80,
        "trail_update_throttle_ms": 250,
        "backoff_step_points": 10,
        "breakeven_lock_points": 10,
        "max_spread_points_when_armed": 40,
        "max_latency_ms_on_close": 500,
    }
    trailing_cfg = {**trailing_defaults, **(trailing_cfg_raw or {})}

    closure_defaults = {
        "close_on_full_profit": True,
        "require_full_count_for_profit_close": True,
        "min_green_pnl_pips": 0.0,
        "rt_fast_window_ms": 2500,
        "rt_poll_interval_ms": 100,
        "max_loss_pips": 15.0,
        "trail_trigger_pips": 10.0,
        "trail_distance_pips": 5.0,
        "trail_require_full_count": False,
        "trail_update_min_interval_ms": 120,
        "require_all_seen_green_once": True,
        "all_seen_green_pips": 5.0,
        "loss_guard_arming_ms": 5000,
        "cooldown_after_exit_s": float(
            config.get("cooldown_after_exit_s", 0.0)
            or bs_cfg.get("cooldown_after_exit_s", 0.0)
            or 0.0
        ),
    }
    closure_cfg = {**closure_defaults, **(bs_cfg.get("closure_rules", {}) or {})}

    # --------- Requête finale ---------
    request = {
        "symbol": symbol_name,
        "volume": float(vol),
        "price": float(entry_price),
        "sl": float(sl_price),
        "tp": 0.0,  # 🔒 pas de TP pour BURST (trailing only)
        "type": order_type_const,
        "action": action_const,
        "deviation": int(deviation_points),
        "magic": magic,
        "comment": comment,
        "strategy_type": (str(config.get("strategy_name", "burst_scalping")).lower()),
        "rule_name": str(trade_decision.get("rule_name", "burst_scalping")),
        # --- meta (ignorés par MT5) ---
        "_meta_no_tp": True,
        "_meta_trailing": trailing_cfg,  # (fix: plus de doublon)
        "_meta_closure_rules": closure_cfg,
        "_meta_entry_source": trade_decision.get("source", "core_decision"),
        "_meta_stops_level_points": stops_lvl_points,
        "_meta_point": point,
        "_meta_digits": digits,
        "basket_id": basket_id,
        "burst_size": trade_decision.get("burst_size"),
        "burst_index": trade_decision.get("burst_index"),
        "is_burst_trade": True,
    }

    self.logger.info(
        f"[BURST] Requête: {action} {request['symbol']} | vol={request['volume']:.4f} | "
        f"entry={entry_price} | sl={sl_price} | basket={basket_id} | trailing={trailing_cfg}"
    )
    return request


def execute_burst_single_master(self, payload: dict) -> dict:
    """
    Envoie UNE SEULE position 'master' (market deal) pour un burst scalping
    et initialise tout l'état de trailing panier côté exécuteur.
    Retourne un dict {status, basket_id, ticket, deal, request, retcode, retcode_str}.
    """
    import time, logging, hashlib, random, math, re
    from datetime import datetime, timezone

    logger = getattr(self, "logger", logging.getLogger(__name__))

    # --------- Helpers sûrs ---------
    def _is_connected() -> bool:
        try:
            attr = getattr(self.mt5_connector, "is_connected", None)
            return bool(attr()) if callable(attr) else bool(attr)
        except Exception:
            return False

    def _reconnect_if_needed():
        try:
            fn = getattr(self.mt5_connector, "reconnect_if_needed", None) or getattr(
                self.mt5_connector, "connect", None
            )
            if callable(fn):
                fn()
        except Exception:
            pass

    def _normalize_comment(text: str, fallback: str, max_len: int = 31) -> str:
        raw = (text or fallback or "").strip()
        raw = raw.replace("|", "").replace(" ", "")
        raw = re.sub(r"[^A-Za-z0-9._-]", "", raw)
        return raw[:max_len]

    def _get_cfg(path: str, default=None):
        # config_manager.get d'abord, sinon lecture dans active_config (dotted)
        try:
            if hasattr(self, "config_manager"):
                v = self.config_manager.get(path, None)
                if v is not None:
                    return v
        except Exception:
            pass
        node = active_config or {}
        try:
            for part in path.split("."):
                node = node[part]
            return node
        except Exception:
            return default

    # --------- 0) Déballage & normalisation ---------
    td = (payload or {}).get("trade_decision") or {}
    market_context = (payload or {}).get("market_context") or {}
    active_config = (payload or {}).get("active_config") or {}

    action = str(td.get("action", "")).upper()
    asset = str(td.get("asset", "")).upper()
    rule_name = td.get("rule_name") or "burst_scalping"
    strategy_type = td.get("strategy_type") or "unknown"
    order_id = td.get("order_id", "N/A")

    if action not in ("BUY", "SELL") or not asset or asset == "UNKNOWN":
        reason = f"execute_burst_single_master input invalide: action={action}, asset={asset}"
        logger.error(reason)
        return {"status": "failed", "reason": reason}

    # Connexion
    if not _is_connected():
        _reconnect_if_needed()
    if not _is_connected():
        reason = "mt5_not_connected"
        logger.error(reason)
        return {"status": "failed", "reason": reason}

    mt5 = getattr(self.mt5_connector, "mt5", None)
    if not mt5:
        return {"status": "failed", "reason": "mt5_not_available"}

    # --------- 1) Mapping & sélection symbole ---------
    try:
        broker_symbol = self._map_symbol_for_broker(asset, market_context) or asset
    except Exception:
        broker_symbol = asset
    broker_symbol = str(broker_symbol).upper()

    # resolve + infos symbole
    try:
        resolve = getattr(self.mt5_connector, "resolve_broker_symbol", None)
        if callable(resolve):
            broker_symbol = resolve(broker_symbol) or broker_symbol
        si = self.mt5_connector.get_symbol_info(broker_symbol)
        if not si or not getattr(si, "name", None):
            return {"status": "failed", "reason": f"invalid_symbol:{broker_symbol}"}
    except Exception as e:
        logger.error(f"Symbol info KO: {e}")
        return {"status": "failed", "reason": f"symbol_info_error:{e}"}

    # MarketWatch selection
    try:
        sel = getattr(self.mt5_connector, "ensure_symbol_selected", None)
        ok_sel = bool(sel(broker_symbol)) if callable(sel) else True
        if not ok_sel:
            info = self.mt5_connector.get_symbol_info(broker_symbol)
            if not info or (hasattr(info, "visible") and not info.visible):
                sub = getattr(self.mt5_connector, "symbol_select", None)
                if callable(sub) and not sub(broker_symbol, True):
                    return {
                        "status": "failed",
                        "reason": f"symbol_not_selected:{broker_symbol}",
                    }
    except Exception as e:
        logger.warning(f"ensure_symbol_selected KO: {e}")

    # --------- 2) Volume (validation simple — aucune normalisation/calcul ici) ---------
    try:
        vol = float(td["volume"])
        if not math.isfinite(vol) or vol <= 0:
            raise ValueError("invalid_volume")
    except Exception:
        reason = "missing_or_invalid_volume"
        logger.error(reason)
        return {"status": "failed", "reason": reason}

    # --------- 3) Prix, slippage & policies ---------
    # policy (FOK par défaut)
    fill_str = str(_get_cfg("execution_policy.type_filling", "FOK")).upper()
    type_filling = getattr(
        mt5, f"ORDER_FILLING_{fill_str}", getattr(mt5, "ORDER_FILLING_FOK", None)
    )
    if type_filling is None:
        type_filling = getattr(mt5, "ORDER_FILLING_FOK", 0)

    deviation = int(_get_cfg("execution_policy.max_deviation_points", 20) or 20)
    magic = int(_get_cfg("trade_executor_settings.magic", 0) or 0)

    # ordre market → BUY sur ask, SELL sur bid
    def _get_price(sym: str, side: str) -> float:
        try:
            t = mt5.symbol_info_tick(sym)
            if not t:
                return 0.0
            if side == "BUY" and hasattr(t, "ask") and t.ask:
                return float(t.ask)
            if side == "SELL" and hasattr(t, "bid") and t.bid:
                return float(t.bid)
            return float(getattr(t, "ask", 0.0) or getattr(t, "bid", 0.0) or 0.0)
        except Exception:
            return 0.0

    price = _get_price(broker_symbol, action)
    if price <= 0:
        return {"status": "failed", "reason": "price_unavailable"}

    mt5_type = (
        getattr(mt5, "ORDER_TYPE_BUY", 0)
        if action == "BUY"
        else getattr(mt5, "ORDER_TYPE_SELL", 1)
    )

    # --------- 4) Trailing params (défauts robustes) ---------
    trailing = ((td.get("_meta") or {}).get("trailing")) or {}
    trigger_pips = float(
        trailing.get(
            "trigger_pips",
            _get_cfg(
                "burst_single_master.trail.trigger_pips",
                _get_cfg("dynamic_trailing.trigger_pips", 10.0),
            ),
        )
        or 10.0
    )
    distance_pips = float(
        trailing.get(
            "distance_pips",
            _get_cfg(
                "burst_single_master.trail.distance_pips",
                _get_cfg("dynamic_trailing.distance_pips", 5.0),
            ),
        )
        or 5.0
    )
    min_hold_ms = int(
        _get_cfg(
            "burst_single_master.min_hold_ms",
            _get_cfg("dynamic_trailing.min_hold_ms", 0),
        )
        or 0
    )

    # --------- 5) Basket ID + commentaire court ---------
    basket_id = td.get("basket_id")
    if not basket_id:
        seed = f"{asset}|{int(time.time()*1000)}|{random.random()}"
        hid = hashlib.sha1(seed.encode()).hexdigest()[:8]
        basket_id = f"burst_{asset}_{hid}"

    comment = _normalize_comment(basket_id, fallback=f"burst_{asset}")

    # --------- 6) Construction requête DEAL ---------
    req = {
        "action": getattr(mt5, "TRADE_ACTION_DEAL", 1),
        "symbol": getattr(si, "name", broker_symbol),
        "type": mt5_type,
        "volume": float(vol),
        "price": float(price),
        "type_filling": type_filling,
        "type_time": getattr(mt5, "ORDER_TIME_GTC", 0),
        "deviation": int(deviation),
        "magic": int(magic),
        "comment": comment,  # court, stable, = basket_id (détection & annulation faciles)
        # pas de SL/TP ici → trailing only
        "sl": 0.0,
        "tp": 0.0,
        # meta (ignorés par MT5)
        "strategy_type": "burst_scalping",
        "rule_name": rule_name,
        "basket_id": basket_id,
        "is_burst_trade": True,
    }

    # --------- 7) Envoi avec retry slippage ---------
    # Retcodes
    RET_DONE = getattr(mt5, "TRADE_RETCODE_DONE", None)
    RET_PLACED = getattr(mt5, "TRADE_RETCODE_PLACED", None)
    RET_DONE_PARTIAL = getattr(mt5, "TRADE_RETCODE_DONE_PARTIAL", None)
    OK_CODES = {RET_DONE, RET_PLACED, RET_DONE_PARTIAL}

    RET_REQUOTE = getattr(mt5, "TRADE_RETCODE_REQUOTE", None)
    RET_PRICE_OFF = getattr(
        mt5,
        "TRADE_RETCODE_PRICE_OFF",
        getattr(mt5, "TRADE_RETCODE_INVALID_PRICE", None),
    )
    RET_TIMEOUT = getattr(mt5, "TRADE_RETCODE_TIMEOUT", None)
    RET_CONNECTION = getattr(mt5, "TRADE_RETCODE_NO_CONNECTION", None)

    max_retries = int(_get_cfg("trade_executor_settings.max_send_retries", 2) or 2)
    sleep_between = float(
        _get_cfg("trade_executor_settings.retry_sleep_seconds", 0.05) or 0.05
    )
    result = None
    retcode = None

    for attempt in range(max_retries + 1):
        try:
            result = self.mt5_connector.order_send(req)
        except Exception as e:
            logger.error(
                f"[BURST] order_send exception (try {attempt+1}/{max_retries+1}): {e}",
                exc_info=True,
            )
            result = None

        retcode = getattr(result, "retcode", None)
        if retcode in OK_CODES:
            break

        # cas retryables → rafraîchir prix et re-tenter
        if retcode in {RET_REQUOTE, RET_PRICE_OFF, RET_TIMEOUT, RET_CONNECTION}:
            time.sleep(sleep_between)
            # sur reconnexion perdue
            if retcode in {RET_TIMEOUT, RET_CONNECTION} and not _is_connected():
                _reconnect_if_needed()
            # met à jour le prix
            new_px = _get_price(broker_symbol, action)
            if new_px > 0:
                req["price"] = float(new_px)
            continue

        # non-retryable → stop
        break

    # mapping retcode str lisible
    retcode_str = str(retcode)
    try:
        for k, v in (
            (getattr(self, "mt5_mappings", {}) or {}).get("trade_retcodes", {}).items()
        ):
            if getattr(mt5, v, None) == retcode:
                retcode_str = k
                break
    except Exception:
        pass

    if not result or retcode not in OK_CODES:
        reason = f"retcode={retcode_str}"
        logger.error(f"[BURST] ❌ Order send FAILED ({broker_symbol}) {reason}")

        # feedback / audit soft
        try:
            fb = self.feedback_pipeline(
                order_id=order_id, status="failed", reason=reason
            )
            self._feedback_safe(td, fb)
        except Exception:
            pass
        try:
            if hasattr(self, "audit_logger"):
                self.audit_logger.log_trade_execution(
                    {
                        "status": "rejected",
                        "symbol": broker_symbol,
                        "action": action,
                        "order_type": "MARKET",
                        "volume": req["volume"],
                        "entry_price": req["price"],
                        "sl_price": req["sl"],
                        "tp_price": req["tp"],
                        "strategy_type": req.get("strategy_type"),
                        "rule_name": req.get("rule_name"),
                        "magic_number": req.get("magic"),
                        "request": req,
                        "response": {
                            "retcode": retcode,
                            "retcode_str": retcode_str,
                        },
                        "is_burst_trade": True,
                    },
                    getattr(self, "execution_context", {}) or {},
                )
        except Exception:
            pass

        return {
            "status": "failed",
            "reason": reason,
            "request": req,
            "retcode": retcode,
            "retcode_str": retcode_str,
        }

    # --------- 8) Post-envoi : trailing state & locks ---------
    ticket = getattr(result, "order", None)
    deal = getattr(result, "deal", None)

    # registres
    if not hasattr(self, "_open_positions"):
        self._open_positions = {}
    if not hasattr(self, "_burst_trailing_state"):
        self._burst_trailing_state = {}
    if not hasattr(self, "_basket_trail_armed"):
        self._basket_trail_armed = {}
    if not hasattr(self, "_basket_peak_pips"):
        self._basket_peak_pips = {}
    if not hasattr(self, "_active_burst_locks"):
        self._active_burst_locks = {}

    # trailing state par panier
    self._burst_trailing_state[basket_id] = {
        "trigger": float(trigger_pips),
        "distance": float(distance_pips),
        "trigger_pips": float(trigger_pips),
        "distance_pips": float(distance_pips),
        "min_hold_ms": int(min_hold_ms),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    self._basket_trail_armed[basket_id] = False
    self._basket_peak_pips[basket_id] = 0.0
    # lock d'asset pour empêcher un nouveau panier concurrent
    try:
        self._active_burst_locks[str(broker_symbol).upper()] = {
            "basket_id": basket_id,
            "since": time.time(),
        }
        # horodatage cooldown (utilisé par pre_trade_checks 5bis)
        self._last_burst_time = time.time()
    except Exception:
        pass

    # meta côté _open_positions (si ticket connu)
    try:
        if ticket is not None:
            self._open_positions[int(ticket)] = {
                "ticket": int(ticket),
                "symbol": getattr(si, "name", broker_symbol),
                "type": 0 if action == "BUY" else 1,
                "volume": float(vol),
                "entry_price": float(req["price"]),
                "sl": 0.0,
                "tp": 0.0,
                "magic": int(magic),
                "comment": comment,
                "open_time": datetime.now(timezone.utc).isoformat(),
                "profit": 0.0,
                "_meta": {
                    "basket_id": basket_id,
                    "trailing": {
                        "trigger_pips": float(trigger_pips),
                        "distance_pips": float(distance_pips),
                        "min_hold_ms": int(min_hold_ms),
                    },
                    "rule_name": rule_name,
                    "strategy_type": strategy_type,
                },
            }
    except Exception:
        pass

    logger.info(
        f"[BURST] ✅ MASTER {action} {broker_symbol} vol={vol} ticket={ticket or 'NA'} "
        f"basket={basket_id} (trg={trigger_pips}p, dist={distance_pips}p, mh={min_hold_ms}ms)"
    )

    # Feedback & audit
    try:
        fb = self.feedback_pipeline(
            order_id=order_id, status="sent", reason="burst_master_sent"
        )
        self._feedback_safe(td, fb)
    except Exception:
        pass
    try:
        if hasattr(self, "audit_logger"):
            self.audit_logger.log_trade_execution(
                {
                    "status": "placed" if retcode == RET_PLACED else "filled",
                    "symbol": broker_symbol,
                    "action": action,
                    "order_type": "MARKET",
                    "volume": req["volume"],
                    "entry_price": req["price"],
                    "sl_price": req["sl"],
                    "tp_price": req["tp"],
                    "strategy_type": req.get("strategy_type"),
                    "rule_name": req.get("rule_name"),
                    "magic_number": req.get("magic"),
                    "ticket": ticket or deal,
                    "request": req,
                    "response": {
                        "retcode": retcode,
                        "retcode_str": retcode_str,
                        "comment": getattr(result, "comment", ""),
                        "order": ticket,
                        "deal": deal,
                        "is_burst_trade": True,
                    },
                },
                getattr(self, "execution_context", {}) or {},
            )
    except Exception:
        pass

    return {
        "status": "sent",
        "mode": "burst",
        "basket_id": basket_id,
        "ticket": ticket,
        "deal": deal,
        "request": req,
        "retcode": retcode,
        "retcode_str": retcode_str,
    }
