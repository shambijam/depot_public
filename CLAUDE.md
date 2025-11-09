# CLAUDE.md - Historique des Modifications

## Session du 9 Novembre 2025

### 🎯 Objectif Principal
Unifier complètement la nomenclature `burst_single_master` → `burst_scalping` pour éliminer toute ambiguïté dans le code et les configurations.

---

## 📋 Problème Identifié

### **Incohérence de Nommage Legacy**
Deux noms différents utilisés pour la même fonctionnalité :
- **Code legacy** : `burst_single_master` (strategy/scalping.py)
- **Configuration moderne** : `burst_scalping` (toutes les configs)

**Impact** :
- Confusion dans la compréhension du code
- Normalisation nécessaire dans run_bot.py pour gérer les alias
- Risque de bugs si la normalisation est oubliée quelque part
- Documentation incohérente

---

## ✅ Solution Appliquée : Unification Complète vers `burst_scalping`

### Fichiers Modifiés

**1. `strategy/scalping.py`**
- Ligne 375 : `"rule_name": "burst_single_master"` → `"burst_scalping"`
- Ligne 750 : `"rule_name": "burst_single_master"` → `"burst_scalping"`
- Ligne 724 : Méthode `_rule_burst_single_master()` → `_rule_burst_scalping()`

**2. `trader/sizing.py`**
- Lignes 339, 345 : Simplifié `rule_name in {"burst_scalping", "burst_single_master"}` → `rule_name == "burst_scalping"`

**3. `trader/order_builder.py`**
- Ligne 568 : Retiré `"burst_single_master"` de la liste des alias

**4. `run_bot.py`**
- Ligne 417 : Retiré `"burst_single_master"` de la liste des alias (2 occurrences)
- Ligne 2530 : Retiré `"burst_single_master"` de la liste des alias

**5. `CLAUDE.md`**
- Remplacement global de `burst_single_master` → `burst_scalping`

---

## 📊 Résultat Final

**Nomenclature unifiée** : `burst_scalping` partout
- ✅ Code source (strategy/scalping.py)
- ✅ Configuration (config_trade_scalping.json, XAUUSD.json)
- ✅ Pipeline d'exécution (run_bot.py, order_builder.py, sizing.py)
- ✅ Documentation (CLAUDE.md)

**Alias historiques conservés** (pour rétrocompatibilité) :
- `"burst"`, `"burst_master"`, `"scalping_burst"`, `"burst_single"`, `""`

→ **Plus aucune référence à `burst_single_master` dans le code**

---

## 🎯 Avantages

1. ✅ **Cohérence totale** code/config
2. ✅ **Meilleure lisibilité** et maintenance
3. ✅ **Moins de risques de bugs** futurs
4. ✅ **Documentation claire** et non ambiguë
5. ✅ **Compréhension immédiate** de la stratégie (burst + scalping)

---

*Dernière mise à jour : 9 Novembre 2025*

---

## Session du 8 Novembre 2025

### 🎯 Objectif Principal
Nettoyer et réorganiser l'architecture des fichiers de configuration pour garantir que **TOUS les trades scalping burst** utilisent **SL/TP 400 pips + trailing à +28 pips**, sans aucune interférence des anciennes méthodes.

---

## 📋 Problèmes Identifiés

### 1. **Redondance Massive entre Fichiers de Configuration**
- `prod_config.json`, `config_trade_scalping.json` et `XAUUSD.json` contenaient les **mêmes paramètres en triple**
- Configuration SL/TP présente dans les 3 fichiers
- Configuration trailing dupliquée
- Risk_per_trade_percent incohérent (0.73% vs 0.30%)
- **Total : 88 lignes de code redondant**

### 2. **Trailing Stop Activé Trop Tôt (Problème Historique)**
- Avant : Trailing s'activait immédiatement et "fusillait" les trades en 2-3 secondes
- Cause : `spread_multiplier: 2.5` retardait l'activation réelle au-delà des 28 pips configurés
- Formule problématique : `ACTIVATION_PIPS = max(28.0, spread_floor_pips)`
- Avec spread élevé (15 pips), activation retardée à 38.5 pips au lieu de 28

