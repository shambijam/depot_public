import MetaTrader5 as mt5
import time
from datetime import datetime
import random

class SniperMinimal:
    def __init__(self):
        self.initialize_mt5()
        self.symbols = ["EURUSD", "GBPUSD"]
        self.trades_taken = 0
        
    def initialize_mt5(self):
        if not mt5.initialize():
            raise Exception("MT5 initialization failed")
        print("✅ MT5 Connected")
        
    def log(self, message):
        timestamp = datetime.now().strftime("%H:%M:%S")
        print(f"[{timestamp}] {message}")
        
    def get_market_data(self, symbol):
        """Récupère les données de marché basiques"""
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            self.log(f"❌ No tick data for {symbol}")
            return None
            
        rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M15, 0, 20)
        if rates is None:
            self.log(f"❌ No rates data for {symbol}")
            return None
            
        return {
            'tick': tick,
            'rates': rates,
            'spread_points': (tick.ask - tick.bid) * 10000
        }
    
    def basic_conditions_check(self, symbol, data):
        """Vérifications ultra-basiques"""
        checks = []
        
        # 1. Spread check (très permissif)
        if data['spread_points'] > 20:  # 20 points max
            checks.append(f"❌ Spread too high: {data['spread_points']:.1f}")
            return False, checks
        checks.append(f"✅ Spread OK: {data['spread_points']:.1f}")
        
        # 2. Market hours (très permissif)
        current_hour = datetime.now().hour
        if current_hour < 6 or current_hour > 22:  # Éviter la nuit seulement
            checks.append(f"❌ Outside trading hours: {current_hour}:00")
            return False, checks
        checks.append(f"✅ Trading hours OK: {current_hour}:00")
        
        # 3. Volatilité minimale
        if len(data['rates']) < 10:
            checks.append("❌ Insufficient data")
            return False, checks
        checks.append("✅ Sufficient data")
        
        return True, checks
    
    def simple_signal_detection(self, symbol, data):
        """Détection de signal ultra-simple"""
        rates = data['rates']
        
        # Simple: prix actuel vs moyenne des 10 dernières bougies
        recent_closes = [rate[4] for rate in rates[-10:]]  # Close prices
        avg_price = sum(recent_closes) / len(recent_closes)
        current_price = data['tick'].bid
        
        # Signal d'achat si prix actuel > moyenne + petit buffer
        # Signal de vente si prix actuel < moyenne - petit buffer
        buffer = avg_price * 0.0001  # 1 pip de buffer
        
        if current_price > avg_price + buffer:
            return "BUY", f"Price {current_price:.5f} > Avg {avg_price:.5f}"
        elif current_price < avg_price - buffer:
            return "SELL", f"Price {current_price:.5f} < Avg {avg_price:.5f}"
        else:
            return None, f"No signal - Price {current_price:.5f} near Avg {avg_price:.5f}"
    
    def execute_test_trade(self, symbol, signal_type, data):
        """Exécute un trade de test (très petit lot)"""
        tick = data['tick']
        
        if signal_type == "BUY":
            trade_type = mt5.ORDER_TYPE_BUY
            price = tick.ask
        else:
            trade_type = mt5.ORDER_TYPE_SELL
            price = tick.bid
            
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": 0.01,  # Lot minimal
            "type": trade_type,
            "price": price,
            "deviation": 20,
            "magic": 999999,
            "comment": f"SNIPER_MINIMAL_TEST_{self.trades_taken}"
        }
        
        self.log(f"🚀 Attempting {signal_type} on {symbol} at {price:.5f}")
        result = mt5.order_send(request)
        
        if result.retcode == mt5.TRADE_RETCODE_DONE:
            self.trades_taken += 1
            self.log(f"✅ Trade #{self.trades_taken} executed! Order: {result.order}")
            return True
        else:
            self.log(f"❌ Trade failed: {result.retcode} - {result.comment}")
            return False
    
    def analyze_symbol(self, symbol):
        """Analyse complète d'un symbole"""
        self.log(f"\n--- Analyzing {symbol} ---")
        
        # 1. Récupération des données
        data = self.get_market_data(symbol)
        if data is None:
            return False
        
        # 2. Vérifications basiques
        conditions_ok, checks = self.basic_conditions_check(symbol, data)
        for check in checks:
            self.log(check)
            
        if not conditions_ok:
            self.log(f"❌ Basic conditions failed for {symbol}")
            return False
        
        # 3. Détection de signal
        signal_type, signal_reason = self.simple_signal_detection(symbol, data)
        self.log(f"📊 Signal: {signal_type or 'NONE'} - {signal_reason}")
        
        if signal_type is None:
            return False
        
        # 4. Exécution
        return self.execute_test_trade(symbol, signal_type, data)
    
    def run_minimal_test(self, max_trades=3, max_cycles=50):
        """Lance le test minimal"""
        self.log("🚀 Starting SNIPER_X Minimal Test")
        self.log(f"Target: {max_trades} trades, Max cycles: {max_cycles}")
        
        cycle = 0
        while self.trades_taken < max_trades and cycle < max_cycles:
            cycle += 1
            self.log(f"\n{'='*40}")
            self.log(f"CYCLE {cycle}/{max_cycles} - Trades taken: {self.trades_taken}")
            self.log(f"{'='*40}")
            
            # Analyse chaque symbole
            for symbol in self.symbols:
                if self.trades_taken >= max_trades:
                    break
                    
                try:
                    self.analyze_symbol(symbol)
                except Exception as e:
                    self.log(f"❌ Error analyzing {symbol}: {e}")
            
            # Attendre avant le prochain cycle
            if self.trades_taken < max_trades:
                self.log("⏳ Waiting 30 seconds...")
                time.sleep(30)
        
        # Résultats finaux
        self.log(f"\n{'='*50}")
        self.log(f"🏁 TEST COMPLETED")
        self.log(f"Trades taken: {self.trades_taken}/{max_trades}")
        self.log(f"Cycles completed: {cycle}/{max_cycles}")
        
        if self.trades_taken == 0:
            self.log("🚨 NO TRADES TAKEN - SYSTEM HAS ISSUES")
        else:
            self.log("✅ SYSTEM CAN TRADE - Check your full system logic")
        
        self.log(f"{'='*50}")

# LANCEMENT DU TEST
if __name__ == "__main__":
    try:
        sniper = SniperMinimal()
        sniper.run_minimal_test(max_trades=2, max_cycles=20)
    except Exception as e:
        print(f"FATAL ERROR: {e}")
    finally:
        mt5.shutdown()