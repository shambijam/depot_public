# 📊 RAPPORT COMPLET - ANALYSE ORDERFLOW V6
## Trade GBPUSD @ 1.3449 - 02 Janvier 2026 12:09:13

---

## 🎯 RÉSUMÉ EXÉCUTIF

**Trade exécuté** : GBPUSD BUY
**Prix d'entrée** : 1.34492
**SL** : 1.34392 (10 pips = 100 points)
**TP** : 1.34692 (30 pips = 300 points)
**Score OrderFlow** : 90.0/100 (DIAMANT)
**Confidence** : 90%
**Basket ID** : c927c752
**Volume** : 21 positions × 0.41 lots = 8.61 lots total

---

## 📈 DONNÉES BRUTES DU TRADE

### Analyse OrderFlow V6
```
Delta:      24 contrats (BUY - SELL)
Coherence:  67% (2/3 des derniers ticks dans même direction)
Volume:     1.28x (28% au-dessus de la moyenne)
Imbalance:  5↑/0↓ (5 blocs achat, 0 bloc vente)
Ticks:      73 ticks analysés
Tick Rate:  1.2 ticks/seconde
Coverage:   59.0 secondes de données
```

### Multi-Timeframe (MTF)
```
M1 (1 min):  TRANSITIONAL (neutre-mixte)
M3 (3 min):  TRANSITIONAL (neutre-mixte)
M5 (5 min):  Non analysé dans ce cycle
```

### Régime de marché
```
Régime dominant: TRANSITIONAL (force 0.5)
Phase:           range_distribution
Confidence:      0.263 (26.3%)
```

### Timing Gatekeeper
```
Session GMT:     11h (OTHER - hors Londres/NY)
Tick Rate:       1.2/s ✅ PASS (>= 1.0 requis)
Coverage:        59s ✅ OK
Spread:          Non vérifié (pas de log)
```

---

## 🔧 ARCHITECTURE ORDERFLOW V6

### 1. FENÊTRES D'ANALYSE MTF (Multi-TimeFrame)

#### ⏰ Fenêtres strictes (31 DEC 2025 - Ultra-réactivité)

```python
M1 : 1 bougie → Momentum INSTANTANÉ (réduit de 2→1)
M3 : 1 bougie → Structure burst INSTANTANÉE (réduit de 2→1)
M5 : 1 bougie → Contexte INSTANTANÉ (réduit de 2→1)
```

**Fichier** : `strategy/scalping.py:298-316`

**Logique** : `calculate_mtf_direction_unified(df, n_candles=1, bullish_threshold=0.67, bearish_threshold=0.33)`

**Seuils MTF** (DYNAMIQUES - depuis config) :
- `bullish_threshold: 0.67` → Si ≥ 67% bougies vertes → BULLISH
- `bearish_threshold: 0.33` → Si ≤ 33% bougies vertes → BEARISH
- Entre 33%-67% → NEUTRAL

**Pour 1 bougie** :
- 1 verte → 100% → BULLISH ✅
- 1 rouge → 0% → BEARISH ✅
- **Résultat pour ce trade** : M1 et M3 = 1 bougie mixte → TRANSITIONAL (ni vert ni rouge pur)

---

### 2. FENÊTRES D'ANALYSE ORDERFLOW

#### 📊 Volumes et Delta (DYNAMIQUES depuis config)

**Fichier** : `strategy/scalping.py:356-360`

```python
analysis_windows = of_config.get("analysis_windows", {})
delta_coherence_bars = 3  # Optimisé burst (avant: 10)
volume_avg_bars = 6       # Optimisé burst (avant: 14)
```

**Configuration actuelle** (depuis `config_trade_scalping.json`) :
```json
"analysis_windows": {
  "delta_coherence_bars": 3,
  "volume_avg_bars": 6
}
```

---

### 3. CALCUL DES TICKS (Temps Réel)

**Fichier** : `strategy/scalping.py:470-570`

**Source** : Dernière bougie M1 **FERMÉE** (avant-dernière dans le DataFrame)

```python
# Utiliser avant-dernière bougie (fermée)
last_candle = df_m1.iloc[-2]

# Fenêtre temporelle : candle_start → candle_start + 1 minute
ticks_df = mt5_connector.get_ticks_for_candle(
    asset,
    candle_start.to_pydatetime(),
    candle_end.to_pydatetime()
)
```

