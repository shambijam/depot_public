# FIX - Scoring OrderFlow V6 Non Transmis au FusionManager

**Date** : 2 Décembre 2025
**Impact** : 🔴 CRITIQUE - Scoring toujours à 0
**Statut** : En cours d'analyse

---

## 🔍 DIAGNOSTIC COMPLET

### Symptôme

```
📈 ORDERFLOW ANALYSIS : 20.0/50 points  ✅ CALCULÉ
👣 FOOTPRINT ANALYSIS : 12.0/30 points  ✅ CALCULÉ
⚡ TRIGGERS DETECTION : 0.0/20 points   ✅ CALCULÉ
🎯 SCORE FINAL : 13.6/100 points        ✅ CALCULÉ
```

**MAIS** :
```
[SIMPLE_SCORE] OF=0.000 FP=0.000 base=0.000  ❌ FusionManager ne reçoit rien
```

---

## 📊 FLUX DE DONNÉES ACTUEL

```
┌──────────────────────────────────────────────────────────────┐
│ 1. market_analyzer.analyze() (run_bot.py:1181)              │
│    ↓                                                          │
│    Retourne market_results avec:                             │
│    - annotated_df                                            │
│    - latest (footprint_score, footprint_status, ...)         │
│    - patterns: {orderflow: {}} ← VIDE !                     │
└──────────────────────────────────────────────────────────────┘
                         ↓
┌──────────────────────────────────────────────────────────────┐
│ 2. Hardcoded OrderFlow à 0 (run_bot.py:1362-1364)           │
│                                                              │
│    latest["orderflow_score"] = 0        ❌                   │
│    latest["orderflow_status"] = "N/A"   ❌                   │
│    latest["orderflow_summary"] = {}     ❌                   │
└──────────────────────────────────────────────────────────────┘
                         ↓
┌──────────────────────────────────────────────────────────────┐
│ 3. Construction signals (run_bot.py:1374)                    │
│                                                              │
│    signals = _build_asset_trading_signals(latest, ...)      │
│    signals["orderflow_summary"] = latest.get(...)  ← 0/N/A  │
└──────────────────────────────────────────────────────────────┘
                         ↓
┌──────────────────────────────────────────────────────────────┐
│ 4. Construction inputs FusionManager (run_bot.py:1447)      │
│                                                              │
│    of, fp, trig, ... = _mk_fusion_inputs(signals, latest)   │
│                                                              │
│    orderflow = {                                             │
│        "score": latest.get("orderflow_score"),  ← 0 !       │
│        "status": latest.get("orderflow_status"), ← "N/A" !  │
│        "summary": latest.get("orderflow_summary") ← {} !    │
│    }                                                         │
└──────────────────────────────────────────────────────────────┘
                         ↓
┌──────────────────────────────────────────────────────────────┐
│ 5. FusionManager.fuse() (run_bot.py:1451)                   │
│                                                              │
│    fusion_mgr.fuse(orderflow=of, footprint=fp, ...)         │
│    ↓                                                         │
│    _normalize_orderflow(of) → score = 0/100 = 0.000         │
│    ↓                                                         │
│    base_score = (OF + FP) / 2 = (0.000 + 0.800) / 2 = 0.400 │
│    ↓                                                         │
│    [SIMPLE_SCORE] OF=0.000 FP=0.800 base=0.400  ❌           │
└──────────────────────────────────────────────────────────────┘
                         ↓
┌──────────────────────────────────────────────────────────────┐
│ 6. Decision Pipeline (PLUS TARD)                             │
│                                                              │
│    ScalpingStrategy.evaluate_entry() est appelé              │
│    ↓                                                         │
│    _analyze_orderflow_v6() calcule le score (20/50 pts)  ✅ │
│    _analyze_footprint_v6() calcule le score (12/30 pts) ✅  │
│    _analyze_triggers_v6() calcule le score (0/20 pts)   ✅  │
│    ↓                                                         │
│    Rapport consolidé affiché (13.6/100)                  ✅ │
│    ↓                                                         │
│    Score stocké dans sm_decision["meta"]["orderflow_v6"]    │
│    MAIS jamais retourné vers FusionManager !  ❌             │
└──────────────────────────────────────────────────────────────┘
```

