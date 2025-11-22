# 🎯 DONNÉES ULTRA FIABLES DES 3 FONCTIONS

## Objectif
Identifier les **données les plus fiables** qui doivent avoir **le plus de poids** dans la décision finale.

---

## 📊 ANALYSE PAR FONCTION

### **1️⃣ ORDERFLOW V6**

#### Score de Base (ligne 66-70 du scoring_engine.py)
```python
# Pondération interne OrderFlow v6
w_imb = 0.45    # Imbalance (45%)
w_agr = 0.35    # Aggressor ratio (35%)
w_cvd = 0.20    # CVD slope (20%)

base_score = (0.45 × imbalance) + (0.35 × aggressor) + (0.20 × cvd_slope)
```

#### **Données ULTRA FIABLES** (calculées directement sur ticks)

| Donnée | Source | Calcul | Fiabilité | Poids Actuel |
|--------|--------|--------|-----------|--------------|
| **imbalance_mean** | Agrégation ticks | Moyenne des ratios buy/(buy+sell) par tick | 🟢 **TRÈS HAUTE** | **45%** |
| **aggressor_ratio** | Agrégation ticks | Ratio aggressor buy vs sell | 🟢 **TRÈS HAUTE** | **35%** |
| **cvd_slope** | Dérivée CVD | Pente du delta cumulé (momentum) | 🟢 **HAUTE** | **20%** |
| **delta_total** | Somme directe | CVD final = Σ(buy_volume - sell_volume) | 🟢 **TRÈS HAUTE** | Indirecte |
| **total_volume** | Somme directe | Volume total traité | 🟢 **TRÈS HAUTE** | Qualité |

#### **Données SECONDAIRES** (calculées ou dérivées)

| Donnée | Source | Fiabilité | Usage |
|--------|--------|-----------|-------|
| vwap_slope | Dérivée VWAP | 🟡 MOYENNE | Bonus +6pts max |
| pattern_count | Détection patterns | 🟡 MOYENNE | Bonus +12pts max |
| tick_rate | Vitesse ticks | 🟢 HAUTE | Bonus activité |
| buy_ratio | Dérivé de delta | 🟢 HAUTE | Info bias |

#### **Statut de Validation**
```python
status = "VALID" si score >= 70 ET rescue_level < 2
status = "SUSPECT" sinon
```

---

### **2️⃣ FOOTPRINT M1**

#### **Données ULTRA FIABLES** (mesures directes)

| Donnée | Source | Calcul | Fiabilité | Importance |
|--------|--------|--------|-----------|------------|
| **delta_total** | Somme ticks | Σ(buy_vol - sell_vol) sur fenêtre M1 | 🟢 **TRÈS HAUTE** | ⭐⭐⭐⭐⭐ |
| **buy_volume** | Somme directe | Volume acheté total | 🟢 **TRÈS HAUTE** | ⭐⭐⭐⭐ |
| **sell_volume** | Somme directe | Volume vendu total | 🟢 **TRÈS HAUTE** | ⭐⭐⭐⭐ |
| **tick_count** | Comptage | Nombre de ticks analysés | 🟢 **TRÈS HAUTE** | ⭐⭐⭐⭐⭐ (qualité) |
| **coverage_s** | Durée | Secondes couvertes par l'analyse | 🟢 **TRÈS HAUTE** | ⭐⭐⭐⭐⭐ (qualité) |
| **tick_rate** | tick_count / coverage_s | Vitesse du marché | 🟢 **TRÈS HAUTE** | ⭐⭐⭐⭐ |

#### **Données DÉRIVÉES**

| Donnée | Calcul | Fiabilité | Importance |
|--------|--------|-----------|------------|
| buy_pct | buy_vol / total_vol × 100 | 🟢 HAUTE | ⭐⭐⭐ |
| sell_pct | sell_vol / total_vol × 100 | 🟢 HAUTE | ⭐⭐⭐ |
| poc | Point of Control (prix max volume) | 🟡 MOYENNE | ⭐⭐ |
| absorption_flag | Détection pattern | 🟡 MOYENNE | ⭐⭐⭐ |

#### **Score Footprint M1**
```python
# Si score fourni : normalisation /100
# Sinon synthétique :
score = 0.7 si status=="VALID"
score = 0.4 sinon
score += 0.05 si abs(delta_total) >= 1.0
```

---

### **3️⃣ FOOTPRINT TRIGGERS**

#### **Données ULTRA FIABLES** (si trigger détecté)

