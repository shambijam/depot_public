# 🔧 IMPLÉMENTATION MULTI-THREADING - 31 Décembre 2025

**Date**: 31 Décembre 2025
**Objectif**: Migrer de 1 thread USDJPY vers 3 threads indépendants (USDJPY, EURUSD, GBPUSD)
**Référence**: ARCHITECTURE_MULTI_THREAD_SCALPING.md

---

## 📊 ARCHITECTURE ACTUELLE VS CIBLE

### ❌ ARCHITECTURE ACTUELLE (1 thread)
```
run_bot.py
├─ scalping_fast_thread() → Analyse USDJPY uniquement
│  ├─ MarketAnalyzer (USDJPY)
│  ├─ ScalpingStrategy (USDJPY)
│  ├─ OrderFlow V6 scoring
│  ├─ Timing gatekeeper
│  └─ Trade execution
│
├─ basket_monitor_thread() → Monitor positions
└─ global_context["USDJPY"] → Résultats partagés
```

**Problèmes**:
- EURUSD et GBPUSD jamais analysés
- Message "LIQUIDITY Thread" obsolète (ligne 3980)
- Impossible de scaler à plusieurs paires

### ✅ ARCHITECTURE CIBLE (3 threads + dashboard)
```
run_bot.py
├─ GlobalScalpingState (NEW)
│  ├─ threading.Lock
│  ├─ asset_states["USDJPY"] → {regime, score, signal, timing}
│  ├─ asset_states["EURUSD"] → {regime, score, signal, timing}
│  └─ asset_states["GBPUSD"] → {regime, score, signal, timing}
│
├─ scalping_worker("USDJPY", offset=0.0s) → Thread 1
├─ scalping_worker("EURUSD", offset=1.5s) → Thread 2
├─ scalping_worker("GBPUSD", offset=3.0s) → Thread 3
│  │
│  └─ Chaque worker:
│     ├─ MarketAnalyzer indépendant
│     ├─ ScalpingStrategy indépendant
│     ├─ OrderFlow V6 scoring
│     ├─ Timing gatekeeper
│     ├─ Trade execution
│     └─ Update GlobalScalpingState (thread-safe)
│
├─ dashboard_worker() → Thread 4 (affichage agrégé 30s)
└─ basket_monitor_thread() → Thread 5 (positions existant)
```

**Avantages**:
- 3 paires analysées simultanément
- Logs compacts (1 ligne par asset)
- Dashboard agrégé lisible
- Offsets échelonnés (évite collision console)
- Régime détection indépendante par asset

---

## 🔧 CODE COMPLET À IMPLÉMENTER

### ÉTAPE 1: Classe GlobalScalpingState (NOUVEAU)

**Emplacement**: run_bot.py, AVANT la fonction scalping_fast_thread (vers ligne 2960)

```python
# ═══════════════════════════════════════════════════════════════════════════
# 🎯 CLASSE GLOBALE: State Management Multi-Threading (31 DEC 2025)
# ═══════════════════════════════════════════════════════════════════════════

class GlobalScalpingState:
    """
    Classe thread-safe pour gérer l'état partagé entre les 3 threads scalping.

    Chaque thread (USDJPY, EURUSD, GBPUSD) met à jour son state indépendamment.
    Le dashboard thread lit l'état agrégé pour affichage console.

    Attributs:
    - asset_states: Dict[str, dict] - État par asset
    - lock: threading.Lock - Synchronisation thread-safe
    """

    def __init__(self, assets: list):
        """
        Initialise le state global pour tous les assets.

        Args:
            assets: Liste des symboles (ex: ["USDJPY", "EURUSD", "GBPUSD"])
        """
        self.lock = threading.Lock()
        self.asset_states = {}

        for asset in assets:
            self.asset_states[asset] = {
                # Market analysis
                "regime": "UNKNOWN",
                "regime_force": 0.0,

                # OrderFlow V6
                "of_score": 0.0,
                "of_bias": "NEUTRAL",
                "of_quality": "NO_TRADE",

                # Timing gatekeeper
                "timing_status": "UNKNOWN",
                "tick_rate": 0.0,
                "coverage_s": 0.0,

                # Decision
                "action": "HOLD",
                "confidence": 0.0,

                # Performance
                "last_update": None,
                "cycle_count": 0,
                "errors_count": 0,
                "last_error": None,
            }

    def update_asset_state(self, asset: str, updates: dict):
        """
        Met à jour l'état d'un asset (thread-safe).

        Args:
            asset: Symbole (ex: "USDJPY")
            updates: Dict avec clés à mettre à jour
        """
        with self.lock:
            if asset not in self.asset_states:
                return

            self.asset_states[asset].update(updates)
            self.asset_states[asset]["last_update"] = time.time()

    def get_asset_state(self, asset: str) -> dict:
        """
        Récupère l'état d'un asset (thread-safe).

        Args:
            asset: Symbole

        Returns:
            Dict avec état actuel (copie)
        """
        with self.lock:
            if asset not in self.asset_states:
                return {}
            return self.asset_states[asset].copy()

    def get_all_states(self) -> dict:
        """
        Récupère l'état de tous les assets (thread-safe).

        Returns:
            Dict[str, dict] - Copie complète du state
        """
        with self.lock:
            return {
                asset: state.copy()
                for asset, state in self.asset_states.items()
            }

    def increment_cycle(self, asset: str):
        """Incrémente le compteur de cycles pour un asset."""
        with self.lock:
            if asset in self.asset_states:
                self.asset_states[asset]["cycle_count"] += 1

    def record_error(self, asset: str, error_msg: str):
        """Enregistre une erreur pour un asset."""
        with self.lock:
            if asset in self.asset_states:
                self.asset_states[asset]["errors_count"] += 1
                self.asset_states[asset]["last_error"] = error_msg
```

