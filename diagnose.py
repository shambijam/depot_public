#!/usr/bin/env python3
"""
Script de diagnostic pour SNIPER_X - Pourquoi zéro trade ?
"""

import json
import sys
from pathlib import Path

def check_config_files():
    """Vérifie les fichiers de configuration critiques"""
    print("🔍 DIAGNOSTIC SNIPER_X - ZÉRO TRADE")
    print("=" * 50)
    
    # Vérifier prod_config.json
    prod_config_path = Path('config/prod_config.json')
    if prod_config_path.exists():
        with open(prod_config_path, 'r', encoding='utf-8') as f:
            prod_config = json.load(f)
            
        print("\n📋 PROD_CONFIG.JSON:")
        print(f"✅ Fichier trouvé: {prod_config_path}")
        
        # Vérifier default_strategy
        default_strategy = prod_config.get('strategies', {}).get('default_strategy')
        print(f"🎯 Stratégie par défaut: {default_strategy}")
        
        # Vérifier la section strategies
        strategies_section = prod_config.get('strategies', {})
        strategy_names = [k for k in strategies_section.keys() if k != 'default_strategy']
        print(f"📊 Stratégies disponibles: {strategy_names}")
        
        # Vérifier phase_detection_settings
        phase_settings = prod_config.get('phase_detection_settings', {})
        lookback = phase_settings.get('lookback_window', 'NON_DÉFINI')
        print(f"🔍 Lookback window: {lookback}")
        
        # Vérifier risk management
        risk_settings = prod_config.get('risk_management_settings', {})
        account_equity = risk_settings.get('default_account_equity', 'NON_DÉFINI')
        print(f"💰 Équité par défaut: {account_equity}")
        
    else:
        print("❌ prod_config.json INTROUVABLE!")
        return False
    
    # Vérifier les configs de stratégies
    print("\n📁 CONFIGURATIONS DE STRATÉGIES:")
    strategy_dir = Path('config/strategy')
    if strategy_dir.exists():
        strategy_files = list(strategy_dir.glob('*.json'))
        for strategy_file in strategy_files:
            print(f"✅ {strategy_file.name}")
            
            # Analyser chaque config de stratégie
            with open(strategy_file, 'r', encoding='utf-8') as f:
                strategy_config = json.load(f)
                
            strategy_name = strategy_config.get('strategy_name', 'UNKNOWN')
            print(f"   📝 Nom: {strategy_name}")
            
            # Vérifier opportunity_triggers
            triggers = strategy_config.get('opportunity_triggers', [])
            print(f"   🎯 Triggers: {len(triggers)}")
            
            if triggers:
                first_trigger = triggers[0]
                conditions = first_trigger.get('conditions', {})
                
                # Conditions critiques
                spread_max = conditions.get('current_spread_points', {}).get('max', 'NON_DÉFINI')
                confidence_min = conditions.get('confidence_score_threshold', {}).get('min', 'NON_DÉFINI')
                
                print(f"      📏 Spread max: {spread_max}")
                print(f"      🎯 Confidence min: {confidence_min}")
                
                # SL/TP
                sl_pips = first_trigger.get('target_sl_pips', 'NON_DÉFINI')
                tp_pips = first_trigger.get('target_tp_pips', 'NON_DÉFINI')
                print(f"      🛡️ SL: {sl_pips} pips, TP: {tp_pips} pips")
            
            print()
    else:
        print("❌ Dossier config/strategy INTROUVABLE!")
        return False
    
    # Vérifier les configs d'assets
    print("🏦 CONFIGURATIONS D'ASSETS:")
    assets_dir = Path('config/assets_config')
    if assets_dir.exists():
        crypto_assets = ['BTCUSD.json', 'ETHUSD.json', 'LTCUSD.json']
        for asset_file in crypto_assets:
            asset_path = assets_dir / asset_file
            if asset_path.exists():
                with open(asset_path, 'r', encoding='utf-8') as f:
                    asset_config = json.load(f)
                
                symbol = asset_config.get('symbol', 'UNKNOWN')
                max_spread = asset_config.get('max_spread', 'NON_DÉFINI')
                auto_blacklist = asset_config.get('auto_blacklist', 'NON_DÉFINI')
                
                print(f"✅ {symbol}: spread_max={max_spread}, auto_blacklist={auto_blacklist}")
            else:
                print(f"❌ {asset_file} MANQUANT")
    
    return True

def analyze_potential_issues():
    """Analyse les problèmes potentiels"""
    print("\n🚨 PROBLÈMES POTENTIELS IDENTIFIÉS:")
    print("-" * 40)
    
    issues = [
        "1. 📊 PhaseObserver analyse seulement 1 barre (insuffisant)",
        "2. 🎯 Confidence score très bas (0.150)",
        "3. ⚙️ Stratégie par défaut possiblement mal configurée",
        "4. 📏 Seuils d'entrée potentiellement trop stricts",
        "5. 💰 Volume/liquidité insuffisant sur crypto"
    ]
    
    for issue in issues:
        print(issue)

def suggest_solutions():
    """Suggère des solutions"""
    print("\n💡 SOLUTIONS RECOMMANDÉES:")
    print("-" * 30)
    
    solutions = [
        "1. 🔍 Augmenter lookback_window à 50+ barres",
        "2. 📊 Réduire confidence_score_threshold à 0.3",
        "3. 📏 Assouplir current_spread_points.max",
        "4. 🎯 Vérifier default_strategy dans prod_config",
        "5. 🚀 Tester avec des données plus longues (1 semaine)"
    ]
    
    for solution in solutions:
        print(solution)

if __name__ == "__main__":
    if check_config_files():
        analyze_potential_issues()
        suggest_solutions()
    else:
        print("\n❌ Impossible de continuer le diagnostic.")
        sys.exit(1)