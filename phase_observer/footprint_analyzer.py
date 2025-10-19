# phase_observer/footprint_analyzer.py

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Any, Tuple, Optional, List
import pandas as pd
import numpy as np
from contextlib import contextmanager
import time

# Tes détecteurs “maison”
from .detectors import (
    detect_imbalance_stacking,
    detect_absorption_reject,
    detect_volume_climax_after_consolidation,
)

"""
Footprint Trigger Analysis — Fixed & Enhanced
---------------------------------------------
- Param mapping correct vers chaque détecteur (plus d'**kwargs inattendus)
- Multi-fenêtres + double passe (normal/soft)
- Sélection du meilleur trigger (priorités cohérentes + alias)
- Fallbacks micro (stacking / absorption) si rien
- Validation & normalisation tolérantes (feeds variés)
- Observabilité (timings) sans garde-fous bloquants
"""


# ========================= enums / dataclasses =========================

class ErrorCode(Enum):
    IMPORT_FAILURE = "E001"
    DATA_VALIDATION = "E002"
    DATETIME_NORMALIZATION = "E003"
    SNAPSHOT_COMPUTATION = "E004"
    TRIGGER_DETECTION = "E005"
    INSUFFICIENT_DATA = "E006"


class TriggerType(Enum):
    CLIMAX = "climax_after_consolidation"
    STACKING = "imbalance_stacking"
    ABSORPTION = "absorption_reject"
    MICRO_STACK = "stacking_inline"
    MICRO_ABSORPTION = "absorption_inline"


@dataclass
class TriggerConfig:
    window_candidates_s: List[int] = field(default_factory=lambda: [3, 5, 8])

    # Climax
    climax_lookback_bars: int = 8
    climax_vol_ratio_min: float = 1.6
    climax_delta_ratio_min: float = 1.3
    climax_need_consolidation: bool = False
    climax_cons_atr_max: float = 2.0

    # Stacking
    stack_delta_ratio_min: float = 1.2
    stack_min_levels: int = 2
    stack_invalidate_opp_ratio: float = 0.65
    stack_vol_lvl_min_med: float = 0.0

    # Absorption
    abs_vol_z_min: float = 1.2
    abs_delta_ratio_max: float = 0.6
    abs_attempts_min: int = 1

    @property
    def soft_params(self) -> Dict[str, float]:
        return {
            "climax_vol_ratio_min": max(1.15, self.climax_vol_ratio_min * 0.85),
            "climax_delta_ratio_min": max(1.10, self.climax_delta_ratio_min * 0.85),
            "stack_delta_ratio_min": max(1.05, self.stack_delta_ratio_min * 0.90),
            "abs_vol_z_min": max(0.80, self.abs_vol_z_min * 0.80),
            "abs_attempts_min": 1,
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "window_candidates_s": self.window_candidates_s,
            "climax_lookback_bars": self.climax_lookback_bars,
            "climax_vol_ratio_min": self.climax_vol_ratio_min,
            "climax_delta_ratio_min": self.climax_delta_ratio_min,
            "climax_need_consolidation": self.climax_need_consolidation,
            "climax_cons_atr_max": self.climax_cons_atr_max,
            "stack_delta_ratio_min": self.stack_delta_ratio_min,
            "stack_min_levels": self.stack_min_levels,
            "stack_invalidate_opp_ratio": self.stack_invalidate_opp_ratio,
            "stack_vol_lvl_min_med": self.stack_vol_lvl_min_med,
            "abs_vol_z_min": self.abs_vol_z_min,
            "abs_delta_ratio_max": self.abs_delta_ratio_max,
            "abs_attempts_min": self.abs_attempts_min,
        }

    @classmethod
    def from_strategy_config(cls, strategy_config: Dict[str, Any]) -> "TriggerConfig":
        node = (
            (strategy_config or {})
            .get("entry_rules", {}).get("scalping", {})
            .get("burst_scalping", {}).get("footprint_triggers", {}) or {}
        )
        c = node.get("climax", {}) or {}
        s = node.get("stacking", {}) or {}
        a = node.get("absorption", {}) or {}
        return cls(
            window_candidates_s=node.get("window_candidates_s", [3, 5, 8]),
            climax_lookback_bars=int(c.get("lookback_bars", 8)),
            climax_vol_ratio_min=float(c.get("vol_ratio_min", 1.6)),
            climax_delta_ratio_min=float(c.get("delta_ratio_min", 1.3)),
            climax_need_consolidation=bool(c.get("need_consolidation", False)),
            climax_cons_atr_max=float(c.get("consolidation_max_atr_mult", 2.0)),
            stack_delta_ratio_min=float(s.get("delta_ratio_min", 1.2)),
            stack_min_levels=int(s.get("min_levels", 2)),
            stack_invalidate_opp_ratio=float(s.get("invalidate_opposite_ratio", 0.65)),
            stack_vol_lvl_min_med=float(node.get("vol_level_min_ratio_median_30s", 0.0)),
            abs_vol_z_min=float(a.get("vol_zscore_min", 1.2)),
            abs_delta_ratio_max=float(a.get("delta_ratio_max", 0.6)),
            abs_attempts_min=int(a.get("attempts_min", 1)),
        )


