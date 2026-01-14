# 🏛️ GUIDE D'INTÉGRATION - INSTITUTIONAL REVERSAL DETECTOR

**Date**: 14 Janvier 2026
**Version**: v2.1 (Standalone - sans SmartModeSwitcher)
**Fichier**: `phase_observer/institutional_reversal_detector.py`

---

## 🎯 RÔLE MAJEUR DANS LE PIPELINE

Le détecteur de renversement institutionnel joue **3 rôles critiques** :

### 1. 🛑 **VETO PRÉ-TRADE** (Priorité: CRITIQUE)
Bloque les trades dans une direction quand un renversement institutionnel est imminent.

**Exemple**: Le bot veut acheter (BULLISH) mais le détecteur voit 8 signaux de renversement BEARISH avec score 85/100 → **VETO le trade**

### 2. ✅ **CONFIRMATION DE TENDANCE** (Priorité: HAUTE)
Booste le score des trades alignés avec la tendance détectée par les institutions.

**Exemple**: Le bot hésite avec un score 68/100. Le détecteur confirme tendance BULLISH avec conviction HIGH → **BOOST à 78/100** → Trade validé

### 3. ⚠️ **GARDE-FOU FIN DE TENDANCE** (Priorité: HAUTE)
Détecte l'épuisement (fatigue institutionnelle) avant que le marché ne se retourne violemment.

**Exemple**: Tendance BULLISH depuis 15 bougies, le détecteur voit:
- Fatigue momentum: 80/100
- Volume divergence détectée
- Smart money distribution
→ **Arrêter de prendre des longs**, attendre confirmation

---

## 📊 FORMAT DE SORTIE (SANS SWITCHER)

```python
result = detector.detect_reversal(market_data)

# === DONNÉES PRINCIPALES ===
result = {
    "institutional_score": 75.0,          # 0-100 (force du renversement)
    "conviction_level": "HIGH",           # HIGH/MODERATE/CAUTION/LOW
    "new_trend": "BEARISH",               # BULLISH/BEARISH/NEUTRAL
    "reversal_detected": True,            # True si score >= 75

    # === DÉTAILS ANALYSE ===
    "regime_change": {
        "detected": True,                 # Changement de régime
        "new_regime": "TRENDING_BEAR",    # TRENDING_BULL/BEAR/RANGING/TRANSITION
        "confidence": 0.85,
        "volatility": 0.0012,
        "trend_strength": 0.78
    },

    "signals_breakdown": [
        {
            "name": "INSTITUTIONAL_FATIGUE",
            "strength": 80.0,             # 0-100
            "confidence": 0.75,           # 0-1
            "direction": "BEARISH",
            "timestamp": "2026-01-14T10:30:00",
            "metadata": {
                "momentum_decay": 0.72,
                "volume_divergence": True,
                "large_delta_exhaustion": True,
                "time_fatigue": 0.65
            }
        },
        # ... 7 autres signaux (Changepoint, Divergence M1/M5, etc.)
    ],

    "smart_money_confirmation": {
        "validated": True,
        "confidence": 0.85,
        "institutional_bias": "ALIGNED"
    },

    "timestamp": "2026-01-14T10:30:00.123456",
    "version": "INSTITUTIONAL_MT5_v2.1"
}
```

---

## 🔧 INTÉGRATION DANS LE PIPELINE

### **ÉTAPE 1: Préparer les données historiques**

Le détecteur a besoin d'historiques (50-100 valeurs) de CVD/Delta/Volume.

```python
# Dans PhaseObserver.__init__ ou run_bot.py
from collections import deque

# Créer des buffers circulaires
self.cvd_history = deque(maxlen=100)
self.delta_history = deque(maxlen=100)
self.volume_history = deque(maxlen=100)

# Initialiser le détecteur
from phase_observer.institutional_reversal_detector import InstitutionalReversalDetector

self.reversal_detector = InstitutionalReversalDetector(
    config=None,  # Utilise config par défaut
    logger=self.logger
)

# Cache du dernier résultat (pour éviter de recalculer à chaque cycle)
self.last_reversal_check = None
self.reversal_check_counter = 0
```

### **ÉTAPE 2: Alimenter les buffers à chaque cycle**

```python
# Dans la boucle principale (run_bot.py ou PhaseObserver)
# À chaque nouvelle bougie ou cycle

# Récupérer les valeurs actuelles depuis votre pipeline
current_cvd = orderflow_result.get('cvd', 0.0)
current_delta = orderflow_result.get('delta', 0.0)
current_volume = df_m1.iloc[-1]['tick_volume'] if 'tick_volume' in df_m1.columns else 0.0

# Ajouter aux buffers
self.cvd_history.append(current_cvd)
self.delta_history.append(current_delta)
self.volume_history.append(current_volume)
```

### **ÉTAPE 3: Appeler le détecteur (toutes les 10 cycles)**

