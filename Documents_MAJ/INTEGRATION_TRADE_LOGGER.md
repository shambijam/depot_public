# 📝 Intégration TradeLogger - Guide d'Implémentation

*Date: 25 Novembre 2025*

---

## 🎯 Objectif

Journaliser **TOUS** les trades avec leurs métriques de scoring pour:
1. **Valider** si le scoring actuel prédit vraiment la qualité des trades
2. **Optimiser** les seuils et pénalités basés sur des données réelles
3. **Améliorer** continuellement le système de manière data-driven

---

## 📊 Architecture

```
run_bot.py (décision trade)
    ↓
fusion_manager.py (_final_decision)
    → Génère: score_final, of_score, fp_score, trigger_type, etc.
    ↓
trade_executor.py (exécution)
    → burst.py (open_burst_basket)
        ├─ ✅ ENTRÉE: trade_logger.log_trade_entry(...)
        └─ (positions ouvertes avec basket_id)
    ↓
Surveillance positions
    → sltp.py (trailing stop, TP hit, SL hit)
        └─ ✅ SORTIE: trade_logger.log_trade_exit(...)
```

---

## 🔧 Modifications à Appliquer

### **1. Initialisation dans TradeExecutor**

**Fichier**: `trader/trade_executor.py`

**Ligne ~60** (après `__init__`):
```python
# ✅ AJOUTÉ (25 Nov 2025): Journalisation trades pour analyse
from trader.trade_logger import TradeLogger
self.trade_logger = TradeLogger(config_manager)
self.logger.info("✅ TradeLogger initialisé")
```

---

### **2. Capture à l'ENTRÉE (après ouverture basket)**

**Fichier**: `trader/burst.py`

**Fonction**: `open_burst_basket()` ligne ~90 (après succès)

**Code à ajouter**:
```python
# ✅ AJOUTÉ (25 Nov 2025): Journalisation ENTRÉE trade
if tickets:  # Si au moins 1 position ouverte
    try:
        # Récupérer métriques depuis trade_decision (passé en paramètre)
        td = base_request.get("trade_decision", {})
        fusion_data = td.get("fusion_data", {})

        # Extraire scores
        score_final = fusion_data.get("fused_confidence", 0.0)
        coherence = fusion_data.get("coherence", {})
        normalized = fusion_data.get("normalized", {})

        n_of = normalized.get("orderflow", {})
        n_fp = normalized.get("footprint", {})
        n_tr = normalized.get("trigger", {})

        of_score = n_of.get("score", 0.0)
        fp_score = n_fp.get("score", 0.0)
        score_base = (of_score + fp_score) / 2.0

        # Trigger
        trigger_type = n_tr.get("type", "none")
        trigger_confidence = n_tr.get("score", 0.0)
        trigger_boost = fusion_data.get("trigger_boost", 0.0)

        # Qualité données
        fp_raw = n_fp.get("raw", {})
        fp_summary = fp_raw.get("summary", {})
        if isinstance(fp_summary, str):
            import json
            try:
                fp_summary = json.loads(fp_summary)
            except:
                fp_summary = {}

        tick_count = int(fp_summary.get("tick_count", 0))
        coverage_s = float(fp_summary.get("coverage_s", 0.0))
        status_of = n_of.get("status", "UNKNOWN")
        status_fp = n_fp.get("status", "UNKNOWN")
        quality_multiplier = fusion_data.get("quality_multiplier", 1.0)

        # Cohérence
        aligned_3_of_3 = coherence.get("aligned3", False)
        matrix = coherence.get("matrix", {})
        conflicts_count = sum(1 for v in matrix.values() if v == "conflict")

        # Infos position
        symbol = base_request.get("symbol", "UNKNOWN")
        direction = "BUY" if base_request.get("action") == "BUY" else "SELL"
        entry_price = float(base_request.get("price", 0.0))
        volume = float(base_request.get("volume", 0.0)) * burst_size
        sl_price = float(base_request.get("sl", 0.0))
        tp_price = float(base_request.get("tp", 0.0))

        # Log entrée
        self.trade_logger.log_trade_entry(
            basket_id=basket_id,
            symbol=symbol,
            direction=direction,
            entry_price=entry_price,
            volume=volume,
            sl_price=sl_price,
            tp_price=tp_price,
            # Scoring
            score_final=score_final,
            score_base=score_base,
            of_score=of_score,
            fp_score=fp_score,
            trigger_type=trigger_type,
            trigger_confidence=trigger_confidence,
            trigger_boost=trigger_boost,
            quality_multiplier=quality_multiplier,
            # Qualité
            tick_count=tick_count,
            coverage_s=coverage_s,
            status_of=status_of,
            status_fp=status_fp,
            # Cohérence
            aligned_3_of_3=aligned_3_of_3,
            conflicts_count=conflicts_count,
            # Contexte
            strategy="scalping",
            burst_size=burst_size,
            # Extra
            tickets=tickets
        )
    except Exception as e:
        self.logger.error(f"❌ [TRADE_LOG] Erreur log entrée: {e}", exc_info=True)
```

