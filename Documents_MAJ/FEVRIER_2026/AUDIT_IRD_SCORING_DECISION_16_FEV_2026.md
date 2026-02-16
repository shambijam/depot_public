# Audit IRD -> Scoring -> Decision — 16 Fevrier 2026

## Probleme signale

Depuis plusieurs sessions, **aucune decision de trade n'est prise sur les renversements**,
surtout quand le marche est calme. Le reversal (IRD) est bien branche au pipeline,
mais le scoring et la decision qui en decoulent ne permettent jamais de declencher un trade.

---

## 1. Ce que l'IRD detecte

**Fichier**: `phase_observer/institutional_reversal_detector.py` (ligne 115-158)

```python
detect_reversal() retourne:
{
    "institutional_score": float (0-100),        # Score composite 4 couches
    "conviction_level": "HIGH|MODERATE|CAUTION|LOW",
    "new_trend": "BULLISH|BEARISH|NEUTRAL",      # Direction du renversement
    "reversal_detected": institutional_score >= 75,  # SEUIL: 75
    "regime_change": dict,
    "signals_breakdown": list,
    "smart_money_confirmation": dict,
}
```

### Les 4 couches de scoring IRD (poids):
| Couche | Poids | Role |
|--------|-------|------|
| Changepoint Detection | 0.35 (35%) | Statistique - rupture de structure |
| Divergence Multi-TF (M1+M5) | 0.30 (30%) | CVD vs prix |
| Smart Money Wyckoff | 0.25 (25%) | Accumulation/Distribution |
| ML Pattern Recognition | 0.10 (10%) | Patterns ML |

Couches **desactivees du scoring** (25 JAN 2026):
- Fatigue → devient VETO separee (circuit breaker)
- Microstructure M1 → geree par PMA
- Confluence → geree par PMA

---

## 2. Comment l'IRD est passe au pipeline

**Fichier**: `run_bot.py` (lignes ~2343-2436)

```python
# Detection
inst_result = institutional_detector.detect_reversal(market_data_for_ird)
inst_score = inst_result.get('institutional_score', 0.0)
new_trend = inst_result.get('new_trend', 'NEUTRAL')
reversal = inst_result.get('reversal_detected', False)

# Fatigue separee
fatigue_signal = institutional_detector.get_fatigue_signal(market_data_for_ird)

# Passe a decide_scalp_action()
inst_result=inst_result,
inst_score=inst_score,
inst_veto_fatigue=...,
inst_veto_reversal=...,
```

**Verdict**: Le passage est correct. Les donnees arrivent bien au pipeline.

---

## 3. PROBLEME CRITIQUE : build_decision() ignore 100% l'IRD

**Fichier**: `phase_observer/market_analyzer.py` (lignes 48-98)

```python
def build_decision(self, orderflow_result, min_score=75.0):
    """Decision directe basee sur OrderFlow V6 UNIQUEMENT"""
    score = orderflow_result.get("score", 0)
    bias = orderflow_result.get("bias", "NEUTRAL")

    if score >= min_score and bias in ["BUY", "SELL"]:
        return {"action": bias, "confidence": score / 100.0, ...}
    else:
        return {"action": "HOLD", ...}
```

**C'est le coeur du probleme**: `build_decision()` ne recoit AUCUN parametre IRD.
La decision de base (BUY/SELL/HOLD) depend **a 100% du score OrderFlow V6**.
L'IRD n'intervient qu'APRES, comme simple bonus/malus dans PMA.

---

## 4. Role reel de l'IRD dans le scoring (apply_pma_adjustments)

**Fichier**: `core/decision_pipeline.py` (lignes 2992-3011)

### BONUS 4: Signal Institutionnel (+10 pts seulement)

```python
# Condition: inst_score >= 65 (pas 75!)
if inst_result is not None and inst_score >= 65:
    inst_trend = inst_result.get('new_trend', 'NEUTRAL')

    # CAS 1: IRD ALIGNE avec signal OrderFlow → +10 pts
    if (inst_trend == "BULLISH" and signal_action == "BUY") or \
       (inst_trend == "BEARISH" and signal_action == "SELL"):
        pma_bonus += 10.0  # ← SEULEMENT 10 POINTS

    # CAS 2: IRD OPPOSE au signal OrderFlow → VETO REVERSAL (trade bloque)
    elif inst_result.get('reversal_detected', False):  # score >= 75
        _inst_veto_reversal = True  # BLOQUE LE TRADE
```

### Recapitulatif du role de l'IRD:

