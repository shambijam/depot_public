# 🚀 OPTIMISATION: Cache Multi-Niveaux & Réanalyse Intelligente

**Remarque utilisateur**: "Analyser 30 ou 50 barres à chaque cycle est totalement aberrant donc cet analyse est mise en cache ? et réanalyser toutes les x minutes ?"

**Date**: 2025-12-10
**Statut**: ⚠️ **OPTIMISATION CRITIQUE NÉCESSAIRE**

---

## 🎯 PROBLÈME IDENTIFIÉ

### Architecture Actuelle (INEFFICACE)

**Cycle actuel** :
```
DATAENGINE Thread (cycle 5s) :
  └─ get_rates(XAUUSD, M1, 50 barres)  ← Récupère 50 barres TOUTES les 5s
      ↓ 300-600ms latence MT5
      ↓ PhaseObserver.analyze() COMPLET sur 50 barres
      ↓ footprint_cache.update()

SCALPING Thread (cycle 5s) :
  └─ get_rates(XAUUSD, M1, 200 barres)  ← Récupère 200 barres TOUTES les 5s
      ↓ 1500-2000ms latence MT5
      ↓ PhaseObserver.analyze() COMPLET sur 200 barres
      ↓ OrderFlow V6 + Footprint V6 + VWAP
      └─ Decision

LIQUIDITY Thread (cycle 60s) :
  └─ Pour CHAQUE symbole (EURUSD, GBPUSD, XAUUSD):
      └─ get_rates(symbol, M1, 200 barres)  ← 3 × 200 barres toutes les 60s
          ↓ ~1500ms latence MT5 × 3
          ↓ PhaseObserver.analyze() × 3
          └─ Decision
```

**PROBLÈMES** :

1. ❌ **DATAENGINE récupère 50 barres toutes les 5s** alors que :
   - Seulement 1 nouvelle bougie M1 toutes les 60s
   - Les 49 autres barres ne changent jamais entre cycles

2. ❌ **SCALPING récupère 200 barres toutes les 5s** alors que :
   - Seulement 1 nouvelle bougie M1 toutes les 60s
   - Les 199 autres barres ne changent jamais

3. ❌ **PhaseObserver analyse TOUTES les barres à chaque cycle** :
   - Régime calculé sur 200 barres = 3h20 de données
   - Volatilité EMA 20, Volume MA 20, Volume Z-score, etc.
   - **Tout recalculé toutes les 5s alors que 95% des données sont identiques**

4. ❌ **Cache actuel TRÈS LIMITÉ** :
   - `footprint_cache` cache seulement le Footprint M1 (1 bougie)
   - Aucun cache pour régime, phase, OrderFlow, VWAP
   - Aucun cache pour les barres historiques

**RÉSULTAT** :
- **Latence gaspillée** : ~2500-3000ms par cycle pour récupérer + analyser données identiques
- **CPU gaspillé** : Calculs répétitifs sur 95% données inchangées
- **Bande passante MT5 gaspillée** : 200 barres × 5 colonnes × 2 threads = 2000 valeurs/5s

---

## ✅ SOLUTION: Cache Multi-Niveaux Intelligent

### Principe

**NE JAMAIS recalculer ce qui n'a pas changé** :

1. **Cache Barres** (TTL 60s) : Stocker les barres historiques, recharger seulement la bougie courante
2. **Cache Régime** (TTL 60s) : Calculer régime une fois par minute (1 nouvelle bougie = pas de changement significatif)
3. **Cache Phase** (TTL 60s) : Idem régime
4. **Cache OrderFlow Historique** (TTL 60s) : Volume MA 14, Delta momentum 10 bars
5. **Cache Footprint M1** (TTL 15s) : DÉJÀ IMPLÉMENTÉ ✅

---

## 📐 ARCHITECTURE OPTIMISÉE

### Niveau 1: Cache Barres (Historique)

**Fichier** : `core/bars_cache.py` (à créer)

