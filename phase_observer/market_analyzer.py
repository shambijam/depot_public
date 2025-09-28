import logging
import pandas as pd
from typing import Dict, Any, List, Optional, Tuple, TYPE_CHECKING
from .orchestrator import PhaseObserver
from .detectors import Detectors   # classe regroupant les détecteurs

LOG = logging.getLogger(__name__)

class MarketAnalyzer:
    def __init__(self, config_manager=None, logger=None):
        self.logger = logger or LOG
        self.config_manager = config_manager
        self.phase_observer = PhaseObserver(config_manager=config_manager, logger=logger)
        self.detectors = Detectors(config_manager=config_manager, logger=logger)

    def analyze(self, df: pd.DataFrame, asset: str = "") -> Dict[str, Any]:
        """
        Analyse unifiée marché :
        - Phases (PhaseObserver)
        - Patterns chandeliers / multi / combos
        - Orderflow
        Retourne un dict standardisé
        """
        if df is None or df.empty:
            return {"annotated_df": pd.DataFrame(), "latest": {}, "patterns": {}}

        # 1️⃣ PhaseObserver → annotate DF
        annotated_df = self.phase_observer.analyze(df.copy(), asset_symbol=asset)

        # 2️⃣ Détecteurs (via la classe Detectors)
        candle_patterns = [
            self.detectors.detect_single_candle(annotated_df, i)
            for i in range(len(annotated_df))
        ]
        multi_patterns = self.detectors.detect_multi_candle_patterns(annotated_df)
        combo_patterns = self.detectors.detect_combos(annotated_df)
        orderflow_signals = self.detectors.detect_orderflow(annotated_df)

        # 3️⃣ Dernière ligne (résumé latest)
        latest = annotated_df.iloc[-1].to_dict()

        return {
            "annotated_df": annotated_df,
            "latest": latest,
            "patterns": {
                "candles": candle_patterns,
                "multi": multi_patterns,
                "combos": combo_patterns,
                "orderflow": orderflow_signals,
            },
            "phase": latest.get("phase"),
            "confidence": latest.get("confidence_score", 0.5),
        }

    def ready_and_confluence_ok(self, confluence_required: int = 2) -> Tuple[bool, str]:
        """
        Vérifie si assez de timeframes donnent la même direction (confluence).
        """
        try:
            # Exemple: simple placeholder
            bullish_count = 0
            bearish_count = 0
            for tf in ["M1", "M5", "M15"]:
                df = self.phase_observer.cached_dfs.get(tf)  # si tu as un cache
                if df is None or df.empty:
                    continue
                last = df.iloc[-1]
                if last.get("phase") == "bullish":
                    bullish_count += 1
                elif last.get("phase") == "bearish":
                    bearish_count += 1

            if bullish_count >= confluence_required:
                return True, "bullish confluence ok"
            if bearish_count >= confluence_required:
                return True, "bearish confluence ok"

            return False, "pas assez de confluence"
        except Exception as e:
            return True, f"skip check (erreur: {e})"
