# 🔧 GUIDE D'IMPLÉMENTATION - MODES BURST

**Date**: 07 Janvier 2026
**Objectif**: Intégrer les 4 modes de burst (niveaux 2, 2.5, 3, 4) dans le code existant

---

## ✅ CE QUI A ÉTÉ FAIT

### 1. Configuration Créée

**Fichiers modifiés** :
- ✅ `/config/strategy/config_trade_scalping.json`
  - Ajout section `burst_modes` avec les 4 niveaux
  - Ajout paramètre `active_burst_mode` pour switcher facilement

- ✅ `/config/assets_config/EURUSD.json`
  - Ajout section `burst_mode_overrides` avec ajustements EURUSD

- ✅ `/config/assets_config/GBPUSD.json`
  - Ajout section `burst_mode_overrides` avec ajustements GBPUSD (haute volatilité)

- ✅ `/config/assets_config/USDJPY.json`
  - Ajout section `burst_mode_overrides` avec ajustements USDJPY (deltas faibles)

### 2. Documentation Créée

- ✅ `/PLAN_TEST_BURST_MODES.md` - Plan de test détaillé sur 4 semaines
- ✅ `/ARCHITECTURE_SCORING_COMPLETE.md` - Documentation complète du système de scoring

---

## 🔄 CE QUI RESTE À FAIRE

### Option A : Implémentation Complète (Recommandée)

Créer une nouvelle fonction dans `scalping.py` qui :
1. Lit le mode actif depuis config
2. Charge les critères du mode (avec overrides par asset)
3. Valide le trade contre ces critères
4. Retourne ACCEPT/REJECT avec raison

### Option B : Implémentation Minimale (Quick Start)

Utiliser les configurations existantes `of_v6_gate` et `decision` qui sont déjà en place, et simplement **ajuster les seuils** selon le mode actif.

---

## 🎯 IMPLÉMENTATION RECOMMANDÉE (Option A)

### Étape 1 : Créer la Fonction de Validation

Ajouter dans `/strategy/scalping.py` :

