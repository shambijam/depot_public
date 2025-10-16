# trade_executor.py - Module Central d'Exécution des Trades pour le Bot SNIPER_X
from __future__ import annotations

import logging
import sys
import time
import re, time
import uuid
import json
import pandas as pd
import numpy as np
import math
import os
import jsonschema
from typing import Any
from datetime import datetime, UTC
from pathlib import Path
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta, UTC, timezone
from typing import TYPE_CHECKING
from core.utils import enforce_no_tp_for_burst


if TYPE_CHECKING:
    from core.config_manager import ConfigManager
from core.utils import CustomJSONEncoder


try:
    # TODO: Remplacer l'import direct par une classe de base abstraite (ABC) pour le connecteur
    #       afin de découpler totalement le TradeExecutor de l'implémentation MT5.
    from mt5_connector import MT5Connector
    import MetaTrader5 as mt5
except ImportError as e:
    # Ce bloc reste une sécurité essentielle
    print(
        f"ERREUR FATALE : Échec de l'importation d'une dépendance. Erreur : {e}",
        file=sys.stderr,
    )
    sys.exit(1)


# --- Définitions d'Exceptions Personnalisées ---
class TradeExecutionError(Exception):
    """Exception levée pour les erreurs critiques durant l'exécution d'un trade."""

    pass


class InvalidDecisionPackageError(ValueError):
    """Exception levée lorsqu'un package de décision est malformé ou incomplet."""

    pass


# ==============================
# === Helpers Trading Utils ====
# ==============================


def _normalize_stops(symbol_info, price, sl, tp):
    point = float(getattr(symbol_info, "point", 0.0) or 0.0)
    digits = int(getattr(symbol_info, "digits", 0) or 0)

    # Arrondis sûrs si présents
    if isinstance(sl, (int, float)):
        sl = round(float(sl), digits)
    else:
        sl = None

    if isinstance(tp, (int, float)):
        tp = round(float(tp), digits)
    else:
        tp = None

    # ⚠️ Ne fais des diffs que si la valeur existe
    sl_dist = (price - sl) if (sl is not None) else None
    tp_dist = (tp - price) if (tp is not None) else None

    # Stops level broker
    stops_lvl_pts = float(
        getattr(symbol_info, "trade_stops_level", 0)
        or getattr(symbol_info, "stops_level", 0)
        or 0
    )
    min_stop = stops_lvl_pts * point

    # Ajustement SL si présent
    if sl is not None and min_stop > 0:
        # BUY: sl < price | SELL: sl > price
        # On corrige seulement si trop proche
        if sl < price and (price - sl) < min_stop:
            sl = round(price - min_stop, digits)
        elif sl > price and (sl - price) < min_stop:
            sl = round(price + min_stop, digits)

    # TP : ne rien faire si None (stratégie trailing-only)
    return sl, tp