@dataclass
class ValidationResult:
    is_valid: bool
    error_code: Optional[ErrorCode] = None
    message: str = ""
    metrics: Dict[str, Any] = field(default_factory=dict)


@dataclass
class TriggerDecision:
    action: str
    asset: str
    trigger: str
    confidence: float
    anchor_price: float
    meta: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action,
            "asset": self.asset,
            "trigger": self.trigger,
            "confidence": self.confidence,
            "anchor_price": self.anchor_price,
            "meta": self.meta,
        }


# ========================= utilities =========================

class DataValidator:
    @staticmethod
    def validate_ticks(ticks: pd.DataFrame) -> ValidationResult:
        if ticks is None or ticks.empty:
            return ValidationResult(False, ErrorCode.INSUFFICIENT_DATA, "Ticks dataframe is empty")

        cols = set(ticks.columns)
        has_price = ("price" in cols) or ({"bid", "ask"} <= cols) or ("last" in cols)
        has_vol   = any(c in cols for c in ("volume", "size", "qty", "amount", "vol"))
        if not has_price or not has_vol:
            return ValidationResult(False, ErrorCode.DATA_VALIDATION, "Missing price/volume-like columns")

        if "price" in cols:
            try:
                if (ticks["price"] <= 0).any():
                    return ValidationResult(False, ErrorCode.DATA_VALIDATION, "Detected non-positive prices")
            except Exception:
                pass  # feed exotique → on laisse passer

        return ValidationResult(True, metrics={"row_count": int(len(ticks))})

    @staticmethod
    def validate_bars(bars: pd.DataFrame) -> ValidationResult:
        if bars is None or bars.empty:
            return ValidationResult(True, message="Bars optional, skipping")
        cols = set(bars.columns)
        required = {"open","high","low","close"}
        missing = required - cols
        if missing:
            return ValidationResult(False, ErrorCode.DATA_VALIDATION, f"Bars missing: {sorted(missing)}")
        try:
            invalid = (
                (bars["high"] < bars["low"]) |
                (bars["high"] < bars["open"]) |
                (bars["high"] < bars["close"]) |
                (bars["low"]  > bars["open"]) |
                (bars["low"]  > bars["close"])
            )
            if invalid.any():
                return ValidationResult(False, ErrorCode.DATA_VALIDATION, f"Invalid OHLC in {int(invalid.sum())} bars")
        except Exception as e:
            return ValidationResult(False, ErrorCode.DATA_VALIDATION, f"OHLC validation failed: {e}")

        vol_col = None
        for c in ("volume", "tick_volume", "tickVolume", "vol"):
            if c in cols:
                vol_col = c
                break
        return ValidationResult(True, metrics={"bar_count": int(len(bars)), "vol_col": vol_col})


