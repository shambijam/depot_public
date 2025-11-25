# 📊 ANALYSE COMPLÈTE DES DONNÉES DES 3 FONCTIONS

## 🎯 Objectif
Établir une **synthèse de qualité** des données reçues par le FusionManager pour créer un système de scoring optimal.

---

## 📥 DONNÉES REÇUES PAR LE FUSIONMANAGER

### **1. OrderFlow v6**

#### Structure Brute (depuis run_bot.py ligne 900-905)
```python
orderflow = {
    "score": 0-100,                      # Score brut OrderFlow v6
    "status": "VALID" | "SUSPECT",       # Qualité des données
    "bias": "BUY" | "SELL" | "NEUTRAL",  # Direction déduite du delta
    "summary": {
        # Métriques OrderFlow v6
        "delta_total": float,            # Delta cumulé (CVD final)
        "imbalance": float,              # Déséquilibre buy/sell (0-1)
        "cvd_slope": float,              # Pente du delta cumulé (momentum)
        "vpoc_price": float,             # Volume POC price
        "vah": float,                    # Value Area High (70%)
        "val": float,                    # Value Area Low (30%)
        "bias": "BUY" | "SELL" | "NEUTRAL",
        "hvn_levels": [],                # High Volume Nodes (zones haute densité)
        "lvn_levels": [],                # Low Volume Nodes (zones basse densité)
        "absorption_flag": bool,         # Détection absorption
    }
}
```

#### Normalisation par FusionManager (ligne 670-715)
```python
normalized_orderflow = {
    "score": 0.0-1.0,              # /100 + pénalité si SUSPECT (*0.6)
    "status": "VALID/SUSPECT",
    "dir": +1 | -1 | 0,           # Déduit du bias ou delta_total
    "delta_total": float,
    "poc": float,                  # vpoc_price
    "absorption": bool,
    "raw": {...}                   # Données brutes complètes
}
```

#### Métriques Disponibles pour Scoring
| Métrique | Source | Type | Plage | Signification |
|----------|--------|------|-------|---------------|
| **score** | of.score | 0-1 | 0.0-1.0 | Score global OrderFlow v6 |
| **delta_total** | summary.delta_total | float | -∞ à +∞ | Force directionnelle cumulée |
| **imbalance** | summary.imbalance | float | 0.0-1.0 | Déséquilibre buy/sell |
| **cvd_slope** | summary.cvd_slope | float | -∞ à +∞ | Momentum (pente CVD) |
| **hvn_count** | len(hvn_levels) | int | 0-N | Zones de support/résistance forte |
| **lvn_count** | len(lvn_levels) | int | 0-N | Zones de faible liquidité |
| **absorption** | absorption_flag | bool | True/False | Absorption détectée |
| **status** | status | str | VALID/SUSPECT | Qualité données |

---

### **2. Footprint M1**

#### Structure Brute (depuis run_bot.py ligne 907-911)
```python
footprint = {
    "score": 0-100,                      # Score brut Footprint M1
    "status": "VALID" | "SUSPECT",       # Qualité des données
    "summary": {
        # Métriques Footprint M1
        "delta_total": float,            # Delta cumulé
        "tick_count": int,               # Nombre de ticks analysés
        "coverage_s": float,             # Durée couverte (secondes)
        "tick_rate": float,              # Ticks/seconde (vitesse volume)
        "buy_volume": float,             # Volume buy
        "sell_volume": float,            # Volume sell
        "total_volume": float,           # Volume total
        "poc": float,                    # Point of Control price
        "absorption_flag": bool,         # Absorption détectée
    }
}
```

#### Normalisation par FusionManager (ligne 717-760)
```python
normalized_footprint = {
    "score": 0.0-1.0,              # /100 OU score synthétique (0.7 si VALID)
    "status": "VALID/SUSPECT",
    "dir": +1 | -1 | 0,           # Déduit du delta_total
    "delta_total": float,
    "poc": float,
    "absorption": bool,
    "raw": {...}                   # Données brutes complètes
}
```

#### Métriques Disponibles pour Scoring
| Métrique | Source | Type | Plage | Signification |
|----------|--------|------|-------|---------------|
| **score** | fp.score | 0-1 | 0.0-1.0 | Score global Footprint M1 |
| **delta_total** | summary.delta_total | float | -∞ à +∞ | Force directionnelle |
| **tick_count** | summary.tick_count | int | 0-N | Profondeur de l'analyse |
| **coverage_s** | summary.coverage_s | float | 0-N | Durée couverte |
| **tick_rate** | summary.tick_rate | float | 0-N | Vitesse volume (ticks/s) |
| **buy_volume** | summary.buy_volume | float | 0-N | Volume acheté |
| **sell_volume** | summary.sell_volume | float | 0-N | Volume vendu |
| **total_volume** | summary.total_volume | float | 0-N | Volume total |
| **buy_pct** | buy_vol/total | float | 0-100% | Domination buy |
| **sell_pct** | sell_vol/total | float | 0-100% | Domination sell |
| **absorption** | absorption_flag | bool | True/False | Absorption détectée |
| **status** | status | str | VALID/SUSPECT | Qualité données |

