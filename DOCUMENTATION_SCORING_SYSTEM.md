# SYSTEME DE SCORING UNIFIE - SNIPER_X

**Version**: 2.0 (24 Janvier 2026)
**Fichier principal**: `strategy/advanced_scoring.py`

---

## 1. VUE D'ENSEMBLE

Le systeme de scoring unifie analyse le marche selon **5 dimensions** ponderees pour produire un score final de 0 a 100 points.

```
┌─────────────────────────────────────────────────────────────────┐
│                    SCORE FINAL (0-100)                          │
├─────────────────────────────────────────────────────────────────┤
│  OrderFlow (35%)  │  Institutional (25%)  │  Context (20%)      │
│  Technical (15%)  │  Risk (5%)                                  │
└─────────────────────────────────────────────────────────────────┘
```

---

## 2. LES 5 COMPOSANTS DU SCORING

### 2.1 ORDERFLOW (35% du score final)

**Objectif**: Mesurer la pression acheteur/vendeur en temps reel

| Sous-composant | Points max | Ce qu'il mesure |
|----------------|------------|-----------------|
| Delta Momentum | 30 pts | Ratio delta/volume (force directionnelle) |
| Volume Confirmation | 20 pts | Volume actuel vs moyenne |
| Imbalance Strength | 10 pts | Desequilibre buy/sell |
| Footprint Bonus | 15 pts | Absorption, clustering si footprint disponible |
| Pattern Bonus | 15 pts | Patterns detectes (5 pts/pattern) |

**Calcul Delta Ratio**:
```
delta_ratio = |delta_combined| / volume_combined

Si footprint disponible:
    delta_combined = (fp_delta * 0.7) + (orderflow_delta * 0.3)
Sinon:
    delta_combined = orderflow_delta
```

**Seuils Delta Score**:
- >= 30% ratio → 30 pts (max)
- 20-30% → 22.5-30 pts
- 10-20% → 15-22.5 pts
- 0-10% → 0-15 pts

**Penalties**:
- Rescue level 1 (soft) → -5 pts
- Rescue level 2+ (hard) → -15 pts
- Volume < 50 sans footprint → -5 pts

---

### 2.2 INSTITUTIONAL (25% du score final)

**Objectif**: Detecter l'activite institutionnelle via 5 analyseurs + microstructure

| Analyseur | Ce qu'il mesure | Score |
|-----------|-----------------|-------|
| **Price Memory** | Niveaux frais vs memoire | 25-75 (ratio fresh/memory) |
| **Market Fatigue** | Epuisement du marche | EXHAUSTED=30, FATIGUED=40, NORMAL=50, ENERGETIC=65 |
| **Market Physics** | Bias physique (inertie, momentum) | BULLISH=75, BEARISH=25, NEUTRAL=50 |
| **Tape Speed** | Vitesse du tape (ticks/seconde) | ratio>=2.0=70, >=1.5=60, >=0.8=50, <0.8=35 |
| **Pressure Ratio** | Pression buy/sell normalisee | 50 + (pressure_norm * 50) |
| **Microstructure** | Analyse ticks temps reel | (tape_speed / 5.0) * 100 |

**Score final**: Moyenne des analyseurs actifs

---

### 2.3 CONTEXT (20% du score final)

**Objectif**: Verifier l'alignement avec le contexte de marche

| Element | Poids | Ce qu'il mesure |
|---------|-------|-----------------|
| Confidence | 50% | Score de confiance des signaux (0-1) |
| Alignment | 50% | Alignement action/phase |

**Calcul Alignment**:
```
Si action=BUY et phase contient (bull, up, accum, trend) → align=1.0
Si action=SELL et phase contient (bear, down, distrib) → align=1.0
Si action=SELL et phase contient (trend) → align=0.8
Sinon → align=0.5
```

**Score**: `(0.5 * confidence + 0.5 * alignment) * 100`

---

### 2.4 TECHNICAL (15% du score final)

**Objectif**: Evaluer la qualite technique du setup

| Element | Ce qu'il mesure |
|---------|-----------------|
| Technical Score | Score du candidat (0-1) * 100 |
| Pattern Bonus | +5 pts par pattern detecte (max +20) |

**Score**: `technical_score + pattern_bonus` (0-100)

---

### 2.5 RISK (5% du score final)

**Objectif**: Evaluer les conditions de risque

