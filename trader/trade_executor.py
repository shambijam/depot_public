# trader/trade_executor.py - Module Central d'Exécution des Trades pour le Bot SNIPER_X
from __future__ import annotations

import re
import time
import logging
import sys
from typing import Any, Optional, Dict, List, TYPE_CHECKING
from datetime import datetime, timedelta, UTC, timezone
from pathlib import Path
from core.utils import enforce_no_tp_for_burst, CustomJSONEncoder
from trader.sizing import _calculate_risk_based_volume  


if TYPE_CHECKING:
    from core.config_manager import ConfigManager
    from mt5_connector import MT5Connector

# --- Mixins/colle de méthodes en provenance des briques ---
from .settings import _load_settings
from .order_builder import prepare_order
from .sizing import _calculate_risk_based_volume
from .sltp import _calculate_sl_tp_prices, _normalize_stops
from .burst import monitor_burst_baskets
from .reconcile import reconcile_state_with_broker, _update_internal_position_state
from .audit import _log_audit_trail, _mark_trade_sent, generate_report
from .validators import (
    manual_override_if_needed,
    approve_pending_order,
    pre_trade_checks,
)
from .trailing import apply_dynamic_trailing, _modify_sl, _bars_since
from .burst import (
    _attach_burst_metadata,
    execute_burst_scalping_order,
    execute_burst_single_master,
)


# Import MT5 tolérant (utile pour constantes/retcodes si tu en appelles ici)
try:
    import MetaTrader5 as mt5
except Exception:
    mt5 = None


# Alias UTC
UTC = timezone.utc


# --- Définitions d'Exceptions Personnalisées ---
class TradeExecutionError(Exception):
    """Exception levée pour les erreurs critiques durant l'exécution d'un trade."""

    pass


class InvalidDecisionPackageError(ValueError):
    """Exception levée lorsqu'un package de décision est malformé ou incomplet."""

    pass


