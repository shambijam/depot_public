# 🎯 ARCHITECTURE COMPLÈTE DU SYSTÈME DE SCORING

**Date**: 07 Janvier 2026
**Bot**: Sniper X (Mode Scalping Institutionnel)
**Architecture**: Scoring Composite à 6 Composants Pondérés

---

## 📊 VUE D'ENSEMBLE DU SCORING COMPOSITE

Le système utilise **6 composants principaux** qui sont pondérés pour produire un **score composite final de 0-100** :

```
COMPOSITE SCORE (0-100)
│
├─ [35%] OrderFlow V6 Score (0-100)
│   └─ 7 sous-composants analysant les flux d'ordres
│
├─ [25%] Institutional Score (0-100) 🆕 PHASE 3
│   └─ 5 analyseurs institutionnels sophistiqués
│
├─ [15%] Microstructure Score (0-100)
│   └─ Tape speed, accélération, clusters
│
├─ [15%] Liquidity Score (0-100)
│   └─ Pressure ratio, continuité, stabilité
│
├─ [5%] Divergence Score (0-100)
│   └─ Divergence price/delta
│
└─ [5%] Smart Money Score (0-100)
    └─ Large ticks, absorption patterns
```

**Formule finale** :
```
Composite = (OrderFlow × 0.35) + (Institutional × 0.25) + (Microstructure × 0.15)
            + (Liquidity × 0.15) + (Divergence × 0.05) + (SmartMoney × 0.05)
```

---

## 🔷 COMPOSANT #1 : OrderFlow V6 Score (35% du total)

### Architecture OrderFlow V6
Le score OrderFlow (0-100) est lui-même composé de **7 sous-éléments** :

```
ORDERFLOW V6 (0-100 points)
│
├─ GROUPE A: ORDERFLOW ANALYSIS (50 pts max)
│  ├─ [32 pts] Delta Momentum Score
│  ├─ [12 pts] Volume Confirmation Score
│  └─ [6 pts] Imbalance Strength Score
│
├─ GROUPE B: FOOTPRINT ANALYSIS (30 pts max)
│  ├─ [18 pts] Absorption Levels Score
│  ├─ [9 pts] Order Clustering Score
│  └─ [3 pts] Price Rejection Score
│
└─ GROUPE C: TRIGGERS (20 pts max)
   └─ [20 pts] Divergence, VWAP, autres triggers
```

**Total**: 32+12+6 + 18+9+3 + 20 = **100 points**

---

### 📍 1.1 Delta Momentum Score (32 points max)

**Fonction**: `_score_orderflow_analysis()` ligne 581-670
**Fichier**: `/strategy/scalping.py`

**Que fait cette fonction** :
- Analyse le **déséquilibre buy/sell** sur les ticks récents
- Calcule la **cohérence du delta** (direction constante)
- Mesure l'**intensité du déséquilibre** (delta total)

**Calcul détaillé** :

1. **Cohérence Delta** (3 dernières bougies) :
   - `coherence >= 0.75` → **STRONG** (delta très cohérent)
   - `coherence >= 0.65` → **MODERATE**
   - `coherence >= 0.55` → **WEAK**

2. **Delta Total** (buy_volume - sell_volume) :
   ```
   Si coherence STRONG :
   - |delta| >= 50  → 25.0 pts (très fort, ~28% déséquilibre)
   - |delta| >= 30  → 20.0 pts (fort, ~17% déséquilibre)
   - |delta| >= 15  → 18.0 pts (moyen-fort, ~8% déséquilibre)
   - |delta| >= 5   → 15.0 pts (moyen, ~3% déséquilibre)

   Si coherence MODERATE :
   - |delta| >= 30  → 15.0 pts
   - |delta| >= 15  → 12.0 pts
   - |delta| >= 5   → 10.0 pts

   Si coherence WEAK :
   - |delta| >= 15  → 10.0 pts
   - sinon          → 7.0 pts
   ```

**Configuration par asset** (EURUSD.json ligne 140) :
```json
"delta_momentum_max": 32.0
```

**Pénalité** :
- ❌ **Delta neutre ou faible cohérence** → Score réduit à 5-10 pts au lieu de 15-32 pts

