# 📊 DOCUMENTATION SYSTÈME DE SCORING ACTUEL

## 🎯 OBJECTIF

Comprendre comment fonctionne le scoring actuel pour concevoir le nouveau système évolutif.

---

## 🔄 FLUX COMPLET DES DONNÉES

### 1️⃣ RÉCUPÉRATION DONNÉES MT5 → `run_bot.py`

**Fichier** : `run_bot.py` lignes 3170-3242

#### A. Récupération Bougies OHLCV (M1)
```python
# Ligne 3208
rates_df = bars_cache.get_or_fetch(
    symbol=asset,
    timeframe="M1",
    count=50,  # 50 bougies M1
    mt5_connector=mt5_connector,
    ttl_seconds=60.0
)
```

**Colonnes `rates_df`** :
- `time` : Timestamp UTC
- `open`, `high`, `low`, `close` : Prix OHLC
- `tick_volume` ou `volume` : Volume
- `spread` : Spread (optionnel)

---

#### B. Récupération Ticks
```python
# Ligne 3223-3228
ticks_df = mt5_connector.get_ticks_for_candle(
    asset,
    candle_start.to_pydatetime(),
    candle_end.to_pydatetime(),
    timeout=5.0
)
```

**Colonnes `ticks_df`** :
- `time` : Timestamp du tick
- `bid` : Prix bid
- `ask` : Prix ask
- `last` : Dernier prix
- `volume` : Volume du tick
- `flags` : Flags MT5 (16=BUY, 32=SELL)
- `side` : Classification 'buy' | 'sell' | 'unknown' (ajouté par mt5_connector)
- `mid` : (bid + ask) / 2
- `spread` : ask - bid

**Note** : `ticks_df` peut être `None` si pas de ticks ou timeout

---

### 2️⃣ ANALYSE MARKET → `MarketAnalyzer`

**Fichier** : `run_bot.py` ligne 3281

```python
market_results = market_analyzer.analyze(
    asset=asset,
    df=rates_df,  # Bougies M1
    ticks=ticks_df  # Ticks de la dernière bougie
)
```

**Retourne `market_results`** :
```python
{
    "latest": {
        "time": Timestamp,
        "close": float,
        "regime": str,  # Ex: "strong_trending_retail_bear"
        "confidence": float,
        ...
    },
    "annotated_df": DataFrame,  # rates_df enrichi avec colonnes calculées
    "signals": [],
    "volatility": float,
    ...
}
```

---

### 3️⃣ CALCUL ORDERFLOW V6 → `ScalpingStrategy._analyze_orderflow_v6()`

**Fichier** : `run_bot.py` ligne 3454-3460

#### Appel
```python
# Préparer asset_signals
asset_signals_for_of = {
    "footprint_summary": {},
    "orderflow_summary": {},
    "ticks_df": ticks_df  # 🆕 Pour analyseurs institutionnels
}

# Appel OrderFlow V6
of_v6_result = scalping_strategy._analyze_orderflow_v6(
    asset=asset,
    df_m1=rates_df_fresh,  # DataFrame M1 (50 bougies)
    df_m3=None,  # Pas utilisé en mode burst
    df_m5=None,  # Pas utilisé en mode burst
    asset_signals=asset_signals_for_of
)
```

---

### 4️⃣ CALCUL SCORE ORDERFLOW V6 → `strategy/scalping.py`

**Fichier** : `strategy/scalping.py` méthode `_analyze_orderflow_v6()` lignes 287-1040

#### INPUTS (Paramètres reçus)

1. **`asset`** : str (ex: "USDJPY")
2. **`df_m1`** : DataFrame bougies M1 (50 bougies)
   - Colonnes : `time`, `open`, `high`, `low`, `close`, `volume`
3. **`df_m3`** : DataFrame M3 (None en mode burst)
4. **`df_m5`** : DataFrame M5 (None en mode burst)
5. **`asset_signals`** : dict
   ```python
   {
       "footprint_summary": {},
       "orderflow_summary": {},
       "ticks_df": DataFrame ou None
   }
   ```

---

#### ÉTAPES DE CALCUL

##### ÉTAPE 0 : Analyse Multi-Timeframe (lignes 364-437)

**Objectif** : Déterminer direction M1, M3, M5

