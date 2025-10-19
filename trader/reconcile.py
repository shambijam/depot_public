# trader/reconcile.py - Module de Réconciliation des Trades pour le Bot SNIPER_X
from __future__ import annotations

import re
import time
from typing import Any, List, Dict, Optional
from datetime import datetime, UTC, timezone
from trader.errors import TradeExecutionError



def reconcile_state_with_broker(self) -> None:
    """
    Réconcilie l'état interne des positions avec le broker (MT5).
    - Source de vérité: broker; on fusionne pour préserver les métadonnées internes (risque, trailing, tags).
    - Reconstitue le risque initial si manquant (order_calc_profit), sinon 0.0.
    - Purge les tickets clos; met à jour last_reconciliation_time (UTC).
    - Applique un trailing si la position le demande et si apply_dynamic_trailing(...) est disponible.
    """
    # MT5 local via connecteur (pas d'import module global)
    mt5 = getattr(getattr(self, "mt5_connector", None), "mt5", None) or getattr(self, "mt5", None)
    if mt5 is None:
        self.logger.warning("[RECONCILE] MT5 indisponible → skip.")
        return


    self.logger.info("Réconciliation des positions avec le broker...")

    # -------- Helpers sûrs --------
    def _is_connected_safe() -> bool:
        try:
            attr = getattr(self.mt5_connector, "is_connected", None)
            return bool(attr()) if callable(attr) else bool(attr)
        except Exception:
            return False

    def _reconnect_if_needed():
        try:
            if not _is_connected_safe():
                rec = getattr(
                    self.mt5_connector, "reconnect_if_needed", None
                ) or getattr(self.mt5_connector, "connect", None)
                if callable(rec):
                    rec()
        except Exception:
            pass

    def _pos_to_dict(p) -> dict:
        if isinstance(p, dict):
            return dict(p)
        if hasattr(p, "_asdict"):
            try:
                return dict(p._asdict())
            except Exception:
                pass
        # extraction tolérante par attributs fréquents
        fields = (
            "ticket",
            "symbol",
            "type",
            "volume",
            "price_open",
            "price_current",
            "profit",
            "sl",
            "tp",
            "magic",
            "comment",
            "time",
            "time_msc",
        )
        d = {}
        for f in fields:
            d[f] = getattr(p, f, None)
        return d

    def _get_positions() -> list[dict]:
        # priorité au wrapper
        try:
            pos = self.mt5_connector.get_positions()
            if pos:
                return [_pos_to_dict(x) for x in pos]
        except Exception as e:
            self.logger.warning(f"[RECONCILE] get_positions wrapper KO: {e}")
        # fallback MT5 brut
        try:
            mt5 = getattr(self.mt5_connector, "mt5", None) or getattr(self, "mt5", None)
            if mt5:
                pos = mt5.positions_get()
                return [_pos_to_dict(x) for x in (pos or [])]
        except Exception as e:
            self.logger.error(f"[RECONCILE] mt5.positions_get KO: {e}")
        return []

    def _estimate_initial_risk_usd(pos: dict) -> float:
        """
        Tente d'évaluer le risque initial en $ via order_calc_profit(volume, entry, SL).
        Retourne 0.0 si non calculable.
        """
        try:
            mt5 = getattr(self.mt5_connector, "mt5", None) or getattr(self, "mt5", None)
            if not mt5:
                return 0.0
            vol = float(pos.get("volume") or 0.0)
            entry = float(pos.get("price_open") or pos.get("entry_price") or 0.0)
            sl = float(pos.get("sl") or 0.0)
            typ = int(pos.get("type") if pos.get("type") is not None else 0)
            if vol <= 0 or entry <= 0 or sl <= 0:
                return 0.0
            # type position = 0 BUY / 1 SELL en MT5
            order_type = (
                getattr(mt5, "ORDER_TYPE_BUY", 0)
                if typ == 0
                else getattr(mt5, "ORDER_TYPE_SELL", 1)
            )
            profit = mt5.order_calc_profit(
                order_type, str(pos.get("symbol")), vol, entry, sl
            )
            # Certaines builds renvoient tuple (retcode, value). Tolérance:
            if isinstance(profit, (tuple, list)) and len(profit) >= 2:
                profit = profit[1]
            profit = float(profit)
            if profit == profit:  # not NaN
                return abs(profit) if profit < 0 else float(profit)
            return 0.0
        except Exception:
            return 0.0

    def _safe_dt(ts: float | None) -> str | None:
        try:
            if ts is None:
                return None
            # MT5 positions.time = epoch seconds
            return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()
        except Exception:
            return None

    def _merge_internal(broker_pos: dict, internal_pos: dict | None) -> dict:
        """
        Fusionne en privilégiant:
        - Données volatiles du broker: price_current, profit, sl, tp, comment
        - Métadonnées internes: initial_risk_usd, trailing flags/params, basket tags, etc.
        """
        base = dict(internal_pos or {})
        base.update(
            {  # valeurs fraîches broker
                "ticket": broker_pos.get("ticket"),
                "symbol": broker_pos.get("symbol"),
                "type": broker_pos.get("type"),
                "volume": broker_pos.get("volume"),
                "entry_price": broker_pos.get("price_open"),
                "current_price": broker_pos.get("price_current"),
                "profit": broker_pos.get("profit"),
                "sl": broker_pos.get("sl"),
                "tp": broker_pos.get("tp"),
                "magic": broker_pos.get("magic"),
                "comment": broker_pos.get("comment"),
                "open_time": _safe_dt(broker_pos.get("time")),
            }
        )
        # initial_risk_usd: préserver s'il existe, sinon essayer de le (re)calculer
        if (
            not isinstance(base.get("initial_risk_usd"), (int, float))
            or base["initial_risk_usd"] <= 0
        ):
            base["initial_risk_usd"] = _estimate_initial_risk_usd(base)

        # placeholders/compat
        base.setdefault("use_trailing", base.get("use_trailing", False))
        base.setdefault("trailing_params", base.get("trailing_params", {}))
        base.setdefault("sl_pips", base.get("sl_pips", None))
        base.setdefault("atr_pips", base.get("atr_pips", None))
        return base

    # -------- Ensure connection then pull broker state --------
    if not _is_connected_safe():
        self.logger.warning(
            "MT5 non connecté pour la réconciliation — tentative de reconnexion..."
        )
        _reconnect_if_needed()
    if not _is_connected_safe():
        self.logger.error("MT5 toujours non connecté — abandon de la réconciliation.")
        return

    try:
        broker_positions = _get_positions()
        if broker_positions is None:
            self.logger.error("Impossible d'obtenir les positions broker (None).")
            return

        if not hasattr(self, "_open_positions") or not isinstance(
            self._open_positions, dict
        ):
            self._open_positions = {}

        broker_map = {
            int(p.get("ticket")): p
            for p in broker_positions
            if p.get("ticket") is not None
        }
        reconciled: dict[int, dict] = {}

        # 1) maj/fusion pour chaque position broker
        for ticket, bpos in broker_map.items():
            ipos = self._open_positions.get(ticket)
            if ipos is None:
                self.logger.warning(
                    f"[RECONCILE] Position #{ticket} ({bpos.get('symbol')}) vue broker mais absente en interne → ajout."
                )
            merged = _merge_internal(bpos, ipos)
            reconciled[ticket] = merged

        # 2) suppression des tickets clos (présents en interne mais absents broker)
        closed_tickets = set(self._open_positions.keys()) - set(broker_map.keys())
        for tk in sorted(closed_tickets):
            try:
                sym = self._open_positions[tk].get("symbol")
            except Exception:
                sym = "?"
            self.logger.info(
                f"[RECONCILE] Ticket #{tk} ({sym}) introuvable broker → purge interne."
            )

        # 3) swap atomique + horodatage
        self._open_positions = reconciled
        self._last_reconciliation_time = datetime.now(timezone.utc)
        self.logger.info(
            f"Réconciliation OK — {len(self._open_positions)} position(s) actives synchronisées."
        )

        # 4) Trailing auto si demandé et si la méthode existe
        if hasattr(self, "apply_dynamic_trailing") and callable(
            getattr(self, "apply_dynamic_trailing")
        ):
            for tk, pos in self._open_positions.items():
                try:
                    if pos.get("use_trailing"):
                        params = pos.get("trailing_params", {}) or {}
                        trigger_pips = float(params.get("trigger_pips", 15.0))
                        step_pips = float(params.get("step_pips", 5.0))
                        self.apply_dynamic_trailing(
                            ticket=tk,
                            sl_pips=trigger_pips,
                            atr_pips=step_pips,
                            symbol=pos.get("symbol"),
                        )
                    else:
                        # fallback technique (optionnel mais sûr)
                        sl_pips = float(pos.get("sl_pips", 6.0) or 6.0)
                        atr_pips = float(pos.get("atr_pips", 3.0) or 3.0)
                        self.logger.info(
                            f"[SAFE TRAILING] #{tk} ({pos.get('symbol')}) sans params explicites → trigger={sl_pips}p, step={atr_pips}p"
                        )
                        self.apply_dynamic_trailing(
                            ticket=tk,
                            sl_pips=sl_pips,
                            atr_pips=atr_pips,
                            symbol=pos.get("symbol"),
                        )
                except Exception as e:
                    self.logger.warning(
                        f"[RECONCILE] Trailing non appliqué sur #{tk}: {e}"
                    )
        else:
            self.logger.debug(
                "[RECONCILE] apply_dynamic_trailing indisponible — étape ignorée."
            )

    except Exception as e:
        self.logger.error(f"Échec réconciliation: {e}", exc_info=True)
        try:
            # alerte tolérante si config_manager expose l’envoi d’alertes
            if hasattr(self, "config_manager"):
                send = getattr(self.config_manager, "send_alert", None)
                if callable(send):
                    send("Réconciliation Échec", f"{e}", "telegram_critical")
        except Exception:
            pass


