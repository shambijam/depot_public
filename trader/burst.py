# trader/burst.py - Gestion des paniers (burst) SANS TRAILING
from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, Optional, List, Tuple

# Import pour calcul correct de la valeur par pip
from trader.sltp import calculate_pip_value_per_lot


# ======================================================================================
# Helpers génériques
# ======================================================================================


def _attach_burst_metadata(self, trade_decision: dict) -> dict:
    """
    Attache des métadonnées de burst (basket_id, horodatage, flag 'burst') à une décision.
    """
    import uuid, time as _t

    if not trade_decision:
        return trade_decision

    basket_id = trade_decision.get("basket_id") or f"burst_{uuid.uuid4().hex[:8]}"
    trade_decision["basket_id"] = basket_id
    trade_decision["burst_timestamp"] = int(_t.time())
    trade_decision.setdefault("meta", {})["burst"] = True
    return trade_decision


def open_burst_basket(self, base_request: dict, burst_size: int) -> dict:
    """
    Ouvre un panier de 'burst_size' tickets avec le MÊME SL/TP et le MÊME basket_id.
    - Conserve le mode single-basket (guardrails effectués en amont).
    - Comment ultra-compact ≤16 chars: 'bs_<id8hex>' (ex: bs_abc12345 = 11 chars)
      (on n'ajoute PAS 'i/n' pour rester sous 31 chars).
    - Met à jour self._last_burst_time pour le cooldown.
    """
    print("🔍 [BURST_DEBUG] FONCTION APPELÉE", flush=True)
    import uuid, time as _t

    # 🔍 DEBUG: Vérifier ce que contient base_request
    print(f"🔍 [BURST_DEBUG] base_request keys: {list(base_request.keys())}", flush=True)
    print(f"🔍 [BURST_DEBUG] has trade_decision: {'trade_decision' in base_request}", flush=True)

    if not isinstance(base_request, dict) or burst_size is None:
        return {"status": "failed", "reason": "bad_args"}

    burst_size = int(burst_size) if burst_size else 1
    if burst_size < 1:
        burst_size = 1

    # Génère un basket_id compact (8 hex) pour respecter limite broker (souvent 16-17 chars)
    basket_id = f"{uuid.uuid4().hex[:8]}"
    # Comment MT5 ultra-compact ≤16 chars: bs_abc12345 (11 chars)
    comment = f"bs_{basket_id}"

    # Prépare requête type (copie défensive)
    def _base_req_copy():
        r = dict(base_request)
        r["comment"] = comment  # compact + détectable
        # On s’assure que SL/TP existent tels que préparés par order_builder
        r["sl"] = float(base_request.get("sl", 0.0) or 0.0)
        if base_request.get("tp") is not None:
            r["tp"] = float(base_request.get("tp", 0.0) or 0.0)
        return r

    tickets, errors = [], []

    # ⚡ OPTION 3: PARALLÉLISATION — Envoi simultané des ordres
    def _send_single_order(idx: int) -> tuple:
        """Envoie un ordre unique (fonction worker pour ThreadPoolExecutor)."""
        req = _base_req_copy()
        burst_num = idx + 1

        print(f"🔍 [SL_TRACE][BURST_#{burst_num}/{burst_size}] Avant envoi | SL={req.get('sl')} | TP={req.get('tp')} | symbol={req.get('symbol')} | basket_id={basket_id}", flush=True)

        try:
            res = self.execute_order(req)
            if res and res.get("status") in {"sent", "placed", "filled"}:
                tk = res.get("order") or res.get("deal") or res.get("ticket")
                if tk:
                    print(f"🔍 [SL_TRACE][BURST_#{burst_num}/{burst_size}] Ordre envoyé | ticket={tk} | status={res.get('status')}", flush=True)
                    return ("success", int(tk), None)
                else:
                    return ("error", None, res)
            else:
                return ("error", None, res)
        except Exception as e:
            return ("error", None, {"exc": str(e)})

    # Parallélisation avec ThreadPoolExecutor (max_workers = burst_size pour envoi simultané)
    try:
        parallel_enabled = bool(self.config_manager.get("burst_parallel_send", True))
    except Exception:
        parallel_enabled = True

    if parallel_enabled and burst_size > 1:
        # ⚡ ENVOI PARALLÈLE — Toutes les positions simultanément (~100ms au lieu de 840ms)
        with ThreadPoolExecutor(max_workers=burst_size) as executor:
            futures = {executor.submit(_send_single_order, idx): idx for idx in range(burst_size)}

            for future in as_completed(futures):
                try:
                    status, ticket, error = future.result()
                    if status == "success" and ticket:
                        tickets.append(ticket)
                    elif error:
                        errors.append(error)
                except Exception as e:
                    errors.append({"exc": str(e)})

        # Un seul micro-délai à la fin pour stabilisation MT5
        _t.sleep(0.01)
    else:
        # FALLBACK: Mode séquentiel (si parallélisation désactivée dans config)
        for idx in range(burst_size):
            status, ticket, error = _send_single_order(idx)
            if status == "success" and ticket:
                tickets.append(ticket)
            elif error:
                errors.append(error)

            # Micro-délai anti-rafale en mode séquentiel
            _t.sleep(float(self.config_manager.get("burst_send_sleep_s", 0.005) or 0.005))

    # marque le cooldown “dernier burst”
    try:
        self._last_burst_time = _t.time()
    except Exception:
        pass

    status = (
        "ok"
        if len(tickets) == burst_size and not errors
        else "partial" if tickets else "failed"
    )

    # ✅ AJOUTÉ (25 Nov 2025): Journalisation ENTRÉE trade pour analyse data-driven
    # DEBUG: Tracer les conditions
    has_tickets = bool(tickets)
    has_attr = hasattr(self, 'trade_logger')
    logger_not_none = has_attr and self.trade_logger is not None
    print(f"🔍 [TRADE_LOG][DEBUG] tickets={has_tickets} hasattr={has_attr} not_none={logger_not_none}", flush=True)

    if tickets and hasattr(self, 'trade_logger') and self.trade_logger is not None:
        try:
            # Récupérer trade_decision depuis base_request
            td = base_request.get("trade_decision", {})
            print(f"🔍 [TRADE_LOG][DEBUG] trade_decision keys: {list(td.keys())}", flush=True)

            # Les données FusionManager sont dans td["fusion_data"]
            fusion_data = td.get("fusion_data", {})
            print(f"🔍 [TRADE_LOG][DEBUG] fusion_data keys: {list(fusion_data.keys())}", flush=True)

            # Score final
            score_final = float(fusion_data.get("fused_confidence", 0.0))

            # Components
            components = fusion_data.get("components", {})

            n_of = components.get("orderflow", {})
            n_fp = components.get("validator", {})  # FusionManager retourne "validator" pas "footprint"
            n_tr = components.get("trigger", {})

            of_score = float(n_of.get("score", 0.0))
            fp_score = float(n_fp.get("score", 0.0))
            score_base = (of_score + fp_score) / 2.0

            # Trigger
            trigger_type = str(n_tr.get("type", "none"))
            trigger_confidence = float(n_tr.get("score", 0.0))
            trigger_boost = float(fusion_data.get("trigger_boost", 0.0))

            # Qualité données
            fp_raw = n_fp.get("raw", {})
            fp_summary = fp_raw.get("summary", {})
            if isinstance(fp_summary, str):
                import json
                try:
                    fp_summary = json.loads(fp_summary)
                except:
                    fp_summary = {}

            tick_count = int(fp_summary.get("tick_count", 0))
            coverage_s = float(fp_summary.get("coverage_s", 0.0))
            status_of = str(n_of.get("status", "UNKNOWN"))
            status_fp = str(n_fp.get("status", "UNKNOWN"))
            quality_multiplier = float(fusion_data.get("quality_multiplier", 1.0))

            # Cohérence
            coherence = fusion_data.get("coherence", {})
            aligned_3_of_3 = bool(coherence.get("aligned3", False))
            matrix = coherence.get("matrix", {})
            conflicts_count = sum(1 for v in matrix.values() if v == "conflict")

            # ✅ Phase de marché (06 DEC 2025)
            # Extraire depuis td["context"] et fusion_data
            context_data = td.get("context", {})
            phase_data = context_data.get("phase", {})

            market_phase = phase_data.get("phase") if isinstance(phase_data, dict) else None
            market_regime = phase_data.get("regime") if isinstance(phase_data, dict) else None
            phase_confidence = phase_data.get("confidence_score") if isinstance(phase_data, dict) else None

            # Régime VWAP depuis components
            n_vw = components.get("vwap", {})
            vwap_regime = n_vw.get("regime")
            vwap_score_value = n_vw.get("score")

            # Infos position
            symbol = str(base_request.get("symbol", "UNKNOWN"))
            direction = "BUY" if base_request.get("action") == "BUY" else "SELL"
            entry_price = float(base_request.get("price", 0.0))
            volume_per_ticket = float(base_request.get("volume", 0.0))
            volume_total = volume_per_ticket * len(tickets)  # Volume réel des positions ouvertes
            sl_price = float(base_request.get("sl", 0.0))
            tp_price = float(base_request.get("tp", 0.0))

            # Log entrée
            self.trade_logger.log_trade_entry(
                basket_id=basket_id,
                symbol=symbol,
                direction=direction,
                entry_price=entry_price,
                volume=volume_total,
                sl_price=sl_price,
                tp_price=tp_price,
                # Scoring
                score_final=score_final,
                score_base=score_base,
                of_score=of_score,
                fp_score=fp_score,
                trigger_type=trigger_type,
                trigger_confidence=trigger_confidence,
                trigger_boost=trigger_boost,
                quality_multiplier=quality_multiplier,
                # Qualité
                tick_count=tick_count,
                coverage_s=coverage_s,
                status_of=status_of,
                status_fp=status_fp,
                # Cohérence
                aligned_3_of_3=aligned_3_of_3,
                conflicts_count=conflicts_count,
                # Contexte
                strategy="scalping",
                burst_size=len(tickets),
                # ✅ Phase de marché (06 DEC 2025)
                market_phase=market_phase,
                market_regime=market_regime,
                vwap_regime=vwap_regime,
                phase_confidence=phase_confidence,
                vwap_score=vwap_score_value,
                # Extra
                tickets=tickets,
                status=status
            )
            self.logger.info(f"✅ [TRADE_LOG][DEBUG] log_trade_entry() appelé avec succès pour basket {basket_id}")
        except Exception as e:
            self.logger.error(f"❌ [TRADE_LOG] Erreur log entrée basket {basket_id}: {e}", exc_info=True)

    return {
        "status": status,
        "basket_id": basket_id,
        "tickets": tickets,
        "errors": errors,
        "size": burst_size,
        "comment": comment,
    }


