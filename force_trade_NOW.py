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
    
    demo_account = brokers["accounts"]["main_demo_broker_A"]
    
    # 2. Connexion directe MT5
    if not mt5.initialize():
        print("❌ Échec initialisation MT5")
        return False
    
    account = int(demo_account["login"])
    password = demo_account["password"]
    server = demo_account["server"]
    
    if not mt5.login(account, password, server):
        print(f"❌ Échec connexion: {mt5.last_error()}")
        mt5.shutdown()
        return False
    
    print(f"✅ Connecté au compte {account}")
    
    # 3. Préparer l'ordre
    symbol = "EURUSD"
    symbol_info = mt5.symbol_info(symbol)
    
    if not symbol_info or not symbol_info.visible:
        print(f"❌ {symbol} non disponible")
        mt5.shutdown()
        return False
    
    # 4. Obtenir le prix
    tick = mt5.symbol_info_tick(symbol)
    if not tick:
        print("❌ Pas de prix disponible")
        mt5.shutdown()
        return False
    
    # 5. ENVOYER L'ORDRE
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": 0.01,  # Micro lot
        "type": mt5.ORDER_TYPE_BUY,
        "price": tick.ask,
        "sl": tick.ask - 0.0010,  # 10 pips SL
        "tp": tick.ask + 0.0020,  # 20 pips TP
        "deviation": 20,
        "magic": 999999,
        "comment": "FORCED_TEST_TRADE",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    
    print(f"📤 Envoi ordre: BUY {symbol} @ {tick.ask:.5f}")
    result = mt5.order_send(request)
    
    if result.retcode == mt5.TRADE_RETCODE_DONE:
        print(f"✅✅✅ TRADE EXÉCUTÉ! Ticket: {result.order}")
        print(f"Volume: {result.volume} | Prix: {result.price}")
        return True
    else:
        print(f"❌ Échec ordre: {result.retcode} - {result.comment}")
        return False
    
    mt5.shutdown()

if __name__ == "__main__":
    if force_trade_direct():
        print("\n🎉 SUCCÈS! Un trade a été ouvert!")
        print("Vérifiez MetaTrader pour voir la position.")
    else:
        print("\n😔 Échec du trade forcé.")