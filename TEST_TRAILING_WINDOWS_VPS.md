# 🔍 Guide de Test - Trailing Stop sur Windows VPS

## ✅ Patch Appliqué

J'ai ajouté **5 points de logs critiques** dans la fonction `update_basket_sltp_dynamically` :

1. **🔥 LOG #1** : Au DÉBUT de la fonction (confirme qu'elle est appelée)
2. **🔥 LOG #2** : Après récupération du contexte basket (confirme les données)
3. **🔥 LOG #3** : Avant l'appel à `apply_dynamic_trailing` (confirme activation)
4. **🔥 LOG #4** : Après l'appel (confirme le nouveau SL calculé)
5. **🔥 LOG #5** : RÉSULTAT FINAL (résumé complet)

Tous les logs commencent par `🔥 [DEBUG_TRAILING]` pour les repérer facilement.

---

## 📋 Instructions de Test (30 minutes)

### Étape 1 : Copier le Fichier sur Windows VPS

**Sur cette machine Ubuntu** :
```bash
# Archiver le fichier modifié
cd /home/workdev/sniper_x_dev
tar -czf sltp_debug.tar.gz trader/sltp.py

# Transférer vers Windows VPS (adapter l'adresse)
scp sltp_debug.tar.gz user@windows_vps_ip:/path/to/bot/
```