class DatetimeNormalizer:
    @staticmethod
    def normalize_series(s: pd.Series) -> pd.Series:
        s = pd.to_datetime(s, errors="coerce")
        if pd.api.types.is_datetime64tz_dtype(s):
            s = s.dt.tz_convert("UTC").dt.tz_localize(None)
        return s

    @staticmethod
    def normalize_index(idx: pd.Index) -> pd.Index:
        idx = pd.to_datetime(idx, errors="coerce")
        if getattr(idx, "tz", None):
            idx = idx.tz_convert("UTC").tz_localize(None)
        return idx

    @classmethod
    def normalize_dataframe(cls, df: pd.DataFrame, prefer_col: str = "dt") -> pd.DataFrame:
        if df is None or df.empty:
            return df
        if pd.api.types.is_datetime64_any_dtype(df.index):
            df.index = cls.normalize_index(df.index)
        dt_cols = [c for c in ("dt","datetime","timestamp","time") if c in df.columns]
        for c in dt_cols:
            if pd.api.types.is_datetime64_any_dtype(df[c]) or pd.api.types.is_object_dtype(df[c]):
                df[c] = cls.normalize_series(df[c])
        picked = next((
            c for c in (prefer_col,"dt","datetime","timestamp","time")
            if c in df.columns and pd.api.types.is_datetime64_any_dtype(df[c])
        ), None)
        if picked is None and pd.api.types.is_datetime64_any_dtype(df.index):
            df["dt"] = df.index; picked = "dt"
        if picked:
            df = df[~df[picked].isna()].sort_values(picked)
        return df


