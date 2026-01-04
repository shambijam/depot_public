# 🏗️ ARCHITECTURE MULTI-THREAD SCALPING - 3 ACTIFS SIMULTANÉS
**Date**: 31 Décembre 2025
**Objectif**: Implémenter 1 thread scalping par actif (USDJPY, EURUSD, GBPUSD)

---

## 📊 ARCHITECTURE ACTUELLE (Avant migration)

### Threads actifs
```
┌─────────────────────────────────────────────────────────┐
│ 🚀 DÉMARRAGE DES THREADS SÉPARÉS                       │
├─────────────────────────────────────────────────────────┤
│ • SCALPING Thread    : Cycle 5s (USDJPY UNIQUEMENT) ⚡  │
│ • BASKET MONITOR     : Surveillance continue (100ms)    │
└─────────────────────────────────────────────────────────┘
```

### Flux d'exécution actuel
```python
# run_bot.py - Ligne ~3111
def scalping_fast_thread(...):
    while not stop_event.is_set():
        # ANALYSE UNIQUEMENT USDJPY
        rates_df = mt5_connector.get_rates("USDJPY", "M1", 50)
        ticks_df = mt5_connector.get_ticks("USDJPY", candle_time)

        # OrderFlow V6 + Timing Gatekeeper
        market_results = market_analyzer.analyze(asset="USDJPY", df=rates_df, ticks=ticks_df)

        # Rapport scalping USDJPY
        logger.info("📊 RAPPORT SCALPING USDJPY | Cycle #X")
        logger.info(f"   Régime actuel    : {regime}")
        logger.info(f"   OrderFlow Score  : {score}/100")
        logger.info(f"   Timing Verdict   : {verdict}")

        time.sleep(5)  # Cycle 5 secondes
```

### Problèmes identifiés
1. ❌ **EURUSD et GBPUSD ne sont PAS analysés** (thread liquidity supprimé)
2. ❌ **Régime de marché = UNKNOWN** (erreur orchestrator.py)
3. ❌ **Un seul rapport console** (lisible mais incomplet)

---

## 🎯 ARCHITECTURE CIBLE (Multi-thread)

### Threads scalping indépendants
```
┌─────────────────────────────────────────────────────────────────┐
│ 🚀 THREADS SCALPING MULTI-ACTIFS (3 threads)                   │
├─────────────────────────────────────────────────────────────────┤
│ • SCALPING-USDJPY  : Cycle 5s | OrderFlow V6 + Timing + Régime │
│ • SCALPING-EURUSD  : Cycle 5s | OrderFlow V6 + Timing + Régime │
│ • SCALPING-GBPUSD  : Cycle 5s | OrderFlow V6 + Timing + Régime │
│ • BASKET MONITOR   : Surveillance continue (100ms)              │
└─────────────────────────────────────────────────────────────────┘
```

### Flux d'exécution par thread
```python
# Nouveau design: 1 fonction générique pour N actifs
def scalping_asset_thread(
    asset_symbol: str,           # "USDJPY", "EURUSD", "GBPUSD"
    stop_event: threading.Event,
    mt5_connector,
    config_manager,
    logger,
    cycle_seconds: int = 5
):
    """Thread scalping dédié à UN actif."""

    # Instances DÉDIÉES par actif
    market_analyzer = MarketAnalyzer(config_manager, asset=asset_symbol)
    scalping_strategy = ScalpingStrategy(config_manager, asset=asset_symbol)

    cycle_count = 0

    while not stop_event.is_set():
        cycle_count += 1

        # 1. Récupération données spécifiques à l'actif
        rates_df = mt5_connector.get_rates(asset_symbol, "M1", 50)
        ticks_df = mt5_connector.get_ticks(asset_symbol, last_candle_time)

        # 2. Analyse marché (OrderFlow V6 + Timing + Régime)
        market_results = market_analyzer.analyze(
            asset=asset_symbol,
            df=rates_df,
            ticks=ticks_df
        )

        # 3. Décision trading
        decision = scalping_strategy.evaluate_entry(
            context=market_results,
            signals={asset_symbol: market_results}
        )

        # 4. Rapport console COMPACT
        logger.info(f"[{asset_symbol}] Cycle #{cycle_count} | "
                   f"Régime={market_results['regime']} | "
                   f"OF={decision['orderflow_score']}/100 | "
                   f"Timing={decision['timing_verdict']} | "
                   f"Action={decision['action']}")

        time.sleep(cycle_seconds)
```

---

