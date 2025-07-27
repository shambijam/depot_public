# strategy/base_strategy.py

import abc
from typing import Dict, Any, List, Optional
import logging

class BaseStrategy(abc.ABC):
    """
    Classe de base abstraite définissant le contrat d'interface pour toutes
    les stratégies de trading de Flexbot V2.
    """
    
    def __init__(self, config_manager_instance: Any, strategy_config: Dict[str, Any]):
        self.config_manager = config_manager_instance
        self.strategy_config = strategy_config
        self.logger = logging.getLogger(f"Strategy.{self.__class__.__name__}")
        self.logger.debug(f"Initialisation de la stratégie '{self.strategy_config.get('strategy_name', 'UnnamedStrategy')}'.")

    @abc.abstractmethod
    def evaluate_entry(self, context: Dict[str, Any], signals: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Évalue les conditions d'entrée potentielles pour tous les actifs gérés par cette stratégie.
        """
        pass

    @abc.abstractmethod
    def evaluate_exit(self, context: Dict[str, Any], current_positions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Évalue les conditions de sortie pour les positions actuellement ouvertes et gérées par cette stratégie.
        """
        pass

    @abc.abstractmethod
    def get_parameters(self) -> Dict[str, Any]:
        """
        Retourne un dictionnaire des paramètres de configuration actuels de la stratégie.
        """
        pass

    @abc.abstractmethod
    def update_strategy_parameters(self, new_params: Dict[str, Any]) -> None:
        """
        Met à jour les paramètres de la stratégie à partir d'un dictionnaire de nouvelles valeurs.
        """
        pass

    def _check_rule_conditions(self, asset_signals: Dict[str, Any], conditions: Dict[str, Any]) -> bool:
        """
        Moteur de validation de règles. Évalue si les signaux d'un actif respectent
        un ensemble de conditions définies dans la configuration.

        Args:
            asset_signals (Dict[str, Any]): Les signaux actuels pour un actif donné.
            conditions (Dict[str, Any]): Le dictionnaire des conditions d'une règle.

        Returns:
            bool: True si toutes les conditions sont remplies, False sinon.
        """
        # Valider la confiance minimale
        min_confidence = conditions.get("min_confidence", 1.0)
        if asset_signals.get('confidence_score', 0.0) < min_confidence:
            return False

        # Valider la phase de marché
        required_phases = conditions.get("phase_must_contain", [])
        current_phase = asset_signals.get('phase', '')
        if required_phases and not any(p in current_phase for p in required_phases):
            return False

        # Valider la présence des signaux requis
        required_signals = conditions.get("signal_must_contain", [])
        if required_signals and not all(asset_signals.get(sig, False) for sig in required_signals):
            return False
            
        # Valider l'absence des signaux interdits
        forbidden_signals = conditions.get("signal_must_not_contain", [])
        if forbidden_signals and any(asset_signals.get(sig, False) for sig in forbidden_signals):
            return False

        return True

    def __str__(self) -> str:
        return f"{self.strategy_config.get('strategy_name', self.__class__.__name__)}"

    def __repr__(self) -> str:
        return self.__str__()