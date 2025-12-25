# phase_observer/market_analyzer.py
"""
🎯 MARKET ANALYZER MINIMALISTE - OrderFlow V6 uniquement

Architecture simplifiée (25 Décembre 2025) :
- PhaseObserver : Annotation des régimes (conservé)
- OrderFlow V6 : Source unique de signaux
- PAS de Fusion, VWAP, Footprint, Momentum

Décision directe basée sur OrderFlow score >= 75
"""
from __future__ import annotations

import logging
from typing import Dict, Any, Optional

import pandas as pd

from .orchestrator import PhaseObserver


LOG = logging.getLogger(__name__)


class MarketAnalyzer:
    """
    Analyse simplifiée du marché :
      - PhaseObserver (annotation régimes)
      - Décision directe depuis OrderFlow V6 (sans fusion)
    """

    def __init__(self, config_manager=None, logger=None):
        self.logger = logger or LOG
        self.config_manager = config_manager

        # PhaseObserver uniquement
        try:
            self.phase_observer = PhaseObserver(config_manager=config_manager)
        except Exception as e:
            self.logger.error(f"[MarketAnalyzer] PhaseObserver init failed: {e}")
            self.phase_observer = None

        self._last_results: Dict[str, Any] = {}

    # ============================================================
    # 🎯 DÉCISION DIRECTE - OrderFlow V6 seul (25 DEC 2025)
    # ============================================================
    def build_decision(
        self,
        orderflow_result: Dict[str, Any],
        min_score: float = 75.0
    ) -> Dict[str, Any]:
        """
        🎯 Décision directe basée sur OrderFlow V6 uniquement

        Args:
            orderflow_result: Résultat de OrderFlowV6.analyze()
                {
                    "score": 0-100,
                    "bias": "BUY"|"SELL"|"NEUTRAL",
                    "summary": {"vpoc_price": float, ...},
                    ...
                }
            min_score: Seuil minimum pour trader (défaut 75)

        Returns:
            {
                "action": "BUY" | "SELL" | "HOLD",
                "confidence": float (0-1),
                "anchor_price": float | None,
                "rationale": str,
                "orderflow_score": float
            }
        """
        score = orderflow_result.get("score", 0)
        bias = orderflow_result.get("bias", "NEUTRAL")
        summary = orderflow_result.get("summary", {})

        # Anchor price depuis VPOC
        anchor_price = summary.get("vpoc_price")

        # Décision simple
        if score >= min_score and bias in ["BUY", "SELL"]:
            return {
                "action": bias,
                "confidence": score / 100.0,
                "anchor_price": anchor_price,
                "rationale": f"OrderFlow {bias} score={score:.1f}/100",
                "orderflow_score": score
            }
        else:
            return {
                "action": "HOLD",
                "confidence": 0.0,
                "anchor_price": None,
                "rationale": f"OrderFlow insuffisant (score={score:.1f}, bias={bias}, seuil={min_score})",
                "orderflow_score": score
            }

    # ============================================================
    # 📊 ANALYSE PRINCIPALE - PhaseObserver + PassThrough
    # ============================================================
    def analyze(
        self,
        asset: str,
        df: pd.DataFrame,
        ticks: Optional[pd.DataFrame] = None,
        *,
        current_price: Optional[float] = None,
        **_
    ) -> Dict[str, Any]:
        """
        Analyse du marché (version simplifiée)

        Args:
            asset: Symbole (USDJPY, etc.)
            df: DataFrame OHLC
            ticks: Ticks (non utilisé ici, OrderFlow les reçoit directement)
            current_price: Prix actuel

        Returns:
            {
                "asset": str,
                "annotated_df": pd.DataFrame avec régimes PhaseObserver,
                "latest": dict de la dernière bougie annotée,
                "patterns": {}  # Vide, patterns supprimés
            }
        """
        if df is None or df.empty:
            return {
                "asset": asset,
                "annotated_df": pd.DataFrame(),
                "latest": {},
                "patterns": {}
            }

        # PhaseObserver annotation
        annotated_df = df.copy()
        if self.phase_observer:
            try:
                annotated_df = self.phase_observer.analyze(annotated_df)
            except Exception as e:
                self.logger.error(f"[MarketAnalyzer] PhaseObserver.analyze() error: {e}")

        # Latest candle
        latest = {}
        if not annotated_df.empty:
            try:
                latest = annotated_df.iloc[-1].to_dict()
            except Exception as e:
                self.logger.warning(f"[MarketAnalyzer] latest extraction error: {e}")

        return {
            "asset": asset,
            "annotated_df": annotated_df,
            "latest": latest,
            "patterns": {}  # Patterns supprimés - OrderFlow géré en externe
        }
