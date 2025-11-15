# Debug Logs - Guide d'Utilisation

## 📋 Objectif

Optimiser les interactions avec Claude en **évitant de polluer l'historique de conversation** avec des centaines de lignes de logs.

---

## 🎯 Méthode Recommandée : Fichier Temporaire

### **Fichier Principal** : `/home/workdev/sniper_x_dev/DEBUG_LOGS.txt`

### **Workflow** :

#### 1. **Capturer les logs**
```bash
# Exemple : Dernières 500 lignes du bot
tail -n 500 logs/bot.log > DEBUG_LOGS.txt

# Ou filtrer par pattern
grep "ERROR\|CRITICAL" logs/bot.log > DEBUG_LOGS.txt

# Ou logs MT5
tail -n 300 logs/mt5_connector.log > DEBUG_LOGS.txt
```

#### 2. **Demander à Claude d'analyser**
Dans la console Claude Code :
```
"Analyser DEBUG_LOGS.txt - Le bot refuse de trader malgré un signal MODERATE"
```

Ou plus court :
```
"DEBUG_LOGS.txt - Pourquoi burst_size=5 au lieu de 8 ?"
```

#### 3. **Claude lit et analyse**
Claude utilise `Read` (consomme 100-500 tokens au lieu de 15,000)

#### 4. **Nettoyer après traitement**
```bash
# Vider le fichier
> DEBUG_LOGS.txt

# Ou supprimer
rm DEBUG_LOGS.txt
```

---

## 📊 Économie de Tokens

| Méthode | Tokens Consommés | Sessions Possibles* |
|---------|------------------|---------------------|
| **Copier-coller dans console** | ~15,000 par log | ~8-10 échanges |
| **Fichier DEBUG_LOGS.txt** | ~300 par log | ~50-80 échanges |

*Basé sur limite 200k tokens par session

**Gain : 5-10x plus d'échanges par session** ✅

---

## 🗂️ Alternative : Logs Horodatés (Optionnel)

Si vous voulez **garder un historique** :

### **Structure** :
```
debug_logs/
├── 2025-11-15_session1.txt  ← Problème sizing
├── 2025-11-15_session2.txt  ← Problème SL/TP
├── 2025-11-15_session3.txt  ← Problème burst_size
└── README.md
```

### **Commandes** :
```bash
# Créer un nouveau fichier horodaté
tail -n 500 logs/bot.log > debug_logs/$(date +%Y-%m-%d_%H%M)_session.txt

# Claude analyse le dernier fichier
ls -t debug_logs/*.txt | head -1  # Affiche le plus récent

# Nettoyer les vieux logs (garder 7 jours)
find debug_logs/ -name "*.txt" -mtime +7 -delete
```

---

## 💡 Cas d'Usage Typiques

### **1. Problème de Trade**
```bash
# Capturer logs autour du problème
grep -A 20 -B 20 "FUSION.*FAST-LANE" logs/bot.log > DEBUG_LOGS.txt

# Demander à Claude
"DEBUG_LOGS.txt - Pourquoi le trade n'a pas été exécuté ?"
```

### **2. Erreur MT5**
```bash
tail -n 200 logs/mt5_connector.log > DEBUG_LOGS.txt

# Demander à Claude
"DEBUG_LOGS.txt - Erreur TRADE_RETCODE_INVALID_STOPS"
```

### **3. Comparaison Avant/Après**
```bash
# Logs avant correction
grep "SIZING" logs/bot_old.log > DEBUG_LOGS.txt

# Ajouter séparateur
echo "\n\n=== APRÈS CORRECTION ===\n\n" >> DEBUG_LOGS.txt

# Logs après correction
grep "SIZING" logs/bot.log >> DEBUG_LOGS.txt

# Demander à Claude
"DEBUG_LOGS.txt - Comparer le sizing avant/après"
```

---

## ⚡ Commandes Rapides (Alias Bash)

Ajoutez dans votre `~/.bashrc` :

```bash
# Alias pour debug rapide
alias debug-bot='tail -n 500 logs/bot.log > DEBUG_LOGS.txt && echo "✅ Logs capturés dans DEBUG_LOGS.txt"'
alias debug-mt5='tail -n 300 logs/mt5_connector.log > DEBUG_LOGS.txt && echo "✅ Logs MT5 capturés"'
alias debug-clear='> DEBUG_LOGS.txt && echo "✅ DEBUG_LOGS.txt vidé"'
alias debug-show='cat DEBUG_LOGS.txt | less'

# Puis dans le terminal :
debug-bot       # Capture logs bot
# [Analyser avec Claude]
debug-clear     # Nettoyer
```

---

## 🎯 Résultat Attendu

### **Avant** (historique pollué) :
```
User: [500 lignes de logs]
Claude: Je vois le problème ligne 347...
User: [300 lignes de logs]
Claude: Autre analyse...
→ Session saturée après 8-10 échanges ❌
```

### **Après** (historique propre) :
```
User: DEBUG_LOGS.txt - Pourquoi burst_size=5 ?
Claude: [Lit fichier] Le problème vient de run_bot.py ligne 2043...
User: DEBUG_LOGS.txt - Vérifier le fix
Claude: [Lit fichier] Confirmé, burst_size=8 maintenant ✅
→ Session continue 50-80 échanges ✅
```

---

## 📝 Notes Importantes

1. **Ne commitez JAMAIS DEBUG_LOGS.txt** dans Git (déjà dans .gitignore)
2. **Videz toujours après analyse** pour éviter confusion
3. **Utilisez des noms descriptifs** si vous gardez un historique
4. **Préférez le fichier temporaire unique** pour simplicité

---

## ✅ Fichier Créé

- `/home/workdev/sniper_x_dev/DEBUG_LOGS.txt` ← **Utilisez celui-ci**
- `/home/workdev/sniper_x_dev/debug_logs/` ← Pour historique (optionnel)

---

*Créé le 15 Novembre 2025 - Optimisation Sessions Claude Code*
