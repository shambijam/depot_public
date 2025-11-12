# Analyse Fusion Manager - Seuils Trop Permissifs

## 🔴 Problèmes Identifiés

### Observation Utilisateur
1. ✅ Premier burst a maintenant toujours un SL (bug corrigé)
2. ❌ Trailing stop ne s'active jamais (même à 400 pips)
3. ❌ **Qualité trades MÉDIOCRE** :
   - Orderflow V6 ne travaille pas
   - Footprints ne filtrent pas
   - Bot prend BUY au lieu de SELL (trades contrariens)
   - **Cause : Paramètres trop permissifs**

---

## 📊 Analyse des Seuils Actuels

### 1. FOOTPRINT (config_trade_scalping.json lignes 77-85)

| Paramètre | Valeur Actuelle | Problème | Recommandation |
|-----------|-----------------|----------|----------------|
| `m1_min_ticks` | 3 | Trop bas | **8** (détection claire) |
| `m1_min_coverage_s` | 1 | Trop court | **3** (confirmation temporelle) |
| `tickrate_min` | 0.1 | Ridicule | **2.5** (activité réelle) |
| `veto_absorption` | false | ❌ Désactivé | **true** (bloque contre-flow) |
| `warn_absorption` | false | Inutilisé | false (OK) |
| `absorption_penalty` | 0.0 | Aucune pénalité | **0.15** (15% malus) |

**Impact** : Footprint accepte TOUT → Ne filtre RIEN

---

### 2. ORDERFLOW (config_trade_scalping.json lignes 86-89)

| Paramètre | Valeur Actuelle | Problème | Recommandation |
|-----------|-----------------|----------|----------------|
| `delta_abs_min` | 5.0 | Trop bas pour XAUUSD | **15.0** (delta significatif) |

**Impact** : Accepte des orderflows faibles → Trades de mauvaise qualité

---

### 3. TRIGGERS (config_trade_scalping.json lignes 90-95)

| Paramètre | Valeur Actuelle | Problème | Recommandation |
|-----------|-----------------|----------|----------------|
| `require_trigger` | false | ❌ NE REQUIERT PAS | **true** (obligatoire) |
| `min_strength` | 0.0 | Aucune force | **0.50** (trigger solide) |
| `allow_opposite_trigger` | true | ❌ Autorise contrarian | **false** (cohérence) |

**Impact** : Prend des trades SANS triggers ou CONTRE les triggers → Explique BUY au lieu de SELL

---

### 4. FUSION BURST_SCALPING (config_trade_scalping.json lignes 96-114)

| Paramètre | Valeur Actuelle | Problème | Recommandation |
|-----------|-----------------|----------|----------------|
| `allow_degraded_vote` | true | Vote dégradé OK | **false** (vote complet requis) |
| `require_phase_alignment` | false | Phase ignorée | **true** (cohérence phase) |
| `require_footprint` | false | ❌ Footprint optionnel | **true** (obligatoire) |
| `min_score_to_fire` | 0.15 | Très bas (15%) | **0.40** (40% minimum) |
| `degraded_vote_conditions.min_of_delta_abs` | 0.0 | Aucun delta | **80.0** (delta minimal) |
| `degraded_vote_conditions.min_of_score` | 0.0 | Aucun score | **0.55** (55% score OF) |

**Impact** : Fusion fire sans confirmation solide → Trades médiocres

---

### 5. FUSION GLOBALE (config_trade_scalping.json lignes 155-171)

| Paramètre | Valeur Actuelle | Problème | Recommandation |
|-----------|-----------------|----------|----------------|
| `seuils_entree.min_confidence` | 0.25 | Très bas (25%) | **0.45** (45% minimum) |
| `seuils_entree.min_orderflow_score` | 0.23 | Très bas (23%) | **0.55** (55% OF requis) |
| `seuils_entree.max_spread_pts` | 999 | Aucune limite | **80** (XAUUSD 8 pips) |
| `scoring_thresholds.direct` | 0.15 | Trop bas | **0.40** (signal direct fort) |
| `scoring_thresholds.conditional` | 0.05 | Ridicule (5%) | **0.25** (signal conditionnel minimum) |

**Impact** : Accepte des signaux faibles → Volume de trades élevé mais qualité médiocre

---

### 6. RÈGLES MÉTIER (config_trade_scalping.json lignes 210-214)

| Paramètre | Valeur Actuelle | Problème | Recommandation |
|-----------|-----------------|----------|----------------|
| `veto_absorption` | false | ❌ Autorise contre-absorption | **true** (veto strict) |
| `never_against_strong_orderflow` | false | ❌ Autorise contre-OF | **true** (jamais contre OF fort) |
| `weak_footprint_score_th` | 0.00 | Aucun seuil | **0.45** (footprint minimum) |

**Impact** : Prend des trades CONTRE l'orderflow fort → BUY quand il faut SELL

---

### 7. PONDÉRATIONS (config_trade_scalping.json lignes 203-208)

| Paramètre | Valeur Actuelle | Recommandation | Raison |
|-----------|-----------------|----------------|--------|
| `trigger_weight` | 0.40 | **0.35** | Reduce légèrement |
| `orderflow_weight` | 0.40 | **0.45** | Augmente (priorité OF) |
| `footprint_weight` | 0.20 | **0.20** | OK |

