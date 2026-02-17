# SESSION 16 FEVRIER 2026 — MTF Queen V3 + Centralisation Scoring

## Resume

Deux chantiers majeurs realises dans cette session :

1. **Centralisation du scoring** dans `advanced_scoring.py` (plan approuve en session precedente, implementé et verifie)
2. **Correction critique MTF Queen V3** — logique asymetrique delta/MTF

---

## PARTIE 1 : Centralisation du scoring dans advanced_scoring.py

### Contexte

Le scoring etait eparpille dans 3 fichiers :
- `strategy/advanced_scoring.py` : composite score (OF + Institutional + Context)
- `core/decision_pipeline.py` : `apply_pma_adjustments()` (bonus/malus MTF, IRD, regime, micro-res)
- `phase_observer/market_analyzer.py` : `build_decision()` (score >= min_score → BUY/SELL/HOLD)

### Separation des responsabilites (nouveau)

```
advanced_scoring.py    = SCORING UNIQUEMENT (calcul score, bonus, malus)
decision_pipeline.py   = VETOS + DECISION (BUY/SELL/HOLD) + BRANCHES + fusion_out
timing_analyzer.py     = VETOS TIMING (inchange)
3 analyseurs           = DETECTION/ANALYSE (recalibres)
```

### Etape 1 : Recalibration des 3 analyseurs

**`phase_observer/market_fatigue_analyzer.py`**
- Absorption : seuil `0.0001` → `0.0003`, volume multiplier `*15` → `*3`
- Volume decroissant : 3 ticks strictement decroissants → tendance lineaire sur 5+ ticks
- ATR seuil : `0.7` → `0.6`

**`phase_observer/market_physics_analyzer.py`**
- Centripetal : `distance_pct > 0.003` → `> 0.0005` (30 pips → 5 pips)
- Nouvelle methode `_calculate_physics_score()` retourne 0-10
- `_derive_physics_bias()` integre barriers + centripetal, seuil abaisse (>=2 → >=1)

**`phase_observer/institutional_reversal_detector.py`**
- Fix bug hidden divergence (meme condition que regular → corrige Higher Low / Lower High)
- Poids redistribues : changepoint 0.35→0.20, divergence 0.30→0.35, pattern 0.10→0.20, capital_flows 0.10
- Seuil `reversal_detected` : 75 → 65
- Validation smart_money : 2/3 → 1/3

### Etape 2 : Nouvelle fonction `calculate_final_score()` dans advanced_scoring.py

Fonction unique centralisant TOUT le scoring (13 etapes) :

```
1.  Score de base = orderflow_score
2.  MALUS FATIGUE : EXHAUSTED=-25, FATIGUED=-15, directionnel=-10
3.  MALUS PHYSICS : deficit=-20, chaos=-15, barrier=-10
4.  BONUS PHYSICS : inertie alignee=+10
5.  BONUS IRD : score>=80 aligné=+25, >=65=+15, flag si oppose
6.  MALUS IRD FATIGUE : inst_veto_fatigue=-50
7.  BONUS MTF : 4/4=+15, 3/4=+10
8.  BONUS FRESH LEVEL : niveau frais < 2 pips=+10
9.  BONUS TREND CONSISTENCY : clarity>=0.7 + strength>=0.6=+5
10. MALUS MICRO-RESISTANCE : STRONG/MODERATE + <1pip + bounce>=0.7=-30
11. MALUS REGIME : RANGE/ACCUM/DISTRIB=-20 (exception si IRD reversal)
12. CONSENSUS : 2+ STOP=-15, 3 alignes=+15
13. score_final = score_brut + bonus - malus
```

**Retourne** : `score_brut`, `score_final`, `bonus_total`, `malus_total`, `adjustments[]`, `ird_reversal_opposed`, `components{}`

### Etape 3 : Adaptation decision_pipeline.py

- Import `calculate_final_score` depuis `strategy.advanced_scoring`
- Nouveaux params `fatigue_result`, `physics_result` dans `decide_scalp_action()`
- Remplacement `build_decision()` + `apply_pma_adjustments()` par un seul appel `calculate_final_score()`
- VETO_DUR et VETO_REVERSAL preserves, lisant `scoring_result`
- `fusion_out` adapte avec `scoring_components`
- **SUPPRIME** : `apply_pma_adjustments()` (~190 lignes)

### Etape 4 : Adaptation run_bot.py

- **SUPPRIME** : `SimpleAdvancedScorer` import et instanciation
- **SUPPRIME** : bloc `calculate_composite_score()` (~35 lignes, bugge)
- **AJOUTE** : extraction `fatigue_result` et `physics_result` depuis `of_v6_result['institutional_analysis']`
- Passage `fatigue_result` et `physics_result` a `decide_scalp_action()`

### Etape 5 : Nettoyage

- **SUPPRIME** de `market_analyzer.py` : `build_decision()` (~50 lignes)
- `MarketAnalyzer.analyze()` garde intact
- `calculate_unified_score()` garde en version simplifiee (legacy pour `scalping.py._score_candidate()`)

### Verification

8 fichiers compiles avec `py_compile` sans erreur :
- `strategy/advanced_scoring.py`
- `core/decision_pipeline.py`
- `run_bot.py`
- `phase_observer/market_analyzer.py`
- `phase_observer/market_fatigue_analyzer.py`
- `phase_observer/market_physics_analyzer.py`
- `phase_observer/institutional_reversal_detector.py`
- `strategy/scalping.py`

---