| Scenario | Effet | Impact reel |
|----------|-------|-------------|
| IRD aligne + score >= 65 | +10 pts bonus | Faible (10 pts sur seuil de 60) |
| IRD oppose + score >= 75 | VETO_REVERSAL | Trade bloque |
| IRD score < 65 | RIEN | Aucun effet |
| IRD score 65-74 aligne | +10 pts bonus | Faible |
| IRD score 65-74 oppose | RIEN | Pas de veto car reversal_detected=False |

**Le probleme**: L'IRD ne peut JAMAIS initier un trade. Il peut seulement:
- Ajouter un petit +10 a un trade deja decide par OrderFlow
- Bloquer un trade si le reversal est oppose (VETO)

---

## 5. Les malus qui empilent en marche calme

**Fichier**: `core/decision_pipeline.py` (lignes 2910-2940)

| Malus | Points | Condition | Frequent en calme? |
|-------|--------|-----------|-------------------|
| MALUS_MICRO_RES | -30 | Resistance STRONG/MODERATE < 1 pip | Oui (consolidation) |
| MALUS_REGIME | -20 | Regime RANGE/ACCUMULATION/DISTRIBUTION | **Tres frequent** |
| MALUS_FATIGUE | -50 | Circuit breaker (fatigue strength >= 80) | Probable |

**En marche calme, le regime est souvent RANGE/ACCUMULATION** → malus -20 systematique.

---

## 6. Seuils de decision et le mur du VETO_DUR

**Fichier**: `core/decision_pipeline.py` (lignes 3021-3025)

```python
VETO_DUR_THRESHOLD = 60.0
if signal_action in ["BUY", "SELL"] and score_ajuste < VETO_DUR_THRESHOLD:
    pma_veto_dur = True  # → action = HOLD, confidence = 0.0
```

### Tous les bonus PMA disponibles:

| Bonus | Points | Condition |
|-------|--------|-----------|
| BONUS_MTF_4/4 | +15 | Alignement parfait 4 timeframes |
| BONUS_MTF_3/4 | +10 | Alignement 3/4 timeframes |
| BONUS_FRESH_LEVEL | +10 | Niveau frais < 2 pips |
| BONUS_TREND_CONSISTENCY | +5 | Clarity >= 0.7 + Strength >= 0.6 |
| BONUS_INSTITUTIONAL | +10 | IRD aligne + score >= 65 |
| **Total max theorique** | **+50** | Tout aligne (rare) |

---

## 7. Simulation: Reversal en marche calme

### Scenario typique:

```
1. DETECTION IRD
   Score IRD = 72 (proche du seuil 75, typique en calme)
   reversal_detected = False (72 < 75)
   new_trend = BULLISH

2. ORDERFLOW V6 (marche calme)
   Score = 48 (faible volume, faible delta, faible CVD)
   Bias = BUY (faible conviction)

3. BUILD_DECISION (market_analyzer.py)
   48 < 75 (min_score) → action = HOLD
   ❌ DEJA BLOQUE ICI - le trade ne demarre meme pas

4. Meme si build_decision passait:
   Score brut = 48
   + BONUS_INSTITUTIONAL = +10 (aligne, score 72 >= 65)
   + BONUS_MTF_3/4 = +10 (si aligne)
   - MALUS_REGIME = -20 (RANGE en marche calme)
   = Score ajuste = 48

5. Score 48 < 60 (VETO_DUR)
   → HOLD

RESULTAT FINAL: HOLD (aucun trade)
```

### Scenario optimiste (reversal fort):

```
1. DETECTION IRD
   Score IRD = 82 (HIGH conviction - rare en calme)
   reversal_detected = True
   new_trend = BULLISH

2. ORDERFLOW V6 (marche calme)
   Score = 52
   Bias = BUY

3. BUILD_DECISION
   52 < 75 → action = HOLD
   ❌ BLOQUE

4. Meme hypothetiquement:
   Score brut = 52
   + BONUS_INSTITUTIONAL = +10
   + BONUS_MTF_4/4 = +15 (scenario ideal)
   - MALUS_REGIME = -20
   = Score ajuste = 57

5. Score 57 < 60 (VETO_DUR)
   → HOLD

RESULTAT: HOLD meme avec un reversal fort
```

---

## 8. Diagnostic: Pourquoi les reversals ne tradent JAMAIS en calme

### Cause 1: build_decision() est le premier mur
- Seuil `min_score = 75.0` pour OrderFlow
- En marche calme, OrderFlow produit 40-55 max
- Le trade est deja HOLD avant que l'IRD n'intervienne

