# strategy/liquidity.py

from typing import Dict, Any, List, Optional
from .base_strategy import BaseStrategy

class LiquidityStrategy(BaseStrategy):
    """
    Moteur de stratégie pour le modèle 'Liquidity'.
    Exécute les règles de sa configuration pour cibler les zones de liquidité.
    """

    def __init__(self, config_manager_instance: Any, strategy_config: Dict[str, Any]):
        super().__init__(config_manager_instance, strategy_config)
        self.logger.info("Moteur de stratégie Liquidity initialisé.")

    def evaluate_entry(self, context: Dict[str, Any], signals: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Évalue les entrées en appliquant les 'decision_rules' de la configuration.
        """
        tradeable_assets = self.strategy_config.get("tradeable_assets", [])
        decision_rules = self.strategy_config.get("decision_rules", [])

        for asset in tradeable_assets:
            asset_signals = signals.get(asset)
            if not asset_signals or not asset_signals.get('is_liquid', True):
                continue

            for rule in sorted(decision_rules, key=lambda r: r.get('priority', 99)):
                if self._check_rule_conditions(asset_signals, rule.get("conditions", {})):
                    current_phase = asset_signals.get('phase', '')
                    action_logic = rule.get("action_logic", {})
                    action_to_take = None

                    if "BUY" in action_logic and action_logic["BUY"] in current_phase:
                        action_to_take = "BUY"
                    elif "SELL" in action_logic and action_logic["SELL"] in current_phase:
                        action_to_take = "SELL"
                    
                    if action_to_take:
                        self.logger.info(f"RÈGLE D'ENTRÉE LIQUIDITY: '{rule.get('name')}' pour {action_to_take} sur {asset}.")
                        return self._build_decision_package(action_to_take, asset, rule)
        return None

    def evaluate_exit(self, context: Dict[str, Any], current_positions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Évalue les sorties en appliquant les 'exit_rules' de la configuration.
        """
        exit_decisions = []
        exit_rules = self.strategy_config.get("exit_rules", [])

        for position in current_positions:
            if position.get('magic') != self.strategy_config.get("magic_number"):
                continue

            asset = position.get('symbol')
            asset_signals = context.get('trading_signals', {}).get(asset)
            if not asset_signals:
                continue
            
            for rule in exit_rules:
                if self._check_rule_conditions(asset_signals, rule.get("conditions", {})):
                    self.logger.info(f"RÈGLE DE SORTIE LIQUIDITY: '{rule.get('name')}' pour la position #{position['ticket']} sur {asset}.")
                    exit_decisions.append({
                        "ticket_to_close": position['ticket'],
                        "reason": rule.get('name', 'Exit condition met')
                    })
                    break 
        return exit_decisions

    def _build_decision_package(self, action: str, asset: str, rule: Dict[str, Any]) -> Dict[str, Any]:
        """Construit le dictionnaire de décision final."""
        return {
            "action": action,
            "asset": asset,
            "order_type": rule.get("order_type", "MARKET"),
            "strategy_type": self.strategy_config.get("strategy_name"),
            "rule_name": rule.get('name'),
            "magic_number": self.strategy_config.get("magic_number"),
            "target_tp_pips": self.strategy_config.get("take_profit_pips"),
            "target_sl_pips": self.strategy_config.get("stop_loss_pips")
        }
        
    def get_parameters(self) -> Dict[str, Any]:
        """Retourne les paramètres de configuration de la stratégie."""
        return self.strategy_config.copy()

    def update_strategy_parameters(self, new_params: Dict[str, Any]) -> None:
        """Met à jour les paramètres de la stratégie."""
        self.logger.info(f"Mise à jour des paramètres de la stratégie Liquidity : {new_params}")
        self.strategy_config.update(new_params)
        self.__init__(self.config_manager, self.strategy_config)