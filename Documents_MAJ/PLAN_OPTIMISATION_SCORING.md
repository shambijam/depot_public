# Plan d'Optimisation Scoring - Data-Driven

*Date de début : 25 Novembre 2025*

---

## 🎯 Stratégie : Collecte de Données puis Resserrage Progressif

### Philosophie
> "On ouvre les vannes pour collecter des données, puis on resserre progressivement tous les X jours selon les résultats réels."

---

## 📊 Phase 1 : COLLECTE DONNÉES (J0 → J14)

### Seuils Actuels (25 Nov 2025)
```json
"scoring_thresholds": {
  "high": 0.65,        // PHASE COLLECTE DONNÉES
  "moderate": 0.55,    // Objectif: 50-100 trades
  "cautious": 0.45     // Resserrage après validation
}
```

### Objectifs Phase 1
- ✅ **50-100 trades** collectés en 7-14 jours
- ✅ **Toutes les métriques** capturées (31 par trade)
- ✅ **Fichiers générés** :
  - `logs/trades_history.jsonl` (analyse automatique)
  - `logs/trades_history.md` (lecture humaine)

### Prédictions Volume
Avec ces seuils permissifs :
- **15-25 trades/jour** estimé (au lieu de 1-2)
- **100 trades** en ~5-7 jours
- **Analyse possible** dès J+7

---

## 📈 Phase 2 : PREMIÈRE ANALYSE (après 50-100 trades)

### Actions à Réaliser

**1. Lancer le script d'analyse**
```bash
python tools/analyze_trades.py --min-trades 50
```

**2. Analyser les métriques clés**
- Win Rate par catégorie de score (HIGH/MODERATE/CAUTIOUS)
- Performance par type de trigger
- Impact des pénalités qualité (tick_count, coverage_s)
- Patterns gagnants vs perdants

**3. Questions à Répondre**
- Les scores HIGH (≥65%) performent-ils mieux que MODERATE (≥55%) ?
- Les triggers améliorent-ils vraiment le win rate ?
- Les pénalités qualité sont-elles justifiées ?
- Quels sont les faux positifs (score élevé mais LOSS) ?

---

## 🔧 Phase 3 : PREMIER RESSERRAGE (J+7 à J+14)

### Décisions Basées sur les Données

**Si win rate HIGH > 60%** → Augmenter seuils
```json
"scoring_thresholds": {
  "high": 0.70,        // +5%
  "moderate": 0.60,    // +5%
  "cautious": 0.50     // +5%
}
```

**Si win rate CAUTIOUS < 45%** → Resserrer encore plus
```json
"scoring_thresholds": {
  "high": 0.75,
  "moderate": 0.65,
  "cautious": 0.55     // Supprimer catégorie CAUTIOUS
}
```

**Si trigger n'améliore pas (+)** → Ajuster bonus trigger
```python
# Dans fusion_manager.py ligne 1246-1253
if trigger_conf >= 0.85:
    trigger_boost = 0.20  # Augmenter de +15% → +20%
```

---

## 📅 Calendrier d'Optimisation

| Phase | Période | Objectif | Seuils |
|-------|---------|----------|--------|
| **Phase 1** | J0-J7 | Collecter 100 trades | 65/55/45 |
| **Analyse 1** | J7 | Première optimisation | - |
| **Phase 2** | J7-J14 | Valider ajustements | 70/60/50 (estimé) |
| **Analyse 2** | J14 | Deuxième optimisation | - |
| **Phase 3** | J14-J21 | Stabiliser système | 75/65/55 (estimé) |
| **Production** | J21+ | Système optimisé | 80/70/60 (cible finale) |

---

## 🎯 Seuils Cibles Finaux (après optimisation)

**Objectif après 200-300 trades** :
```json
"scoring_thresholds": {
  "high": 0.80,        // Win rate ≥70%
  "moderate": 0.70,    // Win rate ≥60%
  "cautious": 0.60     // Win rate ≥50%
}
```

