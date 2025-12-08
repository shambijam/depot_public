# 🔄 LOGIQUE DE RETOURNEMENT EN RANGE - Documentation Complète

**Date:** 8 Décembre 2025
**Version:** 1.0
**Status:** ✅ Implémenté

---

## 📋 Problème Identifié

### Symptôme
En régime **RANGE** (range_retail, range_distribution, BALANCED), le système ne détectait **pas les retournements** aux bornes du range :

- Le prix fait **HAUT → BAS → HAUT → BAS** (oscillation)
- Mais l'analyse continue de donner le **même signal** (BUY ou SELL)
- Résultat : Trades **contre-tendance** (acheter en haut, vendre en bas)

### Exemple Concret
```
Prix dans range 4200-4220:
├─ Prix @ 4218 (upper_tercile) → Signal BUY ❌ ERREUR (devrait être SELL)
├─ Prix @ 4202 (lower_tercile) → Signal SELL ❌ ERREUR (devrait être BUY)
```

---

## 💡 Solution Implémentée

### Principe
En régime RANGE, le prix **rebondit** sur les bornes :
- **Support (bas)** : Prix rebondit vers le HAUT → BUY
- **Résistance (haut)** : Prix rebondit vers le BAS → SELL

### Règles d'Inversion
1. **Upper Tercile** (haut du range, position >= 67%) + signal BUY → **INVERSER en SELL**
2. **Lower Tercile** (bas du range, position <= 33%) + signal SELL → **INVERSER en BUY**

---

## 🔧 Modifications du Code

### 1. market_analyzer.py (Lignes 207-212)

**Ajout du contexte range** dans `build_fused_decision()` :

```python
# ✅ AJOUT (08 DEC 2025): Transmettre position dans le range pour logique de retournement
if latest is not None:
    ctx["range_pos_pct"] = float(latest.get("range_pos_pct", 0.5))
    ctx["in_upper_tercile"] = bool(latest.get("in_upper_tercile", False))
    ctx["in_lower_tercile"] = bool(latest.get("in_lower_tercile", False))
    ctx["phase_observer_regime"] = str(latest.get("regime", "unknown"))
```

**Données transmises :**
- `range_pos_pct` : Position dans le range (0.0 = bas, 1.0 = haut)
- `in_upper_tercile` : True si prix dans les 33% supérieurs
- `in_lower_tercile` : True si prix dans les 33% inférieurs
- `phase_observer_regime` : Régime PhaseObserver (range_retail, trending_bull, etc.)

---

### 2. fusion_manager.py (Lignes 1135-1202)

**Nouvelle fonction `_apply_range_reversal_logic()`** :

```python
def _apply_range_reversal_logic(
    self, direction: str, ctx: Dict[str, Any], n_vw: Dict[str, Any]
) -> Tuple[str, bool]:
    """
    ✅ AJOUT (08 DEC 2025): Logique de retournement en régime RANGE

    Règles de retournement :
    - Si in_upper_tercile + signal BUY → INVERSER en SELL (rebond résistance)
    - Si in_lower_tercile + signal SELL → INVERSER en BUY (rebond support)

    Returns:
        Tuple (nouvelle_direction, inversé_bool)
    """
    if direction == "NEUTRAL":
        return (direction, False)

    # Vérifier si on est en régime RANGE
    vwap_regime = n_vw.get("regime", "").upper()
    phase_regime = ctx.get("phase_observer_regime", "").lower()

    is_range_regime = (
        vwap_regime == "BALANCED" or
        "range" in phase_regime or
        "compression" in phase_regime or
        "sideways" in phase_regime
    )

    if not is_range_regime:
        return (direction, False)

    # Récupérer position dans le range
    in_upper = ctx.get("in_upper_tercile", False)
    in_lower = ctx.get("in_lower_tercile", False)
    range_pos = ctx.get("range_pos_pct", 0.5)

    # Logique de retournement
    if in_upper and direction == "BUY":
        # Haut du range + BUY → SELL (rebond résistance)
        return ("SELL", True)
    elif in_lower and direction == "SELL":
        # Bas du range + SELL → BUY (rebond support)
        return ("BUY", True)

    return (direction, False)
```

