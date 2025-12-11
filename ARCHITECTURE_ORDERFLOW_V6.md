# ARCHITECTURE ORDERFLOW V6 - BURST SCALPING XAUUSD

## DOCUMENT TECHNIQUE COMPLET
**Date:** 2025-12-10
**Version:** V6 (Version actuelle)
**Fichier source:** `/home/workdev/sniper_x_dev/strategy/scalping.py`

---

## TABLE DES MATIÈRES

1. [Vue d'ensemble](#vue-densemble)
2. [Architecture complète du système](#architecture-complète-du-système)
3. [OrderFlow V6 Analysis (50 points)](#orderflow-v6-analysis-50-points)
4. [Footprint V6 Analysis (25 points)](#footprint-v6-analysis-25-points)
5. [VWAP Module (25 points)](#vwap-module-25-points)
6. [Scoring Final et Décision](#scoring-final-et-décision)
7. [Flux d'exécution complet](#flux-dexécution-complet)
8. [Dépendances et intégrations](#dépendances-et-intégrations)
9. [Implémentation dans le nouveau thread](#implémentation-dans-le-nouveau-thread)

---

## VUE D'ENSEMBLE

### Concept Global

Le système **OrderFlow V6** est un système de scoring **multi-composants** qui analyse le marché sur **3 timeframes** (M1, M5, M15) pour générer des signaux de burst scalping sur XAUUSD.

### Score Total : 100 points

```
┌─────────────────────────────────────────────────────────────┐
│                  SCORING BURST SCALPING                     │
├─────────────────────────────────────────────────────────────┤
│  OrderFlow V6   : 0-50 pts  (50% du score)                 │
│  Footprint V6   : 0-25 pts  (25% du score)                 │
│  VWAP Module    : 0-25 pts  (25% du score)                 │
│  ───────────────────────────────────────────────────────    │
│  TOTAL          : 0-100 pts                                 │
└─────────────────────────────────────────────────────────────┘
```

### Poids Dynamiques (selon régime VWAP)

Les poids s'adaptent automatiquement selon le régime de marché détecté par VWAP :

| Régime VWAP | OrderFlow | Footprint | VWAP |
|-------------|-----------|-----------|------|
| **TRENDING** | 40% | 40% | 20% |
| **BALANCED** | 35% | 35% | 30% |
| **REVERSAL** | 30% | 30% | 40% |

**Source:** `phase_observer/vwap/config.py` - Fonction `get_regime_weights()`

---

## ARCHITECTURE COMPLÈTE DU SYSTÈME

### 1. Point d'Entrée : `evaluate_entry()`

**Fichier:** `strategy/scalping.py` (ligne 859)

```python
def evaluate_entry(
    self,
    asset: str,                          # "XAUUSD"
    analyzed_context: Dict[str, Any],    # Contexte du marché
    asset_signals: Dict[str, Any],       # Signaux pré-calculés
) -> Dict[str, Any]:
```

**Rôle:**
- Point d'entrée principal appelé par le pipeline de décision
- Vérifie les conditions préliminaires (spread, ATR, config)
- Orchestre les 3 analyses (OrderFlow, Footprint, VWAP)
- Génère la décision finale de trading

**Flux:**
```
1. Vérifications préliminaires
   ├─ Config burst_scalping activée ?
   ├─ Spread < max_spread_pips ?
   └─ ATR > min_atr_m1_pips ?

2. Récupération DataFrames multi-timeframe
   ├─ df_m1  (200 barres actuellement)
   ├─ df_m5  (20 barres)
   └─ df_m15 (15 barres)

3. Analyse OrderFlow V6 → 0-50 pts

4. Analyse Footprint V6 → 0-25 pts

5. Récupération VWAP → 0-25 pts (calculé en amont)

6. Rapport consolidé (logs)

7. Décision finale burst_scalping
```

---

## ORDERFLOW V6 ANALYSIS (50 POINTS)

### Fonction : `_analyze_orderflow_v6()`

**Fichier:** `strategy/scalping.py` (ligne 86-388)

### Architecture OrderFlow V6

```
┌──────────────────────────────────────────────────────────────┐
│           ORDERFLOW V6 - MULTI-COMPOSANTS                    │
├──────────────────────────────────────────────────────────────┤
│                                                               │
│  0. ANALYSE MULTI-TIMEFRAME (MTF)                            │
│     ├─ M1  : 8 bougies  → Momentum immédiat                 │
│     ├─ M5  : 6 bougies  → Structure court terme             │
│     └─ M15 : 4 bougies  → Contexte moyen terme              │
│                                                               │
│  1. DELTA MOMENTUM (0-25 points)                             │
│     ├─ Delta total (buy_volume - sell_volume)                │
│     ├─ Cohérence sur 10 bougies M1                           │
│     └─ Direction (bullish/bearish/neutral)                   │
│                                                               │
│  2. VOLUME CONFIRMATION (0-15 points)                        │
│     ├─ Tick count temps réel (footprint)                     │
│     ├─ Ratio vs moyenne 14 bougies                           │
│     ├─ POC (Point of Control)                                │
│     └─ Spike detected (volume > 2x moyenne)                  │
│                                                               │
│  3. IMBALANCE STRENGTH (0-10 points)                         │
│     ├─ Imbalance buy count                                   │
│     ├─ Imbalance sell count                                  │
│     └─ Total imbalances détectées                            │
│                                                               │
└──────────────────────────────────────────────────────────────┘
```

### 0. ANALYSE MULTI-TIMEFRAME

**Objectif:** Déterminer l'alignement des timeframes pour renforcer le signal

**Périodes strictes:**
- **M1** : 8 bougies (dernières 8 minutes)
- **M5** : 6 bougies (dernières 30 minutes)
- **M15** : 4 bougies (dernières 60 minutes)

**Code (lignes 128-200):**

```python
# M1 : 8 bougies → Momentum immédiat
if df_m1 is not None and len(df_m1) >= 8:
    m1_closes = df_m1["close"].tail(8).values
    m1_opens = df_m1["open"].tail(8).values
    m1_bullish = sum(1 for i in range(len(m1_closes)) if m1_closes[i] > m1_opens[i])
    m1_bearish = 8 - m1_bullish

    if m1_bullish >= 6:  # 6/8 haussier (75%)
        result["mtf_alignment"]["m1"] = "bullish"
    elif m1_bearish >= 6:  # 6/8 baissier (75%)
        result["mtf_alignment"]["m1"] = "bearish"
    else:
        result["mtf_alignment"]["m1"] = "neutral"
```

**Seuils d'alignement:**
- M1 : 6/8 bougies (75%)
- M5 : 5/6 bougies (83%)
- M15 : 3/4 bougies (75%)

**Bonus MTF Aligned:**
- Si les 3 timeframes sont alignés (même direction) → Flag `mtf_aligned = True`
- Utilisé comme **bonus psychologique** dans l'analyse

---

### 1. DELTA MOMENTUM (0-25 POINTS)

**Objectif:** Mesurer le déséquilibre acheteur/vendeur et sa cohérence

**Données sources:**
- `footprint_summary` (depuis asset_signals)
  - `delta_total` = buy_volume - sell_volume
- `df_m1` : Dernières 10 bougies M1

**Calcul (lignes 203-281):**

```python
# 1. Récupérer delta_total depuis footprint_summary
fp_summary = asset_signals.get("footprint_summary", {})
delta_total = float(fp_summary.get("delta_total", 0))

# 2. Analyser cohérence delta sur 10 bougies M1
closes = df_m1["close"].tail(10).values
opens = df_m1["open"].tail(10).values
bullish_count = sum(1 for i in range(len(closes)) if closes[i] > opens[i])
bearish_count = sum(1 for i in range(len(closes)) if closes[i] < opens[i])

coherence = max(bullish_count, bearish_count) / 10.0  # 0.0 à 1.0
```

**Scoring (seuils adaptés au scalping M1):**

| Cohérence | Delta Total | Score | Interprétation |
|-----------|-------------|-------|----------------|
| **≥ 0.8** (8/10) | ≥ 50 | 25.0 | Très fort |
| | ≥ 30 | 20.0 | Fort |
| | ≥ 15 | 18.0 | Moyen-Fort |
| | ≥ 5 | 15.0 | Moyen |
| **≥ 0.7** (7/10) | ≥ 30 | 15.0 | Modéré |
| | ≥ 15 | 12.0 | Modéré |
| | ≥ 5 | 10.0 | Faible-Modéré |
| **≥ 0.6** (6/10) | ≥ 15 | 10.0 | Faible cohérence |
| | < 15 | 7.0 | |
| **< 0.6** | - | 5.0 | Très faible |

**Exemples delta_total (180 ticks):**
- Delta = 50 → ~28% déséquilibre (ex: 114 buy / 66 sell)
- Delta = 30 → ~17% déséquilibre (ex: 105 buy / 75 sell)
- Delta = 15 → ~8% déséquilibre (ex: 97 buy / 83 sell)
- Delta = 5 → ~3% déséquilibre (ex: 92 buy / 88 sell)

**Output:**
```python
{
    "delta_momentum_score": 0-25,
    "delta_momentum_details": {
        "delta_total": float,
        "coherence": 0.0-1.0,
        "bullish_bars": int,
        "bearish_bars": int,
        "direction": "bullish"/"bearish"/"neutral"
    }
}
```

---

### 2. VOLUME CONFIRMATION (0-15 POINTS)

**Objectif:** Confirmer le signal par une analyse de volume en temps réel

**Données sources:**
- `footprint_summary.tick_count` : Volume tick temps réel (bougie courante)
- `df_m1["tick_volume"]` : Historique volume 14 bougies précédentes
- `footprint_summary.poc` : Point of Control (prix avec le plus de volume)

**Calcul (lignes 283-334):**

```python
# 1. Volume temps réel depuis footprint
current_tick_count = int(fp_summary.get("tick_count", 0))

# 2. Moyenne sur 14 bougies COMPLÈTES (exclure la dernière en cours)
historical_volumes = df_m1["tick_volume"].tail(15).values[:-1]  # 14 dernières
avg_volume = np.mean(historical_volumes)

# 3. Ratio volume courant vs moyenne
volume_ratio = current_tick_count / avg_volume
```

**Scoring:**

| Volume Ratio | Score | Interprétation |
|--------------|-------|----------------|
| ≥ 2.0 | 15.0 | Spike significatif |
| ≥ 1.5 | 12.0 | Volume élevé |
| ≥ 1.2 | 10.0 | Volume au-dessus moyenne |
| ≥ 0.8 | 7.0 | Volume normal (±20%) |
| ≥ 0.5 | 3.0 | Volume modéré |
| < 0.5 | 0.0 | Volume très faible |

**POC (Point of Control):**
- Prix où le volume est le plus concentré
- Récupéré directement depuis `footprint_summary.poc`
- Utilisé comme niveau clé pour entrées

**Output:**
```python
{
    "volume_confirmation_score": 0-15,
    "volume_confirmation_details": {
        "current_volume": float,
        "avg_volume": float,
        "ratio": float,
        "spike_detected": bool,
        "poc": float
    }
}
```

---

### 3. IMBALANCE STRENGTH (0-10 POINTS)

**Objectif:** Détecter les zones de déséquilibre fort (imbalances)

**Données sources:**
- `footprint_summary.imbalance_buy` : Nombre d'imbalances acheteurs
- `footprint_summary.imbalance_sell` : Nombre d'imbalances vendeurs

**Calcul (lignes 336-365):**

```python
imbalance_buy = int(fp_summary.get("imbalance_buy", 0))
imbalance_sell = int(fp_summary.get("imbalance_sell", 0))
total_imbalances = imbalance_buy + imbalance_sell
```

**Scoring:**

| Total Imbalances | Score | Interprétation |
|------------------|-------|----------------|
| ≥ 5 | 10.0 | Beaucoup d'imbalances |
| ≥ 3 | 8.0 | Imbalances significatives |
| ≥ 1 | 5.0 | Imbalances mineures |
| 0 | 0.0 | Aucune imbalance |

**Output:**
```python
{
    "imbalance_strength_score": 0-10,
    "imbalance_strength_details": {
        "imbalance_buy": int,
        "imbalance_sell": int,
        "m1_count": int,
        "total_count": int
    }
}
```

---

### RÉSULTAT ORDERFLOW V6

**Format de sortie (ligne 117-124):**

```python
{
    "delta_momentum_score": 0-25,
    "volume_confirmation_score": 0-15,
    "imbalance_strength_score": 0-10,
    "total_score": 0-50,  # Somme des 3 composants
    "mtf_alignment": {
        "m1": "bullish"/"bearish"/"neutral",
        "m5": "bullish"/"bearish"/"neutral",
        "m15": "bullish"/"bearish"/"neutral"
    },
    "mtf_aligned": bool,  # True si les 3 timeframes alignés
    "details": {
        "mtf": {...},
        "delta_momentum": {...},
        "volume_confirmation": {...},
        "imbalance_strength": {...}
    }
}
```

---

## FOOTPRINT V6 ANALYSIS (25 POINTS)

### Fonction : `_analyze_footprint_v6()`

**Fichier:** `strategy/scalping.py` (ligne 473-696)

### Architecture Footprint V6

```
┌──────────────────────────────────────────────────────────────┐
│           FOOTPRINT V6 - TICKS TEMPS RÉEL                    │
├──────────────────────────────────────────────────────────────┤
│                                                               │
│  Focus : Bougie courante (0-59 secondes)                     │
│  Contexte : 3 bougies précédentes pour confirmation          │
│                                                               │
│  1. ABSORPTION LEVELS (0-12.5 points)                        │
│     ├─ Buy volume / Sell volume                              │
│     ├─ Ratios buy/sell                                       │
│     └─ Bias (STRONG BULLISH/BEARISH/NEUTRAL)                │
│                                                               │
│  2. ORDER CLUSTERING (0-8.5 points)                          │
│     ├─ Analyse 4 dernières bougies                           │
│     ├─ Clusters détectés (volume concentré / range faible)  │
│     └─ Distribution (concentrated/dispersed)                 │
│                                                               │
│  3. PRICE REJECTION (0-4.0 points)                           │
│     ├─ Analyse wicks sur 3 dernières bougies                │
│     ├─ Wick ratio (wick / body)                              │
│     └─ Strength (strong/moderate/weak/none)                  │
│                                                               │
└──────────────────────────────────────────────────────────────┘
```

---

### 1. ABSORPTION LEVELS (0-12.5 POINTS)

**Objectif:** Mesurer le ratio acheteurs/vendeurs pour déterminer la pression dominante

**Données sources:**
- `footprint_summary.buy_volume` : Volume achats
- `footprint_summary.sell_volume` : Volume ventes

**Calcul (lignes 527-580):**

```python
# Récupérer volumes depuis footprint
buy_vol = float(fp_summary.get("buy_volume", 0))
sell_vol = float(fp_summary.get("sell_volume", 0))
total_vol = buy_vol + sell_vol

# Calculer ratios
buy_ratio = buy_vol / total_vol
sell_ratio = sell_vol / total_vol
```

**Scoring:**

| Condition | Score | Bias |
|-----------|-------|------|
| buy_ratio ≥ 0.75 (75%+) | 12.5 | STRONG BULLISH |
| buy_ratio ≥ 0.65 (65%+) | 10.0 | BULLISH |
| sell_ratio ≥ 0.75 (75%+) | 12.5 | STRONG BEARISH |
| sell_ratio ≥ 0.65 (65%+) | 10.0 | BEARISH |
| Autre | 4.0 | NEUTRAL |

**Output:**
```python
{
    "absorption_levels_score": 0-12.5,
    "absorption_details": {
        "buy_volume": float,
        "sell_volume": float,
        "buy_ratio": float,
        "sell_ratio": float,
        "bias": str
    }
}
```

---

### 2. ORDER CLUSTERING (0-8.5 POINTS)

**Objectif:** Détecter les zones de concentration d'ordres (clusters)

**Principe:**
- Volume élevé + Range faible = Ordres concentrés (cluster)
- Analyse sur 4 dernières bougies M1

**Calcul (lignes 582-626):**

```python
# Récupérer données des 4 dernières bougies
volumes = df_m1["tick_volume"].tail(4).values
ranges = (df_m1["high"] - df_m1["low"]).tail(4).values

# Calculer volume par pip (éviter division par 0)
ranges_safe = np.maximum(ranges, 1e-9)
vol_per_pip_arr = volumes / ranges_safe
median_vol_per_pip = np.median(vol_per_pip_arr)

# Compter clusters (si vol_per_pip > médiane)
cluster_count = 0
for i in range(len(volumes)):
    if vol_per_pip_arr[i] > median_vol_per_pip:
        cluster_count += 1
```

**Scoring:**

| Cluster Count | Score | Distribution |
|---------------|-------|-------------|
| ≥ 3 | 8.5 | concentrated |
| ≥ 2 | 6.0 | concentrated |
| ≥ 1 | 3.5 | dispersed |
| 0 | 0.0 | dispersed |

**Output:**
```python
{
    "order_clustering_score": 0-8.5,
    "clustering_details": {
        "cluster_count": int,
        "distribution": "concentrated"/"dispersed"
    }
}
```

---

### 3. PRICE REJECTION (0-4.0 POINTS)

**Objectif:** Détecter les rejets de prix (wicks importants)

**Principe:**
- Wick > 2x body = Rejet net
- Analyse sur 3 dernières bougies M1

**Calcul (lignes 628-675):**

```python
last_bars = df_m1.tail(3)

rejection_count = 0
for idx, row in last_bars.iterrows():
    high_val = row["high"]
    low_val = row["low"]
    open_val = row["open"]
    close_val = row["close"]

    body = abs(close_val - open_val)
    full_range = high_val - low_val

    if full_range > 0:
        upper_wick = high_val - max(open_val, close_val)
        lower_wick = min(open_val, close_val) - low_val

        wick_ratio = max(upper_wick, lower_wick) / body if body > 0 else 0

        # Rejet net si wick > 2x body
        if wick_ratio >= 2.0:
            rejection_count += 1
```

**Scoring:**

| Rejection Count | Score | Strength |
|-----------------|-------|----------|
| 3 | 4.0 | strong |
| 2 | 2.5 | moderate |
| 1 | 1.5 | weak |
| 0 | 0.0 | none |

**Output:**
```python
{
    "price_rejection_score": 0-4.0,
    "rejection_details": {
        "rejection_bars": int,
        "strength": str
    }
}
```

---

### RÉSULTAT FOOTPRINT V6

**Format de sortie:**

```python
{
    "absorption_levels_score": 0-12.5,
    "order_clustering_score": 0-8.5,
    "price_rejection_score": 0-4.0,
    "total_score": 0-25,  # Somme des 3 composants
    "details": {
        "absorption": {...},
        "clustering": {...},
        "rejection": {...}
    }
}
```

---

## VWAP MODULE (25 POINTS)

### Source : `phase_observer/vwap/analyzer.py`

**Note importante:** Le VWAP est calculé **en amont** par le système `phase_observer` et stocké dans `asset_signals["__latest__"]`.

### Récupération dans ScalpingStrategy

**Code (lignes 1232-1242):**

```python
# Récupérer le score VWAP depuis asset_signals
vwap_score_pct = 0.0
vwap_status = "N/A"
vwap_regime = None

latest_signals = asset_signals.get("__latest__", {})
vwap_score_pct = float(latest_signals.get("vwap_score", 0.0)) * 100.0  # 0-1 → 0-100
vwap_status = str(latest_signals.get("vwap_status", "N/A"))
vwap_regime = latest_signals.get("vwap_regime")  # TRENDING/BALANCED/REVERSAL
```

### Composants VWAP

Le module VWAP analyse :

1. **Trend Component** (15 pts max)
   - Slope VWAP (pente)
   - Direction de la tendance
   - Pondéré selon régime VWAP

2. **Position Component** (10 pts max)
   - Distance prix / VWAP
   - Zone (STRONG/NORMAL/WEAK)
   - Bandes VWAP (upper/lower touch)

### Régimes VWAP

| Régime | Caractéristiques | Poids Dynamiques |
|--------|------------------|------------------|
| **TRENDING** | Tendance claire, pente forte | OF=40% FP=40% VW=20% |
| **BALANCED** | Marché équilibré autour VWAP | OF=35% FP=35% VW=30% |
| **REVERSAL** | Signal de retournement | OF=30% FP=30% VW=40% |

---

## SCORING FINAL ET DÉCISION

### Calcul Score Final

**Code (lignes 1226-1230):**

```python
# Scores bruts (sur 75 points : OF=50 + FP=25)
orderflow_score = orderflow_result.get("total_score", 0.0)  # 0-50
footprint_score = footprint_result.get("total_score", 0.0)  # 0-25
final_score = orderflow_score + footprint_score  # 0-75

# VWAP ajouté séparément (0-25 pts)
vwap_score_pts = (vwap_score_pct / 100.0) * 25.0
```

### Score Total sur 100 points

```
SCORE TOTAL = OrderFlow (0-50) + Footprint (0-25) + VWAP (0-25)
```

**Avec poids dynamiques (exemple BALANCED) :**

```
SCORE FINAL = OF*0.35 + FP*0.35 + VWAP*0.30
```

---

### Rapport Consolidé

**Fonction:** `_log_orderflow_consolidated_report()` (lignes 699-856)

**Format du rapport:**

```
======================================================================
📊 ORDERFLOW V6 - ANALYSE BURST SCALPING [XAUUSD]
======================================================================

⏱️  PÉRIODES MULTI-TIMEFRAME :
   • M1  (8 bougies)  → Momentum : BULLISH
   • M5  (6 bougies)  → Structure : BULLISH
   • M15 (4 bougies)  → Contexte  : NEUTRAL
   ✅ ALIGNEMENT MTF DÉTECTÉ

📈 ORDERFLOW ANALYSIS (35% du total) : 42.0/50 points
   ├─ Delta Momentum      : 20.0/25 pts
   │  • Delta total       : 35
   │  • Cohérence         : 80%
   │  • Direction         : BULLISH
   ├─ Volume Confirmation : 15.0/15 pts
   │  • Volume ratio      : 2.15x
   │  • Spike détecté     : OUI
   │  • POC (Point of Control) : 2658.42
   └─ Imbalance Strength  : 7.0/10 pts
      • Imbalances M1    : 3 détectées
      • Imbalances M5    : 0 détectées

👣 FOOTPRINT ANALYSIS (35% du total) : 19.0/25 points
   ├─ Absorption Levels   : 12.5/12.5 pts
   │  • Biais absorption  : STRONG BULLISH
   │  • Buy ratio         : 78%
   │  • Sell ratio        : 22%
   ├─ Order Clustering    : 6.0/8.5 pts
   │  • Clusters détectés : 2
   │  • Distribution      : concentrated
   └─ Price Rejection     : 0.5/4.0 pts
      • Rejets détectés   : 0/3
      • Force rejet       : none

📊 VWAP INSTITUTIONNEL (30% du scoring)
   Score VWAP      : 18.2/25 pts (72.8%)
   Status          : VALID

======================================================================
🎯 SCORE FINAL BURST SCALPING
======================================================================
   OrderFlow (35%) : 42.0/50 pts
   Footprint (35%) : 19.0/25 pts
   VWAP (30%)      : 18.2/25 pts
   ──────────────────────────────────────────────────
   TOTAL (OF+FP+VWAP) : 79.2/100 pts

   🟢 Direction recommandée : BUY
======================================================================
```

---

### Décision Burst Scalping

**Code (lignes 1265-1291):**

```python
entry_mode = str(sm_cfg.get("entry_mode", "MARKET")).upper()
burst_sz = int(sm_cfg.get("burst_size", 5) or 5)

sm_decision = {
    "strategy_type": "scalping",
    "rule_name": "burst_scalping",
    "execution_status": "ready",
    "action": action,  # BUY/SELL
    "asset": asset,
    "order_type": entry_mode,  # MARKET / BUY_LIMIT / SELL_LIMIT
    "entry_price": (float(price) if entry_mode != "MARKET" else None),
    "burst_size": burst_sz,
    "fusion_data": {
        "fused_confidence": final_score / 100.0,
        "orderflow": orderflow_result,
        "footprint": footprint_result,
    },
    "meta": {
        "burst": True,
        "entry_source": "core_decision",
        "per_leg_virtual": bool(sm_cfg.get("per_leg_virtual", True)),
        "atr_m1_pips": atr_m1_pips,
        "orderflow_v6_score": final_score,
    },
}
```

---

## FLUX D'EXÉCUTION COMPLET

### Diagramme de Flux

```
┌─────────────────────────────────────────────────────────────────┐
│                 FLUX COMPLET ORDERFLOW V6                       │
└─────────────────────────────────────────────────────────────────┘

1. APPEL DEPUIS run_bot.py (SCALPING Thread)
   └─> strategy_manager.evaluate_entry(asset="XAUUSD", ...)

2. ScalpingStrategy.evaluate_entry()
   │
   ├─> Vérifications préliminaires
   │   ├─ burst_scalping enabled ?
   │   ├─ Spread < max_spread_pips ?
   │   └─ ATR > min_atr_m1_pips ?
   │
   ├─> Récupération DataFrames multi-timeframe
   │   ├─ df_m1  (depuis analyzed_context ou MT5)
   │   ├─ df_m5  (depuis analyzed_context ou MT5)
   │   └─ df_m15 (depuis analyzed_context ou MT5)
   │
   ├─> _analyze_orderflow_v6(df_m1, df_m5, df_m15, asset_signals)
   │   │
   │   ├─> 0. MTF Alignment (M1/M5/M15)
   │   │   └─> mtf_aligned = True/False
   │   │
   │   ├─> 1. Delta Momentum (0-25 pts)
   │   │   ├─ Récupère footprint_summary.delta_total
   │   │   ├─ Calcule cohérence sur 10 bougies M1
   │   │   └─ Score selon seuils
   │   │
   │   ├─> 2. Volume Confirmation (0-15 pts)
   │   │   ├─ Récupère tick_count temps réel
   │   │   ├─ Calcule ratio vs moyenne 14 bougies
   │   │   ├─ Détecte spike
   │   │   └─ Score selon ratio
   │   │
   │   └─> 3. Imbalance Strength (0-10 pts)
   │       ├─ Compte imbalances buy/sell
   │       └─ Score selon total
   │
   ├─> _analyze_footprint_v6(df_m1, asset_signals)
   │   │
   │   ├─> 1. Absorption Levels (0-12.5 pts)
   │   │   ├─ Calcule buy_ratio / sell_ratio
   │   │   └─ Score selon ratio dominant
   │   │
   │   ├─> 2. Order Clustering (0-8.5 pts)
   │   │   ├─ Analyse 4 dernières bougies
   │   │   ├─ Calcule volume/pip
   │   │   └─ Compte clusters
   │   │
   │   └─> 3. Price Rejection (0-4.0 pts)
   │       ├─ Analyse wicks sur 3 bougies
   │       └─ Compte rejets (wick > 2x body)
   │
   ├─> Récupération VWAP (depuis asset_signals["__latest__"])
   │   ├─ vwap_score_pct (0-100%)
   │   ├─ vwap_status (VALID/WEAK/SUSPECT)
   │   └─ vwap_regime (TRENDING/BALANCED/REVERSAL)
   │
   ├─> _log_orderflow_consolidated_report()
   │   └─> Affiche rapport formaté dans les logs
   │
   └─> Retourne décision burst_scalping
       {
           "strategy_type": "scalping",
           "rule_name": "burst_scalping",
           "action": "BUY"/"SELL",
           "burst_size": 5-8,
           "fusion_data": {
               "fused_confidence": 0.0-1.0,
               "orderflow": {...},
               "footprint": {...}
           },
           "meta": {...}
       }

3. RETOUR À run_bot.py
   └─> run_trade_execution_pipeline(decision)
       └─> TradeExecutor.execute_decision()
           └─> Exécution burst scalping (5-8 positions)
```

---

## DÉPENDANCES ET INTÉGRATIONS

### 1. Dépendances Directes

**Modules Python:**
```python
import numpy as np
import pandas as pd
from phase_observer.vwap.config import get_regime_weights
```

**Classes/Modules internes:**
- `BaseStrategy` : Classe parent
- `ConfigManager` : Configuration dynamique
- `MT5Connector` : Récupération données MT5

---

### 2. Données Entrantes (asset_signals)

Le dictionnaire `asset_signals` doit contenir :

```python
asset_signals = {
    "footprint_summary": {
        # Depuis DataEngine Thread (footprint_cache)
        "delta_total": float,          # buy_volume - sell_volume
        "buy_volume": float,           # Volume achats
        "sell_volume": float,          # Volume ventes
        "tick_count": int,             # Nombre de ticks
        "imbalance_buy": int,          # Nombre imbalances buy
        "imbalance_sell": int,         # Nombre imbalances sell
        "poc": float,                  # Point of Control (prix)
        "coverage_s": float,           # Couverture en secondes
        "tick_rate": float             # Ticks par seconde
    },
    "__latest__": {
        # Depuis VWAP Analyzer (phase_observer)
        "vwap_score": float,           # 0.0-1.0
        "vwap_status": str,            # "VALID"/"WEAK"/"SUSPECT"
        "vwap_regime": str,            # "TRENDING"/"BALANCED"/"REVERSAL"
        "vwap_bias": str               # "BUY"/"SELL"/"NEUTRAL"
    }
}
```

---

### 3. Données Entrantes (analyzed_context)

```python
analyzed_context = {
    "market_data": {
        "XAUUSD": {
            "annotated_rates_df_m1": pd.DataFrame,   # 200 barres M1
            "annotated_rates_df_m5": pd.DataFrame,   # 20 barres M5
            "annotated_rates_df_m15": pd.DataFrame,  # 15 barres M15
            "current_price": float,
            "spread_pips": float,
            "atr_m1_pips": float
        }
    },
    "cycle_count": int,
    "daily_trade_count": int
}
```

---

### 4. Intégration avec DataEngine Thread

**Flux de données:**

```
DATA_ENGINE Thread (cycle 5s)
    ├─> get_rates('XAUUSD', M1, 50)
    ├─> get_ticks_for_candle()
    ├─> MarketAnalyzer.analyze(df, ticks)
    │   └─> Calcule footprint:
    │       - delta_total
    │       - buy_volume / sell_volume
    │       - imbalances
    │       - poc
    │       - tick_count
    └─> footprint_cache.update(symbol, footprint_summary)

SCALPING Thread (cycle 5s)
    ├─> footprint_cache.get('XAUUSD')
    └─> asset_signals["footprint_summary"] = footprint_cache
```

**Important:** Le footprint est calculé par DataEngine sur **50 barres M1**, mais OrderFlow V6 utilise les **données temps réel** (tick_count, delta_total) de la bougie courante.

---

### 5. Intégration avec VWAP Module

**Flux de données:**

```
VWAP Analyzer (dans MarketAnalyzer)
    ├─> Calcule VWAP de session
    ├─> Détecte régime (TRENDING/BALANCED/REVERSAL)
    ├─> Calcule score 0-1.0
    └─> Stocke dans asset_signals["__latest__"]

ScalpingStrategy
    └─> Récupère vwap_score, vwap_status, vwap_regime
```

---

## IMPLÉMENTATION DANS LE NOUVEAU THREAD

### Adaptation pour 20-25 Barres M1

**Modifications nécessaires:**

#### 1. Delta Momentum
- **Actuellement:** Cohérence sur 10 bougies M1
- **Nouveau:** Cohérence sur **8-10 bougies** max (adapter si < 25 barres)

```python
# Adapter lookback selon barres disponibles
lookback_delta = min(10, len(df_m1) - 5)  # Au moins 5 barres de marge
closes = df_m1["close"].tail(lookback_delta).values
```

#### 2. Volume Confirmation
- **Actuellement:** Moyenne sur 14 bougies précédentes
- **Nouveau:** Moyenne sur **12 bougies** (si 25 barres total)

```python
# Moyenne sur 12 bougies si moins de 25 barres disponibles
lookback_volume = min(14, len(df_m1) - 2)
historical_volumes = df_m1["tick_volume"].tail(lookback_volume + 1).values[:-1]
```

#### 3. Order Clustering
- **Actuellement:** 4 dernières bougies
- **Nouveau:** **3-4 bougies** (OK avec 25 barres)

```python
# OK inchangé
volumes = df_m1["tick_volume"].tail(4).values
```

#### 4. Price Rejection
- **Actuellement:** 3 dernières bougies
- **Nouveau:** **3 bougies** (OK avec 25 barres)

```python
# OK inchangé
last_bars = df_m1.tail(3)
```

---

### Structure du Nouveau Thread

```python
# Dans le nouveau SCALPING Thread optimisé

def scalping_reactive_thread():
    """
    Thread scalping réactif avec analyse 20-25 barres M1
    """
    while not stop_event.is_set():
        try:
            # 1. Récupérer 25 barres M1 (au lieu de 200)
            df_m1 = mt5_connector.get_rates('XAUUSD', mt5.TIMEFRAME_M1, bars=25)

            # 2. Récupérer footprint depuis cache (DataEngine)
            footprint_summary = footprint_cache.get('XAUUSD')

            # 3. Récupérer M5/M15 pour MTF alignment
            df_m5 = mt5_connector.get_rates('XAUUSD', mt5.TIMEFRAME_M5, bars=6)
            df_m15 = mt5_connector.get_rates('XAUUSD', mt5.TIMEFRAME_M15, bars=4)

            # 4. Construire asset_signals
            asset_signals = {
                "footprint_summary": footprint_summary,
                "__latest__": {
                    "vwap_score": vwap_score,
                    "vwap_status": vwap_status,
                    "vwap_regime": vwap_regime
                }
            }

            # 5. Appeler OrderFlow V6
            orderflow_result = scalping_strategy._analyze_orderflow_v6(
                asset='XAUUSD',
                df_m1=df_m1,
                df_m5=df_m5,
                df_m15=df_m15,
                asset_signals=asset_signals
            )

            # 6. Appeler Footprint V6
            footprint_result = scalping_strategy._analyze_footprint_v6(
                asset='XAUUSD',
                df_m1=df_m1,
                asset_signals=asset_signals
            )

            # 7. Calculer score final
            final_score = orderflow_result['total_score'] + footprint_result['total_score']

            # 8. Décider si trade
            if final_score >= seuil_trading:
                execute_burst_scalping(...)

            # 9. Sleep jusqu'au prochain cycle (5s)
            time.sleep(5.0)

        except Exception as e:
            logger.error(f"Erreur dans scalping thread: {e}")
            time.sleep(5.0)
```

---

### Points d'Attention

#### ✅ Compatible avec 25 Barres
- Delta Momentum : OK (10 bougies lookback)
- Volume Confirmation : OK (14 bougies lookback)
- Order Clustering : OK (4 bougies)
- Price Rejection : OK (3 bougies)
- **Total requis:** 14-15 barres minimum → 25 barres OK

#### ⚠️ Footprint Cache Dependency
- Le footprint reste calculé par **DataEngine** sur 50 barres
- OrderFlow V6 utilise uniquement les **données temps réel** de la bougie courante
- Pas d'impact sur le passage à 25 barres

#### ⚠️ MTF Alignment
- M5 : 6 bougies (30 minutes) - À récupérer via MT5
- M15 : 4 bougies (60 minutes) - À récupérer via MT5
- **Solution:** Récupérer M5/M15 séparément dans le thread

---

### Fichiers à Créer/Modifier

**Nouveaux fichiers (optionnel):**
```
core/
└── scalping_orderflow_v6.py  # Module standalone OrderFlow V6
    ├── analyze_orderflow_v6()
    ├── analyze_footprint_v6()
    └── log_consolidated_report()
```

**Modifications:**
```
run_bot.py (ligne ~3193)
└── Changer bars=200 → bars=25

strategy/scalping.py
└── Adapter lookback windows si besoin
```

---

## RÉSUMÉ TECHNIQUE

### Points Clés

1. **OrderFlow V6 = 50 pts** (Delta 25 + Volume 15 + Imbalance 10)
2. **Footprint V6 = 25 pts** (Absorption 12.5 + Clustering 8.5 + Rejection 4)
3. **VWAP = 25 pts** (calculé en amont)
4. **Total = 100 pts**
5. **Poids dynamiques** selon régime VWAP
6. **Compatible avec 25 barres M1**

### Dépendances Critiques

- `footprint_cache` (DataEngine Thread)
- `asset_signals["__latest__"]` (VWAP Analyzer)
- `df_m1, df_m5, df_m15` (MT5Connector)

### Timeframes Utilisés

| Composant | M1 | M5 | M15 |
|-----------|----|----|-----|
| MTF Alignment | 8 barres | 6 barres | 4 barres |
| Delta Momentum | 10 barres | - | - |
| Volume Confirmation | 14 barres | - | - |
| Clustering | 4 barres | - | - |
| Rejection | 3 barres | - | - |

**Minimum requis M1:** 15 barres (pour volume confirmation)
**Recommandé:** 25 barres (marge de sécurité)

---

*Document créé le 2025-12-10*
*Basé sur ScalpingStrategy V6 (commit d03e1e5)*