| Spread (pips) | Score |
|---------------|-------|
| <= 5 | 100 |
| 5-10 | 80 |
| 10-15 | 60 |
| > 15 | 30 |

---

## 3. CALCUL DU SCORE FINAL

```python
final_score = (
    0.35 * orderflow_score +
    0.25 * institutional_score +
    0.20 * context_score +
    0.15 * technical_score +
    0.05 * risk_score
)
```

### Seuils de Status

| Score | Status | Signification |
|-------|--------|---------------|
| >= 65 | VALID | Signal exploitable |
| 50-64 | MARGINAL | Signal faible, prudence |
| < 50 | SUSPECT | Ne pas trader |
| rescue_level >= 2 | SUSPECT | Donnees degradees |

### Seuils de Confidence

| Score | Confidence | Action |
|-------|------------|--------|
| >= 75 | STRONG | Trade avec conviction |
| 65-74 | GOOD | Trade normal |
| 55-64 | WEAK | Trade prudent |
| < 55 | NONE | HOLD (pas de trade) |

---

## 4. SYSTEME DE VETO (Timing Gatekeeper)

### 4.1 Principe

Le Timing Gatekeeper produit un **verdict binaire** (PASS/VETO) base sur un **score de veto** (0-100).

```
veto_score >= 80 → VETO (trade bloque)
veto_score < 80 → PASS (trade autorise)
```

### 4.2 Conditions de Veto

| Condition | Penalite | Description |
|-----------|----------|-------------|
| **Heure non autorisee** | +50 | Trading hors heures whitelist |
| **Coverage insuffisante** | +70 | Moins de X secondes de donnees ticks |
| **Tick rate faible** | +30 a +50 | Moins de X ticks/seconde |
| **Session asiatique + tick faible** | +40 | Session Asie + tick_rate < 8 |
| **Tick rate anormal** | +100 | Tick rate > max (probleme feed) |
| **Liquidite faible** | +45 | liquidity_score < 0.30 |
| **Session off-peak** | +30 | Heures creuses |
| **Fin de bougie** | +100 | Trade dans les 10 dernieres secondes de bougie |

### 4.3 Override du Veto

Un signal OrderFlow exceptionnellement fort peut passer outre un veto modere:

| OrderFlow Score | Peut override si veto_score < |
|-----------------|-------------------------------|
| >= 90 | 70 |
| >= 85 | 60 |
| < 85 | Aucun override possible |

```
Exemple:
- OrderFlow = 92/100, Veto = 65 → OVERRIDE (trade autorise)
- OrderFlow = 80/100, Veto = 50 → BLOQUE (pas d'override)
```

---

## 5. FLUX COMPLET DE DECISION

```
┌──────────────────────────────────────────────────────────────────┐
│                         ENTREE                                    │
│  Ticks, Candles M1, Footprint, Signaux, Patterns, Meta           │
└────────────────────────────┬─────────────────────────────────────┘
                             │
                             ▼
┌──────────────────────────────────────────────────────────────────┐
│                  TIMING GATEKEEPER                                │
│  Analyse: Session, Tick rate, Liquidite, Coverage                │
│  Output: PASS/VETO + veto_score (0-100)                          │
└────────────────────────────┬─────────────────────────────────────┘
                             │
              ┌──────────────┴──────────────┐
              │                             │
              ▼                             ▼
        VETO (score >= 80)           PASS (score < 80)
              │                             │
              │                             ▼
              │         ┌──────────────────────────────────────────┐
              │         │          SCORING UNIFIE                   │
              │         │  1. OrderFlow (35%)                       │
              │         │  2. Institutional (25%)                   │
              │         │  3. Context (20%)                         │
              │         │  4. Technical (15%)                       │
              │         │  5. Risk (5%)                             │
              │         │  = final_score (0-100)                    │
              │         └────────────────────┬─────────────────────┘
              │                              │
              │         ┌────────────────────┴────────────────────┐
              │         │                                         │
              │         ▼                                         ▼
              │   Score >= 65 (VALID)                      Score < 65
              │         │                                         │
              │         ▼                                         ▼
              │   Confidence check                            HOLD
              │   STRONG/GOOD/WEAK?                              │
              │         │                                         │
              ▼         ▼                                         │
┌─────────────────────────────────────────────────────────────────┐
│                      DECISION FINALE                             │
├─────────────────────────────────────────────────────────────────┤
│  HOLD  │  Veto non override OU Score < 65 OU Confidence = NONE  │
│  BUY   │  Score >= 65 + Confidence >= WEAK + Action = BUY       │
│  SELL  │  Score >= 65 + Confidence >= WEAK + Action = SELL      │
└─────────────────────────────────────────────────────────────────┘
```