```python
# M1 : 1 bougie
m1_dir = calculate_mtf_direction_unified(df_m1, 1, bullish_threshold=0.67, bearish_threshold=0.33)
# → "bullish" | "bearish" | "neutral"

result["mtf_alignment"] = {
    "m1": m1_dir,
    "m3": m3_dir,  # Idem pour M3
    "m5": m5_dir   # Idem pour M5
}
```

**Fonction utilisée** : `calculate_mtf_direction_unified()` (dans `strategy/scalping.py`)

**Logique** :
- Compte les bougies vertes (bullish) vs rouges (bearish)
- Si ratio ≥ 0.67 → "bullish"
- Si ratio ≤ 0.33 → "bearish"
- Sinon → "neutral"

---

##### ÉTAPE 1 : Delta Momentum (lignes 440-612)

**Source de données** : `df_m1` (bougies M1)

**Fenêtre** : `delta_coherence_bars` bougies (défaut: 3)

**Calcul** :
```python
# 1. Compter bougies bullish vs bearish
bullish_count = sum(1 for i in range(N) if close[i] > open[i])
bearish_count = N - bullish_count

# 2. Delta
delta = bullish_count - bearish_count

# 3. Cohérence (alignement directionnel)
coherence = max(bullish_count, bearish_count) / float(N)  # 0.0 à 1.0

# 4. Direction
if delta > 0:
    direction = "bullish"
elif delta < 0:
    direction = "bearish"
else:
    direction = "neutral"
```

**Stockage** :
```python
delta_details = {
    "delta": delta,
    "bullish_count": bullish_count,
    "bearish_count": bearish_count,
    "coherence": coherence,
    "direction": direction
}

# Score delta : 0-25 points
if abs(delta) >= 3:
    delta_momentum_score = 25.0
elif abs(delta) >= 2:
    delta_momentum_score = 20.0
...
```

---

##### ÉTAPE 2 : Volume Confirmation (lignes 614-698)

**Source de données** : `df_m1` (bougies M1)

**Fenêtre** : `volume_avg_bars` bougies (défaut: 6)

**Calcul** :
```python
# 1. Volume actuel (dernière bougie)
current_volume = df_m1.iloc[-1]["volume"]

# 2. Volume moyen historique
avg_volume = df_m1["volume"].tail(volume_avg_bars).mean()

# 3. Ratio
volume_ratio = current_volume / avg_volume if avg_volume > 0 else 1.0

# 4. Classification
if volume_ratio >= 1.8:
    category = "spike"
elif volume_ratio >= 1.4:
    category = "elevated"
...
```

**Stockage** :
```python
volume_details = {
    "current_volume": current_volume,
    "avg_volume": avg_volume,
    "volume_ratio": volume_ratio,
    "category": category
}

# Score volume : 0-15 points
if volume_ratio >= 1.8:
    volume_confirmation_score = 15.0
elif volume_ratio >= 1.4:
    volume_confirmation_score = 12.0
...
```

---

##### ÉTAPE 3 : Imbalance Strength (lignes 700-792)

**Source de données** : `asset_signals["footprint_summary"]` ou `ticks_df`

**Méthode 1** : Depuis footprint_summary (si disponible)
```python
fp_summary = asset_signals.get("footprint_summary", {})

# Extraire buy/sell ratio
buy_ratio_pct = fp_summary.get("buy_ratio", 0.5) * 100
sell_ratio_pct = fp_summary.get("sell_ratio", 0.5) * 100

# Compter imbalances (zones de déséquilibre)
total_imbalances = fp_summary.get("imbalance_zones", [])
imbalance_buy = sum(1 for z in total_imbalances if z["type"] == "buy")
imbalance_sell = sum(1 for z in total_imbalances if z["type"] == "sell")
```

**Méthode 2** : Depuis ticks_df (fallback)
```python
ticks_df = asset_signals.get("ticks_df", None)

buy_volume = ticks_df[ticks_df["side"] == "buy"]["volume"].sum()
sell_volume = ticks_df[ticks_df["side"] == "sell"]["volume"].sum()

buy_ratio_pct = (buy_volume / (buy_volume + sell_volume)) * 100
sell_ratio_pct = 100 - buy_ratio_pct
```

