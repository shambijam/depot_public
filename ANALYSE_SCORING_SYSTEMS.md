# ANALYSE DES SYSTEMES DE SCORING - 24 Janvier 2026

## VUE D'ENSEMBLE

| Module | Fichier | Lignes | Appelé par |
|--------|---------|--------|------------|
| OrderFlow V6 | `phase_observer/detect_orderflow_v6/scoring_engine.py` | 386 | `orderflow_v6.py` |
| Advanced Scorer | `strategy/advanced_scoring.py` | 706 | `run_bot.py` |
| Scalping interne | `strategy/scalping.py` | ~800 | `ScalpingStrategy` |

---

## 1. SCORING_ENGINE.PY (OrderFlow V6)

**Fonction**: `calculate_score_integrated()`

**Entrées**:
- `metrics`: delta_total, total_volume, imbalance_mean, cvd_slope, buy_ratio, rows
- `patterns`: dict ou list de patterns détectés
- `footprint_data`: buy_volume, sell_volume, delta_total, absorption_flag, imbalance_buy/sell
- `current_regime`: "trending", "consolidation", "range"

**Scores calculés**:
| Composant | Points max | Description |
|-----------|------------|-------------|
| Delta Momentum | 30 | Ratio delta/volume |
| Volume Confirmation | 20 | Ratio volume vs moyenne |
| Imbalance Strength | 10 | Écart imbalance vs 0.5 |
| Footprint Score | 30 | Absorption + Clustering + Rejection |
| Triggers Bonus | 25 | Patterns + Confluence |
| **TOTAL** | **100** | (avec pénalités) |

**Sortie**: `(final_score, status, summary)`

---

## 2. ADVANCED_SCORING.PY (SimpleAdvancedScorer)

**Fonction**: `calculate_composite_score()`

**Entrées**:
- `ticks_df`: DataFrame des ticks
- `candles_df`: DataFrame M1
- `orderflow_score`: Score V6 (0-100) - **RÉUTILISE scoring_engine.py**
- `institutional_analysis`: Résultats des 5 analyseurs

**Scores calculés** (6 composants pondérés):
| Composant | Poids | Source | Description |
|-----------|-------|--------|-------------|
| OrderFlow | 35% | scoring_engine.py | Score V6 existant |
| Institutional | 25% | 5 analyseurs | Price Memory, Fatigue, Physics, Tape, Pressure |
| Microstructure | 15% | ticks_df | Tape speed, accélération, clusters |
| Liquidity | 15% | ticks_df | Pressure ratio, continuité |
| Divergence | 5% | ticks_df + candles | Divergence price/delta |
| Smart Money | 5% | ticks_df | Large ticks, absorption |

**Sortie**: `{composite_score, components, decision, confidence, details}`

---

## 3. SCALPING.PY (Fonctions internes)

### 3.1 _analyze_orderflow_v6() - Lignes 287-1158
**Score**: 0-50 points
- Delta Momentum: 0-25 pts
- Volume Confirmation: 0-15 pts
- Imbalance Strength: 0-10 pts

### 3.2 _analyze_footprint_v6() - Lignes 1255-1586
**Score**: 0-30 points
- Absorption Levels: 0-12.5 pts
- Order Clustering: 0-8.5 pts
- Price Rejection: 0-4 pts
- Timing Score: 0-5 pts

### 3.3 _calculate_institutional_momentum() - Lignes 3345-3456
**Score**: 0-100 points (pondéré)
| Composant | Poids | Description |
|-----------|-------|-------------|
| Price Action | 35% | Candle patterns, body ratio, wicks, sequences |
| Volume Profile | 25% | Trend, spikes, correlation |
| Velocity | 20% | Velocity, acceleration, jerk |
| MTF Structure | 20% | Multi-timeframe alignment |

### 3.4 _log_orderflow_consolidated_report() - Lignes 1588-2007
**Normalisation finale** (USDJPY uniquement):
| Composant | Poids | Source |
|-----------|-------|--------|
| OrderFlow | 30% | _analyze_orderflow_v6 |
| Footprint | 30% | _analyze_footprint_v6 |
| Momentum | 20% | _calculate_institutional_momentum |
| VWAP | 20% | Externe |

