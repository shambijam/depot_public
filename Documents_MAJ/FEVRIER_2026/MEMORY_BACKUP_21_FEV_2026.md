# Sniper X Dev - Key Learnings

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
- **Remaining debt**: `institutional_decision_pipeline` still called in `run_single_pipeline_cycle` — may be dead code
- **Remaining debt**: 3 burst guards duplicated in run_bot.py instead of 1 shared function

## Key Components
- `strategy/advanced_scoring.py`: `calculate_final_score()` — ALL scoring (bonus/malus)
- `core/decision_pipeline.py`: `decide_scalp_action()` — branches + vetos + decision
- `phase_observer/price_memory_analyzer.py`: MTF verdict, micro-resistance, trend analysis
- `phase_observer/institutional_reversal_detector.py`: 6-layer reversal detection (seuil 65)
- `phase_observer/market_fatigue_analyzer.py`: Fatigue detection (calibrated 16 FEV 2026)
- `phase_observer/market_physics_analyzer.py`: Physics bias + score (calibrated 16 FEV 2026)
- `phase_observer/detect_orderflow_v6/`: OrderFlow V6
- `phase_observer/market_analyzer.py`: PhaseObserver annotation only (build_decision removed)

## Separation des responsabilites (16 FEV 2026)
- `advanced_scoring.py` = SCORING UNIQUEMENT (calcul score, bonus, malus)
- `decision_pipeline.py` = VETOS + DECISION (BUY/SELL/HOLD) + BRANCHES + fusion_out
- `timing_analyzer.py` = VETOS TIMING (inchange)
- 3 analyseurs = DETECTION/ANALYSE (inchange)

## MTF Queen Rule V5 — MTF ALL-IN (20 FEV 2026)
- **Location**: `strategy/scalping.py` (INSIDE `_analyze_orderflow_v6()`)
- **REGLE ABSOLUE** : Le MTF decide la direction. TOUJOURS. Delta ne contredit JAMAIS.
- MTF BULLISH → BUY uniquement (delta bullish = +15 bonus, delta autre = pas de bonus)
- MTF BEARISH → SELL uniquement (delta bearish = +15 bonus, delta autre = pas de bonus)
- MTF NEUTRAL → **BLOQUE** (20 FEV 2026: delta ignoré, bias=NEUTRAL, pas de trade)
- **V3 ABANDONNEE** : delta bullish n'est PAS fiable contre MTF BEARISH (pullbacks 3-4 bougies)
- MTF direction via PMA verdict — **M30 SUPPRIMÉ** (20 FEV 2026), scalping sur M15+M5+M1 uniquement
- Poids : M15=0.50, M5=0.30, M1=0.20 | seuil=0.20 (weighted_score)
- **20 FEV 2026**: `alignment_count == 3` requis pour trade (max=3, pas 4). Sinon → NEUTRAL → HOLD.
  - `run_bot.py` : si `alignment_count < 3` → force `mtf_direction = "NEUTRAL"`
  - `scalping.py` MTF Queen V5 NEUTRAL branch : `result["bias"] = "NEUTRAL"` (delta ignore)
- **MTF multi-bougies** : M30=supprimé, M15=2 bougies, M5=5 bougies, M1=5 bougies FERMEES (pas 1 seule!)
- Seuil anti-bruit : 1 pip minimum pour direction

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
- Do NOT recreate FusionManager/fast-lane/SimpleAdvancedScorer code — removed

## Scoring — Composants actuels dans calculate_final_score() (advanced_scoring.py)
- MALUS_FATIGUE_EXHAUSTED: -25 | MALUS_FATIGUE: -15 | MALUS_BUYER/SELLER_FATIGUE: -10
- MALUS_PHYSICS_DEFICIT: -20 | MALUS_PHYSICS_CHAOS: -15 | MALUS_BARRIER: -10
- BONUS_PHYSICS_INERTIE: +10 (inertie alignée avec signal)
- BONUS_IRD_HIGH: +25 (score≥80) | BONUS_IRD: +15 (score≥65) | BONUS_IRD_MODERATE: +10 (score≥40)
- MALUS_IRD_OPPOSE_FORT: -20 (score≥60) | MALUS_IRD_OPPOSE: -10 (score≥40)
- MALUS_FATIGUE_CIRCUIT_BREAKER: -50 (inst_veto_fatigue)
- BONUS_MTF_4/4: +30 (alignment==4, legacy) | BONUS_MTF_3/4: +20 (alignment>=3) — NOTE: max réel = 3 (M30 supprimé)
- BONUS_FRESH_LEVEL: +10 (niveau frais < 2 pips)
- BONUS_TREND_CONSISTENCY: +5 (clarity≥0.7, strength≥0.6, aligné)
- MALUS_MICRO_RES: -30 (résistance STRONG/MODERATE < 1 pip pour BUY)
- MALUS_REGIME: -20 (range/accumulation/distribution, sauf exception IRD)
- BONUS_CONSENSUS_ALIGNED: +15 (3+ analyseurs go) | MALUS_CONSENSUS_BLOCKED: -15 (2+ stop)
- MTF fort aligné (alignment≥3 + signal ok) → facteur réduction malus physics/fatigue: ×0.6
- **CVD slope NON scoré directement** — capté indirectement via régime (gap identifié 21 FEV)

## Recommandations d'amélioration (rapport 21 FEV 2026)
Rapport complet : `Documents_MAJ/FEVRIER_2026/ANALYSE_RAPPORTS_VS_PROJET_21_FEV_2026.md`
- **[HAUTE]** Sizing asymétrique : risk_multiplier selon score_final + alignment_count (×1.5 sur Setup A score≥85+alignment==3+consensus ALIGNED)
- **[MOYENNE]** CVD slope bonus direct dans `advanced_scoring.py` : +8 si cvd_slope aligne signal, -5 si opposé (seuil ±0.3)
- **[MOYENNE]** Réévaluer seuil 3/3 obligatoire : tester si 2/3 avec M15 aligné a des perf. comparables
- **[MOYENNE]** SL/TP dynamique selon score : score≥88 + ALIGNED → tp_multiplier ×1.3
- **[FAIBLE]** Divergence CVD dédiée : exposer layer CVD_Divergence de l'IRD comme bonus explicite (+12)
- **NE PAS implémenter** Scénario 3 (rebond NEUTRAL + range) — décision architecturale MTF ALL-IN maintenue

## Git Conventions
- Commit messages: `Feature_name_increment` (e.g., `Correction_veto_reversal_1`)
- Branch: `dev` for development, `main` for production
- **NEVER commit** — l'utilisateur gere les commits lui-meme. Ne jamais proposer ou executer git commit.
