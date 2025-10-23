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
from trader.errors import TradeExecutionError
# --- Briques (binding en bas du fichier) ---
from trader.order_builder import prepare_order, _build_mt5_request
from trader.sizing import _calculate_risk_based_volume
from trader.sltp import _calculate_sl_tp_prices, _normalize_stops, _split_multi_tp_orders
from trader.burst import monitor_burst_baskets  
from trader.reconcile import (
    reconcile_state_with_broker,
    _update_internal_position_state,
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

    # ------------------------------------------------------------------------
    # Helpers "safe" (alert & feedback)
    # ------------------------------------------------------------------------
    def _send_alert_safe(self, level: str, message: str, alert_type: str = "telegram_critical") -> None:
        try:
            self.config_manager.send_alert(level if level else "ALERTE", message, alert_type=alert_type)
        except TypeError:
            # compat signatures anciennes
            try:
                self.config_manager.send_alert(message=message, alert_type=alert_type)
            except Exception:
                self.logger.warning("Échec send_alert (toutes variantes).")

    def _feedback_safe(self, trade_decision: Dict[str, Any], feedback: Dict[str, Any]) -> None:
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
            request["sl"] = round(float(sl), digits) if isinstance(sl, (int, float)) else 0.0 if sl else 0.0
            request["tp"] = round(float(tp), digits) if isinstance(tp, (int, float)) else 0.0 if tp else 0.0

        # Envoi
        result = None
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
        RET_DONE_PARTIAL = getattr(mt5, "TRADE_RETCODE_DONE_PARTIAL", 10010) if mt5 else 10010
        RET_INVALID_STOPS = getattr(mt5, "TRADE_RETCODE_INVALID_STOPS", 10016) if mt5 else 10016

        summary = {
            "status": "sent" if retcode in (RET_DONE, RET_PLACED, RET_DONE_PARTIAL) else "failed",
            "order": order,
            "deal": deal,
            "ticket": order or deal,
            "price": price,
            "volume": volume,
            "retcode": retcode,
            "retcode_str": retcode_str,
            "message": retcode_str,
        }

        # Si SL/TP invalides à l’envoi mais ordre rempli, tenter l’attache post-fill
        try_attach = (
            retcode in (RET_DONE, RET_DONE_PARTIAL)
            and (request.get("sl") or request.get("tp"))
            and retcode == RET_INVALID_STOPS  # certains brokers renvoient INVALID_STOPS même si exécuté
        )
        if try_attach:
            try:
                pos_id = deal or order
                if pos_id:
                    self.logger.info("[POST-FILL] tentative attache SL/TP.")
                    self.mt5_connector.modify_position_stops(
                        position=pos_id, sl=request.get("sl") or 0.0, tp=request.get("tp") or 0.0
                    )
                    summary["status"] = "filled"
                    summary["message"] = f"{retcode_str} + post-fill SL/TP attach"
            except Exception as e:
                self.logger.warning(f"[POST-FILL] échec attache SL/TP: {e}")

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

        return summary


# ======================================================================================
#  Pipeline d’exécution (pont DecisionPipeline -> TradeExecutor)
# ======================================================================================
def run_trade_execution_pipeline(
    trade_executor: TradeExecutor, decision_package: Dict[str, Any], is_dry_run: bool = False
) -> Dict[str, Any]:
    """
    Pipeline d’exécution **unique** (STANDARD + BURST SL/TP).

    - ❌ SUPPRESSION TOTALE du mode trailing et des branches LIMIT_FOK spécifiques.
    - ✅ BURST = on garde SL/TP (calculés par order_builder) — on ne force plus no_tp.
    - ✅ Sizing pour BURST en scope 'BASKET' (recommandé) si fourni, sinon inchangé.
    - ✅ JOURNALISATION et audit propre.
    """
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
                    {"status": status, **(details or {})}, getattr(trade_executor, "execution_context", {}) or {}
                )
        except Exception:
            pass

    # -------------------- 1) Extraction/normalisation entrée --------------------
    if not isinstance(decision_package, dict) or "final_decision" not in decision_package:
        raise InvalidDecisionPackageError("decision_package manquant ou invalide (clé 'final_decision').")

    td = dict(decision_package.get("final_decision") or {})
    raw_cfg = dict(
        decision_package.get("config_used")
        or decision_package.get("active_config")
        or decision_package.get("config")
        or trade_executor.config_manager.get_current_dynamic_config()
        or {}
    )
    market_context = dict(decision_package.get("context") or {})
    asset = (td.get("asset") or td.get("symbol") or "").upper()

    # -------------------- 2) Harmonisation Action/Symbole -----------------------
    action = str(td.get("action", "")).strip().upper()
    if action in ("LONG", "SHORT"):
        action = "BUY" if action == "LONG" else "SELL"
        td["action"] = action
    td["symbol"] = asset

    # -------------------- 3) Alignement BURST (SL/TP conservés) -----------------
    is_burst = bool(td.get("burst_enabled") or td.get("burst") or td.get("is_burst"))
    if is_burst:
        # on recommande 'BASKET' côté sizing pour répartir le risk% en interne
        td.setdefault("sizing_scope", "BASKET")

        # NE PAS poser de "no_tp" ici. On garde SL/TP fournis par order_builder.
        # s’assurer que l’entrée est MARKET (single-master logique)
        td["entry_style"] = "MARKET"

        # ratio dynamique déjà géré côté order_builder (volume/SL/TP) → on n’y touche pas ici

        _log("info", f"[BURST][PLAN] {action} {asset} | style=MARKET | SL/TP actifs | scope={td.get('sizing_scope')}")

    # -------------------- 4) Adapter le package et construire la requête --------
    adapted_package = {
        "final_decision": td,
        "context": market_context,
        "active_config": raw_cfg,
    }

    try:
        mt5_request = trade_executor.prepare_order(adapted_package)
    except Exception as e:
        reason = f"Préparation d'ordre échouée: {e}"
        logger.error(reason, exc_info=True)
        trade_executor._send_alert_safe("CRITIQUE", reason, alert_type="telegram_critical")
        feedback = {"status": "failed", "reason": str(e)}
        trade_executor._feedback_safe(td, feedback)
        return {"status": "failed", "reason": str(e)}

    # -------------------- 5) DRY RUN -------------------------------------------
    if is_dry_run:
        _log("info", "[EXEC_PIPE][DRY_RUN_SKIP] standard (non exécuté en DRY_RUN).")
        _audit("skipped", {"mode": "dry_run", "reason": "dry_run_skip", "asset": asset})
        return {
            "status": "dry_run_ready",
            "mode": "standard",
            "request": mt5_request,
            "trade_decision": td,
        }

    # -------------------- 6) Envoi ---------------------------------------------
    execution_result = trade_executor.execute_order(mt5_request)
    ok = isinstance(execution_result, dict) and execution_result.get("status") in {"sent", "placed", "filled"}

    if not ok:
        _log(
            "error",
            f"[EXEC_PIPE][STD_FAIL] asset={asset} reason={isinstance(execution_result, dict) and execution_result.get('reason')}",
        )
        _audit(
            "rejected",
            {
                "asset": asset,
                "reason": isinstance(execution_result, dict) and execution_result.get("reason"),
                "mode": "standard",
            },
        )
    else:
        _log("info", f"[EXEC_PIPE][STD_OK] asset={asset} ticket={execution_result.get('ticket')}")
        _audit("filled", {"asset": asset, "mode": "standard"})

    return execution_result


# ======================================================================================
#  Binding des fonctions des briques comme méthodes de TradeExecutor
#  (on supprime TOUT ce qui venait de trailing.py)
# ======================================================================================
TradeExecutor._load_settings = _update_internal_position_state.__globals__.get("_load_settings") or (lambda *a, **k: None)  # fallback si absent
TradeExecutor.prepare_order = prepare_order
TradeExecutor._build_mt5_request = _build_mt5_request
TradeExecutor._split_multi_tp_orders = _split_multi_tp_orders
TradeExecutor._calculate_risk_based_volume = _calculate_risk_based_volume
TradeExecutor._calculate_sl_tp_prices = _calculate_sl_tp_prices
TradeExecutor._normalize_stops = _normalize_stops
TradeExecutor.monitor_burst_baskets = monitor_burst_baskets
TradeExecutor._update_internal_position_state = _update_internal_position_state
TradeExecutor.reconcile_state_with_broker = reconcile_state_with_broker
TradeExecutor.approve_pending_order = approve_pending_order
TradeExecutor.manual_override_if_needed = manual_override_if_needed
TradeExecutor._log_audit_trail = _log_audit_trail
TradeExecutor._mark_trade_sent = _mark_trade_sent
TradeExecutor.generate_report = generate_report
