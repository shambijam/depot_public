# 🎯 RAPPORT COMPLET - Mécanismes de Fermeture des Trades

**Date**: 06 Janvier 2026
**Système**: Burst Scalping avec surveillance temps réel
**Fichier analysé**: `trader/burst.py` (fonction `monitor_burst_baskets`)

---

## ✅ RÉPONSE DIRECTE À VOTRE QUESTION

### **OUI, `target_profit_pips` ferme TOUS vos trades de TOUS les actifs**

**Localisation exacte dans le code** : `trader/burst.py` ligne **1343-1345**

```python
# ✅ FERMETURE si PnL >= target_profit_pips
# 🎯 (05 JAN 2026): Utilise asset_target_profit (spécifique par actif)
if pnl_pips >= asset_target_profit:
    # Fermeture immédiate du basket
```

**Mais ce n'est PAS le seul paramètre qui peut fermer vos trades !**

Il y a **2 MÉCANISMES de fermeture** différents :
1. ✅ **Profit Target** (`target_profit_pips`) - Ferme quand profit atteint
2. 🛡️ **Loss Guard** (`max_loss_pips`) - Ferme quand perte maximale atteinte

---

## 📊 Architecture Complète de Fermeture

### Vue d'Ensemble

```
┌────────────────────────────────────────────────────────────┐
│         FONCTION: monitor_burst_baskets()                  │
│         Fichier: trader/burst.py                           │
├────────────────────────────────────────────────────────────┤
│                                                            │
│  ┌──────────────────────────────────────────────┐         │
│  │  PHASE A: Surveillance PROFIT (Fast Loop)   │         │
│  │  Condition: enable_profit_close = true       │         │
│  │  Durée: rt_fast_window_ms (120000ms = 2min) │         │
│  │  Intervalle: rt_poll_interval_ms (100ms)    │         │
│  └──────────────────────────────────────────────┘         │
│                      │                                     │
│                      ▼                                     │
│          ┌───────────────────────┐                        │
│          │   Check toutes les    │                        │
│          │   100ms (0.1 sec)     │                        │
│          └───────────────────────┘                        │
│                      │                                     │
│          ┌───────────┴───────────┐                        │
│          │                       │                        │
│          ▼                       ▼                        │
│   ┌─────────────┐        ┌─────────────┐                │
│   │ 🛡️ LOSS     │        │ ✅ PROFIT   │                │
│   │   GUARD     │        │   TARGET    │                │
│   │             │        │             │                │
│   │ Priorité 1  │        │ Priorité 2  │                │
│   └─────────────┘        └─────────────┘                │
│          │                       │                        │
│          ▼                       ▼                        │
│   PnL <= -max_loss     PnL >= target_profit              │
│   → FERME              → FERME                            │
│                                                            │
│  ┌──────────────────────────────────────────────┐         │
│  │  PHASE B: Loss Guard UNIQUEMENT (Fallback)  │         │
│  │  Condition: enable_profit_close = false      │         │
│  │           & enable_loss_guard = true         │         │
│  │  Exécution: Une seule vérification           │         │
│  └──────────────────────────────────────────────┘         │
│                                                            │
└────────────────────────────────────────────────────────────┘
```

---

## 🎯 MÉCANISME #1: Profit Target (✅ Fermeture sur Gain)

### Configuration par Asset

| Asset | `target_profit_pips` | Commentaire |
|-------|---------------------|-------------|
| **USDJPY** | **2.1 pips** | Validé par tests réels |
| **EURUSD** | **3.0 pips** | Couvre spread + slippage + edge |
| **GBPUSD** | **3.0 pips** | Plus conservateur (volatilité) |

### Logique de Fermeture (Profit)

**Fichier**: `trader/burst.py` lignes **1271-1387**

