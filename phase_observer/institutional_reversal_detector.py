"""
🏛️ INSTITUTIONAL REVERSAL DETECTOR v2.1
Détecteur de renversement de niveau institutionnel adapté au pipeline MT5

Créé: 10 JAN 2026
Modifié: 14 JAN 2026 - Standalone (sans SmartModeSwitcher)
Auteur: System
Compatible: MT5 uniquement (M1, M5, CVD, Delta, Volume)

Architecture:
- 6 couches core : Changepoint, Divergence Multi-TF, Fatigue, Wyckoff, Microstructure M1, ML Patterns
- 2 couches bonus : Confluence Institutionnelle, CVD Capital Flows
- Standalone - Fonctionne indépendamment (sans switcher de mode)
- Aucune dépendance externe (scipy, orderbook, ticks)
"""

import numpy as np
import pandas as pd
from typing import Dict, List, Optional, Tuple
from collections import deque
from dataclasses import dataclass
import warnings
warnings.filterwarnings('ignore')


# ═══════════════════════════════════════════════════════════════
# DATACLASS SIGNAL
# ═══════════════════════════════════════════════════════════════

@dataclass
class ReversalSignal:
    """Signal de renversement structuré"""
    name: str
    strength: float  # 0-100
    confidence: float  # 0-1
    direction: str  # BULLISH/BEARISH/NEUTRAL
    timestamp: pd.Timestamp
    metadata: Dict


# ═══════════════════════════════════════════════════════════════
# INSTITUTIONAL REVERSAL DETECTOR
# ═══════════════════════════════════════════════════════════════

