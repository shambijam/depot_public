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
import MetaTrader5 as mt5
from collections import namedtuple
from datetime import datetime, timedelta, UTC

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

            self.TIMEFRAMES = {
                key: getattr(mt5, value)
                for key, value in self.mt5_mappings.get("timeframes", {}).items()
            }
            self.TIMEFRAME_M1 = self.TIMEFRAMES.get("M1", mt5.TIMEFRAME_M1)

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

    def get_spread_pips(self, symbol: str) -> float:
        """Retourne le spread en pips avec garde-fous."""
        info = self.mt5.symbol_info(symbol)
        tick = self.mt5.symbol_info_tick(symbol)

        point = (getattr(info, "point", 0.0) or 0.0) if info else 0.0
        bid = getattr(tick, "bid", 0.0) or 0.0
        ask = getattr(tick, "ask", 0.0) or 0.0

        # 1) calcul direct depuis le tick si possible
        if ask > 0.0 and bid > 0.0 and ask >= bid:
            spread_price = ask - bid
        else:
            # 2) fallback: spread en "points" du symbole
            raw_points = getattr(info, "spread", 0) or 0
            spread_price = raw_points * point

        pip = self._pip_size(symbol)
        sp = (spread_price / pip) if pip > 0 else float("inf")

        if not math.isfinite(sp) or sp < 0:
            sp = 1e9  # sentinelle très haute si data foireuse

        return sp

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

            # tick & prix
            tick = mt5.symbol_info_tick(position.symbol)
            if not tick:
                self.logger.error("Tick introuvable pour %s.", position.symbol)
                return False
            price = tick.bid if order_type == self.ORDER_TYPE_SELL else tick.ask

            req = {
                "action": self.TRADE_ACTION_DEAL,
                "symbol": position.symbol,
                "type": order_type,
                "position": position.ticket,  # très important pour clôture
                "volume": position.volume,
                "price": price,
                "deviation": 50,
                "magic": getattr(self, "magic", 0),
                "comment": "SNIPER_X close market",
                "type_filling": self.ORDER_FILLING_RETURN,
                "type_time": self.ORDER_TIME_GTC,
            }
            res = mt5.order_send(req)
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

    # --- AJOUT 2: spread en pips robuste (jamais 'inf') -------------------------

    def get_spread_pips(self, symbol: str) -> float:
        """
        Retourne le spread en *pips*:
        - utilise symbol_info.spread si dispo (>0)
        - sinon calcule (ask-bid)
        - jamais 'inf' (retourne un grand nombre si indisponible)
        """
        info = mt5.symbol_info(symbol)
        if not info:
            return 1e9
        # pip_size standard selon digits
        digits = info.digits or 5
        point = info.point or 1e-5
        pip_size = (
            0.0001
            if digits in (4, 5)
            else (0.01 if digits in (2, 3) else (point or 1e-5))
        )

        # 1) tenter via info.spread
        if (info.spread or 0) > 0:
            spread_price = info.spread * point
            return spread_price / pip_size

        # 2) fallback tick
        tick = mt5.symbol_info_tick(symbol)
        if tick and (tick.ask or 0) > 0 and (tick.bid or 0) > 0:
            spread_price = abs(tick.ask - tick.bid)
            if spread_price > 0:
                return spread_price / pip_size

        # 3) dernier recours: gros nombre pour forcer un "skip" propre
        return 1e9

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

        Args:
            symbol (str): Le nom du symbole.
            action (str): L'action de trading ("BUY" pour le prix Ask, "SELL" pour le prix Bid).

        Returns:
            Optional[float]: Le prix actuel (float), ou None si la récupération échoue.
        """
        if not self.is_connected:
            self.logger.warning(
                f"MT5: Non connecté. Impossible de récupérer le prix actuel pour '{symbol}'."
            )  # Utilise self.logger
            return None

        # Récupère les informations de tick les plus récentes pour le symbole
        symbol_info_tick = self.mt5.symbol_info_tick(symbol)  # Utilise self.mt5
        if symbol_info_tick is None:
            self.logger.error(
                f"MT5: Échec de la récupération des données de tick pour '{symbol}'. Erreur: {self.mt5.last_error()}."
            )  # Utilise self.logger, self.mt5
            return None

        # Déterminer le prix en fonction de l'action souhaitée (utilise les constantes mappées)
        if action.upper() == "BUY":  # Acheter au prix Ask (demande)
            return symbol_info_tick.ask
        elif action.upper() == "SELL":  # Vendre au prix Bid (offre)
            return symbol_info_tick.bid
        else:
            self.logger.warning(
                f"MT5: Action non reconnue '{action}' pour get_current_price. Retourne le prix Bid par défaut."
            )  # Utilise self.logger
            return symbol_info_tick.bid

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

    def get_symbol_info(self, symbol: str) -> Optional[Any]:
        """
        Récupère les informations d'un symbole (spread, point, visibilité, etc.)
        avec sélection automatique dans la Market Watch et logs détaillés.

        Args:
            symbol (str): Le symbole MT5 (ex: "EURUSD", "XAUUSD").

        Returns:
            Optional[Any]: MetaTrader5.SymbolInfo (NamedTuple) si OK, sinon None.
        """
        if not self.is_connected:
            self.logger.error(
                f"MT5: Non connecté. Impossible de récupérer les informations du symbole '{symbol}'."
            )
            return None

        symbol_norm = str(symbol).strip().upper()
        self.logger.debug(
            f"MT5: Tentative de récupération des informations pour le symbole '{symbol_norm}'..."
        )

        try:
            # S'assurer que le symbole est visible/actif dans la Market Watch
            try:
                selected_ok = self.mt5.symbol_select(symbol_norm, True)
                if not selected_ok:
                    self.logger.debug(
                        f"MT5: symbol_select('{symbol_norm}', True) a retourné False (le symbole est peut-être déjà visible ou non disponible)."
                    )
            except Exception as sel_e:
                self.logger.debug(
                    f"MT5: Exception lors de symbol_select('{symbol_norm}'): {sel_e}"
                )

            info = self.mt5.symbol_info(symbol_norm)

            if info is None:
                last_mt5_error = self.mt5.last_error()
                self.logger.error(
                    f"MT5: Échec de la récupération des informations du symbole '{symbol_norm}'. "
                    f"Dernière erreur MT5: {last_mt5_error}."
                )
                self.logger.debug(
                    f"MT5: symbol_info('{symbol_norm}') a retourné None. "
                    f"Vérifiez la visibilité dans la Market Watch et que le symbole existe chez le broker."
                )
                return None

            # Si récupéré mais marqué non visible, retente une sélection
            if getattr(info, "visible", True) is False:
                if self.mt5.symbol_select(symbol_norm, True):
                    # Rafraîchir l'info après sélection
                    info_refreshed = self.mt5.symbol_info(symbol_norm)
                    if info_refreshed is not None:
                        info = info_refreshed

            # Calcule un point "fallback" pour le log si MT5 renvoie point=0
            computed_point = None
            try:
                if (getattr(info, "point", None) in (None, 0)) and hasattr(
                    info, "digits"
                ):
                    computed_point = 10 ** (-int(info.digits))
            except Exception:
                computed_point = None

            # Logs lisibles + debug complet
            point_for_log = (
                computed_point
                if computed_point is not None
                else getattr(info, "point", "N/A")
            )
            self.logger.info(
                f"MT5: Informations du symbole '{symbol_norm}' récupérées. "
                f"Spread: {getattr(info, 'spread', 'N/A')}, Point: {point_for_log}."
            )
            try:
                self.logger.debug(
                    f"MT5: Détails complets du symbole '{symbol_norm}': {info._asdict()}"
                )
            except Exception:
                # Certains environnements peuvent ne pas supporter _asdict()
                pass

            return info

        except Exception as e:
            self.logger.error(
                f"Exception dans get_symbol_info pour '{symbol_norm}': {e}. "
                f"Dernière erreur MT5: {self.mt5.last_error()}.",
                exc_info=True,
            )
            self.logger.debug(
                f"MT5: Une exception a interrompu la récupération des infos symbole pour '{symbol_norm}'. "
                f"Vérifiez le terminal MT5 et la disponibilité du symbole."
            )
            return None

    def get_symbol_info_tick(self, symbol: str):
        """
        Retourne le dernier tick du symbole depuis MetaTrader 5.
        """
        try:
            import MetaTrader5 as mt5

            tick = mt5.symbol_info_tick(symbol)
            if tick is None:
                self.logger.error(
                    f"[MT5C] Impossible de récupérer le tick pour {symbol}."
                )
            return tick
        except Exception as e:
            self.logger.error(f"[MT5C] Erreur get_symbol_info_tick pour {symbol}: {e}")
            return None

    def get_symbol_spread_points(self, symbol: str) -> float:
        """
        Retourne le spread courant en *points* pour `symbol`.

        Stratégie (dans cet ordre) :
        1) spread natif MT5 (info.spread) s'il est > 0  → déjà en points
        2) (ask - bid) / point via symbol_info_tick
        3) fallback (ask/bid) depuis symbol_info
        4) Depth of Market (market_book_get) pour reconstruire ask/bid
        Choix du `point` : min positif parmi (info.point, info.trade_tick_size, 10**(-digits))
        Renvoie 0.0 si non calculable (mais jamais inf/NaN).
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
                    return native
            except Exception:
                pass

            # === Prépare les candidats pour 'point'
            point_candidates = []
            if info is not None:
                try:
                    p = float(getattr(info, "point", 0.0) or 0.0)
                    if p > 0:
                        point_candidates.append(p)
                except Exception:
                    pass
                try:
                    tts = float(getattr(info, "trade_tick_size", 0.0) or 0.0)
                    if tts > 0:
                        point_candidates.append(tts)
                except Exception:
                    pass
                try:
                    digits = int(getattr(info, "digits", 0) or 0)
                    if digits > 0:
                        point_candidates.append(10 ** (-digits))
                except Exception:
                    pass

            point = min(point_candidates) if point_candidates else 0.0

            # === Récup ask/bid (tick en priorité)
            ask = bid = None
            try:
                tick = self.mt5.symbol_info_tick(sym)
                if tick:
                    a = getattr(tick, "ask", None)
                    b = getattr(tick, "bid", None)
                    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
                        ask, bid = float(a), float(b)
            except Exception:
                pass

            # Fallback sur info.{ask,bid} si tick invalide
            if (ask is None or ask <= 0) or (bid is None or bid <= 0) or (ask <= bid):
                try:
                    a = float(getattr(info, "ask", 0) or 0.0) if info else 0.0
                    b = float(getattr(info, "bid", 0) or 0.0) if info else 0.0
                    if a > 0 and b > 0 and a > b:
                        ask, bid = a, b
                except Exception:
                    pass

            # Dernier recours : Depth of Market
            if (ask is None or bid is None or ask <= bid) or point <= 0:
                try:
                    book = self.mt5.market_book_get(sym)
                    if book:
                        # type BUY = bids, SELL = asks
                        best_ask = None
                        best_bid = None
                        for x in book:
                            t = getattr(x, "type", None)
                            price = getattr(x, "price", None)
                            if not isinstance(price, (int, float)):
                                continue
                            if t == self.mt5.BOOK_TYPE_SELL:
                                best_ask = (
                                    price
                                    if (best_ask is None or price < best_ask)
                                    else best_ask
                                )
                            elif t == self.mt5.BOOK_TYPE_BUY:
                                best_bid = (
                                    price
                                    if (best_bid is None or price > best_bid)
                                    else best_bid
                                )
                        if best_ask and best_bid and best_ask > best_bid:
                            ask, bid = float(best_ask), float(best_bid)
                except Exception:
                    pass

            # Calcul final si possible
            if (
                isinstance(ask, (int, float))
                and isinstance(bid, (int, float))
                and ask > bid
                and point > 0
            ):
                spread_pts = (ask - bid) / point
                if spread_pts < 0:
                    spread_pts = abs(spread_pts)  # sécurité flottants
                # en points, garder 2 décimales max (FX typiquement entier)
                return float(round(spread_pts, 2))

        except Exception as e:
            self.logger.debug(
                f"[get_symbol_spread_points] erreur pour {symbol}: {e}", exc_info=True
            )

        # Jamais inf/NaN
        return 0.0

    def order_send(self, request: Dict[str, Any]) -> Optional[Any]:
        """
        Envoie un ordre de trading (achat, vente, modification, clôture) au terminal MetaTrader 5.
        Utilise les constantes MT5 mappées du ConfigManager et assure une journalisation détaillée
        et des alertes en cas d'erreur.

        Args:
            request (Dict[str, Any]): Le dictionnaire de la requête d'ordre MT5, conforme à la structure
                                    attendue par `mt5.order_send()`.

        Returns:
            Optional[Any]: L'objet `MetaTrader5.TradeResult` (NamedTuple) si l'ordre est envoyé et une réponse est reçue,
                        `None` si l'envoi échoue ou si le connecteur n'est pas connecté.
        """
        # Vérifier la connexion
        if not self.is_connected:
            self.logger.error("MT5: Non connecté. Impossible d'envoyer l'ordre.")
            self.config_manager.send_alert(
                "MT5 Non Connecté: Échec Envoi Ordre.", "telegram_critical"
            )
            return None

        # Vérification des clés essentielles dans la requête
        required_keys = [
            "action",
            "symbol",
            "volume",
            "type",
            "price",
            "magic",
            "comment",
        ]
        if not all(key in request for key in required_keys):
            missing_keys = sorted(set(required_keys) - set(request.keys()))
            self.logger.error(
                f"MT5: Requête d'ordre invalide. Clés manquantes: {missing_keys}. Requête: {request}"
            )
            self.config_manager.send_alert(
                f"MT5: Requête d'ordre invalide. Clés manquantes: {missing_keys}.",
                "telegram_critical",
            )
            return None

        # Déterminer l'action pour le log
        action_type_numeric = request.get("type")
        if action_type_numeric == self.ORDER_TYPE_BUY:
            action_str = "ACHAT"
        elif action_type_numeric == self.ORDER_TYPE_SELL:
            action_str = "VENTE"
        elif action_type_numeric == getattr(
            self.mt5,
            self.mt5_mappings.get("order_types", {}).get(
                "BUY_LIMIT", "ORDER_TYPE_BUY_LIMIT"
            ),
            None,
        ):
            action_str = "BUY_LIMIT"
        elif action_type_numeric == getattr(
            self.mt5,
            self.mt5_mappings.get("order_types", {}).get(
                "SELL_LIMIT", "ORDER_TYPE_SELL_LIMIT"
            ),
            None,
        ):
            action_str = "SELL_LIMIT"
        else:
            action_str = f"TypeOrdre_{action_type_numeric}"

        self.logger.info(
            f"MT5: Envoi de l'ordre: {action_str} {request.get('volume')} {request.get('symbol')} "
            f"@ {request.get('price')} (ID Interne: {request.get('order_id', 'N/A')})..."
        )

        # Envoyer l'ordre
        try:
            result = self.mt5.order_send(request)
        except Exception as ex:
            self.logger.exception(f"MT5: Exception lors de order_send(): {ex}")
            self.config_manager.send_alert(
                f"MT5: Exception order_send(): {ex}", "telegram_critical"
            )
            return None

        # Gérer la réponse de l'API
        if result is not None:
            self.logger.info(
                f"MT5: Réponse API. Retcode: {getattr(result, 'retcode', 'N/A')}, "
                f"Commentaire: {getattr(result, 'comment', 'N/A')}, "
                f"Deal: {getattr(result, 'deal', 'N/A')}, Ordre: {getattr(result, 'order', 'N/A')}"
            )

            if getattr(result, "retcode", None) == self.TRADE_RETCODE_DONE:
                self.logger.info(
                    f"MT5: Ordre exécuté avec succès ! "
                    f"Deal #{getattr(result, 'deal', 'N/A')}, "
                    f"Ordre #{getattr(result, 'order', 'N/A')} pour {request.get('symbol')}."
                )
            else:
                retcode_val = getattr(result, "retcode", None)
                retcode_str = ""
                for k, v in self.mt5_mappings.get("trade_retcodes", {}).items():
                    if getattr(self.mt5, v, None) == retcode_val:
                        retcode_str = k
                        break
                self.logger.warning(
                    f"MT5: Ordre non exécuté. Retcode: {retcode_val} ({retcode_str}), "
                    f"Commentaire: {getattr(result, 'comment', 'N/A')}. "
                    f"Erreur système: {self.mt5.last_error()}."
                )
                self.config_manager.send_alert(
                    f"MT5: Ordre non exécuté ({retcode_str or retcode_val}). "
                    f"Commentaire: {getattr(result, 'comment', 'N/A')}",
                    "telegram_critical",
                )
            return result

        # Aucun résultat
        self.logger.error(
            f"MT5: order_send a échoué. Aucune réponse. Erreur système: {self.mt5.last_error()}."
        )
        self.config_manager.send_alert(
            f"MT5: Échec envoi ordre: Aucune réponse. {self.mt5.last_error()}",
            "telegram_critical",
        )
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
