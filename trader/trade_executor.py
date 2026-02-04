# trader/trade_executor.py
from __future__ import annotations

import json
import logging
import time
import math
from datetime import datetime, UTC
from typing import Any, Dict, List, Optional, Tuple

try:
    import MetaTrader5 as mt5  # type: ignore
except Exception:  # MT5 peut ne pas être dispo en environnement de test
    mt5 = None  # noqa

from core.utils import CustomJSONEncoder

from trader.errors import TradeExecutionError, InvalidDecisionPackageError

# --- Briques (binding en bas du fichier) ---
from trader.order_builder import prepare_order, _build_mt5_request
from trader.sizing import _calculate_risk_based_volume
from trader.sltp import (
    _calculate_sl_tp_prices,
    _split_multi_tp_orders,
    _resolve_basket_context_for_sltp,
)
from trader.burst import monitor_burst_baskets, open_burst_basket
from trader.reconcile import (
    reconcile_state_with_broker,
    _update_internal_position_state,
    monitor_pending_orders,
)

# Ces deux fonctions viennent de validators (pas de reconcile)
from trader.validators import (
    manual_override_if_needed,
    approve_pending_order,
)

from trader.audit import _log_audit_trail, _mark_trade_sent, generate_report


# ======================================================================================
#  TradeExecutor
# ======================================================================================
class TradeExecutor:
    """
    Exécuteur central : envoie les ordres construits par order_builder et gère
    l'assemblage SL/TP, les post-traitements et l'audit.

    ⚠️ IMPORTANT
    - BURST utilise désormais **SL/TP** (pas de trailing). On ne neutralise plus les TP.
    - Aucune dépendance à trailing.py.
    """

    def __init__(self, config_manager, mt5_connector, mode: Optional[str] = None):
        self.config_manager = config_manager
        self.mt5_connector = mt5_connector
        self.logger = logging.getLogger(__name__)
        self.mode = (mode or config_manager.get("mode_execution", "DEMO")).upper()

        # Config dynamique (pour sltp.py qui accède à self.config)
        try:
            self.config = config_manager.get_current_dynamic_config()
        except Exception:
            self.config = {}

        # Config stratégie scalping (pour trailing stop)
        # Charger directement depuis le fichier car strategy_manager.get_strategy_config() ne fonctionne pas
        self.strategy_config = {}
        try:
            import json
            import os
            strategy_config_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "config", "strategy", "config_trade_scalping.json"
            )
            if os.path.exists(strategy_config_path):
                with open(strategy_config_path, 'r', encoding='utf-8') as f:
                    self.strategy_config = json.load(f)
                self.logger.info(f"✅ Config stratégie scalping chargée depuis {strategy_config_path}")
            else:
                self.logger.warning(f"⚠️ Fichier config stratégie introuvable: {strategy_config_path}")
        except Exception as e:
            self.logger.error(f"❌ Erreur chargement config stratégie: {e}")
            self.strategy_config = {}

        # Quelques paramètres MT5 utiles (fallback robustes)
        self.mt5_max_retries = int(
            config_manager.get("trade_executor_settings.order_send_max_retries", 2) or 2
        )
        self.mt5_retry_delay_seconds = float(
            config_manager.get("trade_executor_settings.order_send_retry_sleep_s", 0.05)
            or 0.05
        )

        # Throttle interne (ex: anti-spam par symbole)
        self._last_trade_sent_ts: Dict[str, float] = {}

        # Optionnel: AuditLogger attaché dans ConfigManager
        self.audit_logger = getattr(config_manager, "audit_logger", None)

        # ✅ AJOUTÉ (25 Nov 2025): Journalisation trades pour analyse data-driven
        from trader.trade_logger import TradeLogger
        try:
            self.trade_logger = TradeLogger(config_manager)
            self.logger.info("✅ TradeLogger initialisé - journalisation trades activée")
        except Exception as e:
            self.logger.error(f"❌ Erreur initialisation TradeLogger: {e}", exc_info=True)
            self.trade_logger = None

    # ------------------------------------------------------------------------
    # Helpers "safe" (alert & feedback)
    # ------------------------------------------------------------------------
    def _send_alert_safe(
        self, level: str, message: str, alert_type: str = "telegram_critical"
    ) -> None:
        try:
            self.config_manager.send_alert(
                level if level else "ALERTE", message, alert_type=alert_type
            )
        except TypeError:
            # compat signatures anciennes
            try:
                self.config_manager.send_alert(message=message, alert_type=alert_type)
            except Exception:
                self.logger.warning("Échec send_alert (toutes variantes).")

    def _feedback_safe(
        self, trade_decision: Dict[str, Any], feedback: Dict[str, Any]
    ) -> None:
        try:
            if hasattr(self.config_manager, "log_decision"):
                self.config_manager.log_decision(
                    reason="trade_execution_feedback",
                    trade_decision=trade_decision,
                    config=self.config_manager.get_current_dynamic_config(),
                    context=feedback,
                )
        except Exception:
            pass

    # ------------------------------------------------------------------------
    # Envoi d’un ordre (standardisé)
    # ------------------------------------------------------------------------
    def execute_order(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """
        Envoie un ordre MT5 standardisé (MARKET ou PENDING) avec SL/TP si fournis.
        - Gère SL/TP **sans** neutralisation (BURST inclus).
        - Si le broker refuse SL/TP à l’envoi (retcode invalid_stops), on tente l’attache post-fill.

        Returns: dict résumé {'status', 'order', 'deal', 'ticket', 'price', 'volume', 'retcode', 'retcode_str', 'message'}
        """
        symbol = str(request.get("symbol") or request.get("asset") or "").upper()
        if not symbol:
            return {"status": "failed", "reason": "symbol_missing"}

        # Nettoyage/robustesse SL/TP
        sl = request.get("sl")
        tp = request.get("tp")
        if sl is not None or tp is not None:
            try:
                si = self.mt5_connector.symbol_info(symbol)
                digits = int(getattr(si, "digits", 5) or 5) if si else 5
            except Exception:
                digits = 5
            request["sl"] = (
                round(float(sl), digits)
                if isinstance(sl, (int, float))
                else 0.0 if sl else 0.0
            )
            request["tp"] = (
                round(float(tp), digits)
                if isinstance(tp, (int, float))
                else 0.0 if tp else 0.0
            )

        # Envoi
        result = None

        # DEBUG: Traçage envoi MT5
        print(f"🔍 [SL_TRACE][MT5_SEND] Envoi ordre | symbol={request.get('symbol')} | SL={request.get('sl')} | TP={request.get('tp')} | type={request.get('type')} | volume={request.get('volume')}", flush=True)

        try:
            result = self.mt5_connector.order_send(request)
        except Exception as e:
            self.logger.error(f"[MT5] order_send exception: {e}", exc_info=True)
            return {"status": "failed", "reason": f"order_send_exception: {e}"}

        # Analyse du résultat
        if not result:
            return {"status": "failed", "reason": "no_result_from_broker"}

        retcode = getattr(result, "retcode", None)
        retcode_str = getattr(result, "comment", "") or str(retcode)
        order = getattr(result, "order", None)
        deal = getattr(result, "deal", None)
        price = getattr(result, "price", None)
        volume = getattr(result, "volume", None)

        # Codes MT5 usuels
        RET_DONE = getattr(mt5, "TRADE_RETCODE_DONE", 10009) if mt5 else 10009
        RET_PLACED = getattr(mt5, "TRADE_RETCODE_PLACED", 10008) if mt5 else 10008
        RET_DONE_PARTIAL = (
            getattr(mt5, "TRADE_RETCODE_DONE_PARTIAL", 10010) if mt5 else 10010
        )
        RET_INVALID_STOPS = (
            getattr(mt5, "TRADE_RETCODE_INVALID_STOPS", 10016) if mt5 else 10016
        )

        summary = {
            "status": (
                "sent"
                if retcode in (RET_DONE, RET_PLACED, RET_DONE_PARTIAL)
                else "failed"
            ),
            "order": order,
            "deal": deal,
            "ticket": order or deal,
            "price": price,
            "volume": volume,
            "retcode": retcode,
            "retcode_str": retcode_str,
            "message": retcode_str,
        }

        # Si SL/TP invalides à l'envoi, tenter l'attache post-fill
        try_attach = (
            retcode == RET_INVALID_STOPS
            and (request.get("sl") or request.get("tp"))
        )

        # DEBUG: Traçage post-fill
        print(f"🔍 [SL_TRACE][POST_FILL] retcode={retcode} | RET_INVALID_STOPS={RET_INVALID_STOPS} | try_attach={try_attach} | SL={request.get('sl')} | TP={request.get('tp')}", flush=True)

        if try_attach:
            try:
                pos_id = deal or order
                if pos_id:
                    print(f"🔍 [SL_TRACE][POST_FILL] Attache SL/TP | pos_id={pos_id} | SL={request.get('sl')} | TP={request.get('tp')}", flush=True)
                    self.logger.info("[POST-FILL] tentative attache SL/TP.")
                    self.mt5_connector.modify_position_sltp(
                        ticket=pos_id,
                        sl=request.get("sl") or 0.0,
                        tp=request.get("tp") or 0.0,
                    )
                    summary["status"] = "filled"
                    summary["message"] = f"{retcode_str} + post-fill SL/TP attach"
                    print(f"🔍 [SL_TRACE][POST_FILL] ✅ Attache réussie | pos_id={pos_id}", flush=True)
            except Exception as e:
                self.logger.warning(f"[POST-FILL] échec attache SL/TP: {e}")
                print(f"🔍 [SL_TRACE][POST_FILL] ❌ Attache échouée | error={e}", flush=True)

        # Marquage throttle (anti-spam symbol)
        try:
            self._mark_trade_sent(symbol, time.time())
        except Exception:
            pass

        # Audit
        try:
            if self.audit_logger:
                payload = {
                    "request": request,
                    "result": {
                        "retcode": retcode,
                        "retcode_str": retcode_str,
                        "order": order,
                        "deal": deal,
                        "price": price,
                        "volume": volume,
                    },
                }
                self.audit_logger.log_trade_execution(payload, context={})
        except Exception:
            pass

        # Notification Telegram
        try:
            if summary["status"] in ("sent", "filled"):
                action_type = "BUY" if request.get("type") == 0 else "SELL"
                msg = (
                    f"🚀 *Trade Execute*\n"
                    f"Symbol: `{symbol}`\n"
                    f"Action: `{action_type}`\n"
                    f"Volume: `{volume}` lots\n"
                    f"Prix: `{price}`\n"
                    f"Ticket: `{order or deal}`"
                )
                self.config_manager.send_alert(msg, "telegram_trade_confirmed")
        except Exception as e:
            self.logger.debug(f"Notification Telegram non envoyee: {e}")

        return summary


# ======================================================================================
#  Pipeline d’exécution (pont DecisionPipeline -> TradeExecutor)
# ======================================================================================


def run_trade_execution_pipeline(
    trade_executor: "TradeExecutor",
    decision_package: Dict[str, Any],
    is_dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Pipeline d’exécution (STANDARD + BURST) — version “desk” minimaliste

    - S’appuie sur les fonctions existantes qui bossent déjà :
        • trade_executor.prepare_order(adapted_package)  -> construit la requête (SL/TP, volume si géré en amont)
        • trade_executor.execute_order(mt5_request)      -> envoi 1 ticket (STANDARD)
        • trade_executor.open_burst_basket(req, n)       -> envoi N tickets (BURST)
    - Aucune sécurité/fallback/clamp ajoutés ici.
    - Si le volume est 0, c’est à l’amont (décision/builder) de le fournir. Ici on n’intervient pas.

    Entrée attendue (final_decision):
      - action ∈ {"BUY","SELL"} (LONG/SHORT accepté et normalisé)
      - asset/symbol
      - volume (lot par leg) géré par la décision/builder si nécessaire
      - burst_size optionnel (sinon pris via conf)
    """
    import logging

    logger = logging.getLogger(__name__)

    def _log(level: str, msg: str):
        lg = getattr(trade_executor, "logger", logger)
        try:
            getattr(lg, level)(msg)
        except Exception:
            lg.info(msg)

    def _audit(status: str, details: Dict[str, Any]):
        try:
            if hasattr(trade_executor, "audit_logger") and trade_executor.audit_logger:
                trade_executor.audit_logger.log_trade_execution(
                    {"status": status, **(details or {})},
                    getattr(trade_executor, "execution_context", {}) or {},
                )
        except Exception:
            pass

    # -------------------- 1) Lecture / normalisation légère --------------------
    if not isinstance(decision_package, dict) or "final_decision" not in decision_package:
        raise InvalidDecisionPackageError("decision_package manquant ou invalide (clé 'final_decision').")

    td = dict(decision_package.get("final_decision") or {})

    raw_cfg = dict(
        decision_package.get("active_config")
        or decision_package.get("config_used")
        or decision_package.get("config")
        or (
            getattr(trade_executor, "config_manager", None)
            and trade_executor.config_manager.get_current_dynamic_config()
        )
        or {}
    )
    market_context = dict(decision_package.get("market_context") or decision_package.get("context") or {})

    asset = (td.get("asset") or td.get("symbol") or td.get("instrument") or "").strip().upper()
    if asset:
        td["symbol"] = asset

    action = str(td.get("action", "")).strip().upper()
    if action in ("LONG", "SHORT"):
        action = "BUY" if action == "LONG" else "SELL"
    td["action"] = action

    # -------------------- 2) Flags burst simples + style MARKET -----------------
    is_burst_flag = bool(td.get("burst_enabled") or td.get("burst") or td.get("is_burst"))
    if is_burst_flag:
        td.setdefault("sizing_scope", "BASKET")
        td["entry_style"] = "MARKET"
        _log("info", f"[BURST][PLAN] {action} {asset} | style=MARKET | scope={td.get('sizing_scope')}")

    # -------------------- 3) Résolution burst_size (priorité décision > conf) ---
    def _pos_int(x, default=1):
        try:
            v = int(x)
            return v if v > 0 else default
        except Exception:
            return default

    asset_cfgs = (decision_package.get("asset_configs") or market_context.get("asset_configs") or {}) or {}
    asset_cfg  = asset_cfgs.get(asset) or {}

    burst_size = _pos_int(
        td.get("burst_size")
        or td.get("burst_count")
        or (asset_cfg.get("scalping") or {}).get("burst", {}).get("burst_size")
        or (raw_cfg.get("scalping")  or {}).get("burst", {}).get("burst_size")
        or (((raw_cfg.get("entry_rules") or {}).get("scalping") or {}).get("burst_scalping", {}) or {}).get("burst_size")
        or 1,
        1,
    )
    td["burst_size"] = burst_size

    # -------------------- 4) Construction requête via prepare_order -------------
    adapted_package = {
        "trade_decision": td,
        "market_context": market_context,
        "active_config": raw_cfg,
        "final_decision": td,
    }
    try:
        mt5_request = trade_executor.prepare_order(adapted_package)
    except Exception as e:
        _log("error", f"Préparation d'ordre échouée: {e}")
        if hasattr(trade_executor, "_send_alert_safe"):
            try: trade_executor._send_alert_safe("CRITIQUE", f"Préparation d'ordre échouée: {e}", alert_type="telegram_critical")
            except Exception: pass
        if hasattr(trade_executor, "_feedback_safe"):
            try: trade_executor._feedback_safe(td, {"status":"failed","reason":str(e)})
            except Exception: pass
        return {"status": "failed", "reason": str(e)}

    # -------------------- 5) DRY RUN -------------------------------------------
    if is_dry_run:
        return {
            "status": "dry_run_ready",
            "mode": "burst" if burst_size > 1 else "standard",
            "request": dict(mt5_request),
            "trade_decision": td,
        }

    # -------------------- 6) STANDARD vs BURST ----------------------------------
    rule = str(td.get("rule") or td.get("rule_name") or "").lower()
    is_burst = bool(is_burst_flag or ("burst" in rule) or (burst_size > 1))

    if not is_burst or burst_size == 1:
        # STANDARD: un seul ticket
        r = trade_executor.execute_order(dict(mt5_request))
        ok = isinstance(r, dict) and r.get("status") in {"sent", "placed", "filled"}
        if ok:
            _log("info", f"[EXEC][STD_OK] asset={asset} ticket={r.get('ticket')}")
            _audit("filled", {"asset": asset, "mode": "standard"})
        else:
            _log("error", f"[EXEC][STD_FAIL] asset={asset} reason={isinstance(r, dict) and r.get('reason')}")
            _audit("rejected", {"asset": asset, "mode": "standard", "reason": (isinstance(r, dict) and r.get("reason"))})
        return r

    # BURST: délègue à l'implémentation native (gère N tickets, volumes, commentaires, etc.)
    _log("info", f"[EXEC][BURST] open_burst_basket burst_size={burst_size}")

    # ✅ AJOUTÉ (25 Nov 2025): Passer trade_decision pour journalisation
    mt5_request["trade_decision"] = td

    try:
        burst_result = trade_executor.open_burst_basket(mt5_request, burst_size)
    except Exception as e:
        _log("error", f"[EXEC][BURST] open_burst_basket KO: {e}")
        _audit("rejected", {"asset": asset, "mode": "burst", "burst_size": burst_size, "reason": str(e)})
        return {"status": "failed", "reason": str(e)}

    if isinstance(burst_result, dict) and burst_result.get("status") in {"ok", "partial", "filled"}:
        _log("info", f"[EXEC][BURST_OK] {asset} status={burst_result.get('status')}")
        _audit("filled", {"asset": asset, "mode": "burst", "burst_size": burst_size})
    else:
        _log("error", f"[EXEC][BURST_FAIL] {asset} reason={burst_result.get('reason') if isinstance(burst_result, dict) else 'unknown'}")
        _audit("rejected", {"asset": asset, "mode": "burst", "burst_size": burst_size, "reason": "EXEC_FAIL"})

    return burst_result or {"status": "failed", "reason": "burst_exec_error"}



# ======================================================================================
#  Binding des fonctions des briques comme méthodes de TradeExecutor
#  (on supprime TOUT ce qui venait de trailing.py)
# ======================================================================================
TradeExecutor._load_settings = _update_internal_position_state.__globals__.get(
    "_load_settings"
) or (
    lambda *a, **k: None
)  # fallback si absent
TradeExecutor.prepare_order = prepare_order
TradeExecutor._build_mt5_request = _build_mt5_request
TradeExecutor._split_multi_tp_orders = _split_multi_tp_orders
TradeExecutor._calculate_risk_based_volume = _calculate_risk_based_volume
TradeExecutor._calculate_sl_tp_prices = _calculate_sl_tp_prices
TradeExecutor.monitor_burst_baskets = monitor_burst_baskets
TradeExecutor._update_internal_position_state = _update_internal_position_state
TradeExecutor.reconcile_state_with_broker = reconcile_state_with_broker
TradeExecutor.monitor_pending_orders = monitor_pending_orders
TradeExecutor.approve_pending_order = approve_pending_order
TradeExecutor.manual_override_if_needed = manual_override_if_needed
TradeExecutor._log_audit_trail = _log_audit_trail
TradeExecutor._mark_trade_sent = _mark_trade_sent
TradeExecutor.generate_report = generate_report
TradeExecutor.open_burst_basket = open_burst_basket
TradeExecutor._resolve_basket_context_for_sltp = _resolve_basket_context_for_sltp