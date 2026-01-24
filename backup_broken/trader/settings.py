#trader/settings.py - Configuration du Bot SNIPER_X
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict
from trader.errors import TradeExecutionError



def _load_settings(self):
    """
    Charge et assigne les paramètres dynamiques et les mappings MT5 depuis le ConfigManager.
    Résout MT5 via le connecteur (fallback import), sans import module global.
    """
    self.logger.debug("Chargement des paramètres pour le TradeExecutor...")

    # Chemins et paramètres de comportement
    logs_dir = Path(self.config_manager.get("paths.logs", "logs/"))
    audit_filename = self.config_manager.get(
        "trade_executor_settings.audit_trail_file_name", "trade_audit_trail.jsonl"
    )
    self.audit_trail_path = logs_dir / audit_filename
    self.audit_trail_path.parent.mkdir(parents=True, exist_ok=True)

    # Retries MT5
    self.mt5_max_retries = int(self.config_manager.get("trade_executor_settings.mt5_max_retries", 3) or 3)
    if self.mt5_max_retries < 0:
        self.logger.warning("Paramètre 'mt5_max_retries' invalide. Utilisation de 3.")
        self.mt5_max_retries = 3

    self.mt5_retry_delay_seconds = int(
        self.config_manager.get("trade_executor_settings.mt5_retry_delay_seconds", 2) or 2
    )
    if self.mt5_retry_delay_seconds < 0:
        self.mt5_retry_delay_seconds = 0

    # Mappings des constantes MT5
    mt5_mappings = self.config_manager.get("mt5_mappings", {}) or {}
    if not isinstance(mt5_mappings, dict):
        mt5_mappings = {}
    self.mt5_mappings = mt5_mappings


    # --- Résolution du module MT5 (via connecteur, sinon import lazy) ---
    mt5_mod = getattr(getattr(self, "mt5_connector", None), "mt5", None) or getattr(self, "mt5", None)
    if mt5_mod is None:
        try:
            import MetaTrader5 as _mt5  # lazy import
            mt5_mod = _mt5
        except Exception:
            mt5_mod = None

    if mt5_mod is None:
        # Pas de constantes → plus rien ne fonctionnera correctement: on fail-fast
        raise TradeExecutionError("MT5 API indisponible lors du chargement des constantes.")

    # --- Types d'ordres ---
    self.ORDER_TYPE_BUY = getattr(
        mt5_mod, self.mt5_mappings.get("order_types", {}).get("BUY", "ORDER_TYPE_BUY")
    )
    self.ORDER_TYPE_SELL = getattr(
        mt5_mod, self.mt5_mappings.get("order_types", {}).get("SELL", "ORDER_TYPE_SELL")
    )

    # --- Types de positions (close_position etc.) ---
    self.POSITION_TYPE_BUY = getattr(
        mt5_mod, self.mt5_mappings.get("position_types", {}).get("BUY", "POSITION_TYPE_BUY")
    )
    self.POSITION_TYPE_SELL = getattr(
        mt5_mod, self.mt5_mappings.get("position_types", {}).get("SELL", "POSITION_TYPE_SELL")
    )

    # --- Actions de trading ---
    self.TRADE_ACTION_DEAL = getattr(
        mt5_mod, self.mt5_mappings.get("trade_actions", {}).get("DEAL", "TRADE_ACTION_DEAL")
    )
    self.TRADE_ACTION_PENDING = getattr(
        mt5_mod, self.mt5_mappings.get("trade_actions", {}).get("PENDING", "TRADE_ACTION_PENDING")
    )
    self.TRADE_ACTION_MODIFY = getattr(
        mt5_mod, self.mt5_mappings.get("trade_actions", {}).get("MODIFY", "TRADE_ACTION_MODIFY")
    )

    # --- Codes de retour ---
    self.TRADE_RETCODE_DONE = getattr(
        mt5_mod, self.mt5_mappings.get("trade_retcodes", {}).get("RETCODE_DONE", "TRADE_RETCODE_DONE")
    )
    self.TRADE_RETCODE_REQUOTE = getattr(
        mt5_mod, self.mt5_mappings.get("trade_retcodes", {}).get("RETCODE_REQUOTE", "TRADE_RETCODE_REQUOTE")
    )
    self.TRADE_RETCODE_REJECT = getattr(
        mt5_mod, self.mt5_mappings.get("trade_retcodes", {}).get("RETCODE_REJECT", "TRADE_RETCODE_REJECT")
    )
    self.TRADE_RETCODE_TRADE_DISABLED = getattr(
        mt5_mod, self.mt5_mappings.get("trade_retcodes", {}).get("RETCODE_TRADE_DISABLED", "TRADE_RETCODE_TRADE_DISABLED")
    )

    # --- Politiques d'exécution et de temps ---
    self.ORDER_TIME_GTC = getattr(
        mt5_mod, self.mt5_mappings.get("order_time_flags", {}).get("GTC", "ORDER_TIME_GTC")
    )
    self.ORDER_FILLING_FOK = getattr(
        mt5_mod, self.mt5_mappings.get("order_filling_policies", {}).get("FOK", "ORDER_FILLING_FOK")
    )
    self.ORDER_FILLING_IOC = getattr(
        mt5_mod, self.mt5_mappings.get("order_filling_policies", {}).get("IOC", "ORDER_FILLING_IOC")
    )

    self.logger.info(
        f"Paramètres de l'exécuteur chargés. Retries MT5: {self.mt5_max_retries}, Delay: {self.mt5_retry_delay_seconds}s."
    )