def _update_internal_position_state(self, mt5_result: Any, initial_risk: float) -> None:
    """
    Corrigé: ne lit PAS sl/tp depuis OrderSendResult (non exposés par l'API Python MT5).
    Récupère sl/tp depuis la request quand dispo, sinon via positions_get().
    """
    # --- 1) Récupération sûre depuis la requête d’origine
    try:
        req = getattr(mt5_result, "request", {}) or {}
    except Exception:
        req = {}

    def _f(x):
        try:
            return float(x)
        except Exception:
            return None

    symbol = str(req.get("symbol") or "") or None
    order_type = req.get("type")
    volume = _f(req.get("volume"))
    entry_price = _f(req.get("price") or getattr(mt5_result, "price", None))
    sl_price = _f(req.get("sl") or req.get("stop_loss") or req.get("sl_price"))
    tp_price = _f(req.get("tp") or req.get("take_profit") or req.get("tp_price"))
    comment = req.get("comment")
    magic = req.get("magic")

    # --- 2) Compléments via positions_get() si nécessaire
    pos = None
    try:
        mt5 = getattr(self.mt5_connector, "mt5", None) or getattr(self, "mt5", None)
        if mt5 and symbol:
            poss = list(mt5.positions_get(symbol=symbol) or [])
            if poss:
                # la plus récente
                pos = sorted(poss, key=lambda p: int(getattr(p, "time_update", 0)))[-1]
    except Exception:
        pos = None

    if pos:
        if entry_price is None:
            entry_price = _f(getattr(pos, "price_open", None))
        if sl_price is None:
            sl_price = _f(getattr(pos, "sl", None))
        if tp_price is None:
            tp_price = _f(getattr(pos, "tp", None))

    # --- 3) Construction & stockage
    position_data = {
        "ticket": getattr(mt5_result, "deal", None)
        or getattr(mt5_result, "order", None),
        "symbol": symbol,
        "type": order_type,
        "volume": volume,
        "entry_price": entry_price,
        "sl": sl_price,
        "tp": tp_price,
        "magic": magic,
        "comment": comment,
        "open_time": datetime.now(UTC).isoformat(),
        "initial_risk_usd": float(initial_risk or 0.0),
    }

    ticket_key = position_data["ticket"]
    if ticket_key is not None:
        self._open_positions[ticket_key] = position_data
        self.logger.debug(
            f"[STATE] position enregistrée pour ticket={ticket_key}: {position_data}"
        )
    else:
        self.logger.debug(f"[STATE] position (sans ticket) : {position_data}")

    # Optionnel: tenter une réconciliation silencieuse
    try:
        if hasattr(self, "reconcile_state_with_broker"):
            self.reconcile_state_with_broker()
    except Exception:
        pass


