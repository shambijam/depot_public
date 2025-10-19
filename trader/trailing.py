# trader/trailing.py - Module de Suivi des Prix pour le Bot SNIPER_X
from __future__ import annotations

import time
from typing import Any, Optional, Dict, List
from datetime import datetime


def apply_dynamic_trailing(
    self, ticket: int, sl_pips: float, atr_pips: float, symbol: Optional[str] = None
) -> None:
    """
    Applique un trailing stop dynamique (sans détendre le SL).
    - Respecte stops_level/freeze_level/tick_size (+ buffer configurable)
    - BUY: SL = Ask - (dist + buffer) ; SELL: SL = Bid + (dist + buffer)
    - Rate-limit par ticket et pas de micro-update (< ~1/2 tick)
    """
    try:
        pos = (getattr(self, "_open_positions", {}) or {}).get(ticket)
        if not pos:
            return

        # symbole
        sym = symbol or pos.get("symbol")
        if not sym:
            return

        mt5c = getattr(self, "mt5_connector", None)
        if not mt5c:
            return
        mt5 = getattr(mt5c, "mt5", None)

        # côté (0=BUY / 1=SELL) via constante interne
        action = (
            "BUY"
            if pos.get("type") == getattr(self, "POSITION_TYPE_BUY", 0)
            else "SELL"
        )

        # ----- Tick/prix courant côté broker (Ask pour BUY, Bid pour SELL) -----
        bid = ask = None
        if mt5:
            try:
                tk = mt5.symbol_info_tick(sym)
                if tk:
                    bid = float(getattr(tk, "bid", 0.0) or 0.0)
                    ask = float(getattr(tk, "ask", 0.0) or 0.0)
            except Exception:
                pass

        if action == "BUY":
            cp = ask if (ask and ask > 0) else mt5c.get_current_price(sym, "BUY")
        else:
            cp = bid if (bid and bid > 0) else mt5c.get_current_price(sym, "SELL")
        current_price = float(cp or 0.0)
        if not (current_price > 0):
            return

        # ----- Infos symbole & tailles -----
        try:
            si = mt5c.get_symbol_info(sym)
        except Exception:
            si = None

        digits = int(getattr(si, "digits", 5) or 5) if si else 5
        point = float(getattr(si, "point", 1e-5) or 1e-5) if si else 1e-5
        tick_size = (
            float(getattr(si, "trade_tick_size", point) or point) if si else point
        )
        stops_level = int(getattr(si, "trade_stops_level", 0) or 0) if si else 0
        freeze_level = int(getattr(si, "trade_freeze_level", 0) or 0) if si else 0

        # FX 3/5 digits -> 1 pip = 10 points ; métaux (2 digits) -> 1 pip = 1 point
        points_per_pip = 10.0 if digits in (3, 5) else 1.0
        pip_size = point * points_per_pip

        # ----- Buffer mini de sécurité (stops/freeze + extra configurable) -----
        try:
            extra_pts = int(
                self.config_manager.get("risk.sltp_extra_buffer_points", 2) or 2
            )
        except Exception:
            extra_pts = 2
        min_pts = max(stops_level, freeze_level) + max(extra_pts, 0)
        min_dist_price = max(tick_size, min_pts * point)

        # ----- Distance trailing -----
        trailing_dist = max(0.0, float(sl_pips) + float(atr_pips)) * pip_size

        # Candidat SL côté cours + application du buffer (Bid/Ask)
        if action == "BUY":
            new_sl = current_price - trailing_dist
            # SL ne doit pas dépasser Bid - min_dist
            limit = (bid if (bid and bid > 0) else current_price) - min_dist_price
            if new_sl >= limit:
                new_sl = limit
        else:  # SELL
            new_sl = current_price + trailing_dist
            # SL ne doit pas descendre sous Ask + min_dist
            limit = (ask if (ask and ask > 0) else current_price) + min_dist_price
            if new_sl <= limit:
                new_sl = limit

        if not (new_sl == new_sl):  # NaN guard
            return
        new_sl = round(float(new_sl), digits)

        # ----- Ne JAMAIS détendre + éviter les micro-updates (< ~1/2 tick) -----
        cur_sl = pos.get("sl")
        cur_sl = float(cur_sl) if isinstance(cur_sl, (int, float)) else None
        min_move = max(tick_size, point)

        if cur_sl is not None:
            if action == "BUY":
                if not (new_sl > cur_sl + (min_move * 0.5)):
                    return
            else:
                if not (new_sl < cur_sl - (min_move * 0.5)):
                    return

        # ----- Rate-limit par ticket -----
        try:
            min_ms = int(
                self.config_manager.get("dynamic_trailing.min_ms_between_updates", 150)
                or 150
            )
        except Exception:
            min_ms = 150
        if not hasattr(self, "_last_trail_update_ms"):
            self._last_trail_update_ms = {}
        now_ms = int(time.monotonic() * 1000)
        last_ms = int(self._last_trail_update_ms.get(ticket, 0) or 0)
        if now_ms - last_ms < min_ms:
            return

        # ----- Envoi modification SL -----
        ok = False
        try:
            ok = self._modify_sl(ticket, new_sl)
        except Exception as e:
            self.logger.warning(
                f"[TRAILING] _modify_sl exception sur {sym}/{ticket}: {e}"
            )

        if ok:
            self._last_trail_update_ms[ticket] = now_ms
            try:
                self.logger.info(
                    f"[TRAILING] {action} {sym} ticket={ticket}: SL -> {format(new_sl, f'.{digits}f')}"
                )
            except Exception:
                self.logger.info(
                    f"[TRAILING] {action} {sym} ticket={ticket}: SL -> {new_sl}"
                )

    except Exception as e:
        sym_safe = (
            symbol or (pos.get("symbol") if isinstance(pos, dict) else None) or "?"
        )
        self.logger.warning(
            f"[TRAILING] Erreur application trailing sur {sym_safe}/{ticket}: {e}"
        )