```python
class BarsCache:
    """
    Cache intelligent pour barres OHLCV.

    Principe :
    - Stocke barres historiques (N-1 barres complètes)
    - Recharge SEULEMENT la bougie courante (incomplète) à chaque cycle
    - Reconstruit DataFrame complet = historique + courante
    """

    def __init__(self):
        self._cache = {}  # {symbol: {"bars": df, "timestamp": float, "count": int}}
        self._lock = threading.Lock()

    def get_or_fetch(
        self,
        symbol: str,
        timeframe: str,
        count: int,
        mt5_connector: Any,
        ttl_seconds: float = 60.0
    ) -> pd.DataFrame:
        """
        Récupère barres depuis cache ou MT5.

        Logique :
        1. Si cache VIDE ou EXPIRÉ (> TTL) :
           → Recharger TOUTES les barres (count barres complètes)

        2. Si cache VALIDE (< TTL) :
           → Recharger SEULEMENT la bougie courante (M1 incomplète)
           → Remplacer dernière barre du cache par la courante
           → Retourner historique (N-1) + courante (1)

        Gain : 95% réduction appels MT5 (1 barre au lieu de count barres)
        """
        with self._lock:
            now = time.time()
            cached = self._cache.get(symbol)

            # Cache MISS ou EXPIRÉ
            if not cached or (now - cached["timestamp"]) > ttl_seconds:
                df = mt5_connector.get_rates(symbol, timeframe, count)
                if df is not None and not df.empty:
                    self._cache[symbol] = {
                        "bars": df.copy(),
                        "timestamp": now,
                        "count": count
                    }
                return df

            # Cache HIT → Recharger seulement bougie courante
            current_bar = mt5_connector.get_rates(symbol, timeframe, 1)  # 1 barre
            if current_bar is None or current_bar.empty:
                # Fallback : retourner cache ancien
                return cached["bars"]

            # Remplacer dernière barre (incomplète) par courante
            historical = cached["bars"].iloc[:-1]  # N-1 barres complètes
            updated_df = pd.concat([historical, current_bar], ignore_index=True)

            # Mettre à jour cache
            self._cache[symbol] = {
                "bars": updated_df.copy(),
                "timestamp": now,
                "count": count
            }

            return updated_df

    def invalidate(self, symbol: str):
        """Force rechargement complet au prochain appel."""
        with self._lock:
            if symbol in self._cache:
                del self._cache[symbol]

# Singleton global
bars_cache = BarsCache()
```

**Utilisation** :

```python
# AVANT (SCALPING Thread) :
rates_df = mt5_connector.get_rates("XAUUSD", "M1", 30)  # 30 barres toutes les 5s

# APRÈS (SCALPING Thread) :
rates_df = bars_cache.get_or_fetch(
    symbol="XAUUSD",
    timeframe="M1",
    count=30,
    mt5_connector=mt5_connector,
    ttl_seconds=60.0  # Recharge complète toutes les 60s seulement
)
# → Récupère 1 barre au lieu de 30 (95% cycles)
```

**Gain** :
- **Latence MT5** : 50-100ms au lieu de 400-600ms (6-12x plus rapide)
- **Bande passante** : 6 valeurs au lieu de 180 valeurs (30x réduction)

---

### Niveau 2: Cache Régime (Calcul Lent)

**Fichier** : `phase_observer/regime_cache.py` (à créer)

```python
class RegimeCache:
    """
    Cache intelligent pour régime marché.

    Principe :
    - Régime change rarement (1 bougie = 1 minute)
    - Recalculer seulement toutes les N minutes (configurable)
    - Entre-temps : retourner régime cached
    """

    def __init__(self):
        self._cache = {}  # {symbol: {"regime": str, "confidence": float, "timestamp": float}}
        self._lock = threading.Lock()

    def get_or_calculate(
        self,
        symbol: str,
        df: pd.DataFrame,
        detector_func: Callable,
        recalc_interval: float = 60.0  # Recalculer toutes les 60s
    ) -> Dict[str, Any]:
        """
        Retourne régime depuis cache ou calcule si nécessaire.

        Args:
            symbol: Symbole (ex: XAUUSD)
            df: DataFrame barres M1 (20-30 barres)
            detector_func: Fonction calcul régime (ex: RegimeDetectorLite.detect)
            recalc_interval: Intervalle recalcul (défaut 60s = 1 bougie M1)

        Returns:
            {"regime": str, "confidence": float, "age_seconds": float}
        """
        with self._lock:
            now = time.time()
            cached = self._cache.get(symbol)

            # Cache MISS ou EXPIRÉ
            if not cached or (now - cached["timestamp"]) > recalc_interval:
                regime_result = detector_func(df)  # Calcul régime
                self._cache[symbol] = {
                    "regime": regime_result["regime"],
                    "confidence": regime_result["confidence"],
                    "timestamp": now
                }
                return {
                    "regime": regime_result["regime"],
                    "confidence": regime_result["confidence"],
                    "age_seconds": 0.0
                }

            # Cache HIT → Retourner régime cached
            age = now - cached["timestamp"]
            return {
                "regime": cached["regime"],
                "confidence": cached["confidence"],
                "age_seconds": age
            }

    def update(self, symbol: str, regime: str, confidence: float):
        """Mise à jour externe (ex: depuis LIQUIDITY Thread)."""
        with self._lock:
            self._cache[symbol] = {
                "regime": regime,
                "confidence": confidence,
                "timestamp": time.time()
            }

# Singleton global
regime_cache = RegimeCache()
```

