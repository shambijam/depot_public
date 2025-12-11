# ADAPTATION DU RÉGIME VWAP POUR 25 BARRES M1

## DOCUMENT TECHNIQUE CRITIQUE
**Date:** 2025-12-10
**Problématique:** Le VWAP dynamique nécessite un régime de marché, actuellement calculé sur 200 barres
**Objectif:** Adapter le calcul du régime pour fonctionner avec 25 barres M1

---

## TABLE DES MATIÈRES

1. [Comprendre le problème](#comprendre-le-problème)
2. [Architecture actuelle du régime](#architecture-actuelle-du-régime)
3. [Flux de données VWAP dynamique](#flux-de-données-vwap-dynamique)
4. [Solutions d'adaptation](#solutions-dadaptation)
5. [Implémentation recommandée](#implémentation-recommandée)
6. [Code d'implémentation](#code-dimplémentation)

---

## COMPRENDRE LE PROBLÈME

### Le VWAP Dynamique

Le système VWAP adapte ses **poids de scoring** selon le **régime de marché détecté** :

```
┌─────────────────────────────────────────────────────────────┐
│              VWAP DYNAMIQUE - POIDS ADAPTATIFS              │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  Régime TRENDING:                                           │
│    • OrderFlow: 40%   • Footprint: 40%   • VWAP: 20%       │
│    → Privilégie les signaux de momentum                    │
│                                                              │
│  Régime BALANCED:                                           │
│    • OrderFlow: 35%   • Footprint: 35%   • VWAP: 30%       │
│    → Équilibre entre tous les signaux                      │
│                                                              │
│  Régime REVERSAL (Accumulation/Transitional):              │
│    • OrderFlow: 30%   • Footprint: 30%   • VWAP: 40%       │
│    → Privilégie les niveaux VWAP                           │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

### La Dépendance au Régime

**Sans régime → Pas de poids dynamiques → VWAP non optimal**

Le régime est actuellement calculé par **PhaseObserver** qui analyse les **200 barres M1** complètes pour détecter :
- Tendance (via ADX, DI+, DI-)
- Volatilité (Garman-Klass)
- Volume institutionnel
- Breakouts
- Compressions

**Problème:** Avec seulement **25 barres M1**, certains indicateurs deviennent moins précis ou impossibles à calculer correctement.

---

## ARCHITECTURE ACTUELLE DU RÉGIME

### Flux Complet (200 barres)

```
┌──────────────────────────────────────────────────────────────┐
│  1. SCALPING/LIQUIDITY Thread récupère 200 barres M1        │
└────────────────────┬─────────────────────────────────────────┘
                     │
                     ▼
┌──────────────────────────────────────────────────────────────┐
│  2. PhaseObserver.detect_market_regime(df_200_bars)          │
│     ├─ Calcule ADX (14 périodes)                            │
│     ├─ Calcule Volatilité Garman-Klass (20 périodes)       │
│     ├─ Analyse Volume institutionnel (20 périodes MA)       │
│     ├─ Détecte Breakouts (5 barres historique)             │
│     └─ Détecte Compressions                                 │
└────────────────────┬─────────────────────────────────────────┘
                     │
                     ▼
┌──────────────────────────────────────────────────────────────┐
│  3. Retourne régime PhaseObserver (16 régimes possibles)     │
│     Exemples:                                                │
│     • strong_trending_institutional_bull                     │
│     • range_retail                                           │
│     • breakout_bull                                          │
│     • high_volatility_chaos                                  │
└────────────────────┬─────────────────────────────────────────┘
                     │
                     ▼
┌──────────────────────────────────────────────────────────────┐
│  4. RegimeMapper.map_regime(phase_observer_regime)           │
│     Mappe 16 régimes PhaseObserver → 4 régimes VWAP:        │
│     • TRENDING (10→1)                                        │
│     • ACCUMULATION (2→1)                                     │
│     • BALANCED (2→1)                                         │
│     • TRANSITIONAL (4→1)                                     │
└────────────────────┬─────────────────────────────────────────┘
                     │
                     ▼
┌──────────────────────────────────────────────────────────────┐
│  5. VWAPAnalyzer utilise le régime VWAP                      │
│     ├─ get_regime_weights(regime) → poids adaptatifs        │
│     └─ Applique poids: OF%, FP%, VWAP%                      │
└──────────────────────────────────────────────────────────────┘
```

### Indicateurs Utilisés (200 barres)

| Indicateur | Période | Minimum barres requis | Barres actuelles | Compatible 25 barres ? |
|------------|---------|----------------------|------------------|----------------------|
| **ADX** | 14 | ~50 (pour stabilité) | 200 | ⚠️ Marginal (besoin 2x période) |
| **DI+/DI-** | 14 | ~50 | 200 | ⚠️ Marginal |
| **Volatilité GK** | 20 | ~60 | 200 | ❌ Non (besoin 3x période) |
| **Volume MA** | 20 | 20 | 200 | ✅ OUI |
| **Breakout Detection** | 5 | 5 | 200 | ✅ OUI |
| **ADX Quantiles** | 200 | 200 | 200 | ❌ NON (fenêtre trop grande) |

**Conclusion:** Plusieurs indicateurs sont **incompatibles ou imprécis** avec 25 barres.

---

## FLUX DE DONNÉES VWAP DYNAMIQUE

### Actuellement (200 barres)

```
SCALPING Thread (cycle 5s)
    │
    ├─> get_rates('XAUUSD', M1, 200) → df_m1_200_bars
    │
    ├─> PhaseObserver.analyze(df_m1_200_bars)
    │   └─> detect_market_regime(df_m1_200_bars)
    │       └─> Retourne: phase_observer_regime (ex: "range_retail")
    │                     + regime_strength (0.0-1.0)
    │
    ├─> RegimeMapper.map_regime(phase_observer_regime, regime_strength)
    │   └─> Retourne: vwap_regime="BALANCED", confidence=0.5
    │
    ├─> VWAPAnalyzer.analyze(df, current_price, context={
    │                         "phase_observer_regime": "range_retail",
    │                         "regime_strength": 0.5
    │                     })
    │   └─> Applique poids: get_regime_weights("BALANCED")
    │       → OF=35%, FP=35%, VWAP=30%
    │
    └─> FusionManager.fuse(orderflow, footprint, vwap_result)
        └─> Score final avec poids adaptatifs
```

### Avec 25 barres (nouveau système)

```
SCALPING Thread (cycle 5s)
    │
    ├─> get_rates('XAUUSD', M1, 25) → df_m1_25_bars  ⚠️ COURT
    │
    ├─> ⚠️ PROBLÈME: PhaseObserver ne peut plus calculer régime précis
    │
    └─> ❓ SOLUTION: ???
```

---

## SOLUTIONS D'ADAPTATION

### Option 1: Détection de Régime Simplifiée (20-25 barres)

**Principe:** Créer une version "lite" du régime utilisant uniquement des indicateurs compatibles avec 25 barres.

#### Indicateurs Simplifiés

1. **Tendance : Simple Price Slope** (remplace ADX)
   - Régression linéaire sur 20 barres
   - Pente positive forte → TRENDING UP
   - Pente négative forte → TRENDING DOWN
   - Pente faible → BALANCED

2. **Volatilité : ATR** (remplace Garman-Klass)
   - ATR sur 14 barres (OK avec 25 barres)
   - ATR > percentile 75% → High volatility
   - ATR < percentile 25% → Low volatility

3. **Volume : Simple Ratio** (OK avec 25 barres)
   - Volume actuel / Moyenne 14 barres
   - Ratio > 1.5 → Spike institutionnel

4. **Momentum : Delta Price** (nouveau)
   - (Close[-1] - Close[-20]) / Close[-20]
   - % change fort → Trending
   - % change faible → Range

#### Mapping Simplifié

```python
def detect_regime_lite(df_25_bars):
    """
    Détection régime simplifiée pour 25 barres M1

    Retourne un des 4 régimes VWAP directement:
    - TRENDING
    - BALANCED
    - ACCUMULATION
    - TRANSITIONAL
    """

    # 1. Calcul pente (régression linéaire 20 barres)
    slope = calculate_linear_regression_slope(df_25_bars['close'].tail(20))

    # 2. Calcul ATR (14 périodes)
    atr = calculate_atr(df_25_bars, period=14)
    atr_percentile = get_percentile(atr, window=20)

    # 3. Calcul momentum prix
    price_change_pct = (df_25_bars['close'].iloc[-1] - df_25_bars['close'].iloc[-20]) / df_25_bars['close'].iloc[-20]

    # 4. Calcul volume ratio
    volume_ma = df_25_bars['tick_volume'].tail(14).mean()
    volume_ratio = df_25_bars['tick_volume'].iloc[-1] / volume_ma

    # === DÉCISION RÉGIME ===

    # TRENDING: Pente forte + momentum fort
    if abs(slope) > 0.0005 and abs(price_change_pct) > 0.005:
        return "TRENDING", 0.8

    # TRANSITIONAL: Volatilité extrême
    if atr_percentile > 90 or atr_percentile < 10:
        return "TRANSITIONAL", 0.7

    # ACCUMULATION: Pente modérée + volume institutionnel
    if 0.0002 < abs(slope) < 0.0005 and volume_ratio > 1.5:
        return "ACCUMULATION", 0.65

    # BALANCED: Par défaut (range neutre)
    return "BALANCED", 0.5
```

**Avantages:**
- ✅ Fonctionne avec 25 barres
- ✅ Rapide à calculer
- ✅ Indépendant du PhaseObserver

**Inconvénients:**
- ⚠️ Moins précis que la version complète (200 barres)
- ⚠️ Pas de détection breakout/compression fine

---

### Option 2: Récupérer Régime depuis Thread LIQUIDITY

**Principe:** Le LIQUIDITY Thread continue d'analyser 200 barres pour EURUSD/GBPUSD. On peut utiliser son régime comme proxy pour XAUUSD.

```
LIQUIDITY Thread (60s) - 200 barres
    ├─> Analyse EURUSD (200 barres) → regime_eurusd
    ├─> Analyse GBPUSD (200 barres) → regime_gbpusd
    └─> Stocke dans cache global

SCALPING Thread (5s) - 25 barres
    ├─> Analyse XAUUSD (25 barres - orderflow/footprint)
    ├─> Lit regime depuis cache (EURUSD ou GBPUSD)
    └─> Utilise comme proxy pour XAUUSD
```

**Avantages:**
- ✅ Régime précis (calculé sur 200 barres)
- ✅ Pas de réécriture de code
- ✅ Facile à implémenter

**Inconvénients:**
- ❌ Régime EURUSD/GBPUSD peut différer de XAUUSD
- ❌ Dépendance entre threads
- ❌ Latence (régime mis à jour toutes les 60s)

---

### Option 3: Système Hybride (RECOMMANDÉ)

**Principe:** Combiner les deux approches.

```
SCALPING Thread (5s)
    │
    ├─> Analyse 25 barres XAUUSD
    │   ├─> Détection régime LITE (simplifiée sur 25 barres)
    │   └─> regime_fast, confidence_fast
    │
    ├─> Lit régime depuis cache LIQUIDITY Thread (si disponible)
    │   └─> regime_slow, confidence_slow (calculé sur 200 barres)
    │
    └─> Fusion intelligente:
        IF regime_slow disponible ET récent (< 60s):
            • confidence_fast faible (< 0.6) → Utilise regime_slow
            • confidence_fast forte (≥ 0.6) → Utilise regime_fast
            • Moyenne pondérée si confidence_fast modérée
        ELSE:
            • Utilise uniquement regime_fast
```

**Avantages:**
- ✅ Meilleure des deux approches
- ✅ Fallback robuste
- ✅ Adaptatif selon la situation

**Inconvénients:**
- ⚠️ Plus complexe à implémenter

---

## IMPLÉMENTATION RECOMMANDÉE

### Approche: Option 3 (Système Hybride)

#### Étape 1: Créer Détection Régime Lite

**Fichier:** `/home/workdev/sniper_x_dev/phase_observer/regime_detector_lite.py`

```python
"""
Détecteur de régime simplifié pour analyse sur 20-25 barres M1
Compatible avec le nouveau SCALPING Thread réactif
"""

import pandas as pd
import numpy as np
from typing import Tuple

class RegimeDetectorLite:
    """
    Détection de régime de marché simplifiée
    Utilise uniquement des indicateurs compatibles avec 20-25 barres
    """

    def __init__(self, logger=None):
        self.logger = logger

    def detect_regime(
        self,
        df: pd.DataFrame,
        min_bars: int = 20
    ) -> Tuple[str, float]:
        """
        Détecte le régime de marché sur DataFrame court (20-25 barres)

        Args:
            df: DataFrame OHLC (minimum 20 barres recommandé)
            min_bars: Nombre minimum de barres requis

        Returns:
            Tuple (regime, confidence)
            - regime: "TRENDING" | "BALANCED" | "ACCUMULATION" | "TRANSITIONAL"
            - confidence: 0.0-1.0
        """

        if df is None or len(df) < min_bars:
            if self.logger:
                self.logger.warning(
                    f"[RegimeLite] Données insuffisantes: {len(df) if df is not None else 0} bars < {min_bars}"
                )
            return "BALANCED", 0.3  # Fallback faible confiance

        try:
            # === 1. CALCUL INDICATEURS ===

            # 1a. Pente prix (régression linéaire sur 20 barres)
            closes = df['close'].tail(20).values
            x = np.arange(len(closes))
            slope, _ = np.polyfit(x, closes, 1)
            slope_pct = slope / closes[0]  # Pente en %

            # 1b. Momentum prix (variation 20 barres)
            price_change_pct = (df['close'].iloc[-1] - df['close'].iloc[-20]) / df['close'].iloc[-20]

            # 1c. ATR (14 périodes)
            atr = self._calculate_atr(df, period=14)
            atr_current = atr.iloc[-1] if not atr.empty else 0
            atr_mean = atr.tail(14).mean() if len(atr) >= 14 else 0
            atr_ratio = atr_current / atr_mean if atr_mean > 0 else 1.0

            # 1d. Volume ratio
            if 'tick_volume' in df.columns:
                volume_ma = df['tick_volume'].tail(14).mean()
                volume_current = df['tick_volume'].iloc[-1]
                volume_ratio = volume_current / volume_ma if volume_ma > 0 else 1.0
            else:
                volume_ratio = 1.0

            # 1e. Range compression (high-low ratio)
            recent_ranges = (df['high'] - df['low']).tail(5)
            range_mean = recent_ranges.mean()
            range_current = recent_ranges.iloc[-1]
            range_compression = range_current / range_mean if range_mean > 0 else 1.0

            # === 2. DÉCISION RÉGIME ===

            # TRENDING: Pente forte + momentum fort + ATR normal/élevé
            if abs(slope_pct) > 0.0005 and abs(price_change_pct) > 0.005:
                # Trending confirmé par momentum
                confidence = min(0.9, 0.7 + abs(price_change_pct) * 20)
                return "TRENDING", confidence

            # TRANSITIONAL: Volatilité extrême
            if atr_ratio > 2.0:  # ATR > 2x moyenne
                confidence = min(0.85, 0.5 + (atr_ratio - 2.0) / 2)
                return "TRANSITIONAL", confidence

            # COMPRESSION (pré-breakout) → Mappe vers TRANSITIONAL
            if range_compression < 0.5 and atr_ratio < 0.7:
                return "TRANSITIONAL", 0.65

            # ACCUMULATION: Pente modérée + volume institutionnel
            if 0.0002 < abs(slope_pct) < 0.0005 and volume_ratio > 1.5:
                confidence = min(0.75, 0.5 + (volume_ratio - 1.5) / 2)
                return "ACCUMULATION", confidence

            # BALANCED: Par défaut (range neutre)
            # Confiance dépend de la stabilité du range
            range_stability = 1.0 - abs(slope_pct) / 0.001  # Plus la pente est faible, plus stable
            confidence = max(0.4, min(0.6, range_stability))
            return "BALANCED", confidence

        except Exception as e:
            if self.logger:
                self.logger.error(f"[RegimeLite] Erreur détection: {e}", exc_info=True)
            return "BALANCED", 0.3

    def _calculate_atr(self, df: pd.DataFrame, period: int = 14) -> pd.Series:
        """Calcule ATR (Average True Range)"""
        high = df['high']
        low = df['low']
        close = df['close']

        tr1 = high - low
        tr2 = (high - close.shift()).abs()
        tr3 = (low - close.shift()).abs()

        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = tr.rolling(window=period, min_periods=1).mean()

        return atr
```

#### Étape 2: Intégration Système Hybride

**Fichier:** `/home/workdev/sniper_x_dev/phase_observer/regime_resolver.py`

```python
"""
RegimeResolver - Système hybride de résolution de régime
Combine détection rapide (25 barres) et lente (200 barres depuis cache)
"""

import time
from typing import Dict, Any, Optional, Tuple
from .regime_detector_lite import RegimeDetectorLite

class RegimeResolver:
    """
    Résout le régime de marché en combinant:
    - Détection rapide sur 25 barres (regime_fast)
    - Régime lent depuis cache LIQUIDITY Thread (regime_slow)
    """

    def __init__(self, logger=None):
        self.logger = logger
        self.detector_lite = RegimeDetectorLite(logger=logger)
        self.regime_cache = {}  # Cache global partagé avec LIQUIDITY Thread

    def resolve_regime(
        self,
        df_short: pd.DataFrame,  # 25 barres XAUUSD
        asset: str = "XAUUSD",
        cache_ttl: float = 60.0  # TTL cache en secondes
    ) -> Tuple[str, float, Dict[str, Any]]:
        """
        Résout le régime en combinant détection rapide et cache lent

        Args:
            df_short: DataFrame court (20-25 barres)
            asset: Symbole (XAUUSD)
            cache_ttl: Durée de validité du cache (secondes)

        Returns:
            Tuple (regime_final, confidence_final, metadata)
            - regime: "TRENDING" | "BALANCED" | "ACCUMULATION" | "TRANSITIONAL"
            - confidence: 0.0-1.0
            - metadata: Informations de debug
        """

        # === 1. DÉTECTION RAPIDE (25 barres) ===
        regime_fast, confidence_fast = self.detector_lite.detect_regime(df_short)

        if self.logger:
            self.logger.debug(
                f"[RegimeResolver][{asset}] Fast detection: "
                f"regime={regime_fast}, confidence={confidence_fast:.2f}"
            )

        # === 2. LECTURE CACHE LENT (200 barres depuis LIQUIDITY Thread) ===
        regime_slow, confidence_slow, cache_age = self._read_cache_regime(asset, cache_ttl)

        if regime_slow is not None:
            if self.logger:
                self.logger.debug(
                    f"[RegimeResolver][{asset}] Slow cache: "
                    f"regime={regime_slow}, confidence={confidence_slow:.2f}, age={cache_age:.1f}s"
                )

        # === 3. FUSION INTELLIGENTE ===
        regime_final, confidence_final, source = self._fuse_regimes(
            regime_fast, confidence_fast,
            regime_slow, confidence_slow,
            cache_age
        )

        if self.logger:
            self.logger.info(
                f"[RegimeResolver][{asset}] ✅ Final: "
                f"regime={regime_final}, confidence={confidence_final:.2f}, source={source}"
            )

        # === 4. MÉTADONNÉES ===
        metadata = {
            "regime_fast": regime_fast,
            "confidence_fast": confidence_fast,
            "regime_slow": regime_slow,
            "confidence_slow": confidence_slow,
            "cache_age_s": cache_age,
            "source": source,
            "timestamp": time.time()
        }

        return regime_final, confidence_final, metadata

    def _read_cache_regime(
        self,
        asset: str,
        ttl: float
    ) -> Tuple[Optional[str], Optional[float], float]:
        """
        Lit le régime depuis le cache global (LIQUIDITY Thread)

        Returns:
            Tuple (regime, confidence, age_seconds)
            Si cache invalide/absent: (None, None, float('inf'))
        """

        # Tentative lecture depuis cache global
        cache_key = f"regime_{asset}"
        cached_data = self.regime_cache.get(cache_key)

        if cached_data is None:
            # Pas de données en cache
            return None, None, float('inf')

        # Vérifier fraîcheur
        timestamp = cached_data.get("timestamp", 0)
        age = time.time() - timestamp

        if age > ttl:
            # Cache expiré
            if self.logger:
                self.logger.debug(
                    f"[RegimeResolver][{asset}] Cache expiré: age={age:.1f}s > ttl={ttl}s"
                )
            return None, None, age

        # Cache valide
        regime = cached_data.get("regime")
        confidence = cached_data.get("confidence", 0.5)

        return regime, confidence, age

    def _fuse_regimes(
        self,
        regime_fast: str,
        confidence_fast: float,
        regime_slow: Optional[str],
        confidence_slow: Optional[float],
        cache_age: float
    ) -> Tuple[str, float, str]:
        """
        Fusionne les régimes rapide et lent selon la logique hybride

        Returns:
            Tuple (regime_final, confidence_final, source)
        """

        # === CAS 1: Pas de cache lent disponible ===
        if regime_slow is None:
            return regime_fast, confidence_fast, "FAST_ONLY"

        # === CAS 2: Cache lent disponible et récent ===

        # 2a. Confidence rapide très forte → Utilise rapide (marché change vite)
        if confidence_fast >= 0.75:
            return regime_fast, confidence_fast, "FAST_HIGH_CONF"

        # 2b. Confidence rapide faible → Utilise lent (plus fiable)
        if confidence_fast < 0.5 and confidence_slow >= 0.6:
            return regime_slow, confidence_slow, "SLOW_FALLBACK"

        # 2c. Les deux régimes sont identiques → Renforce confiance
        if regime_fast == regime_slow:
            confidence_final = min(1.0, (confidence_fast + confidence_slow) / 2 + 0.1)
            return regime_fast, confidence_final, "BOTH_ALIGNED"

        # 2d. Régimes différents → Moyenne pondérée selon confiances
        # Favorise légèrement le rapide (plus réactif)
        weight_fast = 0.6
        weight_slow = 0.4

        if confidence_fast > confidence_slow:
            # Rapide plus confiant → Utilise rapide
            return regime_fast, confidence_fast * 0.9, "FAST_WEIGHTED"
        else:
            # Lent plus confiant → Utilise lent
            return regime_slow, confidence_slow * 0.9, "SLOW_WEIGHTED"

    def update_cache(self, asset: str, regime: str, confidence: float):
        """
        Met à jour le cache de régime (appelé par LIQUIDITY Thread)

        Args:
            asset: Symbole (XAUUSD, EURUSD, GBPUSD)
            regime: Régime détecté
            confidence: Confiance 0.0-1.0
        """
        cache_key = f"regime_{asset}"
        self.regime_cache[cache_key] = {
            "regime": regime,
            "confidence": confidence,
            "timestamp": time.time()
        }

        if self.logger:
            self.logger.debug(
                f"[RegimeResolver] Cache updated: {asset} → {regime} ({confidence:.2f})"
            )
```

#### Étape 3: Intégration dans SCALPING Thread

```python
# Dans run_bot.py - SCALPING Thread

from phase_observer.regime_resolver import RegimeResolver

# Initialiser RegimeResolver (une fois au démarrage)
regime_resolver = RegimeResolver(logger=logger)

def scalping_fast_thread():
    while not stop_event.is_set():
        try:
            # 1. Récupérer 25 barres M1
            df_m1_25 = mt5_connector.get_rates('XAUUSD', mt5.TIMEFRAME_M1, bars=25)

            # 2. Résoudre régime (système hybride)
            vwap_regime, regime_confidence, regime_metadata = regime_resolver.resolve_regime(
                df_short=df_m1_25,
                asset='XAUUSD',
                cache_ttl=60.0  # Cache valide 60s
            )

            # 3. Construire context pour VWAP
            context = {
                "phase_observer_regime": None,  # Non utilisé (on a déjà le régime VWAP)
                "vwap_regime": vwap_regime,     # Régime VWAP direct
                "regime_confidence": regime_confidence,
                "regime_metadata": regime_metadata
            }

            # 4. Analyser VWAP avec le régime
            vwap_result = vwap_analyzer.analyze(
                df=df_m1_25,
                current_price=current_price,
                context=context
            )

            # 5. Continuer avec OrderFlow V6, Footprint V6, etc.
            # ...

            time.sleep(5.0)

        except Exception as e:
            logger.error(f"Erreur SCALPING Thread: {e}")
            time.sleep(5.0)
```

#### Étape 4: Mise à Jour depuis LIQUIDITY Thread

```python
# Dans run_bot.py - LIQUIDITY Thread

def liquidity_main_thread():
    while not stop_event.is_set():
        try:
            # 1. Analyser EURUSD/GBPUSD avec 200 barres (comme avant)
            for asset in ['EURUSD', 'GBPUSD']:
                df_200 = mt5_connector.get_rates(asset, mt5.TIMEFRAME_M1, bars=200)

                # PhaseObserver détecte régime (version complète)
                phase_observer_regime = phase_observer.detect_market_regime(df_200)

                # Mapper vers régime VWAP
                vwap_regime, confidence = regime_mapper.map_regime(phase_observer_regime)

                # 2. Mettre à jour cache pour SCALPING Thread
                regime_resolver.update_cache(
                    asset=asset,
                    regime=vwap_regime,
                    confidence=confidence
                )

            # 3. Optionnel: Mettre à jour cache pour XAUUSD aussi
            # (utile si on trade XAUUSD aussi dans LIQUIDITY)

            time.sleep(60.0)

        except Exception as e:
            logger.error(f"Erreur LIQUIDITY Thread: {e}")
            time.sleep(60.0)
```

---

## CODE D'IMPLÉMENTATION

### Fichier 1: `phase_observer/regime_detector_lite.py`

Voir section "Étape 1" ci-dessus (code complet fourni).

### Fichier 2: `phase_observer/regime_resolver.py`

Voir section "Étape 2" ci-dessus (code complet fourni).

### Fichier 3: Modifications `run_bot.py`

```python
# === IMPORTS ===
from phase_observer.regime_resolver import RegimeResolver

# === INITIALISATION GLOBALE (avant threads) ===
regime_resolver = RegimeResolver(logger=logger)

# === SCALPING THREAD (ligne ~3155) ===
def scalping_fast_thread(...):
    # ... (code existant)

    # AJOUT: Résolution régime
    vwap_regime, regime_confidence, regime_metadata = regime_resolver.resolve_regime(
        df_short=df_m1_25,
        asset='XAUUSD',
        cache_ttl=60.0
    )

    # AJOUT: Context pour VWAP
    context = {
        "vwap_regime": vwap_regime,
        "regime_confidence": regime_confidence,
        "regime_metadata": regime_metadata
    }

    # Reste du code...

# === LIQUIDITY THREAD (ligne ~3476) ===
def liquidity_main_thread(...):
    # ... (code existant d'analyse EURUSD/GBPUSD)

    # AJOUT: Mise à jour cache régime
    for asset in ['EURUSD', 'GBPUSD']:
        # ... (analyse PhaseObserver existante)

        # Mapper vers VWAP regime
        vwap_regime, confidence = regime_mapper.map_regime(
            phase_observer_regime,
            regime_strength
        )

        # Mettre à jour cache
        regime_resolver.update_cache(
            asset=asset,
            regime=vwap_regime,
            confidence=confidence
        )
```

---

## RÉSUMÉ

### Problème
- VWAP dynamique nécessite un régime de marché
- Régime actuellement calculé sur 200 barres (incompatible avec 25 barres)

### Solution: Système Hybride
1. **Détection rapide** sur 25 barres (RegimeDetectorLite)
   - Indicateurs simplifiés: Slope, ATR, Volume, Momentum
   - Précision acceptable, grande réactivité

2. **Cache lent** depuis LIQUIDITY Thread (200 barres)
   - Régime précis calculé toutes les 60s
   - Utilisé comme référence quand disponible

3. **Fusion intelligente** (RegimeResolver)
   - Combine les deux sources selon confiance
   - Privilégie le rapide si confiance forte
   - Fallback sur le lent si confiance faible

### Avantages
- ✅ Fonctionne avec 25 barres
- ✅ Maintient la précision (via cache)
- ✅ Grande réactivité (5s)
- ✅ Fallback robuste

### Fichiers à Créer
1. `phase_observer/regime_detector_lite.py` (nouveau)
2. `phase_observer/regime_resolver.py` (nouveau)
3. Modifications dans `run_bot.py` (SCALPING et LIQUIDITY threads)

---

*Document créé le 2025-12-10*
*Solution recommandée pour adaptation régime VWAP avec 25 barres M1*
