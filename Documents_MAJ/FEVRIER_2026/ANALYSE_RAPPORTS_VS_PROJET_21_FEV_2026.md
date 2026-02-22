# ANALYSE COMPARATIVE — RAPPORTS DEBUG_LOGS.txt vs PROJET SNIPER_X
**Date :** 21 Février 2026
**Auteur :** Claude (analyse automatique)
**Fichier source :** `DEBUG_LOGS.txt`

---

## 1. SYNTHÈSE DES DEUX RAPPORTS

### Rapport 1 — "Le Mariage Parfait : PMA + OrderFlow V6"
Rapport pédagogique qui pose les fondements de la méthode en 3 scénarios :

| Scénario | Fréquence | Configuration |
|---|---|---|
| **Trend Following** | 70% | MTF 2/3 ou 3/3 + régime `trending` + score ≥ 70 |
| **Divergence CVD** | 20% | MTF 2/3 + régime `consolidation`/`breakout_potential` + divergence confirmée |
| **Rebond support/résistance** | 10% | MTF **NEUTRAL** + régime `range` + HVN/LVN + delta inversé |

**Règle d'or formulée :** `PMA décide la DIRECTION — OrderFlow décide le TIMING`
**Bonus PMA :** +30 (3/3), +15 (2/3), +5 (1/3) — 2/3 acceptable si M15 est dans la majorité.
**Cas interdits :** MTF NEUTRAL + score élevé → pas de trade (CAS N°3).

---

### Rapport 2 — "La Synergie Order Flow V6 & Price Memory"
Rapport stratégique avec workflow en 4 étapes et matrice de décision :

**Workflow :**
1. Verdict Stratégique (PMA) → obtenir MTF_Trend_Verdict
2. Cartographie des niveaux → micro-résistances ou fresh_levels
3. Validation de la pression (OrderFlow) → score, régime, **cvd_slope**
4. Signal de déclenchement → combinaison des deux

**Score de confiance unifié** (formule proposée) :
```
Score_Confiance = Bonus_PMA + Score_OrderFlow
  > 90  = "Setup A" : entrée agressive, SL plus large, TP plus lointain
  < 70  = réduire taille ou ne pas trader
```

**La cvd_slope** est identifiée comme "l'accélérateur" :
- BUY : pente CVD positive et croissante obligatoire
- SELL : pente CVD négative et croissante obligatoire

---

## 2. COMPARAISON AVEC LE PROJET ACTUEL

### 2.1 — Points d'ALIGNEMENT PARFAIT ✅

| Doctrine du rapport | Implémentation projet | Fichier |
|---|---|---|
| PMA = Direction, OF = Timing | MTF verdict imposé AVANT orderflow, scalping.py lit `asset_signals["mtf_direction"]` | `run_bot.py:2104` / `scalping.py` |
| M15 (50%), M5 (30%), M1 (20%) | `weight_m15=0.50, weight_m5=0.30, weight_m1=0.20` | `price_memory_analyzer.py:1129-1131` |
| Bonus PMA (+30 / +20 / +10) | `BONUS_MTF_4/4: +30`, `BONUS_MTF_3/4: +20` (seuil 3+) | `advanced_scoring.py:261-268` |
| Micro-résistance malus | `MALUS_MICRO_RES: -30` si STRONG/MODERATE à < 1 pip | `advanced_scoring.py:307-316` |
| Fresh levels bonus | `BONUS_FRESH_LEVEL: +10` si < 2 pips | `advanced_scoring.py:283-286` |
| Trend consistency bonus | `BONUS_TREND_CONSISTENCY: +5` | `advanced_scoring.py:293-302` |
| Régime `range`/`accumulation` = malus | `MALUS_REGIME: -20` | `advanced_scoring.py:321-337` |
| MTF NEUTRAL = pas de trade | `alignment_count < 3 → mtf_direction = "NEUTRAL"` → HOLD | `run_bot.py:2103-2107` |
| CVD slope pour classif. régime | `cvd_slope` utilisé dans `orderflow_v6.py` pour `trending`/`range`/`consolidation` | `orderflow_v6.py:35-78` |
| Consensus des analyseurs | `consensus ALIGNED/BLOCKED/SPLIT` (+15/-15) | `advanced_scoring.py:344-372` |
| IRD reversal exception régime | IRD MODERATE/HIGH neutralise MALUS_REGIME | `advanced_scoring.py:326-341` |

**Le socle doctrinal des deux rapports est entièrement respecté.**

---

### 2.2 — Points de DIVERGENCE : le projet est PLUS STRICT que les rapports ⚠️

