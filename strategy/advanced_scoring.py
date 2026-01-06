"""
SimpleAdvancedScorer - Système de scoring composite évolutif

Architecture à 5 composants pondérés :
- OrderFlow (50%) : Score V6 existant
- Microstructure (20%) : Tape speed, accélération, clusters
- Liquidity (15%) : Pressure ratio, continuité
- Divergence (10%) : Divergence price/delta
- Smart Money (5%) : Large ticks, absorption patterns

Date création : 03 Janvier 2026
"""

import logging
import pandas as pd
import numpy as np
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


class SimpleAdvancedScorer:
    """
    Scoring composite évolutif pour analyse multi-dimensionnelle du marché.

    Combine 5 signaux institutionnels avec pondération configurable.
    """

    def __init__(self, config: Optional[Dict[str, float]] = None, thresholds: Optional[Dict[str, float]] = None):
        """
        Initialiser le scorer avec poids et seuils configurables.

        Args:
            config: Dictionnaire de poids (optionnel)
                   Si None, utilise poids par défaut
            thresholds: Seuils de décision (optionnel)
                       Si None, utilise seuils par défaut
        """
        # Poids par défaut (somme = 1.0)
        # 06 JAN 2026 PHASE 3: Intégration des 5 analyseurs institutionnels!
        self.weights = {
            'orderflow': 0.35,      # Score V6 existant (réduit 50→35%)
            'institutional': 0.25,  # 🆕 Les 5 analyseurs sophistiqués!
            'microstructure': 0.15, # Tape speed, clusters (réduit 20→15%)
            'liquidity': 0.15,      # Pressure, continuité
            'divergence': 0.05,     # Price/delta divergence (réduit 10→5%)
            'smart_money': 0.05     # Large ticks, absorption
        }

        # 🔧 FIX BUG #3 (05 JAN 2026): Seuils configurables au lieu de hardcodés
        self.thresholds = {
            'STRONG_THRESHOLD': 75.0,
            'GOOD_THRESHOLD': 65.0,
            'WEAK_THRESHOLD': 55.0,
            'NEUTRAL_LOW': 45.0,
            'NEUTRAL_HIGH': 55.0
        }

        # Override avec config custom si fourni
        if config:
            for key in self.weights:
                if key in config:
                    self.weights[key] = config[key]

        # Override seuils si fournis
        if thresholds:
            for key in self.thresholds:
                if key in thresholds:
                    self.thresholds[key] = thresholds[key]

        # Normaliser pour garantir somme = 1.0
        total_weight = sum(self.weights.values())
        if total_weight != 1.0:
            logger.warning(f"[ADVANCED_SCORER] Poids non normalisés (somme={total_weight:.2f}), normalisation automatique")
            for key in self.weights:
                self.weights[key] /= total_weight

        logger.info(f"[ADVANCED_SCORER] Initialisé avec poids: {self.weights}, seuils: {self.thresholds}")


    def calculate_composite_score(
        self,
        ticks_df: Optional[pd.DataFrame],
        candles_df: pd.DataFrame,
        orderflow_score: float,
        institutional_analysis: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Calculer le score composite à partir des 6 composants (PHASE 3 - 06 JAN 2026).

        Args:
            ticks_df: DataFrame des ticks (None si indisponible)
            candles_df: DataFrame M1 (minimum 10 bougies)
            orderflow_score: Score OrderFlow V6 (0-100)
            institutional_analysis: Résultats des 5 analyseurs institutionnels

        Returns:
            {
                'composite_score': float (0-100),
                'components': dict (scores individuels),
                'decision': str (BUY/SELL/HOLD),
                'confidence': str (STRONG/GOOD/WEAK/NONE),
                'details': dict (métriques détaillées)
            }
        """
        # Valider inputs
        if candles_df is None or candles_df.empty:
            logger.error("[ADVANCED_SCORER] candles_df vide, impossible de scorer")
            return self._default_result(reason="NO_CANDLES")

        if len(candles_df) < 10:
            logger.warning(f"[ADVANCED_SCORER] candles_df trop court ({len(candles_df)} bougies), scores limités")

        # Calculer chaque composant
        # 🔧 FIX BUG #8 (05 JAN 2026): Défauts 0.0 au lieu de 50.0 pour pénaliser absence de données
        # Ancien comportement: 50.0 neutre → composite biaisé vers 75+ même sans signal
        # Nouveau comportement: 0.0 pénalité → composite reflète vraiment la qualité du signal
        # 🆕 06 JAN 2026 PHASE 3: Ajout du composant institutional (les 5 analyseurs!)
        try:
            components = {
                'orderflow': orderflow_score,  # Déjà calculé
                'institutional': self._calculate_institutional_score(institutional_analysis) if institutional_analysis else 50.0,  # 🆕 Les 5 analyseurs!
                'microstructure': self._calculate_microstructure_score(ticks_df) if ticks_df is not None else 0.0,
                'liquidity': self._calculate_liquidity_score(ticks_df) if ticks_df is not None else 0.0,
                'divergence': self._calculate_divergence_score(ticks_df, candles_df) if ticks_df is not None else 0.0,
                'smart_money': self._calculate_smart_money_score(ticks_df) if ticks_df is not None else 0.0
            }
        except Exception as e:
            logger.error(f"[ADVANCED_SCORER] Erreur calcul composants: {e}", exc_info=True)
            return self._default_result(reason=f"CALC_ERROR: {e}")

        # Score composite pondéré
        composite_score = sum(
            components[comp] * self.weights[comp]
            for comp in components
        )

        # Clamp 0-100
        composite_score = max(0.0, min(100.0, composite_score))

        # Déterminer décision et confiance
        decision, confidence = self._determine_decision(composite_score, components)

        # Résultat détaillé
        result = {
            'composite_score': round(composite_score, 2),
            'components': {k: round(v, 2) for k, v in components.items()},
            'decision': decision,
            'confidence': confidence,
            'details': {
                'weights': self.weights,
                'has_ticks': ticks_df is not None,
                'candles_count': len(candles_df)
            }
        }

        logger.info(
            f"[ADVANCED_SCORER] Composite={composite_score:.1f}/100 | "
            f"Decision={decision} ({confidence}) | "
            f"Components: OF={components['orderflow']:.0f} INST={components['institutional']:.0f} MS={components['microstructure']:.0f} "
            f"LQ={components['liquidity']:.0f} DV={components['divergence']:.0f} SM={components['smart_money']:.0f}"
        )

        return result


    def _calculate_microstructure_score(self, ticks_df: pd.DataFrame) -> float:
        """
        Analyser la microstructure du marché (tape speed, accélération, clusters).

        Score basé sur :
        - Tape speed (ticks/seconde) : vitesse du tape
        - Accélération : variation de la vitesse
        - Clusters : concentration des trades

        Returns:
            Score 0-100
        """
        if ticks_df is None or ticks_df.empty or len(ticks_df) < 10:
            return 50.0  # Neutre si données insuffisantes

        try:
            # 1. Tape speed (ticks par seconde)
            if 'time' in ticks_df.columns:
                ticks_df['time'] = pd.to_datetime(ticks_df['time'], unit='s')
                duration_seconds = (ticks_df['time'].max() - ticks_df['time'].min()).total_seconds()
                if duration_seconds > 0:
                    tape_speed = len(ticks_df) / duration_seconds
                else:
                    tape_speed = 0.0
            else:
                tape_speed = 0.0

            # Score tape speed : 0-5 ticks/s → 0-100
            speed_score = min(100.0, (tape_speed / 5.0) * 100.0)

            # 2. Accélération (variance de la vitesse)
            # Diviser en 5 segments et calculer vitesse par segment
            segment_size = max(1, len(ticks_df) // 5)
            speeds = []
            for i in range(5):
                start_idx = i * segment_size
                end_idx = min((i + 1) * segment_size, len(ticks_df))
                segment = ticks_df.iloc[start_idx:end_idx]

                if 'time' in segment.columns and len(segment) > 1:
                    seg_duration = (segment['time'].max() - segment['time'].min()).total_seconds()
                    if seg_duration > 0:
                        speeds.append(len(segment) / seg_duration)

            if len(speeds) >= 2:
                acceleration = np.std(speeds)  # Écart-type = accélération
                accel_score = min(100.0, (acceleration / 2.0) * 100.0)
            else:
                accel_score = 50.0

            # 3. Clusters (concentration des trades)
            # Mesurer si les ticks sont groupés ou dispersés
            if 'volume' in ticks_df.columns:
                # Volume moyen des 20% plus gros trades
                top_20_pct = int(len(ticks_df) * 0.2)
                if top_20_pct > 0:
                    sorted_vol = ticks_df['volume'].sort_values(ascending=False)
                    avg_top = sorted_vol.iloc[:top_20_pct].mean()
                    avg_all = ticks_df['volume'].mean()

                    if avg_all > 0:
                        cluster_ratio = avg_top / avg_all
                        cluster_score = min(100.0, (cluster_ratio / 3.0) * 100.0)
                    else:
                        cluster_score = 50.0
                else:
                    cluster_score = 50.0
            else:
                cluster_score = 50.0

            # Score final : moyenne pondérée
            microstructure_score = (
                speed_score * 0.5 +      # Tape speed = 50%
                accel_score * 0.3 +      # Accélération = 30%
                cluster_score * 0.2      # Clusters = 20%
            )

            return round(microstructure_score, 2)

        except Exception as e:
            logger.error(f"[MICROSTRUCTURE] Erreur calcul: {e}", exc_info=True)
            return 50.0


    def _calculate_liquidity_score(self, ticks_df: pd.DataFrame) -> float:
        """
        Analyser la liquidité (pressure ratio, continuité).

        Score basé sur :
        - Buy/Sell pressure ratio
        - Continuité (absence de gaps)
        - Volume stability

        Returns:
            Score 0-100
        """
        if ticks_df is None or ticks_df.empty or len(ticks_df) < 10:
            return 50.0

        try:
            # 1. Buy/Sell pressure ratio
            if 'flags' in ticks_df.columns:
                # MT5 flags: 2=buy, 1=sell
                buy_ticks = (ticks_df['flags'] == 2).sum()
                sell_ticks = (ticks_df['flags'] == 1).sum()

                if sell_ticks > 0:
                    pressure_ratio = buy_ticks / sell_ticks
                    # Ratio équilibré (0.8-1.2) = score élevé
                    # Ratio déséquilibré = score bas (manque liquidité)
                    if 0.8 <= pressure_ratio <= 1.2:
                        pressure_score = 100.0
                    else:
                        # Distance de l'équilibre
                        deviation = abs(pressure_ratio - 1.0)
                        pressure_score = max(0.0, 100.0 - (deviation * 50.0))
                else:
                    pressure_score = 50.0
            else:
                pressure_score = 50.0

            # 2. Continuité (gaps entre ticks)
            if 'time' in ticks_df.columns:
                ticks_df_sorted = ticks_df.sort_values('time')
                time_diffs = ticks_df_sorted['time'].diff().dt.total_seconds()

                # Gaps > 1 seconde = manque de liquidité
                gaps = (time_diffs > 1.0).sum()
                gap_pct = (gaps / len(ticks_df)) * 100.0

                # Moins de gaps = meilleur score
                continuity_score = max(0.0, 100.0 - gap_pct)
            else:
                continuity_score = 50.0

            # 3. Volume stability
            if 'volume' in ticks_df.columns:
                vol_std = ticks_df['volume'].std()
                vol_mean = ticks_df['volume'].mean()

                if vol_mean > 0:
                    cv = vol_std / vol_mean  # Coefficient of variation
                    # CV faible = stable = bonne liquidité
                    stability_score = max(0.0, 100.0 - (cv * 100.0))
                else:
                    stability_score = 50.0
            else:
                stability_score = 50.0

            # Score final : moyenne pondérée
            liquidity_score = (
                pressure_score * 0.4 +      # Pressure ratio = 40%
                continuity_score * 0.4 +    # Continuité = 40%
                stability_score * 0.2       # Volume stability = 20%
            )

            return round(liquidity_score, 2)

        except Exception as e:
            logger.error(f"[LIQUIDITY] Erreur calcul: {e}", exc_info=True)
            return 50.0


    def _calculate_divergence_score(self, ticks_df: pd.DataFrame, candles_df: pd.DataFrame) -> float:
        """
        Détecter divergences price/delta.

        Score basé sur :
        - Divergence entre direction price et delta
        - Force de la divergence

        Returns:
            Score 0-100 (50=pas de divergence, >50=divergence haussière, <50=baissière)
        """
        if ticks_df is None or ticks_df.empty or candles_df is None or len(candles_df) < 5:
            return 50.0

        try:
            # 1. Direction du prix (dernières 5 bougies)
            last_5 = candles_df.tail(5)
            price_change = last_5['close'].iloc[-1] - last_5['close'].iloc[0]
            price_direction = 1 if price_change > 0 else -1 if price_change < 0 else 0

            # 2. Direction du delta (buy - sell volume)
            if 'flags' in ticks_df.columns and 'volume' in ticks_df.columns:
                buy_vol = ticks_df[ticks_df['flags'] == 2]['volume'].sum()
                sell_vol = ticks_df[ticks_df['flags'] == 1]['volume'].sum()
                delta = buy_vol - sell_vol
                delta_direction = 1 if delta > 0 else -1 if delta < 0 else 0
            else:
                return 50.0

            # 3. Détecter divergence
            if price_direction == 0 or delta_direction == 0:
                return 50.0  # Pas de divergence claire

            # Divergence = directions opposées
            is_divergent = (price_direction != delta_direction)

            if is_divergent:
                # Divergence haussière : price down, delta up → score > 50
                # Divergence baissière : price up, delta down → score < 50
                if delta_direction > 0:
                    # Delta haussier, price baissier → signal BUY potentiel
                    divergence_score = 75.0
                else:
                    # Delta baissier, price haussier → signal SELL potentiel
                    divergence_score = 25.0
            else:
                # Pas de divergence → neutre
                divergence_score = 50.0

            return round(divergence_score, 2)

        except Exception as e:
            logger.error(f"[DIVERGENCE] Erreur calcul: {e}", exc_info=True)
            return 50.0


    def _calculate_smart_money_score(self, ticks_df: pd.DataFrame) -> float:
        """
        Détecter empreinte smart money (large ticks, absorption).

        Score basé sur :
        - Présence de large ticks (>3x moyenne)
        - Absorption patterns (large volume sans mouvement prix)

        Returns:
            Score 0-100
        """
        if ticks_df is None or ticks_df.empty or len(ticks_df) < 10:
            return 50.0

        try:
            # 1. Large ticks (>3x volume moyen)
            if 'volume' in ticks_df.columns:
                vol_mean = ticks_df['volume'].mean()
                large_ticks = (ticks_df['volume'] > vol_mean * 3.0).sum()
                large_tick_pct = (large_ticks / len(ticks_df)) * 100.0

                # Plus de large ticks = plus d'activité institutionnelle
                large_tick_score = min(100.0, large_tick_pct * 10.0)
            else:
                large_tick_score = 50.0

            # 2. Absorption patterns (gros volume, petit mouvement)
            if 'volume' in ticks_df.columns and 'bid' in ticks_df.columns and 'ask' in ticks_df.columns:
                ticks_df['mid'] = (ticks_df['bid'] + ticks_df['ask']) / 2.0

                # Diviser en segments de 10 ticks
                absorption_count = 0
                for i in range(0, len(ticks_df) - 10, 10):
                    segment = ticks_df.iloc[i:i+10]
                    total_vol = segment['volume'].sum()
                    price_range = segment['mid'].max() - segment['mid'].min()

                    # Absorption : gros volume (>80% du max) mais faible mouvement (<0.0002)
                    max_vol = ticks_df['volume'].max() * 10  # 10 ticks
                    if total_vol > max_vol * 0.5 and price_range < 0.0002:
                        absorption_count += 1

                # Score absorption
                if len(ticks_df) >= 10:
                    absorption_pct = (absorption_count / (len(ticks_df) // 10)) * 100.0
                    absorption_score = min(100.0, absorption_pct * 5.0)
                else:
                    absorption_score = 50.0
            else:
                absorption_score = 50.0

            # Score final : moyenne
            smart_money_score = (large_tick_score + absorption_score) / 2.0

            return round(smart_money_score, 2)

        except Exception as e:
            logger.error(f"[SMART_MONEY] Erreur calcul: {e}", exc_info=True)
            return 50.0


    def _calculate_institutional_score(self, institutional_analysis: Dict[str, Any]) -> float:
        """
        🆕 06 JAN 2026 PHASE 3: Agréger les 5 analyseurs institutionnels en score 0-100.

        Les 5 analyseurs:
        1. Price Memory → Qualité des niveaux mémoire
        2. Market Fatigue → État d'épuisement du marché
        3. Market Physics → Bias physique (inertie, momentum)
        4. Microstructure (Tape Speed) → Vitesse et ignition
        5. Liquidity Heatmap (Pressure) → Pression buy/sell

        Args:
            institutional_analysis: Dict contenant les 5 analyses

        Returns:
            Score 0-100 (0=bearish fort, 50=neutre, 100=bullish fort)
        """
        if not institutional_analysis:
            return 50.0  # Neutre si pas de données

        try:
            scores = []
            weights = []

            # 1. PRICE MEMORY (20%) - Niveaux frais vs memory signals
            price_memory = institutional_analysis.get('price_memory', {})
            memory_signals = price_memory.get('memory_signals', [])
            fresh_levels = price_memory.get('fresh_levels', [])

            if memory_signals or fresh_levels:
                # Plus de niveaux frais = bullish (opportunité), plus de memory = résistance
                fresh_ratio = len(fresh_levels) / max(1, len(memory_signals) + len(fresh_levels))
                memory_score = 50.0 + (fresh_ratio - 0.5) * 50.0  # 0→25, 0.5→50, 1→75
                scores.append(memory_score)
                weights.append(0.20)
                logger.debug(f"[INST_SCORE] PriceMemory: {memory_score:.1f} (fresh={len(fresh_levels)}, memory={len(memory_signals)})")

            # 2. MARKET FATIGUE (25%) - État du marché
            market_fatigue = institutional_analysis.get('market_fatigue', {})
            fatigue_state = str(market_fatigue.get('market_state', 'NEUTRAL')).upper()
            fatigue_score_raw = float(market_fatigue.get('fatigue_score', 5.0))  # 0-10

            if fatigue_state != 'UNKNOWN':
                # Fatigue high = bullish exhausted (bearish), fatigue low = fresh (bullish)
                fatigue_score = 50.0 + (5.0 - fatigue_score_raw) * 5.0  # 10→0, 5→50, 0→100

                # Ajustement par état
                if fatigue_state == 'EXHAUSTED_BUYERS':
                    fatigue_score = min(fatigue_score, 30.0)  # Force bearish
                elif fatigue_state == 'EXHAUSTED_SELLERS':
                    fatigue_score = max(fatigue_score, 70.0)  # Force bullish
                elif fatigue_state == 'BALANCED':
                    fatigue_score = 50.0

                scores.append(fatigue_score)
                weights.append(0.25)
                logger.debug(f"[INST_SCORE] MarketFatigue: {fatigue_score:.1f} (state={fatigue_state}, score={fatigue_score_raw:.1f}/10)")

            # 3. MARKET PHYSICS (25%) - Bias physique
            market_physics = institutional_analysis.get('market_physics', {})
            physics_bias = str(market_physics.get('physics_bias', 'NEUTRAL')).upper()
            price_inertia = market_physics.get('price_inertia', {})
            inertia_dir = str(price_inertia.get('direction', 'NEUTRAL')).upper()

            if physics_bias != 'UNKNOWN':
                # Convertir bias en score
                if 'BULLISH' in physics_bias or 'BUY' in physics_bias:
                    physics_score = 75.0
                elif 'BEARISH' in physics_bias or 'SELL' in physics_bias:
                    physics_score = 25.0
                else:
                    physics_score = 50.0

                # Boost si inertie alignée
                if inertia_dir == 'UPWARD':
                    physics_score = min(100.0, physics_score + 10.0)
                elif inertia_dir == 'DOWNWARD':
                    physics_score = max(0.0, physics_score - 10.0)

                scores.append(physics_score)
                weights.append(0.25)
                logger.debug(f"[INST_SCORE] MarketPhysics: {physics_score:.1f} (bias={physics_bias}, inertia={inertia_dir})")

            # 4. TAPE SPEED (15%) - Vitesse du tape
            tape_speed = institutional_analysis.get('tape_speed', {})
            speed_ratio = float(tape_speed.get('speed_ratio', 1.0))
            speed_interp = str(tape_speed.get('interpretation', 'NORMAL')).upper()

            if speed_ratio > 0:
                # Speed élevé = activité (neutre à bullish), speed faible = apathie (bearish)
                if speed_ratio >= 2.0:
                    tape_score = 70.0  # Haute activité
                elif speed_ratio >= 1.5:
                    tape_score = 60.0
                elif speed_ratio >= 0.8:
                    tape_score = 50.0  # Normal
                else:
                    tape_score = 35.0  # Apathie

                scores.append(tape_score)
                weights.append(0.15)
                logger.debug(f"[INST_SCORE] TapeSpeed: {tape_score:.1f} (ratio={speed_ratio:.2f}, interp={speed_interp})")

            # 5. PRESSURE RATIO (15%) - Pression buy/sell
            pressure_ratio = institutional_analysis.get('pressure_ratio', {})
            pressure_dir = str(pressure_ratio.get('direction', 'NEUTRAL')).upper()
            pressure_norm = float(pressure_ratio.get('normalized_pressure', 0.0))  # -1 à +1

            if pressure_dir != 'UNKNOWN':
                # Convertir pression en score
                pressure_score = 50.0 + (pressure_norm * 50.0)  # -1→0, 0→50, +1→100

                scores.append(pressure_score)
                weights.append(0.15)
                logger.debug(f"[INST_SCORE] Pressure: {pressure_score:.1f} (dir={pressure_dir}, norm={pressure_norm:.2f})")

            # Calcul final pondéré
            if scores:
                total_weight = sum(weights)
                institutional_score = sum(s * w for s, w in zip(scores, weights)) / total_weight
                institutional_score = max(0.0, min(100.0, institutional_score))

                logger.info(
                    f"[INST_SCORE] ✅ Score Institutionnel={institutional_score:.1f}/100 "
                    f"({len(scores)}/5 analyseurs actifs)"
                )
                return round(institutional_score, 2)
            else:
                logger.warning("[INST_SCORE] Aucun analyseur actif, score neutre 50.0")
                return 50.0

        except Exception as e:
            logger.error(f"[INST_SCORE] Erreur calcul: {e}", exc_info=True)
            return 50.0


    def _determine_decision(self, composite_score: float, components: Dict[str, float]) -> tuple:
        """
        Déterminer la décision (BUY/SELL/HOLD) et le niveau de confiance.

        Args:
            composite_score: Score composite 0-100
            components: Scores individuels des composants

        Returns:
            (decision, confidence)
        """
        # 🔧 FIX BUG #3 (05 JAN 2026): Utiliser seuils configurables depuis self.thresholds
        STRONG_THRESHOLD = self.thresholds['STRONG_THRESHOLD']
        GOOD_THRESHOLD = self.thresholds['GOOD_THRESHOLD']
        WEAK_THRESHOLD = self.thresholds['WEAK_THRESHOLD']
        NEUTRAL_LOW = self.thresholds['NEUTRAL_LOW']
        NEUTRAL_HIGH = self.thresholds['NEUTRAL_HIGH']

        # Déterminer direction
        if composite_score >= NEUTRAL_HIGH:
            decision = "BUY"
        elif composite_score <= NEUTRAL_LOW:
            decision = "SELL"
        else:
            decision = "HOLD"

        # Déterminer confiance
        if decision == "HOLD":
            confidence = "NONE"
        elif composite_score >= STRONG_THRESHOLD or composite_score <= (100.0 - STRONG_THRESHOLD):
            confidence = "STRONG"
        elif composite_score >= GOOD_THRESHOLD or composite_score <= (100.0 - GOOD_THRESHOLD):
            confidence = "GOOD"
        elif composite_score >= WEAK_THRESHOLD or composite_score <= (100.0 - WEAK_THRESHOLD):
            confidence = "WEAK"
        else:
            confidence = "NONE"

        return decision, confidence


    def _default_result(self, reason: str = "UNKNOWN") -> Dict[str, Any]:
        """
        Résultat par défaut en cas d'erreur.
        """
        return {
            'composite_score': 50.0,
            'components': {
                'orderflow': 50.0,
                'microstructure': 50.0,
                'liquidity': 50.0,
                'divergence': 50.0,
                'smart_money': 50.0
            },
            'decision': 'HOLD',
            'confidence': 'NONE',
            'details': {
                'error': reason,
                'weights': self.weights
            }
        }


def test_scorer():
    """
    Test rapide du scorer avec données simulées.
    """
    print("\n" + "="*60)
    print("TEST SimpleAdvancedScorer")
    print("="*60)

    # Créer scorer avec poids par défaut
    scorer = SimpleAdvancedScorer()

    # Données simulées
    ticks_data = {
        'time': pd.date_range('2026-01-03 10:00:00', periods=100, freq='100ms'),
        'bid': np.random.uniform(1.0850, 1.0855, 100),
        'ask': np.random.uniform(1.0851, 1.0856, 100),
        'volume': np.random.randint(1, 10, 100),
        'flags': np.random.choice([1, 2], 100)  # 1=sell, 2=buy
    }
    ticks_df = pd.DataFrame(ticks_data)

    candles_data = {
        'time': pd.date_range('2026-01-03 10:00:00', periods=20, freq='1min'),
        'open': np.random.uniform(1.0850, 1.0855, 20),
        'high': np.random.uniform(1.0855, 1.0860, 20),
        'low': np.random.uniform(1.0845, 1.0850, 20),
        'close': np.random.uniform(1.0850, 1.0855, 20),
        'volume': np.random.randint(100, 500, 20)
    }
    candles_df = pd.DataFrame(candles_data)

    # Test avec différents scores OrderFlow
    for of_score in [30.0, 50.0, 70.0, 90.0]:
        print(f"\n--- Test OrderFlow Score = {of_score} ---")
        result = scorer.calculate_composite_score(
            ticks_df=ticks_df,
            candles_df=candles_df,
            orderflow_score=of_score
        )

        print(f"Composite Score: {result['composite_score']:.1f}/100")
        print(f"Decision: {result['decision']} ({result['confidence']})")
        print(f"Components: {result['components']}")

    print("\n" + "="*60)
    print("TEST TERMINÉ")
    print("="*60 + "\n")


if __name__ == "__main__":
    # Test si exécuté directement
    test_scorer()
