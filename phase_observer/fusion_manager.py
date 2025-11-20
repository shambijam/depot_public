# phase_observer/fusion_manager.py
from __future__ import annotations
from typing import Dict, Any, Optional, Tuple, List
import time, logging, ast

DirectionInt = int  # -1 SELL, 0 NEUTRAL, +1 BUY
LOG = logging.getLogger(__name__)

# --- DEBUG PROBE (active par défaut, coupe avec env FUSION_PROBE=0) ---
import os

FUSION_PROBE = os.getenv("FUSION_PROBE", "1") == "1"


def _probe(logger, msg, *args):
    try:
        if FUSION_PROBE and logger:
            logger.info(msg, *args)
    except Exception:
        pass


def _get_thresholds(cfg: dict):
    cfg = cfg or {}
    th = (cfg.get("scoring_thresholds") or {}) if isinstance(cfg, dict) else {}
    return {
        "cautious": float(th.get("cautious", th.get("direct", 0.55))),
        "moderate": float(th.get("moderate", 0.65)),
        "high": float(th.get("high", 0.80)),
        "conditional": float(th.get("conditional", 0.35)),
        "allow_conditional": bool(cfg.get("allow_conditional_entries", True)),
    }


def _to_float(x, default=None):
    try:
        return float(x)
    except Exception:
        return default


def _dir_from_sign(x: float) -> DirectionInt:
    return 1 if x > 0 else (-1 if x < 0 else 0)


def _now_ts() -> float:
    try:
        return time.time()
    except Exception:
        return 0.0


# ---------- Helpers horodatage ----------
def _ensure_timestamp(
    self, node: Dict[str, Any], ctx: Dict[str, Any]
) -> Dict[str, Any]:
    if node.get("ts") is None:
        node["ts"] = ctx.get("now_ts") or _now_ts()
        node["ts_source"] = "generated"
    else:
        node["ts_source"] = "input"
    return node


# ---------- Hash & cache cohérence ----------
def _hashable(self, obj: Any) -> str:
    try:
        import json

        return json.dumps(obj, sort_keys=True, default=str)
    except Exception:
        return str(obj)


def _coh_key(
    self, n_of: Dict[str, Any], n_fp: Dict[str, Any], n_tr: Dict[str, Any]
) -> str:
    import hashlib

    base = "|".join([self._hashable(n_of), self._hashable(n_fp), self._hashable(n_tr)])
    return hashlib.sha1(base.encode("utf-8")).hexdigest()


def _coherence_cached(self, key: str, compute_fn) -> Dict[str, Any]:
    now = _now_ts()
    hit = self._coh_cache.get(key)
    if hit and (now - hit[0]) <= self._coh_cache_ttl:
        return hit[1]
    res = compute_fn()
    # clamp taille cache à 12
    if len(self._coh_cache) > 12:
        self._coh_cache.pop(next(iter(self._coh_cache)))
    self._coh_cache[key] = (now, res)
    return res


# ---------- Mode dégradé ----------
def _degraded_mode_decision(
    self, n_of: Dict[str, Any], n_fp: Dict[str, Any], n_tr: Dict[str, Any]
) -> Dict[str, Any]:
    available = {
        "orderflow": n_of["score"] > 0 or n_of["dir"] != 0,
        "footprint": True,  # on a toujours un status ; score heuristique si absent
        "triggers": n_tr["dir"] != 0 or n_tr["score"] > 0,
    }
    return {"available": available, "is_degraded": not available["triggers"]}


# ---------- Validation croisée ----------
def _cross_system_validation(
    self, n_of: Dict[str, Any], n_fp: Dict[str, Any], n_tr: Dict[str, Any]
) -> List[str]:
    issues: List[str] = []
    try:
        if (
            abs(float(n_of.get("delta_total", 0))) > 100
            and abs(float(n_fp.get("delta_total", 0))) < 10
        ):
            issues.append("delta_mismatch_of_vs_fp")
    except Exception:
        pass
    # Exemple : trigger SELL mais OF très bullish
    if n_tr["dir"] < 0 and (n_of["dir"] > 0 and n_of["score"] >= 0.7):
        issues.append("trigger_vs_strong_OF_conflict")
    return issues


# ---------- Poids adaptatifs ----------
def _adaptive_weights(
    self, regime: Optional[str], volatility: Optional[str], session: Optional[str]
) -> Optional[Dict[str, float]]:
    # Valeurs par défaut None → pas d’override
    if not (regime or volatility or session):
        return None
    w_tr, w_of, w_fp = 0.50, 0.30, 0.20
    # Volatilité élevée → renforcer orderflow
    if (volatility or "").lower() in ("high", "elevated", "high_volatility"):
        w_tr, w_of, w_fp = 0.45, 0.40, 0.15
    # Trending → renforcer orderflow ; Range → renforcer footprint (structure)
    if (regime or "").lower().startswith("trend"):
        w_of += 0.05
        w_tr -= 0.03
        w_fp -= 0.02
    elif (regime or "").lower().startswith("range"):
        w_fp += 0.05
        w_tr -= 0.03
        w_of -= 0.02
    # Session London → triggers réactifs ; Asia → footprint/structure
    s = (session or "").lower()
    if "london" in s or "europe" in s:
        w_tr += 0.03
        w_of += 0.00
        w_fp -= 0.03
    elif "asia" in s:
        w_fp += 0.03
        w_tr -= 0.02
        w_of -= 0.01

    # Normalise
    total = max(1e-9, w_tr + w_of + w_fp)
    w_tr, w_of, w_fp = w_tr / total, w_of / total, w_fp / total
    return {"trigger": w_tr, "orderflow": w_of, "footprint": w_fp}


