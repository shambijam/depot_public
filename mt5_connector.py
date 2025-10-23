# Contenu du fichier mt5_connector.py (suite)

from typing import List, Dict, Optional, Any, NamedTuple
from pathlib import Path
import pandas as pd
import os
import logging  # Garder l'import de logging
import json
import csv
import sys
import math
import re
import time
import MetaTrader5 as mt5
from collections import namedtuple
from datetime import datetime, timedelta, UTC


# Wrapper fallback (comme un NamedTuple)
SymbolInfoFallback = namedtuple(
    "SymbolInfoFallback",
    ["symbol", "spread", "point", "digits", "trade_contract_size", "trade_tick_size"],
)
# Alias .name pour compatibilité avec du code qui s'attend à 'name' (ex: build_burst_trailing_request)
SymbolInfoFallback.name = property(lambda self: self.symbol)


# Import pour la configuration
from core.config_manager import (
    ConfigManager,
)  # Nécessaire pour accéder à ConfigManager

# Initialisation du Logger pour ce module (au niveau global pour ce fichier)
# NOTE IMPORTANTE : Cette ligne est essentielle. Elle récupère le logger de ce module.
# Aucune autre configuration (setLevel, addHandler, etc.) ne doit être faite ici.
# La configuration sera faite par _setup_logger() quand la classe sera instanciée.
logger = logging.getLogger(__name__)

# TODO: Définir des NamedTuple ou des Pydantic Models pour les structures de données clés
# (Comme suggéré dans le bloc d'imports)


