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
        Retourne (ok: bool, decision: dict) pour scalping burst via triggers Footprint.
        Version sans garde-fous (aucun filtre tickrate/couverture/phase, aucun 'enabled' gate).
        - Normalisation DATETIME unique (tz-aware -> naive UTC).
        - Triggers en cascade (climax -> stacking -> absorption), sans fallback de trade.
        - Entrée ONE_PRICE (burst) + sortie TRAILING_ONLY panier (close_all_at_once).
        """
        import pandas as pd

        # -------- logging util --------
        log = getattr(self, "logger", None)

        def _log(level: str, msg: str):
            try:
                if log:
                    getattr(log, level, log.info)(msg)
                else:
                    print(f"[{level.upper()}] {msg}")
            except Exception:
                pass

        # -------- safe imports --------
        try:
            from .features import _compute_footprint_snapshot, _micro_atr_from_ticks
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

        # --- NORMALISATION DATETIME (tz-aware -> naive UTC) ---------------------------
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

            # 1) index datetime -> naive UTC
            if pd.api.types.is_datetime64_any_dtype(df.index):
                df.index = _to_naive_utc_index(df.index)

            # 2) normaliser colonnes temporelles connues (tz-aware friendly)
            dt_cols = [c for c in ("dt", "datetime", "timestamp", "time") if c in df.columns]
            for c in dt_cols:
                if pd.api.types.is_datetime64_any_dtype(df[c]) or pd.api.types.is_object_dtype(df[c]):
                    df[c] = _to_naive_utc_series(df[c])

            # 3) s'assurer d'une colonne pivot 'dt'
            picked = None
            for c in (prefer_col, "dt", "datetime", "timestamp", "time"):
                if c in df.columns and pd.api.types.is_datetime64_any_dtype(df[c]):
                    picked = c
                    break
            if picked is None and pd.api.types.is_datetime64_any_dtype(df.index):
                df["dt"] = df.index
                picked = "dt"

            # 4) drop NaT + tri
            if picked:
                df = df[~df[picked].isna()]
                try:
                    df = df.sort_values(picked)
                except Exception:
                    pass

            return df
        # ------------------------------------------------------------------------------

        # -------- normalisation unique --------
        try:
            ticks = _normalize_dt_df(ticks, prefer_col="dt")
            bars = _normalize_dt_df(bars, prefer_col="time")  # adapte si besoin à ton schéma
        except Exception as e:
            return False, {"reason": f"datetime normalization error: {e}"}

        # -------- lecture des paramètres (sans gate 'enabled') --------
        sc = strategy_config or {}
        price_step = float(sc.get("price_step", 0.1))

        burst_block = sc.get("entry_rules", {}).get("scalping", {}).get("burst_scalping", {}) or {}
        cfg_fp = burst_block.get("footprint_triggers", {}) or getattr(self, "footprint_triggers", {}) or {}

        burst_count = max(1, int(burst_block.get("burst_size", 5)))  # pas de clamp max_burst_size
        order_entry_style = str(cfg_fp.get("entry_style", "LIMIT_FOK")).upper()
        order_validity_ms = int(cfg_fp.get("validity_ms", 800))
        price_offset_ticks = float(cfg_fp.get("price_offset_ticks", 0.0))
        window_s = int(cfg_fp.get("window_s", 5))

        # -------- snapshot footprint (dépendance dure, pas un "garde-fou") --------
        try:
            df_levels, meta = _compute_footprint_snapshot(ticks, price_step=price_step, window_s=window_s)
            if df_levels is None or df_levels.empty:
                return False, {"reason": "no footprint levels"}
        except Exception as e:
            return False, {"reason": f"footprint snapshot error: {e}"}

        # -------- triggers (analyse pure, aucun filtre autour) --------
        try:
            # 1) Climax après consolidation
            d_climax = detect_volume_climax_after_consolidation(
                bars,
                df_levels,
                lookback_bars=int(cfg_fp.get("climax", {}).get("lookback_bars", 8)),
                vol_ratio_min=float(cfg_fp.get("climax", {}).get("vol_ratio_min", 2.0)),
                delta_ratio_min=float(cfg_fp.get("climax", {}).get("delta_ratio_min", 1.5)),
                need_consolidation=bool(cfg_fp.get("climax", {}).get("need_consolidation", True)),
                consolidation_max_atr_mult=float(cfg_fp.get("climax", {}).get("consolidation_max_atr_mult", 1.0)),
            )
            decision = d_climax if d_climax.get("ok") else None

            # 2) Stacking d'imbalance
            if decision is None:
                d_stack = detect_imbalance_stacking(
                    df_levels,
                    delta_ratio_min=float(cfg_fp.get("stacking", {}).get("delta_ratio_min", 1.3)),
                    min_levels=int(cfg_fp.get("stacking", {}).get("min_levels", 3)),
                    invalidate_opposite_ratio=float(cfg_fp.get("stacking", {}).get("invalidate_opposite_ratio", 0.6)),
                    vol_level_min_ratio_median_30s=float(cfg_fp.get("vol_level_min_ratio_median_30s", 0.0)),
                )
                if d_stack.get("ok"):
                    decision = d_stack

            # 3) Absorption / rejet
            if decision is None:
                d_abs = detect_absorption_reject(
                    df_levels,
                    vol_zscore_min=float(cfg_fp.get("absorption", {}).get("vol_zscore_min", 2.0)),
                    delta_ratio_max=float(cfg_fp.get("absorption", {}).get("delta_ratio_max", 0.5)),
                    attempts_min=int(cfg_fp.get("absorption", {}).get("attempts_min", 2)),
                )
                if d_abs.get("ok"):
                    decision = d_abs

            if decision is None:
                return False, {"reason": "no trigger"}
        except Exception as e:
            return False, {"reason": f"trigger eval error: {e}"}

        # -------- construction de l’ordre & trailing (politique scalping burst) --------
        try:
            micro_atr = float(_micro_atr_from_ticks(ticks, window_s=int(cfg_fp.get("trail_atr_window_s", 10))))
            anchor_price = float(decision.get("anchor_price", meta.get("poc", 0.0)))
            direction = str(decision.get("direction", "BUY")).upper()

            # offset signé selon la direction
            signed_offset = float(price_offset_ticks) * float(price_step)
            signed_offset = -abs(signed_offset) if direction == "SELL" else abs(signed_offset)

            entry_price = float(anchor_price + signed_offset)

            entry = {
                "style": order_entry_style,   # e.g. LIMIT_FOK | MARKET
                "price": entry_price,
                "burst_count": int(burst_count),
                "validity_ms": int(order_validity_ms),
                # ONE_PRICE seulement (pas de ONE_SHOT qui peut impacter le sizing)
                "price_mode": "ONE_PRICE",
            }

            trailing = {
                "phase0": {
                    "window_s": int(cfg_fp.get("trailing", {}).get("phase0_seconds", 5)),
                    "anchor": "footprint_block_or_micro_atr",
                    "mult": float(cfg_fp.get("trailing", {}).get("phase0_mult_micro_atr_10s", 1.0)),
                },
                "phase1": {
                    "mult": float(cfg_fp.get("trailing", {}).get("phase1_mult_micro_atr_10s", 1.5)),
                },
                "phase2": {
                    "mult": float(cfg_fp.get("trailing", {}).get("phase2_mult_micro_atr_10s", 2.0)),
                },
                "clamp": [
                    float(cfg_fp.get("trailing", {}).get("clamp_min", 0.0)),
                    float(cfg_fp.get("trailing", {}).get("clamp_max", 9_999.0)),
                ],
                "micro_atr_10s": micro_atr,
            }

            out = {
                "action": "BUY" if direction == "BUY" else "SELL",
                "asset": asset,
                "trigger": decision.get("trigger", "footprint"),
                "confidence": float(decision.get("confidence", 0.7)),
                "entry": entry,
                "exit": {
                    "type": "TRAILING_ONLY",
                    "phases": trailing,
                    "basket": {
                        "scope": "BASKET",
                        "close_all_at_once": True,
                        "require_all_in_profit": True,
                        "sync_trailing": True,
                    },
                },
                "meta": {**meta, **(decision.get("meta", {}) or {})},
            }
            return True, out
        except Exception as e:
            return False, {"reason": f"order build error: {e}"}

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
