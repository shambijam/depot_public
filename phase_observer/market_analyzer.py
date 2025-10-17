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
        Retourne (ok: bool, decision: dict) pour scalping burst footprint.

        Version robuste :
        - Normalisation DATETIME unique (tz-aware -> naive UTC).
        - Résolution de config tolérante (chemins multiples + fallback instance).
        - Garde-fous flux paramétrables (tickrate/couverture/phase) désactivables par conf.
        - Triggers en cascade (climax -> stacking -> absorption) sans fallback de trade.
        - Entrée ONE_PRICE (burst synchrone) + sortie TRALING_ONLY panier (close_all_at_once).

        Conf pour "zéro garde-fou" (exemple):
            footprint_triggers:
            enabled: true
            tickrate_min: 0
            coverage_s_min_burst: 0
            phase_whitelist: []
            allow_no_clear_phase_if_strong:
                of_delta_abs_min: 0
                tickrate_min: 0
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

        # -------- helpers --------
        def _deep_find_footprint_cfg(d):
            """Retourne (cfg, path_str) en cherchant footprint_triggers partout."""
            stack = [([], d)]
            while stack:
                path, node = stack.pop()
                if isinstance(node, dict):
                    if "footprint_triggers" in node and isinstance(
                        node["footprint_triggers"], dict
                    ):
                        return node["footprint_triggers"], " → ".join(
                            path + ["footprint_triggers"]
                        )
                    for k, v in node.items():
                        stack.append((path + [str(k)], v))
            return {}, ""

        def _get_phase_from_bars(bdf: "pd.DataFrame") -> "str|None":
            try:
                if (
                    bdf is not None
                    and "phase" in bdf.columns
                    and len(bdf["phase"].dropna()) > 0
                ):
                    return str(bdf["phase"].dropna().iloc[-1])
            except Exception:
                pass
            return None

        # --- NORMALISATION DATETIME (tz-aware -> naive UTC) ---------------------------
        def _to_naive_utc_series(s: pd.Series) -> pd.Series:
            s = pd.to_datetime(s, errors="coerce")  # conserve tz si présent
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
            dt_cols = [
                c for c in ("dt", "datetime", "timestamp", "time") if c in df.columns
            ]
            for c in dt_cols:
                if pd.api.types.is_datetime64_any_dtype(
                    df[c]
                ) or pd.api.types.is_object_dtype(df[c]):
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

        # -------- config resolution --------
        try:
            cfg_fp = (
                (strategy_config or {})
                .get("entry_rules", {})
                .get("scalping", {})
                .get("burst_scalping", {})
                .get("footprint_triggers", {})
            )
            cfg_path = "entry_rules → scalping → burst_scalping → footprint_triggers"

            if not cfg_fp:
                # fallback #1: attribut d'instance
                cfg_fp = getattr(self, "footprint_triggers", {}) or {}
                cfg_path = "self.footprint_triggers"

            if not cfg_fp:
                # fallback #2: recherche profonde
                cfg_fp, cfg_path = _deep_find_footprint_cfg(strategy_config or {})

            if not cfg_fp or not bool(cfg_fp.get("enabled", False)):
                return False, {
                    "reason": f"footprint_triggers disabled (cfg not found/enabled at '{cfg_path or 'N/A'}')"
                }
        except Exception as e:
            return False, {"reason": f"config error: {e}"}

        _log("info", f"[FP CFG] path='{cfg_path}' keys={list(cfg_fp.keys())}")

        # -------- normalisation unique --------
        try:
            ticks = _normalize_dt_df(ticks, prefer_col="dt")
            bars = _normalize_dt_df(
                bars, prefer_col="time"
            )  # adapter à ton schéma si besoin
        except Exception as e:
            return False, {"reason": f"datetime normalization error: {e}"}

        # -------- lecture des paramètres --------
        sc = strategy_config or {}
        price_step = float(sc.get("price_step", 0.1))

        order_block = (
            sc.get("entry_rules", {}).get("scalping", {}).get("burst_scalping", {})
            or {}
        )
        burst_count = max(1, int(order_block.get("burst_size", 5)))
        burst_count = min(burst_count, int(cfg_fp.get("max_burst_size", 10) or 10))

        order_entry_style = str(cfg_fp.get("entry_style", "LIMIT_FOK")).upper()
        order_validity_ms = int(cfg_fp.get("validity_ms", 800))
        price_offset_ticks = float(cfg_fp.get("price_offset_ticks", 0.0))

        # hygiène marché (paramétrables, 0 => désactivés)
        tickrate_min = float(cfg_fp.get("tickrate_min", 0.0))
        coverage_s_min = float(cfg_fp.get("coverage_s_min_burst", 0.0))
        phase_whitelist = list(cfg_fp.get("phase_whitelist", []))
        allow = cfg_fp.get("allow_no_clear_phase_if_strong", {}) or {}
        allow_delta_abs_min = float(allow.get("of_delta_abs_min", 0.0))
        allow_tickrate_min = float(allow.get("tickrate_min", tickrate_min))

        # -------- snapshot footprint --------
        try:
            df_levels, meta = _compute_footprint_snapshot(
                ticks, price_step=price_step, window_s=int(cfg_fp.get("window_s", 5))
            )
            if df_levels is None or df_levels.empty:
                return False, {"reason": "no footprint levels"}
        except Exception as e:
            return False, {"reason": f"footprint snapshot error: {e}"}

        # -------- filtres de flux --------
        tr = float(meta.get("tick_rate", 0.0))
        cov = float(meta.get("coverage_s", 0.0))
        if tickrate_min > 0 and tr < tickrate_min:
            return False, {"reason": f"tickrate too low: {tr}/{tickrate_min}s"}
        if coverage_s_min > 0 and cov < coverage_s_min:
            return False, {"reason": f"coverage too short: {cov}s<{coverage_s_min}s"}

        current_phase = _get_phase_from_bars(bars)
        if phase_whitelist and current_phase not in phase_whitelist:
            delta_abs = abs(float(meta.get("delta_total", 0.0)))
            if not (delta_abs >= allow_delta_abs_min and tr >= allow_tickrate_min):
                return False, {
                    "reason": (
                        f"phase '{current_phase}' not in whitelist and not strong-enough "
                        f"(|Δ|={delta_abs}, tr={tr}/s)"
                    )
                }

        # -------- triggers --------
        try:
            d_climax = detect_volume_climax_after_consolidation(
                bars,
                df_levels,
                lookback_bars=int(cfg_fp.get("climax", {}).get("lookback_bars", 8)),
                vol_ratio_min=float(cfg_fp.get("climax", {}).get("vol_ratio_min", 2.0)),
                delta_ratio_min=float(
                    cfg_fp.get("climax", {}).get("delta_ratio_min", 1.5)
                ),
                need_consolidation=bool(
                    cfg_fp.get("climax", {}).get("need_consolidation", True)
                ),
                consolidation_max_atr_mult=float(
                    cfg_fp.get("climax", {}).get("consolidation_max_atr_mult", 1.0)
                ),
            )
            decision = d_climax if d_climax.get("ok") else None

            if decision is None:
                d_stack = detect_imbalance_stacking(
                    df_levels,
                    delta_ratio_min=float(
                        cfg_fp.get("stacking", {}).get("delta_ratio_min", 1.3)
                    ),
                    min_levels=int(cfg_fp.get("stacking", {}).get("min_levels", 3)),
                    invalidate_opposite_ratio=float(
                        cfg_fp.get("stacking", {}).get("invalidate_opposite_ratio", 0.6)
                    ),
                    vol_level_min_ratio_median_30s=float(
                        cfg_fp.get("vol_level_min_ratio_median_30s", 0.0)
                    ),
                )
                if d_stack.get("ok"):
                    decision = d_stack

            if decision is None:
                d_abs = detect_absorption_reject(
                    df_levels,
                    vol_zscore_min=float(
                        cfg_fp.get("absorption", {}).get("vol_zscore_min", 2.0)
                    ),
                    delta_ratio_max=float(
                        cfg_fp.get("absorption", {}).get("delta_ratio_max", 0.5)
                    ),
                    attempts_min=int(
                        cfg_fp.get("absorption", {}).get("attempts_min", 2)
                    ),
                )
                if d_abs.get("ok"):
                    decision = d_abs

            if decision is None:
                return False, {"reason": "no trigger"}
        except Exception as e:
            return False, {"reason": f"trigger eval error: {e}"}

        # -------- construction de l’ordre & trailing --------
        try:
            micro_atr = float(
                _micro_atr_from_ticks(
                    ticks, window_s=int(cfg_fp.get("trail_atr_window_s", 10))
                )
            )
            anchor_price = float(decision.get("anchor_price", meta.get("poc", 0.0)))
            direction = str(decision.get("direction", "BUY")).upper()

            # offset signé selon la direction
            signed_offset = float(price_offset_ticks) * float(price_step)
            signed_offset = (
                -abs(signed_offset) if direction == "SELL" else abs(signed_offset)
            )

            # Pour MARKET, le prix peut être ignoré en aval; on garde ancre+offset pour cohérence
            entry_price = float(anchor_price + signed_offset)

            entry = {
                "style": order_entry_style,  # e.g. LIMIT_FOK | MARKET
                "price": entry_price,
                "burst_count": int(burst_count),
                "validity_ms": int(order_validity_ms),
            }
            # --- sémantique burst ONE_PRICE + ouverture synchrone ---
            entry.update(
                {
                    "price_mode": "ONE_PRICE",  # toutes les tailles au même prix
                    "burst_open": "ONE_SHOT",  # envoi groupé
                    "burst_sync_open": True,  # synchronisation ouverture
                }
            )

            trailing = {
                "phase0": {
                    "window_s": int(
                        cfg_fp.get("trailing", {}).get("phase0_seconds", 5)
                    ),
                    "anchor": "footprint_block_or_micro_atr",
                    "mult": float(
                        cfg_fp.get("trailing", {}).get("phase0_mult_micro_atr_10s", 1.0)
                    ),
                },
                "phase1": {
                    "mult": float(
                        cfg_fp.get("trailing", {}).get("phase1_mult_micro_atr_10s", 1.5)
                    ),
                },
                "phase2": {
                    "mult": float(
                        cfg_fp.get("trailing", {}).get("phase2_mult_micro_atr_10s", 2.0)
                    ),
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
                        "scope": "BASKET",  # logique au niveau panier
                        "close_all_at_once": True,  # fermeture groupée
                        "require_all_in_profit": True,  # ne ferme que si toutes les tailles sont en PV
                        "sync_trailing": True,  # trailing partagé (panier)
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
