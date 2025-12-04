# PLAN D'IMPLÉMENTATION - POIDS DYNAMIQUES FUSIONMANAGER

**Date Planification** : 4 Décembre 2025
**Date Implémentation** : Semaine du 9 Décembre 2025 (après recharge tokens)

---

## 🎯 Objectif

Implémenter le système de **poids dynamiques dans FusionManager** où les pondérations VWAP/OrderFlow/Footprint changent selon le régime de marché détecté.

---

## 📊 Ce Que L'Utilisateur Veut

**Citation** :
> "ce qui m'interassais et que je trouvais tres bien c'etait de changer de scoring selon la phase de marché comme ceci :
> - 08h00 : Régime ACCUMULATION → Poids [25/40/35]
> - 10h30 : News USD → Régime TRANSITIONAL → [35/40/25]
> - 14h00 : London active → Régime TRENDING → [50/30/20]
> - 16h00 : NY join → Régime STRONG_TRENDING → [55/25/20]
>
> c'est a dire que le vwap prends plus de poids selon la phase de marché en cours"

**Traduction** :
- En ACCUMULATION (marché plat) : VWAP 25%, OF 40%, FP 35% (microstructure importante)
- En TRENDING (tendance claire) : VWAP 50-55%, OF 30-25%, FP 20% (VWAP dominant)

---

## 📋 État Actuel (4 Décembre 2025)

### ✅ Ce Qui Est Déjà Fait

**1. RegimeMapper** (Mapping Simple)
- Fichier : `phase_observer/vwap/regime_mapper.py`
- Fonction : Mappe 11 régimes PhaseObserver → 4 régimes VWAP
- Régimes VWAP : TRENDING, ACCUMULATION, BALANCED, TRANSITIONAL
- **PROBLÈME** : Ne contient PAS les poids dynamiques pour FusionManager