class TradeExecutor:
    """
    Exécute les décisions de trading de manière robuste et sécurisée.

    Ce module est le bras armé du bot. Il valide chaque décision, effectue des
    vérifications pré-trade critiques, prépare et envoie les ordres au broker,
    et maintient un état interne des positions ouvertes pour une gestion avancée.
    """

    def __init__(
        self,
        config_manager: ConfigManager,
        mt5_connector: MT5Connector,
        mode: str = "demo",
    ):
        """
        Initialise le TradeExecutor et son état interne.

        Args:
            config_manager (ConfigManager): L'instance du gestionnaire de configuration.
            mt5_connector (MT5Connector): L'instance du connecteur de broker.
            mode (str): Mode d'exécution ("demo" ou "live").
        """
        self.logger = logging.getLogger(__name__)
        self.config_manager = config_manager
        self.mt5_connector = mt5_connector
        self.mode = mode.lower()
        self.mt5 = getattr(self.mt5_connector, "mt5", None)
        self._last_trade_times = {}  # {symbol: datetime}

        # CORRECTION : Suppression de self.mt5 = mt5. Il est plus propre et cohérent que
        # toute interaction avec la librairie MetaTrader5 passe par le mt5_connector.
        # Cela renforce la séparation des responsabilités entre les modules.

        # GESTION D'ÉTAT INTERNE (Excellente pratique, inchangée)
        self._open_positions: Dict[int, Dict[str, Any]] = {}
        self._pending_orders: Dict[str, Dict[str, Any]] = {}
        self._last_reconciliation_time: Optional[datetime] = (
            None  # Initialisé à None pour plus de clarté
        )

        self._burst = {
            "baskets": {},  # basket_id -> meta
            "by_order": {},  # order_id  -> basket_id
        }
        # Cooldown post-exit (par symbole broker)
        self._cooldown_until: dict[str, float] = {}

        # Chargement dynamique des paramètres
        self._load_settings()

        self.logger.info(f"TradeExecutor initialisé en mode {self.mode.upper()}.")

    # --- Pass-throughs vers validators & order_builder (imports locaux pour éviter les cycles) ---

    def pre_trade_checks(self, trade_decision, active_config, market_context):
        from trader.validators import pre_trade_checks as _pre_checks

        return _pre_checks(self, trade_decision, active_config, market_context)

    def _map_symbol_for_broker(self, raw_symbol: str, market_context: dict) -> str:
        from trader.order_builder import _map_symbol_for_broker as _map_fn

        return _map_fn(self, raw_symbol, market_context)

    def prepare_order(self, decision_package: dict) -> dict:
        from trader.order_builder import prepare_order as _prepare_order

        return _prepare_order(self, decision_package)

    def _build_mt5_request(
        self,
        trade_decision: dict,
        config: dict,
        volume: float,
        entry_price_market: float,
        sl_price: float,
        tp_price: float,
        symbol_info,
        trigger_price: float | None = None,
        order_type_str: str = "MARKET",
    ) -> dict:
        from trader.order_builder import _build_mt5_request as _build_req

        return _build_req(
            self,
            trade_decision,
            config,
            volume,
            entry_price_market,
            sl_price,
            tp_price,
            symbol_info,
            trigger_price,
            order_type_str,
        )

        # La réconciliation initiale est gérée par main.py, ce qui est la bonne approche.

    def get_positions(self, symbol: str | None = None):
        """
        Retourne les positions ouvertes (liste de dicts). Filtre par symbole si fourni.
        Résout MT5 via le connecteur en priorité, fallback sur l'import module-level.
        """
        mt5_mod = getattr(getattr(self, "mt5_connector", None), "mt5", None) or mt5
        if mt5_mod is None:
            self.logger.warning("[MT5C] get_positions() : MT5 indisponible.")
            return []

        try:
            positions = (
                mt5_mod.positions_get(symbol=symbol)
                if symbol
                else mt5_mod.positions_get()
            )
            out = []
            for p in positions or []:
                sym = getattr(p, "symbol", "") or ""
                info = mt5_mod.symbol_info(sym) if sym else None
                point = (
                    float(getattr(info, "point", 0.0001) or 0.0001) if info else 0.0001
                )
                out.append(
                    {
                        "ticket": getattr(p, "ticket", None),
                        "symbol": sym or None,
                        "magic": getattr(p, "magic", None),
                        "comment": getattr(p, "comment", "") or "",
                        "type": getattr(
                            p, "type", None
                        ),  # POSITION_TYPE: 0=BUY, 1=SELL
                        "volume": float(getattr(p, "volume", 0) or 0),
                        "price_open": float(getattr(p, "price_open", 0) or 0),
                        "sl": float(getattr(p, "sl", 0) or 0),
                        "tp": float(getattr(p, "tp", 0) or 0),
                        "point": point,
                    }
                )
            return out
        except Exception as e:
            self.logger.warning(f"[MT5C] get_positions() a échoué: {e}")
            return []

    # Compatibilité arrière (ancien appel)
    def get_open_positions(self, symbol: str | None = None):
        self.logger.warning(
            "get_open_positions() est obsolète. Utilise get_positions()."
        )
        return self.get_positions(symbol)

    def execute_order(self, request: dict, is_dry_run: bool = False) -> dict:
        """
        Envoie une requête d'ordre MT5 via MT5Connector et retourne un résumé unifié.
        Améliorations :
        - Retries slippage/requote (PRICE_CHANGED, REQUOTE, OFF_QUOTES) :
            * refresh du prix marché
            * augmentation progressive de la déviation (capée)
        - TP neutralisé pour burst_scalping (trailing-only)
        - Normalisation 'comment' court & ASCII
        - SL différé si stops/freeze ne permettent pas un SL immédiat (puis attach post-fill)
        - Feedback propre (placed/filled/failed) via feedback_pipeline + _feedback_safe
        """

        # ---------- Connexion ----------
        try:
            connected = getattr(self.mt5_connector, "is_connected", False)
            if callable(connected):
                connected = connected()
            if not connected and hasattr(self.mt5_connector, "connect"):
                self.mt5_connector.connect()
                connected = (
                    self.mt5_connector.is_connected()
                    if callable(getattr(self.mt5_connector, "is_connected", None))
                    else bool(getattr(self.mt5_connector, "is_connected", False))
                )
        except Exception:
            connected = False
        if not connected:
            raise TradeExecutionError("MT5 non connecté: envoi interdit.")

        # ---------- Pré-validations ----------
        if not isinstance(request, dict):
            raise TradeExecutionError("Requête MT5 invalide (type non-dict).")

        symbol = request.get("symbol")
        if not symbol:
            raise TradeExecutionError("Requête MT5 invalide: 'symbol' manquant.")
        try:
            vol = float(request.get("volume", 0))
        except Exception:
            vol = 0.0
        if vol <= 0:
            raise TradeExecutionError("Requête MT5 invalide: 'volume' doit être > 0.")

        # Limite (défensive) nb positions sur ce symbole
        try:
            open_positions = self.mt5_connector.get_positions(symbol=symbol) or []
            if len(open_positions) >= 200:
                msg = f"[EXECUTOR] ❌ Trop de positions ouvertes pour {symbol} ({len(open_positions)})."
                self.logger.error(msg)
                raise TradeExecutionError(msg)
        except Exception as e:
            self.logger.warning(f"[EXECUTOR] Check nb positions KO ({symbol}): {e}")

        # ---------- MT5 & constantes ----------
        mt5c = self.mt5_connector
        mt5 = getattr(mt5c, "mt5", None) or getattr(self, "mt5", None)
        if mt5 is None:
            try:
                import MetaTrader5 as _mt5  # type: ignore

                mt5 = _mt5
            except Exception:
                raise TradeExecutionError("MT5 API indisponible sur le connecteur.")

        def _const(group: str, key: str, default_name: str):
            try:
                mapping = (
                    self.mt5_mappings.get(group, {})
                    if isinstance(getattr(self, "mt5_mappings", None), dict)
                    else {}
                ) or {}
                name = mapping.get(key, default_name)
                return getattr(mt5, name)
            except Exception:
                return getattr(mt5, default_name, None)

        # Codes succès
        RET_DONE = _const("trade_retcodes", "DONE", "TRADE_RETCODE_DONE")
        RET_PLACED = _const("trade_retcodes", "PLACED", "TRADE_RETCODE_PLACED")
        RET_DONE_PARTIAL = _const(
            "trade_retcodes", "DONE_PARTIAL", "TRADE_RETCODE_DONE_PARTIAL"
        )
        OK_CODES = {RET_DONE, RET_PLACED, RET_DONE_PARTIAL}

        # Codes connexion
        RET_NO_CONN = getattr(mt5, "TRADE_RETCODE_NO_CONNECTION", None)
        RET_CONN = getattr(mt5, "TRADE_RETCODE_CONNECTION", None)
        RET_TIMEOUT = getattr(mt5, "TRADE_RETCODE_TIMEOUT", None)
        CONNECTION_ERROR_CODES = {
            c for c in (RET_NO_CONN, RET_CONN, RET_TIMEOUT) if c is not None
        }

        # Codes "retryables" (slippage/requote/off quotes/price changed)
        RET_REQUOTE = getattr(mt5, "TRADE_RETCODE_REQUOTE", None)
        RET_PRICE_CHANGED = getattr(mt5, "TRADE_RETCODE_PRICE_CHANGED", None)
        RET_OFF_QUOTES = getattr(mt5, "TRADE_RETCODE_OFF_QUOTES", None)
        RET_REJECT = getattr(mt5, "TRADE_RETCODE_REJECT", None)  # parfois réseau
        RETRYABLE_CODES = {
            c
            for c in (RET_REQUOTE, RET_PRICE_CHANGED, RET_OFF_QUOTES, RET_REJECT)
            if c is not None
        }

        ORDER_TYPE_BUY = _const("order_types", "BUY", "ORDER_TYPE_BUY")
        ORDER_TYPE_SELL = _const("order_types", "SELL", "ORDER_TYPE_SELL")
        ORDER_TYPE_SELL_LIMIT = _const(
            "order_types", "SELL_LIMIT", "ORDER_TYPE_SELL_LIMIT"
        )
        ORDER_TYPE_SELL_STOP = _const(
            "order_types", "SELL_STOP", "ORDER_TYPE_SELL_STOP"
        )

        def _retcode_name(code: int | None) -> str | None:
            if code is None:
                return None
            try:
                for k in dir(mt5):
                    if k.startswith("TRADE_RETCODE_") and getattr(mt5, k, None) == code:
                        return k
            except Exception:
                pass
            return None

        def _side_from_req(req: dict) -> str:
            t = req.get("type")
            try:
                if t == ORDER_TYPE_BUY:
                    return "BUY"
                if t == ORDER_TYPE_SELL:
                    return "SELL"
            except Exception:
                pass
            # fallback conservateur (sera corrigé par le prix rafraîchi)
            return "BUY"

        # ---------- Burst : neutraliser TP ----------
        if (
            request.get("is_burst_trade", False)
            or str(request.get("rule_name", "")).lower() == "burst_scalping"
        ):
            request["tp"] = 0.0
            # on laisse le commentaire se faire normaliser juste après
            self.logger.info(
                f"[EXECUTOR] 🎯 Burst trade → {symbol} (TP neutralisé, trailing attendu)."
            )

        # ---------- Commentaire MT5 sûr ----------
        def _normalize_mt5_comment(req: dict) -> str:
            import re

            raw = str(req.get("comment") or "")
            bid = str(req.get("basket_id") or "")
            if not bid and raw:
                m = re.search(r"basket=([A-Za-z0-9_]+)", raw)
                if m:
                    bid = m.group(1)
            if bid:
                raw = bid
            elif not raw:
                sym = str(req.get("symbol", "")).upper()
                raw = (
                    f"burst_{sym}"
                    if (
                        req.get("is_burst_trade")
                        or str(req.get("rule_name", "")).lower() == "burst_scalping"
                    )
                    else (sym or "order")
                )
            raw = raw.replace("|", "").replace(" ", "")
            raw = re.sub(r"[^A-Za-z0-9._-]", "", raw)
            return raw[:31]

        request["comment"] = _normalize_mt5_comment(request)
        if request.get("tp", None) is None:
            request["tp"] = 0.0  # MT5: 0.0 = pas de TP

        # ---------- Normalisation SL/TP & SL différé si trop proche ----------
        side = _side_from_req(request)
        info = None
        try:
            info = (
                self.mt5_connector.mt5.symbol_info(symbol)
                if getattr(self.mt5_connector, "mt5", None)
                else None
            )
            if not info and getattr(self, "mt5", None):
                info = self.mt5.symbol_info(symbol)
        except Exception:
            info = None

        point = getattr(info, "point", None) or 0.0
        digits = int(getattr(info, "digits", None) or 0)
        tick_size = getattr(info, "trade_tick_size", None) or point or 0.0
        stops_level_pts = int(getattr(info, "trade_stops_level", 0) or 0)
        freeze_level_pts = int(getattr(info, "trade_freeze_level", 0) or 0)
        spread_pts = int(getattr(info, "spread", 0) or 0)
        one_tick_pts = int(round((tick_size or point) / (point or 1.0))) or 1
        min_buf_pts = max(stops_level_pts, freeze_level_pts, spread_pts) + one_tick_pts

        def _get_market_price(sym: str, s: str) -> float:
            px = request.get("price")
            if px:
                return float(px)
            m = None
            try:
                if getattr(self.mt5_connector, "mt5", None):
                    m = self.mt5_connector.mt5.symbol_info_tick(sym)
                if not m and getattr(self, "mt5", None):
                    m = self.mt5.symbol_info_tick(sym)
            except Exception:
                m = None
            if not m:
                return 0.0
            bid = getattr(m, "bid", None)
            ask = getattr(m, "ask", None)
            if s == "BUY" and ask is not None:
                return float(ask)
            if s == "SELL" and bid is not None:
                return float(bid)
            return float(ask or bid or 0.0)

        def _round_to_tick(px: float) -> float:
            if not tick_size or tick_size <= 0:
                return round(float(px), digits)
            steps = round(float(px) / tick_size)
            return round(steps * tick_size, digits)

        def _ensure_min_buffer(sl_target: float, px_ref: float, s: str):
            if px_ref is None or point <= 0:
                return True, sl_target
            dist_pts = abs(px_ref - sl_target) / point
            need_defer = dist_pts < float(min_buf_pts)
            if not need_defer:
                if s == "BUY" and sl_target >= px_ref:
                    need_defer = True
                if s == "SELL" and sl_target <= px_ref:
                    need_defer = True
            return (not need_defer), _round_to_tick(sl_target)

        price = _get_market_price(symbol, side)
        sl = request.get("sl")

        if sl is not None and price and point:
            sl = float(sl)
            if side == "BUY" and sl >= price:
                sl = price - (tick_size or point)
            if side == "SELL" and sl <= price:
                sl = price + (tick_size or point)
            ok_now, sl_ok = _ensure_min_buffer(sl, price, side)
            if ok_now:
                request["sl"] = _round_to_tick(sl_ok)
            else:
                request["_deferred_sl"] = _round_to_tick(sl_ok)
                request["sl"] = 0.0  # MT5: pas de SL à l'envoi

        # ---------- Params retry depuis la conf ----------
        te_settings = self.config_manager.get("trade_executor_settings", {}) or {}
        max_attempts = int(
            te_settings.get("retry_attempts", 2) or 2
        )  # nb de retries (en plus de la 1re tentative)
        sleep_ms = int(te_settings.get("retry_sleep_ms", 150) or 150)
        slip_step = int(te_settings.get("retry_slippage_increment_points", 5) or 5)
        dev_cap = int(te_settings.get("max_deviation_points_cap", 150) or 150)

        # digits pour arrondi du prix au retry
        try:
            si = self.mt5_connector.get_symbol_info(symbol)
            price_digits = int(getattr(si, "digits", digits) or digits)
        except Exception:
            price_digits = digits or 5

        # dry-run
        if is_dry_run:
            self.logger.info(f"[DRY-RUN] MT5 request (non envoyée): {request}")
            try:
                fb = self.feedback_pipeline(
                    order_id=str(request.get("client_order_id", "N/A")),
                    status="placed",
                    reason="dry_run",
                )
                self._feedback_safe(request, fb)
            except Exception:
                pass
            return {
                "status": "placed",
                "reason": "dry_run",
                "retcode": None,
                "retcode_name": None,
                "ticket": None,
                "symbol": symbol,
                "price": request.get("price"),
                "volume": request.get("volume"),
            }

        # ---------- Envoi + retries ----------
        attempt = 0
        last_result = None
        work = dict(request)
        base_dev = int(work.get("deviation", 0) or 0)

        while True:
            attempt += 1
            try:
                self.logger.info(f"[EXEC] Send MT5 (try {attempt}) → {work}")
                result = mt5c.order_send(work)
                last_result = result
                rc = getattr(result, "retcode", None)
                rc_name = _retcode_name(rc)
                self.logger.info(f"[EXEC] retcode={rc} ({rc_name})")

                # Succès immédiat : break
                if rc in OK_CODES:
                    break

                # Retentable ?
                if attempt <= (1 + max_attempts) and rc in RETRYABLE_CODES:
                    # refresh prix + bump déviation
                    new_px = _get_market_price(symbol, side)
                    if new_px:
                        work["price"] = round(float(new_px), price_digits)
                    cur_dev = int(work.get("deviation", base_dev) or 0)
                    cur_dev = min(dev_cap, cur_dev + slip_step)
                    work["deviation"] = cur_dev
                    self.logger.warning(
                        f"[EXEC] Retry ({attempt-1}/{max_attempts}) rc={rc_name} → price={work.get('price')} dev={cur_dev}"
                    )
                    if sleep_ms > 0:
                        time.sleep(sleep_ms / 1000.0)
                    continue

                # Non retentable ou fin des essais → échec
                reason = f"retcode={rc} ({rc_name or 'UNKNOWN'})"
                try:
                    last_err = mt5.last_error()
                    if last_err:
                        if isinstance(last_err, (tuple, list)):
                            reason += f" | last_error={' | '.join(map(str,last_err))}"
                        else:
                            reason += f" | last_error={last_err}"
                except Exception:
                    pass

                self.logger.error(f"[EXEC] Échec définitif: {reason}")
                try:
                    fb = self.feedback_pipeline(
                        order_id=str(work.get("client_order_id", "N/A")),
                        status="failed",
                        reason=reason,
                    )
                    self._feedback_safe(work, fb)
                except Exception:
                    pass
                return {
                    "status": "failed",
                    "reason": reason,
                    "retcode": rc,
                    "retcode_name": rc_name,
                    "ticket": None,
                    "symbol": symbol,
                    "price": float(work.get("price") or 0.0),
                    "volume": float(work.get("volume") or 0.0),
                }

            except Exception as e:
                self.logger.error(f"[EXEC] Exception order_send: {e}", exc_info=True)
                if attempt <= (1 + max_attempts):
                    if sleep_ms > 0:
                        time.sleep(sleep_ms / 1000.0)
                    continue
                try:
                    fb = self.feedback_pipeline(
                        order_id=str(work.get("client_order_id", "N/A")),
                        status="failed",
                        reason=str(e),
                    )
                    self._feedback_safe(work, fb)
                except Exception:
                    pass
                return {
                    "status": "failed",
                    "reason": str(e),
                    "retcode": None,
                    "retcode_name": None,
                }

        # ---------- Succès : normaliser le résultat ----------
        rc = getattr(last_result, "retcode", None)
        rc_name = _retcode_name(rc)
        order_id = getattr(last_result, "order", None)
        deal_id = getattr(last_result, "deal", None)
        res_px = getattr(last_result, "price", None)
        res_vol = getattr(last_result, "volume", None)
        comment = getattr(last_result, "comment", "")
        req_id = getattr(last_result, "request_id", None)

        status = (
            "placed"
            if rc == RET_PLACED
            else ("partially_filled" if rc == RET_DONE_PARTIAL else "filled")
        )
        execution_summary = {
            "status": status,
            "retcode": rc,
            "retcode_name": rc_name,
            "order": order_id,
            "deal": deal_id,
            "symbol": symbol,
            "action": side,
            "price": res_px if res_px else work.get("price"),
            "volume": res_vol if res_vol else work.get("volume"),
            "sl": work.get("sl"),
            "tp": work.get("tp"),
            "comment": comment,
            "request_id": req_id,
        }

        # Stockage pending dans _open_positions (pour monitor/timeout)
        if rc == RET_PLACED and hasattr(self, "_open_positions"):
            try:
                oid = int(order_id or 0)
                if oid:
                    self._open_positions[oid] = {
                        "symbol": symbol,
                        "type": work.get("type"),
                        "volume": work.get("volume"),
                        "price": work.get("price"),
                        "sl": work.get("sl"),
                        "tp": work.get("tp", 0.0),
                        "open_time": datetime.now(UTC).isoformat(),
                        "_meta_timeout_bars": int(
                            work.get("_meta_timeout_bars", 0) or 0
                        ),
                    }
            except Exception as _e:
                self.logger.warning(f"[EXEC] Stockage pending KO: {_e}")

        # Update état interne pour les deals
        if rc in {RET_DONE, RET_DONE_PARTIAL}:
            try:
                self._update_internal_position_state(last_result, initial_risk=0.0)
            except Exception as _e:
                self.logger.warning(f"[EXEC] Update état interne KO: {_e}")

        # SL différé post-fill → attacher
        try:
            if work.get("_deferred_sl") is not None and status in (
                "filled",
                "partially_filled",
            ):
                positions = None
                if getattr(self, "mt5", None):
                    positions = self.mt5.positions_get(symbol=symbol)
                if not positions and getattr(self.mt5_connector, "mt5", None):
                    positions = self.mt5_connector.mt5.positions_get(symbol=symbol)
                pos = None
                if positions:
                    try:
                        pos = sorted(
                            positions, key=lambda p: getattr(p, "time_update", 0)
                        )[-1]
                    except Exception:
                        pos = positions[-1]
                if pos:
                    # récupère à nouveau les contraintes stops/freeze
                    info2 = info or (
                        self.mt5_connector.mt5.symbol_info(symbol)
                        if getattr(self.mt5_connector, "mt5", None)
                        else None
                    )
                    point2 = getattr(info2, "point", None) or 0.0
                    digits2 = int(getattr(info2, "digits", None) or digits or 0)
                    tick2 = getattr(info2, "trade_tick_size", None) or point2 or 0.0
                    spread2 = int(getattr(info2, "spread", 0) or 0)
                    freeze2 = int(getattr(info2, "trade_freeze_level", 0) or 0)
                    stops2 = int(getattr(info2, "trade_stops_level", 0) or 0)
                    one_tick2 = int(round((tick2 or point2) / (point2 or 1.0))) or 1
                    min_buf2 = max(spread2, freeze2, stops2) + one_tick2

                    def _round2(px):
                        if not tick2 or tick2 <= 0:
                            return round(float(px), digits2)
                        steps = round(float(px) / tick2)
                        return round(steps * tick2, digits2)

                    def _mkt(sym, s):
                        m = None
                        if getattr(self.mt5_connector, "mt5", None):
                            m = self.mt5_connector.mt5.symbol_info_tick(sym)
                        if not m and getattr(self, "mt5", None):
                            m = self.mt5.symbol_info_tick(sym)
                        if not m:
                            return None
                        bid = getattr(m, "bid", None)
                        ask = getattr(m, "ask", None)
                        if s == "BUY" and ask is not None:
                            return float(ask)
                        if s == "SELL" and bid is not None:
                            return float(bid)
                        return float(ask or bid or 0.0)

                    px_now = _mkt(symbol, side)
                    sl_target = float(work["_deferred_sl"])
                    if px_now and point2:
                        if side == "BUY":
                            max_sl = px_now - (min_buf2 * point2)
                            sl_target = min(sl_target, max_sl)
                        else:
                            min_sl = px_now + (min_buf2 * point2)
                            sl_target = max(sl_target, min_sl)
                    sl_target = _round2(sl_target)

                    ticket = getattr(pos, "ticket", None)
                    if ticket is None and isinstance(pos, dict):
                        ticket = pos.get("ticket")
                    if ticket is not None:
                        modified = False
                        for fn_name in (
                            "modify_position_sltp",  # <— ajout essentiel
                            "position_modify",
                            "modify_position",
                            "set_sl_tp",
                        ):

                            fn = getattr(self.mt5_connector, fn_name, None)
                            if callable(fn):
                                try:
                                    fn(
                                        ticket=int(ticket),
                                        sl=sl_target,
                                        tp=execution_summary.get("tp", 0.0),
                                    )
                                    modified = True
                                    self.logger.info(
                                        f"[EXECUTOR] SL attaché post-fill (ticket={ticket}, sl={sl_target})."
                                    )
                                    break
                                except Exception as e:
                                    self.logger.warning(
                                        f"[EXECUTOR] {fn_name} a échoué (ticket={ticket}): {e}"
                                    )
                        if not modified:
                            self.logger.warning(
                                "[EXECUTOR] Impossible d’attacher le SL post-fill (aucune méthode disponible)."
                            )
        except Exception as e:
            self.logger.warning(f"[EXECUTOR] Post-fill SL attach ignoré: {e}")

        # ---------- Feedback & audit ----------
        try:
            fb_status = "placed" if status == "placed" else "filled"
            fb = self.feedback_pipeline(
                order_id=str(order_id or deal_id or work.get("client_order_id", "N/A")),
                status=fb_status,
                reason=rc_name or fb_status.upper(),
            )
            self._feedback_safe(work, fb)
        except Exception:
            pass

        if hasattr(self, "audit_logger"):
            try:
                self.audit_logger.log_trade_execution(
                    {
                        "status": execution_summary["status"],
                        "symbol": symbol,
                        "action": side,
                        "order_type": work.get("type"),
                        "volume": execution_summary["volume"],
                        "entry_price": execution_summary["price"],
                        "sl_price": execution_summary["sl"],
                        "tp_price": execution_summary["tp"],
                        "strategy_type": work.get("strategy_type"),
                        "rule_name": work.get("rule_name"),
                        "magic_number": work.get("magic"),
                        "ticket": execution_summary.get("order")
                        or execution_summary.get("deal"),
                        "request": work,
                        "response": {
                            "retcode": rc,
                            "retcode_str": rc_name,
                            "comment": comment,
                            "order": order_id,
                            "deal": deal_id,
                            "is_burst_trade": work.get("is_burst_trade", False),
                        },
                    },
                    getattr(self, "execution_context", {}) or {},
                )
            except Exception:
                pass

        # log stratégie
        strat_type = (work.get("strategy_type") or "").lower()
        if strat_type == "liquidity":
            self.logger.info(
                f"[LIQUIDITY TRADE] ✅ {symbol} | action={side} | entry={execution_summary['price']} "
                f"| sl={execution_summary['sl']} | tp={execution_summary['tp']} | rr={work.get('meta_rr_projected','N/A')}"
            )
        elif strat_type in ("scalping", "burst_scalping"):
            self.logger.info(
                f"[SCALPING TRADE] ⚡ {symbol} | action={side} | entry={execution_summary['price']} "
                f"| sl={execution_summary['sl']} | tp={execution_summary['tp']} | rr={work.get('meta_rr_projected','N/A')}"
            )

        # marquer le throttle "trade envoyé"
        try:
            self._mark_trade_sent(symbol, time.time())
        except Exception:
            pass

        return execution_summary


