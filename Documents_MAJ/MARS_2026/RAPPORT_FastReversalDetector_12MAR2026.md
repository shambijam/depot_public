# Rapport d'implémentation — FastReversalDetector
**Date :** 12 Mars 2026
**Branche :** v8_Dynamique_sltp
**Auteur :** Implémentation Claude Sonnet 4.6

---

## Contexte et problème résolu

### Pourquoi l'IRD est muet sur les micro-retournements

L'`InstitutionalReversalDetector` (IRD) existant est conçu pour détecter des **retournements institutionnels structurels** — des changements de régime de marché sur M5. Il était délibérément conservateur :

| Couche IRD | Données minimales | Temps réel min |
|---|---|---|
| Changepoint (Bayésien) | 50 barres M5 | **250 minutes** |
| Divergence Multi-TF | 30 CVD + 30 barres | **150 minutes** |
| Smart Money / Wyckoff | 20 barres M5 | **100 minutes** |
| Confluence niveaux | 50 barres M5 | **250 minutes** |

**Conséquence :** Sur un marché oscillant en range (notamment les mardis), les micro-retournements durent 5-15 minutes (5-15 barres M1). L'IRD ne peut pas les voir. Le bot tradait dans le vide côté timing de micro-tendance.

### Solution : FastReversalDetector complémentaire

Un module **parallèle** à l'IRD, non en remplacement, qui détecte les micro-retournements avec **10 barres M1 minimum** — données déjà en cache.

---

## Architecture de la solution

### Fichier créé

**`phase_observer/fast_reversal_detector.py`** — Classe `FastReversalDetector`

### Fichiers modifiés

| Fichier | Modification |
|---|---|
| `strategy/advanced_scoring.py` | Paramètre `fast_reversal_result` + §14 scoring |
| `core/decision_pipeline.py` | Paramètre `fast_reversal_result` + transmission à `calculate_final_score` |
| `run_bot.py` | Import + instanciation + appel + transmission |

---

## Détail du FastReversalDetector

### 4 signaux implémentés

#### Signal 1 — Divergence prix/volume
**Principe :** Régression linéaire sur 10 barres M1 (pente prix vs pente volume).

- **BEARISH** : `slope_prix > 0` AND `slope_vol < 0` AND `vol_ratio < 75%` de la moyenne → mouvement haussier s'essouffle
- **BULLISH** : `slope_prix < 0` AND `slope_vol < 0` AND `vol_ratio < 75%` → mouvement baissier s'essouffle

**Fix vs spec originale :** Le volume moyen est calculé **dynamiquement** sur 20 barres précédentes (la spec avait `avg_volume = 50` hardcodé — bug critique).

**Filtre bruit :** Ignoré si mouvement prix < 1.5 pips (anti-bruit).

#### Signal 2 — Absorption volumique
**Principe :** Grosse bougie à gros volume + petit corps → une main institutionnelle absorbe les ordres.

Conditions de déclenchement :
- `tick_volume > avg_volume × 1.5` (dynamique sur 20 barres)
- `|corps| / range < 35%` (corps petit = prix stagne)
- Pré-tendance détectée sur les 4 barres précédentes

**Fix vs spec originale :**
- Volume moyen dynamique (était hardcodé `avg_volume = 50`)
- Direction déduite de la **pré-tendance** (était ignorée = absorption sans direction dans la spec)

Interprétation :
- Prix montait + absorption → **BEARISH** (vendeurs absorbent les achats)
- Prix baissait + absorption → **BULLISH** (acheteurs absorbent les ventes)

#### Signal 3 — Épuisement delta
**Principe :** Delta net fort mais faible mouvement de prix → les ordres ne trouvent pas preneur.

