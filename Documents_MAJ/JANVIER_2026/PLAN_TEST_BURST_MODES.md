# 📋 PLAN DE TEST - MODES BURST (Niveaux 2, 2.5, 3, 4)

**Date de création** : 07 Janvier 2026
**Objectif** : Tester progressivement les 4 modes de burst pour identifier la configuration optimale
**Durée totale** : 4 semaines (1 semaine par mode)
**Assets testés** : EURUSD, GBPUSD, USDJPY

---

## 🎯 Méthodologie de Test

### Principes Généraux

1. **Un mode à la fois** - Tester séquentiellement pour mesurer l'impact précis de chaque ajout
2. **Même période de test** - Idéalement 5-7 jours de trading par mode (du lundi au vendredi)
3. **Conditions constantes** - Garder tous les autres paramètres identiques
4. **Logging verbeux** - Capturer TOUS les signaux (acceptés ET rejetés) pour analyse
5. **Métriques standardisées** - Comparer avec les mêmes KPIs

### Activation d'un Mode

**Fichier** : `/config/strategy/config_trade_scalping.json`

```json
"burst_scalping": {
  "active_burst_mode": "niveau_2",  // ← Changer ici : niveau_2 | niveau_2_5 | niveau_3 | niveau_4
  ...
}
```

### Timing Gatekeeper

⚠️ **IMPORTANT** : Le Timing Gatekeeper reste **TOUJOURS ACTIF** pour tous les modes.

**Raison** : Ne PAS trader hors des heures optimales, même avec un signal parfait.

---

## 📊 Métriques de Performance (KPIs)

Pour chaque mode, collecter les données suivantes :

### A. Métriques de Trading
```
1. Nombre total de trades
2. Win Rate (%)
3. Profit Factor (Total Wins / Total Losses)
4. Average Win (pips)
5. Average Loss (pips)
6. Largest Win (pips)
7. Largest Loss (pips)
8. Total Net Pips
9. Maximum Drawdown (pips et %)
10. Expectancy = (Win Rate × Avg Win) - (Loss Rate × Avg Loss)
```

### B. Métriques de Fréquence
```
1. Trades par jour (moyenne)
2. Trades par session (London, NY, Asia)
3. Trades par asset (EURUSD, GBPUSD, USDJPY)
4. Temps moyen en position (secondes)
5. Délai moyen entry → TP/SL (secondes)
```

### C. Métriques de Qualité
```
1. Ratio signaux détectés / signaux tradés
2. % de trades sortis sur TP (vs SL)
3. % de trades sortis sur early exit
4. Score composite moyen des trades gagnants
5. Score composite moyen des trades perdants
6. Analyse par composant (Delta, Volume, Absorption, etc.)
```

### D. Métriques Institutionnelles (Niveaux 3 et 4)
```
1. Market Fatigue moyen au moment de l'entry
2. Price Memory score moyen
3. Absorption ratio moyen
4. Tape Speed ratio moyen
5. % trades avec bonus absorption activé
```

---

## 📅 SEMAINE 1 : Niveau 2 (Trio Delta/Volume/Imbalance)

### Configuration

**Activation** :
```json
"active_burst_mode": "niveau_2"
```

**Critères d'Entry** :
```
✅ Delta Momentum ≥ 20.0 pts (EURUSD), 22.0 (GBPUSD), 15.0 (USDJPY)
✅ Volume Confirmation ≥ 10.0 pts (EURUSD/GBPUSD), 8.0 (USDJPY)
✅ Imbalance Strength ≥ 5.0 pts
✅ Composite Score ≥ 65.0 (EURUSD), 70.0 (GBPUSD), 62.0 (USDJPY)
```

**Exit** :
- Target : **+3.0 pips** (fixe, sortie rapide)
- Stop Loss : Utilise SL asset config (15-20 pips selon asset)

### Objectifs du Test

