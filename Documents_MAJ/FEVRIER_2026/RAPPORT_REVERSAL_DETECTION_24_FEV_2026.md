# RAPPORT — Comportement de la Détection de Reversal
**Date :** 24 Février 2026 | **Branche :** `v8_Dynamique_sltp`

---

## 1. VUE D'ENSEMBLE — Les 3 gardiens du reversal

Le bot dispose de **3 systèmes indépendants** qui doivent détecter un renversement et en pénaliser le score (ou déclencher un veto direct) :

| Gardien | Fichier | Rôle dans le scoring |
|---|---|---|
| **IRD** | `institutional_reversal_detector.py` | §5 : ±25/±15/±10/±20 + VETO_REVERSAL |
| **Fatigue** | `market_fatigue_analyzer.py` | §2 : -25/-15/-10/-15/-20 |
| **Physics** | `market_physics_analyzer.py` | §3 : -20/-15/-10/-10 + §4 : +10 |

Tous trois sont **appelés avant la décision** dans `run_bot.py` et passés à `decide_scalp_action()` → `calculate_final_score()` dans `advanced_scoring.py`.

---

## 2. GARDIEN 1 — Institutional Reversal Detector (IRD)

### 2.1 Architecture : 5 couches actives

```
_analyze_all_layers()
├── COUCHE 1 : CHANGEPOINT      (poids 0.20)  _detect_statistical_changepoint()
├── COUCHE 2 : DIVERGENCE       (poids 0.30)  _analyze_multi_tf_divergence()  [plusieurs signaux]
├── COUCHE 4 : SMART_MONEY      (poids 0.25)  _detect_smart_money_accumulation()
├── COUCHE 6 : ML_PATTERNS      (poids 0.15)  _detect_ml_patterns()
└── BONUS 2 : CAPITAL_FLOWS     (poids 0.10)  _analyze_cvd_capital_flows()

Désactivées :
  COUCHE 3 : Fatigue → VETO uniquement (get_fatigue_signal())
  BONUS 1  : Confluence → commenté
```

**Score pondéré :**
```
institutional_score = Σ(strength × weight × confidence) / total_weight
                    + bonus_boost (CAPITAL_FLOWS contribue 10% en extra → double comptage ⚠️)
```

### 2.2 Seuil de déclenchement

```python
reversal_detected = institutional_score >= 65  # dans detect_reversal()
```

### 2.3 Détermination de `new_trend` — VOTE MAJORITAIRE (faiblesse critique)

```python
def _determine_new_trend(self, result):
    bullish_count = sum(1 for s in signals_breakdown
                        if s["direction"] == "BULLISH" and s["strength"] > 30)
    bearish_count = sum(1 for s in signals_breakdown
                        if s["direction"] == "BEARISH" and s["strength"] > 30)
    if bullish_count > bearish_count: return "BULLISH"
    elif bearish_count > bullish_count: return "BEARISH"
    else: return "NEUTRAL"
```

**Problème :** Ce vote est **indépendant du score pondéré**. Il est possible d'avoir `institutional_score = 70`
(dominé par CHANGEPOINT BEARISH fort) mais `new_trend = BULLISH` si 2 couches faibles retournent BULLISH
(strength > 30). Dans ce cas : `reversal_detected = True` mais le VETO ne se déclenche jamais.

### 2.4 Condition du VETO_REVERSAL — double verrou

```python
# Dans advanced_scoring.py §5 :
if trend_opposed:
    if inst_result.get('reversal_detected', False):  # score >= 65
        ird_reversal_opposed = True  # Flag levé

# Dans decision_pipeline.py :
if ird_reversal_opposed and signal_action in ["BUY","SELL"] and not pma_veto_dur:
    → HOLD forcé
```

**Les deux conditions doivent être vraies simultanément :**
1. `institutional_score >= 65`
2. `new_trend` est opposé au signal (BEARISH vs BUY, ou BULLISH vs SELL)

Si `new_trend = NEUTRAL` (votes partagés) → pas de veto même avec score = 70.

### 2.5 Impact sur le scoring (§5 advanced_scoring.py)

