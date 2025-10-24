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
        node = (strategy_config or {}).get("entry_rules", {}).get("scalping", {}).get(
            "burst_scalping", {}
        ).get("footprint_triggers", {}) or {}
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
            stack_vol_lvl_min_med=float(
                node.get("vol_level_min_ratio_median_30s", 0.0)
            ),
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
        """
        Validation tolérante des ticks :
        - Vérifie la présence de colonnes prix/volume *ou équivalents*.
        - N'échoue plus si quelques lignes ont un prix non-positif : on valide
        si au moins un sous-ensemble exploitable (prix > 0) existe.
        - Expose des métriques utiles (lignes totales, lignes valides).
        """
        if ticks is None or ticks.empty:
            return ValidationResult(
                False, ErrorCode.INSUFFICIENT_DATA, "Ticks dataframe is empty"
            )

        cols = set(ticks.columns)

        has_price_like = (
            ("price" in cols) or ({"bid", "ask"} <= cols) or ("last" in cols)
        )
        has_vol_like = any(
            c in cols for c in ("volume", "size", "qty", "amount", "vol")
        )
        if not has_price_like or not has_vol_like:
            return ValidationResult(
                False, ErrorCode.DATA_VALIDATION, "Missing price/volume-like columns"
            )

        # Série de prix "effective" (sans muter le DF d'entrée)
        eff_price = None
        try:
            if "price" in cols:
                eff_price = pd.to_numeric(ticks["price"], errors="coerce")
            elif {"bid", "ask"} <= cols:
                bid = pd.to_numeric(ticks["bid"], errors="coerce")
                ask = pd.to_numeric(ticks["ask"], errors="coerce")
                eff_price = (bid + ask) * 0.5
            elif "last" in cols:
                eff_price = pd.to_numeric(ticks["last"], errors="coerce")
        except Exception:
            eff_price = None

        if eff_price is None:
            return ValidationResult(
                False, ErrorCode.DATA_VALIDATION, "No usable price series"
            )

        good_price_mask = eff_price.notna() & (eff_price > 0)
        valid_rows = int(good_price_mask.sum())
        total_rows = int(len(ticks))

        if valid_rows == 0:
            return ValidationResult(
                False, ErrorCode.DATA_VALIDATION, "Detected non-positive prices"
            )

        # (Optionnel) check volume > 0 sur une colonne connue, sans bloquer si mixte
        pos_vol_rows = None
        try:
            vol_col = next(
                (c for c in ("volume", "size", "qty", "amount", "vol") if c in cols),
                None,
            )
            if vol_col is not None:
                vol_series = pd.to_numeric(ticks[vol_col], errors="coerce")
                pos_vol_rows = int((vol_series.fillna(0) > 0).sum())
        except Exception:
            pos_vol_rows = None

        return ValidationResult(
            True,
            metrics={
                "row_count": total_rows,
                "valid_price_rows": valid_rows,
                "positive_volume_rows": pos_vol_rows,
            },
        )

    @staticmethod
    def validate_bars(bars: pd.DataFrame) -> ValidationResult:
        if bars is None or bars.empty:
            return ValidationResult(True, message="Bars optional, skipping")
        cols = set(bars.columns)
        required = {"open", "high", "low", "close"}
        missing = required - cols
        if missing:
            return ValidationResult(
                False, ErrorCode.DATA_VALIDATION, f"Bars missing: {sorted(missing)}"
            )
        try:
            invalid = (
                (bars["high"] < bars["low"])
                | (bars["high"] < bars["open"])
                | (bars["high"] < bars["close"])
                | (bars["low"] > bars["open"])
                | (bars["low"] > bars["close"])
            )
            if invalid.any():
                return ValidationResult(
                    False,
                    ErrorCode.DATA_VALIDATION,
                    f"Invalid OHLC in {int(invalid.sum())} bars",
                )
        except Exception as e:
            return ValidationResult(
                False, ErrorCode.DATA_VALIDATION, f"OHLC validation failed: {e}"
            )

        vol_col = None
        for c in ("volume", "tick_volume", "tickVolume", "vol"):
            if c in cols:
                vol_col = c
                break
        return ValidationResult(
            True, metrics={"bar_count": int(len(bars)), "vol_col": vol_col}
        )


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
    def normalize_dataframe(
        cls, df: pd.DataFrame, prefer_col: str = "dt"
    ) -> pd.DataFrame:
        if df is None or df.empty:
            return df
        if pd.api.types.is_datetime64_any_dtype(df.index):
            df.index = cls.normalize_index(df.index)
        dt_cols = [
            c for c in ("dt", "datetime", "timestamp", "time") if c in df.columns
        ]
        for c in dt_cols:
            if pd.api.types.is_datetime64_any_dtype(
                df[c]
            ) or pd.api.types.is_object_dtype(df[c]):
                df[c] = cls.normalize_series(df[c])
        picked = next(
            (
                c
                for c in (prefer_col, "dt", "datetime", "timestamp", "time")
                if c in df.columns and pd.api.types.is_datetime64_any_dtype(df[c])
            ),
            None,
        )
        if picked is None and pd.api.types.is_datetime64_any_dtype(df.index):
            df["dt"] = df.index
            picked = "dt"
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
    def boost_from_metadata(
        decision: Dict[str, Any], meta: Dict[str, Any], price_step: float
    ) -> float:
        meta = meta or {}
        conf = float(decision.get("confidence", 0.7) or 0.7)
        sdir = 1 if str(decision.get("direction", "BUY")).upper() == "BUY" else -1
        dtot = float(meta.get("delta_total", 0.0) or 0.0)
        if dtot and np.sign(dtot) == sdir:
            conf = min(0.99, conf + 0.05)
        anc = float(decision.get("anchor_price", meta.get("poc", 0.0)) or 0.0)
        poc = float(meta.get("poc", 0.0) or 0.0)
        if anc and poc and abs(anc - poc) <= price_step:
            conf = max(0.55, conf - 0.03)
        return conf

    @staticmethod
    def select_best_decision(
        candidates: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
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
            key=lambda d: (
                float(d.get("confidence", 0.0)),
                prio.get(ConfidenceScorer._alias(d.get("trigger")), 0),
            ),
            reverse=True,
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
    def __init__(
        self, logger=None, metrics_monitor: Optional[PerformanceMonitor] = None
    ):
        self.logger = logger
        self.monitor = metrics_monitor or PerformanceMonitor()
        self.validator = DataValidator()
        self.normalizer = DatetimeNormalizer()
        self.scorer = ConfidenceScorer()
        self._last_signal: Dict[str, Dict[str, Any]] = {}

    # --- SOFT-QUIET-XAU: helpers ---
    def _fp_settings(self, strategy_config):
        node = (strategy_config or {}).get("logging", {})
        # modes: off | soft | verbose (par défaut: soft)
        mode = str(node.get("footprint_mode", "soft")).lower()
        enabled = set(
            map(
                str.upper,
                (strategy_config or {})
                .get("footprint", {})
                .get("enabled_assets", ["XAUUSD"]),
            )
        )
        return mode, enabled

    def _fp_can_log(self, asset_upper: str, tag: str) -> bool:
        # tag ∈ {"SUMMARY","SNAPSHOT","DETAIL"}
        if self._fp_mode == "off":
            return False
        if self._fp_mode == "soft" and tag != "SUMMARY":
            return False
        return asset_upper in self._fp_enabled

    def _fp_log(self, tag: str, fmt: str, *args, level: str = "info"):
        if not getattr(self, "logger", None):
            return
        if not self._fp_can_log(self._asset_upper, tag):
            return
        getattr(self.logger, level, self.logger.info)(fmt, *args)

    def _log_error(
        self, where: str, exc: Exception, extra: Optional[Dict[str, Any]] = None
    ):
        if not getattr(self, "logger", None):
            return
        try:
            self.logger.debug("[FP-ERR][%s] %s extra=%s", where, repr(exc), extra or {})
        except Exception:
            pass

    def _passes_hysteresis(
        self,
        asset: str,
        new_dir: str,
        new_conf: float,
        cooldown_s: int = 0,
        extra_conf: float = 0.0,
    ) -> bool:
        """
        Si cooldown_s==0 -> désactivé (comportement actuel).
        Sinon, empêche un renversement rapide de direction à confiance trop proche.
        """
        if cooldown_s <= 0:
            return True
        st = self._last_signal.get(asset.upper())
        now = time.time()
        if not st:
            return True
        if st["dir"] != new_dir and (now - st["ts"]) < cooldown_s:
            return False
        if st["dir"] != new_dir and new_conf < (st["conf"] + extra_conf):
            return False
        return True

    # ---- public -------------------------------------------------------
    def _dynamic_window_plan(
        self, ticks: pd.DataFrame, cfg: TriggerConfig, tick_count_soft: int
    ) -> list[int]:
        """
        Construit une liste de fenêtres en secondes, adaptée à la densité de ticks.
        - Si flux maigre: on ajoute 13s et 21s.
        - Si flux très dense: on privilégie les petites fenêtres d'abord.
        """
        base = list(cfg.window_candidates_s)
        try:
            # densité ~ ticks / durée observée
            if "dt" in ticks.columns:
                span_s = (ticks["dt"].max() - ticks["dt"].min()).total_seconds()
            else:
                span_s = float(len(ticks))  # fallback conservateur
            span_s = max(1.0, float(span_s))
            density = float(tick_count_soft) / span_s
        except Exception:
            density = 0.0

        # Flux maigre → on prolonge la fenêtre
        if tick_count_soft < 60 or density < 2.0:
            base += [13, 21]

        # Flux pléthorique → on s'assure que 3s est testé en priorité
        if density >= 8.0 and 3 not in base:
            base = [3] + base

        # Dé-dup + ordonnancement
        plan = sorted(set(int(x) for x in base))
        return plan

    def analyze_footprint_triggers(
        self,
        asset: str,
        ticks: pd.DataFrame,
        bars: Optional[pd.DataFrame],
        strategy_config: Dict[str, Any],
    ) -> Tuple[bool, Dict[str, Any]]:

        # --- SOFT-QUIET-XAU: gating + timers visibles partout ---
        self._asset_upper = str(asset).upper()
        self._fp_mode, self._fp_enabled = self._fp_settings(strategy_config)

        # triggers seulement sur actifs autorisés (par défaut: XAUUSD)
        if self._asset_upper not in self._fp_enabled:
            return False, {"reason": "TRIG_DISABLED_ASSET", "asset": self._asset_upper}

        t0 = time.perf_counter()  # chrono pour le résumé
        tick_count_soft = "?"  # fixé après validation

        with self.monitor.measure_phase("total"):
            # 0) pré-traitement feed-agnostic (price/volume)
            ticks = self._ensure_price_volume_columns(ticks)

            # 1) validation
            with self.monitor.measure_phase("validation"):
                vt = self.validator.validate_ticks(ticks)
                try:
                    # si vt.metrics existe, on l’utilise ; sinon fallback len(ticks)
                    tick_count_soft = int(
                        getattr(vt, "metrics", {}).get("row_count", len(ticks))
                    )
                except Exception:
                    tick_count_soft = len(ticks) if ticks is not None else "?"

                if not vt.is_valid:
                    return False, {
                        "reason": vt.message,
                        "error_code": vt.error_code.value,
                    }

                vb = self.validator.validate_bars(bars)
                if not vb.is_valid:
                    return False, {
                        "reason": vb.message,
                        "error_code": vb.error_code.value,
                    }

            # 2) config
            cfg = TriggerConfig.from_strategy_config(strategy_config or {})
            price_step = float((strategy_config or {}).get("price_step", 0.01) or 0.01)

            # 3) datetime normalization
            with self.monitor.measure_phase("datetime_norm"):
                try:
                    ticks = self.normalizer.normalize_dataframe(ticks.copy(), "dt")
                    bars = (
                        self.normalizer.normalize_dataframe(bars.copy(), "time")
                        if bars is not None
                        else None
                    )
                except Exception as e:
                    return False, {
                        "reason": f"Datetime normalization failed: {e}",
                        "error_code": ErrorCode.DATETIME_NORMALIZATION.value,
                    }
            # --- runtime feature flags depuis la strategy_config (tous optionnels) ---
            ft_node = (strategy_config or {}).get("entry_rules", {}).get(
                "scalping", {}
            ).get("burst_scalping", {}).get("footprint_triggers", {}) or {}
            profile = str(ft_node.get("profile", "balanced")).lower()
            min_votes = int(ft_node.get("min_votes", 1))  # 1 = comportement actuel
            cooldown_s = int(ft_node.get("cooldown_s", 0))  # 0 = off (actuel)
            extra_conf = float(ft_node.get("hysteresis_extra_conf", 0.05))
            spread_pts = (
                (strategy_config or {}).get("market", {}).get("spread_pts", None)
            )
            spread_max = (
                (strategy_config or {}).get("guardrails", {}).get("spread_max_pts", 999)
            )
            regime = (strategy_config or {}).get("market", {}).get("regime", None)
            tick_rate = (strategy_config or {}).get("market", {}).get("tick_rate", None)

            # simple spread gating (optionnel)
            if spread_pts is not None and spread_pts > spread_max:
                self._fp_log(
                    "SUMMARY",
                    "[TRIG][%s] none | reason=spread_too_wide (%s>%s)",
                    self._asset_upper,
                    str(spread_pts),
                    str(spread_max),
                    level="info",
                )
                return False, {
                    "reason": "TRIG_SPREAD_TOO_WIDE",
                    "diag": {"spread_pts": spread_pts, "spread_max": spread_max},
                }

            # micro-fallbacks autorisés selon régime
            allow_micro = True
            if str(regime) == "high_volatility_chaos":
                allow_micro = False

            runtime_flags = {
                "allow_micro": allow_micro,
                "cooldown_s": cooldown_s,
                "hysteresis_extra_conf": extra_conf,
                "min_votes": min_votes,
                "tick_rate": tick_rate,
            }

            # ---- plan de fenêtres adaptatif (densité ticks) ----
            window_plan = self._dynamic_window_plan(ticks, cfg, tick_count_soft)
            window_decisions: List[Dict[str, Any]] = []

            # 4) exploration multi-fenêtres / double passe
            best_decision = None
            best_meta = None
            best_win: Optional[int] = None

            for pass_type in ("normal", "soft"):
                base_params = (
                    cfg.to_dict()
                    if pass_type == "normal"
                    else {**cfg.to_dict(), **cfg.soft_params}
                )
                params_map: Dict[str, Any] = {**base_params, **runtime_flags}

                for win in window_plan:
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
                            window_decisions.append(
                                {"win": used_win, "decision": decision, "meta": meta}
                            )
                            if (best_decision is None) or (
                                float(decision.get("confidence", 0))
                                > float(best_decision.get("confidence", 0))
                            ):
                                best_decision, best_meta, best_win = (
                                    decision,
                                    meta,
                                    used_win,
                                )

                # confirmation par votes multi-fenêtres (optionnelle)
                if (
                    not best_decision
                    and int(runtime_flags.get("min_votes", 1)) > 1
                    and window_decisions
                ):
                    groups = {}
                    for wd in window_decisions:
                        d = wd["decision"]
                        alias = self.scorer._alias(d.get("trigger"))
                        key = (alias, str(d.get("direction", "")).upper())
                        groups.setdefault(key, []).append(wd)

                    chosen_rec = None
                    for key, recs in groups.items():
                        if len(recs) >= int(runtime_flags["min_votes"]):
                            # boost léger, prend la plus confiante
                            recs.sort(
                                key=lambda r: float(r["decision"].get("confidence", 0)),
                                reverse=True,
                            )
                            chosen_rec = recs[0]
                            chosen_rec["decision"]["confidence"] = min(
                                0.99, float(chosen_rec["decision"]["confidence"]) + 0.04
                            )
                            break

                    if chosen_rec:
                        best_decision = chosen_rec["decision"]
                        best_meta = chosen_rec["meta"]
                        best_win = chosen_rec["win"]

                if best_decision:
                    break

            # 5) build response / logs
            if not best_decision:
                self._fp_log(
                    "SUMMARY",
                    "[TRIG][%s] none | wins=%s | ticks=%s | dt=%.1fms",
                    self._asset_upper,
                    "/".join(map(str, window_plan)),
                    str(tick_count_soft),
                    (time.perf_counter() - t0) * 1000.0,
                    level="info",
                )
                return False, {
                    "reason": "no_trigger_detected",
                    "diag": {"wins": window_plan, "ticks": tick_count_soft},
                }

            self._fp_log(
                "SUMMARY",
                "[TRIG][%s] %s %s | conf=%.2f | win=%ss | dt=%.1fms",
                self._asset_upper,
                str(best_decision.get("trigger", "footprint")),
                str(
                    best_decision.get("direction", best_decision.get("action", ""))
                ).upper(),
                float(best_decision.get("confidence", 0.0)),
                int(best_win or cfg.window_candidates_s[0]),
                (time.perf_counter() - t0) * 1000.0,
                level="info",
            )

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
        cfg: TriggerConfig,
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
            # === PATCH: Harmonisation colonnes + métriques manquantes ===
            # On veut: vol, delta, delta_ratio, zscore_vol

            cols = {c.lower(): c for c in df_levels.columns}

            def _pick(*names):
                for n in names:
                    if n in cols:
                        return cols[n]
                return None

            # 1) volume
            vol_col = _pick("vol", "volume", "qty", "size", "amount")
            if vol_col is None:
                df_levels["vol"] = 0.0
            else:
                if vol_col != "vol":
                    df_levels.rename(columns={vol_col: "vol"}, inplace=True)

            # 2) delta
            delt_col = _pick("delta", "Δ", "delta_value", "delt")
            if delt_col is None:
                df_levels["delta"] = 0.0
            else:
                if delt_col != "delta":
                    df_levels.rename(columns={delt_col: "delta"}, inplace=True)

            # 3) delta_ratio = |delta| / max(vol, eps)
            if "delta_ratio" not in df_levels.columns:
                eps = 1e-9
                df_levels["delta_ratio"] = (df_levels["delta"].abs()) / (
                    df_levels["vol"].abs() + eps
                )

            # 4) zscore_vol (robuste) — indispensable pour l’absorption
            if "zscore_vol" not in df_levels.columns:
                v = df_levels["vol"].astype(float)
                if v.count() >= 8:
                    med = float(v.median())
                    mad = float((v - med).abs().median())
                    if mad > 1e-9:
                        df_levels["zscore_vol"] = (v - med) / (1.4826 * mad + 1e-9)
                    else:
                        std = float(v.std(ddof=0))
                        df_levels["zscore_vol"] = (v - float(v.mean())) / (std + 1e-9)
                else:
                    df_levels["zscore_vol"] = 0.0

            # 5) LOG instantané (visible seulement en verbose sur actifs autorisés)
            self._fp_log(
                "SNAPSHOT",
                "[FP-SNAPSHOT] win=%ss levels=%d vol_med=%.2f zmax=%.2f dratio_p95=%.2f dsum=%.2f",
                int(window_s),
                int(len(df_levels)),
                float(df_levels["vol"].median() if "vol" in df_levels else 0.0),
                float(
                    df_levels["zscore_vol"].max() if "zscore_vol" in df_levels else 0.0
                ),
                float(
                    df_levels["delta_ratio"].quantile(0.95)
                    if "delta_ratio" in df_levels
                    else 0.0
                ),
                float(df_levels["delta"].sum() if "delta" in df_levels else 0.0),
                level="debug",
            )

        except Exception as e:
            self._log_error("snapshot", e, {"window_s": window_s})
            return None, None, None

        # === CONTEXTE RAPIDE: tick_rate & pas des niveaux (pour micro-triggers) ===
        try:
            coverage_s = float(
                meta.get("coverage_s") or meta.get("coverage_seconds") or window_s
            )
            tick_count = int(meta.get("tick_count") or len(ticks))
            self._tick_rate_tmp = tick_count / max(1.0, coverage_s)
        except Exception:
            self._tick_rate_tmp = None

        try:
            idx = df_levels.index.values.astype(float)
            diffs = np.diff(np.unique(idx))
            self._lvl_step_tmp = (
                float(np.quantile(diffs[diffs > 0], 0.1)) if diffs.size else None
            )
        except Exception:
            self._lvl_step_tmp = None

        # 6) Adaptation contextuelle des seuils (PATCH C)
        #    - Relâchement léger si snapshot faible (zmax/dr_p95 bas)
        #    - Sans effet quand snapshot déjà fort
        dyn_params = dict(params)
        try:
            zmax = float(
                df_levels["zscore_vol"].max() if "zscore_vol" in df_levels else 0.0
            )
            dr_p95 = float(
                df_levels["delta_ratio"].quantile(0.95)
                if "delta_ratio" in df_levels
                else 0.0
            )

            # Volume anémique → absorption un peu plus permissive
            if zmax < 1.0:
                dyn_params["abs_vol_z_min"] = max(
                    0.70,
                    float(dyn_params.get("abs_vol_z_min", cfg.abs_vol_z_min)) * 0.85,
                )

            # Déséquilibres faibles → stacking un peu plus permissif
            if dr_p95 < 0.90:
                dyn_params["stack_delta_ratio_min"] = max(
                    1.05,
                    float(
                        dyn_params.get(
                            "stack_delta_ratio_min", cfg.stack_delta_ratio_min
                        )
                    )
                    * 0.90,
                )
            # Flux très dense → seuils un peu plus permissifs
            tr = getattr(self, "_tick_rate_tmp", None)
            if tr and tr >= 4.0:
                # stacking: autoriser 1 niveau de moins (ex: 3 → 2)
                dyn_params["stack_min_levels"] = max(
                    2, int(dyn_params.get("stack_min_levels", cfg.stack_min_levels)) - 1
                )
                # absorption: exiger 1 tentative de moins
                dyn_params["abs_attempts_min"] = max(
                    1, int(dyn_params.get("abs_attempts_min", cfg.abs_attempts_min)) - 1
                )

        except Exception:
            dyn_params = dict(params)

        # Booster méta : delta total (si absent)
        try:
            if meta is not None and "delta_total" not in meta:
                meta["delta_total"] = float(
                    df_levels["delta"].sum() if "delta" in df_levels else 0.0
                )
        except Exception:
            pass

        candidates: List[Dict[str, Any]] = []

        # --- 1) Climax (bars optionnelles) ---
        if bars is not None and not getattr(bars, "empty", True):
            climax_kwargs = {}
            try:
                lookback = int(
                    dyn_params.get("climax_lookback_bars", cfg.climax_lookback_bars)
                )
                bar_slice = bars.tail(max(lookback + 5, 30))  # slice perf
                climax_kwargs = dict(
                    lookback_bars=lookback,
                    vol_ratio_min=float(
                        dyn_params.get("climax_vol_ratio_min", cfg.climax_vol_ratio_min)
                    ),
                    delta_ratio_min=float(
                        dyn_params.get(
                            "climax_delta_ratio_min", cfg.climax_delta_ratio_min
                        )
                    ),
                    need_consolidation=bool(
                        dyn_params.get(
                            "climax_need_consolidation", cfg.climax_need_consolidation
                        )
                    ),
                    consolidation_max_atr_mult=float(
                        dyn_params.get("climax_cons_atr_max", cfg.climax_cons_atr_max)
                    ),
                )
                d1 = detect_volume_climax_after_consolidation(
                    bar_slice, df_levels, **climax_kwargs
                )
                if d1.get("ok"):
                    candidates.append(d1)
            except Exception as e:
                self._log_error(
                    "climax_detection",
                    e,
                    {"kwargs": climax_kwargs, "window_s": window_s},
                )

        # --- 2) Stacking ---
        stack_kwargs = {}
        try:
            stack_kwargs = dict(
                delta_ratio_min=float(
                    dyn_params.get("stack_delta_ratio_min", cfg.stack_delta_ratio_min)
                ),
                min_levels=int(
                    dyn_params.get("stack_min_levels", cfg.stack_min_levels)
                ),
                invalidate_opposite_ratio=float(
                    dyn_params.get(
                        "stack_invalidate_opp_ratio", cfg.stack_invalidate_opp_ratio
                    )
                ),
                vol_level_min_ratio_median_30s=float(
                    dyn_params.get("stack_vol_lvl_min_med", cfg.stack_vol_lvl_min_med)
                ),
            )
            d2 = detect_imbalance_stacking(df_levels, **stack_kwargs)
            if d2.get("ok"):
                candidates.append(d2)

            else:
                try:
                    self._fp_log(
                        "DETAIL",
                        "[FP-STACK] no trigger | win=%ss | reason=%s | stats={levels:%s, dr_mean:%.3f}",
                        int(window_s),
                        d2.get("reason", "thresholds_not_met"),
                        d2.get("meta", {}).get("levels"),
                        float((d2.get("meta", {}).get("delta_ratio_mean") or 0.0)),
                        level="debug",
                    )

                except Exception:
                    pass

        except Exception as e:
            self._log_error(
                "stacking_detection", e, {"kwargs": stack_kwargs, "window_s": window_s}
            )

        # --- 3) Absorption ---
        abs_kwargs = {}
        try:
            abs_kwargs = dict(
                vol_zscore_min=float(
                    dyn_params.get("abs_vol_z_min", cfg.abs_vol_z_min)
                ),
                delta_ratio_max=float(
                    dyn_params.get("abs_delta_ratio_max", cfg.abs_delta_ratio_max)
                ),
                attempts_min=int(
                    dyn_params.get("abs_attempts_min", cfg.abs_attempts_min)
                ),
            )
            d3 = detect_absorption_reject(df_levels, **abs_kwargs)
            if d3.get("ok"):
                candidates.append(d3)
        except Exception as e:
            self._log_error(
                "absorption_detection", e, {"kwargs": abs_kwargs, "window_s": window_s}
            )

        # --- 4) Fallbacks micro si rien (optionnels selon régime) ---
        allow_micro = bool(params.get("allow_micro", True))
        self._tick_rate_tmp = params.get("tick_rate", None)  # pour micro_absorption
        if not candidates and allow_micro:

            cols = set(df_levels.columns)
            if {"vol", "delta", "delta_ratio"}.issubset(cols):
                fb1 = self._micro_stacking(df_levels)
                if fb1:
                    candidates.append(fb1)
            if {"zscore_vol", "delta_ratio", "delta"}.issubset(cols):
                fb2 = self._micro_absorption(df_levels)
                if fb2:
                    candidates.append(fb2)

                # 3) micro_burst si toujours rien et tick_rate élevé
            if not candidates:
                fb3 = self._micro_burst(df_levels, meta or {})
                if fb3:
                    candidates.append(fb3)

        if not candidates:
            self._fp_log(
                "NO_CAND",
                "[FP-NO-CAND] win=%ss | zmax=%.2f dr_p95=%.2f dr_p90=%.2f dr_mean=%.2f vol_med=%.2f dsum=%.2f",
                int(window_s),
                float(df_levels["zscore_vol"].max()),
                float(df_levels["delta_ratio"].quantile(0.95)),
                float(df_levels["delta_ratio"].quantile(0.90)),
                float(df_levels["delta_ratio"].mean()),
                float(df_levels["vol"].median()),
                float(df_levels["delta"].sum()),
                level="debug",
            )
            return None, meta, window_s

        # --- boost confiance & sélection ---
        for d in candidates:
            d["confidence"] = self.scorer.boost_from_metadata(d, meta, price_step)

        best = self.scorer.select_best_decision(candidates)
        if not best:
            return None, meta, window_s

        # Ajustement "ancre vs POC" (petite pénalité/bonus)
        try:
            poc = float(meta.get("poc", 0.0) if meta else 0.0)
            anc = float(best.get("anchor_price", poc))
            if best.get("direction") == "BUY" and anc < (poc - price_step):
                best["confidence"] = max(0.0, float(best.get("confidence", 0)) - 0.05)
            elif best.get("direction") == "SELL" and anc > (poc + price_step):
                best["confidence"] = max(0.0, float(best.get("confidence", 0)) - 0.05)
            else:
                if abs(anc - poc) >= 2 * price_step:
                    best["confidence"] = min(
                        0.99, float(best.get("confidence", 0)) + 0.03
                    )
        except Exception:
            pass

        # Hysteresis directionnel (optionnel)
        cooldown_s = int(params.get("cooldown_s", 0))
        extra_hyst = float(params.get("hysteresis_extra_conf", 0.05))
        if not self._passes_hysteresis(
            self._asset_upper,
            str(best.get("direction", "")).upper(),
            float(best.get("confidence", 0)),
            cooldown_s,
            extra_hyst,
        ):
            return None, meta, window_s

        # enrichit meta + retourne
        try:
            best["meta"] = {**(best.get("meta") or {}), "used_window_s": int(window_s)}
            # MàJ état hysteresis
            self._last_signal[self._asset_upper] = {
                "ts": time.time(),
                "dir": str(best.get("direction", "")).upper(),
                "conf": float(best.get("confidence", 0)),
            }
            return best, meta, window_s
        except Exception:
            return best, meta, window_s

        except Exception:
            pass

    def _build_trigger_response(
        self,
        asset: str,
        decision: Dict[str, Any],
        meta: Dict[str, Any],
        window_used: int,
    ) -> Dict[str, Any]:
        meta = meta or {}
        direction = str(decision.get("direction", "BUY")).upper()
        return TriggerDecision(
            action="BUY" if direction == "BUY" else "SELL",
            asset=asset,
            trigger=decision.get("trigger", "footprint"),
            confidence=float(decision.get("confidence", 0.7) or 0.7),
            anchor_price=float(decision.get("anchor_price", meta.get("poc", 0.0))),
            meta={
                **meta,
                **(decision.get("meta", {}) or {}),
                "window_used": int(window_used),
            },
        ).to_dict()

    # ------------------- fallbacks micro -------------------

    def _micro_stacking(self, df_levels: pd.DataFrame) -> Optional[Dict[str, Any]]:
        """
        Détection de micro-stacking :
        - 2–4 niveaux adjacents (tolère 1 gap contrôlé) avec même signe
        - delta_ratio >= seuil dynamique (base 1.20 ; 1.10 si flux dense/vol élevé)
        - filtres anti-faux signaux (opposé fort à proximité, volume trop faible, etc.)
        Confiance = base + bonus (run, delta_ratio moyen, zscore, tick_rate) – pénalités (opposé proche).
        """
        try:
            lv = df_levels
            req = {"vol", "delta", "delta_ratio"}
            if lv is None or lv.empty or any(c not in lv.columns for c in req):
                return None

            # Nettoyage + tri
            lv = lv[lv["vol"].astype(float) > 0].sort_index()
            if lv.empty:
                return None

            # Index prix normalisé
            prices = pd.to_numeric(pd.Index(lv.index), errors="coerce").astype(float)
            mask_ok = np.isfinite(prices)
            if not bool(mask_ok.all()):
                lv = lv.loc[mask_ok]
                prices = prices[mask_ok]
            if lv.empty:
                return None

            # Colonnes en np.arrays
            delta = lv["delta"].astype(float).values
            sign = np.sign(delta)
            ratio = lv["delta_ratio"].astype(float).values
            zscore = (
                lv["zscore_vol"].astype(float).values
                if "zscore_vol" in lv.columns
                else np.zeros(len(lv))
            )
            vol = lv["vol"].astype(float).values

            # Step (pas) robuste: priorité à _lvl_step_tmp sinon quantile sur diffs
            step = float(getattr(self, "_lvl_step_tmp", 0.0) or 0.0)
            diffs = np.diff(np.unique(prices))
            diffs = diffs[diffs > 0]
            if diffs.size:
                step = float(step) if step > 0 else float(np.quantile(diffs, 0.20))
            else:
                step = float(step)  # peut rester 0.0

            # Contexte runtime
            tr = float(getattr(self, "_tick_rate_tmp", 0.0) or 0.0)  # ticks/s
            zmed_global = float(np.median(zscore)) if zscore.size else 0.0
            zmax_global = float(np.max(zscore)) if zscore.size else 0.0

            # Seuils dynamiques
            dr_base = 1.20
            if tr >= 6.0 or zmax_global >= 2.0:
                dr_base = 1.10
            min_levels = 3
            if tr >= 5.0 or zmed_global >= 1.2:
                min_levels = 2  # autorise stacks plus courts si flux dense / vol fort

            gap_mult = 1.6  # tolérance d'écart inter-niveaux
            opp_inval_ratio = 0.60  # invalide si opposé >= 0.60 tout près

            vmed = float(np.median(vol)) if vol.size else 0.0

            i = 0
            n = len(prices)
            while i < n:
                # point de départ valide ?
                if sign[i] == 0 or ratio[i] < dr_base:
                    i += 1
                    continue

                sgn = int(sign[i])
                j = i + 1
                run = 1
                gaps = 0
                end_idx = i

                while j < n:
                    if sign[j] != sgn or ratio[j] < dr_base:
                        break
                    if step > 0 and (prices[j] - prices[j - 1]) > gap_mult * step:
                        gaps += 1
                        if gaps > 1:
                            break
                    run += 1
                    end_idx = j
                    j += 1

                if run >= min_levels:
                    # métriques locales sur le stack détecté
                    seg = slice(i, end_idx + 1)
                    dr_mean = float(np.mean(ratio[seg]))
                    zmed_loc = float(np.median(zscore[seg])) if zscore.size else 0.0
                    v_sum = float(np.sum(vol[seg]))

                    # ajustements locaux des seuils quand zscore très bas
                    need_levels = 3 if zmed_loc < 0.8 else min_levels
                    need_ratio = 1.15 if zmed_loc < 0.8 else dr_base

                    if run < need_levels or dr_mean < need_ratio:
                        i = j
                        continue

                    # garde-fou volume: somme du segment vs médiane*run
                    if vmed > 0 and v_sum < 0.7 * vmed * run:
                        i = j
                        continue

                    # Invalidation par opposé fort à proximité (avant/après le bloc)
                    def _opposite_strong(k_from: int, k_to: int) -> bool:
                        if k_from < 0 or k_to >= n:
                            return False
                        k = k_from if k_from >= 0 and k_from < n else k_to
                        return (
                            (sign[k] == -sgn)
                            and (ratio[k] >= opp_inval_ratio)
                            and (
                                step == 0.0
                                or (
                                    abs(
                                        prices[k]
                                        - prices[k_from if k == k_to else k_to]
                                    )
                                    <= 1.4 * max(step, 1e-12)
                                )
                            )
                        )

                    opp_near = _opposite_strong(i - 1, i) or _opposite_strong(
                        end_idx + 1, end_idx
                    )
                    if opp_near:
                        i = j
                        continue

                    # Direction + ancre (fin du stack dans le sens du sgn)
                    direction = "BUY" if sgn > 0 else "SELL"
                    anchor = float(prices[end_idx])

                    # Confiance
                    conf = 0.56
                    # + run (niv. au-delà de 2)
                    conf += 0.06 * float(np.clip(run - 2, 0, 3))
                    # + delta_ratio moyen au-dessus du seuil
                    conf += 0.08 * float(
                        np.clip((dr_mean - need_ratio) / 0.30, 0.0, 1.5)
                    )
                    # + zscore local
                    conf += 0.06 * float(np.clip((zmed_loc - 1.0) / 1.0, 0.0, 1.0))
                    # + tick_rate
                    conf += 0.06 * float(np.clip((tr - 3.0) / 5.0, 0.0, 1.0))
                    # pénalité si opposé non fort mais présent dans un rayon court
                    near_opp_soft = False
                    if step > 0:
                        # cherche un opposé faible juste au bord
                        klist = [i - 1, end_idx + 1]
                        for k in klist:
                            if (
                                0 <= k < n
                                and sign[k] == -sgn
                                and (
                                    prices[min(max(k, 0), n - 1)]
                                    - prices[end_idx if k > end_idx else i]
                                )
                                <= 1.8 * step
                            ):
                                near_opp_soft = True
                                break
                    if near_opp_soft:
                        conf -= 0.03

                    conf = float(np.clip(conf, 0.56, 0.90))

                    # Log diag
                    if hasattr(self, "_fp_log"):
                        self._fp_log(
                            "DETAIL",
                            (
                                f"[MICRO_STACK] dir={direction} conf={conf:.3f} "
                                f"levels={run} gaps={gaps} dr_mean={dr_mean:.2f} "
                                f"zmed_loc={zmed_loc:.2f} tr={tr:.2f} step={step:.5f} anchor={anchor}"
                            ),
                        )

                    # Trigger
                    try:
                        trig = TriggerType.MICRO_STACK.value
                    except Exception:
                        trig = "MICRO_STACK"

                    return {
                        "ok": True,
                        "trigger": trig,
                        "direction": direction,
                        "confidence": conf,
                        "anchor_price": anchor,
                        "meta": {
                            "levels": int(run),
                            "gaps_used": int(gaps),
                            "delta_ratio_mean": float(dr_mean),
                            "zscore_median_local": float(zmed_loc),
                            "tick_rate": tr,
                            "step_used": float(step),
                            "volume_sum": v_sum,
                            "volume_median": float(vmed),
                        },
                    }

                # avancer
                i = j

            return None

        except Exception as e:
            if hasattr(self, "_fp_log"):
                self._fp_log("ERROR", f"[MICRO_STACK] exception: {e!r}")
            return None

    def _micro_absorption(
        self, df_levels: pd.DataFrame, meta: Optional[Dict[str, Any]] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Détecte une micro-absorption:
        - niveau candidat: zscore_vol >= z_need && delta_ratio <= dr_max
        - voisin immédiat: signe delta opposé ET delta_ratio >= dr_op_min
        - proximité: |voisin - candidat| <= step * prox_mult
        - volume voisin: >= 0.7 * median(vol) si dispo
        Seuils dynamiques selon tick_rate.
        """
        try:
            lv = df_levels
            req = {"zscore_vol", "delta_ratio", "delta"}
            if any(c not in lv.columns for c in req):
                return None

            # --- Tri + nettoyage chiffres ---
            lv = lv.sort_index()
            # On tolère les index non-numériques en les convertissant (drop si NaN)
            prices = pd.to_numeric(pd.Index(lv.index), errors="coerce").astype(float)
            mask_ok = np.isfinite(prices)
            if not bool(mask_ok.all()):
                lv = lv.loc[mask_ok]
                prices = prices[mask_ok]
            if lv.empty:
                return None

            # --- step (pas de niveau) robuste ---
            step_cfg = float(getattr(self, "_lvl_step_tmp", 0.0) or 0.0)
            if step_cfg <= 0.0:
                diffs = np.diff(np.unique(prices))
                diffs = diffs[diffs > 0]
                step = float(np.quantile(diffs, 0.2)) if diffs.size else 0.0
            else:
                step = float(step_cfg)

            # --- contexte runtime ---
            tr = float(getattr(self, "_tick_rate_tmp", 0.0) or 0.0)

            # Seuils dynamiques (plus souples en burst)
            if tr < 1.0:
                z_need = 1.30
                dr_max = 0.55
                dr_op_min = 0.65
                prox_mult = 1.1
            elif tr < 4.0:
                z_need = 1.15
                dr_max = 0.60
                dr_op_min = 0.60
                prox_mult = 1.2
            elif tr < 6.0:
                z_need = 1.10
                dr_max = 0.65
                dr_op_min = 0.58
                prox_mult = 1.3
            else:
                z_need = 1.05  # burst élevé → z requis un peu plus bas
                dr_max = 0.70
                dr_op_min = 0.55  # voisin “clair” un peu assoupli
                prox_mult = 1.4

            # --- présélection des candidats (niveau "absorbé") ---
            cand = lv[(lv["zscore_vol"] >= z_need) & (lv["delta_ratio"] <= dr_max)]
            if cand.empty:
                return None
            cand = cand.sort_values("zscore_vol", ascending=False)

            # Pré-calculs utiles
            has_vol = "vol" in lv.columns
            v_med = float(lv["vol"].median()) if has_vol else 0.0
            # Pour retrouver l'index rapidement
            pos_map = {float(p): i for i, p in enumerate(prices)}

            for price, row in cand.iterrows():
                p = float(price)
                i = pos_map.get(p, None)
                if i is None:
                    # fallback si doublons: get_loc peut renvoyer un slice → on tente le 1er
                    try:
                        loc = lv.index.get_loc(price)
                        i = loc.start if isinstance(loc, slice) else int(loc)
                    except Exception:
                        continue

                # voisins gauche/droite
                neighbors_idx = []
                if i > 0:
                    neighbors_idx.append(i - 1)
                if i + 1 < len(lv):
                    neighbors_idx.append(i + 1)

                for j in neighbors_idx:
                    nb = lv.iloc[j]
                    nb_price = float(prices[j])
                    nb_delta = float(nb["delta"])

                    # Opposé clair en delta + ratio min sur le voisin
                    if nb_delta == 0.0:
                        continue
                    if np.sign(nb_delta) == np.sign(float(row["delta"])):
                        continue
                    if float(nb["delta_ratio"]) < dr_op_min:
                        continue

                    # Proximité en pas de niveau
                    if step > 0.0:
                        if abs(nb_price - p) > step * prox_mult:
                            continue

                    # Volume voisin décent
                    if has_vol and v_med > 0.0:
                        if float(nb["vol"]) < 0.7 * v_med:
                            continue

                    # Vérification finale du z-score (cohérente avec z_need)
                    if float(row["zscore_vol"]) < z_need:
                        continue

                    # --- Construction de la décision ---
                    direction = "BUY" if nb_delta > 0 else "SELL"

                    # Confiance: contributions (zscore, force du voisin, proximité, tick_rate)
                    z_bonus = (
                        np.clip((float(row["zscore_vol"]) - z_need) / 0.5, 0.0, 1.5)
                        * 0.08
                    )
                    dr_bonus = (
                        np.clip((float(nb["delta_ratio"]) - dr_op_min) / 0.4, 0.0, 1.0)
                        * 0.07
                    )
                    tr_bonus = np.clip((tr - 4.0) / 3.0, 0.0, 1.0) * 0.06
                    prox_bonus = 0.0
                    if step > 0.0:
                        d_steps = abs(nb_price - p) / step
                        # plus c'est proche (<1.0–1.4 step), plus on bonifie
                        prox_bonus = np.clip(1.4 - d_steps, 0.0, 1.0) * 0.05

                    conf = 0.60 + z_bonus + dr_bonus + tr_bonus + prox_bonus
                    conf = float(np.clip(conf, 0.58, 0.90))

                    # TriggerType robuste
                    try:
                        trig = TriggerType.MICRO_ABSORPTION.value
                    except Exception:
                        trig = "MICRO_ABSORPTION"

                    # Log diagnostique (si dispo)
                    if hasattr(self, "_fp_log"):
                        self._fp_log(
                            "DETAIL",
                            (
                                f"[MICRO_ABS] dir={direction} conf={conf:.3f} "
                                f"z={float(row['zscore_vol']):.2f} dr_nb={float(nb['delta_ratio']):.2f} "
                                f"tick_rate={tr:.2f} step={step:.5f} "
                                f"price={p} anchor={nb_price}"
                            ),
                        )

                    return {
                        "ok": True,
                        "trigger": trig,
                        "direction": direction,
                        "confidence": conf,
                        "anchor_price": float(nb_price),
                        "meta": {
                            "absorbed_level": float(p),
                            "absorbed_zscore": float(row["zscore_vol"]),
                            "neighbor_delta_ratio": float(nb["delta_ratio"]),
                            "tick_rate": tr,
                            "step_used": step,
                        },
                    }

            return None
        except Exception as e:
            # Optionnel: tracer l'exception en détail pour débogage silencieux
            if hasattr(self, "_fp_log"):
                self._fp_log("ERROR", f"[MICRO_ABS] exception: {e!r}")
            return None

    def _micro_burst(
        self, df_levels: pd.DataFrame, meta: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """
        Fallback 'burst' quand le flux est très dense et cohérent.
        Améliorations:
        - Seuils dynamiques avec tick_rate, couverture et cohérence des deltas.
        - Direction robuste: delta_total puis fallback sur déséquilibre (imbalance_buy/sell).
        - Ancre prioritaire: max delta signé DANS le haut quantile de zscore_vol; fallback POC; fallback VWAP(levels).
        - Confiance = base + bonus (tick_rate, zmax, cohérence directionnelle, ratio |Δtot| / Σ|Δ|) – pénalités (couverture trop longue, absorption).
        """
        try:
            lv = df_levels
            if lv is None or lv.empty:
                return None

            # --- index prix → float propre ---
            lv = lv.sort_index()
            prices = pd.to_numeric(pd.Index(lv.index), errors="coerce").astype(float)
            mask_ok = np.isfinite(prices)
            if not bool(mask_ok.all()):
                lv = lv.loc[mask_ok]
                prices = prices[mask_ok]
            if lv.empty:
                return None

            # --- contexte runtime ---
            tr = float(getattr(self, "_tick_rate_tmp", 0.0) or 0.0)
            tick_count = int(meta.get("tick_count", meta.get("tickrate_count", 0)) or 0)
            coverage_s = float(
                meta.get("coverage_s", meta.get("coverage_seconds", 0.0)) or 0.0
            )

            # Seuils: on déclenche en "vrai burst" OU "semi-burst" si zmax est élevé
            zmax = float(lv["zscore_vol"].max() if "zscore_vol" in lv.columns else 0.0)
            tr_min = 4.0 if zmax < 2.0 else 3.5
            if tr < tr_min:
                return None

            # Ratio densité (ticks/seconde) si info disponible
            # (si non dispo, on s'en remet à tr)
            dense_ok = True
            if tick_count > 0 and coverage_s > 0:
                dens = tick_count / max(coverage_s, 1e-9)
                dense_ok = dens >= max(3.0, 0.75 * tr_min)
            if not dense_ok:
                return None

            # --- direction agrégée ---
            dtot = None
            if "delta" in lv.columns:
                dtot = float(meta.get("delta_total", lv["delta"].sum()))
            else:
                dtot = float(meta.get("delta_total", 0.0))

            sgn: Optional[int] = None
            if dtot and dtot != 0.0:
                sgn = 1 if dtot > 0 else -1
            else:
                # Fallback: utiliser les déséquilibres s'ils existent
                ib, is_ = meta.get("imbalance_buy"), meta.get("imbalance_sell")
                if isinstance(ib, (int, float)) and isinstance(is_, (int, float)):
                    diff = float(ib) - float(is_)
                    if abs(diff) >= 2:
                        sgn = 1 if diff > 0 else -1
            if sgn is None:
                return None

            # --- métriques de cohérence directionnelle ---
            abs_sum = None
            ratio_dir = 0.5
            dtot_ratio = 0.0
            if "delta" in lv.columns:
                deltas = lv["delta"].astype(float)
                abs_sum = float(np.sum(np.abs(deltas))) or 0.0
                if abs_sum > 0.0:
                    dtot_ratio = float(abs(dtot)) / abs_sum  # 0..1
                dir_mask = (sgn * deltas) > 0
                ratio_dir = float(np.mean(dir_mask)) if len(dir_mask) else 0.5

            # --- step (pas de niveau) utile pour logs/diag (pas de filtre dur ici) ---
            diffs = np.diff(np.unique(prices))
            diffs = diffs[diffs > 0]
            step = float(np.quantile(diffs, 0.2)) if diffs.size else 0.0

            # --- sélection de l'ancre ---
            anchor = None
            if "delta" in lv.columns and len(lv) > 0:
                # privilégie les niveaux dans le haut quantile de zscore_vol
                if "zscore_vol" in lv.columns:
                    q = float(lv["zscore_vol"].quantile(0.6))
                    pool = lv[lv["zscore_vol"] >= q]
                    if pool.empty:
                        pool = lv
                else:
                    pool = lv

                # niveau de delta signé max dans la direction (sgn * delta max)
                pool_signed_strength = sgn * pool["delta"].astype(float)
                idx = pool_signed_strength.idxmax()
                try:
                    anchor = float(idx)
                except Exception:
                    anchor = None

            # Fallback: POC → sinon VWAP (sur levels)
            if anchor is None:
                poc = float(meta.get("poc", 0.0) or 0.0)
                anchor = poc if np.isfinite(poc) and poc != 0.0 else None
            if anchor is None:
                if "vol" in lv.columns:
                    vol = lv["vol"].astype(float).values
                    if np.sum(vol) > 0:
                        anchor = float(np.sum(prices * vol) / np.sum(vol))
            if anchor is None or not np.isfinite(anchor):
                return None

            # --- calcul de la confiance ---
            # Base
            conf = 0.58

            # + bonus tick_rate (max vers ~8/s)
            conf += 0.10 * float(
                np.clip((tr - tr_min) / max(1e-9, (8.0 - tr_min)), 0.0, 1.0)
            )
            # + bonus zmax (au-delà de 1.2)
            conf += 0.07 * float(np.clip((zmax - 1.2) / 1.0, 0.0, 1.5))
            # + bonus cohérence directionnelle (>=0.5 neutre)
            conf += 0.08 * float(np.clip(ratio_dir - 0.5, 0.0, 0.5) * 2.0)
            # + bonus intensité agrégée
            conf += 0.08 * float(np.clip(dtot_ratio, 0.0, 1.0))

            # Pénalités: couverture trop longue ou absorption détectée côté opposé
            if coverage_s > 25.0:
                conf -= 0.03
            if bool(meta.get("absorption_flag", False)):
                conf -= 0.04

            conf = float(np.clip(conf, 0.58, 0.88))

            # Trigger type robuste
            try:
                trig = TriggerType.MICRO_BURST.value
            except Exception:
                try:
                    trig = TriggerType.MICRO_STACK.value
                except Exception:
                    trig = "MICRO_BURST"

            # Log diag
            if hasattr(self, "_fp_log"):
                self._fp_log(
                    "DETAIL",
                    (
                        f"[MICRO_BURST] dir={'BUY' if sgn>0 else 'SELL'} conf={conf:.3f} "
                        f"tick_rate={tr:.2f} zmax={zmax:.2f} ratio_dir={ratio_dir:.2f} "
                        f"dtot_ratio={dtot_ratio:.2f} ticks={tick_count} cov={coverage_s:.1f}s "
                        f"step={step:.5f} anchor={anchor}"
                    ),
                )

            return {
                "ok": True,
                "trigger": trig,
                "direction": "BUY" if sgn > 0 else "SELL",
                "confidence": conf,
                "anchor_price": float(anchor),
                "meta": {
                    "reason": "micro_burst_dense_flow",
                    "tick_rate": tr,
                    "zmax": zmax,
                    "delta_total": float(dtot),
                    "dtot_ratio": dtot_ratio,
                    "ratio_dir": ratio_dir,
                    "tick_count": tick_count,
                    "coverage_s": coverage_s,
                    "step_used": step,
                },
            }

        except Exception as e:
            if hasattr(self, "_fp_log"):
                self._fp_log("ERROR", f"[MICRO_BURST] exception: {e!r}")
            return None

    # ------------------- helpers -------------------

    def _ensure_price_volume_columns(self, ticks: pd.DataFrame) -> pd.DataFrame:
        """
        Normalisation robuste du flux ticks :
        - Crée/corrige la colonne 'price' même si une 'price' exotique existe mais contient des valeurs <= 0.
        - Sources de vérité, par ordre de priorité : mid(bid,ask) → last → price existante → bid → ask.
        - Remplissages ciblés (ffill/bfill) puis purge des lignes non-positives.
        - Aligne 'volume' depuis une colonne volume-like si besoin.
        """
        if ticks is None or ticks.empty:
            return ticks

        df = ticks.copy()

        # --- Prépare les champs disponibles ---
        cols = set(df.columns)
        has_bid = "bid" in cols
        has_ask = "ask" in cols
        has_bid_ask = has_bid and has_ask
        has_last = "last" in cols
        has_price = "price" in cols

        # Cast numériques sans lever d'exception
        def _to_num(s):
            try:
                return pd.to_numeric(s, errors="coerce")
            except Exception:
                return s

        mid = None
        if has_bid:
            df["bid"] = _to_num(df["bid"])
        if has_ask:
            df["ask"] = _to_num(df["ask"])
        if has_bid_ask:
            mid = (df["bid"] + df["ask"]) * 0.5

        if has_last:
            df["last"] = _to_num(df["last"])

        if has_price:
            df["price"] = _to_num(df["price"])

        # --- Construction / réparation de 'price' ---
        if "price" not in df.columns:
            # Crée 'price' à partir des meilleures sources
            if has_bid_ask:
                df["price"] = mid
            elif has_last:
                df["price"] = df["last"]
            elif has_bid:
                df["price"] = df["bid"]
            elif has_ask:
                df["price"] = df["ask"]
            else:
                # Pas de source fiable → on crée vide (le validateur gèrera)
                df["price"] = np.nan
        else:
            # Répare la 'price' existante si <= 0 ou NaN
            bad_mask = ~np.isfinite(df["price"]) | (df["price"] <= 0)
            if bad_mask.any():
                # 1) remplace d'abord par mid si dispo
                if has_bid_ask:
                    df.loc[bad_mask, "price"] = mid[bad_mask]
                    bad_mask = ~np.isfinite(df["price"]) | (df["price"] <= 0)
                # 2) sinon par 'last' si dispo
                if bad_mask.any() and has_last:
                    df.loc[bad_mask, "price"] = df["last"][bad_mask]
                    bad_mask = ~np.isfinite(df["price"]) | (df["price"] <= 0)
                # 3) sinon par bid/ask si dispo
                if bad_mask.any() and has_bid:
                    df.loc[bad_mask, "price"] = df["bid"][bad_mask]
                    bad_mask = ~np.isfinite(df["price"]) | (df["price"] <= 0)
                if bad_mask.any() and has_ask:
                    df.loc[bad_mask, "price"] = df["ask"][bad_mask]
                    bad_mask = ~np.isfinite(df["price"]) | (df["price"] <= 0)
                # 4) ffill/bfill comme dernier recours (utile si flux sporadique)
                if bad_mask.any():
                    df["price"] = df["price"].ffill().bfill()

        # --- Filtre final: supprimer les lignes au prix non-positif ---
        df = df[pd.to_numeric(df["price"], errors="coerce") > 0].copy()

        # --- Volume: crée 'volume' si absent depuis les colonnes connues ---
        if "volume" not in df.columns:
            for alt in ("volume", "size", "qty", "amount", "vol"):
                if alt in df.columns:
                    df["volume"] = _to_num(df[alt])
                    break
            if "volume" not in df.columns:
                # On laisse sans volume (le validateur pourra refuser si vraiment nécessaire)
                df["volume"] = np.nan

        return df


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
    ok, result = analyzer.analyze_footprint_triggers(
        asset, ticks, bars, strategy_config
    )
    return ok, result