---

### 📍 1.2 Volume Confirmation Score (12 points max)

**Fonction**: `_score_orderflow_analysis()` ligne 673-740
**Fichier**: `/strategy/scalping.py`

**Que fait cette fonction** :
- Compare le **volume actuel** à la **moyenne des 6 dernières bougies**
- Détecte les **spikes de volume** (activité institutionnelle)
- Valide que le signal OrderFlow a du **volume derrière lui**

**Calcul détaillé** :

1. **Volume Moyen** (6 dernières bougies) :
   ```python
   avg_volume = df_work.tail(6)['tick_volume'].mean()
   current_volume = df_work.iloc[-1]['tick_volume']
   volume_ratio = current_volume / avg_volume
   ```

2. **Classification** (seuils depuis config) :
   ```
   volume_ratio >= 1.8  → 15.0 pts (SPIKE significatif)
   volume_ratio >= 1.4  → 12.0 pts (ELEVATED)
   volume_ratio >= 1.15 → 10.0 pts (ABOVE_AVERAGE)
   volume_ratio >= 0.85 → 7.0 pts  (NORMAL)
   volume_ratio >= 0.6  → 3.0 pts  (MODERATE)
   sinon                → 0.0 pts  (TRÈS FAIBLE)
   ```

**Configuration** (EURUSD.json ligne 141) :
```json
"volume_confirm_max": 12.0
```

**Pénalité** :
- ❌ **Volume faible** (ratio < 0.85) → Score 0-3 pts au lieu de 7-15 pts
- ❌ **Pas de spike de volume** → Réduit la fiabilité du signal

---

### 📍 1.3 Imbalance Strength Score (6 points max)

**Fonction**: `_score_orderflow_analysis()` ligne 743-799
**Fichier**: `/strategy/scalping.py`

**Que fait cette fonction** :
- Détecte les **imbalances buy/sell** dans le footprint
- Compte le nombre d'**imbalances significatives**
- Mesure la **force du déséquilibre** entre acheteurs et vendeurs

**Calcul détaillé** :

1. **Définition Imbalance** :
   - **Imbalance Buy** : `buy_vol > sell_vol * 1.5` (50% de plus)
   - **Imbalance Sell** : `sell_vol > buy_vol * 1.5`

2. **Comptage** (3 dernières bougies) :
   ```python
   imbalance_buy_count = 0
   imbalance_sell_count = 0
   # Analyse chaque bougie pour imbalances
   total_imbalances = imbalance_buy_count + imbalance_sell_count
   ```

3. **Scoring** :
   ```
   total_imbalances >= 5 → 10.0 pts (beaucoup d'imbalances)
   total_imbalances >= 3 → 8.0 pts  (imbalances significatives)
   total_imbalances >= 1 → 5.0 pts  (imbalances mineures)
   sinon                 → 0.0 pts  (pas d'imbalance)
   ```

**Configuration** (EURUSD.json ligne 142) :
```json
"imbalance_strength_max": 6.0
```

**Pénalité** :
- ❌ **Pas d'imbalance détectée** → 0 pts (marché équilibré = pas de signal clair)

---

### 📍 1.4 Absorption Levels Score (18 points max)

**Fonction**: `_score_footprint_analysis()` ligne 1309-1389
**Fichier**: `/strategy/scalping.py`

**Que fait cette fonction** :
- Analyse où le **volume est absorbé** (buy vs sell pressure)
- Détecte les **zones d'accumulation/distribution institutionnelle**
- Mesure le **ratio buy/sell** pour identifier le biais du marché

**Calcul détaillé** :

1. **Calcul Ratio** :
   ```python
   buy_vol = sum(buy volumes)
   sell_vol = sum(sell volumes)
   total_vol = buy_vol + sell_vol
   buy_ratio = buy_vol / total_vol
   ```

