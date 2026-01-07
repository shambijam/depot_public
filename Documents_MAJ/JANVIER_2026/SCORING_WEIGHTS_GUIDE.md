# Guide d'Utilisation - Poids de Scoring Configurables

**Date**: 06 Janvier 2026
**Version**: OrderFlow V6 Intégré
**Statut**: ✅ ACTIF

---

## 🎯 Contexte

Le système de scoring OrderFlow V6 est maintenant **entièrement configurable** via les fichiers de configuration JSON.

**Problème résolu** :
- ❌ **AVANT** : Poids hardcodés dans le code (impossible de les ajuster sans modifier le code)
- ✅ **MAINTENANT** : Poids configurables par asset dans les fichiers JSON

---

## 📊 Architecture du Scoring

### Structure 3 Composants

Le score total (0-100 points) est composé de 3 groupes :

```
┌─────────────────────────────────────────────────┐
│  SCORE TOTAL = 100 points                       │
├─────────────────────────────────────────────────┤
│  1. OrderFlow (50 pts par défaut)               │
│     - Delta Momentum: 25 pts                    │
│     - Volume Confirmation: 15 pts               │
│     - Imbalance Strength: 10 pts                │
│                                                  │
│  2. Footprint (30 pts par défaut)               │
│     - Absorption Levels: 15 pts                 │
│     - Order Clustering: 10 pts                  │
│     - Price Rejection: 5 pts                    │
│                                                  │
│  3. Triggers (20 pts par défaut)                │
│     - Pattern Count: 12 pts (60%)               │
│     - Multi-trigger Confluence: 4 pts (20%)     │
│     - Timeframe Alignment: 3 pts (15%)          │
└─────────────────────────────────────────────────┘
```

---

## 🔧 Configuration

### 1. Format de Configuration

Dans votre fichier asset config (ex: `config/assets_config/EURUSD.json`) :

```json
{
  "overrides": {
    "scalping": {
      "orderflow_v6": {
        "scoring_weights": {
          "delta_momentum_max": 28.0,
          "volume_confirm_max": 14.0,
          "imbalance_strength_max": 8.0,
          "absorption_max": 16.0,
          "clustering_max": 10.0,
          "rejection_max": 4.0,
          "triggers_max": 20.0
        }
      }
    }
  }
}
```

### 2. Poids Par Défaut

Si `scoring_weights` n'est **PAS** spécifié, les valeurs par défaut sont :

```python
{
    "delta_momentum_max": 25.0,      # OrderFlow
    "volume_confirm_max": 15.0,      # OrderFlow
    "imbalance_strength_max": 10.0,  # OrderFlow
    "absorption_max": 15.0,          # Footprint
    "clustering_max": 10.0,          # Footprint
    "rejection_max": 5.0,            # Footprint
    "triggers_max": 20.0,            # Triggers
}
```

**Total par défaut** : 25+15+10 (OrderFlow=50) + 15+10+5 (Footprint=30) + 20 (Triggers) = **100 points**

---

## 📐 Exemples de Configuration par Asset

### EURUSD (Volatilité Moyenne)

**Stratégie** : Augmenter importance du delta et absorption, réduire volume

```json
"scoring_weights": {
  "delta_momentum_max": 28.0,        // +3 pts (mouvement directionnel important)
  "volume_confirm_max": 14.0,        // -1 pts (volume moins critique)
  "imbalance_strength_max": 8.0,     // -2 pts (imbalance moins fiable)
  "absorption_max": 16.0,            // +1 pts (absorption fréquente)
  "clustering_max": 10.0,            // inchangé
  "rejection_max": 4.0,              // -1 pts
  "triggers_max": 20.0               // inchangé
}
```

**Total** : 28+14+8 (50) + 16+10+4 (30) + 20 = **100 points**

---

### USDJPY (Faible Volatilité)

**Stratégie** : Réduire delta, augmenter volume et clustering

```json
"scoring_weights": {
  "delta_momentum_max": 20.0,        // -5 pts (deltas plus faibles)
  "volume_confirm_max": 18.0,        // +3 pts (volume critique)
  "imbalance_strength_max": 12.0,    // +2 pts (imbalance fiable)
  "absorption_max": 12.0,            // -3 pts (absorption rare)
  "clustering_max": 13.0,            // +3 pts (clustering fréquent)
  "rejection_max": 5.0,              // inchangé
  "triggers_max": 20.0               // inchangé
}
```

