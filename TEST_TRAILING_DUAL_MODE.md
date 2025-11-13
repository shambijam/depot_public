# Test Trailing Stop - Mode Dual (Thread 2s + Main Loop 60s) 🧪

## 📅 Date : 13 Novembre 2025

## 🎯 Objectif du Test

Tester **LES DEUX systèmes en parallèle** pour voir lequel fonctionne le mieux :

1. **Thread** : Vérification toutes les **2 secondes** (tag `[TRAILING_MONITOR]`)
2. **Main Loop** : Vérification toutes les **60 secondes** (tag `[TRAILING_MAINLOOP]`)

---

## ✅ Modifications Appliquées

### 1. Thread Réactivé et Fixé (main.py)

**Lignes 567-595** : Thread recréé avec message clair

```python
# 🧪 TEST: Thread trailing (2s) + Main loop trailing (60s) EN PARALLÈLE
trailing_thread = threading.Thread(
    target=trailing_stop_monitor_thread,
    args=(...),
    daemon=True,
    name="TrailingStopMonitor"
)
trailing_thread.start()
print("✅ Thread trailing (2s) + Main loop (60s) - MODE TEST")
```

### 2. Fix "too_soon" dans Thread (run_bot.py)

**Ligne 2838** : `force_refresh=True` au lieu de `False`

```python
result = trade_executor.update_basket_sltp_dynamically(
    basket_id=basket_id,
    reason="realtime_monitor",
    force_refresh=True,  # ✅ FIX: Bypass "too_soon" check
)
```

### 3. Main Loop Garde son Code (main.py)

**Lignes 636-701** : Code trailing dans main loop conservé

```python
# 🔄 TRAILING STOP - Intégré dans le main loop
result = trade_executor.update_basket_sltp_dynamically(
    basket_id=basket_id,
    reason="mainloop_cycle",
    force_refresh=True,
)
```

---

## 📊 Ce qu'on va Observer

### Logs Attendus

#### Thread (toutes les 2 secondes)
```
🔄 [TRAILING_MONITOR] Début d'itération...
📊 [TRAILING_MONITOR] Positions récupérées: 5
🎯 [TRAILING_MONITOR] Baskets détectés: {'3e24637e'}
📊 [TRAILING_MONITOR] Basket 3e24637e: 5 pos | PnL=35.00 pips ($31.50) | Seuil activation=28.00p
🔥 [DEBUG_TRAILING] DÉBUT update_basket_sltp_dynamically
🔥 [DEBUG_TRAILING] Basket ID: 3e24637e
🔥 [DEBUG_TRAILING] Reason: realtime_monitor
🔥 [DEBUG_TRAILING][PNL] basket=3e24637e | PnL=35.00 pips
✅ [TRAILING_MONITOR] Basket 3e24637e: trailing mis à jour | PnL=35.0p
```

#### Main Loop (toutes les 60 secondes)
```
[Cycle] SNIPER_X CYCLE #5 - 11:15:00
(... exécution trades ...)
📊 [TRAILING_MAINLOOP] Basket 3e24637e: 5 pos | PnL=42.00 pips ($37.80)
🔥 [DEBUG_TRAILING] DÉBUT update_basket_sltp_dynamically
🔥 [DEBUG_TRAILING] Basket ID: 3e24637e
🔥 [DEBUG_TRAILING] Reason: mainloop_cycle
🔥 [DEBUG_TRAILING][PNL] basket=3e24637e | PnL=42.00 pips
✅ [TRAILING_MAINLOOP] Basket 3e24637e: SL/TP mis à jour | PnL=42.0p
```

---

## 🔍 Questions à Répondre

### 1. Lequel s'active en premier ?

**Thread (2s)** devrait s'activer avant **Main Loop (60s)** car il check plus souvent.

**Attendu** :
- Thread détecte PnL=28 pips à t=10s → Active trailing
- Main Loop détecte PnL=35 pips à t=60s → Trailing déjà actif

### 2. Y a-t-il des conflits ?

**Possibilité** : Les deux systèmes appellent `update_basket_sltp_dynamically` sur le même basket.

**Avec force_refresh=True** : Les deux passent, pas de "too_soon".

**Comportement attendu** :
- Thread modifie SL à t=10s (PnL=28 pips)
- Thread modifie SL à t=20s (PnL=35 pips)
- Main Loop modifie SL à t=60s (PnL=40 pips)
- Pas de conflit (chacun écrase le précédent si nécessaire)

### 3. Y a-t-il des erreurs ?

**À surveiller** :
- ❌ Logs `[ERROR]` ou `[TRAILING_MONITOR/MAINLOOP] ERROR`
- ❌ Exceptions dans les logs
- ❌ Lock timeouts

### 4. Lequel est plus efficace ?

**Thread 2s** :
- ✅ Réagit vite aux mouvements
- ✅ Suit le prix de près
- ❌ Plus de CPU

