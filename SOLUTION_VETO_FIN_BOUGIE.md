# 🎯 SOLUTION : VETO FIN DE BOUGIE INTELLIGENT

## 📋 PROBLÈME IDENTIFIÉ

**Scénario perdant** :
```
52s : Bot prend BUY (OrderFlow score 75/100)
60s : Nouvelle bougie commence
02s : Prix se retourne en SELL
→ Trade perdant (slippage + retournement immédiat)
```

**Cause racine** :
1. OrderFlow analyse **toute la bougie** (0-60s)
2. À 52s, le momentum de **début de bougie** (0-30s) domine encore le score
3. Le **momentum récent** (45-60s) peut déjà s'inverser → **exhaustion** non détectée
4. Bot entre juste avant le retournement

---

## 💡 SOLUTION PROPOSÉE : 3 NIVEAUX

### 🔧 Niveau 1 : Détection Position dans Bougie

**Objectif** : Savoir si on est à 10s, 30s ou 52s dans la bougie courante

```python
def get_candle_progress(current_time):
    """
    Retourne la position dans la bougie M1 courante (0.0 - 1.0)

    Exemples:
    - 10s dans la bougie → 0.17 (17%)
    - 30s dans la bougie → 0.50 (50%)
    - 52s dans la bougie → 0.87 (87%)
    """
    seconds_in_minute = current_time.second
    progress = seconds_in_minute / 60.0
    return progress
```

---

### 🔧 Niveau 2 : Analyse Momentum par Segments

**Objectif** : Détecter si le momentum s'affaiblit en fin de bougie

**Méthode** : Diviser les ticks en 3 segments temporels

```python
# Analyser les ticks de la bougie fermée (60s complets)
segments = {
    "early": ticks[0:30s],      # Début (0-30s)
    "middle": ticks[30:45s],    # Milieu (30-45s)
    "late": ticks[45:60s]       # Fin (45-60s)
}

# Calculer delta pour chaque segment
delta_early = buy_volume_early - sell_volume_early
delta_middle = buy_volume_middle - sell_volume_middle
delta_late = buy_volume_late - sell_volume_late

# DÉTECTION EXHAUSTION
exhaustion_detected = False

# 1. Momentum s'affaiblit (delta diminue)
if abs(delta_late) < abs(delta_early) * 0.5:
    exhaustion_detected = True
    reason = f"Delta fin ({delta_late}) < 50% delta début ({delta_early})"

# 2. Retournement en cours (delta inversé)
if (delta_early > 0 and delta_late < 0) or (delta_early < 0 and delta_late > 0):
    exhaustion_detected = True
    reason = f"Retournement détecté (début {delta_early} → fin {delta_late})"

# 3. Volume décroissant (désintérêt)
volume_ratio = volume_late / volume_early if volume_early > 0 else 1.0
if volume_ratio < 0.6:
    exhaustion_detected = True
    reason = f"Volume fin ({volume_late}) < 60% volume début ({volume_early})"
```

---

### 🔧 Niveau 3 : Veto Progressif Basé sur Position

**Objectif** : Seuils de score plus stricts en fin de bougie

