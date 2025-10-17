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
        Analyse Footprint PURE (sans gardes-fous externes).
        - Normalisation datetime unique.
        - Triggers en cascade: climax -> stacking -> absorption.
        - Si aucun trigger au 1er passage, un 2e passage "soft" abaisse légèrement les seuils (analyse only).
        Retour:
            (ok: bool, decision: dict | {"reason": ...})
            decision = {
                action: "BUY"/"SELL",
                asset: str,
                trigger: str,
                confidence: float,
                anchor_price: float,
                meta: dict
            }
        """
        import pandas as pd

        log = getattr(self, "logger", None)

        def _log(level: str, msg: str):
            try:
                if log:
                    getattr(log, level, log.info)(msg)
            except Exception:
                pass

        # --- imports ---
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

        # --- normalisation datetime ---
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

        # --- paramètres (aucun gate externe) ---
        sc = strategy_config or {}
        # Par défaut plus permissif: 0.01 convient bien à XAUUSD selon broker; tu peux override via conf.
        price_step = float(sc.get("price_step", 0.01))

        cfg = ((sc.get("entry_rules", {}) or {}).get("scalping", {}) or {}).get(
            "burst_scalping", {}
        ).get("footprint_triggers", {}) or {}

        # Pass 1 (valeurs raisonnables)
        p1 = dict(
            climax_lookback_bars=int(cfg.get("climax", {}).get("lookback_bars", 8)),
            climax_vol_ratio_min=float(
                cfg.get("climax", {}).get("vol_ratio_min", 1.6)
            ),  # 2.0 -> 1.6
            climax_delta_ratio_min=float(
                cfg.get("climax", {}).get("delta_ratio_min", 1.3)
            ),  # 1.5 -> 1.3
            climax_need_cons=bool(
                cfg.get("climax", {}).get("need_consolidation", False)
            ),  # True -> False
            climax_cons_atr_max=float(
                cfg.get("climax", {}).get("consolidation_max_atr_mult", 2.0)
            ),  # 1.0 -> 2.0
            stack_delta_ratio_min=float(
                cfg.get("stacking", {}).get("delta_ratio_min", 1.2)
            ),  # 1.3 -> 1.2
            stack_min_levels=int(
                cfg.get("stacking", {}).get("min_levels", 2)
            ),  # 3 -> 2
            stack_inval_opp_ratio=float(
                cfg.get("stacking", {}).get("invalidate_opposite_ratio", 0.65)
            ),
            stack_vol_lvl_min_med=float(cfg.get("vol_level_min_ratio_median_30s", 0.0)),
            abs_vol_z_min=float(
                cfg.get("absorption", {}).get("vol_zscore_min", 1.2)
            ),  # 2.0 -> 1.2
            abs_delta_ratio_max=float(
                cfg.get("absorption", {}).get("delta_ratio_max", 0.6)
            ),
            abs_attempts_min=int(
                cfg.get("absorption", {}).get("attempts_min", 1)
            ),  # 2 -> 1
            window_s=int(cfg.get("window_s", 5)),
        )

        # --- snapshot footprint ---
        try:
            df_levels, meta = _compute_footprint_snapshot(
                ticks, price_step=price_step, window_s=p1["window_s"]
            )
            if df_levels is None or df_levels.empty:
                return False, {"reason": "no footprint levels"}
        except Exception as e:
            return False, {"reason": f"footprint snapshot error: {e}"}

        # --- fonction d'essai des triggers (pour Pass 1 & Pass 2) ---
        def _try_triggers(params) -> "dict|None":
            # 1) Climax (si bars disponibles)
            try:
                d_climax = {}
                if bars is not None and not getattr(bars, "empty", True):
                    d_climax = detect_volume_climax_after_consolidation(
                        bars,
                        df_levels,
                        lookback_bars=params["climax_lookback_bars"],
                        vol_ratio_min=params["climax_vol_ratio_min"],
                        delta_ratio_min=params["climax_delta_ratio_min"],
                        need_consolidation=params["climax_need_cons"],
                        consolidation_max_atr_mult=params["climax_cons_atr_max"],
                    )
                    if d_climax.get("ok"):
                        return d_climax
            except Exception as e:
                _log("debug", f"[TRIGGER] climax error: {e}")

            # 2) Stacking
            try:
                d_stack = detect_imbalance_stacking(
                    df_levels,
                    delta_ratio_min=params["stack_delta_ratio_min"],
                    min_levels=params["stack_min_levels"],
                    invalidate_opposite_ratio=params["stack_inval_opp_ratio"],
                    vol_level_min_ratio_median_30s=params["stack_vol_lvl_min_med"],
                )
                if d_stack.get("ok"):
                    return d_stack
            except Exception as e:
                _log("debug", f"[TRIGGER] stacking error: {e}")

            # 3) Absorption
            try:
                d_abs = detect_absorption_reject(
                    df_levels,
                    vol_zscore_min=params["abs_vol_z_min"],
                    delta_ratio_max=params["abs_delta_ratio_max"],
                    attempts_min=params["abs_attempts_min"],
                )
                if d_abs.get("ok"):
                    return d_abs
            except Exception as e:
                _log("debug", f"[TRIGGER] absorption error: {e}")

            return None

        # --- Pass 1 (normal-permissif) ---
        decision = _try_triggers(p1)

        # --- Pass 2 "soft" (si toujours rien) : baisse légère & contrôlée des seuils ---
        if decision is None:
            p2 = p1.copy()
            p2.update(
                dict(
                    climax_vol_ratio_min=max(1.2, p1["climax_vol_ratio_min"] * 0.85),
                    climax_delta_ratio_min=max(
                        1.1, p1["climax_delta_ratio_min"] * 0.85
                    ),
                    stack_delta_ratio_min=max(1.05, p1["stack_delta_ratio_min"] * 0.9),
                    abs_vol_z_min=max(0.8, p1["abs_vol_z_min"] * 0.8),
                    abs_attempts_min=1,
                )
            )
            decision = _try_triggers(p2)

        if decision is None:
            return False, {"reason": "no trigger"}

        # --- sortie décision ---
        try:
            direction = str(decision.get("direction", "BUY")).upper()
            anchor_price = float(
                decision.get("anchor_price", (meta or {}).get("poc", 0.0))
            )
        except Exception:
            direction, anchor_price = "BUY", float((meta or {}).get("poc", 0.0) or 0.0)

        out = {
            "action": "BUY" if direction == "BUY" else "SELL",
            "asset": asset,
            "trigger": decision.get("trigger", "footprint"),
            "confidence": float(decision.get("confidence", 0.7) or 0.7),
            "anchor_price": anchor_price,
            "meta": {**(meta or {}), **(decision.get("meta", {}) or {})},
        }
        return True, out

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
