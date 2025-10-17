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
            # --------- 0) RÉCUP CONFIG (chemin canonique + fallback rétro-compat) ----------
            # Canonique (ton JSON de stratégie)
            cfg_fp = (
                (strategy_config or {})
                .get("entry_rules", {})
                .get("scalping", {})
                .get("burst_scalping", {})
                .get("footprint_triggers", {})
            )

            # Fallback: attribut déjà injecté dans l'Analyzer
            if not cfg_fp:
                cfg_fp = getattr(self, "footprint_triggers", {}) or {}

            # Fallback legacy: footprint_triggers à la racine (anciens dumps)
            if not cfg_fp:
                cfg_fp = (strategy_config or {}).get("footprint_triggers", {}) or {}

            if not bool(cfg_fp.get("enabled", False)):
                return False, {
                    "reason": "footprint_triggers disabled (cfg not found/enabled)"
                }

            # --------- 1) PARAMS / DEFAULTS ----------
            # price_step pour l’ancrage prix; laisse un défaut safe si non fourni
            price_step = float((strategy_config or {}).get("price_step", 0.1))

            # Seuils “hygiène” simples basés sur TA config actuelle
            tickrate_min = float(cfg_fp.get("tickrate_min", 0.0))  # par seconde
            coverage_s_min = float(cfg_fp.get("coverage_s_min_burst", 0.0))  # seconds
            phase_whitelist = list(
                cfg_fp.get("phase_whitelist", [])
            )  # ex: ["range_distribution", ...]
            allow_strong = cfg_fp.get("allow_no_clear_phase_if_strong", {}) or {}
            allow_delta_abs_min = float(allow_strong.get("of_delta_abs_min", 0.0))
            allow_tickrate_min = float(allow_strong.get("tickrate_min", tickrate_min))

            # Paramètres d’ordre (defaults si non présents dans la conf)
            order_entry_style = str(cfg_fp.get("entry_style", "LIMIT_FOK")).upper()
            order_validity_ms = int(cfg_fp.get("validity_ms", 800))

            # Récup du burst_count depuis la conf scalping -> burst_scalping
            burst_block = (strategy_config or {}).get("entry_rules", {}).get(
                "scalping", {}
            ).get("burst_scalping", {}) or {}
            burst_count = int(burst_block.get("burst_size", 5))

            # Offset prix optionnel (en ticks)
            price_offset_ticks = float(cfg_fp.get("price_offset_ticks", 0.0))

            # --------- 2) SNAPSHOT FOOTPRINT ----------
            from .features import _compute_footprint_snapshot, _micro_atr_from_ticks

            df_levels, meta = _compute_footprint_snapshot(
                ticks, price_step=price_step, window_s=5
            )
            # meta attendus (selon ton implé): spread, tick_rate, coverage_s, delta_total, etc.

            if df_levels is None or df_levels.empty:
                return False, {"reason": "no footprint levels"}

            # --------- 3) FILTRES D’HYGIÈNE (alignés sur ta conf) ----------
            # tick-rate par seconde
            if tickrate_min > 0.0 and float(meta.get("tick_rate", 0.0)) < tickrate_min:
                return False, {
                    "reason": f"tickrate too low: {meta.get('tick_rate', 0.0)} < {tickrate_min}/s"
                }

            # couverture temporelle minimale de la fenêtre
            if (
                coverage_s_min > 0.0
                and float(meta.get("coverage_s", 0.0)) < coverage_s_min
            ):
                return False, {
                    "reason": f"coverage too short: {meta.get('coverage_s', 0.0)}s < {coverage_s_min}s"
                }

            # phase gating — only if whitelist fournie
            current_phase = None
            try:
                if bars is not None and len(bars) > 0 and "phase" in bars.columns:
                    last_non_na = bars["phase"].dropna()
                    if len(last_non_na) > 0:
                        current_phase = str(last_non_na.iloc[-1])
            except Exception:
                current_phase = None

            if phase_whitelist:
                if current_phase not in phase_whitelist:
                    # Bypass si “fort” et clair (tes critères)
                    delta_abs = abs(float(meta.get("delta_total", 0.0)))
                    tr = float(meta.get("tick_rate", 0.0))
                    if not (
                        delta_abs >= allow_delta_abs_min and tr >= allow_tickrate_min
                    ):
                        return False, {
                            "reason": f"phase '{current_phase}' not in whitelist and not strong-enough "
                            f"(Δ|={delta_abs}, tr={tr}/s)"
                        }

            # --------- 4) DÉTECTIONS (ordre de priorité) ----------
            # Import local pour rester cohérent avec ton module
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
                    # garde ce filtre de volumétrie si tu l'utilises côté algo
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

            # --------- 5) CONSTRUCTION DE L’ENTRÉE / TRAILING ----------
            micro_atr = float(_micro_atr_from_ticks(ticks, window_s=10))

            # Prix d’ancrage + offset éventuel
            anchor_price = float(decision.get("anchor_price", meta.get("poc", 0.0)))
            entry_price = anchor_price + price_offset_ticks * price_step

            entry = {
                "style": order_entry_style,  # LIMIT_FOK
                "price": float(entry_price),
                "burst_count": int(burst_count),
                # NOTE: on ne force PAS burst_volume_each ici.
                # Le planner pourra le déduire de la taille totale (risk-based) / burst_count.
                "validity_ms": int(order_validity_ms),
            }

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
                    float(cfg_fp.get("trailing", {}).get("clamp_max", 9999.0)),
                ],
                "micro_atr_10s": micro_atr,
            }

            decision_out = {
                "action": (
                    "BUY"
                    if str(decision.get("direction", "")).upper() == "BUY"
                    else "SELL"
                ),
                "asset": asset,
                "trigger": decision.get("trigger", "footprint"),
                "confidence": float(decision.get("confidence", 0.7)),
                "entry": entry,  # ← le wrapper remappera en top-level (style, price, burst_count, validity_ms)
                "exit": {"type": "TRAILING_ONLY", "phases": trailing},
                "meta": {**meta, **decision.get("meta", {})},
            }
            return True, decision_out

        except Exception as e:
            # garde le message clair pour les logs du pipeline
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