# ======================================================================================
# Fermeture d’un panier (close immédiate, sans trailing)
# ======================================================================================


def close_burst_basket(self, basket_id: str) -> Dict[str, Any]:
    """
    Ferme immédiatement TOUTES les positions appartenant au panier `basket_id`.

    - Identifie les positions par motif strict dans comment/basket_id (regex).
    - Tente une fermeture 'bulk'; sinon fallback ticket par ticket.
    - Annule UNIQUEMENT les ordres en attente dont le commentaire contient le `basket_id`.
    - Mode urgence: si nécessaire, pousse un SL 'au marché' (sans jamais détendre un SL existant).
    - Purge des états internes de panier + pose d’un COOLDOWN par symbole si configuré.
    """
    if not basket_id or not str(basket_id).strip():
        self.logger.warning("[CLOSE] appelé sans basket_id")
        return {"closed": False, "reason": "no_basket_id"}

    mt5c = getattr(self, "mt5_connector", None)
    if not mt5c:
        self.logger.error("[CLOSE] mt5_connector indisponible.")
        return {"closed": False, "reason": "no_connector"}
    mt5 = getattr(mt5c, "mt5", None)
    if not mt5:
        self.logger.error("[CLOSE] module MT5 indisponible.")
        return {"closed": False, "reason": "no_mt5_module"}

    # bot_magic une fois pour toutes
    try:
        BOT_MAGIC = int(
            getattr(self, "magic", 0)
            or self.config_manager.get("magic_number", 0)
            or self.config_manager.get("defaults.magic_number", 0)
            or 0
        )
    except Exception:
        BOT_MAGIC = 0

    def _is_bot_position(p) -> bool:
        try:
            return int(getattr(p, "magic", getattr(p, "magic", 0))) == BOT_MAGIC
        except Exception:
            return False

    def _extract_basket_id_strict(pos) -> Optional[str]:
        """
        Retourne le basket_id UNIQUEMENT si le comment contient 'bs_<id>'.
        Aucune heuristique 'synthetic|...' ici (trop dangereux pour CLOSE).
        """
        try:
            c = str(getattr(pos, "comment", "") or "")
        except Exception:
            c = ""
        m = re.search(r"bs_([a-f0-9]{8})", c)
        return m.group(1) if m else None

    # ---------- Fenêtre de temps optionnelle ----------
    try:
        max_close_ms = int(
            self.config_manager.get("burst_close.max_latency_ms", 0) or 0
        )
    except Exception:
        max_close_ms = 0
    start_ms = int(time.time() * 1000)

    def _time_left_ok() -> bool:
        if max_close_ms <= 0:
            return True
        return (int(time.time() * 1000) - start_ms) < max_close_ms

    # ---------- États internes ----------
    if not hasattr(self, "_closing_baskets"):
        self._closing_baskets = set()
    if not hasattr(self, "_cooldown_until"):
        self._cooldown_until = {}

    if basket_id in self._closing_baskets:
        self.logger.info(f"[CLOSE] Ignoré: '{basket_id}' déjà en fermeture.")
        return {"closed": False, "reason": "already_closing"}
    self._closing_baskets.add(basket_id)

    # ---------- Helpers locaux ----------
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

    def _extract_basket_id(pos) -> str:
        """Reconstruit un basket_id depuis les métadonnées position/commande."""
        bid = _v(pos, "basket_id") or _v(pos, "burst_id")
        if bid:
            return str(bid)
        c = str(_v(pos, "comment", "") or "")
        m = re.search(r"bs_([a-f0-9]{8})", c)
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
            pos = mt5c.get_positions() or []
            # ⛔ On ne garde que nos positions (magic du bot)
            return [p for p in pos if _is_bot_position(p)]
        except Exception as e:
            self.logger.error(f"[CLOSE] impossible de lire les positions: {e}")
            return []

    def _list_pending_orders():
        try:
            if hasattr(mt5c, "get_orders"):
                ords = mt5c.get_orders() or []
            elif hasattr(mt5, "orders_get"):
                ords = mt5.orders_get() or []
            else:
                ords = []
            # ⛔ On ne garde que nos ordres (magic du bot)
            out = []
            for od in ords:
                try:
                    if int(getattr(od, "magic", 0)) == BOT_MAGIC:
                        out.append(od)
                except Exception:
                    pass
            return out
        except Exception:
            return []

    def _cancel_pending_orders_for_basket(bid: str) -> int:
        """Annule SEULEMENT les ordres en attente du bot dont le commentaire contient
        explicitement le tag 'bs_<bid>'. Filtrage strict + budget latence.
        """
        pend = _list_pending_orders()
        if not pend or not bid:
            return 0

        cancelled = 0
        # motif strict pour nouveau format compact
        pat = re.compile(rf"bs_{re.escape(bid)}")

        # (optionnel) limite de sécurité pour éviter de balayer trop d’ordres
        try:
            max_cancel = int(
                self.config_manager.get("burst_close.max_pending_cancel", 50) or 50
            )
        except Exception:
            max_cancel = 50

        # récupère le magic du bot (même logique que côté connector)
        try:
            BOT_MAGIC = int(
                getattr(self, "magic", 0)
                or self.config_manager.get("magic_number", 0)
                or self.config_manager.get("defaults.magic_number", 0)
                or 0
            )
        except Exception:
            BOT_MAGIC = 0

        for od in pend:
            if not _time_left_ok():
                self.logger.warning(
                    "[CLOSE] Budget de latence atteint pendant l'annulation des pendings."
                )
                break

            try:
                # ⛔ ne jamais toucher aux ordres qui ne sont pas du bot
                try:
                    if int(getattr(od, "magic", 0)) != BOT_MAGIC:
                        continue
                except Exception:
                    continue

                comment = str(_v(od, "comment", "") or "")
                # ⛔ exige le tag exact du panier
                if not pat.search(comment):
                    continue

                order_id = _v(od, "order") or _v(od, "ticket")
                if order_id is None:
                    continue

                req = {"action": mt5.TRADE_ACTION_REMOVE, "order": int(order_id)}
                res = mt5c.order_send(req)

                if res and getattr(res, "retcode", None) == mt5.TRADE_RETCODE_DONE:
                    cancelled += 1
                    self.logger.info(
                        f"[CLOSE] Pending #{order_id} annulé (basket={bid})."
                    )
                else:
                    self.logger.warning(
                        f"[CLOSE] Annulation ordre #{order_id} échec "
                        f"retcode={getattr(res,'retcode',None)} comment={getattr(res,'comment','?')}"
                    )

                if cancelled >= max_cancel:
                    self.logger.warning(
                        f"[CLOSE] Seuil d'annulations atteint ({max_cancel}). Stop."
                    )
                    break

            except Exception as e:
                self.logger.error(f"[CLOSE] Annulation ordre KO: {e}")

        return cancelled

    def _force_sl_sweep(symbol: str, positions: list) -> bool:
        """
        Dernier recours: pousse un SL quasi-au-marché pour forcer la sortie (sans détendre le SL existant).
        Buffer = max(tick, stops_level*point, freeze_level*point) + 1*tick.
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

    def _purge_states(bid: str, symbol_hint: Optional[str] = None):
        """Nettoie les états internes liés au panier et applique le cooldown par symbole si configuré."""
        # COOLDOWN post-exit par symbole
        try:
            cd = float(self.config_manager.get("cooldown_after_exit_s", 0) or 0.0)
            if cd <= 0:
                cd = float(
                    self.config_manager.get(
                        "entry_rules.scalping.burst_scalping.cooldown_after_exit_s", 0
                    )
                    or 0.0
                )
            if cd > 0 and symbol_hint:
                self._cooldown_until[str(symbol_hint).upper()] = time.time() + cd
                self.logger.info(
                    f"[COOLDOWN] {str(symbol_hint).upper()} bloqué {int(cd)}s après fermeture panier '{bid}'."
                )
        except Exception as _e:
            self.logger.warning(f"[COOLDOWN] set KO: {_e}")

        # Libération éventuelle des locks par symbole
        if hasattr(self, "_active_burst_locks"):
            if symbol_hint:
                try:
                    self._active_burst_locks.pop(str(symbol_hint).upper(), None)
                except Exception:
                    pass
            else:
                try:
                    for k, v in list(self._active_burst_locks.items()):
                        if (v or {}).get("basket_id") == bid:
                            self._active_burst_locks.pop(k, None)
                except Exception:
                    pass

    # ---------- Core avec déverrouillage garanti ----------
    cancelled = 0
    try:
        # 1) positions du panier
        try:
            all_pos = _list_open_positions()
            # ⛔ On retient UNIQUEMENT nos positions ET tag exact du panier dans 'comment'
            basket_pos = []
            for p in all_pos:
                if not _is_bot_position(p):
                    continue
                bid = _extract_basket_id_strict(p)
                if bid == basket_id:
                    basket_pos.append(p)

        except Exception:
            basket_pos = []

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

        # 2) annule pendings attachés (avant fermeture)
        if _time_left_ok():
            cancelled = _cancel_pending_orders_for_basket(basket_id)

        # 3) Bulk close si dispo + post-check
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
                time.sleep(0.05)
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
                    tickets = [
                        int(_v(p, "ticket"))
                        for p in left
                        if _v(p, "ticket") is not None
                    ]
            except Exception as e:
                self.logger.error(f"[CLOSE] bulk close_positions KO: {e}")

        # 4) Fallback ticket par ticket
        if tickets and _time_left_ok():
            for tk in tickets:
                if not _time_left_ok():
                    self.logger.warning(
                        "[CLOSE] Budget de latence atteint pendant le fallback ticket-by-ticket."
                    )
                    break
                try:
                    # ✅ On appelle directement le connector (pas self.close_position qui peut ne pas exister)
                    mt5c.close_position(int(tk))

                    # Petit délai puis vérif effective de fermeture
                    time.sleep(0.02)
                    still = [p for p in _list_open_positions() if _v(p, "ticket") == tk]
                    if still:
                        tickets_failed += 1
                        self.logger.warning(
                            f"[CLOSE] ticket {tk} non fermé (fallback)."
                        )
                    else:
                        tickets_closed += 1

                except Exception as e:
                    tickets_failed += 1
                    self.logger.error(f"[CLOSE] Échec clôture ticket {tk}: {e}")

        # 5) Si reste quelque chose → mode urgence SL
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

        # ✅ AJOUTÉ (25 Nov 2025): Journalisation SORTIE trade si toutes positions fermées
        if all_closed and hasattr(self, 'trade_logger') and self.trade_logger is not None:
            try:
                # Calculer PnL total depuis les positions fermées
                total_pnl_pips = 0.0
                total_pnl_usd = 0.0
                exit_prices = []

                # Récupérer symbol_info pour calcul précis
                symbol_info_obj = None
                current_rate = None
                try:
                    if hasattr(self, 'mt5_connector') and symbol_hint:
                        symbol_info_obj = self.mt5_connector.get_symbol_info(symbol_hint)
                    if basket_pos:
                        current_rate = _safe_float(_v(basket_pos[0], "price_current"), 0.0)
                except Exception:
                    pass

                for p in basket_pos:
                    try:
                        # Profit en USD (directement depuis MT5)
                        profit_usd = _safe_float(_v(p, "profit"), 0.0)
                        total_pnl_usd += profit_usd

                        # Calcul CORRECT du profit en pips (spécifique au symbole)
                        volume = _safe_float(_v(p, "volume"), 0.01)
                        if volume > 0 and symbol_hint:
                            pip_value_per_lot = calculate_pip_value_per_lot(
                                symbol=symbol_hint,
                                symbol_info=symbol_info_obj,
                                current_rate=current_rate
                            )
                            pip_value_total = pip_value_per_lot * volume
                            profit_pips = profit_usd / pip_value_total if pip_value_total > 0 else 0.0
                        else:
                            profit_pips = 0.0
                        total_pnl_pips += profit_pips

                        # Prix de sortie (si disponible)
                        exit_price = _safe_float(_v(p, "price_current"))
                        if exit_price:
                            exit_prices.append(exit_price)
                    except Exception:
                        pass

                # Prix de sortie moyen
                exit_price_avg = sum(exit_prices) / len(exit_prices) if exit_prices else 0.0

                # Déterminer raison sortie
                if forced_sl:
                    exit_reason = "sl_hit"
                elif tickets_closed == len(basket_pos):
                    exit_reason = "manual_close"  # Fermeture complète normale
                else:
                    exit_reason = "partial_close"

                # Log sortie
                self.trade_logger.log_trade_exit(
                    basket_id=basket_id,
                    exit_price=exit_price_avg,
                    pnl_pips=total_pnl_pips,
                    pnl_usd=total_pnl_usd,
                    exit_reason=exit_reason
                )
            except Exception as e:
                self.logger.error(f"❌ [TRADE_LOG] Erreur log sortie basket {basket_id}: {e}", exc_info=True)

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
        self._closing_baskets.discard(basket_id)


# ======================================================================================
# Outils de monitoring  — close sur conditions de panier
# ======================================================================================


def monitor_burst_baskets(
    self,
    config: dict,
    max_loss_pips: float = 15.0,
    trail_trigger: float = 10.0,  # ignoré (no trailing)
    trail_step: float = 5.0,  # ignoré (no trailing)
    **_,
):
    """
    Watchdog de paniers, SANS trailing.
    Ne touche qu'aux positions du bot taguées 'bs_<id>' ET magic==BOT_MAGIC.
    Fermetures auto désactivées par défaut (closure_rules.enabled=false).
    """

    # ---- Conf / garde-fous ----
    burst_cfg = (
        config.get("entry_rules", {}).get("scalping", {}).get("burst_scalping", {})
    ) or {}
    closure = burst_cfg.get("closure_rules", {}) or {}

    enabled = bool(closure.get("enabled", False))  # <- kill switch
    enable_profit_close = bool(closure.get("enable_profit_close", True))
    enable_loss_guard = bool(closure.get("enable_loss_guard", True))
    require_full_count = bool(closure.get("require_full_count_for_profit_close", True))
    require_all_seen = bool(closure.get("require_all_seen_green_once", False))
    all_seen_green_pips = float(closure.get("all_seen_green_pips", 3.0))
    min_green_pnl_pips = float(closure.get("min_green_pnl_pips", 0.0))
    rt_fast_window_ms = int(closure.get("rt_fast_window_ms", 0))  # ← par défaut OFF
    rt_poll_interval_ms = int(closure.get("rt_poll_interval_ms", 120))
    max_loss_pips = float(closure.get("max_loss_pips", float(max_loss_pips)))
    loss_guard_arming_ms = int(closure.get("loss_guard_arming_ms", 3000))
    min_age_ms_for_any_close = int(
        closure.get("min_age_ms_for_any_close", 3000)
    )  # anti-fermeture trop précoce
    target_profit_pips = float(closure.get("target_profit_pips", 15.0))

    logger = getattr(self, "logger", None)

    if not enabled:
        # totalement passif si non activé - pas de log pour éviter flood (100ms polling)
        return

    # 📊 LOG CONFIG AU DÉMARRAGE DU MONITOR
    if logger:
        logger.info("=" * 80)
        logger.info("🎯 [BASKET_MONITOR] Configuration closure_rules chargée:")
        logger.info(f"   • enabled: {enabled}")
        logger.info("")
        logger.info("   📈 PROFIT (fermeture automatique au gain):")
        logger.info(f"      • enable_profit_close: {enable_profit_close}")
        logger.info(f"      • target_profit_pips: {target_profit_pips} pips  ← Fermeture si atteint")
        logger.info(f"      • require_full_count_for_profit_close: {require_full_count}")
        logger.info("")
        logger.info("   🛡️  LOSS GUARD (protection perte maximale):")
        logger.info(f"      • enable_loss_guard: {enable_loss_guard}  ← {'ACTIVÉ ✅' if enable_loss_guard else 'DÉSACTIVÉ'}")
        logger.info(f"      • max_loss_pips: {max_loss_pips} pips  ← Fermeture si perte >= -{max_loss_pips} pips")
        logger.info(f"      • loss_guard_arming_ms: {loss_guard_arming_ms} ms (délai avant activation)")
        logger.info("")
        logger.info("   ⚙️  PARAMÈTRES GÉNÉRAUX:")
        logger.info(f"      • min_age_ms_for_any_close: {min_age_ms_for_any_close} ms")
        logger.info(f"      • rt_fast_window_ms: {rt_fast_window_ms} ms")
        logger.info(f"      • rt_poll_interval_ms: {rt_poll_interval_ms} ms")
        logger.info("=" * 80)

    # ---- Connexion / états ----
    mt5c = getattr(self, "mt5_connector", None)
    if not mt5c:
        return

    # magic du bot (immunité trades manuels)
    try:
        BOT_MAGIC = int(
            getattr(self, "magic", 0)
            or self.config_manager.get("magic_number", 0)
            or self.config_manager.get("defaults.magic_number", 0)
            or 0
        )
    except Exception:
        BOT_MAGIC = 0

    if not hasattr(self, "_basket_seen_green"):
        self._basket_seen_green = {}  # basket_id -> set(ticket)
    if not hasattr(self, "_basket_all_seen"):
        self._basket_all_seen = {}  # basket_id -> bool
    if not hasattr(self, "_basket_first_seen_ts"):
        self._basket_first_seen_ts = {}  # basket_id -> float(ts)

    # ---------- Helpers ----------
    BASKET_TAG_RE = re.compile(
        r"bs_([a-f0-9]{8})"
    )

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
        if si is None:
            return default
        if isinstance(si, dict):
            return si.get(key, default)
        return getattr(si, key, default)

    def _pip_size_for_symbol(sym: str) -> float:
        """EURUSD/GBPUSD(digits=5)=>1 pip=10 pts ; XAUUSD(digits=2)=>1 pip=1 pt"""
        si = _symbol_info(sym)
        point = _safe_float(_gv(si, "point", 0.0001), 0.0001) or 0.0001
        digits = int(_gv(si, "digits", 5) or 5)
        points_per_pip = 10.0 if digits in (3, 5) else 1.0
        return point * points_per_pip

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
        return (
            (ask if t == 0 else bid)
            if (ask is not None and bid is not None)
            else (ask or bid)
        )

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

    def _is_bot_pos(pos) -> bool:
        # only our positions (magic) AND explicit burst tag in comment
        try:
            if int(_v(pos, "magic", 0)) != BOT_MAGIC:
                return False
        except Exception:
            return False
        c = str(_v(pos, "comment", "") or "")
        return bool(BASKET_TAG_RE.search(c))

    def _extract_basket_id(pos) -> Optional[str]:
        # renvoie None si pas notre panier
        if not _is_bot_pos(pos):
            return None
        m = BASKET_TAG_RE.search(str(_v(pos, "comment", "") or ""))
        return m.group(1) if m else None

    def _snapshot_positions():
        try:
            # filtre *ici* : on ne travaille QUE sur nos paniers
            allp = mt5c.get_positions() or []
            return [p for p in allp if _is_bot_pos(p)]
        except Exception:
            return []

    def _group_baskets(positions: List[dict]) -> Dict[str, List[dict]]:
        buckets: Dict[str, List[dict]] = {}
        for p in positions:
            bid = _extract_basket_id(p)
            if not bid:
                continue
            buckets.setdefault(bid, []).append(p)
        return buckets

    def _basket_stats(positions: List[dict]):
        """Retourne (symbol, direction, pip_size, avg_entry, avg_price, pnl_pips) ou None."""
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

    def _expected_count_from(positions: List[dict]) -> Optional[int]:
        # 1) burst_size embarqué si dispo
        exp = 0
        for p in positions:
            bs = _safe_float(_v(p, "burst_size"))
            if bs and int(bs) > 0:
                exp = max(exp, int(bs))
        # 2) fallback conf globale
        if exp == 0:
            try:
                cfg_bs = int(burst_cfg.get("burst_size", 0) or 0)
                if cfg_bs > 0:
                    exp = cfg_bs
            except Exception:
                pass
        return exp if exp > 0 else None

    def _all_green_and_full(positions: List[dict]) -> bool:
        """Vrai si (optionnel) panier plein ET chaque ticket >= min_green_pnl_pips."""
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

    def _update_seen_green(basket_id: str, positions: List[dict], min_seen_pips: float):
        seen = self._basket_seen_green.setdefault(basket_id, set())
        expected = _expected_count_from(positions) or len(positions)
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
                try:
                    seen.add(int(tk))
                except Exception:
                    pass
        self._basket_all_seen[basket_id] = len(seen) >= expected

    def _close_basket(basket_id: str, positions: List[dict]) -> bool:
        """Ferme le panier (PARALLÈLE pour fermeture simultanée instantanée)."""
        # sécurité: n'agir que si tous les pos sont bien *nos* pos
        if not positions or not all(_is_bot_pos(p) for p in positions):
            return False

        # Extraction des tickets
        tickets = [
            int(_v(p, "ticket"))
            for p in positions
            if _v(p, "ticket") is not None
        ]

        if not tickets:
            return False

        # FERMETURE PARALLÈLE (simultanée - méthode prioritaire)
        mt5c_close_parallel = getattr(mt5c, "close_positions_parallel", None)
        if callable(mt5c_close_parallel):
            try:
                result = mt5c_close_parallel(
                    tickets=tickets,
                    reason="basket_close",
                    comment=f"basket_{basket_id}"
                )

                # Vérifier le résultat
                if result.get("failed", 0) == 0:
                    # SUCCÈS TOTAL - toutes les positions fermées
                    logger.info(f"[CLOSE] ✅ Panier '{basket_id}' fermé INSTANTANÉMENT ({result['total']} positions parallèles).")

                    # Cooldown par symbole si demandé
                    try:
                        sym_from_positions = (
                            (str(_v(positions[0], "symbol", "") or "").upper())
                            if positions
                            else ""
                        )
                        if sym_from_positions:
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
                                if not hasattr(self, "_cooldown_until"):
                                    self._cooldown_until = {}
                                self._cooldown_until[sym_from_positions] = (
                                    time.time() + cd
                                )
                                self.logger.info(
                                    f"[COOLDOWN] {sym_from_positions} bloqué {int(cd)}s après fermeture panier '{basket_id}'."
                                )
                    except Exception as _e:
                        self.logger.warning(f"[COOLDOWN] set KO: {_e}")

                    return True
                else:
                    # ÉCHEC PARTIEL - certaines positions n'ont pas fermé
                    logger.warning(
                        f"[CLOSE] Fermeture partielle '{basket_id}': "
                        f"{result['closed']}/{result['total']} fermées, "
                        f"{result['failed']} échecs"
                    )
                    # On retourne True quand même si la majorité est fermée
                    return result['closed'] > 0

            except Exception as e:
                logger.error(f"[CLOSE] close_positions_parallel KO: {e}")
                # Fallback vers méthode série si erreur

        # FALLBACK: Fermeture série (ancienne méthode)
        logger.warning(f"[CLOSE] Fallback série pour '{basket_id}' ({len(tickets)} positions)")

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
                logger.error(f"[CLOSE] ticket #{_v(p,'ticket')} KO: {e}")

        time.sleep(0.05)
        left = [p for p in _snapshot_positions() if _extract_basket_id(p) == basket_id]
        if not left:
            logger.info(f"[CLOSE] Panier '{basket_id}' fermé (fallback série).")
            return True

        logger.warning(
            f"[CLOSE] Fermeture partielle '{basket_id}' ({ok}/{ok+ko})."
        )
        return False

    # =========================
    # INIT: Enregistrer tous les baskets (requis pour loss_guard)
    # =========================
    # ⚠️ CRITIQUE: Initialiser _basket_first_seen_ts AVANT les phases A/B
    #    Sinon, si enable_profit_close=False, la Phase B (loss_guard) skip tous les baskets !
    open_positions_init = _snapshot_positions()
    if open_positions_init:
        baskets_init = _group_baskets(open_positions_init)
        for basket_id in baskets_init.keys():
            if basket_id not in self._basket_first_seen_ts:
                self._basket_first_seen_ts[basket_id] = time.time()
                logger.info(f"🆕 [BASKET_INIT] {basket_id} enregistré pour surveillance (profit + loss_guard)")

    # =========================
    # Phase A — FAST (profit target)
    # =========================
    target_profit = float(closure.get("target_profit_pips", 15.0))

    if enable_profit_close and rt_fast_window_ms > 0 and rt_poll_interval_ms > 0:
        deadline = time.monotonic() + (rt_fast_window_ms / 1000.0)
        loop_count = 0
        last_log_ts = {}  # Timestamp du dernier log par basket (éviter spam)

        while True:
            loop_count += 1
            open_positions = _snapshot_positions()

            if not open_positions:
                break  # Aucun log : tourne en continu

            baskets = _group_baskets(open_positions)
            if not baskets:
                break  # Aucun log : tourne en continu

            any_action = False
            for basket_id, pos in baskets.items():
                # init âge
                if basket_id not in self._basket_first_seen_ts:
                    self._basket_first_seen_ts[basket_id] = time.time()
                    logger.info(f"🆕 [BASKET_DETECTED] {basket_id} | {len(pos)} positions détectées")

                age_ms = int(
                    (time.time() - self._basket_first_seen_ts[basket_id]) * 1000
                )

                # Vérifier si panier plein (optionnel)
                expected = _expected_count_from(pos) if require_full_count else None

                if age_ms < min_age_ms_for_any_close:
                    continue  # Trop jeune : skip silencieux

                if expected is not None and len(pos) < expected:
                    continue  # Incomplet : skip silencieux

                # ✅ CALCUL PNL BASKET MATHÉMATIQUE
                stats = _basket_stats(pos)
                if not stats:
                    continue  # Stats impossibles : skip silencieux
                sym, direction, pip_size, avg_entry, avg_price, pnl_pips = stats

                # 🎯 (05 JAN 2026): Charger config ASSET-SPECIFIC pour target_profit_pips
                # Permet des seuils différents par actif (ex: USDJPY=2.1, EURUSD=15.0, GBPUSD=20.0)
                asset_target_profit = target_profit  # Fallback sur global
                asset_max_loss = max_loss_pips       # Fallback sur global

                try:
                    config_mgr = getattr(self, "config_manager", None)
                    if config_mgr and hasattr(config_mgr, "config_loader"):
                        asset_config = config_mgr.config_loader.load_asset_config(sym) or {}
                        asset_closure = (
                            asset_config.get("entry_rules", {})
                            .get("scalping", {})
                            .get("burst_scalping", {})
                            .get("closure_rules", {})
                        )
                        if asset_closure:
                            asset_target_profit = float(asset_closure.get("target_profit_pips", target_profit))
                            asset_max_loss = float(asset_closure.get("max_loss_pips", max_loss_pips))
                            if logger and asset_target_profit != target_profit:
                                logger.debug(
                                    f"[ASSET_CONFIG][{sym}] target_profit={asset_target_profit}p "
                                    f"(global={target_profit}p) | max_loss={asset_max_loss}p"
                                )
                except Exception as e_asset:
                    if logger:
                        logger.warning(f"[ASSET_CONFIG][{sym}] Erreur chargement: {e_asset}, using global config")

                # 📊 Log PnL toutes les 5 secondes pour suivre l'évolution
                now = time.time()
                last_log = last_log_ts.get(basket_id, 0)
                if now - last_log >= 5.0:
                    last_log_ts[basket_id] = now
                    logger.info(
                        f"📊 [BASKET_MONITOR] {basket_id} | {sym} {direction} | "
                        f"PnL={pnl_pips:+.1f}p (target={asset_target_profit:.1f}p, max_loss={-asset_max_loss:.1f}p) | "
                        f"Entry={avg_entry:.5f} Current={avg_price:.5f} | "
                        f"Age={age_ms/1000:.1f}s | {len(pos)}/{expected or len(pos)} pos"
                    )

                # 🛡️ LOSS GUARD — Vérification PRIORITAIRE (avant profit)
                # Vérifie si perte >= max_loss_pips ET âge >= loss_guard_arming_ms
                # 🎯 (05 JAN 2026): Utilise asset_max_loss (spécifique par actif)
                if enable_loss_guard and asset_max_loss > 0:
                    if age_ms >= loss_guard_arming_ms:
                        if pnl_pips <= -asset_max_loss:
                            logger.error("=" * 80)
                            logger.error(f"🛡️  [LOSS_GUARD_TRIGGERED] {basket_id} ({sym} {direction})")
                            logger.error(f"   📊 PnL actuel: {pnl_pips:.2f} pips")
                            logger.error(f"   🛡️  Seuil max perte: -{asset_max_loss:.2f} pips [{sym}-specific]")
                            logger.error(f"   ❌ Condition remplie: {pnl_pips:.2f} <= -{asset_max_loss:.2f}")
                            logger.error(f"   ⏱️  Âge du basket: {age_ms/1000:.1f}s (arming: {loss_guard_arming_ms/1000:.1f}s)")
                            logger.error(f"   📦 Positions: {len(pos)}/{expected or len(pos)}")
                            logger.error("   → DÉCLENCHEMENT FERMETURE PROTECTION")
                            logger.error("=" * 80)

                            if _close_basket(basket_id, pos):
                                logger.error("=" * 80)
                                logger.error(f"🛡️  [LOSS_GUARD_CLOSED] Basket {basket_id} fermé par protection perte")
                                logger.error(f"   💔 Perte limitée à: {pnl_pips:.2f} pips")
                                logger.error(f"   🛡️  Seuil max: -{asset_max_loss:.2f} pips [{sym}-specific]")
                                logger.error("=" * 80)
                                # Nettoyer le tracking
                                if basket_id in self._basket_first_seen_ts:
                                    del self._basket_first_seen_ts[basket_id]
                                if basket_id in last_log_ts:
                                    del last_log_ts[basket_id]
                                any_action = True
                                continue  # Passer au basket suivant
                            else:
                                logger.error(f"❌ [LOSS_GUARD_FAILED] Échec fermeture basket {basket_id} | Retry au prochain cycle")
                                # Continue quand même pour checker les autres baskets

                # ✅ FERMETURE si PnL >= target_profit_pips
                # 🎯 (05 JAN 2026): Utilise asset_target_profit (spécifique par actif)
                if pnl_pips >= asset_target_profit:
                    logger.info("=" * 80)
                    logger.info(
                        f"🎯 [PROFIT_TARGET_REACHED] {basket_id} ({sym} {direction})"
                    )
                    logger.info(
                        f"   📊 PnL actuel: {pnl_pips:+.2f} pips"
                    )
                    logger.info(
                        f"   🎯 Seuil configuré: {asset_target_profit:.2f} pips [{sym}-specific] ← target_profit_pips"
                    )
                    logger.info(
                        f"   ✅ Condition remplie: {pnl_pips:.2f} >= {asset_target_profit:.2f}"
                    )
                    logger.info(
                        f"   ⏱️  Âge du basket: {age_ms/1000:.1f}s"
                    )
                    logger.info(
                        f"   📦 Positions: {len(pos)}/{expected or len(pos)}"
                    )
                    logger.info("   → DÉCLENCHEMENT FERMETURE IMMÉDIATE")
                    logger.info("=" * 80)

                    if _close_basket(basket_id, pos):
                        logger.info("=" * 80)
                        logger.info(f"✅ [BASKET_CLOSED_SUCCESS] Basket {basket_id} fermé avec succès !")
                        logger.info(f"   💰 Profit sécurisé: +{pnl_pips:.2f} pips")
                        logger.info(f"   🎯 Seuil utilisé: {asset_target_profit:.2f} pips [{sym}-specific]")
                        logger.info(f"   📈 Performance: {((pnl_pips/asset_target_profit)*100):.1f}% du target")
                        logger.info("=" * 80)
                        # Nettoyer le tracking
                        if basket_id in self._basket_first_seen_ts:
                            del self._basket_first_seen_ts[basket_id]
                        if basket_id in last_log_ts:
                            del last_log_ts[basket_id]
                        any_action = True
                        continue
                    else:
                        logger.error("=" * 80)
                        logger.error(f"❌ [BASKET_CLOSE_FAILED] Échec fermeture du basket {basket_id}")
                        logger.error(f"   📊 PnL: {pnl_pips:+.2f} pips | Target: {target_profit:.2f} pips")
                        logger.error(f"   🔄 Retry au prochain cycle...")
                        logger.error("=" * 80)

            if time.monotonic() >= deadline:
                break  # Deadline : sortie silencieuse
            if not any_action:
                time.sleep(rt_poll_interval_ms / 1000.0)
    else:
        logger.warning(f"⛔ [BASKET_MONITOR] Boucle de surveillance NON démarrée: enable_profit_close={enable_profit_close} | rt_fast_window_ms={rt_fast_window_ms} | rt_poll_interval_ms={rt_poll_interval_ms}")

        # =========================
        # Phase B — LOSS GUARD (protection perte maximale)
        # UNIQUEMENT si boucle profit désactivée (sinon loss_guard déjà géré dans la boucle)
        # =========================
        if enable_loss_guard and max_loss_pips > 0:
            logger.info("=" * 80)
            logger.info(f"🛡️  [LOSS_GUARD] Surveillance protection perte activée:")
            logger.info(f"   • max_loss_pips: {max_loss_pips} pips (fermeture si dépassé)")
            logger.info(f"   • loss_guard_arming_ms: {loss_guard_arming_ms} ms (délai activation)")
            logger.info("=" * 80)

            open_positions = _snapshot_positions()
            if open_positions:
                baskets = _group_baskets(open_positions)
                for basket_id, pos in baskets.items():
                    # Vérifier l'âge minimum (éviter fermeture trop rapide)
                    if basket_id not in self._basket_first_seen_ts:
                        continue

                    age_ms = int((time.time() - self._basket_first_seen_ts[basket_id]) * 1000)

                    # Arming delay : attendre avant d'activer la protection
                    if age_ms < loss_guard_arming_ms:
                        continue

                    # Calculer PnL
                    stats = _basket_stats(pos)
                    if not stats:
                        continue

                    sym, direction, pip_size, avg_entry, avg_price, pnl_pips = stats

                    # 🛡️ FERMETURE si perte >= max_loss_pips (valeur NÉGATIVE)
                    if pnl_pips <= -max_loss_pips:
                        logger.error("=" * 80)
                        logger.error(f"🛡️  [LOSS_GUARD_TRIGGERED] {basket_id} ({sym} {direction})")
                        logger.error(f"   📊 PnL actuel: {pnl_pips:.2f} pips")
                        logger.error(f"   🛡️  Seuil max perte: -{max_loss_pips:.2f} pips")
                        logger.error(f"   ❌ Condition remplie: {pnl_pips:.2f} <= -{max_loss_pips:.2f}")
                        logger.error(f"   ⏱️  Âge du basket: {age_ms/1000:.1f}s (arming: {loss_guard_arming_ms/1000:.1f}s)")
                        logger.error(f"   📦 Positions: {len(pos)}")
                        logger.error("   → DÉCLENCHEMENT FERMETURE PROTECTION")
                        logger.error("=" * 80)

                        if _close_basket(basket_id, pos):
                            logger.error("=" * 80)
                            logger.error(f"🛡️  [LOSS_GUARD_CLOSED] Basket {basket_id} fermé par protection perte")
                            logger.error(f"   💔 Perte limitée à: {pnl_pips:.2f} pips (au lieu de -300 pips)")
                            logger.error(f"   🛡️  Seuil max: -{max_loss_pips:.2f} pips")
                            logger.error(f"   💰 Économisé: {(-300 - pnl_pips):.2f} pips vs SL complet")
                            logger.error("=" * 80)
                            # Nettoyer le tracking
                            if basket_id in self._basket_first_seen_ts:
                                del self._basket_first_seen_ts[basket_id]
                        else:
                            logger.error(f"❌ [LOSS_GUARD_FAILED] Échec fermeture basket {basket_id} | Retry au prochain cycle")
        else:
            if logger:
                logger.info(f"⛔ [LOSS_GUARD] Protection perte désactivée (enable_loss_guard={enable_loss_guard})")


# ======================================================================================
# NOTE: Tous les anciens modes 'single-master' et TOUTE logique de trailing ont été supprimés.
# ======================================================================================
