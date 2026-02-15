# Session 15 FEV 2026 — Migration logique de decision vers decision_pipeline.py

## Objectif

Centraliser TOUTE la logique de decision de trade (3 branches, Triple Filtre, PMA, vetos)
dans `core/decision_pipeline.py`. Le fichier `run_bot.py` ne fait plus que collecter les donnees
et executer les trades.

---

## Changements effectues

### 1. `core/decision_pipeline.py` — Nouvelles fonctions

#### `decide_scalp_action()` (methode de DecisionPipeline)

Signature :
```python
def decide_scalp_action(
    self, asset, orderflow_result_mini, mtf_verdict, mtf_direction,
    timing_verdict, micro_resistance_info, inst_result, inst_score,
    inst_veto_fatigue, inst_veto_reversal, memory_clarity,
    memory_trend_strength, memory_trend_direction, market_results,
    scalping_config_global, asset_cfg, price_memory_analyzer,
    rates_df_fresh, current_price, point, digits, latest, ctx,
    logger_ref=None
) -> dict:
```

Contenu migre depuis `run_bot.py` (anciennes lignes 2624-3406) :
- Override veto logic (can_override_veto, timing_blocks_trade)
- **Branche 1** : TIMING_VETO -> HOLD (retour immediat)
- **Branche 2** : OVERRIDE_VETO (score >= 85) -> regime check + build_decision + Triple Filtre + PMA
- **Branche 3** : PASS_NORMAL (timing OK, score < 85) -> regime check + build_decision + PMA
- Construction de fusion_out

**Consolidation B2+B3** : Le regime check, build_decision, PMA, et construction de fusion_out
sont desormais partages entre les deux branches. Seul le Triple Filtre est specifique a la Branche 2
(`if is_override:`).

#### `apply_pma_adjustments()` (fonction module-level)

Deplacee depuis `run_bot.py` (anciennes lignes 1650-1837). Fonction pure sans dependances.
Calcule les bonus/malus PMA et les vetos (dur, reversal, fatigue).

### 2. `run_bot.py` — Simplification

- **Supprime** : `apply_pma_adjustments()` (~190 lignes)
- **Supprime** : Logique de decision (anciennes lignes 2624-3406, ~780 lignes)
- **Supprime** : `market_analyzer_thread` (inutilise, `build_decision()` est appele par DecisionPipeline)
- **Ajoute** : Appel unique `decision_pipeline.decide_scalp_action()` (~25 lignes)
- **Ajoute** : Extraction `decision_mini` depuis `fusion_out` pour compatibilite dashboard/rapport

### 3. Ce qui RESTE dans `run_bot.py` (scalping_worker)

1. **Collecte de donnees** : bars M1/M5/M15/M30, ticks, account_info, symbol_info
2. **MTF verdict** : calcul avant orderflow via `price_memory_analyzer.get_mtf_trend_verdict()`
3. **OrderFlow V6** : via `scalping_strategy._analyze_orderflow_v6()`
4. **Composite score** : via `advanced_scorer.calculate_composite_score()`
5. **Price Memory** : trend analysis
6. **Micro-resistance** : detection
7. **IRD** : Institutional Reversal Detector
8. **Timing Gatekeeper** : `evaluate_trading_conditions()`
9. **APPEL decision** : `decision_pipeline.decide_scalp_action()` (1 appel)
10. **Dashboard/rapport** : update global_state, display_queue
11. **Execution** : burst guard + skeleton + `run_trade_execution_pipeline()`

---

## Bilan quantitatif

| Fichier | Avant | Apres | Delta |
|---------|-------|-------|-------|
| `run_bot.py` | ~4250 lignes | ~3450 lignes | **-800 lignes** |
| `core/decision_pipeline.py` | ~2336 lignes | ~3052 lignes | **+716 lignes** |
| **Total net** | | | **-84 lignes** (consolidation B2+B3) |

---

## Verification

- `py_compile run_bot.py` : OK
- `py_compile core/decision_pipeline.py` : OK
- Les logs `[DECISION_BRANCH]`, `[TRIPLE_FILTER]`, `[PMA_ADJUSTMENT]` sont preserves
- `fusion_out` retourne la meme structure qu'avant
- `orderflow_result_mini` est mutable (dict passe par reference) : les modifications
  de score dans `decide_scalp_action()` sont visibles dans le worker pour le dashboard
- `decision_mini` est reconstruit depuis `fusion_out` pour les sections rapport/dashboard

---

## Architecture finale

```
scalping_worker() [run_bot.py]
    |
    +-- Collecte donnees (M1/M5/M15/M30, ticks, MTF, OrderFlow, IRD, etc.)
    |
    +-- decision_pipeline.decide_scalp_action()  [decision_pipeline.py]
    |       |
    |       +-- Branche 1: TIMING_VETO -> HOLD
    |       +-- Branche 2/3: Regime check -> build_decision -> [Triple Filtre] -> PMA -> fusion_out
    |               |
    |               +-- apply_pma_adjustments()  [decision_pipeline.py]
    |
    +-- Dashboard/rapport (global_state, display_queue)
    |
    +-- Execution trade (burst guard + skeleton + trade_executor)
```
