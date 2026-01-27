# SNIPER_X — Pipeline complet de prise de trade

> Document de reference — Genere le 27 Janvier 2026
> Decrit chaque etape, chaque seuil, chaque condition pour qu'un trade soit execute.

---

## Table des matieres

1. [Vue d'ensemble du flux](#1-vue-densemble-du-flux)
2. [Etape 1 — Chargement des donnees](#2-etape-1--chargement-des-donnees)
3. [Etape 2 — OrderFlow V6 (signal unique)](#3-etape-2--orderflow-v6-signal-unique)
4. [Etape 3 — Scoring Composite (5 composants)](#4-etape-3--scoring-composite-5-composants)
5. [Etape 4 — Timing Gatekeeper (PASS/VETO)](#5-etape-4--timing-gatekeeper-passveto)
6. [Etape 5 — Triple Filter (gate principal)](#6-etape-5--triple-filter-gate-principal)
7. [Etape 6 — PMA Bonus/Malus](#7-etape-6--pma-bonusmalus)
8. [Etape 7 — Veto Dur](#8-etape-7--veto-dur)
9. [Etape 8 — Burst Mode (execution)](#9-etape-8--burst-mode-execution)
10. [Etape 9 — Risk Management (SL/TP/Sizing)](#10-etape-9--risk-management-sltpsizing)
11. [Etape 10 — Monitoring Basket](#11-etape-10--monitoring-basket)
12. [Resume des Kill Switches](#12-resume-des-kill-switches)
13. [Schemas de decision](#13-schemas-de-decision)
14. [Fichiers source](#14-fichiers-source)

---

## 1. Vue d'ensemble du flux

Chaque cycle (toutes les **2.5 secondes**), le bot execute la sequence suivante pour chaque asset (USDJPY, NAS100) :

```
 CYCLE (2.5s)
    |
    v
 [1] Chargement donnees marche (M1/M5/M15 + ticks 8s)
    |
    v
 [2] OrderFlow V6 ──────────────────── Score brut 0-100
    |                                   (delta + volume + imbalance + coherence)
    |                                   NOTE: Le code MTF M1/M3/M5 dans OrderFlow V6
    |                                   est DESACTIVE (M3=None, M5=None dans l'appel actuel)
    v
 [2b] MTF Trend Verdict ────────────── Direction BULLISH/BEARISH (jamais NEUTRAL)
    |                                   Analyse M15+M5+M1 via price_memory_analyzer
    |                                   Impact: +30 bonus / -35 malus
    v
 [3] Scoring Composite ─────────────── Score pondere 0-100
    |                                   (5 composants: OF 35% + Instit 25% + Context 20% + Tech 15% + Risk 5%)
    v
 [4] Timing Gatekeeper ─────────────── PASS ou VETO
    |                                   (heures, tick rate, age bougie, spread)
    |
    +──── VETO ──── Score OF >= 85 ET veto < 60 ? ──── OUI ──> Override, continue
    |                                                   NON ──> HOLD (fin)
    v
 [5] Triple Filter ─────────────────── 3 filtres, TOUS doivent passer
    |                                   F1: Delta Multi-TF
    |                                   F2: Microstructure (3/4)
    |                                   F3: Contexte (2/3)
    |
    +──── UN filtre echoue ──────────> HOLD (fin)
    |
    v
 [6] PMA Bonus/Malus ───────────────── Ajustement du score
    |                                   (bonus MTF, fresh level, instit...)
    |                                   (malus resistance, contre-tendance, fatigue...)
    v
 [7] Veto Dur ───────────────────────── score_ajuste < 60 ? ──> HOLD (fin)
    |
    v
 [8] Order Builder + Validators ─────── Spread, volume, portfolio checks
    |
    v
 [9] Execution Burst ───────────────── 8 tickets paralleles via MT5
    |
    v
[10] Monitoring Basket ─────────────── Polling 100ms, target profit / loss guard
```

---

## 2. Etape 1 — Chargement des donnees

A chaque cycle, le bot charge :

| Donnee | Source | Quantite |
|--------|--------|----------|
| Barres M1 | MT5 `copy_rates` | 600+ barres |
| Barres M5 | MT5 `copy_rates` | 360+ barres |
| Barres M15 | MT5 `copy_rates` | 240+ barres |
| Ticks temps reel | MT5 `copy_ticks_range` | Fenetre glissante **8 secondes** |

**Fichier source** : `run_bot.py` — fonction `scalping_worker()` (ligne ~3107)

---

## 3. Etape 2 — OrderFlow V6 (signal unique)

L'OrderFlow V6 est la **seule source de signaux**. Il analyse les ticks de la **derniere bougie M1 cloturee** (`iloc[-2]`) et produit un score de 0 a 100.

**Fichier source** : `strategy/scalping.py` — methode `_analyze_orderflow_v6()` (ligne 335)

### 3.1 Calcul du Delta (depuis les ticks)

```
buy_volume  = somme des ticks cote achat
sell_volume = somme des ticks cote vente
delta_total = buy_volume - sell_volume

imbalance   = buy_volume / total_volume   (0.5 = neutre)
```

- Si `delta_total > 0` → pression acheteuse (BUY)
- Si `delta_total < 0` → pression vendeuse (SELL)

### 3.2 Direction Multi-Timeframe dans OrderFlow V6 (CODE MORT)

> **ATTENTION** : Cette section decrit du code qui existe dans `strategy/scalping.py`
> mais qui est **desactive** dans le pipeline actuel. Dans `run_bot.py` ligne 3537-3538,
> M3 et M5 sont toujours passes a `None`, donc le bonus MTF (+10 pts) ne se declenche jamais.
>
> La vraie analyse MTF est le **MTF Trend Verdict** (section 3.8 ci-dessous).

Le code prevu analysait M1/M3/M5 (1 bougie chacun) :
- Ratio bougies vertes >= **0.67** → BULLISH
- Ratio bougies vertes <= **0.33** → BEARISH
- Bonus +10 si M1 et M3 alignes avec delta

### 3.8 MTF Trend Verdict (la VRAIE analyse de direction)

**Fichier source** : `phase_observer/price_memory_analyzer.py` — `get_mtf_trend_verdict()` (ligne 1035)
**Appele depuis** : `run_bot.py` ligne 3657

Cette analyse utilise les donnees **M15, M5 et M1** chargees a l'etape 1 pour determiner la direction du marche.

**Differences cles avec l'analyse OrderFlow V6 :**

| Aspect | OrderFlow V6 MTF (desactive) | MTF Trend Verdict (actif) |
|--------|------------------------------|---------------------------|
| Timeframes | M1, M3, M5 | **M15, M5, M1** |
| Peut etre NEUTRAL ? | Oui | **Jamais** (dojis resolus via 3 bougies) |
| Impact sur score | +10 (jamais applique) | **+30 bonus / -35 malus** |
| Logique | Simple couleur de bougie | "M1 Dictature" : direction validee seulement si M1 est d'accord avec la majorite |

**Logique "M1 Dictature"** :
```
SI 2+ timeframes BULLISH ET M1 = BULLISH → verdict = BULLISH
SI 2+ timeframes BEARISH ET M1 = BEARISH → verdict = BEARISH
SINON → verdict = NEUTRAL (malgre la regle "jamais neutral" par TF individuel)
```

**Impact sur le score (dans run_bot.py PMA Bonus/Malus)** :
- Alignement 3/3 avec signal → **+15 points**
- Alignement 2/3 avec signal → **+10 points**
- Contre-tendance (signal oppose au MTF avec alignement >= 2) → **-35 points**

### 3.3 Score Delta Momentum (0 a 25 points)

Depend de la **coherence directionnelle** (sur `delta_coherence_bars` = **3 bougies**) et de la taille du delta.

**Coherence** = proportion de bougies dans la meme direction :
```
coherence = max(count_bullish, count_bearish) / 3
```

| Coherence | \|delta\| >= 50 | >= 30 | >= 15 | >= 5 |
|-----------|----------------|-------|-------|------|
| >= 0.75 (forte) | **25.0** | 20.0 | 18.0 | 15.0 |
| >= 0.65 (moderee) | 15.0 | 15.0 | 12.0 | 10.0 |
| >= 0.55 (faible) | 10.0 | 10.0 | 7.0 | 7.0 |
| < 0.55 | 5.0 | 5.0 | 5.0 | 5.0 |

**Exemple** : Si les 3 dernieres bougies sont toutes haussieres (coherence = 1.0) et |delta| = 40, le score delta = **20.0**.

### 3.4 Score Volume (0 a 15 points)

Compare le volume actuel a la moyenne des **6 dernieres bougies** :

```
volume_ratio = tick_count_actuel / moyenne_6_barres
```

| Ratio Volume | Classification | Score |
|-------------|---------------|-------|
| >= 1.80 | Spike | **15.0** |
| >= 1.40 | Eleve | 12.0 |
| >= 1.15 | Au-dessus | 10.0 |
| >= 0.85 | Normal | 7.0 |
| >= 0.60 | Modere | 3.0 |
| < 0.60 | Tres faible | 0.0 |

**Exemple** : 180 ticks sur la bougie actuelle vs moyenne de 100 → ratio = 1.80 → score = **15.0** (spike).

### 3.5 Score Imbalance (0 a 10 points)

Mesure le desequilibre achat/vente :

| Total Imbalances (buy + sell) | Score |
|-------------------------------|-------|
| >= 5 | **10.0** |
| >= 3 | 8.0 |
| >= 1 | 5.0 |
| 0 | 0.0 |

### 3.6 Conversion en score progressif (0 a 100)

Les sous-scores bruts sont convertis en echelle progressive :

**Delta Momentum → 0 a 40 points :**

| Score brut delta | Points progressifs |
|-----------------|-------------------|
| >= 20.0 | **40** |
| >= 15.0 | 30 |
| >= 12.0 | 25 |
| >= 8.0 | 15 |
| >= 5.0 | 10 |
| > 0.0 | 5 |

**Volume → 0 a 30 points :**

| Score brut volume | Points progressifs |
|------------------|-------------------|
| >= 12.0 | **30** |
| >= 10.0 | 25 |
| >= 7.0 | 15 |
| >= 5.0 | 10 |
| > 0.0 | 5 |

**Imbalance → 0 a 20 points :**

| Score brut imbalance | Points progressifs |
|---------------------|-------------------|
| >= 8.0 | **20** |
| >= 6.0 | 15 |
| >= 5.0 | 10 |
| >= 3.0 | 5 |

**Coherence → 0 a 10 points :**

| Valeur coherence | Points progressifs |
|-----------------|-------------------|
| >= 0.67 | **10** |
| >= 0.50 | 7 |
| >= 0.33 | 5 |
| > 0.0 | 2 |

**Score total OrderFlow** = delta_progressif + volume_progressif + imbalance_progressif + coherence_progressive + bonus_MTF

### 3.7 Classification du signal

| Score OrderFlow | Qualite |
|----------------|---------|
| >= 80 | EXCELLENT |
| >= 60 | GOOD |
| >= 40 | FAIR |
| >= 20 | WEAK |
| < 20 | NO_TRADE |

---

## 4. Etape 3 — Scoring Composite (5 composants)

Le score composite combine 5 dimensions avec des poids normalises.

**Fichier source** : `strategy/advanced_scoring.py` — fonction `calculate_unified_score()` (ligne 29)

### 4.1 Formule generale

```
score_final = (0.35 x orderflow) + (0.25 x institutional) + (0.20 x context)
            + (0.15 x technical) + (0.05 x risk)
```

Clampe entre 0 et 100.

### 4.2 Composant OrderFlow (0-100) — Poids 35%

| Sous-composant | Plage | Calcul |
|---------------|-------|--------|
| Delta score | 0-30 | Base sur `delta_ratio = \|delta\| / volume_total`. >= 0.3 → 30 |
| Volume score | 0-20 | Base sur `vol_ratio = total / moyenne`. >= 1.2 → 20 |
| Imbalance score | 0-10 | `\|imb_mean - 0.5\| / 0.5 * 10` |
| Footprint bonus | 0-15 | Absorption flag → 15 ; delta_ratio > 0.25 → 10 ; sinon → 5 |
| Pattern bonus | 0-15 | `min(15, nb_patterns * 5)` |
| Penalites | -5 a -15 | rescue_level 1 → -5 ; rescue >= 2 → -15 |

### 4.3 Composant Institutional (0-100) — Poids 25%

Moyenne des sous-scores disponibles parmi 5 analyseurs + microstructure :

| Analyseur | Calcul |
|-----------|--------|
| **Price Memory** | `50 + (fresh_ratio - 0.5) * 50`. Plus les niveaux sont "frais", plus le score est eleve |
| **Market Fatigue** | EXHAUSTED → 30 ; FATIGUED → 40 ; NORMAL → 50 ; ENERGETIC → **65** |
| **Market Physics** | Aligne avec signal → **75** ; oppose → 25 ; neutre → 50 |
| **Tape Speed** | ratio >= 2.0 → **70** ; >= 1.5 → 60 ; >= 0.8 → 50 ; < 0.8 → 35 |
| **Pressure** | `50 + pression_normalisee * 50` |
| **Microstructure** | `min(100, (tape_speed / 5.0) * 100)` |

### 4.4 Composant Context (0-100) — Poids 20%

```
alignment = 1.0 si action alignee avec phase
            0.8 si partiellement alignee
            0.5 si neutre

context_score = (0.5 * confidence + 0.5 * alignment) * 100
```

### 4.5 Composant Technical (0-100) — Poids 15%

```
tech_score = score_technique * 100
pattern_bonus = min(20, nb_patterns * 5)
total = min(100, tech_score + pattern_bonus)
```

### 4.6 Composant Risk (0-100) — Poids 5%

Base uniquement sur le spread :

| Spread (pips) | Score Risk |
|--------------|-----------|
| <= 5 | **100** |
| <= 10 | 80 |
| <= 15 | 60 |
| > 15 | 30 |

### 4.7 Decision issue du scoring composite

| Score Final | Confidence | Decision |
|------------|-----------|----------|
| >= 75 | **STRONG** | BUY ou SELL |
| >= 65 | GOOD | BUY ou SELL |
| >= 55 | WEAK | BUY ou SELL |
| < 55 | NONE | **HOLD** |

---

## 5. Etape 4 — Timing Gatekeeper (PASS/VETO)

Le Timing Gatekeeper produit un **veto_score** pondere de 0 a 100.
- **PASS** si veto_score < 80
- **VETO** si veto_score >= 80

**Fichier source** : `phase_observer/timing_analyzer.py` — fonction `evaluate_trading_conditions()` (ligne 23)

### 5.1 Conditions et poids du veto

| # | Condition | Points Veto | Type |
|---|-----------|------------|------|
| A | Heure NON dans `allowed_hours` | +50 | Modere |
| B | Coverage < `min_coverage_s` (5s) | +70 | Fort |
| C | Tick rate < `min_tick_rate` | +0 a 60 (proportionnel au deficit) | Proportionnel |
| D | Asian Early + tick_rate < 8.0 | +40 | Modere |
| E | Tick rate > `max_tick_rate` | **= 100 (absolu)** | Absolu |
| F | Score liquidite < 0.30 | +45 | Modere |
| G | Qualite session = POOR | +30 | Faible |
| H | Age bougie > `max_candle_age_s` | **= 100 (absolu)** | Absolu |

### 5.2 Parametres par asset

| Parametre | Global | USDJPY | NAS100 |
|-----------|--------|--------|--------|
| min_tick_rate | 1.0/s | **0.5/s** | **2.5/s** |
| max_tick_rate | 200/s | 200/s | **300/s** |
| min_coverage_s | 5.0s | 5.0s | 5.0s |
| max_spread_pips | 1.5 | 1.5 | **200** (2 pts index) |
| max_candle_age_s | 50s | **52s** | 50s |

### 5.3 Heures autorisees (GMT)

| Asset | Heures autorisees |
|-------|------------------|
| Global | 0-11, 14-18 |
| USDJPY | 0-3, 7-10, 13-17 |
| NAS100 | 0-23 (24/7) |

### 5.4 Override du veto

Le veto peut etre **force** dans certains cas :
- Score OrderFlow >= **85** ET veto_score < **60** → le trade passe quand meme
- Score OrderFlow >= **90** ET veto_score < **70** → le trade passe quand meme

---

## 6. Etape 5 — Triple Filter (gate principal)

C'est le **gate le plus important**. Les **3 filtres doivent passer** simultanement.

**Fichier source** : `run_bot.py` (lignes ~4034-4203)

### 6.1 Filtre 1 — Delta Pondere Multi-TF

```
delta_weighted = (delta_m1 * 0.6) + (delta_m3 * 0.4)
```

Ou `delta_m3` = variation de prix en pips sur `lookback_bars` (3 bougies).

**Seuil de direction** = `min_price_change_pips * 3` :

| Asset | min_price_change_pips | Seuil direction |
|-------|----------------------|----------------|
| USDJPY | 3.0 | **9** |
| NAS100 | 20.0 | **60** |

- Si `|delta_weighted| >= seuil` → direction = BUY ou SELL
- Si `|delta_weighted| < seuil` → direction = NEUTRAL → **filtre echoue**

**Verification supplementaire** : `delta_weighted` et `cvd_slope` doivent avoir le **meme signe** (confirmation CVD).

**Confidence du filtre** :
```
confidence = min(1.0, |delta_weighted| / (seuil * 3))
```

### 6.2 Filtre 2 — Microstructure (3 conditions sur 4 requises)

| # | Condition | USDJPY | NAS100 | Default |
|---|-----------|--------|--------|---------|
| 1 | Volume > 150% de la moyenne | ratio > 1.5 | ratio > 1.5 | ratio > 1.5 |
| 2 | Tick rate >= minimum | >= **1.5** ticks/s | >= **5.0** ticks/s | >= 2.0 ticks/s |
| 3 | Coverage >= 5 secondes | >= 5.0s | >= 5.0s | >= 5.0s |
| 4 | CVD aligne avec delta | meme signe | meme signe | meme signe |

**Seuil de passage** : **3 conditions sur 4 minimum**.

**Exemple** : USDJPY avec volume ratio 1.8 (OK), tick rate 2.0 (OK, >= 1.5), coverage 6s (OK), CVD oppose au delta (NON) → 3/4 → **PASS**.

### 6.3 Filtre 3 — Contexte (2 conditions sur 3 requises)

| # | Condition | Detail |
|---|-----------|--------|
| 1 | Fatigue OK | Toujours `true` par defaut (veto global desactive) |
| 2 | Memory fresh | `memory_clarity >= 0.5` (50%+ niveaux frais) |
| 3 | Memory aligne | Direction du Filtre 1 = `memory_trend_direction` |

**Seuil de passage** : **2 conditions sur 3 minimum**.

### 6.4 Decision combinee du Triple Filter

```
tous_filtres_passent = (F1 = BUY ou SELL) ET (F2 pass) ET (F3 pass)

bonus_memory = 30 si memory aligne, sinon 0
score_ajuste = score_original + bonus_memory

SI tous_filtres_passent ET score_ajuste >= asset_min_score :
    → Signal VALIDE (BUY ou SELL)
SINON :
    → HOLD
```

**Score minimum par asset** :

| Asset | asset_min_score |
|-------|----------------|
| USDJPY | **60.0** |
| NAS100 | **60.0** |
| Default | 65.0 |

---

## 7. Etape 6 — PMA Bonus/Malus

Apres le Triple Filter, le score est ajuste par un systeme de bonus et malus.

### 7.1 Malus (penalites)

| Condition | Penalite | Detail |
|-----------|----------|--------|
| Micro-Resistance proche | **-30** | BUY pres d'une resistance forte (< 1 pip, proba rebond >= 70%) |
| Contre-tendance MTF | **-35** | Signal BUY alors que MTF = BEARISH avec alignement >= 2 TF (et inversement) |
| Regime Range/Accumulation | **-20** | Signal directionnel dans un regime non-directionnel |
| Fatigue Circuit Breaker | **-50** | Marche epuise detecte par l'IRD |
| Reversal Institutionnel | **-15** | Signal institutionnel de retournement oppose au signal |

### 7.2 Bonus (recompenses)

| Condition | Bonus | Detail |
|-----------|-------|--------|
| MTF Alignement 3/3 | **+15** | Les 3 timeframes (M1/M5/M15) sont alignes avec le signal |
| MTF Alignement 2/3 | **+10** | 2 timeframes alignes |
| Fresh Level | **+10** | Prix a < 2 pips d'un niveau non teste |
| Trend Consistency | **+5** | clarity >= 0.7 ET strength >= 0.6 ET aligne |
| Signal Institutionnel | **+10** | inst_score >= 65 ET aligne avec le signal |

### 7.3 Score final ajuste

```
score_ajuste = score_brut + total_bonus - total_malus
```

**Exemple concret** :
- Score brut apres Triple Filter : **72**
- Bonus MTF 2/3 : +10
- Bonus Fresh Level : +10
- Malus contre-tendance : -35
- → Score ajuste = 72 + 10 + 10 - 35 = **57** → HOLD (< 60)

---

## 8. Etape 7 — Veto Dur

Derniere verification avant l'execution :

```
SI signal = BUY ou SELL ET score_ajuste < 60.0 :
    → HOLD (PMA_VETO_DUR)
```

C'est un filet de securite qui bloque les signaux trop affaiblis par les malus PMA.

---

## 9. Etape 8 — Burst Mode (execution)

Si toutes les etapes precedentes passent, le trade est execute en **mode burst**.

**Fichiers source** :
- Config : `config/strategy/config_trade_scalping.json`
- Execution : `trader/burst.py`

### 9.1 Parametres generaux du burst

| Parametre | Valeur |
|-----------|--------|
| burst_size | **8** tickets paralleles |
| enforce_single_basket | **true** (1 seul basket a la fois) |
| entry_mode | MARKET |
| mode actif | **niveau_2_5** (Hybrid Boost) |

### 9.2 Les 4 niveaux de burst

#### Niveau 2 — Trio (Baseline)

| Critere | Seuil Global | Seuil USDJPY |
|---------|-------------|-------------|
| delta_momentum_min | 20.0 | **15.0** |
| volume_confirmation_min | 10.0 | **8.0** |
| imbalance_strength_min | 5.0 | 5.0 |
| composite_score_min | 65.0 | **62.0** |
| target_profit | 3.0 pips | 3.0 pips |

#### Niveau 2.5 — Hybrid Boost (ACTIF)

| Critere | Seuil Global | Seuil USDJPY |
|---------|-------------|-------------|
| delta_momentum_min | 18.0 | **12.0** |
| volume_confirmation_min | 10.0 | **8.0** |
| imbalance_strength_min | 5.0 | 5.0 |
| composite_score_min | 66.0 | 66.0 |
| **+ au moins 1 parmi :** | | |
| absorption_levels_min | 10.0 | **8.0** |
| order_clustering_min | 6.0 | **5.0** |
| tape_speed_ratio_min | 2.0 | **1.5** |
| target_profit | 1.8 pips | 1.8 pips |

**Explication** : Le Niveau 2.5 exige le "Trio" (delta + volume + imbalance) **plus** au moins **un signal institutionnel** parmi absorption, clustering ou tape speed. C'est un compromis entre frequence et qualite.

#### Niveau 3 — Absorption Boostee

| Critere | Seuil Global | Seuil USDJPY |
|---------|-------------|-------------|
| delta_momentum_min | 18.0 | **12.0** |
| volume_confirmation_min | 10.0 | 10.0 |
| absorption_levels_min | 10.0 | **8.0** |
| order_clustering_min | 3.5 | **3.0** |
| composite_score_min | 68.0 | **64.0** |
| require_absorption_bonus | true | true |
| target_profit | 4.0 pips | 4.0 pips |

Sortie anticipee a **3.0 pips** si `tape_speed_ratio < 1.0`.

#### Niveau 4 — Institutional Filter

Memes criteres que Niveau 3, **plus** des filtres institutionnels :

| Filtre | Condition |
|--------|-----------|
| Market Fatigue | Etats autorises : ENERGETIC, NORMAL. FATIGUED = +5 pts au seuil composite. EXHAUSTED = +10 pts |
| Price Memory | `min_score >= 50` (fresh_ratio >= 0.5) |
| Market Physics | Bias physique aligne avec direction du delta |

Target profit : **5.0 pips** (sortie anticipee a 3.5 si tape speed < 1.2).

### 9.3 Execution technique du burst

1. `open_burst_basket()` envoie **8 ordres paralleles** via `ThreadPoolExecutor`
2. Tous les ordres partagent le meme `basket_id` (format `bs_<8hex>`)
3. Tous ont le meme SL et TP
4. Le sizing est calcule pour que le **risque total** du basket = budget risque du trade

---

## 10. Etape 9 — Risk Management (SL/TP/Sizing)

### 10.1 Calcul du volume (sizing)

```
montant_risque_max = equity * (risk_pct / 100)
perte_par_lot = stop_distance_points * tick_value / tick_size

// Pour le burst (8 tickets) :
risque_par_ticket = montant_risque_max / 8
volume_brut = risque_par_ticket / perte_par_lot
volume_final = arrondi_inferieur(volume_brut, lot_step)
```

**Pourcentage de risque** : **3.0%** par trade (configurable par asset).

### 10.2 Limites globales

| Limite | Valeur |
|--------|--------|
| Max risk par trade | 2% (global_safety) / 10% (guardrails) |
| Max risk total | 6% |
| Max lot size | 50.0 |
| Max positions ouvertes | 10 |
| Max trades par jour | 20 |
| Max trades par symbole/jour | 5 |

### 10.3 SL/TP par asset

| Parametre | USDJPY | NAS100 |
|-----------|--------|--------|
| Methode SL | PIPS | PIPS |
| SL | **2.5 pips** | **800** (8 pts index) |
| Buffer SL | 2 pips | 200 (2 pts) |
| Methode TP | PIPS | PIPS |
| TP | **3.8 pips** | **1200** (12 pts index) |
| RR base | 1.5 | 1.5 |
| RR floor | 1.0 | 1.0 |
| RR cap | 3.0 | 3.0 |
| Min distance SL-TP | 15 pips | 1500 (15 pts) |

### 10.4 Modulation dynamique du RR

```
rr_effectif = rr_base * (0.9 + 0.2 * confidence)
```

Clampe entre `rr_floor` (1.0) et `rr_cap` (3.0).

### 10.5 Valeurs pip par asset

| Asset | Valeur par pip par lot |
|-------|----------------------|
| USDJPY | 1000 / taux_courant USD |
| NAS100 | 20 USD |

### 10.6 Validations avant envoi

| Validation | Detail |
|-----------|--------|
| **Spread Guard** | spread <= max configure ET spread <= moyenne_session * 2.5 |
| **Fat Finger** | volume <= max absolu par asset (USDJPY: 1.1 lots) ET volume <= moyenne * 5.0 |
| **Portfolio** | nb positions < 10 |

---

## 11. Etape 10 — Monitoring Basket

Apres l'execution, le basket monitor surveille en continu.

**Fichier source** : `trader/burst.py` — `monitor_burst_baskets()` (ligne ~1230)

### 11.1 Parametres de surveillance

| Parametre | Global | USDJPY | NAS100 |
|-----------|--------|--------|--------|
| Polling | 100ms | 100ms | 100ms |
| target_profit_pips | 1.8 | **1.23** | **310** (3.1 pts) |
| max_loss_pips | 80.0 | **15.0** | **2500** (25 pts) |
| min_age_ms | 2000 | 2000 | 2000 |
| loss_guard_arming_ms | 3000 | 3000 | 3000 |
| fast_window_ms | 120000 | 120000 | 120000 |

### 11.2 Logique de cloture

```
A chaque poll (100ms) :

1. SI age_basket < 2000ms → ne rien faire (protection anti-flash)

2. SI P/L_aggregate >= target_profit_pips → FERMER TOUT (profit atteint)

3. SI age_basket >= 3000ms ET P/L_aggregate <= -max_loss_pips → FERMER TOUT (loss guard)

4. SI age_basket >= 120000ms (2 min) → FERMER TOUT (timeout)
```

### 11.3 Phases dynamiques du basket

| % remplissage | Phase | Comportement |
|--------------|-------|--------------|
| <= 50% | ACCUMULATION | TP plus large |
| <= 80% | TARGETING | TP normal |
| > 80% | SECURING | TP plus serre |

---

## 12. Resume des Kill Switches

Un trade est **bloque** si **une seule** de ces conditions est vraie :

| # | Gate | Condition | Effet |
|---|------|-----------|-------|
| 1 | Scoring Composite | score < 55 | HOLD |
| 2 | Timing Veto | veto_score >= 80 ET pas d'override | HOLD |
| 3 | Age bougie | > 50s (NAS100) / 52s (USDJPY) | **VETO ABSOLU** |
| 4 | Tick rate excessif | > 200/s (USDJPY) / 300/s (NAS100) | **VETO ABSOLU** |
| 5 | Triple Filter F1 | delta_weighted insuffisant | HOLD |
| 6 | Triple Filter F2 | < 3/4 micro-conditions | HOLD |
| 7 | Triple Filter F3 | < 2/3 conditions contexte | HOLD |
| 8 | Score minimum | score < 60 (asset min) | HOLD |
| 9 | PMA Veto Dur | score_ajuste < 60 apres bonus/malus | HOLD |
| 10 | Spread Guard | spread > max autorise | BLOCK |
| 11 | Fat Finger | volume > max absolu/dynamique | BLOCK |
| 12 | Portfolio | positions >= 10 | BLOCK |
| 13 | Risk Cap jour | >= 20 trades/jour | BLOCK |
| 14 | Risk Cap symbole | >= 5 trades/symbole/jour | BLOCK |
| 15 | Single Basket | deja 1 basket ouvert | BLOCK |

---

## 13. Schemas de decision

### 13.1 Chemin d'un trade reussi (exemple USDJPY)

```
Cycle N (t = 14:23:02 UTC)
  |
  Ticks 8s charges : 45 ticks, delta = +28
  |
  OrderFlow V6 :
    coherence 3/3 = 1.0, delta = 28 (>= 15) → delta_score = 18.0
    vol_ratio = 1.45 → vol_score = 12.0
    imbalances = 4 → imb_score = 8.0
    → Score progressif = 30 + 25 + 15 + 10 = 80 (EXCELLENT)
    → MTF M1+M3 alignes → +10 = 90
  |
  Scoring Composite :
    OF=90*0.35 + Instit=65*0.25 + Context=70*0.20 + Tech=60*0.15 + Risk=80*0.05
    = 31.5 + 16.25 + 14.0 + 9.0 + 4.0 = 74.75 → GOOD
  |
  Timing : heure 14h (London), tick_rate 2.1/s, age 12s → veto_score = 0 → PASS
  |
  Triple Filter :
    F1: delta_weighted = 28*0.6 + 5*0.4 = 18.8 >= 9 → BUY ✓
    F2: vol 1.45 (NON <1.5) + tick 2.1 (OK) + coverage 6s (OK) + CVD aligne (OK) = 3/4 ✓
    F3: fatigue OK + memory 0.6 (OK) + memory BUY (OK) = 3/3 ✓
    → Bonus memory +30 → score = 90 + 30 = 120 (cap 100) >= 60 ✓
  |
  PMA : MTF 2/3 (+10) + Fresh Level (+10) = +20, aucun malus
    → score_ajuste = 100 + 20 = 120 (cap 100)
  |
  Veto Dur : 100 >= 60 → PASS ✓
  |
  → Execution BUY USDJPY burst x8
    SL = 2.5 pips, TP = 3.8 pips
    Target profit basket = 1.23 pips
```

### 13.2 Chemin d'un trade bloque (exemple)

```
Cycle N (t = 09:15:00 UTC)
  |
  OrderFlow V6 : score = 72 (GOOD)
  Scoring Composite : 68 (GOOD)
  |
  Timing : heure 9h (veto_hours pour USDJPY) → veto_score = 50
           tick_rate = 0.3/s (< 0.5) → +48
           → veto_score = 98 → VETO
  |
  Override ? score OF = 72 < 85 → NON
  |
  → HOLD (Timing Veto)
```

---

## 14. Fichiers source

| Fichier | Role |
|---------|------|
| `run_bot.py` | Boucle principale, scalping_worker, Triple Filter, PMA |
| `strategy/scalping.py` | OrderFlow V6, calcul delta/volume/imbalance, evaluate_entry |
| `strategy/advanced_scoring.py` | Scoring composite 5 composants |
| `phase_observer/timing_analyzer.py` | Timing Gatekeeper PASS/VETO |
| `core/decision_pipeline.py` | Pipeline de decision, fusion signaux |
| `trader/burst.py` | Execution burst, monitoring basket |
| `trader/order_builder.py` | Construction des ordres, validations |
| `trader/sltp.py` | Calcul SL/TP |
| `trader/sizing.py` | Calcul du volume (sizing risk-based) |
| `trader/validators.py` | Spread guard, fat finger, portfolio check |
| `config/strategy/config_trade_scalping.json` | Config strategie scalping |
| `config/prod_config.json` | Config globale, guardrails |
| `config/assets_config/USDJPY.json` | Overrides specifiques USDJPY |
| `config/assets_config/NAS100.json` | Overrides specifiques NAS100 |