def monitor_pending_orders(self) -> None:
    """
    Surveille les ordres LIMIT Liquidity et annule ceux qui dépassent le timeout_bars.
    À appeler à chaque cycle du pipeline.
    """
    # MT5 local via connecteur (pas d'import module global)
    mt5 = getattr(getattr(self, "mt5_connector", None), "mt5", None) or getattr(self, "mt5", None)
    if mt5 is None:
        self.logger.warning("[RECONCILE] MT5 indisponible → skip.")
        return

    # Garde-fou pour le comptage de barres
    if not hasattr(self, "_bars_since") or not callable(getattr(self, "_bars_since")):
        self.logger.warning("[LIQUIDITY] _bars_since indisponible → skip monitor_pending_orders.")
        return

    to_remove = []

    for order_id, order_data in list(getattr(self, "_open_positions", {}).items()):
        try:
            timeout_bars = int(order_data.get("_meta_timeout_bars", 0) or 0)
            if timeout_bars <= 0:
                continue

            if order_data.get("type") not in (
                getattr(mt5, "ORDER_TYPE_BUY_LIMIT", None),
                getattr(mt5, "ORDER_TYPE_SELL_LIMIT", None),
            ):
                continue

            opened_at = order_data.get("open_time")
            bars_elapsed = self._bars_since(opened_at)

            if bars_elapsed >= timeout_bars:
                self.logger.info(
                    f"[LIQUIDITY] ⏱ Timeout {timeout_bars} barres atteint → annulation de l’ordre LIMIT #{order_id}."
                )
                cancel_request = {
                    "action": getattr(mt5, "TRADE_ACTION_REMOVE", None),
                    "order": order_id,
                    "symbol": order_data.get("symbol"),
                }

                # Utiliser le connecteur s'il expose order_send, sinon fallback MT5 brut
                try:
                    sender = getattr(self.mt5_connector, "order_send", None)
                    result = sender(cancel_request) if callable(sender) else mt5.order_send(cancel_request)
                except Exception as e:
                    self.logger.warning(f"[LIQUIDITY] order_send KO: {e}")
                    result = None

                if not result or getattr(result, "retcode", None) != getattr(mt5, "TRADE_RETCODE_DONE", None):
                    self.logger.warning(
                        f"[LIQUIDITY] ❌ Échec annulation ordre LIMIT #{order_id}, retcode={getattr(result,'retcode','N/A')}."
                    )
                else:
                    self.logger.info(f"[LIQUIDITY] ✅ Ordre LIMIT #{order_id} annulé.")
                    to_remove.append(order_id)

        except Exception as e:
            self.logger.warning(f"[LIQUIDITY] Erreur monitor_pending_orders: {e}")

    # Nettoyage des ordres annulés
    for oid in to_remove:
        try:
            self._open_positions.pop(oid, None)
        except Exception:
            pass