```python
# 1. Charger config asset-specific
asset_target_profit = target_profit  # Défaut global (15.0)

# Charger depuis config/assets_config/{SYMBOL}.json
asset_config = config_mgr.config_loader.load_asset_config(sym)
asset_closure = asset_config["overrides"]["scalping"]["burst_scalping"]["closure_rules"]
asset_target_profit = float(asset_closure.get("target_profit_pips", target_profit))

# 2. Calculer PnL en temps réel
pnl_pips = (avg_price - avg_entry) / pip_size * direction_multiplier

# 3. Vérifier condition
if pnl_pips >= asset_target_profit:
    # 🎯 CONDITION REMPLIE → FERMETURE IMMÉDIATE
    logger.info(f"🎯 [PROFIT_TARGET_REACHED] {basket_id}")
    logger.info(f"   📊 PnL actuel: {pnl_pips:+.2f} pips")
    logger.info(f"   🎯 Seuil: {asset_target_profit:.2f} pips ← target_profit_pips")

    # Fermer toutes les positions du basket
    _close_basket(basket_id, positions)
```

### Paramètres de Contrôle (Profit)

**Dans `config/assets_config/{SYMBOL}.json`** :

```json
{
  "closure_rules": {
    "enabled": true,                           // ✅ Activer surveillance
    "enable_profit_close": true,               // ✅ Activer fermeture profit
    "target_profit_pips": 3.0,                 // 🎯 SEUIL PROFIT (VOTRE QUESTION)
    "require_full_count_for_profit_close": false,
    "rt_fast_window_ms": 120000,               // ⏱️ Durée surveillance (2 min)
    "rt_poll_interval_ms": 100,                // ⏱️ Fréquence check (100ms)
    "min_age_ms_for_any_close": 2000           // ⏱️ Âge min avant fermeture (2s)
  }
}
```

### Conditions de Déclenchement (Profit)

Pour qu'un trade soit fermé sur profit, **TOUTES** ces conditions doivent être vraies :

1. ✅ `closure_rules.enabled = true`
2. ✅ `enable_profit_close = true`
3. ✅ `rt_fast_window_ms > 0` (durée surveillance)
4. ✅ `rt_poll_interval_ms > 0` (fréquence check)
5. ✅ `age_ms >= min_age_ms_for_any_close` (âge minimum = 2 secondes)
6. ✅ `pnl_pips >= target_profit_pips` (profit atteint)

**Si `require_full_count_for_profit_close = true`** :
7. ✅ Nombre de positions = nombre attendu (basket complet)

---

## 🛡️ MÉCANISME #2: Loss Guard (❌ Fermeture sur Perte)

### Configuration par Asset

| Asset | `max_loss_pips` | Commentaire |
|-------|----------------|-------------|
| **USDJPY** | **15.0 pips** | Ratio profit/perte = 1:7.1 |
| **EURUSD** | **20.0 pips** | Ratio profit/perte = 1:6.7 |
| **GBPUSD** | **25.0 pips** | Ratio profit/perte = 1:5.6 |

### Logique de Fermeture (Perte)

**Fichier**: `trader/burst.py` lignes **1310-1341**

```python
# 🛡️ LOSS GUARD — Vérification PRIORITAIRE (avant profit)
# Vérifie si perte >= max_loss_pips ET âge >= loss_guard_arming_ms

# 1. Charger config asset-specific
asset_max_loss = max_loss_pips  # Défaut global (15.0)
asset_max_loss = float(asset_closure.get("max_loss_pips", max_loss_pips))

# 2. Vérifier âge minimum (arming delay)
if age_ms >= loss_guard_arming_ms:
    # 3. Vérifier condition perte
    if pnl_pips <= -asset_max_loss:
        # 🛡️ CONDITION REMPLIE → FERMETURE PROTECTION
        logger.error(f"🛡️ [LOSS_GUARD_TRIGGERED] {basket_id}")
        logger.error(f"   📊 PnL actuel: {pnl_pips:.2f} pips")
        logger.error(f"   🛡️ Seuil max perte: -{asset_max_loss:.2f} pips")

        # Fermer toutes les positions du basket
        _close_basket(basket_id, positions)
```

### Paramètres de Contrôle (Perte)

**Dans `config/assets_config/{SYMBOL}.json`** :

```json
{
  "closure_rules": {
    "enabled": true,                  // ✅ Activer surveillance
    "enable_loss_guard": true,        // 🛡️ Activer protection perte
    "max_loss_pips": 20.0,            // 🛡️ SEUIL PERTE MAX
    "loss_guard_arming_ms": 3000      // ⏱️ Délai activation (3s)
  }
}
```

### Conditions de Déclenchement (Perte)

Pour qu'un trade soit fermé sur perte, **TOUTES** ces conditions doivent être vraies :

