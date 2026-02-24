# Sniper X Dev - Key Learnings (22 FEV 2026)

## Architecture Overview
- **Main file**: `run_bot.py` (~3450 lines) - orchestrateur (collecte donnees + execution)
- **Decision file**: `core/decision_pipeline.py` (~2890 lines) - TOUTE la logique de decision
- **Scoring file**: `strategy/advanced_scoring.py` - TOUT le scoring (bonus/malus)
- **Decision flow**: MTF Verdict → OrderFlow V6 → Scoring → Timing Gatekeeper → `decide_scalp_action()`
  - Branche 1: TIMING_VETO (score < 85, timing blocks) → HOLD
  - Branche 2: OVERRIDE_VETO (score >= 85, timing blocks) → Triple Filter + Scoring + Vetos
  - Branche 3: PASS_NORMAL (timing OK, score < 85) → Scoring + Vetos
- MTF verdict computed BEFORE orderflow (12 FEV 2026) and passed via `asset_signals["mtf_direction"]`
- All shared data (mtf_verdict, inst_result, etc.) computed BEFORE branching in run_bot.py

## Scoring Centralization (16 FEV 2026)
- **`calculate_final_score()`** in `strategy/advanced_scoring.py` — centralizes ALL scoring
- **`apply_pma_adjustments()`** SUPPRIMEE (migrated to advanced_scoring)
- **`build_decision()`** SUPPRIMEE from market_analyzer.py (inlined in decision_pipeline)
- **`SimpleAdvancedScorer`** SUPPRIMEE (was buggy composite score)
- `decide_scalp_action()` takes `fatigue_result` + `physics_result` params
- `run_bot.py` extracts fatigue/physics from `of_v6_result['institutional_analysis']`
- Fatigue/Physics/IRD now have real scoring impact (were disconnected before)
- `calculate_unified_score()` kept as legacy for `scalping.py._score_candidate()`

## Code Cleanup History
- **FusionManager REMOVED** (12 FEV 2026, ~1456 lines): ALL DEAD CODE
- **Decision logic migrated** (15 FEV 2026, ~800 lines from run_bot.py → decision_pipeline.py)
- **Scoring centralized** (16 FEV 2026): apply_pma_adjustments, build_decision, SimpleAdvancedScorer removed
- **MicrostructureAnalyzer SUPPRIMÉ** (22 FEV 2026): module orphelin — `phase_observer/microstructure_analyzer.py` supprimé, import + bloc PHASE 2 retiré de scalping.py, champ `microstructure` retiré de types.py, méthodes mortes COUCHE 5 + config weight retiré de IRD
- **LiquidityHeatmap SUPPRIMÉ** (22 FEV 2026): module orphelin — `phase_observer/liquidity_heatmap.py` supprimé, import + bloc PHASE 2 (pressure_ratio, liquidity_grabs) retiré de scalping.py
- **5 orphelins SUPPRIMÉS** (22 FEV 2026): `utils.py`, `config.py`, `regime_resolver.py`, `regime_cache.py`, `regime_detector_lite.py` — ~1260 lignes de code mort. Import `regime_resolver` retiré de run_bot.py.
- **Remaining debt**: `institutional_decision_pipeline` still called in `run_single_pipeline_cycle` — may be dead code
- **Remaining debt**: 3 burst guards duplicated in run_bot.py instead of 1 shared function

## Key Components
- `strategy/advanced_scoring.py`: `calculate_final_score()` — ALL scoring (bonus/malus)
- `core/decision_pipeline.py`: `decide_scalp_action()` — branches + vetos + decision
- `phase_observer/price_memory_analyzer.py`: MTF verdict, micro-resistance, trend analysis
- `phase_observer/institutional_reversal_detector.py`: 6-layer reversal detection (seuil 65)
- `phase_observer/market_fatigue_analyzer.py`: Fatigue detection
- `phase_observer/market_physics_analyzer.py`: Physics bias + score
- `phase_observer/detect_orderflow_v6/`: OrderFlow V6
- `phase_observer/market_analyzer.py`: PhaseObserver annotation only (build_decision removed)