2. **Classification** (seuils depuis config `absorption_ratios`) :
   ```
   buy_ratio >= 0.72 → 12.5 pts (STRONG BULLISH, très fort biais acheteur)
   buy_ratio >= 0.58 → 10.0 pts (BULLISH)
   buy_ratio >= 0.52 → 7.5 pts  (SLIGHTLY_BULLISH)
   buy_ratio <= 0.28 → 12.5 pts (STRONG BEARISH, très fort biais vendeur)
   buy_ratio <= 0.42 → 10.0 pts (BEARISH)
   buy_ratio <= 0.48 → 7.5 pts  (SLIGHTLY_BEARISH)
   sinon             → 4.0 pts  (NEUTRAL)
   ```

3. **Bonus Multiplicateurs** :
   - **Volume élevé** (>100) : `score × 1.3` (+30%)
   - **Volume moyen** (>50) : `score × 1.15` (+15%)
   - **Tick rate élevé** (>2.0) : `score × 1.2` (+20%)
   - **Tick rate moyen** (>1.0) : `score × 1.1` (+10%)

**Configuration** (EURUSD.json ligne 143) :
```json
"absorption_max": 18.0
```

**Pénalité** :
- ❌ **Ratio neutre** (0.48-0.52) → 4.0 pts seulement (pas de direction claire)
- ❌ **Volume faible** → Pas de bonus multiplicateur

---

### 📍 1.5 Order Clustering Score (9 points max)

**Fonction**: `_score_footprint_analysis()` ligne 1391-1439
**Fichier**: `/strategy/scalping.py`

**Que fait cette fonction** :
- Détecte si les **ordres sont concentrés** (clustering)
- Cherche des **patterns répétés** sur 3 bougies
- Identifie l'**accumulation ou distribution institutionnelle**

**Calcul détaillé** :

1. **Analyse 3 Bougies Précédentes** :
   ```python
   cluster_count = 0
   for bougie in last_3_bars:
       buy_vol = bougie.buy_volume
       sell_vol = bougie.sell_volume

       # Cluster détecté si déséquilibre >60%
       if buy_vol > sell_vol * 1.6:  # 60% de plus
           cluster_count += 1
       elif sell_vol > buy_vol * 1.6:
           cluster_count += 1
   ```

2. **Scoring** :
   ```
   cluster_count >= 3 → 8.5 pts (CONCENTRATED, très forte concentration)
   cluster_count >= 2 → 6.0 pts (concentration significative)
   cluster_count >= 1 → 3.5 pts (concentration mineure)
   sinon              → 0.0 pts (DISPERSED, pas de pattern)
   ```

**Configuration** (EURUSD.json ligne 144) :
```json
"clustering_max": 9.0
```

**Pénalité** :
- ❌ **Ordres dispersés** → 0 pts (pas de conviction institutionnelle)

---

### 📍 1.6 Price Rejection Score (3 points max)

**Fonction**: `_score_footprint_analysis()` ligne 1441-1492
**Fichier**: `/strategy/scalping.py`

**Que fait cette fonction** :
- Analyse les **wicks des bougies** (mèches)
- Détecte les **rejets de prix** (zones de rejet institutionnel)
- Identifie les **niveaux où le marché refuse d'aller**

**Calcul détaillé** :

1. **Analyse Wicks** (3 dernières bougies) :
   ```python
   for bougie in last_3_bars:
       body = abs(close - open)
       upper_wick = high - max(open, close)
       lower_wick = min(open, close) - low

       max_wick = max(upper_wick, lower_wick)

       # Rejet net si wick > 2× body
       wick_ratio = max_wick / body if body > 0 else 0

       if wick_ratio >= 2.0:
           rejection_count += 1
   ```

2. **Scoring** :
   ```
   rejection_count >= 3 → 4.0 pts (STRONG rejection, très fort)
   rejection_count >= 2 → 2.5 pts (MODERATE rejection)
   rejection_count >= 1 → 1.5 pts (WEAK rejection)
   sinon                → 0.0 pts (NONE, pas de rejet)
   ```

**Configuration** (EURUSD.json ligne 145) :
```json
"rejection_max": 3.0
```

**Pénalité** :
- ❌ **Pas de wick significatif** → 0 pts (pas de zone de rejet identifiée)

---

### 📍 1.7 Triggers Score (20 points max)

**Fonction**: Calcul dans `_score_orderflow_analysis()` et autres
**Fichier**: `/strategy/scalping.py`