---

## 🎯 PROBLÈME ROOT CAUSE

### Rupture du Flux de Données

1. **OrderFlow V6 est calculé dans `ScalpingStrategy._analyze_orderflow_v6()`** (strategy/scalping.py:85-371)
   - Retourne un dict avec `total_score`, `delta_momentum_score`, etc.
   - Appelé depuis `evaluate_entry()` ligne 1245

2. **MAIS ce calcul arrive TROP TARD** :
   - FusionManager est appelé ligne 1451 de run_bot.py
   - `evaluate_entry()` est appelé APRÈS dans le decision_pipeline
   - Les données ne peuvent pas "remonter" vers FusionManager

3. **Ancienne architecture (28 Nov 2025)** :
   - OrderFlow V6 était calculé dans `market_analyzer.py`
   - Retourné dans `market_results["patterns"]["orderflow"]`
   - FusionManager pouvait le lire

4. **Nouvelle architecture** :
   - OrderFlow V6 déplacé dans `ScalpingStrategy` pour rapport consolidé
   - Mais le pont vers FusionManager n'a pas été créé
   - `market_results["patterns"]["orderflow"]` est maintenant vide (ligne 243 market_analyzer.py)

---

## ✅ SOLUTION PROPOSÉE

### Option A : Calculer OrderFlow V6 AVANT FusionManager

**Principe** : Appeler `_analyze_orderflow_v6()` depuis `run_bot.py` juste après `market_analyzer.analyze()`, stocker le résultat dans `latest`, puis FusionManager pourra le lire.

**Modifications** :

**1. Ajouter méthode publique dans ScalpingStrategy**

```python
# strategy/scalping.py (nouvelle méthode)
def calculate_orderflow_v6_standalone(
    self,
    asset: str,
    df_m1: pd.DataFrame,
    df_m5: Optional[pd.DataFrame],
    df_m15: Optional[pd.DataFrame],
    asset_signals: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Calcule OrderFlow V6 en mode standalone (pour FusionManager).

    Retourne format compatible FusionManager:
    {
        "score": 0-100,
        "status": "VALID"/"SUSPECT",
        "summary": {...},
        "total_score": 0-50,  # Score interne
        "details": {...}
    }
    """
    # Appeler _analyze_orderflow_v6() existante
    result = self._analyze_orderflow_v6(
        asset=asset,
        df_m1=df_m1,
        df_m5=df_m5,
        df_m15=df_m15,
        asset_signals=asset_signals
    )

    # Convertir en format FusionManager
    total_score = result.get("total_score", 0.0)  # 0-50
    score_pct = (total_score / 50.0) * 100.0  # Convertir en 0-100

    # Extraire détails pour summary
    details = result.get("details", {})
    delta_details = details.get("delta_momentum", {})
    volume_details = details.get("volume_confirmation", {})
    imbalance_details = details.get("imbalance_strength", {})

    summary = {
        "delta_total": delta_details.get("delta_total", 0.0),
        "bias": "BUY" if delta_details.get("delta_total", 0) > 0 else "SELL" if delta_details.get("delta_total", 0) < 0 else "NEUTRAL",
        "poc": volume_details.get("poc"),
        "imbalance_count": imbalance_details.get("m1_count", 0),
        "volume_ratio": volume_details.get("ratio", 1.0),
        "mtf_alignment": result.get("mtf_alignment", {})
    }

    # Status selon qualité
    if total_score >= 30.0:  # 60% de 50
        status = "VALID"
    elif total_score >= 15.0:  # 30% de 50
        status = "WEAK"
    else:
        status = "SUSPECT"

    return {
        "score": score_pct,  # 0-100 pour FusionManager
        "status": status,
        "summary": summary,
        "total_score": total_score,  # 0-50 pour rapport consolidé
        "details": details
    }
```