#### DIVERGENCE 1 — Seuil d'alignement MTF (IMPORTANT)

| | Rapports | Projet actuel |
|---|---|---|
| **Minimum requis** | **2/3** (M15 doit être dans la majorité) | **3/3** (tous les 3 TF alignés) |
| **Cas 2/3** | ✅ Trade normal (taille standard) | ❌ HOLD (mtf_direction → NEUTRAL) |
| **Flexibilité** | Oui, avec conditions | Non |

**Analyse :** Le projet impose un seuil absolument maximal (3/3 = "MTF ALL-IN" du 20 FEV 2026). Les rapports recommandent 2/3 comme acceptable si M15 est dans la majorité. Cette sévérité supplémentaire réduit mécaniquement le nombre de trades éligibles, peut-être de 30 à 40%.

**Question à se poser :** Les trades 2/3 avec M15 aligné ont-ils des performances dégradées ou comparables ? Si oui, on laisse de la profitabilité sur la table.

---

#### DIVERGENCE 2 — Scénario 3 (Rebond NEUTRAL + Range) complètement absent

| | Rapports | Projet actuel |
|---|---|---|
| **MTF NEUTRAL + range** | ✅ Trade valide (rebond HVN/LVN) | ❌ HOLD absolu |
| **Fréquence estimée** | 10% des trades | 0% |

**Analyse :** Le rapport 1 décrit explicitement un 3ème scénario : MTF NEUTRAL + régime `range` + HVN proche + delta inversé = ACHAT LIMITE sur support. Le projet bloque **toute** action sur NEUTRAL. C'est un choix architectural délibéré (MTF ALL-IN), mais il supprime un scénario de trading légitimement documenté dans les deux rapports.

---

#### DIVERGENCE 3 — Sizing asymétrique absent

| | Rapports | Projet actuel |
|---|---|---|
| **3/3 + trending** | Taille maximale (full position) | `risk_per_trade_percent` fixe |
| **2/3 + divergence** | Taille réduite (half position) | N/A (2/3 → HOLD) |
| **Score < 70** | Réduire ou ne pas trader | Seuil de score uniquement |

**Analyse :** Les rapports recommandent une **gestion de position asymétrique** : plus le Setup est de qualité (3/3, trending, score > 90), plus la taille augmente. Le projet utilise un `risk_per_trade_percent` fixe. Il n'y a pas de multiplication de lot en fonction de la conviction du setup.

---

### 2.3 — GAPS : fonctionnalités présentes dans les rapports, absentes du projet ❌

#### GAP 1 — CVD Slope comme signal direct (non exposé en scoring)

**Rapport 2 :** "La pente CVD (cvd_slope) est l'accélérateur. Pour un achat, vous devez voir une pente positive qui s'accentue."

**Projet :** Le `cvd_slope` est utilisé **implicitement** dans `orderflow_v6.py` pour classer le régime (`trending` si `|cvd_slope| > 0.3`), mais il **n'est pas exposé en tant que bonus/malus direct** dans `advanced_scoring.py`. Autrement dit :
- Un CVD slope fortement positif avec signal BUY n'apporte **aucun bonus supplémentaire**
- Un CVD slope négatif avec signal BUY ne déclenche **aucun malus**

Le scoring capte la direction du CVD indirectement (via le régime et le delta), mais pas l'**intensité et l'alignement** de la pente.

**Amélioration potentielle :** Exposer `cvd_slope` dans le résultat `orderflow_result_mini` et ajouter dans `advanced_scoring.py` un bonus si la pente s'accentue dans le sens du trade.

---

#### GAP 2 — Divergence CVD/Prix non scorée directement

**Rapport 1 (Scénario 2) :** "OrderFlow détecte une divergence CVD → signe d'épuisement → meilleure entrée du scénario divergence (20% des trades, les plus rentables)"

**Projet :** L'IRD (`institutional_reversal_detector.py`) détecte des reversals institutionnels sur 6 couches (Changepoint, CVD Divergence, RSI, etc.). La divergence CVD **est bien détectée** dans l'IRD (layer `CVD_Divergence`), mais :
- Elle est fondue dans un score global IRD (seuil 65)
- Elle n'a pas de **bonus spécifique** dans le scoring pour les cas où CVD diverge du prix
- Le scénario divergence des rapports est moins exploitable car le projet requiert 3/3 MTF (alors que le rapport 1 dit que 2/3 suffit pour ce scénario)

---

#### GAP 3 — HVN/LVN pour rebonds de range

**Rapport 1 :** "Le Volume Profile (OrderFlow) montre un HVN (High Volume Node) à 1.0500 → fort support → delta positif au contact → ACHAT LIMITE."