def _precheck_and_split_burst(
    symbol_info, desired_vol_list, entry_price, sl_price, account_info
):
    # calc margin par 1 lot (ou par step), puis dimensionne
    contract_size = float(getattr(symbol_info, "trade_contract_size", 0) or 100)
    leverage = float(getattr(account_info, "leverage", 100) or 100)
    # marge approx par lot = (prix * contract_size) / leverage
    margin_per_lot = (entry_price * contract_size) / max(leverage, 1.0)

    free_margin = float(getattr(account_info, "margin_free", 0.0) or 0.0)

    out = []
    fm = free_margin
    for vol in desired_vol_list:
        need = vol * margin_per_lot
        if need <= fm:
            out.append(vol)
            fm -= need
        else:
            # tente un downscale à la plus proche marche broker
            vmin = float(getattr(symbol_info, "volume_min", 0.01) or 0.01)
            vstep = float(getattr(symbol_info, "volume_step", 0.01) or 0.01)
            vmax_afford = max(vmin, (fm // margin_per_lot) * 1.0)  # lot entier
            # quantifie sur marche
            steps = int((vmax_afford - vmin) // vstep)
            vol_adj = max(vmin, vmin + steps * vstep) if steps >= 0 else 0.0
            if vol_adj >= vmin and (vol_adj * margin_per_lot) <= fm:
                out.append(vol_adj)
                fm -= vol_adj * margin_per_lot
            else:
                out.append(0.0)  # on skippera cet ordre

    return [v for v in out if v > 0.0]


def compute_lot_from_risk(
    symbol_info,
    account_info,
    entry,
    sl,
    risk_pct,
    confidence=1.0,
    burst_size=1,
    atr=None,
    atr_ref=10.0,
):

    balance = float(getattr(account_info, "balance", 0.0) or 0.0)
    risk_usd = balance * (risk_pct / 100.0)

    # modulation par la confiance
    risk_usd *= max(0.0, min(1.0, confidence))

    # modulation par la volatilité
    if atr is not None and atr > 0:
        vol_factor = atr_ref / atr
        vol_factor = min(2.0, max(0.5, vol_factor))
        risk_usd *= vol_factor

    # répartition sur un burst
    if burst_size > 1:
        risk_usd /= burst_size

    # --- reste du calcul identique ---
    point = float(getattr(symbol_info, "point", 0.01) or 0.01)
    contract_size = float(getattr(symbol_info, "trade_contract_size", 100) or 100)
    sl_dist_price = abs(entry - sl)
    if sl_dist_price <= 0:
        return 0.0

    value_per_price_unit_per_lot = contract_size
    loss_per_lot = sl_dist_price * value_per_price_unit_per_lot
    if loss_per_lot <= 0:
        return 0.0

    lots = risk_usd / loss_per_lot
    # clamp, quantize et marge → identiques à ton code
    ...
    return lots


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

        # La réconciliation initiale est gérée par main.py, ce qui est la bonne approche.

    def get_positions(self, symbol: str | None = None):
        """
        Retourne les positions ouvertes (liste de dicts). Filtre par symbole si fourni.
        """

        try:
            positions = (
                mt5.positions_get(symbol=symbol) if symbol else mt5.positions_get()
            )
            out = []
            for p in positions or []:
                info = mt5.symbol_info(getattr(p, "symbol", "") or "")
                out.append(
                    {
                        "ticket": getattr(p, "ticket", None),
                        "symbol": getattr(p, "symbol", None),
                        "magic": getattr(p, "magic", None),
                        "comment": getattr(p, "comment", ""),
                        "type": getattr(p, "type", None),  # 0=BUY, 1=SELL
                        "volume": float(getattr(p, "volume", 0) or 0),
                        "price_open": float(getattr(p, "price_open", 0) or 0),
                        "sl": float(getattr(p, "sl", 0) or 0),
                        "tp": float(getattr(p, "tp", 0) or 0),
                        "point": float(getattr(info, "point", 0.0001) or 0.0001),
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

    def reconcile_state_with_broker(self) -> None:
        """
        Réconcilie l'état interne des positions avec le broker (MT5).
        - Source de vérité: broker; on fusionne pour préserver les métadonnées internes (risque, trailing, tags).
        - Reconstitue le risque initial si manquant (order_calc_profit), sinon 0.0.
        - Purge les tickets clos; met à jour last_reconciliation_time (UTC).
        - Applique un trailing si la position le demande et si apply_dynamic_trailing(...) est disponible.
        """
        from datetime import datetime, timezone

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
                mt5 = getattr(self.mt5_connector, "mt5", None) or getattr(
                    self, "mt5", None
                )
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
                mt5 = getattr(self.mt5_connector, "mt5", None) or getattr(
                    self, "mt5", None
                )
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
            self.logger.error(
                "MT5 toujours non connecté — abandon de la réconciliation."
            )
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

    def _load_settings(self):
        """
        Charge et assigne les paramètres dynamiques et les mappings MT5 depuis le ConfigManager.
        """
        self.logger.debug("Chargement des paramètres pour le TradeExecutor...")

        # Chemins et paramètres de comportement
        logs_dir = Path(self.config_manager.get("paths.logs", "logs/"))
        audit_filename = self.config_manager.get(
            "trade_executor_settings.audit_trail_file_name", "trade_audit_trail.jsonl"
        )
        self.audit_trail_path = logs_dir / audit_filename
        self.audit_trail_path.parent.mkdir(parents=True, exist_ok=True)

        self.mt5_max_retries = self.config_manager.get(
            "trade_executor_settings.mt5_max_retries", 3
        )
        self.mt5_retry_delay_seconds = self.config_manager.get(
            "trade_executor_settings.mt5_retry_delay_seconds", 2
        )

        # Mappings des constantes MT5
        self.mt5_mappings = self.config_manager.get("mt5_mappings", {})

        # Types d'ordres
        self.ORDER_TYPE_BUY = getattr(
            mt5, self.mt5_mappings.get("order_types", {}).get("BUY", "ORDER_TYPE_BUY")
        )
        self.ORDER_TYPE_SELL = getattr(
            mt5, self.mt5_mappings.get("order_types", {}).get("SELL", "ORDER_TYPE_SELL")
        )

        # --- AJOUT SANS SUPPRESSION ---
        # Constantes nécessaires pour la fonction `close_position`
        self.POSITION_TYPE_BUY = getattr(
            mt5,
            self.mt5_mappings.get("position_types", {}).get("BUY", "POSITION_TYPE_BUY"),
        )
        self.POSITION_TYPE_SELL = getattr(
            mt5,
            self.mt5_mappings.get("position_types", {}).get(
                "SELL", "POSITION_TYPE_SELL"
            ),
        )
        # --- FIN DE L'AJOUT ---

        # Actions de trading (votre bloc original est conservé intégralement)
        self.TRADE_ACTION_DEAL = getattr(
            mt5,
            self.mt5_mappings.get("trade_actions", {}).get("DEAL", "TRADE_ACTION_DEAL"),
        )
        self.TRADE_ACTION_PENDING = getattr(
            mt5,
            self.mt5_mappings.get("trade_actions", {}).get(
                "PENDING", "TRADE_ACTION_PENDING"
            ),
        )
        self.TRADE_ACTION_MODIFY = getattr(
            mt5,
            self.mt5_mappings.get("trade_actions", {}).get(
                "MODIFY", "TRADE_ACTION_MODIFY"
            ),
        )

        # Codes de retour (votre bloc original est conservé intégralement)
        self.TRADE_RETCODE_DONE = getattr(
            mt5,
            self.mt5_mappings.get("trade_retcodes", {}).get(
                "RETCODE_DONE", "TRADE_RETCODE_DONE"
            ),
        )
        self.TRADE_RETCODE_REQUOTE = getattr(
            mt5,
            self.mt5_mappings.get("trade_retcodes", {}).get(
                "RETCODE_REQUOTE", "TRADE_RETCODE_REQUOTE"
            ),
        )
        self.TRADE_RETCODE_REJECT = getattr(
            mt5,
            self.mt5_mappings.get("trade_retcodes", {}).get(
                "RETCODE_REJECT", "TRADE_RETCODE_REJECT"
            ),
        )
        self.TRADE_RETCODE_TRADE_DISABLED = getattr(
            mt5,
            self.mt5_mappings.get("trade_retcodes", {}).get(
                "RETCODE_TRADE_DISABLED", "TRADE_RETCODE_TRADE_DISABLED"
            ),
        )

        # Politiques d'exécution et de temps (votre bloc original est conservé intégralement)
        self.ORDER_TIME_GTC = getattr(
            mt5,
            self.mt5_mappings.get("order_time_flags", {}).get("GTC", "ORDER_TIME_GTC"),
        )
        self.ORDER_FILLING_FOK = getattr(
            mt5,
            self.mt5_mappings.get("order_filling_policies", {}).get(
                "FOK", "ORDER_FILLING_FOK"
            ),
        )
        self.ORDER_FILLING_IOC = getattr(
            mt5,
            self.mt5_mappings.get("order_filling_policies", {}).get(
                "IOC", "ORDER_FILLING_IOC"
            ),
        )

        # Validation des paramètres
        if not isinstance(self.mt5_max_retries, int) or self.mt5_max_retries < 0:
            self.logger.warning(
                f"Paramètre 'mt5_max_retries' invalide. Utilisation de la valeur par défaut 3."
            )
            self.mt5_max_retries = 3

        self.logger.info(
            f"Paramètres de l'exécuteur chargés. Retries MT5: {self.mt5_max_retries}."
        )

    def _log_audit_trail(self, entry: dict) -> None:
        import json
        from core.config_manager import CustomJSONEncoder  # import tardif, propre

        with self.audit_trail_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, cls=CustomJSONEncoder) + "\n")

        """
        Enregistre une entrée dans le journal d'audit de manière atomique et sécurisée.

        Args:
            entry (dict): Le dictionnaire contenant les détails de l'événement de trade.
        """
        # AMÉLIORATION : Écriture atomique et gestion de la concurrence
        lock_path = self.audit_trail_path.with_suffix(".lock")
        try:
            # 1. Verrouillage pour l'accès concurrent
            # Utiliser `Path.open` avec `x` pour créer exclusivement et éviter les deadlocks simples
            with lock_path.open("x") as lock_file:
                pass  # Le fichier de verrouillage est créé

            # 2. Écriture dans un fichier temporaire
            temp_path = self.audit_trail_path.with_suffix(".tmp")
            with open(temp_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, cls=CustomJSONEncoder) + "\n")

            # 3. Copier le contenu de l'original (s'il existe) puis ajouter la nouvelle ligne du temporaire
            # Cette approche peut être inefficace pour des très grands fichiers.
            # Une meilleure approche serait de ne pas copier l'original dans le temp,
            # mais d'utiliser un FileHandler de logging qui gère l'append,
            # ou de ne pas lire l'original si le fichier est en append-only (JSONL).
            # Pour un JSONL, `mode='a'` est suffisant.

            # Simplification: Pour JSONL (append-only), la copie de l'original est inutile.
            # Juste renommer le temp en original.

            # 4. Remplacer l'ancien fichier par le nouveau (qui est juste la nouvelle ligne)
            # Cette ligne est incorrecte pour l'approche append-only avec .tmp
            # `temp_path.rename(self.audit_trail_path)` devrait être fait si on a RE-écrit TOUT le fichier
            # Pour un JSONL, on écrit directement dans le fichier.

            # La logique la plus simple et correcte pour JSONL append-only avec verrou:
            # 1. Obtenir le verrou
            # 2. Ouvrir le fichier original en mode 'a' (append)
            # 3. Écrire la nouvelle entrée
            # 4. Relâcher le verrou

            # Re-implémentation propre de l'écriture atomique pour JSONL append-only
            with self.audit_trail_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, cls=CustomJSONEncoder) + "\n")

            self.logger.debug(
                f"Entrée du journal d'audit ajoutée : {entry.get('order_id', 'N/A')}"
            )
        except FileExistsError:  # Si le lock existe déjà
            self.logger.warning(
                f"Le fichier d'audit est déjà en cours d'écriture (lock trouvé à {lock_path}). Tentative d'écriture annulée pour éviter la corruption."
            )
            # Ne pas envoyer d'alerte critique ici, juste un avertissement
        except Exception as e:
            self.logger.error(
                f"Échec de l'écriture dans le journal d'audit {self.audit_trail_path}: {e}",
                exc_info=True,
            )
            self.config_manager.send_alert(
                "CRITIQUE",
                f"Échec de l'écriture du journal d'audit: {e}",
                alert_type="telegram_critical",
            )
        finally:
            if lock_path.exists():
                lock_path.unlink()  # Assure que le verrou est toujours relâché

        # TODO: Implémenter une rotation des journaux d'audit (par taille ou par jour) pour éviter
        #       une croissance infinie du fichier. (Ceci sera géré par ConfigManager qui appelle ici).

    def load_decision_package(self, decision_package: dict) -> dict:
        """
        Charge le package de décision sans validation.
        """
        self.logger.debug("Chargement du package de décision (aucune validation)...")

        # Aucune validation, on retourne direct le package
        return decision_package

    def _check_trading_window(
        self, current_time_utc: datetime, symbol: str
    ) -> tuple[bool, str]:
        """
        Vérifie si le trading est autorisé pour l'actif donné à l'heure actuelle.
        Gère correctement les sessions de nuit (passant par minuit).
        """
        start_hour = self.config_manager.get(
            "trade_executor_settings.trading_start_hour_utc", 7
        )
        end_hour = self.config_manager.get(
            "trade_executor_settings.trading_end_hour_utc", 20
        )
        allowed_weekdays = self.config_manager.get(
            "trade_executor_settings.allowed_weekdays", [0, 1, 2, 3, 4]
        )

        if current_time_utc.weekday() not in allowed_weekdays:
            return (
                False,
                f"Hors des jours de trading autorisés (Jour: {current_time_utc.weekday()}).",
            )

        # Session normale (ex: 07:00 -> 20:00)
        if start_hour <= end_hour:
            if not (start_hour <= current_time_utc.hour < end_hour):
                return (
                    False,
                    f"Hors de la fenêtre de trading (Heure UTC: {current_time_utc.hour}).",
                )
        # Session de nuit (ex: 22:00 -> 07:00)
        else:
            if not (
                current_time_utc.hour >= start_hour or current_time_utc.hour < end_hour
            ):
                return (
                    False,
                    f"Hors de la fenêtre de trading de nuit (Heure UTC: {current_time_utc.hour}).",
                )

        return True, "Dans la fenêtre de trading."

    def _check_spread(self, symbol: str, active_config: dict) -> tuple[bool, str]:
        """
        Vérifie le spread en utilisant une double couche de sécurité :
        1. Une limite absolue (garde-fou) pour ne jamais dépasser un seuil maximal.
        2. Une limite dynamique pour détecter les élargissements anormaux par rapport à la moyenne.

        Args:
            symbol (str): Le symbole de l'instrument.
            active_config (dict): La configuration active.

        Returns:
            tuple[bool, str]: True si le spread est acceptable, False sinon, avec une raison.
        """
        try:
            info = self.mt5_connector.get_symbol_info(symbol)
            if info is None:
                return (
                    False,
                    f"Impossible de récupérer les infos du symbole pour {symbol}.",
                )
            current_spread = info.spread
        except Exception as e:
            self.logger.warning(
                f"Échec de l'obtention du spread pour '{symbol}': {e}", exc_info=True
            )
            return False, f"Impossible d'obtenir le spread pour {symbol}."

        # --- Vérification 1: Limite Absolue (Garde-fou) ---
        max_allowed_spread_points = self.config_manager.get(
            "trade_executor_settings.max_allowed_spread_points", 50
        )
        if current_spread > max_allowed_spread_points:
            return (
                False,
                f"Spread ({current_spread}) dépasse la limite absolue ({max_allowed_spread_points}).",
            )

        # --- AMÉLIORATION : Vérification 2: Limite Dynamique (Intelligente) ---
        smart_check_settings = self.config_manager.get(
            "trade_executor_settings.smart_spread_check", {}
        )
        if smart_check_settings.get("enabled", True):
            # Utilise le spread moyen de la session (high+low)/2 comme référence
            # C'est une mesure plus fiable que de tenter de calculer une moyenne mobile sur les ticks.
            spread_high = info.spread_high
            spread_low = info.spread_low

            # S'assurer que le broker fournit ces informations
            if spread_high > 0 and spread_low > 0:
                average_session_spread = (spread_high + spread_low) / 2
                multiplier = smart_check_settings.get("max_multiplier", 2.5)
                dynamic_limit = average_session_spread * multiplier

                if current_spread > dynamic_limit:
                    return (
                        False,
                        f"Spread ({current_spread}) est anormalement élevé par rapport à la moyenne de la session ({average_session_spread:.1f}). Limite dynamique: {dynamic_limit:.1f}.",
                    )

        return True, "Spread acceptable."

    def _check_portfolio_exposure(
        self, active_config: dict, current_context: dict, trade_decision: dict
    ) -> tuple[bool, str]:
        """
        Vérifie si un nouveau trade dépasserait les limites de risque du portefeuille
        en calculant le RISQUE RÉEL TOTAL (pas la valeur notionnelle).
        """
        # 1. Vérification du nombre de positions (inchangée, c'est une bonne sécurité)
        max_positions = self.config_manager.get("global_safety.max_open_positions", 5)
        if len(self._open_positions) >= max_positions:
            return (
                False,
                f"Nombre max de positions ouvertes atteint ({len(self._open_positions)}/{max_positions}).",
            )

        # 2. Calcul du risque total engagé
        account_equity = current_context.get("account_info", {}).get("equity", 0.0)
        if account_equity <= 0:
            return False, "Équité du compte invalide."

        # Calcul du risque pour le NOUVEAU trade proposé
        risk_percent_new_trade = active_config.get("risk_per_trade_percent", 1.0)
        risk_usd_new_trade = account_equity * (risk_percent_new_trade / 100.0)

        # Calcul du risque pour les trades DÉJÀ ouverts
        # NOTE : La méthode la plus précise est de recalculer le risque pour chaque position.
        # Une optimisation future serait de stocker le risque initial lors de l'ouverture.
        total_risk_usd_existing_trades = 0.0
        for ticket, pos in self._open_positions.items():
            # Utilise l'API pour un calcul précis du risque si le SL était touché
            _ret, loss_value = self.mt5_connector.mt5.order_calc_profit(
                self.ORDER_TYPE_BUY if pos["type"] == 0 else self.ORDER_TYPE_SELL,
                pos["symbol"],
                pos["volume"],
                pos["entry_price"],
                pos["sl"],
            )
            if _ret == self.TRADE_RETCODE_DONE and loss_value is not None:
                total_risk_usd_existing_trades += abs(loss_value)
            else:
                self.logger.warning(
                    f"Impossible de calculer le risque pour la position existante #{ticket}."
                )

        # 3. Vérification de la limite de risque globale
        total_potential_risk = total_risk_usd_existing_trades + risk_usd_new_trade
        max_total_risk_percent = self.config_manager.get(
            "global_safety.max_total_risk_percent", 10.0
        )
        max_allowed_total_risk_usd = account_equity * (max_total_risk_percent / 100.0)

        if total_potential_risk > max_allowed_total_risk_usd:
            reason = f"Risque total ({total_potential_risk:.2f}$) dépasserait la limite max ({max_allowed_total_risk_usd:.2f}$)."
            return False, reason

        self.logger.debug(
            f"Risque total du portefeuille après le trade: {total_potential_risk:.2f}$ (Limite: {max_allowed_total_risk_usd:.2f}$)"
        )
        return True, "Exposition du portefeuille acceptable."

    def pre_trade_checks(
        self,
        trade_decision: dict,
        active_config: dict,
        market_context: dict,
    ) -> tuple[bool, str]:
        """
        Pré-checks d’exécution (neutre, sans gating stratégique).
        Ne conserve que les garde-fous indispensables pour éviter des ordres invalides.
        Garde-fous :
        - action & symbole valides + whitelist
        - mapping broker + connexion MT5 + symbole MT5 valide & sélectionné (MarketWatch)
        - limite max de positions ouvertes (globale et optionnellement par symbole)
        - fenêtre horaire & jours autorisés (si configurés)
        - prix courant disponible (côté action)
        - cohérence SL minimal vs stops_level broker (et cap SL scalping si activé)
        """
        # --- import DIAG (neutre si absent) ---
        try:
            from core.diagnostics import get_tracker_from_context
        except Exception:
            get_tracker_from_context = None

        # --- Helpers ---
        def _first_non_empty(*vals):
            for v in vals:
                if isinstance(v, str) and v.strip():
                    return v.strip()
            return None

        def _normalize_action(a: str) -> str:
            a = (a or "").strip().upper()
            return {
                "BUY": "BUY",
                "SELL": "SELL",
                "LONG": "BUY",
                "SHORT": "SELL",
                "CLOSE": "CLOSE",
            }.get(a, "")

        def _mt5_is_connected() -> bool:
            attr = getattr(self.mt5_connector, "is_connected", None)
            try:
                return bool(attr()) if callable(attr) else bool(attr)
            except Exception:
                return False

        def _mt5_reconnect_if_needed():
            recon = getattr(self.mt5_connector, "reconnect_if_needed", None)
            if callable(recon):
                try:
                    recon()
                except Exception:
                    pass

        def _select_symbol_if_needed(sym: str) -> bool:
            try:
                sel = getattr(self.mt5_connector, "ensure_symbol_selected", None)
                if callable(sel):
                    return bool(sel(sym))
                # fallback basique si pas d’API dédiée
                info = self.mt5_connector.get_symbol_info(sym)
                if info and getattr(info, "visible", True):
                    return True
                subscribe = getattr(self.mt5_connector, "symbol_select", None)
                return bool(subscribe(sym, True)) if callable(subscribe) else True
            except Exception:
                return False

        def _diag(reason: str, extra: dict | None = None, sym: str | None = None):
            try:
                if get_tracker_from_context:
                    s = sym or trade_decision.get("asset") or "UNKNOWN"
                    get_tracker_from_context(market_context).note(
                        s, "pre_trade", reason, extra or {}
                    )
            except Exception:
                pass

        def _reject(
            reason: str, extra: dict | None = None, sym: str | None = None
        ) -> tuple[bool, str]:
            _diag(reason, extra, sym)
            return False, reason

        # 1) Action & symbole (normalisés)
        action_raw = _first_non_empty(
            trade_decision.get("final_action"),
            trade_decision.get("selected_action"),
            trade_decision.get("core_action"),
            trade_decision.get("action"),
            trade_decision.get("side"),
            trade_decision.get("direction"),
        )
        action = _normalize_action(action_raw)
        if not action:
            return _reject(f"invalid_action:{action_raw or 'EMPTY'}")

        raw_symbol = _first_non_empty(
            trade_decision.get("asset"),
            trade_decision.get("symbol"),
            trade_decision.get("instrument"),
        )
        if not raw_symbol or raw_symbol.strip().upper() == "UNKNOWN":
            return _reject("asset_missing_or_unknown")
        raw_symbol = raw_symbol.strip().upper()

        # 2) Whitelist (si fournie)
        allowed = set(map(str.upper, (active_config or {}).get("tradeable_assets", [])))
        if allowed and raw_symbol not in allowed:
            return _reject(f"asset_not_allowed:{raw_symbol}", sym=raw_symbol)

        # 3) Mapping broker (unique & cohérent : compte actif)
        broker_symbol = self._map_symbol_for_broker(raw_symbol, market_context)
        if not broker_symbol or broker_symbol.strip().upper() == "UNKNOWN":
            return _reject(f"invalid_broker_mapping:{raw_symbol}", sym=raw_symbol)
        broker_symbol = broker_symbol.strip().upper()

        # 4) Connexion MT5
        if not _mt5_is_connected():
            _mt5_reconnect_if_needed()
            if not _mt5_is_connected():
                return _reject("mt5_not_connected", sym=raw_symbol)

        # 5) Symbole MT5 valide & sélectionné
        symbol_info = self.mt5_connector.get_symbol_info(broker_symbol)
        if not symbol_info or not getattr(symbol_info, "name", None):
            return _reject(f"invalid_mt5_symbol:{broker_symbol}", sym=raw_symbol)
        if not _select_symbol_if_needed(broker_symbol):
            return _reject(f"symbol_not_selected:{broker_symbol}", sym=raw_symbol)

        # ---------- 5bis) Cooldown after exit (hard skip) ----------

        try:
            import time as _t

            cooldown_map = getattr(self, "_cooldown_until", {}) or {}
            until = cooldown_map.get(broker_symbol.upper())
            if until and _t.time() < until:
                remain = int(until - _t.time())
                reason = f"cooldown_after_exit_active:{remain}s"
                # Logging interne seulement (pas de feedback_pipeline ici)
                try:
                    self.logger.info(
                        f"[COOLDOWN] {broker_symbol} encore bloqué {remain}s → skip entrée."
                    )
                except Exception:
                    pass
                return _reject(reason, sym=raw_symbol)
        except Exception as _e:
            # Non-bloquant: on loggue si possible et on continue
            try:
                self.logger.warning(f"[COOLDOWN] check KO: {_e}")
            except Exception:
                pass

        # 6) Fenêtre/Calendrier de trading (hard block si configuré)
        try:
            tes = self.config_manager.get("trade_executor_settings", {}) or {}
            start_h = int(tes.get("trading_start_hour_utc", 0))
            end_h = int(tes.get("trading_end_hour_utc", 24))
            allowed_wd = set(tes.get("allowed_weekdays", list(range(7))))
        except Exception:
            start_h, end_h, allowed_wd = 0, 24, set(range(7))
        from datetime import datetime, timezone

        now_utc = datetime.now(timezone.utc)
        # Bypass de la fenêtre si action CLOSE (on autorise la sortie de risque)
        if action == "CLOSE":
            pass  # on ne bloque jamais une sortie
        else:
            if now_utc.weekday() not in allowed_wd:
                return _reject(
                    f"trading_day_not_allowed:weekday={now_utc.weekday()}",
                    sym=raw_symbol,
                )
            if not (start_h <= now_utc.hour < end_h):
                return _reject(
                    f"trading_time_blocked:{start_h:02d}-{end_h:02d}Z", sym=raw_symbol
                )

        if now_utc.weekday() not in allowed_wd:
            return _reject(
                f"trading_day_not_allowed:weekday={now_utc.weekday()}", sym=raw_symbol
            )
        if not (start_h <= now_utc.hour < end_h):
            return _reject(
                f"trading_time_blocked:{start_h:02d}-{end_h:02d}Z", sym=raw_symbol
            )

        # 7) Limites de positions (globale & par symbole, si configuré)
        active_acc = (market_context or {}).get("active_broker_account", {}) or {}
        max_pos_global = active_acc.get("trade_settings", {}).get(
            "max_open_positions", 999
        )
        current_positions = (market_context or {}).get("open_positions", []) or []
        if (
            isinstance(current_positions, (list, tuple))
            and len(current_positions) >= max_pos_global
        ):
            return _reject(
                f"max_positions_reached:{len(current_positions)}/{max_pos_global}",
                sym=raw_symbol,
            )

        max_pos_per_symbol = active_acc.get("trade_settings", {}).get(
            "max_open_positions_per_symbol"
        )
        if isinstance(max_pos_per_symbol, (int, float)):
            by_sym = sum(
                1
                for p in current_positions
                if str(p.get("symbol", "")).upper() == broker_symbol
            )
            if by_sym >= int(max_pos_per_symbol):
                return _reject(
                    f"max_positions_symbol_reached:{broker_symbol}:{by_sym}/{int(max_pos_per_symbol)}",
                    sym=raw_symbol,
                )

        # 8) Prix courant disponible (côté logiquement consommé par l’action)
        price = self.mt5_connector.get_current_price(broker_symbol, action)
        if not price or price <= 0:
            return _reject("price_unavailable", sym=raw_symbol)

        # 9) (Info) Spread points — NON BLOQUANT (diag)
        try:
            exec_policy = (active_config or {}).get("execution_policy", {}) or {}
            max_spread_points = exec_policy.get("max_spread_points")
            if (
                hasattr(symbol_info, "spread")
                and hasattr(symbol_info, "point")
                and isinstance(max_spread_points, (int, float))
            ):
                _diag(
                    "spread_points_info",
                    {
                        "spread": float(symbol_info.spread),
                        "limit": float(max_spread_points),
                    },
                    raw_symbol,
                )
        except Exception:
            pass

        # 10) SL & stops_level broker (sécurité minimale)
        target_sl_pips = float(trade_decision.get("target_sl_pips", 0) or 0.0)
        is_scalping = "scalping" in str(trade_decision.get("strategy_type", "")).lower()

        # Cap SL scalping optionnel (reject si configuré ainsi)
        sl_cap = float(
            self.config_manager.get("entry_rules.scalping.max_stop_pips_scalp", 0.0)
            or 0.0
        )
        reject_over_cap = bool(
            self.config_manager.get("entry_rules.scalping.reject_if_sl_over_cap", False)
        )
        if is_scalping and sl_cap > 0 and target_sl_pips > sl_cap and reject_over_cap:
            return _reject(
                f"sl_over_cap({target_sl_pips:.2f}>{sl_cap:.2f})",
                {"sl_pips": target_sl_pips, "cap": sl_cap},
                raw_symbol,
            )

        # Stops level broker -> pips (gère trade_stops_level vs stops_level)
        try:
            digits = int(getattr(symbol_info, "digits", 5) or 5)
            points_per_pip = 10.0 if digits in (3, 5) else 1.0
        except Exception:
            points_per_pip = 10.0
        stops_level_points = float(
            getattr(
                symbol_info, "trade_stops_level", getattr(symbol_info, "stops_level", 0)
            )
            or 0
        )
        stops_level_pips = (
            (stops_level_points / points_per_pip) if points_per_pip > 0 else 0.0
        )

        if is_scalping and target_sl_pips > 0 and stops_level_pips > target_sl_pips:
            return _reject(
                "stops_level_too_high_for_scalp",
                {"stops_level_pips": stops_level_pips, "sl_pips": target_sl_pips},
                raw_symbol,
            )

        # ✅ OK pour exécution
        return True, ""

    def _check_fat_finger_volume(
        self, trade_decision: dict, market_context: dict
    ) -> tuple[bool, str]:
        """
        Vérifie si le volume calculé est anormalement élevé (fat-finger check) en utilisant
        une limite absolue et une limite dynamique par rapport à la moyenne récente.
        """
        symbol = trade_decision.get("asset")
        proposed_volume = trade_decision.get("volume", 0.0)

        settings = self.config_manager.get(
            "trade_executor_settings.fat_finger_check", {}
        )

        # --- Vérification 1: Limite Absolue (Garde-fou) ---
        max_absolute_volume = settings.get("max_absolute_volume_for_asset", {})
        if (
            symbol in max_absolute_volume
            and proposed_volume > max_absolute_volume[symbol]
        ):
            return (
                False,
                f"Volume ({proposed_volume:.2f}) dépasse le seuil absolu ({max_absolute_volume[symbol]:.2f}) pour {symbol}.",
            )

        # --- AMÉLIORATION : Vérification 2: Limite Dynamique (Intelligente) ---
        if settings.get("enable_dynamic_check", True):
            # Accéder de manière robuste au DataFrame d'analyse
            market_data_for_asset = market_context.get("market_data", {}).get(
                symbol, {}
            )
            rates_df = market_data_for_asset.get("annotated_rates_df")

            if rates_df is None or "tick_volume" not in rates_df.columns:
                self.logger.warning(
                    f"Données de volume manquantes pour {symbol}. Vérification dynamique du volume ignorée."
                )
                return True, "Volume acceptable (vérification dynamique ignorée)."

            lookback = settings.get("avg_volume_lookback", 20)
            if len(rates_df) >= lookback:
                # Calcul optimisé de la moyenne avec pandas
                avg_recent_volume = (
                    rates_df["tick_volume"].rolling(window=lookback).mean().iloc[-1]
                )

                # Éviter la division par zéro si le volume moyen est nul
                if avg_recent_volume > 1e-9:
                    multiplier = settings.get("max_volume_multiplier_from_avg", 5.0)
                    dynamic_limit = avg_recent_volume * multiplier

                    if proposed_volume > dynamic_limit:
                        reason = (
                            f"Volume ({proposed_volume:.2f}) est > {multiplier}x la moyenne récente "
                            f"({avg_recent_volume:.2f}). Limite dynamique: {dynamic_limit:.2f}."
                        )
                        return False, reason

        return True, "Volume de trade acceptable (fat-finger check)."

    # --- Helpers robustes ---

    def _map_symbol_for_broker(self, raw_symbol: str, market_context: dict) -> str:
        """
        Retourne le symbole broker à partir d'un mapping éventuel.
        Ne renvoie JAMAIS 'UNKNOWN' : si pas de mapping, garde raw_symbol.
        """
        symbol_map = (
            market_context.get("active_broker_account", {}).get("symbol_map", {}) or {}
        )
        broker_symbol = symbol_map.get(
            raw_symbol, raw_symbol
        )  # ✅ fallback = symbole d'origine
        if broker_symbol != raw_symbol:
            self.logger.info(f"[SYMBOL MAP] {raw_symbol} -> {broker_symbol}")
        else:
            self.logger.warning(
                f"[SYMBOL MAP] Pas de mapping pour {raw_symbol}, utilisation telle quelle."
            )
        return broker_symbol

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

    def prepare_order(self, decision_package: dict) -> dict:
        """
        Calcule et prépare la demande d'ordre complète pour MetaTrader 5.
        Zéro tolérance aux valeurs 'UNKNOWN' : on normalise et on valide
        avant toute requête MT5.

        ✅ Décision unique du volume
        - Le volume est TOUJOURS calculé via `_calculate_risk_based_volume(...)`.
        Tout volume présent dans la décision est ignoré.
        """

        # Fallback local si l'exception n'existe pas au module
        try:
            TradeExecutionError
        except NameError:

            class TradeExecutionError(Exception):
                """Raised when order preparation fails."""

                pass

        self.logger.info("Préparation de l'ordre MT5...")

        # --- Unpack sûrs pour éviter les UnboundLocalError ---
        trade_decision = (decision_package or {}).get("trade_decision", {}) or {}
        market_context = (decision_package or {}).get("market_context", {}) or {}
        active_config = (decision_package or {}).get("active_config", {}) or {}

        # --- Raccourcis locaux (laisse ta redondance, inoffensive) ---
        trade_decision = decision_package.get("trade_decision", {}) or {}
        active_config = (
            decision_package.get("active_config")
            or decision_package.get("config_used")
            or {}
        )
        market_context = decision_package.get("market_context", {}) or {}

        # ---------- Helpers internes ----------
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

        def _normalize_volume(symbol_info, vol: float) -> float:
            """
            Clamp & round le volume selon les contraintes du symbole MT5.
            Ne modifie pas la précision naturelle du broker (évite l'arrondi 2 décimales forcé).
            """
            try:
                vmin = float(getattr(symbol_info, "volume_min", 0.01) or 0.01)
                vmax = float(getattr(symbol_info, "volume_max", 100.0) or 100.0)
                vstep = float(getattr(symbol_info, "volume_step", 0.01) or 0.01)
            except Exception:
                vmin, vmax, vstep = 0.01, 100.0, 0.01

            if not isinstance(vol, (int, float)) or vol <= 0:
                # 🔴 Aucun fallback arbitraire — on refuse un volume invalide
                raise TradeExecutionError(
                    f"Volume invalide pour normalisation ({vol})."
                )

            # Clamp dans les bornes broker
            vol = max(vmin, min(vmax, float(vol)))

            # 🔧 Arrondi propre au pas broker
            if vstep > 0:
                steps = round((vol - vmin) / vstep)
                vol = vmin + steps * vstep
                if vol > vmax:
                    vol = vmax

            # 🔒 Sécurité plancher
            if vol < vmin:
                vol = vmin

            return round(
                vol, 8
            )  # précision suffisante sans écraser la granularité broker

        # ---------- 1) Action ----------
        action_raw = _first_non_empty(
            trade_decision.get("final_action"),
            trade_decision.get("selected_action"),
            trade_decision.get("core_action"),
            trade_decision.get("action"),
            trade_decision.get("side"),
            trade_decision.get("direction"),
        )
        action = _normalize_action(action_raw)

        if not action:
            msg = f"Action de trade invalide: '{action_raw}' (attendu: BUY/SELL/CLOSE/LONG/SHORT)."
            self.logger.error(msg)
            raise TradeExecutionError(msg)

        # ---------- 2) Asset ----------
        raw_symbol = _first_non_empty(
            trade_decision.get("asset"),
            trade_decision.get("symbol"),
            trade_decision.get("instrument"),
        )
        if not raw_symbol or raw_symbol.upper() == "UNKNOWN":
            msg = "Asset/symbole manquant ou 'UNKNOWN' dans la décision."
            self.logger.error(msg)
            raise TradeExecutionError(msg)

        raw_symbol = raw_symbol.upper()

        allowed = set(map(str.upper, active_config.get("tradeable_assets", [])))
        if allowed and raw_symbol not in allowed:
            msg = f"Asset '{raw_symbol}' non autorisé par la stratégie (whitelist: {sorted(allowed)})."
            self.logger.error(msg)
            raise TradeExecutionError(msg)

        # ---------- 3) Mapping broker ----------
        # 1) mapping de compte (prioritaire)  2) mapping global  3) défaut = raw
        broker_map_acct = (
            market_context.get("active_broker_account", {}).get("symbol_map") or {}
        )
        broker_map_global = self.config_manager.get("asset_symbol_mapping", {}) or {}
        broker_symbol = (
            str(
                broker_map_acct.get(
                    raw_symbol, broker_map_global.get(raw_symbol, raw_symbol)
                )
            )
            .strip()
            .upper()
        )

        if not broker_symbol or broker_symbol == "UNKNOWN":
            msg = f"Mapping broker invalide pour l'asset '{raw_symbol}' (résultat: '{broker_symbol}')."
            self.logger.error(msg)
            raise TradeExecutionError(msg)

        # ---------- 3bis) Fenêtre/Calendrier de trading (hard block) ----------
        try:
            tes = self.config_manager.get("trade_executor_settings", {}) or {}
            start_h = int(tes.get("trading_start_hour_utc", 0))
            end_h = int(tes.get("trading_end_hour_utc", 24))
            allowed_wd = set(tes.get("allowed_weekdays", list(range(7))))
        except Exception:
            start_h, end_h, allowed_wd = 0, 24, set(range(7))

        now_utc = datetime.utcnow()
        if now_utc.weekday() not in allowed_wd:
            raise TradeExecutionError(
                f"Jour non autorisé pour trader (weekday={now_utc.weekday()})."
            )
        if not (start_h <= now_utc.hour < end_h):
            raise TradeExecutionError(
                f"Hors fenêtre horaire UTC ({start_h:02d}-{end_h:02d})."
            )

        # ---------- [BURST GUARDRAILS] ----------
        try:
            rule_name = str(trade_decision.get("rule_name", "")).lower()
            if rule_name == "burst_scalping":
                burst_cfg = (
                    active_config.get("entry_rules", {})
                    .get("scalping", {})
                    .get("burst_scalping", {})
                ) or {}
                guard_cfg = burst_cfg.get("burst_guardrails", {}) or {}
                max_open_positions = int(guard_cfg.get("max_open_positions", 5))
                cooldown_seconds = int(guard_cfg.get("cooldown_seconds", 90))
                enforce_closure = bool(guard_cfg.get("enforce_burst_closure", True))
                single_burst_global = bool(guard_cfg.get("single_burst_global", True))

                import re, time

                def _field(obj, key, default=None):
                    if isinstance(obj, dict):
                        return obj.get(key, default)
                    return getattr(obj, key, default)

                # 1) Récup selon le scope
                if single_burst_global:
                    all_open = self.mt5_connector.get_positions() or []
                    scope_lbl = "global"
                else:
                    all_open = (
                        self.mt5_connector.get_positions(symbol=broker_symbol) or []
                    )
                    scope_lbl = broker_symbol

                # 2) Détection des paniers burst via 'burst_scalping|basket='
                open_burst_ids = set()
                for p in all_open:
                    c = str(_field(p, "comment", "") or "")
                    m = re.search(r"burst_scalping\|basket=([A-Za-z0-9_]+)", c)
                    if m:
                        open_burst_ids.add(m.group(1))

                now_ts = time.time()

                # 3) Interdiction si un panier existe déjà
                if enforce_closure and len(open_burst_ids) > 0:
                    raise TradeExecutionError(
                        f"⛔ Burst guard ({scope_lbl}): panier(s) en cours = {', '.join(sorted(open_burst_ids))} → interdit d’en démarrer un nouveau."
                    )

                # 4) Cooldown anti-burst rapproché
                last_burst_time = getattr(self, "_last_burst_time", 0)
                if (
                    cooldown_seconds > 0
                    and (now_ts - last_burst_time) < cooldown_seconds
                ):
                    raise TradeExecutionError(
                        f"⏳ Cooldown actif ({now_ts - last_burst_time:.1f}s < {cooldown_seconds}s)."
                    )

                # 5) max_open_positions (info only ici)
                self.logger.info(
                    f"[BURST GUARD] OK pour démarrer (scope={scope_lbl}, aucun panier actif, cooldown OK)."
                )

        except TradeExecutionError:
            raise
        except Exception as e:
            self.logger.warning(f"[BURST GUARD] Vérification partielle échouée: {e}")

        # ---------- 4) Cas CLOSE ----------
        if action == "CLOSE":
            return {
                "action": "CLOSE",
                "symbol": broker_symbol,
                "order_id": trade_decision.get("order_id", str(uuid.uuid4())),
                "ticket_to_close": trade_decision.get("ticket_to_close"),
            }

        # ---------- 5) order_type sécurisé ----------
        order_type = str(trade_decision.get("order_type", "MARKET")).upper()
        allowed_order_types = {
            "MARKET",
            "BUY_LIMIT",
            "SELL_LIMIT",
            "BUY_STOP",
            "SELL_STOP",
        }
        if order_type not in allowed_order_types:
            self.logger.debug(f"order_type inconnu '{order_type}', fallback 'MARKET'.")
            order_type = "MARKET"

        try:
            # ---------- 6) Résolution + infos symbole ----------
            resolved_symbol = self.mt5_connector.resolve_broker_symbol(broker_symbol)
            if not resolved_symbol:
                msg = (
                    f"Symbole MT5 introuvable pour '{broker_symbol}'. "
                    f"Vérifie la correspondance broker / Market Watch."
                )
                self.logger.error(msg)
                raise TradeExecutionError(msg)

            symbol_info = self.mt5_connector.get_symbol_info(resolved_symbol)
            if not symbol_info:
                msg = (
                    f"Symbole MT5 invalide ou introuvable ({resolved_symbol}). "
                    f"Vérifie la correspondance broker."
                )
                self.logger.error(msg)
                raise TradeExecutionError(msg)

            # ⚠️ À partir d’ici on travaille UNIQUEMENT avec resolved_symbol
            broker_symbol = resolved_symbol

            # log contraintes volume broker
            try:
                self.logger.info(
                    f"[VOLUME] constraints broker {broker_symbol}: "
                    f"min={getattr(symbol_info,'volume_min',None)}, "
                    f"step={getattr(symbol_info,'volume_step',None)}, "
                    f"max={getattr(symbol_info,'volume_max',None)}"
                )
            except Exception:
                pass

            # ---------- 6bis) Spread guard (en pips) ----------
            try:
                spread_pips = float(self.mt5_connector.get_spread_pips(broker_symbol))
            except Exception:
                spread_pips = float("inf")

            entry_rules = (active_config.get("entry_rules") or {}).get("scalping") or {}
            cap_soft = entry_rules.get("max_spread_pips")
            cap_hard = entry_rules.get("hard_max_spread_pips")

            max_spread_cap = None
            if isinstance(cap_soft, (int, float)):
                max_spread_cap = float(cap_soft)
            if isinstance(cap_hard, (int, float)):
                max_spread_cap = (
                    min(max_spread_cap, float(cap_hard))
                    if max_spread_cap is not None
                    else float(cap_hard)
                )

            if max_spread_cap is not None:
                if not math.isfinite(spread_pips) or spread_pips > max_spread_cap:
                    raise TradeExecutionError(
                        f"Spread trop élevé: {spread_pips:.3f} pips > cap {max_spread_cap:.3f} pips."
                    )

            # ---------- 7) Prix d'entrée ----------
            entry_price_market = self.mt5_connector.get_current_price(
                broker_symbol, action
            )
            if not entry_price_market or entry_price_market <= 0:
                raise TradeExecutionError(
                    f"Impossible de récupérer un prix de marché valide pour {broker_symbol}."
                )

            entry_price_hint = trade_decision.get("entry_price")
            trigger_price = trade_decision.get("trigger_price")
            if trigger_price is None:
                if (
                    order_type != "MARKET"
                    and isinstance(entry_price_hint, (int, float))
                    and entry_price_hint > 0
                ):
                    trigger_price = entry_price_hint
                else:
                    trigger_price = entry_price_market

            # ---------- 7bis) Sécurisation SL/TP pour Burst ----------
            rule_name_local = str(trade_decision.get("rule_name", "")).lower()

            if rule_name_local == "burst_scalping":
                # Burst : SL uniquement si défini, jamais de TP (trailing géré ailleurs)
                sl_price = float(trade_decision.get("sl_price", 0.0) or 0.0)
                tp_price = None
            else:
                # Autres stratégies → calcul normal
                sl_price, tp_price = self._calculate_sl_tp_prices(
                    trade_decision,
                    active_config,
                    symbol_info,
                    entry_price_market,
                    market_context,
                )

            # ---------- 8a) Sécurité broker & normalisation prix ----------
            try:
                import math  # ok si déjà importé

                point = float(getattr(symbol_info, "point", 0.0001) or 0.0001)
                tick = float(getattr(symbol_info, "trade_tick_size", point) or point)
                digits = int(
                    getattr(symbol_info, "digits", max(0, round(-math.log10(point))))
                )

                # MetaTrader: stops_level / freeze_level en "points"
                stops_level_pts = int(getattr(symbol_info, "stops_level", 0) or 0)
                freeze_level_pts = int(getattr(symbol_info, "freeze_level", 0) or 0)
                broker_min = max(stops_level_pts, freeze_level_pts) * point  # en prix

                # Param config: distance min en pips
                cfg_min_pips = None
                try:
                    cfg_min_pips = (active_config.get("execution", {}) or {}).get(
                        "min_sl_tp_distance_pips", None
                    ) or (active_config.get("scalping", {}) or {}).get(
                        "min_sl_tp_distance_pips", None
                    )
                except Exception:
                    cfg_min_pips = None

                pip_size = 10.0 * point
                cfg_min_price = (
                    float(cfg_min_pips) * pip_size
                    if isinstance(cfg_min_pips, (int, float))
                    else 0.0
                )

                # Gap minimal final en prix
                min_gap_price = max(3.0 * point, broker_min, cfg_min_price)

                def _ceil_to_tick(x: float) -> float:
                    return round(math.ceil(x / tick) * tick, digits)

                def _floor_to_tick(x: float) -> float:
                    return round(math.floor(x / tick) * tick, digits)

                # Ajustements selon le type d'ordre
                has_tp = (tp_price is not None) and (
                    rule_name_local != "burst_scalping"
                )

                if action == "BUY":
                    # SL en-dessous, TP au-dessus (si TP existe)
                    if (entry_price_market - sl_price) < min_gap_price:
                        sl_price = entry_price_market - min_gap_price
                    if has_tp and (tp_price - entry_price_market) < min_gap_price:
                        tp_price = entry_price_market + min_gap_price

                    # Arrondi à la grille
                    sl_price = _floor_to_tick(sl_price)
                    if has_tp:
                        tp_price = _ceil_to_tick(tp_price)

                    # Cohérence finale (sans TP en burst)
                    if not (sl_price < entry_price_market):
                        sl_price = _floor_to_tick(entry_price_market - min_gap_price)
                    if has_tp and not (entry_price_market < tp_price):
                        tp_price = _ceil_to_tick(entry_price_market + min_gap_price)

                elif action == "SELL":
                    # SL au-dessus, TP en-dessous (si TP existe)
                    if (sl_price - entry_price_market) < min_gap_price:
                        sl_price = entry_price_market + min_gap_price
                    if has_tp and (entry_price_market - tp_price) < min_gap_price:
                        tp_price = entry_price_market - min_gap_price

                    # Arrondi à la grille
                    sl_price = _ceil_to_tick(sl_price)
                    if has_tp:
                        tp_price = _floor_to_tick(tp_price)

                    # Cohérence finale (sans TP en burst)
                    if not (entry_price_market < sl_price):
                        sl_price = _ceil_to_tick(entry_price_market + min_gap_price)
                    if has_tp and not (tp_price < entry_price_market):
                        tp_price = _floor_to_tick(entry_price_market - min_gap_price)

                def _fmt(v):
                    return (
                        f"{float(v):.{digits}f}"
                        if isinstance(v, (int, float))
                        else "None"
                    )

                self.logger.info(
                    "[SAFETY] SL/TP normalisés | symbol=%s, entry=%s, SL=%s, TP=%s, min_gap=%s, stops_level=%s, freeze_level=%s",
                    broker_symbol,
                    _fmt(entry_price_market),
                    _fmt(sl_price),
                    _fmt(tp_price),
                    _fmt(min_gap_price),
                    str(stops_level_pts),
                    str(freeze_level_pts),
                )

            except Exception as e:
                self.logger.warning(f"[SAFETY] Normalisation SL/TP échouée: {e}")

            # ---------- 8bis) RR minimum (SOFT permissif) ----------
            try:
                min_rr = float(
                    self.config_manager.get("risk_management.min_rr", 0) or 0.0
                )
            except Exception:
                min_rr = 0.0

            rr_value = None

            # ✅ Appliquer le check RR seulement si la stratégie a un TP (ex: Liquidity)
            if rule_name_local != "burst_scalping":
                if min_rr > 0.0 and tp_price is not None:
                    if action == "BUY":
                        risk = max(entry_price_market - sl_price, 0.0)
                        reward = max(tp_price - entry_price_market, 0.0)
                    else:  # SELL
                        risk = max(sl_price - entry_price_market, 0.0)
                        reward = max(entry_price_market - tp_price, 0.0)

                    rr_value = (reward / risk) if risk > 0 else 0.0

                    if risk <= 0.0 or reward <= 0.0:
                        self.logger.warning(
                            f"⚠️ RR invalide (risk={risk:.6f}, reward={reward:.6f}) → accepté en mode permissif."
                        )
                    elif rr_value < min_rr:
                        self.logger.info(
                            f"ℹ️ RR insuffisant {rr_value:.2f} < min {min_rr:.2f} → accepté en mode permissif."
                        )

            # ---------- 9) Volume (calcul unique via risk-based sizing) ----------
            account_trade_settings = (
                market_context.get("active_broker_account", {}).get(
                    "trade_settings", {}
                )
                or {}
            )

            # ✅ Prérequis : risque défini et SL valide
            risk_pct = float(
                account_trade_settings.get("risk_per_trade_percent", 0.0) or 0.0
            )
            if risk_pct <= 0 or not sl_price or sl_price <= 0:
                raise TradeExecutionError(
                    f"Risk sizing impossible: risk%={risk_pct}, sl_price={sl_price}"
                )

            # ✅ Calcul unique via _calculate_risk_based_volume
            volume_final = float(
                self._calculate_risk_based_volume(
                    {
                        "action": action,
                        "asset": broker_symbol,
                        "order_type": order_type,
                        "confidence": trade_decision.get("confidence", 1.0),
                        "rule_name": trade_decision.get("rule_name"),
                        "volatility_factor": trade_decision.get("volatility_factor"),
                    },
                    active_config,
                    market_context,
                    symbol_info,
                    entry_price_market,
                    sl_price,
                    account_trade_settings,
                )
            )
            self.logger.info(
                f"[VOLUME] Calcul risk-based réussi: risk%={risk_pct}, vol={volume_final:.4f}"
            )

            # ---------- 9a) Normalisation par contraintes symbole ----------
            vol_before_norm = volume_final
            volume_final = _normalize_volume(symbol_info, volume_final)
            self.logger.info(
                f"[VOLUME] normalisation symbole: avant={vol_before_norm} → après={volume_final} "
                f"(min={getattr(symbol_info,'volume_min',None)}, "
                f"step={getattr(symbol_info,'volume_step',None)}, "
                f"max={getattr(symbol_info,'volume_max',None)})"
            )
            if volume_final <= 0:
                raise TradeExecutionError(
                    f"Volume final invalide après normalisation ({volume_final})."
                )

            # ---------- 9b) Fat-finger & caps globaux (optionnels) ----------
            try:
                tes = self.config_manager.get("trade_executor_settings", {}) or {}
                ff = tes.get("fat_finger_check", {}) or {}
                ff_enabled = bool(ff.get("enabled", False))
                vol_safety_enabled = bool(tes.get("volume_safety_enabled", False))

                if ff_enabled:
                    per_asset = ff.get("max_absolute_volume_for_asset") or {}
                    cap_sym = per_asset.get(raw_symbol)
                    if isinstance(cap_sym, (int, float)) and volume_final > float(
                        cap_sym
                    ):
                        raise TradeExecutionError(
                            f"Fat-finger: volume {volume_final} > cap absolu {float(cap_sym)} sur {raw_symbol}."
                        )

                cap_global = tes.get("max_absolute_volume_safety", None)
                if (
                    vol_safety_enabled
                    and isinstance(cap_global, (int, float))
                    and volume_final > float(cap_global)
                ):
                    raise TradeExecutionError(
                        f"Safety cap (global): volume {volume_final} > cap sécurité {float(cap_global)}."
                    )

                account_trade_settings = (
                    market_context.get("active_broker_account", {}).get(
                        "trade_settings", {}
                    )
                    or {}
                )
                acc_min = account_trade_settings.get("min_lot")
                acc_step = account_trade_settings.get("lot_step")
                acc_max = account_trade_settings.get("max_lot")
                self.logger.info(
                    f"[VOLUME] constraints compte: min={acc_min}, step={acc_step}, max={acc_max}"
                )

                if isinstance(acc_max, (int, float)) and volume_final > float(acc_max):
                    raise TradeExecutionError(
                        f"Volume {volume_final} > max lot compte {float(acc_max)}."
                    )
            except TradeExecutionError:
                raise
            except Exception as e:
                self.logger.warning(
                    f"Vérif volume (fat-finger/caps) partielle échouée: {e}"
                )

            # ---------- 10) Construction requête ----------
            if rule_name_local == "burst_scalping":
                # 🚀 Redirection spécifique vers trailing stop
                return self.build_burst_trailing_request(
                    trade_decision,
                    active_config,
                    volume_final,
                    entry_price_market,
                    sl_price,
                    symbol_info,
                )
            else:
                # 🏦 Mode classique avec TP/SL
                return self._build_mt5_request(
                    {
                        "action": action,
                        "asset": broker_symbol,
                        "order_type": order_type,
                    },
                    active_config,
                    volume_final,
                    entry_price_market,
                    sl_price,
                    tp_price,
                    symbol_info,
                    trigger_price,
                    order_type,
                )

        except TradeExecutionError:
            raise
        except Exception as e:
            self.logger.error(
                f"Erreur inattendue préparation ordre {broker_symbol}: {e}",
                exc_info=True,
            )
            raise TradeExecutionError(
                f"Échec inattendu de préparation d'ordre pour {broker_symbol}: {e}"
            ) from e

    def apply_dynamic_trailing(
        self, ticket: int, sl_pips: float, atr_pips: float, symbol: Optional[str] = None
    ) -> None:
        """
        Applique un trailing stop dynamique (sans détendre le SL).
        - Respecte stops_level/freeze_level/tick_size (+ buffer configurable)
        - BUY: SL = Ask - (dist + buffer) ; SELL: SL = Bid + (dist + buffer)
        - Rate-limit par ticket et pas de micro-update (< 1 tick)
        """
        import time

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
            si = None
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

            # ----- Ne JAMAIS détendre + éviter les micro-updates (< 1 tick) -----
            cur_sl = pos.get("sl")
            cur_sl = float(cur_sl) if isinstance(cur_sl, (int, float)) else None
            min_move = max(tick_size, point)

            if cur_sl is not None:
                if action == "BUY":
                    # on ne monte le SL que si on dépasse d'au moins ~1/2 tick
                    if not (new_sl > cur_sl + (min_move * 0.5)):
                        return
                else:
                    if not (new_sl < cur_sl - (min_move * 0.5)):
                        return

            # ----- Rate-limit par ticket -----
            try:
                min_ms = int(
                    self.config_manager.get(
                        "dynamic_trailing.min_ms_between_updates", 150
                    )
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

            # ----- Envoi modification SL (via helper existant) -----
            ok = False
            try:
                ok = bool(self._modify_sl(ticket, new_sl))
            except Exception as e:
                self.logger.warning(
                    f"[TRAILING] _modify_sl exception sur {sym}/{ticket}: {e}"
                )

            if ok:
                self._last_trail_update_ms[ticket] = now_ms
                try:
                    # message homogène avec précision correcte
                    self.logger.info(
                        f"[TRAILING] {action} {sym} ticket={ticket}: SL -> {format(new_sl, f'.{digits}f')}"
                    )
                except Exception:
                    self.logger.info(
                        f"[TRAILING] {action} {sym} ticket={ticket}: SL -> {new_sl}"
                    )

        except Exception as e:
            self.logger.warning(
                f"[TRAILING] Erreur application trailing sur {(symbol or sym)}/{ticket}: {e}"
            )

    def _calculate_sl_tp_prices(
        self,
        trade_decision: dict,
        config: dict,
        symbol_info: Any,
        entry_price: float,
        market_context: dict,
    ) -> tuple[float, Optional[float]]:
        """
        Calcule SL/TP à partir des méthodes SWING / ATR / PIPS et TP via RR / ATR_MULTIPLE / PIPS.
        - Respecte stops_level broker (+ soft buffer 2 ticks).
        - Arrondit aux ticks (trade_tick_size) selon le sens (SL 'vers' l'extérieur, TP 'vers' l'extérieur).
        - Garde-fous spread & cohérence BID/ASK.
        - Désactive le TP si la stratégie/règle l'impose (burst_scalping).
        """
        # imports locaux pour éviter les dépendances module-globales
        import math

        try:
            import pandas as pd
            import numpy as np
        except Exception:  # tolérance si l'appelant n’utilise pas ATR/SWING
            pd = None  # type: ignore
            np = None  # type: ignore

        self.logger.info("Calcul du SL/TP (SWING/ATR/PIPS + RR/ATR_MULTIPLE/PIPS)...")

        # --- Action ---
        action_raw = str(trade_decision.get("action", "")).strip().upper()
        action = {"LONG": "BUY", "SHORT": "SELL"}.get(action_raw, action_raw)
        if action not in ("BUY", "SELL"):
            raise TradeExecutionError(f"Action invalide pour SL/TP: '{action_raw}'")

        # --- Symbole/broker ---
        point = float(getattr(symbol_info, "point", 0.0) or 0.0)
        if point <= 0:
            raise TradeExecutionError("symbol_info.point invalide (<=0).")
        digits = int(getattr(symbol_info, "digits", 0) or 0)
        tick_size = float(getattr(symbol_info, "trade_tick_size", 0.0) or point)
        min_stop_points = int(getattr(symbol_info, "trade_stops_level", 0) or 0)
        min_stop_price = min_stop_points * point

        # soft buffer si broker annonce 0 → ~2 ticks
        min_ticks_soft = 2
        soft_min_price = max(min_stop_price, min_ticks_soft * tick_size)

        # pips (digits 3/5 => 10 points/pip, sinon 1)
        points_per_pip = 10.0 if digits in (3, 5) else 1.0
        pip_size = point * points_per_pip

        # --- Helpers arrondis ---
        def _ceil_to_tick(x: float) -> float:
            if tick_size <= 0:
                return round(float(x), digits)
            steps = math.ceil(float(x) / tick_size - 1e-12)
            return round(steps * tick_size, digits)

        def _floor_to_tick(x: float) -> float:
            if tick_size <= 0:
                return round(float(x), digits)
            steps = math.floor(float(x) / tick_size + 1e-12)
            return round(steps * tick_size, digits)

        # --- Overrides PIPS / spread ---
        sl_pips_override = trade_decision.get("target_sl_pips")
        tp_pips_override = trade_decision.get("target_tp_pips")
        spread_pips = float(trade_decision.get("spread_pips", 0.0) or 0.0)

        # --- Paramètres produit & stratégie ---
        prod_st = config.get("smart_sl_tp_settings", {}) or {}
        sl_method = str(prod_st.get("sl_placement_method", "PIPS")).upper()
        tp_method = str(prod_st.get("tp_placement_method", "RR")).upper()
        rr_ratio = float(prod_st.get("tp_rr_ratio", 1.5) or 1.5)
        if not math.isfinite(rr_ratio) or rr_ratio <= 0:
            rr_ratio = 1.5

        strat_st = config.get("smart_targets") or {}
        st_sl = strat_st.get("stop_loss") or {}
        st_tp = strat_st.get("take_profit") or {}
        sl_hard_min_points = float(st_sl.get("hard_min_points", 0) or 0.0)
        sl_hard_max_points = float(
            st_sl.get("hard_max_points", float("inf")) or float("inf")
        )
        tp_hard_max_points = float(
            st_tp.get("hard_max_points", float("inf")) or float("inf")
        )

        # --- Données marché (pour ATR/SWING) ---
        symbol = (
            str(trade_decision.get("asset") or trade_decision.get("symbol") or "")
        ).upper()
        rates_df = None
        try:
            md = market_context.get("market_data") or {}
            rates_df = md.get(symbol)
        except Exception:
            rates_df = None

        def _compute_atr(df, period: int) -> float:
            if (
                pd is None
                or np is None
                or not isinstance(df, pd.DataFrame)
                or len(df) < period + 2
            ):
                return float("nan")
            try:
                high = df["high"].astype(float)
                low = df["low"].astype(float)
                close = df["close"].astype(float)
                pc = close.shift(1)
                tr = np.maximum.reduce(
                    [
                        (high - low).abs().values,
                        (high - pc).abs().values,
                        (low - pc).abs().values,
                    ]
                )
                # moyenne simple (ou utilise EMA si tu préfères)
                atr = (
                    pd.Series(tr)
                    .rolling(window=period, min_periods=period)
                    .mean()
                    .iloc[-1]
                )
                return float(atr) if pd.notna(atr) and atr > 0 else float("nan")
            except Exception:
                return float("nan")

        # ================= SL =================
        stop_loss_price: float = 0.0

        if isinstance(sl_pips_override, (int, float)) and sl_pips_override > 0:
            sl_dist = float(sl_pips_override) * pip_size
            stop_loss_price = (
                entry_price - sl_dist if action == "BUY" else entry_price + sl_dist
            )
        else:
            if sl_method == "SWING":
                lookback = int(prod_st.get("sl_swing_lookback_period", 10) or 10)
                buffer_pips = float(prod_st.get("sl_buffer_pips", 2) or 2.0)
                if not (
                    isinstance(rates_df, pd.DataFrame) and len(rates_df) >= lookback
                ):
                    sl_method = "ATR"
                else:
                    buf = buffer_pips * pip_size
                    if action == "BUY":
                        stop_loss_price = (
                            float(rates_df.tail(lookback)["low"].min()) - buf
                        )
                    else:
                        stop_loss_price = (
                            float(rates_df.tail(lookback)["high"].max()) + buf
                        )

            if sl_method == "ATR" and stop_loss_price == 0.0:
                atr_p = int(
                    prod_st.get(
                        "sl_atr_period",
                        prod_st.get("atr_settings", {}).get("period", 14),
                    )
                    or 14
                )
                atr_mult = float(prod_st.get("sl_atr_multiplier", 1.2) or 1.2)
                atr = _compute_atr(rates_df, atr_p)
                if not (atr == atr and atr > 0):
                    sl_method = "PIPS"
                else:
                    sl_dist = atr_mult * atr
                    stop_loss_price = (
                        entry_price - sl_dist
                        if action == "BUY"
                        else entry_price + sl_dist
                    )

            if sl_method == "PIPS" and stop_loss_price == 0.0:
                sl_pips = float(config.get("stop_loss_pips", 10) or 10.0)
                sl_dist = sl_pips * pip_size
                stop_loss_price = (
                    entry_price - sl_dist if action == "BUY" else entry_price + sl_dist
                )

        # ================= TP =================
        # Désactivation TP pour burst scalping (via strategy_name ou rule_name ou flag no_tp)
        rule_name = str(trade_decision.get("rule_name", "")).lower()
        no_tp_flag = bool(trade_decision.get("no_tp", False))
        no_tp_strategy = str(config.get("strategy_name", "")).lower() in {
            "burst_scalping",
            "scalping_burst",
        }
        no_tp = no_tp_flag or no_tp_strategy or (rule_name == "burst_scalping")

        take_profit_price: Optional[float] = None
        if not no_tp:
            if isinstance(tp_pips_override, (int, float)) and tp_pips_override > 0:
                tp_dist = float(tp_pips_override) * pip_size
                take_profit_price = (
                    entry_price + tp_dist if action == "BUY" else entry_price - tp_dist
                )
            else:
                if tp_method == "RR":
                    risk = abs(entry_price - stop_loss_price)
                    if risk > 0:
                        tp_dist = risk * rr_ratio
                        take_profit_price = (
                            entry_price + tp_dist
                            if action == "BUY"
                            else entry_price - tp_dist
                        )
                    else:
                        tp_method = "PIPS"

                if tp_method == "ATR_MULTIPLE" and take_profit_price is None:
                    atr_p = int(
                        prod_st.get("tp_atr_period", prod_st.get("sl_atr_period", 14))
                        or 14
                    )
                    atr_mult = float(prod_st.get("tp_atr_multiplier", 2.0) or 2.0)
                    atr = _compute_atr(rates_df, atr_p)
                    if atr == atr and atr > 0:
                        tp_dist = atr_mult * atr
                        take_profit_price = (
                            entry_price + tp_dist
                            if action == "BUY"
                            else entry_price - tp_dist
                        )

                if take_profit_price is None:  # fallback PIPS
                    tp_pips = float(config.get("take_profit_pips", 20) or 20.0)
                    tp_dist = tp_pips * pip_size
                    take_profit_price = (
                        entry_price + tp_dist
                        if action == "BUY"
                        else entry_price - tp_dist
                    )

        # ================= Validations & ajustements =================
        if not (isinstance(entry_price, (int, float)) and entry_price > 0):
            raise TradeExecutionError("Prix d'entrée invalide.")

        # Distances brutes
        sl_dist_price = (
            (entry_price - stop_loss_price)
            if action == "BUY"
            else (stop_loss_price - entry_price)
        )
        if not (sl_dist_price and sl_dist_price > 0):
            raise TradeExecutionError("Distance SL invalide (<=0).")

        tp_dist_price = None
        if take_profit_price is not None:
            tp_dist_price = (
                (take_profit_price - entry_price)
                if action == "BUY"
                else (entry_price - take_profit_price)
            )
            if not (tp_dist_price and tp_dist_price > 0):
                raise TradeExecutionError("Distance TP invalide (<=0).")

        # A) min broker + soft 2 ticks
        sl_dist_price = max(sl_dist_price, soft_min_price)
        if tp_dist_price is not None:
            tp_dist_price = max(tp_dist_price, soft_min_price)

        # B) Hard limits (en points)
        sl_dist_points = sl_dist_price / point
        sl_dist_points = max(sl_dist_points, sl_hard_min_points)
        sl_dist_points = min(sl_dist_points, sl_hard_max_points)
        sl_dist_price = sl_dist_points * point

        tp_dist_points = None
        if tp_dist_price is not None:
            tp_dist_points = tp_dist_price / point
            tp_dist_points = min(tp_dist_points, tp_hard_max_points)
            tp_dist_price = tp_dist_points * point

        # C) Ajustement spread (si fourni) : impose TP >= SL + spread (en pips)
        if tp_dist_price is not None and spread_pips > 0:
            sl_pips_now = sl_dist_points / points_per_pip
            tp_pips_now = (
                tp_dist_points / points_per_pip if tp_dist_points is not None else None
            )
            if tp_pips_now is not None and tp_pips_now < (sl_pips_now + spread_pips):
                tp_dist_points = (sl_pips_now + spread_pips) * points_per_pip
                tp_dist_price = tp_dist_points * point

        # D) Positionnement côté BID/ASK (si tick disponible)
        try:
            tick_map = market_context.get("last_tick") or {}
            tick = tick_map.get(symbol) or {}
            bid = float(tick.get("bid") or 0.0)
            ask = float(tick.get("ask") or 0.0)
        except Exception:
            bid = ask = 0.0

        if bid > 0 and ask > 0 and ask > bid:
            if action == "BUY":
                stop_loss_price = entry_price - sl_dist_price
                if take_profit_price is not None:
                    take_profit_price = entry_price + tp_dist_price
                    # assure TP > ASK + min
                    if take_profit_price < (ask + soft_min_price - 1e-12):
                        take_profit_price = ask + soft_min_price
            else:  # SELL
                stop_loss_price = entry_price + sl_dist_price
                if take_profit_price is not None:
                    take_profit_price = entry_price - tp_dist_price
                    # assure TP < BID - min
                    if take_profit_price > (bid - soft_min_price + 1e-12):
                        take_profit_price = bid - soft_min_price
        else:
            # fallback sans bid/ask
            stop_loss_price = (
                entry_price - sl_dist_price
                if action == "BUY"
                else entry_price + sl_dist_price
            )
            if take_profit_price is not None:
                take_profit_price = (
                    entry_price + (tp_dist_price or 0.0)
                    if action == "BUY"
                    else entry_price - (tp_dist_price or 0.0)
                )

        # E) Arrondi à la grille des ticks + dernières cohérences directionnelles
        if action == "BUY":
            stop_loss_price = _floor_to_tick(
                min(stop_loss_price, entry_price - soft_min_price)
            )
            if take_profit_price is not None:
                take_profit_price = _ceil_to_tick(
                    max(take_profit_price, entry_price + soft_min_price)
                )
                if not (take_profit_price > entry_price):
                    take_profit_price = _ceil_to_tick(entry_price + soft_min_price)
            if not (stop_loss_price < entry_price):
                stop_loss_price = _floor_to_tick(entry_price - soft_min_price)
        else:  # SELL
            stop_loss_price = _ceil_to_tick(
                max(stop_loss_price, entry_price + soft_min_price)
            )
            if take_profit_price is not None:
                take_profit_price = _floor_to_tick(
                    min(take_profit_price, entry_price - soft_min_price)
                )
                if not (take_profit_price < entry_price):
                    take_profit_price = _floor_to_tick(entry_price - soft_min_price)
            if not (stop_loss_price > entry_price):
                stop_loss_price = _ceil_to_tick(entry_price + soft_min_price)

        # Sortie finale
        stop_loss_price = round(float(stop_loss_price), digits)
        take_profit_price = (
            None
            if (no_tp or take_profit_price is None)
            else round(float(take_profit_price), digits)
        )
        return float(stop_loss_price), (
            None if take_profit_price is None else float(take_profit_price)
        )

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
                self.config_manager.get("dynamic_trailing.max_latency_ms_on_close", 0)
                or 0
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
                        if (
                            res
                            and getattr(res, "retcode", None) == mt5.TRADE_RETCODE_DONE
                        ):
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
                                p
                                for p in _list_open_positions()
                                if _v(p, "ticket") == tk
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

        close_on_full_profit = bool(closure.get("close_on_full_profit", True))
        require_full_count = bool(
            closure.get("require_full_count_for_profit_close", True)
        )
        min_green_pnl_pips = float(closure.get("min_green_pnl_pips", 0.0))
        rt_fast_window_ms = int(closure.get("rt_fast_window_ms", 2500))
        rt_poll_interval_ms = int(closure.get("rt_poll_interval_ms", 100))
        max_loss_pips = float(closure.get("max_loss_pips", float(max_loss_pips)))
        trail_trigger_pips = float(
            closure.get("trail_trigger_pips", float(trail_trigger))
        )
        trail_distance_pips = float(
            closure.get(
                "trail_distance_pips", (trail_step if float(trail_step) > 0 else 5.0)
            )
        )
        trail_require_full = bool(closure.get("trail_require_full_count", False))
        # NOUVEAU: intervalle mini entre deux updates de trailing broker (anti-spam)
        trail_update_min_interval_ms = int(
            closure.get("trail_update_min_interval_ms", 120)
        )

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
                            self.logger.info(
                                f"[CLOSE] Panier '{basket_id}' fermé (bulk)."
                            )
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
                                        str(
                                            _v(positions[0], "symbol", "") or ""
                                        ).upper()
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
            left = [
                p for p in _snapshot_positions() if _extract_basket_id(p) == basket_id
            ]
            if not left:
                self._basket_peak_pips.pop(basket_id, None)
                self._basket_trail_armed.pop(basket_id, None)
                self._burst_trailing_state.pop(basket_id, None)
                self._last_trail_update_ms.pop(basket_id, None)  # NOUVEAU
                self.logger.info(
                    f"[CLOSE] Panier '{basket_id}' fermé (fallback tickets)."
                )
                # === COOLDOWN POST-EXIT (par symbole) ===
                try:
                    if not hasattr(self, "_cooldown_until"):
                        self._cooldown_until = {}
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
                    if (
                        direction == "BUY"
                        and cur_sl is not None
                        and target_sl <= cur_sl
                    ):
                        continue
                    if (
                        direction == "SELL"
                        and cur_sl is not None
                        and target_sl >= cur_sl
                    ):
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
                    # 1) CLOSE INSTANTANÉ : Panier plein & TOUT VERT
                    if close_on_full_profit and _all_green_and_full(pos):
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
                        _last_ms = int(
                            self._last_trail_update_ms.get(basket_id, 0) or 0
                        )
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
            try:
                # 1) all-green encore (au cas où)
                if close_on_full_profit and _all_green_and_full(pos):
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
                    self.logger.warning(
                        f"❌ {basket_id} perte {pnl_pips:.1f}p ≤ -{abs(max_loss_pips):.1f}p → CLOSE"
                    )
                    _close_basket(basket_id, pos)
                    continue

                # 3) trailing (armement / peak / retrace)
                expected = _expected_count_from(pos)
                is_full = expected is not None and len(pos) >= expected
                if pnl_pips >= trail_trigger_pips and (
                    (not trail_require_full) or is_full
                ):
                    if not self._basket_trail_armed.get(basket_id, False):
                        self._basket_trail_armed[basket_id] = True
                        self._basket_peak_pips[basket_id] = pnl_pips
                        self.logger.info(
                            f"🛡️ {basket_id} ARMÉ (Phase B) à {pnl_pips:.1f}p"
                        )

                if self._basket_trail_armed.get(basket_id, False):
                    if pnl_pips > float(
                        self._basket_peak_pips.get(basket_id, pnl_pips)
                    ):
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

                    dd = (
                        float(self._basket_peak_pips.get(basket_id, pnl_pips))
                        - pnl_pips
                    )
                    if dd >= trail_distance_pips and pnl_pips > 0.0:
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
        Sizing par risque (compte) — source unique = broker_accounts.trade_settings.risk_per_trade_percent

        Principes:
        - AUCUN volume par défaut : si on ne peut pas garantir le risque -> TradeExecutionError
        - Priorité aux données broker: order_calc_profit / tick_value & tick_size
        - Heuristique pip-value optionnelle (off par défaut)
        - BURST: partage du budget par ticket si non single-master
        - Planchers "sizing floor" (stops_level/freeze_level/spread/ATR) pour éviter SL ~ prix
        - Clamps & arrondis (symbole/compte), caps sécurité/fat-finger, contrôle marge
        """
        import math, os

        # --- Action normalisée ---
        action = str(trade_decision.get("action", "")).upper()
        action = {"LONG": "BUY", "SHORT": "SELL"}.get(action, action)
        if action not in ("BUY", "SELL"):
            raise TradeExecutionError(f"Action invalide pour sizing: '{action}'")

        # --- Équité compte ---
        acct_info = (context or {}).get("account_info", {}) or {}
        equity = acct_info.get("equity")
        if not isinstance(equity, (int, float)) or equity <= 0:
            raise TradeExecutionError("Équité du compte non positive ou manquante.")

        # --- Helpers ---
        def _try_float(x, default=None):
            try:
                return float(x)
            except Exception:
                return default

        def _sget(obj, *names, default=None):
            for n in names:
                if hasattr(obj, n):
                    v = getattr(obj, n)
                    if v is not None:
                        return v
                if isinstance(obj, dict) and obj.get(n) is not None:
                    return obj[n]
            return default

        # --- Risque % (source unique) ---
        risk_pct = None
        risk_source = None

        ovr = _try_float((trade_decision or {}).get("risk_pct_override"))
        if ovr and ovr > 0:
            risk_pct, risk_source = ovr, "decision.override"

        if risk_pct is None:
            r = _try_float((account_trade_settings or {}).get("risk_per_trade_percent"))
            if r and r > 0:
                risk_pct, risk_source = r, "account_trade_settings"

        if risk_pct is None:
            aba = ((context or {}).get("active_broker_account") or {}).get(
                "trade_settings", {}
            ) or {}
            r = _try_float(aba.get("risk_per_trade_percent"))
            if r and r > 0:
                risk_pct, risk_source = r, "context.active_broker_account"

        if risk_pct is None:
            r = _try_float(os.getenv("SNIPER_RISK_PCT"))
            if r and r > 0:
                risk_pct, risk_source = r, "env.SNIPER_RISK_PCT"

        # Legacy (log unique)
        legacy_risk_local = config.get("risk_per_trade_percent") or (
            config.get("risk_management") or {}
        ).get("risk_per_trade_pct")
        if legacy_risk_local is not None and not getattr(
            self, "_warned_legacy_risk", False
        ):
            self.logger.warning(
                "Legacy key détectée pour le risque (%s) — ignorée. "
                "Utiliser broker_accounts.trade_settings.risk_per_trade_percent.",
                legacy_risk_local,
            )
            setattr(self, "_warned_legacy_risk", True)

        if risk_pct is None or risk_pct <= 0:
            raise TradeExecutionError(
                "Risque en % manquant/invalide (source unique requise)."
            )

        # Clip range safe
        min_risk = _try_float(
            self.config_manager.get("risk_management_settings.min_risk_pct", 0.01), 0.01
        )
        max_risk = _try_float(
            self.config_manager.get("risk_management_settings.max_risk_pct", 5.0), 5.0
        )
        risk_pct_clipped = max(min_risk, min(max_risk, float(risk_pct)))
        if risk_pct_clipped != risk_pct:
            self.logger.warning(
                f"[RISK%%] Clip: {risk_pct:.4f}%% -> {risk_pct_clipped:.4f}%% (bounds {min_risk}-{max_risk})"
            )
        risk_pct = risk_pct_clipped

        try:
            self.logger.info(
                f"[RISK%%] Utilisé: {risk_pct:.4f}%% (source={risk_source})"
            )
        except Exception:
            pass

        # --- Budget de risque (compte) ---
        max_dollar_risk_base = float(equity) * (risk_pct / 100.0)
        if max_dollar_risk_base <= 0:
            raise TradeExecutionError("Risque en $ nul/invalide pour le sizing.")

        # Ajustements: confidence [0..1], quality cap (0.5..1.0)
        confidence = float(trade_decision.get("confidence", 1.0) or 1.0)
        confidence = max(0.0, min(1.0, confidence))
        max_dollar_risk = max_dollar_risk_base * confidence

        try:
            q = trade_decision.get("quality")
            if isinstance(q, (int, float)):
                q = max(0.0, min(1.0, float(q)))
                q_cap = 0.5 + 0.5 * q
                if q_cap < 1.0:
                    prev = max_dollar_risk
                    max_dollar_risk *= q_cap
                    self.logger.info(
                        f"[QUALITY CAP] Risk$ {prev:.2f} -> {max_dollar_risk:.2f} (q={q:.2f}, cap={q_cap:.2f})"
                    )
        except Exception:
            pass

        # Partage BURST (sauf single-master)
        def _to_int_pos(x):
            try:
                xi = int(float(x))
                return xi if xi > 0 else None
            except Exception:
                return None

        burst_size = _to_int_pos(trade_decision.get("burst_size"))
        if burst_size is None and isinstance(context, dict):
            for k in (
                "burst_size",
                "burst.count",
                "burst.size",
                "current_burst_size",
                "basket_size",
            ):
                v = context
                for part in k.split("."):
                    v = v.get(part) if isinstance(v, dict) else None
                burst_size = _to_int_pos(v)
                if burst_size:
                    break
        if burst_size is None:
            burst_size = (
                _to_int_pos(
                    (
                        (
                            (config.get("entry_rules", {}) or {}).get("scalping", {})
                            or {}
                        ).get("burst_scalping", {})
                        or {}
                    ).get("burst_size", 1)
                )
                or 1
            )

        single_master_mode = (
            bool(trade_decision.get("burst_single_master"))
            or bool((config.get("burst_single_master", {}) or {}).get("enabled", False))
            or bool(
                (
                    (
                        (config.get("entry_rules", {}) or {}).get("scalping", {}) or {}
                    ).get("burst_scalping", {})
                    or {}
                )
                .get("burst_single_master", {})
                .get("enabled", False)
            )
        )
        if (not single_master_mode) and burst_size and burst_size > 1:
            max_dollar_risk /= burst_size

        # Volatility factor (réducteur uniquement)
        if "volatility_factor" in (trade_decision or {}):
            try:
                vfac = float(trade_decision.get("volatility_factor") or 1.0)
                if math.isfinite(vfac) and vfac > 0:
                    if vfac < 1.0:
                        prev = max_dollar_risk
                        max_dollar_risk *= vfac
                        self.logger.info(
                            f"[VOLATILITY FACTOR] Risk$ {prev:.2f} -> {max_dollar_risk:.2f} (x{vfac:.3f})"
                        )
                    elif vfac > 1.0:
                        self.logger.info(
                            f"[VOLATILITY FACTOR] Facteur>1 ({vfac:.3f}) ignoré (pas d'augmentation du risque)."
                        )
            except Exception as e:
                self.logger.warning(f"[VOLATILITY FACTOR] Ignoré: {e}")

        # --- Distance prix (Entry -> SL) : préparation "sizing floor" ---
        try:
            _ = abs(float(entry_price) - float(sl_price))
        except Exception:
            raise TradeExecutionError("Entry/SL invalides pour sizing.")

        sym_name = _sget(
            symbol_info, "name", default=str(trade_decision.get("asset", "")).upper()
        )
        point = float(_sget(symbol_info, "point", default=0.00001) or 0.00001)
        digits = int(_sget(symbol_info, "digits", default=5) or 5)
        contract = float(
            _sget(symbol_info, "trade_contract_size", "contract_size", default=100000.0)
            or 100000.0
        )
        tick_size = float(
            _sget(symbol_info, "trade_tick_size", "tick_size", default=point) or point
        )
        stops_level_pts = int(_sget(symbol_info, "trade_stops_level", default=0) or 0)
        freeze_level_pts = int(_sget(symbol_info, "trade_freeze_level", default=0) or 0)
        spread_pts = int(_sget(symbol_info, "spread", default=0) or 0)
        one_tick_pts = max(1, int(round((tick_size or point) / (point or 1.0))))
        _min_buf_pts = max(stops_level_pts, freeze_level_pts, spread_pts) + one_tick_pts

        # Planchers de sizing
        try:
            min_sizing_stop_pts = int(
                self.config_manager.get(
                    "risk_management_settings.min_sizing_stop_pts", 0
                )
                or 0
            )
        except Exception:
            min_sizing_stop_pts = 0
        try:
            min_sizing_stop_atr_mult = float(
                self.config_manager.get(
                    "risk_management_settings.min_sizing_stop_atr_mult", 0.25
                )
            )
        except Exception:
            min_sizing_stop_atr_mult = 0.25

        last_atr_pts = 0.0
        try:
            for k in ("last_atr_points", "atr_points", "last_atr_pts", "atr_pts"):
                v = (context or {}).get(k)
                if isinstance(v, (int, float)) and v > 0:
                    last_atr_pts = float(v)
                    break
        except Exception:
            pass
        floor_pts_atr = (
            int(round(last_atr_pts * min_sizing_stop_atr_mult))
            if last_atr_pts > 0 and min_sizing_stop_atr_mult > 0
            else 0
        )
        _sizing_floor_pts = max(_min_buf_pts, min_sizing_stop_pts, floor_pts_atr)

        # SL effectif utilisé pour le calcul de risque (ne modifie PAS le SL réel envoyé)
        if action == "BUY":
            sl_eff = min(
                float(sl_price), float(entry_price) - _min_buf_pts * float(point)
            )
        else:
            sl_eff = max(
                float(sl_price), float(entry_price) + _min_buf_pts * float(point)
            )

        _pt = float(point) if float(point or 0.0) > 0.0 else 1e-9
        price_diff_pts = abs(float(entry_price) - float(sl_eff)) / _pt
        if _sizing_floor_pts > 0 and price_diff_pts < _sizing_floor_pts:
            adjust = _sizing_floor_pts * _pt
            sl_eff = (
                (float(entry_price) - adjust)
                if action == "BUY"
                else (float(entry_price) + adjust)
            )

        price_diff = abs(float(entry_price) - float(sl_eff))
        if price_diff <= 0 or not math.isfinite(price_diff):
            raise TradeExecutionError(
                "Distance Entry-SL effective nulle/invalide pour sizing."
            )

        # --- Perte par lot (devise compte) ---
        per_lot_loss = None
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
                    order_type, sym_name, 1.0, entry_price, sl_eff
                )
                # Certaines implémentations exotiques renvoient un tuple — on protège
                if isinstance(profit, (tuple, list)) and profit:
                    profit = profit[-1]
                per_lot_loss = abs(float(profit))
                if not math.isfinite(per_lot_loss) or per_lot_loss <= 0:
                    per_lot_loss = None
            except Exception as e:
                self.logger.warning(f"mt5.order_calc_profit indisponible: {e}.")
                per_lot_loss = None

        # Fallback 1: tick_value / tick_size
        if per_lot_loss is None or per_lot_loss <= 0:
            tv = _sget(symbol_info, "trade_tick_value", "tick_value", default=0.0)
            ts = _sget(symbol_info, "trade_tick_size", "tick_size", default=0.0)
            try:
                tv = float(tv or 0.0)
                ts = float(ts or 0.0)
            except Exception:
                tv, ts = 0.0, 0.0
            if tv > 0 and ts > 0:
                per_lot_loss = (price_diff / ts) * tv

        # Fallback 2: heuristique pip-value (optionnelle)
        allow_pip_heuristic = bool(
            self.config_manager.get(
                "risk_management_settings.allow_heuristic_pip_fallback", False
            )
        )
        if (per_lot_loss is None or per_lot_loss <= 0) and allow_pip_heuristic:
            points_per_pip = 10.0 if digits in (3, 5) else 1.0
            pip_size = point * points_per_pip
            quote = (
                sym_name[-3:].upper()
                if isinstance(sym_name, str) and len(sym_name) >= 6
                else ""
            )
            if pip_size <= 0:
                raise TradeExecutionError(f"[{sym_name}] pip_size invalide.")
            if quote == "USD":
                pip_value_per_lot = contract * pip_size
                per_lot_loss = (price_diff / pip_size) * pip_value_per_lot
            elif quote == "JPY":
                pip_value_jpy = contract * 0.01
                if not entry_price or not math.isfinite(float(entry_price)):
                    raise TradeExecutionError(
                        f"[{sym_name}] Entry invalide pour conversion JPY."
                    )
                pip_value_acct = pip_value_jpy / float(entry_price)
                per_lot_loss = (price_diff / pip_size) * pip_value_acct
            else:
                raise TradeExecutionError(
                    f"[{sym_name}] tick_value/tick_size indisponibles et heuristique pip-value interdite pour {quote}."
                )

        if per_lot_loss is None or per_lot_loss <= 0 or not math.isfinite(per_lot_loss):
            raise TradeExecutionError("Impossible de calculer la perte par lot.")

        # Planchers de sécurité pour per_lot_loss
        min_dlr_per_lot = float(
            self.config_manager.get(
                "risk_management_settings.min_dollar_risk_per_lot_fallback", 1.0
            )
        )
        if per_lot_loss < min_dlr_per_lot:
            self.logger.debug(
                f"Perte/lot trop faible ({per_lot_loss:.6f}) -> plancher {min_dlr_per_lot:.6f} appliqué."
            )
            per_lot_loss = min_dlr_per_lot
        try:
            per_asset_min = (
                self.config_manager.get(
                    "risk_management_settings.per_asset_min_dollar_risk_per_lot_fallback",
                    {},
                )
                or {}
            ).get(sym_name)
            if (
                isinstance(per_asset_min, (int, float))
                and per_asset_min > min_dlr_per_lot
                and per_lot_loss < float(per_asset_min)
            ):
                self.logger.debug(
                    f"[{sym_name}] Plancher perte/lot (asset): {per_lot_loss:.6f} -> {float(per_asset_min):.6f}"
                )
                per_lot_loss = float(per_asset_min)
        except Exception:
            pass

        # Volume brut (avant caps) + cap marge (théorique 1 lot)
        raw_volume = max_dollar_risk / per_lot_loss
        try:
            if mt5_mod and hasattr(mt5_mod, "order_calc_margin"):
                order_type = (
                    getattr(mt5_mod, "ORDER_TYPE_BUY", 0)
                    if action == "BUY"
                    else getattr(mt5_mod, "ORDER_TYPE_SELL", 1)
                )
                m1 = mt5_mod.order_calc_margin(order_type, sym_name, 1.0, entry_price)
                free_margin = acct_info.get("margin_free") or acct_info.get("balance")
                if m1 and float(m1) > 0 and free_margin and float(free_margin) > 0:
                    raw_max = float(free_margin) / float(m1)
                    if raw_max > 0 and raw_volume > raw_max:
                        self.logger.warning(
                            f"[SIZING] Cap par marge: {raw_volume:.4f} -> {raw_max:.4f} (free={float(free_margin):.2f}, m1={float(m1):.2f})"
                        )
                        raw_volume = raw_max
        except Exception as e:
            self.logger.warning(f"Cap par marge (pré-clamp) ignoré: {e}")

        # Contraintes symbole/compte
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

        # Cap stratégie (optionnel)
        de_risk = (config.get("decision_engine") or {}).get("risk") or {}
        strat_max_lot = de_risk.get("max_lot_size")
        if isinstance(strat_max_lot, (int, float)) and strat_max_lot > 0:
            max_lot_account = min(max_lot_account, float(strat_max_lot))

        # Caps sécurité & fat-finger
        ff_cfg = {}
        try:
            tes = self.config_manager.get("trade_executor_settings", {}) or {}
            ff_cfg = tes.get("fat_finger_check", {}) or {}
            safety_enabled = bool(
                ff_cfg.get("enabled", False) or tes.get("volume_safety_enabled", False)
            )

            max_volume_safety = tes.get("max_absolute_volume_safety", None)
            if (
                safety_enabled
                and isinstance(max_volume_safety, (int, float))
                and math.isfinite(float(max_volume_safety))
                and raw_volume > float(max_volume_safety)
            ):
                self.logger.warning(
                    f"Cap volume sécurité: {raw_volume:.4f} -> {float(max_volume_safety):.4f}"
                )
                raw_volume = float(max_volume_safety)

            per_asset_caps = ff_cfg.get("max_absolute_volume_for_asset", {}) or {}
            asset_cap = per_asset_caps.get(sym_name)
            if (
                safety_enabled
                and isinstance(asset_cap, (int, float))
                and math.isfinite(float(asset_cap))
                and raw_volume > float(asset_cap)
            ):
                self.logger.warning(
                    f"Cap volume actif {sym_name}: {raw_volume:.4f} -> {float(asset_cap):.4f}"
                )
                raw_volume = float(asset_cap)
        except Exception as e:
            self.logger.warning(f"Lecture caps volume sécurité échouée: {e}")

        # Fat-finger dynamique vs historique
        try:
            if bool(ff_cfg.get("enabled", False)) and bool(
                ff_cfg.get("enable_dynamic_check", False)
            ):
                lookback = int(ff_cfg.get("avg_volume_lookback", 20) or 20)
                mult = float(ff_cfg.get("max_volume_multiplier_from_avg", 5.0) or 5.0)
                recent = []
                for k in ("recent_executed_trades", "recent_volumes", "volume_history"):
                    seq = (context or {}).get(k)
                    if isinstance(seq, list):
                        recent = [
                            float(x)
                            for x in seq[-lookback:]
                            if isinstance(x, (int, float))
                        ]
                        if recent:
                            break
                if recent:
                    avg_vol = sum(recent) / max(len(recent), 1)
                    dyn_cap = max(avg_vol * mult, min_lot_account)
                    if raw_volume > dyn_cap:
                        self.logger.warning(
                            f"Fat-finger dynamique: {raw_volume:.4f} -> cap {dyn_cap:.4f}"
                        )
                        raw_volume = dyn_cap
        except Exception as e:
            self.logger.warning(f"Vérif fat-finger dynamique non appliquée: {e}")

        # Arrondi & clamps initiaux
        effective_step = max(lot_step_account, vol_step_sym) or 0.01
        if effective_step <= 0:
            effective_step = 0.01
        volume = max(min_lot_account, vol_min_sym, raw_volume)
        volume = min(max_lot_account, vol_max_sym, volume)
        steps = math.floor(volume / effective_step)
        volume = round(steps * effective_step, 8)
        volume = max(min_lot_account, volume)
        volume = min(max_lot_account, volume)
        if volume <= 0:
            raise TradeExecutionError(f"Volume calculé invalide ({volume}).")

        # Contrôle marge final (réduction si nécessaire)
        try:
            if mt5_mod and hasattr(mt5_mod, "order_calc_margin"):
                order_type = (
                    getattr(mt5_mod, "ORDER_TYPE_BUY", 0)
                    if action == "BUY"
                    else getattr(mt5_mod, "ORDER_TYPE_SELL", 1)
                )
                margin_required = mt5_mod.order_calc_margin(
                    order_type, sym_name, volume, entry_price
                )
                free_margin = acct_info.get("margin_free")
                if (
                    margin_required is not None
                    and free_margin is not None
                    and math.isfinite(float(margin_required))
                    and float(margin_required) > float(free_margin)
                ):
                    ratio = max(float(free_margin) / float(margin_required), 0.0)
                    reduced = max(min_lot_account, vol_min_sym, ratio * volume)
                    steps = math.floor(reduced / effective_step)
                    reduced = round(steps * effective_step, 8)
                    if reduced < min_lot_account:
                        raise TradeExecutionError(
                            "Marge libre insuffisante pour le volume minimum."
                        )
                    self.logger.warning(
                        f"Marge insuffisante: besoin ~{margin_required:.2f}, libre {free_margin:.2f}. "
                        f"Volume réduit {volume:.4f} -> {reduced:.4f}"
                    )
                    volume = reduced
        except Exception as e:
            self.logger.warning(f"Contrôle marge non appliqué: {e}")

        # Failsafe absolu (même si safety off)
        try:
            failsafe_cap = float(
                self.config_manager.get(
                    "trade_executor_settings.absolute_volume_failsafe", 0.0
                )
                or 0.0
            )
            if failsafe_cap > 0 and volume > failsafe_cap:
                self.logger.warning(
                    f"[VOLUME FAILSAFE] {volume:.4f} -> {failsafe_cap:.4f}"
                )
                volume = failsafe_cap
        except Exception:
            pass

        # Vérification du risque réel vs budget (tolérance)
        tol = float(
            self.config_manager.get(
                "trade_executor_settings.max_risk_deviation_multiplier", 1.05
            )
        )
        actual_risk = volume * per_lot_loss
        max_allowed = max_dollar_risk * tol
        if actual_risk > max_allowed and per_lot_loss > 0:
            target_vol = max(min_lot_account, vol_min_sym, max_allowed / per_lot_loss)
            target_vol = min(max_lot_account, vol_max_sym, target_vol)
            steps = math.floor(target_vol / effective_step)
            target_vol = round(steps * effective_step, 8)
            actual_risk = target_vol * per_lot_loss
            if (
                actual_risk > max_allowed
            ):  # ultime petit cran si l'arrondi step nous dépasse encore
                target_vol = max(min_lot_account, round(target_vol - effective_step, 8))
                actual_risk = target_vol * per_lot_loss
            self.logger.warning(
                f"Risque réel {volume * per_lot_loss:.2f} > max {max_dollar_risk:.2f} (tol {tol:.2f}). "
                f"Volume ajusté {volume:.4f} -> {target_vol:.4f}"
            )
            volume = target_vol

        # Log final
        self.logger.info(
            f"Sizing {sym_name}: equity={equity:.2f}, risk%={risk_pct:.4f}, "
            f"risk$={max_dollar_risk:.2f}, per_lot_loss={per_lot_loss:.6f} -> vol={volume:.4f} "
            f"(acc_min={min_lot_account}, acc_step={lot_step_account}, acc_max={max_lot_account}; "
            f"sym_min={vol_min_sym}, sym_step={vol_step_sym}, sym_max={vol_max_sym}; burst_size={burst_size})."
        )

        return float(volume)

    def _cooldown_guard(
        self,
        asset: str,
        now_ts: float,
        *,
        per_asset_cooldown_s: float = 20.0,
        min_gap_any_trade_s: float = 5.0,
        max_new_trades_per_cycle: int = 1,
    ) -> bool:
        """
        Retourne True si on DOIT SKIP l'envoi d'un nouvel ordre (cooldown).
        - per_asset_cooldown_s: délai min entre 2 nouvelles entrées sur le même asset
        - min_gap_any_trade_s: délai min entre 2 nouvelles entrées globales
        - max_new_trades_per_cycle: limite de nouvelles entrées par cycle (sûreté)

        Ne persiste rien: garde mémoire en RAM via attributs.
        """

        try:
            # Mémoire RAM
            if not hasattr(self, "_last_trade_ts_by_asset"):
                self._last_trade_ts_by_asset = {}
            if not hasattr(self, "_cycle_new_trades"):
                self._cycle_new_trades = 0
            if not hasattr(self, "_last_any_trade_ts"):
                self._last_any_trade_ts = 0.0

            # 1) Limite par cycle
            if self._cycle_new_trades >= max_new_trades_per_cycle:
                self.logger.info(
                    f"[THROTTLE] Limite par cycle atteinte ({max_new_trades_per_cycle})."
                )
                return True

            # 2) Gap global
            if (
                self._last_any_trade_ts
                and (now_ts - self._last_any_trade_ts) < min_gap_any_trade_s
            ):
                gap = min_gap_any_trade_s - (now_ts - self._last_any_trade_ts)
                self.logger.info(f"[THROTTLE] Gap global actif ~{gap:.1f}s.")
                return True

            # 3) Cooldown par asset
            last_ts = self._last_trade_ts_by_asset.get(asset, 0.0)
            if last_ts and (now_ts - last_ts) < per_asset_cooldown_s:
                gap = per_asset_cooldown_s - (now_ts - last_ts)
                self.logger.info(f"[THROTTLE] Cooldown {asset} encore ~{gap:.1f}s.")
                return True

            return False
        except Exception as e:
            self.logger.warning(f"[THROTTLE] Guard erreur (ignore): {e}")
            return False

    def _mark_trade_sent(self, asset: str, now_ts: float) -> None:
        """À appeler juste APRÈS un envoi d’ordre réussi."""
        if not hasattr(self, "_last_trade_ts_by_asset"):
            self._last_trade_ts_by_asset = {}
        if not hasattr(self, "_cycle_new_trades"):
            self._cycle_new_trades = 0
        self._last_trade_ts_by_asset[asset] = now_ts
        self._last_any_trade_ts = now_ts
        self._cycle_new_trades += 1

    def _split_multi_tp_orders(
        self,
        trade_decision: dict,
        config: dict,
        volume: float,
        entry_price_market: float,
        sl_price: float,
        tp_prices: list,
        symbol_info: Any,
        trigger_price: Optional[float] = None,
        order_type_str: str = "MARKET",
    ) -> list[dict]:
        """
        Si la stratégie fournit plusieurs TP (ex: [tp1, tp2]),
        on split le volume en plusieurs ordres (50/50 par défaut).
        Chaque ordre est construit via _build_mt5_request.
        """
        # 🚫 Cas spécial Burst → jamais de TP
        rule = str(trade_decision.get("rule_name", "")).lower()
        if rule == "burst_scalping":
            trade_decision.pop("tp_price", None)  # nettoyage
            return [
                self._build_mt5_request(
                    trade_decision,
                    config,
                    volume,
                    entry_price_market,
                    sl_price,
                    None,  # pas de TP (trailing only)
                    symbol_info,
                    trigger_price,
                    order_type_str,
                )
            ]

        if not isinstance(tp_prices, list) or len(tp_prices) <= 1:
            # un seul TP → on passe par _build_mt5_request classique
            return [
                self._build_mt5_request(
                    trade_decision,
                    config,
                    volume,
                    entry_price_market,
                    sl_price,
                    tp_prices[0] if tp_prices else 0.0,
                    symbol_info,
                    trigger_price,
                    order_type_str,
                )
            ]

        # === Split volume en parts égales ===
        sub_vol = round(volume / len(tp_prices), 2)
        requests = []

        for tp in tp_prices:
            if not tp or tp <= 0:
                continue
            req = self._build_mt5_request(
                trade_decision,
                config,
                sub_vol,
                entry_price_market,
                sl_price,
                tp,
                symbol_info,
                trigger_price,
                order_type_str,
            )
            # On marque le TP spécifique dans le commentaire
            req["comment"] = f"{req.get('comment','')}|TP@{tp:.5f}"
            requests.append(req)

        return requests

    def _build_mt5_request(
        self,
        trade_decision: dict,
        config: dict,
        volume: float,
        entry_price_market: float,
        sl_price: float,
        tp_price: float,
        symbol_info: Any,
        trigger_price: Optional[float] = None,
        order_type_str: str = "MARKET",
    ) -> dict:
        """
        Construit et valide la requête finale pour l'API MetaTrader 5, en supportant
        Market / Limit / Stop, avec contrôles durcis (style desk).
        - Arrondis aux digits
        - Distances mini broker (stops_level / trade_stops_level)
        - Cohérence directionnelle prix/SL/TP (vs price_ref)
        - Normalisation volume (min/step/max) par FLOOR (jamais de dépassement)
        - Deviation/Filling policy robustes
        - Expiration (GTC/DAY/SPECIFIED)
        - Métadonnées d’audit & compliance_flags (ignorées par MT5)

        Lève TradeExecutionError en cas d’invalidité bloquante.
        """
        # ——— imports/fallback locaux (évite NameError) ———
        import math, re
        from datetime import datetime, timezone, timedelta

        try:
            TradeExecutionError
        except NameError:

            class TradeExecutionError(Exception):
                pass

        # ——— accès MT5 robuste ———
        mt5 = None
        try:
            import MetaTrader5 as _mt5  # type: ignore

            mt5 = _mt5
        except Exception:
            mt5 = getattr(getattr(self, "mt5_connector", None), "mt5", None)

        self.logger.info("Construction de la requête MT5 finale...")

        # --- Validations de base ---
        action_str = str(trade_decision.get("action", "")).upper()  # BUY / SELL
        expected_symbol = str(trade_decision.get("asset", "") or "N/A")

        if action_str not in ("BUY", "SELL"):
            raise TradeExecutionError(
                f"Action invalide: '{action_str}' (attendu BUY/SELL)."
            )

        if (
            (not symbol_info)
            or (not getattr(symbol_info, "name", None))
            or str(symbol_info.name).upper() == "UNKNOWN"
        ):
            raise TradeExecutionError(
                f"Symbole MT5 invalide ou non résolu (asset={expected_symbol}, symbol_info={getattr(symbol_info, 'name', 'None')})."
            )

        # Broker units
        digits = int(getattr(symbol_info, "digits", 0) or 0)
        point = float(getattr(symbol_info, "point", 0.0) or 0.0)
        if not (point > 0):
            raise TradeExecutionError("symbol_info.point invalide (<= 0).")

        # stops_level (clé broker tolérante)
        stops_lvl_points = float(
            getattr(symbol_info, "trade_stops_level", 0)
            or getattr(symbol_info, "stops_level", 0)
            or 0
        )
        min_stop_distance_price = stops_lvl_points * point

        # --- Volume normalisé (FLOOR sur le step, clamp min/max) ---
        vmin = float(getattr(symbol_info, "volume_min", 0.0) or 0.0)
        vmax = float(getattr(symbol_info, "volume_max", float("inf")) or float("inf"))
        vstep = float(getattr(symbol_info, "volume_step", 0.0) or 0.0)

        if not isinstance(volume, (int, float)) or volume <= 0:
            raise TradeExecutionError(f"Volume invalide ({volume}).")

        vol = float(volume)
        vol = max(vmin, min(vmax, vol))
        if vstep and vstep > 0:
            # FLOOR: ne jamais surdimensionner par rapport au sizing
            steps = math.floor((vol - vmin) / vstep + 1e-12)
            vol = vmin + steps * vstep
            if vol > vmax:
                vol = vmax
        if vol < vmin or vol <= 0:
            raise TradeExecutionError(f"Volume après normalisation invalide ({vol}).")

        self.logger.info(
            f"[VOLUME] avant={volume} -> après={vol} (min={vmin}, step={vstep}, max={vmax})"
        )

        # --- Prix d’entrée & niveaux SL/TP arrondis ---
        if not isinstance(entry_price_market, (int, float)) or entry_price_market <= 0:
            raise TradeExecutionError("Prix d'entrée marché invalide.")
        entry_price_market = round(float(entry_price_market), digits)

        try:
            sl_price = round(float(sl_price), digits)
            if tp_price is not None:
                tp_price = round(float(tp_price), digits)
        except Exception as e:
            raise TradeExecutionError(f"SL/TP invalides: {e}")

        # --- Override LiquidityStrategy: utiliser prix absolus si fournis ---
        if (
            "entry_price" in trade_decision
            and float(trade_decision["entry_price"] or 0) > 0
        ):
            entry_price_market = round(float(trade_decision["entry_price"]), digits)
        if "sl_price" in trade_decision and float(trade_decision["sl_price"] or 0) > 0:
            sl_price = round(float(trade_decision["sl_price"]), digits)
        if "tp_price" in trade_decision and float(trade_decision["tp_price"] or 0) > 0:
            tp_price = round(float(trade_decision["tp_price"]), digits)

        # --- Mapping constantes MT5 (tolérant) ---
        if mt5 is None:
            raise TradeExecutionError("Module/constantes MT5 indisponibles.")

        # Actions
        mt5_action_deal = getattr(mt5, "TRADE_ACTION_DEAL", None)
        mt5_action_pending = getattr(mt5, "TRADE_ACTION_PENDING", None)
        if mt5_action_deal is None or mt5_action_pending is None:
            raise TradeExecutionError("Constantes MT5 TRADE_ACTION introuvables.")

        # Types marché
        ORDER_TYPE_BUY = getattr(mt5, "ORDER_TYPE_BUY", None)
        ORDER_TYPE_SELL = getattr(mt5, "ORDER_TYPE_SELL", None)
        if ORDER_TYPE_BUY is None or ORDER_TYPE_SELL is None:
            raise TradeExecutionError(
                "Constantes MT5 ORDER_TYPE BUY/SELL introuvables."
            )

        # Mappings optionnels
        order_map = (getattr(self, "mt5_mappings", {}) or {}).get(
            "order_types", {}
        ) or {}
        fill_map = (getattr(self, "mt5_mappings", {}) or {}).get(
            "order_filling_policies", {}
        ) or {}
        time_map = (getattr(self, "mt5_mappings", {}) or {}).get(
            "order_time_flags", {}
        ) or {}

        # Time flags
        ORDER_TIME_GTC = getattr(mt5, "ORDER_TIME_GTC", None)
        ORDER_TIME_DAY = getattr(
            mt5,
            time_map.get("DAY", "ORDER_TIME_DAY"),
            getattr(mt5, "ORDER_TIME_DAY", None),
        )
        ORDER_TIME_SPECIFIED = getattr(
            mt5,
            time_map.get("SPECIFIED", "ORDER_TIME_SPECIFIED"),
            getattr(mt5, "ORDER_TIME_SPECIFIED", None),
        )
        if ORDER_TIME_GTC is None:
            raise TradeExecutionError("Constante MT5 ORDER_TIME_GTC introuvable.")

        # Filling policy
        filling_policy_str = str(
            config.get("execution_policy", {}).get("type_filling", "FOK")
        ).upper()
        mt5_filling_policy = getattr(
            mt5,
            fill_map.get(filling_policy_str, "ORDER_FILLING_FOK"),
            getattr(mt5, "ORDER_FILLING_FOK", None),
        )
        if mt5_filling_policy is None:
            raise TradeExecutionError("Constante MT5 filling policy introuvable.")

        # Déviation (points)
        deviation_points = int(
            config.get("execution_policy", {}).get("max_deviation_points", 20) or 20
        )
        if deviation_points < 0:
            deviation_points = 0

        # --- Patch: neutraliser TP pour burst_scalping & flag has_tp ---
        rule = str(trade_decision.get("rule_name", "")).lower()
        if rule == "burst_scalping" and (tp_price is None or tp_price <= 0):
            tp_price = 0.0  # MT5 = pas de TP
            self.logger.debug("[BURST] TP neutralisé → trailing stop only")
        has_tp = bool(tp_price and tp_price > 0)

        # --- Basket/comment (une seule logique, sans double-écriture) ---
        raw_comment = str(trade_decision.get("comment") or "")
        basket_id = str(trade_decision.get("basket_id") or "").strip() or None
        if not basket_id and raw_comment:
            m = re.search(r"basket=([A-Za-z0-9_]+)", raw_comment)
            if m:
                basket_id = m.group(1)
        if not basket_id:
            sym_name = getattr(symbol_info, "name", "") or expected_symbol
            basket_id = f"burst_{str(sym_name).upper()}"

        def _normalize_mt5_comment(text: str, max_len: int = 31) -> str:
            raw = (text or "").strip()
            raw = raw.replace("|", "").replace(" ", "")
            raw = re.sub(r"[^A-Za-z0-9._-]", "", raw)
            return raw[:max_len]

        comment = _normalize_mt5_comment(raw_comment or basket_id)

        # --- Construction base requête ---
        order_type_str = str(order_type_str or "MARKET").upper()
        request = {
            "symbol": symbol_info.name,
            "volume": float(vol),
            "magic": trade_decision.get(
                "magic_number",
                config.get(
                    "magic_number",
                    self.config_manager.get("trade_executor_settings", {}).get(
                        "magic", 0
                    ),
                ),
            ),
            "sl": float(sl_price),
            "type_time": ORDER_TIME_GTC,
            "deviation": int(deviation_points),
            "comment": comment,  # court, stable, peut contenir basket_id
            # — meta (ignorés par MT5) —
            "strategy_type": str(config.get("strategy_name", "unknown")).lower(),
            "rule_name": str(trade_decision.get("rule_name", "")),
            "meta_rr_projected": (
                trade_decision.get("meta_rr_projected")
                or trade_decision.get("rr")
                or trade_decision.get("rr_effective")
            ),
            "basket_id": basket_id,
            "is_burst_trade": (rule == "burst_scalping"),
        }
        if has_tp:
            request["tp"] = float(tp_price)  # seulement si TP actif

        # --- Timeout & mitigation (meta only) ---
        request["_meta_timeout_bars"] = int(trade_decision.get("timeout_bars", 0) or 0)
        request["_meta_use_mitigation"] = bool(
            trade_decision.get("use_mitigation", False)
        )

        # --- Détermination du type d’ordre et prix de référence ---
        if order_type_str == "MARKET":
            request["action"] = mt5_action_deal
            request["type"] = ORDER_TYPE_BUY if action_str == "BUY" else ORDER_TYPE_SELL
            request["price"] = entry_price_market
            request["type_filling"] = mt5_filling_policy
            price_ref = float(request["price"])

        elif order_type_str in ("BUY_LIMIT", "SELL_LIMIT", "BUY_STOP", "SELL_STOP"):
            request["action"] = mt5_action_pending
            mapped = order_map.get(order_type_str, f"ORDER_TYPE_{order_type_str}")
            order_type_const = getattr(mt5, mapped, None)
            if order_type_const is None:
                raise TradeExecutionError(
                    f"Type d'ordre différé non supporté: '{order_type_str}'."
                )
            request["type"] = order_type_const

            # Trigger proposé ou fallback marché
            trig = (
                trigger_price
                if isinstance(trigger_price, (int, float)) and trigger_price > 0
                else entry_price_market
            )
            trig = round(float(trig), digits)

            # Tick pour validations
            tick = getattr(self.mt5_connector, "get_symbol_info_tick", None)
            tick = (
                tick(symbol_info.name)
                if callable(tick)
                else getattr(mt5, "symbol_info_tick", lambda s: None)(symbol_info.name)
            )
            if not tick or not hasattr(tick, "ask") or not hasattr(tick, "bid"):
                raise TradeExecutionError(f"Tick invalide pour {symbol_info.name}.")
            ask = float(getattr(tick, "ask") or 0.0)
            bid = float(getattr(tick, "bid") or 0.0)
            if not (ask > 0 and bid > 0 and ask > bid):
                raise TradeExecutionError(
                    f"Prix marché invalides (ask/bid) pour {symbol_info.name}."
                )

            # Règles MT5: distances min par type, côté BID/ASK
            too_close = (
                (
                    order_type_str == "BUY_LIMIT"
                    and not (trig < ask - min_stop_distance_price)
                )
                or (
                    order_type_str == "SELL_LIMIT"
                    and not (trig > bid + min_stop_distance_price)
                )
                or (
                    order_type_str == "BUY_STOP"
                    and not (trig > ask + min_stop_distance_price)
                )
                or (
                    order_type_str == "SELL_STOP"
                    and not (trig < bid - min_stop_distance_price)
                )
            )
            if too_close:
                raise TradeExecutionError(
                    f"{order_type_str}: trigger {trig:.{digits}f} trop proche du marché "
                    f"(ask={ask:.{digits}f}, bid={bid:.{digits}f}, min={min_stop_distance_price:.{digits}f})."
                )

            request["price"] = trig
            price_ref = float(trig)

            # Expiration
            expiration_policy = str(
                (config.get("order_expiration_policy") or {}).get("type", "GTC")
            ).upper()
            if expiration_policy == "DAY" and ORDER_TIME_DAY is not None:
                request["type_time"] = ORDER_TIME_DAY
            elif expiration_policy == "SPECIFIED" and ORDER_TIME_SPECIFIED is not None:
                request["type_time"] = ORDER_TIME_SPECIFIED
                exp_str = (config.get("order_expiration_policy") or {}).get(
                    "datetime",
                    (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
                )
                try:
                    if isinstance(exp_str, (int, float)):
                        request["expiration"] = int(exp_str)
                    else:
                        exp_dt = datetime.fromisoformat(str(exp_str))
                        if exp_dt.tzinfo is None:
                            exp_dt = exp_dt.replace(tzinfo=timezone.utc)
                        request["expiration"] = int(exp_dt.timestamp())
                except Exception:
                    self.logger.error(f"Expiration invalide: {exp_str}. Fallback GTC.")
                    request["type_time"] = ORDER_TIME_GTC
        else:
            raise TradeExecutionError(f"Type d'ordre non géré: '{order_type_str}'")

        # --- Distances min broker (SL/TP vs price_ref) ---
        if min_stop_distance_price > 0:
            if action_str == "BUY":
                if (price_ref - sl_price) < min_stop_distance_price - 1e-12:
                    raise TradeExecutionError(
                        f"SL trop proche: Δ={price_ref - sl_price:.{digits}f} < min {min_stop_distance_price:.{digits}f}."
                    )
                if has_tp and (tp_price - price_ref) < min_stop_distance_price - 1e-12:
                    raise TradeExecutionError(
                        f"TP trop proche: Δ={tp_price - price_ref:.{digits}f} < min {min_stop_distance_price:.{digits}f}."
                    )
            else:  # SELL
                if (sl_price - price_ref) < min_stop_distance_price - 1e-12:
                    raise TradeExecutionError(
                        f"SL trop proche: Δ={sl_price - price_ref:.{digits}f} < min {min_stop_distance_price:.{digits}f}."
                    )
                if has_tp and (price_ref - tp_price) < min_stop_distance_price - 1e-12:
                    raise TradeExecutionError(
                        f"TP trop proche: Δ={price_ref - tp_price:.{digits}f} < min {min_stop_distance_price:.{digits}f}."
                    )

        # --- Commentaire & tags succincts (template optionnel) ---
        comment_tpl = self.config_manager.get(
            "trading.order_comment_template", "SNIPER_X|{strategy}|{order_type}"
        )
        max_len = int(self.config_manager.get("trading.comment_max_length", 31) or 31)
        strategy_tag = str(config.get("strategy_name", "N/A"))
        rule_name = str(trade_decision.get("rule_name", "") or "")
        rr_proj = (
            trade_decision.get("meta_rr_projected")
            or trade_decision.get("rr")
            or trade_decision.get("rr_effective")
        )

        # Defaults (sécurité)
        request.setdefault(
            "action",
            mt5_action_deal if order_type_str == "MARKET" else mt5_action_pending,
        )
        request.setdefault("type_time", ORDER_TIME_GTC)

        # Meta compliance/audit
        request["_meta_rule_name"] = rule_name
        request["_meta_action"] = action_str
        request["_meta_rr"] = rr_proj
        request["_meta_stops_level_points"] = stops_lvl_points
        request["_meta_point"] = point
        request["_compliance_flags"] = {
            "direction_ok": True,
            "stops_ok": True,
            "price_digits_ok": True,
            "order_type": order_type_str,
        }

        request["strategy_type"] = str(config.get("strategy_name", "unknown")).lower()
        request["rule_name"] = rule_name or config.get("rule_name", "")
        request["meta_rr_projected"] = rr_proj

        self.logger.debug(f"Requête MT5 construite et validée : {request}")
        return request

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
        - Aucun TP logique (TP=0.0 côté MT5, trailing géré par le moteur)
        - SL obligatoire, arrondi aux digits et respect des distances stops_level
        - Volume normalisé (FLOOR sur volume_step) et clampé [min, max]
        - Commentaire court & canonique (<=31, ASCII), compatible avec l’extracteur de panier
        - Trailing config stockée en meta (_meta_trailing)
        - Remontées d’erreurs cohérentes via TradeExecutionError
        """
        import math, re, secrets

        # --- Fallback TradeExecutionError si pas importé ---
        try:
            TradeExecutionError
        except NameError:

            class TradeExecutionError(Exception):
                pass

        # --------- Validations d'entrée ---------
        if not isinstance(trade_decision, dict):
            raise TradeExecutionError("trade_decision invalide (attendu dict).")

        action = str((trade_decision.get("action") or "").upper()).strip()
        if action not in {"BUY", "SELL"}:
            raise TradeExecutionError(f"[BURST] Action invalide: '{action}'.")

        # ⚠️ symbol_info peut ne pas avoir .name => on ne bloque pas là-dessus
        if not symbol_info:
            raise TradeExecutionError("[BURST] symbol_info manquant.")

        # Lecture robuste de digits/point (obligatoires pour les calculs)
        try:
            digits = int(getattr(symbol_info, "digits", 0) or 0)
            point = float(getattr(symbol_info, "point", 0.0) or 0.0)
        except Exception:
            raise TradeExecutionError("[BURST] symbol_info illisible (digits/point).")

        if digits <= 0 or point <= 0.0:
            raise TradeExecutionError(
                "[BURST] symbol_info invalide: digits/point <= 0."
            )

        # Résolution robuste du nom symbole (fallbacks successifs)
        symbol_name = str(
            getattr(symbol_info, "name", "") or getattr(symbol_info, "symbol", "")
        ).strip()
        if not symbol_name:
            # fallback depuis la décision si le broker ne fournit pas un name propre
            symbol_name = str(
                trade_decision.get("asset") or trade_decision.get("symbol") or ""
            ).strip()

        if not symbol_name:
            raise TradeExecutionError(
                "[BURST] Impossible de déterminer le symbole (name/symbol/asset)."
            )

        # Normalisation
        symbol_name = symbol_name.upper()

        # --------- Normalisation volume (FLOOR) ---------
        try:
            vmin = float(getattr(symbol_info, "volume_min", 0.0) or 0.0)
            vmax = float(
                getattr(symbol_info, "volume_max", float("inf")) or float("inf")
            )
            vstep = float(getattr(symbol_info, "volume_step", 0.0) or 0.0)
        except Exception:
            vmin, vmax, vstep = 0.0, float("inf"), 0.0

        if not isinstance(volume, (int, float)) or volume <= 0:
            raise TradeExecutionError(f"[BURST] Volume invalide ({volume}).")

        vol = float(max(vmin, min(vmax, float(volume))))
        if vstep and vstep > 0:
            steps = math.floor((vol - vmin) / vstep + 1e-12)
            vol = max(vmin, vmin + steps * vstep)
            if vol > vmax:
                vol = vmax
        if vol < vmin or vol <= 0:
            raise TradeExecutionError(f"[BURST] Volume normalisé invalide ({vol}).")

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
            elif (
                action == "SELL" and (sl_price - entry_price) < min_stop_distance_price
            ):
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
            raise TradeExecutionError(
                "[BURST] Constantes MT5 (action/type) introuvables."
            )

        # --------- Deviation & magic ---------
        # Cohérent avec _build_mt5_request : on lit d'abord execution_policy, sinon fallback.
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

        # --------- Basket/Comment (compatible extracteur) ---------
        # Priorité: basket_id fourni → sinon génère "burst_<SYMBOL>_<hex6>".
        basket_id = str(trade_decision.get("basket_id") or "").strip()
        if not basket_id:
            # forme courte compatible avec le regex: r"(burst_[A-Z]{3,6}_[a-f0-9]{6,})"
            hex6 = secrets.token_hex(3)
            sym_up = re.sub(r"[^A-Z]", "", str(symbol_name).upper())[:6] or "ASSET"
            basket_id = f"burst_{sym_up}_{hex6}"

        # Commentaire MT5 (≤31, ASCII sûr), **met directement le basket_id** pour un parsing fiable.
        comment = basket_id
        comment = comment.replace(" ", "").replace("|", "")
        comment = re.sub(r"[^A-Za-z0-9._-]", "", comment)[:31]

        # --------- Trailing config (prend depuis decision > config) ---------
        trailing_cfg = (
            trade_decision.get("trailing")
            or (
                ((config.get("entry_rules", {}) or {}).get("scalping", {}) or {}).get(
                    "burst_scalping", {}
                )
                or {}
            )
            .get("tp_sl", {})
            .get("trailing")
            or ((config.get("burst_scalping", {}) or {}).get("tp_sl", {}) or {}).get(
                "trailing"
            )
            or {"enabled": True}
        )

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
            "comment": comment,  # court, stable et parsable
            "strategy_type": (
                str(config.get("strategy_name", "burst_scalping")).lower()
            ),
            "rule_name": str(trade_decision.get("rule_name", "burst_scalping")),
            # --- meta (ignorés par MT5) ---
            "_meta_no_tp": True,
            "_meta_trailing": trailing_cfg,
            "_meta_entry_source": trade_decision.get("source", "core_decision"),
            "_meta_stops_level_points": stops_lvl_points,
            "_meta_point": point,
            "_meta_digits": digits,
            # tags burst utiles aux audits
            "basket_id": basket_id,
            "burst_size": trade_decision.get("burst_size"),
            "burst_index": trade_decision.get("burst_index"),
            "is_burst_trade": True,
        }

        # Compliance flags pour nos logs
        request["_compliance_flags"] = {
            "direction_ok": True,
            "stops_ok": True,
            "order_type": "MARKET_DEAL_BURST",
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
                fn = getattr(
                    self.mt5_connector, "reconnect_if_needed", None
                ) or getattr(self.mt5_connector, "connect", None)
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

        # --------- 2) Volume (normalisation min/step/max - floor) ---------
        # volume direct (single-master) ou défaut config
        try:
            volume = float(td.get("volume"))
            if not math.isfinite(volume) or volume <= 0:
                raise ValueError("volume<=0")
        except Exception:
            try:
                te_settings = (active_config or {}).get(
                    "trade_executor_settings", {}
                ) or {}
                volume = float(te_settings.get("default_volume", 0.01))
            except Exception:
                volume = 0.01

        try:
            vmin = float(getattr(si, "volume_min", 0.01) or 0.01)
            vmax = float(getattr(si, "volume_max", 100.0) or 100.0)
            vstep = float(getattr(si, "volume_step", 0.01) or 0.01)
        except Exception:
            vmin, vmax, vstep = 0.01, 100.0, 0.01

        vol = max(vmin, min(vmax, float(volume)))
        if vstep > 0:
            steps = math.floor((vol - vmin) / vstep + 1e-12)
            vol = vmin + steps * vstep
            if vol < vmin:
                vol = vmin
        if vol <= 0:
            return {"status": "failed", "reason": f"invalid_volume_after_norm:{vol}"}

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
                (getattr(self, "mt5_mappings", {}) or {})
                .get("trade_retcodes", {})
                .items()
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

    def _update_internal_position_state(
        self, mt5_result: Any, initial_risk: float
    ) -> None:
        """
        Met à jour le dictionnaire interne des positions ouvertes de manière centralisée.
        """
        position_data = {
            "ticket": mt5_result.deal,
            "symbol": mt5_result.request.symbol,
            "type": mt5_result.request.type,
            "volume": mt5_result.volume,
            "entry_price": mt5_result.price,
            "sl": mt5_result.sl,
            "tp": mt5_result.tp,
            "magic": mt5_result.request.magic,
            "comment": mt5_result.request.comment,
            "open_time": datetime.now(UTC).isoformat(),
            "initial_risk_usd": initial_risk,
        }
        # La clé du dictionnaire est le 'deal' (ticket de la transaction)
        self._open_positions[mt5_result.deal] = position_data
        self.logger.debug(
            f"État interne mis à jour pour la nouvelle position #{mt5_result.deal}."
        )

    def monitor_pending_orders(self) -> None:
        """
        Surveille les ordres LIMIT Liquidity et annule ceux qui dépassent le timeout_bars.
        À appeler à chaque cycle du pipeline.
        """

        to_remove = []

        for order_id, order_data in list(self._open_positions.items()):
            try:
                timeout_bars = int(order_data.get("_meta_timeout_bars", 0) or 0)

                if timeout_bars > 0 and order_data.get("type") in (
                    mt5.ORDER_TYPE_BUY_LIMIT,
                    mt5.ORDER_TYPE_SELL_LIMIT,
                ):
                    opened_at = order_data.get("open_time")
                    bars_elapsed = self._bars_since(opened_at)

                    if bars_elapsed >= timeout_bars:
                        self.logger.info(
                            f"[LIQUIDITY] ⏱ Timeout {timeout_bars} barres atteint → annulation de l’ordre LIMIT #{order_id}."
                        )
                        cancel_request = {
                            "action": mt5.TRADE_ACTION_REMOVE,
                            "order": order_id,
                            "symbol": order_data["symbol"],
                        }
                        result = self.mt5_connector.mt5.order_send(cancel_request)

                        if not result or result.retcode != mt5.TRADE_RETCODE_DONE:
                            self.logger.warning(
                                f"[LIQUIDITY] ❌ Échec annulation ordre LIMIT #{order_id}, retcode={getattr(result,'retcode','N/A')}."
                            )
                        else:
                            self.logger.info(
                                f"[LIQUIDITY] ✅ Ordre LIMIT #{order_id} annulé."
                            )
                            to_remove.append(order_id)

            except Exception as e:
                self.logger.warning(f"[LIQUIDITY] Erreur monitor_pending_orders: {e}")

        # Nettoyage des ordres annulés
        for oid in to_remove:
            self._open_positions.pop(oid, None)

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
                            if pos_type == getattr(mt5, "ORDER_TYPE_BUY", 0)
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
                                until = time.time() + cd
                                self._cooldown_until[str(sym_for_cooldown).upper()] = (
                                    until
                                )
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
                    "BUY" if typ0 == getattr(mt5, "ORDER_TYPE_BUY", 0) else "SELL"
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
                        trigger_pips = _safe_float(
                            trailing_cfg.get("trigger_pips"), 10.0
                        )
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
                    _mh_dyn = self.config_manager.get(
                        "dynamic_trailing.min_hold_ms", None
                    )
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
                                    datetime.fromisoformat(str(iso)).timestamp()
                                    * 1000.0
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
                if max_spread_pts > 0 and self._basket_trail_armed.get(
                    basket_id, False
                ):
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
                        self.config_manager.get(
                            "dynamic_trailing.backoff_step_points", 0
                        )
                        or 0.0
                    )
                except Exception:
                    backoff_step = 0.0
                if backoff_step > 0.0:
                    target_stop_pips = (
                        int(target_stop_pips / backoff_step) * backoff_step
                    )

                # Kill-switch PULLBACK max
                try:
                    max_pullback_pts = float(
                        self.config_manager.get(
                            "dynamic_trailing.max_pullback_points", 0
                        )
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
                    stops_level_points = _safe_float(
                        getattr(si, "stops_level", None), 0.0
                    )
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
                            lim = (
                                getattr(tick, "bid", None) or ref_price
                            ) - min_dist_price
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
                            lim = (
                                getattr(tick, "ask", None) or ref_price
                            ) + min_dist_price
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
                        tickets_str = ",".join(
                            str(tk) for tk, _ in updated_tickets[:10]
                        )
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

    def _modify_sl(self, ticket: int, new_sl: float):
        """Envoie une requête de modification de SL au broker."""
        try:
            request = {
                "action": self.TRADE_ACTION_MODIFY,
                "position": ticket,
                "sl": new_sl,
            }
            result = self.mt5_connector.order_send(request)
            if result and result.retcode == self.TRADE_RETCODE_DONE:
                self.logger.info(f"Trailing SL modifié pour pos#{ticket} -> {new_sl}")
                self._open_positions[ticket]["sl"] = new_sl
            else:
                self.logger.warning(f"Échec modif trailing SL pour pos#{ticket}")
        except Exception as e:
            self.logger.error(f"Erreur _modify_sl: {e}", exc_info=True)

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
        import time
        from datetime import datetime, timezone as _tz

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
                        "open_time": datetime.now(_tz.utc).isoformat(),
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

    def _send_close_order_with_retries(self, request: dict) -> Optional[Any]:
        """
        Fonction d'aide qui envoie un ordre de clôture avec une logique de retry.
        """
        retcode_actions = self.config_manager.get(
            "mt5_mappings.trade_retcode_actions", {}
        )
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
        import time, re
        from datetime import datetime, timezone as _tz

        UTC = _tz.utc
        self.logger.info(f"[CLOSE] Demande: symbol='{symbol}', ticket='{ticket}'")

        # --------- helpers MT5 / mapping sûrs ----------
        mt5 = getattr(getattr(self, "mt5_connector", None), "mt5", None) or getattr(
            self, "mt5", None
        )
        if mt5 is None:
            try:
                import MetaTrader5 as _mt5  # type: ignore

                mt5 = _mt5
            except Exception:
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
            # Toujours tenter de rafraîchir l'état interne si dispo
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
                if symbol == "ALL":
                    broker_positions = list(mt5.positions_get() or [])
                else:
                    broker_positions = list(mt5.positions_get(symbol=symbol) or [])
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

        # Fallback interne si broker ne renvoie rien mais on a un ticket dans _open_positions
        if (
            not positions_to_close
            and ticket is not None
            and hasattr(self, "_open_positions")
        ):
            op = self._open_positions.get(ticket)
            if isinstance(op, dict):
                positions_to_close = [op]

        if not positions_to_close:
            msg = f"Aucune position à clôturer (symbol='{symbol}', ticket='{ticket}')."
            self.logger.info(f"[CLOSE] {msg}")
            return {
                "success": True,
                "message": msg,
                "closed_count": 0,
                "failed_count": 0,
            }

        # --------- (2) Paramètres retry depuis la conf ----------
        tes = {}
        try:
            tes = self.config_manager.get("trade_executor_settings", {}) or {}
        except Exception:
            tes = {}

        base_deviation = int(tes.get("default_slippage", 20) or 20)
        max_attempts = int(
            tes.get("retry_attempts", 2) or 2
        )  # en plus de la 1re tentative
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
                order_type_close = OT_SELL
                side = "SELL"  # on vend pour fermer un BUY → prix = Bid
            elif ptype == POS_SELL:
                order_type_close = OT_BUY
                side = "BUY"  # on achète pour fermer un SELL → prix = Ask
            else:
                self.logger.error(
                    f"[CLOSE] Type position inconnu ({ptype}) ticket={tkt} → skip."
                )
                failed_count += 1
                continue

            # Premier prix
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

            # Requête basique
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

            # --- boucle d'envoi + retry ---
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

                    # Retryable ?
                    if attempt <= (1 + max_attempts) and rc in RETRYABLE_CODES:
                        # rafraîchir prix + augmenter déviation
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

                    # Échec définitif
                    self.logger.error(
                        f"[CLOSE] Échec ticket={tkt} rc={rc} ({rc_name}) comment={getattr(res,'comment','')}"
                    )
                    failed_count += 1
                    try:
                        # audit trail si dispo
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

            # Si pas de succès → passe à la position suivante
            if not last_result or getattr(last_result, "retcode", None) not in OK_CODES:
                continue

            # --- succès : calcul P&L exact au prix d'exécution ---
            close_price = float(
                getattr(last_result, "price", work["price"]) or work["price"]
            )
            try:
                # action pour calcul = sens initial (BUY si position BUY, SELL si position SELL)
                calc_action = OT_BUY if ptype == POS_BUY else OT_SELL
                pnl_usd = float(
                    mt5.order_calc_profit(
                        calc_action, sym, float(vol), float(entry), float(close_price)
                    )
                )
            except Exception:
                pnl_usd = float(pos.get("profit", 0.0) or 0.0)  # fallback
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

    def log_and_notify(self, mt5_request: dict, execution_status: dict) -> None:
        """
        Journalise l'événement de trade et envoie des notifications via des templates.
        Enrichit le contexte du template pour des notifications plus riches.

        Args:
            mt5_request (dict): La requête d'ordre MT5 qui a été tentée.
            execution_status (dict): Le résultat de la tentative d'exécution.
        """
        status = execution_status.get("status", "unknown")

        # Préparer le contexte pour le template de notification
        # Ajouter plus de variables (P&L, raison du rejet) au contexte du template (TODO implémenté)
        template_context = {
            "order_id": mt5_request.get("order_id", "N/A"),
            "action": mt5_request.get(
                "action", "N/A"
            ),  # L'action numérique MT5 (e.g. 1 pour BUY)
            "action_str": (
                "ACHAT"
                if mt5_request.get("action") == self.TRADE_ACTION_BUY
                else (
                    "VENTE"
                    if mt5_request.get("action") == self.TRADE_ACTION_SELL
                    else (
                        "CLOTURE"
                        if mt5_request.get("action") == self.TRADE_ACTION_CLOSE_BY
                        else "AUTRE"
                    )
                )
            ),  # Chaîne de l'action
            "volume": mt5_request.get("volume", "N/A"),
            "symbol": mt5_request.get("symbol", "N/A"),
            "price": mt5_request.get("price", "N/A"),
            "magic": mt5_request.get("magic", "N/A"),
            "sl": mt5_request.get("sl", "N/A"),
            "tp": mt5_request.get("tp", "N/A"),
            "message": execution_status.get("message", "N/A"),
            "retcode": execution_status.get("mt5_result", {}).get("retcode", "N/A"),
            "comment": execution_status.get("mt5_result", {}).get("comment", "N/A"),
            "deal": execution_status.get("mt5_result", {}).get("deal", "N/A"),
            "order": execution_status.get("mt5_result", {}).get("order", "N/A"),
            "pnl_usd": execution_status.get(
                "pnl_usd", "N/A"
            ),  # Si le P&L est calculé à ce stade (pour clôture)
            "account_id": mt5_request.get(
                "account_id", "N/A"
            ),  # L'ID de compte String de broker_accounts.json
            "broker_name": mt5_request.get("broker_name", "N/A"),  # Le nom du broker
            "timestamp_utc": datetime.now(UTC).isoformat(),
        }

        # Zéro Hard Coding : les messages sont des templates lus depuis la config
        template_key = f"telegram.templates.trade_execution.{status}"  # Nouveau chemin pour les templates d'exécution
        # Fallback pour le template, avec des clés formatées par template_context
        default_template = self.config_manager.get(
            template_key,
            f"**Événement de Trade Inconnu**\nStatut: {status}\nOrdre: {template_context['order_id']} | Symbole: {template_context['symbol']} | Message: {template_context['message']}",
        )

        # Formater le message en utilisant le dictionnaire de contexte préparé
        try:
            message = default_template.format(**template_context)
        except KeyError as e:
            self.logger.error(
                f"Erreur de formatage du template de notification pour '{status}'. Clé manquante: {e}. Utilisation du template par défaut."
            )
            message = default_template  # Revert au template par défaut si erreur de clé
        except Exception as e:
            self.logger.error(
                f"Erreur inattendue lors du formatage du template de notification: {e}. Utilisation du template par défaut.",
                exc_info=True,
            )
            message = default_template

        # Déterminer le canal d'alerte (plus de flexibilité)
        # Les canaux sont désormais spécifiés dans la section `telegram.trade_channels`
        alert_channel = self.config_manager.get(
            f"telegram.trade_channels.{status}", "telegram_critical"
        )  # Fallback critique

        self.config_manager.send_alert(message, alert_channel)

        # La logique d'audit _log_audit_trail ({ ... }) doit être appelée avec des données structurées.
        # Cette partie est déjà gérée dans `execute_order` et `close_position` de manière plus détaillée.
        # Ici, nous ne faisons qu'une journalisation de haut niveau si besoin.
        # _log_audit_trail est une méthode de TradeExecutor qui prend un dictionnaire d'entrée.

    # --- remplace ENTIEREMENT la méthode feedback_pipeline ---

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

    def manual_override_if_needed(self, mt5_request: dict) -> bool:
        """
        Déclenche le workflow "Human-in-the-Loop" de manière non-bloquante.
        Si les conditions sont remplies (volume élevé, etc.), envoie une alerte et met l'ordre en attente
        de validation externe. Implémente un "timeout" pour l'annulation automatique.

        Args:
            mt5_request (dict): La requête d'ordre MT5 à évaluer.

        Returns:
            bool: False, indiquant que le trade n'est pas approuvé immédiatement et est mis en attente.
                  True si aucune intervention n'est requise.
        """
        # Vérifier si l'override manuel est activé
        if not self.config_manager.get(
            "trade_executor_settings.manual_override_enabled", False
        ):
            self.logger.debug(
                "Override manuel désactivé. L'ordre sera exécuté directement."
            )
            return True  # Pas d'intervention, le trade est approuvé pour continuer

        volume = mt5_request.get("volume", 0.0)
        threshold = self.config_manager.get(
            "trade_executor_settings.manual_override_volume_threshold", 1.0
        )

        if volume < threshold:
            self.logger.debug(
                f"Volume ({volume:.2f} lots) sous le seuil d'override manuel ({threshold:.2f}). Pas d'intervention."
            )
            return True  # Volume sous le seuil, pas d'intervention

        order_id = mt5_request.get("order_id")
        self.logger.warning(
            f"Contrôle manuel requis pour l'ordre {order_id} (Volume: {volume:.2f}). Ordre mis en attente."
        )

        # Ajouter l'ordre au dictionnaire des ordres en attente
        # Inclure la date de demande et l'expiration du timeout
        manual_override_timeout_seconds = self.config_manager.get(
            "trade_executor_settings.manual_override_timeout_seconds", 60
        )
        timeout_time = datetime.now(UTC) + timedelta(
            seconds=manual_override_timeout_seconds
        )

        self._pending_orders[order_id] = {
            "request": mt5_request,
            "status": "pending_manual_approval",
            "timestamp_requested": datetime.now(UTC),
            "timeout_utc": timeout_time.isoformat(),  # Timestamp d'expiration
            "reason": f"Volume élevé: {volume:.2f} lots.",  # Raison pour le log interne
        }

        # Envoyer l'alerte via ConfigManager (qui gère l'envoi Telegram)
        self.config_manager.request_manual_override(
            reason=f"Volume élevé ({volume:.2f} lots) pour {mt5_request.get('symbol')}",
            trade_decision=mt5_request,  # Passer la requête MT5 pour plus de détails
            context={
                "volume": volume,
                "threshold": threshold,
            },  # Contexte additionnel pour l'audit
        )

        # Implémenter un "timeout" qui annule automatiquement l'ordre en attente
        # s'il n'est pas validé dans un délai configurable. (TODO implémenté - conceptuallement)
        # La vérification de ce timeout se fera par un processus externe ou dans un cycle régulier (run_bot.py)
        # qui iterera sur _pending_orders et vérifiera 'timeout_utc'.
        return False  # Le trade est mis en attente, il n'est pas approuvé pour exécution immédiate.

    def approve_pending_order(
        self, order_id: str, user: str, action: str = "APPROVE"
    ) -> dict:
        """
        Point d'entrée pour une approbation ou un rejet externe (ex: commande de bot Telegram).
        Traite un ordre en attente et l'envoie pour exécution ou le marque comme rejeté.

        Args:
            order_id (str): L'ID unique de l'ordre en attente.
            user (str): L'identifiant de l'opérateur qui a approuvé/rejeté.
            action (str): L'action à effectuer ('APPROVE' ou 'REJECT').

        Returns:
            dict: Statut de l'opération (succès/échec) et message.
        """
        self.logger.info(
            f"Requête d'action '{action}' reçue pour l'ordre en attente '{order_id}' par l'utilisateur '{user}'."
        )

        if order_id not in self._pending_orders:
            self.logger.warning(
                f"Tentative d'action '{action}' pour un ordre en attente inconnu ou déjà traité : {order_id}"
            )
            return {
                "status": "error",
                "message": "Order ID not found in pending list or already processed.",
            }

        order_data = self._pending_orders.pop(
            order_id
        )  # Retirer l'ordre de la liste des pending

        if action.upper() == "APPROVE":
            self.logger.info(
                f"Approbation manuelle reçue pour l'ordre {order_id} par l'utilisateur '{user}'. Envoi pour exécution..."
            )
            self.config_manager.log_manual_intervention(
                user,
                self.config_manager.ManualAction.TRADE_APPROVED.value,
                {"order_id": order_id, "request_snapshot": order_data["request"]},
            )  # Utilise Enum
            return self.execute_order(order_data["request"])
        elif action.upper() == "REJECT":
            self.logger.info(
                f"Rejet manuel reçu pour l'ordre {order_id} par l'utilisateur '{user}'. Ordre annulé."
            )
            self.config_manager.log_manual_intervention(
                user,
                self.config_manager.ManualAction.TRADE_REJECTED.value,
                {"order_id": order_id, "request_snapshot": order_data["request"]},
            )  # Utilise Enum
            # TODO: Envoyer une requête d'annulation au broker si l'ordre pending a été envoyé
            #       comme ordre différé et qu'il est toujours en attente côté broker.
            return {
                "status": "rejected",
                "message": f"Order {order_id} manually rejected.",
            }
        else:
            self.logger.warning(
                f"Action '{action}' non reconnue pour l'ordre en attente {order_id}. Aucune action prise."
            )
            # Remettre l'ordre si l'action n'est pas reconnue
            self._pending_orders[order_id] = order_data
            return {"status": "error", "message": "Invalid action for pending order."}

    def _load_and_filter_audit_data(
        self, report_date: datetime
    ) -> Optional[pd.DataFrame]:
        """
        Charge et filtre les données du journal d'audit pour un jour donné.
        Assure une gestion robuste des erreurs de lecture.

        Args:
            report_date (datetime): La date UTC pour laquelle générer le rapport.

        Returns:
            Optional[pd.DataFrame]: Le DataFrame filtré des entrées d'audit, ou None en cas d'échec.
        """
        try:
            # AMÉLIORATION PERFORMANCE: Lecture vectorisée du fichier JSONL, beaucoup plus rapide.
            # Assurez-vous que le fichier existe avant de tenter de lire
            if not self.audit_trail_path.exists():
                self.logger.warning(
                    f"Fichier d'audit introuvable à {self.audit_trail_path}. Aucun rapport généré pour le {report_date.date()}."
                )
                return None

            df = pd.read_json(self.audit_trail_path, lines=True)
            df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)  # Assure UTC

            report_date_utc = report_date.astimezone(
                UTC
            ).date()  # Comparaison sur la date seule
            filtered_df = df[df["timestamp"].dt.date == report_date_utc]

            self.logger.info(
                f"Chargé {len(filtered_df)} entrées d'audit pour le {report_date_utc}."
            )
            return filtered_df
        except (
            FileNotFoundError
        ):  # Redondant car vérifié au début, mais garde pour la robustesse
            self.logger.warning(
                f"Fichier d'audit introuvable à {self.audit_trail_path}. Aucun rapport généré."
            )
            return None
        except Exception as e:
            self.logger.error(
                f"Erreur lors de la lecture ou du filtrage du journal d'audit depuis {self.audit_trail_path} : {e}",
                exc_info=True,
            )
            self.config_manager.send_alert(
                "CRITIQUE",
                f"Erreur lecture journal audit: {e}",
                alert_type="telegram_critical",
            )
            return None

    def _export_report_data(self, df: pd.DataFrame, report_path: Path) -> bool:
        """
        Exporte les données du rapport de manière atomique et gère divers formats.
        Assure un formatage professionnel des nombres pour CSV et Markdown.

        Args:
            df (pd.DataFrame): Les données du rapport à exporter.
            report_path (Path): Le chemin complet du fichier de destination, incluant l'extension.

        Returns:
            bool: True si l'exportation réussit, False sinon.
        """
        temp_path = report_path.with_suffix(report_path.suffix + ".tmp")
        try:
            # S'assurer que le répertoire existe
            report_path.parent.mkdir(parents=True, exist_ok=True)

            report_format = report_path.suffix.lower().lstrip(".")
            if report_format == "csv":
                # Utilise une précision configurable pour les floats dans le CSV
                float_format_csv = self.config_manager.get(
                    "trade_executor_settings.report_float_format_csv", "%.5f"
                )
                df.to_csv(
                    temp_path,
                    index=False,
                    encoding="utf-8",
                    float_format=float_format_csv,
                )
            elif report_format == "jsonl":
                df.to_json(
                    temp_path,
                    orient="records",
                    lines=True,
                    date_format="iso",
                    default_handler=str,
                )
            elif report_format == "md":
                with open(temp_path, "w", encoding="utf-8") as f:
                    # Titre du rapport avec la date
                    report_title = self.config_manager.get(
                        "trade_executor_settings.report_title_template",
                        "# Rapport Quotidien des Trades - {date}",
                    )
                    f.write(
                        report_title.format(date=df["timestamp"].iloc[0].date())
                        + "\n\n"
                    )
                    f.write(df.to_markdown(index=False))  # Export en Markdown
            else:
                raise ValueError(f"Format de rapport non supporté : {report_format}")

            temp_path.rename(report_path)  # Rendre atomique
            self.logger.info(f"Rapport généré avec succès à : {report_path}")
            return True
        except Exception as e:
            self.logger.error(
                f"Échec de l'exportation du rapport vers {report_path} : {e}",
                exc_info=True,
            )
            if temp_path.exists():
                temp_path.unlink()  # Nettoyer le fichier temporaire en cas d'erreur
            # Envoyer une alerte critique si l'export du rapport échoue
            self.config_manager.send_alert(
                "CRITIQUE", f"Échec export rapport: {e}", alert_type="telegram_critical"
            )
            return False

    def generate_report(self, report_date: Optional[datetime] = None) -> Optional[Path]:
        """
        Génère un rapport quotidien d'activité à partir du journal d'audit.
        Le rapport peut être formaté en différents formats (CSV, JSONL, MD).

        Args:
            report_date (datetime, optional): La date pour laquelle générer le rapport.
                                            Par défaut, aujourd'hui en UTC.
        Returns:
            Optional[Path]: Le chemin vers le rapport généré, ou None en cas d'échec.
        """
        self.logger.info("Génération du rapport quotidien...")
        report_date = report_date if report_date else datetime.now(UTC)

        df = self._load_and_filter_audit_data(report_date)

        if df is None or df.empty:
            self.logger.info(
                f"Aucune donnée pour générer un rapport pour le {report_date.date()}. Retourne None."
            )
            return None

        # Récupérer le format de rapport depuis la configuration (dynamisé)
        report_format = self.config_manager.get(
            "trade_executor_settings.daily_report_format", "md"
        )

        # Le nom du fichier est construit avec la date et le format
        report_filename = (
            f"daily_trade_report_{report_date.strftime('%Y%m%d')}.{report_format}"
        )
        report_path = Path(
            self.config_manager.get("paths.reports", "output/")
        )  # Utilise le chemin des rapports
        full_report_filepath = report_path / report_filename

        # TODO: Ajouter des métriques de performance avancées (Profit Factor, Sharpe) au DataFrame avant export. (TODO maintenu)
        #       Ceci nécessiterait de calculer ces métriques à partir du `df` ici avant l'export.

        if self._export_report_data(
            df, full_report_filepath
        ):  # Passe le DataFrame et le chemin complet
            # Envoyer une alerte Telegram avec un résumé du rapport
            # Récupérer les métriques principales pour le message Telegram
            # Ceci est une simplification. Idéalement, _generate_report_trade_decisions devrait retourner
            # un dict de métriques que l'on pourrait utiliser ici.

            # Calcul rapide de quelques métriques pour le résumé Telegram
            total_pnl = (
                df[df["event_type"] == "TRADE_CLOSE"]["position_snapshot_at_close"]
                .apply(lambda x: x.get("profit", 0.0) if x else 0.0)
                .sum()
            )
            total_trades = (
                df[df["event_type"].isin(["TRADE_OPEN", "TRADE_CLOSE"])]
                .drop_duplicates(subset=["order_id"])
                .shape[0]
            )

            message_summary = (
                f"📊 **Rapport Quotidien des Trades - {report_date.date()}**\n\n"
            )
            message_summary += f"Trades exécutés: {total_trades}\n"
            message_summary += f"P&L Total (Estimé): `${total_pnl:.2f}`"

            # Limiter la taille du message pour Telegram
            message_summary = message_summary[:4000]  # Max length for Telegram messages

            self.config_manager.send_alert(
                message=message_summary,
                alert_type=self.config_manager.get(
                    "telegram.channels.telegram_daily_report", "telegram_info"
                ),  # Utiliser le canal configuré
            )
            return full_report_filepath

        # TODO: Utiliser un moteur de templates (ex: Jinja2) pour des rapports Markdown/HTML plus riches. (TODO maintenu)
        return None