**Utilisation** :

```python
# AVANT (SCALPING Thread) :
regime = detect_market_regime(df)  # Calcul LOURD toutes les 5s

# APRÈS (SCALPING Thread) :
regime = regime_cache.get_or_calculate(
    symbol="XAUUSD",
    df=df,
    detector_func=lambda df: regime_detector_lite.detect_regime(df),
    recalc_interval=60.0  # Recalcule seulement toutes les 60s
)
# → Retourne cache 95% du temps (sauf toutes les 60s)
```

**Gain** :
- **Latence régime** : 0ms au lieu de 100-200ms (calcul slope, ATR, volume ratio)
- **CPU** : 95% réduction calculs régime

---

### Niveau 3: Cache OrderFlow Historique (Volume MA, Delta)

**Fichier** : `strategy/orderflow_cache.py` (à créer)

```python
class OrderFlowCache:
    """
    Cache intelligent pour composants OrderFlow historiques.

    Principe :
    - Volume MA 14 bars ne change significativement qu'avec nouvelle bougie M1 (60s)
    - Delta momentum 10 bars idem
    - Recalculer seulement toutes les 60s (1 nouvelle bougie)
    """

    def __init__(self):
        self._cache = {}  # {symbol: {"volume_ma_14": float, "delta_momentum": {...}, "timestamp": float}}
        self._lock = threading.Lock()

    def get_volume_ma(
        self,
        symbol: str,
        df: pd.DataFrame,
        recalc_interval: float = 60.0
    ) -> float:
        """
        Retourne Volume MA 14 depuis cache ou calcule.
        """
        with self._lock:
            now = time.time()
            cached = self._cache.get(symbol, {})

            if "volume_ma_14" not in cached or (now - cached.get("timestamp", 0)) > recalc_interval:
                # Recalculer Volume MA 14 (excluant bougie courante)
                if len(df) >= 15:
                    volume_ma = df["tick_volume"].iloc[-15:-1].mean()
                else:
                    volume_ma = df["tick_volume"].mean()

                cached["volume_ma_14"] = volume_ma
                cached["timestamp"] = now
                self._cache[symbol] = cached

            return cached["volume_ma_14"]

    def get_delta_momentum(
        self,
        symbol: str,
        footprints: List[Dict],
        recalc_interval: float = 60.0
    ) -> Dict[str, Any]:
        """
        Retourne Delta momentum (10 bars) depuis cache ou calcule.
        """
        with self._lock:
            now = time.time()
            cached = self._cache.get(symbol, {})

            if "delta_momentum" not in cached or (now - cached.get("timestamp", 0)) > recalc_interval:
                # Recalculer Delta momentum sur 10 barres
                delta_series = [fp["delta_total"] for fp in footprints[-10:]]
                bullish_count = sum(1 for d in delta_series if d > 0)
                bearish_count = sum(1 for d in delta_series if d < 0)
                coherence = max(bullish_count, bearish_count) / 10

                cached["delta_momentum"] = {
                    "coherence": coherence,
                    "bullish_count": bullish_count,
                    "bearish_count": bearish_count
                }
                cached["timestamp"] = now
                self._cache[symbol] = cached

            return cached["delta_momentum"]

# Singleton global
orderflow_cache = OrderFlowCache()
```

**Gain** :
- **Latence OrderFlow** : Volume MA 0ms (cache) au lieu de 5-10ms
- **CPU** : 95% réduction calculs historiques

---

### Niveau 4: Footprint M1 (DÉJÀ IMPLÉMENTÉ)