1. **Établir la baseline** de performance
2. Mesurer la **fréquence brute** de signaux
3. Identifier les **faux bursts** (trades perdants malgré critères validés)
4. Collecter données pour comparaison avec niveaux suivants

### Prédictions

```
Trades/jour   : 8-15 (EURUSD), 6-12 (GBPUSD), 10-18 (USDJPY)
Win Rate      : 55-60%
Avg Win       : 3-4 pips
Profit Factor : 1.2-1.5
Expectancy    : +0.5 à +1.0 pips par trade
```

### Points d'Attention

⚠️ **À surveiller** :
- Trades perdants sur marchés équilibrés (volume sans structure)
- Entrées sur bursts qui s'essoufflent immédiatement
- Deltas élevés mais sans absorption institutionnelle

### Logs à Capturer

```python
# Tous les rejets avec raison
"REJECT: Delta=18.5 < 20.0 (min_delta_momentum)"
"REJECT: Volume=9.2 < 10.0 (min_volume_confirmation)"
"REJECT: Composite=63.8 < 65.0"

# Tous les trades avec breakdown des scores
"ENTRY: Delta=22.5 | Volume=12.0 | Imbalance=8.0 | Composite=68.2 → BUY"
```

---

## 📅 SEMAINE 2 : Niveau 2.5 (Hybrid Boost)

### Configuration

**Activation** :
```json
"active_burst_mode": "niveau_2_5"
```

**Critères d'Entry** :
```
✅ Delta Momentum ≥ 18.0 pts (EURUSD/GBPUSD), 12.0 (USDJPY)
✅ Volume Confirmation ≥ 10.0 pts (EURUSD/GBPUSD), 8.0 (USDJPY)
✅ Imbalance Strength ≥ 5.0 pts
✅ Composite Score ≥ 66.0 (EURUSD), 66.0 (GBPUSD), 62.0 (USDJPY)
✅ AU MOINS UN de :
   - Absorption ≥ 10.0 pts (EURUSD), 12.0 (GBPUSD), 8.0 (USDJPY)
   - Clustering ≥ 6.0 pts (EURUSD/GBPUSD), 5.0 (USDJPY)
   - Tape Speed ratio ≥ 2.0 (EURUSD), 2.5 (GBPUSD), 1.5 (USDJPY)
```

**Exit** :
- Target : **+3.5 pips** (légèrement plus que Niveau 2)
- Stop Loss : Identique asset config

### Objectifs du Test

1. Mesurer l'**impact du filtre "at least one"**
2. Quantifier la **réduction de faux signaux** vs Niveau 2
3. Identifier quel boost institutionnel (Absorption / Clustering / Tape Speed) est le plus efficace
4. Comparer Win Rate et Expectancy vs Niveau 2

### Prédictions

```
Trades/jour   : 6-12 (EURUSD), 4-10 (GBPUSD), 8-14 (USDJPY)
Win Rate      : 62-67% (+7% vs Niveau 2)
Avg Win       : 3.5-5 pips
Profit Factor : 1.5-1.8
Expectancy    : +1.0 à +1.5 pips par trade
```

### Points d'Attention

⚠️ **À surveiller** :
- Quel boost est le plus fréquent (Absorption? Clustering? Tape Speed?)
- Trades rejetés qui auraient été gagnants (over-filtering?)
- Amélioration du Win Rate vs perte de fréquence

### Analyse Spécifique

**Questions à répondre** :
1. Quel % de trades a **Absorption** comme boost?
2. Quel % a **Clustering**?
3. Quel % a **Tape Speed**?
4. Quel % a **plusieurs boosts simultanés**?

**Exemple de log** :
```
ENTRY: Delta=18.5 | Volume=10.5 | Imbalance=5.5 | Composite=67.2
       ✅ BOOST: Absorption=12.0 (>10.0) ← Burst accepté grâce à absorption

REJECT: Delta=19.0 | Volume=10.2 | Imbalance=5.0 | Composite=66.8
        ❌ NO BOOST: Absorption=8.5 (<10), Clustering=4.0 (<6), Tape=1.8 (<2.0)
```