---

### ÉTAPE 2: Fonction scalping_worker générique (NOUVEAU)

**Emplacement**: run_bot.py, REMPLACE la fonction scalping_fast_thread actuelle (ligne 2966)

**IMPORTANT**: Cette fonction est une VERSION GÉNÉRALISÉE de scalping_fast_thread.
Elle accepte un paramètre `asset` dynamique au lieu d'avoir "USDJPY" hardcodé.

```python
# ═══════════════════════════════════════════════════════════════════════════
# 🎯 WORKER THREAD: Scalping générique multi-asset (31 DEC 2025)
# ═══════════════════════════════════════════════════════════════════════════

def scalping_worker(
    asset: str,
    global_state: GlobalScalpingState,
    offset_seconds: float,
    mt5_connector,
    decision_pipeline,
    trade_executor,
    config_manager,
    mecano,
    strategy_manager,
    is_dry_run: bool,
    stop_event: threading.Event,
    logger
):
    """
    🎯 Worker thread dédié au SCALPING pour un asset spécifique.

    Architecture simplifiée (31 DEC 2025):
    - Analyse M1 pour l'asset fourni
    - Timing Gatekeeper → PASS/VETO
    - OrderFlow V6 → Scoring 0-100
    - MarketAnalyzer.build_decision() → BUY/SELL/HOLD
    - Update GlobalScalpingState (thread-safe)

    Args:
        asset: Symbole forex (ex: "USDJPY", "EURUSD", "GBPUSD")
        global_state: Instance GlobalScalpingState pour state partagé
        offset_seconds: Délai avant démarrage (staggered timing)
        [... autres params identiques à scalping_fast_thread]
    """
    # Import MarketAnalyzer au début pour éviter conflit de portée
    from phase_observer.market_analyzer import MarketAnalyzer
    from strategy.scalping import ScalpingStrategy
    import pandas as pd

    # 🎯 OFFSET DE DÉMARRAGE (staggered timing)
    if offset_seconds > 0:
        logger.info(f"[{asset}] ⏱️  Délai démarrage: {offset_seconds:.1f}s")
        time.sleep(offset_seconds)

    cycle_interval = 5  # 5 secondes
    cycle_count = 0

    logger.info(f"🚀 [{asset}] Worker démarré (cycle 5s)")

    # ✅ Instancier MarketAnalyzer et ScalpingStrategy pour cet asset
    try:
        market_analyzer_thread = MarketAnalyzer(config_manager=config_manager, logger=logger)
        logger.info(f"✅ [{asset}] MarketAnalyzer instancié")
    except Exception as e:
        logger.error(f"❌ [{asset}] Impossible de créer MarketAnalyzer: {e}")
        global_state.record_error(asset, f"MarketAnalyzer init failed: {e}")
        return  # Arrêt du thread si MarketAnalyzer échoue

    try:
        strat_cfg = strategy_manager.get_strategy_config("scalping") or {}
        scalping_strategy = ScalpingStrategy(
            config_manager=config_manager,
            strategy_config=strat_cfg,
            mt5_connector=mt5_connector,
            logger=logger
        )
        logger.info(f"✅ [{asset}] ScalpingStrategy instanciée")
    except Exception as e:
        logger.warning(f"⚠️ [{asset}] ScalpingStrategy init failed: {e}")
        scalping_strategy = None

    # Squelette trade decision (pré-calcul config statique)
    trade_decision_skeleton = None
    last_config_update = 0

    # ═══════════════════════════════════════════════════════════════
    # 🔄 BOUCLE PRINCIPALE DU WORKER
    # ═══════════════════════════════════════════════════════════════

    while not stop_event.is_set():
        cycle_count += 1
        cycle_start = time.time()
        global_state.increment_cycle(asset)

        try:
            # ========== CHARGEMENT DONNÉES ==========
            from core.bars_cache import bars_cache

            # Charger barres M1 pour cet asset
            rates_df = bars_cache.get_or_fetch(
                symbol=asset,
                timeframe="M1",
                count=50,
                mt5_connector=mt5_connector,
                ttl_seconds=60.0,
            )

            if rates_df is None or rates_df.empty:
                logger.warning(f"[{asset}] Données M1 indisponibles")
                global_state.update_asset_state(asset, {
                    "regime": "NO_DATA",
                    "action": "HOLD"
                })
                time.sleep(cycle_interval)
                continue

            # ========== CHARGEMENT TICKS ==========
            ticks_df = None
            try:
                last_candle = rates_df.iloc[-2] if len(rates_df) >= 2 else rates_df.iloc[-1]

                if "time" in rates_df.columns:
                    candle_start = pd.to_datetime(last_candle["time"], utc=True, errors="coerce")
                else:
                    candle_start = pd.to_datetime(last_candle.name, utc=True, errors="coerce")

                if pd.isna(candle_start):
                    candle_start = pd.Timestamp.utcnow() - pd.Timedelta(minutes=1)

                candle_end = candle_start + pd.Timedelta(minutes=1)

                ticks_df = mt5_connector.get_ticks_for_candle(
                    asset,
                    candle_start.to_pydatetime(),
                    candle_end.to_pydatetime()
                )

                if ticks_df is not None and not ticks_df.empty:
                    logger.debug(f"[{asset}] ✅ {len(ticks_df)} ticks chargés")
                else:
                    ticks_df = None
            except Exception as e_ticks:
                logger.debug(f"[{asset}] ⚠️ Ticks unavailable: {e_ticks}")
                ticks_df = None

            # ========== ANALYSE MARKET ==========
            try:
                market_results = market_analyzer_thread.analyze(
                    asset=asset,
                    df=rates_df,
                    ticks=ticks_df
                )
                logger.debug(f"[{asset}] market_analyzer.analyze() OK")
            except Exception as e_analysis:
                logger.error(f"[{asset}] Erreur analyze(): {e_analysis}")
                global_state.record_error(asset, f"analyze() failed: {e_analysis}")
                market_results = {"latest": {}, "annotated_df": rates_df}

            # ========== EXTRACTION RÉSULTATS ==========
            latest = market_results.get("latest", {})
            phase_info = market_results.get("phase", {})
            regime = phase_info.get("regime", "UNKNOWN")
            regime_force = phase_info.get("force", 0.0)

            # ========== ORDERFLOW V6 SCORING ==========
            orderflow_result = {"score": 0.0, "bias": "NEUTRAL", "signal_quality": "NO_TRADE"}

            if scalping_strategy and latest:
                try:
                    orderflow_result = scalping_strategy.compute_orderflow_v6(
                        asset=asset,
                        latest=latest,
                        annotated_df=market_results.get("annotated_df")
                    )
                except Exception as e_of:
                    logger.error(f"[{asset}] OrderFlow V6 error: {e_of}")
                    global_state.record_error(asset, f"OrderFlow V6 failed: {e_of}")

            of_score = orderflow_result.get("total_score", 0.0)
            of_bias = orderflow_result.get("bias", "NEUTRAL")
            of_quality = orderflow_result.get("signal_quality", "NO_TRADE")

            # ========== TIMING GATEKEEPER ==========
            timing_result = latest.get("timing_gatekeeper", {})
            timing_status = timing_result.get("status", "UNKNOWN")
            tick_rate = timing_result.get("tick_rate", 0.0)
            coverage_s = timing_result.get("coverage_s", 0.0)

            # ========== DÉCISION FINALE ==========
            decision = {"action": "HOLD", "confidence": 0.0}

            # Logique simplifiée: Trade si timing PASS + score >= 65
            if timing_status == "PASS" and of_score >= 65.0:
                if of_bias == "BUY":
                    decision["action"] = "BUY"
                    decision["confidence"] = of_score / 100.0
                elif of_bias == "SELL":
                    decision["action"] = "SELL"
                    decision["confidence"] = of_score / 100.0

            # ========== UPDATE GLOBAL STATE ==========
            global_state.update_asset_state(asset, {
                "regime": regime,
                "regime_force": regime_force,
                "of_score": of_score,
                "of_bias": of_bias,
                "of_quality": of_quality,
                "timing_status": timing_status,
                "tick_rate": tick_rate,
                "coverage_s": coverage_s,
                "action": decision["action"],
                "confidence": decision["confidence"]
            })

            # ========== LOG COMPACT (1 ligne) ==========
            # Format: [ASSET] Regime | OF: score/bias | Timing: status | Action
            logger.info(
                f"[{asset}] "
                f"R:{regime[:4]}({regime_force:.1f}) | "
                f"OF:{of_score:.0f}/{of_bias[:3]} | "
                f"T:{timing_status[:4]} | "
                f"→{decision['action']}"
            )

            # Si score élevé, afficher détails
            if of_score >= 75.0:
                logger.critical(
                    f"[{asset}] 🎯 SIGNAL FORT: "
                    f"Score={of_score:.0f} ({of_quality}) | "
                    f"Bias={of_bias} | "
                    f"Timing={timing_status} | "
                    f"Decision={decision['action']}"
                )

            # ========== EXÉCUTION TRADE (si applicable) ==========
            # TODO: Implémenter logique trade execution identique à scalping_fast_thread
            # Pour l'instant: skeleton seulement (pas d'exécution réelle)

        except Exception as e:
            logger.error(f"[{asset}] Erreur cycle: {e}", exc_info=True)
            global_state.record_error(asset, str(e))

        # ========== ATTENTE PROCHAIN CYCLE ==========
        elapsed = time.time() - cycle_start
        sleep_time = max(0.1, cycle_interval - elapsed)
        time.sleep(sleep_time)

    logger.info(f"🛑 [{asset}] Worker arrêté")
```

