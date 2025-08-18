# strategy/scalping.py

from typing import Dict, Any, List, Optional
from .base_strategy import BaseStrategy


class ScalpingStrategy(BaseStrategy):
    """
    Stratégie de Scalping améliorée pour SNIPER_X.
    Elle utilise un moteur de règles dynamique et une gestion de sortie proactive
    basée sur l'affaiblissement du momentum.
    """

    def __init__(self, config_manager_instance: Any, strategy_config: Dict[str, Any]):
        """Initialise la stratégie en chargeant les paramètres clés."""
        super().__init__(config_manager_instance, strategy_config)
        # Charger les paramètres spécifiques à la gestion de sortie pour le scalping
        self.enable_momentum_fade_exit = self.strategy_config.get(
            "strategy_toggles", {}
        ).get("enable_momentum_fade_exit", True)
        self.momentum_fade_threshold = self.strategy_config.get(
            "smart_targets", {}
        ).get("momentum_fade_threshold", 0.1)
        self.logger.info(
            "Stratégie de Scalping initialisée avec gestion de sortie par momentum."
        )

    def evaluate_entry(
        self, context: Dict[str, Any], signals: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """
        NOUVELLE LOGIQUE : Fait confiance au DecisionPipeline.
        Trouve le meilleur signal de scalping parmi ceux fournis et prépare l'ordre
        de trade sans re-valider les conditions de marché.
        """
        self.logger.debug(
            "ScalpingStrategy: recherche du meilleur candidat pour l'entrée..."
        )

        best_asset = None
        highest_score = -1.0  # Score combinant confiance et momentum

        tradeable_assets = self.strategy_config.get("tradeable_assets", [])

        for asset in tradeable_assets:
            asset_signals = signals.get(asset)
            if not asset_signals:
                continue

            # Calcul d'un score pour trouver le meilleur candidat pour CETTE stratégie
            confidence = asset_signals.get("confidence_score", 0.0)
            volume_momentum = abs(asset_signals.get("volume_momentum", 0.0))

            current_score = confidence + volume_momentum

            if current_score > highest_score:
                highest_score = current_score
                best_asset = asset

        if best_asset is None:
            self.logger.info(
                "Aucun signal d'actif jugé suffisant pour une entrée de scalping."
            )
            return None

        self.logger.info(
            f"MEILLEUR CANDIDAT TROUVÉ: {best_asset} avec un score de {highest_score:.2f}."
        )

        final_signals = signals[best_asset]
        phase = final_signals.get("phase", "")

        # Détermination de la direction
        action_to_take = None
        if "bullish" in phase or "up" in phase or "expansion_up" in phase:
            action_to_take = "BUY"
        elif "bearish" in phase or "down" in phase or "expansion_down" in phase:
            action_to_take = "SELL"
        else:
            self.logger.warning(
                f"Phase '{phase}' non conclusive pour {best_asset}. Trade annulé."
            )
            return None

        return {
            "action": action_to_take,
            "asset": best_asset,
            "order_type": "MARKET",
            "strategy_type": self.strategy_config.get("strategy_name"),
            "rule_name": f"Scalping entry for {best_asset}",
            "magic_number": self.strategy_config.get("magic_number"),
            "target_tp_pips": self.strategy_config.get("take_profit_pips"),
            "target_sl_pips": self.strategy_config.get("stop_loss_pips"),
        }

        # CETTE MÉTHODE EST SUPPRIMÉE CAR ELLE EST REDONDANTE.
        # Le DecisionPipeline a déjà fait cette analyse.

    def evaluate_exit(
        self, context: Dict[str, Any], current_positions: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Évalue les conditions de sortie pour le scalping, principalement basées sur
        l'affaiblissement du momentum qui a initié le trade.
        """
        self.logger.debug("Évaluation des sorties pour la stratégie de Scalping...")
        exit_decisions = []

        if not self.enable_momentum_fade_exit:
            return []

        for position in current_positions:
            if position.get("magic") != self.strategy_config.get("magic_number"):
                continue

            asset = position.get("symbol")
            asset_signals = context.get("trading_signals", {}).get(asset)
            if not asset_signals:
                continue

            is_buy = position.get("type") == 0  # 0 for BUY in MT5
            volume_momentum = asset_signals.get("volume_momentum", 0.0)

            should_exit = False
            if is_buy and volume_momentum < self.momentum_fade_threshold:
                self.logger.info(
                    f"EXIT SIGNAL: Le momentum d'achat s'est estompé pour la position #{position['ticket']} sur {asset}. Clôture."
                )
                should_exit = True
            elif not is_buy and volume_momentum > -self.momentum_fade_threshold:
                self.logger.info(
                    f"EXIT SIGNAL: Le momentum de vente s'est estompé pour la position #{position['ticket']} sur {asset}. Clôture."
                )
                should_exit = True

            if should_exit:
                exit_decisions.append(
                    {"ticket_to_close": position["ticket"], "reason": "Momentum fade"}
                )

        return exit_decisions

    def get_parameters(self) -> Dict[str, Any]:
        """Retourne les paramètres de configuration de la stratégie."""
        return self.strategy_config.copy()

    def update_strategy_parameters(self, new_params: Dict[str, Any]) -> None:
        """Met à jour les paramètres de la stratégie."""
        self.logger.info(
            f"Mise à jour des paramètres de la stratégie Scalping : {new_params}"
        )
        for key, value in new_params.items():
            if isinstance(value, dict) and isinstance(
                self.strategy_config.get(key), dict
            ):
                self.strategy_config[key].update(value)
            else:
                self.strategy_config[key] = value

        self.__init__(self.config_manager, self.strategy_config)
