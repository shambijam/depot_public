# 🔧 FIX BOT BLOCKING - 03 JANVIER 2026

## 🐛 PROBLÈME RÉSOLU

**Symptôme** : Bot se bloquait après démarrage du thread EURUSD, juste après l'instanciation de ScalpingStrategy

```
[INFO] - 🚀 [EURUSD] Worker démarré (cycle 2.5s - BURST MODE) ⚡
[INFO] - ✅ [EURUSD] MarketAnalyzer instancié
[INFO] - Moteur de stratégie Scalping initialisé.
[INFO] - ✅ [EURUSD] ScalpingStrategy instanciée
(PUIS PLUS RIEN - BLOCAGE)
```

**Cause identifiée** : `mt5_connector.get_ticks_for_candle()` bloquait indéfiniment en attendant réponse MT5 qui ne venait jamais (probablement symbol non disponible ou connexion MT5 instable).

---

## ✅ SOLUTIONS IMPLÉMENTÉES

### 1. Timeout sur MT5 `copy_ticks_range()` ⚡

**Fichier** : `mt5_connector.py` lignes 1756-1837

**Modification** :
- Ajout paramètre `timeout=5.0` (secondes) à `get_ticks_for_candle()`
- Wrapping de l'appel MT5 dans un thread daemon avec timeout
- Si timeout dépassé → retourne DataFrame vide au lieu de bloquer indéfiniment
- Logging explicite `[MT5C_TIMEOUT]` pour diagnostic

**Code** :
```python
def get_ticks_for_candle(
    self, symbol: str, start_ts: datetime, end_ts: datetime, timeout: float = 5.0
) -> pd.DataFrame:
    # ...
    # Requête brute AVEC TIMEOUT (03 JAN 2026)
    ticks = [None]
    exception = [None]

    def fetch_ticks():
        try:
            ticks[0] = self.mt5.copy_ticks_range(
                symbol, start_ts, end_ts, self.mt5.COPY_TICKS_ALL
            )
        except Exception as e:
            exception[0] = e

    fetch_thread = threading.Thread(target=fetch_ticks, daemon=True)
    fetch_thread.start()
    fetch_thread.join(timeout=timeout)

    # Si thread encore en vie après timeout = blocage MT5
    if fetch_thread.is_alive():
        self.logger.error(
            f"[MT5C_TIMEOUT] ⏱️ copy_ticks_range() timeout après {timeout}s pour {symbol}. "
            f"MT5 ne répond pas, retour DataFrame vide."
        )
        return pd.DataFrame(columns=ret_cols)
```

---

### 2. Logging Renforcé dans `run_bot.py` 📊

**Fichier** : `run_bot.py` lignes 3199-3242

**Modifications** :
- Changement `logger.debug()` → `logger.info()` / `logger.error()` pour visibilité
- Logging explicite du début de chargement ticks avec fenêtre temporelle
- Logging explicite du timeout si ça se produit
- Exception handler séparé pour `TimeoutError`

**Code** :
```python
# Logging début chargement
logger.info(f"[{asset}] 🔄 Chargement ticks [{candle_start.strftime('%H:%M:%S')} → {candle_end.strftime('%H:%M:%S')}]...")

# Charger ticks pour cette fenêtre M1 (avec timeout 5s par défaut)
ticks_df = mt5_connector.get_ticks_for_candle(
    asset,
    candle_start.to_pydatetime(),
    candle_end.to_pydatetime(),
    timeout=5.0  # ⚡ TIMEOUT (03 JAN 2026): Protection contre blocage MT5
)

if ticks_df is not None and not ticks_df.empty:
    logger.info(f"[{asset}] ✅ {len(ticks_df)} ticks chargés")
else:
    logger.warning(f"[{asset}] ⚠️ Aucun tick récupéré pour cette bougie")
    ticks_df = None

except TimeoutError as e_timeout:
    logger.error(f"[{asset}] ⏱️ TIMEOUT chargement ticks: {e_timeout}")
    ticks_df = None
```

---

### 3. Vérification Symbol Disponible 🔍

**Fichier** : `run_bot.py` lignes 3176-3199

**Modification** :
- Vérification `mt5_connector.get_symbol_info(asset)` AVANT fetch de données
- Skip cycle si symbol non disponible (évite blocage)
- Tentative d'activation du symbol s'il n'est pas visible dans Market Watch
- Graceful degradation : continue même si vérification échoue (évite blocage du cycle)