## Separation des responsabilites (16 FEV 2026)
- `advanced_scoring.py` = SCORING UNIQUEMENT (calcul score, bonus, malus)
- `decision_pipeline.py` = VETOS + DECISION (BUY/SELL/HOLD) + BRANCHES + fusion_out
- `timing_analyzer.py` = VETOS TIMING (inchange)
- 3 analyseurs = DETECTION/ANALYSE (inchange)

## Modules actifs dans phase_observer/ (après nettoyage 22 FEV 2026)
- `price_memory_analyzer.py` ✅ — MTF + scoring
- `market_fatigue_analyzer.py` ✅ — scoring complet
- `market_physics_analyzer.py` ✅ — scoring complet
- `institutional_reversal_detector.py` ✅ — IRD 6 couches
- `timing_analyzer.py` ✅ — vetos timing
- `orchestrator.py` ✅ — PhaseObserver core
- `market_analyzer.py` ✅ — wrapper MarketAnalyzer
- `detectors.py` ✅ — 4 méthodes actives (50% dead code interne)
- `memory.py` ✅ — PhaseMemoryManager
- `features.py` ✅ — clean_dataframe() seulement (~90% dead code interne)
- `types.py` ✅ — data types
- `detect_orderflow_v6/` ✅ — OrderFlow V6 complet

## MTF Queen Rule V5 — MTF ALL-IN (20 FEV 2026)
- **Location**: `strategy/scalping.py` (INSIDE `_analyze_orderflow_v6()`)
- **REGLE ABSOLUE** : Le MTF decide la direction. TOUJOURS. Delta ne contredit JAMAIS.
- MTF BULLISH → BUY uniquement | MTF BEARISH → SELL uniquement
- MTF NEUTRAL → **BLOQUE** (delta ignoré, bias=NEUTRAL, pas de trade)
- MTF direction via PMA verdict — **M30 SUPPRIMÉ** (20 FEV 2026), scalping sur M15+M5+M1 uniquement
- Poids : M15=0.50, M5=0.30, M1=0.20 | seuil=0.20 (weighted_score)
- `alignment_count == 3` requis pour trade (max=3). Sinon → NEUTRAL → HOLD.
  - `run_bot.py` : si `alignment_count < 3` → force `mtf_direction = "NEUTRAL"`
- **MTF multi-bougies** : M15=2 bougies, M5=5 bougies, M1=5 bougies FERMEES
- Seuil anti-bruit : 1 pip minimum pour direction

## Scoring complet — calculate_final_score() (22 FEV 2026)

**Base** : `score_final = orderflow_score + bonus_total - malus_total` (clampé 0–100)
**MTF malus factor** : si alignment==3 + direction alignée → tous les malus ×0.6

### §0 MTF factor
- `mtf_malus_factor = 0.6` si MTF 3/3 aligné avec signal, sinon `1.0`

### §2 FATIGUE (toutes valeurs × mtf_malus_factor)
- `market_state==EXHAUSTED` → -25
- `market_state==FATIGUED` → -15
- `buyer_fatigue HIGH` + BUY → -10
- `seller_fatigue HIGH` + SELL → -10
- `momentum_fatigue HIGH` (ATR/vol/body décroissants) → -15
- Absorption forte (prix stagne malgré volume) dans sens signal → -20

### §3 PHYSICS MALUS (toutes valeurs × mtf_malus_factor)
- `energy_deficit` → -20
- entropie `CHAOTIC` → -15
- Résistance < 1 pip + BUY → -10
- Support < 1 pip + SELL → -10
- `energy_required > average_energy×2` → -10

### §4 PHYSICS BONUS
- Inertie `likely_to_continue` alignée avec signal → +10

### §5 IRD
- IRD aligné, score≥80 → +25
- IRD aligné, score≥65 → +15
- IRD aligné, score≥40 → +10
- IRD opposé, score≥60 → -20
- IRD opposé, score≥40 → -10
- CVD Divergence régulière alignée (layer DIVERGENCE) → +12

### §6 Circuit breaker
- `inst_veto_fatigue` → -50

### §7 MTF
- Alignment==3 + direction alignée → +30

### §8 PMA Fresh level
- Niveau frais < 2 pips → +10

### §9 PMA Trend consistency
- clarity≥0.7 + strength≥0.6 + aligné → +5