```python
def _validate_burst_mode_criteria(
    self,
    asset: str,
    orderflow_result: Dict[str, Any],
    footprint_result: Dict[str, Any],
    institutional_analysis: Dict[str, Any],
    composite_score: float
) -> Tuple[bool, str]:
    """
    Valide si le trade répond aux critères du mode burst actif.

    Args:
        asset: EURUSD, GBPUSD, USDJPY
        orderflow_result: Résultat de _score_orderflow_analysis()
        footprint_result: Résultat de _score_footprint_analysis()
        institutional_analysis: Résultat des 5 analyseurs
        composite_score: Score composite final (0-100)

    Returns:
        (is_valid, reject_reason)
        - is_valid: True si tous les critères sont validés
        - reject_reason: Raison du rejet si is_valid=False
    """
    # 1. Charger le mode actif
    strat_cfg = self.strategy_config or {}
    burst_cfg = strat_cfg.get("entry_rules", {}).get("scalping", {}).get("burst_scalping", {})
    active_mode = burst_cfg.get("active_burst_mode", "niveau_3")

    # 2. Charger les critères du mode depuis config globale
    burst_modes_cfg = strat_cfg.get("entry_rules", {}).get("scalping", {}).get("burst_modes", {})
    mode_cfg = burst_modes_cfg.get(active_mode, {})

    if not mode_cfg:
        self.logger.error(f"[{asset}] Mode burst '{active_mode}' non trouvé dans config!")
        return False, f"MODE_NOT_FOUND:{active_mode}"

    # 3. Charger les overrides asset-specific
    asset_cfg = self.config_manager.get_asset_config(asset)
    asset_overrides = asset_cfg.get("overrides", {}).get("scalping", {}).get("burst_mode_overrides", {}).get(active_mode, {})

    # 4. Merger critères (overrides asset > config globale)
    criteria = {**mode_cfg.get("criteria", {}), **asset_overrides.get("criteria", {})}

    # 5. Extraire les scores individuels
    delta_momentum = orderflow_result.get("delta_momentum_score", 0.0)
    volume_confirmation = orderflow_result.get("volume_confirmation_score", 0.0)
    imbalance_strength = orderflow_result.get("imbalance_strength_score", 0.0)
    absorption_levels = footprint_result.get("absorption_levels_score", 0.0)
    order_clustering = footprint_result.get("order_clustering_score", 0.0)

    # 6. Valider critères selon le mode

    # NIVEAU 2 : Delta + Volume + Imbalance
    if active_mode == "niveau_2":
        if delta_momentum < criteria.get("delta_momentum_min", 20.0):
            return False, f"DELTA_LOW:{delta_momentum:.1f}<{criteria.get('delta_momentum_min')}"

        if volume_confirmation < criteria.get("volume_confirmation_min", 10.0):
            return False, f"VOLUME_LOW:{volume_confirmation:.1f}<{criteria.get('volume_confirmation_min')}"

        if imbalance_strength < criteria.get("imbalance_strength_min", 5.0):
            return False, f"IMBALANCE_LOW:{imbalance_strength:.1f}<{criteria.get('imbalance_strength_min')}"

        if composite_score < criteria.get("composite_score_min", 65.0):
            return False, f"COMPOSITE_LOW:{composite_score:.1f}<{criteria.get('composite_score_min')}"

        return True, "NIVEAU_2_VALID"

    # NIVEAU 2.5 : Trio + AU MOINS UN boost institutionnel
    elif active_mode == "niveau_2_5":
        # Valider trio de base
        if delta_momentum < criteria.get("delta_momentum_min", 18.0):
            return False, f"DELTA_LOW:{delta_momentum:.1f}"

        if volume_confirmation < criteria.get("volume_confirmation_min", 10.0):
            return False, f"VOLUME_LOW:{volume_confirmation:.1f}"

        if imbalance_strength < criteria.get("imbalance_strength_min", 5.0):
            return False, f"IMBALANCE_LOW:{imbalance_strength:.1f}"

        if composite_score < criteria.get("composite_score_min", 66.0):
            return False, f"COMPOSITE_LOW:{composite_score:.1f}"

        # Vérifier AU MOINS UN boost institutionnel
        require_one = criteria.get("require_at_least_one", {})
        absorption_min = require_one.get("absorption_levels_min", 10.0)
        clustering_min = require_one.get("order_clustering_min", 6.0)
        tape_speed_min = require_one.get("tape_speed_ratio_min", 2.0)

        # Calculer tape_speed_ratio (si disponible)
        tape_speed_data = institutional_analysis.get("tape_speed", {})
        tape_speed_ratio = tape_speed_data.get("speed_ratio", 0.0)

        has_absorption = absorption_levels >= absorption_min
        has_clustering = order_clustering >= clustering_min
        has_tape_speed = tape_speed_ratio >= tape_speed_min

        if not (has_absorption or has_clustering or has_tape_speed):
            return False, f"NO_BOOST:abs={absorption_levels:.1f}<{absorption_min},clust={order_clustering:.1f}<{clustering_min},tape={tape_speed_ratio:.2f}<{tape_speed_min}"

        return True, f"NIVEAU_2_5_VALID:boost={'abs' if has_absorption else 'clust' if has_clustering else 'tape'}"

    # NIVEAU 3 : Absorption Boostée
    elif active_mode == "niveau_3":
        if delta_momentum < criteria.get("delta_momentum_min", 18.0):
            return False, f"DELTA_LOW:{delta_momentum:.1f}"

        if volume_confirmation < criteria.get("volume_confirmation_min", 10.0):
            return False, f"VOLUME_LOW:{volume_confirmation:.1f}"

        if absorption_levels < criteria.get("absorption_levels_min", 10.0):
            return False, f"ABSORPTION_LOW:{absorption_levels:.1f}<{criteria.get('absorption_levels_min')}"

        if order_clustering < criteria.get("order_clustering_min", 3.5):
            return False, f"CLUSTERING_LOW:{order_clustering:.1f}<{criteria.get('order_clustering_min')}"

        if composite_score < criteria.get("composite_score_min", 68.0):
            return False, f"COMPOSITE_LOW:{composite_score:.1f}"

        # Vérifier bonus absorption si requis
        if criteria.get("require_absorption_bonus", False):
            absorption_details = footprint_result.get("absorption_details", {})
            has_volume_bonus = absorption_details.get("volume_bonus") is not None
            has_tick_bonus = absorption_details.get("tick_bonus") is not None

            if not (has_volume_bonus or has_tick_bonus):
                return False, "NO_ABSORPTION_BONUS"

        return True, "NIVEAU_3_VALID"

    # NIVEAU 4 : Institutional Filter
    elif active_mode == "niveau_4":
        # Valider tous les critères du Niveau 3 d'abord
        is_valid_level3, reason = self._validate_niveau_3_criteria(
            criteria, delta_momentum, volume_confirmation, absorption_levels,
            order_clustering, composite_score, footprint_result
        )

        if not is_valid_level3:
            return False, reason

        # Appliquer filtres institutionnels
        inst_filters = mode_cfg.get("institutional_filters", {})
        if not inst_filters.get("enabled", False):
            return True, "NIVEAU_4_VALID:NO_FILTERS"

        # Filtre Market Fatigue
        fatigue_cfg = inst_filters.get("market_fatigue", {})
        allowed_states = fatigue_cfg.get("allowed_states", ["ENERGETIC", "NORMAL"])

        market_fatigue = institutional_analysis.get("market_fatigue", {})
        fatigue_state = market_fatigue.get("market_state", "UNKNOWN")
        fatigue_score_raw = market_fatigue.get("fatigue_score", 5.0)

        if fatigue_state not in allowed_states:
            # Appliquer pénalité au lieu de rejeter dur
            penalty = 0
            if fatigue_state == "FATIGUED":
                penalty = fatigue_cfg.get("penalty_fatigued", 5.0)
            elif fatigue_state == "EXHAUSTED":
                penalty = fatigue_cfg.get("penalty_exhausted", 10.0)

            composite_min_adjusted = criteria.get("composite_score_min", 68.0) + penalty

            if composite_score < composite_min_adjusted:
                return False, f"FATIGUE_PENALTY:{fatigue_state},score={composite_score:.1f}<{composite_min_adjusted:.1f}"

            # Si score dépasse le seuil pénalisé, accepter quand même
            self.logger.warning(
                f"[{asset}] ⚠️ Market {fatigue_state} mais score {composite_score:.1f} "
                f"dépasse seuil pénalisé {composite_min_adjusted:.1f} → ACCEPTÉ"
            )

        # Filtre Price Memory
        memory_cfg = inst_filters.get("price_memory", {})
        min_memory_score = memory_cfg.get("min_score", 50.0)

        price_memory = institutional_analysis.get("price_memory", {})
        memory_signals = price_memory.get("memory_signals", [])
        fresh_levels = price_memory.get("fresh_levels", [])

        if memory_signals or fresh_levels:
            fresh_ratio = len(fresh_levels) / max(1, len(memory_signals) + len(fresh_levels))
            memory_score = 50.0 + (fresh_ratio - 0.5) * 50.0

            if memory_score < min_memory_score:
                return False, f"MEMORY_LOW:{memory_score:.1f}<{min_memory_score}"

        # Filtre Market Physics (alignement)
        physics_cfg = inst_filters.get("market_physics", {})
        require_aligned = physics_cfg.get("require_aligned", True)

        if require_aligned:
            market_physics = institutional_analysis.get("market_physics", {})
            physics_bias = str(market_physics.get("physics_bias", "NEUTRAL")).upper()

            # Déterminer direction du delta (BUY ou SELL)
            delta_details = orderflow_result.get("delta_momentum_details", {})
            delta_direction = delta_details.get("direction", "neutral")

            # Vérifier alignement
            is_aligned = False
            if delta_direction == "buy" and ("BULLISH" in physics_bias or "BUY" in physics_bias):
                is_aligned = True
            elif delta_direction == "sell" and ("BEARISH" in physics_bias or "SELL" in physics_bias):
                is_aligned = True

            if not is_aligned and physics_bias != "NEUTRAL":
                return False, f"PHYSICS_NOT_ALIGNED:delta={delta_direction},physics={physics_bias}"

        return True, "NIVEAU_4_VALID:ALL_FILTERS_PASSED"

    else:
        self.logger.error(f"[{asset}] Mode burst '{active_mode}' non implémenté!")
        return False, f"MODE_NOT_IMPLEMENTED:{active_mode}"


def _validate_niveau_3_criteria(
    self,
    criteria: Dict,
    delta_momentum: float,
    volume_confirmation: float,
    absorption_levels: float,
    order_clustering: float,
    composite_score: float,
    footprint_result: Dict
) -> Tuple[bool, str]:
    """Helper pour valider critères Niveau 3 (utilisé par Niveau 4)."""

    if delta_momentum < criteria.get("delta_momentum_min", 18.0):
        return False, f"DELTA_LOW:{delta_momentum:.1f}"

    if volume_confirmation < criteria.get("volume_confirmation_min", 10.0):
        return False, f"VOLUME_LOW:{volume_confirmation:.1f}"

    if absorption_levels < criteria.get("absorption_levels_min", 10.0):
        return False, f"ABSORPTION_LOW:{absorption_levels:.1f}"

    if order_clustering < criteria.get("order_clustering_min", 3.5):
        return False, f"CLUSTERING_LOW:{order_clustering:.1f}"

    if composite_score < criteria.get("composite_score_min", 68.0):
        return False, f"COMPOSITE_LOW:{composite_score:.1f}"

    if criteria.get("require_absorption_bonus", False):
        absorption_details = footprint_result.get("absorption_details", {})
        has_bonus = (
            absorption_details.get("volume_bonus") is not None or
            absorption_details.get("tick_bonus") is not None
        )
        if not has_bonus:
            return False, "NO_ABSORPTION_BONUS"

    return True, "NIVEAU_3_VALID"
```