---

### **3. Footprint Triggers**

#### Structure Brute (depuis run_bot.py ligne 914-937)
```python
# CAS 1 : Vrai trigger détecté (climax/stacking/absorption)
triggers = {
    "direction": "BUY" | "SELL" | None,
    "confidence": 0.0-1.0,
    "anchor_price": float,
    "trigger_type": "stacking" | "absorption" | "climax" | "micro_stack" | "micro_absorption",
    "action": "BUY" | "SELL" | "HOLD",
    "meta": {
        "used_window_s": 3 | 5 | 8 | 13 | 21,  # Fenêtre détection
        "snapshot_stats": {
            "levels_count": int,         # Niveaux prix analysés
            "delta_ratio_mean": float,   # Force directionnelle moyenne
            "volume_zscore_max": float,  # Intensité volume (écart-type)
        }
    }
}

# CAS 2 : Pas de trigger (fallback orderflow)
triggers = {
    "direction": "BUY" | "SELL" | None,
    "confidence": 0.5,                   # Fallback depuis signals
    "anchor_price": ask | bid,
    "trigger_type": "fusion_pretrigger", # Mode dégradé
}
```

#### Normalisation par FusionManager (ligne 762-776)
```python
normalized_trigger = {
    "score": 0.0-0.99,             # = confidence
    "dir": +1 | -1 | 0,           # Déduit de direction ou action
    "anchor": float,               # anchor_price
    "type": str,                   # trigger_type
    "ts": float | None,            # timestamp (si fourni)
    "raw": {...}                   # Données brutes complètes
}
```

#### Métriques Disponibles pour Scoring
| Métrique | Source | Type | Plage | Signification |
|----------|--------|------|-------|---------------|
| **score** | confidence | 0-1 | 0.0-0.99 | Confiance du trigger |
| **type** | trigger_type | str | voir ci-dessus | Type de pattern détecté |
| **window_s** | meta.used_window_s | int | 3/5/8/13/21 | Fenêtre de détection |
| **levels_count** | snapshot_stats.levels_count | int | 0-N | Profondeur analyse |
| **delta_ratio** | snapshot_stats.delta_ratio_mean | float | 0-1 | Force directionnelle |
| **volume_zscore** | snapshot_stats.volume_zscore_max | float | 0-N | Intensité volume |
| **is_real_trigger** | type != "fusion_pretrigger" | bool | True/False | Vrai pattern ou fallback |

---

## 🔍 ANALYSE DE COHÉRENCE DES DONNÉES

### **Champs Communs aux 3 Fonctions**
| Champ | OrderFlow | Footprint M1 | Triggers | Type |
|-------|-----------|--------------|----------|------|
| **score** | ✅ 0-1 | ✅ 0-1 | ✅ 0-1 | float |
| **dir** | ✅ +1/-1/0 | ✅ +1/-1/0 | ✅ +1/-1/0 | int |
| **delta_total** | ✅ | ✅ | ❌ | float |
| **poc/anchor** | ✅ poc | ✅ poc | ✅ anchor | float |
| **absorption** | ✅ | ✅ | ❌ | bool |
| **status** | ✅ | ✅ | ❌ | str |

### **Métriques Uniques**
| Fonction | Métriques Exclusives |
|----------|---------------------|
| **OrderFlow** | imbalance, cvd_slope, vah/val, hvn/lvn |
| **Footprint M1** | tick_count, tick_rate, coverage_s, buy/sell volume |
| **Triggers** | trigger_type, window_s, delta_ratio, volume_zscore |

---

## 📊 SYNTHÈSE DE QUALITÉ DES DONNÉES

### **Axes de Qualité Identifiés**

#### **1. Qualité Intrinsèque** (fiabilité des données)
```python
# OrderFlow
if status == "VALID" and score >= 0.70:
    qualite_of = "HAUTE"
elif status == "VALID" and score >= 0.50:
    qualite_of = "MOYENNE"
else:
    qualite_of = "BASSE"

# Footprint M1
if status == "VALID" and tick_count >= 100 and coverage_s >= 15.0:
    qualite_fp = "HAUTE"
elif status == "VALID" and tick_count >= 50:
    qualite_fp = "MOYENNE"
else:
    qualite_fp = "BASSE"

# Triggers
if type != "fusion_pretrigger" and score >= 0.75:
    qualite_trig = "HAUTE"
elif type != "fusion_pretrigger" and score >= 0.60:
    qualite_trig = "MOYENNE"
else:
    qualite_trig = "BASSE"  # ou ABSENT si fusion_pretrigger
```

#### **2. Intensité Directionnelle** (force du signal)
```python
# Delta Strength (OrderFlow + Footprint)
delta_combined = abs(of_delta_total) + abs(fp_delta_total)

if delta_combined >= 3000:      # XAUUSD
    intensite_delta = "FORTE"
elif delta_combined >= 1500:
    intensite_delta = "MOYENNE"
else:
    intensite_delta = "FAIBLE"

# Imbalance (OrderFlow)
if imbalance >= 0.70:
    intensite_imb = "FORTE"
elif imbalance >= 0.55:
    intensite_imb = "MOYENNE"
else:
    intensite_imb = "FAIBLE"

# Pattern Quality (Triggers)
if type in ["stacking", "climax"]:
    intensite_pattern = "FORTE"
elif type in ["micro_stack", "absorption"]:
    intensite_pattern = "MOYENNE"
else:
    intensite_pattern = "FAIBLE"
```