## 🖥️ GESTION AFFICHAGE CONSOLE (Défi majeur)

### Problème: 3 threads qui loguent simultanément
```
❌ MAUVAIS AFFICHAGE (illisible)
[USDJPY] Cycle #1 | Régime=transitional | OF=0/100 | Timing=VETO
[EURUSD] Cycle #1 | Régime=trending_retail_bull | OF=78/100 | Timing=PASS
[GBPUSD] ════════════════════════════════════════════
[USDJPY]    Régime actuel    : transitional
[EURUSD]    Score Final      : 78/100 (TRADE)
[GBPUSD]    Verdict          : ❌ VETO
[USDJPY] 🎯 DÉCISION FINALE
```

### Solution 1: Rapports COMPACT (1 ligne par actif)
```python
# Format court pour console multi-thread
logger.info(f"[USDJPY] C#1 | R:transitional | OF:0 | T:VETO | A:HOLD")
logger.info(f"[EURUSD] C#1 | R:trending     | OF:78| T:PASS | A:BUY")
logger.info(f"[GBPUSD] C#1 | R:range        | OF:45| T:VETO | A:HOLD")
```

**Avantages**:
- ✅ Lisible même avec 3 threads
- ✅ Pas de chevauchement
- ✅ Vue d'ensemble rapide

**Inconvénients**:
- ❌ Moins de détails (delta, imbalance, etc.)

### Solution 2: Rapports DÉCALÉS (stagger timing)
```python
# Décalage initial pour éviter collisions
THREAD_START_OFFSETS = {
    "USDJPY": 0,      # Démarre immédiatement
    "EURUSD": 1.5,    # Démarre 1.5s après USDJPY
    "GBPUSD": 3.0     # Démarre 3.0s après USDJPY
}

def scalping_asset_thread(asset_symbol, ...):
    offset = THREAD_START_OFFSETS.get(asset_symbol, 0)
    time.sleep(offset)  # Décalage initial

    while not stop_event.is_set():
        # ... analyse ...

        # Rapport COMPLET (mais décalé)
        logger.info("="*80)
        logger.info(f"📊 RAPPORT SCALPING {asset_symbol} | Cycle #{cycle}")
        logger.info("="*80)
        # ... détails complets ...

        time.sleep(5)
```

**Timing d'exécution**:
```
T=0.0s  : [USDJPY] Rapport complet
T=1.5s  : [EURUSD] Rapport complet
T=3.0s  : [GBPUSD] Rapport complet
T=5.0s  : [USDJPY] Rapport complet
T=6.5s  : [EURUSD] Rapport complet
T=8.0s  : [GBPUSD] Rapport complet
```

**Avantages**:
- ✅ Rapports complets et lisibles
- ✅ Pas de chevauchement si timing respecté
- ✅ Conservation du format actuel

**Inconvénients**:
- ⚠️ Console défile rapidement (15 rapports/minute)
- ⚠️ Si un thread lag, collision possible

### Solution 3: Rapport AGRÉGÉ (1 rapport global)
```python
# Classe partagée entre threads
class GlobalScalpingState:
    def __init__(self):
        self.lock = threading.Lock()
        self.states = {
            "USDJPY": {},
            "EURUSD": {},
            "GBPUSD": {}
        }

    def update(self, asset, data):
        with self.lock:
            self.states[asset] = data

    def get_all(self):
        with self.lock:
            return self.states.copy()

# Thread dédié à l'affichage
def report_aggregated_thread(global_state, stop_event):
    while not stop_event.is_set():
        states = global_state.get_all()

        logger.info("="*80)
        logger.info("📊 RAPPORT SCALPING GLOBAL")
        logger.info("="*80)

        for asset, data in states.items():
            logger.info(f"\n🔹 {asset}")
            logger.info(f"   Régime: {data.get('regime', 'N/A')}")
            logger.info(f"   OrderFlow: {data.get('of_score', 0)}/100")
            logger.info(f"   Timing: {data.get('timing', 'N/A')}")
            logger.info(f"   Action: {data.get('action', 'HOLD')}")

        logger.info("="*80)
        time.sleep(10)  # Rapport global toutes les 10s
```

**Avantages**:
- ✅ **Console propre et lisible**
- ✅ Vue d'ensemble claire
- ✅ Synchronisation garantie

**Inconvénients**:
- ❌ Thread supplémentaire
- ❌ Latence affichage (10s)

---

## ⚙️ RECOMMANDATION: Solution HYBRIDE