**Fichier** : `core/footprint_cache.py` ✅

**Principe actuel** (BON) :
- DATAENGINE calcule Footprint M1 toutes les 5s (bougie courante + ticks)
- SCALPING lit depuis cache (TTL 15s)
- Cache HIT = 0ms au lieu de 100-200ms

**Optimisation possible** :
- DATAENGINE : Utiliser `bars_cache` pour réduire 50→1 barres
- SCALPING : Garder lecture cache (inchangé)

---

## 📊 ARCHITECTURE COMPLÈTE OPTIMISÉE

### DataEngine Thread (Cycle 5s)

```python
def _update_footprint_for_symbol(self, symbol: str):
    """
    Analyse Footprint M1 toutes les 5s avec cache barres.
    """
    # 1️⃣ Récupérer barres depuis CACHE (1 barre au lieu de 20)
    rates_df = bars_cache.get_or_fetch(
        symbol=symbol,
        timeframe="M1",
        count=20,  # Réduit 50→20 (suffisant pour Volume MA 14)
        mt5_connector=self.mt5_connector,
        ttl_seconds=60.0
    )
    # → 95% du temps : récupère 1 barre (50-100ms au lieu de 300-600ms)

    # 2️⃣ Récupérer ticks bougie M1 courante (SANS CACHE, toujours frais)
    ticks_data = self._get_current_m1_ticks(symbol)

    # 3️⃣ Analyser Footprint M1 (pipeline LÉGER)
    footprint_result = self._analyze_footprint_lightweight(symbol, rates_df, ticks_data)
    # → Pas de PhaseObserver complet, juste Footprint + Volume MA 14

    # 4️⃣ Mettre à jour cache Footprint
    footprint_cache.update(symbol, footprint_result)
```

**Latence DataEngine** : **100-200ms** (vs 600-900ms actuellement) ← **75% gain**

---

### Scalping Thread (Cycle 5s)

```python
def scalping_fast_thread():
    """
    Analyse XAUUSD toutes les 5s avec cache multi-niveaux.
    """
    while not stop_event.is_set():
        cycle_start = time.time()

        # 1️⃣ Récupérer barres depuis CACHE (1 barre au lieu de 30)
        df_m1 = bars_cache.get_or_fetch(
            symbol="XAUUSD",
            timeframe="M1",
            count=30,  # Réduit 200→30
            mt5_connector=mt5_connector,
            ttl_seconds=60.0
        )
        # → 95% du temps : récupère 1 barre (50-100ms au lieu de 400-600ms)

        # 2️⃣ Régime VWAP depuis CACHE (recalcul toutes les 60s)
        regime = regime_cache.get_or_calculate(
            symbol="XAUUSD",
            df=df_m1,
            detector_func=lambda df: regime_detector_lite.detect_regime(df),
            recalc_interval=60.0
        )
        # → 95% du temps : cache HIT (0ms au lieu de 100-200ms)

        # 3️⃣ Footprint M1 depuis CACHE (DATAENGINE)
        cached_footprint = footprint_cache.get("XAUUSD", max_age_seconds=15.0)
        # → Cache HIT garanti (DATAENGINE tourne toutes les 5s)

        # 4️⃣ OrderFlow V6 avec CACHE historique
        volume_ma_14 = orderflow_cache.get_volume_ma(
            symbol="XAUUSD",
            df=df_m1,
            recalc_interval=60.0
        )
        # → Cache HIT 95% du temps

        delta_momentum = orderflow_cache.get_delta_momentum(
            symbol="XAUUSD",
            footprints=footprints_history,  # Liste 10 derniers footprints
            recalc_interval=60.0
        )
        # → Cache HIT 95% du temps

        orderflow_result = scalping_strategy._analyze_orderflow_v6(
            asset="XAUUSD",
            df_m1=df_m1[-15:],  # 15 dernières barres suffisent
            asset_signals={"footprint_summary": cached_footprint},
            volume_ma_14=volume_ma_14,  # Depuis cache
            delta_momentum=delta_momentum  # Depuis cache
        )

        # 5️⃣ Footprint V6 (rapide, 4 barres)
        footprint_result = scalping_strategy._analyze_footprint_v6(
            asset="XAUUSD",
            df_m1=df_m1[-4:],  # 4 dernières barres
            asset_signals={"footprint_summary": cached_footprint}
        )

        # 6️⃣ VWAP avec régime cached
        vwap_weights = get_regime_weights(regime["regime"])
        vwap_score = vwap_analyzer.calculate(df_m1, regime=regime["regime"])

        # 7️⃣ Fusion et décision
        final_score = calculate_fusion_score(...)

        if final_score >= 75:
            execute_burst_scalping()

        # Sleep
        elapsed = time.time() - cycle_start
        time.sleep(max(0, 5 - elapsed))
```

