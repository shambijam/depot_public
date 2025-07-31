# Créez ce fichier test_minimal.py
def test_minimal_trade():
    # 1. Test connectivité MT5
    import MetaTrader5 as mt5
    if not mt5.initialize():
        print("❌ MT5 connection failed!")
        return
    
    # 2. Test données de base
    tick = mt5.symbol_info_tick("EURUSD")
    if tick is None:
        print("❌ No EURUSD data!")
        return
    
    print(f"✅ EURUSD Price: {tick.bid}")
    print(f"✅ Spread: {(tick.ask - tick.bid) * 10000} points")
    
    # 3. Test ordre minimal (0.01 lot)
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": "EURUSD",
        "volume": 0.01,
        "type": mt5.ORDER_TYPE_BUY,
        "price": tick.ask,
        "deviation": 10,
        "magic": 12345,
        "comment": "TEST_SNIPER_X"
    }
    
    result = mt5.order_send(request)
    print(f"Test order result: {result}")

# Lancez ce test maintenant
test_minimal_trade()