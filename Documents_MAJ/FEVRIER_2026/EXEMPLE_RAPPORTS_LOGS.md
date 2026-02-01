# EXEMPLES DE RAPPORTS LOGS - SNIPER_X BOT
## Visualisation des Rapports Générés par les Outils d'Analyse

**Version:** 1.0
**Date:** 6 Décembre 2025
**Scripts:** `/tools/analyze_trades.py`, `/tools/quick_stats.py`, `/tools/analyze_by_market_phase.py`

---

## TABLE DES MATIÈRES

1. [Fichier Source: trades_history.jsonl](#1-fichier-source-trades_historyjsonl)
2. [Rapport 1: Quick Stats (Suivi Quotidien)](#2-rapport-1-quick-stats)
3. [Rapport 2: Analyse Complète des Trades](#3-rapport-2-analyse-complète)
4. [Rapport 3: Analyse par Phase de Marché](#4-rapport-3-analyse-par-phase-de-marché)
5. [Utilisation des Rapports](#5-utilisation-des-rapports)

---

## 1. FICHIER SOURCE: trades_history.jsonl

### Format JSON Lines (1 trade par ligne)

```json
{
  "entry_time": "2025-12-06T14:32:18.423Z",
  "exit_time": "2025-12-06T14:47:09.872Z",
  "asset": "XAUUSD",
  "action": "BUY",
  "entry_price": 2050.51,
  "exit_price": 2050.66,
  "volume": 7.84,
  "pnl_pips": 15.0,
  "pnl_usd": 118.40,
  "outcome": "WIN",
  "duration_minutes": 14.85,
  "strategy": "scalping",
  "score_final": 0.82,
  "score_category": "PLATINE",
  "vwap_regime": "TRENDING",
  "market_regime": "BULLISH",
  "market_phase": "TRENDING_BULLISH",
  "trigger_type": "none",
  "trigger_confidence": 0.0,
  "has_real_trigger": false,
  "tick_count": 387,
  "coverage_s": 58.3,
  "quality_multiplier": 1.0,
  "orderflow_score": 78,
  "footprint_score": 65,
  "vwap_score": 0.82,
  "fusion_weights": {
    "orderflow": 0.30,
    "footprint": 0.20,
    "vwap": 0.50
  },
  "coherence": {
    "majority": "BUY",
    "agreement": 0.95,
    "aligned_components": 3
  },
  "sl_price": 2050.3725,
  "sl_distance_pips": 13.75,
  "basket_id": "BURST_XAUUSD_20251206143212_A7F2",
  "tickets_count": 8,
  "metadata": {
    "bot_version": "4.4-unblocked",
    "mode": "DEMO",
    "cycle": 42
  }
}
```

### Deuxième exemple (LOSS):

```json
{
  "entry_time": "2025-12-06T15:12:00.123Z",
  "exit_time": "2025-12-06T15:18:05.456Z",
  "asset": "XAUUSD",
  "action": "BUY",
  "entry_price": 2048.30,
  "exit_price": 2047.20,
  "volume": 7.84,
  "pnl_pips": -110.0,
  "pnl_usd": -862.00,
  "outcome": "LOSS",
  "duration_minutes": 6.08,
  "strategy": "scalping",
  "score_final": 0.68,
  "score_category": "ARGENT",
  "vwap_regime": "TRANSITIONAL",
  "market_regime": "NEUTRAL",
  "market_phase": "RANGE_DISTRIBUTION",
  "trigger_type": "none",
  "trigger_confidence": 0.0,
  "has_real_trigger": false,
  "tick_count": 142,
  "coverage_s": 32.1,
  "quality_multiplier": 0.95,
  "orderflow_score": 62,
  "footprint_score": 58,
  "vwap_score": 0.75,
  "fusion_weights": {
    "orderflow": 0.40,
    "footprint": 0.40,
    "vwap": 0.20
  },
  "coherence": {
    "majority": "BUY",
    "agreement": 0.72,
    "aligned_components": 2
  },
  "sl_price": 2048.1725,
  "sl_distance_pips": 12.75,
  "basket_id": "BURST_XAUUSD_20251206151200_B3C9",
  "tickets_count": 8,
  "close_reason": "LOSS_GUARD_TRIGGERED",
  "metadata": {
    "bot_version": "4.4-unblocked",
    "mode": "DEMO",
    "cycle": 82
  }
}
```

---

## 2. RAPPORT 1: QUICK STATS

### Commande
```bash
python tools/quick_stats.py
```

### Sortie Console

```
======================================================================
📊 STATISTIQUES RAPIDES - COLLECTE DONNÉES
======================================================================

📅 Période : 2025-12-01 → 2025-12-06 (6 jours)
📈 Trades Collectés : 127 trades
⚡ Cadence : 21.2 trades/jour

======================================================================
🎯 RÉSULTATS
======================================================================
  ✅ WIN     :  82 trades
  ❌ LOSS    :  38 trades
  🟡 BE      :   5 trades
  ⏳ PENDING :   2 trades

  📊 Win Rate : 68.3% (82W / 38L)

  💰 PnL Total : +4,520.75 USD
  💵 PnL Moyen : +35.59 USD/trade

======================================================================
📊 DISTRIBUTION PAR CATÉGORIE
======================================================================
  💎 DIAMANT  :  12 trades (  9.4%)
  🔷 PLATINE  :  38 trades ( 29.9%)
  🟡 OR       :  45 trades ( 35.4%)
  🔘 ARGENT   :  24 trades ( 18.9%)
  🟤 BRONZE   :   8 trades (  6.3%)

======================================================================
🎯 OBJECTIFS
======================================================================
  Objectif Phase 1 : 100 trades
  Progression      : [██████████████████████████████████████████████████] 100.0%
  Restant          : 0 trades

✅ Conseil : Assez de données ! Lancez l'analyse complète
            Commande : python tools/analyze_trades.py --min-trades 50

======================================================================
```

---

## 3. RAPPORT 2: ANALYSE COMPLÈTE

### Commande
```bash
python tools/analyze_trades.py --min-trades 50
```

### Sortie Console

```
📁 Fichier: logs/trades_history.jsonl
📊 Trades chargés: 125

================================================================================
📊 PERFORMANCE PAR CATÉGORIE DE SCORE
================================================================================

🟢 DIAMANT     (score ≥90%)
   Trades      :   12 trades
   Win Rate    :  91.7% (11W / 1L / 0BE)
   Avg PnL     :  +18.2 pips (+142.50 USD)
   Avg Duration:   12.3 min

🟢 PLATINE     (score ≥80%)
   Trades      :   38 trades
   Win Rate    :  78.9% (30W / 8L / 0BE)
   Avg PnL     :  +12.5 pips (+97.80 USD)
   Avg Duration:   14.7 min

🟡 OR          (score ≥70%)
   Trades      :   45 trades
   Win Rate    :  64.4% (29W / 16L / 0BE)
   Avg PnL     :   +6.8 pips (+53.20 USD)
   Avg Duration:   13.1 min

🟡 ARGENT      (score ≥60%)
   Trades      :   24 trades
   Win Rate    :  50.0% (12W / 12L / 0BE)
   Avg PnL     :   -2.1 pips (-16.40 USD)
   Avg Duration:   11.8 min

🔴 BRONZE      (score ≥50%)
   Trades      :    8 trades
   Win Rate    :  25.0% (2W / 6L / 0BE)
   Avg PnL     :  -28.5 pips (-223.10 USD)
   Avg Duration:    8.2 min

================================================================================
🎯 NOTE: TRIGGERS FOOTPRINT DÉSACTIVÉS (3 Décembre 2025)
================================================================================

Depuis le 3 décembre 2025, les triggers footprint (stacking, climax, absorption, etc.)
ont été SUPPRIMÉS du pipeline de décision.

Le système utilise maintenant UNIQUEMENT :
  - OrderFlow V6 (score 0-100)
  - Footprint M1 (score 0-100)
  - VWAP Dynamique (score 0-1.0 + détection régime de marché)

La fusion se fait avec des POIDS ADAPTATIFS selon le régime VWAP :
  - TRENDING    : VWAP 50%, OrderFlow 30%, Footprint 20%
  - BALANCED    : VWAP 30%, OrderFlow 35%, Footprint 35%
  - ACCUMULATION: VWAP 25%, OrderFlow 35%, Footprint 40%
  - TRANSITIONAL: VWAP 20%, OrderFlow 40%, Footprint 40%

Le champ "trigger_type" dans les logs vaut maintenant TOUJOURS "none".

================================================================================
📈 IMPACT QUALITÉ DONNÉES (tick_count, coverage_s)
================================================================================

🔍 Segmentation par tick_count:
  🔴 < 50 ticks       :  18 trades | Win Rate:  38.9% | Avg PnL:  -18.2 pips
  🟡 50-100 ticks     :  42 trades | Win Rate:  59.5% | Avg PnL:   +4.5 pips
  🟢 > 100 ticks      :  65 trades | Win Rate:  76.9% | Avg PnL:  +12.8 pips

🔍 Segmentation par coverage_s:
  🔴 < 20s            :  12 trades | Win Rate:  33.3% | Avg PnL:  -22.5 pips
  🟡 20-40s           :  38 trades | Win Rate:  57.9% | Avg PnL:   +3.2 pips
  🟢 > 40s            :  75 trades | Win Rate:  74.7% | Avg PnL:  +11.5 pips

================================================================================
🔗 CORRÉLATION SCORE vs PnL RÉEL
================================================================================
  🟢 Score 90-99%  :  12 trades | Win Rate:  91.7% | Avg PnL:  +18.2 pips
  🟢 Score 80-89%  :  38 trades | Win Rate:  78.9% | Avg PnL:  +12.5 pips
  🟡 Score 70-79%  :  45 trades | Win Rate:  64.4% | Avg PnL:   +6.8 pips
  🟡 Score 60-69%  :  24 trades | Win Rate:  50.0% | Avg PnL:   -2.1 pips
  🔴 Score 50-59%  :   8 trades | Win Rate:  25.0% | Avg PnL:  -28.5 pips

================================================================================
💡 RECOMMANDATIONS D'OPTIMISATION
================================================================================

📉 Pénalités qualité appliquées: 28 trades (57.1% win rate)
   ⚠️ ATTENTION: Les trades pénalisés ont un bon win rate !
   → Recommandation: ASSOUPLIR les pénalités (tick_count, coverage_s)

🎯 Impact Régime VWAP:
   TRENDING       :  42 trades |  78.6% win rate | +14.2 pips avg
   BALANCED       :  38 trades |  71.1% win rate | +10.5 pips avg
   ACCUMULATION   :  28 trades |  57.1% win rate |  +3.8 pips avg
   TRANSITIONAL   :  17 trades |  41.2% win rate |  -8.5 pips avg
   ✅ Les régimes TRENDING/BALANCED sont très performants
   → Recommandation: Favoriser les trades en régimes directionnels

================================================================================
✅ Analyse terminée
================================================================================
```

---

## 4. RAPPORT 3: ANALYSE PAR PHASE DE MARCHÉ

### Commande
```bash
python tools/analyze_by_market_phase.py
```

### Sortie Console

```
================================================================================
📊 ANALYSE DE PERFORMANCE PAR PHASE DE MARCHÉ
================================================================================

Total trades analysés: 125

📈 RÉGIMES VWAP (triés par Win Rate):
--------------------------------------------------------------------------------
  TRENDING             | Trades:  58 | Win Rate:  77.6% | PnL Moy:  +13.2 pips | Score:  79.8%
  BALANCED             | Trades:  35 | Win Rate:  62.9% | PnL Moy:   +5.8 pips | Score:  72.4%
  ACCUMULATION         | Trades:  22 | Win Rate:  54.5% | PnL Moy:   +1.2 pips | Score:  68.2%
  TRANSITIONAL         | Trades:  10 | Win Rate:  30.0% | PnL Moy:  -22.5 pips | Score:  61.5%

================================================================================
```

### Fichier Markdown Généré: `logs/performance_by_phase.md`

```markdown
# 📊 Analyse de Performance par Phase de Marché

*Généré le 2025-12-06 18:15:42*

**Total trades analysés:** 125

---

## 📈 Performance par Régime VWAP

| Régime VWAP | Trades | Win Rate | PnL Moy (pips) | PnL Moy (USD) | Score Moy | W/L/BE |
|-------------|--------|----------|----------------|---------------|-----------|--------|
| 📈 **TRENDING** | 58 | 77.6% | +13.2 | +103.45 | 79.8% | 45/13/0 |
| ⚖️ **BALANCED** | 35 | 62.9% | +5.8 | +45.35 | 72.4% | 22/13/0 |
| 📊 **ACCUMULATION** | 22 | 54.5% | +1.2 | +9.40 | 68.2% | 12/10/0 |
| 🔄 **TRANSITIONAL** | 10 | 30.0% | -22.5 | -176.25 | 61.5% | 3/7/0 |

## 🎯 Performance par Régime PhaseObserver

| Régime PhaseObserver | Trades | Win Rate | PnL Moy (pips) | PnL Moy (USD) | Score Moy | W/L/BE |
|----------------------|--------|----------|----------------|---------------|-----------|--------|
| **BULLISH** | 52 | 75.0% | +12.8 | +100.30 | 78.5% | 39/13/0 |
| **NEUTRAL** | 48 | 62.5% | +4.2 | +32.90 | 70.8% | 30/18/0 |
| **BEARISH** | 25 | 52.0% | -2.5 | -19.60 | 66.2% | 13/12/0 |

## 🚀 Performance par Phase Optimisée

| Phase Optimisée | Trades | Win Rate | PnL Moy (pips) | PnL Moy (USD) | Score Moy | W/L/BE |
|-----------------|--------|----------|----------------|---------------|-----------|--------|
| **TRENDING_BULLISH** | 48 | 79.2% | +14.5 | +113.55 | 81.2% | 38/10/0 |
| **RANGE_ACCUMULATION** | 28 | 64.3% | +6.3 | +49.35 | 73.8% | 18/10/0 |
| **LIQUIDITY_HUNT** | 22 | 59.1% | +3.8 | +29.75 | 71.5% | 13/9/0 |
| **TRENDING_BEARISH** | 15 | 53.3% | +1.2 | +9.40 | 69.2% | 8/7/0 |
| **RANGE_DISTRIBUTION** | 12 | 25.0% | -28.5 | -223.10 | 62.8% | 3/9/0 |

## 💡 Recommandations

### 📈 Régimes VWAP

- ✅ **Meilleur régime**: TRENDING (Win Rate: 77.6%, 58 trades)
- ❌ **Pire régime**: TRANSITIONAL (Win Rate: 30.0%, 10 trades)

💡 **Action suggérée**: Augmenter le poids VWAP en régime TRENDING

⚠️ **Action suggérée**: Réduire le poids VWAP en régime TRANSITIONAL ou filtrer les trades

### 🎯 Régimes PhaseObserver

- ✅ **Meilleur régime**: BULLISH (Win Rate: 75.0%)

---

*Rapport généré par analyze_by_market_phase.py*
```

---

## 5. UTILISATION DES RAPPORTS

### 5.1 Workflow Recommandé

```
┌─────────────────────────────────────────────────────────────┐
│ PHASE 1: COLLECTE (J0 → J7)                                │
├─────────────────────────────────────────────────────────────┤
│ • Lancer bot en mode DEMO                                  │
│ • Laisser tourner 7 jours minimum                          │
│ • Objectif: 100+ trades collectés                          │
│                                                              │
│ Commande quotidienne:                                       │
│   python tools/quick_stats.py                               │
│                                                              │
│ Output:                                                     │
│   - Progression vers objectif (barre 0-100%)               │
│   - Win rate instantané                                    │
│   - Distribution catégories                                │
└─────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│ PHASE 2: ANALYSE INITIALE (J7)                             │
├─────────────────────────────────────────────────────────────┤
│ Commande:                                                   │
│   python tools/analyze_trades.py --min-trades 50            │
│                                                              │
│ Actions:                                                    │
│   1. Identifier catégories performantes (DIAMANT, PLATINE) │
│   2. Analyser triggers efficaces (volume_breakout, impulse)│
│   3. Vérifier impact qualité (tick_count, coverage_s)      │
│   4. Lire recommandations (bonus/malus à ajuster)          │
└─────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│ PHASE 3: OPTIMISATION (J7+)                                │
├─────────────────────────────────────────────────────────────┤
│ Commande:                                                   │
│   python tools/analyze_by_market_phase.py                  │
│                                                              │
│ Actions:                                                    │
│   1. Identifier phases gagnantes (TRENDING_BULLISH)        │
│   2. Ajuster poids adaptatifs fusion_manager.py:           │
│      - TRENDING → VWAP 50% (si win rate > 75%)            │
│      - TRANSITIONAL → VWAP 20% (si win rate < 40%)        │
│   3. Filtrer phases perdantes (RANGE_DISTRIBUTION)         │
│   4. Lire logs/performance_by_phase.md (détails complets)  │
└─────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│ PHASE 4: MONITORING CONTINU (J14+)                         │
├─────────────────────────────────────────────────────────────┤
│ Fréquence:                                                  │
│   - quick_stats.py      : Quotidien (monitoring rapide)    │
│   - analyze_trades.py   : Hebdomadaire (deep dive)         │
│   - analyze_by_market_phase.py : Mensuel (optimisation)    │
│                                                              │
│ KPIs à surveiller:                                          │
│   - Win rate global > 65%                                   │
│   - PnL moyen positif (+10 pips minimum)                   │
│   - Catégories DIAMANT/PLATINE > 40% total trades          │
│   - Régime TRENDING win rate > 75%                         │
└─────────────────────────────────────────────────────────────┘
```

### 5.2 Interprétation des Résultats

#### 🟢 **Signaux Positifs**

| Métrique | Seuil Positif | Action |
|----------|---------------|--------|
| Win Rate DIAMANT | > 85% | ✅ Maintenir catégorie, potentiel LIVE |
| Win Rate PLATINE | > 75% | ✅ Excellente performance, focus ici |
| Régime TRENDING | > 75% | ✅ Augmenter poids VWAP (50% → 55%) |
| Régime BALANCED | > 70% | ✅ Augmenter poids OrderFlow/Footprint |
| Qualité > 100 ticks | > 75% | ✅ Assouplir pénalités (gain marginal) |

#### 🟡 **Signaux Neutres**

| Métrique | Seuil Neutre | Action |
|----------|--------------|--------|
| Win Rate OR | 60-70% | 🟡 Correct, surveiller évolution |
| Win Rate ARGENT | 50-60% | 🟡 Limite acceptable, investiguer |
| Régime BALANCED | 55-65% | 🟡 Performance moyenne, ok |
| Qualité 50-100 ticks | 50-65% | 🟡 Pénalités justifiées |

#### 🔴 **Signaux Négatifs**

| Métrique | Seuil Négatif | Action |
|----------|---------------|--------|
| Win Rate BRONZE | < 40% | ⚠️ BLOQUER catégorie (seuil min 60%) |
| Win Rate global | < 55% | 🚨 STOP LIVE, analyser problème |
| Régime TRANSITIONAL | < 40% | ⚠️ Filtrer trades (skip ce régime) |
| Régime ACCUMULATION | < 45% | ⚠️ Ajuster poids adaptatifs |
| Qualité < 50 ticks | < 40% | ✅ Pénalités justifiées, maintenir |

### 5.3 Exemple Décision Post-Analyse

**Scénario:**
- DIAMANT: 91.7% win rate (12 trades)
- PLATINE: 78.9% win rate (38 trades)
- OR: 64.4% win rate (45 trades)
- ARGENT: 50.0% win rate (24 trades)
- BRONZE: 25.0% win rate (8 trades)

**Décisions:**

1. ✅ **Bloquer BRONZE** → Modifier `fusion_manager.py`:
   ```python
   # Ligne ~1097
   if fused_score < 0.60:  # Ancien: 0.50
       return {"action": "HOLD", "reason": "SCORE_TOO_LOW"}
   ```

2. ✅ **Augmenter poids VWAP en régime TRENDING** (78.6% win rate):
   ```python
   # fusion_manager.py (poids adaptatifs)
   if vwap_regime == "TRENDING":
       w_vwap = 0.55  # Ancien: 0.50
       w_of = 0.28    # Ancien: 0.30
       w_fp = 0.17    # Ancien: 0.20
   ```

3. ⚠️ **Filtrer TRANSITIONAL** (41.2% win rate):
   ```python
   # Ligne ~1050 (poids adaptatifs)
   if vwap_regime == "TRANSITIONAL":
       # Option 1: Poids minimal
       w_vwap = 0.10  # Ancien: 0.20
       # Option 2: Skip complètement
       if fused_score < 0.70:  # Seuil élevé
           return {"action": "HOLD", "reason": "TRANSITIONAL_REGIME"}
   ```

---

## 6. FICHIERS GÉNÉRÉS

### Structure Répertoire `logs/`

```
logs/
├── trades_history.jsonl          # Source (JSON Lines)
│   └── 1 ligne par trade complété
│
├── performance_by_phase.md       # Rapport Markdown
│   └── Généré par analyze_by_market_phase.py
│
├── sniper_x_20251206.log         # Logs principaux
├── trades_20251206.log           # Logs trades
├── audit_20251206.log            # Audit trail (JSON)
└── errors_20251206.log           # Erreurs
```

### Commandes Utiles

```bash
# Compter trades total
wc -l logs/trades_history.jsonl

# Derniers 10 trades
tail -10 logs/trades_history.jsonl | jq .

# Win rate rapide
grep '"outcome":"WIN"' logs/trades_history.jsonl | wc -l
grep '"outcome":"LOSS"' logs/trades_history.jsonl | wc -l

# PnL total
cat logs/trades_history.jsonl | jq -s 'map(.pnl_usd) | add'

# Meilleur trade
cat logs/trades_history.jsonl | jq -s 'max_by(.pnl_usd)'

# Pire trade
cat logs/trades_history.jsonl | jq -s 'min_by(.pnl_usd)'
```

---

## CONCLUSION

Les 3 scripts d'analyse produisent des rapports complémentaires:

1. **`quick_stats.py`** → Monitoring quotidien (30 secondes)
2. **`analyze_trades.py`** → Analyse hebdomadaire approfondie (deep dive)
3. **`analyze_by_market_phase.py`** → Optimisation mensuelle (ajustements)

**Workflow optimal:**
- J1-7: Collecte (quick_stats quotidien)
- J7: Analyse initiale (analyze_trades)
- J14+: Optimisation (analyze_by_market_phase)
- J30+: Révision globale (3 rapports combinés)

**Objectif final:** Win rate > 65%, PnL moyen > +10 pips, catégories DIAMANT/PLATINE > 40%

---

**Document Version:** 1.0
**Dernière MAJ:** 6 Décembre 2025
**Auteur:** Architecture Team SNIPER_X
**Scripts:** `/tools/*.py`
