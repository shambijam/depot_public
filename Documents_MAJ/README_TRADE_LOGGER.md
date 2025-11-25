# 📝 TradeLogger - Guide de Démarrage Rapide

*Version: 1.0 - 25 Novembre 2025*

---

## 🎯 En 3 Phrases

1. **TradeLogger** enregistre **automatiquement** chaque trade avec toutes ses métriques (scores, trigger, qualité)
2. **Après 50-100 trades**, lancez `python tools/analyze_trades.py` pour obtenir des **recommandations d'optimisation**
3. **Ajustez le scoring** selon les données réelles pour améliorer continuellement la performance

---

## ⚡ Démarrage Ultra-Rapide (5 minutes)

### **Étape 1 : Intégration (Déjà Fait ✅)**

Les fichiers suivants ont été créés :
- ✅ `trader/trade_logger.py` (module de journalisation)
- ✅ `tools/analyze_trades.py` (script d'analyse)
- ✅ `INTEGRATION_TRADE_LOGGER.md` (guide détaillé)
- ✅ `SYSTEM_SCORING_DATA_DRIVEN.md` (vue d'ensemble)

### **Étape 2 : Activer la Journalisation**

**Voir le fichier `INTEGRATION_TRADE_LOGGER.md`** pour les instructions détaillées.

**Résumé** :
1. Ajouter `TradeLogger` dans `trade_executor.py` (__init__)
2. Appeler `log_trade_entry()` dans `burst.py` (après ouverture)
3. Appeler `log_trade_exit()` dans `sltp.py` (après clôture)

### **Étape 3 : Lancer le Bot**

```bash
python run_bot.py
```

Les trades seront automatiquement enregistrés dans `logs/trades_history.jsonl`

### **Étape 4 : Analyser (après 50 trades)**

```bash
python tools/analyze_trades.py --min-trades 50
```

Vous obtiendrez des rapports comme :

```
📊 PERFORMANCE PAR CATÉGORIE DE SCORE
🟢 DIAMANT    (score ≥90%)
   Trades      :   12 trades
   Win Rate    :  75.0% (9W / 3L / 0BE)
   Avg PnL     :  +28.3 pips

💡 RECOMMANDATIONS D'OPTIMISATION
✅ Le trigger AMÉLIORE significativement (+22.1%)
   → AUGMENTER le bonus trigger de +15% à +20%
```

---

## 📊 Que Fait TradeLogger ?

### **À L'ENTRÉE du Trade**
Capture automatiquement :
- ✅ Tous les scores (final, base, OF, FP)
- ✅ Trigger (type, confidence, boost)
- ✅ Qualité données (tick_count, coverage_s, status)
- ✅ Cohérence (alignement 3/3, conflits)
- ✅ Position (symbol, direction, entry, SL, TP)

### **À LA SORTIE du Trade**
Met à jour :
- ✅ Résultat (WIN/LOSS/BE)
- ✅ PnL (pips + USD)
- ✅ Durée (minutes)
- ✅ Raison sortie (trailing, TP, SL)

### **Format de Stockage**
Fichier : `logs/trades_history.jsonl`

Format : **JSON Lines** (1 trade par ligne)

Exemple :
```json
{"trade_id": "abc12345", "symbol": "XAUUSD", "direction": "BUY", "score_final": 0.82, "trigger_type": "stacking", "outcome": "WIN", "pnl_pips": 31.7}
```

---

## 🔍 Que Fait le Script d'Analyse ?

### **Rapports Générés**

#### **1. Performance par Catégorie de Score**
Valide si DIAMANT > PLATINE > OR en pratique

```
🟢 DIAMANT (≥90%) : 75.0% win rate | +28.3 pips
🟡 PLATINE (80-89%): 64.0% win rate | +18.5 pips
🟢 OR (70-79%)     : 50.0% win rate |  +2.3 pips  ← Problème !
```

→ **Recommandation** : OR trop proche breakeven, augmenter seuil à 75%

---

#### **2. Performance par Type de Trigger**
Identifie les meilleurs patterns

```
🟢 stacking : 77.8% win rate | +32.5 pips  ← Excellent !
🟡 climax   : 62.5% win rate | +15.8 pips
🔴 none     : 52.9% win rate |  +5.2 pips
```

→ **Recommandation** : Augmenter bonus STACKING de +15% à +25%

---

#### **3. Impact Qualité Données**
Valide si les pénalités sont justifiées

```
🟢 > 100 ticks : 71.9% win rate | +25.8 pips
🔴 < 50 ticks  : 37.5% win rate |  -5.2 pips  ← Pénalité justifiée
```

→ **Recommandation** : Maintenir pénalité tick_count < 50

---

#### **4. Corrélation Score vs PnL**
Vérifie que le score prédit vraiment la qualité

```
Score 90-99%: 75.0% win rate | +28.3 pips
Score 80-89%: 64.0% win rate | +18.5 pips
Score 70-79%: 50.0% win rate |  +2.3 pips
```

→ **Valide** : Plus le score est élevé, meilleure la performance

---

## 🎯 Cycle d'Amélioration

```
1. LANCER bot avec TradeLogger
   ↓ (accumulation 50-100 trades)
2. ANALYSER performances
   ↓ (identifier ajustements)
3. OPTIMISER scoring
   ↓ (modifier fusion_manager.py)
4. VALIDER amélioration
   ↓ (nouveau cycle)
   RETOUR étape 1
```

---

## 💻 Commandes Utiles

### **Analyser les Performances**

```bash
# Minimum 10 trades (aperçu rapide)
python tools/analyze_trades.py --min-trades 10

# Minimum 50 trades (analyse fiable)
python tools/analyze_trades.py --min-trades 50

# Analyse complète (par défaut 100+ trades)
python tools/analyze_trades.py
```

### **Inspecter le Fichier de Log**

```bash
# Voir les 5 derniers trades (format lisible)
tail -n 5 logs/trades_history.jsonl | jq

# Compter trades WIN/LOSS/BE
cat logs/trades_history.jsonl | jq -r '.outcome' | sort | uniq -c

# Calculer win rate global
echo "scale=2; $(cat logs/trades_history.jsonl | jq -r '.outcome' | grep -c "WIN") * 100 / $(wc -l < logs/trades_history.jsonl)" | bc
```

### **Filtrer par Critères**

```bash
# Trades DIAMANT uniquement
cat logs/trades_history.jsonl | jq 'select(.score_category == "DIAMANT")'

# Trades avec trigger STACKING
cat logs/trades_history.jsonl | jq 'select(.trigger_type == "stacking")'

# Trades WIN avec PnL > 30 pips
cat logs/trades_history.jsonl | jq 'select(.outcome == "WIN" and .pnl_pips > 30)'
```

---

## 📁 Structure des Fichiers

```
sniper_x_dev/
├─ trader/
│  └─ trade_logger.py           # Module de journalisation
├─ tools/
│  └─ analyze_trades.py         # Script d'analyse
├─ logs/
│  └─ trades_history.jsonl      # Historique trades (généré auto)
├─ INTEGRATION_TRADE_LOGGER.md  # Guide intégration détaillé
├─ SYSTEM_SCORING_DATA_DRIVEN.md # Vue d'ensemble système
└─ README_TRADE_LOGGER.md       # Ce fichier (démarrage rapide)
```

---

## ⚠️ Points d'Attention

### **1. Nombre de Trades Minimum**

- ✅ **10 trades** : Premier aperçu (non fiable)
- ✅ **50 trades** : Analyse fiable (recommandé)
- ✅ **100+ trades** : Analyse très fiable (optimal)

**Pourquoi ?** Avec 10 trades seulement, les statistiques peuvent être biaisées par quelques trades exceptionnels.

---

### **2. Attendre les Clôtures**

TradeLogger ne compte que les trades **COMPLÉTÉS** (avec outcome WIN/LOSS/BE).

Les trades **en cours** ne sont pas inclus dans l'analyse.

---

### **3. Sauvegarder l'Historique**

Le fichier `logs/trades_history.jsonl` contient toutes vos données.

**Recommandation** : Sauvegarder régulièrement (ex: backup mensuel)

```bash
# Sauvegarder avec date
cp logs/trades_history.jsonl backups/trades_$(date +%Y%m%d).jsonl
```

---

### **4. Format JSON Lines**

Chaque ligne = 1 trade (JSON valide)

**Attention** : Ne PAS éditer manuellement le fichier (risque de corruption)

---

## 🎓 Exemples de Décisions

### **Exemple 1 : Pénalités Trop Strictes**

**Données** :
```
Trades pénalisés (quality_mult < 1.0) : 23 trades
   Win Rate : 62.5%  ← Bon win rate !
```

**Décision** :
```python
# AVANT (trop strict)
if tick_count < 50:
    quality_multiplier *= 0.3  # -70%

# APRÈS (assouplir)
if tick_count < 30:
    quality_multiplier *= 0.7  # -30% seulement
```

---

### **Exemple 2 : Trigger Très Efficace**

**Données** :
```
Avec STACKING (conf ≥0.85) : 88.9% win rate (+35.7 pips)
Sans trigger               : 53.6% win rate (+5.2 pips)
Écart                      : +35.3% win rate !
```

**Décision** :
```python
# AVANT
trigger_boost = 0.15  # +15%

# APRÈS (renforcer)
trigger_boost = 0.25  # +25%
```

---

### **Exemple 3 : Catégorie Inutile**

**Données** :
```
OR (70-79%) : 18 trades | 50.0% win rate | +2.3 pips
```

**Décision** :
```python
# AVANT
if final_score >= 0.70:  # OR accepté
    return "MODERATE"

# APRÈS (plus sélectif)
if final_score >= 0.75:  # OR rejeté
    return "MODERATE"
```

---

## 🚀 Next Steps

### **Immédiat** (Aujourd'hui)
1. Lire `INTEGRATION_TRADE_LOGGER.md` pour intégration
2. Activer TradeLogger dans le code
3. Lancer le bot et vérifier `logs/trades_history.jsonl`

### **Court Terme** (1-2 semaines)
4. Accumuler 50-100 trades
5. Lancer `python tools/analyze_trades.py`
6. Analyser les recommandations

### **Moyen Terme** (1 mois)
7. Ajuster le scoring selon les données
8. Valider l'amélioration (nouveau cycle)
9. Itérer jusqu'à performance optimale

---

## 📚 Documentation Complète

Pour aller plus loin :

1. **INTEGRATION_TRADE_LOGGER.md** : Guide d'intégration détaillé (500 lignes)
2. **SYSTEM_SCORING_DATA_DRIVEN.md** : Architecture complète (400 lignes)
3. **trader/trade_logger.py** : Code source commenté (350 lignes)
4. **tools/analyze_trades.py** : Script d'analyse commenté (350 lignes)

---

## 💡 Philosophie

> **"Remplacer les hypothèses par des faits"**

Avant TradeLogger :
- ❌ Seuils arbitraires (tick_count < 50 = mauvais ?)
- ❌ Pénalités non validées (×0.3 justifié ?)
- ❌ Bonus trigger non optimisés (+15% optimal ?)

Après TradeLogger :
- ✅ Seuils validés par données réelles
- ✅ Pénalités justifiées (ou supprimées)
- ✅ Bonus optimisés selon performance

**Résultat** : Amélioration continue basée sur **faits**, pas sur **intuition**.

---

## ✅ Checklist Rapide

- [ ] Lire `INTEGRATION_TRADE_LOGGER.md`
- [ ] Ajouter `TradeLogger` dans `trade_executor.py`
- [ ] Ajouter `log_trade_entry()` dans `burst.py`
- [ ] Ajouter `log_trade_exit()` dans `sltp.py`
- [ ] Tester avec 1 trade (vérifier fichier créé)
- [ ] Accumuler 50+ trades
- [ ] Lancer `python tools/analyze_trades.py`
- [ ] Ajuster scoring selon recommandations
- [ ] Valider amélioration (nouveau cycle)

---

*Dernière mise à jour : 25 Novembre 2025*
*Auteur : Claude Code*
*Contact : Voir documentation projet*