### 3. **Vieux Patterns Court-Circuitant le Système**
Ordre d'évaluation dans `strategy/scalping.py` :
```python
1. range_accumulation_mtf        → 30 pips par défaut
2. momentum patterns             → SL/TP variables
   - breakout_consolidation
   - trend_pullback
   - inside_bar_breakout
   - momentum_ignition
3. range_accumulation simple     → 30 pips
4. burst_scalping          → 400 pips (JAMAIS ATTEINT si patterns matchent avant)
```

**Impact** : Si un pattern matchait, il retournait sa propre décision avec `target_sl_pips` (souvent 30 pips) et court-circuitait le burst_scalping configuré à 400 pips.

### 4. **Problème burst_size (Historique Critique)**
- Code cherchait `burst_scalping` au lieu de `burst_scalping`
- XAUUSD configuré à 8 positions utilisait toujours 5 (default codé en dur)
- Perte d'override lors du nettoyage (8 → 5 accidentellement)

---

## ✅ Solutions Appliquées

### 1. **Réorganisation Architecture des Configurations (Option B)**

#### **Principe : Séparation Stricte des Responsabilités**

**prod_config.json** - Infrastructure UNIQUEMENT
- Chemins (paths)
- Configuration AI
- Stratégies disponibles (mapping)
- Global safety (limites compte)
- Trade executor settings (retry, telegram, MT5)
- Bot behavior (timezone, hours, cycle)
- Guardrails globaux
- **✅ SUPPRIMÉ** : entry_rules (-48 lignes)
- **✅ CORRIGÉ** : risk_per_trade_pct de 0.73% → 0.30%

**config_trade_scalping.json** - Source de Vérité Stratégie
- Metadata stratégie (strategy_name, magic_number, tradeable_assets)
- `entry_rules.scalping.burst_scalping` COMPLET :
  - SL/TP configuration (400 pips)
  - Trailing configuration (activation 28 pips, step 8 pips)
  - Footprint, orderflow, fusion configs
- Phase detection
- Risk defaults
- Decision rules
- **✅ RESTRUCTURÉ** : trailing déplacé dans burst_scalping
- **✅ AJOUTÉ** : Désactivation des vieux patterns