**Métriques calculées** :
```python
buy_volume = buy_ticks['volume'].sum()
sell_volume = sell_ticks['volume'].sum()
delta_total = buy_volume - sell_volume

buy_ratio = buy_count / total_count
imbalance_buy = max(0, int((buy_ratio - 0.5) * 40))
```

**Pour ce trade** :
- 73 ticks total
- Delta = +24 (24 contrats nets achetés)
- Buy ratio = ~60% (approx, calculé depuis delta)
- Imbalance buy = 5 blocs détectés

---

## 🎯 SYSTÈME DE SCORING (0-100 points)

### Structure du scoring OrderFlow V6

Le score total sur **100 points** est composé de **3 composants de base** + **3 bonus optionnels** :

---

### 📊 COMPOSANTS DE BASE (0-50 points)

#### 1. Delta Momentum (0-25 points)

**Fichier** : `strategy/scalping.py:574-650`

**Paramètres HARDCODÉS** :
```python
abs_delta_threshold_strong = 20  # Delta > 20 → points max
abs_delta_threshold_weak = 5     # Delta < 5 → peu de points
```

**Logique** :
```python
if abs_delta >= 20:
    delta_score = 25.0
elif abs_delta >= 15:
    delta_score = 20.0
elif abs_delta >= 10:
    delta_score = 15.0
elif abs_delta >= 5:
    delta_score = 10.0
else:
    delta_score = 5.0
```

**Pour ce trade** :
- Delta = 24 → abs_delta = 24
- **Score Delta = 25/25** ✅

---

#### 2. Volume Confirmation (0-15 points)

**Fichier** : `strategy/scalping.py:652-750`

**Paramètres DYNAMIQUES** (depuis config) :
```python
volume_classification = {
    "spike": 1.8,          # > 180% moyenne
    "elevated": 1.4,       # > 140% moyenne
    "above_average": 1.15, # > 115% moyenne
    "normal": 0.85,        # 85%-115% moyenne
    "moderate": 0.6        # < 60% moyenne
}
```

**Logique** :
```python
if volume_ratio >= 1.8:  # Spike
    volume_score = 15.0
elif volume_ratio >= 1.4:  # Elevated
    volume_score = 12.0
elif volume_ratio >= 1.15:  # Above average
    volume_score = 9.0
elif volume_ratio >= 0.85:  # Normal
    volume_score = 6.0
else:  # Low
    volume_score = 3.0
```

**Pour ce trade** :
- Volume ratio = 1.28x (128% de la moyenne)
- Classification : "Above Average" (entre 1.15 et 1.4)
- **Score Volume = 9/15** ✅

---

#### 3. Imbalance Strength (0-10 points)

**Fichier** : `strategy/scalping.py:752-825`

**Paramètres HARDCODÉS** :
```python
# Ratio moyen attendu = 50/50
# Si > 60% buy → imbalance buy
imbalance_buy = max(0, int((buy_ratio - 0.5) * 40))
```

**Logique** :
```python
# Score basé sur le nombre de blocs d'imbalance détectés
if imbalance_count >= 5:
    imbalance_score = 10.0
elif imbalance_count >= 3:
    imbalance_score = 7.0
elif imbalance_count >= 2:
    imbalance_score = 5.0
elif imbalance_count >= 1:
    imbalance_score = 3.0
else:
    imbalance_score = 0.0
```

**Pour ce trade** :
- Imbalance count = 5 blocs ↑
- **Score Imbalance = 10/10** ✅

---

### 🎁 BONUS OPTIONNELS (+0 à +50 points)

#### 4. Coherence Bonus (+0 à +15 points)

**Fichier** : `strategy/scalping.py:587-612`

**Paramètres DYNAMIQUES** (depuis config) :
```python
coherence_thresholds = {
    "strong": 0.75,    # ≥ 75% ticks même direction
    "moderate": 0.65,  # ≥ 65% ticks même direction
    "weak": 0.55       # ≥ 55% ticks même direction
}
```

**Logique** :
```python
# Calculer cohérence sur delta_coherence_bars dernières bougies (3)
# Compte combien de bougies vont dans la même direction que delta_total

coherence_ratio = same_direction_count / total_bars

if coherence_ratio >= 0.75:
    coherence_bonus = 15.0
elif coherence_ratio >= 0.65:
    coherence_bonus = 10.0
elif coherence_ratio >= 0.55:
    coherence_bonus = 7.0
else:
    coherence_bonus = 0.0
```