**OU** copier manuellement via RDP :
- Fichier à copier : `/home/workdev/sniper_x_dev/trader/sltp.py`
- Destination : `C:\path\to\bot\trader\sltp.py` (remplacer l'ancien)

---

### Étape 2 : Lancer le Bot avec Logs Visibles

**Sur Windows VPS**, ouvrir PowerShell et lancer :

```powershell
cd C:\path\to\bot
python run_bot.py
```

**OU si vous utilisez un fichier de logs** :
```powershell
python run_bot.py > bot_output.txt 2>&1
```

---

### Étape 3 : Observer les Logs au Démarrage

**Dès le démarrage**, vous DEVEZ voir :

```
🚀 [TRAILING_MONITOR] Thread démarré ! interval=2.0s
✅ Thread de surveillance trailing stop démarré (interval=2.0s)
🔍 [TRAILING_MONITOR] Entrée dans la boucle while...
```

**Si vous NE VOYEZ PAS ces lignes** :
- ❌ Le thread ne démarre pas → Regarder les erreurs Python au démarrage

---

### Étape 4 : Observer la Détection des Baskets

**Toutes les 2 secondes**, vous devriez voir :

```
🔄 [TRAILING_MONITOR] Début d'itération...
📊 [TRAILING_MONITOR] Positions récupérées: 8
🎯 [TRAILING_MONITOR] Baskets détectés: {'abc12345'}
```

**Si vous voyez "Aucun basket"** :
```
⏸️ [TRAILING_MONITOR] Aucun basket, attente 2s...
```
→ ❌ Problème de format commentaire MT5

**Action** : Noter le **commentaire exact** des positions MT5 ouvertes.

---

### Étape 5 : Observer l'Appel de la Fonction

**Quand un basket est détecté**, vous DEVEZ voir :

```
================================================================================
🔥 [DEBUG_TRAILING] DÉBUT update_basket_sltp_dynamically
🔥 [DEBUG_TRAILING] Basket ID: abc12345
🔥 [DEBUG_TRAILING] Reason: realtime_monitor
🔥 [DEBUG_TRAILING] Force refresh: False
================================================================================

🔥 [DEBUG_TRAILING] Contexte récupéré: True
🔥 [DEBUG_TRAILING] Symbol: XAUUSD
🔥 [DEBUG_TRAILING] Direction: BUY
🔥 [DEBUG_TRAILING] PnL pips: 35.5
🔥 [DEBUG_TRAILING] Positions: 8
```

**Si vous NE VOYEZ PAS ce bloc** :
- ❌ La fonction n'est jamais appelée
- → Vérifier que `trade_executor.update_basket_sltp_dynamically` existe (Bug #1 pas corrigé)

---

### Étape 6 : Observer le Diagnostic d'Activation

**Plus bas**, vous verrez le diagnostic :

```
🔍 [SLTP_DIAGNOSTIC] Basket abc12345:
  pnl_pips=35.50 | act_min_pips=28.00 | loss_min_pips=0.00
  perf_trigger=True | time_ok=True | phase_changed=False | price_moved=False
  force_refresh=False | should_update=True
```

**Analyse** :
- ✅ `perf_trigger=True` → Le PnL dépasse 28 pips
- ✅ `should_update=True` → L'update devrait se faire

**Si `should_update=False`** :
- ❌ Conditions pas remplies → Noter TOUTES les valeurs

---

### Étape 7 : Observer l'Appel du Trailing

**Si tout va bien**, vous verrez :

```
🔥 [DEBUG_TRAILING] MODE PROFIT activé pour ticket 123456
🔥 [DEBUG_TRAILING] PnL=35.50p >= ACTIVATION=28.00p
🔥 [DEBUG_TRAILING] Appel apply_dynamic_trailing...
🔥 [DEBUG_TRAILING] Params: activation=28.00p, min_distance=8.00p, interval=2.0s
```

**Puis le résultat** :

```
🔥 [DEBUG_TRAILING] apply_dynamic_trailing returned: 4108.27
🔥 [DEBUG_TRAILING] ✅ NOUVEAU SL CALCULÉ: 4108.27000 (ancien: 4096.00000)
```

**Si vous voyez "returned: None"** :
- ❌ `apply_dynamic_trailing` retourne None
- → Problème dans `_calculate_dynamic_trailing` (Bug #9)

---

### Étape 8 : Observer le Résultat Final

**Enfin**, vous verrez le résumé :

```
================================================================================
🔥 [DEBUG_TRAILING] RÉSULTAT FINAL
🔥 [DEBUG_TRAILING] Basket ID: abc12345
🔥 [DEBUG_TRAILING] Status: success
🔥 [DEBUG_TRAILING] Updates applied: 8
🔥 [DEBUG_TRAILING] Updates failed: 0
🔥 [DEBUG_TRAILING] PnL: 35.50 pips
🔥 [DEBUG_TRAILING]   ✅ Ticket 123456: SL=4108.27, TP=4104.00
🔥 [DEBUG_TRAILING]   ✅ Ticket 123457: SL=4108.27, TP=4104.00
... (8 lignes)
================================================================================
```

**Si `Status: skipped`** :
- ❌ Aucune modification appliquée
- → Regarder les lignes `Updates failed` pour la raison

---

## 🎯 Ce Que Je Dois Savoir

**Après le test**, envoyez-moi** :

1. **Le thread démarre-t-il ?** (OUI/NON + logs de démarrage)
2. **Les baskets sont-ils détectés ?** (OUI/NON + logs détection)
3. **La fonction est-elle appelée ?** (OUI/NON + logs DEBUG #1)
4. **Le contexte est-il récupéré ?** (OUI/NON + logs DEBUG #2 avec PnL)
5. **Le diagnostic d'activation ?** (Copier le bloc SLTP_DIAGNOSTIC)
6. **L'appel du trailing ?** (OUI/NON + logs DEBUG #3-4)
7. **Le résultat final ?** (Copier le bloc RÉSULTAT FINAL)

---

## 🚨 Scénarios Possibles

### Scénario A : Aucun Log `🔥 [DEBUG_TRAILING]`
→ **La fonction n'est jamais appelée**
- Vérifier que le thread démarre
- Vérifier que les baskets sont détectés
- Problème : Bug #1 (fonction pas bindée) OU format commentaire incorrect

### Scénario B : Logs #1 et #2, mais `should_update=False`
→ **Conditions d'activation pas remplies**
- Vérifier le PnL réel (< 28 pips ?)
- Vérifier `perf_trigger`, `time_ok`, etc.
- Problème : Config trailing incorrecte OU trades n'atteignent jamais 28 pips

### Scénario C : Logs #1-3, mais `apply_dynamic_trailing returned: None`
→ **Le trailing ne calcule pas de nouveau SL**
- Problème dans `_calculate_dynamic_trailing`
- Vérifier les logs DEBUG de cette fonction
- Problème : Bug #9 (fonction retourne None) OU throttling (< 2s)

### Scénario D : Logs #1-4 OK, mais `Status: skipped`
→ **Le SL est calculé mais pas appliqué au broker**
- Vérifier les logs `Updates failed` pour la raison
- Problème : Erreur broker MT5 (SL trop proche, requotation, etc.)

### Scénario E : TOUT OK, `Status: success` ✅
→ **LE TRAILING FONCTIONNE !** 🎉
- Vérifier dans MT5 que les SL ont bien bougé
- Observer si le trailing suit le prix

---

## ⚡ Test Rapide Manuel (Optionnel)

**Si vous voulez forcer un test** :

1. Ouvrir manuellement 1 trade XAUUSD en BUY
2. Mettre le commentaire : `bs_12345678` (exactement ce format)
3. SL à -400 pips, TP à +400 pips
4. Laisser le trade monter à +30 pips (manuellement si besoin via SL/TP temporaires)
5. Observer si le bot modifie le SL

---

## 📞 Contact

Une fois le test fait, envoyez-moi **les logs complets** (ou au minimum les 7 points ci-dessus).

Je saurai EXACTEMENT où ça bloque et on corrigera le dernier bug. 💪

**Le trailing DOIT marcher après ça.** 🚀