1. ✅ `closure_rules.enabled = true`
2. ✅ `enable_loss_guard = true`
3. ✅ `max_loss_pips > 0`
4. ✅ `age_ms >= loss_guard_arming_ms` (délai activation = 3 secondes)
5. ✅ `pnl_pips <= -max_loss_pips` (perte maximale atteinte)

---

## ⚠️ ORDRE DE PRIORITÉ

**IMPORTANT** : Le Loss Guard a la **PRIORITÉ** sur le Profit Target !

**Dans le code** (`trader/burst.py` ligne 1310) :

```python
# 🛡️ LOSS GUARD — Vérification PRIORITAIRE (avant profit)
if enable_loss_guard and asset_max_loss > 0:
    if age_ms >= loss_guard_arming_ms:
        if pnl_pips <= -asset_max_loss:
            # Fermeture immédiate
            _close_basket(basket_id, pos)
            continue  # ⚠️ SKIP le check profit

# ✅ FERMETURE si PnL >= target_profit_pips
# (exécuté UNIQUEMENT si loss guard pas déclenché)
if pnl_pips >= asset_target_profit:
    _close_basket(basket_id, pos)
```

**Conséquence** :
- Si perte >= `max_loss_pips` → Fermeture immédiate (loss guard)
- Sinon, si profit >= `target_profit_pips` → Fermeture immédiate (profit target)

---

## 📐 Valeurs Actuelles de Configuration

### USDJPY (Actif par Défaut)

```json
{
  "closure_rules": {
    "enabled": true,
    "enable_profit_close": true,
    "target_profit_pips": 2.1,                  // 🎯 Ferme à +2.1 pips
    "require_full_count_for_profit_close": false,
    "rt_fast_window_ms": 120000,                // 2 minutes surveillance
    "rt_poll_interval_ms": 100,                 // Check toutes les 100ms
    "min_age_ms_for_any_close": 2000,           // Min 2 secondes d'âge
    "enable_loss_guard": true,
    "max_loss_pips": 15.0,                      // 🛡️ Ferme à -15 pips
    "loss_guard_arming_ms": 3000                // Actif après 3 secondes
  }
}
```

**Interprétation** :
- ✅ **Fermeture automatique à +2.1 pips** (profit)
- 🛡️ **Fermeture automatique à -15.0 pips** (perte)
- ⏱️ Surveillance active pendant **2 minutes** (120000ms)
- 🔄 Vérification **toutes les 100ms** (0.1 seconde)
- ⏱️ Trades doivent avoir au moins **2 secondes** avant fermeture
- 🛡️ Loss guard actif après **3 secondes**

### EURUSD

```json
{
  "closure_rules": {
    "target_profit_pips": 3.0,    // 🎯 +3.0 pips
    "max_loss_pips": 20.0         // 🛡️ -20.0 pips
  }
}
```

### GBPUSD

```json
{
  "closure_rules": {
    "target_profit_pips": 3.0,    // 🎯 +3.0 pips
    "max_loss_pips": 25.0         // 🛡️ -25.0 pips
  }
}
```

---

## 🔧 Comment MODIFIER les Seuils de Fermeture

### Option 1: Par Asset (Recommandé)

**Éditer** : `config/assets_config/{SYMBOL}.json`

```json
{
  "overrides": {
    "scalping": {
      "burst_scalping": {
        "closure_rules": {
          "target_profit_pips": 5.0,    // ✅ Augmenter à 5 pips
          "max_loss_pips": 30.0         // ✅ Augmenter à -30 pips
        }
      }
    }
  }
}
```

**Effet** : Changement uniquement pour cet asset.

### Option 2: Global (Tous Assets par Défaut)

**Éditer** : `config/strategy/config_trade_scalping.json`

```json
{
  "entry_rules": {
    "scalping": {
      "burst_scalping": {
        "closure_rules": {
          "target_profit_pips": 10.0,   // ✅ Nouveau défaut global
          "max_loss_pips": 50.0
        }
      }
    }
  }
}
```

**Effet** : Changement pour tous les assets (sauf overrides asset-specific).

---

## ❌ Comment DÉSACTIVER la Fermeture Automatique

### Désactiver UNIQUEMENT Profit Target

