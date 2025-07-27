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
        self.enable_momentum_fade_exit = self.strategy_config.get("strategy_toggles", {}).get("enable_momentum_fade_exit", True)
        self.momentum_fade_threshold = self.strategy_config.get("smart_targets", {}).get("momentum_fade_threshold", 0.1)
        self.logger.info("Stratégie de Scalping initialisée avec gestion de sortie par momentum.")

    def evaluate_entry(self, context: Dict[str, Any], signals: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Évalue les entrées en appliquant les 'decision_rules' de la configuration
        aux signaux à haute fréquence fournis par le PhaseObserver.
        """
        self.logger.debug("Évaluation des entrées pour la stratégie de Scalping...")
        
        tradeable_assets = self.strategy_config.get("tradeable_assets", [])
        decision_rules = self.strategy_config.get("decision_rules", [])

        for asset in tradeable_assets:
            asset_signals = signals.get(asset)
            if not asset_signals or not asset_signals.get('is_liquid', False):
                continue

            for rule in sorted(decision_rules, key=lambda r: r.get('priority', 99)):
                if self._check_rule_conditions(asset_signals, rule.get("conditions", {})):
                    action_logic = rule.get("action_logic", {})
                    current_phase = asset_signals.get('phase', '')
                    action_to_take = None

                    if "BUY" in action_logic and action_logic["BUY"] in current_phase:
                        action_to_take = "BUY"
                    elif "SELL" in action_logic and action_logic["SELL"] in current_phase:
                        action_to_take = "SELL"
                    
                    if action_to_take:
                        self.logger.info(f"RÈGLE DÉCLENCHÉE: '{rule.get('name')}' pour {action_to_take} sur {asset}.")
                        
                        return {
                            "action": action_to_take,
                            "asset": asset,
                            "order_type": rule.get("order_type", "MARKET"),
                            "strategy_type": self.strategy_config.get("strategy_name"),
                            "rule_name": rule.get('name'),
                            "magic_number": self.strategy_config.get("magic_number"),
                            "target_tp_pips": self.strategy_config.get("take_profit_pips"),
                            "target_sl_pips": self.strategy_config.get("stop_loss_pips")
                        }
        return None

    def _check_rule_conditions(self, asset_signals: Dict[str, Any], conditions: Dict[str, Any]) -> bool:
        """Fonction d'aide pour évaluer les conditions d'une règle."""
        confidence_ok = asset_signals.get('confidence_score', 0.0) >= conditions.get('min_confidence', 1.0)
        
        required_phases = conditions.get("phase_must_contain", [])
        current_phase = asset_signals.get('phase', '')
        phase_ok = any(p in current_phase for p in required_phases)

        required_signals = conditions.get("signal_must_contain", [])
        signals_ok = all(asset_signals.get(f'{sig}_detected', asset_signals.get(sig, False)) for sig in required_signals)
        
        # Condition additionnelle spécifique au scalping : le momentum doit être fort
        volume_momentum = asset_signals.get('volume_momentum', 0.0)
        momentum_threshold = conditions.get("min_volume_momentum", 0.5)
        momentum_ok = abs(volume_momentum) >= momentum_threshold

        return confidence_ok and phase_ok and signals_ok and momentum_ok

    def evaluate_exit(self, context: Dict[str, Any], current_positions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Évalue les conditions de sortie pour le scalping, principalement basées sur
        l'affaiblissement du momentum qui a initié le trade.
        """
        self.logger.debug("Évaluation des sorties pour la stratégie de Scalping...")
        exit_decisions = []

        if not self.enable_momentum_fade_exit:
            return []

        for position in current_positions:
            if position.get('magic') != self.strategy_config.get("magic_number"):
                continue

            asset = position.get('symbol')
            asset_signals = context.get('trading_signals', {}).get(asset)
            if not asset_signals:
                continue
            
            is_buy = position.get('type') == 0 # 0 for BUY in MT5
            volume_momentum = asset_signals.get('volume_momentum', 0.0)
            
            # Condition de sortie : Le momentum s'est estompé
            should_exit = False
            if is_buy and volume_momentum < self.momentum_fade_threshold:
                self.logger.info(f"EXIT SIGNAL: Le momentum d'achat s'est estompé pour la position #{position['ticket']} sur {asset}. Clôture.")
                should_exit = True
            elif not is_buy and volume_momentum > -self.momentum_fade_threshold:
                self.logger.info(f"EXIT SIGNAL: Le momentum de vente s'est estompé pour la position #{position['ticket']} sur {asset}. Clôture.")
                should_exit = True

            if should_exit:
                exit_decisions.append({
                    "ticket_to_close": position['ticket'],
                    "reason": "Momentum fade"
                })

        return exit_decisions

    def get_parameters(self) -> Dict[str, Any]:
        """Retourne les paramètres de configuration de la stratégie."""
        return self.strategy_config.copy()

    def update_strategy_parameters(self, new_params: Dict[str, Any]) -> None:
        """Met à jour les paramètres de la stratégie."""
        self.logger.info(f"Mise à jour des paramètres de la stratégie Scalping : {new_params}")
        for key, value in new_params.items():
            if isinstance(value, dict) and isinstance(self.strategy_config.get(key), dict):
                self.strategy_config[key].update(value)
            else:
                self.strategy_config[key] = value
        
        # Recharger les attributs
        self.__init__(self.config_manager, self.strategy_config)