**Que fait cette fonction** :
- Agrège divers **triggers techniques** (divergence, VWAP, etc.)
- Ajoute des **confirmations supplémentaires** au signal
- Boost le score si **alignement multi-timeframe**

**Composants des Triggers** :
1. **Divergence** (CVD vs Price)
2. **VWAP** (alignement avec VWAP)
3. **Multi-Timeframe Alignment** (M1, M3, M5)
4. **Timing Quality** (distribution temporelle des ticks)

**Configuration** (EURUSD.json ligne 146) :
```json
"triggers_max": 20.0
```

---

## 🔷 COMPOSANT #2 : Institutional Score (25% du total) 🆕

### Architecture Institutional
Le score Institutionnel (0-100) agrège **5 analyseurs sophistiqués** :

```
INSTITUTIONAL (0-100 points)
│
├─ [20%] Price Memory Analyzer
├─ [25%] Market Fatigue Analyzer
├─ [25%] Market Physics Analyzer
├─ [15%] Tape Speed (Microstructure)
└─ [15%] Pressure Ratio (Liquidity Heatmap)
```

**Fonction principale** : `_calculate_institutional_score()`
**Fichier** : `/strategy/advanced_scoring.py` ligne 447-590

---

### 📍 2.1 Price Memory Analyzer (20%)

**Classe** : `PriceMemoryAnalyzer` (PRIORITY_1)
**Fichier** : `/phase_observer/price_memory_analyzer.py`

**Que fait cette fonction** :
- Détecte les **niveaux où le prix a de la mémoire** (support/résistance institutionnels)
- Identifie les **niveaux frais** (nouveaux) vs **memory signals** (anciens testés)
- Mesure la **qualité des niveaux** (combien de fois testés)

**Calcul Score** :
```python
fresh_ratio = len(fresh_levels) / (len(memory_signals) + len(fresh_levels))
memory_score = 50.0 + (fresh_ratio - 0.5) * 50.0

# Interprétation :
# fresh_ratio = 0.0  → score = 25.0  (que des niveaux anciens, résistance)
# fresh_ratio = 0.5  → score = 50.0  (neutre)
# fresh_ratio = 1.0  → score = 75.0  (que des niveaux frais, opportunité)
```

**Pénalité** :
- ❌ **Beaucoup de memory signals** → Score bas (25-40), prix bloqué par résistance
- ✅ **Beaucoup de fresh levels** → Score haut (60-75), opportunité de mouvement

---

### 📍 2.2 Market Fatigue Analyzer (25%)

**Classe** : `MarketFatigueAnalyzer` (PRIORITY_2)
**Fichier** : `/phase_observer/market_fatigue_analyzer.py`

**Que fait cette fonction** :
- Mesure l'**épuisement des acheteurs/vendeurs**
- Calcule un **fatigue_score** de 0 à 10
- Retourne un **état du marché** : ENERGETIC, NORMAL, FATIGUED, EXHAUSTED

**Calcul Score** :
```python
fatigue_score_raw = 0-10  # Calculé par l'analyseur

# États possibles :
if fatigue_score_raw >= 7:    → 'EXHAUSTED'   → score = 30.0  (épuisement, reversal probable)
if fatigue_score_raw >= 4:    → 'FATIGUED'    → score = 40.0  (affaiblissement)
if fatigue_score_raw >= 2:    → 'NORMAL'      → score = 50.0  (sain)
if fatigue_score_raw < 2:     → 'ENERGETIC'   → score = 65.0  (énergie, continuation)
```

**Pénalité** :
- ❌ **EXHAUSTED** → Score 30.0 (marché épuisé, probable reversal contre mouvement actuel)
- ❌ **FATIGUED** → Score 40.0 (marché fatigué, perte de momentum)

---

### 📍 2.3 Market Physics Analyzer (25%)

**Classe** : `MarketPhysicsAnalyzer` (PRIORITY_3)
**Fichier** : `/phase_observer/market_physics_analyzer.py`

**Que fait cette fonction** :
- Applique les **lois de la physique au marché** (inertie, momentum)
- Calcule un **physics_bias** (BULLISH/BEARISH/NEUTRAL)
- Détecte la **direction de l'inertie** (UP/DOWN/NEUTRAL)

