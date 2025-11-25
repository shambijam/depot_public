# 🔄 Hot-Reload Configuration - Guide Utilisateur

## 📋 Vue d'ensemble

Le système de **hot-reload** permet de **recharger toutes les configurations sans redémarrer le bot**. Cela permet de modifier les paramètres de trading en temps réel (SL/TP, burst_size, trailing stop, etc.).

---

## ✅ Configs Rechargées Automatiquement

Lorsque vous déclenchez un hot-reload, le système recharge :

1. **Configuration principale** : `config/prod_config.json`
2. **Stratégies** :
   - `config/strategy/config_trade_scalping.json`
   - `config/strategy/config_trade_liquidity.json`
3. **Assets** :
   - `config/assets_config/XAUUSD.json`
   - `config/assets_config/EURUSD.json`
   - `config/assets_config/GBPUSD.json`
4. **Modules** :
   - `config/phase_observer_config.json`
   - `config/telegram_config.json`

---

## 🚀 Utilisation (VPS Linux)

### Méthode 1 : Script Automatique (RECOMMANDÉ)

```bash
# 1. Modifier vos fichiers de config
nano config/strategy/config_trade_scalping.json

# 2. Recharger sans redémarrer (détection auto du PID)
./reload_config.sh

# 3. Vérifier les logs
tail -f logs/sniper_x_main.log | grep HOT-RELOAD
```

### Méthode 2 : Commande Manuelle

```bash
# 1. Trouver le PID du bot
pgrep -f "python.*cli.py"
# Exemple de sortie : 12345

# 2. Envoyer le signal SIGUSR1
kill -SIGUSR1 12345

# 3. Vérifier les logs
tail -f logs/sniper_x_main.log | grep HOT-RELOAD
```

---

## 🪟 Utilisation (Windows)

**⚠️ LIMITATION WINDOWS** : Windows ne supporte pas les signaux UNIX (SIGUSR1).

### Solution Actuelle : Redémarrage Rapide

```powershell
# 1. Modifier vos fichiers de config
notepad config\strategy\config_trade_scalping.json

# 2. Arrêter le bot
Ctrl+C dans la console du bot

# 3. Relancer immédiatement
python cli.py start --mode DEMO
```

**Temps d'arrêt estimé** : ~5-10 secondes (acceptable pour les modifications hors heures de marché).

---

## 📊 Logs Attendus

Lors d'un hot-reload réussi, vous verrez :

```
================================================================================
🔄 [HOT-RELOAD] Signal de rechargement reçu !
================================================================================
🔄 [HOT-RELOAD] Début du rechargement de toutes les configurations...
🔄 [HOT-RELOAD] Cache assets vidé (3 entrées)
🔄 [HOT-RELOAD] prod_config.json rechargé
🔄 [HOT-RELOAD] phase_observer_config.json rechargé et fusionné
🔄 [HOT-RELOAD] telegram_config.json rechargé et fusionné
🔄 [HOT-RELOAD] Stratégies rechargées via StrategyManager
✅ [HOT-RELOAD] Rechargement terminé avec succès !
================================================================================
✅ [HOT-RELOAD] Configuration rechargée avec succès
💡 [HOT-RELOAD] Les prochains cycles utiliseront la nouvelle config
================================================================================
```

---

## 🎯 Cas d'Usage Typiques

### Exemple 1 : Modifier le SL/TP

```bash
# 1. Éditer la config scalping
nano config/strategy/config_trade_scalping.json

# Changer :
# "pips": 300  →  "pips": 400

# 2. Recharger
./reload_config.sh

# 3. Résultat : Les prochains trades utiliseront SL/TP 400 pips
```

### Exemple 2 : Modifier le burst_size

```bash
# 1. Éditer la config XAUUSD
nano config/assets_config/XAUUSD.json

# Changer :
# "burst_size": 8  →  "burst_size": 5

# 2. Recharger
./reload_config.sh

# 3. Résultat : Les prochains trades XAUUSD utiliseront 5 positions
```

### Exemple 3 : Activer/Désactiver closure_rules

```bash
# 1. Éditer la config scalping
nano config/strategy/config_trade_scalping.json

# Changer :
# "closure_rules": { "enabled": false  →  "enabled": true }

# 2. Recharger
./reload_config.sh

# 3. Résultat : monitor_burst_baskets commence à surveiller les fermetures auto
```

---

## ⚠️ Points d'Attention

### Ce qui est rechargé ✅
- Tous les fichiers JSON de configuration
- Paramètres de stratégie (SL/TP, burst_size, etc.)
- Seuils de fusion (weights, confidence)
- Closure rules (activation, target_profit_pips)

### Ce qui n'est PAS rechargé ❌
- **Positions déjà ouvertes** : Les trades en cours conservent leur SL/TP d'origine
- **Code Python** : Les modifications dans les fichiers .py nécessitent un redémarrage complet
- **Connexions MT5** : La connexion reste active

### Timing Recommandé
- ✅ **Idéal** : En dehors des heures de trading (marché fermé)
- ✅ **Acceptable** : Entre deux cycles (pas de position ouverte)
- ⚠️ **Risqué** : Pendant qu'un trade est en cours

---

## 🔧 Dépannage

### Problème : "Impossible de trouver le processus du bot"

```bash
# Vérifier que le bot est bien lancé
ps aux | grep python | grep cli.py

# Si absent, lancer le bot
python cli.py start --mode DEMO
```

### Problème : "Permission denied"

```bash
# Rendre le script exécutable
chmod +x reload_config.sh
```

### Problème : "Aucun log de rechargement"

```bash
# Vérifier le PID utilisé
pgrep -f "python.*cli.py"

# Vérifier les logs complets
tail -n 100 logs/sniper_x_main.log
```

---

## 📈 Améliorations Futures Possibles

1. **Option B : File Watcher** automatique (détection changements JSON)
2. **Option C : Rechargement cyclique** (toutes les 30s)
3. **API REST** : Endpoint HTTP pour recharger (`POST /reload`)
4. **Interface Web** : Bouton "Reload Config" dans un dashboard

Pour l'instant, le système actuel (signal SIGUSR1) est **optimal** pour un VPS Linux.

---

## 📝 Notes Techniques

### Implémentation
- **Handler** : `run_bot.py` ligne 3109-3144
- **Méthode reload** : `core/config_manager.py` ligne 178-239
- **Signal** : SIGUSR1 (Linux) / SIGBREAK (Windows - non implémenté)

### Performance
- **Overhead** : 0% CPU (pas de polling)
- **Durée rechargement** : ~50-200ms (dépend du nombre de configs)
- **Thread-safe** : Oui (Python GIL)

---

**Dernière mise à jour** : 18 Novembre 2025
**Version** : 1.0.0
