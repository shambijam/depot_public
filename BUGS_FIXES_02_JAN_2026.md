# 🐛 CORRECTION BUGS TIMING GATEKEEPER - 02 JAN 2026

## 📋 Bugs Identifiés (depuis RAPPORT_ORDERFLOW_V6_ANALYSE_COMPLETE.md)

### Bug #1: GMT Hours veto NOT applied ❌

**Symptôme**:
- Trade exécuté à **11h GMT**
- Config GBPUSD.json spécifie: `allowed_hours_gmt = [7, 8, 9, 13, 14, 15]`
- **11h GMT n'est PAS dans la liste** → Trade aurait dû être VETO
- Mais timing_gatekeeper a retourné **PASS** ✅ (incorrect)

**Impact**: Trades exécutés en dehors des heures optimales Londres/NY overlap

---

### Bug #2: Asset-specific tick_rate ignored ❌

**Symptôme**:
- Trade exécuté avec **1.2 ticks/s**
- Config GBPUSD.json spécifie: `min_tick_rate = 2.5`
- **1.2 < 2.5** → Trade aurait dû être VETO
- Mais timing_gatekeeper a retourné **PASS** ✅ (incorrect)

**Impact**: Trades exécutés dans conditions de liquidité faible

---

## 🔍 Analyse de la Cause Racine

### 🚨 BUG CRITIQUE DÉCOUVERT: Mauvaise méthode ConfigManager

**Problème #1 dans `run_bot.py`** (3 occurrences: lignes 1452, 3290, 3455):

**Code AVANT** (bugué):
```python
# ❌ FAUX: get_asset_config() N'EXISTE PAS dans ConfigManager
asset_config_timing = config_manager.get_asset_config(asset) if hasattr(config_manager, 'get_asset_config') else {}
```