```
IRD aligné (new_trend == sens du signal) :
  score >= 80 → BONUS_IRD_HIGH    : +25
  score >= 65 → BONUS_IRD         : +15
  score >= 40 → BONUS_IRD_MODERATE: +10

IRD opposé (new_trend != sens du signal) :
  score >= 60 → MALUS_IRD_OPPOSE_FORT : -20
  score >= 40 → MALUS_IRD_OPPOSE      : -10

Divergence CVD alignée (layer DIVERGENCE, regular.detected) :
  → BONUS_CVD_DIVERGENCE : +12

Aucun malus si new_trend == NEUTRAL (même avec score élevé)
```

### 2.6 Données requises par l'IRD

| Couche | Données nécessaires | Condition de fiabilité |
|---|---|---|
| CHANGEPOINT | M5 candles | >= 50 bougies M5 (environ 4h de données) |
| DIVERGENCE | CVD M1 + M5 | >= 30 valeurs CVD |
| SMART_MONEY | M5 OHLCV + volume | >= 20 bougies M5 |
| ML_PATTERNS | M1 OHLCV | >= 20 bougies M1 |
| CAPITAL_FLOWS | CVD values | >= 15 valeurs CVD |

---

## 3. GARDIEN 2 — Market Fatigue Analyzer

### 3.1 Architecture : 4 sous-systèmes

```
calculate_fatigue_indicators()
├── buyer_fatigue      (poids 0.30)  _calculate_buyer_fatigue()
├── seller_fatigue     (poids 0.30)  _calculate_seller_fatigue()
├── momentum_fatigue   (poids 0.20)  _calculate_momentum_fatigue()
└── exhaustion_signals (poids 0.20)  _detect_exhaustion_patterns()

fatigue_score = buyer×0.3 + seller×0.3 + momentum×0.2 + exhaustion×0.2

market_state :
  EXHAUSTED  : fatigue_score >= 7.0
  FATIGUED   : fatigue_score >= 4.0
  NORMAL     : fatigue_score >= 2.0
  ENERGETIC  : fatigue_score < 2.0
```

### 3.2 Sous-système buyer/seller fatigue (ticks uniquement)

```python
# Critères pour buyer_fatigue (symétrique pour seller) :
fatigue_points += 3  si slope volume buy < 0 (déclin linéaire sur 5 ticks)
fatigue_points += 2  si fréquence buy décroissante (intervalles s'allongent)
fatigue_points += 4  si absorption : price_change < 0.03% ET volume > 3× moyenne

fatigue_level = "HIGH"   si fatigue_points >= 6
             = "MEDIUM"  si fatigue_points >= 3
             = "LOW"     si fatigue_points < 3
```

**Dépendance critique :** Ce sous-système exige des **ticks avec colonne `side`** ('buy'/'sell').
Si `ticks_df is None` ou pas de colonne side → score = 0, contribution = 0 (60% du score composite perdu).

### 3.3 Sous-système momentum_fatigue (bougies M1)

```python
# Compare les 5 dernières bougies vs les 5 bougies précédentes :
if avg_recent_ATR  < avg_older_ATR  × 0.60 : +4 points   (seuil -40%)
if avg_recent_vol  < avg_older_vol  × 0.60 : +3 points   (seuil -40%)
if avg_recent_body < avg_older_body × 0.50 : +3 points   (seuil -50%)

HIGH = >= 6 points  → nécessite au moins 2 critères sur 3
```

**Point critique :** Un ralentissement de 30% (ATR à 70% de la valeur précédente) **ne déclenche rien**.
La détection commence à -40% de baisse.

### 3.4 Sous-système exhaustion_signals

```python
# Pattern 1 — Climax volume :
if last_volume > avg_volume × 2.0 AND body < range × 0.30 : +5 points

# Pattern 2 — Divergence volume/prix :
if price fait nouveau high ET recent_vol < older_vol × 0.70 : +3 points

HIGH = >= 6 points  → nécessite les deux patterns simultanément
```

### 3.5 Impact sur le scoring (§2 advanced_scoring.py)

**TOUS les malus sont multipliés par `mtf_malus_factor` :**