**Main Loop 60s** :
- ✅ Simple, pas de threading
- ✅ Moins de CPU
- ❌ Moins réactif

---

## 🧪 Scénarios de Test

### Scénario 1 : Trade atteint +28 pips rapidement

**Timeline** :
- t=0s : Trade ouvert, PnL=0
- t=15s : PnL=+28 pips
- t=60s : Premier cycle main loop

**Attendu** :
- Thread active trailing à t=15s ✅
- Main Loop vérifie à t=60s (trailing déjà actif)

**Gagnant** : **Thread** (plus réactif)

---

### Scénario 2 : Trade oscille autour de +28 pips

**Timeline** :
- t=0s : Trade ouvert
- t=20s : PnL=+28 pips
- t=30s : PnL=+35 pips → Thread déplace SL
- t=40s : PnL=+42 pips → Thread déplace SL
- t=50s : PnL=+38 pips → Trade continue
- t=60s : PnL=+40 pips → Main loop check

**Attendu** :
- Thread suit les mouvements t=20s, t=30s, t=40s ✅
- Main Loop arrive trop tard (t=60s)

**Gagnant** : **Thread** (suit mieux le prix)

---

### Scénario 3 : Trade reste stable à +30 pips

**Timeline** :
- t=0s : Trade ouvert
- t=10s : PnL=+30 pips
- t=20s : PnL=+30 pips (stable)
- t=60s : PnL=+30 pips (stable)

**Attendu** :
- Thread active à t=10s, puis ne modifie plus (SL stable)
- Main Loop vérifie à t=60s (trailing déjà actif, rien à faire)

**Résultat** : Les deux fonctionnent, thread juste plus rapide

---

## 📋 Checklist de Test

### Avant de lancer
- [x] Thread réactivé (main.py ligne 567-595)
- [x] force_refresh=True dans thread (run_bot.py ligne 2838)
- [x] force_refresh=True dans main loop (main.py ligne 678)
- [x] Logs améliorés (tous les status loggés)

### Pendant le test (observer les logs)
- [ ] Thread démarre correctement
- [ ] Main loop trailing s'exécute à chaque cycle
- [ ] PnL calculé correctement par les deux
- [ ] Activation du trailing (au moins un des deux)
- [ ] SL/TP modifiés sur MT5
- [ ] Pas d'erreurs/exceptions

### Après le test (analyser)
- [ ] Lequel s'est activé en premier ?
- [ ] Y a-t-il eu des conflits ?
- [ ] Des erreurs/warnings ?
- [ ] Quel système est le plus efficace ?

---

## 🎯 Décision Finale (Après Test)

### Si Thread fonctionne bien
→ **GARDER uniquement le Thread (2s)**
→ Supprimer le code main loop (inutile si thread marche)

### Si Main Loop fonctionne bien
→ **GARDER uniquement le Main Loop (60s)**
→ Supprimer le thread (simplicité)

### Si les deux fonctionnent
→ **Choisir selon priorité** :
- **Réactivité** → Thread 2s
- **Simplicité** → Main Loop 60s

### Si aucun ne fonctionne
→ Analyser les logs d'erreur
→ Identifier le nouveau problème
→ Fix ciblé

---

## 📝 Commandes Utiles

### Lancer le bot
```bash
python cli.py start --mode DEMO
```

### Filtrer logs thread
```bash
grep "TRAILING_MONITOR" logs/*.log
```

### Filtrer logs main loop
```bash
grep "TRAILING_MAINLOOP" logs/*.log
```

### Voir tous les trailing logs
```bash
grep "TRAILING" logs/*.log | grep -E "(MONITOR|MAINLOOP)"
```

### Voir les activations
```bash
grep "trailing mis à jour" logs/*.log
```

### Voir les erreurs
```bash
grep -E "ERROR|SKIP|error" logs/*.log | grep TRAILING
```

---

## 💡 Notes

### Force_refresh=True

**Avant** : `force_refresh=False` → Return "skipped" (too_soon)
**Après** : `force_refresh=True` → Bypass le check, évalue toujours

**Impact** : Les deux systèmes (thread + main loop) peuvent s'exécuter sans conflit.

### Logs Améliorés

Les deux systèmes loguent maintenant **TOUS** les status :
- ✅ `success` → SL/TP modifié
- ⏭️ `skipped` → Aucune modification (raison loggée)
- ❌ `error` → Erreur (raison loggée)

### Concurrence

Les deux systèmes peuvent appeler `update_basket_sltp_dynamically` en même temps sur le même basket.

**Pas de problème** car :
1. Chaque appel recalcule le SL optimal
2. Le dernier appel écrase le précédent
3. MT5 accepte les modifications successives

---

*Test dual mode configuré le 13 Novembre 2025 - 11:30*
*Objectif : Déterminer le meilleur système de trailing stop* 🎯
