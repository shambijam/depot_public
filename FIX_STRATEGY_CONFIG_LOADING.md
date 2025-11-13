# Fix Strategy Config Loading - Trailing Stop ✅

## 🐛 Problème Identifié

**Symptôme** : Trailing stop ne s'activait JAMAIS malgré PnL > +28 pips. Les logs montraient :
```
🔍 [CONFIG_SOURCE] Trailing config chargée depuis: empty_fallback | trail_cfg trouvé: False
🔥 [DEBUG_TRAILING] Params: activation=0.00p, min_distance=0.00p, interval=2.0s
🔥 [DEBUG_TRAILING] apply_dynamic_trailing returned: None
```

**ET POURTANT** :
```
📊 [TRAILING_MONITOR] Basket a49849f7: 5 pos | PnL=37.69 pips ($33.92) | Seuil activation=28.00p
```

Le trailing **devrait s'activer à +28 pips** mais **ne s'active PAS** car paramètres à 0.00 !

---

## 🔍 Root Cause

**Logs de démarrage** :
```
[WARNING] - [StrategyManager] Configuration introuvable pour la stratégie 'scalping'.
```

**Code problématique** (trader/trade_executor.py ligne 72-75) :
```python
# Config stratégie scalping (pour trailing stop)
try:
    self.strategy_config = config_manager.strategy_manager.get_strategy_config("scalping") or {}
except Exception:
    self.strategy_config = {}
```

**Problème** : `strategy_manager.get_strategy_config("scalping")` retourne `None` ou `{}`, donc `self.strategy_config` est vide !

**Conséquence** : Dans `trader/sltp.py` ligne 2263, la lecture de config échoue :
```python
if hasattr(self, 'strategy_config') and self.strategy_config:
    trail_cfg = ((((self.strategy_config or {}).get("entry_rules") or {})
                  .get("scalping") or {})
                  .get("burst_scalping") or {})
                  .get("trailing") or {}
    if trail_cfg:
        config_source = "strategy_config"
```

`self.strategy_config` est `{}` → `trail_cfg` est `{}` → `config_source = "empty_fallback"`

---

## ✅ Solution Appliquée

**Chargement DIRECT du fichier config** au lieu de passer par `strategy_manager`.

**trader/trade_executor.py (ligne 71-89)** :
```python
# Config stratégie scalping (pour trailing stop)
# Charger directement depuis le fichier car strategy_manager.get_strategy_config() ne fonctionne pas
self.strategy_config = {}
try:
    import json
    import os
    strategy_config_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "config", "strategy", "config_trade_scalping.json"
    )
    if os.path.exists(strategy_config_path):
        with open(strategy_config_path, 'r', encoding='utf-8') as f:
            self.strategy_config = json.load(f)
        self.logger.info(f"✅ Config stratégie scalping chargée depuis {strategy_config_path}")
    else:
        self.logger.warning(f"⚠️ Fichier config stratégie introuvable: {strategy_config_path}")
except Exception as e:
    self.logger.error(f"❌ Erreur chargement config stratégie: {e}")
    self.strategy_config = {}
```

**Avantages** :
- ✅ Charge DIRECTEMENT le fichier JSON (pas de dépendance à strategy_manager)
- ✅ Log clair du succès/échec de chargement
- ✅ Chemin absolu calculé dynamiquement (portable)
- ✅ Fallback à `{}` si erreur (pas de crash)

---

## 📊 Impact Attendu

### Logs AVANT Correction
```
[WARNING] - [StrategyManager] Configuration introuvable pour la stratégie 'scalping'.
🔍 [CONFIG_SOURCE] Trailing config chargée depuis: empty_fallback | trail_cfg trouvé: False
🔥 [DEBUG_TRAILING] Params: activation=0.00p, min_distance=0.00p  ❌
📊 [TRAILING_MONITOR] Basket: PnL=37.69 pips | Seuil activation=28.00p
Updates applied: 0  ❌
Updates failed: 5  ❌
```

### Logs APRÈS Correction (Attendu)
```
[INFO] - ✅ Config stratégie scalping chargée depuis /path/to/config/strategy/config_trade_scalping.json
🔍 [CONFIG_SOURCE] Trailing config chargée depuis: strategy_config | trail_cfg trouvé: True  ✅
🔥 [DEBUG_TRAILING] Params: activation=28.00p, min_distance=8.00p  ✅ CORRIGÉ !
📊 [TRAILING_MONITOR] Basket: PnL=37.69 pips | Seuil activation=28.00p
🔥 [DEBUG_TRAILING] ✅ NOUVEAU SL CALCULÉ: 4195.85 (ancien: 4192.87)
Updates applied: 5  ✅ TOUTES LES 5 POSITIONS
Updates failed: 0   ✅
```

---

## 🧪 Test de Validation

### Relancer le Bot
```bash
python cli.py start --mode DEMO
```

### Vérifier Log Démarrage
**Attendu** :
```
[INFO] - ✅ Config stratégie scalping chargée depuis C:\Users\...\config\strategy\config_trade_scalping.json
```

**Si absent** : Vérifier chemin du fichier config_trade_scalping.json

### Vérifier Config Chargée
**Chercher dans logs pendant trade** :
```bash
grep "CONFIG_SOURCE" logs/*.log
```

**AVANT correction** : `CONFIG_SOURCE] Trailing config chargée depuis: empty_fallback` ❌
**APRÈS correction** : `CONFIG_SOURCE] Trailing config chargée depuis: strategy_config` ✅

### Vérifier Params
```bash
grep "Params: activation=" logs/*.log
```

**AVANT correction** : `Params: activation=0.00p, min_distance=0.00p` ❌
**APRÈS correction** : `Params: activation=28.00p, min_distance=8.00p` ✅

### Vérifier Activation
**Avec PnL >= +28 pips** :
```bash
grep "Updates applied:" logs/*.log
```

**AVANT correction** : `Updates applied: 0` ❌
**APRÈS correction** : `Updates applied: 5` (ou 8 pour XAUUSD) ✅

---

## 🎯 Résumé

| Aspect | Avant | Après |
|--------|-------|-------|
| **Méthode chargement** | `strategy_manager.get_strategy_config()` ❌ | Lecture directe fichier JSON ✅ |
| **Config chargée** | `{}` (vide) ❌ | `config_trade_scalping.json` complet ✅ |
| **trail_cfg trouvé** | `False` ❌ | `True` ✅ |
| **ACTIVATION_PIPS** | 0.00 ❌ | 28.00 ✅ |
| **MIN_DISTANCE_PIPS** | 0.00 ❌ | 8.00 ✅ |
| **apply_trailing retourne** | None ❌ | Nouveau SL ✅ |
| **Positions traitées** | 0/5 ❌ | 5/5 (ou 8/8) ✅ |

---

## 🚨 Note Importante

**Pourquoi strategy_manager.get_strategy_config() ne fonctionne pas ?**

Le `StrategyManager` charge les **classes Python** des stratégies (LiquidityStrategy, ScalpingStrategy) mais ne semble pas exposer correctement les **configurations JSON** via `get_strategy_config()`.

**Solution temporaire** : Charger directement le fichier JSON.

**Solution future** : Corriger `StrategyManager.get_strategy_config()` pour qu'il retourne correctement la config.

---

*Fix appliqué le 12 Novembre 2025 - 19:00*