# ---------- Maj métriques ----------
def _update_metrics(self, dt: float, decision: str, fused: float):
    try:
        m = self.metrics
        n = m["decisions_taken"] + 1
        m["decisions_taken"] = n
        # moyenne glissante simple
        m["avg_processing_time"] = ((n - 1) * m["avg_processing_time"] + dt) / n
        m["confidence_distribution"].append(float(fused))
        # cap distribution à 500
        if len(m["confidence_distribution"]) > 500:
            m["confidence_distribution"] = m["confidence_distribution"][-500:]
    except Exception:
        pass


class FusionManager:
    """
    Orchestration modulaire (OFv6 + Footprint + Triggers).
    Sortie :
      {
        "ok": bool,
        "action": "BUY"|"SELL"|"HOLD",
        "signal_type": "HIGH_CONVICTION_*" | "MODERATE_*" | "CAUTIOUS_*" | "WAIT_CONFIRMATION" | "INSUFFICIENT_DATA",
        "direction": "BUY"|"SELL"|"NEUTRAL",
        "fused_confidence": float(0..1),
        "anchor_price": float|None,
        "rationale": str,
        "components": {"orderflow":..., "validator":..., "trigger":...},
        "consensus": {"maj": "BUY/SELL/TIE", "agreement": float, "votes":[...]},
        "quality": {"is_valid": bool, "quality_score": float, "missing":[], "warnings":[]},
        "suggested_trailing": {"distance": float, "unit": "price", "note": str}
      }
    """

    def __init__(self, logger: Optional[logging.Logger] = None):
        self.log = logger or LOG
        # Metrics runtime
        self.metrics = {
            "decisions_taken": 0,
            "avg_processing_time": 0.0,
            "confidence_distribution": [],
        }
        # Mini-cache cohérence (TTL en secondes)
        self._coh_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}

    def _log_consolidated_report(
        self,
        asset: str,
        n_tr: Dict[str, Any],
        n_of: Dict[str, Any],
        n_fp: Dict[str, Any],
        fused: float,
        decision: Dict[str, Any],
        weights: Dict[str, float],
    ):
        """
        📊 BILAN CONSOLIDÉ : Rapport unifié montrant comment les 3 fonctions travaillent ensemble.

        Architecture du flux de données :

        1️⃣ FOOTPRINT M1 (Validation institutionnelle)
           → Agrège ticks par niveau de prix
           → Calcule buy/sell/delta/POC
           → OUTPUT: DataFrame avec volumes réels

        2️⃣ ORDERFLOW V6 (Analyse Volume Profile)
           → Lit barres M1 (avec tick_volume)
           → Calcule VPOC, VA, HVN/LVN, imbalances
           → OUTPUT: Score directional + metrics

        3️⃣ FOOTPRINT TRIGGERS (Détection patterns)
           → UTILISE le DataFrame de Footprint M1
           → Cherche STACKING/ABSORPTION/CLIMAX
           → OUTPUT: Pattern détecté + confidence

        4️⃣ FUSION (Synthèse pondérée)
           → Score = w_trigger × trigger + w_orderflow × OF + w_footprint × FP
           → Décision finale (BUY/SELL/HOLD)
        """
        try:
            # Symboles directionnels
            dir_symbols = {1: "🟢 BUY", -1: "🔴 SELL", 0: "⚪ NEUTRAL"}

            # En-tête
            self.log.info("=" * 80)
            self.log.info(f"📊 BILAN CONSOLIDÉ - {asset}")
            self.log.info("=" * 80)

            # Section 1 : Inputs bruts
            self.log.info("┌─────────────────────────────────────────────────────────────────────┐")
            self.log.info("│ 1️⃣  FOOTPRINT M1 (Validation Institutionnelle)                      │")
            self.log.info("├─────────────────────────────────────────────────────────────────────┤")
            fp_status = n_fp.get("status", "UNKNOWN")
            fp_score = n_fp.get("score", 0.0)
            fp_dir = dir_symbols.get(n_fp.get("dir", 0), "⚪ NEUTRAL")
            fp_delta = n_fp.get("delta_total", 0.0)
            fp_poc = n_fp.get("poc", None)
            fp_absorption = n_fp.get("absorption", False)

            # Extraire métriques détaillées depuis raw
            fp_raw = n_fp.get("raw", {})
            fp_summary = fp_raw.get("summary", {})
            if isinstance(fp_summary, str):
                try:
                    import ast
                    fp_summary = ast.literal_eval(fp_summary)
                except:
                    fp_summary = {}

            # Métriques ticks
            tick_count = fp_summary.get("tick_count", 0)
            coverage_s = fp_summary.get("coverage_s", 0.0)
            tick_rate = fp_summary.get("tick_rate", 0.0)

            # Métriques buy/sell
            buy_vol = fp_summary.get("buy_volume", 0.0)
            sell_vol = fp_summary.get("sell_volume", 0.0)
            total_vol = fp_summary.get("total_volume", buy_vol + sell_vol)
            buy_pct = (buy_vol / total_vol * 100) if total_vol > 0 else 50.0
            sell_pct = (sell_vol / total_vol * 100) if total_vol > 0 else 50.0

            self.log.info(f"│ Status     : {fp_status:<15} Score    : {fp_score:>6.2%}           │")
            self.log.info(f"│ Direction  : {fp_dir:<18} Delta    : {fp_delta:>8.1f}         │")
            self.log.info(f"│ POC Price  : {fp_poc if fp_poc else 'N/A':<20} Absorption: {'✅ YES' if fp_absorption else '❌ NO':<10}│")
            self.log.info(f"│                                                                     │")
            self.log.info(f"│ 📊 Ticks Metrics:                                                   │")
            self.log.info(f"│   • Tick Count : {tick_count:>6} ticks    Coverage: {coverage_s:>6.1f}s           │")
            self.log.info(f"│   • Tick Rate  : {tick_rate:>6.2f} ticks/s                               │")
            self.log.info(f"│                                                                     │")
            self.log.info(f"│ 📈 Volume Distribution:                                             │")
            self.log.info(f"│   • Buy Volume : {buy_vol:>8.1f} ({buy_pct:>5.1f}%)                           │")
            self.log.info(f"│   • Sell Volume: {sell_vol:>8.1f} ({sell_pct:>5.1f}%)                           │")
            self.log.info(f"│   • Total      : {total_vol:>8.1f}                                       │")
            self.log.info("└─────────────────────────────────────────────────────────────────────┘")

            self.log.info("┌─────────────────────────────────────────────────────────────────────┐")
            self.log.info("│ 2️⃣  ORDERFLOW V6 (Analyse Volume Profile)                           │")
            self.log.info("├─────────────────────────────────────────────────────────────────────┤")
            of_score = n_of.get("score", 0.0)
            of_dir = dir_symbols.get(n_of.get("dir", 0), "⚪ NEUTRAL")
            of_delta = n_of.get("delta_total", 0.0)
            of_absorption = n_of.get("absorption", False)
            of_status = n_of.get("status", "UNKNOWN")
            of_poc = n_of.get("poc", None)

            # Extraire métriques OrderFlow depuis raw
            of_raw = n_of.get("raw", {})
            of_summary = of_raw.get("summary", {})
            if isinstance(of_summary, str):
                try:
                    import ast
                    of_summary = ast.literal_eval(of_summary)
                except:
                    of_summary = {}

            # Métriques avancées
            imbalance = of_summary.get("imbalance", 0.0)
            cvd_slope = of_summary.get("cvd_slope", 0.0)
            bias = of_summary.get("bias", "NEUTRAL")

            # Volume Profile nodes
            vah = of_summary.get("vah", None)  # Value Area High
            val = of_summary.get("val", None)  # Value Area Low
            vpoc = of_summary.get("vpoc_price", of_poc)

            # HVN/LVN counts
            hvn_count = len(of_summary.get("hvn_levels", []))
            lvn_count = len(of_summary.get("lvn_levels", []))

            self.log.info(f"│ Status     : {of_status:<15} Score    : {of_score:>6.2%}           │")
            self.log.info(f"│ Direction  : {of_dir:<18} Bias     : {bias:<10}      │")
            self.log.info(f"│ Delta Total: {of_delta:>8.1f}         Absorption: {'✅ YES' if of_absorption else '❌ NO':<10}│")
            self.log.info(f"│                                                                     │")
            self.log.info(f"│ 📊 Volume Profile:                                                  │")
            self.log.info(f"│   • VPOC       : {vpoc if vpoc else 'N/A':<15}                              │")
            self.log.info(f"│   • VAH (70%)  : {vah if vah else 'N/A':<15}                              │")
            self.log.info(f"│   • VAL (30%)  : {val if val else 'N/A':<15}                              │")
            self.log.info(f"│                                                                     │")
            self.log.info(f"│ 📈 Orderflow Metrics:                                               │")
            self.log.info(f"│   • Imbalance  : {imbalance:>6.3f}      (déséquilibre buy/sell)         │")
            self.log.info(f"│   • CVD Slope  : {cvd_slope:>6.3f}      (pente delta cumulé)           │")
            self.log.info(f"│   • HVN Nodes  : {hvn_count:>2}           (zones haute densité)          │")
            self.log.info(f"│   • LVN Nodes  : {lvn_count:>2}           (zones basse densité)          │")
            self.log.info("└─────────────────────────────────────────────────────────────────────┘")

            self.log.info("┌─────────────────────────────────────────────────────────────────────┐")
            self.log.info("│ 3️⃣  FOOTPRINT TRIGGERS (Patterns - Utilise Footprint M1)            │")
            self.log.info("├─────────────────────────────────────────────────────────────────────┤")
            tr_score = n_tr.get("score", 0.0)
            tr_dir = dir_symbols.get(n_tr.get("dir", 0), "⚪ NEUTRAL")
            tr_type = n_tr.get("type", "none")
            tr_conf = tr_score  # confidence = score normalisé
            tr_anchor = n_tr.get("anchor", None)

            # Extraire métriques Trigger depuis raw
            tr_raw = n_tr.get("raw", {})

            # Détails du pattern détecté
            pattern_action = tr_raw.get("action", "HOLD")
            pattern_direction = tr_raw.get("direction", "NEUTRAL")
            pattern_meta = tr_raw.get("meta", {})

            # Fenêtre utilisée pour détection
            window_used = pattern_meta.get("used_window_s", "N/A")

            # Métriques du snapshot utilisé
            snapshot_stats = pattern_meta.get("snapshot_stats", {})
            levels_count = snapshot_stats.get("levels_count", 0)
            delta_ratio_mean = snapshot_stats.get("delta_ratio_mean", 0.0)
            volume_zscore_max = snapshot_stats.get("volume_zscore_max", 0.0)

            # Type de pattern détecté (STACKING, ABSORPTION, CLIMAX)
            trigger_type_detail = tr_raw.get("trigger_type", tr_type)

            # Reason si échec
            reason = tr_raw.get("reason", "")

            # Emoji selon pattern
            pattern_emoji = {
                "stacking": "📚",
                "absorption": "🛡️",
                "climax": "💥",
                "micro_stack": "📖",
                "micro_absorption": "🛡",
                "none": "⚪",
            }.get(tr_type.lower(), "❓")

            self.log.info(f"│ Pattern    : {pattern_emoji} {tr_type:<17} Confidence: {tr_conf:>6.2%}      │")
            self.log.info(f"│ Direction  : {tr_dir:<18} Action   : {pattern_action:<10}      │")
            self.log.info(f"│ Anchor     : {tr_anchor if tr_anchor else 'N/A':<20}                             │")
            self.log.info(f"│                                                                     │")

            if tr_type != "none" and levels_count > 0:
                self.log.info(f"│ 📊 Detection Metrics:                                               │")
                self.log.info(f"│   • Window Used: {window_used}s                                             │")
                self.log.info(f"│   • Price Levels: {levels_count:<3}     (niveaux analysés)               │")
                self.log.info(f"│   • Delta Ratio : {delta_ratio_mean:>5.3f}      (force directionnelle)        │")
                self.log.info(f"│   • Vol Z-Score : {volume_zscore_max:>5.2f}      (écart-type volume)          │")
            else:
                self.log.info(f"│ ❌ Aucun pattern détecté                                            │")
                if reason:
                    self.log.info(f"│    Reason: {reason:<55}│")

            self.log.info("└─────────────────────────────────────────────────────────────────────┘")

            # Section 2 : Fusion pondérée
            self.log.info("┌─────────────────────────────────────────────────────────────────────┐")
            self.log.info("│ 4️⃣  FUSION PONDÉRÉE (Synthèse)                                      │")
            self.log.info("├─────────────────────────────────────────────────────────────────────┤")
            w_tr = weights.get("trigger", 0.5)
            w_of = weights.get("orderflow", 0.25)
            w_fp = weights.get("footprint", 0.25)
            contrib_tr = w_tr * tr_score
            contrib_of = w_of * of_score
            contrib_fp = w_fp * fp_score

            self.log.info(f"│ Pondérations: Trigger={w_tr:.0%}  OrderFlow={w_of:.0%}  Footprint={w_fp:.0%}   │")
            self.log.info(f"│ Contributions:                                                      │")
            self.log.info(f"│   • Trigger    : {w_tr:.2f} × {tr_score:.2f} = {contrib_tr:>5.3f}                      │")
            self.log.info(f"│   • OrderFlow  : {w_of:.2f} × {of_score:.2f} = {contrib_of:>5.3f}                      │")
            self.log.info(f"│   • Footprint  : {w_fp:.2f} × {fp_score:.2f} = {contrib_fp:>5.3f}                      │")
            self.log.info(f"│ ───────────────────────────────────────────────────────────────────│")
            self.log.info(f"│ Score Fusionné : {fused:>5.3f} ({fused*100:>5.1f}%)                                 │")
            self.log.info("└─────────────────────────────────────────────────────────────────────┘")

            # Section 3 : Décision finale
            self.log.info("┌─────────────────────────────────────────────────────────────────────┐")
            self.log.info("│ 🎯 DÉCISION FINALE                                                   │")
            self.log.info("├─────────────────────────────────────────────────────────────────────┤")
            action = decision.get("action", "HOLD")
            signal = decision.get("signal_type", "UNKNOWN")
            direction = decision.get("direction", "NEUTRAL")

            # Emoji selon action
            action_emoji = {"BUY": "🟢", "SELL": "🔴", "HOLD": "⏸️"}.get(action, "⚪")

            # Seuils
            if fused >= 0.80:
                level = "HIGH_CONVICTION (≥80%)"
            elif fused >= 0.70:
                level = "MODERATE (≥70%)"
            elif fused >= 0.55:
                level = "CAUTIOUS (≥55%)"
            else:
                level = "INSUFFISANT (<55%)"

            self.log.info(f"│ Action  : {action_emoji} {action:<15}                                       │")
            self.log.info(f"│ Signal  : {signal:<45}│")
            self.log.info(f"│ Level   : {level:<45}│")
            self.log.info(f"│ Direction: {direction:<45}│")
            self.log.info("└─────────────────────────────────────────────────────────────────────┘")

            # Section 4 : Flux de données
            self.log.info("┌─────────────────────────────────────────────────────────────────────┐")
            self.log.info("│ 🔄 FLUX DE DONNÉES                                                   │")
            self.log.info("├─────────────────────────────────────────────────────────────────────┤")
            self.log.info("│ Barres M1 (tick_volume) ──┬──→ OrderFlow V6 → Score + VPOC         │")
            self.log.info("│                           │                                         │")
            self.log.info("│ Ticks récents ────────────┴──→ Footprint M1 → DataFrame            │")
            self.log.info("│                                     ↓                               │")
            self.log.info("│                              Footprint Triggers → Pattern           │")
            self.log.info("│                                                                     │")
            self.log.info("│ Fusion Manager ← (Trigger + OrderFlow + Footprint) → Décision      │")
            self.log.info("└─────────────────────────────────────────────────────────────────────┘")

            self.log.info("=" * 80)

        except Exception as e:
            self.log.error(f"[FUSION] Erreur génération rapport consolidé: {e}", exc_info=True)
        self._coh_cache_ttl: float = 5.0

    # -------------- Public API --------------
    def fuse(
        self,
        orderflow: Dict[str, Any],
        footprint: Dict[str, Any],
        triggers: Optional[Dict[str, Any]],
        strategy_config: Optional[Dict[str, Any]] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        cfg = (strategy_config or {}).get("fusion", {}) or {}
        ctx = context or {}

        # 1) Validation / qualité
        quality = self._validate_inputs(
            orderflow or {}, footprint or {}, triggers or {}
        )
        if not quality["is_valid"]:
            return self._mk_hold(
                signal_type="INSUFFICIENT_DATA",
                rationale=f"inputs invalides: missing={quality['missing']}, warnings={quality['warnings']}",
                quality=quality,
            )

        t0 = _now_ts()
        # 2) Normalisation compacte
        n_of = self._normalize_orderflow(orderflow)
        n_fp = self._normalize_footprint(footprint)
        n_tr = self._normalize_trigger(triggers or {})

        # timestamps garantis
        n_of = self._ensure_timestamp(n_of, ctx)
        n_fp = self._ensure_timestamp(n_fp, ctx)
        n_tr = self._ensure_timestamp(n_tr, ctx)

        # validation croisée → warnings qualité
        x_issues = self._cross_system_validation(n_of, n_fp, n_tr)
        if x_issues:
            quality["warnings"].extend([f"csv:{w}" for w in x_issues])

        # 3) Cohérence (direction pondérée + matrice simple)
        _key = self._coh_key(n_of, n_fp, n_tr)
        coherence = self._coherence_cached(
            _key, lambda: self._analyze_coherence(n_of, n_fp, n_tr, ctx)
        )
        degraded = self._degraded_mode_decision(n_of, n_fp, n_tr)
        if degraded["is_degraded"]:
            quality["warnings"].append("degraded_mode_no_triggers")

        # 4) Règles métier scalping
        rules_eval = self._apply_business_rules(n_of, n_fp, n_tr, coherence, cfg, ctx)

        # 5) Confiance fusionnée (pondération + bonus cohérence − malus conflit)
        fused = self._calculate_fused_confidence(
            n_of, n_fp, n_tr, coherence, quality, cfg, ctx, rules_eval
        )

        # === DEBUG 1C: DUMP normalized inputs ===
        try:
            of_sum = (n_of.get("summary") or {}) if isinstance(n_of, dict) else {}
            fp_sum = (n_fp.get("summary") or {}) if isinstance(n_fp, dict) else {}
            _probe(
                self.logger,
                "[FUSION/DUMP] OF(score=%.2f, |Δ|=%.1f, bias=%s) | FP(score=%.2f, tickrate=%.2f/s, cov=%.2fs) | TR(dir=%s, conf=%.2f) | coherence=%.3f | fused=%.3f | quality=%s",
                float((n_of.get("score") if isinstance(n_of, dict) else 0.0) or 0.0),
                float(abs((of_sum.get("delta_total") or 0.0))),
                str(n_of.get("bias") if isinstance(n_of, dict) else None),
                float((n_fp.get("score") if isinstance(n_fp, dict) else 0.0) or 0.0),
                float((fp_sum.get("tick_rate") or 0.0)),
                float((fp_sum.get("coverage_s") or 0.0)),
                str((n_tr.get("direction") if isinstance(n_tr, dict) else None)),
                float(
                    (n_tr.get("confidence") if isinstance(n_tr, dict) else 0.0) or 0.0
                ),
                float((coherence or 0.0)),
                float((fused or 0.0)),
                str(quality),
            )
        except Exception:
            pass

        # 7) Génération décision (catégories + action BUY/SELL/HOLD)
        decision = self._final_decision("AUTO", fused, n_tr, coherence, cfg)

        # 9) Rationale
        rationale = self._rationale(decision, n_of, n_fp, n_tr, coherence, rules_eval)
        # metrics update
        self._update_metrics(
            dt=_now_ts() - t0, decision=decision["action"], fused=fused
        )

        # consensus texte BUY/SELL/TIE
        maj = coherence["majority"]
        maj_str = "BUY" if maj > 0 else ("SELL" if maj < 0 else "TIE")

        # ok=True seulement si action != HOLD ET fused >= seuil minimum (0.55 CAUTIOUS)
        # Cela évite que des signaux faibles (< 55%) soient exécutés
        is_actionable = decision["action"] != "HOLD" and fused >= 0.55

        # 📊 BILAN CONSOLIDÉ : Rapport unifié des 3 fonctions
        # Récupérer les poids utilisés pour la fusion
        adaptive_w = self._compute_adaptive_weights(
            ctx.get("regime"), ctx.get("volatility"), ctx.get("session")
        )
        if adaptive_w:
            weights_used = adaptive_w
        else:
            # Lire depuis config ou utiliser défauts
            p = cfg.get("ponderations", {})
            weights_used = {
                "trigger": _to_float(p.get("trigger_weight"), 0.50),
                "orderflow": _to_float(p.get("orderflow_weight"), 0.25),
                "footprint": _to_float(p.get("footprint_weight"), 0.25),
            }

        # Appeler le rapport consolidé (actif seulement si FUSION_PROBE=1)
        if FUSION_PROBE:
            asset_name = ctx.get("asset", "UNKNOWN")
            self._log_consolidated_report(
                asset=asset_name,
                n_tr=n_tr,
                n_of=n_of,
                n_fp=n_fp,
                fused=fused,
                decision=decision,
                weights=weights_used,
            )

        return {
            "ok": is_actionable,
            "action": decision["action"],
            "signal_type": decision["signal_type"],
            "direction": decision["direction"],
            "fused_confidence": round(float(fused), 3),
            "anchor_price": decision["anchor_price"],
            "rationale": rationale,
            "components": {"orderflow": n_of, "validator": n_fp, "trigger": n_tr},
            "consensus": {
                "maj": maj_str,
                "agreement": round(coherence["agreement"], 3),
                "votes": coherence["votes"],
            },
            "quality": quality,
        }

    # -------------- 1) Input Validator --------------
    def _validate_inputs(
        self, of: Dict[str, Any], fp: Dict[str, Any], tr: Dict[str, Any]
    ) -> Dict[str, Any]:
        missing, warnings = [], []

        def _req(d, path, keys):
            miss = []
            for k in keys:
                if d.get(k) is None:
                    miss.append(f"{path}.{k}")
            return miss

        # schémas minimaux
        missing += _req(of, "orderflow", ["score", "status"])
        missing += _req(fp, "footprint", ["status"])
        # triggers optionnels mais recommandés
        if not tr or (tr.get("direction") is None and tr.get("action") is None):
            warnings.append("trigger.missing_direction")

        # Anti‐NaN / types incohérents
        for name, d in [("orderflow", of), ("footprint", fp)]:
            try:
                _ = str(d.get("status", ""))
            except Exception:
                warnings.append(f"{name}.status_bad_type")
            sc = d.get("score")
            if sc is not None:
                try:
                    _ = float(sc)
                except Exception:
                    warnings.append(f"{name}.score_bad_type")

        # score de qualité naïf (FIX: pénaliser s'il Y A des missing)
        quality_score = 1.0
        if warnings:
            quality_score -= min(0.3, 0.05 * len(warnings))
        if missing:
            quality_score -= 0.5
        quality_score = max(0.0, quality_score)

        return {
            "is_valid": len(missing) == 0,
            "quality_score": quality_score,
            "missing": missing,
            "warnings": warnings,
        }

    # -------------- Normalisations --------------
    def _normalize_orderflow(self, of: Dict[str, Any]) -> Dict[str, Any]:
        # of v6: {score:0..100, status, summary{delta_total, imbalance, cvd_slope, vpoc_price, bias, ...}}
        status = str(of.get("status", "SUSPECT")).upper()
        score01 = max(0.0, min(1.0, _to_float(of.get("score"), 0.0) / 100.0))
        if status != "VALID":
            score01 *= 0.6

        summ = of.get("summary") or {}
        if isinstance(summ, str):
            try:
                summ = ast.literal_eval(summ)
            except Exception:
                summ = {}

        delta = _to_float(summ.get("delta_total"), 0.0)

        # compat v6: 'vpoc_price' → champ interne 'poc'
        poc = _to_float(summ.get("poc", summ.get("vpoc_price", None)), None)

        # bias (BUY/SELL/NEUTRAL)
        bias_top = of.get("bias")
        bias_sum = summ.get("bias")
        bias = str(bias_top or bias_sum or "")
        if not bias:
            bias = (
                "BUY"
                if (delta or 0.0) > 0
                else ("SELL" if (delta or 0.0) < 0 else "NEUTRAL")
            )

        absorption = bool(summ.get("absorption_flag", False))
        dir_int = (
            1
            if bias == "BUY"
            else (-1 if bias == "SELL" else _dir_from_sign(delta or 0.0))
        )

        return {
            "score": score01,
            "status": status,
            "dir": dir_int,
            "delta_total": float(delta or 0.0),
            "poc": poc,
            "absorption": absorption,
            "raw": of,
        }

    def _normalize_footprint(self, fp: Dict[str, Any]) -> Dict[str, Any]:
        status = str(fp.get("status", "SUSPECT")).upper()
        score01 = None
        if fp.get("score") is not None:
            score01 = max(0.0, min(1.0, _to_float(fp.get("score"), 0.0) / 100.0))

        summ = fp.get("summary") or {}
        if isinstance(summ, str):
            try:
                summ = ast.literal_eval(summ)
            except Exception:
                summ = {}

        delta = _to_float(fp.get("delta_total"), None)
        if delta is None:
            delta = _to_float(summ.get("delta_total"), 0.0)

        poc = (
            _to_float(fp.get("poc"), None)
            if fp.get("poc") is not None
            else _to_float(summ.get("poc"), None)
        )
        absorption = bool(fp.get("absorption_flag", summ.get("absorption_flag", False)))

        if score01 is None:
            score01 = 0.7 if status == "VALID" else 0.4
            if abs(delta or 0.0) >= 1.0:
                score01 += 0.05
            score01 = max(0.0, min(1.0, score01))

        if status != "VALID":
            score01 *= 0.6

        dir_int = _dir_from_sign(delta or 0.0)

        return {
            "score": score01,
            "status": status,
            "dir": dir_int,
            "delta_total": float(delta or 0.0),
            "poc": poc,
            "absorption": absorption,
            "raw": fp,
        }

    def _normalize_trigger(self, tr: Dict[str, Any]) -> Dict[str, Any]:
        a = str(tr.get("direction") or tr.get("action") or "").upper()
        dir_int = 1 if a == "BUY" else (-1 if a == "SELL" else 0)
        conf = max(0.0, min(0.99, _to_float(tr.get("confidence"), 0.0) or 0.0))
        anchor = _to_float(tr.get("anchor_price"), None)
        ttype = str(tr.get("trigger_type") or "unknown")
        ts = _to_float(tr.get("timestamp"), None)
        return {
            "score": conf,
            "dir": dir_int,
            "anchor": anchor,
            "type": ttype,
            "ts": ts,
            "raw": tr,
        }

    # -------------- 2) Coherence Analyzer --------------
    def _analyze_coherence(
        self,
        n_of: Dict[str, Any],
        n_fp: Dict[str, Any],
        n_tr: Dict[str, Any],
        ctx: Dict[str, Any],
    ) -> Dict[str, Any]:
        votes = []
        if n_tr["dir"] != 0:
            votes.append(("trigger", n_tr["dir"], n_tr["score"]))
        if n_of["dir"] != 0:
            votes.append(("orderflow", n_of["dir"], n_of["score"]))
        if n_fp["dir"] != 0:
            votes.append(("validator", n_fp["dir"], n_fp["score"]))

        pos = sum(w for _, d, w in votes if d > 0)
        neg = sum(w for _, d, w in votes if d < 0)
        if pos > neg:
            maj = 1
        elif neg > pos:
            maj = -1
        else:
            maj = 0
        total_w = sum(w for *_, w in votes) or 1.0
        agreement = (max(pos, neg)) / total_w if total_w > 0 else 0.0

        # lead/lag (si timestamps fournis)
        now_ts = ctx.get("now_ts") or _now_ts()
        lead = {"trigger_age_s": None, "orderflow_age_s": None, "footprint_age_s": None}
        for k, n in (
            ("trigger_age_s", n_tr),
            ("orderflow_age_s", n_of),
            ("footprint_age_s", n_fp),
        ):
            ts = n.get("ts")
            if ts is not None:
                try:
                    lead[k] = max(0.0, float(now_ts) - float(ts))
                except Exception:
                    lead[k] = None

        matrix = {
            "trigger_vs_of": (
                "aligned"
                if n_tr["dir"] == n_of["dir"]
                else "conflict" if (n_tr["dir"] * n_of["dir"] < 0) else "neutral"
            ),
            "trigger_vs_fp": (
                "aligned"
                if n_tr["dir"] == n_fp["dir"]
                else "conflict" if (n_tr["dir"] * n_fp["dir"] < 0) else "neutral"
            ),
            "of_vs_fp": (
                "aligned"
                if n_of["dir"] == n_fp["dir"]
                else "conflict" if (n_of["dir"] * n_fp["dir"] < 0) else "neutral"
            ),
        }

        return {
            "majority": maj,
            "agreement": max(0.0, min(1.0, agreement)),
            "votes": [
                (n, "BUY" if d > 0 else "SELL" if d < 0 else "NEUTRAL", round(w, 3))
                for n, d, w in votes
            ],
            "matrix": matrix,
            "leadlag": lead,
        }

    # -------------- 3) Business Rules Engine --------------
    def _apply_business_rules(
        self, n_of, n_fp, n_tr, coherence, cfg, ctx
    ) -> Dict[str, Any]:
        rules = cfg.get("regles_metier", {}) or {}
        # priorités fixées par ton cahier des charges
        never_against_strong_of = bool(
            rules.get("never_against_strong_orderflow", True)
        )
        veto_absorption = bool(rules.get("veto_absorption", True))
        triple_bonus = bool(rules.get("triple_confirmation_bonus", True))
        weak_fp_penalty_th = _to_float(rules.get("weak_footprint_score_th", 0.60), 0.60)

        allow = True
        notes: List[str] = []

        # “NEVER AGAINST STRONG ORDERFLOW” (score > 0.80 → of fort)
        if never_against_strong_of and n_of["score"] >= 0.80:
            if n_tr["dir"] != 0 and n_tr["dir"] != n_of["dir"]:
                allow = False
                notes.append("AGAINST_STRONG_ORDERFLOW")

        # “ABSORPTION VETO”
        if veto_absorption and n_fp["absorption"]:
            if n_tr["dir"] != 0 and n_tr["dir"] != n_fp["dir"]:
                allow = False
                notes.append("ABSORPTION_VETO")

        # “WEAK FOOTPRINT PENALTY”
        weak_fp = n_fp["score"] < weak_fp_penalty_th
        if weak_fp:
            notes.append("WEAK_FOOTPRINT")

        # “TRIPLE CONFIRMATION BONUS” (cohérence 3/3)
        aligned3 = (
            coherence["matrix"]["trigger_vs_of"] == "aligned"
            and coherence["matrix"]["trigger_vs_fp"] == "aligned"
            and coherence["matrix"]["of_vs_fp"] == "aligned"
        )

        # Timing optimisation (lead/lag)
        timing_bonus = 0.0
        trig_age = coherence["leadlag"].get("trigger_age_s")
        if trig_age is not None and trig_age <= 5.0 and n_of["score"] >= 0.50:
            timing_bonus += 0.03
            notes.append("TIMING_OK")

        return {
            "allow": allow,
            "reasons": notes,
            "aligned3": aligned3,
            "weak_fp": weak_fp,
            "timing_bonus": timing_bonus,
            "weak_fp_penalty_th": weak_fp_penalty_th,
            "triple_bonus": triple_bonus,
        }

    # -------------- 4) Confidence Fusion System --------------
    def _calculate_fused_confidence(
        self, n_of, n_fp, n_tr, coherence, quality, cfg, ctx, rules_eval
    ) -> float:
        p = cfg.get("ponderations", {}) or {}
        w_tr = _to_float(p.get("trigger_weight"), 0.50)
        w_of = _to_float(p.get("orderflow_weight"), 0.25)
        w_fp = _to_float(p.get("footprint_weight"), 0.25)

        # Override adaptatif (si activé) basé sur contexte (regime/volatility/session)
        aw_enabled = bool((cfg.get("adaptive_weights", True)))
        if aw_enabled:
            aw = self._adaptive_weights(
                ctx.get("regime"), ctx.get("volatility"), ctx.get("session")
            )
            if aw:
                w_tr, w_of, w_fp = aw["trigger"], aw["orderflow"], aw["footprint"]

        # Normalisation des poids
        total = (w_tr or 0) + (w_of or 0) + (w_fp or 0)
        if total <= 0:
            w_tr, w_of, w_fp = 0.5, 0.25, 0.25
        else:
            w_tr, w_of, w_fp = w_tr / total, w_of / total, w_fp / total

        base = (w_tr * n_tr["score"]) + (w_of * n_of["score"]) + (w_fp * n_fp["score"])

        # Bonus cohérence 0..15%
        coh_bonus_max = _to_float(p.get("coherence_bonus_max"), 0.15)
        base *= 1.0 + coh_bonus_max * coherence["agreement"]

        # Malus conflit 0..25% si matrice montre des conflits
        matrix = coherence["matrix"]
        conflicts = sum(1 for v in matrix.values() if v == "conflict")
        conflict_malus = min(0.25, 0.10 * conflicts)
        base *= 1.0 - conflict_malus

        # Qualité des données
        q = quality.get("quality_score", 1.0)
        base *= 0.85 + 0.15 * q

        # Malus FP faible (seuil issu des règles)
        if rules_eval.get("weak_fp", False):
            base *= 0.85

        # Bonus triple-confirmation + bonus timing
        if rules_eval.get("aligned3") and rules_eval.get("triple_bonus", True):
            base *= 1.08
        base = base + rules_eval.get("timing_bonus", 0.0)

        return max(0.0, min(0.99, float(base)))

    # -------------- 6) Decision Generator --------------
    def _final_decision(
        self, mode: str, fused: float, n_tr, coherence, cfg
    ) -> Dict[str, Any]:
        # direction finale: majorité pondérée; sinon direction du trigger; sinon NEUTRAL
        maj = coherence["majority"]
        direction = (
            "BUY"
            if maj > 0
            else (
                "SELL"
                if maj < 0
                else (
                    "BUY"
                    if n_tr["dir"] > 0
                    else "SELL" if n_tr["dir"] < 0 else "NEUTRAL"
                )
            )
        )
        anchor_price = n_tr[
            "anchor"
        ]  # l’ancre du trigger reste prioritaire; POC pris plus haut si None

        if mode == "HOLD":
            return {
                "action": "HOLD",
                "signal_type": "WAIT_CONFIRMATION",
                "direction": "NEUTRAL",
                "anchor_price": anchor_price,
            }

        if fused >= 0.80 and direction in ("BUY", "SELL"):
            return {
                "action": direction,
                "signal_type": f"HIGH_CONVICTION_{direction}",
                "direction": direction,
                "anchor_price": anchor_price,
            }
        if fused >= 0.70 and direction in ("BUY", "SELL"):
            return {
                "action": direction,
                "signal_type": f"MODERATE_{direction}",
                "direction": direction,
                "anchor_price": anchor_price,
            }
        if fused >= 0.55 and direction in ("BUY", "SELL"):
            return {
                "action": direction,
                "signal_type": f"CAUTIOUS_{direction}",
                "direction": direction,
                "anchor_price": anchor_price,
            }

        return {
            "action": "HOLD",
            "signal_type": "WAIT_CONFIRMATION",
            "direction": "NEUTRAL",
            "anchor_price": anchor_price,
        }

    # -------------- 7) Rationale Builder --------------
    def _rationale(self, decision, n_of, n_fp, n_tr, coherence, rules_eval) -> str:

        parts = []
        st = decision["signal_type"]
        if decision["action"] == "HOLD":
            parts.append(f"{st} car")
        else:
            parts.append(f"{st} car")

        dir_of = (
            "bullish"
            if n_of["dir"] > 0
            else ("bearish" if n_of["dir"] < 0 else "neutre")
        )
        dir_fp = (
            "bullish"
            if n_fp["dir"] > 0
            else ("bearish" if n_fp["dir"] < 0 else "neutre")
        )
        trig_txt = (
            "aucun trigger"
            if n_tr["dir"] == 0
            else f"trigger={'BUY' if n_tr['dir']>0 else 'SELL'} conf={n_tr['score']:.2f}"
        )

        parts += [
            f"orderflow {dir_of} (score={n_of['score']:.2f}, Δ={n_of['delta_total']:.2f})",
            f"footprint {dir_fp}{' avec ABSORPTION' if n_fp['absorption'] else ''}",
            trig_txt,
            f"cohérence={coherence['agreement']:.2f}, votes={coherence['votes']}",
        ]
        if rules_eval["reasons"]:
            parts.append("règles=" + ",".join(rules_eval["reasons"]))

        return " | ".join(parts)

    # -------------- Trailing-only (scalping) --------------
    def _suggest_trailing(
        self,
        fused: float,
        cfg: Dict[str, Any],
        strategy_config: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        price_step = _to_float((strategy_config or {}).get("price_step", 0.01), 0.01)
        base_steps = _to_float(cfg.get("base_trail_steps"), 20.0)
        mult = (
            0.8
            if fused >= 0.80
            else (1.0 if fused >= 0.65 else (1.1 if fused >= 0.55 else 1.25))
        )
        dist = max(price_step, base_steps * price_step * mult)
        return {
            "distance": float(dist),
            "unit": "price",
            "note": "Scalping: trailing-stop only, no TP.",
        }

    # -------------- helpers --------------
    def _mk_hold(
        self, signal_type: str, rationale: str, quality: Dict[str, Any], **kw
    ) -> Dict[str, Any]:
        out = {
            "ok": False,
            "action": "HOLD",
            "signal_type": signal_type,
            "direction": "NEUTRAL",
            "fused_confidence": 0.0,
            "anchor_price": None,
            "rationale": rationale,
            "components": kw.get("components", {}),
            "consensus": kw.get(
                "coherence", {"maj": "TIE", "agreement": 0.0, "votes": []}
            ),
            "quality": quality,
            "suggested_trailing": {
                "distance": 0.0,
                "unit": "price",
                "note": "No decision",
            },
        }
        # enrich si fournis
        if "n_of" in kw or "n_fp" in kw or "n_tr" in kw:
            out["components"] = {
                "orderflow": kw.get("n_of"),
                "validator": kw.get("n_fp"),
                "trigger": kw.get("n_tr"),
            }
        if "fused" in kw:
            out["fused_confidence"] = float(kw["fused"])
        return out


# === HOTFIX: bind des helpers module-level comme méthodes d'instance ===
FusionManager._ensure_timestamp = _ensure_timestamp
FusionManager._hashable = _hashable
FusionManager._coh_key = _coh_key
FusionManager._coherence_cached = _coherence_cached
FusionManager._degraded_mode_decision = _degraded_mode_decision
FusionManager._cross_system_validation = _cross_system_validation
FusionManager._adaptive_weights = _adaptive_weights
FusionManager._update_metrics = _update_metrics