---

### **3. Capture à la SORTIE (clôture positions)**

**Fichier**: `trader/sltp.py` (ou `trader/burst.py` si watchdog)

**Fonction**: Détection clôture basket (trailing, TP, SL)

**Code à ajouter**:
```python
# ✅ AJOUTÉ (25 Nov 2025): Journalisation SORTIE trade
try:
    # Calculer PnL
    total_pnl_pips = 0.0
    total_pnl_usd = 0.0
    exit_price_avg = 0.0

    for pos in closed_positions:
        profit_pips = pos.get("profit_pips", 0.0)
        profit_usd = pos.get("profit", 0.0)
        total_pnl_pips += profit_pips
        total_pnl_usd += profit_usd
        exit_price_avg += pos.get("close_price", 0.0)

    exit_price_avg /= len(closed_positions) if closed_positions else 1

    # Déterminer raison sortie
    if "trailing" in context:
        exit_reason = "trailing_stop"
    elif all_tp_hit:
        exit_reason = "tp_hit"
    elif all_sl_hit:
        exit_reason = "sl_hit"
    else:
        exit_reason = "partial_close"

    # Log sortie
    self.trade_logger.log_trade_exit(
        basket_id=basket_id,
        exit_price=exit_price_avg,
        pnl_pips=total_pnl_pips,
        pnl_usd=total_pnl_usd,
        exit_reason=exit_reason,
        # MFE/MAE si trackés
        max_favorable_excursion_pips=context.get("max_profit_pips"),
        max_adverse_excursion_pips=context.get("max_drawdown_pips")
    )
except Exception as e:
    self.logger.error(f"❌ [TRADE_LOG] Erreur log sortie: {e}", exc_info=True)
```

---

### **4. Nettoyage Périodique (optionnel)**

**Fichier**: `run_bot.py`

**Fonction**: Boucle principale (toutes les heures par ex.)

**Code à ajouter**:
```python
# Nettoyage des trades "oubliés" (> 24h sans clôture)
if cycle_count % 360 == 0:  # Toutes les heures (si cycle 10s)
    try:
        stale_count = trade_executor.trade_logger.cleanup_stale_trades(max_age_hours=24)
        if stale_count > 0:
            logger.warning(f"⚠️ {stale_count} trades stalés nettoyés")
    except Exception as e:
        logger.error(f"❌ Erreur cleanup trades: {e}")
```

---

## 📊 Utilisation du Script d'Analyse

### **Après 10 trades** (premier aperçu):
```bash
python tools/analyze_trades.py --min-trades 10
```

### **Après 50 trades** (analyse fiable):
```bash
python tools/analyze_trades.py --min-trades 50
```

### **Analyse complète** (100+ trades):
```bash
python tools/analyze_trades.py
```

---

## 📈 Rapports Générés

Le script affiche:

### 1. **Performance par Catégorie de Score**
```
🟢 DIAMANT    (score ≥90%)
   Trades      :   12 trades
   Win Rate    :  75.0% (9W / 3L / 0BE)
   Avg PnL     :  +28.3 pips (+282.50 USD)
   Avg Duration:   12.5 min

🟡 PLATINE    (score 80-89%)
   Trades      :   25 trades
   Win Rate    :  64.0% (16W / 9L / 0BE)
   Avg PnL     :  +18.5 pips (+185.20 USD)
   Avg Duration:   15.2 min
```

### 2. **Performance par Type de Trigger**
```
🟢 stacking
   Trades   :   18
   Win Rate :  77.8% (14W / 4L)
   Avg PnL  :  +32.5 pips
   Avg Conf :  82.3%

🟡 climax
   Trades   :    8
   Win Rate :  62.5% (5W / 3L)
   Avg PnL  :  +15.8 pips
   Avg Conf :  76.5%
```

### 3. **Impact Qualité Données**
```
🔍 Segmentation par tick_count:
  🟢 > 100 ticks     :  32 trades | Win Rate:  71.9% | Avg PnL:  +25.8 pips
  🟡 50-100 ticks    :  15 trades | Win Rate:  60.0% | Avg PnL:  +12.0 pips
  🔴 < 50 ticks      :   8 trades | Win Rate:  37.5% | Avg PnL:   -5.2 pips
```

### 4. **Recommandations**
```
💡 RECOMMANDATIONS D'OPTIMISATION

📉 Pénalités qualité appliquées: 23 trades (45.2% win rate)
   ✅ Les pénalités semblent justifiées (faible win rate)
   → Recommandation: MAINTENIR ou RENFORCER les pénalités

🎯 Impact du Trigger:
   Avec trigger    :  28 trades |  75.0% win rate
   Sans trigger    :  17 trades |  52.9% win rate
   ✅ Le trigger AMÉLIORE significativement la sélection (+22.1%)
   → Recommandation: AUGMENTER le bonus trigger
```

---

## 🎯 Décisions Basées sur Données

### **Exemple 1: Les pénalités qualité sont-elles justifiées ?**