```python
mtf_malus_factor = 0.6  si MTF 3/3 aligné avec signal
                 = 1.0  sinon

MALUS_FATIGUE_EXHAUSTED    : -25 × mtf_factor  (market_state EXHAUSTED)
MALUS_FATIGUE              : -15 × mtf_factor  (market_state FATIGUED)
MALUS_BUYER_FATIGUE        : -10 × mtf_factor  (buyer_fatigue HIGH + BUY)
MALUS_SELLER_FATIGUE       : -10 × mtf_factor  (seller_fatigue HIGH + SELL)
MALUS_MOMENTUM_FATIGUE     : -15 × mtf_factor  (momentum_fatigue HIGH)
MALUS_ABSORPTION           : -20 × mtf_factor  (absorption forte dans sens signal)
```

**Résultat avec MTF 3/3 :**

| Signal fatigue | Valeur brute | Avec MTF 3/3 (×0.6) |
|---|---|---|
| EXHAUSTED | -25 | **-15** |
| FATIGUED | -15 | **-9** |
| MOMENTUM HIGH | -15 | **-9** |
| BUYER/SELLER HIGH | -10 | **-6** |
| ABSORPTION | -20 | **-12** |

---

## 4. GARDIEN 3 — Market Physics Analyzer

### 4.1 Architecture : 5 principes physiques

```
apply_physics_principles()
├── energy_conservation   _analyze_energy_conservation()
├── price_inertia         _calculate_price_inertia()
├── centripetal_movement  _detect_centripetal_movement()
├── energy_barriers       _identify_energy_barriers()
└── market_entropy        _calculate_market_entropy()
```

### 4.2 Détail de chaque principe

**Énergie (energy_conservation) :**
```python
current_energy = last_volume × last_range
average_energy = mean(volume × range, 10 dernières bougies)

energy_deficit = current_energy < average_energy × 0.50  (< 50% de la moyenne)
energy_surplus = current_energy > average_energy × 2.00  (> 200% de la moyenne)
```

**Inertie (price_inertia) :**
```python
momentum = mean(volume × price_change, 5 dernières bougies)
likely_to_continue = (3 derniers momentum même sens) AND inertia_strength > 0
```
Note : `likely_to_continue` génère le seul **BONUS** du système Physics (+10), **sans réduction MTF**.

**Mouvement centripète — SIGNAL IGNORÉ EN SCORING :**
```python
reversal_likely = distance_pct > 0.0005  # 0.05% du prix (~5 pips EURUSD)
# Ce signal est calculé dans _calculate_physics_score() et _derive_physics_bias()
# mais il N'EST PAS extrait dans calculate_final_score() de advanced_scoring.py
# → Contribution au scoring final : ZÉRO
```

**Barrières énergétiques (energy_barriers) :**
```python
resistance = max(high, 20 dernières bougies)  # approximation simpliste
support    = min(low,  20 dernières bougies)
# Utilisé dans §3 scoring : resistance < 1 pip + BUY → -10
```

**Entropie (market_entropy) :**
```python
entropy = Shannon entropy(buy_ratio, sell_ratio) sur 30 derniers ticks
ORDERED      = entropy < 0.50   (direction claire)
SEMI_ORDERED = entropy < 0.80
CHAOTIC      = entropy >= 0.80  (ticks mélangés)
```

### 4.3 Impact sur le scoring (§3 et §4 advanced_scoring.py)

```python
# MALUS × mtf_malus_factor
MALUS_PHYSICS_DEFICIT   : -20 × mtf_factor   (energy_deficit)
MALUS_ENTROPY_CHAOS     : -15 × mtf_factor   (entropy CHAOTIC)
MALUS_BARRIER_RES       : -10 × mtf_factor   (resistance < 1 pip + BUY)
MALUS_BARRIER_SUP       : -10 × mtf_factor   (support < 1 pip + SELL)
MALUS_ENERGY_BARRIER    : -10 × mtf_factor   (energy_required > average × 2)

# BONUS sans mtf_factor
BONUS_PHYSICS_INERTIE   : +10  (likely_to_continue aligné)
```

---

## 5. LA CHAÎNE DE SCORING COMPLÈTE

