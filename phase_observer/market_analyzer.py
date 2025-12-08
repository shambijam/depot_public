# phase_observer/market_analyzer.py
from __future__ import annotations

import logging
from typing import Dict, Any, Tuple, Optional, List

import pandas as pd

from .orchestrator import PhaseObserver
# === [IMPORTS PATTERNS SUPPRIMÉS - Session 23 Nov 2025] ===
# Imports detect_single_candle, detect_multi_candle_patterns, detect_combos retirés
# Raison : Détecteurs de patterns/bougies supprimés du système
# === [IMPORT ORDERFLOW V6 SUPPRIMÉ - Session 28 Nov 2025] ===
# detect_orderflow_v6 supprimé → remplacé par analyse intégrée dans ScalpingStrategy
from .footprint_analyzer import FootprintAnalyzer
from .fusion_manager import FusionManager
# ✅ [IMPORT VWAP MODULE - Session 03 Dec 2025]
from .vwap import create_vwap_analyzer


LOG = logging.getLogger(__name__)


class MarketAnalyzer:
    """
    Analyse unifiée du marché :
      - PhaseObserver (annotation du DF)
      - Détecteurs chandeliers (single/multi/combos)
      - OrderFlow V6 (métriques institutionnelles + volume profile)
      - Footprint triggers (pass-through)
      - FusionManager (OFv6 + FP M1 + triggers) via `build_fused_decision`
    """

    def __init__(self, config_manager=None, logger=None):
        self.logger = logger or LOG
        self.config_manager = config_manager

        # Briques principales (robustes à l’init)
        try:
            self.phase_observer = PhaseObserver(config_manager=config_manager)
        except Exception as e:
            self.logger.error(f"[MarketAnalyzer] PhaseObserver init failed: {e}")
            self.phase_observer = None

        self._last_results: Dict[str, Any] = {}
        self._confluence_cache: Dict[str, pd.DataFrame] = {}

        try:
            self.footprint = FootprintAnalyzer(logger=self.logger)
        except Exception as e:
            self.logger.error(f"[MarketAnalyzer] FootprintAnalyzer init failed: {e}")
            self.footprint = None

        try:
            # NB: si ta FusionManager accepte (config_manager, logger), branche-les ici
            self.fusion_manager = FusionManager()
        except Exception as e:
            self.logger.error(f"[MarketAnalyzer] FusionManager init failed: {e}")
            self.fusion_manager = None

    # ============================================================
    # ✅ VWAP MODULE — Analyse VWAP institutionnelle (03 DEC 2025)
    # ============================================================
    def analyze_vwap(
        self,
        asset: str,
        df: pd.DataFrame,
        current_price: float,
        strategy_config: Dict[str, Any],
        context: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Analyse VWAP institutionnelle via VWAPAnalyzer

        Args:
            asset: Symbol (XAUUSD, EURUSD, etc.)
            df: DataFrame OHLC (depuis OrderFlow V6 ou MarketData)
            current_price: Prix actuel
            strategy_config: Config stratégie
            context: Contexte additionnel

        Returns:
            Dict depuis VWAPAnalysisResult.to_dict()
            {
                "score": 0.0-1.0,
                "status": "VALID"|"SUSPECT"|"INVALID",
                "bias": "BUY"|"SELL"|"NEUTRAL",
                "vwap_value": float,
                "distance_pips": float,
                "slope": float,
                "zone": str,
                "regime": str,
                "summary": {...},
                ...
            }
        """
        try:
            # Créer analyseur VWAP (léger, pas besoin de cache entre appels)
            vwap_analyzer = create_vwap_analyzer(asset, strategy_config)

            # Analyse complète
            result = vwap_analyzer.analyze(df, current_price, context)

            # Convertir en dict pour fusion
            return result.to_dict()

        except Exception as e:
            self.logger.error(f"[MarketAnalyzer] VWAP analysis failed: {e}", exc_info=True)
            return {
                "score": 0.0,
                "status": "INVALID",
                "bias": "NEUTRAL",
                "vwap_value": 0.0,
                "distance_pips": 0.0,
                "slope": 0.0,
                "zone": "NEUTRAL",
                "regime": "BALANCED",
                "summary": {"error": str(e)},
            }

    # ============================================================
    # 🔹 FUSION MANAGER — décision unifiée (OFv6 + FP M1 + VWAP)
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
        """
        Construit la décision fusionnée.
        Attend dans `market_results["patterns"]["orderflow"]` le dict OFv6.
        Tolère l’absence de footprint/trigger et comble anchor depuis POC si nécessaire.
        """
        if self.fusion_manager is None:
            return {"status": "SUSPECT", "reason": "FusionManager unavailable"}

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

        # ✅ ANALYSE VWAP (03 DEC 2025)
        # Récupérer DataFrame M1 depuis market_results
        # Essayer plusieurs clés possibles pour le DataFrame
        df_m1 = (market_results or {}).get("annotated_rates_df") or (market_results or {}).get("annotated_df")
        current_price = (latest or {}).get("close") or (latest or {}).get("current_price") if latest else None

        vwap_result = {}
        if df_m1 is not None and current_price is not None:
            try:
                vwap_result = self.analyze_vwap(
                    asset=asset,
                    df=df_m1,
                    current_price=current_price,
                    strategy_config=strategy_config,
                    context=context
                )
                self.logger.info(
                    f"[MarketAnalyzer] ✅ VWAP | score={vwap_result.get('score', 0):.3f} | "
                    f"bias={vwap_result.get('bias')} | zone={vwap_result.get('zone')} | "
                    f"slope={vwap_result.get('slope', 0):.6f}"
                )
            except Exception as e:
                self.logger.error(f"[MarketAnalyzer] VWAP analysis failed: {e}")
                vwap_result = {}  # Fallback vide
        else:
            self.logger.warning(f"[MarketAnalyzer] VWAP skipped: df_m1={df_m1 is not None}, price={current_price}")

        # On transmet aussi un contexte optionnel (spread/session/régime/horodatage…)
        ctx = dict(context or {})
        ctx.setdefault("now_ts", None)  # si absent, FusionManager utilise time.time()
        ctx["asset"] = asset  # ✅ AJOUTÉ: Transmettre l'asset pour le rapport consolidé

        # ✅ AJOUT (08 DEC 2025): Transmettre position dans le range pour logique de retournement
        self.logger.info(f"[RANGE_CTX_DEBUG] {asset} | latest type={type(latest)} | is_none={latest is None} | bool={bool(latest)}")
        if latest is not None and (isinstance(latest, dict) and latest or not isinstance(latest, dict)):
            try:
                # Pandas Series utilise .get() mais peut retourner pd.NA ou NaN
                import pandas as pd

                # Extraire valeurs avec fallback robuste
                range_pos = latest.get("range_pos_pct")
                if range_pos is None or (isinstance(range_pos, float) and pd.isna(range_pos)):
                    range_pos = 0.5

                in_upper = latest.get("in_upper_tercile")
                if in_upper is None or (hasattr(pd, 'isna') and pd.isna(in_upper)):
                    in_upper = False

                in_lower = latest.get("in_lower_tercile")
                if in_lower is None or (hasattr(pd, 'isna') and pd.isna(in_lower)):
                    in_lower = False

                regime = latest.get("regime")
                if regime is None or (hasattr(pd, 'isna') and pd.isna(regime)):
                    regime = "unknown"

                ctx["range_pos_pct"] = float(range_pos)
                ctx["in_upper_tercile"] = bool(in_upper)
                ctx["in_lower_tercile"] = bool(in_lower)
                ctx["phase_observer_regime"] = str(regime)

                # 🔍 DEBUG LOG
                self.logger.info(
                    f"[RANGE_CONTEXT] {asset} | regime={regime} | pos={float(range_pos):.0%} | "
                    f"upper={bool(in_upper)} | lower={bool(in_lower)}"
                )
            except Exception as e:
                self.logger.warning(f"[RANGE_CONTEXT] Failed to extract range data: {e}")
                ctx["range_pos_pct"] = 0.5
                ctx["in_upper_tercile"] = False
                ctx["in_lower_tercile"] = False
                ctx["phase_observer_regime"] = "unknown"

        try:
            fused = self.fusion_manager.fuse(
                orderflow=of,
                footprint=fp_payload,
                vwap=vwap_result,
                strategy_config=strategy_config,
                context=ctx,
            )
        except Exception as e:
            self.logger.error(f"[MarketAnalyzer] Fusion failed: {e}", exc_info=True)
            return {"status": "SUSPECT", "error": str(e)}

        # Si pas d'ancre côté trigger, harmonise avec POC footprint, puis OF
        if fused.get("anchor_price") is None:
            poc_fp = fp_payload.get("summary", {}).get("poc")
            if poc_fp is not None:
                try:
                    fused["anchor_price"] = float(poc_fp)
                except Exception:
                    pass
            if fused.get("anchor_price") is None:
                poc_of = (of or {}).get("poc")
                if poc_of is not None:
                    try:
                        fused["anchor_price"] = float(poc_of)
                    except Exception:
                        pass

        return fused

    # ============================================================
    # 🔹 Analyse unifiée
    # ============================================================
    def analyze(
        self,
        df: pd.DataFrame,
        asset: str = "",
        ticks: Optional[pd.DataFrame] = None,
        footprint_summary: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Étapes :
          1) PhaseObserver (annotation df)
          2) Détecteurs chandeliers et combos
          3) OrderFlow V6 (avec paramètres issus de la config si dispo)
          4) Dernier point (Series)
          5) Qualité + confluence
        """
        if df is None or df.empty:
            return {"annotated_df": pd.DataFrame(), "latest": None, "patterns": {}}

        # 1️⃣ PhaseObserver
        if self.phase_observer is None:
            self.logger.warning(
                "[MarketAnalyzer] PhaseObserver unavailable → passthrough df"
            )
            annotated_df = df.copy()
        else:
            try:
                annotated_df = self.phase_observer.analyze(
                    df.copy(), asset_symbol=asset, ticks=ticks
                )
            except Exception as e:
                self.logger.error(f"[MarketAnalyzer] PhaseObserver analyze failed: {e}")
                annotated_df = df.copy()

        if annotated_df is None or annotated_df.empty:
            return {"annotated_df": pd.DataFrame(), "latest": None, "patterns": {}}

        # 2️⃣ Détecteurs factuels
        # === [DÉTECTEURS PATTERNS SUPPRIMÉS - Session 23 Nov 2025] ===
        # Appels à detect_single_candle, detect_multi_candle_patterns, detect_combos retirés (18 lignes)
        # Raison : Détecteurs de patterns/bougies supprimés du système
        candles = []
        multi_patterns = []
        combo_patterns = []

        # 2️⃣bis OrderFlow V6 - SUPPRIMÉ (Session 28 Nov 2025)
        # → Analyse déplacée dans ScalpingStrategy._analyze_orderflow_v6()

        # Récupérer les données Footprint M1 depuis le résultat PhaseObserver
        # SOURCE 1: Depuis footprint_summary (passé en paramètre - cache scalping)
        # SOURCE 2: Depuis annotated_df (footprint_summary dans la dernière ligne)
        footprint_data = None

        if footprint_summary is not None and isinstance(footprint_summary, dict):
            # SOURCE 1: Cache scalping
            footprint_data = footprint_summary
            self.logger.debug(f"[MarketAnalyzer] Footprint M1 depuis cache: delta={footprint_data.get('delta_total', 0):.1f}")

        elif not annotated_df.empty and 'footprint_summary' in annotated_df.columns:
            # SOURCE 2: Depuis annotated_df (dernière ligne)
            try:
                import json
                fp_json = annotated_df.iloc[-1]['footprint_summary']
                if isinstance(fp_json, str):
                    footprint_data = json.loads(fp_json)
                    self.logger.debug(f"[MarketAnalyzer] Footprint M1 depuis annotated_df: delta={footprint_data.get('delta_total', 0):.1f}")
                elif isinstance(fp_json, dict):
                    footprint_data = fp_json
                    self.logger.debug(f"[MarketAnalyzer] Footprint M1 depuis annotated_df: delta={footprint_data.get('delta_total', 0):.1f}")
            except Exception as e:
                self.logger.warning(f"[MarketAnalyzer] Footprint extraction from annotated_df failed: {e}")

        # === [ORDERFLOW V6 SUPPRIMÉ - Session 28 Nov 2025] ===
        # Ancienne analyse detect_orderflow_v6 supprimée
        # → Remplacée par analyse OrderFlow V6 intégrée dans ScalpingStrategy._analyze_orderflow_v6()
        orderflow_signals = {}

        # 3️⃣ Dernier point brut (Series Pandas)
        try:
            latest = annotated_df.iloc[-1].copy()  # .copy() pour éviter SettingWithCopyWarning
        except Exception:
            latest = None

        # 3️⃣bis Enrichir footprint_summary avec métadonnées ticks (pour validation footprint M1)
        if latest is not None and ticks is not None and not ticks.empty:
            try:
                # Calculer métadonnées depuis ticks_df
                tick_count = len(ticks)

                # Coverage temporel (secondes)
                if "time" in ticks.columns:
                    time_col = ticks["time"]
                    time_min = time_col.min()
                    time_max = time_col.max()
                    if pd.notna(time_min) and pd.notna(time_max):
                        coverage_s = (time_max - time_min).total_seconds()
                    else:
                        coverage_s = 0.0
                else:
                    coverage_s = 0.0

                # Tick rate (ticks/seconde)
                tick_rate = tick_count / coverage_s if coverage_s > 0 else 0.0

                # Enrichir footprint_summary (EN CONSERVANT les champs existants !)
                # ✅ FIX (24 Nov 2025): Ne PAS écraser buy_volume/sell_volume/buy_pct venant de detectors.py
                fp_summ = latest.get("footprint_summary")
                if isinstance(fp_summ, str):
                    # Si c'est une JSON string, la parser avec json.loads() (PAS ast.literal_eval)
                    # ✅ FIX (24 Nov 2025): orchestrator.py stocke footprint_summary en JSON string
                    try:
                        import json
                        fp_summ = json.loads(fp_summ)
                    except Exception as e:
                        self.logger.error(f"[MarketAnalyzer] JSON parse failed for footprint_summary: {e}")
                        fp_summ = {}
                elif not isinstance(fp_summ, dict):
                    fp_summ = {}
                else:
                    # ✅ Créer une COPIE pour ne pas modifier l'original
                    fp_summ = dict(fp_summ)

                # Ajouter/mettre à jour UNIQUEMENT les métadonnées temporelles (SANS écraser le reste)
                fp_summ["tick_count"] = tick_count
                fp_summ["coverage_s"] = round(coverage_s, 2)
                fp_summ["tick_rate"] = round(tick_rate, 2)

                # Remettre dans latest (sur la copie, pas de warning)
                # 🔧 FIX (24 Nov 2025): Garder en dict, ne PAS convertir en string
                latest["footprint_summary"] = fp_summ

                self.logger.info(
                    f"[MarketAnalyzer][{asset}] ✅ footprint_summary enrichi: "
                    f"tick_count={tick_count}, coverage_s={coverage_s:.2f}, tick_rate={tick_rate:.2f}"
                )
            except Exception as e:
                self.logger.warning(f"[MarketAnalyzer][{asset}] Échec enrichissement footprint_summary: {e}")

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
            "phase": (latest.get("phase") if latest is not None else None),
            "confidence": (
                latest.get("confidence_score", 0.5) if latest is not None else 0.5
            ),
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
            diag = {"n_bars": n_bars, "has_volume": has_volume, "has_time": has_time}
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

    # ============================================================
    # 🔹 Paramètres OrderFlow V6 (depuis config si dispo)
    # ============================================================
    def _get_ofv6_params(self, asset: str = "") -> Dict[str, Any]:
        """
        Lit des paramètres OFv6 depuis la config si disponible ; sinon valeurs sûres.
        Cherche d’abord une section globale `orderflow_v6`, puis un override par actif.
        """
        # Défauts sûrs
        params = {
            "imbalance_threshold": 0.20,
            "cvd_smoothing": 0.0,
            "price_bins": 20,
            "vp_options": {
                # options avancées du Volume Profile ; toutes facultatives
                # "use_ohlc_overlap": True, "body_gain": 0.6, "coverage": 0.70,
                # "bin_width": None, "tick_size": None, "max_bins": 400, "ib_bars": 30,
                # "return_nodes": False,
            },
        }

        cm = self.config_manager
        if cm is None:
            return params

        try:
            # global
            ofg = cm.get("orderflow_v6", {}) or {}
            # override actif (si tu as une convention type config/assets/<ASSET>.json → à brancher ici)
            ofa = {}
            try:
                # Exemple d’override par asset si ton ConfigManager expose une méthode dédiée :
                # ofa = (cm.load_asset_config(asset) or {}).get("orderflow_v6", {}) or {}
                pass
            except Exception:
                pass

            # merge (asset override > global > defaults)
            merged = dict(params)
            for src in (ofg, ofa):
                if not isinstance(src, dict):
                    continue
                if (
                    "imbalance_threshold" in src
                    and src["imbalance_threshold"] is not None
                ):
                    merged["imbalance_threshold"] = float(src["imbalance_threshold"])
                if "cvd_smoothing" in src and src["cvd_smoothing"] is not None:
                    merged["cvd_smoothing"] = float(src["cvd_smoothing"])
                if "price_bins" in src and src["price_bins"] is not None:
                    merged["price_bins"] = int(src["price_bins"])
                if "vp_options" in src and isinstance(src["vp_options"], dict):
                    # on n’écrase pas tout, on met à jour
                    merged["vp_options"] = {**merged["vp_options"], **src["vp_options"]}

            return merged
        except Exception as e:
            self.logger.debug(f"[MarketAnalyzer] _get_ofv6_params fallback: {e}")
            return params
