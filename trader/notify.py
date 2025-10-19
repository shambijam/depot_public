#trader/notify.py - Module de Notification pour le Bot SNIPER_X
from __future__ import annotations

from typing import Any, Optional, Dict
from datetime import datetime, UTC



def _send_alert_safe(self, level: str, message: str, alert_type: str | None = None):
    """
    Envoie une alerte en s'adaptant à la signature de ConfigManager.send_alert.
    Évite les erreurs 'multiple values' et 'too many positional arguments'.
    """
    try:
        if alert_type is None:
            return self.config_manager.send_alert(level, message)
        # d'abord en mot-clé (si supporté)
        return self.config_manager.send_alert(level, message, alert_type=alert_type)
    except TypeError:
        self.logger.warning(
            "[ALERT] send_alert ne supporte pas 'alert_type'; envoi sans ce paramètre."
        )
        return self.config_manager.send_alert(level, message)

def _feedback_safe(self, suggestion: dict, feedback: dict):
    """
    Tente d'envoyer le feedback à l'IA en respectant la signature réelle:
        feedback_on_result(suggestion, result)
    """
    ai = getattr(self.config_manager, "ai_decision_instance", None)
    if not ai:
        return
    try:
        return ai.feedback_on_result(suggestion, feedback)
    except Exception as e:
        self.logger.warning(
            f"[AI FEEDBACK] Impossible d'appeler feedback_on_result: {e}"
        )

def log_and_notify(self, mt5_request: dict, execution_status: dict) -> None:
    """
    Journalise l'événement de trade et envoie des notifications basées sur templates.
    - Affiche l'action utilisateur (ACHAT/VENTE/CLOTURE) à partir du 'type' (ORDER_TYPE_*), pas du 'action' MT5.
    - Envoi via _send_alert_safe(level, message, alert_type=...).
    """
    status = str(execution_status.get("status", "unknown")).lower()

    # Déterminer l'action lisible (ACHAT/VENTE/CLOTURE)
    order_type = mt5_request.get("type")
    action_str = "AUTRE"
    try:
        ot_buy = getattr(self, "ORDER_TYPE_BUY", None)
        ot_sell = getattr(self, "ORDER_TYPE_SELL", None)
        if order_type == ot_buy:
            action_str = "ACHAT"
        elif order_type == ot_sell:
            action_str = "VENTE"
        else:
            # Heuristic close
            if str(mt5_request.get("close_reason", "")).strip():
                action_str = "CLOTURE"
    except Exception:
        pass

    # Contexte pour le template
    template_context: Dict[str, Any] = {
        "order_id": mt5_request.get("order_id", "N/A"),
        "action": order_type,  # numérique ORDER_TYPE_* (si présent)
        "action_str": action_str,
        "volume": mt5_request.get("volume", "N/A"),
        "symbol": mt5_request.get("symbol", "N/A"),
        "price": mt5_request.get("price", "N/A"),
        "magic": mt5_request.get("magic", "N/A"),
        "sl": mt5_request.get("sl", "N/A"),
        "tp": mt5_request.get("tp", "N/A"),
        "message": execution_status.get("message", "N/A"),
        "retcode": (execution_status.get("mt5_result") or {}).get("retcode", "N/A"),
        "comment": (execution_status.get("mt5_result") or {}).get("comment", "N/A"),
        "deal": (execution_status.get("mt5_result") or {}).get("deal", "N/A"),
        "order": (execution_status.get("mt5_result") or {}).get("order", "N/A"),
        "pnl_usd": execution_status.get("pnl_usd", "N/A"),
        "account_id": mt5_request.get("account_id", "N/A"),
        "broker_name": mt5_request.get("broker_name", "N/A"),
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "status": status,
    }

    # Template (config → fallback)
    template_key = f"telegram.templates.trade_execution.{status}"
    default_template = self.config_manager.get(
        template_key,
        (
            "**Trade**\n"
            "Statut: {status}\n"
            "Ordre: {order_id} | Symbole: {symbol} | Action: {action_str} | Volume: {volume} | "
            "Prix: {price} | SL: {sl} | TP: {tp}\n"
            "Retcode: {retcode} | Comment: {comment}\n"
            "Message: {message}"
        ),
    )

    # Formatage robuste
    try:
        message = default_template.format(**template_context)
    except KeyError as e:
        self.logger.error(
            f"[NOTIFY] Clé manquante dans le template '{template_key}': {e}. "
            "Utilisation du template brut.", exc_info=False
        )
        message = default_template
    except Exception as e:
        self.logger.error(
            f"[NOTIFY] Formatage template '{template_key}' KO: {e}. "
            "Utilisation du template brut.", exc_info=True
        )
        message = default_template

    # Choix du canal + niveau
    alert_channel = self.config_manager.get(
        f"telegram.trade_channels.{status}", "telegram_critical"
    )
    level_map = {
        "success": "INFO",
        "done": "INFO",
        "placed": "INFO",
        "warning": "WARNING",
        "rejected": "CRITIQUE",
        "error": "CRITIQUE",
        "failed": "CRITIQUE",
        "unknown": "CRITIQUE",
    }
    level = level_map.get(status, "INFO")

    # Envoi (signature sûre)
    try:
        _send_alert_safe(self, level, message, alert_type=alert_channel)
    except Exception as e:
        self.logger.error(f"[NOTIFY] Envoi alerte KO: {e}", exc_info=True)
 
def feedback_pipeline(
    self,
    order_id: str,
    status: str,
    reason: str = "",
    pnl_usd: Optional[float] = None,
) -> dict:
    """
    Construit et publie un feedback standardisé (logger passif).
    Ne bloque jamais le pipeline en cas d'échec de log.
    """
    feedback = {
        "order_id": order_id,
        "execution_status": status,
        "reason": reason,
        "timestamp": datetime.now(UTC).isoformat(),
        "pnl_usd": pnl_usd,
    }
    self.logger.info(
        f"Feedback ordre {order_id}: status='{status}', reason='{reason}', pnl={pnl_usd if pnl_usd is not None else 'N/A'}."
    )

    # Publication vers le logger passif (si présent)
    try:
        ai_mod = getattr(self.config_manager, "ai_decision_instance", None)
        if ai_mod:
            # Appel moderne (decision={}, result=feedback)
            ai_mod.feedback_on_result({}, feedback)
            self.logger.debug(
                f"Feedback envoyé à AIDecision (logger passif) pour ordre {order_id}."
            )
    except Exception as e:
        # Soft-fail: jamais bloquant
        self.logger.error(
            f"Échec envoi feedback à AIDecision pour ordre {order_id}: {e}",
            exc_info=True,
        )

    return feedback

