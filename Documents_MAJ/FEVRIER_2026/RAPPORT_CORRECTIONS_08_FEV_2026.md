# RAPPORT D'ANALYSE ET CORRECTIONS - 08 FEVRIER 2026

**Commit**: `9e23fbe` - `Reglages_ajustements_reversal_memory_perte_1`
**Branche**: `dev`
**Fichiers modifiés**: 5 (512 insertions, 205 suppressions)

---

## CONTEXTE - Probleme signale

Le bot prenait des **BUY en pleine foret de bougies rouges** malgre la correction du veto reversal du 5 fevrier (`4faceaf`). L'Institutional Reversal Detector semblait completement muet face aux renversements, causant des trades perdants repetitifs.

**Demande**: Analyse complete du projet pour identifier pourquoi les protections ne fonctionnaient pas et pourquoi l'IRD etait inefficace.

---

## DIAGNOSTIC - 6 Problemes critiques identifies

### Probleme 1 (CRITIQUE) - La Branche 3 est une passoire

La correction du 5 fevrier n'avait ete ajoutee **QUE dans la Branche 2** (OVERRIDE, score >= 85). La **Branche 3** (PASS_NORMAL), par laquelle passe la **majorite des trades**, n'avait :
- AUCUN malus (contre-tendance, range, fatigue)
- AUCUN veto reversal
- AUCUN PMA_VETO_DUR
- Les valeurs bonus/malus codees en dur a `0.0`

**Impact**: La porte d'entree principale (Branche 3, cas courant) etait grande ouverte tandis que la porte arriere (Branche 2, cas rare) etait blindee.

### Probleme 2 (CRITIQUE) - L'IRD est sourd et muet

L'Institutional Reversal Detector ne pouvait **physiquement pas** detecter un renversement :

| Couche | Poids | Statut | Raison |
|--------|-------|--------|--------|
| Changepoint | 35% | MORT | Besoin 50 bougies M5, recevait 20 |
| Divergence | 30% | MORT | CVD et delta = listes vides (TODO dans le code) |
| Fatigue | VETO | MORT | Delta vide |
| Smart Money | 25% | PARTIEL | Stub retournant toujours `validated=True` |
| ML Patterns | 10% | PARTIEL | Fonctionnel mais insuffisant |

**Score max atteignable**: ~25-30/100. Seuil pour `reversal_detected=True` : 75.
**La detection de reversal etait mathematiquement impossible.**

### Probleme 3 (MAJEUR) - Le M1 dicte le verdict MTF

Le Price Memory Analyzer avait un systeme de "dictature du M1" :
- Analyse basee sur **1 seule bougie** par timeframe
- M1 avait un **droit de veto absolu** : si M15+M5 disent BEARISH mais M1 est BULLISH → verdict NEUTRAL

**Consequence**: Un simple rebond de 30 secondes en M1 annulait un consensus baissier de 15 minutes. Le bot voyait des "opportunites BUY" dans des tendances clairement baissieres.

### Probleme 4 - Corrections au mauvais endroit

Les protections de la correction du 5 fevrier etaient dans la Branche 2 uniquement. La majorite des trades passant par la Branche 3, elles etaient inefficaces.

### Probleme 5 - `build_decision()` trop simpliste

La fonction `build_decision()` utilisee par la Branche 3 retournait directement le bias OrderFlow sans aucun filtrage supplementaire.

### Probleme 6 - Smart Money Validation factice

`_validate_smart_money()` dans l'IRD etait un stub :
```python
# AVANT
return {
    "validated": True,        # Toujours True !
    "confidence": 0.85,       # Valeur fixe
    "institutional_bias": "ALIGNED",
    "validation_methods": ["VOLUME_ANALYSIS", "ORDER_FLOW", "CVD_ANALYSIS"]
}
```

---

## SCHEMA DU PROBLEME

```
Marche en chute libre (-45 pips)
    |
    +-- OrderFlow: "Petit rebond sur les ticks!" → BUY, score 63
    +-- IRD: "..." (muet, pas assez de donnees)
    +-- MTF: "M15=BEAR, M5=BEAR, M1=une bougie verte" → NEUTRAL (pas BEARISH!)
    +-- Timing: OK
    |
    +--→ Branche 3 : score 63 >= 60 → BUY execute
         Aucun malus, aucun veto, rien.
         → PERTE
```

---

## CORRECTIONS IMPLEMENTEES

### Correction 1 (CRITIQUE) - Branche 3 protegee

**Fichier**: `run_bot.py` (+210 lignes)

Creation de `apply_pma_adjustments()` (~210 lignes), fonction reutilisable appelee dans les **DEUX** branches :
- **Branche 2** (ligne ~4534) : Remplace le code inline
- **Branche 3** (ligne ~4780) : **AJOUT** complet des protections

**Malus implementes** :
| Malus | Points | Condition |
|-------|--------|-----------|
| MICRO_RESISTANCE | -30 | BUY pres d'une resistance STRONG/MODERATE |
| CONTRE_TENDANCE_MTF | -35 | Signal oppose au MTF (2+ TF alignes) |
| REGIME_RANGE | -20 | Regime range/accumulation/distribution |
| FATIGUE | -50 | Circuit breaker IRD (marche epuise) |

**Bonus implementes** :
| Bonus | Points | Condition |
|-------|--------|-----------|
| MTF_3/3 | +15 | Alignement parfait 3 TF + signal |
| MTF_2/3 | +10 | Alignement 2 TF + signal |
| FRESH_LEVEL | +10 | Niveau frais a moins de 2 pips |
| TREND_CONSISTENCY | +5 | Clarity >= 0.7 et Strength >= 0.6 |
| INSTITUTIONAL | +10 | Score IRD >= 65 aligne avec signal |

