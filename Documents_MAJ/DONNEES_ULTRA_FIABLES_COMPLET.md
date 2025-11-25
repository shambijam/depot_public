# 🎯 DONNÉES ULTRA FIABLES - VERSION COMPLÈTE

## ⚠️ CORRECTION IMPORTANTE

Vous avez raison ! J'avais manqué des **données primordiales** du Footprint M1 :
- **buy_volume / sell_volume** (volumes acheteur/vendeur)
- **buy_ticks / sell_ticks** (nombre de ticks acheteur/vendeur)

Ces données sont **FONDAMENTALES** car elles représentent la **pression réelle** du marché.

---

## 📊 LISTE COMPLÈTE DES DONNÉES REÇUES

### **1️⃣ ORDERFLOW V6**

#### **Données BRUTES (Mesures Directes)**

| Donnée | Type | Source | Fiabilité | Importance |
|--------|------|--------|-----------|------------|
| **ask_volume** (buy_volume) | float | Somme ticks buy | 🟢 **TRÈS HAUTE** | ⭐⭐⭐⭐⭐ |
| **bid_volume** (sell_volume) | float | Somme ticks sell | 🟢 **TRÈS HAUTE** | ⭐⭐⭐⭐⭐ |
| **delta_total** | float | ask_volume - bid_volume | 🟢 **TRÈS HAUTE** | ⭐⭐⭐⭐⭐ |
| **total_volume** | float | ask_volume + bid_volume | 🟢 **TRÈS HAUTE** | ⭐⭐⭐⭐ |
| **buy_ticks** (nombre) | int | Comptage ticks buy | 🟢 **TRÈS HAUTE** | ⭐⭐⭐⭐⭐ |
| **sell_ticks** (nombre) | int | Comptage ticks sell | 🟢 **TRÈS HAUTE** | ⭐⭐⭐⭐⭐ |

#### **Données CALCULÉES (Ratios et Dérivées)**

| Donnée | Calcul | Fiabilité | Importance | Poids Actuel OF |
|--------|--------|-----------|------------|-----------------|
| **imbalance_mean** | Moyenne(buy/(buy+sell)) par tick | 🟢 **TRÈS HAUTE** | ⭐⭐⭐⭐⭐ | **45%** |
| **aggressor_ratio** | Ratio aggressor buy vs sell | 🟢 **HAUTE** | ⭐⭐⭐⭐ | **35%** |
| **cvd_slope** | Pente du delta cumulé | 🟢 **HAUTE** | ⭐⭐⭐⭐ | **20%** |
| **buy_ratio** | buy_volume / total_volume | 🟢 **HAUTE** | ⭐⭐⭐⭐ | Dérivée |

#### **Données SECONDAIRES**

| Donnée | Type | Fiabilité | Usage |
|--------|------|-----------|-------|
| tick_rate | ticks/seconde | 🟢 HAUTE | Activité marché |
| pattern_count | Détection patterns | 🟡 MOYENNE | Bonus +12pts |
| vwap_slope | Dérivée VWAP | 🟡 MOYENNE | Bonus +6pts |

---

### **2️⃣ FOOTPRINT M1**

#### **Données PRIMORDIALES (Mesures Directes)**

| Donnée | Type | Source | Fiabilité | Importance |
|--------|------|--------|-----------|------------|
| **buy_volume** | float | Σ volumes buy | 🟢 **TRÈS HAUTE** | ⭐⭐⭐⭐⭐ |
| **sell_volume** | float | Σ volumes sell | 🟢 **TRÈS HAUTE** | ⭐⭐⭐⭐⭐ |
| **delta_total** | float | buy_vol - sell_vol | 🟢 **TRÈS HAUTE** | ⭐⭐⭐⭐⭐ |
| **tick_count** | int | Nombre total de ticks | 🟢 **TRÈS HAUTE** | ⭐⭐⭐⭐⭐ (qualité) |
| **coverage_s** | float | Durée couverte (secondes) | 🟢 **TRÈS HAUTE** | ⭐⭐⭐⭐⭐ (qualité) |
| **tick_rate** | float | tick_count / coverage_s | 🟢 **TRÈS HAUTE** | ⭐⭐⭐⭐ |

#### **Données DÉRIVÉES**