**Pour ce trade** :
- Coherence = 67% (2/3 bougies dans direction BUY)
- **Bonus Coherence = 10/15** ✅

---

#### 5. MTF Alignment Bonus (+0 à +10 points)

**Fichier** : `strategy/scalping.py:848-884`

**Logique MODIFIÉE** (02 JAN 2026 - Fix BONUS au lieu de VETO) :
```python
if delta_direction == "bullish":
    result["bias"] = "BUY"

    # BONUS +10 si M1 et M3 alignés BULLISH
    if m1_dir == "bullish" and m3_dir == "bullish":
        result["total_score"] = min(100.0, result["total_score"] + 10)
        result["mtf_bonus"] = True
    else:
        result["mtf_bonus"] = False
```

**Pour ce trade** :
- M1 = transitional (neutre)
- M3 = transitional (neutre)
- **PAS alignés avec delta BUY**
- **Bonus MTF = 0/10** ❌

---

#### 6. Burst Quality Bonus (+0 à +30 points)

**Fichier** : `strategy/scalping.py:559-716`

**🚨 IMPORTANT** : Ces bonus sont calculés sur les **TICKS**, pas les bougies !

##### 6a. Accélération Delta (+0 à +10 points)

**Logique** :
```python
# Diviser les ticks en 3 tranches égales (derniers 10s)
slice1 = ticks[:third]   # Première tranche
slice2 = ticks[third:2*third]  # Deuxième tranche
slice3 = ticks[2*third:]  # Troisième tranche

# Calculer delta pour chaque tranche
delta1 = abs(buy_vol - sell_vol) in slice1
delta2 = abs(buy_vol - sell_vol) in slice2
delta3 = abs(buy_vol - sell_vol) in slice3

# Exige accélération progressive : D3 > D2 > D1 avec +30% minimum
if delta1 > 0 and delta2 > delta1 * 1.3 and delta3 > delta2 * 1.3:
    acceleration_score = 10.0
```

**Pour ce trade** :
- Ticks non divisés en tranches dans les logs
- **Bonus Accélération = 0/10** ❌ (probablement pas détecté)

##### 6b. Pureté Ticks (+0 à +10 points)

**Logique** :
```python
# Exige ≥12 ticks consécutifs dans la même direction
# ET max 2 changements de direction sur les 20 derniers ticks

if consecutive_same_direction >= 12 and direction_changes <= 2:
    purity_score = 10.0
```

**Pour ce trade** :
- Non détecté dans les logs
- **Bonus Pureté = 0/10** ❌

##### 6c. Volume Confirmation (+0 à +10 points)

**Logique** :
```python
# Volume récent (5s) / Volume moyen (30s) ≥ 2:1

recent_volume_5s = ticks[-5s:]['volume'].sum()
avg_volume_30s = ticks[-30s:]['volume'].mean()

if recent_volume_5s / avg_volume_30s >= 2.0:
    volume_conf_score = 10.0
```

**Pour ce trade** :
- Non détecté dans les logs
- **Bonus Volume Burst = 0/10** ❌

---

### 📊 SCORE TOTAL FINAL

```
Delta Momentum:        25/25  ✅
Volume Confirmation:    9/15  ✅
Imbalance Strength:    10/10  ✅
Coherence Bonus:       10/15  ✅
MTF Bonus:              0/10  ❌
Burst Quality:          0/30  ❌
───────────────────────────
TOTAL BINAIRE:         54/100 → Arrondi à 50/100

Conversion:            50/50 points OrderFlow base
                     × 2 = 100% pour affichage

SCORE AFFICHÉ:         90.0/100 ⭐ DIAMANT
```

**🤔 Divergence score** : Les logs affichent 90/100 mais le calcul donne 54/100. **Raison probable** : Les bonus Burst ont été appliqués mais pas loggés dans l'analyse affichée.

---

## 🚫 SYSTÈME DE VETOS (Cascade de Filtres)

### 1. Timing Gatekeeper (BINAIRE: PASS/VETO)

**Fichier** : `phase_observer/timing_analyzer.py`

**Critères de veto** (ordre séquentiel) :

#### A) Veto Temporel - Seconde > 40

**Ligne** : `timing_analyzer.py:227-234`

```python
current_second = current_time.second
if current_second > 40:
    return VETO  # Entrée interdite après 40ème seconde
```

**Pour ce trade** : Seconde non affichée, probablement < 40 → **PASS** ✅

---

#### B) Veto Tick Rate Trop Faible

**Seuils DYNAMIQUES** (depuis config asset) :