## PARTIE 2 : Correction critique MTF Queen V3

### Probleme identifie

Un trade SELL USDCHF pris en pleine tendance bullish apres 4 bougies rouges (pullback).

### Cause racine 1 : `LOOKBACK_CANDLES = 1`

**Fichier** : `phase_observer/price_memory_analyzer.py`, methode `analyze_single_timeframe()`

L'ancienne version regardait **1 seule bougie** (potentiellement en cours) pour determiner la direction :
```python
LOOKBACK_CANDLES = 1
last_candle = candles.iloc[-1]
if candle_close < candle_open:
    direction = 'BEARISH'  # 1 bougie rouge = BEARISH
```

4 bougies rouges de pullback dans une tendance bullish → MTF flip BEARISH instantanement.

**Correction** : Mouvement NET sur N bougies FERMEES

```python
LOOKBACK_MAP = {'M30': 3, 'M15': 4, 'M5': 5, 'M1': 5}
closed = candles.iloc[:-1]  # Exclut bougie en cours
window = closed.iloc[-lookback:]
net_pips = (window_close - window_open) / point
MIN_NET_PIPS = 1.0  # Seuil anti-bruit
```

- M30 : 3 bougies fermees = 1h30 de contexte macro
- M15 : 4 bougies fermees = 1h de direction
- M5 : 5 bougies fermees = 25min de momentum
- M1 : 5 bougies fermees = 5min de micro-tendance

### Cause racine 2 : Le delta commandait la direction

**Fichier** : `strategy/scalping.py`, section MTF Queen dans `_analyze_orderflow_v6()`

L'ancienne V2 :
```
Delta decide direction → Queen valide ou bloque
Delta +1 (25 vs 24 = bruit) classé "bullish"
→ Queen bloque BUY car MTF=BEARISH
→ bias NEUTRAL → JAMAIS de SELL malgre MTF 4/4 BEARISH
```

### Correction : MTF Queen V3 — Logique asymetrique

Constat operationnel du bot :
- **Delta BULLISH = TRES FIABLE** → on peut BUY les yeux fermes
- **Delta BEARISH = PAS FIABLE** → seule l'analyse MTF guide en bearish

**Nouvelles regles** :

```
BUY  : Delta bullish suffit (fiable). MTF BULLISH = bonus +15
SELL : MTF BEARISH seul guide. Delta ignore pour direction.
```

| Situation | Bias | Bonus | Raison |
|-----------|------|-------|--------|
| Delta bullish + MTF BULLISH | **BUY** | **+15** | Delta fiable + MTF confirme |
| Delta bullish + MTF autre | **BUY** | 0 | Delta fiable, pas de bonus |
| MTF BEARISH + delta bearish | **SELL** | **+15** | MTF guide + delta confirme |
| MTF BEARISH + delta autre | **SELL** | 0 | MTF guide seul |
| Delta pas bullish + MTF pas BEARISH | NEUTRAL | 0 | Pas de signal |

### Code implementé (scalping.py)

```python
# BUY : Delta bullish fiable, on y va
if delta_direction == "bullish":
    result["bias"] = "BUY"
    if mtf_direction == "BULLISH":
        bonus +15  # Double confirmation
    # MTF ne bloque JAMAIS un delta bullish

# SELL : MTF BEARISH est le seul guide
elif mtf_direction == "BEARISH":
    result["bias"] = "SELL"
    if delta_direction == "bearish":
        bonus +15  # Delta confirme
    # Delta ignore pour la direction SELL

# Sinon : NEUTRAL
else:
    result["bias"] = "NEUTRAL"
```

### Verification par logs

**SELL via MTF** (USDCHF) :
```
[MTF_QUEEN][USDCHF] SELL - MTF=BEARISH + delta confirme (imb=0.40) → +15 pts (score=45.0/100)
[MTF_QUEEN][USDCHF] SELL - MTF=BEARISH + delta confirme (imb=0.45) → +15 pts (score=47.0/100)
```

**BUY via delta** (USDJPY) :
```
[MTF_QUEEN][USDJPY] BUY - delta bullish fiable (imb=0.54), MTF=BEARISH → pas de bonus
[MTF_QUEEN][USDJPY] BUY - delta bullish fiable (imb=0.63), MTF=BEARISH → pas de bonus
```

**Scoring** :
```
[SCORING][USDJPY] Score: 20.0 -> 0.0 (Bonus: +0, Malus: -35)
[SCORING][USDCHF] Score: 22.0 -> 7.0 (Bonus: +0, Malus: -15)
```

Zero erreur dans tous les logs. Pipeline stable.

---

## Fichiers modifies (session complete)

| Fichier | Modifications |
|---------|---------------|
| `strategy/advanced_scoring.py` | Reecrit — `calculate_final_score()` centralise |
| `core/decision_pipeline.py` | Suppression `apply_pma_adjustments()`, appel `calculate_final_score()` |
| `run_bot.py` | Suppression composite bugge, passage fatigue/physics |
| `phase_observer/market_analyzer.py` | Suppression `build_decision()` |
| `phase_observer/market_fatigue_analyzer.py` | Recalibration seuils |
| `phase_observer/market_physics_analyzer.py` | Recalibration + `physics_score` |
| `phase_observer/institutional_reversal_detector.py` | Fix bugs + reponderation |
| `phase_observer/price_memory_analyzer.py` | MTF multi-bougies (N fermees au lieu de 1) |
| `strategy/scalping.py` | MTF Queen V3 asymetrique |