```
score_final = orderflow_score + bonus_total - malus_total  (clampé 0-100)

Contribution maximale de chaque section :
  §2  Fatigue         : min = -70 × mtf_factor        max = 0
  §3  Physics malus   : min = -65 × mtf_factor        max = 0
  §4  Physics bonus   : min = 0                       max = +10
  §5  IRD             : min = -20                     max = +25 (+12 CVD div.)
  §6  Circuit breaker : min = -50                     max = 0
  §7  MTF bonus       : min = 0                       max = +30
  §8  Fresh level     : min = 0                       max = +10
  §9  Trend consist.  : min = 0                       max = +5
  §10 Micro-résist.   : min = -30                     max = 0
  §11 Régime          : min = -20                     max = 0
  §11b Delta momentum : min = -5                      max = +8
  §12 Consensus       : min = -15                     max = +15
```

---

## 6. SIMULATION — Scénario "fin de tendance" typique

### Contexte : tendance BULLISH en déclin, le bot veut BUY

| Condition réelle du marché | Valeur mesurée | Seuil de détection | Verdict |
|---|---|---|---|
| MTF 3/3 BULLISH | alignment = 3 | requis == 3 | ✅ Score +30 |
| ATR récent = 72% ATR ancien | 72% | seuil 60% | ❌ Pas de malus |
| Volume récent = 75% volume ancien | 75% | seuil 60% | ❌ Pas de malus |
| Body récent = 55% body ancien | 55% | seuil 50% | ✅ +3 pts momentum |
| **momentum_fatigue** | score = 3/10 | HIGH = >= 6 | ❌ **MEDIUM, aucun malus** |
| market_state | NORMAL (score = 0.6/10) | FATIGUED = >= 4 | ❌ Aucun malus |
| IRD changepoint | pas encore détecté | score < 65 | ❌ malus -10 si score 40-60 |
| OrderFlow | score = 65/100 | — | Base score 65 |

**Résultat du scoring :**
```
score_final = 65 (OF) + 30 (MTF) - 0 (fatigue) - 0 (physics) + 10 (IRD moderate) = 105 → clampé 100
→ SETUP_A (score >= 85 + MTF 3/3) → risk × 1.5 → TRADE AGRESSIF
```
Le bot entre en BUY avec la taille maximale. Le marché continue de décélérer et reverse 3 bougies plus tard.

### Scénario "légèrement au-dessus des seuils"

| Condition | Valeur | Verdict |
|---|---|---|
| ATR récent = 58% ATR ancien | 58% | ✅ +4 pts |
| Volume récent = 58% volume ancien | 58% | ✅ +3 pts |
| momentum_fatigue | score = 7/10 | **HIGH** → MALUS |
| market_state | FATIGUED (score = 4.5) | **FATIGUED** → MALUS |
| MTF factor | 0.6 | (MTF 3/3) |

```
MALUS_FATIGUE           = -15 × 0.6 = -9
MALUS_MOMENTUM_FATIGUE  = -15 × 0.6 = -9

score_final = 65 (OF) + 30 (MTF) + 10 (IRD) - 9 - 9 = 87 → SETUP_A encore ✅
```
Même avec les deux signaux déclenchés, le score reste à 87. Le bot entre toujours.

---

## 7. FAILLES IDENTIFIÉES — Tableau récapitulatif

