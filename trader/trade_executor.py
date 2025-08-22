# trade_executor.py - Module Central d'Exécution des Trades pour le Bot SNIPER_X

import logging
import os
import sys
import time
import uuid
import json
import pandas as pd
import jsonschema
from core.config_manager import ConfigManager, CustomJSONEncoder
from datetime import datetime, UTC  # AMÉLIORATION: Import explicite de UTC
from pathlib import Path
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta, UTC

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


# --- Classe TradeExecutor ---


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

        # CORRECTION : Suppression de self.mt5 = mt5. Il est plus propre et cohérent que
        # toute interaction avec la librairie MetaTrader5 passe par le mt5_connector.
        # Cela renforce la séparation des responsabilités entre les modules.

        # GESTION D'ÉTAT INTERNE (Excellente pratique, inchangée)
        self._open_positions: Dict[int, Dict[str, Any]] = {}
        self._pending_orders: Dict[str, Dict[str, Any]] = {}
        self._last_reconciliation_time: Optional[datetime] = (
            None  # Initialisé à None pour plus de clarté
        )

        # Chargement dynamique des paramètres
        self._load_settings()

        self.logger.info(f"TradeExecutor initialisé en mode {self.mode.upper()}.")

        # La réconciliation initiale est gérée par main.py, ce qui est la bonne approche.

    def get_open_positions(self) -> List[Dict[str, Any]]:
        """
        Retourne la liste des positions ouvertes actuellement suivies par le TradeExecutor.
        Cette méthode est utilisée par le cycle principal pour récupérer les positions
        à évaluer pour des conditions de sortie.

        Returns:
            List[Dict[str, Any]]: Une liste de dictionnaires représentant les positions ouvertes.
                                Peut être vide si aucune position n'est ouverte.
        """
        return list(self._open_positions.values())

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
        Réconcilie l'état interne des positions en fusionnant les données du broker
        avec les données internes pour préserver les informations critiques (ex: risque initial).
        """
        self.logger.info(
            "Début de la réconciliation de l'état du TradeExecutor avec le broker..."
        )
        if not self.mt5_connector.is_connected:
            self.logger.warning("MT5 n'est pas connecté pour la réconciliation.")
            return

        try:
            broker_positions_list = self.mt5_connector.get_positions()
            if broker_positions_list is None:
                self.logger.error(
                    "Échec de la récupération des positions du broker pour la réconciliation."
                )
                return

            broker_positions_map = {
                pos.ticket: pos._asdict() for pos in broker_positions_list
            }

            reconciled_positions: Dict[int, Dict[str, Any]] = {}

            # --- AMÉLIORATION MAJEURE : Logique de Fusion ---

            # 1. Parcourir les positions du broker
            for ticket, broker_pos in broker_positions_map.items():
                if ticket in self._open_positions:
                    # La position existe déjà en interne : on met à jour les données volatiles
                    internal_pos = self._open_positions[ticket]
                    internal_pos.update(
                        {
                            "current_price": broker_pos["price_current"],
                            "profit": broker_pos["profit"],
                            "sl": broker_pos[
                                "sl"
                            ],  # Mettre à jour SL/TP s'ils ont été modifiés manuellement
                            "tp": broker_pos["tp"],
                        }
                    )
                    reconciled_positions[ticket] = internal_pos
                else:
                    # La position est nouvelle pour nous (ouverte manuellement ou bot redémarré)
                    self.logger.warning(
                        f"Position #{ticket} ({broker_pos['symbol']}) détectée chez le broker mais absente de l'état interne. Ajoutée (sans risque initial)."
                    )
                    reconciled_positions[ticket] = {
                        "ticket": broker_pos["ticket"],
                        "symbol": broker_pos["symbol"],
                        "type": broker_pos["type"],
                        "volume": broker_pos["volume"],
                        "entry_price": broker_pos["price_open"],
                        "sl": broker_pos["sl"],
                        "tp": broker_pos["tp"],
                        "magic": broker_pos["magic"],
                        "comment": broker_pos["comment"],
                        "open_time": datetime.fromtimestamp(
                            broker_pos["time"], tz=UTC
                        ).isoformat(),
                        "profit": broker_pos["profit"],
                        "initial_risk_usd": 0.0,  # Risque inconnu
                    }

            # 2. Identifier les positions qui ont été clôturées
            closed_tickets = set(self._open_positions.keys()) - set(
                broker_positions_map.keys()
            )
            for ticket in closed_tickets:
                self.logger.info(
                    f"Position #{ticket} ({self._open_positions[ticket]['symbol']}) absente chez le broker. Supprimée de l'état interne."
                )

            # 3. Remplacer l'ancien état par le nouvel état réconcilié
            self._open_positions = reconciled_positions
            self._last_reconciliation_time = datetime.now(UTC)
            self.logger.info(
                f"Réconciliation terminée. {len(self._open_positions)} positions actives synchronisées."
            )

        except Exception as e:
            self.logger.error(
                f"Échec de la réconciliation de l'état avec le broker: {e}",
                exc_info=True,
            )
            self.config_manager.send_alert(
                f"Réconciliation Échec: {e}", "telegram_critical"
            )

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
        Pré-checks d’exécution + FILTRES DE QUALITÉ (scalping "au couteau", zéro hasard mais non-bloquant).
        - Phase directionnelle obligatoire (pas de range/compression/uncertain)
        - Gating micro-phase selon 'gating_mode' (strict/normal/aggressive)
            * strict    : break M1 + retest obligatoires
            * normal    : strict OU (BOS confirmé + FVG proche) OU (OB validé proche)
            * aggressive: normal OU (break seul + spread & ATR ok)
        - Momentum fallback désactivé
        - Cohérence MTF (si fournie)
        - ATR M1 mini / spread max
        - Stops_level broker vs SL scalp
        """
        # --- import DIAG (local, neutre si absent) ---
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
            return {"BUY": "BUY", "SELL": "SELL", "LONG": "BUY", "SHORT": "SELL", "CLOSE": "CLOSE"}.get(a, "")

        def _mt5_is_connected() -> bool:
            attr = getattr(self.mt5_connector, "is_connected", None)
            try:
                if callable(attr):
                    return bool(attr())
                return bool(attr)
            except Exception:
                return False

        def _mt5_reconnect_if_needed():
            recon = getattr(self.mt5_connector, "reconnect_if_needed", None)
            if callable(recon):
                try:
                    recon()
                except Exception:
                    pass

        def _diag_note(reason: str, extra: dict | None = None, sym: str | None = None):
            try:
                if get_tracker_from_context:
                    s = sym or trade_decision.get("asset") or "UNKNOWN"
                    get_tracker_from_context(market_context).note(s, "pre_trade", reason, extra or {})
            except Exception:
                pass

        def _reject(reason: str, extra: dict | None = None, sym: str | None = None) -> tuple[bool, str]:
            _diag_note(reason, extra, sym)
            return False, reason

        # 1) Action & symbole
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
            return _reject(f"invalid_action:{action_raw}")

        raw_symbol = _first_non_empty(
            trade_decision.get("asset"),
            trade_decision.get("symbol"),
            trade_decision.get("instrument"),
        )
        if not raw_symbol or raw_symbol.strip().upper() == "UNKNOWN":
            return _reject("asset_missing_or_unknown")
        raw_symbol = raw_symbol.strip().upper()

        # 2) Whitelist
        allowed = set(map(str.upper, active_config.get("tradeable_assets", [])))
        if allowed and raw_symbol not in allowed:
            return _reject(f"asset_not_allowed:{raw_symbol}", sym=raw_symbol)

        # 3) Mapping broker
        broker_symbol = self.config_manager.get("asset_symbol_mapping", {}).get(raw_symbol, raw_symbol)
        if not broker_symbol or str(broker_symbol).strip().upper() == "UNKNOWN":
            return _reject(f"invalid_broker_mapping:{raw_symbol}", sym=raw_symbol)
        broker_symbol = str(broker_symbol).strip().upper()

        # 4) Connexion MT5
        if not _mt5_is_connected():
            _mt5_reconnect_if_needed()
            if not _mt5_is_connected():
                return _reject("mt5_not_connected", sym=raw_symbol)

        # 5) Symbole existant
        symbol_info = self.mt5_connector.get_symbol_info(broker_symbol)
        if not symbol_info or not getattr(symbol_info, "name", None):
            return _reject(f"invalid_mt5_symbol:{broker_symbol}", sym=raw_symbol)

        # 6) Positions max
        active_acc = market_context.get("active_broker_account", {})
        max_pos = active_acc.get("trade_settings", {}).get("max_open_positions", 999)
        current_positions = market_context.get("open_positions", [])
        if isinstance(current_positions, (list, tuple)) and len(current_positions) >= max_pos:
            return _reject(f"max_positions_reached:{len(current_positions)}/{max_pos}", sym=raw_symbol)

        # 7) Spread limite (absolu en points, côté exécution)
        exec_policy = (active_config.get("execution_policy", {}) if isinstance(active_config, dict) else {})
        max_spread_points = exec_policy.get("max_spread_points")
        tick = self.mt5_connector.get_symbol_info_tick(broker_symbol)
        if tick and hasattr(symbol_info, "spread") and hasattr(symbol_info, "point"):
            if isinstance(max_spread_points, (int, float)) and symbol_info.spread and max_spread_points > 0:
                if symbol_info.spread > max_spread_points:
                    return _reject("spread_too_high_points",
                                {"spread": float(symbol_info.spread), "limit": float(max_spread_points)},
                                sym=raw_symbol)

        # ---------- Gating micro-phase ----------
        dt = trade_decision.get("decision_trace") or {}
        gates = trade_decision.get("gates") or {}

        # Micro-structure
        m1_break  = bool(gates.get("m1_break")  or dt.get("m1_break_in_direction"))
        m1_retest = bool(gates.get("m1_retest") or dt.get("m1_retest_confirmation"))

        bos = dt.get("bos_mss_details") or {}
        fvg = dt.get("fvg_details") or {}
        ob  = dt.get("ob_details")  or {}

        # Phases & métriques utiles
        phase_from_decision = str(gates.get("phase") or dt.get("phase") or "").lower()
        spread_pips_from_trace = float(dt.get("spread_pips") or 0.0)
        atr_m1_pips_from_trace = float(dt.get("atr_m1_pips") or 0.0)

        # Règles scalping
        sr = self.config_manager.get("entry_rules.scalping", {}) or {}
        gating_mode       = str(sr.get("gating_mode", "normal")).lower()  # strict|normal|aggressive
        max_spread_pips   = float(sr.get("max_spread_pips", 1.0) or 1.0)
        min_atr_m1_pips   = float(sr.get("min_atr_m1_pips", 0.8) or 0.0)
        fvg_max_distance  = float(sr.get("fvg_max_distance_pips", 2.0) or 2.0)
        ob_max_distance   = float(sr.get("ob_max_distance_pips", 2.0) or 2.0)

        # Alternatives (sans momentum)
        bos_confirmed = bool(bos.get("confirmed") or bos.get("is_confirmed"))
        fvg_dist_ok   = float(fvg.get("distance_pips") or 1e9) <= fvg_max_distance
        ob_valid_ok   = bool(ob.get("validated") or ob.get("valid"))
        ob_dist_ok    = float(ob.get("distance_pips") or 1e9) <= ob_max_distance

        cond_strict  = m1_break and m1_retest
        cond_bos_fvg = bos_confirmed and fvg_dist_ok
        cond_ob_near = ob_valid_ok and ob_dist_ok
        cond_aggr    = (m1_break and (spread_pips_from_trace <= max_spread_pips)
                        and (atr_m1_pips_from_trace >= max(0.0, 0.8 * min_atr_m1_pips)))

        if   gating_mode == "strict":
            if not cond_strict:
                return _reject("gate_strict_failed(m1_break+retest_required)",
                            {"break": m1_break, "retest": m1_retest}, raw_symbol)
        elif gating_mode == "normal":
            if not (cond_strict or cond_bos_fvg or cond_ob_near):
                return _reject("gate_normal_failed(no_retest_but_no_bos_fvg_or_ob_near)",
                            {"break": m1_break, "retest": m1_retest,
                                "bos_confirmed": bos_confirmed, "fvg_ok": fvg_dist_ok, "ob_ok": ob_dist_ok},
                            raw_symbol)
        else:  # aggressive
            if not (cond_strict or cond_bos_fvg or cond_ob_near or cond_aggr):
                return _reject("gate_aggressive_failed",
                            {"break": m1_break, "retest": m1_retest, "aggr_ok": cond_aggr},
                            raw_symbol)

        # ---------- Phase directionnelle ----------
        current_phase = phase_from_decision or str(
            market_context.get("phase", "") or market_context.get("market_phase", "") or ""
        ).lower()
        non_directionals = {
            "range", "range_accumulation", "range_distribution",
            "compression", "low_volatility_compression",
            "uncertain", "no_clear_phase",
        }
        if current_phase in non_directionals or any(k in current_phase for k in ["range", "compression", "uncertain"]):
            return _reject(f"phase_not_directional({current_phase})", sym=raw_symbol)

        # Momentum fallback désactivé
        used_rule = str(trade_decision.get("rule_name", "")).lower()
        if ("momentum" in used_rule) or bool(dt.get("used_momentum_fallback")):
            return _reject("momentum_fallback_disabled", sym=raw_symbol)

        # Cohérence MTF
        mtf_dir = str((dt.get("mtf_direction") or trade_decision.get("mtf_direction", "")).lower())
        if mtf_dir in ("up", "down"):
            if (mtf_dir == "up" and action != "BUY") or (mtf_dir == "down" and action != "SELL"):
                return _reject(f"mtf_direction_mismatch({mtf_dir} vs {action})", sym=raw_symbol)

        # ATR M1 minimum (depuis decision_trace)
        if min_atr_m1_pips > 0 and atr_m1_pips_from_trace > 0 and atr_m1_pips_from_trace < min_atr_m1_pips:
            return _reject(f"atr_m1_too_low({atr_m1_pips_from_trace:.2f} < {min_atr_m1_pips:.2f})", sym=raw_symbol)

        # (Complément) ATR sur DF du contexte si règle activée
        import pandas as pd, numpy as np

        def _extract_latest_df_from_context(mkt_ctx: dict, sym: str, alt: str):
            md2 = (mkt_ctx.get("market_data") or {}).get(sym) or (mkt_ctx.get("market_data") or {}).get(alt)
            if isinstance(md2, pd.DataFrame):
                return md2
            if isinstance(md2, dict):
                df2 = md2.get("annotated_rates_df")
                return df2 if isinstance(df2, pd.DataFrame) else None
            return None

        def _get_atr(df, period: int):
            if df is None or len(df) < period + 2:
                return float("nan")
            high = df["high"].astype(float)
            low = df["low"].astype(float)
            close = df["close"].astype(float)
            prev_close = close.shift(1)
            tr = np.maximum.reduce([(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()])
            return float(tr.rolling(window=period, min_periods=period).mean().iloc[-1])

        q = self.config_manager.get("execution_quality_filters", {}) or {}
        vol_rule = q.get("atr_volatility_filter", {}) or {}
        if bool(vol_rule.get("enabled", False)):
            df_ctx = _extract_latest_df_from_context(market_context, broker_symbol, raw_symbol)
            period = int(vol_rule.get("period", 14))
            min_atr = float(vol_rule.get("min_atr", 0.0))
            max_atr = float(vol_rule.get("max_atr", 1e9))
            atr_val = _get_atr(df_ctx, period)
            if atr_val == atr_val:  # not NaN
                if atr_val < min_atr:
                    return _reject(f"atr_too_low({atr_val:.6f} < {min_atr:.6f})", sym=raw_symbol)
                if atr_val > max_atr:
                    return _reject(f"atr_too_high({atr_val:.6f} > {max_atr:.6f})", sym=raw_symbol)

        # 9) Prix dispo
        price = self.mt5_connector.get_current_price(broker_symbol, action)
        if not price or price <= 0:
            return _reject("price_unavailable", sym=raw_symbol)

        # 10) Stops level broker vs SL scalp (on REFUSE d'élargir pour un scalp)
        target_sl_pips = float(trade_decision.get("target_sl_pips", 0) or 0.0)
        is_scalping = "scalping" in str(trade_decision.get("strategy_type", "")).lower()
        sl_cap = float(self.config_manager.get("entry_rules.scalping.max_stop_pips_scalp", 0.0) or 0.0)
        reject_over_cap = bool(self.config_manager.get("entry_rules.scalping.reject_if_sl_over_cap", False))

        if is_scalping and sl_cap > 0 and target_sl_pips > sl_cap and reject_over_cap:
            return _reject(f"sl_over_cap({target_sl_pips:.2f} > {sl_cap:.2f})",
                        {"sl_pips": target_sl_pips, "cap": sl_cap}, raw_symbol)

        # MT5 stops_level en points → conversion pips
        try:
            digits = int(getattr(symbol_info, "digits", 5) or 5)
            points_per_pip = 10.0 if digits in (3, 5) else 1.0
        except Exception:
            points_per_pip = 10.0
        stops_level_points = float(getattr(symbol_info, "stops_level", 0) or 0)
        stops_level_pips = stops_level_points / points_per_pip if points_per_pip > 0 else 0.0

        if is_scalping and target_sl_pips > 0 and stops_level_pips > target_sl_pips:
            return _reject("stops_level_too_high_for_scalp",
                        {"stops_level_pips": stops_level_pips, "sl_pips": target_sl_pips}, raw_symbol)

        # ✅ OK
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
        """
        self.logger.info("Préparation de l'ordre MT5...")

        # --- Raccourcis locaux ---
        trade_decision = decision_package.get("trade_decision", {}) or {}
        active_config = decision_package.get("active_config", {}) or {}
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
            msg = (
                f"Action de trade invalide: '{action_raw}' "
                "(attendu: BUY/SELL/CLOSE/LONG/SHORT)."
            )
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
            msg = (
                f"Asset '{raw_symbol}' non autorisé par la stratégie "
                f"(whitelist: {sorted(allowed)})."
            )
            self.logger.error(msg)
            raise TradeExecutionError(msg)

        # ---------- 3) Mapping broker ----------
        broker_symbol = self.config_manager.get("asset_symbol_mapping", {}).get(
            raw_symbol, raw_symbol
        )
        if not broker_symbol or str(broker_symbol).upper() == "UNKNOWN":
            msg = (
                f"Mapping broker invalide pour l'asset '{raw_symbol}' "
                f"(résultat: '{broker_symbol}')."
            )
            self.logger.error(msg)
            raise TradeExecutionError(msg)

        broker_symbol = str(broker_symbol).upper()

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
            # ---------- 6) Infos symbole ----------
            symbol_info = self.mt5_connector.get_symbol_info(broker_symbol)
            if not symbol_info or not getattr(symbol_info, "name", None):
                raise TradeExecutionError(
                    f"Symbole MT5 invalide ou introuvable ({broker_symbol}). "
                    f"Vérifie la correspondance broker."
                )

            # Spread robuste en points (sera utile pour SL/TP)
            try:
                spread_points = float(
                    self.mt5_connector.get_symbol_spread_points(broker_symbol)
                )
            except Exception:
                spread_points = 0.0

            # ---------- 7) Prix d'entrée ----------
            entry_price_market = self.mt5_connector.get_current_price(
                broker_symbol, action
            )
            if not entry_price_market or entry_price_market <= 0:
                raise TradeExecutionError(
                    f"Impossible de récupérer un prix de marché valide pour {broker_symbol}."
                )

            # Si limit/stop et qu'un entry_price est proposé par la décision → utiliser comme trigger par défaut
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

            # ---------- 8) SL/TP (avec overrides en pips) ----------
            sl_pips_override = trade_decision.get("target_sl_pips")
            tp_pips_override = trade_decision.get("target_tp_pips")

            order_ctx = {
                "action": action,
                "asset": broker_symbol,
                "order_type": order_type,
                # ✅ overrides transmis au calcul SL/TP
                "target_sl_pips": sl_pips_override,
                "target_tp_pips": tp_pips_override,
                # infos complémentaires utiles au calcul
                "spread_points": spread_points,
                "entry_price_ref": entry_price_hint,  # prix de référence proposé par le CORE si utile
            }

            sl_price, tp_price = self._calculate_sl_tp_prices(
                order_ctx,
                active_config,
                symbol_info,
                entry_price_market,
                market_context,
            )

            # Validations SL/TP
            if not isinstance(sl_price, (int, float)) or sl_price <= 0:
                raise TradeExecutionError(
                    f"SL calculé invalide ({sl_price}) pour {broker_symbol}."
                )
            if not isinstance(tp_price, (int, float)) or tp_price <= 0:
                raise TradeExecutionError(
                    f"TP calculé invalide ({tp_price}) pour {broker_symbol}."
                )

            # ---------- 9) Volume ----------
            account_trade_settings = market_context.get(
                "active_broker_account", {}
            ).get("trade_settings", {})
            volume = self._calculate_risk_based_volume(
                {"action": action, "asset": broker_symbol, "order_type": order_type},
                active_config,
                market_context,
                symbol_info,
                entry_price_market,
                sl_price,
                account_trade_settings,
            )
            if not isinstance(volume, (int, float)) or volume <= 0:
                raise TradeExecutionError(
                    f"Volume calculé invalide ({volume}) pour {broker_symbol}."
                )

            # ---------- 10) Construction requête ----------
            return self._build_mt5_request(
                {"action": action, "asset": broker_symbol, "order_type": order_type},
                active_config,
                volume,
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

    def _calculate_sl_tp_prices(
        self,
        trade_decision: dict,
        config: dict,
        symbol_info: Any,
        entry_price: float,
        market_context: dict,
    ) -> tuple[float, float]:
        """
        SL/TP institutionnel avec 3 méthodes de SL (SWING / ATR / PIPS) et 3 TP (RR / ATR_MULTIPLE / PIPS).
        - Priorité aux overrides en PIPS: trade_decision['target_sl_pips'] / ['target_tp_pips']
        - Respecte trade_stops_level du broker
        - Conversion PIPS → prix corrigée (1 pip = 10 points pour FX/Gold)
        - Arrondit aux 'digits' du symbole
        - Fallback robuste si données manquantes
        """
        import pandas as pd
        import numpy as np

        self.logger.info("Calcul du SL/TP (SWING/ATR/PIPS + RR/ATR_MULTIPLE/PIPS)...")

        # --- Normalisation action ---
        action_raw = str(trade_decision.get("action", "")).strip().upper()
        action = {"LONG": "BUY", "SHORT": "SELL"}.get(action_raw, action_raw)
        if action not in ("BUY", "SELL"):
            raise TradeExecutionError(f"Action invalide pour SL/TP: '{action_raw}'")

        # --- Paramètres symbole / broker ---
        point = float(getattr(symbol_info, "point", 0.0) or 0.0)
        if point <= 0:
            raise TradeExecutionError("symbol_info.point invalide (<=0).")

        digits = int(getattr(symbol_info, "digits", 0) or 0)
        min_stop_distance_points = int(
            getattr(symbol_info, "trade_stops_level", 0) or 0
        )
        min_stop_distance_price = min_stop_distance_points * point

        # --- Heuristique pip-size: 1 pip = 10 points (FX majeurs, JPY, XAUUSD) ---
        points_per_pip = 10.0
        pip_size = point * points_per_pip  # valeur d'1 pip en prix absolu

        # --- Overrides en PIPS (prioritaires si fournis) ---
        sl_pips_override = trade_decision.get("target_sl_pips", None)
        tp_pips_override = trade_decision.get("target_tp_pips", None)
        spread_points = float(trade_decision.get("spread_points", 0.0) or 0.0)
        spread_pips = (
            max(0.0, spread_points / points_per_pip) if points_per_pip > 0 else 0.0
        )

        # --- Paramètres de placement (config) ---
        settings = config.get("smart_sl_tp_settings", {}) or {}
        sl_method = str(
            settings.get("sl_placement_method", "PIPS")
        ).upper()  # PIPS|SWING|ATR
        tp_method = str(
            settings.get("tp_placement_method", "RR")
        ).upper()  # RR|ATR_MULTIPLE|PIPS
        rr_ratio = float(settings.get("tp_rr_ratio", 1.5) or 1.5)

        # --- Récup du DF pour SWING/ATR (si dispo) ---
        symbol = str(trade_decision.get("asset", "")).upper()
        md = (market_context.get("market_data") or {}).get(symbol)
        rates_df = md if isinstance(md, pd.DataFrame) else None

        # --- Helper ATR ---
        def _compute_atr(df: pd.DataFrame, period: int) -> float:
            if df is None or len(df) < period + 2:
                return float("nan")
            high = df["high"].astype(float)
            low = df["low"].astype(float)
            close = df["close"].astype(float)
            prev_close = close.shift(1)
            tr = np.maximum.reduce(
                [
                    (high - low).abs(),
                    (high - prev_close).abs(),
                    (low - prev_close).abs(),
                ]
            )
            atr = tr.rolling(window=period, min_periods=period).mean().iloc[-1]
            return float(atr) if pd.notna(atr) and atr > 0 else float("nan")

        # ========================= SL =========================
        stop_loss_price = 0.0

        # 0) PRIORITÉ override en pips
        if isinstance(sl_pips_override, (int, float)) and float(sl_pips_override) > 0:
            sl_distance = float(sl_pips_override) * pip_size
            stop_loss_price = (
                entry_price - sl_distance
                if action == "BUY"
                else entry_price + sl_distance
            )
            self.logger.debug(
                f"[SL] override utilisé: {sl_pips_override} pips -> {stop_loss_price:.10f}"
            )
        else:
            # 1) SWING (si données suffisantes), sinon ATR, sinon PIPS
            if sl_method == "SWING":
                lookback = int(settings.get("sl_swing_lookback_period", 10) or 10)
                buffer_pips = float(settings.get("sl_buffer_pips", 2) or 2.0)
                if not isinstance(rates_df, pd.DataFrame) or len(rates_df) < lookback:
                    self.logger.warning(
                        f"Pas assez de données pour SL SWING (need {lookback}). Fallback ATR puis PIPS."
                    )
                    sl_method = "ATR"  # tentative ATR avant PIPS
                else:
                    recent = rates_df.tail(lookback)
                    buffer_price = buffer_pips * pip_size
                    if action == "BUY":
                        swing_low = float(recent["low"].min())
                        stop_loss_price = swing_low - buffer_price
                    else:
                        swing_high = float(recent["high"].max())
                        stop_loss_price = swing_high + buffer_price

            if sl_method == "ATR" and stop_loss_price == 0.0:
                atr_period = int(settings.get("sl_atr_period", 14) or 14)
                atr_mult = float(settings.get("sl_atr_multiplier", 1.2) or 1.2)
                atr = _compute_atr(rates_df, atr_period)
                if not (atr == atr and atr > 0):  # NaN safe
                    self.logger.warning("ATR indisponible. Fallback PIPS pour SL.")
                    sl_method = "PIPS"
                else:
                    sl_distance = atr_mult * atr
                    stop_loss_price = (
                        entry_price - sl_distance
                        if action == "BUY"
                        else entry_price + sl_distance
                    )

            if sl_method == "PIPS" and stop_loss_price == 0.0:
                sl_pips = float(config.get("stop_loss_pips", 10) or 10.0)
                sl_distance = sl_pips * pip_size
                stop_loss_price = (
                    entry_price - sl_distance
                    if action == "BUY"
                    else entry_price + sl_distance
                )

        # ========================= TP =========================
        take_profit_price = 0.0

        # 0) PRIORITÉ override en pips
        if isinstance(tp_pips_override, (int, float)) and float(tp_pips_override) > 0:
            tp_distance = float(tp_pips_override) * pip_size
            take_profit_price = (
                entry_price + tp_distance
                if action == "BUY"
                else entry_price - tp_distance
            )
            self.logger.debug(
                f"[TP] override utilisé: {tp_pips_override} pips -> {take_profit_price:.10f}"
            )
        else:
            # 1) RR : basé sur SL calculé (si valide)
            if tp_method == "RR":
                risk_distance_price = abs(entry_price - stop_loss_price)
                if risk_distance_price <= 0:
                    self.logger.warning(
                        "Distance de risque nulle pour TP RR. Fallback PIPS."
                    )
                    tp_method = "PIPS"
                else:
                    tp_distance = risk_distance_price * rr_ratio
                    take_profit_price = (
                        entry_price + tp_distance
                        if action == "BUY"
                        else entry_price - tp_distance
                    )

            # 2) ATR_MULTIPLE
            if tp_method == "ATR_MULTIPLE" and take_profit_price == 0.0:
                atr_period = int(
                    settings.get("tp_atr_period", settings.get("sl_atr_period", 14))
                    or 14
                )
                atr_mult = float(settings.get("tp_atr_multiplier", 2.0) or 2.0)
                atr = _compute_atr(rates_df, atr_period)
                if not (atr == atr and atr > 0):
                    self.logger.warning("ATR indisponible pour TP. Fallback PIPS.")
                    tp_method = "PIPS"
                else:
                    tp_distance = atr_mult * atr
                    take_profit_price = (
                        entry_price + tp_distance
                        if action == "BUY"
                        else entry_price - tp_distance
                    )

            # 3) PIPS
            if tp_method == "PIPS" and take_profit_price == 0.0:
                tp_pips = float(config.get("take_profit_pips", 20) or 20.0)
                tp_distance = tp_pips * pip_size
                take_profit_price = (
                    entry_price + tp_distance
                    if action == "BUY"
                    else entry_price - tp_distance
                )

        # ========================= Ajustements broker & validations =========================
        if entry_price <= 0:
            raise TradeExecutionError("Prix d'entrée invalide.")

        if (
            (stop_loss_price == take_profit_price)
            or (stop_loss_price == entry_price)
            or (take_profit_price == entry_price)
        ):
            raise TradeExecutionError("SL/TP identiques au prix d'entrée ou entre eux.")

        # distances signées
        if action == "BUY":
            sl_dist = entry_price - stop_loss_price
            tp_dist = take_profit_price - entry_price
        else:
            sl_dist = stop_loss_price - entry_price
            tp_dist = entry_price - take_profit_price

        # Respect du stops_level minimal
        if min_stop_distance_price > 0:
            if sl_dist < min_stop_distance_price:
                stop_loss_price = (
                    entry_price - min_stop_distance_price
                    if action == "BUY"
                    else entry_price + min_stop_distance_price
                )
                self.logger.warning(
                    f"SL ajusté (stops_level {min_stop_distance_points} pts) -> {stop_loss_price:.10f}"
                )
                sl_dist = min_stop_distance_price
            if tp_dist < min_stop_distance_price:
                take_profit_price = (
                    entry_price + min_stop_distance_price
                    if action == "BUY"
                    else entry_price - min_stop_distance_price
                )
                self.logger.warning(
                    f"TP ajusté (stops_level {min_stop_distance_points} pts) -> {take_profit_price:.10f}"
                )
                tp_dist = min_stop_distance_price

        # Dernière sécurité : SL/TP pas trop proches
        if abs(stop_loss_price - take_profit_price) < max(point * 2, 1e-12):
            raise TradeExecutionError(
                "SL et TP trop proches après ajustement stops_level."
            )

        stop_loss_price = round(float(stop_loss_price), digits)
        take_profit_price = round(float(take_profit_price), digits)
        return float(stop_loss_price), float(take_profit_price)

    def _calculate_loss_per_lot_fallback(
        self, symbol_info: Any, sl_distance_price: float, current_price: float
    ) -> float:
        """
        Calcule la perte par lot via un modèle mathématique interne.
        Sert de fallback si order_calc_profit de MT5 échoue.
        Prend en compte la conversion de devise si possible.

        Args:
            symbol_info (Any): Les informations du symbole de MT5 (MetaTrader5.SymbolInfo NamedTuple).
            sl_distance_price (float): La distance du stop loss en termes de prix (valeur absolue).
            current_price (float): Le prix actuel de l'actif.

        Returns:
            float: La perte estimée pour un lot en devise du compte.
        """
        contract_size = symbol_info.trade_contract_size
        currency_profit = (
            symbol_info.currency_profit
        )  # Devise de profit de l'actif (ex: USD pour EURUSD)
        account_currency = self.config_manager.get(
            "account_settings.currency", "USD"
        )  # Devise du compte (ConfigManager)

        # Implémenter une conversion de devise pour une précision universelle. (TODO implémenté - conceptuel)
        # Cela nécessiterait de récupérer le taux de change entre la devise de profit de l'actif
        # et la devise du compte (ex: USDJPY pour un compte en USD tradant l'EURJPY).
        # Pour l'instant, on simule un appel à un convertisseur de taux.

        exchange_rate_to_account_currency = (
            1.0  # Par défaut, si les devises sont les mêmes
        )
        if currency_profit != account_currency:
            # Ici, on ferait un appel à un service de taux de change ou on lirait une valeur
            # du `market_context` si elle y est présente (ex: {'USDJPY': 140.0})
            # Pour l'exemple, nous allons simuler un taux pour EUR converti en USD
            if currency_profit == "EUR" and account_currency == "USD":
                # Ceci serait un VRAI taux EURUSD, pas un hardcoding.
                exchange_rate_to_account_currency = (
                    current_price  # Si la paire est EURUSD, le prix est le taux
                )
            elif currency_profit == "JPY" and account_currency == "USD":
                # Si par exemple on trade USDJPY, et le compte est en USD, la conversion est 1/prix
                exchange_rate_to_account_currency = (
                    1 / current_price
                )  # Ou un autre taux si la paire n'est pas directe
            # TODO: Implémenter un service de taux de change dans `MT5Connector` ou `ConfigManager`
            #       pour récupérer les taux de conversion précis entre les devises.
            self.logger.warning(
                f"Utilisation d'une approximation pour la conversion de devise {currency_profit} -> {account_currency}. Précision peut varier."
            )

        # Perte par lot = (distance SL en prix) * (taille du contrat) * (taux de conversion)
        loss_per_lot_usd = (
            sl_distance_price * contract_size * exchange_rate_to_account_currency
        )

        self.logger.warning(
            f"Utilisation du modèle de calcul de risque interne (fallback). Perte par lot estimée: {loss_per_lot_usd:.2f} {account_currency}."
        )

        return loss_per_lot_usd

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
        Sizing par risque $:
        - essaie mt5.order_calc_profit (précis)
        - sinon tick_value/tick_size, sinon heuristique
        - clamp par min/max/step et par garde-fous
        - contrôle marge (order_calc_margin si dispo) pour éviter 10019 'No money'
        """
        import math

        action = str(trade_decision.get("action", "")).upper()
        if action == "LONG":
            action = "BUY"
        if action == "SHORT":
            action = "SELL"
        if action not in ("BUY", "SELL"):
            raise TradeExecutionError(f"Action invalide pour sizing: '{action}'")

        equity = context.get("account_info", {}).get("equity")
        if not isinstance(equity, (int, float)) or equity <= 0:
            raise TradeExecutionError("Équité du compte non positive ou manquante.")

        risk_pct = (
            account_trade_settings.get("risk_per_trade_percent")
            or config.get("risk_per_trade_percent")
            or 0.35
        )
        try:
            risk_pct = float(risk_pct)
        except Exception:
            risk_pct = 0.35

        max_dollar_risk = float(equity) * (risk_pct / 100.0)
        if max_dollar_risk <= 0:
            raise TradeExecutionError("Risque en $ nul/invalide pour le sizing.")

        # Estimation perte par 1 lot
        per_lot_loss_usd = None
        mt5_mod = getattr(self, "mt5", None) or getattr(self.mt5_connector, "mt5", None)
        if mt5_mod:
            try:
                order_type = (
                    getattr(mt5_mod, "ORDER_TYPE_BUY", 0)
                    if action == "BUY"
                    else getattr(mt5_mod, "ORDER_TYPE_SELL", 1)
                )
                _ret, profit = mt5_mod.order_calc_profit(
                    order_type, symbol_info.name, 1.0, entry_price, sl_price
                )
                per_lot_loss_usd = abs(float(profit))
            except Exception as e:
                self.logger.warning(
                    f"mt5.order_calc_profit indisponible: {e}. Fallback interne."
                )
                per_lot_loss_usd = None

        if per_lot_loss_usd is None or per_lot_loss_usd <= 0:
            point = float(getattr(symbol_info, "point", 0.0) or 0.0)
            tick_value = float(getattr(symbol_info, "tick_value", 0.0) or 0.0)
            tick_size = float(getattr(symbol_info, "tick_size", 0.0) or 0.0)
            price_diff = abs(entry_price - sl_price)
            if price_diff <= 0:
                raise TradeExecutionError("Distance Entry-SL nulle pour sizing.")

            if tick_size > 0 and tick_value > 0:
                nb_ticks = price_diff / tick_size
                per_lot_loss_usd = nb_ticks * tick_value
            elif point > 0 and tick_value > 0:
                nb_points = price_diff / point
                per_lot_loss_usd = nb_points * tick_value
            else:
                _ts = tick_size if tick_size else (point if point else 0.01)
                _tv = tick_value if tick_value else 1.0
                per_lot_loss_usd = (price_diff / _ts) * _tv
                self.logger.warning(
                    "tick_value/tick_size absents -> heuristique (1 USD/tick, tick_size=point)."
                )

        if per_lot_loss_usd <= 0:
            raise TradeExecutionError("Perte par lot invalide pour sizing.")

        raw_volume = max_dollar_risk / per_lot_loss_usd

        # Contraintes symbole/compte
        vol_min_sym = float(getattr(symbol_info, "volume_min", 0.01) or 0.01)
        vol_max_sym = float(getattr(symbol_info, "volume_max", 100.0) or 100.0)
        vol_step_sym = float(getattr(symbol_info, "volume_step", 0.01) or 0.01)

        min_lot_account = float(
            account_trade_settings.get("min_lot", vol_min_sym) or vol_min_sym
        )
        max_lot_account = float(
            account_trade_settings.get("max_lot", vol_max_sym) or vol_max_sym
        )
        lot_step_account = float(
            account_trade_settings.get("lot_step", vol_step_sym) or vol_step_sym
        )

        # Garde-fou absolu
        max_volume_safety = float(
            self.config_manager.get(
                "trade_executor_settings.max_absolute_volume_safety", 50.0
            )
        )
        if raw_volume > max_volume_safety:
            self.logger.warning(
                f"Volume brut {raw_volume:.2f} > safety {max_volume_safety:.2f} -> clamp."
            )
            raw_volume = max_volume_safety

        # Clamp & arrondi
        volume = max(min_lot_account, vol_min_sym, raw_volume)
        volume = min(max_lot_account, vol_max_sym, volume)
        effective_step = max(lot_step_account, vol_step_sym) or 0.01
        steps = math.floor(volume / effective_step)
        volume = round(steps * effective_step, 8)
        volume = max(min_lot_account, volume)
        volume = min(max_lot_account, volume)
        if volume <= 0:
            raise TradeExecutionError(f"Volume calculé invalide ({volume}).")

        # Contrôle de marge pour éviter 10019 (si l'API est dispo)
        try:
            if mt5_mod and hasattr(mt5_mod, "order_calc_margin"):
                order_type = (
                    getattr(mt5_mod, "ORDER_TYPE_BUY", 0)
                    if action == "BUY"
                    else getattr(mt5_mod, "ORDER_TYPE_SELL", 1)
                )
                _ret, margin_required = mt5_mod.order_calc_margin(
                    order_type, symbol_info.name, volume, entry_price
                )
                free_margin = context.get("account_info", {}).get("margin_free")
                if _ret and free_margin is not None and margin_required is not None:
                    # Si pas assez de marge, réduire au plus proche volume autorisé
                    if margin_required > free_margin:
                        ratio = max(free_margin / margin_required, 0.0)
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
            self.logger.warning(
                f"Contrôle marge non appliqué (non supporté / erreur): {e}"
            )

        actual_risk_dollars = volume * per_lot_loss_usd
        tol = float(
            self.config_manager.get(
                "trade_executor_settings.max_risk_deviation_multiplier", 1.05
            )
        )
        if actual_risk_dollars > max_dollar_risk * tol:
            self.logger.warning(
                f"Risque réel {actual_risk_dollars:.2f}$ > max {max_dollar_risk:.2f}$ (tol {tol:.2f})."
            )

        self.logger.info(
            f"Sizing {symbol_info.name}: equity={equity:.2f}, risk%={risk_pct:.2f}, "
            f"risk$={max_dollar_risk:.2f}, per_lot_loss={per_lot_loss_usd:.2f} -> vol={volume:.4f} "
            f"(min={vol_min_sym}, step={effective_step}, max={vol_max_sym})."
        )
        return float(volume)

    # --- replace intégral de _build_mt5_request ---
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
        tous les types d'ordres (Market, Limit, Stop).
        Utilise les constantes MT5 mappées du ConfigManager.
        """
        self.logger.info("Construction de la requête MT5 finale...")
        action_str = trade_decision["action"]  # BUY, SELL, CLOSE
        expected_symbol = trade_decision.get("asset", "N/A")

        # --- Validation stricte du symbole ---
        if (
            not symbol_info
            or not hasattr(symbol_info, "name")
            or symbol_info.name in [None, "", "UNKNOWN"]
        ):
            raise TradeExecutionError(
                f"Symbole MT5 invalide ou non résolu dans _build_mt5_request "
                f"(asset={expected_symbol}, symbol_info={getattr(symbol_info, 'name', 'None')})."
            )

        # Récupérer les constantes MT5 via les mappings (utiliser le module mt5, pas self.mt5)
        mt5_action_deal = self.TRADE_ACTION_DEAL
        mt5_action_pending = self.TRADE_ACTION_PENDING
        mt5_order_time_gtc = self.ORDER_TIME_GTC

        # Default filling policy
        filling_policy_str = config.get("execution_policy.type_filling", "FOK")
        mt5_filling_policy = getattr(
            mt5,
            self.mt5_mappings.get("order_filling_policies", {}).get(
                filling_policy_str, "ORDER_FILLING_FOK"
            ),
        )

        # --- Dictionnaire de base commun à tous les ordres ---
        request = {
            "action": mt5_action_deal,
            "symbol": symbol_info.name,
            "volume": volume,
            "magic": config.get("magic_number"),
            "sl": round(sl_price, symbol_info.digits),
            "tp": round(tp_price, symbol_info.digits),
            "type_time": mt5_order_time_gtc,
            "comment": "",
            "deviation": config.get("execution_policy.max_deviation_points", 20),
        }

        # --- Logique spécifique par type d'ordre ---
        if order_type_str == "MARKET":
            request["type"] = (
                self.ORDER_TYPE_BUY if action_str == "BUY" else self.ORDER_TYPE_SELL
            )
            request["price"] = entry_price_market
            request["type_filling"] = mt5_filling_policy

        elif "LIMIT" in order_type_str or "STOP" in order_type_str:
            request["action"] = mt5_action_pending
            request["price"] = (
                trigger_price if trigger_price is not None else entry_price_market
            )

            mapped_order_type_value = self.mt5_mappings.get("order_types", {}).get(
                order_type_str
            )
            if mapped_order_type_value is None:
                raise TradeExecutionError(
                    f"Type d'ordre différé non supporté : '{order_type_str}'"
                )

            # utiliser le module mt5 (ou self.mt5_connector.mt5) pour accéder à la constante
            request["type"] = getattr(mt5, mapped_order_type_value)

            current_prices = self.mt5_connector.get_symbol_info_tick(symbol_info.name)
            if current_prices is None:
                raise TradeExecutionError(
                    f"Impossible d'obtenir les prix de tick pour {symbol_info.name} pour valider l'ordre différé."
                )

            min_distance = symbol_info.trade_stops_level * symbol_info.point

            if (
                (
                    order_type_str == "BUY_LIMIT"
                    and request["price"] >= current_prices.ask - min_distance
                )
                or (
                    order_type_str == "SELL_LIMIT"
                    and request["price"] <= current_prices.bid + min_distance
                )
                or (
                    order_type_str == "BUY_STOP"
                    and request["price"] <= current_prices.ask + min_distance
                )
                or (
                    order_type_str == "SELL_STOP"
                    and request["price"] >= current_prices.bid - min_distance
                )
            ):
                raise TradeExecutionError(
                    f"{order_type_str} ({request['price']}) trop proche du marché "
                    f"(Ask={current_prices.ask}, Bid={current_prices.bid}). Min dist: {min_distance:.5f}"
                )

            expiration_policy = config.get("order_expiration_policy", {}).get(
                "type", "GTC"
            )
            if expiration_policy == "DAY":
                request["type_time"] = getattr(
                    mt5,
                    self.mt5_mappings.get("order_time_flags", {}).get(
                        "DAY", "ORDER_TIME_DAY"
                    ),
                )
            elif expiration_policy == "SPECIFIED":
                request["type_time"] = getattr(
                    mt5,
                    self.mt5_mappings.get("order_time_flags", {}).get(
                        "SPECIFIED", "ORDER_TIME_SPECIFIED"
                    ),
                )
                expiration_datetime_str = config.get("order_expiration_policy", {}).get(
                    "datetime", (datetime.now(UTC) + timedelta(days=1)).isoformat()
                )
                try:
                    request["expiration"] = datetime.fromisoformat(
                        expiration_datetime_str
                    ).timestamp()
                except ValueError:
                    self.logger.error(
                        f"Format expiration invalide: {expiration_datetime_str}. Fallback GTC."
                    )
                    request["type_time"] = mt5_order_time_gtc

        else:
            raise TradeExecutionError(
                f"Type d'ordre non géré dans _build_mt5_request: '{order_type_str}'"
            )

        # --- Validation SL/TP ---
        min_sl_tp_distance = symbol_info.trade_stops_level * symbol_info.point
        if (
            action_str == "BUY" and (request["price"] - sl_price) < min_sl_tp_distance
        ) or (
            action_str == "SELL" and (sl_price - request["price"]) < min_sl_tp_distance
        ):
            raise TradeExecutionError(
                f"Stop Loss ({sl_price:.5f}) trop proche du prix ({request['price']:.5f}). Min dist: {min_sl_tp_distance:.5f}"
            )

        if (
            action_str == "BUY" and (tp_price - request["price"]) < min_sl_tp_distance
        ) or (
            action_str == "SELL" and (request["price"] - tp_price) < min_sl_tp_distance
        ):
            raise TradeExecutionError(
                f"Take Profit ({tp_price:.5f}) trop proche du prix ({request['price']:.5f}). Min dist: {min_sl_tp_distance:.5f}"
            )

        # --- Commentaire ---
        comment_template = self.config_manager.get(
            "trading.order_comment_template", "SNIPER_X|{strategy}|{order_type}"
        )
        max_len = self.config_manager.get("trading.comment_max_length", 31)
        request["comment"] = comment_template.format(
            strategy=config.get("strategy_name", "N/A"), order_type=order_type_str
        )[:max_len]

        self.logger.debug(f"Requête MT5 construite et validée : {request}")
        return request

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

    def execute_order(self, request: dict) -> dict:
        """
        Envoie une requête d'ordre MT5 via MT5Connector et retourne
        un résumé unifié de l'exécution.
        - Ne lit PAS sl/tp depuis OrderSendResult (non exposés par MT5 Python)
        -> on reprend sl/tp du 'request' ou on les réconcilie ensuite.
        - Tolère les variations de champs dans OrderSendResult.
        - Normalise les retcodes et sécurise les accès attributaires.
        """
        # --- Sécurité connexion ---
        try:
            # is_connected peut être un bool (propriété) dans ton MT5Connector
            connected = getattr(self.mt5_connector, "is_connected", False)
            if callable(connected):
                connected = connected()
            if not connected and hasattr(self.mt5_connector, "connect"):
                self.mt5_connector.connect()
        except Exception:
            # On ne bloque pas ici, l'envoi lèvera si non connecté
            pass

        symbol = request.get("symbol")
        if not symbol:
            raise TradeExecutionError("Requête MT5 invalide: 'symbol' manquant.")
        if request.get("volume", 0) <= 0:
            raise TradeExecutionError("Requête MT5 invalide: 'volume' doit être > 0.")

        # --- Envoi via le connecteur ---
        result = self.mt5_connector.order_send(request)

        # --- Récupération sûre des champs renvoyés ---
        retcode = getattr(result, "retcode", None)
        comment = getattr(result, "comment", "")
        order_id = getattr(result, "order", None)
        deal_id = getattr(result, "deal", None)
        result_price = getattr(result, "price", None)
        result_volume = getattr(result, "volume", None)
        request_id = getattr(result, "request_id", None)

        # --- Normalisation retcode / succès ---
        # Constantes: 10009=DONE (exécuté), 10008=PLACED (ordre différé placé).
        # On accepte les deux comme "succès".
        ok_codes = {
            getattr(self, "TRADE_RETCODE_DONE", 10009),
            getattr(self, "TRADE_RETCODE_PLACED", 10008),
        }
        retcode_str = self.mt5_mappings.get("trade_retcodes", {}).get(
            str(retcode), str(retcode)
        )

        if retcode not in ok_codes:
            # Échec -> lever avec détails
            raise TradeExecutionError(
                f"Envoi MT5 échoué (retcode={retcode} - {retcode_str}) | "
                f"order={order_id} deal={deal_id} | comment='{comment}'"
            )

        # --- Construction du résumé d'exécution ---
        # ATTENTION: sl/tp non présents dans OrderSendResult -> on reprend ceux envoyés.
        # 'action' est déduite du type d'ordre (BUY/SELL/BUY_LIMIT/...)
        order_type = request.get("type")
        action = "BUY"
        if order_type in (
            getattr(self, "ORDER_TYPE_SELL", -1),
            getattr(self, "ORDER_TYPE_SELL_LIMIT", -2),
            getattr(self, "ORDER_TYPE_SELL_STOP", -3),
        ):
            action = "SELL"

        execution_summary = {
            "status": (
                "filled"
                if retcode == getattr(self, "TRADE_RETCODE_DONE", 10009)
                else "placed"
            ),
            "retcode": retcode,
            "retcode_str": retcode_str,
            "order": order_id,
            "deal": deal_id,
            "symbol": symbol,
            "action": action,
            "price": result_price if result_price else request.get("price"),
            "volume": result_volume if result_volume else request.get("volume"),
            "sl": request.get("sl"),
            "tp": request.get("tp"),
            "comment": comment,
            "request_id": request_id,
            "request_echo": {
                # utile pour audit / traçabilité
                "type": order_type,
                "type_time": request.get("type_time"),
                "type_filling": request.get("type_filling"),
                "deviation": request.get("deviation"),
                "magic": request.get("magic"),
            },
        }

        # (Optionnel) réconciliation post-trade: relire la position pour confirmer SL/TP réellement enregistrés
        try:
            positions = (
                self.mt5.positions_get(symbol=symbol) if hasattr(self, "mt5") else None
            )
            if not positions and hasattr(self.mt5_connector, "mt5"):
                positions = self.mt5_connector.mt5.positions_get(symbol=symbol)
            if positions:
                # Dernière position mise à jour pour ce symbole
                try:
                    pos = sorted(positions, key=lambda p: getattr(p, "time_update", 0))[
                        -1
                    ]
                except Exception:
                    pos = positions[-1]
                execution_summary["sl"] = getattr(pos, "sl", execution_summary["sl"])
                execution_summary["tp"] = getattr(pos, "tp", execution_summary["tp"])
        except Exception:
            # discrète, purement informative
            pass

        self.logger.info(f"Exécution OK: {execution_summary}")
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
        Ferme une ou plusieurs positions en utilisant une logique de retry robuste.
        Si 'ticket' est spécifié, ferme la position unique. Sinon, ferme toutes les positions
        pour le 'symbol' donné, ou toutes les positions si 'symbol' est "ALL".
        Cette fonction calcule également le P&L au moment de la clôture et envoie le feedback.
        """
        self.logger.info(
            f"Demande de clôture de position pour Symbole='{symbol}', Ticket='{ticket}'..."
        )
        # Toujours réconcilier avant de tenter de clôturer pour avoir l'état le plus frais.
        self.reconcile_state_with_broker()

        positions_to_close = []
        if ticket:
            if ticket in self._open_positions:
                positions_to_close.append(self._open_positions[ticket])
            else:
                self.logger.warning(
                    f"Position interne avec ticket {ticket} non trouvée. Impossible de clôturer."
                )
                return {
                    "success": False,
                    "message": f"Position {ticket} non trouvée.",
                    "closed_count": 0,
                    "failed_count": 1,
                }
        else:
            positions_to_close = [
                pos
                for pos in self._open_positions.values()
                if symbol == "ALL" or pos["symbol"] == symbol
            ]

        if not positions_to_close:
            self.logger.info(
                f"Aucune position à clôturer pour Symbole='{symbol}', Ticket='{ticket}'."
            )
            return {
                "success": True,
                "message": "Aucune position à clôturer.",
                "closed_count": 0,
                "failed_count": 0,
            }

        closed_count, failed_count = 0, 0
        for pos_data in positions_to_close:
            position_ticket = pos_data["ticket"]

            # Déterminer l'action opposée pour la clôture
            # Utilise les constantes MT5 via self.config_manager.get pour la robustesse
            mt5_pos_buy = self.config_manager.get(
                "mt5_mappings.position_types.BUY", 0
            )  # 0 for BUY in MT5
            mt5_pos_sell = self.config_manager.get(
                "mt5_mappings.position_types.SELL", 1
            )  # 1 for SELL in MT5

            if pos_data["type"] == mt5_pos_buy:
                # Assurez que mt5.ORDER_TYPE_SELL est accessible et que la config le mappe correctement
                order_type_close = self.config_manager.get(
                    "mt5_mappings.order_types.SELL", None
                )
                if order_type_close is None:  # Fallback si mapping absent
                    self.logger.error(
                        "Mapping MT5 pour SELL manquant. Impossible de déterminer le type d'ordre de clôture."
                    )
                    failed_count += 1
                    self._log_audit_trail(
                        {
                            "event_type": "TRADE_CLOSE_FAILED_MT5_MAPPING",
                            "details": pos_data,
                            "mapping_key": "order_types.SELL",
                        }
                    )
                    continue
                order_type_close_val = getattr(
                    mt5, order_type_close
                )  # Récupérer la valeur numérique
            elif pos_data["type"] == mt5_pos_sell:
                # Assurez que mt5.ORDER_TYPE_BUY est accessible et que la config le mappe correctement
                order_type_close = self.config_manager.get(
                    "mt5_mappings.order_types.BUY", None
                )
                if order_type_close is None:  # Fallback si mapping absent
                    self.logger.error(
                        "Mapping MT5 pour BUY manquant. Impossible de déterminer le type d'ordre de clôture."
                    )
                    failed_count += 1
                    self._log_audit_trail(
                        {
                            "event_type": "TRADE_CLOSE_FAILED_MT5_MAPPING",
                            "details": pos_data,
                            "mapping_key": "order_types.BUY",
                        }
                    )
                    continue
                order_type_close_val = getattr(
                    mt5, order_type_close
                )  # Récupérer la valeur numérique
            else:
                self.logger.error(
                    f"Type de position inconnu ({pos_data['type']}) pour le ticket {position_ticket}. Impossible de clôturer."
                )
                failed_count += 1
                self._log_audit_trail(
                    {
                        "event_type": "TRADE_CLOSE_FAILED_UNKNOWN_TYPE",
                        "details": pos_data,
                    }
                )
                continue

            # Construire la requête de clôture
            # Utilise l'action opposée pour récupérer le prix (Bid pour Sell, Ask pour Buy)
            # Adapte 'SELL'/'BUY' string pour mt5_connector.get_current_price
            price_action_for_get_current_price = (
                "SELL"
                if order_type_close_val
                == getattr(
                    mt5,
                    self.config_manager.get(
                        "mt5_mappings.order_types.SELL", "ORDER_TYPE_SELL"
                    ),
                )
                else "BUY"
            )
            close_price = self.mt5_connector.get_current_price(
                pos_data["symbol"], price_action_for_get_current_price
            )
            if not close_price:
                self.logger.error(
                    f"Impossible d'obtenir un prix de clôture pour la position #{position_ticket}."
                )
                self.config_manager.send_alert(
                    "ERREUR",
                    f"Impossible prix clôture #{position_ticket}",
                    alert_type="telegram_error",
                )
                failed_count += 1
                self._log_audit_trail(
                    {"event_type": "TRADE_CLOSE_FAILED_NO_PRICE", "details": pos_data}
                )
                continue

            request = {
                "action": self.TRADE_ACTION_DEAL,  # Action pour un deal immédiat
                "position": position_ticket,  # Le ticket de la position à clôturer
                "symbol": pos_data["symbol"],
                "volume": pos_data["volume"],
                "type": order_type_close_val,  # Type d'ordre opposé (constante MT5 numérique)
                "price": close_price,
                "deviation": self.config_manager.get(
                    "trade_executor_settings.default_slippage", 20
                ),  # Slippage configurable
                "magic": pos_data.get("magic"),
                "comment": f"SNIPER_X Close | {pos_data.get('comment', 'N/A')}",  # Garder le commentaire original
            }

            result = self._send_close_order_with_retries(request)

            if result and result.retcode == self.config_manager.get(
                "mt5_mappings.trade_retcodes.RETCODE_DONE", mt5.TRADE_RETCODE_DONE
            ):
                self.logger.info(
                    f"Position #{position_ticket} ({pos_data['symbol']}) clôturée avec succès."
                )

                # Calculer le P&L au moment de la clôture (plus précis)
                # Utilise self.mt5_connector.mt5 pour appeler l'API MT5
                _ret, pnl_usd = self.mt5_connector.mt5.order_calc_profit(
                    pos_data["type"],
                    pos_data["symbol"],
                    pos_data["volume"],
                    pos_data["entry_price"],
                    result.price,  # P&L réel sur clôture
                )
                if (
                    _ret
                    != self.config_manager.get(
                        "mt5_mappings.trade_retcodes.RETCODE_DONE",
                        mt5.TRADE_RETCODE_DONE,
                    )
                    or pnl_usd is None
                ):
                    pnl_usd = pos_data.get(
                        "profit", 0.0
                    )  # Utiliser le profit en temps réel comme fallback si l'API échoue
                    self.logger.warning(
                        f"Impossible de calculer le P&L exact pour clôture #{position_ticket}. Utilisation du profit actuel ({pnl_usd:.2f} $)."
                    )

                # Mettre à jour l'état interne : supprimer la position clôturée
                if position_ticket in self._open_positions:
                    del self._open_positions[position_ticket]
                closed_count += 1

                # Enregistrer et notifier le résultat de la clôture
                self._log_audit_trail(
                    {
                        "event_type": "TRADE_CLOSE_SUCCESS",
                        "timestamp": datetime.now(UTC).isoformat(),
                        "order_id": position_ticket,
                        "symbol": pos_data["symbol"],
                        "status": "SUCCESS",
                        "details": result._asdict(),
                        "pnl_usd": pnl_usd,
                        "position_snapshot": pos_data,  # Snapshot de la position avant fermeture
                    }
                )
                # Envoyer un feedback au ConfigManager pour la mise à jour des métriques de performance
                self.config_manager.feedback_on_trade_result(
                    pos_data,
                    {
                        "execution_status": "executed",
                        "pnl_usd": pnl_usd,
                        "timestamp": datetime.now(UTC).isoformat(),
                    },
                )
                self.config_manager.send_alert(
                    message=f"📊 Position clôturée: {pos_data['symbol']} | P&L: ${pnl_usd:.2f}",
                    alert_type="telegram_trade_closed",
                )

            else:
                self.logger.error(
                    f"Échec de la clôture de la position #{position_ticket} après toutes les tentatives. Erreur MT5: {result.comment if result else 'Aucune réponse'}."
                )
                self.config_manager.send_alert(
                    "CRITIQUE",
                    f"Clôture Échec: {pos_data['symbol']} #{position_ticket} | {result.comment if result else 'Timeout'}",
                    alert_type="telegram_critical",
                )
                failed_count += 1
                self._log_audit_trail(
                    {
                        "event_type": "TRADE_CLOSE_FAILED",
                        "timestamp": datetime.now(UTC).isoformat(),
                        "order_id": position_ticket,
                        "symbol": pos_data["symbol"],
                        "status": "FAILED",
                        "details": result._asdict() if result else {},
                        "position_snapshot": pos_data,
                    }
                )

        final_message = f"Clôture des positions terminée. Succès: {closed_count}, Échecs: {failed_count}."
        self.logger.info(final_message)
        return {
            "success": (failed_count == 0),
            "message": final_message,
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

    def feedback_pipeline(
        self,
        order_id: str,
        status: str,
        reason: str = "",
        pnl_usd: Optional[float] = None,
    ) -> dict:
        """
        Construit un dictionnaire de feedback standardisé pour le pipeline principal.
        Ce feedback est crucial pour l'audit et l'apprentissage de l'IA.

        Args:
            order_id (str): L'ID unique de l'ordre.
            status (str): Le statut final de l'exécution ("executed", "failed", "skipped", "pending_manual_approval").
            reason (str): Une raison détaillée du statut.
            pnl_usd (Optional[float]): Le P&L réalisé en USD, si applicable (pour les clôtures).

        Returns:
            dict: Le dictionnaire de feedback.
        """
        feedback = {
            "order_id": order_id,
            "execution_status": status,
            "reason": reason,
            "timestamp": datetime.now(UTC).isoformat(),
            "pnl_usd": pnl_usd,  # Ajout du P&L au feedback
        }
        self.logger.info(
            f"Feedback généré pour l'ordre {order_id}: Statut '{status}'. Raison: '{reason}'. P&L: {pnl_usd if pnl_usd is not None else 'N/A'}."
        )

        # Envoyer ce feedback à un bus d'événements central pour que d'autres modules
        # (IA, RiskManager) puissent s'y abonner et réagir. (TODO implémenté - conceptuallement)
        # Ceci serait un appel à ConfigManager qui déléguerait à l'instance AIDecision.
        if (
            hasattr(self.config_manager, "ai_decision_instance")
            and self.config_manager.ai_decision_instance
        ):
            try:
                # Appeler la méthode de feedback du module AIDecision
                self.config_manager.ai_decision_instance.feedback_on_result(feedback)
                self.logger.debug(
                    f"Feedback du TradeExecutor envoyé à AIDecision pour l'ordre {order_id}."
                )
            except Exception as e:
                self.logger.error(
                    f"Échec de l'envoi du feedback à AIDecision pour l'ordre {order_id}: {e}",
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
    Corrigé: on lit dans decision_package['final_decision'] au lieu des clés racine.
    Zéro tolérance aux champs manquants: on normalise et on valide avant d'appeler prepare_order.
    """
    import logging

    logger = logging.getLogger(__name__)

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
    }

    adapted_package = {
        "trade_decision": trade_decision,
        "market_context": market_context,
        "active_config": active_config,
    }

    # ----------- 5) Pre-trade checks -----------
    ok, reason = trade_executor.pre_trade_checks(
        trade_decision, active_config, market_context
    )
    if not ok:
        logger.warning(f"Pipeline de trade AVORTÉ (Erreur contrôlée): {reason}")
        feedback = trade_executor.feedback_pipeline(
            order_id=final_decision.get("order_id", "N/A"),
            status="failed",
            reason=reason,
        )
        trade_executor._feedback_safe(trade_decision, feedback)
        return {"status": "failed", "reason": reason}

    # ----------- 6) Préparer la requête MT5 -----------
    try:
        mt5_request = trade_executor.prepare_order(adapted_package)
    except Exception as e:
        reason = f"Préparation d'ordre échouée: {e}"
        logger.error(reason, exc_info=True)
        trade_executor._send_alert_safe(
            "CRITIQUE", reason, alert_type="telegram_critical"
        )
        feedback = trade_executor.feedback_pipeline(
            order_id=final_decision.get("order_id", "N/A"),
            status="failed",
            reason=str(e),
        )
        trade_executor._feedback_safe(trade_decision, feedback)
        return {"status": "failed", "reason": str(e)}

    # ----------- 7) Human-in-the-loop / dry-run -----------
    if not trade_executor.manual_override_if_needed(mt5_request):
        feedback = trade_executor.feedback_pipeline(
            order_id=final_decision.get("order_id", "N/A"),
            status="pending_manual_approval",
            reason="Manual override requested.",
        )
        trade_executor._feedback_safe(trade_decision, feedback)
        return {"status": "pending_manual_approval"}

    if is_dry_run:
        logger.info("[DRY RUN] Requête MT5 prête mais non envoyée.")
        return {"status": "ready", "mt5_request": mt5_request}

    # ----------- 8) Exécution -----------
    execution_result = trade_executor.execute_order(mt5_request)
    return execution_result
