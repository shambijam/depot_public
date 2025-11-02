# phase_observer/market_analyzer.py
import logging
import pandas as pd
from typing import Dict, Any, Tuple, Optional

from .orchestrator import PhaseObserver
from .detectors import (
    detect_single_candle,
    detect_multi_candle_patterns,
    detect_combos,
)
from .detect_orderflow_v6.orderflow_v6 import detect_orderflow_v6
from .footprint_analyzer import FootprintAnalyzer
from .fusion_manager import FusionManager


LOG = logging.getLogger(__name__)


class MarketAnalyzer:
    def __init__(self, config_manager=None, logger=None):
        self.logger = logger or LOG
        self.config_manager = config_manager
        self.phase_observer = PhaseObserver(config_manager=config_manager)
        self._last_results: Dict[str, Any] = {}
        self._confluence_cache: Dict[str, pd.DataFrame] = {}
        self.footprint = FootprintAnalyzer(logger=self.logger)
        self.fusion_manager = FusionManager()


    # === Pass-through pour l'analyse des triggers footprint ===
    def analyze_footprint_triggers(
        self,
        asset: str,
        ticks: pd.DataFrame,
        bars: Optional[pd.DataFrame],
        strategy_config: Dict[str, Any],
    ) -> Tuple[bool, Dict[str, Any]]:
        return self.footprint.analyze_footprint_triggers(asset, ticks, bars, strategy_config)
    
    # ============================================================
    # 🔹 FUSION MANAGER — décision unifiée (OFv6 + FP M1 + Trigger)
    # ============================================================
    def build_fused_decision(
        self,
        asset: str,
        strategy_config: Dict[str, Any],
        footprint_trigger: Optional[Dict[str, Any]],
        market_results: Dict[str, Any],
        *,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        of = (market_results or {}).get("patterns", {}).get("orderflow") or {}
        latest = (market_results or {}).get("latest")
        
        # Compat v6 → expose bias / poc au top-level pour la Fusion
        try:
            _summ = (of or {}).get("summary") or {}
            if _summ:
                if "bias" in _summ and "bias" not in of:
                    of["bias"] = _summ.get("bias")
                if ("poc" not in of) and (_summ.get("vpoc_price") is not None):
                    of["poc"] = _summ.get("vpoc_price")
        except Exception:
            pass


        # Compacter le Footprint M1 depuis latest.*
        fp_payload = {"status": "SUSPECT", "summary": {}}
        if latest is not None:
            summ = latest.get("footprint_summary")
            # tolère str(dict)
            if isinstance(summ, str):
                try:
                    import ast
                    summ = ast.literal_eval(summ)
                except Exception:
                    summ = {}
            fp_payload = {
                "status": str(latest.get("footprint_status") or "SUSPECT"),
                "score": float(latest.get("footprint_score") or 0.0),
                "summary": summ or {},
            }

        trig = footprint_trigger or {}

        # On transmet aussi un contexte optionnel (spread/session/régime/horodatage…)
        ctx = dict(context or {})
        ctx.setdefault("now_ts", None)  # si absent, FusionManager utilisera time.time()

        # Run fusion
        fused = self.fusion_manager.fuse(orderflow=of, footprint=fp_payload, triggers=trig, strategy_config=strategy_config, context=ctx)

        # Si pas d'ancre côté trigger, la brique utilisera le POC footprint: on harmonise ici
        if fused.get("anchor_price") is None:
            poc = fp_payload.get("summary", {}).get("poc")
            if poc is not None:
                try:
                    fused["anchor_price"] = float(poc)
                except Exception:
                    pass
        return fused


        
    # ============================================================
    # 🔹 Analyse unifiée
    # ============================================================
    
    
    def analyze(self, df: pd.DataFrame, asset: str = "") -> Dict[str, Any]:
        if df is None or df.empty:
            return {"annotated_df": pd.DataFrame(), "latest": None, "patterns": {}}

        # 1️⃣ PhaseObserver (annotate le DF)
        annotated_df = self.phase_observer.analyze(df.copy(), asset_symbol=asset)
        if annotated_df is None or annotated_df.empty:
            return {"annotated_df": pd.DataFrame(), "latest": None, "patterns": {}}

        # 2️⃣ Détecteurs factuels
        candles = [
            detect_single_candle(annotated_df, i) for i in range(len(annotated_df))
        ]
        multi_patterns = detect_multi_candle_patterns(annotated_df)
        combo_patterns = detect_combos(annotated_df)
        orderflow_signals = detect_orderflow_v6(annotated_df)

        # 3️⃣ Dernier point brut (Series Pandas)
        latest = annotated_df.iloc[-1]  # ⚠️ garde la Series → pas de .to_dict()

        # 4️⃣ Scoring qualité
        quality_score, quality_diag = self._compute_quality_metrics(
            annotated_df, latest
        )

        # 5️⃣ Confluence MTF (si dispo dans cache)
        confluence = self._compute_confluence()

        # 6️⃣ Package institutionnel
        results = {
            "annotated_df": annotated_df,
            "latest": latest,  # Series → sera converti plus tard
            "patterns": {
                "candles": candles,
                "multi": multi_patterns,
                "combos": combo_patterns,
                "orderflow": orderflow_signals,
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
    # 🔹 Métriques de qualité
    # ============================================================
    def _compute_quality_metrics(
        self, df: pd.DataFrame, latest
    ) -> Tuple[float, Dict[str, Any]]:
        """
        Exemple simple: qualité = nombre de barres valides, présence des colonnes essentielles.
        """
        diag = {}
        if df is None or df.empty:
            return 0.0, {"reason": "empty_df"}

        try:
            n_bars = len(df)
            has_volume = "tick_volume" in df.columns
            has_time = "time" in df.columns
            score = 0.5
            if n_bars >= 200:
                score += 0.3
            if has_volume:
                score += 0.1
            if has_time:
                score += 0.1
            diag = {
                "n_bars": n_bars,
                "has_volume": has_volume,
                "has_time": has_time,
            }
            return min(1.0, score), diag
        except Exception as e:
            return 0.0, {"error": str(e)}

    # ============================================================
    # 🔹 Confluence
    # ============================================================
    def _compute_confluence(self) -> Dict[str, Any]:
        """
        Retourne une mesure simple de confluence à partir du cache interne.
        """
        try:
            bullish = 0
            bearish = 0
            for tf, df in self._confluence_cache.items():
                if df is None or df.empty:
                    continue
                last = df.iloc[-1]
                if last.get("phase") == "bullish":
                    bullish += 1
                elif last.get("phase") == "bearish":
                    bearish += 1
            return {"bullish": bullish, "bearish": bearish}
        except Exception as e:
            return {"error": str(e)}

    # ============================================================
    # 🔹 Ready & confluence check
    # ============================================================
  
    def ready_and_confluence_ok(self, confluence_required: int = 2) -> Tuple[bool, str]:
        try:
            bullish_count = 0
            bearish_count = 0
            for tf, df in self._confluence_cache.items():
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
