# Session 15 Fevrier 2026 - Suppression Fast-Lane & Nettoyage

## Resume

Nettoyage majeur de `run_bot.py` : suppression de tout le code mort lie a FusionManager et a la fast-lane d'execution, ainsi que suppression du dossier `backup_broken/`.

**Commit**: `e7a1a59` - `Supression_fast-lane_run-bot_1`
**Impact**: 170 fichiers modifies, **93 554 lignes supprimees**, 5 lignes ajoutees

---

## 1. Suppression Fast-Lane dans run_bot.py

### Avant / Apres
| Metrique | Avant | Apres |
|----------|-------|-------|
| Lignes `run_bot.py` | 5 848 | 4 397 |
| Lignes supprimees | - | 1 456 |

### Code supprime dans run_bot.py

#### Fonctions supprimees
- `_quick_vote_fusion()` : Vote rapide de secours pour FusionManager (spread guard, footprint, scoring combine)
- `_scale100()` : Normalisation 0-100 pour les inputs Fusion
- `_mk_fusion_inputs()` : Construction des inputs pour FusionManager (signals, footprint, orderflow)
- `_fusion_applies()` : Verification si un asset est eligible pour Fusion
- Fonctions internes : `_dig()`, `_safe_float()`, `_fmt_float()`, `_open_burst_ids()`, `_score_fd()`, `_resolve()`

#### Variables / Constantes supprimees
- `_fusion_mgr = None` (initialisation FusionManager desactivee)
- `FUSION_ASSETS` : Liste des assets eligibles Fusion
- `fusion_scalping_decisions` : Dictionnaire de decisions Fusion par asset
- References aux configs `entry_rules.scalping.fusion.*`

#### Logique supprimee
- **Fast-lane d'execution** : Boucle qui executait les trades directement depuis FusionManager sans passer par `decision_pipeline`
- **Gardes fast-lane** : Burst guards, slippage checks, basket checks specifiques a la fast-lane
- **Prix d'ancrage** : Calcul du prix de reference pour execution fast-lane
- **Trigger type** `fusion_pretrigger` / `fusion_scalping`
- **TTL Fusion** : Expiration des decisions Fusion (`fusion.ttl_ms`)

#### Docstring mise a jour
La docstring de `run_single_pipeline_cycle()` a ete mise a jour pour refleter la nouvelle architecture :
```
Architecture (12 FEV 2026):
  - MarketAnalyzer (PhaseObserver + PatternEngine) -> annotated_df + latest
  - decision_pipeline (institutional_decision_pipeline) pour scalping + liquidity
  - SLTP dynamique (trailing-only pour scalping), maintenance periodique
  - Note: FusionManager/fast-lane supprimes -- scalping_worker threads gerent tout
```

---

## 2. Suppression du dossier backup_broken/

Suppression complete du dossier `backup_broken/` qui contenait une copie obsolete de tout le projet (169 fichiers, ~92 000 lignes).

### Contenu supprime
- **Code source** : Copie complete de tous les modules (core/, phase_observer/, strategy/, trader/, orchestration/, utils/)
- **Configs** : Copies des fichiers JSON de configuration
- **Documentation** : Copies des fichiers MD de Decembre 2025 et Janvier 2026
- **Assets** : Images PNG (sniper_left.png, sniper_right.png)

---

## 3. Architecture actuelle apres nettoyage

Le flux de trading dans `run_bot.py` est maintenant :

```
run_single_pipeline_cycle()
  |
  +-> MarketAnalyzer (PhaseObserver + PatternEngine)
  |     -> annotated_df + latest
  |
  +-> institutional_decision_pipeline()
  |     -> decisions scalping + liquidity
  |
  +-> SLTP dynamique + maintenance periodique
```

Les trades scalping sont entierement geres par les **scalping_worker threads** via :
- MTF Verdict (PMA) -> OrderFlow V6 -> Timing Gatekeeper -> 3 Branches

**FusionManager et fast-lane ne sont plus necessaires** car les scalping_worker threads prennent leurs propres decisions de trading de maniere autonome.

---

## 4. Dette technique restante

- `institutional_decision_pipeline` (core/decision_pipeline.py) est toujours appele dans `run_single_pipeline_cycle()` — potentiellement du code mort a investiguer
- 3 burst guards dupliques dans run_bot.py au lieu d'une fonction partagee
- Le dossier `core/` (decision_pipeline.py, strategy_manager.py) est largement contourne par les scalping_worker threads