```python
# Format COMPACT par défaut + Rapport DÉTAILLÉ sur demande

def scalping_asset_thread(asset_symbol, global_state, stop_event, ...):
    cycle_count = 0

    while not stop_event.is_set():
        cycle_count += 1

        # ... analyse ...

        # 1. MAJ état global (pour rapport agrégé)
        global_state.update(asset_symbol, {
            'regime': market_results.get('regime', 'UNKNOWN'),
            'of_score': decision.get('orderflow_score', 0),
            'timing': decision.get('timing_verdict', 'N/A'),
            'action': decision.get('action', 'HOLD'),
            'confidence': decision.get('confidence', 0),
            'delta': market_results.get('delta_total', 0),
            'tick_rate': market_results.get('tick_rate', 0)
        })

        # 2. Log COMPACT (toujours)
        logger.info(f"[{asset_symbol:6}] C#{cycle_count:03d} | "
                   f"R:{market_results.get('regime', 'UNK'):20} | "
                   f"OF:{decision.get('orderflow_score', 0):3}/100 | "
                   f"T:{decision.get('timing_verdict', 'N/A'):4} | "
                   f"A:{decision.get('action', 'HOLD'):4}")

        # 3. Rapport DÉTAILLÉ uniquement si SIGNAL FORT
        if decision.get('orderflow_score', 0) >= 75:
            logger.info("="*80)
            logger.info(f"🎯 SIGNAL FORT DÉTECTÉ - {asset_symbol}")
            logger.info("="*80)
            logger.info(f"   OrderFlow Score  : {decision['orderflow_score']}/100")
            logger.info(f"   Delta Total      : {market_results.get('delta_total', 0)}")
            logger.info(f"   Imbalance        : {market_results.get('imbalance', 0)}")
            logger.info(f"   Timing Verdict   : {decision.get('timing_verdict', 'N/A')}")
            logger.info(f"   Action           : {decision.get('action', 'HOLD')}")
            logger.info("="*80)

        time.sleep(5)

# Thread rapport agrégé (toutes les 30s)
def report_dashboard_thread(global_state, stop_event):
    while not stop_event.is_set():
        time.sleep(30)  # Rapport global toutes les 30s

        states = global_state.get_all()

        logger.info("\n" + "="*80)
        logger.info("📊 DASHBOARD SCALPING - Vue d'ensemble")
        logger.info("="*80)

        for asset in ["USDJPY", "EURUSD", "GBPUSD"]:
            data = states.get(asset, {})
            logger.info(f"\n🔹 {asset:6} | Régime: {data.get('regime', 'N/A'):20} | "
                       f"OF:{data.get('of_score', 0):3} | "
                       f"Timing:{data.get('timing', 'N/A'):4} | "
                       f"Action:{data.get('action', 'HOLD'):4}")

        logger.info("="*80 + "\n")
```

**Affichage console résultant**:
```
[USDJPY] C#001 | R:transitional         | OF:  0/100 | T:VETO | A:HOLD
[EURUSD] C#001 | R:trending_retail_bull | OF: 78/100 | T:PASS | A:BUY

================================================================================
🎯 SIGNAL FORT DÉTECTÉ - EURUSD
================================================================================
   OrderFlow Score  : 78/100
   Delta Total      : 145
   Imbalance        : 0.68
   Timing Verdict   : PASS
   Action           : BUY
================================================================================

[GBPUSD] C#001 | R:range                | OF: 12/100 | T:VETO | A:HOLD
[USDJPY] C#002 | R:transitional         | OF:  3/100 | T:VETO | A:HOLD
[EURUSD] C#002 | R:trending_retail_bull | OF: 82/100 | T:PASS | A:BUY
[GBPUSD] C#002 | R:range                | OF: 15/100 | T:VETO | A:HOLD

... (30 secondes plus tard) ...

================================================================================
📊 DASHBOARD SCALPING - Vue d'ensemble
================================================================================

🔹 USDJPY | Régime: transitional         | OF:  3 | Timing:VETO | Action:HOLD
🔹 EURUSD | Régime: trending_retail_bull | OF: 82 | Timing:PASS | Action:BUY
🔹 GBPUSD | Régime: range                | OF: 15 | Timing:VETO | Action:HOLD
================================================================================
```

---

## 🔧 DÉTECTION DE RÉGIME PAR ACTIF

### Problème actuel
- **Régime = UNKNOWN** pour tous les actifs (erreur orchestrator.py corrigée)
- Détection régime centralisée (pas par actif)