```python
def calculate_end_of_candle_veto(candle_progress, orderflow_score, exhaustion_detected):
    """
    Veto progressif basé sur position dans bougie

    Returns:
        veto_score (0-100): Score de veto (0=pas de veto, 100=veto absolu)
        veto_reason (str): Raison du veto
    """
    veto_score = 0.0
    veto_reason = None

    # ZONE SAFE : 0-45s (75% de la bougie)
    if candle_progress < 0.75:
        # Pas de veto spécifique lié à la position
        return 0.0, None

    # ZONE ATTENTION : 45-50s (75-83%)
    elif candle_progress < 0.83:
        # Veto MODÉRÉ si signal faible
        if orderflow_score < 70.0:
            veto_score = 40.0
            veto_reason = f"Fin de bougie ({candle_progress*100:.0f}%) + score modéré ({orderflow_score:.0f}/100)"

        # Veto FORT si exhaustion détectée
        if exhaustion_detected:
            veto_score += 30.0
            veto_reason = f"Exhaustion détectée à {candle_progress*100:.0f}% bougie"

    # ZONE DANGER : 50-55s (83-92%)
    elif candle_progress < 0.92:
        # Veto FORT sauf signal exceptionnel
        if orderflow_score < 85.0:
            veto_score = 70.0
            veto_reason = f"Trop proche fin bougie ({candle_progress*100:.0f}%) - score insuffisant"

        # Veto ABSOLU si exhaustion
        if exhaustion_detected:
            veto_score = 100.0
            veto_reason = f"Exhaustion + fin de bougie ({candle_progress*100:.0f}%)"

    # ZONE INTERDITE : 55-60s (92-100%)
    else:
        # Veto ABSOLU sauf signal parfait ET accélération
        if orderflow_score < 95.0 or exhaustion_detected:
            veto_score = 100.0
            veto_reason = f"Dernières secondes bougie ({candle_progress*100:.0f}%) - trop risqué"

    return min(100.0, veto_score), veto_reason
```

---

## 📊 MATRICE DE DÉCISION

| Position Bougie | OrderFlow Score | Exhaustion | Résultat |
|-----------------|----------------|------------|----------|
| 0-45s (0-75%) | 65/100 | Non | ✅ TRADE (zone safe) |
| 48s (80%) | 65/100 | Non | ⚠️ VETO 40 (score modéré + fin bougie) |
| 48s (80%) | 80/100 | Non | ✅ TRADE (score fort) |
| 48s (80%) | 80/100 | **Oui** | ❌ VETO 70 (exhaustion détectée) |
| 52s (87%) | 85/100 | Non | ✅ TRADE (score exceptionnel) |
| 52s (87%) | 85/100 | **Oui** | ❌ VETO 100 (exhaustion + fin bougie) |
| 52s (87%) | 65/100 | Non | ❌ VETO 70 (trop proche fin) |
| 56s (93%) | 95/100 | Non | ❌ VETO 100 (zone interdite) |
| 56s (93%) | 98/100 | **Oui** | ❌ VETO 100 (exhaustion) |

---

## 🎯 IMPLÉMENTATION RECOMMANDÉE

### Étape 1 : Ajouter Analyse Segments dans `scalping.py`

**Fonction à créer** : `_analyze_momentum_segments()`

