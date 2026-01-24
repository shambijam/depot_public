# CHANGELOG 16 JANVIER 2026

## 1. ConfigMerger - closure_rules

**Fichiers modifiés:**
- `core/config_merge.py`
- `core/config_manager.py`
- `trader/burst.py`

**Problème:** Le `target_profit_pips` des configs actifs (GBPUSD.json, etc.) n'était pas chargé correctement.

**Solution:**
- Ajout de `get_closure_rules_config(asset_symbol)` dans ConfigMerger
- Fusionne automatiquement base config + asset overrides
- `burst.py` utilise maintenant `config_manager.get_closure_rules_config(sym)`

---

## 2. MTF Analysis (Multi-TimeFrame)

**Fichiers modifiés:**
- `phase_observer/price_memory_analyzer.py`
- `run_bot.py`

### Logique MTF

Analyse de **1 bougie** sur chaque timeframe:
- M15: 1 bougie
- M5: 1 bougie
- M1: 1 bougie

**Règle simple:**
- `close < open` = BEARISH (rouge)
- `close > open` = BULLISH (vert)
- JAMAIS NEUTRAL

### Bonus MTF

| Alignement | Bonus |
|------------|-------|
| 3/3 | +30 pts |
| 2/3 | +15 pts |
| 1/3 | +5 pts |
| 0/3 | 0 pts |

### Logs

```
[MTF_CANDLE][GBPUSD][M15] 🔴 O=1.33850 H=1.33860 L=1.33820 C=1.33830 | C<O → BEARISH
[MTF_CANDLE][GBPUSD][M5]  🔴 O=1.33840 H=1.33845 L=1.33825 C=1.33830 | C<O → BEARISH
[MTF_CANDLE][GBPUSD][M1]  🔴 O=1.33835 H=1.33840 L=1.33828 C=1.33830 | C<O → BEARISH
🐻 [MTF_VERDICT][GBPUSD] BEARISH (3/3) | M15:🔴 M5:🔴 M1:🔴 | Bonus: +30 pts
```

### Fix fetch M5/M15

**Erreur corrigée:** `MT5Connector.get_rates() got an unexpected keyword argument 'count'`

**Avant (faux):**
```python
mt5_connector.get_rates(asset, mt5.TIMEFRAME_M5, count=30)
```

**Après (correct):**
```python
mt5_connector.get_rates(asset, "M5", 1)  # 1 seule bougie
```

---

## 3. Méthodes ajoutées

### price_memory_analyzer.py

- `_get_point_size(asset)` - Retourne la taille du point par actif
- `analyze_single_timeframe(asset, timeframe, candles, current_price)` - Analyse 1 TF
- `get_mtf_trend_verdict(asset, candles_m15, candles_m5, candles_m1, current_price)` - Verdict final

### config_merge.py

- `get_closure_rules_config(asset_symbol, strategy_name)` - Config closure_rules fusionnée

### config_manager.py

- `get_closure_rules_config(asset_symbol, strategy_name)` - Délégation vers ConfigMerger

---

## 4. Valeurs closure_rules par actif

| Actif | target_profit_pips | max_loss_pips |
|-------|-------------------|---------------|
| EURUSD | 1.6 | 25 |
| GBPUSD | 1.4 | 25 |
| USDJPY | 1.21 | 25 |
| NAS100 | 170 | 250 |