| Donnée | Calcul | Fiabilité | Importance |
|--------|--------|-----------|------------|
| buy_pct | (buy_vol / total_vol) × 100 | 🟢 HAUTE | ⭐⭐⭐⭐ |
| sell_pct | (sell_vol / total_vol) × 100 | 🟢 HAUTE | ⭐⭐⭐⭐ |
| total_volume | buy_vol + sell_vol | 🟢 TRÈS HAUTE | ⭐⭐⭐⭐ |
| poc | Point of Control | 🟡 MOYENNE | ⭐⭐ |
| absorption_flag | Détection pattern | 🟡 MOYENNE | ⭐⭐⭐ |

---

### **3️⃣ FOOTPRINT TRIGGERS**

#### **Données ULTRA FIABLES (Si Pattern Détecté)**

| Donnée | Source | Fiabilité | Importance |
|--------|--------|-----------|------------|
| **trigger_type** | "stacking" / "climax" / "absorption" | 🟢 **HAUTE** si détecté | ⭐⭐⭐⭐⭐ |
| **confidence** | 0.0-0.99 | 🟢 **HAUTE** si détecté | ⭐⭐⭐⭐⭐ |
| **delta_ratio_mean** | Force directionnelle snapshot | 🟢 **HAUTE** | ⭐⭐⭐⭐ |
| **volume_zscore_max** | Intensité volume (écart-type) | 🟢 **HAUTE** | ⭐⭐⭐⭐ |
| **levels_count** | Niveaux prix analysés | 🟢 **HAUTE** | ⭐⭐⭐⭐ (qualité) |
| **used_window_s** | Fenêtre (3/5/8/13/21s) | 🟢 **TRÈS HAUTE** | ⭐⭐⭐ |

---

## 🏆 HIÉRARCHIE CORRIGÉE - TOP 12 DONNÉES

### **NIVEAU 1 : PRESSION MARCHÉ (Ultra Primordial)**

| Rang | Donnée | Sources | Pourquoi Ultra Fiable ? | Poids Suggéré |
|------|--------|---------|-------------------------|---------------|
| **1** | **buy_volume** (OF + FP) | Somme directe ticks buy | Mesure BRUTE pression acheteuse | **15%** |
| **2** | **sell_volume** (OF + FP) | Somme directe ticks sell | Mesure BRUTE pression vendeuse | **15%** |
| **3** | **delta_total** (OF + FP combiné) | buy_vol - sell_vol | Différentiel NET de pression | **15%** |

**→ TOTAL 45% sur la PRESSION RÉELLE du marché**

---

### **NIVEAU 2 : RATIOS DIRECTIONNELS (Très Fiables)**

| Rang | Donnée | Source | Pourquoi Fiable ? | Poids Suggéré |
|------|--------|--------|-------------------|---------------|
| **4** | **buy_pct** (FP) | (buy_vol / total_vol) × 100 | Domination buy en % | **10%** |
| **5** | **sell_pct** (FP) | (sell_vol / total_vol) × 100 | Domination sell en % | **10%** |
| **6** | **imbalance_mean** (OF) | Moyenne buy/(buy+sell) par tick | Agrégation robuste | **10%** |

**→ TOTAL 30% sur les RATIOS de domination**

---

### **NIVEAU 3 : DYNAMIQUE ET QUALITÉ (Fiables)**

| Rang | Donnée | Source | Pourquoi Important ? | Poids Suggéré |
|------|--------|--------|---------------------|---------------|
| **7** | **cvd_slope** (OF) | Pente delta cumulé | Momentum | **10%** |
| **8** | **tick_rate** (FP) | Vitesse marché | Activité/Liquidité | **5%** |
| **9** | **tick_count** (FP) | Profondeur échantillon | QUALITÉ données | **Filtre** |
| **10** | **coverage_s** (FP) | Durée analyse | QUALITÉ données | **Filtre** |

**→ TOTAL 15% sur DYNAMIQUE + Filtres QUALITÉ**

---

### **NIVEAU 4 : AMPLIFICATEUR (Si Présent)**

| Rang | Donnée | Source | Pourquoi Important ? | Poids |
|------|--------|--------|---------------------|-------|
| **11** | **trigger_type** (Triggers) | Pattern détecté | Validation institutionnelle | **BONUS +10%** |
| **12** | **volume_zscore_max** (Triggers) | Intensité | Événement exceptionnel | **BONUS +5%** |

**→ TOTAL +15% BONUS si trigger présent**

---

## 📊 REDONDANCES ET COHÉRENCES

### **Delta Total (3 sources)**

