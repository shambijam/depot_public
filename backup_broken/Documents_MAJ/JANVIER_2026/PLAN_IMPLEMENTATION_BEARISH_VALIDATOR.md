# 📋 PLAN D'IMPLÉMENTATION - BEARISH VALIDATOR M1

**Date**: 14 Janvier 2026
**Version**: v1.0 - Scalping Burst Mode
**Budget Performance**: < 300ms par validation (toutes les 10 cycles)

---

## 🎯 OBJECTIF

Implémenter un **validateur asymétrique** pour améliorer les trades BEARISH en combinant:
1. **InstitutionalReversalDetector** → Détecte QUAND renverser
2. **PriceMemoryAnalyzer (M1)** → Détecte OÙ le prix va réagir
3. **Timing Validator** → Détecte le MOMENT optimal (30-45s)

---

## 📁 FICHIERS À MODIFIER

### **1. run_bot.py** (Principal)
**Lignes**: 3780-3900 (décision finale)
**Modifications**:
- ✅ Créer buffers historiques CVD/Delta/Volume
- ✅ Alimenter buffers à chaque cycle
- ✅ Appeler reversal detector (1x/10 cycles)
- ✅ Appeler bearish validator si direction=SELL
- ✅ Ajuster score composite avec boost/malus

### **2. phase_observer/price_memory_analyzer.py**
**Nouvelle méthode**: `detect_micro_resistance_m1()` (déjà créée ligne 679-834)
**Modifications**:
- ✅ Méthode déjà implémentée
- ⚠️ À tester avec données réelles M1

### **3. phase_observer/bearish_scalping_validator.py** (NOUVEAU)
**À créer**: Validateur M1 ultra-rapide
**Rôle**: Combine reversal + micro-résistances + timing