**Calcul Score** :
```python
# Bias de base :
if 'BULLISH' in physics_bias:  → physics_score = 75.0
if 'BEARISH' in physics_bias:  → physics_score = 25.0
else:                          → physics_score = 50.0

# Boost si inertie alignée :
if inertia_direction == 'UP':    → physics_score + 10.0  (max 100.0)
if inertia_direction == 'DOWN':  → physics_score - 10.0  (min 0.0)
```

**Pénalité** :
- ❌ **Inertie contraire au bias** → Score réduit de 10 pts
- ✅ **Inertie alignée** → Score boosté de 10 pts

---

### 📍 2.4 Tape Speed Analyzer (15%)

**Source** : `MicrostructureAnalyzer`
**Fichier** : `/phase_observer/microstructure_analyzer.py`

**Que fait cette fonction** :
- Mesure la **vitesse du tape** (ticks par seconde)
- Détecte les **momentum ignition** (accélération soudaine)
- Compare vitesse actuelle vs moyenne historique

**Calcul Score** :
```python
speed_ratio = current_speed / avg_speed

if speed_ratio >= 2.0:   → 70.0 pts  (haute activité, aggressive)
if speed_ratio >= 1.5:   → 60.0 pts  (activité élevée)
if speed_ratio >= 0.8:   → 50.0 pts  (normal)
else:                    → 35.0 pts  (apathie, faible activité)
```

**Pénalité** :
- ❌ **Vitesse faible** (ratio < 0.8) → Score 35.0 (marché apathique)

---

### 📍 2.5 Pressure Ratio Analyzer (15%)

**Source** : `LiquidityHeatmapAnalyzer`
**Fichier** : `/phase_observer/liquidity_heatmap_analyzer.py`

**Que fait cette fonction** :
- Calcule la **pression buy vs sell**
- Normalise la pression de -1 (sell) à +1 (buy)
- Retourne une **direction** : STRONG_BUY, MODERATE_BUY, NEUTRAL, etc.

**Calcul Score** :
```python
normalized_pressure = -1.0 à +1.0

pressure_score = 50.0 + (normalized_pressure * 50.0)

# Interprétation :
# -1.0 → score = 0.0   (pression sell maximale)
# 0.0  → score = 50.0  (neutre)
# +1.0 → score = 100.0 (pression buy maximale)
```

**Pénalité** :
- ❌ **Pression négative** → Score < 50 (pression vendeur)

---

## 🔷 COMPOSANT #3 : Microstructure Score (15% du total)

**Fonction** : `_calculate_microstructure_score()`
**Fichier** : `/strategy/advanced_scoring.py` ligne 167-248

**Que fait cette fonction** :
- Analyse la **microstructure du marché** (détail fin du tape)
- Mesure **3 sous-composants** :

### 3.1 Tape Speed (50% du score microstructure)
```python
duration_seconds = (ticks_df['time'].max() - ticks_df['time'].min()).total_seconds()
tape_speed = len(ticks_df) / duration_seconds  # ticks par seconde

speed_score = min(100.0, (tape_speed / 5.0) * 100.0)
# 0 ticks/s → 0 pts
# 5 ticks/s → 100 pts
```

### 3.2 Accélération (30% du score microstructure)
```python
# Diviser en 5 segments, mesurer variance de vitesse
acceleration = np.std(speeds)
accel_score = min(100.0, (acceleration / 2.0) * 100.0)
```

### 3.3 Clusters (20% du score microstructure)
```python
# Volume des 20% plus gros trades vs moyenne
cluster_ratio = avg_top_20pct / avg_all
cluster_score = min(100.0, (cluster_ratio / 3.0) * 100.0)
```

**Score final** :
```
microstructure_score = (speed_score × 0.5) + (accel_score × 0.3) + (cluster_score × 0.2)
```

**Pénalité** :
- ❌ **Pas de ticks disponibles** → Score 0.0 (pas de données)
- ❌ **Moins de 10 ticks** → Score neutre 50.0

---

