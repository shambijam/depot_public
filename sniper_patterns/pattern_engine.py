# sniper_patterns/pattern_engine.py

import pandas as pd
import json
import logging
from pathlib import Path
from typing import List, Dict, Any, Optional

# Briques fonctionnelles
from .candle_detector import detect_single_candle
# pattern_engine.py
from .multi_candle_detector import detect_multi_candle
from .combo_detector import detect_combos
from .context_enricher import enrich_context
from .structure_detector import enrich_structure
from .multi_tf_confirmer import confirm_multi_tf
from .orderflow_detector import detect_orderflow   

LOG = logging.getLogger(__name__)


class PatternEngine:
    """
    🏛️ PatternEngine (mode Dev Desk)
    ------------------------------------------------
    Orchestrateur unique de lecture du marché :
    - Chandeliers simples (individuels)
    - Patterns multi-bougies
    - Combinaisons enrichies (confluences, MTF, OB/FVG/BOS)
    - Order Flow (déséquilibre, absorption, exhaustion)
    - Ajout contextuel : phase, volume, volatilité

    Retourne un tableau consolidé de signaux exploitables
    pour le pipeline décisionnel du bot.
    """

    def __init__(
        self,
        enable_context=True,
        enable_structure=True,
        enable_multi_tf=True,
        enable_orderflow=True,
        patterns_file: Optional[str] = "config/sniper_patterns.json",
        auto_reload: bool = False,
    ):
        self.enable_context = enable_context
        self.enable_structure = enable_structure
        self.enable_multi_tf = enable_multi_tf
        self.enable_orderflow = enable_orderflow

        self.patterns_file = Path(patterns_file) if patterns_file else None
        self.auto_reload = auto_reload
        self._patterns_mtime = None
        self.patterns = self._load_patterns()
 

    def _load_patterns(self) -> Dict[str, Any]:
        """Charge le JSON des patterns (safe). Retourne {} si erreur."""
        if not self.patterns_file:
            return {}
        try:
            if not self.patterns_file.exists():
                LOG.warning("Patterns file introuvable: %s", self.patterns_file)
                return {}
            # auto-reload check: si modifié, relire
            if self.auto_reload:
                mtime = self.patterns_file.stat().st_mtime
                if self._patterns_mtime and mtime == self._patterns_mtime:
                    return self.patterns or {}
                self._patterns_mtime = mtime

            with self.patterns_file.open("r", encoding="utf-8") as f:
                data = json.load(f)
            LOG.info("Patterns JSON chargé depuis %s", self.patterns_file)
            return data or {}
        except Exception as e:
            LOG.exception("Erreur chargement patterns JSON: %s", e)
            return {}

    def analyze(self, df: pd.DataFrame, with_combo: bool = True, detailed: bool = False) -> Dict[str, Any]:
        """
        Version Dev-Desk (banque privée) de l'analyse des patterns.

        - Retourne des listes **alignées** sur len(df) pour chaque type de signal :
        * candle_signals: List[Optional[Dict]]
        * multi_signals:  List[Optional[List[Dict]]]
        * combo_signals:  List[Optional[List[Dict]]]
        * orderflow_signals: List[Optional[Dict]]

        - Ajoute une section `meta` résumant le run.
        - Flag `detailed` pour logs détaillés (trace par bougie).
        - Résilient : normalise / pad / truncate les sorties pour éviter tout mismatch downstream.
        """
        # garde-temps / validations rapides
        if df is None:
            return {
                "candle_signals": [],
                "multi_signals": [],
                "combo_signals": [],
                "orderflow_signals": [],
                "meta": {"nb_rows": 0, "last_timestamp": None, "summary": {}},
            }
        n = len(df)
        if n < 1:
            return {
                "candle_signals": [None] * n,
                "multi_signals": [None] * n,
                "combo_signals": [None] * n,
                "orderflow_signals": [None] * n,
                "meta": {"nb_rows": n, "last_timestamp": None, "summary": {}},
            }

        # ---------- helpers locaux ----------
        def _ensure_len(lst, name: str):
            # tronque ou pad avec None pour garantir len == n
            if lst is None:
                return [None] * n
            if len(lst) == n:
                return lst
            if len(lst) > n:
                LOG.debug("PatternEngine._ensure_len: truncating %s from %d to %d", name, len(lst), n)
                return lst[:n]
            # pad
            LOG.debug("PatternEngine._ensure_len: padding %s from %d to %d", name, len(lst), n)
            return list(lst) + [None] * (n - len(lst))

        def _compute_context_at(i_idx: int) -> Dict[str, Any]:
            # réimplémentation légère et robuste du context_enricher pour usage par-signal
            ctx: Dict[str, Any] = {}
            try:
                high = df["high"].iloc[i_idx]
                low = df["low"].iloc[i_idx]
                close = df["close"].iloc[i_idx]
                candle_size = high - low
                atr = df["atr"].iloc[i_idx] if "atr" in df.columns else candle_size

                # volatilité qualitative
                if atr <= 0:
                    ctx["volatility"] = "inconnu"
                else:
                    r = candle_size / atr
                    ctx["volatility"] = "faible" if r < 0.8 else "normale" if r < 1.5 else "élevée"

                # position in range
                if high == low:
                    ctx["range_position"] = "inconnu"
                else:
                    rel = (close - low) / (high - low)
                    ctx["range_position"] = "bas" if rel < 0.33 else "milieu" if rel < 0.66 else "haut"

                # volume
                if "volume_zscore" in df.columns:
                    try:
                        vz = float(df["volume_zscore"].iloc[i_idx])
                        ctx["volume_zscore"] = vz
                        ctx["volume_anomaly"] = abs(vz) > 2
                    except Exception:
                        ctx["volume_zscore"] = None
                        ctx["volume_anomaly"] = False
                else:
                    ctx["volume_zscore"] = None
                    ctx["volume_anomaly"] = False

                # structure proximity
                for key in ("ob_zone", "fvg", "bos"):
                    ctx[f"near_{key}"] = key in df.columns and not pd.isna(df[key].iloc[i_idx])
                ctx["high_confluence"] = sum(1 for k in ("ob_zone", "fvg", "bos") if ctx.get(f"near_{k}", False)) >= 2

                # phase
                if "phase" in df.columns:
                    ctx["phase"] = str(df["phase"].iloc[i_idx])

            except Exception as e:
                LOG.debug("PatternEngine._compute_context_at error at %d: %s", i_idx, e)
            return ctx

        # ---------- 1) Candles (aligned, one dict or None per index) ----------
        candle_signals = [None] * n
        for i in range(n):
            try:
                s = detect_single_candle(df, i, patterns=self.patterns)
                candle_signals[i] = s if s else None
            except Exception as e:
                LOG.error("PatternEngine.analyze: detect_single_candle error idx=%d -> %s", i, e)
                candle_signals[i] = None

        # ---------- 2) Multi-candle (aligned, list per index or None) ----------
        multi_signals: List[Optional[List[Dict[str, Any]]]] = [None] * n
        for i in range(n):
            try:
                res = detect_multi_candle(df, i, patterns=self.patterns)  # returns list (0..m) for that index
                multi_signals[i] = res if res else None
            except Exception as e:
                LOG.error("PatternEngine.analyze: detect_multi_candle error idx=%d -> %s", i, e)
                multi_signals[i] = None

        # ---------- 3) Combo signals (use detect_combos which should be aligned already) ----------
        combo_raw = detect_combos(df, patterns=self.patterns) if with_combo else [None] * n
        combo_signals = _ensure_len(combo_raw, "combo_signals")

        # ---------- 4) Orderflow (aligned) ----------
        orderflow_raw = detect_orderflow(df, patterns=self.patterns) if self.enable_orderflow else [None] * n
        orderflow_signals = _ensure_len(orderflow_raw, "orderflow_signals")

        # ---------- 5) Enrichissements appliqués localement (pour garder robustesse) ----------
        # Context & structure & multi-tf confirmation: appliqués sur chaque signal individuel présent
        for i in range(n):
            ctx = _compute_context_at(i)

            # structure snippet
            struct = {
                "near_ob": "ob_zone" in df.columns and not pd.isna(df["ob_zone"].iloc[i]),
                "near_fvg": "fvg" in df.columns and not pd.isna(df["fvg"].iloc[i]),
                "near_bos": "bos" in df.columns and not pd.isna(df["bos"].iloc[i]),
            }

            # a) enrich combos (combo_signals may be list of dicts or None)
            if combo_signals[i]:
                try:
                    enriched_list = []
                    for elem in combo_signals[i]:
                        # defensive copy
                        e = dict(elem)
                        if self.enable_context:
                            e["context"] = ctx
                        if self.enable_structure:
                            e["structure"] = struct
                        # multi-tf quick confirmation (pattern_m5/pattern_m15 presence)
                        confirmed = []
                        for tf in ("pattern_m5", "pattern_m15"):
                            if tf in df.columns and e.get("pattern") and df[tf].iloc[i] == e.get("pattern"):
                                confirmed.append(tf.upper())
                        if confirmed:
                            e["confirmed_tf"] = confirmed
                            e["is_multi_tf_confirmed"] = True
                        else:
                            e.setdefault("confirmed_tf", [])
                            e.setdefault("is_multi_tf_confirmed", False)
                        enriched_list.append(e)
                    combo_signals[i] = enriched_list
                except Exception as ex:
                    LOG.error("PatternEngine.analyze: error enriching combo at idx=%d -> %s", i, ex)
                    # leave original

            # b) enrich multi_signals list-of-patterns
            if multi_signals[i]:
                try:
                    enriched_multi = []
                    for elem in multi_signals[i]:
                        e = dict(elem)
                        if self.enable_context:
                            e["context"] = ctx
                        if self.enable_structure:
                            e["structure"] = struct
                        # confirmed tf
                        confirmed = []
                        for tf in ("pattern_m5", "pattern_m15"):
                            if tf in df.columns and e.get("pattern") and df[tf].iloc[i] == e.get("pattern"):
                                confirmed.append(tf.upper())
                        if confirmed:
                            e["confirmed_tf"] = confirmed
                            e["is_multi_tf_confirmed"] = True
                        else:
                            e.setdefault("confirmed_tf", [])
                            e.setdefault("is_multi_tf_confirmed", False)
                        enriched_multi.append(e)
                    multi_signals[i] = enriched_multi
                except Exception as ex:
                    LOG.error("PatternEngine.analyze: error enriching multi at idx=%d -> %s", i, ex)

            # c) optionally attach simple context to single candle signals (if present)
            if candle_signals[i]:
                try:
                    e = dict(candle_signals[i])
                    if self.enable_context:
                        e["context"] = ctx
                    if self.enable_structure:
                        e["structure"] = struct
                    # confirmed tf for single candle
                    confirmed = []
                    for tf in ("pattern_m5", "pattern_m15"):
                        if tf in df.columns and e.get("pattern") and df[tf].iloc[i] == e.get("pattern"):
                            confirmed.append(tf.upper())
                    if confirmed:
                        e["confirmed_tf"] = confirmed
                        e["is_multi_tf_confirmed"] = True
                    candle_signals[i] = e
                except Exception as ex:
                    LOG.debug("PatternEngine.analyze: failed to enrich candle idx=%d -> %s", i, ex)

            # d) orderflow signals enrichment (attach timestamp/index if missing)
            if orderflow_signals[i]:
                try:
                    of = dict(orderflow_signals[i])
                    of.setdefault("index", i)
                    of.setdefault("timestamp", str(df.index[i]) if hasattr(df.index, "dtype") else None)
                    orderflow_signals[i] = of
                except Exception:
                    pass

            # detailed log per-index
            if detailed:
                try:
                    LOG.info(
                        "PatternEngine[IDX=%d] candle=%s | multi=%s | combo=%s | of=%s",
                        i,
                        bool(candle_signals[i]),
                        bool(multi_signals[i]),
                        bool(combo_signals[i]),
                        bool(orderflow_signals[i]),
                    )
                except Exception:
                    pass

        # ---------- 6) Meta / Résumé ----------
        def _count_entries(aligned_list):
            if aligned_list is None:
                return 0
            cnt = 0
            for item in aligned_list:
                if item is None:
                    continue
                # if list (multiple patterns) count elements, if dict count 1
                if isinstance(item, list):
                    cnt += len(item)
                else:
                    cnt += 1
            return cnt

        meta = {
            "nb_rows": n,
            "last_timestamp": str(df.index[-1]) if n and hasattr(df.index, "__len__") else None,
            "summary": {
                "candles": _count_entries(candle_signals),
                "multi": _count_entries(multi_signals),
                "combos": _count_entries(combo_signals),
                "orderflow": _count_entries(orderflow_signals),
            },
        }

        return {
            "candle_signals": candle_signals,
            "multi_signals": multi_signals,
            "combo_signals": combo_signals,
            "orderflow_signals": orderflow_signals,
            "meta": meta,
        }

    def latest_signal(self, df: pd.DataFrame, prefer_combo: bool = True, prefer_orderflow: bool = False) -> Optional[Dict[str, Any]]:
        """
        Retourne le dernier signal pertinent (desk mode).
        Priorité : combo > orderflow > multi > candle
        """
        results = self.analyze(df, with_combo=prefer_combo)

        if prefer_combo and results["combo_signals"]:
            return results["combo_signals"][-1]
        if prefer_orderflow and results["orderflow_signals"]:
            return results["orderflow_signals"][-1]
        if results["multi_signals"]:
            return results["multi_signals"][-1]
        if results["candle_signals"]:
            return results["candle_signals"][-1]
        return None

    def trace_pipeline(self, df: pd.DataFrame) -> None:
        """
        Trace détaillée (mode desk) du pipeline.
        """
        results = self.analyze(df, with_combo=True)

        print("============================================================")
        print("🔍 TRACE PATTERN ENGINE (mode Dev Desk)")
        for i, sig in enumerate(results["combo_signals"]):
            if sig:
                print(
                    f"[{i}] {sig.get('timestamp','?')} | "
                    f"{sig.get('pattern','-')} | "
                    f"type={sig.get('signal_type','?')} | "
                    f"bullish={sig.get('is_bullish','?')} | "
                    f"OB={sig.get('near_ob',False)} FVG={sig.get('near_fvg',False)} BOS={sig.get('near_bos',False)} | "
                    f"TF_conf={sig.get('confirmed_tf',[])}"
                )
        if results["orderflow_signals"]:
            print("--- ORDERFLOW ---")
            for i, sig in enumerate(results["orderflow_signals"]):
                if sig:
                    print(
                        f"[{i}] {sig.get('timestamp','?')} | "
                        f"{sig.get('orderflow_pattern','-')} | "
                        f"imbalance={sig.get('imbalance_pct','?')} | "
                        f"dominance={sig.get('dominance','?')}"
                    )
        print("============================================================")
        