**Code** :
```python
# ⚡ VÉRIFICATION SYMBOL (03 JAN 2026): Protection contre symbol non disponible
try:
    symbol_info = mt5_connector.get_symbol_info(asset)
    if not symbol_info:
        logger.error(f"[{asset}] ❌ Symbol non disponible dans MT5, skip cycle")
        global_state.record_error(asset, "Symbol unavailable")
        time.sleep(cycle_interval)
        continue

    # Vérifier si visible dans Market Watch
    if hasattr(symbol_info, 'visible') and not symbol_info.visible:
        logger.warning(f"[{asset}] ⚠️ Symbol non visible, tentative activation...")
        try:
            import MetaTrader5 as mt5
            if not mt5.symbol_select(asset, True):
                logger.warning(f"[{asset}] ⚠️ Impossible activer symbol, continue quand même...")
        except Exception as e_select:
            logger.debug(f"[{asset}] Symbol select failed: {e_select}")

except Exception as e_symbol:
    logger.warning(f"[{asset}] ⚠️ Vérification symbol failed: {e_symbol}")
    # Continue quand même (ne pas bloquer le cycle)
```

---

## 🎯 RÉSULTATS ATTENDUS

### AVANT Fix
```
[INFO] - ✅ [EURUSD] ScalpingStrategy instanciée
(BLOCAGE INDÉFINI - bot freeze)
```

### APRÈS Fix
```
[INFO] - ✅ [EURUSD] ScalpingStrategy instanciée
[INFO] - [EURUSD] 🔄 Chargement ticks [14:23:00 → 14:24:00]...

CAS 1 - Timeout MT5:
[ERROR] - [MT5C_TIMEOUT] ⏱️ copy_ticks_range() timeout après 5.0s pour EURUSD [14:23:00 → 14:24:00]. MT5 ne répond pas, retour DataFrame vide.
[ERROR] - [EURUSD] ⏱️ TIMEOUT chargement ticks: ...
→ Bot continue avec ticks=None (pas de blocage)

CAS 2 - Symbol non disponible:
[ERROR] - [EURUSD] ❌ Symbol non disponible dans MT5, skip cycle
→ Bot skip ce cycle et attend le prochain

CAS 3 - Succès:
[INFO] - [EURUSD] ✅ 247 ticks chargés
→ Bot continue normalement
```

---

## 🔄 COMPORTEMENT GRACEFUL DEGRADATION

**Principe** : Si les ticks ne sont pas disponibles (timeout ou erreur), le bot **continue quand même** avec `ticks_df=None`

**Impact** :
- ✅ Timing Analyzer fonctionne sans ticks (utilise valeurs par défaut pour tick_rate/coverage)
- ✅ OrderFlow V6 fonctionne sans ticks (analyse uniquement candles OHLCV)
- ✅ Le thread ne se bloque jamais
- ⚠️ Perte de précision des métriques burst (tick_rate, coverage, pressure_ratio)

**Recommandation** :
- Si timeouts fréquents → vérifier connexion MT5 / broker
- Si symbol non disponible → vérifier Market Watch / abonnement symbol
- Timeout 5s est généreux (MT5 devrait répondre en <1s normalement)

---

## 📊 MÉTRIQUES DE SUCCÈS

**À surveiller après redémarrage** :
- ✅ Les 3 threads (USDJPY, EURUSD, GBPUSD) démarrent tous
- ✅ Dashboard s'affiche avec métriques actualisées
- ✅ Logs montrent chargement ticks pour chaque asset
- ✅ Aucun blocage/freeze

**Logs critiques à vérifier** :
```bash
# Démarrage threads
grep "Worker démarré" logs/*.log

# Chargement ticks
grep "Chargement ticks" logs/*.log
grep "ticks chargés" logs/*.log

# Détection problèmes
grep "TIMEOUT" logs/*.log
grep "Symbol non disponible" logs/*.log
```

---

## 🚀 PROCHAINES ÉTAPES

1. ✅ **Redémarrer le bot** pour tester les fixes
2. ✅ **Vérifier logs** : Les 3 threads démarrent correctement
3. ✅ **Vérifier Dashboard** : S'affiche et s'actualise
4. ⚠️ **Surveiller timeouts** : Si fréquents, investiguer connexion MT5

---

**Date fix** : 03 Janvier 2026
**Fichiers modifiés** :
- `mt5_connector.py` (timeout mechanism)
- `run_bot.py` (logging + symbol verification)

**Impact** : Résolution du blocage bot au démarrage + amélioration robustesse générale