### Solution: Instances MarketAnalyzer dédiées
```python
# AVANT (partagé)
market_analyzer = MarketAnalyzer(config_manager)  # Instance globale
market_analyzer.analyze(asset="USDJPY", ...)      # Analyse USDJPY
market_analyzer.analyze(asset="EURUSD", ...)      # Écrase cache USDJPY !

# APRÈS (dédié)
market_analyzer_usdjpy = MarketAnalyzer(config_manager, asset="USDJPY")
market_analyzer_eurusd = MarketAnalyzer(config_manager, asset="EURUSD")
market_analyzer_gbpusd = MarketAnalyzer(config_manager, asset="GBPUSD")

# Chaque instance maintient son propre état
market_analyzer_usdjpy.analyze(asset="USDJPY", ...)  # Régime USDJPY
market_analyzer_eurusd.analyze(asset="EURUSD", ...)  # Régime EURUSD (indépendant)
```

### Régimes possibles par actif
```python
REGIMES = [
    "trending_retail_bull",        # Tendance haussière retail
    "trending_retail_bear",        # Tendance baissière retail
    "liquidity_eqh_eql",          # Liquidité (EQH/EQL)
    "transitional",               # Transition entre phases
    "range",                      # Range/consolidation
    "range_accumulation",         # Accumulation en range
    "range_distribution",         # Distribution en range
    "high_volatility_chaos"       # Volatilité extrême
]
```

**Exemple résultat**:
```
USDJPY: transitional         (Force: 0.76)
EURUSD: trending_retail_bull (Force: 0.85)
GBPUSD: range                (Force: 0.62)
```

---

## 🚀 PLAN D'IMPLÉMENTATION

### PHASE 1: Refactoring thread scalping (2h)
1. **Créer fonction générique** `scalping_asset_thread(asset_symbol, ...)`
2. **Créer classe GlobalScalpingState** pour partage état
3. **Instances MarketAnalyzer dédiées** (1 par actif)
4. **Tester avec USDJPY seul** (validation régression)

### PHASE 2: Ajout EURUSD (1h)
1. **Lancer 2e thread** `scalping_asset_thread("EURUSD", ...)`
2. **Vérifier isolation** (USDJPY et EURUSD indépendants)
3. **Tester affichage console** (2 threads simultanés)
4. **Ajuster décalage timing** si collision logs

### PHASE 3: Ajout GBPUSD (1h)
1. **Lancer 3e thread** `scalping_asset_thread("GBPUSD", ...)`
2. **Vérifier performance** (CPU, RAM, MT5 connexions)
3. **Tester affichage console** (3 threads simultanés)
4. **Optimiser logs** (format compact final)

### PHASE 4: Dashboard agrégé (1h)
1. **Créer thread rapport** `report_dashboard_thread(...)`
2. **Affichage synthèse** toutes les 30s
3. **Logs détaillés** uniquement si OrderFlow ≥ 75
4. **Tests stress** (vérifier stabilité 1h+)

### PHASE 5: Validation finale (1h)
1. **Tester 3 actifs simultanés** pendant 1h
2. **Vérifier isolation régime** (USDJPY ≠ EURUSD ≠ GBPUSD)
3. **Mesurer performance** (CPU < 50%, RAM < 2GB)
4. **Tester gestion erreurs** (1 actif fail ne crash pas les autres)

---

## ⚠️ RISQUES & MITIGATIONS

### Risque 1: Collision console (logs illisibles)
**Mitigation**: Format compact + décalage timing + dashboard agrégé

### Risque 2: Performance dégradée (3x plus de threads)
**Mitigation**:
- Cycle 5s (pas plus rapide)
- Instances légères (pas de duplication data MT5)
- Cache MarketAnalyzer par actif

### Risque 3: MT5 rate limiting (3x plus de requêtes)
**Mitigation**:
- get_rates() et get_ticks() ont cache intégré
- Limiter bars_count à 50 (pas 200)
- Monitoring log MT5 errors

### Risque 4: Race conditions (accès concurrent)
**Mitigation**:
- GlobalScalpingState avec threading.Lock()
- Basket Monitor déjà thread-safe
- Pas de partage data entre threads scalping

### Risque 5: Ordre d'exécution imprévisible
**Mitigation**:
- Décalage initial (0s, 1.5s, 3s)
- Logs avec timestamp précis
- Dashboard pour vue d'ensemble

---

## 📈 MÉTRIQUES DE SUCCÈS