**Résultat**:
1. `hasattr(config_manager, 'get_asset_config')` → **False** (méthode n'existe pas)
2. Donc `asset_config_timing = {}` → **Toujours vide !**
3. `timing_analyzer.py` reçoit `asset_config = {}` → Config asset jamais chargée

**Méthode correcte** (ConfigManager.py:121):
```python
def load_asset_config(self, asset: str) -> Dict[str, Any]:
    """Charge et met en cache la config d'un actif (EURUSD.json, GBPUSD.json, etc.)"""
    # Lecture directe du JSON
    with open(f"config/assets_config/{asset}.json", "r") as f:
        return json.load(f)
```

**Impact**:
- **100% des trades** utilisaient la config GLOBALE au lieu de la config ASSET
- Heures GMT spécifiques **JAMAIS appliquées**
- Seuils tick_rate asset-specific **JAMAIS appliqués**
- `timing_config_asset` était **TOUJOURS vide** → Merge ne faisait rien

---

### Problème #2 dans `timing_analyzer.py` (lignes 64-106)

**Code AVANT** (logique de merge inadéquate):
```python
timing_config = {}

# PRIORITÉ 1: Config asset
if asset_config:
    timing_config = asset_config.get("overrides", {}).get("scalping", {}).get("timing_gatekeeper", {})

# PRIORITÉ 2: Config globale (fallback)
if not timing_config and scalping_config:  # ❌ Ne fusionne pas, remplace
    timing_config = scalping_config.get("entry_rules", {}).get("scalping", {}).get("timing_gatekeeper", {})
```

**Problème secondaire** (masqué par le bug #1):
- Si `asset_config = {}` (à cause du bug #1), `timing_config = {}` aussi
- Condition `if not timing_config` est **True**
- Config globale **REMPLACE** au lieu de fusionner

**Config globale** (`config_trade_scalping.json:72-88`):
```json
"allowed_hours_gmt": [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 14, 15, 16]
                                                         ↑
                                                      11h GMT AUTORISÉ (FAUX pour GBPUSD)
```

**Config asset GBPUSD** (`GBPUSD.json:195-197`):
```json
"allowed_hours_gmt": [7, 8, 9, 13, 14, 15],  // ✅ 11h GMT EXCLU
"min_tick_rate": 2.5                          // ✅ Seuil strict
```

**Résultat du bug**:
- Bot utilise config globale (11h autorisé, min_tick_rate=1.0)
- Au lieu de config asset (11h interdit, min_tick_rate=2.5)

---

## ✅ SOLUTION APPLIQUÉE

### Correction #1: `run_bot.py` (3 occurrences: lignes 1452, 3290, 3455)

**Changement**: Utiliser `load_asset_config()` au lieu de `get_asset_config()`

**Code APRÈS** (corrigé):
```python
# ✅ CORRECT: load_asset_config() existe et retourne le JSON complet
asset_config_timing = config_manager.load_asset_config(asset)
```

**Impact**:
- `asset_config_timing` maintenant contient `{"symbol": "GBPUSD", "overrides": {...}}`
- `timing_analyzer.py` reçoit la vraie config asset
- Merge config peut maintenant fonctionner correctement

---

### Correction #2: `phase_observer/timing_analyzer.py` (lignes 64-113)

**Approche**: Merge **granulaire paramètre par paramètre**
- Base: Config globale (tous les paramètres par défaut)
- Override: Config asset écrase les paramètres spécifiques

**Code APRÈS** (corrigé):
```python
# ========================================================================
# 0️⃣ CONFIGURATION (02 JAN 2026 - MERGE ASSET + GLOBAL avec priorité ASSET)
# ========================================================================
# 🐛 FIX BUG #1 & #2: Merge granulaire paramètre par paramètre
timing_config_global = {}
timing_config_asset = {}

# 1. Charger config GLOBALE d'abord (base)
if scalping_config:
    entry_rules = scalping_config.get("entry_rules", {})
    scalping_rules = entry_rules.get("scalping", {})
    timing_config_global = scalping_rules.get("timing_gatekeeper", {})

# 2. Charger config ASSET (écrase global)
if asset_config:
    overrides = asset_config.get("overrides", {})
    scalping_overrides = overrides.get("scalping", {})
    timing_config_asset = scalping_overrides.get("timing_gatekeeper", {})

# 3. MERGE: Start avec global, puis écrase avec asset
timing_config = dict(timing_config_global)  # Copie base globale
timing_config.update(timing_config_asset)   # ✅ Écrase avec params asset-specific

# 🔍 DEBUG: Log config finale
logger.critical(
    f"[TIMING_CONFIG_FINAL][{asset}] "
    f"allowed_hours_gmt={timing_config.get('allowed_hours_gmt', 'NOT_SET')} | "
    f"min_tick_rate={timing_config.get('min_tick_rate', 'NOT_SET')} | "
    f"source={'ASSET' if timing_config_asset else 'GLOBAL'}"
)

# Seuils (avec fallback hardcodé si absent)
min_tick_rate = timing_config.get("min_tick_rate", 1.0)
min_coverage_s = timing_config.get("min_coverage_s", 40.0)
max_tick_rate = timing_config.get("max_tick_rate", 200.0)
```

---

## 🎯 Comportement Attendu Après Fix

### Pour GBPUSD

**Config finale après merge**:
```python
timing_config = {
    # ✅ Depuis ASSET (prioritaire)
    "allowed_hours_gmt": [7, 8, 9, 13, 14, 15],  # ← GBPUSD.json:195
    "min_tick_rate": 2.5,                         # ← GBPUSD.json:197
    "min_coverage_s": 40.0,                       # ← GBPUSD.json:198
    "max_tick_rate": 200.0,                       # ← GBPUSD.json:199
    "max_spread_pips": 1.8,                       # ← GBPUSD.json:200

    # ✅ Depuis GLOBAL (non écrasés)
    "enabled": true,                              # ← config_trade_scalping.json:70
    "optimal_hours_gmt": {...}                    # ← config_trade_scalping.json:90-98
}
```

**VETO attendus**:
1. **11h GMT** → VETO ❌ "🚫 Heure 11h GMT NON autorisée (whitelist: [7, 8, 9, 13, 14, 15])"
2. **tick_rate=1.2** → VETO ❌ "Tick rate trop faible (1.2 < 2.5 ticks/sec)"

---

### Pour EURUSD

**Config EURUSD** (`EURUSD.json:195-200`):
```json
"allowed_hours_gmt": [7, 8, 9, 10, 13, 14, 15, 16],
"min_tick_rate": 2.0
```

**Config finale après merge**:
```python
{
    "allowed_hours_gmt": [7, 8, 9, 10, 13, 14, 15, 16],  # ✅ ASSET
    "min_tick_rate": 2.0,                                 # ✅ ASSET
    # ... autres params
}
```

---

### Pour USDJPY

**Config USDJPY** (`USDJPY.json:254-260`):
```json
"timing_gatekeeper": {
    "comment": "⚠️ DÉPRÉCIÉ: allowed_hours_gmt déplacé vers config_trade_scalping.json",
    "enabled": true,
    "min_tick_rate": 0.5,
    "min_coverage_s": 30.0
}
```

**Config finale après merge**:
```python
{
    # ✅ Depuis ASSET
    "min_tick_rate": 0.5,      # Adapté DEMO
    "min_coverage_s": 30.0,

    # ✅ Depuis GLOBAL (USDJPY n'a pas allowed_hours_gmt dans asset)
    "allowed_hours_gmt": [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 14, 15, 16]
}
```

---

## 📊 Tests de Validation

### Test Case 1: GBPUSD à 11h GMT avec tick_rate=1.2

**Avant fix**:
```
[TIMING_GATEKEEPER][GBPUSD] PASS ✅ | session=LONDON | tick_rate=1.2/s
```

**Après fix attendu**:
```
[TIMING_CONFIG_FINAL][GBPUSD] allowed_hours_gmt=[7, 8, 9, 13, 14, 15] | min_tick_rate=2.5 | source=ASSET
[TIMING_GATEKEEPER][GBPUSD] VETO ❌ | veto_reason="🚫 Heure 11h GMT NON autorisée (whitelist: [7, 8, 9, 13, 14, 15])"
```

---

### Test Case 2: GBPUSD à 14h GMT avec tick_rate=1.2

**Avant fix**:
```
[TIMING_GATEKEEPER][GBPUSD] PASS ✅ (heure OK mais tick_rate insuffisant)
```

**Après fix attendu**:
```
[TIMING_CONFIG_FINAL][GBPUSD] allowed_hours_gmt=[...14...] | min_tick_rate=2.5 | source=ASSET
[TIMING_GATEKEEPER][GBPUSD] VETO ❌ | veto_reason="Tick rate trop faible (1.2 < 2.5 ticks/sec)"
```

---

### Test Case 3: GBPUSD à 14h GMT avec tick_rate=3.0

**Avant fix**:
```
[TIMING_GATEKEEPER][GBPUSD] PASS ✅
```

**Après fix attendu**:
```
[TIMING_CONFIG_FINAL][GBPUSD] allowed_hours_gmt=[...14...] | min_tick_rate=2.5 | source=ASSET
[TIMING_GATEKEEPER][GBPUSD] PASS ✅ | session=LONDON | tick_rate=3.0/s
```

---

## 📁 Fichiers Modifiés

| Fichier | Lignes | Changement |
|---------|--------|------------|
| `run_bot.py` | 1452, 3290, 3455 | Correction appel `load_asset_config()` (3 occurrences) |
| `phase_observer/timing_analyzer.py` | 64-113 | Logique merge config ASSET + GLOBAL + logs debug |

---

## 🔧 Debug Logs Ajoutés

### Logs de traçage config (timing_analyzer.py:83-102)

Pour débugger le chargement de la config asset, plusieurs logs CRITICAL ont été ajoutés:

```python
# 1. Structure asset_config reçue
logger.critical(
    f"[TIMING_DEBUG][{asset}] asset_config reçue = "
    f"type={type(asset_config).__name__} | "
    f"keys={list(asset_config.keys())} | "
    f"has_overrides={('overrides' in asset_config)}"
)

# 2. Overrides extraits
logger.critical(f"[TIMING_DEBUG][{asset}] overrides = keys={list(overrides.keys())}")

# 3. Scalping overrides
logger.critical(f"[TIMING_DEBUG][{asset}] scalping_overrides = keys={list(scalping_overrides.keys())}")

# 4. Timing config asset final
logger.critical(f"[TIMING_DEBUG][{asset}] timing_config_asset = {timing_config_asset}")

# 5. Config finale après merge
logger.critical(
    f"[TIMING_CONFIG_FINAL][{asset}] "
    f"allowed_hours_gmt={timing_config.get('allowed_hours_gmt', 'NOT_SET')} | "
    f"min_tick_rate={timing_config.get('min_tick_rate', 'NOT_SET')} | "
    f"source={'ASSET' if timing_config_asset else 'GLOBAL'}"
)
```

### Exemple de sortie attendue (GBPUSD)

**Avant fix**:
```
[CRITICAL] [TIMING_DEBUG][GBPUSD] asset_config reçue = type=dict | keys=[] | has_overrides=False
[CRITICAL] [TIMING_DEBUG][GBPUSD] overrides = keys=[]
[CRITICAL] [TIMING_DEBUG][GBPUSD] scalping_overrides = keys=[]
[CRITICAL] [TIMING_DEBUG][GBPUSD] timing_config_asset = {}
[CRITICAL] [TIMING_CONFIG_FINAL][GBPUSD] allowed_hours_gmt=[0, 1, ..., 11, ..., 16] | min_tick_rate=1.0 | source=GLOBAL
```

**Après fix**:
```
[CRITICAL] [TIMING_DEBUG][GBPUSD] asset_config reçue = type=dict | keys=['symbol', 'description', 'overrides'] | has_overrides=True
[CRITICAL] [TIMING_DEBUG][GBPUSD] overrides = keys=['scalping']
[CRITICAL] [TIMING_DEBUG][GBPUSD] scalping_overrides = keys=['entry_rules', 'closure_rules', 'sltp', 'timing_gatekeeper']
[CRITICAL] [TIMING_DEBUG][GBPUSD] timing_config_asset = {'enabled': True, 'allowed_hours_gmt': [7, 8, 9, 13, 14, 15], 'min_tick_rate': 2.5, ...}
[CRITICAL] [TIMING_CONFIG_FINAL][GBPUSD] allowed_hours_gmt=[7, 8, 9, 13, 14, 15] | min_tick_rate=2.5 | source=ASSET
```

**Pour EURUSD**:
```
[CRITICAL] [TIMING_CONFIG_FINAL][EURUSD] allowed_hours_gmt=[7, 8, 9, 10, 13, 14, 15, 16] | min_tick_rate=2.0 | source=ASSET
```

**Pour USDJPY**:
```
[CRITICAL] [TIMING_CONFIG_FINAL][USDJPY] allowed_hours_gmt=[0, 1, 2, ..., 11, ..., 16] | min_tick_rate=0.5 | source=ASSET
```

---

## ✅ STATUT

**Bugs corrigés**: ✅ **2/2**

### Corrections appliquées

1. ✅ **run_bot.py** (3 occurrences): `get_asset_config()` → `load_asset_config()`
   - Ligne 1452: Thread principal
   - Ligne 3290: Config merge asset overrides
   - Ligne 3455: Thread worker scalping

2. ✅ **timing_analyzer.py**: Merge granulaire config ASSET + GLOBAL
   - Config globale comme base
   - Config asset écrase paramètre par paramètre
   - Logs debug détaillés pour traçage

### Résultats attendus

**Bug #1 corrigé**: GMT Hours veto maintenant appliqué correctement
- GBPUSD à 11h GMT → **VETO** ❌ (11h non dans [7,8,9,13,14,15])
- EURUSD à 11h GMT → **VETO** ❌ (11h non dans [7,8,9,10,13,14,15,16])
- USDJPY à 11h GMT → **PASS** ✅ (11h dans [0-11,14-16])

**Bug #2 corrigé**: Asset-specific tick_rate maintenant respecté
- GBPUSD tick_rate < 2.5 → **VETO** ❌
- EURUSD tick_rate < 2.0 → **VETO** ❌
- USDJPY tick_rate < 0.5 → **VETO** ❌

### Prochaine étape - VALIDATION

**À vérifier dans les logs**:

1. **Config asset chargée**:
```
[TIMING_DEBUG][GBPUSD] asset_config reçue = type=dict | keys=['symbol', 'description', 'overrides'] | has_overrides=True
```

2. **Config finale correcte**:
```
[TIMING_CONFIG_FINAL][GBPUSD] allowed_hours_gmt=[7, 8, 9, 13, 14, 15] | min_tick_rate=2.5 | source=ASSET
```

3. **VETO appliqués**:
```
[TIMING_VETO][GBPUSD] 🚫 Heure 11h GMT NON autorisée (whitelist: [7, 8, 9, 13, 14, 15])
[TIMING_VETO][GBPUSD] Tick rate trop faible (1.2 < 2.5 ticks/sec)
```

---

**Date**: 02 Janvier 2026
**Session**: Correction bugs Timing Gatekeeper
**Status**: ✅ TERMINÉ