**Total** : 20+18+12 (50) + 12+13+5 (30) + 20 = **100 points**

---

### XAUUSD (Haute Volatilité)

**Stratégie** : Maximiser delta et absorption, réduire triggers

```json
"scoring_weights": {
  "delta_momentum_max": 30.0,        // +5 pts (mouvements forts)
  "volume_confirm_max": 12.0,        // -3 pts
  "imbalance_strength_max": 8.0,     // -2 pts
  "absorption_max": 18.0,            // +3 pts (absorption intense)
  "clustering_max": 8.0,             // -2 pts
  "rejection_max": 4.0,              // -1 pts
  "triggers_max": 20.0               // inchangé
}
```

**Total** : 30+12+8 (50) + 18+8+4 (30) + 20 = **100 points**

---

## 🔬 Utilisation Programmatique

### Appel Direct de `detect_orderflow_v6()`

```python
from phase_observer.detect_orderflow_v6 import detect_orderflow_v6

# Charger config
asset_config = load_json("config/assets_config/EURUSD.json")
scoring_weights = asset_config["overrides"]["scalping"]["orderflow_v6"]["scoring_weights"]

# Préparer vp_options
vp_options = {
    "price_bins": 20,
    "coverage": 0.70,
    "scoring_weights": scoring_weights  # ✅ Passer les poids ici
}

# Appel
result = detect_orderflow_v6(
    df_m1=df,
    imbalance_threshold=0.20,
    cvd_smoothing=0.0,
    price_bins=20,
    vp_options=vp_options,  # ✅ Inclut scoring_weights
    logger=logger,
    footprint_data=footprint_data
)

print(f"Score: {result['score']:.1f}/100")
print(f"Status: {result['status']}")
```

### Appel via Stratégie (Automatique)

Si vous utilisez la classe `ScalpingStrategy` :

```python
# La stratégie charge automatiquement les poids depuis
# config/assets_config/{SYMBOL}.json

strategy = ScalpingStrategy(
    asset="EURUSD",
    config_manager=config_manager
)

# Les poids configurables sont automatiquement appliqués
analyzed = strategy.analyze_context(df_m1, df_m3, df_m5, ...)
```

---

## 🎯 Règles de Configuration

### 1. Contraintes de Total

**Important** : Le total des poids doit rester proche de **100 points** pour maintenir la cohérence du système.

```
OrderFlow Total + Footprint Total + Triggers Total ≈ 100
```

**Exemple VALIDE** :
- OrderFlow: 28+14+8 = 50 ✅
- Footprint: 16+10+4 = 30 ✅
- Triggers: 20 ✅
- **Total: 100** ✅

**Exemple INVALIDE** :
- OrderFlow: 40+20+15 = 75 ❌
- Footprint: 20+15+10 = 45 ❌
- Triggers: 30 ❌
- **Total: 150** ❌ (déséquilibré !)

### 2. Ratios Recommandés

**Composants OrderFlow** (total ~50 pts) :
- Delta Momentum: **45-60%** du total OrderFlow (22-30 pts)
- Volume Confirmation: **25-35%** (12-18 pts)
- Imbalance Strength: **15-25%** (8-12 pts)

**Composants Footprint** (total ~30 pts) :
- Absorption: **40-60%** du total Footprint (12-18 pts)
- Clustering: **30-40%** (9-12 pts)
- Rejection: **10-20%** (3-6 pts)

**Triggers** (total ~20 pts) :
- Généralement fixe à **20 pts**
- Peut être réduit à 15-18 si vous voulez donner plus aux autres composants

---

## 📈 Optimisation par Backtesting

### Processus Recommandé

1. **Baseline** : Commencer avec poids par défaut
2. **Analyser** : Identifier quels composants sont les plus prédictifs
3. **Ajuster** : Augmenter poids des composants performants
4. **Tester** : Backtester avec nouveaux poids
5. **Valider** : Comparer Sharpe ratio, win rate, drawdown
6. **Itérer** : Répéter jusqu'à convergence

### Métriques à Surveiller

```python
# Après backtest
metrics = {
    "sharpe_ratio": 2.1,         # Target: > 2.0
    "win_rate": 0.68,            # Target: > 0.65
    "avg_win_loss_ratio": 1.8,  # Target: > 1.5
    "max_drawdown": -0.03,       # Target: < -0.05
    "total_trades": 450
}
```