---

### ÉTAPE 3: Dashboard Worker (NOUVEAU)

**Emplacement**: run_bot.py, APRÈS scalping_worker (vers ligne 3300)

```python
# ═══════════════════════════════════════════════════════════════════════════
# 📊 DASHBOARD THREAD: Affichage agrégé (31 DEC 2025)
# ═══════════════════════════════════════════════════════════════════════════

def dashboard_worker(
    global_state: GlobalScalpingState,
    stop_event: threading.Event,
    logger
):
    """
    Thread dashboard: affiche état agrégé des 3 assets toutes les 30 secondes.

    Format table:
    ╔══════════════════════════════════════════════════════════════╗
    ║ DASHBOARD SCALPING - 31 DEC 2025 03:58:45 GMT               ║
    ╠══════════════════════════════════════════════════════════════╣
    ║ USDJPY │ RANGE(0.7) │ OF:65/BUY │ PASS │ →BUY   │ C:142     ║
    ║ EURUSD │ TREND(0.8) │ OF:45/SEL │ VETO │ →HOLD  │ C:141     ║
    ║ GBPUSD │ UNKN(0.0)  │ OF:0/NEU  │ VETO │ →HOLD  │ C:140     ║
    ╚══════════════════════════════════════════════════════════════╝
    """
    dashboard_interval = 30  # 30 secondes

    logger.info("📊 [DASHBOARD] Thread démarré (affichage 30s)")

    while not stop_event.is_set():
        try:
            time.sleep(dashboard_interval)

            # Récupérer états
            all_states = global_state.get_all_states()

            # Header
            now_gmt = pd.Timestamp.utcnow().strftime("%d %b %Y %H:%M:%S GMT")
            logger.info("=" * 80)
            logger.info(f"📊 DASHBOARD SCALPING - {now_gmt}")
            logger.info("=" * 80)

            # Table header
            logger.info(
                f"{'ASSET':<8} │ {'REGIME':<11} │ {'ORDERFLOW':<12} │ "
                f"{'TIM':<4} │ {'ACTION':<6} │ {'CYCLES':<8}"
            )
            logger.info("─" * 80)

            # Lignes par asset
            for asset in ["USDJPY", "EURUSD", "GBPUSD"]:
                state = all_states.get(asset, {})

                regime = state.get("regime", "UNKNOWN")[:4]
                regime_force = state.get("regime_force", 0.0)
                of_score = state.get("of_score", 0.0)
                of_bias = state.get("of_bias", "NEUTRAL")[:3]
                timing = state.get("timing_status", "UNKNOWN")[:4]
                action = state.get("action", "HOLD")
                cycles = state.get("cycle_count", 0)
                errors = state.get("errors_count", 0)

                # Format ligne
                regime_str = f"{regime}({regime_force:.1f})"
                of_str = f"{of_score:.0f}/{of_bias}"
                action_str = f"→{action}"
                cycle_str = f"C:{cycles}"
                if errors > 0:
                    cycle_str += f" E:{errors}"

                logger.info(
                    f"{asset:<8} │ {regime_str:<11} │ {of_str:<12} │ "
                    f"{timing:<4} │ {action_str:<6} │ {cycle_str:<8}"
                )

            logger.info("=" * 80)

        except Exception as e:
            logger.error(f"[DASHBOARD] Erreur affichage: {e}")

    logger.info("🛑 [DASHBOARD] Thread arrêté")
```

