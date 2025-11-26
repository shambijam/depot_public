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
        update_interval_seconds: float = 5.0,
        stop_event: Optional[threading.Event] = None
    ):
        """
        Initialise le DataEngine.

        Args:
            symbols: Liste des symboles à analyser (ex: ['XAUUSD', 'EURUSD'])
            mt5_connector: Instance du connecteur MT5 pour récupérer les ticks
            market_analyzer: Instance du MarketAnalyzer pour analyser footprint
            update_interval_seconds: Intervalle entre chaque cycle (défaut: 5s)
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
            # 1. Récupérer les ticks de la bougie M1 en cours
            # (depuis le début de la bougie jusqu'à maintenant)
            ticks_data = self._get_current_m1_ticks(symbol)

            # Vérifier si les données sont valides (DataFrame pandas)
            if ticks_data is None or (hasattr(ticks_data, 'empty') and ticks_data.empty):
                self.logger.debug(
                    f"⚠️ [DATA_ENGINE][{symbol}] Aucun tick disponible (marché fermé?)"
                )
                return

            # 2. Analyser le footprint avec MarketAnalyzer
            footprint_result = self._analyze_footprint(symbol, ticks_data)

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
        ticks_data: List[Any]
    ) -> Optional[Dict[str, Any]]:
        """
        Analyse le footprint depuis les ticks.

        IMPORTANT: Cette méthode réutilise EXACTEMENT la même logique
        que le thread SCALPING actuel pour garantir la cohérence.

        Args:
            symbol: Symbole
            ticks_data: Liste des ticks

        Returns:
            Dict contenant:
            - footprint_summary: Résumé (tick_count, coverage_s, buy_pct, etc.)
            - trigger_data: Données du trigger détecté (si présent)
            - footprint_df: DataFrame du footprint (optionnel)
        """
        try:
            # Convertir les ticks en DataFrame pandas si besoin
            import pandas as pd
            if not isinstance(ticks_data, pd.DataFrame):
                # Convertir la liste de ticks MT5 en DataFrame
                if len(ticks_data) == 0:
                    return None

                ticks_df = pd.DataFrame(ticks_data)
            else:
                ticks_df = ticks_data

            # Appeler la méthode analyze() du MarketAnalyzer
            # avec les ticks pour qu'il fasse l'analyse footprint
            result = self.market_analyzer.analyze(
                df=None,  # Pas besoin de barres OHLC pour footprint
                asset=symbol,
                ticks=ticks_df
            )

            # Le résultat contient déjà footprint_summary enrichi
            # par le MarketAnalyzer (voir market_analyzer.py ligne ~300)
            if 'footprint_summary' not in result:
                self.logger.warning(
                    f"⚠️ [DATA_ENGINE][{symbol}] footprint_summary manquant dans résultat"
                )
                return None

            return {
                'footprint_summary': result.get('footprint_summary', {}),
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
