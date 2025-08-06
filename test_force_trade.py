#!/usr/bin/env python3
"""
Script de test pour FORCER le bot à prendre un trade
"""
import json
import sys
from pathlib import Path

def force_trade_mode():
    """Active le mode trading forcé dans les configs"""
    
    # 1. Modifier prod_config.json
    prod_config_path = Path("config/prod_config.json")
    with open(prod_config_path, 'r', encoding='utf-8') as f:  # ← AJOUT encoding='utf-8'
        config = json.load(f)
    
    # Baisser TOUS les seuils
    config["ai"]["opportunity_filtering"]["min_signal_confidence"] = 0.01
    config["scoring_rules"]["min_optimal_score_threshold"] = 0.001
    config["scoring_rules"]["base_score"] = 0.8
    
    # Ajouter ces lignes si elles n'existent pas
    if "decision_making" not in config:
        config["decision_making"] = {}
    if "confidence_thresholds" not in config["decision_making"]:
        config["decision_making"]["confidence_thresholds"] = {}
    
    config["decision_making"]["confidence_thresholds"]["medium"] = 0.3
    config["decision_making"]["confidence_thresholds"]["low"] = 0.1
    
    with open(prod_config_path, 'w', encoding='utf-8') as f:  # ← AJOUT encoding='utf-8'
        json.dump(config, f, indent=4, ensure_ascii=False)
    print("✅ prod_config.json modifié pour mode FORCE")
    
    # 2. Modifier phase_observer_config.json
    phase_config_path = Path("config/phase_observer_config.json")
    if phase_config_path.exists():
        with open(phase_config_path, 'r', encoding='utf-8') as f:  # ← AJOUT encoding='utf-8'
            config = json.load(f)
        
        if "core_parameters" not in config:
            config["core_parameters"] = {}
            
        config["core_parameters"]["volatility_threshold"] = 0.00001
        config["core_parameters"]["volume_zscore"] = 0.5
        config["core_parameters"]["impulse_threshold"] = 0.00001
        
        if "confidence_score_calculation" not in config:
            config["confidence_score_calculation"] = {}
        config["confidence_score_calculation"]["base_confidence"] = 0.5
        
        with open(phase_config_path, 'w', encoding='utf-8') as f:  # ← AJOUT encoding='utf-8'
            json.dump(config, f, indent=4, ensure_ascii=False)
        print("✅ phase_observer_config.json modifié pour mode FORCE")
    else:
        print("⚠️ phase_observer_config.json non trouvé")
    
    print("\n🔥 MODE FORCE_TRADE ACTIVÉ !")
    print("Redémarrez le bot : python cli.py start --mode DEMO")

if __name__ == "__main__":
    force_trade_mode()