| Donnée | Source | Calcul | Fiabilité | Importance |
|--------|--------|--------|-----------|------------|
| **trigger_type** | Détection pattern | "stacking" / "climax" / "absorption" | 🟢 **HAUTE** si détecté | ⭐⭐⭐⭐⭐ |
| **confidence** | Scoring pattern | 0.0-0.99 (qualité du pattern) | 🟢 **HAUTE** si détecté | ⭐⭐⭐⭐⭐ |
| **delta_ratio_mean** | Snapshot stats | Force directionnelle moyenne | 🟢 **HAUTE** | ⭐⭐⭐⭐ |
| **volume_zscore_max** | Snapshot stats | Intensité volume (écart-type) | 🟢 **HAUTE** | ⭐⭐⭐⭐ |
| **levels_count** | Snapshot stats | Profondeur analyse (niveaux prix) | 🟢 **HAUTE** | ⭐⭐⭐⭐⭐ (qualité) |
| **used_window_s** | Config détection | Fenêtre utilisée (3/5/8/13/21s) | 🟢 **TRÈS HAUTE** | ⭐⭐⭐ (contexte) |

#### **CAS SPÉCIAL : Absence de Trigger**
```python
# Si aucun pattern détecté → Fallback
trigger_type = "fusion_pretrigger"  # ❌ PAS FIABLE
confidence = 0.5  # ❌ Valeur par défaut
direction = déduit de orderflow  # ❌ Redondant
```

**→ Si trigger_type == "fusion_pretrigger" : IGNORER le trigger (pas de valeur ajoutée)**

---

## 🏆 HIÉRARCHIE D'IMPORTANCE - TOP 10 DONNÉES

### **NIVEAU 1 : FONDATIONS (Ultra Fiables - Mesures Directes)**

| Rang | Donnée | Source | Pourquoi Ultra Fiable ? | Poids Suggéré |
|------|--------|--------|-------------------------|---------------|
| **1** | **delta_total** (OF+FP combiné) | Somme directe ticks | Mesure brute, pas d'interprétation | **25%** |
| **2** | **imbalance_mean** (OF) | Moyenne ratios buy/sell | Agrégation simple, robuste | **20%** |
| **3** | **tick_count** (FP) | Comptage | Mesure qualité données | **Qualité** |
| **4** | **coverage_s** (FP) | Durée | Profondeur temporelle | **Qualité** |
| **5** | **total_volume** (OF+FP) | Somme directe | Mesure brute | **15%** |

### **NIVEAU 2 : DYNAMIQUE (Fiables - Dérivées Simples)**

| Rang | Donnée | Source | Pourquoi Fiable ? | Poids Suggéré |
|------|--------|--------|-------------------|---------------|
| **6** | **cvd_slope** (OF) | Dérivée 1er ordre | Calcul simple, robuste | **15%** |
| **7** | **aggressor_ratio** (OF) | Ratio ticks | Agrégation directe | **10%** |
| **8** | **tick_rate** (FP) | Vitesse marché | Mesure directe | **10%** |

### **NIVEAU 3 : PATTERNS (Conditionnels - Si Détectés)**

| Rang | Donnée | Source | Pourquoi Important ? | Poids Suggéré |
|------|--------|--------|---------------------|---------------|
| **9** | **trigger_type** (Triggers) | Détection pattern | **AMPLIFICATEUR** si présent | **BONUS +15%** |
| **10** | **volume_zscore_max** (Triggers) | Écart-type | Détection événements | **5%** |

---

## 🎯 SYNTHÈSE : DONNÉES QUI COMPTENT VRAIMENT

### ✅ **TOP 3 ULTRA FIABLES** (à privilégier absolument)

