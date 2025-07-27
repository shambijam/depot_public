# strategy/crypto.py

from typing import Dict, Any, List, Optional
import numpy as np
from .base_strategy import BaseStrategy

class CryptoStrategy(BaseStrategy):
    """
    Stratégie spécifiquement conçue pour les actifs de cryptomonnaie,
    se concentrant sur la volatilité et les concepts de liquidité.
    Cette version améliorée inclut un calcul de SL/TP dynamique et une logique de sortie proactive.
    """

    def __init__(self, config_manager_instance: Any, strategy_config: Dict[str, Any]):
        """Initialise la stratégie en chargeant les paramètres clés."""
        super().__init__(config_manager_instance, strategy_config)
        # Charger les paramètres de gestion de trade dynamique
        self.risk_reward_ratio = self.strategy_config.get("smart_targets", {}).get("take_profit_multiplier", 2.0)
        self.sl_buffer_pips = self.strategy_config.get("smart_targets", {}).get("sl_buffer_pips", 5)
        self.logger.info("Stratégie Crypto initialisée avec gestion de trade dynamique.")

    def evaluate_entry(self, context: Dict[str, Any], signals: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Évalue les conditions d'entrée en se basant sur les règles de décision.
        Si un setup est valide, calcule un SL/TP dynamique basé sur la structure du marché.
        """
        self.logger.debug("Évaluation des entrées pour la stratégie Crypto...")
        
        tradeable_assets = self.strategy_config.get("tradeable_assets", [])
        decision_rules = self.strategy_config.get("decision_rules", [])

        for asset in tradeable_assets:
            asset_signals = signals.get(asset)
            if not asset_signals or not asset_signals.get('is_liquid', False):
                continue

            for rule in sorted(decision_rules, key=lambda r: r.get('priority', 99)):
                conditions = rule.get("conditions", {})
                
                # La vérification des règles est maintenant dans la classe de base
                if self._check_rule_conditions(asset_signals, conditions):
                    action_logic = rule.get("action_logic", {})
                    current_phase = asset_signals.get('phase', '')
                    action_to_take = None

                    if "BUY" in action_logic and action_logic["BUY"] in current_phase:
                        action_to_take = "BUY"
                    elif "SELL" in action_logic and action_logic["SELL"] in current_phase:
                        action_to_take = "SELL"
                    
                    if action_to_take:
                        self.logger.info(f"RÈGLE DÉCLENCHÉE: '{rule.get('name')}' pour {action_to_take} sur {asset}.")
                        
                        decision = {
                            "action": action_to_take,
                            "asset": asset,
                            "order_type": rule.get("order_type", "MARKET"),
                            "strategy_type": self.strategy_config.get("strategy_name"),
                            "rule_name": rule.get('name'),
                            "magic_number": self.strategy_config.get("magic_number")
                        }
                        
                        # Enrichir la décision avec un SL/TP dynamique
                        return self._calculate_sl_tp_for_entry(decision, asset_signals)
        return None

    def _calculate_sl_tp_for_entry(self, decision: Dict[str, Any], asset_signals: Dict[str, Any]) -> Dict[str, Any]:
        """Calcule le Stop Loss et le Take Profit dynamiquement basés sur la structure du marché."""
        point = asset_signals.get('symbol_point_value', 0.01)
        current_price = asset_signals.get('current_price')
        action = decision['action']
        
        if not current_price or not point:
            self.logger.warning(f"Données de prix ou de point manquantes pour {decision['asset']}. Utilisation du SL/TP fixe.")
            decision['target_sl_pips'] = self.strategy_config.get("stop_loss_pips", 20)
            decision['target_tp_pips'] = self.strategy_config.get("take_profit_pips", 40)
            return decision

        sl_price = 0.0
        rule_name = decision.get('rule_name', '').lower()

        if "order block" in rule_name and asset_signals.get('ob_details'):
            ob_details = asset_signals['ob_details']
            sl_level = ob_details['bottom'] if action == "BUY" else ob_details['top']
            sl_price = sl_level - (self.sl_buffer_pips * point) if action == "BUY" else sl_level + (self.sl_buffer_pips * point)
        elif "liquidity grab" in rule_name and asset_signals.get('liquidity_grab_details'):
            lg_details = asset_signals['liquidity_grab_details']
            sl_level = lg_details['level_swept']
            sl_price = sl_level - (self.sl_buffer_pips * point) if action == "BUY" else sl_level + (self.sl_buffer_pips * point)
        else: # Fallback
            sl_pips_fallback = self.strategy_config.get("stop_loss_pips", 20)
            sl_price = current_price - (sl_pips_fallback * point) if action == "BUY" else current_price + (sl_pips_fallback * point)

        risk_distance = abs(current_price - sl_price)
        tp_price = current_price + (risk_distance * self.risk_reward_ratio) if action == "BUY" else current_price - (risk_distance * self.risk_reward_ratio)

        decision['target_sl_pips'] = risk_distance / point
        decision['target_tp_pips'] = abs(tp_price - current_price) / point
        
        return decision

    def evaluate_exit(self, context: Dict[str, Any], current_positions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Évalue les conditions de sortie en cherchant des signaux qui invalident le trade initial.
        """
        exit_decisions = []
        exit_rules = self.strategy_config.get("exit_rules", []) # Utilise les exit_rules de la config

        for position in current_positions:
            if position.get('magic') != self.strategy_config.get("magic_number"):
                continue

            asset = position.get('symbol')
            asset_signals = context.get('trading_signals', {}).get(asset)
            if not asset_signals:
                continue
            
            for rule in exit_rules:
                if self._check_rule_conditions(asset_signals, rule.get("conditions", {})):
                    self.logger.info(f"RÈGLE DE SORTIE: '{rule.get('name')}' pour la position #{position['ticket']} sur {asset}.")
                    exit_decisions.append({
                        "ticket_to_close": position['ticket'],
                        "reason": rule.get('name', 'Exit condition met')
                    })
                    break # Sortir de la boucle des règles dès qu'une condition de sortie est remplie
        return exit_decisions

    # --- MÉTHODES ABSTRAITES IMPLÉMENTÉES ---

    def get_parameters(self) -> Dict[str, Any]:
        """
        Retourne les paramètres de configuration actuels de la stratégie.
        """
        return self.strategy_config.copy()

    def update_strategy_parameters(self, new_params: Dict[str, Any]) -> None:
        """
        Met à jour les paramètres internes de la stratégie, typiquement suite à
        une recommandation de l'IA ou une adaptation dynamique.
        """
        self.logger.info(f"Mise à jour des paramètres de la stratégie Crypto : {new_params}")
        # Utiliser une fusion pour mettre à jour les dictionnaires imbriqués
        for key, value in new_params.items():
            if isinstance(value, dict) and isinstance(self.strategy_config.get(key), dict):
                self.strategy_config[key].update(value)
            else:
                self.strategy_config[key] = value
        
        # Recharger les attributs qui dépendent de la configuration
        self.__init__(self.config_manager, self.strategy_config)