---

## 📅 SEMAINE 3 : Niveau 3 (Absorption Boostée) ⭐ OPTIMAL

### Configuration

**Activation** :
```json
"active_burst_mode": "niveau_3"
```

**Critères d'Entry** :
```
✅ Delta Momentum ≥ 18.0 pts (EURUSD), 20.0 (GBPUSD), 12.0 (USDJPY)
✅ Volume Confirmation ≥ 10.0 pts (EURUSD/GBPUSD), 8.0 (USDJPY)
✅ Absorption Levels ≥ 10.0 pts (EURUSD), 12.0 (GBPUSD), 8.0 (USDJPY)
✅ Order Clustering ≥ 3.5 pts (EURUSD), 4.0 (GBPUSD), 3.0 (USDJPY)
✅ Composite Score ≥ 68.0 (EURUSD), 70.0 (GBPUSD), 64.0 (USDJPY)
✅ Require Absorption Bonus = true (bonus volume OU tick rate)
```

**Exit** :
- Target principal : **+4.0 pips**
- Early Exit : **+3.0 pips** si `tape_speed_ratio < 1.0`
- Stop Loss : Identique asset config

### Objectifs du Test

1. **Valider l'hypothèse** : Absorption + Clustering = meilleurs bursts institutionnels
2. Mesurer l'impact du **bonus absorption requis**
3. Tester la **sortie dynamique** (early exit si tape faiblit)
4. Comparer qualité vs fréquence vs Niveaux 2 et 2.5

### Prédictions

```
Trades/jour   : 5-10 (EURUSD), 3-8 (GBPUSD), 6-12 (USDJPY)
Win Rate      : 65-70% (+5% vs Niveau 2.5)
Avg Win       : 4-6 pips
Profit Factor : 1.8-2.2
Expectancy    : +1.5 à +2.5 pips par trade
```

### Points d'Attention

⚠️ **À surveiller** :
- **Efficacité du bonus absorption** : Les trades avec bonus volume/tick rate sont-ils vraiment meilleurs?
- **Early exit** : Combien de trades sortent à +3.0 pips au lieu de +4.0?
- **Fréquence** : Est-ce qu'on manque trop de trades vs Niveau 2.5?

### Analyse Spécifique

**Questions critiques** :
1. **Absorption avec bonus** : Win Rate de ces trades vs sans bonus?
2. **Early exit efficace?** : Combien de fois évite-t-on un reversal grâce à l'early exit?
3. **Clustering efficace?** : Les trades avec clustering ≥6 pts sont-ils meilleurs que ceux avec 3.5-6 pts?

**Exemple de breakdown** :
```
TRADE #1:
  Entry: Delta=20.5 | Volume=11.0 | Absorption=12.0 (bonus: high_volume ×1.3)
         Clustering=6.0 | Composite=70.2 → BUY
  Exit:  +4.0 pips (target atteint, tape_speed=2.1 reste élevé) ✅ WIN

TRADE #2:
  Entry: Delta=18.5 | Volume=10.5 | Absorption=10.5 (bonus: tick_rate ×1.1)
         Clustering=4.0 | Composite=68.5 → SELL
  Exit:  +3.0 pips (early exit, tape_speed chute à 0.8) ⚠️ WIN mais early

TRADE #3:
  Entry: Delta=19.0 | Volume=10.0 | Absorption=11.0 (bonus: medium_volume ×1.15)
         Clustering=5.0 | Composite=69.8 → BUY
  Exit:  -15.0 pips (SL, reversal rapide malgré bons scores) ❌ LOSS
```

---

## 📅 SEMAINE 4 : Niveau 4 (Institutional Filter)

### Configuration

**Activation** :
```json
"active_burst_mode": "niveau_4"
```