### Cause 2: L'IRD n'est qu'un bonus (+10 pts)
- Un bonus de +10 sur un score de 48 = 58 → toujours sous VETO_DUR (60)
- L'IRD ne peut JAMAIS compenser un OrderFlow faible
- Il faudrait un bonus de +25-30 minimum pour etre significatif

### Cause 3: Les malus s'empilent en calme
- MALUS_REGIME (-20) est quasi-systematique en calme (RANGE/ACCUMULATION)
- MALUS_FATIGUE (-50) possible si marche epuise
- Ces malus annulent completement les bonus IRD

### Cause 4: Aucun mecanisme de "reversal override"
- Pas de branche speciale "si reversal HIGH conviction → bypasser VETO_DUR"
- Pas de seuil OrderFlow reduit quand IRD confirme
- Pas de min_score adaptatif dans build_decision()

### Cause 5: Seuil IRD trop haut pour marche calme
- `reversal_detected` exige score >= 75
- En calme: signaux faibles dans les 4 couches → score IRD typique 40-65
- Le seuil 65 pour le bonus est atteint rarement

---

## 9. Schema du flux actuel (probleme)

```
IRD detect_reversal()
  |
  |  score=72, trend=BULLISH
  |
  v
OrderFlow V6
  |
  |  score=48, bias=BUY
  |
  v
build_decision() ←── AUCUN PARAMETRE IRD
  |
  |  48 < 75 → action=HOLD ←── PREMIER MUR
  |
  v
apply_pma_adjustments()
  |
  |  +10 (IRD aligne) -20 (RANGE) = score 38
  |
  v
VETO_DUR: 38 < 60 ←── DEUXIEME MUR
  |
  v
Decision finale: HOLD (trade perdu)
```

---

## 10. Pistes de correction

### Option A: Adapter build_decision() pour integrer l'IRD
Passer `inst_result` a `build_decision()` et reduire `min_score` quand un reversal
de haute conviction est detecte:
```
Si IRD.conviction == HIGH → min_score passe de 75 a 55
Si IRD.conviction == MODERATE → min_score passe de 75 a 65
```

### Option B: Augmenter significativement le bonus IRD
Passer de +10 a +20/+25 pour les reversals haute conviction:
```
IRD score >= 80 (HIGH) → +25 pts au lieu de +10
IRD score >= 65 (MODERATE) → +15 pts au lieu de +10
```

### Option C: Creer un bypass VETO_DUR pour reversals
Si `reversal_detected=True` ET `conviction=HIGH`, le VETO_DUR (seuil 60)
pourrait etre abaisse a 45-50.

### Option D: Reduire le malus RANGE quand un reversal est detecte
Un reversal valide signifie que le RANGE est en train de casser.
Le MALUS_REGIME de -20 ne devrait pas s'appliquer si un reversal haute conviction
est detecte:
```
Si reversal_detected ET conviction >= MODERATE → MALUS_REGIME = 0
```

### Option E: Combinaison (recommandee)
- build_decision() integre IRD pour ajuster min_score (Option A)
- Bonus IRD augmente pour HIGH conviction (Option B: +25)
- MALUS_REGIME neutralise si reversal (Option D)
- VETO_DUR abaisse si reversal HIGH (Option C: seuil 50 au lieu de 60)

---

## 11. Fichiers concernes pour correction

| Fichier | Modification | Lignes |
|---------|-------------|--------|
| `phase_observer/market_analyzer.py` | build_decision() integrer inst_result | 48-98 |
| `core/decision_pipeline.py` | apply_pma_adjustments() bonus IRD | 2992-3011 |
| `core/decision_pipeline.py` | MALUS_REGIME conditionnel | 2926-2933 |
| `core/decision_pipeline.py` | VETO_DUR seuil adaptatif | 3021-3025 |
| `core/decision_pipeline.py` | decide_scalp_action() passer inst_result a build_decision | ~1180-1220 |

---

## 12. Resume executif

| Element | Etat actuel | Probleme |
|---------|-------------|----------|
| IRD detection | ✅ Fonctionne | Seuil 75 trop haut pour calme |
| IRD → run_bot.py | ✅ Bien passe | - |
| IRD → pipeline | ✅ Bien branche | - |
| IRD → build_decision | ❌ ABSENT | Ignore 100% l'IRD |
| IRD → scoring PMA | ⚠️ Insuffisant | +10 pts seulement |
| VETO_DUR en calme | ❌ Bloque tout | Pas d'exception reversal |
| MALUS_REGIME en calme | ❌ Penalise | -20 meme si reversal valide |
| Decision reversal en calme | ❌ IMPOSSIBLE | Structurellement bloque |