**Impact** : Rééquilibre vers orderflow (plus fiable que triggers)

---

### 8. ORDERFLOW V6 XAUUSD (XAUUSD.json lignes 136-142)

| Paramètre | Valeur Actuelle | État | Recommandation |
|-----------|-----------------|------|----------------|
| `min_score` | 0.60 | ✅ OK | 0.60 (garder) |
| `require_ncp_strong` | false | ❌ Désactivé | **true** (NCP requis) |
| `min_delta_abs` | 120.0 | ✅ OK | 120.0 (garder) |
| `min_imbalance` | 0.52 | ✅ OK | 0.55 (durcir légèrement) |
| `min_tickrate` | 2.5 | Correct | 3.5 (durcir) |

**Note** : Orderflow V6 gate est correct MAIS ne s'applique pas si fusion_manager laisse passer sans lui

---

## 🎯 Stratégie de Correction

### Priorité 1 : BLOCAGE DES TRADES CONTRARIENS
```json
"triggers": {
  "require_trigger": true,        // ✅ OBLIGATOIRE
  "min_strength": 0.50,            // ✅ Trigger solide requis
  "allow_opposite_trigger": false  // ✅ BLOQUER contrariens
}

"regles_metier": {
  "veto_absorption": true,                  // ✅ Veto strict
  "never_against_strong_orderflow": true,   // ✅ Jamais contre OF fort
  "weak_footprint_score_th": 0.45           // ✅ Footprint minimum
}
```

### Priorité 2 : ACTIVATION ORDERFLOW V6
```json
"fusion": {
  "require_footprint": true,         // ✅ Footprint obligatoire
  "require_phase_alignment": true,   // ✅ Phase cohérente
  "min_score_to_fire": 0.40,         // ✅ 40% minimum
  "allow_degraded_vote": false,      // ✅ Vote complet requis
  "degraded_vote_conditions": {
    "min_of_delta_abs": 80.0,        // ✅ Delta minimal si dégradé
    "min_of_score": 0.55             // ✅ Score OF minimal si dégradé
  }
}

"seuils_entree": {
  "min_confidence": 0.45,            // ✅ 45% confiance
  "min_orderflow_score": 0.55,       // ✅ 55% OF score
  "max_spread_pts": 80               // ✅ 8 pips max XAUUSD
}
```

### Priorité 3 : DURCIR FOOTPRINTS
```json
"footprint": {
  "m1_min_ticks": 8,               // ✅ Signal clair
  "m1_min_coverage_s": 3,          // ✅ Confirmation temporelle
  "tickrate_min": 2.5,             // ✅ Activité réelle
  "veto_absorption": true,         // ✅ Bloque contre-flow
  "absorption_penalty": 0.15       // ✅ Pénalité 15%
}

"orderflow": {
  "delta_abs_min": 15.0            // ✅ Delta significatif
}
```

---

## 📈 Impact Attendu

### AVANT (Configuration Actuelle)
- ❌ Footprint accepte tout (m1_min_ticks=3, tickrate=0.1)
- ❌ Triggers optionnels (require_trigger=false)
- ❌ Autorise trades contrariens (allow_opposite_trigger=true)
- ❌ Fusion score très bas (0.15 = 15%)
- ❌ Orderflow V6 bypassé par fusion permissive
- ❌ Prend BUY quand devrait SELL

**Résultat** : Volume élevé (10-20 trades/jour) mais qualité MÉDIOCRE

### APRÈS (Configuration Durcie)
- ✅ Footprint filtre vraiment (m1_min_ticks=8, tickrate=2.5, veto_absorption=true)
- ✅ Triggers OBLIGATOIRES (require_trigger=true, min_strength=0.50)
- ✅ BLOQUE trades contrariens (allow_opposite_trigger=false)
- ✅ Fusion score élevé (0.40 = 40%)
- ✅ Orderflow V6 travaille (min_orderflow_score=0.55)
- ✅ Règles métier strictes (veto_absorption=true, never_against_strong_orderflow=true)

**Résultat Attendu** : Volume réduit (5-10 trades/jour) mais qualité EXCELLENTE ⭐

---

## 🔧 Fichiers à Modifier

1. **config/strategy/config_trade_scalping.json**
   - Lignes 77-95 : Footprint, orderflow, triggers
   - Lignes 96-114 : Fusion burst_scalping
   - Lignes 155-171 : Fusion globale, seuils_entree
   - Lignes 203-214 : Pondérations, règles métier

2. **config/assets_config/XAUUSD.json**
   - Lignes 136-142 : of_v6_gate (durcir légèrement)

---

## ⚠️ Note sur le Trailing Stop

Le trailing stop ne s'active jamais (même à 400 pips). C'est un problème SÉPARÉ à investiguer APRÈS avoir corrigé la qualité des trades.

**Hypothèse** :
- Soit la fonction `update_basket_sltp_dynamically` n'est jamais appelée
- Soit le calcul de PnL est incorrect
- Soit il y a un autre bug dans le code de trailing

**Plan** : Corriger DABORD la qualité des trades, PUIS investiguer le trailing.

---

*Analyse créée le 12 Novembre 2025*