def run_trade_execution_pipeline(
    trade_executor, decision_package: dict, is_dry_run: bool = False
) -> dict:
    """
    Pont unique entre la décision (DecisionPipeline) et l'exécution (TradeExecutor).
    Lit dans decision_package['final_decision'].
    Normalise/valide avant prepare_order.

    Ajouts/Corrections majeurs:
    - Import robuste de TradeExecutionError (fallback local si absent)
    - Garde-fou "fat-finger" volume (cap global / par actif)
    - Correctif structurel: les étapes 5→7 ne sont pas conditionnées par 'volume is not None'
    - Support is_dry_run: renvoie la requête/pre-payload sans exécuter d'ordre
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

    # ----------- 4) Construire le trade_decision standardisé -----------
    trade_decision = {
        "action": action,
        "asset": asset,
        "volume": final_decision.get("volume"),
        "order_type": order_type,
        "trigger_price": final_decision.get("trigger_price"),
        "target_sl_pips": final_decision.get("target_sl_pips"),
        "target_tp_pips": final_decision.get("target_tp_pips"),
        "rule_name": final_decision.get("rule_name"),
        "strategy_type": final_decision.get("strategy_type", "unknown"),
        "burst_enabled": bool(final_decision.get("burst_enabled", False)),
        "order_id": final_decision.get("order_id", "N/A"),
    }
    adapted_package = {
        "trade_decision": trade_decision,
        "market_context": market_context,
        "active_config": active_config,
    }

    # ----------- 4bis) Garde-fou volume (fat-finger hard cap) -----------
    try:
        te_settings = (active_config or {}).get("trade_executor_settings", {}) or {}
        ff = te_settings.get("fat_finger_check", {}) or {}
        per_asset_caps = ff.get("max_absolute_volume_for_asset", {}) or {}
        global_cap = float(te_settings.get("max_absolute_volume_safety", 10.0))
        asset_cap = float(per_asset_caps.get(asset, global_cap))
        hard_cap = min(asset_cap, global_cap)
    except Exception:
        hard_cap = 10.0  # fallback ultra conservateur

    vol = trade_decision.get("volume")
    if vol is not None:
        try:
            volf = float(vol)
        except Exception:
            reason = f"Volume invalide (non numérique): {vol!r}"
            logger.warning(reason)
            feedback = trade_executor.feedback_pipeline(
                order_id=trade_decision["order_id"], status="failed", reason=reason
            )
            trade_executor._feedback_safe(trade_decision, feedback)
            return {"status": "failed", "reason": reason}

        if volf <= 0:
            reason = "Volume invalide (<= 0)."
            logger.warning(reason)
            feedback = trade_executor.feedback_pipeline(
                order_id=trade_decision["order_id"], status="failed", reason=reason
            )
            trade_executor._feedback_safe(trade_decision, feedback)
            return {"status": "failed", "reason": reason}

        if volf > hard_cap:
            reason = (
                f"Volume demandé {volf:.2f} > cap sécurité {hard_cap:.2f} pour {asset} (fat-finger). "
                "Passage en validation manuelle."
            )
            logger.warning(reason)
            feedback = trade_executor.feedback_pipeline(
                order_id=trade_decision["order_id"],
                status="pending_manual_approval",
                reason=reason,
            )
            trade_executor._feedback_safe(trade_decision, feedback)
            return {
                "status": "pending_manual_approval",
                "reason": reason,
                "suggested_volume": hard_cap,
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
            # métadonnées burst (comment, basket_id, trailing meta…)
            td_with_meta = trade_executor._attach_burst_metadata(dict(trade_decision))

            if is_dry_run:
                return {
                    "status": "dry_run_ready",
                    "mode": "burst",
                    "payload": {
                        "trade_decision": td_with_meta,
                        "market_context": market_context,
                        "active_config": active_config,
                    },
                }

            # Exécution BURST (single master)
            return trade_executor.execute_burst_single_master(
                {
                    "trade_decision": td_with_meta,
                    "market_context": market_context,
                    "active_config": active_config,
                }
            )

        # --- Mode standard ---
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
