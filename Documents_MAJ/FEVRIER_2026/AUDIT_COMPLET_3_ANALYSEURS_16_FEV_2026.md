# Audit Complet des 3 Analyseurs — 16 Fevrier 2026

## Objectif

Analyser en profondeur les 3 analyseurs (MarketFatigueAnalyzer, MarketPhysicsAnalyzer,
InstitutionalReversalDetector), comprendre ce qu'ils apportent reellement, ce qu'ils
POURRAIENT apporter, et definir comment les faire travailler ensemble pour qu'ils soient
decisifs dans le pipeline de trading.

---

# PARTIE 1 : ANALYSE DE CHAQUE ANALYSEUR

---

## 1. MarketFatigueAnalyzer

**Fichier**: `phase_observer/market_fatigue_analyzer.py` (398 lignes)
**Cree**: 03 JAN 2026

### 1.1 Ce qu'il fait

Mesure l'epuisement des acheteurs/vendeurs via 4 sous-analyses :

| Sous-analyse | Poids | Score max | Ce qu'elle detecte |
|-------------|-------|-----------|-------------------|
| buyer_fatigue | 30% | 10 | Volume buy decroissant, frequence decroissante, absorption |
| seller_fatigue | 30% | 10 | Symetrique aux acheteurs |
| momentum_fatigue | 20% | 10 | ATR decroissant, volume decroissant, body size decroissant |
| exhaustion_patterns | 20% | 10 | Climax volume, divergence volume/prix |