```python
# OrderFlow v6
of_delta_total = Σ(ask_volume - bid_volume)

# Footprint M1
fp_delta_total = Σ(buy_volume - sell_volume)

# Combiné (meilleure fiabilité)
delta_combined = of_delta_total + fp_delta_total
```

**→ Si les 2 deltas sont ALIGNÉS (même signe) : HAUTE CONFIANCE**

---

### **Buy/Sell Volumes (2 sources)**

```python
# OrderFlow v6
of_buy_volume = Σ(ask_volume)
of_sell_volume = Σ(bid_volume)

# Footprint M1
fp_buy_volume = Σ(buy_volume)
fp_sell_volume = Σ(sell_volume)
```

**→ Si OF et FP montrent même domination : TRÈS HAUTE CONFIANCE**

---

### **Imbalance (2 méthodes)**

```python
# Méthode 1 : Imbalance Mean (OF v6)
imbalance_mean = Moyenne(buy/(buy+sell)) par tick

# Méthode 2 : Buy Percentage (FP M1)
buy_pct = (buy_volume / total_volume) × 100

# Cohérence
if (imbalance_mean - 0.5) × 2 ≈ (buy_pct - 50) / 50:
    # Les 2 méthodes sont cohérentes
```

---

## 🎯 FORMULE DE SCORING OPTIMALE CORRIGÉE

### **ÉTAPE 1 : Score Pression Marché (45%)**

```python
# Normalisation volumes
buy_vol_combined = of_buy_volume + fp_buy_volume
sell_vol_combined = of_sell_volume + fp_sell_volume
total_vol_combined = buy_vol_combined + sell_vol_combined

# Ratios
buy_dominance = buy_vol_combined / total_vol_combined  # 0-1
sell_dominance = sell_vol_combined / total_vol_combined  # 0-1

# Delta normalisé
delta_combined = of_delta_total + fp_delta_total
delta_normalized = np.tanh(delta_combined / 2000)  # -1 à +1 (seuil 2000 pour XAUUSD)

# Score pression
if delta_normalized > 0:  # Pression BUY
    pression_score = (
        0.15 × buy_dominance +           # Domination buy
        0.15 × (1 - sell_dominance) +    # Faiblesse sell
        0.15 × abs(delta_normalized)     # Force delta
    )
else:  # Pression SELL
    pression_score = (
        0.15 × sell_dominance +
        0.15 × (1 - buy_dominance) +
        0.15 × abs(delta_normalized)
    )
```

---

### **ÉTAPE 2 : Score Ratios (30%)**

```python
# Buy/Sell percentages (FP)
buy_pct_norm = buy_pct / 100  # 0-1
sell_pct_norm = sell_pct / 100  # 0-1

# Imbalance (OF)
imbalance_deviation = abs(imbalance_mean - 0.5)  # 0-0.5

# Score ratios
ratios_score = (
    0.10 × imbalance_deviation * 2 +     # OF imbalance (0-1)
    0.10 × abs(buy_pct_norm - 0.5) * 2 + # FP buy% (0-1)
    0.10 × abs(sell_pct_norm - 0.5) * 2  # FP sell% (0-1)
)
```

---

### **ÉTAPE 3 : Score Dynamique (15%)**

```python
# CVD Slope (momentum)
cvd_norm = np.tanh(cvd_slope / 2.0)  # -1 à +1

# Tick Rate (activité)
tick_rate_norm = min(1.0, tick_rate / 80)  # 0-1 (80 ticks/s = max)

# Score dynamique
dynamique_score = (
    0.10 × abs(cvd_norm) +      # Momentum
    0.05 × tick_rate_norm       # Activité
)
```

---

### **ÉTAPE 4 : Filtre Qualité (Obligatoire)**

```python
qualite_multiplier = 1.0

# Tick count minimum
if tick_count < 50:
    qualite_multiplier *= 0.3  # Pénalité sévère
elif tick_count < 100:
    qualite_multiplier *= 0.7

# Coverage minimum
if coverage_s < 10:
    qualite_multiplier *= 0.4
elif coverage_s < 20:
    qualite_multiplier *= 0.8

# Status validation
if status_OF != "VALID":
    qualite_multiplier *= 0.7
if status_FP != "VALID":
    qualite_multiplier *= 0.7
```

---

### **ÉTAPE 5 : Score de Base**

```python
base_score = (pression_score + ratios_score + dynamique_score) × qualite_multiplier
# base_score : 0.0 - 0.90
```

