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

    # 16 FEV 2026: build_decision() SUPPRIMEE
    # Decision integree dans decision_pipeline.decide_scalp_action()
    # Scoring centralise dans advanced_scoring.calculate_final_score()

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

        # PhaseObserver annotation (01 JAN 2026: FIX - Passer asset_symbol)
        annotated_df = df.copy()
        if self.phase_observer:
            try:
                annotated_df = self.phase_observer.analyze(annotated_df, asset_symbol=asset)
            except Exception as e:
                self.logger.error(f"[MarketAnalyzer] PhaseObserver.analyze() error: {e}")

        # Latest candle
        # 🔧 FIX (05 JAN 2026): Utiliser iloc[-2] (bougie fermée) pour cohérence avec ScalpingStrategy
        # ScalpingStrategy analyse iloc[-2] pour avoir ticks complets → Régime doit matcher !
        latest = {}
        if not annotated_df.empty:
            try:
                # Utiliser avant-dernière bougie (fermée) pour synchronisation avec OrderFlow
                latest = annotated_df.iloc[-2].to_dict()
                self.logger.debug(
                    f"[MarketAnalyzer] Régime basé sur bougie FERMÉE (iloc[-2]) pour sync avec OrderFlow"
                )
            except Exception as e:
                self.logger.warning(f"[MarketAnalyzer] latest extraction error: {e}")
                # Fallback sur dernière si problème
                if len(annotated_df) >= 1:
                    latest = annotated_df.iloc[-1].to_dict()

        return {
            "asset": asset,
            "annotated_df": annotated_df,
            "latest": latest,
            "patterns": {}  # Patterns supprimés - OrderFlow géré en externe
        }