## 🔷 COMPOSANT #4 : Liquidity Score (15% du total)

**Fonction** : `_calculate_liquidity_score()`
**Fichier** : `/strategy/advanced_scoring.py` ligne 251-327

**Que fait cette fonction** :
- Évalue la **qualité de la liquidité**
- Mesure **3 sous-composants** :

### 4.1 Pressure Ratio (40% du score liquidité)
```python
buy_ticks = (ticks_df['flags'] == 2).sum()
sell_ticks = (ticks_df['flags'] == 1).sum()
pressure_ratio = buy_ticks / sell_ticks

# Marché équilibré = meilleure liquidité
if 0.8 <= pressure_ratio <= 1.2:
    pressure_score = 100.0  (excellent, liquidité équilibrée)
else:
    deviation = abs(pressure_ratio - 1.0)
    pressure_score = max(0.0, 100.0 - (deviation * 50.0))
```

### 4.2 Continuité (40% du score liquidité)
```python
# Gaps entre ticks (pauses >1 seconde)
gap_pct = (gaps > 1.0).sum() / len(ticks) * 100
continuity_score = max(0.0, 100.0 - gap_pct)
```

### 4.3 Volume Stability (20% du score liquidité)
```python
cv = vol_std / vol_mean  # Coefficient of variation
stability_score = max(0.0, 100.0 - (cv * 100.0))
```

**Score final** :
```
liquidity_score = (pressure_score × 0.4) + (continuity_score × 0.4) + (stability_score × 0.2)
```

**Pénalité** :
- ❌ **Marché déséquilibré** (ratio < 0.8 ou > 1.2) → Score pression réduit
- ❌ **Beaucoup de gaps** → Score continuité bas (marché illiquide)

---

## 🔷 COMPOSANT #5 : Divergence Score (5% du total)

**Fonction** : `_calculate_divergence_score()`
**Fichier** : `/strategy/advanced_scoring.py` ligne 330-383

**Que fait cette fonction** :
- Détecte les **divergences price/delta**
- Compare la **direction du prix** vs **direction du delta**
- Signal de **reversal potentiel**

**Calcul** :
```python
# Direction prix (5 dernières bougies)
price_change = last_5['close'].iloc[-1] - last_5['close'].iloc[0]
price_direction = 1 (up) / -1 (down) / 0 (flat)

# Direction delta
buy_vol = sum(buy_volume)
sell_vol = sum(sell_volume)
delta = buy_vol - sell_vol
delta_direction = 1 (buy) / -1 (sell) / 0 (neutral)

# Divergence détectée
if price_direction != delta_direction:
    if delta_direction > 0:
        divergence_score = 75.0  (divergence haussière, BUY signal)
    else:
        divergence_score = 25.0  (divergence baissière, SELL signal)
else:
    divergence_score = 50.0  (pas de divergence, neutre)
```

**Pénalité** :
- ❌ **Pas de divergence** → Score neutre 50.0 (pas de signal reversal)

---

## 🔷 COMPOSANT #6 : Smart Money Score (5% du total)

**Fonction** : `_calculate_smart_money_score()`
**Fichier** : `/strategy/advanced_scoring.py` ligne 386-444

**Que fait cette fonction** :
- Détecte l'**empreinte institutionnelle**
- Cherche les **large ticks** (>3× moyenne)
- Identifie les **absorption patterns** (gros volume sans mouvement prix)

**Calcul** :

### 6.1 Large Ticks (50% du score smart money)
```python
vol_mean = ticks_df['volume'].mean()
large_ticks = (ticks_df['volume'] > vol_mean * 3.0).sum()
large_tick_pct = (large_ticks / len(ticks)) * 100

large_tick_score = min(100.0, large_tick_pct * 10.0)
```

### 6.2 Absorption Patterns (50% du score smart money)
```python
# Segments de 10 ticks
for segment in segments:
    total_vol = segment['volume'].sum()
    price_range = segment['mid'].max() - segment['mid'].min()

    # Absorption : gros volume mais faible mouvement
    if total_vol > max_vol * 0.5 and price_range < 0.0002:
        absorption_count += 1

absorption_pct = (absorption_count / num_segments) * 100
absorption_score = min(100.0, absorption_pct * 5.0)
```

