# RAPPORT COMPLET - LOGIQUE DE DÉCISION DE PRISE DE TRADE
## SNIPER_X BOT - BURST SCALPING USDJPY

**Date**: 24 Décembre 2025
**Version Bot**: 4.4-unblocked
**Mode**: Burst Scalping USDJPY
**Cycle**: 5 secondes

---

## TABLE DES MATIÈRES

1. [Executive Summary](#1-executive-summary)
2. [Point d'Entrée - Cycle 5 Secondes](#2-point-dentrée---cycle-5-secondes)
3. [Collecte de Données](#3-collecte-de-données)
4. [Analyse Multi-Composants](#4-analyse-multi-composants)
5. [Fusion des Signaux](#5-fusion-des-signaux)
6. [Filtres & VETOS](#6-filtres--vetos)
7. [Décision Finale](#7-décision-finale)
8. [Exécution](#8-exécution)
9. [Surveillance & Fermeture](#9-surveillance--fermeture)
10. [Diagramme de Flux Complet](#10-diagramme-de-flux-complet)
11. [Liste Exhaustive des Facteurs](#11-liste-exhaustive-des-facteurs)
12. [Seuils et Valeurs par Défaut](#12-seuils-et-valeurs-par-défaut)
13. [Zones d'Incohérence & Bugs](#13-zones-dincohérence--bugs)
14. [Recommandations](#14-recommandations)

---

## 1. EXECUTIVE SUMMARY

SNIPER_X est un bot de trading institutionnel basé sur une architecture multi-composants avec fusion de signaux. Le système fonctionne en **cycle ultra-rapide de 5 secondes** pour USDJPY en mode BURST SCALPING.

### Architecture Globale
```
OrderFlow V6 (30-50%) + Footprint M1 (25-40%) + VWAP (20-50%) + Momentum (10%)
         ↓
  FusionManager (Poids Adaptatifs selon Régime VWAP)
         ↓
  Filtres VETO (Momentum / MTF Alignment / Entry Quality)
         ↓
  Classification par Seuils (HIGH 80% / MODERATE 75% / CONDITIONAL 60%)
         ↓
  Exécution Burst (11 ordres simultanés)
         ↓
  Monitoring Temps Réel (100ms polling)
```

### Métriques Clés
- **Intervalle**: 5 secondes
- **Burst Size**: 11 positions simultanées
- **Volume/position**: 0.10 lot USDJPY
- **SL**: 50 pips (0.50 points)
- **TP**: 75 pips (0.75 points, RR 1.5)
- **Profit Target Panier**: +60 pips
- **Loss Guard Panier**: -80 pips

---

## 2. POINT D'ENTRÉE - CYCLE 5 SECONDES

### 2.1 Démarrage Bot
**Fichier**: `run_bot.py:main()` - Ligne 3947

**Séquence d'initialisation**:
1. Chargement configs (prod_config.json, config_trade_scalping.json, USDJPY.json)
2. Connexion MT5 persistante
3. Instanciation modules (ConfigManager, StrategyManager, DecisionPipeline, etc.)
4. Création threads

**Threads lancés**:
| Thread | Intervalle | Objectif |
|--------|------------|----------|
| **ScalpingThread** | 5s | Analyse + Exécution (USDJPY) |
| LiquidityThread | 60s | Liquidity EURUSD/GBPUSD |
| BasketMonitorThread | Continu (100ms) | Surveillance PnL paniers |
| DataEngine | Background | Pré-calcul footprint |

### 2.2 Boucle Scalping Thread
**Fonction**: `scalping_fast_thread()` - Ligne 3182

```python
while not stop_event.is_set():  # Boucle infinie
    cycle_start = time.time()

    # 1. Récupération données (cache-first)
    rates_df = bars_cache.get("USDJPY", "M1", 50, ttl=60s)
    cached_footprint = footprint_cache.get("USDJPY", 15s)
    df_m3 = mt5.get_rates("M3", 8)
    df_m5 = mt5.get_rates("M5", 6)

    # 2. Analyse marché (MarketAnalyzer)
    # 3. VWAP Analysis
    # 4. OrderFlow V6
    # 5. Footprint V6 + Timing
    # 6. Momentum Institutionnel
    # 7. Fusion Manager → Décision
    # 8. Exécution (si ok)

    elapsed = time.time() - cycle_start
    sleep_time = max(0, 5 - elapsed)
    time.sleep(sleep_time)
```

---

## 3. COLLECTE DE DONNÉES

### 3.1 Sources de Données

| Source | Type | Fenêtre | TTL Cache | Origine |
|--------|------|---------|-----------|---------|
| **Barres M1** | OHLCV | 50 barres | 60s | `bars_cache` → MT5 |
| **Ticks M1** | Tick-by-tick | Bougie courante (0-60s) | 15s | `footprint_cache` / DataEngine |
| **Barres M3** | OHLCV | 8 barres | None | `mt5.get_rates()` direct |
| **Barres M5** | OHLCV | 6 barres | None | `mt5.get_rates()` direct |
| **Tick actuel** | Price/Spread | Instantané | None | `mt5.get_symbol_tick()` |

### 3.2 Données Récupérées

**MarketAnalyzer.analyze()** retourne:
```python
{
    "annotated_df": DataFrame,       # Barres avec indicateurs
    "latest": {
        "close": float,
        "current_price": float,
        "regime": str,               # PhaseObserver (trending/range/compression)
        "range_pos_pct": float,      # Position dans range (0-1)
        "in_upper_tercile": bool,    # >66%
        "in_lower_tercile": bool,    # <33%
        "orderflow_score": float,    # 0-100
        "orderflow_status": str,     # VALID/WEAK/SUSPECT
        "footprint_status": str,
        "vwap_score": float,         # 0-1
        "vwap_status": str,
        "vwap_regime": str,          # TRENDING/ACCUMULATION/BALANCED/TRANSITIONAL
        "vwap_bias": str,            # BUY/SELL/NEUTRAL
        "vwap_distance_pips": float
    },
    "footprint": {...},
    "footprint_trigger": {...}
}
```

---

## 4. ANALYSE MULTI-COMPOSANTS

### 4.1 ORDERFLOW V6 (30-50% du score)

**Fichier**: `phase_observer/detect_orderflow_v6/orderflow_v6.py`

**Données**: 10 dernières barres M1

**Métriques calculées**:
```python
delta_total = Σ(buy_volume - sell_volume)  # Somme cumulée
imbalance = buy_volume / total_volume      # Ratio 0-1
cvd = cumulative_volume_delta              # CVD cumulé
cvd_slope = trend(cvd) sur N barres
tick_rate = ticks / coverage_s
coverage_s = durée_couverture_ticks
volume_ratio = current_vol / avg_vol
```

**Scoring Engine** (0-100 pts):
| Composant | Poids Max | Calcul |
|-----------|-----------|--------|
| **Delta** | 30 pts | min(30, abs(delta_total) / 15.0 × 30) |
| **Imbalance** | 25 pts | 25 si ≥0.70, 15 si ≥0.60, 0 sinon |
| **Tickrate** | 20 pts | 20 si ≥3.0, 10 si ≥1.5, 0 sinon |
| **Divergence** | 10 pts | 10 si détectée (prix vs CVD) |
| **VPOC Drift** | 5 pts | 5 si drift <10 points |

**Status**: VALID (≥28) / WEAK (≥14) / SUSPECT (<14)

**Seuils USDJPY** (vs XAUUSD):
- `delta_abs_strong`: **15.0** (vs 180)
- `imbalance_extreme`: **0.70**
- `tickrate_min`: **1.5** ticks/s (vs 4-8)
- `coverage_s_min`: **3s**

### 4.2 FOOTPRINT ANALYSIS (25-40% du score)

**Fichier**: `strategy/scalping.py`
**Fonction**: `_analyze_footprint_v6()` - Ligne 1145

**Analyse**: Ticks bougie M1 en cours

**Métriques**:
```python
buy_volume = Σ(ticks buy)
sell_volume = Σ(ticks sell)
delta_total = buy - sell
buy_ratio = buy / total
tick_count = nombre_ticks
coverage_s = durée_couverture
tick_rate = ticks / coverage_s
```

**Scoring** (0-25 pts):
- tick_count ≥ 30: +5 pts
- coverage_s ≥ 10s: +5 pts
- tick_rate ≥ 1.5: +5 pts
- abs(delta) significatif: +5 pts
- buy_ratio cohérent: +5 pts

**Status**: VALID (≥15) / WEAK (≥7.5) / SUSPECT

**Triggers détectés**:
- **Climax Volume**: Volume extrême
- **Order Stacking**: Accumulation progressive
- **Absorption**: Achat dans baisse / Vente dans montée

### 4.3 VWAP INSTITUTIONNEL (20-50% du score)

**Fichier**: `phase_observer/vwap/analyzer.py`

**Calcul VWAP**:
```python
vwap = Σ(price × volume) / Σ(volume)  # Session (reset minuit UTC)
```

**Régimes VWAP** (poids adaptatifs):
| Régime | Poids VWAP | Poids OF | Poids FP | Poids MOM |
|--------|------------|----------|----------|-----------|
| **TRENDING** | 50% | 30% | 10% | 10% |
| **BALANCED** | 30% | 35% | 25% | 10% |
| **ACCUMULATION** | 25% | 35% | 30% | 10% |
| **TRANSITIONAL** | 20% | 40% | 30% | 10% |

**Métriques**:
```python
score: 0.0-1.0           # Score normalisé
status: VALID/SUSPECT
bias: BUY/SELL/NEUTRAL   # Direction institutionnelle
regime: str              # Type régime
distance_pips: float     # Distance prix-VWAP
slope: float             # Tendance VWAP
```

**Signaux**:
- Prix > VWAP + distance → **SELL** (surachat)
- Prix < VWAP - distance → **BUY** (survente)

### 4.4 MOMENTUM INSTITUTIONNEL (10% du score)

**Fichier**: `strategy/scalping.py`
**Classe**: `MomentumAnalyzerInstitutional` - Ligne 165

**Score 0-100 pts** (4 composants):

#### A. Candle Strength (30 pts)
```python
body_ratio = abs(close - open) / (high - low)
coherence = direction identique 3 bougies
close_position = (close - low) / (high - low)

score = body_ratio×18 + coherence×10 + close_pos×5 + last_3×7.5
```

#### B. Volume Confirmation (25 pts)
```python
volume_ratio = current_vol / avg_vol
volume_momentum = trend(volume) sur 5 barres

score = volume_ratio×18.75 + volume_momentum×20
```

#### C. Price Acceleration (25 pts)
```python
move_pct = (close - close[-3]) / close[-3]
acceleration = diff(move_pct)

score = move_pct×66.67 + acceleration×10 + regularity×5
```

#### D. MTF Alignment (20 pts)
```python
m1_direction = analyze(8 barres M1)
m3_direction = analyze(6 barres M3)
m5_direction = analyze(6 barres M5)

score = 20 si tous alignés, 15 si 2/3, 10 si 1/3, 0 sinon
```

**Seuils direction**:
- BULLISH: ≥6/8 bougies vertes (75%)
- BEARISH: ≤2/8 bougies vertes (25%)
- NEUTRAL: entre les deux

**Quality**: EXCELLENT (≥65) / GOOD (≥50) / FAIR (≥35) / POOR (<35)

### 4.5 TIMING ANALYZER (Multiplicateur ×0.85-1.20)

**Fichier**: `phase_observer/timing_analyzer.py`

**Analyse**: Distribution temporelle ticks sur bougie M1

**Quartiles** (60s divisé en 4×15s):
```
Q1 = [0-15s]    # Setup rapide
Q2 = [15-30s]
Q3 = [30-45s]
Q4 = [45-60s]
```

**Métriques**:
```python
buy_concentration_q1: % volume buy en Q1
sell_concentration_q1: % volume sell en Q1
velocity_ratio: buy_velocity / sell_velocity
timing_score: 0.0-5.0
```

**Multiplicateurs appliqués au score final**:
- EXCELLENT (score ≥3.5): × 1.20 (+20%)
- GOOD (score ≥2.5): × 1.10 (+10%)
- FAIR (score ≥1.5): × 1.00
- POOR (score <1.5): × 0.85 (-15%)
- VETO (catastrophique): × 0.50 (-50%)

**Seuils USDJPY**:
- `q1_expected_min`: **35%** (vs 60% XAUUSD)
- `q1_strong`: **50%**

---

## 5. FUSION DES SIGNAUX

**Fichier**: `phase_observer/fusion_manager.py`
**Fonction**: `fuse()` - Ligne 314

### 5.1 Normalisation (0-1)

```python
# OrderFlow: 0-100 → 0-1
n_of = {
    "score": orderflow["score"] / 100.0,
    "dir": +1 (BUY) / -1 (SELL) / 0 (NEUTRAL),
    "delta_total": float,
    "status": "VALID/WEAK/SUSPECT"
}

# Footprint: 0-100 → 0-1
n_fp = {
    "score": footprint["score"] / 100.0,
    "dir": +1 / -1 / 0
}

# VWAP: déjà 0-1
n_vw = {
    "score": 0.0-1.0,
    "dir": +1 / -1 / 0,
    "bias": "BUY/SELL/NEUTRAL",
    "regime": "TRENDING/BALANCED/ACCUMULATION/TRANSITIONAL"
}
```

### 5.2 Poids Adaptatifs

**Fonction**: `_adaptive_weights()` - Ligne 147

Poids déterminés par **VWAP regime** (prioritaire):

```python
# TRENDING (tendance institutionnelle forte)
weights = {"vwap": 0.50, "orderflow": 0.30, "footprint": 0.10, "momentum": 0.10}

# BALANCED (range, équilibre)
weights = {"vwap": 0.30, "orderflow": 0.35, "footprint": 0.25, "momentum": 0.10}

# ACCUMULATION (compression, micro-structure)
weights = {"vwap": 0.25, "orderflow": 0.35, "footprint": 0.30, "momentum": 0.10}

# TRANSITIONAL (chaos, changement)
weights = {"vwap": 0.20, "orderflow": 0.40, "footprint": 0.30, "momentum": 0.10}
```

### 5.3 Calcul Confiance Fusionnée

**Formule de base**:
```python
weighted_score = (
    (orderflow_score × w_of) +
    (footprint_score × w_fp) +
    (vwap_score × w_vw) +
    (momentum_score × w_mom)
)
```

**Bonus/Malus**:
```python
# MALUS conflits (directions opposées)
if conflicts >= 2: weighted_score *= 0.85  # -15%
elif conflicts == 1: weighted_score *= 0.92  # -8%

# BONUS alignement 3/3 (OF + FP + VWAP tous alignés)
if alignments >= 3: weighted_score += 0.05  # +5%

# Multiplicateur Timing
if timing_quality == "EXCELLENT": weighted_score *= 1.20
elif timing_quality == "GOOD": weighted_score *= 1.10
elif timing_quality == "POOR": weighted_score *= 0.85
elif timing_quality == "VETO": weighted_score *= 0.50
```

**Normalisation finale**:
```python
final_score = max(0.0, min(0.99, weighted_score))  # Clamp 0-0.99
```

### 5.4 Détermination Direction

**Vote majoritaire pondéré**:
```python
# Chaque composant vote avec son score comme poids
pos_votes = sum(weight for comp, dir, weight in votes if dir > 0)
neg_votes = sum(weight for comp, dir, weight in votes if dir < 0)

if pos_votes > neg_votes:
    direction = "BUY"
elif neg_votes > pos_votes:
    direction = "SELL"
else:
    # Égalité → Fallback VWAP bias
    direction = vwap_bias
```

---

## 6. FILTRES & VETOS

### 6.1 VETO MOMENTUM INSTITUTIONNEL

**Ligne**: 1388-1449
**Config**: `veto_rules.momentum_veto.enabled = true`

**Règle**: Bloquer trades incohérents avec momentum fort

```python
THRESHOLD = 45.0  # Score minimum pour veto

# VETO 1: BUY avec momentum BEARISH FORT
if direction == "BUY" and momentum_direction == "BEARISH" and momentum_score > 45:
    return HOLD  # "MOMENTUM_VETO_BUY"

# VETO 2: SELL avec momentum BULLISH FORT
if direction == "SELL" and momentum_direction == "BULLISH" and momentum_score > 45:
    return HOLD  # "MOMENTUM_VETO_SELL"
```

### 6.2 VETO MTF ALIGNMENT

**Ligne**: 1470-1534
**Config**: `veto_rules.mtf_alignment_veto.enabled = true`

**Règle**: Forcer alignement M1 et M3

```python
# Récupérer directions MTF
m1_direction = "BULLISH/BEARISH/NEUTRAL"  # 8 barres
m3_direction = "BULLISH/BEARISH/NEUTRAL"  # 6 barres
m5_direction = "BULLISH/BEARISH/NEUTRAL"  # 6 barres

# Check alignement (allow_neutral = true)
m1_m3_aligned = (
    m1_direction == m3_direction OR
    m1_direction == "NEUTRAL" OR
    m3_direction == "NEUTRAL"
)

if not m1_m3_aligned:
    return HOLD  # "MTF_ALIGNMENT_VETO"
```

### 6.3 RANGE REVERSAL LOGIC

**Fonction**: `_apply_range_reversal_logic()` - Ligne 1152

**Détection régime RANGE**:
```python
is_range = (
    vwap_regime == "BALANCED" OR
    "range" in phase_observer_regime OR
    "compression" in phase_observer_regime
)
```

**Logique d'inversion** (4 confirmations):
```python
# Haut du range + signal BUY → Potentiel SELL
if in_upper_tercile and direction == "BUY":
    confirmations = 0
    if buy_ratio < 0.45: confirmations += 1
    if orderflow_bias in ["BEARISH", "NEUTRAL"]: confirmations += 1
    if momentum_m1 == "BEARISH": confirmations += 1
    if range_pos > 0.85: confirmations += 1

    # Inverser si >= 60% confirmations (3/4)
    if confirmations / 4 >= 0.60:
        direction = "SELL"
```

### 6.4 BURST GUARD

**Ligne**: 3746-3765

```python
# Vérifier aucun panier burst déjà ouvert
open_burst_ids = extract_from_positions(comment ~ r"bs_[a-f0-9]{8}")

if open_burst_ids:
    return HOLD  # Un seul panier autorisé simultanément
```

---

## 7. DÉCISION FINALE

**Fonction**: `_final_decision()` - Ligne 1535-1581

### 7.1 Seuils de Confiance

**Config**: `config/strategy/config_trade_scalping.json`

```json
{
    "scoring_thresholds": {
        "high": 0.80,           // 80% - HIGH_CONVICTION
        "moderate": 0.75,       // 75% - MODERATE
        "cautious": 0.75,       // 75% - CAUTIOUS
        "conditional": 0.60     // 60% - CONDITIONAL (minimum)
    },
    "allow_conditional_entries": true
}
```

### 7.2 Classification Cascade

```python
if fused >= 0.80 and direction in ("BUY", "SELL"):
    signal_type = "HIGH_CONVICTION_" + direction
    action = direction
    ok = True

elif fused >= 0.75 and direction in ("BUY", "SELL"):
    signal_type = "MODERATE_" + direction
    action = direction
    ok = True

elif fused >= 0.75 and direction in ("BUY", "SELL"):
    signal_type = "CAUTIOUS_" + direction
    action = direction
    ok = True

elif allow_conditional and fused >= 0.60 and direction in ("BUY", "SELL"):
    signal_type = "CONDITIONAL_" + direction
    action = direction
    ok = True

else:
    signal_type = "WAIT_CONFIRMATION"
    action = "HOLD"
    ok = False
```

### 7.3 Retour FusionManager

```python
{
    "ok": bool,                      # True si exécutable
    "action": "BUY/SELL/HOLD",
    "signal_type": str,              # HIGH_CONVICTION_BUY, etc.
    "direction": "BUY/SELL/NEUTRAL",
    "fused_confidence": 0.0-0.99,
    "vwap_contribution": float,
    "momentum_contribution": float,
    "anchor_price": float,           # Prix VWAP
    "rationale": str,
    "components": {...},
    "consensus": {...},
    "quality": {...}
}
```

---

## 8. EXÉCUTION

### 8.1 Construction Trade Decision

**Ligne**: 3772-3806

```python
trade_decision = {
    "symbol": "USDJPY",
    "rule_name": "burst_scalping",
    "burst_size": 11,
    "action": "BUY" ou "SELL",
    "confidence": 0.75,
    "sltp": {
        "sl_method": "PIPS",
        "sl": {"pips": 50},      # 50 pips = 0.50 points USDJPY
        "tp": {"pips": 75},      # 75 pips = 0.75 points, RR 1.5
        "rr_base": 1.5
    },
    "context": {...},
    "fusion_data": {...}
}
```

### 8.2 Calcul SL/TP

**Pour USDJPY**:
```
1 pip USDJPY = 0.01 (2ème décimale)
1 point = 0.001

SL = 50 pips = 50 × 0.01 = 0.50 points
TP = 75 pips = 75 × 0.01 = 0.75 points

Prix entrée = 150.500
Prix SL (BUY) = 150.500 - 0.500 = 150.000
Prix TP (BUY) = 150.500 + 0.750 = 151.250
```

### 8.3 Volume Sizing

```python
burst_size = 11 ordres
lot_per_order = 0.10 lot USDJPY

total_exposition = 0.10 × 11 = 1.10 lots
```

### 8.4 Exécution Pipeline

**Fonction**: `run_trade_execution_pipeline()` - Ligne 3810

```python
1. Validation decision_package
2. Construction 11 ordres MT5 identiques
3. Génération basket_id unique (bs_XXXXXXXX)
4. Envoi séquentiel 11 ordres (mt5.order_send() × 11)
5. Tracking positions ouvertes
6. Monitoring temps réel (basket_monitor_thread)
```

**Format ordre MT5**:
```python
{
    "symbol": "USDJPY",
    "type": ORDER_TYPE_BUY,          # ou SELL
    "volume": 0.10,
    "price": current_ask/bid,
    "sl": price ± 0.50,
    "tp": price ± 0.75,
    "comment": "bs_12ab34cd_1/11",   # basket_id + leg
    "magic": 52001
}
```

---

## 9. SURVEILLANCE & FERMETURE

**Thread**: `basket_monitor_thread()` - Ligne 3863
**Intervalle**: Continu (polling 100ms)

### 9.1 Règles de Fermeture

**Config USDJPY**:
```python
{
    "enable_profit_close": true,
    "target_profit_pips": 60.0,              # +60 pips = close all
    "require_full_count_for_profit_close": true,  # 11/11 requis
    "rt_poll_interval_ms": 100,              # Check 100ms
    "min_age_ms_for_any_close": 2000,        # Âge min 2s

    "enable_loss_guard": true,
    "max_loss_pips": 80.0,                   # -80 pips = stop panier
    "loss_guard_arming_ms": 3000             # Actif après 3s
}
```

### 9.2 Logique Monitoring

```python
while basket_ouvert:
    # Toutes les 100ms

    # 1. Calculer PnL panier
    pnl_pips = sum(position.profit) / pip_value

    # 2. CHECK PROFIT TARGET
    if pnl_pips >= 60.0 and count == 11 and age >= 2000:
        close_all_positions(basket_id)
        log("✅ PROFIT +60 pips")
        break

    # 3. CHECK LOSS GUARD
    if pnl_pips <= -80.0 and age >= 3000:
        close_all_positions(basket_id)
        log("❌ LOSS -80 pips")
        break

    sleep(0.1)
```

---

## 10. DIAGRAMME DE FLUX COMPLET

```
┌─────────────────────────────────────────────────────────────────┐
│  MAIN THREAD                                                    │
│  ├─ ConfigManager.load()                                       │
│  ├─ MT5Connector.connect()                                     │
│  ├─ StrategyManager.init()                                     │
│  └─ Threads:                                                   │
│     ├─ ScalpingThread (5s) ← PRINCIPAL                        │
│     ├─ LiquidityThread (60s)                                   │
│     ├─ BasketMonitorThread (100ms)                             │
│     └─ DataEngine (background)                                 │
└─────────────────────────────────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────────┐
│  SCALPING THREAD (5 secondes)                                  │
└─────────────────────────────────────────────────────────────────┘
    │
    ├─ [1] COLLECTE DONNÉES
    │  ├─ rates_df (M1, 50 barres, cache 60s)
    │  ├─ footprint (cache 15s)
    │  ├─ df_m3 (8 barres)
    │  └─ df_m5 (6 barres)
    │
    ├─ [2] ANALYSE MARCHÉ
    │  └─ MarketAnalyzer.analyze()
    │     ├─ PhaseObserver (regime, range_pos)
    │     ├─ Patterns (EQH, EQL)
    │     └─ Features (ATR, BB)
    │
    ├─ [3] VWAP
    │  └─ VWAPAnalyzer.analyze()
    │     ├─ Calcul VWAP session
    │     ├─ Détection régime
    │     ├─ Distance & Slope
    │     └─ Score 0-1 + Bias
    │
    ├─ [4] ORDERFLOW V6
    │  └─ detect_orderflow_v6()
    │     ├─ calculate_volume_metrics()
    │     │  ├─ delta_total
    │     │  ├─ imbalance
    │     │  ├─ cvd / cvd_slope
    │     │  └─ tick_rate
    │     ├─ detect_patterns()
    │     ├─ detect_divergences()
    │     ├─ calculate_volume_profile()
    │     └─ calculate_score() → 0-100 pts
    │
    ├─ [5] FOOTPRINT V6 + TIMING
    │  └─ _analyze_footprint_v6()
    │     ├─ Extraction ticks bougie
    │     ├─ Timing Analyzer
    │     │  ├─ Distribution Q1/Q2/Q3/Q4
    │     │  ├─ Concentration Q1
    │     │  ├─ Velocity analysis
    │     │  └─ Quality → Multiplicateur
    │     └─ Footprint scoring → 0-25 pts
    │
    ├─ [6] MOMENTUM INSTITUTIONNEL
    │  └─ MomentumAnalyzerInstitutional.analyze()
    │     ├─ Candle Strength (30 pts)
    │     ├─ Volume Confirmation (25 pts)
    │     ├─ Price Acceleration (25 pts)
    │     ├─ MTF Alignment (20 pts)
    │     └─ Total 0-100 pts
    │
    ├─ [7] FUSION MANAGER
    │  └─ FusionManager.fuse()
    │     ├─ [7.1] Normalisation 0-1
    │     ├─ [7.2] Validation inputs
    │     ├─ [7.3] Poids adaptatifs (VWAP regime)
    │     ├─ [7.4] Calcul confiance fusionnée
    │     │  ├─ weighted_score
    │     │  ├─ Malus conflits
    │     │  ├─ Bonus alignement
    │     │  └─ Multiplicateur timing
    │     ├─ [7.5] Détermination direction
    │     │  ├─ Vote majoritaire
    │     │  └─ Range reversal
    │     ├─ [7.6] FILTRES VETO
    │     │  ├─ VETO Momentum (>45 opposé)
    │     │  ├─ VETO MTF (M1≠M3)
    │     │  └─ Entry Quality (config only)
    │     ├─ [7.7] Classification seuils
    │     │  ├─ ≥0.80: HIGH_CONVICTION
    │     │  ├─ ≥0.75: MODERATE/CAUTIOUS
    │     │  ├─ ≥0.60: CONDITIONAL
    │     │  └─ <0.60: WAIT_CONFIRMATION
    │     └─ [7.8] Retour {ok, action, ...}
    │
    ├─ [8] DÉCISION EXÉCUTION
    │  │
    │  ├─ IF fusion["ok"] == True:
    │  │  ├─ Burst Guard (check aucun panier)
    │  │  ├─ Injection valeurs dynamiques
    │  │  └─ run_trade_execution_pipeline()
    │  │     ├─ Construction 11 ordres MT5
    │  │     ├─ Génération basket_id
    │  │     ├─ Envoi séquentiel × 11
    │  │     └─ Tracking positions
    │  │
    │  └─ ELSE: Log WHY_NO_TRADE
    │
    └─ [9] SLEEP CYCLE
       └─ sleep(max(0, 5 - elapsed))

┌─────────────────────────────────────────────────────────────────┐
│  BASKET MONITOR (Continu, 100ms)                               │
└─────────────────────────────────────────────────────────────────┘
    │
    └─ BOUCLE (100ms polling)
       ├─ Récupérer positions basket
       ├─ Calculer PnL panier (pips)
       ├─ CHECK Profit Target (+60 pips)
       ├─ CHECK Loss Guard (-80 pips)
       └─ sleep(0.1)
```

---

## 11. LISTE EXHAUSTIVE DES FACTEURS

### 11.1 OrderFlow V6 (30-50%)

| Facteur | Poids | Seuil USDJPY | Impact |
|---------|-------|--------------|--------|
| delta_total | 30 pts | 15.0 (fort) | abs(delta)/15 × 30 |
| imbalance | 25 pts | 0.60/0.70 | 25 si ≥0.70, 15 si ≥0.60 |
| tick_rate | 20 pts | 1.5/3.0 | 20 si ≥3.0, 10 si ≥1.5 |
| divergence | 10 pts | N/A | +10 si détectée |
| vpoc_drift | 5 pts | <10 points | +5 si drift <10 |

**Total**: 0-100 pts | **Status**: VALID (≥28) / WEAK (≥14) / SUSPECT

### 11.2 Footprint (25-40%)

| Facteur | Poids | Seuil USDJPY | Impact |
|---------|-------|--------------|--------|
| tick_count | 5 pts | ≥30 | +5 pts |
| coverage_s | 5 pts | ≥10s | +5 pts |
| tick_rate | 5 pts | ≥1.5 | +5 pts |
| delta_total | 5 pts | Significatif | +5 pts |
| buy_ratio | 5 pts | Cohérent | +5 pts |

**Total**: 0-25 pts | **Status**: VALID (≥15) / WEAK (≥7.5) / SUSPECT

### 11.3 VWAP (20-50% selon régime)

| Facteur | Impact |
|---------|--------|
| regime | TRENDING(50%) / BALANCED(30%) / ACCUMULATION(25%) / TRANSITIONAL(20%) |
| bias | BUY/SELL/NEUTRAL |
| distance_pips | Validation zone |
| slope | >0 (bullish) / <0 (bearish) |

**Score**: 0-1.0

### 11.4 Momentum (10%)

| Composant | Poids | Total |
|-----------|-------|-------|
| Candle Strength | 30 pts | 100 pts |
| Volume Confirmation | 25 pts | |
| Price Acceleration | 25 pts | |
| MTF Alignment | 20 pts | |

**Quality**: EXCELLENT (≥65) / GOOD (≥50) / FAIR (≥35) / POOR

### 11.5 Timing (Multiplicateur)

| Quality | Multiplicateur |
|---------|----------------|
| EXCELLENT | × 1.20 |
| GOOD | × 1.10 |
| FAIR | × 1.00 |
| POOR | × 0.85 |
| VETO | × 0.50 |

### 11.6 Cohérence

| Facteur | Impact |
|---------|--------|
| Conflits 1 | -8% |
| Conflits 2+ | -15% |
| Alignement 3/3 | +5% |

---

## 12. SEUILS ET VALEURS PAR DÉFAUT

### 12.1 FusionManager

```json
{
    "scoring_thresholds": {
        "high": 0.80,
        "moderate": 0.75,
        "cautious": 0.75,
        "conditional": 0.60
    },
    "allow_conditional_entries": true
}
```

### 12.2 OrderFlow V6 (USDJPY)

```json
{
    "delta_abs_strong": 15.0,
    "imbalance_min": 0.55,
    "imbalance_extreme": 0.70,
    "tickrate_min": 1.5,
    "tickrate_burst_min": 3.0,
    "coverage_s_min": 3,
    "vpoc_drift_max_points": 10.0
}
```

### 12.3 Footprint (USDJPY)

```json
{
    "m1_min_ticks": 30,
    "m1_min_coverage_s": 10,
    "tickrate_min": 1.5
}
```

### 12.4 Timing (USDJPY)

```json
{
    "q1_expected_min": 0.35,
    "q1_strong": 0.50,
    "q1_weak": 0.25
}
```

### 12.5 Momentum

```json
{
    "bullish_threshold": 0.67,
    "bearish_threshold": 0.33,
    "excellent": 65,
    "good": 50,
    "fair": 35
}
```

### 12.6 VETO

```json
{
    "momentum_veto": {
        "strong_threshold": 45.0,
        "moderate_threshold": 30.0
    },
    "mtf_alignment_veto": {
        "require_m1_m3_aligned": true,
        "allow_neutral": true
    }
}
```

### 12.7 SL/TP (USDJPY)

```json
{
    "sl_pips": 50,
    "tp_pips": 75,
    "rr_base": 1.5
}
```

### 12.8 Closure Rules

```json
{
    "target_profit_pips": 60.0,
    "max_loss_pips": 80.0,
    "rt_poll_interval_ms": 100,
    "min_age_ms": 2000,
    "loss_guard_arming_ms": 3000
}
```

---

## 13. ZONES D'INCOHÉRENCE & BUGS

### 13.1 Incohérences Détectées

#### A. Seuils Identiques Moderate/Cautious
**Fichier**: `config/strategy/config_trade_scalping.json`
```json
"moderate": 0.75,
"cautious": 0.75    // IDENTIQUE!
```
**Impact**: Aucune distinction
**Recommandation**: Ajuster `cautious: 0.70`

#### B. Entry Quality VETO Non Implémenté
**Config**: `veto_rules.entry_quality_veto.enabled = true`
**Code**: Aucune implémentation trouvée
**Impact**: Config ignorée
**Recommandation**: Implémenter ou retirer

#### C. Timing Multiplier Application Floue
**Code**: Multiplicateur calculé mais application incertaine
**Impact**: Bonus timing peut être ignoré
**Recommandation**: Clarifier ordre application

#### D. MTF Allow Neutral Paradoxe
**Config**: `allow_neutral: true`
**Problème**: M1=NEUTRAL + M3=BEARISH + Trade BUY → Accepté
**Impact**: Accepter trades contre M3 si M1 indécis
**Recommandation**: Exiger M3 aligné, pas juste M1 neutral

### 13.2 Bugs Potentiels

#### A. Cache Footprint - Race Condition
**Problème**: DataEngine écrit cache, ScalpingThread lit cache (pas de lock)
**Risque**: Données partielles
**Recommandation**: threading.Lock

#### B. Range Reversal - Confirmations Non Pondérées
**Problème**: Toutes confirmations poids égal (25%)
**Recommandation**: Pondérer (range_pos 40%, OF 30%, momentum 20%, ratio 10%)

#### C. Seuil Momentum VETO 45 Non Validé
**Problème**: Seuil arbitraire
**Recommandation**: Backtester 35/40/45/50

#### D. Timing USDJPY 35% vs XAUUSD 60%
**Problème**: USDJPY très permissif (-42%)
**Recommandation**: Valider empiriquement

---

## 14. RECOMMANDATIONS

### Priorité 1 (CRITIQUE)

1. ✅ **Implémenter Entry Quality VETO** ou retirer config
2. ✅ **Clarifier Timing Multiplier** application
3. ✅ **Ajouter Lock Cache Footprint** (race condition)
4. ✅ **Valider Momentum VETO seuil 45** (backtester)

### Priorité 2 (IMPORTANT)

5. ✅ **Pondérer Range Reversal** confirmations
6. ✅ **Différencier Moderate/Cautious** seuils
7. ✅ **Coverage Footprint relatif** (vs absolu 10s)
8. ✅ **Valider Timing USDJPY 35%** (vs 60% XAUUSD)

### Priorité 3 (OPTIMISATION)

9. ✅ **Cache Momentum 60s** (éviter recalcul 5s)
10. ✅ **VWAP Reset Tokyo** timezone (vs UTC)
11. ✅ **Unifier Burst Size** hiérarchie résolution

---

## CONCLUSION

Le système SNIPER_X est une architecture sophistiquée multi-composants avec fusion adaptative. Les principaux points d'amélioration concernent:

1. **Validation empirique** des seuils arbitraires
2. **Clarification** de l'ordre d'application des bonus/malus
3. **Pondération** des confirmations Range Reversal
4. **Threading safety** pour le cache footprint

Le flow de décision est cohérent mais peut bénéficier d'ajustements basés sur données historiques réelles.

---

**Fin du rapport**
**Total lignes analysées**: 8000+
**Fichiers analysés**: 15+
**Date**: 24 Décembre 2025