def run_trade_execution_pipeline(
    trade_executor, decision_package: dict, is_dry_run: bool = False
) -> dict:
    """
    Pont unique entre la décision (DecisionPipeline) et l'exécution (TradeExecutor).
    Lit dans decision_package['final_decision'].
    Normalise/valide avant prepare_order.

    Corrections majeures (sizing clean):
    - ❌ Ne propage plus le 'volume' provenant de la décision amont.
    - ❌ Suppression du garde-fou 'fat-finger' sur un volume fourni par l'amont (inutile car ignoré).
    - ✅ En mode BURST, calcul du lot via la fonction unique `_calculate_risk_based_volume`
      (avec SL cohérent) avant d'appeler `execute_burst_single_master`.
    - ✅ En mode BURST + LIMIT_FOK, on fournit `burst_volume_each` = lot par ticket
      issu du risk% (scope BASKET traité par la fonction de sizing).
    - ✅ En mode STANDARD, inchangé: sizing fait dans `prepare_order`.
    """
    import logging

    logger = logging.getLogger(__name__)

    class TradeExecutionError(Exception):
        pass

    # ----------- Helpers locaux -----------
    def _first_non_empty(*vals):
        for v in vals:
            if isinstance(v, str) and v.strip():
                return v.strip()
        return None

    def _normalize_action(a: str) -> str:
        a = (a or "").strip().upper()
        mapping = {
            "BUY": "BUY",
            "SELL": "SELL",
            "LONG": "BUY",
            "SHORT": "SELL",
            "CLOSE": "CLOSE",
        }
        return mapping.get(a, "")

    # ----------- 0) Validation structure paquet -----------
    if not isinstance(decision_package, dict):
        reason = "Paquet de décision invalide (type non-dict)."
        logger.error(reason)
        trade_executor._send_alert_safe(
            "CRITIQUE", reason, alert_type="telegram_critical"
        )
        raise TradeExecutionError(reason)

    final_decision = (
        decision_package.get("final_decision")
        or decision_package.get("trade_decision")
        or {}
    )
    market_context = (
        decision_package.get("context") or decision_package.get("market_context") or {}
    )
    active_config = (
        decision_package.get("config_used")
        or decision_package.get("active_config")
        or {}
    )

    if not final_decision:
        reason = "Paquet de décision incomplet: 'final_decision' manquant."
        logger.error(reason)
        trade_executor._send_alert_safe(
            "CRITIQUE", reason, alert_type="telegram_critical"
        )
        raise TradeExecutionError(reason)

    # ----------- 1) Action (multi-champs + normalisation) -----------
    action_raw = _first_non_empty(
        final_decision.get("final_action"),
        final_decision.get("selected_action"),
        final_decision.get("core_action"),
        final_decision.get("action"),
        final_decision.get("side"),
        final_decision.get("direction"),
    )
    action = _normalize_action(action_raw)
    if not action:
        reason = f"Action de trade invalide: '{action_raw}' (attendu: BUY/SELL/CLOSE/LONG/SHORT)."
        logger.error(reason)
        trade_executor._send_alert_safe(
            "CRITIQUE", reason, alert_type="telegram_critical"
        )
        raise TradeExecutionError(reason)

    # ----------- 2) Asset -----------
    raw_asset = _first_non_empty(
        final_decision.get("asset"),
        final_decision.get("symbol"),
        final_decision.get("instrument"),
    )
    if not raw_asset or raw_asset.upper() == "UNKNOWN":
        reason = "Asset/symbole manquant ou 'UNKNOWN' dans la décision."
        logger.error(reason)
        trade_executor._send_alert_safe(
            "CRITIQUE", reason, alert_type="telegram_critical"
        )
        raise TradeExecutionError(reason)
    asset = raw_asset.upper()

    # ----------- 3) order_type propre -----------
    order_type = str(final_decision.get("order_type", "MARKET")).upper()
    allowed_order_types = {"MARKET", "BUY_LIMIT", "SELL_LIMIT", "BUY_STOP", "SELL_STOP"}
    if order_type not in allowed_order_types:
        logger.debug(f"order_type inconnu '{order_type}', fallback 'MARKET'.")
        order_type = "MARKET"

    # ----------- 4) Construire le trade_decision standardisé (sans 'volume') -----------
    
    # --- Résolution robuste du burst_size (decision -> config -> défaut) ---
    def _resolve_burst_size(fd: dict, cfg: dict) -> int:
        try:
            v = fd.get("burst_count") or fd.get("burst_size")
            if v is not None:
                return int(v)
        except Exception:
            pass
        try:
            return int(
                (((cfg or {}).get("entry_rules") or {}).get("scalping") or {})
                .get("burst_scalping", {})
                .get("burst_size", 1)
                or 1
            )
        except Exception:
            return 1

    resolved_burst = _resolve_burst_size(final_decision, active_config)
    logger.info(f"[BURST] resolved_burst_size={resolved_burst}")

    trade_decision = {
        "action": action,
        "asset": asset,
        "order_type": order_type,
        "trigger_price": final_decision.get("trigger_price"),
        "target_sl_pips": final_decision.get("target_sl_pips"),
        "target_tp_pips": final_decision.get("target_tp_pips"),
        "rule_name": final_decision.get("rule_name"),
        "strategy_type": final_decision.get("strategy_type", "unknown"),
        "burst_enabled": bool(final_decision.get("burst_enabled", False)),
        "order_id": final_decision.get("order_id", "N/A"),
        "entry_style": final_decision.get("entry_style"),
        "price": final_decision.get("price"),
        "confidence": final_decision.get("confidence"),
        "volatility_factor": final_decision.get("volatility_factor"),
        "burst_size": int(resolved_burst),

        "sizing_scope": (
            "BASKET"
            if (
                final_decision.get("burst_enabled")
                or final_decision.get("rule_name") == "burst_scalping"
            )
            else "SINGLE"
        ),
    }
    adapted_package = {
        "trade_decision": trade_decision,
        "market_context": market_context,
        "active_config": active_config,
    }

    # ----------- 5) Pre-trade checks (toujours exécutés) -----------
    ok, reason = trade_executor.pre_trade_checks(
        trade_decision, active_config, market_context
    )
    if not ok:
        logger.warning(f"Pipeline de trade AVORTÉ (Erreur contrôlée): {reason}")
        feedback = trade_executor.feedback_pipeline(
            order_id=trade_decision["order_id"], status="failed", reason=reason
        )
        trade_executor._feedback_safe(trade_decision, feedback)
        return {"status": "failed", "reason": reason}

    # ----------- 6) Sélection du mode (BURST vs STANDARD) -----------
    try:
        is_burst = bool(
            trade_decision.get("burst_enabled", False)
            or trade_decision.get("rule_name") == "burst_scalping"
        )

        if is_burst:
            # ——— BURST: sizing unique via fonction centrale (et SL cohérent) ———
            td_with_meta = trade_executor._attach_burst_metadata(dict(trade_decision))

            # Résoudre symbole broker + infos + prix
            try:
                broker_symbol = (
                    trade_executor.mt5_connector.resolve_broker_symbol(asset) or asset
                )
                symbol_info = trade_executor.mt5_connector.get_symbol_info(
                    broker_symbol
                )
                if not symbol_info:
                    raise TradeExecutionError(
                        f"Symbole MT5 introuvable: {broker_symbol}"
                    )
                entry_price_market = trade_executor.mt5_connector.get_current_price(
                    broker_symbol, action
                )
                if not entry_price_market or entry_price_market <= 0:
                    raise TradeExecutionError(
                        f"Prix de marché invalide pour {broker_symbol}."
                    )
            except Exception as e:
                raise TradeExecutionError(f"Résolution symbole/prix KO: {e}")

            # SL requis pour sizing risk% (pas de TP en burst)
            sl_price = None
            try:
                # sl direct ?
                if (
                    isinstance(final_decision.get("sl_price"), (int, float))
                    and final_decision["sl_price"] > 0
                ):
                    sl_price = float(final_decision["sl_price"])
                else:
                    sl_calc, _tp_ignored = trade_executor._calculate_sl_tp_prices(
                        trade_decision,
                        active_config,
                        symbol_info,
                        entry_price_market,
                        market_context,
                    )
                    sl_price = float(sl_calc or 0.0)
                if not (sl_price and sl_price > 0):
                    raise TradeExecutionError(
                        "Burst: SL requis introuvable pour sizing."
                    )
            except Exception as e:
                raise TradeExecutionError(f"Calcul SL burst KO: {e}")

            # Sizing unique (scope BASKET respecté via 'burst_size')
            try:
                account_trade_settings = (
                    market_context.get("active_broker_account", {}) or {}
                ).get("trade_settings", {}) or {}
                per_ticket_volume = float(
                    trade_executor._calculate_risk_based_volume(
                        {
                            "action": action,
                            "asset": broker_symbol,
                            "order_type": order_type,
                            "rule_name": "burst_scalping",
                            "strategy_type": trade_decision.get(
                                "strategy_type", "unknown"
                            ),
                            "sizing_scope": "BASKET",
                            "burst_size": int(resolved_burst),

                        },
                        active_config,
                        market_context,
                        symbol_info,
                        entry_price_market,
                        sl_price,
                        account_trade_settings,
                    )
                )
            except Exception as e:
                raise TradeExecutionError(f"Sizing burst KO: {e}")

            # Appliquer sizing/SL au payload
            td_with_meta["sl_price"] = sl_price
            td_with_meta["volume"] = per_ticket_volume
            td_with_meta["no_tp"] = True

            # Style entrant & prix limite éventuel
            style = str(
                (
                    final_decision.get("entry_style")
                    or trade_decision.get("entry_style")
                    or ""
                )
            ).upper()
            limit_price = float(
                (
                    final_decision.get("price")
                    or trade_decision.get("price")
                    or final_decision.get("trigger_price")
                    or 0.0
                )
                or 0.0
            )

            if is_dry_run:
                return {
                    "status": "dry_run_ready",
                    "mode": "burst",
                    "payload": {
                        "trade_decision": td_with_meta,
                        "market_context": market_context,
                        "active_config": active_config,
                    },
                    "style": style,
                    "price": limit_price,
                }

            # LIMIT_FOK → ordre burst limité (par ticket = per_ticket_volume)
            if style == "LIMIT_FOK" and limit_price > 0:
                burst_decision = {
                    "asset": asset,
                    "action": action,
                    "entry_style": "LIMIT_FOK",
                    "price": limit_price,
                    "burst_count": int(resolved_burst),
                    "burst_volume_each": per_ticket_volume, 
                    "validity_ms": int(final_decision.get("validity_ms", 800) or 800),
                    "no_fallback": True,
                    "comment": td_with_meta.get("comment"),
                }
                return trade_executor.execute_burst_scalping_order(
                    burst_decision, active_config
                )

            # Sinon: MARKET “single master” (trailing-only)
            return trade_executor.execute_burst_single_master(
                {
                    "trade_decision": td_with_meta,
                    "market_context": market_context,
                    "active_config": active_config,
                }
            )

        # --- Mode standard: sizing fait dans prepare_order ---
        mt5_request = trade_executor.prepare_order(adapted_package)

    except Exception as e:
        reason = f"Préparation d'ordre échouée: {e}"
        logger.error(reason, exc_info=True)
        trade_executor._send_alert_safe(
            "CRITIQUE", reason, alert_type="telegram_critical"
        )
        feedback = trade_executor.feedback_pipeline(
            order_id=trade_decision["order_id"], status="failed", reason=str(e)
        )
        trade_executor._feedback_safe(trade_decision, feedback)
        return {"status": "failed", "reason": str(e)}

    # ----------- 7) Exécution standard (ou dry-run) -----------
    if is_dry_run:
        return {
            "status": "dry_run_ready",
            "mode": "standard",
            "request": mt5_request,
            "trade_decision": trade_decision,
        }

    execution_result = trade_executor.execute_order(mt5_request)
    return execution_result


