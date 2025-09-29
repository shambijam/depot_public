import logging
import pandas as pd
import numpy as np
from typing import Dict, Any, List, Optional, Tuple
from .orchestrator import PhaseObserver
from .detectors import Detectors   # classe regroupant les détecteurs
from .detectors import (
        detect_single_candle,
        detect_multi_candle_patterns,
        detect_combos,
        detect_orderflow,
    )

LOG = logging.getLogger(__name__)

class MarketAnalyzer:
    """
    🏦 MarketAnalyzer (Desk Banque Privée)
    ------------------------------------------------
    Analyse unifiée du marché, prête pour le pipeline institutionnel :
    - Détection de phase via PhaseObserver
    - Détection chandeliers, patterns multi, combos
    - Lecture orderflow (delta/imbalance/absorptions)
    - Confluence multi-timeframe
    - Scoring de qualité (volatilité, spread, volume, clarté)
    """

    def __init__(self, config_manager=None, logger=None):
        self.logger = logger or LOG
        self.config_manager = config_manager
        self.phase_observer = PhaseObserver(config_manager=config_manager)
        self.detectors = Detectors(config_manager=config_manager, logger=self.logger)

        # cache interne
        self._last_results: Dict[str, Any] = {}
        self._confluence_cache: Dict[str, pd.DataFrame] = {}

    # ============================================================
    # 🔹 Analyse unifiée
    # ============================================================
    def analyze(self, df: pd.DataFrame, asset: str = "") -> Dict[str, Any]:
        if df is None or df.empty:
            return {"annotated_df": pd.DataFrame(), "latest": {}, "patterns": {}}

        # 1️⃣ PhaseObserver (annotate le DF)
        annotated_df = self.phase_observer.analyze(df.copy(), asset_symbol=asset)
        if annotated_df is None or annotated_df.empty:
            return {"annotated_df": pd.DataFrame(), "latest": {}, "patterns": {}}

        # 2️⃣ Détecteurs factuels
  

        candles = [detect_single_candle(annotated_df, i) for i in range(len(annotated_df))]
        multi_patterns = detect_multi_candle_patterns(annotated_df)
        combo_patterns = detect_combos(annotated_df)
        orderflow_signals = detect_orderflow(annotated_df)


        # 3️⃣ Résumé dernier point
        latest = annotated_df.iloc[-1].to_dict()

        # 4️⃣ Scoring qualité
        quality_score, quality_diag = self._compute_quality_metrics(annotated_df, latest)

        # 5️⃣ Confluence MTF (si dispo dans cache)
        confluence = self._compute_confluence()

        # 6️⃣ Package institutionnel
        results = {
            "annotated_df": annotated_df,
            "latest": latest,
            "patterns": {
                "candles": candles,
                "multi": multi,
                "combos": combos,
                "orderflow": orderflow,
            },
            "phase": latest.get("phase"),
            "confidence": latest.get("confidence_score", 0.5),
            "quality_metrics": quality_diag,
            "quality_score": quality_score,
            "confluence": confluence,
        }

        self._last_results[asset] = results
        return results

    # ============================================================
    # 🔹 Confluence multi-timeframe
    # ============================================================
    def ready_and_confluence_ok(self, confluence_required: int = 2) -> Tuple[bool, str]:
        try:
            bullish, bearish = 0, 0
            for tf, df in self._confluence_cache.items():
                if df is None or df.empty:
                    continue
                last = df.iloc[-1]
                if str(last.get("phase", "")).lower().startswith("bull"):
                    bullish += 1
                elif str(last.get("phase", "")).lower().startswith("bear"):
                    bearish += 1

            if bullish >= confluence_required:
                return True, f"bullish confluence ({bullish}/{confluence_required})"
            if bearish >= confluence_required:
                return True, f"bearish confluence ({bearish}/{confluence_required})"

            return False, "pas assez de confluence"
        except Exception as e:
            return True, f"skip check (erreur: {e})"

    # ============================================================
    # 🔹 Scoring qualité institutionnel
    # ============================================================
    def _compute_quality_metrics(self, df: pd.DataFrame, latest: Dict[str, Any]) -> Tuple[float, Dict[str, Any]]:
        try:
            if df is None or df.empty:
                return 0.0, {}

            # volatilité (écart-type %)
            vol = df["close"].pct_change().std() * 100
            # spread relatif (si dispo)
            spread = float(latest.get("spread", 0.0))
            price = float(latest.get("close", 1.0))
            spread_bps = (spread / price) * 10000 if price else 0.0
            # volume relatif
            vol_ratio = float(latest.get("volume_ratio", 1.0))

            score = 0.5
            if vol > 0.2:
                score += 0.2
            if spread_bps < 2.0:  # bon marché
                score += 0.2
            if vol_ratio > 1.2:  # volume élevé
                score += 0.1

            diag = {
                "volatility_pct": round(vol, 3),
                "spread_bps": round(spread_bps, 2),
                "volume_ratio": round(vol_ratio, 2),
            }
            return min(1.0, score), diag
        except Exception as e:
            return 0.5, {"error": str(e)}

    # ============================================================
    # 🔹 Gestion cache MTF
    # ============================================================
    def update_confluence_cache(self, tf: str, df: pd.DataFrame):
        """Met à jour le cache multi-timeframe pour la confluence."""
        if isinstance(df, pd.DataFrame) and not df.empty:
            self._confluence_cache[tf.upper()] = df.tail(200)