### Étape 2 : Appeler la Validation dans le Flux Principal

Trouver où l'action (BUY/SELL) est décidée et ajouter la validation :

```python
# AVANT (quelque part dans decide() ou analyze()):
if total_score >= min_orderflow_score:
    action = "BUY" if delta > 0 else "SELL"

# APRÈS :
if total_score >= min_orderflow_score:
    action = "BUY" if delta > 0 else "SELL"

    # ✅ NOUVEAU (07 JAN 2026): Valider contre le mode burst actif
    is_valid, reject_reason = self._validate_burst_mode_criteria(
        asset=asset,
        orderflow_result=orderflow_result,
        footprint_result=footprint_result,
        institutional_analysis=institutional_analysis,
        composite_score=composite_score
    )

    if not is_valid:
        self.logger.info(
            f"[{asset}] ❌ BURST MODE REJECT: {reject_reason} | "
            f"Delta={delta_momentum:.1f} | Volume={volume_confirmation:.1f} | "
            f"Absorption={absorption_levels:.1f} | Composite={composite_score:.1f}"
        )
        action = None  # Annuler le trade
    else:
        self.logger.info(
            f"[{asset}] ✅ BURST MODE ACCEPT: {reject_reason} | "
            f"Composite={composite_score:.1f}"
        )
```

