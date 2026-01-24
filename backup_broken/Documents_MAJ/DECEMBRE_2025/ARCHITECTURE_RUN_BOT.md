# 🏗️ ARCHITECTURE COMPLÈTE DE RUN_BOT.PY

## 📋 TABLE DES MATIÈRES
1. [Vue d'ensemble](#vue-densemble)
2. [Initialisation (main)](#initialisation-main)
3. [Architecture Multi-Threads](#architecture-multi-threads)
4. [Thread SCALPING (10s) - FAST LANE](#thread-scalping-10s---fast-lane)
5. [Thread LIQUIDITY (60s) - PIPELINE COMPLET](#thread-liquidity-60s---pipeline-complet)
6. [Thread BASKET MONITOR (100ms)](#thread-basket-monitor-100ms)
7. [Thread DATA ENGINE (5s)](#thread-data-engine-5s)
8. [Flux de Données](#flux-de-données)

---

## 🎯 VUE D'ENSEMBLE

Le bot SNIPER_X utilise une **architecture multi-threads** pour trader simultanément sur plusieurs timeframes et stratégies :

```
┌─────────────────────────────────────────────────────────────┐
│                    MAIN THREAD                              │
│  • Initialisation modules                                  │
│  • Lancement des 4 threads workers                         │
│  • Surveillance (Ctrl+C pour arrêt propre)                 │
└─────────────────────────────────────────────────────────────┘
                            │
        ┌───────────────────┼───────────────────┐
        │                   │                   │
        ▼                   ▼                   ▼
┌───────────────┐  ┌───────────────┐  ┌───────────────┐
│ DATA ENGINE   │  │  SCALPING     │  │  LIQUIDITY    │
│   Thread      │  │   Thread      │  │    Thread     │
│   Cycle 5s    │  │   Cycle 10s   │  │   Cycle 60s   │
└───────┬───────┘  └───────┬───────┘  └───────┬───────┘
        │                  │                   │
        │    ┌─────────────┴──────────────────┘
        │    │
        ▼    ▼
   ┌────────────────┐
   │ BASKET MONITOR │
   │    Thread      │
   │  Cycle 100ms   │
   └────────────────┘
```

---

## 🚀 INITIALISATION (MAIN)

### Étape 1 : Configuration & Logging
```python
def main(args):
    # 1. Setup logging (INFO/DEBUG/WARNING/ERROR/CRITICAL)
    setup_production_logging(log_level=args.log_level)

    # 2. ConfigManager (chargement configs JSON)
    config_manager = ConfigManager()

    # 3. Mode d'exécution (DEMO/LIVE/DRY_RUN)
    bot_mode = args.mode or config_manager.get("mode_execution", "DEMO")
    is_dry_run = args.dry_run

    # 4. Hot-Reload (kill -SIGUSR1 <PID> pour recharger config sans redémarrage)
    setup_hot_reload_handler()
```

### Étape 2 : Instanciation des Modules Fondamentaux
```python
    # 5. Modules principaux
    mt5_connector = MT5Connector()              # Connexion MetaTrader 5
    phase_observer = PhaseObserver()            # Détection phases de marché
    trade_executor = TradeExecutor()            # Exécution ordres MT5
    mecano = Mecano()                           # FusionManager (OF+FP+TR)
    strategy_manager = StrategyManager()        # Gestion stratégies (scalping/liquidity)
    decision_pipeline = DecisionPipeline()      # Pipeline décisionnel institutionnel
```

### Étape 3 : Vérification Environnement
```python
    # 6. Vérifications critiques
    verify_environment_and_config()

    # 7. Connexion MT5 persistante
    mt5_connector.connect(account_details)
    trade_executor.reconcile_state_with_broker()
```

### Étape 4 : Lancement des Threads
```python
    # 8. Création des threads workers
    data_engine = DataEngine(symbols=['XAUUSD'], cycle=5s)
    scalping_thread = Thread(target=scalping_fast_thread, cycle=10s)
    liquidity_thread = Thread(target=liquidity_main_thread, cycle=60s)
    basket_monitor = Thread(target=basket_monitor_thread, cycle=100ms)

    # 9. Démarrage
    data_engine.start()      # ✅ PREMIER (pré-remplir cache)
    scalping_thread.start()  # ✅ DEUXIÈME (consomme cache)
    liquidity_thread.start()
    basket_monitor.start()

    # 10. Boucle infinie (attend Ctrl+C)
    while True:
        time.sleep(1)
```

---

## 🧵 ARCHITECTURE MULTI-THREADS

### Global Context Partagé (Thread-Safe)
```python
global_context_shared = {}    # Dict partagé entre threads
context_lock = threading.Lock()  # Lock pour synchronisation

# Écriture (thread SCALPING/LIQUIDITY)
with context_lock:
    global_context["XAUUSD"] = market_results

# Lecture (thread BASKET_MONITOR)
with context_lock:
    xauusd_data = global_context.get("XAUUSD", {})
```

### Communication Inter-Threads
```
DATA ENGINE (5s)
    │
    ├──> Écrit dans footprint_cache
    │
    ▼
SCALPING (10s)
    │
    ├──> Lit footprint_cache (CACHE HIT ⚡)
    ├──> Écrit dans global_context["XAUUSD"]
    │
    ▼
BASKET MONITOR (100ms)
    │
    └──> Lit global_context["XAUUSD"]
         └──> Surveille P&L et ferme si +15 pips
```

---

## ⚡ THREAD SCALPING (10s) - FAST LANE

**Responsabilité** : Analyse ultra-rapide XAUUSD uniquement, exécution immédiate si signal

### Cycle 10 secondes

```
┌─────────────────────────────────────────────────────────────────┐
│ CYCLE SCALPING (toutes les 10 secondes)                        │
└─────────────────────────────────────────────────────────────────┘
                            │
            ┌───────────────┴───────────────┐
            ▼                               ▼
    ┌───────────────┐              ┌────────────────┐
    │ 1. GET RATES  │              │ 2. CACHE READ  │
    │   XAUUSD M1   │              │  Footprint 15s │
    │   200 bars    │              │  max age       │
    └───────┬───────┘              └────────┬───────┘
            │                               │
            │                    ┌──────────┴──────────┐
            │                    │ CACHE HIT? ⚡       │ 
            │                    └──────────┬──────────┘
            │                               │
            │               ┌───────────────┴───────────────┐
            │               │                               │
            │           YES │                           NO  │
            │               ▼                               ▼
            │    ┌──────────────────┐           ┌──────────────────┐
            │    │ Analyse RAPIDE   │           │ Analyse COMPLÈTE │
            │    │ (sans footprint) │           │ (avec ticks)     │
            │    │ + inject cache   │           │ FALLBACK rare   │
            │    └─────────┬────────┘           └─────────┬────────┘
            │              │                               │
            └──────────────┴───────────────────────────────┘
                           │
                           ▼
              ┌────────────────────────┐
              │ 3. MARKET ANALYZER     │
              │  • Phase detection     │
              │  • OrderFlow V6        │
              │  • Patterns            │
              │  • Features            │
              └────────────┬───────────┘
                           │
                           ▼
              ┌────────────────────────┐
              │ 4. STORE CONTEXT       │
              │  global_context[XAUUSD]│
              │  (avec lock)           │
              └────────────┬───────────┘
                           │
                           ▼
              ┌────────────────────────┐
              │ 5. FUSION MANAGER      │
              │  • OF + FP + TR        │
              │  • Scoring pondéré     │
              │  • Thresholds 65%      │
              └────────────┬───────────┘
                           │
                ┌──────────┴──────────┐
                │                     │
           HOLD │                     │ BUY/SELL
                ▼                     ▼
    ┌───────────────────┐   ┌────────────────────┐
    │ 📊 BILAN CONSOLIDÉ│   │ 6. BUILD TRADE     │
    │  Logs détaillés   │   │    DECISION        │
    │  Score insuffisant│   │  • burst_size=8    │
    └───────────────────┘   │  • SL/TP 300 pips  │
                            │  • Risk 1.5%       │
                            └─────────┬──────────┘
                                      │
                                      ▼
                            ┌────────────────────┐
                            │ 7. EXECUTE TRADE   │
                            │  run_trade_        │
                            │  execution_pipeline│
                            └─────────┬──────────┘
                                      │
                                      ▼
                            ┌────────────────────┐
                            │ 8. BURST MANAGER   │
                            │  Envoi 8 ordres MT5│
                            │  + Trade Logger    │
                            └────────────────────┘
```

### Code Simplifié
```python
def scalping_fast_thread():
    while not stop_event.is_set():
        # 1. Récupérer données XAUUSD
        rates_df = mt5_connector.get_rates("XAUUSD", "M1", 200)

        # 2. Vérifier cache footprint
        cached_footprint = footprint_cache.get("XAUUSD", max_age=15s)

        if cached_footprint:
            # CACHE HIT ⚡ (cas normal, 90% du temps)
            market_results = market_analyzer.analyze(rates_df, "XAUUSD", ticks=None)
            market_results['footprint'] = cached_footprint['footprint_summary']
            market_results['footprint_trigger'] = cached_footprint['trigger_data']
        else:
            # CACHE MISS (rare, DataEngine en retard)
            market_results = market_analyzer.analyze(rates_df, "XAUUSD")  # Avec ticks

        # 3. Stocker dans global_context
        with context_lock:
            global_context["XAUUSD"] = market_results

        # 4. FusionManager
        fusion_out = fusion_mgr.fuse(
            orderflow=market_results["orderflow_v6"],
            footprint=market_results["footprint"],
            triggers=market_results["footprint_trigger"],
            strategy_config=strat_cfg,
            context={"asset": "XAUUSD", "phase": market_results["phase"]}
        )

        # 5. Si signal valide → Exécution immédiate
        if fusion_out.get("ok"):  # Score >= 65%
            side = fusion_out["action"]  # BUY ou SELL
            conf = fusion_out["fused_confidence"]

            # Construction trade decision
            td = {
                "symbol": "XAUUSD",
                "action": side,
                "rule_name": "burst_scalping",
                "confidence": conf,
                "burst_size": 8,
                "sltp": {...}  # SL/TP 300 pips
            }

            # Exécution
            res = run_trade_execution_pipeline(trade_executor, decision_pkg)

        # 6. Attendre 10 secondes
        time.sleep(10)
```

---

## 🌊 THREAD LIQUIDITY (60s) - PIPELINE COMPLET

**Responsabilité** : Analyse multi-assets (EURUSD, GBPUSD, XAUUSD), pipeline décisionnel institutionnel

### Cycle 60 secondes

```
┌─────────────────────────────────────────────────────────────────┐
│ CYCLE LIQUIDITY (toutes les 60 secondes)                       │
└─────────────────────────────────────────────────────────────────┘
                            │
                            ▼
              ┌────────────────────────┐
              │ 1. BUILD GLOBAL CONTEXT│
              │  • Account info        │
              │  • Open positions      │
              │  • Asset configs       │
              │  • Market data         │
              └────────────┬───────────┘
                           │
                           ▼
              ┌────────────────────────┐
              │ 2. DECISION PIPELINE   │
              │    (Institutionnel)    │
              └────────────┬───────────┘
                           │
        ┌──────────────────┼──────────────────┐
        │                  │                  │
        ▼                  ▼                  ▼
┌────────────┐    ┌────────────┐    ┌────────────┐
│ EURUSD     │    │ GBPUSD     │    │ XAUUSD     │
│ Analysis   │    │ Analysis   │    │ Analysis   │
└──────┬─────┘    └──────┬─────┘    └──────┬─────┘
       │                 │                  │
       │    ┌────────────┴──────────┐       │
       │    │                       │       │
       ▼    ▼                       ▼       ▼
┌──────────────────┐      ┌──────────────────┐
│ Stratégie        │      │ Stratégie        │
│ SCALPING         │      │ LIQUIDITY        │
│ (fusion_scalping)│      │ (sweep, eqh_eql) │
└────────┬─────────┘      └────────┬─────────┘
         │                         │
         └────────────┬────────────┘
                      │
                      ▼
         ┌────────────────────────┐
         │ 3. MERGE DECISIONS     │
         │  scalping_decisions=[] │
         │  liquidity_decisions=[]│
         └────────────┬───────────┘
                      │
                      ▼
         ┌────────────────────────┐
         │ 4. FILTER (run_bot.py) │
         │  ONLY: rule_name=      │
         │   "fusion_scalping"    │
         │  AND asset="XAUUSD"    │
         │  AND fusion_data exists│
         └────────────┬───────────┘
                      │
           ┌──────────┴──────────┐
           │                     │
      EMPTY│                     │ DECISIONS
           ▼                     ▼
   ┌───────────────┐   ┌────────────────┐
   │ WHY_NO_TRADE  │   │ 5. EXECUTE     │
   │  Logs raisons │   │    TRADES      │
   └───────────────┘   └────────────────┘
```

### Code Simplifié
```python
def liquidity_main_thread():
    while not stop_event.is_set():
        # 1. Construction contexte global
        global_context = _build_global_context(
            mt5_connector,
            config_manager,
            mecano,
            strategy_manager,
            cycle_count
        )

        # 2. Decision Pipeline (analyse multi-assets)
        decision_package = decision_pipeline.run_decision_cycle(
            base_config=base_config,
            global_context=global_context
        )

        # 3. Extraction décisions
        scalping_decisions = decision_package.get("scalping_decisions", [])
        liquidity_decisions = decision_package.get("liquidity_decisions", [])

        # 4. Filtre SCALPING : FUSION-ONLY + XAUUSD + fusion_data valide
        scalping_decisions = [
            d for d in scalping_decisions
            if d.get("rule_name") == "fusion_scalping"
            and d.get("asset") == "XAUUSD"
            and d.get("fusion_data")  # ✅ Bloque trades à 0%
            and d.get("fusion_data", {}).get("fused_confidence", 0) > 0
        ]

        # 5. Exécution
        if scalping_decisions:
            for td in scalping_decisions:
                run_trade_execution_pipeline(trade_executor, {...})

        # 6. Attendre 60 secondes
        time.sleep(60)
```

---

## 🔍 THREAD BASKET MONITOR (100ms)

**Responsabilité** : Surveillance continue des baskets ouverts, fermeture automatique +15 pips

### Cycle 100ms (ultra-rapide)

```
┌─────────────────────────────────────────────────────────────────┐
│ CYCLE BASKET MONITOR (toutes les 100 millisecondes)            │
└─────────────────────────────────────────────────────────────────┘
                            │
                            ▼
              ┌────────────────────────┐
              │ 1. GET OPEN POSITIONS  │
              │    from MT5            │
              └────────────┬───────────┘
                           │
                           ▼
              ┌────────────────────────┐
              │ 2. GROUP BY BASKET_ID  │
              │  (comment field)       │
              └────────────┬───────────┘
                           │
                           ▼
              ┌────────────────────────┐
              │ 3. FOR EACH BASKET     │
              │  • Calcul P&L total    │
              │  • Âge du basket       │
              │  • Nombre positions    │
              └────────────┬───────────┘
                           │
        ┌──────────────────┼──────────────────┐
        │                  │                  │
        ▼                  ▼                  ▼
┌────────────┐    ┌────────────┐    ┌────────────┐
│ PROFIT     │    │ LOSS GUARD │    │ TRAILING   │
│ +15 pips   │    │ -48 pips   │    │ (if SL hit)│
│ → CLOSE    │    │ → CLOSE    │    │ → CLOSE    │
└────────────┘    └────────────┘    └────────────┘
```

### Code Simplifié
```python
def basket_monitor_thread():
    while not stop_event.is_set():
        # Configuration
        closure_rules = config.get("entry_rules.scalping.burst_scalping.closure_rules")
        target_profit_pips = 15.0  # Fermeture à +15 pips
        max_loss_pips = 48.0       # Loss guard à -48 pips

        # Surveiller tous les baskets
        trade_executor.monitor_burst_baskets(
            config=config,
            target_profit_pips=target_profit_pips,
            max_loss_pips=max_loss_pips
        )

        # Attendre 100ms
        time.sleep(0.1)
```

---

## 💾 THREAD DATA ENGINE (5s)

**Responsabilité** : Pré-calcul asynchrone du Footprint, stockage dans cache

### Cycle 5 secondes

```
┌─────────────────────────────────────────────────────────────────┐
│ CYCLE DATA ENGINE (toutes les 5 secondes)                      │
└─────────────────────────────────────────────────────────────────┘
                            │
                            ▼
              ┌────────────────────────┐
              │ 1. FOR EACH SYMBOL     │
              │    ['XAUUSD']          │
              └────────────┬───────────┘
                           │
                           ▼
              ┌────────────────────────┐
              │ 2. GET TICKS           │
              │   Dernière minute M1   │
              │   (59 secondes)        │
              └────────────┬───────────┘
                           │
                           ▼
              ┌────────────────────────┐
              │ 3. ANALYZE FOOTPRINT   │
              │  market_analyzer.      │
              │  _analyze_footprint_m1 │
              │  • Delta par niveau    │
              │  • POC, Absorption     │
              │  • Triggers            │
              └────────────┬───────────┘
                           │
                           ▼
              ┌────────────────────────┐
              │ 4. STORE IN CACHE      │
              │  footprint_cache.set(  │
              │    symbol="XAUUSD",    │
              │    data={...}          │
              │  )                     │
              └────────────┬───────────┘
                           │
                           ▼
              ┌────────────────────────┐
              │ 5. LOG PERF            │
              │  ✅ ticks=304          │
              │  coverage=59.0s        │
              │  analysis=617.0ms      │
              └────────────────────────┘
```

### Avantages du Cache Asynchrone
```
SANS DATA ENGINE (ancien système):
  Scalping Thread (10s) → calcul footprint → 617ms latence

AVEC DATA ENGINE (nouveau système):
  Data Engine (5s) → calcul footprint → cache → 0ms latence
  Scalping Thread (10s) → lecture cache → 617ms gagnés ⚡

Gain de performance: 71% (617ms → 0ms)
```

---

## 🔄 FLUX DE DONNÉES COMPLET

### Vue Temporelle (1 minute)

```
Temps (secondes)
│
0s  ─┬─ DATA ENGINE     : Calcul Footprint → Cache
     │
5s  ─┼─ DATA ENGINE     : Calcul Footprint → Cache
     │
10s ─┼─ SCALPING THREAD : Lecture Cache → FusionManager → HOLD (score 49%)
     │  📊 BILAN CONSOLIDÉ
     │
15s ─┼─ DATA ENGINE     : Calcul Footprint → Cache
     │
20s ─┼─ SCALPING THREAD : Lecture Cache → FusionManager → HOLD (score 52%)
     │  📊 BILAN CONSOLIDÉ
     │
25s ─┼─ DATA ENGINE     : Calcul Footprint → Cache
     │
30s ─┼─ SCALPING THREAD : Lecture Cache → FusionManager → BUY ✅ (score 69%)
     │  🎯 TRADE EXÉCUTÉ
     │
35s ─┼─ DATA ENGINE     : Calcul Footprint → Cache
     │
40s ─┼─ SCALPING THREAD : Lecture Cache → FusionManager → HOLD (score 45%)
     │  📊 BILAN CONSOLIDÉ
     │
45s ─┼─ DATA ENGINE     : Calcul Footprint → Cache
     │
50s ─┼─ SCALPING THREAD : Lecture Cache → FusionManager → HOLD (score 51%)
     │  📊 BILAN CONSOLIDÉ
     │
55s ─┼─ DATA ENGINE     : Calcul Footprint → Cache
     │
60s ─┴─ LIQUIDITY THREAD: Pipeline Complet → EURUSD/GBPUSD/XAUUSD
       🤖 [DECISION PIPELINE]
       📦 Décisions multiples détectées
       ✅ TRADE DÉCIDÉ (si signal valide)
```

### Flux de Décision (SCALPING FAST-LANE)

```
┌──────────────────────────────────────────────────────────────────┐
│                    SCALPING FAST-LANE (10s)                      │
└──────────────────────────────────────────────────────────────────┘
        │
        ▼
┌──────────────────┐
│ MT5 Connector    │  get_rates("XAUUSD", "M1", 200 bars)
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│ Footprint Cache  │  get("XAUUSD", max_age=15s)
│  (from DataEngine│  → CACHE HIT ⚡ (90% du temps)
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│ Market Analyzer  │  analyze(rates_df, "XAUUSD", ticks=None)
│  • Phase         │  → range_accumulation, trending_bear, etc.
│  • OrderFlow V6  │  → score=19.1%, delta=-165.9, status=SUSPECT
│  • Patterns      │  → bos_detected, sweep_detected, etc.
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│ Global Context   │  with context_lock:
│  (thread-safe)   │    global_context["XAUUSD"] = market_results
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│ FusionManager    │  fuse(orderflow, footprint, triggers, cfg, ctx)
│  • OF: 19.1%     │
│  • FP: 80.0%     │  → Score Base = (19.1 + 80.0) / 2 = 49.5%
│  • TR: none      │  → Boost Trigger = 0%
│                  │  → Score Final = 49.5%
└────────┬─────────┘
         │
         ├──────────────┬──────────────┐
         │              │              │
    Score < 65%    65% ≤ Score < 80%  Score ≥ 80%
         │              │              │
         ▼              ▼              ▼
   ┌─────────┐    ┌─────────┐    ┌──────────┐
   │  HOLD   │    │ CAUTIOUS│    │   HIGH   │
   │ (WAIT)  │    │  ENTRY  │    │ CONVICTION│
   └────┬────┘    └────┬────┘    └─────┬────┘
        │              │               │
        │              └───────┬───────┘
        │                      │
        ▼                      ▼
┌────────────────┐   ┌─────────────────────┐
│ 📊 BILAN       │   │ Trade Execution     │
│  CONSOLIDÉ     │   │  • Burst 8 ordres   │
│  Logs détaillés│   │  • SL/TP 300 pips   │
│  Score 49.5%   │   │  • Risk 1.5%        │
│  INSUFFISANT   │   │  • Volume 0.21 lots │
└────────────────┘   └──────────┬──────────┘
                                │
                                ▼
                     ┌─────────────────────┐
                     │ MT5 Order Execution │
                     │  8 ordres envoyés   │
                     │  + Trade Logger     │
                     └─────────────────────┘
```

---

## 📊 POINTS CLÉS DE L'ARCHITECTURE

### 1. Double Vérification (Defense in Depth)
```
FAST-LANE (10s)          PIPELINE (60s)
     │                        │
     ├─ FusionManager         ├─ DecisionPipeline
     │  Score >= 65%?         │  • Contexte global
     │                        │  • Multi-strategies
     │                        │  • Final validation
     │                        │
     └────────┬───────────────┘
              │
              ▼
         Trade Execution
```

### 2. Filtre de Sécurité (run_bot.py:2460-2469)
```python
# ✅ BLOQUEUR CRITIQUE: Rejeter trades invalides
scalping_decisions = [
    d for d in decision_package.get("scalping_decisions")
    if d.get("rule_name") == "fusion_scalping"      # ✅ Fusion uniquement
    and d.get("asset") == "XAUUSD"                  # ✅ XAUUSD uniquement
    and d.get("fusion_data")                        # ✅ Données fusion présentes
    and d.get("fusion_data", {}).get("fused_confidence") > 0  # ✅ Score > 0%
]
```

### 3. Cache Asynchrone (Performance Boost)
```
Ancien système (synchrone):
  ┌─────────────┐
  │ SCALPING    │ ──┐
  │ Thread      │   │
  └─────────────┘   │
                    ├─ 617ms latence (calcul footprint)
  ┌─────────────┐   │
  │ LIQUIDITY   │ ──┘
  │ Thread      │
  └─────────────┘

Nouveau système (asynchrone):
  ┌─────────────┐
  │ DATA ENGINE │ ──> Calcul footprint → Cache (617ms)
  │ Thread (5s) │
  └─────────────┘
         │
         │ Cache partagé
         │
  ┌──────┴──────┐
  │             │
  ▼             ▼
┌─────────┐ ┌──────────┐
│SCALPING │ │LIQUIDITY │ ──> Lecture cache (0ms) ⚡
│Thread   │ │Thread    │
└─────────┘ └──────────┘

Gain: 71% performance (617ms → 0ms)
```

### 4. Thread Safety (Synchronisation)
```python
# Global context partagé avec lock
global_context_shared = {}
context_lock = threading.Lock()

# Écriture (SCALPING/LIQUIDITY threads)
with context_lock:
    global_context["XAUUSD"] = market_results

# Lecture (BASKET_MONITOR thread)
with context_lock:
    data = global_context.get("XAUUSD", {})
```

---

## 🎯 RÉSUMÉ DES RESPONSABILITÉS

| Thread         | Cycle | Responsabilité                             | Assets      |
| -------------- | ----- | ------------------------------------------ | ----------- |
| DATA ENGINE    | 5s    | Pré-calcul Footprint → Cache               | XAUUSD      |
| SCALPING       | 10s   | Analyse rapide + FusionManager + Exécution | XAUUSD      |
| LIQUIDITY      | 60s   | Pipeline complet multi-assets              | ALL (3)     |
| BASKET MONITOR | 100ms | Surveillance P&L + Auto-close +15 pips     | ALL baskets |

### Flux Décisionnel
```
DATA ENGINE (5s) → Cache Footprint
    │
    ▼
SCALPING (10s) → Lecture Cache → FusionManager → HOLD/BUY/SELL
    │                                │
    │                                ├─ HOLD → 📊 Bilan Consolidé (logs)
    │                                └─ BUY/SELL → Trade Execution
    │
    ▼
LIQUIDITY (60s) → Pipeline Complet → Validation finale
    │
    └─ Filtre fusion_scalping + XAUUSD + fusion_data valide
       │
       └─ Trade Execution (si validé)
```

---

## 📝 NOTES IMPORTANTES

1. **XAUUSD uniquement** : Le filtre ligne 2465 bloque EURUSD/GBPUSD
2. **Cache obligatoire** : DataEngine DOIT tourner, sinon CACHE MISS
3. **Seuils configurables** : 60/65/80% dans config_trade_scalping.json
4. **Pénalités allégées** : OrderFlow rescue_level=2 → -10 pts (au lieu de -25)
5. **Pas de cap** : OrderFlow peut scorer >69% même avec rescue_level=2

---

FIN DU DOCUMENT
