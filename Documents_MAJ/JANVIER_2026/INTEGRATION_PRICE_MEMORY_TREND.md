# 🧠 INTÉGRATION PRICE MEMORY TREND - 08 JAN 2026

## ✅ FONCTION IMPLÉMENTÉE

La fonction `analyze_trend_structure()` est maintenant disponible dans `PriceMemoryAnalyzer`.

### Ce qu'elle fait:
1. **Détecte Higher Highs / Higher Lows** → BULLISH
2. **Détecte Lower Highs / Lower Lows** → BEARISH
3. **Calcule le déplacement net** (+30 pips, -25 pips)
4. **Mesure la clarté de tendance** (0-1.0)
5. **Retourne direction + force** (BULLISH 0.85, BEARISH 0.70, RANGE 0.30)

---

## 📍 OÙ L'APPELER DANS LE CODE

### EMPLACEMENT 1: Dans `run_bot.py` - Thread Scalping

**Ligne ~3450** (juste après le chargement des données de marché):

```python
# ========== ANALYSE PRICE MEMORY TREND (08 JAN 2026) ==========
try:
    from phase_observer.price_memory_analyzer import PriceMemoryAnalyzer

    price_memory_analyzer = PriceMemoryAnalyzer(logger=logger)

    # Appeler l'analyse de tendance
    trend_structure = price_memory_analyzer.analyze_trend_structure(
        historical_data=df_m1,
        current_price=latest_candle.get('close', 0.0)
    )

    # Stocker dans une variable pour usage ultérieur
    memory_trend_direction = trend_structure['trend_direction']  # BULLISH/BEARISH/RANGE
    memory_trend_strength = trend_structure['trend_strength']    # 0.0-1.0
    memory_net_pips = trend_structure['net_displacement']['net_pips']
    memory_net_direction = trend_structure['net_displacement']['net_direction']

    logger.info(
        f"[PRICE_MEMORY_TREND][{asset}] {memory_trend_direction} "
        f"(strength={memory_trend_strength:.2f}) | "
        f"Net: {memory_net_direction} {memory_net_pips:+.1f} pips"
    )

except Exception as e_memory_trend:
    logger.error(f"[{asset}] Erreur Price Memory Trend: {e_memory_trend}")
    memory_trend_direction = "RANGE"
    memory_trend_strength = 0.0
    memory_net_pips = 0.0
    memory_net_direction = "FLAT"
```

---

## 🎯 INTÉGRATION DANS LA DÉCISION FINALE

### EMPLACEMENT 2: Après le calcul du score composite

**Ligne ~3650** (avant la décision finale):

```python
# ═══════════════════════════════════════════════════════════════
# 🧠 VETO/BOOST basé sur Price Memory Trend (08 JAN 2026)
# ═══════════════════════════════════════════════════════════════

# Extraire la direction du signal OrderFlow
signal_direction = decision_mini.get("action", "HOLD")  # BUY/SELL/HOLD
of_score = orderflow_result_mini.get("score", 0.0)

# Appliquer VETO ou BOOST selon alignement
if signal_direction in ["BUY", "SELL"]:

    # CAS 1: Signal CONTRE tendance forte → VETO
    if signal_direction == "BUY" and memory_trend_direction == "BEARISH":
        if memory_trend_strength > 0.70:
            logger.warning(
                f"[PRICE_MEMORY_VETO][{asset}] BUY contre tendance BEARISH forte "
                f"(strength={memory_trend_strength:.2f}, net={memory_net_pips:+.1f} pips)"
            )
            decision_mini["action"] = "HOLD"
            decision_mini["rationale"] = f"VETO: BUY contre tendance BEARISH ({memory_net_pips:+.1f} pips)"
            decision_mini["confidence"] = 0.0

    elif signal_direction == "SELL" and memory_trend_direction == "BULLISH":
        if memory_trend_strength > 0.70:
            logger.warning(
                f"[PRICE_MEMORY_VETO][{asset}] SELL contre tendance BULLISH forte "
                f"(strength={memory_trend_strength:.2f}, net={memory_net_pips:+.1f} pips)"
            )
            decision_mini["action"] = "HOLD"
            decision_mini["rationale"] = f"VETO: SELL contre tendance BULLISH ({memory_net_pips:+.1f} pips)"
            decision_mini["confidence"] = 0.0

    # CAS 2: Signal AVEC tendance forte → BOOST
    elif signal_direction == "BUY" and memory_trend_direction == "BULLISH":
        if memory_trend_strength > 0.60:
            boost_points = 15
            orderflow_result_mini["score"] += boost_points
            logger.info(
                f"[PRICE_MEMORY_BOOST][{asset}] BUY aligné avec tendance BULLISH "
                f"(strength={memory_trend_strength:.2f}, boost=+{boost_points})"
            )

    elif signal_direction == "SELL" and memory_trend_direction == "BEARISH":
        if memory_trend_strength > 0.60:
            boost_points = 15
            orderflow_result_mini["score"] += boost_points
            logger.info(
                f"[PRICE_MEMORY_BOOST][{asset}] SELL aligné avec tendance BEARISH "
                f"(strength={memory_trend_strength:.2f}, boost=+{boost_points})"
            )

    # CAS 3: Signal contre tendance modérée → PENALTY
    elif signal_direction == "BUY" and memory_trend_direction == "BEARISH":
        if memory_trend_strength > 0.40:
            penalty_points = 10
            orderflow_result_mini["score"] -= penalty_points
            logger.info(
                f"[PRICE_MEMORY_PENALTY][{asset}] BUY contre tendance BEARISH modérée "
                f"(strength={memory_trend_strength:.2f}, penalty=-{penalty_points})"
            )

    elif signal_direction == "SELL" and memory_trend_direction == "BULLISH":
        if memory_trend_strength > 0.40:
            penalty_points = 10
            orderflow_result_mini["score"] -= penalty_points
            logger.info(
                f"[PRICE_MEMORY_PENALTY][{asset}] SELL contre tendance BULLISH modérée "
                f"(strength={memory_trend_strength:.2f}, penalty=-{penalty_points})"
            )
```