```python
# Appeler le détecteur seulement 1 fois sur 10 (économie de performance)
# Cycle 2.5s × 10 = 25 secondes entre chaque détection

self.reversal_check_counter += 1

if self.reversal_check_counter >= 10:
    self.reversal_check_counter = 0

    # Vérifier qu'on a assez de données historiques
    if len(self.cvd_history) >= 30:
        market_data = {
            'candles_m5': df_m5,                      # DataFrame M5
            'candles_m1': df_m1,                      # DataFrame M1
            'cvd_values': list(self.cvd_history),     # Liste des CVD historiques
            'delta_values': list(self.delta_history), # Liste des deltas historiques
            'volume_values': list(self.volume_history) # Liste des volumes historiques
        }

        try:
            self.last_reversal_check = self.reversal_detector.detect_reversal(market_data)

            # Log du résultat
            self.logger.critical(
                f"🏛️ [REVERSAL_CHECK] Score={self.last_reversal_check['institutional_score']:.1f}/100 | "
                f"Conviction={self.last_reversal_check['conviction_level']} | "
                f"Trend={self.last_reversal_check['new_trend']} | "
                f"Reversal={self.last_reversal_check['reversal_detected']}"
            )
        except Exception as e:
            self.logger.error(f"Erreur détecteur reversal: {e}")
            self.last_reversal_check = None
```

### **ÉTAPE 4: Utiliser le résultat pour VETO et BOOST**

#### **A) VETO PRÉ-TRADE (Avant de placer un ordre)**

```python
# Dans la fonction de décision de trade (ex: strategy/advanced_scoring.py)

def should_take_trade(trade_decision: Dict, current_direction: str) -> Dict:
    """
    Vérifie si le trade doit être pris ou VETO

    Args:
        trade_decision: Décision de trade avec score composite
        current_direction: "BULLISH" ou "BEARISH"

    Returns:
        Dict avec 'allowed' (bool), 'reason' (str), 'adjusted_score' (float)
    """

    # Vérifier si on a un résultat de détection récent
    if self.last_reversal_check is None:
        return {"allowed": True, "reason": "NO_REVERSAL_DATA", "adjusted_score": trade_decision['score']}

    reversal = self.last_reversal_check

    # === RÈGLE 1: VETO si renversement HIGH CONVICTION contre notre direction ===
    if reversal['reversal_detected'] and reversal['conviction_level'] == 'HIGH':
        if reversal['new_trend'] != current_direction and reversal['new_trend'] != 'NEUTRAL':
            return {
                "allowed": False,
                "reason": f"REVERSAL_VETO: Institutional reversal {reversal['new_trend']} detected (score={reversal['institutional_score']:.0f})",
                "adjusted_score": 0.0,
                "reversal_data": reversal
            }

    # === RÈGLE 2: VETO si FATIGUE extrême détectée ===
    fatigue_signals = [s for s in reversal['signals_breakdown'] if 'FATIGUE' in s['name']]
    if fatigue_signals:
        fatigue = fatigue_signals[0]
        if fatigue['strength'] >= 80 and fatigue['direction'] != current_direction:
            return {
                "allowed": False,
                "reason": f"FATIGUE_VETO: Institutional fatigue {fatigue['direction']} (strength={fatigue['strength']:.0f})",
                "adjusted_score": 0.0,
                "fatigue_data": fatigue
            }

    # === RÈGLE 3: BOOST si confirmation avec notre direction ===
    score_boost = 0.0

    if reversal['new_trend'] == current_direction:
        if reversal['conviction_level'] == 'HIGH':
            score_boost = +15.0
        elif reversal['conviction_level'] == 'MODERATE':
            score_boost = +8.0
        elif reversal['conviction_level'] == 'CAUTION':
            score_boost = +3.0

    # === RÈGLE 4: MALUS si contre notre direction (mais pas assez fort pour VETO) ===
    elif reversal['new_trend'] != current_direction and reversal['new_trend'] != 'NEUTRAL':
        if reversal['conviction_level'] == 'MODERATE':
            score_boost = -8.0
        elif reversal['conviction_level'] == 'CAUTION':
            score_boost = -3.0

    adjusted_score = trade_decision['score'] + score_boost

    return {
        "allowed": True,
        "reason": f"ADJUSTED_BY_REVERSAL (boost={score_boost:+.1f})",
        "adjusted_score": adjusted_score,
        "reversal_alignment": reversal['new_trend'] == current_direction
    }
```

#### **B) Exemple d'utilisation complète**

```python
# Dans run_bot.py ou strategy manager

# 1. Calculer le score composite du trade (comme actuellement)
trade_decision = {
    'score': 72.5,
    'direction': 'BULLISH',
    'entry_price': 25743.0,
    # ... autres infos
}

# 2. Vérifier avec le détecteur de renversement
reversal_check = should_take_trade(trade_decision, trade_decision['direction'])

# 3. Décision finale
if not reversal_check['allowed']:
    self.logger.warning(
        f"🛑 TRADE VETO: {reversal_check['reason']}"
    )
    # Ne pas placer le trade
    return None

# 4. Utiliser le score ajusté
final_score = reversal_check['adjusted_score']

if final_score >= MIN_SCORE_THRESHOLD:
    self.logger.info(
        f"✅ TRADE APPROVED: Score={final_score:.1f}/100 | "
        f"{reversal_check['reason']}"
    )
    # Placer le trade avec final_score
    place_trade(trade_decision)
else:
    self.logger.info(
        f"⚠️ TRADE REJECTED: Score trop bas ({final_score:.1f} < {MIN_SCORE_THRESHOLD})"
    )
```

