# 🎯 Guide de Réglage des Seuils de Score - FusionManager

**Date:** 2025-11-25
**Objectif:** Resserrer les critères pour réduire le nombre de trades et améliorer la qualité

---

## 📊 Comprendre les Seuils Actuels

### Configuration Actuelle (Trop Permissive)

```json
"scoring_thresholds": {
  "high": 0.65,        // 65% - HIGH_CONVICTION
  "moderate": 0.55,    // 55% - MODERATE
  "cautious": 0.45,    // 45% - CAUTIOUS ← VOTRE SEUIL D'ENTRÉE ACTUEL
  "conditional": 0.40  // 40% - Jamais utilisé en pratique
}
```

---

## 🔍 Explication de Chaque Paramètre

### 1. **`high` (65%)** - HIGH_CONVICTION

**Signification:**
- Setup avec consensus **unanime** (3/3 composants alignés)
- Trigger détecté avec **haute confiance** (>80%)
- OrderFlow ET Footprint tous deux **valides** (pas SUSPECT)
- Qualité des données **excellente**

**Comportement:**
- Label: `HIGH_CONVICTION_BUY` ou `HIGH_CONVICTION_SELL`
- Ces trades ont statistiquement le **meilleur taux de réussite**

**Exemple:**
```
OrderFlow: 70% BUY (VALID)
Footprint: 82% BUY (VALID)
Trigger: absorption_reject 91% BUY
→ Score final: 0.75 (75%) → HIGH_CONVICTION_BUY ✅
```

---

### 2. **`moderate` (55%)** - MODERATE

**Signification:**
- Setup avec **bon consensus** (au moins 2/3 composants alignés)
- Trigger peut être absent ou faible (<80%)
- OrderFlow OU Footprint peut être SUSPECT (mais pas les deux)
- Qualité des données **correcte**

**Comportement:**
- Label: `MODERATE_BUY` ou `MODERATE_SELL`
- Risque plus élevé que HIGH_CONVICTION mais acceptable

**Exemple:**
```
OrderFlow: 35% BUY (SUSPECT)
Footprint: 78% BUY (VALID)
Trigger: stacking 72% BUY
→ Score final: 0.58 (58%) → MODERATE_BUY ⚠️
```

---

### 3. **`cautious` (45%)** - CAUTIOUS ⚠️ **← VOUS ÊTES ICI**

**Signification:**
- Setup avec consensus **faible** ou divergent
- Trigger absent ou très faible (<60%)
- OrderFlow **souvent SUSPECT** (score <70%)
- Qualité des données **moyenne à médiocre**

**Comportement:**
- Label: `CAUTIOUS_BUY` ou `CAUTIOUS_SELL`
- **RISQUE ÉLEVÉ** - Beaucoup de faux signaux
- **C'est ce seuil qui vous fait prendre trop de trades !**

**Exemple (vos logs):**
```
OrderFlow: 19% SELL (SUSPECT)
Footprint: 80% SELL (VALID)
Trigger: absorption_reject 85.9% SELL
→ Score base: 47.2%
→ Trigger boost: +20%
→ Score final: 67.1% (CAUTIOUS_SELL mais passe quand même)
```

**Problème:** Avec OrderFlow à 19%, le setup est **douteux**, mais le trigger fort (+20%) le fait passer.

---

### 4. **`conditional` (40%)** - Jamais Utilisé

**Signification:**
- Seuil "de secours" pour des cas très spécifiques
- En pratique, **jamais atteint** car le bot bloque en dessous de `cautious`

**Comportement:**
- Si vous abaissez `cautious` en dessous de 40%, ce seuil prendra le relais
- **NE PAS MODIFIER** ce paramètre (gardez-le à 0.40)

---

## 🎯 Paramètres Recommandés pour Resserrer

### Option 1: Moderate (Équilibré) ⭐ **RECOMMANDÉ**

**Objectif:** Réduire les trades de ~50%, garder seulement les setups corrects

```json
"scoring_thresholds": {
  "high": 0.70,        // 70% (au lieu de 65%)
  "moderate": 0.60,    // 60% (au lieu de 55%)
  "cautious": 0.55,    // 55% (au lieu de 45%) ← NOUVEAU SEUIL D'ENTRÉE
  "conditional": 0.40  // 40% (ne pas toucher)
}
```

**Impact:**
- Élimine les setups avec OrderFlow < 30%
- Exige un Footprint > 70% (VALID)
- Trigger devient quasi-obligatoire pour compenser un OF faible
- **Réduction estimée: 40-60% des trades**

**Exemple (trade refusé):**
```
OrderFlow: 19% SELL (SUSPECT) ← Trop faible
Footprint: 80% SELL (VALID)
Trigger: +20%
→ Score final: 67.1%
→ ❌ REFUSÉ (< 55%) car base 47% + boost 20% = 67% mais OF trop suspect
```