---

## CHEVAUCHEMENTS IDENTIFIES

### A. TAPE SPEED / MICROSTRUCTURE (3 implémentations!)
| Fichier | Fonction | Calcule |
|---------|----------|---------|
| advanced_scoring.py | `_calculate_microstructure_score()` | ticks/sec, accélération, clusters |
| advanced_scoring.py | `_calculate_institutional_score()` | Tape Speed (composant 4) |
| scalping.py | `_analyze_velocity_acceleration()` | velocity, acceleration, jerk |

### B. VOLUME ANALYSIS (3 implémentations!)
| Fichier | Fonction | Calcule |
|---------|----------|---------|
| scoring_engine.py | Volume Confirmation | vol_ratio vs moyenne |
| advanced_scoring.py | `_calculate_liquidity_score()` | Pressure ratio, continuité |
| scalping.py | `_analyze_volume_profile()` | Trend, spikes, correlation |

### C. INSTITUTIONAL / SMART MONEY (4 implémentations!)
| Fichier | Fonction | Calcule |
|---------|----------|---------|
| advanced_scoring.py | `_calculate_institutional_score()` | 5 analyseurs externes |
| advanced_scoring.py | `_calculate_smart_money_score()` | Large ticks, absorption |
| scalping.py | `_calculate_institutional_momentum()` | PA, Volume, Velocity, MTF |
| scalping.py | `_detect_institutional_bias()` | Bias directionnel |

### D. ORDERFLOW / FOOTPRINT (2 implémentations!)
| Fichier | Fonction | Calcule |
|---------|----------|---------|
| scoring_engine.py | `calculate_score_integrated()` | Delta, Volume, Imbalance, FP |
| scalping.py | `_analyze_orderflow_v6()` + `_analyze_footprint_v6()` | Même chose! |

---

## DEPENDANCES

```
run_bot.py
    └── SimpleAdvancedScorer.calculate_composite_score()
            ├── orderflow_score (from scoring_engine.py via orderflow_v6.py)
            ├── institutional_analysis (from 5 analyseurs phase_observer/)
            ├── _calculate_microstructure_score()
            ├── _calculate_liquidity_score()
            ├── _calculate_divergence_score()
            └── _calculate_smart_money_score()

ScalpingStrategy.evaluate_entry()
    ├── _analyze_orderflow_v6()
    ├── _analyze_footprint_v6()
    ├── _calculate_institutional_momentum()
    └── _log_orderflow_consolidated_report()
```

---

## PLAN DE CONSOLIDATION PROPOSE

### Phase 1: Identifier le "maître"
- **Garder**: `advanced_scoring.py` comme module central
- **Migrer**: Logique de `scoring_engine.py` → `advanced_scoring.py`
- **Supprimer**: Fonctions dupliquées de `scalping.py`

### Phase 2: Fonctions à migrer vers advanced_scoring.py
1. `calculate_score_integrated()` → méthode de SimpleAdvancedScorer
2. `_analyze_footprint_v6()` → intégrer dans calcul OrderFlow

### Phase 3: Fonctions à supprimer de scalping.py
1. `_analyze_orderflow_v6()` (~870 lignes) - remplacer par appel à advanced_scoring
2. `_analyze_footprint_v6()` (~330 lignes) - fusionner
3. `_calculate_institutional_momentum()` (~110 lignes) - doublon de `_calculate_institutional_score`
4. Helpers associés (~200 lignes):
   - `_analyze_price_action()`
   - `_analyze_volume_profile()`
   - `_analyze_velocity_acceleration()`
   - `_analyze_multi_timeframe_structure()`
   - `_determine_direction_and_quality()`
   - `_detect_institutional_bias()`
   - `_assess_entry_quality()`

### Estimation: ~1500 lignes à supprimer/consolider

---

## RISQUES

1. **closure_rules** - Ne pas casser le passage de config (bug du 23 janvier)
2. **USDJPY specifics** - Certaines fonctions sont USDJPY-only
3. **Appels existants** - Vérifier tous les appelants avant suppression

---

*Document généré le 24 janvier 2026*