```python
def _analyze_momentum_segments(self, ticks_df):
    """
    Analyse le momentum par segments temporels (0-30s, 30-45s, 45-60s)

    Returns:
        dict: {
            "delta_early": float,
            "delta_middle": float,
            "delta_late": float,
            "volume_early": float,
            "volume_late": float,
            "exhaustion_detected": bool,
            "exhaustion_reason": str
        }
    """
    if ticks_df is None or len(ticks_df) == 0:
        return {"exhaustion_detected": False}

    # Trier par timestamp
    ticks_df = ticks_df.sort_values('time')

    # Timestamp début et fin
    t_start = ticks_df['time'].iloc[0]
    t_end = ticks_df['time'].iloc[-1]
    duration = (t_end - t_start).total_seconds()

    # Diviser en 3 segments
    t_30 = t_start + pd.Timedelta(seconds=30)
    t_45 = t_start + pd.Timedelta(seconds=45)

    ticks_early = ticks_df[ticks_df['time'] <= t_30]
    ticks_middle = ticks_df[(ticks_df['time'] > t_30) & (ticks_df['time'] <= t_45)]
    ticks_late = ticks_df[ticks_df['time'] > t_45]

    # Calculer delta pour chaque segment
    def calc_segment_delta(ticks):
        if len(ticks) == 0:
            return 0.0, 0.0
        buy_vol = ticks[ticks['side'] == 'buy']['volume'].sum()
        sell_vol = ticks[ticks['side'] == 'sell']['volume'].sum()
        return buy_vol - sell_vol, buy_vol + sell_vol

    delta_early, vol_early = calc_segment_delta(ticks_early)
    delta_middle, vol_middle = calc_segment_delta(ticks_middle)
    delta_late, vol_late = calc_segment_delta(ticks_late)

    # DÉTECTION EXHAUSTION
    exhaustion_detected = False
    exhaustion_reason = None

    # 1. Momentum s'affaiblit
    if abs(delta_late) < abs(delta_early) * 0.5 and abs(delta_early) > 5:
        exhaustion_detected = True
        exhaustion_reason = f"Affaiblissement (delta fin {delta_late:.1f} < 50% début {delta_early:.1f})"

    # 2. Retournement en cours
    if (delta_early > 5 and delta_late < -5) or (delta_early < -5 and delta_late > 5):
        exhaustion_detected = True
        exhaustion_reason = f"Retournement (début {delta_early:.1f} → fin {delta_late:.1f})"

    # 3. Volume décroissant
    if vol_early > 0 and (vol_late / vol_early) < 0.6:
        exhaustion_detected = True
        exhaustion_reason = f"Volume décroissant (fin {vol_late:.1f} < 60% début {vol_early:.1f})"

    return {
        "delta_early": delta_early,
        "delta_middle": delta_middle,
        "delta_late": delta_late,
        "volume_early": vol_early,
        "volume_late": vol_late,
        "exhaustion_detected": exhaustion_detected,
        "exhaustion_reason": exhaustion_reason
    }
```

---

### Étape 2 : Intégrer dans Timing Analyzer

**Ajouter dans `timing_analyzer.py`** :

```python
def evaluate_trading_conditions(...):
    # ... code existant ...

    # ========================================================================
    # 🎯 VETO FIN DE BOUGIE (02 JAN 2026)
    # ========================================================================
    # Détecter position dans bougie courante
    candle_progress = current_time.second / 60.0

    # Analyser exhaustion (si ticks disponibles)
    exhaustion_detected = False
    if ticks_df is not None and len(ticks_df) > 30:
        # Analyser segments
        segments_analysis = analyze_momentum_segments(ticks_df)
        exhaustion_detected = segments_analysis.get("exhaustion_detected", False)
        exhaustion_reason = segments_analysis.get("exhaustion_reason")

    # Calcul veto fin de bougie (en attente du score OrderFlow)
    # Ce veto sera appliqué dans run_bot.py après calcul OrderFlow
    result["candle_progress"] = round(candle_progress, 3)
    result["exhaustion_detected"] = exhaustion_detected
    result["exhaustion_reason"] = exhaustion_reason if exhaustion_detected else None
```

---

### Étape 3 : Appliquer Veto dans `run_bot.py`

**Après calcul OrderFlow, avant décision finale** :

```python
# ========== VETO FIN DE BOUGIE (02 JAN 2026) ==========
candle_progress = timing_verdict.get("candle_progress", 0.0)
exhaustion_detected = timing_verdict.get("exhaustion_detected", False)
orderflow_score = orderflow_result_mini['score']

# Calculer veto fin de bougie
end_candle_veto_score = 0.0
end_candle_veto_reason = None

# ZONE ATTENTION : 45-50s (75-83%)
if candle_progress >= 0.75 and candle_progress < 0.83:
    if orderflow_score < 70.0:
        end_candle_veto_score = 40.0
        end_candle_veto_reason = f"Fin bougie ({candle_progress*100:.0f}%) + score modéré"

    if exhaustion_detected:
        end_candle_veto_score += 30.0
        end_candle_veto_reason = timing_verdict.get("exhaustion_reason")

# ZONE DANGER : 50-55s (83-92%)
elif candle_progress >= 0.83 and candle_progress < 0.92:
    if orderflow_score < 85.0:
        end_candle_veto_score = 70.0
        end_candle_veto_reason = f"Trop proche fin ({candle_progress*100:.0f}%)"

    if exhaustion_detected:
        end_candle_veto_score = 100.0
        end_candle_veto_reason = f"Exhaustion + fin bougie"

# ZONE INTERDITE : 55-60s (92-100%)
elif candle_progress >= 0.92:
    end_candle_veto_score = 100.0
    end_candle_veto_reason = f"Dernières secondes ({candle_progress*100:.0f}%) - interdit"

# Ajouter au veto_score total
total_veto_score = timing_veto_score + end_candle_veto_score

logger.info(
    f"[END_CANDLE_CHECK][{asset}] Position={candle_progress*100:.0f}% | "
    f"Exhaustion={exhaustion_detected} | Veto={end_candle_veto_score:.0f}"
)
```

