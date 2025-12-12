"""
DataEngine - Thread d'Analyse Asynchrone Footprint

Ce thread tourne en arrière-plan pour:
1. Récupérer les ticks des symboles prioritaires
2. Calculer le footprint M1 en continu
3. Mettre à jour le cache partagé

Le thread SCALPING lit ensuite ce cache sans bloquer.

Date: 26 Novembre 2025
"""

import threading
import time
import logging
from typing import List, Optional, Dict, Any

from core.footprint_cache import footprint_cache
from core.bars_cache import bars_cache  # ✅ PHASE 2: Cache barres historiques


logger = logging.getLogger(__name__)


class DataEngine(threading.Thread):
    """
    Thread d'analyse asynchrone des données footprint.

    Boucle toutes les `update_interval_seconds` pour:
    - Analyser le footprint de chaque symbole prioritaire
    - Mettre à jour le cache partagé

    Le thread SCALPING peut alors lire le cache instantanément.
    """

    def __init__(
        self,
        symbols: List[str],
        mt5_connector,
        market_analyzer,
        update_interval_seconds: float = 2.5,  # ⚡ OPTIMISÉ: 2.5s (au lieu de 5s) pour mouvements ultra-rapides
        stop_event: Optional[threading.Event] = None
    ):
        """
        Initialise le DataEngine.

        Args:
            symbols: Liste des symboles à analyser (ex: ['XAUUSD', 'EURUSD'])
            mt5_connector: Instance du connecteur MT5 pour récupérer les ticks
            market_analyzer: Instance du MarketAnalyzer pour analyser footprint
            update_interval_seconds: Intervalle entre chaque cycle (défaut: 2.5s) ⚡ MODE ULTRA-RAPIDE
            stop_event: Event partagé pour arrêter proprement le thread
        """
        super().__init__(name="DataEngine", daemon=True)
        self.symbols = symbols
        self.mt5_connector = mt5_connector
        self.market_analyzer = market_analyzer
        self.update_interval = update_interval_seconds
        self.stop_event = stop_event or threading.Event()

        self.logger = logger
        self.cycle_count = 0

        self.logger.info(
            f"🔧 [DATA_ENGINE] Initialisé | symbols={symbols} | interval={self.update_interval}s"
        )

    def run(self):
        """
        Boucle principale du thread DataEngine.

        Toutes les `update_interval_seconds`:
        1. Pour chaque symbole prioritaire:
           - Récupère les ticks de la bougie M1 en cours
           - Analyse le footprint
           - Met à jour le cache
        2. Sleep jusqu'au prochain cycle
        """
        self.logger.info("🚀 [DATA_ENGINE] Thread démarré")

        while not self.stop_event.is_set():
            cycle_start = time.time()
            self.cycle_count += 1

            try:
                # Analyser chaque symbole
                for symbol in self.symbols:
                    if self.stop_event.is_set():
                        break

                    try:
                        self._update_footprint_for_symbol(symbol)
                    except Exception as e:
                        self.logger.error(
                            f"❌ [DATA_ENGINE] Erreur analyse {symbol}: {e}",
                            exc_info=True
                        )

                # Statistiques de cycle
                cycle_duration = time.time() - cycle_start
                self.logger.debug(
                    f"⏱️ [DATA_ENGINE] Cycle #{self.cycle_count} terminé | "
                    f"duration={cycle_duration:.3f}s | symbols={len(self.symbols)}"
                )

                # Sleep jusqu'au prochain cycle
                sleep_time = max(0, self.update_interval - cycle_duration)
                if sleep_time > 0:
                    self.stop_event.wait(timeout=sleep_time)

            except Exception as e:
                self.logger.error(
                    f"❌ [DATA_ENGINE] Erreur critique cycle #{self.cycle_count}: {e}",
                    exc_info=True
                )
                # Continue malgré l'erreur pour ne pas crasher le thread
                self.stop_event.wait(timeout=self.update_interval)

        self.logger.info("🛑 [DATA_ENGINE] Thread arrêté proprement")

    def _update_footprint_for_symbol(self, symbol: str) -> None:
        """
        Analyse le footprint pour un symbole et met à jour le cache.

        Args:
            symbol: Symbole à analyser (ex: XAUUSD)
        """
        analysis_start = time.time()

        try:
            # ✅ PHASE 2: Utiliser cache barres (20 barres au lieu de 50)
            # 95% du temps: récupère 1 barre seulement (bougie courante)
            # Recharge complète toutes les 60s seulement
            rates_df = bars_cache.get_or_fetch(
                symbol=symbol,
                timeframe="M1",
                count=20,  # ✅ RÉDUIT: 50→20 (suffisant pour Volume MA 14)
                mt5_connector=self.mt5_connector,
                ttl_seconds=60.0,
            )
            if rates_df is None or (hasattr(rates_df, 'empty') and rates_df.empty):
                self.logger.debug(
                    f"⚠️ [DATA_ENGINE][{symbol}] Barres M1 indisponibles (marché fermé?)"
                )
                return

            # 2. Récupérer les ticks de la bougie M1 en cours
            # (depuis le début de la bougie jusqu'à maintenant)
            ticks_data = self._get_current_m1_ticks(symbol)

            # Vérifier si les ticks sont valides (DataFrame pandas)
            if ticks_data is None or (hasattr(ticks_data, 'empty') and ticks_data.empty):
                self.logger.debug(
                    f"⚠️ [DATA_ENGINE][{symbol}] Aucun tick disponible (marché fermé?)"
                )
                return

            # 3. Analyser le footprint avec MarketAnalyzer
            footprint_result = self._analyze_footprint(symbol, rates_df, ticks_data)

            if not footprint_result:
                self.logger.debug(
                    f"⚠️ [DATA_ENGINE][{symbol}] Analyse footprint retournée vide"
                )
                return

            # 3. Calculer le temps d'analyse
            analysis_duration_ms = (time.time() - analysis_start) * 1000

            # 4. Enrichir les données avec timing
            footprint_result['analysis_time_ms'] = analysis_duration_ms
            footprint_result['cache_timestamp'] = time.time()

            # 5. Mettre à jour le cache
            footprint_cache.update(symbol, footprint_result)

            # 6. Log succès
            ticks = footprint_result.get('footprint_summary', {}).get('tick_count', 0)
            coverage = footprint_result.get('footprint_summary', {}).get('coverage_s', 0)

            self.logger.info(
                f"✅ [DATA_ENGINE][{symbol}] Footprint mis à jour | "
                f"ticks={ticks} | coverage={coverage:.1f}s | "
                f"analysis={analysis_duration_ms:.1f}ms"
            )

        except Exception as e:
            self.logger.error(
                f"❌ [DATA_ENGINE][{symbol}] Erreur analyse footprint: {e}",
                exc_info=True
            )

    def _get_current_m1_ticks(self, symbol: str) -> Optional[Any]:
        """
        Récupère les ticks de la bougie M1 en cours.

        Args:
            symbol: Symbole

        Returns:
            DataFrame des ticks, ou None si erreur
        """
        try:
            # Récupérer les ticks depuis le début de la bougie M1 actuelle
            # jusqu'à maintenant (bougie incomplète mais fraîche)
            from datetime import datetime, timezone, timedelta

            # Arrondir à la minute en cours
            now = datetime.now(timezone.utc)
            candle_start = now.replace(second=0, microsecond=0)
            candle_end = now

            # Appel MT5 pour récupérer les ticks de la bougie M1 en cours
            # Note: get_ticks_for_candle() attend normalement une bougie complète (60s)
            # mais fonctionne aussi pour une bougie en cours
            ticks_df = self.mt5_connector.get_ticks_for_candle(
                symbol=symbol,
                start_ts=candle_start,
                end_ts=candle_end
            )

            # Retourner le DataFrame (ou None si vide)
            if ticks_df is None or len(ticks_df) == 0:
                return None

            return ticks_df

        except Exception as e:
            self.logger.error(
                f"❌ [DATA_ENGINE][{symbol}] Erreur récupération ticks: {e}",
                exc_info=True
            )
            return None

    def _analyze_footprint(
        self,
        symbol: str,
        rates_df: Any,
        ticks_data: Any
    ) -> Optional[Dict[str, Any]]:
        """
        Analyse le footprint depuis les barres M1 et les ticks.

        IMPORTANT: Cette méthode réutilise EXACTEMENT la même logique
        que le thread SCALPING actuel pour garantir la cohérence.

        Args:
            symbol: Symbole
            rates_df: DataFrame des barres M1 OHLC
            ticks_data: DataFrame des ticks

        Returns:
            Dict contenant:
            - footprint_summary: Résumé (tick_count, coverage_s, buy_pct, etc.)
            - trigger_data: Données du trigger détecté (si présent)
            - footprint_df: DataFrame du footprint (optionnel)
        """
        try:
            import pandas as pd

            # Vérifier que les DataFrames sont valides
            if not isinstance(rates_df, pd.DataFrame) or rates_df.empty:
                self.logger.warning(f"[DATA_ENGINE][{symbol}] rates_df invalide")
                return None

            if not isinstance(ticks_data, pd.DataFrame) or ticks_data.empty:
                self.logger.warning(f"[DATA_ENGINE][{symbol}] ticks_data invalide")
                return None

            # Appeler la méthode analyze() du MarketAnalyzer
            # AVEC les barres M1 (df) ET les ticks
            result = self.market_analyzer.analyze(
                df=rates_df,  # ✅ Barres M1 nécessaires
                asset=symbol,
                ticks=ticks_data  # Ticks pour analyse footprint
            )

            # ✅ FIX (12 Dec 2025): Extraire footprint_summary depuis annotated_df (pas latest)
            # Raison: latest est une COPY (pandas Series), les modifications par MarketAnalyzer
            # ne persist pas. Il faut lire depuis le DataFrame annoté directement.

            self.logger.info(f"🔍 [DEBUG_DATA_ENGINE][{symbol}] result keys: {list(result.keys())}")

            annotated_df = result.get('annotated_df')
            if annotated_df is None or (hasattr(annotated_df, 'empty') and annotated_df.empty):
                self.logger.warning(f"⚠️ [DATA_ENGINE][{symbol}] annotated_df manquant ou vide")
                return None

            self.logger.info(f"🔍 [DEBUG_DATA_ENGINE][{symbol}] annotated_df shape: {annotated_df.shape}")
            self.logger.info(f"🔍 [DEBUG_DATA_ENGINE][{symbol}] 'footprint_summary' in columns: {'footprint_summary' in annotated_df.columns}")

            if 'footprint_summary' not in annotated_df.columns:
                self.logger.warning(f"⚠️ [DATA_ENGINE][{symbol}] Colonne footprint_summary absente")
                self.logger.info(f"🔍 [DEBUG_DATA_ENGINE][{symbol}] Colonnes disponibles: {list(annotated_df.columns)}")
                return None

            # Récupérer footprint_summary de la dernière ligne
            fp_sum_raw = annotated_df.iloc[-1]['footprint_summary']

            self.logger.info(f"🔍 [DEBUG_DATA_ENGINE][{symbol}] fp_sum_raw type: {type(fp_sum_raw)}")
            if isinstance(fp_sum_raw, str):
                self.logger.info(f"🔍 [DEBUG_DATA_ENGINE][{symbol}] fp_sum_raw (string) preview: {fp_sum_raw[:200] if len(fp_sum_raw) > 200 else fp_sum_raw}")
            elif isinstance(fp_sum_raw, dict):
                self.logger.info(f"🔍 [DEBUG_DATA_ENGINE][{symbol}] fp_sum_raw (dict) keys: {list(fp_sum_raw.keys())}")

            # Parser JSON string si nécessaire (orchestrator.py stocke en JSON string ligne 1154)
            if isinstance(fp_sum_raw, str):
                try:
                    import json
                    footprint_summary = json.loads(fp_sum_raw)
                    self.logger.info(f"✅ [DEBUG_DATA_ENGINE][{symbol}] JSON parsé - keys: {list(footprint_summary.keys())}")
                except Exception as e:
                    self.logger.error(f"❌ [DATA_ENGINE][{symbol}] Échec parsing JSON footprint_summary: {e}")
                    footprint_summary = {}
            elif isinstance(fp_sum_raw, dict):
                footprint_summary = fp_sum_raw
                self.logger.info(f"✅ [DEBUG_DATA_ENGINE][{symbol}] Dict direct - keys: {list(footprint_summary.keys())}")
            else:
                footprint_summary = {}
                self.logger.warning(f"⚠️ [DEBUG_DATA_ENGINE][{symbol}] Type inattendu: {type(fp_sum_raw)}")

            # Vérifier les clés critiques
            critical_keys = ['buy_volume', 'sell_volume', 'delta_total', 'poc']
            missing_keys = [k for k in critical_keys if k not in footprint_summary]
            if missing_keys:
                self.logger.warning(f"⚠️ [DEBUG_DATA_ENGINE][{symbol}] Clés manquantes: {missing_keys}")
            else:
                self.logger.info(f"✅ [DEBUG_DATA_ENGINE][{symbol}] Toutes les clés critiques présentes!")

            if not footprint_summary:
                self.logger.warning(f"⚠️ [DATA_ENGINE][{symbol}] footprint_summary vide ou manquant")
                return None

            return {
                'footprint_summary': footprint_summary,
                'trigger_data': result.get('footprint_trigger', {}),
                'footprint_df': result.get('footprint_df'),
                'raw_result': result  # Garder tout au cas où
            }

        except Exception as e:
            self.logger.error(
                f"❌ [DATA_ENGINE][{symbol}] Erreur analyse footprint: {e}",
                exc_info=True
            )
            return None

    def stop(self):
        """Arrête proprement le thread DataEngine."""
        self.logger.info("🛑 [DATA_ENGINE] Arrêt demandé...")
        self.stop_event.set()