Conditions :
- `|delta| >= seuil_dynamique` (p70 de l'historique si colonne 'delta' disponible, sinon 30.0 absolu)
- `mouvement_prix_5barres < 2.0 pips`

**Fix vs spec originale :** Seuil delta dynamique (histogramme) vs seuil absolu arbitraire. Utilise la colonne 'delta' de `rates_df_fresh` si ajoutée par OrderFlow V6.

Interprétation :
- `delta > 0` + faible mouvement → **BEARISH** (acheteurs épuisés)
- `delta < 0` + faible mouvement → **BULLISH** (vendeurs épuisés)

#### Signal 4 — Rejet Ichimoku
**Principe :** Le HIGH ou LOW de la dernière bougie M1 a touché un niveau Tenkan/Kijun et a été rejeté.

Utilise `ichimoku_result` déjà calculé par `IchimokuAnalyzer` — **zéro recalcul**.

Détection de rejet de résistance **(BEARISH)** :
```
0 <= (high - level) / point < seuil_pips
ET bougie rouge (close < open)
ET close < level
```

Détection de rejet de support **(BULLISH)** :
```
0 <= (level - low) / point < seuil_pips
ET bougie verte (close > open)
ET close > level
```

Seuils : Kijun ±2.0 pips | Tenkan ±1.5 pips
Priorité : M5 > M1 (M5 = niveau plus fiable)

**Fix vs spec originale :** Logique HIGH/LOW (la spec comparait le close au niveau sans vérifier que le HIGH avait réellement touché).

### Règle de verdict

```
reversal_detected = (signal_count >= 2)
direction = vote_pondéré(strength) avec seuil ×1.2 (même méthode que _determine_new_trend IRD v8)
confidence = avg_strength × (signal_count / 3.0), clampé 0-1
```

---

## Intégration dans la pipeline de scoring

### §14 dans `calculate_final_score()` (advanced_scoring.py)

**Condition d'application :** `reversal_detected=True` ET `signal_action in ('BUY','SELL')`

| Cas | Impact |
|---|---|
| Direction OPPOSÉE au signal, 2 signaux | `-12 × mtf_malus_factor` |
| Direction OPPOSÉE au signal, 3-4 signaux | `-20 × mtf_malus_factor` |
| Direction IDENTIQUE au signal | `+8` |

### Coordination MTF

Le `mtf_malus_factor` (déjà calculé en §0) est appliqué au malus :
- MTF 3/3 stable (4+ cycles) → `mtf_malus_factor = 0.5` → malus réduit de 50%
- MTF 3/3 frais (1 cycle) → `mtf_malus_factor = 0.9` → malus quasi complet
- MTF non aligné → `mtf_malus_factor = 1.0` → malus plein

**Raisonnement :** Un micro-retournement contre un MTF 3/3 fort et stable = simple pullback dans la tendance = normal, pas dramatique. La réduction du malus reflète cette réalité.

### Impact sur les scores typiques

```
Score avant §14 : 65-80 (trade normal)
+ MALUS_FAST_REVERSAL -12 (2 signaux, MTF non aligné) → 53-68
+ MALUS_FAST_REVERSAL -10 (2 signaux, MTF 3/3 ×0.9) → 55-70
+ MALUS_FAST_REVERSAL -10 (2 signaux, MTF 3/3 stable ×0.5) → -6 → 59-74
+ MALUS_FAST_REVERSAL -20 (3+ signaux) → 45-60 (likely VETO_DUR < 60)
+ BONUS_FAST_REVERSAL +8 → 73-88
```

---

## Flow d'exécution dans run_bot.py

```
Ichimoku analyze() → ichimoku_result
         ↓
FastReversal analyze(df_m1, ichimoku_result, delta_value, point, mtf_direction)
         ↓                          ↑
         |              delta depuis orderflow_result_mini['summary']['delta']
         ↓
fast_reversal_result → decide_scalp_action() → calculate_final_score() §14
```

Le `FastReversalDetector` est instancié **une seule fois** par `scalping_worker` (comme `IchimokuAnalyzer`), à côté de l'injection dans la boucle principale.

### Log en cas de détection

```
[FAST_REVERSAL][USDJPY] ⚡ 2 signaux BEARISH conf=0.48 |
DIV_PV_BEARISH: prix+3.2p vol=62%moy | ABSORPTION_BEARISH: vol=180/92 corps=28%
```

### Log dans le scoring (via §14)

```
[SCORING_DETAIL][USDJPY] ... | MALUS_FAST_REVERSAL: -12 (BEARISH contre BUY, 2 signaux) [DIV_PV_BEARISH: ... | ABSORPTION_BEARISH: ...]
```

---

## Différences par rapport à la spec originale (DEBUG_LOGS.txt)

| Aspect | Spec originale | Implémentation réelle |
|---|---|---|
| `avg_volume = 50` | Hardcodé → bug critique | Calculé dynamiquement (20 barres) |
| Absorption direction | Manquante | Déduite de la pré-tendance |
| Rejet Ichimoku logic | Close vs level (imprécis) | HIGH/LOW vs level (correct) |
| Intégration | `ScalpingStrategy.make_decision()` (inexistant) | `decide_scalp_action()` → `calculate_final_score()` |
| Action | Hard block / `close_position()` (inexistant) | Malus scoring §14 (architecture respectée) |
| MTF | Ignoré (conflit avec MTF ALL-IN) | `mtf_malus_factor` appliqué |
| `price_change` | Non défini | `df_m1` 5 dernières barres en pips |

---

## Séparation des responsabilités (inchangée)

```
advanced_scoring.py   = SCORING UNIQUEMENT (§14 ajouté)
decision_pipeline.py  = VETOS + DECISION (transmetteur uniquement)
fast_reversal.py      = DETECTION/ANALYSE micro-retournement M1
ichimoku_analyzer.py  = fournit les niveaux T/K (réutilisé, pas recalculé)
```

---

## Données requises

| Source | Colonne | Obligatoire |
|---|---|---|
| `rates_df_fresh` (M1 50 barres) | close, open, high, low, tick_volume | Oui |
| `rates_df_fresh` | delta | Non (fallback seuil 30.0) |
| `ichimoku_result` | m5.tenkan, m5.kijun | Non (Signal 4 désactivé si absent) |
| `orderflow_result_mini['summary']` | delta | Non (fallback 0.0) |

**Total appels MT5 supplémentaires : 0**

---

## Limites connues

1. **Signal 3 (delta exhaustion)** : Si `rates_df_fresh` n'a pas de colonne 'delta', utilise un seuil absolu de 30.0 qui peut être inadapté selon l'asset et la session. La dynamique dépend de l'existence de la colonne.

2. **Signal 4 (rejet Ichimoku)** : Ne détecte qu'un rejet sur la **dernière bougie M1** — peut manquer des rejets sur les bougies précédentes si le cycle bot est lent.

3. **Marchés très volatils** : Sur des ATR > 20 pips/min (news), les seuils (2.0 pips DELTA_MOVE_MIN, 1.5/2.0 pips Ichimoku) peuvent fire sur du bruit. En production, surveiller les faux positifs post-news.

---

## Constantes configurables (dans la classe)

```python
MIN_BARS = 10             # Barres M1 minimum
LOOKBACK_DIV = 10         # Fenêtre divergence prix/volume
VOL_LOOKBACK = 20         # Fenêtre volume moyen dynamique
ABSORPTION_VOL_MULT = 1.5 # Seuil volume absorption (×moy)
ABSORPTION_BODY_RATIO = 0.35  # Seuil corps/range absorption
DELTA_MOVE_MIN_PIPS = 2.0     # Mouvement min delta "efficace"
TENKAN_THRESHOLD_PIPS = 1.5   # Distance rejet Tenkan
KIJUN_THRESHOLD_PIPS = 2.0    # Distance rejet Kijun
```