### Étape 3 : Tester

1. Activer `"active_burst_mode": "niveau_2"` dans config_trade_scalping.json
2. Redémarrer le bot
3. Vérifier logs :
   ```
   [EURUSD] ✅ BURST MODE ACCEPT: NIVEAU_2_VALID | Composite=67.2
   [GBPUSD] ❌ BURST MODE REJECT: DELTA_LOW:17.5<20.0 | ...
   ```

---

## 🚀 IMPLÉMENTATION RAPIDE (Option B)

Si vous voulez tester **immédiatement sans modifier le code**, utilisez les configurations existantes :

### Adapter les Seuils dans Asset Config

**Pour Niveau 2** (EURUSD exemple) :

```json
"entry_rules": {
  "scalping": {
    "burst_scalping": {
      "use_orderflow_v6": true,
      "min_score": 65.0,
      "of_v6_gate": {
        "min_score": 0.70,
        "require_ncp_strong": true,
        "min_delta_abs": 100.0,
        "min_imbalance": 0.60,
        "min_tickrate": 3.0
      }
    }
  }
},
"decision": {
  "min_orderflow_score": 65,
  "min_confidence": 0.70
}
```

**Pour Niveau 3** :

```json
"min_score": 68.0,
"of_v6_gate": {
  "min_score": 0.72,
  "min_delta_abs": 100.0,
  "min_absorption": 10.0,  // ⚠️ À ajouter si pas dans code
  "min_clustering": 3.5     // ⚠️ À ajouter si pas dans code
},
"decision": {
  "min_orderflow_score": 68
}
```