**Latence SCALPING** : **200-400ms** (vs 2500ms actuellement) ← **85% gain**

---

## 📊 COMPARAISON AVANT/APRÈS

### Latence par Cycle

| Thread | Avant (Actuel) | Après (Optimisé) | Gain |
|--------|----------------|------------------|------|
| **DATAENGINE** | 600-900ms | 100-200ms | **-75%** |
| **SCALPING** | 2500ms | 200-400ms | **-85%** |
| **LIQUIDITY** | 4500ms (3 symboles) | 600-900ms | **-80%** |

### Appels MT5 par Minute

| Avant | Après | Gain |
|-------|-------|------|
| DATAENGINE: 50 bars × 12 cycles = **600 bars/min** | 1 bar × 11 + 50 bars × 1 = **61 bars/min** | **-90%** |
| SCALPING: 200 bars × 12 cycles = **2400 bars/min** | 1 bar × 11 + 30 bars × 1 = **41 bars/min** | **-98%** |
| LIQUIDITY: 200 bars × 3 symboles = **600 bars/min** | 1 bar × 3 × 1 = **63 bars/min** | **-90%** |
| **TOTAL** | **3600 bars/min** | **165 bars/min** | **-95%** |

### Calculs CPU par Minute

| Composant | Avant | Après | Gain |
|-----------|-------|-------|------|
| **Régime VWAP** | 12 calculs/min | 1 calcul/min | **-92%** |
| **Volume MA 14** | 12 calculs/min | 1 calcul/min | **-92%** |
| **Delta Momentum** | 12 calculs/min | 1 calcul/min | **-92%** |
| **PhaseObserver complet** | 12 cycles/min | 1 cycle/min | **-92%** |

---

## 🎯 FRÉQUENCES DE RECALCUL OPTIMALES

### Composants Temps Réel (PAS DE CACHE)

| Composant | Fréquence | Raison |
|-----------|-----------|--------|
| **Footprint M1 (ticks)** | 5s | Ticks bougie courante (change en temps réel) |
| **Prix courant** | 5s | Décision trading (doit être frais) |
| **VWAP score** | 5s | Distance prix/VWAP (change en temps réel) |

### Composants Historiques (CACHE 60s)

| Composant | Fréquence | Raison |
|-----------|-----------|--------|
| **Régime VWAP** | 60s | 1 nouvelle bougie M1 = changement minimal |
| **Volume MA 14** | 60s | 14 barres historiques + 1 nouvelle = changement 7% |
| **Delta Momentum 10** | 60s | 10 barres historiques + 1 nouvelle = changement 10% |
| **Phase detection** | 60s | Phase change rarement (plusieurs bougies) |
| **Barres historiques** | 60s | 1 nouvelle bougie M1 toutes les 60s |

### Composants Lents (CACHE 5-10 min)

| Composant | Fréquence | Raison |
|-----------|-----------|--------|
| **Régime SLOW (200 bars)** | 300s (5 min) | Contexte macro, change très lentement |
| **Config reload** | 600s (10 min) | Config fichier, change rarement |

---

## ✅ PLAN D'IMPLÉMENTATION

### Phase 1: Cache Barres (PRIORITÉ HAUTE)

**Fichiers à créer** :
- `core/bars_cache.py` : Classe BarsCache

**Fichiers à modifier** :
- `core/data_engine.py:132` : Utiliser `bars_cache.get_or_fetch()` au lieu de `mt5_connector.get_rates()`
- `run_bot.py:3193` : Utiliser `bars_cache.get_or_fetch()` au lieu de `mt5_connector.get_rates()`
- `run_bot.py:3511` : Utiliser `bars_cache.get_or_fetch()` pour LIQUIDITY

**Gain immédiat** : **-90% appels MT5**, **-50% latence**

---