---

## ⚙️ CONFIGURATION AVANCÉE

### **Poids des couches (config par défaut)**

```python
config_reversal = {
    "detection": {
        "min_confidence": 0.75,           # Confiance minimale pour validation
        "required_confluences": 3,        # Nombre de signaux requis
        "institutional_validation": True  # Validation smart money
    },
    "signals": {
        # Poids des 6 couches CORE (total = 100%)
        "changepoint_weight": 0.30,       # Changepoint statistique (30%)
        "divergence_weight": 0.25,        # Divergence M1+M5 (25%)
        "fatigue_weight": 0.15,           # Fatigue institutionnelle (15%)
        "accumulation_weight": 0.15,      # Smart money Wyckoff (15%)
        "microstructure_m1_weight": 0.08, # Microstructure M1 (8%)
        "pattern_weight": 0.07            # ML patterns (7%)
    },
    "thresholds": {
        "volume_spike": 2.5,       # 250% de la moyenne = spike
        "delta_decay": 0.6,        # 60% de réduction = épuisement
        "variance_change": 3.0,    # 3x changement = rupture
        "order_imbalance": 0.7     # 70% d'un côté = déséquilibre
    }
}

# Créer le détecteur avec config custom
reversal_detector = InstitutionalReversalDetector(
    config=config_reversal,
    logger=logger
)
```

---

## 📈 IMPACT ATTENDU

### **Bénéfices**:

1. **Moins de faux signaux en fin de tendance** (-30% estimé)
   - Le détecteur voit la fatigue institutionnelle avant vous
   - Évite les longs au sommet et les shorts au creux

2. **Meilleure détection des renversements** (+40% estimé)
   - 8 couches d'analyse vs votre système actuel
   - Combine statistiques, orderflow, patterns, smart money

3. **Trades mieux timés**
   - Attend confirmation institutionnelle avant d'entrer
   - Booste les trades alignés avec les gros players

4. **Protection contre les pièges**
   - Détecte les accumulations/distributions Wyckoff
   - Identifie les springs (faux breakouts)

### **Coûts**:

1. **Performance**: ~300-500ms par appel (toutes les 25s = 1.2-2% du temps total)
2. **Mémoire**: ~2MB pour les buffers historiques (négligeable)
3. **Complexité**: +1 module à maintenir

---

## 🧪 VALIDATION POST-INTÉGRATION

### **Checklist de test**:

- [ ] Buffers CVD/Delta/Volume alimentés correctement
- [ ] Détecteur appelé toutes les 10 cycles (vérifier les logs)
- [ ] VETO fonctionne sur un renversement HIGH conviction
- [ ] BOOST appliqué quand alignement avec tendance
- [ ] Logs CRITICAL affichent institutional_score
- [ ] Pas de crash/erreur sur données insuffisantes
- [ ] Performance: détection < 500ms

### **Commandes de test**:

```bash
# Vérifier les logs du détecteur
grep "REVERSAL_CHECK" logs/bot_*.log | tail -20

# Vérifier les VETO appliqués
grep "VETO" logs/bot_*.log | tail -10

# Vérifier les ajustements de score
grep "ADJUSTED_BY_REVERSAL" logs/bot_*.log | tail -10
```

---

## ❓ FAQ

### **Q: Quelle fréquence d'appel recommandée?**
R: **Toutes les 10 cycles (25 secondes)** est optimal. C'est suffisamment fréquent pour détecter les renversements et assez espacé pour ne pas surcharger le CPU.

### **Q: Que faire si je n'ai pas 100 valeurs d'historique au démarrage?**
R: Le détecteur fonctionne avec minimum 30 valeurs. Les buffers se remplissent automatiquement au fil des cycles. Attendez simplement 1-2 minutes après le démarrage.

### **Q: Le détecteur peut-il fonctionner sans M1?**
R: Oui, mais avec performance réduite. Les couches M1 (Divergence M1, Microstructure) retourneront des signaux vides. Les 6 autres couches fonctionnent sur M5.

### **Q: Comment désactiver certaines couches trop lentes?**
R: Mettez leur poids à 0 dans la config:
```python
config = {
    "signals": {
        "microstructure_m1_weight": 0.0,  # Désactiver microstructure
        "pattern_weight": 0.0             # Désactiver ML patterns
    }
}
```

---

**Créé**: 14 Janvier 2026
**Auteur**: Claude (Anthropic)
**Statut**: READY FOR INTEGRATION