---

### Option 2: High Only (Strict) 🔒

**Objectif:** Prendre UNIQUEMENT les meilleurs setups (réduction ~80% des trades)

```json
"scoring_thresholds": {
  "high": 0.75,        // 75% (au lieu de 65%)
  "moderate": 0.65,    // 65% (au lieu de 55%)
  "cautious": 0.65,    // 65% (au lieu de 45%) ← SEUIL D'ENTRÉE = MODERATE
  "conditional": 0.40  // 40% (ne pas toucher)
}
```

**Impact:**
- Exige OrderFlow > 50% (VALID)
- Exige Footprint > 70% (VALID)
- Trigger obligatoire avec confiance > 75%
- Consensus **unanime** (3/3) requis
- **Réduction estimée: 70-80% des trades**

**Résultat:** Très peu de trades, mais **qualité maximale**

---

### Option 3: Custom (Agressif mais Filtré) ⚡

**Objectif:** Garder un volume raisonnable mais éliminer le bruit

```json
"scoring_thresholds": {
  "high": 0.68,        // 68% (compromis)
  "moderate": 0.58,    // 58% (léger resserrement)
  "cautious": 0.50,    // 50% (au lieu de 45%) ← NOUVEAU SEUIL D'ENTRÉE
  "conditional": 0.40  // 40% (ne pas toucher)
}
```

**Impact:**
- Élimine les setups avec score base < 40%
- Garde les triggers forts (boost +15-20%)
- Tolère un OF SUSPECT si FP + Trigger compensent
- **Réduction estimée: 30-40% des trades**

---

## 🔧 Comment Modifier (Étape par Étape)

### 1. Localiser le Fichier de Config

Le paramètre se trouve dans **l'un de ces fichiers** (selon votre architecture):

**Option A:** `config/config_trade_scalping.json` (stratégie scalping)
```json
{
  "entry_rules": {
    "scalping": {
      "fusion": {
        "scoring_thresholds": {
          "high": 0.65,
          "moderate": 0.55,
          "cautious": 0.45,
          "conditional": 0.40
        }
      }
    }
  }
}
```

**Option B:** `config/assets_config/XAUUSD.json` (config spécifique XAUUSD)
```json
{
  "entry_rules": {
    "scalping": {
      "fusion": {
        "scoring_thresholds": {
          "high": 0.65,
          "moderate": 0.55,
          "cautious": 0.45,
          "conditional": 0.40
        }
      }
    }
  }
}
```

**Option C:** `config/config_main.json` (config globale - rare)

---

### 2. Éditer le Fichier

**Sur Windows VPS:**

1. Ouvrir **Bloc-notes** (Notepad) ou **Notepad++**
2. **Fichier** → **Ouvrir** → Naviger vers le fichier config
3. Chercher `"scoring_thresholds"`
4. Modifier les valeurs selon l'option choisie
5. **Fichier** → **Enregistrer**

**⚠️ ATTENTION:** Respectez la syntaxe JSON:
- Virgules entre les lignes (sauf la dernière)
- Pas d'espace avant les deux-points `:`
- Valeurs entre 0.0 et 1.0 (pas de pourcentage)

---

### 3. Redémarrer le Bot

**Important:** Les modifications de config nécessitent un redémarrage pour être prises en compte.

```powershell
# Arrêter le bot (Ctrl+C dans la console)
# Puis relancer:
python run_bot.py
```

---

### 4. Vérifier l'Application

Après redémarrage, les logs afficheront:

```
[FUSION][DEBUG] Seuils: high=0.70 moderate=0.60 cautious=0.55
```

**Si un setup est refusé:**
```
[FUSION][XAUUSD] HOLD: WAIT_CONFIRMATION (score 52% < seuil 55%)
```

**Si un setup passe:**
```
[FUSION][XAUUSD] BUY score=67.10 (MODERATE_BUY)
```

---

## 📊 Analyse de Vos Trades Actuels

### Trade 1 (Logs ligne 79):
```
OrderFlow: 15.3% SELL (SUSPECT)
Footprint: 79% SELL (VALID)
Base: 47.2%
Trigger: absorption_reject +20%
→ Score final: 67.1% (CAUTIOUS_SELL)
```

**Avec seuil 55% (Option 1):**
- Base 47.2% < 55% → ❌ **REFUSÉ** (même avec trigger +20% = 67%)
- Raison: OrderFlow trop faible (15.3%)

**Avec seuil 50% (Option 3):**
- Base 47.2% < 50% mais score final 67.1% > 50% → ✅ **ACCEPTÉ**

---

