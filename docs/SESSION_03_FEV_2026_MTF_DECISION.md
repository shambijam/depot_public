# Session 03 Février 2026 - Correction Système MTF & Décision de Trade

## Problème Initial

Le bot prenait des trades dans la mauvaise direction :
- Prenait des **BUY** alors que le marché était clairement **BEARISH**
- Prenait des **SELL** alors que le marché était clairement **BULLISH**

---

## Causes Identifiées

### 1. Décision BUY sans vérification MTF (BRANCHE 2)

**Fichier:** `run_bot.py` (lignes 4079-4112)

**Avant:**
```python
# BUY : Décidé par delta positif (inchangé, ultra fiable)
if delta_weighted > 0 and abs(delta_weighted) >= delta_threshold:
    filtre1_direction = "BUY"  # ← Pas de check MTF !
```

Le BUY était décidé **uniquement** par le delta positif, sans vérifier que le MTF confirmait la direction BULLISH.

### 2. Analyse MTF basée sur 1 seule bougie

**Fichier:** `phase_observer/price_memory_analyzer.py` (fonction `analyze_single_timeframe`)

**Avant:**
```python
last_candle = candles.iloc[-1]
if candle_close < candle_open:
    direction = 'BEARISH'
elif candle_close > candle_open:
    direction = 'BULLISH'
```

L'analyse MTF regardait **seulement la dernière bougie** de chaque timeframe. Un petit rebond (1 bougie verte) suffisait à dire "BULLISH" même si le marché descendait depuis 3 heures.

---

## Corrections Apportées

### Correction 1: Confirmation MTF obligatoire pour BUY et SELL

**Fichier:** `run_bot.py` (lignes 4079-4155)

**Nouvelle logique:**
- **BUY** = Delta+ **ET** MTF M5+M1 BULLISH
- **SELL** = Delta- **ET** MTF M5+M1 BEARISH
- **SELL via MTF seul** = Si MTF M5+M1 BEARISH (même si delta faible)
- **HOLD** = Si delta et MTF ne sont pas alignés

```python
# BUY : Delta+ ET MTF confirme BULLISH (M5+M1)
if delta_weighted > 0 and abs(delta_weighted) >= delta_threshold:
    if m5_m1_bullish:
        filtre1_direction = "BUY"
    else:
        # Delta+ mais MTF pas BULLISH → REFUS
        filtre1_direction = "HOLD"

# SELL : Delta- ET MTF confirme BEARISH (M5+M1)
elif delta_weighted < 0 and abs(delta_weighted) >= delta_threshold:
    if m5_m1_bearish:
        filtre1_direction = "SELL"
    else:
        # Delta- mais MTF pas BEARISH → REFUS
        filtre1_direction = "HOLD"
```

**Logs ajoutés:**
- `[MTF_CONFIRM] ✅ BUY confirmé` - Delta+ ET MTF BULLISH
- `[MTF_CONFIRM] ❌ BUY REFUSÉ` - Delta+ mais MTF pas BULLISH
- `[MTF_CONFIRM] ✅ SELL confirmé` - Delta- ET MTF BEARISH
- `[MTF_CONFIRM] ❌ SELL REFUSÉ` - Delta- mais MTF pas BEARISH

### Correction 2: Analyse MTF multi-bougies

**Fichier:** `phase_observer/price_memory_analyzer.py` (fonction `analyze_single_timeframe`)

**Nouvelle logique:**
- Analyse **2 bougies** par timeframe (au lieu de 1)
- M15 : 2 bougies = 30 minutes
- M5 : 2 bougies = 10 minutes
- M1 : 2 bougies = 2 minutes

```python
LOOKBACK_CANDLES = 2

# Compter les bougies vertes vs rouges
for _, c in recent_candles.iterrows():
    if c_close > c_open:
        green_count += 1
    elif c_close < c_open:
        red_count += 1

# Calculer le mouvement net du prix
net_pips = (last_close - first_open) / point

# Déterminer direction basée sur majorité ET mouvement net
if red_count > green_count or net_pips < -2:
    direction = 'BEARISH'
elif green_count > red_count or net_pips > 2:
    direction = 'BULLISH'
```

**Nouveau format de log:**
```
[MTF_CANDLE][USDCHF][M15] 🔴 [2 bougies] 🟢0 vs 🔴2 | Net: -8.5 pips | → BEARISH
```

---

## Résumé des Fichiers Modifiés

| Fichier | Modification |
|---------|--------------|
| `run_bot.py` | Ajout confirmation MTF obligatoire pour BUY (lignes 4079-4155) |
| `phase_observer/price_memory_analyzer.py` | Analyse 2 bougies au lieu de 1 (fonction `analyze_single_timeframe`) |

---

## Règles de Validation Trade (Après Correction)

### Pour un BUY:
1. ✅ Delta positif (≥ threshold)
2. ✅ MTF M5 = BULLISH
3. ✅ MTF M1 = BULLISH
4. (Bonus) MTF M15 = BULLISH → confidence +15%

### Pour un SELL:
1. ✅ Delta négatif (≥ threshold) OU MTF seul
2. ✅ MTF M5 = BEARISH
3. ✅ MTF M1 = BEARISH
4. (Bonus) MTF M15 = BEARISH → confidence +15%

### HOLD (pas de trade):
- Delta+ mais MTF pas BULLISH
- Delta- mais MTF pas BEARISH
- MTF non aligné (M5 ≠ M1)

---

## Test

Après ces corrections, le bot devrait :
1. **Ne plus prendre de BUY** quand le marché descend (MTF BEARISH)
2. **Ne plus prendre de SELL** quand le marché monte (MTF BULLISH)
3. **Afficher des logs clairs** sur la raison de la décision

Surveiller les logs:
- `[MTF_CONFIRM]` - Décisions BRANCHE 2
- `[MTF_M5M1_BLOCK]` - Décisions BRANCHE 3
- `[MTF_CANDLE]` - Analyse des bougies par timeframe