### Phase 2: Cache Régime (PRIORITÉ HAUTE)

**Fichiers à créer** :
- `phase_observer/regime_cache.py` : Classe RegimeCache

**Fichiers à modifier** :
- `run_bot.py:3193` : Utiliser `regime_cache.get_or_calculate()` au lieu de calcul direct

**Gain immédiat** : **-92% calculs régime**, **-10-15% latence**

---

### Phase 3: Cache OrderFlow (PRIORITÉ MOYENNE)

**Fichiers à créer** :
- `strategy/orderflow_cache.py` : Classe OrderFlowCache

**Fichiers à modifier** :
- `strategy/scalping.py:_analyze_orderflow_v6()` : Utiliser `orderflow_cache.get_volume_ma()` et `orderflow_cache.get_delta_momentum()`

**Gain immédiat** : **-5-10% latence**, **-20% CPU OrderFlow**

---

### Phase 4: Pipeline DataEngine Léger (PRIORITÉ BASSE)

**Fichiers à modifier** :
- `core/data_engine.py:_analyze_footprint()` : Créer `_analyze_footprint_lightweight()` (sans PhaseObserver complet)

**Gain immédiat** : **-30% latence DataEngine**, **-50% CPU DataEngine**

---

## 📋 CHECKLIST OPTIMISATION

### Niveau 1: Cache Barres (85% gain)

- [ ] Créer `core/bars_cache.py` (classe BarsCache)
- [ ] Modifier DATAENGINE : 50→20 bars + cache
- [ ] Modifier SCALPING : 200→30 bars + cache
- [ ] Modifier LIQUIDITY : 200 bars + cache
- [ ] Tester cache HIT/MISS ratio (>95% attendu)

### Niveau 2: Cache Régime (10% gain)

- [ ] Créer `phase_observer/regime_cache.py` (classe RegimeCache)
- [ ] Créer RegimeDetectorLite (30 bars)
- [ ] Créer RegimeResolver (hybrid fast+cache)
- [ ] LIQUIDITY : Update cache régime toutes les 60s
- [ ] SCALPING : Read cache régime (TTL 60s)

### Niveau 3: Cache OrderFlow (5% gain)

- [ ] Créer `strategy/orderflow_cache.py` (classe OrderFlowCache)
- [ ] Cache Volume MA 14 (TTL 60s)
- [ ] Cache Delta Momentum (TTL 60s)
- [ ] Intégrer dans `_analyze_orderflow_v6()`

### Niveau 4: Pipeline Léger DataEngine (optionnel)

- [ ] Créer `_analyze_footprint_lightweight()` (sans PhaseObserver complet)
- [ ] Calculer seulement Footprint + Volume MA 14
- [ ] Régime/Phase depuis cache externe

---

## ✅ CONCLUSION

### Réponse à la Question

**"Analyser 30 ou 50 barres à chaque cycle est totalement aberrant donc cet analyse est mise en cache ?"**

**RÉPONSE** : **ACTUELLEMENT NON ❌** (sauf Footprint M1)

**Architecture actuelle** :
- ✅ **Footprint M1** : Cached (DATAENGINE → SCALPING)
- ❌ **Barres historiques** : PAS cached (récupérées 50-200 barres toutes les 5s)
- ❌ **Régime** : PAS cached (recalculé toutes les 5s)
- ❌ **OrderFlow historique** : PAS cached (recalculé toutes les 5s)

**"Et réanalyser toutes les X minutes ?"**

**RÉPONSE** : **DEVRAIT être toutes les 60s** (1 nouvelle bougie M1)

**Optimisation recommandée** :
- ✅ **Cache barres** : TTL 60s (recharge 1 barre au lieu de 30-200)
- ✅ **Cache régime** : TTL 60s (recalcule seulement avec nouvelle bougie)
- ✅ **Cache OrderFlow** : TTL 60s (Volume MA 14, Delta momentum)
- ✅ **Footprint M1** : TTL 15s (déjà optimal)

**Gains attendus** :
- **Latence** : **-85%** (2500ms → 400ms)
- **Appels MT5** : **-95%** (3600 bars/min → 165 bars/min)
- **Calculs CPU** : **-90%** (régime, OrderFlow historique)

---

**Document créé par** : Claude Code
**Date** : 2025-12-10
**Statut** : ⚠️ Optimisation critique recommandée
