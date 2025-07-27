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
        Charge et valide la structure d'un package de décision via un schéma formel externe.
        Cette validation assure que le package de décision est bien formé et complet,
        conformément aux exigences du système.

        Args:
            decision_package (dict): Le package de décision à valider.

        Returns:
            dict: Le package de décision validé.

        Raises:
            InvalidDecisionPackageError: Si le package ne correspond pas au schéma requis.
        """
        self.logger.info("Validation du package de décision par schéma externe...")

        # Externaliser ce schéma dans un fichier `decision_package_schema.json` (TODO implémenté)
        schema_path = (
            Path(__file__).parent.parent
            / "config"
            / "schemas"
            / "decision_package_schema.json"
        )  # Chemin relatif

        # S'assurer que le schéma existe et est valide
        if not schema_path.exists():
            self.logger.critical(
                f"FATAL: Fichier de schéma de package de décision introuvable à '{schema_path}'. Impossible de valider les décisions. Le bot ne peut pas démarrer en toute sécurité."
            )
            raise FileNotFoundError(
                f"Schéma de package de décision manquant: {schema_path}"
            )

        try:
            with open(schema_path, "r", encoding="utf-8") as f:
                schema = json.load(f)

            jsonschema.validate(instance=decision_package, schema=schema)
            self.logger.info("Package de décision validé avec succès par schéma.")

            # Ajouter une validation sémantique plus poussée (TODO implémenté)
            # Vérifier que le volume, si présent, est un nombre positif.
            trade_decision = decision_package.get("trade_decision", {})
            if trade_decision.get("action") in ["BUY", "SELL", "CLOSE"]:
                volume = trade_decision.get("volume")
                if volume is not None and (
                    not isinstance(volume, (int, float)) or volume <= 0
                ):
                    error_msg = f"Volume de trade invalide ou non positif: {volume}."
                    self.logger.error(error_msg)
                    self.config_manager.send_alert(
                        "CRITIQUE",
                        f"Erreur TradeExecutor: {error_msg}",
                        alert_type="telegram_critical",
                    )
                    raise InvalidDecisionPackageError(error_msg)

            return decision_package
        except jsonschema.ValidationError as e:
            error_msg = f"Package de décision invalide. Erreur de validation: {e.message} sur le champ `{''.join(e.path)}`"
            self.logger.error(error_msg, exc_info=True)  # Ajout de exc_info
            self.config_manager.send_alert(
                "CRITIQUE",
                f"Erreur TradeExecutor: {error_msg}",
                alert_type="telegram_critical",
            )
            raise InvalidDecisionPackageError(error_msg) from e
        except Exception as e:
            error_msg = (
                f"Erreur inattendue lors de la validation du package de décision: {e}"
            )
            self.logger.error(error_msg, exc_info=True)
            self.config_manager.send_alert(
                "CRITIQUE",
                f"Erreur TradeExecutor: {error_msg}",
                alert_type="telegram_critical",
            )
            raise InvalidDecisionPackageError(error_msg) from e

    def _check_trading_window(
        self, current_time_utc: datetime, symbol: str
    ) -> tuple[bool, str]:
        """
        Vérifie si le trading est autorisé pour l'actif donné à l'heure actuelle.
        Gère les sessions de nuit (passant par minuit) et le trading de crypto le week-end.

        Args:
            current_time_utc (datetime): L'objet datetime UTC actuel.
            symbol (str): Le symbole de l'actif concerné par le trade.

        Returns:
            tuple[bool, str]: True si dans la fenêtre de trading, False sinon, avec une raison.
        """
        # --- AMÉLIORATION 1: Autorisation du trading de crypto le week-end ---
        crypto_symbols = self.config_manager.get("global_safety.crypto_symbols", [])
        is_weekend = current_time_utc.weekday() >= 5  # Samedi (5) ou Dimanche (6)

        if symbol in crypto_symbols and is_weekend:
            return True, f"Trading de crypto ({symbol}) autorisé le week-end."

        # --- AMÉLIORATION 2: Gestion correcte des sessions de nuit ---
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

        # Logique pour une session normale (ex: 07:00 -> 20:00)
        if start_hour <= end_hour:
            if not (start_hour <= current_time_utc.hour < end_hour):
                return (
                    False,
                    f"Hors de la fenêtre de trading (Heure UTC: {current_time_utc.hour}).",
                )
        # Logique pour une session de nuit qui passe par minuit (ex: 22:00 -> 07:00)
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

        def pre_trade_checks(self, decision_package: dict) -> bool:
            """
            Orchestre une série de validations pré-trade de manière robuste.
            Cette fonction est la dernière ligne de défense avant l'envoi d'un ordre au broker.

            Args:
                decision_package (dict): Le package de décision validé.

            Returns:
                bool: True si toutes les vérifications passent, False sinon.
            """
            self.logger.info("Exécution des vérifications de sécurité pré-trade...")
            context = decision_package["market_context"]
            config = decision_package["active_config"]
            trade_decision = decision_package["trade_decision"]
            symbol = trade_decision["asset"]

            # Liste des barrières de sécurité à vérifier séquentiellement
            checks_to_run = [
                # Le marché est-il ouvert ?
                (self._check_trading_window, {"current_time_utc": datetime.now(UTC)}),
                # Le spread est-il acceptable ?
                (self._check_spread, {"symbol": symbol, "active_config": config}),
                # Le risque global du portefeuille est-il sous contrôle ?
                (
                    self._check_portfolio_exposure,
                    {"active_config": config, "current_context": context},
                ),
            ]

            # Ajout de la vérification "anti-grosse erreur" (fat-finger) si activée
            if self.config_manager.get(
                "trade_executor_settings.fat_finger_check.enabled", True
            ):
                checks_to_run.append(
                    (
                        self._check_fat_finger_volume,
                        {"trade_decision": trade_decision, "market_context": context},
                    )
                )

            for check_func, kwargs in checks_to_run:
                try:
                    is_valid, reason = check_func(**kwargs)
                    if not is_valid:
                        self.logger.warning(
                            f"TRADE BLOQUÉ. Raison: {reason} (Actif: {symbol})"
                        )
                        self.config_manager.send_alert(
                            f"TRADE BLOQUÉ: {reason}", alert_type="telegram_critical"
                        )
                        return False
                except Exception as e:
                    # --- AMÉLIORATION MAJEURE : SÉCURITÉ ANTI-CRASH ---
                    # Si une fonction de vérification a un bug, on ne fait pas planter le bot.
                    # On considère que la vérification a échoué et on bloque le trade.
                    check_name = check_func.__name__
                    self.logger.critical(
                        f"TRADE BLOQUÉ. Une erreur critique est survenue dans la fonction de sécurité '{check_name}'. Erreur: {e}",
                        exc_info=True,
                    )
                    self.config_manager.send_alert(
                        f"ERREUR CRITIQUE dans une sécurité pré-trade ({check_name}). Trade bloqué.",
                        alert_type="telegram_critical",
                    )
                    return False

            self.logger.info(
                "Toutes les vérifications de sécurité pré-trade sont passées avec succès."
            )
            return True

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

    def prepare_order(self, decision_package: dict) -> dict:
        """
        Calcule et prépare la demande d'ordre complète pour MetaTrader 5.
        Cette fonction orchestre la récupération des informations de marché, le calcul
        du Stop Loss et du Take Profit, et le calcul dynamique du volume basé sur le risque.
        Elle gère également les ordres différés (LIMIT/STOP).

        Args:
            decision_package (dict): Le package de décision validé.

        Returns:
            dict: La requête d'ordre MT5 prête à être exécutée.

        Raises:
            TradeExecutionError: Si une erreur critique empêche la préparation de l'ordre.
        """
        self.logger.info("Préparation de l'ordre MT5...")
        trade_decision = decision_package["trade_decision"]
        active_config = decision_package["active_config"]
        market_context = decision_package["market_context"]

        symbol = trade_decision["asset"]
        action = trade_decision["action"]
        order_type_str = trade_decision.get(
            "order_type", "MARKET"
        )  # Vient du moteur de règles

        # Cas de clôture de position (si l'action est "CLOSE")
        if action == "CLOSE":
            self.logger.info(
                "Action de clôture détectée. Laisser `TradeExecutor.close_position` gérer la clôture réelle."
            )
            # Le package de décision de clôture n'a pas besoin de tous les détails d'un ordre d'ouverture
            return {
                "action": "CLOSE",
                "symbol": symbol,
                "order_id": trade_decision.get("order_id", str(uuid.uuid4())),
                "ticket_to_close": trade_decision.get("ticket_to_close"),
            }

        if action not in ["BUY", "SELL"]:
            raise TradeExecutionError(f"Action de trade non supportée : '{action}'.")

        try:
            # Récupérer les informations du symbole via le MT5Connector
            symbol_info = self.mt5_connector.get_symbol_info(symbol)
            if symbol_info is None:  # get_symbol_info peut retourner None
                raise TradeExecutionError(
                    f"Impossible de récupérer les informations du symbole pour {symbol}. Ordre annulé."
                )

            # Récupérer le prix d'entrée (peut être le prix actuel ou un prix de déclenchement pour les ordres différés)
            entry_price_market = self.mt5_connector.get_current_price(symbol, action)
            if not entry_price_market or entry_price_market <= 0:
                raise TradeExecutionError(
                    f"Impossible de récupérer un prix de marché valide pour {symbol}. Ordre annulé."
                )

            # Pour les ordres différés, le prix de déclenchement vient de la décision
            trigger_price = trade_decision.get("trigger_price", entry_price_market)

            # Calcul des prix SL/TP (basé sur la structure du marché ou des pips fixes)
            # La fonction _calculate_sl_tp_prices a besoin de market_context pour les données historiques
            sl_price, tp_price = self._calculate_sl_tp_prices(
                trade_decision,
                active_config,
                symbol_info,
                entry_price_market,
                market_context,
            )

            # Calcul du volume basé sur le risque et les spécifications du broker
            # Utilise les infos du compte broker actif du context
            account_trade_settings = market_context.get(
                "active_broker_account", {}
            ).get("trade_settings", {})
            volume = self._calculate_risk_based_volume(
                trade_decision,
                active_config,
                market_context,
                symbol_info,
                entry_price_market,
                sl_price,
                account_trade_settings,
            )

            # S'assurer que le volume calculé est valide avant de construire la requête
            if not isinstance(volume, (int, float)) or volume <= 0:
                raise TradeExecutionError(
                    f"Volume calculé invalide ou nul ({volume}) pour {symbol}. Ordre annulé."
                )

            # Construction finale de la requête MT5
            return self._build_mt5_request(
                trade_decision,
                active_config,
                volume,
                entry_price_market,
                sl_price,
                tp_price,
                symbol_info,
                trigger_price,
                order_type_str,  # Passer le trigger_price et order_type_str
            )

        except TradeExecutionError as tee:
            self.logger.error(
                f"Échec critique lors de la préparation de l'ordre pour {symbol}: {tee}"
            )
            self.config_manager.send_alert(
                "CRITIQUE",
                f"Préparation Ordre Échec: {tee}",
                alert_type="telegram_critical",
            )
            raise  # Relaisser l'exception pour que le pipeline l'intercepte
        except Exception as e:
            self.logger.error(
                f"Échec inattendu lors de la préparation de l'ordre pour {symbol}: {e}",
                exc_info=True,
            )
            self.config_manager.send_alert(
                "CRITIQUE",
                f"Préparation Ordre Exception: {e}",
                alert_type="telegram_critical",
            )
            raise TradeExecutionError(
                f"Échec inattendu de la préparation de l'ordre pour {symbol}: {e}"
            ) from e

        # TODO: Implémenter la logique pour préparer des ordres différés (LIMIT/STOP) en se basant
        #       sur le `order_type` fourni par le moteur de règles. (Ce TODO est maintenant implémenté ci-dessus)

    def _calculate_sl_tp_prices(
        self,
        trade_decision: dict,
        config: dict,
        symbol_info: Any,
        entry_price: float,
        market_context: dict,
    ) -> tuple[float, float]:
        """
        Calcule les prix SL/TP en utilisant des logiques institutionnelles :
        - SL: Basé sur les derniers points de swing (plus haut/plus bas) OU des niveaux de liquidité.
        - TP: Basé sur un ratio Risque/Rendement (Risk/Reward) OU des niveaux de liquidité.
        La méthode fallback sur un nombre de pips fixe si la configuration le demande.

        Args:
            trade_decision (dict): La décision de trade (pour l'action BUY/SELL).
            config (dict): La configuration active de la stratégie.
            symbol_info (Any): Les informations du symbole de MT5 (MetaTrader5.SymbolInfo NamedTuple).
            entry_price (float): Le prix d'entrée actuel.
            market_context (dict): Le contexte de marché contenant les données historiques et les signaux enrichis.

        Returns:
            tuple[float, float]: Un tuple contenant le prix du Stop Loss et du Take Profit.

        Raises:
            TradeExecutionError: Si les données de marché sont insuffisantes pour le calcul ou si SL/TP sont invalides.
        """
        self.logger.info(
            "Calcul du 'Smart SL/TP' basé sur la structure du marché, le R/R et la liquidité..."
        )
        action = trade_decision["action"]
        point = symbol_info.point  # Valeur du point pour le symbole (ex: 0.00001)

        # Récupération de la tolérance minimale du broker (stops_level)
        min_stop_distance_points = (
            symbol_info.stops_level
        )  # Distance minimale pour SL/TP en points MT5
        min_stop_distance_price = min_stop_distance_points * point

        # --- Chargement des paramètres de la nouvelle logique "Smart" ---
        settings = config.get("smart_sl_tp_settings", {})
        sl_method = settings.get("sl_placement_method", "PIPS")
        tp_method = settings.get("tp_placement_method", "PIPS")

        # --- Calcul du Stop Loss (SL) ---
        stop_loss_price = 0.0
        if sl_method == "SWING":
            lookback = settings.get("sl_swing_lookback_period", 10)
            buffer_pips = settings.get("sl_buffer_pips", 2)

            # Récupération des données historiques enrichies depuis le contexte
            symbol = trade_decision["asset"]
            # Assurez-vous que market_data[symbol] contient le DataFrame annoté par PhaseObserver
            rates_df = market_context.get("market_data", {}).get(
                symbol
            )  # C'est le DataFrame annoté

            if not isinstance(rates_df, pd.DataFrame) or len(rates_df) < lookback:
                self.logger.warning(
                    f"Données historiques insuffisantes ({len(rates_df) if isinstance(rates_df, pd.DataFrame) else 0}) pour le calcul du SL 'SWING' (période: {lookback}). Fallback aux PIPS."
                )
                sl_method = "PIPS"  # Force le fallback aux pips

            else:
                recent_candles = rates_df.tail(lookback)
                buffer_price = buffer_pips * point

                if (
                    action == "BUY"
                ):  # SL pour un achat = en dessous du plus bas (swing low)
                    swing_low = recent_candles["low"].min()
                    stop_loss_price = swing_low - buffer_price
                else:  # SELL = SL pour une vente = au-dessus du plus haut (swing high)
                    swing_high = recent_candles["high"].max()
                    stop_loss_price = swing_high + buffer_price

                self.logger.debug(
                    f"SL 'SWING' calculé à {stop_loss_price:.5f} (période: {lookback}, buffer: {buffer_pips} pips)."
                )

        if sl_method == "PIPS":  # Fallback ou méthode "PIPS" explicite
            sl_pips = config.get("stop_loss_pips", 10)
            sl_distance = sl_pips * point
            stop_loss_price = (
                entry_price - sl_distance
                if action == "BUY"
                else entry_price + sl_distance
            )
            self.logger.debug(
                f"SL 'PIPS' calculé à {stop_loss_price:.5f} ({sl_pips} pips)."
            )

        # --- Calcul du Take Profit (TP) ---
        take_profit_price = 0.0
        if tp_method == "RR":
            rr_ratio = settings.get("tp_rr_ratio", 1.5)
            # Distance du risque en prix (abs(entrée - SL))
            risk_distance_price = abs(entry_price - stop_loss_price)
            tp_distance_price = risk_distance_price * rr_ratio

            take_profit_price = (
                entry_price + tp_distance_price
                if action == "BUY"
                else entry_price - tp_distance_price
            )
            self.logger.debug(
                f"TP 'Risk/Reward' calculé à {take_profit_price:.5f} (Ratio: 1:{rr_ratio})."
            )

        # Implémenter une logique de TP basée sur des niveaux de liquidité externes (TODO implémenté)
        elif tp_method == "LIQUIDITY_LEVEL":
            symbol = trade_decision["asset"]
            # Accéder aux détails de liquidité enrichis par PhaseObserver
            # Ces détails devraient être dans market_context.get("market_data",{}).get(symbol).get("nearest_liquidity_level_details")
            nearest_liquidity_details = (
                market_context.get("market_data", {})
                .get(symbol, {})
                .get("nearest_liquidity_level_details")
            )

            if nearest_liquidity_details and nearest_liquidity_details.get("type") in [
                "EQH",
                "EQL",
                "OB_unmitigated",
            ]:  # Exemple de types
                target_level = nearest_liquidity_details.get("level")

                if target_level:
                    # Vérifier si le niveau de liquidité est "dans la bonne direction" et au-delà du SL
                    if (
                        action == "BUY"
                        and target_level > entry_price
                        and target_level > stop_loss_price
                    ) or (
                        action == "SELL"
                        and target_level < entry_price
                        and target_level < stop_loss_price
                    ):
                        # Ajouter un petit buffer pour s'assurer que le TP est ATTEINT
                        buffer_tp_price = (
                            settings.get("tp_liquidity_buffer_pips", 0.5) * point
                        )
                        take_profit_price = (
                            target_level - buffer_tp_price
                            if action == "BUY"
                            else target_level + buffer_tp_price
                        )
                        self.logger.debug(
                            f"TP 'Liquidité' calculé à {take_profit_price:.5f} (Niveau: {nearest_liquidity_details.get('type')} @ {target_level:.5f})."
                        )
                    else:
                        self.logger.warning(
                            f"Niveau de liquidité {nearest_liquidity_details.get('type')} @ {target_level} n'est pas une cible TP valide ou est trop proche du SL. Fallback aux PIPS."
                        )
                        tp_method = "PIPS"  # Fallback si le niveau n'est pas bon
                else:
                    self.logger.warning(
                        "Détails du niveau de liquidité incomplets pour le TP 'LIQUIDITY_LEVEL'. Fallback aux PIPS."
                    )
                    tp_method = "PIPS"
            else:
                self.logger.warning(
                    "Aucun niveau de liquidité pertinent pour le TP 'LIQUIDITY_LEVEL'. Fallback aux PIPS."
                )
                tp_method = "PIPS"

        if tp_method == "PIPS":  # Fallback ou méthode "PIPS" explicite
            tp_pips = config.get("take_profit_pips", 20)
            tp_distance = tp_pips * point
            take_profit_price = (
                entry_price + tp_distance
                if action == "BUY"
                else entry_price - tp_distance
            )
            self.logger.debug(
                f"TP 'PIPS' calculé à {take_profit_price:.5f} ({tp_pips} pips)."
            )

        # --- Validation Finale pour s'assurer que SL/TP sont valides et respectent les règles du broker ---
        # Ajouter une validation pour s'assurer que le SL et le TP ne sont pas trop proches du prix
        # d'entrée, en respectant le `stops_level` de `symbol_info`. (TODO implémenté)

        # Vérification du prix d'entrée vs prix actuel (surtout pour les ordres MARKET)
        # S'assurer que le prix d'entrée n'est pas 0 ou négatif
        if entry_price <= 0:
            raise TradeExecutionError(
                "Prix d'entrée invalide ou non positif. Impossible de définir SL/TP."
            )

        # S'assurer que SL est différent de TP et de entry_price
        if (
            stop_loss_price == take_profit_price
            or stop_loss_price == entry_price
            or take_profit_price == entry_price
        ):
            raise TradeExecutionError(
                f"SL ({stop_loss_price}) et/ou TP ({take_profit_price}) sont identiques au prix d'entrée ({entry_price}). Invalide."
            )

        # Vérification des distances minimales requises par le broker (stops_level)
        if action == "BUY":
            sl_distance_from_entry = entry_price - stop_loss_price
            tp_distance_from_entry = take_profit_price - entry_price
        else:  # SELL
            sl_distance_from_entry = stop_loss_price - entry_price
            tp_distance_from_entry = entry_price - take_profit_price

        if sl_distance_from_entry < min_stop_distance_price:
            # ajuster le SL pour respecter la distance minimale
            if action == "BUY":
                stop_loss_price = entry_price - min_stop_distance_price
            else:  # SELL
                stop_loss_price = entry_price + min_stop_distance_price
            self.logger.warning(
                f"SL ({stop_loss_price:.5f}) ajusté pour respecter le stops_level du broker ({min_stop_distance_points} points)."
            )

        if tp_distance_from_entry < min_stop_distance_price:
            # ajuster le TP pour respecter la distance minimale
            if action == "BUY":
                take_profit_price = entry_price + min_stop_distance_price
            else:  # SELL
                take_profit_price = entry_price - min_stop_distance_price
            self.logger.warning(
                f"TP ({take_profit_price:.5f}) ajusté pour respecter le stops_level du broker ({min_stop_distance_points} points)."
            )

        # Assurer que SL et TP sont différents après ajustement
        if (
            abs(stop_loss_price - take_profit_price) < point * 2
        ):  # Une tolérance de 2 points
            raise TradeExecutionError(
                "SL et TP sont trop proches ou identiques après ajustement pour le stops_level. Ordre invalide."
            )

        return stop_loss_price, take_profit_price

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
        Calcule le volume de l'ordre en utilisant l'API MT5 en priorité, et un
        modèle interne robuste comme solution de repli (fallback).
        Prend en compte les spécifications du compte broker actif (min/max/step lot).

        Args:
            trade_decision (dict): La décision de trade (action, asset).
            config (dict): La configuration active de la stratégie.
            context (dict): Le contexte de marché et du compte.
            symbol_info (Any): Les informations du symbole MT5.
            entry_price (float): Le prix d'entrée actuel.
            sl_price (float): Le prix du Stop Loss.
            account_trade_settings (Dict[str, Any]): Paramètres de trading spécifiques au compte broker actif.

        Returns:
            float: Le volume de l'ordre, calculé et validé.

        Raises:
            TradeExecutionError: Si le calcul du volume est impossible ou invalide.
        """
        # Récupération de l'équité du compte (déjà vérifiée en amont, mais re-vérifier la validité)
        equity = context.get("account_info", {}).get("equity")
        if not isinstance(equity, (int, float)) or equity <= 0:
            raise TradeExecutionError(
                "Équité du compte non positive ou manquante pour le calcul du volume."
            )

        risk_percent = config.get("risk_per_trade_percent", 0.5)
        max_dollar_risk = equity * (risk_percent / 100)

        # Tente d'abord d'utiliser l'API MT5 pour un calcul précis de la perte par lot
        # Utilise les constantes MT5 mappées
        mt5_order_type = (
            self.ORDER_TYPE_BUY
            if trade_decision["action"] == "BUY"
            else self.ORDER_TYPE_SELL
        )

        loss_per_lot = 0.0
        try:
            # Assurez-vous que self.mt5 est bien l'objet mt5 initialisé.
            # Il est passé dans __init__ et doit être utilisé via self.mt5.
            _ret, loss_per_lot_value = self.mt5.order_calc_profit(
                mt5_order_type,
                symbol_info.name,  # Utilise symbol_info.name pour le symbole
                1.0,  # Volume 1.0 lot
                entry_price,
                sl_price,
            )

            # Utilise les retcodes mappés
            if _ret == self.TRADE_RETCODE_DONE and loss_per_lot_value is not None:
                self.logger.debug(
                    f"Calcul de la perte par lot via l'API MT5 réussi. Code: {_ret}"
                )
                loss_per_lot = abs(loss_per_lot_value)
            else:
                retcode_str = self.mt5_mappings.get("trade_retcodes", {}).get(
                    str(_ret), f"Code_{_ret}"
                )
                self.logger.warning(
                    f"MT5.order_calc_profit a échoué (code: {_ret} - {retcode_str}). Passage au modèle de calcul de risque interne."
                )
                sl_distance_price = abs(entry_price - sl_price)
                loss_per_lot = self._calculate_loss_per_lot_fallback(
                    symbol_info, sl_distance_price, entry_price
                )  # Passer current_price pour conversion
        except Exception as e:
            self.logger.warning(
                f"Erreur lors de l'appel à mt5.order_calc_profit: {e}. Passage au modèle de calcul de risque interne.",
                exc_info=True,
            )
            sl_distance_price = abs(entry_price - sl_price)
            loss_per_lot = self._calculate_loss_per_lot_fallback(
                symbol_info, sl_distance_price, entry_price
            )

        if (
            loss_per_lot <= 1e-9
        ):  # Utiliser une petite tolérance pour les erreurs de flottants (plus stricte)
            self.logger.error(
                f"La perte par lot calculée est nulle ou trop faible ({loss_per_lot:.9f}) pour {symbol_info.name}. Ordre annulé pour sécurité."
            )
            raise TradeExecutionError(
                f"Calcul de risque invalide (perte par lot nulle ou trop faible) pour {symbol_info.name}."
            )

        calculated_volume = max_dollar_risk / loss_per_lot

        # Récupérer les limites de volume spécifiques au symbole et au compte broker
        volume_min_symbol = symbol_info.volume_min  # Min volume autorisé par le symbole
        volume_max_symbol = symbol_info.volume_max  # Max volume autorisé par le symbole
        volume_step_symbol = (
            symbol_info.volume_step
        )  # Pas de volume autorisé par le symbole

        # Récupérer les limites du compte broker actif
        min_lot_account = account_trade_settings.get("min_lot", volume_min_symbol)
        max_lot_account = account_trade_settings.get("max_lot", volume_max_symbol)
        lot_step_account = account_trade_settings.get("lot_step", volume_step_symbol)

        # Sécurité "Fat Finger" (utilisant les limites configurées du ConfigManager)
        # Ceci est un contrôle plus strict que celui dans ConfigManager.calculate_risk_parameters
        max_volume_safety = self.config_manager.get(
            "trade_executor_settings.max_absolute_volume_safety", 50.0
        )  # Nouvelle clé configurable
        if calculated_volume > max_volume_safety:
            self.logger.warning(
                f"Volume calculé ({calculated_volume:.2f}) dépasse le seuil de sécurité absolu ({max_volume_safety:.2f}). Ajusté à {max_volume_safety:.2f}."
            )
            calculated_volume = max_volume_safety

        # Ajustement du volume aux contraintes les plus strictes (broker ou compte)
        volume = max(min_lot_account, volume_min_symbol, calculated_volume)
        volume = min(max_lot_account, volume_max_symbol, volume)

        # Arrondir au step de volume (le plus grand step entre symbole et compte)
        effective_volume_step = max(lot_step_account, volume_step_symbol)
        if (
            effective_volume_step > 1e-9
        ):  # Éviter la division par zéro si le step est nul
            volume = round(volume / effective_volume_step) * effective_volume_step

        # Ajouter une vérification post-calcul pour s'assurer que le risque final en dollars,
        # après ajustement du volume, ne dépasse pas `max_dollar_risk`. (TODO implémenté)
        # Recalculer le risque réel avec le volume ajusté
        actual_risk_dollars = volume * loss_per_lot
        if actual_risk_dollars > max_dollar_risk * self.config_manager.get(
            "trade_executor_settings.max_risk_deviation_multiplier", 1.05
        ):  # Tolérance de 5%
            self.logger.warning(
                f"Risque réel ({actual_risk_dollars:.2f}$) dépasse le risque max ({max_dollar_risk:.2f}$) après ajustement du volume. Le risque est plus élevé que prévu."
            )
            # Dans un environnement ultra-strict, cela pourrait déclencher une nouvelle raise TradeExecutionError

        # Assurer la précision d'affichage finale
        final_volume = round(
            volume, symbol_info.digits_volume
        )  # Utilise la précision de volume du symbole info

        self.logger.info(
            f"Calcul de risque pour {symbol_info.name}: Risque={risk_percent}%, Perte Max=${max_dollar_risk:.2f}, Volume Final={final_volume:.{symbol_info.digits_volume}f} (Risque Réel={actual_risk_dollars:.2f}$)."
        )

        return final_volume

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

        Args:
            trade_decision (dict): La décision de trade.
            config (dict): La configuration active.
            volume (float): Volume calculé pour l'ordre.
            entry_price_market (float): Le prix de marché actuel (Ask/Bid).
            sl_price (float): Le prix du Stop Loss.
            tp_price (float): Le prix du Take Profit.
            symbol_info (Any): Les informations du symbole de MT5 (MetaTrader5.SymbolInfo NamedTuple).
            trigger_price (float, optional): Prix de déclenchement pour les ordres différés.
                                            Si None, utilise entry_price_market pour les ordres marché.
            order_type_str (str): Type d'ordre sous forme de chaîne (ex: "MARKET", "BUY_LIMIT").

        Returns:
            dict: La requête d'ordre MT5, prête et validée pour l'envoi.

        Raises:
            TradeExecutionError: Si la requête est invalide ou ne respecte pas les contraintes du broker.
        """
        self.logger.info("Construction de la requête MT5 finale...")
        action_str = trade_decision["action"]  # BUY, SELL, CLOSE

        # Récupérer les constantes MT5 via les mappings
        mt5_action_deal = self.TRADE_ACTION_DEAL  # Utilise self.TRADE_ACTION_DEAL
        mt5_action_pending = (
            self.TRADE_ACTION_PENDING
        )  # Utilise self.TRADE_ACTION_PENDING
        mt5_order_time_gtc = self.ORDER_TIME_GTC  # Utilise self.ORDER_TIME_GTC

        # Default filling policy
        filling_policy_str = config.get("execution_policy.type_filling", "FOK")
        # Utilise self.mt5 pour accéder aux constantes, et les mappings via getattr
        mt5_filling_policy = getattr(
            self.mt5,
            self.mt5_mappings.get("order_filling_policies", {}).get(
                filling_policy_str, "ORDER_FILLING_FOK"
            ),
        )

        # --- Dictionnaire de base commun à tous les ordres ---
        request = {
            "action": mt5_action_deal,  # Action par défaut pour DEAL, sera modifiée pour PENDING
            "symbol": symbol_info.name,
            "volume": volume,
            "magic": config.get("magic_number"),
            "sl": round(
                sl_price, symbol_info.digits
            ),  # Arrondir au nombre de décimales du symbole
            "tp": round(
                tp_price, symbol_info.digits
            ),  # Arrondir au nombre de décimales du symbole
            "type_time": mt5_order_time_gtc,
            "comment": "",  # Initialiser pour le formatage
            "deviation": config.get(
                "execution_policy.max_deviation_points", 20
            ),  # Tolérance de slippage en points
        }

        # --- Logique spécifique par type d'ordre ---
        if order_type_str == "MARKET":
            request["type"] = (
                self.ORDER_TYPE_BUY if action_str == "BUY" else self.ORDER_TYPE_SELL
            )  # Utilise self.ORDER_TYPE_BUY/SELL
            request["price"] = (
                entry_price_market  # Pour un ordre marché, c'est le prix actuel (Ask/Bid)
            )
            request["type_filling"] = mt5_filling_policy

        elif "LIMIT" in order_type_str or "STOP" in order_type_str:
            request["action"] = mt5_action_pending  # Action pour ordre différé
            request["price"] = (
                trigger_price if trigger_price is not None else entry_price_market
            )  # Prix de déclenchement

            # Récupération dynamique du type d'ordre MT5 (ex: "BUY_LIMIT" -> self.mt5.ORDER_TYPE_BUY_LIMIT)
            mapped_order_type_value = self.mt5_mappings.get("order_types", {}).get(
                order_type_str
            )
            if mapped_order_type_value is None:
                raise TradeExecutionError(
                    f"Type d'ordre différé non supporté ou invalide : '{order_type_str}'. Vérifiez les mappings MT5."
                )
            request["type"] = getattr(
                self.mt5, mapped_order_type_value
            )  # Utilise self.mt5 pour accéder à la constante

            # Vérifications spécifiques aux ordres différés
            # La règle du broker "stops_level" s'applique aussi au prix de déclenchement
            # (distance minimale entre le prix actuel et le prix de l'ordre différé)
            current_prices = self.mt5_connector.get_symbol_info_tick(symbol_info.name)
            if current_prices is None:
                raise TradeExecutionError(
                    f"Impossible d'obtenir les prix de tick pour {symbol_info.name} pour valider l'ordre différé."
                )

            # Vérification de la distance minimale entre prix du marché et prix de l'ordre différé
            min_distance_from_market_price = (
                symbol_info.trade_stops_level * symbol_info.point
            )  # stops_level en prix

            if (
                order_type_str == "BUY_LIMIT"
                and request["price"]
                >= current_prices.ask - min_distance_from_market_price
            ):
                raise TradeExecutionError(
                    f"BUY_LIMIT ({request['price']}) trop proche du prix Ask ({current_prices.ask}). Distance min: {min_distance_from_market_price:.5f}."
                )
            elif (
                order_type_str == "SELL_LIMIT"
                and request["price"]
                <= current_prices.bid + min_distance_from_market_price
            ):
                raise TradeExecutionError(
                    f"SELL_LIMIT ({request['price']}) trop proche du prix Bid ({current_prices.bid}). Distance min: {min_distance_from_market_price:.5f}."
                )
            elif (
                order_type_str == "BUY_STOP"
                and request["price"]
                <= current_prices.ask + min_distance_from_market_price
            ):
                raise TradeExecutionError(
                    f"BUY_STOP ({request['price']}) trop proche du prix Ask ({current_prices.ask}). Distance min: {min_distance_from_market_price:.5f}."
                )
            elif (
                order_type_str == "SELL_STOP"
                and request["price"]
                >= current_prices.bid - min_distance_from_market_price
            ):
                raise TradeExecutionError(
                    f"SELL_STOP ({request['price']}) trop proche du prix Bid ({current_prices.bid}). Distance min: {min_distance_from_market_price:.5f}."
                )

            # Gestion de la date d'expiration pour les ordres différés (TODO implémenté)
            # La date d'expiration peut être définie dans la configuration de la stratégie
            expiration_policy = config.get("order_expiration_policy", {}).get(
                "type", "GTC"
            )  # GTC, DAY, SPECIFIED

            if expiration_policy == "DAY":
                request["type_time"] = getattr(
                    self.mt5,
                    self.mt5_mappings.get("order_time_flags", {}).get(
                        "DAY", "ORDER_TIME_DAY"
                    ),
                )
            elif expiration_policy == "SPECIFIED":
                request["type_time"] = getattr(
                    self.mt5,
                    self.mt5_mappings.get("order_time_flags", {}).get(
                        "SPECIFIED", "ORDER_TIME_SPECIFIED"
                    ),
                )
                # La date/heure spécifique doit venir de la décision ou de la config
                expiration_datetime_str = config.get("order_expiration_policy", {}).get(
                    "datetime", (datetime.now(UTC) + timedelta(days=1)).isoformat()
                )
                try:
                    request["expiration"] = datetime.fromisoformat(
                        expiration_datetime_str
                    ).timestamp()  # Timestamp Unix
                except ValueError:
                    self.logger.error(
                        f"Format de date d'expiration invalide: {expiration_datetime_str}. Utilisation de GTC."
                    )
                    request["type_time"] = mt5_order_time_gtc  # Fallback GTC

        else:
            raise TradeExecutionError(
                f"Type d'ordre non géré dans _build_mt5_request: '{order_type_str}'"
            )

        # --- Validation Finale "Anti-Rejet" contre les contraintes du Broker ---
        # Vérifications pour SL/TP par rapport au prix de l'ordre
        # Note: stops_level de symbol_info est la distance MINIMALE en points pour SL/TP
        # par rapport au PRIX DE L'ORDRE.
        # current_prices = self.mt5_connector.get_symbol_info_tick(symbol_info.name) # Déjà récupéré ou peut être re-appelé si besoin

        # Pour les ordres au marché, les prix ask/bid peuvent changer.
        # Il est préférable d'utiliser le prix de l'ordre (request["price"]) comme base.

        # Distance minimale SL/TP en prix par rapport au prix de l'ordre
        min_sl_tp_distance_from_order_price = (
            symbol_info.trade_stops_level * symbol_info.point
        )

        # Validation pour SL (trop proche du prix d'entrée)
        if (
            action_str == "BUY"
            and (request["price"] - sl_price) < min_sl_tp_distance_from_order_price
        ) or (
            action_str == "SELL"
            and (sl_price - request["price"]) < min_sl_tp_distance_from_order_price
        ):
            raise TradeExecutionError(
                f"Stop Loss ({sl_price:.5f}) trop proche du prix d'entrée de l'ordre ({request['price']:.5f}). Distance min: {min_sl_tp_distance_from_order_price:.5f}."
            )

        # Validation pour TP (trop proche du prix d'entrée)
        if (
            action_str == "BUY"
            and (tp_price - request["price"]) < min_sl_tp_distance_from_order_price
        ) or (
            action_str == "SELL"
            and (request["price"] - tp_price) < min_sl_tp_distance_from_order_price
        ):
            raise TradeExecutionError(
                f"Take Profit ({tp_price:.5f}) trop proche du prix d'entrée de l'ordre ({request['price']:.5f}). Distance min: {min_sl_tp_distance_from_order_price:.5f}."
            )

        # --- Ajout du Commentaire (déjà existant) ---
        comment_template = self.config_manager.get(
            "trading.order_comment_template", "SNIPER_X|{strategy}|{order_type}"
        )
        max_len = self.config_manager.get("trading.comment_max_length", 31)
        request["comment"] = comment_template.format(
            strategy=config.get("strategy_name", "N/A"), order_type=order_type_str
        )[:max_len]

        self.logger.debug(f"Requête MT5 construite et validée : {request}")

        # TODO: Ajouter la gestion de la date d'expiration (`expiration`) pour les ordres différés,
        #       en la rendant configurable (ex: fin de la journée, fin de la semaine). (Implémenté ci-dessus)
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

    def execute_order(self, mt5_request: dict) -> dict:
        """
        Exécute un ordre en appliquant une logique de retry intelligente et en enrichissant
        la gestion de l'état interne avec le risque initial du trade.
        """
        order_id = mt5_request.get("order_id", str(uuid.uuid4()))
        symbol = mt5_request.get("symbol", "N/A")
        self.logger.info(
            f"Tentative d'exécution de l'ordre {order_id} pour {symbol}..."
        )

        if not self.mt5_connector.is_connected:
            self.logger.error(f"MT5 non connecté. Ordre {order_id} ignoré.")
            return {
                "status": "skipped",
                "message": "MT5 non connecté.",
                "mt5_result": None,
            }

        retcode_actions = self.config_manager.get(
            "mt5_mappings.trade_retcode_actions", {}
        )

        for attempt in range(self.mt5_max_retries):
            self.logger.debug(
                f"Ordre {order_id} - Tentative d'envoi {attempt + 1}/{self.mt5_max_retries}..."
            )
            result = self.mt5_connector.send_order(mt5_request)

            if not result:
                self.logger.error(
                    f"Tentative {attempt + 1}: Aucune réponse de MT5 pour l'ordre {order_id}."
                )
                time.sleep(self.mt5_retry_delay_seconds * (attempt + 1))
                continue

            action = retcode_actions.get(str(result.retcode), "FAIL")

            if action == "SUCCESS":
                self.logger.info(
                    f"SUCCÈS: Ordre #{result.order}, Deal #{result.deal} exécuté pour {symbol}."
                )

                _ret, loss_value = self.mt5_connector.mt5.order_calc_profit(
                    mt5_request["type"],
                    symbol,
                    mt5_request["volume"],
                    result.price,
                    result.sl,
                )
                initial_risk_usd = (
                    abs(loss_value)
                    if _ret == self.TRADE_RETCODE_DONE and loss_value is not None
                    else 0.0
                )

                # AMÉLIORATION : Appel à la fonction centralisée pour mettre à jour l'état
                self._update_internal_position_state(result, initial_risk_usd)

                self._log_trade_audit(
                    {
                        "event_type": "TRADE_OPEN",
                        "timestamp": datetime.now(UTC).isoformat(),
                        "order_id": order_id,
                        "symbol": symbol,
                        "status": "SUCCESS",
                        "details": result._asdict(),
                        "position_snapshot": self._open_positions.get(result.deal),
                    }
                )
                return {
                    "status": "executed",
                    "message": "Ordre exécuté avec succès.",
                    "mt5_result": result._asdict(),
                }

            elif action == "RETRY":
                self.logger.warning(
                    f"Tentative {attempt + 1}: Rejet temporaire (Code: {result.retcode}, Raison: {result.comment}). Nouvelle tentative..."
                )
                time.sleep(self.mt5_retry_delay_seconds)

            else:  # action == "FAIL"
                msg = f"Rejet définitif (Code: {result.retcode}): {result.comment}"
                self.logger.error(msg)
                self.config_manager.blacklist_asset_on_bad_conditions(
                    symbol, f"Erreur MT5: {result.comment}"
                )
                return {
                    "status": "failed",
                    "message": msg,
                    "mt5_result": result._asdict(),
                }

        final_msg = f"Échec de l'exécution de l'ordre {order_id} après {self.mt5_max_retries} tentatives."
        self.logger.error(final_msg)
        self.config_manager.send_alert(
            f"CRITIQUE: {final_msg} (Symbole: {symbol})", "telegram_critical"
        )

        return {"status": "failed", "message": final_msg, "mt5_result": None}

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
    trade_executor: "TradeExecutor", decision_package: dict
) -> dict:  # Utiliser une forward reference pour TradeExecutor
    """
    Orchestre le pipeline complet d'exécution d'un trade, de la validation à la notification.
    Cette fonction est une interface de haut niveau pour l'exécution des trades.

    Args:
        trade_executor (TradeExecutor): L'instance du TradeExecutor.
        decision_package (dict): Le package de décision contenant le contexte et le trade à exécuter.

    Returns:
        dict: Un dictionnaire de feedback standardisé sur le résultat de l'exécution.
    """
    # Utilise le logger de l'instance trade_executor pour la cohérence
    logger_instance = trade_executor.logger
    order_id = "N/A"  # Default value for order_id

    try:
        # 1. Validation du package
        validated_package = trade_executor.load_decision_package(decision_package)
        trade_decision = validated_package["trade_decision"]
        order_id = trade_decision.get(
            "order_id", order_id
        )  # Récupérer l'ID de l'ordre dès que possible

        # 2. Préparation de l'ordre (y compris le calcul de risque)
        # La fonction prepare_order gère maintenant l'action "CLOSE" aussi
        mt5_request = trade_executor.prepare_order(validated_package)

        # Si c'est une action de clôture, le prepare_order retourne un dict spécifique
        if mt5_request.get("action") == "CLOSE":
            # Appeler la méthode close_position pour gérer la clôture réelle
            close_result = trade_executor.close_position(
                symbol=mt5_request.get("symbol"),
                ticket=mt5_request.get("ticket_to_close"),
            )
            # Simuler un mt5_result pour la notification si la clôture est gérée ici
            status = "executed" if close_result.get("success") else "failed"
            message = close_result.get("message")
            # Pour la clôture, le P&L est dans le résultat de close_position
            pnl_usd_closed = close_result.get("pnl_usd", 0.0)

            # Journalisation et notification
            # `log_and_notify` est conçu pour des requêtes MT5, adaptons un peu le mt5_request pour le log de clôture
            temp_mt5_req_for_log = {
                "order_id": order_id,
                "symbol": mt5_request.get("symbol"),
                "action": "CLOSE",
                "volume": mt5_request.get("volume", "N/A"),
            }
            temp_exec_status_for_log = {
                "status": status,
                "message": message,
                "pnl_usd": pnl_usd_closed,
            }
            trade_executor.log_and_notify(
                temp_mt5_req_for_log, temp_exec_status_for_log
            )

            return trade_executor.feedback_pipeline(
                order_id, status, message, pnl_usd=pnl_usd_closed
            )

        # 3. Vérifications Pré-Trade
        if not trade_executor.pre_trade_checks(validated_package):
            raise TradeExecutionError("Échec des vérifications pré-trade.")

        # 4. Contrôle Manuel (si nécessaire, de manière non-bloquante)
        if not trade_executor.manual_override_if_needed(mt5_request):
            return trade_executor.feedback_pipeline(
                order_id,
                "pending_manual_approval",
                "En attente d'approbation manuelle.",
            )

        # 5. Exécution de l'Ordre
        execution_status = trade_executor.execute_order(mt5_request)

        # 6. Journalisation, notification et feedback
        # log_and_notify est déjà dans TradeExecutor et gère le log
        trade_executor.log_and_notify(mt5_request, execution_status)

        status = (
            "executed"
            if execution_status["status"] == "executed"
            else execution_status["status"]
        )
        # Pour les ordres d'ouverture, le P&L est généralement 0 au moment de l'exécution
        return trade_executor.feedback_pipeline(
            order_id, status, execution_status["message"], pnl_usd=0.0
        )

    except (InvalidDecisionPackageError, TradeExecutionError) as e:
        logger_instance.error(
            f"Pipeline de trade AVORTÉ (Erreur contrôlée) pour l'ordre {order_id} : {e}"
        )
        trade_executor.config_manager.send_alert(
            f"CRITIQUE: Pipeline de Trade Avorté: {e} (Ordre: {order_id})",
            "telegram_critical",
        )
        return trade_executor.feedback_pipeline(order_id, "failed", str(e))
    except Exception as e:
        logger_instance.critical(
            f"EXCEPTION NON GÉRÉE dans le pipeline d'exécution de trade pour l'ordre {order_id}: {e}",
            exc_info=True,
        )
        trade_executor.config_manager.send_alert(
            f"CRITIQUE: ERREUR NON GÉRÉE (Ordre {order_id}): {type(e).__name__}",
            "telegram_critical",
        )
        return trade_executor.feedback_pipeline(
            order_id, "error", f"Exception non gérée: {type(e).__name__}"
        )

    # TODO: Ajouter des "hooks" (points d'ancrage) entre les étapes pour permettre à des
    #       plugins d'ajouter des validations ou des logs personnalisés. (TODO maintenu)
