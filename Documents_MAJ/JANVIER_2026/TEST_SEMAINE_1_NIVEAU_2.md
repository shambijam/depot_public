# 📊 TEST SEMAINE 1 - NIVEAU 2 (Trio Delta/Volume/Imbalance)

**Date de début** : 07 Janvier 2026
**Mode actif** : `niveau_2`
**Durée prévue** : 5-7 jours de trading
**Statut** : 🟢 EN COURS

---

## 🎯 OBJECTIFS DE CETTE SEMAINE

1. ✅ Établir la **baseline de performance** (référence pour comparaisons futures)
2. ✅ Mesurer la **fréquence brute** des signaux (combien de trades/jour)
3. ✅ Identifier les **patterns de trades perdants** (pourquoi les losses?)
4. ✅ Valider que le système fonctionne correctement

---

## ⚙️ CONFIGURATION ACTIVE

### Mode Niveau 2 - Critères d'Entry

**Critères GLOBAUX** (pour tous les assets) :
```
✅ Delta Momentum Score ≥ seuil asset
✅ Volume Confirmation Score ≥ seuil asset
✅ Imbalance Strength Score ≥ 5.0
✅ Composite Score ≥ seuil asset
✅ Timing Gatekeeper = PASS (toujours actif)
```

### Seuils par Asset

| Asset | Delta Min | Volume Min | Imbalance Min | Composite Min |
|-------|-----------|------------|---------------|---------------|
| **EURUSD** | 20.0 | 10.0 | 5.0 | 65.0 |
| **GBPUSD** | 22.0 | 10.0 | 5.0 | 70.0 |
| **USDJPY** | 15.0 | 8.0 | 5.0 | 62.0 |

### Exit Strategy

- **Target** : +3.0 pips (sortie rapide pour burst classique)
- **Stop Loss** : Selon asset config (15-20 pips)
- **Trailing** : Désactivé pour niveau 2 (sortie fixe)

---

## 📈 PRÉDICTIONS (À VÉRIFIER)

| Métrique | Prédiction Niveau 2 |
|----------|---------------------|
| **Trades/jour** | 8-15 (EURUSD), 6-12 (GBPUSD), 10-18 (USDJPY) |
| **Win Rate** | 55-60% |
| **Avg Win** | 3-4 pips |
| **Avg Loss** | -15 à -20 pips |
| **Profit Factor** | 1.2-1.5 |
| **Expectancy** | +0.5 à +1.0 pips par trade |

---

## 📝 DONNÉES À COLLECTER QUOTIDIENNEMENT

### Chaque Jour (Matin et Soir)

#### A. Comptage de Trades
```
Date: ___ / ___ / 2026
├─ EURUSD: ___ trades (Wins: ___, Losses: ___)
├─ GBPUSD: ___ trades (Wins: ___, Losses: ___)
└─ USDJPY: ___ trades (Wins: ___, Losses: ___)

Total jour: ___ trades | Win Rate: ___%
```

#### B. Observations Importantes

**Trades Gagnants** :
- Quel asset a le plus de wins?
- À quelles heures GMT les meilleurs trades?
- Scores typiques (Delta, Volume, Composite)?

**Trades Perdants** :
- Pourquoi le loss? (reversal rapide? pas de follow-through?)
- Y avait-il des signaux institutionnels manquants?
- Market Fatigue était EXHAUSTED/FATIGUED?

**Signaux Rejetés** (dans les logs) :
- Combien rejetés par Delta < min?
- Combien par Volume < min?
- Combien par Composite < min?

---

## 🔍 CHECKLIST DE MONITORING

### Avant de Lancer (MAINTENANT)

- [x] Config `active_burst_mode` = "niveau_2" ✅
- [x] Timing Gatekeeper ENABLED ✅
- [x] Blocked Phases ENABLED ✅
- [ ] Vérifier solde compte et position size
- [ ] Nettoyer les logs anciens (optionnel)
- [ ] Sauvegarder config actuelle (backup)
- [ ] **REDÉMARRER LE BOT** ← ACTION REQUISE