### **4. phase_observer/institutional_reversal_detector.py**
**Statut**: ✅ Déjà standalone (v2.1)
**Modifications**: Aucune (prêt à l'emploi)

---

## 🔧 ÉTAPE 1: BUFFERS HISTORIQUES (run_bot.py)

### **1.1 Initialisation des buffers**

**Fichier**: `run_bot.py`
**Ligne**: Après initialisation de PhaseObserver (~ligne 3200)

```python
# === BUFFERS HISTORIQUES POUR REVERSAL DETECTOR ===
from collections import deque

self.cvd_history = {
    'USDJPY': deque(maxlen=100),
    'NAS100': deque(maxlen=100),
    'GBPUSD': deque(maxlen=100)
}

self.delta_history = {
    'USDJPY': deque(maxlen=100),
    'NAS100': deque(maxlen=100),
    'GBPUSD': deque(maxlen=100)
}

self.volume_history = {
    'USDJPY': deque(maxlen=100),
    'NAS100': deque(maxlen=100),
    'GBPUSD': deque(maxlen=100)
}

# Cache du dernier check reversal (éviter recalculs)
self.last_reversal_check = {
    'USDJPY': None,
    'NAS100': None,
    'GBPUSD': None
}

# Compteur pour appeler reversal detector 1x/10 cycles
self.reversal_check_counter = {
    'USDJPY': 0,
    'NAS100': 0,
    'GBPUSD': 0
}

logger.info("✅ [BEARISH_VALIDATOR] Buffers historiques initialisés (100 valeurs max)")
```

### **1.2 Alimentation des buffers**

**Fichier**: `run_bot.py`
**Ligne**: Après calcul orderflow (~ligne 3700)

```python
# === ALIMENTATION BUFFERS HISTORIQUES ===
# Récupérer les valeurs actuelles depuis orderflow_result
current_cvd = orderflow_result.get('cvd', 0.0)
current_delta = orderflow_result.get('delta', 0.0)
current_volume = df_m1.iloc[-1]['tick_volume'] if 'tick_volume' in df_m1.columns else 0.0

# Ajouter aux buffers
self.cvd_history[symbol].append(current_cvd)
self.delta_history[symbol].append(current_delta)
self.volume_history[symbol].append(current_volume)

logger.debug(
    f"[BUFFER_FEED][{symbol}] CVD={current_cvd:.2f} | "
    f"Delta={current_delta:.0f} | Volume={current_volume:.0f} | "
    f"Buffer size: {len(self.cvd_history[symbol])}"
)
```

---

## 🔧 ÉTAPE 2: REVERSAL DETECTOR INTÉGRATION

### **2.1 Appel du reversal detector**

**Fichier**: `run_bot.py`
**Ligne**: Avant décision finale (~ligne 3750)

```python
# === REVERSAL DETECTOR (1x/10 cycles = 25s) ===
self.reversal_check_counter[symbol] += 1

if self.reversal_check_counter[symbol] >= 10:
    self.reversal_check_counter[symbol] = 0

    # Vérifier qu'on a assez de données (minimum 30 valeurs)
    if len(self.cvd_history[symbol]) >= 30:
        try:
            market_data = {
                'candles_m5': df_m5,
                'candles_m1': df_m1,
                'cvd_values': list(self.cvd_history[symbol]),
                'delta_values': list(self.delta_history[symbol]),
                'volume_values': list(self.volume_history[symbol])
            }

            # Appeler le détecteur
            reversal_result = phase_observer.reversal_detector.detect_reversal(market_data)
            self.last_reversal_check[symbol] = reversal_result

            # Log du résultat
            logger.critical(
                f"🏛️ [REVERSAL_CHECK][{symbol}] "
                f"Score={reversal_result['institutional_score']:.1f}/100 | "
                f"Conviction={reversal_result['conviction_level']} | "
                f"Trend={reversal_result['new_trend']} | "
                f"Reversal={reversal_result['reversal_detected']}"
            )

        except Exception as e:
            logger.error(f"[REVERSAL_DETECTOR][{symbol}] Erreur: {e}")
            self.last_reversal_check[symbol] = None
    else:
        logger.debug(
            f"[REVERSAL_DETECTOR][{symbol}] Pas assez de données "
            f"({len(self.cvd_history[symbol])} < 30)"
        )
```

---

## 🔧 ÉTAPE 3: BEARISH VALIDATOR (Nouveau fichier)

### **3.1 Créer le fichier**

**Fichier**: `phase_observer/bearish_scalping_validator.py`

```python
"""
🐻 BEARISH SCALPING VALIDATOR M1 v1.0
Validation croisée ultra-rapide pour trades BEARISH en scalping burst mode

Créé: 14 JAN 2026
Budget: < 150ms par validation

Architecture:
- FAST-TRACK: < 5s (reversal score élevé + résistance très proche)
- STANDARD: 10-15s (validation complète reversal + micro-résistance + timing)
"""

import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Any
from dataclasses import dataclass
import logging
from datetime import datetime


@dataclass
class BearishValidationResult:
    """Résultat de validation BEARISH"""
    validation_level: str  # STRONG / MODERATE / WEAK / REJECTED
    confidence: float      # 0.0-1.0
    score_boost: float     # -15 à +20 (ajustement du score composite)
    reasons: List[str]     # Raisons détaillées
    reversal_score: float  # 0-100
    micro_resistance: Optional[Dict]  # Données micro-résistance
    timing_quality: str    # OPTIMAL / ACCEPTABLE / LATE
    should_take_trade: bool


class BearishScalpingValidator:
    """
    🐻 VALIDATEUR BEARISH M1 ULTRA-RAPIDE

    Optimisé pour scalping burst (< 22s total):
    - Phase 1 (2-3s): Signal rapide (delta négatif)
    - Phase 2 (5-8s): Validation sniper (micro-résistance M1)
    - Phase 3 (10-15s): Confirmation institutionnelle

    Budget: < 150ms par validation
    """

    def __init__(
        self,
        reversal_detector,
        price_memory_analyzer,
        config: Optional[Dict] = None,
        logger=None
    ):
        self.reversal_detector = reversal_detector
        self.price_memory = price_memory_analyzer
        self.logger = logger or logging.getLogger(__name__)

        # Config par défaut (M1 ultra-rapide)
        self.config = config or {
            # === STRONG VALIDATION (Fast-track) ===
            "strong": {
                "min_reversal_score": 70,
                "min_micro_resistance_distance_pips": 3.0,
                "max_micro_resistance_distance_pips": 10.0,
                "min_bounce_probability": 0.65,
                "max_resistance_age_minutes": 10,
                "timing_window": (30, 45),  # 30-45s optimal
                "score_boost": +20.0
            },

            # === MODERATE VALIDATION (Standard) ===
            "moderate": {
                "min_reversal_score": 55,
                "min_micro_resistance_distance_pips": 5.0,
                "max_micro_resistance_distance_pips": 15.0,
                "min_bounce_probability": 0.55,
                "max_resistance_age_minutes": 15,
                "timing_window": (20, 48),  # 20-48s acceptable
                "score_boost": +10.0
            },

            # === WEAK VALIDATION (Minimal) ===
            "weak": {
                "min_reversal_score": 45,
                "min_micro_resistance_distance_pips": 8.0,
                "max_micro_resistance_distance_pips": 20.0,
                "min_bounce_probability": 0.45,
                "max_resistance_age_minutes": 20,
                "timing_window": (10, 50),  # 10-50s large window
                "score_boost": +3.0
            },

            # === ASSET-SPECIFIC THRESHOLDS ===
            "assets": {
                "NAS100": {
                    "pip_multiplier": 0.25,  # NAS100 points × 0.25
                    "min_reversal_score": 65,  # Plus strict
                },
                "USDJPY": {
                    "pip_multiplier": 1.0,
                    "min_reversal_score": 60,
                },
                "GBPUSD": {
                    "pip_multiplier": 1.0,
                    "min_reversal_score": 60,
                }
            }
        }

    def validate_bearish_trade(
        self,
        symbol: str,
        current_price: float,
        current_time: datetime,
        candle_open_time: datetime,
        historical_data_m1: pd.DataFrame,
        reversal_result: Dict
    ) -> BearishValidationResult:
        """
        🎯 VALIDATION BEARISH PRINCIPALE

        Args:
            symbol: Asset (NAS100, USDJPY, GBPUSD)
            current_price: Prix actuel
            current_time: Timestamp actuel
            candle_open_time: Ouverture bougie M1 actuelle
            historical_data_m1: DataFrame M1 (50 bougies)
            reversal_result: Résultat du reversal detector

        Returns:
            BearishValidationResult
        """

        # === ÉTAPE 1: REVERSAL DETECTOR ===
        if not reversal_result:
            return self._rejected("NO_REVERSAL_DATA", "Reversal detector pas exécuté")

        reversal_score = reversal_result.get('institutional_score', 0.0)
        reversal_trend = reversal_result.get('new_trend', 'NEUTRAL')
        reversal_conviction = reversal_result.get('conviction_level', 'LOW')

        # Vérifier direction BEARISH
        if reversal_trend != 'BEARISH':
            return self._rejected(
                "WRONG_DIRECTION",
                f"Reversal trend is {reversal_trend}, not BEARISH"
            )

        # === ÉTAPE 2: MICRO-RÉSISTANCE M1 ===
        try:
            micro_resistance = self.price_memory.detect_micro_resistance_m1(
                historical_data=historical_data_m1,
                current_price=current_price,
                lookback_minutes=15
            )
        except Exception as e:
            self.logger.error(f"[BEARISH_VALIDATOR][{symbol}] Erreur micro-résistance: {e}")
            micro_resistance = None

        # === ÉTAPE 3: TIMING QUALITY ===
        candle_age_seconds = (current_time - candle_open_time).total_seconds()
        timing_quality = self._evaluate_timing(candle_age_seconds)

        # === ÉTAPE 4: DÉTERMINER NIVEAU DE VALIDATION ===
        validation_level, confidence, score_boost, reasons = self._determine_validation_level(
            symbol=symbol,
            reversal_score=reversal_score,
            reversal_conviction=reversal_conviction,
            micro_resistance=micro_resistance,
            timing_quality=timing_quality,
            candle_age_seconds=candle_age_seconds
        )

        # === ÉTAPE 5: LOG DÉTAILLÉ ===
        self.logger.critical(
            f"🐻 [BEARISH_VALIDATION][{symbol}] "
            f"Level={validation_level} | "
            f"Confidence={confidence:.2f} | "
            f"Boost={score_boost:+.1f} | "
            f"Reversal={reversal_score:.0f}/100 ({reversal_conviction}) | "
            f"Resistance={micro_resistance['distance_pips'] if micro_resistance else 'N/A':.1f}p | "
            f"Timing={timing_quality} ({candle_age_seconds:.0f}s)"
        )

        return BearishValidationResult(
            validation_level=validation_level,
            confidence=confidence,
            score_boost=score_boost,
            reasons=reasons,
            reversal_score=reversal_score,
            micro_resistance=micro_resistance,
            timing_quality=timing_quality,
            should_take_trade=(validation_level in ['STRONG', 'MODERATE'])
        )

    def _evaluate_timing(self, candle_age_seconds: float) -> str:
        """Évalue la qualité du timing"""
        if 30 <= candle_age_seconds <= 45:
            return "OPTIMAL"
        elif 20 <= candle_age_seconds <= 50:
            return "ACCEPTABLE"
        else:
            return "LATE"

    def _determine_validation_level(
        self,
        symbol: str,
        reversal_score: float,
        reversal_conviction: str,
        micro_resistance: Optional[Dict],
        timing_quality: str,
        candle_age_seconds: float
    ) -> tuple:
        """
        Détermine le niveau de validation et le boost à appliquer

        Returns:
            tuple: (validation_level, confidence, score_boost, reasons)
        """
        reasons = []

        # Config asset-specific
        asset_config = self.config['assets'].get(symbol, {})
        min_reversal_override = asset_config.get('min_reversal_score', None)

        # === STRONG VALIDATION (Fast-track) ===
        strong_cfg = self.config['strong']
        min_reversal_strong = min_reversal_override or strong_cfg['min_reversal_score']

        if reversal_score >= min_reversal_strong and micro_resistance:
            distance_pips = micro_resistance['distance_pips']
            bounce_prob = micro_resistance['bounce_probability']
            age_minutes = micro_resistance['age_minutes']

            if (strong_cfg['min_micro_resistance_distance_pips'] <= distance_pips <= strong_cfg['max_micro_resistance_distance_pips'] and
                bounce_prob >= strong_cfg['min_bounce_probability'] and
                age_minutes <= strong_cfg['max_resistance_age_minutes'] and
                timing_quality in ['OPTIMAL', 'ACCEPTABLE']):

                reasons.append(f"✅ Reversal score ÉLEVÉ: {reversal_score:.0f}/100 ({reversal_conviction})")
                reasons.append(f"✅ Micro-résistance PROCHE: {distance_pips:.1f} pips")
                reasons.append(f"✅ Bounce probability: {bounce_prob:.0%}")
                reasons.append(f"✅ Timing: {timing_quality} ({candle_age_seconds:.0f}s)")

                return ('STRONG', 0.9, strong_cfg['score_boost'], reasons)

        # === MODERATE VALIDATION (Standard) ===
        moderate_cfg = self.config['moderate']
        min_reversal_moderate = min_reversal_override or moderate_cfg['min_reversal_score']

        if reversal_score >= min_reversal_moderate:
            if micro_resistance:
                distance_pips = micro_resistance['distance_pips']
                bounce_prob = micro_resistance['bounce_probability']
                age_minutes = micro_resistance['age_minutes']

                if (moderate_cfg['min_micro_resistance_distance_pips'] <= distance_pips <= moderate_cfg['max_micro_resistance_distance_pips'] and
                    bounce_prob >= moderate_cfg['min_bounce_probability'] and
                    age_minutes <= moderate_cfg['max_resistance_age_minutes']):

                    reasons.append(f"✓ Reversal score MODÉRÉ: {reversal_score:.0f}/100")
                    reasons.append(f"✓ Micro-résistance ACCEPTABLE: {distance_pips:.1f} pips")
                    reasons.append(f"✓ Bounce probability: {bounce_prob:.0%}")

                    return ('MODERATE', 0.7, moderate_cfg['score_boost'], reasons)

        # === WEAK VALIDATION (Minimal) ===
        weak_cfg = self.config['weak']

        if reversal_score >= weak_cfg['min_reversal_score']:
            reasons.append(f"⚠ Reversal score FAIBLE: {reversal_score:.0f}/100")

            if micro_resistance:
                reasons.append(f"⚠ Résistance éloignée: {micro_resistance['distance_pips']:.1f} pips")
            else:
                reasons.append(f"⚠ Aucune résistance proche")

            return ('WEAK', 0.5, weak_cfg['score_boost'], reasons)

        # === REJECTED ===
        reasons.append(f"❌ Reversal score insuffisant: {reversal_score:.0f}/100")
        reasons.append(f"❌ Pas de micro-résistance valide")

        return ('REJECTED', 0.0, -15.0, reasons)

    def _rejected(self, reason: str, details: str) -> BearishValidationResult:
        """Retourne une validation rejetée"""
        return BearishValidationResult(
            validation_level='REJECTED',
            confidence=0.0,
            score_boost=-15.0,
            reasons=[f"❌ {reason}: {details}"],
            reversal_score=0.0,
            micro_resistance=None,
            timing_quality='N/A',
            should_take_trade=False
        )
```

---

## 🔧 ÉTAPE 4: INTÉGRATION DANS DÉCISION FINALE

### **4.1 Modifier run_bot.py (décision finale)**

**Fichier**: `run_bot.py`
**Ligne**: ~3841-3900 (PRICE-FIRST DECISION)

```python
# === BEARISH VALIDATION (NOUVEAU) ===
bearish_boost = 0.0

if trade_decision['action'] == 'SELL' and hasattr(phase_observer, 'bearish_validator'):
    try:
        # Récupérer le résultat du reversal detector
        reversal_result = self.last_reversal_check.get(symbol, None)

        # Appeler le validateur BEARISH
        bearish_validation = phase_observer.bearish_validator.validate_bearish_trade(
            symbol=symbol,
            current_price=current_price,
            current_time=datetime.now(),
            candle_open_time=df_m1.iloc[-1]['time'],  # Bougie actuelle M1
            historical_data_m1=df_m1,
            reversal_result=reverish_result
        )

        # Appliquer le boost/malus
        bearish_boost = bearish_validation.score_boost

        # VETO si REJECTED
        if bearish_validation.validation_level == 'REJECTED':
            logger.warning(
                f"🛑 [BEARISH_VETO][{symbol}] Trade BEARISH rejeté | "
                f"Reasons: {' | '.join(bearish_validation.reasons)}"
            )
            return None  # Ne pas placer le trade

        logger.info(
            f"🐻 [BEARISH_BOOST][{symbol}] {bearish_validation.validation_level} | "
            f"Boost={bearish_boost:+.1f} | Confidence={bearish_validation.confidence:.2f}"
        )

    except Exception as e:
        logger.error(f"[BEARISH_VALIDATOR][{symbol}] Erreur: {e}")
        bearish_boost = 0.0

# === AJUSTER LE SCORE COMPOSITE ===
original_score = trade_decision.get('fusion_data', {}).get('orderflow_score', 0.0)
adjusted_score = original_score + bearish_boost

logger.critical(
    f"📊 [SCORE_ADJUSTMENT][{symbol}] "
    f"Original={original_score:.1f} | "
    f"Boost={bearish_boost:+.1f} | "
    f"Adjusted={adjusted_score:.1f}"
)

# Utiliser adjusted_score pour la décision finale
if adjusted_score >= seuil_minimum:
    # Placer le trade
    ...
else:
    # HOLD
    ...
```

---

## 📋 CHECKLIST D'INTÉGRATION

### **Phase 1: Préparation (30 min)**
- [ ] Créer buffers CVD/Delta/Volume dans run_bot.py
- [ ] Tester alimentation des buffers sur 1 minute
- [ ] Vérifier que les buffers se remplissent correctement (logs)

### **Phase 2: Reversal Detector (1h)**
- [ ] Instancier InstitutionalReversalDetector dans PhaseObserver
- [ ] Intégrer appel reversal detector (1x/10 cycles)
- [ ] Vérifier les logs CRITICAL reversal_check
- [ ] Tester avec minimum 30 valeurs historiques

### **Phase 3: Bearish Validator (1h30)**
- [ ] Créer fichier bearish_scalping_validator.py
- [ ] Implémenter BearishScalpingValidator
- [ ] Instancier dans PhaseObserver
- [ ] Tester validation BEARISH sur cas synthétique

### **Phase 4: Intégration Pipeline (1h)**
- [ ] Modifier décision finale dans run_bot.py
- [ ] Appliquer boost/malus au score composite
- [ ] Implémenter VETO si REJECTED
- [ ] Tester sur données réelles

### **Phase 5: Tuning (2h)**
- [ ] Ajuster seuils de validation (STRONG/MODERATE/WEAK)
- [ ] Vérifier performance < 300ms
- [ ] Tester avec NAS100 (cas le plus difficile)
- [ ] Valider avec backtest sur données historiques

---

## ⚙️ CONFIG RECOMMANDÉE PAR ASSET

### **NAS100** (Plus strict)
```python
"NAS100": {
    "pip_multiplier": 0.25,      # Points × 0.25
    "min_reversal_score": 65,    # Plus élevé
    "strong_boost": +25.0,       # Boost plus important
    "moderate_boost": +12.0
}
```

### **USDJPY** (Standard)
```python
"USDJPY": {
    "pip_multiplier": 1.0,
    "min_reversal_score": 60,
    "strong_boost": +20.0,
    "moderate_boost": +10.0
}
```

### **GBPUSD** (Standard)
```python
"GBPUSD": {
    "pip_multiplier": 1.0,
    "min_reversal_score": 60,
    "strong_boost": +20.0,
    "moderate_boost": +10.0
}
```

---

## 🧪 TESTS DE VALIDATION

### **Test 1: Buffers Historiques**
```bash
# Lancer le bot et vérifier les logs
grep "BUFFER_FEED" logs/bot_*.log | tail -50

# Vérifier que les buffers se remplissent
grep "Buffer size:" logs/bot_*.log
```

### **Test 2: Reversal Detector**
```bash
# Vérifier les appels du reversal detector
grep "REVERSAL_CHECK" logs/bot_*.log | tail -20

# Vérifier les scores institutionnels
grep "institutional_score" logs/bot_*.log
```

### **Test 3: Bearish Validation**
```bash
# Vérifier les validations BEARISH
grep "BEARISH_VALIDATION" logs/bot_*.log | tail -20

# Vérifier les boost appliqués
grep "SCORE_ADJUSTMENT" logs/bot_*.log
```

### **Test 4: Performance**
```bash
# Mesurer le temps d'exécution de la validation
grep "BEARISH_VALIDATION.*ms" logs/bot_*.log
```

---

## 📊 MÉTRIQUES À SURVEILLER

1. **Taux d'acceptation BEARISH**: Objectif 30-40% des BULLISH
2. **Boost moyen appliqué**: Objectif +10 à +15 points
3. **Temps de validation**: Objectif < 150ms
4. **Taux de VETO**: Objectif 20-30%

---

**Créé**: 14 Janvier 2026
**Auteur**: Claude (Anthropic)
**Statut**: READY TO IMPLEMENT