⚠️ **Limitation** : Cette approche ne permet pas de valider les critères complexes comme "require_at_least_one" du Niveau 2.5 ou les filtres institutionnels du Niveau 4.

---

## 📝 CHECKLIST D'IMPLÉMENTATION

### Phase 1 : Préparation
- [x] Configuration des 4 modes créée
- [x] Overrides par asset créés
- [x] Plan de test documenté
- [ ] Fonction `_validate_burst_mode_criteria()` ajoutée à scalping.py
- [ ] Helper `_validate_niveau_3_criteria()` ajouté

### Phase 2 : Intégration
- [ ] Appel de validation ajouté dans le flux principal
- [ ] Logs de reject/accept activés
- [ ] Tests unitaires créés (optionnel)

### Phase 3 : Test
- [ ] Mode Niveau 2 testé en live
- [ ] Mode Niveau 2.5 testé
- [ ] Mode Niveau 3 testé
- [ ] Mode Niveau 4 testé

### Phase 4 : Optimisation
- [ ] Résultats analysés
- [ ] Mode optimal sélectionné
- [ ] Configuration finale validée

---

## 🎯 RECOMMANDATION FINALE

**Je recommande l'Option A (Implémentation Complète)** car :

1. ✅ **Flexibilité totale** : Tous les 4 modes testables
2. ✅ **Configuration externalisée** : Pas besoin de modifier le code pour changer de mode
3. ✅ **Traçabilité** : Logs clairs des rejets avec raisons
4. ✅ **Évolutif** : Facile d'ajouter de nouveaux modes

**Temps d'implémentation estimé** : 2-3 heures

---

## ❓ QUESTIONS / BLOCKERS

Si vous rencontrez des difficultés :

1. **Où appeler `_validate_burst_mode_criteria()`** : Cherchez dans scalping.py où `action = "BUY"` ou `action = "SELL"` est défini, et ajoutez la validation juste après.

2. **Comment obtenir institutional_analysis** : Il devrait être retourné par `of_v6_result.get('institutional_analysis', {})` si les 5 analyseurs sont appelés.

3. **Tester sans live trading** : Utilisez un mode "dry run" ou paper trading pour valider la logique avant de trader réellement.

---

**Prêt pour l'implémentation ?** 🚀

N'hésitez pas si vous avez besoin d'aide pour :
- Localiser exactement où ajouter le code
- Débugger l'intégration
- Tester les premiers résultats

---

**Document créé le** : 07 Janvier 2026
**Auteur** : Claude Sonnet 4.5
**Version** : 1.0
