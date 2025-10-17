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
        Analyse Footprint (sans gardes-fous externes).
        - Normalisation datetime unique.
        - Multi-fenêtres (ex: 3s/5s/8s), double passe (normal -> soft).
        - Triggers: climax -> stacking -> absorption.
        - Sélection 'meilleur signal' (score + confluences).
        - Fallbacks inline (micro-stacking / micro-absorption) pour éviter un 'no trigger' abusif.
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
        import numpy as np

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
        price_step = float(sc.get("price_step", 0.01))
        cfg = ((sc.get("entry_rules", {}) or {}).get("scalping", {}) or {}).get(
            "burst_scalping", {}
        ).get("footprint_triggers", {}) or {}

        # candidates de fenêtres (tu peux overrider via conf: window_candidates_s: [3,5,8])
        win_cands = list(cfg.get("window_candidates_s", []) or [])
        if not win_cands:
            # ordre court -> moyen pour capter des bursts rapides
            win_cands = [3, 5, 8]

        # seuils par défaut (passe 1)
        base = dict(
            climax_lookback_bars=int(cfg.get("climax", {}).get("lookback_bars", 8)),
            climax_vol_ratio_min=float(cfg.get("climax", {}).get("vol_ratio_min", 1.6)),
            climax_delta_ratio_min=float(
                cfg.get("climax", {}).get("delta_ratio_min", 1.3)
            ),
            climax_need_cons=bool(
                cfg.get("climax", {}).get("need_consolidation", False)
            ),
            climax_cons_atr_max=float(
                cfg.get("climax", {}).get("consolidation_max_atr_mult", 2.0)
            ),
            stack_delta_ratio_min=float(
                cfg.get("stacking", {}).get("delta_ratio_min", 1.2)
            ),
            stack_min_levels=int(cfg.get("stacking", {}).get("min_levels", 2)),
            stack_inval_opp_ratio=float(
                cfg.get("stacking", {}).get("invalidate_opposite_ratio", 0.65)
            ),
            stack_vol_lvl_min_med=float(cfg.get("vol_level_min_ratio_median_30s", 0.0)),
            abs_vol_z_min=float(cfg.get("absorption", {}).get("vol_zscore_min", 1.2)),
            abs_delta_ratio_max=float(
                cfg.get("absorption", {}).get("delta_ratio_max", 0.6)
            ),
            abs_attempts_min=int(cfg.get("absorption", {}).get("attempts_min", 1)),
        )

        # soft (passe 2) – auto-relâchement léger
        soft = dict(
            climax_vol_ratio_min=max(1.15, base["climax_vol_ratio_min"] * 0.85),
            climax_delta_ratio_min=max(1.10, base["climax_delta_ratio_min"] * 0.85),
            stack_delta_ratio_min=max(1.05, base["stack_delta_ratio_min"] * 0.90),
            abs_vol_z_min=max(0.80, base["abs_vol_z_min"] * 0.80),
            abs_attempts_min=1,
        )

        # --- helpers locaux -------------------------------------------------
        def _score_boost_from_meta(dec, meta):
            """Petit boost de confiance si la direction colle à l'empreinte globale."""
            try:
                conf = float(dec.get("confidence", 0.7) or 0.7)
                sdir = 1 if str(dec.get("direction", "BUY")).upper() == "BUY" else -1
                dtot = float(meta.get("delta_total", 0.0) or 0.0)
                if dtot != 0 and np.sign(dtot) == sdir:
                    conf = min(0.99, conf + 0.05)  # boost léger
                # Si anchor ~ proche POC, petit malus (on préfère un bloc net plutôt que le POC)
                anc = float(dec.get("anchor_price", meta.get("poc", 0.0)) or 0.0)
                poc = float(meta.get("poc", 0.0) or 0.0)
                if anc and poc and abs(anc - poc) <= price_step:
                    conf = max(0.55, conf - 0.03)
                return conf
            except Exception:
                return float(dec.get("confidence", 0.7) or 0.7)

        def _pick_best(*cands):
            """Choisit la meilleure décision par confidence; tie-break: stacking>climax>absorption."""
            cands = [c for c in cands if c and c.get("ok")]
            if not cands:
                return None
            # boost meta déjà inclus dans les candidats passés ici
            prio = {
                "stacking": 3,
                "climax_after_consolidation": 2,
                "absorption_reject": 1,
            }
            cands.sort(
                key=lambda d: (
                    float(d.get("confidence", 0.0)),
                    prio.get(str(d.get("trigger", "")), 0),
                ),
                reverse=True,
            )
            return cands[0]

        def _micro_stack_inline(df_levels, delta_ratio_min=1.1, max_gap=1):
            """Fallback minimaliste: repère un run 2-3 niveaux adjacents même signe & ratio > seuil."""
            try:
                lv = df_levels[df_levels["vol"] > 0].copy()
                if lv.empty:
                    return None
                # ordonne
                lv = lv.sort_index()
                idx = lv.index.values.astype(float)
                sign = np.sign(lv["delta"].values)
                ratio = lv["delta_ratio"].values
                # pas approximatif
                diffs = np.diff(np.unique(idx))
                step = np.quantile(diffs[diffs > 0], 0.1) if len(diffs) > 0 else 0.0
                best = None
                i = 0
                while i < len(idx) - 1:
                    if sign[i] == 0 or ratio[i] < delta_ratio_min:
                        i += 1
                        continue
                    j = i + 1
                    gaps = 0
                    run = 1
                    sgn = sign[i]
                    while j < len(idx):
                        if sign[j] != sgn or ratio[j] < delta_ratio_min:
                            break
                        if step > 0 and (idx[j] - idx[j - 1]) > 1.6 * step:
                            gaps += 1
                            if gaps > max_gap:
                                break
                        run += 1
                        j += 1
                    if run >= 2:
                        direction = "BUY" if sgn > 0 else "SELL"
                        anchor = float(idx[j - 1])
                        conf = min(
                            0.9,
                            0.55
                            + 0.1 * (run - 2)
                            + 0.1
                            * np.clip(ratio[i:j].mean() / delta_ratio_min, 0, 1.5),
                        )
                        best = {
                            "ok": True,
                            "trigger": "stacking_inline",
                            "direction": direction,
                            "confidence": conf,
                            "anchor_price": anchor,
                            "meta": {
                                "levels": int(run),
                                "delta_ratio_mean": float(ratio[i:j].mean()),
                            },
                        }
                        break
                    i = j
                return best
            except Exception:
                return None

        def _micro_absorption_inline(df_levels, z_min=1.1, opp_ratio=0.6):
            """Fallback absorption simple: zscore_vol élevé + voisin opposé fort."""
            try:
                lv = df_levels.copy()
                req = {"zscore_vol", "delta_ratio", "delta"}
                if any(c not in lv.columns for c in req):
                    return None
                cands = lv[(lv["zscore_vol"] >= z_min) & (lv["delta_ratio"] <= 0.6)]
                if cands.empty:
                    return None
                lv = lv.sort_index()
                for price, row in cands.iterrows():
                    i = lv.index.get_loc(price)
                    neigh = []
                    if i > 0:
                        neigh.append(lv.iloc[i - 1])
                    if i + 1 < len(lv):
                        neigh.append(lv.iloc[i + 1])
                    for nb in neigh:
                        if (
                            np.sign(nb["delta"]) != np.sign(row["delta"])
                            and nb["delta_ratio"] >= opp_ratio
                        ):
                            direction = "BUY" if nb["delta"] > 0 else "SELL"
                            conf = min(
                                0.9, 0.6 + 0.2 * (row["zscore_vol"] / max(1.0, z_min))
                            )
                            return {
                                "ok": True,
                                "trigger": "absorption_inline",
                                "direction": direction,
                                "confidence": float(conf),
                                "anchor_price": float(nb.name),
                                "meta": {
                                    "absorbed_level": float(price),
                                    "absorbed_zscore": float(row["zscore_vol"]),
                                },
                            }
                return None
            except Exception:
                return None

        # --- exploration multi-fenêtres & double passe ---------------------
        best_decision = None
        best_meta = None

        for pass_kind in ("normal", "soft"):
            p = dict(base)
            if pass_kind == "soft":
                p.update(soft)

            for win in win_cands:
                # snapshot
                try:
                    df_levels, meta = _compute_footprint_snapshot(
                        ticks, price_step=price_step, window_s=int(win)
                    )
                    if df_levels is None or df_levels.empty:
                        continue
                except Exception as e:
                    _log("debug", f"[SNAPSHOT] win={win}s error: {e}")
                    continue

                # 1) climax
                d1 = {}
                try:
                    if bars is not None and not getattr(bars, "empty", True):
                        d1 = detect_volume_climax_after_consolidation(
                            bars,
                            df_levels,
                            lookback_bars=p["climax_lookback_bars"],
                            vol_ratio_min=p["climax_vol_ratio_min"],
                            delta_ratio_min=p["climax_delta_ratio_min"],
                            need_consolidation=p["climax_need_cons"],
                            consolidation_max_atr_mult=p["climax_cons_atr_max"],
                        )
                except Exception as e:
                    _log("debug", f"[TRIGGER] climax error: {e}")
                    d1 = {}

                # 2) stacking
                try:
                    d2 = detect_imbalance_stacking(
                        df_levels,
                        delta_ratio_min=p["stack_delta_ratio_min"],
                        min_levels=p["stack_min_levels"],
                        invalidate_opposite_ratio=p["stack_inval_opp_ratio"],
                        vol_level_min_ratio_median_30s=p["stack_vol_lvl_min_med"],
                    )
                except Exception as e:
                    _log("debug", f"[TRIGGER] stacking error: {e}")
                    d2 = {}

                # 3) absorption
                try:
                    d3 = detect_absorption_reject(
                        df_levels,
                        vol_zscore_min=p["abs_vol_z_min"],
                        delta_ratio_max=p["abs_delta_ratio_max"],
                        attempts_min=p["abs_attempts_min"],
                    )
                except Exception as e:
                    _log("debug", f"[TRIGGER] absorption error: {e}")
                    d3 = {}

                # 4) fallbacks inline si rien
                if not (d1.get("ok") or d2.get("ok") or d3.get("ok")):
                    d4 = _micro_stack_inline(df_levels)
                    d5 = _micro_absorption_inline(df_levels)
                else:
                    d4 = d5 = None

                # boost de confiance meta & sélection
                cand_list = []
                for d in (d1, d2, d3, d4, d5):
                    if d and d.get("ok"):
                        d = dict(d)
                        d["confidence"] = _score_boost_from_meta(d, meta)
                        cand_list.append(d)

                pick = _pick_best(*cand_list) if cand_list else None
                if pick:
                    # garde le meilleur global
                    if (best_decision is None) or (
                        float(pick.get("confidence", 0))
                        > float(best_decision.get("confidence", 0))
                    ):
                        best_decision = pick
                        best_meta = meta

            if best_decision is not None:
                break  # on s'arrête à la première passe qui déclenche (normal ou soft)

        if best_decision is None:
            return False, {"reason": "no trigger"}

        # --- sortie décision finale ---
        try:
            direction = str(best_decision.get("direction", "BUY")).upper()
            anchor_price = float(
                best_decision.get("anchor_price", (best_meta or {}).get("poc", 0.0))
            )
        except Exception:
            direction, anchor_price = "BUY", float(
                (best_meta or {}).get("poc", 0.0) or 0.0
            )

        out = {
            "action": "BUY" if direction == "BUY" else "SELL",
            "asset": asset,
            "trigger": best_decision.get("trigger", "footprint"),
            "confidence": float(best_decision.get("confidence", 0.7) or 0.7),
            "anchor_price": anchor_price,
            "meta": {
                **(best_meta or {}),
                **(best_decision.get("meta", {}) or {}),
                "window_used": int(win_cands[0] if isinstance(win_cands, list) else 0),
            },
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