**Critères d'Entry** :
```
✅ Tous les critères du Niveau 3 (Delta, Volume, Absorption, Clustering, Composite)
✅ PLUS Filtres Institutionnels :

   Market Fatigue :
   - État ENERGETIC ou NORMAL uniquement
   - Si FATIGUED : composite_min +5 pts (pénalité)
   - Si EXHAUSTED : composite_min +10 pts (rejet quasi-certain)

   Price Memory :
   - Score ≥ 50.0 (EURUSD), 52.0 (GBPUSD), 48.0 (USDJPY)
   - fresh_ratio ≥ 0.5 (pas trop de vieux niveaux bloquants)

   Market Physics :
   - Physics bias aligné avec direction du delta
   - Inertie dans la même direction (pas de contre-tendance)
```

**Exit** :
- Target principal : **+5.0 pips**
- Early Exit : **+3.5 pips** si `tape_speed_ratio < 1.2`
- Stop Loss : Identique asset config

### Objectifs du Test

1. Mesurer l'**impact des filtres contextuels**
2. Quantifier **combien de trades sont rejetés** par chaque filtre
3. Comparer **qualité maximale** vs **fréquence minimale**
4. Déterminer si les filtres valent la **perte de fréquence**

### Prédictions

```
Trades/jour   : 3-7 (EURUSD), 2-5 (GBPUSD), 4-8 (USDJPY)
Win Rate      : 70-75% (+5% vs Niveau 3)
Avg Win       : 5-7 pips
Profit Factor : 2.0-2.5
Expectancy    : +2.0 à +3.5 pips par trade
```

### Points d'Attention

⚠️ **À surveiller** :
- **Over-filtering?** : Est-ce qu'on rejette trop de bons trades?
- **Fréquence critique** : <3 trades/jour peut être problématique pour scalping
- **Quel filtre est le plus efficace?** : Fatigue? Memory? Physics?

### Analyse Détaillée par Filtre

**Comptabiliser les rejets** :

```
Total signaux Niveau 3 validés : 45
  ├─ Rejetés par Market Fatigue (EXHAUSTED) : 8 (18%)
  ├─ Rejetés par Price Memory (<50) : 5 (11%)
  ├─ Rejetés par Physics (non aligné) : 6 (13%)
  └─ Acceptés Niveau 4 : 26 (58%)

Analyse des 8 rejetés par EXHAUSTED :
  - Auraient été gagnants : 3 (38%)
  - Auraient été perdants : 5 (62%) ← Filtre efficace!
```

**Questions critiques** :
1. **Market Fatigue** : Les trades rejetés par EXHAUSTED sont-ils vraiment plus souvent perdants?
2. **Price Memory** : Les niveaux anciens bloquent-ils vraiment les bursts?
3. **Physics Alignment** : Les trades contre la physique sont-ils perdants?

**Exemple de log** :
```
CANDIDATE TRADE:
  Niveau 3 validé: Delta=20.0 | Volume=11.0 | Absorption=12.5 | Clustering=6.0
                   Composite=72.0 ✅

  Filtres Institutionnels:
  ├─ Market Fatigue: EXHAUSTED (fatigue_score=8.2/10) ❌ REJECT
  │  → Composite requis: 68 + 10 (pénalité) = 78.0
  │  → Composite actuel: 72.0 < 78.0 → REJET
  └─ Trade non exécuté

  Résultat a posteriori: Prix a fait +2 pips puis reversal -8 pips
  ✅ FILTRE EFFICACE (aurait été LOSS)
```

---

## 📊 COMPARAISON FINALE (Fin Semaine 4)

### Tableau Récapitulatif