---

### ÉTAPE 4: Modifications run_bot.py - Lancement threads (REMPLACER)

**Emplacement**: run_bot.py, fonction `run_bot_live()`, section création/lancement threads (lignes 3976-4060)

**CODE ACTUEL À REMPLACER**:
```python
# ANCIEN CODE (lignes 3976-4060 environ)
logger.info("🚀 DÉMARRAGE DES THREADS SÉPARÉS")
logger.info("=" * 80)
logger.info("  • DATAENGINE Thread     : Cycle 5s (Analyse Footprint asynchrone) [USDJPY]")
logger.info("  • SCALPING Thread       : Cycle 5s (USDJPY UNIQUEMENT) ⚡")
logger.info("  • LIQUIDITY Thread      : Cycle 60s (EURUSD, GBPUSD) ← USDJPY exclu")  # ❌ OBSOLÈTE
logger.info("  • BASKET MONITOR Thread : Surveillance continue (polling 100ms)")
logger.info("=" * 80)

# ... création contexte global ...

scalping_thread = threading.Thread(
    target=scalping_fast_thread,
    args=(mt5_connector, decision_pipeline, trade_executor, ...)
)

basket_monitor = threading.Thread(
    target=monitor_burst_baskets,
    args=(...)
)

scalping_thread.start()
basket_monitor.start()
```