**Stockage** :
```python
imbalance_details = {
    "imbalance_buy": imbalance_buy,
    "imbalance_sell": imbalance_sell,
    "total_count": total_imbalances,
    "buy_ratio": buy_ratio_pct,
    "sell_ratio": sell_ratio_pct,
    "direction": "BUY" | "SELL" | "NEUTRAL"
}

# Score imbalance : 0-10 points
if total_imbalances >= 5:
    imbalance_strength_score = 10.0
elif total_imbalances >= 3:
    imbalance_strength_score = 8.0
...
```

---

##### ÉTAPE 4 : Scoring Progressif (lignes 810-865)

**Système actuel** : Points cumulatifs

```python
progressive_score = 0.0

# 1. DELTA MOMENTUM (0-40 points)
if delta_momentum_score >= 20.0:
    progressive_score += 40.0
elif delta_momentum_score >= 15.0:
    progressive_score += 30.0
elif delta_momentum_score >= 12.0:
    progressive_score += 25.0
...

# 2. VOLUME CONFIRMATION (0-30 points)
if volume_confirmation_score >= 12.0:
    progressive_score += 30.0
elif volume_confirmation_score >= 10.0:
    progressive_score += 25.0
...

# 3. IMBALANCE STRENGTH (0-20 points)
if imbalance_strength_score >= 8.0:
    progressive_score += 20.0
...

# 4. COHÉRENCE (0-10 points)
coherence_pct = delta_details.get("coherence", 0.0)
if coherence_pct >= 0.67:
    progressive_score += 10.0
...

# Score final (0-100)
result["total_score"] = min(100.0, progressive_score)
```

---

##### ÉTAPE 5 : Signal Quality (lignes 866-876)

```python
if progressive_score >= 80.0:
    result["signal_quality"] = "EXCELLENT"
elif progressive_score >= 60.0:
    result["signal_quality"] = "STRONG"
elif progressive_score >= 40.0:
    result["signal_quality"] = "MODERATE"
elif progressive_score >= 20.0:
    result["signal_quality"] = "WEAK"
else:
    result["signal_quality"] = "NO_TRADE"
```

---

##### ÉTAPE 6 : Bias Directionnel (lignes 878-917)

```python
delta_direction = delta_details.get("direction", "neutral")

if delta_direction == "bullish":
    result["bias"] = "BUY"

    # BONUS +10 si M1 et M3 alignés BULLISH
    if m1_dir == "bullish" and m3_dir == "bullish":
        result["total_score"] = min(100.0, result["total_score"] + 10)
        result["mtf_bonus"] = True

elif delta_direction == "bearish":
    result["bias"] = "SELL"

    # BONUS +10 si M1 et M3 alignés BEARISH
    if m1_dir == "bearish" and m3_dir == "bearish":
        result["total_score"] = min(100.0, result["total_score"] + 10)
        result["mtf_bonus"] = True

else:
    result["bias"] = "NEUTRAL"
```

---

##### ÉTAPE 7 : Analyseurs Institutionnels (lignes 936-1031)

**🆕 Ajoutés 03 JAN 2026**

```python
institutional_analysis = {}

# 1. PriceMemoryAnalyzer
memory_result = price_memory.analyze_price_memory(df_m1, current_price)
institutional_analysis['price_memory'] = memory_result

# 2. MarketFatigueAnalyzer
fatigue_result = fatigue_analyzer.calculate_fatigue_indicators(ticks_df, df_m1.tail(20))
institutional_analysis['market_fatigue'] = fatigue_result

# 3. MarketPhysicsAnalyzer
physics_result = physics_analyzer.apply_physics_principles(ticks_df, df_m1)
institutional_analysis['market_physics'] = physics_result

# 4. MicrostructureAnalyzer
tape_speed = microstructure.analyze_tape_speed(ticks_df)
institutional_analysis['tape_speed'] = tape_speed

# 5. LiquidityHeatmap
pressure = liquidity_map.calculate_pressure_ratio(ticks_df, window_seconds=5.0)
institutional_analysis['pressure_ratio'] = pressure

# Ajouter au résultat
result['institutional_analysis'] = institutional_analysis
```

**⚠️ IMPORTANT** : Ces analyseurs sont calculés mais **PAS encore utilisés dans le score final** !