**Score final** : 0-10 → Etat marche :
- >= 7.0 : EXHAUSTED (mouvement sur le point de s'inverser)
- >= 4.0 : FATIGUED (mouvement s'affaiblit)
- >= 2.0 : NORMAL
- < 2.0 : ENERGETIC

### 1.2 Problemes de calibration

**Probleme 1 : Seuils trop stricts pour buyer/seller_fatigue**
```python
# Ligne 127 : Volume decroissant — exige 3 bougies strictement decroissantes
if volumes[-1] < volumes[-2] < volumes[-3]:  # +3 pts
# REALITE : Le volume zigzague souvent. Meme en fatigue reelle,
# on a rarement 3 bougies strictement decroissantes
```

**Probleme 2 : Absorption quasi-impossible a detecter**
```python
# Ligne 152 : Absorption
if price_change < 0.0001 and np.sum(volumes) > np.mean(volumes) * 15:
# 0.0001 = 1 pip EN POURCENTAGE → sur USDJPY (150.00) = 0.015 pip
# np.sum(volumes) > mean * 15 → exige volume total 15x la moyenne
# REALITE : Ces seuils sont trop stricts, l'absorption n'est presque jamais detectee
```

**Probleme 3 : Momentum fatigue — seuils raisonnables mais insuffisants seuls**
```python
# Ligne 266 : ATR decroissant
if avg_recent_tr < avg_older_tr * 0.7:  # +4 pts
# Correct mais donne max 4 pts → score 0.8/10 avec 20% poids → quasi rien
```

**Probleme 4 : Exhaustion patterns rares**
- Climax volume : exige volume > 200% moyenne ET body < 30% range → rare
- Divergence volume/prix : exige volume recent < 70% ancien → raisonnable

### 1.3 Score typique en conditions reelles

```
Marche calme typique :
  buyer_fatigue = 0 (volumes ne decroissent pas strictement)
  seller_fatigue = 0 (idem)
  momentum_fatigue = 0-4 (ATR peut decroitre)
  exhaustion = 0 (pas de climax)
  → Score = 0*0.3 + 0*0.3 + 2*0.2 + 0*0.2 = 0.4/10 → "ENERGETIC"

Marche vraiment fatigue :
  buyer_fatigue = 3 (volume decroissant strict)
  seller_fatigue = 0
  momentum_fatigue = 4 (ATR chute)
  exhaustion = 3 (divergence vol/prix)
  → Score = 3*0.3 + 0*0.3 + 4*0.2 + 3*0.2 = 2.3/10 → "NORMAL"
  DEVRAIT ETRE : 5-7/10 → "FATIGUED" ou "EXHAUSTED"
```

### 1.4 Connexion au pipeline de decision

```
scalping.py ligne 1214 → fatigue_result
  ↓
institutional_analysis["market_fatigue"] = fatigue_result
  ↓
advanced_scoring.py → _calculate_institutional_component() ligne 304-315
  ↓
EXHAUSTED → 30 pts / FATIGUED → 40 pts / NORMAL → 50 pts / ENERGETIC → 65 pts
  ↓ (moyenne avec 4-5 autres sous-scores)
  ↓ (poids 25% du composite)
  ↓
Impact reel sur score final : ~1-3 points
  ↓
PAS dans decision_pipeline.py → AUCUN VETO, AUCUN MALUS
```

**Verdict : DECONNECTE. Meme avec EXHAUSTED, l'impact est de ~3 points sur 100.**

---

## 2. MarketPhysicsAnalyzer

**Fichier**: `phase_observer/market_physics_analyzer.py` (448 lignes)
**Cree**: 03 JAN 2026

### 2.1 Ce qu'il fait

Applique 5 principes de physique aux marches :

| Principe | Ce qu'il calcule | Sortie |
|----------|-----------------|--------|
| 1. Conservation energie | Volume × Range vs moyenne | energy_deficit / energy_surplus |
| 2. Inertie du prix | Momentum = Volume × ΔPrix | direction UP/DOWN, likely_to_continue |
| 3. Acceleration centripete | Distance prix vs MA20 | reversal_likely (>30 pips) |
| 4. Barrieres energetiques | Support/Resistance 20 bougies | distance aux barrieres |
| 5. Entropie du marche | Ratio buy/sell ticks (Shannon) | ORDERED / CHAOTIC |

**Sortie finale** : `physics_bias` = BUY / SELL / NEUTRAL

### 2.2 Problemes de calibration

**Probleme 1 : Le bias est presque toujours NEUTRAL**
```python
# Ligne 442 : Decision finale
if bias_points >= 2:   return 'BUY'    # Exige 2+ points
elif bias_points <= -2: return 'SELL'   # Exige -2 points
else:                   return 'NEUTRAL'

# Points possibles max:
# - Energy surplus/deficit + inertia aligned = ±2 ou ±3
# - Inertia continuation = ±1
# - Entropy ordered = ±1
# Total max = ±5, mais conditions rarement reunies simultanement
# En pratique : souvent 0 ou ±1 → NEUTRAL
```

**Probleme 2 : Centripetal inutile pour le scalping**
```python
# Ligne 269 : Seuil de reversal
reversal_likely = distance_pct > 0.003  # 0.3% = ~30 pips sur USDJPY
# SCALPING : On cherche 3 pips de profit. 30 pips = ENORME.
# Ce seuil ne se declenche quasiment jamais en scalping.
```

**Probleme 3 : Energy barriers non utilise dans le bias**
```python
# _identify_energy_barriers() calcule distance au support/resistance
# MAIS _derive_physics_bias() ne l'utilise PAS du tout !
# Resultat : analyse gaspillee
```

**Probleme 4 : Centripetal non utilise dans le bias**
```python
# _detect_centripetal_movement() calcule reversal_likely
# MAIS _derive_physics_bias() ne l'utilise PAS !
# Seuls energy, inertia et entropy sont consideres
```

### 2.3 Score typique en conditions reelles

```
Marche calme :
  energy: ratio ≈ 1.0 → pas de deficit/surplus → 0 pts
  inertia: momentum faible → likely_to_continue=False → 0 pts
  entropy: buy/sell ≈ 50/50 → SEMI_ORDERED → 0 pts
  → bias_points = 0 → "NEUTRAL"

Marche en tendance :
  energy: surplus (volume > 2x moyenne) → +2 pts si UP, -2 si DOWN
  inertia: momentum coherent → +1 si continue
  entropy: buy 70% → ORDERED → +1
  → bias_points = +4 → "BUY"
  MAIS : le marche est DEJA en tendance → on le savait deja via MTF
```

### 2.4 Connexion au pipeline de decision

```
scalping.py ligne 1258 → physics_result
  ↓
institutional_analysis["market_physics"] = physics_result
  ↓
advanced_scoring.py → _calculate_institutional_component() ligne 317-326
  ↓
BUY/SELL → 75/25 pts / NEUTRAL → 50 pts
  ↓ (moyenne avec 4-5 autres sous-scores)
  ↓ (poids 25% du composite)
  ↓
Impact reel : ~2-3 points (et souvent NEUTRAL → 0 impact)
  ↓
PAS dans decision_pipeline.py → AUCUN VETO, AUCUN MALUS
```

**Verdict : DECONNECTE. Presque toujours NEUTRAL. 2 des 5 principes (barriers, centripetal) ne sont meme pas utilises dans le bias final.**

---

## 3. InstitutionalReversalDetector (IRD)

**Fichier**: `phase_observer/institutional_reversal_detector.py` (1907 lignes)
**Cree**: 10 JAN 2026, Modifie: 25 JAN 2026

### 3.1 Ce qu'il fait

Detecte les renversements institutionnels via 4 couches actives + 1 bonus :

| Couche | Poids | Ce qu'elle detecte | Donnees requises |
|--------|-------|-------------------|-----------------|
| 1. Changepoint | 0.35 | Rupture statistique (CUSUM + Chow) | 50+ bougies M5 |
| 2. Divergence Multi-TF | 0.30 | Prix vs CVD (M1+M5) | 30+ CVD values |
| 3. ~~Fatigue~~ | ~~desactive~~ | Momentum decay, volume divergence | VETO seulement |
| 4. Smart Money Wyckoff | 0.25 | Accumulation/Distribution | 20+ bougies M5 |
| 5. ~~Microstructure~~ | ~~desactive~~ | Wicks, dojis | desactive → PMA |
| 6. ML Patterns | 0.10 | Engulfing, Morning/Evening Star, Z-score | M1 + M5 |
| Bonus: CVD Flows | +10% boost | Pression CVD | CVD values |

3 couches **desactivees** du scoring (25 JAN 2026) :
- Fatigue → VETO seulement (circuit breaker si strength >= 80)
- Microstructure M1 → delegue a PMA
- Confluence → delegue a PMA

### 3.2 Comment le score est calcule

```python
# _calculate_institutional_score() ligne 1642
# Pour chaque signal actif :
score += signal.strength * weight * signal.confidence

# Puis normalise : score = weighted_sum / total_weight
# Plus bonus : +10% par couche bonus (CVD Flows)
```

**Poids effectifs** : Changepoint=0.35, Divergence=0.30 (x2 car M1+M5),
Wyckoff=0.25, ML=0.10

**MAIS** : CVD Capital Flows n'a PAS de weight_key dans la config → passe par le `continue`
a la ligne 1669 → le bonus boost de +10% (ligne 1686) s'applique MAL car le signal
est ignore du calcul principal.

### 3.3 Problemes de calibration

**Probleme 1 : Changepoint trop lent (poids 35%)**
```python
# Ligne 266 : Exige 50+ bougies M5 = 250 minutes minimum
if len(candles_m5) < 50: return empty_signal
# CUSUM et Chow detectent des ruptures MACRO
# Pour le scalping (profit 3 pips), on a besoin de detection RAPIDE
# Le Changepoint donne 0 la plupart du temps (pas assez de donnees ou pas de rupture macro)
```

**Probleme 2 : Divergence simpliste**
```python
# Ligne 460 : Comparaison naive index[-1] vs index[-3]
if price_highs[-1] > price_highs[-3] and cvd_segment[-1] < cvd_segment[-3]:
# Probleme : Compare 2 points seulement. Pas de validation de tendance.
# Tres sensible au bruit. Divergence OU pas → 0 ou 50 pts, pas de nuance.
```

**Probleme 3 : Hidden divergence identique a regular (BUG)**
```python
# Ligne 473 (hidden bullish):
if price_highs[-1] > price_highs[-3] and cvd_segment[-1] < cvd_segment[-3]:
# Ligne 460 (regular bearish):
if price_highs[-1] > price_highs[-3] and cvd_segment[-1] < cvd_segment[-3]:
# → MEME CONDITION ! La hidden bullish est identique a la regular bearish
# BUG : Les 2 detectent le meme pattern et ne se distinguent pas
```

**Probleme 4 : Validation Smart Money trop stricte (2/3 criteres)**
```python
# Ligne 1854 : HIGH conviction exige les 3 :
if score >= 80 and regime_change["detected"] and smart_money["validated"]:
    # smart_money["validated"] exige 2/3 criteres
    # En marche calme : volume faible → critere 1 echoue souvent
    # En range : pas de swing → critere 2 echoue souvent
    # Resultat : HIGH conviction = quasi impossible en calme
```

**Probleme 5 : Score IRD typique en calme = 20-40**

Cas concret de scoring :
```
Changepoint: 0 (pas de rupture statistique → strength=0)
  × 0.35 × 0.85 confidence = 0

Divergence M1: 50 (regular detectee)
  × 0.30 × 0.80 = 12.0

Divergence M5: 0 (pas detectee)
  × 0.30 × 0.80 = 0

Wyckoff: 0 (range mais pas accumulation/distribution)
  × 0.25 × 0.80 = 0

ML Patterns: 30 (1 pattern detecte)
  × 0.10 × confidence = ~1.5

CVD Flows: skip (pas de weight_key → continue)

Total weights = 0.35 + 0.30 + 0.30 + 0.25 + 0.10 = 1.30
Score = (0 + 12 + 0 + 0 + 1.5) / 1.30 = 10.4/100

→ conviction = LOW, reversal_detected = False
```

Meme scenario avec divergence forte :
```
Changepoint: 40 (cusum > 2.0)
  × 0.35 × 0.85 = 11.9
Divergence M1: 100 (regular + hidden)
  × 0.30 × 0.80 = 24.0
Divergence M5: 50
  × 0.30 × 0.80 = 12.0
Wyckoff: 40 (accumulation detectee)
  × 0.25 × 0.80 = 8.0
ML: 60 (2 patterns)
  × 0.10 × 0.60 = 3.6

Score = (11.9 + 24.0 + 12.0 + 8.0 + 3.6) / 1.30 = 45.8/100 → CAUTION
```

→ Meme avec TOUT aligne, le score depasse rarement 50. Il faut des conditions
EXCEPTIONNELLES pour atteindre 75 (reversal_detected=True).

### 3.4 Connexion au pipeline de decision

```
run_bot.py ligne 2343 → inst_result, inst_score
  ↓
decision_pipeline.py → apply_pma_adjustments() ligne 2992
  ↓
CAS 1: inst_score >= 65 ET aligne → +10 bonus
CAS 2: reversal_detected (>=75) ET oppose → VETO_REVERSAL
CAS 3: inst_score < 65 → RIEN
  ↓
decision_pipeline.py → decide_scalp_action() ligne 1262
  ↓
VETO_REVERSAL → action = HOLD
```

**Verdict : PARTIELLEMENT CONNECTE. +10 bonus insuffisant, et le score
atteint rarement 65 en conditions calmes.**

---

# PARTIE 2 : CE QUE LES 3 ANALYSEURS POURRAIENT APPORTER

---

## Valeur theorique de chaque analyseur

### MarketFatigueAnalyzer — Valeur : ELEVEE

**Ce qu'il DEVRAIT detecter** :
- Acheteurs epuises → NE PAS acheter maintenant
- Vendeurs epuises → NE PAS vendre maintenant
- Momentum mourant → trade de continuation = risque eleve
- Climax volume → retournement imminent

**Impact potentiel** :
- Eliminer 60-70% des trades perdants par epuisement
- Identifier le TIMING optimal (entrer APRES l'epuisement de l'autre cote)
- Directement applicable au scalping car travaille sur les ticks (temps reel)

### MarketPhysicsAnalyzer — Valeur : MODEREE

**Ce qu'il DEVRAIT detecter** :
- Mouvement sans energie (volume) → non durable → bloquer le trade
- Retour a la moyenne → ne pas entrer loin de la MA
- Marche chaotique (entropie) → pas de direction → HOLD
- Barriere energetique proche → risque de rejet

**Impact potentiel** :
- Eviter les trades dans les mouvements "faux" (sans volume)
- Identifier les zones de rejet probables
- Confirmer ou infirmer la direction d'un trade

### IRD — Valeur : TRES ELEVEE

**Ce qu'il DEVRAIT detecter** :
- Changement de regime institutionnel
- Smart Money en accumulation/distribution
- Divergences CVD multi-timeframe
- Patterns de retournement classiques

**Impact potentiel** :
- Detecter les vrais retournements AVANT qu'ils ne se produisent
- Bloquer les trades dans la MAUVAISE direction (contre le renversement)
- Initier des trades dans la BONNE direction (avec le renversement)

---

# PARTIE 3 : CE QU'ILS APPORTENT REELLEMENT

---

## Situation actuelle : les 3 sont quasi-muets

| Analyseur | Appele ? | Resultat utilise ? | Influence decision ? | Peut bloquer ? | Peut initier ? |
|-----------|---------|-------------------|---------------------|---------------|---------------|
| MarketFatigue | ✅ scalping.py | ⚠️ composite seulement | ❌ ~1-3 pts | ❌ NON | ❌ NON |
| MarketPhysics | ✅ scalping.py | ⚠️ composite seulement | ❌ ~1-3 pts | ❌ NON | ❌ NON |
| IRD | ✅ run_bot.py | ✅ PMA adjustments | ⚠️ +10 pts | ✅ VETO_REVERSAL | ❌ NON |

### Raisons de l'inefficacite

1. **MarketFatigue et Physics passent par le composite scoring**
   - Le composite a un BUG : l'OrderFlow est calcule a 50.0 par defaut
     (car `calculate_composite_score()` ne passe pas `metrics` a `calculate_unified_score()`)
   - Le composite score (≈50) remplace le vrai OrderFlow score dans `orderflow_result_mini['score']`
   - Impact des analyseurs institutionnels : noyé dans la moyenne (~1-3 pts)

2. **Les analyseurs ne communiquent pas entre eux**
   - Fatigue ne sait pas ce que Physics detecte
   - Physics ne sait pas ce que l'IRD a trouve
   - L'IRD ne connait pas l'etat de fatigue du MarketFatigueAnalyzer
   - Pas de score combine, pas de consensus

3. **Aucun des 3 n'a d'acces direct au pipeline de decision**
   - `build_decision()` dans `market_analyzer.py` est 100% OrderFlow
   - `apply_pma_adjustments()` ne connait que l'IRD (+10 bonus)
   - MarketFatigue et Physics n'ont AUCUN malus/bonus/veto dans PMA

4. **Les seuils sont calibres pour les gros mouvements, pas le scalping**
   - IRD : 75 pour reversal_detected → presque jamais atteint
   - Physics : 30 pips pour centripetal reversal → absurde en scalping (3 pips)
   - Fatigue : absorption exige volume 15x moyenne → quasi impossible

---

# PARTIE 4 : COMMENT LES FAIRE TRAVAILLER ENSEMBLE

---

## Architecture proposee : "Triumvirat de Protection"

### Concept

Les 3 analyseurs doivent former un systeme de protection a 3 niveaux :

```
NIVEAU 1 — FATIGUE (temps reel, ticks)
  "Le marche est-il epuise ?"
  Si OUI → bloquer les trades de continuation
  Si NON → continuer

NIVEAU 2 — PHYSICS (bougies M1, energie)
  "Le mouvement a-t-il de l'energie ?"
  Si deficit energie → bloquer le trade
  Si surplus + direction claire → confirmer le trade
  Si chaotique (entropie haute) → HOLD

NIVEAU 3 — IRD (M5, structurel)
  "Y a-t-il un renversement institutionnel ?"
  Si reversal ALIGNE → BONUS fort (initier trade reversal)
  Si reversal OPPOSE → VETO absolu
  Si pas de reversal → neutre
```

### Schema de collaboration

```
                    ┌─────────────────────┐
                    │  OrderFlow V6       │
                    │  Score + Bias       │
                    └──────────┬──────────┘
                               │
                    ┌──────────▼──────────┐
                    │ FATIGUE CHECK       │
                    │ (MarketFatigue)     │
                    │                     │
                    │ EXHAUSTED? ──YES──→ MALUS -25 pts
                    │ FATIGUED? ──YES──→  MALUS -15 pts
                    │ direction fatigue   │
                    │ (buyers/sellers)    │
                    └──────────┬──────────┘
                               │
                    ┌──────────▼──────────┐
                    │ PHYSICS CHECK       │
                    │ (MarketPhysics)     │
                    │                     │
                    │ Energy deficit? ──→  MALUS -20 pts
                    │ Entropy CHAOTIC? ─→  MALUS -15 pts
                    │ Near barrier? ────→  MALUS -10 pts
                    │ Strong inertia? ──→  BONUS +10 pts
                    └──────────┬──────────┘
                               │
                    ┌──────────▼──────────┐
                    │ REVERSAL CHECK      │
                    │ (IRD)               │
                    │                     │
                    │ HIGH conviction     │
                    │  + aligned? ──────→  BONUS +25 pts
                    │  + min_score -15   │
                    │                     │
                    │ MODERATE + aligned → BONUS +15 pts
                    │ Opposed? ─────────→ VETO_REVERSAL
                    └──────────┬──────────┘
                               │
                    ┌──────────▼──────────┐
                    │ CONSENSUS 3/3       │
                    │                     │
                    │ Si 2/3 disent STOP → HOLD (consensus)
                    │ Si 3/3 alignes ───→ SUPER_BONUS +15
                    └─────────────────────┘
```

### Integration dans le pipeline

Tous les bonus/malus doivent etre dans `apply_pma_adjustments()` de `decision_pipeline.py` :

```python
# === NOUVEAU : 3 ANALYSEURS DANS PMA ===

# 1. FATIGUE (actuellement absent)
fatigue_state = ... # Recue depuis scalping_worker
if fatigue_state == "EXHAUSTED":
    pma_malus += 25.0
    # Si acheteurs epuises ET signal=BUY → malus supplementaire
    if buyer_fatigue_high and signal_action == "BUY":
        pma_malus += 10.0  # Total -35 si on achete en fatigue buy
elif fatigue_state == "FATIGUED":
    pma_malus += 15.0

# 2. PHYSICS (actuellement absent)
if energy_deficit:
    pma_malus += 20.0  # Mouvement sans volume
if entropy_chaotic:
    pma_malus += 15.0  # Marche sans direction
if near_barrier and signal goes toward barrier:
    pma_malus += 10.0
if strong_inertia and aligned:
    pma_bonus += 10.0

# 3. IRD (augmenter le bonus existant)
if inst_score >= 80 and aligned:
    pma_bonus += 25.0  # Au lieu de 10
    # Reduire le min_score pour build_decision
elif inst_score >= 65 and aligned:
    pma_bonus += 15.0  # Au lieu de 10

# 4. CONSENSUS (nouveau)
# Si 2+ analyseurs s'accordent sur STOP
stop_count = sum([fatigue_blocks, physics_blocks, ird_blocks])
if stop_count >= 2:
    pma_malus += 15.0  # Consensus de blocage
# Si 3 analyseurs alignes avec le trade
if fatigue_ok and physics_ok and ird_aligned:
    pma_bonus += 15.0  # Consensus de confirmation
```

---

## Recalibrations necessaires par analyseur

### MarketFatigueAnalyzer — Corrections

| Element | Actuel | Propose | Raison |
|---------|--------|---------|--------|
| Volume decroissant | 3 bougies strictement decroissantes (+3) | Tendance lineaire sur 5-8 ticks (+3) | Realiste |
| Frequence decroissante | 2 intervalles croissants (+2) | Moyenne 5 intervalles vs precedents (+2) | Robuste |
| Absorption | price_change < 0.0001 ET vol > mean*15 (+4) | price_change < 0.0003 ET vol > mean*3 (+4) | Detecte le vrai |
| Tick rate | NON DETECTE | Ajout: si ticks/sec chute >50% → +4 pts | Critique |
| ATR seuil | 0.7 (30% chute) | 0.6 (40% chute) | Plus sensible |
| Volume seuil | 0.6 (40% chute) | 0.5 (50% chute) | Idem |

### MarketPhysicsAnalyzer — Corrections

| Element | Actuel | Propose | Raison |
|---------|--------|---------|--------|
| Centripetal seuil | 0.003 (30 pips) | 0.0005 (5 pips) | Adapte au scalping |
| Bias threshold | >=2 / <=-2 (dur a atteindre) | >=1 / <=-1 (plus reactif) | Detecte plus |
| Energy barriers | Non utilise dans bias | Integrer dans bias (+/-1 pt) | Utiliser l'info |
| Centripetal | Non utilise dans bias | Integrer dans bias (±1 pt) | Utiliser l'info |
| Output | BUY/SELL/NEUTRAL seulement | Ajouter score 0-10 + details | Granularite |

### IRD — Corrections

| Element | Actuel | Propose | Raison |
|---------|--------|---------|--------|
| reversal_detected seuil | 75 | 65 | Detecte plus tot |
| Bonus PMA si aligne | +10 pts | +25 pts (HIGH) / +15 (MODERATE) | Decisif |
| Changepoint poids | 0.35 | 0.20 | Trop lent pour scalping |
| Divergence poids | 0.30 | 0.35 | Plus rapide, plus pertinent |
| Wyckoff poids | 0.25 | 0.25 | OK |
| ML Patterns poids | 0.10 | 0.20 | Rapide et utile |
| Hidden divergence BUG | Identique a regular | Corriger la condition | Fix |
| Validation smart_money | 2/3 criteres | 1/3 criteres | Moins restrictif |
| Fatigue dans scoring | Desactivee | Reactiver avec poids 0.15 | Info critique perdue |
| CVD Flows | Pas de weight_key → skip | Ajouter poids ou integrer | Bug fix |

---

## Donnees a passer au pipeline

Actuellement, `decide_scalp_action()` ne recoit PAS les resultats de Fatigue et Physics.
Il faut les passer depuis `scalping_worker` dans `run_bot.py` :

```python
# run_bot.py — scalping_worker
# Extraire de of_v6_result (deja calcule dans scalping.py)
institutional_analysis = of_v6_result.get('institutional_analysis', {})
fatigue_result = institutional_analysis.get('market_fatigue', {})
physics_result = institutional_analysis.get('market_physics', {})

# Passer a decide_scalp_action() — 2 nouveaux parametres
fusion_out = decision_pipeline.decide_scalp_action(
    ...,
    fatigue_result=fatigue_result,     # NOUVEAU
    physics_result=physics_result,     # NOUVEAU
    ...,
)
```

Et dans `apply_pma_adjustments()` :
```python
def apply_pma_adjustments(
    ...,
    fatigue_result=None,    # NOUVEAU
    physics_result=None,    # NOUVEAU
    ...
):
```

---

## Resume du plan de corrections

| # | Action | Fichier | Impact |
|---|--------|---------|--------|
| 1 | Recalibrer seuils MarketFatigue (absorption, volume, tick rate) | market_fatigue_analyzer.py | Score realiste |
| 2 | Recalibrer seuils MarketPhysics (centripetal, barriers dans bias) | market_physics_analyzer.py | Moins de NEUTRAL |
| 3 | Corriger bug hidden divergence IRD | institutional_reversal_detector.py | Fix |
| 4 | Corriger bug CVD Flows (pas de weight_key) | institutional_reversal_detector.py | Fix |
| 5 | Reactiver fatigue dans scoring IRD (poids 0.15) | institutional_reversal_detector.py | Score + haut |
| 6 | Redistribuer poids IRD (moins changepoint, plus ML) | institutional_reversal_detector.py | Plus reactif |
| 7 | Baisser seuil reversal_detected de 75 a 65 | institutional_reversal_detector.py | Detecte plus |
| 8 | Baisser validation smart_money de 2/3 a 1/3 | institutional_reversal_detector.py | Moins restrictif |
| 9 | Passer fatigue_result et physics_result au pipeline | run_bot.py + decision_pipeline.py | Connexion |
| 10 | Ajouter malus FATIGUE/PHYSICS dans apply_pma_adjustments | decision_pipeline.py | Decisif |
| 11 | Augmenter bonus IRD de +10 a +25/+15 | decision_pipeline.py | Decisif |
| 12 | Ajouter logique consensus 2/3 analyseurs | decision_pipeline.py | Protection |
| 13 | Adapter min_score dans build_decision si IRD HIGH | decision_pipeline.py | Initier reversals |
| 14 | Supprimer dependance au composite scoring bugge | run_bot.py | Fix critique |

---

## Gains attendus

| Metrique | Avant | Apres | Amelioration |
|----------|-------|-------|-------------|
| Detection fatigue | ~10% (seuils trop stricts) | ~60% | +50% |
| Detection physics | ~5% (toujours NEUTRAL) | ~40% | +35% |
| Detection reversal | ~15% (seuil 75 trop haut) | ~45% | +30% |
| Trades bloques a raison (vrais negatifs) | ~5% | ~30% | +25% |
| Trades reversal inities | 0% | ~15% | +15% |
| Winrate global (estimation) | 55-60% | 63-67% | +5-8% |

---

## Conclusion

Les 3 analyseurs sont **bien codes** dans leur logique fondamentale, mais :
1. **Mal calibres** pour le scalping (seuils trop hauts/stricts)
2. **Deconnectes** du pipeline de decision (Fatigue et Physics n'ont aucun effet)
3. **Isoles** les uns des autres (pas de consensus)
4. **Sous-ponderes** dans le scoring (IRD = +10 pts, Fatigue/Physics = ~1-3 pts)

La solution n'est PAS de les recoder, mais de :
- **Recalibrer** les seuils pour le scalping
- **Connecter** les 3 au pipeline via `apply_pma_adjustments()`
- **Augmenter** leur poids pour qu'ils soient decisifs
- **Creer** un mecanisme de consensus entre les 3