**2. VWAP Adaptive Scoring** (Scoring Interne)
- Fichier : `phase_observer/vwap/signals.py` (lignes 84-121)
- Fonction : Ajuste trend_weight/position_weight selon régime
- **PROBLÈME** : Ce n'est PAS ce que l'utilisateur veut (c'est du scoring interne VWAP)

**3. Tests de Validation**
- Fichiers : `tests/test_regime_mapping_logic.py`, `tests/test_regime_mapper_simple.py`
- Statut : ✅ Tous les tests passent

### ❌ Ce Qui MANQUE (Ce Que L'Utilisateur Veut)

**FusionManager Dynamic Weights** :
- Actuellement : Poids FIXES dans FusionManager
  ```python
  # phase_observer/fusion_manager.py ligne 617-619
  w_of = 0.25  # ❌ TOUJOURS 25%
  w_fp = 0.25  # ❌ TOUJOURS 25%
  w_vw = 0.50  # ❌ TOUJOURS 50%
  ```
- Attendu : Poids DYNAMIQUES qui changent selon le régime
  ```python
  # Si régime = TRENDING
  w_vwap = 0.50  # VWAP domine
  w_of = 0.30
  w_fp = 0.20

  # Si régime = ACCUMULATION
  w_vwap = 0.25  # Microstructure domine
  w_of = 0.40
  w_fp = 0.35
  ```

---

## 🔧 Plan d'Implémentation (Semaine Prochaine)

### Phase 1 : Décision sur les Régimes (30 min)

**Question à trancher** : 4 régimes ou 8 régimes ?

**Option A : 4 Régimes (Simple)**
- TRENDING, ACCUMULATION, BALANCED, TRANSITIONAL
- Avantage : Déjà implémenté dans RegimeMapper
- Inconvénient : Moins granulaire

**Option B : 8 Régimes (Rapport)**
- STRONG_TRENDING, TRENDING, ACCUMULATION, COMPRESSION, RANGE_EXTENSION, EXTREME_REVERSION, TRANSITIONAL, BREAKOUT
- Avantage : Plus précis, suit le rapport
- Inconvénient : Nécessite d'implémenter la détection de sous-régimes

**Recommandation** : Commencer avec 4 régimes (déjà testés), ajouter les 8 plus tard si besoin.

---

### Phase 2 : Création Matrice de Poids (1 heure)

**Fichier à créer** : `config/regime_weights.json`

```json
{
  "REGIME_WEIGHTS": {
    "TRENDING": {
      "description": "Tendance claire, VWAP leader",
      "weights": {
        "vwap": 0.50,
        "orderflow": 0.30,
        "footprint": 0.20
      },
      "veto_power": false,
      "min_confidence": 0.70
    },

    "ACCUMULATION": {
      "description": "Marché range, microstructure importante",
      "weights": {
        "vwap": 0.25,
        "orderflow": 0.40,
        "footprint": 0.35
      },
      "veto_power": false,
      "min_confidence": 0.75
    },

    "BALANCED": {
      "description": "Équilibre, poids neutres",
      "weights": {
        "vwap": 0.35,
        "orderflow": 0.35,
        "footprint": 0.30
      },
      "veto_power": false,
      "min_confidence": 0.70
    },

    "TRANSITIONAL": {
      "description": "Changement de régime, prudence",
      "weights": {
        "vwap": 0.35,
        "orderflow": 0.40,
        "footprint": 0.25
      },
      "veto_power": false,
      "min_confidence": 0.78
    }
  }
}
```

**Note** : Si on passe à 8 régimes, ajouter STRONG_TRENDING (55/25/20), etc.

---

### Phase 3 : Modification FusionManager (2-3 heures)

**Fichier** : `phase_observer/fusion_manager.py`

#### A) Ajout Chargement Config (ligne ~50)

```python
import json
from pathlib import Path

# Charger regime_weights.json
REGIME_WEIGHTS_FILE = Path(__file__).parent.parent / "config" / "regime_weights.json"
with open(REGIME_WEIGHTS_FILE, "r") as f:
    REGIME_WEIGHTS = json.load(f)["REGIME_WEIGHTS"]
```

#### B) Nouvelle Fonction `_get_dynamic_weights()` (après ligne 619)

```python
def _get_dynamic_weights(self, vwap_data: Optional[dict]) -> dict:
    """
    Retourne les poids dynamiques selon le régime VWAP.

    Args:
        vwap_data: Données VWAP contenant le régime

    Returns:
        dict: {"vwap": 0.50, "orderflow": 0.30, "footprint": 0.20}
    """
    # Poids par défaut (BALANCED)
    default_weights = {
        "vwap": 0.35,
        "orderflow": 0.35,
        "footprint": 0.30
    }

    # Si pas de données VWAP, utiliser défaut
    if not vwap_data:
        return default_weights

    # Extraire le régime
    regime = vwap_data.get("regime", "BALANCED")

    # Charger les poids depuis la config
    if regime in REGIME_WEIGHTS:
        return REGIME_WEIGHTS[regime]["weights"]
    else:
        self.logger.warning(f"[FUSION] Régime inconnu: {regime}, utilise BALANCED")
        return default_weights
```

#### C) Modification de `fuse()` (ligne 560-680)

**AVANT (poids fixes)** :
```python
# Ligne 617-619
w_of = _to_float(p.get("orderflow_weight"), 0.25)
w_fp = _to_float(p.get("footprint_weight"), 0.25)
w_vw = _to_float(p.get("vwap_weight"), 0.50)
```

**APRÈS (poids dynamiques)** :
```python
# Ligne 617-625 (remplacer)
# ===== POIDS DYNAMIQUES SELON RÉGIME VWAP =====
dynamic_weights = self._get_dynamic_weights(vwap_data)
w_vw = dynamic_weights["vwap"]
w_of = dynamic_weights["orderflow"]
w_fp = dynamic_weights["footprint"]

self.logger.info(
    f"[FUSION][DYNAMIC_WEIGHTS] Régime={vwap_data.get('regime', 'UNKNOWN')} | "
    f"Poids: VWAP={w_vw:.0%}, OF={w_of:.0%}, FP={w_fp:.0%}"
)
```

#### D) Ajout Log Détaillé (ligne 675)

```python
# Après calcul du score fusionné (ligne 675)
self.logger.info(
    f"[FUSION][RESULT] Score={fused:.1%} | "
    f"VWAP={vwap_score:.1%}×{w_vw:.0%}={vwap_score*w_vw:.1%} | "
    f"OF={of_score:.1%}×{w_of:.0%}={of_score*w_of:.1%} | "
    f"FP={fp_score:.1%}×{w_fp:.0%}={fp_score*w_fp:.1%}"
)
```

---

### Phase 4 : Tests et Validation (1 heure)

#### Test Unitaire : `tests/test_dynamic_weights.py`

```python
#!/usr/bin/env python3
"""
Tests du système de poids dynamiques FusionManager
"""

def test_dynamic_weights_trending():
    """Test poids en régime TRENDING"""
    vwap_data = {"regime": "TRENDING"}
    weights = fusion_manager._get_dynamic_weights(vwap_data)

    assert weights["vwap"] == 0.50
    assert weights["orderflow"] == 0.30
    assert weights["footprint"] == 0.20
    print("✅ TRENDING: VWAP 50%, OF 30%, FP 20%")

def test_dynamic_weights_accumulation():
    """Test poids en régime ACCUMULATION"""
    vwap_data = {"regime": "ACCUMULATION"}
    weights = fusion_manager._get_dynamic_weights(vwap_data)

    assert weights["vwap"] == 0.25
    assert weights["orderflow"] == 0.40
    assert weights["footprint"] == 0.35
    print("✅ ACCUMULATION: VWAP 25%, OF 40%, FP 35%")

def test_dynamic_weights_fallback():
    """Test fallback si régime inconnu"""
    vwap_data = {"regime": "UNKNOWN"}
    weights = fusion_manager._get_dynamic_weights(vwap_data)

    # Devrait utiliser BALANCED par défaut
    assert weights["vwap"] == 0.35
    assert weights["orderflow"] == 0.35
    assert weights["footprint"] == 0.30
    print("✅ FALLBACK: BALANCED (35/35/30)")

if __name__ == "__main__":
    test_dynamic_weights_trending()
    test_dynamic_weights_accumulation()
    test_dynamic_weights_fallback()
    print("\n✅ TOUS LES TESTS PASSENT")
```

#### Test en Conditions Réelles

**Logs Attendus** :
```
[VWAP][REGIME] PhaseObserver=trending_institutional_bull → VWAP_Regime=TRENDING
[FUSION][DYNAMIC_WEIGHTS] Régime=TRENDING | Poids: VWAP=50%, OF=30%, FP=20%
[FUSION][RESULT] Score=78.5% | VWAP=85%×50%=42.5% | OF=70%×30%=21.0% | FP=75%×20%=15.0%
```

---

## 🎯 Résumé des Fichiers à Modifier

| Fichier | Action | Estimation |
|---------|--------|------------|
| `config/regime_weights.json` | ✅ Créer | 30 min |
| `phase_observer/fusion_manager.py` | ✅ Modifier | 2 heures |
| `tests/test_dynamic_weights.py` | ✅ Créer | 30 min |

**Total estimé** : **3 heures**

---

## ⚠️ Points d'Attention

1. **Vérifier que VWAP retourne bien le régime** :
   - Fichier : `phase_observer/vwap/analyzer.py`
   - Ligne 134-147 : Le régime doit être dans `derivatives`

2. **S'assurer que vwap_data arrive dans FusionManager** :
   - Fichier : `run_bot.py` ligne 1850-2100 (scalping thread)
   - Vérifier que `vwap_data` est bien passé à `fusion_manager.fuse()`

3. **Si 8 régimes** :
   - Ajouter détection sous-régimes dans `RegimeMapper`
   - Exemple : TRENDING → STRONG_TRENDING si `slope_consistency > 0.8` ET `distance > 300 pips`

---

## 📊 Impact Attendu

**Avant (Poids Fixes)** :
```
ACCUMULATION : VWAP 50%, OF 30%, FP 20%  ❌ (VWAP trop influent pour marché plat)
TRENDING     : VWAP 50%, OF 30%, FP 20%  ✅ (correct)
```

**Après (Poids Dynamiques)** :
```
ACCUMULATION : VWAP 25%, OF 40%, FP 35%  ✅ (microstructure domine)
TRENDING     : VWAP 50%, OF 30%, FP 20%  ✅ (VWAP domine)
STRONG_TREND : VWAP 55%, OF 25%, FP 20%  ✅ (VWAP très dominant)
```

**Résultat** : Meilleure adaptation au contexte de marché, moins de faux signaux.

---

## 🚀 Prochaines Étapes (Semaine Prochaine)

1. **Lundi** : Créer `regime_weights.json` + Tests unitaires
2. **Mardi** : Modifier FusionManager + Validation
3. **Mercredi** : Tests en conditions réelles + Ajustements
4. **Jeudi** : Documenter et commit

---

*Document créé le 4 Décembre 2025*
*À implémenter semaine du 9 Décembre 2025*