```json
{
  "closure_rules": {
    "enabled": true,
    "enable_profit_close": false,    // ❌ Désactiver fermeture profit
    "enable_loss_guard": true        // ✅ Garder protection perte
  }
}
```

**Effet** :
- ❌ Pas de fermeture automatique sur profit
- ✅ Loss guard reste actif
- Trades fermés uniquement par SL/TP ou loss guard

### Désactiver UNIQUEMENT Loss Guard

```json
{
  "closure_rules": {
    "enabled": true,
    "enable_profit_close": true,     // ✅ Garder fermeture profit
    "enable_loss_guard": false       // ❌ Désactiver protection perte
  }
}
```

**Effet** :
- ✅ Fermeture automatique sur profit
- ❌ Pas de protection perte (SL broker uniquement)

### Désactiver TOUTE Fermeture Automatique

```json
{
  "closure_rules": {
    "enabled": false                 // ❌ TOUT désactiver
  }
}
```

**Effet** :
- ❌ Pas de surveillance temps réel
- ❌ Pas de fermeture automatique
- Trades fermés uniquement par SL/TP du broker

**⚠️ DANGER** : Si `enabled = false`, vous perdez :
- La fermeture rapide sur profit (scalping inefficace)
- La protection loss guard (risque de grosses pertes)

---

## 📊 Logs de Fermeture

### Exemple: Fermeture sur Profit

```
================================================================================
🎯 [PROFIT_TARGET_REACHED] burst_abc12345 (USDJPY BUY)
   📊 PnL actuel: +2.3 pips
   🎯 Seuil configuré: 2.1 pips [USDJPY-specific] ← target_profit_pips
   ✅ Condition remplie: 2.3 >= 2.1
   ⏱️  Âge du basket: 4.2s
   📦 Positions: 21/21
   → DÉCLENCHEMENT FERMETURE IMMÉDIATE
================================================================================
✅ [BASKET_CLOSED_SUCCESS] Basket burst_abc12345 fermé avec succès !
   💰 Profit sécurisé: +2.3 pips
   🎯 Seuil utilisé: 2.1 pips [USDJPY-specific]
   📈 Performance: 109.5% du target
================================================================================
```

### Exemple: Fermeture sur Perte (Loss Guard)

```
================================================================================
🛡️  [LOSS_GUARD_TRIGGERED] burst_def45678 (EURUSD SELL)
   📊 PnL actuel: -21.5 pips
   🛡️  Seuil max perte: -20.0 pips [EURUSD-specific]
   ❌ Condition remplie: -21.5 <= -20.0
   ⏱️  Âge du basket: 8.1s (arming: 3.0s)
   📦 Positions: 21/21
   → DÉCLENCHEMENT FERMETURE PROTECTION
================================================================================
🛡️  [LOSS_GUARD_CLOSED] Basket burst_def45678 fermé par protection perte
   💔 Perte limitée à: -21.5 pips
   🛡️  Seuil max: -20.0 pips [EURUSD-specific]
================================================================================
```

---

## 🎯 Scénarios d'Utilisation

### Scénario 1: Scalping Rapide (Actuel)

**Configuration** :
```json
{
  "target_profit_pips": 2.1,    // Petit profit
  "max_loss_pips": 15.0,        // Protection serrée
  "rt_fast_window_ms": 120000   // 2 minutes surveillance
}
```

**Comportement** :
- Fermeture rapide dès +2.1 pips
- Protection forte à -15 pips
- Adapté pour scalping haute fréquence

### Scénario 2: Scalping Relaxé

**Configuration** :
```json
{
  "target_profit_pips": 5.0,    // Plus de marge
  "max_loss_pips": 25.0,        // Plus de tolérance
  "rt_fast_window_ms": 300000   // 5 minutes surveillance
}
```

**Comportement** :
- Laisse courir jusqu'à +5 pips
- Plus de marge avant loss guard
- Moins de fermetures prématurées

### Scénario 3: Swing Trading (Désactivé)

**Configuration** :
```json
{
  "enable_profit_close": false,  // Pas de fermeture auto
  "enable_loss_guard": true,     // Seulement protection
  "max_loss_pips": 100.0         // Large stop
}
```

**Comportement** :
- Pas de fermeture sur profit
- Seulement SL/TP du broker
- Protection uniquement sur grosses pertes

---

## ⚙️ Code Source Détaillé