| # | Faille | Localisation | Impact |
|---|---|---|---|
| **F1** | `mtf_malus_factor = 0.6` appliqué à TOUS les malus fatigue/physics | `advanced_scoring.py` lignes 131, 136, 145, 150, 158, 167, 187, 195, 202, 207, 219 | La protection est réduite de 40% exactement quand le bot trade (MTF 3/3) |
| **F2** | `momentum_fatigue HIGH` nécessite 60% de baisse (seuils trop stricts) | `market_fatigue_analyzer.py` lignes 279, 290, 313 | Les épuisements graduels (30-40% de baisse) sont invisibles au scoring |
| **F3** | `MEDIUM` fatigue = zéro malus dans scoring | `advanced_scoring.py` — seulement `== 'HIGH'` vérifié | Un marché en déclin modéré n'est pas pénalisé |
| **F4** | `new_trend` (vote majoritaire) peut contredire `institutional_score` | `institutional_reversal_detector.py` ligne 1716-1728 | VETO_REVERSAL ne se déclenche pas si les votes sont partagés (NEUTRAL) |
| **F5** | VETO_REVERSAL nécessite 2 conditions simultanées | `advanced_scoring.py` ligne 263, `decision_pipeline.py` ligne 1327 | Si `new_trend = NEUTRAL` avec score >= 65 → pas de veto malgré score élevé |
| **F6** | Centripetal movement (`reversal_likely`) non extrait dans scoring | `advanced_scoring.py` — `centripetal_acceleration` non utilisé | Signal "prix trop éloigné de la moyenne" complètement ignoré en scoring |
| **F7** | CAPITAL_FLOWS doublement compté | `institutional_reversal_detector.py` lignes 1504-1508 et 1523-1525 | Légère distorsion du score institutionnel à la hausse |
| **F8** | `buyer_fatigue/seller_fatigue` dépend de ticks avec colonne `side` | `market_fatigue_analyzer.py` ligne 115 | Si ticks non disponibles → 60% du score composite fatigue = 0 |

---

## 8. OBSERVATIONS SUR LA CONCEPTION

### Ce qui était voulu vs ce qui se passe

La logique du `mtf_malus_factor = 0.6` repose sur ce raisonnement :
> *"Si le MTF macro confirme la direction, les signaux de bruit local (fatigue, physique) sont moins fiables. On réduit leur impact."*

C'est raisonnable pour des faux positifs (bruit de courte durée pendant une vraie tendance).
**Mais c'est contre-productif en fin de tendance** :
- Le MTF est un indicateur **directionnel** basé sur la direction des N dernières bougies fermées
- Il reste bullish tant que les 5 dernières M5 sont majoritairement vertes
- Il est **structurellement en retard** de 1 à 5 bougies sur le renversement
- Pendant cet intervalle, fatigue et physique détectent déjà l'épuisement
- Mais leurs signaux sont précisément réduits à cause du MTF encore aligné

**La réduction MTF protège contre le bruit court terme mais aveugle le bot aux signaux d'épuisement de fin de tendance.**

### Temporalité réelle de chaque couche

| Couche | Temporalité | Ce qu'elle détecte vraiment |
|---|---|---|
| MTF | Retard 1–5 bougies | Direction historique des dernières bougies |
| IRD Changepoint | Retard 0–10 bougies M5 | Rupture statistique dans la série de prix |
| IRD CVD Divergence | Avance 0–3 bougies | Désalignement CVD/prix (signal avancé) |
| IRD Smart Money | Retard 5–20 bougies M5 | Wyckoff : distribution/accumulation |
| Fatigue Momentum | Avance 0–2 bougies | Décélération ATR/volume (signal avancé si seuils bas) |
| Fatigue Absorption | Temps réel | Prix stagne malgré volume (meilleur signal instantané) |
| Physics Inertie | Temps réel | Cohérence des 5 derniers momentum |
| Physics Centripète | Temps réel | Distance à la moyenne 20 périodes (**ignoré en scoring**) |

Les signaux les plus avancés (CVD divergence, momentum fatigue, absorption) sont soit sous-seuillés,
soit réduits par le MTF factor.

---

## 9. CONCLUSION

Le bot détecte les renversements **après** qu'ils ont commencé, pas avant.
Les seuls signaux véritablement avancés sont structurellement inhibés :

- La **CVD divergence** nécessite IRD score >= 65 ET `new_trend` explicitement opposé (vote fragile)
- La **momentum fatigue** a des seuils à -40% qui ratent les épuisements graduels
- L'**absorption** est le meilleur signal instantané mais dépend des ticks avec colonne `side`
- Le **centripète** est calculé mais non utilisé en scoring

Le MTF bonus (+30) combiné à la réduction malus (×0.6) crée une asymétrie systématique qui favorise
l'entrée en fin de tendance lors des configurations MTF 3/3.

---

*Rapport généré le 24/02/2026 à partir du code source actuel.*
*Fichiers analysés : `market_fatigue_analyzer.py`, `market_physics_analyzer.py`,*
*`institutional_reversal_detector.py`, `advanced_scoring.py`, `decision_pipeline.py`*