### Pendant la Semaine (Chaque Jour)

- [ ] Vérifier logs le matin (8h-9h GMT)
- [ ] Vérifier logs le soir (18h-19h GMT)
- [ ] Noter trades exceptionnels (screenshot si possible)
- [ ] Documenter conditions de marché (news, volatilité)
- [ ] Remplir tableau quotidien ci-dessus

### Fin de Semaine (Vendredi Soir ou Samedi)

- [ ] Exporter tous les trades en CSV depuis MT5
- [ ] Calculer tous les KPIs (voir section ci-dessous)
- [ ] Analyser les rejets (logs)
- [ ] Comparer avec prédictions
- [ ] Remplir la section "RÉSULTATS FINAUX" ci-dessous
- [ ] Décider : passer au Niveau 2.5 ou réitérer Niveau 2?

---

## 📊 KPIs À CALCULER (FIN DE SEMAINE)

### Formules

```python
# Win Rate
win_rate = (nombre_wins / total_trades) * 100

# Average Win/Loss
avg_win = sum(tous_wins_pips) / nombre_wins
avg_loss = sum(tous_losses_pips) / nombre_losses  # Négatif

# Profit Factor
total_wins_pips = sum(tous_wins_pips)
total_losses_pips = abs(sum(tous_losses_pips))
profit_factor = total_wins_pips / total_losses_pips

# Expectancy (pips par trade)
expectancy = (win_rate/100 * avg_win) + ((1-win_rate/100) * avg_loss)

# Net Pips
net_pips = total_wins_pips - total_losses_pips

# Max Drawdown
max_drawdown = pire_série_de_losses_consécutives
```

---

## 📋 RÉSULTATS FINAUX (À REMPLIR FIN SEMAINE)

### Jour par Jour

#### Lundi __ Janvier
```
Trades: ___
Wins: ___ | Losses: ___
Win Rate: ___%
Net Pips: ___
Best Trade: ___ pips (asset: ___, heure: ___)
Worst Trade: ___ pips (asset: ___, heure: ___)
Notes: ___________
```

#### Mardi __ Janvier
```
[Même format]
```

#### Mercredi __ Janvier
```
[Même format]
```

#### Jeudi __ Janvier
```
[Même format]
```

#### Vendredi __ Janvier
```
[Même format]
```

---

### RÉSULTATS HEBDOMADAIRES

**Total Semaine 1 (Niveau 2)** :

| Métrique | Résultat | Prédiction | Écart |
|----------|----------|------------|-------|
| **Total Trades** | ___ | 40-80 | ___ |
| **Win Rate** | ___% | 55-60% | ___% |
| **Avg Win** | ___ pips | 3-4 pips | ___ |
| **Avg Loss** | ___ pips | -15 à -20 | ___ |
| **Profit Factor** | ___ | 1.2-1.5 | ___ |
| **Expectancy** | ___ pips | +0.5 à +1.0 | ___ |
| **Net Pips** | ___ | +20 à +50 | ___ |
| **Max Drawdown** | ___ pips | -30 à -50 | ___ |

**Par Asset** :

| Asset | Trades | Win Rate | Net Pips | Notes |
|-------|--------|----------|----------|-------|
| EURUSD | ___ | ___% | ___ | _________ |
| GBPUSD | ___ | ___% | ___ | _________ |
| USDJPY | ___ | ___% | ___ | _________ |

**Par Session** :

| Session | Heures GMT | Trades | Win Rate | Notes |
|---------|------------|--------|----------|-------|
| Tokyo | 0h-3h | ___ | ___% | _________ |
| Londres | 7h-10h | ___ | ___% | _________ |
| Londres/NY | 13h-18h | ___ | ___% | _________ |

---

## 🔍 ANALYSE DES REJETS (depuis logs)

**Total Signaux Détectés** : ___
**Total Signaux Tradés** : ___
**Total Signaux Rejetés** : ___

