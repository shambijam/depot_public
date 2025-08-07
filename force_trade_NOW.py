#!/usr/bin/env python3
"""
FORCE UN TRADE IMMÉDIATEMENT - Bypass total du système
"""
import MetaTrader5 as mt5
import time
from pathlib import Path
import json

def force_trade_direct():
    """Envoie un ordre DIRECTEMENT à MT5, sans passer par le bot"""
    
    print("🔥 FORCE TRADE DIRECT - BYPASS TOTAL")
    
    # 1. Charger les credentials MT5
    broker_file = Path("config/broker_accounts.json")
    with open(broker_file, 'r', encoding='utf-8') as f:
        brokers = json.load(f)
    
    print(f"📋 Structure trouvée: {brokers.keys()}")
    
    # Trouver le compte demo
    demo_account = None
    
    # Si "accounts" est une liste
    if isinstance(brokers.get("accounts"), list):
        for account in brokers["accounts"]:
            if "demo" in account.get("account_id", "").lower() or account.get("mode") == "DEMO":
                demo_account = account
                break
    # Si "accounts" est un dictionnaire
    elif isinstance(brokers.get("accounts"), dict):
        demo_account = brokers["accounts"].get("main_demo_broker_A")
    
    if not demo_account:
        print("❌ Aucun compte DEMO trouvé!")
        print(f"Comptes disponibles: {json.dumps(brokers, indent=2)[:500]}...")
        return False
    
    print(f"✅ Compte trouvé: {demo_account.get('account_id', 'Unknown')}")
    
    # 2. Connexion directe MT5
    if not mt5.initialize():
        print("❌ Échec initialisation MT5")
        return False
    
    # Extraire les infos de connexion
    account = int(demo_account.get("login", demo_account.get("account_number", 0)))
    password = str(demo_account.get("password", ""))
    server = demo_account.get("server", "")
    
    print(f"📡 Connexion: {account} @ {server}")
    
    if not mt5.login(account, password, server):
        error = mt5.last_error()
        print(f"❌ Échec connexion: {error}")
        mt5.shutdown()
        return False
    
    print(f"✅ Connecté au compte {account}")
    
    # 3. Préparer l'ordre
    symbol = "EURUSD"
    
    # Sélectionner le symbole
    if not mt5.symbol_select(symbol, True):
        print(f"❌ Impossible de sélectionner {symbol}")
        mt5.shutdown()
        return False
    
    symbol_info = mt5.symbol_info(symbol)
    if not symbol_info:
        print(f"❌ {symbol} non disponible")
        mt5.shutdown()
        return False
    
    # 4. Obtenir le prix
    tick = mt5.symbol_info_tick(symbol)
    if not tick:
        print("❌ Pas de prix disponible")
        mt5.shutdown()
        return False
    
    print(f"💰 Prix actuel: Bid={tick.bid:.5f} Ask={tick.ask:.5f}")
    
    # 5. Calculer le volume minimum
    min_volume = symbol_info.volume_min
    volume = max(0.01, min_volume)  # Au moins 0.01 ou le minimum requis
    
    # 6. ENVOYER L'ORDRE
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": volume,
        "type": mt5.ORDER_TYPE_BUY,
        "price": tick.ask,
        "sl": round(tick.ask - 0.0010, 5),  # 10 pips SL
        "tp": round(tick.ask + 0.0020, 5),  # 20 pips TP
        "deviation": 20,
        "magic": 999999,
        "comment": "FORCED_TEST",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    
    print(f"📤 Envoi ordre: BUY {symbol} {volume} lots @ {tick.ask:.5f}")
    