**Projet :** Le Volume Profile est calculé dans `orderflow_v6.py` (nodes, zones de liquidité), mais :
- `advanced_scoring.py` utilise les **fresh_levels** (niveaux non encore testés) mais pas les **HVN**
- Les HVN/LVN ne sont pas extraits et exposés comme bonus de scoring
- Sans ce mécanisme, le Scénario 3 (rebond range) ne peut de toute façon pas fonctionner (MTF NEUTRAL bloqué)

---

#### GAP 4 — Score de confiance comme guide de SL/TP

**Rapport 2 :** "Si le Score de Confiance est > 90 → 'Setup A' → SL plus large pour laisser respirer, TP plus lointain."

**Projet :** Le score final (0-100) est utilisé uniquement comme filtre binaire (seuil pour HOLD vs TRADE). Il ne module pas le SL/TP distance. Les setups de très haute conviction (score > 90) ne bénéficient pas d'un TP plus ambitieux.

---

## 3. TABLEAU SYNTHÉTIQUE

| Recommandation des rapports | État dans le projet | Priorité |
|---|---|---|
| PMA = direction, OF = timing | ✅ Implémenté parfaitement | — |
| Poids MTF M15/M5/M1 | ✅ M15=50%, M5=30%, M1=20% | — |
| Micro-résistances malus | ✅ -30 si forte et < 1 pip | — |
| Fresh levels bonus | ✅ +10 si < 2 pips | — |
| Régime `range` malus | ✅ -20 | — |
| NEUTRAL MTF = HOLD | ✅ (plus strict : 3/3 obligatoire) | — |
| Consensus analyseurs | ✅ ALIGNED/BLOCKED | — |
| **2/3 MTF acceptable** | ⚠️ Bloqué (projet : 3/3 obligatoire) | MOYEN |
| **Scénario 3 rebond range** | ⚠️ Absent (NEUTRAL bloqué) | FAIBLE |
| **CVD slope comme bonus direct** | ❌ Non exposé en scoring | MOYEN |
| **Divergence CVD scorée spécifiquement** | ❌ Fondue dans IRD | MOYEN |
| **Sizing asymétrique** | ❌ Risk fixe par trade | ÉLEVÉ |
| **SL/TP dynamique selon score** | ❌ Score = filtre binaire seulement | MOYEN |
| **HVN/LVN pour range** | ❌ Absent (lié au Gap Scénario 3) | FAIBLE |

---

## 4. RECOMMANDATIONS CONCRÈTES (par ordre de priorité)

### 🔴 PRIORITÉ HAUTE — Sizing Asymétrique

Le rapport recommande de **charger davantage** sur les meilleurs setups. Le projet a toutes les informations disponibles (`score_final`, `alignment_count`, `consensus`) mais ne les utilise pas pour moduler la taille.

**Implémentation suggérée** (dans `decision_pipeline.py` ou `run_bot.py`) :
```python
# Exemple conceptuel — sizing dynamique
if score_final >= 85 and alignment_count == 3 and consensus == "ALIGNED":
    risk_multiplier = 1.5   # +50% sur "Setup A"
elif score_final >= 70:
    risk_multiplier = 1.0   # taille normale
else:
    risk_multiplier = 0.5   # taille réduite (garde-fou)
```

Attention : doit rester dans les limites de `max_risk_exposure` et `max_positions`.

---

### 🟡 PRIORITÉ MOYENNE — CVD Slope Directional Bonus

Exposer `cvd_slope` depuis `orderflow_result_mini` et l'ajouter dans `advanced_scoring.calculate_final_score()` :

**Logique suggérée :**
```python
# Dans advanced_scoring.py — Section BONUS CVD SLOPE
cvd_slope = orderflow_result_mini.get("cvd_slope", 0.0)
if signal_action == "BUY" and cvd_slope > 0.3:
    bonus_total += 8.0
    adjustments.append(f"BONUS_CVD_SLOPE_BUY: +8 (pente={cvd_slope:.2f})")
elif signal_action == "SELL" and cvd_slope < -0.3:
    bonus_total += 8.0
    adjustments.append(f"BONUS_CVD_SLOPE_SELL: +8 (pente={cvd_slope:.2f})")
elif (signal_action == "BUY" and cvd_slope < -0.2) or (signal_action == "SELL" and cvd_slope > 0.2):
    malus_total += 5.0
    adjustments.append(f"MALUS_CVD_SLOPE_OPPOSE: -5 (pente contre signal)")
```

**Bénéfice :** Capte directement "l'accélérateur" décrit dans le rapport 2, au lieu d'attendre que le régime soit classifié `trending`.