### Performance
- ✅ CPU usage < 50% (moyenne)
- ✅ RAM usage < 2GB
- ✅ Latence analyse < 1s par actif
- ✅ Pas de lag MT5 connexion

### Fonctionnel
- ✅ 3 régimes indépendants (USDJPY ≠ EURUSD ≠ GBPUSD)
- ✅ OrderFlow V6 indépendant par actif
- ✅ Timing Gatekeeper indépendant par actif
- ✅ Rapports console lisibles

### Stabilité
- ✅ 0 crash pendant 1h de test
- ✅ Gestion erreurs isolée (1 actif fail ≠ crash global)
- ✅ Recovery automatique si MT5 disconnect

---

## 🔍 EXEMPLE CODE FINAL

```python
# run_bot.py - Architecture finale

class GlobalScalpingState:
    """État partagé entre threads scalping."""
    def __init__(self):
        self.lock = threading.Lock()
        self.states = {"USDJPY": {}, "EURUSD": {}, "GBPUSD": {}}

    def update(self, asset, data):
        with self.lock:
            self.states[asset] = data

    def get_all(self):
        with self.lock:
            return self.states.copy()


def scalping_asset_thread(
    asset_symbol: str,
    global_state: GlobalScalpingState,
    stop_event: threading.Event,
    mt5_connector,
    config_manager,
    logger,
    start_offset: float = 0
):
    """Thread scalping dédié à UN actif."""

    # Décalage initial pour éviter collisions
    time.sleep(start_offset)

    # Instances DÉDIÉES
    market_analyzer = MarketAnalyzer(config_manager, asset=asset_symbol)
    scalping_strategy = ScalpingStrategy(config_manager, asset=asset_symbol)

    cycle_count = 0
    logger.info(f"🚀 [SCALPING-{asset_symbol}] Thread démarré (offset={start_offset}s)")

    while not stop_event.is_set():
        try:
            cycle_count += 1

            # 1. Récupération données
            rates_df = mt5_connector.get_rates(asset_symbol, "M1", 50)
            last_candle_time = rates_df.iloc[-1]['time']
            ticks_df = mt5_connector.get_ticks(asset_symbol, last_candle_time)

            # 2. Analyse marché
            market_results = market_analyzer.analyze(
                asset=asset_symbol,
                df=rates_df,
                ticks=ticks_df
            )

            # 3. Décision trading
            decision = scalping_strategy.evaluate_entry(
                context=market_results,
                signals={asset_symbol: market_results}
            )

            # 4. MAJ état global
            global_state.update(asset_symbol, {
                'regime': market_results.get('regime', 'UNKNOWN'),
                'regime_strength': market_results.get('regime_strength', 0),
                'of_score': decision.get('orderflow_score', 0),
                'timing': decision.get('timing_verdict', 'N/A'),
                'action': decision.get('action', 'HOLD'),
                'confidence': decision.get('confidence', 0),
                'delta': market_results.get('delta_total', 0),
                'tick_rate': market_results.get('tick_rate', 0),
                'spread': market_results.get('spread', 0)
            })

            # 5. Log COMPACT
            logger.info(
                f"[{asset_symbol:6}] C#{cycle_count:03d} | "
                f"R:{market_results.get('regime', 'UNK')[:20]:20} | "
                f"OF:{decision.get('orderflow_score', 0):3}/100 | "
                f"T:{decision.get('timing_verdict', 'N/A')[:4]:4} | "
                f"A:{decision.get('action', 'HOLD')[:4]:4}"
            )

            # 6. Rapport DÉTAILLÉ si signal fort
            if decision.get('orderflow_score', 0) >= 75:
                logger.info("="*80)
                logger.info(f"🎯 SIGNAL FORT - {asset_symbol}")
                logger.info("="*80)
                logger.info(f"   Régime           : {market_results.get('regime', 'UNKNOWN')}")
                logger.info(f"   OrderFlow Score  : {decision['orderflow_score']}/100")
                logger.info(f"   Delta Total      : {market_results.get('delta_total', 0)}")
                logger.info(f"   Timing Verdict   : {decision.get('timing_verdict', 'N/A')}")
                logger.info(f"   Action           : {decision.get('action', 'HOLD')}")
                logger.info("="*80)

            # 7. Exécution trade si décision BUY/SELL
            if decision.get('action') in ['BUY', 'SELL']:
                # ... logique exécution ...
                pass

        except Exception as e:
            logger.error(f"[{asset_symbol}] Erreur cycle #{cycle_count}: {e}", exc_info=True)

        time.sleep(5)  # Cycle 5s

    logger.info(f"🛑 [SCALPING-{asset_symbol}] Thread arrêté proprement")


def report_dashboard_thread(
    global_state: GlobalScalpingState,
    stop_event: threading.Event,
    logger
):
    """Thread dashboard agrégé (toutes les 30s)."""

    while not stop_event.is_set():
        time.sleep(30)

        states = global_state.get_all()

        logger.info("\n" + "="*80)
        logger.info("📊 DASHBOARD SCALPING - Vue d'ensemble")
        logger.info("="*80)

        for asset in ["USDJPY", "EURUSD", "GBPUSD"]:
            data = states.get(asset, {})
            logger.info(
                f"🔹 {asset:6} | "
                f"Régime: {data.get('regime', 'N/A')[:20]:20} | "
                f"Force: {data.get('regime_strength', 0):.2f} | "
                f"OF:{data.get('of_score', 0):3} | "
                f"T:{data.get('timing', 'N/A')[:4]:4} | "
                f"A:{data.get('action', 'HOLD')[:4]:4}"
            )

        logger.info("="*80 + "\n")


# Main - Lancement threads
if __name__ == "__main__":
    # État global partagé
    global_state = GlobalScalpingState()

    # Stop events
    stop_scalping_usdjpy = threading.Event()
    stop_scalping_eurusd = threading.Event()
    stop_scalping_gbpusd = threading.Event()
    stop_dashboard = threading.Event()
    stop_basket_monitor = threading.Event()

    # Threads scalping (avec décalage)
    thread_usdjpy = threading.Thread(
        target=scalping_asset_thread,
        args=("USDJPY", global_state, stop_scalping_usdjpy, mt5_connector, config_manager, logger, 0.0),
        daemon=True,
        name="ScalpingThread-USDJPY"
    )

    thread_eurusd = threading.Thread(
        target=scalping_asset_thread,
        args=("EURUSD", global_state, stop_scalping_eurusd, mt5_connector, config_manager, logger, 1.5),
        daemon=True,
        name="ScalpingThread-EURUSD"
    )

    thread_gbpusd = threading.Thread(
        target=scalping_asset_thread,
        args=("GBPUSD", global_state, stop_scalping_gbpusd, mt5_connector, config_manager, logger, 3.0),
        daemon=True,
        name="ScalpingThread-GBPUSD"
    )

    # Thread dashboard
    thread_dashboard = threading.Thread(
        target=report_dashboard_thread,
        args=(global_state, stop_dashboard, logger),
        daemon=True,
        name="DashboardThread"
    )

    # Thread basket monitor (inchangé)
    thread_basket = threading.Thread(
        target=basket_monitor_thread,
        args=(stop_basket_monitor, ...),
        daemon=True,
        name="BasketMonitorThread"
    )

    # Démarrage
    logger.info("="*80)
    logger.info("🚀 DÉMARRAGE THREADS SCALPING MULTI-ACTIFS")
    logger.info("="*80)

    thread_usdjpy.start()
    thread_eurusd.start()
    thread_gbpusd.start()
    thread_dashboard.start()
    thread_basket.start()

    logger.info("✅ Tous les threads démarrés avec succès")
    logger.info("   → Appuyez sur Ctrl+C pour arrêter proprement")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("\n🛑 Arrêt demandé...")

        stop_scalping_usdjpy.set()
        stop_scalping_eurusd.set()
        stop_scalping_gbpusd.set()
        stop_dashboard.set()
        stop_basket_monitor.set()

        thread_usdjpy.join(timeout=5)
        thread_eurusd.join(timeout=5)
        thread_gbpusd.join(timeout=5)
        thread_dashboard.join(timeout=5)
        thread_basket.join(timeout=5)

        logger.info("✅ Tous les threads arrêtés proprement")
```

---

## 📝 NOTES IMPORTANTES

1. **OrderFlow V6** : Chaque actif a ses propres seuils (USDJPY delta=15 vs EURUSD delta=100)
2. **Timing Gatekeeper** : Heures GMT identiques pour tous actifs (0-7h, 14-16h)
3. **Régime de marché** : Indépendant par actif (USDJPY peut être "range" pendant que EURUSD est "trending")
4. **Basket Monitor** : Thread unique qui surveille les 3 actifs simultanément
5. **Performance** : 3 threads à 5s = 36 analyses/minute (vs 12 actuellement)

---

**FIN DU DOCUMENT**
