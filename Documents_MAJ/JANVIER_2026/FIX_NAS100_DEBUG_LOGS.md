# 🔍 FIX NAS100 SL/TP - LOGS DE DEBUG ACTIVÉS

**Date**: 14 Janvier 2026
**Statut**: 🔧 **DEBUG EN COURS**
**Problème**: NAS100 utilise toujours SL=20 pips, TP=30 pips au lieu de SL=800 pips, TP=1200 pips

---

## 📋 RÉSUMÉ DU PROBLÈME

### **Symptôme**

```bash
# Log actuel (INCORRECT)
🔍 [SL_TP_CONFIG_DEBUG] Asset NAS100 | SL_pips_config=20.0 | TP_pips_config=30.0

# Attendu (CORRECT)
🔍 [SL_TP_CONFIG_DEBUG] Asset NAS100 | SL_pips_config=800.0 | TP_pips_config=1200.0
```

### **Cause Suspectée**

La config NAS100.json n'est **PAS mergée** dans la config globale avant d'être passée à `sltp.py`.

**Hypothèses** :
1. `load_asset_config()` ne trouve pas le fichier NAS100.json
2. `load_asset_config()` retourne un dict vide
3. Le merge fonctionne mais les valeurs sont écrasées par la config globale
4. La config est mergée mais pas au bon endroit dans l'arborescence

---

## 🔧 LOGS DE DEBUG AJOUTÉS

### **1. Log au Chargement de la Config Asset** (Ligne 207)

```python
logger.critical(
    f"🔍 [CONFIG_LOAD][{asset}] Asset config loaded | "
    f"has_entry_rules={has_entry_rules} | has_sltp={has_sltp} | sl_pips={sl_pips}"
)
```

**Ce log vous dira** :
- ✅ La config NAS100.json est-elle chargée ?
- ✅ Contient-elle la section `entry_rules.scalping.burst_scalping.sltp` ?
- ✅ Quelle valeur de `sl_pips` est lue (devrait être 800) ?

### **2. Log Après le Merge** (Ligne 261)

```python
logger.critical(
    f"🔍 [CONFIG_MERGE][{asset}] Après merge | "
    f"sl_pips={sl_pips_after_merge}"
)
```

**Ce log vous dira** :
- ✅ Après le merge, quelle valeur de `sl_pips` est dans `merged_config` ?
- ✅ Le merge a-t-il fonctionné correctement ?

---

## 🧪 COMMENT TESTER

### **Étape 1 : Redémarrer le Bot**

```bash
# Arrêter le bot actuel (Ctrl+C)
# Relancer
python run_bot.py
```

### **Étape 2 : Chercher les Logs de Debug**

```bash
# Dans le terminal ou dans les logs
grep "CONFIG_LOAD.*NAS100" logs/bot_*.log
grep "CONFIG_MERGE.*NAS100" logs/bot_*.log
```

### **Étape 3 : Interpréter les Résultats**

#### **Scénario A : Config non chargée**

```bash
⚠️ [CONFIG_LOAD][NAS100] Asset config VIDE ou non trouvé !
```

**Diagnostic** : Le fichier `config/assets_config/NAS100.json` n'est pas trouvé ou illisible.

**Solution** :
- Vérifier que le fichier existe : `ls -la config/assets_config/NAS100.json`
- Vérifier les permissions : `chmod 644 config/assets_config/NAS100.json`
- Vérifier le JSON est valide : `python -m json.tool config/assets_config/NAS100.json`

---

#### **Scénario B : Config chargée SANS entry_rules**

```bash
🔍 [CONFIG_LOAD][NAS100] Asset config loaded | has_entry_rules=False | has_sltp=False | sl_pips=N/A
```

**Diagnostic** : Le fichier NAS100.json est chargé mais ne contient pas la section `entry_rules`.

**Solution** : Vérifier la structure du fichier NAS100.json :
```bash
grep -A 5 '"entry_rules"' config/assets_config/NAS100.json
```

Devrait montrer :
```json
"entry_rules": {
  "scalping": {
    "burst_scalping": {
      "sltp": {
        "sl": { "pips": 800 }
```

---

#### **Scénario C : Config chargée AVEC entry_rules mais sl_pips=N/A**

```bash
🔍 [CONFIG_LOAD][NAS100] Asset config loaded | has_entry_rules=True | has_sltp=True | sl_pips=N/A
```

**Diagnostic** : La structure existe mais `sl.pips` est manquant ou null.