---

### 🟡 PRIORITÉ MOYENNE — Réévaluer le seuil MTF 3/3 obligatoire

Le passage de 2/3 à 3/3 (20 FEV 2026) est une décision de sévérité maximale. Il serait intéressant de mesurer empiriquement :

- **Combien de trades sont bloqués à 2/3 avec M15 aligné ?**
- **Quel est leur taux de succès hypothétique ?**

Si les configurations 2/3 (M15 ✅ + un autre TF) montrent des résultats similaires, une option intermédiaire serait :

```python
# Dans run_bot.py — Alternative moins restrictive
if mtf_verdict.alignment_count == 3:
    mtf_direction = mtf_verdict.direction
elif mtf_verdict.alignment_count == 2 and m15_dir == mtf_verdict.direction:
    # M15 est dans la majorité → direction confirmée mais avec bonus réduit
    mtf_direction = mtf_verdict.direction
    # Forcer un malus dans le scoring pour signaler le setup moins fort
else:
    mtf_direction = "NEUTRAL"
```

---

### 🟡 PRIORITÉ MOYENNE — SL/TP dynamique selon score

Utiliser `score_final` pour ajuster le TP multiplier, comme le rapport 2 le suggère pour les "Setup A" :

```python
# Dans la construction des ordres
if fusion_out.get("score_final", 0) >= 88 and consensus == "ALIGNED":
    tp_multiplier = 1.3  # TP 30% plus lointain sur setup exceptionnel
    sl_multiplier = 1.2  # SL légèrement plus large pour laisser respirer
else:
    tp_multiplier = 1.0
    sl_multiplier = 1.0
```

---

### 🟢 PRIORITÉ FAIBLE — Divergence CVD dédiée (Scénario 2)

L'IRD détecte déjà les divergences CVD dans ses 6 couches. Pour valoriser davantage le Scénario 2 des rapports, il suffirait de :

1. Exposer si la layer `CVD_Divergence` est active dans le résultat IRD
2. Ajouter un bonus spécifique `BONUS_CVD_DIVERGENCE: +12` quand la divergence est confirmée ET dans le sens du MTF

Ceci est déjà partiellement couvert par `BONUS_IRD` (si IRD score ≥ 65), mais le rendre plus explicite améliorerait la tracabilité dans les logs.

---

## 5. POINT D'ATTENTION : MEMORY.md OUTDATED

Le fichier `MEMORY.md` (utilisé comme contexte de session) contient une **erreur factuelle** sur les poids MTF :

```
MEMORY.md (INCORRECT) :
MTF direction via PMA verdict (M30=0.40, M15=0.30, M5=0.20, M1=0.10, seuil=0.20)
"alignment_count == 4" requis pour trade

CODE RÉEL (price_memory_analyzer.py:1082-1131) :
M30 SUPPRIMÉ le 20 FEV 2026
weight_m15=0.50, weight_m5=0.30, weight_m1=0.20
alignment_count max = 3 (pas 4)
Seuil : alignment_count == 3 requis (pas == 4)
```

**Action requise :** Mettre à jour le MEMORY.md pour corriger ces 3 points.

---

## 6. CONCLUSION GÉNÉRALE

Les deux rapports dans `DEBUG_LOGS.txt` sont de haute qualité et décrivent exactement la philosophie architecturale du projet :

> **"PMA décide la direction, OrderFlow décide le timing."**

Cette doctrine est **intégralement implémentée** et même renforcée par rapport aux rapports (3/3 obligatoire au lieu de 2/3).

Les 3 points d'amélioration les plus impactants sont :

1. **Sizing asymétrique** (HAUTE priorité) : tirer parti du score_final et de l'alignment pour charger davantage sur les meilleurs setups — c'est de la profitabilité laissée sur la table.

2. **CVD slope comme bonus direct** (MOYENNE priorité) : l'accélérateur CVD des rapports est partiellement caché dans la classification du régime, mais n'est pas exposé explicitement dans le scoring.

3. **Réévaluation du seuil 3/3** (MOYENNE priorité, données empiriques nécessaires) : le passage à 3/3 (MTF ALL-IN) est défensif mais potentiellement trop restrictif si les setups 2/3 avec M15 aligné ont de bonnes performances.

Le projet est **architecturalement solide et fidèle à la doctrine**. Les améliorations identifiées sont des optimisations de rentabilité, pas des corrections de bugs fondamentaux.

---

*Rapport généré le 21/02/2026 — basé sur l'analyse de `DEBUG_LOGS.txt` et du code source actuel.*