---

#### OUTPUT (Résultat retourné)

```python
result = {
    # Scores
    "delta_momentum_score": 0-25,
    "volume_confirmation_score": 0-15,
    "imbalance_strength_score": 0-10,
    "total_score": 0-100,
    "signal_quality": "EXCELLENT" | "STRONG" | "MODERATE" | "WEAK" | "NO_TRADE",

    # Direction
    "bias": "BUY" | "SELL" | "NEUTRAL",
    "mtf_bonus": True | False,
    "mtf_conflict": False,

    # MTF Alignment
    "mtf_alignment": {
        "m1": "bullish" | "bearish" | "neutral",
        "m3": "...",
        "m5": "..."
    },

    # Détails (pour logs)
    "delta_details": {...},
    "volume_details": {...},
    "imbalance_strength_details": {...},

    # 🆕 Analyseurs institutionnels (non utilisés dans score pour l'instant)
    "institutional_analysis": {
        "price_memory": {...},
        "market_fatigue": {...},
        "market_physics": {...},
        "tape_speed": {...},
        "pressure_ratio": {...}
    }
}
```

---

### 5️⃣ UTILISATION DU SCORE → `run_bot.py`

**Fichier** : `run_bot.py` lignes 3463-3690

#### Extraction Résultat

```python
# Ligne 3464-3471
orderflow_result_mini = {
    "score": of_v6_result.get("total_score", 0.0),
    "bias": of_v6_result.get("bias", "NEUTRAL"),
    "summary": of_v6_result
}
```

---

#### ÉTAPE 1 : Timing Gatekeeper (lignes 3479-3520)

**Inputs** :
- `ticks_df` : Pour analyse tick_rate et coverage
- `scalping_config_global` : Config timing_gatekeeper
- `asset_config_timing` : Config asset-spécifique

**Appel** :
```python
timing_verdict = evaluate_trading_conditions(
    asset=asset,
    current_time=pd.Timestamp.now(tz='UTC'),
    ticks_df=ticks_df,
    scalping_config=scalping_config_global,
    asset_config=asset_config_timing
)
```

**Retourne** :
```python
{
    "verdict": "PASS" | "VETO",
    "veto_score": 0-100,  # 🆕 Score pondéré (02 JAN)
    "veto_reason": str,
    "quality_metrics": {
        "tick_rate": float,
        "coverage": float,
        "session": str,
        "gmt_hour": int
    }
}
```

---

#### ÉTAPE 2 : Décision Intelligente (lignes 3521-3690)

**Logique actuelle** :

```python
veto_score = timing_verdict.get("veto_score", 0.0)
orderflow_score = orderflow_result_mini['score']

# 1. CHECK VETO
can_override_veto = False

if orderflow_score >= 90.0 and veto_score < 70.0:
    can_override_veto = True  # Signal exceptionnel passe outre veto modéré
elif orderflow_score >= 85.0 and veto_score < 60.0:
    can_override_veto = True  # Signal très fort passe outre veto faible

# 2. VETO ABSOLU
if veto_score >= 80.0 and not can_override_veto:
    fusion_out = {
        "ok": False,
        "action": "HOLD",
        "veto_reason": "TIMING_VETO"
    }

# 3. CHECK RÉGIME DE MARCHÉ
elif not phase_is_blocked:
    # Régime OK → Construire décision
    decision_mini = market_analyzer.build_decision(
        orderflow_result=orderflow_result_mini,
        min_score=70.0  # Seuil minimum pour BUY/SELL
    )

    if decision_mini["action"] in ["BUY", "SELL"]:
        fusion_out = {
            "ok": True,
            "action": decision_mini["action"],
            "fused_confidence": decision_mini["confidence"],
            "orderflow_score": orderflow_score,
            ...
        }
    else:
        fusion_out = {
            "ok": False,
            "action": "HOLD"
        }
```

---

#### ÉTAPE 3 : Exécution Trade (lignes 3819+)

```python
if fusion_out.get("ok") and trade_decision_skeleton is not None:
    # Vérifier burst guard (un seul panier à la fois)
    # Construire trade complet
    # Exécuter via TradeExecutor
    ...
```

---

## 📊 RÉSUMÉ FLUX DE DONNÉES