**XAUUSD.json** - Asset Spécifique + Overrides UNIQUEMENT
- Symbol info (point, digits, contract_size)
- Volatility (spread max, daily range pour l'OR)
- Risk management (base_lot_size, daily/weekly limits)
- Strategy toggles / whitelist
- Overrides spécifiques XAUUSD :
  - ✅ `burst_size: 8` (override de 5)
  - ✅ `orderflow_v6` settings spécifiques OR
  - ✅ `of_v6_gate` parameters
- **✅ SUPPRIMÉ** : sltp et trailing identiques (-40 lignes)
- **✅ CORRIGÉ** : risk_per_trade_percent de 0.73% → 0.30%

**Gain : -88 lignes de redondance supprimées**

---

### 2. **Fix Trailing Stop - Garantie Activation à 28 Pips**

**Avant** :
```json
"broker_floors": {
  "min_sl_distance_pips": 6.0,
  "spread_multiplier": 2.5,
  "extra_buffer_pips": 1.0
}
```

**Code problématique (sltp.py ligne 2065-2067)** :
```python
spread_floor_pips = (2.5 × cur_spread_pips) + 1.0
ACTIVATION_PIPS = max(28.0, spread_floor_pips)  # Retardé si spread > 10.8 pips
```

**Après** :
```json
"broker_floors": {
  "min_sl_distance_pips": 6.0,
  "spread_multiplier": 0.0,    // ✅ DÉSACTIVÉ
  "extra_buffer_pips": 0.0     // ✅ DÉSACTIVÉ
}
```

**Résultat** :
```python
spread_floor_pips = 0.0
ACTIVATION_PIPS = max(28.0, 0.0) = 28.0  // ✅ TOUJOURS 28 pips
```

---

### 3. **Désactivation des Vieux Patterns**

**Modifications dans config_trade_scalping.json** :

```json
"momentum": {
  "enabled": false,  // ✅ AJOUTÉ
  "breakout": { ... },
  "trend_pullback": { ... },
  "ignition": { ... }
},
"patterns": {
  "enabled": false,  // ✅ AJOUTÉ
  "inside_bar": { ... }
},
"range_accumulation": {
  "enabled": false   // ✅ AJOUTÉ
},
"range_accumulation_mtf": {
  "enabled": false   // ✅ AJOUTÉ
}
```

**Impact** : SEUL `burst_scalping` est maintenant évalué dans `strategy/scalping.py`

---

### 4. **Fix burst_size - Correction Critique**

#### **Problème 1 : Mauvais Nom de Clé**

**Fichier** : `strategy/scalping.py` ligne 313-315

**Avant** :
```python
sm_cfg = ((strat_cfg.get("entry_rules") or {}).get("scalping") or {}).get(
    "burst_scalping", {}  # ❌ MAUVAISE CLÉ
) or {}
burst_sz = sm_cfg.get("burst_size", 5)  # Retourne TOUJOURS 5
```

**Après** :
```python
# Config burst_scalping
sm_cfg = ((strat_cfg.get("entry_rules") or {}).get("scalping") or {}).get(
    "burst_scalping", {}  # ✅ BONNE CLÉ
) or {}
burst_sz = sm_cfg.get("burst_size", 5)  # Lit la vraie config
```

#### **Problème 2 : Perte d'Override**

**Fichier** : `config/assets_config/XAUUSD.json` ligne 134

**Commit 5d60cf0** (ce matin) :
```json
"burst_size": 8,  ✅ Correct
```

**Commit 051ff37** (nettoyage - erreur) :
```json
"burst_size": 5,  ❌ Changé accidentellement
```

**Après correction** :
```json
"burst_size": 8,  ✅ Restauré
```

---

## 📊 Flux de Trading Final Garanti

```
┌─────────────────────────────────────────────────────────────┐
│ 1. FusionManager (3 fonctions phares)                       │
│    - Génère signal de trading                                │
│    - NE met PAS de target_sl_pips                           │
└────────────────────────┬────────────────────────────────────┘
                         ↓
┌─────────────────────────────────────────────────────────────┐
│ 2. ScalpingStrategy.evaluate_entry()                         │
│    Tests patterns (TOUS DÉSACTIVÉS) ✅                       │
│    - range_accumulation_mtf → SKIP                          │
│    - momentum patterns → SKIP                                │
│    - range_accumulation → SKIP                               │
│    → Arrive à burst_scalping                            │
│                                                              │
│    Lecture burst_size (CORRIGÉE) ✅                          │
│    - XAUUSD: lit 8 (depuis config merged)                   │
│    - EURUSD: lit 5 (depuis config default)                  │
└────────────────────────┬────────────────────────────────────┘
                         ↓
┌─────────────────────────────────────────────────────────────┐
│ 3. burst_scalping                                       │
│    - Retourne décision SANS target_sl_pips                  │
│    - Burst size : 5 (EURUSD) ou 8 (XAUUSD)                  │
└────────────────────────┬────────────────────────────────────┘
                         ↓
┌─────────────────────────────────────────────────────────────┐
│ 4. sltp.py._calculate_sl_tp_prices()                        │
│    - Cherche target_sl_pips → PAS TROUVÉ                   │
│    - Lit config : entry_rules.scalping.burst_scalping.sltp │
│    - APPLIQUE SL/TP 400 pips ✅                             │
└────────────────────────┬────────────────────────────────────┘
                         ↓
┌─────────────────────────────────────────────────────────────┐
│ 5. order_builder.py._resolve_burst_size()                   │
│    - Lit trade_decision["burst_size"]                       │
│    - XAUUSD: 8 positions ✅                                 │
│    - EURUSD: 5 positions ✅                                 │
└────────────────────────┬────────────────────────────────────┘
                         ↓
┌─────────────────────────────────────────────────────────────┐
│ 6. burst.py.open_burst_basket()                             │
│    - for _ in range(burst_size): send_order()               │
│    - XAUUSD: Ouvre 8 positions ✅                           │
│    - EURUSD: Ouvre 5 positions ✅                           │
└────────────────────────┬────────────────────────────────────┘
                         ↓
┌─────────────────────────────────────────────────────────────┐
│ 7. Monitoring Trailing (sltp.py ligne 2129-2184)            │
│    - Surveillance toutes les 2 secondes                      │
│    - Si pnl_pips >= 28.0 (ACTIVATION_PIPS) ✅               │
│    - Active trailing (step 8 pips)                          │
│    - Sécurise le profit si retour en arrière                │
└─────────────────────────────────────────────────────────────┘
```

---

## 🔧 Commits Créés

### Commit 1 : `051ff37` - "Refacto config architecture et fix trailing activation"

**Fichiers modifiés** :
- `config/prod_config.json` (-50 lignes)
- `config/strategy/config_trade_scalping.json` (+20/-20 lignes)
- `config/assets_config/XAUUSD.json` (-40 lignes)

**Changements** :
1. Séparation stricte des responsabilités
2. Suppression doublons SL/TP (88 lignes)
3. Uniformisation risk_per_trade_percent à 0.30%
4. Désactivation spread_multiplier (0.0)
5. Garantit activation trailing à exactement +28 pips

### Modifications Additionnelles (Non commitées)

**Fichiers modifiés** :
- `strategy/scalping.py` (ligne 314) - Fix lecture burst_scalping
- `config/strategy/config_trade_scalping.json` - Désactivation patterns
- `config/assets_config/XAUUSD.json` (ligne 134) - Restauration burst_size: 8

---

## 📖 Analyse de la Cascade des Fallbacks (sltp.py)

### Extraction Configuration SL (ligne 488-492)
```python
sl_pips_default = dyn_sl.get("pips",              # ÉTAPE 1 → 400 ✅
    legacy.get("stop_loss_pips",                   # ÉTAPE 2 → N/A (disabled)
        config.get("stop_loss_pips", 10)           # ÉTAPE 3/4 → N/A / 10 (jamais atteint)
    )
)
```

**Ordre de priorité** :
1. ✅ `dyn_sl["pips"]` = 400 (depuis config_trade_scalping.json) → **UTILISÉ**
2. ⚠️ `legacy["stop_loss_pips"]` → Vide (smart_sl_tp_settings désactivé)
3. ⚠️ `config["stop_loss_pips"]` → N'existe pas dans prod_config.json
4. ❌ Fallback codé en dur : 10 pips → Jamais atteint

**Résultat : 400 pips TOUJOURS appliqués** ✅

---

## 🧪 Test de Validation burst_size

**Simulation Python** :
```python
# Config générique (config_trade_scalping.json)
config_scalping["entry_rules"]["scalping"]["burst_scalping"]["burst_size"] = 5

# Config XAUUSD (après merge)
config_xauusd["entry_rules"]["scalping"]["burst_scalping"]["burst_size"] = 8

# Résolution
EURUSD: scalping.py lit 5 → order_builder résout 5 ✅
XAUUSD: scalping.py lit 8 → order_builder résout 8 ✅
```

**Résultat test** :
```
=== EURUSD (config générique) ===
scalping.py lit: 5
order_builder.py résout: 5

=== XAUUSD (config merged) ===
scalping.py lit: 8
order_builder.py résout: 8

✅ RÉSULTAT:
  - Assets génériques: 5 positions
  - XAUUSD: 8 positions
```

---

## ⚠️ Points d'Attention Identifiés

### 1. ~~Incohérence de Nommage~~ ✅ CORRIGÉ
- ~~Code cherchait : `entry_rules.scalping.burst_scalping`~~
- ~~Config avait : `entry_rules.scalping.burst_scalping`~~
- **✅ RÉSOLU** : Code corrigé pour lire `burst_scalping`

### 2. Fichier `run_bot.py` Ligne 200
Ajout de `"entry_rules"` dans `sections_to_merge` pour permettre aux assets d'override les paramètres SL/TP.

**Commit précédent** : `5d60cf0` - "Refacto_Claude_sltp_dyn"

---

## 📈 Résultats Attendus

### Stratégie Scalping Burst (UNIQUE méthode active)
```
1. Entrée → Burst de 5 positions (8 pour XAUUSD)
            SL/TP fixes à 400 pips

2. Surveillance → Toutes les 2 secondes

3. Activation trailing → Dès que profit >= +28 pips EXACTEMENT

4. Gestion dynamique →
   - Si profit continue : Trailing suit (step 8 pips)
   - Si retour : Trailing coupe et sécurise le profit
```

### Configuration Finale Validée
- ✅ SL : 400 pips
- ✅ TP : 400 pips
- ✅ RR dynamique : 1.5x (base), 1.0-3.0 (floor-cap)
- ✅ Trailing activation : 28 pips (garanti, sans retard)
- ✅ Trailing step : 8 pips
- ✅ Update interval : 2 secondes
- ✅ Break-even : désactivé
- ✅ Risk per trade : 0.30%
- ✅ Burst size : 5 (EURUSD/GBPUSD), 8 (XAUUSD)

---

## 🔍 Fichiers Concernés

### Configuration
- `/config/prod_config.json`
- `/config/strategy/config_trade_scalping.json`
- `/config/assets_config/XAUUSD.json`

### Code
- `/trader/sltp.py` (ligne 253-750 : calcul SL/TP, ligne 1023-1300 : trailing)
- `/strategy/scalping.py` (ligne 82-400 : evaluate_entry, **ligne 314 : fix burst_scalping**)
- `/core/decision_pipeline.py` (ligne 1384-1410, 2129-2320 : risk parameters)
- `/run_bot.py` (ligne 165-211 : merge config)

### Schémas
- `/config/schemas/asset_schema.json`
- `/config/schemas/strategy_schema.json`

---

## 📝 Notes de Session

### Philosophie de Configuration Adoptée
**Option B : Séparation Stricte**
- Chaque paramètre a UNE "maison" unique
- Overrides explicites uniquement si nécessaire
- Hiérarchie claire : Global → Stratégie → Asset

### Principe de Fusion Automatique
```python
# run_bot.py ligne 206-209
merged_config = _deep_merge_dicts(
    config_trade_scalping["entry_rules"],  # Base
    XAUUSD["overrides"]["scalping"]["entry_rules"]  # Override
)
```

### Priorité Override (du plus fort au plus faible)
```
XAUUSD.json (overrides)
    ↓ écrase
config_trade_scalping.json
    ↓ écrase
prod_config.json
```

---

## 🎯 Confirmation Finale

**Question : "À l'heure où je vous parle, la seule et unique façon de trader en scalping est mon sltp dynamique à 400 pips ?"**

**Réponse : OUI** ✅

Après les corrections appliquées :
1. ✅ Tous les vieux patterns sont désactivés
2. ✅ Seul `burst_scalping` est actif (nom corrigé)
3. ✅ Aucun `target_sl_pips` n'est mis par la stratégie
4. ✅ `sltp.py` lit les 400 pips de la config
5. ✅ Trailing s'active à exactement 28 pips (sans retard)
6. ✅ FusionManager prime pour la génération de signaux
7. ✅ burst_size corrigé : 5 pour assets génériques, 8 pour XAUUSD

**Tous les trades scalping burst utilisent maintenant SL/TP 400 pips + trailing à +28 pips.** 🚀

---

## 📅 Prochaines Sessions (Suggestions)

### Points à Vérifier
1. ~~Harmoniser nommage `burst_scalping` vs `burst_scalping`~~ ✅ FAIT
2. Tester en conditions réelles (spread élevé, news)
3. Analyser les logs de trades pour confirmer les 400 pips et burst_size correct

### Évolutions Possibles
1. Configuration du trailing par asset (si EURUSD a besoin de 20 pips au lieu de 28)
2. Ajout de métriques de performance du trailing
3. Optimisation du burst_size selon volatilité

---

*Document maintenu par Claude Code*
*Dernière mise à jour : 8 Novembre 2025 - 17:30*