**Solution** : Vérifier la structure complète :
```bash
cat config/assets_config/NAS100.json | grep -A 20 '"sltp"'
```

---

#### **Scénario D : Config chargée correctement (sl_pips=800) mais merge échoue**

```bash
🔍 [CONFIG_LOAD][NAS100] Asset config loaded | has_entry_rules=True | has_sltp=True | sl_pips=800
🔍 [CONFIG_MERGE][NAS100] Après merge | sl_pips=20.0  ❌ PROBLÈME ICI
```

**Diagnostic** : La config NAS100 est chargée correctement, mais après le merge, la valeur est écrasée par la config globale.

**Solution** : Problème dans `_deep_merge_dicts()` - la config globale écrase la config asset au lieu de l'inverse.

**Fix** : Inverser l'ordre du merge (ligne 247) :
```python
# AVANT (INCORRECT)
merged_section = _deep_merge_dicts(
    merged_config.get(section, {}),  # Config globale (base)
    asset_section                     # Config asset (override)
)

# APRÈS (CORRECT) - Asset DOIT écraser global
merged_section = _deep_merge_dicts(
    asset_section,                    # Config asset (base) ← PRIORITÉ
    merged_config.get(section, {})   # Config globale (override si manquant)
)
```

**NON ATTENDEZ !** C'est l'inverse. La logique actuelle est correcte : `_deep_merge_dicts(base, override)` où `override` écrase `base`. Donc :
```python
merged_section = _deep_merge_dicts(
    merged_config.get(section, {}),  # Base = config globale
    asset_section                     # Override = config asset (ÉCRASE la globale)
)
```

Donc si ça échoue, le problème est dans `_deep_merge_dicts()` lui-même.

---

#### **Scénario E : Merge fonctionne (sl_pips=800) mais sltp.py reçoit 20.0**

```bash
🔍 [CONFIG_LOAD][NAS100] Asset config loaded | has_entry_rules=True | has_sltp=True | sl_pips=800
🔍 [CONFIG_MERGE][NAS100] Après merge | sl_pips=800  ✅ MERGE OK
🔍 [SL_TP_CONFIG_DEBUG] Asset NAS100 | SL_pips_config=20.0  ❌ PROBLÈME APRÈS
```

**Diagnostic** : Le merge fonctionne, mais la config mergée n'est PAS passée à `sltp.py`.

**Solution** : Problème dans la propagation de la config de `run_bot.py` → `order_builder.py` → `sltp.py`.

**Fix** : Vérifier comment `merged_config` est passé au `OrderBuilder` ou `BurstScalping`.

---

## 🎯 CE QUE VOUS DEVEZ FAIRE

### **Action Immédiate**

1. **Redémarrez le bot** avec la nouvelle version (logs debug activés)
2. **Copiez-collez les logs** `CONFIG_LOAD` et `CONFIG_MERGE` pour NAS100
3. **Envoyez-moi les logs** pour que je diagnostique le problème exact

### **Logs à Chercher**

```bash
# Chercher ces 2 logs critiques
grep "CONFIG_LOAD.*NAS100" DEBUG_LOGS.txt
grep "CONFIG_MERGE.*NAS100" DEBUG_LOGS.txt
```

**Format attendu** :
```
🔍 [CONFIG_LOAD][NAS100] Asset config loaded | has_entry_rules=True | has_sltp=True | sl_pips=800
🔍 [CONFIG_MERGE][NAS100] Après merge | sl_pips=800
```

Si vous voyez `sl_pips=800` dans les deux logs, mais `SL_TP_CONFIG_DEBUG` montre toujours 20.0, alors le problème est dans la propagation de la config vers `sltp.py`.

---

## 📁 FICHIERS MODIFIÉS

### `/home/workdev/sniper_x_dev/run_bot.py`

**Modifications** :
- Lignes 188-212 : Log de debug au chargement de l'asset config
- Lignes 252-264 : Log de debug après le merge

---

## ✅ PROCHAINES ÉTAPES

1. **Vous** : Redémarrez le bot
2. **Vous** : Copiez les logs `CONFIG_LOAD` et `CONFIG_MERGE` pour NAS100
3. **Moi** : Je diagnostique le problème exact selon le scénario
4. **Moi** : J'applique le fix approprié
5. **Vous** : Redémarrez et testez

---

**Créé** : 14 Janvier 2026
**Statut** : 🔧 EN ATTENTE DE VOS LOGS DEBUG