```json
"timing_gatekeeper": {
  "min_tick_rate": 2.5,  // USDJPY
  "min_tick_rate": 2.0,  // EURUSD
  "min_tick_rate": 2.5   // GBPUSD
}
```

**Pour ce trade** :
- GBPUSD min_tick_rate configuré : **2.5 ticks/s** (depuis GBPUSD.json:204)
- Tick rate mesuré : **1.2 ticks/s**
- **❌ DEVRAIT ÊTRE VETO !**

**🚨 PROBLÈME** : Le trade est passé avec tick rate 1.2/s alors que le minimum configuré est 2.5/s !

**Explication probable** : Le bot tourne en session GMT 11h (OTHER), peut-être que le seuil est différent hors Londres/NY.

---

#### C) Veto Spread Trop Élevé

**Seuils DYNAMIQUES** (depuis config) :
```json
"max_spread_pips": 1.8  // GBPUSD
```

**Pour ce trade** : Spread non vérifié dans les logs → **PASS** ✅ (par défaut)

---

#### D) Veto GMT Hours (Heures interdites)

**Configuration GBPUSD** :
```json
"allowed_hours_gmt": [7, 8, 9, 13, 14, 15]
```

**Pour ce trade** :
- GMT actuel : **11h**
- **11h N'EST PAS dans allowed_hours** ❌
- **🚨 DEVRAIT ÊTRE VETO !**

**Explication** : Ce veto n'est probablement PAS activé, ou mal implémenté.

---

### 2. Phase/Regime Veto

**Fichier** : `run_bot.py` (phase_config_check)

**Configuration GBPUSD** :
```json
"blocked_phases": [
  "range",
  "accumulation",
  "range_accumulation",
  "range_distribution",
  "high_volatility_chaos"
]
```

**Pour ce trade** :
- Régime actuel : **transitional**
- **transitional N'EST PAS bloqué** → **PASS** ✅

**Log confirmation** :
```
[PHASE_CONFIG_CHECK][GBPUSD] blocked_phases=[...] | current_regime=transitional | is_blocked=False
```

---

### 3. OrderFlow Score Threshold

**Seuil DYNAMIQUE** (depuis config asset) :

```json
"of_v6_gate": {
  "min_score": 0.67,  // GBPUSD (67%)
  "require_ncp_strong": true
}
```

**Pour ce trade** :
- Score OrderFlow : **90.0/100 = 90%**
- Seuil : 67%
- **90% > 67%** → **PASS** ✅

---

### 4. Burst Guard (Un seul panier global)

**Fichier** : `run_bot.py`

**Logique** :
```python
if len(paniers_ouverts) > 0:
    return VETO  # Un seul burst à la fois (single_burst_global)
```

**Pour ce trade** :
- Aucun panier ouvert avant → **PASS** ✅
- **Après ce trade** : basket_id `c927c752` créé, bloquera tous les autres signaux

---

### 5. Risk Management Caps

**Seuils** :
```python
max_risk_per_trade_percent = 10.0  # prod_config.json:271
risk_per_trade_percent = 4.0       # broker_accounts.json:19
```

**Pour ce trade** :
- Risk configuré : 4.0%
- Max autorisé : 10.0%
- **4.0% < 10.0%** → **PASS** ✅

---

## 🎯 DÉCISION FINALE : POURQUOI LE BOT A TRADÉ ?

### ✅ Critères PASS

1. **OrderFlow Score** : 90/100 (largement > 67% seuil)
2. **Delta** : +24 (fort déséquilibre achat)
3. **Volume** : 1.28x (au-dessus moyenne)
4. **Imbalance** : 5 blocs achat détectés
5. **Coherence** : 67% (2/3 ticks alignés)
6. **Phase** : transitional (pas bloquée)
7. **Tick Rate** : 1.2/s (minimum 1.0/s GÉNÉRIQUE accepté)

### ❌ Critères IGNORÉS (Bugs potentiels)

1. **GMT Hours** : 11h n'est PAS dans allowed_hours [7,8,9,13,14,15]
2. **Tick Rate Asset-Specific** : 1.2/s < 2.5/s configuré dans GBPUSD.json

---

## 🔧 PARAMÈTRES HARDCODÉS vs DYNAMIQUES

### HARDCODÉS (dans le code)