def execute_exit_orders(
    self, exit_decisions: List[Dict[str, Any]], is_dry_run: bool = False
) -> Dict[str, Any]:
    """
    Exécute une liste de décisions de clôture de positions fournies par le ConfigManager.

    Args:
        exit_decisions (List[Dict[str, Any]]): Liste de dictionnaires de décision de sortie.
                                                    Chaque dict doit contenir au moins 'ticket_to_close' et 'reason'.
        is_dry_run (bool): Si True, simule l'exécution sans envoyer d'ordres réels.

    Returns:
        Dict[str, Any]: Résumé de l'exécution des sorties (nombre de succès/échecs).
    """
    self.logger.info(f"Exécution de {len(exit_decisions)} ordre(s) de sortie...")

    success_count = 0
    fail_count = 0

    for decision in exit_decisions:
        ticket = decision.get("ticket_to_close")
        reason = decision.get("reason", "Raison non spécifiée")

        if ticket is None:
            self.logger.error(
                f"Décision de sortie invalide : 'ticket_to_close' manquant. Ignoré."
            )
            fail_count += 1
            continue

        self.logger.info(
            f"Tente de clôturer la position #{ticket} (Raison: {reason})..."
        )

        if is_dry_run:
            self.logger.info(
                f"DRY RUN: Simulation de clôture pour position #{ticket}. (Raison: {reason})."
            )
            success_count += 1
            continue

        try:
            close_result = self.close_position(ticket=ticket)

            if close_result.get("success"):
                self.logger.info(f"Position #{ticket} clôturée avec succès.")
                success_count += 1

                # === LOG SPÉCIAL LIQUIDITY EXIT ===
                if "liquidity" in str(reason).lower():
                    self.logger.info(
                        f"[LIQUIDITY EXIT] ⛔ Ticket={ticket} | Reason={reason} "
                        f"| ClosedPrice={close_result.get('price', 'N/A')} "
                        f"| Volume={close_result.get('volume', 'N/A')}"
                    )

            else:
                self.logger.error(
                    f"Échec de la clôture de la position #{ticket}. Message: {close_result.get('message', 'N/A')}"
                )
                fail_count += 1
        except Exception as e:
            self.logger.error(
                f"Erreur inattendue lors de la tentative de clôture de position #{ticket}: {e}",
                exc_info=True,
            )
            self.config_manager.send_alert(
                "CRITIQUE",
                f"Erreur critique clôture position #{ticket}: {e}",
                alert_type="telegram_critical",
            )
            fail_count += 1

    self.logger.info(
        f"Exécution des ordres de sortie terminée. Succès: {success_count}, Échecs: {fail_count}."
    )
    return {"success_count": success_count, "fail_count": fail_count}


