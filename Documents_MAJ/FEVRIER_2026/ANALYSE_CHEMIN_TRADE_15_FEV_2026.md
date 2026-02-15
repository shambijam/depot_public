# Analyse du Chemin de Trade - 15 Fevrier 2026

## Probleme identifie

En tracant le chemin complet d'un trade du debut a la fin, on decouvre que **la logique de decision de trade est dispersee dans `run_bot.py`** alors que le seul endroit ou un trade devrait etre decide est **`core/decision_pipeline.py`**.

`run_bot.py` ne devrait que :
- Collecter les donnees (bars, signals, MTF)
- Appeler `decision_pipeline.py` pour obtenir une decision
- Executer la decision recue

Au lieu de ca, `run_bot.py` contient sa propre logique de decision complete a l'interieur de `scalping_worker()`.

---

## Points de decision trouves dans run_bot.py

### Dans `scalping_worker()` (ligne ~1844) - 3 Branches

#### BRANCHE 1 : TIMING_VETO (lignes 2662-2686)
- **Condition** : `timing_blocks_trade=True` ET `score < 85`
- **Decision** : Force `action = "HOLD"` directement
- **Probleme** : La decision HOLD est codee en dur dans run_bot.py

#### BRANCHE 2 : OVERRIDE_VETO (lignes 2687-3188)
- **Condition** : `can_override_veto=True` (score >= 85)
- **Decisions dans run_bot.py** :
  - Phase regime check (lignes 2719-2743) → HOLD si RANGE/ACCUMULATION/DISTRIBUTION
  - `build_decision()` appele (ligne 2763) → BUY/SELL/HOLD
  - **Triple Filtre complet** code dans run_bot.py :
    - Filtre 1 - Delta Weighted (lignes 2850-2932) → direction BUY/SELL/HOLD
    - Filtre 2 - Microstructure (lignes 2934-2980) → pass/fail
    - Filtre 3 - Context (lignes 2982-2998) → pass/fail
  - Decision finale Triple Filtre (lignes 3000-3048) → BUY/SELL ou HOLD
  - PMA Adjustments (lignes 3054-3170) → bonus/malus + VETO_DUR + VETO_REVERSAL

#### BRANCHE 3 : PASS_NORMAL (lignes 3190-3406)
- **Condition** : `timing_blocks_trade=False` ET `score < 85`
- **Decision** : **CODE IDENTIQUE a la Branche 2** (copie-colle)
  - Phase regime check (lignes 3206-3231)
  - `build_decision()` (ligne 3252)
  - Triple Filtre (meme logique)
  - PMA Adjustments (lignes 3283-3343)

---

## Points d'execution (ou l'ordre est envoye)

| Lieu | Lignes | Fonction | Description |
|------|--------|----------|-------------|
| run_bot.py | 3594-3668 | `scalping_worker()` | Execution apres `fusion_out.ok=True` → `run_trade_execution_pipeline()` |
| run_bot.py | 1293-1520 | `run_single_pipeline_cycle()` | Boucle sur `scalping_decisions` → `run_trade_execution_pipeline()` |
| run_bot.py | 1502-1523 | `run_single_pipeline_cycle()` | Boucle sur `liquidity_decisions` → `_execute_single_decision()` |
| run_bot.py | 385-508 | `_execute_single_decision()` | `prepare_order()` → `execute_order()` |

---

## Le vrai probleme : duplication massive

### Code duplique entre Branche 2 et Branche 3
La logique suivante est **copiee-collee** entre les deux branches :
- Phase regime check
- `build_decision()`
- Triple Filtre (3 filtres)
- Decision finale
- PMA Adjustments
- Construction de `fusion_out`

### Decision dans run_bot.py au lieu de decision_pipeline.py
Toute cette logique de decision devrait etre dans `core/decision_pipeline.py` :
- **Triple Filtre** (Delta + Microstructure + Context)
- **PMA Adjustments** (bonus/malus, VETO_DUR, VETO_REVERSAL)
- **Phase regime check**
- **build_decision()**

`run_bot.py` ne devrait faire QUE :
1. Collecter les donnees (MTF, OrderFlow, bars)
2. Appeler `decision_pipeline.decide(data)` → recevoir BUY/SELL/HOLD
3. Executer si BUY/SELL

---

## Situation de decision_pipeline.py (core/)

`institutional_decision_pipeline()` (ligne 179 de decision_pipeline.py) est appele depuis `run_single_pipeline_cycle()` (ligne ~1005 de run_bot.py) mais :
- Les scalping_worker threads **ne l'utilisent PAS** — ils ont leur propre logique de decision
- Il produit `scalping_decisions` et `liquidity_decisions` mais c'est **largement contourne**
- C'est potentiellement du **code mort** a investiguer

---

## Schema du flux actuel (problematique)

```
scalping_worker() dans run_bot.py
  |
  +-- Collecte donnees (MTF, OrderFlow, bars) ✅ OK
  |
  +-- DECISION DANS RUN_BOT.PY ❌ MAUVAIS
  |     +-- Phase regime check
  |     +-- build_decision()
  |     +-- Triple Filtre (3 filtres)
  |     +-- PMA Adjustments + Vetos
  |     +-- Construction fusion_out
  |
  +-- Execution (run_trade_execution_pipeline) ✅ OK


run_single_pipeline_cycle() dans run_bot.py
  |
  +-- Appel decision_pipeline.institutional_decision_pipeline() ← potentiellement mort
  +-- Execution des decisions recues ✅ OK
```

## Schema du flux cible (correct)

```
scalping_worker() dans run_bot.py
  |
  +-- Collecte donnees (MTF, OrderFlow, bars) ✅
  |
  +-- decision = decision_pipeline.decide(data) ✅ Centralise
  |
  +-- Execution si BUY/SELL ✅


decision_pipeline.py
  |
  +-- Phase regime check
  +-- build_decision()
  +-- Triple Filtre (3 filtres)
  +-- PMA Adjustments + Vetos
  +-- Return BUY/SELL/HOLD
```

---

## Actions a mener

1. **Identifier** tout le code de decision dans `scalping_worker()` de run_bot.py
2. **Migrer** cette logique vers `decision_pipeline.py` dans une nouvelle methode
3. **Eliminer la duplication** entre Branche 2 et Branche 3 (meme logique)
4. **Clarifier** si `institutional_decision_pipeline()` est encore utilise ou s'il est mort
5. **run_bot.py** ne doit garder que : collecte de donnees + appel decision_pipeline + execution