| Métrique | Niveau 2 | Niveau 2.5 | Niveau 3 | Niveau 4 |
|----------|----------|------------|----------|----------|
| **Trades/jour** | 8-15 | 6-12 | 5-10 | 3-7 |
| **Win Rate** | 55-60% | 62-67% | 65-70% | 70-75% |
| **Avg Win** | 3-4 pips | 3.5-5 pips | 4-6 pips | 5-7 pips |
| **Profit Factor** | 1.2-1.5 | 1.5-1.8 | 1.8-2.2 | 2.0-2.5 |
| **Expectancy** | +0.5 à +1.0 | +1.0 à +1.5 | +1.5 à +2.5 | +2.0 à +3.5 |
| **Complexité** | ⭐ Faible | ⭐⭐ Moyenne | ⭐⭐⭐ Moyenne-Haute | ⭐⭐⭐⭐ Élevée |
| **Réactivité** | ⚡⚡⚡ Très rapide | ⚡⚡⚡ Rapide | ⚡⚡ Modérée | ⚡ Sélective |

### Calcul du ROI par Mode

**Hypothèse** : Capital 10,000 EUR, 0.1 lot par trade, risque 2% par trade

```
Niveau 2:
  - 10 trades/jour × 5 jours = 50 trades/semaine
  - Win Rate 58% → 29 wins, 21 losses
  - Wins: 29 × 3.5 pips × 10 EUR/pip = +1,015 EUR
  - Losses: 21 × -15 pips × 10 EUR/pip = -3,150 EUR
  - Net: -2,135 EUR ❌ (pas rentable avec avg loss trop élevé)

Niveau 2.5:
  - 8 trades/jour × 5 jours = 40 trades/semaine
  - Win Rate 65% → 26 wins, 14 losses
  - Wins: 26 × 4.0 pips × 10 EUR/pip = +1,040 EUR
  - Losses: 14 × -15 pips × 10 EUR/pip = -2,100 EUR
  - Net: -1,060 EUR ❌ (encore négatif)

Niveau 3:
  - 7 trades/jour × 5 jours = 35 trades/semaine
  - Win Rate 68% → 24 wins, 11 losses
  - Wins: 24 × 5.0 pips × 10 EUR/pip = +1,200 EUR
  - Losses: 11 × -15 pips × 10 EUR/pip = -1,650 EUR
  - Net: -450 EUR ⚠️ (limite)

Niveau 4:
  - 5 trades/jour × 5 jours = 25 trades/semaine
  - Win Rate 73% → 18 wins, 7 losses
  - Wins: 18 × 6.0 pips × 10 EUR/pip = +1,080 EUR
  - Losses: 7 × -15 pips × 10 EUR/pip = -1,050 EUR
  - Net: +30 EUR ✅ (positif mais marginal)
```

⚠️ **ATTENTION** : Ces calculs montrent que **le SL est trop large** (-15 à -20 pips) pour du burst scalping avec targets +3-6 pips.

**Action requise** : Adapter les SL par mode :
- Niveau 2/2.5 : SL = -6 pips (RR 1:2)
- Niveau 3 : SL = -8 pips (RR 1:2)
- Niveau 4 : SL = -10 pips (RR 1:2)

### Décision Finale

**Critères de sélection** :

1. **Si priorité = FRÉQUENCE** → Choisir **Niveau 2.5**
   - Bon équilibre qualité/quantité
   - Assez de trades pour maintenir activité

2. **Si priorité = QUALITÉ** → Choisir **Niveau 3** ⭐ RECOMMANDÉ
   - Meilleur compromis expectancy/fréquence
   - Signature institutionnelle claire
   - Pas trop sélectif

3. **Si priorité = WIN RATE MAX** → Choisir **Niveau 4**
   - Mais accepter faible fréquence (3-7 trades/jour)
   - Idéal pour compte prop firm (besoin de consistency)

4. **Si tests montrent Niveau 2 suffisant** → Garder Niveau 2
   - Plus simple = moins de points de défaillance
   - Si Win Rate et Expectancy suffisants

---

## 🔧 Ajustements Post-Test

### Si Win Rate trop faible (<55%)