**2. Appeler depuis run_bot.py AVANT FusionManager**

```python
# run_bot.py (après ligne 1360, AVANT ligne 1443)

# === Calcul OrderFlow V6 pour FusionManager ===
try:
    if asset == "XAUUSD":  # Ou condition selon config
        # Récupérer DataFrames multi-timeframe
        df_m1 = annotated_rates_df  # Déjà disponible
        df_m5 = None
        df_m15 = None

        # Charger M5/M15 si nécessaire
        try:
            import MetaTrader5 as mt5
            df_m5 = mt5_connector.get_rates(asset, mt5.TIMEFRAME_M5, count=20)
            df_m15 = mt5_connector.get_rates(asset, mt5.TIMEFRAME_M15, count=15)
        except Exception as e:
            logger.debug(f"[OF V6] Impossible charger M5/M15: {e}")

        # Calculer OrderFlow V6
        scalping_strategy = strategy_manager.strategies.get("scalping")
        if scalping_strategy:
            of_v6_result = scalping_strategy.calculate_orderflow_v6_standalone(
                asset=asset,
                df_m1=df_m1,
                df_m5=df_m5,
                df_m15=df_m15,
                asset_signals=signals
            )

            # Stocker dans latest (remplace le hardcode ligne 1362-1364)
            latest["orderflow_score"] = of_v6_result.get("score", 0.0)
            latest["orderflow_status"] = of_v6_result.get("status", "SUSPECT")
            latest["orderflow_summary"] = of_v6_result.get("summary", {})

            logger.info(
                f"[OF V6] {asset} → Score={of_v6_result['score']:.1f}/100 "
                f"({of_v6_result['total_score']:.1f}/50 pts) | "
                f"Status={of_v6_result['status']}"
            )
except Exception as e:
    logger.error(f"[OF V6] Erreur calcul OrderFlow V6: {e}", exc_info=True)
    # Garder les valeurs par défaut (0/N/A)
```

**3. Supprimer le hardcode ligne 1362-1364**

```python
# AVANT (run_bot.py:1362-1364)
latest["orderflow_score"] = 0
latest["orderflow_status"] = "N/A"
latest["orderflow_summary"] = {}

# APRÈS (lignes supprimées ou commentées)
# OrderFlow V6 maintenant calculé au-dessus
```

---

### Option B : Passer les Résultats via asset_signals

**Principe** : Faire en sorte que `_analyze_orderflow_v6()` stocke ses résultats dans `asset_signals`, qui est passé ensuite à FusionManager.

**Problème** : `asset_signals` est construit AVANT l'appel à OrderFlow V6, donc nécessite refactoring plus important.

---

### Option C : Dual Scoring (Actuel + Nouveau)

**Principe** : Garder le rapport consolidé dans `ScalpingStrategy` ET calculer OrderFlow V6 dans `market_analyzer`.

**Problème** : Code dupliqué, maintenance difficile.

---

## 🎯 RECOMMANDATION

**Option A** est la meilleure car :
- ✅ Réutilise le code existant `_analyze_orderflow_v6()`
- ✅ Pas de duplication
- ✅ FusionManager reçoit les vraies données
- ✅ Rapport consolidé continue de fonctionner
- ✅ Flux clair et maintenable

---

## 📋 PLAN D'IMPLÉMENTATION

1. ✅ Créer `calculate_orderflow_v6_standalone()` dans `strategy/scalping.py`
2. ✅ Appeler depuis `run_bot.py` après `market_analyzer.analyze()`
3. ✅ Stocker résultat dans `latest`
4. ✅ Supprimer hardcode ligne 1362-1364
5. ✅ Tester que FusionManager reçoit les scores
6. ✅ Vérifier logs `[SIMPLE_SCORE] OF=20.0 FP=12.0 base=16.0`

---

*Document créé le 2 Décembre 2025*