def monitor_trailing_stops(self) -> None:
    """
    Trailing **PANIER** en temps réel (peak → drawdown) côté broker.
    - Regroupe les positions par basket_id (robuste objets/dicts via comment 'basket=...').
    - Maintient un SL *par position* équivalent au stop panier : SL_pos = entry_pos ± (peak_pips - distance_pips) * pip_size
    (BUY: '+', SELL: '-'), arrondi aux digits et respect du stops_level broker.
    - N’emploie **aucun TP** (on déplace uniquement le SL).
    - S’appuie sur l’état partagé par monitor_burst_baskets(): self._basket_trail_armed / self._basket_peak_pips,
    et à défaut, lit _meta_trailing encodé lors de la création de l’ordre.
    """
    try:
        mt5 = getattr(self.mt5_connector, "mt5", None)
        if not mt5:
            return

        positions = mt5.positions_get()
        if not positions:
            return

        # État partagé (création tolérante)
        if not hasattr(self, "_basket_trail_armed"):
            self._basket_trail_armed = {}
        if not hasattr(self, "_basket_peak_pips"):
            self._basket_peak_pips = {}
        if not hasattr(self, "_burst_trailing_state"):
            self._burst_trailing_state = {}
        # G2 — registre throttle
        if not hasattr(self, "_last_trail_update_ms"):
            self._last_trail_update_ms = {}
        # H — reentrancy lock set
        if not hasattr(self, "_closing_baskets"):
            self._closing_baskets = set()
        # J — cooldown map
        if not hasattr(self, "_cooldown_until"):
            self._cooldown_until = {}
        # O1 — registres stop-floor anti-régression
        if not hasattr(self, "_basket_sl_floor_buy"):
            self._basket_sl_floor_buy = {}  # floor pour BUY
        if not hasattr(self, "_basket_sl_ceiling_sell"):
            self._basket_sl_ceiling_sell = {}  # ceiling pour SELL

        import re, time
        from datetime import datetime

        # ---------- Helpers ----------
        def _v(obj, key, default=None):
            if isinstance(obj, dict):
                return obj.get(key, default)
            return getattr(obj, key, default)

        def _safe_float(x, default=None):
            try:
                return float(x)
            except Exception:
                return default

        def _symbol_info(symbol: str):
            try:
                return mt5.symbol_info(symbol)
            except Exception:
                return None

        def _pip_size_from(si):
            if not si:
                # fallback par défaut FX 5 digits
                return 0.0001
            point = _safe_float(getattr(si, "point", None), 0.0001) or 0.0001
            digits = int(getattr(si, "digits", 5) or 5)
            pip_points = 10.0 if digits in (3, 5) else 1.0
            return point * pip_points

        def _extract_basket_id(pos):
            # champ direct si injecté
            bid = _v(pos, "basket_id") or _v(pos, "burst_id")
            if bid:
                return str(bid)
            # commentaire 'basket='
            c = str(_v(pos, "comment", "") or "")
            m = re.search(r"burst_scalping\|.*?\|.*?\|basket=([A-Za-z0-9_]+)", c)
            if m:
                return m.group(1)
            # motif 'burst_<SYMBOL>_<hash>'
            m = re.search(r"(burst_[A-Z]{3,6}_[a-f0-9]{6,})", c, re.IGNORECASE)
            if m:
                return m.group(1)
            # fallback synthétique stable
            sym = str(_v(pos, "symbol", "") or "").upper()
            magic = _v(pos, "magic") or ""
            ep = _safe_float(_v(pos, "price_open"), 0.0)
            ep_key = f"{ep:.2f}" if ep is not None else "na"
            return f"synthetic|{sym}|{magic}|{ep_key}"

        def _expected_from_comment(pos):
            c = str(_v(pos, "comment", "") or "")
            m = re.search(r"\|(\d+)/(\d+)", c)
            if m:
                try:
                    return int(m.group(2))
                except Exception:
                    return None
            return None

        # single-master: expected=1 si activé
        def _expected_count_single_master_default():
            try:
                return bool(
                    self.config_manager.get("burst_single_master.enabled", False)
                )
            except Exception:
                return False

        # F/H/J/K — fermer un panier au marché (avec lock, cooldown, budget latence)
        def _close_basket(basket_id, pos_list) -> bool:
            # H: reentrancy lock
            if basket_id in self._closing_baskets:
                self.logger.debug(
                    f"[KILL] déjà en fermeture basket={basket_id} — ignore."
                )
                return False
            self._closing_baskets.add(basket_id)

            # K1 — budget de latence (ms)
            try:
                max_close_ms = int(
                    self.config_manager.get(
                        "dynamic_trailing.max_latency_ms_on_close", 0
                    )
                    or 0
                )
            except Exception:
                max_close_ms = 0
            start_ms = int(time.time() * 1000)

            closed_any = False
            sym_for_cooldown = None
            try:
                tick_cache = {}
                for p in pos_list:
                    sym = _v(p, "symbol")
                    if not sym:
                        continue
                    sym_for_cooldown = sym
                    if sym not in tick_cache:
                        tick_cache[sym] = mt5.symbol_info_tick(sym)
                    tick = tick_cache[sym]

                    # K2 — respect du budget de latence
                    if max_close_ms > 0:
                        now_ms = int(time.time() * 1000)
                        elapsed = now_ms - start_ms
                        if elapsed >= max_close_ms:
                            remaining = sum(
                                1
                                for x in pos_list
                                if (_safe_float(_v(x, "volume"), 0) or 0) > 0
                            )
                            self.logger.warning(
                                f"[KILL] {basket_id} budget {elapsed}ms ≥ {max_close_ms}ms → ABORT fermeture (reste ~{remaining} tickets)."
                            )
                            break

                    pos_type = _v(p, "type")
                    vol = _safe_float(_v(p, "volume"), None)
                    ticket = int(_v(p, "ticket"))
                    if vol is None or vol <= 0:
                        continue

                    # BUY -> SELL (close), SELL -> BUY (close)
                    order_type = (
                        getattr(mt5, "ORDER_TYPE_SELL", 1)
                        if pos_type == getattr(mt5, "POSITION_TYPE_BUY", 0)
                        else getattr(mt5, "ORDER_TYPE_BUY", 0)
                    )

                    price = _safe_float(
                        getattr(
                            tick,
                            (
                                "ask"
                                if order_type == getattr(mt5, "ORDER_TYPE_BUY", 0)
                                else "bid"
                            ),
                            None,
                        ),
                        None,
                    )

                    req = {
                        "action": getattr(mt5, "TRADE_ACTION_DEAL", 1),
                        "symbol": sym,
                        "position": ticket,
                        "type": order_type,
                        "volume": vol,
                        "price": price,
                        "deviation": 20,
                        "magic": _v(p, "magic"),
                        "comment": f"burst_close|basket={basket_id}",
                    }
                    result = self.mt5_connector.order_send(req)
                    if result and getattr(result, "retcode", None) == getattr(
                        mt5, "TRADE_RETCODE_DONE", 10009
                    ):
                        closed_any = True
                        self.logger.warning(
                            f"[KILL] Close ticket={ticket} basket={basket_id} OK"
                        )
                    else:
                        self.logger.warning(
                            f"[KILL] ❌ Close ticket={ticket} basket={basket_id} retcode={getattr(result,'retcode','N/A')}"
                        )

                # J — cooldown après fermeture du panier
                if closed_any and sym_for_cooldown:
                    try:
                        cd = float(
                            self.config_manager.get("cooldown_after_exit_s", 0) or 0.0
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
                            until = time.time() + cd
                            self._cooldown_until[str(sym_for_cooldown).upper()] = until
                            self.logger.info(
                                f"[COOLDOWN] {str(sym_for_cooldown).upper()} bloqué {int(cd)}s après fermeture panier '{basket_id}'."
                            )
                    except Exception as _e:
                        self.logger.warning(f"[COOLDOWN] set KO: {_e}")
            except Exception as _e:
                self.logger.error(
                    f"[KILL] Erreur close basket={basket_id}: {_e}", exc_info=True
                )
            finally:
                # H2 — libération du lock + O4 purge des bornes
                self._closing_baskets.discard(basket_id)
                self._basket_sl_floor_buy.pop(basket_id, None)
                self._basket_sl_ceiling_sell.pop(basket_id, None)
            return closed_any

        # 1) Regrouper par basket_id
        baskets = {}
        for p in positions:
            bid = _extract_basket_id(p)
            baskets.setdefault(bid, []).append(p)

        # 2) Parcours des paniers
        for basket_id, pos_list in baskets.items():
            if not pos_list:
                continue

            # symbol/direction communs (burst homogène)
            symbol = _v(pos_list[0], "symbol")
            if not symbol:
                continue
            si = _symbol_info(symbol)
            if not si:
                continue

            digits = int(getattr(si, "digits", 5) or 5)
            point = _safe_float(getattr(si, "point", None), 0.0001) or 0.0001
            pip_size = _pip_size_from(si)

            # current price
            try:
                tick = mt5.symbol_info_tick(symbol)
            except Exception:
                tick = None
            if not tick:
                continue

            # direction
            typ0 = _v(pos_list[0], "type", None)
            direction = (
                "BUY" if typ0 == getattr(mt5, "POSITION_TYPE_BUY", 0) else "SELL"
            )

            ref_price = (
                getattr(tick, "bid", None)
                if direction == "BUY"
                else getattr(tick, "ask", None)
            )
            if ref_price is None:
                ref_price = _safe_float(_v(pos_list[0], "price_current"), None)
            if ref_price is None:
                continue

            # PnL panier (pips)
            entries = [_safe_float(_v(p, "price_open")) for p in pos_list]
            entries = [x for x in entries if x is not None]
            if not entries:
                continue
            avg_entry = sum(entries) / max(1, len(entries))
            pnl_pips = (
                ((ref_price - avg_entry) / pip_size)
                if direction == "BUY"
                else ((avg_entry - ref_price) / pip_size)
            )

            # trailing config (priorité: état partagé -> meta ticket -> défauts)
            trail = self._burst_trailing_state.get(basket_id) or {}
            trigger_pips = _safe_float(trail.get("trigger"), None) or _safe_float(
                trail.get("trigger_pips"), None
            )
            distance_pips = _safe_float(trail.get("distance"), None) or _safe_float(
                trail.get("distance_pips"), None
            )
            if trigger_pips is None or distance_pips is None:
                first_ticket = _v(pos_list[0], "ticket")
                trailing_cfg = None
                if hasattr(self, "_open_positions"):
                    trailing_cfg = (
                        (self._open_positions.get(first_ticket, {}) or {}).get(
                            "_meta", {}
                        )
                        or {}
                    ).get("trailing")
                if trailing_cfg:
                    trigger_pips = _safe_float(trailing_cfg.get("trigger_pips"), 10.0)
                    distance_pips = _safe_float(
                        trailing_cfg.get("distance_pips")
                        or trailing_cfg.get("step_pips"),
                        5.0,
                    )
                else:
                    trigger_pips = 10.0
                    distance_pips = 5.0

            # Gate d’armement (min_hold_ms) — support 2 emplacements
            try:
                _mh_dyn = self.config_manager.get("dynamic_trailing.min_hold_ms", None)
                if _mh_dyn is None:
                    _mh_dyn = self.config_manager.get(
                        "burst_single_master.min_hold_ms", 0
                    )
                min_hold_ms = int(_mh_dyn or 0)
            except Exception:
                min_hold_ms = 0

            # Âge du panier (ms)
            age_ms = 0.0
            try:
                ts_list = []
                for p in pos_list:
                    tmsc = _safe_float(_v(p, "time_msc"))
                    if tmsc:
                        ts_list.append(tmsc)
                        continue
                    tsec = _safe_float(_v(p, "time"))
                    if tsec:
                        ts_list.append(float(tsec) * 1000.0)
                        continue
                    iso = _v(p, "open_time")
                    if iso:
                        try:
                            ts_list.append(
                                datetime.fromisoformat(str(iso)).timestamp() * 1000.0
                            )
                        except Exception:
                            pass
                if ts_list:
                    age_ms = max(0.0, time.time() * 1000.0 - min(ts_list))
            except Exception:
                pass

            # Panier "plein" (info)
            exp_n = _expected_from_comment(pos_list[0])
            if exp_n is None and _expected_count_single_master_default():
                exp_n = 1
            is_full = exp_n is not None and len(pos_list) >= exp_n

            # Kill-switch SPREAD (si armé)
            try:
                max_spread_pts = float(
                    self.config_manager.get(
                        "dynamic_trailing.max_spread_points_when_armed", 0
                    )
                    or 0
                )
            except Exception:
                max_spread_pts = 0.0
            if max_spread_pts > 0 and self._basket_trail_armed.get(basket_id, False):
                try:
                    bid = _safe_float(getattr(tick, "bid", None), None)
                    ask = _safe_float(getattr(tick, "ask", None), None)
                    if bid is not None and ask is not None and point > 0:
                        spread_pts = (ask - bid) / point
                        if spread_pts >= max_spread_pts:
                            self.logger.warning(
                                f"[KILL] {basket_id} spread {spread_pts:.1f} ≥ {max_spread_pts:.1f} pts → CLOSE"
                            )
                            if _close_basket(basket_id, pos_list):
                                continue
                except Exception:
                    pass

            # Armement (seuil + délai)
            if pnl_pips >= float(trigger_pips) and (age_ms >= float(min_hold_ms)):
                if not self._basket_trail_armed.get(basket_id, False):
                    self._basket_trail_armed[basket_id] = True
                    self._basket_peak_pips[basket_id] = pnl_pips
                    self.logger.info(
                        f"[TRAIL-PANIER] ARMÉ basket={basket_id} @ {pnl_pips:.1f}p (trigger={float(trigger_pips):.1f}p, full={is_full}, age={age_ms:.0f}ms)"
                    )
                else:
                    if pnl_pips > float(
                        self._basket_peak_pips.get(basket_id, pnl_pips)
                    ):
                        self._basket_peak_pips[basket_id] = pnl_pips

            if not self._basket_trail_armed.get(basket_id, False):
                continue  # pas armé

            # Stop cible relatif à l'ENTRY
            peak = float(self._basket_peak_pips.get(basket_id, pnl_pips))
            target_stop_pips = peak - float(distance_pips)
            if target_stop_pips < 0:
                target_stop_pips = 0.0

            # N — quantification en marches
            try:
                backoff_step = float(
                    self.config_manager.get("dynamic_trailing.backoff_step_points", 0)
                    or 0.0
                )
            except Exception:
                backoff_step = 0.0
            if backoff_step > 0.0:
                target_stop_pips = int(target_stop_pips / backoff_step) * backoff_step

            # Kill-switch PULLBACK max
            try:
                max_pullback_pts = float(
                    self.config_manager.get("dynamic_trailing.max_pullback_points", 0)
                    or 0
                )
            except Exception:
                max_pullback_pts = 0.0
            dd = peak - pnl_pips
            if max_pullback_pts > 0 and dd >= max_pullback_pts and pnl_pips > 0.0:
                self.logger.warning(
                    f"[KILL] {basket_id} pullback {dd:.1f}p ≥ {max_pullback_pts:.1f}p → CLOSE"
                )
                if _close_basket(basket_id, pos_list):
                    continue

            # stops_level broker
            stops_level_points = _safe_float(
                getattr(si, "trade_stops_level", None), None
            )
            if stops_level_points is None:
                stops_level_points = _safe_float(getattr(si, "stops_level", None), 0.0)
            min_dist_price = (stops_level_points or 0.0) * point

            # Breakeven Lock
            try:
                be_lock_pts = float(
                    self.config_manager.get(
                        "dynamic_trailing.breakeven_lock_points", 0.0
                    )
                    or 0.0
                )
            except Exception:
                be_lock_pts = 0.0
            be_lock_price = None
            if be_lock_pts > 0.0:
                be_lock_price = (
                    (avg_entry + be_lock_pts * pip_size)
                    if direction == "BUY"
                    else (avg_entry - be_lock_pts * pip_size)
                )

            # G2 — throttle
            try:
                throttle_ms = int(
                    self.config_manager.get(
                        "dynamic_trailing.trail_update_throttle_ms", 250
                    )
                    or 250
                )
            except Exception:
                throttle_ms = 250
            now_ms = int(time.time() * 1000)
            last_ms = int(self._last_trail_update_ms.get(basket_id, 0) or 0)
            allow_update = (throttle_ms <= 0) or ((now_ms - last_ms) >= throttle_ms)

            updates = 0
            updated_tickets = []
            failed_tickets = []

            if allow_update:
                # Calcul et envoi des SL par ticket
                for p in pos_list:
                    entry = _safe_float(_v(p, "price_open"))
                    if entry is None:
                        continue

                    if direction == "BUY":
                        desired_sl = entry + target_stop_pips * pip_size
                        # respect stops_level: SL <= Bid - min_dist
                        lim = (getattr(tick, "bid", None) or ref_price) - min_dist_price
                        if lim is not None:
                            desired_sl = min(desired_sl, lim)
                        # ne jamais dépasser le prix courant
                        desired_sl = min(desired_sl, ref_price)
                        # clamp BE lock
                        if be_lock_price is not None:
                            desired_sl = max(desired_sl, be_lock_price)
                            desired_sl = min(desired_sl, ref_price)
                        # O2 — floor anti-régression (BUY)
                        floor = self._basket_sl_floor_buy.get(basket_id)
                        if floor is not None:
                            desired_sl = max(desired_sl, float(floor))
                    else:  # SELL
                        desired_sl = entry - target_stop_pips * pip_size
                        # respect stops_level: SL >= Ask + min_dist
                        lim = (getattr(tick, "ask", None) or ref_price) + min_dist_price
                        if lim is not None:
                            desired_sl = max(desired_sl, lim)
                        # ne jamais dépasser le prix courant
                        desired_sl = max(desired_sl, ref_price)
                        # clamp BE lock
                        if be_lock_price is not None:
                            desired_sl = min(desired_sl, be_lock_price)
                            desired_sl = max(desired_sl, ref_price)
                        # O2 — ceiling anti-régression (SELL)
                        ceiling = self._basket_sl_ceiling_sell.get(basket_id)
                        if ceiling is not None:
                            desired_sl = min(desired_sl, float(ceiling))

                    desired_sl = round(float(desired_sl), digits)
                    current_sl = _safe_float(_v(p, "sl"), None)

                    improve = (
                        direction == "BUY"
                        and (current_sl is None or desired_sl > current_sl)
                    ) or (
                        direction == "SELL"
                        and (current_sl is None or desired_sl < current_sl)
                    )
                    if not improve:
                        continue

                    sl_update = {
                        "action": getattr(mt5, "TRADE_ACTION_SLTP", 3),
                        "symbol": symbol,
                        "position": int(_v(p, "ticket")),
                        "sl": desired_sl,
                        "tp": _safe_float(_v(p, "tp"), 0.0) or 0.0,
                    }
                    result = self.mt5_connector.order_send(sl_update)
                    if result and getattr(result, "retcode", None) == getattr(
                        mt5, "TRADE_RETCODE_DONE", 10009
                    ):
                        updates += 1
                        ticket_id = int(_v(p, "ticket"))
                        updated_tickets.append((ticket_id, desired_sl))
                    else:
                        ticket_id = int(_v(p, "ticket"))
                        failed_tickets.append(ticket_id)

                # L3 — log agrégé + MAJ throttle
                if updates > 0:
                    self._last_trail_update_ms[basket_id] = now_ms

                if updates > 0 and updated_tickets:
                    tickets_str = ",".join(str(tk) for tk, _ in updated_tickets[:10])
                    sl_preview = updated_tickets[0][1]
                    self.logger.info(
                        f"[TRAIL-PANIER] {symbol} basket={basket_id} UPDATE_SL x{updates} "
                        f"tickets=[{tickets_str}{'...' if len(updated_tickets) > 10 else ''}] -> SL≈{sl_preview} "
                        f"(peak={peak:.1f}p target={target_stop_pips:.1f}p pnl={pnl_pips:.1f}p)"
                    )

                    # O3 — mise à jour des bornes anti-régression par panier
                    new_values = [float(slv) for _, slv in updated_tickets]
                    if direction == "BUY":
                        prev = self._basket_sl_floor_buy.get(basket_id)
                        new_floor = (
                            max(new_values)
                            if prev is None
                            else max(prev, max(new_values))
                        )
                        self._basket_sl_floor_buy[basket_id] = new_floor
                    else:
                        prev = self._basket_sl_ceiling_sell.get(basket_id)
                        new_ceiling = (
                            min(new_values)
                            if prev is None
                            else min(prev, min(new_values))
                        )
                        self._basket_sl_ceiling_sell[basket_id] = new_ceiling

                if failed_tickets:
                    self.logger.warning(
                        f"[TRAIL-PANIER] {symbol} basket={basket_id} ❌ SL FAIL tickets={failed_tickets}"
                    )
            else:
                self.logger.debug(
                    f"[TRAIL-PANIER] throttle {basket_id} ({now_ms - last_ms}ms < {throttle_ms}ms) — skip update SL."
                )

            if updates == 0 and allow_update:
                self.logger.debug(
                    f"[TRAIL-PANIER] Aucune amélioration SL requise (basket={basket_id}, pnl={pnl_pips:.1f}p, peak={peak:.1f}p)."
                )

    except Exception as e:
        self.logger.error(
            f"[TRAIL-PANIER] Erreur monitor_trailing_stops: {e}", exc_info=True
        )


def _modify_sl(self, ticket: int, new_sl: float) -> bool:
    """Envoie une requête de modification de SL au broker (TRADE_ACTION_SLTP)."""
    try:
        mt5 = getattr(getattr(self, "mt5_connector", None), "mt5", None) or getattr(
            self, "mt5", None
        )
        action_sltp = getattr(mt5, "TRADE_ACTION_SLTP", None)  # sûr pour MT5
        if action_sltp is None:
            # fallback (rare) : on tente quand même avec l'action déjà mappée
            action_sltp = getattr(self, "TRADE_ACTION_MODIFY", None)

        request = {
            "action": action_sltp,
            "position": int(ticket),
            "sl": float(new_sl),
            "tp": 0.0,  # on ne touche pas au TP (burst = pas de TP)
        }
        result = self.mt5_connector.order_send(request)
        ok = bool(result) and (
            getattr(result, "retcode", None)
            == getattr(self, "TRADE_RETCODE_DONE", None)
        )
        if ok:
            self.logger.info(f"Trailing SL modifié pour pos#{ticket} -> {new_sl}")
            try:
                if hasattr(self, "_open_positions") and ticket in self._open_positions:
                    self._open_positions[ticket]["sl"] = float(new_sl)
            except Exception:
                pass
            return True
        else:
            self.logger.warning(
                f"Échec modif trailing SL pour pos#{ticket} (retcode={getattr(result, 'retcode', 'N/A')})"
            )
            return False
    except Exception as e:
        self.logger.error(f"Erreur _modify_sl: {e}", exc_info=True)
        return False


def _bars_since(self, open_time_str: str) -> int:
    """
    Retourne le nombre de barres écoulées depuis open_time.
    Basé sur timeframe en minutes (configurable: execution.bar_size_minutes).
    """

    try:
        if not open_time_str:
            return 0
        open_time = datetime.fromisoformat(str(open_time_str))
        now = datetime.utcnow()
        elapsed_minutes = (now - open_time).total_seconds() / 60.0
        bar_size_min = int(self.config_manager.get("execution.bar_size_minutes", 1))
        return int(elapsed_minutes // bar_size_min)
    except Exception:
        return 0