```
MT5 (Broker)
    ↓
    ├─→ rates_df (50 bougies M1)
    └─→ ticks_df (ticks dernière bougie)
         ↓
MarketAnalyzer.analyze()
    ↓
    └─→ market_results (régime, signals, annotated_df)
         ↓
ScalpingStrategy._analyze_orderflow_v6()
    ↓
    ├─→ MTF Alignment (M1/M3/M5 direction)
    ├─→ Delta Momentum (0-25 pts)
    ├─→ Volume Confirmation (0-15 pts)
    ├─→ Imbalance Strength (0-10 pts)
    ├─→ Cohérence (0-10 pts)
    ├─→ Scoring Progressif (0-100)
    ├─→ Bias (BUY/SELL/NEUTRAL)
    └─→ 🆕 Institutional Analysis (non utilisé dans score)
         ↓
orderflow_result_mini
    ↓
    ├─→ Timing Gatekeeper (PASS/VETO avec score 0-100)
    └─→ Phase Veto (régime bloqué ?)
         ↓
Décision Intelligente
    ↓
    ├─→ Override veto si signal fort
    ├─→ MarketAnalyzer.build_decision() (si PASS)
    └─→ fusion_out (ok=True/False, action, confidence)
         ↓
Exécution Trade (si fusion_out.ok == True)
```

---

## 🎯 POINTS D'EXTENSION POUR NOUVEAU SYSTÈME

### Option 1 : Remplacer le Scoring Progressif

**Fichier** : `strategy/scalping.py` lignes 810-865

**Action** : Remplacer la logique de `progressive_score` par votre nouveau système

**Avantage** : Simple, tout reste au même endroit

---

### Option 2 : Nouveau Scoring Module

**Créer** : `strategy/institutional_scoring.py`

**Fonction** :
```python
def calculate_institutional_score(
    orderflow_result: dict,
    institutional_analysis: dict,
    market_results: dict
) -> dict:
    """
    Combine tous les analyseurs pour un score final 0-100
    """
    score = 0.0

    # Pondérer OrderFlow V6
    score += orderflow_result["total_score"] * 0.50

    # Pondérer analyseurs institutionnels
    if institutional_analysis.get("market_fatigue", {}).get("market_state") == "EXHAUSTED":
        score *= 0.5  # Pénalité 50%

    # Bonus/malus Physics
    physics_bias = institutional_analysis.get("market_physics", {}).get("physics_bias")
    if physics_bias == orderflow_result["bias"]:
        score += 10  # Bonus alignement

    ...

    return {
        "final_score": min(100, score),
        "bias": ...,
        "confidence": ...
    }
```

**Appel** : Dans `run_bot.py` après `_analyze_orderflow_v6()`

---

### Option 3 : Scoring Pondéré Configurable

**Fichier** : `config/prod_config.json`

**Ajouter** :
```json
"institutional_scoring": {
  "enabled": true,
  "method": "weighted",  // ou "neural" pour futur ML
  "weights": {
    "orderflow_v6": 0.40,
    "price_memory": 0.10,
    "market_fatigue": 0.15,
    "market_physics": 0.10,
    "microstructure": 0.10,
    "liquidity_heatmap": 0.10,
    "theta_flow": 0.05,  // À activer plus tard
    ...
  },
  "veto_rules": {
    "market_fatigue_exhausted": true,  // VETO si EXHAUSTED
    "physics_contra_bias": false       // Pas de veto si physics oppose bias
  }
}
```

---

## 📝 DOCUMENTATION PRÊTE

Vous avez maintenant :
1. ✅ Flux complet des données (MT5 → Décision finale)
2. ✅ Détail de chaque étape de calcul OrderFlow V6
3. ✅ Structure des inputs/outputs de chaque fonction
4. ✅ Points d'extension pour nouveau système
5. ✅ Options d'architecture pour scoring évolutif

**Vous pouvez maintenant concevoir votre nouveau système de scoring en sachant exactement :**
- Quelles données sont disponibles
- Où et comment les récupérer
- Où insérer votre nouveau calcul
- Comment le rendre configurable et évolutif

---

**Quand vous serez prêt avec le nouveau design, envoyez-moi le rapport et je l'implémenterai ! 🚀**