### §10 Micro-résistance
- Résistance STRONG/MODERATE < 1 pip + bounce≥70% + BUY → -30

### §11 Régime
- range/accumulation/distribution (sauf exception IRD reversal MODERATE+) → -20

### §11b Delta momentum
- delta_momentum_score≥12 + aligné → +8
- delta_momentum_score≥12 + opposé → -5

### §12 Consensus
- 3+ analyseurs GO → +15
- 2+ analyseurs STOP → -15

## Améliorations implémentées — v8 (22 FEV 2026, branche v8_Dynamique_sltp)
- **Sizing asymétrique** : SETUP_A (score≥85+align==3+ALIGNED)→risk×1.5+tp×1.3+sl×1.2 | NORMAL (score≥70)→×1.0 | REDUCED→risk×0.75
  - `run_bot.py` : bloc après td["trade"] → td["risk_multiplier"], td["tp_multiplier"], td["sl_multiplier"], td["sizing_tier"]
  - `trader/order_builder.py` : applique risk_multiplier sur resolved_risk_pct
  - `trader/sltp.py` : applique tp_multiplier puis sl_multiplier
- **BONUS_DELTA_MOMENTUM**: +8 (delta≥12 aligné) | MALUS_DELTA_OPPOSE: -5 (opposé) — section 11b
- **BONUS_CVD_DIVERGENCE**: +12 — section 5, layer IRD "DIVERGENCE" regular.detected aligné
- **BONUS_MTF_3/3: +30** (corrigé 22 FEV — était +20 à cause du résidu alignment==4 jamais atteint)
- **CVD slope exposé** : `calculate_score_integrated()` expose cvd_slope/delta/vol_ratio dans summary → triple filtre decision_pipeline fonctionnel (cvd_aligned était toujours False avant)
- **MALUS_MOMENTUM_FATIGUE: -15** | **MALUS_ABSORPTION: -20** | **MALUS_ENERGY_BARRIER: -10** ajoutés

## Gaps restants (post-v8, 22 FEV 2026)
- **HVN/LVN scoring** : non implémenté, lié au Scénario 3 bloqué
- **Seuil MTF 2/3** : décision empirique — nécessite backtest
- **NE PAS implémenter** Scénario 3 (rebond NEUTRAL + range) — décision architecturale définitive

## Important Patterns
- `bars_cache.get_or_fetch()` for MT5 data (M1=50, M5=60, M15=10, M30=10 bars/ttl=120s)
- `orderflow_result_mini` dict is mutable — modifications in `decide_scalp_action()` visible in caller
- fatigue/physics extracted from `of_v6_result['institutional_analysis']` in run_bot.py

## Common Pitfalls
- MTF direction is imposed by PMA in run_bot.py - scalping.py just reads `asset_signals["mtf_direction"]`
- Do NOT add decision logic in run_bot.py — it belongs in `decision_pipeline.decide_scalp_action()`
- Do NOT add scoring logic in decision_pipeline — it belongs in `advanced_scoring.calculate_final_score()`
- Do NOT add local MTF calculations in scalping.py
- IRD needs sufficient data (50+ M5 bars for Changepoint, 30+ CVD values for Divergence)
- Do NOT recreate FusionManager/fast-lane/SimpleAdvancedScorer/MicrostructureAnalyzer/LiquidityHeatmap code — supprimés

## Doctrine — 3 Scénarios de trading
**Règle d'or : "PMA décide la DIRECTION — OrderFlow décide le TIMING"**
| Scénario | Fréquence | Configuration |
|---|---|---|
| **Trend Following** | 70% | MTF 3/3 + régime `trending` + score ≥ 70 |
| **Divergence CVD** | 20% | MTF 3/3 + régime `consolidation/breakout_potential` + divergence IRD |
| **Rebond range** | 10% | MTF NEUTRAL + range + HVN/LVN — **NON IMPLÉMENTÉ** |

## Git Conventions
- Commit messages: `Feature_name_increment` (e.g., `Correction_veto_reversal_1`)
- Branch: `v8_Dynamique_sltp` (actuelle) | `main` pour production
- **NEVER commit** — l'utilisateur gere les commits lui-meme.