---

### 3. fusion_manager.py (Lignes 1205-1247)

**Modification de `_final_decision()`** pour appeler la logique de retournement :

```python
def _final_decision(
    self, mode: str, fused: float, n_vw, coherence, cfg, ctx: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    ✅ MISE À JOUR (08 DEC 2025): Ajout logique retournement en range
    """
    ctx = ctx or {}

    # Calcul direction initiale (majorité pondérée)
    maj = coherence["majority"]
    if maj > 0:
        direction = "BUY"
    elif maj < 0:
        direction = "SELL"
    else:
        # Fallback VWAP bias
        vwap_bias = n_vw.get("bias", "neutral").upper()
        if vwap_bias == "BULLISH":
            direction = "BUY"
        elif vwap_bias == "BEARISH":
            direction = "SELL"
        else:
            direction = "NEUTRAL"

    # ✅ AJOUT: Appliquer logique de retournement en range
    original_direction = direction
    direction, was_reversed = self._apply_range_reversal_logic(direction, ctx, n_vw)

    if was_reversed:
        _probe(
            self.log,
            f"[RANGE_REVERSAL] ✅ Direction finale inversée: {original_direction} → {direction}"
        )

    # Continuer avec la nouvelle direction...
```

---

## 📊 Logs Attendus

### Cas 1 : Inversion en Haut du Range
```
[INFO] - [VWAP_REGIME_MAPPER] 🔄 PhaseObserver 'range_retail' → VWAP 'BALANCED'
[INFO] - [FUSION_ENTREE] OrderFlow score=60 | Footprint status=VALID | VWAP score=0.68
[INFO] - [DECISION_FINALE] fused=0.580 direction=BUY
[INFO] - [RANGE_REVERSAL] 🔄 INVERSION BUY→SELL | Raison: UPPER_TERCILE (pos=72%) | Regime: range_retail (BALANCED)
[INFO] - [RANGE_REVERSAL] ✅ Direction finale inversée: BUY → SELL
[INFO] - [TRACE] FUSION fused=0.580 | action=SELL | signal=CONDITIONAL_SELL
```

### Cas 2 : Inversion en Bas du Range
```
[INFO] - [VWAP_REGIME_MAPPER] 🔄 PhaseObserver 'range_retail' → VWAP 'BALANCED'
[INFO] - [FUSION_ENTREE] OrderFlow score=55 | Footprint status=VALID | VWAP score=0.65
[INFO] - [DECISION_FINALE] fused=0.550 direction=SELL
[INFO] - [RANGE_REVERSAL] 🔄 INVERSION SELL→BUY | Raison: LOWER_TERCILE (pos=28%) | Regime: range_retail (BALANCED)
[INFO] - [RANGE_REVERSAL] ✅ Direction finale inversée: SELL → BUY
[INFO] - [TRACE] FUSION fused=0.550 | action=BUY | signal=CONDITIONAL_BUY
```

### Cas 3 : Pas d'Inversion (Milieu du Range)
```
[INFO] - [VWAP_REGIME_MAPPER] 🔄 PhaseObserver 'range_retail' → VWAP 'BALANCED'
[INFO] - [FUSION_ENTREE] OrderFlow score=62 | Footprint status=VALID | VWAP score=0.70
[INFO] - [DECISION_FINALE] fused=0.600 direction=BUY
[INFO] - [TRACE] FUSION fused=0.600 | action=BUY | signal=CONDITIONAL_BUY
(Pas de log RANGE_REVERSAL car position = 48%, ni upper ni lower tercile)
```

### Cas 4 : Pas d'Inversion (Régime TRENDING)
```
[INFO] - [VWAP_REGIME_MAPPER] 🔄 PhaseObserver 'trending_institutional_bull' → VWAP 'TRENDING'
[INFO] - [FUSION_ENTREE] OrderFlow score=75 | Footprint status=VALID | VWAP score=0.82
[INFO] - [DECISION_FINALE] fused=0.750 direction=BUY
[INFO] - [TRACE] FUSION fused=0.750 | action=BUY | signal=HIGH_CONVICTION_BUY
(Pas de log RANGE_REVERSAL car régime TRENDING, pas BALANCED)
```