### Fonction Principale

**Fichier** : `trader/burst.py`
**Fonction** : `monitor_burst_baskets()`
**Lignes** : 844-1450

### Sections Clés

| Section | Lignes | Description |
|---------|--------|-------------|
| **Init Baskets** | 1216-1222 | Enregistrement baskets pour tracking |
| **Phase A: Fast Loop** | 1227-1394 | Surveillance profit + loss guard |
| **Calcul PnL** | 1265-1269 | Calcul mathématique PnL en pips |
| **Load Asset Config** | 1271-1296 | Chargement config asset-specific |
| **Loss Guard Check** | 1310-1341 | Vérification perte maximale (PRIORITÉ) |
| **Profit Check** | 1343-1387 | Vérification profit target |
| **Phase B: Loss Guard Only** | 1396-1449 | Fallback si profit_close désactivé |

---

## 🔍 Debugging

### Activer Logs Détaillés

**Dans `run_bot.py`** :

```python
import logging
logging.basicConfig(
    level=logging.DEBUG,  # Au lieu de INFO
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
```

### Logs Utiles

```
[BASKET_MONITOR] burst_abc12345 | USDJPY BUY | PnL=+1.8p (target=2.1p, max_loss=-15.0p) | ...
```

**Interprétation** :
- `PnL=+1.8p` : Profit actuel
- `target=2.1p` : Seuil fermeture profit (`target_profit_pips`)
- `max_loss=-15.0p` : Seuil fermeture perte (`max_loss_pips`)

### Vérifier Config Chargée

**Ajouter dans `burst.py` ligne 1290** :

```python
logger.info(
    f"[ASSET_CONFIG][{sym}] target_profit={asset_target_profit}p "
    f"(global={target_profit}p) | max_loss={asset_max_loss}p"
)
```

---

## ✅ CHECKLIST DE VÉRIFICATION

Avant de modifier les paramètres :

- [ ] Comprendre `target_profit_pips` (fermeture profit)
- [ ] Comprendre `max_loss_pips` (fermeture perte)
- [ ] Vérifier ordre de priorité (loss guard > profit)
- [ ] Backtest avec nouveaux seuils
- [ ] Comparer ratio profit/perte (ex: 2.1p / 15p = 1:7.1)
- [ ] Tester en démo avant live
- [ ] Monitorer logs pendant 1 journée
- [ ] Ajuster si trop de fermetures prématurées
- [ ] Documenter changements dans config

---

## 🎯 CONCLUSION

### Réponse à Votre Question

**OUI**, `target_profit_pips` est **LE paramètre principal** qui ferme vos trades sur profit.

**Mais il y en a un DEUXIÈME** : `max_loss_pips` (fermeture sur perte).

### Récapitulatif

| Paramètre | Rôle | Valeur USDJPY | Valeur EURUSD | Valeur GBPUSD |
|-----------|------|---------------|---------------|---------------|
| **`target_profit_pips`** | ✅ Ferme si profit >= seuil | **2.1 pips** | **3.0 pips** | **3.0 pips** |
| **`max_loss_pips`** | 🛡️ Ferme si perte >= seuil | **15.0 pips** | **20.0 pips** | **25.0 pips** |
| **`enable_profit_close`** | Active/désactive profit | `true` | `true` | `true` |
| **`enable_loss_guard`** | Active/désactive perte | `true` | `true` | `true` |
| **`rt_fast_window_ms`** | Durée surveillance | 120000ms (2min) | 120000ms | 120000ms |
| **`rt_poll_interval_ms`** | Fréquence check | 100ms | 100ms | 100ms |

### Pour Modifier

1. **Éditer** : `config/assets_config/{SYMBOL}.json`
2. **Section** : `overrides.scalping.burst_scalping.closure_rules`
3. **Modifier** : `target_profit_pips` et/ou `max_loss_pips`
4. **Relancer** le bot

**Exemple** :
```json
{
  "closure_rules": {
    "target_profit_pips": 5.0,    // ✅ Nouveau seuil profit
    "max_loss_pips": 30.0         // ✅ Nouveau seuil perte
  }
}
```

---

**Document généré le** : 06 Janvier 2026
**Auteur** : Analyse complète des mécanismes de fermeture
**Fichier source** : `trader/burst.py`
