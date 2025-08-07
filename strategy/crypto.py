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
        self.risk_reward_ratio = self.strategy_config.get("smart_targets", {}).get("take_profit_multiplier", 2.0)
        self.sl_buffer_pips = self.strategy_config.get("smart_targets", {}).get("sl_buffer_pips", 5)
        self.logger.info("Stratégie Crypto initialisée avec gestion de trade dynamique.")

    def evaluate_entry(self, context: Dict[str, Any], signals: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        NOUVELLE LOGIQUE : Sélectionne l'actif crypto avec le plus fort potentiel
        et délègue le calcul des paramètres de trade à la logique de SL/TP dynamique.
        """
        best_asset = None
        # Seuil de confiance plus élevé pour la volatilité des cryptos
        min_confidence_threshold = self.strategy_config.get("min_confidence_for_entry", 0.7)
        highest_confidence = min_confidence_threshold
        
        tradeable_assets = self.strategy_config.get("tradeable_assets", [])

        for asset in tradeable_assets:
            asset_signals = signals.get(asset)
            if not asset_signals:
                continue
            
            confidence = asset_signals.get('confidence_score', 0.0)
            if confidence > highest_confidence:
                highest_confidence = confidence
                best_asset = asset
        
        if not best_asset:
            self.logger.info("Aucun signal crypto n'a dépassé le seuil de confiance pour une entrée.")
            return None

        self.logger.info(f"MEILLEUR CANDIDAT CRYPTO: {best_asset} (Confiance: {highest_confidence:.2f}).")
        
        final_signals = signals[best_asset]
        phase = final_signals.get('phase', '')
        action_to_take = "BUY" if "bullish" in phase or "up" in phase else "SELL" if "bearish" in phase or "down" in phase else None

        if not action_to_take:
            return None
            
        # Construire une décision de base avant de l'enrichir
        decision = {
            "action": action_to_take,
            "asset": best_asset,
            "order_type": "MARKET",
            "strategy_type": self.strategy_config.get("strategy_name"),
            "rule_name": f"Dynamic SL/TP for {best_asset}",
            "magic_number": self.strategy_config.get("magic_number")
        }
        
        # ON CONSERVE ET ON UTILISE LA LOGIQUE DE CALCUL SPÉCIFIQUE
        return self._calculate_sl_tp_for_entry(decision, final_signals)


    def _calculate_sl_tp_for_entry(self, decision: Dict[str, Any], asset_signals: Dict[str, Any]) -> Dict[str, Any]:
        """Calcule le Stop Loss et le Take Profit dynamiquement basés sur la structure du marché."""
        point = asset_signals.get('symbol_point_value', 0.01)
        current_price = asset_signals.get('current_price')
        action = decision['action']
        
        if not current_price or not point:
            self.logger.warning(f"Données de prix ou de point manquantes pour {decision['asset']}. Utilisation du SL/TP fixe.")
            decision['target_sl_pips'] = self.strategy_config.get("stop_loss_pips", 200) # Valeurs plus grandes pour crypto
            decision['target_tp_pips'] = self.strategy_config.get("take_profit_pips", 400)
            return decision

        sl_price = 0.0
        # On peut se baser sur les signaux détectés pour un SL intelligent
        if action == "BUY" and asset_signals.get('ob_details'):
            sl_level = asset_signals['ob_details']['zone'][0] # Bas de l'OB
            sl_price = sl_level - (self.sl_buffer_pips * point)
        elif action == "SELL" and asset_signals.get('ob_details'):
            sl_level = asset_signals['ob_details']['zone'][1] # Haut de l'OB
            sl_price = sl_level + (self.sl_buffer_pips * point)
        else: # Fallback
            sl_pips_fallback = self.strategy_config.get("stop_loss_pips", 200)
            sl_price = current_price - (sl_pips_fallback * point) if action == "BUY" else current_price + (sl_pips_fallback * point)

        risk_distance = abs(current_price - sl_price)
        tp_price = current_price + (risk_distance * self.risk_reward_ratio) if action == "BUY" else current_price - (risk_distance * self.risk_reward_ratio)

        # Convertir les distances en pips pour le paquet de décision
        decision['target_sl_pips'] = round(risk_distance / point)
        decision['target_tp_pips'] = round(abs(tp_price - current_price) / point)
        
        self.logger.info(f"SL/TP Dynamique pour {decision['asset']}: SL={decision['target_sl_pips']} pips, TP={decision['target_tp_pips']} pips.")
        
        return decision

    def evaluate_exit(self, context: Dict[str, Any], current_positions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Évalue les conditions de sortie en cherchant des signaux qui invalident le trade initial.
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
                    self.logger.info(f"RÈGLE DE SORTIE: '{rule.get('name')}' pour la position #{position['ticket']} sur {asset}.")
                    exit_decisions.append({
                        "ticket_to_close": position['ticket'],
                        "reason": rule.get('name', 'Exit condition met')
                    })
                    break
        return exit_decisions

    def get_parameters(self) -> Dict[str, Any]:
        """
        Retourne les paramètres de configuration actuels de la stratégie.
        """
        return self.strategy_config.copy()

    def update_strategy_parameters(self, new_params: Dict[str, Any]) -> None:
        """
        Met à jour les paramètres internes de la stratégie.
        """
        self.logger.info(f"Mise à jour des paramètres de la stratégie Crypto : {new_params}")
        for key, value in new_params.items():
            if isinstance(value, dict) and isinstance(self.strategy_config.get(key), dict):
                self.strategy_config[key].update(value)
            else:
                self.strategy_config[key] = value
        
        self.__init__(self.config_manager, self.strategy_config)