def _send_close_order_with_retries(self, request: dict) -> Optional[Any]:
    """
    Fonction d'aide qui envoie un ordre de clôture avec une logique de retry.
    """
    retcode_actions = self.config_manager.get("mt5_mappings.trade_retcode_actions", {})
    for attempt in range(self.mt5_max_retries):
        self.logger.debug(
            f"Tentative de clôture #{request.get('position')} - Essai {attempt + 1}/{self.mt5_max_retries}..."
        )
        result = self.mt5_connector.send_order(request)

        if not result:
            time.sleep(self.mt5_retry_delay_seconds)
            continue

        action = retcode_actions.get(str(result.retcode), "FAIL")
        if action == "SUCCESS":
            return result
        elif action == "RETRY":
            self.logger.warning(
                f"Rejet temporaire de la clôture #{request.get('position')} (Code: {result.retcode}). Nouvelle tentative..."
            )
            time.sleep(self.mt5_retry_delay_seconds)
        else:  # FAIL
            return result  # Retourne le résultat d'échec pour être traité par la fonction appelante

    return None  # Retourne None si toutes les tentatives échouent


def close_position(self, symbol: str = "ALL", ticket: Optional[int] = None) -> dict:
    """
    Ferme une ou plusieurs positions avec logique de retry (slippage/requote).
    - Si 'ticket' est fourni → ferme uniquement ce ticket.
    - Sinon, ferme toutes les positions du 'symbol', ou toutes si symbol == "ALL".
    - Calcule le PnL exact au prix d'exécution (order_calc_profit), avec fallback.
    - Envoie feedback/audit/alert sans jamais casser le flux si une API manque.
    """
    self.logger.info(f"[CLOSE] Demande: symbol='{symbol}', ticket='{ticket}'")

    # --------- helpers MT5 / mapping sûrs ----------
    mt5 = getattr(getattr(self, "mt5_connector", None), "mt5", None) or getattr(
        self, "mt5", None
    )
    if mt5 is None:
        raise TradeExecutionError("MT5 API indisponible.")

    def _map_const(path: str, fallback: str):
        """
        path: ex "order_types.SELL"
        fallback: ex "ORDER_TYPE_SELL"
        Retourne la constante MT5 (valeur int) si dispo, sinon None.
        """
        try:
            name = self.config_manager.get(f"mt5_mappings.{path}", fallback)
        except Exception:
            name = fallback
        return getattr(mt5, name, getattr(mt5, fallback, None))

    # Position types (broker)
    POS_BUY = _map_const("position_types.BUY", "POSITION_TYPE_BUY")
    POS_SELL = _map_const("position_types.SELL", "POSITION_TYPE_SELL")

    # Order types
    OT_BUY = _map_const("order_types.BUY", "ORDER_TYPE_BUY")
    OT_SELL = _map_const("order_types.SELL", "ORDER_TYPE_SELL")

    # Actions
    ACTION_DEAL = getattr(mt5, "TRADE_ACTION_DEAL", None)
    if ACTION_DEAL is None:
        raise TradeExecutionError("Constante TRADE_ACTION_DEAL introuvable.")

    # Retcodes
    RC_DONE = getattr(mt5, "TRADE_RETCODE_DONE", None)
    RC_DONE_PARTIAL = getattr(mt5, "TRADE_RETCODE_DONE_PARTIAL", None)
    OK_CODES = {c for c in (RC_DONE, RC_DONE_PARTIAL) if c is not None}

    RC_REQUOTE = getattr(mt5, "TRADE_RETCODE_REQUOTE", None)
    RC_PRICE_CHANGED = getattr(mt5, "TRADE_RETCODE_PRICE_CHANGED", None)
    RC_OFF_QUOTES = getattr(mt5, "TRADE_RETCODE_OFF_QUOTES", None)
    RC_REJECT = getattr(mt5, "TRADE_RETCODE_REJECT", None)
    RETRYABLE_CODES = {
        c
        for c in (RC_REQUOTE, RC_PRICE_CHANGED, RC_OFF_QUOTES, RC_REJECT)
        if c is not None
    }

    def _retcode_name(code):
        try:
            for k in dir(mt5):
                if k.startswith("TRADE_RETCODE_") and getattr(mt5, k, None) == code:
                    return k
        except Exception:
            pass
        return str(code)

    # --------- (1) Reconcile état et collect positions broker ----------
    try:
        if hasattr(self, "reconcile_state_with_broker") and callable(
            self.reconcile_state_with_broker
        ):
            self.reconcile_state_with_broker()
    except Exception as e:
        self.logger.warning(f"[CLOSE] Reconcile KO: {e}")

    broker_positions = []
    try:
        if ticket is not None:
            broker_positions = [
                p
                for p in (mt5.positions_get() or [])
                if int(getattr(p, "ticket", -1)) == int(ticket)
            ]
        else:
            broker_positions = (
                list(mt5.positions_get() or [])
                if symbol == "ALL"
                else list(mt5.positions_get(symbol=symbol) or [])
            )
    except Exception as e:
        self.logger.warning(f"[CLOSE] positions_get KO: {e}")
        broker_positions = []

    def _pos_to_dict(p):
        return {
            "ticket": int(getattr(p, "ticket", 0) or 0),
            "symbol": str(getattr(p, "symbol", "")),
            "type": int(getattr(p, "type", -1)),
            "volume": float(getattr(p, "volume", 0.0) or 0.0),
            "entry_price": float(getattr(p, "price_open", 0.0) or 0.0),
            "sl": float(getattr(p, "sl", 0.0) or 0.0),
            "tp": float(getattr(p, "tp", 0.0) or 0.0),
            "profit": float(getattr(p, "profit", 0.0) or 0.0),
            "magic": getattr(p, "magic", None),
            "comment": str(getattr(p, "comment", "") or ""),
        }

    positions_to_close = [_pos_to_dict(p) for p in broker_positions]

    if (
        (not positions_to_close)
        and ticket is not None
        and hasattr(self, "_open_positions")
    ):
        op = self._open_positions.get(ticket)
        if isinstance(op, dict):
            positions_to_close = [op]

    if not positions_to_close:
        msg = f"Aucune position à clôturer (symbol='{symbol}', ticket='{ticket}')."
        self.logger.info(f"[CLOSE] {msg}")
        return {"success": True, "message": msg, "closed_count": 0, "failed_count": 0}

    # --------- (2) Paramètres retry depuis la conf ----------
    tes = {}
    try:
        tes = self.config_manager.get("trade_executor_settings", {}) or {}
    except Exception:
        tes = {}

    base_deviation = int(tes.get("default_slippage", 20) or 20)
    max_attempts = int(tes.get("retry_attempts", 2) or 2)  # en plus de la 1re tentative
    sleep_ms = int(tes.get("retry_sleep_ms", 150) or 150)
    slip_step = int(tes.get("retry_slippage_increment_points", 5) or 5)
    dev_cap = int(tes.get("max_deviation_points_cap", 150) or 150)

    # --------- helpers prix / comment ----------
    def _current_price_for_side(sym: str, side: str) -> float:
        try:
            return float(self.mt5_connector.get_current_price(sym, side) or 0.0)
        except Exception:
            return 0.0

    def _normalize_comment_close(sym: str, raw_comment: str) -> str:
        txt = f"close_{sym}" if not raw_comment else f"close_{raw_comment}"
        txt = txt.replace(" ", "").replace("|", "")
        txt = re.sub(r"[^A-Za-z0-9._-]", "", txt)
        return txt[:31]

    def _digits_for(sym: str) -> int:
        try:
            si = self.mt5_connector.get_symbol_info(sym)
            return int(getattr(si, "digits", 5) or 5)
        except Exception:
            return 5

    def _round_px(sym: str, px: float) -> float:
        d = _digits_for(sym)
        try:
            return round(float(px), d)
        except Exception:
            return px

    # --------- (3) Envoi avec retries ----------
    closed_count, failed_count = 0, 0

    for pos in positions_to_close:
        tkt = int(pos.get("ticket", 0) or 0)
        sym = str(pos.get("symbol", "") or "")
        ptype = int(pos.get("type", -1))
        vol = float(pos.get("volume", 0.0) or 0.0)
        entry = float(pos.get("entry_price", 0.0) or 0.0)
        orig_comment = str(pos.get("comment", "") or "")
        magic = pos.get("magic", None)

        if not tkt or not sym or vol <= 0:
            self.logger.error(
                f"[CLOSE] Position invalide (ticket={tkt}, sym='{sym}', vol={vol}) → skip."
            )
            failed_count += 1
            continue

        # Type d'ordre opposé pour fermer
        if ptype == POS_BUY:
            order_type_close, side = OT_SELL, "SELL"  # pour fermer BUY → prix = Bid
        elif ptype == POS_SELL:
            order_type_close, side = OT_BUY, "BUY"  # pour fermer SELL → prix = Ask
        else:
            self.logger.error(
                f"[CLOSE] Type position inconnu ({ptype}) ticket={tkt} → skip."
            )
            failed_count += 1
            continue

        px = _current_price_for_side(sym, side)
        if px <= 0:
            self.logger.error(
                f"[CLOSE] Prix indisponible pour {sym} (side={side}) → skip."
            )
            failed_count += 1
            try:
                if hasattr(self.config_manager, "send_alert"):
                    self.config_manager.send_alert(
                        "ERREUR",
                        f"Prix clôture indisponible: {sym} #{tkt}",
                        alert_type="telegram_error",
                    )
            except Exception:
                pass
            continue

        work = {
            "action": ACTION_DEAL,
            "position": int(tkt),
            "symbol": sym,
            "volume": float(vol),
            "type": int(order_type_close),
            "price": _round_px(sym, px),
            "deviation": int(base_deviation),
            "magic": magic,
            "comment": _normalize_comment_close(sym, orig_comment),
        }

        attempt = 0
        last_result = None
        while True:
            attempt += 1
            try:
                self.logger.info(f"[CLOSE] Send (try {attempt}) → {work}")
                res = self.mt5_connector.order_send(work)
                last_result = res
                rc = getattr(res, "retcode", None)
                rc_name = _retcode_name(rc)

                if rc in OK_CODES:
                    self.logger.info(f"[CLOSE] OK ticket={tkt} rc={rc} ({rc_name})")
                    break

                if attempt <= (1 + max_attempts) and rc in RETRYABLE_CODES:
                    new_px = _current_price_for_side(sym, side)
                    if new_px > 0:
                        work["price"] = _round_px(sym, new_px)
                    cur_dev = int(
                        work.get("deviation", base_deviation) or base_deviation
                    )
                    work["deviation"] = min(dev_cap, cur_dev + slip_step)
                    self.logger.warning(
                        f"[CLOSE] retry rc={rc_name} → price={work['price']} dev={work['deviation']}"
                    )
                    if sleep_ms > 0:
                        time.sleep(sleep_ms / 1000.0)
                    continue

                self.logger.error(
                    f"[CLOSE] Échec ticket={tkt} rc={rc} ({rc_name}) comment={getattr(res,'comment','')}"
                )
                failed_count += 1
                try:
                    if hasattr(self, "_log_audit_trail"):
                        details = (
                            res._asdict()
                            if hasattr(res, "_asdict")
                            else {
                                "retcode": rc,
                                "comment": getattr(res, "comment", ""),
                            }
                        )
                        self._log_audit_trail(
                            {
                                "event_type": "TRADE_CLOSE_FAILED",
                                "timestamp": datetime.now(UTC).isoformat(),
                                "order_id": tkt,
                                "symbol": sym,
                                "status": "FAILED",
                                "details": details,
                                "position_snapshot": dict(pos),
                            }
                        )
                    if hasattr(self.config_manager, "send_alert"):
                        self.config_manager.send_alert(
                            "CRITIQUE",
                            f"Clôture échec: {sym} #{tkt} | rc={rc_name}",
                            alert_type="telegram_critical",
                        )
                except Exception:
                    pass
                break

            except Exception as e:
                self.logger.error(
                    f"[CLOSE] Exception order_send ticket={tkt}: {e}", exc_info=True
                )
                if attempt <= (1 + max_attempts):
                    if sleep_ms > 0:
                        time.sleep(sleep_ms / 1000.0)
                    continue
                failed_count += 1
                break

        if not last_result or getattr(last_result, "retcode", None) not in OK_CODES:
            continue

        # --- succès : calcul P&L exact au prix d'exécution ---
        close_price = float(
            getattr(last_result, "price", work["price"]) or work["price"]
        )
        try:
            calc_action = OT_BUY if ptype == POS_BUY else OT_SELL
            pnl_usd = float(
                mt5.order_calc_profit(
                    calc_action, sym, float(vol), float(entry), float(close_price)
                )
            )
        except Exception:
            pnl_usd = float(pos.get("profit", 0.0) or 0.0)
            self.logger.warning(
                f"[CLOSE] order_calc_profit KO ticket={tkt} → fallback profit courant {pnl_usd:.2f}"
            )

        # --- nettoyer l'état interne ---
        try:
            if hasattr(self, "_open_positions") and tkt in self._open_positions:
                del self._open_positions[tkt]
        except Exception:
            pass

        closed_count += 1

        # --- audit & feedback ---
        try:
            details = (
                last_result._asdict()
                if hasattr(last_result, "_asdict")
                else {
                    "retcode": getattr(last_result, "retcode", None),
                    "comment": getattr(last_result, "comment", ""),
                    "order": getattr(last_result, "order", None),
                    "deal": getattr(last_result, "deal", None),
                    "price": getattr(last_result, "price", None),
                }
            )
            if hasattr(self, "_log_audit_trail"):
                self._log_audit_trail(
                    {
                        "event_type": "TRADE_CLOSE_SUCCESS",
                        "timestamp": datetime.now(UTC).isoformat(),
                        "order_id": tkt,
                        "symbol": sym,
                        "status": "SUCCESS",
                        "details": details,
                        "pnl_usd": pnl_usd,
                        "position_snapshot": dict(pos),
                    }
                )
            if hasattr(self.config_manager, "feedback_on_trade_result"):
                self.config_manager.feedback_on_trade_result(
                    pos,
                    {
                        "execution_status": "executed",
                        "pnl_usd": pnl_usd,
                        "timestamp": datetime.now(UTC).isoformat(),
                    },
                )
            if hasattr(self.config_manager, "send_alert"):
                self.config_manager.send_alert(
                    message=f"📊 Position clôturée: {sym} | P&L: ${pnl_usd:.2f}",
                    alert_type="telegram_trade_closed",
                )
        except Exception:
            pass

    final_msg = f"Clôture terminée. Succès: {closed_count}, Échecs: {failed_count}."
    self.logger.info(f"[CLOSE] {final_msg}")
    return {
        "success": (failed_count == 0),
        "message": final_msg,
        "closed_count": closed_count,
        "failed_count": failed_count,
    }