---

## 6. CONDITIONS POUR QU'UN TRADE SOIT PRIS

### Checklist obligatoire:

1. **Timing Gatekeeper = PASS** (veto_score < 80)
   - OU OrderFlow >= 85 avec veto_score < 60 (override)
   - OU OrderFlow >= 90 avec veto_score < 70 (override)

2. **Score final >= 65** (Status = VALID)

3. **Confidence >= WEAK** (Score >= 55)

4. **Action definie** (BUY ou SELL, pas HOLD)

5. **Pas de rescue_level >= 2** (donnees fiables)

### Resume en une phrase:

> Un trade est pris si le Timing Gatekeeper passe (ou est override par un signal fort), ET le score unifie est >= 65 avec une confidence au moins WEAK.

---

## 7. ARCHITECTURE TECHNIQUE

### Fichiers impliques:

```
strategy/
├── advanced_scoring.py          ← Scoring unifie (fonction centrale)
│   ├── calculate_unified_score()    ← Point d'entree unique
│   ├── calculate_score_integrated() ← Wrapper legacy (orderflow_v6)
│   └── SimpleAdvancedScorer         ← Classe wrapper (run_bot)
│
├── scalping.py                  ← Strategie scalping
│   └── Appelle calculate_unified_score()
│
phase_observer/
├── timing_analyzer.py           ← Timing Gatekeeper (PASS/VETO)
│   └── evaluate_trading_conditions()
│
├── detect_orderflow_v6/
│   └── orderflow_v6.py          ← Detection OrderFlow V6
│       └── Appelle calculate_score_integrated()
│
run_bot.py                       ← Orchestrateur principal
    └── Appelle SimpleAdvancedScorer.calculate_composite_score()
```

### Flux d'appels:

```
run_bot.py
    │
    ├─→ evaluate_trading_conditions() → PASS/VETO
    │
    ├─→ detect_orderflow_v6() → OrderFlow score
    │       └─→ calculate_score_integrated()
    │               └─→ calculate_unified_score()
    │
    └─→ SimpleAdvancedScorer.calculate_composite_score()
            └─→ calculate_unified_score()
```

---

## 8. CONFIGURATION

Les poids peuvent etre personnalises via le parametre `weights`:

```python
custom_weights = {
    'orderflow': 0.40,      # Plus de poids sur OrderFlow
    'institutional': 0.20,
    'context': 0.20,
    'technical': 0.15,
    'risk': 0.05
}

result = calculate_unified_score(
    metrics=...,
    weights=custom_weights
)
```

Les poids sont automatiquement normalises pour sommer a 1.0.

---

## 9. EXEMPLES

### Exemple 1: Trade VALID

```
OrderFlow: 75/100 (delta fort, volume confirme)
Institutional: 65/100 (ENERGETIC, physics BULLISH)
Context: 80/100 (phase bullish, action BUY aligne)
Technical: 70/100 (setup solide)
Risk: 100/100 (spread 3 pips)

Final = 0.35*75 + 0.25*65 + 0.20*80 + 0.15*70 + 0.05*100
      = 26.25 + 16.25 + 16 + 10.5 + 5
      = 74/100

Status: VALID
Confidence: GOOD
Decision: BUY
```

### Exemple 2: Trade VETO

```
Timing Gatekeeper:
- Tick rate = 2.5/s (< 5 minimum) → +40 veto
- Session off-peak → +30 veto
- Liquidite = 0.25 (< 0.30) → +45 veto
Total veto_score = 115 → cap a 100 → VETO

OrderFlow = 72/100 (< 85, pas d'override possible)

Decision: HOLD (timing veto, signal insuffisant pour override)
```

### Exemple 3: Trade OVERRIDE

```
Timing Gatekeeper:
- Session off-peak → +30 veto
- Tick rate modere → +25 veto
Total veto_score = 55 → PASS (mais modere)

OrderFlow = 88/100 (>= 85, peut override veto < 60)

Final Score = 72/100
Status: VALID
Confidence: GOOD

Decision: BUY (veto override par signal fort)
```

---

*Document genere le 24 Janvier 2026*
