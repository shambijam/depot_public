# 🔍 Instructions de Diagnostic - Windows VPS

## ⚡ Objectif

Identifier **EXACTEMENT** pourquoi le trailing stop ne s'active pas malgré les 9 bugs corrigés.

---

## 📋 Étape 1 : Transférer les Fichiers sur Windows VPS

### Option A : Via GitHub (RECOMMANDÉ)

**Sur votre Ubuntu** :
```bash
cd /home/workdev/sniper_x_dev

# Ajouter les nouveaux fichiers
git add diagnose_trailing_vps.py
git add DIAGNOSTIC_VPS.bat
git add INSTRUCTIONS_DIAGNOSTIC_VPS.md

# Commit
git commit -m "Add diagnostic script for trailing stop debugging"

# Push
git push origin prod
```

**Sur Windows VPS** :
```powershell
cd C:\Users\Administrateur\sniper_x_dev

# Pull les dernières modifications
git pull origin prod
```

### Option B : Via Copier-Coller (Si GitHub pose problème)

1. Ouvrir **RDP** vers Windows VPS
2. Copier ces 3 fichiers depuis Ubuntu :
   - `diagnose_trailing_vps.py`
   - `DIAGNOSTIC_VPS.bat`
   - `INSTRUCTIONS_DIAGNOSTIC_VPS.md`
3. Coller dans `C:\Users\Administrateur\sniper_x_dev\`

---

## 🚀 Étape 2 : Exécuter le Diagnostic

### Méthode Simple (Double-Click)

1. Sur Windows VPS, aller dans `C:\Users\Administrateur\sniper_x_dev\`
2. **Double-cliquer** sur `DIAGNOSTIC_VPS.bat`
3. Une fenêtre s'ouvre avec les résultats
4. **Copier TOUTE la sortie** (Ctrl+A puis Ctrl+C)
5. **Me l'envoyer** pour analyse

### Méthode PowerShell (Alternative)

```powershell
cd C:\Users\Administrateur\sniper_x_dev
python diagnose_trailing_vps.py
```

---

## 📊 Ce Que le Script Va Tester

### TEST 1 : Patch Debug Appliqué ?
✅ Vérifie que les 5 logs `🔥 [DEBUG_TRAILING]` sont dans `trader/sltp.py`

**Si FAIL** :
- Le fichier `sltp.py` n'a PAS été mis à jour
- → Re-pull depuis GitHub ou copier manuellement

### TEST 2 : Fonctions Bindées ?
✅ Vérifie que les fonctions critiques existent dans TradeExecutor :
- `update_basket_sltp_dynamically` (Bug #1)
- `_resolve_basket_context_for_sltp` (Bug #4)
- `_calculate_dynamic_trailing` (Bug #9)

**Si FAIL** :
- Un des 3 bugs n'est PAS corrigé
- → Vérifier les imports dans `trader/trade_executor.py`

### TEST 3 : Thread Démarre ?
✅ Cherche les logs `[TRAILING_MONITOR]` dans les fichiers de logs récents

**Si FAIL** :
- Le thread de surveillance NE DÉMARRE PAS
- → Problème dans `run_bot.py` ou config

### TEST 4 : Thread Défini ?
✅ Vérifie que la fonction `trailing_stop_monitor_thread()` existe dans `run_bot.py`

**Si FAIL** :
- Fichier `run_bot.py` pas à jour

---

## 🎯 Scénarios Possibles

### Scénario A : Patch Pas Appliqué
```
❌ LOG #1 MANQUANT
❌ LOG #2 MANQUANT
...
```
**Solution** : Re-pull depuis GitHub ou copier `trader/sltp.py` manuellement

---

### Scénario B : Fonctions Pas Bindées
```
✅ Patch debug appliqué: OUI
❌ TradeExecutor.update_basket_sltp_dynamically MANQUANT
```
**Solution** : Vérifier que `trader/trade_executor.py` a bien les imports et bindings

---

### Scénario C : Thread Ne Démarre Pas
```
✅ Patch debug appliqué: OUI
✅ Toutes les fonctions bindées
❌ Aucun log [TRAILING_MONITOR] trouvé
```
**Solution** : Vérifier `config/prod_config.json` :
```json
"trailing": {
  "enabled": true  // ← DOIT être true
}
```

---

### Scénario D : Thread Tourne Mais Fonction Jamais Appelée
```
✅ Patch debug appliqué: OUI
✅ Toutes les fonctions bindées
✅ Logs [TRAILING_MONITOR] trouvés: 15 lignes
❌ Aucun log 🔥 [DEBUG_TRAILING] trouvé
```
**Causes possibles** :
1. Aucun basket détecté (commentaire MT5 incorrect)
2. Format commentaire ne matche pas `bs_([a-f0-9]{8})`
3. `trade_executor.update_basket_sltp_dynamically` pas bindé

**Solution** : Vérifier le format des commentaires MT5 dans les logs `[TRAILING_MONITOR]`

---

### Scénario E : TOUT OK ✅
```
✅ Patch debug appliqué: OUI
✅ Toutes les fonctions bindées
✅ Logs [TRAILING_MONITOR] trouvés: 15 lignes
✅ Logs 🔥 [DEBUG_TRAILING] trouvés: 25 lignes
```
**Dans ce cas** : Le trailing DEVRAIT fonctionner. Analyser les logs `🔥 [DEBUG_TRAILING]` pour voir pourquoi il ne s'active pas.

---

## 📞 Envoyer les Résultats

**Une fois le diagnostic exécuté** :

1. **Copier TOUTE la sortie** du script (Ctrl+A, Ctrl+C dans la fenêtre)
2. **Me l'envoyer** dans le chat
3. Je saurai EXACTEMENT où ça bloque
4. On corrigera le problème ciblé

---

## ⚠️ Note Importante

**Ne PAS arrêter le bot** pour lancer ce diagnostic. Le script lit simplement :
- Les fichiers Python existants
- Les fichiers de logs récents

Il n'interfère PAS avec le bot en cours d'exécution.

---

## 🚨 Si Problème avec le Script

**Erreur "Python non trouvé"** :
```powershell
# Vérifier l'installation Python
python --version

# Si erreur, utiliser python3
python3 diagnose_trailing_vps.py
```

**Erreur "Module non trouvé"** :
Le script doit être exécuté depuis le **répertoire racine** du projet :
```powershell
cd C:\Users\Administrateur\sniper_x_dev
python diagnose_trailing_vps.py
```

---

## 🎯 Résultat Attendu

En **2 minutes max**, on saura :
1. ✅ Si le patch est appliqué
2. ✅ Si les fonctions sont bindées
3. ✅ Si le thread démarre
4. ✅ Si la fonction est appelée
5. ✅ Quel est LE bug qui bloque

**Après 2 jours de galère, on va ENFIN résoudre ce problème.** 💪

---

*Dernière mise à jour : 11 Novembre 2025*