**NOUVEAU CODE** (31 DEC 2025):
```python
# ═══════════════════════════════════════════════════════════════════════════
# 🚀 LANCEMENT MULTI-THREADING (31 DEC 2025)
# ═══════════════════════════════════════════════════════════════════════════

logger.info("🚀 DÉMARRAGE MULTI-THREADING SCALPING")
logger.info("=" * 80)
logger.info("  • SCALPING USDJPY Thread : Cycle 5s (offset 0.0s)")
logger.info("  • SCALPING EURUSD Thread : Cycle 5s (offset 1.5s)")
logger.info("  • SCALPING GBPUSD Thread : Cycle 5s (offset 3.0s)")
logger.info("  • DASHBOARD Thread       : Affichage agrégé 30s")
logger.info("  • BASKET MONITOR Thread  : Surveillance continue (polling 100ms)")
logger.info("=" * 80)

# ✅ Créer GlobalScalpingState
assets = ["USDJPY", "EURUSD", "GBPUSD"]
global_scalping_state = GlobalScalpingState(assets)

# ✅ Events pour arrêt propre
scalping_stop_event = threading.Event()
dashboard_stop_event = threading.Event()
basket_monitor_stop_event = threading.Event()

# ✅ Créer les 3 threads scalping (staggered timing)
thread_usdjpy = threading.Thread(
    target=scalping_worker,
    args=(
        "USDJPY",                    # asset
        global_scalping_state,       # global state
        0.0,                         # offset: démarre immédiatement
        mt5_connector,
        decision_pipeline,
        trade_executor,
        config_manager,
        mecano,
        strategy_manager,
        is_dry_run,
        scalping_stop_event,
        logger
    ),
    name="ScalpingWorker-USDJPY"
)

thread_eurusd = threading.Thread(
    target=scalping_worker,
    args=(
        "EURUSD",                    # asset
        global_scalping_state,       # global state
        1.5,                         # offset: 1.5s après USDJPY
        mt5_connector,
        decision_pipeline,
        trade_executor,
        config_manager,
        mecano,
        strategy_manager,
        is_dry_run,
        scalping_stop_event,
        logger
    ),
    name="ScalpingWorker-EURUSD"
)

thread_gbpusd = threading.Thread(
    target=scalping_worker,
    args=(
        "GBPUSD",                    # asset
        global_scalping_state,       # global state
        3.0,                         # offset: 3.0s après USDJPY
        mt5_connector,
        decision_pipeline,
        trade_executor,
        config_manager,
        mecano,
        strategy_manager,
        is_dry_run,
        scalping_stop_event,
        logger
    ),
    name="ScalpingWorker-GBPUSD"
)

# ✅ Créer thread dashboard
thread_dashboard = threading.Thread(
    target=dashboard_worker,
    args=(
        global_scalping_state,
        dashboard_stop_event,
        logger
    ),
    name="Dashboard"
)

# ✅ Créer thread basket monitor (inchangé)
basket_monitor = threading.Thread(
    target=monitor_burst_baskets,
    args=(
        mt5_connector,
        trade_executor,
        mecano,
        config_manager,
        is_dry_run,
        basket_monitor_stop_event,
        global_context,  # ⚠️ Garder pour compatibilité
        context_lock,
        logger
    ),
    daemon=True,
    name="BasketMonitor"
)

# ✅ Démarrer tous les threads
thread_usdjpy.start()
thread_eurusd.start()
thread_gbpusd.start()
thread_dashboard.start()
basket_monitor.start()

logger.info("✅ Tous les threads démarrés avec succès")
logger.info("   → Appuyez sur Ctrl+C pour arrêter proprement")
logger.info("=" * 80)

# ✅ Attendre interruption
try:
    while True:
        time.sleep(1)

except KeyboardInterrupt:
    logger.warning("Interruption manuelle détectée (Ctrl+C).")

    # Signal arrêt à tous les threads
    scalping_stop_event.set()
    dashboard_stop_event.set()
    basket_monitor_stop_event.set()

    logger.info("⏳ Attente arrêt propre des threads...")

    # Attendre arrêt avec timeout
    thread_usdjpy.join(timeout=5.0)
    thread_eurusd.join(timeout=5.0)
    thread_gbpusd.join(timeout=5.0)
    thread_dashboard.join(timeout=5.0)
    basket_monitor.join(timeout=5.0)

    # Vérifier threads encore actifs
    for thread in [thread_usdjpy, thread_eurusd, thread_gbpusd, thread_dashboard]:
        if thread.is_alive():
            logger.warning(f"⚠️ Thread {thread.name} n'a pas terminé dans les 5s")
        else:
            logger.info(f"✅ Thread {thread.name} arrêté proprement")

    logger.info("✅ Arrêt complet")
```