---

### **ÉTAPE 6 : BONUS Trigger (Amplificateur)**

```python
trigger_boost = 0.0

if trigger_type in ["stacking", "climax", "absorption"]:
    # Trigger réel détecté
    if confidence >= 0.85:
        trigger_boost = 0.15  # +15% (niveau DIAMANT)
    elif confidence >= 0.75:
        trigger_boost = 0.12  # +12% (niveau PLATINE)
    elif confidence >= 0.65:
        trigger_boost = 0.08  # +8% (niveau OR)

    # Bonus volume exceptionnel
    if volume_zscore_max >= 2.5:
        trigger_boost += 0.03  # +3% supplémentaire

# Application
final_score = base_score + trigger_boost
final_score = max(0.0, min(0.99, final_score))
```

---

## 🎯 GRILLE DE DÉCISION FINALE

| Score Final | Niveau | Composition Typique |
|-------------|--------|---------------------|
| **90-99%** | 💎 DIAMANT | Base 75%+ **+ Trigger excellent (0.85+)** |
| **80-89%** | 🔷 PLATINE | Base 70%+ **+ Trigger bon (0.75+)** OU Base 85%+ sans trigger |
| **70-79%** | 🟡 OR | Base 60%+ **+ Trigger faible (0.65+)** OU Base 75%+ sans trigger |
| **55-69%** | 🔘 ARGENT | Base 55%+ sans trigger |
| **<55%** | ⚪ REJETÉ | Base faible ou qualité insuffisante |

---

## ✅ VALIDATION - CAS CONCRETS

### **CAS 1 : Forte Pression Buy SANS Trigger**

```python
DONNÉES :
  buy_volume (OF+FP) : 12500 + 8300 = 20800
  sell_volume (OF+FP) : 6200 + 4100 = 10300
  delta_total (OF+FP) : 1800 + 1200 = 3000
  buy_pct : 67%
  imbalance_mean : 0.68
  cvd_slope : 1.8
  tick_rate : 72
  tick_count : 180 | coverage_s : 25s | status : VALID/VALID
  Trigger : AUCUN

CALCUL :
  buy_dominance = 20800/31100 = 0.669
  delta_norm = tanh(3000/2000) = 0.905

  pression_score = 0.15×0.669 + 0.15×0.331 + 0.15×0.905 = 0.291
  ratios_score = 0.10×0.36 + 0.10×0.34 + 0.10×0.34 = 0.104
  dynamique_score = 0.10×0.893 + 0.05×0.900 = 0.134

  base = (0.291 + 0.104 + 0.134) × 1.0 = 0.529

  trigger_boost = 0.0

  final = 0.529 + 0.0 = 52.9%
```

**Résultat** : **REJETÉ** (trop juste, manque trigger pour valider)

**→ PROBLÈME : Formule trop stricte !**

---

### **AJUSTEMENT FORMULE**

Le problème c'est que 45% + 30% + 15% = **90% seulement**, ce qui rend difficile d'atteindre 70% sans trigger.

**Solution** : Augmenter les pondérations :

```python
pression_score = (
    0.20 × buy_dominance +        # 20% au lieu de 15%
    0.20 × (1 - sell_dominance) +
    0.20 × abs(delta_normalized)
)

ratios_score = (
    0.15 × imbalance_deviation * 2 +  # 15% au lieu de 10%
    0.15 × abs(buy_pct_norm - 0.5) * 2 +
    0.0  # Suppression redondant
)

# NOUVEAU TOTAL : 60% pression + 30% ratios + 10% dynamique = 100%
```

---

## 📌 CONCLUSION

### **TOP 3 DONNÉES PRIMORDIALES (selon vous)**

1. **buy_volume / sell_volume** (OF + FP)
   - **Pression réelle** du marché
   - **Fiabilité** : 100% (somme directe)
   - **Poids** : **40%** (20% + 20%)

2. **delta_total** (OF + FP combiné)
   - **Force nette** du déséquilibre
   - **Fiabilité** : 100% (différence directe)
   - **Poids** : **20%**

3. **imbalance_mean / buy_pct** (OF + FP)
   - **Domination** en pourcentage
   - **Fiabilité** : 95% (agrégation simple)
   - **Poids** : **30%**

**→ Total 90% sur les 3 données primordiales, +10% dynamique**

---

*Document mis à jour le : 2025-11-22*
*Correction : Ajout buy_volume/sell_volume comme données primordiales*