---

## 📊 AFFICHAGE DANS LE TABLEAU

### EMPLACEMENT 3: Génération du rapport d'affichage

**Ligne ~3987** (remplacement du `trend_str`):

```python
# Formater trend depuis Price Memory + OrderFlow (08 JAN 2026: Fusion Memory + OF)
of_bias = orderflow_result_mini.get("bias", "NEUTRAL")

# Combiner OrderFlow (court terme) + PriceMemory (moyen terme)
if memory_trend_direction == "BULLISH" and of_bias in ["BULLISH", "BUY"]:
    # Alignement parfait
    trend_str = f"🟢🟢 BULL NET{memory_net_pips:+.0f}"

elif memory_trend_direction == "BEARISH" and of_bias in ["BEARISH", "SELL"]:
    # Alignement parfait
    trend_str = f"🔴🔴 BEAR NET{memory_net_pips:+.0f}"

elif memory_trend_direction == "BULLISH" and of_bias in ["BEARISH", "SELL"]:
    # Contre-tendance (signal SELL mais mémoire BULL)
    trend_str = f"⚠️ BEAR NET{memory_net_pips:+.0f}"

elif memory_trend_direction == "BEARISH" and of_bias in ["BULLISH", "BUY"]:
    # Contre-tendance (signal BUY mais mémoire BEAR)
    trend_str = f"⚠️ BULL NET{memory_net_pips:+.0f}"

elif memory_trend_direction == "RANGE":
    # Marché flat/choppy
    trend_str = f"⚪ FLAT NET{memory_net_pips:+.0f}"

else:
    # Fallback
    if of_bias in ["BULLISH", "BUY"]:
        trend_str = f"🟢 BULL NET{memory_net_pips:+.0f}"
    elif of_bias in ["BEARISH", "SELL"]:
        trend_str = f"🔴 BEAR NET{memory_net_pips:+.0f}"
    else:
        trend_str = f"⚪ NEU NET{memory_net_pips:+.0f}"
```

---

## 📈 EXEMPLE DE RÉSULTAT ATTENDU

### Marché avec tendance claire:

```
═══════════════════════════════════════════════════════════════════════
📊 SCALPING MULTI-ACTIFS - 08 Jan 2026 14:35:22
───────────────────────────────────────────────────────────────────────
ASSET  │ RÉGIME     │ TREND             │ SCORING    │ DELTA  │ ACTION
───────┼────────────┼───────────────────┼────────────┼────────┼─────────
USDJPY │ TREN(0.8)  │ 🟢🟢 BULL NET+35  │ 78.5/BUY   │ 🟢+18  │ 📈 BUY
EURUSD │ TRAN(0.5)  │ ⚠️ BULL NET-22    │ 52.3/BUY   │ 🟢+5   │ ⏸️ HOLD
GBPUSD │ RANG(0.9)  │ ⚪ FLAT NET+2     │ 48.1/SEL   │ 🔴-3   │ ⏸️ HOLD
───────────────────────────────────────────────────────────────────────

Détails:
USDJPY: 📈 BUY
└─ Signal BUY (delta +18) ALIGNÉ avec tendance BULLISH (+35 pips)
   Score boosted +15 pts (78.5)

EURUSD: ⏸️ HOLD
└─ Signal BUY (delta +5) CONTRE tendance BEARISH (-22 pips)
   Score pénalisé -10 pts (52.3), signal mixte

GBPUSD: ⏸️ HOLD
└─ Marché FLAT (+2 pips net), pas de direction claire
```

### Marché choppy (EURUSD typique):

```
EURUSD │ TRAN(0.5)  │ 🔴🔴 BEAR NET-18  │ 73.2/SEL   │ 🔴-8   │ 📉 SELL

Détails:
└─ Signal SELL (delta -8) ALIGNÉ avec tendance BEARISH (-18 pips)
   Malgré bougies alternées vert/rouge, déplacement net confirme baisse
   Score boosted +15 pts (73.2)
```

---

## 🔑 AVANTAGES DE CETTE INTÉGRATION

1. **VETO les faux signaux** → Évite BUY contre tendance baissière forte
2. **BOOST les vrais signaux** → Renforce BUY aligné avec tendance haussière
3. **Visibilité claire** → Tableau montre "NET+35" ou "NET-22"
4. **Résoud le problème EURUSD choppy** → Voit la tendance malgré oscillations
5. **Alignement multi-timeframe** → OrderFlow (8s) + Memory (50 bougies)

---

## 🚀 PROCHAINE ÉTAPE

Intégrer ce code dans `run_bot.py` aux 3 emplacements indiqués:
1. Ligne ~3450: Appel `analyze_trend_structure()`
2. Ligne ~3650: VETO/BOOST selon alignement
3. Ligne ~3987: Affichage tableau avec NET pips

Le bot pourra alors dire:
> "Signal BUY détecté (+15 delta), MAIS mémoire montre tendance baissière (-25 pips) → VETO"

ou

> "Signal SELL détecté (-12 delta), ET mémoire confirme tendance baissière (-35 pips) → BOOST +15 pts!"