---

## 📝 CHECKLIST IMPLÉMENTATION

### Phase 1: Préparation
- [x] Lire architecture actuelle run_bot.py
- [ ] Backup run_bot.py → run_bot.py.backup_31dec2025
- [ ] Créer document MULTI_THREAD_IMPLEMENTATION (ce fichier)

### Phase 2: Code Core
- [ ] Ajouter classe GlobalScalpingState (avant ligne 2966)
- [ ] Remplacer scalping_fast_thread par scalping_worker (ligne 2966)
- [ ] Ajouter dashboard_worker (après scalping_worker)

### Phase 3: Intégration
- [ ] Modifier section lancement threads (lignes 3976-4060)
- [ ] Supprimer message obsolète "LIQUIDITY Thread"
- [ ] Ajouter logs multi-threading

### Phase 4: Tests
- [ ] Validation syntaxe Python (`python3 -m py_compile run_bot.py`)
- [ ] Test démarrage: observer 3 threads + dashboard
- [ ] Vérifier logs compacts (1 ligne par asset)
- [ ] Vérifier dashboard agrégé (toutes les 30s)
- [ ] Vérifier staggered timing (0s, 1.5s, 3.0s)

### Phase 5: Validation
- [ ] Comparer logs AVANT (DEBUG_LOGS.txt) vs APRÈS
- [ ] Vérifier EURUSD et GBPUSD analysés
- [ ] Vérifier régimes détectés pour chaque asset
- [ ] Vérifier console lisible (pas de collision)