# --- Binding des fonctions des briques comme méthodes de TradeExecutor ---
TradeExecutor._load_settings = _load_settings
TradeExecutor.prepare_order = prepare_order
TradeExecutor._calculate_risk_based_volume = _calculate_risk_based_volume
TradeExecutor._calculate_sl_tp_prices = _calculate_sl_tp_prices
TradeExecutor._normalize_stops = _normalize_stops
TradeExecutor.monitor_burst_baskets = monitor_burst_baskets
TradeExecutor._update_internal_position_state = _update_internal_position_state
TradeExecutor._log_audit_trail = _log_audit_trail
TradeExecutor._mark_trade_sent = _mark_trade_sent
TradeExecutor.generate_report = generate_report
TradeExecutor.manual_override_if_needed = manual_override_if_needed
TradeExecutor.approve_pending_order = approve_pending_order
TradeExecutor.apply_dynamic_trailing = apply_dynamic_trailing
TradeExecutor._modify_sl = _modify_sl
TradeExecutor._bars_since = _bars_since
TradeExecutor.reconcile_state_with_broker = reconcile_state_with_broker
TradeExecutor._update_internal_position_state = _update_internal_position_state
TradeExecutor.pre_trade_checks = pre_trade_checks
TradeExecutor._attach_burst_metadata = _attach_burst_metadata
TradeExecutor.execute_burst_scalping_order = execute_burst_scalping_order
TradeExecutor.execute_burst_single_master = execute_burst_single_master