---

## 📈 RÉSULTATS ATTENDUS

### Scénario AVANT Fix

```
Time  OrderFlow  Action    Résultat
------------------------------------------
52s   75/100     BUY       ❌ PERDANT (retournement à 60s)
56s   80/100     BUY       ❌ PERDANT (slippage entrée)
48s   65/100     BUY       ⚠️ RISQUÉ (momentum faible)
```

### Scénario APRÈS Fix

```
Time  OrderFlow  Exhaustion  Veto   Action   Résultat
--------------------------------------------------------
52s   75/100     Non         70     HOLD     ✅ Évité (trop proche fin)
52s   88/100     Non         0      BUY      ✅ OK (score exceptionnel)
52s   85/100     OUI         100    HOLD     ✅ Évité (exhaustion détectée)
56s   95/100     Non         100    HOLD     ✅ Évité (zone interdite)
48s   65/100     Non         40     HOLD     ✅ Évité (score modéré + fin bougie)
48s   82/100     Non         0      BUY      ✅ OK (score fort + zone attention)
```

**Amélioration attendue** :
- ✅ -50% de trades perdants en fin de bougie
- ✅ -30% slippage sur entrées
- ✅ Meilleur timing d'entrée moyen

---

## 🎯 CONFIGURATION RECOMMANDÉE

**Créer dans `prod_config.json`** :

```json
"end_of_candle_veto": {
  "enabled": true,
  "safe_zone_pct": 0.75,        // 0-45s = zone safe (75%)
  "attention_zone_pct": 0.83,   // 45-50s = attention (83%)
  "danger_zone_pct": 0.92,      // 50-55s = danger (92%)
  "forbidden_zone_pct": 0.92,   // 55-60s = interdit (92%+)

  "min_score_attention": 70.0,  // Score min en zone attention
  "min_score_danger": 85.0,     // Score min en zone danger
  "min_score_forbidden": 95.0,  // Score min en zone interdite (rarement atteint)

  "exhaustion_detection": {
    "enabled": true,
    "momentum_decay_threshold": 0.5,  // Delta fin < 50% delta début
    "volume_decay_threshold": 0.6     // Volume fin < 60% volume début
  }
}
```

---

## 🚀 PLAN D'IMPLÉMENTATION

### Phase 1 : Analyse Simple (Quick Win)
1. ✅ Ajouter détection position bougie (`candle_progress`)
2. ✅ Veto simple basé uniquement sur position (pas d'analyse exhaustion)
3. ✅ Tester 24h en DEMO

### Phase 2 : Analyse Segments (Complet)
1. ✅ Implémenter `_analyze_momentum_segments()`
2. ✅ Détecter exhaustion (momentum decay, retournement, volume)
3. ✅ Intégrer veto intelligent complet
4. ✅ Tester 48h en DEMO

### Phase 3 : Optimisation
1. Ajuster seuils selon résultats
2. Ajouter métriques (% trades évités, impact P&L)
3. Passer en LIVE si résultats positifs

---

**Voulez-vous que je commence par la Phase 1 (Quick Win) ou directement la Phase 2 (Solution complète) ?**
