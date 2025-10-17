import logging
import pandas as pd
from typing import Dict, Any, Tuple
from .orchestrator import PhaseObserver
from .detectors import (
    detect_single_candle,
    detect_multi_candle_patterns,
    detect_combos,
    detect_orderflow_v5,
    detect_imbalance_stacking,                 
    detect_absorption_reject,                  
    detect_volume_climax_after_consolidation,  
)

LOG = logging.getLogger(__name__)


class MarketAnalyzer:
    def __init__(self, config_manager=None, logger=None):
        self.logger = logger or LOG
        self.config_manager = config_manager
        self.phase_observer = PhaseObserver(config_manager=config_manager)
        self._last_results: Dict[str, Any] = {}
        self._confluence_cache: Dict[str, pd.DataFrame] = {}

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
        candles = [detect_single_candle(annotated_df, i) for i in range(len(annotated_df))]
        multi_patterns = detect_multi_candle_patterns(annotated_df)
        combo_patterns = detect_combos(annotated_df)
        orderflow_signals = detect_orderflow_v5(annotated_df)

        # 3️⃣ Dernier point brut (Series Pandas)
        latest = annotated_df.iloc[-1]  # ⚠️ garde la Series → pas de .to_dict()

        # 4️⃣ Scoring qualité
        quality_score, quality_diag = self._compute_quality_metrics(annotated_df, latest)

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
    def _compute_quality_metrics(self, df: pd.DataFrame, latest) -> Tuple[float, Dict[str, Any]]:
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
    def analyze_footprint_triggers(
        self,
        asset: str,
        ticks: pd.DataFrame,
        bars: pd.DataFrame,
        strategy_config: Dict[str, Any],
    ) -> Tuple[bool, Dict[str, Any]]:
        """
        Renvoie (ok, decision_dict) pour scalping burst:
         - triggers: Climax après consolidation, Stacking, Absorption+Rejet
         - filtres: spread/tick-rate/vol minimal
         - entrée: LIMIT+FOK (pas de fallback)
         - sortie: trailing only (Phase 0→1→2), fermeture panier unique
        """
        try:
            cfg = getattr(self, "footprint_triggers", None)
            if cfg is None:
                # si non chargé via config.py → fallback: lire depuis strategy_config
                cfg = strategy_config.get("footprint_triggers", {"enabled": False})
            if not cfg.get("enabled", False):
                return False, {"reason": "footprint_triggers disabled"}

            # 1) Snapshot footprint sur ticks récents
            price_step = float(strategy_config.get("price_step", 0.1))
            from .features import _compute_footprint_snapshot, _micro_atr_from_ticks
            df_levels, meta = _compute_footprint_snapshot(
                ticks, price_step=price_step, window_s=5
            )

            # 2) Filtres d’hygiène
            filters = cfg["filters"]
            if meta["spread"] > float(filters["spread_max_pts"]):
                return False, {"reason": f"spread too wide: {meta['spread']}"}
            if meta["tick_rate"] < float(filters["tickrate_min_per5s"]):
                return False, {"reason": f"tickrate too low: {meta['tick_rate']}"}
            if df_levels.empty:
                return False, {"reason": "no footprint levels"}

            # 3) Ordre de priorité des triggers
            #    1) Climax après consolidation (gros bursts)
            #    2) Imbalance Stacking
            #    3) Absorption + Rejet
            params = cfg
            decision = None

            d_climax = detect_volume_climax_after_consolidation(
                bars, df_levels,
                lookback_bars=int(params["climax"]["lookback_bars"]),
                vol_ratio_min=float(params["climax"]["vol_ratio_min"]),
                delta_ratio_min=float(params["climax"]["delta_ratio_min"]),
                need_consolidation=bool(params["climax"]["need_consolidation"]),
                consolidation_max_atr_mult=float(params["climax"]["consolidation_max_atr_mult"]),
            )
            if d_climax.get("ok"):
                decision = d_climax

            if decision is None:
                d_stack = detect_imbalance_stacking(
                    df_levels,
                    delta_ratio_min=float(params["stacking"]["delta_ratio_min"]),
                    min_levels=int(params["stacking"]["min_levels"]),
                    invalidate_opposite_ratio=float(params["stacking"]["invalidate_opposite_ratio"]),
                    vol_level_min_ratio_median_30s=float(filters["vol_level_min_ratio_median_30s"]),
                )
                if d_stack.get("ok"):
                    decision = d_stack

            if decision is None:
                d_abs = detect_absorption_reject(
                    df_levels,
                    vol_zscore_min=float(params["absorption"]["vol_zscore_min"]),
                    delta_ratio_max=float(params["absorption"]["delta_ratio_max"]),
                    attempts_min=int(params["absorption"]["attempts_min"]),
                )
                if d_abs.get("ok"):
                    decision = d_abs

            if decision is None:
                return False, {"reason": "no trigger"}

            # 4) Construction de la décision scalping-burst (LIMIT+FOK, trailing only)
            micro_atr = _micro_atr_from_ticks(ticks, window_s=10)
            entry = {
                "style": cfg["order"]["entry_style"],          # "LIMIT_FOK"
                "price": float(decision["anchor_price"]) + float(cfg["order"]["price_offset_ticks"]) * price_step,
                "burst_count": int(cfg["order"]["burst_count"]),
                "burst_volume_each": float(cfg["order"]["burst_volume_each"]),
                "validity_ms": int(params["stacking"]["validity_ms"]),
            }
            trailing = {
                "phase0": {
                    "window_s": cfg["trailing"]["phase0_seconds"],
                    "anchor": "footprint_block_or_micro_atr",
                    "mult": float(cfg["trailing"]["phase0_mult_micro_atr_10s"]),
                },
                "phase1": {"mult": float(cfg["trailing"]["phase1_mult_micro_atr_10s"])},
                "phase2": {"mult": float(cfg["trailing"]["phase2_mult_micro_atr_10s"])},
                "clamp": [float(cfg["trailing"]["clamp_min"]), float(cfg["trailing"]["clamp_max"])],
                "micro_atr_10s": float(micro_atr),
            }

            decision_out = {
                "action": "BUY" if decision["direction"] == "BUY" else "SELL",
                "asset": asset,
                "trigger": decision["trigger"],
                "confidence": float(decision.get("confidence", 0.7)),
                "entry": entry,
                "exit": {
                    "type": "TRAILING_ONLY",
                    "phases": trailing,
                },
                "meta": {**decision.get("meta", {}), **meta},
            }
            return True, decision_out
        except Exception as e:
            return False, {"reason": f"error: {e}"}

    
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