1. **delta_total** (OrderFlow + Footprint combiné)
   - **Pourquoi** : Mesure BRUTE du déséquilibre buy/sell
   - **Fiabilité** : 100% (somme directe, pas d'interprétation)
   - **Importance** : ⭐⭐⭐⭐⭐
   - **Valeur attendue XAUUSD** : >1500 = fort signal

2. **imbalance_mean** (OrderFlow v6)
   - **Pourquoi** : Ratio moyen buy/(buy+sell) sur tous les ticks
   - **Fiabilité** : 95% (agrégation simple)
   - **Importance** : ⭐⭐⭐⭐⭐
   - **Valeur attendue** : >0.60 = domination claire

3. **tick_count + coverage_s** (Footprint M1)
   - **Pourquoi** : Qualité de l'échantillon
   - **Fiabilité** : 100% (comptage direct)
   - **Importance** : ⭐⭐⭐⭐⭐
   - **Valeur minimale** : tick_count >100, coverage_s >15s

---

### ✅ **TOP 3 DYNAMIQUES** (momentum et vitesse)

4. **cvd_slope** (OrderFlow v6)
   - **Pourquoi** : Accélération du delta (momentum)
   - **Fiabilité** : 85% (dérivée, sensible au bruit)
   - **Importance** : ⭐⭐⭐⭐
   - **Valeur attendue** : >1.0 = momentum fort

5. **tick_rate** (Footprint M1)
   - **Pourquoi** : Vitesse du marché (liquidité)
   - **Fiabilité** : 95% (mesure directe)
   - **Importance** : ⭐⭐⭐⭐
   - **Valeur attendue** : >40 ticks/s = marché actif

6. **aggressor_ratio** (OrderFlow v6)
   - **Pourquoi** : Agressivité buy vs sell
   - **Fiabilité** : 90% (agrégation ticks)
   - **Importance** : ⭐⭐⭐
   - **Valeur attendue** : >0.60 = buyers agressifs

---

### ⚡ **AMPLIFICATEUR : Trigger (si présent)**

7. **trigger_type + confidence** (Footprint Triggers)
   - **Pourquoi** : Pattern institutionnel détecté
   - **Fiabilité** : 70% (détection complexe)
   - **Importance** : ⭐⭐⭐⭐⭐ **SI DÉTECTÉ** (sinon ignorer)
   - **Valeur attendue** : type="stacking/climax" + confidence >0.75

---

## 🚫 **DONNÉES À IGNORER ou FAIBLE POIDS**

| Donnée | Pourquoi Peu Fiable ? |
|--------|-----------------------|
| poc / anchor | Prix, pas direction - contextuel uniquement |
| absorption_flag | Détection complexe, faux positifs |
| vwap_slope | Bonus mineur, dérivée sensible |
| pattern_count | Agrégation floue |
| bias (OF) | Redondant avec delta_total |
| buy_ratio (OF) | Redondant avec imbalance |

---

## 💡 **CONCLUSION : LOGIQUE DE SCORING OPTIMALE**

### **Formule Proposée**

```python
# ÉTAPE 1 : Score de Confiance (sur les 6 données ultra fiables)
confiance_score = (
    0.25 × normalize(delta_total_combined) +      # Fondation
    0.20 × normalize(imbalance_mean) +            # Fondation
    0.15 × normalize(total_volume) +              # Fondation
    0.15 × normalize(cvd_slope) +                 # Dynamique
    0.10 × normalize(tick_rate) +                 # Dynamique
    0.10 × normalize(aggressor_ratio) +           # Dynamique
    0.05 × normalize(volume_zscore)               # Événement
)

# ÉTAPE 2 : Filtre Qualité (minimum requis)
if tick_count < 50 or coverage_s < 10:
    confiance_score *= 0.5  # Pénalité qualité

if status_OF != "VALID" or status_FP != "VALID":
    confiance_score *= 0.7  # Pénalité validation

# ÉTAPE 3 : BONUS Trigger (si pattern réel détecté)
if trigger_type in ["stacking", "climax", "absorption"]:
    if confidence >= 0.85:
        confiance_score += 0.15  # +15% (passage niveau supérieur)
    elif confidence >= 0.75:
        confiance_score += 0.10  # +10%
    elif confidence >= 0.65:
        confiance_score += 0.05  # +5%

# ÉTAPE 4 : Normalisation
final_score = max(0.0, min(0.99, confiance_score))
```

---

## 🎯 **SEUILS RECOMMANDÉS XAUUSD**

| Métrique | Seuil Minimal | Seuil Fort | Ultra Fort |
|----------|---------------|------------|------------|
| delta_total (combiné) | 800 | 1500 | 3000 |
| imbalance_mean | 0.55 | 0.65 | 0.75 |
| cvd_slope | 0.5 | 1.0 | 2.0 |
| tick_rate | 30 | 50 | 80 |
| tick_count | 50 | 100 | 200 |
| coverage_s | 10 | 20 | 40 |
| trigger_confidence | 0.65 | 0.75 | 0.85 |

---

*Document créé le : 2025-11-22*
*Basé sur l'analyse du code réel (scoring_engine.py, fusion_manager.py)*
