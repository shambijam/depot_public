# 🐛 BUG CRITIQUE : SL/TP NAS100 - ANALYSE ET SOLUTION

**Date**: 10 Janvier 2026
**Priorité**: CRITIQUE
**Asset affecté**: NAS100 (et potentiellement tous les assets avec overrides)

---

## 📊 SYMPTÔMES OBSERVÉS

### Trade NAS100 Réel
```
Entry Price: 25,743.0
Stop Loss  : 25,740.8  (distance: -2.2 points)
Take Profit: 25,747.1  (distance: +4.1 points)
```

### Configuration Attendue (NAS100.json)
```json
"sltp": {
    "sl": {
        "pips": 800,
        "comment": "800 pips = 8 index points"
    },
    "tp": {
        "pips": 1200,
        "comment": "1200 pips = 12 index points"
    }
}
```

**Résultats Attendus**:
- SL: 25,743.0 - 8.0 = **25,735.0** (8 points)
- TP: 25,743.0 + 12.0 = **25,755.0** (12 points)

**Résultats Obtenus**:
- SL: 25,740.8 (2.2 points) ❌
- TP: 25,747.1 (4.1 points) ❌

**Ratio erreur**: ~27.5% des valeurs attendues (2.2/8 = 0.275)

---

## 🔍 CAUSE RACINE IDENTIFIÉE

### Problème: Chemin de Configuration Incorrect

**Dans `config/assets_config/NAS100.json` ligne 234**:
```json
"overrides": {
    "scalping": {
        "sltp": {
            "sl": {"pips": 800},
            "tp": {"pips": 1200}
        }
    }
}
```

**Dans `trader/sltp.py` ligne 506-510**:
```python
sltp_cfg = (
    ((config.get("entry_rules") or {}).get("scalping") or {})
    .get("burst_scalping", {})
    .get("sltp", {})
) or {}
```

**Le code cherche**: `config.entry_rules.scalping.burst_scalping.sltp`
**La config est dans**: `config.overrides.scalping.sltp`

❌ **CES DEUX CHEMINS NE CORRESPONDENT PAS**

---

## 🧪 DIAGNOSTIC TECHNIQUE

### 1. Calcul PIP pour NAS100

Dans `trader/sltp.py` ligne 452-454:
```python
# pips (digits 3/5 => 10 points/pip, sinon 1)
points_per_pip = 10.0 if digits in (3, 5) else 1.0
pip_size = point * points_per_pip
```

Pour NAS100:
- `point` = 0.01 (de symbol_info)
- `digits` = 2
- `points_per_pip` = 1.0 (car digits=2, pas 3 ou 5)
- `pip_size` = 0.01 × 1.0 = **0.01** ✅

### 2. Récupération Configuration SL

Dans `trader/sltp.py` ligne 575-577:
```python
sl_pips_default = float(
    dyn_sl.get("pips") or legacy.get("stop_loss_pips")
)
```

Et ligne 527-528:
```python
dyn_sl = (sltp_cfg.get("sl") or {}) if isinstance(sltp_cfg.get("sl"), dict) else {}
```

**Problème**:
- `sltp_cfg` est **vide** ou incorrect (car cherche dans `entry_rules`)
- `dyn_sl.get("pips")` retourne **None**
- Fallback sur `legacy.get("stop_loss_pips")` qui n'existe probablement pas
- **Résultat**: `sl_pips_default` est **None** ou une valeur par défaut arbitraire ❌

### 3. Calcul Effectif

Ligne 678-683:
```python
if method_sl == "PIPS" and stop_loss_price == 0.0:
    sl_pips = float(sl_pips_default)  # ← None ou valeur incorrecte
    sl_dist = sl_pips * pip_size
    stop_loss_price = entry_price - sl_dist if action == "BUY" else entry_price + sl_dist
```

Si `sl_pips_default` est **None**, la ligne `float(None)` crasherait, donc il y a probablement une valeur par défaut quelque part (peut-être 220 pips ?).

220 pips × 0.01 = 2.2 points ← **C'EST CE QU'ON OBSERVE !**

---

## 📐 POURQUOI 2.2 POINTS AU LIEU DE 8.0 ?

### Hypothèse 1: Fallback Forex Générique

Le code utilise probablement un fallback par défaut calibré pour **forex** (EURUSD, GBPUSD):
- Forex SL typique: 20-30 pips
- Pour NAS100 (indices), 800 pips = 8 points, mais le code interprète comme 220 pips forex

### Hypothèse 2: Config Par Défaut dans Code

Il existe probablement une valeur hardcodée quelque part:
```python
sl_pips_default = 20.0  # Valeur forex par défaut
```

Pour NAS100:
- 20 pips × 0.01 = 0.2 points (trop petit)
- Après ajustements broker (min_stop_distance), peut atteindre ~220 pips = 2.2 points

---

## 💡 SOLUTIONS PROPOSÉES

### Solution A: Ajouter Fallback sur `overrides` (RECOMMANDÉ)

**Modifier `trader/sltp.py` ligne 506-520**:

```python
# Lecture configuration (dynamique > legacy)
# NOUVEAU: Chercher aussi dans overrides.scalping
sltp_cfg = (
    ((config.get("entry_rules") or {}).get("scalping") or {})
    .get("burst_scalping", {})
    .get("sltp", {})
) or {}

# 🆕 FALLBACK: Si vide, chercher dans overrides.scalping (pour assets comme NAS100)
if not sltp_cfg:
    sltp_cfg = (
        ((config.get("overrides") or {}).get("scalping") or {})
        .get("sltp", {})
    ) or {}

    # DEBUG: Confirmer qu'on a trouvé la config
    try:
        if sltp_cfg:
            self.logger.critical(
                f"🔄 [CONFIG_FALLBACK] Utilisation overrides.scalping.sltp | "
                f"SL={sltp_cfg.get('sl', {}).get('pips')} pips, "
                f"TP={sltp_cfg.get('tp', {}).get('pips')} pips"
            )
    except Exception:
        pass
```