class MT5Connector:
    """
    Gère la connexion et l'interaction avec le terminal MetaTrader 5 via l'API `MetaTrader5`.

    Ce connecteur est responsable de :
    - L'initialisation et la déconnexion de la librairie MT5.
    - L'établissement et la rupture des sessions de trading avec le compte.
    - La récupération des données de marché (historiques et en temps réel).
    - L'obtention des informations sur les symboles et le compte de trading.
    - L'envoi des ordres de trading (marché, stop, limit, etc.).
    - La gestion des positions ouvertes et des ordres en attente.
    - La récupération de l'historique des deals et des ordres.
    """

    _instance: Optional["MT5Connector"] = None
    _is_connected: bool = False
    _login_info: Optional[Dict[str, Any]] = None
    _initialized_mt5_lib: bool = False

    CRITICAL_COLUMNS = [
        "Time",
        "Order",
        "Symbol",
        "Type",
        "Volume",
        "Price",
        "StopLoss",
        "TakeProfit",
        "Commission",
        "Swap",
        "Profit",
    ]
    DEFAULT_ALLOWED_ORDER_TYPES = [
        "BUY",
        "SELL",
        "BUY_LIMIT",
        "SELL_LIMIT",
        "BUY_STOP",
        "SELL_STOP",
    ]
    DATE_FORMATS = [
        "%Y.%m.%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%d.%m.%Y %H:%M:%S",
        "%d/%m/%Y %H:%M:%S",
    ]

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(MT5Connector, cls).__new__(cls)
        return cls._instance

    def __init__(self):
        """
        Initialise le connecteur MT5.

        Les paramètres de configuration sont récupérés via ConfigManager,
        permettant une configuration dynamique des comportements du connecteur.
        """
        # S'assurer que l'initialisation n'est exécutée qu'une seule fois pour le singleton
        if not hasattr(self, "_initialized_instance"):
            self._initialized_instance = True

            self.config_manager = ConfigManager()

            # --- Dynamisation des chemins, noms de fichiers de log et autres paramètres ---
            mt5_settings = self.config_manager.get("mt5_connector_settings", {})
            self.log_file_name = mt5_settings.get("log_file_name", "mt5_connector.log")
            self.trade_history_start_date_str = mt5_settings.get(
                "trade_history_start_date", "2020-01-01"
            )
            self.default_invalid_order_type = mt5_settings.get(
                "default_invalid_order_type", "UNKNOWN"
            )
            self.min_fixed_volume = mt5_settings.get("min_fixed_volume", 0.01)
            self.min_fixed_price = mt5_settings.get("min_fixed_price", 0.00001)
            self.abnormal_profit_threshold = mt5_settings.get(
                "abnormal_profit_threshold", 10000
            )

            log_dir = Path(self.config_manager.get("paths.logs", "logs/"))
            log_dir.mkdir(parents=True, exist_ok=True)
            self._setup_logger()

            # Initialiser les états de connexion
            self._is_connected = False
            self._login_info = None
            self._initialized_mt5_lib = False

            # --- Remplacement des constantes MT5 directes par des mappings ConfigManager ---
            self.mt5_mappings = self.config_manager.get("mt5_mappings", {})

            # Assignation des constantes MT5 via les mappings
            self.ORDER_TYPE_BUY = getattr(
                mt5,
                self.mt5_mappings.get("order_types", {}).get("BUY", "ORDER_TYPE_BUY"),
            )
            self.ORDER_TYPE_SELL = getattr(
                mt5,
                self.mt5_mappings.get("order_types", {}).get("SELL", "ORDER_TYPE_SELL"),
            )
            self.TRADE_ACTION_DEAL = getattr(
                mt5,
                self.mt5_mappings.get("trade_actions", {}).get(
                    "DEAL", "TRADE_ACTION_DEAL"
                ),
            )
            self.TRADE_ACTION_CLOSE_BY = getattr(
                mt5,
                self.mt5_mappings.get("trade_actions", {}).get(
                    "CLOSE_BY", "TRADE_ACTION_CLOSE_BY"
                ),
            )
            self.TRADE_ACTION_MODIFY = getattr(
                mt5,
                self.mt5_mappings.get("trade_actions", {}).get(
                    "MODIFY", "TRADE_ACTION_MODIFY"
                ),
            )

            self.TRADE_RETCODE_DONE = getattr(
                mt5,
                self.mt5_mappings.get("trade_retcodes", {}).get(
                    "RETCODE_DONE", "TRADE_RETCODE_DONE"
                ),
            )
            self.TRADE_RETCODE_REJECT = getattr(
                mt5,
                self.mt5_mappings.get("trade_retcodes", {}).get(
                    "RETCODE_REJECT", "TRADE_RETCODE_REJECT"
                ),
            )
            self.TRADE_RETCODE_REQUOTE = getattr(
                mt5,
                self.mt5_mappings.get("trade_retcodes", {}).get(
                    "RETCODE_REQUOTE", "TRADE_RETCODE_REQUOTE"
                ),
            )
            self.TRADE_RETCODE_TOO_MANY_REQUESTS = getattr(
                mt5,
                self.mt5_mappings.get("trade_retcodes", {}).get(
                    "RETCODE_TOO_MANY_REQUESTS", "TRADE_RETCODE_TOO_MANY_REQUESTS"
                ),
            )
            self.TRADE_RETCODE_NO_CHANGES = getattr(
                mt5,
                self.mt5_mappings.get("trade_retcodes", {}).get(
                    "RETCODE_NO_CHANGES", "TRADE_RETCODE_NO_CHANGES"
                ),
            )
            self.TRADE_RETCODE_TRADE_DISABLED = getattr(
                mt5,
                self.mt5_mappings.get("trade_retcodes", {}).get(
                    "RETCODE_TRADE_DISABLED", "TRADE_RETCODE_TRADE_DISABLED"
                ),
            )
            self.TRADE_RETCODE_CONNECTION = getattr(
                mt5,
                self.mt5_mappings.get("trade_retcodes", {}).get(
                    "RETCODE_CONNECTION", "TRADE_RETCODE_CONNECTION"
                ),
            )

            self.ORDER_TIME_GTC = getattr(
                mt5,
                self.mt5_mappings.get("order_time_flags", {}).get(
                    "GTC", "ORDER_TIME_GTC"
                ),
            )
            self.ORDER_FILLING_FOK = getattr(
                mt5,
                self.mt5_mappings.get("order_filling_policies", {}).get(
                    "FOK", "ORDER_FILLING_FOK"
                ),
            )
            self.ORDER_FILLING_RETURN = getattr(
                mt5,
                self.mt5_mappings.get("order_filling_policies", {}).get(
                    "RETURN", "ORDER_FILLING_RETURN"
                ),
            )
            self.ORDER_FILLING_IOC = getattr(
                mt5,
                self.mt5_mappings.get("order_filling_policies", {}).get(
                    "IOC", "ORDER_FILLING_IOC"
                ),
            )

            self.TIMEFRAMES = {
                key: getattr(mt5, value)
                for key, value in self.mt5_mappings.get("timeframes", {}).items()
            }
            self.TIMEFRAME_M1 = self.TIMEFRAMES.get("M1", mt5.TIMEFRAME_M1)
            
            # Magic number du bot (fallbacks possibles)
            self.magic = int(self.config_manager.get("magic_number", 0)
                            or self.config_manager.get("defaults.magic_number", 0)
                            or 0)

            self.logger.info(
                "MT5Connector initialisé avec succès, constantes MT5 chargées via ConfigManager."
            )
            self._init_position_mappings()  # NEW

            # --- AJOUT 1: mapping inverse & clôture marché ------------------------------

    def _init_position_mappings(self):
        """Appelé à la fin de __init__ pour sécuriser les constantes position."""
        # si tu as des mappings 'position_types' dans la conf, on les prend, sinon fallback MT5
        pos_map = self.mt5_mappings.get("position_types", {})
        self.POSITION_TYPE_BUY = getattr(mt5, pos_map.get("BUY", "POSITION_TYPE_BUY"))
        self.POSITION_TYPE_SELL = getattr(
            mt5, pos_map.get("SELL", "POSITION_TYPE_SELL")
        )

        # inverse pour fermer: BUY -> SELL, SELL -> BUY
        self.ORDER_TYPE_FROM_POSITION = {
            self.POSITION_TYPE_BUY: self.ORDER_TYPE_SELL,
            self.POSITION_TYPE_SELL: self.ORDER_TYPE_BUY,
        }

    def _pip_size(self, symbol: str) -> float:
        s = (symbol or "").upper()
        if s.endswith("JPY"):
            return 0.01
        # Métaux / indices courants en décimales "centimes"
        if s.startswith(("XAU", "XAG", "XPT", "XPD")):
            return 0.01
        return 0.0001  # majors FX

    def close_position_market(self, position) -> bool:
        """
        Ferme une position au marché en utilisant le type inverse:
        - position BUY -> ordre SELL au Bid
        - position SELL -> ordre BUY à l'Ask
        """
                
        try:
            order_type = self.ORDER_TYPE_FROM_POSITION.get(position.type)
            if order_type is None:
                self.logger.error(
                    "Mapping inverse manquant (position.type=%s). "
                    "Vérifie mt5_mappings.position_types.",
                    position.type,
                )
                return False

            # --- Récupération symbol info (nécessaire pour filling & arrondis) ---
            si = self.get_symbol_info(position.symbol)
            if not si:
                self.logger.error("Symbol info introuvable pour %s.", position.symbol)
                return False

            # --- Tick & prix ---
            tick = self.mt5.symbol_info_tick(position.symbol)
            if not tick:
                self.logger.error("Tick introuvable pour %s.", position.symbol)
                return False

            price = tick.bid if order_type == self.ORDER_TYPE_SELL else tick.ask

            # Normalisation du prix selon digits du symbole
            digits = getattr(si, "digits", None)
            if isinstance(digits, int):
                price = round(price, digits)

            # --- Filling mode, résolu à partir du symbol info ---
            try:
                type_filling = self._resolve_order_filling(si, preferred="RETURN")
            except Exception:
                # Fallback doux (si utilitaire indisponible)
                type_filling = getattr(self, "ORDER_FILLING_RETURN", None)

            req = {
                "action": self.TRADE_ACTION_DEAL,
                "symbol": position.symbol,
                "type": order_type,
                "position": position.ticket,  # indispensable pour clôture
                "volume": position.volume,
                "price": price,
                "deviation": 50,
                "magic": getattr(self, "magic", 0),
                "comment": "SNIPER_X close market",
                "type_filling": type_filling,
                "type_time": self.ORDER_TIME_GTC,
            }

            res = self.mt5.order_send(req)
            if res and getattr(res, "retcode", None) == self.TRADE_RETCODE_DONE:
                self.logger.info(
                    "Position #%s fermée (deal=%s).",
                    position.ticket,
                    getattr(res, "deal", "N/A"),
                )
                return True

            self.logger.error(
                "Échec fermeture #%s retcode=%s comment=%s",
                position.ticket,
                getattr(res, "retcode", "?"),
                getattr(res, "comment", "?"),
            )
            return False

        except Exception as e:
            self.logger.exception("close_position_market: %s", e)
            return False

    def _get_position_by_ticket(self, ticket: int):
        """Retourne la position MT5 portant ce ticket, ou None."""
        try:
            positions = self.get_open_positions() or []
            for p in positions:
                if int(getattr(p, "ticket", -1)) == int(ticket):
                    return p
        except Exception:
            pass
        return None

    def close_position(self, ticket: int, *, retry: int = 1) -> bool:
        """
        Ferme une position par ticket avec un DEAL inverse.
        - Choisit automatiquement un filling autorisé (RETURN/IOC/FOK) pour éviter 10030.
        - Arrondit le prix aux digits du symbole.
        """
        import time
        
        # 🔒 Ne jamais fermer un trade manuel
        try:
            pos = self._get_position_by_ticket(ticket)
        except Exception:
            pos = None

        bot_magic = int(getattr(self, "magic", 0)
                        or self.config_manager.get("magic_number", 0)
                        or self.config_manager.get("defaults.magic_number", 0)
                        or 0)
        if pos is not None and int(getattr(pos, "magic", -1)) != bot_magic:
            self.logger.info("[CLOSE][IMMUNITY] Skip manuel: ticket=%s magic=%s != bot.magic=%s",
                            ticket, getattr(pos, "magic", None), bot_magic)
            return False
            
         

        bot_magic = int(getattr(self, "magic", 0) or self.config_manager.get("defaults.magic_number", 0) or 0)
        if pos is not None and int(getattr(pos, "magic", -1)) != bot_magic:
            self.logger.info("[CLOSE][IMMUNITY] Skip manuel: ticket=%s magic=%s != bot.magic=%s",
                            ticket, getattr(pos, "magic", None), bot_magic)
            return False


        pos = self._get_position_by_ticket(ticket)
        if not pos:
            self.logger.warning(
                f"[MT5C] close_position: ticket {ticket} introuvable (déjà fermé ?)"
            )
            return True  # considéré comme fermé

        # Sens inverse (BUY -> SELL ; SELL -> BUY)
        order_type = self.ORDER_TYPE_FROM_POSITION.get(getattr(pos, "type", None))
        if order_type is None:
            self.logger.error(
                f"[MT5C] close_position: mapping inverse indisponible pour type={getattr(pos,'type',None)}"
            )
            return False

        # Infos symbole (pour digits & filling)
        try:
            si = self.get_symbol_info(pos.symbol)
        except Exception:
            si = None

        # Resolve filling autorisé (fallback IOC/RETURN si pas d'info)
        try:
            type_filling = (
                self._resolve_order_filling(si, preferred="RETURN")
                if si
                else getattr(self, "ORDER_FILLING_IOC", self.ORDER_FILLING_RETURN)
            )
        except Exception:
            type_filling = getattr(self, "ORDER_FILLING_IOC", self.ORDER_FILLING_RETURN)

        # Prix côté inverse
        tick = self.mt5.symbol_info_tick(pos.symbol)
        if not tick:
            self.logger.error(
                f"[MT5C] close_position: tick indisponible pour {pos.symbol}"
            )
            return False
        price = tick.bid if order_type == self.ORDER_TYPE_SELL else tick.ask

        # Arrondi prix aux digits du symbole si dispo
        digits = getattr(si, "digits", None)
        if isinstance(digits, int):
            try:
                price = round(float(price), digits)
            except Exception:
                pass

        # Normalisation volume sur le step du symbole (sécurité)
        try:
            volume = float(getattr(pos, "volume", 0.0))
        except Exception:
            volume = 0.0
        if volume <= 0.0:
            self.logger.error(
                f"[MT5C] close_position: volume non valide ({volume}) pour ticket {ticket}"
            )
            return False
        if si:
            step = getattr(si, "volume_step", None)
            vmin = getattr(si, "volume_min", None)
            vmax = getattr(si, "volume_max", None)
            try:
                if step and step > 0:
                    volume = round(volume / step) * step
                if vmin is not None and volume < vmin:
                    volume = vmin
                if vmax is not None and volume > vmax:
                    volume = vmax
            except Exception:
                pass

        deviation = getattr(self, "deviation", 50)
        try:
            deviation = int(deviation)
        except Exception:
            deviation = 50

        base_req = {
            "action": self.TRADE_ACTION_DEAL,
            "symbol": pos.symbol,
            "type": order_type,
            "position": int(pos.ticket),  # indispensable pour clore la position
            "volume": float(volume),
            "price": float(price),
            "deviation": deviation,
            "type_time": self.ORDER_TIME_GTC,
            "comment": "SNIPER_X close by ticket",
        }

        # Ordre d’essai des fillings: résolu -> IOC -> FOK -> RETURN (sans doublons)
        filling_candidates = []
        for tf in [
            type_filling,
            getattr(self, "ORDER_FILLING_IOC", None),
            getattr(self, "ORDER_FILLING_FOK", None),
            getattr(self, "ORDER_FILLING_RETURN", None),
        ]:
            if tf and tf not in filling_candidates:
                filling_candidates.append(tf)

        last_res = None
        for tf in filling_candidates:
            req = dict(base_req)
            req["type_filling"] = tf

            res = self.mt5.order_send(req)
            last_res = res

            if res and getattr(res, "retcode", None) == self.TRADE_RETCODE_DONE:
                self.logger.info(
                    f"[MT5C] close_position: #{ticket} fermé (deal={getattr(res, 'deal', 'N/A')}, filling={tf})."
                )
                return True

            ret = getattr(res, "retcode", None)
            com = getattr(res, "comment", "?")
            self.logger.warning(
                f"[MT5C] close_position: tentative échouée ticket {ticket} retcode={ret} comment={com} filling={tf}"
            )

            # Requote -> petit retry immédiat sur le même filling
            if retry > 0 and ret == getattr(self, "TRADE_RETCODE_REQUOTE", None):
                time.sleep(0.15)
                retry -= 1
                continue

            # Invalid/Unsupported filling -> on essaie le filling suivant
            if ret in (getattr(self, "TRADE_RETCODE_INVALID_FILL", 10030), 10030):
                continue

            # Autre erreur -> on ne s’acharne pas, on passera au post-check
            break

        # Post-check tardif (latence serveur)
        time.sleep(0.15)
        still = self._get_position_by_ticket(ticket)
        if not still:
            self.logger.info(
                f"[MT5C] close_position: #{ticket} confirmé fermé après post-check."
            )
            return True

        self.logger.warning(
            f"[MT5C] close_position: échec final ticket {ticket} retcode={getattr(last_res, 'retcode', '?')} comment={getattr(last_res, 'comment', '?')}"
        )
        return False

    def close_positions(self, tickets: list[int]) -> dict:
        """
        Ferme en série une liste de tickets. Retourne un petit rapport.
        Attendues par close_burst_basket(...).
        """
        import time
                
        total = len(tickets or [])
        ok = ko = 0
        tickets = [int(t) for t in (tickets or []) if t is not None]

        for t in tickets:
            if self.close_position(t):
                ok += 1
            else:
                ko += 1
            time.sleep(0.05)  # micro-throttle

        # post-vérification: certaines fermetures sont async côté serveur
        time.sleep(0.15)
        still_open = []
        open_now = {
            int(getattr(p, "ticket", -1)) for p in (self.get_open_positions() or [])
        }
        for t in tickets:
            if t in open_now:
                still_open.append(t)

        if still_open:
            self.logger.warning(
                f"[MT5C] close_positions: restants non fermés: {still_open}"
            )

        return {"total": total, "closed": ok, "failed": ko, "still_open": still_open}

    def _resolve_order_filling(self, symbol_info, preferred: str | None = None):
        """
        Choisit un type_filling accepté par le symbole.
        preferred: "RETURN" | "IOC" | "FOK" | None (on respecte si compatible)
        Remarque MetaTrader: symbol_info.filling_mode retourne int: 0=FOK, 1=IOC, 2=RETURN.
        """
        fm = getattr(symbol_info, "filling_mode", None)

        # Valeur sûre par défaut si l’info n’est pas disponible
        allowed = self.ORDER_FILLING_IOC

        if isinstance(fm, int):
            if fm == 2:
                allowed = self.ORDER_FILLING_RETURN
            elif fm == 1:
                allowed = self.ORDER_FILLING_IOC
            else:
                allowed = self.ORDER_FILLING_FOK

        # Si on a une préférence et qu’elle est compatible, on la garde
        if preferred == "RETURN" and allowed == self.ORDER_FILLING_RETURN:
            return self.ORDER_FILLING_RETURN
        if preferred == "IOC" and allowed == self.ORDER_FILLING_IOC:
            return self.ORDER_FILLING_IOC
        if preferred == "FOK" and allowed == self.ORDER_FILLING_FOK:
            return self.ORDER_FILLING_FOK

        return allowed

    def get_spread_pips(self, symbol: str) -> float:
        """Retourne le spread en pips avec garde-fous robustes."""
        try:
            info = self.mt5.symbol_info(symbol)
            tick = self.mt5.symbol_info_tick(symbol)

            point = float(getattr(info, "point", 0.0) or 0.0) if info else 0.0
            pip = float(self._pip_size(symbol) or 0.0)
            if pip <= 0.0:
                return float("inf")  # pip inconnu => on bloque

            # 1) Calcul direct à partir du tick (chemin prioritaire)
            bid = float(getattr(tick, "bid", 0.0) or 0.0) if tick else 0.0
            ask = float(getattr(tick, "ask", 0.0) or 0.0) if tick else 0.0
            if ask > 0.0 and bid > 0.0 and ask >= bid:
                spread_price = ask - bid

            # 2) Fallback: spread reporté par le symbole (en "points")
            elif info and point > 0.0:
                raw_points = int(getattr(info, "spread", 0) or 0)
                if raw_points > 0:
                    spread_price = raw_points * point
                else:
                    return float("inf")  # pas de data exploitable

            else:
                return float("inf")  # pas de data exploitable

            sp = spread_price / pip
            if not math.isfinite(sp) or sp < 0.0:
                return float("inf")

            return round(sp, 5)
        except Exception:
            return float("inf")

    # --- AJOUT 3: wrapper order_calc_profit sans "unpack" -----------------------

    def safe_order_calc_profit(
        self, order_type, symbol, volume, price_open, price_close
    ):
        """
        Retourne un float (ou None) au lieu de tenter de déballer un tuple.
        Évite l'erreur 'cannot unpack non-iterable float object'.
        """
        try:
            return mt5.order_calc_profit(
                order_type, symbol, volume, price_open, price_close
            )
        except Exception as e:
            self.logger.warning(
                "order_calc_profit indisponible: %s. Fallback interne.", str(e)
            )
            return None

    def _resolve_timeframe(self, timeframe_str: str):
        """
        Résout un timeframe ('M5', '5', '5M'...) en priorité via prod_config.json -> timeframe_mapping[TF].mt5_name,
        sinon fallback sur les constantes MT5 (TIMEFRAME_M5...). Renvoie (mt5_timeframe_obj, tf_key_str).
        """
        key_raw = str(timeframe_str).strip().upper()
        alias = {
            "1": "M1",
            "1M": "M1",
            "5": "M5",
            "5M": "M5",
            "15": "M15",
            "15M": "M15",
            "30": "M30",
            "30M": "M30",
            "60": "H1",
        }
        tf_key = alias.get(key_raw, key_raw)

        mt5_timeframe = None

        # 1) PROD CONFIG EN PREMIER : timeframe_mapping -> mt5_name
        try:
            cfg_map = {}
            if hasattr(self, "config_manager") and self.config_manager:
                cfg_map = self.config_manager.get("timeframe_mapping", {}) or {}
            if tf_key in cfg_map:
                mt5_name = str(cfg_map[tf_key].get("mt5_name", "")).strip()
                if mt5_name:
                    mt5_timeframe = getattr(self.mt5, mt5_name, None)
        except Exception:
            mt5_timeframe = None

        # 2) Fallback direct sur la lib MT5 (TIMEFRAME_<TF>)
        if mt5_timeframe is None:
            attr_name = f"TIMEFRAME_{tf_key}"
            mt5_timeframe = getattr(self.mt5, attr_name, None)

        # 3) Fallback final M1
        if mt5_timeframe is None:
            self.logger.error(
                f"Timeframe '{timeframe_str}' non valide (résolu '{tf_key}'). Utilisation de M1 par défaut."
            )
            mt5_timeframe = getattr(self, "TIMEFRAME_M1", None) or getattr(
                self.mt5, "TIMEFRAME_M1"
            )

        return mt5_timeframe, tf_key

    def _setup_logger(self) -> None:
        """
        Configure le logger spécifique à MT5Connector pour écrire dans un fichier dédié.
        Cette méthode est appelée pendant l'initialisation.

        Elle s'assure que le logger pour ce module écrit dans le fichier spécifié
        par la configuration dynamique et évite les duplications de handlers.
        Le niveau de logging et l'activation du StreamHandler sont configurables.
        """
        mt5_logger = logging.getLogger(__name__)  # Récupérer le logger de ce module

        # Récupérer le niveau de log et l'activation du console handler depuis la config
        # Les paramètres sont tirés de "mt5_connector_settings.log_level" et "mt5_connector_settings.enable_console_logging"
        log_level_str = self.config_manager.get(
            "mt5_connector_settings.log_level", "INFO"
        ).upper()
        enable_console_logging = self.config_manager.get(
            "mt5_connector_settings.enable_console_logging", False
        )

        # Définir le niveau de log du logger
        mt5_logger.setLevel(
            getattr(logging, log_level_str, logging.INFO)
        )  # Fallback à INFO si le niveau est invalide

        # Supprimer les handlers existants pour éviter les duplications
        if mt5_logger.hasHandlers():
            mt5_logger.handlers.clear()

        # Chemin complet du fichier de log MT5 (lu dynamiquement via ConfigManager)
        log_file_path = (
            Path(self.config_manager.get("paths.logs", "logs/")) / self.log_file_name
        )

        # File handler pour écrire dans le fichier de log
        file_handler = logging.FileHandler(log_file_path, mode="a", encoding="utf-8")
        file_formatter = logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        )
        file_handler.setFormatter(file_formatter)
        file_handler.setLevel(
            getattr(logging, log_level_str, logging.INFO)
        )  # Définir correctement le niveau du FileHandler
        mt5_logger.addHandler(file_handler)

        # Ajouter un Console handler si les logs de MT5Connector doivent aussi apparaître sur la console.
        if enable_console_logging:
            console_handler = logging.StreamHandler(sys.stdout)
            # Utilise un formatateur plus concis pour la console
            console_formatter = logging.Formatter(
                "%(asctime)s - %(levelname)s - [MT5C] - %(message)s"
            )  # [MT5C] pour identification
            console_handler.setFormatter(console_formatter)
            console_handler.setLevel(
                getattr(logging, log_level_str, logging.INFO)
            )  # Définir correctement le niveau du ConsoleHandler
            mt5_logger.addHandler(console_handler)

        # Désactiver la propagation pour éviter le double logging avec le logger racine
        mt5_logger.propagate = False
        self.logger = mt5_logger

        # Utilise self.logger qui est déjà l'instance `mt5_logger` configurée
        self.logger.info(
            f"Logger MT5Connector configuré pour écrire dans: {log_file_path} (Niveau: {log_level_str}). Console logging: {enable_console_logging}"
        )

    def connect(self, account_details: Dict[str, Any]) -> bool:
        """
        Établit une connexion avec le terminal MetaTrader 5 en utilisant les identifiants fournis
        dans le dictionnaire `account_details`. Gère l'initialisation de la librairie `MetaTrader5`
        si ce n'est pas déjà fait.

        Args:
            account_details (Dict[str, Any]): Dictionnaire contenant les informations de connexion
                                                du compte (login, password, server, account_id, etc.).
                                                Ce dictionnaire est typiquement fourni par
                                                `ConfigManager.get_mt5_account_credentials()`.

        Returns:
            bool: True si la connexion est réussie et autorisée, False sinon.
        """
        login = account_details.get("login")
        password = account_details.get("password")
        server = account_details.get("server")
        account_id_str = account_details.get("account_id", "N/A")

        if not all([login, password, server]):
            self.logger.critical(
                f"MT5: Identifiants de compte incomplets fournis pour la connexion (account_id: {account_id_str}). Login, mot de passe ou serveur manquant."
            )
            # CORRECTION: Passage de l'argument alert_type en positionnel
            self.config_manager.send_alert(
                f"MT5 Connexion Échec: Identifiants incomplets pour '{account_id_str}'",
                "telegram_critical",
            )
            return False

        if self._is_connected:
            # Vérifier si c'est le même compte. Si oui, pas besoin de se reconnecter.
            if self._login_info and self._login_info.get("login") == login:
                self.logger.info(
                    f"MT5: Déjà connecté au compte {login} ({account_id_str}). Connexion ignorée."
                )
                return True
            else:
                # Si déjà connecté mais à un compte différent, se déconnecter d'abord.
                self.logger.warning(
                    f"MT5: Déjà connecté à un compte différent ({self._login_info.get('login')}). Déconnexion avant de tenter de se connecter à {login} ({account_id_str})."
                )
                self.disconnect()  # Déconnexion propre

        # Initialiser la librairie MT5 si ce n'est pas déjà fait.
        # Idéalement, cela est géré une seule fois par main.py au démarrage du bot.
        # Ce bloc sert de garde-fou.
        if not self._initialized_mt5_lib:
            self.logger.info(
                "MT5: Tentative d'initialisation de la librairie MetaTrader5."
            )
            # import MetaTrader5 as mt5 # S'assurer que mt5 est accessible ici si ce n'est pas global
            if not mt5.initialize():
                self.logger.critical(
                    f"MT5: Échec de l'initialisation de la librairie MetaTrader5. Erreur: {mt5.last_error()}. Veuillez vérifier que le terminal est démarré."
                )
                # CORRECTION: Passage de l'argument alert_type en positionnel
                self.config_manager.send_alert(
                    f"MT5 Init Échec: {mt5.last_error()}", "telegram_critical"
                )
                return False
            self.mt5 = mt5  # Assignation de l'objet mt5 initialisé
            self._initialized_mt5_lib = True
            self.logger.info("MT5: Librairie MetaTrader5 initialisée.")

        self.logger.info(
            f"MT5: Tentative de connexion au compte {login} ({account_id_str}) sur le serveur {server}..."
        )
        authorized = mt5.login(login, password, server)
        if authorized:
            self._is_connected = True
            # Stocker toutes les informations du compte connecté pour référence future
            self._login_info = account_details
            self.logger.info(
                f"MT5: Connecté avec succès au compte {login} ({account_id_str}) sur le serveur {server}."
            )
            return True
        else:
            last_error = mt5.last_error()
            self.logger.error(
                f"MT5: Échec de la connexion. Login: {login}, Serveur: {server}. Erreur: {last_error}."
            )
            # CORRECTION: Passage de l'argument alert_type en positionnel
            self.config_manager.send_alert(
                f"MT5 Connexion Échec: {last_error}. Compte: {account_id_str}",
                "telegram_critical",
            )
            self._is_connected = False
            self._login_info = None  # Effacer les infos de login précédentes
            return False

    def reconnect_if_needed(self, max_tries: int = 2, delay: float = 0.5) -> bool:
        """
        Tente de (re)connecter MT5 en douceur si nécessaire.
        Utilise self._login_info si disponible.
        """
        import time

        def _is_connected() -> bool:
            try:
                return bool(
                    self.is_connected()
                    if callable(self.is_connected)
                    else self.is_connected
                )
            except Exception:
                return False

        if _is_connected():
            return True

        creds = getattr(self, "_login_info", None) or {}
        for i in range(max_tries):
            try:
                connect_fn = getattr(self, "connect", None)
                if callable(connect_fn):
                    ok = connect_fn(creds) if creds else connect_fn()
                    if ok or _is_connected():
                        return True
            except Exception as e:
                self.logger.warning(
                    f"[MT5C] reconnect attempt {i+1}/{max_tries} failed: {e}"
                )
            time.sleep(delay * (i + 1))  # petit backoff progressif
        return False

    def safe_order_calc_margin(self, order_type, symbol: str, volume, price):
        """
        Wrapper tolérant autour mt5.order_calc_margin(...) → float ou None.
        """
        try:
            m = self.mt5.order_calc_margin(
                order_type, symbol, float(volume), float(price)
            )
            return float(m) if m is not None else None
        except Exception as e:
            self.logger.warning(f"order_calc_margin indisponible: {e}")
            return None

    def modify_position_sltp(
        self, ticket: int, sl=None, tp=None, symbol: str | None = None
    ):
        """
        Attache/ajuste SL/TP sur une position existante.
        - Arrondit aux digits du symbole
        - Ne "détend" jamais un SL existant (on n'éloigne pas le stop)
        - Corrige INVALID_STOPS en élargissant au minimum requis (stops/freeze/spread + buffer)
        Retourne (ok: bool, result: Any).
        """
        mt5 = self.mt5
        
        # 🔒 Immunité manuels: on ne modifie que les positions du bot
        def _pos_magic(p): 
            return int(getattr(p, "magic", -1))
        def _pos_by_ticket_safe(tk: int):
            try:
                poss = list(mt5.positions_get() or [])
                for p in poss:
                    if int(getattr(p, "ticket", -1)) == int(tk):
                        return p
            except Exception:
                pass
            return None

        bot_magic = int(getattr(self, "magic", 0) or self.config_manager.get("defaults.magic_number", 0) or 0)
        _pos = _pos_by_ticket_safe(int(ticket))
        if _pos is not None and _pos_magic(_pos) != bot_magic:
            self.logger.info("[SLTP][IMMUNITY] Skip manuel: ticket=%s magic=%s != bot.magic=%s",
                            ticket, getattr(_pos, "magic", None), bot_magic)
            return True, None  # on considère 'OK' mais on ne touche pas

        # --- helpers locaux ---------------------------------------------------------
        def _f(x):
            try:
                return float(x)
            except Exception:
                return None

        def _symbol_ctx(sym: str):
            si = None
            try:
                si = (
                    self.symbol_info(sym)
                    if hasattr(self, "symbol_info") and callable(self.symbol_info)
                    else mt5.symbol_info(sym)
                )
            except Exception:
                si = None
            if not si:
                return None
            return {
                "digits": int(getattr(si, "digits", 5) or 5),
                "point": float(getattr(si, "point", 10**-5) or 10**-5),
                "stops_level": int(
                    getattr(si, "trade_stops_level", getattr(si, "stops_level", 0)) or 0
                ),
                "freeze_level": int(
                    getattr(si, "trade_freeze_level", getattr(si, "freeze_level", 0))
                    or 0
                ),
                "spread_pts": int(getattr(si, "spread", 0) or 0),
            }

        def _pos_by_ticket(tk: int):
            try:
                poss = list(mt5.positions_get() or [])
                for p in poss:
                    if int(getattr(p, "ticket", -1)) == int(tk):
                        return p
            except Exception:
                pass
            return None

        def _tick(sym: str):
            try:
                t = mt5.symbol_info_tick(sym)
                bid = float(getattr(t, "bid", 0.0) or 0.0)
                ask = float(getattr(t, "ask", 0.0) or 0.0)
                return bid, ask
            except Exception:
                return 0.0, 0.0

        def _enforce_min_distance(
            sym: str, side: int, sl_in, tp_in, digits: int, ctx: dict
        ):
            """
            Applique la distance mini: max(stops, freeze, spread) + buffer (en points).
            Utilise le Bid/Ask courant pour vérifier SL/TP côté marché.
            side: 0=BUY, 1=SELL (comme MT5)
            """
            # buffer configurable
            try:
                buffer_pts = int(
                    self.config_manager.get("risk.sltp_extra_buffer_points", 2) or 2
                )
            except Exception:
                buffer_pts = 2

            min_pts = max(
                ctx["stops_level"], ctx["freeze_level"], ctx["spread_pts"]
            ) + max(buffer_pts, 0)
            min_dist = float(min_pts) * float(ctx["point"])

            bid, ask = _tick(sym)
            sl_out, tp_out = sl_in, tp_in

            if side == 0:  # BUY
                if sl_out is not None:
                    limit = (bid or 0.0) - min_dist
                    if sl_out >= limit:
                        sl_out = round(limit, digits)
                if tp_out is not None:
                    limit = (ask or 0.0) + min_dist
                    if tp_out <= limit:
                        tp_out = round(limit, digits)
            else:  # SELL
                if sl_out is not None:
                    limit = (ask or 0.0) + min_dist
                    if sl_out <= limit:
                        sl_out = round(limit, digits)
                if tp_out is not None:
                    limit = (bid or 0.0) - min_dist
                    if tp_out >= limit:
                        tp_out = round(limit, digits)

            return sl_out, tp_out, min_pts
        
        def _points_per_pip(self, symbol_info) -> int:
            """
            Convertit 1 pip -> N points MT5 en fonction du symbole.
            Par défaut:
            - Forex 5 digits -> 1 pip = 10 points
            - Forex 3 digits -> 1 pip = 10 points
            - Métaux (ex XAUUSD) -> override via config sinon heuristique: 1 pip = 10 points si tick=0.01 et pip=0.10
            """
            point = float(symbol_info.point)
            # Override par config si dispo
            sym = symbol_info.name
            overrides = (getattr(self, "symbol_overrides", {}) or {}).get(sym, {})
            pip_size = overrides.get("pip_size")  # ex: 0.10 pour XAUUSD
            if pip_size is None:
                # Heuristiques propres: ajuste selon digits / tick
                if "XAU" in sym and abs(point - 0.01) < 1e-12:
                    pip_size = 0.10  # pip “trader” le plus courant pour l’or
                elif symbol_info.digits in (3, 5):
                    pip_size = point * 10.0  # ex EURUSD(5d): point=0.00001 => pip=0.0001
                else:
                    pip_size = point  # fallback conservateur

            ppp = max(1, int(round(pip_size / point)))
            return ppp

        # --- 1) récupérer la position / symbole / digits ----------------------------
        pos = _pos_by_ticket(ticket)
        if pos is None and symbol is None:
            self.logger.error(
                f"modify_position_sltp: position introuvable pour ticket={ticket} et symbol=None"
            )
            return False, None

        if symbol is None:
            symbol = str(getattr(pos, "symbol", "") or "")
            if not symbol:
                self.logger.error(
                    f"modify_position_sltp: symbole introuvable pour ticket={ticket}"
                )
                return False, None

        ctx = _symbol_ctx(symbol)
        if not ctx:
            self.logger.error(
                f"modify_position_sltp: symbol_info indisponible pour {symbol}"
            )
            return False, None

        digits = ctx["digits"]
        side = int(getattr(pos, "type", 0) if pos is not None else 0)  # 0=BUY, 1=SELL

        # --- 2) SL/TP initial + arrondis -------------------------------------------
        sl = _f(sl)
        tp = _f(tp)
        if sl is not None:
            sl = round(sl, digits)
        if tp is not None:
            tp = round(tp, digits)

        # Ne jamais "détendre" un SL existant
        cur_sl = _f(getattr(pos, "sl", None)) if pos is not None else None
        if cur_sl is not None and sl is not None:
            if side == 0 and sl < cur_sl:  # BUY: SL plus bas = on éloigne → refuse
                self.logger.info(
                    f"[SLTP] Refus de détendre SL (BUY) {cur_sl} → {sl} (ticket={ticket})"
                )
                sl = None
            if side == 1 and sl > cur_sl:  # SELL: SL plus haut = on éloigne → refuse
                self.logger.info(
                    f"[SLTP] Refus de détendre SL (SELL) {cur_sl} → {sl} (ticket={ticket})"
                )
                sl = None

        # Rien à faire ?
        if sl is None and tp is None:
            self.logger.info(f"[SLTP] Rien à modifier (ticket={ticket}).")
            return True, None

        # --- 3) Pré-ajustement min distance ----------------------------------------
        sl_adj, tp_adj, _ = _enforce_min_distance(symbol, side, sl, tp, digits, ctx)

        # --- 4) Envoi + correction INVALID_STOPS si besoin --------------------------
        def _send(sl_v, tp_v):
            req = {
                "action": getattr(mt5, "TRADE_ACTION_SLTP", None),
                "position": int(ticket),
                "symbol": symbol,
            }
            if sl_v is not None:
                req["sl"] = float(sl_v)
            if tp_v is not None:
                req["tp"] = float(tp_v)
            return mt5.order_send(req)

        res = None
        try:
            res = _send(sl_adj, tp_adj)
            ret = getattr(res, "retcode", None)
        except Exception as e:
            self.logger.error(
                f"modify_position_sltp exception (ticket={ticket}): {e}", exc_info=True
            )
            return False, None

        RET_DONE = getattr(mt5, "TRADE_RETCODE_DONE", None)
        RET_INVALID_STOPS = getattr(mt5, "TRADE_RETCODE_INVALID_STOPS", 10016)

        if ret == RET_DONE:
            return True, res

        # Une seconde tentative en élargissant encore si INVALID_STOPS
        if ret == RET_INVALID_STOPS:
            # élargissement minimal (on reprend bid/ask actuels et on re-applique)
            sl_w, tp_w, min_pts = _enforce_min_distance(
                symbol, side, sl_adj, tp_adj, digits, ctx
            )
            self.logger.warning(
                f"[SLTP] INVALID_STOPS → élargissement à min {min_pts} pts puis nouvel essai (ticket={ticket})."
            )
            try:
                res2 = _send(sl_w, tp_w)
                if getattr(res2, "retcode", None) == RET_DONE:
                    return True, res2
                return False, res2
            except Exception as e:
                self.logger.error(
                    f"modify_position_sltp 2e essai exception (ticket={ticket}): {e}",
                    exc_info=True,
                )
                return False, None

        # autre retcode → KO
        return False, res

    # --- Compatibilité avec d’anciens noms que le TradeExecutor peut appeler ---
    def position_modify(self, ticket: int, sl=None, tp=None, symbol: str | None = None):
        return self.modify_position_sltp(ticket=ticket, sl=sl, tp=tp, symbol=symbol)

    def modify_position(self, ticket: int, sl=None, tp=None, symbol: str | None = None):
        return self.modify_position_sltp(ticket=ticket, sl=sl, tp=tp, symbol=symbol)

    def set_sl_tp(self, ticket: int, sl=None, tp=None, symbol: str | None = None):
        return self.modify_position_sltp(ticket=ticket, sl=sl, tp=tp, symbol=symbol)

    @property
    def is_connected(self) -> bool:
        """
        Retourne l'état actuel de la connexion à MetaTrader 5.
        C'est une propriété en lecture seule.

        Returns:
            bool: True si connecté, False sinon.
        """
        return self._is_connected

    def disconnect(self) -> None:
        """
        Ferme la connexion active avec le compte MetaTrader 5 et désinitialise
        la librairie MT5 si elle a été initialisée par cette instance.

        Cette méthode est appelée pour assurer une fermeture propre de la session MT5.
        """
        if self._is_connected:  # Utiliser l'attribut _is_connected
            self.logger.info(
                f"MT5: Déconnexion du compte {self._login_info.get('login', 'N/A')} et désinitialisation de la librairie MetaTrader 5..."
            )
            mt5.shutdown()  # Désinitialise la librairie MT5
            self._is_connected = False  # Mettre à jour l'état interne
            self._login_info = None  # Effacer les infos de login
            self._initialized_mt5_lib = (
                False  # Indiquer que la librairie n'est plus initialisée
            )
            self.logger.info("MT5: Déconnecté et librairie désinitialisée.")
        else:
            self.logger.info("MT5: Non connecté, aucune déconnexion nécessaire.")

    def get_positions(
        self, symbol: Optional[str] = None
    ) -> Optional[
        List[Any]
    ]:  # Retourne une liste d'objets MetaTrader5.TradePosition NamedTuple
        """
        Récupère toutes les positions ouvertes sur le compte, avec un filtre optionnel par symbole.
        Assure une journalisation cohérente et une gestion robuste des erreurs.

        Args:
            symbol (str, optional): Le symbole de l'instrument pour filtrer les positions.
                                    Si None, toutes les positions ouvertes sont récupérées.

        Returns:
            Optional[List[Any]]: Une liste d'objets `MetaTrader5.TradePosition` (NamedTuple),
                                ou une liste vide si aucune position trouvée, `None` si la récupération échoue.
        """
        # CORRECTION: Utiliser la propriété is_connected sans parenthèses
        if not self.is_connected:
            self.logger.warning(
                f"MT5: Non connecté. Impossible de récupérer les positions ouvertes."
            )  # Utilise self.logger
            return None

        # Récupère les positions ouvertes. Utilise le paramètre `symbol` si fourni.
        positions = (
            self.mt5.positions_get(symbol=symbol)
            if symbol
            else self.mt5.positions_get()
        )  # Utilise self.mt5

        if (
            positions is None
        ):  # Si mt5.positions_get() retourne None, cela signifie une erreur API
            self.logger.error(
                f"MT5: Échec de la récupération des positions. Erreur: {self.mt5.last_error()}."
            )  # Utilise self.logger, self.mt5
            return None

        if len(positions) > 0:
            self.logger.info(
                f"MT5: Récupéré {len(positions)} positions ouvertes ({'pour ' + symbol if symbol else 'total'})."
            )  # Utilise self.logger
            return list(
                positions
            )  # Convertir le tuple de NamedTuple en liste pour plus de flexibilité
        else:
            self.logger.info(
                f"MT5: Aucune position ouverte trouvée ({'pour ' + symbol if symbol else 'total'})."
            )  # Utilise self.logger
            return (
                []
            )  # Retourne une liste vide au lieu de None pour la clarté et la facilité de manipulation

    def get_open_positions(self, symbol: Optional[str] = None):
        """Alias de compatibilité pour le TradeExecutor (évite la redondance)."""
        positions = self.get_positions(symbol)
        return positions or []

    def get_orders(
        self, symbol: Optional[str] = None
    ) -> Optional[
        List[Any]
    ]:  # Retourne une liste d'objets MetaTrader5.TradeOrder NamedTuple
        """
        Récupère tous les ordres en attente (pendants) sur le compte, avec un filtre optionnel par symbole.
        Assure une journalisation cohérente et une gestion robuste des erreurs.

        Args:
            symbol (str, optional): Le symbole de l'instrument pour filtrer les ordres.
                                    Si None, tous les ordres en attente sont récupérés.

        Returns:
            Optional[List[Any]]: Une liste d'objets `MetaTrader5.TradeOrder` (NamedTuple),
                                 ou une liste vide si aucun ordre trouvé, `None` si la récupération échoue.
        """
        if not self.is_connected:
            self.logger.warning(
                f"MT5: Non connecté. Impossible de récupérer les ordres en attente pour '{symbol}'."
            )  # Utilise self.logger
            return None

        # Récupère les ordres en attente. Utilise le paramètre `symbol` si fourni.
        orders = (
            self.mt5.orders_get(symbol=symbol) if symbol else self.mt5.orders_get()
        )  # Utilise self.mt5
        if (
            orders is None
        ):  # Si mt5.orders_get() retourne None, cela signifie une erreur API
            self.logger.error(
                f"MT5: Échec de la récupération des ordres en attente. Erreur: {self.mt5.last_error()}."
            )  # Utilise self.logger, self.mt5
            return None

        if len(orders) > 0:
            self.logger.info(
                f"MT5: Récupéré {len(orders)} ordres en attente ({'pour ' + symbol if symbol else 'total'})."
            )  # Utilise self.logger
            return list(orders)  # Convertir le tuple de NamedTuple en liste
        else:
            self.logger.info(
                f"MT5: Aucun ordre en attente trouvé ({'pour ' + symbol if symbol else 'total'})."
            )  # Utilise self.logger
            return []

    def get_current_price(self, symbol: str, action: str) -> Optional[float]:
        """
        Récupère le prix actuel (Ask pour BUY, Bid pour SELL) pour un symbole donné.
        Version robuste : inclut garde-fous NaN/None, fallback mid-price si ask/bid manquant,
        et logging explicite.
        """
        if not self.is_connected:
            self.logger.warning(
                f"MT5: Non connecté. Impossible de récupérer le prix actuel pour '{symbol}'."
            )
            return None

        try:
            tick = self.mt5.symbol_info_tick(symbol)
        except Exception as e:
            self.logger.error(
                f"MT5: Exception lors de la récupération du tick pour '{symbol}': {e}"
            )
            return None

        if not tick:
            self.logger.error(
                f"MT5: Aucune donnée de tick pour '{symbol}'. Last_error={self.mt5.last_error()}"
            )
            return None

        ask = getattr(tick, "ask", None)
        bid = getattr(tick, "bid", None)

        # Nettoyage NaN ou valeurs aberrantes
        try:
            ask = float(ask) if ask is not None else None
            bid = float(bid) if bid is not None else None
            if ask is not None and not (ask == ask and ask > 0):  # NaN ou <=0
                ask = None
            if bid is not None and not (bid == bid and bid > 0):
                bid = None
        except Exception:
            ask, bid = None, None

        if action.upper() == "BUY":
            if ask is not None:
                return ask
            elif bid is not None:
                self.logger.warning(f"MT5: Ask manquant pour '{symbol}', fallback Bid.")
                return bid
        elif action.upper() == "SELL":
            if bid is not None:
                return bid
            elif ask is not None:
                self.logger.warning(f"MT5: Bid manquant pour '{symbol}', fallback Ask.")
                return ask
        else:
            self.logger.warning(
                f"MT5: Action non reconnue '{action}' -> fallback mid-price si dispo."
            )

        # Fallback mid-price
        if ask is not None and bid is not None:
            return (ask + bid) / 2.0

        self.logger.error(
            f"MT5: Impossible de déterminer un prix valide pour '{symbol}' (ask={ask}, bid={bid})."
        )
        return None

    def get_account_info(
        self,
    ) -> Optional[Any]:  # Retourne un objet MetaTrader5.AccountInfo NamedTuple
        """
        Récupère les informations complètes du compte de trading actuellement connecté.
        Assure une journalisation et une gestion d'erreur appropriées.

        Returns:
            Optional[Any]: L'objet `MetaTrader5.AccountInfo` (NamedTuple) si réussi, `None` sinon.
        """
        if not self.is_connected:
            self.logger.warning(
                "MT5: Non connecté. Impossible de récupérer les informations du compte."
            )
            return None

        account_info = self.mt5.account_info()

        if account_info:
            self.logger.debug(
                f"MT5: Informations du compte récupérées. Équité: {account_info.equity}, Solde: {account_info.balance}"
            )
            return account_info
        else:
            self.logger.error(
                f"MT5: Échec de la récupération des informations du compte. Erreur: {self.mt5.last_error()}."
            )
            return None

    def get_rates(
        self, symbol: str, timeframe_str: str, num_bars: int
    ) -> Optional[pd.DataFrame]:
        """
        Récupère les barres OHLCV pour un symbole/TF.
        Priorité de résolution TF : prod_config.timeframe_mapping -> MT5 -> M1.
        """
        if not self.is_connected:
            self.logger.warning(
                f"MT5: Non connecté. Impossible de récupérer les données pour '{symbol}'."
            )
            return None

        # ✅ PROD_CONFIG EN PREMIER via _resolve_timeframe
        mt5_timeframe, tf_key = self._resolve_timeframe(timeframe_str)

        self.logger.debug(
            f"MT5: Tentative de récupération de {num_bars} barres pour '{symbol}' "
            f"(demandé='{timeframe_str}', résolu='{tf_key}')..."
        )
        try:
            rates = self.mt5.copy_rates_from_pos(
                symbol, mt5_timeframe, 0, int(max(1, num_bars))
            )

            if rates is None:
                last_mt5_error = self.mt5.last_error()
                self.logger.error(
                    f"[{symbol}] ÉCHEC DE LA COLLECTE. MT5 a retourné None. Erreur: {last_mt5_error}"
                )
                self.logger.debug(
                    f"[{symbol}] copy_rates_from_pos('{symbol}', {tf_key}, 0, {num_bars}) → None."
                )
                return None

            if len(rates) == 0:
                self.logger.warning(
                    f"[{symbol}] Tableau vide pour {num_bars} barres (TF='{tf_key}')."
                )
                self.logger.debug(
                    f"[{symbol}] Données insuffisantes ou symbole non tradable pour ce TF."
                )
                return pd.DataFrame()

            rates_frame = pd.DataFrame(rates)
            rates_frame["time"] = pd.to_datetime(
                rates_frame["time"], unit="s", utc=True
            )
            self.logger.debug(
                f"[{symbol}] {len(rates_frame)} barres récupérées (TF='{tf_key}')."
            )
            return rates_frame

        except Exception as e:
            self.logger.error(
                f"Exception in get_rates for {symbol}: {e}. Last MT5 error: {self.mt5.last_error()}.",
                exc_info=True,
            )
            self.logger.debug(
                f"[{symbol}] Exception pendant la récupération des rates (TF='{tf_key}')."
            )
            return None

    def get_ticks(
        self,
        symbol: str,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        count: int = 1000,
    ) -> pd.DataFrame:
        """
        🏦 Récupère les ticks bruts depuis MT5 (Dev Desk Edition)
        - Multi-fallback (range → from(now-5min) → from(now-60min))
        - Retourne toujours un DataFrame exploitable (même vide)
        - Colonnes standardisées: ["time", "bid", "ask", "last", "volume", "mid"]
        """
        import pandas as pd
        from datetime import datetime, timedelta, timezone

        if not getattr(self, "is_connected", False):
            self.logger.warning(
                f"[MT5C] Non connecté. Impossible de récupérer ticks '{symbol}'."
            )
            return pd.DataFrame(columns=["time", "bid", "ask", "last", "volume", "mid"])

        try:
            ticks = None

            # 1️⃣ Mode range si possible
            if start and end:
                ticks = self.mt5.copy_ticks_range(
                    symbol, start, end, self.mt5.COPY_TICKS_ALL
                )

            # 2️⃣ Sinon fallback depuis maintenant
            if ticks is None or len(ticks) == 0:
                ticks = self.mt5.copy_ticks_from(
                    symbol,
                    datetime.now(timezone.utc) - timedelta(minutes=5),
                    count,
                    self.mt5.COPY_TICKS_ALL,
                )

            # 3️⃣ Encore vide ? → fallback large
            if ticks is None or len(ticks) == 0:
                ticks = self.mt5.copy_ticks_from(
                    symbol,
                    datetime.now(timezone.utc) - timedelta(hours=1),
                    count,
                    self.mt5.COPY_TICKS_ALL,
                )

            if ticks is None or len(ticks) == 0:
                self.logger.warning(f"[MT5C] ❌ Aucun tick récupéré pour {symbol}.")
                return pd.DataFrame(
                    columns=["time", "bid", "ask", "last", "volume", "mid"]
                )

            df = pd.DataFrame(ticks)

            # ✅ Normalisation robuste
            if "time" not in df.columns:
                df["time"] = datetime.now(timezone.utc)
            else:
                df["time"] = pd.to_datetime(
                    df["time"], unit="s", utc=True, errors="coerce"
                ).fillna(datetime.now(timezone.utc))

            for col in ["bid", "ask", "last", "volume"]:
                if col not in df.columns:
                    df[col] = 0.0
                df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)

            df["mid"] = (df["bid"] + df["ask"]) / 2.0

            self.logger.info(
                f"[MT5C][{symbol}] ✅ {len(df)} ticks récupérés "
                f"({df['time'].min()} → {df['time'].max()})"
            )

            return df

        except Exception as e:
            self.logger.error(
                f"[MT5C] Erreur get_ticks pour {symbol}: {e}", exc_info=True
            )
            return pd.DataFrame(columns=["time", "bid", "ask", "last", "volume", "mid"])

    def get_ticks_for_candle(
        self, symbol: str, start_ts: datetime, end_ts: datetime
    ) -> pd.DataFrame:
        """
        🎯 Ticks exacts de la bougie M1 fermée : intervalle strict [start_ts, end_ts)
        - AUCUN fallback temporel
        - COPY_TICKS_ALL + filtrage strict sur l'horodatage (time_msc si dispo, sinon time)
        - Décodage flags 16/32 (BUY/SELL) ; fallback tick-rule (Δmid) ; ultime secours mapping 1/2
        - Fenêtre verrouillée à 60s en UTC
        Retourne les colonnes: ["time","bid","ask","last","volume","flags","side","mid","spread"]
        """
        import pandas as pd
        import numpy as np
        from datetime import timedelta, timezone

        ret_cols = [
            "time",
            "bid",
            "ask",
            "last",
            "volume",
            "flags",
            "side",
            "mid",
            "spread",
        ]

        # ── Garde-fou connexion
        if not getattr(self, "is_connected", False):
            self.logger.warning(f"[MT5C] Non connecté. Impossible ticks '{symbol}'.")
            return pd.DataFrame(columns=ret_cols)

        try:
            # ── Normalisation UTC + verrou 60s
            if start_ts.tzinfo is None:
                start_ts = start_ts.replace(tzinfo=timezone.utc)
            if end_ts.tzinfo is None:
                end_ts = end_ts.replace(tzinfo=timezone.utc)
            if (end_ts - start_ts).total_seconds() != 60.0:
                end_ts = start_ts + timedelta(seconds=60)

            # ── Requête brute
            ticks = self.mt5.copy_ticks_range(
                symbol, start_ts, end_ts, self.mt5.COPY_TICKS_ALL
            )
            if ticks is None or len(ticks) == 0:
                self.logger.warning(
                    f"[MT5C] Aucun tick trouvé pour {symbol} [{start_ts} → {end_ts}]"
                )
                return pd.DataFrame(columns=ret_cols)

            df = pd.DataFrame(ticks)

            # ── Typage / colonnes minimales
            df["time"] = pd.to_datetime(
                df.get("time", pd.NaT), unit="s", utc=True, errors="coerce"
            )
            if "time_msc" in df.columns:
                df["time_msc"] = pd.to_datetime(
                    df["time_msc"], unit="ms", utc=True, errors="coerce"
                )

            for c in ("bid", "ask", "last", "volume", "flags"):
                if c not in df.columns:
                    df[c] = 0
                df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)

            # Si pas de volume réel (classique FX/CFD), on pondère par 1 tick = 1
            if (df["volume"] == 0).all():
                df["volume"] = 1.0

            # ── Filtre temporel STRICT (time_msc prioritaire)
            tcol = "time_msc" if "time_msc" in df.columns else "time"
            df = df[(df[tcol] >= start_ts) & (df[tcol] < end_ts)].copy()
            if df.empty:
                self.logger.warning(
                    f"[MT5C] Aucun tick dans la fenêtre stricte pour {symbol} [{start_ts} → {end_ts}]"
                )
                return pd.DataFrame(columns=ret_cols)

            # ── mid & spread
            df["mid"] = (df["bid"] + df["ask"]) / 2.0
            df["spread"] = df["ask"] - df["bid"]

            # ── Epsilon pour la tick-rule (demi-point du symbole)
            try:
                info = self.mt5.symbol_info(symbol)
                point = float(getattr(info, "point", 0.0) or 0.0)
            except Exception:
                point = 0.0
            eps = max(point * 0.5, 1e-12)

            # ── Décodage des flags : 16=BUY, 32=SELL (prioritaires)
            flags = df["flags"].astype(int)
            is_buy_flag = (flags & 16) > 0
            is_sell_flag = (flags & 32) > 0

            side = np.where(
                is_buy_flag, "buy", np.where(is_sell_flag, "sell", "unknown")
            )

            # ── Fallback 1 : tick-rule (Lee–Ready simplifié sur Δmid)
            unk_mask = side == "unknown"
            if unk_mask.any():
                dmid = df["mid"].diff().fillna(0.0)
                dbid = df["bid"].diff().fillna(0.0)
                dask = df["ask"].diff().fillna(0.0)

                side_tick = np.where(
                    dmid > eps,
                    "buy",
                    np.where(
                        dmid < -eps,
                        "sell",
                        np.where(
                            (dask > 0) & (dbid >= 0),
                            "buy",
                            np.where((dbid < 0) & (dask <= 0), "sell", "unknown"),
                        ),
                    ),
                )
                tmp = side.copy()
                tmp[unk_mask] = side_tick[unk_mask]
                side = tmp

            # ── Fallback 2 (ultime) : lecture légère des bits 1/2 (ASK↑ ≈ buy, BID↓ ≈ sell)
            #    ⚠️ Ce n'est pas une "volonté d'agresseur", juste un dernier filet pour classer.
            unk_mask = side == "unknown"
            if unk_mask.any():
                is_ask_bit = (
                    flags & 1
                ) > 0  # BID_CHANGED (1) ? (convention MT5) — on garde la compat compat utilisateur
                is_bid_bit = (flags & 2) > 0  # ASK_CHANGED (2)
                # Remarque: selon broker, la sémantique 1/2 varie; on applique un mapping minimaliste:
                side_bits = np.where(
                    is_ask_bit & ~is_bid_bit,
                    "sell",  # BID_CHANGED seul → pression vendeuse (prix côté bid)
                    np.where(
                        is_bid_bit & ~is_ask_bit,
                        "buy",  # ASK_CHANGED seul → pression acheteuse
                        "unknown",
                    ),
                )
                tmp = side.copy()
                tmp[unk_mask] = side_bits[unk_mask]
                side = tmp

            df["side"] = side

            # ── Stats & log
            total = len(df)
            buy_n = int((df["side"] == "buy").sum())
            sell_n = int((df["side"] == "sell").sum())
            unk_n = int((df["side"] == "unknown").sum())
            coverage = float((df[tcol].max() - df[tcol].min()).total_seconds())

            self.logger.info(
                f"[MT5C][{symbol}] ✅ {total} ticks (bougie close) {start_ts} → {end_ts} | "
                f"dernier_tick={df[tcol].max()} | BUY={buy_n} | SELL={sell_n} | UNK={unk_n} | couverture={coverage:.1f}s"
            )

            # ── Sortie ordonnée
            out = df.copy()
            for c in ret_cols:
                if c not in out.columns:
                    out[c] = np.nan if c in ("mid", "spread") else 0
            return out[ret_cols]

        except Exception as e:
            self.logger.error(
                f"[MT5C] Erreur get_ticks_for_candle {symbol}: {e}", exc_info=True
            )
            return pd.DataFrame(columns=ret_cols)

    def get_symbol_info(self, symbol: str) -> Optional[Any]:
        """
        Récupère les informations d'un symbole (spread, point, visibilité, etc.)
        avec sélection automatique dans la Market Watch et fallback si MT5 ne fournit pas tout.

        Args:
            symbol (str): Le symbole MT5 (ex: "EURUSD", "XAUUSD").

        Returns:
            SymbolInfoFallback ou MetaTrader5.SymbolInfo
        """
        if not self.is_connected:
            self.logger.error(
                f"[MT5C] Non connecté. Impossible de récupérer les informations du symbole '{symbol}'."
            )
            return None

        symbol_norm = str(symbol).strip().upper()
        self.logger.debug(
            f"[MT5C] Tentative récupération infos pour '{symbol_norm}'..."
        )

        try:
            # Rendre le symbole visible si nécessaire
            try:
                selected_ok = self.mt5.symbol_select(symbol_norm, True)
                if not selected_ok:
                    self.logger.debug(
                        f"[MT5C] symbol_select('{symbol_norm}', True) a retourné False "
                        f"(déjà visible ou symbole non dispo)."
                    )
            except Exception as sel_e:
                self.logger.debug(
                    f"[MT5C] Exception lors de symbol_select('{symbol_norm}'): {sel_e}"
                )

            # Récupération brute
            info = self.mt5.symbol_info(symbol_norm)

            if info is None:
                last_mt5_error = self.mt5.last_error()
                self.logger.error(
                    f"[MT5C] symbol_info('{symbol_norm}') a retourné None. "
                    f"Dernière erreur MT5: {last_mt5_error}."
                )
                return None

            # Récup fallback pour les valeurs critiques
            try:
                # contract_size
                contract_size = getattr(info, "trade_contract_size", None) or getattr(
                    info, "contract_size", None
                )
                if not contract_size or contract_size <= 0:
                    if symbol_norm.startswith("XAU"):
                        contract_size = 100.0
                    elif len(symbol_norm) == 6 and symbol_norm.endswith("USD"):
                        contract_size = 100000.0
                    else:
                        contract_size = 1.0
                    self.logger.warning(
                        f"[FALLBACK] contract_size fixé à {contract_size} pour {symbol_norm}"
                    )

                # tick_size
                tick_size = getattr(info, "trade_tick_size", None) or getattr(
                    info, "point", None
                )
                if not tick_size or tick_size <= 0:
                    digits = getattr(info, "digits", 5)
                    tick_size = 10 ** (-digits)
                    self.logger.warning(
                        f"[FALLBACK] tick_size dérivé de digits={digits} pour {symbol_norm}"
                    )

                # point
                point_val = getattr(info, "point", None)
                if not point_val or point_val <= 0:
                    point_val = 10 ** (-getattr(info, "digits", 5))

                # Retour fallback toujours complet
                wrapped = SymbolInfoFallback(
                    symbol=symbol_norm,
                    spread=getattr(info, "spread", 0),
                    point=point_val,
                    digits=getattr(info, "digits", 5),
                    trade_contract_size=contract_size,
                    trade_tick_size=tick_size,
                )

                self.logger.info(
                    f"[MT5C] Infos '{symbol_norm}' récupérées. Spread={wrapped.spread}, "
                    f"Point={wrapped.point}, Contract={wrapped.trade_contract_size}, TickSize={wrapped.trade_tick_size}"
                )
                return wrapped

            except Exception as fe:
                self.logger.error(
                    f"[FALLBACK] Erreur fallback pour {symbol_norm}: {fe}"
                )
                return info

        except Exception as e:
            self.logger.error(
                f"[MT5C] Exception dans get_symbol_info('{symbol_norm}'): {e}. "
                f"Dernière erreur MT5: {self.mt5.last_error()}",
                exc_info=True,
            )
            return None

    def resolve_broker_symbol(self, base_symbol: str) -> str:
        """Retourne le symbole broker résolu. N’essaie les suffixes QUE si la base échoue."""
        base = str(base_symbol).strip().upper()
        tested = [base]

        # 1) Base d'abord (et on s'arrête si OK)
        if self.get_symbol_info(base):
            return base

        # 2) Sinon seulement, tester les suffixes (config ou défaut)
        suffixes = self.config_manager.get(
            "mt5_symbol_suffixes", [".A", ".I", ".R", ".M"]
        )
        for suf in suffixes:
            cand = f"{base}{str(suf)}".upper()
            tested.append(cand)
            if self.get_symbol_info(cand):
                return cand

        self.logger.error(
            f"Symbole MT5 introuvable pour '{base_symbol}'. Testés: {', '.join(tested)}. "
            f"Vérifie le mapping broker / suffixe exact dans le Market Watch."
        )
        return ""

    def ensure_symbol_selected(self, symbol: str) -> bool:
        """
        Rend le symbole visible dans le Market Watch et vérifie la visibilité.
        Retourne True si le symbole est sélectionné/visible, False sinon.
        """
        try:
            sym = str(symbol or "").strip().upper()
            if not sym:
                return False
            ok = self.mt5.symbol_select(sym, True)
            info = self.mt5.symbol_info(sym)
            return bool(ok or (info and getattr(info, "visible", True)))
        except Exception as e:
            # On loggue en warning, on renvoie False (la préparation d'ordre décidera quoi faire)
            self.logger.warning(f"ensure_symbol_selected({symbol}) failed: {e}")
            return False

    def get_symbol_info_tick(self, symbol: str):
        """
        Retourne le dernier tick du symbole depuis MetaTrader 5 (bid/ask/last).
        """
        try:
            tick = self.mt5.symbol_info_tick(symbol)
            if tick is None:
                self.logger.error(
                    f"[MT5C] Impossible de récupérer le tick pour {symbol}."
                )
                return None
            self.logger.debug(
                f"[MT5C] Tick {symbol} → bid={tick.bid}, ask={tick.ask}, last={tick.last}"
            )
            return tick
        except Exception as e:
            self.logger.error(f"[MT5C] Erreur get_symbol_info_tick pour {symbol}: {e}")
            return None

    def get_symbol_spread_points(self, symbol: str) -> float:
        """
        Retourne le spread courant en *points* pour `symbol`.

        Ordre de décision :
        1) info.spread (déjà en points) si > 0
        2) (ask - bid) / point depuis symbol_info_tick
        3) fallback ask/bid depuis symbol_info
        4) reconstruction via Depth of Market (market_book_get)
        Choix du `point` : priorité à info.point, sinon trade_tick_size, sinon 10**(-digits)
        Jamais NaN/Inf : retourne 0.0 si non calculable.
        """
        try:
            sym = (symbol or "").strip().upper()
            if not sym:
                return 0.0

            # Assure la visibilité si besoin
            info = self.mt5.symbol_info(sym)
            if info is None or getattr(info, "visible", True) is False:
                try:
                    self.mt5.symbol_select(sym, True)
                except Exception:
                    pass
                info = self.mt5.symbol_info(sym)

            # 1) Spread natif (déjà en points)
            try:
                native = float(getattr(info, "spread", 0) or 0.0) if info else 0.0
                if native > 0:
                    return float(round(native, 2))
            except Exception:
                pass

            # Détermination du point (ordre de préférence)
            point = 0.0
            try:
                p = float(getattr(info, "point", 0.0) or 0.0) if info else 0.0
                if p > 0:
                    point = p
                else:
                    tts = (
                        float(getattr(info, "trade_tick_size", 0.0) or 0.0)
                        if info
                        else 0.0
                    )
                    if tts > 0:
                        point = tts
                    else:
                        digits = int(getattr(info, "digits", 0) or 0) if info else 0
                        if digits > 0:
                            point = 10.0 ** (-digits)
            except Exception:
                point = 0.0

            ask = bid = None

            # 2) Tick en priorité
            try:
                tick = self.mt5.symbol_info_tick(sym)
                if tick:
                    a = getattr(tick, "ask", None)
                    b = getattr(tick, "bid", None)
                    a = float(a) if isinstance(a, (int, float)) else None
                    b = float(b) if isinstance(b, (int, float)) else None
                    if a and b and a > 0 and b > 0 and a > b:
                        ask, bid = a, b
            except Exception:
                pass

            # 3) Fallback : symbol_info.{ask,bid}
            if ask is None or bid is None or not (ask > bid > 0):
                try:
                    a = float(getattr(info, "ask", 0.0) or 0.0) if info else 0.0
                    b = float(getattr(info, "bid", 0.0) or 0.0) if info else 0.0
                    if a > 0 and b > 0 and a > b:
                        ask, bid = a, b
                except Exception:
                    pass

            # 4) Dernier recours : Depth of Market
            if (ask is None or bid is None or not (ask > bid)) or point <= 0.0:
                try:
                    book = self.mt5.market_book_get(sym)
                    if book:
                        best_ask = None
                        best_bid = None
                        for x in book:
                            t = getattr(x, "type", None)
                            price = getattr(x, "price", None)
                            if not isinstance(price, (int, float)) or price <= 0:
                                continue
                            if t == getattr(self.mt5, "BOOK_TYPE_SELL", 1):
                                best_ask = (
                                    price
                                    if (best_ask is None or price < best_ask)
                                    else best_ask
                                )
                            elif t == getattr(self.mt5, "BOOK_TYPE_BUY", 2):
                                best_bid = (
                                    price
                                    if (best_bid is None or price > best_bid)
                                    else best_bid
                                )
                        if (
                            isinstance(best_ask, (int, float))
                            and isinstance(best_bid, (int, float))
                            and best_ask > best_bid
                        ):
                            ask, bid = float(best_ask), float(best_bid)
                except Exception:
                    pass

            # Calcul final
            if (
                isinstance(ask, (int, float))
                and isinstance(bid, (int, float))
                and ask > bid
                and point > 0
            ):
                spread_pts = (ask - bid) / point
                # garde-fous num
                if not (spread_pts == spread_pts) or spread_pts <= 0:
                    return 0.0
                # En points : arrondi léger (FX souvent entier)
                return float(round(spread_pts, 2))

        except Exception as e:
            self.logger.debug(
                f"[get_symbol_spread_points] erreur pour {symbol}: {e}", exc_info=True
            )

        # Jamais inf/NaN
        return 0.0

    def order_send(self, request: Dict[str, Any]) -> Optional[Any]:
        """
        Wrapper d'envoi MT5 robuste (simplifié) :
        - Normalise action/type/price/SL/TP
        - Normalise volume (min/step/max) en FLOOR
        - Guards PENDING vs bid/ask + stops/freeze + buffer
        - Filling par défaut : RETURN pour PENDING, IOC pour MARKET (+ fallback IOC→FOK→RETURN)
        - Retry: REQUOTE/PRICE_OFF/TIMEOUT/NO_CONNECTION (refresh prix si MARKET)
        - Enforce distance mini SL/TP; INVALID_STOPS → re-send sans SL/TP (+ attach après exec MARKET)
        """
        import math, re, time

        # --- Connexion ---
        try:
            connected = bool(
                self.is_connected()
                if callable(self.is_connected)
                else self.is_connected
            )
        except TypeError:
            connected = bool(getattr(self, "is_connected", False))
        if not connected:
            try:
                rec = getattr(self, "connect", None) or getattr(
                    self, "reconnect_if_needed", None
                )
                if callable(rec):
                    rec()
                connected = bool(
                    self.is_connected()
                    if callable(self.is_connected)
                    else self.is_connected
                )
            except Exception:
                pass
        if not connected:
            try:
                self.config_manager.send_alert(
                    "MT5 Non Connecté: Échec Envoi Ordre.", "telegram_critical"
                )
            except Exception:
                pass
            return None

        mt5 = self.mt5
        maps = self.mt5_mappings or {}

        # --- Helpers ---
        def _mt5_const(group: str, key: str, default_name: str):
            try:
                name = (maps.get(group, {}) or {}).get(key, default_name)
                return getattr(mt5, name)
            except Exception:
                return getattr(mt5, default_name, None)

        def _resolve_order_type(t):
            if isinstance(t, (int, float)):
                return int(t)
            if isinstance(t, str):
                s = t.strip().upper()
                m = {
                    "BUY": "ORDER_TYPE_BUY",
                    "SELL": "ORDER_TYPE_SELL",
                    "BUY_LIMIT": "ORDER_TYPE_BUY_LIMIT",
                    "SELL_LIMIT": "ORDER_TYPE_SELL_LIMIT",
                    "BUY_STOP": "ORDER_TYPE_BUY_STOP",
                    "SELL_STOP": "ORDER_TYPE_SELL_STOP",
                    "BUY_STOP_LIMIT": "ORDER_TYPE_BUY_STOP_LIMIT",
                    "SELL_STOP_LIMIT": "ORDER_TYPE_SELL_STOP_LIMIT",
                }
                if s in m:
                    return _mt5_const("order_types", s, m[s])
            return None

        def _resolve_action(a):
            if isinstance(a, (int, float)):
                return int(a)
            if isinstance(a, str):
                s = a.strip().upper()
                if s in ("MARKET", "DEAL"):
                    return _mt5_const("trade_actions", "DEAL", "TRADE_ACTION_DEAL")
                if s in ("PENDING", "ORDER"):
                    return _mt5_const(
                        "trade_actions", "PENDING", "TRADE_ACTION_PENDING"
                    )
                if s in ("MODIFY", "SLTP"):
                    return _mt5_const("trade_actions", "SLTP", "TRADE_ACTION_SLTP")
                if s in ("CLOSE", "DEAL_CLOSE"):
                    return _mt5_const("trade_actions", "DEAL", "TRADE_ACTION_DEAL")
            return None

        def _safe_num(x):
            try:
                if x is None:
                    return None
                v = float(x)
                return v if math.isfinite(v) else None
            except Exception:
                return None

        def _norm_comment(
            text: str, fallback: str = "SNIPER_X", max_len: int = 31
        ) -> str:
            raw = str(text if text is not None else fallback)
            raw = raw.replace("|", "").replace(" ", "")
            raw = re.sub(r"[^A-Za-z0-9._-]", "", raw)
            return raw[:max_len]

        def _sanitize_sltp(req: dict, digits: int):
            sl = _safe_num(req.get("sl") or req.get("stop_loss") or req.get("sl_price"))
            tp = _safe_num(
                req.get("tp") or req.get("take_profit") or req.get("tp_price")
            )
            if isinstance(sl, (int, float)):
                req["sl"] = round(float(sl), digits)
            else:
                req.pop("sl", None)
            if isinstance(tp, (int, float)):
                req["tp"] = round(float(tp), digits)
            else:
                req.pop("tp", None)
            for k in ("stop_loss", "sl_price", "take_profit", "tp_price"):
                req.pop(k, None)
            return req.get("sl"), req.get("tp")
             

        def _symbol_ctx(symbol: str):
            si = None
            try:
                si = (
                    self.symbol_info(symbol)
                    if callable(getattr(self, "symbol_info", None))
                    else mt5.symbol_info(symbol)
                )
            except Exception:
                pass
            if not si:
                return None
            return {
                "digits": int(getattr(si, "digits", 5) or 5),
                "point": float(getattr(si, "point", 10**-5) or 10**-5),
                "min_vol": float(
                    getattr(si, "trade_min_volume", getattr(si, "volume_min", 0.01))
                    or 0.01
                ),
                "max_vol": float(
                    getattr(si, "trade_max_volume", getattr(si, "volume_max", 100.0))
                    or 100.0
                ),
                "vol_step": float(
                    getattr(si, "trade_volume_step", getattr(si, "volume_step", 0.01))
                    or 0.01
                ),
                "stops_level": int(
                    getattr(si, "trade_stops_level", getattr(si, "stops_level", 0)) or 0
                ),
                "freeze_level": int(
                    getattr(si, "trade_freeze_level", getattr(si, "freeze_level", 0))
                    or 0
                ),
                "spread_pts": int(getattr(si, "spread", 0) or 0),
                "name": getattr(si, "name", None) or symbol,
            }

        def _enforce_sltp_constraints(
            symbol: str, price: float, order_type: int, sl: float, tp: float, ctx: dict
        ):
            digits, pt = ctx["digits"], ctx["point"]
            try:
                buffer_pts = int(
                    self.config_manager.get("risk.sltp_extra_buffer_points", 2) or 2
                )
            except Exception:
                buffer_pts = 2
            min_pts = max(
                ctx["stops_level"], ctx["freeze_level"], ctx["spread_pts"]
            ) + max(0, buffer_pts)
            min_dist = min_pts * pt

            BUY = _mt5_const("order_types", "BUY", "ORDER_TYPE_BUY")
            SELL = _mt5_const("order_types", "SELL", "ORDER_TYPE_SELL")

            try:
                tk = mt5.symbol_info_tick(symbol)
                bid = float(getattr(tk, "bid", 0.0) or 0.0)
                ask = float(getattr(tk, "ask", 0.0) or 0.0)
            except Exception:
                bid = ask = 0.0

            adj_sl, adj_tp = sl, tp
            if order_type == BUY:
                if isinstance(sl, (int, float)):
                    limit = (bid or price) - min_dist
                    if sl >= limit:
                        adj_sl = round(limit, digits)
                if isinstance(tp, (int, float)):
                    limit = price + min_dist
                    if tp <= limit:
                        adj_tp = round(limit, digits)
            elif order_type == SELL:
                if isinstance(sl, (int, float)):
                    limit = (ask or price) + min_dist
                    if sl <= limit:
                        adj_sl = round(limit, digits)
                if isinstance(tp, (int, float)):
                    limit = price - min_dist
                    if tp >= limit:
                        adj_tp = round(limit, digits)
            return adj_sl, adj_tp, min_pts

        def _pending_trigger_ok(symbol: str, order_type: int, trig: float, ctx: dict):
            try:
                tk = mt5.symbol_info_tick(symbol)
                bid = float(getattr(tk, "bid", 0.0) or 0.0)
                ask = float(getattr(tk, "ask", 0.0) or 0.0)
            except Exception:
                return False, "tick_unavailable"
            pt = ctx["point"]
            try:
                buffer_pts = int(
                    self.config_manager.get("risk.sltp_extra_buffer_points", 2) or 2
                )
            except Exception:
                buffer_pts = 2
            min_pts = max(
                ctx["stops_level"], ctx["freeze_level"], ctx["spread_pts"]
            ) + max(0, buffer_pts)
            min_dist = min_pts * pt

            BUY_LIMIT = _mt5_const("order_types", "BUY_LIMIT", "ORDER_TYPE_BUY_LIMIT")
            SELL_LIMIT = _mt5_const(
                "order_types", "SELL_LIMIT", "ORDER_TYPE_SELL_LIMIT"
            )
            BUY_STOP = _mt5_const("order_types", "BUY_STOP", "ORDER_TYPE_BUY_STOP")
            SELL_STOP = _mt5_const("order_types", "SELL_STOP", "ORDER_TYPE_SELL_STOP")

            if order_type == BUY_LIMIT and not (trig < ask - min_dist):
                return False, f"BUY_LIMIT trig >= ask-min({ask:.5f}-{min_dist:.5f})"
            if order_type == SELL_LIMIT and not (trig > bid + min_dist):
                return False, f"SELL_LIMIT trig <= bid+min({bid:.5f}+{min_dist:.5f})"
            if order_type == BUY_STOP and not (trig > ask + min_dist):
                return False, f"BUY_STOP trig <= ask+min({ask:.5f}+{min_dist:.5f})"
            if order_type == SELL_STOP and not (trig < bid - min_dist):
                return False, f"SELL_STOP trig >= bid-min({bid:.5f}-{min_dist:.5f})"
            return True, "ok"

        # --- Champs minimaux ---
        symbol = request.get("symbol")
        volume = request.get("volume")
        order_type = _resolve_order_type(request.get("type"))
        action = _resolve_action(request.get("action"))
        if order_type is None and isinstance(request.get("order_type"), (str, int)):
            order_type = _resolve_order_type(request.get("order_type"))
        if action is None and isinstance(request.get("order_action"), (str, int)):
            action = _resolve_action(request.get("order_action"))

        if not symbol or not isinstance(symbol, str):
            self.logger.error(f"MT5: 'symbol' manquant/invalide. Req={request}")
            return None
        try:
            volume = float(volume)
        except Exception:
            volume = 0.0
        if not (volume > 0):
            self.logger.error(f"MT5: 'volume' <= 0 pour {symbol}. Req={request}")
            return None
        if order_type is None:
            self.logger.error(f"MT5: 'type' manquant/illégal. Req={request}")
            return None
        if action is None:
            action = _mt5_const("trade_actions", "DEAL", "TRADE_ACTION_DEAL")

        request["comment"] = _norm_comment(request.get("comment"), "SNIPER_X")
        if "magic" not in request:
            try:
                request["magic"] = int(
                    self.config_manager.get("defaults.magic_number", 0) or 0
                )
            except Exception:
                request["magic"] = 0
        if "deviation" not in request:
            try:
                request["deviation"] = int(
                    self.config_manager.get(
                        "trade_executor_settings.slippage_points", 5
                    )
                    or 5
                )
            except Exception:
                request["deviation"] = 5
        if "type_time" not in request:
            request["type_time"] = getattr(mt5, "ORDER_TIME_GTC", None)

        try:
            mt5.symbol_select(symbol, True)
        except Exception:
            pass

        # --- Contexte symbole & volume (VALIDATION SEULE, pas d'ajustement) ---
        ctx = _symbol_ctx(symbol)
        if not ctx:
            self.logger.error(f"MT5: symbol_info indisponible pour {symbol}.")
            return None

        vmin, vmax, vstep = ctx["min_vol"], ctx["max_vol"], ctx["vol_step"]
        vol_in = float(volume)

        # 1) Bornes broker
        if not (vol_in >= vmin and vol_in <= vmax):
            self.logger.error(
                f"MT5: Volume hors bornes broker (vol={vol_in}, min={vmin}, max={vmax}). "
                f"Aucun auto-ajustement — échec dur (exécution pure)."
            )
            return None

        # 2) Alignement au pas broker (FLOOR interdit ici → pure exécution)
        def _is_step_aligned(vol: float, base: float, step: float) -> bool:
            if step <= 0:
                return True
            steps = round((vol - base) / step)
            aligned = base + steps * step
            return abs(aligned - vol) < 1e-12

        if not _is_step_aligned(vol_in, vmin, vstep):
            self.logger.error(
                f"MT5: Volume non aligné au step broker (vol={vol_in}, base={vmin}, step={vstep}). "
                f"Aucun auto-ajustement — échec dur (exécution pure)."
            )
            return None

        # ✅ Exécution pure : on NE MODIFIE PAS le volume transmis
        request["volume"] = round(vol_in, 8)

        # --- Prix / type / filling défaut ---
        digits = ctx["digits"]
        try:
            price_val = float(request.get("price"))
        except Exception:
            price_val = 0.0

        BUY = _mt5_const("order_types", "BUY", "ORDER_TYPE_BUY")
        SELL = _mt5_const("order_types", "SELL", "ORDER_TYPE_SELL")
        BUY_LIMIT = _mt5_const("order_types", "BUY_LIMIT", "ORDER_TYPE_BUY_LIMIT")
        SELL_LIMIT = _mt5_const("order_types", "SELL_LIMIT", "ORDER_TYPE_SELL_LIMIT")
        BUY_STOP = _mt5_const("order_types", "BUY_STOP", "ORDER_TYPE_BUY_STOP")
        SELL_STOP = _mt5_const("order_types", "SELL_STOP", "ORDER_TYPE_SELL_STOP")

        is_market_action = action == _mt5_const(
            "trade_actions", "DEAL", "TRADE_ACTION_DEAL"
        )
        is_market_type = order_type in (BUY, SELL)
        is_pending_type = order_type in (BUY_LIMIT, SELL_LIMIT, BUY_STOP, SELL_STOP)

        def _market_price(side: str) -> float:
            try:
                t = mt5.symbol_info_tick(symbol)
                if not t:
                    return 0.0
                if side == "BUY" and getattr(t, "ask", None):
                    return float(t.ask)
                if side == "SELL" and getattr(t, "bid", None):
                    return float(t.bid)
                return float(getattr(t, "ask", 0.0) or getattr(t, "bid", 0.0) or 0.0)
            except Exception:
                return 0.0

        if (
            is_market_action
            and is_market_type
            and (not price_val or not math.isfinite(price_val) or price_val <= 0)
        ):
            side = "BUY" if order_type == BUY else "SELL"
            px = _market_price(side)
            if px <= 0:
                self.logger.error(
                    f"MT5: Prix market indisponible pour {symbol} ({side})."
                )
                return None
            request["price"] = round(float(px), digits)
            price_val = request["price"]
        elif price_val > 0:
            request["price"] = round(float(price_val), digits)
            price_val = request["price"]
        elif is_pending_type:
            self.logger.error(
                f"MT5: Prix requis pour un PENDING {symbol}. Req={request}"
            )
            return None

        if "stoplimit" in request and isinstance(request["stoplimit"], (int, float)):
            request["stoplimit"] = round(float(request["stoplimit"]), digits)

        if "type_filling" not in request:
            request["type_filling"] = (
                _mt5_const("type_filling", "RETURN", "ORDER_FILLING_RETURN")
                if is_pending_type
                else _mt5_const("type_filling", "IOC", "ORDER_FILLING_IOC")
            )

        # --- SL/TP & guards ---
        sl_val, tp_val = _sanitize_sltp(request, digits)

        if is_pending_type and price_val:
            ok_trg, why = _pending_trigger_ok(symbol, order_type, price_val, ctx)
            if not ok_trg:
                self.logger.error(f"MT5: Pending guard refusé ({why}). Req={request}")
                return None
            side_for_future = BUY if order_type in (BUY_LIMIT, BUY_STOP) else SELL
            if sl_val is not None or tp_val is not None:
                sl_fix, tp_fix, min_pts = _enforce_sltp_constraints(
                    symbol, price_val, side_for_future, sl_val, tp_val, ctx
                )
                if sl_fix != sl_val or tp_fix != tp_val:
                    if sl_fix is not None:
                        request["sl"] = sl_fix
                    else:
                        request.pop("sl", None)
                    if tp_fix is not None:
                        request["tp"] = tp_fix
                    else:
                        request.pop("tp", None)
                    sl_val, tp_val = sl_fix, tp_fix

        if (
            is_market_type
            and is_market_action
            and (sl_val is not None or tp_val is not None)
        ):
            sl_fix, tp_fix, _ = _enforce_sltp_constraints(
                symbol, price_val, order_type, sl_val, tp_val, ctx
            )
            if sl_fix != sl_val or tp_fix != tp_val:
                if sl_fix is not None:
                    request["sl"] = sl_fix
                if tp_fix is not None:
                    request["tp"] = tp_fix
                sl_val, tp_val = sl_fix, tp_fix

        # --- Logs lisibles ---
        type_name_map = {
            BUY: "BUY",
            SELL: "SELL",
            BUY_LIMIT: "BUY_LIMIT",
            SELL_LIMIT: "SELL_LIMIT",
            BUY_STOP: "BUY_STOP",
            SELL_STOP: "SELL_STOP",
        }
        self.logger.info(
            f"MT5: Envoi ordre: {type_name_map.get(order_type, order_type)} {request.get('volume')} {symbol} @ {request.get('price')} sl={request.get('sl')} tp={request.get('tp')}"
        )

        # --- Retcodes / fallback fillings ---
        RET_DONE = getattr(
            mt5,
            (maps.get("trade_retcodes", {}) or {}).get("DONE", "TRADE_RETCODE_DONE"),
            None,
        )
        RET_PLACED = getattr(
            mt5,
            (maps.get("trade_retcodes", {}) or {}).get(
                "PLACED", "TRADE_RETCODE_PLACED"
            ),
            None,
        )
        RET_DONE_PARTIAL = getattr(
            mt5,
            (maps.get("trade_retcodes", {}) or {}).get(
                "DONE_PARTIAL", "TRADE_RETCODE_DONE_PARTIAL"
            ),
            None,
        )
        RET_INVALID_FILL = (
            getattr(
                mt5,
                (maps.get("trade_retcodes", {}) or {}).get(
                    "INVALID_FILL", "TRADE_RETCODE_INVALID_FILL"
                ),
                None,
            )
            or 10030
        )
        RET_INVALID_STOPS = (
            getattr(
                mt5,
                (maps.get("trade_retcodes", {}) or {}).get(
                    "INVALID_STOPS", "TRADE_RETCODE_INVALID_STOPS"
                ),
                None,
            )
            or 10016
        )
        RET_REQUOTE = getattr(
            mt5,
            (maps.get("trade_retcodes", {}) or {}).get(
                "REQUOTE", "TRADE_RETCODE_REQUOTE"
            ),
            None,
        )
        RET_PRICE_OFF = getattr(
            mt5,
            (maps.get("trade_retcodes", {}) or {}).get(
                "PRICE_OFF", "TRADE_RETCODE_PRICE_OFF"
            ),
            None,
        ) or getattr(mt5, "TRADE_RETCODE_INVALID_PRICE", None)
        RET_TIMEOUT = getattr(
            mt5,
            (maps.get("trade_retcodes", {}) or {}).get(
                "TIMEOUT", "TRADE_RETCODE_TIMEOUT"
            ),
            None,
        )
        RET_NO_CONN = getattr(
            mt5,
            (maps.get("trade_retcodes", {}) or {}).get(
                "NO_CONNECTION", "TRADE_RETCODE_NO_CONNECTION"
            ),
            None,
        )

        success_set = (RET_DONE, RET_PLACED, RET_DONE_PARTIAL)
        executed_set = (RET_DONE, RET_DONE_PARTIAL)
        retryable = {
            x
            for x in (RET_REQUOTE, RET_PRICE_OFF, RET_TIMEOUT, RET_NO_CONN)
            if x is not None
        }

        if is_pending_type:
            filling_candidates = [
                getattr(mt5, "ORDER_FILLING_RETURN", None),
                getattr(mt5, "ORDER_FILLING_IOC", None),
                getattr(mt5, "ORDER_FILLING_FOK", None),
            ]
        else:
            filling_candidates = [
                request.get("type_filling"),
                getattr(mt5, "ORDER_FILLING_IOC", None),
                getattr(mt5, "ORDER_FILLING_FOK", None),
                getattr(mt5, "ORDER_FILLING_RETURN", None),
            ]
        filling_candidates = [tf for tf in filling_candidates if tf is not None]

        try:
            max_retries = int(
                self.config_manager.get(
                    "trade_executor_settings.order_send_max_retries", 2
                )
                or 2
            )
            sleep_between = float(
                self.config_manager.get(
                    "trade_executor_settings.order_send_retry_sleep_s", 0.05
                )
                or 0.05
            )
        except Exception:
            max_retries, sleep_between = 2, 0.05

        def _refresh_price_for_retry(req: dict):
            if not (is_market_action and is_market_type):
                return
            side = "BUY" if order_type == BUY else "SELL"
            new_px = 0.0
            try:
                t = mt5.symbol_info_tick(symbol)
                if t:
                    new_px = float(
                        getattr(t, "ask" if side == "BUY" else "bid", 0.0) or 0.0
                    )
            except Exception:
                new_px = 0.0
            if new_px > 0:
                req["price"] = round(new_px, digits)

        last_res = None
        result = None

        for tf in filling_candidates:
            req_base = dict(request)
            req_base["type_filling"] = tf
            attempt = 0
            while attempt <= max_retries:
                req = dict(req_base)
                try:
                    res = mt5.order_send(req)
                except Exception as ex:
                    self.logger.exception(
                        f"MT5: Exception order_send() (filling={tf}, try={attempt+1}/{max_retries+1}): {ex}"
                    )
                    res = None

                last_res = res
                if res is None:
                    attempt += 1
                    if attempt <= max_retries:
                        time.sleep(sleep_between)
                        _refresh_price_for_retry(req_base)
                        continue
                    break

                ret = getattr(res, "retcode", None)
                cmt = getattr(res, "comment", "N/A")
                self.logger.info(
                    f"MT5: Retcode={ret} (filling={tf}, try={attempt+1}) comment={cmt}"
                )

                if ret in success_set:
                    result = res
                    if (
                        (ret in executed_set)
                        and is_market_action
                        and (sl_val is not None or tp_val is not None)
                    ):
                        # attacher SL/TP sur la dernière position **du BOT** (filtrée par magic/comment)
                        def _attach_sltp(symbol: str, target_sl, target_tp):
                            req_mod = {
                                "action": _mt5_const("trade_actions", "SLTP", "TRADE_ACTION_SLTP"),
                                "symbol": symbol,
                            }
                            try:
                                positions = list(mt5.positions_get(symbol=symbol) or [])
                            except Exception:
                                positions = []

                            # 🔒 Filtrage strict: ne toucher **que** les positions du bot
                            bot_magic = int(request.get("magic", 0))
                            bot_comment = str(request.get("comment", ""))

                            positions = [
                                p for p in positions
                                if int(getattr(p, "magic", -1)) == bot_magic
                                and (not bot_comment or bot_comment in str(getattr(p, "comment", "")))
                            ]
                            if not positions:
                                self.logger.info("[SLTP] Skip attach: aucune position du bot (magic/comment) sur %s", symbol)
                                return None

                            latest = sorted(positions, key=lambda p: int(getattr(p, "ticket", 0)), reverse=True)[0]
                            req_mod["position"] = int(getattr(latest, "ticket", 0))

                            if target_sl is not None:
                                req_mod["sl"] = float(target_sl)
                            if target_tp is not None:
                                req_mod["tp"] = float(target_tp)

                            return mt5.order_send(req_mod)

                        res_mod = _attach_sltp(symbol, request.get("sl"), request.get("tp"))
                        if getattr(res_mod, "retcode", None) == RET_INVALID_STOPS:
                            sl_fix, tp_fix, _ = _enforce_sltp_constraints(
                                symbol,
                                request.get("price"),
                                order_type,
                                request.get("sl"),
                                request.get("tp"),
                                ctx,
                            )
                            _ = _attach_sltp(symbol, sl_fix, tp_fix)


                            def _attach2(symbol: str, target_sl, target_tp):
                                req_mod = {
                                    "action": _mt5_const(
                                        "trade_actions", "SLTP", "TRADE_ACTION_SLTP"
                                    ),
                                    "symbol": symbol,
                                }
                                try:
                                    positions = list(
                                        mt5.positions_get(symbol=symbol) or []
                                    )
                                except Exception:
                                    positions = []
                                if not positions:
                                    return None
                                latest = sorted(
                                    positions,
                                    key=lambda p: int(getattr(p, "ticket", 0)),
                                    reverse=True,
                                )[0]
                                req_mod["position"] = int(getattr(latest, "ticket"))
                                if target_sl is not None:
                                    req_mod["sl"] = target_sl
                                if target_tp is not None:
                                    req_mod["tp"] = target_tp
                                return mt5.order_send(req_mod)

                            sl_w, tp_w, _ = _enforce_sltp_constraints(
                                symbol,
                                request.get("price"),
                                order_type,
                                sl_val,
                                tp_val,
                                ctx,
                            )
                            _ = _attach2(symbol, sl_w, tp_w)
                        break
                    break

                # Filling non supporté
                if ret in (10030, getattr(mt5, "TRADE_RETCODE_INVALID_FILL", None)) or (
                    "Unsupported filling mode" in str(cmt)
                ):
                    self.logger.warning(
                        f"MT5: Filling {tf} non supporté → on essaie un autre."
                    )
                    break

                break  # non-retryable

            if result is not None:
                break

        # --- Sorties ---
        if result is not None:
            return result

        if last_res is not None:
            ret = getattr(last_res, "retcode", None)
            try:
                last_err = mt5.last_error()
                last_err_str = (
                    f"{last_err}"
                    if not isinstance(last_err, (tuple, list))
                    else " | ".join(map(str, last_err))
                )
            except Exception:
                last_err_str = "N/A"
            self.logger.warning(
                f"MT5: Ordre non exécuté. Retcode={ret}. SysErr={last_err_str}."
            )
            try:
                self.config_manager.send_alert(
                    f"MT5: Ordre non exécuté (ret={ret}). Comment: {getattr(last_res,'comment','N/A')}",
                    "telegram_critical",
                )
            except Exception:
                pass
            return last_res

        try:
            last_err = mt5.last_error()
            last_err_str = (
                f"{last_err}"
                if not isinstance(last_err, (tuple, list))
                else " | ".join(map(str, last_err))
            )
        except Exception:
            last_err_str = "N/A"
        self.logger.error(
            f"MT5: order_send a échoué. Aucune réponse. SysErr={last_err_str}."
        )
        try:
            self.config_manager.send_alert(
                f"MT5: Échec envoi ordre: Aucune réponse. {last_err_str}",
                "telegram_critical",
            )
        except Exception:
            pass
        return None

    def get_trade_history(self) -> pd.DataFrame:
        """
        Récupère l'historique des trades (deals) depuis MetaTrader 5 pour une période donnée.
        La date de début de l'historique est configurable via ConfigManager.

        Returns:
            pd.DataFrame: Un DataFrame Pandas des deals historiques,
                        ou un DataFrame vide si la récupération échoue ou aucun deal trouvé.
        """
        if not self.is_connected:
            self.logger.error(
                "MT5: Non connecté. Impossible de récupérer l'historique des trades."
            )  # Utilise self.logger
            return pd.DataFrame()

        # La date de début est lue depuis la configuration dynamique.
        # `self.trade_history_start_date_str` est déjà chargé dans __init__ depuis ConfigManager.
        start_date_str = self.trade_history_start_date_str

        try:
            # Assumer le format YYYY-MM-DD pour la config (`trade_history_start_date_str`)
            from_date = datetime.strptime(start_date_str, "%Y-%m-%d").astimezone(
                UTC
            )  # Assure UTC
        except ValueError:
            self.logger.error(
                f"MT5: Format de date invalide pour trade_history_start_date: '{start_date_str}'. Utilisation de 2020-01-01 comme fallback sécuritaire."
            )  # Utilise self.logger
            from_date = datetime(
                2020, 1, 1, tzinfo=UTC
            )  # Fallback sécuritaire en cas d'erreur de format, assure UTC

        to_date = datetime.now(UTC)  # La date de fin reste l'heure actuelle, assure UTC
        deals = self.mt5.history_deals_get(from_date, to_date)  # Utilise self.mt5

        if deals is None:  # Vérifier si la récupération a échoué
            self.logger.error(
                f"MT5: Échec de la récupération des deals historiques de {from_date} à {to_date}. Erreur: {self.mt5.last_error()}."
            )  # Utilise self.logger, self.mt5
            return pd.DataFrame()  # Retourne un DataFrame vide en cas d'erreur

        if len(deals) > 0:
            self.logger.info(
                f"MT5: Récupéré {len(deals)} deals historiques de {from_date} à {to_date}."
            )  # Utilise self.logger
            deals_frame = pd.DataFrame(list(deals))
            # Convertir le timestamp Unix en datetime, assure UTC
            deals_frame["time"] = pd.to_datetime(
                deals_frame["time"], unit="s", utc=True
            )
            return deals_frame
        else:
            self.logger.info(
                f"MT5: Aucun deal historique trouvé de {from_date} à {to_date}."
            )  # Utilise self.logger
            return pd.DataFrame()

    def _detect_separator(self, file_path: str) -> str:
        """
        Détecte le séparateur (virgule, point-virgule, tabulation) d'un fichier CSV.
        Les séparateurs à tester sont configurables dynamiquement.

        Args:
            file_path (str): Chemin du fichier CSV.

        Returns:
            str: Le séparateur détecté (';', ',', '\t') ou la virgule par défaut.
        """
        # Dynamiser la liste des séparateurs à tester (TODO implémenté)
        separators_to_test = self.config_manager.get(
            "mt5_connector_settings.csv_separators_to_test", [";", ",", "\t"]
        )

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                first_line = f.readline()
                for sep in separators_to_test:
                    if sep in first_line:
                        self.logger.debug(
                            f"MT5: Séparateur '{sep}' détecté pour '{file_path}'."
                        )  # Utilise self.logger
                        return sep
                self.logger.warning(
                    f"MT5: Impossible de détecter le séparateur pour '{file_path}'. Utilisation de la virgule par défaut."
                )  # Utilise self.logger
                return ","  # Fallback par défaut si aucun n'est détecté
        except Exception as e:
            self.logger.error(
                f"MT5: Erreur lors de la détection du séparateur pour '{file_path}': {e}",
                exc_info=True,
            )  # Utilise self.logger
            return ","  # Fallback en cas d'erreur de lecture

    def _convert_to_datetime(
        self, df: pd.DataFrame, column: str, formats: Optional[List[str]] = None
    ) -> pd.Series:
        """
        Convertit une colonne de DataFrame en type datetime, en essayant plusieurs formats de date.
        Les formats de date peuvent être configurés dynamiquement.

        Args:
            df (pd.DataFrame): Le DataFrame contenant la colonne à convertir.
            column (str): Le nom de la colonne à convertir.
            formats (List[str], optional): Une liste de formats de date à essayer (ex: ["%Y-%m-%d", "%d/%m/%Y"]).
                                          Si None, les formats dynamiques du ConfigManager seront utilisés.

        Returns:
            pd.Series: La colonne convertie en type datetime (ou pd.NaT pour les échecs de conversion),
                       avec le fuseau horaire UTC.
        """
        # Les formats de date sont dynamisés via ConfigManager (TODO implémenté)
        # self.config_manager.get("mt5_connector_settings.date_formats")
        formats_to_try = (
            formats
            if formats is not None
            else self.config_manager.get(
                "mt5_connector_settings.date_formats", self.DATE_FORMATS
            )
        )

        for fmt in formats_to_try:
            try:
                # Utilise `utc=True` pour que les datetimes soient toujours conscients du fuseau horaire et en UTC
                converted_series = pd.to_datetime(
                    df[column], format=fmt, errors="coerce", utc=True
                )
                if (
                    not converted_series.isnull().all()
                ):  # Vérifie si au moins une conversion a réussi
                    return converted_series
            except (
                ValueError
            ):  # Gère les erreurs de format si 'errors' n'était pas 'coerce'
                # Continuer à essayer d'autres formats
                continue
        self.logger.warning(
            f"MT5: Impossible de convertir la colonne '{column}' en datetime en utilisant les formats spécifiés ({formats_to_try}). Retourne des valeurs NaT."
        )  # Utilise self.logger
        return pd.Series(
            pd.NaT, index=df.index, dtype="datetime64[ns, UTC]"
        )  # Assure le dtype UTC même pour les NaT

    def load_logs(self, file_path: Optional[str] = None) -> pd.DataFrame:
        """
        Charge un ou plusieurs fichiers de logs de trades (généralement CSV) dans un DataFrame Pandas.
        Cette méthode est capable de charger un fichier spécifique ou tous les fichiers
        `.csv` du répertoire de logs configuré. Elle inclut une vérification des duplications
        et un nettoyage initial du DataFrame.

        Args:
            file_path (str, optional): Chemin d'un fichier de log spécifique à charger.
                                    Si `None`, charge tous les fichiers `.csv` du répertoire de logs par défaut.

        Returns:
            pd.DataFrame: Un DataFrame Pandas consolidé de tous les logs chargés,
                        prêt pour le nettoyage et l'analyse. Retourne un DataFrame vide si aucun log n'est chargé.
        """
        all_dfs = []
        if file_path:
            file_list = [Path(file_path)]
            self.logger.info(
                f"MT5: Chargement du fichier de log unique : {file_path}"
            )  # Utilise self.logger
        else:
            # Récupérer le répertoire de logs depuis la configuration dynamique
            log_dir = Path(
                self.config_manager.get("paths.logs", "logs/")
            )  # Utilise self.config_manager
            if not log_dir.is_dir():
                self.logger.info(
                    f"MT5: Le répertoire de logs '{log_dir}' n'a pas été trouvé. Aucun log à charger par défaut."
                )  # Utilise self.logger
                return pd.DataFrame()

            # Utilise un suffixe configurable pour les logs à charger (ex: '.csv', '.jsonl')
            log_file_suffix_to_load = self.config_manager.get(
                "mt5_connector_settings.log_file_extension_to_load", ".csv"
            )
            file_list = list(
                log_dir.glob(f"*{log_file_suffix_to_load}")
            )  # Cherche tous les fichiers avec le suffixe configuré
            self.logger.info(
                f"MT5: Chargement de tous les fichiers de log '{log_file_suffix_to_load}' depuis : {log_dir}"
            )  # Utilise self.logger

        if not file_list:
            self.logger.warning(
                "MT5: Aucun fichier de log correspondant au suffixe trouvé pour le chargement."
            )
            return pd.DataFrame()

        for f_path in file_list:
            try:
                # La méthode _detect_separator est déjà présente
                separator = self._detect_separator(
                    str(f_path)
                )  # Convertir Path en str pour la méthode
                df = pd.read_csv(f_path, sep=separator, encoding="utf-8")
                df["source_file"] = (
                    f_path.name
                )  # Ajoute le nom du fichier source pour l'audit
                all_dfs.append(df)
                self.logger.info(
                    f"MT5: Fichier '{f_path.name}' chargé avec succès."
                )  # Utilise self.logger
            except Exception as e:
                self.logger.error(
                    f"MT5: Échec du chargement du fichier '{f_path.name}': {e}",
                    exc_info=True,
                )  # Utilise self.logger

        if not all_dfs:
            self.logger.warning(
                "MT5: Aucun fichier de log n'a été chargé avec succès."
            )  # Utilise self.logger
            return pd.DataFrame()

        combined_df = pd.concat(all_dfs, ignore_index=True)

        # Supprimer les doublons basés sur toutes les colonnes
        initial_rows_concat = len(combined_df)
        combined_df = combined_df.drop_duplicates()
        if len(combined_df) < initial_rows_concat:
            self.logger.info(
                f"MT5: Supprimé {initial_rows_concat - len(combined_df)} lignes dupliquées pendant la concaténation."
            )  # Utilise self.logger

        return self._clean_dataframe(combined_df)

    def detect_anomalies(self, df: pd.DataFrame) -> Dict[str, List[str]]:
        """
        Détecte diverses anomalies dans le DataFrame des logs de trades.
        Cette fonction est essentielle pour assurer la qualité des données avant l'analyse.

        Args:
            df (pd.DataFrame): Le DataFrame des logs de trades (idéalement déjà nettoyé par _clean_dataframe).

        Returns:
            Dict[str, List[str]]: Un dictionnaire où les clés sont les types d'anomalies
                                  et les valeurs sont des listes de messages décrivant les problèmes.
        """
        self.logger.info(
            "MT5: Détection des anomalies dans les logs..."
        )  # Utilise self.logger
        anomalies = {
            "missing_critical_columns": [],
            "incorrect_data_types": [],
            "missing_values": [],
            "invalid_order_types": [],
            "out_of_range_values": [],
            "duplicate_orders": [],
            "abnormal_profits": [],  # Nouvelle anomalie
        }

        # 1. Colonnes critiques manquantes (déjà géré dans _clean_dataframe, mais vérification finale)
        for (
            col
        ) in self.CRITICAL_COLUMNS:  # self.CRITICAL_COLUMNS est défini dans la classe
            if col not in df.columns:
                anomalies["missing_critical_columns"].append(
                    f"La colonne critique '{col}' est manquante."
                )

        if anomalies["missing_critical_columns"]:
            self.logger.warning(
                "MT5: Saut des vérifications détaillées des anomalies en raison de colonnes critiques manquantes."
            )  # Utilise self.logger
            return anomalies  # Retourne tôt si des colonnes critiques sont absentes

        # 2. Types de données incorrects (après _clean_dataframe, devrait être minimal)
        # Les types attendus peuvent être configurés dynamiquement si le schéma des logs varie.
        expected_types = {
            "Time": "datetime64[ns, UTC]",  # Assurer le type UTC
            "Order": "int64",
            "Symbol": "object",
            "Type": "object",
            "Volume": "float64",
            "Price": "float64",
            "StopLoss": "float64",
            "TakeProfit": "float64",
            "Commission": "float64",
            "Swap": "float64",
            "Profit": "float64",
        }
        for col, expected_type in expected_types.items():
            if col in df.columns and not pd.api.types.is_dtype_equal(
                df[col].dtype, expected_type
            ):
                anomalies["incorrect_data_types"].append(
                    f"La colonne '{col}' a un type incorrect : {df[col].dtype}, attendu {expected_type}."
                )

        # 3. Valeurs manquantes dans les colonnes critiques
        # Les colonnes à vérifier pour les valeurs manquantes pourraient être dynamisées.
        for col in ["Time", "Order", "Symbol", "Volume", "Price"]:
            if df[col].isnull().any():
                anomalies["missing_values"].append(
                    f"Valeurs manquantes dans la colonne '{col}'."
                )

        # 4. Types d'ordre invalides
        if "Type" in df.columns:
            # self.DEFAULT_ALLOWED_ORDER_TYPES est défini dans la classe
            invalid_type_rows = ~df["Type"].isin(self.DEFAULT_ALLOWED_ORDER_TYPES)
            if invalid_type_rows.any():
                invalid_types_found = (
                    df.loc[invalid_type_rows, "Type"].unique().tolist()
                )
                anomalies["invalid_order_types"].append(
                    f"Types d'ordre invalides trouvés : {invalid_types_found}. Types autorisés: {self.DEFAULT_ALLOWED_ORDER_TYPES}."
                )

        # 5. Valeurs hors plage (ex: Volume <= 0, Prix <= 0)
        # Ces vérifications sont déjà corrigées par `_clean_dataframe` mais sont incluses ici pour la détection des ANOMALIES.
        # Les seuils pour les valeurs hors plage peuvent être dynamisés via `mt5_connector_settings`.
        min_volume_threshold = self.config_manager.get(
            "mt5_connector_settings.min_volume_threshold_for_anomaly", 1e-6
        )  # Nouveau param (très petit pour >0)
        min_price_threshold = self.config_manager.get(
            "mt5_connector_settings.min_price_threshold_for_anomaly", 1e-6
        )  # Nouveau param

        if "Volume" in df.columns and (df["Volume"] <= min_volume_threshold).any():
            anomalies["out_of_range_values"].append(
                f"Le volume contient des valeurs non positives ou extrêmement faibles (<{min_volume_threshold})."
            )
        if "Price" in df.columns and (df["Price"] <= min_price_threshold).any():
            anomalies["out_of_range_values"].append(
                f"Le prix contient des valeurs non positives ou extrêmement faibles (<{min_price_threshold})."
            )

        # 6. Ordres dupliqués (basés sur Order ID et Time)
        if "Order" in df.columns and "Time" in df.columns:
            duplicates = df[df.duplicated(subset=["Order", "Time"], keep=False)]
            if not duplicates.empty:
                # Limiter le nombre d'IDs dupliqués logués pour éviter les messages trop longs
                duplicated_orders_ids = duplicates["Order"].unique().tolist()
                anomalies["duplicate_orders"].append(
                    f"IDs d'ordre dupliqués trouvés : {duplicated_orders_ids[:5]}... ({len(duplicated_orders_ids)} uniques)."
                )

        # 7. Profits ou Pertes Anormaux (Nouvelle anomalie)
        # self.abnormal_profit_threshold est déjà chargé dans __init__
        if (
            "Profit" in df.columns
            and (df["Profit"].abs() > self.abnormal_profit_threshold).any()
        ):
            abnormal_profit_trades = df[
                df["Profit"].abs() > self.abnormal_profit_threshold
            ]
            anomalies["abnormal_profits"].append(
                f"Valeurs de profit/perte anormalement élevées détectées (>{self.abnormal_profit_threshold}). Ex: {abnormal_profit_trades[['Order', 'Symbol', 'Profit']].head(2).to_dict('records')}"
            )

        # Log des anomalies détectées
        for anomaly_type, issues in anomalies.items():
            if issues:
                self.logger.warning(
                    f"MT5: Anomalies détectées - '{anomaly_type.replace('_', ' ')}': {issues}"
                )  # Utilise self.logger
            else:
                self.logger.info(
                    f"MT5: Aucune anomalie de type '{anomaly_type.replace('_', ' ')}' détectée."
                )  # Utilise self.logger
        return anomalies

    def auto_fix_logs(
        self, df: Optional[pd.DataFrame] = None, file_path: Optional[str] = None
    ) -> pd.DataFrame:
        """
        Applique des corrections automatiques aux anomalies détectées dans un DataFrame de logs de trades.
        Cette méthode charge un DataFrame si non fourni, puis applique des corrections
        pour standardiser les types d'ordre invalides, corriger les volumes/prix non positifs,
        et supprimer les doublons. Les valeurs de correction sont lues dynamiquement.

        Args:
            df (pd.DataFrame, optional): Le DataFrame à corriger. Si `None`, la fonction tentera
                                        de charger les logs depuis `file_path`.
            file_path (str, optional): Chemin du fichier de log à charger et corriger.
                                    Ignoré si `df` est fourni.

        Returns:
            pd.DataFrame: Le DataFrame corrigé et standardisé.
                        Retourne un DataFrame vide si aucune donnée n'est disponible ou chargée.
        """
        if df is None:
            if file_path:
                df = self.load_logs(file_path)
            else:
                self.logger.error(
                    "MT5: Aucun DataFrame ni chemin de fichier fourni pour auto_fix_logs. Opération avortée."
                )  # Utilise self.logger
                return pd.DataFrame()

        if df.empty:
            self.logger.info(
                "MT5: Le DataFrame est vide, saut de l'auto-correction."
            )  # Utilise self.logger
            return df

        self.logger.info(
            "MT5: Application des corrections automatiques aux logs..."
        )  # Utilise self.logger

        # NOTE: initial_anomalies est collecté mais non utilisé directement ici pour la logique de correction.
        # Il peut être utilisé pour un rapport d'audit plus détaillé.
        # initial_anomalies = self.detect_anomalies(df)

        # S'assurer que `audit_log_messages` est initialisé pour cette exécution
        if not hasattr(self, "audit_log_messages"):
            self.audit_log_messages = []
        self.audit_log_messages.append("\n--- Processus d'Auto-Correction ---\n")

        # Re-nettoyer pour s'assurer que les types de base sont corrects avant les corrections spécifiques
        df = self._clean_dataframe(df.copy())

        # Fix 3: Remplir les valeurs manquantes dans les colonnes numériques non critiques avec 0.0
        numeric_cols_to_fill = [
            "StopLoss",
            "TakeProfit",
            "Commission",
            "Swap",
            "Profit",
        ]
        for col in numeric_cols_to_fill:
            if col in df.columns and df[col].isnull().any():
                df[col] = df[col].fillna(0.0)
                self.audit_log_messages.append(
                    f"- Valeurs manquantes remplies dans '{col}' avec 0.0."
                )

        # Fix 4: Standardiser les types d'ordre invalides vers une valeur par défaut configurable
        # La valeur `self.default_invalid_order_type` est chargée dynamiquement dans `__init__`.
        if "Type" in df.columns:
            # self.DEFAULT_ALLOWED_ORDER_TYPES est une constante de classe (utilisée ici)
            invalid_type_rows = ~df["Type"].isin(self.DEFAULT_ALLOWED_ORDER_TYPES)
            if invalid_type_rows.any():
                invalid_types_found = (
                    df.loc[invalid_type_rows, "Type"].unique().tolist()
                )
                df.loc[invalid_type_rows, "Type"] = (
                    self.default_invalid_order_type
                )  # Utilisation de la valeur dynamique
                self.audit_log_messages.append(
                    f"- Types d'ordre invalides {invalid_types_found} standardisés à '{self.default_invalid_order_type}'."
                )

        # Fix 5: Corriger les volumes/prix hors plage vers des valeurs par défaut configurables
        # Les valeurs `self.min_fixed_volume` et `self.min_fixed_price` sont chargées dynamiquement dans `__init__`.
        if "Volume" in df.columns and (df["Volume"] <= 0).any():
            rows_fixed = df[df["Volume"] <= 0].shape[0]
            df.loc[df["Volume"] <= 0, "Volume"] = (
                self.min_fixed_volume
            )  # Utilisation de la valeur dynamique
            self.audit_log_messages.append(
                f"- Corrigé {rows_fixed} volumes non positifs à {self.min_fixed_volume}."
            )

        if "Price" in df.columns and (df["Price"] <= 0).any():
            rows_fixed = df[df["Price"] <= 0].shape[0]
            df.loc[df["Price"] <= 0, "Price"] = (
                self.min_fixed_price
            )  # Utilisation de la valeur dynamique
            self.audit_log_messages.append(
                f"- Corrigé {rows_fixed} prix non positifs à {self.min_fixed_price}."
            )

        # Fix 6: Supprimer les ordres dupliqués (garder la première occurrence)
        if "Order" in df.columns and "Time" in df.columns:
            initial_len = len(df)
            df.drop_duplicates(subset=["Order", "Time"], keep="first", inplace=True)
            if len(df) < initial_len:
                self.audit_log_messages.append(
                    f"- Supprimé {initial_len - len(df)} entrées d'ordres dupliquées."
                )

        # Re-trier et réinitialiser l'index après les corrections
        df.sort_values(by="Time", inplace=True)
        df.reset_index(drop=True, inplace=True)
        self.logger.info(
            "MT5: Corrections automatiques appliquées."
        )  # Utilise self.logger
        return df

    def export_logs(self, df: pd.DataFrame, output_path: str) -> None:
        """
        Exporte un DataFrame de logs (généralement corrigé) vers un nouveau fichier CSV.
        Le séparateur de sortie peut être dynamisé via ConfigManager.

        Args:
            df (pd.DataFrame): Le DataFrame à exporter.
            output_path (str): Le chemin complet du fichier de destination (incluant le nom du fichier et l'extension).
        """
        try:
            output_dir = Path(output_path).parent
            output_dir.mkdir(
                parents=True, exist_ok=True
            )  # S'assurer que le répertoire de sortie existe

            # Le séparateur de sortie (',') peut être dynamisé via ConfigManager (TODO implémenté)
            output_separator = self.config_manager.get(
                "mt5_connector_settings.csv_output_separator", ","
            )

            df.to_csv(
                output_path, index=False, encoding="utf-8", sep=output_separator
            )  # Utilise le séparateur dynamique
            self.logger.info(
                f"MT5: Logs corrigés exportés vers '{output_path}'."
            )  # Utilise self.logger
        except Exception as e:
            self.logger.error(
                f"MT5: Échec de l'exportation des logs vers '{output_path}': {e}",
                exc_info=True,
            )  # Utilise self.logger

    def repair_and_audit_single_file(self, input_path: str, output_path: str) -> None:
        """
        Charge un seul fichier de log de trades, applique les corrections automatiques,
        génère un rapport d'audit détaillé des modifications, puis exporte le fichier corrigé
        et le rapport d'audit.

        Args:
            input_path (str): Chemin complet du fichier de log d'entrée à réparer.
            output_path (str): Chemin complet pour exporter le fichier de log corrigé.
                            Le rapport d'audit sera exporté à côté (ex: `output_path_audit.log`).
        """
        self.logger.info(
            f"MT5: Démarrage du processus de réparation pour '{input_path}'."
        )  # Utilise self.logger
        # S'assurer que `audit_log_messages` est initialisé pour cette exécution
        if not hasattr(self, "audit_log_messages"):
            self.audit_log_messages = []
        self.audit_log_messages = (
            []
        )  # Réinitialiser le journal d'audit pour cette exécution spécifique

        df = self.load_logs(input_path)
        if df.empty:
            self.logger.error(
                f"MT5: Échec du chargement des données depuis '{input_path}'. Processus de réparation avorté."
            )  # Utilise self.logger
            self.audit_log_messages.append(
                f"ERROR: Échec du chargement des données depuis '{input_path}'."
            )
            # Envoyer une alerte si le chargement des logs est critique pour le bot
            self.config_manager.send_alert(
                "CRITIQUE",
                f"MT5 Log Repair Échec: Impossible de charger '{input_path}'.",
                alert_type="telegram_critical",
            )
            return

        self.audit_log_messages.append(f"--- Rapport d'Audit pour {input_path} ---")
        self.audit_log_messages.append(
            f"Début du traitement à {datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S UTC')}"
        )  # Utilise UTC
        self.audit_log_messages.append(f"Nombre initial de lignes: {len(df)}")

        initial_anomalies = self.detect_anomalies(df)
        self.audit_log_messages.append(
            "\n--- Anomalies Initiales Détectées Avant Auto-Correction ---"
        )
        if any(initial_anomalies.values()):
            for k, v in initial_anomalies.items():
                if v:
                    self.audit_log_messages.append(f"- {k}: {v}")
        else:
            self.audit_log_messages.append(
                "- Aucune anomalie majeure détectée initialement."
            )
        self.audit_log_messages.append(
            "---------------------------------------------------\n"
        )

        df_repaired = self.auto_fix_logs(df)

        final_anomalies = self.detect_anomalies(df_repaired)
        self.audit_log_messages.append(
            "\n--- Anomalies Finales Après Auto-Correction ---"
        )
        if any(final_anomalies.values()):
            for k, v in final_anomalies.items():
                if v:
                    self.audit_log_messages.append(f"- {k}: {v}")
        else:
            self.audit_log_messages.append(
                "- Toutes les anomalies détectées ont été corrigées ou aucune nouvelle anomalie introduite."
            )
        self.audit_log_messages.append("---------------------------------------\n")

        self.audit_log_messages.append(
            f"Nombre final de lignes après réparation: {len(df_repaired)}"
        )
        self.audit_log_messages.append(
            f"Fin du traitement à {datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S UTC')}"
        )  # Utilise UTC

        self.export_logs(df_repaired, output_path)  # Exporte le DataFrame corrigé

        # Le nom du fichier d'audit est dérivé du chemin de sortie
        audit_file_name_suffix = self.config_manager.get(
            "mt5_connector_settings.audit_report_file_suffix", "_audit_report.log"
        )
        audit_file_path = (
            Path(output_path)
            .with_suffix("")
            .with_name(f"{Path(output_path).stem}{audit_file_name_suffix}")
        )

        try:
            # Assurer l'écriture atomique pour le rapport d'audit
            temp_audit_file_path = audit_file_path.with_suffix(".tmp")
            with open(temp_audit_file_path, "w", encoding="utf-8") as f:
                f.write("\n".join(self.audit_log_messages))
            temp_audit_file_path.rename(audit_file_path)  # Rendre atomique

            self.logger.info(
                f"MT5: Journal d'audit exporté vers '{audit_file_path}'."
            )  # Utilise self.logger
        except Exception as e:
            self.logger.error(
                f"MT5: Échec de l'exportation du journal d'audit vers '{audit_file_path}': {e}",
                exc_info=True,
            )  # Utilise self.logger
            if temp_audit_file_path.exists():
                temp_audit_file_path.unlink()  # Nettoyage en cas d'erreur
            self.config_manager.send_alert(
                "CRITIQUE",
                f"MT5 Log Audit Échec: Impossible d'exporter le rapport d'audit pour '{input_path}'.",
                alert_type="telegram_critical",
            )

    def validate_logs(self, file_path: Optional[str] = None) -> bool:
        """
        Valide l'intégrité et la qualité des logs de trades après le nettoyage.
        Cette fonction est une vérification finale cruciale pour la qualité des données.

        Args:
            file_path (str, optional): Chemin d'un fichier de log spécifique à valider.
                                       Si `None`, charge et valide tous les fichiers CSV (ou configurés)
                                       du répertoire de logs par défaut.

        Returns:
            bool: True si les logs passent toutes les vérifications de validation et ne contiennent pas d'anomalies critiques,
                  False sinon.
        """
        self.logger.info(
            f"MT5: Validation des logs depuis {file_path if file_path else 'tous les fichiers dans le répertoire de logs'}..."
        )  # Utilise self.logger
        df = self.load_logs(file_path)
        if df.empty:
            self.logger.error(
                "MT5: Aucune donnée à valider. Validation échouée."
            )  # Utilise self.logger
            # Ne pas envoyer d'alerte critique ici, load_logs gère déjà les erreurs de chargement.
            return False

        anomalies = self.detect_anomalies(df)

        # La validation réussit si aucune anomalie n'est détectée dans les catégories critiques
        # Les catégories critiques peuvent être configurables.
        critical_anomaly_types = self.config_manager.get(
            "mt5_connector_settings.critical_anomaly_types",
            [
                "missing_critical_columns",
                "incorrect_data_types",
                "missing_values",
                "invalid_order_types",
            ],
        )

        is_valid = True
        for anomaly_type in critical_anomaly_types:
            if anomalies.get(
                anomaly_type
            ):  # Si des problèmes sont trouvés dans une catégorie critique
                is_valid = False
                self.logger.error(
                    f"MT5: Validation échouée : Anomalies critiques de type '{anomaly_type}' détectées. Problèmes: {anomalies[anomaly_type]}."
                )
                self.config_manager.send_alert(
                    "ERREUR_VALIDATION_LOG",
                    f"MT5: Anomalies critiques dans logs: {anomaly_type}. '{file_path or 'all_logs'}'",
                    alert_type="telegram_critical",
                )

        if (
            is_valid
        ):  # Si pas d'anomalies critiques, on peut aussi checker les non-critiques
            if any(anomalies.values()):  # Si des anomalies non critiques sont présentes
                self.logger.warning(
                    "MT5: Les logs ont des anomalies non critiques. La validation est techniquement 'True', mais l'attention est requise."
                )
                for anomaly_type, issues in anomalies.items():
                    if (
                        issues and anomaly_type not in critical_anomaly_types
                    ):  # Log les non-critiques
                        self.logger.warning(f"- {anomaly_type}: {issues}")
            else:
                self.logger.info(
                    "MT5: Les logs ont passé toutes les vérifications de validation. Les données sont propres et cohérentes."
                )

        # Vérifications supplémentaires d'intégrité pour une perspective institutionnelle
        if "Time" in df.columns and not df["Time"].is_monotonic_increasing:
            self.logger.error(
                "MT5: Validation échouée : Les logs ne sont pas dans l'ordre chronologique (colonne 'Time')."
            )  # Utilise self.logger
            is_valid = False
            self.config_manager.send_alert(
                "ERREUR_VALIDATION_LOG",
                f"MT5: Logs désordonnés pour '{file_path or 'all_logs'}'",
                alert_type="telegram_critical",
            )
        else:
            self.logger.info(
                "MT5: Validation passée : Les logs sont dans l'ordre chronologique."
            )  # Utilise self.logger

        # Récupérer le seuil de profit anormal depuis la configuration
        abnormal_profit_threshold = (
            self.abnormal_profit_threshold
        )  # Utilise l'attribut de l'instance
        if "Profit" in df.columns:
            if (df["Profit"].abs() > abnormal_profit_threshold).any():
                self.logger.warning(
                    f"MT5: Avertissement de validation : Valeurs de profit/perte potentiellement anormales détectées (dépassent {abnormal_profit_threshold})."
                )  # Utilise self.logger
                # Cette anomalie est déjà détectée par detect_anomalies, mais le warning ici est un rappel.

        # TODO: Implémenter des vérifications de gaps dans les données chronologiques (trous de temps).
        # TODO: Implémenter des vérifications de données manquantes critiques si df est grand (sampling).

        return is_valid