**Critères de Validation** :
- ✅ HIGH_CONVICTION : Win rate ≥ 70%
- ✅ MODERATE : Win rate ≥ 60%
- ✅ CAUTIOUS : Win rate ≥ 50%
- ✅ Volume trades : 5-10 par jour (qualité > quantité)

---

## 📝 Log des Modifications

### 25 Novembre 2025 - Ouverture Vannes
**Seuils** : 85/75/65 → **65/55/45**
**Raison** : Phase collecte données initiale
**Objectif** : 100 trades en 7 jours
**Résultat** : ⏳ En cours...

---

### Template pour Prochaines Modifications

```markdown
### [DATE] - [DESCRIPTION]
**Seuils** : XX/YY/ZZ → **AA/BB/CC**
**Raison** : [Basé sur analyse de N trades]
**Résultats Observés** :
  - Win Rate HIGH : XX%
  - Win Rate MODERATE : YY%
  - Win Rate CAUTIOUS : ZZ%
  - Trades/jour : N
**Décision** : [Augmenter/Diminuer/Maintenir]
**Résultat** : ⏳ En validation...
```

---

## 🔍 Métriques de Suivi

### À Tracker Tous les 7 Jours

**1. Volume**
- Nombre total de trades
- Trades par jour (moyenne)
- Distribution par catégorie (HIGH/MOD/CAUT)

**2. Performance**
- Win rate global
- Win rate par catégorie de score
- PnL moyen par catégorie
- Durée moyenne par catégorie

**3. Qualité**
- Trigger présent : XX% des trades
- Win rate avec trigger vs sans trigger
- Impact tick_count sur résultats
- Impact coverage_s sur résultats

**4. Scoring**
- Distribution des scores finaux (histogramme)
- Corrélation score → outcome (WIN/LOSS)
- Faux positifs (score élevé + LOSS)
- Faux négatifs (score faible + WIN, si on les capturait)

---

## 💡 Optimisations Identifiées à Appliquer

### Après Analyse #1 (exemple)
```markdown
✅ Trigger "stacking" : Win rate 78% → AUGMENTER bonus de +15% à +20%
❌ Pénalité tick_count < 100 : Pas d'impact sur résultats → SUPPRIMER
⚠️  Catégorie CAUTIOUS : Win rate 48% (breakeven) → AUGMENTER seuil à 0.50
✅ Trigger présent : +22% win rate → CONFIRMER importance
```

---

## 🚀 Commandes Rapides

### Lancer une Analyse
```bash
# Analyse complète (minimum 50 trades)
python tools/analyze_trades.py --min-trades 50

# Analyse détaillée (tous les trades)
python tools/analyze_trades.py --min-trades 1

# Voir les derniers trades
tail -n 50 logs/trades_history.md
```

### Vérifier les Fichiers de Logs
```bash
# Nombre de trades collectés
wc -l logs/trades_history.jsonl

# Statistiques rapides
cat logs/trades_history.jsonl | jq '.outcome' | sort | uniq -c

# Trades gagnants
cat logs/trades_history.jsonl | jq 'select(.outcome=="WIN")'
```

---

## 📌 Notes Importantes

1. **Ne JAMAIS modifier les seuils sans données** : Toute modification doit être justifiée par l'analyse
2. **Patience** : Laisser au moins 50 trades avant de juger
3. **Itération** : Petits ajustements progressifs (+5% à la fois)
4. **Documentation** : Noter TOUTES les modifications dans ce fichier
5. **Comparaison** : Toujours comparer avant/après sur mêmes métriques

---

## 🎯 Success Criteria (Système Optimisé)

Le système sera considéré **optimisé** quand :

✅ **Win rate stable** : ≥60% sur 100+ trades
✅ **Volume raisonnable** : 5-15 trades/jour
✅ **Cohérence** : Scores élevés = win rate élevé (corrélation)
✅ **Reproductibilité** : Résultats similaires sur 2 périodes de 100 trades
✅ **Confiance** : Chaque paramètre justifié par données réelles

---

*Document vivant - Mis à jour après chaque analyse*
*Dernière modification : 25 Novembre 2025*