### Exemple de Log d'Optimisation

```
[BACKTEST] EURUSD - 2024-01-01 to 2024-12-31

Default Weights:
  delta_momentum_max: 25.0
  Sharpe: 1.8 | Win Rate: 64% | Avg W/L: 1.6

Optimized Weights:
  delta_momentum_max: 28.0  (+3)
  volume_confirm_max: 14.0  (-1)
  imbalance_strength_max: 8.0  (-2)
  absorption_max: 16.0  (+1)
  Sharpe: 2.3 (+27%) ✅ | Win Rate: 68% (+4%) ✅ | Avg W/L: 1.9 (+19%) ✅

Conclusion: Poids optimisés VALIDÉS
```

---

## ⚠️ Avertissements

### 1. Suroptimisation (Overfitting)

**Risque** : Ajuster les poids uniquement sur données historiques peut créer de l'overfitting.

**Solution** :
- Utiliser **walk-forward analysis**
- Valider sur **out-of-sample data**
- Tester sur **multiple périodes** (bull, bear, ranging)

### 2. Cohérence Multi-Assets

**Problème** : Poids très différents entre assets peuvent indiquer un problème.

**Exemple** :
```
EURUSD: delta_momentum_max = 28.0
USDJPY: delta_momentum_max = 20.0
GBPUSD: delta_momentum_max = 45.0  ❌ Trop différent !
```

**Solution** : Maintenir des poids relativement similaires (±30% max)

### 3. Stabilité dans le Temps

**Recommandation** : Réviser les poids tous les **3-6 mois** max.

**Ne PAS** :
- Ajuster les poids après chaque trade perdant
- Changer radicalement les poids sans backtest
- Utiliser des poids différents pour démo vs live

---

## 📝 Changelog

### Version 1.0 (06 JAN 2026)

**Changements** :
- ✅ Suppression code mort (fonction `calculate_score` ancienne)
- ✅ Ajout paramètre `scoring_weights` à `calculate_score_integrated()`
- ✅ Modification `orderflow_v6.py` pour passer poids depuis `vp_options`
- ✅ Mise à jour config `EURUSD.json` avec exemple `scoring_weights`
- ✅ Documentation complète

**Impact** :
- **Avant** : Poids hardcodés, impossible à ajuster → 0% flexibilité
- **Après** : Poids configurables par asset → 100% flexibilité ✅

---

## 🔗 Fichiers Concernés

### Code Source

- `phase_observer/detect_orderflow_v6/scoring_engine.py` : Système de scoring
- `phase_observer/detect_orderflow_v6/orderflow_v6.py` : Interface publique
- `strategy/scalping.py` : Intégration stratégie

### Configuration

- `config/assets_config/EURUSD.json` : Exemple EURUSD
- `config/assets_config/USDJPY.json` : À configurer
- `config/assets_config/GBPUSD.json` : À configurer
- `config/assets_config/XAUUSD.json` : À configurer

### Documentation

- `ANALYSE_ORDERFLOW_V6_COMPLETE.md` : Analyse complète du système
- `SCORING_WEIGHTS_GUIDE.md` : Ce guide (utilisation)

---

## ✅ Checklist de Validation

Avant de déployer vos poids configurés :

- [ ] Total des poids ≈ 100 points
- [ ] Ratios respectés (delta 45-60%, volume 25-35%, etc.)
- [ ] Backtest sur 6+ mois de données
- [ ] Sharpe ratio > 2.0
- [ ] Win rate > 65%
- [ ] Testé sur multiple régimes de marché
- [ ] Validé en out-of-sample
- [ ] Comparé avec poids par défaut
- [ ] Documenté dans config JSON
- [ ] Commité dans git avec message clair

---

## 🆘 Support

**Questions** :
- Lire `ANALYSE_ORDERFLOW_V6_COMPLETE.md` pour comprendre l'architecture
- Vérifier les logs de scoring dans la console
- Comparer avec exemples EURUSD/USDJPY/XAUUSD

**Debug** :
```python
# Activer logs détaillés
import logging
logging.basicConfig(level=logging.DEBUG)

# Vérifier que les poids sont chargés
print(f"Poids utilisés: {vp_options.get('scoring_weights')}")
```

**Contact** :
- Issue GitHub si bug détecté
- Pull Request si amélioration proposée

---

**Document généré le** : 06 Janvier 2026
**Auteur** : Système OrderFlow V6
**Version** : 1.0
