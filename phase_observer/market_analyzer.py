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
        candles = [
            detect_single_candle(annotated_df, i) for i in range(len(annotated_df))
        ]
        multi_patterns = detect_multi_candle_patterns(annotated_df)
        combo_patterns = detect_combos(annotated_df)
        orderflow_signals = detect_orderflow_v5(annotated_df)

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
    def analyze_footprint_triggers(
        self,
        asset: str,
        ticks: "pd.DataFrame",
        bars: "pd.DataFrame",
        strategy_config: "Dict[str, Any]",
    ):
        """
        Analyse Footprint PURE (sans garde-fous, sans packaging d'ordre).
        Retour: (ok: bool, decision: dict)
            decision = {
                action: "BUY"/"SELL",
                asset: str,
                trigger: str,
                confidence: float,
                anchor_price: float,
                meta: dict   # footprint meta enrichi
            }
        """
        import pandas as pd

        # ---------- logging util (ne change pas la logique) ----------
        log = getattr(self, "logger", None)

        def _log(level: str, msg: str):
            try:
                if log:
                    getattr(log, level, log.info)(msg)
            except Exception:
                pass

        # ---------- imports nécessaires ----------
        try:
            from .features import _compute_footprint_snapshot
        except Exception as e:
            return False, {"reason": f"features import error: {e}"}

        try:
            from .detectors import (
                detect_volume_climax_after_consolidation,
                detect_imbalance_stacking,
                detect_absorption_reject,
            )
        except Exception as e:
            return False, {"reason": f"triggers import error: {e}"}

        # ---------- normalisation DATETIME (strictement technique) ----------
        def _to_naive_utc_series(s: pd.Series) -> pd.Series:
            s = pd.to_datetime(s, errors="coerce")
            if pd.api.types.is_datetime64tz_dtype(s):
                s = s.dt.tz_convert("UTC").dt.tz_localize(None)
            return s

        def _to_naive_utc_index(idx: pd.Index) -> pd.Index:
            idx = pd.to_datetime(idx, errors="coerce")
            if getattr(idx, "tz", None) is not None:
                idx = idx.tz_convert("UTC").tz_localize(None)
            return idx

        def _normalize_dt_df(df: pd.DataFrame, prefer_col: str = "dt") -> pd.DataFrame:
            if df is None or df.empty:
                return df
            df = df.copy()
            if pd.api.types.is_datetime64_any_dtype(df.index):
                df.index = _to_naive_utc_index(df.index)
            dt_cols = [
                c for c in ("dt", "datetime", "timestamp", "time") if c in df.columns
            ]
            for c in dt_cols:
                if pd.api.types.is_datetime64_any_dtype(
                    df[c]
                ) or pd.api.types.is_object_dtype(df[c]):
                    df[c] = _to_naive_utc_series(df[c])
            picked = None
            for c in (prefer_col, "dt", "datetime", "timestamp", "time"):
                if c in df.columns and pd.api.types.is_datetime64_any_dtype(df[c]):
                    picked = c
                    break
            if picked is None and pd.api.types.is_datetime64_any_dtype(df.index):
                df["dt"] = df.index
                picked = "dt"
            if picked:
                df = df[~df[picked].isna()]
                try:
                    df = df.sort_values(picked)
                except Exception:
                    pass
            return df

        try:
            ticks = _normalize_dt_df(ticks, prefer_col="dt")
            bars = _normalize_dt_df(bars, prefer_col="time")
        except Exception as e:
            return False, {"reason": f"datetime normalization error: {e}"}

        # ---------- paramètres MINIMAUX (pas de gate/whitelist/tickrate/etc.) ----------
        sc = strategy_config or {}
        price_step = float(
            sc.get("price_step", 0.1)
        )  # besoin technique pour le snapshot
        # Seuils triggers lisibles directement dans la conf, sinon valeurs par défaut
        cfg = ((sc.get("entry_rules", {}) or {}).get("scalping", {}) or {}).get(
            "burst_scalping", {}
        ).get("footprint_triggers", {}) or {}

        climax_lookback_bars = int(cfg.get("climax", {}).get("lookback_bars", 8))
        climax_vol_ratio_min = float(cfg.get("climax", {}).get("vol_ratio_min", 2.0))
        climax_delta_ratio_min = float(
            cfg.get("climax", {}).get("delta_ratio_min", 1.5)
        )
        climax_need_cons = bool(cfg.get("climax", {}).get("need_consolidation", True))
        climax_cons_atr_max = float(
            cfg.get("climax", {}).get("consolidation_max_atr_mult", 1.0)
        )

        stack_delta_ratio_min = float(
            cfg.get("stacking", {}).get("delta_ratio_min", 1.3)
        )
        stack_min_levels = int(cfg.get("stacking", {}).get("min_levels", 3))
        stack_inval_opp_ratio = float(
            cfg.get("stacking", {}).get("invalidate_opposite_ratio", 0.6)
        )
        stack_vol_lvl_min_med = float(cfg.get("vol_level_min_ratio_median_30s", 0.0))

        abs_vol_z_min = float(cfg.get("absorption", {}).get("vol_zscore_min", 2.0))
        abs_delta_ratio_max = float(
            cfg.get("absorption", {}).get("delta_ratio_max", 0.5)
        )
        abs_attempts_min = int(cfg.get("absorption", {}).get("attempts_min", 2))

        window_s = int(cfg.get("window_s", 5))

        # ---------- snapshot footprint (dépendance utile, pas un filtre) ----------
        try:
            df_levels, meta = _compute_footprint_snapshot(
                ticks, price_step=price_step, window_s=window_s
            )
            if df_levels is None or df_levels.empty:
                return False, {"reason": "no footprint levels"}
        except Exception as e:
            return False, {"reason": f"footprint snapshot error: {e}"}

        # ---------- TRIGGERS (analyse pure, sans autre condition) ----------
        try:
            decision = None

            d_climax = detect_volume_climax_after_consolidation(
                bars,
                df_levels,
                lookback_bars=climax_lookback_bars,
                vol_ratio_min=climax_vol_ratio_min,
                delta_ratio_min=climax_delta_ratio_min,
                need_consolidation=climax_need_cons,
                consolidation_max_atr_mult=climax_cons_atr_max,
            )
            if d_climax.get("ok"):
                decision = d_climax

            if decision is None:
                d_stack = detect_imbalance_stacking(
                    df_levels,
                    delta_ratio_min=stack_delta_ratio_min,
                    min_levels=stack_min_levels,
                    invalidate_opposite_ratio=stack_inval_opp_ratio,
                    vol_level_min_ratio_median_30s=stack_vol_lvl_min_med,
                )
                if d_stack.get("ok"):
                    decision = d_stack

            if decision is None:
                d_abs = detect_absorption_reject(
                    df_levels,
                    vol_zscore_min=abs_vol_z_min,
                    delta_ratio_max=abs_delta_ratio_max,
                    attempts_min=abs_attempts_min,
                )
                if d_abs.get("ok"):
                    decision = d_abs

            if decision is None:
                return False, {"reason": "no trigger"}

            direction = str(decision.get("direction", "BUY")).upper()
            anchor_price = float(decision.get("anchor_price", meta.get("poc", 0.0)))
            out = {
                "action": "BUY" if direction == "BUY" else "SELL",
                "asset": asset,
                "trigger": decision.get("trigger", "footprint"),
                "confidence": float(decision.get("confidence", 0.7)),
                "anchor_price": anchor_price,
                "meta": {**meta, **(decision.get("meta", {}) or {})},
            }
            return True, out

        except Exception as e:
            return False, {"reason": f"trigger eval error: {e}"}

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