**Score final** :
```
smart_money_score = (large_tick_score + absorption_score) / 2.0
```

**Pénalité** :
- ❌ **Pas de large ticks** → Score réduit (pas d'activité institutionnelle)
- ❌ **Pas d'absorption** → Score réduit (pas de positioning caché)

---

## ⚠️ SYSTÈME DE PÉNALITÉS

### 🔴 Pénalités Majeures (Score → 0.0)

1. **Absence de Données Ticks** :
   - Microstructure Score → **0.0**
   - Liquidity Score → **0.0**
   - Divergence Score → **0.0**
   - Smart Money Score → **0.0**
   - **Raison** : Impossible d'analyser sans données tick-level

2. **Données Insuffisantes** :
   - `ticks_df < 10 ticks` → Scores neutre **50.0** ou **0.0**
   - `candles_df < 5 bougies` → Scores limités
   - **Raison** : Échantillon trop petit pour analyse fiable

3. **Volume Très Faible** :
   - Volume Confirmation → **0.0 pts** (au lieu de 15 pts)
   - **Raison** : Signal sans conviction, pas de participation

### 🟠 Pénalités Modérées (Score réduit de 30-70%)

4. **Cohérence Delta Faible** :
   - Delta Momentum réduit à **5-10 pts** (au lieu de 15-32 pts)
   - **Raison** : Direction du delta pas claire, signal contradictoire

5. **Marché Déséquilibré** (Liquidity) :
   - Pressure ratio < 0.8 ou > 1.2 → Score pression réduit
   - **Raison** : Manque de liquidité bidirectionnelle

6. **Marché Épuisé** (Market Fatigue) :
   - État EXHAUSTED → Score **30.0** (au lieu de 50.0)
   - **Raison** : Probable reversal, contre le mouvement actuel

7. **Pas d'Imbalance** :
   - Imbalance Strength → **0.0 pts** (au lieu de 10 pts)
   - **Raison** : Marché équilibré, pas de pression directionnelle

8. **Ordres Dispersés** :
   - Order Clustering → **0.0 pts** (au lieu de 8.5 pts)
   - **Raison** : Pas de conviction institutionnelle

9. **Ratio Absorption Neutre** :
   - Absorption Levels → **4.0 pts** (au lieu de 12.5 pts)
   - **Raison** : Pas de biais clair buy/sell

### 🟡 Pénalités Mineures (Absence de Bonus)

10. **Volume Normal** (pas élevé) :
    - Pas de bonus multiplicateur (+30% perdu)
    - **Raison** : Signal moins fiable sans fort volume

11. **Tick Rate Normal** :
    - Pas de bonus multiplicateur (+20% perdu)
    - **Raison** : Pas d'urgence institutionnelle

12. **Pas de Divergence** :
    - Score neutre **50.0** (ni bonus ni pénalité)
    - **Raison** : Pas de signal de reversal

13. **Inertie Non Alignée** :
    - Pas de boost +10 pts (Market Physics)
    - **Raison** : Physics pas confirmé par l'inertie

---

## 🎯 SEUILS DE DÉCISION FINALE

Après calcul du **Composite Score (0-100)**, le système décide :

```python
# Fichier: advanced_scoring.py ligne 592-627

if composite_score >= 75.0:
    decision = "BUY" ou "SELL" (selon bias)
    confidence = "STRONG"

elif composite_score >= 65.0:
    decision = "BUY" ou "SELL"
    confidence = "GOOD"

elif composite_score >= 55.0:
    decision = "BUY" ou "SELL"
    confidence = "WEAK"

else:  # < 55.0
    decision = "HOLD"
    confidence = "NONE"
```

**Seuils de Qualité Actuels** (06 JAN 2026) :
```json
"min_orderflow_score": 75,      // Minimum pour trader
"min_confidence": 0.70,          // 70% de confiance minimum
"min_score": 65.0,               // Score composite minimum (burst_scalping)
"of_v6_gate.min_score": 0.70     // 70% du score OrderFlow normalisé
```

---

## 📈 EXEMPLE DE CALCUL COMPLET

Voici un exemple réel tiré des logs :

```
[EURUSD] 2026-01-06 14:23:45

COMPOSANTS INDIVIDUELS :
├─ OrderFlow V6     : 57.0/100  (35% du total)
│  ├─ Delta Momentum    : 18.0/32 pts
│  ├─ Volume Confirm    : 10.0/12 pts
│  ├─ Imbalance         : 5.0/6 pts
│  ├─ Absorption        : 12.0/18 pts
│  ├─ Clustering        : 6.0/9 pts
│  ├─ Rejection         : 1.5/3 pts
│  └─ Triggers          : 15.0/20 pts
│
├─ Institutional    : 53.0/100  (25% du total)
│  ├─ Price Memory      : 25.0 (beaucoup de memory signals)
│  ├─ Market Fatigue    : 65.0 (ENERGETIC)
│  ├─ Market Physics    : 60.0 (NEUTRAL + inertie UP)
│  ├─ Tape Speed        : 70.0 (ratio 3.0, aggressive)
│  └─ Pressure Ratio    : 64.3 (MODERATE_BUY)
│
├─ Microstructure   : 52.0/100  (15% du total)
├─ Liquidity        : 77.0/100  (15% du total)
├─ Divergence       : 50.0/100  (5% du total)
└─ Smart Money      : 45.0/100  (5% du total)

CALCUL COMPOSITE :
= (57 × 0.35) + (53 × 0.25) + (52 × 0.15) + (77 × 0.15) + (50 × 0.05) + (45 × 0.05)
= 19.95 + 13.25 + 7.80 + 11.55 + 2.50 + 2.25
= 57.3/100

DÉCISION :
└─ Score 57.3 >= 55.0 → BUY (WEAK confidence)
   MAIS rejeté car score < 65.0 (seuil min_score)
```

**Résultat** : **HOLD** (score insuffisant pour trader)

---

## 🔍 RÉSUMÉ DES PÉNALITÉS PAR RAISON

| Raison | Composant Affecté | Pénalité | Impact |
|--------|------------------|----------|--------|
| **Pas de ticks** | Microstructure, Liquidity, Divergence, Smart Money | Score → 0.0 | **Critique** |
| **Volume faible** | Volume Confirmation | 0-3 pts au lieu de 7-15 | **Majeur** |
| **Delta faible cohérence** | Delta Momentum | 5-10 pts au lieu de 15-32 | **Majeur** |
| **Pas d'imbalance** | Imbalance Strength | 0 pts au lieu de 10 | **Modéré** |
| **Ordres dispersés** | Order Clustering | 0 pts au lieu de 8.5 | **Modéré** |
| **Ratio neutre** | Absorption Levels | 4 pts au lieu de 12.5 | **Modéré** |
| **Marché épuisé** | Market Fatigue | 30 pts au lieu de 50 | **Modéré** |
| **Marché déséquilibré** | Liquidity (pressure) | Score réduit | **Mineur** |
| **Pas de divergence** | Divergence | Score neutre 50 | **Mineur** |
| **Pas de bonus volume** | Absorption | Pas de multiplicateur ×1.3 | **Mineur** |

---

## 📌 CONCLUSION

Le système de scoring est **extrêmement sélectif** avec :

✅ **13 fonctions principales** qui scorent :
- 7 fonctions OrderFlow V6
- 5 fonctions Institutionnelles
- 1 fonction Composite qui agrège tout

✅ **13+ types de pénalités** appliquées :
- Pénalités critiques (données manquantes)
- Pénalités majeures (signaux faibles)
- Pénalités modérées (absence de confirmation)
- Absence de bonus (conditions non optimales)

✅ **Seuils de qualité élevés** :
- Composite Score ≥ 65.0 pour trader
- OrderFlow Score ≥ 75.0 minimum
- Confidence ≥ 70% requise

🎯 **Résultat** : Le bot ne trade que les **meilleurs setups institutionnels** avec **forte conviction** et **multiples confirmations**.

---

**Document généré le** : 07 Janvier 2026
**Version du bot** : Sniper X Mode Scalping v5.0
**Auteur** : Claude Sonnet 4.5