**Raisons de Rejet** (estimer %) :

```
Delta < min        : ___ rejets (___%)
Volume < min       : ___ rejets (___%)
Imbalance < min    : ___ rejets (___%)
Composite < min    : ___ rejets (___%)
Timing Gatekeeper  : ___ rejets (___%)
Blocked Phase      : ___ rejets (___%)
Spread trop large  : ___ rejets (___%)
Autre              : ___ rejets (___%)
```

**Question clé** : Les signaux rejetés auraient-ils été gagnants?
- Échantillonner 5-10 rejets et vérifier a posteriori
- Si beaucoup auraient été gagnants → Seuils trop stricts
- Si beaucoup auraient été perdants → Seuils bien calibrés

---

## 💡 OBSERVATIONS & INSIGHTS

### Points Positifs
```
1. ___________
2. ___________
3. ___________
```

### Points Négatifs
```
1. ___________
2. ___________
3. ___________
```

### Patterns Identifiés
```
Trades Gagnants:
- ___________

Trades Perdants:
- ___________
```

### Améliorations Possibles
```
1. ___________
2. ___________
3. ___________
```

---

## 🎯 DÉCISION POUR SEMAINE 2

### Option A : Passer au Niveau 2.5 ✅
**Conditions** :
- Win Rate ≥ 50%
- Profit Factor > 1.0
- Système fonctionne comme prévu

**Action** : Activer `"active_burst_mode": "niveau_2_5"` et documenter Semaine 2

### Option B : Réitérer Niveau 2 ⚠️
**Conditions** :
- Win Rate < 50%
- Profit Factor < 1.0
- Anomalies ou bugs détectés

**Action** : Analyser problèmes, ajuster seuils, retester Niveau 2

### Option C : Retour en Arrière ❌
**Conditions** :
- Win Rate < 40%
- Perte nette significative
- Système instable

**Action** : Revenir à configuration précédente, revoir stratégie

---

## 📞 SUPPORT & AIDE

**En cas de problème** :

1. **Bot ne trade pas** :
   - Vérifier Timing Gatekeeper (heures GMT)
   - Vérifier logs : raisons de rejet?
   - Vérifier spread (< 1.5 pips pour EURUSD?)

2. **Trop de trades perdants** :
   - Vérifier Market Fatigue dans logs (EXHAUSTED?)
   - Vérifier si trades contre régime dominant
   - Considérer augmenter composite_min de +2 pts

3. **Pas assez de trades** :
   - Vérifier que Timing Gatekeeper est en heures actives
   - Vérifier logs : seuils trop stricts?
   - Considérer réduire delta_min de -2 pts

---

## 📌 NOTES IMPORTANTES

⚠️ **SANS modification de code**, le système utilisera les seuils existants dans `of_v6_gate` et `decision`. Les critères exacts du Niveau 2 (Delta ≥20, Volume ≥10, Imbalance ≥5) ne sont **pas garantis d'être appliqués strictement**.

Ce test sert surtout à :
1. Valider que le système fonctionne
2. Établir une baseline de référence
3. Observer le comportement réel vs prédictions

Pour une application **exacte** des critères Niveau 2, il faudrait l'implémentation complète (Option 2).

---

## ✅ CHECKLIST DE DÉMARRAGE IMMÉDIAT

- [x] Config modifiée (`active_burst_mode` = "niveau_2") ✅
- [x] Document de suivi créé (ce fichier) ✅
- [ ] **REDÉMARRER LE BOT** ← **ACTION REQUISE MAINTENANT**
- [ ] Vérifier premier trade dans les logs
- [ ] Confirmer que scoring fonctionne
- [ ] Commencer à documenter les résultats

---

**Bonne chance pour la Semaine 1 !** 🚀

Rendez-vous dans 5-7 jours pour analyser les résultats et passer au Niveau 2.5.

---

**Document créé le** : 07 Janvier 2026
**Auteur** : Claude Sonnet 4.5
**Statut** : 🟢 ACTIF - Test en cours