---

## ✅ Détection des Régimes RANGE

La fonction détecte un régime RANGE si **l'une** de ces conditions est vraie :

1. **VWAP Regime = "BALANCED"**
2. **PhaseObserver regime contient "range"** (range_retail, range_distribution, range_institutional)
3. **PhaseObserver regime contient "compression"** (compression)
4. **PhaseObserver regime contient "sideways"** (sideways)

### Régimes Concernés
- ✅ `range_retail`
- ✅ `range_distribution`
- ✅ `range_institutional`
- ✅ `compression`
- ✅ `sideways`
- ✅ Tout régime PhaseObserver mappé en VWAP "BALANCED"

### Régimes NON Concernés
- ❌ `trending_institutional_bull`
- ❌ `trending_retail_bull`
- ❌ `accumulation_bull`
- ❌ `strong_trending_bull`
- ❌ Tout régime VWAP "TRENDING", "ACCUMULATION", "TRANSITIONAL"

---

## 🎯 Avantages de Cette Approche

### 1. **Précision en Range**
- Détecte les **rebonds** aux bornes du range
- Évite d'acheter en **haut** et vendre en **bas**

### 2. **Pas d'Impact sur Trending**
- La logique **ne s'active QUE** en régime RANGE
- Les régimes trending/accumulation continuent normalement

### 3. **Utilise Données Existantes**
- Pas de nouveau calcul
- Réutilise `range_pos_pct`, `in_upper_tercile`, `in_lower_tercile` du PhaseObserver

### 4. **Logs Transparents**
- Chaque inversion est **loguée** avec raison
- Facile à débugger et valider

---

## 🧪 Test du Système

### Commande de Test
```bash
# Lancer le bot et observer les logs
python run_bot.py

# Filtrer les logs de retournement
grep "RANGE_REVERSAL" DEBUG_LOGS.txt
```

### Validation Attendue
1. **En range_retail** : Voir des inversions BUY→SELL (upper) et SELL→BUY (lower)
2. **En trending** : Aucune inversion visible
3. **Milieu du range** : Pas d'inversion (position 40-60%)

---

## 📁 Fichiers Modifiés

| Fichier | Lignes | Modification |
|---------|--------|--------------|
| `phase_observer/market_analyzer.py` | 207-212 | Ajout contexte range au dict ctx |
| `phase_observer/fusion_manager.py` | 1135-1202 | Nouvelle fonction `_apply_range_reversal_logic()` |
| `phase_observer/fusion_manager.py` | 1205-1247 | Appel logique retournement dans `_final_decision()` |
| `phase_observer/fusion_manager.py` | 413 | Passage ctx à `_final_decision()` |

---

## 🚀 Prochaines Étapes

1. ✅ **Tester** avec le bot en mode DEMO
2. ✅ **Valider** les logs montrent bien les inversions
3. ✅ **Analyser** les trades pour confirmer amélioration qualité
4. ⚡ **Ajuster** les seuils `lower_threshold` / `upper_threshold` si nécessaire (actuellement 33% / 67%)

---

## 📝 Notes Techniques

### Position Range (range_pos_pct)
```python
# Calcul dans phase_observer/orchestrator.py (ligne 845-850)
if rolling_high == rolling_low:
    df_an["range_pos_pct"] = 0.5  # Pas de range détectable
else:
    df_an["range_pos_pct"] = (
        (df_an["close"] - rolling_low) / (rolling_high - rolling_low)
    )
```

### Seuils Terciles
```python
# Définis dans phase_observer_config.json ou par défaut:
lower_threshold = 0.33  # 33% inférieur = lower tercile
upper_threshold = 0.67  # 67% supérieur = upper tercile
```

### Modification Possible
Pour rendre les retournements **plus agressifs** (zones plus larges) :
```json
{
  "phase_detection_defaults": {
    "range_position": {
      "lower_threshold": 0.40,  // Plus large (40% au lieu de 33%)
      "upper_threshold": 0.60   // Plus large (60% au lieu de 67%)
    }
  }
}
```

---

**Fin du document** ✅