class ConfidenceScorer:
    @staticmethod
    def _alias(trigger: str) -> str:
        mapping = {
            "stacking": TriggerType.STACKING.value,
            "imbalance_stacking": TriggerType.STACKING.value,
            "climax_after_consolidation": TriggerType.CLIMAX.value,
            "absorption_reject": TriggerType.ABSORPTION.value,
            "stacking_inline": TriggerType.MICRO_STACK.value,
            "absorption_inline": TriggerType.MICRO_ABSORPTION.value,
        }
        return mapping.get(str(trigger), str(trigger))

    @staticmethod
    def boost_from_metadata(decision: Dict[str, Any], meta: Dict[str, Any], price_step: float) -> float:
        meta = meta or {}
        conf = float(decision.get("confidence", 0.7) or 0.7)
        sdir = 1 if str(decision.get("direction","BUY")).upper() == "BUY" else -1
        dtot = float(meta.get("delta_total", 0.0) or 0.0)
        if dtot and np.sign(dtot) == sdir:
            conf = min(0.99, conf + 0.05)
        anc = float(decision.get("anchor_price", meta.get("poc", 0.0)) or 0.0)
        poc = float(meta.get("poc", 0.0) or 0.0)
        if anc and poc and abs(anc - poc) <= price_step:
            conf = max(0.55, conf - 0.03)
        return conf

    @staticmethod
    def select_best_decision(candidates: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        valid = [c for c in candidates if c and c.get("ok")]
        if not valid:
            return None
        prio = {
            TriggerType.STACKING.value: 3,
            TriggerType.CLIMAX.value: 2,
            TriggerType.ABSORPTION.value: 1.5,
            TriggerType.MICRO_STACK.value: 1.2,
            TriggerType.MICRO_ABSORPTION.value: 1.0,
        }
        valid.sort(
            key=lambda d: (float(d.get("confidence", 0.0)), prio.get(ConfidenceScorer._alias(d.get("trigger")), 0)),
            reverse=True
        )
        best = valid[0]
        best["trigger"] = ConfidenceScorer._alias(best.get("trigger"))
        return best


class PerformanceMonitor:
    @contextmanager
    def measure_phase(self, phase_name: str):
        start = time.perf_counter()
        try:
            yield
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            self._record_metric(f"footprint.{phase_name}.duration_ms", elapsed_ms)

    def _record_metric(self, metric_name: str, value: float):
        # Plug vers Prometheus/StatsD si besoin
        pass


# ========================= main analyzer =========================

class FootprintAnalyzer:
    def __init__(self, logger=None, metrics_monitor: Optional[PerformanceMonitor] = None):
        self.logger = logger
        self.monitor = metrics_monitor or PerformanceMonitor()
        self.validator = DataValidator()
        self.normalizer = DatetimeNormalizer()
        self.scorer = ConfidenceScorer()

    # ---- public -------------------------------------------------------

    def analyze_footprint_triggers(
        self,
        asset: str,
        ticks: pd.DataFrame,
        bars: Optional[pd.DataFrame],
        strategy_config: Dict[str, Any],
    ) -> Tuple[bool, Dict[str, Any]]:
        with self.monitor.measure_phase("total"):
            # 0) pré-traitement feed-agnostic (price/volume)
            ticks = self._ensure_price_volume_columns(ticks)

            # 1) validation
            with self.monitor.measure_phase("validation"):
                vt = self.validator.validate_ticks(ticks)
                if not vt.is_valid:
                    return False, {"reason": vt.message, "error_code": vt.error_code.value}
                vb = self.validator.validate_bars(bars)
                if not vb.is_valid:
                    return False, {"reason": vb.message, "error_code": vb.error_code.value}

            # 2) config
            cfg = TriggerConfig.from_strategy_config(strategy_config or {})
            price_step = float((strategy_config or {}).get("price_step", 0.01) or 0.01)

            # 3) datetime normalization
            with self.monitor.measure_phase("datetime_norm"):
                try:
                    ticks = self.normalizer.normalize_dataframe(ticks.copy(), "dt")
                    bars  = self.normalizer.normalize_dataframe(bars.copy(), "time") if bars is not None else None
                except Exception as e:
                    return False, {"reason": f"Datetime normalization failed: {e}",
                                   "error_code": ErrorCode.DATETIME_NORMALIZATION.value}

            # 4) exploration multi-fenêtres / double passe
            best_decision = None
            best_meta = None
            best_win: Optional[int] = None

            for pass_type in ("normal", "soft"):
                params_map: Dict[str, Any] = (
                    cfg.to_dict() if pass_type == "normal"
                    else {**cfg.to_dict(), **cfg.soft_params}
                )

                for win in params_map.get("window_candidates_s", cfg.window_candidates_s):
                    with self.monitor.measure_phase(f"window_{int(win)}s"):
                        decision, meta, used_win = self._analyze_single_window(
                            ticks=ticks,
                            bars=bars,
                            window_s=int(win),
                            params=params_map,
                            price_step=price_step,
                            cfg=cfg,
                        )

                        if decision and decision.get("ok"):
                            if (best_decision is None) or (
                                float(decision.get("confidence", 0)) >
                                float(best_decision.get("confidence", 0))
                            ):
                                best_decision, best_meta, best_win = decision, meta, used_win

                if best_decision:
                    break  # on s'arrête dès qu'on a un signal dans la passe courante

            # 5) Build response
            if not best_decision:
                return False, {"reason": "no_trigger_detected"}

            return True, self._build_trigger_response(
                asset=asset,
                decision=best_decision,
                meta=best_meta,
                window_used=int(best_win or cfg.window_candidates_s[0]),
            )

    # ---- internals ----------------------------------------------------

    def _analyze_single_window(
        self,
        ticks: pd.DataFrame,
        bars: Optional[pd.DataFrame],
        window_s: int,
        params: Dict[str, Any],
        price_step: float,
        cfg: TriggerConfig
    ) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]], Optional[int]]:
        """
        Retourne (best_decision, meta_snapshot, window_used)
        """
        # --- imports dynamiques ---
        try:
            from .features import _compute_footprint_snapshot
        except Exception as e:
            self._log_error("imports", e)
            return None, None, None

        # --- params: tolère dataclass ou dict ---
        if isinstance(params, TriggerConfig):
            params = params.to_dict()

        # --- snapshot footprint ---
        try:
            df_levels, meta = _compute_footprint_snapshot(
                ticks, price_step=price_step, window_s=int(window_s)
            )
            if df_levels is None or df_levels.empty:
                return None, None, None
            try:
                df_levels = df_levels.sort_index()
            except Exception:
                pass
        except Exception as e:
            self._log_error("snapshot", e, {"window_s": window_s})
            return None, None, None

        candidates: List[Dict[str, Any]] = []

        # --- 1) Climax (bars optionnelles) ---
        if bars is not None and not getattr(bars, "empty", True):
            climax_kwargs = {}
            try:
                lookback = int(params.get("climax_lookback_bars", cfg.climax_lookback_bars))
                bar_slice = bars.tail(max(lookback + 5, 30))  # slice perf
                climax_kwargs = dict(
                    lookback_bars=lookback,
                    vol_ratio_min=float(params.get("climax_vol_ratio_min", cfg.climax_vol_ratio_min)),
                    delta_ratio_min=float(params.get("climax_delta_ratio_min", cfg.climax_delta_ratio_min)),
                    need_consolidation=bool(params.get("climax_need_consolidation", cfg.climax_need_consolidation)),
                    consolidation_max_atr_mult=float(params.get("climax_cons_atr_max", cfg.climax_cons_atr_max)),
                )
                d1 = detect_volume_climax_after_consolidation(bar_slice, df_levels, **climax_kwargs)
                if d1.get("ok"):
                    candidates.append(d1)
            except Exception as e:
                self._log_error("climax_detection", e, {"kwargs": climax_kwargs, "window_s": window_s})

        # --- 2) Stacking ---
        stack_kwargs = {}
        try:
            stack_kwargs = dict(
                delta_ratio_min=float(params.get("stack_delta_ratio_min", cfg.stack_delta_ratio_min)),
                min_levels=int(params.get("stack_min_levels", cfg.stack_min_levels)),
                invalidate_opposite_ratio=float(params.get("stack_invalidate_opp_ratio", cfg.stack_invalidate_opp_ratio)),
                vol_level_min_ratio_median_30s=float(params.get("stack_vol_lvl_min_med", cfg.stack_vol_lvl_min_med)),
            )
            d2 = detect_imbalance_stacking(df_levels, **stack_kwargs)
            if d2.get("ok"):
                candidates.append(d2)
        except Exception as e:
            self._log_error("stacking_detection", e, {"kwargs": stack_kwargs, "window_s": window_s})

        # --- 3) Absorption ---
        abs_kwargs = {}
        try:
            abs_kwargs = dict(
                vol_zscore_min=float(params.get("abs_vol_z_min", cfg.abs_vol_z_min)),
                delta_ratio_max=float(params.get("abs_delta_ratio_max", cfg.abs_delta_ratio_max)),
                attempts_min=int(params.get("abs_attempts_min", cfg.abs_attempts_min)),
            )
            d3 = detect_absorption_reject(df_levels, **abs_kwargs)
            if d3.get("ok"):
                candidates.append(d3)
        except Exception as e:
            self._log_error("absorption_detection", e, {"kwargs": abs_kwargs, "window_s": window_s})

        # --- 4) Fallbacks micro si rien ---
        if not candidates:
            cols = set(df_levels.columns)
            if {"vol", "delta", "delta_ratio"}.issubset(cols):
                fb1 = self._micro_stacking(df_levels)
                if fb1: candidates.append(fb1)
            if {"zscore_vol", "delta_ratio", "delta"}.issubset(cols):
                fb2 = self._micro_absorption(df_levels)
                if fb2: candidates.append(fb2)

        if not candidates:
            return None, meta, window_s

        # --- boost confiance & sélection ---
        for d in candidates:
            d["confidence"] = self.scorer.boost_from_metadata(d, meta, price_step)

        best = self.scorer.select_best_decision(candidates)
        if not best:
            return None, meta, window_s

        try:
            best["meta"] = {**(best.get("meta") or {}), "used_window_s": int(window_s)}
        except Exception:
            pass

        return best, meta, window_s

    def _build_trigger_response(self, asset: str, decision: Dict[str, Any], meta: Dict[str, Any], window_used: int) -> Dict[str, Any]:
        meta = meta or {}
        direction = str(decision.get("direction","BUY")).upper()
        return TriggerDecision(
            action="BUY" if direction == "BUY" else "SELL",
            asset=asset,
            trigger=decision.get("trigger","footprint"),
            confidence=float(decision.get("confidence",0.7) or 0.7),
            anchor_price=float(decision.get("anchor_price", meta.get("poc", 0.0))),
            meta={**meta, **(decision.get("meta",{}) or {}), "window_used": int(window_used)},
        ).to_dict()

    # ------------------- fallbacks micro -------------------

    def _micro_stacking(self, df_levels: pd.DataFrame) -> Optional[Dict[str, Any]]:
        """2-3 niveaux adjacents, même signe, delta_ratio > 1.1 (tolère 1 gap)."""
        try:
            lv = df_levels
            req = {"vol","delta","delta_ratio"}
            if any(c not in lv.columns for c in req):
                return None
            lv = lv[lv["vol"] > 0].sort_index()
            if lv.empty:
                return None

            idx = lv.index.values.astype(float)
            sign = np.sign(lv["delta"].values)
            ratio = lv["delta_ratio"].values

            diffs = np.diff(np.unique(idx))
            step = np.quantile(diffs[diffs > 0], 0.1) if len(diffs) > 0 else 0.0

            i = 0
            while i < len(idx) - 1:
                if sign[i] == 0 or ratio[i] < 1.1:
                    i += 1
                    continue
                j = i + 1
                gaps = 0
                run = 1
                sgn = sign[i]
                while j < len(idx):
                    if sign[j] != sgn or ratio[j] < 1.1:
                        break
                    if step > 0 and (idx[j] - idx[j - 1]) > 1.6 * step:
                        gaps += 1
                        if gaps > 1:
                            break
                    run += 1
                    j += 1
                if run >= 2:
                    direction = "BUY" if sgn > 0 else "SELL"
                    anchor = float(idx[j - 1])
                    conf = min(0.9, 0.55 + 0.15 * (run - 2) + 0.1 * np.clip(ratio[i:j].mean() / 1.1, 0, 1.5))
                    return {
                        "ok": True,
                        "trigger": TriggerType.MICRO_STACK.value,
                        "direction": direction,
                        "confidence": float(conf),
                        "anchor_price": anchor,
                        "meta": {"levels": int(run), "delta_ratio_mean": float(ratio[i:j].mean())},
                    }
                i = j
            return None
        except Exception:
            return None

    def _micro_absorption(self, df_levels: pd.DataFrame) -> Optional[Dict[str, Any]]:
        """zscore_vol >= 1.1 + voisin opposé clair (delta_ratio>=0.6)."""
        try:
            lv = df_levels
            req = {"zscore_vol","delta_ratio","delta"}
            if any(c not in lv.columns for c in req):
                return None
            lv = lv.sort_index()
            cand = lv[(lv["zscore_vol"] >= 1.1) & (lv["delta_ratio"] <= 0.6)]
            if cand.empty:
                return None
            for price, row in cand.iterrows():
                i = lv.index.get_loc(price)
                neigh = []
                if i > 0: neigh.append(lv.iloc[i - 1])
                if i + 1 < len(lv): neigh.append(lv.iloc[i + 1])
                for nb in neigh:
                    if np.sign(nb["delta"]) != np.sign(row["delta"]) and nb["delta_ratio"] >= 0.6:
                        direction = "BUY" if nb["delta"] > 0 else "SELL"
                        conf = min(0.9, 0.6 + 0.2 * (row["zscore_vol"] / 1.1))
                        return {
                            "ok": True,
                            "trigger": TriggerType.MICRO_ABSORPTION.value,
                            "direction": direction,
                            "confidence": float(conf),
                            "anchor_price": float(nb.name),
                            "meta": {"absorbed_level": float(price), "absorbed_zscore": float(row["zscore_vol"])},
                        }
            return None
        except Exception:
            return None

    # ------------------- helpers -------------------

    def _ensure_price_volume_columns(self, ticks: pd.DataFrame) -> pd.DataFrame:
        df = ticks.copy()
        if "price" not in df.columns:
            if "last" in df.columns:
                df["price"] = df["last"]
            elif "bid" in df.columns and "ask" in df.columns:
                df["price"] = (df["bid"] + df["ask"]) * 0.5
        if "volume" not in df.columns:
            for alt in ("volume", "size", "qty", "amount", "vol"):
                if alt in df.columns:
                    df["volume"] = df[alt]
                    break
        return df

    def _log_error(self, context: str, error: Exception, extras: Dict[str, Any] = None):
        if self.logger:
            try:
                self.logger.error(
                    f"[FootprintAnalyzer] {context} failed",
                    extra={"error": str(error), "context": context, **(extras or {})}
                )
            except Exception:
                self.logger.error(f"[FootprintAnalyzer] {context} failed: {error}")
        else:
            print(f"[ERROR] {context}: {error} | {extras or {}}")


# ========================= wrapper (même signature) =========================
# -> drop-in : si un vieux appel importe directement cette fonction

def analyze_footprint_triggers(
    self,
    asset: str,
    ticks: "pd.DataFrame",
    bars: "pd.DataFrame",
    strategy_config: "Dict[str, Any]",
):
    analyzer = FootprintAnalyzer(logger=getattr(self, "logger", None))
    ok, result = analyzer.analyze_footprint_triggers(asset, ticks, bars, strategy_config)
    return ok, result