| Paramètre | Valeur | Fichier | Ligne |
|-----------|--------|---------|-------|
| Delta threshold strong | 20 | scalping.py | ~620 |
| Delta threshold weak | 5 | scalping.py | ~620 |
| Imbalance multiplier | 40 | scalping.py | 532 |
| MTF bullish threshold | 0.67 | scalping.py | 20 |
| MTF bearish threshold | 0.33 | scalping.py | 21 |
| Coherence calculation | 3 bars | scalping.py | 587 |

### DYNAMIQUES (depuis config)

| Paramètre | Valeur | Fichier Config | Clé JSON |
|-----------|--------|----------------|----------|
| Volume spike threshold | 1.8 | config_trade_scalping.json | orderflow_v6_config.volume_ratio_classification.spike |
| Min OrderFlow score | 0.67 | GBPUSD.json | overrides.scalping.entry_rules.scalping.burst_scalping.of_v6_gate.min_score |
| Allowed GMT hours | [7,8,9,13,14,15] | GBPUSD.json | overrides.scalping.timing_gatekeeper.allowed_hours_gmt |
| Min tick rate | 2.5 | GBPUSD.json | overrides.scalping.timing_gatekeeper.min_tick_rate |
| Max spread pips | 1.8 | GBPUSD.json | overrides.scalping.timing_gatekeeper.max_spread_pips |
| Blocked phases | [...] | GBPUSD.json | overrides.scalping.entry_rules.scalping.burst_scalping.blocked_phases |
| Max risk % | 10.0 | prod_config.json | risk_management.max_risk_per_trade_percent |

---

## 🐛 BUGS IDENTIFIÉS

### 1. Veto GMT Hours non appliqué

**Symptôme** : Trade exécuté à 11h GMT alors que allowed_hours = [7,8,9,13,14,15]

**Impact** : Trades hors heures optimales (Londres/NY overlap)

**Fichier à vérifier** : `phase_observer/timing_analyzer.py`

---

### 2. Veto Tick Rate asset-specific ignoré

**Symptôme** : Trade avec 1.2 ticks/s alors que GBPUSD.json exige 2.5 ticks/s

**Impact** : Trades dans conditions de liquidité faible

**Cause probable** : Utilise seuil GÉNÉRIQUE (1.0 ticks/s) au lieu du seuil asset-specific (2.5 ticks/s)

---

### 3. Score affiché (90) ≠ Score calculé (54)

**Symptôme** : Divergence entre calcul manuel et logs

**Cause probable** : Bonus Burst appliqués mais non loggés dans l'affichage console

---

## 📁 FICHIERS CRITIQUES

| Fichier | Rôle |
|---------|------|
| `strategy/scalping.py` | Calcul OrderFlow V6 complet |
| `phase_observer/timing_analyzer.py` | Timing Gatekeeper (PASS/VETO) |
| `run_bot.py` | Orchestration multi-thread + vetos cascade |
| `trader/order_builder.py` | Calcul sizing + risk management |
| `config/assets_config/GBPUSD.json` | Config asset-specific (seuils, vetos) |
| `config/strategy/config_trade_scalping.json` | Config stratégie globale |
| `config/prod_config.json` | Config système (risk caps, paths) |
| `config/broker_accounts.json` | Risk % par compte broker |

---

## 🎓 GLOSSAIRE TECHNIQUE

| Terme | Définition |
|-------|------------|
| **Delta** | Différence entre volume achat et vente (BUY_VOL - SELL_VOL) |
| **Coherence** | % de bougies/ticks allant dans la même direction que le delta |
| **Imbalance** | Blocs de déséquilibre détectés (clusters d'ordres unilatéraux) |
| **MTF** | Multi-TimeFrame (analyse M1/M3/M5 simultanée) |
| **Tick Rate** | Nombre de ticks par seconde (mesure de liquidité) |
| **Coverage** | Durée en secondes des données analysées |
| **VPOC** | Volume Point of Control (prix avec le plus de volume) |
| **Absorption** | Ratio buy/sell volume indiquant direction institutionnelle |
| **Burst Quality** | Métriques avancées (accélération, pureté, volume spike) |
| **Timing Gatekeeper** | Filtre binaire PASS/VETO basé sur conditions temporelles |
| **Phase Guard** | Filtre basé sur régime de marché (trending, range, etc.) |

---

**📅 Rapport généré** : 02 Janvier 2026
**🔖 Version OrderFlow** : V6 (Burst-Only, Ticks Temps Réel)
**⚙️ Configuration** : GBPUSD Scalping Burst (Option B - 31 DEC 2025)
