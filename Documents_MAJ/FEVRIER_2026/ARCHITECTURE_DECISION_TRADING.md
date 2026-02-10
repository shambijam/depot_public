# ARCHITECTURE DECISION TRADING - Sniper X

## 1. Chaine d'execution (qui fait quoi)

| Composant | Role | Fichier |
|-----------|------|---------|
| ScalpingStrategy._analyze_orderflow_v6() | **L'ANALYSTE** - decide BUY/SELL/NEUTRAL via delta + MTF | `strategy/scalping.py` |
| build_decision() | CONVERTISSEUR trivial (passe-plat score→action) | `phase_observer/market_analyzer.py` |
| run_bot.py | **CHEF D'ORCHESTRE** - charge donnees, branches, PMA, vetos | `run_bot.py` |
| trade_executor | EXECUTEUR PUR - zero intelligence, execute l'ordre | `trader/trade_executor.py` |
| pipeline.py | Wrapper alternatif pour evaluate_entry | `strategy/pipeline.py` |
| decision_pipeline | Pipeline institutionnel secondaire | `core/decision_pipeline.py` |

### Flux simplifie

```
MT5 → bars_cache (M1/M5/M15) → run_bot.py
                                     │
                                     ├─ scalping_strategy._analyze_orderflow_v6()
                                     │   ├─ Delta M1 → direction (bullish/bearish/neutral)
                                     │   ├─ MTF alignment (M1/M5 + M15 optionnel)
                                     │   ├─ MTF QUEEN VETO → refuse BUY si M5+M1 pas BULLISH
                                     │   └─ Score progressif 0-100 + bias BUY/SELL/NEUTRAL
                                     │
                                     ├─ price_memory_analyzer → mtf_verdict, micro-resistance
                                     ├─ institutional_reversal_detector → veto reversal/fatigue
                                     │
                                     ├─ Timing Gatekeeper → 3 Branches
                                     │   ├─ Branche 1: TIMING_VETO → HOLD
                                     │   ├─ Branche 2: OVERRIDE_VETO → Triple Filter + PMA
                                     │   └─ Branche 3: PASS_NORMAL → build_decision + PMA
                                     │
                                     └─ trade_executor.execute()
```

## 2. Le probleme fondamental (AVANT correction 10 FEV 2026)

### Symptome
Le bot prenait des BUY en pleine marche bearish malgre des corrections MTF multiples.

### Cause racine

**`scalping.py` ligne 1022** : Le bias etait decide sur le delta M1 SEUL.
```python
# AVANT (casse):
if delta_direction == "bullish":
    result["bias"] = "BUY"  # ← Decide BUY sans regarder M5/M15
```

**`run_bot.py` ligne 3750** : `df_m5=None` → M5 jamais envoye a la strategie.
```python
# AVANT (casse):
of_v6_result = scalping_strategy._analyze_orderflow_v6(
    asset=asset,
    df_m1=rates_df_fresh,
    df_m3=None,
    df_m5=None,          # ← M5 JAMAIS passe!
    asset_signals=asset_signals_for_of
)
```

### Deux systemes MTF paralleles qui ne se parlaient pas

| Systeme | Fichier | Timeframes | Utilise par |
|---------|---------|------------|-------------|
| price_memory_analyzer | `phase_observer/price_memory_analyzer.py` | M15 + M5 + M1 | run_bot.py (verdict, malus) |
| scalping.py MTF | `strategy/scalping.py` | M1 + M3 (M5=None!) | _analyze_orderflow_v6 |

Resultat : La strategie voyait M1 bullish (1 bougie verte) et emettait BUY.
Toutes les corrections MTF dans run_bot.py tentaient de rattraper APRES COUP.

### Corrections MTF redondantes supprimees (10 FEV 2026)

| Correction | Lignes | Pourquoi inefficace |
|-----------|--------|---------------------|
| MALUS_CONTRE_MTF (-35pts) | apply_pma_adjustments() | Requiert alignment_count >= 2, echoue quand M5+M1 verts |
| Triple filter MTF (Branche 2) | ~4333-4398 | Re-check M5+M1 APRES que la strategie a decide |
| M5M1_BLOCK (Branche 3) | ~4734-4765 | Idem, APRES la decision, uniquement Branche 3 |

## 3. La solution (10 FEV 2026 - MTF Queen)

### Principe
Mettre la regle MTF DANS `scalping.py` AVANT la decision de bias.
La strategie refuse elle-meme d'emettre un BUY en bearish.

```
AVANT (casse):
  scalping.py : delta M1 → BUY (sans voir M5/M15)
  run_bot.py  : tente de bloquer apres coup (echoue)

APRES (correct):
  run_bot.py  : passe M5 + M15 a la strategie
  scalping.py : delta M1 + check M5+M1 alignes → BUY ou NEUTRAL
  run_bot.py  : fait confiance a la strategie, plus de doublons MTF
```

### Modifications

1. **run_bot.py** : `df_m5=rates_df_m5` (au lieu de None) + `df_m15` via asset_signals
2. **scalping.py** : MTF Queen Rule - VETO si M5+M1 pas alignes avec delta
3. **run_bot.py** : Suppression MALUS_CONTRE_MTF, Triple filter MTF, M5M1_BLOCK

### Elements conserves

| Element | Raison |
|---------|--------|
| Chargement M5/M15 (lignes 3488-3510) | Necessaire pour passer a la strategie |
| Verdict MTF pondere (PMA) | Utile pour logging Telegram |
| MALUS_MICRO_RES (-30pts) | Protection micro-resistance, pas MTF |
| MALUS_REGIME (-20pts) | Protection range/accumulation |
| MALUS_FATIGUE (-50pts) | Circuit breaker fatigue |
| VETO_REVERSAL | Detection retournement institutionnel |
| VETO_FATIGUE | Circuit breaker marche epuise |

## 4. Regles MTF Queen (scalping.py)

### Veto
- **BUY** : Requiert M5 BULLISH ET M1 BULLISH (sinon → NEUTRAL)
- **SELL** : Requiert M5 BEARISH ET M1 BEARISH (sinon → NEUTRAL)

### Bonus 3/3
- Si M15 + M5 + M1 alignes dans la meme direction → +15 pts bonus

### Champ de sortie
- `result["mtf_veto"]` : True si le signal a ete bloque par MTF
- `result["mtf_3_3"]` : True si 3 timeframes alignes (bonus applique)