**Actions** :
1. Augmenter `composite_score_min` de +3 pts
2. Augmenter `delta_momentum_min` de +2 pts
3. Vérifier Timing Gatekeeper (peut-être trop permissif?)

### Si Fréquence trop faible (<3 trades/jour)

**Actions** :
1. Réduire `composite_score_min` de -2 pts
2. Réduire `absorption_levels_min` de -1 pt
3. Considérer passer au niveau inférieur

### Si SL hit trop souvent (>40%)

**Actions** :
1. Augmenter critères d'absorption (signe de conviction)
2. Activer filtres Market Fatigue (éviter marchés épuisés)
3. Revoir targets (peut-être trop ambitieux)

---

## 📝 Checklist Avant Chaque Test

### Préparation (Dimanche soir)

- [ ] Vérifier `active_burst_mode` dans config_trade_scalping.json
- [ ] Vérifier que Timing Gatekeeper est ENABLED
- [ ] Vérifier que blocked_phases est ENABLED
- [ ] Sauvegarder config précédente (backup)
- [ ] Nettoyer logs anciens
- [ ] Activer logging verbeux : `"orderflow_mode": "verbose"`
- [ ] Documenter hypothèses du test dans un fichier `TEST_NIVEAU_X_HYPOTHESES.md`

### Pendant le Test (Chaque jour)

- [ ] Vérifier logs matin et soir
- [ ] Noter trades exceptionnels (très gros win/loss)
- [ ] Capturer screenshots de setups intéressants
- [ ] Documenter conditions de marché (news, volatilité)

### Fin de Test (Vendredi soir)

- [ ] Exporter tous les trades dans CSV
- [ ] Calculer tous les KPIs
- [ ] Analyser rejets (raisons principales)
- [ ] Documenter conclusions dans `RESULTATS_NIVEAU_X.md`
- [ ] Comparer avec prédictions initiales
- [ ] Décider : continuer niveau suivant ou itérer sur niveau actuel?

---

## 📂 Structure des Fichiers de Résultats

Pour chaque semaine de test, créer :

```
/results/
├── semaine_1_niveau_2/
│   ├── trades.csv                    # Export MT5 de tous les trades
│   ├── logs_orderflow.txt            # Logs OrderFlow complets
│   ├── rejets_analysis.txt           # Analyse des signaux rejetés
│   ├── kpis_summary.json             # KPIs en JSON
│   ├── charts/                       # Screenshots de setups
│   │   ├── setup_win_001.png
│   │   ├── setup_loss_001.png
│   │   └── ...
│   └── RESULTATS_NIVEAU_2.md         # Rapport final de la semaine
│
├── semaine_2_niveau_2_5/
│   └── ... (même structure)
│
├── semaine_3_niveau_3/
│   └── ...
│
├── semaine_4_niveau_4/
│   └── ...
│
└── COMPARAISON_FINALE.md             # Analyse comparative des 4 modes
```

---

## 🎯 Objectif Final

À la fin des 4 semaines, avoir :

1. ✅ Données empiriques sur les 4 modes
2. ✅ Classement objectif basé sur KPIs
3. ✅ Choix du mode optimal pour production
4. ✅ Configuration finale validée et documentée
5. ✅ Compréhension claire de ce qui fonctionne et pourquoi

---

**Prêt à démarrer les tests ?**

👉 **ÉTAPE 1** : Activer `"active_burst_mode": "niveau_2"` dans config_trade_scalping.json
👉 **ÉTAPE 2** : Redémarrer le bot
👉 **ÉTAPE 3** : Monitorer pendant 5-7 jours
👉 **ÉTAPE 4** : Analyser résultats
👉 **ÉTAPE 5** : Passer au niveau suivant

**Bonne chance !** 🚀

---

**Document créé le** : 07 Janvier 2026
**Auteur** : Claude Sonnet 4.5
**Version** : 1.0