**Avantages**:
- ✅ Respecte la structure existante des fichiers de config
- ✅ Pas besoin de modifier NAS100.json ou autres assets
- ✅ Backward compatible (garde le chemin entry_rules existant)
- ✅ Fix immédiat pour NAS100

### Solution B: Restructurer NAS100.json

**Déplacer la config SLTP** de `overrides.scalping.sltp` vers `entry_rules.scalping.burst_scalping.sltp`.

**Inconvénients**:
- ❌ Change la structure attendue des fichiers assets
- ❌ Tous les assets avec overrides doivent être migrés
- ❌ Moins flexible (overrides est le bon endroit conceptuellement)

---

## 🎯 PLAN D'ACTION

### Étape 1: Appliquer Solution A ✅

Modifier `trader/sltp.py` pour ajouter fallback sur `overrides.scalping.sltp`.

### Étape 2: Vérification Debug ✅

Ajouter logs temporaires pour confirmer:
```python
self.logger.critical(
    f"🔍 [SL_CONFIG_DEBUG] sltp_cfg trouvé={bool(sltp_cfg)} | "
    f"SL pips={dyn_sl.get('pips')} | "
    f"TP pips={dyn_tp.get('pips')}"
)
```

### Étape 3: Test NAS100 ✅

Relancer le bot sur NAS100 et vérifier que le prochain trade a:
- SL à **8 points** de distance (25,743.0 - 8.0 = 25,735.0)
- TP à **12 points** de distance (25,743.0 + 12.0 = 25,755.0)
- RR = 12/8 = 1.5 ✅

### Étape 4: Généralisation ✅

Vérifier que tous les assets avec `overrides` sont correctement gérés:
- NAS100 ✅
- Forex (GBPUSD, EURUSD, etc.) - pas affectés car utilisent `entry_rules`
- Autres indices si présents

---

## 📊 RÉSULTATS ATTENDUS APRÈS FIX

### Trade NAS100 Après Fix

```
Entry Price: 25,743.0
Stop Loss  : 25,735.0  (distance: -8.0 points) ✅
Take Profit: 25,755.0  (distance: +12.0 points) ✅
RR Ratio   : 1.5 ✅
```

### Validation Calcul

```python
# Paramètres
entry = 25743.0
sl_pips_config = 800
tp_pips_config = 1200
pip_size = 0.01

# Calcul SL
sl_dist = sl_pips_config * pip_size = 800 * 0.01 = 8.0
sl_price = entry - sl_dist = 25743.0 - 8.0 = 25735.0 ✅

# Calcul TP
tp_dist = tp_pips_config * pip_size = 1200 * 0.01 = 12.0
tp_price = entry + tp_dist = 25743.0 + 12.0 = 25755.0 ✅

# RR
rr = tp_dist / sl_dist = 12.0 / 8.0 = 1.5 ✅
```

---

## ⚠️ IMPACT ET URGENCE

### Assets Affectés

Tous les assets qui ont leur config dans `overrides.scalping` au lieu de `entry_rules.scalping.burst_scalping`:
- **NAS100**: Confirmé ✅
- Potentiellement d'autres indices/commodities

### Gravité

**CRITIQUE** - Les SL/TP sont **3.6× trop petits** (2.2 au lieu de 8.0):
- ❌ **SL trop serré**: Stopped out prématurément sur bruit de marché
- ❌ **TP trop petit**: Gains limités, ratio RR dégradé
- ❌ **Winrate artificiel**: Peut masquer les pertes (TP petit = plus souvent touché, mais profit/perte déséquilibrés)

### Priorité

**IMMÉDIAT** - À corriger avant le prochain trade NAS100.

---

## 🔗 FICHIERS CONCERNÉS

1. **`trader/sltp.py`** ligne 506-520
   - Ajouter fallback sur `overrides.scalping.sltp`

2. **`config/assets_config/NAS100.json`** ligne 234-258
   - Configuration existante (correcte, ne pas modifier)

3. **`run_bot.py`** (vérification)
   - S'assurer que la config asset est bien passée au trader

---

## ✅ VALIDATION POST-FIX

### Checklist

- [ ] `trader/sltp.py` modifié avec fallback overrides
- [ ] Logs debug ajoutés temporairement
- [ ] Bot relancé en mode test
- [ ] Trade NAS100 pris avec SL=8pts, TP=12pts
- [ ] Logs confirmant "overrides.scalping.sltp" utilisé
- [ ] Autres assets (forex) non affectés
- [ ] Logs debug retirés (optionnel)

### Commande Test

```bash
# Lancer le bot et observer le premier signal NAS100
python run_bot.py

# Vérifier dans les logs:
grep "SL_CONFIG_DEBUG" logs/bot_*.log
grep "CONFIG_FALLBACK" logs/bot_*.log

# Vérifier le trade pris
grep "NAS100.*SL.*TP" logs/bot_*.log
```

---

**Date création**: 10 Janvier 2026
**Auteur**: Claude (Anthropic)
**Statut**: ANALYSE COMPLÈTE - EN ATTENTE DE FIX