### Trade 2 (Logs ligne 184):
```
OrderFlow: 15.3% SELL (SUSPECT)
Footprint: 79% SELL (VALID)
Base: 47.2%
Trigger: aucun (fusion_pretrigger 65.9% HOLD)
→ Score final: 47.2% (WAIT_CONFIRMATION)
```

**Avec seuil 45% (actuel):**
- 47.2% > 45% → ✅ Passe théoriquement, mais action=HOLD donc refusé

**Avec seuil 55% (Option 1):**
- 47.2% < 55% → ❌ **REFUSÉ** (correct, pas de trigger)

---

## 🎯 Ma Recommandation pour Demain

### Commencez avec **Option 1** (Équilibré)

```json
"scoring_thresholds": {
  "high": 0.70,
  "moderate": 0.60,
  "cautious": 0.55,
  "conditional": 0.40
}
```

**Pourquoi ?**
- ✅ Élimine les setups avec OF < 30% (trop suspect)
- ✅ Garde les bons triggers (compensation possible)
- ✅ Réduit le volume de ~50% (moins de noise)
- ✅ Conserve les opportunités valides

**Si demain vous avez encore trop de trades:**
→ Passez à **Option 2** (High Only, seuil 65%)

**Si demain vous n'avez AUCUN trade:**
→ Revenez à **Option 3** (seuil 50%)

---

## 📝 Autres Paramètres à Considérer

### 1. Filtres de Qualité (Complémentaires)

**Dans le même fichier config, cherchez:**

```json
"validation_rules": {
  "require_triggers": true,           // ← Forcer la détection d'un trigger
  "min_tick_count": 100,              // ← Minimum 100 ticks (au lieu de 50)
  "require_valid_orderflow": true,    // ← Bloquer si OF SUSPECT
  "require_unanimous_consensus": true // ← Exiger 3/3 composants alignés
}
```

**Impact:** Réduit drastiquement le volume (peut-être trop strict pour commencer)

---

### 2. Cooldown (Espacement des Trades)

```json
"cooldown_after_exit_s": 60  // 60s au lieu de 30s
```

Évite de reprendre un trade trop vite après une sortie.

---

### 3. Tick Count Minimum (Qualité Footprint)

Dans les logs, vous avez:
```
Ticks: 68 ticks | Coverage: 10.0s
```

**Problème:** 68 ticks sur 10s = 6.8 ticks/s → **qualité moyenne**

**Solution:** Exiger minimum 100-150 ticks

```json
"min_tick_count": 150  // Au lieu de 50
```

---

## 🔄 Plan d'Action pour Demain

### Étape 1: Modifier la Config (5 min)

1. Ouvrir `config/config_trade_scalping.json`
2. Modifier `scoring_thresholds` avec **Option 1**
3. Enregistrer

### Étape 2: Redémarrer le Bot

```powershell
# Arrêter (Ctrl+C)
# Relancer
python run_bot.py
```

### Étape 3: Observer les Logs

Chercher ces lignes:
```
[FUSION][XAUUSD] BUY score=XX.X (MODERATE_BUY)  ← Trade pris
[FUSION][XAUUSD] HOLD: WAIT_CONFIRMATION        ← Trade refusé
```

### Étape 4: Analyser en Fin de Journée

Comparer:
- **Nombre de trades:** Avant vs Après
- **Qualité moyenne:** Score moyen des trades pris
- **Taux de réussite:** % de trades gagnants

### Étape 5: Ajuster si Nécessaire

- **Trop de trades encore ?** → Monter à 60% (Option 2 partielle)
- **Pas assez de trades ?** → Redescendre à 50% (Option 3)
- **Parfait ?** → Garder 55% ✅

---

## ⚠️ Points d'Attention

### 1. Ne Modifiez Qu'UN Paramètre à la Fois

**Mauvaise approche:**
```json
// Modifier scoring_thresholds + min_tick_count + cooldown en même temps
```

**Bonne approche:**
```json
// Jour 1: Modifier seulement scoring_thresholds
// Jour 2: Si besoin, ajouter min_tick_count
// Jour 3: Si besoin, augmenter cooldown
```

Sinon vous ne saurez pas quel paramètre a l'effet.

### 2. Donnez 1 Journée de Test Minimum

Ne changez pas les paramètres toutes les heures. Laissez au moins une session complète (ex: session Londres + NY).

### 3. Sauvegardez l'Ancienne Config

Avant de modifier:
```powershell
copy config\config_trade_scalping.json config\config_trade_scalping.json.backup
```

Comme ça vous pouvez revenir en arrière si nécessaire.

---

## 📞 Besoin d'Aide ?

Si demain vous avez des questions sur les résultats:
- Copiez les logs dans `DEBUG_LOGS.txt`
- Notez combien de trades ont été pris vs refusés
- Je pourrai analyser et affiner les paramètres

---

Bon réglage ! 🎯