#### **3. Volume et Activité** (liquidité)
```python
# Tick Rate (Footprint M1)
if tick_rate >= 60:           # 60+ ticks/s
    activite = "HAUTE"
elif tick_rate >= 30:
    activite = "MOYENNE"
else:
    activite = "BASSE"

# Volume Z-Score (Triggers)
if volume_zscore >= 2.5:      # >2.5 écart-types
    intensite_vol = "EXCEPTIONNELLE"
elif volume_zscore >= 2.0:
    intensite_vol = "FORTE"
else:
    intensite_vol = "NORMALE"
```

#### **4. Momentum** (dynamique)
```python
# CVD Slope (OrderFlow)
if abs(cvd_slope) >= 2.0:
    momentum = "FORT"
elif abs(cvd_slope) >= 1.0:
    momentum = "MOYEN"
else:
    momentum = "FAIBLE"
```

#### **5. Cohérence Inter-Fonctions** (consensus)
```python
# Alignement directionnel
aligned_functions = [
    of_dir if of_dir != 0 else None,
    fp_dir if fp_dir != 0 else None,
    trig_dir if trig_dir != 0 else None,
]
aligned_functions = [d for d in aligned_functions if d is not None]

if len(set(aligned_functions)) == 1 and len(aligned_functions) == 3:
    consensus = "UNANIME" (3/3)
elif len(set(aligned_functions)) == 1 and len(aligned_functions) == 2:
    consensus = "MAJORITAIRE" (2/3)
elif len(set(aligned_functions)) > 1:
    consensus = "CONFLIT"
else:
    consensus = "INDETERMINE"
```

---

## 🎯 PROPOSITION : SYSTÈME DE NOTATION MULTI-CRITÈRES

### **Score Composite sur 100 points**

```python
# PARTIE 1 : Qualité des Données (20 points)
qualite_score = 0

if of_status == "VALID":           qualite_score += 5
if fp_status == "VALID":           qualite_score += 5
if tick_count >= 100:              qualite_score += 5
if type != "fusion_pretrigger":   qualite_score += 5

# PARTIE 2 : Intensité Directionnelle (25 points)
intensite_score = 0

# Delta combiné
if delta_combined >= 3000:         intensite_score += 10
elif delta_combined >= 1500:      intensite_score += 6

# Imbalance
if imbalance >= 0.70:              intensite_score += 8
elif imbalance >= 0.55:           intensite_score += 5

# Pattern quality
if type in ["stacking", "climax"]: intensite_score += 7
elif type in ["micro_stack"]:     intensite_score += 4

# PARTIE 3 : Volume et Activité (20 points)
volume_score = 0

# Tick rate
if tick_rate >= 60:                volume_score += 8
elif tick_rate >= 30:             volume_score += 5

# Volume Z-Score
if volume_zscore >= 2.5:           volume_score += 12
elif volume_zscore >= 2.0:        volume_score += 8

# PARTIE 4 : Momentum (15 points)
momentum_score = 0

if abs(cvd_slope) >= 2.0:          momentum_score += 15
elif abs(cvd_slope) >= 1.0:       momentum_score += 10
elif abs(cvd_slope) >= 0.5:       momentum_score += 5

# PARTIE 5 : Consensus (20 points)
consensus_score = 0

if consensus == "UNANIME":         consensus_score += 20
elif consensus == "MAJORITAIRE":  consensus_score += 12
elif consensus == "CONFLIT":      consensus_score -= 10

# SCORE FINAL
score_total = qualite_score + intensite_score + volume_score + momentum_score + consensus_score
score_total = max(0, min(100, score_total))  # Clamp 0-100
```

### **Échelle de Décision**

| Score Total | Niveau | Signal | Action |
|-------------|--------|--------|--------|
| **90-100** | 💎 DIAMANT | HIGH_CONVICTION_IMMEDIATE | Trade immédiat |
| **80-89** | 🔷 PLATINE | HIGH_CONVICTION | Trade fort |
| **70-79** | 🟡 OR | MODERATE | Trade modéré |
| **60-69** | 🔘 ARGENT | CAUTIOUS | Trade prudent |
| **50-59** | 🟤 BRONZE | WAIT | Surveillance |
| **<50** | ⚪ REJETÉ | HOLD | Pas de trade |

---

## 🔧 PROCHAINES ÉTAPES

1. **Valider les seuils** selon vos observations XAUUSD
2. **Tester le scoring** sur données historiques
3. **Ajuster les pondérations** (20/25/20/15/20) selon importance
4. **Intégrer dans FusionManager** via nouvelle fonction `_calculate_composite_score()`

---

*Document créé le : 2025-11-22*
*Auteur : Analyse FusionManager*