class InstitutionalReversalDetector:
    """
    🏛️ DÉTECTEUR INSTITUTIONNEL DE RENVERSEMENT

    Intègre 6+2 couches d'analyse :
    CORE:
    1. Changepoint Detection (statistique bayésienne)
    2. Divergence Multi-Timeframe (M1, M5)
    3. Fatigue du momentum institutionnel
    4. Accumulation/Distribution smart money (Wyckoff)
    5. Microstructure d'épuisement (M1)
    6. Pattern recognition ML

    BONUS:
    7. Confluence de niveaux institutionnels
    8. Flux de capitaux (CVD proxy)
    """

    def __init__(self, config: Dict = None, logger=None):
        self.config = config or self._default_config()
        self.logger = logger
        self.signals_history = deque(maxlen=1000)
        self.market_regime = "TRENDING_BULL"
        self.regime_confidence = 0.0
        self.current_trend = "BULLISH"  # Pour compatibilité

        # Performance tracking
        self.performance_stats = {
            "total_signals": 0,
            "correct_signals": 0,
            "false_positives": 0
        }

    def _default_config(self) -> Dict:
        """
        Configuration institutionnelle adaptée MT5

        🆕 25 JAN 2026: Poids redistribués après nettoyage des couches redondantes
        Couches actives: Changepoint, Divergence, Wyckoff, ML Patterns, CVD Flows
        Couches désactivées: Fatigue (→VETO), Microstructure (→PMA), Confluence (→PMA)
        """
        return {
            "detection": {
                "min_confidence": 0.75,
                "required_confluences": 2,  # Réduit de 3 à 2 (moins de couches)
                "institutional_validation": True
            },
            "signals": {
                # Poids NORMALISES a 1.0 (18 FEV 2026) - 4 couches core + CVD Flows
                "changepoint_weight": 0.20,      # Changepoint (statistique)
                "divergence_weight": 0.30,       # Divergence Multi-TF (M1+M5) - CRITIQUE
                "accumulation_weight": 0.25,     # Smart Money Wyckoff - COEUR IRD
                "pattern_weight": 0.15,          # ML Pattern Recognition
                "capital_flows_weight": 0.10,    # CVD Capital Flows
                # DESACTIVES (gere par PMA/MPA) — cle presente pour eviter KeyError
                "fatigue_weight": 0.0,
            },
            "thresholds": {
                "volume_spike": 2.5,       # 250% de la moyenne
                "delta_decay": 0.6,        # 60% de réduction
                "variance_change": 3.0,    # 3x changement de variance
                "order_imbalance": 0.7,    # 70% d'un côté
                "fatigue_veto_threshold": 80  # Seuil VETO Fatigue (Circuit Breaker)
            }
        }

    # ═══════════════════════════════════════════════════════════════
    # MÉTHODES PRINCIPALES (Standalone - 14 JAN 2026)
    # ═══════════════════════════════════════════════════════════════

    def detect_reversal(self, market_data: Dict) -> Dict:
        """
        🏛️ MÉTHODE PRINCIPALE - Détection de renversement institutionnel

        Args:
            market_data: Dict contenant:
                - candles_m5: DataFrame M5
                - candles_m1: DataFrame M1
                - cvd_values: Liste CVD
                - delta_values: Liste deltas
                - volume_values: Liste volumes

        Returns:
            Dict avec données institutionnelles:
                - institutional_score: float (0-100) - Force du renversement
                - conviction_level: str (HIGH/MODERATE/CAUTION/LOW)
                - new_trend: str (BULLISH/BEARISH/NEUTRAL) - Direction du renversement
                - regime_change: Dict - Changement de régime détecté
                - signals_breakdown: List[Dict] - Détail des 8 couches
                - reversal_detected: bool - True si score >= 75
                - smart_money_confirmation: Dict - Validation smart money
        """
        # Détection institutionnelle interne
        result = self._detect_institutional_reversal_internal(market_data)

        # Score institutionnel
        institutional_score = result["institutional_score"]

        return {
            # === DONNÉES PRINCIPALES ===
            "institutional_score": institutional_score,
            "conviction_level": result["conviction_level"],
            "new_trend": self._determine_new_trend(result),
            "reversal_detected": institutional_score >= 65,

            # === DÉTAILS ANALYSE ===
            "regime_change": result["regime_change"],
            "signals_breakdown": result["signals_breakdown"],
            "smart_money_confirmation": result["smart_money_confirmation"],

            # === MÉTADONNÉES ===
            "timestamp": result["timestamp"],
            "version": result["version"]
        }

    def _detect_institutional_reversal_internal(self, market_data: Dict) -> Dict:
        """Détection institutionnelle interne (8 couches)"""

        # 1. Analyse des 8 couches
        all_signals = self._analyze_all_layers(market_data)

        # 2. Calcul du score institutionnel pondéré
        institutional_score = self._calculate_institutional_score(all_signals)

        # 3. Détection de changement de régime
        regime_change = self._detect_regime_change(market_data, institutional_score)

        # 4. Validation smart money
        smart_money_confirmation = self._validate_smart_money(market_data)

        # 5. Décision finale
        decision = self._make_institutional_decision(
            institutional_score,
            regime_change,
            smart_money_confirmation
        )

        # 6. Mise à jour tracking
        self.performance_stats["total_signals"] += 1

        # 7. Log si logger disponible
        if self.logger:
            self.logger.info(
                f"[INSTITUTIONAL_DETECTOR] Score: {institutional_score:.1f}/100 | "
                f"Conviction: {decision['conviction_level']} | "
                f"Action: {decision['action']}"
            )

        return {
            **decision,
            "institutional_score": institutional_score,
            "regime_change": regime_change,
            "smart_money_confirmation": smart_money_confirmation,
            "signals_breakdown": self._format_signals_breakdown(all_signals),
            "timestamp": pd.Timestamp.now(),
            "version": "INSTITUTIONAL_MT5_v2.0"
        }

    def _analyze_all_layers(self, market_data: Dict) -> List[ReversalSignal]:
        """
        Exécute les couches d'analyse (NETTOYÉ 25 JAN 2026)

        Architecture "Monopole" - Évite les redondances avec PMA et MPA:
        - PMA (Price Memory) gère: Géométrie, Niveaux S/R, Micro-Structure M1
        - MPA (Market Physics) gère: Physique, Énergie, Inertie
        - IRD (Institutional) gère: Wyckoff, ML Patterns, Divergences CVD, Changepoint
        """
        signals = []

        # ✅ COUCHE 1 : Changepoint Detection (UNIQUE → Garder)
        signals.append(self._detect_statistical_changepoint(market_data))

        # ✅ COUCHE 2 : Divergence Multi-TF (UNIQUE → Garder)
        signals.extend(self._analyze_multi_tf_divergence(market_data))

        # ❌ COUCHE 3 : Fatigue Institutionnelle (REDONDANT MPA → Retirer du SCORING)
        # NOTE: On garde l'analyse mais on NE L'AJOUTE PAS aux signaux de scoring
        # Elle sera utilisée UNIQUEMENT pour le VETO de sécurité (Circuit Breaker)
        # signals.append(self._analyze_institutional_fatigue(market_data))
        # → Voir get_fatigue_signal() pour accès direct au VETO

        # ✅ COUCHE 4 : Smart Money Wyckoff (UNIQUE → Garder)
        # C'est le cœur du module institutionnel (Distribution/Accumulation)
        signals.append(self._detect_smart_money_accumulation(market_data))

        # ✅ COUCHE 6 : ML Pattern Recognition (UNIQUE → Garder)
        signals.append(self._detect_ml_patterns(market_data))

        # ❌ BONUS 1 : Confluence Institutionnelle (REDONDANT PMA → Retirer)
        # PriceMemoryAnalyzer gère les Niveaux Frais et les Pivots
        # signals.append(self._analyze_institutional_confluence(market_data))

        # ✅ BONUS 2 : CVD Capital Flows (UNIQUE → Garder)
        signals.append(self._analyze_cvd_capital_flows(market_data))

        return signals

    def get_fatigue_signal(self, market_data: Dict) -> ReversalSignal:
        """
        🆕 25 JAN 2026: Accès direct à la couche Fatigue pour VETO de sécurité

        Utilisé comme "Circuit Breaker" dans run_bot.py:
        - Si strength >= 80 → Marché épuisé → VETO du trade
        """
        return self._analyze_institutional_fatigue(market_data)

    # ═══════════════════════════════════════════════════════════════
    # COUCHE 1 : CHANGEPOINT DETECTION (sans scipy)
    # ═══════════════════════════════════════════════════════════════

    def _detect_statistical_changepoint(self, market_data: Dict) -> ReversalSignal:
        """
        Détection de changement de régime par statistiques bayésiennes
        SANS scipy.stats - Version numpy pure
        """
        candles_m5 = market_data.get('candles_m5', pd.DataFrame())

        if len(candles_m5) < 10:
            return self._empty_signal("CHANGEPOINT")

        prices = candles_m5['close'].values[-100:] if len(candles_m5) >= 100 else candles_m5['close'].values

        # 1. Calcul des moments statistiques
        returns = np.diff(np.log(prices))

        # 2. Changement de variance (volatilité)
        window_size = min(20, max(3, len(returns) // 3))
        if len(returns) < window_size + 5:
            return self._empty_signal("CHANGEPOINT")

        variances = [np.var(returns[i:i+window_size]) for i in range(len(returns)-window_size)]

        # 3. Détection de rupture par CUSUM
        cusum_stat = self._cusum_detection(variances)

        # 4. Test de Chow pour rupture structurelle (version numpy)
        chow_stat = self._chow_test_numpy(prices)

        # 5. Score composite
        changepoint_score = 0.0
        if cusum_stat > 2.0:  # Seuil institutionnel
            changepoint_score += 40.0
        if chow_stat < 0.1:  # p-value significative (plus permissif sans scipy)
            changepoint_score += 60.0

        # 6. Direction du changement
        direction = self._determine_changepoint_direction(prices, returns)

        return ReversalSignal(
            name="STATISTICAL_CHANGEPOINT",
            strength=min(100.0, changepoint_score),
            confidence=0.85,
            direction=direction,
            timestamp=pd.Timestamp.now(),
            metadata={
                "cusum_stat": cusum_stat,
                "chow_stat": chow_stat,
                "variance_change": variances[-1] / variances[0] if len(variances) > 0 and variances[0] != 0 else 1.0,
                "detection_method": "BAYESIAN_CUSUM_NUMPY"
            }
        )

    def _cusum_detection(self, data: np.ndarray) -> float:
        """Cumulative Sum algorithm for changepoint detection"""
        if len(data) < 10:
            return 0.0

        mean = np.mean(data)
        std = np.std(data)

        if std == 0:
            return 0.0

        # Standardize
        standardized = (data - mean) / std

        # Cumulative sum
        cusum = np.cumsum(standardized - np.mean(standardized))

        # Max absolute deviation
        max_dev = np.max(np.abs(cusum))

        return max_dev

    def _chow_test_numpy(self, prices: np.ndarray) -> float:
        """Test de Chow SANS scipy - Version numpy pure"""
        if len(prices) < 10:
            return 1.0

        mid = len(prices) // 2
        segment1 = prices[:mid]
        segment2 = prices[mid:]

        var1 = np.var(segment1)
        var2 = np.var(segment2)
        var_total = np.var(prices)

        if var_total == 0:
            return 1.0

        # F-statistic simplifiée
        f_stat = (var1 + var2) / (2 * var_total)

        # Conversion approximative en p-value
        p_value = 1.0 / (1.0 + f_stat)

        return p_value

    def _determine_changepoint_direction(self, prices: np.ndarray, returns: np.ndarray) -> str:
        """Détermine la direction du changepoint"""
        if len(returns) < 10:
            return "NEUTRAL"

        recent_mean = np.mean(returns[-5:])
        previous_mean = np.mean(returns[-20:-5] if len(returns) >= 20 else returns[:-5])

        if recent_mean > previous_mean:
            return "BULLISH"
        elif recent_mean < previous_mean:
            return "BEARISH"
        else:
            return "NEUTRAL"

    # ═══════════════════════════════════════════════════════════════
    # COUCHE 2 : DIVERGENCE MULTI-TIMEFRAME (M1+M5)
    # ═══════════════════════════════════════════════════════════════

    def _analyze_multi_tf_divergence(self, market_data: Dict) -> List[ReversalSignal]:
        """Analyse de divergence sur M1 et M5"""
        signals = []

        # M1 divergence (CVD historique)
        if 'candles_m1' in market_data and 'cvd_values' in market_data:
            div_m1 = self._detect_advanced_divergence(
                market_data['candles_m1'],
                market_data['cvd_values'],
                timeframe='M1'
            )
            signals.append(div_m1)

        # 18 FEV 2026: M5 divergence DESACTIVEE — 50 M1 CVD → 10 M5 apres
        # aggregation, mais la detection demande 30+ valeurs → toujours 0.0
        # On garde M1 divergence seule (50 valeurs, suffit largement)

        return signals

    def _aggregate_cvd_to_m5(self, cvd_m1: List[float]) -> List[float]:
        """Agrège CVD M1 en M5 (moyenne par groupe de 5)"""
        if len(cvd_m1) < 5:
            return cvd_m1

        cvd_m5 = []
        for i in range(0, len(cvd_m1), 5):
            chunk = cvd_m1[i:i+5]
            if len(chunk) > 0:
                cvd_m5.append(np.mean(chunk))

        return cvd_m5

    def _detect_advanced_divergence(self, candles: pd.DataFrame,
                                   cvd_values: List[float],
                                   timeframe: str) -> ReversalSignal:
        """Détection avancée de divergence (régulière + cachée)"""
        if len(candles) < 10 or len(cvd_values) < 10:
            return self._empty_signal(f"DIVERGENCE_{timeframe}")

        price_highs = candles['high'].values[-10:]
        price_lows = candles['low'].values[-10:]
        cvd_segment = list(cvd_values[-10:])

        # 1. Divergence régulière
        regular_div = self._check_regular_divergence(price_highs, price_lows, cvd_segment)

        # 2. Divergence cachée
        hidden_div = self._check_hidden_divergence_impl(price_highs, price_lows, cvd_segment)

        # Score composite
        divergence_score = 0.0
        direction = "NEUTRAL"

        if regular_div['detected']:
            divergence_score += 50.0
            direction = regular_div['direction']

        if hidden_div['detected']:
            divergence_score += 50.0
            direction = hidden_div['direction']

        return ReversalSignal(
            name=f"ADVANCED_DIVERGENCE_{timeframe}",
            strength=min(100.0, divergence_score),
            confidence=0.8,
            direction=direction,
            timestamp=pd.Timestamp.now(),
            metadata={
                "regular": regular_div,
                "hidden": hidden_div,
                "timeframe": timeframe
            }
        )

    def _check_regular_divergence(self, price_highs, price_lows, cvd_segment):
        """Divergence régulière basique"""
        # Baissière : Prix monte, CVD descend
        if len(price_highs) >= 3 and len(cvd_segment) >= 3:
            if (price_highs[-1] > price_highs[-3] and cvd_segment[-1] < cvd_segment[-3]):
                return {"detected": True, "direction": "BEARISH", "type": "REGULAR_BEARISH"}

        # Haussière : Prix descend, CVD monte
        if len(price_lows) >= 3 and len(cvd_segment) >= 3:
            if (price_lows[-1] < price_lows[-3] and cvd_segment[-1] > cvd_segment[-3]):
                return {"detected": True, "direction": "BULLISH", "type": "REGULAR_BULLISH"}

        return {"detected": False, "direction": "NEUTRAL", "type": "NONE"}

    def _check_hidden_divergence_impl(self, price_highs, price_lows, cvd_segment):
        """Divergence cachée (continuation)"""
        # Hidden bullish : Prix fait HL (Higher Low), CVD fait LL (Lower Low)
        if len(price_lows) >= 3 and len(cvd_segment) >= 3:
            if (price_lows[-1] > price_lows[-3] and cvd_segment[-1] < cvd_segment[-3]):
                return {"detected": True, "direction": "BULLISH", "type": "HIDDEN_BULLISH"}

        # Hidden bearish : Prix fait LH (Lower High), CVD fait HH (Higher High)
        if len(price_highs) >= 3 and len(cvd_segment) >= 3:
            if (price_highs[-1] < price_highs[-3] and cvd_segment[-1] > cvd_segment[-3]):
                return {"detected": True, "direction": "BEARISH", "type": "HIDDEN_BEARISH"}

        return {"detected": False, "direction": "NEUTRAL", "type": "NONE"}

    # ═══════════════════════════════════════════════════════════════
    # COUCHE 3 : FATIGUE INSTITUTIONNELLE (sans ticks)
    # ═══════════════════════════════════════════════════════════════

    def _analyze_institutional_fatigue(self, market_data: Dict) -> ReversalSignal:
        """Analyse la fatigue des institutions SANS ticks_data"""
        delta_values = market_data.get('delta_values', [])
        volume_values = market_data.get('volume_values', [])

        if len(delta_values) < 15 or len(volume_values) < 15:
            return self._empty_signal("FATIGUE")

        # 1. Décroissance exponentielle du momentum
        momentum_decay = self._calculate_momentum_decay_impl(delta_values)

        # 2. Volume divergence (prix monte mais volume baisse)
        volume_divergence = self._check_volume_divergence_impl(volume_values, delta_values)

        # 3. Large delta exhaustion (remplace large player)
        large_delta_exhaustion = self._detect_large_delta_exhaustion(delta_values)

        # 4. Time-based fatigue (durée de la tendance)
        time_fatigue = self._calculate_time_based_fatigue_impl(market_data)

        # Score composite
        fatigue_score = 0.0
        if momentum_decay > 0.7:
            fatigue_score += 35.0
        if volume_divergence:
            fatigue_score += 30.0
        if large_delta_exhaustion:
            fatigue_score += 25.0
        if time_fatigue > 0.6:
            fatigue_score += 20.0

        # Direction basée sur la tendance actuelle
        current_trend = self._get_current_trend_impl(market_data)
        direction = "BEARISH" if current_trend == "BULLISH" else "BULLISH"

        return ReversalSignal(
            name="INSTITUTIONAL_FATIGUE",
            strength=min(100.0, fatigue_score),
            confidence=0.75,
            direction=direction,
            timestamp=pd.Timestamp.now(),
            metadata={
                "momentum_decay": momentum_decay,
                "volume_divergence": volume_divergence,
                "large_delta_exhaustion": large_delta_exhaustion,
                "time_fatigue": time_fatigue,
                "current_trend": current_trend
            }
        )

    def _calculate_momentum_decay_impl(self, delta_values: List[float]) -> float:
        """Calcul décroissance momentum"""
        if len(delta_values) < 6:
            return 0.0

        window = min(len(delta_values), 6)
        deltas = np.array(delta_values[-window:])

        # Momentum = moyenne mobile exponentielle
        ema_short = np.mean(deltas[-3:])
        ema_long = np.mean(deltas[-window:])

        if abs(ema_long) < 1:
            return 0.0

        # Decay = réduction du momentum
        decay = 1.0 - (abs(ema_short) / abs(ema_long))

        return max(0.0, min(1.0, decay))

    def _check_volume_divergence_impl(self, volume_values: List[float], delta_values: List[float]) -> bool:
        """Volume baisse, delta/prix monte → divergence"""
        if len(volume_values) < 10 or len(delta_values) < 10:
            return False

        volumes = np.array(volume_values[-10:])
        deltas = np.array(delta_values[-10:])

        # Tendance volume
        volume_trend = np.polyfit(range(len(volumes)), volumes, 1)[0]

        # Tendance delta
        delta_trend = np.polyfit(range(len(deltas)), deltas, 1)[0]

        # Divergence : volume baisse ET delta monte (ou inverse)
        if volume_trend < 0 and delta_trend > 0:
            return True
        if volume_trend > 0 and delta_trend < 0:
            return True

        return False

    def _detect_large_delta_exhaustion(self, delta_values: List[float]) -> bool:
        """Gros deltas disparaissent (fatigue)"""
        if len(delta_values) < 10:
            return False

        deltas = np.array(delta_values[-10:])

        # Comparer 1ère moitié vs 2ème moitié
        first_half = deltas[:5]
        second_half = deltas[5:]

        # Compter deltas >30 (gros)
        large_first = np.sum(np.abs(first_half) > 30)
        large_second = np.sum(np.abs(second_half) > 30)

        # Épuisement : gros deltas diminuent de >50%
        if large_first > 0 and large_second < large_first * 0.5:
            return True

        return False

    def _calculate_time_based_fatigue_impl(self, market_data: Dict) -> float:
        """Fatigue temporelle (durée tendance)"""
        candles_m5 = market_data.get('candles_m5', pd.DataFrame())

        if len(candles_m5) < 5:
            return 0.0

        # Compter bougies consécutives dans même direction
        candles_list = candles_m5.tail(5).to_dict('records')

        consecutive_green = 0
        consecutive_red = 0
        current_streak = 0

        for c in reversed(candles_list):
            if c['close'] > c['open']:
                if consecutive_green == current_streak:
                    consecutive_green += 1
                    current_streak += 1
                else:
                    break
            else:
                if consecutive_red == current_streak:
                    consecutive_red += 1
                    current_streak += 1
                else:
                    break

        max_streak = max(consecutive_green, consecutive_red)

        # Fatigue si >10 bougies consécutives
        fatigue = min(1.0, max_streak / 15.0)

        return fatigue

    def _get_current_trend_impl(self, market_data: Dict) -> str:
        """Tendance actuelle (BULLISH/BEARISH)"""
        candles_m5 = market_data.get('candles_m5', pd.DataFrame())

        if len(candles_m5) < 5:
            return "NEUTRAL"

        last_5 = candles_m5.tail(5).to_dict('records')
        green_count = sum(1 for c in last_5 if c['close'] > c['open'])

        if green_count >= 4:
            return "BULLISH"
        elif green_count <= 1:
            return "BEARISH"
        else:
            return "NEUTRAL"

    # ═══════════════════════════════════════════════════════════════
    # COUCHE 4 : SMART MONEY WYCKOFF (sans ticks)
    # ═══════════════════════════════════════════════════════════════

    def _detect_smart_money_accumulation(self, market_data: Dict) -> ReversalSignal:
        """
        Détecte l'accumulation ou distribution des smart money
        Basé sur le concept Wyckoff SANS ticks_data
        """
        candles = market_data.get('candles_m5', pd.DataFrame())
        volume = market_data.get('volume_values', [])

        if len(candles) < 5 or len(volume) < 5:
            return self._empty_signal("SMART_MONEY")

        # 1. Phase Wyckoff
        wyckoff_phase = self._identify_wyckoff_phase_impl(candles, volume)

        # 2. Volume analysis (volume élevé sur les corrections)
        accumulation_volume = self._check_accumulation_volume_impl(candles, volume)

        # 3. Price springs & tests (tests de support/résistance)
        spring_detected = self._detect_spring_test_impl(candles)

        # 4. Effort vs Result (fort volume sans mouvement)
        effort_result = self._analyze_effort_vs_result_impl(candles, volume)

        # 5. Wick absorption (remplace absorption patterns)
        wick_absorption = self._analyze_wick_absorption(candles)

        # Score et direction
        smart_money_score = 0.0
        direction = "NEUTRAL"

        if wyckoff_phase in ["ACCUMULATION", "REACCUMULATION"]:
            smart_money_score += 40.0
            direction = "BULLISH"
        elif wyckoff_phase in ["DISTRIBUTION", "REDISTRIBUTION"]:
            smart_money_score += 40.0
            direction = "BEARISH"

        if accumulation_volume:
            smart_money_score += 25.0

        if spring_detected:
            smart_money_score += 20.0

        if effort_result["imbalance"]:
            smart_money_score += 15.0

        if wick_absorption:
            smart_money_score += 10.0

        return ReversalSignal(
            name="SMART_MONEY_ACCUMULATION",
            strength=min(100.0, smart_money_score),
            confidence=0.8,
            direction=direction,
            timestamp=pd.Timestamp.now(),
            metadata={
                "wyckoff_phase": wyckoff_phase,
                "accumulation_volume": accumulation_volume,
                "spring_detected": spring_detected,
                "effort_result": effort_result,
                "wick_absorption": wick_absorption
            }
        )

    def _identify_wyckoff_phase_impl(self, candles: pd.DataFrame, volume: List[float]) -> str:
        """Identifie phase Wyckoff simplifié"""
        if len(candles) < 20:
            return "NEUTRAL"

        last_20 = candles.tail(20).to_dict('records')
        vol_last_20 = volume[-20:] if len(volume) >= 20 else volume

        # Range (prix oscillant)
        price_range = max([c['high'] for c in last_20]) - min([c['low'] for c in last_20])
        avg_price = np.mean([c['close'] for c in last_20])
        range_pct = price_range / avg_price if avg_price > 0 else 0

        # Volume moyen élevé
        avg_volume = np.mean(vol_last_20)
        recent_volume = np.mean(vol_last_20[-5:])

        # ACCUMULATION : Range etroit (<5%), volume eleve
        # 18 FEV 2026: seuil range 0.02 → 0.05, volume 1.3 → 1.2 (etait trop strict)
        if range_pct < 0.05 and recent_volume > avg_volume * 1.2:
            # Verifie si prix bas du range
            current_price = last_20[-1]['close']
            range_low = min([c['low'] for c in last_20])
            if current_price < range_low + price_range * 0.35:
                return "ACCUMULATION"

        # DISTRIBUTION : Range etroit, volume eleve, prix haut
        if range_pct < 0.05 and recent_volume > avg_volume * 1.2:
            current_price = last_20[-1]['close']
            range_high = max([c['high'] for c in last_20])
            if current_price > range_high - price_range * 0.35:
                return "DISTRIBUTION"

        return "NEUTRAL"

    def _check_accumulation_volume_impl(self, candles: pd.DataFrame, volume: List[float]) -> bool:
        """Volume élevé sur corrections (smart money achète)"""
        if len(candles) < 10:
            return False

        last_10 = candles.tail(10).to_dict('records')
        vol_last_10 = volume[-10:] if len(volume) >= 10 else volume

        # Identifier corrections (bougies rouges)
        corrections = []
        for i, c in enumerate(last_10):
            if i < len(vol_last_10) and c['close'] < c['open']:
                corrections.append(vol_last_10[i])

        if len(corrections) < 3:
            return False

        # Volume moyen corrections vs global
        avg_correction_vol = np.mean(corrections)
        avg_total_vol = np.mean(vol_last_10)

        # Accumulation si volume corrections >1.5x normal
        return avg_correction_vol > avg_total_vol * 1.5

    def _detect_spring_test_impl(self, candles: pd.DataFrame) -> bool:
        """Détecte spring (faux breakout puis retour)"""
        if len(candles) < 15:
            return False

        candles_list = candles.tail(15).to_dict('records')

        # Trouver support récent (low des 10 dernières)
        support = min([c['low'] for c in candles_list[:10]])

        # Spring : bougie casse support puis clôture au-dessus
        for c in candles_list[10:]:
            if c['low'] < support and c['close'] > support:
                return True

        return False

    def _analyze_effort_vs_result_impl(self, candles: pd.DataFrame, volume: List[float]) -> Dict:
        """Effort (volume) vs Result (mouvement prix)"""
        if len(candles) < 10:
            return {"imbalance": False, "ratio": 0.0}

        last_10 = candles.tail(10).to_dict('records')
        vol_last_10 = volume[-10:] if len(volume) >= 10 else volume

        # Effort = volume total
        total_volume = np.sum(vol_last_10)

        # Result = mouvement prix total
        price_move = abs(last_10[-1]['close'] - last_10[0]['close'])
        avg_price = np.mean([c['close'] for c in last_10])
        price_move_pct = price_move / avg_price if avg_price > 0 else 0

        # Imbalance : volume élevé, mouvement faible
        avg_volume = np.mean(vol_last_10)

        if total_volume > avg_volume * 15 and price_move_pct < 0.01:
            return {"imbalance": True, "ratio": total_volume / max(price_move_pct, 0.001)}

        return {"imbalance": False, "ratio": 0.0}

    def _analyze_wick_absorption(self, candles: pd.DataFrame) -> bool:
        """Wicks longs = absorption institutionnelle"""
        if len(candles) < 5:
            return False

        last_5 = candles.tail(5).to_dict('records')

        long_wicks = 0
        for c in last_5:
            body = abs(c['close'] - c['open'])
            upper_wick = c['high'] - max(c['close'], c['open'])
            lower_wick = min(c['close'], c['open']) - c['low']

            # Wick >2x body
            if body > 0 and (upper_wick > body * 2 or lower_wick > body * 2):
                long_wicks += 1

        # Absorption si ≥3 bougies avec longs wicks
        return long_wicks >= 3

    # ═══════════════════════════════════════════════════════════════
    # COUCHE 5 : MICROSTRUCTURE M1 (remplace ticks)
    # ═══════════════════════════════════════════════════════════════

    # ═══════════════════════════════════════════════════════════════
    # COUCHE 6 : ML PATTERN RECOGNITION
    # ═══════════════════════════════════════════════════════════════

    def _detect_ml_patterns(self, market_data: Dict) -> ReversalSignal:
        """ML patterns légers (sans bibliothèque ML externe)"""
        # 1. Price patterns
        price_patterns = self._recognize_price_patterns_impl(market_data)

        # 2. Volume patterns
        volume_patterns = self._recognize_volume_patterns_impl(market_data)

        # 3. Time patterns
        time_patterns = self._recognize_time_patterns_impl(market_data)

        # 4. Statistical patterns
        statistical_patterns = self._recognize_statistical_patterns_impl(market_data)

        # Ensemble
        ensemble = self._ensemble_ml_prediction_impl(
            price_patterns, volume_patterns, time_patterns, statistical_patterns
        )

        return ReversalSignal(
            name="ML_PATTERN_RECOGNITION",
            strength=ensemble["strength"],
            confidence=ensemble["confidence"],
            direction=ensemble["direction"],
            timestamp=pd.Timestamp.now(),
            metadata={
                "price_patterns": price_patterns,
                "volume_patterns": volume_patterns,
                "time_patterns": time_patterns,
                "statistical_patterns": statistical_patterns
            }
        )

    def _recognize_price_patterns_impl(self, market_data: Dict) -> Dict:
        """Candlestick patterns classiques"""
        candles_m1 = market_data.get('candles_m1', pd.DataFrame())

        if len(candles_m1) < 10:
            return {"detected": [], "strength": 0.0}

        last_10 = candles_m1.tail(10).to_dict('records')
        patterns_detected = []

        # Pattern 1: Engulfing
        if len(last_10) >= 2:
            prev = last_10[-2]
            curr = last_10[-1]

            # Bullish engulfing
            if (prev['close'] < prev['open'] and
                curr['close'] > curr['open'] and
                curr['close'] > prev['open'] and
                curr['open'] < prev['close']):
                patterns_detected.append("BULLISH_ENGULFING")

            # Bearish engulfing
            if (prev['close'] > prev['open'] and
                curr['close'] < curr['open'] and
                curr['close'] < prev['open'] and
                curr['open'] > prev['close']):
                patterns_detected.append("BEARISH_ENGULFING")

        # Pattern 2: Morning/Evening Star
        if len(last_10) >= 3:
            c1 = last_10[-3]
            c2 = last_10[-2]
            c3 = last_10[-1]

            # Morning star (reversal bullish)
            if (c1['close'] < c1['open'] and  # Bearish
                abs(c2['close'] - c2['open']) < (c1['open'] - c1['close']) * 0.3 and  # Doji
                c3['close'] > c3['open'] and  # Bullish
                c3['close'] > c1['open']):
                patterns_detected.append("MORNING_STAR")

            # Evening star (reversal bearish)
            if (c1['close'] > c1['open'] and  # Bullish
                abs(c2['close'] - c2['open']) < (c1['close'] - c1['open']) * 0.3 and  # Doji
                c3['close'] < c3['open'] and  # Bearish
                c3['close'] < c1['open']):
                patterns_detected.append("EVENING_STAR")

        strength = len(patterns_detected) * 30.0

        return {
            "detected": patterns_detected,
            "strength": min(100.0, strength),
            "count": len(patterns_detected)
        }

    def _recognize_volume_patterns_impl(self, market_data: Dict) -> Dict:
        """Volume patterns"""
        volume_values = market_data.get('volume_values', [])

        if len(volume_values) < 20:
            return {"detected": [], "strength": 0.0}

        volumes = np.array(volume_values[-20:])
        patterns_detected = []

        # Climax volume (pic extrême)
        avg_volume = np.mean(volumes[:-1])
        current_volume = volumes[-1]

        if current_volume > avg_volume * 3:
            patterns_detected.append("VOLUME_CLIMAX")

        # Volume décroissant (épuisement)
        vol_trend = np.polyfit(range(len(volumes)), volumes, 1)[0]
        if vol_trend < -avg_volume * 0.1:
            patterns_detected.append("VOLUME_EXHAUSTION")

        # Volume croissant (confirmation)
        if vol_trend > avg_volume * 0.1:
            patterns_detected.append("VOLUME_CONFIRMATION")

        strength = len(patterns_detected) * 25.0

        return {
            "detected": patterns_detected,
            "strength": min(100.0, strength),
            "count": len(patterns_detected)
        }

    def _recognize_time_patterns_impl(self, market_data: Dict) -> Dict:
        """Time-based patterns (sessions, cycles)"""
        patterns_detected = []

        # Heure actuelle
        try:
            current_time = pd.Timestamp.now(tz='UTC')
            hour = current_time.hour

            # London session (8h-12h UTC) - Volatilité élevée
            if 8 <= hour < 12:
                patterns_detected.append("LONDON_SESSION")

            # New York session (13h-17h UTC) - Volatilité élevée
            if 13 <= hour < 17:
                patterns_detected.append("NY_SESSION")

            # Dead zone (0h-4h UTC) - Faible volatilité
            if 0 <= hour < 4:
                patterns_detected.append("DEAD_ZONE")
        except:
            pass

        strength = len(patterns_detected) * 20.0

        return {
            "detected": patterns_detected,
            "strength": min(100.0, strength),
            "session": patterns_detected[0] if patterns_detected else "UNKNOWN"
        }

    def _recognize_statistical_patterns_impl(self, market_data: Dict) -> Dict:
        """Statistical outliers (Z-score)"""
        candles_m5 = market_data.get('candles_m5', pd.DataFrame())

        if len(candles_m5) < 5:
            return {"detected": [], "strength": 0.0}

        prices = candles_m5['close'].values[-min(len(candles_m5), 20):]
        patterns_detected = []
        z_score = 0.0

        # Z-score du dernier prix
        mean_price = np.mean(prices[:-1])
        std_price = np.std(prices[:-1])

        if std_price > 0:
            z_score = (prices[-1] - mean_price) / std_price

            # Outlier haut (Z > 2)
            if z_score > 2.0:
                patterns_detected.append("OVERBOUGHT_OUTLIER")

            # Outlier bas (Z < -2)
            if z_score < -2.0:
                patterns_detected.append("OVERSOLD_OUTLIER")

        # Variance spike
        returns = np.diff(np.log(prices))
        if len(returns) >= 10:
            current_variance = np.var(returns[-5:])
            avg_variance = np.var(returns[:-5])

            if avg_variance > 0 and current_variance > avg_variance * 2:
                patterns_detected.append("VARIANCE_SPIKE")

        strength = len(patterns_detected) * 30.0

        return {
            "detected": patterns_detected,
            "strength": min(100.0, strength),
            "z_score": z_score
        }

    def _ensemble_ml_prediction_impl(self, price_patterns, volume_patterns,
                                     time_patterns, statistical_patterns) -> Dict:
        """Combine tous les patterns"""
        total_strength = 0.0
        votes_bullish = 0
        votes_bearish = 0

        # Price patterns
        if "BULLISH_ENGULFING" in price_patterns.get("detected", []) or \
           "MORNING_STAR" in price_patterns.get("detected", []):
            votes_bullish += 2
            total_strength += price_patterns.get("strength", 0) * 0.4

        if "BEARISH_ENGULFING" in price_patterns.get("detected", []) or \
           "EVENING_STAR" in price_patterns.get("detected", []):
            votes_bearish += 2
            total_strength += price_patterns.get("strength", 0) * 0.4

        # Volume patterns
        if "VOLUME_CLIMAX" in volume_patterns.get("detected", []):
            votes_bearish += 1  # Climax souvent avant reversal
            total_strength += volume_patterns.get("strength", 0) * 0.2

        # Statistical patterns
        if "OVERBOUGHT_OUTLIER" in statistical_patterns.get("detected", []):
            votes_bearish += 1
            total_strength += statistical_patterns.get("strength", 0) * 0.2

        if "OVERSOLD_OUTLIER" in statistical_patterns.get("detected", []):
            votes_bullish += 1
            total_strength += statistical_patterns.get("strength", 0) * 0.2

        # Time patterns (bonus)
        if time_patterns.get("session") in ["LONDON_SESSION", "NY_SESSION"]:
            total_strength += time_patterns.get("strength", 0) * 0.2

        # Direction finale
        if votes_bullish > votes_bearish:
            direction = "BULLISH"
            confidence = min(1.0, votes_bullish / 5.0)
        elif votes_bearish > votes_bullish:
            direction = "BEARISH"
            confidence = min(1.0, votes_bearish / 5.0)
        else:
            direction = "NEUTRAL"
            confidence = 0.5

        return {
            "strength": min(100.0, total_strength),
            "confidence": confidence,
            "direction": direction,
            "votes_bullish": votes_bullish,
            "votes_bearish": votes_bearish
        }

    # ═══════════════════════════════════════════════════════════════
    # BONUS 1 : CONFLUENCE INSTITUTIONNELLE
    # ═══════════════════════════════════════════════════════════════

    def _analyze_institutional_confluence(self, market_data: Dict) -> ReversalSignal:
        """Analyse la confluence des niveaux institutionnels"""
        candles_m5 = market_data.get('candles_m5', pd.DataFrame())

        if len(candles_m5) < 10:
            return self._empty_signal("CONFLUENCE")

        # 1. Fibonacci confluence
        fib_confluence = self._analyze_fibonacci_confluence_impl(candles_m5)

        # 2. Pivot points confluence
        pivot_confluence = self._analyze_pivot_confluence_impl(candles_m5)

        # 3. Volume profile nodes
        volume_nodes = self._analyze_volume_profile_nodes_impl(candles_m5)

        # 4. Support/Resistance institutionnels
        institutional_levels = self._identify_institutional_levels_impl(candles_m5)

        # Score de confluence
        confluence_count = sum([
            fib_confluence["confluent"],
            pivot_confluence["confluent"],
            volume_nodes["significant"],
            institutional_levels["present"]
        ])

        if confluence_count >= 3:
            confluence_score = min(100.0, confluence_count * 25)

            # Direction basée sur position du prix
            if fib_confluence.get("resistance_hit", False):
                direction = "BEARISH"
            elif fib_confluence.get("support_hit", False):
                direction = "BULLISH"
            else:
                direction = "NEUTRAL"
        else:
            confluence_score = 0.0
            direction = "NEUTRAL"

        return ReversalSignal(
            name="INSTITUTIONAL_CONFLUENCE",
            strength=confluence_score,
            confidence=0.9,
            direction=direction,
            timestamp=pd.Timestamp.now(),
            metadata={
                "fib_confluence": fib_confluence,
                "pivot_confluence": pivot_confluence,
                "volume_nodes": volume_nodes,
                "institutional_levels": institutional_levels,
                "confluence_count": confluence_count
            }
        )

    def _analyze_fibonacci_confluence_impl(self, candles: pd.DataFrame) -> Dict:
        """Fibonacci retracements"""
        if len(candles) < 50:
            return {"confluent": False}

        # Swing high/low sur 50 bougies
        swing_high = candles['high'].max()
        swing_low = candles['low'].min()

        # Niveaux Fib
        diff = swing_high - swing_low
        fib_levels = {
            "0.236": swing_low + diff * 0.236,
            "0.382": swing_low + diff * 0.382,
            "0.500": swing_low + diff * 0.500,
            "0.618": swing_low + diff * 0.618,
            "0.786": swing_low + diff * 0.786
        }

        # Prix actuel
        current_price = candles.iloc[-1]['close']

        # Distance au niveau le plus proche
        min_distance = float('inf')
        closest_level = None

        for level_name, level_price in fib_levels.items():
            distance = abs(current_price - level_price) / current_price if current_price > 0 else 0
            if distance < min_distance:
                min_distance = distance
                closest_level = level_name

        # Confluent si <0.5% du niveau
        confluent = min_distance < 0.005

        # Direction
        resistance_hit = confluent and current_price > swing_low + diff * 0.5
        support_hit = confluent and current_price < swing_low + diff * 0.5

        return {
            "confluent": confluent,
            "closest_level": closest_level,
            "distance_pct": min_distance,
            "resistance_hit": resistance_hit,
            "support_hit": support_hit
        }

    def _analyze_pivot_confluence_impl(self, candles: pd.DataFrame) -> Dict:
        """Pivot points standard"""
        if len(candles) < 24:
            return {"confluent": False}

        # Pivots sur 24 dernières bougies M5 (2 heures)
        period_candles = candles.tail(24)

        high = period_candles['high'].max()
        low = period_candles['low'].min()
        close = period_candles.iloc[-1]['close']

        # Pivot classique
        pivot = (high + low + close) / 3

        # Résistances/Supports
        r1 = 2 * pivot - low
        s1 = 2 * pivot - high
        r2 = pivot + (high - low)
        s2 = pivot - (high - low)

        # Prix actuel
        current_price = candles.iloc[-1]['close']

        # Confluence si proche d'un pivot (<0.3%)
        pivot_levels = [pivot, r1, s1, r2, s2]
        min_distance = min([abs(current_price - level) / current_price for level in pivot_levels if current_price > 0])

        confluent = min_distance < 0.003

        return {
            "confluent": confluent,
            "pivot": pivot,
            "r1": r1,
            "s1": s1,
            "distance_pct": min_distance
        }

    def _analyze_volume_profile_nodes_impl(self, candles: pd.DataFrame) -> Dict:
        """Volume profile POC"""
        if len(candles) < 50:
            return {"significant": False}

        # Price bins
        prices = candles['close'].values
        volumes = candles['volume'].values if 'volume' in candles.columns else np.ones(len(candles))

        # Créer bins de prix
        price_min = prices.min()
        price_max = prices.max()
        num_bins = 20

        if price_max <= price_min:
            return {"significant": False}

        bins = np.linspace(price_min, price_max, num_bins)
        volume_profile = np.zeros(num_bins - 1)

        # Remplir volume profile
        for price, volume in zip(prices, volumes):
            bin_index = np.searchsorted(bins, price) - 1
            if 0 <= bin_index < len(volume_profile):
                volume_profile[bin_index] += volume

        # POC = bin avec le plus de volume
        poc_index = np.argmax(volume_profile)
        poc_price = (bins[poc_index] + bins[poc_index + 1]) / 2

        # Prix actuel proche du POC ?
        current_price = candles.iloc[-1]['close']
        distance = abs(current_price - poc_price) / current_price if current_price > 0 else 0

        significant = distance < 0.005

        return {
            "significant": significant,
            "poc_price": poc_price,
            "distance_pct": distance
        }

    def _identify_institutional_levels_impl(self, candles: pd.DataFrame) -> Dict:
        """Support/Resistance par swing points"""
        if len(candles) < 50:
            return {"present": False}

        # Identifier swing highs/lows
        swing_highs = []
        swing_lows = []

        candles_list = candles.to_dict('records')

        for i in range(2, len(candles_list) - 2):
            # Swing high : plus haut que voisins
            if (candles_list[i]['high'] > candles_list[i-1]['high'] and
                candles_list[i]['high'] > candles_list[i-2]['high'] and
                candles_list[i]['high'] > candles_list[i+1]['high'] and
                candles_list[i]['high'] > candles_list[i+2]['high']):
                swing_highs.append(candles_list[i]['high'])

            # Swing low : plus bas que voisins
            if (candles_list[i]['low'] < candles_list[i-1]['low'] and
                candles_list[i]['low'] < candles_list[i-2]['low'] and
                candles_list[i]['low'] < candles_list[i+1]['low'] and
                candles_list[i]['low'] < candles_list[i+2]['low']):
                swing_lows.append(candles_list[i]['low'])

        # Prix actuel proche d'un swing ?
        current_price = candles.iloc[-1]['close']

        all_levels = swing_highs + swing_lows
        if len(all_levels) == 0:
            return {"present": False}

        min_distance = min([abs(current_price - level) / current_price for level in all_levels if current_price > 0])

        present = min_distance < 0.005

        return {
            "present": present,
            "swing_highs": swing_highs[-3:] if swing_highs else [],
            "swing_lows": swing_lows[-3:] if swing_lows else [],
            "distance_pct": min_distance
        }

    # ═══════════════════════════════════════════════════════════════
    # BONUS 2 : CVD CAPITAL FLOWS
    # ═══════════════════════════════════════════════════════════════

    def _analyze_cvd_capital_flows(self, market_data: Dict) -> ReversalSignal:
        """CVD comme proxy des flux institutionnels"""
        cvd_values = market_data.get('cvd_values', [])
        volume_values = market_data.get('volume_values', [])

        if len(cvd_values) < 30 or len(volume_values) < 30:
            return self._empty_signal("CAPITAL_FLOWS")

        # 1. Tendance CVD
        cvd_trend = self._calculate_cvd_trend(cvd_values)

        # 2. Accélération CVD
        cvd_acceleration = self._calculate_cvd_acceleration(cvd_values)

        # 3. CVD/Volume efficacité
        cvd_volume_efficiency = self._calculate_cvd_volume_efficiency(cvd_values, volume_values)

        # 4. Retournement de flux
        flow_reversal = self._detect_cvd_flow_reversal(cvd_values)

        # Score
        flow_score = 0.0
        direction = "NEUTRAL"

        # 18 FEV 2026: seuils abaisses (0.7→0.35, 1.5→1.0, 0.6→0.4)
        if abs(cvd_trend) > 0.35:
            flow_score += 40.0
            direction = "BULLISH" if cvd_trend > 0 else "BEARISH"

        if cvd_acceleration > 1.0:
            flow_score += 30.0

        if cvd_volume_efficiency > 0.4:
            flow_score += 20.0

        if flow_reversal:
            flow_score += 30.0

        return ReversalSignal(
            name="CVD_CAPITAL_FLOWS",
            strength=min(100.0, flow_score),
            confidence=0.7,
            direction=direction,
            timestamp=pd.Timestamp.now(),
            metadata={
                "cvd_trend": cvd_trend,
                "cvd_acceleration": cvd_acceleration,
                "cvd_volume_efficiency": cvd_volume_efficiency,
                "flow_reversal": flow_reversal
            }
        )

    def _calculate_cvd_trend(self, cvd_values: List[float]) -> float:
        """Tendance CVD normalisée"""
        if len(cvd_values) < 20:
            return 0.0

        cvd = np.array(cvd_values[-20:])

        # Régression linéaire
        trend = np.polyfit(range(len(cvd)), cvd, 1)[0]

        # Normaliser (-1 à 1)
        avg_cvd = np.mean(np.abs(cvd))
        if avg_cvd == 0:
            return 0.0

        normalized_trend = trend / avg_cvd

        return max(-1.0, min(1.0, normalized_trend))

    def _calculate_cvd_acceleration(self, cvd_values: List[float]) -> float:
        """Accélération du CVD"""
        if len(cvd_values) < 20:
            return 0.0

        cvd = np.array(cvd_values[-20:])

        # Vitesse = différence
        velocity = np.diff(cvd)

        # Accélération = différence de vitesse
        if len(velocity) < 2:
            return 0.0

        acceleration = np.diff(velocity)

        # Moyenne des accélérations récentes
        recent_accel = np.mean(acceleration[-5:]) if len(acceleration) >= 5 else np.mean(acceleration)
        avg_accel = np.mean(np.abs(acceleration))

        if avg_accel == 0:
            return 0.0

        # Ratio
        accel_ratio = abs(recent_accel) / avg_accel

        return accel_ratio

    def _calculate_cvd_volume_efficiency(self, cvd_values: List[float], volume_values: List[float]) -> float:
        """Efficacité CVD/Volume"""
        if len(cvd_values) < 20 or len(volume_values) < 20:
            return 0.0

        cvd = np.array(cvd_values[-20:])
        volume = np.array(volume_values[-20:])

        # CVD change
        cvd_change = abs(cvd[-1] - cvd[0])

        # Volume total
        volume_total = np.sum(volume)

        if volume_total == 0:
            return 0.0

        # Efficacité = CVD change / Volume
        efficiency = cvd_change / volume_total

        # Normaliser (0-1)
        return min(1.0, efficiency * 1000)  # Ajuster facteur selon échelle

    def _detect_cvd_flow_reversal(self, cvd_values: List[float]) -> bool:
        """Détecte retournement de flux CVD"""
        if len(cvd_values) < 20:
            return False

        cvd = np.array(cvd_values[-20:])

        # Tendance 1ère moitié vs 2ème moitié
        trend1 = np.polyfit(range(10), cvd[:10], 1)[0]
        trend2 = np.polyfit(range(10), cvd[10:], 1)[0]

        # Retournement si signe change ET différence >50%
        if (trend1 > 0 and trend2 < 0) or (trend1 < 0 and trend2 > 0):
            if abs(trend2 - trend1) > abs(trend1) * 0.5:
                return True

        return False

    # ═══════════════════════════════════════════════════════════════
    # FONCTIONS UTILITAIRES
    # ═══════════════════════════════════════════════════════════════

    def _empty_signal(self, name: str) -> ReversalSignal:
        """Signal vide (données insuffisantes)"""
        return ReversalSignal(
            name=name,
            strength=0.0,
            confidence=0.0,
            direction="NEUTRAL",
            timestamp=pd.Timestamp.now(),
            metadata={"error": "Insufficient data"}
        )

    def _calculate_institutional_score(self, signals: List[ReversalSignal]) -> float:
        """
        Calcule un score institutionnel pondéré.

        27 FEV 2026 — Correction normalisation :
        Ancienne formule : weighted_sum / 1.0 → score 7-19/100 même pour signaux forts
        car les layers inactifs (strength=0) pesaient dans le dénominateur.

        Nouvelle formule : normalisation par les poids × confidence des seuls layers
        ACTIFS (strength > 0). Un signal fort sur 1 layer = score élevé sur ce layer.
        Score = "à quel point les signaux qui ont détecté quelque chose sont-ils convaincants?"
        """
        if not signals:
            return 0.0

        total_weight = 0.0
        weighted_sum = 0.0

        weights = self.config["signals"]

        for signal in signals:
            # Mapping nom → poids
            weight_key = None
            if "CHANGEPOINT" in signal.name:
                weight_key = "changepoint_weight"
            elif "DIVERGENCE" in signal.name:
                weight_key = "divergence_weight"
            elif "FATIGUE" in signal.name:
                weight_key = "fatigue_weight"
            elif "SMART_MONEY" in signal.name:
                weight_key = "accumulation_weight"
            elif "PATTERN" in signal.name or "ML" in signal.name:
                weight_key = "pattern_weight"
            elif "CAPITAL_FLOWS" in signal.name or "CVD" in signal.name:
                weight_key = "capital_flows_weight"
            else:
                # Bonus layers (confluence) - pas de poids fixe
                continue

            if weight_key and weight_key in weights:
                weight = weights[weight_key]
                if signal.strength > 0:
                    # 27 FEV 2026: normaliser uniquement sur les signaux actifs
                    # → les layers silencieux ne diluent plus le score
                    weighted_sum += signal.strength * weight * signal.confidence
                    total_weight += weight * signal.confidence

        if total_weight == 0:
            return 0.0

        # Normalisation sur les signaux actifs uniquement
        final_score = min(100.0, weighted_sum / total_weight)

        return final_score

    def _detect_regime_change(self, market_data: Dict, score: float) -> Dict:
        """Détecte un changement de régime de marché"""
        candles_m5 = market_data.get('candles_m5', pd.DataFrame())

        if len(candles_m5) < 10:
            return {"detected": False, "new_regime": self.market_regime}

        prices = candles_m5['close'].values[-100:] if len(candles_m5) >= 100 else candles_m5['close'].values

        # Calcul de régimes
        volatility = np.std(np.diff(np.log(prices)))
        trend_strength = self._calculate_trend_strength_impl(prices)

        # Détection
        if score > 75 and volatility > np.mean(prices) * 0.002:
            new_regime = "TRANSITION"
            self.regime_confidence = 0.8
        elif trend_strength > 0.7:
            new_regime = "TRENDING_BULL" if np.mean(prices[-5:]) > np.mean(prices[-20:-5] if len(prices) >= 20 else prices[:-5]) else "TRENDING_BEAR"
            self.regime_confidence = 0.9
        else:
            new_regime = "RANGING"
            self.regime_confidence = 0.6

        detected = new_regime != self.market_regime
        self.market_regime = new_regime

        return {
            "detected": detected,
            "new_regime": new_regime,
            "confidence": self.regime_confidence,
            "volatility": volatility,
            "trend_strength": trend_strength
        }

    def _calculate_trend_strength_impl(self, prices: np.ndarray) -> float:
        """Force de tendance (0-1)"""
        if len(prices) < 20:
            return 0.0

        # Régression linéaire
        trend = np.polyfit(range(len(prices)), prices, 1)[0]

        # R² (coefficient de détermination)
        prices_mean = np.mean(prices)
        ss_total = np.sum((prices - prices_mean) ** 2)

        if ss_total == 0:
            return 0.0

        # Prédictions
        x = np.arange(len(prices))
        predictions = trend * x + (prices_mean - trend * len(prices) / 2)
        ss_residual = np.sum((prices - predictions) ** 2)

        r_squared = 1 - (ss_residual / ss_total)

        return max(0.0, min(1.0, r_squared))

    def _validate_smart_money(self, market_data: Dict) -> Dict:
        """
        08 FEV 2026: Vraie validation Smart Money basée sur 3 critères.
        validated=True seulement si au moins 2/3 critères satisfaits.
        """
        candles_m5 = market_data.get('candles_m5', pd.DataFrame())
        candles_m1 = market_data.get('candles_m1', pd.DataFrame())
        volume_values = market_data.get('volume_values', [])
        cvd_values = market_data.get('cvd_values', [])

        criteria_met = 0
        validation_methods = []

        # Critère 1: Volume Profile Confirmation
        # Volume sur les dernières bougies doit être au-dessus de la moyenne
        try:
            if len(volume_values) >= 10:
                recent_vol = volume_values[-5:]
                avg_vol = np.mean(volume_values[-20:]) if len(volume_values) >= 20 else np.mean(volume_values)
                recent_avg = np.mean(recent_vol)
                if avg_vol > 0 and recent_avg >= avg_vol * 1.2:
                    criteria_met += 1
                    validation_methods.append("VOLUME_PROFILE_CONFIRMED")
                else:
                    validation_methods.append("VOLUME_PROFILE_WEAK")
            else:
                validation_methods.append("VOLUME_PROFILE_NO_DATA")
        except Exception:
            validation_methods.append("VOLUME_PROFILE_ERROR")

        # Critère 2: Price Action - Swing Points Validation
        # Vérifier si les dernières bougies forment un swing (HH/HL ou LH/LL)
        try:
            candles_for_pa = candles_m1 if candles_m1 is not None and len(candles_m1) >= 10 else candles_m5
            if candles_for_pa is not None and len(candles_for_pa) >= 10:
                highs = candles_for_pa['high'].values[-10:]
                lows = candles_for_pa['low'].values[-10:]

                # Chercher un swing point dans les 5 dernières bougies
                # Swing High: bougie[i] high > bougie[i-1] high ET > bougie[i+1] high
                # Swing Low: bougie[i] low < bougie[i-1] low ET < bougie[i+1] low
                swing_detected = False
                for i in range(1, len(highs) - 1):
                    if highs[i] > highs[i-1] and highs[i] > highs[i+1]:
                        swing_detected = True
                        break
                    if lows[i] < lows[i-1] and lows[i] < lows[i+1]:
                        swing_detected = True
                        break

                if swing_detected:
                    criteria_met += 1
                    validation_methods.append("PRICE_ACTION_SWING_CONFIRMED")
                else:
                    validation_methods.append("PRICE_ACTION_NO_SWING")
            else:
                validation_methods.append("PRICE_ACTION_NO_DATA")
        except Exception:
            validation_methods.append("PRICE_ACTION_ERROR")

        # Critère 3: CVD Flow Divergence
        # CVD doit montrer un changement de direction cohérent
        try:
            if len(cvd_values) >= 10:
                cvd_recent = cvd_values[-5:]
                cvd_older = cvd_values[-10:-5]

                cvd_recent_slope = cvd_recent[-1] - cvd_recent[0] if len(cvd_recent) >= 2 else 0
                cvd_older_slope = cvd_older[-1] - cvd_older[0] if len(cvd_older) >= 2 else 0

                # Divergence = changement de direction du CVD
                if (cvd_recent_slope > 0 and cvd_older_slope < 0) or \
                   (cvd_recent_slope < 0 and cvd_older_slope > 0):
                    criteria_met += 1
                    validation_methods.append("CVD_DIVERGENCE_CONFIRMED")
                elif abs(cvd_recent_slope) > abs(cvd_older_slope) * 1.5:
                    # Accélération du CVD = signal fort
                    criteria_met += 1
                    validation_methods.append("CVD_ACCELERATION_CONFIRMED")
                else:
                    validation_methods.append("CVD_NO_DIVERGENCE")
            else:
                validation_methods.append("CVD_NO_DATA")
        except Exception:
            validation_methods.append("CVD_ERROR")

        # Validation: au moins 1/3 critères doivent être satisfaits
        validated = criteria_met >= 1
        confidence = criteria_met / 3.0

        return {
            "validated": validated,
            "confidence": round(confidence, 2),
            "institutional_bias": "ALIGNED" if validated else "UNCONFIRMED",
            "criteria_met": criteria_met,
            "validation_methods": validation_methods
        }

    def _make_institutional_decision(self, score: float,
                                    regime_change: Dict,
                                    smart_money: Dict) -> Dict:
        """Prend une décision institutionnelle"""

        if score >= 80 and regime_change["detected"] and smart_money["validated"]:
            action = "HIGH_CONVICTION_REVERSAL"
            conviction_level = "HIGH"
            confidence = 0.9
        elif score >= 65 and regime_change["detected"]:
            action = "MODERATE_CONVICTION_REVERSAL"
            conviction_level = "MODERATE"
            confidence = 0.75
        elif score >= 50:
            action = "CAUTION_REVERSAL_WARNING"
            conviction_level = "CAUTION"
            confidence = 0.6
        else:
            action = "NO_REVERSAL_DETECTED"
            conviction_level = "LOW"
            confidence = 0.4

        return {
            "action": action,
            "conviction_level": conviction_level,
            "confidence": confidence
        }

    def _determine_new_trend(self, result: Dict) -> str:
        """Détermine nouvelle tendance basée sur signaux (pondéré par force)"""
        signals_breakdown = result.get("signals_breakdown", [])

        bullish_weight = 0.0
        bearish_weight = 0.0

        for s in signals_breakdown:
            strength = s.get("strength", 0)
            direction = s.get("direction", "NEUTRAL")
            if direction == "BULLISH":
                bullish_weight += strength
            elif direction == "BEARISH":
                bearish_weight += strength

        # Seuil de significativité : 20% de différence pour éviter les faux positifs
        if bullish_weight > bearish_weight * 1.2:
            return "BULLISH"
        elif bearish_weight > bullish_weight * 1.2:
            return "BEARISH"
        else:
            return "NEUTRAL"

    # === Fonctions helper SmartModeSwitcher supprimées (14 JAN 2026) ===
    # Plus besoin de _calculate_simple_score et _map_action_to_switcher

    def _format_signals_breakdown(self, signals: List[ReversalSignal]) -> List[Dict]:
        """Formate les signaux pour l'affichage"""
        return [
            {
                "name": s.name,
                "strength": s.strength,
                "confidence": s.confidence,
                "direction": s.direction,
                "timestamp": s.timestamp.isoformat(),
                "metadata": s.metadata
            }
            for s in signals
        ]