---

## 🎯 IMPACTS ATTENDUS

### Avant (1 thread)
```
[SCALPING_THREAD] Démarré (cycle 5s)
[SCALPING_THREAD] Données USDJPY indisponibles
[ORDERFLOW_SCORING_BINAIRE][USDJPY] liquid=True | strong_imbalance=False ...
[TIMING_GATEKEEPER][USDJPY] VETO: tick_rate 0.5 < 1.0
```

### Après (3 threads + dashboard)
```
🚀 DÉMARRAGE MULTI-THREADING SCALPING
  • SCALPING USDJPY Thread : Cycle 5s (offset 0.0s)
  • SCALPING EURUSD Thread : Cycle 5s (offset 1.5s)
  • SCALPING GBPUSD Thread : Cycle 5s (offset 3.0s)
  • DASHBOARD Thread       : Affichage agrégé 30s

[USDJPY] R:RANG(0.7) | OF:65/BUY | T:PASS | →BUY
[EURUSD] R:TREN(0.8) | OF:45/SEL | T:VETO | →HOLD
[GBPUSD] R:UNKN(0.0) | OF:0/NEU | T:VETO | →HOLD

📊 DASHBOARD SCALPING - 31 Dec 2025 03:58:45 GMT
ASSET    │ REGIME      │ ORDERFLOW    │ TIM  │ ACTION │ CYCLES
USDJPY   │ RANG(0.7)   │ 65/BUY       │ PASS │ →BUY   │ C:142
EURUSD   │ TREN(0.8)   │ 45/SEL       │ VETO │ →HOLD  │ C:141
GBPUSD   │ UNKN(0.0)   │ 0/NEU        │ VETO │ →HOLD  │ C:140
```

---

## ⚠️ NOTES IMPORTANTES

### 1. Compatibilité global_context
Le code actuel utilise `global_context["USDJPY"]` dans certains endroits.
Pour compatibilité, on peut garder ce dict en parallèle de GlobalScalpingState.

### 2. Trade Execution
La logique complète d'exécution de trade n'est PAS incluse dans scalping_worker.
Il faut copier la section trade execution depuis scalping_fast_thread (lignes 3300-3500).

### 3. Logs compacts
Format compact: `[ASSET] R:regime | OF:score/bias | T:timing | →action`
Logs détaillés uniquement si score >= 75 (signal fort).

### 4. Performance
- 3 threads × 5s cycle = charge CPU modérée
- Offsets échelonnés évitent pics simultanés
- Dashboard 30s évite spam console

### 5. Erreurs thread-safe
Utiliser `global_state.record_error()` pour logging thread-safe.

---

## 🚀 COMMANDES POUR TESTER

### Backup avant modification
```bash
cp run_bot.py run_bot.py.backup_31dec2025
```

### Validation syntaxe
```bash
python3 -m py_compile run_bot.py
```

### Lancement test
```bash
python3 run_bot.py --mode scalping > DEBUG_MULTI_THREAD.txt 2>&1
```

### Vérifier threads actifs
```bash
# Dans les logs, chercher:
grep "Worker démarré" DEBUG_MULTI_THREAD.txt
grep "DASHBOARD" DEBUG_MULTI_THREAD.txt | head -5
```

### Comparer performance
```bash
# Nombre de cycles par asset
grep "\[USDJPY\]" DEBUG_MULTI_THREAD.txt | wc -l
grep "\[EURUSD\]" DEBUG_MULTI_THREAD.txt | wc -l
grep "\[GBPUSD\]" DEBUG_MULTI_THREAD.txt | wc -l
```

---

**FIN DU DOCUMENT**
*Document créé pour assurer continuité implémentation multi-threading*
*Reprendre depuis Phase 2 de la checklist*