**Données** (après 50 trades):
- Trades avec tick_count < 50: **37.5% win rate**
- Trades avec tick_count > 100: **71.9% win rate**

**→ Décision**: ✅ **MAINTENIR** la pénalité `× 0.3` pour tick_count < 50

---

### **Exemple 2: Le trigger améliore-t-il vraiment ?**

**Données** (après 50 trades):
- Trades SANS trigger (base ≥70%): **58.3% win rate**
- Trades AVEC STACKING (≥0.85): **87.5% win rate** (+29.2%)

**→ Décision**: ✅ **AUGMENTER** le bonus trigger de +15% à +20% pour STACKING

---

### **Exemple 3: Score DIAMANT = meilleurs trades ?**

**Données** (après 100 trades):
- DIAMANT (≥90%): **75.0% win rate**, +28.3 pips
- PLATINE (80-89%): **64.0% win rate**, +18.5 pips
- OR (70-79%): **50.2% win rate**, +2.1 pips

**→ Décision**:
- ✅ **GARDER** seuil HIGH_CONVICTION à 80%
- ⚠️ **AUGMENTER** seuil MODERATE de 70% à 75% (OR trop proche du breakeven)

---

## 🔄 Boucle d'Amélioration Continue

```
1. LANCER le bot avec TradeLogger activé
   ↓
2. ACCUMULER 50-100 trades
   ↓
3. ANALYSER avec tools/analyze_trades.py
   ↓
4. IDENTIFIER les ajustements nécessaires
   ↓
5. MODIFIER fusion_manager.py (seuils, pénalités, bonus)
   ↓
6. RETOUR étape 1 (nouveau cycle)
```

---

## ✅ Checklist d'Intégration

- [ ] Ajouter `from trader.trade_logger import TradeLogger` dans trade_executor.py
- [ ] Initialiser `self.trade_logger = TradeLogger(config_manager)` dans __init__
- [ ] Ajouter `log_trade_entry()` dans burst.py (après ouverture basket)
- [ ] Ajouter `log_trade_exit()` dans sltp.py (clôture positions)
- [ ] Tester avec 1 trade pour vérifier le fichier logs/trades_history.jsonl
- [ ] Ajouter nettoyage périodique dans run_bot.py (optionnel)
- [ ] Lancer le bot et accumuler 50+ trades
- [ ] Exécuter `python tools/analyze_trades.py`
- [ ] Ajuster le scoring selon les recommandations

---

## 📝 Format du Fichier de Log

**Fichier**: `logs/trades_history.jsonl`

**Format**: JSON Lines (1 trade par ligne)

**Exemple**:
```json
{"trade_id": "abc12345", "symbol": "XAUUSD", "direction": "BUY", "entry_time": "2025-11-25T14:30:25Z", "entry_price": 4085.50, "volume": 0.56, "sl_price": 4045.50, "tp_price": 4125.50, "score_final": 0.82, "score_base": 0.70, "score_of": 0.75, "score_fp": 0.65, "trigger_type": "stacking", "trigger_confidence": 0.88, "trigger_boost": 0.12, "quality_multiplier": 1.0, "tick_count": 180, "coverage_s": 55.3, "status_of": "VALID", "status_fp": "VALID", "aligned_3_of_3": true, "conflicts_count": 0, "score_category": "PLATINE", "outcome": "WIN", "exit_time": "2025-11-25T14:42:18Z", "exit_price": 4117.20, "pnl_pips": 31.7, "pnl_usd": 177.52, "duration_minutes": 11.88, "exit_reason": "trailing_stop"}
```

---

## 🎓 Points d'Attention

1. **Passer trade_decision à open_burst_basket()**
   - Actuellement, `base_request` ne contient pas forcément `trade_decision`
   - Solution: Ajouter `base_request["trade_decision"] = td` dans order_builder.py

2. **Détection clôture basket**
   - Le code actuel ne track pas facilement les clôtures complètes
   - Solution: Ajouter un hook dans le watchdog burst ou trailing stop

3. **MFE/MAE (Max Favorable/Adverse Excursion)**
   - Nécessite de tracker le profit max/min pendant la vie du trade
   - Optionnel mais très utile pour analyser la qualité du timing d'exit

4. **Volume réel vs volume demandé**
   - En cas de partial fill, le volume réel peut être < volume demandé
   - Capturer le volume réel depuis les positions ouvertes

---

## 🚀 Prochaines Étapes

1. **Court terme** (aujourd'hui):
   - Implémenter l'intégration dans trade_executor.py et burst.py
   - Tester avec 1 trade pour valider le format

2. **Moyen terme** (1-2 semaines):
   - Accumuler 50-100 trades
   - Première analyse avec tools/analyze_trades.py
   - Ajuster les seuils selon les recommandations

3. **Long terme** (1-2 mois):
   - Machine Learning pour optimiser automatiquement les poids
   - A/B Testing: comparer ancien vs nouveau scoring
   - Dashboard temps réel des métriques

---

*Document créé le: 25 Novembre 2025*
*Auteur: Claude Code*
*Objectif: Optimisation data-driven du scoring*