**Vetos** :
- **VETO_DUR** : Score ajuste < 60 → trade rejete
- **VETO_REVERSAL** : Reversal detecte en direction opposee → trade bloque
- **VETO_FATIGUE** : Circuit breaker actif → -50 pts

### Correction 2 (CRITIQUE) - IRD alimente correctement

**Fichier**: `run_bot.py`

- M5 bars : `count=20` → `count=60` (Changepoint peut fonctionner avec 50+ bougies)
- CVD : Extraction depuis `rates_df_fresh['cvd']` (colonnes ajoutees par OrderFlow V6)
- Delta : Extraction depuis `rates_df_fresh['delta']` avec fallback `tick_volume` signe

**Resultat** : Les 6 couches de l'IRD peuvent maintenant fonctionner :
- Changepoint : 60 bougies M5 > seuil de 50 → ACTIF
- Divergence : CVD reel au lieu de liste vide → ACTIF
- Fatigue : Delta reel → ACTIF

### Correction 3 (MAJEUR) - Verdict MTF pondere

**Fichier**: `phase_observer/price_memory_analyzer.py` (+43 lignes)

Remplacement de la "dictature du M1" par un **systeme pondere** :

| Timeframe | Poids |
|-----------|-------|
| M15 | 50% |
| M5 | 30% |
| M1 | 20% |

**Seuil de decision** : Score pondere >= 0.30 pour direction claire.

**Exemple** : M15=BEARISH, M5=BEARISH, M1=BULLISH
- Score = (-0.50) + (-0.30) + (+0.20) = **-0.60** → BEARISH
- Avant : M1 BULLISH → veto → NEUTRAL (le bot achetait)

**Lookback** : Passe de 1 a 3 bougies pour lisser le bruit M1.

> **NOTE 09 FEV 2026** : Le lookback a 3 bougies a ete remis a 1 bougie car trop restrictif pour le scalping (zero trades en production). Le systeme de ponderation M15>M5>M1 reste en place.

### Correction 4 (MODERE) - Smart Money Validation reelle

**Fichier**: `phase_observer/institutional_reversal_detector.py` (+85 lignes)

Remplacement du stub par 3 criteres reels (2/3 requis pour validation) :

1. **Volume Profile** : Volume recent > 120% de la moyenne → CONFIRMED
2. **Price Action** : Detection de swing points (HH/HL ou LH/LL) dans les 10 dernieres bougies → CONFIRMED
3. **CVD Flow Divergence** : Changement de direction du CVD ou acceleration > 1.5x → CONFIRMED

```python
# APRES
validated = criteria_met >= 2  # Au moins 2/3 criteres
confidence = criteria_met / 3.0  # 0.33, 0.66, ou 1.0
```

### Correction 5 - Ajustements configs SL/Loss Guard

**Fichiers**: `config/assets_config/USDCHF.json`, `USDJPY.json`

| Parametre | Asset | Avant | Apres |
|-----------|-------|-------|-------|
| SL pips | USDCHF | 18.0 | 10.0 |
| max_loss_pips | USDCHF | 12.0 | 8.0 |
| SL pips | USDJPY | 20.0 | 10.0 |
| max_loss_pips | USDJPY | 15.0 | 10.0 |

---

## FICHIERS MODIFIES - RESUME

| Fichier | Modifications | Lignes |
|---------|---------------|--------|
| `run_bot.py` | `apply_pma_adjustments()` + appels B2/B3 + donnees IRD (M5=60, CVD, delta) | +346 |
| `phase_observer/price_memory_analyzer.py` | Ponderation MTF (M15=50%, M5=30%, M1=20%) + lookback 3 bougies | +43 |
| `phase_observer/institutional_reversal_detector.py` | `_validate_smart_money()` reelle (3 criteres, 2/3 requis) | +85 |
| `config/assets_config/USDCHF.json` | SL 18→10, max_loss 12→8 | +2 |
| `config/assets_config/USDJPY.json` | SL 20→10, max_loss 15→10 | +2 |

---

## SCHEMA APRES CORRECTION

```
Marche en chute libre (-45 pips)
    |
    +-- OrderFlow: "Petit rebond!" → BUY, score 63
    +-- IRD: "REVERSAL BEARISH detecte (score 78)" → VETO_REVERSAL
    +-- MTF: "M15=BEAR(50%), M5=BEAR(30%), M1=BULL(20%)" → Score -0.60 → BEARISH
    +-- Timing: OK
    |
    +--→ Branche 3 :
         Score brut: 63
         MALUS_CONTRE_MTF: -35 (MTF BEARISH vs BUY)
         Score ajuste: 28
         VETO_DUR: Score 28 < 60 → TRADE REJETE
         VETO_REVERSAL: Reversal BEARISH → TRADE BLOQUE
         → PAS DE TRADE (2 protections declenchees)
```

---

## POINTS D'ATTENTION

1. **Lookback MTF** : Remis a 1 bougie le 09/02 (3 bougies = trop restrictif, zero trades). A surveiller en production.
2. **IRD donnees** : Verifier dans les logs que Changepoint et Divergence produisent maintenant des scores non-nuls.
3. **M5 60 bougies** : Augmentation de la charge MT5. Surveiller la latence du cycle.
4. **Seuil VETO_DUR a 60** : Potentiellement ajustable si trop de trades bloques.
